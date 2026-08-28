"""Tests for housewatch.detect: gateway discovery, router identity, timezone.

Every seam here is faked -- no real subprocess, no real network, no real
filesystem access to /etc/localtime. Router/timezone values in fixtures are
deliberately generic (RFC 5737 addresses, placeholder vendor names) since
this module has no house-specific behavior to protect.
"""

from __future__ import annotations

import subprocess
from pathlib import PurePosixPath

import pytest

from housewatch.detect import (
    RouterIdentity,
    default_gateway,
    parse_router_identity,
    system_timezone,
)

# ---------------------------------------------------------------------------
# default_gateway() -- darwin
# ---------------------------------------------------------------------------


def _completed(stdout: str = "", returncode: int = 0) -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess(args=["x"], returncode=returncode, stdout=stdout, stderr="")


def test_default_gateway_darwin_success() -> None:
    seen_cmds = []

    def run(cmd: list[str]) -> "subprocess.CompletedProcess[str]":
        seen_cmds.append(cmd)
        return _completed(
            "   route to: default\ndestination: default\n    gateway: 192.0.2.1\n interface: en0\n"
        )

    assert default_gateway(platform="darwin", run=run) == "192.0.2.1"
    assert seen_cmds == [["route", "-n", "get", "default"]]


def test_default_gateway_darwin_no_gateway_line() -> None:
    run = lambda cmd: _completed("destination: default\n interface: en0\n")
    assert default_gateway(platform="darwin", run=run) is None


def test_default_gateway_darwin_empty_gateway_value() -> None:
    run = lambda cmd: _completed("gateway: \n")
    assert default_gateway(platform="darwin", run=run) is None


def test_default_gateway_darwin_nonzero_exit() -> None:
    run = lambda cmd: _completed("gateway: 192.0.2.1\n", returncode=1)
    assert default_gateway(platform="darwin", run=run) is None


def test_default_gateway_darwin_garbage_stdout() -> None:
    run = lambda cmd: _completed("not even close to route output\n")
    assert default_gateway(platform="darwin", run=run) is None


# ---------------------------------------------------------------------------
# default_gateway() -- linux
# ---------------------------------------------------------------------------


def test_default_gateway_linux_success() -> None:
    seen_cmds = []

    def run(cmd: list[str]) -> "subprocess.CompletedProcess[str]":
        seen_cmds.append(cmd)
        return _completed("default via 192.0.2.1 dev eth0 proto dhcp metric 100\n")

    assert default_gateway(platform="linux", run=run) == "192.0.2.1"
    assert seen_cmds == [["ip", "-4", "route", "show", "default"]]


def test_default_gateway_linux_no_via_token() -> None:
    run = lambda cmd: _completed("default dev eth0 scope link\n")
    assert default_gateway(platform="linux", run=run) is None


def test_default_gateway_linux_via_at_end_of_line() -> None:
    run = lambda cmd: _completed("default dev eth0 via\n")
    assert default_gateway(platform="linux", run=run) is None


def test_default_gateway_linux_nonzero_exit() -> None:
    run = lambda cmd: _completed("default via 192.0.2.1 dev eth0\n", returncode=1)
    assert default_gateway(platform="linux", run=run) is None


def test_default_gateway_linux_garbage_stdout() -> None:
    run = lambda cmd: _completed("Network is unreachable\n")
    assert default_gateway(platform="linux", run=run) is None


# ---------------------------------------------------------------------------
# default_gateway() -- cross-cutting failure shapes and defaults
# ---------------------------------------------------------------------------


def test_default_gateway_unsupported_platform_never_calls_run() -> None:
    def run(cmd: list[str]) -> "subprocess.CompletedProcess[str]":
        raise AssertionError("run() must not be called for an unsupported platform")

    assert default_gateway(platform="win32", run=run) is None


def test_default_gateway_run_raises_oserror() -> None:
    def run(cmd: list[str]) -> "subprocess.CompletedProcess[str]":
        raise OSError("no such file or directory: route")

    assert default_gateway(platform="darwin", run=run) is None


def test_default_gateway_run_raises_timeout() -> None:
    def run(cmd: list[str]) -> "subprocess.CompletedProcess[str]":
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=5.0)

    assert default_gateway(platform="darwin", run=run) is None


def test_default_gateway_defaults_platform_to_sys_platform(monkeypatch) -> None:
    import housewatch.detect as detect_mod

    monkeypatch.setattr(detect_mod.sys, "platform", "linux")
    seen_cmds = []

    def run(cmd: list[str]) -> "subprocess.CompletedProcess[str]":
        seen_cmds.append(cmd)
        return _completed("default via 192.0.2.1 dev eth0\n")

    assert default_gateway(run=run) == "192.0.2.1"
    assert seen_cmds == [["ip", "-4", "route", "show", "default"]]


