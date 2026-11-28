"""Fix: the protective stop could not be moved on Binance Futures while a closePosition stop rested (live defect, 27 Nov 2026).

PROVES (stateful fake exchange that now REFUSES a second closePosition stop in the same direction with the error class ccxt 4.5.88 really raises for it, a plain ExchangeError carrying Binance's JSON answer with code
-4130): the move succeeds through a reduceOnly stop sized from the exchange's own position while the old stop rests; the old stop is cancelled only after the new one reads back resting; when both placements are refused the
old stop is untouched; there is no cancel-first path; a failed move is retried after 5, then 15, then at most every 30 minutes per trade with a failure count in the journal and the once-per-6-hours customer alert (which says the lock is run
by the bot, not by a resting stop); longs and shorts; Spot is unchanged; the fallback is never taken for network or authentication errors; the journal carries only the sanitized class, code and message (200 characters);
a stop that cannot be confirmed cancelled on a flat position is not a false ProtectionStillOpen and a leftover stop is cleared once the trade is closed.
DOES NOT PROVE: what real Binance does. The fake is a model built from the ccxt source and the owner's journal; Binance's own documentation could not be read here. The Testnet check (ops/testnet/check_second_stop.py)
and a live trade where the lock moves a stop are the real confirmation."""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from datetime import datetime, timedelta, timezone

import ccxt
import ccxt.async_support as ccxt_async
import pytest
from helpers_live import BEARISH_OHLCV, FakeNotifier, live_context, open_protected, the_trade
from test_live_donchian import db, resting_stops  # noqa: F401  (db is the fixture)

from mba_bot import lifecycle, venue
from mba_bot.db import session_scope
from mba_bot.models import Trade


@pytest.fixture(autouse=True)
def _clean_failures():
    lifecycle._MOVE_FAILURES.clear()
    yield
    lifecycle._MOVE_FAILURES.clear()


def run_moves(aid, client, settings, steps, *, notifier=None, reason="trailing_stop", before=None):
    """`steps`: list of (minutes after the first move, new_stop, price). Returns the results, the notifier, and what `before(step_index, ctx)` returned for each step (e.g. a snapshot of the exchange)."""
    notifier = notifier or FakeNotifier()
    results = []
    base = datetime.now(timezone.utc)

    async def go():
        with session_scope(autoflush=False) as session:
            ctx = live_context(session, aid, client, settings, notifier)
            trade = session.get(Trade, the_trade(aid).id)
            await ctx.venue.prepare(managing=True)
            for i, (minutes, new_stop, price) in enumerate(steps):
                ctx.now = base + timedelta(minutes=minutes)
                if before:
                    before(i, ctx)
                results.append(await lifecycle.move_stop(ctx, trade, new_stop=new_stop, current_price=price, reason=reason))

    asyncio.run(go())
    return results, notifier


def calls_of(client, name):
    return [c for c in client.calls if c[0] == name]


def long_trade(monkeypatch, **kw):
    client, settings, aid = open_protected(monkeypatch, market="futures", **kw)
    t = the_trade(aid)
    client.price = t.entry_price * 1.05
    return client, settings, aid, t


# ------------------------------------------------------------------------------------------------ the fix
def test_the_move_succeeds_through_a_reduce_only_stop_while_the_old_closeposition_stop_rests(monkeypatch, db):
    client, settings, aid, t = long_trade(monkeypatch)
    old_cid = lifecycle.client_id(t, "stop", t.protection_attempt)
    assert client.orders[old_cid]["close_position"] is True
    client.reject_second_close_stop = True  # Binance refuses a second closePosition stop: a plain ExchangeError with code -4130
    results, _ = run_moves(aid, client, settings, [(0, t.entry_price * 1.01, client.price)])
    assert results == ["moved"]
    after = the_trade(aid)
    assert after.protective_kind == venue.KIND_FUTURES_REDUCE and after.stop_loss_price > t.stop_loss_price and after.protection_attempt == t.protection_attempt + 1
    rest = resting_stops(client)
    assert len(rest) == 1 and rest[0]["reduce_only"] is True and rest[0]["close_position"] is False
    assert abs(rest[0]["amount"] - abs(client.position)) < 1e-9  # sized from the exchange's own position, not from the bot's stored number
    assert client.orders[old_cid]["status"] == "canceled"
    assert client.violations == []


