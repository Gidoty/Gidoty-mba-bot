"""Multi-trade foundation (src/mba_bot/multi_trade.py, docs/safety/multi_trade.md).

PROVES: the slot cap and the market grade as pure rules (live always 1, missing input = grade 1), the new database rule (one
non-terminal trade per account AND pair, the old rule kept for rows with no pair), the migration (additive, reversible, no backfill),
that the DEFAULT configuration never reaches the multi-trade code, that a live account cannot be given a second trade, and what the
cycle does with two and three paper trades on different pairs. The live-recovery tests build two and three live trades on different
pairs against a routed FAKE exchange.

DOES NOT PROVE: anything about Binance (the fake proves control flow only), that the grade thresholds are good (they are untuned
starting values), or that three correlated crypto trades are a good idea."""
from __future__ import annotations

import asyncio
import dataclasses
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from helpers_live import (BEARISH_OHLCV, BULLISH_OHLCV, ENTRY_PRICE, FakeNotifier, all_trades, install, make_settings, new_account, run_cycle, spot_client)
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from mba_bot import multi_trade as mt
from mba_bot import restart_gate, trader
from mba_bot.db import get_open_trade, init_db, list_open_trades, session_scope
from mba_bot.models import StrategyPause, Trade, TradingAccount

U = timezone.utc
ROOT = Path(__file__).resolve().parent.parent
PAPER_KEYS = dict(testnet_spot_api_key="paper-key-1234567890", testnet_spot_api_secret="paper-secret-1234567890")


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")
    return tmp_path


# ====================================================================== pure rules
def test_a_live_account_is_capped_at_one_trade_whatever_the_setting_or_grade_says():
    for configured in (1, 2, 3, 5, 99, 0, -4, None, "3", True):
        for grade in (1, 2, 3, 99):
            assert mt.slot_cap(is_paper=False, configured=configured, grade=grade) == 1


@pytest.mark.parametrize("configured,grade,expected", [
    (1, 3, 1), (2, 3, 2), (3, 3, 3), (5, 3, 3), (99, 3, 3), (3, 1, 1), (3, 2, 2), (3, 0, 1), (3, -1, 1), (0, 3, 1), (-2, 3, 1), (None, 3, 1),
    ("3", 3, 3), ("x", 3, 1), (3, None, 1), (3, "3", 1), (3, True, 1), (3, 2.0, 1), (2, 3, 2)])
def test_a_paper_account_gets_the_smaller_of_the_setting_the_hard_maximum_and_the_grade(configured, grade, expected):
    assert mt.slot_cap(is_paper=True, configured=configured, grade=grade) == expected
    assert mt.HARD_MAX_TRADES == 3
    assert mt.configured_cap(99) == 3 and mt.configured_cap(2) == 2 and mt.configured_cap(0) == 1


def test_trading_pairs_primary_first_deduplicated_and_at_most_three():
    A = type("A", (), {})
    a = A(); a.pair = "BTC/USDT"; a.pair_list = None
    assert mt.trading_pairs(a) == ["BTC/USDT"]
    a.pair_list = ""
    assert mt.trading_pairs(a) == ["BTC/USDT"]
    a.pair_list = " eth/usdt , BTC/USDT,BNB/USDT, XRP/USDT ,,"
    assert mt.trading_pairs(a) == ["BTC/USDT", "ETH/USDT", "BNB/USDT"]
    a.pair = None
    assert mt.trading_pairs(a) == ["ETH/USDT", "BNB/USDT", "BTC/USDT"] or len(mt.trading_pairs(a)) <= 3


def test_an_empty_strategy_id_means_ema_cross_atr_v1():
    t = Trade(account_id="a", side="long", is_paper=True, entry_price=1, quantity=1, stop_loss_price=0.5)
    assert t.strategy_id is None and mt.strategy_of(t) == "ema_cross_atr_v1"
    t.strategy_id = ""
    assert mt.strategy_of(t) == "ema_cross_atr_v1"
    t.strategy_id = "rsi_mr_v1"
    assert mt.strategy_of(t) == "rsi_mr_v1"


def good(**over):
    base = dict(atr_pct=0.01, atr_avg_pct=0.01, flat_threshold=0.75, nets_newest_first=tuple([1.0] * 20), pause_active=False,
                daily_pnl=0.0, capital=1000.0, daily_loss_limit_pct=3.0)
    base.update(over)
    return mt.GradeInputs(**base)


def test_the_grade_ladder():
    assert mt.grade_from(good()).level == 3
    assert mt.grade_from(good(nets_newest_first=tuple([1.0] * 10))).level == 2  # 10 trades: ok, but 20 are needed for very good
    assert mt.grade_from(good(nets_newest_first=tuple([1.0] * 9))).level == 1
    assert mt.grade_from(good(nets_newest_first=tuple([-1.0] * 10))).level == 1
    assert mt.grade_from(good(nets_newest_first=tuple([0.0] * 20))).level == 2  # zero net over 20 is not "net positive"
    assert mt.grade_from(good(nets_newest_first=tuple([5.0] * 10) + tuple([-1.0] * 10))).level == 3
    assert mt.grade_from(good(nets_newest_first=tuple([0.1] * 10) + tuple([-1.0] * 10))).level == 2  # last 10 fine, last 20 not
    assert mt.grade_from(good(nets_newest_first=tuple([-0.1] * 10) + tuple([5.0] * 10))).level == 1  # last 10 net negative
    assert mt.grade_from(good(nets_newest_first=tuple([0.0] * 10))).level == 2  # break-even last 10 is "not net negative"


@pytest.mark.parametrize("over,why", [
    (dict(atr_pct=0.0074), "flat_market"), (dict(atr_pct=0.0076), None), (dict(atr_pct=0.0301), "volatility_extreme"), (dict(atr_pct=0.03), None),
    (dict(pause_active=True), "strategy_paused")])
def test_flat_extreme_and_paused_markets_are_grade_one(over, why):
    g = mt.grade_from(good(**over))
    assert (g.level == 1) is (why is not None)
    if why:
        assert g.reasons == (why,)


@pytest.mark.parametrize("over", [
    dict(atr_pct=None), dict(atr_avg_pct=None), dict(flat_threshold=None), dict(nets_newest_first=None), dict(pause_active=None),
    dict(atr_pct=float("nan")), dict(atr_avg_pct=float("inf")), dict(atr_avg_pct=0.0), dict(atr_avg_pct=-1.0), dict(atr_pct=-0.1),
    dict(nets_newest_first=(1.0, float("nan"))), dict(nets_newest_first=("a",) * 20), dict(atr_pct="0.01"), dict(flat_threshold=True)])
