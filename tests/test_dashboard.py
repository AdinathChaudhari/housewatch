"""Tests for housewatch.webapp (the local read-only dashboard).

Named test_dashboard.py per this task's file-ownership brief even though
the module under test is housewatch.webapp — see webapp.py's module
docstring for why the module itself is named webapp.py, not dashboard.py
(cli.py already hard-imports `from housewatch import webapp`).

Uses Flask's test client against a seeded temp SQLite db (writer Store,
closed before the app ever opens its own readonly Store — matching the
single-writer discipline the rest of the project already enforces) and
against a completely fresh/absent db, since the dashboard has to survive a
household that has never run the collector at all.
"""

from __future__ import annotations

import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from housewatch.config import Config
from housewatch.model import IntervalResult, Reading
from housewatch.store import Store
from housewatch.webapp import create_app

UTC = ZoneInfo("UTC")

ALL_ROUTES = [
    "/",
    "/healthz",
    "/api/summary",
    "/api/days",
    "/api/days?n=7",
    "/api/rate",
    "/api/rate?minutes=60",
    "/api/unknown",
    "/api/events",
]


# ---------------------------------------------------------------------------
# helpers — same shapes test_store.py already uses, kept local so this file
# has no cross-test-module import coupling
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
        down_bytes=down, up_bytes=up, down_wraps=0, up_wraps=0, note="clean",
    )


def _suspect(start_ts: float, end_ts: float, down: int, up: int) -> IntervalResult:
    return IntervalResult(
        kind="suspect", start_ts=start_ts, end_ts=end_ts,
        down_bytes=down, up_bytes=up, down_wraps=0, up_wraps=0,
        note="possible undercount up to 4.295 GB per direction",
    )


def _gap(start_ts: float, end_ts: float) -> IntervalResult:
    return IntervalResult(
        kind="gap", start_ts=start_ts, end_ts=end_ts,
        down_bytes=None, up_bytes=None, down_wraps=None, up_wraps=None,
        note="delta unrecoverable: multiple wraps possible",
    )


@pytest.fixture
def empty_cfg(tmp_path: Path) -> Config:
    """A valid Config whose db_path does not exist on disk yet."""
    return Config(db_path=str(tmp_path / "empty" / "housewatch.db"), timezone="UTC")


@pytest.fixture
def initialized_empty_cfg(tmp_path: Path) -> Config:
    """A Config whose db exists (schema initialized) but has zero samples —
    the other empty state: 'the collector ran init but never polled'."""
    cfg = Config(db_path=str(tmp_path / "init-empty" / "housewatch.db"), timezone="UTC")
    with Store(Path(cfg.db_path), readonly=False) as s:
        s.init_schema()
    return cfg


@pytest.fixture
def seeded_cfg(tmp_path: Path) -> Config:
    """A Config with a db seeded with: a baseline, a clean 'ok' interval, a
    'suspect' interval (nonzero bytes, flagged), and a 'gap' interval (NULL
    bytes) — all inside 'today' (UTC) — plus a monthly cap, so both the
    honesty rules and the cap-math plumbing have something to render."""
    cfg = Config(
        db_path=str(tmp_path / "seeded" / "housewatch.db"),
        timezone="UTC",
        monthly_cap_gb=100.0,
        billing_cycle_start_day=1,
    )
    # Anchored to end at (just before) "now" and spanning only ~61 minutes
    # total, rather than starting at "now" and running forward — the
    # dashboard's "today" queries use the real wall clock at request time,
    # so every seeded interval must end at or before it. Kept under ~1h10m
    # (not several hours) to keep the small residual risk of straddling a
    # UTC midnight during a slow test run negligible.
    now = time.time()
    t3 = now - 5
    gap_dur = 3600  # 1h gap: long enough to be unrecoverable ('gap', not 'suspect')
    t2 = t3 - gap_dur
    t1 = t2 - 40  # 'suspect'-length window
    t0 = t1 - 10  # 'ok' window

    with Store(Path(cfg.db_path), readonly=False) as s:
        s.init_schema()
        s.record_poll(_reading(t0, recv=1_000, sent=200, uptime=5000), _baseline(t0), UTC)
        s.record_poll(
            _reading(t1, recv=2_000, sent=400, uptime=5010),
            _ok(t0, t1, down=123_456, up=45_678),
            UTC,
        )
        s.record_poll(
            _reading(t2, recv=3_000, sent=600, uptime=5050),
            _suspect(t1, t2, down=5_000_000_000, up=1_000_000_000),
            UTC,
        )
        s.record_poll(
            _reading(t3, recv=4_000, sent=800, uptime=5050 + 7200 + 5),
            _gap(t2, t3),
            UTC,
        )
        s.record_event(t3, "collector_start", "test seed")

    return cfg


