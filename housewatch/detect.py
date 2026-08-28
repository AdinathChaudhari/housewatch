"""Environment auto-detection: default gateway, router identity, system timezone.

A leaf module by design: it imports nothing from ``housewatch.*``, so
``igd.py`` (which needs ``default_gateway``) and ``config.py`` (which needs
``system_timezone``) can both import FROM here without an import cycle.
Every public function here is TOTAL -- it returns its documented "unknown"
value (``None``, or an all-``None`` :class:`RouterIdentity`) instead of
raising. Detection failure is always "degrade to a documented default and
say so"; the *saying* belongs to the caller, the degrading belongs here.

Anything that shells out or touches the network is isolated behind an
injectable seam (``run=`` on :func:`default_gateway`) or takes its input as
plain bytes/mappings (:func:`parse_router_identity`,
:func:`system_timezone`), so tests exercise every branch with no real
subprocess and no real network.

Explicitly NOT in this module: link-speed detection. A router's own
reported Layer1 up/downstream bitrates are placeholder values on at least
some firmware (observed during development), so ``max_link_mbps`` stays a
manual, floored setting (see ``config.MIN_SAFE_LINK_MBPS`` and the
``i_know_my_link_is_slower`` opt-in) rather than something this module
measures. A future suggestion mechanism built on top of this module may
only ever propose RAISING that ceiling, never lowering it silently --
lowering it silently would widen every wrap-certainty window in
``counters.py`` without the user noticing.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Router identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RouterIdentity:
    """What a router called itself in ``gatedesc.xml``, for display only.

    Never used for anything that affects correctness (routing, discovery,
    counter math) -- purely so a user can confirm "yes, that's my router"
    and so a bug report carries a device identity instead of a bare IP.
    """

    friendly_name: str | None
    manufacturer: str | None
    model_name: str | None
    model_number: str | None

    def summary(self) -> str:
        """Render as ``"<head> (<remaining details>)"``, never empty.

        ``friendly_name`` heads the summary when present; the parenthetical
        then lists manufacturer, model name, and model number (dropping
        ``None`` parts, and dropping model number when it duplicates model
        name). When ``friendly_name`` is missing, model name is promoted to
        the head instead (then manufacturer, if model name is also
        missing), and whatever is left forms the parenthetical. When every
        field is ``None`` this returns the literal string
        ``"unidentified UPnP device"`` rather than an empty string.
        """
        model_number = self.model_number
        if model_number is not None and model_number == self.model_name:
            model_number = None

        fields: dict[str, str | None] = {
            "friendly_name": self.friendly_name,
            "model_name": self.model_name,
            "manufacturer": self.manufacturer,
            "model_number": model_number,
        }

        head_order = ("friendly_name", "model_name", "manufacturer", "model_number")
        head_key = next((k for k in head_order if fields[k]), None)
        if head_key is None:
            return "unidentified UPnP device"

        head = fields[head_key]
        assert head is not None  # narrowed by head_key lookup above

        paren_order = ("manufacturer", "model_name", "model_number")
        parens = [fields[k] for k in paren_order if k != head_key and fields[k]]
        if parens:
            return f"{head} ({' '.join(str(p) for p in parens)})"
        return head


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
            text = (child.text or "").strip()
            return text or None
    return None


def parse_router_identity(gatedesc_xml: bytes) -> RouterIdentity:
    """Extract friendlyName/manufacturer/modelName/modelNumber from ``gatedesc.xml``.

    PURE: bytes in, dataclass out -- no I/O, no clock. Takes the FIRST
    ``<device>`` element in document order, which is the root device;
    nested ``WANDevice``/``WANConnectionDevice`` elements come later in the
    tree. Matches elements by LOCAL tag name only -- the same defensive rule
    ``igd.py`` uses for SOAP parsing, because some firmware ships malformed
    or missing XML namespaces in these documents, so namespace-strict
    lookups are forbidden here too.

    TOTAL: malformed XML, a document with no ``<device>``, or missing
    fields all yield ``None`` fields rather than raising. Identity is
    display-only, so it must be IMPOSSIBLE for this to fail a poll.
    """
    try:
        root = ET.fromstring(gatedesc_xml)
    except ET.ParseError as exc:
        logger.debug("parse_router_identity: malformed gatedesc.xml: %s", exc)
        return RouterIdentity(None, None, None, None)

    device_el = _find_first_by_local_name(root, "device")
    if device_el is None:
        logger.debug("parse_router_identity: no <device> element found")
        return RouterIdentity(None, None, None, None)

    return RouterIdentity(
        friendly_name=_child_text_by_local_name(device_el, "friendlyName"),
        manufacturer=_child_text_by_local_name(device_el, "manufacturer"),
        model_name=_child_text_by_local_name(device_el, "modelName"),
        model_number=_child_text_by_local_name(device_el, "modelNumber"),
    )


# ---------------------------------------------------------------------------
# Default gateway discovery
# ---------------------------------------------------------------------------


def _run_subprocess(cmd: list[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(cmd, capture_output=True, text=True, timeout=5.0)


def default_gateway(
    *,
    platform: str | None = None,
    run: Callable[[list[str]], "subprocess.CompletedProcess[str]"] | None = None,
) -> str | None:
    """Return the machine's default IPv4 gateway address, or None.

    Never assumed, never hardcoded: this is the only way ``igd.py`` learns
    a router address when the user hasn't pinned one in config.

    ``platform`` defaults to ``sys.platform`` and ``run`` defaults to a
    private wrapper around ``subprocess.run`` -- both are seams so tests
    never spawn a real process.

    - macOS (``"darwin"``): runs ``route -n get default`` and parses the
      ``gateway:`` line.
    - Linux (``"linux"``, preparing the planned Raspberry Pi port): runs
      ``ip -4 route show default`` and parses the token after
      ``"default via"``.
    - Anything else: unsupported, returns None.

    Returns None -- NEVER raises -- on an unsupported platform, a failed
    spawn (``OSError``), a timeout, a non-zero exit, or output with no
    parseable gateway. The returned string is not validated as an IP; a
    hostname gateway is legal. Callers treat None as "cannot resolve a
    router host right now": ``igd.UpnpIgdSource._resolve_router_host``
    raises ``SourceUnreachable`` with an explanatory message rather than
    guessing an address.
    """
    plat = platform if platform is not None else sys.platform
    runner = run if run is not None else _run_subprocess

    if plat == "darwin":
        cmd = ["route", "-n", "get", "default"]
    elif plat == "linux":
        cmd = ["ip", "-4", "route", "show", "default"]
    else:
        logger.debug("default_gateway: unsupported platform %r, cannot detect", plat)
        return None

    try:
        proc = runner(cmd)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("default_gateway: %s failed to run: %s", cmd[0], exc)
        return None

    if proc.returncode != 0:
        logger.debug("default_gateway: %s exited %d", cmd[0], proc.returncode)
        return None

    if plat == "darwin":
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line.startswith("gateway:"):
                gw = line.split(":", 1)[1].strip()
                return gw or None
        return None

    # linux: "default via 192.0.2.1 dev eth0 proto dhcp metric 100"
    for line in proc.stdout.splitlines():
        tokens = line.split()
        if "via" not in tokens:
            continue
        idx = tokens.index("via")
        if idx + 1 >= len(tokens):
            continue
        gw = tokens[idx + 1].strip()
        if gw:
            return gw
    return None


# ---------------------------------------------------------------------------
# System timezone
# ---------------------------------------------------------------------------


def system_timezone(
    *,
    environ: Mapping[str, str] | None = None,
    localtime_path: Path | None = None,
) -> str | None:
    """Return the host's IANA zone name, or None if it cannot be determined.

    Checks ``$TZ`` first (only if it names a resolvable IANA zone), then
    resolves the ``/etc/localtime`` symlink (macOS points it into
    ``.../zoneinfo/...``, Linux typically into
    ``/usr/share/zoneinfo/...``) and takes the path suffix after the
    ``zoneinfo`` path component, validating it via ``ZoneInfo``. Returns
    None rather than guessing; never raises.

    ``environ``/``localtime_path`` default to ``os.environ`` and
    ``Path("/etc/localtime")`` -- seams so tests run with no real
    environment or filesystem dependency. Degrade path when this returns
    None is owned by the caller: ``config.tzinfo_of`` logs a WARNING and
    falls back to a fixed UTC offset sampled at call time.
    """
    env = environ if environ is not None else os.environ
    ltpath = localtime_path if localtime_path is not None else Path("/etc/localtime")

    tz_env = env.get("TZ", "").strip()
    if tz_env:
        try:
            ZoneInfo(tz_env)
        except (ZoneInfoNotFoundError, ValueError):
            pass
        else:
            return tz_env

    try:
        resolved = ltpath.resolve()
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
