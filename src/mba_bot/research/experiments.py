"""Evaluate a candidate strategy change against the baseline under pre-registered rules.

This is the machinery a Phase 1 variant (closed-candle signals, a trend filter, a trailing stop...) will be
judged by. Nothing here proposes a variant; it only makes the judging honest:

  * development uses only the time before the lockbox and only the development pairs
  * the holdout pairs are scored once, for generalisation, never used to choose anything
  * every configuration evaluated is written to the ledger, and the deflated Sharpe ratio is computed
    against the number of configurations actually tried
  * the verdict comes from thresholds fixed in `PassCriteria` before the run
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from .engine import BacktestConfig, PairData, run_backtest
from .metrics import daily_returns, summarize, trade_stats
from .reporting import data_hash, load_pair_data
from .validation import (
    Fold, Lockbox, PassCriteria, TrialRegistry, deflated_sharpe_ratio, evaluate_gate, make_folds, walk_forward,
)


@dataclass(frozen=True)
class Candidate:
    name: str
    config: BacktestConfig
    description: str = ""


class _Data:
    """Loads each pair once."""

    def __init__(self, root: Path, need_5m: bool):
        self.root, self.need_5m, self._cache = root, need_5m, {}

    def get(self, symbol: str) -> PairData:
        if symbol not in self._cache:
            from . import data as D

            self._cache[symbol] = load_pair_data(self.root, D.pair_for(symbol), with_5m=self.need_5m)
        return self._cache[symbol]


def _run(data: _Data, symbol: str, cfg: BacktestConfig, start: int | None, end: int | None):
    return run_backtest({symbol: data.get(symbol)}, replace(cfg, start_ts=start, end_ts=end, auto=False))


def _pooled_summary(results) -> dict:
    trades = [t for r in results for t in r.counted_trades()]
    s = trade_stats(trades)
    s["n_pairs"] = len(results)
    s["max_drawdown_pct"] = max((summarize(r)["max_drawdown_pct"] for r in results), default=None)
    rets = [daily_returns(r.equity) for r in results]
    s["sharpe_daily"] = _portfolio_sharpe(_equal_weight(rets))
    return s


def _equal_weight(series: list[list[float]]) -> list[float]:
    n = min((len(s) for s in series), default=0)
    return [statistics.fmean(s[i] for s in series) for i in range(n)]


def _portfolio_sharpe(rets: list[float]) -> float | None:
    if len(rets) < 30:
        return None
    sd = statistics.pstdev(rets)
    return statistics.fmean(rets) / sd if sd > 0 else None


def evaluate_candidate(
    root: Path,
    candidate: Candidate,
    baseline: Candidate,
    *,
    dev_symbols: list[str],
    holdout_symbols: list[str],
    registry: TrialRegistry,
    family: str,
    lockbox: Lockbox,
    criteria: PassCriteria = PassCriteria(),
    train_days: float = 365.0,
    test_days: float = 90.0,
    progress: Callable[[str], None] = lambda m: None,
) -> dict:
    """The lockbox is never touched here: every run is capped at the end of the development range."""
    lockbox.assert_tunable(dev_symbols)
    root = Path(root)
    need_5m = "production_replay" in (candidate.config.mode, baseline.config.mode)
    data = _Data(root, need_5m)
    dh = data_hash(root)
    d_start, d_end = lockbox.develop_range()

    def run_group(cand: Candidate, symbols: list[str], cost_mult: float | None = None, start=d_start, end=d_end):
        cfg = cand.config if cost_mult is None else replace(cand.config, costs=replace(cand.config.costs, multiplier=cost_mult))
        return [_run(data, s, cfg, start, end) for s in symbols]

    progress("baseline, development pairs")
    base_runs = run_group(baseline, dev_symbols)
    progress("candidate, development pairs")
    cand_runs = run_group(candidate, dev_symbols)
    progress("candidate under cost stress")
    stress_runs = run_group(candidate, dev_symbols, cost_mult=criteria.cost_stress_multiplier)
    base_s, cand_s, stress_s = _pooled_summary(base_runs), _pooled_summary(cand_runs), _pooled_summary(stress_runs)
    latest_bar = max(r.span[1] for r in base_runs + cand_runs + stress_runs)

    progress("walk-forward folds")
    folds: list[Fold] = make_folds(d_start, d_end, train_days=train_days, test_days=test_days)

    def evaluate(params: dict, start: int, end: int) -> dict:
        cfg = candidate.config
        return _pooled_summary([_run(data, s, cfg, start, end) for s in dev_symbols])

    wf = walk_forward(evaluate, [{"candidate": candidate.name}], folds, registry=registry, family=family, data_hash=dh, min_trades=1) if folds else None

    cand_hash = registry.record_trial(
        family=family, config={"candidate": candidate.name, "config": candidate.config}, data_hash=dh,
        summary={"n_trades": cand_s.get("n_trades"), "expectancy_r": cand_s.get("expectancy_r"), "sharpe_daily": cand_s.get("sharpe_daily")}, note=candidate.description,
    )
    daily = _equal_weight([daily_returns(r.equity) for r in cand_runs])
    dsr = deflated_sharpe_ratio(daily, max(registry.n_trials(family), 1), registry.trial_sharpes(family))

    progress("holdout pairs")
    hold_cand = hold_base = None
    if holdout_symbols:
        hold_base = _pooled_summary(run_group(baseline, holdout_symbols))
        hold_cand = _pooled_summary(run_group(candidate, holdout_symbols))

    gate = evaluate_gate(
        criteria, candidate=cand_s, baseline=base_s, stressed=stress_s, walk_forward_report=wf, dsr=dsr,
        holdout_candidate=hold_cand, holdout_baseline=hold_base,
    )
    return {
        "candidate": candidate.name, "baseline": baseline.name, "config_hash": cand_hash, "data_hash": dh, "family": family,
        "development_range": [d_start, d_end], "lockbox_untouched": latest_bar <= d_end + 3_600_000, "trials_in_family": registry.n_trials(family),
        "baseline_summary": _brief(base_s), "candidate_summary": _brief(cand_s), "stressed_summary": _brief(stress_s),
        "holdout": {"baseline": _brief(hold_base) if hold_base else None, "candidate": _brief(hold_cand) if hold_cand else None},
        "walk_forward": wf, "deflated_sharpe": dsr, "gate": gate,
    }


def _brief(s: dict | None) -> dict | None:
    if s is None:
        return None
    return {k: s.get(k) for k in ("n_trades", "win_rate", "expectancy_r", "expectancy_r_gross", "profit_factor", "avg_cost_r", "net_pnl", "max_drawdown_pct", "sharpe_daily")}
