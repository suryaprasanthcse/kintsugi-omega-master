"""The Surgeon session runner: headless Claude Code in the hypothesis worktree, then an attested
commit (spec §5.7 Surgeon, §4.5.2; plan §2a; amendments A-16, A-17, A-18).

``SurgeonStep`` (Stage 5) owns the attempt algorithm (adopt by nonce, or clean up and issue a
new nonce); this module is its ``SessionRunner``:

1. The protected-path pre-commit hook is installed in the target repository (idempotent).
2. The prompt is rendered from the pinned ``surgeon`` template: the card (root cause, intent
   constraints with verified quotes, allowed files, diff budget), a cause-report excerpt and the
   aggregate feedback of earlier rounds. The nonce is in it, so it lands in the transcript (C4).
3. ``claude -p`` (``claude-sonnet-5-5``) runs in the worktree with Read, Edit, Glob, Grep, the
   repository's test runner and ``entire graph`` only; Entire's hooks record the session.
4. The orchestrator commits what the session's Edit/Write calls explain (A-16), refusing
   protected paths and an unapproved large diff, signs the attestation over the tree, parent,
   role, incident and the prompt hash (A-17), and verifies the ``Entire-Checkpoint`` trailer.

The self-report and session record go to the case file under the Surgeon's own row; the Auditor
can never read them. A refused commit is recorded as the orchestrator's ``refusal`` and the step
returns no commit, so the hypothesis fails its gates instead of failing the incident.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kintsugi.control import contracts as c
from kintsugi.crew import claude_session, templates
from kintsugi.crew.context import CrewContext
from kintsugi.gates import precommit
from kintsugi.gates.protected_paths import load_policy
from kintsugi.proof import attest
from kintsugi.proof.commit import CommitRefused, orchestrator_commit, prompt_digest
from kintsugi.proof.commit import tool_edited_paths as edited
from kintsugi.sandbox import worktree

MODEL = "claude-sonnet-5-5"


def allowed_tools() -> tuple[str, ...]:
    return ("Read", "Edit", "Glob", "Grep", "Bash(python -m pytest:*)", "Bash(entire graph:*)")


def session_env(target_root: Path) -> dict[str, str]:
    """``python`` is Omega's interpreter (the host runtime is installed there); the worktree's
    package root comes first on ``PYTHONPATH`` so tests import the code being edited."""
    scripts = str(Path(sys.executable).parent)
    return {
        "PATH": scripts + os.pathsep + os.environ.get("PATH", ""),
        "PYTHONPATH": str(target_root),
    }


def _bullets(items: list[dict[str, Any]]) -> str:
    return "\n".join(f'- {i.get("clause")}: "{i.get("quote")}"' for i in items) or "(none)"


def render_prompt(
    card: dict[str, Any], *, incident: str, nonce: str, cause: str, feedback: str
) -> templates.Rendered:
    return templates.render(
        "surgeon",
        incident=incident,
        hypothesis=card["hypothesis"],
        nonce=nonce,
        title=card["title"],
        root_cause=card["root_cause"],
        approach=card["approach"],
        intent_constraints=_bullets(list(card.get("intent_constraints") or [])),
        allowed_files=", ".join(card.get("allowed_files") or []) or "(none)",
        diff_budget=card["diff_budget"],
        cause_excerpt=cause,
        feedback=feedback,
        test_command="python -m pytest -q",
    )


class ClaudeSurgeon:
    """``SessionRunner`` for :class:`kintsugi.control.surgeon_step.SurgeonStep`."""

    def __init__(
        self,
        ctx: CrewContext,
        signer: attest.Signer,
        *,
        model: str = MODEL,
        max_budget_usd: float = 4.0,
        timeout_s: float = 2400.0,
        run: Callable[..., claude_session.SessionResult] = claude_session.run,
        require_checkpoint: bool = True,  # False only where Entire is not installed (unit tests)
    ) -> None:
        self.ctx = ctx
        self.signer = signer
        self.model = model
        self.max_budget_usd = max_budget_usd
        self.timeout_s = timeout_s
        self._run = run
        self.require_checkpoint = require_checkpoint

    def _context(self, card: c.Card, brief: dict[str, Any]) -> tuple[str, str]:
        view = self.ctx.case(card.incident_id).reader("surgeon")
        symptom = (view.get("symptom", "summary", None) or {}).get("summary", "")
        feedback = {
            name: view.get("feedback", name)
            for name in view.names("feedback")
            if name != card.hypothesis
        }
        cause = f"Symptom: {symptom}\nRoot cause (Coordinator): {brief['root_cause']}"
        return cause, json.dumps(feedback, indent=1) if feedback else "(first round)"

    def run(
        self,
        wt: worktree.Worktree,
        card: c.Card,
        nonce: str,
        attempt: int,
        on_start: Callable[[int], None],
    ) -> str | None:
        brief = json.loads(card.brief)
        target = self.ctx.target(str(brief["host"]))
        case = self.ctx.case(card.incident_id)
        precommit.install(target.repo)
        cause, feedback = self._context(card, brief)
        rendered = render_prompt(
            brief, incident=card.incident_id, nonce=nonce, cause=cause, feedback=feedback
        )
        started = datetime.now(UTC).isoformat(timespec="seconds")
        request = claude_session.SessionRequest(
            prompt=rendered.prompt,
            cwd=wt.path,
            allowed_tools=allowed_tools(),
            model=self.ctx.models.get("surgeon", self.model),
            session_id=str(uuid.uuid4()),
            max_budget_usd=self.max_budget_usd,
            timeout_s=self.timeout_s,
            extra_env=session_env(wt.path / target.package_root),
            append_system_prompt=rendered.system,
        )
        result = self._run(request, on_start=on_start)
        stream = result.stream
        case.add_cost("surgeon", stream.cost_usd, f"{card.hypothesis} attempt {attempt}")
        case.put(
            "surgeon_session",
            card.hypothesis,
            {
                "session": result.session_id,
                "attempt": attempt,
                "exit": result.exit_code,
                "timed_out": result.timed_out,
                "cost_usd": stream.cost_usd,
                "turns": stream.num_turns,
                "tool_uses": len(stream.tool_uses),
                "transcript_sha256": result.transcript_sha256,
                "prompt_sha": rendered.prompt_sha,
                "template_sha": rendered.template_sha,
            },
            by="surgeon",
        )
        case.put(
            "surgeon_report",
            card.hypothesis,
            {"report": (stream.result or {}).get("result")},
            by="surgeon",
        )
        attestation = attest.SessionAttestation(
            nonce=nonce,
            role=target.repair_role,  # "self-repair" when Omega repairs itself (C14)
            incident=card.incident_id,
            worktree=f"wt/{card.incident_id}/{card.hypothesis}",
            base_sha=wt.base_sha,
            prompt_template_sha=prompt_digest(rendered.prompt),
            t_start=started,
            hypothesis=card.hypothesis,
        )
        try:
            outcome = orchestrator_commit(
                wt.path,
                attestation=attestation,
                signer=self.signer,
                subject=f"fix({card.incident_id}): {brief['title']} [{card.hypothesis}]"[:100],
                body=f"Hypothesis {card.hypothesis}, round {card.round}: {brief['approach']}"[:500],
                policy=load_policy(),
                scopes=target.scopes,
                session_paths=edited(stream.tool_uses, wt.path),
                approved_large_diff=bool(brief.get("approved_large_diff")),
                require_checkpoint=self.require_checkpoint,
            )
        except CommitRefused as refusal:
            case.put(
                "refusal",
                card.hypothesis,
                {"reason": str(refusal)[:1000], "attempt": attempt},
                by="surgeon",
            )
            return None
        case.put(
            "outcome",
            card.hypothesis,
            {
                "commit": outcome.sha,
                "checkpoint": outcome.checkpoint_id,
                "paths": list(outcome.changed_paths),
                "lines": outcome.changed_lines,
                "attempt": attempt,
                "nonce": nonce,
            },
            by="surgeon",
        )
        return outcome.sha
