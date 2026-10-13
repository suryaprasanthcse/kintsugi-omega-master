"""Activities: every piece of non-deterministic work in the control plane (spec §5.3.2).

Workflow code never calls an LLM, git, HTTP, a clock or a random generator (§5.3.3); it asks an
activity. Each activity here carries the §5.3.2 contract as data (``SPECS``): start-to-close
timeout, heartbeat timeout, retry policy, non-retryable error types, idempotency key and the task
queue (worker pool) that runs it. The work itself is delegated to a ``Crew`` port, which Stage 6
fills with the real roles; Stage 5 tests use scripted crews.

Idempotency. Before doing the work an activity looks its key up in a ``StepLedger``: a recorded
``DONE`` result is returned as is (a retried or replayed step never repeats a side effect), and a
new result is recorded before the activity returns. Steps whose side effect can outlive the
process (the Surgeon's commit, the ship's branch and PR) additionally check for the side effect
itself on a retry (``kintsugi.control.surgeon_step``, Stage 5 commit 3).

Heartbeats. Long activities heartbeat at a third of their heartbeat timeout while the crew works
in a thread, so a killed worker is noticed within one heartbeat timeout (§6.6: 60 s) and the
retry policy schedules attempt 2.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol

from temporalio import activity
from temporalio.api.common.v1 import Payload
from temporalio.common import RetryPolicy
from temporalio.converter import default as default_converter
from temporalio.exceptions import ApplicationError

from kintsugi.control import contracts as c
from kintsugi.control.dbconn import connect, enable_wal

CREW, SURGEON, SENSES = "kintsugi-crew", "kintsugi-surgeon", "kintsugi-senses"
TASK_QUEUES = (CREW, SURGEON, SENSES)


# --------------------------------------------------------------------------- errors


class InvalidAlert(Exception):
    """triage: the alert does not describe a real failure (non-retryable)."""


class BudgetExceeded(Exception):
    """surgeon: the incident's budget is spent (non-retryable; G6)."""


class ProtectedPathViolation(Exception):
    """surgeon: the attempt touched a protected path (non-retryable; §5.11)."""


class DeliveryRefused(Exception):
    """A sealed sabotage cannot be delivered (no port for its class): retrying cannot help."""


NON_RETRYABLE = (InvalidAlert, BudgetExceeded, ProtectedPathViolation, DeliveryRefused)


# --------------------------------------------------------------------------- the §5.3.2 table


@dataclass(frozen=True)
class ActivitySpec:
    name: str
    start_to_close: timedelta
    heartbeat: timedelta | None
    max_attempts: int
    non_retryable: tuple[str, ...]
    task_queue: str
    key: Callable[..., tuple[Any, ...]]
    # The answer depends on when it is asked, not only on the arguments (a fleet scan: hosts
    # get protected between a maturation and a wave), so one run's retries may reuse it but
    # another workflow must not: the key carries the calling workflow's id.
    per_workflow: bool = False

    def retry_policy(self) -> RetryPolicy:
        return RetryPolicy(
            initial_interval=timedelta(seconds=1),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(minutes=1),
            maximum_attempts=self.max_attempts,
            non_retryable_error_types=list(self.non_retryable),
        )

    def idempotency_key(self, *args: Any, workflow_id: str | None = None) -> str:
        parts = self.key(*args)
        if self.per_workflow:
            parts = (*parts, workflow_id or "")
        return f"{self.name}:{_digest(parts)}"


