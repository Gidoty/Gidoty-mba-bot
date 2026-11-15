"""rsi_mr_v1: indicators (RSI, ADX), the pure signal and exit rules (src/mba_bot/rsi_mr.py, docs/safety/rsi_mr_v1.md).

PROVES: RSI against the published Wilder worked example (StockCharts RSI table), ADX against an independent straight-from-the-definition
implementation and its basic properties, every signal rule (long, short, no signal, each filter, spot long-only, forming candle ignored)
and every exit rule. DOES NOT PROVE: that the strategy makes money. Nothing here is a backtest."""
from __future__ import annotations

import math
import random

import pytest

from mba_bot import rsi_mr as R
from mba_bot.indicators import adx, atr, rsi
from mba_bot.strategy import Candle

HOUR = 3_600_000

# The StockCharts RSI worked example (14-period, Wilder smoothing): closes and the published RSI column.
SC_CLOSES = [44.3389, 44.0902, 44.1497, 43.6124, 44.3278, 44.8264, 45.0955, 45.4245, 45.8433, 46.0826, 45.8931, 46.0328, 45.6140, 46.2820, 46.2820,
             46.0028, 46.0328, 46.4116, 46.2222, 45.6439, 46.2122, 46.2521, 45.7137, 46.4515, 45.7835, 45.3548, 44.0288, 44.1783, 44.2181, 44.5672,
             43.4205, 42.6628, 43.1314]
SC_RSI = [70.53, 66.32, 66.55, 69.41, 66.36, 57.97, 62.93, 63.26, 56.06, 62.38, 54.71, 50.42, 39.99, 41.46, 41.87, 45.46, 37.30, 33.08, 37.77]


def mk(closes, spread=0.4, start=0):
    out = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        out.append(Candle((start + i) * HOUR, o, max(o, c) + spread, min(o, c) - spread, c, 1.0))
    return out


# Hand-checked fixtures: mean-reverting noise (seeded once, embedded as literals so they cannot drift).
LONG = [98.86, 98.13, 99.35, 100.17, 100.02, 99.61, 101.49, 101.14, 101.79, 100.56, 101.07, 101.03, 104.06, 103.4, 102.57, 101.7, 100.62, 102.17, 100.5,
        100.34, 99.47, 97.71, 98.11, 98.27, 99.1, 100.2, 101.17, 101.74, 100.84, 100.39, 101.09, 100.78, 101.61, 99.37, 98.66, 100.9, 99.11, 98.58, 99.12,
        99.19, 97.13, 98.57, 98.71, 98.84, 98.0, 99.29, 100.58, 99.94, 100.82, 101.63, 100.43, 100.34, 101.14, 100.84, 102.12, 102.05, 100.03, 99.52, 99.09,
        98.85, 99.03, 99.36, 97.93, 97.51, 96.83, 96.18, 93.97, 96.24]  # RSI 27.9 -> 40.3, ADX 23.2: a long signal on the last candle
SHORT = [99.07, 100.26, 99.78, 98.86, 98.38, 98.77, 98.51, 98.23, 99.99, 100.08, 98.18, 98.39, 99.19, 97.89, 97.37, 97.35, 97.39, 97.79, 99.31, 98.94, 99.51,
         99.21, 99.46, 99.58, 99.7, 100.47, 102.11, 101.23, 101.1, 99.9, 100.47, 99.71, 99.86, 99.63, 101.93, 101.63, 101.69, 100.12, 99.05, 99.24, 98.65,
         96.84, 98.59, 98.55, 97.95, 99.41, 99.82, 100.66, 101.04, 101.84, 101.21, 100.66, 101.12, 101.93, 103.55, 105.46, 103.74]  # RSI 70.8 -> 61.4, ADX 21.5
TRENDY = [98.88, 99.54, 98.83, 100.4, 101.04, 103.43, 102.67, 102.86, 101.02, 99.4, 99.56, 100.38, 99.21, 99.58, 99.69, 100.59, 99.56, 99.75, 99.26, 98.76,
          98.75, 99.47, 99.15, 99.76, 99.8, 101.92, 101.67, 101.36, 100.59, 100.33, 101.41, 101.11, 101.77, 100.76, 101.41, 101.72, 101.21, 101.27, 100.91,
          100.57, 100.71, 101.57, 101.24, 101.5, 100.07, 99.8, 98.6, 97.49, 96.34, 97.39]  # RSI 29.7 -> 37.1 but ADX 25.8: blocked


