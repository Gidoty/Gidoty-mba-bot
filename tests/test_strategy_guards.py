"""Strategy guards (src/mba_bot/strategy_guards.py, docs/safety/strategy_guards.md): what limits the downside of the EMA strategy and
the controls shared by every strategy. All switch-gated: STRATEGY_GUARDS_MODE off (default) = today's behaviour exactly.

PROVES: each guard's boundary, that off changes nothing, that observe changes nothing and writes nothing, that enforce refuses a NEW
entry with a specific reason and no order, that the kill switch stays authoritative, that pauses persist and only the owner ends an
expectancy pause. DOES NOT PROVE: that any threshold helps in real markets (nothing here is a backtest of real data), the real fee tier,
real slippage, or anything about Binance. The fake exchange proves control flow only."""
from __future__ import annotations

import json
import logging
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from helpers_live import (BULLISH_OHLCV, ENTRY_PRICE, FakeNotifier, all_trades, install, make_settings, new_account, open_protected, run_cycle, spot_client,
                          the_trade)

from mba_bot import db as db_mod
from mba_bot import kill_switch, strategy_guards as g, strategy_pause
from mba_bot.config import load_settings, parse_strategy_guards_mode
from mba_bot.db import init_db, session_scope
from mba_bot.models import DecisionLog, StrategyPause, Trade, TradingAccount
from mba_bot.strategy import SignalType, StrategySignal, candles_from_ohlcv, generate_signal

U = timezone.utc
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=U)
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")
    return tmp_path


# ====================================================================== pure decisions
@pytest.mark.parametrize("gap,minimum,fires", [(0.17, 0.0, False), (0.17, 0.18, True), (0.18, 0.18, False), (0.1799, 0.18, True), (0.5, 0.18, False),
                                              (None, 0.18, True), (float("nan"), 0.18, True), (None, 0.0, False), (0.0, 0.01, True)])
def test_ema_gap_boundary(gap, minimum, fires):
    hit = g.ema_gap_hit(gap, minimum)
    assert (hit is not None) is fires
    if hit:
        assert hit.code == g.R_EMA_GAP and not hit.creates_pause and hit.message is None  # a signal filter: never sent to the customer


@pytest.mark.parametrize("atr_pct,margin,fires", [
    (0.0042, 2.0, False),   # expected 0.42% vs cost 0.21%: ratio exactly 2.0 passes
    (0.00419, 2.0, True),   # just below
    (0.01, 2.0, False), (0.001, 2.0, True), (0.001, 0.0, False), (None, 2.0, True), (0.0, 2.0, True), (-1.0, 2.0, True), (float("nan"), 2.0, True)])
def test_fee_guard_boundary(atr_pct, margin, fires):
    # round-trip cost = (2 x 5 + 11) bps = 0.21 percent; expected move = 1 ATR
    hit = g.fee_guard_hit(atr_pct=atr_pct, margin=margin, fee_bps=5.0, slippage_bps=11.0, expected_move_atr=1.0)
    assert (hit is not None) is fires
    if hit:
        assert hit.code == g.R_FEE_GUARD and hit.message and "trading costs" in hit.message


def test_fee_guard_uses_round_trip_fees_plus_slippage():
    cost = g.fee_guard_hit(atr_pct=0.001, margin=1.0, fee_bps=5.0, slippage_bps=11.0, expected_move_atr=1.0)
    assert cost.detail["cost_pct"] == 0.21 and cost.detail["expected_pct"] == 0.1
    assert g.fee_guard_hit(atr_pct=0.001, margin=1.0, fee_bps=0.0, slippage_bps=0.0, expected_move_atr=1.0) is None
    assert g.fee_guard_hit(atr_pct=0.001, margin=1.0, fee_bps=5.0, slippage_bps=11.0, expected_move_atr=3.0) is None  # a bigger expected move passes


@pytest.mark.parametrize("n_open,risk,new,maxp,maxr,fires", [
    (0, 0.0, 1.0, 1, 2.0, False), (1, 1.0, 1.0, 1, 2.0, True), (1, 1.0, 1.0, 2, 2.0, False), (1, 1.0, 1.5, 2, 2.0, True), (1, 1.0, 1.0, 0, 0.0, False),
    (0, 0.0, 2.0, 1, 2.0, False), (0, 0.0, 2.01, 1, 2.0, True), (2, 2.0, 0.5, 3, 2.0, True), (2, 1.5, 0.5, 3, 2.0, False)])
def test_portfolio_caps(n_open, risk, new, maxp, maxr, fires):
    hit = g.portfolio_hit(open_positions=n_open, open_risk_pct=risk, new_risk_pct=new, max_positions=maxp, max_total_risk_pct=maxr)
    assert (hit is not None) is fires


