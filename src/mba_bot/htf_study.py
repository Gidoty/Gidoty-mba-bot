"""The higher-timeframe study (docs/research/htf_preregistration.md). Research only: no network, no key, no order, no live effect.

Why: the cost of a trade in R is (fees + slippage + funding) / stop distance, and the stop distance in percent of price grows with the candle length. On 1h the cost is about 0.15 to 0.20 R per trade (cost split). Does a strategy on
4h or daily candles, with a stop 2 to 4 times as wide, keep an edge after costs? EMA and Donchian are tested on 4h and 1d candles and RSI on 4h, same four pairs, same costs, with a SMALL FIXED set of parameters registered first
(the shipped parameters and one classic alternative for EMA and Donchian; RSI as shipped). 4h and daily candles are built from the 1h candles (complete groups only), which is exactly how the exchange builds them.
Walk-forward: the nine 182-day windows of the entry study. The first six (2022-04 to 2025-03) are the DEVELOPMENT windows; the last three (2025-04 to 2026-09) are the CONFIRMATION windows, opened once, only for cells that passed development."""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import donchian as DN
from . import entry_study as E
from . import profit_backtest as P
from . import rsi_mr as R
from . import shortframe_backtest as SF
from .indicators import adx as adx_series
from .indicators import atr as atr_series
from .indicators import ema as ema_series
from .indicators import rsi as rsi_series
from .research import bootstrap
from .strategy import Candle

PROTOCOL = "higher-timeframe study v1: EMA and Donchian on 4h and 1d, RSI on 4h, fixed parameters, walk-forward development and one confirmation look"
PAIRS = P.PAIRS
HOUR = 3_600_000
DAY = 86_400_000
GROUP = {"4h": 4 * HOUR, "1d": DAY}
LEVEL = 0.99
BOOT = {"reps": 2000, "level": LEVEL, "seed": 20260505}
DEV_WINDOWS = (0, 1, 2, 3, 4, 5)
CONFIRM_WINDOWS = (6, 7, 8)
RULE = {"dev_min_trades": 40, "dev_min_positive_windows": 5, "confirm_min_trades": 15, "confirm_min_positive_windows": 2}
EMA_SETS = {"s1": (20, 50), "s2": (12, 26)}
DON_SETS = {"s1": (20, 10), "s2": (55, 20)}
CELLS = tuple([("ema", tf, s) for tf in ("4h", "1d") for s in ("s1", "s2")] + [("donchian", tf, s) for tf in ("4h", "1d") for s in ("s1", "s2")] + [("rsi", "4h", "s1")])


def cell_name(c) -> str:
    return f"{c[0]} {c[1]} {c[2]}"


# ================================================================================ plan
_PINNED = ("htf_study.py", "entry_study.py", "profit_backtest_dr.py", "profit_backtest.py", "shortframe_backtest.py", "donchian.py", "rsi_mr.py", "indicators.py", "strategy.py", "reporting.py", "research/bootstrap.py",
           "research/archive.py", "research/funding_archive.py")


def source_pins() -> dict:
    here = Path(__file__).parent
    return {n: hashlib.sha256((here / n).read_bytes()).hexdigest() for n in _PINNED}


def plan_blob() -> dict:
    return {"protocol": PROTOCOL, "pairs": PAIRS, "cells": CELLS, "ema_sets": EMA_SETS, "donchian_sets": DON_SETS, "ema": SF.EMA_PARAMS, "donchian_defaults": vars(DN.DonchianParams()), "rsi": vars(R.RsiParams()),
            "run": (E.RUN_START, E.RUN_END), "test_starts": E.TEST_STARTS, "test_len": E.TEST_LEN, "dev_windows": DEV_WINDOWS, "confirm_windows": CONFIRM_WINDOWS, "rule": RULE, "level": LEVEL, "bootstrap": BOOT,
            "costs": {"fee_bps": P.FEE_BPS, "slippage_round_trip_bps": P.SLIP_ROUND_TRIP_BPS}, "sources_sha256": source_pins()}


def plan_hash() -> str:
    return hashlib.sha256(json.dumps(plan_blob(), sort_keys=True, default=str).encode()).hexdigest()[:16]


def data_hash(root: Path) -> str:
    from . import profit_backtest_dr as D

    return D.data_hash(root)


