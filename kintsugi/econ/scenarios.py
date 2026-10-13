"""Runway scenarios (Stage 9, spec §10 and §3.5): Base, Growth and Network, month by month.

Each scenario grows the fleet from ``starting_hosts`` at a monthly rate and prices every month
with the §3.5 unit economics:

    hosts(m)   = round(N0 · (1 + g)^(m − 1))                        month m = 1, 2, …
    q(N)       = antibody coverage, log-linear through §3.5.5's points (10, 0.30), (100, 0.70),
                 (1000, 0.90), plus the scenario's coverage uplift (Network: +0.10), capped
    E[C](N)    = q·c_s + (1 − q)·c_p                                  (§3.5.2)
    μ_eff      = μ · (1 − v)                                          (§3.5.4, v: vaccination)
    COGS/host  = C_mon + μ_eff · E[C]
    revenue    = hosts · (P + F_prev · μ · v)                         (F_prev: outcome fee)
    gross profit = revenue − hosts · COGS/host
    cash(m)    = cash(m − 1) + gross profit − fixed costs

The **breakeven month** is the first month whose gross profit covers the fixed costs; the
**lowest cash** is the minimum month-end balance and its month; **runway** is the month cash
would first go negative (none within the horizon when it never does). Inputs come from
``business/plan.json``: the company's cash and fixed costs (placeholders while
``company.placeholder`` is true, A-73) and the ``scenarios`` block (the blueprint's [I]
figures until actuals replace them nightly, §4.6.5).
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

DEFAULT_COVERAGE = ((10.0, 0.30), (100.0, 0.70), (1000.0, 0.90))  # §3.5.5 [I]
Q_CAP = 0.99


@dataclass(frozen=True)
class Scenario:
    name: str
    growth: float  # monthly host growth, e.g. 0.25
    q_uplift: float = 0.0  # added to q(N) (Network: shared library coverage)
    vaccination: float = 0.0  # v: share of incidents prevented
    outcome_fee_usd: float = 0.0  # F_prev per prevented incident


@dataclass(frozen=True)
class Drivers:
    starting_hosts: int
    price_usd_month: float
    c_mon_usd_month: float
    incidents_per_host_month: float  # μ
    c_primary_usd: float
    c_secondary_usd: float
    horizon_months: int = 24
    coverage: tuple[tuple[float, float], ...] = DEFAULT_COVERAGE
    scenarios: tuple[Scenario, ...] = field(default_factory=tuple)

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Drivers:
        return cls(
            starting_hosts=int(data["starting_hosts"]),
            price_usd_month=float(data["price_usd_month"]),
            c_mon_usd_month=float(data["c_mon_usd_month"]),
            incidents_per_host_month=float(data["incidents_per_host_month"]),
            c_primary_usd=float(data["c_primary_usd"]),
            c_secondary_usd=float(data["c_secondary_usd"]),
            horizon_months=int(data.get("horizon_months", 24)),
            coverage=tuple(
                (float(n), float(q)) for n, q in data.get("coverage") or DEFAULT_COVERAGE
            ),
            scenarios=tuple(
                Scenario(
                    str(s["name"]),
                    float(s["growth"]),
                    float(s.get("q_uplift", 0.0)),
                    float(s.get("vaccination", 0.0)),
                    float(s.get("outcome_fee_usd", 0.0)),
                )
                for s in data["scenarios"]
            ),
        )


def coverage(hosts: float, points: Sequence[tuple[float, float]] = DEFAULT_COVERAGE) -> float:
    """q(N): log-linear interpolation between the coverage points, flat outside them."""
    pts = sorted(points)
    if hosts <= pts[0][0]:
        return pts[0][1]
    if hosts >= pts[-1][0]:
        return pts[-1][1]
    for (n0, q0), (n1, q1) in itertools.pairwise(pts):
        if n0 <= hosts <= n1:
            t = (math.log10(hosts) - math.log10(n0)) / (math.log10(n1) - math.log10(n0))
            return q0 + t * (q1 - q0)
    return pts[-1][1]  # pragma: no cover - the loop covers every interior point


def project(drivers: Drivers, scenario: Scenario, cash_usd: float, fixed_usd_month: float
            ) -> list[dict[str, Any]]:  # fmt: skip
    """The scenario month by month."""
    rows = []
    cash = cash_usd
    for month in range(1, drivers.horizon_months + 1):
        hosts = round(drivers.starting_hosts * (1.0 + scenario.growth) ** (month - 1))
        q = min(coverage(hosts, drivers.coverage) + scenario.q_uplift, Q_CAP)
        e_cost = q * drivers.c_secondary_usd + (1.0 - q) * drivers.c_primary_usd
        mu_eff = drivers.incidents_per_host_month * (1.0 - scenario.vaccination)
        cogs_host = drivers.c_mon_usd_month + mu_eff * e_cost
        fee = scenario.outcome_fee_usd * drivers.incidents_per_host_month * scenario.vaccination
        revenue = hosts * (drivers.price_usd_month + fee)
        gross = revenue - hosts * cogs_host
        cash += gross - fixed_usd_month
        rows.append({
            "scenario": scenario.name, "month": month, "hosts": hosts, "coverage_q": q,
            "expected_incident_cost_usd": e_cost, "cogs_per_host_usd": cogs_host,
            "gross_margin_pct": 100.0 * gross / revenue if revenue else 0.0,
            "revenue_usd": revenue, "gross_profit_usd": gross,
            "net_burn_usd": fixed_usd_month - gross, "cash_usd": cash,
        })  # fmt: skip
    return rows


def summarize(rows: Sequence[Mapping[str, Any]], fixed_usd_month: float) -> dict[str, Any]:
    """Breakeven month, lowest cash and its month, runway, and the month-6/12 snapshot."""
    breakeven = next((r["month"] for r in rows if r["gross_profit_usd"] >= fixed_usd_month), None)
    low = min(rows, key=lambda r: r["cash_usd"])
    broke = next((r["month"] for r in rows if r["cash_usd"] < 0), None)
    at = {r["month"]: r for r in rows}
    summary: dict[str, Any] = {
        "scenario": rows[0]["scenario"], "breakeven_month": breakeven,
        "lowest_cash_usd": low["cash_usd"], "lowest_cash_month": low["month"],
        "runway_months": broke - 1 if broke is not None else None,
        "cash_at_horizon_usd": rows[-1]["cash_usd"], "horizon_months": rows[-1]["month"],
    }  # fmt: skip
    for month in (6, 12):
        if month in at:
            summary[f"month{month}_hosts"] = at[month]["hosts"]
            summary[f"month{month}_gm_pct"] = at[month]["gross_margin_pct"]
    return summary


def run(drivers: Drivers, cash_usd: float, fixed_usd_month: float
        ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:  # fmt: skip
    """Every scenario: (summaries, monthly rows)."""
    summaries, months = [], []
    for scenario in drivers.scenarios:
        rows = project(drivers, scenario, cash_usd, fixed_usd_month)
        summaries.append({"growth_pct": 100.0 * scenario.growth, "q_uplift": scenario.q_uplift,
                          "vaccination": scenario.vaccination,
                          "outcome_fee_usd": scenario.outcome_fee_usd,
                          **summarize(rows, fixed_usd_month)})  # fmt: skip
        months.extend(rows)
    return summaries, months