@pytest.mark.parametrize("hours_ago,cooldown,fires", [(1, 4, True), (3.99, 4, True), (4, 4, False), (5, 4, False), (0.1, 0, False)])
def test_cooldown_boundary(hours_ago, cooldown, fires):
    assert (g.cooldown_hit(NOW - timedelta(hours=hours_ago), NOW, cooldown) is not None) is fires
    assert g.cooldown_hit(None, NOW, 4) is None


@pytest.mark.parametrize("pnl,capital,limit,fires", [(-30.0, 1000.0, 3.0, True), (-29.99, 1000.0, 3.0, False), (-100.0, 1000.0, 0.0, False), (50.0, 1000.0, 3.0, False),
                                                     (-1.0, None, 3.0, True), (-1.0, 0.0, 3.0, True), (-1.0, float("nan"), 3.0, True)])
def test_daily_loss_boundary(pnl, capital, limit, fires):
    hit = g.daily_loss_hit(net_pnl_today=pnl, capital=capital, limit_pct=limit)
    assert (hit is not None) is fires
    if hit and capital and capital == capital and capital > 0:
        assert hit.owner_alert and not hit.creates_pause


def test_consecutive_losses_and_the_streak_guard():
    assert g.consecutive_losses([-1, -2, -3, 5, -1]) == 3 and g.consecutive_losses([0.0, -1]) == 0 and g.consecutive_losses([]) == 0
    assert g.loss_streak_hit([-1, -1, 4], 3, 24) is None
    hit = g.loss_streak_hit([-1, -1, -1, 4], 3, 24)
    assert hit.code == g.R_LOSS_STREAK and hit.creates_pause and hit.pause_hours == 24 and hit.owner_alert
    assert g.loss_streak_hit([-1, -1, -1], 3, 0).pause_hours is None  # 0 hours means until the owner releases it
    assert g.loss_streak_hit([-1, -1, -1], 0, 24) is None


def test_expectancy_guard_waits_for_a_full_window_and_never_resumes_by_itself():
    assert g.expectancy_hit([-1.0] * 19, 20) is None  # not enough trades to judge
    hit = g.expectancy_hit([-0.2] * 20, 20)
    assert hit.code == g.R_EXPECTANCY and hit.creates_pause and hit.pause_hours is None and hit.owner_alert
    assert g.expectancy_hit([0.0] * 20, 20) is None  # zero is not negative
    assert g.expectancy_hit([1.0, 1.0] + [-0.1] * 18, 20) is None  # mean +0.01
    assert g.expectancy_hit([1.0, 1.0] + [-0.2] * 18, 20) is not None
    assert g.expectancy_hit([-5.0] * 30 + [], 0) is None
    # only the newest `window` trades count
    assert g.expectancy_hit([0.5] * 20 + [-9.0] * 30, 20) is None


def trade_row(*, pnl, entry=100.0, stop=90.0, qty=1.0, exit_price=None, closed=NOW, reason="crossover_exit", paper=False, pair="BTC/USDT", status="closed"):
    t = Trade(account_id="a", side="long", is_paper=paper, entry_price=entry, quantity=qty, stop_loss_price=stop,
              exit_price=exit_price if exit_price is not None else (None if pnl is None else (entry + pnl / qty if qty else entry)),
              pnl=pnl, closed_at=closed, close_reason=reason, status=status, pair=pair)
    return t


def test_trade_result_models_fees_and_r():
    r = g.trade_result(trade_row(pnl=-10.0), 5.0)
    assert math.isclose(r.net, -10.0 - 0.0005 * (100.0 + 90.0)) and math.isclose(r.net_r, r.net / 10.0)
    assert g.trade_result(trade_row(pnl=-10.0), 0.0).net == -10.0
    w = g.trade_result(trade_row(pnl=20.0), 5.0)
    assert w.net < 20.0 and w.net_r > 0
    for bad in (trade_row(pnl=None), trade_row(pnl=1.0, closed=None), trade_row(pnl=1.0, stop=100.0), trade_row(pnl=1.0, qty=0.0)):
        assert g.trade_result(bad, 5.0) is None
    odd = trade_row(pnl=1.0)
    odd.exit_price = None
    assert g.trade_result(odd, 5.0) is None


