"""The sealed test of profit protection v2 (docs/research/profit_rules_preregistration.md). Research only: no network, no key, no order, no live effect.

Question: on the same candles, what do the profit rules change for the EMA strategy (ema_cross_atr_v1, 2 ATR initial stop, exit on the opposite crossover) after real costs? Rule sets compared, per timeframe (1h and
15m), per pair (BTC, ETH, BNB, XRP), on a tuning window and ONE untouched confirmation window:
  RS0   the current rules (nothing added);
  RS84  PR #84's rules (take profit 3 R, break-even at 1 R, trail from 1.5 R by 2 ATR);
  each option of the staged lock (activation level, tiered trail, stage 1 placement, the time condition), the time stop, the two reversal exits, partial profit and re-entry, ALONE (on top of RS0);
  BEST  the combination chosen from the tuning window by a rule fixed in the plan (`choose_best`).
Costs: taker fee per side plus the fee guard's round-trip slippage (both on every order leg), funding at the historical rate of every settlement the position was open for (public archive, no key), plus a small fixed
surcharge for every ORDER beyond the entry and one exit (a partial exit). A re-entry is a full extra round trip with all of it.

Simulation conventions (as in shortframe_backtest and the other pre-registered backtests): signal on a closed candle, entry at the next candle's open, the stop is judged first inside a candle (it wins ties), a candle
that opens beyond a level fills at its open, a stop level EARNED by a candle applies from the NEXT candle (the order of events inside a candle is unknown), unfinished trades at the window end are not counted.
What is NOT modelled, and why it matters: the live bot looks every 5 minutes and so can act inside a candle (these runs act at candle closes, a conservative reading of rules that run every round); Part B's call
limits (minimum improvement and interval per stop move); exchange stop-order fills differ from the assumed slippage; the account's real fee tier (the standard 5 bps is assumed). Donchian and RSI are NOT simulated.
A result here is a MEASUREMENT on two windows, not evidence that a rule will make money."""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import sys
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from . import ema_exits, ema_reversal, profit_protect, reentry, reporting
from . import shortframe_backtest as SF
from .indicators import atr as atr_series
from .indicators import ema as ema_series
from .research import funding_archive
from .strategy import Candle

PROTOCOL = "profit protection v2 sealed test v1: EMA on 1h and 15m, rule sets after costs and funding"
PAIRS = SF.PAIRS
TIMEFRAMES = ("1h", "15m")
TF_MS = SF.TF_MS
FEE_BPS = SF.FEE_BPS_PER_SIDE
SLIP_PER_SIDE_BPS = SF.SLIP_BPS_PER_SIDE
SLIP_ROUND_TRIP_BPS = SF.SLIPPAGE_ROUND_TRIP_BPS
EXTRA_ORDER_BPS = 2.0  # every order beyond the entry and one exit (a partial exit) costs this much more, as a share of its notional (rounding, a second order's latency); an assumption
TUNE, CONFIRM = SF.TUNE, SF.CONFIRM
STOP_ATR = SF.EMA_PARAMS["stop_atr"]
DAY_MS = SF.DAY_MS
RISK_PCT = SF.RISK_PCT
PROTECTIVE = reentry.PROTECTIVE_REASONS


# ================================================================================ rule sets
@dataclass(frozen=True)
class RuleSet:
    name: str
    family: str = ""  # options in one family are alternatives (the combination takes at most one per family)
    rules84: ema_exits.ExitRules = ema_exits.ExitRules()
    lock: profit_protect.LockRules = profit_protect.LockRules()
    max_hold_candles: int = 0
    tighten_atr: float = 1.0
    reversal: ema_reversal.ReversalRules = ema_reversal.ReversalRules()
    partial_r: float = 0.0
    partial_fraction: float = 0.0
    reentry: reentry.ReentryRules = reentry.ReentryRules()


def _lock(**kw) -> profit_protect.LockRules:
    return profit_protect.LockRules(**kw)


