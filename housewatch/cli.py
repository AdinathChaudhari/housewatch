"""The ``python -m housewatch <cmd>`` commands.

Six subcommands, no more:

* ``run``    — the daemon loop itself, and what the LaunchAgent invokes
               (``./install.sh`` renders com.housewatch.collector.plist and
               bootstraps it). It blocks in the foreground, polling on the
               configured cadence, until it receives SIGTERM/SIGINT
               (``launchctl bootout`` sends SIGTERM) or is killed.
* ``once``   — a single real poll, persisted, with the outcome printed.
               For debugging: "did that poll actually work, and what did it
               write".
* ``status`` — today's usage, this billing cycle's usage vs. cap, last
               sample age, and a gap count. Read-only.
* ``serve``  — the web dashboard. ``housewatch.webapp`` is imported lazily,
               inside this function, specifically so that a missing/broken
               flask install never breaks ``run``/``once``/``status`` — only
               this one subcommand needs it.
* ``doctor`` — is the router reachable, is the db writable, is the
               LaunchAgent loaded. Prints an actionable fix line under each
               failed check, not just a bare failure.
* ``config`` — read or change the persisted config without hand-editing
               TOML: ``config show`` (every value plus whether it came from
               the file or a default), ``config set-cap <GB>``, ``config
               clear-cap``, ``config set-cycle-day <1-28>``. Every write goes
               through the same validation a hand-edited file would, so a
               bad value is rejected before anything on disk changes — see
               ``_apply_config_mutation``.

Every command loads config the same way (``config.load()``, default path)
and every command that touches the database respects the house's
single-writer rule: only ``run`` and ``once`` open a read-write ``Store``,
guarded by the pidfile lock so two overlapping ``run``/``once`` invocations
cannot both hold write access at the same time; ``status``/``doctor`` open
readonly.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import logging
import logging.handlers
import math
import os
import signal
import subprocess
import sys
import threading
import time
import tomllib
from dataclasses import fields, replace
from pathlib import Path

from housewatch import billing
from housewatch import config as config_mod
from housewatch import counters
from housewatch import collector as collector_mod
from housewatch.collector import Collector, build_source
from housewatch.config import Config, ConfigError
from housewatch.model import SourceError
from housewatch.store import Store

logger = logging.getLogger(__name__)

# The collector's LaunchAgent label. This MUST match the <string> under
# <key>Label</key> in com.housewatch.collector.plist, which is what install.sh
# actually renders into ~/Library/LaunchAgents/ -- menubar.py's
# collector_is_loaded()/start_collector()/stop_collector() all address the
# agent by this name. They disagreed once (this said a personal-namespaced
# label, e.g. com.example.housewatch, while everything installable said
# com.housewatch.collector), which silently
# left the menubar's Start/Stop Collector control permanently broken and made
# `doctor` report a healthy install as "not loaded". test_cli.py pins this
# constant against the plist and the shell scripts so it cannot drift again.
LAUNCHD_LABEL = "com.housewatch.collector"

# Runtime state directory: the pidfile (single-instance guard) lives here,
# alongside where config.py already keeps its own files, rather than
# inventing a third location.
_RUNTIME_DIR = Path.home() / ".config" / "housewatch"
PIDFILE_PATH = _RUNTIME_DIR / "housewatch.pid"

_LOG_MAX_BYTES = 5 * 1024 * 1024
_LOG_BACKUP_COUNT = 5


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _load_config_or_die() -> Config:
    try:
        return config_mod.load()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        raise SystemExit(1)


def _setup_logging(cfg: Config) -> None:
    """Structured logging to a rotating file under ``config.LOG_DIR``.

    Separate from (and in addition to) whatever launchd itself redirects
    stdout/stderr to (``agent.out.log``/``agent.err.log`` — see the
    LaunchAgent plist): those are raw, un-rotated process output, so without
    this handler the log would grow forever. Also attaches a stream handler
    so ``once``/``status``/``doctor`` run interactively still show log lines
    on the terminal.
    """
    config_mod.LOG_DIR.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, cfg.log_level, logging.INFO)

    root = logging.getLogger("housewatch")
    root.setLevel(level)
    root.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z"
    )

    file_handler = logging.handlers.RotatingFileHandler(
        config_mod.LOG_DIR / "housewatch.log",
        maxBytes=_LOG_MAX_BYTES,
        backupCount=_LOG_BACKUP_COUNT,
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    root.addHandler(stream_handler)


@contextlib.contextmanager
def _pidfile_lock():
    """Single-instance guard for anything opening a read-write Store.

    An advisory ``flock`` on a file that stays open for the life of the
    process — held locks are released automatically if the process dies for
    any reason (crash, kill -9), so there is no stale-pidfile cleanup logic
    to get wrong. Raises ``RuntimeError`` with the blocking PID (when
    determinable) if another instance already holds the lock.
    """
    _RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(PIDFILE_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            holder = ""
            try:
                with open(PIDFILE_PATH) as f:
                    holder = f.read().strip()
            except OSError:
                pass
            detail = f" (pid {holder})" if holder else ""
            raise RuntimeError(
                f"another housewatch instance already holds the lock at "
                f"{PIDFILE_PATH}{detail} — stop it first (launchctl bootout "
                f"gui/$(id -u)/{LAUNCHD_LABEL}, or kill it if run manually)"
            ) from None

        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        os.fsync(fd)
        yield
    finally:
        # Closing releases the flock even if something above raised.
        os.close(fd)


def _is_locked() -> bool:
    """Non-destructive check: is some other process currently holding the
    pidfile lock? Used by ``doctor`` to decide whether it is safe to open a
    throwaway read-write Store for a write-check, without stealing the lock
    out from under a running daemon."""
    if not PIDFILE_PATH.exists():
        return False
    fd = os.open(PIDFILE_PATH, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _fmt_bytes(n: int | float | None) -> str:
    if n is None:
        return "unknown"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000.0:
            return f"{n:.2f} {unit}"
        n /= 1000.0
    return f"{n:.2f} PB"


def _fmt_duration(seconds: float | None) -> str:
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


# ---------------------------------------------------------------------------
# Config rendering + the config read/write path shared by `config *`
# ---------------------------------------------------------------------------
#
# `config.read_overrides`/`config.write_overrides` are the frozen, canonical
# home for this (config.py owns the file format); the fallbacks below give
# `config show`/`set-cap`/`clear-cap`/`set-cycle-day` a working, self-
# contained implementation of the identical contract in the meantime, so
# this module never hard-depends on landing order. Once config.py exports
# them, `getattr` below picks up the canonical version automatically — no
# further change needed here.

_CONFIG_WRITE_HEADER = (
    "# written by `housewatch config` -- hand-edits welcome, but comments\n"
    "# are not preserved by config set-*; full key documentation lives in\n"
    "# config.example.toml.\n"
)


def _render_timezone(cfg: Config) -> str:
    """'local' -> 'local -> <resolved zone>' or the UNRESOLVED fallback line.

    A non-'local' value is already an explicit IANA name, so it's shown as
    given -- only 'local' needs the OS-resolution step spelled out.
    """
    if cfg.timezone != "local":
        return cfg.timezone
    zone = config_mod.system_zone_name()
    if zone is not None:
        return f"local -> {zone}"
    return "local -> UNRESOLVED (fixed-offset fallback)"


def _render_field_value(cfg: Config, key: str) -> str:
    """Render one Config field for `config show`/`doctor`.

    Three keys get a human sentence instead of the raw repr, because their
    default is a meaningful absence (no cap, auto-detect, OS-resolved zone)
    rather than just another number -- everything else prints as-is.
    """
    value = getattr(cfg, key)
    if key == "monthly_cap_gb" and value is None:
        return "unset (no cap — usage still tracked)"
    if key == "router_host" and value is None:
        return "auto-detect default gateway"
    if key == "timezone":
        return _render_timezone(cfg)
    return str(value)


def _fallback_read_overrides(path: Path | None) -> dict[str, object]:
    target = path if path is not None else config_mod.DEFAULT_PATH
    if not target.exists():
        return {}
    try:
        with target.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"{target}: not valid TOML ({exc}). Fix the syntax or delete "
            "the file to fall back to defaults."
        ) from exc
    except OSError as exc:
        raise ConfigError(f"{target}: could not read config file: {exc}") from exc

    field_names = frozenset(f.name for f in fields(Config))
    unknown = set(raw) - field_names
    if unknown:
        raise ConfigError(
            f"{target}: unknown config key(s) {sorted(unknown)}. "
            f"Valid keys: {sorted(field_names)}."
        )
    return raw


def _fallback_write_overrides(overrides: dict[str, object], path: Path | None) -> None:
    target = path if path is not None else config_mod.DEFAULT_PATH
    field_names = frozenset(f.name for f in fields(Config))

    unknown = set(overrides) - field_names
    if unknown:
        raise ConfigError(
            f"unknown config key(s) {sorted(unknown)}. Valid keys: {sorted(field_names)}."
        )
    for key, value in overrides.items():
        if value is None:
            raise ConfigError(
                f"cannot write {key}=None -- TOML has no null; to clear a key, "
                "remove it from the overrides instead of setting it to None."
            )

    target.parent.mkdir(parents=True, exist_ok=True)
    lines = _CONFIG_WRITE_HEADER.splitlines() + [""]
    for f in fields(Config):
        if f.name not in overrides:
            continue
        lines.append(f"{f.name} = {config_mod._toml_scalar(overrides[f.name])}")
    content = "\n".join(lines) + "\n"

    tmp_path = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, target)
    finally:
        tmp_path.unlink(missing_ok=True)


def _config_read_overrides(path: Path | None = None) -> dict[str, object]:
    """Just the keys explicitly present in the config file ({} if absent).

    Prefers `config.read_overrides` (the frozen, canonical implementation)
    when it exists; falls back to a self-contained equivalent otherwise —
    see the section comment above.
    """
    fn = getattr(config_mod, "read_overrides", None)
    if fn is not None:
        return fn(path)
    return _fallback_read_overrides(path)


def _config_write_overrides(overrides: dict[str, object], path: Path | None = None) -> None:
    """Write only the given keys, atomically. See `_config_read_overrides`."""
    fn = getattr(config_mod, "write_overrides", None)
    if fn is not None:
        fn(overrides, path)
        return
    _fallback_write_overrides(overrides, path)


def _apply_config_mutation(mutate):
    """The one write path every `config set-*`/`clear-*` command uses.

    read_overrides() -> mutate the dict in place -> replace(Config(), **raw)
    -> validate() -> write_overrides(raw). Any step failing (a pre-existing
    broken file, or the new value making the config invalid) prints the
    standard ``config error: ...`` line and returns None *before* writing
    anything — a bad `set-cap`/`set-cycle-day` can never corrupt a
    previously-valid file. Returns (candidate_config, raw_overrides) on
    success.
    """
    try:
        raw = _config_read_overrides()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return None

    mutate(raw)

    try:
        candidate = replace(Config(), **raw)
    except TypeError as exc:
        print(f"config error: malformed config table ({exc}).", file=sys.stderr)
        return None

    try:
        config_mod.validate(candidate)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return None

    try:
        _config_write_overrides(raw)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return None

    return candidate, raw


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _load_config_or_die()
    _setup_logging(cfg)
    logger.info("housewatch run: starting (pid %d)", os.getpid())

    try:
        with _pidfile_lock():
            store = Store(Path(os.path.expanduser(cfg.db_path)), readonly=False)
            store.init_schema()
            try:
                source = build_source(cfg, store)
                collector = Collector(cfg, source, store)

                stop = threading.Event()

                def _handle_signal(signum: int, _frame: object) -> None:
                    logger.info("received signal %d, shutting down", signum)
                    stop.set()

                signal.signal(signal.SIGTERM, _handle_signal)
                signal.signal(signal.SIGINT, _handle_signal)

                collector.run_forever(stop)
            finally:
                store.close()
    except RuntimeError as exc:
        logger.error("%s", exc)
        print(str(exc), file=sys.stderr)
        return 1

    logger.info("housewatch run: clean exit")
    return 0


# ---------------------------------------------------------------------------
# once
# ---------------------------------------------------------------------------


def cmd_once(args: argparse.Namespace) -> int:
    cfg = _load_config_or_die()
    _setup_logging(cfg)

    try:
        with _pidfile_lock():
            store = Store(Path(os.path.expanduser(cfg.db_path)), readonly=False)
            store.init_schema()
            try:
                source = build_source(cfg, store)
                collector = Collector(cfg, source, store)
                print(f"source: {source.describe()}")
                result = collector.poll_once()
            finally:
                store.close()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    # `identity` only exists once igd.py's discovery-identity wiring has
    # landed; until then this is silently absent rather than guessed.
    identity = getattr(source, "identity", None)
    if identity is not None:
        print(f"router: {identity.summary()} at {source.location}")

    if result is None:
        print("poll FAILED (source unreachable or unparseable) — see log for detail")
        return 1

    print(f"kind:       {result.kind}")
    print(f"start_ts:   {result.start_ts:.3f}")
    print(f"end_ts:     {result.end_ts:.3f}")
    print(f"duration:   {_fmt_duration(result.end_ts - result.start_ts)}")
    print(f"down:       {_fmt_bytes(result.down_bytes)}"
          + (f" ({result.down_bytes} B, {result.down_wraps} wrap(s))" if result.down_bytes is not None else ""))
    print(f"up:         {_fmt_bytes(result.up_bytes)}"
          + (f" ({result.up_bytes} B, {result.up_wraps} wrap(s))" if result.up_bytes is not None else ""))
    print(f"note:       {result.note}")
    if result.kind == "baseline":
        print("first reading recorded — usage deltas start with the next poll")
    return 0


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _load_config_or_die()
    db_path = Path(os.path.expanduser(cfg.db_path))
    if not db_path.exists():
        print(f"no database yet at {db_path} — run `python -m housewatch once` first")
        return 1

    tz = config_mod.tzinfo_of(cfg)
    now = time.time()

    store = Store(db_path, readonly=True)
    try:
        today = store.day_usage(_today_key(now, tz), tz, now)
        cycle = store.cycle_summary(cfg, now)
        last = store.last_reading()
        gaps = store.notable_intervals(now - 30 * 86400, now, kinds=("gap",))
        router_identity = store.get_meta("router_identity")
    finally:
        store.close()

    print("== Today ==")
    print(f"  down: {_fmt_bytes(today.down_bytes)}  up: {_fmt_bytes(today.up_bytes)}")
    if today.suspect_down or today.suspect_up:
        print(f"  (includes suspect: down {_fmt_bytes(today.suspect_down)}, up {_fmt_bytes(today.suspect_up)})")
    unknown_today = today.gap_s + today.uncovered_s
    print(f"  unknown time: {_fmt_duration(unknown_today)}"
          + ("  (>= prefix warranted)" if unknown_today > 60 else ""))

    print("== This billing cycle ==")
    used_str = _fmt_bytes(cycle.used_bytes)
    cap_str = _fmt_bytes(cycle.cap_bytes) if cycle.cap_bytes is not None else "no cap set"
    prefix = ">= " if cycle.unknown_s > 60 else ""
    print(f"  {prefix}{used_str} used of {cap_str}  ({cycle.cycle_start} .. {cycle.cycle_end})")
    if cycle.cap_bytes is None:
        print("  set one (optional — usage is tracked either way): "
              "python -m housewatch config set-cap <GB>")
    print(f"  days: {cycle.days_elapsed:.1f} / {cycle.days_total:.1f}")
    print(f"  projected (estimate): {_fmt_bytes(cycle.projected_bytes)}")
    if cycle.over_cap_projected is True:
        print("  WARNING: projected usage exceeds the configured cap")
    print(f"  unknown time this cycle: {_fmt_duration(cycle.unknown_s)}")

    print("== Collector ==")
    if last is None:
        print("  no sample recorded yet")
    else:
        age = now - last.ts
        flag = " (!)" if age > 5 * cfg.poll_interval_s else ""
        print(f"  last sample: {_fmt_duration(age)} ago{flag}")
    print(f"  gap intervals in last 30 days: {len(gaps)}")
    print(f"  router: {router_identity or 'not yet identified'}")

    return 0


def _today_key(now: float, tz) -> str:
    return billing.day_key(now, tz)


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> int:
    cfg = _load_config_or_die()
    _setup_logging(cfg)

    db_path = Path(os.path.expanduser(cfg.db_path))
    if not db_path.exists():
        print(
            f"no database yet at {db_path} — run `python -m housewatch once` "
            "(or start `run`) at least once before serving the dashboard",
            file=sys.stderr,
        )
        return 1

    try:
        from housewatch import webapp  # lazy: a missing/broken flask must never break `run`/`once`/`status`
    except ImportError as exc:
        print(
            f"cannot start the web dashboard: {exc}\n"
            "the `housewatch.webapp` module or one of its dependencies "
            "(flask, jinja2) is not available in this interpreter",
            file=sys.stderr,
        )
        return 1

    # Symmetry with `run`: SIGTERM must produce a logged, exit-0 shutdown,
    # not a silent 143. Werkzeug's dev server has no stop() we can call from
    # a handler, so the handler raises KeyboardInterrupt in the main thread,
    # which app.run() already unwinds cleanly.
    def _handle_signal(signum: int, _frame: object) -> None:
        logger.info("received signal %d, shutting down the dashboard", signum)
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _handle_signal)

    app = webapp.create_app(cfg)
    try:
        app.run(
            host=cfg.web_host,
            port=cfg.web_port,
            debug=False,
            use_reloader=False,
            threaded=True,
        )
    except KeyboardInterrupt:
        pass  # SIGTERM (handler above) or Ctrl-C: both are a clean stop
    logger.info("housewatch serve: clean exit")
    return 0


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    from housewatch import igd

    cfg = _load_config_or_die()
    ok = True

    print("== Config ==")
    print(f"  OK — {config_mod.DEFAULT_PATH} (or defaults)")
    print(f"  monthly_cap_gb: {_render_field_value(cfg, 'monthly_cap_gb')}")
    _cycle_day = cfg.billing_cycle_start_day
    _cycle_note = " (calendar month)" if _cycle_day == 1 else ""
    print(f"  billing_cycle_start_day: {_cycle_day}{_cycle_note}")
    print(f"  timezone: {_render_field_value(cfg, 'timezone')}")
    print(f"  poll_interval_s={cfg.poll_interval_s}  max_link_mbps={cfg.max_link_mbps}  "
          f"certainty window={counters.certainty_window_s(cfg.max_link_mbps * 1e6 / 8):.1f}s")
    if cfg.max_link_mbps < config_mod.MIN_SAFE_LINK_MBPS:
        # Reachable only with i_know_my_link_is_slower=true. A widened
        # certainty window must be visible wherever a human looks.
        print(f"  WARNING — max_link_mbps is below {config_mod.MIN_SAFE_LINK_MBPS:g}: byte "
              "deltas are being called 'certain' over a much longer gap than")
        print("            the hardware justifies. Raise it unless the WAN port really "
              "is that slow (see config.example.toml).")

    print("== Router reachability (SSDP + SOAP via a live read) ==")
    source = igd.UpnpIgdSource(
        router_host=cfg.router_host,
        http_timeout_s=min(cfg.http_timeout_s, 3.0),
        ssdp_timeout_s=min(cfg.ssdp_timeout_s, 3.0),
    )
    try:
        reading = source.read()
    except SourceError as exc:
        ok = False
        print(f"  FAIL — {exc}")
        print("  fix: confirm you're on the house LAN (not a full-tunnel VPN — "
              "check `route -n get default`), and that the router at "
              f"{cfg.router_host or '<auto-detected gateway>'} is powered on.")
    else:
        print(f"  OK — {source.describe()}, location={source.location}")
        print(f"       bytes_recv={reading.bytes_recv} bytes_sent={reading.bytes_sent} "
              f"uptime_s={reading.uptime_s} pkts={reading.pkts_recv},{reading.pkts_sent}")
        identity = getattr(source, "identity", None)
        if identity is not None:
            print(f"       identity: {identity.summary()}")

    print("== Database ==")
    db_path = Path(os.path.expanduser(cfg.db_path))
    if not db_path.parent.exists():
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            ok = False
            print(f"  FAIL — cannot create {db_path.parent}: {exc}")
    if db_path.exists():
        writable = os.access(db_path, os.W_OK)
        if writable:
            print(f"  OK — {db_path} exists and is writable")
        else:
            ok = False
            print(f"  FAIL — {db_path} exists but is not writable")
            print(f"  fix: chmod u+w {db_path}")
    elif _is_locked():
        print(f"  SKIP — {db_path} does not exist yet, and a housewatch instance "
              "currently holds the write lock (cannot safely double-check without "
              "racing it); it will create the db on its next poll")
    else:
        try:
            probe_store = Store(db_path, readonly=False)
            probe_store.init_schema()
            probe_store.close()
            print(f"  OK — created and initialized {db_path}")
        except OSError as exc:
            ok = False
            print(f"  FAIL — cannot create/write {db_path}: {exc}")
            print(f"  fix: check permissions on {db_path.parent}")

    print("== Packet recovery ==")
    try:
        with Store(db_path, readonly=True) as st:
            disabled = st.get_meta(collector_mod.PACKET_CALIBRATION_META_KEY) == "1"
    except Exception:
        disabled = False
    if disabled:
        print("  OFF — this router's byte and packet counters were measured "
              "using different accounting units,")
        print("        so the packet-envelope recovery tier is disabled. Long gaps "
              "classify as suspect/gap")
        print("        instead of recovered. See the 'calibration' row in `events`.")
    else:
        print("  ON — no counter-unit disagreement observed")

    print("== LaunchAgent ==")
    try:
        result = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            print(f"  OK — {LAUNCHD_LABEL} is loaded")
        else:
            ok = False
            print(f"  FAIL — {LAUNCHD_LABEL} is not loaded")
            plist = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
            if plist.exists():
                print(f"  fix: launchctl bootstrap gui/{os.getuid()} {plist}")
            else:
                print("  fix: run ./install.sh from the housewatch checkout")
                print(f"       (renders com.housewatch.collector.plist -> {plist}")
                print(f"        with {sys.executable} and bootstraps it)")
            print("  (not needed to use housewatch — `python -m housewatch run` "
                  "in a terminal works; the agent only makes it survive logout.)")
    except (OSError, subprocess.TimeoutExpired) as exc:
        ok = False
        print(f"  FAIL — could not query launchctl: {exc}")

    print()
    print("all checks passed" if ok else "one or more checks FAILED — see fixes above")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def _gb_type(raw: str) -> float:
    """argparse type= for `config set-cap`'s positional GB argument."""
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"'{raw}' is not a number of GB — give the cap in decimal GB "
            "(1 GB = 1e9 bytes), e.g. 1000 for a 1 TB plan."
        )
    if math.isnan(value) or math.isinf(value):
        raise argparse.ArgumentTypeError(
            f"'{raw}' is not a number of GB — give the cap in decimal GB "
            "(1 GB = 1e9 bytes), e.g. 1000 for a 1 TB plan."
        )
    if value <= 0:
        raise argparse.ArgumentTypeError(
            "the cap must be greater than 0. To remove the cap entirely: "
            "python -m housewatch config clear-cap"
        )
    return value


