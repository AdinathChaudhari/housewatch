"""Tests for collector.py — the daemon loop.

No real time ever passes in these tests: every clock read is a plain
``FakeClock`` object under direct test control, and every "sleep" is a
``FakeSleeper`` that just advances that clock and returns immediately. This
is what lets the backoff-doubling and sleep-gap scenarios (which would
otherwise take a minute-plus of wall time) run in milliseconds while still
exercising the real scheduling arithmetic in ``Collector.run_forever``.

``FakeSource`` is the whole point of ``model.WanCounterSource`` being a
Protocol: it drives ``Collector`` through a scripted sequence of successful
reads and injected ``SourceError``s without touching a network at all.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from housewatch.collector import (
    CALIBRATION_MIN_INTERVALS,
    CALIBRATION_MIN_PACKETS,
    PACKET_CALIBRATION_META_KEY,
    FAILURE_EVENT_THRESHOLD,
    MAX_BACKOFF_S,
    Collector,
    build_source,
)
from housewatch.config import Config
from housewatch.igd import UpnpIgdSource
from housewatch.model import Reading, SourceUnreachable
from housewatch.store import Store


class FakeClock:
    """A mutable "now" under direct test control — no real time involved."""

    def __init__(self, start: float) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class FakeSleeper:
    """Records every requested wait and advances a FakeClock instead of
    actually blocking, so scheduling logic (backoff, drift-corrected
    cadence) runs at test speed."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.advance(seconds)


class FakeSource:
    """Scripted ``model.WanCounterSource``. Each script entry is either an
    ``Exception`` instance (raised) or a dict of ``Reading`` fields minus
    ``ts`` (``ts`` is stamped from the injected clock at call time, exactly
    like the real ``UpnpIgdSource`` stamps it right after its last mandatory
    SOAP call returns).

    Sets a caller-supplied ``threading.Event`` the moment the script is
    exhausted, from inside the very call that pops the last entry — this is
    what lets ``run_forever`` tests stop deterministically right after the
    last scripted poll, without a separate iteration-counting mechanism that
    could drift out of sync with the scheduler's own logic.
    """

    def __init__(self, clock: FakeClock, script: list, stop_when_exhausted: threading.Event | None = None) -> None:
        self._clock = clock
        self._script = list(script)
        self._stop = stop_when_exhausted
        self.calls = 0
        self.location = "http://192.0.2.1:1900/fake/gatedesc.xml"

    def read(self) -> Reading:
        self.calls += 1
        if not self._script:
            raise AssertionError("FakeSource script exhausted but read() called again")
        item = self._script.pop(0)
        if not self._script and self._stop is not None:
            self._stop.set()
        if isinstance(item, Exception):
            raise item
        fields = dict(item)
        fields.setdefault("ts", self._clock())
        return Reading(**fields)

    def describe(self) -> str:
        return "FakeSource"


@pytest.fixture
def store(tmp_db_path: Path) -> Store:
    s = Store(tmp_db_path, readonly=False)
    s.init_schema()
    yield s
    s.close()


# ---------------------------------------------------------------------------
# Normal polling
# ---------------------------------------------------------------------------


def test_normal_polling_persists_baseline_then_ok(tmp_config: Config, store: Store):
    clock = FakeClock(1_000.0)
    source = FakeSource(
        clock,
        [
            dict(bytes_recv=1_000_000, bytes_sent=500_000, uptime_s=100, pkts_recv=1000, pkts_sent=800, ts=1_000.0),
            dict(bytes_recv=1_100_000, bytes_sent=520_000, uptime_s=105, pkts_recv=1100, pkts_sent=820, ts=1_005.0),
            dict(bytes_recv=1_150_000, bytes_sent=530_000, uptime_s=110, pkts_recv=1150, pkts_sent=830, ts=1_010.0),
        ],
    )
    collector = Collector(tmp_config, source, store, clock=clock)

    r1 = collector.poll_once()
    r2 = collector.poll_once()

    assert r1.kind == "baseline"
    assert r1.down_bytes is None and r1.up_bytes is None

    assert r2.kind == "ok"
    assert r2.down_bytes == 100_000
    assert r2.up_bytes == 20_000
    assert r2.down_wraps == 0 and r2.up_wraps == 0

    last = store.last_reading()
    assert last is not None
    assert last.bytes_recv == 1_100_000 and last.ts == 1_005.0

    # The first poll's None -> FakeSource's fixed location is not a change
    # worth an events row (it's "we now know where the router is", already
    # implied by 'collector_start') — see test_discovery_event_on_location_change
    # for the real-change case. Normal, steady-state polling produces zero
    # events.
    assert store.recent_events(10) == []

    collector.poll_once()  # location still unchanged -> still no event
    assert store.recent_events(10) == []