TIERS = ((0.0, 3.0), (2.0, 2.0), (4.0, 1.5))
RS0 = RuleSet("RS0 current rules")
RS84 = RuleSet("RS84 PR #84 rules", rules84=ema_exits.ExitRules(take_profit_r=3.0, trail_trigger_r=1.5, trail_atr=2.0, breakeven_r=1.0))
OPTIONS = (
    RuleSet("A1 stage 1 at +1R", "activation", lock=_lock(stage1_r=1.0)),
    RuleSet("A2 stage 1 at +1.5R", "activation", lock=_lock(stage1_r=1.5)),
    RuleSet("A3 stage 1 at +2R", "activation", lock=_lock(stage1_r=2.0)),
    RuleSet("B1 staged lock (+1R cost buffer, +2R lock 1R, then 2 ATR trail)", "activation", lock=_lock(stage1_r=1.0, stage2_r=2.0, stage2_lock_r=1.0, trail_start_r=2.0, trail_atr=2.0)),
    RuleSet("P1 stage 1 beyond noise (+1R, best price minus 2 ATR)", "activation", lock=_lock(stage1_r=1.0, stage1_atr=2.0)),
    RuleSet("T1 tiered trail (from +1R: 3 ATR, 2 ATR from +2R, 1.5 ATR from +4R)", "trail", lock=_lock(trail_start_r=1.0, trail_tiers=TIERS)),
    RuleSet("M1 trail 3 ATR from +1R, 1.5 ATR after 24 candles", "trail", lock=_lock(trail_start_r=1.0, trail_atr=3.0, tighten_after_candles=24, tighten_trail_atr=1.5)),
    RuleSet("D1 time stop at 72 candles", "time", max_hold_candles=72, tighten_atr=1.0),
    RuleSet("E1 exit on a close back across the fast EMA (after +0.5R)", "reversal_fast", reversal=ema_reversal.ReversalRules(fast_cross=True, min_profit_r=0.5)),
    RuleSet("E2 exit when the fast/slow gap has shrunk by 50%", "reversal_gap", reversal=ema_reversal.ReversalRules(gap_fraction=0.5)),
    RuleSet("I1 partial profit (half at +1.5R, rest to entry plus costs, trail 2 ATR from +2R)", "partial", lock=_lock(trail_start_r=2.0, trail_atr=2.0), partial_r=1.5, partial_fraction=0.5),
    RuleSet("J1 stage 1 at +1R and one re-entry after a protective exit", "reentry", lock=_lock(stage1_r=1.0), reentry=reentry.ReentryRules(enabled=True)),
)
ALL_SETS = (RS0, RS84) + OPTIONS


def choose_best(tune_rows: dict) -> RuleSet:
    """The pre-registered combination rule, applied to the TUNING numbers only. An option qualifies if, pooled over the four pairs on the tuning window, its net total R is above RS0's AND its give-back rate is not
    higher than RS0's. From each family the qualifying option with the highest net total R is taken (an option of the 'activation' family and one of the 'trail' family are combined: the activation option's stage
    levels with the trail family's trail). The combination is the union of the chosen options; with none qualifying it is RS0 itself."""
    base = tune_rows[RS0.name]
    chosen: dict[str, RuleSet] = {}
    for opt in OPTIONS:
        row = tune_rows[opt.name]
        if row["total_net_r"] is None or base["total_net_r"] is None:
            continue
        gb, gb0 = row["give_back_rate"], base["give_back_rate"]
        if row["total_net_r"] > base["total_net_r"] and (gb is None or gb0 is None or gb <= gb0):
            cur = chosen.get(opt.family)
            if cur is None or row["total_net_r"] > tune_rows[cur.name]["total_net_r"]:
                chosen[opt.family] = opt
    if not chosen:
        return replace(RS0, name="BEST (no option qualified: equals RS0)")
    lock = profit_protect.LockRules()
    out = RuleSet("BEST (" + " + ".join(o.name.split(" ")[0] for o in chosen.values()) + ")")
    for opt in chosen.values():
        if opt.family == "activation":
            lock = replace(lock, stage1_r=opt.lock.stage1_r, stage1_atr=opt.lock.stage1_atr, stage2_r=opt.lock.stage2_r, stage2_lock_r=opt.lock.stage2_lock_r, trail_start_r=opt.lock.trail_start_r or lock.trail_start_r, trail_atr=opt.lock.trail_atr if opt.lock.trail_start_r else lock.trail_atr)
        elif opt.family == "trail":
            lock = replace(lock, trail_start_r=opt.lock.trail_start_r, trail_atr=opt.lock.trail_atr, trail_tiers=opt.lock.trail_tiers, tighten_after_candles=opt.lock.tighten_after_candles, tighten_trail_atr=opt.lock.tighten_trail_atr)
        elif opt.family == "time":
            out = replace(out, max_hold_candles=opt.max_hold_candles, tighten_atr=opt.tighten_atr)
        elif opt.family == "reversal_fast":
            out = replace(out, reversal=replace(out.reversal, fast_cross=True, min_profit_r=opt.reversal.min_profit_r))
        elif opt.family == "reversal_gap":
            out = replace(out, reversal=replace(out.reversal, gap_fraction=opt.reversal.gap_fraction))
        elif opt.family == "partial":
            out = replace(out, partial_r=opt.partial_r, partial_fraction=opt.partial_fraction)
            lock = replace(lock, trail_start_r=lock.trail_start_r or opt.lock.trail_start_r, trail_atr=lock.trail_atr if lock.trail_start_r else opt.lock.trail_atr)
        elif opt.family == "reentry":
            out = replace(out, reentry=opt.reentry)
            lock = replace(lock, stage1_r=lock.stage1_r or opt.lock.stage1_r)
    return replace(out, lock=lock)


