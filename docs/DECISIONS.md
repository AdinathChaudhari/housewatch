# Decisions

Why a value is that value, matching the pattern in google-drive-suite's
`docs/DECISIONS.md`. One entry per real decision: the value, the
alternatives rejected, and what would change the answer. Anything unmeasured
is marked as such — nothing here is invented.

---

## D-001 — Poll interval: 10 seconds

**Decision:** `poll_interval_s = 10.0` default, validated at startup against
`poll_interval_s <= (2**32 * 8) / (max_link_mbps * 1e6) / 3`.

**Why:** The router's byte counters are `ui4` — they wrap at `2**32` bytes
(4,294,967,296). At the planning rate (1 Gbps, a typical GbE WAN port
ceiling — *not* the bogus 512000 bitrate some firmware reports for this field), a
counter wraps in `2**32 * 8 / 1e9 = 34.36` seconds. That number,
`certainty_window_s()` in `counters.py`, is the hard boundary past which a
single-wrap correction (`new < prev => add one modulus`) stops being
*provably* correct — beyond it, two-plus wraps become physically possible
and the true delta is ambiguous from bytes alone.

10 s was chosen, not something closer to 34 s, because the collector must
tolerate **missed polls**, not just successful ones on schedule. At 10 s,
two consecutive failures (a 30 s gap) still lands inside the 34.36 s
certainty window — the classifier still proves the delta exactly, no
degradation to `suspect`/`gap`. The `/3` divisor in the validation
inequality builds that same two-miss margin into `config.validate()`
itself, so a user who sets a slower interval gets a startup `ConfigError`
naming the actual bound, not silent corruption discovered later as a
mysteriously wrong monthly total.

**Alternatives rejected:**
- **15 s** — only one missed poll (30 s total) before leaving the certainty
  window entirely; a Mac that sleeps and a Wi-Fi that blips make that margin
  too thin.
- **30 s** — zero failure tolerance at 1 Gbps; any single missed poll risks
  ambiguity.
- **5 s** — doubles router request volume (SOAP round trips measured at
  13–35 ms warm) for margin the design doesn't need; 10 s already keeps
  two-miss tolerance.

**Cost, measured:** 5 SOAP calls/poll × 8,640 polls/day = 43,200 requests/day
≈ 0.5 req/s average against the router's UPnP daemon. Zero errors observed
during the probing that produced this number.