def test_pause_activity_and_window_start():
    def p(code, *, paused=0, resume=None, released=None):
        return StrategyPause(account_id="a", strategy_id=g.STRATEGY_ID, code=code, is_paper=False, paused_at=NOW + timedelta(hours=paused),
                             resume_at=None if resume is None else NOW + timedelta(hours=resume), released_at=None if released is None else NOW + timedelta(hours=released))
    assert g.is_active(p(g.R_LOSS_STREAK), NOW) and g.is_active(p(g.R_LOSS_STREAK, resume=5), NOW) and not g.is_active(p(g.R_LOSS_STREAK, resume=-1), NOW)
    assert not g.is_active(p(g.R_EXPECTANCY, released=-1), NOW) and g.is_active(p(g.R_EXPECTANCY, paused=-24 * 30), NOW)  # an expectancy pause never times out
    rows = [p(g.R_LOSS_STREAK, paused=-30, resume=-20), p(g.R_LOSS_STREAK, paused=-10, resume=10), p(g.R_EXPECTANCY, paused=-50, released=-40)]
    assert g.window_start(rows, g.R_LOSS_STREAK, NOW) == NOW - timedelta(hours=20)  # the one that ended; the running one does not count yet
    assert g.window_start(rows, g.R_EXPECTANCY, NOW) == NOW - timedelta(hours=40) and g.window_start([], g.R_EXPECTANCY, NOW) is None


# ====================================================================== settings
def test_defaults_change_nothing(monkeypatch):
    for k in ("STRATEGY_GUARDS_MODE", "EMA_MIN_GAP_ATR", "EMA_STOP_COOLDOWN_HOURS", "FEE_GUARD_MARGIN", "STRATEGY_DAILY_LOSS_PCT", "STRATEGY_PAUSE_AFTER_LOSSES"):
        monkeypatch.delenv(k, raising=False)
    s = load_settings()
    assert s.strategy_guards_mode == "off"
    assert (s.ema_min_gap_atr, s.ema_stop_cooldown_hours, s.fee_guard_margin, s.strategy_daily_loss_pct, s.strategy_pause_after_losses) == (0.0, 0.0, 0.0, 0.0, 0)
    assert (s.max_open_positions, s.max_total_risk_pct, s.strategy_expectancy_window) == (1, 2.0, 20)


@pytest.mark.parametrize("raw,mode", [(None, "off"), ("", "off"), ("1", "off"), ("true", "off"), ("on", "off"), ("log", "off"), ("Observe", "observe"), (" ENFORCE ", "enforce"), ("enforce ", "enforce")])
def test_the_mode_is_off_unless_named(raw, mode):
    assert parse_strategy_guards_mode(raw) == mode  # never enforce, never observe, by accident


def test_settings_are_clamped(monkeypatch):
    for k, v in {"EMA_MIN_GAP_ATR": "99", "EMA_STOP_COOLDOWN_HOURS": "-5", "FEE_GUARD_MARGIN": "1000", "MAX_OPEN_POSITIONS": "999", "STRATEGY_EXPECTANCY_WINDOW": "-3"}.items():
        monkeypatch.setenv(k, v)
    s = load_settings()
    assert s.ema_min_gap_atr == 5.0 and s.fee_guard_margin == 100.0 and s.ema_stop_cooldown_hours == 0.0 and s.max_open_positions == 50 and s.strategy_expectancy_window == 0


# ====================================================================== the signal carries the gap
def test_the_signal_reports_the_gap_in_atrs_and_is_otherwise_unchanged():
    from helpers_live import STRATEGY_KWARGS
    sig = generate_signal(candles_from_ohlcv(BULLISH_OHLCV), **STRATEGY_KWARGS)
    assert sig.signal == SignalType.BULLISH_CROSSOVER and math.isclose(sig.gap_atr, abs(sig.fast_ema - sig.slow_ema) / sig.atr)
    flat = generate_signal(candles_from_ohlcv([[i, 100.0, 100.5, 99.5, 100.0, 1.0] for i in range(20)]), **STRATEGY_KWARGS)
    assert flat.signal == SignalType.NONE and flat.gap_atr is None
    assert StrategySignal(SignalType.NONE, "x", None, None, None, None).gap_atr is None  # old constructions still work


# ====================================================================== database reads
def acct(db, **kw):
    settings = make_settings()
    return new_account(settings, **kw)


def add_trades(account_id, rows):
    with session_scope() as s:
        for t in rows:
            t.account_id = account_id
            s.add(t)


def evaluate(account_id, settings, *, paper=False, now=NOW, signal=None):
    with session_scope() as s:
        a = s.get(TradingAccount, account_id)
        sig = signal or StrategySignal(SignalType.BULLISH_CROSSOVER, "x", 1.0, 1.0, 1.0, 0.05, 0.5)
        return g.evaluate(s, a, settings, signal=sig, is_paper=paper, now=now)


