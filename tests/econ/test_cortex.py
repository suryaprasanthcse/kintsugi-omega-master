"""The economic cortex (Stage 9, L5): plan + live actuals → budget; §6.8 through the crew."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kintsugi.control import contracts as c
from kintsugi.crew.context import CrewContext
from kintsugi.crew.coordinator import Coordinator
from kintsugi.crew.factory import FileLedger, OmegaCrew
from kintsugi.econ import governor
from kintsugi.econ.actuals import Actuals, Spend
from kintsugi.econ.cortex import BusinessPlan, Cortex, PlanError
from kintsugi.fleet.hosts import load_host
from kintsugi.gates.target import load_target
from kintsugi.proof.canonical import canonicalize, sha256_hex
from kintsugi.proof.ledger import genesis_hash

REPO = Path(__file__).resolve().parents[2]
PLAN = REPO / "business" / "plan.json"
OCT_19 = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)  # 12 days left in a 30-day month (§6.8)
SPENT = Actuals().add(
    Spend("2026-10-05", "triage", "repair", 9.20, "INC-0007", "case"),  # §6.8 month to date
    Spend("2026-10-06", "triage", "repair", 50.0, "INC-REH-9", "evidence", "rehearsal"),
)


def test_the_plan_mirrors_the_hosts_and_is_pinned_by_its_hash() -> None:
    plan = BusinessPlan.load(PLAN)
    assert plan.sha == sha256_hex(canonicalize(json.loads(PLAN.read_text(encoding="utf-8"))))
    assert set(plan.hosts) == {"ledger", "scout", "triage"}
    for host, entry in plan.hosts.items():
        econ = load_host(REPO / "hosts" / host / "host.yaml").economics
        assert entry.price_usd_month == econ.price_usd_month
        assert entry.tier == econ.tier
        assert entry.c_mon_usd_month == econ.llm_budget_usd_month
    t1 = plan.host("triage").treatments[0]
    assert (t1.probe_cost, t1.delta_cost_per_month) == (0.9, 3.6)
    assert plan.host("triage").changes[t1.name]["to"] == "claude-sonnet-5-5"
    with pytest.raises(PlanError, match="no entry"):
        plan.host("omega")


def test_the_plan_may_not_lower_power() -> None:
    data = json.loads(PLAN.read_text(encoding="utf-8"))
    data["policy"]["power_floor"] = 0.7
    with pytest.raises(PlanError, match=r"below 0\.8"):
        BusinessPlan.from_json(data)


def test_section_6_8_through_the_cortex() -> None:
    cortex = Cortex(BusinessPlan.load(PLAN), lambda: SPENT, clock=lambda: OCT_19)
    p = cortex.inputs("triage")
    assert (p.spent_mtd, p.c_mon, p.expected_remaining_incidents) == (9.2, 6.0, 3.0)
    assert p.month == "2026-10"
    assert cortex.days_remaining() == 12
    b = governor.incident_budget(price=99, gm_floor=p.gm_floor, c_mon=p.c_mon,
                                 spent_mtd=p.spent_mtd,
                                 expected_remaining_incidents=p.expected_remaining_incidents,
                                 r_fail=0.15, lam_per_h=1.25, v_per_task=1.50)  # fmt: skip
    assert round(b.budget, 2) == 4.83
    decision = cortex.decide("triage", budget_usd=b.budget, n_required=60, var_usd=b.var,
                             k_options=(3, 2, 1))  # fmt: skip
    assert decision.action is governor.Action.TREAT_AND_ESCALATE
    assert decision.treatment is not None
    assert decision.treatment.name.startswith("T1")
    assert round(decision.alternatives[decision.treatment.name], 2) == 2.34
    assert round(decision.alternatives["repair K=1"], 2) == 8.00
    assert decision.rejected["repair with lower n"] == "power below 0.8 is forbidden by policy"


@pytest.fixture
def triage_ctx(tmp_path: Path) -> CrewContext:
    return CrewContext(state=tmp_path / "crew", targets={"triage": load_target(REPO, "triage")})


def test_an_unaffordable_repair_escalates_with_the_l5_decision_on_the_ledger(
    triage_ctx: CrewContext, tmp_path: Path
) -> None:
    cortex = Cortex(BusinessPlan.load(PLAN), lambda: SPENT, clock=lambda: OCT_19)
    sig = c.Signature("INC-0009", "triage", "sha256:" + "0" * 64, ("t-1",))
    triage_ctx.case("INC-0009").put("incident_open", "open", {"var_estimate": 6.75},
                                    by="sentinel")  # fmt: skip
    ledger = FileLedger(tmp_path / "ledger.jsonl", genesis_hash("t0", b"\0" * 32))
    crew: Any = SimpleNamespace(coordinator=Coordinator(triage_ctx, cortex=cortex),
                                ctx=triage_ctx, ledger=ledger)  # fmt: skip
    plan = OmegaCrew.do_plan_budget(crew, None, sig, c.Cause(("classify_component",)))  # type: ignore[arg-type]
    OmegaCrew.do_plan_budget(crew, None, sig, c.Cause(("classify_component",)))  # type: ignore[arg-type]
    assert plan.budget_usd == 0.0  # the workflow escalates: "no repair budget (L5)"
    stored = triage_ctx.case("INC-0009").reader("governor").get("plan", "plan")
    assert stored["business_plan_sha"] == cortex.plan.sha
    assert stored["inputs"]["spent_mtd"] == 9.2  # live actuals, rehearsals excluded
    decision = stored["economic_decision"]
    assert decision["action"] == "treat_and_escalate"
    assert decision["change"]["file"] == "hosts/triage/config/routing.yaml"
    assert decision["power_floor"] == 0.8
    assert stored["n_pairs"] >= 34  # the full-power sample, never reduced
    entries = [e for e in ledger.entries() if e.get("type") == "economics"]
    assert len(entries) == 1  # a retried step records the decision once
    assert entries[0]["plan_sha"] == plan.plan_sha
    assert entries[0]["rejected"]["repair with lower n"].startswith("power below 0.8")


def test_an_affordable_repair_keeps_the_repair_and_pins_the_plan(
    triage_ctx: CrewContext,
) -> None:
    rich = Actuals()
    cortex = Cortex(BusinessPlan.load(PLAN), lambda: rich, clock=lambda: OCT_19)
    sig = c.Signature("INC-0010", "triage", "sha256:" + "1" * 64, ("t-2",))
    triage_ctx.case("INC-0010").put("incident_open", "open", {"var_estimate": 40.0},
                                    by="sentinel")  # fmt: skip
    plan = Coordinator(triage_ctx, cortex=cortex).plan_budget(sig, c.Cause(("x",)))
    stored = triage_ctx.case("INC-0010").reader("governor").get("plan", "plan")
    assert plan.budget_usd > 0
    assert plan.k >= 1
    assert stored["economic_decision"] is None
    assert stored["business_plan_sha"] == cortex.plan.sha
