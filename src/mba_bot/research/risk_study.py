"""Risk-governance study: what does a 1% versus a 2% per-trade risk do to drawdown, ruin, compounding, notional caps and
minimum-notional blocks?

Research only. It reads trades from the research backtester and changes nothing in the service. The production cap (2%) is
untouched; this produces the evidence for a later decision.

Two views, deliberately separate:
  engine grid   the exact backtester at each (capital tier, risk %), so notional caps, exchange minimums and rounding are
                real. One historical path per cell, so it shows mechanics, not a distribution.
  Monte Carlo   resamples the baseline's net R-multiples in 30-day calendar blocks (keeping losing streaks and clustering)
                and compounds them at each risk fraction. This gives distributions (max drawdown percentiles, probability of
                losing half the account) but ignores caps and minimums, which the grid covers.
R-multiples already include costs. Costs scale with position size exactly as risk does, so cost-in-R is the same at 1% and 2%
until a cap or minimum binds; that is why the grid is needed alongside the simulation.
"""
from __future__ import annotations

import random
import statistics
from collections import defaultdict
from dataclasses import replace

from . import confirm_stats as cs
from .engine import MODE_REPLAY, AccountParams, TradeRecord
from .metrics import trade_stats

DAY_MS = 86_400_000
RISKS = (0.5, 1.0, 1.5, 2.0)
TIERS = (100.0, 250.0, 500.0, 1000.0, 5000.0)
RUIN_LEVELS = (0.5, 0.25)  # equity at or below this share of the start counts as losing that much of the account


def _pct(sorted_values: list[float], q: float) -> float:
    return sorted_values[min(len(sorted_values) - 1, int(q * len(sorted_values)))]