def test_the_old_stop_is_cancelled_only_after_the_new_one_reads_back_resting(monkeypatch, db):
    client, settings, aid, t = long_trade(monkeypatch)
    old_cid = lifecycle.client_id(t, "stop", t.protection_attempt)
    new_cid = lifecycle.client_id(t, "stop", t.protection_attempt + 1)
    client.reject_second_close_stop = True
    seen = {}

    def on_cancel(fake, cid):
        if cid == old_cid:
            seen["new_status_at_old_cancel"] = fake.orders[new_cid]["status"]
            seen["old_status_at_old_cancel"] = fake.orders[old_cid]["status"]

    client.on_cancel = on_cancel
    run_moves(aid, client, settings, [(0, t.entry_price * 1.01, client.price)])
    assert seen == {"new_status_at_old_cancel": "open", "old_status_at_old_cancel": "open"}
    seq = [c[0] for c in client.calls]
    assert seq.index("fetch_order") < len(seq) and [i for i, n in enumerate(seq) if n == "cancel_order"][0] > max(i for i, n in enumerate(seq) if n == "create_order")
    # the read-back of the new order happened before the cancel of the old one
    fetch_new = [i for i, c in enumerate(client.calls) if c[0] == "fetch_order" and c[1] == new_cid]
    cancel_old = [i for i, c in enumerate(client.calls) if c[0] == "cancel_order" and c[1] == old_cid]
    assert fetch_new and cancel_old and fetch_new[0] < cancel_old[0]


def test_when_both_placements_are_refused_the_old_stop_is_untouched_and_the_alert_names_the_lock(monkeypatch, db):
    client, settings, aid, t = long_trade(monkeypatch)
    old_cid = lifecycle.client_id(t, "stop", t.protection_attempt)
    client.reject_second_close_stop = True
    client.reject_reduce_only_stop = True
    results, notifier = run_moves(aid, client, settings, [(0, t.entry_price * 1.01, client.price)], reason="profit_lock")
    assert results == ["kept"]
    after = the_trade(aid)
    assert after.stop_loss_price == t.stop_loss_price and after.protection_attempt == t.protection_attempt and after.status == "open" and after.protection_status == "protected"
    assert client.orders[old_cid]["status"] == "open" and len(resting_stops(client)) == 1
    assert [c for c in client.calls if c[0] == "cancel_order" and c[1] == old_cid] == []  # the old stop was never cancelled
    text = "\n".join(notifier.texts)
    assert "run by the bot and not by a resting stop" in text and "closes the trade at the market" in text and f"{t.stop_loss_price:.4f}" in text and "in a few minutes" in text and "at most every 30 minutes" in text


def test_there_is_no_cancel_first_path_in_the_code():
    import inspect

    src = inspect.getsource(lifecycle.move_stop)
    assert src.index("place_protection") < src.index("get_protection") < src.index("_cas(") < src.index("cancel_protection(")
    assert src.count("cancel_protection(") == 1


@pytest.mark.parametrize("refuse_close", [False, True])
def test_there_is_no_cancel_first_path_at_run_time(monkeypatch, db, refuse_close):
    """Whatever the exchange does, a cancel of the old stop never precedes a successful placement of the new one."""
    client, settings, aid, t = long_trade(monkeypatch)
    old_cid = lifecycle.client_id(t, "stop", t.protection_attempt)
    client.reject_second_close_stop = refuse_close
    run_moves(aid, client, settings, [(0, t.entry_price * 1.01, client.price)])
    order = [(c[0], c[1] if len(c) > 1 else None) for c in client.calls]
    first_cancel_old = next(i for i, c in enumerate(order) if c == ("cancel_order", old_cid))
    assert any(name == "create_order" for name, _ in order[:first_cancel_old])


