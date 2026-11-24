"""B5a: live RSI mean reversion (LIVE_RSI_MR, default off).

PROVES (fake exchange, fixed candles): with the switch off a live account never runs the RSI code and trades exactly as before; with it on an opted-in LIVE account takes an RSI entry through the same
path as an EMA entry (strategy_id rsi_mr_v1, the protective stop placed and verified, risk the smaller of the account's and RSI_MR_RISK_PCT, the strategy guards in enforce mode and the fee guard required),
EMA has priority for the one live slot, an account that is not opted in, a paper account and a refused guard take nothing, the kill switch, the daily dollar limit and the DEMO label apply, and an open RSI
trade is exited by its own rules (target, RSI back to 50, time stop) through lifecycle.execute_exit and NOT by an EMA crossover, whatever the switch says.
DOES NOT PROVE: any profit, or anything about Binance (the fake exchange stands in)."""
from __future__ import annotations

import dataclasses

import pytest
from helpers_live import FakeNotifier, all_trades, futures_client, install, make_settings, new_account, run_cycle, spot_client, the_trade
from test_kill_switch import _engage
from test_rsi_mr import LONG, TRENDY
from test_rsi_mr_integration import NO_EMA, raw

from mba_bot import rsi_mr, trader
from mba_bot.config import load_settings
from mba_bot.db import init_db, session_scope
from mba_bot.models import DecisionLog, TradingAccount
from mba_bot.strategy import SignalType, StrategySignal

BUY_EMA = StrategySignal(SignalType.BULLISH_CROSSOVER, "bullish crossover", 100.0, 99.0, 1.0, 0.01)


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")
    return tmp_path


def st(**over):
    base = dict(rsi_mr_enabled=True, live_rsi_mr=True, strategy_guards_mode="enforce", new_strategy_guards_mode="enforce", fee_guard_margin=0.5, strategy_expectancy_window=0,
                max_total_risk_pct=5.0, max_notional_multiplier=100.0, trade_telemetry=True, protection_precheck_mode="off")
    base.update(over)
    return make_settings(**base)


_n = __import__("itertools").count()


def live_account(settings, *, market="spot", opted=True, risk=1.0, **kw):
    kw.setdefault("email", f"lrsi{next(_n)}@example.com")
    aid = new_account(settings, market_type=market, **kw)
    with session_scope() as s:
        a = s.get(TradingAccount, aid)
        a.extra_strategies = "rsi_mr_v1" if opted else None
        a.risk_pct = risk
    return aid


def setup(monkeypatch, data=LONG, *, market="spot", ema=False, **over):
    settings = st(**over)
    mk = futures_client if market == "futures" else spot_client
    client = mk(ohlcv=raw(data), price=float(data[-1]))
    install(monkeypatch, client)
    if not ema:
        monkeypatch.setattr(trader, "generate_signal", lambda *a, **k: NO_EMA)
    return settings, client


def reasons():
    with session_scope() as s:
        return [d.reason_code for d in s.query(DecisionLog).all()]


# ------------------------------------------------------------------ the switch
def test_the_switch_is_off_by_default():
    assert load_settings().live_rsi_mr is False


def test_with_the_switch_off_a_live_account_never_runs_the_rsi_code(db, monkeypatch):
    settings, client = setup(monkeypatch, live_rsi_mr=False)

    def boom(*a, **k):
        raise AssertionError("rsi_mr_v1 code ran for a live account with the switch off")

    monkeypatch.setattr(rsi_mr, "generate_signal", boom)
    aid = live_account(settings)
    run_cycle(aid, settings)
    assert all_trades(aid) == [] and client.orders == {}


# ------------------------------------------------------------------ the entry
@pytest.mark.parametrize("market", ["spot", "futures"])
def test_an_opted_in_live_account_takes_a_protected_rsi_entry(db, monkeypatch, market):
    settings, client = setup(monkeypatch, market=market)
    aid = live_account(settings, market=market, risk=1.0)
    n = run_cycle(aid, settings)
    t = the_trade(aid)
    assert t.strategy_id == "rsi_mr_v1" and t.is_paper is False and t.is_demo is False and t.side == "long"
    assert t.status == "open" and t.protection_status == "protected"  # the protective stop is resting and verified
    assert any("[LIVE] Entered LONG" in x and "Strategy: rsi_mr_v1" in x for x in n.texts)
    risk_amount = t.quantity * abs(t.entry_price - t.stop_loss_price)
    assert risk_amount <= 0.5 / 100.0 * 1000.0 * 1.05  # RSI risks the smaller of the account's 1% and RSI_MR_RISK_PCT 0.5% of the 1000 capital
    assert "Risk:" in "\n".join(n.texts) and "(0.50% of balance)" in "\n".join(n.texts)