def test_a_loss_streak_uses_net_results_of_strategy_trades_only(db):
    aid = acct(db)
    st = make_settings(strategy_pause_after_losses=3, strategy_expectancy_window=0, max_open_positions=0, max_total_risk_pct=0.0)
    add_trades(aid, [trade_row(pnl=-5.0, closed=NOW - timedelta(hours=h)) for h in (1, 2)] + [trade_row(pnl=-5.0, closed=NOW - timedelta(hours=3), reason=reason) for reason in g.INCIDENT_REASONS])
    assert [h.code for h in evaluate(aid, st)] == []  # incident exits are not strategy results: only 2 real losses
    add_trades(aid, [trade_row(pnl=-5.0, closed=NOW - timedelta(hours=0.5))])
    assert [h.code for h in evaluate(aid, st)] == [g.R_LOSS_STREAK]
    add_trades(aid, [trade_row(pnl=+20.0, closed=NOW - timedelta(minutes=1))])
    assert evaluate(aid, st) == []  # a win ends the streak


def test_a_tiny_win_that_fees_turn_into_a_loss_counts_as_a_loss(db):
    aid = acct(db)
    st = make_settings(strategy_pause_after_losses=2, strategy_expectancy_window=0, max_open_positions=0, max_total_risk_pct=0.0)
    add_trades(aid, [trade_row(pnl=0.05, closed=NOW - timedelta(hours=h)) for h in (1, 2)])  # gross +0.05 but fees are about 0.1
    assert [h.code for h in evaluate(aid, st)] == [g.R_LOSS_STREAK]
    assert evaluate(aid, make_settings(strategy_pause_after_losses=2, fee_guard_fee_bps=0.0, strategy_expectancy_window=0, max_open_positions=0, max_total_risk_pct=0.0)) == []


def test_paper_and_live_results_are_kept_apart(db):
    aid = acct(db)
    st = make_settings(strategy_pause_after_losses=2, strategy_expectancy_window=0, max_open_positions=0, max_total_risk_pct=0.0)
    add_trades(aid, [trade_row(pnl=-5.0, closed=NOW - timedelta(hours=h), paper=True) for h in (1, 2, 3)])
    assert evaluate(aid, st, paper=False) == [] and [h.code for h in evaluate(aid, st, paper=True)] == [g.R_LOSS_STREAK]


def test_daily_loss_counts_today_utc_only(db):
    aid = acct(db, allocated_capital=1000.0)
    st = make_settings(strategy_daily_loss_pct=3.0, strategy_expectancy_window=0, max_open_positions=0, max_total_risk_pct=0.0, fee_guard_fee_bps=0.0)
    add_trades(aid, [trade_row(pnl=-40.0, closed=NOW.replace(hour=0, minute=0) - timedelta(minutes=1))])  # yesterday 23:59 UTC
    assert evaluate(aid, st) == []
    add_trades(aid, [trade_row(pnl=-20.0, closed=NOW.replace(hour=0, minute=0, second=1)), trade_row(pnl=-9.0, closed=NOW - timedelta(hours=1))])
    assert evaluate(aid, st) == []  # 29 of 1000 = 2.9 percent
    add_trades(aid, [trade_row(pnl=-1.0, closed=NOW - timedelta(minutes=5))])
    assert [h.code for h in evaluate(aid, st)] == [g.R_DAILY_LOSS]  # exactly 3.0
    assert evaluate(aid, st, now=NOW + timedelta(days=1)) == []  # the next UTC day starts clean


def test_cooldown_applies_to_stops_on_this_pair_only(db):
    aid = acct(db)
    st = make_settings(ema_stop_cooldown_hours=4.0, strategy_expectancy_window=0, max_open_positions=0, max_total_risk_pct=0.0)
    add_trades(aid, [trade_row(pnl=-5.0, closed=NOW - timedelta(hours=1), reason="crossover_exit")])
    assert evaluate(aid, st) == []  # only a STOP LOSS starts a cooldown
    add_trades(aid, [trade_row(pnl=-5.0, closed=NOW - timedelta(hours=1), reason="stop_loss", pair="ETH/USDT")])
    assert evaluate(aid, st) == []  # another pair
    add_trades(aid, [trade_row(pnl=-5.0, closed=NOW - timedelta(hours=3), reason="stop_loss")])
    hits = evaluate(aid, st)
    assert [h.code for h in hits] == [g.R_STOP_COOLDOWN] and hits[0].detail["minutes_left"] in (59, 60)
    assert evaluate(aid, st, now=NOW + timedelta(hours=1, seconds=1)) == []


