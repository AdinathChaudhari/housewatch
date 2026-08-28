"""Pure billing-cycle and local-day date math.

Kept separate from ``counters.py`` because it is a different pure domain: that
module reasons about 32-bit arithmetic, this one about calendars.

Everything here works in **local** time and converts to UTC epoch seconds only
at the boundaries. That is deliberate. "Today's usage" and "this cycle's usage"
are questions about the user's wall clock, and half-hour-offset zones exist
(UTC+05:30, UTC+05:45, UTC+09:30) — which means any shortcut that buckets on
UTC hours misaligns every single day boundary in those zones. Days are also
not assumed to be 86400 seconds long: local midnights are resolved
individually, so a day that crosses a DST transition correctly comes out 23 or
25 hours long. Some zones have no DST at all, but the collector can run
anywhere, and a date bug that only appears twice a year in one timezone is not
a bug anyone finds quickly.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, tzinfo
from datetime import time as dtime

__all__ = ["cycle_bounds", "day_key", "local_days_spanned"]


# A single interval is split at local midnights; this bounds the loop so a
# corrupt timestamp cannot spin forever. ~27 years of days is far beyond any
# real interval (the longest plausible one is a multi-week laptop shutdown).
_MAX_DAY_SEGMENTS = 10_000

_SECONDS_PER_DAY = 86_400


def _clamped_day(year: int, month: int, start_day: int) -> int:
    """Clamp ``start_day`` to a day that exists in the given month.

    A cycle starting on the 31st has no 31st in February. Clamping down keeps
    the cycle anchored to month end rather than silently spilling into the next
    month. ``config`` restricts the configured value to 1–28 so this never
    fires in practice, but the function is correct for any input so that the
    restriction can be relaxed later without a subtle break.
    """
    return min(start_day, calendar.monthrange(year, month)[1])


def _local_midnight(day: date, tz: tzinfo) -> datetime:
    """Local 00:00 on ``day``, as a tz-aware datetime.

    ``fold=0`` picks the first occurrence when a wall time is ambiguous (a
    DST fall-back that lands on midnight).
    """
    return datetime.combine(day, dtime(0, 0), tzinfo=tz).replace(fold=0)


def cycle_bounds(now_local: datetime, start_day: int) -> tuple[datetime, datetime]:
    """Return ``[cycle_start, next_cycle_start)`` containing ``now_local``.

    Half-open so that consecutive cycles tile the timeline with no gap and no
    double-counted midnight.

    Args:
        now_local: tz-aware local datetime inside the cycle of interest.
        start_day: day of month the cycle resets on, 1–31 (clamped per month).

    Raises:
        ValueError: ``now_local`` is naive, or ``start_day`` is out of range.
    """
    if now_local.tzinfo is None:
        raise ValueError("cycle_bounds requires a tz-aware now_local")
    if not 1 <= start_day <= 31:
        raise ValueError(f"start_day must be 1-31, got {start_day!r}")

    tz = now_local.tzinfo
    year, month = now_local.year, now_local.month

    # If today is on or past this month's (clamped) reset day, the current
    # cycle opened this month; otherwise it opened in the previous month.
    if now_local.day >= _clamped_day(year, month, start_day):
        cy, cm = year, month
    elif month == 1:
        cy, cm = year - 1, 12
    else:
        cy, cm = year, month - 1

    start = _local_midnight(date(cy, cm, _clamped_day(cy, cm, start_day)), tz)

    ny, nm = (cy + 1, 1) if cm == 12 else (cy, cm + 1)
    end = _local_midnight(date(ny, nm, _clamped_day(ny, nm, start_day)), tz)

    return start, end


def day_key(ts_utc: float, tz: tzinfo) -> str:
    """``'YYYY-MM-DD'`` of the local date containing the UTC instant ``ts_utc``.

    This string is the database's day bucket key. It is stored rather than
    derived at query time so that a later timezone config change cannot
    silently rewrite history.
    """
    return datetime.fromtimestamp(ts_utc, tz).strftime("%Y-%m-%d")


def local_days_spanned(
    start_utc: float, end_utc: float, tz: tzinfo
) -> list[tuple[str, float, float]]:
    """Split ``[start_utc, end_utc)`` at local midnights.

    Returns ``[(day_key, overlap_start_utc, overlap_end_utc), ...]`` in
    chronological order, covering the interval exactly once with no gaps.
    Empty when ``end_utc <= start_utc``.

    This is how an interval that straddles midnight gets apportioned to the
    right days instead of being dumped wholly onto whichever day it happened to
    end in — which at a 10-second poll interval matters for at most one sample
    a day, but matters absolutely for a multi-hour gap interval after the Mac
    wakes up.

    Raises:
        ValueError: the interval is implausibly long, which indicates a corrupt
            timestamp rather than real elapsed time.
    """
    if end_utc <= start_utc:
        return []

    segments: list[tuple[str, float, float]] = []
    cursor = start_utc

    while cursor < end_utc:
        if len(segments) >= _MAX_DAY_SEGMENTS:
            raise ValueError(
                f"interval [{start_utc}, {end_utc}) spans more than "
                f"{_MAX_DAY_SEGMENTS} local days; timestamps are almost "
                f"certainly corrupt"
            )

        local = datetime.fromtimestamp(cursor, tz)
        next_midnight = _local_midnight(local.date() + timedelta(days=1), tz).timestamp()

        # Guard against a pathological tz where the computed next midnight does
        # not advance (a DST transition that skips 00:00). Stepping a flat day
        # keeps the loop monotonic; the day_key stays correct because it is
        # taken from the cursor, not from the boundary.
        if next_midnight <= cursor:
            next_midnight = cursor + _SECONDS_PER_DAY

        segment_end = min(next_midnight, end_utc)
        segments.append((local.strftime("%Y-%m-%d"), cursor, segment_end))
        cursor = segment_end

    return segments
