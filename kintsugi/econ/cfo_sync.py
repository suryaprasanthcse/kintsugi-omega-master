"""The cfo.ai model (Stage 9, spec §4.6.4): unit economics, runway, scenarios, plan vs actual.

cfo.ai's Ari MCP (spike A8) is not wired in this edition, so the sync is a file export the
Architect imports: ``business/cfo_export/`` holds one CSV per tab, each derived from
``business/plan.json`` and the actuals and nothing else:

- ``unit_economics.csv``: per host, price, C_mon, the observed primary and secondary response
  costs (c_p, c_s) with their sample sizes and phase, E[C] = q·c_s + (1 − q)·c_p at the
  observed coverage, the month's incident cost, COGS and gross margin against the floor.
- ``coverage_scenarios.csv``: the same at coverage q ∈ {0.3, 0.7, 0.9} (spec §3.5.5).
- ``scenarios.csv``: the §10 runway scenarios (Base +25%/month, Growth +40%/month, Network
  +25%/month with +0.10 coverage and 15% of incidents prevented): month-6 and month-12 hosts and
  gross margin, breakeven month, lowest cash and its month, runway, cash at the horizon
  (``kintsugi.econ.scenarios``); ``scenario_months.csv`` holds every month of each.
- ``runway.csv``: months of runway from the plan's cash and fixed costs; while either is null
  the row says so instead of inventing a number, and while ``company.placeholder`` is true the
  status says the figures are disclosed placeholders (A-73).
- ``plan_vs_actual.csv``: per host and month, the planned C_mon and repair pool against the
  actual operating cost and repair-side spend.
- ``wright.json`` beside them: the nightly learning-curve refit.

``write_export`` is what ``kintsugi econ cfo-export`` runs.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from kintsugi.econ import governor, wright
from kintsugi.econ import scenarios as scenario_engine
from kintsugi.econ.actuals import Actuals
from kintsugi.econ.cortex import BusinessPlan

SCENARIO_Q = (0.3, 0.7, 0.9)
EXPORT_DIR = Path("business") / "cfo_export"
PLACEHOLDER_STATUS = ("computed from disclosed PLACEHOLDER cash and fixed costs (A-73), "
                      "not the Architect's figures")  # fmt: skip


def _csv(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue()


def _money(x: float) -> str:
    return f"{x:.4f}"


def observed_coverage(actuals: Actuals, phase: str | None) -> float:
    units = actuals.unit_costs(phase=phase)
    total = int(units["primary_n"]) + int(units["secondary_n"])
    return int(units["secondary_n"]) / total if total else 0.0


def economics_row(plan: BusinessPlan, host: str, c_p: float, c_s: float, q: float,
                  ) -> dict[str, float]:  # fmt: skip
    h = plan.host(host)
    e_cost = governor.expected_incident_cost(q, c_s, c_p)
    incidents = h.expected_remaining_incidents
    cogs = h.c_mon_usd_month + e_cost * incidents
    return {"e_cost": e_cost, "incident_month": e_cost * incidents, "cogs": cogs,
            "gm": governor.gross_margin(h.price_usd_month, cogs)}  # fmt: skip


def unit_economics(plan: BusinessPlan, actuals: Actuals, phase: str | None) -> str:
    units = actuals.unit_costs(phase=phase)
    c_p, c_s = float(units["primary_mean_usd"]), float(units["secondary_mean_usd"])
    q = observed_coverage(actuals, phase)
    rows = []
    for host in sorted(plan.hosts):
        h = plan.host(host)
        row = economics_row(plan, host, c_p, c_s, q)
        meets = "yes" if row["gm"] >= plan.gm_floor else "no"
        rows.append([host, h.tier, _money(h.price_usd_month), _money(h.c_mon_usd_month),
                     _money(c_p), units["primary_n"], _money(c_s), units["secondary_n"],
                     phase or "all", f"{q:.3f}", _money(row["e_cost"]),
                     f"{h.expected_remaining_incidents:g}", _money(row["incident_month"]),
                     _money(row["cogs"]), f"{100 * row['gm']:.1f}",
                     f"{100 * plan.gm_floor:.1f}", meets])  # fmt: skip
    header = ("host", "tier", "price_usd_month", "c_mon_usd_month", "c_primary_usd",
              "primary_n", "c_secondary_usd", "secondary_n", "cost_phase", "coverage_q",
              "expected_incident_cost_usd", "incidents_month", "incident_cost_month_usd",
              "cogs_month_usd", "gross_margin_pct", "gm_floor_pct", "meets_floor")  # fmt: skip
    return _csv(header, rows)


def scenarios(plan: BusinessPlan, actuals: Actuals, phase: str | None) -> str:
    units = actuals.unit_costs(phase=phase)
    c_p, c_s = float(units["primary_mean_usd"]), float(units["secondary_mean_usd"])
    rows = []
    for host in sorted(plan.hosts):
        for q in SCENARIO_Q:
            row = economics_row(plan, host, c_p, c_s, q)
            rows.append([host, f"{q:.1f}", _money(row["e_cost"]), _money(row["incident_month"]),
                         _money(row["cogs"]), f"{100 * row['gm']:.1f}"])  # fmt: skip
    header = ("host", "coverage_q", "expected_incident_cost_usd", "incident_cost_month_usd",
              "cogs_month_usd", "gross_margin_pct")  # fmt: skip
    return _csv(header, rows)


def growth_scenarios(plan_data: Mapping[str, Any]) -> tuple[str, str]:
    """``scenarios.csv`` and ``scenario_months.csv`` (§10) from the plan's drivers and its
    company cash and fixed costs; without them each file says what to set."""
    company = plan_data.get("company") or {}
    cash, fixed = company.get("cash_usd"), company.get("fixed_costs_usd_month")
    block = plan_data.get("scenarios")
    header = ("scenario", "growth_pct", "q_uplift", "vaccination", "outcome_fee_usd",
              "month6_hosts", "month6_gm_pct", "month12_hosts", "month12_gm_pct",
              "breakeven_month", "lowest_cash_usd", "lowest_cash_month", "runway_months",
              "cash_at_horizon_usd", "horizon_months", "status")  # fmt: skip
    month_header = ("scenario", "month", "hosts", "coverage_q", "expected_incident_cost_usd",
                    "cogs_per_host_usd", "gross_margin_pct", "revenue_usd", "gross_profit_usd",
                    "net_burn_usd", "cash_usd")  # fmt: skip
    if not block or cash is None or fixed is None:
        missing = "the scenarios block" if not block else "company.cash_usd and fixed costs"
        row = [""] * (len(header) - 1) + [f"set {missing} in business/plan.json"]
        return _csv(header, [row]), _csv(month_header, [])
    status = PLACEHOLDER_STATUS if company.get("placeholder") else "computed"
    summaries, months = scenario_engine.run(scenario_engine.Drivers.from_json(block),
                                            float(cash), float(fixed))  # fmt: skip

    def cell(value: Any, money: bool = False) -> str:
        if value is None:
            return "none within horizon"
        if isinstance(value, float):
            return _money(value) if money else f"{value:.2f}"
        return str(value)

    rows = [[s["scenario"], cell(s["growth_pct"]), cell(s["q_uplift"]), cell(s["vaccination"]),
             cell(s["outcome_fee_usd"]), cell(s["month6_hosts"]), cell(s["month6_gm_pct"]),
             cell(s["month12_hosts"]), cell(s["month12_gm_pct"]), cell(s["breakeven_month"]),
             cell(s["lowest_cash_usd"], True), cell(s["lowest_cash_month"]),
             cell(s["runway_months"]), cell(s["cash_at_horizon_usd"], True),
             cell(s["horizon_months"]), status] for s in summaries]  # fmt: skip
    monthly = [[m["scenario"], m["month"], m["hosts"], f"{m['coverage_q']:.4f}",
                _money(m["expected_incident_cost_usd"]), _money(m["cogs_per_host_usd"]),
                f"{m['gross_margin_pct']:.2f}", _money(m["revenue_usd"]),
                _money(m["gross_profit_usd"]), _money(m["net_burn_usd"]),
                _money(m["cash_usd"])] for m in months]  # fmt: skip
    return _csv(header, rows), _csv(month_header, monthly)


def runway(plan_data: Mapping[str, Any], plan: BusinessPlan, actuals: Actuals,
           phase: str | None) -> str:  # fmt: skip
    company = plan_data.get("company") or {}
    cash, fixed = company.get("cash_usd"), company.get("fixed_costs_usd_month")
    header = ("cash_usd", "fixed_costs_usd_month", "revenue_usd_month", "cogs_usd_month",
              "net_burn_usd_month", "runway_months", "status")  # fmt: skip
    units = actuals.unit_costs(phase=phase)
    c_p, c_s = float(units["primary_mean_usd"]), float(units["secondary_mean_usd"])
    q = observed_coverage(actuals, phase)
    revenue = sum(plan.host(h).price_usd_month for h in plan.hosts)
    cogs = sum(economics_row(plan, h, c_p, c_s, q)["cogs"] for h in plan.hosts)
    if cash is None or fixed is None:
        return _csv(header, [["", "", _money(revenue), _money(cogs), "", "",
                              "set company.cash_usd and company.fixed_costs_usd_month in "
                              "business/plan.json"]])  # fmt: skip
    burn = float(fixed) + cogs - revenue
    months = "infinite" if burn <= 0 else f"{float(cash) / burn:.1f}"
    status = PLACEHOLDER_STATUS if company.get("placeholder") else "computed"
    return _csv(header, [[_money(float(cash)), _money(float(fixed)), _money(revenue),
                          _money(cogs), _money(burn), months, status]])  # fmt: skip


def plan_vs_actual(plan: BusinessPlan, actuals: Actuals) -> str:
    months = sorted({s.month for s in actuals.spends})
    rows = []
    for month in months:
        for host in sorted(plan.hosts):
            h = plan.host(host)
            planned_pool = governor.pool(h.price_usd_month, plan.gm_floor, h.c_mon_usd_month)
            rehearsal = actuals.total(host=host, month=month, phase="rehearsal")
            spent = actuals.month_to_date(host, month)
            rows.append([month, host, _money(h.c_mon_usd_month),
                         _money(actuals.operating_cost(host, month)), _money(planned_pool),
                         _money(spent), _money(rehearsal)])  # fmt: skip
    header = ("month", "host", "planned_c_mon_usd", "actual_operating_usd",
              "planned_repair_pool_usd", "actual_repair_spend_usd",
              "rehearsal_spend_usd")  # fmt: skip
    return _csv(header, rows)


def learning_curve(actuals: Actuals) -> dict[str, Any]:
    """Every response's cost in the order it happened (date, then source run)."""
    lines = sorted(actuals.select(phase=None, kinds=("repair", "vaccination")),
                   key=lambda s: (s.date, s.source, s.ref))  # fmt: skip
    found = wright.refit([s.usd for s in lines])
    found["refs"] = [s.ref for s in lines]
    found["phases"] = sorted({s.phase for s in lines})
    return found


