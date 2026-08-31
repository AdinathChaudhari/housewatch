# housewatch

Whole-house WAN usage tracker: polls the router's UPnP counters, reconstructs
true byte totals across 32-bit counter wraps and router reboots, tracks a
billing cycle and cap. `README.md` is the user-facing entry point (run/
install/troubleshoot); this file is for a future session touching the code.

## Defensive UPnP handling (do not re-derive, re-verify if these seem wrong)

UPnP IGD is a standard, but consumer firmware implements it inconsistently.
These are general rules, each grounded in something actually observed on real
hardware during development (one TP-Link Aginet unit, among others) — treat
them as "the wire is not as clean as the spec promises" rules that apply
broadly, not as facts about one named device this code was written to match.

- **Never assume or hardcode the router's address.** The default gateway is
  discovered at runtime (`detect.default_gateway()` — `route -n get default`
  on macOS, `ip -4 route show default` on Linux), never configured or cached
  across a failure. UPnP IGD itself typically runs on port 1900 with no
  authentication required — that much is the standard, not house-specific.
- **SSDP `LOCATION` URLs commonly embed a path token that is regenerated on
  reboot on at least some firmware** (e.g.
  `http://192.0.2.1:1900/<token>/gatedesc.xml` — `192.0.2.1` is the RFC 5737
  documentation address used here as a placeholder, not a real gateway).
  Never hardcode the token — always re-derive it. `igd.py`'s 3-stage retry
  ladder (cached URLs → re-fetch gatedesc from the cached LOCATION → full
  SSDP) exists specifically to re-derive it after a reboot without
  hardcoding anything.
- **Some firmware ships a malformed namespace in `GetStatusInfoResponse`**
  (observed: `xmlns:u="urn:schemas-upnp-org:service:GetStatusInfo:1"` where
  `WANIPConnection:1` was expected). `igd.py`'s XML helpers therefore match
  elements by **local tag name only**, ignoring namespace — do not "fix"
  that into a namespace-strict lookup; it will break real routers that do
  this.
- **Reported Layer1 up/downstream bitrate fields are placeholder values on
  at least some firmware** (observed: a flat 512000 regardless of actual
  link speed, on one unit) and must never be used for anything. The real
  planning rate is `config.max_link_mbps` (default 1000, a typical GbE WAN
  port ceiling), which every wrap-safety window in `counters.py` derives
  from — see "`max_link_mbps` is a physical ceiling" under Landmines. This
  is also why `detect.py` deliberately does not attempt link-speed
  detection at all — a router's own reported number cannot be trusted, and a
  future auto-suggestion mechanism may only ever propose *raising* this
  value, never lowering it silently.
- **A router's identity is for display only, never for behavior.**
  `gatedesc.xml`'s `friendlyName`/`manufacturer`/`modelName` are parsed by
  `detect.parse_router_identity` purely so a user can confirm the right
  device and so a bug report names real hardware. Parsing is total by
  design: malformed XML or missing fields degrade to `None`s (rendered as
  "unidentified UPnP device"), never an exception — identity must never be
  allowed to fail a poll.

## The counter-wrap problem — read this before touching counters.py

The router's WAN byte counters are unsigned 32-bit (`ui4`): they wrap at
`2**32` bytes (~4.295 GB). At gigabit speeds that's under 35 seconds. A
naive "read counter, subtract previous" collector doesn't just break on a
wrap — it can come back with a value that looks like a **perfectly normal
increase** while silently undercounting by exactly one modulus, because if
the true delta lands in `[2**32, 2*2**32)` the wrapped-around counter is
still numerically larger than the previous reading. There is no way to
detect that case from the byte counters alone after the fact.

The whole defense against this lives in **`housewatch/counters.py`**, and
only there. It is a pure function (`classify(prev, curr, thresholds,
modulus)`) — no I/O, no clock reads, every input a plain value — which is
what makes every branch reachable from a unit test with plain ints
(`tests/test_counters.py`). If you change wrap/reboot/gap
handling, change it there and prove it with a test; nothing else in the
codebase should need to know about `2**32`.

