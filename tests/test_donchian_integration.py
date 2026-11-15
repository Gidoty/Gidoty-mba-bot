"""donchian_breakout_v1 inside the trading cycle (trader._process_multi, trader._maybe_enter, strategy_guards.evaluate).

PROVES: with the switch off nothing changes (every Donchian function raises), a live account never runs the strategy whatever any setting says,
it trades only with the PR #45 guards and the fee guard on, the guards apply to it per strategy (cooldown after a stop on the same pair), the slot
rule with Donchian first, and the exits. All against a FAKE exchange and fixed candles. DOES NOT PROVE: any profit, or anything about Binance."""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import time
from datetime import datetime, timedelta, timezone

import pytest
from helpers_live import FakeNotifier, all_trades, futures_client, install, make_settings, new_account, run_cycle, spot_client
from test_donchian import base, cd, with_last

from mba_bot import donchian as D
from mba_bot import multi_trade as mt
from mba_bot import rsi_mr, strategy_guards as g, trader
from mba_bot.config import load_settings
from mba_bot.db import init_db, session_scope
from mba_bot.models import DecisionLog, StrategyPause, Trade, TradingAccount
from mba_bot.strategy import SignalType, StrategySignal

U = timezone.utc
HOUR = 3_600_000
KEYS = dict(testnet_spot_api_key="paper-key-1234567890", testnet_spot_api_secret="paper-secret-1234567890",
            demo_futures_api_key="demo-key-1234567890", demo_futures_api_secret="demo-secret-1234567890")
PAIRS = "ETH/USDT,BNB/USDT"
NO_EMA = StrategySignal(SignalType.NONE, "no crossover", 100.0, 100.0, 1.0, 0.01)

LONG_ROW = (100.0, 103.5, 100.5, 103.0, 200.0)
SHORT_ROW = (100.0, 99.5, 96.5, 97.0, 200.0)


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")
    return tmp_path


def raw(candles, *, forming=False):
    """ccxt rows [ts, o, h, l, c, v] for `candles`, re-timed so the last one has just CLOSED (or is still forming with forming=True)."""
    end = int(time.time() * 1000) // HOUR * HOUR + (HOUR if forming else 0)
    n = len(candles)
    return [[end - (n - i) * HOUR, c.open, c.high, c.low, c.close, c.volume] for i, c in enumerate(candles)]


def settings(**over):
    base_ = dict(KEYS, donchian_enabled=True, strategy_guards_mode="enforce", fee_guard_margin=0.5, strategy_expectancy_window=0, max_total_risk_pct=5.0,
                 max_notional_multiplier=100.0, trade_telemetry=True)
    base_.update(over)
    return make_settings(**base_)


def paper_account(st, email="don@example.com", pair_list=None, market="spot", risk=1.0):
    aid = new_account(st, email=email, market_type=market)
    with session_scope() as s:
        a = s.get(TradingAccount, aid)
        a.bot_started_at = None  # PAPER
        a.pair_list = pair_list
        a.risk_pct = risk
    return aid


def setup(db, monkeypatch, candles, *, market="spot", ema=False, pair_list=None, risk=1.0, **over):
    st = settings(max_open_positions=over.pop("max_open_positions", 1), **over)
    data = raw(candles)
    mk = futures_client if market == "futures" else spot_client
    client = mk(ohlcv_by_pair={p: data for p in ("BTC/USDT", "ETH/USDT", "BNB/USDT")}, ohlcv=data)
    install(monkeypatch, client)
    if not ema:
        monkeypatch.setattr(trader, "generate_signal", lambda *a, **k: NO_EMA)
    return st, client, paper_account(st, pair_list=pair_list, market=market, risk=risk)


def decisions():
    with session_scope() as s:
        return [(d.strategy_id, d.pair, d.outcome, d.reason_code) for d in s.query(DecisionLog).all()]


def seed_closed(aid, strategy, pair, nets, *, minutes_ago=30, reason="stop_loss"):
    now = datetime.now(U)
    with session_scope() as s:
        for i, net in enumerate(nets):
            s.add(Trade(account_id=aid, side="long", is_paper=True, entry_price=100.0, quantity=1.0, stop_loss_price=90.0, exit_price=100.0 + net, pnl=net,
                        status="closed", close_reason=reason, pair=pair, strategy_id=strategy, opened_at=now - timedelta(hours=5),
                        closed_at=now - timedelta(minutes=minutes_ago + i)))