def test_the_reduce_only_stop_is_sized_from_the_exchange_position_when_it_differs_from_the_stored_quantity(monkeypatch, db):
    client, settings, aid, t = long_trade(monkeypatch)
    client.reject_second_close_stop = True
    client.position = round(client.position * 0.5 / client.step) * client.step  # the customer closed half by hand: the exchange holds less than the trade's stored quantity
    assert abs(client.position) < t.quantity
    results, _ = run_moves(aid, client, settings, [(0, t.entry_price * 1.01, client.price)])
    assert results == ["moved"]
    assert abs(resting_stops(client)[0]["amount"] - abs(client.position)) < 1e-9


def test_a_short_moves_the_same_way(monkeypatch, db):
    client, settings, aid = open_protected(monkeypatch, market="futures", ohlcv=BEARISH_OHLCV)
    t = the_trade(aid)
    assert t.side == "short"
    client.price = t.entry_price * 0.95
    client.reject_second_close_stop = True
    results, _ = run_moves(aid, client, settings, [(0, t.entry_price * 0.99, client.price)])
    assert results == ["moved"]
    after = the_trade(aid)
    assert after.protective_kind == venue.KIND_FUTURES_REDUCE and after.stop_loss_price < t.stop_loss_price
    rest = resting_stops(client)
    assert len(rest) == 1 and rest[0]["side"] == "buy" and rest[0]["reduce_only"] is True


def test_a_move_in_the_wrong_direction_is_still_never_made(monkeypatch, db):
    client, settings, aid, t = long_trade(monkeypatch)
    client.reject_second_close_stop = True
    before = len(calls_of(client, "create_order"))
    results, _ = run_moves(aid, client, settings, [(0, t.stop_loss_price - 1.0, client.price), (0, client.price + 1.0, client.price)])
    assert results == ["skipped", "skipped"] and len(calls_of(client, "create_order")) == before  # a loosening and a stop beyond the price send nothing


def test_after_the_fallback_the_next_move_still_works_whatever_kind_rests(monkeypatch, db):
    client, settings, aid, t = long_trade(monkeypatch)
    client.reject_second_close_stop = True
    price = client.price
    results, _ = run_moves(aid, client, settings, [(0, t.entry_price * 1.01, price), (31, t.entry_price * 1.02, price), (62, t.entry_price * 1.03, price)])
    assert results == ["moved", "moved", "moved"]
    after = the_trade(aid)
    assert len(resting_stops(client)) == 1 and abs(after.stop_loss_price - venue_round(client, t.entry_price * 1.03)) < 1e-6
    # the protective kind recorded matches the order that rests, every time
    rest = resting_stops(client)[0]
    assert (after.protective_kind == venue.KIND_FUTURES_REDUCE) == bool(rest["reduce_only"])


def venue_round(client, x):
    return round(x / client.tick) * client.tick


# ------------------------------------------------------------------------------------------------ the fallback condition
@pytest.mark.parametrize(
    "exc, expected",
    [
        (ccxt_async.ExchangeError('binanceusdm {"code":-4130,"msg":"An open stop or take profit order with GTE and closePosition in the direction is existing."}'), True),
        (ccxt_async.ExchangeError("binanceusdm {\"code\":-9999,\"msg\":\"something unmapped\"}"), True),  # a plain ExchangeError: a code the pinned ccxt does not map
        (ccxt_async.InvalidOrder("x"), True),  # the existing rule
        (ccxt_async.BadRequest("x"), True),  # the existing rule
        (ccxt_async.RequestTimeout("x"), False),
        (ccxt_async.NetworkError("x"), False),
        (ccxt_async.DDoSProtection("x"), False),
        (ccxt_async.RateLimitExceeded("x"), False),
        (ccxt_async.ExchangeNotAvailable("x"), False),
        (ccxt_async.AuthenticationError("x"), False),
        (ccxt_async.PermissionDenied("x"), False),
        (ccxt_async.AccountSuspended("x"), False),
        (ccxt_async.InsufficientFunds("x"), False),
        (ValueError("x"), False),
        (ccxt_async.AuthenticationError('binanceusdm {"code":-4130,"msg":"never for authentication errors"}'), False),
        (ccxt_async.RequestTimeout('binanceusdm {"code":-4130,"msg":"never for network errors"}'), False),
    ],
)
def test_the_reduce_only_fallback_covers_the_real_error_and_never_network_or_authentication_errors(exc, expected):
    assert venue.may_fall_back_to_reduce_only(exc) is expected


