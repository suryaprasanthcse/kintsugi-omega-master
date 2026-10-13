"""`kintsugi audit`: checks C1–C14 over the autonomy window W (spec §7.5, amendment A-67).

Every check is a pure function over explicit inputs, so the mutation suite can hand it a clean
fixture and a tampered one. :func:`run_audit` collects those inputs: from a clone of the
repository (the ``ledger`` branch, ``anchor-*`` tags, checkpoint refs, ``sabotage-log/``, the
Antibody Library, the holdout manifest) and, online, from the GitHub and neatlogs APIs. Offline
(``--offline DIR``) it reads a published bundle directory and the clone, and never the network.

A check whose inputs are unavailable is SKIP, never PASS. Exit codes (§7.5): 0 every selected
check passes · 1 at least one fails · 2 none fails but at least one was skipped.

The window record (``.kintsugi/window.json``, committed at T0; A-67) fixes what W is: T0 and T1,
the protected branch and its SHA at T0, the T0 attestation public key (the genesis input), the
holdout manifest digest at T0, the ruleset ids and the G7 check name.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from kintsugi.gates.protected_paths import Policy, load_policy
from kintsugi.memory.antibody import LIFECYCLE, AntibodyState, AntibodyVersion
from kintsugi.memory.library import AntibodyLibrary, LibraryError
from kintsugi.proof import attest, commit, ledger, sealed
from kintsugi.proof.anchor import TAG_PREFIX, Anchor, verify_anchor
from kintsugi.sandbox import proc
from kintsugi.senses.completeness import ReportedHit, completeness
from kintsugi.senses.store import SeenHit

CHECKS = tuple(f"C{i}" for i in range(1, 15))
TITLES = {
    "C1": "ledger chain",
    "C2": "attested commits",
    "C3": "signatures",
    "C4": "checkpoint linkage",
    "C5": "prompt provenance",
    "C6": "sealed sabotage",
    "C7": "independent CI",
    "C8": "trace existence",
    "C9": "completeness",
    "C10": "cost reconciliation",
    "C11": "antibody lineage",
    "C12": "rulesets",
    "C13": "holdout integrity",
    "C14": "self-repair scope",
}
WINDOW_FILE = ".kintsugi/window.json"
REVEAL_SUFFIX = ".reveal.json"  # sabotage-log/<id>.reveal.json = {spec, salt} (§7.2 step 6)
SELF_REPAIR_ROLE = "self-repair"
COST_TOLERANCE = 0.01
EXIT_OK, EXIT_FAIL, EXIT_SKIPPED = 0, 1, 2


class Status(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass(frozen=True)
class CheckResult:
    check: str
    status: Status
    summary: str
    problems: tuple[str, ...] = ()

    @property
    def title(self) -> str:
        return TITLES[self.check]

    def as_dict(self) -> dict[str, Any]:
        return {"check": self.check, "title": self.title, "status": str(self.status),
                "summary": self.summary, "problems": list(self.problems)}  # fmt: skip


def verdict(check: str, summary: str, problems: Iterable[str] = ()) -> CheckResult:
    found = tuple(problems)
    return CheckResult(check, Status.FAIL if found else Status.PASS, summary, found)


def skip(check: str, why: str) -> CheckResult:
    return CheckResult(check, Status.SKIP, f"skipped: {why}")


def _at(text: str) -> datetime:
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# ---- the window record ---------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """What W is (A-67). ``t1`` is None while the window is still open."""

    t0: str
    repo: str
    branch: str
    base: str
    attest_pubkey: str
    holdout_manifest: str
    rulesets: tuple[int, ...] = ()
    g7_check: str = "G7"
    ledger_ref: str = "ledger"
    t1: str | None = None

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Window:
        return cls(
            t0=str(data["t0"]), repo=str(data["repo"]), branch=str(data.get("branch", "main")),
            base=str(data["base"]), attest_pubkey=str(data["attest_pubkey"]),
            holdout_manifest=str(data["holdout_manifest"]),
            rulesets=tuple(int(r) for r in data.get("rulesets") or ()),
            g7_check=str(data.get("g7_check", "G7")),
            ledger_ref=str(data.get("ledger_ref", "ledger")),
            t1=str(data["t1"]) if data.get("t1") else None,
        )  # fmt: skip

    @property
    def start(self) -> datetime:
        return _at(self.t0)

    def end(self, now: datetime | None = None) -> datetime:
        return _at(self.t1) if self.t1 else (now or datetime.now(UTC))

    @property
    def closed(self) -> bool:
        return self.t1 is not None

    def public_key(self) -> Ed25519PublicKey:
        return attest.load_public_key(self.attest_pubkey)

    def genesis(self) -> str:
        raw = self.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return ledger.genesis_hash(self.t0, raw)


# ---- C1 ------------------------------------------------------------------------------------


def c1_ledger(
    entries: Sequence[Mapping[str, Any]],
    genesis: str,
    anchors: Sequence[Anchor],
    public_key: Ed25519PublicKey,
    bundles: Mapping[str, str] | None = None,
) -> CheckResult:
    """The chain from h_0, every anchor's signature and hash, and every published bundle's
    digest against the ledger entry that recorded it."""
    problems: list[str] = []
    report = ledger.verify(entries, genesis, anchors={a.seq: a.h for a in anchors})
    if not report.ok:
        problems.append(f"seq {report.first_bad_seq}: {report.reason}")
    seen_tags: set[str] = set()
    for anchor in anchors:
        if not verify_anchor(anchor, public_key):
            problems.append(f"{anchor.tag}: signature does not verify against the T0 key")
        if anchor.tag in seen_tags:
            problems.append(f"{anchor.tag}: anchored twice")
        seen_tags.add(anchor.tag)
    if entries and not anchors:
        problems.append("no anchor covers the ledger")
    # E.2 names the field ``bundle_sha256``; ``bundle`` is the earlier spelling, still accepted.
    recorded = {(str(e.get("incident")), str(e.get("bundle_sha256") or e.get("bundle")))
                for e in entries if e.get("bundle_sha256") or e.get("bundle")}  # fmt: skip
    for incident, digest in sorted((bundles or {}).items()):
        if (incident, digest) not in recorded:
            problems.append(f"{incident}: bundle {digest[:19]}… is not the one the ledger records")
    return verdict("C1", f"{len(entries)} entries, {len(anchors)} anchors", problems)


# ---- C2–C5 ---------------------------------------------------------------------------------


def _git(repo: Path, *args: str, check: bool = True) -> proc.Result:
    result = proc.run(["git", "-C", os.fspath(repo), *args], timeout_s=120)
    if check and not result.ok:
        raise RuntimeError(f"git {args[0]} failed: {result.stderr.strip()[-300:]}")
    return result


def merge_problem(repo: Path, sha: str, parents: Sequence[str]) -> str | None:
    """A merge commit carries no attestation of its own (A-51). It is accepted only as git's own
    conflict-free merge of its two parents, so every line it brings comes from a parent."""
    if len(parents) != 2:
        return f"a merge of {len(parents)} parents"
    merged = _git(repo, "merge-tree", "--write-tree", "--no-messages", *parents, check=False)
    if merged.returncode != 0:
        return "the parents do not merge cleanly"
    tree = _git(repo, "rev-parse", f"{sha}^{{tree}}").stdout.strip()
    if merged.stdout.split()[0] != tree:
        return "the merge tree differs from git's merge of its parents"
    return None


@dataclass
class Autonomy:
    results: dict[str, CheckResult]
    human_commits: int


def c2_c5(repo: Path, commits: Sequence[str], public_key: Ed25519PublicKey) -> Autonomy:
    bad: dict[str, list[str]] = {key: [] for key in ("C2", "C3", "C4", "C5")}
    attested = merges = human = 0
    for sha in commits:
        parents = _git(repo, "rev-list", "--parents", "-n", "1", sha).stdout.split()[1:]
        if len(parents) > 1:
            merges += 1
            problem = merge_problem(repo, sha, parents)
            if problem:
                bad["C2"].append(f"{sha[:8]} (merge): {problem}")
            continue
        attested += 1
        report = commit.verify_commit(repo, sha, public_key=public_key)
        if report.problems.get("C2"):
            human += 1
            bad["C2"].append(f"{sha[:8]}: {'; '.join(report.problems['C2'])}")
            for key in ("C3", "C4", "C5"):
                bad[key].append(f"{sha[:8]}: not attested")
            continue
        for key in ("C3", "C4", "C5"):
            if report.problems.get(key):
                bad[key].append(f"{sha[:8]}: {'; '.join(report.problems[key])}")
    results = {
        "C2": verdict("C2", f"{attested - human}/{attested} commits in W"
                      + (f", {merges} clean merges" if merges else ""), bad["C2"]),
        "C3": verdict("C3", f"{attested - len(bad['C3'])}/{attested} valid (ed25519)", bad["C3"]),
        "C4": verdict("C4", f"{attested - len(bad['C4'])}/{attested} nonce found in transcript",
                      bad["C4"]),
        "C5": verdict("C5", f"{attested - len(bad['C5'])}/{attested}", bad["C5"]),
    }  # fmt: skip
    return Autonomy(results, human)


# ---- C6 ------------------------------------------------------------------------------------


def c6_sabotage(
    commitments: Sequence[Mapping[str, Any]],
    reveals: Mapping[str, Mapping[str, Any]],
    entries: Sequence[Mapping[str, Any]],
    honeytoken_hits: Callable[[str], Sequence[Any]],
    *,
    window_closed: bool,
) -> CheckResult:
    """Each commitment matches its reveal; it was sealed (in the ledger) before it was delivered,
    and not delivered before ``not_before``; its honeytoken never reached a checkpoint."""
    problems: list[str] = []
    unrevealed: list[str] = []
    hits_total = 0
    for record in commitments:
        sid = str(record.get("id"))
        reveal = reveals.get(sid)
        if reveal is None:
            unrevealed.append(sid)
            continue
        spec = dict(reveal.get("spec") or {})
        try:
            salt = bytes.fromhex(str(reveal.get("salt", "")))
        except ValueError:
            salt = b""
        if not sealed.verify_reveal(str(record.get("c")), spec, salt):
            problems.append(f"{sid}: the reveal does not open the commitment")
            continue
        if spec.get("id") != sid:
            problems.append(f"{sid}: the revealed spec is {spec.get('id')!r}")
        try:
            family = sealed.class_family(str(spec.get("class")))
        except ValueError:
            family = None
        if family != record.get("class_family"):
            problems.append(f"{sid}: published class family {record.get('class_family')!r} "
                            f"is not the revealed class's {family!r}")  # fmt: skip
        problems += _sabotage_order(sid, record, entries)
        hits = list(honeytoken_hits(str(spec.get("honeytoken", ""))))
        hits_total += len(hits)
        if not spec.get("honeytoken"):
            problems.append(f"{sid}: the spec has no honeytoken")
        elif hits:
            problems.append(f"{sid}: honeytoken found in {len(hits)} checkpoint file(s)")
    if unrevealed and window_closed:
        problems.append(f"not revealed after W: {unrevealed}")
    summary = f"{len(commitments) - len(unrevealed)}/{len(commitments)} commitments, " \
              f"{hits_total} honeytoken hits"  # fmt: skip
    if unrevealed and not window_closed and not problems:
        return CheckResult("C6", Status.SKIP, f"skipped: {summary}; W is open, "
                           f"{len(unrevealed)} sealed until the reveal")  # fmt: skip
    return verdict("C6", summary, problems)


def _sabotage_order(
    sid: str, record: Mapping[str, Any], entries: Sequence[Mapping[str, Any]]
) -> list[str]:
    mine = [e for e in entries if e.get("type") == "sabotage" and e.get("sabotage") == sid]
    sealed_at = [e for e in mine if e.get("event") == "sealed"]
    delivered = [e for e in mine if e.get("event") == "delivered"]
    problems: list[str] = []
    if not sealed_at:
        return [f"{sid}: no ledger entry records the seal"]
    if sealed_at[0].get("c") != record.get("c"):
        problems.append(f"{sid}: the ledger sealed a different commitment")
    if _at(str(record["sealed_at"])) > _at(str(record["not_before"])):
        problems.append(f"{sid}: sealed after its own not_before")
    for entry in delivered:
        if int(entry["seq"]) < int(sealed_at[0]["seq"]):
            problems.append(f"{sid}: delivered (seq {entry['seq']}) before it was sealed")
        if _at(str(entry["at"])) < _at(str(record["not_before"])):
            problems.append(f"{sid}: delivered before not_before")
    return problems


# ---- C7–C10, C12 (online) -------------------------------------------------------------------


def c7_ci(
    prs: Mapping[str, str],
    checks: Callable[[str], Sequence[Mapping[str, Any]]] | None,
    g7_check: str,
) -> CheckResult:
    """Every PR the ledger records in W has its G7 run, and it passed. ``prs``: url → label."""
    if checks is None:
        return skip("C7", "needs the GitHub Actions API")
    problems: list[str] = []
    for url, label in sorted(prs.items()):
        runs = [k for k in checks(url) if g7_check in str(k.get("name", ""))
                and str(k.get("bucket")) != "skipping"]  # fmt: skip
        if not runs:
            problems.append(f"{label} {url}: no G7 run")
        elif not all(str(k.get("bucket")) == "pass" for k in runs):
            problems.append(f"{label} {url}: G7 did not pass")
    return verdict("C7", f"{len(prs) - len(problems)}/{len(prs)} runs green", problems)


def c8_traces(
    wanted: Mapping[str, tuple[str, datetime, datetime]],
    started_at: Callable[[str], datetime | None] | None,
) -> CheckResult:
    """``wanted``: trace id → (incident, earliest, latest). A trace must exist in neatlogs and
    have started inside its incident's window."""
    if started_at is None:
        return skip("C8", "needs a neatlogs read token")
    problems: list[str] = []
    for trace_id, (incident, lo, hi) in sorted(wanted.items()):
        found = started_at(trace_id)
        if found is None:
            problems.append(f"{incident}: trace {trace_id} does not exist")
        elif not lo <= found <= hi:
            problems.append(f"{incident}: trace {trace_id} started {found.isoformat()}, "
                            "outside the incident window")  # fmt: skip
    return verdict("C8", f"{len(wanted) - len(problems)}/{len(wanted)}", problems)