def test_discovery_event_on_location_change(tmp_config: Config, store: Store):
    """A real relocation (router rebooted, per-boot random gatedesc token
    changed) between two successful polls IS a transition worth an events
    row — unlike the first poll's None -> location assignment."""
    clock = FakeClock(1_000.0)
    source = FakeSource(
        clock,
        [
            dict(bytes_recv=1_000, bytes_sent=500, uptime_s=10, pkts_recv=1, pkts_sent=1, ts=1_000.0),
            dict(bytes_recv=2_000, bytes_sent=600, uptime_s=20, pkts_recv=2, pkts_sent=2, ts=1_010.0),
        ],
    )
    collector = Collector(tmp_config, source, store, clock=clock)
    collector.poll_once()
    assert store.recent_events(10) == []

    source.location = "http://192.0.2.1:1900/rebooted/gatedesc.xml"
    collector.poll_once()

    events = store.recent_events(10)
    assert [e[1] for e in events] == ["discovery"]
    assert store.get_meta("last_location") == source.location


# ---------------------------------------------------------------------------
# A counter wrap
# ---------------------------------------------------------------------------


def test_counter_wrap_is_resolved_and_flagged(tmp_config: Config, store: Store):
    modulus = 2**32
    clock = FakeClock(2_000.0)
    prev_bytes = modulus - 1_000  # 4294966296
    curr_bytes = 500  # wrapped past 0
    source = FakeSource(
        clock,
        [
            dict(bytes_recv=prev_bytes, bytes_sent=200_000, uptime_s=50_000, pkts_recv=10, pkts_sent=10, ts=2_000.0),
            # 5s gap, well inside the certainty window (34.36s @1Gbps) — the
            # single-wrap correction is provably exact here.
            dict(bytes_recv=curr_bytes, bytes_sent=210_000, uptime_s=50_005, pkts_recv=20, pkts_sent=20, ts=2_005.0),
        ],
    )
    collector = Collector(tmp_config, source, store, clock=clock)

    collector.poll_once()  # baseline
    result = collector.poll_once()

    assert result.kind == "ok_wrap"
    assert result.down_bytes == 1_500  # (500 - 4294966296) mod 2**32
    assert result.down_wraps == 1
    assert result.up_bytes == 10_000
    assert result.up_wraps == 0

    last = store.last_reading()
    assert last.bytes_recv == curr_bytes  # baseline advanced despite the wrap


# ---------------------------------------------------------------------------
# A router reboot
# ---------------------------------------------------------------------------


def test_reboot_is_detected_and_events_recorded(tmp_config: Config, store: Store):
    clock = FakeClock(3_000.0)
    source = FakeSource(
        clock,
        [
            dict(bytes_recv=9_000_000, bytes_sent=4_000_000, uptime_s=100_000, pkts_recv=5000, pkts_sent=4000, ts=3_000.0),
            # Uptime dropped hard — this is a reboot, not a wrap, no matter
            # what the byte counters look like.
            dict(bytes_recv=10_000, bytes_sent=5_000, uptime_s=30, pkts_recv=5, pkts_sent=3, ts=3_010.0),
        ],
    )
    collector = Collector(tmp_config, source, store, clock=clock)

    collector.poll_once()  # baseline
    result = collector.poll_once()

    assert result.kind == "reset"
    assert result.down_bytes is None and result.up_bytes is None

    last = store.last_reading()
    assert last is not None and last.uptime_s == 30  # baseline advances even on reset

    events = store.recent_events(10)
    kinds = [e[1] for e in events]
    assert "reset" in kinds


# ---------------------------------------------------------------------------
# Unreachable router: backoff, then recovery
# ---------------------------------------------------------------------------


