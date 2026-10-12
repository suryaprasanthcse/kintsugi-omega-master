"""Run the replay suite: rebuild every case, diagnose, grade (Stage 12).

python -m omega.evals.run --root <scratch dir outside any repository> [--out file.json]
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from omega import agent
from omega.evals import cases, replay
from omega.graders import grade


def run_suite(root: Path, selected: Sequence[str] | None = None) -> dict[str, Any]:
    grades = []
    for case in cases.CASES:
        if selected and case["id"] not in selected:
            continue
        repo = replay.build(case, root)
        grades.append(grade.grade(repo, case, agent.diagnose(repo, str(case["symptom"]))))
    return {"summary": grade.summary(grades), "grades": grades,
            "recorded_cases": len(cases.CASES), "target_cases": cases.TARGET_SIZE}  # fmt: skip


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Omega's replay suite (root-cause hit@3).")
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as scratch:
        report = run_suite(args.root or Path(scratch))
    text = json.dumps(report, indent=1, sort_keys=True) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8", newline="\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
