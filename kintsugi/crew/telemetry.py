"""The crew's own neatlogs telemetry (spec §4.1.2).

- Every crew role runs under ``workflow_name = "kintsugi:<role>"``: one ``neatlogs.Client`` per
  role (the SDK's context-scoped clients), activated around each step that role performs.
- A step that belongs to an incident is a trace whose root is the ``WORKFLOW`` span
  ``incident:<INC>``, carrying the session id ``inc-<INC>``; the step itself is an ``AGENT`` span
  ``<role>.<activity>`` under it. ``GET /traces?sessionId=inc-<INC>`` and
  ``GET /sessions/inc-<INC>/export`` then return every crew trace of the incident.
- **Cost, exactly once.** All spend lands in the case file's ``costs.jsonl`` (headless sessions
  report ``total_cost_usd``; ``CaseFile.add_cost``). When a step ends, its trace claims the
  incident's cost lines no trace has reported yet (a cursor in SQLite, shared by the crew and
  Surgeon pool processes) and carries their sum as ``neatlogs.agent.cost_usd``. Concurrent steps
  may split lines between them, but each line is reported once, so the session total equals the
  incident's recorded actuals: what audit C10 reconciles within 1%. Whether neatlogs sums that
  attribute into its session cost is spike A3 (§4.1.2 [A]); the journal keeps the same numbers.
- **Journal.** Each step is also appended to ``<state>/<INC>.jsonl`` (role, activity, workflow
  name, session, trace id, attempt, cost, whether it was exported); the proof bundle attaches it.

Without ``NEATLOGS_API_KEY`` the clients are created with export disabled: spans and journal
are still produced and nothing leaves the machine (as for the hosts, ``hostkit.telemetry``).
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from kintsugi.control.dbconn import connect, enable_wal

# Activity → the §5.7 role that performs it (the ledger plumbing runs as the notary).
ROLES: Mapping[str, str] = {
    "route_hit": "sentinel",
    "triage": "pathologist", "reproduce": "pathologist", "tag_traces": "pathologist",
    "park": "pathologist",
    "attribute": "archivist", "fixes_prior": "archivist",
    "plan_budget": "coordinator", "hypothesize": "coordinator", "coordinate": "coordinator",
    "dispute_experiment": "coordinator", "budget_exhausted": "coordinator",
    "escalate": "coordinator", "ship": "coordinator", "revert": "coordinator",
    "fixes_resolve": "coordinator", "onboard_step": "coordinator",
    "surgeon": "surgeon", "rewind": "surgeon", "rebase_and_rerun": "surgeon",
    "audit_gates": "auditor", "canary_plan": "auditor", "canary_sample": "auditor",
    "emit_bundle": "auditor",
    "match_antibody": "immunologist", "synthesize_antibody": "immunologist",
    "motif_scan": "immunologist", "mature_antibody": "immunologist",
    "promote_antibody": "immunologist", "apply_antibody": "immunologist",
    "backfill_receptor": "immunologist", "quarantine_related": "immunologist",
    "revert_vaccination": "immunologist", "refine_antibody": "immunologist",
    "seal_sabotage": "saboteur", "deliver_sabotage": "saboteur",
    "record_transition": "notary", "ledger_append": "notary", "anchor_ledger": "notary",
}  # fmt: skip


def role_of(activity: str) -> str:
    return ROLES.get(activity, "coordinator")


def workflow_name(role: str) -> str:
    return f"kintsugi:{role}"


def session_id(incident: str) -> str:
    return f"inc-{incident}"


BY_ID = ("escalate", "dispute_experiment", "tag_traces")  # the incident id is their first arg


def incident_of(activity: str, args: Sequence[Any]) -> str | None:
    """The incident a step works for, from its arguments (None for fleet-level steps)."""
    if activity in BY_ID and args and isinstance(args[0], str):
        return args[0]
    for arg in args:
        found = getattr(arg, "incident_id", None)
        if isinstance(found, str) and found:
            return found
        if isinstance(arg, Mapping) and isinstance(arg.get("incident"), str) and arg["incident"]:
            return str(arg["incident"])
        if isinstance(arg, (list, tuple)) and arg:
            inner = getattr(arg[0], "incident_id", None)
            if isinstance(inner, str) and inner:
                return inner
    return None


class CostCursor:
    """How many of an incident's cost lines are already on a trace (shared across processes)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        enable_wal(self.path)
        with connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS cost_cursor (incident TEXT PRIMARY KEY,"
                       " line INTEGER NOT NULL)")  # fmt: skip

    def claim(self, incident: str, lines: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """The lines past the cursor, which moves to the end: each line is claimed once."""
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT line FROM cost_cursor WHERE incident = ?",
                             (incident,)).fetchone()  # fmt: skip
            start = int(row[0]) if row else 0
            fresh = [dict(line) for line in lines[start:]]
            db.execute("INSERT INTO cost_cursor (incident, line) VALUES (?, ?) ON CONFLICT"
                       "(incident) DO UPDATE SET line = excluded.line",
                       (incident, max(start, len(lines))))  # fmt: skip
        return fresh


