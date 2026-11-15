"""Audit L6: when a candle OPENS beyond the stop (a gap), the stop fills at the candle open, the worse of the two, not at the stop price. Same function for the backtest and the
paper exits (one rule). A stop the candle only touches intrabar still fills at the stop price."""
from __future__ import annotations

import pytest
from test_donchian import base, cd

from mba_bot import donchian as D
from mba_bot import donchian_backtest as DB
from mba_bot import rsi_mr as R

HOUR = 3_600_000


def rsi_walk(side, row, *, stop, entry):
    pre = [100.0 if side == "long" else 80.0 + 0.4 * i for i in range(30)]
    cs = [cd(i, v, v + 0.1, v - 0.1, v) for i, v in enumerate(pre)]
    cs.append(cd(30, *row))
    return R.exit_check(side=side, entry=entry, stop=stop, opened_ms=30 * HOUR, closed=cs, tf_ms=HOUR)


def test_rsi_long_stop_gapped_through_fills_at_the_open():
    ex = rsi_walk("long", (95.0, 95.5, 94.0, 94.5, 100.0), stop=97.0, entry=100.0)  # opens at 95, below the 97 stop
    assert ex.reason == R.EXIT_STOP and ex.price == 95.0


def test_rsi_long_stop_touched_intrabar_fills_at_the_stop():
    ex = rsi_walk("long", (99.0, 99.5, 96.0, 96.5, 100.0), stop=97.0, entry=100.0)
    assert ex.reason == R.EXIT_STOP and ex.price == 97.0


def test_rsi_short_stop_gapped_through_fills_at_the_open():
    e = 80.0 + 0.4 * 29
    ex = rsi_walk("short", (e + 5.0, e + 5.5, e + 4.5, e + 5.0, 100.0), stop=e + 3.0, entry=e)
    assert ex.reason == R.EXIT_STOP and ex.price == e + 5.0


def test_donchian_initial_stop_gapped_through_fills_at_the_open():
    cs = base(40) + [cd(40, 98.0, 98.5, 97.0, 97.5)]  # opens at 98, below the 100 stop
    ex = D.exit_check(side="long", entry=103.0, stop=100.0, opened_ms=40 * HOUR, closed=cs, tf_ms=HOUR)
    assert ex.reason == D.EXIT_STOP and ex.price == 98.0
    ex = D.exit_check(side="long", entry=103.0, stop=100.0, opened_ms=40 * HOUR, closed=base(40) + [cd(40, 103.0, 103.5, 99.0, 99.5)], tf_ms=HOUR)
    assert ex.price == 100.0  # touched, not gapped


def test_donchian_short_stop_gapped_through_fills_at_the_open():
    cs = base(40) + [cd(40, 102.0, 102.5, 101.0, 101.5)]
    ex = D.exit_check(side="short", entry=97.0, stop=100.0, opened_ms=40 * HOUR, closed=cs, tf_ms=HOUR)
    assert ex.reason == D.EXIT_STOP and ex.price == 102.0


def test_donchian_trailing_stop_gapped_through_fills_at_the_open():
    rows = [(103, 110.0, 103.0, 109.0), (109, 115.0, 108.0, 114.0)]
    cs = base(40) + [cd(40 + i, *r) for i, r in enumerate(rows)]
    gap = cd(42, 104.0, 104.5, 103.5, 104.0)  # far below any trail
    ex = D.exit_check(side="long", entry=103.0, stop=100.0, opened_ms=40 * HOUR, closed=cs + [gap], tf_ms=HOUR)
    assert ex.reason == D.EXIT_TRAIL and ex.price == 104.0


def test_the_backtest_books_the_gap_fill_with_slippage_against_the_trade():
    cs = base(40) + [cd(40, 100.0, 103.5, 100.5, 103.0, 200.0), cd(41, 103.2, 103.4, 103.0, 103.2, 100.0), cd(42, 97.0, 97.5, 96.0, 96.5, 100.0), cd(43, 96.5, 96.6, 96.4, 96.5, 100.0)]
    trades, _ = DB.simulate("BTCUSDT", cs, D.DonchianParams(), "spot", cs[0].timestamp, cs[-1].timestamp + HOUR)
    assert len(trades) == 1 and trades[0].reason == D.EXIT_STOP
    assert trades[0].exit == pytest.approx(97.0 * (1 - 5 / 1e4))  # the open (97), not the 100 stop, less slippage
