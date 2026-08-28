"""Tests for housewatch.store: schema, writer transaction, and the query API.

Scope note: cross-module date algebra (exact billing-cycle clamping rules,
exact local-midnight split points) is billing.py's own test_billing.py's
job. These tests exercise what store.py owns: that it accrues whatever
billing.py hands back correctly and losslessly, that day/cycle queries slice
the right rows, and — the hard requirement — that "unknown" and "zero" can
never be confused by a reader of DayUsage/CycleSummary.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from housewatch import store as store_mod
from housewatch.config import Config
from housewatch.model import IntervalResult, Reading
from housewatch.store import Store

UTC = ZoneInfo("UTC")
HALF_HOUR_TZ = ZoneInfo("Asia/Yangon")
NEW_YORK = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _reading(ts: float, recv: int = 0, sent: int = 0, uptime: int = 1000) -> Reading:
    return Reading(
        ts=ts, bytes_recv=recv, bytes_sent=sent, uptime_s=uptime,
        pkts_recv=None, pkts_sent=None,
    )


def _baseline(ts: float) -> IntervalResult:
    return IntervalResult(
        kind="baseline", start_ts=ts, end_ts=ts,
        down_bytes=None, up_bytes=None, down_wraps=None, up_wraps=None,
        note="first sample",
    )


def _ok(start_ts: float, end_ts: float, down: int, up: int) -> IntervalResult:
    return IntervalResult(
        kind="ok", start_ts=start_ts, end_ts=end_ts,
        down_bytes=down, up_bytes=up, down_wraps=0, up_wraps=0, note="",
    )


def _recovered(start_ts: float, end_ts: float, down: int, up: int) -> IntervalResult:
    return IntervalResult(
        kind="recovered", start_ts=start_ts, end_ts=end_ts,
        down_bytes=down, up_bytes=up, down_wraps=0, up_wraps=0,
        note="resolved by packet envelope",
    )


def _suspect(start_ts: float, end_ts: float, down: int, up: int) -> IntervalResult:
    return IntervalResult(
        kind="suspect", start_ts=start_ts, end_ts=end_ts,
        down_bytes=down, up_bytes=up, down_wraps=0, up_wraps=0,
        note="possible undercount",
    )


def _gap(start_ts: float, end_ts: float) -> IntervalResult:
    return IntervalResult(
        kind="gap", start_ts=start_ts, end_ts=end_ts,
        down_bytes=None, up_bytes=None, down_wraps=None, up_wraps=None,
        note="delta unrecoverable: multiple wraps possible",
    )


def _reset(start_ts: float, end_ts: float) -> IntervalResult:
    return IntervalResult(
        kind="reset", start_ts=start_ts, end_ts=end_ts,
        down_bytes=None, up_bytes=None, down_wraps=None, up_wraps=None,
        note="uptime prev->curr over gap",
    )


def _local_midnight_utc(day: str, tz) -> float:
    return Store._day_bounds_utc(day, tz)[0]


# ---------------------------------------------------------------------------
# schema creation, idempotent re-open
# ---------------------------------------------------------------------------


def test_init_schema_creates_tables(tmp_db_path: Path) -> None:
    with Store(tmp_db_path, readonly=False) as s:
        s.init_schema()
        tables = {
            row[0]
            for row in s._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {"meta", "samples", "intervals", "days", "events"} <= tables


def test_init_schema_stamps_version(tmp_db_path: Path) -> None:
    with Store(tmp_db_path, readonly=False) as s:
        s.init_schema()
        assert s.get_meta("schema_version") == "1"
        assert s.get_meta("created_at") is not None


def test_init_schema_idempotent_reopen(tmp_db_path: Path) -> None:
    with Store(tmp_db_path, readonly=False) as s:
        s.init_schema()
        s.record_event(1000.0, "collector_start", "")

    # Reopen and re-init: must not raise, and must not wipe existing data.
    with Store(tmp_db_path, readonly=False) as s:
        s.init_schema()
        s.init_schema()  # twice in a row too
        events = s.recent_events(10)
    assert len(events) == 1
    assert events[0][1] == "collector_start"


def test_journal_mode_is_wal(tmp_db_path: Path) -> None:
    with Store(tmp_db_path, readonly=False) as s:
        s.init_schema()
        mode = s._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"


# ---------------------------------------------------------------------------
# insert / read round trip
# ---------------------------------------------------------------------------


def test_record_poll_baseline_then_ok_round_trips(tmp_db_path: Path) -> None:
    day = "2025-06-15"
    t0 = _local_midnight_utc(day, UTC) + 3600  # 01:00 UTC that day
    t1 = t0 + 10

    with Store(tmp_db_path, readonly=False) as s:
        s.init_schema()
        s.record_poll(_reading(t0), _baseline(t0), UTC)
        s.record_poll(_reading(t1, recv=1000, sent=200), _ok(t0, t1, 1000, 200), UTC)

        du = s.day_usage(day, UTC, now_ts=t1 + 1)
        assert du.down_bytes == 1000
        assert du.up_bytes == 200
        assert du.ok_s == pytest.approx(10.0)
        assert du.suspect_down == 0
        assert du.suspect_up == 0

        assert s.last_reading() == _reading(t1, recv=1000, sent=200)

        samples = s._conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        intervals = s._conn.execute("SELECT COUNT(*) FROM intervals").fetchone()[0]
        assert samples == 2
        assert intervals == 2  # baseline row IS persisted


def test_baseline_interval_does_not_touch_days_table(tmp_db_path: Path) -> None:
    t0 = _local_midnight_utc("2025-06-15", UTC) + 3600
    with Store(tmp_db_path, readonly=False) as s:
        s.init_schema()
        s.record_poll(_reading(t0), _baseline(t0), UTC)
        rows = s._conn.execute("SELECT COUNT(*) FROM days").fetchone()[0]
    assert rows == 0


def test_suspect_bytes_land_in_both_totals_and_suspect_subset(tmp_db_path: Path) -> None:
    day = "2025-06-15"
    t0 = _local_midnight_utc(day, UTC) + 3600
    t1 = t0 + 40

    with Store(tmp_db_path, readonly=False) as s:
        s.init_schema()
        s.record_poll(_reading(t0), _baseline(t0), UTC)
        s.record_poll(_reading(t1), _suspect(t0, t1, 5_000_000_000, 1_000_000_000), UTC)
        du = s.day_usage(day, UTC, now_ts=t1 + 1)

    assert du.down_bytes == 5_000_000_000  # suspect bytes ARE counted in totals...
    assert du.suspect_down == 5_000_000_000  # ...AND flagged separately for the UI
    assert du.up_bytes == 1_000_000_000
    assert du.suspect_up == 1_000_000_000
    assert du.suspect_s == pytest.approx(40.0)


# ---------------------------------------------------------------------------
# the critical honesty invariant: unknown must never read back as zero
# ---------------------------------------------------------------------------


def test_gap_interval_never_reads_as_zero_usage(tmp_db_path: Path) -> None:
    """A day entirely covered by a 'gap' interval must be distinguishable
    from a day that genuinely saw zero bytes: down_bytes/up_bytes are 0 in
    both cases, but gap_s (or uncovered_s) must be nonzero ONLY for the
    unknown day, so a UI reading DayUsage can never conflate the two."""
    day = "2025-06-20"
    day_start = _local_midnight_utc(day, UTC)
    day_end = _local_midnight_utc("2025-06-21", UTC)

    with Store(tmp_db_path, readonly=False) as s:
        s.init_schema()
        s.record_poll(_reading(day_start), _baseline(day_start), UTC)
        s.record_poll(_reading(day_end, uptime=999999), _gap(day_start, day_end), UTC)
        du_unknown = s.day_usage(day, UTC, now_ts=day_end)

        # A genuinely idle day (an 'ok' interval with zero delta) for contrast.
        idle_day = "2025-06-21"
        idle_start = day_end
        idle_end = idle_start + 10
        s.record_poll(_reading(idle_end), _ok(idle_start, idle_end, 0, 0), UTC)
        du_idle = s.day_usage(idle_day, UTC, now_ts=idle_end)

    assert du_unknown.down_bytes == 0
    assert du_unknown.up_bytes == 0
    assert du_unknown.gap_s == pytest.approx(86400.0)
    assert du_unknown.uncovered_s == 0.0  # the gap interval itself covers the whole day

    assert du_idle.down_bytes == 0
    assert du_idle.up_bytes == 0
    assert du_idle.gap_s == 0.0
    assert du_idle.ok_s == pytest.approx(10.0)
    # Idle day is NOT flagged unknown even though bytes are also 0 — the
    # distinguishing signal is gap_s/uncovered_s, not down_bytes/up_bytes.


def test_day_with_no_rows_at_all_is_fully_uncovered_not_zero(tmp_db_path: Path) -> None:
    """A day the collector never even ran on: no `days` row exists at all.
    down_bytes reads back as 0 (there is no other sane value), but
    uncovered_s must equal the FULL elapsed day, proving the API can tell
    'never observed' apart from 'observed, zero usage'."""
    with Store(tmp_db_path, readonly=False) as s:
        s.init_schema()
        # DB has data for a totally different day only.
        t0 = _local_midnight_utc("2025-01-01", UTC)
        s.record_poll(_reading(t0), _baseline(t0), UTC)

        day = "2025-06-20"
        now_ts = _local_midnight_utc("2025-06-21", UTC)  # full day has elapsed
        du = s.day_usage(day, UTC, now_ts=now_ts)

    assert du.down_bytes == 0
    assert du.up_bytes == 0
    assert du.uncovered_s == pytest.approx(86400.0)
    assert du.ok_s == du.recovered_s == du.suspect_s == du.gap_s == 0.0


def test_reset_interval_recorded_with_null_bytes(tmp_db_path: Path) -> None:
    t0 = _local_midnight_utc("2025-06-15", UTC) + 3600
    t1 = t0 + 15
    with Store(tmp_db_path, readonly=False) as s:
        s.init_schema()
        s.record_poll(_reading(t0), _baseline(t0), UTC)
        s.record_poll(_reading(t1, uptime=5), _reset(t0, t1), UTC)
        row = s._conn.execute(
            "SELECT kind, down_bytes, up_bytes FROM intervals WHERE kind='reset'"
        ).fetchone()
    assert row["kind"] == "reset"
    assert row["down_bytes"] is None
    assert row["up_bytes"] is None


# ---------------------------------------------------------------------------
# schema-level honesty invariant (CHECK constraints)
# ---------------------------------------------------------------------------


def test_schema_rejects_bytes_on_a_gap_kind_row(tmp_db_path: Path) -> None:
    with Store(tmp_db_path, readonly=False) as s:
        s.init_schema()
        with pytest.raises(sqlite3.IntegrityError):
            s._conn.execute(
                "INSERT INTO intervals(start_ts, end_ts, kind, down_bytes, up_bytes, "
                "down_wraps, up_wraps, note) VALUES (0, 10, 'gap', 100, 0, NULL, NULL, '')"
            )


def test_schema_rejects_null_bytes_on_an_ok_kind_row(tmp_db_path: Path) -> None:
    with Store(tmp_db_path, readonly=False) as s:
        s.init_schema()
        with pytest.raises(sqlite3.IntegrityError):
            s._conn.execute(
                "INSERT INTO intervals(start_ts, end_ts, kind, down_bytes, up_bytes, "
                "down_wraps, up_wraps, note) VALUES (0, 10, 'ok', NULL, 0, 0, 0, '')"
            )


# ---------------------------------------------------------------------------
# multi-day split: exact byte/duration conservation regardless of billing's
# exact per-second split point
# ---------------------------------------------------------------------------


def test_interval_spanning_midnight_splits_across_two_days_losslessly(
    tmp_db_path: Path,
) -> None:
    day1 = "2025-06-15"
    day2 = "2025-06-16"
    midnight = _local_midnight_utc(day2, UTC)
    start = midnight - 600  # 10 min before local midnight
    end = midnight + 600  # 10 min after
    total_down = 123_456_789
    total_up = 987_654

    with Store(tmp_db_path, readonly=False) as s:
        s.init_schema()
        s.record_poll(_reading(start), _baseline(start), UTC)
        s.record_poll(
            _reading(end, recv=total_down, sent=total_up),
            _recovered(start, end, total_down, total_up),
            UTC,
        )
        du1 = s.day_usage(day1, UTC, now_ts=end)
        du2 = s.day_usage(day2, UTC, now_ts=end)

    # Both days got some credit...
    assert du1.down_bytes > 0
    assert du2.down_bytes > 0
    # ...and the sum is EXACT (no bytes invented or dropped at the split).
    assert du1.down_bytes + du2.down_bytes == total_down
    assert du1.up_bytes + du2.up_bytes == total_up
    assert du1.recovered_s + du2.recovered_s == pytest.approx(end - start, abs=1e-6)


# ---------------------------------------------------------------------------
# DST-transition day bucketing (America/New_York; a half-hour-offset zone
# has no DST shift)
# ---------------------------------------------------------------------------


def test_spring_forward_day_is_23_hours(tmp_db_path: Path) -> None:
    # 2024-03-10: US spring-forward (clocks skip 02:00->03:00 local).
    day = "2024-03-10"
    now_ts = _local_midnight_utc("2024-03-11", NEW_YORK)  # full day elapsed
    with Store(tmp_db_path, readonly=False) as s:
        s.init_schema()
        du = s.day_usage(day, NEW_YORK, now_ts=now_ts)
    assert du.uncovered_s == pytest.approx(23 * 3600.0)


def test_fall_back_day_is_25_hours(tmp_db_path: Path) -> None:
    # 2024-11-03: US fall-back (clocks repeat 01:00-02:00 local).
    day = "2024-11-03"
    now_ts = _local_midnight_utc("2024-11-04", NEW_YORK)
    with Store(tmp_db_path, readonly=False) as s:
        s.init_schema()
        du = s.day_usage(day, NEW_YORK, now_ts=now_ts)
    assert du.uncovered_s == pytest.approx(25 * 3600.0)


def test_half_hour_zone_has_no_dst_day_is_24_hours(tmp_db_path: Path) -> None:
    day = "2025-06-15"
    now_ts = _local_midnight_utc("2025-06-16", HALF_HOUR_TZ)
    with Store(tmp_db_path, readonly=False) as s:
        s.init_schema()
        du = s.day_usage(day, HALF_HOUR_TZ, now_ts=now_ts)
    assert du.uncovered_s == pytest.approx(24 * 3600.0)


# ---------------------------------------------------------------------------
# recent_days
# ---------------------------------------------------------------------------


def test_recent_days_oldest_first_includes_today(tmp_db_path: Path) -> None:
    today = "2025-06-20"
    now_ts = _local_midnight_utc(today, UTC) + 3600
    with Store(tmp_db_path, readonly=False) as s:
        s.init_schema()
        days = s.recent_days(3, UTC, now_ts)
    assert [d.day for d in days] == ["2025-06-18", "2025-06-19", "2025-06-20"]


# ---------------------------------------------------------------------------
# billing-cycle math across a month boundary + cap projection
# ---------------------------------------------------------------------------


def test_cycle_summary_month_boundary_only_includes_current_cycle_days(
    tmp_db_path: Path,
) -> None:
    cfg = Config(
        billing_cycle_start_day=1,
        timezone="UTC",
        db_path=str(tmp_db_path),
        monthly_cap_gb=None,
    )
    with Store(tmp_db_path, readonly=False) as s:
        s.init_schema()

        # One 'ok' interval entirely on 2025-01-31 (PREVIOUS cycle: excluded).
        jan31_start = _local_midnight_utc("2025-01-31", UTC) + 3600
        jan31_end = jan31_start + 10
        s.record_poll(_reading(jan31_start), _baseline(jan31_start), UTC)
        s.record_poll(
            _reading(jan31_end, recv=999_999_999, sent=1),
            _ok(jan31_start, jan31_end, 999_999_999, 1),
            UTC,
        )

        # One 'ok' interval on 2025-02-05 (CURRENT cycle: included).
        feb5_start = _local_midnight_utc("2025-02-05", UTC) + 3600
        feb5_end = feb5_start + 10
        s.record_poll(
            _reading(feb5_end, recv=3_000_000_000, sent=1_500_000_000),
            _ok(feb5_start, feb5_end, 2_000_000_000, 1_500_000_000),
            UTC,
        )

        now_ts = _local_midnight_utc("2025-02-10", UTC) + 3600
        summary = s.cycle_summary(cfg, now_ts)

    assert summary.cycle_start == "2025-02-01"
    assert summary.used_bytes == 2_000_000_000 + 1_500_000_000  # Jan 31 excluded
    assert summary.days_total == pytest.approx(28.0)  # Feb 2025, non-leap
    assert 9.0 < summary.days_elapsed < 10.0


def test_cycle_summary_cap_projection_and_over_cap_flag(tmp_db_path: Path) -> None:
    cfg = Config(
        billing_cycle_start_day=1,
        timezone="UTC",
        db_path=str(tmp_db_path),
        monthly_cap_gb=1.0,  # 1 GB cap -> 1_000_000_000 bytes
    )
    with Store(tmp_db_path, readonly=False) as s:
        s.init_schema()
        start = _local_midnight_utc("2025-03-01", UTC)
        # Burn 900 MB in the first hour of a 31-day cycle.
        t1 = start + 3600
        s.record_poll(_reading(start), _baseline(start), UTC)
        s.record_poll(
            _reading(t1, recv=900_000_000, sent=0),
            _ok(start, t1, 900_000_000, 0),
            UTC,
        )
        now_ts = t1
        summary = s.cycle_summary(cfg, now_ts)

    assert summary.cap_bytes == 1_000_000_000
    assert summary.used_bytes == 900_000_000
    # At 900MB/hour sustained over a 31-day cycle the projection blows past 1GB.
    assert summary.projected_bytes > summary.cap_bytes
    assert summary.over_cap_projected is True


def test_cycle_summary_no_cap_is_none(tmp_db_path: Path) -> None:
    cfg = Config(billing_cycle_start_day=1, timezone="UTC", db_path=str(tmp_db_path))
    with Store(tmp_db_path, readonly=False) as s:
        s.init_schema()
        now_ts = _local_midnight_utc("2025-03-15", UTC)
        summary = s.cycle_summary(cfg, now_ts)
    assert summary.cap_bytes is None
    assert summary.over_cap_projected is None


# ---------------------------------------------------------------------------
# gap/suspect windows surfaced honestly
# ---------------------------------------------------------------------------


def test_notable_intervals_surfaces_gap_and_suspect_but_not_ok(tmp_db_path: Path) -> None:
    t0 = _local_midnight_utc("2025-06-15", UTC)
    t1 = t0 + 10
    t2 = t1 + 3600
    t3 = t2 + 40

    with Store(tmp_db_path, readonly=False) as s:
        s.init_schema()
        s.record_poll(_reading(t0), _baseline(t0), UTC)
        s.record_poll(_reading(t1, recv=10, sent=10), _ok(t0, t1, 10, 10), UTC)
        s.record_poll(_reading(t2, uptime=99999999), _gap(t1, t2), UTC)
        s.record_poll(_reading(t3), _suspect(t2, t3, 4_000_000_000, 0), UTC)

        windows = s.notable_intervals(t0, t3)

    kinds = [w.kind for w in windows]
    assert kinds == ["gap", "suspect"]
    gap_window = windows[0]
    assert gap_window.down_bytes is None
    assert gap_window.start_ts == t1
    assert gap_window.end_ts == t2
    suspect_window = windows[1]
    assert suspect_window.down_bytes == 4_000_000_000


# ---------------------------------------------------------------------------
# rate_series: holes as None, never a fake zero line
# ---------------------------------------------------------------------------


def test_rate_series_excludes_baseline_and_holes_unknown_kinds(tmp_db_path: Path) -> None:
    t0 = _local_midnight_utc("2025-06-15", UTC)
    t1 = t0 + 10
    t2 = t1 + 20

    with Store(tmp_db_path, readonly=False) as s:
        s.init_schema()
        s.record_poll(_reading(t0), _baseline(t0), UTC)
        s.record_poll(_reading(t1, recv=800, sent=400), _ok(t0, t1, 800, 400), UTC)
        s.record_poll(_reading(t2, uptime=1), _reset(t1, t2), UTC)

        points = s.rate_series(minutes=60, now_ts=t2 + 1)

    assert len(points) == 2  # baseline excluded
    ok_point, reset_point = points
    assert ok_point.kind == "ok"
    assert ok_point.down_bps == pytest.approx(800 * 8 / 10)
    assert ok_point.up_bps == pytest.approx(400 * 8 / 10)
    assert reset_point.kind == "reset"
    assert reset_point.down_bps is None
    assert reset_point.up_bps is None


# ---------------------------------------------------------------------------
# events, meta, last_reading survives restart
# ---------------------------------------------------------------------------


def test_last_reading_survives_close_and_reopen(tmp_db_path: Path) -> None:
    t0 = 1_700_000_000.0
    with Store(tmp_db_path, readonly=False) as s:
        s.init_schema()
        s.record_poll(_reading(t0, recv=1, sent=2, uptime=3), _baseline(t0), UTC)

    with Store(tmp_db_path, readonly=False) as s:
        assert s.last_reading() == _reading(t0, recv=1, sent=2, uptime=3)


def test_recent_events_most_recent_first(tmp_db_path: Path) -> None:
    with Store(tmp_db_path, readonly=False) as s:
        s.init_schema()
        s.record_event(100.0, "collector_start", "")
        s.record_event(200.0, "failures", "3 consecutive")
        s.record_event(300.0, "recovered", "after 3")
        events = s.recent_events(2)
    assert [e[1] for e in events] == ["recovered", "failures"]


# ---------------------------------------------------------------------------
# retention / pruning
# ---------------------------------------------------------------------------


def test_prune_deletes_old_rows_per_retention(tmp_db_path: Path) -> None:
    cfg = Config(
        db_path=str(tmp_db_path),
        retention_samples_days=1,
        retention_intervals_days=45,
        retention_events_days=1,
        timezone="UTC",
    )
    now_ts = _local_midnight_utc("2025-06-20", UTC) + 3600
    old_ts = now_ts - 10 * 86400  # 10 days old: past the 1-day sample/event retention
    recent_ts = now_ts - 3600

    with Store(tmp_db_path, readonly=False) as s:
        s.init_schema()
        s.record_poll(_reading(old_ts), _baseline(old_ts), UTC)
        s.record_poll(_reading(recent_ts), _baseline(recent_ts), UTC)
        s.record_event(old_ts, "collector_start", "")
        s.record_event(recent_ts, "collector_start", "")

        s.prune(now_ts, cfg)

        sample_tss = {r[0] for r in s._conn.execute("SELECT ts FROM samples").fetchall()}
        event_tss = {r[0] for r in s._conn.execute("SELECT ts FROM events").fetchall()}

    assert old_ts not in sample_tss
    assert recent_ts in sample_tss
    assert old_ts not in event_tss
    assert recent_ts in event_tss


class _ExecuteSpy:
    """Wraps a sqlite3.Connection to record which SQL strings pass through
    execute(). sqlite3.Connection.execute is a read-only C-level attribute
    on the instance, so it can't be monkeypatched directly -- instead we
    substitute Store._conn itself with this proxy, which forwards
    everything else (row_factory, executescript, ...) unchanged."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._real = conn
        self.calls: list[str] = []

    def execute(self, sql, *a, **kw):
        self.calls.append(sql)
        return self._real.execute(sql, *a, **kw)

    def __getattr__(self, name):
        return getattr(self._real, name)

    # `with self._conn:` (used by record_poll/prune/etc.) relies on the
    # context-manager protocol, which Python looks up on the type, not via
    # __getattr__ instance fallback -- so these must be defined explicitly.
    def __enter__(self):
        return self._real.__enter__()

    def __exit__(self, exc_type, exc, tb):
        return self._real.__exit__(exc_type, exc, tb)


