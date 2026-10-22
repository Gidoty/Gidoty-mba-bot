"""Performance and risk metrics for a backtest (or for live trades expressed the same way).

Everything is computed from NET results (after fees, slippage and funding) unless a name says
otherwise. Where a statistic is unreliable on the sample at hand it is returned as None with a
reason instead of a number that looks precise:

  Sharpe / Sortino   need >= 90 daily observations and >= 30 trades
  expectancy CI      trade-level bootstrap (assumes trades are independent draws; real trades
                     cluster in regimes, so treat the interval as optimistic)
  win rate           Wilson interval
"""
from __future__ import annotations

import math
import random
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable, Iterable

from .engine import RunResult, TradeRecord

MIN_DAILY_OBS = 90
MIN_TRADES_FOR_RATIOS = 30
DAY_MS = 86_400_000


def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n <= 0:
        return None
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def bootstrap_mean_ci(values: list[float], *, iterations: int = 4000, seed: int = 11, alpha: float = 0.05) -> dict | None:
    if len(values) < 5:
        return None
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(iterations))
    lo = means[int(alpha / 2 * iterations)]
    hi = means[int((1 - alpha / 2) * iterations) - 1]
    return {"low": lo, "high": hi, "prob_positive": sum(1 for m in means if m > 0) / iterations}


def daily_returns(curve: list[tuple[int, float]]) -> list[float]:
    """Simple daily returns of the mark-to-market equity series (last point of each UTC day)."""
    last: dict[int, float] = {}
    for ts, eq in curve:
        last[ts // DAY_MS] = eq
    days = sorted(last)
    out = []
    for a, b in zip(days, days[1:]):
        if last[a] > 0:
            out.append(last[b] / last[a] - 1.0)
    return out


def max_drawdown(curve: list[tuple[int, float]]) -> dict:
    peak = -math.inf
    peak_ts = 0
    worst = 0.0
    worst_usd = 0.0
    longest = 0
    start_ts = None
    for ts, eq in curve:
        if eq > peak:
            peak, peak_ts = eq, ts
            if start_ts is not None:
                longest = max(longest, ts - start_ts)
                start_ts = None
        else:
            if start_ts is None:
                start_ts = peak_ts
            if peak > 0:
                worst = max(worst, (peak - eq) / peak)
            worst_usd = max(worst_usd, peak - eq)
    if start_ts is not None and curve:
        longest = max(longest, curve[-1][0] - start_ts)
    return {"max_drawdown_pct": worst * 100.0, "max_drawdown_usd": worst_usd, "longest_drawdown_days": longest / DAY_MS}


def sharpe_sortino(rets: list[float], n_trades: int) -> dict:
    if len(rets) < MIN_DAILY_OBS or n_trades < MIN_TRADES_FOR_RATIOS:
        return {"sharpe": None, "sortino": None, "sharpe_note": f"not computed: needs >= {MIN_DAILY_OBS} days and >= {MIN_TRADES_FOR_RATIOS} trades (have {len(rets)} days, {n_trades} trades)"}
    mean = statistics.fmean(rets)
    sd = statistics.pstdev(rets)
    down = math.sqrt(sum(min(r, 0.0) ** 2 for r in rets) / len(rets))
    k = math.sqrt(365.0)
    sharpe = mean / sd * k if sd > 0 else None
    sortino = mean / down * k if down > 0 else None
    se = math.sqrt((1 + 0.5 * (mean / sd) ** 2) / len(rets)) * k if sd > 0 else None
    return {"sharpe": sharpe, "sortino": sortino, "sharpe_se": se, "sharpe_note": "annualised from daily mark-to-market returns (365 days)"}


def streaks(values: list[float]) -> dict:
    longest_loss = cur = 0
    longest_win = curw = 0
    counts: dict[int, int] = defaultdict(int)
    for v in values:
        if v <= 0:
            cur += 1
            longest_loss = max(longest_loss, cur)
            curw = 0
        else:
            if cur >= 3:
                counts[cur] += 1
            cur = 0
            curw += 1
            longest_win = max(longest_win, curw)
    if cur >= 3:
        counts[cur] += 1
    return {"longest_losing_streak": longest_loss, "longest_winning_streak": longest_win, "losing_streaks_of_3_plus": dict(sorted(counts.items()))}


def _pf(wins_sum: float, losses_sum: float) -> float | None:
    return wins_sum / abs(losses_sum) if losses_sum < 0 else None


def trade_stats(trades: list[TradeRecord]) -> dict:
    """Per-trade statistics (no equity curve needed), shared by the headline and the breakdown tables."""
    n = len(trades)
    if n == 0:
        return {"n_trades": 0}
    r = [t.r_net for t in trades]
    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl <= 0]
    gross_r = [t.r_gross for t in trades]
    cost_r = [(t.fees + t.slippage_cost + t.funding) / t.planned_risk for t in trades if t.planned_risk]
    out = {
        "n_trades": n,
        "win_rate": len(wins) / n,
        "win_rate_ci95": wilson_interval(len(wins), n),
        "expectancy_r": statistics.fmean(r),
        "expectancy_r_gross": statistics.fmean(gross_r),
        "median_r": statistics.median(r),
        "avg_win_r": statistics.fmean([t.r_net for t in wins]) if wins else None,
        "avg_loss_r": statistics.fmean([t.r_net for t in losses]) if losses else None,
        "payoff_ratio": (statistics.fmean([t.r_net for t in wins]) / abs(statistics.fmean([t.r_net for t in losses]))) if wins and losses and statistics.fmean([t.r_net for t in losses]) < 0 else None,
        "profit_factor": _pf(sum(t.net_pnl for t in wins), sum(t.net_pnl for t in losses)),
        "net_pnl": sum(t.net_pnl for t in trades),
        "gross_pnl": sum(t.gross_pnl for t in trades),
        "fees": sum(t.fees for t in trades),
        "slippage": sum(t.slippage_cost for t in trades),
        "funding": sum(t.funding for t in trades),
        "avg_cost_r": statistics.fmean(cost_r) if cost_r else None,
        "avg_hold_hours": statistics.fmean([t.hold_hours for t in trades]),
        "best_r": max(r),
        "worst_r": min(r),
    }
    gross = out["gross_pnl"]
    out["cost_drag_pct_of_gross"] = (100.0 * (out["fees"] + out["slippage"] + out["funding"]) / gross) if gross > 0 else None
    if n >= 2:
        sd = statistics.stdev(r)
        out["expectancy_t_stat"] = out["expectancy_r"] / (sd / math.sqrt(n)) if sd > 0 else None
    return out


