"""B3: small-capital pairs (SMALL_CAPITAL_PAIRS, default off).

PROVES (fake exchange, scratch SQLite): the minimum order comes only from the loaded exchange filters (minimum notional, and the quantity step or minimum quantity at the price); the capital needed is
that minimum divided by the customer's leverage cap (1 on Spot); a pair the capital cannot reach is recorded per account, pair, reason and day (counted, not duplicated), shown to the customer with the
capital needed, and left out of Auto Select, which then trades the next viable pair; a pair whose minimum is unknown is never skipped; with the switch off nothing is recorded or shown and Auto Select
scans exactly as before; the report command is read-only and prints no secret or full account id; the migration is additive and reversible.
DOES NOT PROVE: any real Binance minimum (the fixtures set their own), or the screens in a browser."""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import itertools
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from helpers_live import BULLISH_OHLCV, all_trades, install, make_settings, new_account, run_cycle, spot_client
from sqlalchemy import create_engine, inspect
from test_pair_expansion_engine import _fut, _multi_market

from mba_bot import pair_skip_report, small_capital as sc, trader
from mba_bot.db import init_db, session_scope
from mba_bot.models import PairSkip, TradingAccount

ROOT = Path(__file__).resolve().parent.parent
_n = itertools.count()


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")
    return tmp_path


def rules(**kw):
    base = dict(min_notional=100.0, market_min_qty=0.001, step=0.001)
    base.update(kw)
    return SimpleNamespace(**base)


# ------------------------------------------------------------------ pure rules
def test_the_minimum_order_is_the_larger_of_the_notional_and_the_smallest_quantity_at_the_price():
    assert sc.min_order_value(rules(min_notional=100.0), None) == 100.0  # no price: the notional only
    assert sc.min_order_value(rules(min_notional=100.0, market_min_qty=0.001), 50000.0) == 100.0  # 0.001 x 50000 = 50 < 100
    assert sc.min_order_value(rules(min_notional=5.0, market_min_qty=0.01), 50000.0) == 500.0  # the quantity floor is larger
    assert sc.min_order_value(rules(min_notional=5.0, market_min_qty=0.0, step=0.01), 50000.0) == 500.0  # at least one step
    assert sc.min_order_value(rules(min_notional=None), None) is None and sc.min_order_value(None, 100.0) is None


def test_capital_needed_is_the_minimum_over_the_leverage_cap():
    v = sc.assess(rules(min_notional=300.0), None, capital=50.0, leverage_cap=3)
    assert v.viable is False and v.min_capital == 100.0 and v.min_order_value == 300.0
    assert sc.assess(rules(min_notional=300.0), None, capital=100.0, leverage_cap=1).min_capital == 300.0  # Spot: 1x
    edge = sc.assess(rules(min_notional=300.0), None, capital=100.0, leverage_cap=3.0)
    assert edge.viable is True and edge.min_capital == 100.0  # capital x cap = 300 equals the minimum: viable
    assert sc.assess(rules(min_notional=300.0), None, capital=99.99, leverage_cap=3).viable is False
    unknown_capital = sc.assess(rules(min_notional=300.0), None, capital=None, leverage_cap=3)
    assert unknown_capital.viable is None and unknown_capital.min_capital == 100.0  # the capital needed is still stated
    assert sc.assess(rules(min_notional=None, market_min_qty=0, step=0), None, capital=1.0, leverage_cap=3).viable is None  # unknown: never skipped
    assert sc.leverage_cap_for("spot", 5) == 1.0 and sc.leverage_cap_for("futures", 5) == 5.0 and sc.leverage_cap_for("futures", None) == 1.0


def test_the_message_names_the_pair_the_capital_needed_and_the_leverage_cap():
    v = sc.assess(rules(min_notional=100.0), None, capital=20.0, leverage_cap=3)
    m = sc.message("BTC/USDT", v)
    assert m.startswith("BTC/USDT is too small for your trading capital.") and "about 33.34 USDT" in m and "your 3x leverage limit" in m and "Your trading capital is 20.00 USDT" in m
    assert "Choose another pair or raise your trading capital." in m


