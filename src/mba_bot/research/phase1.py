"""Phase 1: a pre-registered, disciplined search for robust improvements to the EMA 20/50 + ATR strategy.

Nothing here changes the service. It measures candidate rules in the research backtester and records every one of
them. The plan below (candidates, survival screen, folds, lockbox) is fixed BEFORE any candidate result is seen; its
hash is printed with every run and committed in docs/research/phase1_preregistration.md. Changing the plan changes the
hash, and a run with a different hash is a different experiment.

Protocol
  S0  Reproduce the Phase 0 baseline with this engine (identical numbers, or stop).
  S1  Descriptive studies on the baseline trades (no tuning): sides, volatility, ADX, exits, cross-event flips.
  S2  Every candidate on the DEVELOPMENT pairs (the original four) and the DEVELOPMENT period (everything before the
      lockbox), at base costs. Survival screen C1-C9 against the baseline over the same pairs and period, with the
      out-of-sample view taken from walk-forward test windows. Walk-forward selection inside each parameter family.
  S3  Only survivors: cost stress (1.5x, 2x), then the six HOLDOUT pairs once, then the deflated Sharpe ratio.
  S4  The lockbox (the last months of data) is NOT touched by `run`. A separate, explicit, logged command opens it, once,
      for the single candidate that survived S3.

The six added pairs are never used to choose or tune anything. The first four are the only pairs a candidate is judged on
during development.
"""
from __future__ import annotations

import hashlib
import json
import pickle
import statistics
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..venue import rules_from_market
from . import analysis, data as datalib, synthetic, tape as tapelib
from .engine import (
    MODE_CLOSED, MODE_REPLAY, AccountParams, BacktestConfig, CostModel, PairData, Policy, RunResult, StrategyParams,
    TradeRecord, _full_timeline, run_backtest, timeline_key,
)
from .metrics import daily_returns, max_drawdown, trade_stats
from .reporting import data_hash
from .validation import Lockbox, TrialRegistry, code_version, config_hash, deflated_sharpe_ratio, make_folds, walk_forward

ORIGINAL_FOUR = ("BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT")
EXTRA_SIX = ("SOL/USDT", "DOGE/USDT", "SUI/USDT", "LINK/USDT", "ADA/USDT", "LTC/USDT")
DAY_MS = 86_400_000


# ------------------------------------------------------------------ the pre-registered plan


@dataclass(frozen=True)
class Candidate:
    id: str
    hypothesis: str  # H1 signal quality | H2 volatility | H3 direction | H4 exits | H5 auto select
    family: str  # candidates that differ only in one parameter share a family
    label: str
    policy: Policy
    scope: str = "fixed"  # "fixed" pair or "auto" over the original four
    diagnostic: bool = False  # measured to understand the strategy; never eligible for adoption
    seeds: tuple[int, ...] = ()  # Auto random baseline: averaged over these seeds


