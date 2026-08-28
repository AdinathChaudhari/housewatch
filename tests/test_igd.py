"""Tests for housewatch.igd: SSDP/gatedesc/SOAP parsing and error mapping.

Everything below except the ``live`` group runs against canned fixture
strings — no real network. Fixture shapes are built to match router
behavior captured live during development (LOCATION path token, service
list, and — critically — a malformed ``GetStatusInfoResponse`` namespace
observed on one TP-Link Aginet unit, which is why the parser matches XML
elements by local tag name only).

The ``live`` group makes one real call against a real router — either an
explicit ``HOUSEWATCH_LIVE_ROUTER_HOST`` or the machine's own auto-detected
default gateway — and is skipped automatically (via skipif + a
reachability guard) when no router is reachable, so the suite stays green
anywhere else.
"""

from __future__ import annotations

import os
import socket
import subprocess

import pytest
import requests

from housewatch import igd
from housewatch.igd import (
    CounterOutOfRangeError,
    DiscoveryFailedError,
    MalformedXmlError,
    MissingFieldError,
    SoapFaultError,
    UnexpectedServiceListError,
    UpnpIgdSource,
    _ControlUrls,
    _collect_service_control_urls,
    _find_soap_fault,
    _local_name,
    _parse_gatedesc,
    _parse_location_header,
    _parse_ui4,
    _parse_xml,
    default_gateway,
)
from housewatch.model import Reading, SourceUnreachable

ROUTER_HOST = "192.0.2.1"
LOCATION = f"http://{ROUTER_HOST}:1900/tok1234/gatedesc.xml"

SVC_L3FWD = "urn:schemas-upnp-org:service:Layer3Forwarding:1"
SVC_WANCOMMON = igd.SVC_WAN_COMMON_IFC
SVC_WANIPCONN = igd.SVC_WAN_IP_CONN


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _gatedesc_xml(*, include_wanipconn: bool = True, include_wancommon: bool = True) -> bytes:
    """A realistic nested IGD device tree, matching the shape real firmware ships:
    root device -> WANDevice -> WANConnectionDevice, each with its own
    serviceList, plus Layer3Forwarding directly on the root device. The
    'tok1234' segment stands in for the per-boot random token — tests must
    never assume this literal value is stable, only that whatever value
    appears here is what gets extracted into the control URLs.
    """
    wancommon_service = (
        f"""<service>
            <serviceType>{SVC_WANCOMMON}</serviceType>
            <serviceId>urn:upnp-org:serviceId:WANCommonIFC1</serviceId>
            <controlURL>/upnp/control/tok1234/WANCommonIFC1</controlURL>
            <eventSubURL>/upnp/event/tok1234/WANCommonIFC1</eventSubURL>
            <SCPDURL>/WANCommonIFC1.xml</SCPDURL>
          </service>"""
        if include_wancommon
        else ""
    )
    wanipconn_service = (
        f"""<service>
                <serviceType>{SVC_WANIPCONN}</serviceType>
                <serviceId>urn:upnp-org:serviceId:WANIPConn1</serviceId>
                <controlURL>/upnp/control/tok1234/WANIPConn1</controlURL>
                <eventSubURL>/upnp/event/tok1234/WANIPConn1</eventSubURL>
                <SCPDURL>/WANIPConn1.xml</SCPDURL>
              </service>"""
        if include_wanipconn
        else ""
    )
    xml = f"""<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>urn:schemas-upnp-org:device:InternetGatewayDevice:1</deviceType>
    <friendlyName>Example Router</friendlyName>
    <manufacturer>ExampleNet</manufacturer>
    <UDN>uuid:11111111-2222-3333-4444-555555555555</UDN>
    <serviceList>
      <service>
        <serviceType>{SVC_L3FWD}</serviceType>
        <serviceId>urn:upnp-org:serviceId:Layer3Forwarding1</serviceId>
        <controlURL>/upnp/control/tok1234/dummy</controlURL>
        <eventSubURL>/upnp/event/tok1234/dummy</eventSubURL>
        <SCPDURL>/dummy.xml</SCPDURL>
      </service>
    </serviceList>
    <deviceList>
      <device>
        <deviceType>urn:schemas-upnp-org:device:WANDevice:1</deviceType>
        <friendlyName>WANDevice</friendlyName>
        <UDN>uuid:66666666-7777-8888-9999-000000000000</UDN>
        <serviceList>
          {wancommon_service}
        </serviceList>
        <deviceList>
          <device>
            <deviceType>urn:schemas-upnp-org:device:WANConnectionDevice:1</deviceType>
            <friendlyName>WANConnectionDevice</friendlyName>
            <UDN>uuid:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee</UDN>
            <serviceList>
              {wanipconn_service}
            </serviceList>
          </device>
        </deviceList>
      </device>
    </deviceList>
  </device>
</root>"""
    return xml.encode("utf-8")