def export(plan_path: Path, actuals: Actuals) -> dict[str, str]:
    """The export's files and their contents (paths relative to ``business/cfo_export``)."""
    data = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    plan = BusinessPlan.from_json(data)
    live = bool(actuals.select(kinds=("repair", "vaccination")))
    phase = "live" if live else None  # until W runs, the rehearsals are the only observations
    meta = {"business_plan_sha": plan.sha, "cost_phase": phase or "all (rehearsal only)",
            "observed_coverage_q": round(observed_coverage(actuals, phase), 6),
            "unit_costs": actuals.unit_costs(phase=phase)}  # fmt: skip
    growth = growth_scenarios(data)
    return {
        "unit_economics.csv": unit_economics(plan, actuals, phase),
        "coverage_scenarios.csv": scenarios(plan, actuals, phase),
        "scenarios.csv": growth[0],
        "scenario_months.csv": growth[1],
        "runway.csv": runway(data, plan, actuals, phase),
        "plan_vs_actual.csv": plan_vs_actual(plan, actuals),
        "wright.json": json.dumps(learning_curve(actuals), indent=1, sort_keys=True) + "\n",
        "meta.json": json.dumps(meta, indent=1, sort_keys=True) + "\n",
    }


def write_export(repo: Path, actuals: Actuals, out: Path | None = None) -> list[Path]:
    folder = out or Path(repo) / EXPORT_DIR
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in export(Path(repo) / "business" / "plan.json", actuals).items():
        (folder / name).write_text(text, encoding="utf-8", newline="\n")
        written.append(folder / name)
    return written
