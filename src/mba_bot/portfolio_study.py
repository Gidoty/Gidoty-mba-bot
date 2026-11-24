"""The portfolio view of the EMA 4h and 1d trades (docs/research/ema_portfolio_preregistration.md). Research only: no network, no key, no order, no live effect.

The SAME trades as the EMA universe study (development windows only, 2022-04 to 2025-03; the confirmation windows are not read), combined as one account that can hold at most k positions: k = 1 (first signal wins, ties by
catalog order, as Auto Select scans), 2, 3, and unlimited. A signal that arrives while every slot is full is dropped (the live entry window is 10 minutes after the candle closes, so a signal cannot wait). Equity moves
when a trade closes: equity x (1 + risk x net R), risk per trade 1.0% (the bot's default risk percent), also 0.5% and 2.0%. Closed-trade equity only; open trades are not marked to market."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from . import ema_universe as U
from . import entry_study as E
from . import htf_study as H
from .research import bootstrap

PROTOCOL = "portfolio view v1: the EMA universe development trades under 1, 2, 3 and unlimited slots"
SLOTS = (1, 2, 3, None)  # None = unlimited
RISKS = (0.5, 1.0, 2.0)  # percent of equity risked per trade
PRIMARY_RISK = 1.0
BOOT = dict(U.BOOT)
YEAR = 365.25 * U.DAY


def slot_name(k) -> str:
    return "unlimited" if k is None else f"{k} slot" + ("s" if k > 1 else "")


_PINNED = ("portfolio_study.py", "ema_universe.py", "htf_study.py", "cost_sensitivity.py", "entry_study.py", "profit_backtest.py", "shortframe_backtest.py", "research/bootstrap.py")


def plan_blob() -> dict:
    here = Path(__file__).parent
    return {"protocol": PROTOCOL, "slots": SLOTS, "risks": RISKS, "primary_risk": PRIMARY_RISK, "cells": U.CELLS, "span": (E.TEST_STARTS[0], E.TEST_STARTS[6]), "tie": "catalog order", "bootstrap": BOOT,
            "equity": "closed trades only, compounding at each close in exit order", "sources_sha256": {n: hashlib.sha256((here / n).read_bytes()).hexdigest() for n in _PINNED}}


def plan_hash() -> str:
    return hashlib.sha256(json.dumps(plan_blob(), sort_keys=True, default=str).encode()).hexdigest()[:16]


def end_ts(t) -> int:
    return t.entry_ts + t.hold_ms


def take(trades: list, k) -> tuple[list, list]:
    """(taken, dropped). Trades in signal order (entry time, then catalog order); a trade is taken when fewer than k taken trades are still open at its entry time (a position that ends exactly when the signal arrives is closed)."""
    taken, dropped, open_ends = [], [], []
    for t in sorted(trades, key=U.order_key):
        open_ends = [e for e in open_ends if e > t.entry_ts]
        if k is None or len(open_ends) < k:
            taken.append(t)
            open_ends.append(end_ts(t))
        else:
            dropped.append(t)
    return taken, dropped


def losing_streak(trades: list) -> int:
    """The longest run of consecutive losing trades in closing order."""
    best = cur = 0
    for t in sorted(trades, key=lambda x: (end_ts(x), U.CATALOG.index(x.pair))):
        cur = cur + 1 if t.net_r < 0 else 0
        best = max(best, cur)
    return best


def drawdowns(trades: list, risk_pct: float) -> dict:
    """Largest peak-to-trough fall of the cumulative R (closing order) and of the compounded equity (risk_pct of equity per R); the lowest equity as a share of the start."""
    r_cum = peak_r = max_dd_r = 0.0
    eq = peak_eq = 1.0
    max_dd_eq = 0.0
    low = 1.0
    for t in sorted(trades, key=lambda x: (end_ts(x), U.CATALOG.index(x.pair))):
        r_cum += t.net_r
        peak_r = max(peak_r, r_cum)
        max_dd_r = max(max_dd_r, peak_r - r_cum)
        eq *= max(0.0, 1.0 + risk_pct / 100.0 * t.net_r)
        peak_eq = max(peak_eq, eq)
        max_dd_eq = max(max_dd_eq, 1.0 - eq / peak_eq)
        low = min(low, eq)
    return {"max_dd_r": max_dd_r, "max_dd_equity": max_dd_eq, "lowest_equity": low, "final_equity": eq}


def tail(trades: list) -> dict:
    nets = sorted((t.net_r for t in trades), reverse=True)
    n, tot = len(nets), sum(nets)
    return {"win_rate": (sum(1 for x in nets if x > 0) / n) if n else None, "median_r": (sorted(nets)[n // 2] if n else None), "top5_share_of_total": (sum(nets[:5]) / tot) if tot > 0 else None,
            "best_r": nets[0] if n else None, "worst_r": nets[-1] if n else None}


def summarize(trades: list, k, *, span_ms: int) -> dict:
    taken, dropped = take(trades, k)
    blocks = {}
    for t in taken:
        blocks[H._month(t.entry_ts)] = blocks.get(H._month(t.entry_ts), 0.0) + t.net_r
    n = len(taken)
    years = span_ms / YEAR
    return {"slots": slot_name(k), "signals": len(trades), "taken": n, "dropped": len(dropped), "taken_share": (n / len(trades)) if trades else None, "taken_per_year": n / years,
            "total_net_r": sum(t.net_r for t in taken), "mean_net_r": (sum(t.net_r for t in taken) / n) if n else None, "total_block_ci": bootstrap.block_sum_ci(blocks, **BOOT),
            "dropped_total_net_r": sum(t.net_r for t in dropped), "dropped_mean_net_r": (sum(t.net_r for t in dropped) / len(dropped)) if dropped else None,
            "losing_streak": losing_streak(taken), "tail": tail(taken), "pilot_months_100": (100 / (n / years) * 12.0) if n else None,
            "dd": {str(r): drawdowns(taken, r) for r in RISKS}, "max_open": max_open(taken), "open_risk_pct_max": max_open(taken) * PRIMARY_RISK}


def max_open(trades: list) -> int:
    ev = sorted([(t.entry_ts, 1) for t in trades] + [(end_ts(t), -1) for t in trades], key=lambda x: (x[0], x[1]))  # at the same time a close comes before an open
    cur = peak = 0
    for _, d in ev:
        cur += d
        peak = max(peak, cur)
    return peak


def run(root: Path, out_dir: Path, *, expect_plan_hash: str | None = None) -> dict:
    ph = plan_hash()
    if expect_plan_hash and expect_plan_hash != ph:
        raise SystemExit(f"plan hash {ph} does not match the registered {expect_plan_hash}")
    sims, _ = U.simulate(root)
    lo, hi = E.TEST_STARTS[0], E.TEST_STARTS[6]  # the confirmation windows are not read
    res = {}
    for cell, trades in sims.items():
        dev = [t for t in trades if lo <= t.entry_ts < hi]
        res[H.cell_name(cell)] = {slot_name(k): summarize(dev, k, span_ms=hi - lo) for k in SLOTS}
        res[H.cell_name(cell) + " BNB"] = {slot_name(k): summarize(U.with_bnb(dev), k, span_ms=hi - lo) for k in SLOTS}
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "portfolio_results.json").write_text(json.dumps({"plan_hash": ph, "results": res}, indent=1, default=str))
    (out_dir / "portfolio_report.md").write_text(report(res))
    return res


def report(res: dict) -> str:
    f = lambda x, s="+.1f": "n/a" if x is None else format(x, s)  # noqa: E731
    L = [f"# EMA 4h and 1d as one account: development windows (2022-04 to 2025-03)", "", f"Plan hash `{plan_hash()}`. Risk per trade {PRIMARY_RISK}% of equity (drawdown also at {RISKS[0]}% and {RISKS[-1]}% in the JSON). Standard fees.", "",
         "| cell | slots | signals | taken | taken per year | dropped | total net R [99% interval] | mean net R | dropped trades' total R | max drawdown R | max drawdown equity | lowest equity | final equity | longest losing streak | max open | trades to 100 (months) |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for cell, per in res.items():
        if cell.endswith("BNB"):
            continue
        for name, s in per.items():
            d = s["dd"][str(PRIMARY_RISK)]
            ci = s["total_block_ci"]
            L.append(f"| {cell} | {name} | {s['signals']} | {s['taken']} | {s['taken_per_year']:.1f} | {s['dropped']} | {f(s['total_net_r'])} [{f(ci[1]) if ci else 'n/a'}, {f(ci[2]) if ci else 'n/a'}] | {f(s['mean_net_r'], '+.3f')} | "
                     f"{f(s['dropped_total_net_r'])} | {d['max_dd_r']:.1f} | {d['max_dd_equity'] * 100:.0f}% | {d['lowest_equity']:.2f} | {d['final_equity']:.2f} | {s['losing_streak']} | {s['max_open']} | {f(s['pilot_months_100'], '.1f')} |")
    L += ["", "Shape of the taken trades:", "", "| cell | slots | win rate | median R | best R | worst R | top 5 trades' share of total |", "|---|---|---|---|---|---|---|"]
    for cell, per in res.items():
        if cell.endswith("BNB"):
            continue
        for name, s in per.items():
            t = s["tail"]
            L.append(f"| {cell} | {name} | {f(t['win_rate'] and t['win_rate'] * 100, '.0f')}% | {f(t['median_r'], '+.2f')} | {f(t['best_r'])} | {f(t['worst_r'])} | {f(t['top5_share_of_total'] and t['top5_share_of_total'] * 100, '.0f')}% |")
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mba_bot.portfolio_study")
    ap.add_argument("--root", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--expect-plan-hash")
    ap.add_argument("--plan", action="store_true")
    a = ap.parse_args(argv)
    if a.plan:
        print(plan_hash())
        return 0
    try:
        run(a.root, a.out, expect_plan_hash=a.expect_plan_hash)
    except Exception as exc:  # noqa: BLE001
        print(f"portfolio study failed ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 2
    print(f"done; report in {a.out}/portfolio_report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
