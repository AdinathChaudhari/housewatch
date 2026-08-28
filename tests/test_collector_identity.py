"""Regression tests: the collector must persist the router's identity.

This chain broke silently once. `igd.UpnpIgdSource` parses the device
description and exposes `.identity`; `cli.cmd_status` reads
`meta['router_identity']` and renders it. Nothing in between ever wrote the
value, so `status` reported "router: not yet identified" forever while
`doctor` -- which builds its own source and asks it directly -- displayed the
router correctly. Two surfaces disagreeing about the same fact is the tell.

Nothing tested the write, which is why it survived. These tests cover it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from housewatch.collector import Collector
from housewatch.config import Config
from housewatch.store import Store

from tests.test_collector import FakeClock, FakeSource


@pytest.fixture
def store(tmp_db_path: Path) -> Store:
    """Local mirror of test_collector.py's fixture, which is module-private."""
    s = Store(tmp_db_path, readonly=False)
    s.init_schema()
    yield s
    s.close()


@dataclass
class FakeIdentity:
    """Stands in for ``detect.RouterIdentity`` -- only ``summary()`` is used."""

    text: str

    def summary(self) -> str:
        return self.text


class IdentifiedSource(FakeSource):
    """A FakeSource that also advertises an identity, like the real one."""

    def __init__(self, clock, script, identity: str | None = None) -> None:
        super().__init__(clock, script)
        self.identity = FakeIdentity(identity) if identity is not None else None
        self.location = "http://192.0.2.1:1900/desc/gatedesc.xml"


def _script(n: int) -> list[dict]:
    """n plausible consecutive readings, monotonic in bytes and uptime."""
    return [
        dict(
            bytes_recv=1_000_000 + i * 10_000_000,
            bytes_sent=500_000 + i * 1_000_000,
            uptime_s=100_000 + i * 10,
            pkts_recv=10_000 + i * 5_000,
            pkts_sent=8_000 + i * 4_000,
        )
        for i in range(n)
    ]


def test_identity_is_persisted_on_the_first_successful_poll(
    tmp_config: Config, store: Store
):
    """The bug: this meta key was never written at all."""
    clock = FakeClock(1_000.0)
    source = IdentifiedSource(clock, _script(2), identity="ExampleRouter (Acme XR-1)")
    collector = Collector(tmp_config, source, store, clock=clock)

    collector.poll_once()

    assert store.get_meta("router_identity") == "ExampleRouter (Acme XR-1)"


def test_first_identity_is_a_baseline_not_an_event(tmp_config: Config, store: Store):
    """Same rule as last_location: learning the identity is not a transition.

    Otherwise every collector start writes a spurious 'discovery' row, and
    under KeepAlive that means every login, restart and crash-recovery.
    """
    clock = FakeClock(1_000.0)
    source = IdentifiedSource(clock, _script(3), identity="ExampleRouter (Acme XR-1)")
    collector = Collector(tmp_config, source, store, clock=clock)

    collector.poll_once()
    clock.advance(10.0)
    collector.poll_once()  # identity unchanged

    assert store.recent_events(10) == []


def test_a_changed_identity_is_an_event(tmp_config: Config, store: Store):
    """Swapping the box behind the gateway address IS worth recording."""
    clock = FakeClock(1_000.0)
    source = IdentifiedSource(clock, _script(3), identity="ExampleRouter (Acme XR-1)")
    collector = Collector(tmp_config, source, store, clock=clock)
    collector.poll_once()
    assert store.recent_events(10) == []

    source.identity = FakeIdentity("OtherRouter (Acme XR-2)")
    clock.advance(10.0)
    collector.poll_once()

    events = store.recent_events(10)
    assert [e[1] for e in events] == ["discovery"]
    assert store.get_meta("router_identity") == "OtherRouter (Acme XR-2)"


def test_a_source_without_identity_is_not_an_error(tmp_config: Config, store: Store):
    """`identity` is optional on the WanCounterSource protocol.

    A future per-device source, or one built before identity existed, must keep
    polling normally rather than crashing the daemon over a display field.
    """
    clock = FakeClock(1_000.0)
    source = IdentifiedSource(clock, _script(2), identity=None)
    collector = Collector(tmp_config, source, store, clock=clock)

    result = collector.poll_once()

    assert result is not None
    assert store.get_meta("router_identity") is None
    assert store.recent_events(10) == []


def test_identity_survives_a_restart_without_relogging(
    tmp_config: Config, store: Store
):
    """A second Collector over the same store must not re-log an unchanged id."""
    clock = FakeClock(1_000.0)
    first = IdentifiedSource(clock, _script(2), identity="ExampleRouter (Acme XR-1)")
    Collector(tmp_config, first, store, clock=clock).poll_once()
    assert store.get_meta("router_identity") == "ExampleRouter (Acme XR-1)"

    clock2 = FakeClock(1_010.0)
    resumed = [
        dict(
            bytes_recv=21_000_000 + i * 10_000_000,
            bytes_sent=2_500_000 + i * 1_000_000,
            uptime_s=100_010 + i * 10,
            pkts_recv=20_000 + i * 5_000,
            pkts_sent=16_000 + i * 4_000,
        )
        for i in range(2)
    ]
    second = IdentifiedSource(clock2, resumed, identity="ExampleRouter (Acme XR-1)")
    Collector(tmp_config, second, store, clock=clock2).poll_once()

    assert store.recent_events(10) == []
    assert store.get_meta("router_identity") == "ExampleRouter (Acme XR-1)"


def test_status_reads_exactly_this_meta_key(tmp_config: Config, store: Store):
    """Pins the contract cli.cmd_status depends on: the exact meta key.

    The original failure was a key nobody wrote, not a key written wrongly, so
    assert on the literal string cli.py reads.
    """
    clock = FakeClock(1_000.0)
    source = IdentifiedSource(clock, _script(2), identity="ExampleRouter (Acme XR-1)")
    Collector(tmp_config, source, store, clock=clock).poll_once()

    assert store.get_meta("router_identity") is not None, (
        "cli.cmd_status reads exactly meta['router_identity']; if this key is "
        "renamed, status silently reverts to 'not yet identified'"
    )
