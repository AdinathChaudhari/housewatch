"""UPnP IGD client — the concrete ``model.WanCounterSource`` implementation.

Talks to a router's UPnP Internet Gateway Device service to read cumulative
WAN byte/packet counters and connection uptime. This works against any
IGD-compliant router, not one specific model — everything here is I/O and
parsing; the wrap/reboot arithmetic that turns raw counters into usable
deltas lives in ``counters.py`` and is deliberately kept out of this
module.

Why discovery, every time
--------------------------
``gatedesc.xml`` and every ``controlURL`` live behind a path segment (a
LOCATION URL typically looks like
``http://192.0.2.1:1900/<token>/gatedesc.xml``, using an RFC 5737 example
address here) that looks like a per-boot random token on at least some
firmware. It is **never** hardcoded here: every control URL this class
uses was either handed in by the caller as a previously-cached value, or
was derived, this run, from a LOCATION this class itself fetched and
parsed. See ``_full_discovery`` and ``_refresh_from_location``.

Bounded retry ladder
---------------------
``read()`` tries at most three ways to get a coherent snapshot, cheapest
first, and never loops beyond that:

1. **Cached control URLs** (from a prior successful discovery this
   process). Cheapest — no SSDP, no gatedesc fetch, straight to the SOAP
   calls.
2. **Re-fetch gatedesc.xml from the cached LOCATION** and rebuild control
   URLs. Cheap — one HTTP GET, no multicast — and is what fixes a stale
   control URL after the router quietly re-rendered gatedesc.xml without
   actually rebooting.
3. **Full SSDP M-SEARCH** to find LOCATION again from scratch, then fetch
   and parse gatedesc.xml. This is what recovers from an actual reboot,
   which is the one event that changes the random path token.

Each stage that successfully produces control URLs is followed by exactly
one attempt at the real poll; a stage is only entered when the one before
it failed. That bounds total network activity per ``read()`` call to at
most one SSDP burst, one or two gatedesc GETs, and one set of SOAP calls —
never a hot loop against the router.

Dependency note
----------------
The task brief that accompanies this file says "stdlib only (socket,
urllib, xml.etree)". This module uses ``requests`` instead, because the
architect's spec for this exact file calls for "a new requests.Session per
read; no keep-alive assumptions" and ``requests>=2.31`` is a declared
dependency in this project's ``pyproject.toml`` (already installed in the
target venv). Where those two instructions conflict, the more specific,
file-scoped one wins; ``xml.etree.ElementTree`` and ``socket`` (for raw
SSDP, which ``requests`` cannot do) are still stdlib as required.
"""

from __future__ import annotations

import logging
import socket
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urljoin, urlsplit

import requests

from housewatch.detect import RouterIdentity, default_gateway, parse_router_identity
from housewatch.model import Reading, SourceError, SourceParseError, SourceUnreachable

logger = logging.getLogger("housewatch.igd")

# ---------------------------------------------------------------------------
# UPnP constants — these service-type URNs are the standard IGD profile and
# are NOT per-router-boot secrets like the gatedesc/controlURL path segment
# is. Hardcoding these is correct; hardcoding a controlURL path is not.
# ---------------------------------------------------------------------------

IGD_DEVICE_TYPE = "urn:schemas-upnp-org:device:InternetGatewayDevice:1"
SVC_WAN_COMMON_IFC = "urn:schemas-upnp-org:service:WANCommonInterfaceConfig:1"
SVC_WAN_IP_CONN = "urn:schemas-upnp-org:service:WANIPConnection:1"

SSDP_MCAST_ADDR = "239.255.255.250"
SSDP_MCAST_PORT = 1900
SSDP_MX = 2

UI4_MAX = 2**32 - 1