def test_a_network_error_on_the_closeposition_stop_is_not_turned_into_a_reduce_only_attempt(monkeypatch, db):
    client, settings, aid, t = long_trade(monkeypatch)
    client.fail("create_order", role="stop", exc=ccxt.RequestTimeout("binanceusdm timed out"), times=5)
    before = len(calls_of(client, "create_order"))
    results, _ = run_moves(aid, client, settings, [(0, t.entry_price * 1.01, client.price)])
    assert results == ["kept"]
    assert len(calls_of(client, "create_order")) - before == 1  # one try, no reduceOnly retry
    assert all(not o["reduce_only"] for o in resting_stops(client))


# ------------------------------------------------------------------------------------------------ the back-off
def test_the_backoff_schedule_is_5_then_15_then_30_minutes():
    assert lifecycle.MOVE_RETRY_BACKOFF_SCHEDULE == (300, 900, 1800)
    assert [lifecycle.move_backoff_seconds(n) for n in (1, 2, 3, 4, 10, 0)] == [300, 900, 1800, 1800, 1800, 300]


def test_a_repeating_failure_backs_off_5_then_15_then_30_minutes_and_is_counted(monkeypatch, db, caplog):
    client, settings, aid, t = long_trade(monkeypatch)
    client.reject_second_close_stop = True
    client.reject_reduce_only_stop = True
    price = client.price
    minutes = [0, 4, 5, 10, 19, 20, 36, 49, 50, 79, 80]
    steps = [(m, t.entry_price * 1.01, price) for m in minutes]
    marks = {}

    def before(i, ctx):
        marks[i] = len(calls_of(client, "create_order"))

    with caplog.at_level(logging.WARNING):
        results, notifier = run_moves(aid, client, settings, steps, before=before)
    marks[len(steps)] = len(calls_of(client, "create_order"))
    assert results == ["kept"] * len(steps)
    sent = [marks[i + 1] - marks[i] for i in range(len(steps))]  # create_order calls made during each step
    # tries (closePosition, then reduceOnly) at minute 0; again at 5 (5 minutes after the first failure); at 20 (15 after the second); at 50 and 80 (30 after the third and fourth). Nothing is sent in between.
    assert sent == [2, 0, 2, 0, 0, 2, 0, 0, 2, 0, 2]
    assert lifecycle.move_failures(t.id) == 5
    lines = [r.getMessage() for r in caplog.records if "trailing stop move" in r.getMessage()]
    assert len(lines) == 5
    for i, (n, wait) in enumerate([(1, 5), (2, 15), (3, 30), (4, 30), (5, 30)]):
        assert f"consecutive failures {n};" in lines[i] and f"next try in {wait} minutes" in lines[i], lines[i]
    assert len([x for x in notifier.texts if "Could not move" in x]) == 1  # the customer alert stays once per 6 hours


def test_one_transient_failure_costs_five_minutes_not_thirty(monkeypatch, db):
    client, settings, aid, t = long_trade(monkeypatch)
    client.fail("create_order", role="stop", exc=ccxt.RequestTimeout("binanceusdm timed out"), times=1)  # a single blip
    results, _ = run_moves(aid, client, settings, [(0, t.entry_price * 1.01, client.price), (4, t.entry_price * 1.01, client.price), (5, t.entry_price * 1.01, client.price)])
    assert results == ["kept", "kept", "moved"]  # backed off at minute 4, tried again at minute 5 and moved
    assert lifecycle.move_failures(t.id) == 0


