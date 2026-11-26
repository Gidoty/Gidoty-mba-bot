"""Part G: the give-back metric and the owner alert (docs/safety/give_back_alarm.md).

PROVES: the give-back flags (reached +1 R, closed below entry net of assumed costs, closed far below the peak) for long and short and every missing-data case; the report and the admin SQL give the same counts on the same
trades; the alert fires once per trade at the set fraction, never before the best price reached the set R, never for a paper trade, is off by default and a typo cannot switch it on, carries no personal data, and changes nothing.
DOES NOT PROVE: that the alert reaches a phone (Telegram is replaced by a recorder here)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from helpers_live import make_settings, new_account, run_cycle, the_trade
from helpers_profit import candles, live

from mba_bot import admin_service, config, reporting, trader
from mba_bot.db import init_db, session_scope
from mba_bot.models import Trade

U = timezone.utc


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")


def T(side="long", entry=100.0, stop=90.0, peak=112.0, exit_=99.0, **kw):
    return SimpleNamespace(side=side, entry_price=entry, initial_stop_price=stop, peak_price=peak, exit_price=exit_, **kw)


# ---------------------------------------------------------------------------------------------- the flags
def test_a_trade_that_reached_1r_and_closed_below_entry_net_of_costs_is_a_give_back():
    assert reporting.giveback_flags(T()) == (True, True, True, True)  # peak +1.2R, exit 99 (below entry, 1.3R under the peak)
    assert reporting.giveback_flags(T(exit_=100.2))[2] is True  # above the entry but not by the round-trip cost (0.21): still below entry NET of costs
    assert reporting.giveback_flags(T(exit_=100.21))[2] is False and reporting.giveback_flags(T(exit_=100.5))[2] is False
    assert reporting.giveback_flags(T(peak=109.9))[1:3] == (False, False)  # never reached +1R: not counted, whatever it did
    assert reporting.giveback_flags(T(peak=110.0))[1] is True  # exactly +1R counts


def test_a_short_is_the_mirror_image():
    assert reporting.giveback_flags(T(side="short", entry=100.0, stop=110.0, peak=88.0, exit_=101.0)) == (True, True, True, True)
    assert reporting.giveback_flags(T(side="short", entry=100.0, stop=110.0, peak=88.0, exit_=99.8))[2] is True  # below the entry by less than the cost
    assert reporting.giveback_flags(T(side="short", entry=100.0, stop=110.0, peak=88.0, exit_=99.7))[2] is False
    assert reporting.giveback_flags(T(side="short", entry=100.0, stop=110.0, peak=91.0, exit_=101.0))[1:3] == (False, False)


def test_closed_far_below_the_peak_means_more_than_1r_worse_than_the_best_price():
    assert reporting.giveback_flags(T(peak=125.0, exit_=114.9))[3] is True  # 10.1 below the peak, R = 10
    assert reporting.giveback_flags(T(peak=125.0, exit_=115.0))[3] is False  # exactly 1R: not more than
    assert reporting.giveback_flags(T(side="short", entry=100.0, stop=110.0, peak=75.0, exit_=85.1))[3] is True


def test_missing_data_is_never_counted():
    for bad in (T(peak=None), T(stop=None), T(exit_=None), T(stop=100.0)):
        assert reporting.giveback_flags(bad) == (False, False, False, False)


def test_the_report_counts_the_flags_and_gives_a_rate_over_the_trades_that_reached_1r():
    trades = [
        SimpleNamespace(status="closed", is_paper=False, is_demo=False, pnl=-1.0, entry_fee_quote=None, exit_fee_quote=None, strategy_id=None, pair="BTC/USDT", **T(peak=112.0, exit_=99.0).__dict__),
        SimpleNamespace(status="closed", is_paper=False, is_demo=False, pnl=5.0, entry_fee_quote=None, exit_fee_quote=None, strategy_id=None, pair="BTC/USDT", **T(peak=125.0, exit_=122.0).__dict__),
        SimpleNamespace(status="closed", is_paper=False, is_demo=False, pnl=0.3, entry_fee_quote=None, exit_fee_quote=None, strategy_id=None, pair="BTC/USDT", **T(peak=104.0, exit_=103.0).__dict__),
        SimpleNamespace(status="closed", is_paper=False, is_demo=False, pnl=0.0, entry_fee_quote=None, exit_fee_quote=None, strategy_id=None, pair="BTC/USDT", **T(peak=None, exit_=100.0).__dict__),
    ]
    g = reporting.build(trades)["modes"][0]["total"]
    assert (g["trades_with_peak"], g["reached_1r"], g["gave_back_to_loss"], g["give_back_rate"], g["closed_far_below_peak"]) == (3, 2, 1, 0.5, 1)
    none = reporting.build([trades[3]])["modes"][0]["total"]
    assert none["trades_with_peak"] == 0 and none["give_back_rate"] is None


def test_the_admin_sql_gives_the_same_counts_as_the_report(db):
    settings = make_settings()
    aid = new_account(settings, market_type="futures", email="gb@example.com")
    now = datetime.now(U)
    rows = [("long", 100.0, 90.0, 112.0, 99.0), ("long", 100.0, 90.0, 125.0, 122.0), ("long", 100.0, 90.0, 104.0, 103.0), ("long", 100.0, 90.0, None, 100.0),
            ("short", 100.0, 110.0, 88.0, 101.0), ("short", 100.0, 110.0, 75.0, 85.1), ("long", 100.0, 90.0, 125.0, 114.9), ("long", 100.0, 90.0, 110.0, 100.2), ("short", 100.0, 110.0, 88.0, 100.1)]
    with session_scope() as s:
        for side, entry, stop, peak, exit_ in rows:
            s.add(Trade(account_id=aid, side=side, is_paper=False, entry_price=entry, quantity=1.0, stop_loss_price=stop, initial_stop_price=stop, peak_price=peak, exit_price=exit_, pnl=exit_ - entry,
                        status="closed", closed_at=now - timedelta(hours=1), pair="BTC/USDT", live_slot=None))
        trades = s.query(Trade).all()
        want = reporting.build(trades)["modes"][0]["total"]
        got = admin_service.results_panel(s, now)["totals"][0]
    for k in ("trades_with_peak", "reached_1r", "gave_back_to_loss", "closed_far_below_peak", "give_back_rate"):
        assert got[k] == want[k], k
    assert want["reached_1r"] >= 4 and want["gave_back_to_loss"] >= 2 and want["closed_far_below_peak"] >= 2  # the data exercises every flag


# ---------------------------------------------------------------------------------------------- the alert
def recorder(monkeypatch):
    sent = []

    async def fake(settings, text):
        sent.append(text)
        return True

    monkeypatch.setattr(trader.strategy_guards, "alert_owner", fake)
    return sent


def test_the_alert_is_off_by_default_and_a_typo_never_switches_it_on(monkeypatch):
    for n in ("GIVEBACK_ALERT_FRACTION", "GIVEBACK_ALERT_MIN_PEAK_R"):
        monkeypatch.delenv(n, raising=False)
    s = config.load_settings()
    assert (s.giveback_alert_fraction, s.giveback_alert_min_peak_r) == (0.0, 1.0)
    for bad in ("abc", "", "-1", "0", "nan", "inf", " ", "50%"):
        monkeypatch.setenv("GIVEBACK_ALERT_FRACTION", bad)
        assert config.load_settings().giveback_alert_fraction == 0.0, bad
    monkeypatch.setenv("GIVEBACK_ALERT_FRACTION", "0.01")
    assert config.load_settings().giveback_alert_fraction == 0.1
    monkeypatch.setenv("GIVEBACK_ALERT_FRACTION", "7")
    assert config.load_settings().giveback_alert_fraction == 0.95


def test_one_alert_per_trade_when_the_set_fraction_of_the_peak_profit_is_given_back_and_nothing_changes(db, monkeypatch):
    sent = recorder(monkeypatch)
    client, settings, aid, base = live(monkeypatch, giveback_alert_fraction=0.5)
    candles(client, base, [(95, 100, 92, 99), (99, 140, 98, 120)])  # peak 140 (+1.06 R), now 120: 40% given back
    run_cycle(aid, settings)
    assert sent == []
    candles(client, base, [(95, 100, 92, 99), (99, 140, 98, 110)])  # now 110: 60% given back
    stops_before = len(client.orders_by_role("stop"))
    run_cycle(aid, settings)
    assert len(sent) == 1 and "Give-back" in sent[0] and "+1.1 R" in sent[0] and "Alert only" in sent[0]
    run_cycle(aid, settings)
    candles(client, base, [(95, 100, 92, 99), (99, 140, 98, 100)])
    run_cycle(aid, settings)
    assert len(sent) == 1  # once per trade, not once per round
    t = the_trade(aid)
    assert t.status == "open" and t.stop_loss_price == 43.0 and len(client.orders_by_role("stop")) == stops_before and not client.orders_by_role("exit")  # alert only
    assert "@" not in sent[0] and "example.com" not in sent[0]  # no email, no name


def test_no_alert_when_the_best_price_never_reached_the_set_r_or_with_the_switch_off(db, monkeypatch):
    sent = recorder(monkeypatch)
    client, settings, aid, base = live(monkeypatch, giveback_alert_fraction=0.5)
    candles(client, base, [(95, 100, 92, 99), (99, 120, 98, 91)])  # peak 120 = +0.64 R: below the default +1 R
    run_cycle(aid, settings)
    assert sent == []


def test_the_switch_off_sends_nothing_however_far_it_falls(db, monkeypatch):
    sent = recorder(monkeypatch)
    client, settings, aid, base = live(monkeypatch)
    candles(client, base, [(95, 100, 92, 99), (99, 140, 98, 100)])
    run_cycle(aid, settings)
    assert sent == []


def test_a_paper_trade_never_alerts_the_owner(db, monkeypatch):
    sent = recorder(monkeypatch)
    now = datetime.now(U)
    aid = new_account(make_settings(), market_type="futures", email="gbp@example.com")
    with session_scope() as s:
        t = Trade(account_id=aid, side="long", is_paper=True, entry_price=100.0, quantity=1.0, stop_loss_price=90.0, initial_stop_price=90.0, peak_price=130.0, status="open", opened_at=now - timedelta(hours=3), pair="BTC/USDT")
        s.add(t)
        s.flush()
        acct = SimpleNamespace(id=aid, bot_code="b1", pair="BTC/USDT")
        import asyncio

        asyncio.run(trader._giveback_alert(s, acct, t, 105.0, make_settings(giveback_alert_fraction=0.5), now=now))
    assert sent == []


# ---------------------------------------------------------------------------------------------- the alert never raises into the cycle
def _locked_error():
    import sqlite3

    from sqlalchemy.exc import OperationalError

    return OperationalError("stmt", {}, sqlite3.OperationalError("database is locked"))


class _ExpiredTrade:
    """Every attribute read fails once broken: an expired ORM object whose reload hits 'database is locked'. The id is readable only before that."""

    def __init__(self):
        object.__setattr__(self, "broken", False)
        object.__setattr__(self, "_v", dict(id="trade-789", is_paper=False, status="open", side="long", entry_price=100.0, initial_stop_price=90.0, peak_price=140.0, pair="BTCUSDT"))

    def __getattr__(self, name):
        if object.__getattribute__(self, "broken"):
            raise _locked_error()
        try:
            return object.__getattribute__(self, "_v")[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value) if name == "broken" else object.__getattribute__(self, "_v").__setitem__(name, value)


class _Sess:
    def __init__(self, *, active=True, rollback_fails=False):
        self.is_active, self.rollbacks, self.rollback_fails = active, 0, rollback_fails

    def rollback(self):
        self.rollbacks += 1
        if self.rollback_fails:
            raise _locked_error()


def _alert(trade, session, settings, price=100.0):
    import asyncio

    asyncio.run(trader._giveback_alert(session, SimpleNamespace(id="a", pair="BTCUSDT", bot_code="X"), trade, price, settings, now=datetime.now(U)))


def test_the_handler_reads_no_orm_attribute_when_the_trade_is_expired_and_nothing_escapes(monkeypatch, caplog):
    t = _ExpiredTrade()
    settings = make_settings(giveback_alert_fraction=0.5)

    def claim(*a, **k):
        t.broken = True  # the failed commit leaves the trade expired, then the reload fails
        raise _locked_error()

    monkeypatch.setattr(trader.protection_guard, "claim_alert", claim)
    s = _Sess(active=False)
    with caplog.at_level("ERROR"):
        _alert(t, s, settings)  # must return normally
    assert "trade trade-789: could not send the give-back alert" in caplog.text
    assert s.rollbacks == 1  # a transaction that cannot continue is rolled back


def test_a_healthy_transaction_is_not_rolled_back_by_the_handler(monkeypatch):
    t = _ExpiredTrade()
    monkeypatch.setattr(trader.protection_guard, "claim_alert", lambda *a, **k: (_ for _ in ()).throw(_locked_error()))
    s = _Sess(active=True)
    _alert(t, s, make_settings(giveback_alert_fraction=0.5))
    assert s.rollbacks == 0  # the cycle's other pending changes are kept


def test_a_failing_rollback_and_a_failing_log_are_swallowed(monkeypatch):
    t = _ExpiredTrade()
    monkeypatch.setattr(trader.protection_guard, "claim_alert", lambda *a, **k: (_ for _ in ()).throw(_locked_error()))
    monkeypatch.setattr(trader.logger, "exception", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("log broken")))
    _alert(t, _Sess(active=False, rollback_fails=True), make_settings(giveback_alert_fraction=0.5))  # raises nothing


def test_the_owner_send_failing_is_swallowed(monkeypatch):
    t = _ExpiredTrade()
    monkeypatch.setattr(trader.protection_guard, "claim_alert", lambda *a, **k: True)

    async def boom(settings, text):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(trader.strategy_guards, "alert_owner", boom)
    _alert(t, _Sess(), make_settings(giveback_alert_fraction=0.5))


def test_a_commit_that_fails_in_the_claim_never_breaks_the_real_cycle(db, monkeypatch):
    """Real cycle, real database: the claim's commit hits 'database is locked'. claim_alert falls back to its in-memory rule, the cycle runs to the end, the trade is untouched, no cycle failure is recorded."""
    import sqlite3

    from sqlalchemy import event

    from mba_bot import db as dbmod

    sent = recorder(monkeypatch)
    client, settings, aid, base = live(monkeypatch, giveback_alert_fraction=0.5)
    candles(client, base, [(95, 100, 92, 99), (99, 140, 98, 110)])
    state = {"n": 0}

    def lock_commit(conn, cursor, statement, parameters, context, executemany):
        if "alert_marks" in statement and statement.lstrip().upper().startswith("INSERT"):
            state["n"] += 1
            raise sqlite3.OperationalError("database is locked")

    event.listen(dbmod._engine, "before_cursor_execute", lock_commit)
    try:
        run_cycle(aid, settings)  # raises nothing
    finally:
        event.remove(dbmod._engine, "before_cursor_execute", lock_commit)
    assert state["n"] >= 1
    t = the_trade(aid)
    assert t.status == "open" and t.stop_loss_price == 43.0 and not client.orders_by_role("exit")
    with session_scope() as s:
        from mba_bot.models import TradingAccount

        assert s.get(TradingAccount, aid).last_error is None
