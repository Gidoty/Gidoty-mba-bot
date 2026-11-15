"""Account-wide daily loss pause (ACCOUNT_DAILY_LOSS_PCT) and the Futures leverage ceiling (FUTURES_MAX_LEVERAGE).

PROVES (fake exchange, scratch SQLite, frozen clock): the Lagos calendar-day boundary; day-start equity persisted and measured against realised plus unrealised
equity; the pause blocks NEW live entries only, ends by itself at the next local midnight, writes one audit row and one alert; every unreadable input fails
closed; paper accounts and the default (off) are untouched; the ceiling refuses a leverage above it or a margin mode that is not isolated and never writes to
Binance; the migration is additive and reversible.

DOES NOT PROVE: anything about Binance (the fake proves control flow only), that ccxt's Futures balance carries `totalMarginBalance` as read here, or that the
VPS has a tz database for the zone."""
from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from ema_closed_helpers import Clock
from helpers_live import (BEAR_PRICE, BEARISH_OHLCV, BULLISH_OHLCV, FLAT_OHLCV, FakeNotifier, all_trades, futures_client, install, make_settings, new_account,
                          run_cycle, spot_client)
from sqlalchemy import create_engine, inspect

from mba_bot import account_loss as al
from mba_bot import exchange
from mba_bot.db import init_db, session_scope
from mba_bot.models import AccountDayEquity, AccountLossPause, DecisionLog

U = timezone.utc
ROOT = Path(__file__).resolve().parent.parent
LAGOS = al.zone("Africa/Lagos")
NOON = datetime(2027, 3, 10, 12, 0, tzinfo=U)  # 13:00 in Lagos, the same calendar day


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")


def live_settings(**over):
    return make_settings(max_notional_multiplier=100.0, trade_telemetry=True, account_daily_loss_pct=4.0, **over)


def rig(monkeypatch, settings, *, market="spot", at=NOON, balance=1000.0, email="live@example.com", **client_kw):
    client = (spot_client if market == "spot" else futures_client)(free_balance={"USDT": balance}, ohlcv=FLAT_OHLCV, **client_kw)
    install(monkeypatch, client)
    clock = Clock()
    clock.install(monkeypatch)
    clock.instant = at
    return client, clock, new_account(settings, market_type=market, email=email)


def rows(model):
    with session_scope() as s:
        return list(s.query(model).all())


def reasons():
    with session_scope() as s:
        return [d.reason_code for d in s.query(DecisionLog).all()]


def live_orders(client):
    return [o for o in client.orders.values()]


# ------------------------------------------------------------------ pure rules
def test_the_day_boundary_is_lagos_midnight_not_utc_midnight():
    assert al.day_key(datetime(2027, 3, 10, 22, 59, tzinfo=U), LAGOS) == "2027-03-10"  # 23:59 in Lagos
    assert al.day_key(datetime(2027, 3, 10, 23, 0, tzinfo=U), LAGOS) == "2027-03-11"  # 00:00 in Lagos
    assert al.next_day_start(NOON, LAGOS) == datetime(2027, 3, 10, 23, 0, tzinfo=U)
    assert al.day_start(NOON, LAGOS) == datetime(2027, 3, 9, 23, 0, tzinfo=U)
    assert al.day_key(datetime(2027, 3, 10, 0, 30, tzinfo=U), LAGOS) == "2027-03-10"  # UTC midnight is NOT a boundary: 01:30 Lagos, same day


@pytest.mark.parametrize("start,equity,limit,hit", [
    (1000, 960, 4, True), (1000, 960.01, 4, False), (1000, 959, 4, True), (1000, 1100, 4, False), (1000, 960, 0, False), (1000, 0, 4, True),
    (0, 100, 4, True), (-5, 100, 4, True), (float("nan"), 100, 4, True), (1000, float("nan"), 4, True), (1000, float("-inf"), 4, True),
    (1000, None, 4, True), (None, 1000, 4, True),
])
def test_daily_loss_rule(start, equity, limit, hit):
    assert (al.daily_loss_hit(start_equity=start, equity=equity, limit_pct=limit) is not None) is hit


