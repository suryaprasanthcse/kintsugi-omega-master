"""The control plane's workflows on Temporal (spec §5.3.1, §5.13).

- ``FleetSupervisor``: the long-running singleton. Receives ``hit`` signals, dedupes them,
  routes each through the Sentinel, opens ``Incident`` children within a per-host concurrency
  limit, forwards ``drift``, holds budgets and paused antibodies; ``continue_as_new`` every N.
- ``Incident``: the §5.13 loop. Every state change is a legal ``states.py`` transition, the run
  always ends RESOLVED, ESCALATED or PARKED (I3), and nothing ships without a G0–G6 winner (I1).
- ``Canary``: a Wald SPRT on live-traffic batches, with durable timers between them.
- ``CrossHostMaturation``: applies a candidate antibody to a second matching host.
- ``VaccinationWave``: a motif scan, then per-host application; pausable by signal.
- ``Onboarding``: steps O1–O7 for a new host.
- ``SabotageCampaign``: seals every fault before delivering any, on a schedule; Arena requests.
- ``AuditAnchor``: an hourly ledger anchor; ``continue_as_new`` daily.

Determinism (§5.3.3): workflow code calls no LLM, git, HTTP, clock or random generator, never
iterates a set (hash order differs across worker processes), and waits on durable timers only.
Code changes to running workflows go behind ``workflow.patched("…")``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.workflow import ParentClosePolicy

with workflow.unsafe.imports_passed_through():
    from kintsugi.control import contracts as c
    from kintsugi.control.activities import CREW, SPECS
    from kintsugi.control.states import IncidentState, next_state
    from kintsugi.stats.sprt import SPRT
    from kintsugi.stats.sprt import Decision as SprtDecision

S = IncidentState
SEEN_HIT_MEMORY = 5000  # hit keys remembered across continue_as_new (the store dedupes for good)
L4_REVERIFY_WINNER = "l4-reverify-winner"  # Stage 6: drift before shipping re-checks the winner
CANARY_HOST_P0 = "canary-host-p0"  # Stage 7: the canary's H0 comes from the host's baseline
ANTIBODY_LIFECYCLE = "antibody-lifecycle"  # Stage 7: maturation promotes MATURED → ACTIVE
L3_VACCINATION_CANARY = "l3-vaccination-canary"  # Stage 7: canary, revert, quarantine, refine
PAUSE_FORWARD = "pause-forwards-to-wave"  # Stage 7: pause_antibody reaches the running wave
CAMPAIGN_DELIVERY_GUARD = "campaign-delivery-guard"  # one failed delivery never ends a campaign


async def act(name: str, *args: Any, config: c.ControlConfig, result_type: Any = None) -> Any:
    """Run activity ``name`` with its §5.3.2 contract (timeouts, heartbeat, retries, pool)."""
    spec = SPECS[name]
    heartbeat = spec.heartbeat
    if heartbeat is not None and config.heartbeat_s is not None:
        heartbeat = timedelta(seconds=config.heartbeat_s)
    return await workflow.execute_activity(
        name,
        args=list(args),
        task_queue=spec.task_queue,
        start_to_close_timeout=spec.start_to_close,
        heartbeat_timeout=heartbeat,
        retry_policy=spec.retry_policy(),
        result_type=result_type,
    )


# --------------------------------------------------------------------------- Incident


@workflow.defn
class Incident:
    def __init__(self) -> None:
        self.state: str = str(S.OPEN)
        self.transitions: list[c.TransitionRecord] = []
        self.drift: tuple[str, str] | None = None
        self.attached: list[c.Hit] = []
        self.hypotheses: dict[str, int] = {}  # every hypothesis id used, across passes

    @workflow.signal
    def drift_signal(self, repo: str, new_main: str) -> None:
        """L4: main moved under the repair (§6.4)."""
        self.drift = (repo, new_main)

    @workflow.signal
    def attach(self, hit: c.Hit) -> None:
        self.attached.append(hit)

    @workflow.query
    def current_state(self) -> str:
        return self.state

    @workflow.query
    def transition_log(self) -> list[c.TransitionRecord]:
        return list(self.transitions)

    @workflow.run
    async def run(self, alert: c.Alert, config: c.ControlConfig) -> c.Outcome:
        outcome = await self._loop(alert, config)
        await act("ledger_append", {"type": "incident", "incident": alert.incident_id,
                                    "state": outcome.state, "reason": outcome.reason,
                                    "transitions": len(self.transitions)},
                  config=config, result_type=str)  # fmt: skip
        if alert.supervisor_id:
            supervisor = workflow.get_external_workflow_handle(alert.supervisor_id)
            await supervisor.signal("incident_closed", args=[alert.incident_id, outcome.state])
        return outcome

    async def _to(self, incident_id: str, target: S, outcome: str, config: c.ControlConfig) -> None:
        current = S(self.state)
        if next_state(current, outcome) is not target:
            raise ApplicationError(f"illegal transition {current} --{outcome}--> {target}",
                                   non_retryable=True)  # fmt: skip
        record = c.TransitionRecord(incident_id, len(self.transitions), str(current),
                                    str(target), outcome)  # fmt: skip
        await act("record_transition", record, config=config)
        self.transitions.append(record)
        self.state = str(target)

    def _end(self, incident_id: str, reason: str, shipment: c.Shipment | None = None) -> c.Outcome:
        return c.Outcome(incident_id, self.state, reason, tuple(self.transitions), shipment)

    async def _escalate(self, incident_id: str, reason: str, config: c.ControlConfig) -> c.Outcome:
        await act("escalate", incident_id, reason, config=config)
        return self._end(incident_id, reason)

    async def _loop(self, alert: c.Alert, config: c.ControlConfig) -> c.Outcome:
        inc = alert.incident_id
        while True:
            sig = await act("triage", alert, config=config, result_type=c.Signature)
            sig = replace(sig, pass_no=alert.reentries)
            await act("tag_traces", inc, list(sig.trace_ids), "before", config=config,
                      result_type=int)  # fmt: skip
            antibody = await act("match_antibody", sig, config=config,
                                 result_type=c.AntibodyMatch | None)  # fmt: skip
            repro = await act("reproduce", sig, config=config, result_type=c.Repro)
            if alert.reentries == 0:
                if not repro.ok:
                    await self._to(inc, S.PARKED, "g0_not_reproducible_after_3_attempts", config)
                    await act("park", sig, "G0 did not reproduce", config=config)
                    return self._end(inc, "G0 did not reproduce the failure")
                await self._to(inc, S.REPRODUCED, "g0_fails_3_of_3", config)
            cause = await act("attribute", sig, repro, config=config, result_type=c.Cause)
            if alert.reentries == 0:
                if not cause.suspects:
                    await self._to(inc, S.ESCALATED, "attribution_found_no_suspect", config)
                    return await self._escalate(inc, "attribution found no suspect symbol", config)
                await self._to(inc, S.ATTRIBUTED, "cause_report_has_suspect", config)
            plan = await act("plan_budget", sig, cause, config=config, result_type=c.BudgetPlan)
            if alert.reentries == 0:
                if plan.budget_usd <= 0:
                    await self._to(inc, S.ESCALATED, "budget_zero", config)
                    return await self._escalate(inc, "no repair budget (L5)", config)
                await self._to(inc, S.REPAIRING, "budget_positive", config)
            prior = None
            if antibody is not None:  # secondary response: the recipe is the hypothesis source
                prior = f"antibody:{antibody.antibody_id}@{antibody.version}:{antibody.recipe_sha}"
            else:
                prior = await act("fixes_prior", sig, config=config, result_type=str | None)

            winner = await self._rounds(sig, cause, plan, prior, config)
            if winner is None:
                await self._to(inc, S.ESCALATED, "rounds_or_budget_exhausted_or_cheaper_treatment",
                               config)  # fmt: skip
                return await self._escalate(inc, "rounds or budget exhausted", config)

            shipment = await act("ship", winner, config=config, result_type=c.Shipment)
            if not shipment.g7_ok or shipment.pr_url is None:
                await self._to(inc, S.ESCALATED, "rounds_or_budget_exhausted_or_cheaper_treatment",
                               config)  # fmt: skip
                return await self._escalate(inc, "G7 failed on the winner's PR", config)
            await self._to(inc, S.SHIPPED, "winner_passed_g0_g6_pr_open_g7_green", config)
            if not shipment.merged:
                await self._to(inc, S.ESCALATED, "merge_blocked", config)
                return await self._escalate(inc, "the merge was blocked", config)
            await self._to(inc, S.CANARY, "merged_or_pr_opened", config)

            canary = await workflow.execute_child_workflow(
                Canary.run, args=[shipment, config], id=f"canary-{inc}-{alert.reentries}"
            )
            if canary.rejected:  # L3
                reverted = await act("revert", shipment, config=config, result_type=bool)
                await act("quarantine_related", shipment, config=config, result_type=int)
                if reverted and alert.reentries < config.max_canary_reentries:
                    await self._to(inc, S.REPAIRING, "sprt_rejects_reverted_and_quarantined",
                                   config)  # fmt: skip
                    alert = alert.with_evidence(f"canary:{canary.samples}:{canary.llr:.4f}")
                    continue
                await self._to(inc, S.ESCALATED, "sprt_rejects_revert_failed_or_reentry_limit",
                               config)  # fmt: skip
                return await self._escalate(inc, "canary rejected; no re-entry left", config)

            await self._to(inc, S.RESOLVED, "sprt_accepts_h0_or_pr_only_canary_skipped", config)
            await act("tag_traces", inc, list(canary.trace_ids), "after", config=config,
                      result_type=int)  # fmt: skip
            ab = await act("synthesize_antibody", winner, cause, config=config,
                           result_type=c.AntibodyRef)  # fmt: skip
            await workflow.execute_child_workflow(
                CrossHostMaturation.run, args=[ab, sig.host, config], id=f"mature-{ab.antibody_id}"
            )
            await workflow.start_child_workflow(
                VaccinationWave.run, args=[ab, config], id=f"vaccinate-{ab.antibody_id}",
                parent_close_policy=ParentClosePolicy.ABANDON,
            )  # fmt: skip
            await act("backfill_receptor", ab, config=config)
            await act("fixes_resolve", sig, shipment, config=config)
            return self._end(inc, "canary accepted; antibody synthesized", shipment)

    async def _rounds(
        self,
        sig: c.Signature,
        cause: c.Cause,
        plan: c.BudgetPlan,
        prior: str | None,
        config: c.ControlConfig,
    ) -> c.SurgeonOutcome | None:
        history: list[c.GateVerdict] = []
        for rnd in range(min(plan.max_rounds, config.max_rounds)):
            if rnd > 0:
                await self._to(sig.incident_id, S.REPAIRING,
                               "round_failed_rounds_and_budget_remain", config)  # fmt: skip
            cards = await act("hypothesize", sig, cause, plan, rnd, history, prior, config=config,
                              result_type=list[c.Card])  # fmt: skip
            for card in cards:
                if card.hypothesis in self.hypotheses:
                    raise ApplicationError(
                        f"hypothesis {card.hypothesis} reused in {sig.incident_id}",
                        non_retryable=True,
                    )
                self.hypotheses[card.hypothesis] = rnd
            outs: list[c.SurgeonOutcome] = list(await asyncio.gather(*[
                act("surgeon", card, config=config, result_type=c.SurgeonOutcome) for card in cards
            ]))  # fmt: skip
            verdicts: list[c.GateVerdict] = list(await asyncio.gather(*[
                act("audit_gates", out, config=config, result_type=c.GateVerdict) for out in outs
            ]))  # fmt: skip
            decision = await act("coordinate", sig, rnd, verdicts, config=config,
                                 result_type=c.Decision)  # fmt: skip
            if decision.dispute:
                decision = await act("dispute_experiment", sig.incident_id, decision,
                                     config=config, result_type=c.Decision)  # fmt: skip
            if decision.winner is not None:
                passed = {v.hypothesis for v in verdicts if v.passed}
                if decision.winner not in passed:  # I1: only a G0–G6 pass may ship
                    raise ApplicationError(
                        f"winner {decision.winner} did not pass G0–G6", non_retryable=True
                    )
                winner = next(o for o in outs if o.hypothesis == decision.winner)
                if self.drift is None or not workflow.patched(L4_REVERIFY_WINNER):
                    return winner
                # L4 (§6.4): main moved during the round. The winner is re-verified merged
                # onto the new main (its attested commit is never rewritten, A-49); it ships
                # only if it still passes, otherwise the round counts as failed.
                repo, new_main = self.drift
                self.drift = None
                await act("rebase_and_rerun", [winner], repo, new_main, config=config)
                moved = replace(winner, diff_sha=f"{winner.commit_sha}@{new_main}")
                recheck = await act("audit_gates", moved, config=config,
                                    result_type=c.GateVerdict)  # fmt: skip
                if recheck.passed:
                    return moved
                verdicts = [v for v in verdicts if v.hypothesis != winner.hypothesis] + [recheck]
            history.extend(verdicts)
            await act("rewind", outs, config=config)
            if self.drift is not None:  # L4: rebase onto the new main, re-run impacted gates
                repo, new_main = self.drift
                self.drift = None
                await act("rebase_and_rerun", outs, repo, new_main, config=config)
            if await act("budget_exhausted", sig, plan, rnd, config=config, result_type=bool):
                return None
        return None


# --------------------------------------------------------------------------- Canary


@workflow.defn
class Canary:
    @workflow.run
    async def run(self, shipment: c.Shipment, config: c.ControlConfig) -> c.CanaryResult:
        p0, ratio = config.canary_p0, config.canary_ratio
        if workflow.patched(CANARY_HOST_P0):  # Stage 7: H0 is the host's own baseline (§5.12)
            plan = await act("canary_plan", shipment, config=config, result_type=c.CanaryPlan)
            p0, ratio = plan.p0, plan.ratio
        sprt = SPRT.regression(p0, ratio=ratio)
        llr, samples = 0.0, 0
        for batch in range(config.canary_max_batches):
            if batch:
                await asyncio.sleep(config.canary_interval_s)  # durable timer
            failures = await act("canary_sample", shipment, batch, config=config,
                                 result_type=list[bool])  # fmt: skip
            for failed in failures:
                samples += 1
                llr += sprt.step(failed)
                verdict = sprt.decide(llr)
                if verdict is SprtDecision.REJECT:
                    return c.CanaryResult(False, True, samples, llr)
                if verdict is SprtDecision.ACCEPT:
                    return c.CanaryResult(True, False, samples, llr)
        # Out of batches without a boundary: no evidence of a regression at the planned power.
        return c.CanaryResult(True, False, samples, llr)


# --------------------------------------------------------------------------- antibodies


@workflow.defn
class CrossHostMaturation:
    @workflow.run
    async def run(self, ab: c.AntibodyRef, origin: str, config: c.ControlConfig) -> c.StepResult:
        hosts = await act("motif_scan", ab, config=config, result_type=list[str])
        if not workflow.patched(ANTIBODY_LIFECYCLE):
            second = next((h for h in hosts if h != origin), None)
            if second is None:
                return c.StepResult("mature", False, {"reason": "no second matching host"})
            result = await act("apply_antibody", ab, second, config=config,
                               result_type=c.VaccinationResult)  # fmt: skip
            return c.StepResult("mature", result.applied, {"host": second, "reason": result.reason})
        # Stage 7 (§5.8.2): the first second host the antibody actually repairs (induced trigger,
        # recipe, G0–G6, a pull request) matures it; the receptor's precision then activates it.
        tried: dict[str, str] = {}
        for host in hosts:
            if host == origin:
                continue
            result = await act("mature_antibody", ab, host, config=config,
                               result_type=c.VaccinationResult)  # fmt: skip
            tried[host] = result.reason
            if result.applied:
                promoted = await act("promote_antibody", ab, host, config=config,
                                     result_type=c.AntibodyRef)  # fmt: skip
                return c.StepResult("mature", True, {"host": host, "state": promoted.state,
                                                     "version": promoted.version,
                                                     "reason": result.reason})  # fmt: skip
        return c.StepResult("mature", False, {"reason": "no second host matured", "tried": tried})


@workflow.defn
class VaccinationWave:
    def __init__(self) -> None:
        self.paused = False
        self.done: list[str] = []
        self.l3: list[dict[str, Any]] = []  # autoimmune episodes: canary, revert, refinement

    @workflow.signal
    def pause(self) -> None:
        self.paused = True

    @workflow.signal
    def resume(self) -> None:
        self.paused = False

    @workflow.query
    def progress(self) -> list[str]:
        return list(self.done)

    @workflow.query
    def autoimmunity(self) -> list[dict[str, Any]]:
        return list(self.l3)

    @workflow.run
    async def run(self, ab: c.AntibodyRef, config: c.ControlConfig) -> list[c.VaccinationResult]:
        hosts = await act("motif_scan", ab, config=config, result_type=list[str])
        results: list[c.VaccinationResult] = []
        if not workflow.patched(L3_VACCINATION_CANARY):
            for host in hosts:
                await workflow.wait_condition(lambda: not self.paused)
                result = await act("apply_antibody", ab, host, config=config,
                                   result_type=c.VaccinationResult)  # fmt: skip
                results.append(result)
                self.done.append(host)
            return results
        # Stage 7 (§6.5 step 14, §6.7): every merged vaccination runs its own canary. A rejection
        # is autoimmunity: pause, revert (then a reverse canary), quarantine, learn the missing
        # precondition, and resume with the refined version on the hosts it still matches.
        queue = list(hosts)
        while queue:
            host = queue.pop(0)
            await workflow.wait_condition(lambda: not self.paused)
            result = await act("apply_antibody", ab, host, config=config,
                               result_type=c.VaccinationResult)  # fmt: skip
            results.append(result)
            self.done.append(host)
            shipment = result.shipment
            if not (result.applied and shipment is not None and shipment.merged):
                continue
            canary_id = f"canary-{shipment.incident_id}-{ab.version}"
            canary = await workflow.execute_child_workflow(
                Canary.run, args=[shipment, config], id=canary_id
            )
            if not canary.rejected:
                continue
            self.paused = True
            episode: dict[str, Any] = {
                "host": host,
                "version": ab.version,
                "canary": {"n": canary.samples, "llr": canary.llr},
            }
            await self._tell_supervisor("pause_antibody", ab.antibody_id, config)
            undone = await act("revert_vaccination", shipment, config=config,
                               result_type=c.Shipment | None)  # fmt: skip
            if undone is not None and undone.merged:
                reverse = await workflow.execute_child_workflow(
                    Canary.run, args=[undone, config], id=f"canary-{undone.incident_id}-reverse"
                )
                episode["reverse_canary"] = {"accepted": reverse.accepted, "n": reverse.samples,
                                             "llr": reverse.llr}  # fmt: skip
            episode["revert"] = undone.pr_url if undone is not None else None
            episode["quarantined"] = await act("quarantine_related", shipment, config=config,
                                               result_type=int)  # fmt: skip
            refined = await act("refine_antibody", ab, shipment, config=config,
                                result_type=c.AntibodyRef | None)  # fmt: skip
            episode["refined"] = f"{refined.antibody_id}@{refined.version}" if refined else None
            self.l3.append(episode)
            if refined is None or refined.state != "ACTIVE":
                return results  # stays quarantined: no further application
            ab = refined
            rescan = await act("motif_scan", ab, config=config, result_type=list[str])
            queue = [h for h in rescan if h not in self.done]
            self.paused = False
            await self._tell_supervisor("resume_antibody", ab.antibody_id, config)
        return results

    async def _tell_supervisor(
        self, signal: str, antibody_id: str, config: c.ControlConfig
    ) -> None:
        try:
            supervisor = workflow.get_external_workflow_handle(config.supervisor_id)
            await supervisor.signal(signal, antibody_id)
        except Exception:  # a wave started without a supervisor (tests, drills) carries on
            workflow.logger.info("no supervisor to tell: %s %s", signal, antibody_id)


# --------------------------------------------------------------------------- onboarding, proof


ONBOARDING_STEPS = ("O1", "O2", "O3", "O4", "O5", "O6", "O7")


@workflow.defn
class Onboarding:
    @workflow.run
    async def run(self, host: str, config: c.ControlConfig) -> list[c.StepResult]:
        results: list[c.StepResult] = []
        for step in ONBOARDING_STEPS:
            result = await act("onboard_step", host, step, config=config,
                               result_type=c.StepResult)  # fmt: skip
            results.append(result)
            if not result.ok:
                break
        return results


@dataclass
class Campaign:
    """Sabotage specs and their delivery delays (seconds after the campaign starts)."""

    specs: list[tuple[str, float]] = field(default_factory=list)


@workflow.defn
class SabotageCampaign:
    def __init__(self) -> None:
        self.requests: list[tuple[str, float]] = []
        self.sealed: dict[str, str] = {}
        self.delivered: list[str] = []
        self.failed: dict[str, str] = {}  # spec id → why its delivery failed (guarded runs)
        self.closed = False

    @workflow.signal
    def request(self, spec_id: str, delay_s: float) -> None:
        """Arena: an outside sabotage request, sealed on arrival (§7.7)."""
        self.requests.append((spec_id, delay_s))

    @workflow.signal
    def close(self) -> None:
        self.closed = True

    @workflow.query
    def commitments(self) -> dict[str, str]:
        return dict(self.sealed)

    @workflow.query
    def failures(self) -> dict[str, str]:
        return dict(self.failed)

    async def _deliver(self, spec_id: str, config: c.ControlConfig) -> None:
        """One delivery, isolated: a spec that cannot be delivered (after its retries, or
        refused outright) is recorded on the ledger and the campaign moves on, so a public
        Arena request can never stop the sealed schedule behind it."""
        if not workflow.patched(CAMPAIGN_DELIVERY_GUARD):
            await act("deliver_sabotage", spec_id, config=config)
            self.delivered.append(spec_id)
            return
        try:
            await act("deliver_sabotage", spec_id, config=config)
        except ActivityError as error:
            cause = error.cause if error.cause is not None else error
            reason = f"{type(cause).__name__}: {cause}"[:300]
            self.failed[spec_id] = reason
            await act("ledger_append", {"type": "sabotage", "event": "delivery-failed",
                                        "sabotage": spec_id, "reason": reason},
                      config=config, result_type=str)  # fmt: skip
            return
        self.delivered.append(spec_id)

    @workflow.run
    async def run(self, campaign: Campaign, config: c.ControlConfig) -> list[str]:
        queue = list(campaign.specs)
        # Seal every planned fault before the first delivery (§7.2: commitment before injection).
        for spec_id, _ in queue:
            self.sealed[spec_id] = await act("seal_sabotage", spec_id, config=config,
                                             result_type=str)  # fmt: skip
        elapsed = 0.0
        while queue or not self.closed:
            while self.requests:
                spec_id, delay = self.requests.pop(0)
                self.sealed[spec_id] = await act("seal_sabotage", spec_id, config=config,
                                                 result_type=str)  # fmt: skip
                queue.append((spec_id, elapsed + delay))
            if not queue:
                await workflow.wait_condition(lambda: bool(self.requests) or self.closed)
                continue
            queue.sort(key=lambda item: item[1])
            spec_id, at = queue.pop(0)
            if at > elapsed:
                await asyncio.sleep(at - elapsed)
                elapsed = at
            await self._deliver(spec_id, config)
        return list(self.delivered)


@workflow.defn
class AuditAnchor:
    @workflow.run
    async def run(self, first_hour: int, config: c.ControlConfig, hours: int = 24) -> int:
        for hour in range(first_hour, first_hour + hours):
            await act("anchor_ledger", hour, config=config, result_type=str)
            await asyncio.sleep(config.anchor_interval_s)
        workflow.continue_as_new(args=[first_hour + hours, config, hours])


# --------------------------------------------------------------------------- FleetSupervisor


@dataclass
class SupervisorState:
    """What a FleetSupervisor run hands to the next one at ``continue_as_new``."""

    open: dict[str, str] = field(default_factory=dict)  # incident → host
    waiting: list[c.Alert] = field(default_factory=list)  # held back by the per-host limit
    pending: list[c.Hit] = field(default_factory=list)  # signalled, not yet routed
    seen: list[str] = field(default_factory=list)  # recent hit keys (bounded)
    budgets: dict[str, float] = field(default_factory=dict)
    paused_antibodies: list[str] = field(default_factory=list)
    closed: dict[str, str] = field(default_factory=dict)  # incident → terminal state
    events: int = 0  # across runs
    runs: int = 1
    operator_paused: str | None = None


@workflow.defn
class FleetSupervisor:
    def __init__(self) -> None:
        self.s = SupervisorState()
        self.config = c.ControlConfig()
        self.run_events = 0
        self.drifts: list[tuple[str, str]] = []
        self.forward: list[tuple[str, str]] = []  # (pause | resume, antibody) for running waves

    # signals (§5.3.3) -------------------------------------------------------------------
    @workflow.signal
    def hit(self, hit: c.Hit) -> None:
        self.s.pending.append(hit)

    @workflow.signal
    def drift(self, repo: str, new_main: str) -> None:
        self.drifts.append((repo, new_main))

    @workflow.signal
    def budget_update(self, host: str, pool_usd: float) -> None:
        self.s.budgets[host] = pool_usd

    @workflow.signal
    def pause_antibody(self, antibody_id: str) -> None:
        if antibody_id not in self.s.paused_antibodies:
            self.s.paused_antibodies.append(antibody_id)
        self.forward.append(("pause", antibody_id))

    @workflow.signal
    def resume_antibody(self, antibody_id: str) -> None:
        if antibody_id in self.s.paused_antibodies:
            self.s.paused_antibodies.remove(antibody_id)
        self.forward.append(("resume", antibody_id))

    @workflow.signal
    def operator_pause(self, reason: str) -> None:
        self.s.operator_paused = reason

    @workflow.signal
    def operator_resume(self) -> None:
        self.s.operator_paused = None

    @workflow.signal
    def incident_closed(self, incident_id: str, state: str) -> None:
        self.s.open.pop(incident_id, None)
        self.s.closed[incident_id] = state

    # queries ----------------------------------------------------------------------------
    @workflow.query
    def status(self) -> dict[str, Any]:
        return {"open": len(self.s.open), "waiting": len(self.s.waiting),
                "pending": len(self.s.pending), "events": self.s.events, "runs": self.s.runs,
                "closed": dict(self.s.closed), "operator_paused": self.s.operator_paused,
                "paused_antibodies": list(self.s.paused_antibodies)}  # fmt: skip

    @workflow.query
    def open_incidents(self) -> dict[str, str]:
        return dict(self.s.open)

    @workflow.query
    def budget(self, host: str) -> float | None:
        return self.s.budgets.get(host)

    # run --------------------------------------------------------------------------------
    @workflow.run
    async def run(self, config: c.ControlConfig, state: SupervisorState | None = None) -> None:
        self.config = config
        if state is not None:
            self.s = state
            self.s.runs += 1
        while True:
            await workflow.wait_condition(self._has_work)
            if self.forward:
                if workflow.patched(PAUSE_FORWARD):
                    await self._forward_to_waves()
                self.forward.clear()  # a history from before the patch only recorded the list
            while self.drifts:
                repo, new_main = self.drifts.pop(0)
                for incident_id in list(self.s.open):
                    child = workflow.get_external_workflow_handle(incident_id)
                    await child.signal("drift_signal", args=[repo, new_main])
            if self.s.operator_paused is None:
                await self._start_waiting(config)
                while self.s.pending and self.s.operator_paused is None:
                    await self._route(self.s.pending.pop(0), config)
                    self.s.events += 1
                    self.run_events += 1
                    if self.run_events >= config.continue_as_new_every:
                        await workflow.wait_condition(workflow.all_handlers_finished)
                        workflow.continue_as_new(args=[config, self.s])

    def _host_open(self, host: str) -> int:
        return sum(1 for h in self.s.open.values() if h == host)

    async def _forward_to_waves(self) -> None:
        """§6.7 step 17: a paused antibody's VaccinationWave halts its pending applications."""
        while self.forward:
            action, antibody_id = self.forward.pop(0)
            try:
                wave = workflow.get_external_workflow_handle(f"vaccinate-{antibody_id}")
                await wave.signal(action)
            except Exception:  # no wave running for it: the paused list is the record
                workflow.logger.info("no running wave for %s (%s)", antibody_id, action)

    def _has_work(self) -> bool:
        if self.drifts or self.forward:
            return True
        if self.s.operator_paused is not None:
            return False
        limit = self.config.max_open_per_host
        return bool(self.s.pending) or any(self._host_open(a.host) < limit for a in self.s.waiting)

    async def _start_waiting(self, config: c.ControlConfig) -> None:
        still: list[c.Alert] = []
        for alert in self.s.waiting:
            if self._host_open(alert.host) < config.max_open_per_host:
                await self._start(alert, config)
            else:
                still.append(alert)
        self.s.waiting = still

    async def _route(self, hit: c.Hit, config: c.ControlConfig) -> None:
        if hit.hit_key in self.s.seen:
            return
        self.s.seen.append(hit.hit_key)
        del self.s.seen[:-SEEN_HIT_MEMORY]
        decision = await act("route_hit", hit, list(self.s.open), config=config,
                             result_type=c.RouteDecision)  # fmt: skip
        if decision.action == "attach" and decision.incident_id in self.s.open:
            child = workflow.get_external_workflow_handle(decision.incident_id)
            await child.signal("attach", hit)
        elif decision.action == "open" and decision.incident_id:
            if decision.incident_id in self.s.open or decision.incident_id in self.s.closed:
                return
            alert = c.Alert(decision.incident_id, hit.host or "unknown", (hit,),
                            decision.severity, config.supervisor_id)  # fmt: skip
            if self._host_open(alert.host) < config.max_open_per_host:
                await self._start(alert, config)
            else:
                self.s.waiting.append(alert)

    async def _start(self, alert: c.Alert, config: c.ControlConfig) -> None:
        await workflow.start_child_workflow(
            Incident.run,
            args=[alert, config],
            id=alert.incident_id,
            task_queue=CREW,
            parent_close_policy=ParentClosePolicy.ABANDON,
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        self.s.open[alert.incident_id] = alert.host


WORKFLOWS = (FleetSupervisor, Incident, Canary, CrossHostMaturation, VaccinationWave, Onboarding,
             SabotageCampaign, AuditAnchor)  # fmt: skip
