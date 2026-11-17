"""B7: live multi-position (LIVE_MULTI_POSITION, default off).

PROVES (fake exchange per pair, fixed candles): the pure rules (slot cap, open risk at the current stop, the additional-entry risk and daily-limit checks, shared capital); with the switch off a live account
holds one trade whatever its slot setting says; with it on an account with N slots opens at most ONE new trade per cycle, one per pair, up to N, each protected; the open risk across the trades is capped
(percent of the capital and the daily dollar limit) for the second and third trade and a refused entry sends no order; the kill switch stops additional entries; each trade is managed by its own strategy's
rules (an EMA exit closes one pair only); the priority is Donchian, then EMA, then RSI; the slot setting is validated by the API (switch, 1 to 3, not with Auto Select); the migration is additive.
DOES NOT PROVE: Binance's shared-margin behaviour (each fake keeps its own balance), any profit, or the screens in a browser."""
from __future__ import annotations

import dataclasses
import itertools
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from helpers_live import BEAR_PRICE, BEARISH_OHLCV, BULLISH_OHLCV, FakeNotifier, all_trades, install, make_settings, new_account, run_cycle
from multi_fake import routed_futures
from sqlalchemy import create_engine, inspect
from test_donchian import with_last
from test_donchian_integration import LONG_ROW, raw as don_raw
from test_kill_switch import _engage

from mba_bot import live_multi as lm, trader
from mba_bot.db import init_db, session_scope
from mba_bot.models import DecisionLog, Trade, TradingAccount

ROOT = Path(__file__).resolve().parent.parent
_n = itertools.count()


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")
    return tmp_path


def T(side="long", qty=1.0, entry=100.0, stop=95.0):
    return SimpleNamespace(side=side, quantity=qty, entry_price=entry, stop_loss_price=stop)


# ------------------------------------------------------------------ pure rules
def test_the_slot_cap_is_one_unless_the_switch_is_on_and_never_above_three():
    assert [lm.slot_cap(v, switch_on=False) for v in (None, 1, 2, 3, 9)] == [1, 1, 1, 1, 1]
    assert [lm.slot_cap(v, switch_on=True) for v in (None, 0, -2, 1, 2, 3, 4, 99)] == [1, 1, 1, 1, 2, 3, 3, 3]
    assert [lm.slot_cap(v, switch_on=True) for v in ("3", 2.0, True, [3])] == [1, 1, 1, 1]  # not a whole number: one slot


def test_trade_risk_is_the_quantity_times_the_distance_to_the_stop_as_it_rests_now():
    assert lm.trade_risk(T("long", 2.0, 100.0, 95.0)) == 10.0 and lm.trade_risk(T("short", 2.0, 100.0, 104.0)) == 8.0
    assert lm.trade_risk(T("long", 2.0, 100.0, 103.0)) == 0.0  # a trailed stop past break-even risks nothing
    assert lm.trade_risk(T("long", None, 100.0, 95.0)) == float("inf") and lm.trade_risk(T("long", 1.0, float("nan"), 95.0)) == float("inf")
    assert lm.total_open_risk([T("long", 1.0, 100.0, 95.0), T("short", 1.0, 50.0, 52.0)]) == 7.0