def test_unreachable_router_backs_off_and_recovers(tmp_config: Config, store: Store):
    clock = FakeClock(10_000.0)
    stop = threading.Event()
    script = [
        SourceUnreachable("router unreachable (1)"),
        SourceUnreachable("router unreachable (2)"),
        SourceUnreachable("router unreachable (3)"),
        dict(bytes_recv=1_000_000, bytes_sent=500_000, uptime_s=200, pkts_recv=100, pkts_sent=90),
        dict(bytes_recv=1_050_000, bytes_sent=520_000, uptime_s=210, pkts_recv=110, pkts_sent=95),
    ]
    source = FakeSource(clock, script, stop_when_exhausted=stop)
    sleeper = FakeSleeper(clock)
    collector = Collector(tmp_config, source, store, clock=clock, sleeper=sleeper)

    assert tmp_config.poll_interval_s == 10.0  # the doubling sequence below assumes this

    collector.run_forever(stop)

    # Backoff doubles on each failure (10 -> 20 -> 40 -> 60, capped at
    # MAX_BACKOFF_S), then collapses straight back to the configured
    # interval the instant a poll succeeds.
    assert sleeper.calls == [20.0, 40.0, 60.0, 10.0]
    assert max(sleeper.calls) <= MAX_BACKOFF_S

    assert source.calls == 5  # exactly the scripted reads, no extra polling after exhaustion

    kinds = [e[1] for e in store.recent_events(50)]
    assert kinds.count("collector_start") == 1
    assert kinds.count("collector_stop") == 1
    assert kinds.count("failures") == 1  # one event at the 3rd consecutive failure, not 3
    assert kinds.count("recovered") == 1  # one event on the poll that ended the outage

    last = store.last_reading()
    assert last is not None and last.bytes_recv == 1_050_000

    # Two real samples were persisted (the baseline + the one 'ok' interval
    # after it) despite three failed polls in between producing no rows.
    rows = store.notable_intervals(0, clock() + 1, kinds=("baseline", "ok", "ok_wrap", "recovered", "suspect"))
    assert [r.kind for r in rows] == ["baseline", "ok"]


def test_failure_threshold_constant_is_reasonable():
    # Sanity guard on the module constant the test above hardcodes the
    # expectation of exactly one 'failures' event around.
    assert FAILURE_EVENT_THRESHOLD == 3


# ---------------------------------------------------------------------------
# Simulated sleep gap
# ---------------------------------------------------------------------------


def test_long_sleep_gap_is_never_treated_as_one_huge_delta(tmp_config: Config, store: Store):
    clock = FakeClock(50_000.0)
    source = FakeSource(
        clock,
        [
            dict(bytes_recv=1_000_000, bytes_sent=500_000, uptime_s=300, pkts_recv=None, pkts_sent=None, ts=50_000.0),
            # Filled in below once the clock has been advanced, so ts
            # reflects "the Mac just woke up 3 hours later".
        ],
    )
    collector = Collector(tmp_config, source, store, clock=clock)
    baseline = collector.poll_once()
    assert baseline.kind == "baseline"

    # The Mac slept for 3 hours: no polls happened, but wall-clock time (and
    # the router's own uptime) kept advancing the whole time. Packets are
    # unavailable this poll too, so nothing can narrow the wrap count.
    three_hours = 3 * 3600
    clock.advance(three_hours)
    source._script.append(
        dict(bytes_recv=51_000_000, bytes_sent=20_000_000, uptime_s=300 + three_hours, pkts_recv=None, pkts_sent=None)
    )

    result = collector.poll_once()

    assert result.kind == "gap"
    assert result.down_bytes is None
    assert result.up_bytes is None
    assert result.down_wraps is None and result.up_wraps is None

    # The baseline still advances past the gap...
    last = store.last_reading()
    assert last is not None and last.bytes_recv == 51_000_000

    # ...and the interval is recorded as an honest gap, not silently
    # dropped or attributed a fabricated byte count.
    gaps = store.notable_intervals(0, clock() + 1, kinds=("gap",))
    assert len(gaps) == 1
    assert gaps[0].down_bytes is None


def test_run_forever_reanchors_after_a_sleep_instead_of_bursting(tmp_config: Config, store: Store):
    """A wait that comes back having taken far longer than requested (real
    system sleep: the process is simply suspended and wakes with the clock
    way ahead) must not cause the scheduler to fire a burst of catch-up
    polls — it should notice the deadline is long past and re-anchor to
    "now + interval" for the next one, polling exactly once for the gap."""
    clock = FakeClock(100_000.0)
    stop = threading.Event()

    calls: list[float] = []

    def sleeper(seconds: float) -> None:
        calls.append(seconds)
        # However long was requested, the machine actually slept 2 hours.
        clock.advance(7200.0)

    script = [
        dict(bytes_recv=1_000_000, bytes_sent=500_000, uptime_s=500, pkts_recv=10, pkts_sent=10),
        dict(bytes_recv=2_000_000, bytes_sent=600_000, uptime_s=500 + 7210, pkts_recv=20, pkts_sent=20),
    ]
    source = FakeSource(clock, script, stop_when_exhausted=stop)
    collector = Collector(tmp_config, source, store, clock=clock, sleeper=sleeper)

    collector.run_forever(stop)

    # Exactly two polls happened (one per scripted reading) — no burst of
    # immediate re-polls to "catch up" on the two hours that passed during
    # the single wait.
    assert source.calls == 2
    assert len(calls) == 1  # one wait, which is what carried the 2h jump

    rows = store.notable_intervals(
        0, clock() + 1, kinds=("baseline", "ok", "ok_wrap", "recovered", "suspect", "gap", "reset", "anomaly")
    )
    assert [r.kind for r in rows] == ["baseline", "gap"]


