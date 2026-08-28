"""Tests for housewatch.menubar.

Scope: every pure formatting/decision function (magnitudes, thresholds, cap
math), the Store-backed build_snapshot() against a real (tmp) db including
its failure paths, the launchctl/dashboard/log side-effecting helpers with
subprocess mocked out (no real launchctl calls, no real server spawned), the
plist template renders correctly, and — the task's explicit ask — that
HouseWatchMenubarApp actually constructs under the real venv interpreter
without entering the rumps event loop. No test here requires a display or
touches the real LaunchAgent/launchctl state.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from housewatch import menubar
from housewatch.config import Config
from housewatch.model import IntervalResult, Reading
from housewatch.store import Store

# ---------------------------------------------------------------------------
# format_bytes_compact
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (0, "0 B"),
        (1, "1 B"),
        (999, "999 B"),
        (1000, "1.0 KB"),
        (1500, "1.5 KB"),
        (999_000, "999.0 KB"),
        (1_000_000, "1.0 MB"),
        (2_500_000, "2.5 MB"),
        (1_000_000_000, "1.0 GB"),
        (3_200_000_000, "3.2 GB"),
        (1_000_000_000_000, "1.0 TB"),
        (1_500_000_000_000, "1.5 TB"),
        (None, "?"),
    ],
)
def test_format_bytes_compact_magnitudes(n: int | None, expected: str) -> None:
    assert menubar.format_bytes_compact(n) == expected


def test_format_bytes_compact_negative_keeps_sign() -> None:
    assert menubar.format_bytes_compact(-5_000) == "-5.0 KB"


def test_format_bytes_compact_never_reads_unknown_as_zero() -> None:
    # The house-wide invariant: an unrecoverable delta is None, never 0.
    assert menubar.format_bytes_compact(None) != "0 B"


# ---------------------------------------------------------------------------
# is_stale
# ---------------------------------------------------------------------------


def test_is_stale_none_age_is_always_stale() -> None:
    assert menubar.is_stale(None, 10.0) is True


def test_is_stale_threshold_boundary() -> None:
    assert menubar.is_stale(50.0, 10.0) is False  # exactly 5x: not yet stale
    assert menubar.is_stale(50.1, 10.0) is True


def test_is_stale_well_within_interval() -> None:
    assert menubar.is_stale(12.0, 10.0) is False


# ---------------------------------------------------------------------------
# cap_progress_pct / days_left
# ---------------------------------------------------------------------------


def test_cap_progress_pct_unset_cap_is_none() -> None:
    assert menubar.cap_progress_pct(500, None) is None


def test_cap_progress_pct_zero_cap_is_none() -> None:
    assert menubar.cap_progress_pct(500, 0) is None


def test_cap_progress_pct_normal() -> None:
    assert menubar.cap_progress_pct(420_000_000_000, 1_000_000_000_000) == pytest.approx(42.0)


def test_cap_progress_pct_over_cap_not_clamped() -> None:
    assert menubar.cap_progress_pct(1_500, 1_000) == pytest.approx(150.0)


def test_days_left_normal() -> None:
    assert menubar.days_left(9.0, 30.0) == pytest.approx(21.0)


def test_days_left_floors_at_zero() -> None:
    assert menubar.days_left(35.0, 30.0) == 0.0


# ---------------------------------------------------------------------------
# format_title
# ---------------------------------------------------------------------------


def _cycle(
    used_bytes: int = 0,
    cap_bytes: int | None = None,
    days_elapsed: float = 0.0,
    days_total: float = 30.0,
    unknown_s: float = 0.0,
    projected_bytes: int = 0,
    over_cap_projected: bool | None = None,
):
    from housewatch.store import CycleSummary

    return CycleSummary(
        cycle_start="2026-08-01",
        cycle_end="2026-08-30",
        used_bytes=used_bytes,
        cap_bytes=cap_bytes,
        days_elapsed=days_elapsed,
        days_total=days_total,
        unknown_s=unknown_s,
        projected_bytes=projected_bytes,
        over_cap_projected=over_cap_projected,
    )


def _snap(**kwargs) -> menubar.Snapshot:
    defaults = dict(ok=True, error=None, poll_interval_s=10.0)
    defaults.update(kwargs)
    return menubar.Snapshot(**defaults)


def test_format_title_failed_query_is_a_dash() -> None:
    snap = menubar.Snapshot(ok=False, error="db missing", poll_interval_s=10.0)
    assert menubar.format_title(snap) == "–"


def test_format_title_no_cap_shows_today_total() -> None:
    today = menubar.DaySlice(3_000_000_000, 200_000_000, 0, 0, 0.0)
    snap = _snap(today=today, last_sample_age_s=5.0)
    assert menubar.format_title(snap) == "3.2 GB"


def test_format_title_cap_configured_shows_cycle_progress() -> None:
    cycle = _cycle(used_bytes=420, cap_bytes=1000, days_elapsed=9.0, days_total=30.0)
    snap = _snap(cycle=cycle, last_sample_age_s=5.0)
    assert menubar.format_title(snap) == "42% - 21d"


def test_format_title_stale_appends_warning() -> None:
    today = menubar.DaySlice(1_000, 0, 0, 0, 0.0)
    snap = _snap(today=today, last_sample_age_s=999.0)  # >> 5*10
    assert menubar.format_title(snap).endswith(" (!)")


def test_format_title_unknown_time_appends_warning_even_when_fresh() -> None:
    today = menubar.DaySlice(1_000, 0, 0, 0, 120.0)  # > UNKNOWN_WARN_S
    snap = _snap(today=today, last_sample_age_s=5.0)
    assert menubar.format_title(snap).endswith(" (!)")


def test_format_title_healthy_no_warning() -> None:
    today = menubar.DaySlice(1_000, 0, 0, 0, 5.0)
    snap = _snap(today=today, last_sample_age_s=5.0)
    assert "(!)" not in menubar.format_title(snap)


# ---------------------------------------------------------------------------
# format_day_line / format_cycle_line / format_projected_line /
# format_last_sample_line / format_data_quality_line
# ---------------------------------------------------------------------------


def test_format_day_line_plain() -> None:
    day = menubar.DaySlice(1_000_000_000, 500_000_000, 0, 0, 0.0)
    assert menubar.format_day_line("Today", day) == "Today: 1.0 GB down / 500.0 MB up"


def test_format_day_line_flags_suspect() -> None:
    day = menubar.DaySlice(1_000, 500, 200, 0, 0.0)
    assert "suspect" in menubar.format_day_line("Today", day)


def test_format_cycle_line_no_cap() -> None:
    cycle = _cycle(used_bytes=1_000_000_000)
    line = menubar.format_cycle_line(cycle)
    assert "no cap set" in line
    assert "1.0 GB" in line


def test_format_cycle_line_prefixes_when_unknown_time_is_significant() -> None:
    cycle = _cycle(used_bytes=1_000, cap_bytes=2_000, unknown_s=120.0)
    assert menubar.format_cycle_line(cycle).startswith("Cycle: >= ")


def test_format_cycle_line_no_prefix_when_unknown_time_is_small() -> None:
    cycle = _cycle(used_bytes=1_000, cap_bytes=2_000, unknown_s=5.0)
    assert "Cycle: >= " not in menubar.format_cycle_line(cycle)


def test_format_projected_line_over_cap_glyph() -> None:
    cycle = _cycle(projected_bytes=2_000, cap_bytes=1_000, over_cap_projected=True)
    assert "over cap" in menubar.format_projected_line(cycle)


def test_format_projected_line_under_cap_no_glyph() -> None:
    cycle = _cycle(projected_bytes=500, cap_bytes=1_000, over_cap_projected=False)
    assert "over cap" not in menubar.format_projected_line(cycle)


def test_format_last_sample_line_never_seen() -> None:
    line = menubar.format_last_sample_line(None, stale=True)
    assert "never" in line


def test_format_last_sample_line_stale_warns() -> None:
    line = menubar.format_last_sample_line(999.0, stale=True)
    assert "⚠" in line  # warning glyph


def test_format_last_sample_line_fresh_no_warning() -> None:
    line = menubar.format_last_sample_line(5.0, stale=False)
    assert "⚠" not in line


def test_format_data_quality_line_counts() -> None:
    line = menubar.format_data_quality_line(125.0, 3)
    assert "3 gap/suspect" in line


def test_dashboard_url() -> None:
    cfg = Config(web_host="127.0.0.1", web_port=8321)
    assert menubar.dashboard_url(cfg) == "http://127.0.0.1:8321/"


# ---------------------------------------------------------------------------
# build_snapshot — against a real (tmp) Store
# ---------------------------------------------------------------------------


def _reading(ts: float, recv: int = 0, sent: int = 0, uptime: int = 1000) -> Reading:
    return Reading(ts=ts, bytes_recv=recv, bytes_sent=sent, uptime_s=uptime, pkts_recv=None, pkts_sent=None)


def _ok_interval(start_ts: float, end_ts: float, down: int, up: int) -> IntervalResult:
    return IntervalResult(
        kind="ok", start_ts=start_ts, end_ts=end_ts,
        down_bytes=down, up_bytes=up, down_wraps=0, up_wraps=0, note="",
    )


def _baseline(ts: float) -> IntervalResult:
    return IntervalResult(
        kind="baseline", start_ts=ts, end_ts=ts,
        down_bytes=None, up_bytes=None, down_wraps=None, up_wraps=None, note="first sample",
    )


def test_build_snapshot_missing_db_degrades_gracefully(tmp_db_path: Path) -> None:
    cfg = Config(db_path=str(tmp_db_path))
    snap = menubar.build_snapshot(cfg, time.time())
    assert snap.ok is False
    assert snap.error is not None
    assert menubar.format_title(snap) == "–"


def test_build_snapshot_reports_real_data(tmp_db_path: Path) -> None:
    cfg = Config(db_path=str(tmp_db_path), timezone="UTC")
    writer = Store(tmp_db_path, readonly=False)
    writer.init_schema()
    now = time.time()
    t0 = now - 3600
    writer.record_poll(_reading(t0), _baseline(t0), menubar.config_mod.tzinfo_of(cfg))
    writer.record_poll(
        _reading(now, recv=2_000_000_000, sent=1_000_000_000),
        _ok_interval(t0, now, down=2_000_000_000, up=1_000_000_000),
        menubar.config_mod.tzinfo_of(cfg),
    )
    writer.close()

    snap = menubar.build_snapshot(cfg, now)
    assert snap.ok is True
    assert snap.today.down_bytes == 2_000_000_000
    assert snap.today.up_bytes == 1_000_000_000
    assert snap.last_sample_age_s == pytest.approx(0.0, abs=1.0)
    assert snap.cycle is not None
    assert snap.cycle.used_bytes >= 3_000_000_000


def test_build_snapshot_no_cap_set_path(tmp_db_path: Path) -> None:
    cfg = Config(db_path=str(tmp_db_path), monthly_cap_gb=None)
    writer = Store(tmp_db_path, readonly=False)
    writer.init_schema()
    writer.close()

    snap = menubar.build_snapshot(cfg, time.time())
    assert snap.ok is True
    assert snap.cycle is not None
    assert snap.cycle.cap_bytes is None
    assert "no cap" in menubar.format_cycle_line(snap.cycle)
    # No cap configured -> title falls back to a byte count, not '0%'.
    title = menubar.format_title(snap)
    assert "%" not in title


# ---------------------------------------------------------------------------
# launchctl helpers — subprocess mocked, never touch real launchd state
# ---------------------------------------------------------------------------


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["launchctl"], returncode=returncode, stdout=stdout, stderr=stderr)


def test_collector_is_loaded_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(menubar, "_launchctl", lambda *a: _completed(0))
    assert menubar.collector_is_loaded() is True


def test_collector_is_loaded_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(menubar, "_launchctl", lambda *a: _completed(3))
    assert menubar.collector_is_loaded() is False


def test_collector_is_loaded_survives_launchctl_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object) -> None:
        raise OSError("no launchctl")

    monkeypatch.setattr(menubar, "_launchctl", boom)
    assert menubar.collector_is_loaded() is False


def test_start_collector_no_plist_fails_without_calling_launchctl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)  # no ~/Library/LaunchAgents/... here
    calls = []
    monkeypatch.setattr(menubar, "_launchctl", lambda *a: calls.append(a) or _completed(0))
    ok, detail = menubar.start_collector()
    assert ok is False
    assert "no LaunchAgent installed" in detail
    assert calls == []


def test_stop_collector_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(menubar, "_launchctl", lambda *a: _completed(0))
    ok, detail = menubar.stop_collector()
    assert ok is True


def test_stop_collector_failure_reports_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(menubar, "_launchctl", lambda *a: _completed(1, stderr="No such process"))
    ok, detail = menubar.stop_collector()
    assert ok is False
    assert "No such process" in detail


# ---------------------------------------------------------------------------
# ensure_dashboard_running
# ---------------------------------------------------------------------------


def test_ensure_dashboard_running_already_up(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    host, port = srv.getsockname()
    try:
        cfg = Config(web_host=host, web_port=port)
        popen_calls = []
        monkeypatch.setattr(
            menubar.subprocess, "Popen", lambda *a, **k: popen_calls.append((a, k))
        )
        ok, detail = menubar.ensure_dashboard_running(cfg)
        assert ok is True
        assert detail == "already running"
        assert popen_calls == []  # nothing spawned — it was already listening
    finally:
        srv.close()


def test_ensure_dashboard_running_spawns_and_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    # Free port nobody is listening on.
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    _, free_port = probe.getsockname()
    probe.close()

    cfg = Config(web_host="127.0.0.1", web_port=free_port)

    class _FakeProc:
        pass

    spawned = []
    monkeypatch.setattr(
        menubar.subprocess, "Popen", lambda *a, **k: spawned.append((a, k)) or _FakeProc()
    )
    monkeypatch.setattr(menubar, "_DASHBOARD_START_TIMEOUT_S", 0.3)
    monkeypatch.setattr(menubar, "_DASHBOARD_START_POLL_S", 0.05)

    ok, detail = menubar.ensure_dashboard_running(cfg)
    assert ok is False
    assert "did not come up" in detail
    assert len(spawned) == 1
    assert spawned[0][0][0] == [menubar.sys.executable, "-m", "housewatch", "serve"]


# ---------------------------------------------------------------------------
# open_log
# ---------------------------------------------------------------------------


def test_open_log_missing_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(menubar.config_mod, "LOG_DIR", tmp_path / "nope")
    ok, detail = menubar.open_log()
    assert ok is False
    assert "no log file" in detail


def test_open_log_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "housewatch.log").write_text("hello\n")
    monkeypatch.setattr(menubar.config_mod, "LOG_DIR", log_dir)

    calls = []
    monkeypatch.setattr(
        menubar.subprocess, "run", lambda *a, **k: calls.append((a, k))
    )
    ok, detail = menubar.open_log()
    assert ok is True
    assert calls


# ---------------------------------------------------------------------------
# LaunchAgent plist template
# ---------------------------------------------------------------------------


def test_render_launchagent_plist_substitutes_all_placeholders() -> None:
    rendered = menubar.render_launchagent_plist(
        python_path="/path/to/venv/bin/python3",
        workdir="/path/to/housewatch",
        home="/path/to/home",
    )
    assert "{PYTHON}" not in rendered
    assert "{WORKDIR}" not in rendered
    assert "{HOME}" not in rendered
    assert "/path/to/venv/bin/python3" in rendered
    assert "housewatch.menubar" in rendered
    assert "com.housewatch.menubar" in rendered
    assert "<key>SuccessfulExit</key>" in rendered


def test_menubar_launchagent_label_differs_from_collector_label() -> None:
    """Two agents, two labels - the menubar must never boot out the
    collector (or vice versa) because they share a service name."""
    from housewatch.cli import LAUNCHD_LABEL

    assert menubar.MENUBAR_LAUNCHD_LABEL != LAUNCHD_LABEL
    assert menubar.MENUBAR_LAUNCHD_LABEL.startswith("com.housewatch.")
    assert LAUNCHD_LABEL.startswith("com.housewatch.")


def test_menubar_label_matches_its_own_plist() -> None:
    """The rendered plist's Label must be the label menubar.py addresses."""
    import plistlib

    rendered = menubar.render_launchagent_plist(
        python_path="/path/to/venv/bin/python3",
        workdir="/tmp/housewatch",
        home="/path/to/home",
    )
    parsed = plistlib.loads(rendered.encode("utf-8"))
    assert parsed["Label"] == menubar.MENUBAR_LAUNCHD_LABEL