def _client(cfg: Config):
    app = create_app(cfg)
    app.testing = True
    return app.test_client()


# ---------------------------------------------------------------------------
# empty states must never 500
# ---------------------------------------------------------------------------


def test_no_db_file_all_routes_200(empty_cfg: Config) -> None:
    client = _client(empty_cfg)
    for route in ALL_ROUTES:
        resp = client.get(route)
        assert resp.status_code == 200, f"{route} -> {resp.status_code}: {resp.data[:300]!r}"


def test_no_db_file_summary_reports_no_data(empty_cfg: Config) -> None:
    client = _client(empty_cfg)
    body = client.get("/api/summary").get_json()
    assert body["has_data"] is False
    assert body["today"] is None
    assert body["cycle"] is None
    assert "run" in body["message"]  # points at what to do next


def test_no_db_file_healthz_not_ok(empty_cfg: Config) -> None:
    client = _client(empty_cfg)
    body = client.get("/healthz").get_json()
    assert body["ok"] is False
    assert body["last_sample_age_s"] is None


def test_no_db_file_days_and_rate_are_empty_lists_not_errors(empty_cfg: Config) -> None:
    client = _client(empty_cfg)
    assert client.get("/api/days").get_json() == []
    assert client.get("/api/rate").get_json() == []
    assert client.get("/api/unknown").get_json() == []
    assert client.get("/api/events").get_json() == []


def test_index_renders_with_no_db(empty_cfg: Config) -> None:
    client = _client(empty_cfg)
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"housewatch" in resp.data


def test_schema_initialized_but_zero_samples_all_routes_200(initialized_empty_cfg: Config) -> None:
    client = _client(initialized_empty_cfg)
    for route in ALL_ROUTES:
        resp = client.get(route)
        assert resp.status_code == 200, f"{route} -> {resp.status_code}"
    summary = client.get("/api/summary").get_json()
    assert summary["has_data"] is False
    healthz = client.get("/healthz").get_json()
    assert healthz["ok"] is False
    assert healthz["reason"] == "no sample recorded yet"


# ---------------------------------------------------------------------------
# seeded db: routes return 200, and the actual numbers/labels are right
# ---------------------------------------------------------------------------


def test_seeded_all_routes_200(seeded_cfg: Config) -> None:
    client = _client(seeded_cfg)
    for route in ALL_ROUTES:
        resp = client.get(route)
        assert resp.status_code == 200, f"{route} -> {resp.status_code}: {resp.data[:300]!r}"


def test_seeded_healthz_ok(seeded_cfg: Config) -> None:
    client = _client(seeded_cfg)
    body = client.get("/healthz").get_json()
    assert body["ok"] is True
    assert body["last_sample_age_s"] is not None
    assert body["last_sample_age_s"] < 5 * seeded_cfg.poll_interval_s


def test_seeded_summary_today_totals(seeded_cfg: Config) -> None:
    client = _client(seeded_cfg)
    body = client.get("/api/summary").get_json()
    assert body["has_data"] is True
    today = body["today"]
    # ok(123_456/45_678) + suspect(5e9/1e9); the gap interval contributes 0
    # bytes (never a guessed number) even though it spans real elapsed time.
    assert today["down_bytes"] == 123_456 + 5_000_000_000
    assert today["up_bytes"] == 45_678 + 1_000_000_000
    assert today["suspect_down"] == 5_000_000_000
    assert today["suspect_up"] == 1_000_000_000
    # the 1-hour gap must show up as unknown seconds, not vanish
    assert today["gap_s"] >= 3500


def test_seeded_cap_math(seeded_cfg: Config) -> None:
    client = _client(seeded_cfg)
    body = client.get("/api/summary").get_json()
    cap = body["cap"]
    assert cap["monthly_cap_gb"] == 100.0
    assert cap["cap_bytes"] == 100_000_000_000  # decimal GB, matches store.py's convention
    cycle = body["cycle"]
    assert cycle["cap_bytes"] == cap["cap_bytes"]
    assert cycle["used_bytes"] >= 123_456 + 45_678 + 5_000_000_000 + 1_000_000_000
    # cap is set, so over_cap_projected must be a real bool, never left None
    assert isinstance(cycle["over_cap_projected"], bool)