def test_one_row_per_account_pair_reason_and_day_with_a_count(db):
    aid = new_account(make_settings())
    v = sc.assess(rules(min_notional=100.0), None, capital=20.0, leverage_cap=3)
    day1 = datetime(2027, 3, 10, 12, 0, tzinfo=timezone.utc)
    with session_scope() as s:
        a = s.get(TradingAccount, aid)
        for i in range(3):
            sc.record_skip(s, a, "BTC/USDT", sc.R_BELOW_MIN_CAPITAL, v, now=day1 + timedelta(minutes=5 * i))
        sc.record_skip(s, a, "BTC/USDT", sc.R_BELOW_FILTERS, v, now=day1)
        sc.record_skip(s, a, "BTC/USDT", sc.R_BELOW_MIN_CAPITAL, v, now=day1 + timedelta(days=1))
    with session_scope() as s:
        rows = s.query(PairSkip).order_by(PairSkip.day_key, PairSkip.reason).all()
        assert [(r.day_key, r.reason, r.count) for r in rows] == [("2027-03-10", sc.R_BELOW_FILTERS, 1), ("2027-03-10", sc.R_BELOW_MIN_CAPITAL, 3), ("2027-03-11", sc.R_BELOW_MIN_CAPITAL, 1)]
        assert rows[1].min_capital == pytest.approx(33.333, rel=1e-3) and rows[1].leverage_cap == 3.0 and rows[1].capital == 20.0
        newest = sc.latest_for_pair(s, aid, "BTC/USDT", now=day1 + timedelta(days=1, minutes=5))
        assert newest.day_key == "2027-03-11" and sc.latest_for_pair(s, aid, "ETH/USDT", now=day1) is None
        assert sc.latest_for_pair(s, aid, "BTC/USDT", now=day1 + timedelta(days=5)) is None  # older than 24 hours: no message


# ------------------------------------------------------------------ Auto Select
def _scan(client, settings, *, market="futures", capital=20.0, with_viability=True):
    aid = new_account(settings, email=f"sc{next(_n)}@example.com", market_type=market, allocated_capital=capital)
    out: list | None = [] if with_viability else None

    async def go():
        with session_scope(autoflush=False) as session:
            account = session.get(TradingAccount, aid)
            account.max_leverage = 3
            result = await trader._scan_auto_pair(client, account, settings, capital=capital, viability_out=out)
            return result

    return aid, asyncio.run(go()), out


def test_auto_select_skips_pairs_the_capital_cannot_trade_at_the_leverage_cap_and_takes_the_next(db):
    settings = make_settings(small_capital_pairs=True)
    client = _multi_market(_fut(), {"BTC/USDT": 100.0, "ETH/USDT": 20.0, "BNB/USDT": 5.0, "XRP/USDT": 5.0})
    _, result, skipped = _scan(client, settings, capital=20.0)  # 20 x 3 = 60: BTC (100) is out
    assert result[0] == "ETH/USDT" and [p for p, _ in skipped] == ["BTC/USDT"] and skipped[0][1].min_capital == pytest.approx(100 / 3)
    _, result, skipped = _scan(client, settings, capital=34.0)  # 102 >= 100
    assert result[0] == "BTC/USDT" and skipped == []


def test_a_pair_with_an_unknown_minimum_is_never_skipped(db):
    settings = make_settings(small_capital_pairs=True)
    client = _multi_market(_fut(), {"BTC/USDT": None, "ETH/USDT": 5.0, "BNB/USDT": 5.0, "XRP/USDT": 5.0})
    _, result, skipped = _scan(client, settings, capital=1.0)
    assert result[0] == "BTC/USDT" and "BTC/USDT" not in [p for p, _ in skipped]  # no minimum given: not skipped, whatever the capital


def test_spot_accounts_are_judged_at_1x(db):
    import copy

    settings = make_settings(small_capital_pairs=True)
    client = spot_client()
    original = client.load_markets

    async def load_markets():
        await original()
        base = client.markets[client.symbol]
        markets = {}
        for pair, minimum in {"BTC/USDT": 100.0, "ETH/USDT": 20.0, "BNB/USDT": 5.0, "XRP/USDT": 5.0}.items():
            market = copy.deepcopy(base)
            market["symbol"], market["id"] = pair, pair.replace("/", "")
            market["info"]["filters"] = [f for f in market["info"]["filters"] if f["filterType"] not in ("NOTIONAL", "MIN_NOTIONAL")] + [{"filterType": "NOTIONAL", "minNotional": str(minimum)}]
            markets[pair] = market
        client.markets = markets
        return markets

    client.load_markets = load_markets
    aid = new_account(settings, email="spot-sc@example.com", market_type="spot", allocated_capital=30.0)

    async def go():
        out: list = []
        with session_scope(autoflush=False) as session:
            account = session.get(TradingAccount, aid)
            account.max_leverage = 5  # a Futures setting: Spot ignores it
            r = await trader._scan_auto_pair(client, account, settings, capital=30.0, viability_out=out)
        return r, out

    result, out = asyncio.run(go())
    assert [p for p, _ in out] == ["BTC/USDT"] and out[0][1].leverage_cap == 1.0 and out[0][1].min_capital == 100.0  # 30 x 1 < 100; ETH at 20 is reachable
    assert result[0] == "ETH/USDT"