def block_sequences(trades: list[TradeRecord], block_days: int = 30) -> list[list[float]]:
    real = sorted((t for t in trades if t.exit_reason != "end_of_data"), key=lambda t: t.entry_ts)
    if not real:
        return []
    origin = real[0].entry_ts
    blocks: dict[int, list[float]] = defaultdict(list)
    for t in real:
        blocks[(t.entry_ts - origin) // (block_days * DAY_MS)].append(t.r_net)
    return [blocks[k] for k in sorted(blocks)]


def monte_carlo(trades: list[TradeRecord], *, risk_pcts=RISKS, horizon_trades: int = 150, sims: int = 5000, seed: int = 11, block_days: int = 30,
                max_leverage_loss_floor: float = 0.0) -> dict:
    """Compounding at `risk_pct` of current equity per trade over `horizon_trades` trades (about one year of Auto trading is
    on the order of the historical trades per year; the report states the actual number)."""
    blocks = block_sequences(trades, block_days)
    if not blocks:
        return {"error": "no trades"}
    rng = random.Random(seed)
    paths = []
    for _ in range(sims):
        seq: list[float] = []
        while len(seq) < horizon_trades:
            seq.extend(rng.choice(blocks))
        paths.append(seq[:horizon_trades])
    out: dict = {"horizon_trades": horizon_trades, "sims": sims, "block_days": block_days, "mean_r": statistics.fmean(r for p in paths for r in p),
                 "worst_single_r": min(r for p in paths for r in p), "by_risk": {}}
    for f in risk_pcts:
        mdds, finals, ruin = [], [], {lvl: 0 for lvl in RUIN_LEVELS}
        for p in paths:
            eq = peak = 1.0
            mdd = 0.0
            floor = {lvl: False for lvl in RUIN_LEVELS}
            for r in p:
                eq *= max(max_leverage_loss_floor, 1.0 + f / 100.0 * r)
                peak = max(peak, eq)
                mdd = max(mdd, (peak - eq) / peak)
                for lvl in RUIN_LEVELS:
                    if eq <= lvl:
                        floor[lvl] = True
            mdds.append(mdd * 100)
            finals.append((eq - 1.0) * 100)
            for lvl, hit in floor.items():
                ruin[lvl] += hit
        mdds.sort()
        finals.sort()
        out["by_risk"][str(f)] = {
            "max_drawdown_pct": {q: _pct(mdds, q / 100) for q in (5, 25, 50, 75, 95, 99)},
            "final_return_pct": {q: _pct(finals, q / 100) for q in (5, 25, 50, 75, 95)},
            "prob_lose_half": ruin[0.5] / sims, "prob_lose_three_quarters": ruin[0.25] / sims,
            "prob_negative_after_horizon": sum(1 for x in finals if x < 0) / sims,
            "median_final_multiple": 1.0 + _pct(finals, 0.5) / 100,
        }
    return out


def _account_row(trades: list[TradeRecord], digest, acct: AccountParams) -> dict:
    real = [t for t in trades if t.exit_reason != "end_of_data"]
    capped = 0
    for t in real:
        intended = acct.risk_pct / 100.0 * min(acct.allocated_capital, t.equity_before)
        if intended > 0 and t.planned_risk < 0.98 * intended:
            capped += 1
    eq, curve = digest.start_equity, []
    for i, r in enumerate(digest.daily):
        eq *= 1 + r
        curve.append((i, eq))
    return {"entries": digest.counters.get("entries", 0), "blocked_exchange_filters": digest.counters.get("blocked_exchange_filters", 0),
            "blocked_size_zero": digest.counters.get("blocked_size_zero", 0), "signals": digest.counters.get("actionable_signals", 0),
            "capped_trades": capped, "capped_share": (capped / len(real)) if real else None,
            "expectancy_r": trade_stats(real).get("expectancy_r") if real else None,
            "return_pct": (digest.end_equity / digest.start_equity - 1) * 100 if digest.start_equity else None,
            "max_dd_pct": digest.max_dd_pct, "ulcer": cs.ulcer_index(curve), "end_equity": digest.end_equity}


def engine_grid(lab, symbols: list[str], *, tiers=TIERS, risks=RISKS, auto: bool = False, start_ts: int | None = None, end_ts: int | None = None,
                progress=lambda m: None) -> dict:
    """The production baseline at every (capital tier, risk) cell. `lab` is a phase1.Lab; its signal tapes are shared, so each
    cell costs one replay."""
    from .engine import Policy

    grid: dict = {}
    for s in symbols:
        lab.load(s)
    for tier in tiers:
        for risk in risks:
            acct = AccountParams(allocated_capital=tier, risk_pct=risk, max_risk_pct=max(2.0, risk))
            cfg = replace(lab.config(Policy(), MODE_REPLAY, auto=auto, start_ts=start_ts, end_ts=end_ts), account=acct)
            progress(f"tier {tier:g} risk {risk:g}%")
            d = lab.run(symbols, cfg)
            grid[f"{tier:g}|{risk:g}"] = {"tier": tier, "risk_pct": risk, **_account_row(d.trades, d, acct)}
    return grid


def render(mc: dict, grids: dict[str, dict]) -> str:
    out = ["# Risk study: 1% versus 2% per trade", "",
           "Research only. The production cap (2%) is unchanged. R-multiples come from the production-replay baseline, after costs.", ""]
    if "error" not in mc:
        out += [f"## Monte Carlo ({mc['sims']} paths of {mc['horizon_trades']} trades, {mc['block_days']}-day blocks)", "",
                f"Mean R per trade in the sample {mc['mean_r']:+.3f}; worst single trade {mc['worst_single_r']:+.2f} R.", "",
                "| risk per trade | median max drawdown | 95th pct drawdown | 99th pct drawdown | median return | 5th pct return | P(lose half) | P(lose 3/4) | P(negative) |",
                "|---|---|---|---|---|---|---|---|---|"]
        for f, v in mc["by_risk"].items():
            out.append(f"| {f}% | {v['max_drawdown_pct'][50]:.1f}% | {v['max_drawdown_pct'][95]:.1f}% | {v['max_drawdown_pct'][99]:.1f}% | {v['final_return_pct'][50]:+.1f}% | "
                       f"{v['final_return_pct'][5]:+.1f}% | {v['prob_lose_half']:.1%} | {v['prob_lose_three_quarters']:.1%} | {v['prob_negative_after_horizon']:.1%} |")
    for name, grid in grids.items():
        out += ["", f"## Engine grid: {name}", "", "| capital | risk | entries | signals blocked by exchange minimums | size rounds to zero | trades hit by the notional cap | return | max drawdown | ulcer |",
                "|---|---|---|---|---|---|---|---|---|"]
        for cell in grid.values():
            cs_ = cell["capped_share"]
            out.append(f"| {cell['tier']:g} | {cell['risk_pct']:g}% | {cell['entries']} | {cell['blocked_exchange_filters']} | {cell['blocked_size_zero']} | "
                       f"{'n/a' if cs_ is None else f'{cs_:.0%}'} | {cell['return_pct']:+.1f}% | {cell['max_dd_pct']:.1f}% | {cell['ulcer'] if cell['ulcer'] is None else round(cell['ulcer'], 1)} |")
    out += ["", "## How to read this", "",
            "- Drawdown and ruin scale roughly with the risk fraction; doubling the risk does not double the return once the notional cap or a minimum binds.",
            "- A single historical path (the grid) is one draw; the Monte Carlo shows how far the same trades could have gone against you.",
            "- Compounding assumes the trades keep their historical edge. If the edge is smaller or gone, the higher risk loses faster.",
            "- This study cannot see exchange-side events (gaps through the stop, liquidation, API outage)."]
    return "\n".join(out) + "\n"
