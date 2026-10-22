"""Read-only Binance USD-M Futures market-data downloader and local store.

SAFETY (enforced in code and pinned by tests/test_research_data.py):

* Only PUBLIC market-data endpoints, in `ALLOWED_PATHS`, over HTTPS to
  `fapi.binance.com`, with plain GET requests. No API key, no signature, no
  authenticated header, no POST/DELETE, no order endpoint.
* This module imports nothing from the trading service except the Candle
  dataclass, and never reads environment variables or `.env`.
* It cannot place an order because no code path here knows how to.

STORE LAYOUT (`root` is a directory outside git, default ./research_data):

    root/klines/<interval>/<SYMBOL>.csv      ts,open,high,low,close,volume,quote_volume,trades,taker_buy_volume
    root/funding/<SYMBOL>.csv                ts,rate,mark_price          (every funding event)
    root/premium/<interval>/<SYMBOL>.csv     ts,open,high,low,close      (premium index = (mark - index)/index, the basis proxy)
    root/meta/exchange_info_<UTC date>.json  filters of the requested symbols, as served that day
    root/manifest.json                       per file: rows, first/last ts, sha256, downloaded_at

`ts` is the bar open time in epoch milliseconds, UTC. Only CLOSED bars are
stored (a bar still forming at download time is dropped), so a file never
contains a number that can still change. Downloads resume: re-running appends
only what is newer than the file's last row.
"""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..strategy import Candle

logger = logging.getLogger(__name__)

HOST = "fapi.binance.com"
BASE_URL = f"https://{HOST}"
ALLOWED_PATHS = frozenset(
    {
        "/fapi/v1/klines",
        "/fapi/v1/premiumIndexKlines",
        "/fapi/v1/fundingRate",
        "/fapi/v1/exchangeInfo",
        "/fapi/v1/time",
    }
)

DEFAULT_PAIRS = (
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "SOL/USDT",
    "DOGE/USDT", "SUI/USDT", "LINK/USDT", "ADA/USDT", "LTC/USDT",
)

INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
KLINE_LIMIT = 1500
FUNDING_LIMIT = 1000
# Binance IP rate limit is 2400 weight/minute; a 1500-row klines call weighs 10.
# Stay far under it, and back off completely when the server says so.
MIN_SECONDS_BETWEEN_REQUESTS = 0.6
WEIGHT_PAUSE_AT = 1500


class DownloadError(RuntimeError):
    pass


def symbol_for(pair: str) -> str:
    """'BTC/USDT' -> 'BTCUSDT'."""
    return pair.replace("/", "").upper()


def pair_for(symbol: str) -> str:
    return f"{symbol[:-4]}/USDT" if symbol.endswith("USDT") else symbol


# Signature of the injectable transport: (url, timeout) -> (status, headers, body)
Opener = Callable[[str, float], "tuple[int, dict, bytes]"]


def _default_opener(url: str, timeout: float) -> tuple[int, dict, bytes]:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "mba-research/1 (public market data)"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - https URL built from BASE_URL only
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read() if exc.fp else b""


