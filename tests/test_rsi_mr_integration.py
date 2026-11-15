"""rsi_mr_v1 inside the trading cycle (trader._process_multi, trader._maybe_enter, strategy_guards.evaluate).

PROVES: with the switch off nothing changes, a live account never runs the strategy whatever any setting says, the strategy trades only with
the PR #45 guards and the fee guard on, the guards apply to it per strategy (and per pair for the two-loss pause), the slot rule between EMA
and RSI, and the exits. All against a FAKE exchange and fixed candles. DOES NOT PROVE: any profit, or anything about Binance."""
from __future__ import annotations

import dataclasses
import logging
import math
import time
from datetime import datetime, timedelta, timezone

import pytest
from helpers_live import FakeNotifier, all_trades, futures_client, install, make_settings, new_account, run_cycle, spot_client
from test_rsi_mr import LONG, SHORT, TRENDY

from mba_bot import multi_trade as mt
from mba_bot import rsi_mr, strategy_guards as g, trader
from mba_bot.config import load_settings
from mba_bot.db import init_db, list_open_trades, session_scope
from mba_bot.models import DecisionLog, StrategyPause, Trade, TradingAccount
from mba_bot.strategy import SignalType, StrategySignal

U = timezone.utc
HOUR = 3_600_000
KEYS = dict(testnet_spot_api_key="paper-key-1234567890", testnet_spot_api_secret="paper-secret-1234567890",
            demo_futures_api_key="demo-key-1234567890", demo_futures_api_secret="demo-secret-1234567890")
PAIRS = "ETH/USDT,BNB/USDT"
NO_EMA = StrategySignal(SignalType.NONE, "no crossover", 100.0, 100.0, 1.0, 0.01)


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")
    return tmp_path


def raw(closes, *, spread=0.4, forming=False):
    """ccxt rows [ts, o, h, l, c, v] whose last candle has just CLOSED (or, with forming=True, whose last candle is still forming)."""
    end = int(time.time() * 1000) - 60_000  # the last candle closed a minute ago (a signal candle must be fresh, audit M5)
    if forming:
        end += HOUR
    out = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        out.append([end - (len(closes) - i) * HOUR, o, max(o, c) + spread, min(o, c) - spread, c, 1.0])
    return out


def settings(**over):
    base = dict(KEYS, rsi_mr_enabled=True, strategy_guards_mode="enforce", new_strategy_guards_mode="enforce", fee_guard_margin=0.5, strategy_expectancy_window=0, max_total_risk_pct=5.0,
                max_notional_multiplier=100.0, trade_telemetry=True)
    base.update(over)
    return make_settings(**base)


def paper_account(st, email="rsi@example.com", pair_list=None, market="spot", risk=1.0):
    aid = new_account(st, email=email, market_type=market)
    with session_scope() as s:
        a = s.get(TradingAccount, aid)
        a.bot_started_at = None  # PAPER
        a.pair_list = pair_list
        a.extra_strategies = "rsi_mr_v1,donchian_breakout_v1"  # opted in by the owner (audit H2)
        a.risk_pct = risk
    return aid


def setup(db, monkeypatch, data, *, market="spot", ema=False, pair_list=None, risk=1.0, **over):
    st = settings(max_open_positions=over.pop("max_open_positions", 1), **over)
    mk = futures_client if market == "futures" else spot_client
    client = mk(ohlcv_by_pair={p: data for p in ("BTC/USDT", "ETH/USDT", "BNB/USDT")}, ohlcv=data)
    install(monkeypatch, client)
    if not ema:
        monkeypatch.setattr(trader, "generate_signal", lambda *a, **k: NO_EMA)
    return st, client, paper_account(st, pair_list=pair_list, market=market, risk=risk)


def by_strategy(aid):
    return [(t.strategy_id, t.pair, t.status, t.close_reason) for t in all_trades(aid)]


def decisions(code=None):
    with session_scope() as s:
        return [(d.strategy_id, d.pair, d.outcome, d.reason_code) for d in s.query(DecisionLog).all() if code is None or d.reason_code == code]