# ---------------------------------------------------------------------------
# build_source
# ---------------------------------------------------------------------------


def test_build_source_reuses_cached_location(tmp_config: Config, store: Store):
    store.set_meta("last_location", "http://192.0.2.1:1900/cached/gatedesc.xml")
    source = build_source(tmp_config, store)
    assert isinstance(source, UpnpIgdSource)
    assert source.location == "http://192.0.2.1:1900/cached/gatedesc.xml"


def test_build_source_with_no_cached_location(tmp_config: Config, store: Store):
    source = build_source(tmp_config, store)
    assert isinstance(source, UpnpIgdSource)
    assert source.location is None


# ---------------------------------------------------------------------------
# Scheduling: the re-anchor is what stops a wake-from-sleep turning into a
# burst of back-to-back polls at the router. The older re-anchor test above
# could not detect a burst (its script ran out first), so deleting the
# re-anchor left the suite green. This one measures poll SPACING.
# ---------------------------------------------------------------------------


def test_reanchor_prevents_a_catch_up_burst_after_an_8_hour_sleep(
    tmp_config: Config, store: Store
):
    """Mutating ``next_deadline = candidate if candidate > now else
    now + interval`` down to just ``next_deadline = candidate`` makes the
    loop fire every remaining poll instantly, hammering the router with
    2,880 requests to "catch up" on 8 hours. Asserted here as spacing: no
    two consecutive polls may land closer together than poll_interval_s."""
    clock = FakeClock(100_000.0)
    stop = threading.Event()
    interval = tmp_config.poll_interval_s

    first_wait = [True]

    def sleeper(seconds: float) -> None:
        if first_wait[0]:
            first_wait[0] = False
            clock.advance(8 * 3600.0)  # the machine actually slept 8 hours
        else:
            clock.advance(seconds)

    poll_times: list[float] = []

    class TimestampingSource(FakeSource):
        def read(self):
            poll_times.append(clock())
            return super().read()

    script = [
        dict(
            bytes_recv=1_000_000 * (i + 1),
            bytes_sent=100_000 * (i + 1),
            uptime_s=500 + i * 10,
            pkts_recv=1_000 * (i + 1),
            pkts_sent=500 * (i + 1),
        )
        for i in range(6)
    ]
    source = TimestampingSource(clock, script, stop_when_exhausted=stop)
    collector = Collector(tmp_config, source, store, clock=clock, sleeper=sleeper)
    collector.run_forever(stop)

    assert len(poll_times) == 6
    # Poll 0 -> 1 straddles the 8h sleep (huge spacing, fine). Every pair
    # after that must respect the cadence.
    gaps = [b - a for a, b in zip(poll_times[1:], poll_times[2:])]
    assert gaps, "not enough polls to measure spacing"
    assert min(gaps) >= interval, (
        f"catch-up burst: consecutive polls {min(gaps)}s apart, "
        f"poll_interval_s is {interval}s"
    )


# ---------------------------------------------------------------------------
# Packet-unit calibration: the runtime test of the frame-size assumption the
# 'recovered' tier's "proof" rests on.
# ---------------------------------------------------------------------------


