from mba_bot.indicators import atr, ema


def test_ema_pads_none_before_warmup():
    values = [1, 2, 3]
    result = ema(values, period=5)
    assert result == [None, None, None]


def test_ema_seed_is_simple_average():
    values = [1, 2, 3, 4, 5]
    result = ema(values, period=5)
    assert result[:4] == [None, None, None, None]
    assert result[4] == sum(values) / 5


def test_ema_matches_hand_computed_values():
    # period=2 => multiplier = 2/3
    values = [1, 2, 3, 4]
    result = ema(values, period=2)
    assert result[0] is None
    assert result[1] == (1 + 2) / 2  # seed = SMA(1,2)
    # ema[2] = (3 - 1.5) * 2/3 + 1.5 = 2.5
    assert round(result[2], 6) == 2.5
    # ema[3] = (4 - 2.5) * 2/3 + 2.5 = 3.5
    assert round(result[3], 6) == 3.5


def test_ema_rejects_non_positive_period():
    import pytest

    with pytest.raises(ValueError):
        ema([1, 2, 3], period=0)


def test_atr_pads_none_before_warmup():
    highs = [10, 11, 12]
    lows = [9, 10, 11]
    closes = [9.5, 10.5, 11.5]
    result = atr(highs, lows, closes, period=5)
    assert result == [None, None, None]


def test_atr_seed_uses_true_range_average():
    # 3 candles, period=2: TR[0] = high-low (no prev close), TR[1], TR[2]
    highs = [10, 12, 11]
    lows = [9, 10, 9]
    closes = [9.5, 11, 10]
    result = atr(highs, lows, closes, period=2)
    assert result[0] is None
    assert result[1] is None
    # TR[1] = max(12-10, |12-9.5|, |10-9.5|) = max(2, 2.5, 0.5) = 2.5
    # TR[2] = max(11-9, |11-11|, |9-11|) = max(2, 0, 2) = 2
    # seed = avg(TR[1], TR[2]) = 2.25
    assert round(result[2], 6) == 2.25


def test_atr_rejects_mismatched_lengths():
    import pytest

    with pytest.raises(ValueError):
        atr([1, 2], [1], [1, 2], period=1)
