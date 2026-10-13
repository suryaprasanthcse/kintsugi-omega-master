"""The cfo.ai export and the nightly Wright refit (Stage 9)."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

from kintsugi.cli import main
from kintsugi.econ import cfo_sync, wright
from kintsugi.econ.actuals import Actuals, Spend
from kintsugi.econ.cortex import BusinessPlan

REPO = Path(__file__).resolve().parents[2]
PLAN = REPO / "business" / "plan.json"
EXPORT = REPO / "business" / "cfo_export"


def _rows(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


def test_the_committed_export_is_what_the_plan_and_actuals_give() -> None:
    files = cfo_sync.export(PLAN, Actuals.load(REPO / "business" / "actuals.json"))
    assert sorted(files) == sorted(p.name for p in EXPORT.iterdir())
    for name, text in files.items():
        assert (EXPORT / name).read_text(encoding="utf-8") == text, name
    meta = json.loads(files["meta.json"])
    assert meta["business_plan_sha"] == BusinessPlan.load(PLAN).sha
    assert meta["cost_phase"] == "all (rehearsal only)"  # no live spend before W
    triage = next(r for r in _rows(files["unit_economics.csv"]) if r["host"] == "triage")
    assert triage["meets_floor"] == "yes"
    assert float(triage["gross_margin_pct"]) >= 70.0
    assert len(_rows(files["coverage_scenarios.csv"])) == 3 * len(cfo_sync.SCENARIO_Q)
    growth = {r["scenario"]: r for r in _rows(files["scenarios.csv"])}
    assert sorted(growth) == ["Base", "Growth", "Network"]
    assert all(r["status"] == cfo_sync.PLACEHOLDER_STATUS for r in growth.values())
    assert growth["Base"]["month12_hosts"] == "116"
    assert len(_rows(files["scenario_months.csv"])) == 3 * 24
    runway = _rows(files["runway.csv"])[0]
    assert runway["status"] == cfo_sync.PLACEHOLDER_STATUS  # A-73: disclosed, not the Architect's
    burn = 18000.0 + float(runway["cogs_usd_month"]) - 697.0
    assert float(runway["net_burn_usd_month"]) == pytest.approx(burn, abs=1e-3)
    assert float(runway["runway_months"]) == pytest.approx(250000.0 / burn, abs=0.05)


def test_the_committed_plan_labels_its_company_figures_as_placeholders() -> None:
    company = json.loads(PLAN.read_text(encoding="utf-8"))["company"]
    assert company["placeholder"] is True
    assert "PLACEHOLDER (A-73)" in company["note"]


def test_runway_without_cash_or_fixed_costs_says_so() -> None:
    data = json.loads(PLAN.read_text(encoding="utf-8"))
    data["company"] = {"cash_usd": None, "fixed_costs_usd_month": None}
    plan = BusinessPlan.from_json(data)
    actuals = Actuals.load(REPO / "business" / "actuals.json")
    row = _rows(cfo_sync.runway(data, plan, actuals, None))[0]
    assert "set company.cash_usd" in row["status"]
    assert row["runway_months"] == ""


def test_runway_is_computed_only_from_the_architects_numbers() -> None:
    data = json.loads(PLAN.read_text(encoding="utf-8"))
    data["company"] = {"cash_usd": 10000.0, "fixed_costs_usd_month": 1000.0}
    plan = BusinessPlan.from_json(data)
    actuals = Actuals.load(REPO / "business" / "actuals.json")
    row = _rows(cfo_sync.runway(data, plan, actuals, None))[0]
    burn = 1000.0 + float(row["cogs_usd_month"]) - 697.0
    assert row["status"] == "computed"
    assert float(row["runway_months"]) == pytest.approx(10000.0 / burn, abs=0.05)
    data["company"]["fixed_costs_usd_month"] = 0.0
    assert _rows(cfo_sync.runway(data, plan, actuals, None))[0]["runway_months"] == "infinite"


def test_live_spend_replaces_the_rehearsal_unit_costs() -> None:
    live = Actuals.load(REPO / "business" / "actuals.json").add(
        Spend("2026-10-20", "triage", "repair", 2.0, "INC-0001", "case"),
        Spend("2026-10-21", "scout", "vaccination", 0.2, "VAC-AB-2-scout", "case"),
    )
    meta = json.loads(cfo_sync.export(PLAN, live)["meta.json"])
    assert meta["cost_phase"] == "live"
    assert meta["unit_costs"]["primary_mean_usd"] == 2.0
    assert meta["observed_coverage_q"] == 0.5


def test_the_refit_needs_three_points_and_recovers_the_exponent() -> None:
    assert wright.refit([1.0, 0.5])["status"] == "insufficient"
    costs = [wright.predict(n, 10.0, 0.4) for n in range(1, 9)]
    found = wright.refit(costs)
    assert found["status"] == "fitted"
    assert found["b"] == pytest.approx(0.4, abs=1e-6)
    assert found["progress_ratio"] == pytest.approx(2**-0.4, abs=1e-6)


def test_the_econ_commands(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                           capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    monkeypatch.chdir(REPO)
    assert main(["econ", "refit"]) == 0
    assert json.loads(capsys.readouterr().out)["n_points"] == 4
    assert main(["econ", "cfo-export", "--out", str(tmp_path / "cfo")]) == 0
    assert (tmp_path / "cfo" / "unit_economics.csv").read_text(encoding="utf-8") == (
        EXPORT / "unit_economics.csv").read_text(encoding="utf-8")  # fmt: skip
    capsys.readouterr()
    assert main(["econ", "actuals"]) == 0
    assert json.loads(capsys.readouterr().out)["rehearsal"]["secondary_n"] == 2
