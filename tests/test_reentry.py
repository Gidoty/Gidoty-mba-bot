"""Part J: re-entry after a protective exit (docs/safety/reentry.md). Off by default.

PROVES: the pure decision and every condition in it (protective reasons only, cooldown and window edges, limit per trend, trend intact, beyond the exit level by the ATR margin, both sides); the settings (off, typos);
that with the rule on the cycle makes an ORDINARY entry (through `_maybe_enter`, so the kill switch, guards, risk, stop, protection check and fee guard all apply) and links it to the previous trade; that nothing happens with
the rule off, after a plain stop-out, a time stop or a reversal exit, at the per-trend limit, on a stale candle, in a flat market, or while a trade is open; and how it interacts with the stop cooldown.
DOES NOT PROVE: that re-entering makes money (Part H measures that)."""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest
from helpers_live import futures_client, install, make_settings, new_account, run_cycle, the_trade
from test_kill_switch import _engage

from mba_bot import config, reentry as re_, trader
from mba_bot.db import init_db, session_scope
from mba_bot.models import Trade
from mba_bot.strategy import SignalType, StrategySignal

U = timezone.utc
HOUR = 3_600_000
R = re_.ReentryRules


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")


def D(**o):
    base = dict(rules=R(enabled=True), side="long", prev_reason="trailing_stop", prev_exit_price=100.0, candles_since_exit=5, reentries_so_far=0, fast=105.0, slow=100.0, last_close=103.0, atr=2.0)
    base.update(o)
    return re_.decide(**base)


# ---------------------------------------------------------------------------------------------- pure
def test_it_never_allows_anything_with_the_rule_off():
    assert D(rules=R(enabled=False)) == (False, "off")
    assert D() == (True, "ok")


def test_only_a_protective_exit_qualifies():
    for ok in ("break_even_stop", "trailing_stop", "profit_lock"):
        assert D(prev_reason=ok)[0] is True
    for bad in ("stop_loss", "time_stop", "reversal_fast_ema", "reversal_gap", "crossover_exit", "take_profit", "exchange_stop_triggered", None, ""):
        assert D(prev_reason=bad) == (False, "previous_exit_not_protective"), bad


def test_the_cooldown_and_the_window_have_exact_edges():
    assert D(candles_since_exit=1) == (False, "cooldown") and D(candles_since_exit=2)[0] is True
    assert D(candles_since_exit=48)[0] is True and D(candles_since_exit=49) == (False, "window_over")
    assert D(rules=R(enabled=True, cooldown_candles=5, window_candles=10), candles_since_exit=4)[1] == "cooldown" and D(rules=R(enabled=True, cooldown_candles=5, window_candles=10), candles_since_exit=5)[0] is True


def test_the_limit_per_trend_is_exact():
    assert D(reentries_so_far=0)[0] is True and D(reentries_so_far=1) == (False, "limit_per_trend")
    assert D(rules=R(enabled=True, max_per_trend=2), reentries_so_far=1)[0] is True and D(rules=R(enabled=True, max_per_trend=2), reentries_so_far=2)[1] == "limit_per_trend"


def test_the_trend_must_be_intact_and_the_close_must_be_beyond_the_exit_level_by_the_atr_margin():
    assert D(fast=99.0, slow=100.0) == (False, "trend_not_intact") and D(fast=100.0, slow=100.0)[1] == "trend_not_intact"
    assert D(last_close=102.0)[1] == "not_beyond_exit_level" and D(last_close=102.01)[0] is True  # exit 100 + 1 x ATR 2
    assert D(rules=R(enabled=True, above_exit_atr=2.0), last_close=103.99)[1] == "not_beyond_exit_level" and D(rules=R(enabled=True, above_exit_atr=2.0), last_close=104.01)[0] is True
    for missing in ({"fast": None}, {"slow": None}, {"last_close": None}, {"atr": None}, {"atr": 0.0}):
        assert D(**missing) == (False, "no_data"), missing
    assert D(prev_exit_price=None)[1] == "no_exit_price" and D(prev_exit_price=0.0)[1] == "no_exit_price"


def test_a_short_mirrors_every_condition():
    s = dict(side="short", prev_exit_price=100.0, fast=95.0, slow=100.0, last_close=97.0)
    assert D(**s) == (True, "ok")
    assert D(**{**s, "fast": 101.0})[1] == "trend_not_intact"
    assert D(**{**s, "last_close": 98.0})[1] == "not_beyond_exit_level" and D(**{**s, "last_close": 97.99})[0] is True