def test_prune_wal_checkpoints_at_most_once_per_local_day(tmp_db_path: Path) -> None:
    cfg = Config(db_path=str(tmp_db_path), timezone="UTC")
    day1_ts = _local_midnight_utc("2025-06-20", UTC) + 100
    with Store(tmp_db_path, readonly=False) as s:
        s.init_schema()
        spy = _ExecuteSpy(s._conn)
        s._conn = spy  # type: ignore[assignment]

        s.prune(day1_ts, cfg)
        s.prune(day1_ts + 60, cfg)  # same local day: must NOT checkpoint again
        s.prune(day1_ts + 86400, cfg)  # next local day: must checkpoint again

        s._conn = spy._real  # restore real connection so `with` can close it cleanly

    checkpoint_calls = [c for c in spy.calls if "wal_checkpoint" in c]
    assert len(checkpoint_calls) == 2


# ---------------------------------------------------------------------------
# readonly store: enforced write guard
# ---------------------------------------------------------------------------


def test_readonly_store_requires_existing_db(tmp_db_path: Path) -> None:
    assert not tmp_db_path.exists()
    with pytest.raises(FileNotFoundError):
        Store(tmp_db_path, readonly=True)


def test_readonly_store_cannot_write(tmp_db_path: Path) -> None:
    with Store(tmp_db_path, readonly=False) as s:
        s.init_schema()

    with Store(tmp_db_path, readonly=True) as ro:
        with pytest.raises(PermissionError):
            ro.record_event(1.0, "x", "")
        with pytest.raises(PermissionError):
            ro.set_meta("k", "v")
        with pytest.raises(PermissionError):
            ro.init_schema()
        with pytest.raises(PermissionError):
            ro.record_poll(_reading(1.0), _baseline(1.0), UTC)

        # Even a raw write attempt against the DB-level mode=ro connection
        # fails, as a backstop below our own guard.
        with pytest.raises(sqlite3.OperationalError):
            ro._conn.execute("INSERT INTO events(ts, kind, detail) VALUES (1, 'x', '')")


