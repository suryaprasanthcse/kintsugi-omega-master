"""The Coordinator and the governor's plan: budget, hypotheses, decisions, disputes (spec §4.6,
§5.7, §6.2 step 9, §6.3).

- ``plan_budget`` (governor): B_ι = min(Cap_ι, κ·VaR_ι) from the host's economics and the
  plan inputs; the paired sample n from Connor's formula at power 0.8; the fork count K as the
  largest that fits B_ι (capped by policy). The frozen plan is hashed (``plan_sha``).
- ``hypothesize`` (``claude-opus-5-5``): K hypothesis cards from the cause report, the verified
  intent record, the prior and the aggregate feedback of earlier rounds. Protected paths are
  removed from every card's allowed files; a diff budget over 120 lines needs a stated reason,
  which is recorded as the Coordinator's approval. Hypothesis ids are unique per incident.
- ``coordinate``: the §5.7 decision rule. Candidates that passed G0–G6 → G5 concerns ranked down
  (kept as fallbacks, chosen only in the last round) → the Pareto front on (Δpass ↑, cost Δ ↓,
  p95 Δ ↓). One candidate on the front wins; several mean a **dispute**, settled by an
  experiment whose protocol is registered (hashed and stored) *before* it runs.
- ``dispute_experiment`` re-reads the registered protocol, checks its hash, runs it (stress
  profile, the same tasks and seeds for every candidate) and applies the pre-registered
  criterion: higher pass rate; tie → lower cost per task; tie → smaller diff.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from kintsugi.control import contracts as c
from kintsugi.crew.consult import consult
from kintsugi.crew.context import CrewContext
from kintsugi.crew.manifest import ContextBuilder
from kintsugi.econ import governor
from kintsugi.econ.cortex import Cortex, LiveInputs, decision_record
from kintsugi.gates.checkout import clean_checkout, rev
from kintsugi.gates.evals import run_tasks
from kintsugi.gates.protected_paths import load_policy
from kintsugi.proof.canonical import canonicalize, sha256_hex
from kintsugi.sandbox import chaos_proxy as chaos
from kintsugi.stats import power

MAX_K = 3  # policy cap on parallel Surgeons (spec example K = 3)
DISPUTE_RUNS = 30


@dataclass(frozen=True)
class PlanInputs:
    """Frozen business-plan inputs until Stage 9 wires cfo.ai actuals (spec §4.6.4)."""

    gm_floor: float = governor.GM_FLOOR_DEFAULT
    c_mon: float = 18.0
    spent_mtd: float = 0.0
    expected_remaining_incidents: float = 4.0
    cost_per_run: float = 0.004  # holdout task-run (service calls + metered model)
    cost_per_hypothesis: float = 3.0  # Surgeon session + its audits
    p01: float = 0.02  # expected pass→fail share of a good fix
    holdout_pairs: int = 120  # 40 tasks × 3 seeds


CARD_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "cards": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "root_cause": {"type": "string"},
                    "approach": {"type": "string"},
                    "intent_constraints": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "clause": {"type": "string"},
                                "quote": {"type": "string"},
                            },
                            "required": ["clause", "quote"],
                            "additionalProperties": False,
                        },
                    },
                    "allowed_files": {"type": "array", "items": {"type": "string"}},
                    "diff_budget": {"type": "integer", "minimum": 1},
                    "large_diff_reason": {"type": "string"},
                },
                "required": [
                    "title",
                    "root_cause",
                    "approach",
                    "intent_constraints",
                    "allowed_files",
                    "diff_budget",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["cards"],
    "additionalProperties": False,
}


def digest(value: Any) -> str:
    return "sha256:" + sha256_hex(canonicalize(value))


# Differences smaller than these are measurement noise (pass-rate share, relative cost, relative
# p95): they never make one candidate dominate another, so equivalent fixes stay on the front
# and a pre-registered experiment settles them (§5.7 disputes, §6.3 H2′ vs H3″).
TOLERANCE = (0.02, 0.02, 0.05)


def pareto_front(candidates: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Non-dominated candidates on (delta_pass ↑, cost_delta ↓, p95_delta ↓), within noise."""

    def key(item: Mapping[str, Any]) -> tuple[float, float, float]:
        g4 = item["g4"]
        return (
            float(g4.get("delta_pass") or 0.0),
            float(g4.get("cost_delta") or 0.0),
            float(g4.get("p95_delta") or 0.0),
        )

    def dominates(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
        (pa, ca, la), (pb, cb, lb) = key(a), key(b)
        tp, tc, tl = TOLERANCE
        no_worse = pa >= pb - tp and ca <= cb + tc and la <= lb + tl
        better = pa > pb + tp or ca < cb - tc or la < lb - tl
        return no_worse and better

    return [x for x in candidates if not any(dominates(y, x) for y in candidates if y is not x)]


class Coordinator:
    def __init__(
        self, ctx: CrewContext, inputs: PlanInputs | None = None, cortex: Cortex | None = None
    ) -> None:
        self.ctx = ctx
        self.inputs = inputs or PlanInputs()
        self.cortex = cortex  # Stage 9: the business plan and live actuals replace ``inputs``

    # ---- governor ------------------------------------------------------------------------------

    def plan_budget(self, sig: c.Signature, cause: c.Cause) -> c.BudgetPlan:
        target = self.ctx.target(sig.host)
        econ = target.host.economics
        case = self.ctx.case(sig.incident_id)
        opened = case.reader("governor").get("incident_open", "open", None) or {}
        p: PlanInputs | LiveInputs = self.inputs
        if self.cortex is not None:
            p = self.cortex.inputs(sig.host)
        r_fail = 0.18
        if opened.get("var_estimate"):
            r_fail = min(
                1.0,
                float(opened["var_estimate"])
                / (econ.tasks_per_hour * econ.value_per_failed_task_usd * 24.0),
            )
        budget = governor.incident_budget(
            price=econ.price_usd_month,
            gm_floor=p.gm_floor,
            c_mon=p.c_mon,
            spent_mtd=p.spent_mtd,
            expected_remaining_incidents=p.expected_remaining_incidents,
            r_fail=max(r_fail, 0.01),
            lam_per_h=econ.tasks_per_hour,
            v_per_task=econ.value_per_failed_task_usd,
        )
        psi, delta = power.discordant(max(r_fail, p.p01 + 0.05), p.p01)
        n = power.connor_n(psi, delta)
        repair = governor.plan_repair(
            n,
            p.cost_per_run,
            p.cost_per_hypothesis,
            budget.budget,
            [k for k in governor.K_OPTIONS if k <= MAX_K],
        )
        k = repair.k if repair else 0
        economics = None
        if self.cortex is not None and repair is None:  # L5: never a smaller n, a cheaper path
            choice = self.cortex.decide(sig.host, budget_usd=budget.budget, n_required=n,
                                        var_usd=budget.var,
                                        k_options=tuple(k for k in governor.K_OPTIONS
                                                        if k <= MAX_K))  # fmt: skip
            economics = decision_record(choice, self.cortex.plan, sig.host)
        plan = {
            "incident": sig.incident_id,
            "host": sig.host,
            "inputs": vars(p),
            "economics": econ.model_dump(),
            "r_fail": round(r_fail, 4),
            "pool": round(budget.pool, 4),
            "remaining": round(budget.remaining, 4),
            "cap": round(budget.cap, 4),
            "var": round(budget.var, 4),
            "budget_usd": round(budget.budget if repair else 0.0, 4),
            "k": k,
            "n_pairs": n,
            "holdout_pairs": p.holdout_pairs,
            "max_rounds": 3,
            "tier": self.ctx.models.get("surgeon", "claude-sonnet-5-5"),
            "cause_sha": digest(
                {"suspects": list(cause.suspects), "intent": cause.intent_record_sha}
            ),
        }
        if self.cortex is not None:
            plan["business_plan_sha"] = self.cortex.plan.sha  # pinned per incident
            plan["economic_decision"] = economics
        plan["plan_sha"] = digest(plan)
        case.put("plan", "plan", plan, by="governor")
        if economics is not None:  # the L5 decision span, for the ledger and the dossier
            case.put("plan", "economics", {**economics, "plan_sha": plan["plan_sha"]},
                     by="governor")  # fmt: skip
        return c.BudgetPlan(plan["budget_usd"], k, 3, plan["tier"], plan["plan_sha"])

    def budget_exhausted(self, sig: c.Signature, plan: c.BudgetPlan, rnd: int) -> bool:
        spent = self.ctx.case(sig.incident_id).spent()
        p = self.cortex.inputs(sig.host) if self.cortex is not None else self.inputs
        next_round = max(1, plan.k) * p.cost_per_hypothesis
        return spent >= plan.budget_usd or spent + next_round > plan.budget_usd + 1e-9

    # ---- hypotheses ---------------------------------------------------------------------------

    def _next_ids(self, case: Any, k: int) -> list[str]:
        used = case.reader("coordinator").names("card")
        start = 1 + max((int(n[1:]) for n in used if n[1:].isdigit()), default=0)
        return [f"H{start + i}" for i in range(k)]

    def hypothesize(
        self,
        sig: c.Signature,
        cause: c.Cause,
        plan: c.BudgetPlan,
        rnd: int,
        history: list[c.GateVerdict],
        prior: str | None,
    ) -> list[c.Card]:
        target = self.ctx.target(sig.host)
        case = self.ctx.case(sig.incident_id)
        view = case.reader("coordinator")
        report = view.get("cause", "report", None) or {}
        record = view.get("intent_record", "record", None) or {}
        feedback = {v.hypothesis: view.get("feedback", v.hypothesis, None) for v in history}
        k = max(1, plan.k)
        policy = load_policy()
        builder = ContextBuilder("coordinator", sig.incident_id)
        for name, value in (
            ("incident", sig.incident_id),
            ("host", sig.host),
            ("round", rnd),
            ("k", k),
            ("diff_budget", policy.diff_budget_lines),
        ):
            builder.bind(name, value)
        builder.add("symptom", sig.summary, source="symptom summary", why="what fails")
        builder.add(
            "cause",
            json.dumps(
                {
                    key: report.get(key)
                    for key in (
                        "suspects",
                        "mechanism",
                        "regions",
                        "impact_sets",
                        "regression",
                        "first_bad_checkpoint",
                        "originating_commit",
                    )
                },
                indent=1,
            ),
            source="Archivist cause report",
            why="where and why",
        )
        builder.add(
            "intent",
            json.dumps(
                {
                    key: record.get(key)
                    for key in ("stated_goal", "behavioral_contract", "checkpoint")
                },
                indent=1,
            ),
            source="verified intent record",
            why="constraints the fix must keep",
        )
        builder.add(
            "plan",
            f"B=${plan.budget_usd:.2f}, K={k}, tier {plan.tier}, plan {plan.plan_sha}",
            source="governor plan",
            why="budget and fork count",
        )
        builder.add("prior", prior or "(none)", source="Fixes / antibody prior", why="evidence")
        builder.add(
            "history",
            json.dumps(feedback, indent=1) if feedback else "(round 0)",
            source="aggregate gate feedback",
            why="what failed and why",
        )
        builder.exclude("holdout tasks", "invariant I5")
        builder.exclude("surgeon transcripts and self-reports", "role isolation (§5.7)")
        files = sorted(
            {
                str(r["file"])
                for r in report.get("regions") or []
                if not policy.violations([str(r["file"])], ["surgeon"])
            }
        )

        def fallback() -> dict[str, Any]:
            return {"cards": deterministic_cards(report, record, prior, files, k, rnd)}

        answer = consult(
            self.ctx.model,
            case,
            builder,
            "coordinator",
            schema=CARD_SCHEMA,
            fallback=fallback,
            max_budget_usd=3.0,
            model_id=self.ctx.model_for("coordinator"),
        )
        base = rev(target.repo, self.ctx.base_ref)
        cards: list[c.Card] = []
        for hyp, raw in zip(self._next_ids(case, k), list(answer.data["cards"])[:k], strict=False):
            allowed = [f for f in raw["allowed_files"] if not policy.violations([f], ["surgeon"])]
            budget = int(raw["diff_budget"])
            approved = budget > policy.diff_budget_lines and bool(raw.get("large_diff_reason"))
            card = {
                **raw,
                "allowed_files": allowed or files,
                "hypothesis": hyp,
                "round": rnd,
                "host": sig.host,
                "base_sha": base,
                "approved_large_diff": approved,
                "diff_budget": budget if approved else min(budget, policy.diff_budget_lines),
            }
            case.put("card", hyp, card, by="coordinator")
            cards.append(c.Card(sig.incident_id, hyp, rnd, json.dumps(card, sort_keys=True), base))
        return cards

    # ---- decisions ----------------------------------------------------------------------------

    def coordinate(self, sig: c.Signature, rnd: int, verdicts: list[c.GateVerdict]) -> c.Decision:
        case = self.ctx.case(sig.incident_id)
        view = case.reader("coordinator")
        evidence = [view.get("verdict", v.hypothesis) for v in verdicts if v.passed]
        clean = [e for e in evidence if e["gates"]["G5"]["status"] == "pass"]
        fallbacks = [e for e in evidence if e["gates"]["G5"]["status"] == "warn"]
        last_round = rnd >= 2
        pool = clean or (fallbacks if last_round else [])
        record: dict[str, Any] = {
            "round": rnd,
            "passed": [e["hypothesis"] for e in evidence],
            "fallbacks": [e["hypothesis"] for e in fallbacks],
        }
        if not pool:
            case.put("decision", f"round{rnd}", {**record, "winner": None}, by="coordinator")
            return c.Decision(rnd)
        front = pareto_front(pool)
        record["front"] = [e["hypothesis"] for e in front]
        if len(front) == 1:
            winner = front[0]["hypothesis"]
            case.put("decision", f"round{rnd}", {**record, "winner": winner}, by="coordinator")
            return c.Decision(rnd, winner)
        target = self.ctx.target(sig.host)
        repro = view.get("repro", "g0", None) or {}
        protocol = {
            "incident": sig.incident_id,
            "host": sig.host,
            "round": rnd,
            "question": "which Pareto-optimal candidate holds up under a harsher fault profile?",
            "candidates": sorted(
                (
                    {
                        "hypothesis": e["hypothesis"],
                        "commit": e["commit"],
                        "diff_lines": e["diff_lines"],
                    }
                    for e in front
                ),
                key=lambda x: x["hypothesis"],
            ),
            "profile": target.stress_profile or "background",
            "tasks": list(repro.get("tasks") or [])[:10],
            "seeds": [101, 102, 103],
            "runs_per_candidate": DISPUTE_RUNS,
            "criterion": ["higher pass rate", "tie: lower mean cost per task", "tie: smaller diff"],
        }
        sha = digest(protocol)
        case.put(
            "experiment",
            sha.removeprefix("sha256:")[:16],
            {**protocol, "sha": sha},
            by="coordinator",
        )
        case.put(
            "decision", f"round{rnd}", {**record, "winner": None, "dispute": sha}, by="coordinator"
        )
        return c.Decision(rnd, None, True, sha)

    def dispute_experiment(self, incident_id: str, decision: c.Decision) -> c.Decision:
        sha = str(decision.experiment_sha)
        case = self.ctx.case(incident_id)
        protocol = dict(
            case.reader("coordinator").get("experiment", sha.removeprefix("sha256:")[:16])
        )
        protocol.pop("sha", None)
        if digest(protocol) != sha:
            raise ValueError(f"{incident_id}: the dispute protocol changed after registration")
        target = self.ctx.target(str(protocol["host"]))
        profile = (
            target.stress() if protocol["profile"] != "background" else None
        ) or chaos.background_profile()
        target.make_importable()
        pool = {
            t["id"]: t
            for t in importlib.import_module(f"{target.host.package}.evals.generate").tasks("train")
        }
        ids = protocol["tasks"] or sorted(pool)[:10]
        tasks = [pool[i] for i in ids if i in pool]
        per_seed = max(1, protocol["runs_per_candidate"] // max(1, len(tasks)))
        seeds = list(protocol["seeds"])[:per_seed] or [101]
        results = []
        for candidate in protocol["candidates"]:
            runs = []
            with clean_checkout(target.repo, candidate["commit"]) as checkout:
                for seed in seeds:
                    runs += run_tasks(
                        target, checkout, tasks, seed=seed, profile=profile, phase="dispute"
                    ).runs
            passed = sum(r.passed for r in runs)
            cost = sum(r.cost_usd for r in runs) / max(1, len(runs))
            results.append(
                {
                    "hypothesis": candidate["hypothesis"],
                    "runs": len(runs),
                    "passed": passed,
                    "pass_rate": passed / max(1, len(runs)),
                    "mean_cost": round(cost, 6),
                    "diff_lines": candidate["diff_lines"],
                }
            )
        ranked = sorted(
            results,
            key=lambda r: (-r["pass_rate"], r["mean_cost"], r["diff_lines"], r["hypothesis"]),
        )
        winner = ranked[0]["hypothesis"]
        tie = len(ranked) > 1 and ranked[0]["pass_rate"] == ranked[1]["pass_rate"]
        case.put(
            "experiment",
            sha.removeprefix("sha256:")[:16] + "-result",
            {
                "sha": sha,
                "results": results,
                "winner": winner,
                "decided_by": ("tie on pass rate, then cost" if tie else "pass rate"),
            },
            by="coordinator",
        )
        return c.Decision(decision.round, winner, True, sha)


def deterministic_cards(
    report: Mapping[str, Any],
    record: Mapping[str, Any],
    prior: str | None,
    files: list[str],
    k: int,
    rnd: int,
) -> list[dict[str, Any]]:
    """Offline cards: the minimal fix, the prior, a revert of the originating change."""
    suspects = ", ".join(report.get("suspects") or ["the suspect code"])
    constraints = list(record.get("behavioral_contract") or [])
    base = {"intent_constraints": constraints, "allowed_files": files, "diff_budget": 40}
    options = [
        {
            "title": f"minimal fix in {suspects}",
            "root_cause": report.get("mechanism") or suspects,
            "approach": "change the smallest scope that removes the mechanism",
        },
        {
            "title": "apply the prior suggestion",
            "root_cause": prior or suspects,
            "approach": prior or "harden the side-effecting call",
        },
        {
            "title": "revert the originating change",
            "root_cause": suspects,
            "approach": f"revert {report.get('originating_commit')}",
        },
    ]
    if rnd:
        options = [{**o, "title": f"{o['title']} (round {rnd} refinement)"} for o in options]
    return [{**base, **option} for option in options[:k]]
