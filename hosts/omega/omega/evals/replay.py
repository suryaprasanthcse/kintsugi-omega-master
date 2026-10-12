"""Rebuild a replay case's repository exactly, from committed sources (Stage 12).

- ``ledger-practice-regression``: the Ledger practice fixture, then the disclosed practice
  refactor as its own commit (the Stage 6 delivery).
- ``crm-practice``: the CRM practice fixture, whose defect is latent (A-61): one commit.
- ``scout``, ``triage``: the fleet host's package as it is in this repository: one commit.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from kintsugi.sandbox import proc

OMEGA = Path(__file__).resolve().parents[4]
FIXTURES = OMEGA / "tests" / "fixtures" / "rehearsal"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "holdout")
IDENTITY = ("-c", "user.name=Surya Prasanth R", "-c", "user.email=suryaprasanthcse@gmail.com")


def _git(repo: Path, *args: str) -> str:
    return proc.run(["git", "-C", str(repo), *args], timeout_s=120, check=True).stdout.strip()


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, *IDENTITY, "commit", "-q", "--no-verify", "-m", message)


SOURCES = {
    "ledger-practice-regression": FIXTURES / "ledger_practice",
    "crm-practice": FIXTURES / "crm_practice",
    "scout": OMEGA / "hosts" / "scout",
    "triage": OMEGA / "hosts" / "triage",
}


def build(case: Mapping[str, Any], root: Path) -> Path:
    """The case's repository under ``root`` (outside every repository), HEAD = the incident."""
    kind = str(case["build"])
    if kind not in SOURCES:
        raise ValueError(f"{case['id']}: unknown build {kind!r}")
    repo = Path(root) / str(case["id"])
    shutil.copytree(SOURCES[kind], repo, ignore=IGNORE)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "core.autocrlf", "false")
    _commit(repo, f"replay: {kind}")
    if kind == "ledger-practice-regression":
        shutil.copyfile(FIXTURES / "refactor" / "reconcile.py",
                        repo / "practice_ledger" / "reconcile.py")  # fmt: skip
        _commit(repo, "refactor(reconcile): name the retried pass and document it")
    return repo
