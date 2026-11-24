"""Part F: net P&L (docs/safety/net_pnl.md): Futures funding per trade, the asset of a commission paid in BNB, the real taker fee tier, and the report lines.

PROVES (fake exchange, scratch SQLite): funding is summed from the account's income history over the holding period and stored signed; a failed funding read leaves NULL and never keeps a trade open; a Spot trade has
funding 0; a BNB commission is recorded as asset and amount while the quote fee stays NULL (never guessed); the real taker rate is used for the cost buffer when it can be read and the assumed one otherwise; reports show
gross, fees, funding and net as separate figures and net only when fees AND funding are known for every trade; the take profit target is net of costs.
DOES NOT PROVE: what Binance returns (the income endpoint and commissionRate are UNVERIFIED here) or that net is positive."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from helpers_live import BEAR_PRICE, BEARISH_OHLCV, futures_client, install, make_settings, new_account, open_protected, run_cycle, spot_client, the_trade

from mba_bot import fee_tier, profit_protect as pp, reporting, venue as venue_mod
from mba_bot.db import init_db, session_scope
from mba_bot.models import Trade


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")
    fee_tier.reset_cache()


def run(coro):
    return asyncio.run(coro)


def F(client):
    return venue_mod.build_venue(client, market_type="futures", pair="BTC/USDT")


# ---------------------------------------------------------------------------------------------- the venue reads
def test_funding_is_summed_inside_the_window_signed_and_a_bad_answer_is_none():
    c = futures_client()
    c.funding_rows = [{"timestamp": 1_000, "amount": -0.30}, {"timestamp": 2_000, "amount": 0.10}, {"timestamp": 9_000_000, "amount": -5.0}]
    assert run(F(c).funding_income(start_ms=500, end_ms=3_000)) == pytest.approx(-0.20)
    assert run(F(c).funding_income(start_ms=3_000, end_ms=4_000)) == 0.0  # none in the window: a clean zero, which is KNOWN
    c.funding_rows = [{"timestamp": 1_000, "amount": "x"}]
    assert run(F(c).funding_income(start_ms=0, end_ms=2_000)) is None
    c.funding_rows = [{"timestamp": 1_000, "amount": float("nan")}]
    assert run(F(c).funding_income(start_ms=0, end_ms=2_000)) is None
    sc = spot_client()
    spot = venue_mod.build_venue(sc, market_type="spot", pair="BTC/USDT")
    assert run(spot.funding_income(start_ms=0, end_ms=1)) == 0.0 and not [x for x in sc.calls if x[0] == "fetch_funding_history"]


def test_the_taker_rate_is_a_sane_fraction_or_none():
    c = futures_client()
    c.taker_fee = 0.00045
    assert run(F(c).taker_fee_rate()) == pytest.approx(0.00045)
    for bad in (0.5, -0.1, float("nan")):
        c.taker_fee = bad
        assert run(F(c).taker_fee_rate()) is None


def test_the_fee_tier_uses_the_account_rate_caches_it_and_falls_back_to_the_assumed_rate():
    c = futures_client()
    c.taker_fee = 0.0004
    v = F(c)
    fee_tier.reset_cache()
    clock = [100.0]
    assert run(fee_tier.taker_bps(v, "a", assumed_bps=5.0, clock=lambda: clock[0])) == (pytest.approx(4.0), "account")
    n = len([x for x in c.calls if x[0] == "fetch_trading_fee"])
    run(fee_tier.taker_bps(v, "a", assumed_bps=5.0, clock=lambda: clock[0]))
    assert len([x for x in c.calls if x[0] == "fetch_trading_fee"]) == n  # cached
    clock[0] += 24 * 3600 + 1
    run(fee_tier.taker_bps(v, "a", assumed_bps=5.0, clock=lambda: clock[0]))
    assert len([x for x in c.calls if x[0] == "fetch_trading_fee"]) == n + 1
    c2 = futures_client()  # no commission endpoint: the assumed rate, said so, and not re-tried every round
    fee_tier.reset_cache()
    assert run(fee_tier.taker_bps(F(c2), "b", assumed_bps=5.0, clock=lambda: 0.0)) == (5.0, "assumed")
    k = len([x for x in c2.calls if x[0] == "fetch_trading_fee"])
    run(fee_tier.taker_bps(F(c2), "b", assumed_bps=5.0, clock=lambda: 10.0))
    assert len([x for x in c2.calls if x[0] == "fetch_trading_fee"]) == k


# ---------------------------------------------------------------------------------------------- recorded at close
def close_trade(client, settings, aid):
    client._ohlcv, client.price = BEARISH_OHLCV, BEAR_PRICE
    run_cycle(aid, settings)
    return the_trade(aid)


def test_funding_is_stored_signed_at_close(db, monkeypatch):
    client, settings, aid = open_protected(monkeypatch, market="futures")
    now = int(time.time() * 1000)
    client.funding_rows = [{"timestamp": now - 1000, "amount": -0.25}, {"timestamp": now - 500, "amount": 0.05}]
    t = close_trade(client, settings, aid)
    assert t.status == "closed" and t.funding_quote == pytest.approx(-0.20) and t.funding_read_at is not None
    assert [x for x in client.calls if x[0] == "fetch_funding_history"]


def test_a_failed_funding_read_leaves_null_and_never_keeps_the_trade_open(db, monkeypatch):
    import ccxt

    client, settings, aid = open_protected(monkeypatch, market="futures")
    client.fail("fetch_funding_history", exc=ccxt.RequestTimeout("down"), times=9)
    t = close_trade(client, settings, aid)
    assert t.status == "closed" and t.funding_quote is None and t.funding_read_at is None  # unknown, not zero


def test_a_spot_trade_has_no_funding_and_no_funding_read(db, monkeypatch):
    client, settings, aid = open_protected(monkeypatch, market="spot")
    t = close_trade(client, settings, aid)
    assert t.status == "closed" and t.funding_quote == 0.0 and not [x for x in client.calls if x[0] == "fetch_funding_history"]


def test_a_bnb_commission_is_recorded_as_asset_and_amount_and_the_quote_fee_stays_null(db, monkeypatch):
    client, settings, aid = open_protected(monkeypatch, market="futures")
    for f in client.fills:
        if isinstance(f.get("fee"), dict):
            f["fee"] = {"currency": "BNB", "cost": 0.002}
    orig = client._add_fill

    def bnb_fill(side, amount, price, order_id=None, fee=None):  # every later fill (the exit) is also charged in BNB
        return orig(side, amount, price, order_id, {"currency": "BNB", "cost": 0.002} if fee else None)

    client._add_fill = bnb_fill
    t = close_trade(client, settings, aid)
    assert t.status == "closed" and t.entry_fee_quote is None and t.exit_fee_quote is None  # never guessed or valued
    assert t.fee_asset == "BNB" and t.fee_asset_amount == pytest.approx(0.004)  # entry and exit, as paid


def test_a_usdt_commission_records_no_other_asset(db, monkeypatch):
    client, settings, aid = open_protected(monkeypatch, market="futures")
    t = close_trade(client, settings, aid)
    assert t.fee_asset is None and t.fee_asset_amount is None and t.entry_fee_quote is not None


def test_the_other_fee_assets_helper_ignores_quote_base_zero_and_junk():
    assert venue_mod.fee_other_of([{"currency": "BNB", "cost": 0.01}, {"currency": "USDT", "cost": 1.0}, {"currency": "BTC", "cost": 1.0}, {"currency": "BNB", "cost": 0.02}], base="BTC", quote="USDT") == {"BNB": pytest.approx(0.03)}
    assert venue_mod.fee_other_of([{"currency": "BNB", "cost": 0}, {"currency": "BNB", "cost": -1}, {"currency": "BNB", "cost": float("nan")}, {"currency": None, "cost": 1}, "x"], base="BTC", quote="USDT") == {}


# ---------------------------------------------------------------------------------------------- the cost buffer uses the tier and funding
def test_the_live_stage_1_buffer_uses_the_real_taker_rate_and_the_funding_paid(db, monkeypatch):
    from helpers_profit import candles, live

    client, settings, aid, base = live(monkeypatch, profit_lock_stage1_r=1.0)
    client.taker_fee = 0.0004  # 4 bps: a better tier than the assumed 5
    now = int(time.time() * 1000)
    client.funding_rows = [{"timestamp": now - 1000, "amount": -0.30}]  # paid 0.30 on the position
    candles(client, base, [(95, 100, 92, 99), (99, 140, 98, 139)])
    run_cycle(aid, settings)
    t = the_trade(aid)
    qty = t.quantity
    expected = 90.0 + 90.0 * (2 * 4.0 + 11.0) / 10_000 + 0.30 / qty
    assert t.stop_loss_price == pytest.approx(expected, abs=0.011) and t.funding_quote == pytest.approx(-0.30)
    assert [x for x in client.calls if x[0] == "fetch_trading_fee"]


def test_with_no_commission_endpoint_the_assumed_rate_is_used_and_funding_received_never_lowers_the_buffer(db, monkeypatch):
    from helpers_profit import candles, live

    client, settings, aid, base = live(monkeypatch, profit_lock_stage1_r=1.0)
    now = int(time.time() * 1000)
    client.funding_rows = [{"timestamp": now - 1000, "amount": +0.50}]  # received
    candles(client, base, [(95, 100, 92, 99), (99, 140, 98, 139)])
    run_cycle(aid, settings)
    assert the_trade(aid).stop_loss_price == pytest.approx(90.0 + 90.0 * 21 / 10_000, abs=0.011)


def test_the_take_profit_target_is_net_of_costs():
    from mba_bot import ema_exits

    assert ema_exits.take_profit_level("long", 100.0, 90.0, 2.0) == 120.0  # the plain level of PR #84 is unchanged
    assert ema_exits.take_profit_level("long", 100.0, 90.0, 2.0, 0.21) == pytest.approx(120.21)
    assert ema_exits.take_profit_level("short", 100.0, 110.0, 2.0, 0.21) == pytest.approx(79.79)
    assert ema_exits.take_profit_level("long", 100.0, 90.0, 2.0, -5.0) == 120.0  # a negative buffer never lowers the target
    assert ema_exits.take_profit_level("long", 100.0, 90.0, 0.0, 0.21) is None  # off stays off


# ---------------------------------------------------------------------------------------------- the report lines
def T(pnl, entry_fee, exit_fee, funding, kind="stop_market_close", paper=False):
    return SimpleNamespace(is_paper=paper, is_demo=False, pnl=pnl, entry_fee_quote=entry_fee, exit_fee_quote=exit_fee, funding_quote=funding, protective_kind=kind, strategy_id=None, pair="BTC/USDT", status="closed")


def group(report):
    return report["modes"][0]["total"]


def test_net_is_gross_minus_fees_plus_funding_only_when_both_are_known_for_every_trade():
    r = reporting.build([T(10.0, 0.5, 0.5, -0.4), T(-2.0, 0.2, 0.2, 0.1)])
    g = group(r)
    assert (g["gross_pnl"], g["fees"], g["funding"], g["fees_complete"], g["funding_complete"]) == (8.0, 1.4, pytest.approx(-0.3), True, True)
    assert g["net_pnl"] == pytest.approx(8.0 - 1.4 - 0.3)  # funding paid reduces it, funding received adds
    unknown = group(reporting.build([T(10.0, 0.5, 0.5, -0.4), T(-2.0, 0.2, 0.2, None)]))
    assert unknown["fees_complete"] is True and unknown["funding_complete"] is False and unknown["net_pnl"] is None and unknown["trades_with_funding"] == 1
    no_fee = group(reporting.build([T(10.0, None, None, -0.4)]))
    assert no_fee["funding_complete"] is True and no_fee["fees_complete"] is False and no_fee["net_pnl"] is None


def test_a_spot_trade_counts_as_funding_known_and_a_practice_group_never_has_net():
    spot = group(reporting.build([T(5.0, 0.1, 0.1, None, kind="stop_loss")]))
    assert spot["funding_complete"] is True and spot["net_pnl"] == pytest.approx(4.8) and spot["funding"] == 0.0  # Spot has none: known zero, the net is gross - fees
    paper = group(reporting.build([T(5.0, 0.1, 0.1, 0.0, paper=True)]))
    assert paper["net_pnl"] is None and paper["funding_complete"] is False and paper["simulated"] is True and paper["funding"] is None and paper["trades_with_funding"] == 0


def test_the_daily_summary_says_which_part_is_known():
    today = "2027-03-10"
    full = reporting.daily_summary_text(today, [T(10.0, 0.5, 0.5, -0.4)], "USDT")
    assert "net +8.60 USDT after fees and funding (gross +10.00, fees 1.00, funding -0.40)" in full
    part = reporting.daily_summary_text(today, [T(10.0, 0.5, 0.5, None)], "USDT")
    assert "after fees, before funding" in part and "net" not in part.split("Live:")[1]
    none_ = reporting.daily_summary_text(today, [T(10.0, None, None, None)], "USDT")
    assert "before fees" in none_ and "net" not in none_.split("Live:")[1]


def test_the_net_pnl_migration_is_additive_idempotent_and_reversible(tmp_path):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect, text

    path = next((Path(__file__).resolve().parents[1] / "alembic" / "versions").glob("a7d3e9f1c5b2_*.py"))
    spec = importlib.util.spec_from_file_location("net_pnl_migration", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    assert mig.down_revision == "e5b2c8d4f1a7"
    eng = create_engine(f"sqlite:///{tmp_path}/m.db")
    with eng.begin() as c:
        c.execute(text("create table trades (id varchar(36) primary key)"))
        c.execute(text("insert into trades values ('t1')"))

    def go(fn):
        with eng.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                fn()

    go(mig.upgrade)
    go(mig.upgrade)
    cols = {c["name"]: c for c in inspect(eng).get_columns("trades")}
    assert all(cols[n]["nullable"] for n in ("funding_quote", "funding_read_at", "fee_asset", "fee_asset_amount"))
    with eng.begin() as c:
        assert c.execute(text("select funding_quote, fee_asset from trades")).one() == (None, None)  # no backfill: an old trade stays unknown
    go(mig.downgrade)
    go(mig.downgrade)
    assert not {"funding_quote", "funding_read_at", "fee_asset", "fee_asset_amount"} & {c["name"] for c in inspect(eng).get_columns("trades")}