# ====================================================================== RSI
def test_rsi_matches_the_published_wilder_example():
    out = [v for v in rsi(SC_CLOSES, 14) if v is not None]
    assert len(out) == len(SC_RSI)
    for got, want in zip(out, SC_RSI):
        assert round(got, 2) == want


def test_rsi_shape_padding_and_extremes():
    r = rsi(SC_CLOSES, 14)
    assert len(r) == len(SC_CLOSES) and r[:14] == [None] * 14 and r[14] is not None
    assert rsi([1.0] * 5, 14) == [None] * 5
    assert rsi([float(i) for i in range(1, 30)], 14)[-1] == 100.0  # only gains
    assert rsi([float(i) for i in range(30, 1, -1)], 14)[-1] == 0.0  # only losses
    assert rsi([5.0] * 30, 14)[-1] == 50.0  # nothing moved
    with pytest.raises(ValueError):
        rsi([1.0] * 30, 0)


def test_rsi_stays_between_0_and_100_on_random_data():
    rng = random.Random(3)
    x, closes = 100.0, []
    for _ in range(300):
        x = max(1.0, x + rng.gauss(0, 2))
        closes.append(x)
    assert all(0.0 <= v <= 100.0 for v in rsi(closes, 14) if v is not None)


# ====================================================================== ADX
def adx_reference(h, l, c, n=14):
    """Wilder's ADX written the long way from the definition (running sums, plain loops), to compare with the production code."""
    tr, pdm, mdm = [], [], []
    for i in range(1, len(c)):
        tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
        up, dn = h[i] - h[i - 1], l[i - 1] - l[i]
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
    out = [None] * len(c)
    atr_s, p_s, m_s = sum(tr[:n]), sum(pdm[:n]), sum(mdm[:n])
    dxs = []
    for i in range(n, len(tr) + 1):
        if i > n:
            atr_s = atr_s - atr_s / n + tr[i - 1]
            p_s = p_s - p_s / n + pdm[i - 1]
            m_s = m_s - m_s / n + mdm[i - 1]
        pdi, mdi = 100 * p_s / atr_s, 100 * m_s / atr_s
        dxs.append(100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else 0.0)
    a = sum(dxs[:n]) / n
    out[2 * n - 1] = a
    for k in range(n, len(dxs)):
        a = (a * (n - 1) + dxs[k]) / n
        out[n + k] = a
    return out


def test_adx_matches_an_independent_implementation():
    rng = random.Random(11)
    x, h, l, c = 100.0, [], [], []
    for _ in range(200):
        x += rng.gauss(0.05, 1.0)
        h.append(x + abs(rng.gauss(0, 0.5))); l.append(x - abs(rng.gauss(0, 0.5))); c.append(x)
    got, want = adx(h, l, c, 14), adx_reference(h, l, c, 14)
    assert got[:27] == [None] * 27 and got[27] is not None  # first value at index 2 * period - 1
    for g, w in zip(got, want):
        assert (g is None) == (w is None)
        if g is not None:
            assert math.isclose(g, w, rel_tol=1e-9, abs_tol=1e-9)


def test_adx_is_high_in_a_trend_and_low_in_a_range():
    up = [100.0 + i for i in range(80)]
    assert adx([v + 0.3 for v in up], [v - 0.3 for v in up], up, 14)[-1] > 60  # a steady trend
    rng = random.Random(5)
    flat = [100 + rng.gauss(0, 0.5) for _ in range(120)]
    assert adx([v + 0.3 for v in flat], [v - 0.3 for v in flat], flat, 14)[-1] < 25  # noise around a level
    for v in adx([v + 0.3 for v in up], [v - 0.3 for v in up], up, 14):
        assert v is None or 0.0 <= v <= 100.0


def test_adx_edge_cases():
    assert adx([1.0] * 10, [1.0] * 10, [1.0] * 10, 14) == [None] * 10
    assert adx([5.0] * 60, [5.0] * 60, [5.0] * 60, 14)[-1] == 0.0  # nothing moves: no trend, no division by zero
    with pytest.raises(ValueError):
        adx([1.0], [1.0, 2.0], [1.0], 14)
    with pytest.raises(ValueError):
        adx([1.0] * 40, [1.0] * 40, [1.0] * 40, 0)


# ====================================================================== signal
def test_long_signal_and_its_numbers():
    s = R.generate_signal(mk(LONG))
    assert s.side == "long" and s.prev_rsi < 30 < s.rsi and s.adx < 25 and s.atr > 0 and s.target_atr == 1.0
    assert 0 < s.atr_pct < 0.1