PLAN: tuple[Candidate, ...] = (
    # --- H1: keep the valuable early entries, reject the crosses that vanish before the candle closes
    Candidate("H1-P3", "H1", "H1_persist", "cross must persist 3 consecutive polls (15 min)", Policy(confirm_polls=3)),
    Candidate("H1-P6", "H1", "H1_persist", "cross must persist 6 consecutive polls (30 min)", Policy(confirm_polls=6)),
    Candidate("H1-P9", "H1", "H1_persist", "cross must persist 9 consecutive polls (45 min)", Policy(confirm_polls=9)),
    Candidate("H1-M05", "H1", "H1_margin", "EMA gap at least 0.05 ATR when entering", Policy(confirm_margin_atr=0.05)),
    Candidate("H1-M10", "H1", "H1_margin", "EMA gap at least 0.10 ATR when entering", Policy(confirm_margin_atr=0.10)),
    Candidate("H1-M20", "H1", "H1_margin", "EMA gap at least 0.20 ATR when entering", Policy(confirm_margin_atr=0.20)),
    Candidate("H1-WC", "H1", "H1_close", "enter only on a closed candle (exits unchanged)", Policy(wait_for_close_sides=("long", "short"))),
    # --- H2: volatility, judged against each pair's own trailing 90-day history (no universal threshold)
    Candidate("H2-V67", "H2", "H2_skip", "skip entries above the 67th volatility percentile", Policy(vol_skip_above=0.67)),
    Candidate("H2-V80", "H2", "H2_skip", "skip entries above the 80th volatility percentile", Policy(vol_skip_above=0.80)),
    Candidate("H2-V90", "H2", "H2_skip", "skip entries above the 90th volatility percentile", Policy(vol_skip_above=0.90)),
    Candidate("H2-HR80", "H2", "H2_half", "half the risk above the 80th volatility percentile", Policy(vol_half_risk_above=0.80)),
    # --- H3: direction-aware rules (shorts only; longs keep production behaviour)
    Candidate("H3-SC", "H3", "H3_short_close", "shorts enter only on a closed candle", Policy(wait_for_close_sides=("short",))),
    Candidate("H3-SP6", "H3", "H3_short_persist", "shorts need a 30-minute persistent cross", Policy(confirm_polls=6, confirm_sides=("short",))),
    Candidate("H3-SM10", "H3", "H3_short_margin", "shorts need an EMA gap of 0.10 ATR", Policy(confirm_margin_atr=0.10, confirm_sides=("short",))),
    Candidate("H3-ST0", "H3", "H3_short_trend", "shorts only while the 30-day return is at or below 0", Policy(short_max_ret30d=0.0)),
    Candidate("H3-ST10", "H3", "H3_short_trend", "shorts only while the 30-day return is at or below -10%", Policy(short_max_ret30d=-0.10)),
    Candidate("D-NOSHORT", "H3", "diag_side", "DIAGNOSTIC: no shorts (measures what shorts contribute)", Policy(short_allowed=False), diagnostic=True),
    Candidate("D-NOLONG", "H3", "diag_side", "DIAGNOSTIC: no longs (measures what longs contribute)", Policy(long_allowed=False), diagnostic=True),
    # --- H4: conservative exits that must not cut the large winners
    Candidate("H4-XC", "H4", "H4_exit_close", "act on the opposite crossover only when the hourly candle closes", Policy(exit_on_close_only=True)),
    Candidate("H4-T3", "H4", "H4_trail", "trail 3 ATR behind the best price once +1R", Policy(trail_atr_mult=3.0, trail_start_r=1.0)),
    Candidate("H4-T4", "H4", "H4_trail", "trail 4 ATR behind the best price once +1R", Policy(trail_atr_mult=4.0, trail_start_r=1.0)),
    Candidate("H4-T5", "H4", "H4_trail", "trail 5 ATR behind the best price once +1R", Policy(trail_atr_mult=5.0, trail_start_r=1.0)),
    Candidate("H4-B10", "H4", "H4_be", "stop to breakeven after +1.0R", Policy(breakeven_at_r=1.0)),
    Candidate("H4-B15", "H4", "H4_be", "stop to breakeven after +1.5R", Policy(breakeven_at_r=1.5)),
    Candidate("H4-B20", "H4", "H4_be", "stop to breakeven after +2.0R", Policy(breakeven_at_r=2.0)),
    # --- H5: Auto Select ranking (original four pairs, one position)
    Candidate("H5-RND", "H5", "H5_auto", "NULL BENCHMARK: random choice among signalling pairs (5 seeds)", Policy(auto_score="random"), scope="auto", diagnostic=True, seeds=(1, 2, 3, 4, 5)),
    Candidate("H5-CALM", "H5", "H5_auto", "calmest signalling pair first (lowest volatility percentile)", Policy(auto_score="calm"), scope="auto"),
    Candidate("H5-MOM", "H5", "H5_auto", "most momentum-aligned signalling pair first (72h return in ATR units)", Policy(auto_score="momentum"), scope="auto"),
    Candidate("H5-COMP", "H5", "H5_auto", "average rank of calm and momentum", Policy(auto_score="composite"), scope="auto"),
)


@dataclass
class Phase1Options:
    dev_pairs: tuple[str, ...] = ORIGINAL_FOUR
    holdout_pairs: tuple[str, ...] = EXTRA_SIX
    auto_pairs: tuple[str, ...] = ORIGINAL_FOUR
    lockbox_months: float = 6.0
    lockbox_embargo_days: float = 3.0
    train_days: float = 365.0
    test_days: float = 91.0
    fold_embargo_days: float = 1.0
    stress: tuple[float, ...] = (1.5, 2.0)
    account: AccountParams = field(default_factory=lambda: AccountParams(allocated_capital=1000.0))
    costs: CostModel = field(default_factory=CostModel)
    strategy: StrategyParams = field(default_factory=StrategyParams)
    screen: analysis.Screen = field(default_factory=analysis.Screen)


def plan_hash(options: Phase1Options | None = None, plan: tuple[Candidate, ...] = PLAN) -> str:
    options = options or Phase1Options()
    blob = {"plan": [asdict(c) for c in plan], "options": asdict(options)}
    return hashlib.sha1(json.dumps(blob, sort_keys=True, default=str).encode()).hexdigest()[:16]


# ------------------------------------------------------------------ data access (one pair at a time)


@dataclass
class RunDigest:
    symbols: list[str]
    trades: list[TradeRecord]
    max_dd_pct: float
    daily: list[float]
    start_equity: float
    end_equity: float
    counters: dict[str, int]
    notes: list[str]

    def counted(self) -> list[TradeRecord]:
        return [t for t in self.trades if t.exit_reason != "end_of_data"]


def digest(res: RunResult) -> RunDigest:
    return RunDigest(res.symbols, res.trades, max_drawdown(res.equity)["max_drawdown_pct"], daily_returns(res.equity), res.start_equity,
                     res.end_equity, res.counters, res.notes)