**What would change the answer:** A WAN link faster than 1 Gbps (unlikely —
the reference deployment's WAN port is physically GbE) would shrink the certainty window
and require a shorter interval; a slower confirmed link (`max_link_mbps` set
lower) widens it and could justify polling less often, at the cost of a
longer worst-case "how stale can the dashboard be" window.

---

## D-002 — Data source: UPnP IGD, not the admin UI or SSH

**Decision:** Poll the router's UPnP Internet Gateway Device service
(`WANCommonInterfaceConfig`, `WANIPConnection`) over unauthenticated SOAP.

**Why:** Verified live against the development router: UPnP IGD requires
**no credentials** and exposes exactly the two counters (bytes
received/sent) and the one discriminator (`NewUptime`) this project needs,
at `urn:schemas-upnp-org:service:WANCommonInterfaceConfig:1` /
`WANIPConnection:1`. Some consumer routers' admin UIs use a nonstandard
crypto challenge-response for login — scraping it would mean
reverse-engineering that handshake per firmware and then a web UI's
HTML/JS, for numbers that UPnP already hands over as structured XML with
zero auth. SSH/telnet may also be open on some hardware, but using them
would mean guessing/storing router credentials this project has never
needed, for a login banner that is not part of this task's scope.

**Alternatives rejected:**
- **Admin UI scraping** — requires solving a nonstandard crypto login
  challenge first, and HTML scraping is fragile against firmware updates in
  a way structured SOAP responses are not.
- **SSH/telnet** — requires credentials (never provided or needed by this
  project), and shell-parsing whatever counter command this firmware
  exposes (unverified it even has one) is strictly more fragile than a
  documented UPnP action.

**What would change the answer:** If this firmware's UPnP IGD service were
ever disabled (it's user-toggleable on most consumer routers), the only
fallback would be one of the rejected options above — SSH is the more
plausible fallback since it doesn't need the admin UI's crypto challenge.

---

## D-003 — Reboot vs. wrap: uptime is checked first

> **Superseded in part by D-008.** Uptime is still checked *first* and still
> wins outright, but it is no longer the *sole* discriminator: relying on it
> alone let two reachable reset cases through as ~4.29 GB of invented
> traffic. Read D-008 alongside this entry.

**Decision:** `counters.classify()` checks `WANIPConnection GetStatusInfo
NewUptime` for evidence of a reset (uptime dropped, or grew implausibly
little relative to elapsed wall-clock time) *before* doing any byte-counter
wrap arithmetic — and a reset verdict wins even when the byte pattern would
also look like a valid, resolvable wrap.

**Why:** A wrap and a reboot both make the byte counters go from a large
number toward a small one, so bytes alone can't tell them apart. A reboot
additionally resets the connection uptime; a wrap does not touch uptime at
all. Uptime is therefore the only signal this firmware exposes that
disambiguates the two — and it has to be checked *before* wrap arithmetic,
not as a tiebreaker after, because a byte pattern consistent with "exactly
one wrap happened" is also exactly the pattern a reboot produces if the
reboot happened to land such that post-reboot bytes are numerically close to
`(pre-reboot bytes) mod 2**32`. There is a live test for this
(`test_reset_wins_even_when_bytes_also_look_like_a_valid_wrap`) proving the
order matters, not just the check's existence.

Two sub-cases, both implemented:
- **Hard reset** — uptime dropped outright (beyond `uptime_jitter_s = 5.0`
  of slack for reporting jitter around the exact reconnect moment).
- **Soft reset** — uptime *grew*, but by less than the wall-clock gap allows
  (beyond `uptime_slack_s = 30.0` plus 2% of the gap) — a reconnect happened
  and un-happened somewhere inside a gap long enough to hide it from a naive
  `curr.uptime < prev.uptime` check.

**Alternatives rejected:**
- **Infer reset from a byte-counter drop alone** — indistinguishable from a
  wrap; this is exactly the bug this project exists to avoid.
- **Poll `GetStatusInfo`'s `NewConnectionStatus` instead of `NewUptime`** —
  untested whether any given firmware flips it around a soft-reset event
  that a long gap could hide; uptime's monotonicity-under-normal-operation
  is a stronger, better-understood signal and was verified live on the
  development router (well over 100 days of continuous uptime observed,
  with the byte counters having wrapped many times over in that span —
  proof uptime and byte-counter resets are genuinely independent signals,
  at least on the firmware this was developed against).

**What would change the answer:** If this firmware were ever found to reset
`NewUptime` on something *other* than an actual reboot/WAN reconnect (a
firmware bug, or a stats-clear button in the admin UI that also touches
uptime), every "soft reset" classification downstream would be wrong. This
is flagged as risk #1 in the original design brief — unverified across many
reconnect events, worth revisiting if `reset` events turn up unexpectedly
often in the `events` table after a week of real running.

---

## D-004 — Unknown is modeled explicitly, never as zero

**Decision:** `IntervalResult.down_bytes`/`up_bytes` are `int | None`, never
defaulted to `0`. Enforced at the SQLite schema level too: `CHECK
((down_bytes IS NOT NULL) = (kind IN ('ok','ok_wrap','recovered','suspect')))`
on the `intervals` table — a `gap`/`reset`/`anomaly`/`baseline` row is
physically incapable of carrying a byte value, not just conventionally
supposed to avoid it.

**Why:** A household data cap defended with a silently-zeroed unknown period
is worse than no measurement at all — it reads as authoritative precision it
doesn't have. If a 3-hour gap (Mac asleep, Wi-Fi off, router mid-reboot)
were recorded as "0 bytes used," the monthly total would be a confident,
wrong number instead of a number with an honestly-labeled hole in it.
`store.DayUsage.uncovered_s` and `gap_s` exist specifically so "we don't
know" and "we know it was zero" are never the same bit of information.

**Alternatives rejected:**
- **Default unknown deltas to 0** — the corruption this whole project exists
  to prevent, just moved one layer up (from silent counter-wrap corruption
  to silent gap-period corruption).
- **Impute an average rate for gap periods and store that as fact** —
  `store.CycleSummary.projected_bytes` *does* do rate-based imputation, but
  only for the forward-looking projection, and it is explicitly labeled an
  estimate in every consumer. The `days`/`intervals` tables themselves never
  store an imputed value as if it were measured.

**What would change the answer:** Nothing — this is a correctness invariant,
not a tunable. Any future consumer of `IntervalResult`/`DayUsage` that
coerces `None` to `0` is introducing a bug, not making a different tradeoff.

---

## D-005 — Retention: 7 / 90 / 90 days (samples / intervals / events)

**Decision:** `retention_samples_days = 7`, `retention_intervals_days = 90`
(hard floor enforced in `config.validate()` at `>= 45`), `retention_events_days = 90`.

**Why:** `intervals` is the source of truth for every usage number the UI
ever shows, and it needs to cover a full billing cycle (up to ~31 days,
`billing_cycle_start_day` clamped to 1–28 so no cycle can straddle more than
~59 days at the absolute extreme, but a normal cycle is ~28–31 days) plus
the 30-day dashboard view, with margin. 90 days covers roughly a full
quarter of history at a modest storage cost (documented estimate in the
schema comments: ~778k rows / ~60 MB at 90 days of 10 s polling —
unmeasured against a real running instance, since this checkout has not run
continuously for 90 days). `samples` is raw forensic data (every poll, not
just classified deltas) — useful for debugging the classifier against real
router behavior, but not something any UI query reads, so 7 days is enough
to catch a live problem without keeping ~60k rows/week around indefinitely.
`events` (state transitions: resets, failure runs, discovery re-runs) is
cheap per-row and small in volume, so it gets the same 90-day window as
`intervals` for a matching "look back one billing cycle plus slack" story.

**Alternatives rejected:**
- **Keep `intervals` for exactly one billing cycle (~31 days)** — leaves no
  slack for a user checking last month's total a few days into the new
  cycle, and provides no margin against a bug being noticed late.
- **Keep `samples` for 90 days like `intervals`** — samples are 5–6 raw
  integers per row with no aggregation value beyond forensic replay of the
  classifier; keeping 90 days of them (vs. 7) is nearly 13× the storage for
  a use case (debugging) that rarely needs to look back further than a
  week.

**What would change the answer:** A billing cycle start day near the 28th
combined with a short month could push the *effective* cycle length close
to 59 days in a pathological back-to-back case — still comfortably under
the 90-day `intervals` retention, but worth re-checking the arithmetic if
`billing_cycle_start_day`'s clamp (D-006) is ever relaxed back toward 31.

---

## D-006 — Config format: TOML, not the originally-specified JSON

**Decision:** Config lives at `~/.config/housewatch/config.toml`, loaded
with the stdlib's `tomllib`, hand-written back out on save (the stdlib has
no TOML writer). `billing_cycle_start_day` is clamped to **1–28**, not
1–31.

**Why:** This implementation was built against a specific, later task
instruction that asked for TOML at `~/.config/housewatch/config.toml` and a
1–28 billing-day range, an owned `config.example.toml` deliverable, and
Python-3.11+-only stdlib tooling (`tomllib` is stdlib as of 3.11 — this
runs on 3.14). An earlier architect-level design doc for this same project
named JSON at `~/Library/Application Support/housewatch/config.json` and a
1–31 range instead. Both cannot be true; `config.py`'s module docstring
documents the conflict explicitly rather than silently picking one. The
1–28 clamp specifically: a start day of 29, 30, or 31 doesn't exist in every
month, and `billing.cycle_bounds()`'s clamp-to-month-end behavior (31 → Feb
28/29) means the *effective* reset day would silently shift in
Feb/Apr/Jun/Sep/Nov for a user who configured 31 expecting "the last day of
the month" — 28 is the largest value that means the same calendar day in
every month, so the config value and the actual behavior never diverge.

