"""B1b: the daily-loss equity sample and pause run on EVERY normal end of a live cycle, whichever `return` the cycle took (idle auto scan, withdrawal block, no market
data, multi path, unhealthy trade, open-trade path, plain end), at most one balance read per account per cycle, and never after an exception. Also the read-only
`equity_probe` CLI.

PROVES (fake exchange, scratch SQLite, frozen clock): one sample per path; a failed read logs the warning and the cycle goes on; the pause is written by the tick on an
idle cycle and while a trade is open without touching the trade; the probe prints only an id, a market and a rounded number or a fixed failure class.

DOES NOT PROVE: anything about Binance, or that ccxt's Futures reply carries `totalMarginBalance` in production (checked only against ccxt 4.5.88's source and its documented
sample reply)."""
from __future__ import annotations

import asyncio
import dataclasses
import re
from datetime import timedelta

import pytest
from helpers_live import BEARISH_OHLCV, BULLISH_OHLCV, FLAT_OHLCV, FakeNotifier, all_trades, install, make_settings, new_account, open_protected, run_cycle
from test_account_daily_loss import NOON, live_settings, reasons, rig, rows

from mba_bot import exchange, trader
from mba_bot.db import init_db, session_scope
from mba_bot.models import AccountDayEquity, AccountLossPause, TradingAccount


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")
    return tmp_path


def fut(monkeypatch, *, auto=False, email="live@example.com", **over):
    st = live_settings(**over)
    client, clock, aid = rig(monkeypatch, st, market="futures", email=email)
    client.total_margin_balance = 1000.0
    if auto:
        with session_scope() as s:
            s.get(TradingAccount, aid).pair_selection_mode = "auto"
    return st, client, clock, aid


def samples():
    return rows(AccountDayEquity)


# ------------------------------------------------------------------ one test per return path
def test_idle_auto_scan_path_records_a_sample(db, monkeypatch):
    st, client, clock, aid = fut(monkeypatch, auto=True)
    run_cycle(aid, st)  # every auto pair is flat: _scan_auto_pair returns None and process_account returns early
    assert len(samples()) == 1 and samples()[0].start_equity == 1000.0 and all_trades(aid) == []


def test_withdrawal_block_path_records_a_sample(db, monkeypatch):
    st, client, clock, aid = fut(monkeypatch)
    with session_scope() as s:
        s.get(TradingAccount, aid).withdrawal_blocked_at = NOON - timedelta(days=1)
    run_cycle(aid, st)
    assert len(samples()) == 1 and all_trades(aid) == []


def test_no_market_data_path_records_a_sample(db, monkeypatch):
    st, client, clock, aid = fut(monkeypatch)
    client._ohlcv = []
    run_cycle(aid, st)
    assert len(samples()) == 1


def test_multi_path_records_a_sample(db, monkeypatch):
    st, client, clock, aid = fut(monkeypatch)
    called = []

    async def stub(*a, **k):
        called.append(1)

    monkeypatch.setattr(trader, "_use_multi", lambda *a, **k: True)
    monkeypatch.setattr(trader, "_process_multi", stub)
    run_cycle(aid, st)
    assert called and len(samples()) == 1


def test_plain_end_of_cycle_records_a_sample(db, monkeypatch):
    st, client, clock, aid = fut(monkeypatch)  # a fixed pair, flat market: falls off the end of the function
    run_cycle(aid, st)
    assert len(samples()) == 1


def _open_futures(monkeypatch):
    client, settings, aid = open_protected(monkeypatch, market="futures")
    client.total_margin_balance = 1000.0
    return client, dataclasses.replace(settings, account_daily_loss_pct=4.0, trade_telemetry=True), aid


def test_unhealthy_open_trade_path_records_a_sample(db, monkeypatch):
    client, st, aid = _open_futures(monkeypatch)
    real = trader._manage_live_trade

    async def unhealthy(*a, **k):
        trade, ctx = await real(*a, **k)
        trade.protection_status = "pending"  # not protected: the cycle returns before any market data
        return trade, ctx

    monkeypatch.setattr(trader, "_manage_live_trade", unhealthy)
    run_cycle(aid, st)
    assert len(samples()) == 1