def test_an_invalid_time_zone_name_is_none():
    assert al.zone("Not/AZone") is None and al.zone("") is None and al.zone("Africa/Lagos") is not None


def test_a_new_day_starts_from_a_recent_sample_else_from_the_first_read(db):
    aid = new_account(make_settings())
    with session_scope() as s:
        al.record_sample(s, aid, 1000.0, datetime(2027, 3, 10, 20, 0, tzinfo=U), LAGOS)  # first ever: approximate
        assert s.query(AccountDayEquity).one().approximate is True
        al.record_sample(s, aid, 990.0, datetime(2027, 3, 10, 22, 40, tzinfo=U), LAGOS)  # 23:40 Lagos, 20 minutes before midnight
    with session_scope() as s:
        d2 = al.record_sample(s, aid, 940.0, datetime(2027, 3, 10, 23, 45, tzinfo=U), LAGOS)  # first read of the new day, after a drop
        assert (d2.day_key, d2.start_equity, d2.approximate) == ("2027-03-11", 990.0, False)  # measured from the last sample, not from the lower first read
    with session_scope() as s:
        al.record_sample(s, aid, 900.0, datetime(2027, 3, 11, 5, 0, tzinfo=U), LAGOS)
        d3 = al.record_sample(s, aid, 800.0, datetime(2027, 3, 12, 22, 0, tzinfo=U), LAGOS)  # the last sample was ~17 hours before midnight: too old
        assert (d3.day_key, d3.start_equity, d3.approximate) == ("2027-03-12", 800.0, True)


# ------------------------------------------------------------------ the pause in the real cycle
def test_default_off_reads_nothing_and_writes_nothing(db, monkeypatch):
    st = make_settings(max_notional_multiplier=100.0, trade_telemetry=True)  # ACCOUNT_DAILY_LOSS_PCT defaults to 0
    assert st.account_daily_loss_pct == 0.0
    client, _, aid = rig(monkeypatch, st)
    client._ohlcv = BULLISH_OHLCV

    async def boom(*a, **k):
        raise AssertionError("the equity read must not run with the pause off")

    monkeypatch.setattr(exchange, "fetch_account_equity", boom)
    run_cycle(aid, st)
    assert all_trades(aid) and rows(AccountDayEquity) == [] and rows(AccountLossPause) == []


def test_no_loss_means_the_entry_goes_through_and_the_start_is_recorded(db, monkeypatch):
    st = live_settings()
    client, _, aid = rig(monkeypatch, st)
    client._ohlcv = BULLISH_OHLCV
    run_cycle(aid, st)
    assert len(all_trades(aid)) == 1
    day = rows(AccountDayEquity)
    assert len(day) == 1 and day[0].start_equity == 1000.0 and day[0].day_key == "2027-03-10" and rows(AccountLossPause) == []


def test_a_loss_at_the_limit_blocks_the_new_entry_writes_one_audit_row_and_sends_one_alert(db, monkeypatch):
    st = live_settings()
    client, clock, aid = rig(monkeypatch, st)
    run_cycle(aid, st)  # an idle cycle takes the day's first sample: 1000
    assert rows(AccountDayEquity)[0].start_equity == 1000.0
    client.free["USDT"] = 960.0  # down 4.0%
    client._ohlcv = BULLISH_OHLCV
    n = FakeNotifier()
    clock.instant = NOON + timedelta(minutes=5)
    run_cycle(aid, st, n)
    assert all_trades(aid) == [] and live_orders(client) == []  # no order was sent
    pauses = rows(AccountLossPause)
    assert len(pauses) == 1 and pauses[0].kind == "daily_loss" and pauses[0].limit_pct == 4.0 and abs(pauses[0].loss_pct - 4.0) < 1e-6
    assert pauses[0].ends_at.replace(tzinfo=U) == datetime(2027, 3, 10, 23, 0, tzinfo=U)  # the next Lagos midnight
    assert pauses[0].start_equity == 1000.0 and pauses[0].equity_at_hit == 960.0 and pauses[0].timezone_name == "Africa/Lagos"
    assert "account_daily_loss_pause" in reasons()
    assert len([t for t in n.texts if "paused for the rest of today" in t]) == 1
    for i in range(3):  # later polls the same day: still blocked, no second row, no second alert
        clock.instant += timedelta(minutes=5)
        run_cycle(aid, st, n)
    assert len(rows(AccountLossPause)) == 1 and len([t for t in n.texts if "paused for the rest of today" in t]) == 1 and all_trades(aid) == []