def test_the_settings_are_off_by_default_clamped_and_a_typo_never_switches_the_rule_on(monkeypatch):
    for n in ("REENTRY_ENABLED", "REENTRY_COOLDOWN_CANDLES", "REENTRY_WINDOW_CANDLES", "REENTRY_MAX_PER_TREND", "REENTRY_ABOVE_EXIT_ATR"):
        monkeypatch.delenv(n, raising=False)
    s = config.load_settings()
    assert (s.reentry_enabled, s.reentry_cooldown_candles, s.reentry_window_candles, s.reentry_max_per_trend, s.reentry_above_exit_atr) == (False, 2, 48, 1, 1.0)
    for bad in ("", "no", "tru", "2", " ", "enabled"):
        monkeypatch.setenv("REENTRY_ENABLED", bad)
        assert config.load_settings().reentry_enabled is False, bad
    monkeypatch.setenv("REENTRY_ENABLED", "true")
    monkeypatch.setenv("REENTRY_MAX_PER_TREND", "99")
    monkeypatch.setenv("REENTRY_COOLDOWN_CANDLES", "0")
    monkeypatch.setenv("REENTRY_ABOVE_EXIT_ATR", "abc")
    s = config.load_settings()
    assert (s.reentry_enabled, s.reentry_max_per_trend, s.reentry_cooldown_candles, s.reentry_above_exit_atr) == (True, 5, 1, 1.0)  # clamped; a bad ATR margin falls back to the default, never to zero


# ---------------------------------------------------------------------------------------------- the cycle (fake exchange)
NONE_UP = StrategySignal(SignalType.NONE, "no crossover", 101.0, 100.0, 2.0, 0.02)


