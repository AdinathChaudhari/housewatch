"""Tests for billing-cycle and local-day date math.

Two timezones carry most of the weight here:

* ``Asia/Yangon`` — a UTC+06:30 zone with no DST. The half-hour offset is the
  reason day bucketing cannot take a shortcut through UTC hours.
* ``America/New_York`` — has DST, so it is the only way to prove that days are
  not assumed to be 86400 seconds long.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from housewatch.billing import cycle_bounds, day_key, local_days_spanned

HALF_HOUR_TZ = ZoneInfo("Asia/Yangon")
NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

SECONDS_PER_DAY = 86_400


# ---------------------------------------------------------------------------
# cycle_bounds
# ---------------------------------------------------------------------------


def test_cycle_bounds_day_one_is_the_calendar_month():
    """The configured house setting: cap resets 00:00 local on the 1st."""
    start, end = cycle_bounds(datetime(2026, 8, 27, 15, 4, tzinfo=HALF_HOUR_TZ), 1)
    assert start == datetime(2026, 8, 1, 0, 0, tzinfo=HALF_HOUR_TZ)
    assert end == datetime(2026, 9, 1, 0, 0, tzinfo=HALF_HOUR_TZ)


def test_cycle_bounds_is_half_open_at_the_reset_instant():
    """Midnight on the reset day belongs to the new cycle, not the old one."""
    start, _ = cycle_bounds(datetime(2026, 9, 1, 0, 0, 0, tzinfo=HALF_HOUR_TZ), 1)
    assert start == datetime(2026, 9, 1, 0, 0, tzinfo=HALF_HOUR_TZ)

    # One second earlier is still the previous cycle.
    start_before, end_before = cycle_bounds(
        datetime(2026, 8, 31, 23, 59, 59, tzinfo=HALF_HOUR_TZ), 1
    )
    assert start_before == datetime(2026, 8, 1, 0, 0, tzinfo=HALF_HOUR_TZ)
    assert end_before == datetime(2026, 9, 1, 0, 0, tzinfo=HALF_HOUR_TZ)


def test_consecutive_cycles_tile_with_no_gap_or_overlap():
    _, end = cycle_bounds(datetime(2026, 8, 15, 12, 0, tzinfo=HALF_HOUR_TZ), 15)
    next_start, _ = cycle_bounds(end, 15)
    assert next_start == end


def test_cycle_bounds_before_reset_day_looks_back_a_month():
    start, end = cycle_bounds(datetime(2026, 8, 3, 9, 0, tzinfo=HALF_HOUR_TZ), 15)
    assert start == datetime(2026, 7, 15, 0, 0, tzinfo=HALF_HOUR_TZ)
    assert end == datetime(2026, 8, 15, 0, 0, tzinfo=HALF_HOUR_TZ)


def test_cycle_bounds_crosses_year_boundary_backwards():
    start, end = cycle_bounds(datetime(2026, 1, 5, 0, 30, tzinfo=HALF_HOUR_TZ), 20)
    assert start == datetime(2025, 12, 20, 0, 0, tzinfo=HALF_HOUR_TZ)
    assert end == datetime(2026, 1, 20, 0, 0, tzinfo=HALF_HOUR_TZ)


def test_cycle_bounds_crosses_year_boundary_forwards():
    start, end = cycle_bounds(datetime(2026, 12, 25, 0, 0, tzinfo=HALF_HOUR_TZ), 20)
    assert start == datetime(2026, 12, 20, 0, 0, tzinfo=HALF_HOUR_TZ)
    assert end == datetime(2027, 1, 20, 0, 0, tzinfo=HALF_HOUR_TZ)


@pytest.mark.parametrize(
    ("now", "expected_start", "expected_end"),
    [
        # A 31st-of-the-month cycle has no 31st in February: clamp to the 28th
        # (2026 is not a leap year) rather than spilling into March.
        (
            datetime(2026, 2, 28, 12, 0, tzinfo=HALF_HOUR_TZ),
            datetime(2026, 2, 28, 0, 0, tzinfo=HALF_HOUR_TZ),
            datetime(2026, 3, 31, 0, 0, tzinfo=HALF_HOUR_TZ),
        ),
        (
            datetime(2026, 2, 10, 12, 0, tzinfo=HALF_HOUR_TZ),
            datetime(2026, 1, 31, 0, 0, tzinfo=HALF_HOUR_TZ),
            datetime(2026, 2, 28, 0, 0, tzinfo=HALF_HOUR_TZ),
        ),
    ],
)
def test_cycle_bounds_clamps_day_31_into_short_months(now, expected_start, expected_end):
    assert cycle_bounds(now, 31) == (expected_start, expected_end)


def test_cycle_bounds_clamps_to_february_29_in_a_leap_year():
    start, _ = cycle_bounds(datetime(2028, 2, 29, 6, 0, tzinfo=HALF_HOUR_TZ), 31)
    assert start == datetime(2028, 2, 29, 0, 0, tzinfo=HALF_HOUR_TZ)


def test_cycle_bounds_rejects_naive_datetime():
    with pytest.raises(ValueError, match="tz-aware"):
        cycle_bounds(datetime(2026, 8, 27, 15, 0), 1)


@pytest.mark.parametrize("bad_day", [0, -1, 32, 99])
def test_cycle_bounds_rejects_out_of_range_start_day(bad_day):
    with pytest.raises(ValueError, match="start_day"):
        cycle_bounds(datetime(2026, 8, 27, 15, 0, tzinfo=HALF_HOUR_TZ), bad_day)


# ---------------------------------------------------------------------------
# day_key
# ---------------------------------------------------------------------------


def test_day_key_uses_local_date_not_utc_date():
    """19:00 UTC on the 26th is already 01:30 on the 27th in a UTC+06:30 zone.

    This is exactly the case a UTC-bucketing implementation gets wrong.
    """
    ts = datetime(2026, 8, 26, 19, 0, tzinfo=UTC).timestamp()
    assert day_key(ts, UTC) == "2026-08-26"
    assert day_key(ts, HALF_HOUR_TZ) == "2026-08-27"


def test_day_key_at_exact_local_midnight_belongs_to_the_new_day():
    ts = datetime(2026, 8, 27, 0, 0, 0, tzinfo=HALF_HOUR_TZ).timestamp()
    assert day_key(ts, HALF_HOUR_TZ) == "2026-08-27"


def test_day_key_one_second_before_local_midnight():
    ts = datetime(2026, 8, 26, 23, 59, 59, tzinfo=HALF_HOUR_TZ).timestamp()
    assert day_key(ts, HALF_HOUR_TZ) == "2026-08-26"


# ---------------------------------------------------------------------------
# local_days_spanned
# ---------------------------------------------------------------------------


def test_empty_when_end_is_not_after_start():
    ts = datetime(2026, 8, 27, 12, 0, tzinfo=HALF_HOUR_TZ).timestamp()
    assert local_days_spanned(ts, ts, HALF_HOUR_TZ) == []
    assert local_days_spanned(ts, ts - 5, HALF_HOUR_TZ) == []


def test_single_segment_within_one_local_day():
    start = datetime(2026, 8, 27, 10, 0, tzinfo=HALF_HOUR_TZ).timestamp()
    end = datetime(2026, 8, 27, 10, 0, 10, tzinfo=HALF_HOUR_TZ).timestamp()

    segments = local_days_spanned(start, end, HALF_HOUR_TZ)

    assert segments == [("2026-08-27", start, end)]


def test_interval_straddling_local_midnight_splits_in_two():
    """The one sample a day that crosses midnight must be apportioned."""
    start = datetime(2026, 8, 27, 23, 59, 55, tzinfo=HALF_HOUR_TZ).timestamp()
    end = datetime(2026, 8, 28, 0, 0, 5, tzinfo=HALF_HOUR_TZ).timestamp()
    midnight = datetime(2026, 8, 28, 0, 0, tzinfo=HALF_HOUR_TZ).timestamp()

    segments = local_days_spanned(start, end, HALF_HOUR_TZ)

    assert segments == [
        ("2026-08-27", start, midnight),
        ("2026-08-28", midnight, end),
    ]


def test_multi_day_gap_produces_one_segment_per_local_day():
    """The shape of a laptop-was-shut-for-days gap interval."""
    start = datetime(2026, 8, 25, 22, 0, tzinfo=HALF_HOUR_TZ).timestamp()
    end = datetime(2026, 8, 28, 3, 0, tzinfo=HALF_HOUR_TZ).timestamp()

    segments = local_days_spanned(start, end, HALF_HOUR_TZ)

    assert [key for key, _, _ in segments] == [
        "2026-08-25",
        "2026-08-26",
        "2026-08-27",
        "2026-08-28",
    ]


def test_segments_tile_the_interval_exactly_once():
    """No gaps, no overlaps, no lost or double-counted seconds.

    This is the property that keeps a long gap from silently losing time when
    it is apportioned across days.
    """
    start = datetime(2026, 8, 25, 22, 0, tzinfo=HALF_HOUR_TZ).timestamp()
    end = datetime(2026, 8, 28, 3, 0, tzinfo=HALF_HOUR_TZ).timestamp()

    segments = local_days_spanned(start, end, HALF_HOUR_TZ)

    assert segments[0][1] == start
    assert segments[-1][2] == end
    for (_, _, prev_end), (_, next_start, _) in zip(segments, segments[1:]):
        assert prev_end == next_start
    assert sum(e - s for _, s, e in segments) == pytest.approx(end - start)


def test_day_keys_are_strictly_increasing_and_unique():
    start = datetime(2026, 8, 25, 22, 0, tzinfo=HALF_HOUR_TZ).timestamp()
    end = datetime(2026, 9, 2, 3, 0, tzinfo=HALF_HOUR_TZ).timestamp()

    keys = [key for key, _, _ in local_days_spanned(start, end, HALF_HOUR_TZ)]

    assert keys == sorted(keys)
    assert len(keys) == len(set(keys))


def test_dst_spring_forward_day_is_23_hours_not_24():
    """2026-03-08 in New York loses an hour at 02:00.

    A hardcoded 86400 would over-measure this day and shift every boundary
    after it.
    """
    start = datetime(2026, 3, 8, 0, 0, tzinfo=NY).timestamp()
    end = datetime(2026, 3, 9, 0, 0, tzinfo=NY).timestamp()

    segments = local_days_spanned(start, end, NY)

    assert [key for key, _, _ in segments] == ["2026-03-08"]
    assert segments[0][2] - segments[0][1] == 23 * 3600
    assert end - start < SECONDS_PER_DAY


def test_dst_fall_back_day_is_25_hours_not_24():
    """2026-11-01 in New York repeats an hour at 02:00."""
    start = datetime(2026, 11, 1, 0, 0, tzinfo=NY).timestamp()
    end = datetime(2026, 11, 2, 0, 0, tzinfo=NY).timestamp()

    segments = local_days_spanned(start, end, NY)

    assert [key for key, _, _ in segments] == ["2026-11-01"]
    assert segments[0][2] - segments[0][1] == 25 * 3600
    assert end - start > SECONDS_PER_DAY


def test_interval_across_a_dst_transition_still_tiles_exactly():
    start = datetime(2026, 3, 7, 12, 0, tzinfo=NY).timestamp()
    end = datetime(2026, 3, 9, 12, 0, tzinfo=NY).timestamp()

    segments = local_days_spanned(start, end, NY)

    assert [key for key, _, _ in segments] == ["2026-03-07", "2026-03-08", "2026-03-09"]
    assert sum(e - s for _, s, e in segments) == pytest.approx(end - start)
    assert segments[0][1] == start
    assert segments[-1][2] == end


def test_rejects_absurdly_long_interval():
    """A corrupt timestamp must fail loudly rather than spin."""
    start = datetime(1970, 1, 1, tzinfo=UTC).timestamp()
    end = datetime(2026, 1, 1, tzinfo=UTC).timestamp()

    with pytest.raises(ValueError, match="corrupt"):
        local_days_spanned(start, end, HALF_HOUR_TZ)


def test_day_key_of_each_segment_matches_its_start_instant():
    """Cross-check: the two functions must agree on which day an instant is in."""
    start = datetime(2026, 8, 25, 22, 0, tzinfo=HALF_HOUR_TZ).timestamp()
    end = datetime(2026, 8, 28, 3, 0, tzinfo=HALF_HOUR_TZ).timestamp()

    for key, seg_start, _ in local_days_spanned(start, end, HALF_HOUR_TZ):
        assert day_key(seg_start, HALF_HOUR_TZ) == key


def test_segments_of_a_cycle_cover_every_day_of_that_cycle():
    """Integration of the two halves of this module: a full August cycle."""
    cycle_start, cycle_end = cycle_bounds(
        datetime(2026, 8, 27, 15, 0, tzinfo=HALF_HOUR_TZ), 1
    )
    segments = local_days_spanned(
        cycle_start.timestamp(), cycle_end.timestamp(), HALF_HOUR_TZ
    )

    assert len(segments) == 31  # August
    assert segments[0][0] == "2026-08-01"
    assert segments[-1][0] == "2026-08-31"
    assert all(
        e - s == SECONDS_PER_DAY for _, s, e in segments
    ), "this zone has no DST, so every day in the cycle is exactly 24h"


def test_february_cycle_has_28_days_in_a_common_year():
    cycle_start, cycle_end = cycle_bounds(datetime(2026, 2, 14, tzinfo=HALF_HOUR_TZ), 1)
    segments = local_days_spanned(
        cycle_start.timestamp(), cycle_end.timestamp(), HALF_HOUR_TZ
    )
    assert len(segments) == 28


def test_february_cycle_has_29_days_in_a_leap_year():
    cycle_start, cycle_end = cycle_bounds(datetime(2028, 2, 14, tzinfo=HALF_HOUR_TZ), 1)
    segments = local_days_spanned(
        cycle_start.timestamp(), cycle_end.timestamp(), HALF_HOUR_TZ
    )
    assert len(segments) == 29


def test_half_hour_offset_shifts_the_utc_boundary():
    """Local midnight in a UTC+06:30 zone is 17:30 UTC the previous day.

    Stated as an explicit assertion because it is the single fact that makes
    UTC-hour bucketing wrong in a half-hour-offset zone.
    """
    local_midnight = datetime(2026, 8, 27, 0, 0, tzinfo=HALF_HOUR_TZ)
    as_utc = local_midnight.astimezone(UTC)
    assert (as_utc.hour, as_utc.minute) == (17, 30)
    assert as_utc.date() == (local_midnight.date() - timedelta(days=1))