def test_with_no_viability_list_the_scan_is_exactly_as_before(db):
    settings = make_settings(small_capital_pairs=False)
    client = _multi_market(_fut(), {"BTC/USDT": 100.0, "ETH/USDT": 20.0, "BNB/USDT": 5.0, "XRP/USDT": 5.0})
    _, result, out = _scan(client, settings, capital=None, with_viability=False)
    assert result[0] == "BTC/USDT" and out is None


# ------------------------------------------------------------------ the entry path and the customer message
def _entry(monkeypatch, settings, *, min_notional, capital):
    client = _fut(min_notional=min_notional)
    install(monkeypatch, client)
    aid = new_account(settings, market_type="futures", allocated_capital=capital)
    client._ohlcv = BULLISH_OHLCV
    run_cycle(aid, settings)
    return client, aid


def test_a_skipped_entry_is_recorded_with_the_capital_needed_when_the_switch_is_on(db, monkeypatch):
    st = make_settings(max_notional_multiplier=100.0, small_capital_pairs=True, protection_precheck_mode="off")
    client, aid = _entry(monkeypatch, st, min_notional=5000.0, capital=1000.0)
    assert all_trades(aid) == [] and client.orders == {}  # no order was sent
    with session_scope() as s:
        rows = s.query(PairSkip).all()
        assert len(rows) == 1 and rows[0].pair == "BTC/USDT" and rows[0].reason in sc.REASONS and rows[0].capital == 1000.0
        assert rows[0].leverage_cap == 3.0 and rows[0].min_capital >= 5000.0 / 3 - 1e-6  # the notional floor over the default 3x cap


def test_with_the_switch_off_nothing_is_recorded_and_the_entry_is_refused_as_before(db, monkeypatch):
    st = make_settings(max_notional_multiplier=100.0, small_capital_pairs=False, protection_precheck_mode="off")
    client, aid = _entry(monkeypatch, st, min_notional=5000.0, capital=1000.0)
    assert all_trades(aid) == [] and client.orders == {}
    with session_scope() as s:
        assert s.query(PairSkip).count() == 0


def test_a_viable_pair_records_nothing(db, monkeypatch):
    st = make_settings(max_notional_multiplier=100.0, small_capital_pairs=True, protection_precheck_mode="off")
    _, aid = _entry(monkeypatch, st, min_notional=5.0, capital=1000.0)
    assert len(all_trades(aid)) == 1
    with session_scope() as s:
        assert s.query(PairSkip).count() == 0


def test_a_paper_account_records_nothing(db, monkeypatch):
    st = make_settings(max_notional_multiplier=100.0, small_capital_pairs=True, protection_precheck_mode="off", demo_futures_api_key="k", demo_futures_api_secret="s")
    st = dataclasses.replace(st, trade_telemetry=True)
    client = _fut(min_notional=5000.0)
    install(monkeypatch, client)
    aid = new_account(st, market_type="futures", allocated_capital=1000.0)
    with session_scope() as s:
        s.get(TradingAccount, aid).bot_started_at = None
    client._ohlcv = BULLISH_OHLCV
    run_cycle(aid, st)
    with session_scope() as s:
        assert s.query(PairSkip).count() == 0  # a paper cycle never reaches the live minimum-order refusal; the helper also refuses paper (defence in depth)


# ------------------------------------------------------------------ the report
def test_the_report_is_read_only_and_prints_no_secret_or_full_id(db, monkeypatch, capsys):
    aid = new_account(make_settings(), email="report-me@example.com")
    now = datetime.now(timezone.utc)
    v = sc.assess(rules(min_notional=100.0), None, capital=20.0, leverage_cap=3)
    with session_scope() as s:
        a = s.get(TradingAccount, aid)
        sc.record_skip(s, a, "BTC/USDT", sc.R_BELOW_MIN_CAPITAL, v, now=now)
        sc.record_skip(s, a, "BTC/USDT", sc.R_BELOW_MIN_CAPITAL, v, now=now)
        sc.record_skip(s, a, "ETH/USDT", sc.R_BELOW_FILTERS, v, now=now - timedelta(days=30))  # too old for 7 days
    path = db / "test.db"
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    assert pair_skip_report.main([]) == 0
    out = capsys.readouterr().out
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before  # nothing written
    assert "by reason (skips): pair_below_minimum_capital=2" in out and "BTC/USDT" in out and "ETH/USDT" not in out and "up to 33.34 USDT" in out
    assert aid[:8] in out and aid not in out and "report-me" not in out and "@" not in out
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}/missing.db")
    assert pair_skip_report.main([]) == 2 and not (db / "missing.db").exists()