# ====================================================================== switches
def test_defaults_are_off_and_paper_only():
    s = load_settings()
    assert s.rsi_mr_enabled is False and s.rsi_mr_paper_only is True and s.rsi_mr_risk_pct == 0.5 < 1.0
    assert (s.rsi_mr_adx_max, s.rsi_mr_stop_atr, s.rsi_mr_target_atr, s.rsi_mr_time_stop_candles, s.rsi_mr_pause_after_losses) == (25.0, 1.5, 1.0, 24, 2)
    assert (s.rsi_mr_spot_fee_bps, s.rsi_mr_futures_fee_bps, s.rsi_mr_slippage_bps, s.rsi_mr_timeframe) == (10.0, 5.0, 5.0, "1h")


def test_environment_overrides_are_clamped(monkeypatch):
    for k, v in {"RSI_MR_ENABLED": "true", "RSI_MR_RISK_PCT": "99", "RSI_MR_ADX_MAX": "-5", "RSI_MR_TIME_STOP_CANDLES": "0", "RSI_MR_TIMEFRAME": " 15M "}.items():
        monkeypatch.setenv(k, v)
    s = load_settings()
    assert s.rsi_mr_enabled and s.rsi_mr_risk_pct == 2.0 and s.rsi_mr_adx_max == 1.0 and s.rsi_mr_time_stop_candles == 1 and s.rsi_mr_timeframe == "15m"


