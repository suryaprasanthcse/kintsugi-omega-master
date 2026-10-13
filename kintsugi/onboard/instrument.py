"""O3: a static health card of a local checkout (Stage 10). No code runs, nothing is fetched.

The stdlib ``ast`` reads every Python file outside tests and virtualenvs:

- model clients (imports) and model calls;
- **tools**: functions decorated as tools, whether ``@tool`` (any framework's) or a tracing
  span of kind TOOL (``@span(kind="TOOL", name=…)``, neatlogs' and OpenInference's form);
- **HTTP calls**: client methods (``httpx.post``, ``session.get``, ``client.request("PUT", …)``)
  and wrapper calls that name their HTTP method as a literal argument
  (``external.call("crm", "POST", "/contacts")``); POST, PUT, PATCH and DELETE are
  side-effecting;
- **side-effecting tools**: tools whose body makes a side-effecting HTTP call (the
  ``is_side_effecting`` precondition of the motif engine, §4.4.3);
- retry wrappers, whether neatlogs tracing is initialised, tests and CI.

``needs`` lists what onboarding must add before Omega can watch the agent.
"""

from __future__ import annotations

import ast
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", "tests", "test", "build", "dist"}
MODEL_MODULES = ("anthropic", "openai", "google.generativeai", "litellm", "langchain")
HTTP_VERBS = {"get", "post", "put", "patch", "delete", "head", "options"}
HTTP_WRITES = {"POST", "PUT", "PATCH", "DELETE"}
HTTP_RECEIVERS = ("requests", "httpx", "session", "client", "http", "api")


@dataclass
class HealthCard:
    python_files: int = 0
    model_imports: list[str] = field(default_factory=list)
    model_calls: int = 0
    tools: list[str] = field(default_factory=list)
    side_effecting_tools: list[str] = field(default_factory=list)
    http_calls: list[str] = field(default_factory=list)
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


def _kwarg(call: ast.Call, key: str) -> str | None:
    for keyword in call.keywords:
        if keyword.arg == key and isinstance(keyword.value, ast.Constant):
            return str(keyword.value.value)
    return None


def tool_name(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """The tool's name when ``node`` is declared as a tool, else ``None``."""
    for decorator in node.decorator_list:
        called = _name(decorator)
        if called.split(".")[-1] == "tool":
            name = _kwarg(decorator, "name") if isinstance(decorator, ast.Call) else None
            return name or node.name
        if isinstance(decorator, ast.Call) and called.split(".")[-1] in ("span", "trace"):
            kind = (_kwarg(decorator, "kind") or "").upper()
            if kind == "TOOL":
                return _kwarg(decorator, "name") or node.name
    return None


def http_method(call: ast.Call) -> str | None:
    """The HTTP method an outgoing call uses, when the call is recognisably HTTP."""
    called = _name(call.func)
    parts = called.split(".")
    receiver = parts[-2].lower() if len(parts) >= 2 else ""
    literal = [a.value.upper() for a in call.args
               if isinstance(a, ast.Constant) and isinstance(a.value, str)
               and a.value.upper() in HTTP_WRITES | {"GET", "HEAD", "OPTIONS"}]  # fmt: skip
    if parts[-1] == "request" and literal:  # client.request("PUT", url)
        return literal[0]
    if parts[-1] in HTTP_VERBS and len(parts) >= 2 and receiver.endswith(HTTP_RECEIVERS):
        return parts[-1].upper()
    if literal and len(call.args) >= 2:  # a wrapper naming its method: call("crm", "POST", …)
        return literal[0]
    return None


def _http_in(body: ast.AST) -> list[tuple[int, str, str]]:
    found = []
    for node in ast.walk(body):
        if isinstance(node, ast.Call):
            method = http_method(node)
            if method is not None:
                found.append((node.lineno, method, _name(node.func)))
    return found


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
                tool = tool_name(node)
                if tool is not None:
                    card.tools.append(
                        f"{where}:{node.name}" + (f" ({tool})" if tool != node.name else "")
                    )
                    if any(method in HTTP_WRITES for _, method, _ in _http_in(node)):
                        card.side_effecting_tools.append(f"{where}:{node.name}")
                if "retry" in node.name.lower() or any("retry" in d.lower() for d in decorators):
                    card.retry_wrappers.append(f"{where}:{node.name}")
            elif isinstance(node, ast.Call):
                called = _name(node.func)
                if called.endswith(("messages.create", "chat.completions.create",
                                    "generate_content", "completion")):  # fmt: skip
                    card.model_calls += 1
                method = http_method(node)
                if method is not None:
                    card.http_calls.append(f"{where}:{node.lineno} {method} {called}")
                    if method in HTTP_WRITES:
                        card.side_effecting_http.append(f"{where}:{node.lineno} {called}")
    card.tests = any((root / d).is_dir() for d in ("tests", "test"))
    card.ci = (root / ".github" / "workflows").is_dir()
    if not card.neatlogs_init:
        card.needs.append("neatlogs tracing (init + a task root span per task)")
    if not card.tests:
        card.needs.append("a test suite (Omega's G3 compares pass/fail flips)")
    if not card.model_imports:
        card.needs.append("no model client found: confirm the agent's entry point")
    if not card.tools:
        card.needs.append("no tool declarations found: tool spans let receptors see tool calls")
    return card
