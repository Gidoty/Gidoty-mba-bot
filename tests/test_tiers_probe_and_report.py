"""The tiers probe (python -m mba_bot.tiers_probe) and the opportunity report (python -m mba_bot.opportunity_report): read-only, and they print nothing secret."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from helpers_live import make_settings, new_account
from test_maintenance_tiers import MARKET, SOL_BRACKETS, SYMBOL, XRP_BRACKETS, FakeClient, no_sleep

from mba_bot import exchange, opportunity, telemetry, tiers_probe
from mba_bot import opportunity_report
from mba_bot.db import init_db, session_scope
from mba_bot.models import DecisionLog, TradingAccount

SECRETS = ("synthetic-", "signature", "apikey", "X-MBX", "secret")


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(exchange, "_sleep", no_sleep)


class ProbeClient(FakeClient):
    async def load_markets(self):
        return {}

    async def close(self):
        return None


def run_probe(client, pair="XRP/USDT"):
    return "\n".join(asyncio.run(tiers_probe.probe(tiers_probe.ReadOnlyClient(client), pair)))


# ------------------------------------------------------------------ what it prints
def test_a_good_answer_prints_attempts_shape_count_and_first_and_last_bracket():
    c = ProbeClient(steps=[None], raw=[[{"symbol": "XRPUSDT", "brackets": XRP_BRACKETS}]])
    c.steps = [c.ccxt_tiers()]
    out = run_probe(c)
    assert "attempt 1" in out and "WORKED" in out and "attempt 2" in out
    assert "raw answer shape list of 1" in out
    assert "brackets: 5" in out and "first bracket: floor 0, cap 10,000, rate 0.0065" in out
    assert "last bracket: floor 5,000,000, cap 50,000,000, rate 0.5" in out and "plausibility check: passes" in out
    assert "the entry check's own read: WORKED" in out


def test_a_single_object_answer_shows_its_shape():
    c = ProbeClient(steps=[{}], raw=[{"symbol": "XRPUSDT", "brackets": XRP_BRACKETS}])
    out = run_probe(c)
    assert "attempt 1" in out and "FAILED" in out  # ccxt returns {} for a single object
    assert "attempt 2" in out and "WORKED" in out and "single object" in out


def test_a_failure_prints_the_cause_not_a_traceback():
    c = ProbeClient(steps=[RuntimeError("boom apiKey=abcdefghijklmnopqrstuvwxyz0123456789 signature=feedfaceXXXX")], raw=[RuntimeError("boom2")])
    out = run_probe(c)
    assert "FAILED, cause" in out and "abcdefghijklmnopqrstuvwxyz" not in out and "feedface" not in out and "Traceback" not in out
    assert "the entry check's own read: FAILED" in out


def test_an_empty_answer_says_empty():
    out = run_probe(ProbeClient(steps=[{}], raw=[[]]))
    assert "FAILED, cause" in out and "empty" in out.lower()


# ------------------------------------------------------------------ it cannot place, change or cancel anything
@pytest.mark.parametrize("method", ["create_order", "cancel_order", "cancel_all_orders", "set_leverage", "set_margin_mode", "fapiPrivatePostOrder", "fapiPrivateDeleteOrder", "fapiPrivatePostLeverage", "edit_order", "withdraw", "fetch_balance", "fetch_positions"])
def test_the_read_only_wrapper_refuses_everything_but_the_listed_reads(method):
    c = ProbeClient(steps=[{}])
    setattr(c, method, lambda *a, **k: (_ for _ in ()).throw(AssertionError("reached the real client")))
    with pytest.raises(tiers_probe.ReadOnlyViolation):
        getattr(tiers_probe.ReadOnlyClient(c), method)
    assert not any(m in tiers_probe.ALLOWED for m in ("create_order", "cancel_order", "set_leverage", "fetch_balance", "fetch_positions", "withdraw"))


def test_the_wrapper_cannot_be_modified_or_used_to_reach_the_inner_client():
    w = tiers_probe.ReadOnlyClient(ProbeClient(steps=[{}]))
    with pytest.raises(tiers_probe.ReadOnlyViolation):
        w.create_order = lambda *a: None
    with pytest.raises(tiers_probe.ReadOnlyViolation):
        w.options


def test_the_probe_only_issues_the_expected_read_requests():
    c = ProbeClient(steps=[{}], raw=[[{"symbol": "XRPUSDT", "brackets": XRP_BRACKETS}]])
    run_probe(c)
    assert {x[0] for x in c.calls} <= {"fetch_leverage_tiers", "raw"}
    assert len(c.calls) <= 4


def test_source_names_no_write_method():
    import re
    from pathlib import Path

    src = Path(tiers_probe.__file__).read_text()
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith(("#", '"""')))
    assert not re.search(r"create_order|cancel_order|set_leverage|set_margin|PostOrder|DeleteOrder|withdraw|edit_order", code.replace('"create_order"', "")), "the probe's code mentions a write method"


# ------------------------------------------------------------------ the command
@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "probe.db"
    url = f"sqlite:///{path}"
    st = make_settings(database_url=url)
    init_db(url)
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("ENCRYPTION_KEY", st.encryption_key)
    return st


def _futures_account(st, email="owner-secret-mail@example.com", **kw):
    aid = new_account(st, market_type="futures", pair="SOL/USDT", email=email)
    with session_scope() as s:
        a = s.get(TradingAccount, aid)
        a.pair_selection_mode = "auto"
        a.extra_strategies = "donchian_breakout_v1,rsi_mr_v1"
    return aid


