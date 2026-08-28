"""rumps menubar app for housewatch.

Composition
-----------
This process is separate from the collector daemon (``python -m housewatch
run``, launched by the collector's own LaunchAgent — see ``cli.py``). The
menubar app never opens a read-write ``Store``: it is a *viewer* of whatever
the collector has already written, plus a remote control for the collector
process via ``launchctl`` (never by signaling/killing a pid directly — the
house rule from drive-offload: stop with ``launchctl bootout``, start with
``launchctl bootstrap``, so ``KeepAlive``/``Quit`` semantics stay correct).

Run it directly with ``python -m housewatch.menubar`` (or via
``menubar_launchagent.plist``, this module's own LaunchAgent template, kept
separate from the collector's so either can be started/stopped/crash-
restarted independently).

Two layers, on purpose
-----------------------
1. Pure functions (``format_*``, ``is_stale``, ``cap_progress_pct``,
   ``days_left``, ``build_snapshot``'s *shape* via :class:`Snapshot`) — no
   rumps, no GUI. ``test_menubar.py`` imports and calls these directly, so
   the formatting/decision logic is fully covered without a display or an
   event loop.
2. ``HouseWatchMenubarApp`` — the thin rumps.App wrapper that calls layer 1
   and paints the result. Every network/sqlite call happens on a background
   thread spawned from the timer/menu callbacks; the callbacks themselves
   return immediately so the UI thread is never blocked (a blocked rumps
   callback freezes the whole menubar, spinning-beachball style, since
   NSTimer fires on the main run loop).

The ``import rumps`` is guarded so this module (and its pure functions) can
still be imported and tested on an interpreter that does not have rumps
installed — only constructing/running ``HouseWatchMenubarApp`` requires it.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

from housewatch import config as config_mod
from housewatch.cli import LAUNCHD_LABEL
from housewatch.config import Config
from housewatch.store import CycleSummary, Store

try:  # pragma: no cover - exercised only on an interpreter without rumps
    import rumps
except ImportError:  # pragma: no cover
    rumps = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# This app's own LaunchAgent label — deliberately distinct from the
# collector's (``housewatch.cli.LAUNCHD_LABEL``) so the two processes can be
# started/stopped/crash-restarted independently of each other.
MENUBAR_LAUNCHD_LABEL = "com.housewatch.menubar"

# A sample older than this multiple of the poll interval means the collector
# is very probably not running (or stuck) rather than just between polls.
# Matches the same "5x poll interval" rule the architect spec used for the
# dashboard's own staleness flag, so the two UIs never disagree about what
# "stale" means.
STALE_POLL_MULTIPLE = 5.0

# A day with more than this much unknown (gap + uncovered) time gets a
# warning treatment, matching the '>=' -prefix / '(!)' rule used elsewhere
# in the house (store.py's DayUsage, webapp's UI-honesty rules).
UNKNOWN_WARN_S = 60.0

# How often the rumps.Timer fires a background refresh.
REFRESH_INTERVAL_S = 30.0

# How many trailing local days (today inclusive) make up the "last 7 days"
# menu line.
WEEK_DAYS = 7

# How long to wait, and how often to poll, for a just-spawned dashboard
# server to start accepting connections before giving up.
_DASHBOARD_START_TIMEOUT_S = 4.0
_DASHBOARD_START_POLL_S = 0.2


# ---------------------------------------------------------------------------
# Pure data + formatting + decisions — no rumps, no I/O beyond what's already
# been collected into a Snapshot. Every function below is unit-tested with
# plain values in test_menubar.py.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DaySlice:
    """The handful of DayUsage fields the menubar actually displays, copied
    out into a plain dataclass so Snapshot (and every test) can be built
    without importing/constructing a real housewatch.store.DayUsage."""

    down_bytes: int
    up_bytes: int
    suspect_down: int
    suspect_up: int
    unknown_s: float  # gap_s + uncovered_s — "unknown" is one number to the user


@dataclass(frozen=True)
class Snapshot:
    """Everything one refresh needs, decoupled from Store/sqlite so the
    formatting layer is testable with plain constructed values and so a
    failed query (db missing, locked, malformed) has one obvious shape:
    ``ok=False`` plus an error string, never a partially-filled snapshot
    that formatting code might misread as real data.
    """

    ok: bool
    error: str | None
    today: DaySlice = field(
        default_factory=lambda: DaySlice(0, 0, 0, 0, 0.0)
    )
    yesterday: DaySlice = field(
        default_factory=lambda: DaySlice(0, 0, 0, 0, 0.0)
    )
    week: DaySlice = field(
        default_factory=lambda: DaySlice(0, 0, 0, 0, 0.0)
    )
    cycle: CycleSummary | None = None
    last_sample_age_s: float | None = None
    notable_count_30d: int = 0
    poll_interval_s: float = 10.0


def _failed_snapshot(cfg: Config, error: str) -> Snapshot:
    return Snapshot(ok=False, error=error, poll_interval_s=cfg.poll_interval_s)


def format_bytes_compact(n: float | int | None) -> str:
    """'3.2 GB' style: short enough not to crowd the menubar. Decimal (1000-
    based) units, matching store.py's cap_bytes convention (1 GB = 1e9 B) so
    the menubar and the cycle-cap math never disagree by a factor of
    1.024. ``None`` -> '?' (never '0 B' — unknown must never read as zero,
    the house-wide invariant from model.py)."""
    if n is None:
        return "?"
    value = float(n)
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value < 1000.0:
        return f"{sign}{value:.0f} B"
    for unit in ("KB", "MB", "GB"):
        value /= 1000.0
        if value < 1000.0:
            return f"{sign}{value:.1f} {unit}"
    value /= 1000.0
    return f"{sign}{value:.1f} TB"


def _fmt_duration_compact(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def is_stale(
    last_sample_age_s: float | None,
    poll_interval_s: float,
    multiple: float = STALE_POLL_MULTIPLE,
) -> bool:
    """True when the last sample is old enough that the collector is very
    probably not running (or wedged) rather than just between polls. No
    sample at all (``None``) always counts as stale — there is nothing more
    stale than "never"."""
    if last_sample_age_s is None:
        return True
    return last_sample_age_s > multiple * poll_interval_s


def cap_progress_pct(used_bytes: int, cap_bytes: int | None) -> float | None:
    """Percent of the monthly cap used, or ``None`` when no cap is
    configured (the unset-cap path: the title falls back to a plain byte
    count instead of a meaningless '0%'). Not clamped to 100 — a value over
    100 is real information (already over cap) that a clamp would hide."""
    if cap_bytes is None or cap_bytes <= 0:
        return None
    return used_bytes / cap_bytes * 100.0


def days_left(days_elapsed: float, days_total: float) -> float:
    """Days remaining in the billing cycle, floored at 0 (a cycle that has
    technically run past its own boundary — clock skew, a delayed query —
    should never display as negative)."""
    return max(days_total - days_elapsed, 0.0)


def format_title(snap: Snapshot) -> str:
    """The compact menubar title.

    - Query failed (db missing/locked/corrupt): a bare dash — a clear,
      crash-free "something's wrong, look at the menu" signal per the task's
      degrade-gracefully requirement.
    - Cap configured: cycle progress, e.g. '42% - 21d' (percent used, days
      left in the billing cycle) — more actionable than a raw byte count
      once there's a budget to watch.
    - No cap: today's total (down+up), e.g. '3.2 GB'.
    - ' (!)' is appended whenever the collector looks stale OR today has
      more than a minute of unknown time — one glyph, two honest reasons,
      matching the '(!)' convention from the architect's dashboard spec.
    """
    if not snap.ok:
        return "–"  # en dash

    stale = is_stale(snap.last_sample_age_s, snap.poll_interval_s)
    cap_bytes = snap.cycle.cap_bytes if snap.cycle is not None else None
    if cap_bytes is not None and snap.cycle is not None:
        pct = cap_progress_pct(snap.cycle.used_bytes, cap_bytes)
        dleft = days_left(snap.cycle.days_elapsed, snap.cycle.days_total)
        assert pct is not None  # cap_bytes is not None here, by construction
        base = f"{pct:.0f}% - {dleft:.0f}d"
    else:
        total = snap.today.down_bytes + snap.today.up_bytes
        if snap.today.unknown_s > UNKNOWN_WARN_S:
            # Same rule as format_day_line: a bare number for a day with
            # hours of unknown time reads as a measurement it is not, and
            # '0 B (!)' still scans as "you used nothing" at a glance.
            base = "no data" if total == 0 else f"≥{format_bytes_compact(total)}"
        else:
            base = format_bytes_compact(total)

    warn = stale or snap.today.unknown_s > UNKNOWN_WARN_S
    return f"{base} (!)" if warn else base


def format_day_line(label: str, day: DaySlice) -> str:
    """One day's usage, honouring the house UI-honesty rules.

    These are binding on BOTH UIs (the dashboard's app.js already honoured
    them; this file did not): a day carrying more than a minute of unknown
    time reads '>= N GB', never a bare 'N GB', and a day with nothing
    measured at all reads 'no data', never '0 B down / 0 B up'. '0 B' for a
    day the collector never saw is exactly the misreading this project
    exists to prevent -- it says "you used nothing" when the truth is "we
    have no idea".
    """
    unknown = day.unknown_s > UNKNOWN_WARN_S
    if unknown and day.down_bytes == 0 and day.up_bytes == 0:
        return f"{label}: no data ({_fmt_duration_compact(day.unknown_s)} unmeasured)"

    prefix = ">= " if unknown else ""
    line = (
        f"{label}: {prefix}{format_bytes_compact(day.down_bytes)} down / "
        f"{prefix}{format_bytes_compact(day.up_bytes)} up"
    )
    if unknown:
        line += f"  ({_fmt_duration_compact(day.unknown_s)} unknown)"
    if day.suspect_down or day.suspect_up:
        line += "  (includes suspect data)"
    return line


def format_cycle_line(cycle: CycleSummary) -> str:
    used = format_bytes_compact(cycle.used_bytes)
    cap = "no cap set" if cycle.cap_bytes is None else format_bytes_compact(cycle.cap_bytes)
    prefix = ">= " if cycle.unknown_s > UNKNOWN_WARN_S else ""
    return f"Cycle: {prefix}{used} of {cap}  ({cycle.cycle_start}..{cycle.cycle_end})"


def format_projected_line(cycle: CycleSummary) -> str:
    line = f"Projected: {format_bytes_compact(cycle.projected_bytes)} (estimate)"
    if cycle.over_cap_projected:
        line += "  ⚠ over cap"
    return line


def format_last_sample_line(last_sample_age_s: float | None, stale: bool) -> str:
    if last_sample_age_s is None:
        return "Last sample: never seen — is the collector running?"
    line = f"Last sample: {_fmt_duration_compact(last_sample_age_s)} ago"
    if stale:
        line += "  ⚠ collector looks stopped"
    return line


def format_data_quality_line(today_unknown_s: float, notable_count_30d: int) -> str:
    return (
        f"Data quality: {_fmt_duration_compact(today_unknown_s)} unknown today, "
        f"{notable_count_30d} gap/suspect window(s) in last 30d"
    )


def dashboard_url(cfg: Config) -> str:
    return f"http://{cfg.web_host}:{cfg.web_port}/"


# ---------------------------------------------------------------------------
# I/O: building a Snapshot from the real Store, and the launchctl / dashboard
# side-effecting helpers. Every one of these is written to raise nothing —
# failures come back as a value (Snapshot.ok=False, or a (bool, str) result
# tuple) so a caller on a background thread never needs a bare except:
# Exception around GUI code to stay safe.
# ---------------------------------------------------------------------------


def build_snapshot(cfg: Config, now_ts: float) -> Snapshot:
    """Query the (readonly) Store for everything one refresh needs.

    Every failure mode the task calls out explicitly degrades to
    ``Snapshot(ok=False, ...)`` rather than raising: a missing db (first run,
    before the collector's first poll), a locked db (WAL means this should
    be rare, but a foreign process or a corrupt file could still cause it),
    or any other sqlite/parsing error. The menubar must never crash or show
    a traceback dialog over "the collector hasn't started yet".
    """
    db_path = Path(os.path.expanduser(cfg.db_path))
    try:
        store = Store(db_path, readonly=True)
    except Exception as exc:  # FileNotFoundError (no db yet), or anything else
        return _failed_snapshot(cfg, str(exc))

    try:
        tz = config_mod.tzinfo_of(cfg)
        recent = store.recent_days(WEEK_DAYS, tz, now_ts)  # oldest..today
        today_du = recent[-1]
        yesterday_du = recent[-2] if len(recent) >= 2 else recent[-1]
        week_down = sum(d.down_bytes for d in recent)
        week_up = sum(d.up_bytes for d in recent)
        week_suspect_down = sum(d.suspect_down for d in recent)
        week_suspect_up = sum(d.suspect_up for d in recent)
        week_unknown = sum(d.gap_s + d.uncovered_s for d in recent)

        cycle = store.cycle_summary(cfg, now_ts)
        last = store.last_reading()
        last_age = (now_ts - last.ts) if last is not None else None
        notable = store.notable_intervals(now_ts - 30 * 86400.0, now_ts)

        return Snapshot(
            ok=True,
            error=None,
            today=DaySlice(
                today_du.down_bytes,
                today_du.up_bytes,
                today_du.suspect_down,
                today_du.suspect_up,
                today_du.gap_s + today_du.uncovered_s,
            ),
            yesterday=DaySlice(
                yesterday_du.down_bytes,
                yesterday_du.up_bytes,
                yesterday_du.suspect_down,
                yesterday_du.suspect_up,
                yesterday_du.gap_s + yesterday_du.uncovered_s,
            ),
            week=DaySlice(
                week_down, week_up, week_suspect_down, week_suspect_up, week_unknown
            ),
            cycle=cycle,
            last_sample_age_s=last_age,
            notable_count_30d=len(notable),
            poll_interval_s=cfg.poll_interval_s,
        )
    except Exception as exc:
        return _failed_snapshot(cfg, str(exc))
    finally:
        store.close()


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", *args], capture_output=True, text=True, timeout=10
    )


def collector_is_loaded() -> bool:
    """Whether the collector's LaunchAgent is currently loaded (not the same
    as "healthy" — a loaded-but-stuck collector is what Snapshot's staleness
    check is for)."""
    try:
        result = _launchctl("print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}")
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def start_collector() -> tuple[bool, str]:
    """Start the collector via ``launchctl bootstrap`` — never by spawning
    the process directly, so the LaunchAgent's KeepAlive/RunAtLoad semantics
    stay in charge of it exactly as they do after a real login (the
    drive-offload house rule)."""
    plist = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    if not plist.exists():
        return False, f"no LaunchAgent installed at {plist} (see `python -m housewatch doctor`)"
    try:
        result = _launchctl("bootstrap", f"gui/{os.getuid()}", str(plist))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"launchctl bootstrap failed: {exc}"
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        return False, f"launchctl bootstrap failed: {detail}"
    return True, "collector started"


def stop_collector() -> tuple[bool, str]:
    """Stop the collector via ``launchctl bootout`` — never by killing the
    pid. ``bootout`` is what lets a deliberate stop actually stick until the
    next bootstrap, exactly like the menubar's own Quit is meant to."""
    try:
        result = _launchctl("bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"launchctl bootout failed: {exc}"
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        return False, f"launchctl bootout failed (already stopped?): {detail}"
    return True, "collector stopped"


def _port_reachable(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ensure_dashboard_running(cfg: Config) -> tuple[bool, str]:
    """Make sure the dashboard is reachable at ``cfg.web_host:web_port``,
    starting ``python -m housewatch serve`` as a detached background process
    if nothing answers there yet. Runs entirely off the caller's thread of
    choice (the menubar callback spawns a worker thread before calling
    this) since both the initial connect probe and the startup wait can
    each take up to a few seconds.
    """
    if _port_reachable(cfg.web_host, cfg.web_port):
        return True, "already running"

    python = sys.executable
    try:
        subprocess.Popen(
            [python, "-m", "housewatch", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # survives the menubar app exiting
        )
    except OSError as exc:
        return False, f"could not start dashboard server: {exc}"

    deadline = time.monotonic() + _DASHBOARD_START_TIMEOUT_S
    while time.monotonic() < deadline:
        if _port_reachable(cfg.web_host, cfg.web_port):
            return True, "started"
        time.sleep(_DASHBOARD_START_POLL_S)
    return False, (
        "dashboard did not come up in time — check "
        "`python -m housewatch serve` runs cleanly (needs flask installed "
        "and a db that already has at least one poll in it)"
    )


def open_log() -> tuple[bool, str]:
    log_path = config_mod.LOG_DIR / "housewatch.log"
    if not log_path.exists():
        return False, f"no log file yet at {log_path}"
    try:
        subprocess.run(["open", str(log_path)], check=True, timeout=5)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return False, f"could not open log: {exc}"
    return True, "opened"


# ---------------------------------------------------------------------------
# LaunchAgent template for this app itself (see menubar_launchagent.plist —
# this is the string that file's content matches; kept here too so a future
# install helper does not have to reparse the file to know its own tokens).
# ---------------------------------------------------------------------------


def render_launchagent_plist(python_path: str, workdir: str, home: str) -> str:
    """Render this app's own LaunchAgent plist from the template shipped at
    ``menubar_launchagent.plist``, substituting ``{PYTHON}``, ``{WORKDIR}``,
    ``{HOME}``. ``./install.sh`` installs the COLLECTOR's agent
    (``com.housewatch.collector.plist``) only — the menubar app is optional,
    so this renders its plist for a by-hand install and keeps the template
    testable.
    """
    template_path = Path(__file__).resolve().parent / "menubar_launchagent.plist"
    text = template_path.read_text(encoding="utf-8")
    return (
        text.replace("{PYTHON}", python_path)
        .replace("{WORKDIR}", workdir)
        .replace("{HOME}", home)
    )


# ---------------------------------------------------------------------------
# The rumps app itself.
# ---------------------------------------------------------------------------

if rumps is not None:

    class HouseWatchMenubarApp(rumps.App):  # type: ignore[misc]
        """Glanceable today/cycle usage in the menubar, with a dropdown for
        detail and controls. Construction (this method) does no I/O beyond
        building menu widgets and kicking off one background refresh — it
        never touches the network or sqlite on the calling thread, so it is
        safe to construct (though not to ``.run()``) in a headless check.
        """

        def __init__(self, cfg: Config) -> None:
            super().__init__("housewatch", title="–", quit_button=None)
            self.cfg = cfg
            self._refresh_lock = threading.Lock()

            self.item_today = rumps.MenuItem("Today: –")
            self.item_yesterday = rumps.MenuItem("Yesterday: –")
            self.item_week = rumps.MenuItem("Last 7 days: –")
            self.item_cycle = rumps.MenuItem("Cycle: –")
            self.item_projected = rumps.MenuItem("Projected: –")
            self.item_last_sample = rumps.MenuItem("Last sample: –")
            self.item_quality = rumps.MenuItem("Data quality: –")
            self.item_collector_toggle = rumps.MenuItem(
                "Start Collector", callback=self._on_toggle_collector
            )

            self.menu = [
                self.item_today,
                self.item_yesterday,
                self.item_week,
                None,
                self.item_cycle,
                self.item_projected,
                None,
                self.item_last_sample,
                self.item_quality,
                None,
                rumps.MenuItem("Open Dashboard", callback=self._on_open_dashboard),
                rumps.MenuItem("Refresh Now", callback=self._on_refresh_now),
                rumps.MenuItem("Open Log", callback=self._on_open_log),
                None,
                self.item_collector_toggle,
                None,
                rumps.MenuItem("Quit", callback=self._on_quit),
            ]

            self.timer = rumps.Timer(self._on_timer, REFRESH_INTERVAL_S)
            self.timer.start()
            self._refresh_async()

        # -- refresh plumbing --------------------------------------------

        def _on_timer(self, _sender: object) -> None:
            self._refresh_async()

        def _on_refresh_now(self, _sender: object) -> None:
            self._refresh_async()

        def _refresh_async(self) -> None:
            # A lock, not a queue: if a refresh is already in flight when the
            # timer fires again, skip rather than pile up threads — the next
            # timer tick 30s later is a fine substitute for a dropped tick.
            if not self._refresh_lock.acquire(blocking=False):
                return
            threading.Thread(target=self._refresh_worker, daemon=True).start()

        def _refresh_worker(self) -> None:
            try:
                snap = build_snapshot(self.cfg, time.time())
                self._apply_snapshot(snap)
            except Exception:
                logger.exception("menubar refresh failed")
            finally:
                self._refresh_lock.release()

        def _apply_snapshot(self, snap: Snapshot) -> None:
            self.title = format_title(snap)
            if not snap.ok:
                self.item_today.title = f"housewatch: no data ({snap.error})"
                self.item_yesterday.title = ""
                self.item_week.title = ""
                self.item_cycle.title = ""
                self.item_projected.title = ""
                self.item_last_sample.title = "Last sample: –"
                self.item_quality.title = ""
                return

            stale = is_stale(snap.last_sample_age_s, snap.poll_interval_s)
            self.item_today.title = format_day_line("Today", snap.today)
            self.item_yesterday.title = format_day_line("Yesterday", snap.yesterday)
            self.item_week.title = format_day_line("Last 7 days", snap.week)
            if snap.cycle is not None:
                self.item_cycle.title = format_cycle_line(snap.cycle)
                self.item_projected.title = format_projected_line(snap.cycle)
            self.item_last_sample.title = format_last_sample_line(
                snap.last_sample_age_s, stale
            )
            self.item_quality.title = format_data_quality_line(
                snap.today.unknown_s, snap.notable_count_30d
            )

            loaded = collector_is_loaded()
            self.item_collector_toggle.title = (
                "Stop Collector" if loaded else "Start Collector"
            )

        # -- menu actions --------------------------------------------------

        def _on_open_dashboard(self, _sender: object) -> None:
            def work() -> None:
                ok, detail = ensure_dashboard_running(self.cfg)
                if ok:
                    webbrowser.open(dashboard_url(self.cfg))
                else:
                    rumps.notification(
                        "housewatch", "Couldn't open the dashboard", detail
                    )

            threading.Thread(target=work, daemon=True).start()

        def _on_open_log(self, _sender: object) -> None:
            def work() -> None:
                ok, detail = open_log()
                if not ok:
                    rumps.notification("housewatch", "Couldn't open the log", detail)

            threading.Thread(target=work, daemon=True).start()

        def _on_toggle_collector(self, _sender: object) -> None:
            def work() -> None:
                if collector_is_loaded():
                    ok, detail = stop_collector()
                else:
                    ok, detail = start_collector()
                if not ok:
                    rumps.notification("housewatch", "Collector control failed", detail)
                self._refresh_async()

            threading.Thread(target=work, daemon=True).start()

        def _on_quit(self, _sender: object) -> None:
            # No Store/thread of ours needs a join: the menubar never holds
            # a read-write handle, and every background thread here is a
            # short-lived daemon thread, so a plain quit is already clean.
            rumps.quit_application()

else:  # pragma: no cover - only when rumps genuinely is not installed

    HouseWatchMenubarApp = None  # type: ignore[assignment,misc]


def main() -> None:
    if rumps is None:
        print(
            "rumps is not installed in this interpreter — the housewatch "
            "menubar app cannot run here. Use the venv that has it "
            "(pip install rumps), e.g. ./.venv/bin/python3 from this checkout.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    cfg = config_mod.load()
    logging.basicConfig(level=getattr(logging, cfg.log_level, logging.INFO))
    app = HouseWatchMenubarApp(cfg)
    app.run()


if __name__ == "__main__":
    main()
