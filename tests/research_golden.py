"""Fixed synthetic store and snapshot function for the Phase 0 regression guard (tests/golden/phase0_engine.json)."""
import hashlib
import json
from pathlib import Path

from mba_bot.research import reporting, synthetic
from mba_bot.research.engine import MODE_CLOSED, MODE_REPLAY, AccountParams, BacktestConfig, PairData, run_backtest


def snapshot(res):
    rows = [[t.symbol, t.side, t.entry_ts, t.exit_ts, round(t.signal_price, 8), round(t.entry_price, 8), round(t.exit_price, 8), round(t.stop_price, 8),
             round(t.quantity, 8), round(t.net_pnl, 8), round(t.fees, 8), round(t.slippage_cost, 8), round(t.funding, 8), t.exit_reason,
             t.signal_confirmed_at_close, t.same_hour_reentry, round(t.mfe_r, 6), round(t.mae_r, 6)] for t in res.trades]
    return {"n": len(rows), "trades_sha": hashlib.sha1(json.dumps(rows).encode()).hexdigest(), "counters": {k: v for k, v in res.counters.items() if k in PHASE0_COUNTERS},
            "end_equity": round(res.end_equity, 8), "curve_sha": hashlib.sha1(json.dumps([[a, round(b, 8)] for a, b in res.equity]).encode()).hexdigest()}


PHASE0_COUNTERS = {"polls", "actionable_signals", "entries", "blocked_exchange_filters", "blocked_size_zero", "blocked_no_rules", "blocked_no_stop_order",
                   "same_hour_reentries", "liquidation_risk_entries"}


def make_store(root):
    synthetic.write_store(Path(root), pairs=["BTC/USDT", "ETH/USDT", "SOL/USDT"], days=75, seed=12)


def run_all(root):
    root = Path(root)
    pds = {s: reporting.load_pair_data(root, p, with_5m=True) for s, p in (("BTCUSDT", "BTC/USDT"), ("ETHUSDT", "ETH/USDT"), ("SOLUSDT", "SOL/USDT"))}
    acct = AccountParams(allocated_capital=1000.0)
    out = {}
    for mode in (MODE_REPLAY, MODE_CLOSED):
        for sym in pds:
            out[f"{sym}|{mode}"] = snapshot(run_backtest({sym: pds[sym]}, BacktestConfig(mode=mode, account=acct)))
        out[f"AUTO|{mode}"] = snapshot(run_backtest(pds, BacktestConfig(mode=mode, account=acct, auto=True)))
    hourly_only = {s: PairData(p.symbol, p.hourly, None, p.funding, p.rules) for s, p in pds.items()}
    out["BTCUSDT|closed_hourly"] = snapshot(run_backtest({"BTCUSDT": hourly_only["BTCUSDT"]}, BacktestConfig(mode=MODE_CLOSED, account=acct)))
    return out