def c9_completeness(
    reported: Iterable[ReportedHit] | None,
    seen: Mapping[str, SeenHit] | None,
    start: datetime,
    end: datetime,
    ledger_incidents: set[str],
) -> CheckResult:
    """Every hit in W was claimed and decided (I7), and every incident it maps to is in the
    ledger."""
    if reported is None or seen is None:
        return skip("C9", "needs the detection hits (neatlogs or a published hits.json)")
    report = completeness(reported, seen, start, end)
    problems = [f"hit {key}: never claimed" for key in report.missing]
    problems += [f"hit {key}: claimed but never decided" for key in report.pending]
    problems += [f"{inc}: maps hits but is not in the ledger"
                 for inc in sorted(report.incidents - ledger_incidents)]  # fmt: skip
    summary = f"{report.total} hits → {len(report.incidents)} incidents + " \
              f"{report.attached + report.held} dedupes"  # fmt: skip
    return verdict("C9", summary, problems)


def c10_cost(actual_usd: float, sessions: Mapping[str, float | None] | None) -> CheckResult:
    """The cost actuals Omega recorded equal the neatlogs session costs within 1%."""
    if sessions is None:
        return skip("C10", "needs a neatlogs read token")
    problems = [f"session {sid} is unknown to neatlogs" for sid, v in sessions.items() if v is None]
    total = sum(v for v in sessions.values() if v is not None)
    delta = abs(actual_usd - total) / total if total else (0.0 if not actual_usd else 1.0)
    if delta > COST_TOLERANCE:
        problems.append(f"actual ${actual_usd:.4f} vs neatlogs ${total:.4f} (Δ {delta:.1%})")
    return verdict("C10", f"Δ {delta:.1%} over {len(sessions)} sessions", problems)


