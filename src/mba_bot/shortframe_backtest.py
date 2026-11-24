"""Short-timeframe research: can the bot's three strategies (EMA cross, Donchian breakout, RSI mean reversion) work on 5m, 15m and 30m candles after real costs? 1h is the control. Pre-registered; RESEARCH ONLY.
Nothing here touches the trading service: it reads candle files and writes reports. Registration: docs/research/shortframe_preregistration.md.

    python -m mba_bot.shortframe_backtest plan
    python -m mba_bot.shortframe_backtest tune    --root research_data --out research_out/shortframe --expect-plan-hash <hash>
    python -m mba_bot.shortframe_backtest confirm --root research_data --out research_out/shortframe --confirm --reason "<why>" --expect-plan-hash <hash>

No parameter is tuned. The rules are the live code's, unchanged (EMA 20/50 with its flat filter and a 2 ATR stop, exiting on the opposite crossover; Donchian with its trailing stop; RSI with its target and time stop),
applied to candles of another length. A cell (strategy x timeframe) either clears the pre-registered rule on the tuning window or it does not; only cells that clear it go to the confirmation window, which is
read ONCE (a one-look ledger beside the data). Costs: Futures taker fee on both sides plus the fee guard's 11 bps round-trip slippage assumption. Signals use closed candles; the entry is the NEXT candle's open;
when one candle touches the stop and something else, the stop wins; a candle that opens beyond the stop fills at its open. Indicators are computed over the whole series (the live bot uses the latest 200
candles: a small, named difference). RSI exits use a 400-candle warm-up slice (a difference below 1e-6 from the full history)."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import donchian as DN
from . import rsi_mr as R
from .indicators import adx as adx_series
from .indicators import atr as atr_series
from .indicators import ema as ema_series
from .indicators import rsi as rsi_series
from .research.data import load_candles as _load_store
from .strategy import Candle

PROTOCOL = "short-timeframe research v1: EMA, Donchian, RSI on 5m, 15m, 30m (1h control), costs included"
PAIRS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT")
TIMEFRAMES = ("5m", "15m", "30m", "1h")
TF_MS = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000}
STRATEGIES = ("ema", "donchian", "rsi")
FEE_BPS_PER_SIDE = 5.0  # USD-M Futures taker, as in the other backtests
SLIPPAGE_ROUND_TRIP_BPS = 11.0  # the fee guard's assumption, split evenly over the two sides
SLIP_BPS_PER_SIDE = SLIPPAGE_ROUND_TRIP_BPS / 2.0
RISK_PCT = 0.5  # for the equity curve and the drawdown only
DAY_MS = 86_400_000
EMBARGO_DAYS = 3
DONCHIAN_COOLDOWN_MS = 4 * 3_600_000  # the live default after an initial-stop exit, in real time
RSI_WARMUP = 400
EXIT_SLICE = 6000

EMA_PARAMS = {"fast": 20, "slow": 50, "atr_period": 14, "flat_threshold": 0.75, "rolling_window": 50, "stop_atr": 2.0}


def _ms(text: str) -> int:
    return int(datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


# Windows (UTC, end exclusive). Tune: 2025-11-01 to 2026-07-01. Embargo: 3 days. Confirm: 2026-07-04 to 2026-09-15. Nothing after 2026-09-15 is read: that stretch was the sealed window of the earlier 1h backtests.
TUNE = (_ms("2025-11-01"), _ms("2026-07-01"))
CONFIRM = (TUNE[1] + EMBARGO_DAYS * DAY_MS, _ms("2026-09-15"))
MONTHS_NEEDED = ["2025-10", "2025-11", "2025-12", "2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"]  # October 2025 is indicator warm-up only

# The pass rule, per cell (strategy x timeframe), pooled over the four pairs, in one window.
GATE = {"min_trades": 100, "min_net_expectancy_r": 0.05, "max_cost_share_of_gross": 0.5, "min_positive_pairs": 3}


# ================================================================================ plan
_PINNED = ("shortframe_backtest.py", "strategy.py", "donchian.py", "rsi_mr.py", "indicators.py", "research/archive.py")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_pins() -> dict:
    here = Path(__file__).parent
    return {name: _sha(here / name) for name in _PINNED}


def plan_blob() -> dict:
    return {"protocol": PROTOCOL, "pairs": PAIRS, "timeframes": TIMEFRAMES, "strategies": STRATEGIES, "fee_bps_per_side": FEE_BPS_PER_SIDE, "slippage_round_trip_bps": SLIPPAGE_ROUND_TRIP_BPS,
            "risk_pct": RISK_PCT, "tune": TUNE, "confirm": CONFIRM, "embargo_days": EMBARGO_DAYS, "gate": GATE, "ema": EMA_PARAMS, "donchian": asdict(DN.DonchianParams()),
            "donchian_cooldown_ms": DONCHIAN_COOLDOWN_MS, "rsi": asdict(R.RsiParams()), "rsi_warmup": RSI_WARMUP, "exit_slice": EXIT_SLICE, "sources_sha256": source_pins(),
            "rules": "closed-candle signal, next-open entry, stop first, gap fills at open, one position per pair, pairs pooled with no slot limit, unfinished trades not counted, confirm only for cells that clear the gate on tune"}


def plan_hash() -> str:
    return hashlib.sha256(json.dumps(plan_blob(), sort_keys=True).encode()).hexdigest()[:16]


# ================================================================================ data
def data_hash(root: Path) -> str:
    h = hashlib.sha256()
    for tf in TIMEFRAMES:
        for sym in PAIRS:
            p = Path(root) / "klines" / tf / f"{sym}.csv"
            if not p.exists():
                raise FileNotFoundError(f"no {tf} candles for {sym} under {root}")
            h.update(f"{tf}{sym}".encode() + _sha(p).encode())
    return h.hexdigest()[:16]


def load_all(root: Path) -> dict[tuple[str, str], list[Candle]]:
    out = {}
    for tf in TIMEFRAMES:
        for sym in PAIRS:
            cs = sorted(_store(root, sym, tf), key=lambda c: c.timestamp)
            out[(sym, tf)] = cs
    return out


def _store(root: Path, sym: str, tf: str) -> list[Candle]:
    return _load_store(Path(root), sym, tf)


# ================================================================================ simulation
@dataclass(frozen=True)
class SimTrade:
    pair: str
    side: str
    entry_ts: int
    exit_ts: int
    entry: float
    exit: float
    stop: float
    reason: str
    gross_r: float  # before fees and slippage
    net_r: float
    hold_ms: int


def _cost_trade(symbol: str, side: str, entry_ts: int, exit_ts: int, raw_entry: float, raw_exit: float, stop_fn, reason: str, tf_ms: int) -> SimTrade | None:
    """Fill both sides against the trade (slippage), charge the taker fee on both sides, express in R of the slipped entry's stop distance."""
    slip = SLIP_BPS_PER_SIDE / 1e4
    fee = FEE_BPS_PER_SIDE / 1e4
    entry = raw_entry * (1 + slip) if side == "long" else raw_entry * (1 - slip)
    exit_ = raw_exit * (1 - slip) if side == "long" else raw_exit * (1 + slip)
    stop = stop_fn(entry)
    dist = abs(entry - stop)
    if not dist > 0 or not ((side == "long" and stop < entry) or (side == "short" and stop > entry)):
        return None
    gross = ((raw_exit - raw_entry) if side == "long" else (raw_entry - raw_exit)) / dist
    move = (exit_ - entry) if side == "long" else (entry - exit_)
    net = (move - fee * (entry + exit_)) / dist
    return SimTrade(symbol, side, entry_ts, exit_ts, entry, exit_, stop, reason, gross, net, max(exit_ts + tf_ms - entry_ts, tf_ms))