def test_only_a_second_or_third_entry_is_checked_against_the_open_risk_caps():
    open1 = [T("long", 1.0, 100.0, 90.0)]  # open risk 10
    assert lm.check_additional_entry([], new_risk=999.0, capital=100.0, cap_pct=1.0, daily_limit_usd=1.0) is None  # the first trade is judged by the existing rules
    assert lm.check_additional_entry(open1, new_risk=5.0, capital=1000.0, cap_pct=2.0, daily_limit_usd=None) is None  # 15 <= 20
    assert lm.check_additional_entry(open1, new_risk=10.01, capital=1000.0, cap_pct=2.0, daily_limit_usd=None) == lm.R_TOTAL_RISK  # 20.01 > 20
    assert lm.check_additional_entry(open1, new_risk=10.0, capital=1000.0, cap_pct=2.0, daily_limit_usd=None) is None  # exactly the cap
    assert lm.check_additional_entry(open1, new_risk=5.0, capital=1000.0, cap_pct=0.0, daily_limit_usd=None) is None  # no percent cap
    assert lm.check_additional_entry(open1, new_risk=5.0, capital=1000.0, cap_pct=50.0, daily_limit_usd=14.9) == lm.R_DAILY_LIMIT  # 15 > the daily limit
    assert lm.check_additional_entry(open1, new_risk=5.0, capital=1000.0, cap_pct=50.0, daily_limit_usd=15.0) is None
    for bad in (dict(new_risk=float("nan")), dict(new_risk=-1.0), dict(capital=None), dict(capital=0.0), dict(capital=float("inf"))):
        kw = dict(new_risk=1.0, capital=1000.0, cap_pct=50.0, daily_limit_usd=None)
        kw.update(bad)
        assert lm.check_additional_entry(open1, **kw) == lm.R_TOTAL_RISK, bad  # fail closed
    assert lm.check_additional_entry([T("long", None, 1.0, 1.0)], new_risk=1.0, capital=1000.0, cap_pct=50.0, daily_limit_usd=None) == lm.R_TOTAL_RISK  # an unreadable open trade blocks more


def test_the_capital_is_shared_between_the_open_trades():
    open2 = [T("long", 1.0, 100.0, 95.0), T("long", 3.0, 100.0, 95.0)]  # 400 of position value
    assert lm.committed_capital(open2, 4) == 100.0 and lm.committed_capital(open2, 1) == 400.0
    assert lm.free_capital(1000.0, open2, 4) == 900.0 and lm.free_capital(300.0, open2, 1) == 0.0 and lm.free_capital(None, open2, 1) == 0.0
    assert lm.free_capital(1000.0, [T("long", None, 1.0, 1.0)], 1) == 0.0  # unreadable: nothing is free


# ------------------------------------------------------------------ the cycle
PAIRS = {"BTC/USDT": (BULLISH_OHLCV, None), "ETH/USDT": (BULLISH_OHLCV, None), "BNB/USDT": (BULLISH_OHLCV, None)}


def multi_settings(**over):
    base = dict(live_multi_position=True, max_notional_multiplier=100.0, protection_precheck_mode="off", trade_telemetry=True, live_total_risk_cap_pct=0.0)
    base.update(over)
    return make_settings(**base)


def multi_account(settings, *, slots=3, pairs="ETH/USDT,BNB/USDT", capital=5000.0, risk=0.5):
    aid = new_account(settings, market_type="futures", email=f"mp{next(_n)}@example.com", allocated_capital=capital)
    with session_scope() as s:
        a = s.get(TradingAccount, aid)
        a.live_slots, a.pair_list, a.risk_pct = slots, pairs, risk
    return aid


def rig(monkeypatch, settings, **kw):
    client = routed_futures(kw.pop("pairs", PAIRS))
    for f in client._fakes.values():
        f.total_margin_balance = 1000.0
    install(monkeypatch, client)
    return client


def live_pairs_of(aid):
    return sorted(t.pair for t in all_trades(aid) if t.status not in ("closed", "void"))


def decisions():
    with session_scope() as s:
        return [d.reason_code for d in s.query(DecisionLog).all()]


def test_with_the_switch_off_a_live_account_holds_one_trade_whatever_its_slot_setting_says(db, monkeypatch):
    settings = multi_settings(live_multi_position=False)
    client = rig(monkeypatch, settings)
    aid = multi_account(settings, slots=3)
    for _ in range(3):
        run_cycle(aid, settings)
    assert live_pairs_of(aid) == ["BTC/USDT"]


