"""The entry-rule study (docs/research/entry_rules_preregistration.md). Research only: no network, no key, no order, no live effect.

Question: does any entry rule have a positive net edge after costs on 1h candles, and is it stable across time? The three strategies (EMA, Donchian, RSI) are simulated with their own exits (no profit rules) on BTC, ETH, BNB and XRP
from 2020-04 to 2026-09, costs as in the other pre-registered studies (5 bps taker per side, 11 bps round-trip slippage, historical funding), once per CANDIDATE RULE. A candidate is a filter on the entry signal, a guard, or a
choice of pair, all FIXED in the plan (nothing is fitted). Walk-forward: nine test windows of six months (2022-04 to 2026-09), each with the 24 months before it as its tune window.

A candidate passes only if ALL of: P1 at least 300 trades in the nine test windows; P2 pooled net R per trade above zero and the 99% month-block interval of the pooled TOTAL above zero; P3 net total R above zero in at
least 7 of the 9 test windows; P4 (does the rule itself add?) its mean net R per trade exceeds the unfiltered strategy's with the 99% month-block interval of the difference above zero. P1 to P3 together mean "an edge that
survives costs and the windows"; P4 says whether the rule is what made it. About 40 candidates are tested at the 1% level, so about 0.4 false passes are expected by chance: a single pass proves little. The walk-forward pick
(the best candidate on each tune window, traded in its test window) is reported with the same intervals."""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from . import donchian as DN
from . import profit_backtest as P
from . import profit_backtest_dr as D
from . import rsi_mr as R
from . import shortframe_backtest as SF
from .indicators import adx as adx_series
from .indicators import atr as atr_series
from .indicators import ema as ema_series
from .indicators import rsi as rsi_series
from .research import bootstrap
from .strategy import Candle

PROTOCOL = "entry-rule study v1: EMA, Donchian, RSI on 1h with fixed candidate rules, 9 walk-forward test windows 2022-04 to 2026-09, 99% month-block intervals"
PAIRS = P.PAIRS
HOUR = 3_600_000
TF_MS = HOUR
DAY = 86_400_000
LEVEL = 0.99
BOOT = {"reps": 2000, "level": LEVEL, "seed": 20260202}
STRATEGIES = ("ema", "donchian", "rsi")


def _ms(text: str) -> int:
    return SF._ms(text)


RUN_START = _ms("2020-04-01")
RUN_END = _ms("2026-10-01")
TEST_STARTS = [_ms(f"{y}-{m:02d}-01") for y, m in ((2022, 4), (2022, 10), (2023, 4), (2023, 10), (2024, 4), (2024, 10), (2025, 4), (2025, 10), (2026, 4))]
TEST_LEN = 182 * DAY  # six months (the shortest six-month span), fixed so every window has the same length and none overlaps the next
TUNE_LEN = 730 * DAY
PASS = {"min_trades": 300, "min_positive_windows": 7, "windows": 9}

CANDIDATES = {
    "ema": ("gap", "htf4", "htfD", "adx", "vol", "volat", "cool24", "streak3", "fee3"),
    "donchian": ("htf4", "cool24", "streak3", "fee3"),
    "rsi": ("htfD", "streak3", "fee3"),
}
FILTER_PARAMS = {"gap_atr": 0.25, "htf_fast": 20, "htf_slow": 50, "adx_min": 20.0, "vol_mult": 1.0, "vol_avg_bars": 20, "volat_lookback": 500, "cool_hours": 24, "streak_n": 3, "streak_pause_hours": 24,
                 "fee_margin": 3.0, "fee_roundtrip_bps": 21.0, "pair_trailing_days": 365}


# ================================================================================ plan
_PINNED = ("entry_study.py", "profit_backtest_dr.py", "profit_backtest.py", "shortframe_backtest.py", "donchian.py", "rsi_mr.py", "indicators.py", "strategy.py", "reporting.py", "research/bootstrap.py", "research/archive.py",
           "research/funding_archive.py")


def source_pins() -> dict:
    here = Path(__file__).parent
    return {n: hashlib.sha256((here / n).read_bytes()).hexdigest() for n in _PINNED}