def _covers(include: Iterable[str], branch: str) -> bool:
    names = set(include)
    return bool(names & {f"refs/heads/{branch}", "~ALL"}) or (
        branch == "main" and "~DEFAULT_BRANCH" in names
    )


def c12_rulesets(
    histories: Mapping[int, Sequence[Mapping[str, Any]]] | None,
    start: datetime,
    end: datetime,
    branch: str,
) -> CheckResult:
    """Each ruleset recorded at T0 was active on the branch for all of W. A history version is
    ``{"updated_at", "enforcement", "include": [ref patterns]}``; the state at time t is the
    newest version not after t."""
    if histories is None:
        return skip("C12", "needs the GitHub rulesets API")
    problems: list[str] = []
    if not histories:
        problems.append("no ruleset was recorded at T0")
    for rid, versions in sorted(histories.items()):
        ordered = sorted(versions, key=lambda v: _at(str(v["updated_at"])))
        before = [v for v in ordered if _at(str(v["updated_at"])) <= start]
        during = [v for v in ordered if start < _at(str(v["updated_at"])) <= end]
        for v in ([before[-1]] if before else []) + during:
            if v.get("enforcement") != "active" or not _covers(v.get("include") or (), branch):
                problems.append(f"ruleset {rid}: not active on {branch} from {v['updated_at']}")
        if not before:
            problems.append(f"ruleset {rid}: did not exist at T0")
    hours = (end - start).total_seconds() / 3600
    return verdict("C12", f"{len(histories)} rulesets, active {hours:.2f}h", problems)


