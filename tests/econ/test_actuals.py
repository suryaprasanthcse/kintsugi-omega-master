"""Actuals (Stage 9): spend lines with their sources, month-to-date, the cfo.ai CSV."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from kintsugi.econ.actuals import Actuals, ActualsError, Spend, collect, from_spend_ledger

REPO = Path(__file__).resolve().parents[2]


def test_the_committed_actuals_are_what_the_evidence_records(tmp_path: Path) -> None:
    actuals = collect(REPO, spend_ledger=tmp_path / "none.sqlite")
    assert (REPO / "business" / "actuals.json").read_text(encoding="utf-8") == actuals.to_json()
    assert (REPO / "business" / "actuals.csv").read_text(encoding="utf-8") == actuals.to_csv()
    by_ref = {s.ref: s for s in actuals.spends}
    # The Stage 6 primary repair at its case-file cost (what G6 counts), dated by its own run.
    primary = by_ref["INC-R235459-0001"]
    assert (primary.usd, primary.date, primary.kind) == (0.665376, "2026-10-11", "repair")
    assert by_ref["VAC-AB-1-crm-practice"].usd == 0.08905
    assert by_ref["VAC-AB-1-scout"].host == "scout"
    assert all(s.phase == "rehearsal" for s in actuals.spends)
    units = actuals.unit_costs(phase="rehearsal")
    assert units["secondary_n"] == 2
    assert units["secondary_mean_usd"] == pytest.approx(0.096738)


def test_rehearsals_never_enter_a_live_month_to_date() -> None:
    actuals = Actuals().add(
        Spend("2026-10-11", "triage", "repair", 2.0, "INC-REH-1", "evidence", "rehearsal"),
        Spend("2026-10-12", "triage", "repair", 1.5, "INC-0001", "case"),
        Spend("2026-10-12", "triage", "treatment", 0.9, "INC-0002", "case"),
        Spend("2026-10-12", "triage", "llm", 0.4, "proxy:triage", "spend.sqlite"),
        Spend("2026-09-30", "triage", "repair", 7.0, "INC-0000", "case"),
    )
    assert actuals.month_to_date("triage", "2026-10") == 2.4
    assert actuals.operating_cost("triage", "2026-10") == 0.4
    assert actuals.month_to_date("ledger", "2026-10") == 0.0


def test_spend_lines_are_validated_and_deduplicated() -> None:
    with pytest.raises(ActualsError, match="kind"):
        Spend("2026-10-12", "triage", "lunch", 1.0, "x", "y")
    with pytest.raises(ActualsError, match="negative"):
        Spend("2026-10-12", "triage", "repair", -1.0, "x", "y")
    with pytest.raises(ValueError, match="does not match format"):
        Spend("12/10/2026", "triage", "repair", 1.0, "x", "y")
    line = Spend("2026-10-12", "triage", "repair", 1.0, "INC-1", "case")
    assert len(Actuals().add(line, line).spends) == 1
    back = Actuals.from_json(Actuals().add(line).to_json())
    assert back.spends == (line,)
    assert Actuals.load(Path("does-not-exist.json")).spends == ()


def test_host_model_spend_comes_from_the_proxy_ledger(tmp_path: Path) -> None:
    path = tmp_path / "spend.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE charges (id TEXT PRIMARY KEY, account TEXT, day TEXT, month "
                   "TEXT, model TEXT, upstream TEXT, billable INTEGER, cost_usd REAL, usage "
                   "TEXT, task TEXT, ts TEXT)")  # fmt: skip
        rows = [
            ("a", "triage", "2026-10-12", 1, 0.25),
            ("b", "triage", "2026-10-12", 1, 0.5),
            ("c", "triage", "2026-10-12", 0, 9.0),  # not billable
            ("d", "ledger", "2026-10-13", 1, 0.1),
        ]
        for rid, account, day, billable, usd in rows:
            db.execute("INSERT INTO charges VALUES (?, ?, ?, ?, 'm', 'u', ?, ?, '{}', 't', 'ts')",
                       (rid, account, day, day[:7], billable, usd))  # fmt: skip
    spends = from_spend_ledger(path)
    assert [(s.host, s.date, s.usd, s.kind) for s in spends] == [
        ("triage", "2026-10-12", 0.75, "llm"), ("ledger", "2026-10-13", 0.1, "llm")]  # fmt: skip
    assert from_spend_ledger(tmp_path / "missing.sqlite") == []
    csv_text = Actuals().add(*spends).to_csv()
    assert csv_text.splitlines()[0] == "date,host,kind,usd,ref,source,phase"
    assert json.loads(Actuals().add(*spends).to_json())["fields"][3] == "usd"
