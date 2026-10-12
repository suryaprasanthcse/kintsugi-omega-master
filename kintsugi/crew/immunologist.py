"""The Immunologist: builds, matures, vaccinates with, quarantines and refines antibodies (§5.7).

Inputs are the resolved incident's case file (winner, cause, intent, verdicts, costs), the
before/after code graphs and the fleet's graphs; outputs live in the Antibody Library
(``kintsugi.memory.library``) and the ledger.

- **synthesize** (§6.5 step 12): the motif is the catalogue template whose embedding exists in
  the origin's graph *before* the fix, touches the cause report's suspects, and is gone *after*
  it (validated, not asserted); the recipe has ``{S} {F} {R} {Enc} {intent}`` slots
  (``claude-opus-5-5`` drafts it, a deterministic template is the fallback); the receptor is a
  regex Detection on ``grader.verdict`` for the pathogen's verdict family, its precision measured
  by Omega on recorded history (``/detections/preview`` is OAuth-only, A-29); the contract is
  the winner's gate thresholds; evidence and lineage name the incident, checkpoints, PR and CI.
- **backfill** (retroactive diagnosis): the receptor over recorded history finds past
  occurrences no incident ever claimed, with their dollar impact (Omega-side retro scan, A-29).
- **scan** (§6.5 step 14): the motif (and its preconditions) on every fleet host's graph.
- **induced trigger**: a host is vaccinated only if 429s injected into its retried scope
  produce a failure that the same seed without them does not; the failing runs, re-recorded and
  replayed 3/3 (G0), become the vaccination's cassette.
- **apply**: a vaccination case ``VAC-<AB>-<host>`` (trigger cassette, a card whose approach is
  the rendered recipe, the plan), a Surgeon session through ``SurgeonStep``, the blind Auditor's
  G0–G6, then the host's shipper: merged on our fleet, a pull request only on foreign hosts.
- **quarantine** (§6.7 step 17): QUARANTINED, the receptor stays on, open vaccination PRs are
  told why; **eval_augment** (step 18) turns the canary's failing runs into seed-addressed
  regression cases committed by a notary; **refine** (step 19) learns the missing precondition
  contrastively, has the rationale written and checked, and bumps the minor version.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import networkx as nx

from kintsugi.control import contracts as c
from kintsugi.crew.casefile import CaseFile
from kintsugi.crew.consult import consult
from kintsugi.crew.context import CrewContext
from kintsugi.crew.manifest import ContextBuilder
from kintsugi.gates import g0
from kintsugi.gates.canary import baseline_p0, failing_runs
from kintsugi.gates.checkout import clean_checkout, diff_text, rev
from kintsugi.gates.evals import run_tasks
from kintsugi.gates.protected_paths import load_policy
from kintsugi.gates.target import Target
from kintsugi.memory import contrastive, graph, predicates
from kintsugi.memory import motif as m
from kintsugi.memory.antibody import Antibody, AntibodyState, MotifSpec
from kintsugi.memory.library import AntibodyLibrary, cassette_slug
from kintsugi.sandbox import chaos_proxy as chaos
from kintsugi.senses.signature import canonical_error

MODEL = "claude-opus-5-5"
TRIGGER_RATE = 0.30  # 429s injected into the retried scope's services (the stress level)
TRIGGER_TASKS = 20
TRIGGER_SEEDS = (1, 2)
MIN_RECEPTOR_PRECISION = 0.9
VACCINATION_BUDGET_USD = 6.0  # a secondary response's budget: one Surgeon, its gates
NO_GROUP = "00000000-0000-0000-0000-000000000000"  # receptors are drafted before a project

# The motif catalogue. A template names the generic shape of a pathogen class; synthesis keeps
# the one the origin's own before/after graphs validate.
TEMPLATES: dict[str, dict[str, Any]] = {
    "state-reset-on-retry": {
        "nodes": {
            "R": {"kind": "function", "pred": ["is_retry_wrapper"]},
            "Enc": {"kind": "function"},
            "F": {"kind": "function"},
            "S": {"kind": "class", "pred": ["has_mutable_accumulator"]},
        },
        "edges": [["Enc", "R", "CALLS"], ["Enc", "F", "CONTAINS"], ["F", "S", "CONSTRUCTS"]],
    },
}
RECIPE_FALLBACK = (
    "Move the construction of `{S}` out of `{F}`, the scope that `{R}` retries inside `{Enc}`, "
    "so the state `{S}` accumulates survives a retry. Preserve the resume semantics: a retry "
    "continues where the failed attempt stopped and never repeats a side effect it already "
    "made. Keep the intent of the code you change: {intent}"
)
RECIPE_SCHEMA = {
    "type": "object",
    "properties": {"recipe": {"type": "string"}, "description": {"type": "string"}},
    "required": ["recipe", "description"],
    "additionalProperties": False,
}
RATIONALE_SCHEMA = {
    "type": "object",
    "properties": {"rationale": {"type": "string"}},
    "required": ["rationale"],
    "additionalProperties": False,
}


class SynthesisError(RuntimeError):
    pass


# ---- recorded history --------------------------------------------------------------------------


@dataclass(frozen=True)
class Record:
    """One recorded task run: the unit the receptor (a ``grader.verdict`` detection) sees."""

    trace_id: str
    host: str
    verdict: str
    source: str  # where it was recorded (a run, a canary, a baseline)
    incident: str | None = None  # the incident that claimed it, if any


class History:
    """Recorded task runs: observation folders (failures, ``LocalTraces`` format) and verdict
    logs (every run, ``{"trace_id","host","verdict","source"}`` JSON lines)."""

    def __init__(self, records: Iterable[Record] = ()) -> None:
        self.records = list(records)

    @classmethod
    def load(cls, *paths: Path, claimed: Mapping[str, str] | None = None) -> History:
        claimed = claimed or {}
        found: dict[str, Record] = {}
        for path in paths:
            files = sorted(path.rglob("*.jsonl")) if path.is_dir() else [path]
            for file in files:
                if not file.is_file():
                    continue
                for line in file.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    trace = str(data["trace_id"])
                    found[trace] = Record(
                        trace,
                        str(data["host"]),
                        str(data["verdict"]),
                        str(data.get("source") or file.stem),
                        claimed.get(trace),
                    )
        return cls(found.values())

    @staticmethod
    def write(path: Path, records: Iterable[Record]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps(
                {"trace_id": r.trace_id, "host": r.host, "verdict": r.verdict, "source": r.source},
                sort_keys=True,
            )
            for r in records
        ]
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write("".join(line + "\n" for line in lines))
        return path


def receptor_rule(verdicts: Iterable[str]) -> str:
    """The verdict family of the pathogen: ``FAIL:DUPLICATE_ENTRY`` → ``^"?FAIL:DUPLICATE_``.

    Span outputs are JSON-encoded, so the stored value may carry its opening quote (R-007)."""
    reasons = sorted({v.removeprefix("FAIL:") for v in verdicts if v.startswith("FAIL:")})
    if not reasons:
        raise SynthesisError("no failing verdict to build a receptor from")
    families = {r.split("_")[0] + ("_" if "_" in r else "") for r in reasons}
    if len(families) != 1:
        return '^"?FAIL:(' + "|".join(re.escape(r) for r in reasons) + ")"
    return '^"?FAIL:' + re.escape(families.pop())


def receptor_body(
    ab_id: str, rule: str, description: str, *, group_id: str = NO_GROUP
) -> dict[str, Any]:
    """A neatlogs Detection (POST /detections) for the antibody's receptor, fleet-wide; the
    group is filled in when the receptor is created in a project (pinned contract)."""
    return {
        "type": "regex",
        "displayName": f"KINTSUGI {ab_id} receptor",
        "description": f"{description} (KINTSUGI-Ω antibody {ab_id} receptor)"[:500],
        "groupId": group_id,
        "isActive": True,
        "runOn": {"frequency": "span_name", "span_names": ["grader.verdict"]},
        "config": {"output_pattern": rule, "case_sensitive": True},
    }


@dataclass(frozen=True)
class ReceptorScore:
    precision: float | None
    flagged: int
    true_positives: int
    false_positives: tuple[str, ...]
    scanned: int


def score_receptor(
    rule: str, error_class: str, vulnerable: set[str], history: History
) -> ReceptorScore:
    """Precision on history: a flagged run is a true occurrence when its failure has the
    pathogen's error class on a host whose code carries the motif (structurally vulnerable)."""
    pattern = re.compile(rule)
    flagged = [r for r in history.records if pattern.search(f'"{r.verdict}"')]
    true = [
        r for r in flagged if canonical_error(r.verdict) == error_class and r.host in vulnerable
    ]
    false = tuple(sorted(r.trace_id for r in flagged if r not in true))
    precision = len(true) / len(flagged) if flagged else None
    return ReceptorScore(precision, len(flagged), len(true), false, len(history.records))


