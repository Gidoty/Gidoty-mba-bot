from mba_bot.indicators import ema
from mba_bot.strategy import Candle, SignalType, candles_from_ohlcv, generate_signal

STRATEGY_KWARGS = dict(ema_fast_period=2, ema_slow_period=3, atr_period=2, atr_flat_threshold=0.75, atr_rolling_window=3)


def make_candles(closes, ranges=None):
    ranges = ranges or [1.0] * len(closes)
    return [
        Candle(timestamp=i, open=c, high=c + r, low=c - r, close=c, volume=1.0)
        for i, (c, r) in enumerate(zip(closes, ranges))
    ]


def test_insufficient_history_returns_none():
    signal = generate_signal(make_candles([100, 101]), **STRATEGY_KWARGS)
    assert signal.signal == SignalType.NONE
    assert "insufficient" in signal.reason


def test_flat_prices_produce_no_crossover():
    signal = generate_signal(make_candles([100] * 10), **STRATEGY_KWARGS)
    assert signal.signal == SignalType.NONE
    assert signal.reason == "no crossover"


def find_crossover_index(closes, *, fast_period, slow_period, direction):
    """Locate the first index where a fast/slow EMA crossover happens, so
    tests can build a candle window that ends exactly on the crossover
    instead of guessing where one falls in a hand-picked price series."""
    fast = ema(closes, fast_period)
    slow = ema(closes, slow_period)
    for i in range(1, len(closes)):
        if None in (fast[i - 1], slow[i - 1], fast[i], slow[i]):
            continue
        if direction == "up" and fast[i - 1] <= slow[i - 1] and fast[i] > slow[i]:
            return i
        if direction == "down" and fast[i - 1] >= slow[i - 1] and fast[i] < slow[i]:
            return i
    raise AssertionError(f"no {direction} crossover found in series")


def test_bullish_crossover_detected():
    closes = [100, 95, 90, 85, 80, 75, 70, 65, 60, 58, 90, 120, 150]
    idx = find_crossover_index(closes, fast_period=2, slow_period=3, direction="up")
    candles = make_candles(closes[: idx + 1], ranges=[5.0] * (idx + 1))

    signal = generate_signal(candles, **STRATEGY_KWARGS)
    assert signal.signal == SignalType.BULLISH_CROSSOVER


def test_bearish_crossover_detected():
    closes = [60, 65, 70, 75, 80, 85, 90, 95, 100, 102, 70, 40, 10]
    idx = find_crossover_index(closes, fast_period=2, slow_period=3, direction="down")
    candles = make_candles(closes[: idx + 1], ranges=[5.0] * (idx + 1))

    signal = generate_signal(candles, **STRATEGY_KWARGS)
    assert signal.signal == SignalType.BEARISH_CROSSOVER


def test_flat_market_suppresses_a_real_crossover():
    closes = [100, 95, 90, 85, 80, 75, 70, 65, 60, 58, 90, 120, 150]
    idx = find_crossover_index(closes, fast_period=2, slow_period=3, direction="up")
    candles = make_candles(closes[: idx + 1], ranges=[5.0] * (idx + 1))

    # Sanity: this exact window produces a real crossover under a normal threshold.
    assert generate_signal(candles, **STRATEGY_KWARGS).signal == SignalType.BULLISH_CROSSOVER

    # An impossibly strict threshold (current ATR would need to be 100x its
    # own rolling average) always suppresses, proving the volatility filter
    # actually gates the otherwise-valid crossover confirmed above.
    signal = generate_signal(candles, **{**STRATEGY_KWARGS, "atr_flat_threshold": 100.0})
    assert signal.signal == SignalType.NONE
    assert "flat" in signal.reason


def test_candles_from_ohlcv_maps_ccxt_row_order():
    ohlcv = [[1700000000000, 10.0, 12.0, 9.0, 11.0, 100.0]]
    candles = candles_from_ohlcv(ohlcv)
    assert candles == [Candle(timestamp=1700000000000, open=10.0, high=12.0, low=9.0, close=11.0, volume=100.0)]
