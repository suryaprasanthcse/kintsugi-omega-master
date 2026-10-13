"""§7.4 / E.3: an incident closes with ``evidence/<INC>/bundle.json``; its digest is on the ledger.

The crew assembles the bundle from its case file, ledger and telemetry journal; the ``Incident``
workflow hands it the facts only it holds and writes ``bundle_sha256`` into the closing ledger
entry (E.2); audit C1 accepts the bundle on disk and rejects one edited afterwards.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kintsugi.control import contracts as c
from kintsugi.control.activities import CREW, StepContext
from kintsugi.control.workflows import Incident
from kintsugi.crew import bundler
from kintsugi.crew.casefile import CaseFile
from kintsugi.crew.context import CrewContext
from kintsugi.crew.factory import OmegaCrew
from kintsugi.proof import attest, audit, schemas
from kintsugi.proof.anchor import sign_anchor, tag_name
from kintsugi.proof.audit import Status
from kintsugi.proof.bundle import file_sha256, read_bundle, verify_attachments
from kintsugi.proof.ledger import Ledger, genesis_hash
from tests.helpers.control import FAST, Scenario, crew_for, temporal

INC = "INC-0042"
T0 = datetime(2026, 10, 12, 13, 20, tzinfo=UTC)


def _case(root: Path) -> CaseFile:
    """A case file as the roles leave it after a repair that shipped (aggregates only)."""
    case = CaseFile(root, INC)
    case.put("repro", "g0", {"ok": True, "cassette_sha": "sha256:" + "c" * 64, "failing": ["t1"],
                             "attempts": [{"evidence": {"replays_failing": 3}}]},
             by="pathologist")  # fmt: skip
    case.put("cassette", "g0", {"sha256": "sha256:" + "c" * 64, "interactions": 4},
             by="pathologist")  # fmt: skip
    case.put("intent_record", "record", {"suspects": ["run_reconcile"], "checkpoint": "ck_7f3a",
                                         "commit": "abc123", "quotes": ["retry keeps state"]},
             by="archivist")  # fmt: skip
    case.put("cause", "report", {"suspects": ["run_reconcile", "ReconcileSession.__init__"],
                                 "first_bad_checkpoint": "ck_7f3a"}, by="archivist")  # fmt: skip
    case.put("graph_context", "search", {"query": "duplicate journal entries"}, by="archivist")
    case.put("plan", "plan", {"budget_usd": 12.4, "plan_sha": "sha256:" + "p" * 64},
             by="governor")  # fmt: skip
    for hyp, passed in (("H1", True), ("H2", False)):
        case.put("card", hyp, {"round": 0, "host": "ledger", "title": hyp}, by="coordinator")
        case.put("outcome", hyp, {"commit": f"sha-{hyp}", "checkpoint": f"ck_{hyp}"},
                 by="surgeon")  # fmt: skip
        gates = {"G0": {"status": "pass"}, "G4": {"status": "pass" if passed else "fail"}}
        case.put("verdict", hyp, {"gates": gates, "passed": passed,
                                  "g4": {"b": 26, "c": 1, "p": 2.1e-7}}, by="auditor")  # fmt: skip
    case.put("reverify", "H1", {"G3": "rerun:7", "G4": "rerun:intersection{run_reconcile}",
                                "new_main": "f00"}, by="auditor")  # fmt: skip
    case.add_cost("surgeon", 6.5, "H1 session")
    case.add_cost("surgeon", 2.84, "H2 session")
    case.add_cost("archivist", 0.5, "intent record")
    return case


def _closing(state: str = "RESOLVED") -> dict[str, Any]:
    shipment = c.Shipment(INC, "H1", f"kintsugi/{INC}/H1", "https://github.com/o/r/pull/13",
                          True, True, "merge-H1")  # fmt: skip
    winner = c.SurgeonOutcome(INC, "H1", 1, f"kintsugi/{INC}/H1", "nonce", "sha-H1", "ck_H1")
    from dataclasses import asdict

    return {"incident": INC, "host": "ledger", "state": state, "reason": "canary accepted",
            "pass": 0, "transitions": ["OPEN->REPRODUCED", "CANARY->RESOLVED"],
            "traces": {"failing": ["trace-aa", "local:obs-1"], "after": ["trace-bb"]},
            "canary": {"accepted": True, "rejected": False, "n": 46, "llr": -2.3},
            "winner": asdict(winner), "shipment": asdict(shipment),
            "antibody": "AB-17@1.0"}  # fmt: skip


def _crew(tmp_path: Path) -> OmegaCrew:
    ctx = CrewContext(state=tmp_path / "crew", targets={})
    return OmegaCrew(ctx, runner=lambda: None,  # type: ignore[arg-type,return-value]
                     evidence_root=tmp_path / "evidence")  # fmt: skip


def _step() -> StepContext:
    return StepContext("emit_bundle", "emit_bundle:key", 1, lambda *details: None)


def test_the_bundle_is_written_in_the_e3_shape_with_its_attachments(tmp_path: Path) -> None:
    crew = _crew(tmp_path)
    _case(crew.ctx.state / "cases")
    crew.ledger.append({"type": "transition", "incident": INC})
    ref = crew.do_emit_bundle(_step(), _closing())
    path = tmp_path / "evidence" / INC / "bundle.json"
    assert Path(ref.path) == path
    bundle, on_disk = read_bundle(path.parent)
    assert ref.sha256 == on_disk == file_sha256(path)  # canonical bytes: file digest = digest
    data = json.loads(path.read_bytes())
    assert schemas.errors("E3", data) == []
    assert data["incident"] == INC
    assert data["host"] == "ledger"
    assert data["traces"] == {"failing": ["trace-aa"], "before": ["trace-aa"],
                              "after": ["trace-bb"]}  # fmt: skip
    assert data["local_traces"] == ["local:obs-1"]  # never offered to C8 as a neatlogs id
    assert data["cassette"] == {"g0": "3/3 fail", "path": "case/cassette/g0.json",
                                "sha256": "sha256:" + "c" * 64}  # fmt: skip
    assert data["cause"]["suspects"] == ["run_reconcile", "ReconcileSession.__init__"]
    assert data["cause"]["first_bad_checkpoint"] == "ck_7f3a"
    assert data["cause"]["intent_record"]["quotes"] == ["retry keeps state"]
    [rnd] = data["rounds"]
    assert [(h["id"], h["checkpoint"], h["passed"]) for h in rnd["hyps"]] == [
        ("H1", "ck_H1", True), ("H2", "ck_H2", False)]  # fmt: skip
    assert data["winner"] == {"hyp": "H1", "commit": "sha-H1", "checkpoint": "ck_H1",
                              "stats": {"b": 26, "c": 1, "p": 2.1e-7}}  # fmt: skip
    assert data["reverify"] == {"G3": "rerun:7", "G4": "rerun:intersection{run_reconcile}"}
    assert data["ship"]["pr"] == "https://github.com/o/r/pull/13"
    assert data["canary"]["decision"] == "accept"
    assert data["canary"]["n"] == 46
    assert data["cost"]["total_usd"] == data["cost"]["actual_usd"] == 9.84
    assert data["cost"]["by_role"] == {"archivist": 0.5, "surgeon": 9.34}
    assert "sessions" not in data["cost"]  # no telemetry journal: no session is claimed
    assert data["antibody"] == "AB-17@1.0"
    assert data["checkpoints"] == ["ck_H1", "ck_H2"]
    assert data["ledger"]["entries"] == 1
    assert bundle.incident == INC
    for attached in (
        "case/cassette/g0.json",
        "case/intent_record/record.json",
        "case/verdict/H1.json",
        "case/reverify/H1.json",
        "case/costs.jsonl",
    ):
        assert (path.parent / attached).is_file(), attached
    assert verify_attachments(path.parent) == []


def test_an_escalated_incident_still_closes_with_a_bundle(tmp_path: Path) -> None:
    crew = _crew(tmp_path)
    CaseFile(crew.ctx.state / "cases", INC).add_cost("archivist", 0.25, "attribution")
    closing = {**_closing("ESCALATED"), "winner": None, "shipment": None, "canary": None,
               "antibody": None, "reason": "rounds or budget exhausted"}  # fmt: skip
    ref = crew.do_emit_bundle(_step(), closing)
    data = json.loads(Path(ref.path).read_bytes())
    assert schemas.errors("E3", data) == []
    assert data["outcome"]["state"] == "ESCALATED"
    assert not {"winner", "ship", "canary", "antibody"} & set(data)  # never invented
    assert data["cost"]["actual_usd"] == 0.25


def test_audit_c1_checks_the_bundle_on_disk_against_the_ledger(tmp_path: Path) -> None:
    crew = _crew(tmp_path)
    _case(crew.ctx.state / "cases")
    ref = crew.do_emit_bundle(_step(), _closing())
    signer = attest.Ed25519Signer.generate()
    genesis = genesis_hash(T0.isoformat(), signer.public_key_raw())
    book = Ledger(genesis)
    book.append({"type": "incident", "incident": INC, "state": "RESOLVED",
                 "at": (T0 + timedelta(minutes=9)).isoformat(),
                 "bundle_sha256": ref.sha256})  # fmt: skip
    entries = list(book.entries)
    anchors = [sign_anchor(tag_name(T0 + timedelta(minutes=30)), 1, entries[0]["hash"], signer)]
    public = attest.load_public_key(signer.public_key_openssh())
    published = audit._bundles(tmp_path / "evidence")  # what `make judge` reads, per incident
    digests = {inc: b["digest"] for inc, b in published.items()}
    assert digests == {INC: ref.sha256}
    assert audit.c1_ledger(entries, genesis, anchors, public, digests).status is Status.PASS
    path = Path(ref.path)
    data = json.loads(path.read_bytes())
    data["cost"]["actual_usd"] = 0.01  # someone edits the cost after the fact
    path.write_text(json.dumps(data), encoding="utf-8")
    tampered = {inc: b["digest"] for inc, b in audit._bundles(tmp_path / "evidence").items()}
    result = audit.c1_ledger(entries, genesis, anchors, public, tampered)
    assert result.status is Status.FAIL
    assert "is not the one the ledger records" in result.problems[0]


def test_the_incident_workflow_records_the_bundle_digest_on_its_closing_entry(
    tmp_path: Path,
) -> None:
    ledger: list[dict[str, Any]] = []
    closings: list[dict[str, Any]] = []

    def emit(closing: dict[str, Any]) -> c.BundleRef:
        closings.append(closing)
        case = CaseFile(tmp_path / "cases", closing["incident"])
        path, digest = bundler.emit(tmp_path / "evidence", case, closing)
        return c.BundleRef(closing["incident"], path.as_posix(), digest)

    async def scenario() -> c.Outcome:
        crew = crew_for(Scenario())
        crew.answers["emit_bundle"] = emit
        crew.answers["ledger_append"] = lambda entry: ledger.append(entry) or "h"
        crew.answers["canary_sample"] = lambda shipment, batch: [False] * 40
        alert = c.Alert(INC, "ledger", (c.Hit("k1", "d", "trace-1", "s", receptor="R-007"),))
        async with temporal(crew) as (_env, client):
            handle = await client.start_workflow(Incident.run, args=[alert, FAST], id=INC,
                                                 task_queue=CREW)  # fmt: skip
            return await handle.result()

    outcome = asyncio.run(scenario())
    assert outcome.state == "RESOLVED"
    [closing] = closings
    assert closing["traces"]["failing"] == ["trace-1"]
    assert closing["winner"]["hypothesis"] == "H1"
    assert closing["shipment"]["pr_url"] == "https://example.invalid/pr/H1"
    assert closing["canary"]["accepted"] is True
    assert closing["antibody"] == f"AB-{INC}@1.0"
    [entry] = [e for e in ledger if e.get("type") == "incident"]
    path = tmp_path / "evidence" / INC / "bundle.json"
    assert entry["bundle_sha256"] == file_sha256(path)  # E.2: the digest of the file on disk
    assert entry["state"] == "RESOLVED"


def test_a_bundle_that_cannot_be_written_never_keeps_the_incident_open() -> None:
    ledger: list[dict[str, Any]] = []

    def broken(closing: dict[str, Any]) -> c.BundleRef:
        raise OSError("disk full")

    async def scenario() -> c.Outcome:
        crew = crew_for(Scenario(repro_ok=False))
        crew.answers["emit_bundle"] = broken
        crew.answers["ledger_append"] = lambda entry: ledger.append(entry) or "h"
        alert = c.Alert("INC-0043", "ledger", ())
        async with temporal(crew) as (_env, client):
            handle = await client.start_workflow(Incident.run, args=[alert, FAST], id="INC-0043",
                                                 task_queue=CREW)  # fmt: skip
            return await handle.result()

    outcome = asyncio.run(scenario())
    assert outcome.state == "PARKED"  # I3 holds
    [entry] = [e for e in ledger if e.get("type") == "incident"]
    assert "bundle_sha256" not in entry
    assert "disk full" in entry["bundle_error"]