def _soap_response(action: str, service_type: str, fields: dict[str, str]) -> bytes:
    body_fields = "".join(f"<{k}>{v}</{k}>" for k, v in fields.items())
    xml = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body>"
        f'<u:{action}Response xmlns:u="{service_type}">{body_fields}</u:{action}Response>'
        "</s:Body></s:Envelope>"
    )
    return xml.encode("utf-8")


def _status_info_response_malformed_ns(uptime: int, status: str = "Connected", last_error: str = "ERROR_NONE") -> bytes:
    """Reproduces the live-verified firmware quirk: GetStatusInfoResponse
    declares xmlns:u="urn:schemas-upnp-org:service:GetStatusInfo:1" instead
    of the correct WANIPConnection:1 service type. A namespace-strict
    parser would fail to find NewUptime here; the local-name matcher must
    not.
    """
    xml = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body>"
        '<u:GetStatusInfoResponse xmlns:u="urn:schemas-upnp-org:service:GetStatusInfo:1">'
        f"<NewConnectionStatus>{status}</NewConnectionStatus>"
        f"<NewLastConnectionError>{last_error}</NewLastConnectionError>"
        f"<NewUptime>{uptime}</NewUptime>"
        "</u:GetStatusInfoResponse>"
        "</s:Body></s:Envelope>"
    )
    return xml.encode("utf-8")


def _soap_fault_xml(error_code: str = "401", error_description: str = "Invalid Action") -> bytes:
    xml = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body><s:Fault>"
        "<faultcode>s:Client</faultcode>"
        "<faultstring>UPnPError</faultstring>"
        '<detail><UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
        f"<errorCode>{error_code}</errorCode>"
        f"<errorDescription>{error_description}</errorDescription>"
        "</UPnPError></detail>"
        "</s:Fault></s:Body></s:Envelope>"
    )
    return xml.encode("utf-8")


TRUNCATED_XML = b'<?xml version="1.0"?><s:Envelope><s:Body><u:GetTotalBytesRece'


# ---------------------------------------------------------------------------
# Fake requests.Session for network-free integration tests
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, content: bytes) -> None:
        self.status_code = status_code
        self.content = content
        self.text = content.decode("utf-8", errors="replace")


def _resolve(entry, url: str) -> _FakeResponse:
    if isinstance(entry, Exception):
        raise entry
    if callable(entry) and not isinstance(entry, _FakeResponse):
        return entry(url)
    assert isinstance(entry, _FakeResponse)
    return entry


class _FakeSession:
    """A requests.Session stand-in keyed by URL (GET) or (URL, action) (POST).

    A missing key raises a ConnectionError, same shape as a real refused
    connection, so tests that only configure some URLs implicitly assert
    "nothing else gets called".
    """

    def __init__(self, get_responses=None, post_responses=None) -> None:
        self.get_responses = dict(get_responses or {})
        self.post_responses = dict(post_responses or {})
        self.get_calls: list[str] = []
        self.post_calls: list[tuple[str, str]] = []

    def get(self, url, timeout=None):
        self.get_calls.append(url)
        entry = self.get_responses.get(url)
        if entry is None:
            raise requests.exceptions.ConnectionError(f"no fake GET response for {url}")
        return _resolve(entry, url)

    def post(self, url, data=None, headers=None, timeout=None):
        soap_action = (headers or {}).get("SOAPAction", "")
        action = soap_action.strip('"').split("#")[-1]
        self.post_calls.append((url, action))
        entry = self.post_responses.get((url, action))
        if entry is None:
            raise requests.exceptions.ConnectionError(f"no fake POST response for ({url}, {action})")
        return _resolve(entry, url)

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


