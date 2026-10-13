"""The shared sensory store: one SQLite-WAL database for the ingress and the poller (spec §4.1.7).

Webhooks deliver at least once and can be lost; the poller is the source of truth. Both paths
claim hits in the same ``seen_hits`` table, so **each hit is processed once**, whichever path saw
it first, and both feed the same durable queue that the control plane consumes (Stage 5 binds
the queue to ``FleetSupervisor.hit``).

The key of a hit is ``(detection, trace, span)``: a webhook payload carries the detection, trace
and span ids but not the hit id the poller sees (spike A6), so the natural key is the only one
both paths share. The poller fills in ``hit_id`` when it meets a hit a webhook claimed first.

Tables (§5.4 schema; ``seen_hits`` is the same table the reduced-mode FSM will use):

- ``seen_events``   webhook event ids, deduplicated for 24 hours (replay protection, §5.6);
- ``seen_hits``     claimed hits, their source and, once the Sentinel decides, the incident and
                    disposition that I7 checks: ``opened`` or ``attached`` (an incident), or
                    ``held`` (below its receptor's paging threshold, recorded against the
                    receptor's watch record ``WATCH-<receptor>-<host>``; amendment A-47);
- ``watermarks``    per detection, the newest ``detectedAt`` the poller has swept;
- ``queue``         alerts and hits for the control plane, in order.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from kintsugi.control.dbconn import enable_wal

EVENT_WINDOW_S = 24 * 3600
Source = Literal["webhook", "poller", "metric"]
Disposition = Literal["opened", "attached", "held"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_events (
  event_id TEXT PRIMARY KEY, type TEXT NOT NULL, delivery_id TEXT, received_at REAL NOT NULL,
  payload_sha256 TEXT NOT NULL, evidence_path TEXT, duplicates INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS seen_hits (
  hit_key TEXT PRIMARY KEY, hit_id TEXT, detection_id TEXT NOT NULL, trace_id TEXT NOT NULL,
  span_id TEXT NOT NULL, source TEXT NOT NULL CHECK (source IN ('webhook','poller','metric')),
  receptor TEXT, host TEXT, detected_at TEXT, seen_at REAL NOT NULL, incident TEXT,
  disposition TEXT CHECK (disposition IS NULL OR disposition IN ('opened','attached','held'))
);
CREATE INDEX IF NOT EXISTS seen_hits_detected ON seen_hits (detected_at);
CREATE TABLE IF NOT EXISTS watermarks (
  detection_id TEXT PRIMARY KEY, newest_detected_at TEXT, swept_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS queue (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL CHECK (kind IN ('alert','hit')),
  payload_json TEXT NOT NULL, enqueued_at REAL NOT NULL, taken_at REAL
);
"""


def hit_key(detection_id: str, trace_id: str, span_id: str) -> str:
    return f"{detection_id}:{trace_id}:{span_id}"


@dataclass(frozen=True)
class HitEvent:
    """One hit, as the control plane receives it (the ``hit`` signal payload, §5.3.3)."""

    hit_key: str
    detection_id: str
    trace_id: str
    span_id: str
    source: Source
    hit_id: str | None = None
    detected_at: str | None = None
    receptor: str | None = None
    host: str | None = None
    event_id: str | None = None
    score: float | None = None


@dataclass(frozen=True)
class QueueItem:
    seq: int
    kind: str
    payload: dict[str, Any]
    enqueued_at: float


@dataclass(frozen=True)
class SeenHit:
    hit_key: str
    hit_id: str | None
    detection_id: str
    trace_id: str
    span_id: str
    source: str
    receptor: str | None
    host: str | None
    detected_at: str | None
    incident: str | None
    disposition: str | None


class HitSink(Protocol):
    """Where newly claimed hits go. Stage 5 binds this to ``FleetSupervisor.hit``."""

    def emit(self, hit: HitEvent) -> None: ...