# ---- C11, C13, C14 (offline) ----------------------------------------------------------------


def _vkey(version: str) -> tuple[int, int, int]:
    v = AntibodyVersion.parse(version)
    return (v.major, v.minor, v.patch)


def lineage_problems(library: AntibodyLibrary, ab_id: str) -> list[str]:
    """Replay ``lineage.json`` and compare it with the antibody, its version files and the
    index: versions, the state machine, quarantines, protected hosts and evidence links."""
    try:
        ab = library.load(ab_id)
    except LibraryError as error:
        return [str(error)]
    lineage = library.lineage(ab_id)
    events: list[dict[str, Any]] = list(lineage.get("events") or [])
    if not events or events[0].get("event") != "synthesized":
        return [f"{ab_id}: the lineage does not start with its synthesis"]
    problems: list[str] = []
    versions = [str(v) for v in lineage.get("versions") or []]
    if versions != list(ab.lineage.versions):
        problems.append(f"{ab_id}: lineage versions {versions} ≠ antibody {ab.lineage.versions}")
    folder = library.root / ab_id / "versions"
    on_disk = sorted((p.stem for p in folder.glob("*.yaml")), key=_vkey)
    if on_disk != sorted(versions, key=_vkey):
        problems.append(f"{ab_id}: version files {on_disk} ≠ lineage {versions}")
    if not versions or versions[-1] != ab.version:
        problems.append(f"{ab_id}: current version {ab.version} is not the lineage's last")
    entry = library.index().get("antibodies", {}).get(ab_id, {})
    if entry.get("version") != ab.version or entry.get("state") != str(ab.state):
        problems.append(f"{ab_id}: index says {entry.get('version')}/{entry.get('state')}")
    state = AntibodyState(str(events[0].get("state", "CANDIDATE")))
    current = str(events[0].get("version"))
    last_at = _at(str(events[0]["at"]))
    quarantines = 0
    evidence = library.evidence(ab_id)
    hosts = {str(evidence["host"])} if evidence.get("host") else set()
    refined: set[str] = set()
    for event in events[1:]:
        kind, version = event.get("event"), str(event.get("version"))
        at = _at(str(event["at"]))
        if at < last_at:
            problems.append(f"{ab_id}: event {kind} at {event['at']} goes back in time")
        last_at = at
        if kind == "refined":
            if event.get("from") != current or _vkey(version) <= _vkey(current):
                problems.append(f"{ab_id}: refinement {event.get('from')} → {version} "
                                f"does not follow {current}")  # fmt: skip
            current = version
            refined.add(version)
        elif version != current:
            problems.append(f"{ab_id}: {kind} event for {version} while {current} is current")
        if kind == "state":
            source, target = AntibodyState(str(event["from"])), AntibodyState(str(event["to"]))
            if source is not state:
                problems.append(f"{ab_id}: state event from {source} but the replay is {state}")
            if target not in LIFECYCLE[source]:
                problems.append(f"{ab_id}: {source} → {target} is not a lifecycle transition")
            state = target
            quarantines += target is AntibodyState.QUARANTINED
        elif kind == "protected":
            hosts.add(str(event["host"]))
        elif kind == "unprotected":
            hosts.discard(str(event["host"]))
    if state is not ab.state:
        problems.append(f"{ab_id}: the replayed state {state} ≠ the antibody's {ab.state}")
    if quarantines != ab.lineage.quarantines or quarantines != lineage.get("quarantines", 0):
        problems.append(f"{ab_id}: {quarantines} quarantines replayed, "
                        f"{ab.lineage.quarantines} recorded")  # fmt: skip
    if hosts != set(ab.lineage.hosts_protected):
        problems.append(f"{ab_id}: replayed hosts {sorted(hosts)} ≠ {ab.lineage.hosts_protected}")
    missing = [v for v in versions[1:] if v not in refined]
    if missing:
        problems.append(f"{ab_id}: versions {missing} have no refinement event")
    for refinement in lineage.get("refinements") or []:
        if str(refinement.get("version")) not in refined:
            problems.append(f"{ab_id}: refinement {refinement.get('version')} has no event")
    if evidence.get("origin_incident") != events[0].get("incident"):
        problems.append(f"{ab_id}: evidence names {evidence.get('origin_incident')}, the "
                        f"synthesis {events[0].get('incident')}")  # fmt: skip
    return problems


