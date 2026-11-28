#!/usr/bin/env python3
"""Take-profit reach report: how far did the closed trades run, and what would a take profit at TP1 to TP5 have done? READ ONLY. Run by the owner.

    python3 ops/reports/take_profit_reach_report.py --db /path/to/mba_bot.db [--owner-email YOU@EXAMPLE.COM]

What it does
  * Opens the SQLite database read-only (`mode=ro`, `PRAGMA query_only`): it cannot create, change or lock anything, it needs no key and imports nothing from the bot.
  * Takes every CLOSED live trade and paper trade (demo trades are left out) that has a best price (`peak_price`) and an initial stop (`initial_stop_price`). Trades without them are counted and skipped.
  * R is the distance from the entry to the INITIAL stop. Peak R is how far the best price was beyond the entry, in R (never below 0). A level is "reached" when peak R is at least 1, 1.5, 2, 3, 4 or 5.
  * Hypothetical result with a take profit at level L (an ASSUMPTION, not a measurement): a trade that reached L closes at exactly L R, minus an estimated round-trip cost; a trade that did not reach L ends as it
    did. The initial stop and the bot's own exits are assumed unchanged. The cost estimate is the assumed taker fee on both fills plus the assumed round-trip slippage (defaults 5 bps per side and 11 bps, the
    bot's own assumed figures), as a share of the entry price. Both numbers can be changed with --fee-bps and --slip-rt-bps.
  * To compare like with like, every trade also gets a "basis result": its stored gross P&L minus the same cost estimate. The actual net result (the stored P&L after the real fees and funding) is shown where the
    database knows it; it is unknown for practice trades and for trades whose fee was paid in BNB.

What it prints: totals and tables by kind (live or practice), strategy and pair; and, if --owner-email is given, the owner's own live trades one by one under a short trade code (the first 8 characters of the
trade id). It prints NO account id, email, key, customer name or other customer data: the owner's email is used only to select his rows and is never printed. Other customers' trades appear only inside the
aggregates. It never writes a file.

Limits: the best price is what the bot observed (the live price each round and candle highs and lows), so a very short spike can be missing. The hypothetical results ignore the order of events inside a candle, the
real fill of a monitored market exit, polling gaps and the single-slot effect of closing earlier. Small groups mean little; the counts are printed beside every figure."""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from collections import defaultdict

LEVELS = (1.0, 1.5, 2.0, 3.0, 4.0, 5.0)
EMA_ID = "ema_cross_atr_v1"  # strategy_id NULL means this one (multi_trade.strategy_of)
DEFAULT_FEE_BPS, DEFAULT_SLIP_RT_BPS = 5.0, 11.0

QUERY = """
SELECT t.id, t.side, t.is_paper, t.entry_price, t.exit_price, t.quantity, t.pnl, t.initial_stop_price, t.peak_price,
       t.entry_fee_quote, t.exit_fee_quote, t.funding_quote, t.protective_kind, t.strategy_id, t.pair,
       CASE WHEN :owner IS NOT NULL AND lower(c.email) = lower(:owner) THEN 1 ELSE 0 END AS is_owner
FROM trades t
LEFT JOIN trading_accounts a ON a.id = t.account_id
LEFT JOIN customers c ON c.id = a.customer_id
WHERE t.status = 'closed' AND COALESCE(t.is_demo, 0) = 0
"""


