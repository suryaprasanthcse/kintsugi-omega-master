"""Wright's-law learning curve and Zipf coverage (spec §3.5).

    C_n = C_1 · n^(−b),   PR = 2^(−b),   LR = 1 − PR

b is estimated by ordinary least squares on ln C_n = ln C_1 − b·ln n + ε. The standard error of
b̂ comes from OLS (a t-interval), with a seeded pairs-bootstrap interval as a cross-check.

Zipf coverage: with K classes and exponent s, learning the m most frequent classes covers
Q(m) = H_{m,s} / H_{K,s} of incidents, where H_{m,s} = Σ_{k=1..m} k^(−s).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import ArrayLike
from scipy.stats import t as student_t


@dataclass(frozen=True)
class WrightFit:
    b: float
    c1: float
    se_b: float
    ci_ols: tuple[float, float]
    ci_bootstrap: tuple[float, float]
    r2: float
    n_points: int

    @property
    def progress_ratio(self) -> float:
        return progress_ratio(self.b)

    @property
    def learning_rate(self) -> float:
        return 1.0 - self.progress_ratio


def progress_ratio(b: float) -> float:
    return 2.0 ** (-b)


def predict(n: float, c1: float, b: float) -> float:
    return c1 * n ** (-b)


def _ols(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    x_mean, y_mean = x.mean(), y.mean()
    sxx = float(((x - x_mean) ** 2).sum())
    slope = float(((x - x_mean) * (y - y_mean)).sum()) / sxx
    return slope, float(y_mean - slope * x_mean)


def fit(
    n_cumulative: ArrayLike,
    cost: ArrayLike,
    *,
    confidence: float = 0.95,
    n_resamples: int = 5_000,
    seed: int = 0,
) -> WrightFit:
    n = np.asarray(n_cumulative, dtype=np.float64)
    c = np.asarray(cost, dtype=np.float64)
    if n.shape != c.shape or n.ndim != 1 or n.size < 3:
        raise ValueError("need at least three paired (n, cost) observations")
    if (n <= 0).any() or (c <= 0).any():
        raise ValueError("cumulative counts and costs must be positive")
    x, y = np.log(n), np.log(c)
    if float(((x - x.mean()) ** 2).sum()) == 0.0:
        raise ValueError("cumulative counts must vary")
    slope, intercept = _ols(x, y)
    residuals = y - (intercept + slope * x)
    dof = n.size - 2
    ssr = float((residuals**2).sum())
    sst = float(((y - y.mean()) ** 2).sum())
    se = math.sqrt(ssr / dof / float(((x - x.mean()) ** 2).sum()))
    b = -slope
    t_crit = float(student_t.ppf(1.0 - (1.0 - confidence) / 2.0, dof))
    ci_ols = (b - t_crit * se, b + t_crit * se)

    rng = np.random.default_rng(seed)
    slopes: list[float] = []
    for _ in range(n_resamples):
        index = rng.integers(0, n.size, size=n.size)
        xs = x[index]
        if float(((xs - xs.mean()) ** 2).sum()) == 0.0:
            continue  # a degenerate resample carries no slope information
        slopes.append(-_ols(xs, y[index])[0])
    tail = (1.0 - confidence) / 2.0
    ci_boot = (float(np.quantile(slopes, tail)), float(np.quantile(slopes, 1.0 - tail)))
    r2 = 1.0 - ssr / sst if sst > 0 else 1.0
    return WrightFit(
        b=b,
        c1=math.exp(intercept),
        se_b=se,
        ci_ols=ci_ols,
        ci_bootstrap=ci_boot,
        r2=r2,
        n_points=int(n.size),
    )


def refit(costs: Sequence[float], *, seed: int = 0) -> dict[str, Any]:
    """The nightly refit (Stage 9): the learning curve over every incident's cost, in the order
    the incidents happened (n = 1, 2, …). Fewer than three points cannot be fitted honestly."""
    points = [{"n": i + 1, "cost_usd": round(float(c), 6)} for i, c in enumerate(costs)]
    if len(points) < 3:
        return {"status": "insufficient", "n_points": len(points), "points": points}
    found = fit([p["n"] for p in points], [p["cost_usd"] for p in points], seed=seed)
    return {
        "status": "fitted", "n_points": found.n_points, "points": points,
        "b": round(found.b, 6), "c1": round(found.c1, 6), "se_b": round(found.se_b, 6),
        "ci_ols": [round(v, 6) for v in found.ci_ols],
        "ci_bootstrap": [round(v, 6) for v in found.ci_bootstrap],
        "r2": round(found.r2, 6), "progress_ratio": round(found.progress_ratio, 6),
        "learning_rate": round(found.learning_rate, 6),
    }  # fmt: skip


def zipf_coverage(m: int, k_classes: int, s: float) -> float:
    if not 0 <= m <= k_classes:
        raise ValueError("need 0 ≤ m ≤ K")

    def harmonic(upto: int) -> float:
        return sum(k ** (-s) for k in range(1, upto + 1))

    return harmonic(m) / harmonic(k_classes)