def test_the_customer_alert_repeats_after_six_hours_not_before(monkeypatch, db):
    client, settings, aid, t = long_trade(monkeypatch)
    client.reject_second_close_stop = True
    client.reject_reduce_only_stop = True
    price = client.price
    steps = [(m, t.entry_price * 1.01, price) for m in (0, 31, 62, 200, 331, 362, 393)]
    _, notifier = run_moves(aid, client, settings, steps)
    assert len([x for x in notifier.texts if "Could not move" in x]) == 2  # at minute 0 and again after 6 hours (minute 393 > 360)


def test_a_success_clears_the_failure_count(monkeypatch, db):
    client, settings, aid, t = long_trade(monkeypatch)
    client.reject_second_close_stop = True
    client.reject_reduce_only_stop = True
    price = client.price

    def before(i, ctx):
        if i == 1:
            client.reject_reduce_only_stop = False

    results, _ = run_moves(aid, client, settings, [(0, t.entry_price * 1.01, price), (31, t.entry_price * 1.01, price)], before=before)
    assert results == ["kept", "moved"] and lifecycle.move_failures(t.id) == 0


def test_the_backoff_never_delays_an_exit(monkeypatch, db):
    """The lock's exit (price through the level) goes through execute_exit, not move_stop: a backed-off move cannot hold it back."""
    import inspect

    from mba_bot import trader

    src = inspect.getsource(trader._live_profit_lock)
    assert src.index('action == "exit"') < src.index('action == "move"') and "execute_exit" in src.split('action == "move"')[0]
    assert "_MOVE_FAILURES" not in src and "backed" not in src


# ------------------------------------------------------------------------------------------------ the journal
def test_the_journal_carries_only_the_class_the_code_and_a_truncated_message(monkeypatch, db, caplog):
    client, settings, aid, t = long_trade(monkeypatch)
    long_msg = "M" * 400
    secret = "SECRET-api-key-ABC123 signature=deadbeefdeadbeefdeadbeef"
    boom = ccxt.ExchangeError('binanceusdm POST https://fapi.binance.com/fapi/v1/algoOrder?timestamp=1&signature=cafebabecafebabecafebabe X-MBX-APIKEY: ' + secret + ' {"code":-4130,"msg":"' + long_msg + '"}')
    client.fail("create_order", role="stop", exc=boom, times=1)
    client.reject_reduce_only_stop = True
    with caplog.at_level(logging.DEBUG):
        results, _ = run_moves(aid, client, settings, [(0, t.entry_price * 1.01, client.price)])
    assert results == ["kept"]
    text = caplog.text
    assert "attempted=closePosition" in text and "error_class=ExchangeError" in text and "code=-4130" in text
    assert "M" * 200 in text and "M" * 201 not in text  # truncated to 200 characters
    for needle in ("SECRET", "ABC123", "deadbeef", "cafebabe", "X-MBX-APIKEY", "algoOrder", "timestamp=", "fapi.binance.com"):
        assert needle not in text, needle


def test_place_protection_raises_when_both_attempts_are_refused_and_says_what_was_tried(monkeypatch, db):
    client, settings, aid, t = long_trade(monkeypatch)
    client.reject_second_close_stop = True
    client.reject_reduce_only_stop = True
    out = {}

    async def go():
        with session_scope(autoflush=False) as session:
            ctx = live_context(session, aid, client, settings, FakeNotifier())
            await ctx.venue.prepare(managing=True)
            try:
                await ctx.venue.place_protection(side="long", quantity=t.quantity, stop_price=t.entry_price * 1.01, client_id="x-stop-9", entry_price=t.entry_price)
            except Exception as exc:  # noqa: BLE001
                out["exc"] = exc

    asyncio.run(go())
    exc = out["exc"]
    assert isinstance(exc, ccxt_async.ExchangeError)
    assert "attempted=reduceOnly" in exc.protection_diag and "first attempt: attempted=closePosition" in exc.protection_diag and "code=-4130" in exc.protection_diag


