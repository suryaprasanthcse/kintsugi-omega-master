"""O2: a static health card of a local checkout (Stage 10). No code runs, nothing is fetched.

The stdlib ``ast`` reads every Python file outside tests and virtualenvs: model clients and
calls, tool definitions, retry wrappers, side-effecting HTTP calls, whether neatlogs tracing is
initialised, plus the presence of tests and CI. ``needs`` lists what onboarding must add before
Omega can watch the agent.
"""

from __future__ import annotations

import ast
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", "tests", "test", "build", "dist"}
MODEL_MODULES = ("anthropic", "openai", "google.generativeai", "litellm", "langchain")
HTTP_WRITES = {"post", "put", "patch", "delete"}
HTTP_MODULES = {"requests", "httpx", "session", "client"}


@dataclass
class HealthCard:
    python_files: int = 0
    model_imports: list[str] = field(default_factory=list)
    model_calls: int = 0
    tools: list[str] = field(default_factory=list)
    retry_wrappers: list[str] = field(default_factory=list)
    side_effecting_http: list[str] = field(default_factory=list)
    neatlogs_init: bool = False
    tests: bool = False
    ci: bool = False
    needs: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py")
                  if not SKIP.intersection(p.relative_to(root).parts[:-1]))  # fmt: skip


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_name(node.value)}.{node.attr}"
    if isinstance(node, ast.Call):
        return _name(node.func)
    return ""


def scan(root: Path) -> HealthCard:
    root = Path(root)
    card = HealthCard()
    for path in _files(root):
        card.python_files += 1
        where = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                    [node.module or ""]  # fmt: skip
                for name in names:
                    if name.startswith(MODEL_MODULES) and name not in card.model_imports:
                        card.model_imports.append(name)
                    if name.startswith("neatlogs"):
                        card.neatlogs_init = True
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                decorators = [_name(d) for d in node.decorator_list]
                if any(d.split(".")[-1] == "tool" for d in decorators):
                    card.tools.append(f"{where}:{node.name}")
                if "retry" in node.name.lower() or any("retry" in d.lower() for d in decorators):
                    card.retry_wrappers.append(f"{where}:{node.name}")
            elif isinstance(node, ast.Call):
                called = _name(node.func)
                parts = called.split(".")
                if called.endswith(("messages.create", "chat.completions.create",
                                    "generate_content", "completion")):  # fmt: skip
                    card.model_calls += 1
                if (len(parts) >= 2 and parts[-1] in HTTP_WRITES
                        and parts[-2].lower() in HTTP_MODULES):  # fmt: skip
                    card.side_effecting_http.append(f"{where}:{node.lineno} {called}")
    card.tests = any((root / d).is_dir() for d in ("tests", "test"))
    card.ci = (root / ".github" / "workflows").is_dir()
    if not card.neatlogs_init:
        card.needs.append("neatlogs tracing (init + a task root span per task)")
    if not card.tests:
        card.needs.append("a test suite (Omega's G3 compares pass/fail flips)")
    if not card.model_imports:
        card.needs.append("no model client found: confirm the agent's entry point")
    return card
