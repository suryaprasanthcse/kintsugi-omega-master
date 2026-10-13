"""The incident's proof bundle (spec §7.4, Appendix E.3), written when the incident closes.

``emit`` assembles ``bundle.json`` from what the crew recorded while it worked: the case file
(G0 cassette, cause report and Intent Record, cards, gate verdicts, decisions, L4 records, the
budget plan, costs by role), the incident's ledger entries, the session telemetry journal
(``kintsugi.crew.telemetry``) and the facts the ``Incident`` workflow closed with (outcome,
traces, canary, shipment, antibody). The case-file artifacts it summarises are copied beside it
as attachments, listed by digest in ``attachments.json``; the bundle's own digest goes into the
incident's ledger entry as ``bundle_sha256`` (E.2), which audit C1 checks against the file.

Nothing is invented: a section the incident never reached (no winner, no canary) is left out,
and the sealed sabotage behind an incident is not named here (diagnosis is blind; the reveal
after W links them, §6.10). Trace ids recorded only locally (``local:…``) are kept apart from
the neatlogs ids audit C8 looks up.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from kintsugi.crew.casefile import CaseFile
from kintsugi.proof.bundle import ProofBundle, write_bundle

# Case-file kinds copied into the bundle directory (aggregates only: the information-flow matrix
# already keeps holdout tasks and secrets out of the case file).
ATTACHED_KINDS = ("cassette", "repro", "intent_record", "cause", "graph_context", "card",
                  "outcome", "verdict", "reverify", "decision", "experiment", "plan")  # fmt: skip
LOCAL = "local:"


def _load(case: CaseFile, kind: str, name: str) -> dict[str, Any]:
    return dict(case._load(kind, name)) if case.exists(kind, name) else {}


def _real(ids: Sequence[str]) -> list[str]:
    return [t for t in ids if not str(t).startswith(LOCAL)]


def journal_lines(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _traces(closing: Mapping[str, Any], journal: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    failing = [str(t) for t in (closing.get("traces") or {}).get("failing") or []]
    after = [str(t) for t in (closing.get("traces") or {}).get("after") or []]
    traces: dict[str, list[str]] = {}
    if _real(failing):
        traces["failing"] = _real(failing)
        traces["before"] = _real(failing)  # tagged phase:before when the incident opened
    if _real(after):
        traces["after"] = _real(after)
    crew = sorted({str(j["trace_id"]) for j in journal if j.get("exported") and j.get("trace_id")})
    if crew:
        traces["crew"] = crew
    local = sorted({t for t in [*failing, *after] if t.startswith(LOCAL)})
    return {"traces": traces, "local_traces": local or None}


def _cassette(case: CaseFile) -> dict[str, Any] | None:
    repro = _load(case, "repro", "g0")
    if not repro:
        return None
    attempts = repro.get("attempts") or []
    replays = (attempts[-1].get("evidence") or {}).get("replays_failing") if attempts else None
    cassette: dict[str, Any] = {"g0": (f"{replays}/3 fail" if replays is not None
                                       else ("reproduced" if repro.get("ok") else
                                             "not reproduced"))}  # fmt: skip
    if case.exists("cassette", "g0"):
        cassette["path"] = "case/cassette/g0.json"
    if repro.get("cassette_sha"):
        cassette["sha256"] = str(repro["cassette_sha"])
    return cassette


def _cause(case: CaseFile) -> dict[str, Any] | None:
    report = _load(case, "cause", "report")
    record = _load(case, "intent_record", "record")
    if not report and not record:
        return None
    cause: dict[str, Any] = {"suspects": list(report.get("suspects") or record.get("suspects")
                                              or [])}  # fmt: skip
    outputs = [
        f"case/{kind}/{name}.json"
        for kind, name in (("graph_context", "search"), ("cause", "report"))
        if case.exists(kind, name)
    ]
    if outputs:
        cause["graph_outputs"] = outputs
    if record:
        quotes = record.get("quotes") or record.get("behavioral_contract") or []
        cause["intent_record"] = {"checkpoint": record.get("checkpoint"),
                                  "commit": record.get("commit"),
                                  "quotes": list(quotes) if isinstance(quotes, list)
                                  else [str(quotes)]}  # fmt: skip
    if report.get("first_bad_checkpoint"):
        cause["first_bad_checkpoint"] = str(report["first_bad_checkpoint"])
    return cause


def _rounds(case: CaseFile) -> list[dict[str, Any]]:
    by_round: dict[int, list[dict[str, Any]]] = {}
    for hyp in case.names("card"):
        card = _load(case, "card", hyp)
        verdict = _load(case, "verdict", hyp)
        outcome = _load(case, "outcome", hyp)
        entry: dict[str, Any] = {"id": hyp, "checkpoint": outcome.get("checkpoint"),
                                 "commit": outcome.get("commit"),
                                 "gates": {g: v.get("status") for g, v in
                                           (verdict.get("gates") or {}).items()},
                                 "passed": verdict.get("passed")}  # fmt: skip
        by_round.setdefault(int(card.get("round") or 0), []).append(entry)
    return [{"round": rnd, "hyps": hyps} for rnd, hyps in sorted(by_round.items())]


def _winner(case: CaseFile, closing: Mapping[str, Any]) -> dict[str, Any] | None:
    winner = closing.get("winner") or {}
    hyp = winner.get("hypothesis")
    if not hyp:
        return None
    verdict = _load(case, "verdict", str(hyp))
    stats = verdict.get("g4") or {}
    return {
        "hyp": hyp,
        "commit": winner.get("commit_sha"),
        "checkpoint": winner.get("checkpoint_id"),
        "stats": {k: stats[k] for k in sorted(stats) if not isinstance(stats[k], (dict, list))},
    }


def _dispute(case: CaseFile) -> dict[str, Any] | None:
    names = case.names("experiment")
    if not names:
        return None
    return {name: _load(case, "experiment", name) for name in names}


def _reverify(case: CaseFile, closing: Mapping[str, Any]) -> dict[str, str] | None:
    hyp = (closing.get("winner") or {}).get("hypothesis")
    names = case.names("reverify")
    pick = str(hyp) if hyp and str(hyp) in names else (names[-1] if names else None)
    if pick is None:
        return None
    record = _load(case, "reverify", pick)
    return {k: str(v) for k, v in sorted(record.items()) if k.startswith("G")}


def _ship(case: CaseFile, closing: Mapping[str, Any]) -> dict[str, Any] | None:
    shipment = closing.get("shipment")
    if not shipment:
        return None
    decision = _load(case, "decision", f"ship-{shipment.get('hypothesis')}")
    g7 = decision.get("g7")
    return {"pr": shipment.get("pr_url"), "issue": decision.get("issue"),
            "branch": shipment.get("branch"), "merged": shipment.get("merged"),
            "merge_sha": shipment.get("merge_sha"), "g7_ok": shipment.get("g7_ok"),
            "g7": g7 if isinstance(g7, str) else (g7 or {}).get("summary")}  # fmt: skip


def _cost(case: CaseFile, journal: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """``actual_usd`` and ``sessions`` are what audit C10 reconciles (neatlogs session costs)."""
    spent = case.spent()
    cost: dict[str, Any] = {"total_usd": spent, "actual_usd": spent,
                            "by_role": case.costs_by_role()}  # fmt: skip
    if journal:  # the crew's traces carried this incident's spend in its neatlogs session
        cost["sessions"] = sorted({str(j["session"]) for j in journal if j.get("session")})
        cost["telemetry_usd"] = round(sum(float(j.get("cost_usd") or 0.0) for j in journal), 6)
    return cost


def assemble(
    case: CaseFile,
    closing: Mapping[str, Any],
    *,
    ledger_entries: Sequence[Mapping[str, Any]] = (),
    journal: Sequence[Mapping[str, Any]] = (),
    canary: Mapping[str, Any] | None = None,
) -> ProofBundle:
    incident = str(closing["incident"])
    traces = _traces(closing, journal)
    mine = [e for e in ledger_entries if e.get("incident") == incident]
    rounds = _rounds(case)
    # Beyond E.3 (the model keeps unknown sections): the outcome, the incident's ledger span,
    # every attempt's checkpoint, the pinned plan and any local-only trace ids.
    extra: dict[str, Any] = {
        "outcome": {"state": closing.get("state"), "reason": closing.get("reason"),
                    "pass": closing.get("pass", 0),
                    "transitions": list(closing.get("transitions") or [])},
        "ledger": {"entries": len(mine), "first_seq": mine[0].get("seq") if mine else None,
                   "last_hash": mine[-1].get("hash") if mine else None},
        "checkpoints": sorted({str(r["checkpoint"]) for rnd in rounds
                               for r in rnd["hyps"] if r.get("checkpoint")}),
    }  # fmt: skip
    if traces["local_traces"]:
        extra["local_traces"] = traces["local_traces"]
    plan = _load(case, "plan", "plan")
    if plan.get("plan_sha"):
        extra["plan_sha256"] = str(plan["plan_sha"])
    return ProofBundle(
        incident=incident,
        host=str(closing.get("host") or "unknown"),
        traces=traces["traces"],
        cassette=_cassette(case),
        cause=_cause(case),
        rounds=rounds,
        dispute=_dispute(case),
        winner=_winner(case, closing),
        reverify=_reverify(case, closing),
        ship=_ship(case, closing),
        canary=dict(canary) if canary else None,
        cost=_cost(case, journal),
        antibody=closing.get("antibody"),
        **extra,
    )


def attach(case: CaseFile, directory: Path, journal_path: Path | None) -> None:
    """Copy the summarised case-file artifacts (and the telemetry journal) beside the bundle."""
    target = directory / "case"
    if target.exists():
        shutil.rmtree(target)
    for kind in ATTACHED_KINDS:
        source = case.root / kind
        if source.is_dir():
            shutil.copytree(source, target / kind)
    costs = case.root / "costs.jsonl"
    if costs.is_file():
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(costs, target / "costs.jsonl")
    if journal_path is not None and journal_path.is_file():
        (directory / "telemetry").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(journal_path, directory / "telemetry" / "sessions.jsonl")


def emit(
    root: Path,
    case: CaseFile,
    closing: Mapping[str, Any],
    *,
    ledger_entries: Sequence[Mapping[str, Any]] = (),
    journal_path: Path | None = None,
    canary: Mapping[str, Any] | None = None,
) -> tuple[Path, str]:
    """Write ``<root>/<INC>/bundle.json`` and its attachments; return (path, sha256)."""
    journal = journal_lines(journal_path)
    bundle = assemble(case, closing, ledger_entries=ledger_entries, journal=journal,
                      canary=canary)  # fmt: skip
    directory = Path(root) / bundle.incident
    directory.mkdir(parents=True, exist_ok=True)
    attach(case, directory, journal_path)
    digest = write_bundle(directory, bundle)
    return directory / "bundle.json", digest
