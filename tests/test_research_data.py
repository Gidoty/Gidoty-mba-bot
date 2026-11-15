"""The research data downloader (src/mba_bot/research/data.py) and its command line.

Adapted from the research branch's tests/test_research_data.py: the safety pins and the download behaviour are kept; the quality-gate tests are left behind with
quality.py; the fake exchange generates its own deterministic candles instead of importing the branch's synthetic module. NOTHING here touches the network: the
downloader is driven through an injected transport that serves generated data in Binance's wire format and records every request."""
from __future__ import annotations

import ast
import csv
import json
import random
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mba_bot.research import data
from mba_bot.research.data import DownloadError, PublicHTTP

HOUR = 3_600_000
NOW = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)  # half way through the 00:00 hourly bar
END_MS = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)  # the last CLOSED bar ends here
START_PRICE = {"BTCUSDT": 40000.0, "ETHUSDT": 2500.0, "BNBUSDT": 350.0, "XRPUSDT": 0.6, "SOLUSDT": 100.0}


def generate_hourly(symbol: str, *, bars: int, end_ms: int = END_MS, seed: int = 1) -> list[list]:
    """Deterministic hourly klines [open_ts, o, h, l, c, volume] ending at end_ms (the last bar opens one hour before it)."""
    rng = random.Random(f"{symbol}:{seed}")
    price, out = START_PRICE.get(symbol, 100.0), []
    for i in range(bars):
        o = price
        price = max(price * (1 + rng.gauss(0, 0.004)), 0.0001)
        hi, lo = max(o, price) * (1 + abs(rng.gauss(0, 0.001))), min(o, price) * (1 - abs(rng.gauss(0, 0.001)))
        out.append([end_ms - (bars - i) * HOUR, round(o, 6), round(hi, 6), round(lo, 6), round(price, 6), round(rng.lognormvariate(5, 0.5), 3)])
    return out


