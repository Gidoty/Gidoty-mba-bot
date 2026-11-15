"""The files the downloader writes are exactly the files the two offline backtests read.

The test runs the owner's command (src/mba_bot/research/__main__.py download ...) against a FAKE transport that serves generated candles, so nothing touches the
network, then feeds the written files to rsi_backtest and donchian_backtest: their loaders, their data hash, their window check, and a full tuning run of each."""
from __future__ import annotations

import csv
import time
from pathlib import Path

import pytest
from test_research_data import HOUR, FakeBinance, http_for

from mba_bot import donchian_backtest as DB
from mba_bot import rsi_backtest as RB
from mba_bot.research import data
from mba_bot.research.__main__ import main as cli

PAIRS = "BTC/USDT,ETH/USDT,BNB/USDT,XRP/USDT"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT")
# the owner's command (see docs/research/downloading_candles.md)
OWNER_ARGS = ["download", "--years", "0.6", "--pairs", PAIRS, "--hourly-only", "--no-funding", "--no-premium"]


@pytest.fixture
def downloaded(tmp_path, monkeypatch):
    end_ms = int(time.time() * 1000) // HOUR * HOUR  # the last bar has just closed
    fake = FakeBinance(days=235, symbols=SYMBOLS, end_ms=end_ms)  # a little more history than 0.6 years (219 days) asks for
    monkeypatch.setattr(data, "PublicHTTP", lambda: http_for(fake))
    root = tmp_path / "research_data"
    assert cli(OWNER_ARGS[:1] + ["--root", str(root)] + OWNER_ARGS[1:]) == 0
    return root, fake


def test_the_command_writes_only_the_1h_files_the_backtests_read_and_a_manifest(downloaded):
    root, fake = downloaded
    assert sorted(p.name for p in (root / "klines" / "1h").glob("*.csv")) == sorted(f"{s}.csv" for s in SYMBOLS)
    assert {p.name for p in root.iterdir()} == {"klines", "meta", "manifest.json"}  # no 5m, funding or premium
    with (root / "klines" / "1h" / "BTCUSDT.csv").open() as fh:
        assert next(csv.reader(fh)) == ["ts", "open", "high", "low", "close", "volume", "quote_volume", "trades", "taker_buy_volume"]


def test_both_runners_load_exactly_what_was_written(downloaded):
    root, fake = downloaded
    for sym in SYMBOLS:
        written = fake.bars[sym]
        for loader in (RB.load_candles, DB.RB.load_candles):
            candles = loader(root, sym)
            assert len(candles) > 5000
            assert [c.timestamp for c in candles] == sorted({c.timestamp for c in candles})  # strictly increasing, no duplicates
            by_ts = {row[0]: row for row in written}
            for c in candles[:50] + candles[-50:]:
                _, o, h, l, cl, v = by_ts[c.timestamp]
                assert (c.open, c.high, c.low, c.close, c.volume) == (o, h, l, cl, v)
            assert all(b.timestamp - a.timestamp == HOUR for a, b in zip(candles, candles[1:]))  # no gaps in the generated data
    assert RB.kline_path(root, "BTCUSDT") == data.kline_path(root, "1h", "BTCUSDT")  # one layout, one path function


def test_the_data_hash_and_the_window_check_accept_the_six_month_download(downloaded):
    root, _ = downloaded
    assert len(RB.data_hash(root)) == 16
    loaded = RB.load_all(root)
    assert set(loaded) == set(RB.PAIRS) == set(SYMBOLS)
    w = RB.windows(loaded)  # needs 6 months plus 14 days of warm-up: raises if the download is too short
    assert w["tune"][0] < w["tune"][1] < w["sealed"][0] < w["sealed"][1]


def test_a_shorter_download_is_refused_by_the_runners_so_years_0_6_is_the_right_command(tmp_path, monkeypatch):
    end_ms = int(time.time() * 1000) // HOUR * HOUR
    fake = FakeBinance(days=235, symbols=SYMBOLS, end_ms=end_ms)
    monkeypatch.setattr(data, "PublicHTTP", lambda: http_for(fake))
    root = tmp_path / "short"
    assert cli(["download", "--root", str(root), "--years", "0.45", "--pairs", PAIRS, "--hourly-only", "--no-funding", "--no-premium"]) == 0
    with pytest.raises(ValueError, match="less than 6 months"):
        RB.windows(RB.load_all(root))


def test_each_runner_runs_a_full_tuning_on_the_written_files(downloaded, tmp_path):
    root, _ = downloaded
    rsi = RB.run_tune(root, tmp_path / "rsi_out")
    don = DB.run_tune(root, tmp_path / "don_out")
    assert rsi["plan_hash"] == RB.plan_hash() and don["plan_hash"] == DB.plan_hash()
    assert rsi["data_hash"] == don["data_hash"] == RB.data_hash(root)
    assert (tmp_path / "rsi_out" / "tune_report.md").exists() and (tmp_path / "don_out" / "tune_report.md").exists()


def test_the_download_makes_few_requests_and_all_are_public_gets(downloaded):
    _, fake = downloaded
    # 4 symbols x ceil(5256 bars / 1500) = 16 klines calls, plus one exchangeInfo
    assert len(fake.urls) <= 4 * 4 + 1 + 4
    assert all(u.startswith("https://fapi.binance.com/fapi/v1/") for u in fake.urls)


def test_the_documented_command_is_the_one_tested():
    doc = (Path(__file__).resolve().parent.parent / "docs" / "research" / "downloading_candles.md").read_text()
    assert '--root research_data --years 0.6 --pairs "BTC/USDT,ETH/USDT,BNB/USDT,XRP/USDT" --hourly-only --no-funding --no-premium' in doc
    assert PAIRS in doc and "--years" in OWNER_ARGS and "0.6" in OWNER_ARGS