# ---- fleet graphs ------------------------------------------------------------------------------


def package_dir(target: Target) -> str:
    root = target.package_root.strip("/")
    return target.host.package if root in ("", ".") else f"{root}/{target.host.package}"


def graph_at(target: Target, sha: str) -> nx.DiGraph:
    """The host's code graph at ``sha`` (one snapshot of a clean checkout, restricted)."""
    with clean_checkout(target.repo, sha) as checkout:
        snap = graph.snapshot(checkout, worktree=True)
        return predicates.host_graph(
            snap,
            package_dir=package_dir(target),
            side_effect_tools=target.host.side_effecting_tools(),
            source_root=checkout,
        )


def scope_services(target: Target, g: nx.DiGraph, scope: str) -> list[str]:
    """External services the retried scope reaches (tools with ``external:`` it calls)."""
    tools = {name: tool.external for name, tool in target.host.tools.items() if tool.external}
    reached = [scope] + [
        t for _, t, d in g.out_edges(scope, data=True) if m.TRANSITIVE_CALLS in d["rels"]
    ]
    return sorted({tools[n] for node in reached if (n := str(g.nodes[node].get("name"))) in tools})


def trigger_profile(services: Sequence[str], *, rate: float = TRIGGER_RATE) -> chaos.Profile:
    background = chaos.background_profile()
    rules = list(background.rules) + [
        chaos.Rule(service, "status", rate, "*", {"status": 429}) for service in services
    ]
    return chaos.Profile(f"induced-trigger:{'+'.join(services)}", background.seed, tuple(rules))


