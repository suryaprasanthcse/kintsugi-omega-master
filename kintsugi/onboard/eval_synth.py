"""O4 and O5: seed evals and the baseline from exported traces (Stage 10).

``synthesize`` turns each failing trace into a seed eval case: the input the agent saw, the
failure class it produced (the grader's verdict, or the failing tool's error), and the trace
it came from. The cases are proposals for the maintainers' eval suite, never a holdout.
``baseline`` is the pass rate of the exported traces with its 95% Wilson interval.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from kintsugi.senses.records import TraceRecord
from kintsugi.stats.wilson import wilson_interval


def synthesize(records: Sequence[TraceRecord]) -> list[dict[str, Any]]:
    cases = []
    for record in records:
        failing = [v for v in record.verdicts if not v.passed]
        broken = next((s for s in record.spans if s.failed), None)
        if not failing and broken is None and not record.failed:
            continue
        root = next((s for s in record.spans if s.parent_id is None), None)
        expected = failing[0].verdict if failing else (broken.name if broken else record.status)
        cases.append({
            "id": f"seed-{record.trace_id[:12]}",
            "source_trace": record.trace_id,
            "input_preview": str(root.input if root is not None else "")[:2000],
            "expected_failure": str(expected)[:200],
            "task_id": failing[0].task_id if failing else None,
        })  # fmt: skip
    return cases


def baseline(records: Sequence[TraceRecord]) -> dict[str, Any]:
    n = len(records)
    passed = sum(1 for r in records
                 if not r.failed and all(v.passed for v in r.verdicts))  # fmt: skip
    low, high = wilson_interval(passed, n) if n else (0.0, 0.0)
    return {"traces": n, "passed": passed, "pass_rate": round(passed / n, 4) if n else 0.0,
            "wilson_95": [round(low, 4), round(high, 4)]}  # fmt: skip
