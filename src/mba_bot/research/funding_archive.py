"""Public Binance archive reader for USD-M funding rates (research only; the sealed test of profit protection v2 uses it). Same rules as `research/archive.py`: plain HTTPS GET, one host
(data.binance.vision), one URL shape, no key, no account, no environment variable. Writes `root/funding/<SYMBOL>.csv` with the columns calc_time,funding_interval_hours,last_funding_rate (one row per
settlement). A month the archive does not have is reported and skipped, never invented.

    python -m mba_bot.research.funding_archive download --root research_data --symbols BTCUSDT,ETHUSDT --months 2025-11,2025-12"""
from __future__ import annotations

import argparse
import io
import sys
import zipfile
from pathlib import Path
from typing import Callable

from .archive import HOST, _default_fetch
from .data import _append

URL = "https://" + HOST + "/data/futures/um/monthly/fundingRate/{symbol}/{symbol}-fundingRate-{month}.zip"
FIELDS = ["calc_time", "funding_interval_hours", "last_funding_rate"]


def funding_url(symbol: str, month: str) -> str:
    if not (symbol.isalnum() and symbol.isupper()):
        raise ValueError("unsupported symbol")
    year, mon = month.split("-")
    if not (len(year) == 4 and year.isdigit() and len(mon) == 2 and mon.isdigit() and 1 <= int(mon) <= 12):
        raise ValueError("a month looks like 2026-10")
    return URL.format(symbol=symbol, month=month)


def funding_path(root: Path, symbol: str) -> Path:
    return Path(root) / "funding" / f"{symbol}.csv"


def parse_month(zip_bytes: bytes) -> list[list[str]]:
    """The settlements of one monthly file as store rows. The archive's header line (if any) is skipped."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        text = z.read(z.namelist()[0]).decode()
    rows = []
    for line in text.splitlines():
        parts = line.strip().split(",")
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        rows.append([parts[0], parts[1], parts[2]])
    return rows


def download(root: Path, symbols: list[str], months: list[str], fetch: Callable[[str], bytes | None] = _default_fetch) -> dict:
    added: dict[str, int] = {}
    missing: list[str] = []
    for symbol in symbols:
        path = funding_path(root, symbol)
        path.parent.mkdir(parents=True, exist_ok=True)
        last = None
        if path.exists() and path.stat().st_size > 0:
            with path.open() as fh:
                for line in fh:
                    if line[:1].isdigit():
                        last = int(line.split(",", 1)[0])
        total = 0
        for month in sorted(months):
            raw = fetch(funding_url(symbol, month))
            if raw is None:
                missing.append(f"{symbol} funding {month}")
                continue
            rows = [r for r in parse_month(raw) if last is None or int(r[0]) > last]
            total += _append(path, FIELDS, rows)
            if rows:
                last = int(rows[-1][0])
        added[symbol] = total
    return {"added": added, "missing": missing}


def load(root: Path, symbol: str) -> list[tuple[int, float]]:
    """(settlement time in ms, rate) oldest first; an empty list when there is no file."""
    path = funding_path(root, symbol)
    if not path.exists():
        return []
    out = []
    with path.open() as fh:
        for line in fh:
            parts = line.strip().split(",")
            if len(parts) >= 3 and parts[0].isdigit():
                out.append((int(parts[0]), float(parts[2])))
    return sorted(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mba_bot.research.funding_archive")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("download")
    d.add_argument("--root", required=True, type=Path)
    d.add_argument("--symbols", required=True)
    d.add_argument("--months", required=True)
    args = ap.parse_args(argv)
    try:
        out = download(args.root, args.symbols.split(","), args.months.split(","))
    except Exception as exc:  # noqa: BLE001 - class name only
        print(f"funding download failed ({type(exc).__name__})", file=sys.stderr)
        return 2
    for sym, n in out["added"].items():
        print(f"{sym} funding: {n} settlements added")
    for m in out["missing"]:
        print(f"missing in the archive: {m}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