class Lab:
    """Loads market data a pair at a time and caches signal tapes on disk, so a run stays inside a modest amount of memory."""

    def __init__(self, root: Path, out_dir: Path, options: Phase1Options, progress: Callable[[str], None] = lambda m: None):
        self.root, self.options, self.progress = Path(root), options, progress
        self.tape_dir = Path(out_dir) / "tapes"
        self.tape_dir.mkdir(parents=True, exist_ok=True)
        self.fp = data_hash(self.root)
        self._pairs: dict[str, PairData] = {}
        self._aux: dict[str, tapelib.HourlyAux] = {}
        self._sp_hash = config_hash(asdict(options.strategy))

    # -- loading
    def load(self, symbol: str) -> PairData:
        if symbol not in self._pairs:
            info = datalib.load_exchange_info(self.root).get(symbol)
            rules = rules_from_market({"info": info}) if info else None
            five = datalib.load_candles(self.root, symbol, "5m") if datalib.kline_path(self.root, "5m", symbol).exists() else None
            self._pairs[symbol] = PairData(symbol, datalib.load_candles(self.root, symbol, "1h"), five, datalib.load_funding(self.root, symbol), rules)
        return self._pairs[symbol]

    def drop(self, symbols: list[str]) -> None:
        for s in symbols:
            self._pairs.pop(s, None)

    def aux(self, symbol: str) -> tapelib.HourlyAux:
        if symbol not in self._aux:
            self._aux[symbol] = tapelib.build_hourly_aux(self.load(symbol).hourly, self.options.strategy.atr_period)
        return self._aux[symbol]

    def span(self, symbol: str) -> tuple[int, int]:
        h = self.load(symbol).hourly
        return h[0].timestamp, h[-1].timestamp + 3_600_000

    # -- tapes
    def _sp(self) -> dict:
        s = self.options.strategy
        return dict(ema_fast_period=s.ema_fast_period, ema_slow_period=s.ema_slow_period, atr_period=s.atr_period,
                    atr_flat_threshold=s.atr_flat_threshold, atr_rolling_window=s.atr_rolling_window)

    def _warm(self) -> int:
        s = self.options.strategy
        return max(s.ema_slow_period + 5, s.atr_rolling_window + 5)

    def replay_tapes(self, symbols: list[str]) -> dict[str, tapelib.SignalTape]:
        pairs = {s: self.load(s) for s in symbols}
        tl = _full_timeline(pairs, True)
        out = {}
        for s in symbols:
            path = self.tape_dir / f"{self.fp}_{self._sp_hash}_replay_{s}_{'+'.join(sorted(symbols))}_{timeline_key(tl)}.pkl"
            if path.exists():
                out[s] = pickle.loads(path.read_bytes())
                continue
            self.progress(f"building replay signal tape for {s} ({len(tl)} polls)")
            t = tapelib.build_replay_tape(s, pairs[s].hourly, tl, pairs[s].exec_bars, self._sp(), limit=self.options.strategy.ohlcv_limit, warm=self._warm())
            path.write_bytes(pickle.dumps(t, protocol=pickle.HIGHEST_PROTOCOL))
            out[s] = t
        return out

    def closed_tapes(self, symbols: list[str]) -> dict[str, tapelib.SignalTape]:
        out = {}
        for s in symbols:
            path = self.tape_dir / f"{self.fp}_{self._sp_hash}_closed_{s}.pkl"
            if path.exists():
                out[s] = pickle.loads(path.read_bytes())
                continue
            p = self.load(s)
            t = tapelib.build_closed_tape(s, p.hourly, self._sp(), limit=self.options.strategy.ohlcv_limit, warm=self._warm())
            path.write_bytes(pickle.dumps(t, protocol=pickle.HIGHEST_PROTOCOL))
            out[s] = t
        return out

    # -- runs
    def config(self, policy: Policy = Policy(), mode: str = MODE_REPLAY, *, auto: bool = False, multiplier: float = 1.0,
               start_ts: int | None = None, end_ts: int | None = None) -> BacktestConfig:
        o = self.options
        return BacktestConfig(mode=mode, strategy=o.strategy, account=o.account, costs=replace(o.costs, multiplier=multiplier), auto=auto,
                              policy=policy, start_ts=start_ts, end_ts=end_ts)

    def run(self, symbols: list[str], cfg: BacktestConfig) -> RunDigest:
        pairs = {s: self.load(s) for s in symbols}
        tapes = self.replay_tapes(symbols) if cfg.mode == MODE_REPLAY else None
        ctapes = self.closed_tapes(symbols)
        res = run_backtest(pairs, cfg, tapes=tapes, closed_tapes=ctapes, aux={s: self.aux(s) for s in symbols})
        return digest(res)


# ------------------------------------------------------------------ the runner


def _by_pair(digests: dict[str, RunDigest]) -> dict[str, list[TradeRecord]]:
    return {s: d.counted() for s, d in digests.items()}


def _auto_by_pair(d: RunDigest, symbols: list[str]) -> dict[str, list[TradeRecord]]:
    out = {s: [] for s in symbols}
    for t in d.counted():
        out.setdefault(t.symbol, []).append(t)
    return out


def _sharpe_daily(rets: list[float]) -> float | None:
    if len(rets) < 30:
        return None
    sd = statistics.pstdev(rets)
    return statistics.fmean(rets) / sd if sd > 0 else None


def _equal_weight(series: list[list[float]]) -> list[float]:
    n = min((len(s) for s in series), default=0)
    return [statistics.fmean(s[i] for s in series) for i in range(n)]


def _pooled_stats(trades: list[TradeRecord]) -> dict:
    s = trade_stats([t for t in trades if t.exit_reason != "end_of_data"])
    return {k: s.get(k) for k in ("n_trades", "win_rate", "expectancy_r", "expectancy_r_gross", "profit_factor", "avg_cost_r", "net_pnl")}


