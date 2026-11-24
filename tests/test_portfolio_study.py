"""The portfolio view (src/mba_bot/portfolio_study.py). Research only.

PROVES: the slot rule (first signal wins, ties by catalog order, a signal is dropped not queued, a position ending exactly at a signal frees the slot); unlimited slots take everything; drawdown, equity and losing streak
arithmetic; that only development-window trades are used; the plan hash moves with the slots. DOES NOT PROVE anything about profit."""
from __future__ import annotations

import pytest

from mba_bot import entry_study as E
from mba_bot import portfolio_study as S
from mba_bot import profit_backtest as P

DAY = 86_400_000
BASE = E.TEST_STARTS[1]


def T(net, day, hold_days=2, pair="BTCUSDT"):
    ts = BASE + int(day * DAY)
    return P.SimTrade(pair, "long", ts, ts + hold_days * DAY, 100.0, 100.0, 10.0, 90.0, 100.0, [P.Leg(1.0, 100.0, ts, "x")], "x", net + 0.1, net, 0.0, 0.0, False, None, hold_days * DAY)


def test_one_slot_takes_the_first_signal_and_drops_what_arrives_while_it_is_open():
    trades = [T(1.0, 0, 5, "BTCUSDT"), T(2.0, 1, 2, "ETHUSDT"), T(3.0, 4.9, 1, "XRPUSDT"), T(4.0, 5.0, 1, "SOLUSDT")]
    taken, dropped = S.take(trades, 1)
    assert [t.pair for t in taken] == ["BTCUSDT", "SOLUSDT"] and [t.pair for t in dropped] == ["ETHUSDT", "XRPUSDT"]  # the position ending exactly at day 5 frees the slot for SOL


def test_a_simultaneous_signal_goes_to_the_earlier_pair_in_the_catalog():
    taken, dropped = S.take([T(1.0, 0, 3, "LTCUSDT"), T(1.0, 0, 3, "ETHUSDT")], 1)
    assert [t.pair for t in taken] == ["ETHUSDT"] and [t.pair for t in dropped] == ["LTCUSDT"]


def test_more_slots_take_more_and_unlimited_takes_everything():
    trades = [T(1.0, 0, 10, p) for p in ("BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT")]
    assert [len(S.take(trades, k)[0]) for k in (1, 2, 3, None)] == [1, 2, 3, 4]
    assert S.max_open(S.take(trades, 2)[0]) == 2 and S.max_open(trades) == 4


def test_a_dropped_signal_is_not_queued_for_later():
    taken, dropped = S.take([T(1.0, 0, 3, "BTCUSDT"), T(1.0, 1, 5, "ETHUSDT")], 1)
    assert len(taken) == 1 and len(dropped) == 1


def test_drawdown_equity_and_losing_streak():
    trades = [T(2.0, 0, 1), T(-1.0, 2, 1), T(-1.0, 4, 1), T(-1.0, 6, 1), T(5.0, 8, 1), T(-1.0, 10, 1)]
    d = S.drawdowns(trades, 1.0)
    assert d["max_dd_r"] == pytest.approx(3.0) and d["final_equity"] == pytest.approx(1.02 * 0.99 ** 3 * 1.05 * 0.99)
    assert d["max_dd_equity"] == pytest.approx(1 - 0.99 ** 3) and d["lowest_equity"] == pytest.approx(1.02 * 0.99 ** 3)
    assert S.losing_streak(trades) == 3 and S.losing_streak([]) == 0
    assert S.drawdowns([T(-200.0, 0)], 1.0)["lowest_equity"] == 0.0  # a loss beyond the whole account ends at zero, never negative


def test_the_streak_is_in_closing_order_not_signal_order():
    a = T(-1.0, 0, 10, "BTCUSDT")  # opens first, closes last
    b, c = T(-1.0, 1, 1, "ETHUSDT"), T(1.0, 3, 1, "BNBUSDT")
    assert S.losing_streak([a, b, c]) == 1  # closing order b, c, a: loss, win, loss (signal order a, b, c would say 2)


def test_tail_summary():
    t = S.tail([T(10.0, 0), T(-1.0, 1), T(-1.0, 2), T(1.0, 3)])
    assert t["win_rate"] == 0.5 and t["best_r"] == 10.0 and t["worst_r"] == -1.0 and t["top5_share_of_total"] == 1.0
    many = S.tail([T(10.0, 0), T(4.0, 1), T(3.0, 2), T(2.0, 3), T(1.0, 4)] + [T(-1.0, 5 + i) for i in range(5)] + [T(0.5, 20)])
    assert many["top5_share_of_total"] == pytest.approx(20 / 15.5) and many["median_r"] == 0.5


def test_summary_counts_cost_of_the_single_slot():
    trades = [T(1.0, 0, 5), T(3.0, 1, 2, "ETHUSDT"), T(-2.0, 6, 1, "XRPUSDT")]
    one, unl = S.summarize(trades, 1, span_ms=365 * DAY), S.summarize(trades, None, span_ms=365 * DAY)
    assert one["taken"] == 2 and one["dropped"] == 1 and one["dropped_total_net_r"] == 3.0 and unl["taken"] == 3
    assert unl["total_net_r"] - one["total_net_r"] == pytest.approx(one["dropped_total_net_r"]) and one["taken_per_year"] == pytest.approx(2 / (365 / 365.25))


def test_only_development_window_trades_are_used(tmp_path, monkeypatch):
    dev = T(1.0, 5)
    late = P.SimTrade("BTCUSDT", "long", E.TEST_STARTS[6] + DAY, E.TEST_STARTS[6] + 2 * DAY, 100.0, 100.0, 10.0, 90.0, 100.0, [P.Leg(1.0, 100.0, 0, "x")], "x", 99.0, 99.0, 0.0, 0.0, False, None, DAY)
    monkeypatch.setattr(S.U, "simulate", lambda root, cells=S.U.CELLS: ({c: [dev, late] for c in cells}, {}))
    res = S.run(tmp_path, tmp_path / "o")
    assert all(v["unlimited"]["signals"] == 1 and v["unlimited"]["total_net_r"] == 1.0 for k, v in res.items() if not k.endswith("BNB"))
    assert (tmp_path / "o" / "portfolio_report.md").exists()


def test_the_plan_hash_moves_with_the_slots(monkeypatch):
    base = S.plan_hash()
    assert S.plan_hash() == base
    monkeypatch.setattr(S, "SLOTS", (1, 2, None))
    assert S.plan_hash() != base


def test_a_break_even_trade_ends_a_losing_streak():
    assert S.losing_streak([T(-1.0, 0, 1), T(0.0, 2, 1), T(-1.0, 4, 1)]) == 1


def test_a_position_closing_exactly_when_another_opens_is_not_counted_as_two_open():
    assert S.max_open([T(1.0, 0, 2, "BTCUSDT"), T(1.0, 2, 2, "ETHUSDT")]) == 1