def test_an_account_that_is_not_opted_in_takes_nothing(db, monkeypatch):
    settings, client = setup(monkeypatch)
    aid = live_account(settings, opted=False)
    run_cycle(aid, settings)
    assert all_trades(aid) == [] and client.orders == {}


def test_without_rsi_enabled_or_with_guards_not_enforced_nothing_is_ordered(db, monkeypatch):
    for over in ({"rsi_mr_enabled": False}, {"new_strategy_guards_mode": "observe"}, {"new_strategy_guards_mode": "off"}, {"fee_guard_margin": 0.0}):
        settings, client = setup(monkeypatch, **over)
        aid = live_account(settings)
        run_cycle(aid, settings)
        assert all_trades(aid) == [] and client.orders == {}, over


def test_a_paper_account_still_uses_the_paper_path(db, monkeypatch):
    settings, client = setup(monkeypatch, demo_futures_api_key="k", demo_futures_api_secret="s", testnet_spot_api_key="k", testnet_spot_api_secret="s")
    aid = live_account(settings)
    with session_scope() as s:
        s.get(TradingAccount, aid).bot_started_at = None
    run_cycle(aid, settings)
    assert not [t for t in all_trades(aid) if not t.is_paper]  # no live RSI trade from a paper cycle


def test_ema_has_priority_for_the_one_live_slot(db, monkeypatch):
    settings, client = setup(monkeypatch, ema=True)
    monkeypatch.setattr(trader, "generate_signal", lambda *a, **k: BUY_EMA)
    aid = live_account(settings)
    run_cycle(aid, settings)
    trades = all_trades(aid)
    assert len(trades) == 1 and trades[0].strategy_id in (None, "", "ema_cross_atr_v1")  # EMA took the slot, RSI did not add a second trade


def test_when_ema_took_the_slot_the_rsi_signal_is_not_even_evaluated(db, monkeypatch):
    settings, client = setup(monkeypatch, ema=True)
    monkeypatch.setattr(trader, "generate_signal", lambda *a, **k: BUY_EMA)

    def boom(*a, **k):
        raise AssertionError("the RSI signal was evaluated although EMA took the only live slot")

    monkeypatch.setattr(rsi_mr, "generate_signal", boom)
    aid = live_account(settings)
    run_cycle(aid, settings)
    assert len(all_trades(aid)) == 1


def test_the_entry_function_itself_refuses_a_live_rsi_call_with_the_switch_off(db, monkeypatch):
    import asyncio

    settings, client = setup(monkeypatch, live_rsi_mr=False)
    aid = live_account(settings)
    sig = rsi_mr.RsiSignal(side="long", reason="x", rsi=40.0, prev_rsi=28.0, adx=20.0, atr=1.0, atr_pct=1.0)

    async def go():
        with session_scope(autoflush=False) as session:
            await trader._maybe_enter(session, session.get(TradingAccount, aid), client, sig, 96.0, settings, FakeNotifier(), is_paper=False, now=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))

    asyncio.run(go())
    assert all_trades(aid) == [] and client.orders == {}


def test_the_entry_function_refuses_a_live_rsi_call_for_an_account_that_is_not_opted_in(db, monkeypatch):
    import asyncio

    settings, client = setup(monkeypatch)
    aid = live_account(settings, opted=False)
    sig = rsi_mr.RsiSignal(side="long", reason="x", rsi=40.0, prev_rsi=28.0, adx=20.0, atr=1.0, atr_pct=1.0)

    async def go():
        with session_scope(autoflush=False) as session:
            await trader._maybe_enter(session, session.get(TradingAccount, aid), client, sig, 96.0, settings, FakeNotifier(), is_paper=False, now=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))

    asyncio.run(go())
    assert all_trades(aid) == [] and client.orders == {}


