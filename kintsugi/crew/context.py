"""What every crew role is given: case files, targets, the model, the senses (spec §5.7).

A role never reaches for global state: the worker builds one :class:`CrewContext` and each role
receives it. Failing traces come through a :class:`TraceSource`: the neatlogs Public API in
production (``records.load_trace`` → ``signature.from_trace``) or the observations a rehearsal
recorded locally (``signature.from_chaos_log``).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from kintsugi.control import contracts as c
from kintsugi.control.dbconn import connect, enable_wal
from kintsugi.crew.casefile import CaseFile
from kintsugi.crew.llm import Model
from kintsugi.gates.canary import BASELINES
from kintsugi.gates.target import Target
from kintsugi.senses.detections import Binding
from kintsugi.senses.signature import FailureObservation, from_trace
from kintsugi.senses.store import SenseStore


class TraceSource(Protocol):
    def observations(self, alert: c.Alert) -> list[FailureObservation]: ...


class NeatlogsTraces:
    """Failing traces through the neatlogs Public API (Stage 4 client)."""

    def __init__(self, api: Any) -> None:
        self.api = api

    def observations(self, alert: c.Alert) -> list[FailureObservation]:
        from kintsugi.senses.records import load_trace

        found: dict[str, FailureObservation] = {}
        for hit in alert.hits:
            if hit.trace_id in found:
                continue
            obs = from_trace(load_trace(self.api, hit.trace_id), [hit.detection_id])
            if obs is not None:
                found[hit.trace_id] = obs
        return list(found.values())


class LocalTraces:
    """Observations a run recorded as JSON lines (``<dir>/*.jsonl``), keyed by trace id."""

    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)

    @staticmethod
    def write(folder: Path, name: str, observations: Iterable[FailureObservation]) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{name}.jsonl"
        lines = [json.dumps(vars(o), sort_keys=True, default=list) for o in observations]
        path.write_text("".join(line + "\n" for line in lines), encoding="utf-8", newline="\n")
        return path

    def _index(self) -> dict[str, FailureObservation]:
        index = {}
        for path in sorted(self.folder.glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    data = json.loads(line)
                    data["preceding"] = tuple(data["preceding"])
                    data["detection_ids"] = tuple(data["detection_ids"])
                    obs = FailureObservation(**data)
                    index[obs.trace_id] = obs
        return index

    def observations(self, alert: c.Alert) -> list[FailureObservation]:
        index = self._index()
        return [index[h.trace_id] for h in alert.hits if h.trace_id in index]


class IncidentIndex:
    """Incident ids and what each incident is about (host, receptors), shared by workers."""

    def __init__(self, path: Path, *, prefix: str = "INC", first: int = 1) -> None:
        self.path = str(path)
        self.prefix = prefix
        self.first = first
        self._lock = threading.Lock()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        enable_wal(self.path)  # once per file, retried (kintsugi.control.dbconn)
        with self._db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS incidents (id TEXT PRIMARY KEY, host TEXT, "
                "receptor TEXT, opened_at REAL, n INTEGER UNIQUE)"
            )

    def _db(self) -> AbstractContextManager[sqlite3.Connection]:
        return connect(self.path)  # shared by workers: waits, commits, always closes

    def open(self, host: str, receptor: str) -> str:
        with self._lock, self._db() as db:
            row = db.execute("SELECT COALESCE(MAX(n), ?) FROM incidents", (self.first - 1,))
            n = int(row.fetchone()[0]) + 1
            incident = f"{self.prefix}-{n:04d}"
            db.execute(
                "INSERT INTO incidents VALUES (?, ?, ?, ?, ?)",
                (incident, host, receptor, time.time(), n),
            )
            return incident

    def register(self, incident: str, host: str, receptor: str) -> str:
        """An incident whose id is not sequential (a vaccination ``VAC-…``, an L3 revert):
        recorded so its steps can find their host. Idempotent."""
        with self._lock, self._db() as db:
            db.execute(
                "INSERT OR IGNORE INTO incidents VALUES (?, ?, ?, ?, NULL)",
                (incident, host, receptor, time.time()),
            )
        return incident

    def receptor_of(self, incident: str) -> str | None:
        """What opened it: a receptor id, or ``antibody:AB-n@v`` for a vaccination."""
        with self._db() as db:
            row = db.execute("SELECT receptor FROM incidents WHERE id = ?", (incident,)).fetchone()
        return str(row[0]) if row and row[0] is not None else None

    def host_of(self, incident: str) -> str | None:
        with self._db() as db:
            row = db.execute("SELECT host FROM incidents WHERE id = ?", (incident,)).fetchone()
        return str(row[0]) if row else None


@dataclass
class CrewContext:
    state: Path  # .state/crew
    targets: Mapping[str, Target]
    model: Model | None = None
    store: SenseStore | None = None
    bindings: Mapping[str, Binding] = field(default_factory=dict)
    traces: TraceSource | None = None
    holdout_identity: Path | None = None  # read only inside the Auditor's G4 activity (I5)
    base_ref: str = "main"
    incident_prefix: str = "INC"
    models: Mapping[str, str] = field(default_factory=dict)  # role → model id override
    clock: Callable[[], float] = time.time
    g4_seeds: tuple[int, ...] = (1, 2, 3)  # holdout × seeds = the paired sample (§5.12)
    baselines: Path = BASELINES  # recorded host baselines: the canary's H0 (gates.canary)

    def case(self, incident: str) -> CaseFile:
        return CaseFile(self.state / "cases", incident)

    def target(self, host: str) -> Target:
        if host not in self.targets:
            raise KeyError(f"no repair target for host {host!r}")
        return self.targets[host]

    def model_for(self, role: str) -> str | None:
        return self.models.get(role)

    @property
    def incidents(self) -> IncidentIndex:
        return IncidentIndex(self.state / "incidents.sqlite", prefix=self.incident_prefix)