def run_phase1(root: Path, out_dir: Path, options: Phase1Options | None = None, *, plan: tuple[Candidate, ...] = PLAN,
               baseline_json: Path | None = None, skip_repro: bool = False, expect_fingerprint: str | None = None,
               progress: Callable[[str], None] = lambda m: None) -> dict:
    options = options or Phase1Options()
    root, out_dir = Path(root), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    lab = Lab(root, out_dir, options, progress)
    if expect_fingerprint and lab.fp != expect_fingerprint:
        raise RuntimeError(f"the data fingerprint is {lab.fp}, not the expected {expect_fingerprint}: this is not the dataset the baseline was run on")
    registry = TrialRegistry(out_dir / "trials.jsonl")
    screen = options.screen
    dev = [datalib.symbol_for(p) for p in options.dev_pairs]
    hold = [datalib.symbol_for(p) for p in options.holdout_pairs]
    auto_syms = [datalib.symbol_for(p) for p in options.auto_pairs]
    all_syms = dev + [h for h in hold if h not in dev]
    present = [s for s in all_syms if datalib.kline_path(root, "1h", s).exists() and datalib.kline_path(root, "5m", s).exists()]
    missing = [s for s in all_syms if s not in present]
    dev = [s for s in dev if s in present]
    hold = [s for s in hold if s in present]
    if len(dev) < 2:
        raise RuntimeError("Phase 1 needs 1h and 5-minute data for the development pairs")

    spans = {s: lab.span(s) for s in present}
    first_ts = min(a for a, _ in spans.values())
    last_ts = min(b for _, b in spans.values())
    box = Lockbox(first_ts=first_ts, last_ts=last_ts, months=options.lockbox_months, holdout_symbols=frozenset(hold), embargo_days=options.lockbox_embargo_days)
    dev_start, dev_end = box.develop_range()
    folds = make_folds(dev_start, dev_end, train_days=options.train_days, test_days=options.test_days, embargo_days=options.fold_embargo_days)
    box.assert_tunable(dev)
    results: dict = {
        "meta": {
            "plan_hash": plan_hash(options, plan), "full_plan_hash": plan_hash(options), "partial": plan != PLAN, "code_version": code_version(),
            "data_fingerprint": lab.fp, "synthetic": synthetic.is_synthetic(root), "generated_at": datetime.now(timezone.utc).isoformat(),
            "dev_pairs": dev, "holdout_pairs": hold, "missing_pairs": missing,
            "span": [first_ts, last_ts], "development_range": [dev_start, dev_end], "lockbox_range": list(box.lockbox_range()),
            "folds": [{"index": f.index, "train": list(f.train), "test": list(f.test)} for f in folds],
            "account": asdict(options.account), "costs": asdict(options.costs), "screen": asdict(screen),
            "n_trials": 0,
        },
        "repro": [], "diagnostics": {}, "candidates": {}, "families": {}, "survivors": [], "stress": {}, "holdout": {}, "dsr": {}, "tiers": {},
    }

    fixed = [c for c in plan if c.scope == "fixed"]
    autos = [c for c in plan if c.scope == "auto"]
    cand_runs: dict[str, dict[str, RunDigest]] = {c.id: {} for c in fixed}
    base_dev: dict[str, RunDigest] = {}
    closed_dev: dict[str, RunDigest] = {}
    base_hold: dict[str, RunDigest] = {}
    flip: dict = {}
    repro_full: dict[str, RunDigest] = {}

    # ---- Pass A: development pairs, one at a time
    for sym in dev:
        progress(f"[{sym}] development runs")
        lab.load(sym)
        base_dev[sym] = lab.run([sym], lab.config(Policy(), MODE_REPLAY, start_ts=dev_start, end_ts=dev_end))
        closed_dev[sym] = lab.run([sym], lab.config(Policy(), MODE_CLOSED, start_ts=dev_start, end_ts=dev_end))
        if not skip_repro:
            repro_full[f"{sym}|{MODE_REPLAY}"] = lab.run([sym], lab.config(Policy(), MODE_REPLAY))
            repro_full[f"{sym}|{MODE_CLOSED}"] = lab.run([sym], lab.config(Policy(), MODE_CLOSED))
        for c in fixed:
            cand_runs[c.id][sym] = lab.run([sym], lab.config(c.policy, MODE_REPLAY, start_ts=dev_start, end_ts=dev_end))
        tl = _full_timeline({sym: lab.load(sym)}, True)
        import bisect as _b
        flip[sym] = analysis.cross_event_study(lab.replay_tapes([sym])[sym], tl, _b.bisect_left(tl, dev_start), _b.bisect_left(tl, dev_end))
        lab.drop([sym])
    # ---- holdout pairs: baseline only (descriptive + reproduction); candidates only if they survive
    for sym in hold:
        progress(f"[{sym}] holdout baseline")
        lab.load(sym)
        base_hold[sym] = lab.run([sym], lab.config(Policy(), MODE_REPLAY, start_ts=dev_start, end_ts=dev_end))
        if not skip_repro:
            repro_full[f"{sym}|{MODE_REPLAY}"] = lab.run([sym], lab.config(Policy(), MODE_REPLAY))
            repro_full[f"{sym}|{MODE_CLOSED}"] = lab.run([sym], lab.config(Policy(), MODE_CLOSED))
        lab.drop([sym])
    # ---- Auto Select (the original four together)
    auto_base: RunDigest | None = None
    auto_closed: RunDigest | None = None
    auto_runs: dict[str, list[RunDigest]] = {}
    asyms = [s for s in auto_syms if s in present]
    if len(asyms) >= 2 and (autos or not skip_repro):
        progress("[AUTO] original four pairs")
        for s in asyms:
            lab.load(s)
        auto_base = lab.run(asyms, lab.config(Policy(), MODE_REPLAY, auto=True, start_ts=dev_start, end_ts=dev_end))
        auto_closed = lab.run(asyms, lab.config(Policy(), MODE_CLOSED, auto=True, start_ts=dev_start, end_ts=dev_end))
        if not skip_repro:
            repro_full[f"AUTO|{MODE_REPLAY}"] = lab.run(asyms, lab.config(Policy(), MODE_REPLAY, auto=True))
            repro_full[f"AUTO|{MODE_CLOSED}"] = lab.run(asyms, lab.config(Policy(), MODE_CLOSED, auto=True))
        for c in autos:
            seeds = c.seeds or (0,)
            auto_runs[c.id] = [lab.run(asyms, lab.config(replace(c.policy, auto_seed=sd), MODE_REPLAY, auto=True, start_ts=dev_start, end_ts=dev_end)) for sd in seeds]
        lab.drop(asyms)

    # ---- S0 reproduction of the Phase 0 baseline
    if baseline_json is not None and not skip_repro:
        results["repro"] = _reproduction(Path(baseline_json), repro_full)

    # ---- S1 descriptive studies on the baseline
    base_tr = _by_pair(base_dev)
    hold_tr = _by_pair(base_hold)
    results["diagnostics"] = {
        "sides_development": analysis.side_persistence(base_tr),
        "sides_by_test_fold": analysis.side_by_fold(base_tr, folds),
        "sides_holdout": analysis.side_persistence(hold_tr) if hold_tr else None,
        "volatility_development": analysis.volatility_study(base_tr),
        "volatility_holdout": analysis.volatility_study(hold_tr) if hold_tr else None,
        "adx_development": analysis.adx_study(base_tr),
        "adx_holdout": analysis.adx_study(hold_tr) if hold_tr else None,
        "exits_development": analysis.exit_study(base_tr),
        "exits_holdout": analysis.exit_study(hold_tr) if hold_tr else None,
        "cross_event_flips": flip,
        "baseline_vs_closed_candle": {
            sym: {"replay": _pooled_stats(base_dev[sym].counted()), "closed": _pooled_stats(closed_dev[sym].counted()),
                  "replay_dd": base_dev[sym].max_dd_pct, "closed_dd": closed_dev[sym].max_dd_pct} for sym in dev
        },
        "filter_counterfactuals": _counterfactuals(plan, base_tr),
    }
    if auto_base is not None and auto_closed is not None:
        results["diagnostics"]["auto_baseline"] = {"replay": _pooled_stats(auto_base.counted()), "closed": _pooled_stats(auto_closed.counted()),
                                                   "replay_dd": auto_base.max_dd_pct, "closed_dd": auto_closed.max_dd_pct}

    # ---- S2 candidates
    base_trades_all = base_tr
    base_dd = {s: d.max_dd_pct for s, d in base_dev.items()}
    sharpes: dict[str, float] = {}
    cand_daily: dict[str, list[float]] = {}
    for c in fixed:
        digs = cand_runs[c.id]
        ctr = _by_pair(digs)
        cmp_ = analysis.compare(ctr, base_trades_all, {s: d.max_dd_pct for s, d in digs.items()}, base_dd, screen)
        folds_out = analysis.fold_table([t for ts in ctr.values() for t in ts], [t for ts in base_trades_all.values() for t in ts], folds)
        if "checks" in cmp_:
            analysis.apply_oos_checks(cmp_, folds_out, screen)
        daily = _equal_weight([d.daily for d in digs.values()])
        cand_daily[c.id] = daily
        sh = _sharpe_daily(daily)
        if sh is not None:
            sharpes[c.id] = sh
        results["candidates"][c.id] = _candidate_entry(c, cmp_, folds_out, sh, digs)
        registry.record_trial(family="phase1", config={"id": c.id, "policy": asdict(c.policy), "scope": c.scope}, data_hash=lab.fp,
                              summary={"n_trades": cmp_["candidate"]["n_trades"], "expectancy_r": cmp_["candidate"]["expectancy_r"], "sharpe_daily": sh},
                              note=c.label)
    if auto_base is not None:
        abase = _auto_by_pair(auto_base, asyms)
        for c in autos:
            runs = auto_runs[c.id]
            if c.seeds:  # the random null benchmark: per-seed results, no screen
                base_r = analysis.mean_r(auto_base.counted())
                per_seed = []
                for sd, r in zip(c.seeds, runs):
                    rr = analysis.mean_r(r.counted())
                    per_seed.append({"seed": sd, "n_trades": len(r.counted()), "expectancy_r": rr, "delta_vs_production_score": (rr - base_r) if (rr is not None and base_r is not None) else None,
                                     "max_drawdown_pct": r.max_dd_pct})
                    registry.record_trial(family="phase1", config={"id": c.id, "seed": sd, "policy": asdict(c.policy), "scope": c.scope}, data_hash=lab.fp,
                                          summary={"n_trades": len(r.counted()), "expectancy_r": rr, "sharpe_daily": _sharpe_daily(r.daily)}, note=c.label)
                vals = [x["expectancy_r"] for x in per_seed if x["expectancy_r"] is not None]
                results["candidates"][c.id] = {"id": c.id, "hypothesis": c.hypothesis, "family": c.family, "label": c.label, "policy": asdict(c.policy), "scope": c.scope,
                                               "diagnostic": True, "null_benchmark": True, "per_seed": per_seed, "mean_expectancy_r": statistics.fmean(vals) if vals else None,
                                               "spread_expectancy_r": (max(vals) - min(vals)) if vals else None, "production_score_expectancy_r": base_r, "passed_screen": False,
                                               "failed_checks": []}
                continue
            ctr = _auto_by_pair(runs[0], asyms)
            cmp_ = analysis.compare(ctr, abase, {s: runs[0].max_dd_pct for s in asyms}, {s: auto_base.max_dd_pct for s in asyms}, screen)
            folds_out = analysis.fold_table([t for ts in ctr.values() for t in ts], [t for ts in abase.values() for t in ts], folds)
            if "checks" in cmp_:
                analysis.apply_oos_checks(cmp_, folds_out, screen)
            cand_daily[c.id] = runs[0].daily
            sh = _sharpe_daily(runs[0].daily)
            if sh is not None:
                sharpes[c.id] = sh
            results["candidates"][c.id] = _candidate_entry(c, cmp_, folds_out, sh, None)
            registry.record_trial(family="phase1", config={"id": c.id, "policy": asdict(c.policy), "scope": c.scope}, data_hash=lab.fp,
                                  summary={"n_trades": cmp_["candidate"]["n_trades"], "expectancy_r": cmp_["candidate"]["expectancy_r"], "sharpe_daily": sh}, note=c.label)
        results["diagnostics"]["auto_null_benchmark_note"] = "H5-RND is the null: if a scoring rule cannot beat random choice, its ranking carries no information"

    # families: walk-forward selection and neighbour stability
    results["families"] = _families(plan, results["candidates"], cand_runs, base_trades_all, folds, registry, lab.fp)
    n_trials = sum(max(1, len(c.seeds)) for c in plan)
    results["meta"]["n_trials"] = n_trials

    # ---- survivors
    survivors = [c.id for c in plan if not c.diagnostic and results["candidates"].get(c.id, {}).get("passed_screen")]
    results["survivors"] = survivors
    surv_fixed = [c for c in fixed if c.id in survivors]
    surv_auto = [c for c in autos if c.id in survivors]

    # ---- S3: cost stress, holdout pairs, deflated Sharpe  (survivors only)
    if survivors:
        progress(f"survivors: {', '.join(survivors)}")
        stress_base: dict[float, dict[str, RunDigest]] = {m: {} for m in options.stress}
        stress_cand: dict[str, dict[float, dict[str, RunDigest]]] = {c.id: {m: {} for m in options.stress} for c in surv_fixed}
        hold_cand: dict[str, dict[str, RunDigest]] = {c.id: {} for c in surv_fixed}
        if surv_fixed:
            for sym in dev:
                lab.load(sym)
                for m in options.stress:
                    stress_base[m][sym] = lab.run([sym], lab.config(Policy(), MODE_REPLAY, multiplier=m, start_ts=dev_start, end_ts=dev_end))
                    for c in surv_fixed:
                        stress_cand[c.id][m][sym] = lab.run([sym], lab.config(c.policy, MODE_REPLAY, multiplier=m, start_ts=dev_start, end_ts=dev_end))
                lab.drop([sym])
            for sym in hold:
                lab.load(sym)
                for c in surv_fixed:
                    hold_cand[c.id][sym] = lab.run([sym], lab.config(c.policy, MODE_REPLAY, start_ts=dev_start, end_ts=dev_end))
                lab.drop([sym])
        for c in surv_fixed:
            results["stress"][c.id] = {
                str(m): {"candidate": _pooled_stats([t for d in stress_cand[c.id][m].values() for t in d.counted()]),
                         "baseline": _pooled_stats([t for d in stress_base[m].values() for t in d.counted()])} for m in options.stress
            }
            hc = {s: d.counted() for s, d in hold_cand[c.id].items()}
            results["holdout"][c.id] = _holdout_entry(hc, hold_tr, screen)
        if surv_auto:
            for s in asyms:
                lab.load(s)
            stress_auto_base = {m: lab.run(asyms, lab.config(Policy(), MODE_REPLAY, auto=True, multiplier=m, start_ts=dev_start, end_ts=dev_end)) for m in options.stress}
            for c in surv_auto:
                results["stress"][c.id] = {}
                for m in options.stress:
                    runs = [lab.run(asyms, lab.config(replace(c.policy, auto_seed=sd), MODE_REPLAY, auto=True, multiplier=m, start_ts=dev_start, end_ts=dev_end)) for sd in (c.seeds or (0,))]
                    results["stress"][c.id][str(m)] = {"candidate": _pooled_stats([t for r in runs for t in r.counted()]), "baseline": _pooled_stats(stress_auto_base[m].counted())}
                results["holdout"][c.id] = {"note": "Auto Select universe is the original four pairs by design; there are no holdout pairs for it"}
            lab.drop(asyms)
        for cid in survivors:
            results["dsr"][cid] = deflated_sharpe_ratio(cand_daily[cid], n_trials, list(sharpes.values()))
        for cid in survivors:
            results["tiers"][cid] = _tier(cid, results, screen)
    return results