def plan_blob() -> dict:
    return {"protocol": PROTOCOL, "pairs": PAIRS, "run": (RUN_START, RUN_END), "test_starts": TEST_STARTS, "test_len_ms": TEST_LEN, "tune_len_ms": TUNE_LEN, "level": LEVEL, "bootstrap": BOOT, "pass_rule": PASS,
            "candidates": CANDIDATES, "filter_params": FILTER_PARAMS, "ema": SF.EMA_PARAMS, "donchian": vars(DN.DonchianParams()), "rsi": vars(R.RsiParams()), "donchian_cooldown_ms": SF.DONCHIAN_COOLDOWN_MS,
            "costs": {"fee_bps": P.FEE_BPS, "slippage_round_trip_bps": P.SLIP_ROUND_TRIP_BPS}, "sources_sha256": source_pins(),
            "pair_candidates": "each pair alone; and the pair with the best trailing 365-day net total R at each test-window start",
            "walk_forward_pick": "on each tune window the candidate (including none) with the highest mean net R among those with at least 100 tune trades, traded in the following test window"}


def plan_hash() -> str:
    return hashlib.sha256(json.dumps(plan_blob(), sort_keys=True, default=str).encode()).hexdigest()[:16]


def data_hash(root: Path) -> str:
    return D.data_hash(root)


# ================================================================================ features (no look-ahead)
@dataclass
class Feat:
    cs: list[Candle]
    atr: list
    adx: list
    fast: list
    slow: list
    vol_avg: list
    atr_pct: list
    atr_med: list
    htf4: list
    htfD: list


def _htf_trend(cs: list[Candle], group_ms: int, fast: int, slow: int) -> list[int]:
    """+1 / -1 / 0 per 1h candle: the sign of (fast EMA - slow EMA) of the closes of the latest COMPLETED group bar (4h or daily) as of that candle's close. A group bar is complete when its last 1h candle has closed."""
    out, ef, es, n_groups = [], None, None, 0
    af, a_s = 2.0 / (fast + 1), 2.0 / (slow + 1)
    for c in cs:
        if (c.timestamp + HOUR) % group_ms == 0:
            n_groups += 1
            ef = c.close if ef is None else ef + af * (c.close - ef)
            es = c.close if es is None else es + a_s * (c.close - es)
        out.append(0 if (es is None or n_groups < slow) else (1 if ef > es else (-1 if ef < es else 0)))
    return out


def build_feat(cs: list[Candle]) -> Feat:
    closes = [c.close for c in cs]
    atr = atr_series([c.high for c in cs], [c.low for c in cs], closes, SF.EMA_PARAMS["atr_period"])
    adx = adx_series([c.high for c in cs], [c.low for c in cs], closes, 14)
    fast, slow = ema_series(closes, SF.EMA_PARAMS["fast"]), ema_series(closes, SF.EMA_PARAMS["slow"])
    n, vb, lb = len(cs), FILTER_PARAMS["vol_avg_bars"], FILTER_PARAMS["volat_lookback"]
    vol_avg = [None] * n
    for i in range(vb, n):
        vol_avg[i] = sum(c.volume for c in cs[i - vb:i]) / vb
    pct = [(a / c) if (a is not None and c) else None for a, c in zip(atr, closes)]
    med = [None] * n
    for i in range(lb, n, 1):
        if i % 24 == 0 or med[i - 1] is None:
            window = [v for v in pct[i - lb:i] if v is not None]
            med[i] = median(window) if len(window) > lb // 2 else None
        else:
            med[i] = med[i - 1]  # the median of the last 500 bars is refreshed once a day: it moves very slowly
    return Feat(cs, atr, adx, fast, slow, vol_avg, pct, med, _htf_trend(cs, 4 * HOUR, FILTER_PARAMS["htf_fast"], FILTER_PARAMS["htf_slow"]), _htf_trend(cs, DAY, FILTER_PARAMS["htf_fast"], FILTER_PARAMS["htf_slow"]))