# ---------------------------------------------------------------------------
# Import + construct under the real venv interpreter, without the event loop
# ---------------------------------------------------------------------------


def test_app_constructs_without_entering_event_loop(tmp_db_path: Path) -> None:
    """The task's explicit requirement: HouseWatchMenubarApp must be
    constructible (import + __init__) with no display/event loop entered.
    Construction only builds menu widgets and kicks off one background
    refresh thread — it never calls .run(), which is the only rumps entry
    point that would actually start the Cocoa run loop."""
    if menubar.rumps is None:
        pytest.skip("rumps not installed in this interpreter")

    cfg = Config(db_path=str(tmp_db_path))  # db intentionally absent
    app = menubar.HouseWatchMenubarApp(cfg)
    assert isinstance(app, menubar.rumps.App)
    # Give the background refresh thread a moment; must not raise, must not
    # hang (it hits the degrade-gracefully path since the db doesn't exist).
    time.sleep(0.3)
    assert app.title == "–"
    app.timer.stop()


# ---------------------------------------------------------------------------
# UI-honesty rules on the DAY lines and the title.
#
# The gapPolicy makes these binding on both UIs. app.js honoured them;
# menubar.py did not: a day with 18 hours of no data rendered
# 'Today: 0 B down / 0 B up', and a partially-covered day rendered a bare
# number with no '>=' and no mention that most of it was unattributed.
# format_cycle_line already applied the '>= ' prefix, so the rule was
# understood in the same file and simply skipped on the day lines.
# ---------------------------------------------------------------------------