# ================================================================================ candles
def aggregate(cs: list[Candle], group_ms: int) -> list[Candle]:
    """Complete groups only: a 4h or daily candle needs every one of its 1h candles."""
    need = group_ms // HOUR
    out, cur, key = [], [], None
    for c in cs:
        k = c.timestamp // group_ms
        if k != key:
            if cur and len(cur) == need:
                out.append(Candle(cur[0].timestamp, cur[0].open, max(x.high for x in cur), min(x.low for x in cur), cur[-1].close, sum(x.volume for x in cur)))
            cur, key = [], k
        cur.append(c)
    if cur and len(cur) == need:
        out.append(Candle(cur[0].timestamp, cur[0].open, max(x.high for x in cur), min(x.low for x in cur), cur[-1].close, sum(x.volume for x in cur)))
    return out


# ================================================================================ simulators
def finish(pair, side, nxt_ts, raw_entry, dist, stop0, exit_idx, raw_exit, reason, cs, events, marks, tf_ms):
    legs = [P.Leg(1.0, raw_exit, cs[exit_idx].timestamp, reason)]
    entry, gross, net, extra = P._cost(side, raw_entry, dist, legs, 0.0)
    fr = P._funding_r(side, dist, [(t, r) for t, r in events if nxt_ts < t <= cs[exit_idx].timestamp + tf_ms], marks, cs, nxt_ts, [(nxt_ts - 1, 1.0)])
    return P.SimTrade(pair, side, nxt_ts, cs[exit_idx].timestamp, raw_entry, entry, dist, stop0, entry, legs, reason, gross, net + fr, fr, extra, False, None, max(cs[exit_idx].timestamp + tf_ms - nxt_ts, tf_ms))


def ema_signals(cs: list[Candle], fast_p: int, slow_p: int):
    """The EMA strategy's crossover verdicts and ATR on any candle length: the same rule as `shortframe_backtest.ema_signal_series` with the EMA periods as arguments (flat-market filter and ATR as shipped)."""
    closes = [c.close for c in cs]
    fast, slow = ema_series(closes, fast_p), ema_series(closes, slow_p)
    atr = atr_series([c.high for c in cs], [c.low for c in cs], closes, SF.EMA_PARAMS["atr_period"])
    n, W = len(cs), SF.EMA_PARAMS["rolling_window"]
    pct = [(a / c) if (a is not None and c) else None for a, c in zip(atr, closes)]
    avg, total, cnt = [0.0] * n, 0.0, 0
    for i in range(n):
        if pct[i] is not None:
            total += pct[i]
            cnt += 1
        j = i - W
        if j >= 0 and pct[j] is not None:
            total -= pct[j]
            cnt -= 1
        avg[i] = (total / cnt) if cnt else 0.0
    out = [None] * n
    for i in range(1, n):
        if fast[i] is None or fast[i - 1] is None or slow[i] is None or slow[i - 1] is None or atr[i] is None:
            continue
        up = fast[i - 1] <= slow[i - 1] and fast[i] > slow[i]
        down = fast[i - 1] >= slow[i - 1] and fast[i] < slow[i]
        if not (up or down) or (avg[i] > 0 and pct[i] < avg[i] * SF.EMA_PARAMS["flat_threshold"]):
            continue
        out[i] = "long" if up else "short"
    return out, atr


def sim_ema(pair, cs, tf_ms, params, start_ms, end_ms, events):
    sigs, atr = ema_signals(cs, *params)
    marks = {c.timestamp: c.open for c in cs}
    out, n, i = [], len(cs), 1
    slip = P.SLIP_PER_SIDE_BPS / 1e4
    while i < n - 1:
        c = cs[i]
        if c.timestamp < start_ms:
            i += 1
            continue
        if c.timestamp + tf_ms > end_ms:
            break
        side = sigs[i]
        if side is None:
            i += 1
            continue
        nxt = cs[i + 1]
        entry = nxt.open * (1 + slip) if side == "long" else nxt.open * (1 - slip)
        dist = SF.EMA_PARAMS["stop_atr"] * atr[i]
        stop = entry - dist if side == "long" else entry + dist
        exit_idx = raw = reason = None
        for j in range(i + 1, n):
            cj = cs[j]
            if side == "long" and cj.low <= stop:
                exit_idx, raw, reason = j, min(stop, cj.open), "stop_loss"
            elif side == "short" and cj.high >= stop:
                exit_idx, raw, reason = j, max(stop, cj.open), "stop_loss"
            else:
                s2 = sigs[j]
                if s2 is not None and s2 != side:
                    exit_idx, raw, reason = j, cj.close, "crossover_exit"
            if exit_idx is not None:
                break
        if exit_idx is None or cs[exit_idx].timestamp + tf_ms > end_ms:
            i += 1
            continue
        out.append(finish(pair, side, nxt.timestamp, nxt.open, dist, stop, exit_idx, raw, reason, cs, events, marks, tf_ms))
        i = max(exit_idx, i + 1)
    return out


