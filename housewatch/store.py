"""SQLite persistence for housewatch: schema, writer transaction, query API.

Concurrency guarantee (how readers never block the writer and never see a
half-written sample)
------------------------------------------------------------------------
Exactly one read-write connection exists process-wide (the collector's,
``Store(db_path, readonly=False)``); the menubar and every web request use
``Store(db_path, readonly=True)`` connections instead. The writer turns on
``PRAGMA journal_mode=WAL`` at open. Under WAL, writers append new pages to a
separate ``-wal`` file instead of overwriting the main database file in
place, and readers keep reading from a *consistent snapshot* of the pages
that existed at the moment their read transaction started. That gives two
guarantees for free:

1. Readers never block on the writer, and the writer never blocks on
   readers (WAL's whole design goal, unlike rollback-journal mode where a
   writer holds an exclusive lock). ``busy_timeout`` (5000 ms writer / 2000
   ms reader) only matters for the rare writer-vs-writer case, which cannot
   happen here because there is only ever one writer.
2. Readers never observe a torn transaction. Every mutating method in this
   module (``record_poll``, ``record_event``, ``set_meta``, ``prune``) does
   its INSERT/UPDATE statements inside one ``with self._conn:`` block, which
   is sqlite3's implicit-transaction context manager: it opens the
   transaction before the first statement and COMMITs (or ROLLBACKs on
   exception) when the block exits. SQLite's WAL commit is atomic — a
   concurrent reader's snapshot either predates the whole transaction or
   postdates it, never a partial slice of it. ``record_poll`` in particular
   inserts the sample row, the interval row, and the day rollup upsert (and
   updates ``meta['last_reading']``) inside a single such block, so a reader
   can never see a sample row without its interval, or an interval without
   its day-rollup credit.

Day bucketing is done in the *local* timezone (``tz`` is threaded through
every day-bucketing call), not UTC, because a UTC-hour rollup would put
"today" in two different UTC dates for most of the day — and in a
half-hour-offset zone (UTC+05:30, UTC+05:45, UTC+09:30) it never lines up
with a UTC hour at all. Local-midnight boundaries are computed by attaching a
``zoneinfo``-based ``tzinfo`` directly to a naive local wall-clock
``datetime`` (see ``_day_bounds_utc`` and ``housewatch.billing``) — this is
the correct way to get DST-safe UTC offsets in the stdlib: the offset is
resolved for that specific wall-clock instant, so a day that spans a DST
transition correctly comes out 23 or 25 hours long instead of a hardcoded 24.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as dtime
from datetime import timedelta, tzinfo
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from housewatch import billing
from housewatch import config as config_mod
from housewatch.config import Config
from housewatch.model import IntervalResult, Kind, Reading

# ---------------------------------------------------------------------------
# Query-result dataclasses (store.py owns these; model.py stays I/O-free and
# does not need to know about rollups/summaries).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DayUsage:
    day: str
    down_bytes: int
    up_bytes: int
    suspect_down: int
    suspect_up: int
    ok_s: float
    recovered_s: float
    suspect_s: float
    gap_s: float
    uncovered_s: float


@dataclass(frozen=True)
class CycleSummary:
    cycle_start: str
    cycle_end: str
    used_bytes: int
    cap_bytes: int | None
    days_elapsed: float
    days_total: float
    unknown_s: float
    projected_bytes: int
    over_cap_projected: bool | None


@dataclass(frozen=True)
class RatePoint:
    ts: float
    down_bps: float | None
    up_bps: float | None
    kind: Kind


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "1"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS samples (
  ts         REAL PRIMARY KEY,
  bytes_recv INTEGER NOT NULL CHECK (bytes_recv BETWEEN 0 AND 4294967295),
  bytes_sent INTEGER NOT NULL CHECK (bytes_sent BETWEEN 0 AND 4294967295),
  uptime_s   INTEGER NOT NULL CHECK (uptime_s >= 0),
  pkts_recv  INTEGER CHECK (pkts_recv IS NULL OR pkts_recv BETWEEN 0 AND 4294967295),
  pkts_sent  INTEGER CHECK (pkts_sent IS NULL OR pkts_sent BETWEEN 0 AND 4294967295)
);

CREATE TABLE IF NOT EXISTS intervals (
  id         INTEGER PRIMARY KEY,
  start_ts   REAL NOT NULL,
  end_ts     REAL NOT NULL CHECK (end_ts >= start_ts),
  kind       TEXT NOT NULL CHECK (kind IN
             ('baseline','ok','ok_wrap','recovered','suspect','gap','reset','anomaly')),
  down_bytes INTEGER CHECK (down_bytes IS NULL OR down_bytes >= 0),
  up_bytes   INTEGER CHECK (up_bytes   IS NULL OR up_bytes   >= 0),
  down_wraps INTEGER,
  up_wraps   INTEGER,
  note       TEXT NOT NULL DEFAULT '',
  CHECK ((down_bytes IS NOT NULL) = (kind IN ('ok','ok_wrap','recovered','suspect'))),
  CHECK ((up_bytes   IS NOT NULL) = (kind IN ('ok','ok_wrap','recovered','suspect')))
);
CREATE INDEX IF NOT EXISTS idx_intervals_end   ON intervals (end_ts);
CREATE INDEX IF NOT EXISTS idx_intervals_start ON intervals (start_ts);

CREATE TABLE IF NOT EXISTS days (
  day           TEXT PRIMARY KEY,
  down_bytes    INTEGER NOT NULL DEFAULT 0,
  up_bytes      INTEGER NOT NULL DEFAULT 0,
  suspect_down  INTEGER NOT NULL DEFAULT 0,
  suspect_up    INTEGER NOT NULL DEFAULT 0,
  ok_s          REAL NOT NULL DEFAULT 0,
  recovered_s   REAL NOT NULL DEFAULT 0,
  suspect_s     REAL NOT NULL DEFAULT 0,
  gap_s         REAL NOT NULL DEFAULT 0
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS events (
  id     INTEGER PRIMARY KEY,
  ts     REAL NOT NULL,
  kind   TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);
"""