def test_exchange_error_info_reads_only_code_and_message():
    info = venue.exchange_error_info(ccxt.ExchangeError('binanceusdm {"code":-4130,"msg":"An open stop\\nexists."}'))
    assert info == {"error_class": "ExchangeError", "code": -4130, "msg": "An open stop exists."}
    none = venue.exchange_error_info(ValueError("no json here, key=ABCDEF"))
    assert none == {"error_class": "ValueError", "code": None, "msg": None}


# ------------------------------------------------------------------------------------------------ Spot is unchanged
def test_spot_does_not_use_the_futures_fallback(monkeypatch, db):
    client, settings, aid = open_protected(monkeypatch, market="spot")
    t = the_trade(aid)
    price = t.entry_price * 1.05
    client.price = price
    before_kind = t.protective_kind
    assert not before_kind.startswith("stop_market")
    client.fail("create_order", role="stop", exc=ccxt.ExchangeError('binance {"code":-4130,"msg":"x"}'), times=5)
    results, _ = run_moves(aid, client, settings, [(0, t.entry_price * 1.01, price)])
    assert results == ["kept"]
    assert the_trade(aid).protective_kind == before_kind and not any(o.get("reduce_only") for o in client.orders.values())


# ------------------------------------------------------------------------------------------------ a flat position after an exit
def test_a_stop_that_cannot_be_confirmed_cancelled_on_a_flat_position_is_not_a_false_protection_still_open(monkeypatch, db):
    client, settings, aid, t = long_trade(monkeypatch)
    old_cid = lifecycle.client_id(t, "stop", t.protection_attempt)
    client.externally_close_position()  # the position is flat; the closePosition stop is left resting by the model
    client.fail("cancel_order", role="stop", exc=ccxt.ExchangeError('binanceusdm {"code":-2011,"msg":"Unknown order sent."}'), times=1)
    out = {}

    async def go():
        with session_scope(autoflush=False) as session:
            ctx = live_context(session, aid, client, settings, FakeNotifier())
            await ctx.venue.prepare(managing=True)
            out["final"] = await ctx.venue.cancel_protection(kind=venue.KIND_FUTURES_CLOSE, client_id=old_cid)

    asyncio.run(go())
    assert out["final"] is None  # gone, not ProtectionStillOpen


def test_the_same_stop_on_an_open_position_still_raises_protection_still_open(monkeypatch, db):
    client, settings, aid, t = long_trade(monkeypatch)
    old_cid = lifecycle.client_id(t, "stop", t.protection_attempt)
    client.fail("cancel_order", role="stop", exc=ccxt.ExchangeError('binanceusdm {"code":-2011,"msg":"Unknown order sent."}'), times=1)

    async def go():
        with session_scope(autoflush=False) as session:
            ctx = live_context(session, aid, client, settings, FakeNotifier())
            await ctx.venue.prepare(managing=True)
            await ctx.venue.cancel_protection(kind=venue.KIND_FUTURES_CLOSE, client_id=old_cid)

    with pytest.raises(venue.ProtectionStillOpen):
        asyncio.run(go())


def _cancel_with_unreadable_position(client, settings, aid, cid):
    async def go():
        with session_scope(autoflush=False) as session:
            ctx = live_context(session, aid, client, settings, FakeNotifier())
            await ctx.venue.prepare(managing=True)

            async def unreadable():
                raise ccxt.NetworkError("binanceusdm position read failed")

            ctx.venue.signed_position = unreadable
            await ctx.venue.cancel_protection(kind=venue.KIND_FUTURES_CLOSE, client_id=cid)

    asyncio.run(go())


