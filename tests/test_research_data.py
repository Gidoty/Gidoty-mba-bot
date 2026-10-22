"""Research data layer: the public-only downloader, the local store and the quality gate.

The downloader is driven through an injected fake transport that serves a generated
store in Binance's wire format; nothing here touches the network."""
from __future__ import annotations

import ast
import csv
import json
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mba_bot.research import data, quality, synthetic
from mba_bot.research.data import DownloadError, PublicHTTP

HOUR = 3_600_000
NOW = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)  # half way through the 00:00 hourly bar
SYMBOLS = ["BTCUSDT", "SOLUSDT"]


class FakeBinance:
    """Serves klines/funding/exchangeInfo for generated symbols and records every request."""

    def __init__(self, tmp_path, *, days=30):
        self.src = tmp_path / "source"
        synthetic.write_store(self.src, pairs=["BTC/USDT", "SOL/USDT"], days=days, seed=3, with_5m=True, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.urls: list[str] = []
        self.script: list[tuple[int, dict]] = []  # queued (status, headers) overrides for the next calls

    def __call__(self, url, timeout):
        self.urls.append(url)
        if self.script:
            status, headers = self.script.pop(0)
            return status, headers, b"[]"
        parsed = urllib.parse.urlparse(url)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        path = parsed.path
        if path == "/fapi/v1/time":
            return 200, {}, b'{"serverTime": 1767225600000}'
        if path == "/fapi/v1/exchangeInfo":
            return 200, {}, json.dumps(synthetic.exchange_info_for(["BTCUSDT", "SOLUSDT", "ETHUSDT"])).encode()
        if path in ("/fapi/v1/klines", "/fapi/v1/premiumIndexKlines"):
            interval = q["interval"]
            step = data.INTERVAL_MS[interval]
            src = data.kline_path(self.src, interval, q["symbol"]) if path.endswith("/klines") and "premium" not in path else data.premium_path(self.src, interval, q["symbol"])
            out = []
            with open(src) as fh:
                for r in csv.DictReader(fh):
                    ts = int(r["ts"])
                    if ts >= int(q["startTime"]):
                        vol = r.get("volume", "0")
                        out.append([ts, r["open"], r["high"], r["low"], r["close"], vol, ts + step - 1, r.get("quote_volume", "0"), int(r.get("trades", 0) or 0), r.get("taker_buy_volume", "0"), "0", "0"])
                    if len(out) >= int(q["limit"]):
                        break
            return 200, {}, json.dumps(out).encode()
        if path == "/fapi/v1/fundingRate":
            out = []
            with open(data.funding_path(self.src, q["symbol"])) as fh:
                for r in csv.DictReader(fh):
                    if int(r["ts"]) >= int(q["startTime"]):
                        out.append({"symbol": q["symbol"], "fundingTime": int(r["ts"]), "fundingRate": r["rate"], "markPrice": r["mark_price"]})
                    if len(out) >= int(q["limit"]):
                        break
            return 200, {}, json.dumps(out).encode()
        return 404, {}, b"{}"


def http_for(fake, sleeps=None):
    return PublicHTTP(opener=fake, sleep=(sleeps.append if sleeps is not None else (lambda s: None)), min_interval=0)


# ------------------------------------------------------------------ safety


@pytest.mark.parametrize("path", ["/fapi/v1/order", "/fapi/v2/account", "/fapi/v1/leverage", "/sapi/v1/capital/withdraw/apply", "/fapi/v1/listenKey", "/api/v3/order"])
def test_only_public_market_data_endpoints_can_be_called(path, tmp_path):
    fake = FakeBinance(tmp_path, days=2)
    with pytest.raises(DownloadError):
        http_for(fake).get(path, {"symbol": "BTCUSDT"})
    assert fake.urls == [], "a refused path must not even reach the transport"


def test_a_full_download_only_ever_sends_plain_unsigned_https_gets_to_the_futures_host(tmp_path):
    fake = FakeBinance(tmp_path, days=5)
    data.download_all(tmp_path / "store", pairs=["BTC/USDT", "SOL/USDT"], years=0.01, intervals=("1h", "5m"), http=http_for(fake), now=NOW)
    assert fake.urls
    for url in fake.urls:
        p = urllib.parse.urlparse(url)
        assert p.scheme == "https" and p.netloc == "fapi.binance.com" and p.path in data.ALLOWED_PATHS
        for forbidden in ("signature", "timestamp=", "apikey", "recvwindow"):
            assert forbidden not in url.lower()


def test_the_downloader_source_has_no_credential_order_or_environment_access():
    tree = ast.parse(Path(data.__file__).read_text())
    for node in ast.walk(tree):  # documentation may name what is forbidden; code may not use it
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and node.body and isinstance(node.body[0], ast.Expr) and isinstance(getattr(node.body[0], "value", None), ast.Constant) and isinstance(node.body[0].value.value, str):
            node.body = node.body[1:] or [ast.Pass()]
    source = ast.unparse(tree)
    for needle in ("X-MBX-APIKEY", "signature", "create_order", "os.environ", "getenv", "dotenv", "ENCRYPTION_KEY", "api_secret", "hmac"):
        assert needle not in source, needle
    internal = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level > 0]
    assert internal == ["strategy"], f"data.py may only import the Candle type, got {internal}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(a.name.split(".")[0] in {"ccxt", "sqlalchemy"} for a in node.names)