def ema_signal_series(candles: list[Candle]) -> tuple[list[str | None], list[float | None]]:
    """The EMA strategy's verdict on every candle ("long", "short" or None), and the ATR series. The same rule as strategy.generate_signal (a crossover of the fast over the slow EMA, ignored when the ATR% is below
    the flat-market threshold of its rolling average), computed once over the series instead of once per candle."""
    closes = [c.close for c in candles]
    fast, slow = ema_series(closes, EMA_PARAMS["fast"]), ema_series(closes, EMA_PARAMS["slow"])
    atr = atr_series([c.high for c in candles], [c.low for c in candles], closes, EMA_PARAMS["atr_period"])
    n = len(candles)
    # rolling mean of ATR% over the last `rolling_window` values ending at i (None entries skipped), exactly as strategy.generate_signal reads atr_values[-window:]
    W = EMA_PARAMS["rolling_window"]
    pct = [(a / c) if (a is not None and c) else None for a, c in zip(atr, closes)]
    avg: list[float] = [0.0] * n
    total, cnt = 0.0, 0
    for i in range(n):
        if pct[i] is not None:
            total += pct[i]
            cnt += 1
        j = i - W
        if j >= 0 and pct[j] is not None:
            total -= pct[j]
            cnt -= 1
        avg[i] = (total / cnt) if cnt else 0.0
    out: list[str | None] = [None] * n
    for i in range(1, n):
        if fast[i] is None or fast[i - 1] is None or slow[i] is None or slow[i - 1] is None or atr[i] is None:
            continue
        up = fast[i - 1] <= slow[i - 1] and fast[i] > slow[i]
        down = fast[i - 1] >= slow[i - 1] and fast[i] < slow[i]
        if not (up or down):
            continue
        if avg[i] > 0 and pct[i] < avg[i] * EMA_PARAMS["flat_threshold"]:
            continue
        out[i] = "long" if up else "short"
    return out, atr