@pytest.mark.parametrize("really_flat", [True, False])
def test_an_unreadable_position_is_never_taken_for_flat(monkeypatch, db, really_flat):
    """The stop still reads as resting and the position cannot be read: ProtectionStillOpen, exactly as before, whether or not the position happens to be flat."""
    client, settings, aid, t = long_trade(monkeypatch)
    old_cid = lifecycle.client_id(t, "stop", t.protection_attempt)
    if really_flat:
        client.externally_close_position()
    client.fail("cancel_order", role="stop", exc=ccxt.ExchangeError('binanceusdm {"code":-2011,"msg":"Unknown order sent."}'), times=1)
    with pytest.raises(venue.ProtectionStillOpen):
        _cancel_with_unreadable_position(client, settings, aid, old_cid)
    assert resting_stops(client)  # the stop still rests: nothing was reported gone


@pytest.mark.parametrize("fallback", [False, True])
def test_a_stop_that_still_rests_on_a_position_that_is_not_flat_raises_protection_still_open(monkeypatch, db, fallback):
    """Both stop kinds (closePosition, and the reduceOnly one the fallback places): resting plus an open position is never 'gone'."""
    client, settings, aid, t = long_trade(monkeypatch)
    if fallback:
        client.reject_second_close_stop = True
        run_moves(aid, client, settings, [(0, t.entry_price * 1.01, client.price)])
        t = the_trade(aid)
        assert t.protective_kind == venue.KIND_FUTURES_REDUCE
    cid = lifecycle.client_id(t, "stop", t.protection_attempt)
    assert abs(client.position) > 0
    client.fail("cancel_order", role="stop", exc=ccxt.ExchangeError('binanceusdm {"code":-2011,"msg":"Unknown order sent."}'), times=1)

    async def go():
        with session_scope(autoflush=False) as session:
            ctx = live_context(session, aid, client, settings, FakeNotifier())
            await ctx.venue.prepare(managing=True)
            await ctx.venue.cancel_protection(kind=t.protective_kind, client_id=cid)

    with pytest.raises(venue.ProtectionStillOpen):
        asyncio.run(go())


def test_an_exit_whose_stop_cancel_failed_leaves_no_stop_resting_after_the_trade_is_closed(monkeypatch, db, caplog):
    client, settings, aid, t = long_trade(monkeypatch)
    client.fail("cancel_order", role="stop", exc=ccxt.ExchangeError('binanceusdm {"code":-2011,"msg":"Unknown order sent."}'), times=1)
    result = {}

    async def go():
        with session_scope(autoflush=False) as session:
            ctx = live_context(session, aid, client, settings, FakeNotifier())
            trade = session.get(Trade, t.id)
            await ctx.venue.prepare(managing=True)
            result["r"] = await lifecycle.execute_exit(ctx, trade, reason="profit_lock", price_hint=client.price)

    with caplog.at_level(logging.WARNING):
        asyncio.run(go())
    assert result["r"] == "closed" and the_trade(aid).status == "closed" and client.position == 0
    assert resting_stops(client) == []  # the old stop that could not be confirmed cancelled did not outlive the trade
    assert "still reads as resting after cancel" in caplog.text and "protective stop cancel failed" in caplog.text and "code=-2011" in caplog.text


def test_a_flat_position_with_no_listed_conditional_order_closes_as_closed_on_the_exchange(monkeypatch, db):
    """Nothing resting and nothing held: the reconcile pass records the trade closed from exchange truth (position 0, stop not triggered -> externally closed)."""
    from helpers_live import run_reconcile

    client, settings, aid, t = long_trade(monkeypatch)
    old_cid = lifecycle.client_id(t, "stop", t.protection_attempt)
    client.orders[old_cid]["status"] = "canceled"
    client.externally_close_position()
    run_reconcile(aid, settings)
    after = the_trade(aid)
    assert after.status == "closed" and after.close_reason == "externally_closed"
    assert resting_stops(client) == []