# ------------------------------------------------------------------ helpers for the runner


def _candidate_entry(c: Candidate, cmp_: dict, folds_out: dict, sharpe: float | None, digs: dict[str, RunDigest] | None) -> dict:
    checks = cmp_.get("checks", {})
    failed = [k for k, v in checks.items() if not v]
    entry = {"id": c.id, "hypothesis": c.hypothesis, "family": c.family, "label": c.label, "policy": asdict(c.policy), "scope": c.scope,
             "diagnostic": c.diagnostic, "compare": cmp_, "walk_forward": folds_out, "sharpe_daily": sharpe,
             "passed_screen": (not c.diagnostic) and bool(checks) and not failed, "failed_checks": failed}
    if digs is not None:
        entry["counters"] = {s: d.counters for s, d in digs.items()}
    return entry


def _holdout_entry(cand_by_pair: dict[str, list[TradeRecord]], base_by_pair: dict[str, list[TradeRecord]], screen: analysis.Screen) -> dict:
    per = {}
    for sym, bts in base_by_pair.items():
        cm = analysis.mean_r(cand_by_pair.get(sym, []))
        bm = analysis.mean_r(bts)
        per[sym] = {"candidate_r": cm, "baseline_r": bm, "delta": (cm - bm) if (cm is not None and bm is not None) else None,
                    "n_candidate": len(cand_by_pair.get(sym, [])), "n_baseline": len(bts)}
    pooled_c = analysis.mean_r([t for ts in cand_by_pair.values() for t in ts])
    pooled_b = analysis.mean_r([t for ts in base_by_pair.values() for t in ts])
    not_worse = sum(1 for v in per.values() if v["delta"] is not None and v["delta"] >= 0)
    return {"per_pair": per, "pooled_candidate_r": pooled_c, "pooled_baseline_r": pooled_b,
            "pooled_delta": (pooled_c - pooled_b) if (pooled_c is not None and pooled_b is not None) else None, "pairs_not_worse": not_worse,
            "pass": pooled_c is not None and pooled_b is not None and pooled_c >= pooled_b and not_worse >= min(screen.holdout_min_pairs_not_worse, len(per))}


