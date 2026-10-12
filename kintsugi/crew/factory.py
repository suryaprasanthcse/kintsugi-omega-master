"""The real crew behind the control plane's activities (Stage 5's ``Crew`` port).

``OmegaCrew.handle`` dispatches each activity to its role: Sentinel (``route_hit``), Pathologist
(``triage``, ``reproduce``), Archivist (``attribute``), governor and Coordinator
(``plan_budget``, ``hypothesize``, ``coordinate``, ``dispute_experiment``, ``budget_exhausted``),
Surgeons (``surgeon`` through Stage 5's ``SurgeonStep``), the Auditor (``audit_gates``), the
shipper (``ship``) and the L4 re-verification (``rebase_and_rerun``). Ledger-shaped steps
(``record_transition``, ``park``, ``escalate``, ``ledger_append``) append to a hash-chained
ledger file (``kintsugi.proof.ledger`` format) until Stage 8 moves it to the ``ledger`` branch.

Stage 7 owns antibodies, motifs, vaccination and quarantine: those steps answer honestly that
nothing exists yet (no antibody match, an empty motif scan, a placeholder antibody reference
recorded as pending). The canary is real: each batch runs the host's tasks on the merged code
under the background chaos profile.

Workers load it through ``kintsugi.crew.runtime`` (configured per deployment or rehearsal).
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kintsugi.control import contracts as c
from kintsugi.control.activities import StepContext
from kintsugi.control.revert import Reverter, RevertRecord
from kintsugi.control.surgeon_step import AttemptLog, SessionRunner, SurgeonStep
from kintsugi.crew.archivist import Archivist
from kintsugi.crew.auditor import Auditor
from kintsugi.crew.context import CrewContext
from kintsugi.crew.coordinator import Coordinator
from kintsugi.crew.immunologist import Immunologist, SynthesisError, render_recipe
from kintsugi.crew.notary import Notary, NotaryRequest
from kintsugi.crew.pathologist import Pathologist
from kintsugi.crew.saboteur import SabotageRefused, Saboteur
from kintsugi.crew.sentinel import Sentinel
from kintsugi.econ.cortex import Cortex
from kintsugi.gates import canary
from kintsugi.gates.checkout import rev
from kintsugi.memory.antibody import Antibody, AntibodyState
from kintsugi.memory.graph import GraphError
from kintsugi.memory.library import AntibodyLibrary, LibraryError
from kintsugi.proof.anchor import post_text
from kintsugi.proof.attest import Signer
from kintsugi.proof.ledger import Ledger, genesis_hash
from kintsugi.proof.ledger_branch import BranchLedger
from kintsugi.sandbox import worktree
from kintsugi.senses.drift import reverify_plan
from kintsugi.senses.fixes_mcp import Fixes, FixesUnavailable

CANARY_TASKS = 10


class FileLedger:
    """The hash-chained ledger in a JSONL file, appended under a process lock."""

    def __init__(self, path: Path, genesis: str) -> None:
        self.path = Path(path)
        self.genesis = genesis
        self._lock = threading.Lock()

    def append(self, entry: Mapping[str, Any]) -> str:
        with self._lock:
            text = self.path.read_text(encoding="utf-8") if self.path.is_file() else ""
            ledger = Ledger.from_jsonl(text, self.genesis)
            record = ledger.append(
                {"at": datetime.now(UTC).isoformat(timespec="seconds"), **dict(entry)}
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(ledger.to_jsonl(), encoding="utf-8", newline="\n")
            return str(record["hash"])

    def entries(self) -> tuple[dict[str, Any], ...]:
        text = self.path.read_text(encoding="utf-8") if self.path.is_file() else ""
        return Ledger.from_jsonl(text, self.genesis).entries


@dataclass(frozen=True)
class Anchoring:
    """Stage 8: hourly signed anchors of a ``BranchLedger`` (A-67). Omega writes the tag and the
    post text under ``out_dir``; the Architect posts the text. With ``remote`` set, the ledger
    branch and its anchor tags are pushed after each anchor (fast-forward only, Stage 13a)."""

    signer: Signer
    repo_url: str
    out_dir: Path
    remote: str | None = None


@dataclass
class SabotageLane:
    """Stage 8: the Saboteur's activities. ``identity`` is the age secret that opens the sealed
    specs; only this lane holds it. ``plans`` are specs not sealed yet (sealed before T0 by
    default). ``publish`` commits a commitment record through a notary (inside W);
    ``deliver(request, host)`` runs the delivery notary in a worktree of the host and ships it
    through the upstream lane, returning a reference."""

    saboteur: Saboteur
    identity: str
    deliver: Callable[[NotaryRequest, str], str]
    plans: Mapping[str, tuple[dict[str, Any], str | None]] = field(default_factory=dict)
    publish: Callable[[NotaryRequest], str] | None = None


class Shipper:
    """``ship`` (``kintsugi.control.ship``): issue → PR with the receipt → G7 → merge."""

    def ship(self, out: c.SurgeonOutcome) -> c.Shipment:  # pragma: no cover - protocol
        raise NotImplementedError


# A disclosed drill's stand-in for one host's vaccination (kintsugi.crew.l3): (crew, step,
# antibody, host) → the result the wave sees. Never configured outside a rehearsal.
Applier = Callable[["OmegaCrew", StepContext, Antibody, str], c.VaccinationResult]


@dataclass
class Immunity:
    """Stage 7: what the crew needs for antibodies, beyond the primary response."""

    immunologist: Immunologist
    shippers: Mapping[str, Shipper] = field(default_factory=dict)  # per host: merge or PR-only
    reverters: Mapping[str, Reverter] = field(default_factory=dict)  # per host
    notary: Notary | None = None  # eval-augment commits (trace-to-test)
    github: Any = None  # GhCli: quarantine notices on open vaccination PRs
    pr_prefixes: tuple[str, ...] = ("",)  # branch prefixes those PRs live under
    appliers: Mapping[str, Applier] = field(default_factory=dict)  # disclosed drills only

    @property
    def library(self) -> AntibodyLibrary:
        return self.immunologist.library


class OmegaCrew:
    def __init__(
        self,
        ctx: CrewContext,
        *,
        runner: Callable[[], SessionRunner],
        shipper: Shipper | None = None,
        fixes: Fixes | None = None,
        tags: Any = None,  # kintsugi.senses.tags.TagWriter for neatlogs traces
        ledger: FileLedger | BranchLedger | None = None,
        immunity: Immunity | None = None,  # None: Stage 6 behaviour (primary responses only)
        anchoring: Anchoring | None = None,
        sabotage: SabotageLane | None = None,
        cortex: Cortex | None = None,  # Stage 9: business plan + live actuals behind budgets
    ) -> None:
        self.ctx = ctx
        self.runner = runner
        self.shipper = shipper
        self.fixes = fixes
        self.tags = tags
        self.immunity = immunity
        self.ledger = ledger or FileLedger(
            ctx.state / "ledger.jsonl", genesis_hash("rehearsal", b"\0" * 32)
        )
        self.anchoring = anchoring
        self.sabotage = sabotage
        self.auditor = Auditor(ctx)
        self.coordinator = Coordinator(ctx, cortex=cortex)
        if immunity is not None and immunity.immunologist.ledger is None:
            immunity.immunologist.ledger = self.ledger

    def handle(self, ctx: StepContext, *args: Any) -> Any:
        handler = getattr(self, f"do_{ctx.name}", None)
        if handler is None:
            raise NotImplementedError(f"the crew has no {ctx.name} step")
        return handler(ctx, *args)

    # ---- senses and diagnosis ------------------------------------------------------------------

    def do_route_hit(self, step: StepContext, hit: c.Hit, open_incidents: list[str]) -> Any:
        return Sentinel(self.ctx).route(hit, open_incidents)

    def do_triage(self, step: StepContext, alert: c.Alert) -> c.Signature:
        return Pathologist(self.ctx).triage(alert)

    def do_tag_traces(self, step: StepContext, incident: str, ids: list[str], phase: str) -> int:
        real = [t for t in ids if not t.startswith("local:")]
        if self.tags is None or not real:
            return 0
        for trace in real:
            self.tags.apply(trace, add=[f"incident:{incident}", f"phase:{phase}"])
        return len(real)

    def do_match_antibody(self, step: StepContext, sig: c.Signature) -> c.AntibodyMatch | None:
        """§5.8.3: exact φ, then the semantic fallback, then the motif on the host's graph; only
        an ACTIVE antibody answers (its recipe becomes the Coordinator's prior, A-42)."""
        if self.immunity is None:
            return None
        imm = self.immunity.immunologist
        case = self.ctx.case(sig.incident_id)
        fields = (case.reader("immunologist").get("signature", "phi", None) or {}).get("fields")
        found = imm.library.retrieve(sig.phi, fields or {})
        if found is None:
            return None
        ab = found.antibody
        bindings = imm.bindings(ab, imm.graph(sig.host))
        decision = {"ref": ab.ref, "how": found.how, "similarity": found.similarity,
                    "structural": bool(bindings)}  # fmt: skip
        if not bindings:  # step 3: the code no longer carries the motif → primary response
            case.put("antibody", "match", {**decision, "used": False}, by="immunologist")
            return None
        intent = "keep the documented behaviour of the code you change"
        recipe = render_recipe(ab.recipe, bindings[0], intent=intent)
        case.put("prior", "antibody", {**decision, "recipe": recipe, "binding": bindings[0]},
                 by="immunologist")  # fmt: skip
        self.ledger.append({"type": "antibody", "event": "matched", "antibody": ab.ref,
                            "incident": sig.incident_id, "how": found.how})  # fmt: skip
        digest = hashlib.sha256(ab.recipe.encode("utf-8")).hexdigest()
        return c.AntibodyMatch(ab.id, ab.version, f"sha256:{digest}")

    def do_reproduce(self, step: StepContext, sig: c.Signature) -> c.Repro:
        return Pathologist(self.ctx).reproduce(sig)

    def do_attribute(self, step: StepContext, sig: c.Signature, repro: c.Repro) -> c.Cause:
        return Archivist(self.ctx).attribute(sig, repro)

    def do_fixes_prior(self, step: StepContext, sig: c.Signature) -> str | None:
        if self.fixes is None:
            return None
        try:
            suggestions = self.fixes.triage_list(trace_ids=list(sig.trace_ids)[:20])
            if not suggestions:
                return None
            found = self.fixes.triage_get_investigation(str(suggestions[0]["id"]))
        except FixesUnavailable:
            return None
        return json.dumps(found)[:2000]

    # ---- planning and decisions -----------------------------------------------------------------

    def do_plan_budget(self, step: StepContext, sig: c.Signature, cause: c.Cause) -> Any:
        plan = self.coordinator.plan_budget(sig, cause)
        economics = self.ctx.case(sig.incident_id).reader("governor").get(
            "plan", "economics", None)  # fmt: skip
        if economics is not None:  # L5: the decision span, every alternative and its cost
            mine = [e for e in self.ledger.entries() if e.get("type") == "economics"
                    and e.get("incident") == sig.incident_id]  # fmt: skip
            if not mine:  # a retried step records it once
                self.ledger.append({"type": "economics", "incident": sig.incident_id,
                                    "host": sig.host, **economics})  # fmt: skip
        return plan

    def do_hypothesize(self, step: StepContext, *args: Any) -> list[c.Card]:
        sig, cause, plan, rnd, history, prior = args
        if prior and prior.startswith("antibody:"):  # a secondary response: the host's recipe
            stored = self.ctx.case(sig.incident_id).reader("coordinator").get(
                "prior", "antibody", None)  # fmt: skip
            if stored:
                prior = f"Antibody {stored['ref']} recipe (secondary response): {stored['recipe']}"
        return self.coordinator.hypothesize(sig, cause, plan, rnd, history, prior)

    def do_coordinate(self, step: StepContext, *args: Any) -> c.Decision:
        return self.coordinator.coordinate(*args)

    def do_dispute_experiment(self, step: StepContext, incident: str, d: c.Decision) -> Any:
        return self.coordinator.dispute_experiment(incident, d)

    def do_budget_exhausted(self, step: StepContext, *args: Any) -> bool:
        return self.coordinator.budget_exhausted(*args)

    # ---- repair -------------------------------------------------------------------------------

    def do_surgeon(self, step: StepContext, card: c.Card) -> c.SurgeonOutcome:
        brief = json.loads(card.brief)
        target = self.ctx.target(str(brief["host"]))
        case = self.ctx.case(card.incident_id)
        plan = case.reader("governor").get("plan", "plan", None) or {}
        budget = float(plan.get("budget_usd", 0.0))
        if plan and case.spent() >= budget:  # G6: a round never starts past the budget
            case.put(
                "refusal",
                card.hypothesis,
                {"reason": f"budget spent: ${case.spent():.2f} of ${budget:.2f} (G6)"},
                by="surgeon",
            )
            return c.SurgeonOutcome(
                card.incident_id,
                card.hypothesis,
                step.attempt,
                worktree.branch_name(card.incident_id, card.hypothesis),
                "",
                None,
            )
        attempts = AttemptLog(self.ctx.state / "attempts.sqlite")
        return SurgeonStep(target.repo, attempts, self.runner()).run(card, step)

    def do_audit_gates(self, step: StepContext, out: c.SurgeonOutcome) -> c.GateVerdict:
        return self.auditor.audit(out)

    def do_rewind(self, step: StepContext, outs: list[c.SurgeonOutcome]) -> None:
        """Discard the worktrees, keep each attempt's commit (and checkpoint) as evidence."""
        for out in outs:
            host = self._host(out)
            try:
                wt = worktree.open_existing(
                    self.ctx.target(host).repo, out.incident_id, out.hypothesis
                )
            except worktree.WorktreeError:
                continue  # already rewound (a retried step)
            kept = worktree.discard(wt)
            self.ledger.append(
                {
                    "type": "rewind",
                    "incident": out.incident_id,
                    "hypothesis": out.hypothesis,
                    "pinned": kept.pinned_ref,
                    "head": kept.head,
                }
            )

    def do_rebase_and_rerun(
        self, step: StepContext, outs: list[c.SurgeonOutcome], repo: str, new_main: str
    ) -> None:
        """L4: record, per candidate, what the move of main forces the gates to re-check."""
        for out in outs:
            if out.commit_sha is None:
                continue
            case = self.ctx.case(out.incident_id)
            card = case.reader("auditor").get("card", out.hypothesis)
            target = self.ctx.target(str(card["host"]))
            previous = case.reader("auditor").get("verdict", out.hypothesis, None) or {}
            try:
                plan = reverify_plan(target.repo, str(card["base_sha"]), new_main, out.commit_sha)
                record: dict[str, Any] = {
                    **plan.record(),
                    "rerun_g4": plan.rerun_g4,
                    "intersection": list(plan.intersection),
                    "changed": list(plan.changed),
                    "patched": list(plan.patched),
                    "tests": list(plan.tests),
                }
            except (GraphError, OSError) as error:  # no graph here: re-run everything
                record = {
                    "G1": "rerun",
                    "G2": "rerun",
                    "G3": "rerun:all",
                    "G4": "rerun:graph-unavailable",
                    "G7": "final-head",
                    "rerun_g4": True,
                    "graph_error": str(error)[:300],
                }
            record.update(
                {
                    "repo": repo,
                    "old_main": card["base_sha"],
                    "new_main": new_main,
                    "g4": previous.get("g4") or {},
                }
            )
            case.put("reverify", out.hypothesis, record, by="auditor")
            self.ledger.append(
                {
                    "type": "reverify",
                    "incident": out.incident_id,
                    "hypothesis": out.hypothesis,
                    "record": {k: v for k, v in record.items() if k.startswith("G")},
                }
            )

    def do_ship(self, step: StepContext, out: c.SurgeonOutcome) -> c.Shipment:
        if self.shipper is None:
            return c.Shipment(out.incident_id, out.hypothesis, out.branch, None, False, False)
        return self.shipper.ship(out)

    def do_canary_plan(self, step: StepContext, shipment: c.Shipment) -> c.CanaryPlan:
        """H0 for this canary: the shipment's host's baseline failure rate (``gates.canary``)."""
        host = self._shipment_host(shipment)
        baseline = canary.baseline_p0(host, baselines=self.ctx.baselines)
        return c.CanaryPlan(host, baseline.p0, canary.RATIO, baseline.source)

    def do_canary_sample(self, step: StepContext, shipment: c.Shipment, batch: int) -> list[bool]:
        """One live-traffic batch on the merged code: which tasks failed (recorded per batch)."""
        target = self.ctx.target(self._shipment_host(shipment))
        sha = shipment.merge_sha or shipment.branch
        runs = canary.sample(
            target, sha, batch, n=CANARY_TASKS, record=self.canary_record(shipment)
        )
        return [not run.passed for run in runs]

    def canary_record(self, shipment: c.Shipment) -> Path:
        sha = (shipment.merge_sha or shipment.branch).replace("/", "_")
        return self.ctx.state / "canary" / shipment.incident_id / sha[:40]

    # ---- ledger-shaped steps -----------------------------------------------------------------

    def do_record_transition(self, step: StepContext, record: c.TransitionRecord) -> None:
        self.ledger.append(
            {"type": "transition", "incident": record.incident_id, "transition": _plain(record)}
        )

    def do_park(self, step: StepContext, sig: c.Signature, reason: str) -> None:
        self.ledger.append({"type": "park", "incident": sig.incident_id, "reason": reason})

    def do_escalate(self, step: StepContext, incident: str, reason: str) -> None:
        case = self.ctx.case(incident)
        view = case.reader("coordinator")
        dossier = {name: view.get("decision", name) for name in view.names("decision")}
        self.ledger.append(
            {
                "type": "escalate",
                "incident": incident,
                "reason": reason,
                "decisions": dossier,
                "spent_usd": case.spent(),
            }
        )

    def do_ledger_append(self, step: StepContext, entry: dict[str, Any]) -> str:
        return self.ledger.append(entry)

    def _lane(self) -> SabotageLane:
        if self.sabotage is None:
            raise NotImplementedError("no sabotage lane is configured (Stage 8, §7.2)")
        return self.sabotage

    def do_seal_sabotage(self, step: StepContext, spec_id: str) -> str:
        """Seal (or find the pre-T0 seal), record it in the ledger, publish it; returns c."""
        lane = self._lane()
        record = lane.saboteur.record(spec_id)
        if record is None:
            if spec_id not in lane.plans:
                raise SabotageRefused(f"{spec_id}: neither sealed nor planned")
            record = lane.saboteur.seal(*lane.plans[spec_id])
        mine = [e for e in self.ledger.entries() if e.get("sabotage") == spec_id]
        sealed_before = [e for e in mine if e.get("event") == "sealed"]
        if not sealed_before:
            self.ledger.append({"type": "sabotage", "event": "sealed", "sabotage": spec_id,
                                "c": record["c"], "not_before": record["not_before"]})  # fmt: skip
            if lane.publish is not None:
                lane.publish(lane.saboteur.commitment_request(record))
        return str(record["c"])

    def do_deliver_sabotage(self, step: StepContext, spec_id: str) -> None:
        """Deliver after ``not_before`` (the workflow schedules it); the prompt is the spec's
        framing and the patch only (A-52), checked for leaks before it leaves."""
        lane = self._lane()
        spec, _seal, patch = lane.saboteur.open(spec_id, lane.identity)
        not_before = datetime.fromisoformat(str(spec["schedule"]["not_before"]).replace("Z",
                                            "+00:00"))  # fmt: skip
        if datetime.now(UTC) < not_before:
            raise SabotageRefused(f"{spec_id}: not before {spec['schedule']['not_before']}")
        if patch is None:
            raise NotImplementedError(f"{spec['class']}: only patch deliveries are wired")
        lane.deliver(lane.saboteur.delivery_request(spec, patch), str(spec["target"]["host"]))
        self.ledger.append({"type": "sabotage", "event": "delivered", "sabotage": spec_id})

    def do_anchor_ledger(self, step: StepContext, hour: int) -> str:
        """AuditAnchor's hourly step: sign the head, write the post text; returns the tag."""
        if self.anchoring is None or not isinstance(self.ledger, BranchLedger):
            raise NotImplementedError("anchors need a BranchLedger and the T0 signer (A-67)")
        anchor = self.ledger.anchor(self.anchoring.signer)
        out = self.anchoring.out_dir / f"{anchor.tag}.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(post_text(anchor, self.anchoring.repo_url) + "\n", encoding="utf-8")
        if self.anchoring.remote is not None:  # a rejected push fails the step: a rewrite
            self.ledger.push(self.anchoring.remote)
        return anchor.tag

    # ---- Stage 7: antibodies (§5.8), vaccination (§6.5 steps 12–14), L3 (§6.7) -----------------

    def _ab(self, ref: c.AntibodyRef) -> Antibody | None:
        if self.immunity is None:
            return None
        try:
            return self.immunity.library.ref(f"{ref.antibody_id}@{ref.version}")
        except LibraryError:
            return None

    def do_synthesize_antibody(
        self, step: StepContext, out: c.SurgeonOutcome, cause: c.Cause
    ) -> c.AntibodyRef:
        if self.immunity is None:
            self.ledger.append({"type": "antibody", "incident": out.incident_id,
                                "state": "no antibody library configured"})  # fmt: skip
            return c.AntibodyRef(f"AB-{out.incident_id}", "0.0", "NONE")
        try:
            ab = self.immunity.immunologist.synthesize(out)
        except SynthesisError as error:  # no structural motif: the repair stays a one-off
            self.ledger.append({"type": "antibody", "incident": out.incident_id,
                                "state": "not synthesised",
                                "reason": str(error)[:300]})  # fmt: skip
            return c.AntibodyRef(f"AB-{out.incident_id}", "0.0", "NONE")
        return c.AntibodyRef(ab.id, ab.version, str(ab.state))

    def do_motif_scan(self, step: StepContext, ab: c.AntibodyRef) -> list[str]:
        found = self._ab(ab)
        if found is None or self.immunity is None:
            return []
        return self.immunity.immunologist.scan(found)

    def do_apply_antibody(self, step: StepContext, ab: c.AntibodyRef, host: str) -> Any:
        found = self._ab(ab)
        if found is None or self.immunity is None:
            return c.VaccinationResult(ab.antibody_id, host, False, "no such antibody")
        if found.state is not AntibodyState.ACTIVE:  # a paused or quarantined recipe stops
            return c.VaccinationResult(ab.antibody_id, host, False, f"{found.ref} is {found.state}")
        current = self.immunity.library.load(found.id)  # protection is current, not pinned
        if host in current.lineage.hosts_protected:
            return c.VaccinationResult(ab.antibody_id, host, False, f"already carries {found.id}")
        applier = self.immunity.appliers.get(host)
        if applier is not None:
            return applier(self, step, found, host)
        return self.vaccinate(step, found, host)

    def do_mature_antibody(self, step: StepContext, ab: c.AntibodyRef, host: str) -> Any:
        found = self._ab(ab)
        if found is None or self.immunity is None:
            return c.VaccinationResult(ab.antibody_id, host, False, "no such antibody")
        return self.vaccinate(step, found, host)

    def vaccinate(self, step: StepContext, ab: Antibody, host: str) -> c.VaccinationResult:
        """Induced trigger → the vaccination case → a Surgeon on the recipe → G0–G6 → ship."""
        assert self.immunity is not None
        imm = self.immunity.immunologist
        trigger = imm.induced_trigger(ab, host)
        imm.library.record(ab.id, {"event": "induced-trigger", **trigger.as_dict()})
        if not trigger.produced_failure:  # §6.5 step 14: never vaccinate a host it cannot help
            return c.VaccinationResult(ab.id, host, False,
                                       "the induced trigger produced no failure")  # fmt: skip
        case, card = imm.prepare(ab, host, trigger)
        target = self.ctx.target(host)
        attempts = AttemptLog(self.ctx.state / "attempts.sqlite")
        out = SurgeonStep(target.repo, attempts, self.runner()).run(card, step)
        verdict = self.auditor.audit(out)
        record = {"verdict": _plain(verdict), "commit": out.commit_sha,
                  "trigger": trigger.as_dict()}  # fmt: skip
        case.put("antibody", f"apply-{ab.version}", record, by="immunologist")
        if not verdict.passed:
            imm.library.record(ab.id, {"event": "vaccination-failed", "host": host,
                                       "reason": verdict.reason[:300]})  # fmt: skip
            return c.VaccinationResult(ab.id, host, False, f"gates: {verdict.reason}"[:500])
        shipper = self.immunity.shippers.get(host)
        if shipper is None:
            return c.VaccinationResult(ab.id, host, False, "G0–G6 pass; no shipper for the host")
        shipment = shipper.ship(out)
        if shipment.pr_url is None:
            return c.VaccinationResult(ab.id, host, False, "the pull request was not opened")
        imm.library.protect(ab.id, host)
        how = "merged" if shipment.merged else "pull request opened"
        self.ledger.append({"type": "vaccination", "antibody": ab.ref, "host": host,
                            "pr": shipment.pr_url, "merged": shipment.merged,
                            "commit": out.commit_sha})  # fmt: skip
        return c.VaccinationResult(ab.id, host, True, f"G0–G6 pass; {how}", shipment)

    def do_promote_antibody(self, step: StepContext, ab: c.AntibodyRef, host: str) -> Any:
        if self.immunity is None:
            return ab
        promoted = self.immunity.immunologist.promote(ab.antibody_id, host,
                                                      why=f"matured on {host}")  # fmt: skip
        return c.AntibodyRef(promoted.id, promoted.version, str(promoted.state))

    def do_backfill_receptor(self, step: StepContext, ab: c.AntibodyRef) -> None:
        found = self._ab(ab)
        if found is not None and self.immunity is not None:
            self.immunity.immunologist.backfill(found)

    # ---- L3: revert, quarantine, trace-to-test, refinement ---------------------------------------

    def _canary_why(self, shipment: c.Shipment, host: str, what: str) -> tuple[str, int]:
        """The rejection, recomputed from the recorded batches (the same SPRT the workflow ran)."""
        batches = [[r["verdict"] != "PASS" for r in b["runs"]]
                   for b in canary.records(self.canary_record(shipment))]  # fmt: skip
        baseline = canary.baseline_p0(host, baselines=self.ctx.baselines)
        result = canary.decide(canary.sprt_for(baseline), batches)
        why = (f"The canary's SPRT rejected {what} on {host} at n={result.n} "
               f"(LLR {result.llr:.3f}).")  # fmt: skip
        return why, result.n

    def _revert(self, shipment: c.Shipment, what: str) -> RevertRecord | None:
        if self.immunity is None:
            return None
        host = self._shipment_host(shipment)
        reverter = self.immunity.reverters.get(host)
        if reverter is None:
            return None
        why, n = self._canary_why(shipment, host, what)
        failing = canary.failing_runs(self.canary_record(shipment))
        cases = [(seed, task) for seed, task, _ in failing]
        record = f"REV-{shipment.incident_id}"[:64]
        self.ctx.incidents.register(record, host, f"revert:{shipment.incident_id}")
        result = reverter.revert(self.ctx.target(host), shipment, record=record,
                                 title=f"{what} on {host} (canary SPRT rejected at n={n})",
                                 why=why, cases=cases)  # fmt: skip
        self.ledger.append({"type": "revert", "incident": shipment.incident_id, "host": host,
                            "ok": result.ok, "pr": result.pr_url, "commit": result.commit,
                            "checkpoint": result.checkpoint, "gates": result.gates,
                            "reason": result.reason})  # fmt: skip
        return result

    def do_revert(self, step: StepContext, shipment: c.Shipment) -> bool:
        result = self._revert(shipment, shipment.hypothesis)
        return bool(result and result.ok)

    def do_revert_vaccination(self, step: StepContext, shipment: c.Shipment) -> Any:
        result = self._revert(shipment, shipment.hypothesis)
        if result is None:
            return None
        self.ctx.case(shipment.incident_id).put(
            "antibody", "revert", _plain(result), by="immunologist"
        )
        return result.shipment(result.record) if result.ok else None

    def _related(self, shipment: c.Shipment) -> list[str]:
        """Antibodies behind a rejected shipment: the vaccination's own, or a secondary
        response's prior."""
        opened = self.ctx.incidents.receptor_of(shipment.incident_id) or ""
        if opened.startswith("antibody:"):
            return [opened.removeprefix("antibody:")]
        prior = self.ctx.case(shipment.incident_id).reader("coordinator").get(
            "prior", "antibody", None)  # fmt: skip
        return [str(prior["ref"])] if prior else []

    def do_quarantine_related(self, step: StepContext, shipment: c.Shipment) -> int:
        if self.immunity is None:
            return 0
        imm = self.immunity.immunologist
        host = self._shipment_host(shipment)
        count = 0
        for ref in self._related(shipment):
            why, _ = self._canary_why(shipment, host, ref)
            prefixes = self.immunity.pr_prefixes
            imm.quarantine(ref.split("@")[0], host, why=why, github=self.immunity.github,
                           pr_prefix=prefixes[0], more_prefixes=prefixes[1:])  # fmt: skip
            count += 1
        return count

    def do_refine_antibody(
        self, step: StepContext, ab: c.AntibodyRef, shipment: c.Shipment
    ) -> c.AntibodyRef | None:
        found = self._ab(ab)
        if found is None or self.immunity is None:
            return None
        imm = self.immunity.immunologist
        host = self._shipment_host(shipment)
        case = self.ctx.case(shipment.incident_id)
        reverted = case.reader("immunologist").get("antibody", "revert", None) or {}
        trace_to_test = self.eval_augment(found, host, shipment, reverted)
        target = self.ctx.target(host)
        before = rev(target.repo, f"{shipment.merge_sha}^1")  # the host before the vaccination
        _, n = self._canary_why(shipment, host, found.ref)
        try:
            revised, _ = imm.refine(found.id, (host, imm.graph(host, before)), case=case,
                                    trigger=f"canary SPRT reject on {host} at n={n}",
                                    revert_pr=reverted.get("pr_url"),
                                    trace_to_test=trace_to_test)  # fmt: skip
        except SynthesisError as error:
            self.ledger.append({"type": "antibody", "event": "refinement-failed",
                                "antibody": found.ref, "reason": str(error)[:300]})  # fmt: skip
            return None
        # A minor version keeps the recipe, so the maturation's gate evidence carries over; the
        # refined motif must still bind the host it matured on.
        origin = str(found.evidence.get("host"))
        matured = [h for h in revised.lineage.hosts_protected if h != origin]
        still = [h for h in matured if imm.matcher(revised.motif).matches_any(imm.graph(h))]
        if not still:
            return c.AntibodyRef(revised.id, revised.version, str(revised.state))
        why = (f"re-matured: {revised.ref} still binds {still[0]}; the recipe is unchanged "
               f"since {found.version}")  # fmt: skip
        promoted = imm.promote(found.id, still[0], why=why)
        if self.immunity.github is not None:
            for prefix in self.immunity.pr_prefixes:
                for pr in self.immunity.github.pr_list_open(f"{prefix}kintsugi/VAC-{found.id}-"):
                    self.immunity.github.pr_comment(int(pr["number"]), (
                        f"Resumed: {promoted.ref} refined after the quarantine (learned "
                        f"precondition: {'; '.join(promoted.preconditions)}) and re-matured; "
                        "this pull request is covered by the refined version."))  # fmt: skip
        return c.AntibodyRef(promoted.id, promoted.version, str(promoted.state))

    def eval_augment(
        self, ab: Antibody, host: str, shipment: c.Shipment, reverted: Mapping[str, Any]
    ) -> str | None:
        """§6.7 step 18: the canary's failing runs become seed-addressed regression cases in the
        host's train regression suite, committed by a notary (scope ``eval_augment``) after the
        Auditor's check: train tasks only, and each case failed on the vaccinated merge and
        passes on the reverted code (the revert's expedited G2 replayed exactly these)."""
        if self.immunity is None or self.immunity.notary is None:
            return None
        imm = self.immunity.immunologist
        reverter = self.immunity.reverters.get(host)
        cases = imm.regression_cases(self.canary_record(shipment), ab,
                                     f"canary of {ab.ref} on {host}")  # fmt: skip
        g2 = (reverted.get("gates") or {}).get("G2") or {}
        if not cases or reverter is None or not g2.get("passed"):
            return None
        target = self.ctx.target(host)
        path = imm.regressions_path(target, ab, f"{ab.id} {host} canary")
        lines = (json.dumps(cases, indent=1, sort_keys=True) + "\n").splitlines()
        diff = (f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n"
                f"+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n"
                + "".join(f"+{line}\n" for line in lines))  # fmt: skip
        record = f"EVA-{ab.id}-{host}"[:64]
        base = reverter.base_head(target.repo, record)
        wt = worktree.create(target.repo, record, "augment", base=base)
        title = f"test({host}): add the {ab.ref} canary regressions (trace-to-test)"
        notarised = self.immunity.notary.commit(wt.path, NotaryRequest(
            operation="eval-augment", reference=f"{ab.ref} on {host}", incident=record,
            purpose=(f"The canary that rejected {ab.ref} on {host} recorded these failing runs; "
                     "each becomes a regression case (task and seed) in the train suite."),
            diff=diff, subject=title, scopes=("eval_augment",)),
            package_root=target.package_root)  # fmt: skip
        shipped = reverter.ship_commit(target.repo, notarised.sha, wt.branch, title,
                                       f"Trace-to-test for {ab.ref}: {len(cases)} cases. Auditor "
                                       "check: train tasks only; each failed on the vaccinated "
                                       "merge and passes on the revert.")  # fmt: skip
        self.ledger.append({"type": "eval-augment", "antibody": ab.ref, "host": host,
                            "path": path, "cases": len(cases), "commit": notarised.sha,
                            "checkpoint": notarised.checkpoint_id,
                            "pr": shipped["pr_url"]})  # fmt: skip
        return path

    def do_fixes_resolve(self, step: StepContext, sig: c.Signature, shipment: c.Shipment) -> None:
        """§4.2: mark the incident's claimed neatlogs Fix suggestion resolved, with the PR."""
        if self.fixes is None or shipment.pr_url is None:
            return None
        try:
            suggestions = self.fixes.triage_list(trace_ids=list(sig.trace_ids)[:20])
            for suggestion in suggestions[:1]:
                self.fixes.triage_update_status(str(suggestion["id"]), "resolved",
                                                pr_url=shipment.pr_url)  # fmt: skip
        except FixesUnavailable:
            return None
        return None

    # ---- helpers ------------------------------------------------------------------------------

    def _host(self, out: c.SurgeonOutcome) -> str:
        card = self.ctx.case(out.incident_id).reader("auditor").get("card", out.hypothesis)
        return str(card["host"])

    def _shipment_host(self, shipment: c.Shipment) -> str:
        host = self.ctx.incidents.host_of(shipment.incident_id)
        if host is not None:
            return host
        card = (
            self.ctx.case(shipment.incident_id).reader("auditor").get("card", shipment.hypothesis)
        )
        return str(card["host"])


def _plain(value: Any) -> dict[str, Any]:
    from kintsugi.crew.casefile import plain

    return dict(plain(value))