def test_open_trade_without_market_data_path_records_a_sample(db, monkeypatch):
    client, st, aid = _open_futures(monkeypatch)
    client._ohlcv = []
    run_cycle(aid, st)
    assert len(samples()) == 1


def test_open_trade_monitor_path_records_a_sample_futures(db, monkeypatch):
    client, st, aid = _open_futures(monkeypatch)
    run_cycle(aid, st)
    assert len(samples()) == 1 and all_trades(aid)[0].status == "open"


def test_open_trade_monitor_path_values_a_spot_position_at_mark(db, monkeypatch):
    settings = live_settings()
    client, clock, aid = rig(monkeypatch, settings, at=NOON)
    client._ohlcv = BULLISH_OHLCV
    run_cycle(aid, settings)  # entry cycle: the gate's reading is shared with the tick (see the read-count test)
    trade = all_trades(aid)[0]
    clock.instant = NOON + timedelta(minutes=5)
    run_cycle(aid, settings)
    expected = client.free["USDT"] + client.used.get("USDT", 0.0) + trade.quantity * client.price
    row = samples()[0]
    assert abs(row.last_equity - expected) < 1e-6 and row.start_equity == 1000.0


def test_no_sample_after_an_exception(db, monkeypatch):
    st, client, clock, aid = fut(monkeypatch)

    async def boom(*a, **k):
        raise RuntimeError("exchange down")

    client.fetch_ohlcv = boom
    run_cycle(aid, st, swallow=True)
    assert samples() == [] and not [c for c in client.calls if c[0] == "fetch_balance"]


def test_paper_and_unlinked_and_no_credential_accounts_take_no_sample(db, monkeypatch):
    st, client, clock, aid = fut(monkeypatch)
    with session_scope() as s:
        s.get(TradingAccount, aid).notification_target = None  # returns before any client exists
    run_cycle(aid, st)
    assert samples() == []


# ------------------------------------------------------------------ reads, failure, pause
def test_at_most_one_equity_read_per_cycle_idle_and_entry(db, monkeypatch):
    calls = []
    real = exchange.fetch_account_equity_ex

    async def spy(*a, **k):
        calls.append(1)
        return await real(*a, **k)

    monkeypatch.setattr(exchange, "fetch_account_equity_ex", spy)
    st, client, clock, aid = fut(monkeypatch, auto=True)
    run_cycle(aid, st)
    assert len(calls) == 1  # idle cycle: the tick alone
    # an entry cycle: the entry gate reads, the tick at the end reuses that reading
    settings = live_settings()
    client2, clock2, aid2 = rig(monkeypatch, settings, email="entry@example.com")
    client2._ohlcv = BULLISH_OHLCV
    calls.clear()
    run_cycle(aid2, settings)
    assert all_trades(aid2) and len(calls) == 1


def test_a_failed_read_logs_the_warning_and_the_cycle_carries_on(db, monkeypatch, caplog):
    st, client, clock, aid = fut(monkeypatch)

    async def broken():
        raise RuntimeError("balance endpoint down")

    client.fetch_balance = broken
    run_cycle(aid, st)  # must not raise
    assert samples() == []
    assert any("could not record an equity sample" in r.getMessage() for r in caplog.records)
    from mba_bot.db import session_scope as ss

    with ss() as s:
        assert s.get(TradingAccount, aid).last_scanned_at is not None  # the cycle ran


def test_the_warning_is_not_repeated_every_cycle(db, monkeypatch, caplog):
    st, client, clock, aid = fut(monkeypatch)

    async def broken():
        raise RuntimeError("down")

    client.fetch_balance = broken
    for i in range(4):
        clock.instant = NOON + timedelta(minutes=5 * i)
        run_cycle(aid, st)
    assert len([r for r in caplog.records if "could not record an equity sample" in r.getMessage()]) == 1