class PublicHTTP:
    """GET-only client for Binance's public futures market data."""

    def __init__(self, *, opener: Opener | None = None, sleep: Callable[[float], None] = time.sleep, min_interval: float = MIN_SECONDS_BETWEEN_REQUESTS):
        self._opener = opener or _default_opener
        self._sleep = sleep
        self._min_interval = min_interval
        self._last = 0.0
        self.requests = 0

    def get(self, path: str, params: dict | None = None):
        if path not in ALLOWED_PATHS:
            raise DownloadError(f"refusing to call {path}: not an allowed public market-data endpoint")
        query = urllib.parse.urlencode(params or {})
        url = f"{BASE_URL}{path}" + (f"?{query}" if query else "")
        attempt = 0
        while True:
            wait = self._min_interval - (time.monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
            status, headers, body = self._opener(url, 30.0)
            self._last = time.monotonic()
            self.requests += 1
            lowered = {str(k).lower(): v for k, v in (headers or {}).items()}
            if status == 200:
                used = lowered.get("x-mbx-used-weight-1m")
                if used and used.isdigit() and int(used) >= WEIGHT_PAUSE_AT:
                    self._sleep(30)
                return json.loads(body.decode("utf-8"))
            if status in (418, 429) or status >= 500:
                attempt += 1
                if attempt > 6:
                    raise DownloadError(f"{path}: giving up after repeated HTTP {status}")
                retry_after = lowered.get("retry-after")
                delay = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else min(2**attempt, 60)
                logger.warning("HTTP %s from %s; waiting %.0fs (attempt %d)", status, path, delay, attempt)
                self._sleep(delay)
                continue
            raise DownloadError(f"{path}: unexpected HTTP {status}")


# --------------------------------------------------------------------- CSV helpers

KLINE_FIELDS = ["ts", "open", "high", "low", "close", "volume", "quote_volume", "trades", "taker_buy_volume"]
FUNDING_FIELDS = ["ts", "rate", "mark_price"]
PREMIUM_FIELDS = ["ts", "open", "high", "low", "close"]


def _last_ts(path: Path) -> int | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    last = None
    with path.open("r", newline="") as fh:
        for row in csv.reader(fh):
            if row and row[0] != "ts":
                last = row[0]
    return int(last) if last else None


def _append(path: Path, fields: list[str], rows: list[list[str]]) -> int:
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="") as fh:
        writer = csv.writer(fh)
        if new_file:
            writer.writerow(fields)
        writer.writerows(rows)
    return len(rows)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def kline_path(root: Path, interval: str, symbol: str) -> Path:
    return Path(root) / "klines" / interval / f"{symbol}.csv"


def funding_path(root: Path, symbol: str) -> Path:
    return Path(root) / "funding" / f"{symbol}.csv"


def premium_path(root: Path, interval: str, symbol: str) -> Path:
    return Path(root) / "premium" / interval / f"{symbol}.csv"


# --------------------------------------------------------------------- downloads


@dataclass
class FileResult:
    path: Path
    new_rows: int
    total_rows: int


