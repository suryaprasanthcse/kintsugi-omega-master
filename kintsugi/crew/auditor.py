"""The Auditor: pass or fail on every gate, blind to the Surgeons (spec §5.7, §5.11).

Reads the card, the attested commit (or the orchestrator's refusal of it), the G0 record and
cassette, the intent record and cause report, and nothing a Surgeon wrote about its own work:
the case file refuses ``surgeon_report`` and ``surgeon_session`` to this role. Order:

1. **Diff check** (anti-gaming, the second half of "protected paths enforced twice"): protected
   paths, the card's allowed files, the diff budget (unless the card records the Coordinator's
   approval), no new outbound network calls.
2. **G1–G4** on clean checkouts of the base and the candidate (or, after upstream drift, the
   candidate merged onto the new main, re-verified per the L4 policy). G4 is skipped once a
   cheaper gate has failed: a discarded hypothesis does not spend holdout evaluations.
3. **G5** with ``claude-opus-5-5`` on the patch and the verified intent record only.
4. **G6** through the governor's view of the cost log.

The full evidence goes to the Coordinator (``verdict/<H>``); Surgeons get ``feedback/<H>``:
failed gates, the flipped visible tests and the replay diff, and G4 as aggregates only.
The holdout identity is read from its file inside the G4 step and never stored (I5).
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from kintsugi.control import contracts as c
from kintsugi.crew.context import CrewContext
from kintsugi.gates import antigaming, g1, g2, g3, g4, g5, g6
from kintsugi.gates.checkout import clean_checkout, diff_text, merged_checkout
from kintsugi.gates.common import GATES, GateResult, failed, passed
from kintsugi.gates.target import Target
from kintsugi.memory.graph import GraphError
from kintsugi.sandbox.cassette import Cassette

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _incident_lock(incident: str) -> threading.Lock:
    """Audits of one incident run one at a time: G4's latency bound compares wall-clock runs,
    which concurrent gate runs of sibling hypotheses would skew."""
    with _locks_guard:
        return _locks.setdefault(incident, threading.Lock())


def skipped(gate: str, why: str) -> GateResult:
    return failed(gate, f"skipped: {why}", skipped=True)


class Auditor:
    def __init__(self, ctx: CrewContext) -> None:
        self.ctx = ctx

    def audit(self, out: c.SurgeonOutcome) -> c.GateVerdict:
        with _incident_lock(out.incident_id):
            return self._audit(out)

    def _audit(self, out: c.SurgeonOutcome) -> c.GateVerdict:
        case = self.ctx.case(out.incident_id)
        view = case.reader("auditor")
        card = view.get("card", out.hypothesis)
        target = self.ctx.target(str(card["host"]))
        repro = view.get("repro", "g0")
        gates: dict[str, GateResult] = {
            "G0": passed("G0", "reproduced 3/3") if repro.get("ok") else failed("G0", "no repro")
        }
        base, head = str(card["base_sha"]), out.commit_sha
        reverify = None
        if out.diff_sha and "@" in out.diff_sha:  # L4: the candidate merged onto a moved main
            reverify = view.get("reverify", out.hypothesis, None)
        diff = None
        if head is None:
            refusal = view.get("refusal", out.hypothesis, None) or {}
            reason = f"no attested commit: {refusal.get('reason', 'the session made none')}"
            for gate in GATES[1:]:
                gates[gate] = skipped(gate, reason)
        else:
            diff = antigaming.check(
                target.repo, base, head, approved_large_diff=bool(card.get("approved_large_diff")),
                scopes=target.scopes,
            )  # fmt: skip
            outside = [p for p in diff.paths if p not in set(card.get("allowed_files") or [])]
            if outside and card.get("allowed_files"):
                problems = [] if diff.result.passed else [diff.result.summary]
                problems.append(f"edits outside the card's allowed files: {outside}")
                diff = antigaming.DiffCheck(
                    failed("diff", "; ".join(problems), **diff.result.evidence),
                    diff.paths,
                    diff.lines,
                    diff.network,
                )
            if not diff.result.passed:
                for gate in GATES[1:]:
                    gates[gate] = skipped(gate, "the diff check failed")
            else:
                gates.update(self._run_gates(case, card, target, repro, base, head, reverify))
                record = view.get("intent_record", "record", None) or {}
                report = view.get("cause", "report", None) or {}
                gates["G5"] = self._g5(case, out, card, record, report, target, base, head)
                plan = case.reader("governor").get("plan", "plan", None) or {}
                gates["G6"] = g6.within_budget(case.spent(), float(plan.get("budget_usd", 0.0)))
        verdict_ok = all(gates[g].passed for g in GATES)
        reason = "; ".join(f"{g}: {gates[g].summary}" for g in GATES if not gates[g].passed)
        if diff is not None and not diff.result.passed:
            reason = f"diff check: {diff.result.summary}"
        evidence = {
            "hypothesis": out.hypothesis,
            "commit": head,
            "base": base,
            "diff": diff.result.as_dict() if diff else None,
            "diff_lines": diff.lines if diff else 0,
            "gates": {g: gates[g].as_dict() for g in GATES},
            "g4": gates["G4"].evidence if gates["G4"].passed else {},
            "reverify": reverify,
            "passed": verdict_ok,
        }
        case.put("verdict", out.hypothesis, evidence, by="auditor")
        case.put("feedback", out.hypothesis, feedback(gates, diff), by="auditor")
        return c.GateVerdict(
            out.hypothesis,
            verdict_ok,
            {g: gates[g].passed for g in GATES},
            head,
            reason or "G0–G6 pass",
        )

    def _run_gates(
        self,
        case: Any,
        card: Mapping[str, Any],
        target: Target,
        repro: Mapping[str, Any],
        base: str,
        head: str,
        reverify: Mapping[str, Any] | None,
    ) -> dict[str, GateResult]:
        view = case.reader("auditor")
        cassette = Cassette.from_dict(view.get("cassette", "g0"))
        tasks = _tasks(target, repro.get("tasks") or [])
        results: dict[str, GateResult] = {}
        onto = str(reverify["new_main"]) if reverify else None
        with (
            clean_checkout(target.repo, onto or base) as before,
            (
                merged_checkout(target.repo, head, onto)
                if onto
                else clean_checkout(target.repo, head)
            ) as after,
        ):
            results["G1"] = g1.build_static(target, after)
            results["G2"] = g2.reproduction_fixed(
                target,
                after,
                cassette,
                tasks,
                list(repro.get("failing") or []),
                seed=int(repro["seed"]),
                split=str(repro["split"]),
            )
            results["G3"] = self._g3(target, before, after, onto or base, reverify)
            if not all(r.passed for r in results.values()):
                results["G4"] = skipped("G4", "a cheaper gate failed")
            elif reverify and not reverify.get("rerun_g4", True) and reverify.get("g4"):
                results["G4"] = passed(
                    "G4", "reused: no impact intersection (L4)", **dict(reverify["g4"])
                )
            else:
                results["G4"] = self._g4(target, before, after, onto or base)
        return results

    def _g3(
        self,
        target: Target,
        before: Path,
        after: Path,
        base: str,
        reverify: Mapping[str, Any] | None,
    ) -> GateResult:
        tests = list(reverify.get("tests") or []) if reverify else None
        baseline = self.ctx.state / "baselines" / f"{target.id}-{base[:12]}.json"
        baseline.parent.mkdir(parents=True, exist_ok=True)
        try:
            if not baseline.is_file():
                g3.record_baseline(target, before, baseline)
            return g3.no_collateral(target, after, baseline, tests=tests or None)
        except (GraphError, FileNotFoundError, OSError):  # no Entire here: same delta via pytest
            return g3.pytest_flips(
                g3.pytest_results(target, before), g3.pytest_results(target, after)
            )

    def _g4(self, target: Target, before: Path, after: Path, base: str) -> GateResult:
        if self.ctx.holdout_identity is None:
            return failed("G4", "no holdout identity is configured for the Auditor")
        identity = self.ctx.holdout_identity.read_text(encoding="ascii").strip()
        try:
            # Base and candidate run back to back per seed in every audit: a base run cached
            # from an earlier audit was measured under different machine load, which skews the
            # p95 bound (wall-clock latency) in either direction.
            return g4.held_out_efficacy(target, before, after, identity, seeds=self.ctx.g4_seeds)
        finally:
            del identity

    def _g5(
        self,
        case: Any,
        out: c.SurgeonOutcome,
        card: Mapping[str, Any],
        record: Mapping[str, Any],
        report: Mapping[str, Any],
        target: Target,
        base: str,
        head: str,
    ) -> GateResult:
        originating = report.get("originating_commit")
        origin_diff = ""
        if originating:
            origin_diff = diff_text(target.repo, f"{originating}^", str(originating))
        return g5.judge(
            self.ctx.model,
            case,
            hypothesis=out.hypothesis,
            title=str(card.get("title", "")),
            record=record,
            patch=diff_text(target.repo, base, head),
            originating=origin_diff,
        )


def feedback(gates: Mapping[str, GateResult], diff: antigaming.DiffCheck | None) -> dict[str, Any]:
    """What a Surgeon may see of a verdict: visible-test names, the replay diff, G4 aggregates."""
    out: dict[str, Any] = {"failed": [g for g in GATES if not gates[g].passed]}
    if diff is not None and not diff.result.passed:
        out["diff"] = diff.result.summary
    for gate in ("G1", "G2", "G3", "G5", "G6"):
        if not gates[gate].passed and not gates[gate].evidence.get("skipped"):
            out[gate] = gates[gate].summary
    if "flips" in gates["G3"].evidence:
        out["G3_flips"] = gates["G3"].evidence["flips"]
    if "still_failing" in gates["G2"].evidence:
        out["G2_still_failing"] = gates["G2"].evidence["still_failing"]
    g4_evidence = gates["G4"].evidence
    if "delta_pass" in g4_evidence:  # §5.11: Δpass, the confidence interval and the cost Δ only
        out["G4"] = {
            key: g4_evidence[key]
            for key in ("delta_pass", "ci_before", "ci_after", "cost_delta", "p95_delta")
        }
        out["G4_summary"] = gates["G4"].summary
    return out


def _tasks(target: Target, ids: list[str]) -> list[dict[str, Any]]:
    import importlib

    target.make_importable()
    generator = importlib.import_module(f"{target.host.package}.evals.generate")
    pool = {t["id"]: t for t in generator.tasks("train")}
    return [pool[i] for i in ids if i in pool]
