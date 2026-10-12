"""The self-repair rehearsal (Stage 12, INC-SELF-01): before/after traces, C14, the refusal."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kintsugi.crew import self_repair_rehearsal as sr
from kintsugi.sandbox import proc

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def summary(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    if proc.which("git") is None:
        pytest.skip("git is not installed")
    root = tmp_path_factory.mktemp("self-repair")
    return sr.run(sr.SelfRepairRehearsal("self-test", root / "run", log=lambda s: None))


def test_the_self_repair_commits_as_self_repair_and_passes_c14(summary: dict[str, Any]) -> None:
    assert summary["commit"]["role"] == ["self-repair/H1"]
    assert summary["commit"]["paths"] == ["kintsugi/crew/archivist.py"]
    assert summary["commit"]["merge_sha"]
    assert summary["c14"]["status"] == "PASS"


def test_a_self_repair_on_the_audit_is_refused_before_it_exists(summary: dict[str, Any]) -> None:
    attempt = summary["forbidden_attempt"]
    assert attempt["refused"] is True
    assert "kintsugi/proof/**" in attempt["reason"]


def test_the_after_trace_set_is_no_worse_and_better_with_the_graph(summary: dict[str, Any]) -> None:
    before, after = summary["before"]["summary"], summary["after"]["summary"]
    assert len(summary["before"]["grades"]) == len(summary["after"]["grades"]) == 4
    assert after["hit_at_3"] >= before["hit_at_3"]
    if before["methods"] == ["entire-graph"]:  # where entire-graph is installed (not in CI)
        assert (before["hit_at_3"], after["hit_at_3"]) == (0.25, 0.75)
    suspects = [s for g in summary["after"]["grades"] for s in g["suspects"]]
    if after["methods"] == ["entire-graph"]:
        assert all("/evals/" not in s["file"] and "/graders/" not in s["file"] for s in suspects)


def test_the_main_branch_is_untouched_by_the_rehearsal() -> None:
    text = (REPO / "kintsugi" / "crew" / "archivist.py").read_text(encoding="utf-8")
    assert "def rank_repairable" not in text  # the patch lives in the rehearsal fixture only
    assert sr.PATCH.is_file()
