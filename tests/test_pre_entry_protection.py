"""Pre-entry protection check and the repeat-failure limit.

Background: an account at 100x leverage on XRP opened and immediately emergency-closed the same kind of
trade 23 times in under two hours, because the 2x ATR stop sat beyond the liquidation price and Binance
cannot hold a stop there. These tests pin: (1) the calculation, (2) that an unviable entry is refused
BEFORE any order, with a decision-log row and one alert, (3) that a viable entry is untouched, (4) that
unreliable inputs fail closed, and (5) that repeated protection failures stop further entries.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest
from helpers_live import *  # noqa: F401,F403

from mba_bot import protection_guard, scheduler, telemetry
from mba_bot.db import init_db, session_scope
from mba_bot.models import DecisionLog, Trade, TradingAccount
from mba_bot.risk import assess_stop_viability


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")
    telemetry.reset_for_tests()
    protection_guard.reset_for_tests()
    yield
    scheduler._account_locks.clear()
    scheduler._stragglers.clear()
    telemetry.reset_for_tests()
    protection_guard.reset_for_tests()


def _check(**over):
    kw = dict(
        side="long", entry_price=100.0, stop_price=98.0, quantity=1.0, leverage=10.0, effective_capital=100.0,
        margin_mode="isolated", maint_margin_rate=0.01, safety_fraction=0.75,
    )
    kw.update(over)
    return assess_stop_viability(**kw)


# ------------------------------------------------------------------ the calculation


def test_the_incident_100x_with_a_2_percent_stop_is_refused():
    r = _check(leverage=100.0, stop_price=98.0)
    assert not r.viable and r.reason == "stop_beyond_safe_liquidation_distance"
    assert r.liquidation_distance_pct == pytest.approx(0.0)  # 1/100 - 1% maintenance margin


def test_5x_with_a_1_percent_stop_is_allowed_like_the_protected_bnb_trades():
    r = _check(leverage=5.0, stop_price=99.0)
    assert r.viable and r.reason == "ok"
    assert r.liquidation_distance_pct == pytest.approx(19.0) and r.max_allowed_stop_pct == pytest.approx(14.25)


@pytest.mark.parametrize(
    "leverage, stop_pct, viable",
    [
        (20.0, 3.0, True),    # allowed = 0.75 * (5% - 1%) = 3.0% exactly: equality passes
        (20.0, 3.01, False),  # just past the boundary
        (25.0, 2.0, True),    # allowed = 0.75 * 3% = 2.25%
        (30.0, 2.0, False),   # allowed = 0.75 * 2.33% = 1.75%
        (50.0, 2.0, False),
        (10.0, 6.75, True),   # allowed = 0.75 * 9% = 6.75% exactly
    ],
)
def test_boundaries(leverage, stop_pct, viable):
    stop = 100.0 * (1 - stop_pct / 100.0)
    assert _check(leverage=leverage, stop_price=stop).viable is viable


def test_short_is_symmetric_with_long():
    assert not _check(side="short", leverage=100.0, stop_price=102.0).viable
    assert _check(side="short", leverage=5.0, stop_price=101.0).viable
    assert _check(side="short", leverage=20.0, stop_price=103.0).viable
    assert not _check(side="short", leverage=20.0, stop_price=103.01).viable


def test_leverage_so_high_that_no_liquidation_distance_remains_is_refused():
    for lev in (100.0, 125.0, 500.0):
        assert not _check(leverage=lev, stop_price=99.9).viable


def test_unknown_margin_mode_is_treated_as_isolated_the_conservative_case():
    r = _check(margin_mode=None, leverage=100.0)
    assert not r.viable and r.margin_mode_assumed == "isolated"


def test_cross_margin_uses_the_collateral_not_just_the_leverage_setting():
    # 100x set, but a cross account holding 100 against a 100-notional position is liquidated ~99% away.
    r = _check(margin_mode="cross", leverage=100.0, quantity=1.0, entry_price=100.0, effective_capital=100.0)
    assert r.viable and r.liquidation_distance_pct == pytest.approx(99.0)
    # Same leverage, collateral only just above the margin: cross cannot help, stays refused.
    assert not _check(margin_mode="cross", leverage=100.0, quantity=10.0, effective_capital=11.0).viable
    # Cross never reports a smaller distance than isolated would.
    assert _check(margin_mode="cross", leverage=5.0).liquidation_distance_pct >= _check(margin_mode="isolated", leverage=5.0).liquidation_distance_pct


@pytest.mark.parametrize(
    "over",
    [
        dict(leverage=0.0), dict(leverage=-5.0), dict(leverage=float("nan")), dict(leverage=float("inf")),
        dict(leverage=None), dict(entry_price=0.0), dict(entry_price=float("nan")), dict(stop_price=0.0),
        dict(stop_price=None), dict(quantity=0.0), dict(effective_capital=0.0),
        dict(stop_price=101.0),               # long with the stop above entry
        dict(side="short", stop_price=99.0),  # short with the stop below entry
        dict(side="sideways"), dict(maint_margin_rate=0.6), dict(maint_margin_rate=-0.1),
        dict(safety_fraction=0.0), dict(safety_fraction=1.5),
    ],
)
def test_missing_or_unreliable_inputs_fail_closed(over):
    r = _check(**over)
    assert not r.viable and r.reason == "protection_check_inputs_invalid"


def test_a_lower_assumed_maintenance_rate_is_less_conservative_so_the_default_matters():
    assert not _check(leverage=50.0, stop_price=99.0, maint_margin_rate=0.01).viable    # 0.75*1% = 0.75% < 1%
    assert _check(leverage=50.0, stop_price=99.0, maint_margin_rate=0.005).viable       # 0.75*1.5% = 1.125% >= 1%


# ------------------------------------------------------------------ in the entry path
# helpers_live's synthetic candles give a stop about 52% from entry: viable at 1x (allowed ~74%), refused at 2x (~37%).


def _settings(**over):
    base = dict(max_notional_multiplier=100.0, protection_precheck_enabled=True, trade_telemetry=True)
    base.update(over)
    return make_settings(**base)


def _alerts(notifier):
    return [t for t in notifier.texts if t.startswith("MBA Bot did not open a trade")]


def _decisions():
    with session_scope() as session:
        return session.query(DecisionLog).all()


def test_an_unviable_entry_is_refused_before_any_order_with_a_decision_row_and_one_alert(db, monkeypatch):
    settings = _settings()
    client = futures_client(leverage_by_symbol={"BTC/USDT": 2.0})
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures")

    notifier = run_cycle(account_id, settings)

    assert client.market_orders == [] and all_trades(account_id) == []
    rows = [d for d in _decisions() if d.reason_code == telemetry.R_STOP_UNVIABLE]
    assert len(rows) == 1 and rows[0].outcome == telemetry.REJECTED and rows[0].stage == telemetry.STAGE_ENTRY
    detail = rows[0].detail_json
    for key in ("leverage", "stop_distance_pct", "est_liquidation_distance_pct", "max_allowed_stop_pct", "margin_mode_assumed"):
        assert key in detail
    assert "secret" not in detail.lower() and "api_key" not in detail.lower()
    assert len(_alerts(notifier)) == 1 and "leverage" in _alerts(notifier)[0].lower()
    with session_scope() as session:
        assert "leverage" in session.get(TradingAccount, account_id).last_error.lower()

    # The same condition next round: no order, the dashboard message stays, and the customer is not alerted again.
    notifier2 = run_cycle(account_id, settings, notifier)
    assert client.market_orders == [] and len(_alerts(notifier2)) == 1


def test_a_viable_entry_is_not_touched_by_the_check(db, monkeypatch):
    settings = _settings()
    client = futures_client(leverage_by_symbol={"BTC/USDT": 1.0})
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures")

    run_cycle(account_id, settings)

    trade = the_trade(account_id)
    assert trade.status == "open" and trade.protection_status == "protected"
    assert [d for d in _decisions() if d.reason_code == telemetry.R_STOP_UNVIABLE] == []


def test_the_check_can_be_switched_off_and_then_behaves_exactly_as_before(db, monkeypatch):
    settings = _settings(protection_precheck_enabled=False)
    client = futures_client(leverage_by_symbol={"BTC/USDT": 2.0})
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures")

    run_cycle(account_id, settings)

    assert the_trade(account_id).status == "open"


def test_spot_is_never_subject_to_the_check(db, monkeypatch):
    settings = _settings()
    client = spot_client()
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="spot")

    run_cycle(account_id, settings)

    assert the_trade(account_id).status == "open"


def test_the_refusal_works_without_telemetry_but_then_writes_no_decision_row(db, monkeypatch):
    settings = _settings(trade_telemetry=False)
    client = futures_client(leverage_by_symbol={"BTC/USDT": 2.0})
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures")

    notifier = run_cycle(account_id, settings)

    assert client.market_orders == [] and len(_alerts(notifier)) == 1


# ------------------------------------------------------------------ repeated failures


def _failed_exit_rows(account_id, count, *, closed_ago_hours=1.0, pair="BTC/USDT", reason="protection_failed_emergency_exit"):
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        for i in range(count):
            t = now - timedelta(hours=closed_ago_hours, minutes=i)
            session.add(
                Trade(
                    account_id=account_id, side="long", is_paper=False, entry_price=10.0, quantity=1.0,
                    stop_loss_price=9.0, status="closed", opened_at=t, closed_at=t, pair=pair, close_reason=reason,
                )
            )


def test_repeated_protection_failures_block_further_entries_with_a_decision_row_and_one_alert(db, monkeypatch):
    settings = _settings(protection_precheck_enabled=False, protection_fail_limit=2)
    client = futures_client(leverage_by_symbol={"BTC/USDT": 1.0})
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures")
    _failed_exit_rows(account_id, 2)

    notifier = run_cycle(account_id, settings)

    assert client.market_orders == []
    rows = [d for d in _decisions() if d.reason_code == telemetry.R_PROTECTION_FAIL_LIMIT]
    assert len(rows) == 1 and "failures" in rows[0].detail_json
    assert len(_alerts(notifier)) == 1 and "paused" in _alerts(notifier)[0].lower()
    assert len(all_trades(account_id)) == 2  # only the seeded ones: nothing new was opened

    notifier2 = run_cycle(account_id, settings, notifier)
    assert client.market_orders == [] and len(_alerts(notifier2)) == 1


def test_below_the_limit_trading_continues(db, monkeypatch):
    settings = _settings(protection_precheck_enabled=False, protection_fail_limit=2)
    client = futures_client(leverage_by_symbol={"BTC/USDT": 1.0})
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures")
    _failed_exit_rows(account_id, 1)

    run_cycle(account_id, settings)

    assert the_trade(account_id).status == "open"


def test_failures_outside_the_window_other_pairs_other_reasons_and_paper_do_not_count(db, monkeypatch):
    settings = _settings(protection_precheck_enabled=False, protection_fail_limit=2, protection_fail_window_hours=24.0)
    client = futures_client(leverage_by_symbol={"BTC/USDT": 1.0})
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures")
    _failed_exit_rows(account_id, 5, closed_ago_hours=30.0)                      # too old
    _failed_exit_rows(account_id, 5, pair="XRP/USDT")                            # another pair
    _failed_exit_rows(account_id, 5, reason="crossover_exit")                    # not a protection failure
    with session_scope() as session:
        now = datetime.now(timezone.utc)
        session.add(Trade(account_id=account_id, side="long", is_paper=True, entry_price=1.0, quantity=1.0, stop_loss_price=0.9,
                          status="closed", opened_at=now, closed_at=now, pair="BTC/USDT", close_reason="protection_failed_emergency_exit"))

    run_cycle(account_id, settings)

    assert the_trade(account_id).status == "open"


def test_the_limit_can_be_disabled(db, monkeypatch):
    settings = _settings(protection_precheck_enabled=False, protection_fail_limit=0)
    client = futures_client(leverage_by_symbol={"BTC/USDT": 1.0})
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures")
    _failed_exit_rows(account_id, 10)

    run_cycle(account_id, settings)

    assert the_trade(account_id).status == "open"


def test_the_incident_loop_is_cut_off_after_the_limit_end_to_end(db, monkeypatch):
    """No pre-entry check (the old behaviour); the stop is beyond liquidation every time. The first two entries
    open and are emergency-closed, then the limit stops the loop instead of a 23-trade run."""
    settings = _settings(protection_precheck_enabled=False, protection_fail_limit=2, protection_alert_repeat_hours=6.0)
    client = futures_client(liquidation_price=60.0, leverage_by_symbol={"BTC/USDT": 1.0})
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures")

    notifier = None
    for _ in range(6):
        notifier = run_cycle(account_id, settings, notifier)

    emergency = [t for t in all_trades(account_id) if t.close_reason == "protection_failed_emergency_exit"]
    assert len(emergency) == 2
    assert len(client.market_orders) == 4  # two entries and their two emergency exits, nothing after
    assert len(_alerts(notifier)) == 1


def test_a_failing_lookup_never_blocks_trading():
    class Boom:
        def execute(self, *a, **k):
            raise RuntimeError("db down")

    assert protection_guard.limit_reached(
        Boom(), "a", "BTC/USDT", now=datetime.now(timezone.utc), limit=2, window_hours=24.0
    ) is None


def test_alert_repeat_window():
    protection_guard.reset_for_tests()
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert protection_guard.should_alert("a", "BTC/USDT", "r", now=t0, repeat_hours=6)
    assert not protection_guard.should_alert("a", "BTC/USDT", "r", now=t0 + timedelta(hours=5), repeat_hours=6)
    assert protection_guard.should_alert("a", "BTC/USDT", "r", now=t0 + timedelta(hours=6, minutes=1), repeat_hours=6)
    assert protection_guard.should_alert("a", "XRP/USDT", "r", now=t0, repeat_hours=6)  # another pair is independent
    protection_guard.reset_for_tests()


def test_the_journal_names_the_refusing_check_for_a_protection_failure(db, monkeypatch, caplog):
    import logging

    settings = _settings(protection_precheck_enabled=False, protection_fail_limit=0)
    client = futures_client(liquidation_price=60.0, leverage_by_symbol={"BTC/USDT": 1.0})
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures")

    with caplog.at_level(logging.WARNING, logger="mba_bot.lifecycle"):
        run_cycle(account_id, settings)

    assert any("failed (ProtectionUnavailable: stop is at or beyond the liquidation price)" in r.getMessage() for r in caplog.records)
