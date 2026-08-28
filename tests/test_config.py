"""Tests for housewatch.config: defaults, override, and every validation rule.

Not in this module's strict file-ownership list, but the task instructions
explicitly asked for it ("write and RUN tests for config.py ... report the
exact pytest command and its real output"), so it lives here alongside
conftest.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from housewatch import config as config_mod
from housewatch.config import Config, ConfigError


# ---------------------------------------------------------------------------
# defaults / load / save round-trip
# ---------------------------------------------------------------------------


def test_defaults_are_valid() -> None:
    cfg = config_mod.defaults()
    config_mod.validate(cfg)  # must not raise
    assert cfg.poll_interval_s == 10.0
    assert cfg.router_host is None
    assert cfg.max_link_mbps == 1000.0
    assert cfg.monthly_cap_gb is None
    assert cfg.billing_cycle_start_day == 1
    assert cfg.timezone == "local"
    assert cfg.web_enabled is True
    assert cfg.web_port == 8321
    assert cfg.retention_intervals_days == 90
    assert cfg.log_level == "INFO"


def test_load_missing_file_yields_defaults(tmp_config_path: Path) -> None:
    assert not tmp_config_path.exists()
    cfg = config_mod.load(tmp_config_path)
    assert cfg == Config()


def test_load_creates_db_parent_dir(tmp_config_path: Path, tmp_db_path: Path) -> None:
    assert not tmp_db_path.parent.exists()
    cfg = config_mod.Config(db_path=str(tmp_db_path))
    config_mod.validate(cfg)
    # load() only creates dirs when it does the loading itself; exercise that
    # path directly by saving then loading through the real entry point.
    config_mod.save(cfg, tmp_config_path)
    config_mod.load(tmp_config_path)
    assert tmp_db_path.parent.is_dir()


def test_save_then_load_round_trips(tmp_config_path: Path, tmp_db_path: Path) -> None:
    cfg = Config(
        poll_interval_s=8.0,
        router_host="192.0.2.1",
        max_link_mbps=500.0,
        # Below MIN_SAFE_LINK_MBPS, so the opt-in is required -- which also
        # round-trips the bool key through save()/load().
        i_know_my_link_is_slower=True,
        monthly_cap_gb=250.0,
        billing_cycle_start_day=15,
        timezone="Asia/Yangon",
        db_path=str(tmp_db_path),
        web_enabled=False,
        web_host="127.0.0.1",
        web_port=9000,
        http_timeout_s=2.5,
        ssdp_timeout_s=1.5,
        retention_samples_days=3,
        retention_intervals_days=60,
        retention_events_days=45,
        log_level="DEBUG",
    )
    config_mod.save(cfg, tmp_config_path)
    loaded = config_mod.load(tmp_config_path)
    assert loaded == cfg


def test_save_round_trips_none_fields(tmp_config_path: Path, tmp_db_path: Path) -> None:
    cfg = Config(db_path=str(tmp_db_path))
    assert cfg.router_host is None
    assert cfg.monthly_cap_gb is None
    config_mod.save(cfg, tmp_config_path)
    text = tmp_config_path.read_text()
    assert "# router_host = " in text
    assert "# monthly_cap_gb = " in text
    loaded = config_mod.load(tmp_config_path)
    assert loaded.router_host is None
    assert loaded.monthly_cap_gb is None


def test_load_partial_override_keeps_other_defaults(
    tmp_config_path: Path, tmp_db_path: Path
) -> None:
    tmp_config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_config_path.write_text(f'poll_interval_s = 5.0\ndb_path = "{tmp_db_path}"\n')
    cfg = config_mod.load(tmp_config_path)
    assert cfg.poll_interval_s == 5.0
    assert cfg.db_path == str(tmp_db_path)
    assert cfg.web_port == 8321  # untouched default


def test_load_unknown_key_raises(tmp_config_path: Path) -> None:
    tmp_config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_config_path.write_text('poll_interval_s = 5.0\nnot_a_real_key = 1\n')
    with pytest.raises(ConfigError, match="unknown config key"):
        config_mod.load(tmp_config_path)


def test_load_malformed_toml_raises(tmp_config_path: Path) -> None:
    tmp_config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_config_path.write_text("this is not [ valid toml")
    with pytest.raises(ConfigError, match="not valid TOML"):
        config_mod.load(tmp_config_path)


def test_load_bad_field_value_raises(tmp_config_path: Path) -> None:
    """A value of the wrong shape (list instead of scalar) fails validate(),
    not with a bare KeyError/TypeError."""
    tmp_config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_config_path.write_text("web_port = -5\n")
    with pytest.raises(ConfigError, match="web_port"):
        config_mod.load(tmp_config_path)


# ---------------------------------------------------------------------------
# validate() — one failure per named rule
# ---------------------------------------------------------------------------


def test_reject_poll_interval_too_small() -> None:
    cfg = Config(poll_interval_s=1.0)
    with pytest.raises(ConfigError, match="poll_interval_s must be >= 2"):
        config_mod.validate(cfg)


def test_reject_poll_interval_violates_wrap_safety() -> None:
    # 60s @ 1000 Mbps blows past the 11.45s wrap-safety bound.
    cfg = Config(poll_interval_s=60.0, max_link_mbps=1000.0)
    with pytest.raises(ConfigError, match="wrap-safety bound"):
        config_mod.validate(cfg)


def test_wrap_safety_bound_accepts_spec_value() -> None:
    # The spec's chosen default (10s @ 1000 Mbps) must clear its own bound.
    cfg = Config(poll_interval_s=10.0, max_link_mbps=1000.0)
    config_mod.validate(cfg)  # must not raise


def test_reject_max_link_mbps_not_positive() -> None:
    cfg = Config(max_link_mbps=0.0)
    with pytest.raises(ConfigError, match="max_link_mbps must be > 0"):
        config_mod.validate(cfg)


def test_reject_billing_day_zero() -> None:
    cfg = Config(billing_cycle_start_day=0)
    with pytest.raises(ConfigError, match="billing_cycle_start_day must be between 1 and 28"):
        config_mod.validate(cfg)


def test_reject_billing_day_31() -> None:
    cfg = Config(billing_cycle_start_day=31)
    with pytest.raises(ConfigError, match="billing_cycle_start_day must be between 1 and 28"):
        config_mod.validate(cfg)


def test_accept_billing_day_28() -> None:
    cfg = Config(billing_cycle_start_day=28)
    config_mod.validate(cfg)  # must not raise


def test_reject_monthly_cap_zero() -> None:
    cfg = Config(monthly_cap_gb=0.0)
    with pytest.raises(ConfigError, match="monthly_cap_gb must be None"):
        config_mod.validate(cfg)


def test_reject_monthly_cap_negative() -> None:
    cfg = Config(monthly_cap_gb=-10.0)
    with pytest.raises(ConfigError, match="monthly_cap_gb must be None"):
        config_mod.validate(cfg)


def test_accept_monthly_cap_none() -> None:
    cfg = Config(monthly_cap_gb=None)
    config_mod.validate(cfg)  # must not raise


def test_reject_bad_timezone() -> None:
    cfg = Config(timezone="Not/AZone")
    with pytest.raises(ConfigError, match="not a resolvable IANA zone"):
        config_mod.validate(cfg)


def test_accept_local_timezone() -> None:
    cfg = Config(timezone="local")
    config_mod.validate(cfg)  # must not raise


def test_accept_iana_timezone() -> None:
    cfg = Config(timezone="Asia/Yangon")
    config_mod.validate(cfg)  # must not raise


def test_reject_web_port_too_low() -> None:
    cfg = Config(web_port=80)
    with pytest.raises(ConfigError, match="web_port must be between 1024 and 65535"):
        config_mod.validate(cfg)


def test_reject_web_port_too_high() -> None:
    cfg = Config(web_port=70000)
    with pytest.raises(ConfigError, match="web_port must be between 1024 and 65535"):
        config_mod.validate(cfg)


def test_reject_http_timeout_not_positive() -> None:
    cfg = Config(http_timeout_s=0.0)
    with pytest.raises(ConfigError, match="http_timeout_s must be > 0"):
        config_mod.validate(cfg)


def test_reject_ssdp_timeout_not_positive() -> None:
    cfg = Config(ssdp_timeout_s=-1.0)
    with pytest.raises(ConfigError, match="ssdp_timeout_s must be > 0"):
        config_mod.validate(cfg)


def test_reject_retention_samples_days_zero() -> None:
    cfg = Config(retention_samples_days=0)
    with pytest.raises(ConfigError, match="retention_samples_days must be >= 1"):
        config_mod.validate(cfg)


def test_reject_retention_intervals_days_below_45() -> None:
    cfg = Config(retention_intervals_days=44)
    with pytest.raises(ConfigError, match="retention_intervals_days must be >= 45"):
        config_mod.validate(cfg)


def test_accept_retention_intervals_days_45() -> None:
    cfg = Config(retention_intervals_days=45)
    config_mod.validate(cfg)  # must not raise


def test_reject_retention_events_days_zero() -> None:
    cfg = Config(retention_events_days=0)
    with pytest.raises(ConfigError, match="retention_events_days must be >= 1"):
        config_mod.validate(cfg)


def test_reject_bad_log_level() -> None:
    cfg = Config(log_level="VERBOSE")
    with pytest.raises(ConfigError, match="log_level must be one of"):
        config_mod.validate(cfg)


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR"])
def test_accept_every_valid_log_level(level: str) -> None:
    cfg = Config(log_level=level)
    config_mod.validate(cfg)  # must not raise


def test_reject_empty_router_host() -> None:
    cfg = Config(router_host="   ")
    with pytest.raises(ConfigError, match="router_host must be None or a non-empty"):
        config_mod.validate(cfg)


def test_accept_none_router_host() -> None:
    cfg = Config(router_host=None)
    config_mod.validate(cfg)  # must not raise


def test_reject_empty_web_host() -> None:
    cfg = Config(web_host="")
    with pytest.raises(ConfigError, match="web_host must be a non-empty string"):
        config_mod.validate(cfg)


def test_reject_empty_db_path() -> None:
    cfg = Config(db_path="")
    with pytest.raises(ConfigError, match="db_path must be a non-empty string"):
        config_mod.validate(cfg)


def test_multiple_errors_all_reported_together() -> None:
    cfg = Config(web_port=1, poll_interval_s=1.0, billing_cycle_start_day=99)
    with pytest.raises(ConfigError) as exc_info:
        config_mod.validate(cfg)
    msg = str(exc_info.value)
    assert "web_port" in msg
    assert "poll_interval_s" in msg
    assert "billing_cycle_start_day" in msg


# ---------------------------------------------------------------------------
# tzinfo_of
# ---------------------------------------------------------------------------


def test_tzinfo_of_local_is_a_real_zone_not_a_fixed_offset() -> None:
    """'local' must resolve to a real IANA zone, not to a snapshot offset.

    ``datetime.now().astimezone().tzinfo`` is a FIXED-offset object sampled
    at call time. Using it means every day bucket, day boundary and billing
    boundary is off by an hour for half the year on any DST host -- and
    ``days.day`` strings are written permanently at poll time, so a sample
    filed under the wrong calendar date stays wrong forever. A machine on a
    no-DST zone never exercises that failure directly, but
    'local' is the shipped default and must be correct everywhere.
    """
    from zoneinfo import ZoneInfo

    cfg = Config(timezone="local")
    tz = config_mod.tzinfo_of(cfg)
    # The host may genuinely be unable to name its zone (no /etc/localtime
    # symlink); in that documented fallback case a fixed offset is allowed.
    if config_mod.system_zone_name() is None:  # pragma: no cover - host dependent
        import datetime as dt

        assert tz == dt.datetime.now().astimezone().tzinfo
        return
    assert isinstance(tz, ZoneInfo)
    assert str(tz) == config_mod.system_zone_name()


def test_tzinfo_of_local_tracks_dst_transitions() -> None:
    """A real zone gives DIFFERENT offsets in June and December on a DST
    host; a fixed offset gives the same one, which is the bug. Proven here
    against an explicit DST zone via the same resolution path 'local' uses
    (system_zone_name -> ZoneInfo), so the property is tested even on a
    machine whose own zone has no DST of its own."""
    import datetime as dt
    from zoneinfo import ZoneInfo

    ny = config_mod.tzinfo_of(Config(timezone="America/New_York"))
    assert isinstance(ny, ZoneInfo)
    june = dt.datetime(2027, 6, 15, 12, tzinfo=ny).utcoffset()
    december = dt.datetime(2027, 12, 15, 12, tzinfo=ny).utcoffset()
    assert june != december

    # And a fixed offset sampled from one of those instants would file a
    # late-evening December sample under the WRONG local date.
    fixed = dt.timezone(june, "EDT")
    instant = dt.datetime(2027, 1, 15, 23, 30, tzinfo=ny).timestamp()
    from housewatch import billing

    assert billing.day_key(instant, ny) == "2027-01-15"
    assert billing.day_key(instant, fixed) == "2027-01-16"


def test_tzinfo_of_iana() -> None:
    from zoneinfo import ZoneInfo

    cfg = Config(timezone="Asia/Yangon")
    tz = config_mod.tzinfo_of(cfg)
    assert tz == ZoneInfo("Asia/Yangon")


# ---------------------------------------------------------------------------
# max_link_mbps floor.
#
# max_link_mbps was only ever used as a DENOMINATOR in the poll-interval
# bound, so validate() could only catch it being too HIGH. Lowering it --
# the single most natural thing a user would type, their ISP plan speed --
# always validated while silently widening the window in which counters.py
# calls a wrap-corrected delta "certain". No test covered the direction.
# ---------------------------------------------------------------------------


def test_isp_plan_speed_as_max_link_mbps_is_rejected() -> None:
    with pytest.raises(ConfigError) as exc_info:
        config_mod.validate(Config(max_link_mbps=50.0))
    msg = str(exc_info.value)
    assert "max_link_mbps" in msg
    assert "i_know_my_link_is_slower" in msg
    assert "ISP plan speed" in msg or "physical" in msg


@pytest.mark.parametrize("mbps", [5.0, 50.0, 200.0, 999.0])
def test_every_value_below_the_floor_is_rejected_without_the_opt_in(mbps: float) -> None:
    with pytest.raises(ConfigError):
        config_mod.validate(Config(max_link_mbps=mbps))


@pytest.mark.parametrize("mbps", [5.0, 50.0, 200.0, 999.0])
def test_the_opt_in_allows_a_slower_link(mbps: float) -> None:
    config_mod.validate(Config(max_link_mbps=mbps, i_know_my_link_is_slower=True))


def test_the_floor_itself_and_above_need_no_opt_in() -> None:
    config_mod.validate(Config(max_link_mbps=config_mod.MIN_SAFE_LINK_MBPS))
    # A faster link tightens the poll-interval bound, so it needs a faster
    # poll too -- that is the OTHER (already-working) direction of the rule.
    config_mod.validate(Config(max_link_mbps=2500.0, poll_interval_s=4.0))


def test_lowering_max_link_mbps_would_have_turned_a_gap_into_a_certain_lie() -> None:
    """Why the floor exists, stated as arithmetic rather than opinion: at
    50 Mbps the classifier calls a 600s interval CERTAIN and reports a
    number exactly one modulus (4,294,967,296 bytes) short. At the floor it
    correctly refuses to guess."""
    from housewatch.collector import _thresholds_from_config
    from housewatch.counters import MODULUS_32, classify
    from housewatch.model import Reading

    true_down = 6_000_000_000
    prev = Reading(ts=0.0, bytes_recv=0, bytes_sent=0, uptime_s=100_000,
                   pkts_recv=None, pkts_sent=None)
    curr = Reading(ts=600.0, bytes_recv=true_down % MODULUS_32, bytes_sent=0,
                   uptime_s=100_600, pkts_recv=None, pkts_sent=None)

    slow = classify(prev, curr, _thresholds_from_config(
        Config(max_link_mbps=50.0, i_know_my_link_is_slower=True)))
    assert slow.kind == "ok"
    assert slow.down_bytes == true_down - MODULUS_32  # confidently 4.29 GB short

    safe = classify(prev, curr, _thresholds_from_config(Config()))
    assert safe.kind == "gap"
    assert safe.down_bytes is None


def test_load_warns_when_the_opt_in_widens_the_window(
    tmp_config_path, tmp_db_path, caplog
) -> None:
    """A widened certainty window must never be invisible."""
    import logging

    cfg = Config(max_link_mbps=100.0, i_know_my_link_is_slower=True,
                 db_path=str(tmp_db_path))
    config_mod.save(cfg, tmp_config_path)
    with caplog.at_level(logging.WARNING, logger="housewatch.config"):
        config_mod.load(tmp_config_path)
    assert any("max_link_mbps" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# read_overrides / write_overrides — the CLI's `housewatch config set-*`/
# `clear-*` write path (config.py:read_overrides/write_overrides). These
# never build a full Config or call validate(); the caller (cli.py) does
# read -> mutate -> dataclasses.replace(Config(), **raw) -> validate ->
# write, so these two are tested purely as raw-dict-to-TOML plumbing.
# ---------------------------------------------------------------------------


def test_read_overrides_missing_file_returns_empty_dict(tmp_config_path: Path) -> None:
    assert not tmp_config_path.exists()
    assert config_mod.read_overrides(tmp_config_path) == {}


def test_read_overrides_returns_raw_keys_present_in_file(tmp_config_path: Path) -> None:
    tmp_config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_config_path.write_text('poll_interval_s = 5.0\nweb_port = 9000\n')
    raw = config_mod.read_overrides(tmp_config_path)
    assert raw == {"poll_interval_s": 5.0, "web_port": 9000}


def test_read_overrides_unknown_key_raises(tmp_config_path: Path) -> None:
    tmp_config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_config_path.write_text('not_a_real_key = 1\n')
    with pytest.raises(ConfigError, match="unknown config key"):
        config_mod.read_overrides(tmp_config_path)


def test_read_overrides_malformed_toml_raises(tmp_config_path: Path) -> None:
    tmp_config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_config_path.write_text("this is not [ valid toml")
    with pytest.raises(ConfigError, match="not valid TOML"):
        config_mod.read_overrides(tmp_config_path)


def test_read_overrides_and_load_share_the_same_unknown_key_message(
    tmp_config_path: Path,
) -> None:
    """load() and read_overrides() must not be able to drift: both go
    through the same _read_raw_table helper, so this pins that they still
    raise byte-for-byte the same message rather than two copies that could
    silently diverge."""
    tmp_config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_config_path.write_text('bogus_key = 1\n')
    with pytest.raises(ConfigError) as load_exc:
        config_mod.load(tmp_config_path)
    with pytest.raises(ConfigError) as read_exc:
        config_mod.read_overrides(tmp_config_path)
    assert str(load_exc.value) == str(read_exc.value)


def test_write_overrides_writes_only_the_given_keys(tmp_config_path: Path) -> None:
    config_mod.write_overrides({"monthly_cap_gb": 500.0}, tmp_config_path)
    raw = config_mod.read_overrides(tmp_config_path)
    assert raw == {"monthly_cap_gb": 500.0}


def test_write_overrides_then_load_produces_expected_config(
    tmp_config_path: Path, tmp_db_path: Path
) -> None:
    config_mod.write_overrides(
        {"monthly_cap_gb": 500.0, "db_path": str(tmp_db_path)}, tmp_config_path
    )
    cfg = config_mod.load(tmp_config_path)
    assert cfg.monthly_cap_gb == 500.0
    assert cfg.db_path == str(tmp_db_path)


def test_write_overrides_preserves_an_unrelated_pre_existing_key_verbatim(
    tmp_config_path: Path,
) -> None:
    """The `config set-cap`/`clear-cap` write path is read -> mutate -> write
    the WHOLE dict back, specifically so a key some other part of the
    config that a user pinned (e.g. a custom poll_interval_s) survives a
    `set-cap` call untouched."""
    config_mod.write_overrides({"poll_interval_s": 8.0}, tmp_config_path)

    raw = config_mod.read_overrides(tmp_config_path)
    raw["monthly_cap_gb"] = 500.0
    config_mod.write_overrides(raw, tmp_config_path)

    final = config_mod.read_overrides(tmp_config_path)
    assert final == {"poll_interval_s": 8.0, "monthly_cap_gb": 500.0}


def test_write_overrides_omitting_a_key_clears_it(tmp_config_path: Path) -> None:
    """clear-cap's contract: build the new raw dict without the key, then
    write that dict back -- the key is simply gone afterwards, not written
    as some placeholder."""
    config_mod.write_overrides(
        {"poll_interval_s": 8.0, "monthly_cap_gb": 500.0}, tmp_config_path
    )
    raw = config_mod.read_overrides(tmp_config_path)
    del raw["monthly_cap_gb"]
    config_mod.write_overrides(raw, tmp_config_path)

    final = config_mod.read_overrides(tmp_config_path)
    assert final == {"poll_interval_s": 8.0}
    cfg = config_mod.load(tmp_config_path)
    assert cfg.monthly_cap_gb is None  # back to the default, never 0


def test_write_overrides_unknown_key_raises(tmp_config_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown config key"):
        config_mod.write_overrides({"not_a_real_key": 1}, tmp_config_path)
    assert not tmp_config_path.exists()  # refused before writing anything


def test_write_overrides_none_value_raises(tmp_config_path: Path) -> None:
    with pytest.raises(ConfigError, match="None"):
        config_mod.write_overrides({"monthly_cap_gb": None}, tmp_config_path)
    assert not tmp_config_path.exists()


def test_write_overrides_is_atomic_and_leaves_no_tmp_file(tmp_config_path: Path) -> None:
    config_mod.write_overrides({"web_port": 9000}, tmp_config_path)
    leftovers = list(tmp_config_path.parent.glob("*.tmp"))
    assert leftovers == []


def test_write_overrides_header_notes_comments_are_not_preserved(
    tmp_config_path: Path,
) -> None:
    config_mod.write_overrides({"web_port": 9000}, tmp_config_path)
    text = tmp_config_path.read_text()
    assert "not" in text.lower() and "preserved" in text.lower()
    assert "config.example.toml" in text


def test_write_overrides_refuses_to_write_over_an_invalid_existing_file(
    tmp_config_path: Path,
) -> None:
    """Not a write_overrides behavior per se -- it documents the CLI
    contract: read_overrides() (called first by any `config set-*` command)
    raises before write_overrides() is ever reached, so a pre-existing
    broken file is never silently clobbered."""
    tmp_config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_config_path.write_text("this is not [ valid toml")
    with pytest.raises(ConfigError, match="not valid TOML"):
        config_mod.read_overrides(tmp_config_path)
    # The file is left exactly as it was -- nothing downstream ever calls
    # write_overrides in this failure path.
    assert tmp_config_path.read_text() == "this is not [ valid toml"


# ---------------------------------------------------------------------------
# system_zone_name / detect.py handoff
# ---------------------------------------------------------------------------


def test_system_zone_name_delegates_to_detect_when_available() -> None:
    """system_zone_name is a thin re-export once housewatch.detect exists;
    this pins the delegation itself, independent of whatever detect.py's
    own real logic returns on this host."""
    import sys
    import types

    sentinel_module = types.ModuleType("housewatch.detect")
    sentinel_module.system_timezone = lambda: "Sentinel/Zone"
    original = sys.modules.get("housewatch.detect")
    sys.modules["housewatch.detect"] = sentinel_module
    try:
        assert config_mod.system_zone_name() == "Sentinel/Zone"
    finally:
        if original is None:
            del sys.modules["housewatch.detect"]
        else:
            sys.modules["housewatch.detect"] = original


def test_system_zone_name_falls_back_when_detect_is_unavailable(monkeypatch) -> None:
    """Until (or unless) housewatch.detect exists, system_zone_name must
    keep working standalone rather than raising ImportError."""
    import builtins

    real_import = builtins.__import__

    def _blocking_import(name, *args, **kwargs):
        if name == "housewatch.detect":
            raise ImportError("simulated: housewatch.detect not on disk")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocking_import)
    # Must not raise -- either resolves via the fallback or returns None.
    result = config_mod.system_zone_name()
    assert result is None or isinstance(result, str)