# ====================================================================== switches
def test_defaults_are_off_and_paper_only():
    s = load_settings()
    assert s.donchian_enabled is False and s.donchian_paper_only is True and s.donchian_risk_pct == 0.5 < 1.0 and s.donchian_timeframe == "1h"
    assert (s.donchian_entry_period, s.donchian_exit_period, s.donchian_volume_mult, s.donchian_atr_extreme_mult, s.donchian_stop_atr, s.donchian_trail_atr) == (20, 10, 1.5, 3.0, 1.5, 2.0)
    assert (s.donchian_spot_fee_bps, s.donchian_futures_fee_bps, s.donchian_slippage_bps, s.donchian_stop_cooldown_hours) == (10.0, 5.0, 5.0, 4.0)


def test_environment_overrides_are_clamped(monkeypatch):
    for k, v in {"DONCHIAN_ENABLED": "true", "DONCHIAN_RISK_PCT": "99", "DONCHIAN_ENTRY_PERIOD": "0", "DONCHIAN_STOP_COOLDOWN_HOURS": "-3", "DONCHIAN_TIMEFRAME": " 4H "}.items():
        monkeypatch.setenv(k, v)
    s = load_settings()
    assert s.donchian_enabled and s.donchian_risk_pct == 2.0 and s.donchian_entry_period == 2 and s.donchian_stop_cooldown_hours == 0.0 and s.donchian_timeframe == "4h"