def c11_lineage(library: AntibodyLibrary, ids: Sequence[str] | None = None) -> CheckResult:
    chosen = list(ids) if ids is not None else library.ids()
    problems = [p for ab_id in chosen for p in lineage_problems(library, ab_id)]
    quarantines = 0
    for ab_id in chosen:
        try:
            quarantines += library.load(ab_id).lineage.quarantines
        except LibraryError:
            continue
    summary = f"{len(chosen)} antibodies, {quarantines} quarantine" + ("s" * (quarantines != 1))
    return verdict("C11", summary, problems)


def manifest_digest(repo: Path) -> str:
    from kintsugi.gates.holdout import MANIFEST

    return "sha256:" + hashlib.sha256((Path(repo) / MANIFEST).read_bytes()).hexdigest()


def c13_holdout(repo: Path, recorded: str | None) -> CheckResult:
    """Every sealed holdout file hashes to the manifest, and the manifest is byte-identical to
    the one recorded at T0."""
    from kintsugi.gates import holdout

    problems = holdout.verify(Path(repo))
    lines = [ln for ln in holdout._manifest_lines(Path(repo) / holdout.MANIFEST)
             if not ln.startswith("#")]  # fmt: skip
    summary = f"{len(lines) - len(problems)}/{len(lines)} manifests"
    if recorded is None:
        if problems:
            return verdict("C13", summary, problems)
        return CheckResult("C13", Status.SKIP, f"skipped: {summary} match; no T0 record")
    if (Path(repo) / holdout.MANIFEST).is_file() and manifest_digest(repo) != recorded:
        problems.append("the holdout manifest changed since T0")
    return verdict("C13", summary, problems)


def c14_self_repair(changes: Mapping[str, Sequence[str]], policy: Policy) -> CheckResult:
    """``changes``: each self-repair commit in W → its changed paths."""
    problems = [f"{sha[:8]}: touches {v.path} ({v.pattern})"
                for sha, paths in sorted(changes.items())
                for v in policy.violations(paths, ["self_repair"])]  # fmt: skip
    clean = len(changes) - len({p.split(":")[0] for p in problems})
    return verdict("C14", f"{clean}/{len(changes)}", problems)


