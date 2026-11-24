"""Bootstrap confidence intervals for the research studies. Pure and seeded: the same numbers give the same interval. Resamples with replacement and reports a percentile interval.

A trade's net R is not independent of its neighbours (two pairs can hold the same market move, and a trend can run for weeks), so these intervals are NARROWER than the truth. The studies say so next to every interval and also
report a block bootstrap by calendar month, which keeps a month's trades together; read the wider of the two."""
from __future__ import annotations

import random


def _pct(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return float("nan")
    k = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[k]


def mean_ci(values: list[float], *, reps: int = 2000, level: float = 0.95, seed: int = 1) -> tuple[float, float, float] | None:
    """(mean, low, high) of the mean by resampling the values. None for fewer than 2 values."""
    n = len(values)
    if n < 2:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(reps):
        s = 0.0
        for _ in range(n):
            s += values[rng.randrange(n)]
        means.append(s / n)
    means.sort()
    a = (1 - level) / 2
    return sum(values) / n, _pct(means, a), _pct(means, 1 - a)


def block_sum_ci(blocks: dict, *, reps: int = 2000, level: float = 0.95, seed: int = 1) -> tuple[float, float, float] | None:
    """(total, low, high) of the TOTAL by resampling whole blocks (for example calendar months) with replacement: block -> sum of the values in it. The total of a resample is rescaled to the original number of blocks
    by construction (the same count of blocks is drawn). None for fewer than 2 blocks."""
    vals = list(blocks.values())
    n = len(vals)
    if n < 2:
        return None
    rng = random.Random(seed)
    totals = []
    for _ in range(reps):
        totals.append(sum(vals[rng.randrange(n)] for _ in range(n)))
    totals.sort()
    a = (1 - level) / 2
    return sum(vals), _pct(totals, a), _pct(totals, 1 - a)


def excludes_zero(ci: tuple[float, float, float] | None) -> bool:
    return ci is not None and (ci[1] > 0 or ci[2] < 0)