def _install_fake_session(monkeypatch, get_responses=None, post_responses=None) -> _FakeSession:
    fake = _FakeSession(get_responses, post_responses)
    monkeypatch.setattr(requests, "Session", lambda: fake)
    return fake


def _happy_soap_responses(
    *,
    bytes_recv: int = 1_400_000_000,
    bytes_sent: int = 300_000_000,
    uptime: int = 9_000_804,
    pkts_recv: int = 900_000_000,
    pkts_sent: int = 450_000_000,
    wancommon_url: str = f"http://{ROUTER_HOST}:1900/upnp/control/tok1234/WANCommonIFC1",
    wanipconn_url: str = f"http://{ROUTER_HOST}:1900/upnp/control/tok1234/WANIPConn1",
) -> dict:
    return {
        (wancommon_url, "GetTotalBytesReceived"): _FakeResponse(
            200, _soap_response("GetTotalBytesReceived", SVC_WANCOMMON, {"NewTotalBytesReceived": str(bytes_recv)})
        ),
        (wancommon_url, "GetTotalBytesSent"): _FakeResponse(
            200, _soap_response("GetTotalBytesSent", SVC_WANCOMMON, {"NewTotalBytesSent": str(bytes_sent)})
        ),
        (wanipconn_url, "GetStatusInfo"): _FakeResponse(200, _status_info_response_malformed_ns(uptime)),
        (wancommon_url, "GetTotalPacketsReceived"): _FakeResponse(
            200,
            _soap_response(
                "GetTotalPacketsReceived", SVC_WANCOMMON, {"NewTotalPacketsReceived": str(pkts_recv)}
            ),
        ),
        (wancommon_url, "GetTotalPacketsSent"): _FakeResponse(
            200, _soap_response("GetTotalPacketsSent", SVC_WANCOMMON, {"NewTotalPacketsSent": str(pkts_sent)})
        ),
    }


# ---------------------------------------------------------------------------
# Low-level parsing: gatedesc.xml -> control URLs
# ---------------------------------------------------------------------------


def test_parse_gatedesc_good() -> None:
    urls = _parse_gatedesc(_gatedesc_xml(), LOCATION)
    assert urls == _ControlUrls(
        wan_common_ifc=f"http://{ROUTER_HOST}:1900/upnp/control/tok1234/WANCommonIFC1",
        wan_ip_conn=f"http://{ROUTER_HOST}:1900/upnp/control/tok1234/WANIPConn1",
    )


def test_parse_gatedesc_missing_wanipconnection_is_unexpected_service_list() -> None:
    data = _gatedesc_xml(include_wanipconn=False)
    with pytest.raises(UnexpectedServiceListError):
        _parse_gatedesc(data, LOCATION)


def test_parse_gatedesc_missing_wancommon_is_unexpected_service_list() -> None:
    data = _gatedesc_xml(include_wancommon=False)
    with pytest.raises(UnexpectedServiceListError):
        _parse_gatedesc(data, LOCATION)


def test_collect_service_control_urls_finds_nested_services() -> None:
    import xml.etree.ElementTree as ET

    root = ET.fromstring(_gatedesc_xml())
    services = _collect_service_control_urls(root, LOCATION)
    assert SVC_L3FWD in services
    assert SVC_WANCOMMON in services
    assert SVC_WANIPCONN in services
    assert services[SVC_WANIPCONN].startswith(f"http://{ROUTER_HOST}")


# ---------------------------------------------------------------------------
# Low-level parsing: XML defensiveness
# ---------------------------------------------------------------------------


def test_parse_xml_truncated_raises_malformed() -> None:
    with pytest.raises(MalformedXmlError):
        _parse_xml(TRUNCATED_XML, context="test")


def test_parse_xml_good_roundtrips() -> None:
    root = _parse_xml(_gatedesc_xml(), context="test")
    assert _local_name(root.tag) == "root"


# ---------------------------------------------------------------------------
# Low-level parsing: local-name matching survives the malformed namespace
# ---------------------------------------------------------------------------


def test_status_info_malformed_namespace_still_yields_uptime() -> None:
    root = _parse_xml(_status_info_response_malformed_ns(9_000_804), context="test")
    from housewatch.igd import _find_first_and_text

    assert _find_first_and_text(root, "NewUptime") == "9000804"
    assert _find_first_and_text(root, "NewConnectionStatus") == "Connected"