@dataclass(frozen=True)
class Trigger:
    host: str
    services: tuple[str, ...]
    produced_failure: bool
    failing: tuple[str, ...]  # tasks that fail under the trigger and pass without it
    classes: Mapping[str, int]
    seed: int | None
    reproduction: g0.Reproduction | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "host": self.host,
            "services": list(self.services),
            "produced_failure": self.produced_failure,
            "failing": list(self.failing),
            "classes": dict(self.classes),
            "seed": self.seed,
        }
        if self.reproduction is not None:
            out["g0"] = self.reproduction.result.as_dict()
        return out


# ---- the Immunologist --------------------------------------------------------------------------


@dataclass
class Immunologist:
    ctx: CrewContext
    library: AntibodyLibrary
    history: History = field(default_factory=History)
    positives: Mapping[str, nx.DiGraph] = field(default_factory=dict)  # known-vulnerable graphs
    ledger: Any = None  # FileLedger
    classify: predicates.Classifier | None = None

    def __post_init__(self) -> None:
        self.registry = predicates.registry(
            classify=self.classify,
            cache=predicates.VerdictCache(self.ctx.state / "predicate-verdicts.json"),
        )
        self._graphs: dict[tuple[str, str], nx.DiGraph] = {}

    # ---- helpers ------------------------------------------------------------------------------

    def _log(self, entry: Mapping[str, Any]) -> None:
        if self.ledger is not None:
            self.ledger.append(dict(entry))

    def graph(self, host: str, sha: str | None = None) -> nx.DiGraph:
        target = self.ctx.target(host)
        sha = sha or rev(target.repo, self.ctx.base_ref)
        key = (host, sha)
        if key not in self._graphs:
            self._graphs[key] = graph_at(target, sha)
        return self._graphs[key]

    def matcher(self, motif: MotifSpec) -> m.MotifMatcher:
        return m.MotifMatcher(motif, predicates=self.registry)

    def bindings(self, ab: Antibody, g: nx.DiGraph) -> list[dict[str, str]]:
        return [predicates.names(g, e) for e in self.matcher(ab.motif).matches(g)]

    # ---- synthesis ----------------------------------------------------------------------------

    def synthesize(self, out: c.SurgeonOutcome, *, merge_sha: str | None = None) -> Antibody:
        case = self.ctx.case(out.incident_id)
        view = case.reader("immunologist")
        card = view.get("card", out.hypothesis)
        host = str(card["host"])
        target = self.ctx.target(host)
        signature = view.get("signature", "phi")
        symptom = view.get("symptom", "summary")
        report = view.get("cause", "report", None) or {}
        record = view.get("intent_record", "record", None) or {}
        verdict = view.get("verdict", out.hypothesis, None) or {}
        outcome = view.get("outcome", out.hypothesis, None) or {}
        shipped = view.get("decision", f"ship-{out.hypothesis}", None) or {}
        before_sha, after_sha = str(card["base_sha"]), str(out.commit_sha)
        before, after = self.graph(host, before_sha), self.graph(host, after_sha)
        suspects = {str(s).split(".")[-1] for s in report.get("suspects") or []}
        name, motif, binding = self._motif(before, after, suspects)
        clauses = record.get("behavioral_contract") or []
        intent = "; ".join(f'{c_["clause"]} ("{c_["quote"]}")' for c_ in clauses[:3]) or str(
            record.get("stated_goal") or "the documented behaviour of the code you change"
        )
        recipe, description = self._recipe(case, out, target, binding, intent, report)
        members = self._observations(case, signature["fields"])
        verdicts = [str(o.get("verdict")) for o in members if o.get("verdict")] or [
            f"FAIL:{signature['fields'].get('error_class', 'UNKNOWN')}"
        ]
        rule = receptor_rule(verdicts)
        error_class = str(signature["fields"].get("error_class") or canonical_error(verdicts[0]))
        ab_id = self.library.next_id()
        score = score_receptor(rule, error_class, {host}, self.history)
        g4 = verdict.get("g4") or {}
        p0 = baseline_p0(host, baselines=self.ctx.baselines)
        ab = Antibody.model_validate(
            {
                "id": ab_id,
                "version": "1.0",
                "state": AntibodyState.CANDIDATE,
                "signature": {
                    "hash": signature["phi"],
                    "fields": {**signature["fields"], "label": signature.get("label")},
                },
                "receptor": {"type": "regex", "rule": rule, "precision_preview": score.precision},
                "motif": motif.model_dump(mode="json"),
                "preconditions": [],
                "recipe": recipe,
                "contract": {
                    "repro": "3/3",
                    "holdout": {
                        "test": "mcnemar_exact",
                        "alpha": 0.05,
                        "holm": True,
                        "origin": {
                            k: g4.get(k) for k in ("b", "c", "p", "pass_before", "pass_after")
                        },
                    },
                    "cost_delta_ub": 0.05,
                    "p95_delta_ub": 0.10,
                    "canary": {
                        "p0": "host_baseline",
                        "p1": "2*p0",
                        "alpha": 0.05,
                        "beta": 0.10,
                        "origin_p0": p0.p0,
                    },
                },
                "evidence": {
                    "origin_incident": out.incident_id,
                    "host": host,
                    "template": name,
                    "binding": binding,
                },
                "economics": {"primary_usd": round(case.spent(), 4)},
                "lineage": {"versions": ["1.0"], "quarantines": 0, "hosts_protected": [host]},
            }
        )
        evidence = {
            "origin_incident": out.incident_id,
            "host": host,
            "symptom": symptom.get("summary"),
            "traces": list(signature.get("trace_ids") or []),
            "checkpoints": {
                "intent": record.get("checkpoint"),
                "repair": outcome.get("checkpoint"),
                "originating": report.get("first_bad_checkpoint"),
            },
            "commits": {
                "originating": report.get("originating_commit"),
                "before": before_sha,
                "repair": after_sha,
                "merge": merge_sha,
            },
            "prs": [shipped["pr"]] if shipped.get("pr") else [],
            "ci_runs": [shipped["g7"]] if isinstance(shipped.get("g7"), str) else [],
            "g4": g4,
            "motif_validation": {
                "template": name,
                "before": binding,
                "after": None,
                "matches_before_fix": True,
                "matches_after_fix": False,
            },
            "receptor_precision": score.__dict__,
            "cost_usd": round(case.spent(), 4),
        }
        self.library.save(
            ab,
            receptor_body=receptor_body(ab_id, rule, description),
            evidence=evidence,
            description=description,
            event={"event": "synthesized", "incident": out.incident_id, "state": "CANDIDATE"},
        )
        case.put(
            "antibody",
            ab.ref,
            {
                "ref": ab.ref,
                "state": "CANDIDATE",
                "binding": binding,
                "rule": rule,
                "precision": score.precision,
            },
            by="immunologist",
        )
        self._log(
            {
                "type": "antibody",
                "event": "synthesized",
                "antibody": ab.ref,
                "incident": out.incident_id,
                "motif": name,
            }
        )
        return ab

    def _motif(
        self, before: nx.DiGraph, after: nx.DiGraph, suspects: set[str]
    ) -> tuple[str, MotifSpec, dict[str, str]]:
        for name, raw in TEMPLATES.items():
            motif = MotifSpec.model_validate(raw)
            matcher = self.matcher(motif)
            found = [predicates.names(before, e) for e in matcher.matches(before)]
            touching = [
                b
                for b in found
                if not suspects or {v.split(".")[-1] for v in b.values()} & suspects
            ]
            if touching and not matcher.matches_any(after):
                return name, motif, touching[0]
        raise SynthesisError("no motif template matches the code before the fix and not after it")

    def _observations(self, case: CaseFile, fields: Mapping[str, Any]) -> list[dict[str, Any]]:
        """The failing runs of the incident's own cluster (the Pathologist's clustering)."""
        view = case.reader("immunologist")
        found: list[dict[str, Any]] = []
        for name in view.names("observations"):
            data = view.get("observations", name)
            for cluster in data.get("clusters", []) if isinstance(data, dict) else []:
                if all(
                    cluster.get("fields", {}).get(k) == v
                    for k, v in fields.items()
                    if k in cluster.get("fields", {})
                ):
                    found += list(cluster.get("members") or [])
        return found

    def _recipe(
        self,
        case: CaseFile,
        out: c.SurgeonOutcome,
        target: Target,
        binding: Mapping[str, str],
        intent: str,
        report: Mapping[str, Any],
    ) -> tuple[str, str]:
        view = case.reader("immunologist")
        card = view.get("card", out.hypothesis)
        builder = ContextBuilder("immunologist", out.incident_id)
        builder.bind("incident", out.incident_id)
        builder.bind("host", target.id)
        builder.add(
            "binding",
            json.dumps(binding, indent=1),
            source="motif binding (before)",
            why="the structure the recipe names",
        )
        builder.add(
            "mechanism",
            str(report.get("mechanism") or ""),
            source="cause report",
            why="why it failed",
        )
        builder.add(
            "fix",
            f"{card.get('title')}: {card.get('approach')}",
            source="winning card",
            why="what repaired it",
        )
        builder.add(
            "diff",
            diff_text(target.repo, str(card["base_sha"]), str(out.commit_sha)),
            source="winning diff",
            why="the repair, generalised into the recipe",
        )
        builder.add("intent", intent, source="verified intent record", why="what to keep")
        builder.exclude("holdout tasks", "invariant I5")
        builder.exclude("surgeon transcripts and self-reports", "role isolation (§5.7)")
        fallback_recipe = RECIPE_FALLBACK
        description = (
            f"State reset on retry: `{binding.get('F')}`, retried by "
            f"`{binding.get('R')}`, rebuilds `{binding.get('S')}` on every attempt, so a "
            "retry repeats the side effects the failed attempt already made."
        )
        answer = consult(
            self.ctx.model,
            case,
            builder,
            "immunologist_recipe",
            schema=RECIPE_SCHEMA,
            fallback=lambda: {"recipe": fallback_recipe, "description": description},
            max_budget_usd=2.0,
            model_id=self.ctx.model_for("immunologist") or MODEL,
        )
        recipe = str(answer.data["recipe"])
        if not all(slot in recipe for slot in ("{S}", "{F}", "{R}")):
            recipe = fallback_recipe  # a recipe must name its bindings, not one host's symbols
        return recipe, str(answer.data["description"])

    # ---- receptor, backfill -------------------------------------------------------------------

    def receptor_precision(self, ab: Antibody) -> ReceptorScore:
        vulnerable = {*ab.lineage.hosts_protected, *self._vulnerable_hosts(ab)}
        error_class = str(ab.signature.fields.get("error_class"))
        return score_receptor(ab.receptor.rule, error_class, vulnerable, self.history)

    def _vulnerable_hosts(self, ab: Antibody) -> set[str]:
        matcher = self.matcher(ab.motif)
        found = set()
        for host in self.ctx.targets:
            try:
                if matcher.matches_any(self.graph(host)):
                    found.add(host)
            except Exception:  # a host without a graph is not evidence either way
                continue
        return found

    def backfill(self, ab: Antibody) -> dict[str, Any]:
        """Retroactive diagnosis: the receptor over recorded history, unclaimed occurrences."""
        pattern = re.compile(ab.receptor.rule)
        hits = [r for r in self.history.records if pattern.search(f'"{r.verdict}"')]
        silent = [r for r in hits if r.incident is None]
        by_host: dict[str, int] = {}
        impact = 0.0
        for r in silent:
            by_host[r.host] = by_host.get(r.host, 0) + 1
            try:
                impact += self.ctx.target(r.host).host.economics.value_per_failed_task_usd
            except KeyError:
                continue
        result = {
            "antibody": ab.ref,
            "scanned": len(self.history.records),
            "hits": len(hits),
            "silent": len(silent),
            "silent_by_host": by_host,
            "silent_traces": sorted(r.trace_id for r in silent)[:50],
            "impact_usd": round(impact, 2),
            "method": "omega-retro-scan (A-29: /detections/{id}/backfills needs the detection "
            "created through the API; the scan reads the same verdict spans Omega recorded)",
        }
        self.library.record(
            ab.id,
            {"event": "backfill", **{k: v for k, v in result.items() if k != "silent_traces"}},
        )
        self._log(
            {
                "type": "antibody",
                "event": "backfill",
                "antibody": ab.ref,
                "silent": len(silent),
                "impact_usd": result["impact_usd"],
            }
        )
        return result

    # ---- scan and the induced trigger ---------------------------------------------------------

    def scan(self, ab: Antibody, *, exclude: Iterable[str] = ()) -> list[str]:
        """Hosts whose current graph matches the motif, minus the protected and ``exclude``."""
        skip = {*ab.lineage.hosts_protected, *exclude, str(ab.evidence.get("host"))}
        matcher = self.matcher(ab.motif)
        found = []
        for host in sorted(self.ctx.targets):
            if (
                host in skip or self.ctx.targets[host].self_repair
            ):  # Omega itself: §6.9, not a vaccine
                continue
            if matcher.matches_any(self.graph(host)):
                found.append(host)
        self.library.record(ab.id, {"event": "scan", "matched": found})
        return found

    def induced_trigger(self, ab: Antibody, host: str) -> Trigger:
        import importlib

        target = self.ctx.target(host)
        g = self.graph(host)
        [embedding, *_] = self.matcher(ab.motif).matches(g) or [None]
        if embedding is None:
            return Trigger(host, (), False, (), {}, None)
        services = tuple(scope_services(target, g, embedding["F"]))
        if not services:
            return Trigger(host, (), False, (), {}, None)
        profile = trigger_profile(services)
        target.make_importable()
        pool = importlib.import_module(f"{target.host.package}.evals.generate").tasks("train")
        tasks = pool[:TRIGGER_TASKS]
        base = rev(target.repo, self.ctx.base_ref)
        classes: dict[str, int] = {}
        with clean_checkout(target.repo, base) as checkout:
            for seed in TRIGGER_SEEDS:
                clean = run_tasks(target, checkout, tasks, seed=seed, phase="trigger").verdicts()
                induced = run_tasks(
                    target, checkout, tasks, seed=seed, profile=profile, phase="trigger"
                ).verdicts()
                failing = sorted(
                    t for t, v in induced.items() if v != "PASS" and clean.get(t) == "PASS"
                )
                for t in failing:
                    classes[induced[t]] = classes.get(induced[t], 0) + 1
                if not failing:
                    continue
                # The G0 cassette prefers the pathogen's own verdict family: a run that only
                # exhausted its retries makes, once repaired, requests the recording never saw
                # (a cassette miss), so no fix could pass G2 on it. Without any such run (Scout:
                # retry exhaustion only), the cassette holds what the trigger did produce.
                family = re.compile(ab.receptor.rule)
                own = [t for t in failing if family.search(f'"{induced[t]}"')]
                ordered = own or failing
                chosen = [x for x in tasks if x["id"] in set(ordered[:5])]
                repro = g0.reproduce(
                    target, checkout, chosen, seed=seed, split="train", profile=profile
                )
                return Trigger(host, services, repro.ok, tuple(ordered), classes, seed, repro)
        return Trigger(host, services, False, (), classes, None)

    # ---- vaccination --------------------------------------------------------------------------

    def record_id(self, ab: Antibody, host: str) -> str:
        return f"VAC-{ab.id}-{host}"

    def prepare(self, ab: Antibody, host: str, trigger: Trigger) -> tuple[CaseFile, c.Card]:
        """The vaccination's case file: what the Surgeon, the Auditor and the shipper read."""
        target = self.ctx.target(host)
        record = self.record_id(ab, host)
        self.ctx.incidents.register(record, host, f"antibody:{ab.ref}")
        case = self.ctx.case(record)
        g = self.graph(host)
        binding = self.bindings(ab, g)[0]
        embedding = self.matcher(ab.motif).matches(g)[0]
        files = sorted(
            {
                str(g.nodes[embedding[k]].get("file"))
                for k in ("Enc", "F", "S")
                if k in embedding and g.nodes[embedding[k]].get("file")
            }
        )
        files = [
            f if target.package_root in ("", ".") else f"{target.package_root}/{f}" for f in files
        ]
        files = [f for f in files if not load_policy().violations([f], ["surgeon"])]
        base = rev(target.repo, self.ctx.base_ref)
        recipe = render_recipe(ab.recipe, binding, intent=_host_intent(g, binding))
        repro = trigger.reproduction
        if repro is None or repro.cassette is None:
            raise SynthesisError(f"{host}: the induced trigger left no cassette")
        description = self.library.index()["antibodies"].get(ab.id, {}).get("description", "")
        case.put("cassette", "g0", repro.cassette.as_dict(), by="immunologist")
        case.put(
            "repro",
            "g0",
            {
                "ok": repro.ok,
                "base_sha": base,
                "seed": trigger.seed,
                "split": "train",
                "profile": "induced-trigger",
                "tasks": list(trigger.failing[:5]),
                "failing": repro.failing,
                "trigger": trigger.as_dict(),
                "cassette_sha": repro.cassette.sha256,
            },
            by="immunologist",
        )
        case.put(
            "symptom",
            "summary",
            {
                "label": f"vaccination with {ab.ref}",
                "summary": (
                    f"Vaccination of `{host}` with {ab.ref}: the induced trigger (429s in "
                    f"{', '.join(trigger.services)} inside the retried scope) failed "
                    f"{len(trigger.failing)} tasks the same seed passes without it "
                    f"({dict(trigger.classes)})."
                ),
            },
            by="immunologist",
        )
        case.put(
            "signature",
            "phi",
            {
                "phi": ab.signature.hash,
                "fields": ab.signature.fields,
                "label": f"vaccination {ab.ref}",
                "trace_ids": [],
            },
            by="immunologist",
        )
        case.put(
            "intent_record",
            "record",
            {
                "stated_goal": f"Apply {ab.ref}: {description}",
                "behavioral_contract": [],
                "checkpoint": None,
                "source": "antibody recipe",
            },
            by="immunologist",
        )
        case.put(
            "plan",
            "plan",
            {
                "budget_usd": VACCINATION_BUDGET_USD,
                "k": 1,
                "plan_sha": _digest({"ab": ab.ref, "host": host}),
                "secondary": True,
            },
            by="immunologist",
        )
        hyp = f"V{ab.version.replace('.', '')}"
        card = {
            "title": f"Vaccinate {host} with {ab.ref}",
            "root_cause": str(description or "state reset on retry"),
            "approach": recipe,
            "intent_constraints": [],
            "allowed_files": files,
            "diff_budget": load_policy().diff_budget_lines,
            "hypothesis": hyp,
            "round": 0,
            "host": host,
            "base_sha": base,
            "antibody": ab.ref,
        }
        case.put("card", hyp, card, by="immunologist")
        return case, c.Card(record, hyp, 0, json.dumps(card, sort_keys=True), base)

    # ---- lifecycle ----------------------------------------------------------------------------

    def promote(self, ab_id: str, host: str, *, why: str) -> Antibody:
        """CANDIDATE (or QUARANTINED after refinement) → MATURED → ACTIVE if the receptor is
        precise enough on history; otherwise it stays MATURED."""
        ab = self.library.load(ab_id)
        if ab.state is AntibodyState.QUARANTINED:
            ab = self.library.transition(
                ab_id, AntibodyState.ACTIVE, why=why, host=host, rematured=True
            )
            self.library.protect(ab_id, host)
            return self.library.load(ab_id)
        if ab.state is AntibodyState.CANDIDATE:
            ab = self.library.transition(ab_id, AntibodyState.MATURED, why=why, host=host)
        self.library.protect(ab_id, host)
        score = self.receptor_precision(self.library.load(ab_id))
        precise = score.precision is not None and score.precision >= MIN_RECEPTOR_PRECISION
        self.library.record(
            ab_id,
            {
                "event": "receptor-precision",
                **score.__dict__,
                "false_positives": list(score.false_positives),
            },
        )
        if precise and ab.state is AntibodyState.MATURED:
            ab = self.library.transition(
                ab_id,
                AntibodyState.ACTIVE,
                why=f"receptor precision {score.precision:.2f} on {score.scanned} recorded runs",
            )
        self._log(
            {
                "type": "antibody",
                "event": "promoted",
                "antibody": ab.ref,
                "state": str(self.library.load(ab_id).state),
                "host": host,
            }
        )
        return self.library.load(ab_id)

    def quarantine(
        self,
        ab_id: str,
        host: str,
        *,
        why: str,
        github: Any = None,
        pr_prefix: str = "",
        more_prefixes: Sequence[str] = (),
    ) -> dict[str, Any]:
        """QUARANTINED: the recipe stops, the receptor stays on, open vaccination PRs are told."""
        ab = self.library.load(ab_id)
        if ab.state is AntibodyState.ACTIVE:
            ab = self.library.transition(
                ab_id,
                AntibodyState.QUARANTINED,
                why=why,
                host=host,
                receptor="still active: the pathogen is real",
            )
        self.library.unprotect(ab_id, host, why=why)
        commented: list[str] = []
        if github is not None:
            notice = (
                f"On hold: {ab.ref} quarantined pending refinement. {why} Its receptor "
                "stays active; recipe application is paused."
            )
            for prefix in dict.fromkeys([pr_prefix, *more_prefixes]):
                for pr in github.pr_list_open(f"{prefix}kintsugi/VAC-{ab_id}-"):
                    github.pr_comment(int(pr["number"]), notice)
                    commented.append(str(pr.get("url", pr["number"])))
        self.library.record(ab_id, {"event": "quarantine-notices", "prs": commented})
        self._log(
            {
                "type": "antibody",
                "event": "quarantined",
                "antibody": ab.ref,
                "host": host,
                "why": why,
                "notified": commented,
            }
        )
        return {
            "antibody": ab.ref,
            "state": str(self.library.load(ab_id).state),
            "notified": commented,
            "receptor_active": True,
        }

    # ---- trace-to-test ------------------------------------------------------------------------

    def regression_cases(self, record: Path, ab: Antibody, why: str) -> list[dict[str, Any]]:
        return [
            {
                "task": task,
                "seed": seed,
                "profile": "background",
                "verdict_when_found": verdict,
                "found_by": why,
                "antibody": ab.ref,
            }
            for seed, task, verdict in failing_runs(record)
        ]

    def regressions_path(self, target: Target, ab: Antibody, label: str) -> str:
        base = package_dir(target)
        return f"{base}/evals/regressions/{cassette_slug(label)}.json"

    # ---- refinement ---------------------------------------------------------------------------

    def refine(
        self,
        ab_id: str,
        negative: tuple[str, nx.DiGraph],
        *,
        case: CaseFile | None = None,
        trigger: str,
        revert_pr: str | None = None,
        trace_to_test: str | None = None,
    ) -> tuple[Antibody, contrastive.Learned]:
        """Learn the precondition that excludes ``negative`` and keeps every positive."""
        ab = self.library.load(ab_id)
        matcher = self.matcher(ab.motif)
        positives = []
        origin_host = str(ab.evidence.get("host"))
        before_sha = str(self.library.evidence(ab_id).get("commits", {}).get("before"))
        known = {f"{origin_host}@pre-fix": self.graph(origin_host, before_sha), **self.positives}
        for host in ab.lineage.hosts_protected:
            if host != origin_host:
                known.setdefault(host, self.graph(host))
        for name, g in known.items():
            found = matcher.matches(g)
            if found:
                positives.append(contrastive.Example(name, g, found[0]))
        neg_name, neg_graph = negative
        neg_matches = matcher.matches(neg_graph)
        if not neg_matches:
            raise SynthesisError(f"{neg_name} does not match {ab.ref}: nothing to learn")
        negatives = [contrastive.Example(neg_name, neg_graph, neg_matches[0])]
        learned = contrastive.learn(ab.motif, positives, negatives)
        problems = contrastive.verify(learned, positives, negatives, predicates=self.registry)
        if not learned.separated or problems:
            raise SynthesisError(f"contrastive learning failed: {problems or 'not separated'}")
        rationale = self._rationale(ab, learned, case)
        revised = ab.revise(
            "precondition",
            motif=learned.motif,
            preconditions=[*ab.preconditions, *learned.preconditions],
        )
        refinement = {
            "trigger": trigger,
            "learned_precondition": list(learned.preconditions),
            "selected": list(learned.selected),
            "table": learned.table,
            "positives": [p.host for p in positives],
            "negatives": [neg_name],
            "rationale": rationale,
            "auditor_check": "re-matched: positives kept, negatives excluded",
            "revert_pr": revert_pr,
            "trace_to_test": trace_to_test,
        }
        self.library.save(
            revised, event={"event": "refined", "from": ab.version, "refinement": refinement}
        )
        lineage = self.library.lineage(ab_id)
        lineage.setdefault("refinements", []).append({"version": revised.version, **refinement})
        (self.library.root / ab_id / "lineage.json").write_text(
            json.dumps(lineage, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        self._log(
            {
                "type": "antibody",
                "event": "refined",
                "antibody": revised.ref,
                "precondition": list(learned.preconditions),
            }
        )
        return revised, learned

    def _rationale(self, ab: Antibody, learned: contrastive.Learned, case: CaseFile | None) -> str:
        fallback = (
            f"Every positive keeps '{'; '.join(learned.preconditions)}', and every negative lacks "
            "it: the retried scope's repeated work is only harmful when it repeats a side "
            "effect; a scope whose accumulator only holds reads can start over safely."
        )
        if case is None:
            return fallback
        builder = ContextBuilder("immunologist", case.incident)
        builder.bind("antibody", ab.ref)
        builder.add(
            "table",
            json.dumps(learned.table, indent=1, sort_keys=True),
            source="candidate truth table",
            why="what separates the matches",
        )
        builder.add(
            "selected",
            ", ".join(learned.preconditions),
            source="greedy set cover",
            why="the learned precondition",
        )
        answer = consult(
            self.ctx.model,
            case,
            builder,
            "immunologist_rationale",
            schema=RATIONALE_SCHEMA,
            fallback=lambda: {"rationale": fallback},
            max_budget_usd=1.0,
            model_id=self.ctx.model_for("immunologist") or MODEL,
        )
        return str(answer.data["rationale"])


def render_recipe(recipe: str, binding: Mapping[str, str], *, intent: str) -> str:
    out = recipe
    for slot, value in {**binding, "intent": intent}.items():
        out = out.replace("{" + slot + "}", value)
    return out


def _host_intent(g: nx.DiGraph, binding: Mapping[str, str]) -> str:
    """The bound code's own documentation: what the recipe must not contradict."""
    for _node, data in g.nodes(data=True):
        if data.get("qualified_name") == binding.get("S"):
            source = str(data.get("source") or "")
            doc = re.search(r'"""(.*?)"""', source, re.DOTALL)
            if doc:
                return " ".join(doc.group(1).split())
    return "the documented behaviour of the code you change"


def _digest(data: Any) -> str:
    raw = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()