def summarize(result: RunResult) -> dict:
    trades = result.counted_trades()
    out = trade_stats(trades)
    span_days = max((result.span[1] - result.span[0]) / DAY_MS, 1e-9)
    out["span_days"] = span_days
    out["start_equity"] = result.start_equity
    out["end_equity"] = result.end_equity
    out["total_return_pct"] = (result.end_equity / result.start_equity - 1.0) * 100.0 if result.start_equity else None
    years = span_days / 365.25
    out["annualised_return_pct"] = ((result.end_equity / result.start_equity) ** (1 / years) - 1.0) * 100.0 if result.start_equity > 0 and result.end_equity > 0 and years > 0 else None
    out["trades_per_month"] = len(trades) / (span_days / 30.4375)
    held = sum(t.hold_hours for t in trades)
    out["exposure_pct"] = 100.0 * held / (span_days * 24.0)
    out.update(max_drawdown(result.equity))
    dd = out["max_drawdown_pct"]
    out["calmar"] = (out["annualised_return_pct"] / dd) if (out["annualised_return_pct"] is not None and dd > 0) else None
    out.update(sharpe_sortino(daily_returns(result.equity), len(trades)))
    if trades:
        out.update(streaks([t.net_pnl for t in trades]))
        out["expectancy_r_bootstrap"] = bootstrap_mean_ci([t.r_net for t in trades])
        out.update(_excursion_stats(trades))
        out.update(_confirmation_stats(trades))
    out["counters"] = dict(result.counters)
    return out


def _excursion_stats(trades: list[TradeRecord]) -> dict:
    winners = [t for t in trades if t.net_pnl > 0]
    reached_1r = [t for t in trades if t.mfe_r >= 1.0]
    gave_back = [t for t in reached_1r if t.net_pnl <= 0]
    return {
        "avg_mfe_r": statistics.fmean([t.mfe_r for t in trades]),
        "avg_mae_r": statistics.fmean([t.mae_r for t in trades]),
        "avg_winner_giveback_r": statistics.fmean([t.mfe_r - t.r_net for t in winners]) if winners else None,
        "pct_reached_1r_then_lost": (100.0 * len(gave_back) / len(reached_1r)) if reached_1r else None,
    }


def _confirmation_stats(trades: list[TradeRecord]) -> dict:
    known = [t for t in trades if t.signal_confirmed_at_close is not None]
    out: dict = {}
    if known:
        conf = [t for t in known if t.signal_confirmed_at_close]
        unconf = [t for t in known if not t.signal_confirmed_at_close]
        out["pct_entries_confirmed_at_candle_close"] = 100.0 * len(conf) / len(known)
        out["expectancy_r_confirmed"] = statistics.fmean([t.r_net for t in conf]) if conf else None
        out["expectancy_r_unconfirmed"] = statistics.fmean([t.r_net for t in unconf]) if unconf else None
        out["n_unconfirmed"] = len(unconf)
    again = [t for t in trades if t.same_hour_reentry]
    out["n_same_hour_reentries"] = len(again)
    out["expectancy_r_same_hour_reentry"] = statistics.fmean([t.r_net for t in again]) if again else None
    return out


def breakdown(trades: Iterable[TradeRecord], key: Callable[[TradeRecord], object], *, min_trades: int = 1) -> dict[str, dict]:
    groups: dict[str, list[TradeRecord]] = defaultdict(list)
    for t in trades:
        groups[str(key(t))].append(t)
    out = {}
    for k in sorted(groups):
        if len(groups[k]) >= min_trades:
            s = trade_stats(groups[k])
            out[k] = {x: s.get(x) for x in ("n_trades", "win_rate", "expectancy_r", "expectancy_r_gross", "profit_factor", "net_pnl", "avg_cost_r", "avg_hold_hours", "worst_r")}
    return out


def year_of(t: TradeRecord) -> int:
    return datetime.fromtimestamp(t.entry_ts / 1000, tz=timezone.utc).year


def quarter_of(t: TradeRecord) -> str:
    d = datetime.fromtimestamp(t.entry_ts / 1000, tz=timezone.utc)
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"
