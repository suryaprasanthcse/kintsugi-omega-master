"""The upstream lane (Stage 13a): deliver and publish through a notary, a PR and normal CI."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from kintsugi.crew.notary import Notary, NotaryRequest
from kintsugi.crew.rehearsal import refactor_diff
from kintsugi.crew.saboteur import new_file_diff
from kintsugi.crew.upstream import UpstreamConfig, UpstreamError, UpstreamLane, ci_verdict
from kintsugi.proof import attest
from tests.helpers import practice as pr
from tests.helpers.notary import FakeNotarySession

NOW = datetime(2026, 10, 13, 1, 2, tzinfo=UTC)


class FakeGitHub:
    def __init__(self, checks: list[list[dict[str, Any]]], *, merge: str | None = "m" * 40) -> None:
        self.checks = checks
        self.merge = merge
        self.created: list[tuple[str, str, str, str]] = []
        self.merged: list[tuple[int, str]] = []

    def pr_find(self, head: str) -> dict[str, Any] | None:
        return None

    def pr_create(self, base: str, head: str, title: str, body: str) -> dict[str, Any]:
        self.created.append((base, head, title, body))
        return {"number": 3, "url": "https://github.com/o/r/pull/3"}

    def pr_checks(self, number: int) -> list[dict[str, Any]]:
        return self.checks.pop(0) if len(self.checks) > 1 else self.checks[0]

    def pr_merge(self, number: int, method: str) -> str | None:
        self.merged.append((number, method))
        return self.merge


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    pr.git(origin, "init", "-q", "--bare", "-b", "main")
    p = pr.make_repo(tmp_path / "repo")
    pr.git(p.repo, "remote", "add", "origin", str(origin))
    pr.git(p.repo, "push", "-q", "origin", "main")
    return p.repo


def _lane(repo: Path, gh: FakeGitHub, **cfg: Any) -> UpstreamLane:
    notary = Notary(attest.Ed25519Signer.generate(), run=FakeNotarySession(),
                    require_checkpoint=False)  # fmt: skip
    return UpstreamLane(repo, notary, gh, UpstreamConfig(host_root=".", poll_s=0, **cfg),
                        sleep=lambda s: None, clock=lambda: NOW)  # fmt: skip


def _delivery() -> NotaryRequest:
    return NotaryRequest(
        operation="upstream-change",
        reference="ledger-practice",
        incident="UPSTREAM-ledger-practice",
        purpose="Refactor for readability. Apply this patch and run the existing tests.",
        diff=refactor_diff(),
        subject="ledger-practice: refactor for readability",
    )


def test_ci_verdict_waits_for_every_relevant_check() -> None:
    assert ci_verdict([], ()) is None
    assert ci_verdict([{"name": "lint", "bucket": "pending"}], ()) is None
    assert ci_verdict([{"name": "lint", "bucket": "pass"},
                       {"name": "g7", "bucket": "skipping"}], ()) is True  # fmt: skip
    assert ci_verdict([{"name": "lint", "bucket": "pass"},
                       {"name": "test", "bucket": "fail"}], ()) is False  # fmt: skip
    assert ci_verdict([{"name": "lint", "bucket": "pass"}], ("test",)) is None
    assert ci_verdict([{"name": "omega-ci / test", "bucket": "pass"}], ("test",)) is True


def test_deliver_commits_through_a_notary_and_merges_on_green_ci(repo: Path) -> None:
    gh = FakeGitHub([[{"name": "test", "bucket": "pending"}], [{"name": "test", "bucket": "pass"}]])
    lane = _lane(repo, gh)
    url = lane.deliver(_delivery(), "ledger-practice")
    assert url == "https://github.com/o/r/pull/3"
    base, head, title, body = gh.created[0]
    assert (base, head) == ("main", "upstream/ledger-practice-202610130102")
    assert title == "ledger-practice: refactor for readability"
    assert "Apply this patch" in body
    assert gh.merged == [(3, "merge")]  # A-51: a merge commit keeps the attested commit
    pushed = pr.git(repo, "ls-remote", "origin", f"refs/heads/{head}").split()[0]
    message = pr.git(repo, "log", "-1", "--format=%B", pushed)
    assert attest.parse_trailers(message)["Kintsugi-Role"] == ["notary/upstream-change"]
    assert pr.git(repo, "rev-parse", f"{pushed}^") == pr.git(repo, "rev-parse", "origin/main")
    assert not (repo / "wt" / "UPSTREAM-ledger-practice" / "ledger-practice-202610130102").exists()


def test_a_red_ci_never_merges(repo: Path) -> None:
    gh = FakeGitHub([[{"name": "test", "bucket": "fail"}]])
    with pytest.raises(UpstreamError, match="CI is red"):
        _lane(repo, gh).deliver(_delivery(), "ledger-practice")
    assert gh.merged == []


def test_a_ci_that_never_reports_times_out(repo: Path) -> None:
    gh = FakeGitHub([[]])
    with pytest.raises(UpstreamError, match="not green in time"):
        _lane(repo, gh, ci_timeout_s=0).deliver(_delivery(), "ledger-practice")
    assert gh.merged == []


def test_publish_lands_a_commitment_record(repo: Path) -> None:
    gh = FakeGitHub([[{"name": "test", "bucket": "pass"}]])
    request = NotaryRequest(operation="seal-commitment", reference="sabotage-log/S-90.json",
                            incident="SEAL-S-90", purpose="Publish this commitment record.",
                            diff=new_file_diff("sabotage-log/S-90.json", '{"c":"sha256:00"}'),
                            subject="sabotage-log: commitment S-90",
                            scopes=("surgeon", "eval_augment"))  # fmt: skip
    assert _lane(repo, gh).publish(request) == "m" * 40
    assert gh.created[0][1] == "upstream/seal-202610130102"
