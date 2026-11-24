"""Auto Select tie measurement: strength numbers in the ledger, the 'ties' section of the opportunity report, and the proof that the pick itself did not change.

A measurement, not evidence that any ranking rule works. No network: the candle read is injected (a fake), and the public client is checked with a fake class."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from helpers_live import all_trades, run_cycle
from test_auto_select_all_strategies import DON_FLAT, PAIRS, RSI_FLAT, RSI_HIT, _trend_breakout, auto_account, db, ledger, rig, st  # noqa: F401
from test_donchian_integration import raw as don_raw

from mba_bot import donchian, exchange, opportunity, opportunity_report, opportunity_ties, rsi_mr
from mba_bot.db import init_db, session_scope
from mba_bot.models import DecisionLog

U = timezone.utc
HOUR = 3_600_000
SRC = Path(__file__).resolve().parents[1] / "src" / "mba_bot"


# ------------------------------------------------------------------------------------------------ strength
def test_donchian_strength_is_the_distance_beyond_the_channel_in_atr_units():
    long = donchian.DonchianSignal("long", "x", close=107.7, upper=104.4, lower=101.5, atr=1.1)
    short = donchian.DonchianSignal("short", "x", close=98.0, upper=104.4, lower=101.5, atr=1.75)
    assert opportunity.strength(opportunity.DONCHIAN_ID, long) == pytest.approx(3.0)
    assert opportunity.strength(opportunity.DONCHIAN_ID, short) == pytest.approx(2.0)


def test_rsi_strength_is_the_distance_beyond_the_entry_line_in_rsi_points():
    p = rsi_mr.RsiParams()
    assert opportunity.strength(opportunity.RSI_ID, rsi_mr.RsiSignal("long", "x", rsi=p.oversold + 4.5)) == pytest.approx(4.5)
    assert opportunity.strength(opportunity.RSI_ID, rsi_mr.RsiSignal("short", "x", rsi=p.overbought - 2.0)) == pytest.approx(2.0)


@pytest.mark.parametrize("sig", [donchian.DonchianSignal(None, "no breakout"), donchian.DonchianSignal("long", "x", close=1.0, upper=0.5, lower=0.1, atr=0.0), donchian.DonchianSignal("long", "x", close=None, upper=1, lower=0, atr=1)])
def test_donchian_strength_is_none_without_a_side_or_numbers(sig):
    assert opportunity.strength(opportunity.DONCHIAN_ID, sig) is None


def test_strength_never_raises_and_ema_has_none():
    assert opportunity.strength(opportunity.EMA_ID, object()) is None
    assert opportunity.strength(opportunity.DONCHIAN_ID, object()) is None


def test_a_signal_row_carries_the_strength_and_a_no_signal_row_does_not():
    out, code, nums = opportunity.verdict(opportunity.DONCHIAN_ID, donchian.DonchianSignal("long", "x", close=107.7, upper=104.4, lower=101.5, atr=1.1))
    assert out == opportunity.OUT_SIGNAL and nums["strength"] == pytest.approx(3.0)
    out, code, nums = opportunity.verdict(opportunity.RSI_ID, rsi_mr.RsiSignal(None, "no RSI cross", rsi=55.0))
    assert out == opportunity.OUT_NO_SIGNAL and "strength" not in nums


# ------------------------------------------------------------------------------------------------ the ledger rows of a real tie, and the pick is unchanged
def _two_donchian_hits(monkeypatch, *, ledger_on=True):
    settings = st(live_rsi_mr=False, opportunity_ledger=ledger_on)
    strong, weak = don_raw(_trend_breakout(jump=6.0)), don_raw(_trend_breakout(jump=3.8))
    client = rig(monkeypatch, {"BTC/USDT": DON_FLAT, "ETH/USDT": weak, "BNB/USDT": DON_FLAT, "XRP/USDT": strong}, trade_pair="ETH/USDT")
    aid = auto_account(settings)
    run_cycle(aid, settings)
    return aid, client


def test_a_tie_records_strengths_and_the_pick_is_still_the_first_pair_in_scan_order_even_when_a_later_pair_is_stronger(db, monkeypatch):
    aid, _ = _two_donchian_hits(monkeypatch)
    trades = all_trades(aid)
    assert len(trades) == 1 and trades[0].pair == "ETH/USDT"  # ETH is first in scan order; XRP was stronger and was NOT chosen
    sig_rows = {r.pair: json.loads(r.detail_json) for r in ledger(aid, "donchian_breakout_v1") if r.outcome == "signal"}
    assert set(sig_rows) == {"ETH/USDT", "XRP/USDT"} and sig_rows["XRP/USDT"]["strength"] > sig_rows["ETH/USDT"]["strength"] > 0
    (skipped,) = [r for r in ledger(aid, "donchian_breakout_v1") if r.reason_code == opportunity.R_NOT_BEST]
    d = json.loads(skipped.detail_json)
    assert skipped.pair == "XRP/USDT" and d["picked"] == "ETH/USDT" and d["picked_strength"] == sig_rows["ETH/USDT"]["strength"] and d["strength"] == sig_rows["XRP/USDT"]["strength"]
    assert skipped.candle_ts is not None  # the signal candle, so the report can find the next 24 candles


def test_the_pick_is_identical_with_the_ledger_on_and_off(db, monkeypatch, tmp_path):
    aid_on, _ = _two_donchian_hits(monkeypatch, ledger_on=True)
    picked_on = [(t.pair, t.strategy_id, t.side) for t in all_trades(aid_on)]
    init_db(f"sqlite:///{tmp_path}/off.db")
    opportunity.reset_for_tests()
    aid_off, _ = _two_donchian_hits(monkeypatch, ledger_on=False)
    assert [(t.pair, t.strategy_id, t.side) for t in all_trades(aid_off)] == picked_on and ledger(aid_off) == []


def test_an_rsi_tie_records_rsi_point_strengths(db, monkeypatch):
    settings = st(live_donchian=False)
    rig(monkeypatch, {"BTC/USDT": RSI_FLAT, "ETH/USDT": RSI_HIT, "BNB/USDT": RSI_HIT, "XRP/USDT": RSI_FLAT}, trade_pair="ETH/USDT")
    aid = auto_account(settings)
    run_cycle(aid, settings)
    assert [t.pair for t in all_trades(aid)] == ["ETH/USDT"]
    (skipped,) = [r for r in ledger(aid, "rsi_mr_v1") if r.reason_code == opportunity.R_NOT_BEST]
    d = json.loads(skipped.detail_json)
    assert skipped.pair == "BNB/USDT" and d["picked"] == "ETH/USDT" and isinstance(d["strength"], float) and isinstance(d["picked_strength"], float)


# ------------------------------------------------------------------------------------------------ analysis
def row(pair, candle_ts, *, outcome="signal", account="a1" * 18, strategy="donchian_breakout_v1", side="long", price=100.0, strength=1.0, picked=None, picked_strength=None, tf="1h"):
    return {"account_id": account, "strategy_id": strategy, "pair": pair, "candle_ts": candle_ts, "outcome": outcome, "side": side, "price": price, "market_type": "futures", "tf": tf,
            "strength": strength, "picked": picked, "picked_strength": picked_strength, "ts": datetime.now(U)}


def test_analyse_counts_tie_rounds_out_of_rounds_with_any_signal():
    t0 = 1_700_000_000_000
    rows = [row("BTC/USDT", t0), row("ETH/USDT", t0, strength=2.0), row("ETH/USDT", t0, outcome="not_taken", picked="BTC/USDT", picked_strength=1.0, strength=2.0),  # a tie
            row("XRP/USDT", t0 + HOUR),  # a lone signal
            row("BNB/USDT", t0, strategy="rsi_mr_v1", side="short"),  # a lone RSI signal on the same candle: another round
            row("BTC/USDT", t0 + 2 * HOUR, account="b2" * 18), row("ETH/USDT", t0 + 2 * HOUR, account="b2" * 18)]  # a tie on another account
    a = opportunity_ties.analyse(rows)
    assert a["rounds"] == 4 and len(a["ties"]) == 2
    first = next(t for t in a["ties"] if t["account_id"].startswith("a1"))
    assert first["picked"] == "BTC/USDT" and [(m["pair"], m["picked"]) for m in first["members"]] == [("BTC/USDT", True), ("ETH/USDT", False)]


def test_a_signal_seen_on_one_pair_only_is_never_a_tie_and_a_missing_candle_is_skipped():
    assert opportunity_ties.analyse([row("BTC/USDT", 1_700_000_000_000)]) == {"rounds": 1, "ties": []}
    assert opportunity_ties.analyse([row("BTC/USDT", None), row("ETH/USDT", None)])["ties"] == []


def test_forward_moves_long_and_short_in_the_signals_direction():
    tf = HOUR
    candles = [[i * tf, 100, 100 + h, 100 - l, c, 1] for i, (h, l, c) in enumerate([(3, 1, 102), (5, 2, 104), (4, 6, 99)])]
    now_ms = 10 * tf
    long = opportunity_ties.forward_moves("long", 100.0, candles, tf_ms=tf, now_ms=now_ms)
    assert long == {"n": 3, "best": pytest.approx(0.05), "worst": pytest.approx(-0.06), "at_end": pytest.approx(-0.01)}
    short = opportunity_ties.forward_moves("short", 100.0, candles, tf_ms=tf, now_ms=now_ms)
    assert short == {"n": 3, "best": pytest.approx(0.06), "worst": pytest.approx(-0.05), "at_end": pytest.approx(0.01)}


def test_a_candle_that_has_not_closed_is_not_counted_and_nothing_available_is_reported_as_zero():
    tf = HOUR
    candles = [[0, 100, 101, 99, 100, 1], [tf, 100, 150, 50, 100, 1]]
    mv = opportunity_ties.forward_moves("long", 100.0, candles, tf_ms=tf, now_ms=tf + 10)  # the second candle is still forming
    assert mv["n"] == 1 and mv["best"] == pytest.approx(0.01)
    assert opportunity_ties.forward_moves("long", 100.0, [], tf_ms=tf, now_ms=10 * tf) == {"n": 0, "best": None, "worst": None, "at_end": None}
    assert opportunity_ties.forward_moves(None, 100.0, candles, tf_ms=tf, now_ms=10 * tf)["best"] is None


# ------------------------------------------------------------------------------------------------ the report
def _seed(tmp_path, monkeypatch, *, ago_hours=40):
    url = f"sqlite:///{tmp_path}/r.db"
    init_db(url)
    monkeypatch.setenv("DATABASE_URL", url)
    now = datetime.now(U)
    candle = int((now - timedelta(hours=ago_hours)).replace(minute=0, second=0, microsecond=0).timestamp() * 1000)
    aid = "c3" * 18
    with session_scope() as s:
        def add(pair, outcome, reason, **detail):
            s.add(DecisionLog(ts=now - timedelta(hours=ago_hours), account_id=aid, mode="live", market_type="futures", pair=pair, strategy_id="donchian_breakout_v1", stage=opportunity.STAGE, outcome=outcome,
                              reason_code=reason, signal="long", price=100.0, candle_ts=candle, detail_json=json.dumps(dict(detail, tf="1h"))))
        add("ETH/USDT", "signal", "donchian_breakout_long", strength=3.0)
        add("XRP/USDT", "signal", "donchian_breakout_long", strength=6.0)
        add("XRP/USDT", "not_taken", opportunity.R_NOT_BEST, picked="ETH/USDT", picked_strength=3.0, strength=6.0)
        add("BTC/USDT", "signal", "donchian_breakout_long", strength=1.0)  # BTC alone on ANOTHER candle below
    with session_scope() as s:
        s.query(DecisionLog).filter(DecisionLog.pair == "BTC/USDT").one().candle_ts = candle + 5 * HOUR
    return now, candle, aid


def fake_fetch(calls, n=24):
    def fetch(market_type, pair, tf, since_ms, limit):
        calls.append((market_type, pair, tf, since_ms, limit))
        base = {"ETH/USDT": 1.0, "XRP/USDT": 2.0}[pair]
        return [[since_ms + i * HOUR, 100, 100 + base * (i + 1), 100 - base, 100 + base, 1] for i in range(n)]
    return fetch


def test_the_report_shows_the_count_at_the_top_each_pairs_strength_and_the_24_candle_moves(tmp_path, monkeypatch):
    now, candle, _ = _seed(tmp_path, monkeypatch)
    calls = []
    lines = opportunity_report.render(hours=72, now=now, fetch=fake_fetch(calls))
    assert lines[1].startswith("ties: 1 of 2 rounds with a Donchian or RSI signal had two or more pairs signalling the same strategy (50%)")  # right under the title
    text = "\n".join(lines)
    assert "picked ETH/USDT, skipped XRP/USDT" in text and "ETH/USDT (picked): long, strength 3" in text and "XRP/USDT (skipped): long, strength 6" in text
    assert "next 24 candles: best +24.00%, worst -1.00%, move at 24 +1.00%" in text  # ETH: base 1 -> best 100+24, worst 100-1, last close 101
    assert "best +48.00%, worst -2.00%, move at 24 +2.00%" in text  # XRP
    assert sorted(c[1] for c in calls) == ["ETH/USDT", "XRP/USDT"] and all(c[3] == candle + HOUR and c[4] == 24 for c in calls)  # from the candle AFTER the signal, 24 of them
    assert "not evidence that any ranking rule works" in text and "judged on new forward data" in text and "sealed window" in text


def test_fewer_than_24_candles_is_said_plainly_and_marked_partial(tmp_path, monkeypatch):
    now, _, _ = _seed(tmp_path, monkeypatch, ago_hours=40)
    text = "\n".join(opportunity_report.render(hours=72, now=now, fetch=fake_fetch([], n=3)))
    assert "ONLY 3 OF 24 candles exist yet, PARTIAL" in text and "move at candle 3" in text
    empty = "\n".join(opportunity_report.render(hours=72, now=now, fetch=lambda *a: []))
    assert "no candle has closed after the signal yet" in empty


def test_a_failed_candle_read_is_reported_by_class_only(tmp_path, monkeypatch):
    now, _, _ = _seed(tmp_path, monkeypatch)

    def broken(*a):
        raise RuntimeError("apiKey=SECRET1234567890123456789012345")

    text = "\n".join(opportunity_report.render(hours=72, now=now, fetch=broken))
    assert "candles could not be read (RuntimeError)" in text and "SECRET" not in text


def test_without_candles_no_exchange_call_is_made_and_the_report_says_how_to_ask(tmp_path, monkeypatch, capsys):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(opportunity_ties, "public_candles", lambda *a: (_ for _ in ()).throw(AssertionError("a candle read without --candles")))
    assert opportunity_report.main(["--hours", "72"]) == 0
    out = capsys.readouterr().out
    assert "price moves not read: add --candles" in out and "strength 6" in out


def test_with_the_candles_flag_the_public_reader_is_used(tmp_path, monkeypatch, capsys):
    _seed(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(opportunity_ties, "public_candles", fake_fetch(calls))
    assert opportunity_report.main(["--hours", "72", "--candles"]) == 0
    assert len(calls) == 2 and "next 24 candles" in capsys.readouterr().out


def test_the_public_reader_builds_a_client_with_no_key_and_closes_it(monkeypatch):
    seen = {}

    class FakeClient:
        def __init__(self, params):
            seen["params"] = params

        async def fetch_ohlcv(self, symbol, timeframe=None, since=None, limit=None):
            seen["call"] = (symbol, timeframe, since, limit)
            return [[since, 1, 2, 0.5, 1.5, 3]]

        async def close(self):
            seen["closed"] = True

    monkeypatch.setitem(exchange._EXCHANGE_CLASSES, ("binance", "futures"), FakeClient)
    rows = opportunity_ties.public_candles("futures", "XRP/USDT", "1h", 1_700_000_000_000, 24)
    assert rows and seen["params"] == {"enableRateLimit": True} and "apiKey" not in seen["params"] and "secret" not in seen["params"]
    assert seen["call"] == ("XRP/USDT:USDT", "1h", 1_700_000_000_000, 25) and seen["closed"] is True


def test_the_report_changes_nothing(tmp_path, monkeypatch):
    now, _, _ = _seed(tmp_path, monkeypatch)
    with session_scope() as s:
        before = s.query(DecisionLog).count()
    opportunity_report.render(hours=72, now=now, fetch=fake_fetch([]))
    with session_scope() as s:
        assert s.query(DecisionLog).count() == before


def test_the_trading_code_never_imports_the_tie_module():
    importers = [p.name for p in SRC.rglob("*.py") if "opportunity_ties" in p.read_text() and p.name not in ("opportunity_ties.py", "opportunity_report.py")]
    assert importers == []