def _count_rows(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", newline="") as fh:
        return max(sum(1 for _ in fh) - 1, 0)


def download_klines(http: PublicHTTP, root: Path, symbol: str, interval: str, *, start_ms: int, now_ms: int, premium: bool = False, bounded: bool = False) -> FileResult:
    step = INTERVAL_MS[interval]
    path = premium_path(root, interval, symbol) if premium else kline_path(root, interval, symbol)
    fields = PREMIUM_FIELDS if premium else KLINE_FIELDS
    last = _last_ts(path)
    cursor = start_ms if last is None else last + step
    endpoint = "/fapi/v1/premiumIndexKlines" if premium else "/fapi/v1/klines"
    added = 0
    while cursor < now_ms:
        params = {"symbol": symbol, "interval": interval, "startTime": cursor, "limit": KLINE_LIMIT}
        if bounded:
            params["endTime"] = now_ms - 1
        data = http.get(endpoint, params)
        if not data:
            break
        rows: list[list[str]] = []
        for k in data:
            open_ts, close_ts = int(k[0]), int(k[6])
            if close_ts >= now_ms:  # still forming: never store a number that can change
                continue
            if open_ts < cursor:
                continue
            if premium:
                rows.append([str(open_ts), k[1], k[2], k[3], k[4]])
            else:
                rows.append([str(open_ts), k[1], k[2], k[3], k[4], k[5], k[7], str(k[8]), k[9]])
        added += _append(path, fields, rows)
        newest_open = int(data[-1][0])
        if len(data) < KLINE_LIMIT or newest_open + step <= cursor:
            break
        cursor = newest_open + step
    return FileResult(path, added, _count_rows(path))


def download_funding(http: PublicHTTP, root: Path, symbol: str, *, start_ms: int, now_ms: int, bounded: bool = False) -> FileResult:
    path = funding_path(root, symbol)
    last = _last_ts(path)
    cursor = start_ms if last is None else last + 1
    added = 0
    while cursor < now_ms:
        params = {"symbol": symbol, "startTime": cursor, "limit": FUNDING_LIMIT}
        if bounded:
            params["endTime"] = now_ms - 1
        data = http.get("/fapi/v1/fundingRate", params)
        if not data:
            break
        rows = [[str(int(r["fundingTime"])), r["fundingRate"], r.get("markPrice", "")] for r in data if cursor <= int(r["fundingTime"]) < now_ms]
        added += _append(path, FUNDING_FIELDS, rows)
        newest = int(data[-1]["fundingTime"])
        if len(data) < FUNDING_LIMIT or newest < cursor:
            break
        cursor = newest + 1
    return FileResult(path, added, _count_rows(path))


def snapshot_exchange_info(http: PublicHTTP, root: Path, symbols: list[str], *, today: str) -> Path:
    """Save the current trading rules (filters, order types, status) for the
    requested symbols. Binance serves only the CURRENT rules, so this is a
    point-in-time record: historical backtests that use it assume the rules
    did not change, which the report states."""
    info = http.get("/fapi/v1/exchangeInfo")
    wanted = set(symbols)
    kept = [s for s in info.get("symbols", []) if s.get("symbol") in wanted]
    out = Path(root) / "meta" / f"exchange_info_{today}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"fetched_for": today, "symbols": kept}, indent=1, sort_keys=True))
    return out


def write_manifest(root: Path, *, now: datetime) -> dict:
    root = Path(root)
    files = {}
    for path in sorted(root.rglob("*.csv")):
        rel = str(path.relative_to(root))
        rows = _count_rows(path)
        first = last = None
        if rows:
            with path.open("r", newline="") as fh:
                reader = csv.reader(fh)
                next(reader)
                first = int(next(reader)[0])
            last = _last_ts(path)
        files[rel] = {"rows": rows, "first_ts": first, "last_ts": last, "sha256": _sha256(path)}
    manifest = {"generated_at": now.astimezone(timezone.utc).isoformat(), "files": files}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True))
    return manifest


def download_all(
    root: Path,
    *,
    pairs: tuple[str, ...] | list[str] = DEFAULT_PAIRS,
    years: float = 3.0,
    intervals: tuple[str, ...] = ("1h",),
    include_funding: bool = True,
    include_premium: bool = True,
    interval_years: dict[str, float] | None = None,
    http: PublicHTTP | None = None,
    now: datetime | None = None,
    progress: Callable[[str], None] = lambda msg: None,
) -> dict:
    http = http or PublicHTTP()
    now = now or datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    start_ms = int((now.timestamp() - years * 365.25 * 86400) * 1000)
    interval_start = {i: int((now.timestamp() - y * 365.25 * 86400) * 1000) for i, y in (interval_years or {}).items()}
    symbols = [symbol_for(p) for p in pairs]
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    results: dict[str, int] = {}
    snapshot_exchange_info(http, root, symbols, today=now.strftime("%Y-%m-%d"))
    for sym in symbols:
        for interval in intervals:
            if interval not in INTERVAL_MS:
                raise DownloadError(f"unsupported interval {interval}")
            res = download_klines(http, root, sym, interval, start_ms=interval_start.get(interval, start_ms), now_ms=now_ms)
            progress(f"{sym} klines {interval}: +{res.new_rows} (total {res.total_rows})")
            results[f"klines/{interval}/{sym}"] = res.new_rows
        if include_premium:
            res = download_klines(http, root, sym, "1h", start_ms=start_ms, now_ms=now_ms, premium=True)
            progress(f"{sym} premium 1h: +{res.new_rows} (total {res.total_rows})")
            results[f"premium/1h/{sym}"] = res.new_rows
        if include_funding:
            res = download_funding(http, root, sym, start_ms=start_ms, now_ms=now_ms)
            progress(f"{sym} funding: +{res.new_rows} (total {res.total_rows})")
            results[f"funding/{sym}"] = res.new_rows
    manifest = write_manifest(root, now=now)
    return {"requests": http.requests, "new_rows": results, "files": len(manifest["files"])}