def test_an_expectancy_pause_needs_a_full_window_and_a_release_starts_a_fresh_one(db):
    aid = acct(db)
    st = make_settings(strategy_expectancy_window=20, max_open_positions=0, max_total_risk_pct=0.0)
    add_trades(aid, [trade_row(pnl=-3.0, closed=NOW - timedelta(hours=h + 1)) for h in range(19)])
    assert evaluate(aid, st) == []
    add_trades(aid, [trade_row(pnl=-3.0, closed=NOW - timedelta(minutes=30))])
    hits = evaluate(aid, st)
    assert [h.code for h in hits] == [g.R_EXPECTANCY]
    with session_scope() as s:  # the pause row the trader would write
        g.create_pause(s, aid, g.STRATEGY_ID, hits[0], is_paper=False, now=NOW)
    assert g.R_PAUSED in [h.code for h in evaluate(aid, st, now=NOW + timedelta(days=60))]  # no timeout, ever
    with session_scope() as s:
        assert g.release_pauses(s, aid, g.STRATEGY_ID, is_paper=None, now=NOW + timedelta(days=61)) == 1
    later = NOW + timedelta(days=62)
    assert evaluate(aid, st, now=later) == []  # the old 20 losses no longer count: a fresh window
    add_trades(aid, [trade_row(pnl=-3.0, closed=later - timedelta(minutes=h + 1)) for h in range(20)])
    assert [h.code for h in evaluate(aid, st, now=later)] == [g.R_EXPECTANCY]  # and the new ones can pause again


def test_a_loss_streak_pause_times_out_and_the_same_trades_do_not_re_pause(db):
    aid = acct(db)
    st = make_settings(strategy_pause_after_losses=3, strategy_streak_pause_hours=24.0, strategy_expectancy_window=0, max_open_positions=0, max_total_risk_pct=0.0)
    add_trades(aid, [trade_row(pnl=-3.0, closed=NOW - timedelta(hours=h + 1)) for h in range(3)])
    hit = evaluate(aid, st)[0]
    with session_scope() as s:
        row = g.create_pause(s, aid, g.STRATEGY_ID, hit, is_paper=False, now=NOW)
        assert row.resume_at is not None
    assert evaluate(aid, st, now=NOW + timedelta(hours=23))[0].code == g.R_PAUSED  # the existing pause is first, so enforce acts on it and writes no second one
    assert evaluate(aid, st, now=NOW + timedelta(hours=24, seconds=1)) == []  # ended: the three old losses are not counted again


def test_portfolio_numbers_count_non_terminal_trades(db):
    aid = acct(db)
    st = make_settings(strategy_expectancy_window=0, max_open_positions=1, max_total_risk_pct=2.0)
    add_trades(aid, [trade_row(pnl=0.0, status="open", closed=None)])
    assert [h.code for h in evaluate(aid, st)] == [g.R_PORTFOLIO_CAP]


def test_one_position_per_account_is_a_database_guarantee():
    """The cap on open positions is structural today: the partial unique index on trades."""
    src = (ROOT / "src/mba_bot/models.py").read_text()
    assert "status NOT IN ('closed', 'void')" in src and "unique=True" in src.split("status NOT IN ('closed', 'void')")[0][-300:]


# ====================================================================== the trader: off, observe, enforce
def decisions(code=None):
    with session_scope() as s:
        rows = s.query(DecisionLog).all()
    return [(d.outcome, d.reason_code) for d in rows if code is None or d.reason_code == code]


def settings_for(tmp_path, **over):
    base = dict(max_notional_multiplier=100.0, trade_telemetry=True, kill_switch_file=str(tmp_path / "KILL_SWITCH"), kill_switch_read_retry_seconds=0.0,
                kill_switch_read_retry_interval_seconds=0.01, owner_alert_chat_id="777")
    base.update(over)
    return make_settings(**base)


def setup_cycle(db, monkeypatch, email="guards@example.com", **over):
    st = settings_for(db, **over)
    client = spot_client()
    install(monkeypatch, client)
    aid = new_account(st, email=email)
    return st, client, aid


def orders(client):
    return [c for c in client.calls if c[0] == "create_order"]


@pytest.fixture
def owner(monkeypatch):
    sent = []

    class Rec:
        def __init__(self, token):
            pass

        async def send(self, target, message):
            sent.append((target, message))

    monkeypatch.setattr(g, "TelegramNotifier", Rec)
    return sent


def test_with_the_mode_off_the_guards_never_run_and_the_entry_is_taken(db, monkeypatch):
    from mba_bot import trader

    def boom(*a, **k):
        raise AssertionError("a guard ran while the mode was off")
    monkeypatch.setattr(g, "evaluate", boom)
    monkeypatch.setattr(trader, "_strategy_guards_block", boom)  # not even entered
    st, client, aid = setup_cycle(db, monkeypatch, ema_min_gap_atr=5.0, fee_guard_margin=1e6, strategy_daily_loss_pct=0.0001)  # thresholds set, mode left off
    run_cycle(aid, st)
    assert the_trade(aid).status == "open" and len(orders(client)) >= 1


