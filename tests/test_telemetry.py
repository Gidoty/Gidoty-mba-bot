"""Phase 0 research telemetry: entry features, per-trade context and the decision log.

What is pinned here:
* OFF (the default) means no telemetry rows and byte-for-byte the same trade as ON.
* ON records what cannot be reconstructed later (signal, sizing inputs, features) and
  why an entry was taken or refused, without secrets, without idle-poll noise, once per
  candle, and bounded per day.
* A telemetry failure can never stop or alter a trade.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import math
from datetime import datetime, timedelta, timezone

import pytest
from helpers_live import *  # noqa: F401,F403

from mba_bot import scheduler, telemetry, trader
from mba_bot.config import SUPPORTED_PAIRS
from mba_bot.crypto_utils import encrypt
from mba_bot.db import init_db, session_scope
from mba_bot.features import adx, compute_features, efficiency_ratio
from mba_bot.models import ApiCredential, Customer, DecisionLog, Trade, TradeTelemetry, TradingAccount
from mba_bot.strategy import Candle, candles_from_ohlcv


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")
    telemetry.reset_for_tests()
    yield
    scheduler._account_locks.clear()
    scheduler._stragglers.clear()
    telemetry.reset_for_tests()


def _rows(model):
    with session_scope() as session:
        return session.query(model).all()


def _trade_shape(trade):
    return (trade.side, trade.pair, trade.entry_price, trade.quantity, trade.stop_loss_price, trade.status, trade.protection_status)


# ------------------------------------------------------------------ features


def _trend(n=120, step=1.0, start=100.0):
    return [Candle(i * 3_600_000, start + i * step, start + i * step + 0.5, start + i * step - 0.5, start + i * step, 10.0) for i in range(n)]


def _chop(n=120):
    out = []
    for i in range(n):
        c = 100.0 + (1.0 if i % 2 else -1.0)
        out.append(Candle(i * 3_600_000, c, c + 0.5, c - 0.5, c, 10.0))
    return out


def test_efficiency_ratio_separates_a_straight_line_from_chop():
    assert efficiency_ratio([c.close for c in _trend()], 20) == pytest.approx(1.0)
    assert efficiency_ratio([c.close for c in _chop()], 20) < 0.1
    assert efficiency_ratio([1.0, 2.0], 20) is None
    assert efficiency_ratio([5.0] * 30, 20) == 0.0


def test_adx_is_high_in_a_trend_and_low_in_chop_and_none_when_short():
    t, c = _trend(), _chop()
    adx_t = adx([x.high for x in t], [x.low for x in t], [x.close for x in t])[-1]
    adx_c = adx([x.high for x in c], [x.low for x in c], [x.close for x in c])[-1]
    assert adx_t is not None and adx_c is not None and adx_t > 40 > adx_c
    assert adx([1.0] * 5, [1.0] * 5, [1.0] * 5) == [None] * 5


def test_features_are_finite_json_safe_and_never_raise_on_short_or_degenerate_input():
    kw = dict(ema_fast_period=20, ema_slow_period=50, atr_period=14, atr_rolling_window=50)
    for candles in ([], _trend(2), _trend(30), _trend(200), _chop(200), [Candle(0, 1.0, 1.0, 1.0, 1.0, 0.0)] * 80):
        feats = compute_features(candles, **kw, now_ms=10**12)
        json.dumps(feats, allow_nan=False)  # no NaN / inf
        for v in feats.values():
            assert v is None or math.isfinite(v)
    full = compute_features(_trend(200), **kw)
    for key in ("atr", "atr_pct", "ema_gap_atr", "adx14", "efficiency_ratio20", "ret_24", "realized_vol_24", "atr_ratio"):
        assert full.get(key) is not None, key


def test_feature_bar_progress_says_how_much_of_the_last_candle_has_formed():
    candles = _trend(120)
    last = candles[-1].timestamp
    f = compute_features(candles, ema_fast_period=20, ema_slow_period=50, atr_period=14, atr_rolling_window=50, now_ms=last + 900_000)
    assert f["last_bar_progress"] == pytest.approx(0.25)
    f = compute_features(candles, ema_fast_period=20, ema_slow_period=50, atr_period=14, atr_rolling_window=50, now_ms=last + 9_000_000)
    assert f["last_bar_progress"] == 1.0


# ------------------------------------------------------------------ OFF = nothing, same trade


def _live_futures_cycle(monkeypatch, settings, **client_kw):
    client = futures_client(**client_kw)
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures", pair="BTC/USDT")
    run_cycle(account_id, settings)
    return client, account_id


def test_off_by_default_writes_no_telemetry_and_on_changes_nothing_about_the_trade(db, monkeypatch):
    assert make_settings().trade_telemetry is False
    off = make_settings(max_notional_multiplier=100.0)
    _, acct_off = _live_futures_cycle(monkeypatch, off)
    shape_off = _trade_shape(the_trade(acct_off))
    assert _rows(TradeTelemetry) == [] and _rows(DecisionLog) == []

    on = dataclasses.replace(off, trade_telemetry=True)
    client = futures_client()
    install(monkeypatch, client)
    acct_on = new_account(on, email="second@example.com", market_type="futures", pair="BTC/USDT")
    run_cycle(acct_on, on)
    assert _trade_shape(the_trade(acct_on)) == shape_off, "telemetry must not change the trade"
    assert len(_rows(TradeTelemetry)) == 1


# ------------------------------------------------------------------ ON: live futures long


def test_live_entry_records_context_and_decisions(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0, trade_telemetry=True)
    client, account_id = _live_futures_cycle(monkeypatch, settings)
    trade = the_trade(account_id)
    assert trade.status == "open"

    (row,) = _rows(TradeTelemetry)
    assert row.trade_id == trade.id
    assert row.strategy_id == telemetry.STRATEGY_ID and len(row.strategy_version) == 12
    assert row.mode == "live" and row.market_type == "futures" and row.pair == "BTC/USDT"
    assert row.signal_type == "bullish_crossover" and row.signal_price == ENTRY_PRICE
    assert row.atr and row.atr > 0 and row.atr_pct and row.stop_price == pytest.approx(trade.stop_loss_price, rel=0.05)
    assert row.effective_capital == pytest.approx(1000.0)
    assert row.planned_risk_amount == pytest.approx(trade.quantity * abs(trade.entry_price - trade.stop_loss_price))
    assert row.planned_quantity == trade.quantity and row.account_leverage == 1.0
    assert row.auto_rank is None
    feats = json.loads(row.features_json)
    assert feats["atr"] and "ema_gap_atr" in feats and feats["feature_version"] == 1
    assert json.loads(row.strategy_params_json)["ema_fast_period"] == settings.ema_fast_period

    log = {(d.stage, d.outcome, d.reason_code) for d in _rows(DecisionLog)}
    assert ("signal", "accepted", "signal_actionable") in log
    assert ("entry", "accepted", "entry_submitted") in log
    entry = next(d for d in _rows(DecisionLog) if d.reason_code == "entry_submitted")
    assert entry.mode == "live" and entry.pair == "BTC/USDT" and entry.price == ENTRY_PRICE


def test_slippage_is_measurable_from_signal_price_versus_the_recorded_fill(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0, trade_telemetry=True)
    _, account_id = _live_futures_cycle(monkeypatch, settings)
    trade, (row,) = the_trade(account_id), _rows(TradeTelemetry)
    slippage_bps = (trade.entry_price - row.signal_price) / row.signal_price * 1e4
    assert math.isfinite(slippage_bps)  # the fake fills at the signal price; real fills differ and are now visible


# ------------------------------------------------------------------ ON: paper


def _paper_account(settings, email="paper@example.com") -> str:
    with session_scope() as session:
        customer = Customer(email=email, password_hash="x", has_paid=False)
        session.add(customer)
        session.flush()
        now = datetime.now(timezone.utc)
        account = TradingAccount(
            customer_id=customer.id, bot_code="MBA-PAPER1", pair="BTC/USDT", market_type="spot",
            notification_channel="telegram", notification_target="555000111", trial_ends_at=now + timedelta(days=7),
        )
        session.add(account)
        session.flush()
        return account.id


def test_paper_entry_records_the_same_context(db, monkeypatch):
    settings = make_settings(trade_telemetry=True, testnet_spot_api_key="k", testnet_spot_api_secret="s")
    client = spot_client()
    install(monkeypatch, client)
    account_id = _paper_account(settings)
    run_cycle(account_id, settings)
    trade = the_trade(account_id)
    assert trade.is_paper and trade.status == "open"
    (row,) = _rows(TradeTelemetry)
    assert row.trade_id == trade.id and row.mode == "paper" and row.signal_price == ENTRY_PRICE
    assert row.effective_capital == 10_000.0 and row.planned_quantity == trade.quantity
    assert ("entry", "accepted", "entry_paper") in {(d.stage, d.outcome, d.reason_code) for d in _rows(DecisionLog)}


def test_spot_bearish_crossover_is_logged_as_skipped_not_silently_dropped(db, monkeypatch):
    settings = make_settings(trade_telemetry=True, testnet_spot_api_key="k", testnet_spot_api_secret="s")
    client = spot_client(ohlcv=BEARISH_OHLCV, price=BEAR_PRICE)
    install(monkeypatch, client)
    account_id = _paper_account(settings)
    run_cycle(account_id, settings)
    assert the_trade(account_id) is None
    assert ("signal", "skipped", "spot_long_only") in {(d.stage, d.outcome, d.reason_code) for d in _rows(DecisionLog)}


# ------------------------------------------------------------------ rejected opportunities


def test_a_refused_entry_is_logged_with_its_reason_and_numbers(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0, trade_telemetry=True)
    client = futures_client(min_notional=10_000_000.0)  # no sized quantity can satisfy this
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures", pair="BTC/USDT")
    run_cycle(account_id, settings)
    assert the_trade(account_id) is None and _rows(TradeTelemetry) == []
    rejected = [d for d in _rows(DecisionLog) if d.outcome == "rejected"]
    assert [d.reason_code for d in rejected] == ["below_exchange_minimum_or_filters"]
    detail = json.loads(rejected[0].detail_json)
    assert detail["wanted_notional"] > 0 and detail["capital"] == 1000.0


def test_idle_polls_write_nothing(db, monkeypatch):
    settings = make_settings(trade_telemetry=True, max_notional_multiplier=100.0)
    client = futures_client(ohlcv=FLAT_OHLCV, price=100.0)
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures", pair="BTC/USDT")
    for _ in range(6):
        run_cycle(account_id, settings)
    assert _rows(DecisionLog) == [] and _rows(TradeTelemetry) == []


def test_the_same_refusal_on_the_same_candle_is_written_once(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0, trade_telemetry=True)
    client = futures_client(min_notional=10_000_000.0)
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures", pair="BTC/USDT")
    for _ in range(5):  # five 5-minute polls inside the same hourly candle
        run_cycle(account_id, settings)
    assert len([d for d in _rows(DecisionLog) if d.outcome == "rejected"]) == 1
    assert len([d for d in _rows(DecisionLog) if d.reason_code == "signal_actionable"]) == 1


def test_log_is_capped_per_account_per_day(db):
    settings = make_settings(trade_telemetry=True)
    account = type("A", (), {"id": "acct-1", "pair": "BTC/USDT", "market_type": "futures"})()
    with session_scope() as session:
        for i in range(telemetry._DAILY_CAP_PER_ACCOUNT + 150):
            telemetry.log_decision(
                session, settings, account, stage="signal", outcome="rejected", reason="x", is_paper=True, candle_ts=i,
                now=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
    assert len(_rows(DecisionLog)) == telemetry._DAILY_CAP_PER_ACCOUNT


# ------------------------------------------------------------------ never harms a trade


def test_a_telemetry_failure_cannot_stop_or_change_a_trade(db, monkeypatch):
    base = make_settings(max_notional_multiplier=100.0)
    _, acct_ref = _live_futures_cycle(monkeypatch, base)
    reference = _trade_shape(the_trade(acct_ref))

    def boom(*a, **k):
        raise RuntimeError("telemetry exploded")

    monkeypatch.setattr(telemetry, "compute_features", boom)
    monkeypatch.setattr(telemetry, "DecisionLog", boom)
    monkeypatch.setattr(telemetry, "TradeTelemetry", boom)
    on = dataclasses.replace(base, trade_telemetry=True)
    client = futures_client()
    install(monkeypatch, client)
    acct = new_account(on, email="boom@example.com", market_type="futures", pair="BTC/USDT")
    run_cycle(acct, on)
    assert _trade_shape(the_trade(acct)) == reference
    assert len(_rows(TradeTelemetry)) == 0


# ------------------------------------------------------------------ Auto scan observation


def test_auto_scan_collector_observes_without_changing_the_result(db, monkeypatch):
    settings = make_settings(futures_pair_expansion=True)
    client = futures_client(leverage_by_symbol={p: 1.0 for p in SUPPORTED_PAIRS})
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures")

    async def go(scan_out):
        with session_scope(autoflush=False) as session:
            account = session.get(TradingAccount, account_id)
            return await trader._scan_auto_pair(client, account, settings, scan_out=scan_out)

    plain = asyncio.run(go(None))
    out: dict = {}
    observed = asyncio.run(go(out))
    assert (plain[0], plain[2]) == (observed[0], observed[2])
    assert out["ranked"] and out["ranked"][0][0] == observed[0]


# ------------------------------------------------------------------ hygiene


def test_no_secret_shaped_content_is_stored(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0, trade_telemetry=True)
    _live_futures_cycle(monkeypatch, settings)
    blob = " ".join(
        str(getattr(r, c.name)) for model in (TradeTelemetry, DecisionLog) for r in _rows(model) for c in model.__table__.columns
    ).lower()
    for needle in ("secret", "apikey", "api_key", "synthetic-", "password", "token"):
        assert needle not in blob
    columns = {c.name for m in (TradeTelemetry, DecisionLog) for c in m.__table__.columns}
    assert not {c for c in columns if "key" in c or "secret" in c or "token" in c}


def test_strategy_version_is_deterministic_and_tracks_parameters():
    a = make_settings()
    assert telemetry.strategy_version(a) == telemetry.strategy_version(dataclasses.replace(a))
    b = dataclasses.replace(a, ema_fast_period=a.ema_fast_period + 1)
    assert telemetry.strategy_version(a) != telemetry.strategy_version(b)


# ------------------------------------------------------------------ the scheduler's lock rule still holds with telemetry on

from test_scheduler_concurrency import PAPER_KEYS, WriteLockProbe, _add_latency, _use  # noqa: E402


@pytest.fixture
def probe(db):
    p = WriteLockProbe()
    yield p
    p.close()


def test_no_write_lock_is_held_across_a_suspension_with_telemetry_on_through_live_entries(probe, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0, trade_telemetry=True)
    notifier = FakeNotifier()
    real_send = notifier.send

    async def send(target, message):
        probe.check("notifier")
        await real_send(target, message)

    notifier.send = send
    clients = {e: _add_latency(futures_client(), 0.01, hooks=[probe.check]) for e in ("a@example.com", "b@example.com")}
    _use(monkeypatch, clients["a@example.com"], by_email=clients, notifier=notifier)
    ids = {e: new_account(settings, email=e, market_type="futures") for e in clients}

    asyncio.run(scheduler.run_once(settings))

    assert probe.violations == []
    for email, account_id in ids.items():
        trade = the_trade(account_id)
        assert trade.status == "open" and trade.protection_status == "protected", email
    assert len(_rows(TradeTelemetry)) == 2
    assert {d.account_id for d in _rows(DecisionLog)} == set(ids.values())


def test_no_write_lock_is_held_across_a_suspension_with_telemetry_on_for_paper_and_refused_entries(probe, monkeypatch):
    settings = make_settings(trade_telemetry=True, max_notional_multiplier=100.0, **PAPER_KEYS)
    notifier = FakeNotifier()
    real_send = notifier.send

    async def send(target, message):
        probe.check("notifier")
        await real_send(target, message)

    notifier.send = send
    paper = _add_latency(spot_client(), 0.01, hooks=[probe.check])
    refused = _add_latency(futures_client(min_notional=10_000_000.0), 0.01, hooks=[probe.check])
    _use(monkeypatch, paper, by_email={"refused@example.com": refused}, notifier=notifier)
    paper_id = _paper_account(settings, "paper@example.com")
    refused_id = new_account(settings, email="refused@example.com", market_type="futures")

    asyncio.run(scheduler.run_once(settings))

    assert probe.violations == []
    assert the_trade(paper_id).is_paper and the_trade(refused_id) is None
    reasons = {d.reason_code for d in _rows(DecisionLog)}
    assert {"entry_paper", "below_exchange_minimum_or_filters"} <= reasons


def test_adding_telemetry_rows_never_flushes_the_session(db):
    from sqlalchemy import event

    settings = make_settings(trade_telemetry=True)
    account = type("A", (), {"id": "acct-1", "pair": "BTC/USDT", "market_type": "futures", "risk_pct": 1.0, "pair_selection_mode": "manual"})()
    flushes = []
    with session_scope(autoflush=False) as session:
        event.listen(session, "before_flush", lambda *a: flushes.append(1))
        telemetry.log_decision(session, settings, account, stage="entry", outcome="rejected", reason="x", is_paper=False, candle_ts=1)
        fake_trade = type("T", (), {"id": "t-1", "pair": "BTC/USDT"})()
        sig = type("S", (), {"signal": type("E", (), {"value": "bullish_crossover"})(), "reason": "r", "atr": 1.0, "atr_pct": 0.01, "fast_ema": 1.0, "slow_ema": 1.0})()
        ctx = telemetry.EntryContext(signal=sig, price=100.0, candle_ts=1, timeframe="1h", features={"atr": 1.0})
        telemetry.record_entry(session, settings, account, fake_trade, ctx=ctx, is_paper=False, effective_capital=1000.0, risk_amount=10.0, quantity=1.0, stop_price=95.0)
        assert flushes == [], "queued in memory only; the caller's own commit writes them"
        assert len(session.new) == 2
        session.rollback()


# ------------------------------------------------------------------ containment of telemetry failures


def test_flag_on_without_the_migration_disables_telemetry_and_trading_carries_on(db, monkeypatch, caplog):
    from sqlalchemy import text

    import mba_bot.db as dbmod

    with dbmod._engine.begin() as conn:
        conn.execute(text("drop table trade_telemetry"))
        conn.execute(text("drop table decision_log"))
    telemetry.reset_for_tests()
    settings = make_settings(max_notional_multiplier=100.0, trade_telemetry=True)
    _, account_id = _live_futures_cycle(monkeypatch, settings)
    assert the_trade(account_id).status == "open" and the_trade(account_id).protection_status == "protected"
    assert caplog.text.count("telemetry tables are missing") == 1
    assert telemetry.enabled(settings) is False


def _poison_the_telemetry_row(monkeypatch):
    # An invalid row: strategy_version is NOT NULL, so the INSERT itself fails.
    monkeypatch.setattr(telemetry, "strategy_version", lambda settings: None)


def test_a_telemetry_row_that_cannot_be_written_never_costs_the_live_trade(db, monkeypatch):
    _poison_the_telemetry_row(monkeypatch)
    settings = make_settings(max_notional_multiplier=100.0, trade_telemetry=True)
    client, account_id = _live_futures_cycle(monkeypatch, settings)
    trade = the_trade(account_id)
    assert trade.status == "open" and trade.protection_status == "protected"
    assert len(client.orders_by_role("entry")) == 1
    assert _rows(TradeTelemetry) == []
    with session_scope() as session:
        acct = session.get(TradingAccount, account_id)
        assert acct.last_error is None and acct.last_signal == "bullish_crossover" and acct.last_scanned_at is not None, "the rest of the cycle's state is still written"
    assert "signal_actionable" in {d.reason_code for d in _rows(DecisionLog)}, "only the failing rows were rolled back"


def test_a_telemetry_row_that_cannot_be_written_never_costs_the_paper_trade(db, monkeypatch):
    _poison_the_telemetry_row(monkeypatch)
    settings = make_settings(trade_telemetry=True, testnet_spot_api_key="k", testnet_spot_api_secret="s")
    install(monkeypatch, spot_client())
    account_id = _paper_account(settings)
    run_cycle(account_id, settings)
    trade = the_trade(account_id)
    assert trade is not None and trade.is_paper and trade.status == "open"
    assert _rows(TradeTelemetry) == []


# ------------------------------------------------------------------ Auto Select context on a real entry


def test_an_auto_entry_records_its_rank_score_and_the_scan_decisions(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0, trade_telemetry=True)
    client = futures_client(leverage_by_symbol={p: 1.0 for p in SUPPORTED_PAIRS})
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures")
    with session_scope() as session:
        session.get(TradingAccount, account_id).pair_selection_mode = "auto"
    run_cycle(account_id, settings)
    trade = the_trade(account_id)
    assert trade is not None and trade.status == "open"
    (row,) = _rows(TradeTelemetry)
    assert row.pair_selection_mode == "auto" and row.auto_rank == 1 and row.auto_candidates == len(SUPPORTED_PAIRS)
    assert row.auto_score is not None and row.auto_score >= 0
    picks = [d for d in _rows(DecisionLog) if d.reason_code == "auto_pick"]
    assert len(picks) == 1 and picks[0].pair == trade.pair
    assert len(json.loads(picks[0].detail_json)["ranked"]) == len(SUPPORTED_PAIRS)