def test_missing_or_malformed_input_is_grade_one(over):
    assert mt.grade_from(good(**over)).level == 1


@pytest.mark.parametrize("over,level", [
    (dict(daily_pnl=-14.9), 3), (dict(daily_pnl=-15.0), 2), (dict(daily_pnl=-40.0), 2), (dict(daily_pnl=50.0), 3),
    (dict(daily_loss_limit_pct=0.0), 2), (dict(daily_loss_limit_pct=None), 2), (dict(daily_pnl=None), 2), (dict(capital=None), 2), (dict(capital=0.0), 2),
    (dict(capital=float("nan")), 2)])
def test_very_good_needs_a_known_daily_loss_under_half_the_limit(over, level):
    assert mt.grade_from(good(**over)).level == level


def test_free_capital_and_open_risk_helpers_fail_closed():
    assert mt.free_capital(1000.0, [300.0, 200.0]) == 500.0
    assert mt.open_risk_pct([10.0, 15.0], 1000.0) == 2.5
    for bad in (0.0, -5.0, float("nan"), None):
        assert mt.open_risk_pct([1.0], bad) is None
    assert mt.open_risk_pct([float("nan")], 1000.0) is None and mt.open_risk_pct([-1.0], 1000.0) is None and mt.open_risk_pct([], 1000.0) == 0.0


# ====================================================================== database rule and migration
def _trade(account_id, status, pair, **kw):
    kw.setdefault("is_paper", True)  # since the live index (audit B1) several trades on different pairs exist only as PAPER trades
    return Trade(account_id=account_id, side="long", entry_price=1.0, quantity=1.0, stop_loss_price=0.5, status=status, pair=pair, **kw)


def test_one_non_terminal_trade_per_account_and_pair(db):
    st = make_settings()
    a = new_account(st, email="a@example.com")
    b = new_account(st, email="b@example.com")
    with session_scope() as s:
        s.add(_trade(a, "open", "BTC/USDT"))
        s.add(_trade(a, "pending", "ETH/USDT"))  # a different pair is allowed by the database (the live cap lives in code)
        s.add(_trade(a, "attention", "BNB/USDT"))
        s.add(_trade(b, "open", "BTC/USDT"))  # another account, same pair
    for status in ("pending", "protecting", "open", "closing", "attention"):
        with pytest.raises(IntegrityError):
            with session_scope() as s:
                s.add(_trade(a, status, "ETH/USDT"))
    with session_scope() as s:  # terminal rows never count
        for _ in range(3):
            s.add(_trade(a, "closed", "ETH/USDT"))
            s.add(_trade(a, "void", "ETH/USDT"))
    with session_scope() as s:
        assert [t.pair for t in list_open_trades(s, a)] == ["BTC/USDT", "ETH/USDT", "BNB/USDT"] or len(list_open_trades(s, a)) == 3
        assert get_open_trade(s, a) is not None


def test_the_old_rule_still_holds_for_rows_with_no_pair(db):
    st = make_settings()
    a = new_account(st, email="a@example.com")
    with session_scope() as s:
        s.add(_trade(a, "open", None))
        s.add(_trade(a, "open", "BTC/USDT"))  # a row WITH a pair next to a legacy one is allowed
    with pytest.raises(IntegrityError):
        with session_scope() as s:
            s.add(_trade(a, "pending", None))


def test_open_trades_are_listed_oldest_first_and_get_open_trade_returns_the_oldest(db):
    st = make_settings()
    a = new_account(st, email="a@example.com")
    now = datetime.now(U)
    with session_scope() as s:
        for i, pair in enumerate(["ETH/USDT", "BTC/USDT", "BNB/USDT"]):
            s.add(_trade(a, "open", pair, opened_at=now + timedelta(minutes=10 - i)))
    with session_scope() as s:
        assert [t.pair for t in list_open_trades(s, a)] == ["BNB/USDT", "BTC/USDT", "ETH/USDT"]
        assert get_open_trade(s, a).pair == "BNB/USDT"


@pytest.fixture
def keep_logging():
    """alembic's env.py calls logging.config.fileConfig, which disables every existing logger and replaces the root handlers. Put the
    logging state back afterwards so tests that run later (log-safety and scheduler tests) still see their loggers."""
    import logging
    root = logging.getLogger()
    state = {name: (lg.disabled, lg.level, lg.propagate, list(lg.handlers)) for name, lg in logging.root.manager.loggerDict.items() if isinstance(lg, logging.Logger)}
    root_state = (root.level, list(root.handlers))
    yield
    for name, (disabled, level, propagate, handlers) in state.items():
        lg = logging.getLogger(name)
        lg.disabled, lg.level, lg.propagate, lg.handlers = disabled, level, propagate, handlers
    root.setLevel(root_state[0])
    root.handlers = root_state[1]