# ---- collecting the inputs ------------------------------------------------------------------


@dataclass
class Selection:
    checks: tuple[str, ...] = CHECKS
    incident: str | None = None
    sabotage: str | None = None
    antibody: str | None = None


@dataclass
class Online:
    """Read-only API access; a None port makes its checks SKIP."""

    pr_checks: Callable[[str], Sequence[Mapping[str, Any]]] | None = None
    ruleset_history: Callable[[int], Sequence[Mapping[str, Any]]] | None = None
    trace_started: Callable[[str], datetime | None] | None = None
    session_cost: Callable[[str], float | None] | None = None
    hits: Callable[[datetime], tuple[list[ReportedHit], dict[str, SeenHit]]] | None = None


@dataclass
class AuditReport:
    results: list[CheckResult] = field(default_factory=list)
    human_commits: int | None = None

    @property
    def exit_code(self) -> int:
        if any(r.status is Status.FAIL for r in self.results):
            return EXIT_FAIL
        if any(r.status is Status.SKIP for r in self.results):
            return EXIT_SKIPPED
        return EXIT_OK

    def as_json(self) -> dict[str, Any]:
        return {"exit_code": self.exit_code, "human_commits": self.human_commits,
                "checks": [r.as_dict() for r in self.results]}  # fmt: skip

    def render(self) -> str:
        lines = []
        for r in self.results:
            label = f"{r.check:<4}{r.title} "
            lines.append(f"{label:.<42} {r.summary:<44} {r.status}")
            lines += [f"      - {p}" for p in r.problems[:20]]
            if len(r.problems) > 20:
                lines.append(f"      … {len(r.problems) - 20} more")
        if self.human_commits is not None:
            lines.append(f"HUMAN COMMITS IN WINDOW: {self.human_commits}")
        return "\n".join(lines)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _ref_exists(repo: Path, ref: str) -> bool:
    return _git(repo, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}", check=False).ok


def _first_ref(repo: Path, names: Sequence[str]) -> str | None:
    return next((n for n in names if _ref_exists(repo, n)), None)


def load_window(repo: Path, offline: Path | None) -> Window | None:
    data = _read_json(offline / "window.json") if offline else None
    data = data or _read_json(Path(repo) / WINDOW_FILE)
    return Window.from_json(data) if data else None


def load_ledger(repo: Path, window: Window, offline: Path | None) -> list[dict[str, Any]] | None:
    if offline and (offline / "ledger.jsonl").is_file():
        text = (offline / "ledger.jsonl").read_text(encoding="utf-8")
    else:
        ref = _first_ref(repo, [f"origin/{window.ledger_ref}", window.ledger_ref])
        if ref is None:
            return None
        text = _git(repo, "show", f"{ref}:ledger.jsonl").stdout
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def load_anchors(repo: Path, offline: Path | None) -> list[Anchor]:
    if offline and (offline / "anchors.json").is_file():
        return [Anchor.from_dict(a) for a in _read_json(offline / "anchors.json")]
    out = _git(repo, "for-each-ref", f"refs/tags/{TAG_PREFIX}*",
               "--format=%(refname:short)%00%(contents)%01").stdout  # fmt: skip
    anchors = []
    for record in out.split("\x01"):
        tag, _, message = record.strip().partition("\x00")
        if tag:
            anchors.append(Anchor.from_message(tag, message))
    return sorted(anchors, key=lambda a: a.seq)


def window_commits(repo: Path, window: Window, end: datetime) -> list[str]:
    """Commits on the protected branch (and pinned attempts, plan §2a L1) after the T0 commit
    (the first that added the window record; else ``base``) whose commit time is inside W,
    oldest first."""
    branch = _first_ref(repo, [f"origin/{window.branch}", window.branch])
    heads = [branch] if branch else []
    heads += _git(repo, "for-each-ref", "refs/kintsugi/attempts/",
                  "--format=%(refname)").stdout.split()  # fmt: skip
    if not heads:
        return []
    base = window.base
    if branch:
        added = _git(repo, "log", "--diff-filter=A", "--format=%H", branch, "--",
                     WINDOW_FILE).stdout.split()  # fmt: skip
        base = added[-1] if added else base
    out = _git(repo, "log", "--reverse", "--format=%H %ct", *heads, f"^{base}").stdout
    lo, hi = window.start.timestamp(), end.timestamp()
    return [sha for sha, ct in (line.split() for line in out.splitlines() if line.strip())
            if lo <= int(ct) <= hi]  # fmt: skip


def _bundles(root: Path | None) -> dict[str, dict[str, Any]]:
    from kintsugi.proof.bundle import BUNDLE_FILE, file_sha256

    found: dict[str, dict[str, Any]] = {}
    if root is None or not root.is_dir():
        return found
    for path in sorted(root.glob(f"*/{BUNDLE_FILE}")):
        data = json.loads(path.read_bytes())
        found[str(data.get("incident"))] = {"digest": file_sha256(path), "data": data}
    return found


