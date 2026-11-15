"""donchian_breakout_v1: the pure channel, volume, signal, stop and exit rules (src/mba_bot/donchian.py, docs/safety/donchian_breakout_v1.md).

PROVES: the channel, volume and ATR numbers against hand-built values, every signal rule (long, short, Spot short refused, wick only, no volume,
extreme ATR, the breakout candle itself excluded from its own channel, forming candle ignored), the stop choice and every exit rule.
DOES NOT PROVE: that the strategy makes money. Nothing here is a backtest."""
from __future__ import annotations

import math

import pytest

from mba_bot import donchian as D
from mba_bot import rsi_mr
from mba_bot.indicators import atr
from mba_bot.strategy import Candle

HOUR = 3_600_000


def cd(i, o, h, l, c, v=100.0):
    return Candle(i * HOUR, o, h, l, c, v)


def base(n=40, *, high=101.0, low=99.0, close=100.0, vol=100.0):
    """n flat candles: every true range is exactly high - low, so ATR = high - low, the 20-candle channel is [low, high] and its midline their mean."""
    return [cd(i, close, high, low, close, vol) for i in range(n)]


def with_last(candle_row, n=40, **kw):
    cs = base(n, **kw)
    o, h, l, c, v = candle_row
    cs.append(cd(n, o, h, l, c, v))
    return cs


# ====================================================================== the numbers, by hand
def test_channel_volume_and_atr_against_hand_values():
    cs = with_last((100.0, 103.5, 100.5, 103.0, 200.0))
    s = D.signal_from_candles(cs)
    assert (s.upper, s.lower, s.mid) == (101.0, 99.0, 100.0)  # the 20 candles BEFORE the breakout candle
    assert s.avg_volume == 100.0 and s.volume == 200.0
    # breakout candle true range = max(103.5 - 100.5, |103.5 - 100|, |100.5 - 100|) = 3.5; Wilder ATR = (13 x 2 + 3.5) / 14
    want_atr = (13 * 2.0 + 3.5) / 14
    assert math.isclose(s.atr, want_atr) and math.isclose(s.atr_pct, want_atr / 103.0)
    assert math.isclose(s.atr_pct_avg, 2.0 / 100.0)  # the 20 candles before the breakout candle: ATR 2 on a price of 100
    assert s.side == "long"


def test_channel_function_is_highest_high_and_lowest_low():
    cs = [cd(0, 1, 5, 1, 2), cd(1, 2, 9, 0.5, 3), cd(2, 3, 4, 2, 3)]
    assert D.channel(cs, 3) == (9, 0.5)


def test_the_breakout_candle_is_not_part_of_its_own_channel_or_average():
    s = D.signal_from_candles(with_last((100.0, 500.0, 100.5, 103.0, 10_000.0)))  # a huge high and a huge volume on the breakout candle
    assert s.upper == 101.0 and s.avg_volume == 100.0


def test_the_channel_is_the_previous_twenty_not_more_not_less():
    cs = base(40)
    cs[19] = cd(19, 100, 150.0, 99, 100)  # 21 candles before the breakout candle (index 40): outside the channel
    cs[20] = cd(20, 100, 101.0, 99, 100)
    s = D.signal_from_candles(cs + [cd(40, 100.0, 103.5, 100.5, 103.0, 200.0)])
    assert s.upper == 101.0 and s.side == "long"
    cs[20] = cd(20, 100, 150.0, 99, 100)  # now it IS the 20th candle before
    s = D.signal_from_candles(cs + [cd(40, 100.0, 103.5, 100.5, 103.0, 200.0)])
    assert s.upper == 150.0 and s.side is None


# ====================================================================== signals
def test_long_signal_and_the_midline_stop():
    s = D.signal_from_candles(with_last((100.0, 103.5, 100.5, 103.0, 200.0)))
    assert s.side == "long" and s.reason == "close beyond the channel with volume"
    # midline 100 vs 103 - 1.5 x 2.107 = 99.84: the midline is closer to entry
    assert s.stop == 100.0