def passes(name: str, f: Feat, i: int, side: str) -> bool:
    """A stateless filter at the signal candle `i` (everything it reads is known at that candle's close)."""
    if name == "gap":
        return f.atr[i] is not None and f.fast[i] is not None and f.slow[i] is not None and abs(f.fast[i] - f.slow[i]) / f.atr[i] >= FILTER_PARAMS["gap_atr"]
    if name in ("htf4", "htfD"):
        t = (f.htf4 if name == "htf4" else f.htfD)[i]
        return t == (1 if side == "long" else -1)
    if name == "adx":
        return f.adx[i] is not None and f.adx[i] >= FILTER_PARAMS["adx_min"]
    if name == "vol":
        return f.vol_avg[i] is not None and f.cs[i].volume >= FILTER_PARAMS["vol_mult"] * f.vol_avg[i]
    if name == "volat":
        return f.atr_pct[i] is not None and f.atr_med[i] is not None and f.atr_pct[i] >= f.atr_med[i]
    if name == "fee3":
        return f.atr_pct[i] is not None and f.atr_pct[i] * 10_000 >= FILTER_PARAMS["fee_margin"] * FILTER_PARAMS["fee_roundtrip_bps"]
    return True


class State:
    """What the stateful guards remember for one pair and one strategy."""

    def __init__(self):
        self.last_stop_exit_ms = None
        self.losses = 0
        self.pause_until_ms = 0


def blocked_by_guard(rule: str | None, st: State, t_ms: int) -> bool:
    if rule == "cool24":
        return st.last_stop_exit_ms is not None and t_ms < st.last_stop_exit_ms + FILTER_PARAMS["cool_hours"] * HOUR
    if rule == "streak3":
        return t_ms < st.pause_until_ms
    return False


def record(rule: str | None, st: State, trade) -> None:
    exit_ms = trade.exit_ts + HOUR
    if trade.reason == "stop_loss":
        st.last_stop_exit_ms = exit_ms
    st.losses = st.losses + 1 if trade.net_r <= 0 else 0
    if rule == "streak3" and st.losses >= FILTER_PARAMS["streak_n"]:
        st.pause_until_ms = exit_ms + FILTER_PARAMS["streak_pause_hours"] * HOUR
        st.losses = 0


def _ok(rule, f, st, i, side) -> bool:
    if rule is None:
        return True
    if rule in ("cool24", "streak3"):
        return not blocked_by_guard(rule, st, f.cs[i].timestamp + HOUR)
    return passes(rule, f, i, side)


# ================================================================================ the simulators (own exits only, a candidate rule gates the entry)
def sim_ema(pair, f: Feat, rule, start_ms, end_ms, events):
    cs, atr = f.cs, f.atr
    sigs, _ = SF.ema_signal_series(cs)
    marks = {c.timestamp: c.open for c in cs}
    out, st, n = [], State(), len(cs)
    slip = P.SLIP_PER_SIDE_BPS / 1e4
    i = 1
    while i < n - 1:
        c = cs[i]
        if c.timestamp < start_ms:
            i += 1
            continue
        if c.timestamp + HOUR > end_ms:
            break
        side = sigs[i]
        if side is None or not _ok(rule, f, st, i, side):
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
        if exit_idx is None or cs[exit_idx].timestamp + HOUR > end_ms:
            i += 1
            continue
        t = D._finish(pair, side, nxt.timestamp, nxt.open, dist, stop, exit_idx, raw, reason, entry, cs, events, marks)
        out.append(t)
        record(rule, st, t)
        i = max(exit_idx, i + 1)
    return out


def sim_donchian(pair, f: Feat, rule, start_ms, end_ms, events):
    cs, atr = f.cs, f.atr
    p = DN.DonchianParams()
    atr_d = atr_series([c.high for c in cs], [c.low for c in cs], [c.close for c in cs], p.atr_period)
    idx = {c.timestamp: k for k, c in enumerate(cs)}
    marks = {c.timestamp: c.open for c in cs}
    out, st, n = [], State(), len(cs)
    cooldown_until = 0
    slip = P.SLIP_PER_SIDE_BPS / 1e4
    i = DN.min_candles(p) - 1
    while i < n - 1:
        c = cs[i]
        if c.timestamp < start_ms:
            i += 1
            continue
        if c.timestamp + HOUR > end_ms:
            break
        if c.timestamp + HOUR < cooldown_until:
            i += 1
            continue
        sig = DN.signal_at(cs, atr_d, i, p, allow_short=True)
        if sig.side is None or not _ok(rule, f, st, i, sig.side):
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
            ex = DN.exit_check(side=sig.side, entry=entry, stop=stop, opened_ms=nxt.timestamp, closed=cs[s0:e0], tf_ms=TF_MS, p=p, atr_values=atr_d[s0:e0])
            if ex is not None or e0 >= n:
                break
        if ex is None or ex.candle_ts + HOUR > end_ms:
            i += 1
            continue
        e = idx[ex.candle_ts]
        dist = abs(entry - stop)
        if not dist > 0:
            i += 1
            continue
        t = D._finish(pair, sig.side, nxt.timestamp, nxt.open, dist, stop, e, ex.price, ex.reason, entry, cs, events, marks)
        out.append(t)
        record(rule, st, t)
        if ex.reason == DN.EXIT_STOP:
            cooldown_until = cs[e].timestamp + HOUR + SF.DONCHIAN_COOLDOWN_MS
        i = max(e, i + 1)
    return out