# ================================================================================ plan
_PINNED = ("profit_backtest.py", "profit_protect.py", "ema_exits.py", "ema_reversal.py", "reentry.py", "reporting.py", "shortframe_backtest.py", "strategy.py", "indicators.py", "research/funding_archive.py", "research/archive.py")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_pins() -> dict:
    here = Path(__file__).parent
    return {name: _sha(here / name) for name in _PINNED}


def _jsonable(obj):
    return asdict(obj) if hasattr(obj, "__dataclass_fields__") else obj


def plan_blob() -> dict:
    return {
        "protocol": PROTOCOL, "pairs": PAIRS, "timeframes": TIMEFRAMES, "fee_bps_per_side": FEE_BPS, "slippage_round_trip_bps": SLIP_ROUND_TRIP_BPS, "extra_order_bps": EXTRA_ORDER_BPS, "risk_pct": RISK_PCT,
        "tune": TUNE, "confirm": CONFIRM, "stop_atr": STOP_ATR, "ema": SF.EMA_PARAMS, "assumed_fee_bps_for_buffer": reporting.ASSUMED_FEE_BPS, "giveback_report_r": reporting.GIVEBACK_REPORT_R,
        "rule_sets": [_jsonable(s) for s in ALL_SETS],
        "combination_rule": choose_best.__doc__, "winner_cut_short": "a rule trade with net R above zero whose same entry in RS0 ended later with a higher net R",
        "recovered_reentry": "a re-entry whose net R is above zero",
        "sources_sha256": source_pins(),
        "rules": "closed-candle signal, next-open entry, stop first, gap fills at open, level earned applies next candle, funding at historical settlements, unfinished trades not counted, confirm read once",
    }


def plan_hash() -> str:
    return hashlib.sha256(json.dumps(plan_blob(), sort_keys=True, default=str).encode()).hexdigest()[:16]


# ================================================================================ data
def load_candles(root: Path) -> dict[tuple[str, str], list[Candle]]:
    return {(sym, tf): sorted(SF._store(Path(root), sym, tf), key=lambda c: c.timestamp) for tf in TIMEFRAMES for sym in PAIRS}


def load_funding(root: Path) -> dict[str, list[tuple[int, float]]]:
    return {sym: funding_archive.load(Path(root), sym) for sym in PAIRS}


def data_hash(root: Path) -> str:
    h = hashlib.sha256()
    for tf in TIMEFRAMES:
        for sym in PAIRS:
            p = Path(root) / "klines" / tf / f"{sym}.csv"
            if not p.exists():
                raise FileNotFoundError(f"no {tf} candles for {sym} under {root}")
            h.update(f"{tf}{sym}".encode() + _sha(p).encode())
    for sym in PAIRS:
        p = funding_archive.funding_path(Path(root), sym)
        if not p.exists():
            raise FileNotFoundError(f"no funding rates for {sym} under {root}")
        h.update(f"funding{sym}".encode() + _sha(p).encode())
    return h.hexdigest()[:16]


# ================================================================================ simulation
@dataclass
class Leg:
    weight: float
    raw_exit: float
    ts: int
    reason: str


@dataclass
class SimTrade:
    pair: str
    side: str
    entry_ts: int
    exit_ts: int
    raw_entry: float
    entry: float
    dist: float
    stop0: float
    peak: float
    legs: list[Leg]
    reason: str
    gross_r: float
    net_r: float
    funding_r: float
    extra_cost_r: float
    reentry: bool = False
    prev_key: tuple | None = None
    hold_ms: int = 0

    @property
    def exit_avg(self) -> float:
        return sum(l.weight * l.raw_exit for l in self.legs)


@dataclass
class Series:
    candles: list[Candle]
    sigs: list
    atr: list
    fast: list
    slow: list
    flat: list


def build_series(candles: list[Candle]) -> Series:
    sigs, atr = SF.ema_signal_series(candles)
    closes = [c.close for c in candles]
    fast, slow = ema_series(closes, SF.EMA_PARAMS["fast"]), ema_series(closes, SF.EMA_PARAMS["slow"])
    n, W = len(candles), SF.EMA_PARAMS["rolling_window"]
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
    flat = [bool(pct[i] is not None and avg[i] > 0 and pct[i] < avg[i] * SF.EMA_PARAMS["flat_threshold"]) for i in range(n)]
    return Series(candles, sigs, atr, fast, slow, flat)


