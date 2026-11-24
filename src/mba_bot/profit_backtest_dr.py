"""The sealed test of the profit rules for Donchian and RSI (docs/research/profit_rules_dr_preregistration.md). Research only: no network, no key, no order, no live effect.

The same question PR #96 asked of the EMA strategy, asked of donchian_breakout_v1 and rsi_mr_v1 on 1h candles, BTC, ETH, BNB, XRP, after real costs and historical funding, over a much longer history, with bootstrap confidence
intervals. Rule sets: RS0 (the strategy as it runs: its own stop, trail, channel exit / target, RSI exit, time stop), LOCK (the +1R lock: stop to entry plus the cost buffer once the best price is 1 R beyond the entry), and
RS84X (PR #84's take profit, break-even and trail adapted to each strategy, below). The rules are an OVERLAY: the strategy's own exit is computed exactly as the pre-registered simulations compute it, and a rule stop that is
touched earlier (or at the same candle at a better price) ends the trade first. Conventions are PR #96's: signal on a closed candle, entry at the next open, a stop is judged before anything else in a candle, a candle that opens
beyond a level fills at its open, a level earned by a candle applies from the NEXT candle, a price through an earned level at a candle close leaves at that close, unfinished trades at the window end are not counted.

RS84X: take profit 3 R (net of costs; Donchian only: RSI keeps its own profit target), break-even stop at the entry at +1 R, trail 2 ATR from +1.5 R. R is the distance from the (slipped) entry to the strategy's initial stop.
NOT modelled: the 5-minute rounds (these runs act at candle closes), the exchange-call limits, real fills of stop orders, the account's real fee tier. A result is a MEASUREMENT on two windows, not evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import donchian as DN
from . import ema_exits, profit_backtest as P, profit_lock, reporting
from . import rsi_mr as R
from . import shortframe_backtest as SF
from .indicators import adx as adx_series
from .indicators import atr as atr_series
from .indicators import rsi as rsi_series
from .research import bootstrap
from .strategy import Candle

PROTOCOL = "profit rules for Donchian and RSI sealed test v1: 1h, 4 pairs, 2020-04 to 2025-10, bootstrap intervals"
PAIRS = P.PAIRS
TF = "1h"
TF_MS = SF.TF_MS[TF]
DAY_MS = SF.DAY_MS
STRATEGIES = ("donchian", "rsi")


def _ms(text: str) -> int:
    return SF._ms(text)


TUNE = (_ms("2020-04-01"), _ms("2025-01-01"))
CONFIRM = (_ms("2025-01-04"), _ms("2025-11-01"))  # 3-day embargo; no earlier study read any of it (they used 2025-11 onward)
BOOT = {"reps": 2000, "level": 0.95, "seed": 20260101}


@dataclass(frozen=True)
class Rules:
    name: str
    lock_r: float = 0.0
    r84: ema_exits.ExitRules | None = None


RS0 = Rules("RS0 current rules")
LOCK = Rules("LOCK +1R profit lock", lock_r=1.0)
RS84X_DON = Rules("RS84X PR #84 style (TP 3R, break-even 1R, trail 2 ATR from 1.5R)", r84=ema_exits.ExitRules(take_profit_r=3.0, trail_trigger_r=1.5, trail_atr=2.0, breakeven_r=1.0))
RS84X_RSI = Rules("RS84X PR #84 style (own target kept, break-even 1R, trail 2 ATR from 1.5R)", r84=ema_exits.ExitRules(take_profit_r=0.0, trail_trigger_r=1.5, trail_atr=2.0, breakeven_r=1.0))
RULE_SETS = {"donchian": (RS0, LOCK, RS84X_DON), "rsi": (RS0, LOCK, RS84X_RSI)}
STOP_TYPE = {"donchian": (DN.EXIT_STOP, DN.EXIT_TRAIL), "rsi": (R.EXIT_STOP,)}


# ================================================================================ plan
_PINNED = ("profit_backtest_dr.py", "profit_backtest.py", "profit_lock.py", "profit_protect.py", "ema_exits.py", "reporting.py", "shortframe_backtest.py", "donchian.py", "rsi_mr.py", "indicators.py", "strategy.py",
           "research/bootstrap.py", "research/archive.py", "research/funding_archive.py")


def source_pins() -> dict:
    here = Path(__file__).parent
    return {n: hashlib.sha256((here / n).read_bytes()).hexdigest() for n in _PINNED}


def plan_blob() -> dict:
    return {"protocol": PROTOCOL, "pairs": PAIRS, "timeframe": TF, "tune": TUNE, "confirm": CONFIRM, "fee_bps_per_side": P.FEE_BPS, "slippage_round_trip_bps": P.SLIP_ROUND_TRIP_BPS, "bootstrap": BOOT,
            "rule_sets": {k: [(r.name, r.lock_r, None if r.r84 is None else vars(r.r84)) for r in v] for k, v in RULE_SETS.items()}, "donchian": vars(DN.DonchianParams()), "rsi": vars(R.RsiParams()),
            "donchian_cooldown_ms": SF.DONCHIAN_COOLDOWN_MS, "rsi_warmup": SF.RSI_WARMUP, "exit_slice": SF.EXIT_SLICE, "sources_sha256": source_pins(),
            "rules": "overlay on the strategy's own exit; stop first; gap fills at the open; level earned applies next candle; funding at historical settlements; unfinished trades not counted; confirm read once"}


def plan_hash() -> str:
    return hashlib.sha256(json.dumps(plan_blob(), sort_keys=True, default=str).encode()).hexdigest()[:16]


def data_hash(root: Path) -> str:
    h = hashlib.sha256()
    for sym in PAIRS:
        for sub in ("klines/1h", "funding"):
            p = Path(root) / sub / f"{sym}.csv"
            if not p.exists():
                raise FileNotFoundError(f"missing {sub}/{sym}.csv under {root}")
            h.update(f"{sub}{sym}".encode() + hashlib.sha256(p.read_bytes()).hexdigest().encode())
    return h.hexdigest()[:16]


# ================================================================================ the overlay
def overlay(*, side: str, entry: float, stop0: float, cs: list[Candle], atr: list, j0: int, e: int, base_price: float, base_reason: str, base_is_stop: bool, rules: Rules, buffer: float):
    """Walk the candles j0..e (entry candle to the strategy's own exit candle) with the profit rules. Returns (exit index, raw exit price, reason, best price seen)."""
    long_side = side == "long"
    rs_stop, rs_label = None, None
    peak = entry
    tp = ema_exits.take_profit_level(side, entry, stop0, rules.r84.take_profit_r, buffer) if rules.r84 and rules.r84.take_profit_r > 0 else None
    seen = entry
    for j in range(j0, e + 1):
        c = cs[j]
        seen = max(seen, c.high) if long_side else min(seen, c.low)
        # 1. a rule stop that stood before this candle
        if rs_stop is not None and ((long_side and c.low <= rs_stop) or ((not long_side) and c.high >= rs_stop)):
            px = min(rs_stop, c.open) if long_side else max(rs_stop, c.open)
            if j == e and base_is_stop and ((long_side and base_price >= px) or ((not long_side) and base_price <= px)):
                return e, base_price, base_reason, seen  # the strategy's own stop is the higher one: it is touched first
            return j, px, rs_label, seen
        # 2. the strategy's own stop-type exit
        if j == e and base_is_stop:
            return e, base_price, base_reason, seen
        # 3. take profit
        if tp is not None and ((long_side and c.high >= tp) or ((not long_side) and c.low <= tp)):
            return j, (max(tp, c.open) if long_side else min(tp, c.open)), "take_profit", seen
        # 4. the strategy's own close- or target-type exit
        if j == e:
            return e, base_price, base_reason, seen
        # 5. the levels earned by this candle apply from the next one
        peak = max(peak, c.high) if long_side else min(peak, c.low)
        cand = []
        if rules.r84 is not None and (rules.r84.trail_trigger_r > 0 or rules.r84.breakeven_r > 0):
            lv = ema_exits.running_level(side, entry, stop0, peak, rules.r84, atr[j])
            if lv is not None:
                cand.append(lv)
        if rules.lock_r > 0:
            lv = profit_lock.lock_level(side=side, entry=entry, initial_stop=stop0, peak=peak, lock_r=rules.lock_r, buffer=buffer)
            if lv is not None:
                cand.append((lv, "profit_lock"))
        if cand:
            level, label = max(cand, key=lambda x: x[0]) if long_side else min(cand, key=lambda x: x[0])
            if rs_stop is None or (level > rs_stop if long_side else level < rs_stop):
                if (long_side and c.close <= level) or ((not long_side) and c.close >= level):
                    return j, c.close, label, seen
                rs_stop, rs_label = level, label
    return e, base_price, base_reason, seen


def _finish(pair, side, nxt_ts, raw_entry, dist, stop0, exit_idx, raw_exit, reason, peak, cs, events, marks):
    legs = [P.Leg(1.0, raw_exit, cs[exit_idx].timestamp, reason)]
    entry, gross, net, extra = P._cost(side, raw_entry, dist, legs, 0.0)
    fr = P._funding_r(side, dist, [(t, r) for t, r in events if nxt_ts < t <= cs[exit_idx].timestamp + TF_MS], marks, cs, nxt_ts, [(nxt_ts - 1, 1.0)])
    return P.SimTrade(pair, side, nxt_ts, cs[exit_idx].timestamp, raw_entry, entry, dist, stop0, peak, legs, reason, gross, net + fr, fr, extra, False, None, max(cs[exit_idx].timestamp + TF_MS - nxt_ts, TF_MS))


def simulate_donchian(pair: str, cs: list[Candle], start_ms: int, end_ms: int, rules: Rules, events: list) -> tuple[list, int]:
    p = DN.DonchianParams()
    atr = atr_series([c.high for c in cs], [c.low for c in cs], [c.close for c in cs], p.atr_period)
    idx = {c.timestamp: k for k, c in enumerate(cs)}
    marks = {c.timestamp: c.open for c in cs}
    out, unfinished, cooldown_until = [], 0, 0
    i, n = DN.min_candles(p) - 1, len(cs)
    slip = P.SLIP_PER_SIDE_BPS / 1e4
    while i < n - 1:
        c = cs[i]
        if c.timestamp < start_ms:
            i += 1
            continue
        if c.timestamp + TF_MS > end_ms:
            break
        if c.timestamp + TF_MS < cooldown_until:
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
            ex = DN.exit_check(side=sig.side, entry=entry, stop=stop, opened_ms=nxt.timestamp, closed=cs[s0:e0], tf_ms=TF_MS, p=p, atr_values=atr[s0:e0])
            if ex is not None or e0 >= n:
                break
        if ex is None or ex.candle_ts + TF_MS > end_ms:
            unfinished += 1
            i += 1
            continue
        e = idx[ex.candle_ts]
        dist = abs(entry - stop)
        if not dist > 0:
            i += 1
            continue
        k, px, reason, peak = overlay(side=sig.side, entry=entry, stop0=stop, cs=cs, atr=atr, j0=i + 1, e=e, base_price=ex.price, base_reason=ex.reason, base_is_stop=ex.reason in STOP_TYPE["donchian"], rules=rules,
                                      buffer=P._buffer(nxt.open))
        out.append(_finish(pair, sig.side, nxt.timestamp, nxt.open, dist, stop, k, px, reason, peak, cs, events, marks))
        if reason == DN.EXIT_STOP:
            cooldown_until = cs[k].timestamp + TF_MS + SF.DONCHIAN_COOLDOWN_MS
        j = i + 1
        while j < n and cs[j].timestamp < cs[k].timestamp:
            j += 1
        i = max(j, i + 1)
    return out, unfinished


def simulate_rsi(pair: str, cs: list[Candle], start_ms: int, end_ms: int, rules: Rules, events: list) -> tuple[list, int]:
    p = R.RsiParams()
    closes = [c.close for c in cs]
    r = rsi_series(closes, p.rsi_period)
    a = adx_series([c.high for c in cs], [c.low for c in cs], closes, p.adx_period)
    t_atr = atr_series([c.high for c in cs], [c.low for c in cs], closes, p.atr_period)
    idx = {c.timestamp: k for k, c in enumerate(cs)}
    marks = {c.timestamp: c.open for c in cs}
    out, unfinished = [], 0
    i, n = max(R.min_candles(p), 1), len(cs)
    slip = P.SLIP_PER_SIDE_BPS / 1e4
    while i < n - 1:
        c = cs[i]
        if c.timestamp < start_ms:
            i += 1
            continue
        if c.timestamp + TF_MS > end_ms:
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
        ex = R.exit_check(side=sig.side, entry=entry, stop=stop, opened_ms=nxt.timestamp, closed=cs[s0:horizon], tf_ms=TF_MS, p=p)
        if ex is None or ex.candle_ts + TF_MS > end_ms:
            unfinished += 1
            i += 1
            continue
        e = idx[ex.candle_ts]
        dist = abs(entry - stop)
        if not dist > 0:
            i += 1
            continue
        k, px, reason, peak = overlay(side=sig.side, entry=entry, stop0=stop, cs=cs, atr=t_atr, j0=i + 1, e=e, base_price=ex.price, base_reason=ex.reason, base_is_stop=ex.reason in STOP_TYPE["rsi"], rules=rules,
                                      buffer=P._buffer(nxt.open))
        out.append(_finish(pair, sig.side, nxt.timestamp, nxt.open, dist, stop, k, px, reason, peak, cs, events, marks))
        j = i + 1
        while j < n and cs[j].timestamp < cs[k].timestamp:
            j += 1
        i = max(j, i + 1)
    return out, unfinished


SIM = {"donchian": simulate_donchian, "rsi": simulate_rsi}


# ================================================================================ statistics
def _month(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m")


def stats(trades: list, base_by_key: dict | None) -> dict:
    m = P.metrics(trades, base_by_key)
    nets = [t.net_r for t in trades]
    ci = bootstrap.mean_ci(nets, **BOOT)
    blocks: dict = {}
    for t in trades:
        blocks[_month(t.entry_ts)] = blocks.get(_month(t.entry_ts), 0.0) + t.net_r
    bci = bootstrap.block_sum_ci(blocks, **BOOT)
    m["mean_ci"] = ci
    m["total_block_ci"] = bci
    m["months_with_trades"] = len(blocks)
    return m


def delta_stats(trades: list, base: list) -> dict:
    """The difference against RS0 per ENTRY (pair, entry time): the rule's net R minus the baseline's, with a missing trade counting as zero on its side. Intervals: iid over entries (narrow) and a block bootstrap by month (wider)."""
    a = {(t.pair, t.entry_ts): t for t in trades}
    b = {(t.pair, t.entry_ts): t for t in base}
    keys = sorted(set(a) | set(b))
    deltas, months = [], {}
    for k in keys:
        d = (a[k].net_r if k in a else 0.0) - (b[k].net_r if k in b else 0.0)
        deltas.append(d)
        months[_month(k[1])] = months.get(_month(k[1]), 0.0) + d
    ci = bootstrap.mean_ci(deltas, **BOOT)
    bci = bootstrap.block_sum_ci(months, **BOOT)
    return {"entries": len(keys), "total": sum(deltas), "mean_ci": ci, "total_block_ci": bci, "distinguishable": bootstrap.excludes_zero(ci) and bootstrap.excludes_zero(bci)}


def run_window(candles: dict, funding: dict, window: tuple[int, int]) -> dict:
    out: dict = {}
    for strat in STRATEGIES:
        per: dict = {}
        for rs in RULE_SETS[strat]:
            trades = []
            for pair in PAIRS:
                ts, _ = SIM[strat](pair, candles[pair], window[0], window[1], rs, funding[pair])
                trades += ts
            per[rs.name] = trades
        base = per[RS0.name]
        base_by_key = {(t.pair, t.entry_ts): t for t in base}
        res = {}
        for name, trades in per.items():
            s = stats(trades, base_by_key)
            s["delta_vs_rs0"] = None if name == RS0.name else delta_stats(trades, base)
            res[name] = s
        out[strat] = res
    return out


# ================================================================================ report
def _d(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _f(x, spec="+.3f", none="n/a") -> str:
    return none if x is None else format(x, spec)


def _ci(ci, spec="+.3f") -> str:
    return "n/a" if ci is None else f"[{ci[1]:{spec}}, {ci[2]:{spec}}]"


def report(title: str, window: tuple[int, int], rows: dict, extra: list[str] | None = None) -> str:
    months = (window[1] - window[0]) / DAY_MS / 30.44
    lines = [f"# {title}", "", f"Window {_d(window[0])} to {_d(window[1])} (UTC, end exclusive), {months:.0f} months. Plan hash `{plan_hash()}`. 1h candles, BTC, ETH, BNB, XRP pooled. Costs: {P.FEE_BPS:g} bps taker per side, "
             f"{P.SLIP_ROUND_TRIP_BPS:g} bps round-trip slippage, historical funding. R = distance from the entry to the strategy's initial stop. Intervals are 95% bootstrap intervals; the first (trades) ignores that "
             "neighbouring trades share market moves and is too narrow, the second resamples whole calendar months and is the one to read.", ""]
    for strat, res in rows.items():
        lines += [f"## {strat}", "", "| rule set | trades | avg net R | CI (trades) | total net R | CI (months) | profit factor | max DD % | worst losing streak | give-back rate | winners cut short | vs RS0: total R | CI (trades, mean per entry) | CI (months, total) | distinguishable from noise |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for name, m in res.items():
            d = m["delta_vs_rs0"]
            gb = "n/a" if m["give_back_rate"] is None else f"{m['give_back_rate'] * 100:.0f}% ({m['gave_back']} of {m['reached_1r']})"
            lines.append(f"| {name} | {m['trades']} | {_f(m['avg_net_r'])} | {_ci(m['mean_ci'])} | {_f(m['total_net_r'], '+.1f')} | {_ci(m['total_block_ci'], '+.1f')} | {_f(m['profit_factor'], '.2f')} | {m['max_dd_pct']:.1f} | "
                         f"{m['worst_losing_streak']} | {gb} | {m['winners_cut_short']} | {'-' if d is None else _f(d['total'], '+.1f')} | {'-' if d is None else _ci(d['mean_ci'], '+.4f')} | "
                         f"{'-' if d is None else _ci(d['total_block_ci'], '+.1f')} | {'-' if d is None else ('YES' if d['distinguishable'] else 'no')} |")
        lines.append("")
    lines += extra or []
    return "\n".join(lines) + "\n"


# ================================================================================ tune, confirm, ledger
class SealedError(Exception):
    pass


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def ledger_path(root: Path) -> Path:
    return Path(root) / "sealed_looks.profit_dr.json"


def read_looks(root: Path, dh: str) -> list:
    p = ledger_path(root)
    if not p.exists():
        return []
    try:
        return list(json.loads(p.read_text()).get(dh, []))
    except (ValueError, AttributeError):
        raise SealedError("the one-look ledger beside the data is unreadable; refusing to open the confirmation window")


def record_look(root: Path, dh: str, entry: dict) -> int:
    p = ledger_path(root)
    content = json.loads(p.read_text()) if p.exists() else {}
    content.setdefault(dh, []).append(entry)
    p.write_text(json.dumps(content, indent=1))
    return len(content[dh])


def _load(root: Path):
    candles = {pair: sorted(SF._store(Path(root), pair, TF), key=lambda c: c.timestamp) for pair in PAIRS}
    funding = P.load_funding(root)
    return candles, funding


def _json(obj):
    return json.loads(json.dumps(obj, default=lambda o: list(o) if isinstance(o, tuple) else str(o)))


def run_tune(root: Path, out_dir: Path, *, expect_plan_hash: str | None = None) -> dict:
    ph = plan_hash()
    if expect_plan_hash and expect_plan_hash != ph:
        raise SealedError(f"plan hash {ph} does not match the registered {expect_plan_hash}: a pinned file or the plan changed")
    dh = data_hash(root)
    candles, funding = _load(root)
    rows = run_window(candles, funding, TUNE)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "tune_results.json").write_text(json.dumps({"plan_hash": ph, "data_hash": dh, "created": _stamp(), "rows": _json(rows)}, indent=1))
    (out_dir / "selected.json").write_text(json.dumps({"plan_hash": ph, "data_hash": dh, "confirm_window": CONFIRM}, indent=1))
    (out_dir / "tune_report.md").write_text(report("Profit rules for Donchian and RSI: in-sample window", TUNE, rows))
    return {"plan_hash": ph, "data_hash": dh, "rows": rows}


def run_confirm(root: Path, out_dir: Path, *, reason: str, confirm: bool, expect_plan_hash: str | None = None, second_look: bool = False) -> dict:
    out_dir = Path(out_dir)
    sel = out_dir / "selected.json"
    if not sel.exists():
        raise SealedError("run the tuning first: there is no selected.json")
    if not confirm:
        raise SealedError("opening the confirmation window needs an explicit --confirm")
    if not (reason or "").strip():
        raise SealedError("opening the confirmation window needs a reason")
    chosen = json.loads(sel.read_text())
    ph, dh = plan_hash(), data_hash(root)
    if expect_plan_hash and expect_plan_hash != ph:
        raise SealedError(f"plan hash {ph} does not match the registered {expect_plan_hash}")
    if chosen["plan_hash"] != ph:
        raise SealedError("plan_hash differs from the tuning: a pinned file or the plan changed after tuning")
    if chosen["data_hash"] != dh:
        raise SealedError("data_hash differs from the tuning: the candle or funding files changed")
    looks = read_looks(root, dh)
    contaminated = bool(looks)
    if looks and not second_look:
        raise SealedError("the confirmation window was already read once; a second look needs --second-look and is labelled contaminated")
    record_look(root, dh, {"ts": _stamp(), "reason": reason.strip(), "plan_hash": ph, "second_look": contaminated})  # BEFORE anything is read
    candles, funding = _load(root)
    rows = run_window(candles, funding, CONFIRM)
    (out_dir / "confirm_results.json").write_text(json.dumps({"plan_hash": ph, "data_hash": dh, "created": _stamp(), "contaminated": contaminated, "rows": _json(rows)}, indent=1))
    extra = ["THIS IS A SECOND LOOK: it is not unseen evidence."] if contaminated else []
    (out_dir / "final_report.md").write_text(report("Profit rules for Donchian and RSI: confirmation window (read once)", CONFIRM, rows, extra))
    return {"plan_hash": ph, "data_hash": dh, "opened": True, "contaminated": contaminated, "rows": rows}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mba_bot.profit_backtest_dr")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan")
    for name in ("tune", "confirm"):
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
        if args.cmd == "tune":
            run_tune(args.root, args.out, expect_plan_hash=args.expect_plan_hash)
            print(f"tune done; report in {args.out}/tune_report.md")
            return 0
        run_confirm(args.root, args.out, reason=args.reason, confirm=args.confirm, expect_plan_hash=args.expect_plan_hash, second_look=args.second_look)
        print(f"confirm done; window read; report in {args.out}/final_report.md")
        return 0
    except SealedError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - class name only
        print(f"profit backtest failed ({type(exc).__name__})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
