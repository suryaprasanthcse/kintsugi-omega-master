"""Workflows on a time-skipping Temporal test server (spec §5.3, §5.13; plan Stage 5).

Every Incident ends RESOLVED, ESCALATED or PARKED and its transition log folds legally (I3);
nothing ships without a G0–G6 winner (I1); a canary rejection re-enters within its bound; a
drift signal mid-round triggers the L4 rebase; the supervisor's signals, queries, concurrency
limit and continue_as_new hold; and every recorded history replays deterministically.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import timedelta
from typing import Any

import pytest
from temporalio import workflow
from temporalio.client import Client, WorkflowFailureError, WorkflowHistory
from temporalio.service import RPCError
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner

from kintsugi.control import contracts as c
from kintsugi.control.activities import CREW
from kintsugi.control.states import IncidentState, fold
from kintsugi.control.workflows import (
    WORKFLOWS,
    AuditAnchor,
    Campaign,
    Canary,
    CrossHostMaturation,
    FleetSupervisor,
    Incident,
    Onboarding,
    SabotageCampaign,
    VaccinationWave,
)
from kintsugi.stats.sprt import SPRT
from tests.helpers.control import FAST, Scenario, crew_for, temporal

ALERT = c.Alert("INC-0042", "ledger", (c.Hit("k1", "d", "t", "s", receptor="R-007"),))


def run(coro: Any) -> Any:
    return asyncio.run(coro)


async def incident(scenario: Scenario, config: c.ControlConfig = FAST, alert: c.Alert = ALERT):
    crew = crew_for(scenario)
    async with temporal(crew) as (_env, client):
        handle = await client.start_workflow(
            Incident.run, args=[alert, config], id=alert.incident_id, task_queue=CREW
        )
        outcome: c.Outcome = await handle.result()
        history = await handle.fetch_history()
    return outcome, crew, history


def legal(outcome: c.Outcome) -> IncidentState:
    events = [(IncidentState(t.source), IncidentState(t.target), t.outcome)
              for t in outcome.transitions]  # fmt: skip
    return fold(events)


def test_a_repaired_incident_resolves_through_every_stage() -> None:
    outcome, crew, _ = run(incident(Scenario()))
    assert outcome.state == "RESOLVED"
    assert legal(outcome) is IncidentState.RESOLVED
    assert [t.target for t in outcome.transitions] == ["REPRODUCED", "ATTRIBUTED", "REPAIRING",
                                                      "SHIPPED", "CANARY", "RESOLVED"]  # fmt: skip
    names = [name for name, _ in crew.calls]
    assert names.index("ship") > max(i for i, n in enumerate(names) if n == "audit_gates")  # I1
    assert crew.count("ship") == 1
    assert crew.count("surgeon") == 2  # K = 2 parallel Surgeons
    assert outcome.shipment is not None
    assert outcome.shipment.hypothesis == "H1"


@pytest.mark.parametrize(
    ("scenario", "state", "last"),
    [
        (Scenario(repro_ok=False), "PARKED", "g0_not_reproducible_after_3_attempts"),
        (Scenario(suspect=False), "ESCALATED", "attribution_found_no_suspect"),
        (Scenario(budget_usd=0.0), "ESCALATED", "budget_zero"),
        (Scenario(passing={}), "ESCALATED", "rounds_or_budget_exhausted_or_cheaper_treatment"),
        (Scenario(passing={0: 2}, budget_exhausted_after=0), "ESCALATED",
         "rounds_or_budget_exhausted_or_cheaper_treatment"),
        (Scenario(g7_ok=False), "ESCALATED", "rounds_or_budget_exhausted_or_cheaper_treatment"),
        (Scenario(merged=False), "ESCALATED", "merge_blocked"),
        (Scenario(canary={0: "reject"}, revert_ok=False), "ESCALATED",
         "sprt_rejects_revert_failed_or_reentry_limit"),
        (Scenario(passing={0: 1}), "RESOLVED", "sprt_accepts_h0_or_pr_only_canary_skipped"),
        (Scenario(antibody=True), "RESOLVED", "sprt_accepts_h0_or_pr_only_canary_skipped"),
    ],
)  # fmt: skip
def test_every_path_ends_terminal_with_a_legal_log(
    scenario: Scenario, state: str, last: str
) -> None:
    outcome, crew, _ = run(incident(scenario))
    assert outcome.state == state  # I3
    assert legal(outcome) is IncidentState(state)
    assert outcome.transitions[-1].outcome == last
    assert [t.seq for t in outcome.transitions] == list(range(len(outcome.transitions)))
    if state != "RESOLVED" or scenario.antibody:
        assert crew.count("ship") <= 1


def test_rounds_are_bounded_and_failed_rounds_are_rewound() -> None:
    outcome, crew, _ = run(incident(Scenario(passing={})))
    assert crew.count("hypothesize") == FAST.max_rounds
    assert crew.count("rewind") == FAST.max_rounds
    assert crew.count("ship") == 0
    repairing = [t for t in outcome.transitions if t.target == "REPAIRING"]
    assert len(repairing) == FAST.max_rounds  # budget_positive + two round retries


def test_a_rejected_canary_reverts_quarantines_and_re_enters_with_new_hypotheses() -> None:
    outcome, crew, _ = run(incident(Scenario(canary={0: "reject"}, passing={0: 0, 1: 0})))
    assert outcome.state == "RESOLVED"
    assert legal(outcome) is IncidentState.RESOLVED
    assert "sprt_rejects_reverted_and_quarantined" in [t.outcome for t in outcome.transitions]
    assert crew.count("revert") == 1
    assert crew.count("quarantine_related") == 1
    assert crew.count("triage") == 2  # re-entry with the canary evidence
    assert outcome.shipment is not None
    assert outcome.shipment.hypothesis == "H11"  # pass 1's first card: ids never repeat


def test_canary_re_entries_are_bounded() -> None:
    always = Scenario(canary={0: "reject", 1: "reject", 2: "reject"}, passing={0: 0, 1: 0, 2: 0})
    outcome, crew, _ = run(incident(always))
    assert outcome.state == "ESCALATED"
    assert outcome.transitions[-1].outcome == "sprt_rejects_revert_failed_or_reentry_limit"
    assert crew.count("revert") == FAST.max_canary_reentries + 1


def test_a_coordinator_cannot_ship_a_hypothesis_that_failed_its_gates() -> None:
    with pytest.raises(WorkflowFailureError):
        run(incident(Scenario(bad_winner=True)))


def test_a_drift_signal_mid_round_rebases_and_reruns_the_impacted_gates() -> None:
    async def scenario() -> tuple[c.Outcome, Any]:
        gate = threading.Event()
        crew = crew_for(Scenario(passing={0: 1}, gate=gate))
        async with temporal(crew) as (_env, client):
            handle = await client.start_workflow(Incident.run, args=[ALERT, FAST],
                                                 id="INC-drift", task_queue=CREW)  # fmt: skip
            while crew.count("surgeon") == 0:
                await asyncio.sleep(0.05)
            await handle.signal(Incident.drift_signal, args=["hosts/ledger", "main-sha-2"])
            gate.set()
            return await handle.result(), crew

    outcome, crew = run(scenario())
    assert outcome.state == "RESOLVED"
    assert crew.count("rebase_and_rerun") == 1  # L4 signal path


def test_the_canary_runs_the_sprt_with_durable_timers() -> None:
    async def scenario() -> tuple[c.CanaryResult, c.CanaryResult, timedelta]:
        crew = crew_for(Scenario(canary={0: "accept", 1: "reject"}))
        ship = c.Shipment("INC-1", "H1", "b", "pr", True, True, "m")
        async with temporal(crew) as (env, client):
            started = await env.get_current_time()
            accepted = await client.execute_workflow(Canary.run, args=[ship, FAST], id="can-a",
                                                     task_queue=CREW)  # fmt: skip
            skipped = await env.get_current_time() - started
            # A different merge: the same one would replay the recorded batches (idempotency).
            other = c.Shipment("INC-1", "H3", "b3", "pr3", True, True, "m3")
            rejected = await client.execute_workflow(Canary.run, args=[other, FAST], id="can-r",
                                                     task_queue=CREW)  # fmt: skip
        return accepted, rejected, skipped

    accepted, rejected, skipped = run(scenario())
    sprt = SPRT.regression(FAST.canary_p0, ratio=FAST.canary_ratio)
    assert accepted.accepted
    assert accepted.samples == sprt.runs_to_accept_without_failures()
    assert rejected.rejected
    assert rejected.samples == 5  # ln(2) per failure crosses ln(0.9/0.05)
    assert skipped >= timedelta(seconds=FAST.canary_interval_s)  # a timer, skipped not slept


def test_the_canary_tests_against_the_hosts_own_baseline() -> None:
    # Stage 7 (canary-host-p0): H0 is the shipment's host's baseline, not a fleet constant.
    async def scenario() -> c.CanaryResult:
        crew = crew_for(Scenario())
        crew.answers["canary_plan"] = lambda shipment: c.CanaryPlan("scout", 0.031, 2.0, "test")
        ship = c.Shipment("VAC-AB-1-scout", "AB-1@1.0", "b", "pr", True, True, "m")
        async with temporal(crew) as (_env, client):
            return await client.execute_workflow(Canary.run, args=[ship, FAST], id="can-p0",
                                                 task_queue=CREW)  # fmt: skip

    result = run(scenario())
    assert result.accepted
    assert result.samples == SPRT.regression(0.031).runs_to_accept_without_failures()
    assert result.samples != SPRT.regression(FAST.canary_p0).runs_to_accept_without_failures()


# --------------------------------------------------------------------------- supervisor


def hit(n: int, host: str = "ledger", receptor: str = "R-007") -> c.Hit:
    return c.Hit(f"key-{n}", "det", f"t{n}", "s", receptor=receptor, host=host)


async def wait_for(predicate: Any, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not await predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("condition not reached")
        await asyncio.sleep(0.05)


def test_the_supervisor_routes_dedupes_limits_and_answers_queries() -> None:
    async def scenario() -> dict[str, Any]:
        gate = threading.Event()
        crew = crew_for(Scenario(gate=gate))
        config = c.ControlConfig(max_open_per_host=1, canary_interval_s=1.0)
        async with temporal(crew) as (_env, client):
            sup = await client.start_workflow(FleetSupervisor.run, args=[config, None],
                                              id=config.supervisor_id, task_queue=CREW)  # fmt: skip
            await sup.signal(FleetSupervisor.budget_update, args=["ledger", 71.70])
            await sup.signal(FleetSupervisor.hit, hit(1))  # opens INC-R-007-ledger
            await sup.signal(FleetSupervisor.hit, hit(1))  # duplicate key: ignored
            await sup.signal(FleetSupervisor.hit, hit(2))  # attaches to the open incident
            await sup.signal(FleetSupervisor.hit, hit(3, receptor="R-001"))  # same host: waits
            await sup.signal(FleetSupervisor.hit, hit(4, host="scout"))  # another host: opens
            await sup.signal(FleetSupervisor.pause_antibody, "AB-3")

            async def two_open() -> bool:
                return len(await sup.query(FleetSupervisor.open_incidents)) == 2

            await wait_for(two_open)
            mid = await sup.query(FleetSupervisor.status)
            open_now = await sup.query(FleetSupervisor.open_incidents)
            budget = await sup.query(FleetSupervisor.budget, "ledger")
            gate.set()  # let the Surgeons finish: incidents close and the waiting one starts

            async def all_closed() -> bool:
                status = await sup.query(FleetSupervisor.status)
                return len(status["closed"]) == 3 and status["open"] == 0

            await wait_for(all_closed, timeout=60)
            final = await sup.query(FleetSupervisor.status)
            await sup.terminate("test over")
        return {"mid": mid, "open": open_now, "budget": budget, "final": final,
                "routes": crew.count("route_hit")}  # fmt: skip

    out = run(scenario())
    assert out["open"] == {"INC-R-007-ledger": "ledger", "INC-R-007-scout": "scout"}
    assert out["mid"]["waiting"] == 1  # the second ledger incident waits for the first
    assert out["mid"]["paused_antibodies"] == ["AB-3"]
    assert out["budget"] == 71.70
    assert out["routes"] == 4  # the duplicate hit never reached the Sentinel
    assert set(out["final"]["closed"]) == {"INC-R-007-ledger", "INC-R-007-scout",
                                           "INC-R-001-ledger"}  # fmt: skip
    assert set(out["final"]["closed"].values()) == {"RESOLVED"}


def test_operator_pause_holds_hits_until_resume() -> None:
    async def scenario() -> tuple[int, int]:
        crew = crew_for(Scenario(route="drop"))
        async with temporal(crew) as (_env, client):
            sup = await client.start_workflow(FleetSupervisor.run, args=[FAST, None],
                                              id="sup-pause", task_queue=CREW)  # fmt: skip
            await sup.signal(FleetSupervisor.operator_pause, "maintenance")
            for n in range(3):
                await sup.signal(FleetSupervisor.hit, hit(n))
            await asyncio.sleep(0.5)
            held = (await sup.query(FleetSupervisor.status))["pending"]
            await sup.signal(FleetSupervisor.operator_resume)

            async def drained() -> bool:
                return (await sup.query(FleetSupervisor.status))["pending"] == 0

            await wait_for(drained)
            await sup.terminate("test over")
        return held, crew.count("route_hit")

    held, routed = run(scenario())
    assert held == 3
    assert routed == 3


def test_the_supervisor_continues_as_new_and_keeps_its_state() -> None:
    async def scenario() -> tuple[dict[str, Any], int, WorkflowHistory]:
        crew = crew_for(Scenario(route="drop"))
        config = c.ControlConfig(continue_as_new_every=3)
        async with temporal(crew) as (_env, client):
            sup = await client.start_workflow(FleetSupervisor.run, args=[config, None],
                                              id="sup-can", task_queue=CREW)  # fmt: skip
            await sup.signal(FleetSupervisor.budget_update, args=["triage", 6.0])
            for n in range(7):
                await sup.signal(FleetSupervisor.hit, hit(n))

            async def seven() -> bool:
                try:  # the run continues as new every 3 events: a query may land mid-switch
                    return (await sup.query(FleetSupervisor.status))["events"] == 7
                except RPCError:  # "query deadline exceeded" while the next run starts
                    return False

            await wait_for(seven)
            await sup.signal(FleetSupervisor.hit, hit(2))  # seen before the continue_as_new
            latest = client.get_workflow_handle("sup-can")  # follows the continue_as_new chain

            async def eight() -> bool:  # a condition, not a fixed sleep: robust under load
                try:
                    return (await latest.query(FleetSupervisor.status))["events"] == 8
                except RPCError:  # mid continue_as_new the new run may not answer yet
                    return False

            await wait_for(eight)
            answers: dict[str, Any] = {}

            async def settled() -> bool:  # the same tolerance for the final reads
                try:
                    answers["status"] = await latest.query(FleetSupervisor.status)
                    answers["budget"] = await latest.query(FleetSupervisor.budget, "triage")
                except RPCError:
                    return False
                return True

            await wait_for(settled)
            status, budget = answers["status"], answers["budget"]
            first_run = client.get_workflow_handle("sup-can", run_id=sup.result_run_id)
            history = await first_run.fetch_history()
            await latest.terminate("test over")
        assert budget == 6.0
        return status, crew.count("route_hit"), history

    status, routed, history = run(scenario())
    assert status["runs"] == 3  # 7 events at 3 per run
    assert status["events"] == 8
    assert routed == 7  # the repeated key was remembered across runs
    assert history.events


# --------------------------------------------------------------------------- the others


def test_vaccination_pauses_and_resumes_by_signal() -> None:
    async def scenario() -> tuple[list[str], list[c.VaccinationResult]]:
        crew = crew_for(Scenario())
        ab = c.AntibodyRef("AB-1", "1.0")
        async with temporal(crew) as (_env, client):
            wave = await client.start_workflow(VaccinationWave.run, args=[ab, FAST], id="vac",
                                               task_queue=CREW)  # fmt: skip
            await wave.signal(VaccinationWave.pause)
            await asyncio.sleep(0.5)
            paused_progress = await wave.query(VaccinationWave.progress)
            await wave.signal(VaccinationWave.resume)
            results = await wave.result()
        return paused_progress, results

    paused, results = run(scenario())
    assert len(paused) < 3
    assert [r.host for r in results] == ["ledger", "scout", "triage"]


def test_maturation_promotes_on_the_first_second_host_that_repairs() -> None:
    async def scenario() -> tuple[c.StepResult, Any]:
        crew = crew_for(Scenario())
        crew.answers["motif_scan"] = lambda ab: ["ledger", "scout", "crm-practice"]
        crew.answers["mature_antibody"] = lambda ab, host: c.VaccinationResult(
            ab.antibody_id, host, host == "crm-practice",
            "pull request opened" if host == "crm-practice" else "gates: G2")  # fmt: skip
        ab = c.AntibodyRef("AB-1", "1.0", "CANDIDATE")
        async with temporal(crew) as (_env, client):
            result = await client.execute_workflow(
                CrossHostMaturation.run, args=[ab, "ledger", FAST], id="mat", task_queue=CREW
            )
        return result, crew

    result, crew = run(scenario())
    assert result.ok
    assert result.detail["host"] == "crm-practice"
    assert result.detail["state"] == "ACTIVE"
    assert crew.count("mature_antibody") == 2  # scout failed its gates first
    assert crew.count("promote_antibody") == 1


def _l3_crew() -> Any:
    crew = crew_for(Scenario())
    scans = {"1.0": ["scout", "oss-1"], "1.1": ["oss-1"]}
    crew.answers["motif_scan"] = lambda ab: scans[ab.version]

    def apply(ab: c.AntibodyRef, host: str) -> c.VaccinationResult:
        shipment = c.Shipment(
            f"VAC-{ab.antibody_id}-{host}",
            f"{ab.antibody_id}@{ab.version}",
            f"kintsugi/VAC-{host}",
            "pr",
            True,
            True,
            f"m-{host}-{ab.version}",
        )
        return c.VaccinationResult(ab.antibody_id, host, True, "merged", shipment)

    def canary(shipment: c.Shipment, batch: int) -> list[bool]:
        wrong = shipment.merge_sha == "m-scout-1.0"  # the autoimmune vaccination
        return [True, False] * 6 if wrong else [False] * 40

    crew.answers["apply_antibody"] = apply
    crew.answers["canary_sample"] = canary
    return crew


def test_an_autoimmune_vaccination_is_reverted_quarantined_refined_and_the_wave_resumes() -> None:
    async def scenario() -> tuple[list[c.VaccinationResult], list[dict[str, Any]], Any]:
        crew = _l3_crew()
        ab = c.AntibodyRef("AB-1", "1.0", "ACTIVE")
        async with temporal(crew) as (_env, client):
            wave = await client.start_workflow(VaccinationWave.run, args=[ab, FAST],
                                               id="vaccinate-AB-1", task_queue=CREW)  # fmt: skip
            results = await wave.result()
            episodes = await wave.query(VaccinationWave.autoimmunity)
        return results, episodes, crew

    results, episodes, crew = run(scenario())
    assert [(r.host, r.shipment.hypothesis if r.shipment else None) for r in results] == [
        ("scout", "AB-1@1.0"), ("oss-1", "AB-1@1.1")]  # fmt: skip
    [episode] = episodes
    assert episode["host"] == "scout"
    assert episode["revert"] == "pr-revert"
    assert episode["reverse_canary"]["accepted"]
    assert episode["refined"] == "AB-1@1.1"
    assert episode["quarantined"] == 1
    for name in ("revert_vaccination", "quarantine_related", "refine_antibody"):
        assert crew.count(name) == 1
    names = [n for n, _ in crew.calls]
    assert names.index("revert_vaccination") < names.index("quarantine_related")
    assert names.index("quarantine_related") < names.index("refine_antibody")


def test_a_paused_antibody_reaches_its_running_wave_through_the_supervisor() -> None:
    async def scenario() -> tuple[list[str], list[str], dict[str, Any]]:
        gate = threading.Event()
        crew = crew_for(Scenario())

        def slow(ab: c.AntibodyRef, host: str) -> c.VaccinationResult:
            gate.wait(timeout=30)
            return c.VaccinationResult(ab.antibody_id, host, True)

        crew.answers["apply_antibody"] = slow
        config = c.ControlConfig(supervisor_id="sup-pause", canary_interval_s=1.0)
        ab = c.AntibodyRef("AB-5", "1.0", "ACTIVE")
        async with temporal(crew) as (_env, client):
            sup = await client.start_workflow(FleetSupervisor.run, args=[config, None],
                                              id=config.supervisor_id, task_queue=CREW)  # fmt: skip
            wave = await client.start_workflow(VaccinationWave.run, args=[ab, config],
                                               id="vaccinate-AB-5", task_queue=CREW)  # fmt: skip
            await sup.signal(FleetSupervisor.pause_antibody, "AB-5")

            async def forwarded() -> bool:
                return "AB-5" in (await sup.query(FleetSupervisor.status))["paused_antibodies"]

            await wait_for(forwarded)
            await asyncio.sleep(0.5)  # the supervisor forwards the pause to the wave
            gate.set()
            await asyncio.sleep(1.0)
            held = await wave.query(VaccinationWave.progress)
            await sup.signal(FleetSupervisor.resume_antibody, "AB-5")

            async def finished() -> bool:  # poll: awaiting a result would let the time-skipping
                return len(await wave.query(VaccinationWave.progress)) == 3  # server time out sup

            await wait_for(finished)
            done = await wave.query(VaccinationWave.progress)
            status = await sup.query(FleetSupervisor.status)
            await sup.terminate("test over")
        return held, done, status

    held, done, status = run(scenario())
    assert len(held) <= 1  # held at most after the application already running
    assert done == ["ledger", "scout", "triage"]
    assert status["paused_antibodies"] == []


def test_onboarding_runs_o1_to_o7_and_stops_at_a_failure() -> None:
    async def scenario() -> tuple[list[c.StepResult], list[c.StepResult]]:
        crew = crew_for(Scenario())
        async with temporal(crew) as (_env, client):
            good = await client.execute_workflow(Onboarding.run, args=["oss-1", FAST], id="on-1",
                                                 task_queue=CREW)  # fmt: skip
            bad = await client.execute_workflow(Onboarding.run, args=["broken", FAST], id="on-2",
                                                task_queue=CREW)  # fmt: skip
        return good, bad

    good, bad = run(scenario())
    assert [s.name for s in good] == ["O1", "O2", "O3", "O4", "O5", "O6", "O7"]
    assert [s.name for s in bad][-1] == "O5"
    assert not bad[-1].ok


def test_sabotage_is_sealed_before_any_delivery() -> None:
    async def scenario() -> tuple[list[str], list[str], dict[str, str]]:
        crew = crew_for(Scenario())
        campaign = Campaign(specs=[("S-17", 3600.0), ("S-18", 7200.0)])
        async with temporal(crew) as (_env, client):
            handle = await client.start_workflow(SabotageCampaign.run, args=[campaign, FAST],
                                                 id="sab", task_queue=CREW)  # fmt: skip
            await handle.signal(SabotageCampaign.request, args=["ARENA-1", 60.0])
            await handle.signal(SabotageCampaign.close)
            delivered = await handle.result()
            commitments = await handle.query(SabotageCampaign.commitments)
        return [n for n, _ in crew.calls], delivered, commitments

    names, delivered, commitments = run(scenario())
    assert names[:2] == ["seal_sabotage", "seal_sabotage"]  # both sealed before the first fault
    assert sorted(delivered) == ["ARENA-1", "S-17", "S-18"]
    assert set(commitments) == {"S-17", "S-18", "ARENA-1"}
    first_delivery = names.index("deliver_sabotage")
    assert names[:first_delivery].count("seal_sabotage") >= 2


def test_a_failed_delivery_never_stops_the_sealed_schedule() -> None:
    """An Arena request that cannot be delivered (refused, or out of retries) is recorded and
    the campaign delivers the sealed faults behind it (campaign-delivery-guard)."""
    from kintsugi.control.activities import DeliveryRefused

    ledger: list[dict[str, Any]] = []

    def deliver(spec_id: str) -> None:
        if spec_id == "S-A0001":
            raise DeliveryRefused("S-A0001: no delivery port for env.rate_limit")
        if spec_id == "S-A0002":
            raise RuntimeError("transient failure on every attempt")

    async def scenario() -> tuple[list[str], dict[str, str]]:
        crew = crew_for(Scenario())
        crew.answers["deliver_sabotage"] = deliver
        crew.answers["ledger_append"] = lambda entry: ledger.append(entry) or "h"
        campaign = Campaign(specs=[("S-17", 3600.0), ("S-18", 7200.0)])
        async with temporal(crew) as (_env, client):
            handle = await client.start_workflow(SabotageCampaign.run, args=[campaign, FAST],
                                                 id="sab-guard", task_queue=CREW)  # fmt: skip
            await handle.signal(SabotageCampaign.request, args=["S-A0001", 60.0])
            await handle.signal(SabotageCampaign.request, args=["S-A0002", 120.0])
            await handle.signal(SabotageCampaign.close)
            delivered = await handle.result()
            failures = await handle.query(SabotageCampaign.failures)
        return delivered, failures

    delivered, failures = run(scenario())
    assert delivered == ["S-17", "S-18"]  # the sealed schedule survives both failures
    assert set(failures) == {"S-A0001", "S-A0002"}
    assert "DeliveryRefused" in failures["S-A0001"]
    assert [(e["event"], e["sabotage"]) for e in ledger] == [
        ("delivery-failed", "S-A0001"), ("delivery-failed", "S-A0002")]  # fmt: skip


def test_the_audit_anchor_ticks_hourly_and_continues_as_new() -> None:
    async def scenario() -> list[str]:
        crew = crew_for(Scenario())
        async with temporal(crew) as (env, client):
            handle = await client.start_workflow(AuditAnchor.run, args=[0, FAST, 2], id="anchor",
                                                 task_queue=CREW)  # fmt: skip
            await env.sleep(timedelta(hours=5))
            await client.get_workflow_handle("anchor").terminate("test over")
            del handle
        return [name for name, _ in crew.calls if name == "anchor_ledger"]

    anchors = run(scenario())
    assert len(anchors) >= 4  # two runs of two hourly anchors: continue_as_new happened


# --------------------------------------------------------------------------- replay


def test_recorded_histories_replay_deterministically() -> None:
    histories = [run(incident(scenario))[2] for scenario in (
        Scenario(), Scenario(canary={0: "reject"}, passing={0: 0, 1: 0}), Scenario(passing={}),
    )]  # fmt: skip
    replayer = Replayer(workflows=list(WORKFLOWS))
    for history in histories:
        run(replayer.replay_workflow(history))


@workflow.defn(name="Incident")
class ReorderedIncident:
    """A code change without ``workflow.patched``: reproduce before triage."""

    @workflow.run
    async def run(self, alert: c.Alert, config: c.ControlConfig) -> c.Outcome:
        from kintsugi.control.workflows import act

        sig = c.Signature(alert.incident_id, alert.host, "phi", ())
        await act("reproduce", sig, config=config, result_type=c.Repro)
        raise AssertionError("unreachable in replay")


def test_an_unpatched_code_change_is_caught_by_replay() -> None:
    history = run(incident(Scenario()))[2]
    replayer = Replayer(workflows=[ReorderedIncident], workflow_runner=UnsandboxedWorkflowRunner())
    result = run(replayer.replay_workflow(history, raise_on_replay_failure=False))
    assert result.replay_failure is not None  # non-determinism detected


def test_workflows_import_cleanly_into_the_sandbox() -> None:
    names = {getattr(w, "__temporal_workflow_definition").name for w in WORKFLOWS}
    assert names == {
        "FleetSupervisor",
        "Incident",
        "Canary",
        "CrossHostMaturation",
        "VaccinationWave",
        "Onboarding",
        "SabotageCampaign",
        "AuditAnchor",
    }
    assert isinstance(Client, type)
