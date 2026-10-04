"""Bootstrap confidence range for per-day estimates built from sampled windows."""

from __future__ import annotations

import random


def bootstrap_rate(values: list[float], secs: list[float], n: int = 2000,
                   seed: int = 1, lo_q: float = 0.10, hi_q: float = 0.90):
    """values[i] happened in secs[i] seconds. Returns (per_day, lo, hi).

    Resamples whole windows with replacement, so bursty activity (a few
    windows with huge numbers) shows up as a wide range.
    """
    if not values or sum(secs) <= 0:
        return 0.0, 0.0, 0.0
    point = sum(values) / sum(secs) * 86_400
    if len(values) < 2:
        return point, point, point
    rnd = random.Random(seed)
    k = len(values)
    outs = []
    for _ in range(n):
        idx = [rnd.randrange(k) for _ in range(k)]
        s = sum(secs[i] for i in idx)
        outs.append(sum(values[i] for i in idx) / s * 86_400 if s > 0 else 0.0)
    outs.sort()
    return point, outs[int(lo_q * (n - 1))], outs[int(hi_q * (n - 1))]


def rel_width(point: float, lo: float, hi: float) -> float:
    """Half-width of the range relative to the estimate (0.25 = ±25%)."""
    if point <= 0:
        return float("inf")
    return (hi - lo) / 2 / point
