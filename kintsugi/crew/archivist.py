"""The Archivist: the responsible code, and why it exists (spec §4.3.2, §5.7, §6.2 step 8).

1. **Localise.** ``entire graph search`` on the symptom summary (ranked regions, JSON) and the
   same search as budgeted agent context (``--max-context-bytes 24576``) at the incident's base.
2. **Blame.** ``git blame --porcelain`` over every ranked region; the originating change is the
   newest commit that wrote lines in them.
3. **Map to a checkpoint two independent ways**: the commit's ``Entire-Checkpoint`` trailer and
   ``entire why <file>:<line> --json`` on one of its lines. They must agree, or the record says
   they did not.
4. **Read the session.** From the checkpoint's stored transcript, select the turns whose
   Edit/Write tool calls touched the blamed file (Entire's "Intent" field is ignored: it shows
   the first prompt of the checkpoint's scope, plan §2b).
5. **Extract** (``claude-opus-5-5``): suspects, stated goal and behavioural contract clauses,
   each with a verbatim quote. **Verify**: a quote that is not, character for character, in the
   transcript's decoded text (or is longer than 300 characters) is dropped with its clause.
6. **Regression check.** The G0 cassette replayed on the originating commit's parent: if it
   passes, the incident is a regression, and a bisect over the commits since then finds the first
   bad commit and its checkpoint (``no-checkpoint`` when a commit has none).

The result is the ``CauseReport`` (case file ``cause/report``) and the intent record
(``intent_record/record``, with the transcript's SHA-256 so the Auditor re-checks the same
bytes).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kintsugi.control import contracts as c
from kintsugi.crew.consult import consult
from kintsugi.crew.context import CrewContext
from kintsugi.crew.manifest import ContextBuilder
from kintsugi.gates.checkout import clean_checkout, rev
from kintsugi.gates.evals import run_tasks
from kintsugi.gates.target import Target
from kintsugi.memory import entire, graph
from kintsugi.proof.attest import ENTIRE_TRAILER, parse_trailers
from kintsugi.proof.canonical import canonicalize, sha256_hex
from kintsugi.sandbox import proc
from kintsugi.sandbox.cassette import Cassette

MAX_QUOTE = 300
EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
CONTRACT_WORDS = re.compile(
    r"(no behaviou?r change|keep (the )?behaviou?r|behaviou?r identical|identical behaviou?r|"
    r"without changing|preserv\w*|purely|only (a )?(readability|refactor)|must not|do not change)",
    re.IGNORECASE,
)

SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "suspects": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "stated_goal": {"type": "string"},
        "behavioral_contract": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"clause": {"type": "string"}, "quote": {"type": "string"}},
                "required": ["clause", "quote"],
                "additionalProperties": False,
            },
        },
        "mechanism": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["suspects", "stated_goal", "behavioral_contract", "mechanism", "confidence"],
    "additionalProperties": False,
}


def _git(repo: Path, *args: str) -> str:
    return proc.run(["git", "-C", str(repo), *args], timeout_s=120, check=True).stdout


# ---- blame ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class BlameLine:
    commit: str
    line: int
    time: int
    path: str = ""


def recent_regions(repo: Path, commits: int = 5) -> list[dict[str, Any]]:
    """Git-only localisation when entire-graph is unavailable: the hunks the most recent
    non-root commits wrote, as regions of the current tree (recorded as such in the report)."""
    log = _git(repo, "rev-list", f"--max-count={commits}", "--no-merges", "HEAD").split()
    regions: list[dict[str, Any]] = []
    for commit in log:
        if not _git(repo, "rev-list", "--parents", "-n", "1", commit).split()[1:]:
            continue  # the root commit
        diff = _git(repo, "diff", "--unified=0", f"{commit}^", commit, "--", "*.py")
        path = ""
        for row in diff.splitlines():
            if row.startswith("+++ b/"):
                path = row[6:]
            hunk = re.match(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", row)
            if hunk and path and not _is_test(path):
                start, count = int(hunk.group(1)), int(hunk.group(2) or 1)
                if count:
                    regions.append(
                        {
                            "file_path": path,
                            "start_line": start,
                            "end_line": start + count - 1,
                            "qualified_name": None,
                        }
                    )
    return regions


def localise(
    checkout: Path, symptom: str, *, top_k: int = 10
) -> tuple[list[dict[str, Any]], str, str]:
    """Step 1: ranked non-test regions for a symptom at a checkout, the agent context, and the
    method: ``entire-graph`` search, or ``git-recent`` (the hunks of recent commits) where
    entire-graph is unavailable. Omega's self-repair replay suite (``hosts/omega``) grades it."""
    try:
        found = graph.search(checkout, symptom, top_k=top_k)
        context = graph.context(checkout, symptom, top_k=top_k, max_context_bytes=24576)
        method = "entire-graph"
    except (graph.GraphError, OSError):  # no entire-graph here: localise with git only
        found = {"results": recent_regions(checkout)}
        context = "(entire graph unavailable: regions changed by recent commits)"
        method = "git-recent"
    regions = [r for r in found.get("results") or [] if not _is_test(str(r.get("file_path", "")))]
    return regions, context, method