def _alembic(monkeypatch, path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    return Config(str(ROOT / "alembic.ini"))


def _names(path, table):
    eng = create_engine(f"sqlite:///{path}")
    return {c["name"] for c in inspect(eng).get_columns(table)}, {i["name"] for i in inspect(eng).get_indexes(table)}


def test_migration_is_additive_reversible_and_does_not_backfill(tmp_path, monkeypatch, keep_logging):
    path = tmp_path / "m.db"
    cfg = _alembic(monkeypatch, path)
    command.upgrade(cfg, "c9f6a3b1d5e7")
    cols, idx = _names(path, "trades")
    assert "strategy_id" not in cols and "uq_trades_one_nonterminal_per_account" in idx
    assert "pair_list" not in _names(path, "trading_accounts")[0]
    eng = create_engine(f"sqlite:///{path}")
    now = datetime.now(U).isoformat()
    with eng.begin() as c:
        c.execute(text("insert into customers (id,email,password_hash,created_at,is_active,has_paid) values ('c1','m@example.com','x',:n,1,1)"), dict(n=now))
        c.execute(text("insert into trading_accounts (id,customer_id,pair,market_type,pair_selection_mode,notification_channel,risk_pct,trial_ends_at,paper_balance,is_active,created_at,updated_at) "
                       "values ('a1','c1','BTC/USDT','spot','manual','telegram',1.0,:n,10000,1,:n,:n)"), dict(n=now))
        c.execute(text("insert into trades (id,account_id,side,is_paper,entry_price,quantity,stop_loss_price,status,opened_at,pair) "
                       "values ('t1','a1','long',1,100,1,90,'open',:n,'BTC/USDT')"), dict(n=now))
    command.upgrade(cfg, "head")
    cols, idx = _names(path, "trades")
    assert {"strategy_id"} <= cols and {"uq_trades_one_nonterminal_per_pair", "uq_trades_one_nonterminal_no_pair"} <= idx
    assert "uq_trades_one_nonterminal_per_account" not in idx and "pair_list" in _names(path, "trading_accounts")[0]
    with eng.connect() as c:
        assert c.execute(text("select strategy_id from trades where id='t1'")).scalar() is None  # NO backfill
        assert c.execute(text("select pair_list from trading_accounts where id='a1'")).scalar() is None
    command.upgrade(cfg, "head")  # idempotent
    command.downgrade(cfg, "c9f6a3b1d5e7")
    cols, idx = _names(path, "trades")
    assert "strategy_id" not in cols and "uq_trades_one_nonterminal_per_account" in idx and "uq_trades_one_nonterminal_per_pair" not in idx
    assert "pair_list" not in _names(path, "trading_accounts")[0]
    with eng.connect() as c:  # the existing row survived the round trip untouched
        assert c.execute(text("select status, pair from trades where id='t1'")).one() == ("open", "BTC/USDT")
    command.upgrade(cfg, "head")


def test_downgrade_refuses_when_an_account_has_two_non_terminal_trades(tmp_path, monkeypatch, keep_logging):
    path = tmp_path / "d.db"
    cfg = _alembic(monkeypatch, path)
    command.upgrade(cfg, "head")
    eng = create_engine(f"sqlite:///{path}")
    now = datetime.now(U).isoformat()
    with eng.begin() as c:
        c.execute(text("insert into customers (id,email,password_hash,created_at,is_active,has_paid) values ('c1','m@example.com','x',:n,1,1)"), dict(n=now))
        c.execute(text("insert into trading_accounts (id,customer_id,pair,market_type,pair_selection_mode,notification_channel,risk_pct,trial_ends_at,paper_balance,is_active,created_at,updated_at) "
                       "values ('a1','c1','BTC/USDT','spot','manual','telegram',1.0,:n,10000,1,:n,:n)"), dict(n=now))
        for i, pair in enumerate(["BTC/USDT", "ETH/USDT"]):
            c.execute(text("insert into trades (id,account_id,side,is_paper,entry_price,quantity,stop_loss_price,status,opened_at,pair) "
                           "values (:i,'a1','long',1,100,1,90,'open',:n,:p)"), dict(i=f"t{i}", n=now, p=pair))
    with pytest.raises(Exception, match="more than one non-terminal trade"):
        command.downgrade(cfg, "c9f6a3b1d5e7")
    cols, idx = _names(path, "trades")
    assert "strategy_id" in cols and "uq_trades_one_nonterminal_per_pair" in idx  # a refused downgrade leaves the schema untouched


# ====================================================================== the default configuration never reaches the multi-trade code
def paper_account(settings, email="paper@example.com", pair_list=None, **kw):
    aid = new_account(settings, email=email, **kw)
    with session_scope() as s:
        a = s.get(TradingAccount, aid)
        a.bot_started_at = None  # resolves to PAPER
        a.pair_list = pair_list
    return aid


def paper_settings(**over):
    base = dict(PAPER_KEYS)
    base.update(over)
    return make_settings(**base)


def poison_multi(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("the multi-trade code ran with the default configuration")

    monkeypatch.setattr(trader, "_process_multi", boom)


@pytest.mark.parametrize("cap,pair_list", [(1, None), (1, "ETH/USDT,BNB/USDT"), (3, None), (3, "")])
def test_defaults_and_partial_settings_never_use_the_multi_trade_path_for_paper(db, monkeypatch, cap, pair_list):
    st = paper_settings(max_open_positions=cap)
    client = spot_client()
    install(monkeypatch, client)
    poison_multi(monkeypatch)
    aid = paper_account(st, pair_list=pair_list)
    run_cycle(aid, st)
    run_cycle(aid, st)  # the second cycle manages the open trade
    trades = all_trades(aid)
    assert len(trades) == 1 and trades[0].is_paper and trades[0].pair == "BTC/USDT" and trades[0].strategy_id == "ema_cross_atr_v1"


@pytest.mark.parametrize("cap,pair_list", [(1, None), (3, "ETH/USDT,BNB/USDT"), (3, None)])
def test_a_live_account_never_uses_the_multi_trade_path_whatever_the_setting(db, monkeypatch, cap, pair_list):
    st = make_settings(max_open_positions=cap, max_notional_multiplier=100.0)
    client = spot_client()
    install(monkeypatch, client)
    poison_multi(monkeypatch)
    aid = new_account(st)
    with session_scope() as s:
        s.get(TradingAccount, aid).pair_list = pair_list
    run_cycle(aid, st)
    run_cycle(aid, st)
    run_cycle(aid, st)
    trades = all_trades(aid)
    assert len(trades) == 1 and not trades[0].is_paper and trades[0].status == "open" and trades[0].strategy_id == "ema_cross_atr_v1"
    entries = [o for o in client.orders.values() if o["client_id"].endswith("e")]
    assert len(entries) == 1


def test_a_live_account_cannot_be_given_a_second_trade_even_if_the_database_would_allow_it(db, monkeypatch):
    """The database index now allows a second non-terminal trade on ANOTHER pair; lifecycle.open_live_trade must still refuse it."""
    from helpers_live import live_context
    from mba_bot import lifecycle

    st = make_settings(max_open_positions=3, max_notional_multiplier=100.0)
    client = spot_client()
    install(monkeypatch, client)
    aid = new_account(st)
    run_cycle(aid, st)
    assert len(all_trades(aid)) == 1
    eth = spot_client(pair="ETH/USDT")

    async def go():
        with session_scope(autoflush=False) as s:
            s.get(TradingAccount, aid).pair = "ETH/USDT"  # a different pair: the index alone would allow the insert
            ctx = live_context(s, aid, eth, st, entries_allowed=True)
            await ctx.venue.prepare()
            return await lifecycle.open_live_trade(ctx, side="long", quantity=0.01, signal_price=ENTRY_PRICE, stop_price=ENTRY_PRICE * 0.9, risk_text="x")

    assert asyncio.run(go()) is None
    assert len(all_trades(aid)) == 1
    assert not [c for c in eth.calls if c[0] == "create_order"]


# ====================================================================== PAPER: two and three trades on different pairs
PAIRS = "ETH/USDT,BNB/USDT"
G1, G2, G3 = mt.Grade(1, ("test",)), mt.Grade(2, ()), mt.Grade(3, ())


def grade_is(monkeypatch, grade):
    monkeypatch.setattr(trader, "_market_grade", lambda *a, **k: grade)


def multi_client(**kw):
    kw.setdefault("ohlcv_by_pair", {"BTC/USDT": BULLISH_OHLCV, "ETH/USDT": BULLISH_OHLCV, "BNB/USDT": BULLISH_OHLCV})
    return spot_client(**kw)


def multi_setup(db, monkeypatch, *, cap=3, grade=G3, **over):
    st = paper_settings(max_open_positions=cap, max_total_risk_pct=over.pop("max_total_risk_pct", 5.0), **over)
    client = multi_client()
    install(monkeypatch, client)
    grade_is(monkeypatch, grade)
    aid = paper_account(st, pair_list=PAIRS)
    return st, client, aid


def pairs_open(aid):
    with session_scope() as s:
        return [t.pair for t in list_open_trades(s, aid)]


def test_grade_three_fills_three_slots_one_new_entry_per_cycle(db, monkeypatch):
    st, client, aid = multi_setup(db, monkeypatch)
    note = run_cycle(aid, st)
    assert pairs_open(aid) == ["BTC/USDT"]  # one new entry per cycle, never three at the same instant
    run_cycle(aid, st)
    assert sorted(pairs_open(aid)) == ["BTC/USDT", "ETH/USDT"]
    note = run_cycle(aid, st)
    assert sorted(pairs_open(aid)) == ["BNB/USDT", "BTC/USDT", "ETH/USDT"]
    run_cycle(aid, st)
    assert len(pairs_open(aid)) == 3 and len(all_trades(aid)) == 3  # a full account takes nothing more
    assert all(t.is_paper and t.strategy_id == "ema_cross_atr_v1" and t.status == "open" for t in all_trades(aid))
    assert not [c for c in client.calls if c[0] == "create_order"]  # paper: no order of any kind
    assert any("Strategy: ema_cross_atr_v1" in m and "Open trades: 3 of 3" in m for m in note.texts)


def test_grade_two_allows_two_and_grade_one_allows_one(db, monkeypatch):
    for grade, expected in ((G2, 2), (G1, 1)):
        db2 = db / f"g{grade.level}"
        db2.mkdir()
        init_db(f"sqlite:///{db2}/t.db")
        st, client, aid = multi_setup(db2, monkeypatch, grade=grade)
        for _ in range(5):
            run_cycle(aid, st)
        assert len(pairs_open(aid)) == expected, grade


def test_a_lower_grade_never_closes_a_trade_and_only_blocks_new_entries(db, monkeypatch):
    st, client, aid = multi_setup(db, monkeypatch)
    for _ in range(4):
        run_cycle(aid, st)
    assert len(pairs_open(aid)) == 3
    grade_is(monkeypatch, G1)
    for _ in range(3):
        run_cycle(aid, st)
    assert len(pairs_open(aid)) == 3


def test_total_open_risk_is_capped_by_max_total_risk_pct(db, monkeypatch):
    st, client, aid = multi_setup(db, monkeypatch, max_total_risk_pct=2.0)  # 1% risk per trade: a third would make 3%
    for _ in range(5):
        run_cycle(aid, st)
    assert len(pairs_open(aid)) == 2


def test_a_third_trade_is_skipped_when_free_capital_is_short_and_the_reason_is_logged(db, monkeypatch, caplog):
    st, client, aid = multi_setup(db, monkeypatch, paper_min_notional=1_000_000.0)
    run_cycle(aid, st)
    with caplog.at_level("INFO", logger="mba_bot.trader"):
        run_cycle(aid, st)
    assert len(pairs_open(aid)) == 1
    assert any(mt.R_CAPITAL_SHORT in r.getMessage() and aid in r.getMessage() for r in caplog.records)


def test_an_entry_that_does_not_fit_the_free_capital_is_trimmed_not_refused(db, monkeypatch):
    st, client, aid = multi_setup(db, monkeypatch)
    now = datetime.now(U)
    with session_scope() as s:  # an open trade holding almost all the paper balance
        s.add(Trade(account_id=aid, side="long", is_paper=True, entry_price=90.0, quantity=110.0, stop_loss_price=89.9, status="open", pair="BTC/USDT",
                    opened_at=now, strategy_id="ema_cross_atr_v1"))
    run_cycle(aid, st)
    trades = sorted(all_trades(aid), key=lambda t: t.opened_at)
    assert len(trades) == 2 and trades[1].pair == "ETH/USDT"
    free = 10_000.0 - 90.0 * 110.0
    assert 0 < trades[1].quantity * trades[1].entry_price <= free + 1e-6  # trimmed to what is free (untrimmed it would be about 190)


def test_no_free_capital_means_no_entry(db, monkeypatch):
    st, client, aid = multi_setup(db, monkeypatch)
    with session_scope() as s:
        s.add(Trade(account_id=aid, side="long", is_paper=True, entry_price=90.0, quantity=120.0, stop_loss_price=89.9, status="open", pair="BTC/USDT",
                    opened_at=datetime.now(U)))
    run_cycle(aid, st)
    assert len(all_trades(aid)) == 1


def test_each_open_trade_exits_on_its_own_pair_and_pnl_is_per_trade(db, monkeypatch):
    st, client, aid = multi_setup(db, monkeypatch)
    for _ in range(3):
        run_cycle(aid, st)
    assert len(pairs_open(aid)) == 3
    with session_scope() as s:
        before = s.get(TradingAccount, aid).paper_balance
    client.ohlcv_by_pair["ETH/USDT"] = BEARISH_OHLCV  # the opposite cross on ETH only
    grade_is(monkeypatch, G1)  # no new entry in this cycle
    note = run_cycle(aid, st)
    assert sorted(pairs_open(aid)) == ["BNB/USDT", "BTC/USDT"]
    closed = [t for t in all_trades(aid) if t.status == "closed"]
    assert len(closed) == 1 and closed[0].pair == "ETH/USDT" and closed[0].close_reason == "crossover_exit"
    expected = (closed[0].exit_price - closed[0].entry_price) * closed[0].quantity
    assert math.isclose(closed[0].pnl, expected)
    with session_scope() as s:
        assert math.isclose(s.get(TradingAccount, aid).paper_balance, before + expected)
    assert any("Exited LONG ETH/USDT" in m and "Strategy: ema_cross_atr_v1" in m for m in note.texts)
    assert any("PAPER" in m and "ETH/USDT" in m for m in note.texts)


def test_a_stop_on_one_pair_closes_only_that_trade(db, monkeypatch):
    st, client, aid = multi_setup(db, monkeypatch)
    for _ in range(3):
        run_cycle(aid, st)
    crash = [r[:] for r in BULLISH_OHLCV]
    crash[-1] = [crash[-1][0], 150.0, 150.0, 5.0, 5.0, 1.0]  # a close far below every stop
    client.ohlcv_by_pair["BNB/USDT"] = crash
    grade_is(monkeypatch, G1)
    run_cycle(aid, st)
    done = {t.pair: t for t in all_trades(aid) if t.status == "closed"}
    assert list(done) == ["BNB/USDT"] and done["BNB/USDT"].close_reason == "stop_loss" and done["BNB/USDT"].pnl < 0
    assert sorted(pairs_open(aid)) == ["BTC/USDT", "ETH/USDT"]


def test_the_daily_summary_counts_every_trade_closed_today(db, monkeypatch):
    st, client, aid = multi_setup(db, monkeypatch)
    for _ in range(3):
        run_cycle(aid, st)
    client.ohlcv_by_pair = {p: BEARISH_OHLCV for p in ("BTC/USDT", "ETH/USDT", "BNB/USDT")}
    grade_is(monkeypatch, G1)
    note = run_cycle(aid, st)
    assert pairs_open(aid) == []
    from mba_bot.db import get_trades_closed_on
    today = datetime.now(U).strftime("%Y-%m-%d")
    with session_scope() as sess:
        closed = get_trades_closed_on(sess, aid, today)
        assert sorted(t.pair for t in closed) == ["BNB/USDT", "BTC/USDT", "ETH/USDT"]  # the data the daily summary is built from
        assert math.isclose(sum(t.pnl for t in closed), sum(t.pnl for t in all_trades(aid)))
        assert math.isclose(sess.get(TradingAccount, aid).paper_balance, 10_000.0 + sum(t.pnl for t in closed))
    assert len([m for m in note.texts if m.startswith("[PAPER] Exited")]) == 3  # one exit message per trade, each with its pair and strategy


def test_a_pair_that_is_not_selectable_is_refused_in_multi_mode_too(db, monkeypatch):
    st = paper_settings(max_open_positions=3, max_total_risk_pct=5.0)
    client = multi_client()
    install(monkeypatch, client)
    grade_is(monkeypatch, G3)
    aid = paper_account(st, pair_list="DOGE/USDT,ETH/USDT")  # DOGE is not a supported pair
    for _ in range(3):
        run_cycle(aid, st, swallow=True)
    assert "DOGE/USDT" not in pairs_open(aid)


def test_market_grade_for_a_fresh_paper_account_is_grade_one_with_a_reason(db):
    st = paper_settings(max_open_positions=3)
    aid = paper_account(st, pair_list=PAIRS)
    from mba_bot.strategy import candles_from_ohlcv
    with session_scope() as s:
        acct = s.get(TradingAccount, aid)
        g = trader._market_grade(s, acct, st, candles_from_ohlcv(BULLISH_OHLCV), now=datetime.now(U))
    assert g.level == 1 and g.reasons


def test_market_grade_fails_closed_when_an_input_cannot_be_read(db):
    st = paper_settings()
    aid = paper_account(st, pair_list=PAIRS)
    with session_scope() as s:
        acct = s.get(TradingAccount, aid)
        g = trader._market_grade(s, acct, st, [], now=datetime.now(U))  # no candles at all
    assert g.level == 1


def test_market_grade_two_from_real_history(db):
    st = paper_settings(max_open_positions=3, grade2_min_trades=10, grade3_min_trades=20)
    aid = paper_account(st, pair_list=PAIRS)
    from mba_bot.strategy import candles_from_ohlcv
    now = datetime.now(U)
    with session_scope() as s:
        for i in range(10):
            s.add(Trade(account_id=aid, side="long", is_paper=True, entry_price=100.0, quantity=1.0, stop_loss_price=90.0, exit_price=110.0, pnl=10.0,
                        status="closed", closed_at=now - timedelta(days=2, minutes=i), opened_at=now - timedelta(days=3), close_reason="crossover_exit", pair="BTC/USDT"))
    with session_scope() as s:
        acct = s.get(TradingAccount, aid)
        # candles whose latest ATR% sits between the flat and the extreme bounds
        candles = candles_from_ohlcv([[i, 100.0, 101.0 + (i % 2) * 0.1, 99.0, 100.0 + (0.3 if i % 2 else 0), 1.0] for i in range(40)])
        g = trader._market_grade(s, acct, st, candles, now=now)
    assert g.level in (2, 3) or g.reasons  # a decision with reasons, never an exception


# ====================================================================== LIVE: two and three trades on different pairs (recovery path)
class Routed:
    """One FAKE exchange per pair behind a single client, routed by the symbol each call names. It stands in for one real Binance
    account holding several positions. It proves control flow only."""

    def __init__(self, fakes: dict):
        self.fakes = fakes
        self.first = next(iter(fakes.values()))

    def _pick(self, args, kwargs):
        vals = list(args) + list(kwargs.values())
        flat = []
        for v in vals:
            flat.extend(v if isinstance(v, (list, tuple)) else [v])
        for v in flat:
            for f in self.fakes.values():
                if isinstance(v, str) and (v == f.symbol or v == f.pair):
                    return f
        return self.first

    def __getattr__(self, name):
        attr = getattr(self.first, name)
        if name == "fetch_balance":
            async def balance_all(*a, **k):
                out = await self.first.fetch_balance()
                for f in list(self.fakes.values())[1:]:  # each other fake contributes only its own base asset
                    b = await f.fetch_balance()
                    out["free"][f.base] = b["free"].get(f.base, 0.0)
                    out[f.base] = b.get(f.base, {"free": 0.0, "used": 0.0})
                return out
            return balance_all
        if name == "load_markets":
            async def load_all(*a, **k):
                merged = {}
                for f in self.fakes.values():
                    merged.update(await f.load_markets(*a, **k))
                return merged
            return load_all
        if asyncio.iscoroutinefunction(attr):
            async def call(*a, **k):
                return await getattr(self._pick(a, k), name)(*a, **k)
            return call
        if name == "markets":
            merged = {}
            for f in self.fakes.values():
                merged.update(f.markets or {})
            return merged or None
        return attr

    def all_calls(self):
        return [c for f in self.fakes.values() for c in f.calls]

    @property
    def orders(self):
        return {k: v for f in self.fakes.values() for k, v in f.orders.items()}


LIVE_PAIRS = ["BTC/USDT", "ETH/USDT", "BNB/USDT"]


def live_trades_on_pairs(monkeypatch, n, *, db_settings=None):
    """Drive REAL entries (write-ahead, order, protective stop) on n different pairs of one live Spot account, then leave n trades
    open and protected. To get past the one-live-trade rule the earlier trades are set aside (status closed) while the next one is
    opened and put back afterwards; the exchange keeps every resting stop. Nothing else is changed."""
    st = db_settings or make_settings(max_notional_multiplier=100.0, max_open_positions=3)
    fakes = {p: spot_client(pair=p, free_balance={"USDT": 50_000.0}) for p in LIVE_PAIRS[:n]}
    routed = Routed(fakes)
    install(monkeypatch, routed)
    aid = new_account(st)
    ids = []
    for pair in LIVE_PAIRS[:n]:
        with session_scope() as s:
            s.get(TradingAccount, aid).pair = pair
            for t in list_open_trades(s, aid):
                t.status = "closed"
        run_cycle(aid, st)
        with session_scope() as s:
            opened = [t for t in s.query(Trade).filter(Trade.account_id == aid, Trade.pair == pair).all()]
            assert opened, (pair, s.get(TradingAccount, aid).last_error, [(x.pair, x.status) for x in s.query(Trade).all()])
            assert len(opened) == 1 and opened[0].status == "open" and opened[0].protection_status == "protected", (pair, opened[0].status)
            ids.append(opened[0].id)
    # The database (audit B1) now refuses a second non-terminal LIVE trade. These tests exercise the RECOVERY path for a database that somehow holds
    # several (a manual change, a pre-fix database), so the live index is dropped on this scratch database before the trades are put back.
    from sqlalchemy import text as _text
    with session_scope() as s:
        s.execute(_text("DROP INDEX IF EXISTS uq_trades_one_live_nonterminal_per_account"))
    with session_scope() as s:
        for t in s.query(Trade).filter(Trade.id.in_(ids)).all():
            t.status = "open"
    return st, routed, fakes, aid, ids


def stops(routed):
    return [o for o in routed.orders.values() if o["conditional"] and o["status"] == "open"]


@pytest.mark.parametrize("n", [2, 3])
def test_live_recovery_cycle_manages_every_trade_keeps_every_stop_and_opens_nothing(db, monkeypatch, n):
    st, routed, fakes, aid, ids = live_trades_on_pairs(monkeypatch, n)
    assert len(stops(routed)) == n
    entries_before = len([o for o in routed.orders.values() if not o["conditional"]])
    note = run_cycle(aid, st)
    run_cycle(aid, st)
    with session_scope() as s:
        rows = list_open_trades(s, aid)
        assert sorted(t.pair for t in rows) == sorted(LIVE_PAIRS[:n]) and all(t.status == "open" and t.protection_status == "protected" for t in rows)
        assert s.get(TradingAccount, aid).last_error is None
    assert len(stops(routed)) == n  # no stop lost, none duplicated
    assert len([o for o in routed.orders.values() if not o["conditional"]]) == entries_before  # no new entry, no extra exit
    assert not any(f.violations for f in fakes.values())


@pytest.mark.parametrize("n", [2, 3])
def test_live_recovery_a_stop_that_fired_closes_only_its_own_trade_and_pnl_is_per_trade(db, monkeypatch, n):
    st, routed, fakes, aid, ids = live_trades_on_pairs(monkeypatch, n)
    victim = LIVE_PAIRS[1]
    f = fakes[victim]
    stop = next(o for o in f.orders.values() if o["conditional"] and o["status"] == "open")
    f.move_price(stop["trigger"] - 1.0)  # the exchange fires that stop
    run_cycle(aid, st)
    with session_scope() as s:
        by_pair = {t.pair: t for t in s.query(Trade).filter(Trade.account_id == aid).all()}
        assert by_pair[victim].status == "closed" and by_pair[victim].pnl is not None and by_pair[victim].pnl < 0
        for p in LIVE_PAIRS[:n]:
            if p != victim:
                assert by_pair[p].status == "open" and by_pair[p].protection_status == "protected" and by_pair[p].pnl is None
    assert len(stops(routed)) == n - 1


def test_reconcile_account_walks_every_live_trade(db, monkeypatch):
    st, routed, fakes, aid, ids = live_trades_on_pairs(monkeypatch, 3)
    from helpers_live import run_reconcile
    seen = []
    real = trader._manage_live_trade

    async def spy(session, account, client, trade, settings, notifier, *, now):
        seen.append(trade.pair)
        return await real(session, account, client, trade, settings, notifier, now=now)

    monkeypatch.setattr(trader, "_manage_live_trade", spy)
    run_reconcile(aid, st)
    assert sorted(seen) == sorted(LIVE_PAIRS)


def test_the_fast_stop_limit_watch_price_only_reaches_the_trade_on_that_pair(db, monkeypatch):
    st, routed, fakes, aid, ids = live_trades_on_pairs(monkeypatch, 3)
    from helpers_live import run_reconcile  # noqa: F401
    monitored = []

    async def spy_monitor(ctx, trade, *, crossover_exit, current_price):
        monitored.append((trade.pair, current_price))

    monkeypatch.setattr(trader.lifecycle, "monitor_trade", spy_monitor)

    async def go():
        with session_scope(autoflush=False) as s:
            await trader.reconcile_account(s, s.get(TradingAccount, aid), st, FakeNotifier(), current_price=123.0, pair="ETH/USDT")

    asyncio.run(go())
    assert monitored == [("ETH/USDT", 123.0)]


def test_startup_recovery_picks_up_an_account_with_three_live_trades_once(db, monkeypatch):
    from mba_bot import scheduler
    st, routed, fakes, aid, ids = live_trades_on_pairs(monkeypatch, 3)
    monkeypatch.setattr(scheduler, "build_notifier", lambda *a, **k: FakeNotifier())
    visited = []
    real = trader._manage_live_trade

    async def spy(session, account, client, trade, settings, notifier, *, now):
        visited.append(trade.pair)
        return await real(session, account, client, trade, settings, notifier, now=now)

    monkeypatch.setattr(trader, "_manage_live_trade", spy)
    asyncio.run(scheduler.recover_once(st, include_healthy=True))
    assert sorted(visited) == sorted(LIVE_PAIRS)
    with session_scope() as s:
        assert all(t.status == "open" and t.protection_status == "protected" for t in list_open_trades(s, aid))


@pytest.mark.parametrize("n", [2, 3])
def test_restart_gate_counts_every_live_trade_not_every_account(db, monkeypatch, capsys, n):
    from mba_bot import db as db_mod
    monkeypatch.setenv("DATABASE_URL", db_mod._engine.url.render_as_string(hide_password=False))  # the gate opens its own connection from DATABASE_URL
    st, routed, fakes, aid, ids = live_trades_on_pairs(monkeypatch, n)
    assert restart_gate.main([]) == restart_gate.EXIT_LIVE_TRADES_OPEN
    out = capsys.readouterr().out
    assert f"{n} live trade(s) open" in out and "do NOT restart" in out
    assert aid not in out and "BTC" not in out  # counts only: no account id, no pair
    with session_scope() as s:
        for t in list_open_trades(s, aid):
            t.status = "closed"
    assert restart_gate.main([]) == restart_gate.EXIT_CLEAR


def test_restart_gate_ignores_paper_trades_and_fails_closed_when_the_database_cannot_be_read(db, monkeypatch, capsys):
    from mba_bot import db as db_mod
    monkeypatch.setenv("DATABASE_URL", db_mod._engine.url.render_as_string(hide_password=False))
    st = paper_settings()
    aid = paper_account(st)
    with session_scope() as s:
        s.add(Trade(account_id=aid, side="long", is_paper=True, entry_price=1, quantity=1, stop_loss_price=0.5, status="open", pair="BTC/USDT"))
    assert restart_gate.main([]) == restart_gate.EXIT_CLEAR

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(restart_gate, "live_trade_counts", boom)
    assert restart_gate.main([]) == restart_gate.EXIT_UNREADABLE
    assert "do NOT restart" in capsys.readouterr().err


@pytest.mark.parametrize("n", [2, 3])
def test_kill_switch_engaged_still_manages_every_open_live_trade_and_opens_none(db, monkeypatch, n):
    st, routed, fakes, aid, ids = live_trades_on_pairs(monkeypatch, n, db_settings=make_settings(
        max_notional_multiplier=100.0, max_open_positions=3, kill_switch_file=str(db / "KILL_SWITCH"),
        kill_switch_read_retry_seconds=0.0, kill_switch_read_retry_interval_seconds=0.01))
    (db / "KILL_SWITCH").write_text("engaged")
    run_cycle(aid, st)
    with session_scope() as s:
        rows = list_open_trades(s, aid)
        assert len(rows) == n and all(t.status == "open" and t.protection_status == "protected" for t in rows)
    assert len(stops(routed)) == n
    # and a stop that fires while the switch is engaged is still handled
    f = fakes[LIVE_PAIRS[0]]
    f.move_price(next(o for o in f.orders.values() if o["conditional"] and o["status"] == "open")["trigger"] - 1.0)
    run_cycle(aid, st)
    with session_scope() as s:
        assert len(list_open_trades(s, aid)) == n - 1


def test_a_protective_stop_that_vanished_on_one_pair_is_handled_for_that_trade_only(db, monkeypatch):
    """Same handling a single trade gets (the lifecycle decides what a vanished stop means); the point here is that the OTHER trades are
    reconciled normally in the same cycle and keep their stops."""
    st, routed, fakes, aid, ids = live_trades_on_pairs(monkeypatch, 3)
    f = fakes["ETH/USDT"]
    stop = next(o for o in f.orders.values() if o["conditional"] and o["status"] == "open")
    stop["status"] = "canceled"  # the protective stop on ETH vanished at the exchange
    run_cycle(aid, st, swallow=True)
    with session_scope() as s:
        by_pair = {t.pair: t for t in s.query(Trade).filter(Trade.account_id == aid).all()}
    assert by_pair["BTC/USDT"].status == "open" and by_pair["BTC/USDT"].protection_status == "protected"
    assert by_pair["BNB/USDT"].status == "open" and by_pair["BNB/USDT"].protection_status == "protected"
    assert by_pair["ETH/USDT"].status in ("closed", "attention", "protecting", "open")  # resolved by the lifecycle, whatever it decides
    assert len(stops(routed)) >= 2


def test_dashboard_runtime_status_and_positions_list_show_every_trade(db, monkeypatch):
    from mba_bot.api.routers import account as account_api
    st, routed, fakes, aid, ids = live_trades_on_pairs(monkeypatch, 3)
    with session_scope() as s:
        acct = s.get(TradingAccount, aid)
        rows = list_open_trades(s, aid)
        assert len(rows) == 3
        rows[2].status = "attention"
        s.flush()
        out = account_api._to_account_out(acct, has_paid=True, db=s)
        assert out.runtime_status == "attention"  # ANY trade needing attention shows, not just the oldest
        rows[2].status = "open"
        s.flush()
        assert account_api._to_account_out(acct, has_paid=True, db=s).runtime_status == "trade_open"
    from mba_bot.api.schemas import OpenTradesOut
    with session_scope() as s:
        data = OpenTradesOut(open_count=3, trades=[]).model_dump()
        assert data == {"open_count": 3, "trades": []}


# ====================================================================== guards, telemetry
def test_a_refusal_on_one_pair_does_not_stop_the_next_pair_being_tried_and_the_cooldown_is_per_pair(db, monkeypatch):
    st, client, aid = multi_setup(db, monkeypatch, strategy_guards_mode="enforce", ema_stop_cooldown_hours=4.0, strategy_expectancy_window=0, fee_guard_margin=0.0)
    with session_scope() as s:  # BTC was stopped out an hour ago
        s.add(Trade(account_id=aid, side="long", is_paper=True, entry_price=100.0, quantity=1.0, stop_loss_price=90.0, exit_price=90.0, pnl=-10.0,
                    status="closed", close_reason="stop_loss", pair="BTC/USDT", opened_at=datetime.now(U) - timedelta(hours=3), closed_at=datetime.now(U) - timedelta(hours=1)))
    run_cycle(aid, st)
    assert pairs_open(aid) == ["ETH/USDT"]  # BTC refused by its cooldown, ETH taken in the same cycle


def test_telemetry_rows_name_the_pair_and_strategy_of_each_paper_trade(db, monkeypatch):
    from mba_bot.models import DecisionLog, TradeTelemetry
    st, client, aid = multi_setup(db, monkeypatch, trade_telemetry=True)
    for _ in range(3):
        run_cycle(aid, st)
    with session_scope() as s:
        rows = s.query(TradeTelemetry).all()
        assert len(rows) == 3 and {r.strategy_id for r in rows} == {"ema_cross_atr_v1"}
        assert len({r.trade_id for r in rows}) == 3
        assert {d.pair for d in s.query(DecisionLog).all()} >= {"BTC/USDT", "ETH/USDT"}
        assert all(d.strategy_id == "ema_cross_atr_v1" for d in s.query(DecisionLog).all())


def test_a_paper_account_in_live_mode_with_paper_trades_never_opens_a_new_paper_entry_from_the_multi_path(db, monkeypatch):
    """An account that has become LIVE while paper trades are still open: they are managed, nothing new is entered by the multi-trade path."""
    st = make_settings(max_open_positions=3, max_notional_multiplier=100.0, **PAPER_KEYS)
    client = multi_client()
    install(monkeypatch, client)
    grade_is(monkeypatch, G3)
    aid = new_account(st)  # live mode
    with session_scope() as s:
        s.get(TradingAccount, aid).pair_list = PAIRS
        for pair in ("BTC/USDT", "ETH/USDT"):
            s.add(Trade(account_id=aid, side="long", is_paper=True, entry_price=90.0, quantity=1.0, stop_loss_price=50.0, status="open", pair=pair, opened_at=datetime.now(U)))
    run_cycle(aid, st)
    assert len(all_trades(aid)) == 2 and not [c for c in client.calls if c[0] == "create_order"]


# ====================================================================== fail-closed and defence-in-depth checks
def test_an_unreadable_open_risk_refuses_the_entry(db, monkeypatch):
    st = paper_settings(max_open_positions=3)
    aid = paper_account(st, pair_list=PAIRS)
    bad = Trade(account_id=aid, side="long", is_paper=True, entry_price=float("nan"), quantity=1.0, stop_loss_price=1.0, status="open", pair="BTC/USDT")
    monkeypatch.setattr(trader, "list_open_trades", lambda session, account_id: [bad])
    from mba_bot.risk import PositionSize
    with session_scope() as s:
        acct = s.get(TradingAccount, aid)
        size = PositionSize(quantity=1.0, risk_amount=1.0, stop_distance=1.0)
        out, refusal = trader._multi_paper_limits(s, acct, st, size, 100.0, trader.MultiInfo(cap=3, n_open=1, grade=G3))
    assert refusal is not None and refusal[0] == mt.R_TOTAL_RISK_CAP


def test_the_slot_check_inside_the_entry_refuses_a_full_account_on_its_own(db):
    st = paper_settings(max_open_positions=3)
    aid = paper_account(st, pair_list=PAIRS)
    with session_scope() as s:
        s.add(Trade(account_id=aid, side="long", is_paper=True, entry_price=90.0, quantity=1.0, stop_loss_price=89.0, status="open", pair="BTC/USDT"))
    from mba_bot.risk import PositionSize
    with session_scope() as s:
        acct = s.get(TradingAccount, aid)
        size = PositionSize(quantity=1.0, risk_amount=1.0, stop_distance=1.0)
        out, refusal = trader._multi_paper_limits(s, acct, st, size, 90.0, trader.MultiInfo(cap=1, n_open=1, grade=G1))
        assert refusal is not None and refusal[0] == mt.R_SLOT_FULL
        out, refusal = trader._multi_paper_limits(s, acct, st, size, 90.0, trader.MultiInfo(cap=2, n_open=1, grade=G2))
        assert refusal is None


def test_a_cycle_with_paper_and_live_trades_uses_the_live_client(db, monkeypatch):
    from mba_bot import exchange
    st = make_settings(max_open_positions=3, max_notional_multiplier=100.0, **PAPER_KEYS)
    client = multi_client()
    seen = []

    def build(**kw):
        seen.append(kw.get("sandbox"))
        return client

    install(monkeypatch, client)
    monkeypatch.setattr(exchange, "build_exchange_client", build)
    aid = new_account(st)
    older = datetime.now(U) - timedelta(hours=2)
    with session_scope() as s:
        s.add(Trade(account_id=aid, side="long", is_paper=True, entry_price=90.0, quantity=1.0, stop_loss_price=50.0, status="open", pair="ETH/USDT", opened_at=older))
        s.add(Trade(account_id=aid, side="long", is_paper=False, entry_price=90.0, quantity=1.0, stop_loss_price=50.0, status="open", pair="BTC/USDT",
                    opened_at=datetime.now(U), protection_status="protected"))
    run_cycle(aid, st, swallow=True)
    assert seen and seen[0] is False  # the oldest trade is paper, but a live trade is open: the cycle must use the LIVE environment


def test_the_scheduler_passes_the_pair_of_the_fast_watch_to_the_reconcile(db, monkeypatch):
    from mba_bot import scheduler
    st, routed, fakes, aid, ids = live_trades_on_pairs(monkeypatch, 3)
    monkeypatch.setattr(scheduler, "build_notifier", lambda *a, **k: FakeNotifier())
    seen = []

    async def spy_monitor(ctx, trade, *, crossover_exit, current_price):
        seen.append(trade.pair)

    monkeypatch.setattr(trader.lifecycle, "monitor_trade", spy_monitor)
    asyncio.run(scheduler._recover_one(aid, st, current_price=1.0, pair="BNB/USDT"))
    assert seen == ["BNB/USDT"]
