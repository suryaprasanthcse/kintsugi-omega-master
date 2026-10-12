"""L5 treatments (Stage 9, spec §6.8): a configuration change Omega decides, committed by a notary.

When the governor chooses *treat and escalate*, the treatment is a change named in the business
plan (``business/plan.json``), e.g. Triage's T1: route ``classify_component`` from Haiku to
Sonnet in ``hosts/triage/config/routing.yaml``. Like a revert (plan §2a L3) it is an
orchestrator-decided commit, so it runs inside a notary session (an attested commit with an
Entire checkpoint, C4) in a disposable worktree off the base, and lands through the same path as
an auto-revert (``Reverter.ship_commit``: a local ``--no-ff`` merge offline, or a pull request
merged with a merge commit). The change is computed here, exactly: one routing line, from the
plan's ``from`` model to its ``to`` model; anything else is refused.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Mapping
from typing import Any

from kintsugi.control.revert import Reverter
from kintsugi.crew.notary import Notary, NotaryRequest
from kintsugi.gates.target import Target
from kintsugi.sandbox import proc, worktree


class TreatmentError(RuntimeError):
    pass


def treatment_diff(target: Target, base: str, change: Mapping[str, str]) -> str:
    """The unified diff that moves ``change['step']`` from ``from`` to ``to`` in ``file``."""
    path, step = str(change["file"]), str(change["step"])
    if not path.startswith(f"{target.package_root.rstrip('/')}/"):
        raise TreatmentError(f"{path} is outside the host's package {target.package_root}")
    shown = proc.run(["git", "-C", str(target.repo), "show", f"{base}:{path}"], timeout_s=60)
    if not shown.ok:
        raise TreatmentError(f"{path} does not exist at {base[:12]}")
    before = shown.stdout
    line = re.compile(rf"^(\s+){re.escape(step)}:\s*{re.escape(str(change['from']))}\s*$",
                      re.MULTILINE)  # fmt: skip
    after, count = line.subn(lambda m: f"{m.group(1)}{step}: {change['to']}", before)
    if count != 1:
        raise TreatmentError(f"{path}: expected one '{step}: {change['from']}' line, found {count}")
    return "".join(difflib.unified_diff(before.splitlines(keepends=True),
                                        after.splitlines(keepends=True),
                                        fromfile=f"a/{path}", tofile=f"b/{path}"))  # fmt: skip


class Treater:
    def __init__(self, notary: Notary, reverter: Reverter) -> None:
        self.notary = notary
        self.reverter = reverter  # its ship_commit lands orchestrator commits (L3 path)

    def apply(
        self, target: Target, change: Mapping[str, str], *, record: str, treatment: str, why: str
    ) -> dict[str, Any]:
        base = self.reverter.base_head(target.repo, record)
        diff = treatment_diff(target, base, change)
        wt = worktree.create(target.repo, record, "treat", base=base)
        subject = f"{target.id}: L5 treatment {treatment}"[:100]
        try:
            notarised = self.notary.commit(wt.path, NotaryRequest(
                operation="l5-treatment", reference=treatment, incident=record,
                purpose=(f"{why} The governor chose this treatment over a repair (spec §6.8); "
                         "the patch changes one routing line and nothing else."),
                diff=diff, subject=subject, scopes=("surgeon",)),
                package_root=target.package_root)  # fmt: skip
        finally:
            worktree.release_lock(wt)
        body = (f"## Kintsugi L5 treatment\n\n{why}\n\n- `{change['step']}`: `{change['from']}` → "
                f"`{change['to']}` in `{change['file']}`, committed by an attested notary "
                f"session (checkpoint `{notarised.checkpoint_id or 'n/a'}`).\n")  # fmt: skip
        shipped = self.reverter.ship_commit(target.repo, notarised.sha, wt.branch, subject, body)
        return {"ok": bool(shipped["merge_sha"]), "commit": notarised.sha,
                "checkpoint": notarised.checkpoint_id, "pr": shipped["pr_url"],
                "merge_sha": shipped["merge_sha"], "reason": shipped["reason"]}  # fmt: skip