What it does, in order, per poll:

1. **Uptime decides reboot vs. wrap, before any byte arithmetic runs.**
   `WANIPConnection GetStatusInfo NewUptime` is the sole discriminator. A
   reboot resets the byte counters toward zero; a wrap resets them by
   subtracting a modulus while wrapping through it. Both look similar in
   the raw numbers, but only a reboot also resets uptime. This is checked
   *first*, and wins even when the byte pattern would also look like a
   valid wrap — see `test_reset_wins_even_when_bytes_also_look_like_a_valid_wrap`.
   Two reboot signatures: `hard` (uptime dropped), `soft` (uptime grew but
   by less than wall-clock elapsed — a reconnect happened and un-happened
   inside a long gap without the poll ever seeing the drop).

   **Uptime is not the only witness, and relying on it alone was a real
   shipped bug.** Two more checks run immediately after, because a stats
   clear in the admin UI (spec case E4) or a reconnect timed to slip past
   both uptime tests leaves uptime perfectly continuous while the byte
   counters jump back toward zero — which read as a wrap invents ~4.29 GB
   per direction and books it against the cap, under `suspect`, a kind
   every consumer treats as a LOWER bound:
   - **B2b, packet discontinuity (proof).** Inside the packet window a
     packet counter cannot advance a full modulus, so its mod-modulus delta
     is EXACT — and an exact delta above the physical packet-rate ceiling is
     impossible for continuous counters. That is proof of a reset, not a
     hint. It cannot false-positive on a genuine wrap, where the packet
     counter keeps increasing normally.
   - **B2c, packets unavailable (honest unknown).** If the optional packet
     SOAP calls failed, and a counter decreased while the PREVIOUS reading's
     uptime was short enough to have hidden a reconnect, "wrapped" and
     "reset" are equally consistent with the evidence. The interval becomes
     `anomaly` with NULL bytes rather than crediting a near-modulus number.

   - **B2d, reset signature (honest unknown).** Closes what used to be the
     blind spot below. With no packet corroboration available, if BOTH
     directions decreased in the same interval AND both mod-modulus
     residuals exceed half a modulus (`RESET_SIGNATURE_FRACTION`), the
     counters were reset, not wrapped. The two directions carry very
     different volumes, so both crossing the modulus boundary in one poll
     would need them simultaneously within one interval's traffic of it; a
     reset zeroes both at once by construction. Interval becomes `reset`
     with NULL bytes.

     This is deliberately not a rate argument. The physical ceiling already
     covers gaps short enough for rate to be decisive, and it goes slack in
     the ~34-120 s band -- which is precisely the band B2d covers.

   Formerly a known blind spot, now closed by B2d: a counter clear with
   fully continuous uptime AND no packet counters at all. Before B2d this
   was booked as ~4.29 GB per direction of traffic that never happened --
   as `ok_wrap` (labelled certain) at ~34 s, and as `suspect` above it,
   which inverted `suspect`'s own lower-bound contract. Pinned by
   `tests/test_counters_reset_signature.py`; mutation-checked.
2. **Once reboot is ruled out**, the classifier sizes the gap against the
   physical byte-rate ceiling (`config.max_link_mbps`) to decide which of
   four tiers applies:
   - `ok`/`ok_wrap` — gap short enough that at most one wrap is physically
     possible (**certainty window**, `counters.certainty_window_s()` —
     34.36 s at 1 Gbps default). The wrap count is provably exact.
   - `recovered` — gap too long for byte-only certainty, but the packet
     counters (which move ≥64× slower) narrow it to exactly one answer.
     Proven by construction: wrap candidates sit exactly one modulus apart,
     so a packet-derived byte envelope can fit at most one of them. Usable
     out to ~47.7 min. **This proof is conditional** on the router's two
     counters sharing accounting units — a firmware reporting GRO-coalesced
     super-packets would resolve confidently to the wrong candidate. That
     assumption is measured continuously rather than asserted; see the
     `packet_recovery_enabled` landmine below.
   - `suspect` — gap bounded to at most one *extra* possible wrap (under
     68.7 s at defaults) but packet recovery didn't apply/resolve. A real
     number is recorded, flagged as a possible undercount, and included in
     totals — never silently dropped, never silently trusted either.
   - `gap` — two or more wraps are physically possible and packet recovery
     didn't resolve it. Bytes are `None`. **Never guess past this point.**