def _digest(parts: tuple[Any, ...]) -> str:
    raw = json.dumps([_plain(p) for p in parts], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _plain(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return {k: _plain(getattr(value, k)) for k in value.__dataclass_fields__}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    return value


M = timedelta(minutes=1)
S = timedelta(seconds=1)
_TABLE: tuple[ActivitySpec, ...] = (
    # Spec §5.3.2, row by row.
    # Keys carry the pass number (0, +1 per canary re-entry) so a re-entry is new work, and a
    # hypothesis id is unique within an incident across passes.
    ActivitySpec("triage", 5 * M, None, 3, ("InvalidAlert",), CREW,
                 lambda alert: (alert.incident_id, alert.reentries, "triage")),
    ActivitySpec("reproduce", 15 * M, 60 * S, 3, (), CREW,
                 lambda sig: (sig.incident_id, sig.pass_no, sig.phi)),
    ActivitySpec("attribute", 20 * M, 60 * S, 2, (), CREW,
                 lambda sig, repro: (sig.incident_id, sig.pass_no, "attribute")),
    ActivitySpec("plan_budget", 2 * M, None, 3, (), CREW,
                 lambda sig, cause: (sig.incident_id, sig.pass_no, cause.suspects,
                                     cause.intent_record_sha)),
    ActivitySpec("surgeon", 45 * M, 60 * S, 2, ("BudgetExceeded", "ProtectedPathViolation"),
                 SURGEON, lambda card: (card.incident_id, card.hypothesis, card.round)),
    ActivitySpec("audit_gates", 60 * M, 60 * S, 2, (), CREW,
                 lambda out: (out.incident_id, out.hypothesis, out.diff_sha or out.commit_sha)),
    ActivitySpec("coordinate", 5 * M, None, 3, (), CREW,
                 lambda sig, rnd, verdicts: (sig.incident_id, sig.pass_no, rnd)),
    ActivitySpec("dispute_experiment", 30 * M, 60 * S, 1, (), CREW,
                 lambda incident_id, decision: (incident_id, decision.experiment_sha)),
    ActivitySpec("ship", 10 * M, None, 5, (), CREW,
                 lambda out: (f"kintsugi/{out.incident_id}/{out.hypothesis}",)),
    ActivitySpec("revert", 10 * M, None, 5, (), CREW,
                 lambda shipment: (shipment.merge_sha or shipment.branch, "revert")),
    ActivitySpec("synthesize_antibody", 20 * M, 60 * S, 2, (), CREW,
                 lambda out, cause: (out.incident_id, "ab")),
    ActivitySpec("ledger_append", 1 * M, None, 10, (), CREW,
                 lambda entry: (hashlib.sha256(json.dumps(entry, sort_keys=True).encode())
                                .hexdigest(),)),
    # The loop's other steps (§5.13), with the default contract: 5 minutes, 3 attempts.
    ActivitySpec("route_hit", 2 * M, None, 3, (), CREW,
                 lambda hit, open_incidents: (hit.hit_key, "route")),
    ActivitySpec("record_transition", 1 * M, None, 10, (), CREW,
                 lambda t: (t.incident_id, t.seq)),
    ActivitySpec("tag_traces", 5 * M, None, 3, (), SENSES,
                 lambda incident_id, trace_ids, phase: (incident_id, tuple(trace_ids), phase)),
    ActivitySpec("match_antibody", 5 * M, None, 3, (), CREW,
                 lambda sig: (sig.incident_id, sig.pass_no, sig.phi, "match")),
    ActivitySpec("fixes_prior", 5 * M, None, 2, (), CREW,
                 lambda sig: (sig.incident_id, sig.pass_no, "prior")),
    ActivitySpec("hypothesize", 10 * M, 60 * S, 3, (), CREW,
                 lambda sig, cause, plan, rnd, history, prior: (sig.incident_id, sig.pass_no,
                                                                 rnd)),
    ActivitySpec("rewind", 5 * M, None, 3, (), SURGEON,
                 lambda outs: tuple((o.incident_id, o.hypothesis, o.attempt) for o in outs)),
    ActivitySpec("rebase_and_rerun", 20 * M, 60 * S, 2, (), SURGEON,
                 lambda outs, repo, new_main: (repo, new_main, len(outs))),
    ActivitySpec("budget_exhausted", 2 * M, None, 3, (), CREW,
                 lambda sig, plan, rnd: (sig.incident_id, sig.pass_no, rnd, "spent")),
    ActivitySpec("park", 5 * M, None, 5, (), CREW,
                 lambda sig, reason: (sig.incident_id, sig.pass_no, "park")),
    ActivitySpec("escalate", 5 * M, None, 5, (), CREW,
                 lambda incident_id, reason: (incident_id, "escalate")),
    ActivitySpec("quarantine_related", 5 * M, None, 3, (), CREW,
                 lambda shipment: (shipment.incident_id, shipment.merge_sha, "quarantine")),
    ActivitySpec("backfill_receptor", 10 * M, None, 2, (), SENSES,
                 lambda ab: (ab.antibody_id, ab.version, "backfill")),
    ActivitySpec("fixes_resolve", 5 * M, None, 3, (), CREW,
                 lambda sig, shipment: (sig.incident_id, shipment.pr_url, "resolve")),
    ActivitySpec("canary_sample", 10 * M, 60 * S, 3, (), SENSES,
                 lambda shipment, batch: (shipment.merge_sha or shipment.branch, batch)),
    ActivitySpec("canary_plan", 2 * M, None, 3, (), CREW,
                 lambda shipment: (shipment.incident_id, shipment.merge_sha or shipment.branch,
                                   "plan")),
    # Stage 7: the antibody lifecycle (§5.8.2, §6.5 steps 13–14, §6.7 steps 16–19).
    ActivitySpec("mature_antibody", 45 * M, 60 * S, 2, ("ProtectedPathViolation",), SURGEON,
                 lambda ab, host: (ab.antibody_id, ab.version, host, "mature")),
    ActivitySpec("promote_antibody", 5 * M, None, 3, (), CREW,
                 lambda ab, host: (ab.antibody_id, ab.version, host, "promote")),
    ActivitySpec("revert_vaccination", 30 * M, 60 * S, 3, (), CREW,
                 lambda shipment: (shipment.merge_sha or shipment.branch, "revert-vaccination")),
    ActivitySpec("refine_antibody", 45 * M, 60 * S, 2, (), CREW,
                 lambda ab, shipment: (ab.antibody_id, ab.version, shipment.merge_sha, "refine")),
    ActivitySpec("apply_antibody", 45 * M, 60 * S, 2, ("ProtectedPathViolation",), SURGEON,
                 lambda ab, host: (ab.antibody_id, ab.version, host)),
    ActivitySpec("motif_scan", 10 * M, None, 3, (), CREW,
                 lambda ab: (ab.antibody_id, ab.version, "scan"), per_workflow=True),
    ActivitySpec("onboard_step", 30 * M, 60 * S, 3, (), CREW,
                 lambda host, step: (host, step)),
    ActivitySpec("seal_sabotage", 5 * M, None, 3, (), CREW,
                 lambda spec_id: (spec_id, "seal")),
    ActivitySpec("deliver_sabotage", 10 * M, None, 3, ("DeliveryRefused",), CREW,
                 lambda spec_id: (spec_id, "deliver")),
    ActivitySpec("anchor_ledger", 5 * M, None, 5, (), CREW,
                 lambda hour: (hour, "anchor")),
    # §7.4: the incident's proof bundle, once per close (a canary re-entry closes later).
    ActivitySpec("emit_bundle", 5 * M, None, 3, (), CREW,
                 lambda closing: (closing["incident"], closing.get("pass", 0), "bundle")),
)  # fmt: skip
SPECS: Mapping[str, ActivitySpec] = {spec.name: spec for spec in _TABLE}


# --------------------------------------------------------------------------- idempotency store


class StepLedger(Protocol):
    def get(self, key: str) -> bytes | None: ...

    def put(self, key: str, data: bytes) -> None: ...


class MemoryLedger:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> bytes | None:
        with self._lock:
            return self.data.get(key)

    def put(self, key: str, data: bytes) -> None:
        with self._lock:
            self.data.setdefault(key, data)


class SqliteLedger:
    """Activity results by idempotency key, shared by every worker on the machine."""

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        enable_wal(self.path)  # once per file, retried (kintsugi.control.dbconn)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS activity_results (idem_key TEXT PRIMARY KEY,"
                       " data BLOB NOT NULL, at REAL DEFAULT (julianday('now')))")  # fmt: skip

    def _db(self) -> AbstractContextManager[sqlite3.Connection]:
        return connect(self.path)  # shared by every worker: waits, commits, always closes

    def get(self, key: str) -> bytes | None:
        with self._db() as db:
            row = db.execute("SELECT data FROM activity_results WHERE idem_key = ?", (key,))
            found = row.fetchone()
        return bytes(found[0]) if found else None

    def put(self, key: str, data: bytes) -> None:
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO activity_results (idem_key, data) VALUES (?, ?)",
                       (key, data))  # fmt: skip


