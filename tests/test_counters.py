"""Exhaustive tests for housewatch.counters — the acceptance gate for the
whole project. Every branch (B0-B7 as named in counterAlgorithm) gets its own
test, plus the boundary/edge cases the spec calls out by name.

All Reading construction goes through the small helpers below so each test
can vary only the field(s) it cares about. No network, no clock reads, no
sqlite — classify() and its helpers are pure, so every test passes plain ints
and floats straight in.
"""

from __future__ import annotations

import pytest

from housewatch.counters import (
    AMBIGUOUS_WRAP_MULTIPLIER,
    MODULUS_32,
    Thresholds,
    certainty_window_s,
    classify,
    CALIBRATION_TOLERANCE,
    calibration_contribution,
    implausible_bytes_per_packet,
    packet_deltas,
    packet_window_open,
    wrap_delta,
)
from housewatch.model import Reading

T = Thresholds()  # defaults: 1 Gbps planning rate, GbE packet ceiling, etc.


def mk(
    ts: float,
    bytes_recv: int,
    bytes_sent: int,
    uptime_s: int,
    pkts_recv: int | None = None,
    pkts_sent: int | None = None,
) -> Reading:
    return Reading(
        ts=ts,
        bytes_recv=bytes_recv,
        bytes_sent=bytes_sent,
        uptime_s=uptime_s,
        pkts_recv=pkts_recv,
        pkts_sent=pkts_sent,
    )


# ---------------------------------------------------------------------------
# wrap_delta / certainty_window_s — the standalone arithmetic helpers
# ---------------------------------------------------------------------------


def test_wrap_delta_no_wrap() -> None:
    assert wrap_delta(1000, 1500, wraps=0) == 500


def test_wrap_delta_one_wrap() -> None:
    # curr < prev, one wrap: true delta reconstitutes to a sane positive value.
    assert wrap_delta(4_000_000_000, 100_000_000, wraps=1) == 100_000_000 - 4_000_000_000 + MODULUS_32


def test_wrap_delta_custom_modulus() -> None:
    assert wrap_delta(10, 5, wraps=1, modulus=100) == (5 - 10) + 100


def test_certainty_window_s_defaults() -> None:
    # 2**32 / 125_000_000 = 34.359... seconds, matching the spec's derivation.
    w = certainty_window_s(T.max_bytes_per_sec)
    assert w == pytest.approx(34.359738368, rel=1e-9)


def test_certainty_window_s_scales_with_modulus() -> None:
    assert certainty_window_s(125_000_000.0, modulus=2**64) == pytest.approx(
        certainty_window_s(125_000_000.0) * 2**32, rel=1e-9
    )


# ---------------------------------------------------------------------------
# B0 — FIRST_SAMPLE (baseline)
# ---------------------------------------------------------------------------


def test_first_sample_is_baseline() -> None:
    curr = mk(1000.0, 500, 300, 100)
    r = classify(None, curr)
    assert r.kind == "baseline"
    assert r.start_ts == r.end_ts == 1000.0
    assert r.down_bytes is None
    assert r.up_bytes is None
    assert r.down_wraps is None
    assert r.up_wraps is None


# ---------------------------------------------------------------------------
# B1 — CLOCK_ANOMALY (non-positive gap)
# ---------------------------------------------------------------------------


def test_zero_gap_is_anomaly() -> None:
    prev = mk(1000.0, 500, 300, 100)
    curr = mk(1000.0, 600, 400, 105)
    r = classify(prev, curr)
    assert r.kind == "anomaly"
    assert r.down_bytes is None and r.up_bytes is None


def test_negative_gap_is_anomaly() -> None:
    prev = mk(1000.0, 500, 300, 100)
    curr = mk(990.0, 600, 400, 105)  # clock stepped backward
    r = classify(prev, curr)
    assert r.kind == "anomaly"


# ---------------------------------------------------------------------------
# B2 — RESET (reboot / WAN reconnect), uptime as sole discriminator
# ---------------------------------------------------------------------------


def test_hard_reset_uptime_drops() -> None:
    prev = mk(1000.0, 2_000_000, 1_000_000, 100_000)
    curr = mk(1100.0, 500_000, 200_000, 50)  # uptime collapsed: reboot
    r = classify(prev, curr)
    assert r.kind == "reset"
    assert r.down_bytes is None and r.up_bytes is None
    assert r.down_wraps is None and r.up_wraps is None