3. **Unknown is a first-class value, never zero.** `IntervalResult.down_bytes`/
   `up_bytes` are `None` for `gap`/`reset`/`anomaly`/`baseline` — enforced at
   the SQLite schema level too (`CHECK ((down_bytes IS NOT NULL) = (kind IN
   (...)))` in `store.py`). Any UI or aggregate code that coerces `None` to
   `0` reintroduces the exact bug this project exists to prevent.

The 10 s default poll interval is not arbitrary — it's chosen so that even
two consecutive missed polls (30 s of silence) still sit inside the 34.36 s
certainty window at gigabit. `config.validate()` enforces the same
inequality (`poll_interval_s <= 2**32*8 / (max_link_mbps*1e6) / 3`) as a
hard startup error, so a misconfigured poll interval fails loudly instead of
corrupting data quietly.

## Module map

| File | Owns |
|---|---|
| `housewatch/model.py` | `Reading`, `IntervalResult`, `WanCounterSource` protocol, typed exceptions. Zero I/O, zero third-party imports — pure types only. |
| `housewatch/detect.py` | Environment auto-detection: default gateway, router identity for display, local timezone. Stdlib-only leaf module — imports nothing from `housewatch.*`, so `config.py` and `igd.py` both import from it without an import cycle. Every public function is total: it degrades to a documented default (usually `None`) instead of raising. Deliberately does not attempt link-speed detection — see the Layer1-bitrate note above. |
| `housewatch/config.py` | Load/save/validate TOML config. Owns the wrap-safety inequality and the billing-day clamp. `config show`/`set-cap`/`clear-cap`/`set-cycle-day` (in `cli.py`) are the supported way to edit it without hand-touching TOML. |
| `housewatch/igd.py` | UPnP IGD client (`UpnpIgdSource`). SSDP discovery, gatedesc.xml parsing, SOAP calls, the 3-stage retry ladder, router identity capture. All I/O for talking to the router lives here and nowhere else — gateway resolution and identity parsing delegate to `detect.py`. |
| `housewatch/counters.py` | The wrap/reboot/gap state machine (`classify`). Pure. **The whole ballgame** — see above. |
| `housewatch/billing.py` | Pure calendar math: billing-cycle bounds, local-day bucketing, midnight-splitting. Separate from `counters.py` because it's a different pure domain (dates, not bit-width arithmetic). Half-hour-offset zones exist (UTC+05:30, UTC+09:30), which is why everything here is local-time-first, never UTC-hour buckets. |
| `housewatch/store.py` | SQLite schema, the one-transaction `record_poll`, and every read query (`day_usage`, `cycle_summary`, `rate_series`, `notable_intervals`, ...). WAL mode; single rw writer, N readonly readers. |
| `housewatch/collector.py` | The daemon loop (`Collector.run_forever`/`poll_once`). Scheduling, backoff, event-logging policy. No wrap arithmetic, no SQL — wires clock + source + classifier + store together. |
| `housewatch/cli.py` | `python -m housewatch <run\|once\|status\|serve\|doctor\|config>`. Also owns the pidfile single-instance lock and log setup. |
| `housewatch/__main__.py` | Thin `python -m housewatch` shim; delegates to `cli.main`. |
| `housewatch/webapp.py` | The Flask dashboard: `create_app`, the `/api/*` JSON routes, `/healthz`. Opens a fresh READONLY `Store` per request. |
| `housewatch/menubar.py` | The rumps menubar app, plus the pure `format_*`/`Snapshot` layer it is tested through, plus the `launchctl` remote control for the collector. |
| `housewatch/templates/index.html`, `housewatch/static/{app.js,style.css}` | Dashboard assets, served locally (no CDN — the dashboard must work offline). |

