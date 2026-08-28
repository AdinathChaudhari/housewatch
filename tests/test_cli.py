"""CLI-level invariants.

The one bug this file exists to prevent: ``doctor`` used to print

    fix: python -m housewatch install-agent   (if that subcommand exists ...)

for the failing LaunchAgent check — naming a subcommand the parser has
never registered, hedged with "if that subcommand exists in this build".
A fix line that tells the user to run a command that does not exist is
worse than no fix line, and nothing caught it because there were no tests
over the CLI surface at all.

Note on stubbing: the LaunchAgent fix line is only reached when the
``launchctl print`` subprocess *returns non-zero*. Making the stub raise
instead sends ``cmd_doctor`` down its ``except OSError`` branch, which
prints a different message and silently skips the line under test — so
these tests would pass against the very bug they exist to catch.
"""

from __future__ import annotations

import argparse
import plistlib
import re
from dataclasses import replace
from pathlib import Path

import pytest

from housewatch import cli
from housewatch import config as config_mod
from housewatch import igd
from housewatch.config import Config, ConfigError
from housewatch.model import SourceError


def _registered_subcommands() -> set[str]:
    """Every subcommand name the parser actually accepts."""
    parser = cli.build_parser()
    names: set[str] = set()
    for action in parser._actions:
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict):
            names.update(choices)
    return names


def test_parser_registers_the_documented_subcommands():
    assert _registered_subcommands() == {"run", "once", "status", "serve", "doctor", "config"}


def _housewatch_invocations(text: str) -> set[str]:
    """Subcommand names in any 'python -m housewatch <cmd>' the text prints."""
    return set(re.findall(r"python -m housewatch\s+([a-z][a-z-]*)", text))


class _NotLoaded:
    """Stands in for the CompletedProcess of a failing `launchctl print`."""

    returncode = 1
    stdout = ""
    stderr = "Could not find service"


@pytest.fixture
def doctor_output(capsys, monkeypatch, tmp_path):
    """``doctor`` output with router unreachable and the agent not loaded.

    Both are forced to the *failure* path (not the error path) so that the
    fix lines are actually printed, and neither the real router nor the
    real database is touched.
    """
    cfg = replace(config_mod.Config(), db_path=str(tmp_path / "doctor.db"))
    monkeypatch.setattr(cli, "_load_config_or_die", lambda: cfg)

    def _unreachable(self):
        raise SourceError("forced: router unreachable")

    monkeypatch.setattr(igd.UpnpIgdSource, "read", _unreachable)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw: _NotLoaded())
    # Step 0 fix: cmd_doctor's LaunchAgent-fix branch checks Path.home() for
    # an existing plist. On a machine where the collector agent is genuinely
    # installed (e.g. the developer's own), that check finds it and doctor
    # prints the "launchctl bootstrap ..." fix line instead of the
    # "run ./install.sh" line this fixture is built to exercise -- making the
    # fixture's behavior depend on the state of the machine running the
    # suite. Pointing Path.home() at an empty tmp_path makes the
    # plist-absent branch deterministic everywhere.
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)

    rc = cli.cmd_doctor(object())
    out = capsys.readouterr().out
    assert rc == 1, "doctor should exit non-zero when checks fail"
    assert "is not loaded" in out, (
        "the LaunchAgent fix line was never reached — this fixture is not "
        "exercising the code it claims to"
    )
    return out


def test_doctor_fix_lines_name_only_real_subcommands(doctor_output):
    invented = _housewatch_invocations(doctor_output) - _registered_subcommands()
    assert not invented, f"doctor suggests nonexistent subcommand(s): {sorted(invented)}"


def test_doctor_never_hedges_about_its_own_commands(doctor_output):
    lowered = doctor_output.lower()
    for hedge in ("if that subcommand exists", "if it exists", "may not exist"):
        assert hedge not in lowered, f"doctor hedges about its own CLI: {hedge!r}"