def simulate_ema(symbol: str, candles: list[Candle], tf: str, start_ms: int, end_ms: int) -> tuple[list[SimTrade], int]:
    tf_ms = TF_MS[tf]
    sigs, atr = ema_signal_series(candles)
    n = len(candles)

    def signal(i: int) -> str | None:
        return sigs[i]

    trades: list[SimTrade] = []
    unfinished = 0
    i = 1
    while i < n - 1:
        c = candles[i]
        if c.timestamp < start_ms:
            i += 1
            continue
        if c.timestamp + tf_ms > end_ms:
            break
        side = signal(i)
        if side is None:
            i += 1
            continue
        nxt = candles[i + 1]
        slip = SLIP_BPS_PER_SIDE / 1e4
        entry = nxt.open * (1 + slip) if side == "long" else nxt.open * (1 - slip)
        stop = entry - EMA_PARAMS["stop_atr"] * atr[i] if side == "long" else entry + EMA_PARAMS["stop_atr"] * atr[i]
        exit_raw = exit_ts = reason = None
        j = i + 1
        while j < n:
            cj = candles[j]
            if side == "long" and cj.low <= stop:
                exit_raw, reason = min(stop, cj.open), "stop_loss"
            elif side == "short" and cj.high >= stop:
                exit_raw, reason = max(stop, cj.open), "stop_loss"
            else:
                s2 = signal(j)
                if s2 is not None and s2 != side:
                    exit_raw, reason = cj.close, "crossover_exit"
            if exit_raw is not None:
                exit_ts = cj.timestamp
                break
            j += 1
        if exit_raw is None or exit_ts + tf_ms > end_ms:
            unfinished += 1
            i += 1
            continue
        a_i = atr[i]
        t = _cost_trade(symbol, side, nxt.timestamp, exit_ts, nxt.open, exit_raw, lambda e: e - EMA_PARAMS["stop_atr"] * a_i if side == "long" else e + EMA_PARAMS["stop_atr"] * a_i, reason, tf_ms)
        if t is not None:
            trades.append(t)
        i = max(j, i + 1)
    return trades, unfinished