def test_an_account_with_slots_opens_one_trade_per_cycle_one_per_pair_up_to_its_slots(db, monkeypatch):
    settings = multi_settings()
    client = rig(monkeypatch, settings)
    aid = multi_account(settings, slots=2)
    run_cycle(aid, settings)
    assert live_pairs_of(aid) == ["BTC/USDT"]  # one new entry per cycle
    run_cycle(aid, settings)
    assert live_pairs_of(aid) == ["BTC/USDT", "ETH/USDT"]
    run_cycle(aid, settings)
    assert live_pairs_of(aid) == ["BTC/USDT", "ETH/USDT"]  # the slots are full: BNB waits
    for t in all_trades(aid):
        assert t.status == "open" and t.protection_status == "protected" and t.is_paper is False and t.pair in ("BTC/USDT", "ETH/USDT")
    assert len({t.pair for t in all_trades(aid)}) == 2  # never two on one pair


def test_three_slots_fill_three_pairs_and_no_more(db, monkeypatch):
    settings = multi_settings()
    client = rig(monkeypatch, settings)
    aid = multi_account(settings, slots=3)
    for _ in range(5):
        run_cycle(aid, settings)
    assert live_pairs_of(aid) == ["BNB/USDT", "BTC/USDT", "ETH/USDT"]


def test_the_open_risk_across_trades_is_capped_by_percent_of_capital(db, monkeypatch):
    settings = multi_settings(live_total_risk_cap_pct=0.75)  # a second 0.5% trade would take the open risk to 1.0%
    client = rig(monkeypatch, settings)
    aid = multi_account(settings, slots=3, risk=0.5)
    run_cycle(aid, settings)
    orders_after_first = sum(len(f.orders) for f in client._fakes.values())
    run_cycle(aid, settings)
    assert live_pairs_of(aid) == ["BTC/USDT"] and "live_total_open_risk_cap" in decisions()
    assert sum(len(f.orders) for f in client._fakes.values()) == orders_after_first  # the refused entry sent no order


def test_the_open_risk_is_also_capped_by_the_daily_dollar_limit(db, monkeypatch):
    settings = multi_settings(account_daily_loss_limit=True)
    client = rig(monkeypatch, settings)
    aid = multi_account(settings, slots=3, risk=0.5)  # one trade risks about 25 USDT of 5000
    with session_scope() as s:
        s.get(TradingAccount, aid).daily_loss_limit_usd = 30.0  # room for one trade's risk, not two
    run_cycle(aid, settings)
    run_cycle(aid, settings)
    assert live_pairs_of(aid) == ["BTC/USDT"] and "live_open_risk_above_daily_limit" in decisions()


def test_the_kill_switch_stops_additional_entries_but_not_management(db, monkeypatch):
    settings = multi_settings()
    client = rig(monkeypatch, settings)
    aid = multi_account(settings, slots=3)
    run_cycle(aid, settings)
    _engage()
    run_cycle(aid, settings)
    assert live_pairs_of(aid) == ["BTC/USDT"]
    t = all_trades(aid)[0]
    assert t.status == "open" and t.protection_status == "protected"


def test_each_trade_is_managed_by_its_own_strategy_and_an_exit_closes_one_pair_only(db, monkeypatch):
    settings = multi_settings()
    client = rig(monkeypatch, settings)
    aid = multi_account(settings, slots=2)
    run_cycle(aid, settings)
    run_cycle(aid, settings)
    assert live_pairs_of(aid) == ["BTC/USDT", "ETH/USDT"]
    fb = client._fakes["BTC/USDT:USDT"]
    fb._ohlcv, fb.price = BEARISH_OHLCV, BEAR_PRICE  # EMA turns bearish on BTC only
    run_cycle(aid, settings)
    trades = all_trades(aid)
    btc_first = next(t for t in trades if t.pair == "BTC/USDT" and t.status == "closed")
    assert btc_first.close_reason == "crossover_exit"
    eth = next(t for t in trades if t.pair == "ETH/USDT")
    assert eth.status == "open" and eth.protection_status == "protected"  # the other pair is untouched (the freed slot may be taken again: on Futures a bearish EMA crossover is a short entry)


