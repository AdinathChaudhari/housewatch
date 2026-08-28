"""The daemon loop: polls the WAN counter source on a schedule, classifies
every reading through ``counters.classify``, and persists the outcome via
``store.Store``. This module owns the *scheduling and failure-handling*
policy; it deliberately contains no wrap/reboot arithmetic (that is
``counters.py``'s job) and no SQL (that is ``store.py``'s job) — it only
wires clock reads, the source, the classifier, and the store together.

Design choices worth stating up front
--------------------------------------
* **Drift-corrected scheduling.** The loop does not ``sleep(interval)``
  after every poll (which would slowly drift by however long each poll
  takes) — it computes an absolute next-deadline (``last_scheduled +
  interval``) and sleeps only the remainder. A poll that overran its slot
  causes the *next* wait to be skipped/shortened rather than the schedule
  slipping forever.
* **Sleep/wake and long gaps are not special-cased here.** If wall-clock
  time jumps far ahead of one interval (the Mac slept, Wi-Fi was down,
  ``Event.wait`` simply oversleeps through system sleep), the very next
  successful ``source.read()`` produces a ``Reading`` whose timestamp is
  however far in the future it really is. ``counters.classify`` already
  turns a big gap into 'gap'/'recovered'/'suspect' — correctly bounding or
  admitting the unknown — so the *only* thing this loop must do is never
  try to catch up with a burst of back-to-back immediate polls once it
  notices the deadline is long past; it simply re-anchors to
  ``now + interval`` and moves on (see the re-anchor arithmetic in
  ``run_forever``).
* **Exponential backoff on failure, capped, and reset on success.** A
  router that is temporarily unreachable (Wi-Fi off, VPN up, reboot) must
  not be hammered every ``poll_interval_s``; the wait doubles up to a fixed
  ceiling and collapses back to the configured interval the moment a poll
  succeeds again.
* **Events, not spam.** Every failed poll is logged, but only two events
  ever land in the store for a failure run: one when the run crosses the
  "this is now a notable outage" threshold, one when it recovers. A router
  blip that fails once and recovers next poll produces zero rows in
  ``events`` — the failure counter still resets, just silently.
* **The baseline always advances.** Per ``counters.classify``'s contract,
  the caller (here) sets the new baseline (``store.record_poll`` writes
  ``meta['last_reading']``) after *every* successful poll, regardless of
  what kind the interval classified as — classification decides only what
  the interval row says, never whether to advance.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Callable

from housewatch import config as config_mod
from housewatch import counters
from housewatch import igd
from housewatch.config import Config
from housewatch.model import IntervalResult, Reading, SourceError, WanCounterSource
from housewatch.store import Store

logger = logging.getLogger(__name__)

# Ceiling for the exponential poll-failure backoff. Chosen to bound how
# stale "last sample age" can get during an outage without hammering a
# router that is legitimately down (Wi-Fi off, VPN full-tunnel, reboot);
# every scenario in the spec's gap policy tolerates minutes-to-hours of
# outage gracefully via the 'gap' classification, so 60s of poll spacing
# during an outage costs nothing in correctness, only in how quickly the
# next successful poll happens to land after connectivity returns.
MAX_BACKOFF_S: float = 60.0

# Consecutive poll failures before an outage is worth an events-table row.
# At the default 10s poll interval this is ~30s of downtime — long enough
# that it is not just a single dropped UDP-ish SOAP call, short enough that
# a real outage is flagged well before it becomes a multi-minute gap.
FAILURE_EVENT_THRESHOLD: int = 3

# How often (in seconds of simulated/real wall time) the paused-loop check
# re-polls the pause Event. Small enough that "Resume" in the menubar feels
# immediate; irrelevant to correctness since pausing never touches the
# classifier or the store.
PAUSE_POLL_S: float = 1.0

# meta key recording that this router's packet counters were caught
# disagreeing with its byte counters about what a "packet" is. Sticky on
# purpose: once proven wrong, the packet-recovery tier stays off across
# restarts until a human clears the key.
PACKET_CALIBRATION_META_KEY = "packet_recovery_disabled"

# How much evidence the frame-size calibration needs before it renders a
# verdict, per direction.
#
# These are large ON PURPOSE. igd.read() fetches the byte counters and the
# packet counters in separate SOAP calls, so within one interval whatever
# crosses the WAN between those calls lands in one counter and not the
# other. Measured on one router during development at a 3s poll on bursty
# traffic, back-to-back intervals read 27.4 and 30,400.0 bytes/packet -- both
# nonsense, both artifacts of that skew, and their sum (1203.5) the truth.
# A single interval's ratio is therefore NOT a measurement. Accumulating
# until the window holds this many packets makes the skew (a bounded offset
# contributed once at each end of the window) a few percent, well inside
# counters.CALIBRATION_TOLERANCE.
#
# On a typical home line the down window fills in a few minutes and the up
# window in tens of minutes -- down and up carry very different volumes.
# Until then no verdict is rendered, which
# is the correct behaviour: "not enough evidence yet" is not "all clear".
CALIBRATION_MIN_PACKETS = 100_000
CALIBRATION_MIN_INTERVALS = 20


def build_source(cfg: Config, store: Store) -> WanCounterSource:
    """Construct the configured WAN counter source.

    Today this is always ``igd.UpnpIgdSource``; the ``model.WanCounterSource``
    protocol exists precisely so a future per-device source can be swapped
    in here without collector.py changing at all. ``cached_location`` is
    read from the store's persisted meta so a warm restart skips a full
    SSDP round-trip when the router hasn't rebooted since the last run.
    """
    return igd.UpnpIgdSource(
        router_host=cfg.router_host,
        http_timeout_s=cfg.http_timeout_s,
        ssdp_timeout_s=cfg.ssdp_timeout_s,
        cached_location=store.get_meta("last_location"),
    )


def _thresholds_from_config(
    cfg: Config, packet_recovery_enabled: bool = True
) -> counters.Thresholds:
    """Derive ``counters.Thresholds`` from ``cfg.max_link_mbps``.

    Every other threshold field keeps ``counters.Thresholds``'s own default
    (they are frame-size/protocol physics, not user-configurable — see that
    dataclass's docstring) — only the byte-rate ceiling is a function of the
    configured planning rate. ``packet_recovery_enabled`` is not from config
    either: it is a MEASURED verdict about this router's firmware, supplied
    by the caller from persisted calibration state.
    """
    return counters.Thresholds(
        max_bytes_per_sec=cfg.max_link_mbps * 1_000_000.0 / 8.0,
        packet_recovery_enabled=packet_recovery_enabled,
    )


class Collector:
    """Owns the single read-write ``Store`` connection and the WAN source,
    and runs the poll loop. Not thread-safe internally — exactly one thread
    (the daemon loop) is expected to call ``poll_once``/``run_forever``.
    """

    def __init__(
        self,
        cfg: Config,
        source: WanCounterSource,
        store: Store,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.source = source
        self.store = store
        self.clock = clock
        self._sleeper = sleeper
        self._stop: threading.Event | None = None

        # Packet recovery is only sound if this router's byte and packet
        # counters share accounting units. A previous run may already have
        # proven they do not (see _calibrate_packet_units); that verdict is
        # persisted and re-applied here so a restart cannot quietly re-enable
        # a tier that was disabled for cause.
        packet_recovery_enabled = True
        try:
            packet_recovery_enabled = store.get_meta(PACKET_CALIBRATION_META_KEY) != "1"
        except Exception:  # pragma: no cover - defensive; get_meta never raises today
            pass
        self.thresholds = _thresholds_from_config(cfg, packet_recovery_enabled)

        # Frame-size calibration accumulators, per direction:
        # [total_bytes, total_pkts, contributing_intervals]. In memory only --
        # a restart just restarts the measurement window, which delays a
        # verdict but never fabricates one. The VERDICT itself is persisted
        # (PACKET_CALIBRATION_META_KEY), so a proven-bad router stays bad.
        self._calib_down = [0, 0, 0]
        self._calib_up = [0, 0, 0]

        self._consecutive_failures = 0
        self._failure_event_logged = False
        self._last_location: str | None = None
        self._last_identity: str | None = None
        try:
            self._last_location = store.get_meta("last_location")
            self._last_identity = store.get_meta("router_identity")
        except Exception:  # pragma: no cover - defensive; get_meta itself never raises today
            self._last_location = None
            self._last_identity = None

    # ------------------------------------------------------------------
    # One poll
    # ------------------------------------------------------------------

    def poll_once(self) -> IntervalResult | None:
        """Read the source once, classify against the stored baseline, and
        persist the outcome. Returns the ``IntervalResult`` on success, or
        ``None`` if the source read itself failed (``SourceError`` and its
        subclasses — the only exceptions this method swallows; anything
        else is a bug and propagates to the caller).
        """
        try:
            curr = self.source.read()
        except SourceError as exc:
            self._handle_failure(exc)
            return None
        return self._handle_success(curr)

    def _handle_success(self, curr: Reading) -> IntervalResult:
        prev = self.store.last_reading()
        interval = counters.classify(prev, curr, self.thresholds)
        tz = config_mod.tzinfo_of(self.cfg)
        self.store.record_poll(curr, interval, tz)

        if prev is not None:
            self._calibrate_packet_units(prev, curr, interval)

        location = getattr(self.source, "location", None)
        if location is not None and location != self._last_location:
            self.store.set_meta("last_location", location)
            if self._last_location is not None:
                # Only an actual CHANGE from a previously-known location is a
                # "discovery re-run" transition worth an events row (a
                # rebooted router got a new random gatedesc path). The very
                # first poll's None -> location assignment is not a
                # transition — it is simply "we now know where the router
                # is", already implied by 'collector_start'.
                self.store.record_event(
                    curr.ts, "discovery", f"IGD location changed to {location}"
                )
            self._last_location = location

        # Router identity, from the device description the source already
        # fetched. Display-only: `status` and `doctor` show it so a user can
        # confirm the tool is reading the device they think it is, and so a bug
        # report says which hardware it came from. It must never influence
        # classification, and a source that cannot supply one is not an error.
        #
        # Same baseline rule as last_location above: the first observation is
        # "we now know what the router is", not a transition worth an events
        # row. Only a genuine change gets logged — that means the device on the
        # other end of the gateway address was swapped, which is worth knowing.
        identity_obj = getattr(self.source, "identity", None)
        identity = identity_obj.summary() if identity_obj is not None else None
        if identity is not None and identity != self._last_identity:
            self.store.set_meta("router_identity", identity)
            if self._last_identity is not None:
                self.store.record_event(
                    curr.ts, "discovery", f"router identity changed to {identity}"
                )
            self._last_identity = identity

        if self._failure_event_logged:
            self.store.record_event(
                curr.ts,
                "recovered",
                f"poll succeeded after {self._consecutive_failures} consecutive failures",
            )
            self._failure_event_logged = False
        self._consecutive_failures = 0

        if interval.kind == "reset":
            self.store.record_event(curr.ts, "reset", interval.note)
        elif interval.kind == "anomaly":
            self.store.record_event(curr.ts, "anomaly", interval.note)

        return interval

    def _calibrate_packet_units(
        self, prev: Reading, curr: Reading, interval: IntervalResult
    ) -> None:
        """Test the packet-envelope's frame-size assumption against reality.

        ``counters.py``'s 'recovered' tier calls its answer PROOF, but that
        proof is conditional on an assumption about firmware nobody
        verified: that the byte and packet counters count the same events,
        with every packet between 64 and 1518 bytes. Violate it -- a SoC
        reporting GRO/LRO-coalesced super-packets is the realistic way --
        and the envelope still returns exactly one confident candidate, just
        the wrong one. Measured: a 300 s interval carrying 30.0 GB at 9000
        bytes/packet comes back 'recovered' with 4.23 GB, six wraps missing,
        with no suspect flag on it.

        So the assumption gets measured. 'ok' intervals are the one tier
        where BOTH deltas are provably exact, so they are the only ones that
        contribute. Crucially the ratio is judged on an ACCUMULATED window,
        never on one interval: the byte and packet counters come from
        separate SOAP calls, and on bursty traffic that read skew swamps a
        single interval's ratio completely (measured live: 27.4 then
        30,400.0 bytes/packet on consecutive 3 s intervals whose combined
        ratio was a perfectly ordinary 1203.5). Judging per-interval would
        disable packet recovery on a healthy router within a minute, which
        an earlier version of this method did.

        Once a full window's ratio lands outside the tolerated band, the two
        counters demonstrably do not share units and packet recovery is
        switched off for good (persisted, with an events row). Those
        intervals then degrade to 'suspect'/'gap' -- honest -- instead of a
        proven-looking lie. Never re-enables: firmware that coalesces
        sometimes is exactly as unusable as firmware that always does.
        """
        if not self.thresholds.packet_recovery_enabled:
            return
        contribution = counters.calibration_contribution(prev, curr, interval)
        if contribution is None:
            return
        down_bytes, down_pkts, up_bytes, up_pkts = contribution

        for label, acc, delta_bytes, delta_pkts in (
            ("down", self._calib_down, down_bytes, down_pkts),
            ("up", self._calib_up, up_bytes, up_pkts),
        ):
            acc[0] += delta_bytes
            acc[1] += delta_pkts
            acc[2] += 1
            if acc[1] < CALIBRATION_MIN_PACKETS or acc[2] < CALIBRATION_MIN_INTERVALS:
                continue  # not enough evidence yet; keep accumulating

            ratio = counters.implausible_bytes_per_packet(acc[0], acc[1], self.thresholds)
            observed = acc[0] / acc[1]
            windows = (acc[1], acc[2])
            acc[0] = acc[1] = acc[2] = 0  # start a fresh window either way
            if ratio is None:
                logger.debug(
                    "packet calibration ok (%s): %.1f bytes/packet over %d packets "
                    "in %d intervals",
                    label, observed, windows[0], windows[1],
                )
                continue

            detail = (
                f"router {label} counters report {ratio:.0f} bytes/packet measured over "
                f"{windows[0]:,} packets in {windows[1]} provably-exact intervals, outside the "
                f"[{self.thresholds.min_pkt_bytes}, {self.thresholds.max_pkt_bytes}] Ethernet "
                f"frame range the packet-recovery envelope assumes. Byte and packet counters do "
                f"not share accounting units; packet recovery is now disabled and long gaps will "
                f"classify as suspect/gap instead of recovered."
            )
            logger.error("packet recovery disabled: %s", detail)
            self.store.set_meta(PACKET_CALIBRATION_META_KEY, "1")
            self.store.record_event(curr.ts, "calibration", detail)
            self.thresholds = _thresholds_from_config(self.cfg, packet_recovery_enabled=False)
            return

    def _handle_failure(self, exc: SourceError) -> None:
        self._consecutive_failures += 1
        logger.warning(
            "poll failed (%d consecutive): %s", self._consecutive_failures, exc
        )
        if self._consecutive_failures >= FAILURE_EVENT_THRESHOLD and not self._failure_event_logged:
            self._failure_event_logged = True
            self.store.record_event(
                self.clock(),
                "failures",
                f"{self._consecutive_failures} consecutive poll failures: {exc}",
            )

    # ------------------------------------------------------------------
    # The loop
    # ------------------------------------------------------------------

    def _wait(self, seconds: float) -> None:
        """Sleep for ``seconds``, or until ``stop`` is set, whichever first.

        When no ``sleeper`` was injected (the real/production path), this
        waits on the ``threading.Event`` passed to ``run_forever`` — so a
        SIGTERM/SIGINT handler calling ``stop.set()`` wakes an in-progress
        wait immediately instead of the process sitting out the rest of a
        (possibly 60s-backed-off) sleep before it notices it should exit.
        """
        seconds = max(0.0, seconds)
        if self._sleeper is not None:
            self._sleeper(seconds)
        else:
            assert self._stop is not None
            self._stop.wait(seconds)

    def _maybe_prune(self, last_prune_hour: int | None) -> int | None:
        """Run ``store.prune`` once per local-hour change. Returns the hour
        that should be remembered as "already pruned" for the next call.
        Failures are logged and swallowed — pruning is housekeeping, never
        worth taking the daemon down over.
        """
        try:
            tz = config_mod.tzinfo_of(self.cfg)
            now = self.clock()
            hour = datetime.fromtimestamp(now, tz).hour
            if hour != last_prune_hour:
                self.store.prune(now, self.cfg)
                return hour
        except Exception:
            logger.exception("prune failed; continuing")
        return last_prune_hour

    def run_forever(
        self, stop: threading.Event, pause: threading.Event | None = None
    ) -> None:
        """Block, polling on the configured cadence, until ``stop`` is set.

        Scheduling: ``next_deadline`` starts at "now" (poll immediately on
        start) and advances by exactly ``poll_interval_s`` after each
        successful poll — *unless* that arithmetic deadline already lies in
        the past by the time the poll finished (a poll that overran its
        slot, or a system sleep/wake that swallowed real time), in which
        case it re-anchors to ``now + poll_interval_s`` rather than firing
        immediately over and over to "catch up". A failed poll instead
        backs off exponentially (see ``MAX_BACKOFF_S``).

        Pausing: while ``pause`` is set, the loop wakes every
        ``PAUSE_POLL_S`` and polls nothing — the schedule is not advanced
        and no failure/backoff state changes — so polling resumes on a
        fresh, undistorted cadence the moment ``pause`` clears.

        Every exception raised out of one iteration's body (a bug in this
        code, not a routine ``SourceError``) is caught, logged, and the loop
        continues — this daemon must never die silently, only on ``stop``.
        """
        self._stop = stop
        interval = self.cfg.poll_interval_s
        backoff = interval
        next_deadline = self.clock()
        last_prune_hour: int | None = None

        self.store.record_event(self.clock(), "collector_start", self.source.describe())
        try:
            while not stop.is_set():
                if pause is not None and pause.is_set():
                    self._wait(PAUSE_POLL_S)
                    continue

                now = self.clock()
                if now < next_deadline:
                    self._wait(next_deadline - now)
                    continue  # re-check stop/pause before touching the network

                try:
                    result = self.poll_once()
                except Exception:
                    logger.exception("unhandled exception in poll loop; continuing")
                    result = None

                if result is None:
                    backoff = min(backoff * 2.0, MAX_BACKOFF_S)
                    next_deadline = self.clock() + backoff
                else:
                    backoff = interval
                    candidate = next_deadline + interval
                    next_deadline = candidate if candidate > self.clock() else self.clock() + interval

                last_prune_hour = self._maybe_prune(last_prune_hour)
        finally:
            self.store.record_event(self.clock(), "collector_stop", "")