def test_soft_reset_uptime_grew_less_than_gap() -> None:
    # gap=100s but uptime only grew 50s -> a reconnect happened and
    # un-happened somewhere inside the gap, hidden by uptime still net
    # increasing. curr.uptime(1050) < prev.uptime(1000) + gap(100)
    # - (slack(30) + 2%*100=2) = 1068 -> soft reset triggers.
    prev = mk(1000.0, 1_000_000, 500_000, 1000)
    curr = mk(1100.0, 1_100_000, 550_000, 1050)
    r = classify(prev, curr)
    assert r.kind == "reset"
    assert r.down_bytes is None


def test_hard_reset_alone_triggers_where_soft_reset_would_not() -> None:
    """Isolate the HARD check from the SOFT check: a small gap (1s) with
    uptime decreasing by 10s trips hard_reset (decrease of 10s > the 5s
    jitter allowance) while NOT tripping soft_reset (curr.uptime is well
    above prev.uptime + gap - (slack=30 + 2%*gap) = prev.uptime - 29.02, so
    the soft inequality is false). Without this test, deleting the hard
    check entirely is invisible: every other reset test in this suite also
    satisfies the soft condition and would still catch a reboot on its own.
    """
    prev = mk(1000.0, 1_000_000, 500_000, 500)
    curr = mk(1001.0, 1_000_100, 500_050, 490)  # uptime dropped by 10s
    # Confirm the trap: soft_reset's inequality must be false here, so only
    # the hard check can be responsible for classifying this as a reset.
    gap = curr.ts - prev.ts
    soft_threshold = prev.uptime_s + gap - (T.uptime_slack_s + 0.02 * gap)
    assert curr.uptime_s >= soft_threshold  # soft would NOT fire
    r = classify(prev, curr)
    assert r.kind == "reset"


def test_uptime_equal_within_slack_is_not_a_reset() -> None:
    # E1: uptime tracks gap closely (well within slack) -> continuous, falls
    # through to normal wrap classification, not a reset.
    prev = mk(1000.0, 1_000_000, 500_000, 1000)
    curr = mk(1010.0, 1_050_000, 520_000, 1010)  # uptime matches gap exactly
    r = classify(prev, curr)
    assert r.kind == "ok"
    assert r.down_bytes == 50_000
    assert r.up_bytes == 20_000


def test_reset_wins_even_when_bytes_also_look_like_a_valid_wrap() -> None:
    """The nasty case: uptime drops (proof of reboot) AND the byte counters
    also decreased in a way that -- taken alone -- would look like a
    perfectly ordinary single wrap for a short gap. Uptime must win outright:
    classify() checks B2 before ever computing byte deltas, so this must
    come back 'reset' with NULL bytes, never 'ok_wrap' with an invented
    number.
    """
    gap = 10.0
    prev_uptime = 100_000
    curr_uptime = 200  # unambiguous hard reset: uptime collapsed

    prev_bytes = 4_000_000_000
    curr_bytes = 100_000_000  # curr < prev: looks like it could be a wrap
    # Sanity-check the trap: if uptime were ignored, this WOULD classify as
    # a plausible single wrap within the certainty window for a 10s gap.
    base = (curr_bytes - prev_bytes) % MODULUS_32
    max_bytes = gap * T.max_bytes_per_sec
    assert max_bytes < MODULUS_32  # would be B3 CERTAIN territory
    assert base <= max_bytes + T.envelope_slack_bytes  # and would pass the guard

    prev = mk(1000.0, prev_bytes, 1_000_000, prev_uptime)
    curr = mk(1000.0 + gap, curr_bytes, 1_100_000, curr_uptime)
    r = classify(prev, curr)
    assert r.kind == "reset"
    assert r.down_bytes is None
    assert r.up_bytes is None


def test_uptime_wrap_documented_as_reset() -> None:
    """E2: the uptime counter itself is a ui4 (wraps at 2**32 seconds =
    ~136 years) -- unreachable in practice, but if it ever happened the
    uptime value would drop sharply just like a reboot and this module
    correctly (if pessimistically) re-baselines rather than misreading it.
    No special-case code exists for this; it is the same hard-reset path.
    """
    prev = mk(1000.0, 500_000, 300_000, MODULUS_32 - 100)
    curr = mk(1010.0, 600_000, 400_000, 5)  # uptime "wrapped" back near 0
    r = classify(prev, curr)
    assert r.kind == "reset"