**Alternatives rejected:**
- **JSON per the original spec** — would have required either abandoning
  the later, more specific TOML instruction or maintaining two config
  loaders; the later instruction was treated as authoritative since it was
  more specific to this exact file.
- **1–31 with silent clamping** — technically matches the original spec's
  numeric range, but "you typed 31, the app quietly treats it as 28 in
  February" is the kind of surprise a billing boundary should never produce
  without the user asking for it.

**What would change the answer:** If a consumer of this config file
specifically needs JSON (e.g. a future non-Python tool reading it), the
TOML choice would need revisiting — nothing today reads this file except
`housewatch.config`.

---

## D-007 — Dashboard binds to localhost only

**Decision:** `web_host` defaults to `127.0.0.1`, not `0.0.0.0`, and the
config schema/example file documents this as a hard boundary rather than a
default that's safe to change casually.

**Why:** The dashboard (`housewatch/webapp.py`) would
serve household bandwidth usage with zero authentication, by design, for
local convenience. Binding to a real interface would expose that to
anything else on the LAN (or beyond, if the router NATs/forwards the port)
with no login. `config.validate()` does not currently reject a non-loopback
`web_host` outright (documented as "unsafe" rather than enforced) — this is
worth hardening into an actual validation error if/when the dashboard
server is built, per the original spec's binding UI-honesty rules.

