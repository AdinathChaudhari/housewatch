"""Flask dashboard: read-only view of the housewatch SQLite db, loopback only.

DEVIATION FROM THE TASK BRIEF THAT SPAWNED THIS FILE (noted, not silently
applied — same house convention ``config.py`` already uses for its own
JSON-vs-TOML divergence): the brief said to own ``housewatch/dashboard.py``.
This module is named ``webapp.py`` instead, because ``cli.py`` — already on
disk, not owned by this file, and not to be edited to fit around a name
picked before ``cli.py`` existed — hard-imports ``from housewatch import
webapp`` and calls ``webapp.create_app(cfg)`` inside ``cmd_serve``. The
architect's own module spec for this piece also names it ``webapp.py``.
Shipping a ``dashboard.py`` nobody imports would leave ``python -m
housewatch serve`` broken, which defeats the point of building a dashboard
at all. ``tests/test_dashboard.py`` (the test file name the brief asked for)
is kept as-is and imports this module.

Why loopback-only (binding is the caller's job, not this module's, but the
constraint shapes everything below): this serves whole-house WAN usage —
how much data the household has used, roughly when, and whether the router
just rebooted. That is household-network metadata, not a secret, but there
is no reason for it to be reachable from anywhere but this Mac, so
``create_app`` never binds a socket itself (``cli.cmd_serve`` does that,
with ``host=cfg.web_host`` — see ``config.py``'s comment: 127.0.0.1 is the
only host this project ever validates as safe).

Read-only discipline: every route opens its own ``Store(db_path,
readonly=True)`` and closes it before returning (a ``try/finally``, not a
context manager held across the request) — see ``store.py``'s WAL
docstring for why this never blocks or is blocked by the collector's one
read-write connection. No route ever constructs a writable ``Store``.

Honesty rules this module is responsible for enforcing in the JSON it hands
the frontend (the frontend then renders them, per ``model.py``'s central
invariant: unknown is never encoded as zero):
  - A day/cycle with no data yet reports ``has_data: false`` sections, never
    fabricated zeros dressed up as real numbers.
  - ``RatePoint`` values keep their ``None`` bps for gap/reset/baseline/
    anomaly rows — never coerced to 0, so the chart draws a hole.
  - The unknown-windows table lists gap/suspect/reset/anomaly intervals by
    name, each carrying its own ``kind`` so the UI can't relabel a suspect
    window as a gap or vice versa.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, render_template

from housewatch import billing
from housewatch import config as config_mod
from housewatch.config import Config
from housewatch.store import Store

logger = logging.getLogger("housewatch.webapp")

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

# Query-param clamps (mirrors the architect's spec exactly): a client can't
# make this module scan the whole intervals table by asking for n=999999,
# and can't get an empty/meaningless response by asking for n=0.
_DAYS_MIN, _DAYS_MAX = 1, 365
_RATE_MINUTES_MIN, _RATE_MINUTES_MAX = 5, 1440
_UNKNOWN_DAYS_MIN, _UNKNOWN_DAYS_MAX = 1, 90
_EVENTS_MIN, _EVENTS_MAX = 1, 500

# A sample this old means the collector is either not running, stuck, or
# the Mac has been off-network long enough that "last sample age" itself
# stops being a reassuring number. 5x poll interval matches the menubar's
# own "(!)" threshold in the architect's spec, so the two UIs agree.
_STALE_MULTIPLIER = 5


def _db_path(cfg: Config) -> Path:
    return Path(os.path.expanduser(cfg.db_path))


def _open_store(cfg: Config) -> Store | None:
    """A fresh readonly Store, or None if there is nothing to open yet.

    None covers both "the db file doesn't exist at all" (fresh install,
    collector never ran) and "it exists but Store still refuses it" (the
    same FileNotFoundError store.py raises deliberately rather than
    ``sqlite3``'s generic message) — both are the same UI state: no data
    yet, tell the user what to run.
    """
    path = _db_path(cfg)
    if not path.exists():
        return None
    try:
        return Store(path, readonly=True)
    except FileNotFoundError:
        return None


def _clamp(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _collector_info(store: Store, cfg: Config, now_ts: float) -> dict[str, Any]:
    last = store.last_reading()
    if last is None:
        return {"last_sample_ts": None, "last_sample_age_s": None, "stale": True}
    age = now_ts - last.ts
    return {
        "last_sample_ts": last.ts,
        "last_sample_age_s": age,
        "stale": age > _STALE_MULTIPLIER * cfg.poll_interval_s,
    }


def _interval_dict(iv: Any) -> dict[str, Any]:
    """One notable (gap/suspect/reset/anomaly) interval as JSON, with an
    explicit human label so the frontend never has to reverse-engineer
    'this row has null bytes' into the word the user should see."""
    label = {
        "gap": "UNKNOWN (gap — unrecoverable, multiple wraps possible)",
        "suspect": "possible undercount (bounded, one wrap ambiguous)",
        "reset": "UNKNOWN (router reboot / WAN reconnect)",
        "anomaly": "UNKNOWN (reading contradicted itself)",
    }.get(iv.kind, iv.kind)
    return {
        "kind": iv.kind,
        "label": label,
        "start_ts": iv.start_ts,
        "end_ts": iv.end_ts,
        "duration_s": iv.end_ts - iv.start_ts,
        "down_bytes": iv.down_bytes,  # None stays None — never 0
        "up_bytes": iv.up_bytes,
        "note": iv.note,
    }


def create_app(cfg: Config) -> Flask:
    """Build the Flask app. Does not bind a socket — the caller (``cli.py``'s
    ``cmd_serve``) decides host/port; this module only ever validates that
    ``cfg.web_host`` is loopback-shaped in a log line, since a misconfigured
    ``web_host`` is exactly the kind of mistake that deserves a loud warning
    rather than a silent trust that whoever wrote the config knew better.
    """
    if cfg.web_host not in ("127.0.0.1", "localhost", "::1"):
        logger.warning(
            "cfg.web_host=%r is not a loopback address — this dashboard reads "
            "household network usage and was designed for 127.0.0.1 only; "
            "serving it elsewhere is your call, not this code's",
            cfg.web_host,
        )

    app = Flask(
        __name__,
        static_folder=str(_STATIC_DIR),
        template_folder=str(_TEMPLATE_DIR),
    )

    @app.get("/")
    def index() -> str:
        return render_template(
            "index.html",
            poll_interval_s=cfg.poll_interval_s,
            web_port=cfg.web_port,
            has_cap=cfg.monthly_cap_gb is not None,
        )

    @app.get("/healthz")
    def healthz() -> tuple[Response, int]:
        now = time.time()
        path = _db_path(cfg)
        if not path.exists():
            body = {
                "ok": False,
                "reason": "no database yet",
                "db_path": str(path),
                "last_sample_age_s": None,
            }
            return jsonify(body), 200

        store = _open_store(cfg)
        if store is None:
            return jsonify({"ok": False, "reason": "database unreadable", "last_sample_age_s": None}), 200
        try:
            info = _collector_info(store, cfg, now)
        finally:
            store.close()

        ok = info["last_sample_age_s"] is not None and not info["stale"]
        return jsonify(
            {
                "ok": ok,
                "reason": None if ok else ("no sample recorded yet" if info["last_sample_ts"] is None else "last sample is stale"),
                "last_sample_ts": info["last_sample_ts"],
                "last_sample_age_s": info["last_sample_age_s"],
                "poll_interval_s": cfg.poll_interval_s,
            }
        ), 200

    @app.get("/api/summary")
    def api_summary() -> tuple[Response, int]:
        now = time.time()
        store = _open_store(cfg)
        if store is None:
            return jsonify(
                {
                    "has_data": False,
                    "message": "no database yet — run `python -m housewatch once` "
                    "(or start the daemon with `python -m housewatch run`) to take "
                    "the first reading",
                    "today": None,
                    "yesterday": None,
                    "cycle": None,
                    "collector": {"last_sample_ts": None, "last_sample_age_s": None, "stale": True},
                }
            ), 200

        try:
            tz = config_mod.tzinfo_of(cfg)
            today_key = billing.day_key(now, tz)
            yesterday_key = billing.day_key(now - 86400, tz)
            today = store.day_usage(today_key, tz, now)
            yesterday = store.day_usage(yesterday_key, tz, now)
            cycle = store.cycle_summary(cfg, now)
            collector = _collector_info(store, cfg, now)
        finally:
            store.close()

        return jsonify(
            {
                "has_data": collector["last_sample_ts"] is not None,
                "today": asdict(today),
                "yesterday": asdict(yesterday),
                "cycle": asdict(cycle),
                "collector": collector,
                "cap": {
                    "monthly_cap_gb": cfg.monthly_cap_gb,
                    "cap_bytes": cycle.cap_bytes,
                },
            }
        ), 200

    @app.get("/api/days")
    def api_days() -> tuple[Response, int]:
        from flask import request

        n = _clamp(request.args.get("n"), _DAYS_MIN, _DAYS_MAX, 30)
        store = _open_store(cfg)
        if store is None:
            return jsonify([]), 200
        try:
            tz = config_mod.tzinfo_of(cfg)
            now = time.time()
            days = store.recent_days(n, tz, now)
        finally:
            store.close()
        return jsonify([asdict(d) for d in days]), 200

    @app.get("/api/rate")
    def api_rate() -> tuple[Response, int]:
        from flask import request

        minutes = _clamp(request.args.get("minutes"), _RATE_MINUTES_MIN, _RATE_MINUTES_MAX, 60)
        store = _open_store(cfg)
        if store is None:
            return jsonify([]), 200
        try:
            now = time.time()
            points = store.rate_series(minutes, now)
        finally:
            store.close()
        return jsonify([asdict(p) for p in points]), 200

    @app.get("/api/unknown")
    def api_unknown() -> tuple[Response, int]:
        from flask import request

        days = _clamp(request.args.get("days"), _UNKNOWN_DAYS_MIN, _UNKNOWN_DAYS_MAX, 30)
        store = _open_store(cfg)
        if store is None:
            return jsonify([]), 200
        try:
            now = time.time()
            intervals = store.notable_intervals(now - days * 86400, now)
        finally:
            store.close()
        # Newest first: the table's job is "what just went wrong", not archaeology.
        return jsonify([_interval_dict(iv) for iv in reversed(intervals)]), 200

    @app.get("/api/events")
    def api_events() -> tuple[Response, int]:
        from flask import request

        n = _clamp(request.args.get("n"), _EVENTS_MIN, _EVENTS_MAX, 50)
        store = _open_store(cfg)
        if store is None:
            return jsonify([]), 200
        try:
            events = store.recent_events(n)
        finally:
            store.close()
        return jsonify([{"ts": ts, "kind": kind, "detail": detail} for ts, kind, detail in events]), 200

    return app


def run_in_thread(cfg: Config, stop: threading.Event) -> threading.Thread:
    """Run the dashboard's Flask app in a daemon thread.

    For the composition root (``menubar.py``, per the architect's spec) to
    start the web dashboard alongside the collector without either blocking
    the other. A crash inside the Flask dev server is logged here and never
    propagates — this module's whole job is being the household's optional
    read-only viewer; it must not be able to take the collector down with
    it. ``stop`` is observed cooperatively: Flask's built-in ``app.run``
    has no clean programmatic shutdown from outside its own thread, so this
    starts a ``werkzeug`` server explicitly and calls ``shutdown()`` from a
    watcher thread when ``stop`` is set, rather than leaving a stray thread
    behind at process exit.
    """
    from werkzeug.serving import make_server

    app = create_app(cfg)
    server = make_server(cfg.web_host, cfg.web_port, app, threaded=True)

    def _serve() -> None:
        try:
            server.serve_forever()
        except Exception:
            logger.exception("housewatch webapp thread crashed")

    def _watch_stop() -> None:
        stop.wait()
        server.shutdown()

    thread = threading.Thread(target=_serve, name="housewatch-webapp", daemon=True)
    thread.start()
    threading.Thread(target=_watch_stop, name="housewatch-webapp-stopwatch", daemon=True).start()
    return thread
