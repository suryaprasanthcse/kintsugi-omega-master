"""The replay grader (Stage 12): root-cause hit@3, cost, p95 latency.

A suspect hits when it is in a ground-truth file and its lines overlap the truth symbol's
definition (found with the stdlib ``ast`` in the case's repository), or when it names the
symbol. hit@3 is the share of cases with a hit among the top three suspects.
"""

from __future__ import annotations

import ast
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


def symbol_span(repo: Path, file: str, symbol: str) -> tuple[int, int] | None:
    path = Path(repo) / file
    if not path.is_file():
        return None
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        kinds = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        if isinstance(node, kinds) and node.name == symbol:
            return node.lineno, int(node.end_lineno or node.lineno)
    return None


def hit(repo: Path, truth: Sequence[Mapping[str, str]], suspect: Mapping[str, Any]) -> bool:
    for t in truth:
        if str(suspect.get("file")) != t["file"]:
            continue
        name = str(suspect.get("symbol") or "")
        if name and name.split(".")[-1] == t["symbol"]:
            return True
        span = symbol_span(repo, t["file"], t["symbol"])
        if span and int(suspect["start"]) <= span[1] and int(suspect["end"]) >= span[0]:
            return True
    return False


def grade(repo: Path, case: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    ranks = [i + 1 for i, s in enumerate(result["suspects"]) if hit(repo, case["truth"], s)]
    return {"id": case["id"], "hit_at_3": bool(ranks), "rank": ranks[0] if ranks else None,
            "method": result["method"], "cost_usd": result["cost_usd"],
            "seconds": result["seconds"], "suspects": list(result["suspects"])}  # fmt: skip


def p95(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def summary(grades: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    n = len(grades)
    hits = sum(1 for g in grades if g["hit_at_3"])
    return {"cases": n, "hits": hits, "hit_at_3": round(hits / n, 4) if n else 0.0,
            "mean_cost_usd": round(sum(g["cost_usd"] for g in grades) / n, 6) if n else 0.0,
            "p95_seconds": round(p95([float(g["seconds"]) for g in grades]), 3),
            "methods": sorted({str(g["method"]) for g in grades})}  # fmt: skip