Also on disk at the project root: `com.housewatch.collector.plist` (the
collector's LaunchAgent template), `housewatch/menubar_launchagent.plist`
(the menubar's, a separate optional agent — `./install.sh --with-menubar`
renders and loads it; plain `./install.sh` installs the collector only, so a
headless or Linux host is never blocked on rumps), `install.sh`, `uninstall.sh`,
`Makefile`, `config.example.toml`, `docs/DECISIONS.md`.

**LaunchAgent labels are `com.housewatch.collector` and
`com.housewatch.menubar`.** `cli.LAUNCHD_LABEL` must equal the `Label` in
`com.housewatch.collector.plist` and the `LABEL` in `install.sh`,
`uninstall.sh` and the `Makefile` — they disagreed once and it silently
broke the menubar's Start/Stop Collector control and made `doctor` report a
healthy install as FAIL. `tests/test_cli.py` now pins all four.

## Config format — deviates from an earlier JSON spec

Config is **TOML** at `~/.config/housewatch/config.toml`, not JSON under
`~/Library/Application Support/`. `billing_cycle_start_day` is clamped to
**1–28**, not 1–31 (see `config.MAX_BILLING_CYCLE_START_DAY`'s docstring —
capping avoids a silent per-month shift in Feb/Apr/Jun/Sep/Nov). This was a
deliberate, documented divergence from an earlier architect spec that named
JSON/1–31; `config.py`'s module docstring records the objection. If you're
reconciling against an older design doc, this file (and the running code)
is the source of truth, not the doc.

## Running tests

```
.venv/bin/python3 -m pytest tests/ -q   # or: python3 -m pytest tests/ -q inside an activated venv
```

Run `pytest` for the current count rather than trusting a number written
here. **The default run touches no network at all.** The one live test,
`tests/test_igd.py::test_live_read_against_real_router`, is opt-in:

```
HOUSEWATCH_LIVE_ROUTER_TEST=1 .venv/bin/python3 -m pytest tests/ -k live
```

Without that variable it skips unconditionally, so the suite behaves
identically whether or not a real router is reachable. Everything else in `test_igd.py`
uses monkeypatched SOAP/SSDP responses, including canned copies of the
malformed `GetStatusInfoResponse` namespace.

Note the paths: the project root is this directory, so the architect spec's
`housewatch/tests/` IS this checkout's `tests/` — run pytest from the
project root (`pyproject.toml`'s `testpaths = ["tests"]`), not from the
repo root above it.

`counters.py` is the acceptance gate: `tests/test_counters.py` covers every
named branch (baseline, clock anomaly, hard/soft reset, the "reset wins even
when bytes also look like a valid wrap" nasty case, certain/ok/ok_wrap,
packet-recovery success and contradiction, all-or-nothing per-direction
rejection, suspect, unresolvable gap, exact-boundary wrap, and a
modulus-parameterized 64-bit passthrough) plus a wrap scenario with the
same shape as one captured live during development (values
re-synthesized): bytes `3700000000 -> 800000000`, uptime `+804s`, no
reboot — classifies `recovered`, true delta 1.39 GB, resolved via the
packet envelope.

## Live checks (need the real router / real network)

- `python -m housewatch doctor` — full live check: SSDP discovery + a real
  SOAP read, plus db writability and LaunchAgent status. Safe to run
  anytime; makes no persisted writes to the usage db.
- `python -m housewatch once` — one real poll, **persisted**. Use this to
  verify the classifier against real router state, not `doctor` (which
  doesn't write).
- `tests/test_igd.py::test_live_read_against_real_router` — see above.

None of these are needed for routine `counters.py`/`billing.py`/`store.py`
work; those three are pure and fully covered by mocked/synthetic tests.

## Landmines

- **The menubar and the collector are two agents on purpose — keep them that
  way.** `install.sh` installs the collector by default and the menubar only
  behind `--with-menubar`/`--menubar-only`; `uninstall.sh` removes both
  (menubar first, since it offers Start/Stop Collector controls that would
  otherwise point at a service already gone). Merging them into one agent
  would put a rumps/PyObjC status-item failure in the same process as the
  single db writer — a UI crash would then cost data, which is exactly the
  trade this split exists to avoid. `rumps` is checked at install time only
  when the menubar is actually requested.

- **`counters.py` must stay pure.** No `time.time()`, no network, no sqlite
  inside that module or its helpers. The instant it needs a fixture instead
  of a plain int to test a branch, the "every branch is a unit test" property
  that makes this module trustworthy is gone.
- **Uptime is checked before byte arithmetic, always.** Don't reorder
  `classify()` to check wrap-ability first as an optimization — the nasty
  case (uptime says reboot, bytes look like a clean wrap) depends on reboot
  winning outright. There's a test for this; if you change the order, expect
  it to fail.
- **`igd.py`'s XML parsing is local-tag-name-only, not namespace-aware.**
  Making it namespace-strict "for correctness" will break real communication
  with routers that exhibit the malformed-namespace behavior noted above.
- **Never hardcode the gatedesc/controlURL path token.** It's per-boot
  random on at least some firmware; `igd.py`'s 3-stage retry ladder (cached
  URLs → re-fetch gatedesc from cached LOCATION → full SSDP) exists
  specifically to re-derive it after a reboot without hardcoding anything.
- **Single-writer discipline is enforced, not just conventional.**
  `cli.py`'s `_pidfile_lock()` takes an advisory `flock` on
  `~/.config/housewatch/housewatch.pid` before any read-write `Store` opens
  — `run` and `once` both go through it, so two overlapping writers can't
  both hold write access. Anything that opens a `Store(readonly=False)`
  outside `cli.py`'s existing commands should go through the same lock, or
  through neither (a throwaway script against a copy of the db).
- **`None` bytes must never become `0`.** Enforced at the schema level
  (`CHECK` constraints tying `down_bytes IS NOT NULL` to the interval kind)
  as well as by convention — but any new aggregate query in `store.py` that
  does a plain `SUM(down_bytes)` across kinds will silently drop `gap`/
  `reset`/`anomaly` rows from the sum, which is correct (they contribute
  `NULL`, not `0`, to a `SUM`) but means the *count* of unknown seconds
  still needs to be surfaced separately — see `DayUsage.uncovered_s` and
  `gap_s` for the pattern.
- **The LaunchAgent plists embed an absolute interpreter path** (not bare
  `python3` — launchd's environment does not reliably share your shell's
  `PATH`, and a venv Python that satisfies PEP 668 needs an explicit path
  anyway). `install.sh` renders `{PYTHON}`/`{WORKDIR}`/`{HOME}` at install
  time from wherever the checkout actually is (honoring `HOUSEWATCH_PYTHON`
  if set), so moving the checkout means re-running `./install.sh`, never
  hand-editing a plist. Both plists use `KeepAlive={SuccessfulExit: false}`
  (the standard pattern for a background service that should stay stopped
  after a deliberate quit) so a clean Quit stays quit until next login. Stop
  with `launchctl bootout`, never by killing the process.
- **`counters.Thresholds.packet_recovery_enabled` is a MEASURED verdict, not
  a setting.** The 'recovered' tier's proof is conditional on the router's
  byte and packet counters sharing accounting units (every packet 64–1518
  bytes). `Collector._calibrate_packet_units` tests that on every
  provably-exact `ok` interval and permanently disables packet recovery
  (meta key `packet_recovery_disabled`, plus a `calibration` events row) the
  first time an observed bytes-per-packet lands outside those bounds. Do not
  "helpfully" re-enable it; clear the meta key by hand if you are sure.
- **`max_link_mbps` is a physical ceiling, never an ISP plan speed.**
  Lowering it widens every certainty window and turns unrecoverable gaps
  into confidently-wrong `ok` numbers exactly one modulus short.
  `config.validate` floors it at `MIN_SAFE_LINK_MBPS` (1000) unless
  `i_know_my_link_is_slower = true` is also set, and `load()` logs a WARNING
  whenever the opt-in is in use.