class SenseStore:
    def __init__(self, path: Path | str, *, clock: Any = time.time) -> None:
        self.path = str(path)
        self._clock = clock
        self._local = threading.local()
        self._opened: list[sqlite3.Connection] = []  # every thread's connection, for close()
        self._opened_lock = threading.Lock()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            enable_wal(self.path)  # once per file, retried (kintsugi.control.dbconn)
        self._connection().executescript(SCHEMA)  # idempotent DDL; commits on its own
        self._migrate_dispositions()

    def _migrate_dispositions(self) -> None:
        """Stage 4 stores only allowed ``opened``/``attached``: rebuild with ``held`` (A-47)."""
        query = "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'seen_hits'"
        row = self._connection().execute(query).fetchone()
        if row is None or "'held'" in row["sql"]:
            return
        new_table = SCHEMA.split("CREATE TABLE IF NOT EXISTS seen_hits", 1)[1].split(";", 1)[0]
        self._connection().executescript(
            "BEGIN IMMEDIATE;"
            "DROP INDEX IF EXISTS seen_hits_detected;"
            "ALTER TABLE seen_hits RENAME TO seen_hits_stage4;"
            f"CREATE TABLE seen_hits {new_table};"
            "INSERT INTO seen_hits SELECT * FROM seen_hits_stage4;"
            "DROP TABLE seen_hits_stage4;"
            "CREATE INDEX IF NOT EXISTS seen_hits_detected ON seen_hits (detected_at);"
            "COMMIT;"
        )

    def _connection(self) -> sqlite3.Connection:
        db = getattr(self._local, "db", None)
        if db is None:
            db = sqlite3.connect(self.path, timeout=5.0, isolation_level=None,
                                 check_same_thread=False)  # fmt: skip
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout = 30000")  # WAL is set once per file, in __init__
            db.execute("PRAGMA synchronous = NORMAL")
            self._local.db = db
            with self._opened_lock:
                self._opened.append(db)
        return db

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        db = self._connection()
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")

    def close(self) -> None:
        """Close every connection this store opened, on any thread (call it when no thread
        is using the store any more; a later call simply opens a new connection)."""
        with self._opened_lock:
            opened, self._opened = self._opened, []
        for db in opened:
            db.close()
        self._local = threading.local()

    # ----------------------------------------------------------------------- events

    def claim_event(
        self,
        event_id: str,
        type_: str,
        payload_sha256: str,
        *,
        delivery_id: str | None = None,
        evidence_path: str | None = None,
    ) -> bool:
        """True for a new event; False for one already seen in the last 24 hours."""
        now = self._clock()
        with self._tx() as db:
            row = db.execute("SELECT received_at FROM seen_events WHERE event_id = ?",
                             (event_id,)).fetchone()  # fmt: skip
            if row is not None and now - row["received_at"] < EVENT_WINDOW_S:
                db.execute("UPDATE seen_events SET duplicates = duplicates + 1 WHERE event_id = ?",
                           (event_id,))  # fmt: skip
                return False
            db.execute(
                "INSERT INTO seen_events (event_id, type, delivery_id, received_at, payload_sha256,"
                " evidence_path) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (event_id) DO UPDATE SET"
                " received_at = excluded.received_at, delivery_id = excluded.delivery_id",
                (event_id, type_, delivery_id, now, payload_sha256, evidence_path),
            )
            return True

    def event_duplicates(self) -> int:
        row = self._connection().execute("SELECT COALESCE(SUM(duplicates), 0) FROM seen_events")
        return int(row.fetchone()[0])

    def prune_events(self) -> int:
        with self._tx() as db:
            cut = self._clock() - EVENT_WINDOW_S
            return db.execute("DELETE FROM seen_events WHERE received_at < ?", (cut,)).rowcount

    # ----------------------------------------------------------------------- hits

    def claim_hit(self, hit: HitEvent) -> bool:
        """Claim ``hit`` for processing. True exactly once per hit key, across both paths."""
        with self._tx() as db:
            inserted = db.execute(
                "INSERT OR IGNORE INTO seen_hits (hit_key, hit_id, detection_id, trace_id, span_id,"
                " source, receptor, host, detected_at, seen_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (hit.hit_key, hit.hit_id, hit.detection_id, hit.trace_id, hit.span_id, hit.source,
                 hit.receptor, hit.host, hit.detected_at, self._clock()),
            ).rowcount  # fmt: skip
            if not inserted and (hit.hit_id or hit.detected_at):
                db.execute(
                    "UPDATE seen_hits SET hit_id = COALESCE(hit_id, ?),"
                    " detected_at = COALESCE(detected_at, ?) WHERE hit_key = ?",
                    (hit.hit_id, hit.detected_at, hit.hit_key),
                )
            if inserted:
                db.execute("INSERT INTO queue (kind, payload_json, enqueued_at) VALUES (?, ?, ?)",
                           ("hit", json.dumps(asdict(hit), sort_keys=True),
                            self._clock()))  # fmt: skip
            return bool(inserted)

    def seen(self, key: str) -> SeenHit | None:
        row = self._connection().execute("SELECT * FROM seen_hits WHERE hit_key = ?", (key,))
        found = row.fetchone()
        return _seen(found) if found else None

    def seen_hits(self) -> dict[str, SeenHit]:
        rows = self._connection().execute("SELECT * FROM seen_hits ORDER BY seen_at")
        return {row["hit_key"]: _seen(row) for row in rows}

    def hits_for(self, receptor: str, host: str) -> list[SeenHit]:
        """Every claimed hit of one receptor on one host, oldest first (the paging window)."""
        rows = self._connection().execute(
            "SELECT * FROM seen_hits WHERE receptor = ? AND host = ? ORDER BY detected_at, seen_at",
            (receptor, host),
        )
        return [_seen(row) for row in rows]

    def assign(self, key: str, incident: str, disposition: Disposition) -> None:
        """The Sentinel's decision for a hit: it opened ``incident``, was attached to it, or is
        held below its receptor's paging threshold against a watch record."""
        with self._tx() as db:
            changed = db.execute(
                "UPDATE seen_hits SET incident = ?, disposition = ? WHERE hit_key = ?",
                (incident, disposition, key),
            ).rowcount
            if not changed:
                raise KeyError(f"unknown hit {key}")

    # ----------------------------------------------------------------------- watermarks

    def watermark(self, detection_id: str) -> str | None:
        query = "SELECT newest_detected_at FROM watermarks WHERE detection_id = ?"
        row = self._connection().execute(query, (detection_id,)).fetchone()
        return row["newest_detected_at"] if row else None

    def set_watermark(self, detection_id: str, newest: str | None) -> None:
        with self._tx() as db:
            db.execute(
                "INSERT INTO watermarks (detection_id, newest_detected_at, swept_at)"
                " VALUES (?, ?, ?) ON CONFLICT (detection_id) DO UPDATE SET"
                " swept_at = excluded.swept_at,"
                " newest_detected_at = CASE WHEN excluded.newest_detected_at IS NULL"
                "   THEN watermarks.newest_detected_at"
                "   WHEN watermarks.newest_detected_at IS NULL"
                "     OR excluded.newest_detected_at > watermarks.newest_detected_at"
                "   THEN excluded.newest_detected_at ELSE watermarks.newest_detected_at END",
                (detection_id, newest, self._clock()),
            )

    # ----------------------------------------------------------------------- queue

    def enqueue_alert(self, payload: Mapping[str, Any]) -> int:
        with self._tx() as db:
            cursor = db.execute(
                "INSERT INTO queue (kind, payload_json, enqueued_at) VALUES ('alert', ?, ?)",
                (json.dumps(dict(payload), sort_keys=True), self._clock()),
            )
            return int(cursor.lastrowid or 0)

    def take(self, limit: int = 100, kinds: Sequence[str] = ("alert", "hit")) -> list[QueueItem]:
        """The oldest untaken items, marked taken (at-most-once hand-off to one consumer)."""
        marks = ",".join("?" * len(kinds))
        with self._tx() as db:
            rows = db.execute(
                f"SELECT * FROM queue WHERE taken_at IS NULL AND kind IN ({marks})"
                " ORDER BY seq LIMIT ?",
                (*kinds, limit),
            ).fetchall()
            if rows:
                db.execute(
                    f"UPDATE queue SET taken_at = ? WHERE seq IN ({','.join('?' * len(rows))})",
                    (self._clock(), *[row["seq"] for row in rows]),
                )
        return [QueueItem(row["seq"], row["kind"], json.loads(row["payload_json"]),
                          row["enqueued_at"]) for row in rows]  # fmt: skip

    def release(self, seqs: Sequence[int]) -> None:
        """Put taken items back (a hand-off that failed); consumers dedupe by hit key."""
        if not seqs:
            return
        marks = ",".join("?" * len(seqs))
        with self._tx() as db:
            db.execute(f"UPDATE queue SET taken_at = NULL WHERE seq IN ({marks})", tuple(seqs))

    def last_taken_at(self) -> float | None:
        """When the control plane last took an item (the Watchdog's 'age of the last hit')."""
        row = self._connection().execute("SELECT MAX(taken_at) FROM queue").fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def pending(self) -> int:
        row = self._connection().execute("SELECT COUNT(*) FROM queue WHERE taken_at IS NULL")
        return int(row.fetchone()[0])

    # ----------------------------------------------------------------------- archive

    def archive(self, dest: Path) -> dict[str, int]:
        """Move the store's hit history aside before a real Sentinel run (or T0).

        Rehearsals leave hits whose dispositions name stand-in incidents (``INC-REH-…``); a real
        Sentinel must not inherit them, and I7 must not count them. The whole database is copied
        to ``dest`` with SQLite's online backup (the evidence stays), then the live store drops
        its claimed hits and its queue. Watermarks and webhook event ids stay, so the poller does
        not re-sweep the archived history and a replayed old delivery is still a duplicate."""
        dest = Path(dest)
        if dest.exists():
            raise FileExistsError(f"{dest} exists: archives are never overwritten")
        dest.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(str(dest))
        try:
            self._connection().backup(target)
        finally:
            target.close()
        with self._tx() as db:
            hits = db.execute("DELETE FROM seen_hits").rowcount
            queued = db.execute("DELETE FROM queue").rowcount
        return {"hits": int(hits), "queued": int(queued)}


def _seen(row: sqlite3.Row) -> SeenHit:
    return SeenHit(row["hit_key"], row["hit_id"], row["detection_id"], row["trace_id"],
                   row["span_id"], row["source"], row["receptor"], row["host"],
                   row["detected_at"], row["incident"], row["disposition"])  # fmt: skip


class QueueSink:
    """The local stand-in for the control plane: hits are already queued by ``claim_hit``."""

    def __init__(self) -> None:
        self.emitted: list[HitEvent] = []

    def emit(self, hit: HitEvent) -> None:
        self.emitted.append(hit)
