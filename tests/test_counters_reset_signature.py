"""Regression tests for B2d — the reset-signature guard.

Background. The adversarial review found that a byte-counter reset with no
accompanying uptime drop was booked as ~4.29 GB per direction of traffic that
never happened. The first fix leaned on the packet counters (B2b), which closes
the case only while those counters are available; ``igd._optional_packet_count``
returns ``None`` on any failure, so they are not guaranteed.

With packets unavailable the remaining defence was B3a's physical rate ceiling,
and that ceiling goes slack exactly where it is needed. Measured on the shipped
thresholds, before B2d existed:

    gap  10-30s   -> anomaly, None          (ceiling tight enough: honest)
    gap  ~34s     -> ok_wrap, 4,257,967,296 (phantom, labelled CERTAIN)
    gap  35-60s   -> suspect, 4,257,967,296 (phantom, and 'suspect' is
                                             documented as a LOWER bound, so
                                             the value inverts its own contract)
    gap  >=120s   -> gap, None              (multi-wrap tier: honest)

B2d closes the middle band using the shape of the decrease rather than a rate
argument. These tests pin every row of that table.
"""

from __future__ import annotations

import dataclasses

import pytest

from housewatch import counters
from housewatch.counters import MODULUS_32, RESET_SIGNATURE_FRACTION, Thresholds
from housewatch.model import Reading

# prev=37 MB reading back as 0 is the exact shape the reviewer reproduced:
#   (0 - 37_000_000) % 2**32 == 4_257_967_296
PREV_BYTES = 37_000_000
PHANTOM = 4_257_967_296

# Long enough that neither uptime check can fire. A router connected for
# weeks or months is ordinary, which is what makes this case reachable in
# real use.
LONG_UPTIME = 100_000


def reset_pair(gap: float, *, with_packets: bool) -> tuple[Reading, Reading]:
    """Counters zeroed, uptime climbing normally: a reset that uptime misses."""
    prev = Reading(
        ts=1_000.0,
        bytes_recv=PREV_BYTES,
        bytes_sent=PREV_BYTES,
        uptime_s=LONG_UPTIME,
        pkts_recv=50_000 if with_packets else None,
        pkts_sent=40_000 if with_packets else None,
    )
    curr = Reading(
        ts=1_000.0 + gap,
        bytes_recv=0,
        bytes_sent=0,
        uptime_s=LONG_UPTIME + int(gap),
        pkts_recv=0 if with_packets else None,
        pkts_sent=0 if with_packets else None,
    )
    return prev, curr


def test_the_repro_arithmetic_is_what_we_think_it_is():
    """Guard the premise, so a later refactor cannot quietly invalidate it."""
    assert (0 - PREV_BYTES) % MODULUS_32 == PHANTOM


@pytest.mark.parametrize("gap", [10, 20, 30, 34, 35, 40, 60, 68, 90, 120, 300, 3600])
def test_reset_never_invents_bytes_at_any_gap(gap: int):
    """The whole table above, including the band that used to be wrong."""
    prev, curr = reset_pair(gap, with_packets=False)
    r = counters.classify(prev, curr)

    assert r.down_bytes is None, f"gap={gap}s invented {r.down_bytes:,} bytes down"
    assert r.up_bytes is None, f"gap={gap}s invented {r.up_bytes:,} bytes up"
    assert r.down_wraps is None and r.up_wraps is None
    assert r.kind in ("reset", "anomaly", "gap"), f"gap={gap}s -> {r.kind!r}"


@pytest.mark.parametrize("gap", [34, 35, 40, 60])
def test_the_specific_phantom_band_is_classified_as_reset(gap: int):
    """34-60s is the band B2d exists for; it must name the cause, not shrug."""
    prev, curr = reset_pair(gap, with_packets=False)
    r = counters.classify(prev, curr)

    assert r.kind == "reset"
    assert r.down_bytes != PHANTOM and r.up_bytes != PHANTOM
    assert "reset" in r.note.lower()


def test_packet_corroborated_reset_still_resolves_via_b2b():
    """B2d must not have displaced the stronger, proof-based path."""
    prev, curr = reset_pair(60, with_packets=True)
    r = counters.classify(prev, curr)

    assert r.kind == "reset"
    assert r.down_bytes is None and r.up_bytes is None
    assert "packet" in r.note.lower(), "expected B2b's packet-discontinuity proof"


# ---------------------------------------------------------------------------
# The other half of the contract: B2d must not eat legitimate wraps.
# ---------------------------------------------------------------------------


def test_ordinary_single_direction_wrap_still_reports_real_bytes():
    """The common case on a busy line — the down counter wraps far more often
    than the up counter."""
    traffic = 150_000_000
    prev = Reading(
        ts=1_000.0,
        bytes_recv=MODULUS_32 - 50_000_000,
        bytes_sent=1_000_000_000,
        uptime_s=LONG_UPTIME,
        pkts_recv=None,
        pkts_sent=None,
    )
    curr = Reading(
        ts=1_010.0,
        bytes_recv=(prev.bytes_recv + traffic) % MODULUS_32,
        bytes_sent=prev.bytes_sent + 5_000_000,
        uptime_s=LONG_UPTIME + 10,
        pkts_recv=None,
        pkts_sent=None,
    )

    r = counters.classify(prev, curr)

    assert r.kind == "ok_wrap"
    assert r.down_bytes == traffic
    assert r.up_bytes == 5_000_000
    assert r.down_wraps == 1 and r.up_wraps == 0