# ---------------------------------------------------------------------------
# LaunchAgent label: one name, four files. This is the single assertion that
# prevents the whole class of bug from recurring.
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_launchd_label_matches_the_plist_that_actually_gets_installed() -> None:
    """cli.LAUNCHD_LABEL is what menubar.py's Start/Stop Collector control
    and doctor's LaunchAgent check address the agent by. It must equal the
    Label in the plist install.sh renders into ~/Library/LaunchAgents/.

    They disagreed once: cli.py said a personal-namespaced label, e.g.
    'com.example.housewatch', while install.sh, uninstall.sh, the Makefile
    and the plist all said 'com.housewatch.collector'. The result was a
    menubar toggle that could
    never stop a running collector (collector_is_loaded() was permanently
    False, so the toggle always took the 'start' branch) and a `doctor` that
    reported a perfectly healthy install as FAIL.
    """
    plist_path = PROJECT_ROOT / "com.housewatch.collector.plist"
    parsed = plistlib.loads(plist_path.read_bytes())
    assert parsed["Label"] == cli.LAUNCHD_LABEL


@pytest.mark.parametrize("script", ["install.sh", "uninstall.sh", "Makefile"])
def test_shell_installers_use_the_same_label(script: str) -> None:
    """The shell side hardcodes the label too; pin every copy to cli.py's."""
    text = (PROJECT_ROOT / script).read_text(encoding="utf-8")
    # Strip a trailing '.plist' so the filename reference counts as the same
    # label, not a second one.
    labels = {
        m.removesuffix(".plist")
        for m in re.findall(r"com\.[A-Za-z0-9_.]*housewatch[A-Za-z0-9_.]*", text)
    }
    assert labels, f"{script} names no housewatch launchd label at all"
    assert labels == {cli.LAUNCHD_LABEL}, (
        f"{script} uses {sorted(labels)}, but cli.LAUNCHD_LABEL is "
        f"{cli.LAUNCHD_LABEL!r}"
    )


def test_doctor_does_not_claim_there_is_no_installer(doctor_output) -> None:
    """doctor used to print 'housewatch ships no installer - the plist is
    yours to write' while install.sh sat in the same directory, and told the
    user to hand-write a plist that ships in the repo."""
    assert "ships no installer" not in doctor_output
    assert "install.sh" in doctor_output


def test_doctor_names_the_installed_label(doctor_output) -> None:
    """The label doctor reports on must be the one install.sh installs."""
    assert cli.LAUNCHD_LABEL in doctor_output


# ---------------------------------------------------------------------------
# `config show` / `set-cap` / `clear-cap` / `set-cycle-day`
# ---------------------------------------------------------------------------


@pytest.fixture
def config_path(tmp_path, monkeypatch) -> Path:
    """Point every config command at an isolated, throwaway file.

    ``config_mod.DEFAULT_PATH`` is read fresh inside the read/write helpers
    on every call (never cached at import time), so patching it here is
    enough to keep every test in this section off the real
    ``~/.config/housewatch/config.toml``.
    """
    path = tmp_path / "config.toml"
    monkeypatch.setattr(config_mod, "DEFAULT_PATH", path)
    return path


def _run(argv: list[str], capsys) -> tuple[int, str, str]:
    parser = cli.build_parser()
    args = parser.parse_args(argv)
    rc = args.func(args)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def test_config_show_on_a_fresh_clone_invents_no_cap(config_path, capsys):
    """The first-run invariant: no file, no cap, nothing asked."""
    rc, out, err = _run(["config", "show"], capsys)
    assert rc == 0
    assert err == ""
    assert "does not exist" in out
    assert "monthly_cap_gb  unset (no cap — usage still tracked)  default" in out
    assert "router_host  auto-detect default gateway  default" in out
    assert "billing_cycle_start_day  1  default" in out
    assert "wrap-certainty window: 34.4s at max_link_mbps=1000" in out


def test_config_show_renders_resolved_local_timezone(config_path, monkeypatch, capsys):
    monkeypatch.setattr(config_mod, "system_zone_name", lambda: "America/Chicago")
    rc, out, _ = _run(["config", "show"], capsys)
    assert rc == 0
    assert "timezone  local -> America/Chicago  default" in out


def test_config_show_renders_unresolved_timezone(config_path, monkeypatch, capsys):
    monkeypatch.setattr(config_mod, "system_zone_name", lambda: None)
    rc, out, _ = _run(["config", "show"], capsys)
    assert rc == 0
    assert "timezone  local -> UNRESOLVED (fixed-offset fallback)  default" in out


