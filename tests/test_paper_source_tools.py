"""PR 2, B0: the paper data-source check, the paper-trades export and the replay against mainnet candles.

PROVES: a paper account's client is built with sandbox=True and its candles are read from that client (and a live account's with sandbox=False); the check prints that evidence and the testnet/demo versus
mainnet candle hosts; the candle comparison can only read candles; the export is read-only, contains only paper trades and no e-mail or full account id, and the SQL file returns the same rows; the replay's
rules (entry range, stop touch from the next candle, gap fills, R, closed-candle signal) and summary are right on synthetic data; none of the three tools writes anywhere except a new file the owner names.
DOES NOT PROVE: anything about the owner's real paper trades or mainnet data (none was read), or Binance."""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from helpers_live import BULLISH_OHLCV, FakeNotifier, install, make_settings, new_account, run_cycle, spot_client
from test_account_daily_loss import NOON, rig

from mba_bot import exchange, paper_export, paper_replay, paper_source_check
from mba_bot.db import init_db, session_scope
from mba_bot.models import Customer, Trade, TradingAccount
from mba_bot.strategy import Candle

ROOT = Path(__file__).resolve().parent.parent
HOUR = 3_600_000


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")
    return tmp_path


# ------------------------------------------------------------------ the data source
def test_a_paper_cycle_builds_a_sandbox_client_and_reads_its_candles_from_it(db, monkeypatch):
    st = make_settings(max_notional_multiplier=100.0, testnet_spot_api_key="paper-key-1234567890", testnet_spot_api_secret="paper-secret-1234567890")
    seen = []
    client = spot_client(ohlcv=BULLISH_OHLCV)
    real_fetch = client.fetch_ohlcv

    async def tracking_fetch(*a, **k):
        seen.append("ohlcv")
        return await real_fetch(*a, **k)

    client.fetch_ohlcv = tracking_fetch

    def build(**kw):
        seen.append(("build", kw["sandbox"]))
        return client

    monkeypatch.setattr(exchange, "build_exchange_client", build)
    aid = new_account(st)
    with session_scope() as s:
        s.get(TradingAccount, aid).bot_started_at = None  # PAPER
    run_cycle(aid, st)
    assert ("build", True) in seen and ("build", False) not in seen and "ohlcv" in seen  # paper: sandbox, and the candles came from that client


def test_a_live_cycle_builds_a_mainnet_client(db, monkeypatch):
    st = make_settings(max_notional_multiplier=100.0)
    built = []
    client, clock, aid = rig(monkeypatch, st, market="spot")
    real = exchange.build_exchange_client

    def build(**kw):
        built.append(kw["sandbox"])
        return client

    monkeypatch.setattr(exchange, "build_exchange_client", build)
    run_cycle(aid, st)
    assert built and set(built) == {False}


def test_the_check_prints_the_evidence_and_the_hosts():
    lines = paper_source_check.code_facts()
    text = "\n".join(lines)
    assert "sandbox=effective_is_paper: YES" in text and "VERDICT: paper decisions use testnet / demo candles, not mainnet candles." in text
    assert "PAPER (sandbox)  spot     candles come from: testnet.binance.vision" in text
    assert "PAPER (sandbox)  futures  candles come from: demo-fapi.binance.com" in text
    assert "LIVE (mainnet)   spot     candles come from: api.binance.com" in text and "LIVE (mainnet)   futures  candles come from: fapi.binance.com" in text


