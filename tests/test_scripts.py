"""The W runtime launcher (Stage 13a step 6): valid shell, and every command it runs exists."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

from kintsugi.cli import _build_parser
from kintsugi.crew import runtime
from kintsugi.sandbox import proc

REPO = Path(__file__).resolve().parents[1]
LAUNCHER = REPO / "scripts" / "w-runtime.sh"


def _commands(prefix: str) -> list[list[str]]:
    """``uv run <prefix> …`` invocations, continuation lines joined, variables replaced."""
    text = LAUNCHER.read_text(encoding="utf-8").replace("\\\n", " ")
    found = []
    for match in re.finditer(rf"uv run {re.escape(prefix)} ([^)\n|&]*)", text):
        found.append(shlex.split(re.sub(r"\$\{?[A-Z_]+\}?", "1", match.group(1))))
    return found


def test_the_launcher_is_valid_bash() -> None:
    bash = proc.which("bash")
    if bash is None:
        pytest.skip("bash is not installed")
    result = proc.run([bash, "-n", LAUNCHER.as_posix()], timeout_s=30)
    assert result.ok, result.stderr


def test_every_kintsugi_command_in_the_launcher_parses() -> None:
    commands = _commands("kintsugi")
    assert len(commands) >= 7
    parser = _build_parser()
    for argv in commands:
        parser.parse_args(argv)  # SystemExit on an unknown command or option


def test_the_launcher_runs_the_w_crew_factory() -> None:
    text = LAUNCHER.read_text(encoding="utf-8")
    assert "--crew kintsugi.crew.runtime:crew" in text
    assert callable(runtime.crew)
    for argv in _commands("python -m kintsugi.crew.runtime"):
        assert argv[0] in ("check", "start-anchors")
    assert "pip install" not in text
    assert "apt" not in text.split()  # it installs nothing