def test_observe_logs_what_it_would_refuse_and_blocks_nothing(db, monkeypatch, caplog, owner):
    caplog.set_level(logging.WARNING)
    st, client, aid = setup_cycle(db, monkeypatch, strategy_guards_mode="observe", ema_min_gap_atr=0.5, fee_guard_margin=1e6, ema_stop_cooldown_hours=4.0)
    add_trades(aid, [trade_row(pnl=-5.0, closed=datetime.now(U) - timedelta(hours=1), reason="stop_loss")])
    run_cycle(aid, st)
    assert the_trade(aid).status in ("open",) and len(orders(client)) >= 1  # NOT blocked
    seen = {r for o, r in decisions() if o == "observed"}
    assert {g.R_EMA_GAP, g.R_FEE_GUARD, g.R_STOP_COOLDOWN} <= seen
    assert not any(o == "rejected" and r in g.ALL_CODES for o, r in decisions())
    assert any("[observe] strategy guard" in m for m in caplog.messages)
    with session_scope() as s:
        assert s.query(StrategyPause).count() == 0  # observe writes nothing
    assert owner == []  # and alerts nobody


def test_enforce_gap_filter_skips_silently_and_places_no_order(db, monkeypatch, owner):
    st, client, aid = setup_cycle(db, monkeypatch, strategy_guards_mode="enforce", ema_min_gap_atr=0.5)
    n = run_cycle(aid, st)
    assert orders(client) == [] and all_trades(aid) == []
    assert (("rejected", g.R_EMA_GAP)) in decisions() and not any("did not open a trade" in t for t in n.texts)  # a signal filter: no customer message
    with session_scope() as s:
        assert s.get(TradingAccount, aid).last_error is None
    st2, client2, aid2 = setup_cycle(db, monkeypatch, email="two@example.com", strategy_guards_mode="enforce", ema_min_gap_atr=0.1)  # gap of the fixture is 0.1738
    run_cycle(aid2, st2)
    assert the_trade(aid2).status == "open"


def test_enforce_fee_guard_refuses_with_a_customer_message(db, monkeypatch):
    st, client, aid = setup_cycle(db, monkeypatch, strategy_guards_mode="enforce", fee_guard_margin=1000.0)
    n = run_cycle(aid, st)
    assert orders(client) == [] and all_trades(aid) == [] and ("rejected", g.R_FEE_GUARD) in decisions()
    assert any("did not open a trade" in t and "trading costs" in t for t in n.texts)
    with session_scope() as s:
        assert "trading costs" in s.get(TradingAccount, aid).last_error
    n2 = run_cycle(aid, st, n)
    assert len([t for t in n2.texts if "trading costs" in t]) == 1  # not repeated every round


def test_enforce_cooldown_after_a_stop(db, monkeypatch):
    st, client, aid = setup_cycle(db, monkeypatch, strategy_guards_mode="enforce", ema_stop_cooldown_hours=4.0)
    add_trades(aid, [trade_row(pnl=-5.0, closed=datetime.now(U) - timedelta(hours=1), reason="stop_loss")])
    n = run_cycle(aid, st)
    assert orders(client) == [] and ("rejected", g.R_STOP_COOLDOWN) in decisions() and any("just stopped out" in t for t in n.texts)
    st2, client2, aid2 = setup_cycle(db, monkeypatch, email="two@example.com", strategy_guards_mode="enforce", ema_stop_cooldown_hours=4.0)
    add_trades(aid2, [trade_row(pnl=-5.0, closed=datetime.now(U) - timedelta(hours=30), reason="stop_loss")])
    run_cycle(aid2, st2)
    assert the_trade(aid2).status == "open"


def test_enforce_loss_streak_pauses_alerts_the_owner_once_and_stays_paused(db, monkeypatch, owner):
    st, client, aid = setup_cycle(db, monkeypatch, strategy_guards_mode="enforce", strategy_pause_after_losses=3, strategy_expectancy_window=0)
    add_trades(aid, [trade_row(pnl=-5.0, closed=datetime.now(U) - timedelta(hours=h + 1)) for h in range(3)])
    n = run_cycle(aid, st)
    assert orders(client) == [] and ("rejected", g.R_LOSS_STREAK) in decisions()
    with session_scope() as s:
        rows = s.query(StrategyPause).all()
        assert len(rows) == 1 and rows[0].code == g.R_LOSS_STREAK and rows[0].resume_at is not None and not rows[0].is_paper
    assert len(owner) == 1 and owner[0][0] == "777" and aid[:8] in owner[0][1] and g.R_LOSS_STREAK in owner[0][1] and "@" not in owner[0][1]
    assert any("paused while the strategy is reviewed" in t for t in n.texts)
    run_cycle(aid, st, n)
    with session_scope() as s:
        assert s.query(StrategyPause).count() == 1  # the second round is refused by the existing pause, it does not write another
    assert len(owner) == 1 and ("rejected", g.R_PAUSED) in decisions() and orders(client) == []