def test_donchian_has_priority_over_ema_across_pairs(db, monkeypatch):
    settings = multi_settings(donchian_enabled=True, live_donchian=True, strategy_guards_mode="enforce", new_strategy_guards_mode="enforce", fee_guard_margin=0.5, strategy_expectancy_window=0, max_total_risk_pct=5.0)
    don = don_raw(with_last(LONG_ROW))
    pairs = {"BTC/USDT": (BULLISH_OHLCV, None), "ETH/USDT": (don, 103.0)}  # EMA is bullish on BTC, a Donchian breakout is on ETH
    client = rig(monkeypatch, settings, pairs=pairs)
    aid = multi_account(settings, slots=2, pairs="ETH/USDT")
    with session_scope() as s:
        s.get(TradingAccount, aid).extra_strategies = "donchian_breakout_v1"
    run_cycle(aid, settings)
    first = all_trades(aid)
    assert len(first) == 1 and first[0].pair == "ETH/USDT" and first[0].strategy_id == "donchian_breakout_v1"  # Donchian before EMA
    run_cycle(aid, settings)
    assert {t.pair: t.strategy_id for t in all_trades(aid)}.keys() == {"ETH/USDT", "BTC/USDT"}


def test_a_second_trade_cannot_open_on_a_pair_that_already_has_one(db, monkeypatch):
    settings = multi_settings()
    client = rig(monkeypatch, settings)
    aid = multi_account(settings, slots=3, pairs="")
    for _ in range(3):
        run_cycle(aid, settings)
    assert live_pairs_of(aid) == ["BTC/USDT"]  # a single pair, however many slots


# ------------------------------------------------------------------ the API and the data
def test_the_slot_setting_is_validated_by_the_api():
    import os
    import tempfile

    from cryptography.fernet import Fernet

    os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/api.db")
    os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
    os.environ.setdefault("JWT_SECRET", "x")
    os.environ.setdefault("COOKIE_SECURE", "false")
    from fastapi.testclient import TestClient

    from mba_bot.api.app import app
    from mba_bot.api.deps import get_settings
    from mba_bot.config import load_settings
    from test_api_live_trading_safety import _signup_with_account

    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "slots@example.com")
        assert client.patch("/api/account", json={"live_slots": 2}).status_code == 409  # the switch is off
        me = client.get("/api/account").json()
        assert me["live_slots"] == 1 and me["live_multi_available"] is False
        app.dependency_overrides[get_settings] = lambda: dataclasses.replace(load_settings(), live_multi_position=True)
        try:
            for bad in (0, 4, -1):
                assert client.patch("/api/account", json={"live_slots": bad}).status_code == 422
            ok = client.patch("/api/account", json={"live_slots": 3})
            assert ok.status_code == 200 and ok.json()["live_slots"] == 3 and ok.json()["live_multi_available"] is True
            conflict = client.patch("/api/account", json={"pair_selection_mode": "auto"})
            assert conflict.status_code == 422 and "Auto Select" in conflict.json()["detail"]
            assert client.patch("/api/account", json={"live_slots": 1}).status_code == 200
            assert client.patch("/api/account", json={"pair_selection_mode": "auto"}).status_code == 200
            assert client.patch("/api/account", json={"live_slots": 2}).status_code == 422  # and not the other way round either
        finally:
            app.dependency_overrides.pop(get_settings, None)


def test_the_migration_is_additive_idempotent_and_reversible(tmp_path, monkeypatch, keep_logging):
    path = tmp_path / "m.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(cfg, "b7f3d5a9c2e4")
    cols = lambda: {c["name"] for c in inspect(create_engine(f"sqlite:///{path}")).get_columns("trading_accounts")}
    before = cols()
    command.upgrade(cfg, "c8a4e6b1d3f5")
    assert cols() == before | {"live_slots"}
    command.upgrade(cfg, "c8a4e6b1d3f5")
    command.downgrade(cfg, "b7f3d5a9c2e4")
    assert cols() == before
    command.upgrade(cfg, "head")
    assert "live_slots" in cols()