def test_short_signal_only_when_shorts_are_allowed():
    s = R.generate_signal(mk(SHORT), allow_short=True)
    assert s.side == "short" and s.prev_rsi > 70 > s.rsi and s.adx < 25
    spot = R.generate_signal(mk(SHORT), allow_short=False)
    assert spot.side is None and "long-only" in spot.reason


def test_no_cross_means_no_signal():
    s = R.generate_signal(mk(LONG[:-1]))
    assert s.side is None and s.reason == "no RSI cross" and s.rsi < 30


def test_the_adx_filter_blocks_a_cross_in_a_trending_market():
    s = R.generate_signal(mk(TRENDY))
    assert s.side is None and "ADX" in s.reason and s.prev_rsi < 30 < s.rsi and s.adx >= 25
    assert R.generate_signal(mk(TRENDY), R.RsiParams(adx_max=30.0)).side == "long"  # the limit is the only difference


def test_the_adx_limit_is_strict():
    s = R.generate_signal(mk(LONG))
    just_at = R.RsiParams(adx_max=s.adx)
    assert R.generate_signal(mk(LONG), just_at).side is None  # ADX equal to the limit is not below it
    assert R.generate_signal(mk(LONG), R.RsiParams(adx_max=s.adx + 1e-9)).side == "long"


def test_insufficient_history_and_unavailable_indicators_give_no_signal():
    assert R.generate_signal(mk(LONG[:20])).side is None and "insufficient" in R.generate_signal(mk(LONG[:20])).reason
    flat = mk([100.0] * 80, spread=0.0)
    s = R.generate_signal(flat)
    assert s.side is None  # zero ATR: nothing can be sized, nothing is traded