def _incident_windows(
    entries: Sequence[Mapping[str, Any]], start: datetime, end: datetime
) -> dict[str, tuple[datetime, datetime]]:
    """Incident → (T0, its last ledger entry): a bundle's traces all precede the close."""
    last: dict[str, datetime] = {}
    for entry in entries:
        if entry.get("incident"):
            at = _at(str(entry["at"]))
            last[str(entry["incident"])] = max(at, last.get(str(entry["incident"]), at))
    return {inc: (start, min(at, end)) for inc, at in last.items()}


def run_audit(
    repo: Path,
    *,
    offline: Path | None = None,
    selection: Selection | None = None,
    online: Online | None = None,
    library_root: Path | None = None,
    policy: Policy | None = None,
    now: datetime | None = None,
) -> AuditReport:
    repo = Path(repo)
    selection = selection or Selection()
    online = Online() if offline is not None or online is None else online
    report = AuditReport()
    want = set(selection.checks)
    results: dict[str, CheckResult] = {}
    library = AntibodyLibrary(library_root or repo / "antibodies")
    if "C11" in want:
        ids = [selection.antibody] if selection.antibody else None
        results["C11"] = c11_lineage(library, ids)
    window = load_window(repo, offline)
    if window is None:
        why = f"no window record ({WINDOW_FILE}): T0 has not happened"
        for check in sorted(want - set(results), key=CHECKS.index):
            if check == "C13":
                results[check] = c13_holdout(repo, None)
            elif check == "C14":
                results[check] = c14_self_repair({}, policy or load_policy())
            else:
                results[check] = skip(check, why)
        report.results = [results[c] for c in CHECKS if c in results]
        return report
    start, end = window.start, window.end(now)
    entries = load_ledger(repo, window, offline)
    bundle_root = offline if offline is not None else repo / "published-bundles"
    bundles = _bundles(bundle_root)
    if offline is None:  # online, in the clone: bundles the crew wrote but nobody published yet
        bundles = {**_bundles(repo / "evidence"), **bundles}
    if selection.incident:
        bundles = {k: v for k, v in bundles.items() if k == selection.incident}
    if "C1" in want:
        results["C1"] = skip("C1", "no ledger branch") if entries is None else c1_ledger(
            entries, window.genesis(), load_anchors(repo, offline), window.public_key(),
            {inc: b["digest"] for inc, b in bundles.items()},
        )  # fmt: skip
    commits: list[str] | None = None
    if want & {"C2", "C3", "C4", "C5", "C14"}:
        commits = window_commits(repo, window, end)
    if want & {"C2", "C3", "C4", "C5"} and commits is not None:
        autonomy = c2_c5(repo, commits, window.public_key())
        results.update({k: v for k, v in autonomy.results.items() if k in want})
        report.human_commits = autonomy.human_commits
    entries = entries or []
    if "C6" in want:
        results["C6"] = _run_c6(repo, offline, entries, window, selection.sabotage)
    prs = {str(e["pr"]): str(e.get("incident") or e.get("antibody") or e.get("type"))
           for e in entries if e.get("pr") and start <= _at(str(e["at"])) <= end}  # fmt: skip
    if "C7" in want:
        results["C7"] = c7_ci(prs, online.pr_checks, window.g7_check)
    if "C8" in want:
        spans = _incident_windows(entries, start, end)
        wanted = {tid: (inc, *spans.get(inc, (start, end)))
                  for inc, b in bundles.items()
                  for tids in (b["data"].get("traces") or {}).values() for tid in tids}  # fmt: skip
        results["C8"] = c8_traces(wanted, online.trace_started)
    if "C9" in want:
        reported, seen = _hits(offline, online, start)
        incidents = {str(e["incident"]) for e in entries if e.get("incident")}
        results["C9"] = c9_completeness(reported, seen, start, end, incidents)
    if "C10" in want:
        actual = sum(float((b["data"].get("cost") or {}).get("actual_usd", 0.0))
                     for b in bundles.values())  # fmt: skip
        sids = sorted({str(s) for b in bundles.values()
                       for s in (b["data"].get("cost") or {}).get("sessions") or []})  # fmt: skip
        costs = None if online.session_cost is None else {s: online.session_cost(s) for s in sids}
        results["C10"] = c10_cost(actual, costs)
    if "C12" in want:
        histories = None
        if online.ruleset_history is not None:
            histories = {rid: online.ruleset_history(rid) for rid in window.rulesets}
        results["C12"] = c12_rulesets(histories, start, end, window.branch)
    if "C13" in want:
        results["C13"] = c13_holdout(repo, window.holdout_manifest)
    if "C14" in want:
        repairs = _self_repairs(repo, commits or [])
        results["C14"] = c14_self_repair(repairs, policy or load_policy())
    report.results = [results[c] for c in CHECKS if c in results]
    return report