def _cycle_day_type(raw: str) -> int:
    """argparse type= for `config set-cycle-day`'s positional day argument."""
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{raw}' is not a whole number.")
    if not (1 <= value <= config_mod.MAX_BILLING_CYCLE_START_DAY):
        raise argparse.ArgumentTypeError(
            f"{value} is outside 1-{config_mod.MAX_BILLING_CYCLE_START_DAY}. "
            "Days 29-31 do not exist in every month, so the cycle boundary "
            "would silently shift in short months. If your ISP resets on "
            f"the 29th-31st, use {config_mod.MAX_BILLING_CYCLE_START_DAY} — "
            "consistently a few days early beats silently different per month."
        )
    return value


def cmd_config_show(args: argparse.Namespace) -> int:
    cfg = _load_config_or_die()
    try:
        overrides = _config_read_overrides()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    target = config_mod.DEFAULT_PATH
    exists = target.exists()
    print(f"config file: {target} "
          f"({'exists' if exists else 'does not exist — using built-in defaults'})")
    for f in fields(Config):
        key = f.name
        rendered = _render_field_value(cfg, key)
        source = "file" if key in overrides else "default"
        print(f"{key}  {rendered}  {source}")

    window_s = counters.certainty_window_s(cfg.max_link_mbps * 1e6 / 8)
    print(f"wrap-certainty window: {window_s:.1f}s at max_link_mbps={cfg.max_link_mbps:g}")
    return 0