def test_the_forming_candle_is_ignored():
    candles = mk(LONG)
    tf = HOUR
    last_open = candles[-1].timestamp
    # "now" is inside the last candle: it is still forming and must be dropped
    closed = R.closed_only(candles, last_open + tf // 2, tf)
    assert len(closed) == len(candles) - 1 and closed[-1] is candles[-2]
    assert R.generate_signal(closed).side is None  # the cross only exists on the forming candle
    # once that candle has closed it counts
    assert len(R.closed_only(candles, last_open + tf, tf)) == len(candles)
    assert R.generate_signal(R.closed_only(candles, last_open + tf, tf)).side == "long"


def test_timeframe_parsing_is_safe():
    assert R.timeframe_ms("1h") == HOUR and R.timeframe_ms("15m") == 900_000 and R.timeframe_ms("4H") == 4 * HOUR and R.timeframe_ms("1d") == 86_400_000
    for bad in ("", "x", "0h", "-1h", "1w", None, "1h30m"):
        assert R.timeframe_ms(bad) == HOUR


# ====================================================================== stop, target and exits
def test_stop_and_target_prices_follow_the_atr():
    assert R.stop_price(100.0, 2.0, "long") == 97.0 and R.stop_price(100.0, 2.0, "short") == 103.0
    assert math.isclose(R.entry_atr_from_stop(100.0, 97.0), 2.0)
    assert math.isclose(R.target_price(100.0, 97.0, "long"), 102.0) and math.isclose(R.target_price(100.0, 103.0, "short"), 98.0)


def walk(side, rows, *, entry=100.0, stop=97.0, opened=0, p=R.RsiParams(), pre=None):
    """rows = [(open, high, low, close)] for the candles AFTER the entry (the first one starts at the entry time)."""
    base = pre if pre is not None else [100.0] * 30
    cs = [Candle(i * HOUR, v, v + 0.1, v - 0.1, v, 1.0) for i, v in enumerate(base)]
    start = len(cs)
    cs += [Candle((start + i) * HOUR, o, h, l, c, 1.0) for i, (o, h, l, c) in enumerate(rows)]
    return R.exit_check(side=side, entry=entry, stop=stop, opened_ms=start * HOUR, closed=cs, tf_ms=HOUR, p=p)


def test_exit_stop_loss_fills_at_the_stop():
    ex = walk("long", [(100, 100.5, 99.0, 99.5), (99.5, 99.6, 96.5, 97.0)])
    assert ex.reason == R.EXIT_STOP and ex.price == 97.0
    rising = [90 + 0.4 * i for i in range(30)]  # a short is entered after a rise: RSI is high, not at the 50 line
    e = rising[-1]
    ex = walk("short", [(e, e + 0.5, e - 0.2, e + 0.2), (e + 0.2, e + 3.6, e + 0.1, e + 3.0)], entry=e, stop=e + 3.0, pre=rising)
    assert ex.reason == R.EXIT_STOP and ex.price == e + 3.0


def test_exit_profit_target_at_one_atr():
    ex = walk("long", [(100, 102.5, 99.5, 102.0)])  # ATR 2 -> target 102
    assert ex.reason == R.EXIT_TARGET and math.isclose(ex.price, 102.0)
    rising = [90 + 0.4 * i for i in range(30)]
    e = rising[-1]
    ex = walk("short", [(e, e + 0.5, e - 2.5, e - 2.0)], entry=e, stop=e + 3.0, pre=rising)  # 1.5 ATR stop = 3.0 -> ATR 2.0 -> target e - 2.0
    assert ex.reason == R.EXIT_TARGET and math.isclose(ex.price, e - 2.0)


def test_when_one_candle_touches_both_the_stop_wins():
    ex = walk("long", [(100, 103.0, 96.0, 100.0)])
    assert ex.reason == R.EXIT_STOP


def test_exit_when_rsi_returns_to_the_line_at_the_close():
    pre = [100 - 0.5 * i for i in range(30)]  # a long fall: RSI near 0
    entry = pre[-1]
    rows = []
    price = entry
    for _ in range(12):  # slow recovery, never touching the target (1 ATR) or the stop
        o, price = price, price + 0.4
        rows.append((o, price + 0.05, o - 0.05, price))
    ex = walk("long", rows, entry=entry, stop=entry - 3.0, pre=pre, p=R.RsiParams(target_atr=50.0, time_stop_candles=100))
    assert ex.reason == R.EXIT_RSI
    closes = pre + [r[3] for r in rows]
    from mba_bot.indicators import rsi as _rsi
    r = _rsi(closes, 14)
    first = next(i for i in range(len(pre), len(closes)) if r[i] >= 50.0)
    assert ex.price == closes[first]  # at the CLOSE of the first candle whose RSI is at 50 or more


def test_exit_after_24_candles():
    rows = [(100, 100.1, 99.9, 100.0)] * 24
    ex = walk("long", rows)
    assert ex.reason in (R.EXIT_TIME, R.EXIT_RSI)
    assert walk("long", rows[:23], p=R.RsiParams(exit_rsi=1000.0)) is None
    ex = walk("long", rows, p=R.RsiParams(exit_rsi=1000.0))
    assert ex.reason == R.EXIT_TIME and ex.price == 100.0
    assert walk("long", rows[:5], p=R.RsiParams(time_stop_candles=5, exit_rsi=1000.0)).reason == R.EXIT_TIME


def test_candles_that_ended_before_the_entry_are_ignored_and_no_event_means_none():
    cs = [Candle(i * HOUR, 100, 100.1, 90.0, 100, 1.0) for i in range(40)]  # old candles with a huge wick down
    assert R.exit_check(side="long", entry=100.0, stop=97.0, opened_ms=39 * HOUR + HOUR, closed=cs, tf_ms=HOUR) is None
    assert R.exit_check(side="long", entry=100.0, stop=97.0, opened_ms=0, closed=[], tf_ms=HOUR) is None


def test_staying_beyond_the_line_is_not_a_cross():
    below = LONG[:-1] + [LONG[-2] - 1.0]  # RSI was 27.9 and falls further: still below 30, no cross back
    s = R.generate_signal(mk(below))
    assert s.side is None and s.reason == "no RSI cross" and s.prev_rsi < 30 and s.rsi < 30
    above = SHORT[:-1] + [SHORT[-2] + 1.0]  # RSI was 70.8 and rises further
    s = R.generate_signal(mk(above), allow_short=True)
    assert s.side is None and s.reason == "no RSI cross" and s.prev_rsi > 70 and s.rsi > 70


def test_the_decision_function_refuses_missing_values():
    ok = dict(now_rsi=35.0, prev_rsi=25.0, now_adx=20.0, now_atr=1.0, close=100.0)
    assert R.signal_from_values(**ok).side == "long"
    for k in ("now_rsi", "prev_rsi", "now_adx", "now_atr"):
        s = R.signal_from_values(**{**ok, k: None})
        assert s.side is None and s.reason == "indicator not available", k
    assert R.signal_from_values(**{**ok, "close": 0.0}).side is None and R.signal_from_values(**{**ok, "now_atr": 0.0}).side is None