def _funding_r(side: str, dist: float, events: list[tuple[int, float]], marks: dict[int, float], candles: list[Candle], start_ts: int, legs_open: list[tuple[int, float]]) -> float:
    """Funding in R for the events after `start_ts`: a long pays rate x notional, a short receives it. `legs_open` is [(time, weight still open from then)] ascending, weight 1 at the entry."""
    if dist <= 0:
        return 0.0
    ts_list = [c.timestamp for c in candles]
    total = 0.0
    for t, rate in events:
        mark = marks.get(t)
        if mark is None:
            k = bisect.bisect_right(ts_list, t) - 1
            mark = candles[k].close if k >= 0 else None
        if mark is None:
            continue
        w = 0.0
        for when, weight in legs_open:
            if when < t:
                w = weight
        cash = -rate * mark * w if side == "long" else rate * mark * w
        total += cash / dist
    return total


def _cost(side: str, raw_entry: float, dist: float, legs: list[Leg], partial_extra: float) -> tuple[float, float, float, float]:
    """(entry price after slippage, gross R, net R before funding, extra-order cost in R) for a trade whose legs' weights sum to 1."""
    slip, fee = SLIP_PER_SIDE_BPS / 1e4, FEE_BPS / 1e4
    entry = raw_entry * (1 + slip) if side == "long" else raw_entry * (1 - slip)
    gross = net = extra = 0.0
    for lg in legs:
        ex = lg.raw_exit * (1 - slip) if side == "long" else lg.raw_exit * (1 + slip)
        move = (ex - entry) if side == "long" else (entry - ex)
        gross += lg.weight * ((lg.raw_exit - raw_entry) if side == "long" else (raw_entry - lg.raw_exit)) / dist
        net += lg.weight * (move - fee * (entry + ex)) / dist
    if len(legs) > 1:
        extra = partial_extra * (EXTRA_ORDER_BPS / 1e4) * entry / dist
        net -= extra
    return entry, gross, net, extra


def _buffer(raw_entry: float, funding_paid_per_unit: float = 0.0) -> float:
    return profit_protect.cost_buffer(raw_entry, fee_bps_per_side=reporting.ASSUMED_FEE_BPS, slippage_bps_round_trip=SLIP_ROUND_TRIP_BPS, funding_paid_per_unit=funding_paid_per_unit)