# ------------------------------------------------------------------ downloading


def test_paging_resume_and_idempotence(tmp_path):
    fake = FakeBinance(tmp_path, days=200)  # 4800 hourly bars -> 4 pages of 1500
    root = tmp_path / "store"
    now_ms = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    start_ms = now_ms - 4800 * HOUR
    res = data.download_klines(http_for(fake), root, "BTCUSDT", "1h", start_ms=start_ms, now_ms=now_ms)
    assert res.new_rows == res.total_rows == 4800
    assert len([u for u in fake.urls if "klines" in u]) == 4
    again = data.download_klines(http_for(fake), root, "BTCUSDT", "1h", start_ms=start_ms, now_ms=now_ms)
    assert again.new_rows == 0 and again.total_rows == 4800

    # Resume: pretend the file stopped 100 bars early.
    path = data.kline_path(root, "1h", "BTCUSDT")
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[:-100]) + "\n")
    resumed = data.download_klines(http_for(fake), root, "BTCUSDT", "1h", start_ms=start_ms, now_ms=now_ms)
    assert resumed.new_rows == 100 and resumed.total_rows == 4800
    candles = data.load_candles(root, "BTCUSDT", "1h")
    assert [c.timestamp for c in candles] == sorted({c.timestamp for c in candles}) and len(candles) == 4800


def test_a_bar_that_is_still_forming_is_never_stored(tmp_path):
    fake = FakeBinance(tmp_path, days=3)
    root = tmp_path / "store"
    # NOW is 00:30, so the 00:00 bar (open) is forming; the source store ends at 23:00 of the day before.
    now_ms = int(NOW.timestamp() * 1000)
    # Make the fake serve a bar for the forming hour as well.
    src = data.kline_path(fake.src, "1h", "BTCUSDT")
    last = src.read_text().splitlines()[-1].split(",")
    forming_ts = int(last[0]) + HOUR
    with src.open("a") as fh:
        fh.write(",".join([str(forming_ts)] + last[1:]) + "\n")
    data.download_klines(http_for(fake), root, "BTCUSDT", "1h", start_ms=now_ms - 3 * 86_400_000, now_ms=now_ms)
    stored = data.load_candles(root, "BTCUSDT", "1h")
    assert stored and stored[-1].timestamp == forming_ts - HOUR
    assert all(c.timestamp + HOUR <= now_ms for c in stored)


def test_funding_history_pages_and_resumes(tmp_path):
    fake = FakeBinance(tmp_path, days=400)  # ~1200 funding events -> two pages
    root = tmp_path / "store"
    now_ms = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    res = data.download_funding(http_for(fake), root, "SOLUSDT", start_ms=now_ms - 400 * 86_400_000, now_ms=now_ms)
    assert res.total_rows >= 1190 and res.new_rows == res.total_rows
    assert data.download_funding(http_for(fake), root, "SOLUSDT", start_ms=now_ms - 400 * 86_400_000, now_ms=now_ms).new_rows == 0
    f = data.load_funding(root, "SOLUSDT")
    assert all(b[0] > a[0] for a, b in zip(f, f[1:]))


def test_rate_limits_are_waited_out_and_honour_retry_after(tmp_path):
    fake = FakeBinance(tmp_path, days=2)
    fake.script = [(429, {"Retry-After": "7"}), (418, {"retry-after": "3"})]
    sleeps: list[float] = []
    http = http_for(fake, sleeps)
    assert http.get("/fapi/v1/time") == {"serverTime": 1767225600000}
    assert 7.0 in sleeps and 3.0 in sleeps


def test_repeated_server_errors_give_up_instead_of_looping_forever(tmp_path):
    fake = FakeBinance(tmp_path, days=2)
    fake.script = [(503, {})] * 20
    with pytest.raises(DownloadError):
        http_for(fake).get("/fapi/v1/klines", {"symbol": "BTCUSDT", "interval": "1h", "startTime": 0, "limit": 5})