def test_just_under_the_limit_does_not_block(db, monkeypatch):
    st = live_settings()
    client, clock, aid = rig(monkeypatch, st)
    run_cycle(aid, st)
    client.free["USDT"] = 960.5  # down 3.95%
    client._ohlcv = BULLISH_OHLCV
    clock.instant = NOON + timedelta(minutes=5)
    run_cycle(aid, st)
    assert len(all_trades(aid)) == 1 and rows(AccountLossPause) == []


def test_the_pause_holds_for_the_day_even_if_equity_recovers_and_ends_at_lagos_midnight(db, monkeypatch):
    st = live_settings()
    client, clock, aid = rig(monkeypatch, st)
    run_cycle(aid, st)
    client.free["USDT"] = 900.0
    client._ohlcv = BULLISH_OHLCV
    clock.instant = NOON + timedelta(minutes=5)
    run_cycle(aid, st)
    assert rows(AccountLossPause) and all_trades(aid) == []
    client.free["USDT"] = 1200.0  # equity is back above the start
    clock.instant = datetime(2027, 3, 10, 22, 59, tzinfo=U)  # 23:59 Lagos: still the same day
    run_cycle(aid, st)
    assert all_trades(aid) == []  # no entry until the day ends
    clock.instant = datetime(2027, 3, 10, 23, 1, tzinfo=U)  # 00:01 Lagos: the next day; resets by itself, no owner action
    run_cycle(aid, st)
    assert len(all_trades(aid)) == 1
    days = {d.day_key: d for d in rows(AccountDayEquity)}
    assert days["2027-03-11"].start_equity == 1200.0 and len(rows(AccountLossPause)) == 1  # the new day starts from the last sample, the audit row stays


def test_the_day_start_survives_a_restart_and_does_not_move_up_with_equity(db, monkeypatch):
    st = live_settings()
    client, clock, aid = rig(monkeypatch, st)
    run_cycle(aid, st)
    client.free["USDT"] = 1100.0
    clock.instant = NOON + timedelta(minutes=5)
    run_cycle(aid, st)  # equity rose: the reference must not rise with it
    assert rows(AccountDayEquity)[0].start_equity == 1000.0
    # (all state is in the database: nothing in memory is needed, so a restart changes nothing)
    client.free["USDT"] = 960.0
    client._ohlcv = BULLISH_OHLCV
    clock.instant = NOON + timedelta(minutes=10)
    run_cycle(aid, st)
    assert all_trades(aid) == [] and len(rows(AccountLossPause)) == 1  # measured from 1000, not from the 1100 peak (that would be a 12.7% loss, also a hit) or a fresh start


def test_an_unreadable_balance_blocks_the_entry(db, monkeypatch):
    st = live_settings()
    client, clock, aid = rig(monkeypatch, st)
    run_cycle(aid, st)

    async def broken():
        raise RuntimeError("balance endpoint down")

    client.fetch_balance = broken
    client._ohlcv = BULLISH_OHLCV
    n = FakeNotifier()
    clock.instant = NOON + timedelta(minutes=5)
    run_cycle(aid, st, n, swallow=True)
    assert all_trades(aid) == [] and live_orders(client) == [] and "account_equity_unreadable" in reasons()
    assert rows(AccountLossPause) == []  # an unreadable balance is a refusal, not a recorded loss


def test_a_malformed_balance_blocks_the_entry(db, monkeypatch):
    st = live_settings()
    client, clock, aid = rig(monkeypatch, st)
    run_cycle(aid, st)
    client.free["USDT"] = float("nan")
    client._ohlcv = BULLISH_OHLCV
    clock.instant = NOON + timedelta(minutes=5)
    run_cycle(aid, st, swallow=True)
    assert all_trades(aid) == [] and "account_equity_unreadable" in reasons()