def test_readonly_store_can_read(tmp_db_path: Path) -> None:
    t0 = 1_700_000_000.0
    with Store(tmp_db_path, readonly=False) as s:
        s.init_schema()
        s.record_poll(_reading(t0, recv=5, sent=6), _baseline(t0), UTC)

    with Store(tmp_db_path, readonly=True) as ro:
        assert ro.last_reading() == _reading(t0, recv=5, sent=6)
        assert ro.get_meta("schema_version") == "1"


# ---------------------------------------------------------------------------
# concurrent reader-while-writer (WAL: readers never blocked, never torn)
# ---------------------------------------------------------------------------


def test_concurrent_reader_while_writer_never_sees_torn_or_blocked(
    tmp_db_path: Path,
) -> None:
    with Store(tmp_db_path, readonly=False) as s:
        s.init_schema()

    n_writes = 40
    base_ts = _local_midnight_utc("2025-06-15", UTC) + 3600
    errors: list[BaseException] = []
    reader_saw_partial_row = False

    def writer() -> None:
        with Store(tmp_db_path, readonly=False) as w:
            prev_ts = base_ts
            w.record_poll(_reading(prev_ts), _baseline(prev_ts), UTC)
            for i in range(n_writes):
                ts = prev_ts + 10
                w.record_poll(
                    _reading(ts, recv=(i + 1) * 1000, sent=(i + 1) * 100),
                    _ok(prev_ts, ts, 1000, 100),
                    UTC,
                )
                prev_ts = ts

    def reader() -> None:
        nonlocal reader_saw_partial_row
        try:
            deadline = time.time() + 10
            while time.time() < deadline:
                with Store(tmp_db_path, readonly=True) as r:
                    # ONE statement -> one consistent snapshot. (Two separate
                    # autocommit SELECTs would each be free to see a
                    # different snapshot if the writer commits between them
                    # -- that would be a test artifact, not evidence of a
                    # torn write, since record_poll's insert of the sample
                    # and its interval is itself one transaction.)
                    n_samples, n_intervals = r._conn.execute(
                        "SELECT (SELECT COUNT(*) FROM samples), "
                        "(SELECT COUNT(*) FROM intervals)"
                    ).fetchone()
                    # A committed sample always has its interval too — WAL
                    # guarantees no reader ever sees one without the other.
                    if n_samples != n_intervals:
                        reader_saw_partial_row = True
                if n_samples >= n_writes + 1:
                    break
        except BaseException as exc:  # pragma: no cover - surfaced via errors list
            errors.append(exc)

    t_writer = threading.Thread(target=writer)
    t_reader = threading.Thread(target=reader)
    t_reader.start()
    t_writer.start()
    t_writer.join(timeout=15)
    t_reader.join(timeout=15)

    assert not errors
    assert not reader_saw_partial_row
    with Store(tmp_db_path, readonly=True) as final:
        assert final._conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == n_writes + 1