# ---------------------------------------------------------------------------
# Low-level parsing: ui4 field extraction
# ---------------------------------------------------------------------------


def test_parse_ui4_good() -> None:
    assert _parse_ui4("42", "field", "ctx") == 42
    assert _parse_ui4("0", "field", "ctx") == 0
    assert _parse_ui4(str(2**32 - 1), "field", "ctx") == 2**32 - 1


def test_parse_ui4_missing_field_raises() -> None:
    with pytest.raises(MissingFieldError):
        _parse_ui4(None, "NewTotalBytesReceived", "ctx")


@pytest.mark.parametrize("bad", ["-1", str(2**32), "not_a_number", ""])
def test_parse_ui4_out_of_range_or_non_numeric_raises(bad: str) -> None:
    with pytest.raises(CounterOutOfRangeError):
        _parse_ui4(bad, "field", "ctx")


def test_missing_field_response_via_full_soap_parse() -> None:
    # A syntactically valid SOAP response whose body simply lacks the field
    # the caller asked for — distinct from truncated/malformed XML.
    empty_body = _soap_response("GetTotalBytesReceived", SVC_WANCOMMON, {})
    root = _parse_xml(empty_body, context="test")
    from housewatch.igd import _find_first_and_text

    assert _find_first_and_text(root, "NewTotalBytesReceived") is None


# ---------------------------------------------------------------------------
# Low-level parsing: SOAP fault detection
# ---------------------------------------------------------------------------


def test_find_soap_fault_detects_fault() -> None:
    root = _parse_xml(_soap_fault_xml("401", "Invalid Action"), context="test")
    fault = _find_soap_fault(root)
    assert fault == ("401", "Invalid Action")


def test_find_soap_fault_none_on_normal_response() -> None:
    root = _parse_xml(
        _soap_response("GetTotalBytesReceived", SVC_WANCOMMON, {"NewTotalBytesReceived": "123"}),
        context="test",
    )
    assert _find_soap_fault(root) is None


# ---------------------------------------------------------------------------
# Low-level parsing: SSDP LOCATION header
# ---------------------------------------------------------------------------


def test_parse_location_header_case_insensitive() -> None:
    data = (
        b"HTTP/1.1 200 OK\r\n"
        b"CACHE-CONTROL: max-age=1800\r\n"
        b"location: http://192.0.2.1:1900/tok1234/gatedesc.xml\r\n"
        b"ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n"
        b"\r\n"
    )
    assert _parse_location_header(data) == LOCATION


def test_parse_location_header_missing_returns_none() -> None:
    data = b"HTTP/1.1 200 OK\r\nCACHE-CONTROL: max-age=1800\r\n\r\n"
    assert _parse_location_header(data) is None


# ---------------------------------------------------------------------------
# default_gateway() (macOS `route -n get default` parsing)
# ---------------------------------------------------------------------------


def test_default_gateway_parses_route_output(monkeypatch) -> None:
    proc = subprocess.CompletedProcess(
        args=["route", "-n", "get", "default"],
        returncode=0,
        stdout="   route to: default\ndestination: default\n    gateway: 192.0.2.1\n interface: en0\n",
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: proc)
    assert default_gateway() == "192.0.2.1"


def test_default_gateway_none_when_no_gateway_line(monkeypatch) -> None:
    proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="route to: default\n")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: proc)
    assert default_gateway() is None


def test_default_gateway_none_on_nonzero_exit(monkeypatch) -> None:
    proc = subprocess.CompletedProcess(args=[], returncode=1, stdout="")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: proc)
    assert default_gateway() is None


def test_default_gateway_none_on_oserror(monkeypatch) -> None:
    def _raise(*a, **k):
        raise OSError("no such command")

    monkeypatch.setattr(subprocess, "run", _raise)
    assert default_gateway() is None


# ---------------------------------------------------------------------------
# UpnpIgdSource.read() — full flow, no real network
# ---------------------------------------------------------------------------