def market(price=90.0):
    """12 hourly candles ending with the hour still forming: the last CLOSED candle closed at the top of this hour."""
    now_ms = int(datetime.now(U).timestamp() * 1000)
    top = (now_ms // HOUR) * HOUR
    rows = [[top - (12 - i) * HOUR, price, price + 1, price - 1, price, 1.0] for i in range(12)] + [[top, price, price + 1, price - 1, price, 1.0]]
    return rows


def setup(monkeypatch, *, prev_reason="trailing_stop", prev_exit=80.0, closed_hours_ago=5.0, reentry_of=None, signal=NONE_UP, **over):
    over.setdefault("ema_entry_window_minutes", 10_000)  # the test hour may be minutes old: the freshness rule is tested on its own
    settings = make_settings(max_notional_multiplier=100.0, **over)
    client = futures_client(ohlcv=market(), price=90.0)
    install(monkeypatch, client)
    monkeypatch.setattr(trader, "generate_signal", lambda *a, **k: signal)
    aid = new_account(settings, market_type="futures", email="re@example.com")
    now = datetime.now(U)
    with session_scope() as s:
        prev = Trade(account_id=aid, side="long", is_paper=False, entry_price=70.0, quantity=1.0, stop_loss_price=60.0, initial_stop_price=60.0, exit_price=prev_exit, pnl=10.0, status="closed", close_reason=prev_reason,
                     opened_at=now - timedelta(hours=closed_hours_ago + 6), closed_at=now - timedelta(hours=closed_hours_ago), pair="BTC/USDT", live_slot=None, reentry_of=reentry_of)
        s.add(prev)
        s.flush()
        pid = prev.id
    return client, settings, aid, pid


def trades(aid):
    with session_scope() as s:
        return s.query(Trade).filter(Trade.account_id == aid, Trade.status != "closed").all()


def test_with_the_rule_off_nothing_is_entered(db, monkeypatch):
    client, settings, aid, pid = setup(monkeypatch)
    run_cycle(aid, settings)
    assert trades(aid) == [] and not client.orders_by_role("entry")


def test_with_the_rule_on_one_ordinary_entry_is_made_protected_and_linked_to_the_previous_trade(db, monkeypatch):
    client, settings, aid, pid = setup(monkeypatch, reentry_enabled=True)
    run_cycle(aid, settings)
    t = the_trade(aid)
    assert t.status == "open" and t.protection_status == "protected" and t.reentry_of == pid and t.side == "long" and t.strategy_id in (None, "ema_cross_atr_v1")
    assert len([o for o in client.orders_by_role("stop") if o["status"] == "open"]) == 1 and client.violations == []  # a verified exchange-side stop, like any entry
    run_cycle(aid, settings)
    assert len(trades(aid)) == 1  # one live slot: nothing more


def test_the_ordinary_entry_guards_apply_so_an_engaged_kill_switch_stops_a_re_entry(db, monkeypatch):
    client, settings, aid, pid = setup(monkeypatch, reentry_enabled=True)
    _engage()
    run_cycle(aid, settings)
    assert trades(aid) == [] and not client.orders_by_role("entry")


def test_a_plain_stop_out_never_leads_to_a_re_entry(db, monkeypatch):
    client, settings, aid, pid = setup(monkeypatch, prev_reason="stop_loss", reentry_enabled=True)
    run_cycle(aid, settings)
    assert trades(aid) == []  # the pure test above covers every other close reason


def test_the_per_trend_limit_blocks_a_second_re_entry(db, monkeypatch):
    client, settings, aid, first = setup(monkeypatch, reentry_enabled=True)
    with session_scope() as s:
        prev = s.get(Trade, first)
        prev2 = Trade(account_id=aid, side="long", is_paper=False, entry_price=85.0, quantity=1.0, stop_loss_price=85.0, initial_stop_price=75.0, exit_price=80.0, pnl=-1.0, status="closed", close_reason="trailing_stop",
                      opened_at=datetime.now(U) - timedelta(hours=6), closed_at=datetime.now(U) - timedelta(hours=5), pair="BTC/USDT", live_slot=None, reentry_of=first)
        prev.closed_at = datetime.now(U) - timedelta(hours=20)
        s.add(prev2)
    run_cycle(aid, settings)
    assert trades(aid) == []  # the last trade was already a re-entry: one per trend


def test_a_stale_closed_candle_never_re_enters(db, monkeypatch):
    client, settings, aid, pid = setup(monkeypatch, reentry_enabled=True, ema_entry_window_minutes=1, closed_hours_ago=40.0)
    client._ohlcv = [[r[0] - 10 * HOUR] + r[1:] for r in market()]  # the last closed candle closed 10 hours ago; the exit was 40 hours ago, so only freshness can refuse
    run_cycle(aid, settings)
    assert trades(aid) == []
    client._ohlcv = market()  # the same trade and window with a fresh candle: the re-entry is made, so the refusal above was about freshness alone
    settings2 = dataclasses.replace(settings, ema_entry_window_minutes=10_000)
    run_cycle(aid, settings2)
    assert the_trade(aid).reentry_of == pid


def test_only_an_ema_trade_can_be_followed_by_a_re_entry(db, monkeypatch):
    client, settings, aid, pid = setup(monkeypatch, reentry_enabled=True)
    with session_scope() as s:
        s.get(Trade, pid).strategy_id = "donchian_breakout_v1"
    run_cycle(aid, settings)
    assert trades(aid) == []


def test_a_flat_market_never_re_enters(db, monkeypatch):
    flat = StrategySignal(SignalType.NONE, "flat market (ATR below threshold)", 101.0, 100.0, 2.0, 0.02)
    client, settings, aid, pid = setup(monkeypatch, reentry_enabled=True, signal=flat)
    run_cycle(aid, settings)
    assert trades(aid) == []


def test_a_price_that_has_not_moved_beyond_the_exit_level_does_not_re_enter(db, monkeypatch):
    client, settings, aid, pid = setup(monkeypatch, reentry_enabled=True, prev_exit=89.0)  # exit 89 + 1 x ATR 2 = 91 > the 90 close
    run_cycle(aid, settings)
    assert trades(aid) == []


def test_a_real_crossover_is_an_ordinary_entry_with_no_link(db, monkeypatch):
    cross = StrategySignal(SignalType.BULLISH_CROSSOVER, "bullish crossover", 101.0, 100.0, 2.0, 0.02)
    client, settings, aid, pid = setup(monkeypatch, reentry_enabled=True, signal=cross)
    run_cycle(aid, settings)
    t = the_trade(aid)
    assert t.status == "open" and t.reentry_of is None


# ---------------------------------------------------------------------------------------------- the stop cooldown
def test_the_stop_cooldown_counts_only_plain_stop_outs_so_it_does_not_block_a_re_entry_after_a_protective_exit(db, monkeypatch):
    client, settings, aid, pid = setup(monkeypatch, reentry_enabled=True, ema_stop_cooldown_hours=24.0, strategy_guards_mode="enforce")
    run_cycle(aid, settings)
    t = the_trade(aid)
    assert t.status == "open" and t.reentry_of == pid  # the trailing-stop exit 5 hours ago is not a stop-out to the cooldown


def test_after_a_plain_stop_out_the_cooldown_still_blocks_an_ordinary_entry_and_re_entry_does_not_bypass_it(db, monkeypatch):
    cross = StrategySignal(SignalType.BULLISH_CROSSOVER, "bullish crossover", 101.0, 100.0, 2.0, 0.02)
    client, settings, aid, pid = setup(monkeypatch, prev_reason="stop_loss", reentry_enabled=True, ema_stop_cooldown_hours=24.0, strategy_guards_mode="enforce", signal=cross)
    run_cycle(aid, settings)
    assert trades(aid) == []  # the cooldown refused the ordinary entry
    monkeypatch.setattr(trader, "generate_signal", lambda *a, **k: NONE_UP)
    run_cycle(aid, settings)
    assert trades(aid) == []  # and no re-entry was invented around it


def test_the_reentry_link_migration_is_additive_idempotent_and_reversible(tmp_path):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect, text

    path = next((Path(__file__).resolve().parents[1] / "alembic" / "versions").glob("b8e4a2f6d1c3_*.py"))
    spec = importlib.util.spec_from_file_location("reentry_migration", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    assert mig.down_revision == "a7d3e9f1c5b2"
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
    assert {c["name"]: c for c in inspect(eng).get_columns("trades")}["reentry_of"]["nullable"] is True
    with eng.begin() as c:
        assert c.execute(text("select reentry_of from trades")).scalar() is None
    go(mig.downgrade)
    go(mig.downgrade)
    assert "reentry_of" not in {c["name"] for c in inspect(eng).get_columns("trades")}
