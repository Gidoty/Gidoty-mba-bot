"""Command line for the research data downloader.   python -m mba_bot.research download --help

  download   fetch public Binance USD-M Futures candles into a local store (read-only, GET only, no credentials)

This is the `download` command of the research branch's command line, alone. It does not read .env, load credentials, place orders or touch the
trading database. The other commands of that branch (quality, baseline, phase1, phase2, ...) are not part of this program.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _pairs(arg: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(p.strip().upper() for p in arg.split(",")) if arg else default


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mba_bot.research", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("download", help="download public futures history")
    d.add_argument("--root", default="research_data")
    d.add_argument("--years", type=float, default=3.0)
    d.add_argument("--years-5m", type=float, default=None, help="history to keep for 5-minute bars (default: same as --years)")
    d.add_argument("--pairs", help="comma separated, e.g. BTC/USDT,ETH/USDT (default: the ten supported pairs)")
    d.add_argument("--hourly-only", action="store_true", help="skip 5-minute bars (the two backtests only read 1h)")
    d.add_argument("--no-funding", action="store_true")
    d.add_argument("--no-premium", action="store_true")

    args = ap.parse_args(argv)

    if args.cmd == "download":
        from . import data as D

        pairs = _pairs(args.pairs, D.DEFAULT_PAIRS)
        intervals = ("1h",) if args.hourly_only else ("1h", "5m")
        iy = {"5m": args.years_5m} if args.years_5m else None
        out = D.download_all(Path(args.root), pairs=pairs, years=args.years, intervals=intervals, include_funding=not args.no_funding,
                             include_premium=not args.no_premium, interval_years=iy, progress=lambda m: print(m, flush=True))
        print(json.dumps({"requests": out["requests"], "files": out["files"]}))
        print(f"manifest: {Path(args.root) / 'manifest.json'}")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