def test_idle_auto_cycle_writes_the_pause_when_equity_is_down_four_percent(db, monkeypatch):
    st, client, clock, aid = fut(monkeypatch, auto=True)
    run_cycle(aid, st)  # day start 1000
    client.total_margin_balance = 960.0  # down exactly 4.0%
    clock.instant = NOON + timedelta(minutes=5)
    n = FakeNotifier()
    run_cycle(aid, st, n)  # still an idle auto cycle: no signal tries to enter
    pauses = rows(AccountLossPause)
    assert len(pauses) == 1 and abs(pauses[0].loss_pct - 4.0) < 1e-6 and all_trades(aid) == [] and client.orders == {}
    assert len([t for t in n.texts if "paused for the rest of today" in t]) == 1
    assert "account_daily_loss_pause" in reasons()
    clock.instant += timedelta(minutes=5)
    run_cycle(aid, st, n)
    assert len(rows(AccountLossPause)) == 1 and len([t for t in n.texts if "paused for the rest of today" in t]) == 1  # one alert, one row
    # the pause then blocks a real signal the same day, and ends at Lagos midnight
    client._ohlcv = BULLISH_OHLCV
    client.total_margin_balance = 1000.0
    clock.instant = NOON + timedelta(minutes=30)
    run_cycle(aid, st, n)
    assert all_trades(aid) == []


def test_the_tick_writes_the_pause_while_a_trade_is_open_and_does_not_touch_it(db, monkeypatch):
    settings = live_settings()
    client, clock, aid = rig(monkeypatch, settings, at=NOON)
    client._ohlcv = BULLISH_OHLCV
    run_cycle(aid, settings)
    assert all_trades(aid)[0].status == "open"
    orders_before = {k: dict(v) for k, v in client.orders.items()}
    client.free["USDT"] -= 100.0  # roughly a 10% fall in equity with the trade open
    clock.instant = NOON + timedelta(minutes=5)
    run_cycle(aid, settings)
    assert len(rows(AccountLossPause)) == 1
    trade = all_trades(aid)[0]
    assert trade.status == "open" and trade.protection_status == "protected"  # untouched
    assert {k: dict(v) for k, v in client.orders.items()} == orders_before  # no order placed, cancelled or changed


def test_a_paused_day_keeps_sampling_so_the_next_day_starts_from_it(db, monkeypatch):
    st, client, clock, aid = fut(monkeypatch, auto=True)
    run_cycle(aid, st)
    client.total_margin_balance = 900.0
    clock.instant = NOON + timedelta(minutes=5)
    run_cycle(aid, st)
    assert len(rows(AccountLossPause)) == 1
    client.total_margin_balance = 880.0  # a later reading, with the pause already in force
    clock.instant = NOON + timedelta(minutes=10)
    run_cycle(aid, st)
    row = samples()[0]
    assert row.last_equity == 880.0 and row.start_equity == 1000.0  # sampled during the pause, start unchanged


def test_zero_or_missing_margin_balance_is_unreadable_not_a_day_start(db, monkeypatch):
    st, client, clock, aid = fut(monkeypatch)
    for value in (0.0, None, float("nan"), -5.0):
        client.total_margin_balance = value
        run_cycle(aid, st)
    assert samples() == []


# ------------------------------------------------------------------ the Futures reply shape
def test_futures_equity_uses_ccxt_info_total_margin_balance_only():
    class C:
        def __init__(self, reply):
            self.reply = reply

        async def fetch_balance(self):
            return self.reply

    # the shape ccxt 4.5.88 returns for Binance USD-M: {'info': <raw account reply with decimal strings>, 'USDT': {...}}
    good = {"info": {"totalWalletBalance": "990.50", "totalUnrealizedProfit": "9.50", "totalMarginBalance": "1000.00000000"}, "USDT": {"free": 1.0, "used": 2.0, "total": 3.0}}
    run = lambda reply: asyncio.run(exchange.fetch_account_equity_ex(C(reply), "BTC/USDT", "futures"))
    assert run(good) == (1000.0, "ok")
    assert run({"info": {"totalWalletBalance": "990.5"}, "USDT": {"free": 5.0}}) == (None, "margin_balance_missing")  # never rebuilt from other fields
    assert run({"info": {"totalMarginBalance": "0.00000000"}}) == (None, "equity_not_positive")
    assert run({"info": {"totalMarginBalance": "abc"}}) == (None, "balance_malformed")
    assert run({"info": {"totalMarginBalance": "nan"}}) == (None, "balance_malformed")
    assert run({"info": "x"}) == (None, "margin_balance_missing") and run(["x"]) == (None, "balance_malformed")


# ------------------------------------------------------------------ the read-only probe
SECRET_TEXT = re.compile(r"synthetic|secret|key|USDT|free|used|@", re.I)