def test_default_gateway_defaults_run_to_real_subprocess_wrapper(monkeypatch) -> None:
    """With no run= seam, it must still go through subprocess.run (and thus
    still degrade to None rather than raise) when the command genuinely
    fails -- proves the default wrapper wires up, without spawning anything
    that could hang the suite.
    """

    def fake_subprocess_run(cmd, **kwargs):
        assert cmd[0] == "route"
        assert kwargs.get("capture_output") is True
        assert kwargs.get("text") is True
        assert kwargs.get("timeout") == 5.0
        return _completed("no gateway here\n")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    assert default_gateway(platform="darwin") is None


# ---------------------------------------------------------------------------
# RouterIdentity.summary()
# ---------------------------------------------------------------------------


def test_summary_all_fields_present() -> None:
    ident = RouterIdentity(
        friendly_name="Example Router",
        manufacturer="ExampleNet",
        model_name="Example IGD",
        model_number="EX-100",
    )
    assert ident.summary() == "Example Router (ExampleNet Example IGD EX-100)"


def test_summary_drops_model_number_matching_model_name() -> None:
    ident = RouterIdentity(
        friendly_name="Example Router",
        manufacturer="ExampleNet",
        model_name="EX-100",
        model_number="EX-100",
    )
    assert ident.summary() == "Example Router (ExampleNet EX-100)"


def test_summary_drops_none_parts() -> None:
    ident = RouterIdentity(
        friendly_name="Example Router",
        manufacturer=None,
        model_name="Example IGD",
        model_number=None,
    )
    assert ident.summary() == "Example Router (Example IGD)"


def test_summary_no_parenthetical_when_only_friendly_name() -> None:
    ident = RouterIdentity(friendly_name="Example Router", manufacturer=None, model_name=None, model_number=None)
    assert ident.summary() == "Example Router"


def test_summary_promotes_model_name_when_friendly_name_missing() -> None:
    ident = RouterIdentity(friendly_name=None, manufacturer="ExampleNet", model_name="Example IGD", model_number=None)
    assert ident.summary() == "Example IGD (ExampleNet)"


def test_summary_promotes_manufacturer_when_friendly_and_model_name_missing() -> None:
    ident = RouterIdentity(friendly_name=None, manufacturer="ExampleNet", model_name=None, model_number="EX-100")
    assert ident.summary() == "ExampleNet (EX-100)"


def test_summary_all_none_returns_literal() -> None:
    ident = RouterIdentity(friendly_name=None, manufacturer=None, model_name=None, model_number=None)
    assert ident.summary() == "unidentified UPnP device"


def test_summary_never_raises_or_returns_empty_string() -> None:
    for combo in (
        RouterIdentity(None, None, None, None),
        RouterIdentity("", "", "", ""),
        RouterIdentity(None, None, None, "EX-100"),
    ):
        result = combo.summary()
        assert isinstance(result, str)
        assert result != ""


# ---------------------------------------------------------------------------
# parse_router_identity()
# ---------------------------------------------------------------------------


def _gatedesc_xml(
    *,
    friendly_name: str | None = "Example Router",
    manufacturer: str | None = "ExampleNet",
    model_name: str | None = "Example IGD",
    model_number: str | None = "EX-100",
    nested_friendly_name: str = "Inner Device",
) -> bytes:
    def tag(name: str, value: str | None) -> str:
        return f"<{name}>{value}</{name}>" if value is not None else ""

    xml = f"""<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>urn:schemas-upnp-org:device:InternetGatewayDevice:1</deviceType>
    {tag("friendlyName", friendly_name)}
    {tag("manufacturer", manufacturer)}
    {tag("modelName", model_name)}
    {tag("modelNumber", model_number)}
    <UDN>uuid:00000000-0000-0000-0000-000000000000</UDN>
    <deviceList>
      <device>
        <deviceType>urn:schemas-upnp-org:device:WANDevice:1</deviceType>
        <friendlyName>{nested_friendly_name}</friendlyName>
        <manufacturer>ShouldNeverSurface</manufacturer>
      </device>
    </deviceList>
  </device>
</root>"""
    return xml.encode("utf-8")


def test_parse_router_identity_full_success() -> None:
    ident = parse_router_identity(_gatedesc_xml())
    assert ident == RouterIdentity(
        friendly_name="Example Router",
        manufacturer="ExampleNet",
        model_name="Example IGD",
        model_number="EX-100",
    )


def test_parse_router_identity_uses_first_device_not_nested() -> None:
    ident = parse_router_identity(_gatedesc_xml(nested_friendly_name="Should Not Win"))
    assert ident.friendly_name == "Example Router"
    assert ident.friendly_name != "Should Not Win"


def test_parse_router_identity_root_fields_missing_does_not_fall_through_to_nested() -> None:
    xml = _gatedesc_xml(friendly_name=None, manufacturer=None, model_name=None, model_number=None)
    ident = parse_router_identity(xml)
    assert ident == RouterIdentity(None, None, None, None)