def _tier(cid: str, results: dict, screen: analysis.Screen) -> dict:
    reasons = []
    for m, row in results["stress"].get(cid, {}).items():
        ce, be = row["candidate"].get("expectancy_r"), row["baseline"].get("expectancy_r")
        if ce is None or be is None or ce - be < screen.stress_min_delta_r:
            reasons.append(f"C10 cost stress {m}x: improvement {None if ce is None or be is None else round(ce - be, 3)} below {screen.stress_min_delta_r}")
    h = results["holdout"].get(cid, {})
    if "pass" in h and not h["pass"]:
        reasons.append("C11 holdout pairs: not at least as good as baseline")
    dsr = (results["dsr"].get(cid) or {}).get("dsr")
    if reasons:
        return {"tier": "REJECTED_AFTER_STRESS", "reasons": reasons, "dsr": dsr}
    if dsr is not None and dsr >= screen.min_dsr:
        return {"tier": "ROBUST", "reasons": [], "dsr": dsr}
    return {"tier": "SUGGESTIVE", "reasons": [f"deflated Sharpe {dsr if dsr is None else round(dsr, 3)} is below {screen.min_dsr}: the improvement is not distinguishable from the best of {results['meta']['n_trials']} trials"], "dsr": dsr}


def _families(plan, cands: dict, cand_runs: dict, base_by_pair: dict, folds, registry: TrialRegistry, fp: str) -> dict:
    out = {}
    fams: dict[str, list[Candidate]] = {}
    for c in plan:
        if c.scope == "fixed" and not c.diagnostic:
            fams.setdefault(c.family, []).append(c)
    base_all = [t for ts in base_by_pair.values() for t in ts]
    for fam, members in fams.items():
        if len(members) < 2 or not folds:
            continue
        trades = {c.id: [t for ts in _by_pair(cand_runs[c.id]).values() for t in ts] for c in members}

        def evaluate(params: dict, start: int, end: int, trades=trades) -> dict:
            win = analysis.in_window(trades[params["id"]], start, end)
            return {"n_trades": len(win), "expectancy_r": analysis.mean_r(win)}

        wf = walk_forward(evaluate, [{"id": c.id} for c in members], folds, registry=registry, family=f"phase1_wf_{fam}", data_hash=fp, min_trades=30)
        chosen_oos = []
        for row in wf["rows"]:
            if row.get("chosen"):
                chosen_oos += analysis.in_window(trades[row["chosen"]["id"]], *row["test"])
        base_oos = [t for f in folds for t in analysis.in_window(base_all, *f.test)]
        sel, bas = analysis.mean_r(chosen_oos), analysis.mean_r(base_oos)
        out[fam] = {"members": [c.id for c in members], "walk_forward": wf, "selected_oos_r": sel, "baseline_oos_r": bas,
                    "selected_oos_delta": (sel - bas) if (sel is not None and bas is not None) else None,
                    "neighbour_stability": analysis.neighbour_stability([(c.id, cands[c.id]["compare"].get("delta_r", 0.0) if "delta_r" in cands[c.id]["compare"] else 0.0) for c in members])}
    return out