ProviderFactory = Callable[[str], Any]  # role → a private OpenTelemetry TracerProvider (tests)


class CrewTelemetry:
    def __init__(
        self,
        state: Path,
        *,
        api_key: str | None = None,
        endpoint: str | None = None,
        tracer_provider: ProviderFactory | None = None,
    ) -> None:
        self.state = Path(state)
        self.api_key = (api_key or "").strip() or None
        self.endpoint = endpoint
        self.tracer_provider = tracer_provider
        self.cursor = CostCursor(self.state / "cost_cursor.sqlite")
        self._clients: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._journal_lock = threading.Lock()

    @classmethod
    def from_env(cls, state: Path, env: Mapping[str, str]) -> CrewTelemetry:
        return cls(state, api_key=env.get("NEATLOGS_API_KEY"),
                   endpoint=env.get("NEATLOGS_ENDPOINT"))  # fmt: skip

    @property
    def exporting(self) -> bool:
        return self.api_key is not None

    def client(self, role: str) -> Any:
        import neatlogs

        with self._lock:
            found = self._clients.get(role)
            if found is None:
                kwargs: dict[str, Any] = {
                    "api_key": self.api_key or "",
                    "workflow_name": workflow_name(role),
                    "tags": [f"role:{role}"],
                    "disable_export": not self.exporting,
                }
                if self.endpoint:
                    kwargs["endpoint"] = self.endpoint
                if self.tracer_provider is not None:
                    kwargs["tracer_provider"] = self.tracer_provider(role)
                found = self._clients[role] = neatlogs.Client(**kwargs)
            return found

    def journal_path(self, incident: str) -> Path:
        return self.state / f"{incident}.jsonl"

    def _journal(self, incident: str, record: Mapping[str, Any]) -> None:
        path = self.journal_path(incident)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(dict(record), sort_keys=True) + "\n"
        with self._journal_lock, path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)

    @contextmanager
    def step(
        self,
        activity: str,
        incident: str | None,
        *,
        attempt: int = 1,
        cost_lines: Callable[[str], Sequence[Mapping[str, Any]]] | None = None,
    ) -> Iterator[Any]:
        """Trace one crew step under its role's client; claim and report its incident's spend."""
        import neatlogs

        role = role_of(activity)
        started = time.time()
        with self.client(role).activate():
            if incident is None:
                with neatlogs.trace(f"{role}.{activity}", kind="AGENT") as span:
                    span.set_attribute("kintsugi.attempt", attempt)
                    yield span
                return
            with neatlogs.trace(f"incident:{incident}", kind="WORKFLOW",
                                session_id=session_id(incident)) as root:  # fmt: skip
                root.set_attribute("kintsugi.incident", incident)
                ok = False
                with neatlogs.trace(f"{role}.{activity}", kind="AGENT") as span:
                    span.set_attribute("kintsugi.attempt", attempt)
                    span.set_attribute("kintsugi.role", role)
                    try:
                        yield span
                        ok = True
                    finally:
                        claimed = (self.cursor.claim(incident, cost_lines(incident))
                                   if cost_lines is not None else [])  # fmt: skip
                        usd = round(sum(float(line.get("usd") or 0.0) for line in claimed), 6)
                        span.set_attribute("neatlogs.agent.cost_usd", usd)
                        span.set_attribute("kintsugi.cost_lines", len(claimed))
                        context = root.get_span_context()
                        self._journal(incident, {
                            "at": round(started, 3), "role": role, "activity": activity,
                            "workflow": workflow_name(role), "session": session_id(incident),
                            "trace_id": f"{context.trace_id:032x}", "attempt": attempt,
                            "cost_usd": usd, "cost_lines": len(claimed), "ok": ok,
                            "exported": self.exporting,
                            "duration_s": round(time.time() - started, 3),
                        })  # fmt: skip

    def flush(self) -> None:
        with self._lock:
            clients = list(self._clients.values())
        for client in clients:
            client.flush()