def poison_donchian(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("donchian_breakout_v1 code ran")

    async def aboom(*a, **k):
        raise AssertionError("donchian_breakout_v1 code ran")

    monkeypatch.setattr(D, "signal_from_candles", boom)
    monkeypatch.setattr(D, "exit_check", boom)
    monkeypatch.setattr(D, "initial_stop", boom)
    monkeypatch.setattr(trader, "_manage_donchian_trade", aboom)


def test_with_the_switch_off_nothing_changes_for_a_paper_account(db, monkeypatch):
    """Every Donchian function raises, every knob is set, the switch is off: the account trades exactly as an account without the feature."""
    st = settings(donchian_enabled=False, strategy_guards_mode="off", donchian_risk_pct=0.01, donchian_volume_mult=0.0)
    client = spot_client()  # BULLISH_OHLCV: the EMA strategy signals
    install(monkeypatch, client)
    poison_donchian(monkeypatch)

    async def no_multi(*a, **k):
        raise AssertionError("the strategy-aware path ran with both switches off")

    monkeypatch.setattr(trader, "_process_multi", no_multi)
    aid = paper_account(st)
    run_cycle(aid, st)
    run_cycle(aid, st)
    trades = all_trades(aid)
    assert len(trades) == 1 and trades[0].strategy_id == "ema_cross_atr_v1" and trades[0].is_paper
    assert math.isclose(trades[0].quantity * abs(trades[0].entry_price - trades[0].stop_loss_price), 100.0, rel_tol=1e-6)  # 1% of 10000, not the Donchian 0.01


def test_with_the_switch_off_the_rsi_path_still_never_touches_donchian(db, monkeypatch):
    st = settings(donchian_enabled=False, rsi_mr_enabled=True)
    client = spot_client(ohlcv_by_pair={p: raw(with_last(LONG_ROW)) for p in ("BTC/USDT", "ETH/USDT", "BNB/USDT")}, ohlcv=raw(with_last(LONG_ROW)))
    install(monkeypatch, client)
    monkeypatch.setattr(trader, "generate_signal", lambda *a, **k: NO_EMA)
    poison_donchian(monkeypatch)
    aid = paper_account(st)
    run_cycle(aid, st)  # the strategy-aware path runs (RSI is on) and must not call a Donchian function
    assert [t for t in all_trades(aid) if t.strategy_id == "donchian_breakout_v1"] == []


@pytest.mark.parametrize("paper_only_setting", [True, False])
def test_a_live_account_never_runs_donchian_whatever_any_setting_says(db, monkeypatch, paper_only_setting):
    st = settings(donchian_paper_only=paper_only_setting, max_open_positions=3)
    data = raw(with_last(LONG_ROW))
    client = spot_client(ohlcv_by_pair={p: data for p in ("BTC/USDT", "ETH/USDT", "BNB/USDT")}, ohlcv=data)
    install(monkeypatch, client)
    poison_donchian(monkeypatch)
    aid = new_account(st)  # LIVE
    with session_scope() as s:
        s.get(TradingAccount, aid).pair_list = PAIRS
    for _ in range(3):
        run_cycle(aid, st, swallow=True)
    assert not any(t.strategy_id == "donchian_breakout_v1" for t in all_trades(aid))


def test_the_entry_itself_refuses_a_live_call_and_a_call_with_the_switch_off(db, monkeypatch):
    st = settings()
    client = spot_client()
    install(monkeypatch, client)
    live = new_account(st)  # LIVE
    sig = D.signal_from_candles(with_last(LONG_ROW))
    assert sig.side == "long"

    async def go(aid, stt, is_paper):
        with session_scope(autoflush=False) as s:
            await trader._maybe_enter(s, s.get(TradingAccount, aid), client, sig, 103.0, stt, FakeNotifier(), is_paper=is_paper, now=datetime.now(U))

    asyncio.run(go(live, st, False))
    assert all_trades(live) == [] and not [c for c in client.calls if c[0] == "create_order"]
    paper = paper_account(st, "p2@example.com")
    asyncio.run(go(paper, dataclasses.replace(st, donchian_enabled=False), True))
    assert all_trades(paper) == []


# ====================================================================== entries
def test_a_long_breakout_opens_a_paper_trade_with_the_channel_stop(db, monkeypatch):
    st, client, aid = setup(db, monkeypatch, with_last(LONG_ROW))
    note = run_cycle(aid, st)
    t = all_trades(aid)[0]
    assert (t.strategy_id, t.side, t.is_paper, t.pair, t.status) == ("donchian_breakout_v1", "long", True, "BTC/USDT", "open")
    assert t.entry_price == 103.0 and t.stop_loss_price == 100.0  # the midline: closer to entry than 1.5 ATR
    assert math.isclose(t.quantity * (t.entry_price - t.stop_loss_price), 50.0, rel_tol=1e-6)  # 0.5 percent of 10000: the Donchian cap, below the account's 1 percent
    assert any("Strategy: donchian_breakout_v1" in m and "Entered LONG BTC/USDT" in m and "0.50%" in m for m in note.texts)
    assert not [c for c in client.calls if c[0] == "create_order"]  # paper: no order


def test_the_risk_is_the_smaller_of_the_account_and_the_strategy_cap(db, monkeypatch):
    st, client, aid = setup(db, monkeypatch, with_last(LONG_ROW), risk=0.2)
    run_cycle(aid, st)
    t = all_trades(aid)[0]
    assert math.isclose(t.quantity * (t.entry_price - t.stop_loss_price), 20.0, rel_tol=1e-6)


def test_a_short_breakdown_trades_on_futures_only(db, monkeypatch):
    st, client, aid = setup(db, monkeypatch, with_last(SHORT_ROW), market="futures")
    run_cycle(aid, st)
    t = all_trades(aid)[0]
    assert (t.side, t.strategy_id, t.entry_price, t.stop_loss_price) == ("short", "donchian_breakout_v1", 97.0, 100.0)
    d2 = db / "spot"
    d2.mkdir()
    init_db(f"sqlite:///{d2}/t.db")
    st, client, aid = setup(d2, monkeypatch, with_last(SHORT_ROW), market="spot")
    run_cycle(aid, st)
    assert all_trades(aid) == []


@pytest.mark.parametrize("row", [(100.0, 104.0, 100.0, 100.5, 300.0),     # a wick beyond the channel only
                                 (100.0, 103.5, 100.5, 103.0, 140.0),     # no volume confirmation
                                 (100.0, 101.5, 100.0, 101.0, 300.0)])    # close equal to the channel
def test_no_trade_for_a_wick_missing_volume_or_a_close_on_the_line(db, monkeypatch, row):
    st, client, aid = setup(db, monkeypatch, with_last(row))
    run_cycle(aid, st)
    assert all_trades(aid) == []


def test_extreme_atr_is_skipped_in_the_cycle(db, monkeypatch):
    cs = base(40, high=100.25, low=99.75)
    cs.append(cd(40, 100.0, 130.0, 100.0, 125.0, 300.0))
    st, client, aid = setup(db, monkeypatch, cs)
    run_cycle(aid, st)
    assert all_trades(aid) == []


def test_the_forming_candle_is_ignored_in_the_cycle(db, monkeypatch):
    st = settings()
    client = spot_client(ohlcv_by_pair={p: raw(with_last(LONG_ROW), forming=True) for p in ("BTC/USDT", "ETH/USDT", "BNB/USDT")},
                         ohlcv=raw(with_last(LONG_ROW), forming=True))
    install(monkeypatch, client)
    monkeypatch.setattr(trader, "generate_signal", lambda *a, **k: NO_EMA)
    aid = paper_account(st)
    run_cycle(aid, st)
    assert all_trades(aid) == []  # the breakout exists only on a candle that has not closed yet


def test_it_does_not_trade_without_the_guards_or_the_fee_guard(db, monkeypatch, caplog):
    st, client, aid = setup(db, monkeypatch, with_last(LONG_ROW), strategy_guards_mode="off")
    with caplog.at_level(logging.WARNING, logger="mba_bot.trader"):
        run_cycle(aid, st)
    assert all_trades(aid) == [] and any("donchian_guards_off" in r.getMessage() and "@" not in r.getMessage() for r in caplog.records)
    assert ("donchian_breakout_v1", "BTC/USDT", "rejected", "donchian_guards_off") in decisions()
    d2 = db / "t2"
    d2.mkdir()
    init_db(f"sqlite:///{d2}/t.db")
    st, client, aid = setup(d2, monkeypatch, with_last(LONG_ROW), fee_guard_margin=0.0)
    run_cycle(aid, st)
    assert all_trades(aid) == [] and ("donchian_breakout_v1", "BTC/USDT", "rejected", "donchian_fee_guard_off") in decisions()


def test_the_fee_guard_blocks_and_uses_the_market_fees_and_slippage(db, monkeypatch):
    st, client, aid = setup(db, monkeypatch, with_last(LONG_ROW), fee_guard_margin=50.0)
    run_cycle(aid, st)
    assert all_trades(aid) == [] and ("donchian_breakout_v1", "BTC/USDT", "rejected", g.R_FEE_GUARD) in decisions()
    # Spot: 2 x 10 bps fees + 2 x 5 bps slippage = 30 bps. Futures: 2 x 5 + 10 = 20 bps. Expected move = 1.0 ATR (the proxy).
    st = settings(fee_guard_margin=1.0)
    spot, fut = paper_account(st, "a@example.com"), paper_account(st, "b@example.com", market="futures")
    sig = dataclasses.replace(D.DonchianSignal("long", "x"), atr_pct=0.0025)
    with session_scope() as s:
        a = g.evaluate(s, s.get(TradingAccount, spot), st, signal=sig, is_paper=True, now=datetime.now(U), strategy_id="donchian_breakout_v1")
        b = g.evaluate(s, s.get(TradingAccount, fut), st, signal=sig, is_paper=True, now=datetime.now(U), strategy_id="donchian_breakout_v1")
    assert [h.code for h in a if h.code == g.R_FEE_GUARD] and not [h for h in b if h.code == g.R_FEE_GUARD]


def test_observe_mode_logs_but_does_not_block(db, monkeypatch):
    st, client, aid = setup(db, monkeypatch, with_last(LONG_ROW), strategy_guards_mode="observe", fee_guard_margin=50.0)
    run_cycle(aid, st)
    assert [t.strategy_id for t in all_trades(aid)] == ["donchian_breakout_v1"]
    assert ("donchian_breakout_v1", "BTC/USDT", "observed", g.R_FEE_GUARD) in decisions()


def test_an_invalid_stop_is_refused(db, monkeypatch):
    st, client, aid = setup(db, monkeypatch, with_last(LONG_ROW))
    monkeypatch.setattr(D, "initial_stop", lambda *a, **k: 104.0)  # on the wrong side of a 103 entry
    run_cycle(aid, st)
    assert all_trades(aid) == [] and ("donchian_breakout_v1", "BTC/USDT", "rejected", "donchian_stop_invalid") in decisions()


# ====================================================================== cooldown and per-strategy guards
def test_the_cooldown_after_a_stop_on_the_same_pair_blocks_that_pair_only(db, monkeypatch):
    st, client, aid = setup(db, monkeypatch, with_last(LONG_ROW), pair_list=PAIRS, max_open_positions=3)
    seed_closed(aid, "donchian_breakout_v1", "BTC/USDT", [-5.0], minutes_ago=60)  # stopped out an hour ago, cooldown 4 hours
    run_cycle(aid, st)
    assert [t.pair for t in all_trades(aid) if t.status == "open"] == ["ETH/USDT"]  # BTC refused, ETH taken in the same cycle
    assert ("donchian_breakout_v1", "BTC/USDT", "rejected", g.R_STOP_COOLDOWN) in decisions()


def test_the_cooldown_ends_and_only_the_initial_stop_counts(db, monkeypatch):
    st, client, aid = setup(db, monkeypatch, with_last(LONG_ROW))
    seed_closed(aid, "donchian_breakout_v1", "BTC/USDT", [-5.0], minutes_ago=5 * 60)  # five hours ago: over
    seed_closed(aid, "donchian_breakout_v1", "BTC/USDT", [4.0], minutes_ago=10, reason="trailing_stop")  # a trailing stop is not a stop-out
    seed_closed(aid, "ema_cross_atr_v1", "BTC/USDT", [-5.0], minutes_ago=10)  # another strategy's stop
    run_cycle(aid, st)
    assert [t.strategy_id for t in all_trades(aid) if t.status == "open"] == ["donchian_breakout_v1"]


def test_the_cooldown_can_be_switched_off(db, monkeypatch):
    st, client, aid = setup(db, monkeypatch, with_last(LONG_ROW), donchian_stop_cooldown_hours=0.0)
    seed_closed(aid, "donchian_breakout_v1", "BTC/USDT", [-5.0], minutes_ago=5)
    run_cycle(aid, st)
    assert [t.strategy_id for t in all_trades(aid) if t.status == "open"] == ["donchian_breakout_v1"]


def test_the_ema_cooldown_setting_does_not_apply_to_donchian_and_the_gap_rule_is_skipped(db, monkeypatch):
    st, client, aid = setup(db, monkeypatch, with_last(LONG_ROW), ema_stop_cooldown_hours=48.0, ema_min_gap_atr=5.0, donchian_stop_cooldown_hours=0.0)
    seed_closed(aid, "donchian_breakout_v1", "BTC/USDT", [-5.0], minutes_ago=5)
    run_cycle(aid, st)
    assert [t.strategy_id for t in all_trades(aid) if t.status == "open"] == ["donchian_breakout_v1"]


def test_the_ema_cooldown_still_applies_to_ema_and_ignores_donchian_stops(db):
    st = settings(ema_stop_cooldown_hours=4.0)
    aid = paper_account(st)
    seed_closed(aid, "donchian_breakout_v1", "BTC/USDT", [-5.0], minutes_ago=10)
    sig = StrategySignal(SignalType.BULLISH_CROSSOVER, "x", 100.0, 99.0, 1.0, 0.01, gap_atr=1.0)
    with session_scope() as s:
        assert g.R_STOP_COOLDOWN not in [h.code for h in g.evaluate(s, s.get(TradingAccount, aid), st, signal=sig, is_paper=True, now=datetime.now(U))]
    seed_closed(aid, "ema_cross_atr_v1", "BTC/USDT", [-5.0], minutes_ago=10)
    with session_scope() as s:
        assert g.R_STOP_COOLDOWN in [h.code for h in g.evaluate(s, s.get(TradingAccount, aid), st, signal=sig, is_paper=True, now=datetime.now(U))]


def test_loss_streak_daily_loss_and_expectancy_use_only_donchians_own_trades(db):
    st = settings(strategy_pause_after_losses=2, strategy_daily_loss_pct=3.0)
    aid = paper_account(st)
    seed_closed(aid, "ema_cross_atr_v1", "BTC/USDT", [-400.0, -5.0, -5.0], minutes_ago=0)
    seed_closed(aid, "rsi_mr_v1", "BTC/USDT", [-400.0], minutes_ago=0)
    sig = dataclasses.replace(D.DonchianSignal("long", "x"), atr_pct=0.05)
    with session_scope() as s:
        codes = [h.code for h in g.evaluate(s, s.get(TradingAccount, aid), st, signal=sig, is_paper=True, now=datetime.now(U), strategy_id="donchian_breakout_v1")]
    assert g.R_LOSS_STREAK not in codes and g.R_DAILY_LOSS not in codes
    seed_closed(aid, "donchian_breakout_v1", "BTC/USDT", [-400.0, -5.0], minutes_ago=0, reason="channel_exit")
    with session_scope() as s:
        codes = [h.code for h in g.evaluate(s, s.get(TradingAccount, aid), st, signal=sig, is_paper=True, now=datetime.now(U), strategy_id="donchian_breakout_v1")]
    assert g.R_LOSS_STREAK in codes and g.R_DAILY_LOSS in codes


# ====================================================================== slot rule with Donchian first
BULL = StrategySignal(SignalType.BULLISH_CROSSOVER, "x", 100.0, 99.0, 1.0, 0.01, gap_atr=1.0)
RSI_LONG = rsi_mr.RsiSignal("long", "x", rsi=35.0, prev_rsi=25.0, adx=15.0, atr=1.0, atr_pct=0.01, target_atr=1.0)


def all_three(monkeypatch, db, **over):
    st, client, aid = setup(db, monkeypatch, with_last(LONG_ROW), ema=True, rsi_mr_enabled=True, **over)
    monkeypatch.setattr(trader, "generate_signal", lambda *a, **k: BULL)
    monkeypatch.setattr(rsi_mr, "generate_signal", lambda *a, **k: RSI_LONG)
    return st, client, aid


def test_when_all_three_signal_on_the_same_pair_donchian_takes_the_slot_and_the_others_are_logged(db, monkeypatch, caplog):
    st, client, aid = all_three(monkeypatch, db)
    with caplog.at_level(logging.INFO, logger="mba_bot.trader"):
        run_cycle(aid, st)
    assert [(t.strategy_id, t.pair) for t in all_trades(aid)] == [("donchian_breakout_v1", "BTC/USDT")]
    lost = [r.getMessage() for r in caplog.records if "strategy_slot_lost_to" in r.getMessage()]
    assert any("ema_cross_atr_v1" in m and "donchian_breakout_v1" in m for m in lost) and any("rsi_mr_v1" in m for m in lost)
    # The decision log de-duplicates on (account, pair, stage, outcome, reason, candle) WITHOUT the strategy id (telemetry.log_decision, from before this PR),
    # so the second loser's row is dropped there. The journal line above is complete. Reported in the PR; not changed here.
    assert ("ema_cross_atr_v1", "BTC/USDT", "skipped", "strategy_slot_lost_to") in decisions()


def test_a_refused_donchian_signal_falls_through_to_ema_then_rsi(db, monkeypatch):
    st, client, aid = all_three(monkeypatch, db)
    seed_closed(aid, "donchian_breakout_v1", "BTC/USDT", [-5.0], minutes_ago=30)  # Donchian is in its cooldown on BTC
    run_cycle(aid, st)
    assert [(t.strategy_id, t.pair) for t in all_trades(aid) if t.status == "open"] == [("ema_cross_atr_v1", "BTC/USDT")]
    d2 = db / "t2"
    d2.mkdir()
    init_db(f"sqlite:///{d2}/t.db")
    st, client, aid = all_three(monkeypatch, d2, ema_min_gap_atr=5.0)  # now the EMA gap guard refuses EMA too
    seed_closed(aid, "donchian_breakout_v1", "BTC/USDT", [-5.0], minutes_ago=30)
    run_cycle(aid, st)
    assert [(t.strategy_id, t.pair) for t in all_trades(aid) if t.status == "open"] == [("rsi_mr_v1", "BTC/USDT")]


def test_donchian_is_tried_before_ema_whatever_the_pair(db, monkeypatch):
    st, client, aid = all_three(monkeypatch, db, pair_list=PAIRS, max_open_positions=3)
    seen = []
    orig = trader._maybe_enter

    async def spy(session, account, client_, signal, price, settings_, notifier, **kw):
        seen.append(type(signal).__name__)
        return await orig(session, account, client_, signal, price, settings_, notifier, **kw)

    monkeypatch.setattr(trader, "_maybe_enter", spy)
    monkeypatch.setattr(trader, "_market_grade", lambda *a, **k: mt.Grade(3, ()))
    run_cycle(aid, st)
    assert seen[0] == "DonchianSignal"
    assert len(all_trades(aid)) == 1
    run_cycle(aid, st)
    assert len(all_trades(aid)) == 2  # one new entry per cycle


def test_a_held_trade_blocks_every_other_strategy_on_that_pair(db, monkeypatch):
    st, client, aid = all_three(monkeypatch, db)
    with session_scope() as s:
        s.add(Trade(account_id=aid, side="long", is_paper=True, entry_price=103.0, quantity=1.0, stop_loss_price=1.0, status="open", pair="BTC/USDT",
                    strategy_id="rsi_mr_v1", opened_at=datetime.now(U)))
    monkeypatch.setattr(rsi_mr, "exit_check", lambda **k: None)
    run_cycle(aid, st)
    assert len(all_trades(aid)) == 1


# ====================================================================== exits through the cycle
def seed_don_trade(aid, *, entry, stop, qty=1.0, side="long", pair="BTC/USDT", opened=None):
    with session_scope() as s:
        s.add(Trade(account_id=aid, side=side, is_paper=True, entry_price=entry, quantity=qty, stop_loss_price=stop, status="open", pair=pair,
                    strategy_id="donchian_breakout_v1", opened_at=opened or datetime.now(U) - timedelta(minutes=1)))


def exit_setup(db, monkeypatch, rows, *, entry=103.0, stop=100.0, side="long", pre=None, **over):
    cs = list(pre) if pre is not None else base(40)
    start = len(cs)
    cs += [cd(start + i, *r) for i, r in enumerate(rows)]
    st = settings(**over)
    data = raw(cs)
    client = spot_client(ohlcv_by_pair={p: data for p in ("BTC/USDT", "ETH/USDT", "BNB/USDT")}, ohlcv=data)
    install(monkeypatch, client)
    monkeypatch.setattr(trader, "generate_signal", lambda *a, **k: NO_EMA)
    aid = paper_account(st)
    end = int(time.time() * 1000) // HOUR * HOUR  # the last candle has just closed; the first row started len(rows) hours earlier
    first_row_start = end - len(rows) * HOUR
    seed_don_trade(aid, entry=entry, stop=stop, side=side, opened=datetime.fromtimestamp((first_row_start + 60_000) / 1000, tz=U))  # entered a minute into it
    return st, client, aid


def test_stop_exit_through_the_cycle(db, monkeypatch):
    st, client, aid = exit_setup(db, monkeypatch, [(103, 103.5, 99.5, 100.5)])
    note = run_cycle(aid, st)
    t = all_trades(aid)[0]
    assert t.status == "closed" and t.close_reason == "stop_loss" and math.isclose(t.exit_price, 100.0) and math.isclose(t.pnl, -3.0)
    with session_scope() as s:
        assert math.isclose(s.get(TradingAccount, aid).paper_balance, 9_997.0)
    assert any("Exited LONG BTC/USDT (stop_loss)" in m and "Strategy: donchian_breakout_v1" in m for m in note.texts)


def test_trailing_stop_exit_through_the_cycle(db, monkeypatch):
    rows = [(103, 110.0, 103.0, 109.0), (109, 115.0, 108.0, 114.0), (114, 114.5, 100.5, 101.0)]
    st, client, aid = exit_setup(db, monkeypatch, rows)
    run_cycle(aid, st)
    t = all_trades(aid)[0]
    assert t.close_reason == "trailing_stop" and t.exit_price > 100.0 and t.pnl > 0  # exited above the initial stop, with a profit


def test_channel_exit_through_the_cycle(db, monkeypatch):
    vol_hist = [cd(i, 120.0, 123.0 + (i % 2), 117.0 - (i % 2), 120.0) for i in range(30)]
    quiet = [cd(30 + i, 120.0, 121.0, 119.0, 120.0) for i in range(10)]
    st, client, aid = exit_setup(db, monkeypatch, [(120, 120.5, 119.2, 119.8), (119.8, 119.9, 118.0, 118.2)], entry=120.0, stop=100.0, pre=vol_hist + quiet)
    run_cycle(aid, st)
    t = all_trades(aid)[0]
    assert t.close_reason == "channel_exit" and math.isclose(t.exit_price, 118.2) and t.pnl < 0


def test_nothing_happens_while_no_exit_condition_is_met(db, monkeypatch):
    st, client, aid = exit_setup(db, monkeypatch, [(103, 103.5, 102.5, 103.0)] * 5)
    run_cycle(aid, st)
    assert all_trades(aid)[0].status == "open"


def test_a_trade_opened_by_donchian_keeps_its_own_exit_rules_if_the_switch_is_turned_off(db, monkeypatch):
    st, client, aid = exit_setup(db, monkeypatch, [(103, 103.5, 99.5, 100.5)], donchian_enabled=False)
    run_cycle(aid, st)
    t = all_trades(aid)[0]
    assert t.status == "closed" and t.close_reason == "stop_loss" and math.isclose(t.exit_price, 100.0)  # its own stop, not the EMA rules


def test_with_the_switch_off_an_open_donchian_trade_is_managed_but_no_entry_is_searched(db, monkeypatch):
    st, client, aid = exit_setup(db, monkeypatch, [(103, 103.5, 102.5, 103.0)] * 3, donchian_enabled=False)
    monkeypatch.setattr(D, "signal_from_candles", lambda *a, **k: (_ for _ in ()).throw(AssertionError("entry search ran with the switch off")))
    run_cycle(aid, st)
    assert len(all_trades(aid)) == 1


def test_no_log_line_or_notification_carries_customer_data(db, monkeypatch, caplog):
    st, client, aid = setup(db, monkeypatch, with_last(LONG_ROW))
    with caplog.at_level(logging.DEBUG):
        note = run_cycle(aid, st)
    blob = "\n".join([r.getMessage() for r in caplog.records] + note.texts)
    assert "don@example.com" not in blob and "paper-key" not in blob and "paper-secret" not in blob


def test_the_portfolio_cap_uses_the_donchian_risk_cap_for_the_new_trade(db):
    st = settings(max_open_positions=3, max_total_risk_pct=1.5)
    aid = paper_account(st)  # the account's own risk is 1.0
    with session_scope() as s:
        s.add(Trade(account_id=aid, side="long", is_paper=True, entry_price=90.0, quantity=1.0, stop_loss_price=89.0, status="open", pair="ETH/USDT",
                    strategy_id="ema_cross_atr_v1", opened_at=datetime.now(U)))
    don = dataclasses.replace(D.DonchianSignal("long", "x"), atr_pct=0.05)
    ema = StrategySignal(SignalType.BULLISH_CROSSOVER, "x", 100.0, 99.0, 1.0, 0.05, gap_atr=1.0)
    with session_scope() as s:
        acct = s.get(TradingAccount, aid)
        d = [h.code for h in g.evaluate(s, acct, st, signal=don, is_paper=True, now=datetime.now(U), strategy_id="donchian_breakout_v1")]
        e = [h.code for h in g.evaluate(s, acct, st, signal=ema, is_paper=True, now=datetime.now(U))]
    assert g.R_PORTFOLIO_CAP not in d  # 1.0 open + 0.5 new = 1.5, within the cap
    assert g.R_PORTFOLIO_CAP in e  # 1.0 open + 1.0 new = 2.0, over it