def test_an_invalid_time_zone_blocks_new_entries(db, monkeypatch):
    st = live_settings(account_day_timezone="Nowhere/Land")
    client, _, aid = rig(monkeypatch, st)
    client._ohlcv = BULLISH_OHLCV
    run_cycle(aid, st)
    assert all_trades(aid) == [] and "account_day_timezone_invalid" in reasons() and live_orders(client) == []


def test_a_paper_account_is_not_affected(db, monkeypatch):
    from mba_bot.models import TradingAccount

    st = live_settings(testnet_spot_api_key="paper-key-1234567890", testnet_spot_api_secret="paper-secret-1234567890")
    client, _, aid = rig(monkeypatch, st, balance=1.0)
    with session_scope() as s:
        s.get(TradingAccount, aid).bot_started_at = None  # PAPER
    client._ohlcv = BULLISH_OHLCV
    run_cycle(aid, st)
    assert all_trades(aid) and all_trades(aid)[0].is_paper and rows(AccountDayEquity) == [] and rows(AccountLossPause) == []


def test_an_open_trade_keeps_its_exit_during_the_loss_and_only_new_entries_stop(db, monkeypatch):
    st = live_settings()
    client, clock, aid = rig(monkeypatch, st)
    client._ohlcv = BULLISH_OHLCV
    run_cycle(aid, st)
    trade = all_trades(aid)[0]
    assert trade.status == "open"
    client.free["USDT"] = 100.0  # the account is far below its day start while the trade is open
    client._ohlcv, client.price = BEARISH_OHLCV, BEAR_PRICE
    clock.instant = NOON + timedelta(minutes=5)
    run_cycle(aid, st)
    closed = all_trades(aid)[0]
    assert closed.status == "closed" and closed.close_reason == "crossover_exit"  # the exit ran in spite of the loss
    client._ohlcv, client.price = BULLISH_OHLCV, BULLISH_OHLCV[-1][4]
    clock.instant = NOON + timedelta(minutes=10)
    run_cycle(aid, st)
    assert len(all_trades(aid)) == 1 and len(rows(AccountLossPause)) == 1  # a NEW entry is the thing that stops


def test_spot_equity_counts_an_open_position_at_mark():
    class C:
        async def fetch_balance(self):
            return {"USDT": {"free": 500.0, "used": 100.0}}

    run = lambda *a: asyncio.run(exchange.fetch_account_equity(C(), "BTC/USDT", "spot", *a))
    assert run() == 600.0 and run([(0.01, 50_000.0)]) == 1100.0
    assert run([(0.01, float("nan"))]) is None and run([(0.01, None)]) is None and run([(-1.0, 10.0)]) is None


@pytest.mark.parametrize("info,expected", [({"totalMarginBalance": "1234.5"}, 1234.5), ({}, None), ({"totalMarginBalance": "nan"}, None), ({"totalMarginBalance": True}, None),
                                           ({"totalMarginBalance": "-3"}, None), (None, None)])
def test_futures_equity_comes_only_from_total_margin_balance(info, expected):
    class C:
        async def fetch_balance(self):
            return {"USDT": {"free": 1.0, "used": 1.0}, "info": info}

    assert asyncio.run(exchange.fetch_account_equity(C(), "BTC/USDT", "futures")) == expected


def test_futures_pause_uses_the_margin_balance_and_a_missing_field_blocks(db, monkeypatch):
    st = live_settings()
    client, clock, aid = rig(monkeypatch, st, market="futures")
    client.total_margin_balance = 1000.0
    run_cycle(aid, st)
    assert rows(AccountDayEquity)[0].start_equity == 1000.0
    client.total_margin_balance = 950.0  # unrealised loss counts
    client._ohlcv = BULLISH_OHLCV
    clock.instant = NOON + timedelta(minutes=5)
    run_cycle(aid, st)
    assert all_trades(aid) == [] and len(rows(AccountLossPause)) == 1
    client2, clock2, aid2 = rig(monkeypatch, st, market="futures", email="second@example.com")  # no totalMarginBalance reported
    client2._ohlcv = BULLISH_OHLCV
    run_cycle(aid2, st, swallow=True)
    assert [t for t in all_trades(aid2)] == [] and "account_equity_unreadable" in reasons()


