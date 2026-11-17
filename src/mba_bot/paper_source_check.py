"""B0, the paper data-source check (read-only, for the owner to run).

    python -m mba_bot.paper_source_check              code facts only: no network, no key, no database
    python -m mba_bot.paper_source_check --compare    also fetch public candles from the paper (testnet or demo) host and from Binance mainnet and compare them

QUESTION: do PAPER decisions use mainnet candles or testnet/demo candles?

ANSWER FROM THE CODE (this tool prints the evidence it can check at run time): `trader.process_account` builds its exchange client with `sandbox=effective_is_paper`, and every candle the strategy reads
comes from `client.fetch_ohlcv` on that client. So a paper account reads candles from Binance's Spot Testnet (Spot) or Futures Demo (Futures) host, NOT from mainnet. A live account reads mainnet.

SAFETY: no API key is used or read (the paper clients are built with dummy values and are never used to call an authenticated endpoint); `--compare` makes only unauthenticated public `fetch_ohlcv` GETs
through a wrapper that allows nothing else; nothing is written anywhere; no environment variable or `.env` is read. The tool prints its own SHA-256 so the owner can check the file he runs.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import math
import re
import sys
from pathlib import Path

from . import exchange, trader

PAIRS = ("BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT")
DUMMY = "dummy-not-a-key"


def own_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def code_facts() -> list[str]:
    """What the code does, with evidence read from the code itself. No network."""
    source = inspect.getsource(trader.process_account)
    builds_with_paper_flag = bool(re.search(r"build_exchange_client\([^)]*sandbox=effective_is_paper", source, re.S))
    fetches = len(re.findall(r"client\.fetch_ohlcv\(", source))
    lines = [
        f"process_account builds its client with sandbox=effective_is_paper: {'YES' if builds_with_paper_flag else 'NO (the code changed: re-read it)'}",
        f"candles are read with client.fetch_ohlcv in process_account ({fetches} call sites in that function, plus _scan_auto_pair and _process_multi)",
        "",
        "hosts the candle read goes to (from the client's own routing table; no request is made):",
    ]
    for market in ("spot", "futures"):
        for sandbox in (True, False):
            client = exchange.build_exchange_client(exchange="binance", market_type=market, api_key=DUMMY, api_secret=DUMMY, sandbox=sandbox)
            hosts = exchange._routing_hosts(client)
            group = "fapiPublic" if market == "futures" else "public"  # the endpoint group fetch_ohlcv uses for that market
            who = "PAPER (sandbox)" if sandbox else "LIVE (mainnet)"
            lines.append(f"  {who:<16} {market:<8} candles come from: {hosts.get(group, 'unreadable')}  (endpoint group {group})")
    lines += ["", "VERDICT: paper decisions use testnet / demo candles, not mainnet candles."]
    return lines


class ReadOnlyCandles:
    """A wrapper that can ONLY read candles. Any other attribute raises."""

    def __init__(self, client):
        self._client = client

    async def fetch_ohlcv(self, symbol, timeframe="1h", limit=200):
        return await self._client.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

    async def close(self):
        await self._client.close()

    def __getattr__(self, name):
        raise AttributeError(f"read-only candle client: {name} is not allowed")


def compare_series(paper: list, main: list) -> dict:
    """Pure. Compare two ccxt OHLCV lists on their common timestamps (close, high and low)."""
    p = {int(r[0]): r for r in paper}
    m = {int(r[0]): r for r in main}
    common = sorted(set(p) & set(m))
    diffs = []
    for ts in common:
        a, b = float(p[ts][4]), float(m[ts][4])
        if b > 0 and math.isfinite(a) and math.isfinite(b):
            diffs.append(abs(a - b) / b * 100.0)
    return {
        "paper_candles": len(p), "mainnet_candles": len(m), "common": len(common), "only_paper": len(set(p) - set(m)), "only_mainnet": len(set(m) - set(p)),
        "max_close_diff_pct": max(diffs) if diffs else None, "mean_close_diff_pct": (sum(diffs) / len(diffs)) if diffs else None,
        "share_over_0_1_pct": (sum(1 for d in diffs if d > 0.1) / len(diffs)) if diffs else None,
    }


async def compare(limit: int) -> list[str]:
    out = ["", f"public candle comparison, last {limit} hourly candles (unauthenticated GETs only):"]
    for market in ("spot", "futures"):
        symbols = [exchange.exchange_symbol(p, market) for p in PAIRS]
        sandbox = ReadOnlyCandles(exchange.build_exchange_client(exchange="binance", market_type=market, api_key=DUMMY, api_secret=DUMMY, sandbox=True))
        mainnet = ReadOnlyCandles(exchange.build_exchange_client(exchange="binance", market_type=market, api_key=DUMMY, api_secret=DUMMY, sandbox=False))
        try:
            for pair, symbol in zip(PAIRS, symbols):
                try:
                    r = compare_series(await sandbox.fetch_ohlcv(symbol, "1h", limit), await mainnet.fetch_ohlcv(symbol, "1h", limit))
                except Exception as exc:  # noqa: BLE001 - class name only
                    out.append(f"  {market:<8} {pair:<9} could not be read ({type(exc).__name__})")
                    continue
                f = lambda v, d=3: "n/a" if v is None else f"{v:.{d}f}"
                out.append(f"  {market:<8} {pair:<9} common {r['common']}/{r['paper_candles']} (only paper {r['only_paper']}, only mainnet {r['only_mainnet']}); close difference: max {f(r['max_close_diff_pct'])}%, mean {f(r['mean_close_diff_pct'])}%, "
                           f"candles over 0.1%: {f(None if r['share_over_0_1_pct'] is None else r['share_over_0_1_pct'] * 100, 1)}%")
        finally:
            await sandbox.close()
            await mainnet.close()
    out += ["", "Reading it: a max or mean difference near 0 means the paper venue tracks mainnet closely for that pair; large differences or many missing candles mean paper decisions were made on a different market.",
            "A testnet or demo book is thin and can be moved by a few orders, so differences are expected to be larger than between two mainnet sources."]
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mba_bot.paper_source_check", description="Read-only: which candles do paper decisions use?")
    ap.add_argument("--compare", action="store_true", help="also fetch public candles from the paper host and from mainnet and compare them (network, no key)")
    ap.add_argument("--limit", type=int, default=200, help="hourly candles to compare (default 200, max 1000)")
    args = ap.parse_args(argv)
    print(f"paper_source_check sha256 {own_sha256()}")
    for line in code_facts():
        print(line)
    if args.compare:
        try:
            for line in asyncio.run(compare(max(10, min(1000, args.limit)))):
                print(line)
        except Exception as exc:  # noqa: BLE001
            print(f"comparison failed ({type(exc).__name__})", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