def cmd_config_set_cap(args: argparse.Namespace) -> int:
    value: float = args.gb

    def mutate(raw: dict[str, object]) -> None:
        raw["monthly_cap_gb"] = value

    outcome = _apply_config_mutation(mutate)
    if outcome is None:
        return 1
    candidate, _raw = outcome

    print(f"monthly_cap_gb = {value:g} -> {config_mod.DEFAULT_PATH}")
    day = candidate.billing_cycle_start_day
    calendar_note = " (calendar month)" if day == 1 else ""
    print(f"billing cycle: starts day {day}{calendar_note} — change with: "
          "python -m housewatch config set-cycle-day <1-28>")
    print("status picks this up immediately; an already-running dashboard "
          "or menubar shows the old cap until restarted.")
    print("note: comments in config.toml are not preserved by config set-* "
          "(documented copy: config.example.toml)")
    return 0


def cmd_config_clear_cap(args: argparse.Namespace) -> int:
    try:
        raw = _config_read_overrides()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    if "monthly_cap_gb" not in raw:
        print("no cap was set — nothing to change")
        return 0

    def mutate(raw: dict[str, object]) -> None:
        del raw["monthly_cap_gb"]

    outcome = _apply_config_mutation(mutate)
    if outcome is None:
        return 1

    print(f"monthly_cap_gb cleared -> {config_mod.DEFAULT_PATH}")
    print("note: comments in config.toml are not preserved by config set-* "
          "(documented copy: config.example.toml)")
    return 0