def test_no_cap_configured_over_cap_is_none(tmp_path: Path) -> None:
    cfg = Config(db_path=str(tmp_path / "nocap" / "housewatch.db"), timezone="UTC")
    now = time.time()
    with Store(Path(cfg.db_path), readonly=False) as s:
        s.init_schema()
        s.record_poll(_reading(now - 20), _baseline(now - 20), UTC)
        s.record_poll(_reading(now, recv=500, sent=100), _ok(now - 20, now, 500, 100), UTC)
    client = _client(cfg)
    body = client.get("/api/summary").get_json()
    assert body["cap"]["cap_bytes"] is None
    assert body["cycle"]["over_cap_projected"] is None


def test_gap_window_renders_as_unknown_never_zero(seeded_cfg: Config) -> None:
    client = _client(seeded_cfg)
    rows = client.get("/api/unknown?days=1").get_json()
    kinds = {r["kind"] for r in rows}
    assert "gap" in kinds
    assert "suspect" in kinds
    gap_row = next(r for r in rows if r["kind"] == "gap")
    assert gap_row["down_bytes"] is None  # never 0 — the honesty invariant
    assert gap_row["up_bytes"] is None
    assert "UNKNOWN" in gap_row["label"]
    suspect_row = next(r for r in rows if r["kind"] == "suspect")
    assert suspect_row["down_bytes"] == 5_000_000_000  # suspect DOES carry a number
    assert suspect_row["up_bytes"] == 1_000_000_000


def test_rate_series_holes_are_null_not_zero(seeded_cfg: Config) -> None:
    client = _client(seeded_cfg)
    points = client.get("/api/rate?minutes=1440").get_json()
    assert points  # the seed data is well within the last 24h
    by_kind = {p["kind"]: p for p in points}
    assert by_kind["gap"]["down_bps"] is None
    assert by_kind["gap"]["up_bps"] is None
    assert by_kind["ok"]["down_bps"] is not None
    assert by_kind["ok"]["down_bps"] > 0


def test_days_query_param_clamped(seeded_cfg: Config) -> None:
    client = _client(seeded_cfg)
    huge = client.get("/api/days?n=999999").get_json()
    assert len(huge) == 365  # clamped, not a 365000-row scan
    zero = client.get("/api/days?n=0").get_json()
    assert len(zero) == 1  # clamped up to the minimum, not an empty/garbage response
    garbage = client.get("/api/days?n=not-a-number").get_json()
    assert len(garbage) == 30  # falls back to the default rather than 500ing


def test_events_route_reflects_seeded_event(seeded_cfg: Config) -> None:
    client = _client(seeded_cfg)
    events = client.get("/api/events").get_json()
    assert any(e["kind"] == "collector_start" for e in events)


def test_readonly_store_never_written_by_dashboard(seeded_cfg: Config) -> None:
    """Hitting every route must not add a single row anywhere — the
    dashboard is read-only against the db by construction (every Store it
    opens is readonly=True), so this is really testing that no route
    accidentally opens a writable Store."""
    client = _client(seeded_cfg)
    with Store(Path(seeded_cfg.db_path), readonly=True) as s:
        before_samples = s._conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        before_events = s._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    for route in ALL_ROUTES:
        client.get(route)

    with Store(Path(seeded_cfg.db_path), readonly=True) as s:
        after_samples = s._conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        after_events = s._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    assert after_samples == before_samples
    assert after_events == before_events


# ---------------------------------------------------------------------------
# The dashboard's JS reads these fields by name. A rename in store.DayUsage
# would silently turn app.js's "is this day entirely unmeasured?" test into
# NaN comparisons and quietly bring back the "a day nobody measured shows a
# number" rendering. Python-side pin for a JS-side dependency.
# ---------------------------------------------------------------------------

APP_JS = Path(__file__).resolve().parents[1] / "housewatch" / "static" / "app.js"


def test_day_payload_carries_every_field_app_js_reads(seeded_cfg: Config) -> None:
    client = create_app(seeded_cfg).test_client()
    days = client.get("/api/days?n=3").get_json()
    assert days
    required = {
        "down_bytes", "up_bytes", "suspect_down", "suspect_up",
        "ok_s", "recovered_s", "suspect_s", "gap_s", "uncovered_s",
    }
    assert required <= set(days[0])


def test_app_js_no_data_test_is_not_gated_on_a_full_calendar_day() -> None:
    """app.js used to require unknownS >= 86400 - 1 before rendering
    'no data', which TODAY can never reach -- so a today with nothing
    measured rendered a number instead. The gate must be "nothing was
    measured", not "a whole calendar day was unmeasured"."""
    text = APP_JS.read_text(encoding="utf-8")
    assert "unknownS >= 86400" not in text
    assert "measuredS" in text
