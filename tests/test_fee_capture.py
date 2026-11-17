"""PR 2, fee capture: the ACTUAL commission of the entry and exit fills is stored on the trade, in the quote currency, from the exchange's own fill records.

PROVES (fake exchange, scratch SQLite): the conversion rules (quote fee, base fee at the fill price, BNB and unknown fees are NOT guessed); entry and exit fees for Spot and Futures; a fee that cannot be read never changes what the
trade lifecycle does; paper trades stay NULL; nothing in the trading path reads the columns; the migration is additive, idempotent and reversible.
DOES NOT PROVE: that real Binance fills carry the fee fields ccxt documents (the fake does), or what Binance charges."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from helpers_live import BEAR_PRICE, BEARISH_OHLCV, BULLISH_OHLCV, all_trades, futures_client, install, make_settings, new_account, open_protected, run_cycle, spot_client, the_trade
from sqlalchemy import create_engine, inspect

from mba_bot import venue as venue_mod
from mba_bot.db import init_db, session_scope
from mba_bot.models import Trade

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")
    return tmp_path


# ------------------------------------------------------------------ the conversion rules
@pytest.mark.parametrize("entries,price,expected", [
    ([{"currency": "USDT", "cost": 0.5}], 100.0, 0.5),
    ([{"currency": "BTC", "cost": 0.001}], 50_000.0, 50.0),
    ([{"currency": "USDT", "cost": 0.5}, {"currency": "BTC", "cost": 0.001}], 50_000.0, 50.5),
    ([{"currency": "BNB", "cost": 0.01}], 100.0, None),  # an asset the bot cannot value: not guessed
    ([{"currency": "BNB", "cost": 0}], 100.0, 0.0),  # a zero fee adds nothing whatever the asset
    ([{"currency": "BTC", "cost": 0.001}], None, None),  # a base fee needs a price
    ([{"currency": "BTC", "cost": 0.001}], 0.0, None),
    ([], 100.0, None),
    (None, 100.0, None),
    ([{"currency": "USDT", "cost": None}], 100.0, None),
    ([{"currency": "USDT", "cost": float("nan")}], 100.0, None),
    ([{"currency": "USDT", "cost": float("inf")}], 100.0, None),
    ([{"currency": "USDT", "cost": 0.5}, {"currency": "BNB", "cost": 0.01}], 100.0, None),  # one unknown part makes the total unknown
    (["x", 3], 100.0, None),
])
def test_fee_quote_of(entries, price, expected):
    assert venue_mod.fee_quote_of(entries, base="BTC", quote="USDT", price=price) == expected


def test_order_from_ccxt_carries_the_quote_fee():
    raw = {"id": "1", "clientOrderId": "c", "status": "closed", "amount": 1, "filled": 1, "average": 100.0, "fees": [{"currency": "USDT", "cost": 0.1}]}
    assert venue_mod.order_from_ccxt(raw, base="BTC", quote="USDT").fee_quote == 0.1
    assert venue_mod.order_from_ccxt({**raw, "fees": []}, base="BTC", quote="USDT").fee_quote is None
    assert venue_mod.order_from_ccxt(raw, base="BTC").fee_quote is None  # no quote given: not guessed


# ------------------------------------------------------------------ the live lifecycle
def _live_spot(monkeypatch, **client_kw):
    return open_protected(monkeypatch, market="spot", **client_kw)


@pytest.mark.parametrize("fee_in", ["base", "quote"])
def test_a_spot_entry_and_exit_record_the_actual_commission(db, monkeypatch, fee_in):
    client, settings, aid = _live_spot(monkeypatch, fee_in=fee_in, fee_rate=0.001)
    t = the_trade(aid)
    expected_entry = t.quantity / (1 - 0.001) * 0.001 * t.entry_price if fee_in == "base" else None
    gross_qty = t.quantity + (t.entry_fee_base or 0.0)
    if fee_in == "base":
        assert t.entry_fee_quote == pytest.approx(t.entry_fee_base * t.entry_price)
    else:
        assert t.entry_fee_quote == pytest.approx(gross_qty * t.entry_price * 0.001)
    client._ohlcv, client.price = BEARISH_OHLCV, BEAR_PRICE
    run_cycle(aid, settings)
    t = the_trade(aid)
    assert t.status == "closed" and t.exit_fee_quote == pytest.approx(t.quantity * t.exit_price * 0.001)


def test_a_fee_in_bnb_is_not_guessed(db, monkeypatch):
    client, settings, aid = _live_spot(monkeypatch, fee_in="bnb")
    assert the_trade(aid).entry_fee_quote is None


def test_no_fee_information_gives_none_and_changes_nothing_else(db, monkeypatch):
    # (a Spot entry with no commission information at all takes the existing emergency path; on Futures the trade simply opens and no fee is claimed)
    st = make_settings(max_notional_multiplier=100.0, protection_precheck_mode="off")
    client = futures_client(fee_info=False, trades_fee_info=False)
    install(monkeypatch, client)
    aid = new_account(st, market_type="futures")
    run_cycle(aid, st)
    t = the_trade(aid)
    assert t.status == "open" and t.protection_status == "protected" and t.entry_fee_quote is None


def test_a_futures_entry_and_exit_take_the_fee_from_the_account_trade_records(db, monkeypatch):
    st = make_settings(max_notional_multiplier=100.0, protection_precheck_mode="off")
    client = futures_client()
    install(monkeypatch, client)
    aid = new_account(st, market_type="futures")
    run_cycle(aid, st)
    t = the_trade(aid)
    assert t.status == "open"
    assert t.entry_fee_quote is None  # not read while the trade is being protected; captured at close
    client._ohlcv, client.price = BEARISH_OHLCV, BEAR_PRICE
    run_cycle(aid, st)
    t = the_trade(aid)
    assert t.status == "closed"
    entry_fills = [f for f in client.fills if f["side"] == "buy"]
    assert entry_fills and t.entry_fee_quote == pytest.approx(sum(f["fee"]["cost"] for f in entry_fills))
    exit_fills = [f for f in client.fills if f["side"] == "sell"]
    assert exit_fills and t.exit_fee_quote == pytest.approx(sum(f["fee"]["cost"] for f in exit_fills))


def test_a_failing_fee_read_never_changes_the_lifecycle(db, monkeypatch):
    async def boom(self, **kw):
        raise RuntimeError("trade history unavailable")

    monkeypatch.setattr(venue_mod.Venue, "fills_fee_quote", boom)
    st = make_settings(max_notional_multiplier=100.0, protection_precheck_mode="off")
    client = futures_client()
    install(monkeypatch, client)
    aid = new_account(st, market_type="futures")
    run_cycle(aid, st)
    t = the_trade(aid)
    assert t.status == "open" and t.protection_status == "protected" and t.entry_fee_quote is None
    client._ohlcv, client.price = BEARISH_OHLCV, BEAR_PRICE
    run_cycle(aid, st)
    t = the_trade(aid)
    assert t.status == "closed" and t.exit_fee_quote is None and t.pnl is not None  # closed exactly as before, the fee just was not captured


def test_pnl_is_unchanged_by_the_fee_columns(db, monkeypatch):
    client, settings, aid = _live_spot(monkeypatch, fee_rate=0.001)
    client._ohlcv, client.price = BEARISH_OHLCV, BEAR_PRICE
    run_cycle(aid, settings)
    t = the_trade(aid)
    assert t.pnl == pytest.approx((t.exit_price - t.entry_price) * t.quantity)  # gross, as labelled on the screens; the fee columns are for reports only


def test_paper_trades_leave_the_columns_null(db, monkeypatch):
    from mba_bot.models import TradingAccount

    st = make_settings(max_notional_multiplier=100.0, testnet_spot_api_key="paper-key-1234567890", testnet_spot_api_secret="paper-secret-1234567890")
    install(monkeypatch, spot_client(ohlcv=BULLISH_OHLCV))
    aid = new_account(st)
    with session_scope() as s:
        s.get(TradingAccount, aid).bot_started_at = None  # PAPER
    run_cycle(aid, st)
    t = the_trade(aid)
    assert t.is_paper and t.entry_fee_quote is None and t.exit_fee_quote is None


def test_nothing_in_the_trading_path_reads_the_fee_columns():
    allowed = {"models.py", "lifecycle.py", "pilot_status.py", "paper_export.py"}
    for path in (ROOT / "src" / "mba_bot").rglob("*.py"):
        if path.name in allowed or "alembic" in str(path):
            continue
        assert "entry_fee_quote" not in path.read_text() and "exit_fee_quote" not in path.read_text(), path
    life = (ROOT / "src" / "mba_bot" / "lifecycle.py").read_text()
    # lifecycle only WRITES them (keyword arguments to _cas/_set), never branches on them
    for m in re.finditer(r"(entry_fee_quote|exit_fee_quote)", life):
        line = life[life.rfind("\n", 0, m.start()) + 1: life.find("\n", m.end())]
        assert "=" in line or "def " in line or "#" in line or "(" in line, line


# ------------------------------------------------------------------ migration
def _cfg(monkeypatch, path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    return cfg


def test_the_migration_is_additive_idempotent_and_reversible(tmp_path, monkeypatch, keep_logging):
    path = tmp_path / "m.db"
    cfg = _cfg(monkeypatch, path)
    command.upgrade(cfg, "c1f4a8d2e6b3")
    cols = lambda: {c["name"] for c in inspect(create_engine(f"sqlite:///{path}")).get_columns("trades")}
    before = cols()
    assert not {"entry_fee_quote", "exit_fee_quote"} & before
    command.upgrade(cfg, "d2a5b9c7e1f4")
    assert cols() == before | {"entry_fee_quote", "exit_fee_quote"}
    command.upgrade(cfg, "d2a5b9c7e1f4")  # idempotent
    command.downgrade(cfg, "c1f4a8d2e6b3")
    assert cols() == before
    command.upgrade(cfg, "head")
    assert {"entry_fee_quote", "exit_fee_quote"} <= cols()


def test_the_head_is_the_fee_revision():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert script.get_heads() == ["e3b6c8a1d5f2"] and script.get_revision("d2a5b9c7e1f4").down_revision == "c1f4a8d2e6b3"  # (the head moved on with the daily loss limit)


def test_no_trade_history_read_happens_before_the_protective_stop_on_a_futures_entry(db, monkeypatch):
    """Protection first: nothing for reporting may sit between the fill and the stop (a slow trade-history read would delay the stop)."""
    st = make_settings(max_notional_multiplier=100.0, protection_precheck_mode="off")
    client = futures_client()
    install(monkeypatch, client)
    aid = new_account(st, market_type="futures")
    run_cycle(aid, st)
    assert the_trade(aid).status == "open"
    names = [c[0] for c in client.calls]
    orders = [i for i, c in enumerate(client.calls) if c[0] == "create_order"]
    assert len(orders) >= 2, client.calls  # the entry and the protective stop
    stop = next(i for i in orders[1:] if "stopLossPrice" in client.calls[i][6])  # the protective stop carries stopLossPrice
    assert "fetch_my_trades" not in names[: stop + 1]
    assert "fetch_my_trades" not in names  # and none at all while the trade is open


def test_the_exit_paths_do_not_read_trade_history_either(db, monkeypatch):
    """Only the final record (_finalize) reads the account's trades for fees; _exit_fills, used while a close is in progress, must not."""
    import inspect

    from mba_bot import lifecycle

    src = inspect.getsource(lifecycle._exit_fills)
    assert "want_fee=False" in src
    assert "fetch_my_trades" not in inspect.getsource(lifecycle._after_entry) and "_fee_from_fills" not in inspect.getsource(lifecycle._after_entry)