def test_the_atr_stop_is_used_when_it_is_closer_than_the_midline():
    cs = base(40, high=111.0, low=89.0, close=100.0)  # wide channel: midline 100, ATR 22
    cs.append(cd(40, 100.0, 113.0, 105.0, 112.0, 200.0))
    s = D.signal_from_candles(cs)
    assert s.side == "long" and s.mid == 100.0
    assert math.isclose(s.stop, 112.0 - 1.5 * s.atr) or s.stop == 100.0
    assert s.stop == max(100.0, 112.0 - 1.5 * s.atr)  # whichever is closer to entry
    assert D.initial_stop(112.0, "long", 100.0, 2.0) == 109.0 and D.initial_stop(112.0, "long", 110.5, 2.0) == 110.5
    assert D.initial_stop(88.0, "short", 100.0, 2.0) == 91.0 and D.initial_stop(88.0, "short", 89.5, 2.0) == 89.5


def test_short_signal_only_when_shorts_are_allowed():
    cs = with_last((100.0, 99.5, 96.5, 97.0, 200.0))
    s = D.signal_from_candles(cs, allow_short=True)
    assert s.side == "short" and s.stop == 100.0 and s.lower == 99.0
    spot = D.signal_from_candles(cs, allow_short=False)
    assert spot.side is None and "long-only" in spot.reason


def test_the_close_must_be_beyond_the_channel_strictly():
    assert D.signal_from_candles(with_last((100.0, 101.5, 100.0, 101.0, 200.0))).side is None  # close == upper
    assert D.signal_from_candles(with_last((100.0, 101.5, 100.0, 101.01, 200.0))).side == "long"


def test_a_wick_beyond_the_channel_does_not_count():
    s = D.signal_from_candles(with_last((100.0, 104.0, 100.0, 100.5, 300.0)))  # high 104 > 101, but it closed back inside
    assert s.side is None and "wick" in s.reason
    s = D.signal_from_candles(with_last((100.0, 100.5, 95.0, 99.5, 300.0)), allow_short=True)  # low through the lower band, closed inside
    assert s.side is None and "wick" in s.reason
    assert D.signal_from_candles(with_last((100.0, 100.5, 99.5, 100.0, 300.0))).reason == "no breakout"


@pytest.mark.parametrize("volume,fires", [(200.0, True), (150.01, True), (150.0, False), (149.99, False), (100.0, False), (0.0, False)])
def test_volume_must_exceed_one_and_a_half_times_the_average(volume, fires):
    s = D.signal_from_candles(with_last((100.0, 103.5, 100.5, 103.0, volume)))
    assert (s.side == "long") is fires
    if not fires:
        assert "volume" in s.reason


def test_no_average_volume_means_no_signal():
    s = D.signal_from_candles(with_last((100.0, 103.5, 100.5, 103.0, 200.0), vol=0.0))
    assert s.side is None and "average volume" in s.reason


def test_extreme_atr_is_skipped_and_the_limit_is_strict():
    cs = base(40, high=100.25, low=99.75)  # calm: ATR 0.5, ATR% 0.005
    cs.append(cd(40, 100.0, 130.0, 100.0, 125.0, 300.0))  # a huge candle: ATR jumps
    s = D.signal_from_candles(cs)
    ratio = s.atr_pct / s.atr_pct_avg
    assert s.side is None and "extreme" in s.reason and ratio > 3.0
    assert D.signal_from_candles(cs, D.DonchianParams(atr_extreme_mult=ratio + 0.01)).side == "long"
    assert D.signal_from_candles(cs, D.DonchianParams(atr_extreme_mult=ratio)).side == "long"  # equal to the limit is not above it
    assert D.signal_from_candles(cs, D.DonchianParams(atr_extreme_mult=ratio - 0.01)).side is None


