"""``kintsugi demo``: a sealed sabotage incident and its verification, replayed locally.

The demo runs the W wiring rehearsal (``kintsugi.crew.w_rehearsal``) end to end in a scratch
directory outside every repository: a disclosed practice host and a disclosed practice spec
(never S-17/18/19), sealed before T0 with a fresh age identity; ``kintsugi t0 genesis`` with a
rehearsal key (never the T0 key); the crew built by the W runtime factory seals the spec onto the
ledger branch, delivers it after ``not_before`` through the upstream lane, anchors the ledger;
after W, ``t1`` and the reveal are committed and ``kintsugi audit`` verifies **C1** (the hash
chain and its signed anchor) and **C6** (commitment ↔ reveal, sealed before delivered, no
honeytoken leak).

It needs no credentials, no network and no model. It exits 0 only when both checks pass; the
checks it cannot exercise offline (they need real sessions, GitHub, neatlogs or a sealed holdout)
are listed, never reported as passed.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kintsugi.crew import w_rehearsal


def run(root: Path | None = None, *, keep: bool = False,
        log: Callable[[str], None] = print) -> dict[str, Any]:  # fmt: skip
    base = Path(root) if root is not None else Path(tempfile.mkdtemp(prefix="kintsugi-demo-"))
    name = datetime.now(UTC).strftime("demo-%Y%m%d-%H%M%S")
    cfg = w_rehearsal.WRehearsal(name, base / name, log=log)
    try:
        return w_rehearsal.run(cfg)
    finally:
        if not keep:
            remove_tree(base / name)


def remove_tree(path: Path) -> None:
    """Delete a scratch tree, including git's read-only object files (Windows refuses those)."""

    def writable_then_retry(func: Callable[[str], object], target: str, _exc: BaseException
                            ) -> None:  # fmt: skip
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if path.exists():
        shutil.rmtree(path, onexc=writable_then_retry)


def render(summary: Mapping[str, Any]) -> str:
    """The demo's report: the ledger, the audit's verdicts and what stays out of reach."""
    lines = [f"KINTSUGI-Omega demo ({summary['run']}, {summary['duration_s']} s, offline)", ""]
    lines.append("ledger: " + " -> ".join(
        f"{e['seq']}:{e['type']}" + (f"/{e['event']}" if e.get("event") else "")
        for e in summary["ledger"]))  # fmt: skip
    lines.append(f"anchor: {summary['anchor']}")
    for check, result in summary["audit"]["results"].items():
        lines.append(f"{check:<4}{result['status']:<6}{result.get('summary', '')}")
    lines.append(f"audit exit code: {summary['audit']['exit_code']}")
    lines.append("not exercised offline: " + "; ".join(
        f"{k} ({v})" for k, v in summary["not_exercised"].items()))  # fmt: skip
    lines.append("disclosed: " + "; ".join(summary["disclosed"]))
    return "\n".join(lines)


def main(*, root: Path | None, keep: bool, as_json: bool) -> int:
    summary = run(root, keep=keep, log=(lambda line: None) if as_json else print)
    print(json.dumps(summary, indent=1, sort_keys=True) if as_json else render(summary))
    return 0 if summary["audit"]["exit_code"] == 0 else 1
