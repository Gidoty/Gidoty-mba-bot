"""PR 2, foreign-order alarm (FOREIGN_ORDER_ALARM, default off): an alert when the account holds an open order or a position the bot did not open, without touching it.

PROVES (fake exchange, scratch SQLite, frozen clock): off by default (no read); the pure rule (bot orders ignored, other orders and positions on pairs the bot has no trade on flagged, zero and unreadable
entries skipped); one alert per distinct set of findings per 24 hours; one look per interval; the bot's own trade and stop never raise it; it only reads (nothing cancelled, closed or placed); a failed
read raises no alert and stops nothing; paper accounts are not looked at.
DOES NOT PROVE: Binance's request weights for the extra reads, or what real orders and positions look like beyond the ccxt shape."""
from __future__ import annotations

from datetime import timedelta

import pytest
from helpers_live import BEARISH_OHLCV, BULLISH_OHLCV, FakeNotifier, all_trades, futures_client, install, make_settings, new_account, run_cycle, spot_client
from test_account_daily_loss import NOON, rig

from mba_bot import foreign_orders as fo
from mba_bot import trader
from mba_bot.db import init_db, session_scope
from mba_bot.models import DecisionLog, TradingAccount


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")
    return tmp_path


def settings_(**over):
    base = dict(max_notional_multiplier=100.0, trade_telemetry=True, foreign_order_alarm="alert", protection_precheck_mode="off")
    base.update(over)
    return make_settings(**base)


class Mono:
    """A controllable monotonic clock for the once-per-interval look."""

    def __init__(self, monkeypatch):
        self.t = 1000.0
        monkeypatch.setattr(trader, "_foreign_now", lambda: self.t)


FOREIGN_POSITION = {"symbol": "ETH/USDT:USDT", "contracts": 0.5, "side": "long", "info": {"positionAmt": "0.5"}}
FOREIGN_ORDER = {"id": "9001", "clientOrderId": "web_abc123", "symbol": "BTC/USDT", "status": "open", "trigger": False}


def texts(n: FakeNotifier):
    return [t for t in n.texts if "did not open" in t]


# ------------------------------------------------------------------ pure rules
def test_the_mode_parser_defaults_to_off():
    assert [fo.parse_mode(x) for x in (None, "", "off", "0", "banana", "alert", "ALERT", "1", "true", "on")] == ["off"] * 5 + ["alert"] * 5


def test_settings_defaults_and_clamps(monkeypatch):
    from mba_bot.config import load_settings

    for k in ("FOREIGN_ORDER_ALARM", "FOREIGN_ORDER_CHECK_MINUTES"):
        monkeypatch.delenv(k, raising=False)
    s = load_settings()
    assert (s.foreign_order_alarm, s.foreign_order_check_minutes) == ("off", 15.0)
    monkeypatch.setenv("FOREIGN_ORDER_ALARM", "1")
    monkeypatch.setenv("FOREIGN_ORDER_CHECK_MINUTES", "1")
    assert (load_settings().foreign_order_alarm, load_settings().foreign_order_check_minutes) == ("alert", 5.0)
    monkeypatch.setenv("FOREIGN_ORDER_CHECK_MINUTES", "9999")
    assert load_settings().foreign_order_check_minutes == 240.0


