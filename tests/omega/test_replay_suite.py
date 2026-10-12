"""hosts/omega (Stage 12): the replay suite of recorded incidents and its root-cause grader."""

from __future__ import annotations

from pathlib import Path

import pytest
from omega import agent
from omega.evals import cases, replay
from omega.evals.run import run_suite
from omega.graders import grade

from kintsugi.memory import graph
from kintsugi.sandbox import proc

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    if proc.which("git") is None:
        pytest.skip("git is not installed")
    root = tmp_path_factory.mktemp("replay")
    return {str(case["id"]): replay.build(case, root) for case in cases.CASES}


def test_the_suite_holds_only_recorded_incidents() -> None:
    ids = [c["id"] for c in cases.CASES]
    assert len(ids) == len(set(ids)) == 4  # of the 20 the plan asks for; never invented
    assert cases.TARGET_SIZE == 20
    for case in cases.CASES:
        assert (REPO / str(case["evidence"])).is_file(), case["id"]
        assert case["symptom_source"]
        assert case["stage"] in (6, 7, 8, 9)


def test_every_ground_truth_symbol_exists_in_its_rebuilt_repository(built: dict[str, Path]) -> None:
    for case in cases.CASES:
        repo = built[str(case["id"])]
        for truth in case["truth"]:
            assert grade.symbol_span(repo, truth["file"], truth["symbol"]), (case["id"], truth)


def test_the_stage_6_case_is_the_delivered_regression(built: dict[str, Path]) -> None:
    repo = built["INC-R235459-0001"]
    changed = proc.run(["git", "-C", str(repo), "diff", "--name-only", "HEAD~1", "HEAD"],
                       timeout_s=30, check=True).stdout.split()  # fmt: skip
    assert changed == ["practice_ledger/reconcile.py"]


def test_the_grader_counts_overlaps_and_names() -> None:
    repo = REPO / "hosts"
    truth = [{"file": "scout/scout/research.py", "symbol": "fetch_all_pages"}]
    span = grade.symbol_span(repo, truth[0]["file"], "fetch_all_pages")
    assert span is not None
    inside = {"file": truth[0]["file"], "start": span[0] + 1, "end": span[0] + 2, "symbol": None}
    named = {"file": truth[0]["file"], "start": 1, "end": 1, "symbol": "research.fetch_all_pages"}
    elsewhere = {"file": truth[0]["file"], "start": 1, "end": 2, "symbol": "Passage"}
    assert grade.hit(repo, truth, inside)
    assert grade.hit(repo, truth, named)
    assert not grade.hit(repo, truth, elsewhere)
    assert grade.p95([1.0, 2.0, 3.0, 10.0]) == 10.0
    assert grade.p95([]) == 0.0
    result = {"suspects": [elsewhere, inside], "method": "m", "cost_usd": 0.0, "seconds": 0.1}
    graded = grade.grade(repo, {"id": "X", "truth": truth}, result)
    assert (graded["hit_at_3"], graded["rank"]) == (True, 2)


def test_the_git_only_fallback_finds_only_the_regression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without entire-graph, localisation sees recent commits only: the Stage 6 regression is
    found, the latent defects (one-commit repositories) are not."""

    def unavailable(*args: object, **kwargs: object) -> object:
        raise graph.GraphError("entire-graph is not installed here")

    monkeypatch.setattr(graph, "search", unavailable)
    report = run_suite(tmp_path)
    assert report["summary"]["methods"] == ["git-recent"]
    by_id = {g["id"]: g for g in report["grades"]}
    assert by_id["INC-R235459-0001"]["hit_at_3"]
    assert report["summary"]["hit_at_3"] == 0.25
    assert report["recorded_cases"] == 4
    assert agent.TOP == 3