def test_read_happy_path_cold_start(monkeypatch) -> None:
    """No cache at all: forces full SSDP discovery, then all five SOAP calls."""
    monkeypatch.setattr(igd, "_ssdp_search", lambda timeout_s: [LOCATION])
    get_responses = {LOCATION: _FakeResponse(200, _gatedesc_xml())}
    post_responses = _happy_soap_responses()
    _install_fake_session(monkeypatch, get_responses, post_responses)

    source = UpnpIgdSource(router_host=ROUTER_HOST)
    assert source.identity is None  # nothing discovered yet
    reading = source.read()

    assert isinstance(reading, Reading)
    assert reading.bytes_recv == 1_400_000_000
    assert reading.bytes_sent == 300_000_000
    assert reading.uptime_s == 9_000_804
    assert reading.pkts_recv == 900_000_000
    assert reading.pkts_sent == 450_000_000
    assert source.location == LOCATION
    assert source.describe() == f"UPnP IGD @ {ROUTER_HOST}"
    assert source.identity is not None
    assert source.identity.friendly_name == "Example Router"
    assert source.identity.manufacturer == "ExampleNet"
    assert source.identity.summary() == "Example Router (ExampleNet)"


def test_identity_survives_a_cached_control_url_read(monkeypatch) -> None:
    """A stage-1 read (cached control URLs, no gatedesc refetch at all)
    doesn't touch identity -- it should still reflect whatever the LAST
    successful discovery parsed, not reset to None."""
    monkeypatch.setattr(igd, "_ssdp_search", lambda timeout_s: [LOCATION])
    get_responses = {LOCATION: _FakeResponse(200, _gatedesc_xml())}
    post_responses = _happy_soap_responses()
    _install_fake_session(monkeypatch, get_responses, post_responses)

    source = UpnpIgdSource(router_host=ROUTER_HOST)
    source.read()
    identity_after_first_read = source.identity
    assert identity_after_first_read is not None

    source.read()  # second read: stage 1, cached control URLs, no GET at all
    assert source.identity == identity_after_first_read


def test_identity_stays_none_when_gatedesc_omits_every_field(monkeypatch) -> None:
    """A device tree with no <friendlyName>/<manufacturer>/etc. still reads
    successfully (identity is display-only and must never fail a poll) --
    it just renders as the documented all-unknown summary."""
    monkeypatch.setattr(igd, "_ssdp_search", lambda timeout_s: [LOCATION])
    bare_xml = (
        b'<?xml version="1.0"?>'
        b'<root xmlns="urn:schemas-upnp-org:device-1-0">'
        b"<device>"
        b"<deviceType>urn:schemas-upnp-org:device:InternetGatewayDevice:1</deviceType>"
        b'<serviceList><service><serviceType>' + SVC_WANCOMMON.encode() + b'</serviceType>'
        b"<controlURL>/upnp/control/tok1234/WANCommonIFC1</controlURL></service>"
        b'<service><serviceType>' + SVC_WANIPCONN.encode() + b'</serviceType>'
        b"<controlURL>/upnp/control/tok1234/WANIPConn1</controlURL></service></serviceList>"
        b"</device></root>"
    )
    get_responses = {LOCATION: _FakeResponse(200, bare_xml)}
    post_responses = _happy_soap_responses(
        wancommon_url=f"http://{ROUTER_HOST}:1900/upnp/control/tok1234/WANCommonIFC1",
        wanipconn_url=f"http://{ROUTER_HOST}:1900/upnp/control/tok1234/WANIPConn1",
    )
    _install_fake_session(monkeypatch, get_responses, post_responses)

    source = UpnpIgdSource(router_host=ROUTER_HOST)
    reading = source.read()  # must not raise -- identity parsing is TOTAL

    assert isinstance(reading, Reading)
    assert source.identity is not None
    assert source.identity.friendly_name is None
    assert source.identity.manufacturer is None
    assert source.identity.summary() == "unidentified UPnP device"


def test_read_cached_control_urls_skip_discovery(monkeypatch) -> None:
    """A warm source (control URLs already resolved) makes no GET/SSDP calls."""

    def _boom(*a, **k):
        raise AssertionError("discovery should not run when control URLs are cached")

    monkeypatch.setattr(igd, "_ssdp_search", _boom)
    post_responses = _happy_soap_responses()
    fake = _install_fake_session(monkeypatch, get_responses={}, post_responses=post_responses)

    source = UpnpIgdSource(router_host=ROUTER_HOST, cached_location=LOCATION)
    source._control_urls = _ControlUrls(
        wan_common_ifc=f"http://{ROUTER_HOST}:1900/upnp/control/tok1234/WANCommonIFC1",
        wan_ip_conn=f"http://{ROUTER_HOST}:1900/upnp/control/tok1234/WANIPConn1",
    )

    reading = source.read()
    assert reading.bytes_recv == 1_400_000_000
    assert fake.get_calls == []  # no gatedesc re-fetch, no discovery