def _run_c6(
    repo: Path, offline: Path | None, entries: Sequence[Mapping[str, Any]], window: Window,
    only: str | None,
) -> CheckResult:  # fmt: skip
    root = offline if offline is not None else repo
    log = sorted((root / "sabotage-log").glob("*.json"))
    commitments = [json.loads(p.read_text(encoding="utf-8"))
                   for p in log if not p.name.endswith(REVEAL_SUFFIX)]  # fmt: skip
    reveals = {str(r["spec"]["id"]): r for p in log if p.name.endswith(REVEAL_SUFFIX)
               if (r := json.loads(p.read_text(encoding="utf-8"))).get("spec")}  # fmt: skip
    if only:
        commitments = [c for c in commitments if c.get("id") == only]
    if not commitments:
        return skip("C6", "no sabotage commitments in sabotage-log/")
    from kintsugi.proof.redaction import honeytoken_hits

    return c6_sabotage(commitments, reveals, entries, lambda token: honeytoken_hits(repo, token),
                       window_closed=window.closed)  # fmt: skip


def _hits(
    offline: Path | None, online: Online, start: datetime
) -> tuple[list[ReportedHit] | None, dict[str, SeenHit] | None]:
    if offline is not None:
        data = _read_json(offline / "hits.json")
        if not data:
            return None, None
        reported = [
            ReportedHit(h["detection_id"], h["trace_id"], h["span_id"], _at(h["detected_at"]))
            for h in data.get("reported", [])
        ]
        seen = {s["hit_key"]: SeenHit(**s) for s in data.get("seen", [])}
        return reported, seen
    if online.hits is None:
        return None, None
    return online.hits(start)


def _self_repairs(repo: Path, commits: Sequence[str]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for sha in commits:
        message = _git(repo, "log", "-1", "--format=%B", sha).stdout
        roles = attest.parse_trailers(message).get("Kintsugi-Role") or []
        if any(r.split("/")[0] == SELF_REPAIR_ROLE for r in roles):
            out = _git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", sha).stdout
            found[sha] = [p for p in out.split("\0") if p]
    return found


# ---- the online ports -----------------------------------------------------------------------


def github_ports(window: Window, token: str | None) -> Online:
    """GitHub-backed ports (C7, C12) through ``gh api``; None when gh is unusable."""
    from kintsugi.control.ship import GhCli, ShipError

    try:
        gh = GhCli(window.repo, token=token)
        gh.api("GET", f"repos/{window.repo}")
    except (ShipError, RuntimeError, ValueError):
        return Online()

    def pr_checks(url: str) -> Sequence[Mapping[str, Any]]:
        return gh.pr_checks(int(url.rstrip("/").rsplit("/", 1)[1]))

    def ruleset_history(rid: int) -> Sequence[Mapping[str, Any]]:
        versions = []
        for item in gh.api("GET", f"repos/{window.repo}/rulesets/{rid}/history") or []:
            state = (gh.api("GET", f"repos/{window.repo}/rulesets/{rid}/history/"
                            f"{item['version_id']}") or {}).get("state") or {}  # fmt: skip
            include = ((state.get("conditions") or {}).get("ref_name") or {}).get("include") or []
            versions.append(
                {
                    "updated_at": item["updated_at"],
                    "enforcement": state.get("enforcement"),
                    "include": include,
                }
            )
        return versions

    return Online(pr_checks=pr_checks, ruleset_history=ruleset_history)


def neatlogs_ports(env: Mapping[str, str], store_path: Path | None) -> Online:
    """neatlogs-backed ports (C8, C9, C10); None when no read token is configured."""
    from kintsugi.senses.neatlogs_api import (
        ApiError,
        Credentials,
        CredentialUnavailable,
        NeatlogsApi,
    )

    try:
        api = NeatlogsApi(Credentials.from_env(env))
    except CredentialUnavailable:
        return Online()

    def trace_started(trace_id: str) -> datetime | None:
        try:
            return api.trace(trace_id).started_at
        except ApiError:
            return None

    def session_cost(session_id: str) -> float | None:
        try:
            data = api.session(session_id)
        except ApiError:
            return None
        value = data.get("total_cost_usd", data.get("cost_usd"))
        return None if value is None else float(value)

    hits: Callable[[datetime], tuple[list[ReportedHit], dict[str, SeenHit]]] | None = None
    if store_path is not None and store_path.is_file():
        from kintsugi.senses.completeness import reported_hits
        from kintsugi.senses.store import SenseStore

        def read_hits(start: datetime) -> tuple[list[ReportedHit], dict[str, SeenHit]]:
            store = SenseStore(store_path)
            try:
                seen = store.seen_hits()
            finally:
                store.close()
            detections = sorted({row.detection_id for row in seen.values()})
            return reported_hits(api, detections, start), seen

        hits = read_hits

    return Online(trace_started=trace_started, session_cost=session_cost, hits=hits)