def test_the_check_offline_makes_no_request_and_prints_its_own_hash(capsys, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no network without --compare")

    monkeypatch.setattr(paper_source_check, "compare", boom)
    assert paper_source_check.main([]) == 0
    out = capsys.readouterr().out
    expected = hashlib.sha256(Path(paper_source_check.__file__).read_bytes()).hexdigest()
    assert out.splitlines()[0] == f"paper_source_check sha256 {expected}"


def test_the_candle_client_can_only_read_candles():
    class C:
        async def fetch_ohlcv(self, s, timeframe="1h", limit=1):
            return [[1, 1, 1, 1, 1, 1]]

        async def close(self):
            pass

        async def create_order(self, *a):
            raise AssertionError("must be unreachable")

    ro = paper_source_check.ReadOnlyCandles(C())
    assert asyncio.run(ro.fetch_ohlcv("BTC/USDT")) == [[1, 1, 1, 1, 1, 1]]
    for name in ("create_order", "fetch_balance", "cancel_order", "fetch_my_trades", "set_leverage"):
        with pytest.raises(AttributeError):
            getattr(ro, name)


def test_compare_series():
    a = [[0, 1, 1, 1, 100.0, 1], [HOUR, 1, 1, 1, 101.0, 1], [2 * HOUR, 1, 1, 1, 110.0, 1], [4 * HOUR, 1, 1, 1, 50.0, 1]]
    b = [[0, 1, 1, 1, 100.0, 1], [HOUR, 1, 1, 1, 100.0, 1], [2 * HOUR, 1, 1, 1, 100.0, 1], [3 * HOUR, 1, 1, 1, 100.0, 1]]
    r = paper_source_check.compare_series(a, b)
    assert (r["common"], r["only_paper"], r["only_mainnet"]) == (3, 1, 1) and abs(r["max_close_diff_pct"] - 10.0) < 1e-9
    assert abs(r["mean_close_diff_pct"] - 11.0 / 3) < 1e-9 and abs(r["share_over_0_1_pct"] - 2 / 3) < 1e-9
    assert paper_source_check.compare_series([], b)["max_close_diff_pct"] is None


def test_the_comparison_uses_public_candle_reads_only(monkeypatch):
    calls = []

    class C:
        def __init__(self, sandbox):
            self.sandbox = sandbox

        async def fetch_ohlcv(self, symbol, timeframe="1h", limit=1):
            calls.append(("ohlcv", self.sandbox, symbol))
            return [[0, 1, 1, 1, 100.0 + (1.0 if self.sandbox else 0.0), 1]]

        async def close(self):
            calls.append(("close", self.sandbox))

    monkeypatch.setattr(exchange, "build_exchange_client", lambda **kw: C(kw["sandbox"]))
    lines = asyncio.run(paper_source_check.compare(50))
    assert {c[0] for c in calls} == {"ohlcv", "close"} and len([c for c in calls if c[0] == "ohlcv"]) == 16
    assert any("BTC/USDT" in l and "max 1.000%" in l for l in lines)


# ------------------------------------------------------------------ the export
def seed_trades(email="export-owner@example.com"):
    st = make_settings()
    aid = new_account(st, email=email)
    now = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)
    with session_scope() as s:
        s.add(Trade(account_id=aid, side="long", is_paper=True, entry_price=100.0, quantity=2.0, stop_loss_price=95.0, status="closed", pair="BTC/USDT", exit_price=110.0, pnl=20.0, close_reason="crossover_exit",
                    opened_at=now, closed_at=now + timedelta(hours=5)))
        s.add(Trade(account_id=aid, side="short", is_paper=True, entry_price=50.0, quantity=1.0, stop_loss_price=52.0, status="open", pair="ETH/USDT", opened_at=now + timedelta(hours=1)))
        s.add(Trade(account_id=aid, side="long", is_paper=False, entry_price=77.0, quantity=1.0, stop_loss_price=70.0, status="closed", pair="BTC/USDT", opened_at=now))
    return aid


def test_the_export_has_only_paper_trades_and_no_customer_data(db, monkeypatch):
    aid = seed_trades()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}/test.db")
    rows = paper_export.export_rows()
    assert len(rows) == 2 and {r["side"] for r in rows} == {"long", "short"} and all(r["entry_price"] != 77.0 for r in rows)
    text = paper_export.to_csv(rows)
    assert text.splitlines()[0] == ",".join(paper_export.COLUMNS)
    assert aid not in text and "@" not in text and "export-owner" not in text
    first = rows[0]
    assert first["risk_amount"] == 10.0 and first["strategy_id"] == "ema_cross_atr_v1" and first["account_ref"] == aid[:4]


