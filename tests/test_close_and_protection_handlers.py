"""The failure handlers on the close, stop-move and protection paths read nothing from the database (docs/safety/close_and_protection_handlers.md).

PROVES (fake exchange, scratch SQLite): at each site, when the account row is expired and cannot be reloaded ('database is locked') at the moment the handler runs, the handler still does its job (a deferred return,
the emergency retry, the next protection attempt, the cancel of a possibly placed stop and the alert, the cleanup that precedes a recorded close) and nothing is raised; the order of exchange actions is unchanged and no
exchange call is added; a close is recorded when the trade object is expired and the database is briefly locked, and when the commit of the close itself is briefly locked (only that local write is retried).
DOES NOT PROVE: how long a real lock lasts, or what Binance returns."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import ccxt.async_support as ccxt_async
import pytest
from helpers_live import BEAR_PRICE, BEARISH_OHLCV, FakeNotifier, live_context, open_protected, run_cycle, the_trade
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from mba_bot import db as dbmod
from mba_bot import fee_tier, lifecycle, trader
from mba_bot.db import session_scope
from mba_bot.models import Trade, TradingAccount

U = timezone.utc


@pytest.fixture
def db(tmp_path):
    dbmod.init_db(f"sqlite:///{tmp_path}/test.db")
    fee_tier.reset_cache()


def locked():
    return OperationalError("SELECT", {}, sqlite3.OperationalError("database is locked"))


class Flaky:
    """Stands in for ctx.account: every attribute read fails while `broken` (an expired row whose reload hits 'database is locked')."""

    def __init__(self, real):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "broken", False)
        object.__setattr__(self, "fail_reads", 0)  # a brief lock: this many reads fail, then the row can be reloaded

    def __getattr__(self, name):
        if object.__getattribute__(self, "broken"):
            raise locked()
        left = object.__getattribute__(self, "fail_reads")
        if left > 0:
            object.__setattr__(self, "fail_reads", left - 1)
            raise locked()
        return getattr(object.__getattribute__(self, "_real"), name)


def trip(client, ctx, name, *, exc=None, times=1, brief=0, also=None):
    """The `times`-th next call of client.<name> leaves ctx.account broken: it raises `exc` (when given) or returns normally. The account heals at the next call of any client method, so only the code that runs
    between the failing call and the next exchange call sees it broken (which is where a failure handler runs)."""
    orig = getattr(client, name)
    state = {"n": 0}
    old_record = client._record

    def record(*a, **k):
        object.__setattr__(ctx.account, "broken", False)
        object.__setattr__(ctx.account, "fail_reads", 0)
        return old_record(*a, **k)

    client._record = record

    async def wrapped(*a, **k):
        if state["n"] < times:
            state["n"] += 1
            if exc is not None:
                _break(ctx)
                raise exc
            result = await orig(*a, **k)
            _break(ctx)
            return result
        return await orig(*a, **k)

    def _break(c):
        if also is not None:
            also()
        if brief:
            object.__setattr__(c.account, "fail_reads", brief)
        else:
            object.__setattr__(c.account, "broken", True)

    setattr(client, name, wrapped)
    return state


def ctx_and_trade(session, aid, client, settings, notifier=None):
    ctx = live_context(session, aid, client, settings, notifier or FakeNotifier())
    ctx.account = Flaky(ctx.account)
    trade = session.get(Trade, the_trade(aid).id)
    return ctx, trade


def run(coro):
    return asyncio.run(coro)


async def prepared(ctx):
    await ctx.venue.prepare(managing=True)


def exit_orders(client):
    return client.orders_by_role("exit")


# ---------------------------------------------------------------------------------------------- finalize_exchange_close
def test_the_close_is_recorded_when_the_cleanup_of_leftover_orders_fails_and_the_account_cannot_be_reloaded(db, monkeypatch):
    client, settings, aid = open_protected(monkeypatch, market="futures")
    with session_scope(autoflush=False) as session:
        ctx, trade = ctx_and_trade(session, aid, client, settings)
        trip(client, ctx, "fetch_open_orders", exc=ccxt_async.NetworkError("down"), brief=2)  # the next two reads of the account row fail, then it can be reloaded

        async def go():
            await prepared(ctx)
            await lifecycle.finalize_exchange_close(ctx, trade, reason="externally_closed")

        run(go())
    t = the_trade(aid)
    assert t.status == "closed" and t.close_reason == "externally_closed"


# ---------------------------------------------------------------------------------------------- execute_exit
def test_execute_exit_defers_when_the_stop_was_cancelled_and_the_exchange_state_read_fails_with_the_account_unreadable(db, monkeypatch):
    """End to end for the exit path: the protective stop is cancelled (step 1), then the position read fails and the account row cannot be reloaded. The exit returns 'deferred' (the reconciler re-protects), nothing is raised
    and no exit order was sent."""
    client, settings, aid = open_protected(monkeypatch, market="futures")
    with session_scope(autoflush=False) as session:
        ctx, trade = ctx_and_trade(session, aid, client, settings)
        trip(client, ctx, "fetch_positions", exc=ccxt_async.NetworkError("down"))

        async def go():
            await prepared(ctx)
            return await lifecycle.execute_exit(ctx, trade, reason="manual")

        result = run(go())
    assert result == "deferred"
    assert exit_orders(client) == []
    assert not [o for o in client.orders_by_role("stop") if o["status"] == "open"]  # the stop was cancelled first, as before: the order of exchange actions is unchanged
    assert the_trade(aid).status == "closing"


def test_the_emergency_exit_retries_after_a_failed_attempt_when_the_account_cannot_be_reloaded(db, monkeypatch):
    client, settings, aid = open_protected(monkeypatch, market="futures")
    with session_scope(autoflush=False) as session:
        ctx, trade = ctx_and_trade(session, aid, client, settings)
        state = trip(client, ctx, "create_order", exc=ccxt_async.NetworkError("down"))

        async def go():
            await prepared(ctx)
            return await lifecycle.execute_exit(ctx, trade, reason="manual", emergency=True)

        result = run(go())
    assert state["n"] == 1 and result == "closed"
    assert len(exit_orders(client)) == 1  # exactly one exit order: the failed attempt placed nothing, the retry placed one
    assert the_trade(aid).status == "closed"


def test_a_cancel_that_cannot_be_confirmed_does_not_stop_a_futures_exit_when_the_account_cannot_be_reloaded(db, monkeypatch):
    client, settings, aid = open_protected(monkeypatch, market="futures")
    with session_scope(autoflush=False) as session:
        ctx, trade = ctx_and_trade(session, aid, client, settings)
        client.fail("cancel_order", exc=ccxt_async.NetworkError("down"), times=1)  # the cancel does not happen, so the stop still rests and cancel_protection raises ProtectionStillOpen
        state = trip(client, ctx, "fetch_order", times=1)  # the read after the cancel returns, then the account is unreadable while the handler runs

        async def go():
            await prepared(ctx)
            return await lifecycle.execute_exit(ctx, trade, reason="manual")

        result = run(go())
    assert state["n"] == 1 and result == "closed"
    assert len(exit_orders(client)) == 1


# ---------------------------------------------------------------------------------------------- establish_protection
def test_the_next_protection_attempt_happens_after_a_failed_one_when_the_account_cannot_be_reloaded(db, monkeypatch, caplog):
    """End to end for protection: the first protective-stop attempt fails with a connection error and the account row cannot be reloaded. The handler still classifies, logs and lets the loop retry: the stop is placed
    and verified on the next attempt."""
    client, settings, aid = open_protected(monkeypatch, market="futures")
    with session_scope(autoflush=False) as session:
        ctx, trade = ctx_and_trade(session, aid, client, settings)
        for o in list(client.orders.values()):  # the resting stop is gone from the exchange: this trade needs protection again
            if o["conditional"] and o["status"] == "open":
                o["status"] = "canceled"
        state = trip(client, ctx, "create_order", exc=ccxt_async.NetworkError("down"))

        async def go():
            await prepared(ctx)
            return await lifecycle.establish_protection(ctx, trade)

        with caplog.at_level("WARNING"):
            outcome = run(go())
    assert state["n"] == 1 and outcome == "protected"
    assert f"account {aid}: protective stop attempt 1/" in caplog.text  # logged with the id read before the attempt
    assert len([o for o in client.orders_by_role("stop") if o["status"] == "open"]) == 1


def test_a_deterministic_refusal_returns_failed_instead_of_raising_when_the_account_cannot_be_reloaded(db, monkeypatch):
    from mba_bot.venue import ProtectionUnavailable

    client, settings, aid = open_protected(monkeypatch, market="futures")
    with session_scope(autoflush=False) as session:
        ctx, trade = ctx_and_trade(session, aid, client, settings)
        for o in list(client.orders.values()):
            if o["conditional"] and o["status"] == "open":
                o["status"] = "canceled"

        async def refuse(**kw):
            ctx.account.broken = True
            raise ProtectionUnavailable("the stop would trigger at once")

        monkeypatch.setattr(ctx.venue, "protection_precheck", refuse)
        outcome = run(lifecycle.establish_protection(ctx, trade))
    assert outcome == "failed"  # the caller's emergency path takes over


# ---------------------------------------------------------------------------------------------- _resolve_spot_quantity
def test_the_spot_quantity_retries_and_gives_false_instead_of_raising_when_the_account_cannot_be_reloaded(db, monkeypatch):
    client, settings, aid = open_protected(monkeypatch, market="spot")
    with session_scope(autoflush=False) as session:
        ctx, trade = ctx_and_trade(session, aid, client, settings)
        state = trip(client, ctx, "fetch_order", exc=ccxt_async.NetworkError("down"), times=99)

        async def go():
            await prepared(ctx)
            return await lifecycle._resolve_spot_quantity(ctx, trade)

        assert run(go()) is False  # False sends the caller to the emergency path
    assert state["n"] == max(1, settings.protection_max_attempts)


# ---------------------------------------------------------------------------------------------- _resolve_entry_submission_error
def test_an_unknown_entry_outcome_is_left_pending_when_the_account_cannot_be_reloaded(db, monkeypatch):
    client, settings, aid = open_protected(monkeypatch, market="futures")
    with session_scope(autoflush=False) as session:
        ctx, trade = ctx_and_trade(session, aid, client, settings)
        trip(client, ctx, "fetch_order", exc=ccxt_async.NetworkError("down"), times=9)

        async def go():
            await prepared(ctx)
            return await lifecycle._resolve_entry_submission_error(ctx, trade, ccxt_async.RequestTimeout("slow"), risk_text="")

        result = run(go())
    assert result is trade  # left pending for the reconciler
    assert len(client.orders_by_role("entry")) == 1  # no second entry order


# ---------------------------------------------------------------------------------------------- move_stop
@pytest.mark.parametrize("pair_on_trade", [True, False])
def test_a_failed_trailing_move_still_cancels_the_possible_new_stop_and_alerts_when_the_account_cannot_be_reloaded(db, monkeypatch, pair_on_trade):
    client, settings, aid = open_protected(monkeypatch, market="futures")
    before = the_trade(aid)
    price = before.entry_price * 1.05
    client.price = price
    notifier = FakeNotifier()
    with session_scope(autoflush=False) as session:
        ctx, trade = ctx_and_trade(session, aid, client, settings, notifier)
        if not pair_on_trade:
            trade.pair = None  # an older row: the pair is then read from the account (not flushed: autoflush is off)
        state = trip(client, ctx, "create_order", exc=ccxt_async.NetworkError("down"))

        async def go():
            await prepared(ctx)
            return await lifecycle.move_stop(ctx, trade, new_stop=before.stop_loss_price + (price - before.stop_loss_price) * 0.5, current_price=price)

        result = run(go())
    assert state["n"] == 1 and result == "kept"
    assert any("Could not move the trailing stop" in t and f"{before.stop_loss_price:.4f}" in t for t in notifier.texts)  # the customer is still told, with the old level
    assert any(c[0] == "cancel_order" for c in client.calls)  # the cancel of a possibly placed new stop was still attempted
    t = the_trade(aid)
    assert t.stop_loss_price == before.stop_loss_price and t.protection_attempt == before.protection_attempt  # the old stop is untouched


# ---------------------------------------------------------------------------------------------- _flag_withdrawal_block
class _Acct:
    def __init__(self):
        self.id = "acc-9"
        self.withdrawal_blocked_at = None
        self.notification_target = "chat"
        self.broken = False

    def __getattribute__(self, name):
        if name != "broken" and object.__getattribute__(self, "broken"):
            raise locked()
        return object.__getattribute__(self, name)


class _Sess:
    def __init__(self, *, commit_fails=False, active=True):
        self.commit_fails, self.is_active, self.rollbacks = commit_fails, active, 0

    def commit(self):
        if self.commit_fails:
            raise locked()

    def rollback(self):
        self.rollbacks += 1


class _Notifier:
    def __init__(self, fails=False, acct=None):
        self.sent, self.fails, self.acct = [], fails, acct

    async def send(self, target, message):
        if self.acct is not None:
            self.acct.broken = True  # the failure leaves the account unreadable
        if self.fails:
            raise RuntimeError("telegram down")
        self.sent.append(message)


def test_the_withdrawal_flag_never_raises_when_the_alert_fails_and_the_account_cannot_be_reloaded(caplog):
    acct = _Acct()
    n = _Notifier(fails=True, acct=acct)
    with caplog.at_level("ERROR"):
        run(trader._flag_withdrawal_block(_Sess(), acct, n, now=datetime.now(U)))
    acct.broken = False
    assert acct.withdrawal_blocked_at is not None and "account acc-9: failed to send the withdrawal-permission alert" in caplog.text


def test_the_withdrawal_flag_swallows_a_failed_commit_sends_no_alert_and_rolls_back_only_if_it_must(caplog):
    acct, n = _Acct(), _Notifier()
    s = _Sess(commit_fails=True, active=False)
    with caplog.at_level("ERROR"):
        run(trader._flag_withdrawal_block(s, acct, n, now=datetime.now(U)))
    assert n.sent == [] and s.rollbacks == 1  # the alert comes only after a durable record, as before
    healthy = _Sess(commit_fails=True, active=True)
    run(trader._flag_withdrawal_block(healthy, _Acct(), _Notifier(), now=datetime.now(U)))
    assert healthy.rollbacks == 0  # a transaction that can continue keeps the cycle's other pending changes


def test_the_withdrawal_flag_is_unchanged_when_nothing_fails():
    acct, n, s = _Acct(), _Notifier(), _Sess()
    run(trader._flag_withdrawal_block(s, acct, n, now=datetime.now(U)))
    assert acct.withdrawal_blocked_at is not None and len(n.sent) == 1
    run(trader._flag_withdrawal_block(s, acct, n, now=datetime.now(U)))
    assert len(n.sent) == 1  # once


# ---------------------------------------------------------------------------------------------- _finalize: the close is recorded
def arm_lock(statements, *, count, table):
    """The next `count` statements of the given kind on `table` raise 'database is locked' (a brief lock)."""
    state = {"left": count, "hits": 0}

    def hook(conn, cursor, statement, parameters, context, executemany):
        if state["left"] > 0 and statement.lstrip().upper().startswith(statements) and table in statement and (statements != ("UPDATE",) or "exit_price" in statement):
            state["left"] -= 1
            state["hits"] += 1
            raise sqlite3.OperationalError("database is locked")

    event.listen(dbmod._engine, "before_cursor_execute", hook)
    return state, hook


def test_a_close_is_recorded_when_the_funding_read_fails_and_the_trade_object_is_expired_and_briefly_locked(db, monkeypatch):
    """End to end (real cycle): at the funding read the trade and account objects are expired and the next two reads of the trades table hit 'database is locked'. The close is still recorded."""
    import ccxt

    client, settings, aid = open_protected(monkeypatch, market="futures")
    client._ohlcv, client.price = BEARISH_OHLCV, BEAR_PRICE
    client.fail("fetch_funding_history", exc=ccxt.RequestTimeout("down"), times=9)
    original = lifecycle.read_funding
    holder = {}

    async def read_with_trouble(ctx, trade):
        ctx.session.expire_all()
        holder["state"], holder["hook"] = arm_lock(("SELECT",), count=2, table="FROM trades")
        return await original(ctx, trade)

    monkeypatch.setattr(lifecycle, "read_funding", read_with_trouble)
    try:
        run_cycle(aid, settings)
    finally:
        if "hook" in holder:
            event.remove(dbmod._engine, "before_cursor_execute", holder["hook"])
    assert holder["state"]["hits"] == 2  # the lock really happened, twice
    t = the_trade(aid)
    assert t.status == "closed" and t.close_reason is not None and t.funding_quote is None
    with session_scope() as s:
        assert s.get(TradingAccount, aid).last_error is None


def test_a_close_is_recorded_when_the_commit_of_the_close_itself_is_briefly_locked(db, monkeypatch):
    client, settings, aid = open_protected(monkeypatch, market="futures")
    client._ohlcv, client.price = BEARISH_OHLCV, BEAR_PRICE
    state, hook = arm_lock(("UPDATE",), count=2, table="UPDATE trades")
    try:
        run_cycle(aid, settings)
    finally:
        event.remove(dbmod._engine, "before_cursor_execute", hook)
    assert state["hits"] >= 1
    t = the_trade(aid)
    assert t.status == "closed" and t.close_reason is not None
    assert len(exit_orders(client)) <= 1  # no exchange order was repeated because of the local retry


def test_a_lock_that_does_not_clear_raises_after_the_retries_and_repeats_no_exchange_call(db, monkeypatch):
    client, settings, aid = open_protected(monkeypatch, market="futures")
    with session_scope(autoflush=False) as session:
        ctx, trade = ctx_and_trade(session, aid, client, settings)
        trade_obj = session.get(Trade, the_trade(aid).id)
        state, hook = arm_lock(("UPDATE",), count=99, table="UPDATE trades")
        try:
            with pytest.raises(OperationalError):
                run(lifecycle._finalize(ctx, trade_obj, reason="manual", fallback_price=1.0, protection="cancelled"))
        finally:
            event.remove(dbmod._engine, "before_cursor_execute", hook)
    assert state["hits"] == 3  # one try and two retries, no more
    assert the_trade(aid).status == "open"  # nothing was recorded; the reconciler tries again next round


# ---------------------------------------------------------------------------------------------- the local retry
def test_only_a_locked_database_is_retried_with_a_short_pause_and_at_most_twice(monkeypatch):
    pauses = []

    async def fake_sleep(seconds):
        pauses.append(seconds)

    monkeypatch.setattr(lifecycle, "_sleep", fake_sleep)
    sess = SimpleNamespace(is_active=True, rollback=lambda: None)
    calls = {"n": 0}

    def other():
        calls["n"] += 1
        raise OperationalError("UPDATE", {}, sqlite3.OperationalError("disk I/O error"))

    with pytest.raises(OperationalError):
        run(lifecycle._db_retry(sess, other))
    assert calls["n"] == 1 and pauses == []  # another error is never retried

    def always_locked():
        calls["n"] += 1
        raise locked()

    calls["n"] = 0
    with pytest.raises(OperationalError):
        run(lifecycle._db_retry(sess, always_locked))
    assert calls["n"] == 3 and len(pauses) == 2 and all(0 < p <= 0.5 for p in pauses)

    seq = iter([locked(), None])

    def once():
        e = next(seq)
        if e:
            raise e
        return "done"

    assert run(lifecycle._db_retry(sess, once)) == "done"


def test_a_failed_retry_rolls_the_session_back_only_when_it_cannot_continue(monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr(lifecycle, "_sleep", no_sleep)
    rollbacks = []
    for active in (False, True):
        sess = SimpleNamespace(is_active=active, rollback=lambda: rollbacks.append(1))
        seq = iter([locked(), None])

        def op():
            e = next(seq)
            if e:
                raise e
            return 1

        run(lifecycle._db_retry(sess, op))
    assert rollbacks == [1]


def test_a_message_to_the_customer_never_raises_when_the_account_cannot_be_reloaded(db, monkeypatch):
    client, settings, aid = open_protected(monkeypatch, market="futures")
    with session_scope(autoflush=False) as session:
        ctx, _ = ctx_and_trade(session, aid, client, settings)
        ctx.account.broken = True
        run(lifecycle._notify(ctx, "hello"))  # raises nothing
        assert ctx.notifier.sent == []


def test_loading_an_object_that_is_already_loaded_touches_no_database(db, monkeypatch):
    client, settings, aid = open_protected(monkeypatch, market="futures")
    with session_scope(autoflush=False) as session:
        ctx, trade = ctx_and_trade(session, aid, client, settings)
        run(lifecycle._ensure_loaded(ctx, trade))  # loads once
        seen = []
        hook = lambda *a: seen.append(a[2])  # noqa: E731
        event.listen(dbmod._engine, "before_cursor_execute", hook)
        try:
            run(lifecycle._ensure_loaded(ctx, trade))
        finally:
            event.remove(dbmod._engine, "before_cursor_execute", hook)
        assert seen == []


@pytest.mark.parametrize("path", ["finalize", "finalize_exchange_close"])
def test_an_expired_trade_is_reloaded_before_the_close_paths_read_it(db, monkeypatch, path):
    client, settings, aid = open_protected(monkeypatch, market="futures")
    with session_scope(autoflush=False) as session:
        ctx, trade = ctx_and_trade(session, aid, client, settings)
        session.expire_all()
        state, hook = arm_lock(("SELECT",), count=2, table="FROM trades")
        try:

            async def go():
                await prepared(ctx)
                if path == "finalize":
                    return await lifecycle._finalize(ctx, trade, reason="manual", fallback_price=1.0, protection="cancelled")
                return await lifecycle.finalize_exchange_close(ctx, trade, reason="externally_closed")

            run(go())
        finally:
            event.remove(dbmod._engine, "before_cursor_execute", hook)
    assert state["hits"] == 2 and the_trade(aid).status == "closed"


def test_a_failed_trailing_move_reads_no_trade_attribute_in_its_handler_when_the_trade_is_expired_and_locked(db, monkeypatch):
    client, settings, aid = open_protected(monkeypatch, market="futures")
    before = the_trade(aid)
    price = before.entry_price * 1.05
    client.price = price
    notifier = FakeNotifier()
    holder = {}
    with session_scope(autoflush=False) as session:
        ctx, trade = ctx_and_trade(session, aid, client, settings, notifier)

        def expire_and_lock():
            session.expire(trade)  # a rollback elsewhere left the trade expired, and the reload hits 'database is locked'
            holder["state"], holder["hook"] = arm_lock(("SELECT",), count=1, table="FROM trades")

        trip(client, ctx, "create_order", exc=ccxt_async.NetworkError("down"), brief=0, also=expire_and_lock)
        trip_state = None

        async def go():
            await prepared(ctx)
            return await lifecycle.move_stop(ctx, trade, new_stop=before.stop_loss_price + (price - before.stop_loss_price) * 0.5, current_price=price)

        try:
            result = run(go())
        finally:
            event.remove(dbmod._engine, "before_cursor_execute", holder["hook"])
    assert result == "kept" and holder["state"]["hits"] == 0  # the handler read nothing from the trade, so the lock was never hit
    assert any("Could not move the trailing stop" in t for t in notifier.texts)