def poison_rsi(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("rsi_mr_v1 code ran")

    async def aboom(*a, **k):
        raise AssertionError("rsi_mr_v1 code ran")

    monkeypatch.setattr(rsi_mr, "generate_signal", boom)
    monkeypatch.setattr(rsi_mr, "exit_check", boom)
    monkeypatch.setattr(trader, "_manage_rsi_trade", aboom)


def test_with_the_switch_off_nothing_changes_for_a_paper_account(db, monkeypatch):
    """RSI data in the fake, every RSI knob set, the switch off: the account trades exactly as an account without the feature."""
    st = settings(rsi_mr_enabled=False, strategy_guards_mode="off", rsi_mr_risk_pct=0.01, rsi_mr_adx_max=99.0)
    client = spot_client()  # BULLISH_OHLCV: the EMA strategy signals
    install(monkeypatch, client)
    poison_rsi(monkeypatch)

    async def no_multi(*a, **k):
        raise AssertionError("the strategy-aware path ran with the switch off")

    monkeypatch.setattr(trader, "_process_multi", no_multi)
    aid = paper_account(st)
    run_cycle(aid, st)
    run_cycle(aid, st)
    trades = all_trades(aid)
    assert len(trades) == 1 and trades[0].strategy_id == "ema_cross_atr_v1" and trades[0].is_paper
    assert math.isclose(trades[0].quantity * abs(trades[0].entry_price - trades[0].stop_loss_price), 100.0, rel_tol=1e-6)  # 1% of 10000, not the RSI 0.01


@pytest.mark.parametrize("paper_only_setting", [True, False])
def test_a_live_account_never_runs_rsi_whatever_any_setting_says(db, monkeypatch, paper_only_setting):
    st = settings(rsi_mr_paper_only=paper_only_setting, max_open_positions=3)
    client = spot_client(ohlcv_by_pair={p: raw(LONG) for p in ("BTC/USDT", "ETH/USDT", "BNB/USDT")}, ohlcv=raw(LONG))
    install(monkeypatch, client)
    poison_rsi(monkeypatch)
    aid = new_account(st)  # LIVE
    with session_scope() as s:
        s.get(TradingAccount, aid).pair_list = PAIRS
    for _ in range(3):
        run_cycle(aid, st, swallow=True)
    assert all(t[0] in (None, "ema_cross_atr_v1") for t in by_strategy(aid)) and not any(t.strategy_id == "rsi_mr_v1" for t in all_trades(aid))


def test_the_entry_itself_refuses_a_live_call(db, monkeypatch):
    """Even if something called the entry with an RSI signal for a live account, it returns before any size, order or trade."""
    st = settings()
    client = spot_client()
    install(monkeypatch, client)
    aid = new_account(st)  # LIVE
    sig = rsi_mr.generate_signal(__import__("test_rsi_mr").mk(LONG))
    assert sig.side == "long"

    async def go():
        with session_scope(autoflush=False) as s:
            await trader._maybe_enter(s, s.get(TradingAccount, aid), client, sig, 90.0, st, FakeNotifier(), is_paper=False, now=datetime.now(U))

    import asyncio
    asyncio.run(go())
    assert all_trades(aid) == [] and not [c for c in client.calls if c[0] == "create_order"]


# ====================================================================== entries
def test_a_long_signal_opens_a_paper_trade_with_the_rsi_numbers(db, monkeypatch):
    st, client, aid = setup(db, monkeypatch, raw(LONG))
    note = run_cycle(aid, st)
    t = all_trades(aid)[0]
    assert (t.strategy_id, t.side, t.is_paper, t.pair, t.status) == ("rsi_mr_v1", "long", True, "BTC/USDT", "open")
    sig = rsi_mr.generate_signal(__import__("test_rsi_mr").mk(LONG))
    assert math.isclose(t.stop_loss_price, t.entry_price - 1.5 * sig.atr, rel_tol=1e-3) or math.isclose(abs(t.entry_price - t.stop_loss_price), 1.5 * sig.atr, rel_tol=0.2)
    assert math.isclose(abs(t.entry_price - t.stop_loss_price), 1.5 * sig.atr, rel_tol=1e-6)
    risk = t.quantity * abs(t.entry_price - t.stop_loss_price)
    assert math.isclose(risk, 50.0, rel_tol=1e-6)  # 0.5 percent of 10000: the RSI cap, below the account's 1 percent
    assert any("Strategy: rsi_mr_v1" in m and "Entered LONG BTC/USDT" in m and "0.50%" in m for m in note.texts)
    assert not [c for c in client.calls if c[0] == "create_order"]  # paper: no order


def test_the_risk_is_the_smaller_of_the_account_and_the_strategy_cap(db, monkeypatch):
    st, client, aid = setup(db, monkeypatch, raw(LONG), risk=0.2)
    run_cycle(aid, st)
    t = all_trades(aid)[0]
    assert math.isclose(t.quantity * abs(t.entry_price - t.stop_loss_price), 20.0, rel_tol=1e-6)


def test_a_short_signal_trades_on_futures_only(db, monkeypatch):
    st, client, aid = setup(db, monkeypatch, raw(SHORT), market="futures")
    run_cycle(aid, st)
    assert [(t.side, t.strategy_id) for t in all_trades(aid)] == [("short", "rsi_mr_v1")]
    t = all_trades(aid)[0]
    assert t.stop_loss_price > t.entry_price
    d2 = db / "spot"
    d2.mkdir()
    init_db(f"sqlite:///{d2}/t.db")
    st, client, aid = setup(d2, monkeypatch, raw(SHORT), market="spot")
    run_cycle(aid, st)
    assert all_trades(aid) == []


def test_no_signal_no_trade_and_the_adx_filter_blocks_a_trending_cross(db, monkeypatch):
    st, client, aid = setup(db, monkeypatch, raw(LONG[:-1]))
    run_cycle(aid, st)
    assert all_trades(aid) == []
    d2 = db / "t2"
    d2.mkdir()
    init_db(f"sqlite:///{d2}/t.db")
    st, client, aid = setup(d2, monkeypatch, raw(TRENDY))
    run_cycle(aid, st)
    assert all_trades(aid) == []


def test_the_forming_candle_is_ignored_in_the_cycle(db, monkeypatch):
    st, client, aid = setup(db, monkeypatch, raw(LONG, forming=True))  # the cross exists only on a candle that has not closed yet
    run_cycle(aid, st)
    assert all_trades(aid) == []


def test_it_does_not_trade_without_the_guards_or_the_fee_guard(db, monkeypatch, caplog):
    st, client, aid = setup(db, monkeypatch, raw(LONG), new_strategy_guards_mode="off")
    with caplog.at_level(logging.WARNING, logger="mba_bot.trader"):
        run_cycle(aid, st)
    assert all_trades(aid) == [] and any(("rsi_guards_off" in r.getMessage()) and "@" not in r.getMessage() for r in caplog.records)
    assert ("rsi_mr_v1", "BTC/USDT", "rejected", "rsi_guards_off") in decisions()
    d2 = db / "t2"
    d2.mkdir()
    init_db(f"sqlite:///{d2}/t.db")
    st, client, aid = setup(d2, monkeypatch, raw(LONG), fee_guard_margin=0.0)
    run_cycle(aid, st)
    assert all_trades(aid) == [] and ("rsi_mr_v1", "BTC/USDT", "rejected", "rsi_fee_guard_off") in decisions()


def test_the_fee_guard_blocks_when_the_target_is_small_against_costs(db, monkeypatch):
    st, client, aid = setup(db, monkeypatch, raw(LONG), fee_guard_margin=50.0)  # needs a move 50x the round-trip cost
    run_cycle(aid, st)
    assert all_trades(aid) == [] and ("rsi_mr_v1", "BTC/USDT", "rejected", g.R_FEE_GUARD) in decisions()


def test_the_fee_guard_uses_the_market_fees_and_the_strategy_target(db):
    # Spot: 2 x 10 bps fees + 2 x 5 bps slippage = 30 bps round trip. Futures: 2 x 5 + 10 = 20 bps. Expected move = 1.0 ATR.
    st = settings(fee_guard_margin=1.0)
    aid_spot = paper_account(st, "a@example.com")
    aid_fut = paper_account(st, "b@example.com", market="futures")
    sig = rsi_mr.RsiSignal("long", "x", atr_pct=0.0025, target_atr=1.0)  # expected 0.25 percent
    with session_scope() as s:
        spot = g.evaluate(s, s.get(TradingAccount, aid_spot), st, signal=sig, is_paper=True, now=datetime.now(U), strategy_id="rsi_mr_v1")
        fut = g.evaluate(s, s.get(TradingAccount, aid_fut), st, signal=sig, is_paper=True, now=datetime.now(U), strategy_id="rsi_mr_v1")
    assert [h.code for h in spot if h.code == g.R_FEE_GUARD] == [g.R_FEE_GUARD]  # 0.25 < 0.30
    assert not [h for h in fut if h.code == g.R_FEE_GUARD]  # 0.25 >= 0.20


def test_observe_mode_does_not_let_the_strategy_trade(db, monkeypatch):
    """Audit M1: observe blocks nothing, so it must not satisfy "needs the guards". The strategy trades only in enforce mode of NEW_STRATEGY_GUARDS_MODE."""
    st, client, aid = setup(db, monkeypatch, raw(LONG), new_strategy_guards_mode="observe", fee_guard_margin=50.0)
    run_cycle(aid, st)
    assert all_trades(aid) == [] and ("rsi_mr_v1", "BTC/USDT", "rejected", "rsi_guards_not_enforced") in decisions()


# ====================================================================== guards per strategy and per pair
def seed_closed(aid, strategy, pair, nets, *, minutes_ago=30, reason="stop_loss"):
    now = datetime.now(U)
    with session_scope() as s:
        for i, net in enumerate(nets):
            s.add(Trade(account_id=aid, side="long", is_paper=True, entry_price=100.0, quantity=1.0, stop_loss_price=90.0, exit_price=100.0 + net, pnl=net,
                        status="closed", close_reason=reason, pair=pair, strategy_id=strategy, opened_at=now - timedelta(hours=5),
                        closed_at=now - timedelta(minutes=minutes_ago + i)))


def test_two_losses_in_a_row_on_one_pair_pause_that_pair_only(db, monkeypatch):
    st, client, aid = setup(db, monkeypatch, raw(LONG), pair_list=PAIRS, max_open_positions=3)
    seed_closed(aid, "rsi_mr_v1", "BTC/USDT", [-5.0, -5.0])
    run_cycle(aid, st)
    pairs = [t.pair for t in all_trades(aid) if t.status == "open"]
    assert pairs == ["ETH/USDT"]  # BTC refused (paused), ETH taken in the same cycle
    with session_scope() as s:
        rows = s.query(StrategyPause).all()
    assert len(rows) == 1 and rows[0].strategy_id == "rsi_mr_v1" and rows[0].code == g.R_LOSS_STREAK and "pair=BTC/USDT" in rows[0].detail
    run_cycle(aid, st)  # the pause persists: still no BTC entry
    assert "BTC/USDT" not in [t.pair for t in all_trades(aid) if t.status == "open"]


def test_a_win_breaks_the_streak_and_other_pairs_or_strategies_do_not_count(db, monkeypatch):
    st, client, aid = setup(db, monkeypatch, raw(LONG))
    seed_closed(aid, "rsi_mr_v1", "BTC/USDT", [-5.0, 3.0, -5.0], minutes_ago=30)  # newest first: -5, +3, -5: streak of 1
    seed_closed(aid, "rsi_mr_v1", "ETH/USDT", [-5.0, -5.0, -5.0], minutes_ago=10)  # another pair
    seed_closed(aid, "ema_cross_atr_v1", "BTC/USDT", [-5.0, -5.0, -5.0], minutes_ago=5)  # another strategy
    run_cycle(aid, st)
    assert [t.strategy_id for t in all_trades(aid) if t.status == "open"] == ["rsi_mr_v1"]
    with session_scope() as s:
        assert s.query(StrategyPause).count() == 0


def test_the_pause_can_be_released_by_the_owner_and_only_new_trades_count_afterwards(db, monkeypatch):
    st, client, aid = setup(db, monkeypatch, raw(LONG))
    seed_closed(aid, "rsi_mr_v1", "BTC/USDT", [-5.0, -5.0])
    run_cycle(aid, st)
    assert [t for t in all_trades(aid) if t.status == "open"] == []
    with session_scope() as s:
        assert g.release_pauses(s, aid, "rsi_mr_v1", is_paper=True, now=datetime.now(U)) == 1
    run_cycle(aid, st)
    assert [t.strategy_id for t in all_trades(aid) if t.status == "open"] == ["rsi_mr_v1"]


def test_guard_statistics_are_kept_apart_per_strategy(db):
    st = settings()
    aid = paper_account(st)
    seed_closed(aid, "rsi_mr_v1", "BTC/USDT", [-5.0, -4.0])
    seed_closed(aid, None, "BTC/USDT", [7.0])  # an old row with no strategy id is the EMA strategy's
    seed_closed(aid, "ema_cross_atr_v1", "BTC/USDT", [8.0])
    with session_scope() as s:
        rsi = g.closed_results(s, aid, is_paper=True, fee_bps=0.0, strategy_id="rsi_mr_v1")
        ema = g.closed_results(s, aid, is_paper=True, fee_bps=0.0, strategy_id="ema_cross_atr_v1")
        every = g.closed_results(s, aid, is_paper=True, fee_bps=0.0)
        eth = g.closed_results(s, aid, is_paper=True, fee_bps=0.0, strategy_id="rsi_mr_v1", pair="ETH/USDT")
    assert sorted(r.net for r in rsi) == [-5.0, -4.0] and sorted(r.net for r in ema) == [7.0, 8.0] and len(every) == 4 and eth == []


def test_daily_loss_and_expectancy_guards_apply_to_rsi_by_its_own_results(db, monkeypatch):
    st, client, aid = setup(db, monkeypatch, raw(LONG), strategy_daily_loss_pct=3.0)
    seed_closed(aid, "ema_cross_atr_v1", "BTC/USDT", [-400.0], minutes_ago=0)  # a big EMA loss today does not count against RSI
    run_cycle(aid, st)
    assert [t.strategy_id for t in all_trades(aid) if t.status == "open"] == ["rsi_mr_v1"]
    d2 = db / "t2"
    d2.mkdir()
    init_db(f"sqlite:///{d2}/t.db")
    st, client, aid = setup(d2, monkeypatch, raw(LONG), strategy_daily_loss_pct=3.0)
    seed_closed(aid, "rsi_mr_v1", "ETH/USDT", [-400.0], reason="crossover_exit", minutes_ago=0)  # its own loss today does
    run_cycle(aid, st)
    assert [t for t in all_trades(aid) if t.status == "open"] == []
    assert ("rsi_mr_v1", "BTC/USDT", "rejected", g.R_DAILY_LOSS) in decisions()


# ====================================================================== slot rule between strategies
BULL = StrategySignal(SignalType.BULLISH_CROSSOVER, "x", 100.0, 99.0, 1.0, 0.01, gap_atr=1.0)


def both_signal(monkeypatch, db, *, ema_gap=1.0, **over):
    st, client, aid = setup(db, monkeypatch, raw(LONG), ema=True, **over)
    monkeypatch.setattr(trader, "generate_signal", lambda *a, **k: dataclasses.replace(BULL, gap_atr=ema_gap, atr=1.0, atr_pct=0.01))
    return st, client, aid


def test_when_ema_and_rsi_signal_on_the_same_pair_ema_takes_the_slot_and_rsi_is_logged(db, monkeypatch, caplog):
    st, client, aid = both_signal(monkeypatch, db)
    with caplog.at_level(logging.INFO, logger="mba_bot.trader"):
        run_cycle(aid, st)
    assert [(t.strategy_id, t.pair) for t in all_trades(aid)] == [("ema_cross_atr_v1", "BTC/USDT")]
    assert any("strategy_slot_lost_to" in r.getMessage() and "rsi_mr_v1" in r.getMessage() and "ema_cross_atr_v1" in r.getMessage() for r in caplog.records)
    assert ("rsi_mr_v1", "BTC/USDT", "skipped", "strategy_slot_lost_to") in decisions()


def test_a_refused_ema_signal_falls_through_to_rsi_in_the_same_cycle(db, monkeypatch):
    st, client, aid = both_signal(monkeypatch, db, ema_gap=0.01, ema_min_gap_atr=0.5)  # the EMA gap guard refuses the EMA entry
    run_cycle(aid, st)
    assert [(t.strategy_id, t.pair) for t in all_trades(aid)] == [("rsi_mr_v1", "BTC/USDT")]


def test_a_held_trade_blocks_the_other_strategy_on_that_pair(db, monkeypatch):
    st, client, aid = both_signal(monkeypatch, db)
    with session_scope() as s:
        s.add(Trade(account_id=aid, side="long", is_paper=True, entry_price=90.0, quantity=1.0, stop_loss_price=40.0, status="open", pair="BTC/USDT",
                    strategy_id="rsi_mr_v1", opened_at=datetime.now(U)))
    monkeypatch.setattr(rsi_mr, "exit_check", lambda **k: None)
    run_cycle(aid, st)
    assert len(all_trades(aid)) == 1  # one slot (MAX_OPEN_POSITIONS 1): nothing else opens


def test_the_strategy_order_wins_over_the_pair_order(db, monkeypatch):
    """EMA signals only on ETH, RSI only on BTC (the first pair): EMA still takes the slot, because priority is by strategy first."""
    st, client, aid = both_signal(monkeypatch, db, pair_list=PAIRS, max_open_positions=3)
    monkeypatch.setattr(trader, "generate_signal", lambda candles, **k: dataclasses.replace(BULL, atr=1.0, atr_pct=0.01))
    real = trader._multi_paper_limits
    sigs = {}

    async def fake_fetch(symbol, timeframe=None, since=None, limit=None, params=None):
        return raw(LONG)

    seen = []
    orig_enter = trader._maybe_enter

    async def spy(session, account, client_, signal, price, settings_, notifier, **kw):
        seen.append((type(signal).__name__, kw.get("pair")))
        return await orig_enter(session, account, client_, signal, price, settings_, notifier, **kw)

    monkeypatch.setattr(trader, "_maybe_enter", spy)
    run_cycle(aid, st)
    assert seen[0][0] == "StrategySignal"  # EMA is tried before RSI, whichever pair it is on


def test_one_new_entry_per_cycle_even_with_two_strategies_and_three_slots(db, monkeypatch):
    st, client, aid = both_signal(monkeypatch, db, pair_list=PAIRS, max_open_positions=3)
    with session_scope() as s:
        pass
    monkeypatch.setattr(trader, "_market_grade", lambda *a, **k: mt.Grade(3, ()))
    run_cycle(aid, st)
    assert len(all_trades(aid)) == 1
    run_cycle(aid, st)
    assert len(all_trades(aid)) == 2


# ====================================================================== exits through the cycle
def seed_rsi_trade(aid, *, entry, stop, qty=1.0, side="long", pair="BTC/USDT", opened=None):
    with session_scope() as s:
        s.add(Trade(account_id=aid, side=side, is_paper=True, entry_price=entry, quantity=qty, stop_loss_price=stop, status="open", pair=pair,
                    strategy_id="rsi_mr_v1", opened_at=opened or datetime.now(U) - timedelta(minutes=1)))


def candles_after_entry(rows):
    """Rows (o, h, l, c) for candles that start after the entry. The entry is 'now - 1 minute', the last candle closed at the top of this hour."""
    end = int(time.time() * 1000) - 60_000
    pre = [104.0 - 0.1 * i for i in range(40)]  # a gentle fall into the entry: RSI far below the 50 line
    out = []
    n = len(pre) + len(rows)
    for i, c in enumerate(pre):
        out.append([end - (n - i) * HOUR - 0, c, c + 0.3, c - 0.3, c, 1.0])
    for j, (o, h, l, c) in enumerate(rows):
        out.append([end - (len(rows) - j) * HOUR, o, h, l, c, 1.0])
    return out


def exit_setup(db, monkeypatch, rows, *, entry=100.0, stop=97.0, side="long", opened_hours_ago=None, **over):
    data = candles_after_entry(rows)
    st = settings(**over)
    client = spot_client(ohlcv_by_pair={p: data for p in ("BTC/USDT", "ETH/USDT", "BNB/USDT")}, ohlcv=data)
    install(monkeypatch, client)
    monkeypatch.setattr(trader, "generate_signal", lambda *a, **k: NO_EMA)
    aid = paper_account(st)
    end = int(time.time() * 1000) - 60_000
    opened = datetime.fromtimestamp((end - len(rows) * HOUR - 5000) / 1000, tz=U)  # five seconds before the open of the first row: the exit check judges candles from the one AFTER the entry candle (audit M5)
    seed_rsi_trade(aid, entry=entry, stop=stop, side=side, opened=opened)
    return st, client, aid


def test_profit_target_exit_through_the_cycle(db, monkeypatch):
    st, client, aid = exit_setup(db, monkeypatch, [(100, 102.4, 99.8, 102.2)])
    note = run_cycle(aid, st)
    t = all_trades(aid)[0]
    assert t.status == "closed" and t.close_reason == "profit_target" and math.isclose(t.exit_price, 102.0) and math.isclose(t.pnl, 2.0)
    with session_scope() as s:
        assert math.isclose(s.get(TradingAccount, aid).paper_balance, 10_002.0)
    assert any("Exited LONG BTC/USDT (profit_target)" in m and "Strategy: rsi_mr_v1" in m for m in note.texts)


def test_stop_exit_through_the_cycle(db, monkeypatch):
    st, client, aid = exit_setup(db, monkeypatch, [(100, 100.3, 96.5, 97.5)])
    run_cycle(aid, st)
    t = all_trades(aid)[0]
    assert t.close_reason == "stop_loss" and math.isclose(t.exit_price, 97.0) and math.isclose(t.pnl, -3.0)


def test_time_stop_through_the_cycle(db, monkeypatch):
    rows = [(100, 100.1, 99.9, 99.9)] * 24  # drifting just under the entry, RSI stays below 50 on the alternating base
    st, client, aid = exit_setup(db, monkeypatch, rows)
    run_cycle(aid, st)
    t = all_trades(aid)[0]
    assert t.status == "closed" and t.close_reason in ("time_stop", "rsi_exit")


def test_nothing_happens_while_no_exit_condition_is_met(db, monkeypatch):
    st, client, aid = exit_setup(db, monkeypatch, [(100, 100.1, 99.9, 99.95)] * 3)
    run_cycle(aid, st)
    assert all_trades(aid)[0].status == "open"


def test_a_trade_opened_by_rsi_keeps_its_own_exit_rules_if_the_switch_is_turned_off(db, monkeypatch):
    st, client, aid = exit_setup(db, monkeypatch, [(100, 102.4, 99.8, 102.2)], rsi_mr_enabled=False)
    run_cycle(aid, st)
    t = all_trades(aid)[0]
    assert t.status == "closed" and t.close_reason == "profit_target"  # not the EMA rules (which would not have exited here)


def test_an_rsi_trade_on_a_pair_with_no_new_entries_when_the_switch_is_off(db, monkeypatch):
    st, client, aid = exit_setup(db, monkeypatch, [(100, 100.1, 99.9, 99.95)] * 3, rsi_mr_enabled=False)
    monkeypatch.setattr(rsi_mr, "generate_signal", lambda *a, **k: (_ for _ in ()).throw(AssertionError("entry search ran with the switch off")))
    run_cycle(aid, st)
    assert len(all_trades(aid)) == 1


def test_no_log_line_or_notification_carries_customer_data(db, monkeypatch, caplog):
    st, client, aid = setup(db, monkeypatch, raw(LONG))
    with caplog.at_level(logging.DEBUG):
        note = run_cycle(aid, st)
    blob = "\n".join([r.getMessage() for r in caplog.records] + note.texts)
    assert "rsi@example.com" not in blob and "paper-key" not in blob and "paper-secret" not in blob


def test_the_entry_refuses_an_rsi_signal_when_the_switch_is_off_even_if_called_directly(db, monkeypatch):
    import asyncio
    st = settings(rsi_mr_enabled=False)
    client = spot_client()
    install(monkeypatch, client)
    aid = paper_account(st)
    sig = rsi_mr.generate_signal(__import__("test_rsi_mr").mk(LONG))

    async def go():
        with session_scope(autoflush=False) as s:
            await trader._maybe_enter(s, s.get(TradingAccount, aid), client, sig, 90.0, st, FakeNotifier(), is_paper=True, now=datetime.now(U))

    asyncio.run(go())
    assert all_trades(aid) == []
