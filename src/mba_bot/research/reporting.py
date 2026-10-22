"""Baseline research report for the CURRENT strategy (EMA 20/50 + ATR stop, Futures).

It answers one question with evidence instead of opinion: after realistic costs, does the strategy
the service runs today have a positive expectancy, where does it come from, and how sure can we be?

Each pair is backtested in both execution modes (see engine.py):
  production_replay  what the service does today (signals read from the forming candle)
  closed_candle      the same signal read only from a closed candle (a Phase 1 candidate, measured here
                     so the decision to build it rests on numbers)
at several cost levels, plus an Auto Select replay over the original four pairs. Nothing is tuned: the
parameters are production's, so there is no selection bias in the baseline itself.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from ..venue import rules_from_market
from . import data as datalib
from . import quality, synthetic
from .engine import MODE_CLOSED, MODE_REPLAY, AccountParams, BacktestConfig, CostModel, PairData, StrategyParams, run_backtest
from .metrics import breakdown, quarter_of, summarize, year_of
from .regimes import label_trades

ORIGINAL_FOUR = ("BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT")
EXTRA_SIX = ("SOL/USDT", "DOGE/USDT", "SUI/USDT", "LINK/USDT", "ADA/USDT", "LTC/USDT")


# ------------------------------------------------------------------ loading


def load_pair_data(root: Path, pair: str, *, with_5m: bool) -> PairData:
    sym = datalib.symbol_for(pair)
    info = datalib.load_exchange_info(root).get(sym)
    rules = rules_from_market({"info": info}) if info else None
    exec_bars = datalib.load_candles(root, sym, "5m") if with_5m else None
    return PairData(sym, datalib.load_candles(root, sym, "1h"), exec_bars, datalib.load_funding(root, sym), rules)


def data_hash(root: Path) -> str:
    manifest = Path(root) / "manifest.json"
    if not manifest.exists():
        return "no-manifest"
    files = json.loads(manifest.read_text()).get("files", {})
    blob = json.dumps({k: v["sha256"] for k, v in sorted(files.items())}, sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


# ------------------------------------------------------------------ one run (also the worker)


@dataclass
class RunSpec:
    key: str
    symbols: list[str]
    config: BacktestConfig


def _tradedict(t) -> dict:
    d = asdict(t)
    d["features"] = {k: d["features"].get(k) for k in ("adx14", "efficiency_ratio20", "ema_gap_atr", "atr_ratio", "ret_24", "bar_progress_at_entry")}
    return d


def execute_run(root: str, spec: RunSpec) -> dict:
    root_p = Path(root)
    pairs = {s: load_pair_data(root_p, datalib.pair_for(s), with_5m=spec.config.mode == MODE_REPLAY or _has_5m(root_p, s)) for s in spec.symbols}
    result = run_backtest(pairs, spec.config)
    hourly = {s: p.hourly for s, p in pairs.items()}
    label_trades(result.trades, hourly)
    counted = result.counted_trades()
    summary = summarize(result)
    summary["sharpe_daily"] = _daily_sharpe(result)
    return {
        "key": spec.key, "symbols": spec.symbols, "mode": spec.config.mode, "cost_multiplier": spec.config.costs.multiplier,
        "summary": summary, "notes": result.notes,
        "by_year": breakdown(counted, year_of), "by_quarter": breakdown(counted, quarter_of),
        "by_side": breakdown(counted, lambda t: t.side), "by_exit": breakdown(counted, lambda t: t.exit_reason),
        "by_pair": breakdown(counted, lambda t: t.symbol),
        "by_trend_state": breakdown(counted, lambda t: t.tags.get("trend_state")),
        "by_market_30d": breakdown(counted, lambda t: t.tags.get("market_30d")),
        "by_volatility": breakdown(counted, lambda t: t.tags.get("volatility")),
        "by_session": breakdown(counted, lambda t: t.tags.get("session")),
        "equity": result.equity[:: max(1, len(result.equity) // 600)],
        "trades": [_tradedict(t) for t in result.trades],
        "daily_returns": _daily(result),
    }


def _has_5m(root: Path, symbol: str) -> bool:
    return datalib.kline_path(root, "5m", symbol).exists()


def _daily(result) -> list[float]:
    from .metrics import daily_returns

    return daily_returns(result.equity)


def _daily_sharpe(result) -> float | None:
    import statistics

    r = _daily(result)
    if len(r) < 30:
        return None
    sd = statistics.pstdev(r)
    return statistics.fmean(r) / sd if sd > 0 else None


# ------------------------------------------------------------------ the report


@dataclass
class BaselineOptions:
    pairs: tuple[str, ...] = ORIGINAL_FOUR + EXTRA_SIX
    holdout_pairs: tuple[str, ...] = EXTRA_SIX
    auto_pairs: tuple[str, ...] = ORIGINAL_FOUR
    cost_multipliers: tuple[float, ...] = (1.0, 1.5, 2.0)
    account: AccountParams = field(default_factory=lambda: AccountParams(allocated_capital=1000.0))
    strategy: StrategyParams = field(default_factory=StrategyParams)
    costs: CostModel = field(default_factory=CostModel)
    run_auto: bool = True
    workers: int = 1
    min_years: float = 3.0


def build_baseline(root: Path, out_dir: Path, options: BaselineOptions, *, progress=lambda m: None) -> dict:
    root, out_dir = Path(root), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pairs = [p for p in options.pairs if _has_1h(root, p)]
    skipped = [p for p in options.pairs if p not in pairs]
    has_5m = {p: _has_5m(root, datalib.symbol_for(p)) for p in pairs}
    q = quality.check_store(root, pairs=list(pairs), intervals=("1h", "5m") if any(has_5m.values()) else ("1h",), min_years=options.min_years)

    specs: list[RunSpec] = []
    for p in pairs:
        sym = datalib.symbol_for(p)
        for mult in options.cost_multipliers:
            costs = replace(options.costs, multiplier=mult)
            for mode in (MODE_REPLAY, MODE_CLOSED):
                if mode == MODE_REPLAY and not has_5m[p]:
                    continue
                cfg = BacktestConfig(mode=mode, strategy=options.strategy, account=options.account, costs=costs)
                specs.append(RunSpec(f"{sym}|{mode}|x{mult}", [sym], cfg))
    auto_syms = [datalib.symbol_for(p) for p in options.auto_pairs if p in pairs]
    if options.run_auto and len(auto_syms) >= 2:
        for mode in (MODE_REPLAY, MODE_CLOSED):
            if mode == MODE_REPLAY and not all(has_5m[p] for p in options.auto_pairs if p in pairs):
                continue
            specs.append(RunSpec(f"AUTO|{mode}|x1.0", auto_syms, BacktestConfig(mode=mode, strategy=options.strategy, account=options.account, costs=replace(options.costs, multiplier=1.0), auto=True)))

    runs = _execute(root, specs, options.workers, progress)
    by_key = {r["key"]: r for r in runs}
    dh = data_hash(root)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "synthetic": synthetic.is_synthetic(root),
        "data_hash": dh,
        "options": {k: (list(v) if isinstance(v, tuple) else v) for k, v in asdict(options).items() if k in ("pairs", "holdout_pairs", "auto_pairs", "cost_multipliers", "account", "strategy", "costs", "workers")},
        "skipped_pairs_no_data": skipped,
        "quality": q["summary"], "quality_files": q["files"],
        "runs": {k: {x: v for x, v in r.items() if x not in ("trades", "daily_returns")} for k, r in by_key.items()},
        "pooled": _pooled(by_key, options),
        "limitations": LIMITATIONS,
    }
    for r in runs:
        _write_trades_csv(out_dir / f"trades_{r['key'].replace('|', '_').replace('/', '')}.csv", r["trades"])
    (out_dir / "baseline.json").write_text(json.dumps(report, indent=1, default=str))
    (out_dir / "baseline.md").write_text(render_markdown(report, by_key))
    report["_daily"] = {k: r["daily_returns"] for k, r in by_key.items()}
    return report


def _has_1h(root: Path, pair: str) -> bool:
    return datalib.kline_path(root, "1h", datalib.symbol_for(pair)).exists()


def _execute(root: Path, specs: list[RunSpec], workers: int, progress) -> list[dict]:
    if workers <= 1 or len(specs) <= 1:
        out = []
        for i, s in enumerate(specs, 1):
            progress(f"[{i}/{len(specs)}] {s.key}")
            out.append(execute_run(str(root), s))
        return out
    with ProcessPoolExecutor(max_workers=min(workers, os.cpu_count() or 1)) as pool:
        futures = [pool.submit(execute_run, str(root), s) for s in specs]
        out = []
        for i, f in enumerate(futures, 1):
            out.append(f.result())
            progress(f"[{i}/{len(specs)}] done")
        return out


def _pool(runs: list[dict]) -> dict:
    """Pooled trade-level stats across the given single-pair runs (equity is per pair, so only
    trade statistics are pooled, never an equity curve)."""
    from .engine import TradeRecord
    from .metrics import trade_stats

    trades = []
    for r in runs:
        for d in r["trades"]:
            if d["exit_reason"] == "end_of_data":
                continue
            d = dict(d)
            trades.append(TradeRecord(**d))
    return trade_stats(trades)


def _pooled(by_key: dict[str, dict], options: BaselineOptions) -> dict:
    out: dict = {}
    for mode in (MODE_REPLAY, MODE_CLOSED):
        for mult in options.cost_multipliers:
            for label, group in (("development", [p for p in options.pairs if p not in options.holdout_pairs]), ("holdout", list(options.holdout_pairs))):
                keys = [f"{datalib.symbol_for(p)}|{mode}|x{mult}" for p in group]
                runs = [by_key[k] for k in keys if k in by_key]
                if runs:
                    s = _pool(runs)
                    out[f"{label}|{mode}|x{mult}"] = {x: s.get(x) for x in ("n_trades", "win_rate", "expectancy_r", "expectancy_r_gross", "profit_factor", "avg_cost_r", "net_pnl")}
    return out


def _write_trades_csv(path: Path, trades: list[dict]) -> None:
    if not trades:
        return
    cols = ["symbol", "side", "entry_ts", "exit_ts", "signal_price", "entry_price", "exit_price", "stop_price", "quantity", "planned_risk",
            "gross_pnl", "slippage_cost", "fees", "funding", "net_pnl", "r_gross", "r_net", "mfe_r", "mae_r", "hold_hours", "exit_reason",
            "entry_signal", "signal_confirmed_at_close", "same_hour_reentry", "atr_pct"]
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols + ["trend_state", "market_30d", "volatility", "session"])
        for t in trades:
            w.writerow([t.get(c) for c in cols] + [t["tags"].get(k) for k in ("trend_state", "market_30d", "volatility", "session")])


# ------------------------------------------------------------------ markdown


LIMITATIONS = [
    "Costs are ASSUMPTIONS (fee tier, slippage), not measurements. The cost-stress rows show how much the result depends on them.",
    "Historical exchange filters are not available from Binance; the latest snapshot is applied to the whole history.",
    "Poll timing is fixed to 5-minute boundaries; the service's polls have an arbitrary phase. Stop triggers assume the last price. A stop-hit bar's favourable excursion is not credited.",
    "Liquidation is not simulated; entries whose stop sits beyond ~80% of the liquidation distance are counted (liquidation_risk_entries).",
    "Bootstrap intervals treat trades as independent; real trades cluster in regimes, so the intervals are optimistic.",
    "Past performance on this sample says nothing certain about the future; one market cycle is a small sample.",
    "Funding uses the recorded funding events; borrowing/insurance fund effects and exchange outages are not modelled.",
    "Max drawdown is measured on hourly mark-to-market closes. Swings inside the hour are not in it (they appear in each trade's adverse excursion).",
    "Trades run on one pair at a time with the account sized off min(allocated capital, equity), like the service. Pairs in a table are independent accounts, not a combined portfolio.",
]


def _f(x, d=2, pct=False, plus=False):
    if x is None:
        return "n/a"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x)
    s = f"{v * (100 if pct else 1):{'+' if plus else ''}.{d}f}"
    return s + ("%" if pct else "")


def _row(*cells) -> str:
    return "| " + " | ".join(str(c) for c in cells) + " |"


def render_markdown(report: dict, by_key: dict[str, dict]) -> str:
    L: list[str] = []
    if report["synthetic"]:
        L += ["> **SYNTHETIC DATA.** Every number below comes from generated prices. It demonstrates the pipeline only and says nothing about real markets.", ""]
    L += ["# Baseline: EMA 20/50 + ATR strategy (Futures)", ""]
    L.append(f"Generated {report['generated_at']} | data fingerprint `{report['data_hash']}` | quality: {report['quality']}")
    if report["skipped_pairs_no_data"]:
        L.append(f"Pairs with no data (skipped): {', '.join(report['skipped_pairs_no_data'])}")
    acct = report["options"]["account"]
    costs = report["options"]["costs"]
    L += ["", "## Assumptions", "",
          f"- Account: allocated capital {acct['allocated_capital']}, risk {acct['risk_pct']}% per trade, notional ceiling {acct['max_notional_multiplier']}x, leverage setting {acct['leverage']}x",
          f"- Costs at 1.0x: taker fee {costs['taker_fee_bps']} bp per side, market slippage {costs['market_slip_bps']} bp, stop slippage {costs['stop_slip_bps']} bp (BTC/ETH lower), funding {'on' if costs['funding'] else 'off'}",
          "- Strategy parameters are production's (nothing tuned).", ""]

    def head_table(mode: str, mult: float = 1.0) -> list[str]:
        out = [_row("Pair", "Trades", "Win %", "E[R] net", "95% CI (bootstrap)", "E[R] gross", "PF", "Net return", "Max DD", "Sharpe", "Cost/trade (R)", "Max losing streak"),
               _row(*["---"] * 12)]
        for k, r in sorted(by_key.items()):
            if not k.endswith(f"|{mode}|x{mult}") or k.startswith("AUTO"):
                continue
            s = r["summary"]
            ci = s.get("expectancy_r_bootstrap")
            out.append(_row(k.split("|")[0], s.get("n_trades", 0), _f(s.get("win_rate"), 1, pct=True), _f(s.get("expectancy_r"), 3, plus=True),
                            f"{_f(ci['low'], 2)} to {_f(ci['high'], 2)}" if ci else "n/a", _f(s.get("expectancy_r_gross"), 3, plus=True),
                            _f(s.get("profit_factor")), _f(s.get("total_return_pct"), 1, plus=True) + "%", _f(s.get("max_drawdown_pct"), 1) + "%",
                            _f(s.get("sharpe")), _f(s.get("avg_cost_r"), 3), s.get("longest_losing_streak", "")))
        return out

    for mode, title in ((MODE_REPLAY, "What the service does today (forming-candle signals, 5-minute polls)"), (MODE_CLOSED, "Same strategy, closed-candle signals (research comparison)")):
        if any(k.endswith(f"|{mode}|x1.0") for k in by_key):
            L += [f"## {title}", ""] + head_table(mode) + [""]

    L += ["## Pooled by group (all trades pooled, net of costs)", "", _row("Group", "Mode", "Cost", "Trades", "Win %", "E[R] net", "E[R] gross", "PF", "Cost/trade (R)"), _row(*["---"] * 9)]
    for k, v in report["pooled"].items():
        grp, mode, mult = k.split("|")
        L.append(_row(grp, mode, mult, v["n_trades"], _f(v["win_rate"], 1, pct=True), _f(v["expectancy_r"], 3, plus=True), _f(v["expectancy_r_gross"], 3, plus=True), _f(v["profit_factor"]), _f(v["avg_cost_r"], 3)))
    L += ["", "*development = the original four pairs; holdout = the six added pairs, which no tuning may use.*", ""]

    L += ["## Cost sensitivity (E[R] net, pooled)", "", _row("Mode", *[f"x{m}" for m in report['options']['cost_multipliers']]), _row(*["---"] * (1 + len(report['options']['cost_multipliers'])))]
    for mode in (MODE_REPLAY, MODE_CLOSED):
        cells = []
        for m in report["options"]["cost_multipliers"]:
            vals = [v for k, v in report["pooled"].items() if k.endswith(f"|{mode}|x{m}")]
            n = sum(v["n_trades"] for v in vals)
            e = sum((v["expectancy_r"] or 0) * v["n_trades"] for v in vals) / n if n else None
            cells.append(_f(e, 3, plus=True))
        L.append(_row(mode, *cells))
    L.append("")

    # Replay-specific diagnostics
    L += ["## Forming-candle effects (production replay)", "", _row("Pair", "% entries still valid at candle close", "E[R] confirmed", "E[R] unconfirmed", "Same-candle re-entries", "E[R] of those"), _row(*["---"] * 6)]
    for k, r in sorted(by_key.items()):
        if k.endswith(f"|{MODE_REPLAY}|x1.0"):
            s = r["summary"]
            L.append(_row(k.split("|")[0], _f(s.get("pct_entries_confirmed_at_candle_close"), 1) + "%" if s.get("pct_entries_confirmed_at_candle_close") is not None else "n/a",
                          _f(s.get("expectancy_r_confirmed"), 3, plus=True), _f(s.get("expectancy_r_unconfirmed"), 3, plus=True), s.get("n_same_hour_reentries", 0), _f(s.get("expectancy_r_same_hour_reentry"), 3, plus=True)))
    L.append("")

    L += ["## Blocked entries (signal seen, but the exchange rules or sizing refused)", "", _row("Run", "Signals", "Entries", "Blocked by exchange minimums", "Blocked, size zero", "Liquidation-risk entries"), _row(*["---"] * 6)]
    for k, r in sorted(by_key.items()):
        if k.endswith("|x1.0"):
            c = r["summary"]["counters"]
            L.append(_row(k, c.get("actionable_signals"), c.get("entries"), c.get("blocked_exchange_filters"), c.get("blocked_size_zero"), c.get("liquidation_risk_entries")))
    L.append("")

    def group_table(title: str, field: str, mode: str) -> list[str]:
        out = [f"### {title} ({mode})", "", _row("Run", "Group", "Trades", "Win %", "E[R] net", "PF"), _row(*["---"] * 6)]
        for k, r in sorted(by_key.items()):
            if not k.endswith(f"|{mode}|x1.0") or k.startswith("AUTO"):
                continue
            for g, v in r[field].items():
                out.append(_row(k.split("|")[0], g, v["n_trades"], _f(v["win_rate"], 1, pct=True), _f(v["expectancy_r"], 3, plus=True), _f(v["profit_factor"])))
        return out + [""]

    L += ["## Breakdowns", ""]
    for title, fld in (("By year", "by_year"), ("By side", "by_side"), ("By exit reason", "by_exit"), ("By trend state at entry (ADX)", "by_trend_state"), ("By 30-day market move", "by_market_30d"), ("By volatility tercile", "by_volatility")):
        L += group_table(title, fld, MODE_REPLAY if any(k.endswith(f"|{MODE_REPLAY}|x1.0") for k in by_key) else MODE_CLOSED)

    autos = {k: r for k, r in by_key.items() if k.startswith("AUTO")}
    if autos:
        L += ["## Auto Select replay (original four pairs, one position at a time)", "", _row("Mode", "Trades", "Win %", "E[R] net", "PF", "Net return", "Max DD", "Pairs traded"), _row(*["---"] * 8)]
        for k, r in sorted(autos.items()):
            s = r["summary"]
            L.append(_row(k.split("|")[1], s.get("n_trades", 0), _f(s.get("win_rate"), 1, pct=True), _f(s.get("expectancy_r"), 3, plus=True), _f(s.get("profit_factor")),
                          _f(s.get("total_return_pct"), 1, plus=True) + "%", _f(s.get("max_drawdown_pct"), 1) + "%", ", ".join(f"{p}:{v['n_trades']}" for p, v in r["by_pair"].items())))
        L.append("")

    L += ["## Data quality", ""]
    for f in report["quality_files"]:
        L.append(f"- `{f['path']}`: **{f['status']}**, {f['rows']} rows, {f['years']:.2f} years" + (f", {f['missing_bars']} missing" if f["missing_bars"] else "") + (f" ({'; '.join(f['notes'])})" if f["notes"] else ""))
    L += ["", "## Limitations", ""] + [f"- {x}" for x in report["limitations"]] + [""]
    return "\n".join(L)