def test_enforce_expectancy_pause_never_resumes_until_the_owner_releases_it(db, monkeypatch, owner, capsys):
    st, client, aid = setup_cycle(db, monkeypatch, strategy_guards_mode="enforce", strategy_expectancy_window=20)
    add_trades(aid, [trade_row(pnl=-3.0, closed=datetime.now(U) - timedelta(hours=h + 1)) for h in range(20)])
    run_cycle(aid, st)
    assert orders(client) == [] and ("rejected", g.R_EXPECTANCY) in decisions()
    with session_scope() as s:
        row = s.query(StrategyPause).one()
        assert row.code == g.R_EXPECTANCY and row.resume_at is None
        row.paused_at = datetime.now(U) - timedelta(days=90)  # three months later it is still paused
    run_cycle(aid, st)
    assert orders(client) == []
    monkeypatch.setenv("DATABASE_URL", db_mod._engine.url.render_as_string(hide_password=False))
    assert strategy_pause.main(["release", "--account", aid[:8], "--note", "reviewed", "--confirm", "RELEASE"]) == 0
    assert "Released 1 pause(s)" in capsys.readouterr().out
    run_cycle(aid, st)  # the 20 old losses no longer count: the bot may enter again
    assert any(t.status == "open" for t in all_trades(aid)) and len(orders(client)) >= 1


def test_a_pause_is_saved_even_if_the_rest_of_the_cycle_fails(db, monkeypatch, owner):
    """The pause is written in its own short transaction: an error later in the same cycle must not lose it."""
    from mba_bot import trader

    async def broken(*a, **k):
        raise RuntimeError("the customer message failed")
    monkeypatch.setattr(trader, "_block_entry", broken)
    st, client, aid = setup_cycle(db, monkeypatch, strategy_guards_mode="enforce", strategy_pause_after_losses=2, strategy_expectancy_window=0)
    add_trades(aid, [trade_row(pnl=-5.0, closed=datetime.now(U) - timedelta(hours=h + 1)) for h in range(2)])
    with pytest.raises(RuntimeError):
        run_cycle(aid, st)
    with session_scope() as s:
        assert s.query(StrategyPause).count() == 1
    assert orders(client) == []


def test_enforce_daily_loss_limit(db, monkeypatch, owner):
    st, client, aid = setup_cycle(db, monkeypatch, strategy_guards_mode="enforce", strategy_daily_loss_pct=3.0, strategy_expectancy_window=0)
    now = datetime.now(U)
    add_trades(aid, [trade_row(pnl=-35.0, closed=now - timedelta(minutes=1), entry=100.0, stop=90.0)])  # allocated capital in the fixture is 1000
    if now.hour == 0 and now.minute < 2:
        pytest.skip("too close to UTC midnight")
    n = run_cycle(aid, st)
    assert orders(client) == [] and ("rejected", g.R_DAILY_LOSS) in decisions() and any("daily loss limit" in t for t in n.texts)
    assert len(owner) == 1
    run_cycle(aid, st, n)
    assert len(owner) == 1  # one owner alert per day, not one per round
    with session_scope() as s:
        assert s.query(StrategyPause).count() == 0  # a daily limit is not a pause: it ends at the next UTC day


def test_the_kill_switch_stays_authoritative_and_the_guards_never_lift_it(db, monkeypatch, owner):
    st, client, aid = setup_cycle(db, monkeypatch, strategy_guards_mode="observe", ema_min_gap_atr=0.0)  # guards would pass everything
    result = kill_switch.set_state(db_mod._engine, engaged=True, actor="test", reason="test engage", retry_seconds=0.0)
    assert result.ok

    def boom(*a, **k):
        raise AssertionError("the guards were consulted although the kill switch blocks first")
    monkeypatch.setattr(g, "evaluate", boom)
    run_cycle(aid, st)
    assert orders(client) == [] and all_trades(aid) == [] and ("rejected", "kill_switch_engaged") in decisions()


def test_enforce_fails_closed_when_a_guard_cannot_be_read_and_observe_does_not(db, monkeypatch, caplog):
    def broken(*a, **k):
        raise RuntimeError("database trouble")
    monkeypatch.setattr(g, "evaluate", broken)
    st, client, aid = setup_cycle(db, monkeypatch, strategy_guards_mode="enforce")
    n = run_cycle(aid, st)
    assert orders(client) == [] and ("rejected", g.R_UNREADABLE) in decisions() and any("safety check could not be completed" in t for t in n.texts)
    st2, client2, aid2 = setup_cycle(db, monkeypatch, email="two@example.com", strategy_guards_mode="observe")
    run_cycle(aid2, st2)
    assert the_trade(aid2).status == "open"  # observe never blocks, even when it cannot look


