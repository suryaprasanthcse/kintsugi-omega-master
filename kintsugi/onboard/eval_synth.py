"""O5 and O6: seed evals and the baseline from exported traces (Stage 10).

``synthesize`` turns each **truly failing** trace into a seed eval case: the input the agent
saw, the failure class it produced (the grader's verdict, or the failing tool's error), and the
trace it came from. A trace counts as failing when its grader verdict is a FAIL, or, without a
verdict, when its task (root span) failed. A trace whose verdict passed although some tool
spans errored is a **transient retry** (the agent recovered, e.g. a 429 retried successfully):
it is never a seed; ``transient`` lists those traces and their recovered spans. The cases are
proposals for the maintainers' eval suite, never a holdout. ``baseline`` is the pass rate of
the exported traces with its 95% Wilson interval, their cost and p95 duration.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from kintsugi.senses.records import TraceRecord
from kintsugi.stats.wilson import wilson_interval


def _root(record: TraceRecord) -> Any:
    return next((s for s in record.spans if s.parent_id is None), None)


def truly_failed(record: TraceRecord) -> bool:
    """A FAIL verdict, or, without any verdict, a failed task (root span or trace status)."""
    if record.verdicts:
        return any(not v.passed for v in record.verdicts)
    root = _root(record)
    return (root is not None and root.failed) or record.status.upper() == "ERROR"


def transient(records: Sequence[TraceRecord]) -> list[dict[str, Any]]:
    """Traces that recovered: errored spans inside a task that did not fail."""
    found = []
    for record in records:
        recovered = [s.name for s in record.spans if s.failed and s.parent_id is not None]
        if recovered and not truly_failed(record):
            found.append({"source_trace": record.trace_id, "task_id": record.task_id,
                          "recovered_spans": recovered})  # fmt: skip
    return found


def synthesize(records: Sequence[TraceRecord]) -> list[dict[str, Any]]:
    cases = []
    for record in records:
        if not truly_failed(record):
            continue  # a passing trace, or a transient retry the agent recovered from
        failing = [v for v in record.verdicts if not v.passed]
        broken = next((s for s in record.spans if s.failed), None)
        root = _root(record)
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
    passed = sum(1 for r in records if not truly_failed(r))  # transient retries pass
    low, high = wilson_interval(passed, n) if n else (0.0, 0.0)
    durations = sorted(r.duration_ms for r in records)
    p95 = durations[max(0, -(-95 * n // 100) - 1)] if n else 0.0  # nearest-rank p95
    return {"traces": n, "passed": passed, "pass_rate": round(passed / n, 4) if n else 0.0,
            "wilson_95": [round(low, 4), round(high, 4)],
            "cost_usd": round(sum(r.cost_usd for r in records), 6),
            "p95_duration_ms": round(float(p95), 3)}  # fmt: skip