# Duration field each interval `kind` accrues into on the `days` rollup.
# 'baseline' is intentionally absent: a baseline row has start_ts == end_ts
# (zero duration) so it never reaches the accrual code at all.
_DURATION_FIELD: dict[str, str] = {
    "ok": "ok_s",
    "ok_wrap": "ok_s",
    "recovered": "recovered_s",
    "suspect": "suspect_s",
    "gap": "gap_s",
    "reset": "gap_s",
    "anomaly": "gap_s",
}

# Migration hook: keyed by the schema_version a migration upgrades *from*.
# Empty today (only version "1" has ever existed) — exists so a future
# version bump has exactly one place to add a step. Each callable must bump
# meta['schema_version'] itself.
_MIGRATIONS: dict[str, Callable[[sqlite3.Connection], None]] = {}


class Store:
    """SQLite persistence + query API. One rw instance (the collector's);
    any number of readonly instances (menubar, webapp)."""

    def __init__(self, db_path: Path, readonly: bool = False) -> None:
        self.db_path = Path(db_path)
        self.readonly = readonly

        if readonly:
            if not self.db_path.exists():
                # mode=ro refuses to create the file; fail with a clear
                # message instead of sqlite3's generic "unable to open
                # database file".
                raise FileNotFoundError(
                    f"housewatch db not found at {self.db_path} — the writer "
                    "must call init_schema() before any readonly Store opens it"
                )
            uri = "file:" + quote(str(self.db_path), safe="/") + "?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True)
            self._conn.execute("PRAGMA busy_timeout=2000")
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")

        self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def _require_writable(self) -> None:
        if self.readonly:
            raise PermissionError(
                f"cannot write to a readonly Store ({self.db_path})"
            )

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def init_schema(self) -> None:
        """Create tables/indexes if absent (idempotent) and stamp
        schema_version. Writer only."""
        self._require_writable()
        self._conn.executescript(_SCHEMA_SQL)
        self._run_migrations()
        with self._conn:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO NOTHING",
                (SCHEMA_VERSION,),
            )
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES ('created_at', ?) "
                "ON CONFLICT(key) DO NOTHING",
                (repr(time.time()),),
            )

    def _run_migrations(self) -> None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        current = row[0] if row is not None else None
        if current is None:
            return  # fresh database; init_schema stamps the version right after
        seen: set[str] = set()
        while current in _MIGRATIONS and current not in seen:
            seen.add(current)
            _MIGRATIONS[current](self._conn)
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            current = row[0] if row is not None else current

    # ------------------------------------------------------------------
    # Writer API (collector thread only)
    # ------------------------------------------------------------------

    def record_poll(self, sample: Reading, interval: IntervalResult, tz: tzinfo) -> None:
        """Persist one successful poll: the raw sample, its classified
        interval, and the local-day rollup credit — as ONE transaction, so a
        crash mid-write can never leave a sample without its interval or an
        interval without its rollup. Called for every successful poll,
        including 'baseline' (first-ever sample)."""
        self._require_writable()
        with self._conn:
            # ON CONFLICT, not a plain INSERT: samples.ts is the PRIMARY KEY,
            # and a wall-clock step backward (an NTP correction on wake -- the
            # exact scenario the classifier's 'anomaly' branch exists for) or
            # two reads landing on one float timestamp produces a duplicate.
            # A raw IntegrityError here rolled back the WHOLE transaction --
            # sample, interval, day rollup AND the meta baseline advance --
            # and Collector.poll_once, which catches only SourceError, then
            # reported the poll as a router failure: backoff doubled for a
            # purely local problem and the baseline never advanced, so the
            # gap grew until it classified 'gap' and discarded a real
            # interval's bytes. A same-timestamp re-read is simply a better
            # snapshot of that instant, so it replaces the earlier one.
            self._conn.execute(
                "INSERT INTO samples(ts, bytes_recv, bytes_sent, uptime_s, "
                "pkts_recv, pkts_sent) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(ts) DO UPDATE SET "
                "bytes_recv = excluded.bytes_recv, "
                "bytes_sent = excluded.bytes_sent, "
                "uptime_s   = excluded.uptime_s, "
                "pkts_recv  = excluded.pkts_recv, "
                "pkts_sent  = excluded.pkts_sent",
                (
                    sample.ts,
                    sample.bytes_recv,
                    sample.bytes_sent,
                    sample.uptime_s,
                    sample.pkts_recv,
                    sample.pkts_sent,
                ),
            )
            self._conn.execute(
                "INSERT INTO intervals(start_ts, end_ts, kind, down_bytes, "
                "up_bytes, down_wraps, up_wraps, note) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    interval.start_ts,
                    interval.end_ts,
                    interval.kind,
                    interval.down_bytes,
                    interval.up_bytes,
                    interval.down_wraps,
                    interval.up_wraps,
                    interval.note,
                ),
            )
            self._upsert_days(interval, tz)
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES ('last_reading', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_reading_to_json(sample),),
            )

    @staticmethod
    def _prorate(total: int, fractions: list[float]) -> list[int]:
        """Split integer `total` across `fractions` (which need not sum to
        exactly 1.0, but do here) so the parts sum EXACTLY to `total`. Every
        part but the last is rounded; the last absorbs the rounding
        remainder. Documented approximation: only affects the rare interval
        (recovered, up to ~48 min) that straddles a local midnight."""
        parts: list[int] = []
        running = 0
        n = len(fractions)
        for i, frac in enumerate(fractions):
            if i == n - 1:
                parts.append(total - running)
            else:
                amt = round(total * frac)
                parts.append(amt)
                running += amt
        return parts

    def _upsert_days(self, interval: IntervalResult, tz: tzinfo) -> None:
        if interval.end_ts <= interval.start_ts:
            return  # baseline (or a zero/negative-duration row): nothing to accrue
        duration_field = _DURATION_FIELD.get(interval.kind)
        if duration_field is None:
            return  # defensive: unknown/baseline-like kind, nothing to accrue

        segments = billing.local_days_spanned(interval.start_ts, interval.end_ts, tz)
        if not segments:
            return

        total_duration = interval.end_ts - interval.start_ts
        has_bytes = interval.down_bytes is not None and interval.up_bytes is not None
        fractions = [(seg_end - seg_start) / total_duration for _, seg_start, seg_end in segments]
        down_parts = self._prorate(interval.down_bytes, fractions) if has_bytes else None
        up_parts = self._prorate(interval.up_bytes, fractions) if has_bytes else None
        is_suspect = interval.kind == "suspect"

        for i, (day, seg_start, seg_end) in enumerate(segments):
            seg_dur = seg_end - seg_start
            down_amt = down_parts[i] if down_parts is not None else 0
            up_amt = up_parts[i] if up_parts is not None else 0
            suspect_down_amt = down_amt if is_suspect else 0
            suspect_up_amt = up_amt if is_suspect else 0
            ok_add = seg_dur if duration_field == "ok_s" else 0.0
            recovered_add = seg_dur if duration_field == "recovered_s" else 0.0
            suspect_add = seg_dur if duration_field == "suspect_s" else 0.0
            gap_add = seg_dur if duration_field == "gap_s" else 0.0
            self._conn.execute(
                """
                INSERT INTO days(day, down_bytes, up_bytes, suspect_down, suspect_up,
                                  ok_s, recovered_s, suspect_s, gap_s)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(day) DO UPDATE SET
                    down_bytes   = down_bytes   + excluded.down_bytes,
                    up_bytes     = up_bytes     + excluded.up_bytes,
                    suspect_down = suspect_down + excluded.suspect_down,
                    suspect_up   = suspect_up   + excluded.suspect_up,
                    ok_s         = ok_s         + excluded.ok_s,
                    recovered_s  = recovered_s  + excluded.recovered_s,
                    suspect_s    = suspect_s    + excluded.suspect_s,
                    gap_s        = gap_s        + excluded.gap_s
                """,
                (
                    day,
                    down_amt,
                    up_amt,
                    suspect_down_amt,
                    suspect_up_amt,
                    ok_add,
                    recovered_add,
                    suspect_add,
                    gap_add,
                ),
            )

    def record_event(self, ts: float, kind: str, detail: str = "") -> None:
        self._require_writable()
        with self._conn:
            self._conn.execute(
                "INSERT INTO events(ts, kind, detail) VALUES (?, ?, ?)",
                (ts, kind, detail),
            )

    def set_meta(self, key: str, value: str) -> None:
        self._require_writable()
        with self._conn:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row is not None else None

    def last_reading(self) -> Reading | None:
        """The collector's baseline, from meta — survives restarts AND
        sample-table pruning (meta rows are never pruned)."""
        raw = self.get_meta("last_reading")
        if raw is None:
            return None
        return _reading_from_json(raw)

    def prune(self, now_ts: float, cfg: Config) -> None:
        """Delete rows past each table's retention window, then checkpoint
        the WAL back into the main db file — but only once per local day
        (tracked via meta), since this is called every time the collector's
        local hour changes, not once a day."""
        self._require_writable()
        with self._conn:
            self._conn.execute(
                "DELETE FROM samples WHERE ts < ?",
                (now_ts - cfg.retention_samples_days * 86400,),
            )
            self._conn.execute(
                "DELETE FROM intervals WHERE end_ts < ?",
                (now_ts - cfg.retention_intervals_days * 86400,),
            )
            self._conn.execute(
                "DELETE FROM events WHERE ts < ?",
                (now_ts - cfg.retention_events_days * 86400,),
            )

        tz = config_mod.tzinfo_of(cfg)
        today = billing.day_key(now_ts, tz)
        if self.get_meta("last_wal_checkpoint_day") != today:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.set_meta("last_wal_checkpoint_day", today)

    # ------------------------------------------------------------------
    # Read API (menubar + webapp; readonly=True)
    # ------------------------------------------------------------------

    @staticmethod
    def _day_bounds_utc(day: str, tz: tzinfo) -> tuple[float, float]:
        """[local midnight of `day`, local midnight of the next day) as UTC
        epoch seconds. DST-safe: the tzinfo is attached directly to each
        naive local wall-clock datetime, so .timestamp() resolves the
        correct UTC offset for that specific date rather than assuming a
        fixed 24h day."""
        d = date.fromisoformat(day)
        start = datetime.combine(d, dtime.min, tzinfo=tz).timestamp()
        end = datetime.combine(d + timedelta(days=1), dtime.min, tzinfo=tz).timestamp()
        return start, end

    def day_usage(self, day: str, tz: tzinfo, now_ts: float) -> DayUsage:
        """Usage for one local calendar day. A day with NO rows at all (the
        collector never ran that day) comes back with all-zero bytes and
        uncovered_s == the full elapsed portion of that day — never a false
        '0 bytes used' reading, because zero bytes and zero coverage are
        reported as two different fields."""
        row = self._conn.execute(
            "SELECT down_bytes, up_bytes, suspect_down, suspect_up, "
            "ok_s, recovered_s, suspect_s, gap_s FROM days WHERE day = ?",
            (day,),
        ).fetchone()
        if row is None:
            down_bytes = up_bytes = suspect_down = suspect_up = 0
            ok_s = recovered_s = suspect_s = gap_s = 0.0
        else:
            down_bytes = row["down_bytes"]
            up_bytes = row["up_bytes"]
            suspect_down = row["suspect_down"]
            suspect_up = row["suspect_up"]
            ok_s = row["ok_s"]
            recovered_s = row["recovered_s"]
            suspect_s = row["suspect_s"]
            gap_s = row["gap_s"]

        day_start_ts, day_end_ts = self._day_bounds_utc(day, tz)
        elapsed = max(min(now_ts, day_end_ts) - day_start_ts, 0.0)
        covered = ok_s + recovered_s + suspect_s + gap_s
        uncovered_s = max(elapsed - covered, 0.0)

        return DayUsage(
            day=day,
            down_bytes=down_bytes,
            up_bytes=up_bytes,
            suspect_down=suspect_down,
            suspect_up=suspect_up,
            ok_s=ok_s,
            recovered_s=recovered_s,
            suspect_s=suspect_s,
            gap_s=gap_s,
            uncovered_s=uncovered_s,
        )

    def recent_days(self, n: int, tz: tzinfo, now_ts: float) -> list[DayUsage]:
        """Oldest-first, `n` local days ending with (and including) today."""
        today = billing.day_key(now_ts, tz)
        today_date = date.fromisoformat(today)
        day_strs = [(today_date - timedelta(days=i)).isoformat() for i in range(n)]
        day_strs.reverse()
        return [self.day_usage(d, tz, now_ts) for d in day_strs]

    def cycle_summary(self, cfg: Config, now_ts: float) -> CycleSummary:
        """Usage + projection for the billing cycle containing `now_ts`.

        cap_bytes uses decimal GB (1 GB = 1e9 bytes), the ISP/networking
        convention, matching monthly_cap_gb's own name.
        cycle_end is the LAST day of the cycle (inclusive), i.e. the day
        before the next cycle's start.
        """
        tz = config_mod.tzinfo_of(cfg)
        now_local = datetime.fromtimestamp(now_ts, tz)
        cycle_start_dt, cycle_end_dt = billing.cycle_bounds(
            now_local, cfg.billing_cycle_start_day
        )
        cycle_start_ts = cycle_start_dt.timestamp()
        cycle_end_ts = cycle_end_dt.timestamp()  # exclusive: start of NEXT cycle

        cycle_start_day = billing.day_key(cycle_start_ts, tz)
        cycle_end_day = billing.day_key(cycle_end_ts - 1, tz)  # inclusive last day

        # Walk every local day from cycle start up to min(now, cycle end),
        # via day_usage() so days with zero rows correctly contribute a full
        # day of uncovered_s instead of silently vanishing from the sum.
        elapsed_days: list[str] = []
        cutoff = min(now_ts, cycle_end_ts)
        d = date.fromisoformat(cycle_start_day)
        while True:
            day_str = d.isoformat()
            day_start_ts, _ = self._day_bounds_utc(day_str, tz)
            if day_start_ts >= cutoff:
                break
            elapsed_days.append(day_str)
            d += timedelta(days=1)

        used_bytes = 0
        unknown_s = 0.0
        for day_str in elapsed_days:
            du = self.day_usage(day_str, tz, now_ts)
            used_bytes += du.down_bytes + du.up_bytes
            unknown_s += du.gap_s + du.uncovered_s

        elapsed_s = max(now_ts - cycle_start_ts, 0.0)
        remaining_s = max(cycle_end_ts - now_ts, 0.0)
        days_elapsed = elapsed_s / 86400.0
        days_total = (cycle_end_ts - cycle_start_ts) / 86400.0

        known_s = max(elapsed_s - unknown_s, 1.0)
        known_rate = used_bytes / known_s
        projected_bytes = int(round(used_bytes + known_rate * (unknown_s + remaining_s)))

        cap_bytes = (
            int(cfg.monthly_cap_gb * 1_000_000_000)
            if cfg.monthly_cap_gb is not None
            else None
        )
        over_cap_projected = (
            None if cap_bytes is None else projected_bytes > cap_bytes
        )

        return CycleSummary(
            cycle_start=cycle_start_day,
            cycle_end=cycle_end_day,
            used_bytes=used_bytes,
            cap_bytes=cap_bytes,
            days_elapsed=days_elapsed,
            days_total=days_total,
            unknown_s=unknown_s,
            projected_bytes=projected_bytes,
            over_cap_projected=over_cap_projected,
        )

    def rate_series(self, minutes: int, now_ts: float) -> list[RatePoint]:
        """Interval-ending points over the last `minutes`, for a chart. bps
        is bits/second (bytes*8/duration). 'baseline' rows (zero duration)
        are excluded; any interval with unknown bytes or non-positive
        duration reports None so charts render a hole, never a fake zero."""
        start_ts = now_ts - minutes * 60
        rows = self._conn.execute(
            "SELECT start_ts, end_ts, kind, down_bytes, up_bytes FROM intervals "
            "WHERE end_ts >= ? AND end_ts <= ? AND kind != 'baseline' "
            "ORDER BY end_ts ASC",
            (start_ts, now_ts),
        ).fetchall()
        points: list[RatePoint] = []
        for row in rows:
            duration = row["end_ts"] - row["start_ts"]
            down_bytes = row["down_bytes"]
            up_bytes = row["up_bytes"]
            if down_bytes is None or up_bytes is None or duration <= 0:
                down_bps = up_bps = None
            else:
                down_bps = down_bytes * 8 / duration
                up_bps = up_bytes * 8 / duration
            points.append(
                RatePoint(ts=row["end_ts"], down_bps=down_bps, up_bps=up_bps, kind=row["kind"])
            )
        return points

    def recent_events(self, n: int) -> list[tuple[float, str, str]]:
        """Most-recent-first, up to `n` events."""
        rows = self._conn.execute(
            "SELECT ts, kind, detail FROM events ORDER BY ts DESC LIMIT ?", (n,)
        ).fetchall()
        return [(row["ts"], row["kind"], row["detail"]) for row in rows]

    def notable_intervals(
        self,
        start_ts: float,
        end_ts: float,
        kinds: tuple[str, ...] = ("suspect", "gap", "reset", "anomaly"),
    ) -> list[IntervalResult]:
        """Gap/suspect/reset/anomaly windows overlapping [start_ts, end_ts],
        oldest-first — so the UI can list/label low-confidence or unknown
        periods honestly instead of only ever seeing the rolled-up totals.
        Not in the architect's module spec's literal method list, but
        required by this task's stated query-API requirements ("a listing
        of gap/suspect windows"); kept as a plain additive method so it
        can't collide with any spec-named signature."""
        placeholders = ",".join("?" for _ in kinds)
        rows = self._conn.execute(
            "SELECT start_ts, end_ts, kind, down_bytes, up_bytes, down_wraps, "
            f"up_wraps, note FROM intervals WHERE end_ts >= ? AND start_ts <= ? "
            f"AND kind IN ({placeholders}) ORDER BY start_ts ASC",
            (start_ts, end_ts, *kinds),
        ).fetchall()
        return [
            IntervalResult(
                kind=row["kind"],
                start_ts=row["start_ts"],
                end_ts=row["end_ts"],
                down_bytes=row["down_bytes"],
                up_bytes=row["up_bytes"],
                down_wraps=row["down_wraps"],
                up_wraps=row["up_wraps"],
                note=row["note"],
            )
            for row in rows
        ]


def _reading_to_json(r: Reading) -> str:
    return json.dumps(
        {
            "ts": r.ts,
            "bytes_recv": r.bytes_recv,
            "bytes_sent": r.bytes_sent,
            "uptime_s": r.uptime_s,
            "pkts_recv": r.pkts_recv,
            "pkts_sent": r.pkts_sent,
        }
    )


def _reading_from_json(s: str) -> Reading:
    d = json.loads(s)
    return Reading(**d)