def test_read_stale_control_url_recovers_via_refresh_from_location(monkeypatch) -> None:
    """Stage 1 (cached control URLs) 404s; stage 2 (re-GET cached LOCATION)
    hands back fresh control URLs which then succeed — without ever doing a
    full SSDP M-SEARCH.
    """

    def _boom(*a, **k):
        raise AssertionError("full SSDP discovery should not be needed here")

    monkeypatch.setattr(igd, "_ssdp_search", _boom)

    stale_wancommon = f"http://{ROUTER_HOST}/upnp/control/STALE/WANCommonIFC1"
    stale_wanipconn = f"http://{ROUTER_HOST}/upnp/control/STALE/WANIPConn1"
    fresh_wancommon = f"http://{ROUTER_HOST}:1900/upnp/control/tok1234/WANCommonIFC1"
    fresh_wanipconn = f"http://{ROUTER_HOST}:1900/upnp/control/tok1234/WANIPConn1"

    get_responses = {LOCATION: _FakeResponse(200, _gatedesc_xml())}  # fresh gatedesc, new token
    post_responses = {
        (stale_wancommon, "GetTotalBytesReceived"): _FakeResponse(404, b""),
        **_happy_soap_responses(wancommon_url=fresh_wancommon, wanipconn_url=fresh_wanipconn),
    }
    _install_fake_session(monkeypatch, get_responses, post_responses)

    source = UpnpIgdSource(router_host=ROUTER_HOST, cached_location=LOCATION)
    source._control_urls = _ControlUrls(wan_common_ifc=stale_wancommon, wan_ip_conn=stale_wanipconn)

    reading = source.read()
    assert reading.bytes_recv == 1_400_000_000
    assert source._control_urls.wan_common_ifc == fresh_wancommon


def test_read_falls_back_to_full_discovery_when_cached_location_is_dead(monkeypatch) -> None:
    """Stage 2 (re-GET cached LOCATION) fails outright; stage 3 (full SSDP)
    finds a new LOCATION and succeeds.
    """
    stale_location = f"http://{ROUTER_HOST}:1900/DEAD/gatedesc.xml"
    fresh_location = LOCATION

    monkeypatch.setattr(igd, "_ssdp_search", lambda timeout_s: [fresh_location])
    get_responses = {fresh_location: _FakeResponse(200, _gatedesc_xml())}  # stale_location deliberately absent -> ConnectionError
    post_responses = _happy_soap_responses()
    _install_fake_session(monkeypatch, get_responses, post_responses)

    source = UpnpIgdSource(router_host=ROUTER_HOST, cached_location=stale_location)
    reading = source.read()

    assert reading.bytes_recv == 1_400_000_000
    assert source.location == fresh_location


def test_read_raises_discovery_failed_when_ssdp_finds_nothing(monkeypatch) -> None:
    monkeypatch.setattr(igd, "_ssdp_search", lambda timeout_s: [])
    _install_fake_session(monkeypatch, {}, {})

    source = UpnpIgdSource(router_host=ROUTER_HOST)
    with pytest.raises(DiscoveryFailedError):
        source.read()
    # DiscoveryFailedError must still satisfy read()'s declared contract.
    with pytest.raises(SourceUnreachable):
        UpnpIgdSource(router_host=ROUTER_HOST).read()


def test_read_raises_discovery_failed_when_ssdp_finds_wrong_host(monkeypatch) -> None:
    other_location = "http://198.51.100.1:1900/xyz/gatedesc.xml"
    monkeypatch.setattr(igd, "_ssdp_search", lambda timeout_s: [other_location])
    _install_fake_session(monkeypatch, {}, {})

    source = UpnpIgdSource(router_host=ROUTER_HOST)
    with pytest.raises(DiscoveryFailedError):
        source.read()