def sim_rsi(pair, f: Feat, rule, start_ms, end_ms, events):
    cs = f.cs
    p = R.RsiParams()
    closes = [c.close for c in cs]
    r = rsi_series(closes, p.rsi_period)
    a = adx_series([c.high for c in cs], [c.low for c in cs], closes, p.adx_period)
    t_atr = atr_series([c.high for c in cs], [c.low for c in cs], closes, p.atr_period)
    idx = {c.timestamp: k for k, c in enumerate(cs)}
    marks = {c.timestamp: c.open for c in cs}
    out, st, n = [], State(), len(cs)
    slip = P.SLIP_PER_SIDE_BPS / 1e4
    i = max(R.min_candles(p), 1)
    while i < n - 1:
        c = cs[i]
        if c.timestamp < start_ms:
            i += 1
            continue
        if c.timestamp + HOUR > end_ms:
            break
        sig = R.signal_from_values(r[i], r[i - 1], a[i], t_atr[i], c.close, p, allow_short=True)
        if sig.side is None or not _ok(rule, f, st, i, sig.side):
            i += 1
            continue
        nxt = cs[i + 1]
        entry = nxt.open * (1 + slip) if sig.side == "long" else nxt.open * (1 - slip)
        stop = R.stop_price(entry, sig.atr, sig.side, p)
        horizon = min(n, i + 2 + p.time_stop_candles + 1)
        s0 = max(0, i + 1 - SF.RSI_WARMUP)
        ex = R.exit_check(side=sig.side, entry=entry, stop=stop, opened_ms=nxt.timestamp, closed=cs[s0:horizon], tf_ms=TF_MS, p=p)
        if ex is None or ex.candle_ts + HOUR > end_ms:
            i += 1
            continue
        e = idx[ex.candle_ts]
        dist = abs(entry - stop)
        if not dist > 0:
            i += 1
            continue
        t = D._finish(pair, sig.side, nxt.timestamp, nxt.open, dist, stop, e, ex.price, ex.reason, entry, cs, events, marks)
        out.append(t)
        record(rule, st, t)
        i = max(e, i + 1)
    return out


SIM = {"ema": sim_ema, "donchian": sim_donchian, "rsi": sim_rsi}