def test_clean_increase_is_untouched():
    prev = Reading(
        ts=1_000.0,
        bytes_recv=1_000_000,
        bytes_sent=500_000,
        uptime_s=LONG_UPTIME,
        pkts_recv=None,
        pkts_sent=None,
    )
    curr = Reading(
        ts=1_010.0,
        bytes_recv=11_000_000,
        bytes_sent=1_500_000,
        uptime_s=LONG_UPTIME + 10,
        pkts_recv=None,
        pkts_sent=None,
    )

    r = counters.classify(prev, curr)

    assert r.kind == "ok"
    assert r.down_bytes == 10_000_000
    assert r.up_bytes == 1_000_000


def test_simultaneous_double_wrap_with_small_deltas_is_not_swallowed():
    """Both directions wrapping at once is what B2d keys on — but only when
    both residuals are near a full modulus. A genuine simultaneous wrap has
    small residuals and must still be measured, not discarded."""
    down_traffic, up_traffic = 40_000_000, 30_000_000
    prev = Reading(
        ts=1_000.0,
        bytes_recv=MODULUS_32 - 20_000_000,
        bytes_sent=MODULUS_32 - 10_000_000,
        uptime_s=LONG_UPTIME,
        pkts_recv=None,
        pkts_sent=None,
    )
    curr = Reading(
        ts=1_010.0,
        bytes_recv=(prev.bytes_recv + down_traffic) % MODULUS_32,
        bytes_sent=(prev.bytes_sent + up_traffic) % MODULUS_32,
        uptime_s=LONG_UPTIME + 10,
        pkts_recv=None,
        pkts_sent=None,
    )

    r = counters.classify(prev, curr)

    assert r.kind == "ok_wrap"
    assert r.down_bytes == down_traffic
    assert r.up_bytes == up_traffic
    assert r.down_wraps == 1 and r.up_wraps == 1


def test_one_direction_near_modulus_alone_does_not_trigger_the_guard():
    """B2d requires BOTH directions. A single large residual is B3a's job."""
    prev = Reading(
        ts=1_000.0,
        bytes_recv=PREV_BYTES,
        bytes_sent=1_000_000_000,
        uptime_s=LONG_UPTIME,
        pkts_recv=None,
        pkts_sent=None,
    )
    curr = Reading(
        ts=1_010.0,
        bytes_recv=0,
        bytes_sent=1_005_000_000,  # up increases normally
        uptime_s=LONG_UPTIME + 10,
        pkts_recv=None,
        pkts_sent=None,
    )

    r = counters.classify(prev, curr)

    # Still honest, and still not the phantom — but via the rate ceiling.
    assert r.down_bytes != PHANTOM
    assert "both directions" not in r.note


# ---------------------------------------------------------------------------
# Threshold boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("side", ["below", "above"])
def test_reset_signature_fraction_boundary(side: str):
    """Straddle RESET_SIGNATURE_FRACTION to pin where the guard starts."""
    gap = 60.0
    half = int(RESET_SIGNATURE_FRACTION * MODULUS_32)
    residual = half - 10_000_000 if side == "below" else half + 10_000_000

    # Choose prev so that (curr - prev) % modulus == residual, with curr == 0.
    prev_val = (-residual) % MODULUS_32
    prev = Reading(
        ts=1_000.0,
        bytes_recv=prev_val,
        bytes_sent=prev_val,
        uptime_s=LONG_UPTIME,
        pkts_recv=None,
        pkts_sent=None,
    )
    curr = Reading(
        ts=1_000.0 + gap,
        bytes_recv=0,
        bytes_sent=0,
        uptime_s=LONG_UPTIME + int(gap),
        pkts_recv=None,
        pkts_sent=None,
    )

    r = counters.classify(prev, curr)

    if side == "above":
        assert r.kind == "reset"
        assert r.down_bytes is None
    else:
        # Below the threshold B2d abstains; whatever tier handles it must still
        # never present an unknown as a clean, certain number.
        assert r.kind != "ok"


def test_guard_is_disabled_when_packets_settle_the_interval():
    """packets_settled short-circuits B2d — proven by a genuine wrap that has
    healthy packet counters and near-modulus residuals in both directions."""
    prev = Reading(
        ts=1_000.0,
        bytes_recv=PREV_BYTES,
        bytes_sent=PREV_BYTES,
        uptime_s=LONG_UPTIME,
        pkts_recv=1_000_000,
        pkts_sent=900_000,
    )
    curr = Reading(
        ts=1_010.0,
        bytes_recv=0,
        bytes_sent=0,
        uptime_s=LONG_UPTIME + 10,
        pkts_recv=1_000_050,  # packets kept climbing normally
        pkts_sent=900_040,
    )

    r = counters.classify(prev, curr)

    # Packets are continuous, so this is NOT a reset by B2b's proof; whatever
    # it is classified as, it must not be attributed via B2d's note.
    assert "both directions" not in r.note
    assert r.down_bytes != PHANTOM


def test_thresholds_are_configurable_without_breaking_the_guard():
    """A caller passing custom thresholds must not lose B2d."""
    prev, curr = reset_pair(60, with_packets=False)
    r = counters.classify(prev, curr, Thresholds(max_bytes_per_sec=250_000_000.0))

    assert r.down_bytes is None
    assert dataclasses.is_dataclass(r)
