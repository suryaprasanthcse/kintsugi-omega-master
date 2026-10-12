"""Stage 12: Omega's repairs of itself can never touch its own verification (spec §6.9, C14)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kintsugi.gates.protected_paths import load_policy
from kintsugi.gates.target import load_target
from kintsugi.proof import attest, audit, passphrase
from kintsugi.proof.commit import CommitRefused, orchestrator_commit
from kintsugi.sandbox import proc
from tests.helpers.checkpoints import git

# Every root the self_repair scope protects (MASTER_HANDOFF Stage 12), one file under each.
PROTECTED = ["kintsugi/proof/audit.py", "kintsugi/stats/sprt.py", "kintsugi/gates/g4.py",
             "kintsugi/sandbox/proc.py", "schemas/E1.json", ".kintsugi/protected_paths.yaml",
             ".github/workflows/omega-ci.yml", "docs/EXECUTION_PLAN.md"]  # fmt: skip
TARGET_YAML = """version: 1
hosts:
  omega:
    host_yaml: hosts/omega/host.yaml
    package_root: "."
    tests: tests
    self_repair: true
  plain:
    host_yaml: hosts/omega/host.yaml
    package_root: "."
    tests: tests
"""
HOST_YAML = """id: omega
package: omega
description: practice
model: claude-haiku-5-5
slo: {pass_rate: 0.9}
economics: {tier: Internal, price_usd_month: 0, value_per_failed_task_usd: 1.0,
            tasks_per_hour: 1.0, llm_budget_usd_month: 1.0}
eval: {splits: {train: 1, holdout: 0}, grader: x}
tools: {}
services: {}
offline_model: omega.offline:respond
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    if proc.which("git") is None:
        pytest.skip("git is not installed")
    path = tmp_path / "omega"
    proc.run(["git", "init", "-q", "-b", "main", str(path)], timeout_s=30, check=True)
    git(path, "config", "user.name", "Surya Prasanth R")
    git(path, "config", "user.email", "suryaprasanthcse@gmail.com")
    for relative, text in {".kintsugi/target.yaml": TARGET_YAML,
                           "hosts/omega/host.yaml": HOST_YAML,
                           "kintsugi/crew/archivist.py": "# archivist\n"}.items():  # fmt: skip
        (path / relative).parent.mkdir(parents=True, exist_ok=True)
        (path / relative).write_text(text, encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "base")
    return path


def test_a_self_target_commits_as_self_repair_under_both_scopes(repo: Path) -> None:
    omega, plain = load_target(repo, "omega"), load_target(repo, "plain")
    assert omega.scopes == ("surgeon", "self_repair")
    assert omega.repair_role == audit.SELF_REPAIR_ROLE
    assert plain.scopes == ("surgeon",)
    assert plain.repair_role == "surgeon"


def _attestation(repo: Path) -> attest.SessionAttestation:
    return attest.SessionAttestation(
        nonce=passphrase.new_nonce(),
        role="self-repair", incident="INC-SELF-01", worktree=".",
        base_sha=git(repo, "rev-parse", "HEAD"), prompt_template_sha="sha256:" + "0" * 64,
        t_start="2026-10-13T00:00:00+00:00", hypothesis="H1",
    )  # fmt: skip


@pytest.mark.parametrize("path", PROTECTED)
def test_a_self_repair_touching_omegas_verification_is_refused(repo: Path, path: str) -> None:
    target = load_target(repo, "omega")
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text("weakened\n", encoding="utf-8")
    with pytest.raises(CommitRefused, match="protected paths"):
        orchestrator_commit(repo, attestation=_attestation(repo),
                            signer=attest.Ed25519Signer.generate(), subject="self-repair",
                            policy=load_policy(), scopes=target.scopes, session_paths=[path],
                            require_checkpoint=False)  # fmt: skip


def test_a_self_repair_of_ordinary_crew_code_commits_and_passes_c14(repo: Path) -> None:
    target = load_target(repo, "omega")
    (repo / "kintsugi/crew/archivist.py").write_text("# archivist, improved\n", encoding="utf-8")
    outcome = orchestrator_commit(repo, attestation=_attestation(repo),
                                  signer=attest.Ed25519Signer.generate(), subject="self-repair",
                                  policy=load_policy(), scopes=target.scopes,
                                  session_paths=["kintsugi/crew/archivist.py"],
                                  require_checkpoint=False)  # fmt: skip
    message = git(repo, "log", "-1", "--format=%B", outcome.sha)
    assert attest.parse_trailers(message)["Kintsugi-Role"] == ["self-repair/H1"]
    changes = audit._self_repairs(repo, [outcome.sha])  # the audit's own reader of W's commits
    assert changes == {outcome.sha: ["kintsugi/crew/archivist.py"]}
    assert audit.c14_self_repair(changes, load_policy()).status is audit.Status.PASS


@pytest.mark.parametrize("path", PROTECTED)
def test_c14_flags_any_self_repair_commit_on_a_protected_root(path: str) -> None:
    result = audit.c14_self_repair({"a" * 40: ["kintsugi/crew/archivist.py", path]},
                                   load_policy())  # fmt: skip
    assert result.status is audit.Status.FAIL
    assert path in result.problems[0]