_CONVERTER = default_converter().payload_converter


def encode(value: Any) -> bytes:
    payload = _CONVERTER.to_payloads([value])[0]
    return json.dumps({"m": {k: v.decode() for k, v in payload.metadata.items()},
                       "d": payload.data.decode("utf-8")}).encode("utf-8")  # fmt: skip


def decode(data: bytes, type_hint: Any) -> Any:
    raw = json.loads(data)
    payload = Payload(metadata={k: v.encode() for k, v in raw["m"].items()},
                      data=raw["d"].encode("utf-8"))  # fmt: skip
    return _CONVERTER.from_payloads([payload], [type_hint])[0]


# --------------------------------------------------------------------------- the crew port


@dataclass(frozen=True)
class StepContext:
    name: str
    key: str
    attempt: int
    heartbeat: Callable[..., None]


class Crew(Protocol):
    """Does the work of an activity. Stage 6 binds the real roles; tests script it."""

    def handle(self, ctx: StepContext, *args: Any) -> Any: ...


class ScriptedCrew:
    """A crew whose answers are functions of (activity, args); records every call."""

    def __init__(self, answers: Mapping[str, Callable[..., Any]]) -> None:
        self.answers = dict(answers)
        self.calls: list[tuple[str, int]] = []
        self._lock = threading.Lock()

    def handle(self, ctx: StepContext, *args: Any) -> Any:
        with self._lock:
            self.calls.append((ctx.name, ctx.attempt))
        if ctx.name not in self.answers:
            raise NotImplementedError(f"the scripted crew has no answer for {ctx.name}")
        return self.answers[ctx.name](*args)

    def count(self, name: str) -> int:
        return sum(1 for called, _ in self.calls if called == name)