def sim_donchian(pair, cs, tf_ms, params, start_ms, end_ms, events):
    p = DN.DonchianParams(entry_period=params[0], exit_period=params[1])
    atr = atr_series([c.high for c in cs], [c.low for c in cs], [c.close for c in cs], p.atr_period)
    idx = {c.timestamp: k for k, c in enumerate(cs)}
    marks = {c.timestamp: c.open for c in cs}
    out, n, cooldown_until = [], len(cs), 0
    slip = P.SLIP_PER_SIDE_BPS / 1e4
    i = DN.min_candles(p) - 1
    while i < n - 1:
        c = cs[i]
        if c.timestamp < start_ms:
            i += 1
            continue
        if c.timestamp + tf_ms > end_ms:
            break
        if c.timestamp + tf_ms < cooldown_until:
            i += 1
            continue
        sig = DN.signal_at(cs, atr, i, p, allow_short=True)
        if sig.side is None:
            i += 1
            continue
        nxt = cs[i + 1]
        entry = nxt.open * (1 + slip) if sig.side == "long" else nxt.open * (1 - slip)
        stop = DN.initial_stop(entry, sig.side, sig.mid, sig.atr, p)
        if not ((sig.side == "long" and stop < entry) or (sig.side == "short" and stop > entry)):
            i += 1
            continue
        s0 = max(0, i + 1 - (p.exit_period + 1))
        ex = None
        for horizon in (SF.EXIT_SLICE, n):
            e0 = min(n, i + 1 + horizon)
            ex = DN.exit_check(side=sig.side, entry=entry, stop=stop, opened_ms=nxt.timestamp, closed=cs[s0:e0], tf_ms=tf_ms, p=p, atr_values=atr[s0:e0])
            if ex is not None or e0 >= n:
                break
        if ex is None or ex.candle_ts + tf_ms > end_ms:
            i += 1
            continue
        e = idx[ex.candle_ts]
        dist = abs(entry - stop)
        if not dist > 0:
            i += 1
            continue
        out.append(finish(pair, sig.side, nxt.timestamp, nxt.open, dist, stop, e, ex.price, ex.reason, cs, events, marks, tf_ms))
        if ex.reason == DN.EXIT_STOP:
            cooldown_until = cs[e].timestamp + tf_ms + SF.DONCHIAN_COOLDOWN_MS
        i = max(e, i + 1)
    return out


def sim_rsi(pair, cs, tf_ms, params, start_ms, end_ms, events):
    p = R.RsiParams()
    closes = [c.close for c in cs]
    r = rsi_series(closes, p.rsi_period)
    a = adx_series([c.high for c in cs], [c.low for c in cs], closes, p.adx_period)
    t_atr = atr_series([c.high for c in cs], [c.low for c in cs], closes, p.atr_period)
    idx = {c.timestamp: k for k, c in enumerate(cs)}
    marks = {c.timestamp: c.open for c in cs}
    out, n = [], len(cs)
    slip = P.SLIP_PER_SIDE_BPS / 1e4
    i = max(R.min_candles(p), 1)
    while i < n - 1:
        c = cs[i]
        if c.timestamp < start_ms:
            i += 1
            continue
        if c.timestamp + tf_ms > end_ms:
            break
        sig = R.signal_from_values(r[i], r[i - 1], a[i], t_atr[i], c.close, p, allow_short=True)
        if sig.side is None:
            i += 1
            continue
        nxt = cs[i + 1]
        entry = nxt.open * (1 + slip) if sig.side == "long" else nxt.open * (1 - slip)
        stop = R.stop_price(entry, sig.atr, sig.side, p)
        horizon = min(n, i + 2 + p.time_stop_candles + 1)
        s0 = max(0, i + 1 - SF.RSI_WARMUP)
        ex = R.exit_check(side=sig.side, entry=entry, stop=stop, opened_ms=nxt.timestamp, closed=cs[s0:horizon], tf_ms=tf_ms, p=p)
        if ex is None or ex.candle_ts + tf_ms > end_ms:
            i += 1
            continue
        e = idx[ex.candle_ts]
        dist = abs(entry - stop)
        if not dist > 0:
            i += 1
            continue
        out.append(finish(pair, sig.side, nxt.timestamp, nxt.open, dist, stop, e, ex.price, ex.reason, cs, events, marks, tf_ms))
        i = max(e, i + 1)
    return out