def _counterfactuals(plan, base_by_pair: dict[str, list[TradeRecord]]) -> dict:
    """What an entry FILTER would have removed from the baseline trades. Persistence and wait-for-close rules delay an
    entry instead of removing it, so they have no counterfactual here; the reference row shows the most any of them could
    remove (the entries that were not still valid at the candle close) and the cross-event study covers the rest."""
    trades = [t for ts in base_by_pair.values() for t in ts]
    preds: dict[str, Callable[[TradeRecord], bool]] = {"ref-entries-not-valid-at-close": lambda t: t.signal_confirmed_at_close is False}
    for c in plan:
        if c.diagnostic or c.scope != "fixed":
            continue
        p = c.policy
        if p.confirm_margin_atr > 0 and p.confirm_polls <= 1:
            preds[c.id] = lambda t, p=p: t.side in p.confirm_sides and (t.features.get("gap_atr_at_entry") or 0.0) < p.confirm_margin_atr
        elif p.vol_skip_above is not None:
            preds[c.id] = lambda t, p=p: (t.features.get("vol_pct_90d") is not None) and t.features["vol_pct_90d"] > p.vol_skip_above
        elif p.short_max_ret30d is not None:
            preds[c.id] = lambda t, p=p: t.side == "short" and t.features.get("ret30d") is not None and t.features["ret30d"] > p.short_max_ret30d
    return {cid: analysis.filter_counterfactual(trades, fn) for cid, fn in preds.items()}


