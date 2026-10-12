"""The L5 rehearsal on Triage (Stage 9, offline, disclosed): the governor's two answers.

Triage is the Pilot-tier host on Haiku (spec §6.8). Its live traffic (``live-1`` … ``live-6``,
180 issues, ~10% with mixed-language titles) runs on scratch clones of this repository through
the offline model (A-23, A-27: the Haiku stand-in reads English keywords only, larger tiers also
read the multilingual lexicon). Both scenarios run the crew's real path: ``do_plan_budget`` with
the economic cortex over ``business/plan.json``, the L5 decision span on the ledger and, when
the governor treats, the :class:`~kintsugi.crew.treatment.Treater` through a notary.

- **(a) measured**: r_fail is Triage's measured live failure rate; spend is the real actuals
  (rehearsal lines only, so month-to-date is $0); the date is today. Whatever the governor
  decides is recorded as it is.
- **(b) spec §6.8**: the spec's own inputs, disclosed as such: r_fail 0.15, $9.20 already spent
  this month, 12 days left. The governor treats and escalates; T1 is applied (one routing line,
  committed by the offline notary stand-in, merged locally) and the same live traffic is measured
  again on the treated code.

Two disclosed stand-ins: the offline model and an offline notary session (``git apply``, so the
treatment commit has no Entire checkpoint).
"""

from __future__ import annotations

import argparse
import importlib
import json
import shutil
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kintsugi.control import contracts as c
from kintsugi.control.revert import RevertConfig, Reverter
from kintsugi.crew.context import CrewContext
from kintsugi.crew.factory import FileLedger, OmegaCrew
from kintsugi.crew.notary import Notary
from kintsugi.crew.treatment import Treater
from kintsugi.crew.w_rehearsal import OfflineNotarySession
from kintsugi.econ.actuals import Actuals, Spend
from kintsugi.econ.cortex import ACTUALS_FILE, PLAN_FILE, BusinessPlan, Cortex
from kintsugi.gates.checkout import clean_checkout
from kintsugi.gates.evals import run_tasks
from kintsugi.gates.target import Target, load_target
from kintsugi.proof import attest
from kintsugi.proof.ledger import genesis_hash
from kintsugi.sandbox import proc

OMEGA = Path(__file__).resolve().parents[2]
HOST = "triage"
BATCHES = tuple(range(1, 7))
SEED = 1
SPEC_68 = {"r_fail": 0.15, "spent_mtd_usd": 9.20, "date": "2026-10-19"}


class L5Error(RuntimeError):
    pass


def _git(repo: Path, *args: str) -> str:
    return proc.run(["git", "-C", str(repo), *args], timeout_s=300, check=True).stdout.strip()


@dataclass
class L5Rehearsal:
    run: str
    root: Path  # outside every repository
    source: Path = OMEGA
    batches: tuple[int, ...] = BATCHES
    log: Callable[[str], None] = print


def clone(cfg: L5Rehearsal, name: str) -> Path:
    path = cfg.root / name
    if path.exists():
        raise L5Error(f"{path} exists: every rehearsal starts fresh")
    _git(cfg.root, "clone", "-q", "--no-hardlinks", str(cfg.source), str(path))
    for key, value in {"user.name": "Surya Prasanth R",
                       "user.email": "suryaprasanthcse@gmail.com"}.items():  # fmt: skip
        _git(path, "config", key, value)
    return path


def measure(target: Target, ref: str, batches: Sequence[int] = BATCHES) -> dict[str, Any]:
    """Triage's live traffic on ``ref``: every batch at the same seed."""
    target.make_importable()
    generate = importlib.import_module("triage.evals.generate")
    tasks = failed = mixed = 0
    failing: list[str] = []
    for batch in batches:
        split = f"live-{batch}"
        batch_tasks = generate.tasks(split)
        with clean_checkout(target.repo, ref) as checkout:
            result = run_tasks(target, checkout, batch_tasks, seed=SEED, split=split,
                               phase="rehearsal")  # fmt: skip
        tasks += len(batch_tasks)
        mixed += sum(1 for t in batch_tasks if t["meta"]["language"] != "en")
        failing += [f"{split}/{task}" for task in result.failed()]
        failed += len(result.failed())
    return {"tasks": tasks, "mixed_language": mixed, "failed": failed,
            "failure_rate": round(failed / tasks, 6), "failing": failing}  # fmt: skip


