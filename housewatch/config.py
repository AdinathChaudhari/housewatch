"""Config load/save/validate for housewatch.

Format is TOML (stdlib ``tomllib`` for reading; this module hand-writes TOML
on save since the standard library ships no TOML writer). A missing file is
not an error — it yields pure defaults so the tool runs with zero setup.

The wrap-safety rule is the one number in this file that is load-bearing for
correctness elsewhere: ``counters.py``'s single-wrap correction is only valid
when the true delta between two successful polls is under one modulus
(2**32 bytes). See ``certainty_window_s`` in ``counters.py`` for the same
constant derived the other way.
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, fields, replace
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ~/.config/housewatch/config.toml — an XDG-style dotfile location, kept
# out of the git-tracked checkout so a real per-machine config never gets
# committed by accident (see .gitignore).
DEFAULT_PATH: Path = Path.home() / ".config" / "housewatch" / "config.toml"

# Fixed log directory for the rotating daemon log. Not a config key — the
# spec's configKeys list has no log_path field, only log_level. Exposed here
# for other modules/docs to reference by one name; creating it belongs to
# whoever actually opens a log file, which today is cli._setup_logging (so
# `run`/`once` make it on demand), not something load() does on every call —
# a config load (including read-only ones like `status`/`doctor`, and every
# test) should not have the side effect of writing to the real filesystem
# outside its own db path.
LOG_DIR: Path = Path.home() / "Library" / "Logs" / "housewatch"

_VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})

# All months have at least 28 days; a start day of 29, 30, or 31 does not
# exist in every month. The spec's billing.py clamps such a day down to the
# last day of a short month (31 -> Feb 28/29), which is a legitimate way to
# handle it — but that clamp means the *effective* cycle start silently
# shifts in Feb/Apr/Jun/Sep/Nov, which is a surprising thing for a billing
# boundary to do without the user asking for it. Capping the config value at
# 28 keeps "day N" meaning the same calendar day in every month, so what the
# user configures is what they get, with no silent per-month shift. 28 (not
# some other floor) is the largest value common to every month, so it is the
# widest range that still guarantees this.
MAX_BILLING_CYCLE_START_DAY = 28

# The lowest max_link_mbps that is safe WITHOUT an explicit opt-in.
#
# max_link_mbps is not a display value and it is not the ISP plan speed: it
# is the physical ceiling counters.py uses to prove how many counter wraps
# COULD have happened in a gap. Lowering it does not make the tool more
# conservative -- it does the opposite. It WIDENS the certainty window
# (2**32 / bytes_per_sec), so intervals that should be flagged 'gap' get
# classified 'ok' with a delta that is exactly one modulus (4.295 GB) short,
# labelled CERTAIN. Measured: 1000 -> 34.4s window, 200 -> 171.8s,
# 50 -> 687.2s, 5 -> 6871.9s.
#
# The old validate() only bounded poll_interval_s from ABOVE by a function
# of max_link_mbps, so lowering max_link_mbps always passed validation --
# the dangerous direction could never trip the check. Hence this floor.
# 1000 Mbps is a common GbE WAN port ceiling, not a guess at any one
# specific device -- it's a floor for confidence, not a device fingerprint.
MIN_SAFE_LINK_MBPS = 1000.0

logger = logging.getLogger(__name__)


class ConfigError(ValueError):
    """Raised for a missing/invalid config file or an out-of-range value.

    Always carries an actionable message — never surfaces as a bare
    KeyError/TypeError from a malformed TOML table.
    """


@dataclass(frozen=True)
class Config:
    """All housewatch settings. Fields/defaults/types match spec configKeys.

    Every value is independently overridable in the TOML file; keys absent
    from the file keep these defaults. An unknown key in the file is a load
    error (ConfigError), not a silently-ignored typo.
    """

    poll_interval_s: float = 10.0
    router_host: str | None = None
    max_link_mbps: float = 1000.0
    # Explicit opt-in required to set max_link_mbps below MIN_SAFE_LINK_MBPS.
    # Not a preference: it is an acknowledgement that a lower planning rate
    # makes wrap corrections unprovable rather than safer. See
    # MIN_SAFE_LINK_MBPS's comment for the measured consequences.
    i_know_my_link_is_slower: bool = False
    monthly_cap_gb: float | None = None
    billing_cycle_start_day: int = 1
    timezone: str = "local"
    db_path: str = "~/Library/Application Support/housewatch/housewatch.db"
    web_enabled: bool = True
    web_host: str = "127.0.0.1"
    web_port: int = 8321
    http_timeout_s: float = 5.0
    ssdp_timeout_s: float = 3.0
    retention_samples_days: int = 7
    retention_intervals_days: int = 90
    retention_events_days: int = 90
    log_level: str = "INFO"


_DEFAULTS: Config = Config()
_FIELD_NAMES: frozenset[str] = frozenset(f.name for f in fields(Config))


def defaults() -> Config:
    """The frozen defaults, exposed as one source of truth for tests/docs."""
    return _DEFAULTS


def _expand(path_str: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(path_str)))


def _read_raw_table(target: Path) -> dict[str, object]:
    """Read+parse ``target`` as a TOML table, or ``{}`` if it doesn't exist.

    Shared by ``load()`` and ``read_overrides()`` so their error messages
    and unknown-key check cannot drift apart -- there used to be exactly
    one copy of this logic (inlined in ``load()``); factoring it out is
    what makes the two callers' behavior provably identical rather than
    "written to match, once".
    """
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

    unknown = set(raw) - _FIELD_NAMES
    if unknown:
        raise ConfigError(
            f"{target}: unknown config key(s) {sorted(unknown)}. "
            f"Valid keys: {sorted(_FIELD_NAMES)}."
        )
    return raw


def read_overrides(path: Path | None = None) -> dict[str, object]:
    """Return the raw key/value overrides set in ``path``'s TOML file.

    ``{}`` for a missing file. Raises ConfigError on malformed TOML or an
    unknown key, via the same ``_read_raw_table`` helper ``load()`` uses --
    so the two cannot silently drift apart. Unlike ``load()``, this does
    NOT validate or build a ``Config``; it hands back the raw table so a
    caller (typically a CLI write command) can read the current on-disk
    overrides, mutate the one key it cares about, and write the whole
    table back with ``write_overrides()``.
    """
    target = path if path is not None else DEFAULT_PATH
    return _read_raw_table(target)


def write_overrides(overrides: dict[str, object], path: Path | None = None) -> None:
    """Atomically write ``overrides`` as the config file's full key set.

    This is a full replace, not a merge with whatever is already on disk:
    every key present in ``overrides`` is written, and every key absent
    from it is simply not in the file afterwards (so its default applies,
    letting a future default change still reach this user). Callers that
    want to change one key while preserving the rest read the current
    table with ``read_overrides()`` first, mutate that dict, and pass the
    whole thing back here -- so an unrelated key a user pinned to a
    non-default value survives untouched.

    ``None`` has no TOML representation, so a ``None`` value anywhere in
    ``overrides`` is a caller bug (to unset/clear a key, remove it from the
    dict instead of setting it to ``None``) and raises ConfigError, same as
    an unknown key. Written atomically (tmp file + ``os.replace``, same
    pattern as ``save()``) so a crash mid-write never leaves a truncated or
    corrupt config. The file carries a fixed header noting that hand-edited
    comments do not survive the next write -- the stdlib has no
    round-tripping TOML writer, and regex line-surgery on TOML is how
    configs get corrupted, so this deliberately does neither.
    """
    target = path if path is not None else DEFAULT_PATH

    unknown = set(overrides) - _FIELD_NAMES
    if unknown:
        raise ConfigError(
            f"cannot write unknown config key(s) {sorted(unknown)}. "
            f"Valid keys: {sorted(_FIELD_NAMES)}."
        )
    none_keys = sorted(k for k, v in overrides.items() if v is None)
    if none_keys:
        raise ConfigError(
            f"cannot write None for config key(s) {none_keys} -- TOML has no "
            "null; remove the key from the dict to clear it instead of "
            "setting it to None."
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# housewatch config — written by `housewatch config`.",
        "# Hand edits are welcome, but comments are NOT preserved across the",
        "# next `housewatch config set-*`/`clear-*` write (the standard",
        "# library ships no round-tripping TOML writer). Full key",
        "# documentation lives in config.example.toml.",
        "",
    ]
    for key, value in overrides.items():
        rendered = _toml_scalar(value)
        assert rendered is not None  # the none_keys check above already ruled this out
        lines.append(f"{key} = {rendered}")
    content = "\n".join(lines) + "\n"

    tmp_path = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, target)
    finally:
        tmp_path.unlink(missing_ok=True)


def load(path: Path | None = None) -> Config:
    """Load config from ``path`` (default: DEFAULT_PATH).

    An absent file yields pure ``Config()`` defaults — zero setup required.
    Unknown keys raise ConfigError. Always calls ``validate()`` before
    returning, and resolves+creates the db path's parent directory as a
    side effect (store.py can then just open the db file). Does NOT create
    LOG_DIR — that belongs to cli._setup_logging (see the LOG_DIR comment
    above); a config load must not have side effects beyond the one
    directory it actually owns.
    """
    target = path if path is not None else DEFAULT_PATH
    raw = _read_raw_table(target)

    try:
        cfg = replace(Config(), **raw)
    except TypeError as exc:
        raise ConfigError(f"{target}: malformed config table ({exc}).") from exc

    validate(cfg)

    if cfg.max_link_mbps < MIN_SAFE_LINK_MBPS:
        # Only reachable with i_know_my_link_is_slower=true (validate rejects
        # it otherwise). A widened certainty window must never be invisible:
        # this is the line `doctor` and the dashboard's health banner echo.
        logger.warning(
            "max_link_mbps=%.6g is below %.6g: the wrap-certainty window is "
            "%.1fs instead of %.1fs, so byte deltas are proven over a much "
            "longer gap than the hardware justifies. Raise it unless the WAN "
            "port really is that slow.",
            cfg.max_link_mbps,
            MIN_SAFE_LINK_MBPS,
            (2**32 * 8) / (cfg.max_link_mbps * 1e6),
            (2**32 * 8) / (MIN_SAFE_LINK_MBPS * 1e6),
        )

    # Resolve + create the db's parent dir so store.py can open the db file
    # without its own mkdir dance.
    try:
        _expand(cfg.db_path).parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(
            f"could not create db directory for db_path={cfg.db_path!r}: {exc}"
        ) from exc

    return cfg


def _toml_scalar(value: object) -> str | None:
    """Render one Python scalar as a TOML value, or None if it is absent.

    ``None`` has no TOML representation, so callers of this helper comment
    the whole key out rather than emit a bare ``key =``.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    raise ConfigError(f"cannot serialize value of type {type(value)!r} to TOML")