# ---------------------------------------------------------------------------
# Typed exceptions
#
# model.py defines the two exceptions read() is contractually allowed to
# raise: SourceUnreachable (connectivity-shaped failures) and
# SourceParseError (the router responded but the content was not usable).
# The task brief additionally asks for typed distinctions the collector can
# use for logging/diagnostics: "router unreachable, discovery failed, SOAP
# fault (surface errorCode/errorDescription), malformed XML". Those four are
# represented as subclasses below so callers that only catch the two base
# types per the model.py contract still work unchanged, while callers that
# want finer detail can catch the specific subclass.
# ---------------------------------------------------------------------------


class DiscoveryFailedError(SourceUnreachable):
    """SSDP M-SEARCH produced no usable IGD LOCATION for this router_host."""


class SoapFaultError(SourceUnreachable):
    """The router accepted the SOAP request but returned a UPnP Fault.

    Carries the raw ``errorCode``/``errorDescription`` from the fault body
    (or the SOAP faultstring, if a UPnPError detail block is absent) so the
    caller can log the router's own explanation rather than a bare "failed".
    """

    def __init__(self, service_type: str, action: str, error_code: str, error_description: str) -> None:
        self.service_type = service_type
        self.action = action
        self.error_code = error_code
        self.error_description = error_description
        super().__init__(
            f"SOAP fault calling {action} on {service_type}: "
            f"errorCode={error_code} errorDescription={error_description!r}"
        )


class MalformedXmlError(SourceParseError):
    """Response body did not parse as XML at all (truncated, non-XML, etc.)."""


class MissingFieldError(SourceParseError):
    """Response parsed as XML but a required field was absent."""


class UnexpectedServiceListError(SourceParseError):
    """gatedesc.xml parsed but did not contain the services housewatch needs."""


class CounterOutOfRangeError(SourceParseError):
    """A counter value parsed but fell outside the legal ui4 range [0, 2**32)."""


# ---------------------------------------------------------------------------
# Default gateway discovery
#
# The actual implementation lives in housewatch.detect (a leaf module with
# no housewatch.* imports of its own, which is what lets both igd.py and
# config.py depend on it without an import cycle -- igd needs
# default_gateway, config needs system_timezone). Imported above and
# re-exported here under its original name, so `from housewatch.igd import
# default_gateway` and `igd.default_gateway(...)` keep working unchanged
# for every existing caller and test.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Namespace-agnostic XML helpers
#
# Defensive rule, not a one-router quirk: some firmware ships a malformed
# namespace in GetStatusInfoResponse (observed in the wild: declaring
# xmlns:u="urn:schemas-upnp-org:service:GetStatusInfo:1" instead of the
# correct WANIPConnection:1 service type, on one TP-Link Aginet unit during
# development). A namespace-strict parser (e.g. ET.find with a qualified
# tag) breaks on that response. Every lookup below therefore matches on the
# LOCAL tag name only, ignoring whatever namespace URI (if any) the element
# actually carries — do not "fix" this into a namespace-strict lookup.
# ---------------------------------------------------------------------------


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_first_by_local_name(root: ET.Element, name: str) -> ET.Element | None:
    for el in root.iter():
        if _local_name(el.tag) == name:
            return el
    return None


def _child_text_by_local_name(el: ET.Element, name: str) -> str | None:
    for child in el:
        if _local_name(child.tag) == name:
            return (child.text or "").strip()
    return None


def _parse_xml(data: bytes, context: str) -> ET.Element:
    """Parse ``data`` as XML, defensively.

    Uses the stdlib's default expat-backed parser with no custom entity
    handlers installed — this code path never registers a DOCTYPE/entity
    resolver, so it never fetches an external entity or expands a
    router-supplied DTD. A home router's XML is otherwise untrusted:
    any parse failure becomes a typed, catchable error instead of an
    uncaught exception.
    """
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise MalformedXmlError(f"malformed XML from {context}: {exc}") from exc


