"""The onboarding desk (Stage 10, spec §5.9.4): the ``onboard_step`` activity's real handler.

The ``Onboarding`` workflow runs O1–O7 as seven ``onboard_step(host, step)`` activities. Each
activity may run in a different worker process, so a host's onboarding is a **case folder**
(``<root>/<host>/``, outside every repository): ``case.json`` (registered once: the intake form,
the checkout, the maintainers' trace export, the package and its side-effecting tools) and one
``<step>.json`` per finished step, which later steps read.

The steps (offline; nothing leaves the machine):

- **O1 Intake**: parses the Clinic form strictly; every consent box; foreign → PR-only.
- **O2 Sandbox**: copies the checkout into the case's sandbox (never the maintainers'
  repository), lists its dependency manifests and digests the tree; dependencies are installed
  only by an ``installer`` port (the gVisor sandbox runner in live mode).
- **O3 Instrument**: the static health card; when neatlogs is not initialised, a deterministic
  instrumentation patch (init before any model import) is applied to the **sandbox copy only**
  and kept as ``instrument.patch``.
- **O4 Entire**: ``entire graph snapshot`` of the sandbox: files, symbols, relations (skipped,
  and saying so, without entire-graph).
- **O5 Eval synthesis**: seed evals from the exported traces' failures, the checkout's existing
  tests, README examples and doctest docstrings; a deterministic 70/30 train/holdout split and
  the holdout manifest's sha256.
- **O6 Baseline**: pass rate with its Wilson interval, cost and p95 duration of the exported
  traces (replay mode: they are read like live traces).
- **O7 Immune scan**: every ACTIVE antibody motif against the sandbox's graph; the receipt's
  shipping terms (PR-only on a foreign repository, never a merge).
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import shutil
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from kintsugi.control import contracts as c
from kintsugi.memory import graph
from kintsugi.memory.library import AntibodyLibrary
from kintsugi.onboard import eval_synth, immune_scan, instrument, intake, replay_mode

STEPS = ("O1", "O2", "O3", "O4", "O5", "O6", "O7")
NAMES = {"O1": "intake", "O2": "sandbox", "O3": "instrument", "O4": "entire",
         "O5": "evals", "O6": "baseline", "O7": "immune_scan"}  # fmt: skip
MANIFESTS = ("pyproject.toml", "requirements.txt", "requirements-dev.txt", "setup.py",
             "setup.cfg", "Pipfile", "uv.lock", "poetry.lock")  # fmt: skip
CACHES = {"__pycache__", ".ruff_cache", ".pytest_cache", ".mypy_cache", ".git", ".venv"}
HOLDOUT_SHARE = 0.3


class OnboardingError(RuntimeError):
    pass


@dataclass(frozen=True)
class OnboardingCase:
    host: str
    form_body: str
    checkout: str  # the maintainers' code, read only
    exports: str | None = None  # their exported traces (replay mode)
    package_dir: str = "."
    side_effect_tools: tuple[str, ...] = ()

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> OnboardingCase:
        return cls(str(data["host"]), str(data["form_body"]), str(data["checkout"]),
                   data.get("exports"), str(data.get("package_dir", ".")),
                   tuple(data.get("side_effect_tools") or ()))  # fmt: skip


def tree_digest(folder: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in Path(folder).rglob("*") if p.is_file()
                       and not CACHES.intersection(p.relative_to(folder).parts)):  # fmt: skip
        digest.update(path.relative_to(folder).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()


def instrumentation_patch(source: str, workflow: str) -> str:
    """The neatlogs init block for an entry point, placed before its first import (§4.1.1)."""
    block = ("import neatlogs  # added by KINTSUGI-Omega onboarding (O3), sandbox copy only\n\n"
             f'neatlogs.init(workflow_name="{workflow}", instrumentations=["anthropic", '
             '"openai"])  # before any model import\n')  # fmt: skip
    lines = source.splitlines(keepends=True)
    at = 0
    try:
        tree = ast.parse(source)
        if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(
                getattr(tree.body[0], "value", None), ast.Constant):  # fmt: skip
            at = tree.body[0].end_lineno or 0  # after the module docstring
        future = [n for n in tree.body if isinstance(n, ast.ImportFrom)
                  and n.module == "__future__"]  # fmt: skip
        if future:
            at = max(at, max(n.end_lineno or 0 for n in future))
    except SyntaxError:
        pass
    return "".join(lines[:at]) + ("\n" if at else "") + block + "\n" + "".join(lines[at:])


def _tests(root: Path) -> list[dict[str, Any]]:
    cases = []
    for path in sorted(root.rglob("test*.py")):
        if CACHES.intersection(path.relative_to(root).parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        where = path.relative_to(root).as_posix()
        for node in tree.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and \
                    node.name.startswith("test"):  # fmt: skip
                cases.append({"id": f"test:{where}::{node.name}", "kind": "existing-test",
                              "source": where, "input_preview": node.name})  # fmt: skip
    return cases


def _readme(root: Path) -> list[dict[str, Any]]:
    cases = []
    for path in sorted(root.glob("README*")):
        text = path.read_text(encoding="utf-8", errors="replace")
        blocks = re.findall(r"```(?:python|py)\n(.*?)```", text, re.S)
        for n, block in enumerate(blocks, 1):
            cases.append({"id": f"readme:{path.name}#{n}", "kind": "readme-example",
                          "source": path.name, "input_preview": block.strip()[:2000]})  # fmt: skip
    return cases


def _docstrings(root: Path) -> list[dict[str, Any]]:
    cases = []
    for path in instrument._files(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        where = path.relative_to(root).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                continue
            doc = ast.get_docstring(node) or ""
            examples = [line.strip()[4:] for line in doc.splitlines()
                        if line.strip().startswith(">>> ")]  # fmt: skip
            if examples:
                cases.append(
                    {
                        "id": f"doc:{where}::{node.name}",
                        "kind": "docstring-example",
                        "source": where,
                        "input_preview": "\n".join(examples)[:2000],
                    }
                )
    return cases


def split(cases: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Deterministic train/holdout split by the case id's digest (30% holdout)."""
    train, holdout = [], []
    for case in cases:
        draw = int(hashlib.sha256(str(case["id"]).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        (holdout if draw < HOLDOUT_SHARE else train).append(dict(case))
    return train, holdout


def manifest_sha(cases: Iterable[Mapping[str, Any]]) -> str:
    from kintsugi.proof.canonical import canonicalize

    return "sha256:" + hashlib.sha256(canonicalize(sorted(str(c["id"]) for c in cases))).hexdigest()


@dataclass
class OnboardingDesk:
    """O1–O7 for registered hosts; ``step`` is the ``onboard_step`` activity's body."""

    root: Path  # case folders, outside every repository
    library: AntibodyLibrary
    installer: Callable[[Path, list[str]], dict[str, Any]] | None = None  # live: sandbox runner
    _steps: dict[str, Callable[[OnboardingCase, Path], dict[str, Any]]] = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self._steps = {"O1": self._intake, "O2": self._sandbox, "O3": self._instrument,
                       "O4": self._entire, "O5": self._evals, "O6": self._baseline,
                       "O7": self._immune_scan}  # fmt: skip

    # ---- the case folder ----------------------------------------------------------------------

    def folder(self, host: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", host):
            raise OnboardingError(f"{host!r} is not a host id")
        return self.root / host

    def register(self, case: OnboardingCase) -> Path:
        folder = self.folder(case.host)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "case.json").write_text(json.dumps(asdict(case), indent=1, sort_keys=True)
                                          + "\n", encoding="utf-8")  # fmt: skip
        return folder

    def case(self, host: str) -> OnboardingCase:
        path = self.folder(host) / "case.json"
        if not path.is_file():
            raise OnboardingError(f"no onboarding case is registered for {host}")
        return OnboardingCase.from_json(json.loads(path.read_text(encoding="utf-8")))

    def result(self, host: str, step: str) -> dict[str, Any]:
        path = self.folder(host) / f"{step}.json"
        if not path.is_file():
            raise OnboardingError(f"{host}: {step} has not run")
        return dict(json.loads(path.read_text(encoding="utf-8")))

    def step(self, host: str, step: str) -> c.StepResult:
        if step not in self._steps:
            return c.StepResult(step, False, {"error": f"unknown onboarding step {step!r}"})
        try:
            case = self.case(host)
            detail = self._steps[step](case, self.folder(host))
        except (OnboardingError, intake.IntakeRefused, replay_mode.ExportError, OSError) as error:
            return c.StepResult(step, False, {"error": str(error)[:500]})
        ok = bool(detail.pop("ok", True))
        (self.folder(host) / f"{step}.json").write_text(
            json.dumps(detail, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8"
        )
        return c.StepResult(step, ok, detail)

    # ---- O1–O7 --------------------------------------------------------------------------------

    def _intake(self, case: OnboardingCase, folder: Path) -> dict[str, Any]:
        accepted = intake.parse(case.form_body)
        return {"repo": accepted.repo, "entry_point": accepted.entry_point,
                "run_command": accepted.run_command, "model_provider": accepted.model_provider,
                "foreign": accepted.foreign, "pr_only": accepted.pr_only,
                "consents": len(accepted.consents)}  # fmt: skip

    def _sandbox(self, case: OnboardingCase, folder: Path) -> dict[str, Any]:
        source = Path(case.checkout)
        if not source.is_dir():
            raise OnboardingError(f"the checkout {source} does not exist")
        sandbox = folder / "sandbox"
        if sandbox.exists():
            shutil.rmtree(sandbox)
        shutil.copytree(source, sandbox, ignore=shutil.ignore_patterns(*CACHES))
        entry = self.result(case.host, "O1")["entry_point"]
        manifests = sorted(m for m in MANIFESTS if (sandbox / m).is_file())
        detail: dict[str, Any] = {
            "sandbox": sandbox.as_posix(), "source_sha256": tree_digest(source),
            "files": sum(1 for p in sandbox.rglob("*") if p.is_file()),
            "manifests": manifests, "entry_point_found": (sandbox / entry).is_file(),
        }  # fmt: skip
        if self.installer is not None:
            detail["install"] = self.installer(sandbox, manifests)
        else:
            detail["install"] = {"status": "not run",
                                 "reason": "no sandbox runner configured (offline)"}  # fmt: skip
        detail["ok"] = detail["entry_point_found"]
        return detail

    def _instrument(self, case: OnboardingCase, folder: Path) -> dict[str, Any]:
        sandbox = Path(self.result(case.host, "O2")["sandbox"])
        card = instrument.scan(sandbox)
        detail: dict[str, Any] = {"health_card": card.as_dict(), "patched": False}
        if not card.neatlogs_init:
            entry = sandbox / self.result(case.host, "O1")["entry_point"]
            before = entry.read_text(encoding="utf-8")
            after = instrumentation_patch(before, f"host:{case.host}")
            entry.write_text(after, encoding="utf-8", newline="\n")
            import difflib

            rel = entry.relative_to(sandbox).as_posix()
            diff = "".join(difflib.unified_diff(before.splitlines(keepends=True),
                                                after.splitlines(keepends=True),
                                                f"a/{rel}", f"b/{rel}"))  # fmt: skip
            (folder / "instrument.patch").write_text(diff, encoding="utf-8", newline="\n")
            detail.update({"patched": True, "patch": (folder / "instrument.patch").as_posix(),
                           "health_card_after": instrument.scan(sandbox).as_dict()})  # fmt: skip
        return detail

    def _entire(self, case: OnboardingCase, folder: Path) -> dict[str, Any]:
        sandbox = Path(self.result(case.host, "O2")["sandbox"])
        try:
            snap = graph.snapshot(sandbox)
        except (graph.GraphError, OSError) as error:
            return {"status": "skipped", "reason": f"entire-graph unavailable: {error}"[:300]}
        return {"status": "indexed", "files": len(snap.files), "symbols": len(snap.symbols),
                "relations": len(snap.relations)}  # fmt: skip

    def _records(self, case: OnboardingCase) -> list[Any]:
        if not case.exports:
            return []
        return replay_mode.ExportedTraces(Path(case.exports)).records()

    def _evals(self, case: OnboardingCase, folder: Path) -> dict[str, Any]:
        sandbox = Path(self.result(case.host, "O2")["sandbox"])
        records = self._records(case)
        seeds = [{**s, "kind": "trace-failure"} for s in eval_synth.synthesize(records)]
        cases = seeds + _tests(sandbox) + _readme(sandbox) + _docstrings(sandbox)
        train, holdout = split(cases)
        for name, rows in (("train", train), ("holdout", holdout)):
            (folder / f"evals.{name}.jsonl").write_text(
                "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8"
            )
        kinds: dict[str, int] = {}
        for case_ in cases:
            kinds[case_["kind"]] = kinds.get(case_["kind"], 0) + 1
        return {"cases": len(cases), "kinds": dict(sorted(kinds.items())),
                "train": len(train), "holdout": len(holdout),
                "holdout_manifest": manifest_sha(holdout), "seeds": seeds}  # fmt: skip

    def _baseline(self, case: OnboardingCase, folder: Path) -> dict[str, Any]:
        records = self._records(case)
        base = eval_synth.baseline(records)
        failures = replay_mode.observations(records, case.host)
        return {**base, "replay": {"traces": len(records), "failures": len(failures)}}

    def _immune_scan(self, case: OnboardingCase, folder: Path) -> dict[str, Any]:
        sandbox = Path(self.result(case.host, "O2")["sandbox"])
        scan = immune_scan.scan(sandbox, self.library, package_dir=case.package_dir,
                                side_effect_tools=case.side_effect_tools,
                                cache=folder / "verdicts.json")  # fmt: skip
        foreign = bool(self.result(case.host, "O1")["foreign"])
        receipt = {"pr_only": foreign, "merge": not foreign,
                   "foreign_posts": "each one waits for the Architect's approval"}  # fmt: skip
        return {**scan, "receipt": receipt}