def run_trade(pair: str, ser: Series, tf: str, i: int, side: str, rs: RuleSet, end_ms: int, events: list[tuple[int, float]], marks: dict[int, float], *, is_reentry=False, prev_key=None) -> tuple[SimTrade | None, int, bool]:
    """One trade from the signal on candle i (entry at the next open). Returns (trade or None, index of the exit candle, finished). `finished` is False when the window ended first."""
    cs, atr = ser.candles, ser.atr
    n = len(cs)
    tf_ms = TF_MS[tf]
    nxt = cs[i + 1]
    raw_entry = nxt.open
    slip = SLIP_PER_SIDE_BPS / 1e4
    entry_slipped = raw_entry * (1 + slip) if side == "long" else raw_entry * (1 - slip)
    dist = STOP_ATR * atr[i]
    if not dist > 0:
        return None, i + 1, True
    long_side = side == "long"
    stop = entry_slipped - dist if long_side else entry_slipped + dist
    stop0, stop_reason = stop, "stop_loss"
    peak = entry_slipped
    buffer0 = _buffer(raw_entry)
    tp = ema_exits.take_profit_level(side, entry_slipped, stop0, rs.rules84.take_profit_r, buffer0) if rs.rules84.take_profit_r > 0 else None
    partial_level = (entry_slipped + rs.partial_r * dist if long_side else entry_slipped - rs.partial_r * dist) if rs.partial_r > 0 and 0 < rs.partial_fraction < 1 else None
    legs: list[Leg] = []
    open_w = 1.0
    open_times: list[tuple[int, float]] = [(nxt.timestamp - 1, 1.0)]
    j = i + 1
    exit_idx, final_reason = None, None
    gap_peak: float | None = None

    def close_all(price: float, reason: str, idx: int) -> None:
        nonlocal exit_idx, final_reason, open_w
        legs.append(Leg(open_w, price, cs[idx].timestamp, reason))
        open_w = 0.0
        exit_idx, final_reason = idx, reason

    while j < n and exit_idx is None:
        c = cs[j]
        # 1. the stop that stood BEFORE this candle (wins ties)
        if long_side and c.low <= stop:
            close_all(min(stop, c.open), stop_reason, j)
            break
        if (not long_side) and c.high >= stop:
            close_all(max(stop, c.open), stop_reason, j)
            break
        # 2. take profit (PR #84)
        if tp is not None and ((long_side and c.high >= tp) or ((not long_side) and c.low <= tp)):
            close_all(max(tp, c.open) if long_side else min(tp, c.open), "take_profit", j)
            break
        # 3. partial profit
        if partial_level is not None and len(legs) == 0 and ((long_side and c.high >= partial_level) or ((not long_side) and c.low <= partial_level)):
            px = max(partial_level, c.open) if long_side else min(partial_level, c.open)
            legs.append(Leg(rs.partial_fraction, px, c.timestamp, "partial_profit"))
            open_w = 1.0 - rs.partial_fraction
            open_times.append((c.timestamp, open_w))
            floor = entry_slipped + buffer0 if long_side else entry_slipped - buffer0
            if (long_side and floor > stop) or ((not long_side) and floor < stop):
                stop, stop_reason = floor, "break_even_stop"  # applies from the next candle
        # 4. the strategy's own exit: the opposite crossover on this candle's close
        s2 = ser.sigs[j]
        if s2 is not None and s2 != side:
            close_all(c.close, "crossover_exit", j)
            break
        # 5. reversal exits (closed candle): the same two pure functions the live rules use, over the precomputed EMAs
        if rs.reversal.any_on:
            sign = 1.0 if long_side else -1.0
            if ser.fast[j] is not None and ser.slow[j] is not None:
                gap_peak = max(gap_peak if gap_peak is not None else float("-inf"), sign * (ser.fast[j] - ser.slow[j]))
            peak_now = max(peak, c.high) if long_side else min(peak, c.low)
            why = None
            if rs.reversal.fast_cross and ema_reversal.fast_cross_hit(side, close=c.close, fast=ser.fast[j], peak=peak_now, entry=entry_slipped, initial_stop=stop0, min_profit_r=rs.reversal.min_profit_r):
                why = ema_reversal.REASON_FAST
            elif rs.reversal.gap_fraction > 0 and ser.fast[j] is not None and ser.slow[j] is not None and ema_reversal.gap_shrink_hit(side, gap_now=sign * (ser.fast[j] - ser.slow[j]), gap_peak=gap_peak, fraction=rs.reversal.gap_fraction):
                why = ema_reversal.REASON_GAP
            if why is not None:
                close_all(c.close, why, j)
                break
        # 6. the peak after this candle
        peak = max(peak, c.high) if long_side else min(peak, c.low)
        held = (c.timestamp + tf_ms - nxt.timestamp) / tf_ms
        a_j = atr[j]
        # 7. time stop
        if rs.max_hold_candles > 0:
            got = profit_protect.time_decision(side=side, entry=entry_slipped, price=c.close, peak=peak, atr=a_j, held_candles=held, limit_candles=rs.max_hold_candles, buffer=buffer0, tighten_atr=rs.tighten_atr)
            if got is not None:
                if got[0] == "exit":
                    close_all(c.close, "time_stop", j)
                    break
                cand = [(got[1], "time_stop")]
            else:
                cand = []
        else:
            cand = []
        # 8. the levels earned by the peak
        if rs.rules84.any_on and (rs.rules84.trail_trigger_r > 0 or rs.rules84.breakeven_r > 0):
            lv = ema_exits.running_level(side, entry_slipped, stop0, peak, rs.rules84, a_j)
            if lv is not None:
                cand.append(lv)
        if rs.lock.any_on:
            lv = profit_protect.staged_level(side=side, entry=entry_slipped, initial_stop=stop0, peak=peak, atr=a_j, held_candles=held, rules=rs.lock, buffer=buffer0)
            if lv is not None:
                cand.append(lv)
        if cand:
            level, label = (max(cand, key=lambda x: x[0]) if long_side else min(cand, key=lambda x: x[0]))
            if profit_protect.is_better(side, level, stop):
                if (long_side and c.close <= level) or ((not long_side) and c.close >= level):
                    close_all(c.close, label, j)  # the price is already through the level: leave at the market
                    break
                stop, stop_reason = level, label
        j += 1
    if exit_idx is None or cs[exit_idx].timestamp + tf_ms > end_ms:
        return None, (exit_idx if exit_idx is not None else n), False
    partial_extra = legs[0].weight if len(legs) > 1 else 0.0
    entry, gross, net, extra = _cost(side, raw_entry, dist, legs, partial_extra)
    fr = _funding_r(side, dist, [(t, r) for t, r in events if nxt.timestamp < t <= cs[exit_idx].timestamp + tf_ms], marks, cs, nxt.timestamp, open_times)
    trade = SimTrade(pair, side, nxt.timestamp, cs[exit_idx].timestamp, raw_entry, entry, dist, stop0, peak, legs, final_reason or "", gross, net + fr, fr, extra, is_reentry, prev_key,
                     max(cs[exit_idx].timestamp + tf_ms - nxt.timestamp, tf_ms))
    return trade, exit_idx, True