def test_without_account_it_lists_accounts_by_short_id_and_makes_no_exchange_call(env, monkeypatch, capsys):
    aid = _futures_account(env)
    new_account(env, market_type="spot", email="spot@example.com")
    monkeypatch.setattr(exchange, "build_exchange_client", lambda **k: (_ for _ in ()).throw(AssertionError("built a client")))
    assert tiers_probe.main([]) == 0
    out = capsys.readouterr().out
    assert aid[:8] in out and aid not in out and "SOL/USDT" in out and "auto" in out and "donchian_breakout_v1" in out
    assert "example.com" not in out and "owner-secret" not in out and out.count("\n  ") == 1  # one Futures account only


def test_with_account_it_probes_through_the_read_only_wrapper_and_prints_no_secret(env, monkeypatch, capsys):
    aid = _futures_account(env)
    built = {}

    def build(**kw):
        built.update(kw)
        c = ProbeClient(steps=[{}], raw=[[{"symbol": "SOLUSDT", "brackets": SOL_BRACKETS}]])
        c.market = lambda pair: dict(MARKET, id="SOLUSDT", symbol=pair)
        built["client"] = c
        return c

    monkeypatch.setattr(exchange, "build_exchange_client", build)
    assert tiers_probe.main(["--account", aid[:8], "--pair", "SOL/USDT"]) == 0
    out = capsys.readouterr().out
    assert "brackets: 3" in out and "list of 1" in out and "last bracket: floor 100,000, cap 30,000,000, rate 0.5" in out
    assert "up to 4 signed READ requests" in out and "places, changes and cancels nothing" in out
    assert built["sandbox"] is False and built["market_type"] == "futures"
    for needle in (built["api_key"], built["api_secret"], "example.com", aid):
        assert needle not in out
    assert not any(c[0] not in ("fetch_leverage_tiers", "raw") for c in built["client"].calls)


def test_it_refuses_a_spot_account_a_short_id_and_an_unknown_id(env, monkeypatch, capsys):
    spot = new_account(env, market_type="spot", email="s@example.com")
    monkeypatch.setattr(exchange, "build_exchange_client", lambda **k: (_ for _ in ()).throw(AssertionError("built a client")))
    assert tiers_probe.main(["--account", spot[:8]]) == 2
    assert tiers_probe.main(["--account", "ab"]) == 2
    assert tiers_probe.main(["--account", "zzzzzzzz"]) == 2


def test_a_failure_prints_only_the_exception_class(env, monkeypatch, capsys):
    aid = _futures_account(env)
    monkeypatch.setattr(exchange, "build_exchange_client", lambda **k: (_ for _ in ()).throw(RuntimeError("apiKey=SECRETSECRETSECRET1234567890")))
    assert tiers_probe.main(["--account", aid[:8]]) == 2
    err = capsys.readouterr().err
    assert "RuntimeError" in err and "SECRET" not in err


# ------------------------------------------------------------------ the report
def _rows(account_id, now):
    def row(**kw):
        base = dict(ts=now - timedelta(hours=1), account_id=account_id, mode="live", market_type="futures", pair="XRP/USDT", stage=opportunity.STAGE)
        base.update(kw)
        return DecisionLog(**base)

    return [
        row(strategy_id=opportunity.EMA_ID, outcome="no_signal", reason_code="ema_no_cross", candle_ts=1),
        row(strategy_id=opportunity.EMA_ID, outcome="signal", reason_code="ema_cross_down", signal="bearish_crossover", candle_ts=2),
        row(strategy_id=opportunity.EMA_ID, stage="entry", outcome="rejected", reason_code="maintenance_tiers_read_failed", candle_ts=2),
        row(strategy_id=opportunity.EMA_ID, stage="entry", outcome="accepted", reason_code=telemetry.R_ENTRY_SUBMITTED, candle_ts=3),
        row(strategy_id=opportunity.DONCHIAN_ID, outcome="no_signal", reason_code="donchian_inside_channel", candle_ts=1),
        row(strategy_id=opportunity.DONCHIAN_ID, outcome="no_signal", reason_code="donchian_inside_channel", candle_ts=2),
        row(strategy_id=opportunity.RSI_ID, outcome="not_taken", reason_code=opportunity.R_NOT_EVALUATED, candle_ts=2),
        row(strategy_id=opportunity.RSI_ID, outcome="no_signal", reason_code="rsi_no_cross", candle_ts=1, ts=now - timedelta(hours=30)),  # outside the window
    ]


def test_the_report_groups_per_account_strategy_and_reason(env, capsys):
    aid = new_account(env, market_type="futures", email="r@example.com")
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        s.add_all(_rows(aid, now))
    lines = opportunity_report.render(hours=24, now=now)
    text = "\n".join(lines)
    assert f"account {aid[:8]}" in text and aid not in text and "example.com" not in text
    assert "candles evaluated 2, signals 1, entries opened 1" in text  # EMA
    assert "maintenance_tiers_read_failed 1" in text
    assert "donchian_inside_channel 2" in text and "candles evaluated 2, signals 0, entries opened 0" in text
    assert "strategy_not_evaluated 1" in text
    assert "rsi_no_cross" not in text  # the row older than 24 hours is outside the window
    assert opportunity_report.main(["--hours", "24"]) == 0
    assert "account " in capsys.readouterr().out


def test_the_report_is_read_only_and_survives_a_missing_database(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/nope.db")
    assert opportunity_report.main([]) == 2
    assert not (tmp_path / "nope.db").exists()
    assert "cannot start" in capsys.readouterr().err


def test_the_report_writes_nothing(env):
    aid = new_account(env, market_type="futures", email="w@example.com")
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        s.add_all(_rows(aid, now))
    with session_scope() as s:
        before = s.query(DecisionLog).count()
    opportunity_report.render(hours=24, now=now)
    with session_scope() as s:
        assert s.query(DecisionLog).count() == before