def download_range(
    root: Path,
    *,
    pairs: tuple[str, ...] | list[str],
    since: datetime,
    until: datetime,
    intervals: tuple[str, ...] = ("1h", "5m"),
    include_funding: bool = True,
    http: PublicHTTP | None = None,
    progress: Callable[[str], None] = lambda msg: None,
) -> dict:
    """Download a fixed calendar range [since, until) into a store of its own. Public market data only.

    Used for the Phase 2 older-history confirmation. Unlike `download_all` the end is explicit, so the store can never
    reach into the period an earlier store already holds. The caller is responsible for the one-shot guard (see
    phase2.guard_download)."""
    if since.tzinfo is None or until.tzinfo is None:
        raise DownloadError("since and until must be timezone-aware")
    if until <= since:
        raise DownloadError("until must be after since")
    http = http or PublicHTTP()
    start_ms, until_ms = int(since.timestamp() * 1000), int(until.timestamp() * 1000)
    symbols = [symbol_for(p) for p in pairs]
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    results: dict[str, int] = {}
    snapshot_exchange_info(http, root, symbols, today=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    for sym in symbols:
        for interval in intervals:
            if interval not in INTERVAL_MS:
                raise DownloadError(f"unsupported interval {interval}")
            res = download_klines(http, root, sym, interval, start_ms=start_ms, now_ms=until_ms, bounded=True)
            progress(f"{sym} klines {interval}: +{res.new_rows} (total {res.total_rows})")
            results[f"klines/{interval}/{sym}"] = res.new_rows
        if include_funding:
            res = download_funding(http, root, sym, start_ms=start_ms, now_ms=until_ms, bounded=True)
            progress(f"{sym} funding: +{res.new_rows} (total {res.total_rows})")
            results[f"funding/{sym}"] = res.new_rows
    manifest = write_manifest(root, now=datetime.now(timezone.utc))
    return {"requests": http.requests, "new_rows": results, "files": len(manifest["files"])}


# --------------------------------------------------------------------- loading


def load_candles(root: Path, symbol: str, interval: str = "1h") -> list[Candle]:
    path = kline_path(Path(root), interval, symbol)
    if not path.exists():
        raise FileNotFoundError(f"no {interval} data for {symbol} under {root}")
    out: list[Candle] = []
    with path.open("r", newline="") as fh:
        for row in csv.DictReader(fh):
            out.append(
                Candle(
                    timestamp=int(row["ts"]), open=float(row["open"]), high=float(row["high"]),
                    low=float(row["low"]), close=float(row["close"]), volume=float(row["volume"]),
                )
            )
    return out


def load_funding(root: Path, symbol: str) -> list[tuple[int, float]]:
    path = funding_path(Path(root), symbol)
    if not path.exists():
        return []
    with path.open("r", newline="") as fh:
        return [(int(r["ts"]), float(r["rate"])) for r in csv.DictReader(fh)]


def load_premium(root: Path, symbol: str, interval: str = "1h") -> dict[int, float]:
    path = premium_path(Path(root), interval, symbol)
    if not path.exists():
        return {}
    with path.open("r", newline="") as fh:
        return {int(r["ts"]): float(r["close"]) for r in csv.DictReader(fh)}


def load_exchange_info(root: Path) -> dict[str, dict]:
    """Latest saved snapshot: {SYMBOL: raw binance symbol entry}."""
    metas = sorted((Path(root) / "meta").glob("exchange_info_*.json"))
    if not metas:
        return {}
    data = json.loads(metas[-1].read_text())
    return {s["symbol"]: s for s in data.get("symbols", [])}