def blame(repo: Path, path: str, start: int, end: int) -> list[BlameLine]:
    out = _git(repo, "blame", "--porcelain", "-L", f"{start},{end}", "HEAD", "--", path)
    lines: list[BlameLine] = []
    times: dict[str, int] = {}
    current: tuple[str, int] | None = None
    for row in out.splitlines():
        header = re.match(r"^([0-9a-f]{40}) \d+ (\d+)", row)
        if header:
            current = (header.group(1), int(header.group(2)))
        elif row.startswith("committer-time ") and current is not None:
            times[current[0]] = int(row.split()[1])
        elif row.startswith("\t") and current is not None:
            lines.append(BlameLine(current[0], current[1], times.get(current[0], 0), path))
    return lines


def originating_commit(blamed: Iterable[BlameLine], root: str | None) -> str | None:
    """The newest commit that wrote lines in the suspect regions (the repository root excluded:
    the initial import is nobody's change)."""
    best: tuple[int, int, str] | None = None
    counts: dict[str, int] = defaultdict(int)
    times: dict[str, int] = {}
    for line in blamed:
        counts[line.commit] += 1
        times[line.commit] = line.time
    for commit, count in counts.items():
        if commit == root:
            continue
        key = (times[commit], count, commit)
        if best is None or key > best:
            best = key
    return best[2] if best else None


# ---- transcripts ----------------------------------------------------------------------------


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(b.get("text", ""))
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p)
    return ""


def transcript_entries(full: bytes) -> list[dict[str, Any]]:
    entries = []
    for raw in full.splitlines():
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def decoded_text(full: bytes) -> str:
    """Every user prompt and assistant text of a native transcript, as plain text: the bytes a
    quote must appear in verbatim."""
    texts = []
    for entry in transcript_entries(full):
        if entry.get("type") in ("user", "assistant"):
            texts.append(_text_of((entry.get("message") or {}).get("content")))
    return "\n".join(t for t in texts if t)


def editing_turns(full: bytes, path: str) -> list[dict[str, str]]:
    """The turns (user prompt + assistant text) whose Edit/Write calls touched ``path``."""
    turns: list[dict[str, Any]] = []
    for entry in transcript_entries(full):
        message = entry.get("message") or {}
        content = message.get("content")
        if entry.get("type") == "user" and not entry.get("isMeta"):
            prompt = _text_of(content)
            if prompt:  # a tool_result-only user entry continues the current turn
                turns.append({"prompt": prompt, "assistant": [], "edits": False})
        elif entry.get("type") == "assistant" and turns:
            text = _text_of(content)
            if text:
                turns[-1]["assistant"].append(text)
            for block in content if isinstance(content, list) else []:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and block.get("name") in EDIT_TOOLS
                ):
                    target = str((block.get("input") or {}).get("file_path") or "")
                    if target.replace("\\", "/").endswith(path):
                        turns[-1]["edits"] = True
    return [
        {"prompt": t["prompt"], "assistant": "\n".join(t["assistant"])} for t in turns if t["edits"]
    ]