def test_an_open_position_is_managed_and_the_guards_are_not_consulted(db, monkeypatch):
    import dataclasses
    client, st, aid = open_protected(monkeypatch)
    st = dataclasses.replace(st, strategy_guards_mode="enforce", strategy_pause_after_losses=1)

    def boom(*a, **k):
        raise AssertionError("a guard was consulted while managing an open position")
    monkeypatch.setattr(g, "evaluate", boom)
    run_cycle(aid, st)  # nothing raises (the guards would have): the open trade is simply managed


def test_a_paused_strategy_never_stops_the_exit_of_an_open_trade():
    """The guards sit in `_maybe_enter` (new entries) only: `_maybe_exit` and the live trade management do not import or call them."""
    src = (ROOT / "src/mba_bot/trader.py").read_text()
    assert src.count("_strategy_guards_block(") == 2  # the definition and the one call
    exit_src = src.split("async def _maybe_exit(")[1].split("async def _flag_withdrawal_block")[0]
    assert "strategy_guards" not in exit_src
    enter = src.split("async def _maybe_enter(")[1]
    assert enter.index("_kill_switch_blocks(") < enter.index("_strategy_guards_block(")  # the kill switch is asked first


def test_the_guards_cannot_place_or_cancel_anything():
    import ast
    tree = ast.parse((ROOT / "src/mba_bot/strategy_guards.py").read_text())
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(x in (m or "") for m in mods for x in ("venue", "lifecycle", "exchange", "risk", "onboarding", "ccxt")), mods


# ====================================================================== the owner's command
def test_the_command_lists_and_releases_and_never_creates_a_table(db, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", db_mod._engine.url.render_as_string(hide_password=False))
    aid = acct(db)
    with session_scope() as s:
        s.add(StrategyPause(account_id=aid, strategy_id=g.STRATEGY_ID, code=g.R_EXPECTANCY, is_paper=False, paused_at=datetime.now(U), detail="trades=20, mean_net_r=-0.2"))
        s.add(StrategyPause(account_id=aid, strategy_id=g.STRATEGY_ID, code=g.R_LOSS_STREAK, is_paper=True, paused_at=datetime.now(U), resume_at=datetime.now(U) + timedelta(hours=5)))
    assert strategy_pause.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "2 active pause(s)" in out and aid[:8] in out and aid not in out and "@" not in out
    assert strategy_pause.main(["release", "--account", aid[:8], "--note", "x", "--confirm", "no"]) == 2
    assert strategy_pause.main(["release", "--account", aid[:5], "--note", "x", "--confirm", "RELEASE"]) == 2
    assert strategy_pause.main(["release", "--account", "ffffffff", "--note", "x", "--confirm", "RELEASE"]) == 2
    assert strategy_pause.main(["release", "--account", aid[:8], "--mode", "paper", "--note", "checked", "--confirm", "RELEASE"]) == 0
    assert "Released 1 pause(s)" in capsys.readouterr().out
    with session_scope() as s:
        rows = {r.code: r for r in s.query(StrategyPause).all()}
        assert rows[g.R_LOSS_STREAK].released_at is not None and rows[g.R_EXPECTANCY].released_at is None and "released: checked" in rows[g.R_LOSS_STREAK].detail


def test_the_command_does_nothing_when_the_table_does_not_exist(tmp_path, monkeypatch, capsys):
    import sqlite3
    path = tmp_path / "old.db"
    sqlite3.connect(path).execute("create table x (a int)").connection.commit()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    assert strategy_pause.main(["status"]) == 0 and "no strategy_pauses table" in capsys.readouterr().out
    assert strategy_pause.main(["release", "--account", "abcdefgh", "--note", "x", "--confirm", "RELEASE"]) == 0
    tables = {r[0] for r in sqlite3.connect(path).execute("select name from sqlite_master where type='table'")}
    assert tables == {"x"}  # nothing was created


# ====================================================================== migration and documentation
def test_the_migration_is_one_idempotent_table_on_top_of_the_notices_one(tmp_path):
    import importlib.util
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect

    path = next((ROOT / "alembic" / "versions").glob("c9f6a3b1d5e7_*.py"))
    spec = importlib.util.spec_from_file_location("pauses_migration", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    assert mig.down_revision == "b8e5f2a0c4d6"
    eng = create_engine(f"sqlite:///{tmp_path}/m.db")

    def run(fn):
        with eng.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                fn()

    run(mig.upgrade)
    run(mig.upgrade)
    assert inspect(eng).get_table_names() == ["strategy_pauses"]
    run(mig.downgrade)
    assert inspect(eng).get_table_names() == []


def test_the_weakness_table_covers_every_strategy_and_says_what_remains():
    doc = (ROOT / "docs/safety/strategy_guards.md").read_text()
    for needle in ("ema_cross_atr_v1", "rsi_mr_v1", "Donchian", "False crossovers", "Late entries", "Fee drag", "Losing streaks", "Overfitting", "stops working", "Remaining risk", "NOT MEASURED"):
        assert needle in doc, needle