# ---------------------------------------------------------------------------
# cycle_summary.unknown_s — the number that tells the user how much of the
# cycle was never measured, AND the denominator correction in the projection.
# It had zero coverage: replacing `unknown_s += du.gap_s + du.uncovered_s`
# with `unknown_s += 0.0` left the whole suite green, i.e. the cycle view
# could silently claim perfect coverage of a period nobody observed.
# ---------------------------------------------------------------------------


def test_cycle_summary_unknown_s_counts_gap_and_uncovered_time(
    tmp_db_path: Path,
) -> None:
    cfg = Config(billing_cycle_start_day=1, timezone="UTC", db_path=str(tmp_db_path))
    with Store(tmp_db_path, readonly=False) as s:
        s.init_schema()
        start = _local_midnight_utc("2025-03-01", UTC)
        t1 = start + 3600          # 1h of measured traffic
        t2 = t1 + 4 * 3600         # 4h explicit gap
        s.record_poll(_reading(start), _baseline(start), UTC)
        s.record_poll(
            _reading(t1, recv=500_000_000, sent=0), _ok(start, t1, 500_000_000, 0), UTC
        )
        s.record_poll(_reading(t2), _gap(t1, t2), UTC)
        now_ts = t2 + 1800  # plus 30 min the collector has not covered at all
        summary = s.cycle_summary(cfg, now_ts)
        today = s.day_usage("2025-03-01", UTC, now_ts)

    assert summary.used_bytes == 500_000_000
    # 4h gap + 30 min uncovered, give or take the float split arithmetic.
    assert summary.unknown_s == pytest.approx(4 * 3600 + 1800, abs=2.0)
    assert summary.unknown_s == pytest.approx(today.gap_s + today.uncovered_s, abs=2.0)
    assert summary.unknown_s > 0.0