class FakeBinance:
    """Serves klines, premium klines, funding and exchangeInfo for generated symbols and records every request."""

    def __init__(self, *, days=30, symbols=("BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT"), end_ms=END_MS):
        self.bars = {s: generate_hourly(s, bars=days * 24, end_ms=end_ms) for s in symbols}
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
            return 200, {}, json.dumps({"symbols": [
                {"symbol": s, "status": "TRADING", "filters": [{"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001", "maxQty": "1000"},
                                                               {"filterType": "PRICE_FILTER", "tickSize": "0.01", "minPrice": "0.01", "maxPrice": "1000000"},
                                                               {"filterType": "MIN_NOTIONAL", "notional": "5"}]} for s in self.bars]}).encode()
        if path in ("/fapi/v1/klines", "/fapi/v1/premiumIndexKlines"):
            step = data.INTERVAL_MS[q["interval"]]
            out = []
            for ts, o, h, l, c, v in self.bars[q["symbol"]]:
                if ts >= int(q["startTime"]) and ("endTime" not in q or ts <= int(q["endTime"])):
                    out.append([ts, str(o), str(h), str(l), str(c), str(v), ts + step - 1, str(round(v * c, 3)), 10, str(round(v / 2, 3)), "0", "0"])
                if len(out) >= int(q["limit"]):
                    break
            return 200, {}, json.dumps(out).encode()
        if path == "/fapi/v1/fundingRate":
            out = []
            first = self.bars[q["symbol"]][0][0]
            for t in range((first // (8 * HOUR) + 1) * 8 * HOUR, END_MS, 8 * HOUR):
                if t >= int(q["startTime"]):
                    out.append({"symbol": q["symbol"], "fundingTime": t, "fundingRate": "0.0001", "markPrice": "100.0"})
                if len(out) >= int(q["limit"]):
                    break
            return 200, {}, json.dumps(out).encode()
        return 404, {}, b"{}"


def http_for(fake, sleeps=None):
    return PublicHTTP(opener=fake, sleep=(sleeps.append if sleeps is not None else (lambda s: None)), min_interval=0)


# ------------------------------------------------------------------ safety


@pytest.mark.parametrize("path", ["/fapi/v1/order", "/fapi/v2/account", "/fapi/v1/leverage", "/sapi/v1/capital/withdraw/apply", "/fapi/v1/listenKey", "/api/v3/order"])
def test_only_public_market_data_endpoints_can_be_called(path):
    fake = FakeBinance(days=2)
    with pytest.raises(DownloadError):
        http_for(fake).get(path, {"symbol": "BTCUSDT"})
    assert fake.urls == [], "a refused path must not even reach the transport"


def test_the_allowed_endpoints_are_exactly_these_five_public_reads():
    assert data.ALLOWED_PATHS == frozenset({"/fapi/v1/klines", "/fapi/v1/premiumIndexKlines", "/fapi/v1/fundingRate", "/fapi/v1/exchangeInfo", "/fapi/v1/time"})
    assert data.HOST == "fapi.binance.com" and data.BASE_URL == "https://fapi.binance.com"


def test_a_full_download_only_ever_sends_plain_unsigned_https_gets_to_the_futures_host(tmp_path):
    fake = FakeBinance(days=5)
    data.download_all(tmp_path / "store", pairs=["BTC/USDT", "SOL/USDT"], years=0.01, intervals=("1h", "5m"), http=http_for(fake), now=NOW)
    assert fake.urls
    for url in fake.urls:
        p = urllib.parse.urlparse(url)
        assert p.scheme == "https" and p.netloc == "fapi.binance.com" and p.path in data.ALLOWED_PATHS
        for forbidden in ("signature", "timestamp=", "apikey", "recvwindow"):
            assert forbidden not in url.lower()


def test_the_default_transport_sends_get_with_no_authentication_header(monkeypatch):
    import urllib.request
    seen = {}

    class R:
        status = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"[]"

    def fake_urlopen(request, timeout):
        seen["method"], seen["headers"], seen["url"] = request.get_method(), dict(request.header_items()), request.full_url
        return R()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert data._default_opener("https://fapi.binance.com/fapi/v1/time", 5.0)[0] == 200
    assert seen["method"] == "GET" and not {k.lower() for k in seen["headers"]} & {"x-mbx-apikey", "authorization", "cookie"}


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


def test_the_command_line_module_has_no_credential_environment_or_trading_imports():
    tree = ast.parse((Path(data.__file__).parent / "__main__.py").read_text())
    source = ast.unparse(tree)
    for needle in ("os.environ", "getenv", "dotenv", "ccxt", "sqlalchemy", "create_order", "config", "crypto_utils", "exchange"):
        assert needle not in source.replace("mba_bot.research", ""), needle
    modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert modules <= {"__future__", "argparse", "json", "sys", "pathlib"}, modules


# ------------------------------------------------------------------ downloading


def test_paging_resume_and_idempotence(tmp_path):
    fake = FakeBinance(days=200)  # 4800 hourly bars -> 4 pages of 1500
    root = tmp_path / "store"
    start_ms = END_MS - 4800 * HOUR
    res = data.download_klines(http_for(fake), root, "BTCUSDT", "1h", start_ms=start_ms, now_ms=END_MS)
    assert res.new_rows == res.total_rows == 4800
    assert len([u for u in fake.urls if "klines" in u]) == 4
    again = data.download_klines(http_for(fake), root, "BTCUSDT", "1h", start_ms=start_ms, now_ms=END_MS)
    assert again.new_rows == 0 and again.total_rows == 4800

    # Resume: pretend the file stopped 100 bars early.
    path = data.kline_path(root, "1h", "BTCUSDT")
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[:-100]) + "\n")
    resumed = data.download_klines(http_for(fake), root, "BTCUSDT", "1h", start_ms=start_ms, now_ms=END_MS)
    assert resumed.new_rows == 100 and resumed.total_rows == 4800
    candles = data.load_candles(root, "BTCUSDT", "1h")
    assert [c.timestamp for c in candles] == sorted({c.timestamp for c in candles}) and len(candles) == 4800