# ------------------------------------------------------------------ the Futures ceiling
def fut(monkeypatch, leverage, *, margin="isolated", ceiling=5.0, **over):
    st = make_settings(max_notional_multiplier=100.0, trade_telemetry=True, futures_max_leverage=ceiling, **over)
    client = futures_client(leverage_by_symbol={"BTC/USDT": leverage}, margin_mode=margin)
    install(monkeypatch, client)
    return st, client, new_account(st, market_type="futures")


@pytest.mark.parametrize("leverage,margin,opens", [(5.0, "isolated", True), (3.0, "isolated", True), (5.01, "isolated", False), (10.0, "isolated", False),
                                                   (2.0, "cross", False), (2.0, None, False)])
def test_the_ceiling_refuses_a_higher_leverage_or_a_margin_mode_that_is_not_isolated(db, monkeypatch, leverage, margin, opens):
    # The pre-entry protection check is switched off here only to isolate the ceiling (this fixture's stop is 52% from entry, which that check refuses on its own;
    # see test_the_ceiling_does_not_replace_the_pre_entry_check).
    st, client, aid = fut(monkeypatch, leverage, margin=margin, protection_precheck_mode="off")
    n = run_cycle(aid, st)
    assert bool(all_trades(aid)) is opens
    if not opens:
        assert live_orders(client) == []
        assert not any(c[0] in ("set_leverage", "set_margin_mode", "setLeverage", "setMarginMode") for c in client.calls)  # the bot never writes either
        assert any("never changes it for you" in t for t in n.texts) and len([t for t in n.texts if "never changes it for you" in t]) == 1  # one customer-safe alert
        assert set(reasons()) & {"futures_leverage_above_ceiling", "futures_margin_not_isolated"}


def test_the_ceiling_does_not_replace_the_pre_entry_check(db, monkeypatch):
    """Leverage 3 is under the ceiling of 5, so the ceiling lets it through; the existing pre-entry check (enforce in the test settings) then refuses the far stop."""
    st, client, aid = fut(monkeypatch, 3.0)
    assert st.protection_precheck_mode == "enforce"
    run_cycle(aid, st)
    assert all_trades(aid) == [] and "stop_beyond_safe_liquidation_distance" in reasons() and live_orders(client) == []


def test_the_ceiling_alert_is_not_repeated_every_round(db, monkeypatch):
    st, client, aid = fut(monkeypatch, 10.0, protection_precheck_mode="off")
    n = FakeNotifier()
    for _ in range(4):
        run_cycle(aid, st, n)
    assert all_trades(aid) == [] and len([t for t in n.texts if "never changes it for you" in t]) == 1


def test_ceiling_off_by_default_changes_nothing(db, monkeypatch):
    st, client, aid = fut(monkeypatch, 20.0, margin="cross", ceiling=0.0, protection_precheck_mode="off")
    assert make_settings().futures_max_leverage == 0.0
    run_cycle(aid, st)
    assert len(all_trades(aid)) == 1 or "futures_leverage_above_ceiling" not in reasons()  # whatever else decides, the ceiling is not what refuses
    assert not (set(reasons()) & {"futures_leverage_above_ceiling", "futures_margin_not_isolated"})


def test_spot_is_never_subject_to_the_futures_ceiling(db, monkeypatch):
    st = make_settings(max_notional_multiplier=100.0, futures_max_leverage=1.0)
    client = spot_client(ohlcv=BULLISH_OHLCV)
    install(monkeypatch, client)
    aid = new_account(st)
    run_cycle(aid, st)
    assert len(all_trades(aid)) == 1


