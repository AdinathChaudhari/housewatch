<!--
GitHub metadata (apply by hand in repo Settings — not rendered on this page):

About: Poll a router's UPnP counters, survive 32-bit wraps and reboots, and
track WAN usage against a cap — a local daemon, CLI & dashboard on SQLite
and Flask.

Topics (17, ranked): bandwidth-monitoring, wan-monitoring, data-usage-tracker,
  network-monitoring, data-cap, upnp, upnp-igd, router, python, sqlite,
  flask, macos, cli, daemon, launchd, menu-bar-app, self-hosted

Social preview: docs/media/hero.png — set it in Settings > Social preview.
-->

# housewatch — Whole-House WAN Data Tracker

**Every gigabyte counted — or honestly marked unknown.**  
*A local daemon that polls your router's own UPnP counters, survives 32-bit wraps and reboots, and never guesses past what it can prove.*

![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![macOS](https://img.shields.io/badge/platform-macOS-lightgrey)

<p align="center">
  <img src="docs/media/hero.png" alt="housewatch — a usage ring on a dark field, one segment left unlit" width="720">
</p>

Routers already count every byte in and out on the WAN port — housewatch just reads that counter honestly. It corrects for the counter wrapping at 2³² bytes and for router reboots, tracks the result against a billing cycle and an optional cap, and refuses to invent a number for any stretch it didn't actually observe.

## The map

| I want to… | Command | What it does |
|---|---|---|
| **Check** usage right now | `python -m housewatch status` | Today's usage, cycle vs. cap, last-sample age — read-only |
| **See** it in a browser | `python -m housewatch serve` | Local dashboard at `127.0.0.1:8321` |
| **Run** it unattended | `./install.sh` | Installs the collector LaunchAgent, survives logout/reboot |
| **Glance** at it | `./install.sh --with-menubar` | Menu-bar readout; its "Open Dashboard" starts the web UI for you |
| **Diagnose** a problem | `python -m housewatch doctor` | Checks router reachability, db, LaunchAgent in one pass |

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/python3 -m housewatch once      # one real poll against your router, prints the result
```

1. **Answer** nothing — it discovers your router over UPnP and just runs.
2. **Read** the printed result: which router it found, what it read, and whether that reading was `baseline` (first-ever, no delta yet) or a classified interval.

That's it — everything below is detail.

---

## Demo

Real command output, with every number re-synthesized so no single connection's readings leak — the shape is what a run actually prints.

A fresh clone, first poll ever recorded (`baseline` — no prior reading to diff against):

```
$ .venv/bin/python3 -m housewatch once
source: UPnP IGD @ auto-detected gateway
router: Example Router (Example Corp ER700) at http://192.0.2.1:1900/a1b2c3/gatedesc.xml
kind:       baseline
start_ts:   1700000000.000
end_ts:     1700000000.000
duration:   0s
down:       unknown
up:         unknown
note:       first sample
first reading recorded — usage deltas start with the next poll
```

Ten seconds later, a real interval:

```
$ .venv/bin/python3 -m housewatch once
source: UPnP IGD @ auto-detected gateway
router: Example Router (Example Corp ER700) at http://192.0.2.1:1900/a1b2c3/gatedesc.xml
kind:       ok
start_ts:   1700000000.000
end_ts:     1700000010.105
duration:   10s
down:       12.00 MB (12000000 B, 0 wrap(s))
up:         1.50 MB (1500000 B, 0 wrap(s))
note:       gap 10.1s within certainty window
```

`status` after a few weeks of running unattended, including one honestly-reported gap:

```
$ python -m housewatch status
== Today ==
  down: 3.87 GB  up: 412.60 MB
  unknown time: 1h48m  (>= prefix warranted)
== This billing cycle ==
  >= 61.20 GB used of 500.00 GB  (2026-08-01 .. 2026-08-31)
  days: 12.4 / 31.0
  projected (estimate): 153.08 GB
  unknown time this cycle: 1h48m
== Collector ==
  last sample: 6m12s ago
  gap intervals in last 30 days: 1
  router: Example Router (Example Corp ER700)
```

That `1h48m` and the leading `>=` are the point: the Mac slept for an hour and forty-eight minutes at some point this cycle, housewatch has no reading for that stretch, and it says so instead of quietly treating it as zero usage.

---

## How it works

The router's WAN byte counters are unsigned 32-bit — they wrap to zero every **2³² bytes (~4.295 GB)**, which at gigabit speeds is under 35 seconds. Read two counter values far enough apart and a wrapped-then-grown counter can look exactly like a normal increase, silently undercounting by one whole wrap with no visible sign anything went wrong. Every poll runs through a classifier that only ever reports a number it can prove, and marks everything else `unknown` rather than guessing:

| Kind | What's proven | What's honestly unknown |
|---|---|---|
| `ok` / `ok_wrap` | Gap short enough that at most one wrap was physically possible — the byte delta is exact | Nothing — this is the certain case |
| `recovered` | Gap too long for byte-only certainty, but the packet counters (which move far slower) narrow it to exactly one possible wrap count | Nothing — proven a different way |
| `suspect` | Gap bounded to at most one *extra* wrap, but packet recovery didn't resolve it | Could be undercounting by up to one wrap (~4.295 GB); recorded and flagged, not dropped |
| `gap` | — | Two or more wraps were physically possible; bytes are `unknown`, not zero |
| `reset` | A router counter-clear or reconnect was detected (via uptime and/or packet evidence) | Bytes on either side of the reset; recorded as `unknown`, never estimated |
| `baseline` | The very first reading | Everything before it — there's no prior reading to diff against |

The poll interval (10s by default) is chosen so two back-to-back missed polls still fit inside the certainty window at gigabit speed. Lowering `max_link_mbps` to "be more careful" does the opposite — it widens every window above and turns gaps that should be `gap`/`suspect` into confidently-wrong `ok` numbers exactly one wrap short. See `CLAUDE.md` for the full state machine and its test coverage if you're touching `housewatch/counters.py`.

---

## Configure

No config file is required — housewatch runs on built-in defaults (no cap, calendar-month billing, auto-detected router and timezone) with zero setup. The two facts that are genuinely yours to set — because housewatch has no way to know your ISP's contract — go through the CLI, not hand-edited TOML:

```bash
python -m housewatch config show               # every value, and whether it's a default or from your file
python -m housewatch config set-cap 1000        # decimal GB — 1000 for a 1 TB/month plan
python -m housewatch config set-cycle-day 5     # your ISP's billing reset day (1-28)
python -m housewatch config clear-cap           # remove the cap; usage keeps being tracked either way
```

Everything else — poll interval, timeouts, retention, the dashboard port — lives in `~/.config/housewatch/config.toml`, documented key-by-key in [`config.example.toml`](config.example.toml). Copy that file to get started by hand, or just write the keys you want to override; anything absent falls back to its default. Note: `config set-*`/`clear-*` rewrite the file's values exactly but do not preserve hand-written comments (the standard library has no round-tripping TOML writer) — the annotated copy stays in `config.example.toml`.

## Requirements

- A router that speaks **UPnP IGD** with its byte counters enabled (nearly all consumer routers do; UPnP is usually on by default and toggled in the admin UI if not). This is a standard, not a specific brand or model — housewatch discovers the device dynamically at every startup via SSDP, never a hardcoded address.
- **Python 3.11+**.
- **macOS today** — the LaunchAgent installer, the optional menubar app (`rumps`), and the paths in `install.sh` all target macOS. `housewatch/detect.py`'s gateway detection already has a Linux branch (`ip -4 route show default`) written for a planned Raspberry Pi port, but the installer and menubar are not ported yet.

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

> The router's counters are not your ISP's billing meter — they include router-originated traffic and count at a different point in the network than your provider does. Expect a small (typically a few percent) divergence from whatever your ISP reports; treat the cap and projection numbers as advisory, not authoritative.

To survive logout/reboot, run `./install.sh` from the checkout. It resolves an interpreter (`HOUSEWATCH_PYTHON`, else this repo's own `.venv`, else whatever `python3` is on `PATH`), verifies it can actually import `housewatch` *before* touching launchd, renders `com.housewatch.collector.plist` with real absolute paths, and bootstraps it. Safe to re-run any time — it boots out the old instance first.

The **menu-bar app is a second, optional agent**: `./install.sh --with-menubar`
(or `make install-menubar`) installs both; `--menubar-only` installs just it.
It shows today's usage — or cycle progress once a cap is set — and its *Open
Dashboard* item starts the web server itself, so you never have to leave
`serve` running in a terminal. They are deliberately two agents rather than
one: the collector holds the single-writer lock on the database, and a
rumps/PyObjC failure drawing a status item must never be able to interrupt
polling. Stop or start just the menu bar with `make menubar-stop` /
`make menubar-start`. `./uninstall.sh` removes both. Stop it with `launchctl bootout gui/$(id -u)/com.housewatch.collector`, never by killing the process (`KeepAlive={SuccessfulExit: false}` means a killed process relaunches, but a clean `bootout` stays stopped). `./uninstall.sh` removes it entirely.

Where things end up:

| What | Where |
|---|---|
| Config | `~/.config/housewatch/config.toml` |
| Database | `~/Library/Application Support/housewatch/housewatch.db` (SQLite, WAL mode) |
| Logs | `~/Library/Logs/housewatch/housewatch.log` (rotated, 5 MB × 5 backups) |
| Pidfile / single-instance lock | `~/.config/housewatch/housewatch.pid` |

---

## Troubleshooting

Run `python -m housewatch doctor` first — it checks everything below in one pass and prints a fix line under anything that fails.

- **Router not discovered / SSDP blocked.** `doctor` reports a discovery-stage failure (e.g. `SSDP M-SEARCH for ... got no responses within 3.0s`) rather than a plain connection error — this usually means UPnP is off in the router's admin settings, or the network blocks UDP multicast (some guest networks and VLANs do this by design). There's no workaround from housewatch's side beyond enabling UPnP or being on a network where multicast reaches the router.
- **VPN up / no default route.** A full-tunnel VPN replaces the default route, so the router becomes unreachable — `doctor` and `once` report `no default IPv4 gateway found (no route to a router — off-network, or a full-tunnel VPN with no local default route)`. This is expected, not a bug; housewatch backs off and retries, and the gap in between is recorded honestly.
- **Collector not running / stale numbers.** `python -m housewatch status` shows "no sample recorded yet" if it never ran, or flags the last-sample age with `(!)` once it's past 5× the poll interval. Check `~/Library/Logs/housewatch/housewatch.log` for why it stopped, and `doctor`'s LaunchAgent check for whether it's even supposed to be running unattended.
- **"another housewatch instance already holds the lock."** `run` and `once` both take an exclusive pidfile lock before opening the database read-write, so two writers can never race. The message names the PID holding it — stop that instance first (`launchctl bootout ...` for the LaunchAgent, or Ctrl-C/kill for a foreground `run`/`once` you started by hand).

---

## FAQ

**Does this work with my router?**
If it speaks UPnP IGD (`WANIPConnection`/`WANCommonInterfaceConfig`) with the byte counters enabled, yes — this is a standard, not a specific brand or model, and discovery is fully dynamic. Run `python -m housewatch doctor` to check; its `identity:` line names your actual device so you can confirm it found the right one.

**Why is there no cap by default?**
Because housewatch has no way to know your ISP's plan or billing date, and a wrong guess would silently misreport your cycle. Usage is tracked with or without a cap — set one any time with `config set-cap`.

**What happens while my Mac sleeps?**
Nothing gets polled, so nothing gets recorded — the stretch shows up as unknown time in `status`, not as zero usage. When the Mac wakes, polling resumes on the next scheduled interval, and short gaps (up to ~47.7 minutes at gigabit) can still often be recovered exactly via the packet-count check described above.

---

## License

No license file has been added to this repository yet — until one is, treat the code as all-rights-reserved rather than assuming a permissive license.

## Acknowledgements

- [Flask](https://flask.palletsprojects.com/) — the local dashboard
- [rumps](https://github.com/jaredks/rumps) — the macOS menubar app