def test_config_show_fails_the_same_way_every_command_does_on_bad_toml(config_path, capsys):
    config_path.write_text("not valid toml [[[\n", encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        _run(["config", "show"], capsys)
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "config error" in err


def test_config_set_cap_writes_the_file_and_reports_provenance(config_path, capsys):
    rc, out, err = _run(["config", "set-cap", "1000"], capsys)
    assert rc == 0
    assert err == ""
    assert f"monthly_cap_gb = 1000 -> {config_path}" in out
    assert "billing cycle: starts day 1 (calendar month)" in out
    assert "python -m housewatch config set-cycle-day <1-28>" in out
    assert "already-running dashboard or menubar shows the old cap until restarted" in out
    assert "note: comments in config.toml are not preserved" in out
    assert config_path.exists()

    rc2, out2, _ = _run(["config", "show"], capsys)
    assert rc2 == 0
    assert "monthly_cap_gb  1000.0  file" in out2


def test_config_set_cap_non_calendar_cycle_day_omits_the_note(config_path, capsys):
    _run(["config", "set-cycle-day", "5"], capsys)
    rc, out, _ = _run(["config", "set-cap", "250"], capsys)
    assert rc == 0
    assert "billing cycle: starts day 5 —" in out
    assert "(calendar month)" not in out


def test_config_set_cap_preserves_an_unrelated_existing_key_verbatim(config_path, capsys):
    config_path.write_text('log_level = "DEBUG"\n', encoding="utf-8")
    rc, out, err = _run(["config", "set-cap", "500"], capsys)
    assert rc == 0
    text = config_path.read_text(encoding="utf-8")
    assert 'log_level = "DEBUG"' in text
    assert "monthly_cap_gb = 500.0" in text


def test_config_set_cap_rejects_non_numeric_input(config_path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.build_parser().parse_args(["config", "set-cap", "abc"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "'abc' is not a number of GB" in err
    assert "1 GB = 1e9 bytes" in err
    assert not config_path.exists()


@pytest.mark.parametrize("raw", ["nan", "inf"])
def test_config_set_cap_rejects_nan_and_infinite_input(config_path, capsys, raw):
    with pytest.raises(SystemExit) as excinfo:
        cli.build_parser().parse_args(["config", "set-cap", raw])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "is not a number of GB" in err
    assert not config_path.exists()


@pytest.mark.parametrize("raw", ["0", "-5"])
def test_config_set_cap_rejects_non_positive_input(config_path, capsys, raw):
    with pytest.raises(SystemExit) as excinfo:
        cli.build_parser().parse_args(["config", "set-cap", raw])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "the cap must be greater than 0" in err
    assert "config clear-cap" in err
    assert not config_path.exists()


def test_config_clear_cap_is_idempotent_and_writes_nothing_when_absent(config_path, capsys):
    rc, out, err = _run(["config", "clear-cap"], capsys)
    assert rc == 0
    assert err == ""
    assert "no cap was set — nothing to change" in out
    assert not config_path.exists()


def test_config_clear_cap_removes_the_key(config_path, capsys):
    _run(["config", "set-cap", "200"], capsys)
    rc, out, err = _run(["config", "clear-cap"], capsys)
    assert rc == 0
    assert f"monthly_cap_gb cleared -> {config_path}" in out
    text = config_path.read_text(encoding="utf-8")
    assert "monthly_cap_gb" not in text

    rc2, out2, _ = _run(["config", "show"], capsys)
    assert "monthly_cap_gb  unset (no cap — usage still tracked)  default" in out2


def test_config_set_cycle_day_writes_the_file(config_path, capsys):
    rc, out, err = _run(["config", "set-cycle-day", "5"], capsys)
    assert rc == 0
    assert f"billing_cycle_start_day = 5 -> {config_path}" in out
    assert "(calendar month)" not in out
    assert "billing_cycle_start_day = 5" in config_path.read_text(encoding="utf-8")


def test_config_set_cycle_day_one_is_labelled_calendar_month(config_path, capsys):
    rc, out, _ = _run(["config", "set-cycle-day", "1"], capsys)
    assert rc == 0
    assert "billing_cycle_start_day = 1 (calendar month)" in out


def test_config_set_cycle_day_rejects_non_integer(config_path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.build_parser().parse_args(["config", "set-cycle-day", "foo"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "'foo' is not a whole number." in err
    assert not config_path.exists()


@pytest.mark.parametrize("raw", ["0", "29", "31", "-1"])
def test_config_set_cycle_day_rejects_out_of_range(config_path, capsys, raw):
    with pytest.raises(SystemExit) as excinfo:
        cli.build_parser().parse_args(["config", "set-cycle-day", raw])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert f"{raw} is outside 1-28" in err
    assert "Days 29-31 do not exist in every month" in err
    assert not config_path.exists()


def test_config_group_requires_a_verb(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.build_parser().parse_args(["config"])
    assert excinfo.value.code == 2


def test_config_refuses_to_write_when_the_existing_file_is_malformed_toml(config_path, capsys):
    original = "not valid toml [[[\n"
    config_path.write_text(original, encoding="utf-8")
    rc, out, err = _run(["config", "set-cap", "500"], capsys)
    assert rc == 1
    assert "config error" in err
    # Nothing must be written on top of the broken file -- the write path
    # fails at the read step, before any mutation is attempted.
    assert config_path.read_text(encoding="utf-8") == original


def test_config_refuses_to_write_when_the_existing_file_has_an_unknown_key(config_path, capsys):
    original = "bogus_key_nobody_recognizes = 1\n"
    config_path.write_text(original, encoding="utf-8")
    rc, out, err = _run(["config", "set-cycle-day", "5"], capsys)
    assert rc == 1
    assert "config error" in err
    assert "unknown config key" in err
    assert config_path.read_text(encoding="utf-8") == original


def test_config_set_cap_write_is_atomic_no_leftover_tmp_file(config_path, capsys):
    rc, _out, _err = _run(["config", "set-cap", "42"], capsys)
    assert rc == 0
    tmp_path_candidate = config_path.with_suffix(config_path.suffix + ".tmp")
    assert config_path.exists()
    assert not tmp_path_candidate.exists()


def test_config_write_header_names_the_command_and_the_example_file(config_path, capsys):
    _run(["config", "set-cap", "42"], capsys)
    text = config_path.read_text(encoding="utf-8")
    assert "housewatch config" in text
    assert "config.example.toml" in text


# ---------------------------------------------------------------------------
# once/status: the router-identity line, the baseline note, the cap hint
# ---------------------------------------------------------------------------

from housewatch.model import Reading  # noqa: E402  (kept near its one use)


class _FakeSource:
    """Stands in for igd.UpnpIgdSource: no network, a scripted Reading."""

    def __init__(self, reading, location="http://192.0.2.1:1900/tok1234/gatedesc.xml",
                 identity=None):
        self._reading = reading
        self.location = location
        if identity is not None:
            self.identity = identity

    def read(self):
        return self._reading

    def describe(self) -> str:
        return "UPnP IGD @ 192.0.2.1"


class _FakeIdentity:
    def __init__(self, text: str):
        self._text = text

    def summary(self) -> str:
        return self._text


@pytest.fixture
def once_cfg(tmp_path, monkeypatch):
    cfg = replace(config_mod.Config(), db_path=str(tmp_path / "once.db"))
    monkeypatch.setattr(cli, "_load_config_or_die", lambda: cfg)
    # Isolate the single-instance pidfile lock from the real
    # ~/.config/housewatch/housewatch.pid -- on a machine where the real
    # collector LaunchAgent is installed and running, that lock is already
    # held, and these tests must not fight the real daemon for it (or write
    # anywhere under the real user's home at all).
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setattr(cli, "_RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(cli, "PIDFILE_PATH", runtime_dir / "housewatch.pid")
    return cfg


def test_once_first_poll_prints_baseline_note_and_no_router_line_without_identity(
    once_cfg, monkeypatch, capsys
):
    reading = Reading(ts=1000.0, bytes_recv=10, bytes_sent=5, uptime_s=100,
                       pkts_recv=1, pkts_sent=1)
    monkeypatch.setattr(cli, "build_source", lambda cfg, store: _FakeSource(reading))

    rc = cli.cmd_once(argparse.Namespace())
    out = capsys.readouterr().out
    assert rc == 0
    assert "kind:       baseline" in out
    assert "first reading recorded — usage deltas start with the next poll" in out
    # The fake source exposes no `identity` attribute at all (mirrors
    # igd.py before that wiring lands) -- no router line should be invented.
    assert "router:" not in out


def test_once_prints_router_identity_when_the_source_provides_one(
    once_cfg, monkeypatch, capsys
):
    reading = Reading(ts=1000.0, bytes_recv=10, bytes_sent=5, uptime_s=100,
                       pkts_recv=1, pkts_sent=1)
    identity = _FakeIdentity("Example Router (ExampleNet)")
    source = _FakeSource(reading, location="http://192.0.2.1:1900/tok1234/gatedesc.xml",
                          identity=identity)
    monkeypatch.setattr(cli, "build_source", lambda cfg, store: source)

    rc = cli.cmd_once(argparse.Namespace())
    out = capsys.readouterr().out
    assert rc == 0
    assert "router: Example Router (ExampleNet) at http://192.0.2.1:1900/tok1234/gatedesc.xml" in out


def test_once_router_line_prints_even_when_the_read_ultimately_fails(
    once_cfg, monkeypatch, capsys
):
    """Identity is captured at gatedesc-parse time, before the SOAP counter
    reads -- so a source that discovered the router but then failed to read
    counters can still know who it found."""

    class _PartialFailure(_FakeSource):
        def read(self):
            raise SourceError("forced: counters unreachable")

    identity = _FakeIdentity("unidentified UPnP device")
    source = _PartialFailure(reading=None, identity=identity)
    monkeypatch.setattr(cli, "build_source", lambda cfg, store: source)

    rc = cli.cmd_once(argparse.Namespace())
    out = capsys.readouterr().out
    assert rc == 1
    assert "router: unidentified UPnP device at" in out
    assert "poll FAILED" in out


def test_status_suggests_set_cap_when_no_cap_configured(tmp_path, monkeypatch, capsys):
    cfg = replace(config_mod.Config(), db_path=str(tmp_path / "status.db"))
    monkeypatch.setattr(cli, "_load_config_or_die", lambda: cfg)
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setattr(cli, "_RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(cli, "PIDFILE_PATH", runtime_dir / "housewatch.pid")

    reading = Reading(ts=1000.0, bytes_recv=10, bytes_sent=5, uptime_s=100,
                       pkts_recv=1, pkts_sent=1)
    monkeypatch.setattr(cli, "build_source", lambda cfg, store: _FakeSource(reading))
    once_rc = cli.cmd_once(argparse.Namespace())
    assert once_rc == 0
    capsys.readouterr()  # discard `once`'s own output

    rc = cli.cmd_status(argparse.Namespace())
    out = capsys.readouterr().out
    assert rc == 0
    assert "no cap set" in out
    assert "set one (optional — usage is tracked either way): python -m housewatch config set-cap <GB>" in out
    assert "router: not yet identified" in out


def test_status_reports_the_persisted_router_identity(tmp_path, monkeypatch, capsys):
    cfg = replace(config_mod.Config(), db_path=str(tmp_path / "status2.db"))
    monkeypatch.setattr(cli, "_load_config_or_die", lambda: cfg)
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setattr(cli, "_RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(cli, "PIDFILE_PATH", runtime_dir / "housewatch.pid")

    reading = Reading(ts=1000.0, bytes_recv=10, bytes_sent=5, uptime_s=100,
                       pkts_recv=1, pkts_sent=1)
    monkeypatch.setattr(cli, "build_source", lambda cfg, store: _FakeSource(reading))
    cli.cmd_once(argparse.Namespace())
    capsys.readouterr()

    from housewatch.store import Store
    store = Store(Path(cfg.db_path), readonly=False)
    try:
        store.set_meta("router_identity", "Example Router (ExampleNet)")
    finally:
        store.close()

    rc = cli.cmd_status(argparse.Namespace())
    out = capsys.readouterr().out
    assert rc == 0
    assert "router: Example Router (ExampleNet)" in out


def test_doctor_reports_config_cap_cycle_and_timezone_summary(doctor_output):
    assert "monthly_cap_gb: unset (no cap — usage still tracked)" in doctor_output
    assert "billing_cycle_start_day: 1 (calendar month)" in doctor_output
    assert "timezone: local ->" in doctor_output