def test_cycle_projection_imputes_unknown_time_at_the_known_rate(
    tmp_db_path: Path,
) -> None:
    """known_rate = used / (elapsed - unknown). Zeroing unknown_s understates
    the rate, so the projection quietly understates the month too."""
    cfg = Config(billing_cycle_start_day=1, timezone="UTC", db_path=str(tmp_db_path))
    with Store(tmp_db_path, readonly=False) as s:
        s.init_schema()
        start = _local_midnight_utc("2025-03-01", UTC)
        t1 = start + 3600
        t2 = t1 + 4 * 3600
        s.record_poll(_reading(start), _baseline(start), UTC)
        s.record_poll(
            _reading(t1, recv=500_000_000, sent=0), _ok(start, t1, 500_000_000, 0), UTC
        )
        s.record_poll(_reading(t2), _gap(t1, t2), UTC)
        now_ts = t2
        summary = s.cycle_summary(cfg, now_ts)

    elapsed_s = now_ts - start
    known_s = max(elapsed_s - summary.unknown_s, 1.0)
    known_rate = summary.used_bytes / known_s
    cycle_total_s = summary.days_total * 86400.0
    expected = summary.used_bytes + known_rate * (cycle_total_s - known_s)
    assert summary.projected_bytes == pytest.approx(expected, rel=1e-6)
    # The whole point: imputing the unknown hours makes the projection LARGER
    # than a naive used-so-far extrapolation over covered time alone.
    assert summary.projected_bytes > summary.used_bytes