def test_exchange_info_snapshot_keeps_only_requested_symbols_and_the_filters(tmp_path):
    fake = FakeBinance(tmp_path, days=2)
    out = data.snapshot_exchange_info(http_for(fake), tmp_path / "store", ["BTCUSDT", "SOLUSDT"], today="2026-01-01")
    saved = json.loads(out.read_text())
    assert sorted(s["symbol"] for s in saved["symbols"]) == ["BTCUSDT", "SOLUSDT"]
    info = data.load_exchange_info(tmp_path / "store")
    assert {f["filterType"] for f in info["BTCUSDT"]["filters"]} >= {"LOT_SIZE", "PRICE_FILTER", "MIN_NOTIONAL"}


def test_manifest_records_hashes_that_change_with_the_data(tmp_path):
    fake = FakeBinance(tmp_path, days=40)
    root = tmp_path / "store"
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    data.download_all(root, pairs=["BTC/USDT"], years=0.05, http=http_for(fake), now=now)
    m1 = json.loads((root / "manifest.json").read_text())
    entry = m1["files"]["klines/1h/BTCUSDT.csv"]
    assert entry["rows"] > 0 and len(entry["sha256"]) == 64 and entry["first_ts"] < entry["last_ts"]
    with data.kline_path(root, "1h", "BTCUSDT").open("a") as fh:
        fh.write("1,1,1,1,1,1,1,1,1\n")
    m2 = data.write_manifest(root, now=now)
    assert m2["files"]["klines/1h/BTCUSDT.csv"]["sha256"] != entry["sha256"]


# ------------------------------------------------------------------ quality gate


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "q"
    synthetic.write_store(root, pairs=["BTC/USDT"], days=120, seed=11, with_5m=False)
    return root


def test_clean_data_passes(store):
    report = quality.check_store(store, pairs=["BTC/USDT"], min_years=0.3)
    assert report["summary"] == {"ok": 3, "warn": 0, "fail": 0}
    assert (store / "quality.json").exists()


def test_a_negative_premium_index_is_normal_not_corruption(store):
    path = data.premium_path(store, "1h", "BTCUSDT")
    rows = path.read_text().splitlines()
    parts = rows[5].split(",")
    rows[5] = ",".join([parts[0], "-0.002", "-0.0015", "-0.0025", "-0.002"])
    path.write_text("\n".join(rows) + "\n")
    q = quality.check_klines(path, "1h", kind="premium", min_years=0.3)
    assert q.status == "ok", q.notes


def test_gaps_duplicates_bad_ohlc_and_short_history_are_each_detected(store):
    path = data.kline_path(store, "1h", "BTCUSDT")
    lines = path.read_text().splitlines()
    gapped = lines[:50] + lines[53:]  # drop 3 bars
    path.write_text("\n".join(gapped) + "\n")
    q = quality.check_klines(path, "1h", min_years=0.3)
    assert q.status == "warn" and q.missing_bars == 3 and q.gaps[0][1] == 3

    dup = gapped[:80] + [gapped[80]] + gapped[80:]
    path.write_text("\n".join(dup) + "\n")
    assert quality.check_klines(path, "1h", min_years=0.3).status == "fail"

    cells = lines[10].split(",")
    cells[2], cells[3] = "1.0", "999999.0"  # high below low
    broken = lines[:10] + [",".join(cells)] + lines[11:]
    path.write_text("\n".join(broken) + "\n")
    q = quality.check_klines(path, "1h", min_years=0.3)
    assert q.status == "fail" and q.bad_ohlc >= 1

    path.write_text("\n".join(lines) + "\n")
    short = quality.check_klines(path, "1h", min_years=3.0)
    assert short.status == "warn" and any("target 3.0" in n for n in short.notes)


def test_missing_and_empty_files_fail(tmp_path):
    assert quality.check_klines(tmp_path / "nope.csv", "1h").status == "fail"
    (tmp_path / "e.csv").write_text("")
    assert quality.check_funding(tmp_path / "e.csv").status == "fail"


def test_render_is_a_readable_summary(store):
    text = quality.render(quality.check_store(store, pairs=["BTC/USDT"], min_years=0.3))
    assert "3 ok" in text and "BTCUSDT" in text


def test_the_download_command_is_wired_to_the_public_downloader(tmp_path, monkeypatch, capsys):
    from mba_bot.research.__main__ import main as cli

    fake = FakeBinance(tmp_path, days=20)
    monkeypatch.setattr(data, "PublicHTTP", lambda: http_for(fake))
    root = tmp_path / "dl"
    assert cli(["download", "--root", str(root), "--years", "1.5", "--pairs", "BTC/USDT", "--hourly-only"]) == 0
    assert data.load_candles(root, "BTCUSDT", "1h") and data.load_funding(root, "BTCUSDT") and (root / "manifest.json").exists()
    assert not data.kline_path(root, "5m", "BTCUSDT").exists(), "--hourly-only skips 5-minute bars"
    assert all(urllib.parse.urlparse(u).path in data.ALLOWED_PATHS for u in fake.urls)
    assert "manifest:" in capsys.readouterr().out