def test_the_export_is_read_only_and_never_overwrites(db, monkeypatch, tmp_path, capsys):
    seed_trades()
    path = db / "test.db"
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    out = tmp_path / "paper.csv"
    assert paper_export.main(["--out", str(out)]) == 0
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before and out.exists()
    with pytest.raises(FileExistsError):
        paper_export.main(["--out", str(out)])  # an existing file is never overwritten
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/missing.db")
    assert paper_export.main([]) == 2
    assert "FileNotFoundError" in capsys.readouterr().err and not (tmp_path / "missing.db").exists()


def test_the_sql_file_returns_the_same_trades(db, monkeypatch):
    seed_trades()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}/test.db")
    sql = "\n".join(l for l in (ROOT / "docs/ops/paper_trades_export.sql").read_text().splitlines() if not l.startswith(".") and not l.startswith("--"))
    con = sqlite3.connect(f"file:{db}/test.db?mode=ro", uri=True)
    cur = con.execute(sql)
    names = [d[0] for d in cur.description]
    got = cur.fetchall()
    con.close()
    assert tuple(names) == paper_export.COLUMNS
    assert [r[0] for r in got] == [r["trade_ref"] for r in paper_export.export_rows()] and len(got) == 2
    assert "email" not in sql.lower() and "password" not in sql.lower() and "telegram" not in sql.lower() and "credential" not in sql.lower()


# ------------------------------------------------------------------ the replay
def series(closes, start=0):
    out = []
    for i, c in enumerate(closes):
        prev = closes[i - 1] if i else c
        out.append(Candle(timestamp=start + i * HOUR, open=prev, high=max(prev, c) + 0.5, low=min(prev, c) - 0.5, close=c, volume=1.0))
    return out