def simulate_donchian(symbol: str, candles: list[Candle], tf: str, start_ms: int, end_ms: int) -> tuple[list[SimTrade], int]:
    p = DN.DonchianParams()
    tf_ms = TF_MS[tf]
    atr_values = atr_series([c.high for c in candles], [c.low for c in candles], [c.close for c in candles], p.atr_period)
    trades: list[SimTrade] = []
    unfinished = 0
    cooldown_until = 0
    i = DN.min_candles(p) - 1
    n = len(candles)
    while i < n - 1:
        c = candles[i]
        if c.timestamp < start_ms:
            i += 1
            continue
        if c.timestamp + tf_ms > end_ms:
            break
        if c.timestamp + tf_ms < cooldown_until:
            i += 1
            continue
        sig = DN.signal_at(candles, atr_values, i, p, allow_short=True)
        if sig.side is None:
            i += 1
            continue
        nxt = candles[i + 1]
        slip = SLIP_BPS_PER_SIDE / 1e4
        entry = nxt.open * (1 + slip) if sig.side == "long" else nxt.open * (1 - slip)
        stop = DN.initial_stop(entry, sig.side, sig.mid, sig.atr, p)
        if not ((sig.side == "long" and stop < entry) or (sig.side == "short" and stop > entry)):
            i += 1
            continue
        s0 = max(0, i + 1 - (p.exit_period + 1))
        ex = None
        for horizon in (EXIT_SLICE, n):
            e0 = min(n, i + 1 + horizon)
            ex = DN.exit_check(side=sig.side, entry=entry, stop=stop, opened_ms=nxt.timestamp, closed=candles[s0:e0], tf_ms=tf_ms, p=p, atr_values=atr_values[s0:e0])
            if ex is not None or e0 >= n:
                break
        if ex is None or ex.candle_ts + tf_ms > end_ms:
            unfinished += 1
            i += 1
            continue
        sig_atr, sig_mid, side = sig.atr, sig.mid, sig.side
        t = _cost_trade(symbol, side, nxt.timestamp, ex.candle_ts, nxt.open, ex.price, lambda e: DN.initial_stop(e, side, sig_mid, sig_atr, p), ex.reason, tf_ms)
        if t is not None:
            trades.append(t)
        if ex.reason == DN.EXIT_STOP:
            cooldown_until = ex.candle_ts + tf_ms + DONCHIAN_COOLDOWN_MS
        j = i + 1
        while j < n and candles[j].timestamp < ex.candle_ts:
            j += 1
        i = max(j, i + 1)
    return trades, unfinished


def simulate_rsi(symbol: str, candles: list[Candle], tf: str, start_ms: int, end_ms: int) -> tuple[list[SimTrade], int]:
    p = R.RsiParams()
    tf_ms = TF_MS[tf]
    closes = [c.close for c in candles]
    r = rsi_series(closes, p.rsi_period)
    a = adx_series([c.high for c in candles], [c.low for c in candles], closes, p.adx_period)
    t_atr = atr_series([c.high for c in candles], [c.low for c in candles], closes, p.atr_period)
    trades: list[SimTrade] = []
    unfinished = 0
    i = max(R.min_candles(p), 1)
    n = len(candles)
    while i < n - 1:
        c = candles[i]
        if c.timestamp < start_ms:
            i += 1
            continue
        if c.timestamp + tf_ms > end_ms:
            break
        sig = R.signal_from_values(r[i], r[i - 1], a[i], t_atr[i], c.close, p, allow_short=True)
        if sig.side is None:
            i += 1
            continue
        nxt = candles[i + 1]
        slip = SLIP_BPS_PER_SIDE / 1e4
        entry = nxt.open * (1 + slip) if sig.side == "long" else nxt.open * (1 - slip)
        stop = R.stop_price(entry, sig.atr, sig.side, p)
        horizon = min(n, i + 2 + p.time_stop_candles + 1)
        s0 = max(0, i + 1 - RSI_WARMUP)
        ex = R.exit_check(side=sig.side, entry=entry, stop=stop, opened_ms=nxt.timestamp, closed=candles[s0:horizon], tf_ms=tf_ms, p=p)
        if ex is None or ex.candle_ts + tf_ms > end_ms:
            unfinished += 1
            i += 1
            continue
        sig_atr, side = sig.atr, sig.side
        t = _cost_trade(symbol, side, nxt.timestamp, ex.candle_ts, nxt.open, ex.price, lambda e: R.stop_price(e, sig_atr, side, p), ex.reason, tf_ms)
        if t is not None:
            trades.append(t)
        j = i + 1
        while j < n and candles[j].timestamp < ex.candle_ts:
            j += 1
        i = max(j, i + 1)
    return trades, unfinished