def verify_quotes(
    clauses: Iterable[Mapping[str, Any]], text: str
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    kept, dropped = [], []
    for clause in clauses:
        quote = str(clause.get("quote") or "")
        item = {"clause": str(clause.get("clause") or ""), "quote": quote}
        (kept if quote and len(quote) <= MAX_QUOTE and quote in text else dropped).append(item)
    return kept, dropped


def heuristic_intent(turns: list[dict[str, str]]) -> dict[str, Any]:
    """Deterministic extraction: the first sentence as the goal, contract-like sentences as
    clauses, each quoted verbatim from the prompt."""
    prompt = turns[0]["prompt"] if turns else ""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", prompt) if s.strip()]
    goal = sentences[0][:MAX_QUOTE] if sentences else ""
    clauses = [
        {"clause": s.rstrip(".!"), "quote": s[:MAX_QUOTE]}
        for s in sentences
        if CONTRACT_WORDS.search(s)
    ]
    return {"stated_goal": goal, "behavioral_contract": clauses}


# ---- the Archivist ----------------------------------------------------------------------------


class Archivist:
    def __init__(self, ctx: CrewContext) -> None:
        self.ctx = ctx

    def attribute(self, sig: c.Signature, repro: c.Repro) -> c.Cause:
        target = self.ctx.target(sig.host)
        case = self.ctx.case(sig.incident_id)
        symptom = case.reader("archivist").get("symptom", "summary")["summary"]
        base = rev(target.repo, self.ctx.base_ref)
        with clean_checkout(target.repo, base) as checkout:
            regions, context, _method = localise(checkout, symptom)
            blamed = [
                line
                for r in regions
                for line in blame(
                    checkout, str(r["file_path"]), int(r["start_line"]), int(r["end_line"])
                )
            ]
            root = _git(checkout, "rev-list", "--max-parents=0", "HEAD").split()[0]
            origin = originating_commit(blamed, root)
            touched = [
                r
                for r in regions
                if any(
                    b.commit == origin and int(r["start_line"]) <= b.line <= int(r["end_line"])
                    for b in blamed
                )
            ]
            impact = {}
            for region in (touched or regions)[:3]:
                name = str(region.get("qualified_name") or region.get("symbol_name"))
                try:
                    impact[name] = graph.impact(checkout, name)
                except (graph.GraphError, OSError) as error:
                    impact[name] = {"error": str(error)[:200]}
            mapping = self._checkpoint(checkout, origin, touched or regions, blamed)
        turns, transcript_sha, text = self._turns(target, mapping, touched or regions)
        record = self._extract(
            sig, case, symptom, context, impact, mapping, turns, text, touched or regions
        )
        record.update(
            {
                "checkpoint": mapping.get("checkpoint"),
                "commit": origin,
                "session": mapping.get("session"),
                "transcript_sha256": transcript_sha,
            }
        )
        regression = self._regression(target, sig, origin, base)
        report = {
            "suspects": record["suspects"],
            "impact_sets": {k: _impact_names(v) for k, v in impact.items()},
            "originating_commit": origin,
            "checkpoint_mapping": mapping,
            "intent_record": {
                k: record[k]
                for k in ("stated_goal", "behavioral_contract", "confidence", "checkpoint")
            },
            "regression": regression["regression"],
            "first_bad_checkpoint": regression.get("first_bad_checkpoint"),
            "bisect": regression.get("steps", []),
            "mechanism": record.get("mechanism", ""),
            "regions": [
                {
                    "file": r["file_path"],
                    "lines": [r["start_line"], r["end_line"]],
                    "symbol": r.get("qualified_name"),
                }
                for r in regions[:10]
            ],
        }
        case.put("graph_context", "search", {"query": symptom, "context": context}, by="archivist")
        case.put("intent_record", "record", record, by="archivist")
        case.put("cause", "report", report, by="archivist")
        sha = "sha256:" + sha256_hex(canonicalize(record))
        text_report = (
            f"{', '.join(record['suspects'])}: {record.get('mechanism', '')} "
            f"(originating {str(origin)[:12]}, checkpoint {mapping.get('checkpoint')})"
        )
        return c.Cause(tuple(record["suspects"]), sha, text_report[:1000])

    def _checkpoint(
        self,
        checkout: Path,
        origin: str | None,
        regions: list[dict[str, Any]],
        blamed: list[BlameLine],
    ) -> dict[str, Any]:
        if origin is None:
            return {"commit": None, "checkpoint": None, "agree": False}
        message = _git(checkout, "log", "-1", "--format=%B", origin)
        trailer = (parse_trailers(message).get(ENTIRE_TRAILER) or [None])[0]
        line = next((b for b in blamed if b.commit == origin), None)
        path = line.path if line is not None and line.path else None
        why: dict[str, Any] = {}
        if line is not None and path is not None:
            try:
                found = entire.why(checkout, path, line.line)
                why = {
                    "checkpoint": found.checkpoint_id,
                    "commit": found.commit_sha,
                    "session": found.session_id,
                    "file": path,
                    "line": line.line,
                }
            except (entire.EntireError, OSError) as error:
                why = {"error": str(error)[:300]}
        agree = bool(trailer) and why.get("checkpoint") == trailer
        return {
            "commit": origin,
            "checkpoint": trailer or why.get("checkpoint"),
            "trailer": trailer,
            "why": why,
            "agree": agree,
            "session": why.get("session"),
            "file": path,
        }

    def _turns(
        self, target: Target, mapping: Mapping[str, Any], regions: list[dict[str, Any]]
    ) -> tuple[list[dict[str, str]], str | None, str]:
        checkpoint = mapping.get("checkpoint")
        if not checkpoint:
            return [], None, ""
        try:
            stored = entire.read_checkpoint(target.repo, str(checkpoint))
        except (entire.EntireError, ValueError):
            return [], None, ""
        full = b"".join(s.full for s in stored.sessions)
        path = str(mapping.get("file") or (regions[0]["file_path"] if regions else ""))
        turns: list[dict[str, str]] = []
        for session in stored.sessions:
            turns += editing_turns(session.full, path)
        return (
            turns,
            "sha256:" + hashlib.sha256(full).hexdigest(),
            "\n".join(decoded_text(s.full) for s in stored.sessions),
        )

    def _extract(
        self,
        sig: c.Signature,
        case: Any,
        symptom: str,
        context: str,
        impact: Mapping[str, Any],
        mapping: Mapping[str, Any],
        turns: list[dict[str, str]],
        text: str,
        regions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        builder = ContextBuilder("archivist", sig.incident_id)
        builder.bind("incident", sig.incident_id)
        builder.bind("host", sig.host)
        builder.add("symptom", symptom, source="pathologist symptom summary", why="the symptom")
        builder.add(
            "graph_context",
            context or "(no regions)",
            source="entire graph search --format agent --max-context-bytes 24576",
            why="top-10 regions for the symptom",
        )
        builder.add(
            "impact",
            json.dumps({k: _impact_names(v) for k, v in impact.items()}),
            source="entire graph impact --depth 2",
            why="blast radius",
        )
        builder.add(
            "blame",
            json.dumps({k: mapping.get(k) for k in ("commit", "checkpoint", "agree", "file")}),
            source="git blame + entire why + trailer",
            why="originating change",
        )
        turn_text = (
            "\n\n".join(f"USER: {t['prompt']}\nASSISTANT: {t['assistant']}" for t in turns)
            or "(no session turns found)"
        )
        builder.add(
            "turns",
            turn_text,
            source=f"checkpoint {mapping.get('checkpoint')}",
            why="intent of the originating change",
        )
        builder.exclude("holdout tasks", "invariant I5")
        names = [
            str(
                r.get("qualified_name")
                or r.get("symbol_name")
                or f"{r['file_path']}:{r['start_line']}"
            )
            for r in regions[:3]
        ]

        def fallback() -> dict[str, Any]:
            intent = heuristic_intent(turns)
            return {
                "suspects": names or ["(unknown)"],
                "mechanism": "",
                "confidence": 0.5 if intent["behavioral_contract"] else 0.3,
                **intent,
            }

        answer = consult(
            self.ctx.model,
            case,
            builder,
            "archivist",
            schema=SCHEMA,
            fallback=fallback,
            max_budget_usd=3.0,
            model_id=self.ctx.model_for("archivist"),
        )
        data = dict(answer.data)
        kept, dropped = verify_quotes(data.get("behavioral_contract") or [], text)
        data["behavioral_contract"] = kept
        data["dropped_clauses"] = dropped
        return data

    def _regression(
        self, target: Target, sig: c.Signature, origin: str | None, base: str
    ) -> dict[str, Any]:
        case = self.ctx.case(sig.incident_id)
        repro = case.tool_read("repro", "g0", tool="bisect")
        raw = case.tool_read("cassette", "g0", tool="bisect")
        if origin is None or not repro.get("ok") or raw is None:
            return {"regression": False}
        cassette = Cassette.from_dict(raw)
        generator_tasks = _tasks(target, repro["failing"])

        def good(commit: str) -> bool:
            with clean_checkout(target.repo, commit) as checkout:
                run = run_tasks(
                    target,
                    checkout,
                    generator_tasks,
                    seed=int(repro["seed"]),
                    split=str(repro["split"]),
                    cassette=cassette,
                    phase="bisect",
                )
            return all(v == "PASS" for v in run.verdicts().values())

        parent = _git(target.repo, "rev-parse", f"{origin}^").strip()
        if not good(parent):
            return {"regression": False, "parent": parent}
        commits = _git(target.repo, "rev-list", "--reverse", f"{parent}..{base}").split()
        steps: list[dict[str, Any]] = []
        low, high = 0, len(commits) - 1  # commits[high] (the base) is bad
        while low < high:
            mid = (low + high) // 2
            ok = good(commits[mid])
            steps.append(
                {
                    "commit": commits[mid],
                    "good": ok,
                    "checkpoint": _checkpoint_of(target.repo, commits[mid]),
                }
            )
            if ok:
                low = mid + 1
            else:
                high = mid
        first_bad = commits[high] if commits else origin
        return {
            "regression": True,
            "parent": parent,
            "first_bad_commit": first_bad,
            "first_bad_checkpoint": _checkpoint_of(target.repo, first_bad),
            "steps": steps,
        }


def _checkpoint_of(repo: Path, commit: str) -> str:
    trailer = parse_trailers(_git(repo, "log", "-1", "--format=%B", commit)).get(ENTIRE_TRAILER)
    return trailer[0] if trailer else "no-checkpoint"


def _tasks(target: Target, ids: Iterable[str]) -> list[dict[str, Any]]:
    import importlib

    target.make_importable()
    pool = {
        t["id"]: t
        for t in importlib.import_module(f"{target.host.package}.evals.generate").tasks("train")
    }
    return [pool[i] for i in ids if i in pool]


def _impact_names(report: Mapping[str, Any]) -> list[str]:
    from kintsugi.senses.drift import impact_endpoints

    return sorted(
        {
            str(e.get("qualified_name") or e.get("name"))
            for e in impact_endpoints(report)
            if e.get("qualified_name") or e.get("name")
        }
    )


def _is_test(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    return "tests" in parts or parts[-1].startswith("test_")
