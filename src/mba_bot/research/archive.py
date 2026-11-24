"""Public Binance archive reader for USD-M Futures candles (research only). Writes the SAME store layout as `research/data.py` (`root/klines/<interval>/<SYMBOL>.csv`, columns ts,open,high,low,close,volume,
quote_volume,trades,taker_buy_volume) so every research tool reads either.

Why it exists: the live-API reader (`research/data.py`, host fapi.binance.com) is refused with HTTP 451 from some locations (it was from the build environment on 24 Nov 2026). Binance also publishes
every month of candles as a public zip file on data.binance.vision (no key, no account). This module reads ONLY that: plain HTTPS GET, one host, one URL shape, nothing else. It imports nothing from the
trading service except the Candle type through research.data's layout, and reads no environment variable.

    python -m mba_bot.research.archive download --root research_data --interval 5m --symbols BTCUSDT,ETHUSDT --months 2025-11,2025-12

Months are complete months (the archive publishes a month after it ends), so every stored bar is a closed bar. A month the archive does not have is reported and skipped, never invented."""
from __future__ import annotations

import argparse
import io
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

from .data import KLINE_FIELDS, _append, kline_path

HOST = "data.binance.vision"
URL = "https://" + HOST + "/data/futures/um/monthly/klines/{symbol}/{interval}/{symbol}-{interval}-{month}.zip"
INTERVALS = ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h")


def archive_url(symbol: str, interval: str, month: str) -> str:
    if interval not in INTERVALS:
        raise ValueError("unsupported interval")
    if not (symbol.isalnum() and symbol.isupper()):
        raise ValueError("unsupported symbol")
    year, mon = month.split("-")
    if not (len(year) == 4 and year.isdigit() and len(mon) == 2 and mon.isdigit() and 1 <= int(mon) <= 12):
        raise ValueError("a month looks like 2026-10")
    return URL.format(symbol=symbol, interval=interval, month=month)


def _default_fetch(url: str) -> bytes | None:
    """GET only. None for a month the archive does not have (404)."""
    if not url.startswith("https://" + HOST + "/"):
        raise ValueError("only the public archive host is allowed")
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "mba-research-archive"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def parse_month(zip_bytes: bytes) -> list[list[str]]:
    """The rows of one monthly file as store rows. The archive's own header line (if any) is skipped; every row is a closed bar."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        text = z.read(z.namelist()[0]).decode()
    rows = []
    for line in text.splitlines():
        parts = line.strip().split(",")
        if len(parts) < 11 or not parts[0].isdigit():
            continue
        rows.append([parts[0], parts[1], parts[2], parts[3], parts[4], parts[5], parts[7], parts[8], parts[9]])
    return rows


def download(root: Path, symbols: list[str], interval: str, months: list[str], fetch: Callable[[str], bytes | None] = _default_fetch) -> dict:
    """Append the months (oldest first) to each symbol's file, skipping bars already stored. Returns {symbol: rows added}, and the list of missing months."""
    added: dict[str, int] = {}
    missing: list[str] = []
    for symbol in symbols:
        path = kline_path(Path(root), interval, symbol)
        last = None
        if path.exists() and path.stat().st_size > 0:
            with path.open() as fh:
                for line in fh:
                    if line[:1].isdigit():
                        last = int(line.split(",", 1)[0])
        total = 0
        for month in sorted(months):
            raw = fetch(archive_url(symbol, interval, month))
            if raw is None:
                missing.append(f"{symbol} {interval} {month}")
                continue
            rows = [r for r in parse_month(raw) if last is None or int(r[0]) > last]
            total += _append(path, KLINE_FIELDS, rows)
            if rows:
                last = int(rows[-1][0])
        added[symbol] = total
    return {"added": added, "missing": missing}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mba_bot.research.archive")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("download")
    d.add_argument("--root", required=True, type=Path)
    d.add_argument("--interval", required=True)
    d.add_argument("--symbols", required=True)
    d.add_argument("--months", required=True, help="comma separated, e.g. 2025-11,2025-12")
    args = ap.parse_args(argv)
    try:
        out = download(args.root, args.symbols.split(","), args.interval, args.months.split(","))
    except Exception as exc:  # noqa: BLE001 - class name only
        print(f"archive download failed ({type(exc).__name__})", file=sys.stderr)
        return 2
    for sym, n in out["added"].items():
        print(f"{sym} {args.interval}: {n} bars added")
    for m in out["missing"]:
        print(f"missing in the archive: {m}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
