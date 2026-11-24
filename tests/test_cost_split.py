"""The cost split (src/mba_bot/cost_split.py). Research only.

PROVES: the identity net = gross - fees - slippage - funding holds trade by trade for long and short trades on hand-made trades and on simulated ones; each component's arithmetic; the shares of the loss (costs and
signal) including the case where gross is positive; exit-reason counts; the stop distance per pair; and that the end-to-end collection runs on synthetic data for all three strategies.
DOES NOT PROVE anything about profit."""
from __future__ import annotations

import random

import pytest

from mba_bot import cost_split as CS
from mba_bot import entry_study as E
from mba_bot import profit_backtest as P
from mba_bot.strategy import Candle

HOUR = 3_600_000
FEE, SLIP = 5.0 / 1e4, 5.5 / 1e4


def mk(side, raw_entry, raw_exit, dist, funding_r=0.0, pair="BTCUSDT", reason="stop_loss"):
    legs = [P.Leg(1.0, raw_exit, 0, reason)]
    entry, gross, net, extra = P._cost(side, raw_entry, dist, legs, 0.0)
    return P.SimTrade(pair, side, 0, HOUR, raw_entry, entry, dist, raw_entry - dist if side == "long" else raw_entry + dist, raw_entry, legs, reason, gross, net + funding_r, funding_r, extra, False, None, HOUR)


def test_the_identity_holds_for_longs_and_shorts_with_and_without_funding():
    for t in (mk("long", 100.0, 103.0, 2.0), mk("long", 100.0, 97.0, 2.0, funding_r=-0.05), mk("short", 100.0, 97.0, 2.0, funding_r=0.02), mk("short", 100.0, 104.0, 2.0)):
        s = CS.split(t)
        assert abs(s["check"]) < 1e-12 and s["net"] == pytest.approx(s["gross"] - s["fee"] - s["slip"] - s["funding"])


def test_each_component_in_r():
    t = mk("long", 100.0, 100.0, 2.0)  # a flat trade: gross 0, so net = -(fees + slippage)
    s = CS.split(t)
    assert s["gross"] == pytest.approx(0.0)
    assert s["slip"] == pytest.approx(SLIP * 200.0 / 2.0)  # 5.5 bps of 100 on each side, over R = 2
    assert s["fee"] == pytest.approx(FEE * (100.0 * (1 + SLIP) + 100.0 * (1 - SLIP)) / 2.0)
    assert s["funding"] == 0.0 and s["net"] == pytest.approx(-(s["fee"] + s["slip"]))
    assert s["stop_pct"] == pytest.approx(2.0 / t.entry * 100.0)
    paid = CS.split(mk("long", 100.0, 100.0, 2.0, funding_r=-0.1))
    assert paid["funding"] == pytest.approx(0.1)  # funding paid is a cost
    got = CS.split(mk("short", 100.0, 100.0, 2.0, funding_r=0.1))
    assert got["funding"] == pytest.approx(-0.1)  # funding received is a negative cost


def test_the_shares_of_the_loss_when_the_signal_loses_and_when_it_does_not():
    losing = [mk("long", 100.0, 99.0, 2.0)] * 5  # gross -0.5 R
    s = CS.summarize(losing)
    assert s["gross"] == pytest.approx(-0.5) and s["cost_share_of_loss"] + s["signal_share_of_loss"] == pytest.approx(1.0) and 0 < s["cost_share_of_loss"] < 0.2
    winning_gross = [mk("long", 100.0, 100.1, 2.0)] * 5  # gross +0.05 R but net negative after about 0.1 R of costs
    w = CS.summarize(winning_gross)
    assert w["gross"] > 0 and w["net"] < 0 and w["cost_share_of_loss"] > 1.0 and w["signal_share_of_loss"] < 0  # the costs are more than the whole loss
    assert CS.summarize([mk("long", 100.0, 110.0, 2.0)])["cost_share_of_loss"] is None  # no loss: no share
    assert CS.summarize([]) == {"trades": 0}


def test_exit_reason_counts_and_the_stop_distance_per_pair():
    ts = [mk("long", 100.0, 99.0, 2.0, reason="stop_loss"), mk("long", 100.0, 101.0, 2.0, reason="channel_exit"), mk("long", 100.0, 101.0, 2.0, reason="channel_exit"), mk("long", 50.0, 51.0, 5.0, pair="XRPUSDT", reason="trailing_stop")]
    s = CS.summarize(ts)
    assert s["reasons"] == {"channel_exit": 2, "stop_loss": 1, "trailing_stop": 1}
    assert s["per_pair"]["BTCUSDT"]["trades"] == 3 and s["per_pair"]["XRPUSDT"]["stop_pct"] == pytest.approx(5.0 / ts[3].entry * 100) and "ETHUSDT" not in s["per_pair"]


def walk(n=2500, seed=5):
    rng = random.Random(seed)
    price, rows = 100.0, []
    for i in range(n):
        o = price
        price *= 1 + rng.gauss(0.004 * (1 if (i // 120) % 2 == 0 else -1), 0.01)
        rows.append(Candle(1_700_000_000_000 // HOUR * HOUR + i * HOUR, o, max(o, price) * 1.003, min(o, price) * 0.997, price, 1.0 + (3.0 if rng.random() < 0.08 else 0.0) + rng.random()))
    return rows


def ranging(n=3000, seed=9):
    rng = random.Random(seed)
    price, rows = 100.0, []
    for i in range(n):
        o = price
        price += (100.0 - price) * 0.05 + rng.gauss(0, 0.9)
        rows.append(Candle(1_700_000_000_000 // HOUR * HOUR + i * HOUR, o, max(o, price) + abs(rng.gauss(0, 0.3)), min(o, price) - abs(rng.gauss(0, 0.3)), price, 1.0 + rng.random()))
    return rows


@pytest.mark.parametrize("strat", ["ema", "donchian", "rsi"])
def test_the_identity_holds_on_simulated_trades_of_every_strategy(strat):
    cs = walk() if strat != "rsi" else ranging()
    f = E.build_feat(cs)
    trades = E.SIM[strat]("X", f, None, cs[0].timestamp, cs[-1].timestamp + HOUR, [])
    assert len(trades) > 3
    s = CS.summarize(trades)
    assert s["max_identity_error"] < 1e-9 and s["net"] == pytest.approx(sum(t.net_r for t in trades) / len(trades))
    assert sum(s["reasons"].values()) == len(trades)


def test_the_plan_hash_is_stable_and_changes_with_a_window_or_a_source(monkeypatch):
    h = CS.plan_hash()
    assert h == CS.plan_hash() and len(h) == 16
    monkeypatch.setitem(CS.BOOT, "reps", 5)
    assert CS.plan_hash() != h
    monkeypatch.undo()
    assert set(CS.source_pins()) >= {"cost_split.py", "entry_study.py", "profit_backtest_dr.py"}


def test_funding_is_part_of_the_costs_in_the_share():
    ts = [mk("long", 100.0, 99.0, 2.0, funding_r=-0.2)] * 4
    s = CS.summarize(ts)
    assert s["funding"] == pytest.approx(0.2) and s["cost"] == pytest.approx(s["fee"] + s["slip"] + 0.2)
    assert s["cost_share_of_loss"] == pytest.approx(s["cost"] / -s["net"])
