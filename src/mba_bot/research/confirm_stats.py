"""Statistics for Phase 2 confirmation.

Why these differ from Phase 1's: Phase 1 compared a candidate with the baseline as two independent samples. A filter's
trades are mostly the baseline's own trades, so that comparison ignores the overlap, and R is heavy-tailed (a few trades supply
most of the profit), so the independent comparison is badly under-powered. Here the two arms are compared on the SAME time
blocks: resample whole blocks of calendar time and recompute both arms' average R on each resample. That keeps the overlap,
keeps the cross-pair clustering inside a block, and keeps the heavy tail in the resample.

Nothing here runs a backtest. All functions take lists of TradeRecord (or plain numbers) and return plain dicts."""
from __future__ import annotations

import bisect
import math
import random
import statistics
from collections import defaultdict
from typing import Iterable

from .engine import TradeRecord

DAY_MS = 86_400_000
HOUR_MS = 3_600_000


def _real(trades: Iterable[TradeRecord]) -> list[TradeRecord]:
    return [t for t in trades if t.exit_reason != "end_of_data"]


# ------------------------------------------------------------------ paired block bootstrap


def paired_block_bootstrap(
    cand: Iterable[TradeRecord], base: Iterable[TradeRecord], *, block_days: int = 14, iterations: int = 5000, seed: int = 7, alpha: float = 0.10
) -> dict | None:
    """Difference in mean R (candidate minus baseline) with blocks of calendar time resampled together.

    `p_positive` is the share of resamples with a strictly positive difference. It is a bootstrap probability, not a p-value;
    `p_one_sided` (share of resamples at or below zero) is what the Holm adjustment uses."""
    c, b = _real(cand), _real(base)
    if not c or not b:
        return None
    origin = min(t.entry_ts for t in c + b)
    width = block_days * DAY_MS
    agg: dict[int, list[float]] = defaultdict(lambda: [0.0, 0, 0.0, 0])  # sum_c, n_c, sum_b, n_b
    for t in c:
        row = agg[(t.entry_ts - origin) // width]
        row[0] += t.r_net
        row[1] += 1
    for t in b:
        row = agg[(t.entry_ts - origin) // width]
        row[2] += t.r_net
        row[3] += 1
    rows = [agg[k] for k in sorted(agg)]
    k = len(rows)
    sc, nc, sb, nb = (sum(r[i] for r in rows) for i in range(4))
    point = sc / nc - sb / nb
    rng = random.Random(seed)
    diffs = []
    for _ in range(iterations):
        picks = rng.choices(rows, k=k)
        a_s = a_n = b_s = b_n = 0
        for r in picks:
            a_s += r[0]
            a_n += r[1]
            b_s += r[2]
            b_n += r[3]
        if a_n and b_n:
            diffs.append(a_s / a_n - b_s / b_n)
    if not diffs:
        return None
    diffs.sort()
    m = len(diffs)
    return {
        "delta": point, "low": diffs[int(alpha * m)], "high": diffs[min(m - 1, int((1 - alpha) * m))],
        "p_positive": sum(1 for d in diffs if d > 0) / m, "p_one_sided": sum(1 for d in diffs if d <= 0) / m,
        "n_blocks": k, "n_candidate": nc, "n_baseline": nb, "block_days": block_days, "iterations": m, "seed": seed,
        "candidate_mean": sc / nc, "baseline_mean": sb / nb,
    }


def design_effect(trades: Iterable[TradeRecord], *, block_days: int = 14, iterations: int = 2000, seed: int = 7) -> dict | None:
    """How much clustering in time inflates the variance of the mean compared with treating trades as independent.
    Effective sample size = n / design_effect."""
    ts = _real(trades)
    if len(ts) < 30:
        return None
    r = [t.r_net for t in ts]
    var_iid = statistics.variance(r) / len(r)
    origin = min(t.entry_ts for t in ts)
    width = block_days * DAY_MS
    blocks: dict[int, list[float]] = defaultdict(list)
    for t in ts:
        blocks[(t.entry_ts - origin) // width].append(t.r_net)
    groups = list(blocks.values())
    k = len(groups)
    if k < 5:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(iterations):
        picks = rng.choices(groups, k=k)
        n = sum(len(g) for g in picks)
        means.append(sum(sum(g) for g in picks) / n)
    var_block = statistics.variance(means)
    de = var_block / var_iid if var_iid > 0 else None
    # Effective n is capped at n: a design effect below 1 (negative dependence between blocks) is noise at this sample size, not extra information.
    return {"design_effect": de, "effective_n": min(len(r), len(r) / de) if de else None, "n": len(r), "n_blocks": k, "block_days": block_days}


# ------------------------------------------------------------------ heavy-tail aware summaries


def _quantile(sorted_values: list[float], q: float) -> float:
    return sorted_values[min(len(sorted_values) - 1, max(0, int(q * len(sorted_values))))]


def robust_summary(trades: Iterable[TradeRecord]) -> dict:
    """R is dominated by a few large winners. These views show how much of the mean they carry."""
    r = [t.r_net for t in _real(trades)]
    if not r:
        return {"n": 0}
    s = sorted(r)
    n = len(s)
    cut = max(1, int(0.01 * n))
    trimmed = s[cut:-cut] if n > 2 * cut else s
    hi = _quantile(s, 0.99)
    top5 = s[-max(1, int(0.05 * n)) :]
    positive = sum(x for x in s if x > 0)
    return {
        "n": n, "mean": statistics.fmean(s), "median": statistics.median(s), "trimmed_mean_1pct": statistics.fmean(trimmed),
        "winsorized_mean_99": statistics.fmean(min(x, hi) for x in s), "mean_without_top_5pct": statistics.fmean(s[: n - len(top5)]) if n > len(top5) else None,
        "top_5pct_share_of_positive_r": (sum(top5) / positive) if positive > 0 else None, "n_ge_3r": sum(1 for x in s if x >= 3.0),
        "top_decile_mean": statistics.fmean(s[-max(1, n // 10) :]),
    }


def retention(base: Iterable[TradeRecord], cand: Iterable[TradeRecord]) -> dict:
    b, c = robust_summary(base), robust_summary(cand)
    return {
        "top_decile_ratio": (c["top_decile_mean"] / b["top_decile_mean"]) if b.get("n") and c.get("n") and b["top_decile_mean"] > 0 else None,
        "big_winner_ratio": (c["n_ge_3r"] / b["n_ge_3r"]) if b.get("n_ge_3r") else None,
        "trade_ratio": (c["n"] / b["n"]) if b.get("n") and c.get("n") is not None else None,
    }


# ------------------------------------------------------------------ multiplicity and costs


def holm_adjust(p_values: dict[str, float]) -> dict[str, float]:
    """Holm-Bonferroni step-down adjusted p-values (controls the family-wise error rate)."""
    items = sorted(p_values.items(), key=lambda kv: kv[1])
    m = len(items)
    out: dict[str, float] = {}
    running = 0.0
    for i, (name, p) in enumerate(items):
        running = max(running, min(1.0, (m - i) * p))
        out[name] = running
    return out


def break_even_multiplier(points: list[tuple[float, float]]) -> dict:
    """Fit E[R](m) = G - C*m over (cost multiplier, expectancy) points. m* = G / C is the cost multiple at which the edge is
    gone. Funding is not scaled by the multiplier, so G is the edge before fees and slippage."""
    pts = [(m, e) for m, e in points if e is not None]
    if len(pts) < 2:
        return {"gross": None, "cost_per_unit": None, "multiplier": None, "r2": None}
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return {"gross": None, "cost_per_unit": None, "multiplier": None, "r2": None}
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    c = -slope
    mult = None
    if c > 0:
        mult = max(0.0, intercept / c)
    return {"gross": intercept, "cost_per_unit": c, "multiplier": mult, "r2": (1 - ss_res / ss_tot) if ss_tot > 0 else None}


# ------------------------------------------------------------------ regimes and event days (from BTC only, causal)


class RegimeIndex:
    """Market regime at a moment, from BTC's own trailing return. bull: above +25% over 90 days; bear: below -25%; else range."""

    def __init__(self, btc_hourly, window_hours: int = 2160, bull: float = 0.25, bear: float = -0.25):
        self.ts = [c.timestamp for c in btc_hourly]
        self.closes = [c.close for c in btc_hourly]
        self.window, self.bull, self.bear = window_hours, bull, bear

    def at(self, ts_ms: int) -> str:
        i = bisect.bisect_right(self.ts, ts_ms) - 1
        if i < self.window or self.closes[i - self.window] <= 0:
            return "unknown"
        r = self.closes[i] / self.closes[i - self.window] - 1.0
        return "bull" if r > self.bull else "bear" if r < self.bear else "range"


def event_days(btc_hourly, quantile: float = 0.99) -> set[int]:
    """UTC days (as ts // 86_400_000) whose BTC close-to-close move is in the top `1 - quantile` of absolute moves."""
    last: dict[int, float] = {}
    for c in btc_hourly:
        last[c.timestamp // DAY_MS] = c.close
    days = sorted(last)
    moves = [(d, abs(last[d] / last[p] - 1.0)) for p, d in zip(days, days[1:]) if last[p] > 0 and d - p == 1]
    if not moves:
        return set()
    thr = _quantile(sorted(m for _, m in moves), quantile)
    return {d for d, m in moves if m >= thr}


def overlaps_event(t: TradeRecord, days: set[int]) -> bool:
    return any(d in days for d in range(t.entry_ts // DAY_MS, t.exit_ts // DAY_MS + 1))


# ------------------------------------------------------------------ equity-curve risk measures


def ulcer_index(curve: list[tuple[int, float]]) -> float | None:
    """Root-mean-square percentage drawdown: penalises depth and duration together."""
    if not curve:
        return None
    peak = -math.inf
    sq = 0.0
    for _, eq in curve:
        peak = max(peak, eq)
        dd = (peak - eq) / peak * 100 if peak > 0 else 0.0
        sq += dd * dd
    return math.sqrt(sq / len(curve))


# ------------------------------------------------------------------ what a filter actually removed


def matched_kept_removed(base: Iterable[TradeRecord], cand: Iterable[TradeRecord], *, block_days: int = 14, seed: int = 7) -> dict:
    """Match baseline trades to candidate trades by (symbol, side, entry time). Baseline trades with a match were kept;
    the rest were removed; candidate trades with no baseline match are new (path effects: skipping one trade frees the
    account for another). For a pure entry filter the removed-versus-kept gap is the cleanest evidence."""
    b, c = _real(base), _real(cand)
    ckeys = {(t.symbol, t.side, t.entry_ts) for t in c}
    bkeys = {(t.symbol, t.side, t.entry_ts) for t in b}
    kept = [t for t in b if (t.symbol, t.side, t.entry_ts) in ckeys]
    removed = [t for t in b if (t.symbol, t.side, t.entry_ts) not in ckeys]
    new = [t for t in c if (t.symbol, t.side, t.entry_ts) not in bkeys]
    out = {"n_kept": len(kept), "n_removed": len(removed), "n_new": len(new),
           "kept_mean": statistics.fmean(t.r_net for t in kept) if kept else None,
           "removed_mean": statistics.fmean(t.r_net for t in removed) if removed else None,
           "new_mean": statistics.fmean(t.r_net for t in new) if new else None}
    out["gap_kept_minus_removed"] = (out["kept_mean"] - out["removed_mean"]) if kept and removed else None
    if kept and removed:
        out["gap_bootstrap"] = paired_block_bootstrap(kept, removed, block_days=block_days, seed=seed)
    return out
