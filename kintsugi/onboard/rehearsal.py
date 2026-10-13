"""The OSS-1 onboarding rehearsal (Stage 10, offline, disclosed): O1–O7 on a local practice host.

The Architect has not chosen OSS-1 yet, so the rehearsal onboards the disclosed **CRM practice**
host (``tests/fixtures/rehearsal/crm_practice``, the Clinic stand-in of A-61) end to end, with
nothing leaving this machine:

1. **Checkout**: a disposable copy of the practice host outside every repository (the immune
   scan's graph and verdict cache are written there, never here).
2. **Exported traces**: the practice host runs its train tasks behind a local fabric (seeded
   chaos proxy and offline LLM proxy on 127.0.0.1) under its own disclosed stress profile; its
   spans are captured in memory and written in the neatlogs Public API's export shape
   (``trace_export``). No neatlogs project exists for it, so this stands in for the
   maintainers' export, and says so.
3. **Onboarding**: ``pipeline.onboard`` on the filled-in intake fixture
   (``tests/fixtures/rehearsal/oss1/intake.md``, all five consent boxes checked) with the
   checkout and the exports: intake, health card, replay mode, seed evals, baseline, immune
   scan, PR-only receipt.

The intake names a fictional, disclosed repository slug that is never contacted: the rehearsal
has no GitHub or neatlogs port at all, and a socket guard (in this process and in the host's)
refuses and counts every non-loopback connection. The evidence records both.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kintsugi.fleet.fabric import Fabric, FabricConfig
from kintsugi.gates.evals import tasks_jsonl
from kintsugi.gates.target import load_target
from kintsugi.memory.library import AntibodyLibrary
from kintsugi.onboard import pipeline, trace_export
from kintsugi.sandbox import proc

OMEGA = Path(__file__).resolve().parents[2]
PRACTICE = OMEGA / "tests" / "fixtures" / "rehearsal" / "crm_practice"
INTAKE = OMEGA / "tests" / "fixtures" / "rehearsal" / "oss1" / "intake.md"
HOST = "crm-practice"
SIDE_EFFECT_TOOLS = ("create_contact",)  # host.yaml: the CRM write is side-effecting


class RehearsalError(RuntimeError):
    pass


@dataclass
class OnboardRehearsal:
    run: str
    root: Path  # scratch, outside every repository
    seed: int = 1
    limit: int = 24
    log: Callable[[str], None] = field(default=print)


def _text(path: Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n")  # the same digest on any checkout's EOLs


def _sha(path: Path) -> str:
    return hashlib.sha256(_text(path)).hexdigest()


CACHES = {"__pycache__", ".ruff_cache", ".pytest_cache", ".mypy_cache", ".onboard-verdicts.json"}


def _tree_sha(folder: Path) -> str:
    """The source tree's digest; tool caches other runs leave behind (ruff, pytest) excluded."""
    digest = hashlib.sha256()
    for path in sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix != ".pyc"
                       and not CACHES.intersection(p.relative_to(folder).parts)):  # fmt: skip
        digest.update(path.relative_to(folder).as_posix().encode("utf-8") + b"\0")
        digest.update(_text(path))
    return digest.hexdigest()


def prepare_checkout(root: Path) -> Path:
    checkout = root / "checkout"
    shutil.copytree(PRACTICE, checkout, ignore=shutil.ignore_patterns("__pycache__"))
    return checkout


def export_traces(checkout: Path, out: Path, *, seed: int, limit: int) -> dict[str, Any]:
    """Run the practice host's train tasks behind a local fabric; export their traces."""
    import importlib

    target = load_target(checkout, HOST)
    target.make_importable()
    tasks = importlib.import_module(f"{target.host.package}.evals.generate").tasks("train")[:limit]
    profile = target.stress()
    if profile is None:
        raise RehearsalError(f"{HOST} declares no stress profile")
    config = FabricConfig(seed=seed, profile=profile, live_services=target.live_services)
    argv = [sys.executable, "-m", "kintsugi.onboard.trace_export", "--host", target.host.package,
            "--split", "train", "--seed", str(seed), "--phase", "rehearsal",
            "--out", str(out)]  # fmt: skip
    with Fabric({target.id: target.host}, config) as fabric:
        env = {**fabric.env(target.id), **target.code_env(checkout)}
        result = proc.run(argv, extra_env=env, input_text=tasks_jsonl(tasks), timeout_s=900)
    if result.returncode != 0:
        raise RehearsalError(f"trace export failed: {result.stderr[-2000:]}")
    lines = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    rows = [line for line in lines if "guard" not in line]
    guard = next((line["guard"] for line in lines if "guard" in line), None)
    if guard is None:
        raise RehearsalError("the trace export did not report its socket guard")
    verdicts: dict[str, int] = {}
    for row in rows:
        verdicts[str(row["verdict"])] = verdicts.get(str(row["verdict"]), 0) + 1
    return {"tasks": len(rows), "verdicts": dict(sorted(verdicts.items())),
            "traces": len([p for p in out.iterdir() if p.is_dir()]),
            "profile": target.stress_profile, "seed": seed, "model_mode": "offline",
            "host_non_loopback_refused": guard["non_loopback_refused"],
            "export_sha256": _tree_sha(out)}  # fmt: skip