def test_read_no_default_gateway_raises_without_any_network_call(monkeypatch) -> None:
    monkeypatch.setattr(igd, "default_gateway", lambda: None)

    def _boom(*a, **k):
        raise AssertionError("no network call should happen without a router_host")

    monkeypatch.setattr(igd, "_ssdp_search", _boom)
    monkeypatch.setattr(requests, "Session", lambda: (_ for _ in ()).throw(AssertionError("no session")))

    source = UpnpIgdSource(router_host=None)
    with pytest.raises(SourceUnreachable):
        source.read()


def test_read_soap_fault_on_mandatory_call_raises(monkeypatch) -> None:
    monkeypatch.setattr(igd, "_ssdp_search", lambda timeout_s: [LOCATION])
    wancommon_url = f"http://{ROUTER_HOST}:1900/upnp/control/tok1234/WANCommonIFC1"
    get_responses = {LOCATION: _FakeResponse(200, _gatedesc_xml())}
    post_responses = {
        (wancommon_url, "GetTotalBytesReceived"): _FakeResponse(500, _soap_fault_xml("501", "Action Failed")),
    }
    _install_fake_session(monkeypatch, get_responses, post_responses)

    source = UpnpIgdSource(router_host=ROUTER_HOST)
    with pytest.raises(SoapFaultError) as excinfo:
        source.read()
    assert excinfo.value.error_code == "501"
    assert excinfo.value.error_description == "Action Failed"
    assert isinstance(excinfo.value, SourceUnreachable)  # satisfies read()'s declared contract


def test_read_optional_packet_failure_degrades_to_none(monkeypatch) -> None:
    monkeypatch.setattr(igd, "_ssdp_search", lambda timeout_s: [LOCATION])
    wancommon_url = f"http://{ROUTER_HOST}:1900/upnp/control/tok1234/WANCommonIFC1"
    wanipconn_url = f"http://{ROUTER_HOST}:1900/upnp/control/tok1234/WANIPConn1"
    get_responses = {LOCATION: _FakeResponse(200, _gatedesc_xml())}
    post_responses = {
        (wancommon_url, "GetTotalBytesReceived"): _FakeResponse(
            200, _soap_response("GetTotalBytesReceived", SVC_WANCOMMON, {"NewTotalBytesReceived": "100"})
        ),
        (wancommon_url, "GetTotalBytesSent"): _FakeResponse(
            200, _soap_response("GetTotalBytesSent", SVC_WANCOMMON, {"NewTotalBytesSent": "200"})
        ),
        (wanipconn_url, "GetStatusInfo"): _FakeResponse(200, _status_info_response_malformed_ns(300)),
        (wancommon_url, "GetTotalPacketsReceived"): _FakeResponse(500, _soap_fault_xml("401", "Invalid Action")),
        (wancommon_url, "GetTotalPacketsSent"): _FakeResponse(500, _soap_fault_xml("401", "Invalid Action")),
    }
    _install_fake_session(monkeypatch, get_responses, post_responses)

    source = UpnpIgdSource(router_host=ROUTER_HOST)
    reading = source.read()

    assert reading.bytes_recv == 100
    assert reading.bytes_sent == 200
    assert reading.uptime_s == 300
    assert reading.pkts_recv is None
    assert reading.pkts_sent is None


def test_read_gatedesc_unexpected_service_list_propagates_as_source_error(monkeypatch) -> None:
    monkeypatch.setattr(igd, "_ssdp_search", lambda timeout_s: [LOCATION])
    get_responses = {LOCATION: _FakeResponse(200, _gatedesc_xml(include_wanipconn=False))}
    _install_fake_session(monkeypatch, get_responses, {})

    source = UpnpIgdSource(router_host=ROUTER_HOST)
    with pytest.raises(UnexpectedServiceListError):
        source.read()


def test_read_truncated_gatedesc_raises_malformed(monkeypatch) -> None:
    monkeypatch.setattr(igd, "_ssdp_search", lambda timeout_s: [LOCATION])
    get_responses = {LOCATION: _FakeResponse(200, TRUNCATED_XML)}
    _install_fake_session(monkeypatch, get_responses, {})

    source = UpnpIgdSource(router_host=ROUTER_HOST)
    with pytest.raises(MalformedXmlError):
        source.read()