def test_day_with_no_data_says_so_instead_of_zero_bytes() -> None:
    day = menubar.DaySlice(
        down_bytes=0, up_bytes=0, suspect_down=0, suspect_up=0, unknown_s=64_800.0
    )
    line = menubar.format_day_line("Today", day)
    assert "no data" in line
    assert "0 B" not in line
    assert "18h" in line


def test_partially_covered_day_gets_the_ge_prefix_and_the_unknown_duration() -> None:
    day = menubar.DaySlice(
        down_bytes=1_000_000_000,
        up_bytes=100_000_000,
        suspect_down=0,
        suspect_up=0,
        unknown_s=64_790.0,
    )
    line = menubar.format_day_line("Yesterday", day)
    assert ">= 1.0 GB down" in line
    assert ">= 100.0 MB up" in line
    assert "unknown" in line


def test_fully_covered_day_keeps_a_plain_number() -> None:
    """The prefix must mean something: a day with no unknown time gets no
    '>=', so the marker stays a real signal rather than decoration."""
    day = menubar.DaySlice(
        down_bytes=1_000_000_000, up_bytes=100_000_000,
        suspect_down=0, suspect_up=0, unknown_s=0.0,
    )
    line = menubar.format_day_line("Today", day)
    assert ">=" not in line
    assert "unknown" not in line
    assert "1.0 GB down" in line