**Alternatives rejected:**
- **Bind `0.0.0.0` with a note to firewall it yourself** — pushes a
  security decision onto a user who may not think to make it, for a feature
  (checking usage from another device on the LAN) that isn't a stated
  requirement.
- **Add authentication instead of restricting the bind address** — more
  work for a single-user, single-Mac tool where the actual threat model is
  "don't expose this to the whole LAN by accident," which loopback-only
  already solves without a login flow to build and maintain.

**What would change the answer:** A future explicit requirement to check
usage from a phone/other device on the same LAN would justify either a
LAN-bound host with real auth, or a Tailscale-style private-network
approach — not a bare `0.0.0.0`.

---

## D-008 — Uptime is no longer the *sole* reset discriminator

**Decision:** After the two uptime checks (D-003), `classify()` runs two
more reset checks before any wrap arithmetic is credited:

- **B2b (proof).** Inside the packet window (gap × 1.5 Mpps < 2**32, i.e.
  under 2863 s) a packet counter cannot have advanced a full modulus, so its
  mod-modulus delta is *exact*. An exact delta above the physical packet-rate
  ceiling `(gap + 2) × max_pkts_per_sec` is impossible for continuous
  counters — so the counters were reset. Returns `reset`, NULL bytes.
- **B2c (honest unknown).** With no packet counters available, if a byte
  counter decreased while the *previous* reading's uptime was short enough
  to have hidden a reconnect (`prev.uptime_s < uptime_slack_s + 0.02·gap +
  uptime_jitter_s`), "wrapped" and "reset" are equally consistent with the
  evidence. Returns `anomaly`, NULL bytes.

**Why:** D-003's claim that uptime is the sole discriminator was wrong, and
the failure was measured, not theoretical. Two reachable cases leave uptime
perfectly continuous while the byte counters jump back toward zero:

1. A stats clear in the admin UI (the design brief's case E4).
2. A WAN reconnect timed so that the new uptime still clears both checks.
   Executed: `prev.uptime=5, curr.uptime=34, gap=60` → `hard_reset=False`,
   `soft_reset=False` (threshold 33.8).

Both used to be read as a wrap. Executed against a real `Store` seeded
with an example timezone and a 50 GB cap: one 60-second interval on an
idle line produced `day_usage.down_bytes = 4,257,967,296` and
`cycle_summary.used_bytes = 8,544,634,592`, flipping `over_cap_projected`
to True — 8.54 GB of a 50 GB
cap burned by an interval in which almost nothing moved. Worse, the kind it
landed under was `suspect`, which D-004 and every consumer treat as a
**lower** bound; overcounting there inverts the contract.

The guard that was supposed to catch this (`base_d > ceiling` inside the
`suspect` tier) was provably dead code: `suspect` is only reached when
`max_bytes >= modulus`, and `base_d` is a mod-modulus residual, so
`base_d < modulus <= max_bytes < ceiling` always. Scanning the whole tier's
gap band in 0.01 s steps with the largest possible base found **0** possible
firings.

**Alternatives rejected:**

- **Reject `wrapped_d` in the suspect tier whenever a reset is at least as
  plausible** (`curr_d <= gap·max_bps and base_d > modulus − gap·max_bps`) —
  derives to *always true* in that tier, since `gap·max_bps >= modulus`
  there by definition. That is not a guard, it deletes the tier.
- **Treat any decreasing packet counter as a reset** — not sound. A packet
  counter starting near the top of its range can legitimately wrap inside
  the window. The rate ceiling is the check that actually holds.

**What would change the answer:** A firmware where the packet counters can
legitimately exceed the GbE packet-rate ceiling (they cannot on a 1 GbE WAN
port) would make B2b unsound and require a different witness.

**This blind spot is now closed — see D-011.** It was real: a counter clear
with fully continuous uptime *and* no packet counters was undetectable, and
`igd.read()` makes packets optional, so it was reachable in principle.

---

## D-011 — B2d: a reset is identified by its shape when nothing else can see it

**Decision:** When the packet counters cannot settle an interval, treat a
simultaneous decrease in BOTH directions, where each mod-modulus residual
exceeds `RESET_SIGNATURE_FRACTION` (0.5) of the modulus, as a counter reset:
`kind='reset'`, NULL bytes.

**Why a value of 0.5.** The two cases sit at opposite ends of the range, so
the threshold is not delicate. A reset makes both directions read back near
zero, and a residual of `(0 - prev) mod 2**32` with a modest `prev` comes out
near a *full* modulus -- the measured repro was 37 MB reading back as
4,257,967,296, or 99% of 2**32. A genuine wrap's residual is just the
interval's real traffic, a few percent at most. Anywhere between those is
equivalent; the midpoint is the least arbitrary place to stand.

**Why not a rate argument.** B3a's physical ceiling (`max_bytes_per_sec * gap`)
already covers gaps short enough for rate to be decisive. Measured on the
shipped thresholds, the phantom survived exactly where that ceiling goes
slack:

| gap | before B2d | |
|---|---|---|
| 10-30 s | `anomaly`, NULL | honest, ceiling tight |
| ~34 s | `ok_wrap`, 4,257,967,296 | phantom, labelled CERTAIN |
| 35-60 s | `suspect`, 4,257,967,296 | phantom, and `suspect` means lower bound |
| >=120 s | `gap`, NULL | honest, multi-wrap tier |

So B2d is aimed at the band the rate argument cannot reach, and uses evidence
the rate argument does not have.

**Why both directions, not one.** A single large residual is B3a's job and has
innocent explanations. Requiring both is what makes the signature specific:
down and up carry very different volumes on a typical home line (observed
roughly an order of magnitude apart during development, e.g. multi-GB down
vs a few hundred MB up over the same window), so both crossing the boundary
within one 10 s poll would require them to be simultaneously within one
interval's traffic of it.

**The asymmetry that settles it.** False positive: one flagged unknown
interval. False negative: ~4.29 GB per direction booked into the day total and
the billing-cycle total the cap is judged against, labelled certain. Those are
not comparable costs.

**Rejected:** widening B3a's ceiling (it would have to grow past a full
modulus, which destroys the single-wrap proof B3 rests on); and demoting the
whole 34-120 s band to `gap` (throws away every legitimate wrap in the band to
catch a rare reset).

**What would change the answer:** a link fast enough to genuinely move >2.1 GB
in *both* directions inside a sub-70 s interval would start tripping B2d on
real traffic. That is >250 Mbps sustained each way; the connection used
during development ran well under that (a residential line measured well
under 100 Mbps). If the line is ever upgraded past ~500 Mbps symmetric,
revisit -- and prefer making the packet counters mandatory over loosening this.

**Verified:** `tests/test_counters_reset_signature.py`, 26 tests. Mutation-
checked: disabling the guard body while leaving the constant in place fails 10
tests naming the exact phantom value. Genuine single-direction wraps, clean
increases, and small simultaneous double-wraps all still report real bytes.

---

## D-009 — `max_link_mbps` has a floor, and the floor needs an opt-in to cross