def test_missing_uptime_raises_rather_than_silently_misclassifying() -> None:
    """model.Reading.uptime_s is a required int by contract (igd.read() is
    documented to be all-or-nothing for the mandatory fields; a Reading
    missing uptime must never exist) -- so classify() never has to *handle*
    a missing uptime in the sense of returning a normal IntervalResult for
    one. What it must not do is silently misclassify garbage that violates
    that contract. Comparing None against an int raises TypeError naturally,
    which is the correct failure mode: loud, not a guessed number.
    """
    prev = mk(1000.0, 500_000, 300_000, 100)
    curr = mk(1010.0, 600_000, 400_000, None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        classify(prev, curr)


# ---------------------------------------------------------------------------
# B3 — CERTAIN (gap within the single-wrap-provable window)
# ---------------------------------------------------------------------------


def test_normal_increase_is_ok() -> None:
    prev = mk(1000.0, 1_000_000, 500_000, 1000)
    curr = mk(1010.0, 1_050_000, 520_000, 1010)
    r = classify(prev, curr)
    assert r.kind == "ok"
    assert r.down_bytes == 50_000
    assert r.up_bytes == 20_000
    assert r.down_wraps == 0
    assert r.up_wraps == 0


def test_zero_delta_first_sample_and_equal_counters() -> None:
    # curr == prev exactly, gap positive and small: idle WAN, delta 0, 'ok'
    # (not 'ok_wrap' -- curr < prev is False when equal, so no wrap is
    # asserted even though the mod-M residual formula would also give 0).
    prev = mk(1000.0, 1_000_000, 500_000, 1000)
    curr = mk(1010.0, 1_000_000, 500_000, 1010)
    r = classify(prev, curr)
    assert r.kind == "ok"
    assert r.down_bytes == 0
    assert r.up_bytes == 0
    assert r.down_wraps == 0
    assert r.up_wraps == 0


def test_exact_boundary_wrap() -> None:
    # prev = 2**32 - 1, curr = 0 -> ok_wrap, delta 1 (the smallest possible
    # wrap, right at the modulus boundary).
    prev = mk(1000.0, MODULUS_32 - 1, 0, 1000)
    curr = mk(1005.0, 0, 0, 1005)
    r = classify(prev, curr)
    assert r.kind == "ok_wrap"
    assert r.down_bytes == 1
    assert r.down_wraps == 1
    assert r.up_bytes == 0
    assert r.up_wraps == 0


def test_both_directions_tracked_independently() -> None:
    # down wraps, up does not, in the same interval -- overall kind reflects
    # "any direction wrapped", but each direction's own bytes/wraps are its
    # own value, not smeared across directions.
    prev = mk(1000.0, MODULUS_32 - 500, 1_000_000, 1000)
    curr = mk(1005.0, 200, 1_050_000, 1005)  # down wrapped, up did not
    r = classify(prev, curr)
    assert r.kind == "ok_wrap"
    assert r.down_bytes == 700  # (200 - (2**32-500)) mod 2**32 == 700
    assert r.down_wraps == 1
    assert r.up_bytes == 50_000
    assert r.up_wraps == 0


def test_b3a_plausibility_guard_rejects_implausible_short_gap_wrap() -> None:
    # A near-full-modulus decrease within a 1-second gap cannot be explained
    # by a single wrap at the planning rate -- E4 (counters reset without
    # uptime resetting) lands here as 'anomaly', not 'ok_wrap'.
    prev = mk(1000.0, 4_000_000_000, 500_000, 1000)
    curr = mk(1001.0, 100_000_000, 500_000, 1001)  # uptime continuous
    r = classify(prev, curr)
    assert r.kind == "anomaly"
    assert r.down_bytes is None


# ---------------------------------------------------------------------------
# B4 — PACKET_RECOVERY (a real-shaped wrap, values re-synthesized)
# ---------------------------------------------------------------------------


def test_wrap_recovered_via_packets() -> None:
    """A wrap with the same shape as one captured during development
    (values re-synthesized): bytes 3700000000 -> 800000000 over an 804 s
    gap, with pkts_recv 880000000 -> 881400000. Byte counters alone are
    ambiguous at this gap (max_bytes >> one modulus), but the packet
    envelope proves exactly one wrap. The 1.4e6 packet delta gives an
    envelope of [0, 2.375 GB] (the low end floors at zero because
    envelope_slack_bytes exceeds 1.4e6 * 64); the one-wrap candidate
    (1.395 GB) fits it and the two-wrap candidate (5.69 GB) does not, so
    exactly one candidate survives. Up direction is a small ordinary
    increase, included to prove both directions resolve independently and
    the interval as a whole comes back 'recovered' only because BOTH do.
    """
    prev = mk(
        ts=0.0,
        bytes_recv=3_700_000_000,
        bytes_sent=250_000_000,
        uptime_s=9_000_000,
        pkts_recv=880_000_000,
        pkts_sent=500_000_000,
    )
    curr = mk(
        ts=804.0,
        bytes_recv=800_000_000,
        bytes_sent=290_000_000,
        uptime_s=9_000_804,  # uptime grew by exactly the gap: continuous
        pkts_recv=881_400_000,
        pkts_sent=500_080_000,
    )
    r = classify(prev, curr)
    assert r.kind == "recovered"
    # 800_000_000 + 2**32 - 3_700_000_000
    assert r.down_bytes == 1_394_967_296
    assert r.down_wraps == 1
    assert r.up_bytes == 40_000_000
    assert r.up_wraps == 0


def test_recovered_rejection_when_multiple_candidates_fit_envelope() -> None:
    """When the packet envelope is too wide to distinguish between two
    wrap-candidates (k=0 and k=1 both plausible), packet recovery must NOT
    guess -- it falls through. With gap=40s (within the B5 bounded-ambiguity
    window), the result must be 'suspect', not 'recovered'.
    """
    gap = 40.0
    prev = mk(
        ts=1000.0,
        bytes_recv=1_000_000,
        bytes_sent=1_000_000,
        uptime_s=5000,
        pkts_recv=1_000_000,
        pkts_sent=1_000_000,
    )
    curr = mk(
        ts=1000.0 + gap,
        bytes_recv=1_000_000,  # base_down == 0: candidates {0, 2**32}
        bytes_sent=1_000_500,  # ordinary small up increase, resolves cleanly
        uptime_s=5000 + int(gap),
        pkts_recv=1_000_000 + 2_700_000,  # huge pkt delta -> wide envelope
        pkts_sent=1_000_000 + 10,
    )
    r = classify(prev, curr)
    assert r.kind == "suspect"
    assert r.down_bytes == 0  # base_down, the minimal-consistent candidate
    assert r.up_bytes == 500


def test_all_or_nothing_direction_rule() -> None:
    """Even when ONE direction resolves uniquely via the packet envelope,
    if the OTHER direction stays ambiguous the whole interval must fall
    through -- never mix a proven direction with an invented one. Down here
    is deliberately ambiguous (mirrors the case above); up would resolve
    uniquely in isolation. Overall kind must not be 'recovered'.
    """
    gap = 40.0
    prev = mk(
        ts=2000.0,
        bytes_recv=1_000_000,
        bytes_sent=500_000,
        uptime_s=9000,
        pkts_recv=1_000_000,
        pkts_sent=1_000_000,
    )
    curr = mk(
        ts=2000.0 + gap,
        bytes_recv=1_000_000,  # ambiguous: candidates {0, 2**32} both fit
        bytes_sent=540_000,  # would resolve cleanly alone
        uptime_s=9000 + int(gap),
        pkts_recv=1_000_000 + 2_700_000,  # wide envelope -> ambiguous
        pkts_sent=1_000_000 + 100,  # narrow envelope -> would resolve alone
    )
    r = classify(prev, curr)
    assert r.kind != "recovered"
    assert r.kind == "suspect"  # gap=40s is within the bounded window


def test_packet_envelope_contradiction_is_anomaly() -> None:
    """If NEITHER candidate fits the packet envelope for a direction, the
    byte counters and packet counters disagree outright -- that is a
    contradiction, not an ambiguity, and must be reported as 'anomaly' with
    NULL bytes rather than any numeric guess.
    """
    gap = 100.0
    prev = mk(
        ts=3000.0,
        bytes_recv=1_000_000,
        bytes_sent=500_000,
        uptime_s=1000,
        pkts_recv=1_000_000,
        pkts_sent=1_000_000,
    )
    curr = mk(
        ts=3000.0 + gap,
        # base_down = 3_000_000_000, candidate k=0. Packet delta implies a
        # tiny envelope that 3 GB cannot possibly fit in.
        bytes_recv=(1_000_000 + 3_000_000_000) % MODULUS_32,
        bytes_sent=540_000,
        uptime_s=1000 + int(gap),
        pkts_recv=1_000_000 + 10,  # tiny packet delta -> tiny envelope
        pkts_sent=1_000_000 + 100,
    )
    r = classify(prev, curr)
    assert r.kind == "anomaly"
    assert r.down_bytes is None


# ---------------------------------------------------------------------------
# B5 — SUSPECT (bounded ambiguity: gap < 2*certainty window, no packets)
# ---------------------------------------------------------------------------


def test_suspect_at_gap_40s_without_packets() -> None:
    gap = 40.0
    prev = mk(1000.0, 1_000_000, 500_000, 5000, pkts_recv=None, pkts_sent=None)
    curr = mk(1000.0 + gap, 1_200_000, 540_000, 5000 + int(gap), pkts_recv=None, pkts_sent=None)
    r = classify(prev, curr)
    assert r.kind == "suspect"
    assert r.down_bytes == 200_000
    assert r.up_bytes == 40_000
    assert r.down_wraps == 0
    assert r.up_wraps == 0
    assert r.down_bytes is not None  # flagged, but never withheld as None


def test_b5_has_no_ceiling_guard_because_one_there_would_be_dead_code() -> None:
    """B5 deliberately carries NO 'delta > ceiling -> anomaly' guard.

    Such a guard used to be copied down from B3 and was provably dead: B5 is
    only reached when max_bytes >= modulus, and base_d is a mod-modulus
    residual, so base_d < modulus <= max_bytes < ceiling ALWAYS. Scanning
    the entire B5 gap band in 0.01s steps with the largest possible base
    (modulus - 1) produces zero firings. Its real job -- catching a counter
    reset that uptime missed -- now lives in B2b/B2c, where it can fire.

    This test asserts BOTH halves: the band really is unguardable that way,
    and a near-full-modulus residual with continuous uptime and no packets
    still classifies 'suspect' (the honest bounded answer), not 'anomaly'.
    """
    firings = 0
    g = 34.0
    while g <= 69.0:
        max_bytes = g * T.max_bytes_per_sec
        if MODULUS_32 <= max_bytes < AMBIGUOUS_WRAP_MULTIPLIER * MODULUS_32:
            if (MODULUS_32 - 1) > max_bytes + T.envelope_slack_bytes:
                firings += 1
        g = round(g + 0.01, 2)
    assert firings == 0

    gap = 40.0  # B5 window: modulus <= max_bytes < 2*modulus
    prev = mk(1000.0, MODULUS_32 - 1, 500_000, 5000, pkts_recv=None, pkts_sent=None)
    curr = mk(1000.0 + gap, MODULUS_32 - 2, 540_000, 5000 + int(gap), pkts_recv=None, pkts_sent=None)
    r = classify(prev, curr)
    assert r.kind == "suspect"
    assert r.down_bytes == MODULUS_32 - 1  # near-full-modulus residual, still accepted
    assert r.down_wraps == 1


# ---------------------------------------------------------------------------
# B2b / B2c — counter reset with uptime that looks continuous.
#
# These are regression tests for the worst defect this project shipped: a
# byte-counter reset NOT accompanied by an uptime drop (the spec's E4 case)
# used to be reported as ~4.29 GB per direction of traffic that never
# happened, and 'suspect' -- documented and consumed as a LOWER bound --
# was the kind it landed under, inverting the contract.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("gap", [10.0, 34.0, 40.0, 60.0, 68.0])
def test_counter_clear_with_continuous_uptime_is_reset_not_phantom_traffic(
    gap: float,
) -> None:
    """E4: someone clears the stats in the admin UI. Byte counters jump back
    toward zero; uptime keeps ticking, perfectly continuous. Read as a wrap,
    this invents ~4.29 GB per direction and books it against the cap.

    The packet counters settle it with proof: inside the packet window a
    packet counter cannot advance a full modulus, so its mod-modulus delta
    is EXACT -- and an exact delta above the physical packet-rate ceiling is
    impossible for continuous counters.
    """
    prev = mk(0.0, 40_000_000, 9_000_000, 500_000, pkts_recv=300_000, pkts_sent=120_000)
    curr = mk(gap, 3_000_000, 700_000, 500_000 + int(gap), pkts_recv=20_000, pkts_sent=8_000)
    r = classify(prev, curr)
    assert r.kind == "reset"
    assert r.down_bytes is None
    assert r.up_bytes is None
    assert "discontinuity" in r.note


def test_wan_reconnect_escaping_both_uptime_checks_is_not_credited() -> None:
    """The uptime checks have one derivable escape: a reconnect so soon
    after the PREVIOUS connection came up that the new uptime still looks
    continuous (prev.uptime=5, curr.uptime=34 over a 60s gap clears both
    hard and soft reset). This used to classify 'suspect' with
    down=4,257,967,296."""
    prev = mk(0.0, 40_000_000, 9_000_000, 5, pkts_recv=300_000, pkts_sent=120_000)
    curr = mk(60.0, 3_000_000, 700_000, 34, pkts_recv=20_000, pkts_sent=8_000)
    r = classify(prev, curr)
    assert r.kind == "reset"
    assert r.down_bytes is None


def test_reset_escape_without_packet_counters_is_unknown_not_a_lower_bound() -> None:
    """Same reconnect-escape, but the optional packet SOAP calls failed so
    there is no packet evidence. 'It wrapped' and 'it reset' are then
    equally consistent, and crediting the wrap would book a near-modulus
    number as if it were a floor. B2c returns unknown instead."""
    prev = mk(0.0, 4_000_000_000, 9_000_000, 5, pkts_recv=None, pkts_sent=None)
    curr = mk(60.0, 3_000_000, 700_000, 34, pkts_recv=None, pkts_sent=None)
    r = classify(prev, curr)
    assert r.kind == "anomaly"
    assert r.down_bytes is None
    assert r.up_bytes is None


def test_b2c_does_not_fire_on_a_normal_wrap_with_healthy_uptime() -> None:
    """The B2c guard keys on the PREVIOUS reading's uptime being short
    enough to hide a reconnect. A router that has been up for hours is not
    in that window, so an ordinary wrap is still credited normally."""
    prev = mk(0.0, MODULUS_32 - 1000, 500_000, 90_000, pkts_recv=None, pkts_sent=None)
    curr = mk(10.0, 5_000, 540_000, 90_010, pkts_recv=None, pkts_sent=None)
    r = classify(prev, curr)
    assert r.kind == "ok_wrap"
    assert r.down_bytes == 6_000


def test_packet_delta_above_the_physical_ceiling_is_proof_of_reset() -> None:
    """Closes reviewer mutation M10. Deleting the packet-count sanity
    ceiling used to leave the suite green; it is now the B2b reset proof,
    so removing it turns a reset into invented traffic and this fails."""
    gap = 300.0  # inside the packet window (2863s), outside byte certainty
    prev = mk(0.0, 2_000_000_000, 500_000_000, 100_000, pkts_recv=4_000_000_000, pkts_sent=3_000_000_000)
    # A packet delta of ~2.3e9 over 300s implies 7.8 Mpps -- 5x a GbE port's
    # 1.488 Mpps ceiling. Impossible for continuous counters.
    curr = mk(gap, 1_000_000, 400_000, 100_000 + int(gap), pkts_recv=2_000_000, pkts_sent=1_500_000)
    r = classify(prev, curr)
    assert r.kind == "reset"
    assert r.down_bytes is None


def test_healthy_wrap_keeps_increasing_packets_so_b2b_cannot_false_positive() -> None:
    """B2b must never fire on a genuine byte wrap: in a real wrap the packet
    counter keeps increasing normally, so its exact delta stays under the
    physical ceiling. This is the re-synthesized wrap above, re-asserted
    from the B2b angle."""
    prev = mk(0.0, 3_700_000_000, 250_000_000, 9_000_000,
              pkts_recv=880_000_000, pkts_sent=500_000_000)
    curr = mk(804.0, 800_000_000, 290_000_000, 9_000_804,
              pkts_recv=881_400_000, pkts_sent=500_080_000)
    r = classify(prev, curr)
    assert r.kind == "recovered"
    assert r.down_bytes == 1_394_967_296


# ---------------------------------------------------------------------------
# B6 — GAP (unbounded ambiguity / long outage / multiple wraps)
# ---------------------------------------------------------------------------


def test_two_or_more_wraps_ambiguous_without_packets() -> None:
    # gap=100s: max_bytes = 100 * 125e6 = 12.5e9, which is >= 2*modulus
    # (8.59e9) -- two or more wraps are physically possible, and with no
    # packet counters to disambiguate this must come back 'gap', never a
    # guessed delta.
    gap = 100.0
    prev = mk(1000.0, 1_000_000, 500_000, 5000, pkts_recv=None, pkts_sent=None)
    curr = mk(1000.0 + gap, 1_200_000, 540_000, 5000 + int(gap), pkts_recv=None, pkts_sent=None)
    r = classify(prev, curr)
    assert r.kind == "gap"
    assert r.down_bytes is None
    assert r.up_bytes is None
    assert r.down_wraps is None
    assert r.up_wraps is None


def test_long_gap_two_hours_without_packets_is_unknown() -> None:
    gap = 7200.0
    prev = mk(1000.0, 1_000_000, 500_000, 5000, pkts_recv=880_000_000, pkts_sent=500_000_000)
    curr = mk(
        1000.0 + gap,
        1_200_000,
        540_000,
        5000 + int(gap),
        pkts_recv=880_000_000 + 50_000,  # present, but the window itself excludes B4
        pkts_sent=500_000_000 + 10_000,
    )
    r = classify(prev, curr)
    assert r.kind == "gap"
    assert r.down_bytes is None
    assert r.up_bytes is None


def test_long_gap_start_end_ts_span_the_full_gap() -> None:
    prev = mk(1000.0, 1_000_000, 500_000, 5000)
    curr = mk(1000.0 + 7200.0, 1_200_000, 540_000, 5000 + 7200)
    r = classify(prev, curr)
    assert r.start_ts == 1000.0
    assert r.end_ts == 1000.0 + 7200.0


# ---------------------------------------------------------------------------
# modulus parameterization (future 64-bit source passthrough)
# ---------------------------------------------------------------------------


def test_modulus_64_passthrough_treats_huge_deltas_as_certain() -> None:
    # With a 2**64 modulus, even a large-looking byte jump over a long gap
    # stays well under one modulus-width, so it must classify as ordinary
    # 'ok' (ample certainty), proving classify() never hardcodes 2**32
    # internally.
    modulus = 2**64
    prev = mk(1000.0, 1_000_000, 500_000, 5000)
    curr = mk(1000.0 + 7200.0, 1_000_000 + 5_000_000_000, 500_000 + 1_000_000, 5000 + 7200)
    r = classify(prev, curr, modulus=modulus)
    assert r.kind == "ok"
    assert r.down_bytes == 5_000_000_000
    assert r.down_wraps == 0


def test_modulus_64_wrap_still_detected() -> None:
    modulus = 2**64
    prev = mk(1000.0, modulus - 1, 0, 1000)
    curr = mk(1005.0, 0, 0, 1005)
    r = classify(prev, curr, modulus=modulus)
    assert r.kind == "ok_wrap"
    assert r.down_bytes == 1
    assert r.down_wraps == 1


# ---------------------------------------------------------------------------
# module constants sanity
# ---------------------------------------------------------------------------


def test_modulus_32_value() -> None:
    assert MODULUS_32 == 4_294_967_296


def test_ambiguous_wrap_multiplier_is_two() -> None:
    assert AMBIGUOUS_WRAP_MULTIPLIER == 2


# ---------------------------------------------------------------------------
# Packet-recovery's frame-size assumption — measured, not asserted.
#
# 'recovered' is presented as PROOF, but the proof is conditional on the
# router's byte and packet counters sharing accounting units with every
# packet between 64 and 1518 bytes. Nothing used to test that assumption at
# runtime, and violating it produced multi-gigabyte errors labelled proven.
# ---------------------------------------------------------------------------


def test_gro_coalesced_packets_defeat_the_envelope_when_recovery_is_on() -> None:
    """The vulnerability itself, stated as a test so the fix has something
    to be measured against: a router reporting 9000-byte coalesced
    super-packets makes the envelope resolve to exactly one candidate --
    the wrong one, 25.8 GB short, with no suspect flag."""
    gap = 300.0
    true_down = 29_999_997_000
    bpp = 9000
    prev = mk(0.0, 0, 0, 100_000, pkts_recv=0, pkts_sent=0)
    curr = mk(
        gap,
        true_down % MODULUS_32,
        0,
        100_000 + int(gap),
        pkts_recv=(true_down // bpp) % MODULUS_32,
        pkts_sent=0,
    )
    r = classify(prev, curr)
    assert r.kind == "recovered"
    assert r.down_bytes is not None
    assert r.down_bytes - true_down == -6 * MODULUS_32  # six wraps silently dropped


def test_disabling_packet_recovery_degrades_that_case_to_gap() -> None:
    """With the measured verdict applied, the same interval comes back
    'gap' -- unknown, which is honest -- instead of a proven-looking lie."""
    gap = 300.0
    true_down = 29_999_997_000
    bpp = 9000
    prev = mk(0.0, 0, 0, 100_000, pkts_recv=0, pkts_sent=0)
    curr = mk(
        gap,
        true_down % MODULUS_32,
        0,
        100_000 + int(gap),
        pkts_recv=(true_down // bpp) % MODULUS_32,
        pkts_sent=0,
    )
    r = classify(prev, curr, Thresholds(packet_recovery_enabled=False))
    assert r.kind == "gap"
    assert r.down_bytes is None


def test_calibration_contribution_only_from_provably_exact_intervals() -> None:
    """The pair is only a real observation on 'ok' intervals, where BOTH the
    byte delta and the packet delta are proven exact. Anywhere else it would
    be a ratio of two guesses."""
    prev = mk(0.0, 0, 0, 100_000, pkts_recv=0, pkts_sent=0)
    curr = mk(10.0, 100_000_000, 9_000_000, 100_010, pkts_recv=11_111, pkts_sent=6_000)
    ok = classify(prev, curr)
    assert ok.kind == "ok"
    assert calibration_contribution(prev, curr, ok) == (
        100_000_000, 11_111, 9_000_000, 6_000,
    )

    # A 'recovered' interval carries bytes too, but they came out of the
    # envelope this calibration exists to police -- feeding them back in
    # would let a wrong answer certify itself.
    rec_prev = mk(0.0, 3_700_000_000, 250_000_000, 9_000_000,
                  pkts_recv=880_000_000, pkts_sent=500_000_000)
    rec_curr = mk(804.0, 800_000_000, 290_000_000, 9_000_804,
                  pkts_recv=881_400_000, pkts_sent=500_080_000)
    rec = classify(rec_prev, rec_curr)
    assert rec.kind == "recovered"
    assert calibration_contribution(rec_prev, rec_curr, rec) is None


def test_calibration_contribution_needs_packet_counters() -> None:
    prev = mk(0.0, 0, 0, 100_000, pkts_recv=None, pkts_sent=None)
    curr = mk(10.0, 1_000_000, 100_000, 100_010, pkts_recv=None, pkts_sent=None)
    r = classify(prev, curr)
    assert r.kind == "ok"
    assert calibration_contribution(prev, curr, r) is None


def test_ordinary_ethernet_ratios_are_plausible() -> None:
    for ratio in (64, 200, 800, 1200, 1518):
        assert implausible_bytes_per_packet(ratio * 200_000, 200_000) is None


def test_gro_sized_ratio_is_reported_as_implausible() -> None:
    verdict = implausible_bytes_per_packet(9000 * 200_000, 200_000)
    assert verdict is not None
    assert round(verdict) == 9000


def test_tolerance_absorbs_residual_read_skew_but_not_coalescing() -> None:
    """The band is min/1.25 .. max*1.25. A few percent of accumulated read
    skew must not trip it; a 5.9x coalescing factor must."""
    T_ = Thresholds()
    just_inside = T_.max_pkt_bytes * CALIBRATION_TOLERANCE * 0.99
    just_outside = T_.max_pkt_bytes * CALIBRATION_TOLERANCE * 1.01
    assert implausible_bytes_per_packet(round(just_inside * 200_000), 200_000) is None
    assert implausible_bytes_per_packet(round(just_outside * 200_000), 200_000) is not None
    # 1518 * 1.25 = 1897.5, comfortably below the 9000 it must catch.
    assert T_.max_pkt_bytes * CALIBRATION_TOLERANCE < 9000


def test_zero_packets_yields_no_verdict_rather_than_a_division_error() -> None:
    assert implausible_bytes_per_packet(0, 0) is None
    assert implausible_bytes_per_packet(5_000_000, 0) is None


def test_read_skew_cancels_when_accumulated() -> None:
    """Consecutive samples with the same shape as ones captured during
    development, at a 3 s poll interval.
    igd.read() fetches the byte counters and the packet counters in separate
    SOAP calls, so a burst landing between them is counted in one and not
    the other: interval A reads 27.4 bytes/packet, interval B reads
    30,400.0. BOTH are nonsense. Their sum is 1203.5, which is the truth --
    which is exactly why the verdict is rendered on an accumulated window
    and never on a single interval."""
    a_bytes, a_pkts = 84_998, 3_103
    b_bytes, b_pkts = 3_799_999, 125

    assert implausible_bytes_per_packet(a_bytes, a_pkts) is not None  # 27.4
    assert implausible_bytes_per_packet(b_bytes, b_pkts) is not None  # 30400.0

    combined = implausible_bytes_per_packet(a_bytes + b_bytes, a_pkts + b_pkts)
    assert combined is None
    assert round((a_bytes + b_bytes) / (a_pkts + b_pkts), 1) == 1203.5


def test_packet_helpers_report_absence_rather_than_zero() -> None:
    prev = mk(0.0, 0, 0, 100_000, pkts_recv=None, pkts_sent=None)
    curr = mk(10.0, 1000, 100, 100_010, pkts_recv=5, pkts_sent=5)
    assert packet_deltas(prev, curr) is None
    assert packet_window_open(10.0) is True
    assert packet_window_open(3000.0) is False
