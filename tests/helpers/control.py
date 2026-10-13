"""A scripted crew and a time-skipping Temporal harness for the control-plane tests."""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from kintsugi.control import contracts as c
from kintsugi.control.activities import CREW, SENSES, SURGEON, OmegaActivities, ScriptedCrew
from kintsugi.control.workflows import WORKFLOWS

FAST = c.ControlConfig(canary_interval_s=60.0, canary_max_batches=6, anchor_interval_s=3600.0)


@dataclass
class Scenario:
    """The environment's answers. ``passing[pass_no]`` = the round whose H-odd card passes."""

    repro_ok: bool = True
    suspect: bool = True
    budget_usd: float = 12.40
    passing: dict[int, int | None] = field(default_factory=lambda: {0: 0})
    budget_exhausted_after: int | None = None
    g7_ok: bool = True
    merged: bool = True
    canary: dict[int, str] = field(default_factory=dict)  # pass → "accept" | "reject"
    revert_ok: bool = True
    antibody: bool = False
    bad_winner: bool = False  # the coordinator names a hypothesis that failed its gates
    gate: threading.Event | None = None  # surgeon waits on it (lets a test signal mid-round)
    route: str = "open"


def crew_for(s: Scenario) -> ScriptedCrew:
    def triage(alert: c.Alert) -> c.Signature:
        return c.Signature(alert.incident_id, alert.host, "phi-" + alert.host, ("trace-1",))

    def cards(sig, cause, plan, rnd, history, prior):
        base = sig.pass_no * 10 + rnd * 2
        return [c.Card(sig.incident_id, f"H{base + n}", rnd, f"brief {n} {prior}", "base")
                for n in (1, 2)]  # fmt: skip

    def surgeon(card: c.Card) -> c.SurgeonOutcome:
        if s.gate is not None:
            s.gate.wait(timeout=30)
        return c.SurgeonOutcome(card.incident_id, card.hypothesis, 1,
                                f"kintsugi/{card.incident_id}/{card.hypothesis}",
                                f"nonce-{card.hypothesis}", f"sha-{card.hypothesis}",
                                diff_sha=f"diff-{card.hypothesis}")  # fmt: skip

    def gates(out: c.SurgeonOutcome) -> c.GateVerdict:
        number = int(out.hypothesis[1:])
        pass_no, rest = divmod(number - 1, 10)
        rnd, slot = divmod(rest, 2)
        passed = s.passing.get(pass_no) == rnd and slot == 0
        return c.GateVerdict(out.hypothesis, passed, {"G0": True, "G3": passed}, out.diff_sha)

    def coordinate(sig, rnd, verdicts: list[c.GateVerdict]) -> c.Decision:
        if s.bad_winner:
            return c.Decision(rnd, winner=verdicts[-1].hypothesis)
        winner = next((v.hypothesis for v in verdicts if v.passed), None)
        return c.Decision(rnd, winner=winner)

    canary_pass = {"n": 0}

    def canary(shipment: c.Shipment, batch: int) -> list[bool]:
        if batch == 0:
            canary_pass["n"] += 1
        mode = s.canary.get(canary_pass["n"] - 1, "accept")
        return [True] * 12 if mode == "reject" else [False] * 40

    def route(hit: c.Hit, open_incidents: list[str]) -> c.RouteDecision:
        incident = f"INC-{hit.receptor}-{hit.host}"
        if s.route == "drop":
            return c.RouteDecision("drop")
        if incident in open_incidents:
            return c.RouteDecision("attach", incident)
        return c.RouteDecision("open", incident)

    answers: dict[str, Any] = {
        "route_hit": route,
        "record_transition": lambda record: None,
        "triage": triage,
        "tag_traces": lambda inc, ids, phase: len(ids),
        "match_antibody": lambda sig: c.AntibodyMatch("AB-7", "1.1", "recipe") if s.antibody
        else None,
        "reproduce": lambda sig: c.Repro(s.repro_ok, "cassette" if s.repro_ok else None,
                                         3 if s.repro_ok else 0, 3),
        "park": lambda sig, reason: None,
        "attribute": lambda sig, repro: c.Cause(("run_reconcile",) if s.suspect else ()),
        "plan_budget": lambda sig, cause: c.BudgetPlan(s.budget_usd, 2, 3, "Team",
                                                       f"plan-{sig.incident_id}-{sig.pass_no}"),
        "fixes_prior": lambda sig: None,
        "hypothesize": cards,
        "surgeon": surgeon,
        "audit_gates": gates,
        "coordinate": coordinate,
        "dispute_experiment": lambda inc, decision: decision,
        "rewind": lambda outs: None,
        "rebase_and_rerun": lambda outs, repo, new_main: None,
        "budget_exhausted": lambda sig, plan, rnd: s.budget_exhausted_after is not None
        and rnd >= s.budget_exhausted_after,
        "escalate": lambda inc, reason: None,
        "ship": lambda out: c.Shipment(out.incident_id, out.hypothesis, out.branch,
                                       f"https://example.invalid/pr/{out.hypothesis}", s.g7_ok,
                                       s.merged, f"merge-{out.hypothesis}" if s.merged else None),
        "revert": lambda shipment: s.revert_ok,
        "quarantine_related": lambda shipment: 1,
        "synthesize_antibody": lambda out, cause: c.AntibodyRef(f"AB-{out.incident_id}", "1.0"),
        "backfill_receptor": lambda ab: None,
        "fixes_resolve": lambda sig, shipment: None,
        "ledger_append": lambda entry: "ledger-hash",
        "canary_sample": canary,
        "canary_plan": lambda shipment: c.CanaryPlan("ledger", FAST.canary_p0, FAST.canary_ratio,
                                                     "scripted: the control plane's default"),
        "apply_antibody": lambda ab, host: c.VaccinationResult(ab.antibody_id, host, True),
        "mature_antibody": lambda ab, host: c.VaccinationResult(ab.antibody_id, host, True,
                                                                "pull request opened"),
        "promote_antibody": lambda ab, host: c.AntibodyRef(ab.antibody_id, ab.version, "ACTIVE"),
        "revert_vaccination": lambda shipment: c.Shipment(
            f"REV-{shipment.incident_id}", "revert", "kintsugi/revert", "pr-revert", True, True,
            f"revert-of-{shipment.merge_sha}"),
        "refine_antibody": lambda ab, shipment: c.AntibodyRef(ab.antibody_id, "1.1", "ACTIVE"),
        "motif_scan": lambda ab: ["ledger", "scout", "triage"],
        "onboard_step": lambda host, step: c.StepResult(step, step != "O5" or host != "broken"),
        "seal_sabotage": lambda spec: f"commitment-{spec}",
        "deliver_sabotage": lambda spec: None,
        "anchor_ledger": lambda hour: f"anchor-{hour}",
        "emit_bundle": lambda closing: c.BundleRef(closing["incident"],
                                                   f"evidence/{closing['incident']}/bundle.json",
                                                   f"sha256:bundle-{closing['incident']}"),
    }  # fmt: skip
    return ScriptedCrew(answers)


@asynccontextmanager
async def temporal(crew: ScriptedCrew) -> AsyncIterator[tuple[WorkflowEnvironment, Client]]:
    """A time-skipping server with workers on all three task queues."""
    env = await WorkflowEnvironment.start_time_skipping()
    acts = OmegaActivities(crew, heartbeat_interval_s=0.05)
    try:
        async with (
            Worker(env.client, task_queue=CREW, workflows=list(WORKFLOWS),
                   activities=acts.activities_for(CREW)),
            Worker(env.client, task_queue=SURGEON, activities=acts.activities_for(SURGEON)),
            Worker(env.client, task_queue=SENSES, activities=acts.activities_for(SENSES)),
        ):  # fmt: skip
            yield env, env.client
    finally:
        await env.shutdown()