# ------------------------------------------------------------------ the database rule, per slot
def trade_row(aid, status, pair, *, paper=False, slot="default"):
    kw = dict(account_id=aid, side="long", is_paper=paper, entry_price=1.0, quantity=1.0, stop_loss_price=0.5, status=status, pair=pair)
    if slot != "default":
        kw["live_slot"] = slot
    return Trade(**kw)


def test_the_database_still_allows_one_live_trade_per_slot_and_a_live_row_without_a_slot_takes_slot_zero(db):
    from sqlalchemy.exc import IntegrityError

    aid = new_account(make_settings())
    with session_scope() as s:
        s.add(trade_row(aid, "open", "BTC/USDT"))  # no slot given: slot 0, as before
    with pytest.raises(IntegrityError):
        with session_scope() as s:
            s.add(trade_row(aid, "pending", "ETH/USDT"))  # a second live trade in slot 0, whatever the pair
    with session_scope() as s:
        s.add(trade_row(aid, "open", "ETH/USDT", slot=1))  # a second slot is a different slot
        s.add(trade_row(aid, "open", "BNB/USDT", slot=2))
    with pytest.raises(IntegrityError):
        with session_scope() as s:
            s.add(trade_row(aid, "open", "XRP/USDT", slot=1))  # slot 1 is taken
    with session_scope() as s:
        assert s.query(Trade).filter(Trade.account_id == aid, Trade.is_paper.is_(False)).count() == 3
        paper = trade_row(aid, "open", "SOL/USDT", paper=True)
        s.add(paper)
    with session_scope() as s:
        assert s.query(Trade).filter(Trade.is_paper.is_(True)).one().live_slot is None  # paper rows carry no slot


def test_a_terminal_trade_frees_its_slot(db):
    aid = new_account(make_settings())
    with session_scope() as s:
        s.add(trade_row(aid, "closed", "BTC/USDT"))
        s.add(trade_row(aid, "open", "BTC/USDT"))  # the closed one does not count


def test_the_slot_migration_backfills_live_rows_replaces_the_index_and_refuses_when_unsafe(tmp_path, monkeypatch, keep_logging):
    from sqlalchemy import text

    path = tmp_path / "s.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(cfg, "c8a4e6b1d3f5")
    eng = create_engine(f"sqlite:///{path}")
    names = lambda: {i["name"] for i in inspect(create_engine(f"sqlite:///{path}")).get_indexes("trades")}
    assert "uq_trades_one_live_nonterminal_per_account" in names() and "uq_trades_live_slot" not in names()
    now = datetime.now(timezone.utc).isoformat()
    with eng.begin() as c:
        c.execute(text("insert into customers (id,email,password_hash,created_at,is_active,has_paid) values ('c1','a@b.c','x',:n,1,1)"), dict(n=now))
        c.execute(text("insert into trading_accounts (id,customer_id,pair,market_type,notification_channel,risk_pct,trial_ends_at,paper_balance,is_active,created_at,updated_at) values ('a1','c1','BTC/USDT','futures','telegram',1,:n,1,1,:n,:n)"), dict(n=now))
        c.execute(text("insert into trades (id,account_id,side,is_paper,entry_price,quantity,stop_loss_price,status,opened_at,pair) values ('t1','a1','long',0,100,1,90,'open',:n,'BTC/USDT')"), dict(n=now))
    command.upgrade(cfg, "d9b5f7c2e4a6")
    assert "uq_trades_live_slot" in names() and "uq_trades_one_live_nonterminal_per_account" not in names()
    with eng.connect() as c:
        assert c.execute(text("select live_slot from trades where id='t1'")).scalar() == 0  # the existing live trade holds slot 0
    command.upgrade(cfg, "d9b5f7c2e4a6")  # idempotent
    with eng.begin() as c:
        c.execute(text("insert into trades (id,account_id,side,is_paper,entry_price,quantity,stop_loss_price,status,opened_at,pair,live_slot) values ('t2','a1','long',0,100,1,90,'open',:n,'ETH/USDT',1)"), dict(n=now))
    with pytest.raises(Exception, match="more than one non-terminal LIVE trade"):
        command.downgrade(cfg, "c8a4e6b1d3f5")  # two open live trades: the old one-per-account index cannot be put back
    with eng.begin() as c:
        c.execute(text("update trades set status='closed' where id='t2'"))
    command.downgrade(cfg, "c8a4e6b1d3f5")
    assert "uq_trades_one_live_nonterminal_per_account" in names() and "uq_trades_live_slot" not in names()