# ---------------------------------------------------------------------------
# record_poll must not blow up the whole transaction on a duplicate timestamp
# ---------------------------------------------------------------------------


def test_record_poll_survives_a_duplicate_sample_timestamp(tmp_db_path: Path) -> None:
    """samples.ts is the PRIMARY KEY. An NTP step backward on wake (risk #8)
    or two reads sharing a float timestamp used to raise IntegrityError,
    which rolled back the sample, the interval, the day rollup AND the meta
    baseline advance -- and Collector.poll_once, catching only SourceError,
    then mis-reported it as a router outage."""
    with Store(tmp_db_path, readonly=False) as s:
        s.init_schema()
        anomaly = IntervalResult(
            kind="anomaly", start_ts=1000.0, end_ts=1000.0,
            down_bytes=None, up_bytes=None, down_wraps=None, up_wraps=None,
            note="non-positive gap (0.000s)",
        )
        s.record_poll(_reading(1000.0, recv=10, sent=5), _baseline(1000.0), UTC)
        s.record_poll(_reading(1000.0, recv=99, sent=42), anomaly, UTC)  # must not raise

        rows = s._conn.execute(
            "SELECT ts, bytes_recv, bytes_sent FROM samples ORDER BY ts"
        ).fetchall()
        assert len(rows) == 1
        # The re-read is a better snapshot of that instant, so it wins.
        assert (rows[0]["bytes_recv"], rows[0]["bytes_sent"]) == (99, 42)
        # And the baseline still advanced, which is what stops the gap growing.
        last = s.last_reading()
        assert last is not None and last.bytes_recv == 99


def test_duplicate_timestamp_still_records_both_intervals(tmp_db_path: Path) -> None:
    """Only the sample row collapses; the interval history does not."""
    with Store(tmp_db_path, readonly=False) as s:
        s.init_schema()
        anomaly = IntervalResult(
            kind="anomaly", start_ts=1000.0, end_ts=1000.0,
            down_bytes=None, up_bytes=None, down_wraps=None, up_wraps=None,
            note="non-positive gap (0.000s)",
        )
        s.record_poll(_reading(1000.0), _baseline(1000.0), UTC)
        s.record_poll(_reading(1000.0), anomaly, UTC)
        kinds = [
            r["kind"]
            for r in s._conn.execute(
                "SELECT kind FROM intervals ORDER BY id"
            ).fetchall()
        ]
    assert kinds == ["baseline", "anomaly"]
