"""Opportunity ledger (docs/safety/opportunity_ledger.md): one decision_log row per account, strategy and closed candle for every evaluation, observability only.

The most important test here is the equivalence test: the same cycles with the ledger off and on make the same entry decisions, orders and trades."""
from __future__ import annotations

import ast
import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from helpers_live import BULLISH_OHLCV, FLAT_OHLCV, FakeNotifier, all_trades, futures_client, install, make_settings, new_account, run_cycle, spot_client
from test_audit_h2_m1_opt_in import NO_EMA, raw
from test_donchian import with_last
from test_rsi_mr import LONG, mk

from mba_bot import opportunity, pilot_status, telemetry, trader
from mba_bot.db import init_db, session_scope
from mba_bot.models import DecisionLog, TradingAccount

U = timezone.utc
KEYS = dict(testnet_spot_api_key="paper-key-1234567890", testnet_spot_api_secret="paper-secret-1234567890")
SRC = Path(__file__).resolve().parents[1] / "src" / "mba_bot"
_REAL_GENERATE_SIGNAL = trader.generate_signal
DON = with_last((100.0, 103.5, 100.5, 103.0, 200.0))
RSI = mk(LONG)


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")
    telemetry.reset_for_tests()
    opportunity.reset_for_tests()
    yield
    telemetry.reset_for_tests()
    opportunity.reset_for_tests()


def _settings(**over):
    base = dict(KEYS, rsi_mr_enabled=True, donchian_enabled=True, strategy_guards_mode="off", new_strategy_guards_mode="enforce", fee_guard_margin=0.5, strategy_expectancy_window=0,
                max_total_risk_pct=5.0, max_notional_multiplier=100.0, opportunity_ledger=True)
    base.update(over)
    return make_settings(**base)


def _paper(st, extra="rsi_mr_v1,donchian_breakout_v1", selection="manual", email="x@example.com"):
    aid = new_account(st, email=email)
    with session_scope() as s:
        a = s.get(TradingAccount, aid)
        a.bot_started_at = None  # PAPER
        a.extra_strategies = extra
        a.pair_selection_mode = selection
    return aid


def _rig(monkeypatch, candles, ema=NO_EMA):
    data = raw(candles)
    client = spot_client(ohlcv_by_pair={p: data for p in ("BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT")}, ohlcv=data)
    install(monkeypatch, client)
    monkeypatch.setattr(trader, "generate_signal", lambda *a, **k: ema)
    return client


def _ledger(account_id=None):
    with session_scope() as s:
        q = s.query(DecisionLog).filter(DecisionLog.stage == opportunity.STAGE)
        if account_id:
            q = q.filter(DecisionLog.account_id == account_id)
        return q.order_by(DecisionLog.id).all()


