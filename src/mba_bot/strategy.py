"""EMA(20/50) crossover strategy with an ATR-based volatility filter.

Deliberately stateless: given a window of candles it answers "what does the
indicator say right now", nothing more. Whether that becomes an order (and
which side - long vs short - is even allowed) is a position/market-type
decision made by `trader.py`, not by this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .indicators import atr, ema


@dataclass(frozen=True)
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def candles_from_ohlcv(ohlcv: list[list[float]]) -> list[Candle]:
    """Convert ccxt's raw `[ts, open, high, low, close, volume]` rows."""
    return [Candle(timestamp=row[0], open=row[1], high=row[2], low=row[3], close=row[4], volume=row[5]) for row in ohlcv]


class SignalType(Enum):
    BULLISH_CROSSOVER = "bullish_crossover"
    BEARISH_CROSSOVER = "bearish_crossover"
    NONE = "none"


@dataclass(frozen=True)
class StrategySignal:
    signal: SignalType
    reason: str
    fast_ema: float | None
    slow_ema: float | None
    atr: float | None
    atr_pct: float | None


def generate_signal(
    candles: list[Candle],
    *,
    ema_fast_period: int,
    ema_slow_period: int,
    atr_period: int,
    atr_flat_threshold: float,
    atr_rolling_window: int,
) -> StrategySignal:
    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]

    fast = ema(closes, ema_fast_period)
    slow = ema(closes, ema_slow_period)
    atr_values = atr(highs, lows, closes, atr_period)

    if (
        len(candles) < 2
        or fast[-1] is None
        or fast[-2] is None
        or slow[-1] is None
        or slow[-2] is None
        or atr_values[-1] is None
    ):
        return StrategySignal(SignalType.NONE, "insufficient candle history", None, None, None, None)

    prev_fast, curr_fast = fast[-2], fast[-1]
    prev_slow, curr_slow = slow[-2], slow[-1]
    curr_atr = atr_values[-1]
    curr_close = closes[-1]
    atr_pct = (curr_atr / curr_close) if curr_close else 0.0

    crossed_up = prev_fast <= prev_slow and curr_fast > curr_slow
    crossed_down = prev_fast >= prev_slow and curr_fast < curr_slow

    if not (crossed_up or crossed_down):
        return StrategySignal(SignalType.NONE, "no crossover", curr_fast, curr_slow, curr_atr, atr_pct)

    window = atr_values[-atr_rolling_window:]
    window_closes = closes[-atr_rolling_window:]
    window_atr_pct = [a / c for a, c in zip(window, window_closes) if a is not None and c]
    rolling_avg_atr_pct = sum(window_atr_pct) / len(window_atr_pct) if window_atr_pct else 0.0

    if rolling_avg_atr_pct > 0 and atr_pct < rolling_avg_atr_pct * atr_flat_threshold:
        return StrategySignal(
            SignalType.NONE,
            "flat/range-bound market: ATR% below rolling threshold",
            curr_fast,
            curr_slow,
            curr_atr,
            atr_pct,
        )

    if crossed_up:
        return StrategySignal(SignalType.BULLISH_CROSSOVER, "EMA fast crossed above slow", curr_fast, curr_slow, curr_atr, atr_pct)
    return StrategySignal(SignalType.BEARISH_CROSSOVER, "EMA fast crossed below slow", curr_fast, curr_slow, curr_atr, atr_pct)