def test_settings_defaults_and_clamps(monkeypatch):
    from mba_bot.config import load_settings

    for k in ("ACCOUNT_DAILY_LOSS_PCT", "ACCOUNT_DAY_TIMEZONE", "FUTURES_MAX_LEVERAGE"):
        monkeypatch.delenv(k, raising=False)
    s = load_settings()
    assert (s.account_daily_loss_pct, s.account_day_timezone, s.futures_max_leverage) == (0.0, "Africa/Lagos", 0.0)
    fields = {f.name: f.default for f in dataclasses.fields(type(s))}
    assert fields["account_daily_loss_pct"] == 0.0 and fields["futures_max_leverage"] == 0.0 and fields["account_day_timezone"] == "Africa/Lagos"
    monkeypatch.setenv("ACCOUNT_DAILY_LOSS_PCT", "4")
    monkeypatch.setenv("FUTURES_MAX_LEVERAGE", "5")
    s = load_settings()
    assert (s.account_daily_loss_pct, s.futures_max_leverage) == (4.0, 5.0)
    monkeypatch.setenv("ACCOUNT_DAILY_LOSS_PCT", "-3")
    monkeypatch.setenv("FUTURES_MAX_LEVERAGE", "9999")
    s = load_settings()
    assert (s.account_daily_loss_pct, s.futures_max_leverage) == (0.0, 125.0)


# ------------------------------------------------------------------ migration
def _cfg(monkeypatch, path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    return cfg


def test_the_migration_is_additive_idempotent_and_reversible(tmp_path, monkeypatch, keep_logging):
    path = tmp_path / "m.db"
    cfg = _cfg(monkeypatch, path)
    command.upgrade(cfg, "f3c8a1d6e2b9")
    names = lambda: set(inspect(create_engine(f"sqlite:///{path}")).get_table_names())
    assert not names() & {"account_day_equity", "account_loss_pauses"}
    command.upgrade(cfg, "head")
    assert names() >= {"account_day_equity", "account_loss_pauses"}
    command.upgrade(cfg, "head")  # idempotent
    command.downgrade(cfg, "f3c8a1d6e2b9")
    assert not names() & {"account_day_equity", "account_loss_pauses"} and "trading_accounts" in names()
    command.upgrade(cfg, "head")
    assert names() >= {"account_day_equity", "account_loss_pauses"}


def test_the_head_is_the_new_revision_on_top_of_the_opt_in_migration():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert script.get_heads() == ["a8d2f6c1e3b7"] and script.get_revision("a8d2f6c1e3b7").down_revision == "f3c8a1d6e2b9"


def test_the_pause_row_is_unique_per_account_kind_and_day(db):
    from sqlalchemy.exc import IntegrityError

    aid = new_account(make_settings())
    hit = al.DailyLossHit(4.0, 1000.0, 960.0)
    with session_scope() as s:
        al.write_pause(s, aid, hit, limit_pct=4.0, now=NOON, tz=LAGOS, tz_name="Africa/Lagos")
        again = al.write_pause(s, aid, hit, limit_pct=4.0, now=NOON, tz=LAGOS, tz_name="Africa/Lagos")
        assert again.day_key == "2027-03-10"  # idempotent
    with pytest.raises(IntegrityError):
        with session_scope() as s:
            s.add(AccountLossPause(account_id=aid, kind="daily_loss", day_key="2027-03-10", ends_at=NOON, start_equity=1, equity_at_hit=1, loss_pct=1, limit_pct=1, timezone_name="x"))


def test_the_owner_steps_use_the_safe_order_and_name_the_rollback():
    text = (ROOT / "docs" / "safety" / "account_daily_loss.md").read_text()
    steps = text[text.index("## Owner steps"):]
    order = ["restart_gate", "Stop the service", "Back up", "alembic upgrade head", "Deploy the code", "Start the service"]
    pos = [steps.index(k) for k in order]
    assert pos == sorted(pos) and "Rollback" in steps and "a8d2f6c1e3b7" in steps and "NOT verified" in text
    env = (ROOT / ".env.example").read_text()
    for k in ("ACCOUNT_DAILY_LOSS_PCT", "ACCOUNT_DAY_TIMEZONE", "FUTURES_MAX_LEVERAGE"):
        assert f"# {k}=" in env