def test_the_pure_rule():
    bot_order = {"id": "1", "clientOrderId": "mba" + "x" * 30, "symbol": "BTC/USDT:USDT"}
    other = {"id": "2", "clientOrderId": "web_1", "symbol": "ETH/USDT:USDT"}
    no_id = {"id": "3", "symbol": "BNB/USDT:USDT"}
    positions = [
        {"symbol": "BTC/USDT:USDT", "contracts": 0.2, "side": "long"},  # the bot's own pair
        {"symbol": "ETH/USDT:USDT", "contracts": 0.5, "side": "long"},  # not the bot's
        {"symbol": "XRP/USDT:USDT", "contracts": 0, "side": None},  # flat
        {"symbol": "SOL/USDT:USDT", "contracts": None, "info": {"positionAmt": "nan"}},  # unreadable: not a finding
        {"symbol": "DOGE/USDT:USDT", "info": {"positionAmt": "-3"}},  # read from info, a short
        "junk",
    ]
    found = fo.find_foreign([bot_order, other, no_id, "junk"], positions, {"BTC/USDT"})
    assert [(f.kind, f.symbol) for f in found] == [("order", "BNB/USDT"), ("order", "ETH/USDT"), ("position", "DOGE/USDT"), ("position", "ETH/USDT")]
    assert fo.find_foreign([], [], set()) == [] and fo.find_foreign(None, None, set()) == []


def test_the_fingerprint_changes_with_the_findings_and_the_message_is_safe():
    a = fo.find_foreign([FOREIGN_ORDER], [], set())
    b = fo.find_foreign([FOREIGN_ORDER], [FOREIGN_POSITION], set())
    assert fo.fingerprint(a) != fo.fingerprint(b) and fo.fingerprint(a) == fo.fingerprint(list(a))
    msg = fo.message(b)
    assert "did not open" in msg and "has not touched" in msg and "will not" in msg and "ETH/USDT" in msg
    assert "web_abc123" not in msg and "9001" not in msg and "key" not in msg.lower().replace("api key", "")


# ------------------------------------------------------------------ the cycle
def test_off_by_default_makes_no_read(db, monkeypatch):
    st = make_settings(max_notional_multiplier=100.0)
    assert st.foreign_order_alarm == "off"
    client, clock, aid = rig(monkeypatch, st, market="futures")
    client.other_positions = [FOREIGN_POSITION]
    n = FakeNotifier()
    run_cycle(aid, st, n)
    assert texts(n) == [] and not [c for c in client.calls if c[0] == "fetch_positions"]


def test_a_foreign_position_and_order_raise_one_alert_and_nothing_is_touched(db, monkeypatch):
    mono = Mono(monkeypatch)
    st = settings_()
    client, clock, aid = rig(monkeypatch, st, market="futures")
    client.other_positions = [FOREIGN_POSITION]
    client.foreign_orders = [{**FOREIGN_ORDER, "symbol": "BTC/USDT:USDT"}]
    n = FakeNotifier()
    before_orders = {k: dict(v) for k, v in client.orders.items()}
    calls_before = len(client.calls)
    run_cycle(aid, st, n)
    assert len(texts(n)) == 1 and "ETH/USDT" in texts(n)[0] and "2 open orders or positions" in texts(n)[0]
    added = {c[0] for c in client.calls[calls_before:]}
    assert not added & {"create_order", "cancel_order", "edit_order", "set_leverage", "set_margin_mode", "cancel_all_orders"}  # only reads
    assert {k: dict(v) for k, v in client.orders.items()} == before_orders and client.foreign_orders and client.other_positions  # nothing touched
    with session_scope() as s:
        rows = [d for d in s.query(DecisionLog).all() if d.reason_code == "foreign_order_or_position"]
        assert len(rows) == 1 and rows[0].outcome == "observed"


def test_one_alert_per_set_per_day_one_look_per_interval_and_a_new_finding_alerts_again(db, monkeypatch):
    mono = Mono(monkeypatch)
    st = settings_(foreign_order_check_minutes=15.0)
    client, clock, aid = rig(monkeypatch, st, market="futures")
    client.other_positions = [FOREIGN_POSITION]
    n = FakeNotifier()
    run_cycle(aid, st, n)
    reads = lambda: len([c for c in client.calls if c[0] == "fetch_positions"])
    assert reads() >= 1 and len(texts(n)) == 1
    first = reads()
    mono.t += 5 * 60
    clock.instant = NOON + timedelta(minutes=5)
    run_cycle(aid, st, n)
    assert reads() == first  # inside the interval: no look at all
    mono.t += 11 * 60
    clock.instant = NOON + timedelta(minutes=16)
    run_cycle(aid, st, n)
    assert reads() == first + 1 and len(texts(n)) == 1  # looked again, same finding, no second alert within 24 hours
    client.other_positions = [FOREIGN_POSITION, {"symbol": "XRP/USDT:USDT", "contracts": 2.0, "side": "short", "info": {"positionAmt": "-2"}}]
    mono.t += 16 * 60
    clock.instant = NOON + timedelta(minutes=33)
    run_cycle(aid, st, n)
    assert len(texts(n)) == 2  # a different set of findings alerts again
    mono.t += 16 * 60
    clock.instant = NOON + timedelta(hours=25)
    run_cycle(aid, st, n)
    assert len(texts(n)) == 3  # the same set again after 24 hours