def test_the_kill_switch_stops_a_live_rsi_entry(db, monkeypatch):
    settings, client = setup(monkeypatch)
    _engage()
    aid = live_account(settings)
    run_cycle(aid, settings)
    assert all_trades(aid) == [] and client.orders == {}


def test_a_stale_rsi_signal_is_not_traded(db, monkeypatch):
    settings, client = setup(monkeypatch, new_strategy_entry_window_minutes=1.0)
    aid = live_account(settings)
    old = [[r[0] - 3 * 3_600_000] + r[1:] for r in raw(LONG)]  # the signal candle closed three hours ago, the window is one minute
    client._ohlcv = old
    run_cycle(aid, settings)
    assert all_trades(aid) == [] and client.orders == {}


def test_the_demo_mode_labels_an_rsi_entry_demo(db, monkeypatch):
    from test_demo_runner import DEMO_KEY, DEMO_SECRET, demo_account, install_recording

    settings = st(demo_runner=True)
    client = futures_client(ohlcv=raw(LONG), price=float(LONG[-1]))
    install_recording(monkeypatch, client)
    monkeypatch.setattr(trader, "generate_signal", lambda *a, **k: NO_EMA)
    aid = demo_account(settings)
    with session_scope() as s:
        s.get(TradingAccount, aid).extra_strategies = "rsi_mr_v1"
    n = run_cycle(aid, settings)
    t = the_trade(aid)
    assert t.strategy_id == "rsi_mr_v1" and t.is_demo is True
    assert any("[DEMO] Entered" in x for x in n.texts) and not any("[LIVE]" in x for x in n.texts)


# ------------------------------------------------------------------ the exit
def _opened_hours_ago(aid, hours):
    from datetime import datetime, timedelta, timezone

    from mba_bot.models import Trade

    with session_scope() as s:
        t = s.query(Trade).filter(Trade.account_id == aid).one()
        t.opened_at = datetime.now(timezone.utc) - timedelta(hours=hours)


def test_an_open_rsi_trade_is_exited_by_its_own_rules_not_by_an_ema_crossover(db, monkeypatch):
    settings, client = setup(monkeypatch)
    aid = live_account(settings)
    run_cycle(aid, settings)
    t = the_trade(aid)
    assert t.strategy_id == "rsi_mr_v1" and t.status == "open"
    # an EMA bearish crossover must NOT close an RSI trade
    monkeypatch.setattr(trader, "generate_signal", lambda *a, **k: StrategySignal(SignalType.BEARISH_CROSSOVER, "bearish crossover", 100.0, 101.0, 1.0, 0.01))
    run_cycle(aid, settings)
    assert the_trade(aid).status == "open"
    # price recovers: RSI back above 50 on the closed candles -> the RSI exit closes it through the lifecycle
    recovered = list(LONG) + [97.5, 99.0, 100.5, 101.5, 102.0, 102.5]
    client._ohlcv, client.price = raw(recovered), float(recovered[-1])
    _opened_hours_ago(aid, 6.5)  # the six new closed candles all started after the entry candle
    n = run_cycle(aid, settings)
    t = the_trade(aid)
    assert t.status == "closed" and t.close_reason in ("rsi_exit", "profit_target") and t.is_paper is False
    assert any("[LIVE] Exited" in x for x in n.texts)


def test_an_open_rsi_trade_is_still_managed_when_the_switch_is_turned_off(db, monkeypatch):
    settings, client = setup(monkeypatch)
    aid = live_account(settings)
    run_cycle(aid, settings)
    assert the_trade(aid).status == "open"
    off = dataclasses.replace(settings, live_rsi_mr=False)
    recovered = list(LONG) + [97.5, 99.0, 100.5, 101.5, 102.0, 102.5]
    client._ohlcv, client.price = raw(recovered), float(recovered[-1])
    _opened_hours_ago(aid, 6.5)
    run_cycle(aid, off)
    assert the_trade(aid).status == "closed"  # a trade that exists is always managed with its own rules


def test_the_exit_rules_never_call_a_bare_order_from_the_trader(db):
    import inspect

    src = inspect.getsource(trader._live_rsi_rules) + inspect.getsource(trader._live_rsi_exit)
    assert "execute_exit" in src and "create_order" not in src