def simulate(pair: str, ser: Series, tf: str, start_ms: int, end_ms: int, rs: RuleSet, events: list[tuple[int, float]]) -> tuple[list[SimTrade], int]:
    cs = ser.candles
    n = len(cs)
    tf_ms = TF_MS[tf]
    marks = {c.timestamp: c.open for c in cs}
    out: list[SimTrade] = []
    unfinished = 0
    i = 1
    while i < n - 1:
        c = cs[i]
        if c.timestamp < start_ms:
            i += 1
            continue
        if c.timestamp + tf_ms > end_ms:
            break
        side = ser.sigs[i]
        if side is None:
            i += 1
            continue
        trade, j, finished = run_trade(pair, ser, tf, i, side, rs, end_ms, events, marks)
        if not finished:
            unfinished += 1
            i += 1
            continue
        if trade is not None:
            out.append(trade)
        last_exit = j
        links = 0
        while trade is not None and rs.reentry.enabled and trade.reason in PROTECTIVE and links < rs.reentry.max_per_trend:
            k = _find_reentry(ser, tf_ms, trade, last_exit, links, rs.reentry, end_ms)
            if k is None:
                break
            nt, nj, fin = run_trade(pair, ser, tf, k, trade.side, rs, end_ms, events, marks, is_reentry=True, prev_key=(trade.pair, trade.entry_ts))
            if not fin or nt is None:
                if not fin:
                    unfinished += 1
                break
            out.append(nt)
            trade, last_exit, links = nt, nj, links + 1
        i = max(last_exit, i + 1)
    return out, unfinished


def _find_reentry(ser: Series, tf_ms: int, prev: SimTrade, exit_idx: int, links: int, rules: reentry.ReentryRules, end_ms: int) -> int | None:
    cs = ser.candles
    n = len(cs)
    exit_price = prev.exit_avg
    for k in range(exit_idx + 1, n - 1):
        since = k - exit_idx
        if since > rules.window_candles:
            return None
        if cs[k].timestamp + tf_ms > end_ms:
            return None
        if ser.sigs[k] is not None:
            return None  # a real crossover: the ordinary entry takes over
        if since < rules.cooldown_candles or ser.flat[k]:
            continue
        ok, _ = reentry.decide(rules=rules, side=prev.side, prev_reason=prev.reason, prev_exit_price=exit_price, candles_since_exit=since, reentries_so_far=links, fast=ser.fast[k], slow=ser.slow[k],
                               last_close=cs[k].close, atr=ser.atr[k])
        if ok:
            return k
    return None


# ================================================================================ metrics
def _flags(t: SimTrade) -> tuple[bool, bool, bool, bool]:
    """The live give-back definitions (reporting.giveback_flags) on a simulated trade: raw entry, the initial stop 2 ATR away, the best price and the quantity-weighted exit price."""
    stop = t.raw_entry - t.dist if t.side == "long" else t.raw_entry + t.dist
    return reporting.giveback_flags(SimpleNamespace(side=t.side, entry_price=t.raw_entry, initial_stop_price=stop, peak_price=t.peak, exit_price=t.exit_avg))


def metrics(trades: list[SimTrade], base_by_key: dict | None = None) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0, "avg_net_r": None, "profit_factor": None, "total_net_r": 0.0, "total_gross_r": 0.0, "cost_share_of_gross": None, "return_pct": 0.0, "max_dd_pct": 0.0, "worst_losing_streak": 0,
                "give_back_rate": None, "reached_1r": 0, "gave_back": 0, "winners_cut_short": 0, "reentries": 0, "reentries_recovered": 0, "avg_hold_hours": None, "funding_r": 0.0, "extra_order_r": 0.0, "win_rate": None}
    ordered = sorted(trades, key=lambda x: x.exit_ts)
    nets = [t.net_r for t in ordered]
    wins, losses = sum(v for v in nets if v > 0), -sum(v for v in nets if v < 0)
    streak = worst = 0
    equity, top, dd = 1.0, 1.0, 0.0
    for t in ordered:
        streak = streak + 1 if t.net_r <= 0 else 0
        worst = max(worst, streak)
        equity *= max(0.0, 1 + RISK_PCT / 100.0 * t.net_r)
        top = max(top, equity)
        dd = max(dd, (top - equity) / top * 100.0)
    reached = gave = 0
    for t in trades:
        _, r1, gb, _ = _flags(t)
        reached += r1
        gave += gb
    gross_total = sum(t.gross_r for t in trades)
    cost_total = gross_total - sum(nets) + sum(t.funding_r for t in trades) * -1.0 * -1.0  # costs = gross - net, funding included
    cost_total = gross_total - (sum(nets))
    cut = 0
    if base_by_key is not None:
        for t in trades:
            b = base_by_key.get((t.pair, t.entry_ts))
            if b is not None and not t.reentry and t.net_r > 0 and b.net_r > t.net_r + 1e-12 and t.exit_ts < b.exit_ts:
                cut += 1
    re_trades = [t for t in trades if t.reentry]
    return {"trades": n, "avg_net_r": sum(nets) / n, "profit_factor": (wins / losses) if losses > 0 else None, "total_net_r": sum(nets), "total_gross_r": gross_total,
            "cost_share_of_gross": (cost_total / gross_total) if gross_total > 0 else None, "return_pct": (equity - 1.0) * 100.0, "max_dd_pct": dd, "worst_losing_streak": worst,
            "give_back_rate": (gave / reached) if reached else None, "reached_1r": reached, "gave_back": gave, "winners_cut_short": cut, "reentries": len(re_trades), "reentries_recovered": sum(1 for t in re_trades if t.net_r > 0),
            "avg_hold_hours": sum(t.hold_ms for t in trades) / n / 3_600_000.0, "funding_r": sum(t.funding_r for t in trades), "extra_order_r": sum(t.extra_cost_r for t in trades), "win_rate": sum(1 for v in nets if v > 0) / n}