SIM = {"ema": sim_ema, "donchian": sim_donchian, "rsi": sim_rsi}
PARAMS = {"ema": EMA_SETS, "donchian": DON_SETS, "rsi": {"s1": ()}}


# ================================================================================ evaluation
def _month(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m")


def evaluate(trades: list, windows: tuple, min_trades: int, min_positive: int) -> dict:
    win = {k: [] for k in windows}
    for t in trades:
        k = E.window_of(t.entry_ts)
        if k in win:
            win[k].append(t)
    pooled = [t for k in windows for t in win[k]]
    n = len(pooled)
    per_window = [{"window": k, "trades": len(win[k]), "net_total_r": sum(t.net_r for t in win[k])} for k in windows]
    blocks = {}
    for t in pooled:
        blocks[_month(t.entry_ts)] = blocks.get(_month(t.entry_ts), 0.0) + t.net_r
    total_ci = bootstrap.block_sum_ci(blocks, **BOOT)
    mean_ci = bootstrap.mean_ci([t.net_r for t in pooled], **BOOT)
    mean = (sum(t.net_r for t in pooled) / n) if n else None
    pos = sum(1 for w in per_window if w["trades"] and w["net_total_r"] > 0)
    stops = [t.dist / t.entry * 100.0 for t in pooled]
    cost = [t.gross_r - t.net_r for t in pooled]  # fees, slippage and funding paid, in R
    return {"trades": n, "mean_net_r": mean, "mean_ci": mean_ci, "total_net_r": sum(t.net_r for t in pooled), "total_block_ci": total_ci, "positive_windows": pos, "per_window": per_window,
            "avg_stop_pct": (sum(stops) / n) if n else None, "avg_cost_r": (sum(cost) / n) if n else None, "avg_gross_r": (sum(t.gross_r for t in pooled) / n) if n else None,
            "P1": n >= min_trades, "P2": bool(n and mean > 0 and total_ci is not None and total_ci[1] > 0), "P3": pos >= min_positive}


def dev_pass(e: dict) -> bool:
    return bool(e["P1"] and e["P2"] and e["P3"])


def trades_per_year(trades: list, start_ms: int, end_ms: int) -> dict:
    years = (end_ms - start_ms) / (365.25 * DAY)
    sel = [t for t in trades if start_ms <= t.entry_ts < end_ms]
    per_pair = {p: sum(1 for t in sel if t.pair == p) / years for p in PAIRS}
    return {"pooled": len(sel) / years, "per_pair_avg": len(sel) / years / len(PAIRS), "per_pair": per_pair, "years": years}


# ================================================================================ the run
def _load(root: Path):
    one_h = {pair: sorted(SF._store(Path(root), pair, "1h"), key=lambda c: c.timestamp) for pair in PAIRS}
    return one_h, P.load_funding(root)


def simulate_cells(root: Path, cells=CELLS) -> dict:
    one_h, funding = _load(root)
    htf = {(pair, tf): aggregate(one_h[pair], GROUP[tf]) for pair in PAIRS for tf in GROUP}
    out = {}
    for cell in cells:
        strat, tf, s = cell
        trades = []
        for pair in PAIRS:
            trades += SIM[strat](pair, htf[(pair, tf)], GROUP[tf], PARAMS[strat][s], E.RUN_START, E.RUN_END, funding[pair])
        out[cell] = trades
    return out


def _fmt(x, spec="+.3f"):
    return "n/a" if x is None else format(x, spec)


def _ci(ci, spec="+.1f"):
    return "n/a" if ci is None else f"[{ci[1]:{spec}}, {ci[2]:{spec}}]"


def report(title: str, rows: dict, tpy: dict, note: list[str]) -> str:
    lines = [f"# {title}", "", f"Plan hash `{plan_hash()}`. 4 pairs pooled, own exits, 5 bps taker per side, 11 bps round-trip slippage, historical funding, {int(LEVEL * 100)}% month-block bootstrap intervals.", "",
             "| cell | trades | trades per year (all 4 pairs) | per pair per year | avg stop distance | avg gross R | avg cost R | mean net R | total net R [interval] | windows > 0 | P1 | P2 | P3 | passes |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for cell, e in rows.items():
        t = tpy[cell]
        lines.append(f"| {cell_name(cell)} | {e['trades']} | {t['pooled']:.1f} | {t['per_pair_avg']:.1f} | {_fmt(e['avg_stop_pct'], '.2f')}% | {_fmt(e['avg_gross_r'])} | {_fmt(e['avg_cost_r'], '.3f')} | {_fmt(e['mean_net_r'])} | "
                     f"{_fmt(e['total_net_r'], '+.1f')} {_ci(e['total_block_ci'])} | {e['positive_windows']} | {'yes' if e['P1'] else 'no'} | {'yes' if e['P2'] else 'no'} | {'yes' if e['P3'] else 'no'} | {'YES' if (e['P1'] and e['P2'] and e['P3']) else 'no'} |")
    return "\n".join(lines + [""] + note) + "\n"


class SealedError(Exception):
    pass


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def ledger_path(root: Path) -> Path:
    return Path(root) / "sealed_looks.htf.json"


def read_looks(root: Path, dh: str) -> list:
    p = ledger_path(root)
    if not p.exists():
        return []
    try:
        return list(json.loads(p.read_text()).get(dh, []))
    except (ValueError, AttributeError):
        raise SealedError("the one-look ledger beside the data is unreadable; refusing to open the confirmation windows")


def record_look(root: Path, dh: str, entry: dict) -> int:
    p = ledger_path(root)
    content = json.loads(p.read_text()) if p.exists() else {}
    content.setdefault(dh, []).append(entry)
    p.write_text(json.dumps(content, indent=1))
    return len(content[dh])


def run_dev(root: Path, out_dir: Path, *, expect_plan_hash: str | None = None) -> dict:
    ph = plan_hash()
    if expect_plan_hash and expect_plan_hash != ph:
        raise SealedError(f"plan hash {ph} does not match the registered {expect_plan_hash}")
    dh = data_hash(root)
    sims = simulate_cells(root)
    dev_end = E.TEST_STARTS[CONFIRM_WINDOWS[0]]  # nothing from the confirmation windows is evaluated, counted or printed here
    rows, tpy = {}, {}
    for cell, trades in sims.items():
        dev_trades = [t for t in trades if t.entry_ts < dev_end]
        rows[cell] = evaluate(dev_trades, DEV_WINDOWS, RULE["dev_min_trades"], RULE["dev_min_positive_windows"])
        tpy[cell] = trades_per_year(dev_trades, E.RUN_START, dev_end)
    passing = [cell_name(c) for c, e in rows.items() if dev_pass(e)]
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    note = ["", f"Cells that pass development (P1 at least {RULE['dev_min_trades']} trades, P2 mean above zero with the interval of the total above zero, P3 positive in at least {RULE['dev_min_positive_windows']} of the 6 development windows): {', '.join(passing) if passing else 'NONE'}.",
            "Trades per year are counted over 2020-04 to 2025-03 (before the confirmation windows)."]
    if not passing:
        note.append("Nothing passes development. The confirmation windows stay unread and no look is recorded.")
    (out_dir / "dev_results.json").write_text(json.dumps({"plan_hash": ph, "data_hash": dh, "created": _stamp(), "rows": {cell_name(c): e for c, e in rows.items()}, "tpy": {cell_name(c): t for c, t in tpy.items()}, "passing": passing}, indent=1, default=str))
    (out_dir / "selected.json").write_text(json.dumps({"plan_hash": ph, "data_hash": dh, "passing": passing}, indent=1))
    (out_dir / "dev_report.md").write_text(report("Higher timeframes: development windows (2022-04 to 2025-03)", rows, tpy, note))
    return {"rows": rows, "passing": passing}


def run_confirm(root: Path, out_dir: Path, *, reason: str, confirm: bool, expect_plan_hash: str | None = None, second_look: bool = False) -> dict:
    out_dir = Path(out_dir)
    sel = out_dir / "selected.json"
    if not sel.exists():
        raise SealedError("run the development first: there is no selected.json")
    if not confirm:
        raise SealedError("opening the confirmation windows needs an explicit --confirm")
    if not (reason or "").strip():
        raise SealedError("opening the confirmation windows needs a reason")
    chosen = json.loads(sel.read_text())
    ph, dh = plan_hash(), data_hash(root)
    if expect_plan_hash and expect_plan_hash != ph:
        raise SealedError(f"plan hash {ph} does not match the registered {expect_plan_hash}")
    if chosen["plan_hash"] != ph:
        raise SealedError("plan_hash differs from the development run: a pinned file or the plan changed")
    if chosen["data_hash"] != dh:
        raise SealedError("data_hash differs from the development run: the candle or funding files changed")
    if not chosen["passing"]:
        (out_dir / "final_report.md").write_text("# Higher timeframes: confirmation windows\n\nNo cell passed development, so the confirmation windows were NOT read (no look was recorded).\n\nAnswer: no cell on 4h or 1d passes the pre-registered rule.\n")
        return {"opened": False, "rows": {}}
    looks = read_looks(root, dh)
    contaminated = bool(looks)
    if looks and not second_look:
        raise SealedError("the confirmation windows were already read once; a second look needs --second-look and is labelled contaminated")
    record_look(root, dh, {"ts": _stamp(), "reason": reason.strip(), "plan_hash": ph, "second_look": contaminated})  # BEFORE anything is read
    cells = [c for c in CELLS if cell_name(c) in chosen["passing"]]
    sims = simulate_cells(root, cells)
    rows = {c: evaluate(sims[c], CONFIRM_WINDOWS, RULE["confirm_min_trades"], RULE["confirm_min_positive_windows"]) for c in cells}
    tpy = {c: trades_per_year(sims[c], E.TEST_STARTS[CONFIRM_WINDOWS[0]], E.TEST_STARTS[-1] + E.TEST_LEN) for c in cells}
    (out_dir / "confirm_results.json").write_text(json.dumps({"plan_hash": ph, "data_hash": dh, "contaminated": contaminated, "rows": {cell_name(c): e for c, e in rows.items()}}, indent=1, default=str))
    note = ["", "Confirmation rule: at least 15 trades, mean net R above zero, total above zero, positive in at least 2 of the 3 windows."] + (["THIS IS A SECOND LOOK: it is not unseen evidence."] if contaminated else [])
    (out_dir / "final_report.md").write_text(report("Higher timeframes: confirmation windows (read once)", rows, tpy, note))
    return {"opened": True, "rows": rows, "contaminated": contaminated}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mba_bot.htf_study")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan")
    for name in ("dev", "confirm"):
        sp = sub.add_parser(name)
        sp.add_argument("--root", required=True, type=Path)
        sp.add_argument("--out", required=True, type=Path)
        sp.add_argument("--expect-plan-hash")
        if name == "confirm":
            sp.add_argument("--confirm", action="store_true")
            sp.add_argument("--reason", default="")
            sp.add_argument("--second-look", action="store_true")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "plan":
            print(plan_hash())
            return 0
        if args.cmd == "dev":
            res = run_dev(args.root, args.out, expect_plan_hash=args.expect_plan_hash)
            print(f"development done; passing cells: {res['passing'] or 'none'}; report in {args.out}/dev_report.md")
            return 0
        res = run_confirm(args.root, args.out, reason=args.reason, confirm=args.confirm, expect_plan_hash=args.expect_plan_hash, second_look=args.second_look)
        print("confirm done; " + ("windows read" if res["opened"] else "windows NOT read (nothing passed development)"))
        return 0
    except SealedError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"htf study failed ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