# --------------------------------------------------------------------------- activities


class OmegaActivities:
    """The activity implementations; register ``activities_for(queue)`` with a worker."""

    def __init__(
        self,
        crew: Crew,
        ledger: StepLedger | None = None,
        *,
        heartbeat_interval_s: float | None = None,  # default: a third of the heartbeat timeout
    ) -> None:
        self.crew = crew
        self.ledger = ledger or MemoryLedger()
        self.heartbeat_interval_s = heartbeat_interval_s

    async def _run(self, name: str, result_type: Any, *args: Any) -> Any:
        spec = SPECS[name]
        info = activity.info()
        key = spec.idempotency_key(*args, workflow_id=info.workflow_id)
        recorded = self.ledger.get(key)
        if recorded is not None:
            return decode(recorded, result_type)
        ctx = StepContext(name, key, info.attempt, activity.heartbeat)
        try:
            if spec.heartbeat is None:
                result = await asyncio.to_thread(self.crew.handle, ctx, *args)
            else:
                interval = self.heartbeat_interval_s or spec.heartbeat.total_seconds() / 3
                result = await _with_heartbeats(interval, lambda: self.crew.handle(ctx, *args))
        except NON_RETRYABLE as error:
            raise ApplicationError(str(error), type=type(error).__name__,
                                   non_retryable=True) from error  # fmt: skip
        self.ledger.put(key, encode(result))
        return result

    @activity.defn(name="route_hit")
    async def route_hit(self, hit: c.Hit, open_incidents: list[str]) -> c.RouteDecision:
        return await self._run("route_hit", c.RouteDecision, hit, open_incidents)

    @activity.defn(name="record_transition")
    async def record_transition(self, record: c.TransitionRecord) -> None:
        return await self._run("record_transition", None, record)

    @activity.defn(name="triage")
    async def triage(self, alert: c.Alert) -> c.Signature:
        return await self._run("triage", c.Signature, alert)

    @activity.defn(name="tag_traces")
    async def tag_traces(self, incident_id: str, trace_ids: list[str], phase: str) -> int:
        return await self._run("tag_traces", int, incident_id, trace_ids, phase)

    @activity.defn(name="match_antibody")
    async def match_antibody(self, sig: c.Signature) -> c.AntibodyMatch | None:
        return await self._run("match_antibody", c.AntibodyMatch | None, sig)

    @activity.defn(name="reproduce")
    async def reproduce(self, sig: c.Signature) -> c.Repro:
        return await self._run("reproduce", c.Repro, sig)

    @activity.defn(name="park")
    async def park(self, sig: c.Signature, reason: str) -> None:
        return await self._run("park", None, sig, reason)

    @activity.defn(name="attribute")
    async def attribute(self, sig: c.Signature, repro: c.Repro) -> c.Cause:
        return await self._run("attribute", c.Cause, sig, repro)

    @activity.defn(name="plan_budget")
    async def plan_budget(self, sig: c.Signature, cause: c.Cause) -> c.BudgetPlan:
        return await self._run("plan_budget", c.BudgetPlan, sig, cause)

    @activity.defn(name="fixes_prior")
    async def fixes_prior(self, sig: c.Signature) -> str | None:
        return await self._run("fixes_prior", str | None, sig)

    @activity.defn(name="hypothesize")
    async def hypothesize(
        self,
        sig: c.Signature,
        cause: c.Cause,
        plan: c.BudgetPlan,
        rnd: int,
        history: list[c.GateVerdict],
        prior: str | None,
    ) -> list[c.Card]:
        """Coordinator: K cards. ``prior`` is a Fixes investigation or an antibody's recipe."""
        return await self._run("hypothesize", list[c.Card], sig, cause, plan, rnd, history,
                               prior)  # fmt: skip

    @activity.defn(name="surgeon")
    async def surgeon(self, card: c.Card) -> c.SurgeonOutcome:
        return await self._run("surgeon", c.SurgeonOutcome, card)

    @activity.defn(name="audit_gates")
    async def audit_gates(self, out: c.SurgeonOutcome) -> c.GateVerdict:
        return await self._run("audit_gates", c.GateVerdict, out)

    @activity.defn(name="coordinate")
    async def coordinate(
        self, sig: c.Signature, rnd: int, verdicts: list[c.GateVerdict]
    ) -> c.Decision:
        return await self._run("coordinate", c.Decision, sig, rnd, verdicts)

    @activity.defn(name="dispute_experiment")
    async def dispute_experiment(self, incident_id: str, decision: c.Decision) -> c.Decision:
        return await self._run("dispute_experiment", c.Decision, incident_id, decision)

    @activity.defn(name="rewind")
    async def rewind(self, outs: list[c.SurgeonOutcome]) -> None:
        return await self._run("rewind", None, outs)

    @activity.defn(name="rebase_and_rerun")
    async def rebase_and_rerun(
        self, outs: list[c.SurgeonOutcome], repo: str, new_main: str
    ) -> None:
        return await self._run("rebase_and_rerun", None, outs, repo, new_main)

    @activity.defn(name="budget_exhausted")
    async def budget_exhausted(self, sig: c.Signature, plan: c.BudgetPlan, rnd: int) -> bool:
        return await self._run("budget_exhausted", bool, sig, plan, rnd)

    @activity.defn(name="escalate")
    async def escalate(self, incident_id: str, reason: str) -> None:
        return await self._run("escalate", None, incident_id, reason)

    @activity.defn(name="ship")
    async def ship(self, out: c.SurgeonOutcome) -> c.Shipment:
        return await self._run("ship", c.Shipment, out)

    @activity.defn(name="revert")
    async def revert(self, shipment: c.Shipment) -> bool:
        return await self._run("revert", bool, shipment)

    @activity.defn(name="quarantine_related")
    async def quarantine_related(self, shipment: c.Shipment) -> int:
        return await self._run("quarantine_related", int, shipment)

    @activity.defn(name="synthesize_antibody")
    async def synthesize_antibody(self, out: c.SurgeonOutcome, cause: c.Cause) -> c.AntibodyRef:
        return await self._run("synthesize_antibody", c.AntibodyRef, out, cause)

    @activity.defn(name="backfill_receptor")
    async def backfill_receptor(self, ab: c.AntibodyRef) -> None:
        return await self._run("backfill_receptor", None, ab)

    @activity.defn(name="fixes_resolve")
    async def fixes_resolve(self, sig: c.Signature, shipment: c.Shipment) -> None:
        return await self._run("fixes_resolve", None, sig, shipment)

    @activity.defn(name="ledger_append")
    async def ledger_append(self, entry: dict[str, Any]) -> str:
        return await self._run("ledger_append", str, entry)

    @activity.defn(name="canary_sample")
    async def canary_sample(self, shipment: c.Shipment, batch: int) -> list[bool]:
        return await self._run("canary_sample", list[bool], shipment, batch)

    @activity.defn(name="canary_plan")
    async def canary_plan(self, shipment: c.Shipment) -> c.CanaryPlan:
        return await self._run("canary_plan", c.CanaryPlan, shipment)

    @activity.defn(name="mature_antibody")
    async def mature_antibody(self, ab: c.AntibodyRef, host: str) -> c.VaccinationResult:
        return await self._run("mature_antibody", c.VaccinationResult, ab, host)

    @activity.defn(name="promote_antibody")
    async def promote_antibody(self, ab: c.AntibodyRef, host: str) -> c.AntibodyRef:
        return await self._run("promote_antibody", c.AntibodyRef, ab, host)

    @activity.defn(name="revert_vaccination")
    async def revert_vaccination(self, shipment: c.Shipment) -> c.Shipment | None:
        return await self._run("revert_vaccination", c.Shipment | None, shipment)

    @activity.defn(name="refine_antibody")
    async def refine_antibody(
        self, ab: c.AntibodyRef, shipment: c.Shipment
    ) -> c.AntibodyRef | None:
        return await self._run("refine_antibody", c.AntibodyRef | None, ab, shipment)

    @activity.defn(name="apply_antibody")
    async def apply_antibody(self, ab: c.AntibodyRef, host: str) -> c.VaccinationResult:
        return await self._run("apply_antibody", c.VaccinationResult, ab, host)

    @activity.defn(name="motif_scan")
    async def motif_scan(self, ab: c.AntibodyRef) -> list[str]:
        return await self._run("motif_scan", list[str], ab)

    @activity.defn(name="onboard_step")
    async def onboard_step(self, host: str, step: str) -> c.StepResult:
        return await self._run("onboard_step", c.StepResult, host, step)

    @activity.defn(name="seal_sabotage")
    async def seal_sabotage(self, spec_id: str) -> str:
        return await self._run("seal_sabotage", str, spec_id)

    @activity.defn(name="deliver_sabotage")
    async def deliver_sabotage(self, spec_id: str) -> None:
        return await self._run("deliver_sabotage", None, spec_id)

    @activity.defn(name="anchor_ledger")
    async def anchor_ledger(self, hour: int) -> str:
        return await self._run("anchor_ledger", str, hour)

    @activity.defn(name="emit_bundle")
    async def emit_bundle(self, closing: dict[str, Any]) -> c.BundleRef:
        return await self._run("emit_bundle", c.BundleRef, closing)

    def activities_for(self, queue: str) -> list[Callable[..., Any]]:
        return [getattr(self, name) for name, spec in SPECS.items() if spec.task_queue == queue]

    def all(self) -> list[Callable[..., Any]]:
        return [getattr(self, name) for name in SPECS]


async def _with_heartbeats(interval_s: float, work: Callable[[], Any]) -> Any:
    """Run blocking ``work`` in a thread, heartbeating every ``interval_s`` until it returns."""
    task = asyncio.ensure_future(asyncio.to_thread(work))
    interval = max(0.05, interval_s)
    while True:
        done, _ = await asyncio.wait({task}, timeout=interval)
        if done:
            return task.result()
        activity.heartbeat("working")


def specs_for(queue: str) -> Sequence[ActivitySpec]:
    return [spec for spec in SPECS.values() if spec.task_queue == queue]