# ================================================================================ statistics
def _month(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m")


def window_of(ts: int) -> int | None:
    for k, s in enumerate(TEST_STARTS):
        if s <= ts < s + TEST_LEN:
            return k
    return None


def block_mean_diff(a: list, b: list, *, reps: int, level: float, seed: int):
    """(mean_a - mean_b, low, high) resampling whole calendar months with replacement (a and b are lists of (month, net R)). None when there are fewer than 3 months or either side is empty."""
    months = sorted({m for m, _ in a} | {m for m, _ in b})
    if len(months) < 3 or not a or not b:
        return None
    ga, gb = {}, {}
    for m, v in a:
        ga.setdefault(m, []).append(v)
    for m, v in b:
        gb.setdefault(m, []).append(v)
    rng = random.Random(seed)
    diffs = []
    for _ in range(reps):
        pick = [months[rng.randrange(len(months))] for _ in months]
        xa = [v for m in pick for v in ga.get(m, ())]
        xb = [v for m in pick for v in gb.get(m, ())]
        if xa and xb:
            diffs.append(sum(xa) / len(xa) - sum(xb) / len(xb))
    if len(diffs) < reps // 2:
        return None
    diffs.sort()
    a_, hi = (1 - level) / 2, 1 - (1 - level) / 2
    pa = [v for _, v in a]
    pb = [v for _, v in b]
    return sum(pa) / len(pa) - sum(pb) / len(pb), diffs[int(a_ * (len(diffs) - 1))], diffs[int(hi * (len(diffs) - 1))]


def evaluate(trades: list, base_trades: list) -> dict:
    """The pre-registered pass rule on the trades of the nine test windows (trades are assigned to a window by their entry time)."""
    win = {k: [] for k in range(len(TEST_STARTS))}
    for t in trades:
        k = window_of(t.entry_ts)
        if k is not None:
            win[k].append(t)
    pooled = [t for k in win for t in win[k]]
    n = len(pooled)
    per_window = [{"trades": len(win[k]), "net_total_r": sum(t.net_r for t in win[k]), "net_per_trade": (sum(t.net_r for t in win[k]) / len(win[k])) if win[k] else None} for k in win]
    blocks = {}
    for t in pooled:
        blocks[_month(t.entry_ts)] = blocks.get(_month(t.entry_ts), 0.0) + t.net_r
    total_ci = bootstrap.block_sum_ci(blocks, **BOOT)
    mean_ci = bootstrap.mean_ci([t.net_r for t in pooled], **BOOT)
    base_pool = [t for t in base_trades if window_of(t.entry_ts) is not None]
    diff = block_mean_diff([(_month(t.entry_ts), t.net_r) for t in pooled], [(_month(t.entry_ts), t.net_r) for t in base_pool], **BOOT)
    pos_windows = sum(1 for w in per_window if w["trades"] and w["net_total_r"] > 0)
    mean = (sum(t.net_r for t in pooled) / n) if n else None
    p1 = n >= PASS["min_trades"]
    p2 = bool(n and mean > 0 and total_ci is not None and total_ci[1] > 0)
    p3 = pos_windows >= PASS["min_positive_windows"]
    p4 = diff is not None and diff[1] > 0
    return {"trades": n, "mean_net_r": mean, "mean_ci": mean_ci, "total_net_r": sum(t.net_r for t in pooled), "total_block_ci": total_ci, "positive_windows": pos_windows, "per_window": per_window,
            "diff_vs_unfiltered": diff, "P1": p1, "P2": p2, "P3": p3, "P4": p4, "edge_survives": p1 and p2 and p3, "passes_all": p1 and p2 and p3 and p4}


# ================================================================================ the run
def _load(root: Path):
    candles = {pair: sorted(SF._store(Path(root), pair, "1h"), key=lambda c: c.timestamp) for pair in PAIRS}
    return candles, P.load_funding(root)


def trailing_best_pair(base: list, window_start: int) -> str:
    """The pair with the best net total R over the 365 days BEFORE `window_start`, counting only trades that had already EXITED by then (nothing from the window or after it is read). Ties go to the pair name."""
    lo = window_start - FILTER_PARAMS["pair_trailing_days"] * DAY
    totals = {p: sum(t.net_r for t in base if t.pair == p and lo <= t.exit_ts < window_start) for p in PAIRS}
    return max(PAIRS, key=lambda p: (totals[p], p))


def run_all(root: Path) -> dict:
    candles, funding = _load(root)
    feats = {pair: build_feat(candles[pair]) for pair in PAIRS}
    results: dict = {}
    for strat in STRATEGIES:
        by_rule: dict[str, list] = {}
        for rule in (None,) + tuple(CANDIDATES[strat]):
            trades = []
            for pair in PAIRS:
                trades += SIM[strat](pair, feats[pair], rule, RUN_START, RUN_END, funding[pair])
            by_rule[rule or "none"] = trades
        base = by_rule["none"]
        # pair candidates are selections on the unfiltered trades (pairs are simulated independently)
        for pair in PAIRS:
            by_rule[f"pair:{pair}"] = [t for t in base if t.pair == pair]
        trail = []
        for s in TEST_STARTS:
            best = trailing_best_pair(base, s)
            trail += [t for t in base if t.pair == best and s <= t.entry_ts < s + TEST_LEN]
        by_rule["pair:trailing_best"] = trail
        evals = {name: evaluate(tr, base) for name, tr in by_rule.items()}
        # the walk-forward pick: the best candidate on each tune window, traded in the next test window
        picks, picked_trades = [], []
        for k, s in enumerate(TEST_STARTS):
            tune = (s - TUNE_LEN, s)
            best_name, best_mean = "none", None
            for name, tr in by_rule.items():
                if name == "pair:trailing_best":
                    continue
                sel = [t.net_r for t in tr if tune[0] <= t.entry_ts < tune[1]]
                if len(sel) >= 100 and (best_mean is None or sum(sel) / len(sel) > best_mean):
                    best_name, best_mean = name, sum(sel) / len(sel)
            picks.append({"window": k, "pick": best_name, "tune_mean": best_mean})
            picked_trades += [t for t in by_rule[best_name] if s <= t.entry_ts < s + TEST_LEN]
        evals["WALK_FORWARD_PICK"] = evaluate(picked_trades, base)
        results[strat] = {"evals": evals, "picks": picks}
    return results


# ================================================================================ report
def _d(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m")


def _f(x, spec="+.3f", none="n/a") -> str:
    return none if x is None else format(x, spec)


def _ci(ci, spec="+.1f") -> str:
    return "n/a" if ci is None else f"[{ci[1]:{spec}}, {ci[2]:{spec}}]"


def report(results: dict) -> str:
    lines = ["# Entry-rule study: results", "", f"Plan hash `{plan_hash()}`. Nine test windows of six months, {_d(TEST_STARTS[0])} to {_d(TEST_STARTS[-1] + TEST_LEN)}, 1h candles, BTC, ETH, BNB, XRP pooled, own exits (no profit rules), "
             f"5 bps taker per side, 11 bps round-trip slippage, historical funding. {int(LEVEL * 100)}% month-block bootstrap intervals. Trades are assigned to a window by entry time.", "",
             "Pass rule (fixed in advance): P1 at least 300 trades; P2 mean net R above zero and the interval of the pooled total above zero; P3 net total R above zero in at least 7 of 9 windows; P4 the mean beats the unfiltered strategy with the interval of the difference above zero.", ""]
    for strat, res in results.items():
        lines += [f"## {strat}", "", "| candidate | trades | mean net R | total net R | total R interval | windows > 0 (of 9) | mean vs unfiltered [interval] | P1 | P2 | P3 | P4 | edge survives (P1-P3) | passes all |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for name, e in res["evals"].items():
            d = e["diff_vs_unfiltered"]
            lines.append(f"| {name} | {e['trades']} | {_f(e['mean_net_r'])} | {_f(e['total_net_r'], '+.1f')} | {_ci(e['total_block_ci'])} | {e['positive_windows']} | {'n/a' if d is None else f'{d[0]:+.3f} [{d[1]:+.3f}, {d[2]:+.3f}]'} | "
                         f"{'yes' if e['P1'] else 'no'} | {'yes' if e['P2'] else 'no'} | {'yes' if e['P3'] else 'no'} | {'yes' if e['P4'] else 'no'} | {'YES' if e['edge_survives'] else 'no'} | {'YES' if e['passes_all'] else 'no'} |")
        lines += ["", "Walk-forward picks (candidate with the best tune-window mean, per test window): " + ", ".join(f"{p['pick']}" for p in res["picks"]), ""]
    return "\n".join(lines) + "\n"


def run(root: Path, out_dir: Path, *, expect_plan_hash: str | None = None) -> dict:
    ph = plan_hash()
    if expect_plan_hash and expect_plan_hash != ph:
        raise ValueError(f"plan hash {ph} does not match the registered {expect_plan_hash}")
    dh = data_hash(root)
    results = run_all(root)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps({"plan_hash": ph, "data_hash": dh, "results": results}, indent=1, default=lambda o: list(o) if isinstance(o, tuple) else str(o)))
    (out_dir / "report.md").write_text(report(results))
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mba_bot.entry_study")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan")
    r = sub.add_parser("run")
    r.add_argument("--root", required=True, type=Path)
    r.add_argument("--out", required=True, type=Path)
    r.add_argument("--expect-plan-hash")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "plan":
            print(plan_hash())
            return 0
        run(args.root, args.out, expect_plan_hash=args.expect_plan_hash)
        print(f"done; report in {args.out}/report.md")
        return 0
    except Exception as exc:  # noqa: BLE001 - class name only
        print(f"entry study failed ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
