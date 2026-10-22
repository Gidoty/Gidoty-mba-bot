"""Seeded SYNTHETIC market data, for tests and for exercising the pipeline offline.

This is NOT market evidence. Nothing measured on it says anything about how the
strategy would have performed on real Binance data; it exists so the downloader,
quality checks, backtester and reports can be tested deterministically in an
environment with no market access. Any report built from it is stamped
"SYNTHETIC" by the CLI.

The generator switches between trending, choppy and high-volatility regimes so
the strategy has something to trade, builds 5-minute bars first, and aggregates
them to hourly bars so the two timeframes are consistent with each other.
"""
from __future__ import annotations

import json
import math
import random
from datetime import datetime, timezone
from pathlib import Path

from .data import (
    FUNDING_FIELDS, KLINE_FIELDS, PREMIUM_FIELDS, _append, funding_path, kline_path, premium_path, symbol_for, write_manifest,
)

FIVE_MIN = 300_000
HOUR = 3_600_000

# The six extra pairs use the filters read from Binance Mainnet on the VPS; the
# four original pairs use typical values (approximate; replaced by the real
# snapshot the downloader saves).
FILTERS = {
    "BTCUSDT": ("0.1", "0.001", "100"), "ETHUSDT": ("0.01", "0.001", "20"), "BNBUSDT": ("0.01", "0.01", "5"),
    "XRPUSDT": ("0.0001", "0.1", "5"), "SOLUSDT": ("0.01", "0.01", "5"), "DOGEUSDT": ("0.00001", "1", "5"),
    "SUIUSDT": ("0.0001", "0.1", "5"), "LINKUSDT": ("0.001", "0.01", "20"), "ADAUSDT": ("0.0001", "1", "5"),
    "LTCUSDT": ("0.01", "0.001", "20"),
}
START_PRICE = {"BTCUSDT": 40000.0, "ETHUSDT": 2500.0, "BNBUSDT": 350.0, "XRPUSDT": 0.6, "SOLUSDT": 100.0,
               "DOGEUSDT": 0.1, "SUIUSDT": 1.0, "LINKUSDT": 15.0, "ADAUSDT": 0.5, "LTCUSDT": 80.0}


def exchange_info_for(symbols: list[str]) -> dict:
    out = []
    for s in symbols:
        tick, step, notional = FILTERS.get(s, ("0.01", "0.001", "5"))
        out.append(
            {
                "symbol": s, "status": "TRADING", "contractType": "PERPETUAL", "baseAsset": s[:-4], "quoteAsset": "USDT",
                "orderTypes": ["LIMIT", "MARKET", "STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET"],
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": tick, "minPrice": tick, "maxPrice": "1000000"},
                    {"filterType": "LOT_SIZE", "minQty": step, "stepSize": step, "maxQty": "1000000"},
                    {"filterType": "MARKET_LOT_SIZE", "minQty": step, "stepSize": step, "maxQty": "100000"},
                    {"filterType": "MIN_NOTIONAL", "notional": notional},
                ],
            }
        )
    return {"symbols": out}


def generate_5m(symbol: str, *, bars: int, start_ms: int, seed: int) -> list[list[float]]:
    """[ts, o, h, l, c, volume] 5-minute rows."""
    rng = random.Random(f"{seed}:{symbol}")
    price = START_PRICE.get(symbol, 100.0)
    rows = []
    drift, vol, regime_left = 0.0, 0.0006, 0
    for i in range(bars):
        if regime_left <= 0:
            kind = rng.choices(["trend_up", "trend_down", "chop", "wild"], weights=[3, 3, 5, 1])[0]
            regime_left = rng.randint(12 * 24, 12 * 24 * 20)  # one day to three weeks of 5m bars
            drift = {"trend_up": 0.00004, "trend_down": -0.00004, "chop": 0.0, "wild": 0.0}[kind]
            vol = {"trend_up": 0.0007, "trend_down": 0.0008, "chop": 0.0005, "wild": 0.0016}[kind]
        regime_left -= 1
        r = drift + vol * rng.gauss(0, 1)
        o = price
        c = o * math.exp(r)
        span = abs(rng.gauss(0, vol * 0.6)) * o
        h, lo = max(o, c) + span, min(o, c) - span
        rows.append([start_ms + i * FIVE_MIN, o, h, max(lo, 1e-9), c, 100.0 + 50.0 * abs(rng.gauss(0, 1))])
        price = c
    return rows


def aggregate(rows_5m: list[list[float]], per: int = 12) -> list[list[float]]:
    out = []
    for i in range(0, len(rows_5m) - per + 1, per):
        chunk = rows_5m[i : i + per]
        out.append([chunk[0][0], chunk[0][1], max(r[2] for r in chunk), min(r[3] for r in chunk), chunk[-1][4], sum(r[5] for r in chunk)])
    return out


def write_store(root: Path, *, pairs: list[str], days: int = 400, seed: int = 7, with_5m: bool = True, now: datetime | None = None) -> dict:
    """Write a complete synthetic store in the real downloader's layout."""
    root = Path(root)
    now = now or datetime(2026, 1, 1, tzinfo=timezone.utc)
    bars_5m = days * 288
    end_ms = int(now.timestamp() * 1000) // HOUR * HOUR
    start_ms = end_ms - bars_5m * FIVE_MIN
    symbols = [symbol_for(p) for p in pairs]
    for sym in symbols:
        rows_5m = generate_5m(sym, bars=bars_5m, start_ms=start_ms, seed=seed)
        rows_1h = aggregate(rows_5m)
        fmt = lambda r, n: [str(int(r[0]))] + [repr(round(x, 8)) for x in r[1:5]] + [repr(round(r[5], 4)), repr(round(r[5] * r[4], 4)), str(n), repr(round(r[5] / 2, 4))]
        if with_5m:
            _append(kline_path(root, "5m", sym), KLINE_FIELDS, [fmt(r, 10) for r in rows_5m])
        _append(kline_path(root, "1h", sym), KLINE_FIELDS, [fmt(r, 100) for r in rows_1h])
        rng = random.Random(f"fund:{seed}:{sym}")
        funding = []
        t = (start_ms // 28_800_000 + 1) * 28_800_000
        while t < end_ms:
            funding.append([str(t), repr(round(0.0001 + 0.00005 * rng.gauss(0, 1), 8)), repr(round(rows_1h[min(len(rows_1h) - 1, int((t - start_ms) // HOUR))][4], 4))])
            t += 28_800_000
        _append(funding_path(root, sym), FUNDING_FIELDS, funding)
        _append(premium_path(root, "1h", sym), PREMIUM_FIELDS, [[str(int(r[0])), repr(round(0.0001 * math.sin(i / 50), 8)), repr(round(0.0001 * math.sin(i / 50) + 0.00005, 8)), repr(round(0.0001 * math.sin(i / 50) - 0.00005, 8)), repr(round(0.0001 * math.sin(i / 50), 8))] for i, r in enumerate(rows_1h)])
    meta = root / "meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / f"exchange_info_{now.strftime('%Y-%m-%d')}.json").write_text(json.dumps({"fetched_for": "synthetic", **exchange_info_for(symbols)}))
    (root / "SYNTHETIC").write_text("This store contains generated data, not market data.\n")
    return write_manifest(root, now=now)


def is_synthetic(root: Path) -> bool:
    return (Path(root) / "SYNTHETIC").exists()