def test_insufficient_history_and_unavailable_indicators():
    assert "insufficient" in D.signal_from_candles(base(30)).reason
    assert D.signal_from_candles(base(30)).side is None
    flat = base(60, high=100.0, low=100.0)
    flat.append(cd(60, 100, 101, 100, 101, 500.0))
    # zero ATR before the breakout candle: the extreme-ATR reference is zero, nothing can be sized, nothing is traded
    assert D.signal_from_candles(flat).side is None


def test_the_forming_candle_is_ignored():
    cs = with_last((100.0, 103.5, 100.5, 103.0, 200.0))
    last_open = cs[-1].timestamp
    closed = rsi_mr.closed_only(cs, last_open + HOUR // 2, HOUR)  # "now" is inside the last candle
    assert len(closed) == len(cs) - 1 and D.signal_from_candles(closed).side is None
    assert D.signal_from_candles(rsi_mr.closed_only(cs, last_open + HOUR, HOUR)).side == "long"


# ====================================================================== exits
def walk(side, rows, *, entry=103.0, stop=100.0, pre=None, p=D.DonchianParams()):
    """rows = [(o, h, l, c)] for the candles after the entry. `pre` are the candles before it (default: 40 flat candles)."""
    cs = list(pre) if pre is not None else base(40)
    start = len(cs)
    cs += [cd(start + i, o, h, l, c) for i, (o, h, l, c) in enumerate(rows)]
    return D.exit_check(side=side, entry=entry, stop=stop, opened_ms=start * HOUR, closed=cs, tf_ms=HOUR, p=p), cs


def test_initial_stop_exit_fills_at_the_stop():
    ex, _ = walk("long", [(103, 103.5, 101.0, 102.0), (102, 102.5, 99.9, 100.2)])
    assert ex.reason == D.EXIT_STOP and ex.price == 100.0
    ex, _ = walk("short", [(97, 98.0, 96.5, 97.5), (97.5, 100.1, 97.4, 99.0)], entry=97.0, stop=100.0)
    assert ex.reason == D.EXIT_STOP and ex.price == 100.0


def test_trailing_stop_ratchets_up_and_never_down():
    rows = [(103, 110.0, 103.0, 109.0), (109, 115.0, 108.0, 114.0), (114, 114.5, 112.5, 113.0)]
    cs = base(40) + [cd(40 + i, *r) for i, r in enumerate(rows)]
    a = atr([c.high for c in cs], [c.low for c in cs], [c.close for c in cs], 14)
    trail1 = 110.0 - 2 * a[40]
    trail2 = max(trail1, 115.0 - 2 * a[41])
    trail3 = max(trail2, 115.0 - 2 * a[42])  # the highest high so far is still 115 on the third candle
    assert trail2 > 100.0 and trail2 > trail1 and trail3 >= trail2  # it moved up, past the initial stop, and never down
    # a later candle that pulls back to the trail: stopped at the TRAIL, not at the initial stop
    ex, _ = walk("long", rows + [(113, 113.2, trail3 - 0.01, 113.0)])
    assert ex.reason == D.EXIT_TRAIL and math.isclose(ex.price, trail3)
    # a candle that stays above the trail does not exit
    ex, _ = walk("long", rows + [(113, 113.2, trail3 + 0.01, 113.0)])
    assert ex is None or ex.reason == D.EXIT_CHANNEL  # (never the trailing stop)
    assert ex is None or ex.price == 113.0


def test_the_trailing_stop_does_not_loosen_when_atr_grows():
    rows = [(103, 110.0, 103.0, 109.0), (109, 109.5, 100.5, 105.0)]  # a wide second candle raises ATR; its low stays above the initial stop
    ex, cs = walk("long", rows)
    a = atr([c.high for c in cs], [c.low for c in cs], [c.close for c in cs], 14)
    trail1 = 110.0 - 2 * a[40]
    assert a[41] > a[40]
    ex2, _ = walk("long", rows + [(105, 105.1, trail1 - 0.01, 105.0)])
    assert ex2 is not None and ex2.reason in (D.EXIT_TRAIL, D.EXIT_STOP, D.EXIT_CHANNEL)


def test_the_stop_beats_the_trail_check_order_and_a_tie_goes_to_the_stop():
    ex, _ = walk("long", [(103, 130.0, 90.0, 120.0)])  # one candle touches the stop (and would have raised the trail): the stop wins
    assert ex.reason == D.EXIT_STOP and ex.price == 100.0


def test_exit_on_a_close_beyond_the_opposite_ten_candle_channel():
    # A volatile history (ATR about 6) then a quiet drift: the trailing stop sits far below, the 10-candle low is what breaks first.
    vol_hist = [cd(i, 120.0, 123.0 + (i % 2), 117.0 - (i % 2), 120.0) for i in range(30)]
    quiet = [cd(30 + i, 120.0, 121.0, 119.0, 120.0) for i in range(10)]
    pre = vol_hist + quiet
    rows = [(120, 120.5, 119.2, 119.8), (119.8, 119.9, 118.0, 118.2)]  # the last closes below 119 (the lowest low of the previous 10 candles)
    ex, cs = walk("long", rows, entry=120.0, stop=100.0, pre=pre)
    assert ex.reason == D.EXIT_CHANNEL and ex.price == 118.2
    a = atr([c.high for c in cs], [c.low for c in cs], [c.close for c in cs], 14)
    assert 121.0 - 2 * a[-1] < 118.0  # precondition: the trailing stop had NOT been touched, so the exit really is the channel one
    # the same candle one tick above the channel low does not exit
    ex, _ = walk("long", [(120, 120.5, 119.2, 119.8), (119.8, 119.9, 118.0, 119.01)], entry=120.0, stop=100.0, pre=pre)
    assert ex is None
    # a short mirrors it: a close above the highest high of the previous 10 candles
    ex, _ = walk("short", [(120, 120.8, 119.5, 120.2), (120.2, 122.0, 120.1, 121.9)], entry=120.0, stop=140.0, pre=pre)
    assert ex.reason == D.EXIT_CHANNEL and ex.price == 121.9


def test_there_is_no_time_stop_and_no_profit_target():
    rows = [(103, 103.5, 102.5, 103.0)] * 200  # a long quiet stay: nothing happens
    ex, _ = walk("long", rows)
    assert ex is None


def test_candles_that_ended_before_the_entry_are_ignored():
    pre = base(40)
    pre[-1] = cd(39, 100, 101, 80.0, 100)  # a huge old wick below the stop
    ex = D.exit_check(side="long", entry=103.0, stop=100.0, opened_ms=40 * HOUR, closed=pre, tf_ms=HOUR)
    assert ex is None
    assert D.exit_check(side="long", entry=103.0, stop=100.0, opened_ms=0, closed=[], tf_ms=HOUR) is None


def test_a_short_close_exactly_on_the_lower_band_is_not_a_breakdown():
    assert D.signal_from_candles(with_last((100.0, 99.5, 98.5, 99.0, 200.0)), allow_short=True).side is None  # close == lower band 99.0
    assert D.signal_from_candles(with_last((100.0, 99.5, 98.5, 98.99, 200.0)), allow_short=True).side == "short"


def test_the_trailing_stop_never_loosens_when_atr_grows():
    rows = [(103, 110.0, 103.0, 109.0), (109, 109.9, 105.5, 109.5)]  # the second candle is wider, ATR grows, the extreme stays 110
    cs = base(40) + [cd(40 + i, *r) for i, r in enumerate(rows)]
    a = atr([c.high for c in cs], [c.low for c in cs], [c.close for c in cs], 14)
    trail1, cand2 = 110.0 - 2 * a[40], 110.0 - 2 * a[41]
    assert cand2 < trail1 and trail1 > 100.0  # a looser candidate than the trail already set
    low3 = (cand2 + trail1) / 2  # between the two: a trail that loosened would not be touched
    ex, _ = walk("long", rows + [(109.5, 109.6, low3, 109.0)])
    assert ex is not None and ex.reason == D.EXIT_TRAIL and math.isclose(ex.price, trail1)