def test_the_bots_own_trade_and_its_protective_stop_never_raise_it(db, monkeypatch):
    mono = Mono(monkeypatch)
    from helpers_live import open_protected

    client, base, aid = open_protected(monkeypatch, market="futures")
    import dataclasses

    st = dataclasses.replace(base, foreign_order_alarm="alert", trade_telemetry=True)
    assert any(o["conditional"] for o in client.orders.values())  # the resting protective stop
    n = FakeNotifier()
    run_cycle(aid, st, n)
    assert texts(n) == [] and all_trades(aid)[0].status == "open"


def test_a_spot_foreign_open_order_is_flagged_and_balances_are_not(db, monkeypatch):
    mono = Mono(monkeypatch)
    st = settings_()
    client, clock, aid = rig(monkeypatch, st, market="spot")
    client.free["ETH"] = 3.0  # coins held for other reasons: normal
    n = FakeNotifier()
    run_cycle(aid, st, n)
    assert texts(n) == []
    client.foreign_orders = [{**FOREIGN_ORDER, "symbol": "BTC/USDT"}]
    mono.t += 16 * 60
    clock.instant = NOON + timedelta(minutes=16)
    run_cycle(aid, st, n)
    assert len(texts(n)) == 1 and client.orders == {}


def test_a_failed_read_raises_no_alert_and_stops_nothing(db, monkeypatch, caplog):
    mono = Mono(monkeypatch)
    st = settings_()
    client, clock, aid = rig(monkeypatch, st, market="futures")

    async def boom(*a, **k):
        raise RuntimeError("down")

    client.fetch_open_orders = boom
    client.fetch_positions = boom
    n = FakeNotifier()
    run_cycle(aid, st, n)
    assert texts(n) == []
    with session_scope() as s:
        assert s.get(TradingAccount, aid).last_scanned_at is not None
    assert any("foreign-order check could not read" in r.getMessage() for r in caplog.records)


def test_a_paper_account_is_not_looked_at(db, monkeypatch):
    mono = Mono(monkeypatch)
    st = settings_(testnet_spot_api_key="paper-key-1234567890", testnet_spot_api_secret="paper-secret-1234567890")
    client, clock, aid = rig(monkeypatch, st, market="spot", balance=1.0)
    with session_scope() as s:
        s.get(TradingAccount, aid).bot_started_at = None
    client.foreign_orders = [{**FOREIGN_ORDER, "symbol": "BTC/USDT"}]
    n = FakeNotifier()
    run_cycle(aid, st, n)
    assert texts(n) == [] and not [c for c in client.calls if c[0] == "fetch_open_orders"]


def test_no_look_after_an_exception(db, monkeypatch):
    mono = Mono(monkeypatch)
    st = settings_()
    client, clock, aid = rig(monkeypatch, st, market="futures")
    client.other_positions = [FOREIGN_POSITION]

    async def boom(*a, **k):
        raise RuntimeError("exchange down")

    client.fetch_ohlcv = boom
    n = FakeNotifier()
    run_cycle(aid, st, n, swallow=True)
    assert texts(n) == [] and not [c for c in client.calls if c[0] == "fetch_positions"]