def cmd_config_set_cycle_day(args: argparse.Namespace) -> int:
    day: int = args.day

    def mutate(raw: dict[str, object]) -> None:
        raw["billing_cycle_start_day"] = day

    outcome = _apply_config_mutation(mutate)
    if outcome is None:
        return 1

    calendar_note = " (calendar month)" if day == 1 else ""
    print(f"billing_cycle_start_day = {day}{calendar_note} -> {config_mod.DEFAULT_PATH}")
    print("note: comments in config.toml are not preserved by config set-* "
          "(documented copy: config.example.toml)")
    return 0


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m housewatch", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run the daemon loop (what the LaunchAgent invokes)")
    p_run.set_defaults(func=cmd_run)

    p_once = sub.add_parser("once", help="one real poll, persisted, outcome printed")
    p_once.set_defaults(func=cmd_once)

    p_status = sub.add_parser("status", help="today's usage, cycle usage vs cap, last sample age")
    p_status.set_defaults(func=cmd_status)

    p_serve = sub.add_parser("serve", help="start the local web dashboard")
    p_serve.set_defaults(func=cmd_serve)

    p_doctor = sub.add_parser("doctor", help="diagnose router/db/LaunchAgent problems")
    p_doctor.set_defaults(func=cmd_doctor)

    p_config = sub.add_parser(
        "config", help="show or change the persisted config (cap, billing day)"
    )
    config_sub = p_config.add_subparsers(dest="config_command", required=True)

    p_config_show = config_sub.add_parser(
        "show", help="print every config value and where it came from"
    )
    p_config_show.set_defaults(func=cmd_config_show)

    p_config_set_cap = config_sub.add_parser(
        "set-cap", help="set the monthly data cap, in decimal GB"
    )
    p_config_set_cap.add_argument(
        "gb", type=_gb_type, help="cap in decimal GB (1 GB = 1e9 bytes)"
    )
    p_config_set_cap.set_defaults(func=cmd_config_set_cap)

    p_config_clear_cap = config_sub.add_parser(
        "clear-cap", help="remove the configured data cap (usage keeps being tracked)"
    )
    p_config_clear_cap.set_defaults(func=cmd_config_clear_cap)

    p_config_set_cycle_day = config_sub.add_parser(
        "set-cycle-day", help="set the billing cycle's start day of month (1-28)"
    )
    p_config_set_cycle_day.add_argument(
        "day", type=_cycle_day_type, help="day of month the billing cycle starts (1-28)"
    )
    p_config_set_cycle_day.set_defaults(func=cmd_config_set_cycle_day)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
