"""L5 (Stage 9): the treatment change, and the Triage rehearsal through the crew (offline)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kintsugi.crew import l5_rehearsal as l5
from kintsugi.crew.treatment import TreatmentError, treatment_diff
from kintsugi.gates.target import load_target
from kintsugi.sandbox import proc

REPO = Path(__file__).resolve().parents[2]
T1 = {"file": "hosts/triage/config/routing.yaml", "step": "classify_component",
      "from": "claude-haiku-5-5", "to": "claude-sonnet-5-5"}  # fmt: skip


@pytest.fixture(scope="module")
def summary(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    if proc.which("git") is None:
        pytest.skip("git is not installed")
    root = tmp_path_factory.mktemp("l5")
    return l5.run(l5.L5Rehearsal("l5-test", root / "run", batches=(3, 4), log=lambda s: None))


def test_the_treatment_changes_one_routing_line_and_nothing_else() -> None:
    target = load_target(REPO, "triage")
    diff = treatment_diff(target, "HEAD", T1)
    changed = [ln for ln in diff.splitlines() if ln[:1] in "+-" and ln[:3] not in ("+++", "---")]
    assert changed == ["-  classify_component: claude-haiku-5-5",
                       "+  classify_component: claude-sonnet-5-5"]  # fmt: skip
    with pytest.raises(TreatmentError, match="expected one"):
        treatment_diff(target, "HEAD", {**T1, "from": "claude-opus-5-5"})
    with pytest.raises(TreatmentError, match="outside the host"):
        treatment_diff(target, "HEAD", {**T1, "file": "hosts/ledger/host.yaml"})


def test_spec_6_8_treats_and_escalates_and_the_treatment_helps(summary: dict) -> None:
    spec = summary["spec_6_8"]
    assert spec["budget_usd"] == 0.0  # no repair: the incident escalates
    assert spec["decision"]["action"] == "treat_and_escalate"
    assert round(spec["decision"]["budget_usd"], 2) == 4.83
    assert spec["treatment"]["ok"] is True
    assert spec["treatment"]["commit"]
    assert [e["type"] for e in spec["ledger"]] == ["economics", "treatment"]
    assert "classify_component: claude-sonnet-5-5" in summary["routing_after"]
    before, after = summary["before"], summary["after_treatment"]
    assert after is not None
    assert after["failed"] < before["failed"]  # 5 → 2 on live-3 and live-4


def test_the_measured_incident_never_lowers_power(summary: dict) -> None:
    measured = summary["measured"]
    assert measured["r_fail"] == summary["before"]["failure_rate"]
    assert measured["spent_mtd_usd"] == 0.0  # only rehearsal spend exists before W
    decision = measured["decision"]
    assert decision["action"] in ("escalate", "treat_and_escalate")  # T1's cost depends on the day
    assert decision["rejected"]["repair with lower n"] == "power below 0.8 is forbidden by policy"
    assert all(why == "exceeds budget" for name, why in decision["rejected"].items()
               if name.startswith("repair K="))  # fmt: skip
    assert decision["business_plan_sha"] == measured["business_plan_sha"]
