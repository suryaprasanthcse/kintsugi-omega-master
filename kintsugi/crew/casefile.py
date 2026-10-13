"""The incident case file and the information-flow matrix (spec §5.7, §5.14).

Activities receive small typed messages (``kintsugi.control.contracts``); everything else an
incident accumulates (failing-trace observations, the cassette, the cause report and intent
record, cards, outcomes, gate evidence, decisions, costs, every crew call) lives in its case
file: ``<state>/crew/<INC>/<kind>/<name>.json`` plus three append-only logs (``calls.jsonl``,
``costs.jsonl``, ``access.jsonl``).

**Who sees what is enforced here**, not by convention. A role reads through
:meth:`CaseFile.reader`, which refuses any artifact outside that role's columns of the §5.7
matrix and logs every read. So the Auditor cannot read a Surgeon's self-report or transcript (it
"cannot be persuaded by them"), a Surgeon cannot read another hypothesis's reasoning or any
budget, and the Sentinel writes nothing but incident openings. ``access.jsonl`` is the evidence.

The matrix columns, as artifact classes:

==================  =========================================================================
``hits``            detection hits and the incident opening
``traces``          failing-trace observations and the cassette
``summary``         signature, symptom summary, G0 result
``source``          graph context and code excerpts
``intent``          cause report and intent record
``budget``          the budget plan and the cost log
``cards``           hypothesis cards
``diffs``           Surgeon outcomes (commits) and the orchestrator's commit refusals
``reasoning``       a Surgeon's self-report and session record
``verdicts``        full gate evidence, decisions, dispute protocols, L4 re-verification
``feedback``        aggregate gate feedback (what Surgeons may see)
``prior``           a Fixes investigation or an antibody recipe
``antibodies``      what the Immunologist synthesised, applied or quarantined for this case
==================  =========================================================================
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

KINDS: Mapping[str, str] = {
    "incident_open": "hits",
    "observations": "traces",
    "cassette": "traces",
    "trace_feedback": "traces",  # §4.2 write-back to the failing traces: culprit span, verdict
    "signature": "summary",
    "symptom": "summary",
    "repro": "summary",
    "graph_context": "source",
    "cause": "intent",
    "intent_record": "intent",
    "plan": "budget",
    "card": "cards",
    "outcome": "diffs",
    "refusal": "diffs",
    "surgeon_report": "reasoning",
    "surgeon_session": "reasoning",
    "verdict": "verdicts",
    "decision": "verdicts",
    "experiment": "verdicts",
    "reverify": "verdicts",
    "feedback": "feedback",
    "prior": "prior",
    "antibody": "antibodies",
}

# Spec §5.7 information-flow matrix (rows: roles; values: readable artifact classes). The
# governor and the shipper are deterministic orchestrator code with their own narrow rows; the
# governor reads the incident opening for its VaR estimate (§4.6.2), never code or verdicts.
_COORDINATOR = {"summary", "intent", "budget", "cards", "verdicts", "feedback", "prior"}
_AUDITOR = {"traces", "summary", "source", "intent", "cards", "diffs", "verdicts"}
# §5.7: the Immunologist sees failing traces, source, intent, diffs, verdicts and the budget
# (plus the cards and priors those reference), never holdout tasks or Surgeon reasoning. It
# assembles a vaccination's case file itself, because a secondary response has no Pathologist,
# Archivist or Coordinator pass: the trigger cassette, the card from the recipe, the plan.
_IMMUNOLOGIST = {"traces", "summary", "source", "intent", "cards", "diffs", "verdicts", "budget",
                 "prior", "antibodies"}  # fmt: skip
READS: Mapping[str, frozenset[str]] = {
    "sentinel": frozenset({"hits"}),
    "pathologist": frozenset({"hits", "traces", "summary"}),
    "archivist": frozenset({"summary", "source", "intent", "prior"}),
    "coordinator": frozenset(_COORDINATOR),
    "surgeon": frozenset({"summary", "intent", "cards", "feedback"}),
    "auditor": frozenset(_AUDITOR),
    "governor": frozenset({"budget", "summary", "hits"}),
    "shipper": frozenset({"summary", "intent", "budget", "cards", "diffs", "verdicts"}),
    "immunologist": frozenset(_IMMUNOLOGIST),
}

WRITES: Mapping[str, frozenset[str]] = {
    "sentinel": frozenset({"hits"}),  # spec: no write except opening or attaching incidents
    "pathologist": frozenset({"traces", "summary"}),
    "archivist": frozenset({"source", "intent"}),
    "coordinator": frozenset({"cards", "verdicts", "prior"}),
    "surgeon": frozenset({"diffs", "reasoning"}),
    "auditor": frozenset({"verdicts", "feedback"}),
    "governor": frozenset({"budget"}),
    "shipper": frozenset({"verdicts"}),
    "immunologist": frozenset(
        {"traces", "summary", "intent", "cards", "budget", "prior", "antibodies"}
    ),  # "prior": a secondary response's recipe, rendered on the host's binding
}


class InformationFlowViolation(PermissionError):
    """A role tried to read or write an artifact outside its row of the §5.7 matrix."""


def plain(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return plain(asdict(value))
    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [plain(v) for v in value]
        return sorted(items, key=str) if isinstance(value, (set, frozenset)) else items
    if isinstance(value, Path):
        return value.as_posix()
    return value


_MISSING = object()
_APPEND_LOCK = threading.Lock()


class CaseFile:
    def __init__(self, root: Path, incident: str) -> None:
        self.incident = incident
        self.root = Path(root) / incident
        self.root.mkdir(parents=True, exist_ok=True)

    # ---- raw storage (orchestrator only) ---------------------------------------------------

    def _path(self, kind: str, name: str) -> Path:
        if kind not in KINDS:
            raise KeyError(f"unknown case-file artifact kind {kind!r}")
        safe = "".join(ch if ch.isalnum() or ch in "-_.@" else "_" for ch in name)
        return self.root / kind / f"{safe}.json"

    def put(self, kind: str, name: str, data: Any, *, by: str) -> Path:
        column = KINDS.get(kind)
        if column is None or column not in WRITES.get(by, frozenset()):
            raise InformationFlowViolation(f"{by} may not write {kind}")
        path = self._path(kind, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(plain(data), indent=1, sort_keys=True, ensure_ascii=False)
                       + "\n", encoding="utf-8", newline="\n")  # fmt: skip
        os.replace(tmp, path)
        self._append("access.jsonl", {"op": "write", "role": by, "kind": kind, "name": name})
        return path

    def exists(self, kind: str, name: str) -> bool:
        return self._path(kind, name).is_file()

    def tool_read(self, kind: str, name: str, *, tool: str) -> Any:
        """Deterministic tooling (the bisect runner, the gate runner) loads the G0 cassette and
        repro record to *execute* them; nothing it reads enters a model's context. Limited to
        trace and summary artifacts, and logged."""
        if KINDS.get(kind) not in ("traces", "summary"):
            raise InformationFlowViolation(f"tool {tool} may not load {kind}")
        if not self.exists(kind, name):
            return None
        self._append("access.jsonl", {"op": "tool", "role": tool, "kind": kind, "name": name})
        return self._load(kind, name)

    def _load(self, kind: str, name: str) -> Any:
        return json.loads(self._path(kind, name).read_text(encoding="utf-8"))

    def names(self, kind: str) -> list[str]:
        folder = self.root / kind
        return sorted(p.stem for p in folder.glob("*.json")) if folder.is_dir() else []

    # ---- role views --------------------------------------------------------------------------

    def reader(self, role: str) -> CaseView:
        if role not in READS:
            raise KeyError(f"unknown role {role!r}")
        return CaseView(self, role)

    # ---- append-only logs ----------------------------------------------------------------------

    def _append(self, log: str, record: Mapping[str, Any]) -> None:
        line = json.dumps({"at": round(time.time(), 3), **plain(dict(record))},
                          sort_keys=True, ensure_ascii=False) + "\n"  # fmt: skip
        with _APPEND_LOCK, open(self.root / log, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)

    def _read_log(self, log: str) -> Iterator[dict[str, Any]]:
        path = self.root / log
        if not path.is_file():
            return
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                yield json.loads(line)

    def add_cost(self, role: str, usd: float, what: str) -> None:
        self._append("costs.jsonl", {"role": role, "usd": round(float(usd), 6), "what": what})

    def cost_lines(self) -> list[dict[str, Any]]:
        """Every complete spend line in order (the crew telemetry reports each on a trace once).
        A line another process is still writing is left for the next reader."""
        path = self.root / "costs.jsonl"
        if not path.is_file():
            return []
        text = path.read_text(encoding="utf-8")
        complete = text[: text.rfind("\n") + 1]
        return [json.loads(line) for line in complete.splitlines() if line.strip()]

    def spent(self) -> float:
        return round(sum(float(r["usd"]) for r in self._read_log("costs.jsonl")), 6)

    def costs_by_role(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        for record in self._read_log("costs.jsonl"):
            totals[record["role"]] = round(totals.get(record["role"], 0.0) + record["usd"], 6)
        return totals

    def log_call(self, record: Mapping[str, Any]) -> None:
        """One crew model call: role, template and prompt hashes, manifest, model, cost, reply."""
        self._append("calls.jsonl", record)

    def calls(self) -> list[dict[str, Any]]:
        return list(self._read_log("calls.jsonl"))

    def accesses(self) -> list[dict[str, Any]]:
        return list(self._read_log("access.jsonl"))


class CaseView:
    """One role's window on a case file: reads outside its matrix row raise and nothing leaks."""

    def __init__(self, case: CaseFile, role: str) -> None:
        self.case = case
        self.role = role

    def _allowed(self, kind: str) -> None:
        column = KINDS.get(kind)
        if column is None:
            raise KeyError(f"unknown case-file artifact kind {kind!r}")
        if column not in READS[self.role]:
            self.case._append("access.jsonl", {"op": "denied", "role": self.role, "kind": kind})
            raise InformationFlowViolation(f"{self.role} may not read {kind} ({column})")

    def get(self, kind: str, name: str, default: Any = _MISSING) -> Any:
        self._allowed(kind)
        if not self.case.exists(kind, name):
            if default is _MISSING:
                raise FileNotFoundError(f"{self.case.incident}: no {kind}/{name}")
            return default
        self.case._append("access.jsonl", {"op": "read", "role": self.role, "kind": kind,
                                           "name": name})  # fmt: skip
        return self.case._load(kind, name)

    def names(self, kind: str) -> list[str]:
        self._allowed(kind)
        return self.case.names(kind)
