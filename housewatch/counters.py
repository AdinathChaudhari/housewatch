"""Pure wrap/reboot/gap state machine for WAN byte counters.

This is the whole ballgame: every byte total the user ever sees flows through
``classify()``. It is a PURE function — no network, no sqlite, no clock reads.
Every input (timestamps, byte counts, uptime, packet counts) arrives as a
plain value on the call, which is what makes every branch reachable from a
unit test with plain ints, and is the only reason this module can be trusted.

The problem in one paragraph: the router's UPnP byte counters are 32-bit
(``ui4``) and WRAP at 2**32 bytes (~4.295 GB). A naive "read counter, subtract
previous" collector silently produces garbage the instant a counter wraps
between two polls — worse, if the true delta lands in [2**32, 2*2**32) the
observed value comes back *larger* than the previous reading and looks like a
perfectly normal increase: a silent ~4.3 GB undercount with no detectable
signature. This module's entire job is to never let that happen: every branch
below either proves the correct delta, bounds it and flags it as such
(``suspect``), or admits the delta is unrecoverable (``gap``) — it never
guesses.

The other failure mode this module guards against is confusing a counter WRAP
with a router REBOOT / WAN reconnect (which also resets the byte counters,
but toward a small number, not by wrapping through it). The discriminator is
the WAN connection uptime (``NewUptime`` from ``WANIPConnection:1
GetStatusInfo``): a reboot is the only thing that can make uptime go
backwards (or grow implausibly little relative to elapsed wall-clock time —
the "soft reset" case, where a reconnect happened and un-happened inside a
long gap). Uptime is checked BEFORE any byte-counter wrap arithmetic, and
wins outright: a byte pattern that *also* happens to look like a valid wrap
is still classified as a reset if uptime says so (see the "nasty case" test).

Uptime is checked first, but it is NOT the only witness, and treating it as
the only one was a real defect: a stats clear in the router's admin UI, or a
WAN reconnect timed to slip past both uptime checks, leaves uptime perfectly
continuous while the byte counters jump back toward zero. Read as a wrap
that invents ~4.29 GB per direction and books it into the day and cycle
totals a data cap is judged against -- under ``suspect``, a kind every
consumer treats as a LOWER bound, so it inverts the contract as well as the
number. Two more checks (B2b, B2c) run before any wrap is credited; see
their comments, and D-008 in ``docs/DECISIONS.md``.

One assumption in this module is measured rather than asserted: the
``recovered`` tier's packet envelope only works if the router's byte and
packet counters count the same events at Ethernet frame sizes. The collector
tests that continuously against provably-exact intervals and flips
``Thresholds.packet_recovery_enabled`` off if the router disagrees -- see
``calibration_contribution`` / ``implausible_bytes_per_packet``, and D-010.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from .model import IntervalResult, Reading

# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

# ui4 (unsigned 32-bit) counters wrap at 2**32 = 4,294,967,296 bytes. This is
# the modulus for every wrap-arithmetic operation in this module. It is a
# named constant (never an inline "2**32" or "4294967296") so the single
# source of truth for the wrap boundary is here, and so a future 64-bit
# source can pass a different modulus through every function below without
# editing a single line of arithmetic (see the modulus= parameter throughout,
# and test_modulus_64_passthrough).
MODULUS_32: Final[int] = 2**32

# At exactly one modulus-width of possible bytes (B3/"CERTAIN" tier), the
# single-wrap correction (new < prev => add one modulus) is PROVABLY correct:
# the true delta between two successful polls cannot exceed one modulus, so
# there is exactly one wrap count consistent with what was observed. Once the
# physically-possible byte range for the elapsed gap reaches or exceeds this
# many modulus-widths, more than one wrap becomes physically possible and the
# delta is ambiguous without external help (packet counters, see B4) — this
# is the "B5 suspect (bounded to one extra wrap) vs B6 gap (unbounded)"
# boundary in classify(). See counterAlgorithm's B3/B5/B6 tiers.
AMBIGUOUS_WRAP_MULTIPLIER: Final[int] = 2

# B2d reset-signature threshold, as a fraction of the modulus.
#
# A counter reset makes both directions read back near zero at once, so each
# direction's mod-modulus residual comes out near a FULL modulus (prev=37 MB
# reads back as 4,257,967,296 = 99% of 2**32). A genuine wrap looks nothing
# like that: its residual is the interval's real traffic, a few percent of the
# modulus at most. Requiring BOTH directions to decrease AND both residuals to
# exceed half a modulus separates the two without a physical-rate argument,
# which is what makes it work at gaps where the rate ceiling is too loose to
# help.
#
# Set at 0.5 rather than something tighter because the two cases sit at
# opposite ends of the range (~99% vs ~3%); anywhere in the middle is
# equivalent, and the midpoint is the least arbitrary place to stand.
RESET_SIGNATURE_FRACTION: Final[float] = 0.5

# How far outside [min_pkt_bytes, max_pkt_bytes] an ACCUMULATED
# bytes-per-packet ratio has to land before it counts as proof that the
# router's two counters use different accounting units. Absorbs the residual
# inter-SOAP-call read skew that survives accumulation (measured at a few
# percent over a 100k-packet window) while staying far below the realistic
# failure mode it exists to catch (GRO/LRO coalescing, ~5.9x above the
# maximum frame size). See implausible_bytes_per_packet.
CALIBRATION_TOLERANCE: Final[float] = 1.25


@dataclass(frozen=True)
class Thresholds:
    """Correctness parameters, not user preferences.

    These are physical ceilings and slack margins the classifier reasons
    about; they are derived from the WAN link's planning rate
    (``max_link_mbps`` in config, default 1 Gbps — a typical consumer
    router's GbE WAN port ceiling) and from Ethernet frame-size physics. The collector builds this
    from ``cfg.max_link_mbps``; nothing else in this module reads config.
    """

    # 1 Gbps / 8 = 125,000,000 bytes/sec. The physical ceiling used to bound
    # how many bytes COULD have crossed the WAN in a given gap — this is what
    # makes the B3/B5/B6 tier boundaries provable rather than heuristic.
    max_bytes_per_sec: float = 125_000_000.0

    # GbE minimum-frame-size ceiling: 1,000,000,000 bits/sec / (64 octets *
    # 8 bits/octet + 20 octets of preamble+IFG*8) rounds up to ~1.488 Mpps;
    # 1,500,000 gives headroom. Packet counters move at least this much
    # slower than byte counters, which is what makes the "packet recovery"
    # tier (B4) usable across a much wider window (47.7 min) than the raw
    # byte-counter certainty window (34.36 s at defaults).
    max_pkts_per_sec: float = 1_500_000.0

    # Reboot discriminator (hard case): uptime must drop by more than this
    # many seconds below the previous reading before it counts as proof of a
    # reboot, absorbing ordinary clock/reporting jitter around a poll that
    # lands right at a reconnect.
    uptime_jitter_s: float = 5.0

    # Reboot discriminator (soft case): uptime is allowed to grow up to
    # (gap - uptime_slack_s - 2% of gap) less than wall-clock elapsed time
    # before it is treated as proof that a reconnect happened and
    # un-happened somewhere inside the gap. Absorbs router-side uptime
    # reporting granularity plus general elapsed-time skew.
    uptime_slack_s: float = 30.0

    # Absorbs the read skew between the two byte-counter SOAP calls
    # (GetTotalBytesReceived, GetTotalBytesSent are two round trips, not
    # atomic) plus general SOAP round-trip jitter — roughly 2 s worth of
    # traffic at the planning rate. Used both as the B3a physical-plausibility
    # guard's slack and as the B4 packet-envelope's slack.
    envelope_slack_bytes: int = 250_000_000

    # Ethernet frame size bounds (bytes), used to convert a packet-count
    # delta into a byte-count range for the B4 packet-envelope check.
    min_pkt_bytes: int = 64
    max_pkt_bytes: int = 1518

    # Master switch for the B4 packet-recovery tier. The envelope in B4 is
    # only sound if the router's byte and packet counters share accounting
    # units and every counted packet really is between min_pkt_bytes and
    # max_pkt_bytes. That is an ASSUMPTION about firmware we did not write.
    # The collector tests it continuously against provably-exact 'ok'
    # intervals (see calibration_contribution) and, the moment an accumulated
    # bytes-per-packet lands outside those bounds -- e.g. a SoC reporting
    # GRO/LRO-coalesced super-packets, which would silently turn a six-wrap
    # interval into a confident 'recovered' number -- flips this to False.
    # With it False, B4 is skipped entirely and those intervals degrade to
    # the honest 'suspect'/'gap' tiers instead of a proven-looking lie.
    packet_recovery_enabled: bool = True


def wrap_delta(prev_value: int, curr_value: int, wraps: int, modulus: int = MODULUS_32) -> int:
    """Reconstitute the true delta given a known number of modulus wraps.

    ``(curr - prev) + wraps * modulus``. This is the inverse of "the counter
    wrapped N times between two reads" — exposed standalone so tests and
    store-side forensic replay can compute a delta from a resolved wrap count
    without duplicating the arithmetic that lives inside ``classify``.
    """
    return (curr_value - prev_value) + wraps * modulus


def certainty_window_s(max_bytes_per_sec: float, modulus: int = MODULUS_32) -> float:
    """Seconds of gap at ``max_bytes_per_sec`` before a single-wrap correction
    stops being provably correct (i.e. before the true delta could reach a
    full modulus). ``modulus / max_bytes_per_sec`` — 34.36 s at the default
    1 Gbps planning rate and 2**32 modulus. Used by ``config.validate`` to
    reject a poll interval that would leave the wrap correction unprovable,
    and by tests to assert the tier boundaries without hardcoding them.
    """
    return modulus / max_bytes_per_sec


def _direction_delta(prev_value: int, curr_value: int, modulus: int) -> tuple[int, bool]:
    """Minimal-consistent (mod-``modulus``) delta and whether it wrapped.

    ``base = (curr - prev) mod modulus`` is a proven LOWER bound on the true
    delta: it is the smallest non-negative delta consistent with the two
    raw ui4 readings, i.e. exactly what "one wrap, if curr < prev, else zero
    wraps" produces. ``wrapped`` is True whenever ``curr < prev`` — that is
    certain proof of AT LEAST one wrap (the counter cannot decrease on its
    own without a wrap or a reset, and a reset is ruled out by this point in
    ``classify``, which always checks uptime first).
    """
    base = (curr_value - prev_value) % modulus
    wrapped = curr_value < prev_value
    return base, wrapped


def packet_deltas(
    prev: Reading, curr: Reading, modulus: int = MODULUS_32
) -> tuple[int, int] | None:
    """Exact mod-``modulus`` packet deltas, or ``None`` if unavailable.

    Only meaningful inside the packet window (``packet_window_open``), where
    a packet counter provably cannot have advanced a full modulus and so the
    mod-modulus residual IS the true delta. Exposed (not private) because
    both ``classify``'s discontinuity check and the collector's frame-size
    calibration need the same arithmetic and must not drift apart.
    """
    if (
        prev.pkts_recv is None
        or prev.pkts_sent is None
        or curr.pkts_recv is None
        or curr.pkts_sent is None
    ):
        return None
    return (
        (curr.pkts_recv - prev.pkts_recv) % modulus,
        (curr.pkts_sent - prev.pkts_sent) % modulus,
    )


def packet_window_open(
    gap: float, thresholds: Thresholds = Thresholds(), modulus: int = MODULUS_32
) -> bool:
    """True while a packet counter cannot have advanced a full modulus in
    ``gap`` seconds, which is what makes its mod-modulus delta exact
    (2863 s / 47.7 min at defaults). Both the B4 recovery tier and the B2b
    discontinuity proof depend on this window being open."""
    return gap * thresholds.max_pkts_per_sec < modulus


def calibration_contribution(
    prev: Reading,
    curr: Reading,
    interval: IntervalResult,
    modulus: int = MODULUS_32,
) -> tuple[int, int, int, int] | None:
    """One interval's contribution to the frame-size measurement, or None.

    Returns ``(down_bytes, down_pkts, up_bytes, up_pkts)`` only for
    ``kind == 'ok'`` -- the single tier where BOTH the byte delta and the
    packet delta are proven exact with zero wraps, so the pair is a real
    observation rather than a ratio of two guesses.

    Note what this deliberately does NOT do: judge anything. A SINGLE
    interval's bytes-per-packet is not a measurement of frame size, because
    ``igd.read()`` fetches the byte counters and the packet counters in
    separate SOAP calls -- whatever crosses the WAN between those two calls
    is counted in one and not the other. Measured on one router during
    development at a 3 s poll interval on bursty traffic, consecutive
    intervals read 27.4 and
    30,400.0 bytes/packet; taken together they read 1203.5, which is the
    truth. The skew is a bounded, self-cancelling offset, so only an
    ACCUMULATED ratio over many intervals means anything. See
    ``implausible_bytes_per_packet`` and the collector's accumulator.
    """
    if interval.kind != "ok" or interval.down_bytes is None or interval.up_bytes is None:
        return None
    pkts = packet_deltas(prev, curr, modulus)
    if pkts is None:
        return None
    return (interval.down_bytes, pkts[0], interval.up_bytes, pkts[1])


def implausible_bytes_per_packet(
    total_bytes: int,
    total_pkts: int,
    thresholds: Thresholds = Thresholds(),
    tolerance: float = CALIBRATION_TOLERANCE,
) -> float | None:
    """The observed bytes-per-packet, if it is outside what B4 assumes.

    Returns ``None`` when the accumulated ratio is consistent with the
    ``[min_pkt_bytes, max_pkt_bytes]`` window the packet envelope is built
    on (or when ``total_pkts`` is zero). Returns the offending ratio when it
    is not -- proof that the router's byte and packet counters do not count
    the same events, which makes every 'recovered' answer unsound on that
    hardware.

    ``tolerance`` widens the window before the verdict trips, absorbing the
    residual inter-SOAP-call read skew that survives accumulation. It is far
    smaller than the gap to the realistic failure mode (GRO/LRO coalescing
    reports ~9000 bytes/packet, 5.9x above max_pkt_bytes), so it buys noise
    immunity without buying blindness.
    """
    if total_pkts <= 0:
        return None
    ratio = total_bytes / total_pkts
    if thresholds.min_pkt_bytes / tolerance <= ratio <= thresholds.max_pkt_bytes * tolerance:
        return None
    return ratio


def _try_packet_recovery(
    gap: float,
    prev: Reading,
    curr: Reading,
    base_down: int,
    base_up: int,
    wrapped_down: bool,
    wrapped_up: bool,
    ceiling: float,
    thresholds: Thresholds,
    modulus: int,
) -> tuple[str, int | None, int | None, int | None, int | None, str] | None:
    """B4 PACKET_RECOVERY.

    Packet counters move at least ``max_pkt_bytes / min_pkt_bytes`` times
    slower than byte counters for a given traffic rate at the frame-size
    extremes, and in practice far slower for typical (non-minimum-frame)
    traffic — within the packet-recovery window they can have wrapped at
    most once, so their mod-``modulus`` delta is exact. That turns a packet
    delta into a narrow byte-count envelope; because wrap candidates for the
    byte counter sit exactly one modulus apart (~4.295 GB), at most one
    candidate can fit inside a sufficiently narrow envelope, which makes the
    resolved delta PROVEN, not guessed.

    Returns ``None`` when packet recovery does not apply or falls through
    (ambiguous or unusable packets) — the caller then falls back to the
    gap-duration-only B5/B6 tiers. Returns a completed
    ``(kind, down_bytes, up_bytes, down_wraps, up_wraps, note)`` tuple when
    it resolves to 'recovered' or contradicts to 'anomaly'.
    """
    if not thresholds.packet_recovery_enabled:
        # Calibration (collector-side) caught this router's packet counter
        # disagreeing with its byte counter about what a "packet" is. The
        # envelope below would still produce a single confident answer; it
        # would just be the wrong one. Degrade to the honest tiers instead.
        return None
    if not packet_window_open(gap, thresholds, modulus):
        return None  # outside the window where a packet-counter delta is provably single-wrap
    pkts = packet_deltas(prev, curr, modulus)
    if pkts is None:
        return None  # packets not available this poll; nothing to recover with
    pkt_delta_down, pkt_delta_up = pkts

    # NOTE: "packet delta exceeds the physical packet ceiling" is NOT
    # handled here any more. Inside the packet window that delta is EXACT,
    # so a value above the physical ceiling is not "packets look unreliable,
    # fall through" - it is positive proof the counters were discontinuous.
    # classify() checks it before this function is ever called (B2b) and
    # returns 'reset'. Falling through to B5 here is exactly what used to
    # turn a counter reset into ~4.29 GB of invented 'suspect' traffic.

    def envelope(pkt_delta: int) -> tuple[float, float]:
        lo = max(0.0, pkt_delta * thresholds.min_pkt_bytes - thresholds.envelope_slack_bytes)
        hi = pkt_delta * thresholds.max_pkt_bytes + thresholds.envelope_slack_bytes
        return lo, hi

    def candidates(base: int) -> list[int]:
        out: list[int] = []
        k = 0
        while base + k * modulus <= ceiling:
            out.append(base + k * modulus)
            k += 1
        return out

    lo_down, hi_down = envelope(pkt_delta_down)
    lo_up, hi_up = envelope(pkt_delta_up)
    cand_down = candidates(base_down)
    cand_up = candidates(base_up)
    feasible_down = [v for v in cand_down if lo_down <= v <= hi_down]
    feasible_up = [v for v in cand_up if lo_up <= v <= hi_up]

    if len(feasible_down) == 1 and len(feasible_up) == 1:
        down_bytes = feasible_down[0]
        up_bytes = feasible_up[0]
        # Self-consistency: the resolved answer must itself imply a frame
        # size inside the bounds the envelope assumed. This is a WEAK check
        # by construction -- envelope_slack_bytes already widens the implied
        # range, so it only bites when the slack dominates a small packet
        # delta -- and it is deliberately NOT the defence against a router
        # whose two counters use different units. That defence is
        # packet_recovery_enabled, driven by the collector's accumulated
        # calibration_contribution / implausible_bytes_per_packet window.
        for value, pkt_delta in ((down_bytes, pkt_delta_down), (up_bytes, pkt_delta_up)):
            if pkt_delta <= 0:
                continue
            implied = value / pkt_delta
            if implied < thresholds.min_pkt_bytes or implied > thresholds.max_pkt_bytes:
                return (
                    "anomaly",
                    None,
                    None,
                    None,
                    None,
                    f"packet recovery implied {implied:.0f} bytes/packet, outside "
                    f"[{thresholds.min_pkt_bytes}, {thresholds.max_pkt_bytes}]",
                )
        # base_d already embeds one wrap whenever the raw counter decreased
        # (curr < prev); the candidate ladder's own k index (how many EXTRA
        # modulus-widths were added on top of base_d) must be added to that,
        # not returned in its place -- see _direction_delta's docstring.
        down_wraps = (down_bytes - base_down) // modulus + (1 if wrapped_down else 0)
        up_wraps = (up_bytes - base_up) // modulus + (1 if wrapped_up else 0)
        return (
            "recovered",
            down_bytes,
            up_bytes,
            down_wraps,
            up_wraps,
            "resolved by packet envelope",
        )

    if len(feasible_down) == 0 or len(feasible_up) == 0:
        return (
            "anomaly",
            None,
            None,
            None,
            None,
            "packet envelope contradicts byte counters",
        )

    return None  # ambiguous in at least one direction: all-or-nothing, fall through


def classify(
    prev: Reading | None,
    curr: Reading,
    thresholds: Thresholds = Thresholds(),
    modulus: int = MODULUS_32,
) -> IntervalResult:
    """Classify the interval between two readings. Total function: never
    raises for any well-typed (int) inputs — every branch below returns an
    ``IntervalResult`` rather than raising, including the "we genuinely don't
    know" branches (``suspect``, ``gap``, ``anomaly``, ``reset``), which
    carry ``None`` bytes rather than an invented number. ``modulus`` is
    parameterized (not hardcoded) so a future 64-bit counter source reuses
    every branch below unchanged (see test_modulus_64_passthrough).

    The caller (the collector) advances its baseline to ``curr`` after EVERY
    call regardless of the returned kind — classification decides only what
    the interval row says, never whether to advance. That policy lives in
    collector.py, not here; this function is not responsible for it.
    """
    # B0: FIRST_SAMPLE. There is no prior reading to diff against, so there
    # is no interval to classify — this is a baseline stamp, not a delta.
    if prev is None:
        return IntervalResult(
            kind="baseline",
            start_ts=curr.ts,
            end_ts=curr.ts,
            down_bytes=None,
            up_bytes=None,
            down_wraps=None,
            up_wraps=None,
            note="first sample",
        )

    gap = curr.ts - prev.ts

    # B1: CLOCK_ANOMALY. A non-positive gap means the wall clock stepped
    # backward (NTP correction) or two reads share a timestamp — either way
    # there is no meaningful elapsed time to attribute bytes to.
    if gap <= 0:
        return IntervalResult(
            kind="anomaly",
            start_ts=prev.ts,
            end_ts=curr.ts,
            down_bytes=None,
            up_bytes=None,
            down_wraps=None,
            up_wraps=None,
            note=f"non-positive gap ({gap:.3f}s)",
        )

    # B2: RESET (reboot / WAN reconnect). Uptime is the FIRST discriminator
    # (B2b/B2c below add two more; uptime is no longer the sole one -- see
    # their comments for the two cases it provably misses), and is checked
    # before any byte-counter wrap arithmetic — a byte pattern
    # that also happens to look like a valid wrap still loses to uptime
    # evidence of a reboot (see the "nasty case" test). Two sub-checks:
    #   hard  — uptime dropped outright (beyond jitter): unambiguous reboot.
    #   soft  — uptime grew, but by less than elapsed wall-clock time allows
    #           (beyond slack): a reconnect happened and un-happened
    #           somewhere inside the gap, hidden by it being long enough
    #           that uptime still net-increased.
    hard_reset = curr.uptime_s < prev.uptime_s - thresholds.uptime_jitter_s
    soft_reset = curr.uptime_s < prev.uptime_s + gap - (thresholds.uptime_slack_s + 0.02 * gap)
    if hard_reset or soft_reset:
        reason = "uptime decreased" if hard_reset else "uptime grew less than elapsed time"
        return IntervalResult(
            kind="reset",
            start_ts=prev.ts,
            end_ts=curr.ts,
            down_bytes=None,
            up_bytes=None,
            down_wraps=None,
            up_wraps=None,
            note=f"{reason}: uptime {prev.uptime_s}->{curr.uptime_s} over {gap:.1f}s gap",
        )

    # B2b: COUNTER DISCONTINUITY, proven by the packet counters.
    #
    # Uptime is not the only witness to a counter reset, and relying on it
    # alone was a real defect: a stats-clear in the admin UI (the spec's E4),
    # or a WAN reconnect whose timing slips through BOTH uptime checks,
    # leaves uptime perfectly continuous while the byte counters jump back
    # toward zero. Read as a wrap, that invents ~4.29 GB per direction of
    # traffic that never happened -- and lands it in the day total and the
    # billing-cycle total the cap is judged against.
    #
    # The packet counters settle it, with proof rather than heuristics:
    # inside the packet window a packet counter cannot have advanced a full
    # modulus, so its mod-modulus delta is EXACT. An exact delta above the
    # physical packet-rate ceiling is therefore not "traffic we can't
    # explain" -- it is impossible for continuous counters, so the counters
    # were not continuous.
    #
    # It cannot false-positive on a genuine byte wrap: in a genuine wrap the
    # packet counter keeps increasing normally, so its exact delta stays at
    # or below the physical ceiling. (The +2 s absorbs the read skew between
    # the byte and packet SOAP calls, which are separate round trips.)
    pkts = packet_deltas(prev, curr, modulus)
    # Hoisted: B2d below needs to know whether the packet counters were in a
    # position to settle this interval at all, not merely whether they did.
    packets_settled = pkts is not None and packet_window_open(gap, thresholds, modulus)
    if packets_settled:
        pkt_ceiling = (gap + 2) * thresholds.max_pkts_per_sec
        if pkts[0] > pkt_ceiling or pkts[1] > pkt_ceiling:
            return IntervalResult(
                kind="reset",
                start_ts=prev.ts,
                end_ts=curr.ts,
                down_bytes=None,
                up_bytes=None,
                down_wraps=None,
                up_wraps=None,
                note=(
                    f"packet counter discontinuity: exact packet delta "
                    f"{max(pkts):,} over {gap:.1f}s exceeds the physical ceiling "
                    f"{pkt_ceiling:,.0f} - counters were reset, not wrapped"
                ),
            )

    # Past B2/B2b, uptime is continuous: the connection did not reset, so any
    # counter decrease is a genuine wrap, not a reboot.
    base_down, wrapped_down = _direction_delta(prev.bytes_recv, curr.bytes_recv, modulus)
    base_up, wrapped_up = _direction_delta(prev.bytes_sent, curr.bytes_sent, modulus)

    # B2c: RESET STILL POSSIBLE (packet counters silent).
    #
    # The uptime checks in B2 have one derivable escape hatch: a reconnect
    # that happened so soon after the PREVIOUS connection came up that the
    # new uptime is still large enough to look continuous. The condition
    # works out to "the previous reading's own uptime was smaller than the
    # slack the soft-reset check allows", which is cheap to test directly.
    # When that window is open AND a counter decreased, "it wrapped" and
    # "it reset" are equally consistent with the evidence -- and crediting
    # the wrap would book a near-modulus number as if it were a lower bound.
    # Unknown is the honest answer.
    reset_window_open = prev.uptime_s < (
        thresholds.uptime_slack_s + 0.02 * gap + thresholds.uptime_jitter_s
    )
    if reset_window_open and (wrapped_down or wrapped_up):
        return IntervalResult(
            kind="anomaly",
            start_ts=prev.ts,
            end_ts=curr.ts,
            down_bytes=None,
            up_bytes=None,
            down_wraps=None,
            up_wraps=None,
            note=(
                f"counter decreased while previous uptime ({prev.uptime_s}s) was short "
                "enough to hide a reconnect: wrap and reset are equally consistent"
            ),
        )

    # B2d: RESET SIGNATURE, both directions, packet counters silent.
    #
    # B2b proves a reset from the packet counters, and B2c catches the case
    # where a short previous uptime could be hiding a reconnect. Neither fires
    # when the packet SOAP calls failed AND uptime has been climbing for
    # months -- an ordinary state for a router nobody has rebooted.
    #
    # What is left as evidence is the shape of the decrease itself. Both
    # counters reading back near zero in the same interval is a reset: on a
    # normal home line the two directions carry very different volumes (down
    # is typically several times up), so for both to cross the modulus
    # boundary within the same poll they would have to be simultaneously
    # within one interval's traffic of it -- while a reset zeroes both at
    # once, by construction.
    #
    # This is deliberately NOT a rate argument. B3a's physical ceiling already
    # covers gaps short enough for the rate to be decisive, and it is exactly
    # the gaps where the ceiling goes slack (~34-120s here) that this guard
    # exists to cover.
    #
    # Cost of a false positive: one flagged unknown interval. Cost of the
    # false negative it replaces: ~4.29 GB per direction booked into the day
    # total and the billing-cycle total the cap is judged against, labelled
    # certain. The asymmetry is the whole argument.
    if (
        not packets_settled
        and wrapped_down
        and wrapped_up
        and base_down >= RESET_SIGNATURE_FRACTION * modulus
        and base_up >= RESET_SIGNATURE_FRACTION * modulus
    ):
        return IntervalResult(
            kind="reset",
            start_ts=prev.ts,
            end_ts=curr.ts,
            down_bytes=None,
            up_bytes=None,
            down_wraps=None,
            up_wraps=None,
            note=(
                f"both directions decreased by near a full modulus "
                f"(down {base_down:,}, up {base_up:,}) with no packet "
                f"corroboration: counters were reset, not wrapped"
            ),
        )

    max_bytes = gap * thresholds.max_bytes_per_sec
    ceiling = max_bytes + thresholds.envelope_slack_bytes

    # B3: CERTAIN. Even at the planning rate, the counter cannot have
    # advanced a full modulus within this gap, so the wrap count is EXACTLY
    # (1 if it decreased, else 0) per direction — the mod-modulus residual
    # IS the true delta, provably.
    if max_bytes < modulus:
        if base_down > ceiling or base_up > ceiling:
            # B3a plausibility guard / E4: a decrease this large cannot be
            # explained by a single wrap at the planning rate for this short
            # a gap — something else reset the counters (e.g. admin-UI stats
            # clear) without resetting uptime.
            return IntervalResult(
                kind="anomaly",
                start_ts=prev.ts,
                end_ts=curr.ts,
                down_bytes=None,
                up_bytes=None,
                down_wraps=None,
                up_wraps=None,
                note="delta exceeds physical ceiling",
            )
        kind = "ok_wrap" if (wrapped_down or wrapped_up) else "ok"
        return IntervalResult(
            kind=kind,
            start_ts=prev.ts,
            end_ts=curr.ts,
            down_bytes=base_down,
            up_bytes=base_up,
            down_wraps=1 if wrapped_down else 0,
            up_wraps=1 if wrapped_up else 0,
            note=f"gap {gap:.1f}s within certainty window",
        )

    # B4: PACKET_RECOVERY (only reachable when max_bytes >= modulus, i.e. the
    # gap is too long for the B3 single-wrap proof to hold on its own).
    recovered = _try_packet_recovery(
        gap, prev, curr, base_down, base_up, wrapped_down, wrapped_up, ceiling, thresholds, modulus
    )
    if recovered is not None:
        kind, down_bytes, up_bytes, down_wraps, up_wraps, note = recovered
        return IntervalResult(
            kind=kind,
            start_ts=prev.ts,
            end_ts=curr.ts,
            down_bytes=down_bytes,
            up_bytes=up_bytes,
            down_wraps=down_wraps,
            up_wraps=up_wraps,
            note=note,
        )

    # B5 vs B6: bounded (one extra possible wrap) vs unbounded ambiguity,
    # decided purely by how many modulus-widths of bytes the gap could
    # physically hold — independent of whether B4 was even attempted.
    if max_bytes < AMBIGUOUS_WRAP_MULTIPLIER * modulus:
        # B5: SUSPECT. The true delta is either base_d or base_d + modulus,
        # nothing else — bounded to a single possible extra wrap. Recorded
        # and flagged (never silently folded into a clean total), per the
        # never-guess rule.
        #
        # There is deliberately NO "base_d > ceiling" guard here. It used to
        # be copied down from B3 and was provably dead code: B5 is only
        # reached when max_bytes >= modulus, and base_d is a mod-modulus
        # residual, so base_d < modulus <= max_bytes < ceiling always. The
        # protection it was supposed to provide -- catching a counter reset
        # that uptime missed -- now lives in B2b/B2c/B2d above, where it can
        # actually fire. B2d specifically covers the gap band that reaches
        # here with the packet counters unavailable.
        return IntervalResult(
            kind="suspect",
            start_ts=prev.ts,
            end_ts=curr.ts,
            down_bytes=base_down,
            up_bytes=base_up,
            down_wraps=1 if wrapped_down else 0,
            up_wraps=1 if wrapped_up else 0,
            note=(
                f"gap {gap:.1f}s: possible undercount up to {modulus:,} bytes "
                "per direction (unresolved single extra wrap)"
            ),
        )

    # B6: GAP. Two or more wraps are physically possible within this gap and
    # packet recovery did not resolve it — the delta is genuinely
    # unrecoverable. No bytes are attributed to any day for this interval;
    # it is recorded as explicit unknown time instead of a guess.
    return IntervalResult(
        kind="gap",
        start_ts=prev.ts,
        end_ts=curr.ts,
        down_bytes=None,
        up_bytes=None,
        down_wraps=None,
        up_wraps=None,
        note=f"gap {gap:.1f}s: delta unrecoverable, multiple wraps possible",
    )