# ------------------------------------------------------------------ the reason map covers every reason the strategies can give
def _no_signal_texts(path: str, call_suffix: str) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse((SRC / path).read_text())):
        if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")).endswith(call_suffix) and len(node.args) >= 2:
            first = ast.unparse(node.args[0])
            if first in ("None", "SignalType.NONE"):
                out |= {n.value for n in ast.walk(node.args[1]) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    return out


@pytest.mark.parametrize("path, suffix, sid", [("strategy.py", "StrategySignal", opportunity.EMA_ID), ("donchian.py", "DonchianSignal", opportunity.DONCHIAN_ID), ("rsi_mr.py", "RsiSignal", opportunity.RSI_ID)])
def test_every_no_signal_reason_text_has_a_code(path, suffix, sid):
    texts = _no_signal_texts(path, suffix)
    assert texts, "the source scan found nothing: the test is broken"
    assert texts <= set(opportunity.REASON_BY_STRATEGY_TEXT[sid]), texts - set(opportunity.REASON_BY_STRATEGY_TEXT[sid])


def test_every_code_has_plain_words_and_fits_the_column():
    codes = {c for m in opportunity.REASON_BY_STRATEGY_TEXT.values() for c in m.values()} | {c for t in opportunity.SIGNAL_CODES.values() for c in t}
    codes |= {opportunity.R_STALE, opportunity.R_NOT_BEST, opportunity.R_NOT_EVALUATED, opportunity.R_CAPITAL_SKIP}
    for c in codes:
        assert c in opportunity.PLAIN and len(c) <= 48, c


def test_verdict_numbers_for_each_strategy():
    from mba_bot import donchian, rsi_mr

    out, code, nums = opportunity.verdict(opportunity.DONCHIAN_ID, donchian.DonchianSignal(None, "no breakout", upper=110.0, lower=90.0, volume=50.0, avg_volume=100.0, atr_pct=0.02, atr_pct_avg=0.03))
    assert (out, code) == (opportunity.OUT_NO_SIGNAL, "donchian_inside_channel") and nums["channel_high"] == 110.0 and nums["channel_low"] == 90.0 and nums["volume_ratio"] == 0.5
    out, code, nums = opportunity.verdict(opportunity.RSI_ID, rsi_mr.RsiSignal(None, "no RSI cross", rsi=55.5, prev_rsi=50.0, adx=18.0, atr_pct=0.01))
    assert (out, code) == (opportunity.OUT_NO_SIGNAL, "rsi_no_cross") and nums["rsi"] == 55.5
    out, code, _ = opportunity.verdict(opportunity.EMA_ID, NO_EMA)
    assert (out, code) == (opportunity.OUT_NO_SIGNAL, "ema_no_cross")


# ------------------------------------------------------------------ rows
def test_off_by_default_writes_nothing(db, monkeypatch):
    st = _settings(opportunity_ledger=False)
    _rig(monkeypatch, DON)
    aid = _paper(st)
    run_cycle(aid, st)
    assert _ledger() == []
    assert make_settings().opportunity_ledger is False


def test_one_row_per_strategy_and_candle_with_the_deciding_numbers(db, monkeypatch):
    st = _settings()
    _rig(monkeypatch, DON)
    aid = _paper(st)
    run_cycle(aid, st)
    rows = _ledger(aid)
    by = {r.strategy_id: r for r in rows if r.outcome != opportunity.OUT_NOT_TAKEN}
    assert set(by) == {opportunity.EMA_ID, opportunity.DONCHIAN_ID, opportunity.RSI_ID}
    assert by[opportunity.EMA_ID].reason_code == "ema_no_cross" and by[opportunity.EMA_ID].outcome == opportunity.OUT_NO_SIGNAL
    assert by[opportunity.DONCHIAN_ID].outcome == opportunity.OUT_SIGNAL and by[opportunity.DONCHIAN_ID].reason_code.startswith("donchian_breakout")
    d = json.loads(by[opportunity.DONCHIAN_ID].detail_json)
    assert d["channel_high"] and d["channel_low"] and by[opportunity.DONCHIAN_ID].price == pytest.approx(103.0)
    assert all(r.candle_ts for r in by.values())


def test_a_second_cycle_on_the_same_candle_adds_nothing(db, monkeypatch):
    st = _settings()
    _rig(monkeypatch, DON)
    aid = _paper(st)
    run_cycle(aid, st)
    n = len(_ledger())
    run_cycle(aid, st)
    assert len(_ledger()) == n


FLAT_DAY = with_last((100.0, 100.4, 99.6, 100.0, 100.0))  # no entry from any strategy, so every later cycle evaluates the same candle again


def test_a_restart_does_not_repeat_rows(db, monkeypatch):
    st = _settings()
    _rig(monkeypatch, FLAT_DAY)
    aid = _paper(st)
    run_cycle(aid, st)
    n = len(_ledger())
    assert n >= 3 and all_trades(aid) == []
    opportunity.reset_for_tests()  # a new process: the in-memory marks are gone, the database check must hold
    run_cycle(aid, st)
    assert len(_ledger()) == n


def test_a_no_signal_candle_is_recorded_with_its_reason(db, monkeypatch):
    st = _settings()
    _rig(monkeypatch, [c for c in RSI])  # an RSI series with no breakout
    aid = _paper(st, extra="donchian_breakout_v1")
    run_cycle(aid, st)
    codes = {r.strategy_id: r.reason_code for r in _ledger(aid)}
    assert codes[opportunity.EMA_ID] == "ema_no_cross"
    assert codes[opportunity.DONCHIAN_ID].startswith("donchian_")


def test_the_daily_cap_bounds_rows_and_never_touches_entry_rows(db, monkeypatch):
    st = _settings(opportunity_ledger_daily_cap=10)
    aid = _paper(st)
    now = datetime.now(U)
    acct = TradingAccount()
    acct.id, acct.market_type, acct.pair_selection_mode = aid, "spot", "manual"
    with session_scope(autoflush=False) as s:
        for i in range(25):
            opportunity.record_not_taken(s, st, acct, strategy_id=opportunity.EMA_ID, pair="BTC/USDT", candle_ts=1000 + i, reason=opportunity.R_STALE, is_paper=True, now=now)
    assert len(_ledger(aid)) == 10
    with session_scope(autoflush=False) as s:  # the entry stage has its own cap and is not starved by the ledger
        telemetry.log_decision(s, make_settings(trade_telemetry=True), acct, stage=telemetry.STAGE_ENTRY, outcome=telemetry.REJECTED, reason="risk_blocked", is_paper=True, now=now, pair="BTC/USDT", strategy_id=opportunity.EMA_ID)
    with session_scope() as s:
        assert s.query(DecisionLog).filter(DecisionLog.stage == telemetry.STAGE_ENTRY).count() == 1


def test_retention_deletes_only_old_ledger_rows(db, monkeypatch):
    st = _settings(opportunity_ledger_keep_days=30)
    aid = _paper(st)
    now = datetime.now(U)
    with session_scope() as s:
        s.add(DecisionLog(ts=now - timedelta(days=40), account_id=aid, mode="paper", market_type="spot", pair="BTC/USDT", strategy_id="ema_crossover_v1", stage=opportunity.STAGE, outcome="no_signal", reason_code="ema_no_cross", candle_ts=1))
        s.add(DecisionLog(ts=now - timedelta(days=40), account_id=aid, mode="paper", market_type="spot", pair="BTC/USDT", strategy_id="ema_crossover_v1", stage="entry", outcome="rejected", reason_code="risk_blocked", candle_ts=1))
        s.add(DecisionLog(ts=now - timedelta(days=2), account_id=aid, mode="paper", market_type="spot", pair="BTC/USDT", strategy_id="ema_crossover_v1", stage=opportunity.STAGE, outcome="no_signal", reason_code="ema_no_cross", candle_ts=2))
    _rig(monkeypatch, DON)
    run_cycle(aid, st)
    with session_scope() as s:
        assert s.query(DecisionLog).filter(DecisionLog.stage == "entry").count() == 1  # the old ENTRY row is never pruned
        assert s.query(DecisionLog).filter(DecisionLog.stage == opportunity.STAGE, DecisionLog.ts < now - timedelta(days=30)).count() == 0
        assert s.query(DecisionLog).filter(DecisionLog.stage == opportunity.STAGE, DecisionLog.candle_ts == 2).count() == 1


def test_a_failing_write_never_reaches_the_cycle(db, monkeypatch):
    st = _settings()
    client = _rig(monkeypatch, DON)
    aid = _paper(st)
    monkeypatch.setattr(opportunity, "DecisionLog", None)  # every write now raises inside the ledger
    run_cycle(aid, st)  # must not raise
    assert _ledger() == []
    assert client.violations == []


def test_pilot_status_last_decisions_ignore_ledger_rows(db, monkeypatch):
    st = _settings()
    _rig(monkeypatch, DON)
    aid = _paper(st)
    run_cycle(aid, st)
    assert len(_ledger(aid)) >= 3
    now = datetime.now(U)
    with session_scope() as s:
        s.add(DecisionLog(ts=now - timedelta(hours=5), account_id=aid, mode="paper", market_type="spot", pair="BTC/USDT", strategy_id="ema_crossover_v1", stage="entry", outcome="rejected", reason_code="risk_blocked"))
    with session_scope() as s:
        lines = pilot_status.account_screen(s, s.get(TradingAccount, aid), st, now)
    text = "\n".join(lines)
    assert "risk_blocked" in text and "ema_no_cross" not in text and "opportunity" not in text  # newer ledger rows did not push the entry row out


# ------------------------------------------------------------------ Auto Select
def _live_auto(st, extra=None):
    aid = new_account(st, market_type="futures")
    with session_scope() as s:
        a = s.get(TradingAccount, aid)
        a.pair_selection_mode = "auto"
        a.extra_strategies = extra
    return aid


def test_auto_select_records_every_scanned_pair_and_why_the_others_were_not_taken(db, monkeypatch):
    st = make_settings(max_notional_multiplier=100.0, opportunity_ledger=True)
    client = futures_client(ohlcv=BULLISH_OHLCV, leverage_by_symbol={p: 1.0 for p in ("BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT")})
    install(monkeypatch, client)
    aid = _live_auto(st)
    run_cycle(aid, st, swallow=True)
    rows = [r for r in _ledger(aid) if r.strategy_id == opportunity.EMA_ID]
    scanned = {r.pair for r in rows if r.outcome in (opportunity.OUT_SIGNAL, opportunity.OUT_NO_SIGNAL)}
    assert scanned == {"BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT"}
    not_best = [r for r in rows if r.reason_code == opportunity.R_NOT_BEST]
    assert len(not_best) == 3 and all(json.loads(r.detail_json)["picked"] for r in not_best)
    assert all(json.loads(r.detail_json)["sel"] == "auto" for r in rows)


def test_auto_select_says_when_donchian_and_rsi_were_not_evaluated(db, monkeypatch):
    st = make_settings(max_notional_multiplier=100.0, opportunity_ledger=True, rsi_mr_enabled=True, donchian_enabled=True, live_rsi_mr=True, live_donchian=True, new_strategy_guards_mode="enforce")
    client = futures_client(ohlcv=FLAT_OHLCV, leverage_by_symbol={p: 1.0 for p in ("BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT")})
    install(monkeypatch, client)
    aid = _live_auto(st, extra="rsi_mr_v1,donchian_breakout_v1")
    run_cycle(aid, st, swallow=True)
    skipped = {r.strategy_id for r in _ledger(aid) if r.reason_code == opportunity.R_NOT_EVALUATED}
    assert skipped == {opportunity.DONCHIAN_ID, opportunity.RSI_ID}
    run_cycle(aid, st, swallow=True)
    assert len([r for r in _ledger(aid) if r.reason_code == opportunity.R_NOT_EVALUATED]) == 2  # once per candle, not per cycle


def test_a_stale_ema_signal_is_recorded(db, monkeypatch):
    st = make_settings(max_notional_multiplier=100.0, opportunity_ledger=True, ema_closed_candle_only=True, ema_entry_window_minutes=10.0)
    client = futures_client(ohlcv=BULLISH_OHLCV)
    install(monkeypatch, client)
    aid = new_account(st, market_type="futures")
    run_cycle(aid, st, swallow=True)
    stale = [r for r in _ledger(aid) if r.reason_code == opportunity.R_STALE]
    assert len(stale) == 1 and client.market_orders == []


# ------------------------------------------------------------------ the equivalence proof: the ledger cannot change a decision
def _fresh_bullish():
    import time

    hour = 3_600_000
    last_open = int(time.time() * 1000) - hour - 120_000  # the last closed candle closed two minutes ago
    n = len(BULLISH_OHLCV)
    rows = [[last_open - (n - 1 - i) * hour, *r[1:]] for i, r in enumerate(BULLISH_OHLCV)]
    rows.append([last_open + hour, *BULLISH_OHLCV[-1][1:]])  # the forming candle
    return rows


def _outcome(tmp_path, monkeypatch, ledger: bool, scenario: str):
    init_db(f"sqlite:///{tmp_path}/{scenario}-{ledger}.db")
    monkeypatch.setattr(trader, "generate_signal", _REAL_GENERATE_SIGNAL)  # an earlier paper scenario in the same test must not leave its stub behind
    telemetry.reset_for_tests()
    opportunity.reset_for_tests()
    st = _settings(opportunity_ledger=ledger, trade_telemetry=True)
    if scenario == "paper_donchian":
        client = _rig(monkeypatch, DON)
        aid = _paper(st)
    elif scenario == "paper_rsi":
        client = _rig(monkeypatch, RSI)
        aid = _paper(st)
    elif scenario == "paper_flat":
        client = _rig(monkeypatch, with_last((100.0, 100.4, 99.6, 100.0, 100.0)))
        aid = _paper(st)
    elif scenario == "live_futures_ema":
        st = make_settings(max_notional_multiplier=100.0, trade_telemetry=True, opportunity_ledger=ledger)
        client = futures_client(ohlcv=BULLISH_OHLCV)
        install(monkeypatch, client)
        aid = new_account(st, market_type="futures")
    elif scenario == "live_futures_ema_fresh":  # the signal candle closed 2 minutes ago: inside the 10-minute window, so the guard lets it through
        st = make_settings(max_notional_multiplier=100.0, trade_telemetry=True, opportunity_ledger=ledger, ema_closed_candle_only=True, ema_entry_window_minutes=10.0)
        client = futures_client(ohlcv=_fresh_bullish())
        install(monkeypatch, client)
        aid = new_account(st, market_type="futures")
    elif scenario == "live_futures_ema_stale":  # the candle timestamps are ancient, so the stale-entry guard refuses: with the ledger on or off
        st = make_settings(max_notional_multiplier=100.0, trade_telemetry=True, opportunity_ledger=ledger, ema_closed_candle_only=True, ema_entry_window_minutes=10.0)
        client = futures_client(ohlcv=BULLISH_OHLCV)
        install(monkeypatch, client)
        aid = new_account(st, market_type="futures")
    else:
        st = make_settings(max_notional_multiplier=100.0, trade_telemetry=True, opportunity_ledger=ledger)
        client = spot_client(ohlcv=FLAT_OHLCV)
        install(monkeypatch, client)
        aid = new_account(st)
    run_cycle(aid, st, swallow=True)
    trades = [(t.strategy_id, t.side, round(t.entry_price or 0, 6), round(t.quantity or 0, 8), t.status) for t in all_trades(aid)]
    with session_scope() as s:
        entry_rows = sorted((d.stage, d.outcome, d.reason_code, d.strategy_id, d.pair) for d in s.query(DecisionLog).filter(DecisionLog.stage != opportunity.STAGE).all())
        acct = s.get(TradingAccount, aid)
        state = (acct.last_error, acct.is_active)
    return trades, entry_rows, state, list(client.market_orders), len(_ledger())


@pytest.mark.parametrize("scenario", ["paper_donchian", "paper_rsi", "paper_flat", "live_futures_ema", "live_futures_ema_fresh", "live_futures_ema_stale", "live_spot_flat"])
def test_entry_decisions_are_identical_with_the_ledger_on_and_off(tmp_path, monkeypatch, scenario):
    off = _outcome(tmp_path, monkeypatch, False, scenario)
    on = _outcome(tmp_path, monkeypatch, True, scenario)
    assert off[:4] == on[:4], scenario  # trades, entry-stage decision rows, account state and every order sent
    assert off[4] == 0 and on[4] > 0, scenario  # and the ledger really was on in the second run


def test_scenarios_actually_trade_or_refuse_so_the_equivalence_is_not_vacuous(tmp_path, monkeypatch):
    assert _outcome(tmp_path, monkeypatch, True, "live_futures_ema")[0] != []  # a live entry happened with the ledger on
    assert _outcome(tmp_path, monkeypatch, True, "paper_donchian")[0] != []  # a paper Donchian entry happened
    fresh = _outcome(tmp_path, monkeypatch, True, "live_futures_ema_fresh")
    assert fresh[0] != [] and fresh[3] != []  # inside the window the live EMA entry happens with the ledger on
    stale = _outcome(tmp_path, monkeypatch, True, "live_futures_ema_stale")
    assert stale[0] == [] and stale[3] == [] and any(r[2] == "signal_stale" for r in stale[1])  # the guard refused, and the ledger did not change that


# ------------------------------------------------------------------ the daily summary lines
def test_summary_lines_are_text_only_and_empty_when_there_is_nothing(db, monkeypatch):
    st = _settings()
    aid = _paper(st)
    now = datetime.now(U)
    with session_scope() as s:
        assert opportunity.summary_lines(s, aid, now) == []
    _rig(monkeypatch, DON)
    run_cycle(aid, st)
    with session_scope() as s:
        lines = opportunity.summary_lines(s, aid, datetime.now(U))
    assert lines and lines[0].startswith("Entry checks, last 24 hours")
    assert all("@" not in l and "key" not in l.lower() for l in lines)