def _reproduction(baseline_json: Path, runs: dict[str, RunDigest]) -> list[dict]:
    """Phase 1 engine, default policy, full period, against the numbers Phase 0 wrote. They must be identical."""
    saved = json.loads(Path(baseline_json).read_text())
    rows = []
    for key, d in runs.items():
        sym, mode = key.split("|")
        k0 = f"{sym}|{mode}|x1.0"
        old = saved.get("runs", {}).get(k0)
        if old is None:
            continue
        s = trade_stats(d.counted())
        n0, e0 = old["summary"].get("n_trades"), old["summary"].get("expectancy_r")
        rows.append({"run": k0, "phase0_trades": n0, "phase1_trades": s.get("n_trades"), "phase0_expectancy_r": e0, "phase1_expectancy_r": s.get("expectancy_r"),
                     "identical": n0 == s.get("n_trades") and e0 is not None and s.get("expectancy_r") is not None and abs(e0 - s["expectancy_r"]) < 1e-9})
    return rows


# ------------------------------------------------------------------ lockbox (explicit, once)


def run_lockbox(root: Path, out_dir: Path, candidate_id: str, options: Phase1Options | None = None, *, plan: tuple[Candidate, ...] = PLAN,
                confirm: bool = False, reason: str = "", progress: Callable[[str], None] = lambda m: None) -> dict:
    """Score ONE candidate on the sealed final months, once, for the pairs it was developed on and the six holdout pairs.
    The opening is written to the ledger; a second opening marks the lockbox contaminated."""
    options = options or Phase1Options()
    cand = next((c for c in plan if c.id == candidate_id), None)
    if cand is None or cand.diagnostic:
        raise ValueError(f"{candidate_id} is not an adoptable candidate in the plan")
    root, out_dir = Path(root), Path(out_dir)
    lab = Lab(root, out_dir, options, progress)
    registry = TrialRegistry(out_dir / "trials.jsonl")
    dev = [datalib.symbol_for(p) for p in options.dev_pairs]
    hold = [datalib.symbol_for(p) for p in options.holdout_pairs]
    present = [s for s in dev + hold if datalib.kline_path(root, "5m", s).exists()]
    spans = {s: lab.span(s) for s in present}
    box = Lockbox(first_ts=min(a for a, _ in spans.values()), last_ts=min(b for _, b in spans.values()), months=options.lockbox_months,
                  holdout_symbols=frozenset(hold), embargo_days=options.lockbox_embargo_days)
    opening = box.open(registry, family="phase1_lockbox", reason=reason or f"final check of {candidate_id}", confirm=confirm)
    lo, hi = box.lockbox_range()
    out: dict = {"candidate": candidate_id, "label": cand.label, "range": [lo, hi], "opening": opening, "per_pair": {}}
    cand_all, base_all = [], []
    syms = [s for s in dev + hold if s in present]
    if cand.scope == "auto":
        asyms = [datalib.symbol_for(p) for p in options.auto_pairs]
        for s in asyms:
            lab.load(s)
        b = lab.run(asyms, lab.config(Policy(), MODE_REPLAY, auto=True, start_ts=lo, end_ts=hi))
        c = lab.run(asyms, lab.config(cand.policy, MODE_REPLAY, auto=True, start_ts=lo, end_ts=hi))
        out["auto"] = {"baseline": _pooled_stats(b.counted()), "candidate": _pooled_stats(c.counted()), "baseline_dd": b.max_dd_pct, "candidate_dd": c.max_dd_pct}
    else:
        for sym in syms:
            lab.load(sym)
            b = lab.run([sym], lab.config(Policy(), MODE_REPLAY, start_ts=lo, end_ts=hi))
            c = lab.run([sym], lab.config(cand.policy, MODE_REPLAY, start_ts=lo, end_ts=hi))
            out["per_pair"][sym] = {"role": "development" if sym in dev else "holdout", "baseline": _pooled_stats(b.counted()), "candidate": _pooled_stats(c.counted()),
                                    "baseline_dd": b.max_dd_pct, "candidate_dd": c.max_dd_pct}
            cand_all += c.counted()
            base_all += b.counted()
            lab.drop([sym])
        out["pooled"] = {"baseline": _pooled_stats(base_all), "candidate": _pooled_stats(cand_all)}
    (out_dir / f"lockbox_{candidate_id}.json").write_text(json.dumps(out, indent=1, default=str))
    return out