def test_parse_router_identity_missing_individual_fields() -> None:
    ident = parse_router_identity(_gatedesc_xml(manufacturer=None, model_number=None))
    assert ident.friendly_name == "Example Router"
    assert ident.manufacturer is None
    assert ident.model_name == "Example IGD"
    assert ident.model_number is None


def test_parse_router_identity_empty_element_text_is_none() -> None:
    xml = _gatedesc_xml(friendly_name="")
    ident = parse_router_identity(xml)
    assert ident.friendly_name is None


def test_parse_router_identity_malformed_xml_returns_all_none() -> None:
    ident = parse_router_identity(b"<root><device><friendlyName>unterminated")
    assert ident == RouterIdentity(None, None, None, None)


def test_parse_router_identity_no_device_element_returns_all_none() -> None:
    ident = parse_router_identity(b"<root><specVersion><major>1</major></specVersion></root>")
    assert ident == RouterIdentity(None, None, None, None)


def test_parse_router_identity_empty_bytes_returns_all_none() -> None:
    ident = parse_router_identity(b"")
    assert ident == RouterIdentity(None, None, None, None)


def test_parse_router_identity_never_raises() -> None:
    for garbage in (b"", b"not xml at all", b"<", b"\x00\x01\x02", b"<root>", _gatedesc_xml()):
        parse_router_identity(garbage)  # must not raise


# ---------------------------------------------------------------------------
# system_timezone()
# ---------------------------------------------------------------------------


class _FakeLocaltimePath:
    """Stands in for Path("/etc/localtime"): only .resolve() is ever called."""

    def __init__(self, resolved: PurePosixPath | None = None, raises: bool = False) -> None:
        self._resolved = resolved
        self._raises = raises

    def resolve(self) -> PurePosixPath:
        if self._raises:
            raise OSError("no such file or directory")
        assert self._resolved is not None
        return self._resolved


def test_system_timezone_valid_tz_env_wins() -> None:
    result = system_timezone(environ={"TZ": "America/Chicago"}, localtime_path=_FakeLocaltimePath(raises=True))
    assert result == "America/Chicago"


def test_system_timezone_garbage_tz_env_falls_through_to_localtime() -> None:
    localtime = _FakeLocaltimePath(resolved=PurePosixPath("/usr/share/zoneinfo/America/New_York"))
    result = system_timezone(environ={"TZ": "Not/A/Real/Zone"}, localtime_path=localtime)
    assert result == "America/New_York"


def test_system_timezone_empty_tz_env_falls_through_to_localtime() -> None:
    localtime = _FakeLocaltimePath(resolved=PurePosixPath("/usr/share/zoneinfo/America/New_York"))
    result = system_timezone(environ={"TZ": ""}, localtime_path=localtime)
    assert result == "America/New_York"


def test_system_timezone_no_tz_env_at_all_falls_through_to_localtime() -> None:
    localtime = _FakeLocaltimePath(resolved=PurePosixPath("/usr/share/zoneinfo/America/New_York"))
    result = system_timezone(environ={}, localtime_path=localtime)
    assert result == "America/New_York"


def test_system_timezone_macos_style_zoneinfo_path() -> None:
    localtime = _FakeLocaltimePath(resolved=PurePosixPath("/var/db/timezone/zoneinfo/America/New_York"))
    result = system_timezone(environ={}, localtime_path=localtime)
    assert result == "America/New_York"


def test_system_timezone_path_without_zoneinfo_component_returns_none() -> None:
    localtime = _FakeLocaltimePath(resolved=PurePosixPath("/some/other/path/America/New_York"))
    result = system_timezone(environ={}, localtime_path=localtime)
    assert result is None


def test_system_timezone_zoneinfo_with_nothing_after_it_returns_none() -> None:
    localtime = _FakeLocaltimePath(resolved=PurePosixPath("/usr/share/zoneinfo"))
    result = system_timezone(environ={}, localtime_path=localtime)
    assert result is None


def test_system_timezone_resolved_but_unresolvable_zone_returns_none() -> None:
    localtime = _FakeLocaltimePath(resolved=PurePosixPath("/usr/share/zoneinfo/Not/A/Real/Zone"))
    result = system_timezone(environ={}, localtime_path=localtime)
    assert result is None


def test_system_timezone_resolve_raises_oserror_returns_none() -> None:
    result = system_timezone(environ={}, localtime_path=_FakeLocaltimePath(raises=True))
    assert result is None


def test_system_timezone_defaults_to_os_environ_and_real_path(monkeypatch) -> None:
    """No seams passed -- must still be total (None or a string), never raise,
    regardless of the real host's timezone configuration."""
    monkeypatch.delenv("TZ", raising=False)
    result = system_timezone()
    assert result is None or isinstance(result, str)