def _parse_ui4(text: str | None, field: str, context: str) -> int:
    if text is None:
        raise MissingFieldError(f"{context}: field {field!r} missing from response")
    try:
        value = int(text)
    except ValueError as exc:
        raise CounterOutOfRangeError(f"{context}: field {field!r} = {text!r} is not an integer") from exc
    if not (0 <= value <= UI4_MAX):
        raise CounterOutOfRangeError(f"{context}: field {field!r} = {value} outside ui4 range [0, {UI4_MAX}]")
    return value


def _find_soap_fault(root: ET.Element) -> tuple[str, str] | None:
    """Return (errorCode, errorDescription) if ``root`` is a SOAP Fault, else None."""
    fault_el = _find_first_by_local_name(root, "Fault")
    if fault_el is None:
        return None
    code: str | None = None
    desc: str | None = None
    faultstring: str | None = None
    for el in fault_el.iter():
        name = _local_name(el.tag)
        if name == "errorCode" and code is None:
            code = (el.text or "").strip()
        elif name == "errorDescription" and desc is None:
            desc = (el.text or "").strip()
        elif name == "faultstring" and faultstring is None:
            faultstring = (el.text or "").strip()
    return (code or "unknown", desc or faultstring or "unknown UPnP fault")


# ---------------------------------------------------------------------------
# gatedesc.xml -> control URLs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ControlUrls:
    wan_common_ifc: str
    wan_ip_conn: str


def _collect_service_control_urls(root: ET.Element, location: str) -> dict[str, str]:
    """Walk every ``<service>`` element regardless of nesting/namespace.

    Real IGD device trees nest WAN services a few levels down
    (root device -> WANDevice -> WANConnectionDevice -> service), so this
    deliberately does not assume a fixed depth; it just iterates every
    element in the tree and picks out ``service`` by local name.
    """
    out: dict[str, str] = {}
    for service_el in root.iter():
        if _local_name(service_el.tag) != "service":
            continue
        service_type = _child_text_by_local_name(service_el, "serviceType")
        control_url = _child_text_by_local_name(service_el, "controlURL")
        if not service_type or not control_url:
            continue
        # controlURL is commonly relative (e.g. "/upnp/control/<token>/WANIPConn1");
        # resolve against LOCATION so both relative and absolute forms work.
        out[service_type] = urljoin(location, control_url)
    return out


def _parse_gatedesc(data: bytes, location: str) -> _ControlUrls:
    root = _parse_xml(data, context=location)
    services = _collect_service_control_urls(root, location)
    missing = [st for st in (SVC_WAN_COMMON_IFC, SVC_WAN_IP_CONN) if st not in services]
    if missing:
        raise UnexpectedServiceListError(
            f"{location}: gatedesc.xml service list missing {missing}; "
            f"found service types: {sorted(services)}"
        )
    return _ControlUrls(
        wan_common_ifc=services[SVC_WAN_COMMON_IFC],
        wan_ip_conn=services[SVC_WAN_IP_CONN],
    )


# ---------------------------------------------------------------------------
# SSDP discovery
# ---------------------------------------------------------------------------


def _ssdp_search(ssdp_timeout_s: float) -> list[str]:
    """Broadcast one M-SEARCH and collect every LOCATION URL that answers.

    Blocks for up to ``ssdp_timeout_s`` total (bounded by both the socket
    timeout and a wall-clock deadline so a router that keeps answering
    cannot extend the wait past the requested window).
    """
    request = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_MCAST_ADDR}:{SSDP_MCAST_PORT}\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"MX: {SSDP_MX}\r\n"
        f"ST: {IGD_DEVICE_TYPE}\r\n"
        "\r\n"
    ).encode("ascii")

    locations: list[str] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        deadline = time.monotonic() + ssdp_timeout_s
        sock.settimeout(ssdp_timeout_s)
        sock.sendto(request, (SSDP_MCAST_ADDR, SSDP_MCAST_PORT))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                data, _addr = sock.recvfrom(65535)
            except socket.timeout:
                break
            except OSError:
                break
            loc = _parse_location_header(data)
            if loc:
                locations.append(loc)
    finally:
        sock.close()
    return locations