**Decision:** `config.validate()` rejects `max_link_mbps < 1000.0`
(`MIN_SAFE_LINK_MBPS`) unless `i_know_my_link_is_slower = true` is also set.
`load()` logs a WARNING whenever the opt-in is in use, and `doctor` echoes
it.

**Why:** `max_link_mbps` only ever appeared as a *denominator* in the
poll-interval bound (D-001), so validation could only ever catch it being
too **high**. Lowering it — writing the ISP plan speed, the single most
natural thing a user would type — always validated, while silently widening
the window in which the classifier calls a wrap-corrected delta *certain*.
Measured: 1000 → 34.4 s window; 200 → 171.8 s; 50 → 687.2 s; 5 → 6871.9 s.

Executed end to end through the real `Config` → `Thresholds` → `classify`
chain: with `max_link_mbps = 50` and a 600 s sleep during a real
6,000,000,000-byte transfer, `classify` returned `kind='ok'`,
`down_bytes=1,705,032,704` — an error of exactly −4,294,967,296 bytes,
labelled CERTAIN. The same inputs at 1000 correctly return `kind='gap'`,
`down_bytes=None`. This is precisely the silent-undercount failure the
project exists to prevent, reachable by editing one number.

**Alternatives rejected:**

- **Document it in `config.example.toml` and trust the reader** — it already
  said "your WAN port's physical ceiling, not your ISP plan speed," and a
  user writing a minimal config from scratch never sees that file.
- **Silently clamp to 1000** — would ignore a genuinely slower physical link
  (a real 100 Mbit WAN port) and lie about what it is doing.

**What would change the answer:** Hardware with a physically slower WAN
port. That is exactly what the opt-in key is for, and why it warns rather
than refuses.

---

## D-010 — The packet-envelope's frame-size assumption is measured, not asserted

**Decision:** `counters.Thresholds.packet_recovery_enabled` is a measured
verdict. The collector accumulates `(bytes, packets)` from provably-exact
`ok` intervals and, once a window holds ≥100,000 packets across ≥20
intervals, checks the ratio against `[64/1.25, 1518×1.25]`. Outside that, the
`recovered` tier is disabled permanently (`meta.packet_recovery_disabled`,
plus a `calibration` events row) and long gaps degrade to `suspect`/`gap`.

**Why:** The `recovered` tier's docstring said "this is proof, not
guesswork." It is proof only *conditional* on an assumption about firmware
nobody verified: that the byte and packet counters count the same events,
with every packet between 64 and 1518 bytes. A randomized sweep with packet
sizes inside that range produced 0 dishonest results; a sweep with
GRO/LRO-coalesced accounting produced 630, all of them `kind='recovered'`.
Executed single case: a 300 s interval carrying a true 29,999,997,000 bytes
at 9000 bytes/packet returns `recovered` with `down_bytes=4,230,193,224` —
six missed wraps, error −25,769,803,776 bytes, no suspect flag.

A self-consistency check on the resolved answer's implied bytes-per-packet
was added too, but it is weak and is documented as such: the envelope
already constrains the answer to roughly that range, so it only bites when
`envelope_slack_bytes` dominates a small packet delta. It does **not** catch
the 9000 B/pkt case above (implied ratio 1269, comfortably inside
[64, 1518]). The accumulator is the real defence.

**Why the window is 100,000 packets and not one interval — measured:**
`igd.read()` fetches the byte counters and the packet counters in *separate*
SOAP calls, so whatever crosses the WAN between them is counted in one and
not the other. A first version of this calibration judged each interval on
its own and disabled packet recovery on the healthy live router within one
minute of running. The real consecutive samples that did it:

| interval | Δbytes | Δpackets | B/pkt |
|---|---|---|---|
| A | 84,998 | 3,103 | **27.4** |
| B | 3,799,999 | 125 | **30,400.0** |
| A+B | 3,884,997 | 3,228 | **1203.5** |

Both single-interval ratios are nonsense; their sum is the truth. The skew
is a bounded offset contributed once at each end of a window, so it becomes
a few percent over 100,000 packets — well inside `CALIBRATION_TOLERANCE`
(1.25), which is in turn far below the 5.9× ratio the check exists to catch.