def test_a_bar_that_is_still_forming_is_never_stored(tmp_path):
    fake = FakeBinance(days=3)
    root = tmp_path / "store"
    now_ms = int(NOW.timestamp() * 1000)  # 00:30: the 00:00 bar is forming
    last = fake.bars["BTCUSDT"][-1]
    fake.bars["BTCUSDT"].append([last[0] + HOUR, *last[1:]])  # the server also serves the forming 00:00 bar
    data.download_klines(http_for(fake), root, "BTCUSDT", "1h", start_ms=now_ms - 3 * 86_400_000, now_ms=now_ms)
    stored = data.load_candles(root, "BTCUSDT", "1h")
    assert stored and stored[-1].timestamp == last[0]
    assert all(c.timestamp + HOUR <= now_ms for c in stored)


def test_funding_history_pages_and_resumes(tmp_path):
    fake = FakeBinance(days=400)  # ~1200 funding events -> two pages
    root = tmp_path / "store"
    res = data.download_funding(http_for(fake), root, "SOLUSDT", start_ms=END_MS - 400 * 86_400_000, now_ms=END_MS)
    assert res.total_rows >= 1190 and res.new_rows == res.total_rows
    assert data.download_funding(http_for(fake), root, "SOLUSDT", start_ms=END_MS - 400 * 86_400_000, now_ms=END_MS).new_rows == 0
    f = data.load_funding(root, "SOLUSDT")
    assert all(b[0] > a[0] for a, b in zip(f, f[1:]))


def test_rate_limits_are_waited_out_and_honour_retry_after():
    fake = FakeBinance(days=2)
    fake.script = [(429, {"Retry-After": "7"}), (418, {"retry-after": "3"})]
    sleeps: list[float] = []
    assert http_for(fake, sleeps).get("/fapi/v1/time") == {"serverTime": 1767225600000}
    assert 7.0 in sleeps and 3.0 in sleeps


def test_the_pause_between_requests_and_the_page_size_stay_far_under_the_ip_limit():
    assert data.MIN_SECONDS_BETWEEN_REQUESTS >= 0.5 and data.KLINE_LIMIT == 1500
    # a 1500-row klines call weighs 10 and the IP limit is 2400 per minute: at most ~100 calls a minute is weight 1000
    assert 60 / data.MIN_SECONDS_BETWEEN_REQUESTS * 10 <= 2400


def test_repeated_server_errors_give_up_instead_of_looping_forever():
    fake = FakeBinance(days=2)
    fake.script = [(503, {})] * 20
    with pytest.raises(DownloadError):
        http_for(fake).get("/fapi/v1/klines", {"symbol": "BTCUSDT", "interval": "1h", "startTime": 0, "limit": 5})


def test_exchange_info_snapshot_keeps_only_requested_symbols_and_the_filters(tmp_path):
    fake = FakeBinance(days=2)
    out = data.snapshot_exchange_info(http_for(fake), tmp_path / "store", ["BTCUSDT", "SOLUSDT"], today="2026-01-01")
    saved = json.loads(out.read_text())
    assert sorted(s["symbol"] for s in saved["symbols"]) == ["BTCUSDT", "SOLUSDT"]
    info = data.load_exchange_info(tmp_path / "store")
    assert {f["filterType"] for f in info["BTCUSDT"]["filters"]} >= {"LOT_SIZE", "PRICE_FILTER", "MIN_NOTIONAL"}


def test_manifest_records_hashes_that_change_with_the_data(tmp_path):
    fake = FakeBinance(days=40)
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


def test_the_download_command_is_wired_to_the_public_downloader(tmp_path, monkeypatch, capsys):
    from mba_bot.research.__main__ import main as cli

    fake = FakeBinance(days=20)
    monkeypatch.setattr(data, "PublicHTTP", lambda: http_for(fake))
    root = tmp_path / "dl"
    assert cli(["download", "--root", str(root), "--years", "1.5", "--pairs", "BTC/USDT", "--hourly-only"]) == 0
    assert data.load_candles(root, "BTCUSDT", "1h") and data.load_funding(root, "BTCUSDT") and (root / "manifest.json").exists()
    assert not data.kline_path(root, "5m", "BTCUSDT").exists(), "--hourly-only skips 5-minute bars"
    assert all(urllib.parse.urlparse(u).path in data.ALLOWED_PATHS for u in fake.urls)
    assert "manifest:" in capsys.readouterr().out