def test_the_report_says_so_when_nothing_was_recorded(db, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}/test.db")
    assert pair_skip_report.main(["--days", "7"]) == 0 and "no skipped pairs recorded" in capsys.readouterr().out


# ------------------------------------------------------------------ the migration
def test_the_migration_is_additive_idempotent_and_reversible(tmp_path, monkeypatch, keep_logging):
    path = tmp_path / "m.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(cfg, "e3b6c8a1d5f2")
    tables = lambda: set(inspect(create_engine(f"sqlite:///{path}")).get_table_names())
    before = tables()
    command.upgrade(cfg, "f4c7d9b2a6e1")
    assert tables() == before | {"pair_skips"}
    command.upgrade(cfg, "f4c7d9b2a6e1")
    command.downgrade(cfg, "e3b6c8a1d5f2")
    assert tables() == before
    command.upgrade(cfg, "head")
    assert "pair_skips" in tables()


def test_the_notice_is_on_the_dashboard_and_in_bot_settings_and_only_shows_the_servers_text():
    front = ROOT / "frontend" / "src"
    app, settings = (front / "App.jsx").read_text(), (front / "pages" / "BotSettings.jsx").read_text()
    assert "<PairSkipNotice account={account} />" in app and "<PairSkipNotice account={account} />" in settings
    notice = (front / "components" / "PairSkipNotice.jsx").read_text()
    assert "pairSkipNotice(account)" in notice and "pair_skip_message" in (front / "lib" / "pairSkip.js").read_text()


def test_the_account_api_carries_the_message_only_while_the_switch_is_on(db):
    import dataclasses

    from mba_bot.api.routers import account as acct_router
    from mba_bot.config import load_settings

    aid = new_account(make_settings(), email="msg@example.com", allocated_capital=20.0)
    v = sc.assess(rules(min_notional=100.0), None, capital=20.0, leverage_cap=3)
    with session_scope() as s:
        a = s.get(TradingAccount, aid)
        sc.record_skip(s, a, a.pair, sc.R_BELOW_MIN_CAPITAL, v)
    with session_scope() as s:
        a = s.get(TradingAccount, aid)
        on = acct_router._pair_skip_fields(a, db=s, settings=dataclasses.replace(load_settings(), small_capital_pairs=True))
        off = acct_router._pair_skip_fields(a, db=s, settings=dataclasses.replace(load_settings(), small_capital_pairs=False))
    assert off == {} and on["pair_skip_message"].startswith("BTC/USDT is too small") and on["pair_skip_min_capital"] == pytest.approx(100 / 3)


def test_a_live_auto_select_cycle_records_every_pair_its_capital_cannot_trade(db, monkeypatch):
    st = make_settings(max_notional_multiplier=100.0, small_capital_pairs=True, protection_precheck_mode="off")
    client = _multi_market(_fut(), {"BTC/USDT": 5000.0, "ETH/USDT": 5000.0, "BNB/USDT": 5000.0, "XRP/USDT": 5000.0})
    install(monkeypatch, client)
    aid = new_account(st, market_type="futures", allocated_capital=20.0)
    with session_scope() as s:
        s.get(TradingAccount, aid).pair_selection_mode = "auto"
    client._ohlcv = BULLISH_OHLCV
    run_cycle(aid, st)
    assert all_trades(aid) == [] and client.orders == {}
    with session_scope() as s:
        rows = s.query(PairSkip).all()
        assert sorted(r.pair for r in rows) == ["BNB/USDT", "BTC/USDT", "ETH/USDT", "XRP/USDT"] and {r.reason for r in rows} == {sc.R_BELOW_MIN_CAPITAL}
        assert all(r.min_capital == pytest.approx(5000 / 3) for r in rows)
    assert not [c for c in client.calls if c[0] == "fetch_ohlcv"], "no candles are fetched for pairs the account cannot trade"


def test_the_pairs_answer_is_unchanged_with_the_switch_off_and_adds_the_capital_only_when_it_has_one():
    from mba_bot.api.schemas import PairOut

    assert PairOut(pair="BTC/USDT", available=True).model_dump() == {"pair": "BTC/USDT", "available": True, "reason": None}
    assert PairOut(pair="BTC/USDT", available=False, reason="x", min_capital=12.5).model_dump()["min_capital"] == 12.5