def scenario(
    cfg: L5Rehearsal, name: str, repo: Path, *, r_fail: float, actuals: Actuals,
    clock: Callable[[], datetime], signer: attest.Ed25519Signer,
) -> dict[str, Any]:  # fmt: skip
    """One incident through ``do_plan_budget`` (and the Treater when the governor treats)."""
    target = load_target(repo, HOST)
    econ = target.host.economics
    state = cfg.root / f"{name}-state"
    ctx = CrewContext(state=state / "crew", targets={HOST: target})
    cortex = Cortex(BusinessPlan.load(repo / PLAN_FILE), lambda: actuals, clock=clock)
    notary = Notary(signer, run=OfflineNotarySession(), require_checkpoint=False)
    treater = Treater(notary, Reverter(notary, RevertConfig(require_g7=False)))
    genesis = genesis_hash(f"rehearsal-{cfg.run}-{name}", signer.public_key_raw())
    ledger = FileLedger(state / "ledger.jsonl", genesis)
    crew = OmegaCrew(ctx, runner=lambda: None, ledger=ledger, cortex=cortex,  # type: ignore[arg-type, return-value]
                     treater=treater)  # fmt: skip
    incident = f"INC-L5{name.upper()}-0001"
    var = r_fail * econ.tasks_per_hour * econ.value_per_failed_task_usd * 24.0
    ctx.case(incident).put("incident_open", "open", {"var_estimate": var}, by="sentinel")
    sig = c.Signature(incident, HOST, "sha256:" + "0" * 64, ("local:l5",))
    budget = crew.do_plan_budget(None, sig, c.Cause(("classify_component",)))  # type: ignore[arg-type]
    plan = ctx.case(incident).reader("governor").get("plan", "plan")
    entries = ledger.entries()
    return {
        "incident": incident, "r_fail": round(r_fail, 6), "var_usd": plan["var"],
        "spent_mtd_usd": plan["inputs"]["spent_mtd"], "month": plan["inputs"]["month"],
        "budget_usd": budget.budget_usd, "n_pairs": plan["n_pairs"], "k": budget.k,
        "business_plan_sha": plan["business_plan_sha"], "plan_sha": budget.plan_sha,
        "decision": plan["economic_decision"],
        "ledger": [{k: e.get(k) for k in ("seq", "type", "action", "treatment", "ok", "reason")
                    if e.get(k) is not None} for e in entries],
        "treatment": next((e for e in entries if e.get("type") == "treatment"), None),
    }  # fmt: skip


def run(cfg: L5Rehearsal) -> dict[str, Any]:
    started = time.monotonic()
    cfg.root.mkdir(parents=True, exist_ok=True)
    signer = attest.Ed25519Signer.generate()  # a rehearsal key, never the T0 key (A-4)
    measured_repo = clone(cfg, "measured")
    before = measure(load_target(measured_repo, HOST), "HEAD", cfg.batches)
    cfg.log(f"[{cfg.run}] live traffic: {before['failed']}/{before['tasks']} failed")
    real = Actuals.load(measured_repo / ACTUALS_FILE)
    measured = scenario(cfg, "measured", measured_repo, r_fail=before["failure_rate"],
                        actuals=real, clock=lambda: datetime.now(UTC), signer=signer)  # fmt: skip
    cfg.log(f"[{cfg.run}] (a) measured: {measured['decision']['action']}")
    spec_repo = clone(cfg, "spec")
    spent = Actuals().add(Spend("2026-10-05", HOST, "repair", SPEC_68["spent_mtd_usd"],
                                "spec-6.8", "spec §6.8 scenario input (disclosed)"))  # fmt: skip
    day = datetime.fromisoformat(str(SPEC_68["date"])).replace(hour=9, tzinfo=UTC)
    spec = scenario(cfg, "spec", spec_repo, r_fail=float(SPEC_68["r_fail"]), actuals=spent,
                    clock=lambda: day, signer=signer)  # fmt: skip
    cfg.log(f"[{cfg.run}] (b) spec §6.8: {spec['decision']['action']}")
    after = None
    if spec["treatment"] and spec["treatment"].get("ok"):
        after = measure(load_target(spec_repo, HOST), "main", cfg.batches)
        cfg.log(f"[{cfg.run}] after T1: {after['failed']}/{after['tasks']} failed")
    routing = _git(spec_repo, "show", "main:hosts/triage/config/routing.yaml")
    return {
        "kind": "l5-rehearsal", "run": cfg.run, "phase": "rehearsal", "model": "offline",
        "host": HOST, "seed": SEED, "batches": [f"live-{b}" for b in cfg.batches],
        "disclosed": ["offline model (Haiku stand-in reads English keywords only, A-27)",
                      "offline notary stand-in (git apply; no Entire checkpoint)",
                      "scenario (b) uses spec §6.8's inputs, not measured ones"],
        "before": before, "measured": measured, "spec_6_8": {"inputs": SPEC_68, **spec},
        "after_treatment": after,
        "routing_after": [ln.strip() for ln in routing.splitlines() if ": claude-" in ln],
        "duration_s": round(time.monotonic() - started, 1),
    }  # fmt: skip


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The L5 rehearsal on Triage (offline).")
    parser.add_argument("--root", type=Path, required=True, help="scratch dir outside any repo")
    parser.add_argument("--run", default=datetime.now(UTC).strftime("l5-%Y%m%d-%H%M%S"))
    parser.add_argument("--out", type=Path)
    parser.add_argument("--keep", action="store_true", help="keep the scratch clones")
    args = parser.parse_args(argv)
    cfg = L5Rehearsal(args.run, args.root / args.run)
    summary = run(cfg)
    text = json.dumps(summary, indent=1, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8", newline="\n")
    if not args.keep:
        shutil.rmtree(cfg.root, ignore_errors=True)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