def open_readonly(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only = ON")
    return con


def _num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def analyse(row, *, fee_bps: float, slip_rt_bps: float):
    """One trade -> a dict of figures, or (None, reason) when it cannot be used."""
    side = row["side"]
    entry, qty, pnl = _num(row["entry_price"]), _num(row["quantity"]), _num(row["pnl"])
    stop, peak = _num(row["initial_stop_price"]), _num(row["peak_price"])
    if side not in ("long", "short"):
        return None, "no_side"
    if peak is None or stop is None:
        return None, "no_peak_or_initial_stop"
    if entry is None or qty is None or qty <= 0 or pnl is None:
        return None, "no_entry_quantity_or_result"
    r_price = abs(entry - stop)
    if not r_price > 0:
        return None, "zero_r"
    long_side = side == "long"
    gain = (peak - entry) if long_side else (entry - peak)
    peak_r = max(0.0, gain / r_price)
    r_quote = r_price * qty
    cost = entry * qty * (2.0 * fee_bps + slip_rt_bps) / 10_000.0
    basis = pnl - cost
    paper = bool(row["is_paper"])
    net = None
    if not paper:
        fees = (_num(row["entry_fee_quote"]), _num(row["exit_fee_quote"]))
        funding = _num(row["funding_quote"])
        spot = not str(row["protective_kind"] or "").startswith("stop_market")  # funding exists only on Futures (reporting.funding_applies)
        if fees[0] is not None and fees[1] is not None and (funding is not None or spot):
            net = pnl - fees[0] - fees[1] + (funding if funding is not None else 0.0)
    out = {
        "code": str(row["id"])[:8], "kind": "practice" if paper else "live", "strategy": row["strategy_id"] or EMA_ID, "pair": row["pair"] or "?", "side": side,
        "r_pct": 100.0 * r_price / entry if entry else None, "peak_r": peak_r, "r_quote": r_quote, "cost": cost, "gross": pnl, "net": net,
        "basis_r": basis / r_quote, "owner": bool(row["is_owner"]),
    }
    out["reached"] = {lv: peak_r >= lv - 1e-9 for lv in LEVELS}
    out["hyp_r"] = {lv: ((lv - cost / r_quote) if out["reached"][lv] else out["basis_r"]) for lv in LEVELS}
    out["hyp_quote"] = {lv: ((lv * r_quote - cost) if out["reached"][lv] else basis) for lv in LEVELS}
    return out, None


def collect(con: sqlite3.Connection, owner_email: str | None, *, fee_bps: float, slip_rt_bps: float):
    seen = 0
    skipped: dict[str, int] = defaultdict(int)
    trades = []
    for row in con.execute(QUERY, {"owner": owner_email}):
        seen += 1
        t, why = analyse(row, fee_bps=fee_bps, slip_rt_bps=slip_rt_bps)
        if t is None:
            skipped[why] += 1
        else:
            trades.append(t)
    return seen, dict(skipped), trades


def _pct(n, d):
    return f"{100.0 * n / d:.0f}%" if d else "-"


def _group_lines(title: str, trades: list) -> list[str]:
    n = len(trades)
    lines = [title]
    if not n:
        return lines + ["  no trades"]
    basis = sum(t["basis_r"] for t in trades) / n
    lines.append(f"  trades {n}; average basis result {basis:+.3f} R (stored gross P&L minus the estimated round-trip cost)")
    lines.append("  level   reached      average result if a take profit fired there   change against the basis")
    for lv in LEVELS:
        hit = sum(1 for t in trades if t["reached"][lv])
        hyp = sum(t["hyp_r"][lv] for t in trades) / n
        lines.append(f"  TP {lv:<4g}  {hit:>4} ({_pct(hit, n):>4})  {hyp:+.3f} R                                      {hyp - basis:+.3f} R")
    return lines


def render(seen: int, skipped: dict, trades: list, *, fee_bps: float, slip_rt_bps: float, owner_given: bool) -> str:
    L = ["TAKE-PROFIT REACH REPORT (read only)", ""]
    L.append(f"Closed live and practice trades found: {seen}. Used: {len(trades)}. Skipped: {sum(skipped.values())}" + (" (" + ", ".join(f"{k}: {v}" for k, v in sorted(skipped.items())) + ")" if skipped else "") + ".")
    L.append(f"Assumed round-trip cost: {2 * fee_bps + slip_rt_bps:g} bps of the entry price (fee {fee_bps:g} bps per side, slippage {slip_rt_bps:g} bps round trip). This is an assumption.")
    L.append("Hypothetical: a trade that reached the level closes there, at the level minus the estimated cost; a trade that did not reach it ends as it did. Stops and own exits are assumed unchanged.")
    L.append("")
    by_kind: dict = defaultdict(list)
    by_strat: dict = defaultdict(list)
    by_pair: dict = defaultdict(list)
    for t in trades:
        by_kind[t["kind"]].append(t)
        by_strat[(t["kind"], t["strategy"])].append(t)
        by_pair[(t["kind"], t["strategy"], t["pair"])].append(t)
    for kind in ("live", "practice"):
        if kind in by_kind:
            L += _group_lines(f"== {kind.upper()}: all strategies and pairs", by_kind[kind]) + [""]
    for key in sorted(by_strat):
        L += _group_lines(f"-- {key[0]} / {key[1]}", by_strat[key]) + [""]
    for key in sorted(by_pair):
        L += _group_lines(f"-- {key[0]} / {key[1]} / {key[2]}", by_pair[key]) + [""]
    mine = [t for t in trades if t["owner"] and t["kind"] == "live"]
    L.append("== YOUR OWN LIVE TRADES")
    if not owner_given:
        L.append("  --owner-email was not given: no individual trades are listed.")
    elif not mine:
        L.append("  none of your closed live trades has peak and initial-stop data (or no account matched).")
    else:
        L.append("  code      pair        side  strategy            R%     peakR  reached        actual (quote)             hypothetical result if the take profit fired (quote)")
        for t in sorted(mine, key=lambda x: x["code"]):
            reached = ",".join(f"{lv:g}" for lv in LEVELS if t["reached"][lv]) or "-"
            actual = f"{t['net']:+.4f} net" if t["net"] is not None else f"{t['gross']:+.4f} gross (net unknown)"
            hyp = "  ".join(f"TP{lv:g} {t['hyp_quote'][lv]:+.4f}" if t["reached"][lv] else f"TP{lv:g} -" for lv in LEVELS)
            L.append(f"  {t['code']}  {t['pair']:<10}  {t['side']:<5} {t['strategy']:<18} {t['r_pct']:.2f}  {t['peak_r']:>5.2f}  {reached:<13}  {actual:<26} {hyp}")
        L.append("  ('-' = the level was not reached, so the trade ended as it did. Quote amounts are in the pair's quote currency.)")
    L.append("")
    L.append("Limits: the best price is what the bot observed, so a short spike can be missing; real take-profit fills, polling gaps, the order of events inside a candle and the single slot are not modelled. Small groups mean little.")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Read-only take-profit reach report. Opens the database read-only and prints no customer data.")
    ap.add_argument("--db", required=True, help="path to the SQLite database file")
    ap.add_argument("--owner-email", default=None, help="your own login email: lists YOUR live trades by short code (never printed)")
    ap.add_argument("--fee-bps", type=float, default=DEFAULT_FEE_BPS, help="assumed taker fee per side in bps (default 5)")
    ap.add_argument("--slip-rt-bps", type=float, default=DEFAULT_SLIP_RT_BPS, help="assumed round-trip slippage in bps (default 11)")
    args = ap.parse_args(argv)
    try:
        con = open_readonly(args.db)
        seen, skipped, trades = collect(con, args.owner_email, fee_bps=args.fee_bps, slip_rt_bps=args.slip_rt_bps)
        con.close()
    except sqlite3.Error as exc:
        print(f"Could not read the database read-only ({type(exc).__name__}). Nothing was changed.", file=sys.stderr)
        return 2
    print(render(seen, skipped, trades, fee_bps=args.fee_bps, slip_rt_bps=args.slip_rt_bps, owner_given=bool(args.owner_email)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
