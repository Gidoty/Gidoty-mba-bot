"""Tests for the Fixed/Manual vs. Auto Select validation module
(backtest.py) - Phase 9 of the capital-control implementation.

Two things matter most here: (1) no look-ahead - whatever happened up to
some bar must be identical whether or not more history is appended after
it, and (2) Auto Select can genuinely reach a different outcome than a
Fixed pair that never had a signal, on the exact same underlying data,
using the real production ranking function rather than a separate
algorithm.
"""
from __future__ import annotations

from mba_bot.backtest import run_auto_select_backtest, run_fixed_pair_backtest
from mba_bot.strategy import Candle

STRATEGY_KWARGS = dict(ema_fast_period=2, ema_slow_period=3, atr_period=2, atr_flat_threshold=0.75, atr_rolling_window=3)


def _candles(closes: list[float]) -> list[Candle]:
    return [Candle(timestamp=i, open=c, high=c + 5.0, low=c - 5.0, close=c, volume=1.0) for i, c in enumerate(closes)]


# A bullish run up, then a decline sharp enough to trip a bearish
# crossover exit - the position opens AND closes within this series, so
# tests can assert on a fully resolved trade rather than a still-open one.
_RISE_THEN_FALL = [100, 95, 90, 85, 80, 75, 70, 65, 60, 58, 90, 120, 150, 140, 120, 100, 80, 60, 40]
_FLAT = [100.0] * len(_RISE_THEN_FALL)


def _run_fixed(closes, **overrides):
    kwargs = dict(
        pair="BTC/USDT",
        candles=_candles(closes),
        market_type="spot",
        starting_balance=10_000.0,
        risk_pct=1.0,
        max_risk_pct=2.0,
        atr_stop_multiplier=2.0,
        **STRATEGY_KWARGS,
    )
    kwargs.update(overrides)
    return run_fixed_pair_backtest(**kwargs)


def test_fixed_pair_backtest_produces_no_trades_on_a_flat_series():
    result = _run_fixed(_FLAT)
    assert result.trades == []
    assert result.ending_balance == result.starting_balance
    assert result.win_rate is None


def test_fixed_pair_backtest_opens_and_closes_a_trade_on_a_real_rise_then_fall():
    result = _run_fixed(_RISE_THEN_FALL)
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.side == "long"
    assert trade.entry_index < trade.exit_index
    assert result.win_rate in (0.0, 1.0)


def test_no_look_ahead_truncated_and_extended_runs_agree_on_everything_that_already_happened():
    full_closes = _RISE_THEN_FALL + [1000.0, 1.0, 1000.0]  # wild, unrelated future data appended
    truncated = _run_fixed(_RISE_THEN_FALL)
    extended = _run_fixed(full_closes)

    # Every trade that concluded within the truncated window must be
    # byte-for-byte identical in the extended run - nothing about it can
    # have been influenced by bars that didn't exist yet at the time.
    concluded_in_truncated = [t for t in truncated.trades if t.exit_index < len(_RISE_THEN_FALL)]
    assert concluded_in_truncated  # the fixture above does produce one
    matching = [t for t in extended.trades if t.exit_index < len(_RISE_THEN_FALL)]
    assert concluded_in_truncated == matching


def test_auto_select_backtest_trades_the_pair_with_the_signal_not_the_flat_one():
    candles_by_pair = {"BTC/USDT": _candles(_FLAT), "ETH/USDT": _candles(_RISE_THEN_FALL)}
    result = run_auto_select_backtest(
        candles_by_pair,
        market_type="spot",
        starting_balance=10_000.0,
        risk_pct=1.0,
        max_risk_pct=2.0,
        atr_stop_multiplier=2.0,
        **STRATEGY_KWARGS,
    )
    assert len(result.trades) == 1
    assert result.trades[0].pair == "ETH/USDT"


def test_auto_select_backtest_produces_no_trades_when_every_candidate_is_flat():
    candles_by_pair = {"BTC/USDT": _candles(_FLAT), "ETH/USDT": _candles(_FLAT)}
    result = run_auto_select_backtest(
        candles_by_pair,
        market_type="spot",
        starting_balance=10_000.0,
        risk_pct=1.0,
        max_risk_pct=2.0,
        atr_stop_multiplier=2.0,
        **STRATEGY_KWARGS,
    )
    assert result.trades == []
    assert result.ending_balance == result.starting_balance


def test_max_drawdown_reflects_a_losing_trade():
    # A rise then an immediate, even sharper fall that stops the long out
    # for a loss - max_drawdown_pct must be positive.
    losing_series = [100, 95, 90, 85, 80, 75, 70, 65, 60, 58, 90, 120, 150, 50, 10]
    result = _run_fixed(losing_series)
    assert len(result.trades) == 1
    assert result.trades[0].pnl < 0
    assert result.max_drawdown_pct > 0