def save(cfg: Config, path: Path | None = None) -> None:
    """Write ``cfg`` to ``path`` (default: DEFAULT_PATH), atomically.

    Writes a tmp file in the same directory then ``os.replace``s it into
    place, so a crash mid-write never leaves a truncated/corrupt config.
    Creates parent dirs. A None-valued field is written as a commented-out
    line (TOML has no null) so the file still documents every key.
    """
    target = path if path is not None else DEFAULT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)

    lines = ["# housewatch config — written by housewatch.config.save()", ""]
    for f in fields(cfg):
        value = getattr(cfg, f.name)
        rendered = _toml_scalar(value)
        if rendered is None:
            lines.append(f"# {f.name} = ")
        else:
            lines.append(f"{f.name} = {rendered}")
    content = "\n".join(lines) + "\n"

    tmp_path = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, target)
    finally:
        tmp_path.unlink(missing_ok=True)


def validate(cfg: Config) -> None:
    """Raise ConfigError on any out-of-range/nonsensical value.

    The poll-interval bound is the wrap-safety inequality from the spec:
    at the configured planning rate (``max_link_mbps``), a poll gap beyond
    this bound risks two-plus counter wraps landing inside one interval,
    which counters.py cannot distinguish from a normal increase (a silent
    undercount, not just a flagged one). ``/3`` builds in margin for two
    consecutive missed polls before the certainty window is threatened.
    """
    errors: list[str] = []

    if cfg.max_link_mbps <= 0:
        errors.append(f"max_link_mbps must be > 0, got {cfg.max_link_mbps!r}")
        max_bound = None
    else:
        max_bound = (2**32 * 8) / (cfg.max_link_mbps * 1e6) / 3

    # The floor. max_link_mbps is used as a DENOMINATOR for the poll-interval
    # bound above, so that bound can only ever catch max_link_mbps being too
    # HIGH -- lowering it always validates while silently widening the window
    # in which counters.py calls a wrap-corrected delta "certain".
    if 0 < cfg.max_link_mbps < MIN_SAFE_LINK_MBPS and not cfg.i_know_my_link_is_slower:
        window_s = (2**32 * 8) / (cfg.max_link_mbps * 1e6)
        safe_window_s = (2**32 * 8) / (MIN_SAFE_LINK_MBPS * 1e6)
        errors.append(
            f"max_link_mbps={cfg.max_link_mbps!r} is below {MIN_SAFE_LINK_MBPS:g} "
            f"Mbps. This is NOT your ISP plan speed -- it is the physical "
            f"ceiling used to PROVE how many 4.295 GB counter wraps could have "
            f"happened between two polls. Lowering it does not add safety "
            f"margin, it removes proof: the certainty window widens from "
            f"{safe_window_s:.1f}s to {window_s:.1f}s, so a gap that should be "
            f"flagged as unrecoverable gets recorded as a certain number that "
            f"is exactly 4,294,967,296 bytes per direction too low. Set it to "
            f"the WAN port's physical ceiling (1000 for a GbE port). If the "
            f"link really is slower at the physical layer (e.g. a 100 Mbit "
            f"port), also set i_know_my_link_is_slower = true to acknowledge "
            f"the wider window."
        )

    if cfg.poll_interval_s < 2:
        errors.append(f"poll_interval_s must be >= 2, got {cfg.poll_interval_s!r}")
    if max_bound is not None and cfg.poll_interval_s > max_bound:
        errors.append(
            f"poll_interval_s={cfg.poll_interval_s!r} exceeds the wrap-safety "
            f"bound of {max_bound:.2f}s at max_link_mbps={cfg.max_link_mbps!r} "
            "(bound = 2**32*8 / (max_link_mbps*1e6) / 3). A slower poll risks "
            "two-plus silent counter wraps landing inside one interval — "
            "lower poll_interval_s. (Raising max_link_mbps also widens this "
            "bound, but only do that if the WAN port is genuinely faster; "
            "see MIN_SAFE_LINK_MBPS for why the value is a physical ceiling, "
            "not a dial.)"
        )

    if not (1 <= cfg.billing_cycle_start_day <= MAX_BILLING_CYCLE_START_DAY):
        errors.append(
            f"billing_cycle_start_day must be between 1 and "
            f"{MAX_BILLING_CYCLE_START_DAY}, got {cfg.billing_cycle_start_day!r} "
            f"(days 29-31 don't exist in every month; see config.py's "
            "MAX_BILLING_CYCLE_START_DAY comment for why 28 rather than 31)"
        )

    if cfg.monthly_cap_gb is not None and cfg.monthly_cap_gb <= 0:
        errors.append(
            f"monthly_cap_gb must be None (no cap) or > 0, got {cfg.monthly_cap_gb!r}"
        )

    if cfg.timezone != "local":
        try:
            ZoneInfo(cfg.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            errors.append(
                f"timezone={cfg.timezone!r} is not 'local' and not a resolvable "
                f"IANA zone name: {exc}"
            )

    if not (1024 <= cfg.web_port <= 65535):
        errors.append(f"web_port must be between 1024 and 65535, got {cfg.web_port!r}")

    if cfg.http_timeout_s <= 0:
        errors.append(f"http_timeout_s must be > 0, got {cfg.http_timeout_s!r}")
    if cfg.ssdp_timeout_s <= 0:
        errors.append(f"ssdp_timeout_s must be > 0, got {cfg.ssdp_timeout_s!r}")

    if cfg.retention_samples_days < 1:
        errors.append(
            f"retention_samples_days must be >= 1, got {cfg.retention_samples_days!r}"
        )
    if cfg.retention_intervals_days < 45:
        errors.append(
            f"retention_intervals_days must be >= 45 (must cover a full billing "
            f"cycle plus the 30-day view), got {cfg.retention_intervals_days!r}"
        )
    if cfg.retention_events_days < 1:
        errors.append(
            f"retention_events_days must be >= 1, got {cfg.retention_events_days!r}"
        )

    if cfg.log_level not in _VALID_LOG_LEVELS:
        errors.append(
            f"log_level must be one of {sorted(_VALID_LOG_LEVELS)}, "
            f"got {cfg.log_level!r}"
        )

    if cfg.router_host is not None and not cfg.router_host.strip():
        errors.append("router_host must be None or a non-empty host/IP string")

    if not cfg.web_host.strip():
        errors.append("web_host must be a non-empty string")

    if not cfg.db_path.strip():
        errors.append("db_path must be a non-empty string")

    if errors:
        joined = "\n  - ".join(errors)
        raise ConfigError(f"invalid housewatch config:\n  - {joined}")


def system_zone_name() -> str | None:
    """The host's IANA zone name, or None if it cannot be determined.

    A thin re-export of ``housewatch.detect.system_timezone``, imported
    lazily so this module keeps working standalone if ``detect.py`` isn't
    present in a given checkout yet; every other caller in this codebase
    should prefer importing ``system_timezone`` from ``housewatch.detect``
    directly. Kept here under its original name too, since ``tzinfo_of``
    below and existing tests already call it as ``config.system_zone_name``.

    The fallback implementation (used only when ``housewatch.detect``
    cannot be imported) checks ``$TZ`` first, then the ``/etc/localtime``
    symlink (macOS points it into ``/var/db/timezone/zoneinfo/...``, Linux
    into ``/usr/share/zoneinfo/...``) and takes the path suffix after the
    ``zoneinfo`` component. Returns None rather than guessing, exactly like
    ``housewatch.detect.system_timezone`` does.
    """
    try:
        from housewatch.detect import system_timezone
    except ImportError:
        return _system_zone_name_fallback()
    return system_timezone()


def _system_zone_name_fallback() -> str | None:
    tz_env = os.environ.get("TZ", "").strip()
    if tz_env:
        try:
            ZoneInfo(tz_env)
        except (ZoneInfoNotFoundError, ValueError):
            pass
        else:
            return tz_env

    localtime = Path("/etc/localtime")
    try:
        resolved = localtime.resolve()
    except OSError:
        return None
    parts = resolved.parts
    if "zoneinfo" not in parts:
        return None
    name = "/".join(parts[parts.index("zoneinfo") + 1 :])
    if not name:
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return name


def tzinfo_of(cfg: Config):
    """Resolve cfg.timezone to a tzinfo. 'local' -> the system's local zone.

    'local' resolves to a real ``ZoneInfo``, NOT to
    ``datetime.now().astimezone().tzinfo``. That expression returns a FIXED
    offset sampled at call time, which silently breaks every day bucket, day
    boundary and billing-cycle boundary for half the year on any DST host --
    and ``days.day`` strings are written permanently at poll time, so a
    sample filed under the wrong calendar date stays wrong. billing.py's
    contract ("a day that crosses a DST transition correctly comes out 23 or
    25 hours long") only holds with a real zone.

    The fixed-offset fallback remains for the case where the system zone
    genuinely cannot be identified; it logs a WARNING when it is used, so a
    degraded resolution is never invisible.
    """
    import datetime as _dt

    if cfg.timezone != "local":
        return ZoneInfo(cfg.timezone)

    name = system_zone_name()
    if name is not None:
        return ZoneInfo(name)
    logger.warning(
        "could not determine the system IANA timezone; falling back to a FIXED "
        "UTC offset. Day boundaries will be wrong across any DST transition. "
        "Set timezone = \"Area/City\" in config.toml to fix this."
    )
    return _dt.datetime.now().astimezone().tzinfo
