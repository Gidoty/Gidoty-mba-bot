"""The funding code never raises into the close path or the stop and lock path (docs/safety/funding_never_blocks_close.md).

PROVES: read_funding and refresh_funding return normally when the account row is expired and cannot be reloaded ('database is locked'), when the commit that saves the figure fails, and when the log call or the
rollback fails; the freshly read figure is still used; a real Futures stop-triggered close is recorded even when the funding read fails at the moment the account row cannot be reloaded.
DOES NOT PROVE: anything about what Binance returns for funding."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import ccxt
import pytest
from helpers_live import BEAR_PRICE, BEARISH_OHLCV, open_protected, run_cycle, the_trade
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from mba_bot import fee_tier, lifecycle
from mba_bot.db import init_db, session_scope
from mba_bot.models import TradingAccount

U = timezone.utc


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")
    fee_tier.reset_cache()


def locked():
    return OperationalError("UPDATE trades", {}, sqlite3.OperationalError("database is locked"))


class Expired:
    """An ORM object whose attribute reads all fail once `broken` is set (an expired row whose reload hits 'database is locked')."""

    def __init__(self, **values):
        object.__setattr__(self, "_v", dict(values))
        object.__setattr__(self, "broken", False)

    def __getattr__(self, name):
        if object.__getattribute__(self, "broken"):
            raise locked()
        try:
            return object.__getattribute__(self, "_v")[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name, value):
        if name == "broken":
            object.__setattr__(self, name, value)
        else:
            object.__getattribute__(self, "_v")[name] = value


class Venue:
    market_type = "futures"

    def __init__(self, income=-0.4, fail=None):
        self.income, self.fail = income, fail

    async def funding_income(self, *, start_ms, end_ms):
        if self.fail:
            raise self.fail
        return self.income


class Sess:
    def __init__(self, commit_fails=False, rollback_fails=False):
        self.commit_fails, self.rollback_fails, self.commits, self.rollbacks = commit_fails, rollback_fails, 0, 0

    def commit(self):
        self.commits += 1
        if self.commit_fails:
            raise locked()

    def rollback(self):
        self.rollbacks += 1
        if self.rollback_fails:
            raise locked()


def make(*, venue=None, session=None, account_broken=False):
    account = Expired(id="acc-1", notification_target=None)
    trade = Expired(funding_quote=None, funding_read_at=None, opened_at=datetime(2027, 1, 1, tzinfo=U), quantity=1.0)
    ctx = SimpleNamespace(account=account, venue=venue or Venue(), now=datetime(2027, 1, 2, tzinfo=U), session=session or Sess())
    return ctx, trade, account


def run(c):
    return asyncio.run(c)


# ---------------------------------------------------------------------------------------------- read_funding
def test_read_funding_returns_none_when_the_account_cannot_be_reloaded_and_the_read_fails(caplog):
    ctx, trade, account = make(venue=Venue(fail=ccxt.RequestTimeout("down")))
    original = ctx.venue.funding_income

    async def fails_and_expires(**kw):
        account.broken = True  # the failure leaves the account row expired and the reload locked
        await original(**kw)

    ctx.venue.funding_income = fails_and_expires
    with caplog.at_level("INFO"):
        assert run(lifecycle.read_funding(ctx, trade)) is None
    assert "account acc-1: could not read the funding income (RequestTimeout)" in caplog.text


def test_read_funding_returns_none_when_the_trade_cannot_be_reloaded():
    ctx, trade, account = make()
    trade.broken = True
    account.broken = True
    assert run(lifecycle.read_funding(ctx, trade)) is None


def test_read_funding_swallows_a_failing_log_call(monkeypatch):
    ctx, trade, account = make(venue=Venue(fail=RuntimeError("x")))
    monkeypatch.setattr(lifecycle.logger, "info", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("log broken")))
    assert run(lifecycle.read_funding(ctx, trade)) is None


def test_read_funding_is_unchanged_when_nothing_fails():
    ctx, trade, _ = make()
    assert run(lifecycle.read_funding(ctx, trade)) == -0.4
    ctx.venue.market_type = "spot"
    assert run(lifecycle.read_funding(ctx, trade)) == 0.0


# ---------------------------------------------------------------------------------------------- refresh_funding
def test_a_commit_that_fails_does_not_raise_and_the_value_just_read_is_used(caplog):
    s = Sess(commit_fails=True)
    ctx, trade, _ = make(session=s)
    with caplog.at_level("WARNING"):
        assert run(lifecycle.refresh_funding(ctx, trade)) == pytest.approx(0.4)
    assert s.commits == 1 and s.rollbacks == 1  # _set rolled back, as it always does
    assert "could not save the funding figure" in caplog.text


def test_a_commit_and_a_rollback_that_both_fail_do_not_raise():
    ctx, trade, _ = make(session=Sess(commit_fails=True, rollback_fails=True))
    assert run(lifecycle.refresh_funding(ctx, trade)) == pytest.approx(0.4)


def test_an_expired_trade_gives_zero_and_never_raises(caplog):
    ctx, trade, account = make()
    trade.broken = True
    account.broken = True
    with caplog.at_level("WARNING"):
        assert run(lifecycle.refresh_funding(ctx, trade)) == 0.0
    assert "account ?: could not refresh the funding figure" in caplog.text


def test_an_odd_stored_value_gives_zero_and_never_raises():
    ctx, trade, _ = make(venue=Venue(income=None))
    trade.funding_quote = "not a number"
    assert run(lifecycle.refresh_funding(ctx, trade)) == 0.0


def test_a_failing_log_call_never_raises(monkeypatch):
    ctx, trade, account = make()
    trade.broken = True
    monkeypatch.setattr(lifecycle.logger, "warning", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("log broken")))
    assert run(lifecycle.refresh_funding(ctx, trade)) == 0.0


def test_refresh_funding_keeps_its_behaviour_when_nothing_fails():
    s = Sess()
    ctx, trade, _ = make(session=s)
    assert run(lifecycle.refresh_funding(ctx, trade)) == pytest.approx(0.4)
    assert trade.funding_quote == -0.4 and trade.funding_read_at == ctx.now and s.commits == 1
    ctx.venue.income = -9.0  # inside the interval: not re-read
    assert run(lifecycle.refresh_funding(ctx, trade)) == pytest.approx(0.4)
    ctx.venue.income = 0.5  # funding received counts as zero paid
    ctx.now = datetime(2027, 1, 3, tzinfo=U)
    assert run(lifecycle.refresh_funding(ctx, trade)) == 0.0


# ---------------------------------------------------------------------------------------------- the real close
def test_the_close_is_recorded_when_the_funding_read_fails_and_the_account_row_cannot_be_reloaded(db, monkeypatch):
    """Real Futures trade, real database, stop triggered on the exchange. At the moment of the funding read the account row is expired and its reload hits 'database is locked' once. The close must still be recorded."""
    from mba_bot import db as dbmod

    client, settings, aid = open_protected(monkeypatch, market="futures")
    client._ohlcv, client.price = BEARISH_OHLCV, BEAR_PRICE
    client.fail("fetch_funding_history", exc=ccxt.RequestTimeout("down"), times=9)
    state = {"armed": False, "hits": 0}

    def lock_account_once(conn, cursor, statement, parameters, context, executemany):
        if state["armed"] and "FROM trading_accounts" in statement:
            state["armed"] = False
            state["hits"] += 1
            raise sqlite3.OperationalError("database is locked")

    original = lifecycle.read_funding

    async def read_with_trouble(ctx, trade):
        ctx.session.expire(ctx.account)  # the account row must be reloaded on its next read
        state["armed"] = True
        return await original(ctx, trade)

    monkeypatch.setattr(lifecycle, "read_funding", read_with_trouble)
    event.listen(dbmod._engine, "before_cursor_execute", lock_account_once)
    try:
        run_cycle(aid, settings)
    finally:
        event.remove(dbmod._engine, "before_cursor_execute", lock_account_once)
    t = the_trade(aid)
    assert state["hits"] == 1  # the trouble really happened, and the wrapper's read_funding is the one called by _finalize
    assert t.status == "closed" and t.close_reason is not None and t.funding_quote is None
    with session_scope() as s:
        assert s.get(TradingAccount, aid).last_error is None