def _coalesced_script(clock: FakeClock, bpp: int, n: int) -> list:
    """n normal 10s polls from a router reporting `bpp` bytes per packet."""
    per_poll_pkts = max(CALIBRATION_MIN_PACKETS // (CALIBRATION_MIN_INTERVALS - 1), 1)
    script = []
    total_bytes = 0
    total_pkts = 0
    for i in range(n):
        script.append(
            dict(
                bytes_recv=total_bytes % (2**32),
                bytes_sent=total_bytes % (2**32),
                uptime_s=100_000 + i * 10,
                pkts_recv=total_pkts % (2**32),
                pkts_sent=total_pkts % (2**32),
            )
        )
        total_pkts += per_poll_pkts
        total_bytes += per_poll_pkts * bpp
    return script


def test_packet_recovery_disabled_when_counters_disagree_about_units(
    tmp_config: Config, store: Store
):
    """A router reporting GRO/LRO-coalesced super-packets makes the packet
    envelope resolve confidently to the wrong candidate. Once a full
    measurement window agrees on a ratio outside the assumed frame range,
    packet recovery is switched off, persisted, and the long gap that
    follows classifies 'gap' instead of a 25.8 GB-short 'recovered'."""
    clock = FakeClock(1000.0)
    polls = CALIBRATION_MIN_INTERVALS + 2
    source = FakeSource(clock, _coalesced_script(clock, bpp=9000, n=polls))
    collector = Collector(tmp_config, source, store, clock=clock)

    assert collector.thresholds.packet_recovery_enabled is True
    for _ in range(polls):
        collector.poll_once()
        clock.advance(10.0)

    assert collector.thresholds.packet_recovery_enabled is False
    assert store.get_meta(PACKET_CALIBRATION_META_KEY) == "1"
    events = store.recent_events(20)
    calib = [d for _, k, d in events if k == "calibration"]
    assert calib, "a calibration verdict must leave an events row"
    assert "9000 bytes/packet" in calib[0]
    assert "provably-exact intervals" in calib[0]


def test_one_skewed_interval_does_not_disable_packet_recovery(
    tmp_config: Config, store: Store
):
    """THE regression test for the first version of this calibration, which
    judged each interval on its own and disabled packet recovery on the real
    router within one minute.

    These are consecutive samples with the same shape as ones captured
    during development (absolute values re-synthesized; the deltas, which
    are what the test proves, are unchanged). The byte and packet counters
    come from separate SOAP calls, so a burst landing between them is
    counted in one and not the other. Interval A reads 27.4 bytes/packet,
    interval B reads 30,400.0 -- both nonsense, both artifacts of read skew,
    combined ratio a perfectly ordinary 1203.5."""
    clock = FakeClock(1_700_000_000.0)
    source = FakeSource(
        clock,
        [
            dict(bytes_recv=1_000_000_000, bytes_sent=0, uptime_s=100_000,
                 pkts_recv=900_000_000, pkts_sent=0),
            dict(bytes_recv=1_000_084_998, bytes_sent=0, uptime_s=100_003,
                 pkts_recv=900_003_103, pkts_sent=0),   # +84,998 B / +3,103 pkts
            dict(bytes_recv=1_003_884_997, bytes_sent=0, uptime_s=100_007,
                 pkts_recv=900_003_228, pkts_sent=0),   # +3,799,999 B / +125 pkts
        ],
    )
    collector = Collector(tmp_config, source, store, clock=clock)
    for _ in range(3):
        collector.poll_once()
        clock.advance(3.0)

    assert collector.thresholds.packet_recovery_enabled is True
    assert store.get_meta(PACKET_CALIBRATION_META_KEY) is None
    assert not [d for _, k, d in store.recent_events(20) if k == "calibration"]


def test_a_partial_window_never_renders_a_verdict(tmp_config: Config, store: Store):
    """"Not enough evidence yet" must not become "all clear", and must not
    become a verdict either. One interval short of the threshold, nothing
    has been decided."""
    clock = FakeClock(1000.0)
    polls = CALIBRATION_MIN_INTERVALS - 2
    source = FakeSource(clock, _coalesced_script(clock, bpp=9000, n=polls))
    collector = Collector(tmp_config, source, store, clock=clock)
    for _ in range(polls):
        collector.poll_once()
        clock.advance(10.0)

    assert collector.thresholds.packet_recovery_enabled is True
    assert store.get_meta(PACKET_CALIBRATION_META_KEY) is None


def test_calibration_verdict_survives_a_restart(tmp_config: Config, store: Store):
    """Once proven wrong, packet recovery must not quietly come back on the
    next launch — the verdict lives in meta, not in process memory."""
    store.set_meta(PACKET_CALIBRATION_META_KEY, "1")
    clock = FakeClock(1000.0)
    collector = Collector(tmp_config, FakeSource(clock, []), store, clock=clock)
    assert collector.thresholds.packet_recovery_enabled is False


def test_normal_ethernet_frame_sizes_leave_packet_recovery_on(
    tmp_config: Config, store: Store
):
    """The calibration must not trip on a healthy router: ~1200 bytes per
    packet is squarely inside the assumed 64..1518 range, no matter how many
    full measurement windows go by."""
    clock = FakeClock(1000.0)
    polls = (CALIBRATION_MIN_INTERVALS + 2) * 3
    source = FakeSource(clock, _coalesced_script(clock, bpp=1200, n=polls))
    collector = Collector(tmp_config, source, store, clock=clock)
    for _ in range(polls):
        collector.poll_once()
        clock.advance(10.0)

    assert collector.thresholds.packet_recovery_enabled is True
    assert store.get_meta(PACKET_CALIBRATION_META_KEY) is None
