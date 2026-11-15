"""rsi_mr_v1: RSI mean reversion, PAPER ONLY. Pure functions: no I/O, no clock, no database.

Rules (docs/safety/rsi_mr_v1.md). Closed candles only; the forming candle is dropped before anything is computed.
* Long: RSI(14) was below 30 on the previous closed candle and is above 30 on the last closed candle.
* Short (Futures only): RSI(14) was above 70 and is below 70. Spot is long only.
* Range filter: trade only when ADX(14) is below 25. A missing ADX or ATR means no signal.
* Stop: 1.5 x ATR(14) from entry. Exit: RSI back to 50, or 1.0 x ATR of profit, whichever comes first. Time stop after 24 candles.

Nothing here is tuned on data. The numbers are the specification; the backtest runner (rsi_backtest.py) measures them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .indicators import adx as adx_series
from .indicators import atr as atr_series
from .indicators import rsi as rsi_series

STRATEGY_ID = "rsi_mr_v1"

# Exit reasons written to trades.close_reason
EXIT_STOP = "stop_loss"
EXIT_TARGET = "profit_target"
EXIT_RSI = "rsi_exit"
EXIT_TIME = "time_stop"


@dataclass(frozen=True)
class RsiParams:
    rsi_period: int = 14
    oversold: float = 30.0
    overbought: float = 70.0
    adx_period: int = 14
    adx_max: float = 25.0
    atr_period: int = 14
    stop_atr: float = 1.5
    target_atr: float = 1.0
    exit_rsi: float = 50.0
    time_stop_candles: int = 24


@dataclass(frozen=True)
class RsiSignal:
    side: str | None  # "long" | "short" | None
    reason: str
    rsi: float | None = None
    prev_rsi: float | None = None
    adx: float | None = None
    atr: float | None = None
    atr_pct: float | None = None
    target_atr: float | None = None  # expected gross move in ATRs, read by the fee guard
    gap_atr: float | None = None  # not used by this strategy; the guards read it for the EMA gap rule only


_TF = re.compile(r"^(\d{1,3})([mhd])$")
_UNIT_MS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}


def timeframe_ms(timeframe: str, default: int = 3_600_000) -> int:
    """'1h' -> 3600000. Anything unrecognised is the 1h default (never zero)."""
    m = _TF.match((timeframe or "").strip().lower())
    if not m or int(m.group(1)) <= 0:
        return default
    return int(m.group(1)) * _UNIT_MS[m.group(2)]


def closed_only(candles, now_ms: int, tf_ms: int) -> list:
    """Drop the candle that is still forming: one whose open time plus its length is after `now`."""
    return [c for c in candles if c.timestamp + tf_ms <= now_ms]


def min_candles(p: RsiParams = RsiParams()) -> int:
    return max(2 * p.adx_period + 1, p.rsi_period + 2, p.atr_period + 2)


def signal_from_values(now_rsi, prev_rsi, now_adx, now_atr, close, p: RsiParams = RsiParams(), *, allow_short: bool = False) -> RsiSignal:
    """The decision itself, from already computed indicator values at the last closed candle. generate_signal (live) and the backtest
    runner both call this, so there is one rule, not two."""
    if now_rsi is None or prev_rsi is None or now_adx is None or now_atr is None or not close or not now_atr > 0:
        return RsiSignal(None, "indicator not available")
    atr_pct = now_atr / close
    common = dict(rsi=now_rsi, prev_rsi=prev_rsi, adx=now_adx, atr=now_atr, atr_pct=atr_pct, target_atr=p.target_atr)
    crossed_up = prev_rsi < p.oversold and now_rsi > p.oversold
    crossed_down = prev_rsi > p.overbought and now_rsi < p.overbought
    if not (crossed_up or crossed_down):
        return RsiSignal(None, "no RSI cross", **common)
    if not now_adx < p.adx_max:
        return RsiSignal(None, "trending market: ADX not below the limit", **common)
    if crossed_up:
        return RsiSignal("long", "RSI crossed back above the oversold line", **common)
    if not allow_short:
        return RsiSignal(None, "short signal on a long-only market", **common)
    return RsiSignal("short", "RSI crossed back below the overbought line", **common)


def generate_signal(closed: list, p: RsiParams = RsiParams(), *, allow_short: bool = False) -> RsiSignal:
    """The strategy's verdict on the last CLOSED candle. `closed` must already exclude the forming candle (see closed_only)."""
    if len(closed) < min_candles(p):
        return RsiSignal(None, "insufficient candle history")
    closes = [c.close for c in closed]
    highs = [c.high for c in closed]
    lows = [c.low for c in closed]
    r = rsi_series(closes, p.rsi_period)
    a = adx_series(highs, lows, closes, p.adx_period)
    t = atr_series(highs, lows, closes, p.atr_period)
    return signal_from_values(r[-1], r[-2], a[-1], t[-1], closes[-1], p, allow_short=allow_short)


def stop_price(entry: float, atr: float, side: str, p: RsiParams = RsiParams()) -> float:
    return entry - p.stop_atr * atr if side == "long" else entry + p.stop_atr * atr


def entry_atr_from_stop(entry: float, stop: float, p: RsiParams = RsiParams()) -> float:
    """The ATR the trade was opened with, recovered from its stored stop (stop distance = stop_atr x ATR). Nothing extra is stored."""
    return abs(entry - stop) / p.stop_atr


def target_price(entry: float, stop: float, side: str, p: RsiParams = RsiParams()) -> float:
    d = p.target_atr * entry_atr_from_stop(entry, stop, p)
    return entry + d if side == "long" else entry - d


@dataclass(frozen=True)
class Exit:
    reason: str
    price: float
    candle_ts: int


def exit_check(*, side: str, entry: float, stop: float, opened_ms: int, closed: list, tf_ms: int, p: RsiParams = RsiParams()) -> Exit | None:
    """Walk the closed candles that started at or after the entry, oldest first, and return the FIRST exit event, or None.

    Per candle: the stop (low/high through it) first, then the profit target, then at the close the RSI line, then the time stop.
    When a candle touches both the stop and the target the stop wins (the conservative reading). The result depends only on the
    candles, never on when the cycle happened to run. Stops and targets fill at their price; the other exits at the candle close.
    """
    if not closed:
        return None
    closes = [c.close for c in closed]
    r = rsi_series(closes, p.rsi_period)
    target = target_price(entry, stop, side, p)
    n = 0
    for i, c in enumerate(closed):
        if c.timestamp < opened_ms:
            continue  # started before the entry: the candle the entry happened in is not judged (the part before the entry never was ours)
        n += 1
        if side == "long":
            if c.low <= stop:
                return Exit(EXIT_STOP, stop, c.timestamp)
            if c.high >= target:
                return Exit(EXIT_TARGET, target, c.timestamp)
            line_hit = r[i] is not None and r[i] >= p.exit_rsi
        else:
            if c.high >= stop:
                return Exit(EXIT_STOP, stop, c.timestamp)
            if c.low <= target:
                return Exit(EXIT_TARGET, target, c.timestamp)
            line_hit = r[i] is not None and r[i] <= p.exit_rsi
        if line_hit:
            return Exit(EXIT_RSI, c.close, c.timestamp)
        if n >= p.time_stop_candles:
            return Exit(EXIT_TIME, c.close, c.timestamp)
    return None
