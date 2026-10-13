"""The §10 runway scenarios: the engine reproduces the blueprint's own table from its drivers."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

from kintsugi.econ import cfo_sync
from kintsugi.econ.scenarios import Drivers, Scenario, coverage, project, run, summarize

REPO = Path(__file__).resolve().parents[2]
BLUEPRINT = Drivers(
    10,
    299.0,
    18.0,
    8.0,
    14.0,
    1.6,
    24,
    scenarios=(
        Scenario("Base", 0.25),
        Scenario("Growth", 0.40),
        Scenario("Network", 0.25, 0.10, 0.15),
    ),
)

# Spec §10's table [I]: (month-6 hosts, GM %), (month-12 hosts, GM %), breakeven, lowest cash.
SECTION_10 = {
    "Base": ((31, 72.9), (116, 80.2), 10, 80_600),
    "Growth": ((54, 76.2), (405, 83.8), 7, 101_700),
    "Network": ((31, 78.9), (116, 85.1), 9, 84_600),
}  # fmt: skip


def test_the_engine_reproduces_section_10_from_its_own_drivers() -> None:
    summaries, _months = run(BLUEPRINT, 150_000.0, 14_000.0)  # §10: $150k cash, $14k/month
    for s in summaries:
        (h6, gm6), (h12, gm12), breakeven, lowest = SECTION_10[s["scenario"]]
        assert (s["month6_hosts"], s["month12_hosts"]) == (h6, h12), s["scenario"]
        assert s["month6_gm_pct"] == pytest.approx(gm6, abs=0.15), s["scenario"]
        assert s["month12_gm_pct"] == pytest.approx(gm12, abs=0.15), s["scenario"]
        assert s["breakeven_month"] == breakeven, s["scenario"]
        assert s["lowest_cash_usd"] == pytest.approx(lowest, abs=400), s["scenario"]
        assert s["runway_months"] is None  # cash stays positive, as §10 says


def test_coverage_follows_section_3_5_5() -> None:
    assert coverage(10) == pytest.approx(0.30)
    assert coverage(100) == pytest.approx(0.70)
    assert coverage(1000) == pytest.approx(0.90)
    assert coverage(5) == pytest.approx(0.30)  # flat outside the points
    assert coverage(5000) == pytest.approx(0.90)
    assert 0.30 < coverage(31) < coverage(116) < 0.90


def test_unit_economics_match_section_3_5_5_at_its_fleet_sizes() -> None:
    # §3.5.5: GM 66.5% at N=10 (q 0.30), 79.7% at N=100 (q 0.70), 86.4% at N=1,000 (q 0.90).
    for start, gm in ((10, 66.5), (100, 79.7), (1000, 86.4)):
        drivers = Drivers(start, 299.0, 18.0, 8.0, 14.0, 1.6, 1, scenarios=())
        [month] = project(drivers, Scenario("flat", 0.0), 0.0, 0.0)
        assert month["gross_margin_pct"] == pytest.approx(gm, abs=0.1), start


def test_a_business_that_never_breaks_even_reports_its_runway() -> None:
    drivers = Drivers(2, 99.0, 18.0, 8.0, 14.0, 1.6, 12, scenarios=())
    rows = project(drivers, Scenario("flat", 0.0), 20_000.0, 5_000.0)
    s = summarize(rows, 5_000.0)
    assert s["breakeven_month"] is None
    broke = next(r["month"] for r in rows if r["cash_usd"] < 0)
    assert s["runway_months"] == broke - 1
    assert s["lowest_cash_month"] == 12


def test_the_outcome_fee_and_vaccination_raise_revenue_and_lower_cogs() -> None:
    drivers = Drivers(10, 299.0, 18.0, 8.0, 14.0, 1.6, 1, scenarios=())
    [plain] = project(drivers, Scenario("plain", 0.0), 0.0, 0.0)
    [fee] = project(drivers, Scenario("fee", 0.0, vaccination=0.15, outcome_fee_usd=15.0), 0.0, 0.0)
    assert fee["revenue_usd"] == pytest.approx(10 * (299.0 + 15.0 * 8.0 * 0.15))
    assert fee["cogs_per_host_usd"] < plain["cogs_per_host_usd"]


def test_the_export_says_what_to_set_without_cash_or_drivers() -> None:
    data = json.loads((REPO / "business" / "plan.json").read_text(encoding="utf-8"))
    data["company"] = {"cash_usd": None, "fixed_costs_usd_month": None}
    summary, months = cfo_sync.growth_scenarios(data)
    [row] = list(csv.DictReader(io.StringIO(summary)))
    assert "set company.cash_usd" in row["status"]
    assert list(csv.DictReader(io.StringIO(months))) == []
    data = json.loads((REPO / "business" / "plan.json").read_text(encoding="utf-8"))
    del data["scenarios"]
    [row] = list(csv.DictReader(io.StringIO(cfo_sync.growth_scenarios(data)[0])))
    assert "scenarios block" in row["status"]


def test_the_committed_plan_drives_the_exported_scenarios() -> None:
    data = json.loads((REPO / "business" / "plan.json").read_text(encoding="utf-8"))
    drivers = Drivers.from_json(data["scenarios"])
    assert [s.name for s in drivers.scenarios] == ["Base", "Growth", "Network"]
    summaries, _ = run(
        drivers, data["company"]["cash_usd"], data["company"]["fixed_costs_usd_month"]
    )
    exported = {
        r["scenario"]: r
        for r in csv.DictReader(
            io.StringIO(
                (REPO / "business" / "cfo_export" / "scenarios.csv").read_text(encoding="utf-8")
            )
        )
    }
    for s in summaries:
        assert exported[s["scenario"]]["breakeven_month"] == str(s["breakeven_month"])
