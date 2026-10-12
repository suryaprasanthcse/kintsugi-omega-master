"""The economic cortex (Stage 9, L5): the business plan and live actuals behind every budget.

The governor's formulas (``kintsugi.econ.governor``) are pure; the cortex feeds them:

- **Plan**: ``business/plan.json`` (:class:`BusinessPlan`), hashed as RFC 8785 JSON. Every
  incident's budget plan pins that hash (``business_plan_sha``), so an audit can tell which plan
  priced it.
- **Actuals**: month-to-date repair-side spend per host from :mod:`kintsugi.econ.actuals`
  (live lines only; rehearsals never count) is the governor's ``spent_mtd``.
- **L5**: when the full-power repair does not fit the budget, :meth:`Cortex.decide` runs
  ``governor.decide``: every alternative with its cost, the reason each was rejected, and the
  choice (a cheaper treatment and an escalation, or an escalation alone). The sample size is
  never reduced: power stays at ``stats.power.POWER_FLOOR`` (0.8) and the governor escalates
  instead.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kintsugi.econ import governor
from kintsugi.econ.actuals import Actuals
from kintsugi.proof.canonical import canonicalize, sha256_hex

PLAN_FILE = Path("business") / "plan.json"
ACTUALS_FILE = Path("business") / "actuals.json"


class PlanError(ValueError):
    pass


@dataclass(frozen=True)
class HostPlan:
    tier: str
    price_usd_month: float
    c_mon_usd_month: float
    expected_remaining_incidents: float
    cost_per_run_usd: float
    cost_per_hypothesis_usd: float
    p01: float
    holdout_pairs: int
    treatments: tuple[governor.Treatment, ...] = ()
    changes: Mapping[str, Mapping[str, str]] = field(default_factory=dict)  # treatment → change


@dataclass(frozen=True)
class BusinessPlan:
    policy: Mapping[str, float]
    hosts: Mapping[str, HostPlan]
    sha: str  # sha256 of the plan's canonical JSON

    @property
    def gm_floor(self) -> float:
        return float(self.policy.get("gm_floor", governor.GM_FLOOR_DEFAULT))

    @property
    def days_in_month(self) -> float:
        return float(self.policy.get("days_in_month", 30.0))

    def host(self, host: str) -> HostPlan:
        if host not in self.hosts:
            raise PlanError(f"the business plan has no entry for host {host!r}")
        return self.hosts[host]

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> BusinessPlan:
        if data.get("version") != 1:
            raise PlanError("unsupported business plan version")
        policy = {k: float(v) for k, v in (data.get("policy") or {}).items()}
        if policy.get("power_floor", 0.8) < 0.8:
            raise PlanError("the plan may not lower power below 0.8")
        hosts: dict[str, HostPlan] = {}
        for host, h in (data.get("hosts") or {}).items():
            treatments = tuple(governor.Treatment(str(t["name"]), float(t["probe_cost_usd"]),
                                                  float(t["delta_cost_usd_month"]))
                               for t in h.get("treatments") or ())  # fmt: skip
            changes = {str(t["name"]): dict(t.get("change") or {})
                       for t in h.get("treatments") or ()}  # fmt: skip
            hosts[str(host)] = HostPlan(
                tier=str(h["tier"]), price_usd_month=float(h["price_usd_month"]),
                c_mon_usd_month=float(h["c_mon_usd_month"]),
                expected_remaining_incidents=float(h["expected_remaining_incidents"]),
                cost_per_run_usd=float(h["cost_per_run_usd"]),
                cost_per_hypothesis_usd=float(h["cost_per_hypothesis_usd"]),
                p01=float(h.get("p01", 0.02)), holdout_pairs=int(h.get("holdout_pairs", 120)),
                treatments=treatments, changes=changes,
            )  # fmt: skip
        return cls(policy, hosts, sha256_hex(canonicalize(dict(data))))

    @classmethod
    def load(cls, path: Path) -> BusinessPlan:
        return cls.from_json(json.loads(Path(path).read_text(encoding="utf-8")))


@dataclass(frozen=True)
class LiveInputs:
    """The governor's inputs for one host at one moment (the Coordinator's ``PlanInputs``
    fields, plus where they came from)."""

    gm_floor: float
    c_mon: float
    spent_mtd: float
    expected_remaining_incidents: float
    cost_per_run: float
    cost_per_hypothesis: float
    p01: float
    holdout_pairs: int
    month: str
    business_plan_sha: str


class Cortex:
    def __init__(
        self,
        plan: BusinessPlan,
        actuals: Callable[[], Actuals],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.plan = plan
        self.actuals = actuals  # read on every plan: the month-to-date is live
        self.clock = clock

    @classmethod
    def from_repo(cls, repo: Path, **kwargs: Any) -> Cortex:
        repo = Path(repo)
        return cls(BusinessPlan.load(repo / PLAN_FILE),
                   lambda: Actuals.load(repo / ACTUALS_FILE), **kwargs)  # fmt: skip

    def inputs(self, host: str) -> LiveInputs:
        h = self.plan.host(host)
        month = self.clock().strftime("%Y-%m")
        return LiveInputs(
            gm_floor=self.plan.gm_floor, c_mon=h.c_mon_usd_month,
            spent_mtd=self.actuals().month_to_date(host, month),
            expected_remaining_incidents=h.expected_remaining_incidents,
            cost_per_run=h.cost_per_run_usd, cost_per_hypothesis=h.cost_per_hypothesis_usd,
            p01=h.p01, holdout_pairs=h.holdout_pairs, month=month,
            business_plan_sha=self.plan.sha,
        )  # fmt: skip

    def days_remaining(self) -> float:
        now = self.clock()
        return max(1.0, self.plan.days_in_month - now.day + 1)

    def decide(
        self, host: str, *, budget_usd: float, n_required: int, var_usd: float,
        horizon_h: float = 24.0, k_options: tuple[int, ...] = governor.K_OPTIONS,
    ) -> governor.EconomicDecision:  # fmt: skip
        """§6.8: repair at full power if it fits, else the cheapest affordable treatment that
        keeps the margin floor (and an escalation), else an escalation."""
        h = self.plan.host(host)
        return governor.decide(
            budget_usd=budget_usd, n_required=n_required, cost_per_run=h.cost_per_run_usd,
            cost_per_hypothesis=h.cost_per_hypothesis_usd, treatments=h.treatments,
            days_remaining=self.days_remaining(), price=h.price_usd_month,
            gm_floor=self.plan.gm_floor, c_mon=h.c_mon_usd_month,
            do_nothing_loss_per_day=var_usd * 24.0 / horizon_h,
            days_in_month=self.plan.days_in_month, k_options=k_options,
        )  # fmt: skip


def decision_record(decision: governor.EconomicDecision, plan: BusinessPlan, host: str,
                    ) -> dict[str, Any]:  # fmt: skip
    """The L5 decision span: every alternative, its cost, why it lost, and the choice."""
    change = plan.host(host).changes.get(decision.treatment.name) if decision.treatment else None
    return {
        "action": str(decision.action),
        "budget_usd": round(decision.budget, 4),
        "plan": vars(decision.plan) if decision.plan else None,
        "treatment": vars(decision.treatment) if decision.treatment else None,
        "change": dict(change) if change else None,
        "alternatives": {k: round(v, 4) for k, v in decision.alternatives.items()},
        "rejected": dict(decision.rejected),
        "power_floor": 0.8,
        "business_plan_sha": plan.sha,
    }  # fmt: skip