def test_a_second_of_unknown_time_is_below_the_warn_threshold() -> None:
    day = menubar.DaySlice(
        down_bytes=5_000_000, up_bytes=1_000_000,
        suspect_down=0, suspect_up=0, unknown_s=12.0,
    )
    assert ">=" not in menubar.format_day_line("Today", day)


def test_title_never_shows_a_bare_zero_for_a_day_with_no_data() -> None:
    snap = menubar.Snapshot(
        ok=True,
        error=None,
        today=menubar.DaySlice(0, 0, 0, 0, 64_800.0),
        cycle=None,
        last_sample_age_s=5.0,
        poll_interval_s=10.0,
    )
    title = menubar.format_title(snap)
    assert title == "no data (!)"


def test_title_prefixes_a_partially_covered_day() -> None:
    snap = menubar.Snapshot(
        ok=True,
        error=None,
        today=menubar.DaySlice(1_000_000_000, 100_000_000, 0, 0, 3_600.0),
        cycle=None,
        last_sample_age_s=5.0,
        poll_interval_s=10.0,
    )
    title = menubar.format_title(snap)
    assert title.startswith("≥")
    assert title.endswith("(!)")


def test_title_is_a_plain_number_when_the_day_is_fully_covered() -> None:
    snap = menubar.Snapshot(
        ok=True,
        error=None,
        today=menubar.DaySlice(1_000_000_000, 100_000_000, 0, 0, 0.0),
        cycle=None,
        last_sample_age_s=5.0,
        poll_interval_s=10.0,
    )
    assert menubar.format_title(snap) == "1.1 GB"