Measured on one router during development, over six 10 s intervals:
958–1361 B/pkt down, 245–685 B/pkt up — both comfortably inside
`[64, 1518]`, so the `recovered` tier was sound on that hardware and
stayed enabled.

**Alternatives rejected:**

- **Judge each interval independently** — measured false positive on the
  development router within one minute (above).
- **Persist the accumulator across restarts** — would add a `set_meta` write
  outside `record_poll`'s single transaction on every poll, to save a delay
  in rendering a verdict that only matters once. In-memory accumulation just
  restarts the window; the *verdict* is what persists.

**What would change the answer:** Observing a router whose ratio sits
legitimately between 1518×1.25 and the coalescing range would require
tightening the tolerance, and a router with jumbo frames on the WAN side
would require raising `max_pkt_bytes` rather than disabling the tier.

**Unmeasured:** how long the *up*-direction window takes to fill on
typical asymmetric home traffic. Up packets running roughly a fifth of
down (the observed ratio during development) implies tens of minutes;
that is arithmetic from the six-interval sample above, not an observation.

---

## Unmeasured (flagged, not invented)

- **Storage size at real 90-day retention** — the ~60 MB `intervals` table
  estimate in `store.py`'s schema comments is a back-of-envelope
  calculation (rows × row size), not a measurement against a database that
  has actually run for 90 days.
- **Frequency of `soft_reset` false positives/negatives across many real
  reconnect events** — verified once, live, that uptime and byte-counter
  resets are independent signals, at least on the firmware this was
  developed against (well over 100 days of continuous uptime observed
  against many byte-counter wraps in that span), but the soft-reset
  heuristic's slack constants (`uptime_slack_s = 30.0`, `+2% of gap`) have
  not been tuned against a week of real `events` table output — see risk #1
  in the original design brief.
- **Divergence between the router's WAN counters and the ISP's actual
  billing meter** — expected to be "a few percent," per general knowledge
  of router-vs-ISP counter differences (framing overhead, router-originated
  traffic, retransmits), not measured against a real ISP bill.

---

## D-012 — The menu-bar app is a second LaunchAgent, and opt-in

**Decision:** `housewatch` installs two independent LaunchAgents, not one.
`./install.sh` loads `com.housewatch.collector` only. The menu-bar app
(`com.housewatch.menubar`) is loaded solely by `--with-menubar` or
`--menubar-only`. `uninstall.sh` removes both.

**Why two agents rather than one process.** The collector holds the single
writer lock on the database. The menu bar draws an `NSStatusItem` through
rumps/PyObjC, which is by a wide margin the most fragile dependency in the
project — it is the only one that needs a GUI session, a working Objective-C
bridge, and a display server. Putting both in one process means any status-item
failure takes down data collection with it, and under `KeepAlive` it would take
it down repeatedly. Split, the worst case for a rumps crash is a missing menu
bar icon; the numbers keep accruing. The reverse also holds: stopping the menu
bar to free a stuck status item never interrupts polling.

**Why opt-in rather than installed by default.** `rumps` is the project's only
GUI dependency and the only one that cannot work headless. The intended next
host is a Raspberry Pi, where a menu bar is meaningless and rumps may not
install at all. Making it opt-in keeps `./install.sh` working on a machine
with no display, and keeps the rumps import check out of the default path.

**Why uninstall takes both, in that order.** The menu bar offers Start/Stop
Collector controls. Removing the collector first would leave a live menu bar
showing stale numbers behind a control pointing at a service that no longer
exists. Menu bar out first, then the collector.

**Rejected:** a single agent with a `--no-menubar` flag (inverts the default in
favour of the more fragile component); launching the menu bar as a subprocess of
the collector (couples their lifetimes again, and puts UI in the writer's
process tree); shipping the menu bar as a `.app` bundle (a build step and a
signing story for something that is 700 lines of Python).

**What would change the answer:** if rumps were ever replaced by something that
works headless and has no native-bridge failure mode, the isolation argument
weakens and one agent would be simpler.

**Verified:** `./install.sh --with-menubar` renders and loads both agents;
`plutil -lint` passes on both rendered plists; `tests/test_cli.py` pins the two
labels against the plists, `install.sh`, `uninstall.sh` and the `Makefile`,
which is what caught them disagreeing once before.
