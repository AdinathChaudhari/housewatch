"""Shared pure types and the pluggable-source contract.

Zero I/O, zero third-party imports. This module exists so that ``counters.py``
can stay a pure function of its arguments — the single property that makes the
wrap/reboot state machine exhaustively unit-testable — and so that a future
per-device data source only has to satisfy :class:`WanCounterSource` rather
than touch the collector.

Named ``model.py`` rather than ``types.py`` to avoid shadowing the stdlib
``types`` module on the import path.

The one invariant worth stating loudly, because every number the user ever sees
depends on it: **unknown is never encoded as zero**. When a delta cannot be
recovered — two or more counter wraps between polls, or a gap longer than the
certainty window — the corresponding field on :class:`IntervalResult` is
``None``, and every consumer is obliged to handle that rather than sum it into
a total. A household data cap defended with silently-zeroed unknowns is worse
than no measurement at all, because it reads as authoritative.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

__all__ = [
    "Kind",
    "Reading",
    "IntervalResult",
    "SourceError",
    "SourceUnreachable",
    "SourceParseError",
    "WanCounterSource",
]


# The classification assigned to one polling interval by ``counters.classify``.
#
#   baseline  — first-ever sample, or the first after a reset; no delta yet
#   ok        — clean monotonic increase, no wrap
#   ok_wrap   — exactly one wrap, unambiguously resolved
#   recovered — wrap count was ambiguous from bytes alone but the packet
#               counters constrained it to a single answer
#   suspect   — a delta was computed but the interval is long enough that a
#               missed wrap is possible; the number is a lower bound
#   gap       — interval too long to say anything; bytes are None
#   reset     — router reboot / WAN reconnect detected via uptime going
#               backwards; counters restart from a fresh baseline
#   anomaly   — the reading contradicts itself (out-of-range value, uptime
#               moving impossibly); bytes are None
Kind = Literal[
    "baseline",
    "ok",
    "ok_wrap",
    "recovered",
    "suspect",
    "gap",
    "reset",
    "anomaly",
]


@dataclass(frozen=True)
class Reading:
    """One raw snapshot of the router's WAN counters.

    Field values are the router's raw ui4 counters, *not* deltas — they wrap at
    ``2**32`` and mean nothing in isolation. Interpretation is
    ``counters.classify``'s job.
    """

    ts: float
    """UTC epoch seconds, captured immediately after the last byte-counter
    SOAP call returns. The individual SOAP calls are not atomic with respect to
    each other, so this is the close of a short window, not an instant."""

    bytes_recv: int
    """Downstream: ``WANCommonInterfaceConfig`` ``NewTotalBytesReceived``."""

    bytes_sent: int
    """Upstream: ``WANCommonInterfaceConfig`` ``NewTotalBytesSent``."""

    uptime_s: int
    """``WANIPConnection`` ``GetStatusInfo`` ``NewUptime`` — WAN connection
    uptime. This is the discriminator that tells a counter wrap apart from a
    router reboot, so it is mandatory: a Reading with an unknown uptime must
    never be constructed."""

    pkts_recv: int | None
    """Raw ui4 packet counter, or ``None`` if the optional SOAP call failed.
    Optional because packet counters are only used to disambiguate wrap counts;
    losing them degrades ``recovered`` outcomes to ``suspect``, nothing worse."""

    pkts_sent: int | None
    """As :attr:`pkts_recv`, upstream."""


@dataclass(frozen=True)
class IntervalResult:
    """What happened between two consecutive readings.

    ``down_bytes``/``up_bytes`` are ``None`` whenever the true delta is not
    recoverable. Consumers must branch on that; they must not coerce it to 0.
    """

    kind: Kind
    start_ts: float
    """Equal to the previous reading's ``ts`` — and to ``end_ts`` for
    ``baseline``, where there is no interval yet."""

    end_ts: float
    """Equal to the current reading's ``ts``."""

    down_bytes: int | None
    """Bytes downstream over the interval, or ``None`` for UNKNOWN."""

    up_bytes: int | None
    """Bytes upstream over the interval, or ``None`` for UNKNOWN."""

    down_wraps: int | None
    """How many ``2**32`` wraps were added to produce :attr:`down_bytes`.
    ``None`` exactly when :attr:`down_bytes` is ``None``."""

    up_wraps: int | None
    """As :attr:`down_wraps`, upstream."""

    note: str
    """Human-readable detail naming the branch that produced this result, so a
    surprising row in the database can be explained after the fact."""


class SourceError(Exception):
    """Base class for every failure of a counter source."""


class SourceUnreachable(SourceError):
    """The router could not be reached at all.

    SSDP discovery found nothing, the TCP connection was refused, or a request
    timed out. Routine and expected: the Mac sleeps, Wi-Fi drops, and a VPN can
    put the LAN gateway out of reach. The collector backs off and retries; it
    does not treat this as data.
    """


class SourceParseError(SourceError):
    """The router answered, but the answer was not usable.

    Malformed XML, a missing field, a SOAP fault, or a counter value outside
    the ui4 range. Distinguished from :class:`SourceUnreachable` because it
    usually means a firmware change rather than a transient network problem,
    and so warrants a louder log line.
    """


@runtime_checkable
class WanCounterSource(Protocol):
    """The contract a counter source must satisfy.

    ``igd.IgdSource`` is the only implementation today. Keeping the collector
    written against this Protocol is what makes the planned move to an
    always-on host — or a future per-device source — a substitution rather than
    a rewrite.
    """

    def read(self) -> Reading:
        """Return one coherent snapshot.

        Must be all-or-nothing for the mandatory fields (``bytes_recv``,
        ``bytes_sent``, ``uptime_s``): either a complete Reading, or a raised
        :class:`SourceError`. Never a Reading with a guessed uptime.

        Raises:
            SourceUnreachable: router not reachable.
            SourceParseError: response present but unusable.
        """
        ...

    def describe(self) -> str:
        """Short human-readable identification, e.g. ``'UPnP IGD @ 192.0.2.1'``."""
        ...