def _parse_location_header(data: bytes) -> str | None:
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return None
    for line in text.split("\r\n"):
        if line.lower().startswith("location:"):
            return line.split(":", 1)[1].strip()
    return None


# ---------------------------------------------------------------------------
# SOAP
# ---------------------------------------------------------------------------

_SOAP_ENVELOPE_TEMPLATE = (
    '<?xml version="1.0"?>'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
    's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
    "<s:Body>"
    '<u:{action} xmlns:u="{service_type}"></u:{action}>'
    "</s:Body></s:Envelope>"
)


def _soap_call(
    session: requests.Session,
    control_url: str,
    service_type: str,
    action: str,
    timeout_s: float,
) -> ET.Element:
    """POST one SOAP action, return the parsed response body Envelope.

    Raises SourceUnreachable for anything connectivity-shaped (timeout,
    connection refused, a bare 404 meaning the control URL is stale),
    SoapFaultError for a well-formed UPnP Fault response, and
    MalformedXmlError if the body doesn't parse as XML at all.
    """
    body = _SOAP_ENVELOPE_TEMPLATE.format(action=action, service_type=service_type)
    headers = {
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPAction": f'"{service_type}#{action}"',
    }
    try:
        resp = session.post(control_url, data=body.encode("utf-8"), headers=headers, timeout=timeout_s)
    except requests.RequestException as exc:
        raise SourceUnreachable(f"POST {control_url} ({action}) failed: {exc}") from exc

    if resp.status_code == 404:
        raise SourceUnreachable(
            f"POST {control_url} ({action}) -> 404; control URL is likely stale (router rebooted?)"
        )

    # A UPnP Fault normally rides on HTTP 500 but the body is what actually
    # carries the meaning — parse it before deciding the HTTP status matters.
    try:
        root = _parse_xml(resp.content, context=f"{control_url} ({action})")
    except MalformedXmlError:
        if resp.status_code >= 400:
            raise SourceUnreachable(
                f"POST {control_url} ({action}) -> HTTP {resp.status_code} with unparseable body"
            ) from None
        raise

    fault = _find_soap_fault(root)
    if fault is not None:
        code, desc = fault
        raise SoapFaultError(service_type, action, code, desc)

    if resp.status_code >= 400:
        raise SourceUnreachable(
            f"POST {control_url} ({action}) -> HTTP {resp.status_code}, no SOAP fault body found"
        )

    return root


def _soap_field(
    session: requests.Session,
    control_url: str,
    service_type: str,
    action: str,
    field: str,
    timeout_s: float,
) -> str | None:
    root = _soap_call(session, control_url, service_type, action, timeout_s)
    return _find_first_and_text(root, field)


def _find_first_and_text(root: ET.Element, field: str) -> str | None:
    el = _find_first_by_local_name(root, field)
    if el is None:
        return None
    return (el.text or "").strip()


def _http_get(session: requests.Session, url: str, timeout_s: float) -> bytes:
    try:
        resp = session.get(url, timeout=timeout_s)
    except requests.RequestException as exc:
        raise SourceUnreachable(f"GET {url} failed: {exc}") from exc
    if resp.status_code == 404:
        raise SourceUnreachable(f"GET {url} -> 404 (LOCATION is likely stale; router rebooted?)")
    if resp.status_code >= 400:
        raise SourceUnreachable(f"GET {url} -> HTTP {resp.status_code}")
    return resp.content


# ---------------------------------------------------------------------------
# The source
# ---------------------------------------------------------------------------