SIMULATORS = {"ema": simulate_ema, "donchian": simulate_donchian, "rsi": simulate_rsi}


# ================================================================================ metrics and the gate
def metrics(trades: list[SimTrade], start_ms: int, end_ms: int) -> dict:
    days = max((end_ms - start_ms) / DAY_MS, 1e-9)
    n = len(trades)
    out = {"trades": n, "trades_per_day": n / days}
    if n == 0:
        return {**out, "win_rate": None, "net_expectancy_r": None, "gross_expectancy_r": None, "cost_r_per_trade": None, "cost_share_of_gross": None, "avg_hold_minutes": None,
                "worst_losing_streak": 0, "max_dd_pct": 0.0, "total_net_r": 0.0}
    ordered = sorted(trades, key=lambda x: x.exit_ts)
    net = [x.net_r for x in ordered]
    gross_total = sum(x.gross_r for x in ordered)
    cost_total = gross_total - sum(net)
    streak = worst = 0
    equity, peak, dd = 1.0, 1.0, 0.0
    for x in ordered:
        streak = streak + 1 if x.net_r <= 0 else 0
        worst = max(worst, streak)
        equity *= 1 + RISK_PCT / 100.0 * x.net_r
        peak = max(peak, equity)
        dd = max(dd, (peak - equity) / peak * 100.0)
    return {**out, "win_rate": sum(1 for v in net if v > 0) / n, "net_expectancy_r": sum(net) / n, "gross_expectancy_r": gross_total / n, "cost_r_per_trade": cost_total / n,
            "cost_share_of_gross": (cost_total / gross_total) if gross_total > 0 else None, "avg_hold_minutes": sum(x.hold_ms for x in ordered) / n / 60_000.0,
            "worst_losing_streak": worst, "max_dd_pct": dd, "total_net_r": sum(net)}


def gate(cell: dict) -> dict:
    """The pre-registered pass rule for one cell in one window. Every part must hold."""
    allm = cell["ALL"]
    pairs_positive = sum(1 for s in PAIRS if cell[s]["total_net_r"] > 0)
    parts = {
        "enough_trades": allm["trades"] >= GATE["min_trades"],
        "net_expectancy": allm["net_expectancy_r"] is not None and allm["net_expectancy_r"] >= GATE["min_net_expectancy_r"],
        "costs_leave_the_edge": allm["cost_share_of_gross"] is not None and allm["cost_share_of_gross"] <= GATE["max_cost_share_of_gross"],
        "positive_pairs": pairs_positive >= GATE["min_positive_pairs"],
    }
    return {"parts": parts, "pairs_positive": pairs_positive, "passes": all(parts.values())}