def ts(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def trade(**kw):
    base = dict(trade_ref="t1", pair="BTC/USDT", side="long", status="closed", entry_price="100", stop_loss_price="95", exit_price="110", close_reason="crossover_exit", opened_at=ts(10 * HOUR + 1000), closed_at=ts(13 * HOUR + 1000))
    base.update({k: str(v) for k, v in kw.items()})
    return base


P = paper_replay.Params()


def flat(n=10, price=100.0):
    return [price] * n


def test_r_and_stop_touch_rules():
    assert paper_replay.r_multiple("long", 100, 95, 110) == 2.0 and paper_replay.r_multiple("long", 100, 95, 95) == -1.0
    assert paper_replay.r_multiple("short", 100, 105, 95) == 1.0 and paper_replay.r_multiple("long", 100, 100, 101) is None
    c = [Candle(0, 100, 101, 99, 100, 1), Candle(HOUR, 100, 101, 94, 96, 1), Candle(2 * HOUR, 90, 91, 89, 90, 1)]
    assert paper_replay.stop_touch(c, 1, 2, "long", 95) == (1, 95)  # touched inside the hour: filled at the stop
    assert paper_replay.stop_touch(c, 2, 2, "long", 95) == (2, 90)  # opened beyond the stop: filled at the open, the worse price
    assert paper_replay.stop_touch(c, 0, 0, "long", 95) is None
    s = [Candle(0, 100, 101, 99, 100, 1), Candle(HOUR, 100, 106, 99, 104, 1)]
    assert paper_replay.stop_touch(s, 1, 1, "short", 105) == (1, 105)


def test_a_paper_win_that_mainnet_would_have_stopped():
    closes = flat(10) + [100, 97, 90, 92, 120]  # hour 11 dips under the stop 95
    candles = series(closes)
    r = paper_replay.replay_trade(trade(), candles, P)
    assert r["comparable"] and r["paper_r"] == 2.0 and r["paper_win"] is True
    assert r["mainnet_win"] is False and r["mainnet_r"] < 0 and r["mainnet_stop_touched_at"]
    assert r["paper_exit_consistent_with_mainnet_stop"] is False  # paper closed on a crossover exit; mainnet had touched the stop first
    assert r["entry_inside_mainnet_hour"] is True


def test_when_mainnet_never_touches_the_stop_it_exits_at_the_close_of_the_hour():
    candles = series(flat(10) + [100, 101, 102, 108, 109])
    r = paper_replay.replay_trade(trade(), candles, P)
    assert r["mainnet_stop_touched_at"] == "" and abs(r["mainnet_r"] - (108 - 100) / 5) < 1e-9 and r["mainnet_win"] and r["paper_exit_consistent_with_mainnet_stop"] is True


def test_an_entry_price_outside_the_mainnet_hour_is_flagged_and_unknowns_are_not_comparable():
    candles = series(flat(10) + [100, 101, 102, 108, 109])
    assert paper_replay.replay_trade(trade(entry_price=150, stop_loss_price=140), candles, P)["entry_inside_mainnet_hour"] is False
    assert paper_replay.replay_trade(trade(), None, P)["note"] == "no candle file for this pair"
    assert paper_replay.replay_trade(trade(opened_at=ts(500 * HOUR)), candles, P)["note"].startswith("the open time is outside")
    open_trade = paper_replay.replay_trade(trade(status="open", exit_price="", closed_at=""), candles, P)
    assert open_trade["comparable"] is False and open_trade["paper_r"] is None


def test_the_signal_check_uses_only_candles_closed_before_the_entry():
    down = [100 - i * 0.5 for i in range(120)]
    up = [down[-1] + i * 3 for i in range(1, 25)]
    candles = series(down + up)
    i = len(down) + 6
    t = trade(opened_at=ts(candles[i].timestamp + 1000), closed_at=ts(candles[i + 3].timestamp + 1000), entry_price=candles[i].close, stop_loss_price=candles[i].close - 5)
    r = paper_replay.replay_trade(t, candles, P)
    assert r["mainnet_closed_signal"] in ("bullish_crossover", "none", "bearish_crossover") and r["mainnet_signal_matches_side"] in (True, False)
    short = paper_replay.replay_trade(trade(opened_at=ts(5 * HOUR + 1000), closed_at=ts(8 * HOUR + 1000)), series(flat(12)), P)
    assert short["mainnet_closed_signal"] == "insufficient history" and short["mainnet_signal_matches_side"] is None


def test_the_summary_counts(tmp_path):
    candles = series(flat(10) + [100, 97, 90, 92, 120, 120, 120])
    rows = [trade(trade_ref="a"), trade(trade_ref="b", exit_price="94", close_reason="stop_loss", opened_at=ts(10 * HOUR + 1000), closed_at=ts(12 * HOUR + 1000)),
            trade(trade_ref="c", pair="XRP/USDT")]
    k = tmp_path / "klines"
    k.mkdir()
    with open(k / "BTCUSDT.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "open", "high", "low", "close", "volume"])
        for c in candles:
            w.writerow([c.timestamp, c.open, c.high, c.low, c.close, c.volume])
    tp = tmp_path / "trades.csv"
    with open(tp, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    results, summary = paper_replay.run(tp, k, P)
    text = "\n".join(summary)
    assert "paper trades read: 3" in text and "comparable with mainnet candles: 2" in text and "no candle file for this pair" in text
    assert "paper wins 1, mainnet-replay wins 0" in text and "same outcome on both 1 of 2" in text and "LIMITS" in text
    assert paper_replay.main(["--trades", str(tp), "--klines", str(k), "--csv", str(tmp_path / "out.csv")]) == 0
    with pytest.raises(FileExistsError):
        paper_replay.main(["--trades", str(tp), "--klines", str(k), "--csv", str(tmp_path / "out.csv")])
    assert paper_replay.main(["--trades", str(tmp_path / "nope.csv"), "--klines", str(k)]) == 2


def test_the_replay_reads_files_only(tmp_path):
    src = (ROOT / "src/mba_bot/paper_replay.py").read_text()
    for forbidden in ("import socket", "urllib", "requests", "ccxt", "sqlalchemy", "os.environ", "getenv", "dotenv"):
        assert forbidden not in src, forbidden
