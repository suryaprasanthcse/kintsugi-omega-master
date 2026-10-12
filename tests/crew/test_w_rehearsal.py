"""The W wiring rehearsal (Stage 13a step 7, offline): T0 → seal → deliver → anchors → audit."""

from __future__ import annotations

from pathlib import Path

import pytest

from kintsugi.crew import w_rehearsal as wr
from kintsugi.proof import attest
from kintsugi.sandbox import proc
from tests.helpers.checkpoints import git


def test_the_w_wiring_rehearsal_passes_c1_and_c6(tmp_path: Path) -> None:
    if proc.which("git") is None:
        pytest.skip("git is not installed")
    cfg = wr.WRehearsal("w-test", tmp_path / "w-test", delay_s=1.0, log=lambda line: None)
    summary = wr.run(cfg)
    assert summary["audit"]["exit_code"] == 0, summary["audit"]
    assert {k: v["status"] for k, v in summary["audit"]["results"].items()} == {
        "C1": "PASS", "C6": "PASS"}  # fmt: skip
    assert [(e["type"], e["event"]) for e in summary["ledger"]] == [
        ("genesis", None), ("sabotage", "sealed"), ("sabotage", "delivered")]  # fmt: skip
    origin = cfg.root / "origin.git"
    assert git(origin, "rev-parse", "refs/heads/ledger")
    assert git(origin, "rev-parse", f"refs/tags/{summary['anchor']}^{{commit}}") == git(
        origin, "rev-parse", "refs/heads/ledger")  # fmt: skip
    # The delivery is an attested notary commit, merged with a merge commit (A-51).
    merge = git(origin, "log", "-1", "--merges", "--format=%H", "main")
    notarised = git(origin, "rev-parse", f"{merge}^2")
    trailers = attest.parse_trailers(git(origin, "log", "-1", "--format=%B", notarised))
    assert trailers["Kintsugi-Role"] == ["notary/upstream-change"]
    assert "S-P1" not in git(origin, "log", "--format=%B", f"{merge}^1..{merge}")  # leak-free
    assert "return []" in git(origin, "show", f"main:{wr.FETCH}")
    assert (cfg.repo / "sabotage-log" / "S-P1.reveal.json").is_file()
    assert summary["model"] == "offline"
    assert "C2-C5" in summary["not_exercised"]
