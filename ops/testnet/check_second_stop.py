#!/usr/bin/env python3
"""Owner-run check on Binance Futures DEMO TRADING (the testnet) ONLY: what does Binance do with a second protective stop?

Run it yourself, on your own machine or on the server, with your own DEMO keys. Claude does not run it and never sees a key.

    python ops/testnet/check_second_stop.py [--symbol BTC/USDT] [--out report.txt]

The keys are typed at a hidden prompt (nothing is echoed). They are never read from arguments, environment variables or files, never written anywhere and never printed. The client is built by the bot's own
`exchange.build_exchange_client(..., sandbox=True)`, which proves from the effective URL table that it routes ONLY to Binance Demo Trading (demo-fapi.binance.com) and refuses to start otherwise: a real key cannot
trade here, because Binance's demo system does not accept a real key and this script has no way to reach the production host.

What it does, in order (one minimum-size position, then everything is cleaned up, also when a step fails):
  1. Refuses to start unless the symbol is flat and has no conditional order resting, and the account is in one-way mode.
  2. Opens a minimum-size LONG with a market order.
  3. Places stop A: a closePosition stop-market (what the bot places first).
  4. Attempts stop B: a SECOND closePosition stop in the same direction. Records accepted, or the exact refusal: exception class, Binance code and message.
  5. Places stop C: a reduceOnly stop-market sized from the position (the bot's fallback). Records accepted or refused.
  6. Lists what rests. Cancels stop A. Lists again.
  7. With C resting, attempts a new closePosition stop D. Records accepted or refused (does a resting reduceOnly stop block a closePosition one?).
  8. Closes the position with a reduceOnly market order and, with a stop still resting, lists the conditional orders: does Binance remove them when the position is flat?
  9. Cancels anything of this script's that is left, and confirms nothing rests and the position is flat.
It prints a plain report (no key, no signature, no request). Testnet may differ from mainnet: the real confirmation is a live trade where the profit lock moves a stop (the journal then shows the code and text).
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

PREFIX = "chk2stop"


def _cid(tag: str) -> str:
    return f"{PREFIX}-{tag}-{int(time.time()) % 10_000_000}"


class Report:
    def __init__(self, say=print):
        self.say = say
        self.rows: list[tuple[str, str]] = []

    def add(self, step: str, outcome: str) -> None:
        self.rows.append((step, outcome))
        self.say(f"  {step}: {outcome}")

    def text(self) -> str:
        width = max((len(s) for s, _ in self.rows), default=10)
        return "\n".join(f"{s.ljust(width)}  {o}" for s, o in self.rows)


def _describe(exc: BaseException) -> str:
    from mba_bot import venue as venue_mod

    info = venue_mod.exchange_error_info(exc)
    return f"REFUSED  class={info['error_class']} code={info['code']} message={info['msg']!r}"


def _kind(order: dict) -> str:
    info = order.get("info") or {}
    if str(info.get("closePosition")).lower() == "true" or order.get("closePosition") is True:
        return "closePosition"
    if str(info.get("reduceOnly")).lower() == "true" or order.get("reduceOnly") is True:
        return "reduceOnly"
    return "other"


async def _resting(client, symbol: str) -> list[dict]:
    rows = await client.fetch_open_orders(symbol, None, None, {"trigger": True})
    return [o for o in rows or [] if isinstance(o, dict)]


def _list_text(rows: list[dict]) -> str:
    if not rows:
        return "nothing rests"
    return "; ".join(f"{(o.get('clientOrderId') or '?')[-16:]} {_kind(o)} trigger={o.get('stopPrice') or o.get('triggerPrice')}" for o in rows)


async def run_check(client, venue, report: Report, *, confirm=None) -> bool:
    """The whole check against `client` (a demo client). Returns True when it finished and left nothing behind. `confirm`, if given, is called before any order is sent (the CLI asks the person)."""
    symbol = venue.symbol
    mine: list[str] = []
    opened = False
    try:
        await venue.prepare()
        rules = venue._require_ready()
        mode = await client.fetch_position_mode(symbol)
        if isinstance(mode, dict) and mode.get("hedged"):
            report.add("start", "ABORTED: the account is in hedge mode; the bot supports one-way mode only. Nothing was sent.")
            return False
        if abs(await venue.signed_position()) > 0:
            report.add("start", "ABORTED: there is already a position on this symbol. Nothing was sent.")
            return False
        if await _resting(client, symbol):
            report.add("start", "ABORTED: conditional orders already rest on this symbol. Nothing was sent.")
            return False
        ticker = await client.fetch_ticker(symbol)
        price = float(ticker["last"])
        floor_qty = max(rules.min_qty, rules.market_min_qty, (rules.min_notional or 0.0) * 1.25 / price)
        qty = math.ceil(floor_qty / rules.step - 1e-9) * rules.step
        qty = float(f"{qty:.12g}")
        report.add("start", f"flat, one-way mode, nothing resting; {symbol} at about {price}; minimum position {qty}")
        if confirm is not None and not confirm(symbol, qty):
            report.add("start", "stopped by you before any order")
            return False

        await client.create_order(symbol, "market", "buy", qty, None, {"clientOrderId": _cid("open")})
        opened = True
        pos = abs(await venue.signed_position())
        report.add("1 open long", f"position {pos}")

        def trig(frac: float) -> float:
            return venue.round_stop(price * frac, side="long")

        base = {"workingType": "MARK_PRICE"}
        a_id = _cid("A")
        mine.append(a_id)
        try:
            await client.create_order(symbol, "market", "sell", None, None, {**base, "stopLossPrice": trig(0.97), "clientOrderId": a_id, "closePosition": True})
            report.add("2 stop A closePosition", "ACCEPTED")
        except Exception as exc:  # noqa: BLE001
            report.add("2 stop A closePosition", _describe(exc))
        b_id = _cid("B")
        mine.append(b_id)
        try:
            await client.create_order(symbol, "market", "sell", None, None, {**base, "stopLossPrice": trig(0.975), "clientOrderId": b_id, "closePosition": True})
            report.add("3 stop B second closePosition, same direction", "ACCEPTED (Binance did NOT refuse a second closePosition stop)")
        except Exception as exc:  # noqa: BLE001
            report.add("3 stop B second closePosition, same direction", _describe(exc))
        c_id = _cid("C")
        mine.append(c_id)
        try:
            await client.create_order(symbol, "market", "sell", pos, None, {**base, "stopLossPrice": trig(0.98), "clientOrderId": c_id, "reduceOnly": True})
            report.add("4 stop C reduceOnly with the position quantity", "ACCEPTED")
        except Exception as exc:  # noqa: BLE001
            report.add("4 stop C reduceOnly with the position quantity", _describe(exc))
        report.add("5 what rests now", _list_text(await _resting(client, symbol)))
        try:
            await client.cancel_order(a_id, symbol, {"clientOrderId": a_id, "trigger": True})
            report.add("6 cancel stop A", "cancelled")
        except Exception as exc:  # noqa: BLE001
            report.add("6 cancel stop A", _describe(exc))
        report.add("7 what rests after the cancel", _list_text(await _resting(client, symbol)))
        d_id = _cid("D")
        mine.append(d_id)
        try:
            await client.create_order(symbol, "market", "sell", None, None, {**base, "stopLossPrice": trig(0.985), "clientOrderId": d_id, "closePosition": True})
            report.add("8 stop D closePosition while stop C (reduceOnly) rests", "ACCEPTED")
        except Exception as exc:  # noqa: BLE001
            report.add("8 stop D closePosition while stop C (reduceOnly) rests", _describe(exc))
        report.add("9 what rests before the close", _list_text(await _resting(client, symbol)))
        await client.create_order(symbol, "market", "sell", abs(await venue.signed_position()), None, {"reduceOnly": True, "clientOrderId": _cid("close")})
        opened = False
        await asyncio.sleep(2.0)
        report.add("10 position after the close", f"{abs(await venue.signed_position())}")
        report.add("11 stops still resting on the FLAT position (does Binance remove them?)", _list_text(await _resting(client, symbol)))
    except Exception as exc:  # noqa: BLE001
        report.add("ERROR", f"the check stopped early: {type(exc).__name__}; cleaning up")
    finally:
        # clean up whatever this script left, whatever happened
        for cid in mine:
            try:
                await client.cancel_order(cid, symbol, {"clientOrderId": cid, "trigger": True})
            except Exception:  # noqa: BLE001
                pass
        try:
            left = abs(await venue.signed_position())
            if opened and left > 0:
                await client.create_order(symbol, "market", "sell", left, None, {"reduceOnly": True, "clientOrderId": _cid("final")})
                await asyncio.sleep(1.0)
        except Exception as exc:  # noqa: BLE001
            report.add("CLEANUP", f"could not close the position: {type(exc).__name__}; close it in the Demo Trading app")
    clean = True
    try:
        flat = abs(await venue.signed_position()) == 0
        rest = await _resting(client, symbol)
        report.add("final", f"position {'flat' if flat else 'NOT FLAT'}; conditional orders: {_list_text(rest)}")
        clean = flat and not rest
    except Exception as exc:  # noqa: BLE001
        report.add("final", f"could not read the final state ({type(exc).__name__}); check the Demo Trading app")
        clean = False
    return clean


def _hidden(prompt: str) -> str:
    return getpass.getpass(prompt).strip()


async def _main(args) -> int:
    from mba_bot import exchange, log_safety, venue as venue_mod

    log_safety.quiet_http_client_logs()
    print("This checks Binance Futures DEMO TRADING only. Use demo keys; real keys do not work there and are never used here.")
    key = _hidden("Demo API key (hidden): ")
    secret = _hidden("Demo API secret (hidden): ")
    if not key or not secret:
        print("No key entered. Nothing was done.")
        return 2
    client = exchange.build_exchange_client(exchange="binance", market_type="futures", api_key=key, api_secret=secret, sandbox=True)
    del key, secret
    symbol = exchange.exchange_symbol(args.symbol, "futures")
    venue = venue_mod.build_venue(client, market_type="futures", pair=args.symbol, entries_allowed=True)
    report = Report()

    def confirm(sym, qty):
        return input(f"Open a minimum position of {qty} on {sym} on the DEMO system now? Type yes: ").strip().lower() == "yes"

    try:
        ok = await run_check(client, venue, report, confirm=confirm)
    finally:
        await client.close()
    print("\n=== REPORT (no key, no signature, no request) ===")
    print(report.text())
    print(f"\nFinished clean: {'yes' if ok else 'NO: look at the lines above and the Demo Trading app'}")
    print("Reminder: the demo system may differ from the production system. The real confirmation is a live trade where the profit lock moves a stop.")
    if args.out:
        Path(args.out).write_text(report.text() + "\n")
        print(f"Report written to {args.out}")
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Binance Futures Demo Trading check of the second protective stop. Keys are typed at a hidden prompt.")
    ap.add_argument("--symbol", default="BTC/USDT", help="pair in the platform's style (default BTC/USDT)")
    ap.add_argument("--out", default=None, help="also write the report to this file")
    return asyncio.run(_main(ap.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