def test_read_wrap_scenario_values_pass_through(monkeypatch) -> None:
    """Passes through the raw counters for a representative wrap scenario —
    the same down-direction shape used in test_counters.py (bytes
    3,700,000,000 -> 800,000,000, pkts 880,000,000 -> 881,400,000, an 804s
    gap; this test only supplies the *second* reading's values, since it's
    checking pass-through, not the wrap arithmetic itself) — proving igd.py
    hands raw counters through untouched. The wrap/reboot arithmetic is
    counters.py's job, not this module's.
    """
    wancommon_url = f"http://{ROUTER_HOST}:1900/upnp/control/tok1234/WANCommonIFC1"
    wanipconn_url = f"http://{ROUTER_HOST}:1900/upnp/control/tok1234/WANIPConn1"
    monkeypatch.setattr(igd, "_ssdp_search", lambda timeout_s: [LOCATION])
    get_responses = {LOCATION: _FakeResponse(200, _gatedesc_xml())}
    post_responses = _happy_soap_responses(
        bytes_recv=800_000_000,
        bytes_sent=300_000_000,
        uptime=9_000_804,
        pkts_recv=881_400_000,
        pkts_sent=450_000_000,
        wancommon_url=wancommon_url,
        wanipconn_url=wanipconn_url,
    )
    _install_fake_session(monkeypatch, get_responses, post_responses)

    source = UpnpIgdSource(router_host=ROUTER_HOST)
    reading = source.read()
    assert reading.bytes_recv == 800_000_000
    assert reading.pkts_recv == 881_400_000
    assert reading.uptime_s == 9_000_804


# ---------------------------------------------------------------------------
# Live check — real network, real router.
#
# OPT-IN ONLY: set HOUSEWATCH_LIVE_ROUTER_TEST=1 to run it. Everything else
# in this suite is fully offline (monkeypatched SOAP/SSDP, canned XML), and
# a default `pytest` run must stay that way — a test that silently opens a
# TCP connection to a specific LAN address when it happens to be on the
# right network makes the suite behave differently in different places,
# which is exactly what a test suite must not do. Even opted in, it still
# self-skips when the router is unreachable.
# ---------------------------------------------------------------------------

LIVE_ROUTER_ENV = "HOUSEWATCH_LIVE_ROUTER_TEST"
# Optional: pin a specific router IP for the live check. Unset, the check
# auto-detects the machine's own default gateway — never a hardcoded LAN
# address, so this file carries no fingerprint of whatever network it was
# last run against.
LIVE_ROUTER_HOST_ENV = "HOUSEWATCH_LIVE_ROUTER_HOST"


def _live_router_host() -> str | None:
    if os.environ.get(LIVE_ROUTER_ENV) != "1":
        return None
    explicit = os.environ.get(LIVE_ROUTER_HOST_ENV, "").strip()
    if explicit:
        return explicit
    return default_gateway()


def _router_reachable(port: int = 1900, timeout_s: float = 1.0) -> bool:
    host = _live_router_host()
    if host is None:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


@pytest.mark.skipif(
    not _router_reachable(),
    reason=(
        f"set {LIVE_ROUTER_ENV}=1 (optionally {LIVE_ROUTER_HOST_ENV}=<ip> to "
        "pin a router; otherwise the machine's own default gateway is "
        "auto-detected) and be on that router's network to run the live check"
    ),
)
def test_live_read_against_real_router() -> None:
    host = _live_router_host()
    assert host is not None  # guaranteed by the skipif guard above
    source = UpnpIgdSource(router_host=host)
    reading = source.read()

    assert 0 <= reading.bytes_recv <= 2**32 - 1
    assert 0 <= reading.bytes_sent <= 2**32 - 1
    assert reading.uptime_s >= 0
    assert source.location is not None
    assert source.location.startswith(f"http://{host}:1900/")

    print(
        "\nLIVE housewatch igd.read() against",
        host,
        "\n  location   =", source.location,
        "\n  bytes_recv =", reading.bytes_recv,
        "\n  bytes_sent =", reading.bytes_sent,
        "\n  uptime_s   =", reading.uptime_s,
        "\n  pkts_recv  =", reading.pkts_recv,
        "\n  pkts_sent  =", reading.pkts_sent,
        "\n  ts         =", reading.ts,
    )