def run(cfg: OnboardRehearsal) -> dict[str, Any]:
    started = time.monotonic()
    cfg.root.mkdir(parents=True, exist_ok=True)
    with trace_export.LoopbackGuard() as guard:
        checkout = prepare_checkout(cfg.root)
        exports = cfg.root / "exports"
        exported = export_traces(checkout, exports, seed=cfg.seed, limit=cfg.limit)
        cfg.log(f"[{cfg.run}] exported {exported['traces']} traces: {exported['verdicts']}")
        library = AntibodyLibrary(OMEGA / "antibodies")
        record = pipeline.onboard(INTAKE.read_text(encoding="utf-8"), checkout, exports,
                                  library, package_dir="practice_crm",
                                  side_effect_tools=SIDE_EFFECT_TOOLS)  # fmt: skip
    refused = list(guard.refused) + list(exported["host_non_loopback_refused"])
    cfg.log(f"[{cfg.run}] onboarded: replay {record['O3_replay']}, "
            f"immune scan {record['O6_immune_scan']['status']}")  # fmt: skip
    return {
        "kind": "onboarding-rehearsal", "run": cfg.run, "phase": "rehearsal",
        "oss": "OSS-1 (rehearsal stand-in: the disclosed CRM practice host)",
        "model": "offline (A-23)",
        "disclosed": [
            "OSS-1 is not chosen yet: the checkout is a disposable copy of the disclosed CRM "
            "practice host (A-61), not an open-source repository",
            "the intake names a fictional repository slug that is never contacted",
            "the exported traces are captured locally from the practice host behind a local "
            "fabric (stress profile, offline model): no neatlogs project exists for it",
        ],
        "intake": {"fixture": INTAKE.relative_to(OMEGA).as_posix(), "sha256": _sha(INTAKE)},
        "checkout": {"source": PRACTICE.relative_to(OMEGA).as_posix(),
                     "sha256": _tree_sha(PRACTICE)},
        "exports": {k: v for k, v in exported.items() if k != "host_non_loopback_refused"},
        "onboarding": record,
        "network": {"non_loopback_connections_refused": len(refused), "refused": refused,
                    "foreign_posts": 0, "ports": "none: no GitHub, neatlogs or cfo.ai client "
                                                 "is constructed by the rehearsal"},
        "duration_s": round(time.monotonic() - started, 1),
    }  # fmt: skip


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The OSS-1 onboarding rehearsal (offline).")
    parser.add_argument("--root", type=Path, help="scratch dir outside any repo (default: temp)")
    parser.add_argument("--run", default=datetime.now(UTC).strftime("oss1-%Y%m%d-%H%M%S"))
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--limit", type=int, default=24)
    parser.add_argument("--out", type=Path, help="write the evidence JSON here")
    args = parser.parse_args(argv)
    base = args.root or Path(tempfile.mkdtemp(prefix="kintsugi-oss1-"))
    cfg = OnboardRehearsal(args.run, base / args.run, seed=args.seed, limit=args.limit)
    try:
        summary = run(cfg)
    finally:
        shutil.rmtree(cfg.root, ignore_errors=True)
    text = json.dumps(summary, indent=1, sort_keys=True) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8", newline="\n")
    print(text)
    clean = summary["network"]["non_loopback_connections_refused"] == 0
    return 0 if clean and summary["onboarding"]["O7_receipt"]["pr_only"] else 1


if __name__ == "__main__":
    sys.exit(main())