class UpnpIgdSource:
    """``model.WanCounterSource`` backed by the router's UPnP IGD service.

    Not thread-safe — the collector owns exactly one instance and calls
    ``read()`` from its single poll loop, per the house's single-writer
    discipline.
    """

    def __init__(
        self,
        router_host: str | None = None,
        http_timeout_s: float = 5.0,
        ssdp_timeout_s: float = 3.0,
        cached_location: str | None = None,
    ) -> None:
        self._router_host = router_host
        self._http_timeout_s = http_timeout_s
        self._ssdp_timeout_s = ssdp_timeout_s
        self._location: str | None = cached_location
        self._control_urls: _ControlUrls | None = None
        self._identity: RouterIdentity | None = None
        self._last_pkt_warning_ts: float = 0.0

    @property
    def location(self) -> str | None:
        """Currently cached LOCATION URL, or None before the first successful discovery."""
        return self._location

    @property
    def identity(self) -> RouterIdentity | None:
        """The router's self-reported identity, or None before any successful
        discovery has parsed a gatedesc.xml. Display-only -- never affects
        routing, discovery, or counter math; see RouterIdentity's docstring.
        """
        return self._identity

    def describe(self) -> str:
        host = self._router_host or "auto-detected gateway"
        return f"UPnP IGD @ {host}"

    # -- discovery stages ---------------------------------------------------

    def _resolve_router_host(self) -> str:
        if self._router_host is not None:
            return self._router_host
        gw = default_gateway()
        if gw is None:
            raise SourceUnreachable(
                "no default IPv4 gateway found (no route to a router — "
                "off-network, or a full-tunnel VPN with no local default route)"
            )
        self._router_host = gw
        return gw

    def _refresh_from_location(self) -> None:
        """Stage 2: re-fetch gatedesc.xml from the already-cached LOCATION."""
        assert self._location is not None
        with requests.Session() as session:
            data = _http_get(session, self._location, self._http_timeout_s)
        self._control_urls = _parse_gatedesc(data, self._location)
        # Display-only and TOTAL (see RouterIdentity/parse_router_identity):
        # this can never fail a poll, so it runs unconditionally right after
        # a successful gatedesc parse, not gated behind its own try/except.
        self._identity = parse_router_identity(data)

    def _full_discovery(self) -> None:
        """Stage 3: SSDP M-SEARCH from scratch, then fetch and parse gatedesc.xml."""
        router_host = self._resolve_router_host()
        locations = _ssdp_search(self._ssdp_timeout_s)
        if not locations:
            raise DiscoveryFailedError(
                f"SSDP M-SEARCH for {IGD_DEVICE_TYPE} got no responses within {self._ssdp_timeout_s}s"
            )
        matched = [loc for loc in locations if urlsplit(loc).hostname == router_host]
        if not matched:
            raise DiscoveryFailedError(
                f"SSDP found {len(locations)} IGD response(s) but none from router_host="
                f"{router_host!r}: {locations}"
            )
        location = matched[0]
        with requests.Session() as session:
            data = _http_get(session, location, self._http_timeout_s)
        control_urls = _parse_gatedesc(data, location)
        # Only commit state once both the fetch and the parse succeeded, so a
        # failed attempt never leaves _location and _control_urls mismatched.
        self._location = location
        self._control_urls = control_urls
        self._identity = parse_router_identity(data)

    # -- the actual poll ------------------------------------------------------

    def _do_read(self) -> Reading:
        """Run the fixed SOAP call sequence against the currently-cached control URLs.

        Order matters: the three mandatory calls happen first and any
        failure among them fails the whole read; the two optional packet
        calls happen last and degrade to None on failure without affecting
        the mandatory fields. A fresh ``requests.Session`` per read means no
        connection is kept alive between calls or across reads — deliberate,
        since miniupnpd (the UPnP daemon this firmware almost certainly
        runs) is known to close idle connections rather than keep them
        warm, so pooling would just buy reconnect churn for no benefit.
        """
        assert self._control_urls is not None
        urls = self._control_urls
        with requests.Session() as session:
            bytes_recv_text = _soap_field(
                session, urls.wan_common_ifc, SVC_WAN_COMMON_IFC,
                "GetTotalBytesReceived", "NewTotalBytesReceived", self._http_timeout_s,
            )
            bytes_recv = _parse_ui4(bytes_recv_text, "NewTotalBytesReceived", urls.wan_common_ifc)

            bytes_sent_text = _soap_field(
                session, urls.wan_common_ifc, SVC_WAN_COMMON_IFC,
                "GetTotalBytesSent", "NewTotalBytesSent", self._http_timeout_s,
            )
            # Captured immediately after GetTotalBytesSent returns, per spec:
            # the collector correlates this ts against uptime_s below, and
            # every SOAP round trip after this point widens that correlation
            # window, so the clock read happens as early as it can.
            ts = time.time()
            bytes_sent = _parse_ui4(bytes_sent_text, "NewTotalBytesSent", urls.wan_common_ifc)

            uptime_text = _soap_field(
                session, urls.wan_ip_conn, SVC_WAN_IP_CONN,
                "GetStatusInfo", "NewUptime", self._http_timeout_s,
            )
            uptime_s = _parse_ui4(uptime_text, "NewUptime", urls.wan_ip_conn)

            # Optional calls share the same session/connection window as the
            # mandatory ones — closing early here (before these ran) was a
            # bug caught during self-review: it would have made every packet
            # fetch open a fresh connection right after the session claimed
            # to be done, which is exactly the "no keep-alive assumptions"
            # session-per-read contract broken from the inside.
            pkts_recv = self._optional_packet_count(
                session, urls.wan_common_ifc, "GetTotalPacketsReceived", "NewTotalPacketsReceived"
            )
            pkts_sent = self._optional_packet_count(
                session, urls.wan_common_ifc, "GetTotalPacketsSent", "NewTotalPacketsSent"
            )

        return Reading(
            ts=ts,
            bytes_recv=bytes_recv,
            bytes_sent=bytes_sent,
            uptime_s=uptime_s,
            pkts_recv=pkts_recv,
            pkts_sent=pkts_sent,
        )

    def _optional_packet_count(
        self, session: requests.Session, control_url: str, action: str, field: str
    ) -> int | None:
        """Best-effort packet counter fetch. Never raises; None on any failure.

        Logs at most once per hour so a router that has simply never
        implemented these two actions doesn't spam the log forever — the
        collector still gets pkts=None on every such read.
        """
        try:
            text = _soap_field(session, control_url, SVC_WAN_COMMON_IFC, action, field, self._http_timeout_s)
            return _parse_ui4(text, field, control_url)
        except SourceError as exc:
            now = time.monotonic()
            if now - self._last_pkt_warning_ts >= 3600.0:
                self._last_pkt_warning_ts = now
                logger.warning("optional packet counter %s unavailable: %s", action, exc)
            return None

    # -- public API -----------------------------------------------------------

    def read(self) -> Reading:
        """Return one coherent Reading, retrying through the discovery ladder.

        Bounded to at most three attempts at the real poll (one per stage
        below); each stage is only attempted after the previous one failed,
        so a healthy router with warm cache costs exactly one attempt and a
        rebooted router costs at most three, never an unbounded loop.
        """
        stages: list[Callable[[], None]] = []
        if self._control_urls is not None:
            stages.append(lambda: None)  # stage 1: use the cache as-is
        if self._location is not None:
            stages.append(self._refresh_from_location)  # stage 2
        stages.append(self._full_discovery)  # stage 3: always available as a last resort

        last_error: SourceError | None = None
        for prepare in stages:
            try:
                prepare()
            except SourceError as exc:
                last_error = exc
                continue
            if self._control_urls is None:
                # Should be unreachable (every stage either raises or sets
                # _control_urls) but guard against a silent no-op stage.
                continue
            try:
                return self._do_read()
            except SourceError as exc:
                last_error = exc
                continue

        assert last_error is not None
        raise last_error