def run_window(series: dict, funding: dict, tf: str, start_ms: int, end_ms: int, sets: list[RuleSet]) -> dict:
    """{rule set name: pooled metrics (all pairs) plus per-pair net totals}. RS0 is always run (the baseline for 'winners cut short')."""
    results: dict[str, dict] = {}
    per_set: dict[str, list[SimTrade]] = {}
    for rs in sets:
        trades: list[SimTrade] = []
        for pair in PAIRS:
            ts, _ = simulate(pair, series[(pair, tf)], tf, start_ms, end_ms, rs, funding[pair])
            trades += ts
        per_set[rs.name] = trades
    base = {(t.pair, t.entry_ts): t for t in per_set[RS0.name]} if RS0.name in per_set else None
    for rs in sets:
        m = metrics(per_set[rs.name], base)
        m["per_pair_net_r"] = {p: sum(t.net_r for t in per_set[rs.name] if t.pair == p) for p in PAIRS}
        m["per_pair_trades"] = {p: sum(1 for t in per_set[rs.name] if t.pair == p) for p in PAIRS}
        results[rs.name] = m
    return results


# ================================================================================ report
def _d(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _f(x, spec="+.3f", none="n/a") -> str:
    return none if x is None else format(x, spec)


def report(title: str, window: tuple[int, int], by_tf: dict, extra: list[str] | None = None) -> str:
    lines = [f"# {title}", "", f"Window {_d(window[0])} to {_d(window[1])} (UTC, end exclusive). Plan hash `{plan_hash()}`. Costs: taker fee {FEE_BPS:g} bps per side and {SLIP_ROUND_TRIP_BPS:g} bps round-trip slippage on every order leg, "
             f"funding at the historical settlements, {EXTRA_ORDER_BPS:g} bps extra on every order beyond the entry and one exit. EMA strategy only, BTC, ETH, BNB and XRP pooled. R = the distance from the entry to the 2 ATR initial stop.", ""]
    for tf, rows in by_tf.items():
        base = rows[RS0.name]
        lines += [f"## {tf} candles", "", "| rule set | trades | avg net R | profit factor | net total R | vs RS0 (R) | return % at 0.5% risk | max DD % | worst losing streak | give-back rate | winners cut short | re-entries (recovered) | cost share of gross | funding R | extra-order R |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for name, m in rows.items():
            delta = None if m["total_net_r"] is None else m["total_net_r"] - base["total_net_r"]
            gb = "n/a" if m["give_back_rate"] is None else f"{m['give_back_rate'] * 100:.0f}% ({m['gave_back']} of {m['reached_1r']})"
            lines.append(f"| {name} | {m['trades']} | {_f(m['avg_net_r'])} | {_f(m['profit_factor'], '.2f')} | {_f(m['total_net_r'], '+.1f')} | {_f(delta, '+.1f')} | {_f(m['return_pct'], '+.1f')} | {m['max_dd_pct']:.1f} | {m['worst_losing_streak']} | {gb} | "
                         f"{m['winners_cut_short']} | {m['reentries']} ({m['reentries_recovered']}) | {_f(m['cost_share_of_gross'] and m['cost_share_of_gross'] * 100, '.0f')}% | {_f(m['funding_r'], '+.1f')} | {_f(m['extra_order_r'], '.1f')} |")
        lines.append("")
    lines += extra or []
    return "\n".join(lines) + "\n"


# ================================================================================ tune, confirm, ledger
class SealedError(Exception):
    pass


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def ledger_path(root: Path) -> Path:
    return Path(root) / "sealed_looks.profit.json"


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


def _run_all(root: Path, window: tuple[int, int], best_by_tf: dict | None = None) -> tuple[dict, dict]:
    candles = load_candles(root)
    series = {k: build_series(v) for k, v in candles.items()}
    funding = load_funding(root)
    out, bests = {}, {}
    for tf in TIMEFRAMES:
        sets = list(ALL_SETS)
        if best_by_tf is not None:
            sets.append(best_by_tf[tf])
        out[tf] = run_window(series, funding, tf, *window, sets)
        bests[tf] = best_by_tf[tf].name if best_by_tf else None
    return out, bests


def run_tune(root: Path, out_dir: Path, *, expect_plan_hash: str | None = None) -> dict:
    ph = plan_hash()
    if expect_plan_hash and expect_plan_hash != ph:
        raise SealedError(f"plan hash {ph} does not match the registered {expect_plan_hash}: a pinned file or the plan changed")
    dh = data_hash(root)
    tune_rows, _ = _run_all(root, TUNE)
    best = {tf: choose_best(tune_rows[tf]) for tf in TIMEFRAMES}
    # the chosen combination's own tuning numbers, for the record
    candles = load_candles(root)
    series = {k: build_series(v) for k, v in candles.items()}
    funding = load_funding(root)
    for tf in TIMEFRAMES:
        row = run_window(series, funding, tf, *TUNE, [RS0, best[tf]])
        tune_rows[tf][best[tf].name] = row[best[tf].name]
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    res = {"protocol": PROTOCOL, "plan_hash": ph, "data_hash": dh, "created": _stamp(), "tune_window": TUNE, "confirm_window": CONFIRM, "rows": tune_rows, "best": {tf: _jsonable(best[tf]) for tf in TIMEFRAMES}}
    (out_dir / "tune_results.json").write_text(json.dumps(res, indent=1, default=str))
    (out_dir / "selected.json").write_text(json.dumps({"plan_hash": ph, "data_hash": dh, "best": {tf: best[tf].name for tf in TIMEFRAMES}, "confirm_window": CONFIRM}, indent=1))
    note = ["", "The combination for each timeframe was chosen from these tuning numbers by the rule in the plan:"] + [f"- {tf}: {best[tf].name}" for tf in TIMEFRAMES]
    (out_dir / "tune_report.md").write_text(report("Profit protection v2: tuning window", TUNE, tune_rows, note))
    return res


def _rebuild_best(name: str, tune_json: dict, tf: str) -> RuleSet:
    """The combination for `tf` exactly as chosen on the tuning window: rebuilt by `choose_best` from the SAVED tuning numbers, so the confirmation run cannot choose differently."""
    return choose_best(tune_json["rows"][tf])


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
    tune_json = json.loads((out_dir / "tune_results.json").read_text())
    best = {tf: _rebuild_best(chosen["best"][tf], tune_json, tf) for tf in TIMEFRAMES}
    record_look(root, dh, {"ts": _stamp(), "reason": reason.strip(), "plan_hash": ph, "second_look": contaminated})  # BEFORE anything is read
    rows, _ = _run_all(root, CONFIRM, best)
    res = {"protocol": PROTOCOL, "plan_hash": ph, "data_hash": dh, "created": _stamp(), "confirm_window": CONFIRM, "opened": True, "contaminated": contaminated, "rows": rows, "best": {tf: best[tf].name for tf in TIMEFRAMES}}
    (out_dir / "confirm_results.json").write_text(json.dumps(res, indent=1, default=str))
    extra = ["", "The combination for each timeframe is the one chosen on the tuning window (not re-chosen here): " + "; ".join(f"{tf}: {best[tf].name}" for tf in TIMEFRAMES) + "."]
    if contaminated:
        extra.append("THIS IS A SECOND LOOK: it is not unseen evidence.")
    (out_dir / "final_report.md").write_text(report("Profit protection v2: confirmation window (read once)", CONFIRM, rows, extra))
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mba_bot.profit_backtest")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan")
    for name in ("tune", "confirm"):
        p = sub.add_parser(name)
        p.add_argument("--root", required=True, type=Path)
        p.add_argument("--out", required=True, type=Path)
        p.add_argument("--expect-plan-hash")
        if name == "confirm":
            p.add_argument("--confirm", action="store_true")
            p.add_argument("--reason", default="")
            p.add_argument("--second-look", action="store_true")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "plan":
            print(plan_hash())
            return 0
        if args.cmd == "tune":
            res = run_tune(args.root, args.out, expect_plan_hash=args.expect_plan_hash)
            print(f"tune done; combinations: {[v['name'] for v in res['best'].values()]}; report in {args.out}/tune_report.md")
            return 0
        res = run_confirm(args.root, args.out, reason=args.reason, confirm=args.confirm, expect_plan_hash=args.expect_plan_hash, second_look=args.second_look)
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