def _probe_env(monkeypatch, tmp_path, settings):
    monkeypatch.setenv("ENCRYPTION_KEY", settings.encryption_key)
    return f"sqlite:///{tmp_path}/test.db"


def test_probe_prints_only_id_market_and_rounded_equity(db, monkeypatch):
    from mba_bot import equity_probe

    st = live_settings()
    client = exchange_client = None
    from helpers_live import futures_client, spot_client

    spot = spot_client(free_balance={"USDT": 1234.5678}, ohlcv=FLAT_OHLCV)
    install(monkeypatch, spot)
    sid = new_account(st, email="s@example.com")
    url = _probe_env(monkeypatch, db, st)
    lines, code = asyncio.run(equity_probe.run(url))
    assert code == 0 and len(lines) == 1
    assert re.fullmatch(rf"{sid} spot equity=1234\.57", lines[0]), lines
    assert not SECRET_TEXT.search(lines[0].replace(sid, "").replace(" spot", ""))
    allowed = {"fetch_balance", "fetch_ticker", "load_markets"}
    assert {c[0] for c in spot.calls} <= allowed and spot.orders == {}  # reads only, no order


def test_probe_futures_and_failure_classes_and_no_exception_text(db, monkeypatch):
    from helpers_live import futures_client

    from mba_bot import equity_probe

    st = live_settings()
    fc = futures_client(ohlcv=FLAT_OHLCV)
    fc.total_margin_balance = 1000.0
    install(monkeypatch, fc)
    fid = new_account(st, market_type="futures", email="f@example.com")
    url = _probe_env(monkeypatch, db, st)
    lines, code = asyncio.run(equity_probe.run(url))
    assert code == 0 and lines == [f"{fid} futures equity=1000.00"]
    fc.total_margin_balance = None  # the field is missing
    lines, code = asyncio.run(equity_probe.run(url))
    assert code == 2 and lines == [f"{fid} futures FAILED margin_balance_missing"]

    async def leaky():
        raise RuntimeError("https://fapi.binance.com/fapi/v3/account?signature=abcdef&apiKey=SECRETKEY123")

    fc.fetch_balance = leaky
    lines, code = asyncio.run(equity_probe.run(url))
    assert lines == [f"{fid} futures FAILED balance_call_failed"] and "SECRETKEY123" not in lines[0] and "signature" not in lines[0]


def test_probe_skips_paper_accounts_and_reports_none_live(db, monkeypatch):
    from mba_bot import equity_probe

    st = live_settings()
    new_account(st)
    with session_scope() as s:
        s.query(TradingAccount).one().bot_started_at = None  # paper
    url = _probe_env(monkeypatch, db, st)
    assert asyncio.run(equity_probe.run(url)) == ([], 3)


def test_probe_never_creates_or_writes_the_database(db, monkeypatch, tmp_path):
    from sqlalchemy import text

    from mba_bot import equity_probe
    from mba_bot.restart_gate import _engine

    with pytest.raises(FileNotFoundError):
        asyncio.run(equity_probe.run(f"sqlite:///{tmp_path}/missing.db"))
    assert not (tmp_path / "missing.db").exists()
    engine = _engine(f"sqlite:///{db}/test.db")
    with pytest.raises(Exception, match="readonly|read-only"):
        with engine.begin() as c:
            c.execute(text("DELETE FROM trading_accounts"))
    engine.dispose()


def test_probe_main_prints_only_a_class_when_it_cannot_start(monkeypatch, capsys, tmp_path):
    from mba_bot import equity_probe

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/nope.db")
    assert equity_probe.main([]) == 2
    err = capsys.readouterr().err
    assert "FileNotFoundError" in err and "nope.db" not in err


def test_owner_docs_name_the_probe_and_the_tick():
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "docs" / "safety" / "account_daily_loss.md").read_text()
    assert "python -m mba_bot.equity_probe" in text and "EVERY normal cycle" in text


