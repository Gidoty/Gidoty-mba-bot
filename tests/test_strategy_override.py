"""The admin override of an account's strategies (docs/safety/strategy_availability.md): step-up, a reason, an audit row, any known strategy whatever is available to customers, refused while a trade is open, and it
changes only `extra_strategies`. Uses the fixtures of tests/test_operator_settings.py."""
from __future__ import annotations

from sqlalchemy import select

from admin_support import mk_trade
from mba_bot import multi_trade
from mba_bot.db import session_scope
from mba_bot.models import AdminChange, TradingAccount
from test_operator_settings import changes, env, post, snapshot  # noqa: F401

DON, RSI = "donchian_breakout_v1", "rsi_mr_v1"


def extras(env):
    with session_scope() as s:
        return s.get(TradingAccount, env.ref).extra_strategies


def test_the_override_sets_any_known_strategy_even_when_none_is_available_to_customers(env):
    r = post(env, f"/customers/{env.ref}/strategies", strategies=[RSI, DON, "ema_cross_atr_v1"])
    assert r.status_code == 200 and r.json()["old"] == "ema only" and set(extras(env).split(",")) == {RSI, DON}
    with session_scope() as s:
        assert multi_trade.account_strategies(s.get(TradingAccount, env.ref)) == {RSI, DON}
    log = changes()[-1]
    assert log[:3] == ("action", "set_strategies", env.target_id) and log[6] == env.admin_id and "ema only" in log[3]
    assert post(env, f"/customers/{env.ref}/strategies", strategies=[]).status_code == 200 and extras(env) is None


def test_unknown_repeated_and_too_many_are_refused_and_nothing_changes(env):
    for bad in (["nope"], [RSI, RSI], [RSI, DON, "ema_cross_atr_v1", "x"]):
        assert post(env, f"/customers/{env.ref}/strategies", strategies=bad).status_code in (422,), bad
    assert extras(env) is None and changes() == []


def test_it_is_refused_while_a_trade_is_open_and_needs_the_step_up(env):
    with session_scope() as s:
        mk_trade(s, s.get(TradingAccount, env.ref), status="open", paper=False, pnl=None)
    r = post(env, f"/customers/{env.ref}/strategies", strategies=[RSI])
    assert r.status_code == 409 and "trade of this account is open" in r.json()["detail"] and extras(env) is None
    assert post(env, f"/customers/{env.ref}/strategies", strategies=[RSI], code="000000").status_code == 401
    assert post(env, "/customers/no-such/strategies", strategies=[RSI]).status_code in (404, 409)


def test_the_override_changes_only_the_strategies_column(env):
    with session_scope() as s:
        a = s.get(TradingAccount, env.ref)
        before = {c.name: getattr(a, c.name) for c in TradingAccount.__table__.columns if c.name not in ("extra_strategies", "updated_at")}
    post(env, f"/customers/{env.ref}/strategies", strategies=[DON])
    with session_scope() as s:
        a = s.get(TradingAccount, env.ref)
        assert {c.name: getattr(a, c.name) for c in TradingAccount.__table__.columns if c.name not in ("extra_strategies", "updated_at")} == before
        assert len(s.scalars(select(AdminChange)).all()) == 1