def run_cell(data: dict, strategy: str, tf: str, start_ms: int, end_ms: int) -> dict:
    per, pooled, unfinished = {}, [], 0
    for sym in PAIRS:
        tr, u = SIMULATORS[strategy](sym, data[(sym, tf)], tf, start_ms, end_ms)
        per[sym] = metrics(tr, start_ms, end_ms)
        pooled += tr
        unfinished += u
    per["ALL"] = metrics(pooled, start_ms, end_ms)
    per["unfinished"] = unfinished
    per["by_reason"] = {k: sum(1 for x in pooled if x.reason == k) for k in sorted({x.reason for x in pooled})}
    per["gate"] = gate(per)
    return per


def run_window(data: dict, start_ms: int, end_ms: int, cells: list[tuple[str, str]] | None = None) -> dict:
    todo = cells if cells is not None else [(s, tf) for s in STRATEGIES for tf in TIMEFRAMES]
    return {f"{s}|{tf}": run_cell(data, s, tf, start_ms, end_ms) for s, tf in todo}


# ================================================================================ reports
def _d(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _f(x, spec="+.3f", none="n/a") -> str:
    return none if x is None else format(x, spec)


def report(title: str, window: tuple[int, int], rows: dict, extra: list[str] | None = None) -> str:
    lines = [f"# {title}", "", f"Window {_d(window[0])} to {_d(window[1])} (UTC, end exclusive). Plan hash `{plan_hash()}`. Futures costs: taker fee {FEE_BPS_PER_SIDE:g} bps per side plus {SLIPPAGE_ROUND_TRIP_BPS:g} bps round-trip slippage.",
             "Pairs pooled (BTC, ETH, BNB, XRP) with no slot limit: an UPPER BOUND on how often the bot could trade. Unfinished trades at the window end are not counted. Net = after fees and slippage; gross = before them.", "",
             "| strategy | timeframe | trades | per day | avg hold (min) | win % | gross R | net R | cost per trade (R) | cost share of gross | worst losing streak | max DD % | pairs net-positive | rule |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for key, cell in rows.items():
        s, tf = key.split("|")
        a, g = cell["ALL"], cell["gate"]
        lines.append(f"| {s} | {tf} | {a['trades']} | {a['trades_per_day']:.2f} | {_f(a['avg_hold_minutes'], '.0f')} | {_f(a['win_rate'] and a['win_rate'] * 100, '.1f')} | {_f(a['gross_expectancy_r'])} | {_f(a['net_expectancy_r'])} | "
                     f"{_f(a['cost_r_per_trade'], '.3f')} | {_f(a['cost_share_of_gross'] and a['cost_share_of_gross'] * 100, '.0f')}% | {a['worst_losing_streak']} | {a['max_dd_pct']:.1f} | {g['pairs_positive']} of 4 | "
                     f"{'CLEARS' if g['passes'] else 'does not clear (' + ', '.join(k for k, v in g['parts'].items() if not v) + ')'} |")
    lines += ["", f"The rule (fixed before any run): at least {GATE['min_trades']} trades, net expectancy at least {GATE['min_net_expectancy_r']:+.2f} R per trade, costs at most {GATE['max_cost_share_of_gross'] * 100:.0f}% of the gross edge, "
              f"net-positive on at least {GATE['min_positive_pairs']} of the 4 pairs. A cell with no gross edge cannot clear it."]
    lines += extra or []
    return "\n".join(lines) + "\n"


# ================================================================================ tune, confirm, ledger
class SealedError(Exception):
    pass


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def ledger_path(root: Path) -> Path:
    return Path(root) / "sealed_looks.shortframe.json"


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


def run_tune(root: Path, out_dir: Path, *, expect_plan_hash: str | None = None) -> dict:
    ph = plan_hash()
    if expect_plan_hash and expect_plan_hash != ph:
        raise SealedError(f"plan hash {ph} does not match the registered {expect_plan_hash}: a pinned file or the plan changed")
    dh = data_hash(root)
    data = load_all(root)
    rows = run_window(data, *TUNE)
    qualifying = [k for k, v in rows.items() if v["gate"]["passes"]]
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    res = {"protocol": PROTOCOL, "plan_hash": ph, "data_hash": dh, "created": _stamp(), "tune_window": TUNE, "confirm_window": CONFIRM, "rows": rows, "qualifying": qualifying}
    (out_dir / "tune_results.json").write_text(json.dumps(res, indent=1))
    (out_dir / "selected.json").write_text(json.dumps({"plan_hash": ph, "data_hash": dh, "qualifying": qualifying, "confirm_window": CONFIRM}, indent=1))
    note = ["", f"Cells that clear the rule on the tuning window and therefore go to the confirmation window: {', '.join(qualifying) if qualifying else 'NONE'}."]
    if not qualifying:
        note.append("Nothing clears the rule. The confirmation window stays unread and nothing is tuned.")
    (out_dir / "tune_report.md").write_text(report("Short-timeframe research: tuning window", TUNE, rows, note))
    return res


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
        raise SealedError("data_hash differs from the tuning: the candle files changed")
    if not chosen["qualifying"]:
        res = {"protocol": PROTOCOL, "plan_hash": ph, "data_hash": dh, "opened": False, "rows": {}, "qualifying": []}
        (out_dir / "final_report.md").write_text("# Short-timeframe research: confirmation window\n\nNo cell cleared the rule on the tuning window, so the confirmation window was NOT read (no look was recorded).\n\n"
                                                 "Answer: no timeframe and strategy in this study clears the pre-registered rule after costs.\n")
        return res
    looks = read_looks(root, dh)
    contaminated = bool(looks)
    if looks and not second_look:
        raise SealedError("the confirmation window was already read once; a second look needs --second-look and is labelled contaminated")
    record_look(root, dh, {"ts": _stamp(), "reason": reason.strip(), "plan_hash": ph, "second_look": contaminated})  # BEFORE anything is read
    data = load_all(root)
    cells = [tuple(k.split("|")) for k in chosen["qualifying"]]
    controls = [(s, "1h") for s in STRATEGIES]
    todo = list(dict.fromkeys(cells + controls))
    rows = run_window(data, *CONFIRM, cells=todo)
    cleared = [k for k in chosen["qualifying"] if rows[k]["gate"]["passes"]]
    res = {"protocol": PROTOCOL, "plan_hash": ph, "data_hash": dh, "created": _stamp(), "confirm_window": CONFIRM, "opened": True, "contaminated": contaminated, "rows": rows,
           "qualifying_on_tune": chosen["qualifying"], "cleared": cleared}
    (out_dir / "confirm_results.json").write_text(json.dumps(res, indent=1))
    extra = ["", f"Cells that cleared on the tuning window: {', '.join(chosen['qualifying'])}. Cells that ALSO clear on the confirmation window: {', '.join(cleared) if cleared else 'NONE'}.",
             "The 1h rows are the control (shown whether or not they cleared on tuning)."]
    if contaminated:
        extra.append("THIS IS A SECOND LOOK: it is not unseen evidence.")
    (out_dir / "final_report.md").write_text(report("Short-timeframe research: confirmation window (read once)", CONFIRM, rows, extra))
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mba_bot.shortframe_backtest")
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
            print(json.dumps({k: v for k, v in plan_blob().items() if k != "sources_sha256"}, indent=1, default=str))
            return 0
        if args.cmd == "tune":
            res = run_tune(args.root, args.out, expect_plan_hash=args.expect_plan_hash)
            print(f"tune done; qualifying cells: {res['qualifying'] or 'none'}; report in {args.out}/tune_report.md")
            return 0
        res = run_confirm(args.root, args.out, reason=args.reason, confirm=args.confirm, expect_plan_hash=args.expect_plan_hash, second_look=args.second_look)
        print("confirm done; " + ("window read" if res.get("opened") else "window NOT read (nothing qualified)") + f"; report in {args.out}/final_report.md")
        return 0
    except SealedError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - class name only
        print(f"short-timeframe research failed ({type(exc).__name__})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