def test_bot_settings_offers_the_slot_control_only_when_the_service_does():
    front = ROOT / "frontend" / "src" / "pages" / "BotSettings.jsx"
    text = front.read_text()
    assert "account.live_multi_available &&" in text and "Open trades at once" in text and "live_slots: slotsDirty ? Number(liveSlots) : undefined" in text
    assert "Lowering it never closes a trade" in text


def test_the_lifecycle_itself_refuses_a_trade_beyond_the_slot_cap(db, monkeypatch):
    import asyncio

    from helpers_live import live_context
    from mba_bot import lifecycle

    settings = multi_settings()
    client = rig(monkeypatch, settings)
    aid = multi_account(settings, slots=3)
    run_cycle(aid, settings)
    assert live_pairs_of(aid) == ["BTC/USDT"]

    async def try_open(cap, pair):
        with session_scope(autoflush=False) as session:
            from mba_bot.venue import build_venue

            account = session.get(TradingAccount, aid)
            venue = build_venue(client, market_type="futures", pair=pair, entries_allowed=True)
            await venue.prepare()
            ctx = lifecycle.LiveContext(session=session, account=account, venue=venue, settings=settings, notifier=FakeNotifier(), now=datetime.now(timezone.utc), slot_cap=cap)
            return await lifecycle.open_live_trade(ctx, side="long", quantity=0.1, signal_price=90.0, stop_price=80.0, risk_text="", pair=pair)

    assert asyncio.run(try_open(1, "ETH/USDT")) is None  # one slot, one trade already open: refused before any order
    assert live_pairs_of(aid) == ["BTC/USDT"]
    opened = asyncio.run(try_open(2, "ETH/USDT"))  # two slots: the same call is allowed and takes slot 1
    assert opened is not None and opened.live_slot == 1


def test_a_second_trade_that_would_exceed_the_shared_capital_is_refused_without_an_order(db, monkeypatch):
    settings = multi_settings()
    client = rig(monkeypatch, settings)
    aid = multi_account(settings, slots=3, capital=1000.0)
    run_cycle(aid, settings)
    first = all_trades(aid)[0]
    used = first.quantity * first.entry_price / 3.0  # the open trade uses this much margin at the default 3x cap
    with session_scope() as s:
        s.get(TradingAccount, aid).allocated_capital = used * 0.9  # the customer lowered the capital below what is already in use
    orders = sum(len(f.orders) for f in client._fakes.values())
    run_cycle(aid, settings)
    assert live_pairs_of(aid) == ["BTC/USDT"] and "live_free_capital_short" in decisions()
    assert sum(len(f.orders) for f in client._fakes.values()) == orders


def test_a_pair_that_already_has_a_trade_is_not_even_scanned_again(db, monkeypatch):
    settings = multi_settings()
    client = rig(monkeypatch, settings)
    aid = multi_account(settings, slots=3)
    run_cycle(aid, settings)
    seen = []
    real = trader._live_strategy_signal

    async def spy(client_, account, pair, strategy, *a, **k):
        seen.append(pair)
        return await real(client_, account, pair, strategy, *a, **k)

    monkeypatch.setattr(trader, "_live_strategy_signal", spy)
    run_cycle(aid, settings)
    assert seen and "BTC/USDT" not in seen  # BTC holds a trade: only the free pairs are looked at