# ------------------------------------------------------------------ Spot position valuation and the probe's failure text
def test_a_spot_position_in_an_unknown_state_makes_the_reading_unreadable(db, monkeypatch):
    client, settings, aid = open_protected(monkeypatch, market="spot")
    for state in ("attention", "pending"):
        with session_scope(autoflush=False) as s:
            account = s.get(TradingAccount, aid)
            all_t = [t for t in s.query(trader.Trade).filter_by(account_id=aid).all()]
            all_t[0].status = state
            s.flush()
            assert asyncio.run(trader._live_equity(s, account, client)) == (None, "position_state_unknown")
            all_t[0].status = "open"


def test_a_trade_closed_earlier_in_the_cycle_is_not_counted_as_a_held_position(db, monkeypatch):
    client, settings, aid = open_protected(monkeypatch, market="spot")
    with session_scope(autoflush=False) as s:
        account = s.get(TradingAccount, aid)
        trade = s.query(trader.Trade).filter_by(account_id=aid).one()
        held, _ = asyncio.run(trader._live_equity(s, account, client))
        trade.status = "closed"  # changed in this session, not yet flushed: the valuation must still see it
        flat, _ = asyncio.run(trader._live_equity(s, account, client))
        assert abs(held - flat - trade.quantity * client.price) < 1e-6 and abs(flat - (client.free["USDT"] + client.used.get("USDT", 0.0))) < 1e-6


def test_probe_never_prints_exception_text_from_inside_the_valuation(db, monkeypatch):
    from helpers_live import spot_client

    from mba_bot import equity_probe

    st = live_settings()
    install(monkeypatch, spot_client(ohlcv=FLAT_OHLCV))
    aid = new_account(st, email="leak@example.com")

    async def leaky(*a, **k):
        raise RuntimeError("https://x/?signature=abcdef&apiKey=SECRETKEY123")

    monkeypatch.setattr(trader, "_live_equity", leaky)
    url = _probe_env(monkeypatch, db, st)
    lines, code = asyncio.run(equity_probe.run(url))
    assert lines == [f"{aid} spot FAILED unexpected_error"] and code == 2


# ------------------------------------------------------------------ paper accounts take no tick (B1b review gap)
def _no_equity_reads(monkeypatch):
    """Any equity read or tick on a paper path fails the test, so the tick condition (`not effective_is_paper`) cannot be dropped unnoticed."""
    async def boom(*a, **k):
        raise AssertionError("a paper account must not read equity or run the tick")

    monkeypatch.setattr(exchange, "fetch_account_equity_ex", boom)
    monkeypatch.setattr(trader, "_account_equity_tick", boom)


def test_a_paper_account_takes_no_tick_and_writes_no_pause(db, monkeypatch):
    st = live_settings(testnet_spot_api_key="paper-key-1234567890", testnet_spot_api_secret="paper-secret-1234567890")
    client, clock, aid = rig(monkeypatch, st, balance=1.0)  # a balance that would be a "loss" against any day start
    with session_scope() as s:
        s.get(TradingAccount, aid).bot_started_at = None  # PAPER
    _no_equity_reads(monkeypatch)
    for minutes, ohlcv in ((0, FLAT_OHLCV), (5, BULLISH_OHLCV), (10, BEARISH_OHLCV)):  # idle, an entry, an exit: every kind of paper cycle
        client._ohlcv = ohlcv
        clock.instant = NOON + timedelta(minutes=minutes)
        run_cycle(aid, st)
    assert samples() == [] and rows(AccountLossPause) == []


def test_a_live_mode_account_whose_open_trades_are_all_paper_takes_no_tick(db, monkeypatch):
    from mba_bot.models import Trade

    st = live_settings(testnet_spot_api_key="paper-key-1234567890", testnet_spot_api_secret="paper-secret-1234567890")
    client, clock, aid = rig(monkeypatch, st, balance=1.0)  # the account itself is in live mode ...
    with session_scope() as s:
        s.add(Trade(account_id=aid, side="long", is_paper=True, entry_price=100.0, quantity=0.01, stop_loss_price=95.0, status="open", pair="BTC/USDT"))  # ... but holds only a paper trade
    _no_equity_reads(monkeypatch)
    for minutes in (0, 5):
        clock.instant = NOON + timedelta(minutes=minutes)
        run_cycle(aid, st)
    assert samples() == [] and rows(AccountLossPause) == []
    with session_scope() as s:
        assert s.get(TradingAccount, aid).last_scanned_at is not None  # the cycle did run
