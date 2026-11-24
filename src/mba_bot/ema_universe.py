"""EMA trend following on 4h and 1d across the bot's whole pair catalog (docs/research/ema_universe_preregistration.md). Research only: no network, no key, no order, no live effect.

Why: the earlier studies left one candidate with a possible edge, the EMA crossover on 4h and 1d candles (positive point estimates on four pairs, not distinguishable from zero). This study asks the same question on every pair of the
bot's own catalog that the public archive covers with enough history and liquidity, with NOTHING tuned: the shipped EMA periods 20/50 and the classic 12/26, the bot's own 2 ATR stop and opposite-crossover exit, the same costs
(5 bps taker per side, 11 bps round-trip slippage, historical funding) and, as a second reading, the BNB fee discount. 4h and daily candles are built from complete groups of 1h candles.
Walk-forward: the nine 182-day windows of the entry study; the first six (2022-04 to 2025-03) are DEVELOPMENT, the last three (2025-04 to 2026-09) are CONFIRMATION and stay unread unless a cell passes development."""
from __future__ import annotations

import argparse
import csv
import dataclasses
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import cost_sensitivity as CSN
from . import entry_study as E
from . import htf_study as H
from . import profit_backtest as P
from . import shortframe_backtest as SF
from .research import bootstrap, funding_archive

PROTOCOL = "ema universe v1: EMA 20/50 and 12/26 on 4h and 1d, every catalog pair that clears the liquidity rule, shipped stop and exit, walk-forward development and one confirmation look"
# The bot's own catalog in scan order (config.ALL_KNOWN_PAIRS: SUPPORTED_PAIRS then FUTURES_EXTRA_PAIRS); a test pins this list against config.
CATALOG = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT", "DOGEUSDT", "SUIUSDT", "LINKUSDT", "ADAUSDT", "LTCUSDT")
FOUR = CATALOG[:4]
LIQUIDITY = {"min_years_of_1h_candles": 3.0, "min_median_daily_quote_volume_usd": 50_000_000, "volume_days": 365, "as_of": "2026-10-31"}
CELLS = (("ema", "4h", "s1"), ("ema", "4h", "s2"), ("ema", "1d", "s1"), ("ema", "1d", "s2"))
EMA_SETS = H.EMA_SETS
DEV_WINDOWS, CONFIRM_WINDOWS = H.DEV_WINDOWS, H.CONFIRM_WINDOWS
RULE = {"dev_min_trades": 150, "dev_min_positive_windows": 5, "dev_min_positive_pairs": 6, "confirm_min_trades": 60, "confirm_min_positive_windows": 2}
LEVEL = H.LEVEL
BOOT = H.BOOT
BNB = {"tier": "regular", "bnb": True, "maker_entry": False}
DAY, HOUR = H.DAY, H.HOUR
CONCURRENCY_SPAN = ("2023-09-01", "2025-04-01")  # every pair has data and every EMA warm-up is over
PILOT_TRADES = 100


def _ms(day: str) -> int:
    return E._ms(day)


_PINNED = ("ema_universe.py", "htf_study.py", "cost_sensitivity.py", "entry_study.py", "profit_backtest_dr.py", "profit_backtest.py", "shortframe_backtest.py", "indicators.py", "strategy.py", "reporting.py",
           "research/bootstrap.py", "research/archive.py", "research/funding_archive.py")


def source_pins() -> dict:
    here = Path(__file__).parent
    return {n: hashlib.sha256((here / n).read_bytes()).hexdigest() for n in _PINNED}


def plan_blob() -> dict:
    return {"protocol": PROTOCOL, "catalog": CATALOG, "four": FOUR, "liquidity": LIQUIDITY, "cells": CELLS, "ema_sets": EMA_SETS, "ema": SF.EMA_PARAMS, "run": (E.RUN_START, E.RUN_END), "test_starts": E.TEST_STARTS,
            "test_len": E.TEST_LEN, "dev_windows": DEV_WINDOWS, "confirm_windows": CONFIRM_WINDOWS, "rule": RULE, "level": LEVEL, "bootstrap": BOOT, "bnb_reading": BNB, "bnb_discount": CSN.BNB_DISCOUNT,
            "costs": {"fee_bps": P.FEE_BPS, "slippage_round_trip_bps": P.SLIP_ROUND_TRIP_BPS}, "concurrency_span": CONCURRENCY_SPAN, "pilot_trades": PILOT_TRADES,
            "trades_per_year_span": "2020-04-01 (or the pair's first candle) to the first confirmation window, counts only", "supplementary": "2020-04-01 to the first development window, reported, not part of any rule",
            "sources_sha256": source_pins()}


def plan_hash() -> str:
    return hashlib.sha256(json.dumps(plan_blob(), sort_keys=True, default=str).encode()).hexdigest()[:16]


def data_hash(root: Path) -> str:
    h = hashlib.sha256()
    for sym in CATALOG:
        for sub in ("klines/1h", "funding"):
            p = Path(root) / sub / f"{sym}.csv"
            if not p.exists():
                raise FileNotFoundError(f"missing {sub}/{sym}.csv under {root}")
            h.update(f"{sub}{sym}".encode() + hashlib.sha256(p.read_bytes()).hexdigest().encode())
    return h.hexdigest()[:16]


# ================================================================================ universe
def liquidity_table(root: Path) -> dict:
    """For every catalog pair: years of 1h candles, the median daily quote volume (USD) over the last `volume_days` days of the file, and whether it clears the registered rule. Reads only the candle file."""
    out = {}
    for sym in CATALOG:
        p = Path(root) / "klines" / "1h" / f"{sym}.csv"
        if not p.exists():
            out[sym] = {"years": 0.0, "median_daily_quote_volume": 0.0, "ok": False}
            continue
        daily, first, last = {}, None, None
        with p.open() as fh:
            for row in csv.DictReader(fh):
                ts = int(row["ts"])
                first = ts if first is None else first
                last = ts
                daily[ts // DAY] = daily.get(ts // DAY, 0.0) + float(row["quote_volume"])
        days = sorted(daily)[-LIQUIDITY["volume_days"]:]
        vals = sorted(daily[d] for d in days)
        med = (vals[len(vals) // 2] if len(vals) % 2 else (vals[len(vals) // 2 - 1] + vals[len(vals) // 2]) / 2) if vals else 0.0
        years = ((last - first) / (365.25 * DAY)) if first is not None else 0.0
        out[sym] = {"years": years, "median_daily_quote_volume": med, "ok": years >= LIQUIDITY["min_years_of_1h_candles"] and med >= LIQUIDITY["min_median_daily_quote_volume_usd"]}
    return out


def qualifying(root: Path) -> list[str]:
    t = liquidity_table(root)
    return [s for s in CATALOG if t[s]["ok"]]


# ================================================================================ simulation
def load(root: Path):
    one_h = {pair: sorted(SF._store(Path(root), pair, "1h"), key=lambda c: c.timestamp) for pair in CATALOG}
    funding = {pair: funding_archive.load(Path(root), pair) for pair in CATALOG}
    return one_h, funding


def order_key(t):
    return (t.entry_ts, CATALOG.index(t.pair))


def simulate(root: Path, cells=CELLS) -> tuple[dict, dict]:
    """({cell: trades sorted by entry time then catalog order}, {(pair, tf): first candle time}). Every catalog pair that has candle data."""
    one_h, funding = load(root)
    htf, firsts = {}, {}
    for pair in CATALOG:
        for tf, g in H.GROUP.items():
            htf[(pair, tf)] = H.aggregate(one_h[pair], g)
            firsts[(pair, tf)] = htf[(pair, tf)][0].timestamp if htf[(pair, tf)] else None
    out = {}
    for cell in cells:
        _, tf, s = cell
        trades = []
        for pair in CATALOG:
            if htf[(pair, tf)]:
                trades += H.sim_ema(pair, htf[(pair, tf)], H.GROUP[tf], EMA_SETS[s], E.RUN_START, E.RUN_END, funding[pair])
        out[cell] = sorted(trades, key=order_key)
    return out, firsts


def with_bnb(trades: list) -> list:
    """The same trades with the net R recomputed for fees paid in BNB (10% off); nothing else changes."""
    return [dataclasses.replace(t, net_r=CSN.recost(t, BNB)) for t in trades]


# ================================================================================ evaluation
def evaluate(trades: list, windows: tuple, min_trades: int, min_positive: int, min_pairs: int) -> dict:
    e = H.evaluate(trades, windows, min_trades, min_positive)
    pooled = [t for t in trades if E.window_of(t.entry_ts) in windows]
    per_pair = {}
    for pair in CATALOG:
        sel = [t for t in pooled if t.pair == pair]
        per_pair[pair] = {"trades": len(sel), "total_net_r": sum(t.net_r for t in sel), "mean_net_r": (sum(t.net_r for t in sel) / len(sel)) if sel else None}
    with_trades = {p: v for p, v in per_pair.items() if v["trades"]}
    positive_pairs = sum(1 for v in with_trades.values() if v["total_net_r"] > 0)
    best = max(with_trades, key=lambda p: with_trades[p]["total_net_r"]) if with_trades else None
    without_best = (e["total_net_r"] - with_trades[best]["total_net_r"]) if best else None
    e.update({"per_pair": per_pair, "positive_pairs": positive_pairs, "pairs_with_trades": len(with_trades), "best_pair": best, "total_without_best_pair": without_best,
              "P4": bool(best is not None and positive_pairs >= min_pairs and without_best > 0)})
    return e


def dev_pass(e: dict) -> bool:
    return bool(e["P1"] and e["P2"] and e["P3"] and e["P4"])


def supplementary(trades: list) -> dict:
    """The history before the first development window (2020-04 to 2022-03), pooled. Reported only; no rule reads it."""
    sel = [t for t in trades if t.entry_ts < E.TEST_STARTS[0]]
    n = len(sel)
    blocks = {}
    for t in sel:
        blocks[H._month(t.entry_ts)] = blocks.get(H._month(t.entry_ts), 0.0) + t.net_r
    return {"trades": n, "mean_net_r": (sum(t.net_r for t in sel) / n) if n else None, "total_net_r": sum(t.net_r for t in sel), "total_block_ci": bootstrap.block_sum_ci(blocks, **BOOT)}


def trades_per_year(trades: list, firsts: dict, tf: str, end_ms: int, pairs=CATALOG) -> dict:
    """Entries per year per pair over [max(2020-04-01, the pair's first candle), end_ms), and the sum over `pairs` (the rate if all of them are active). Counts only."""
    per_pair = {}
    for pair in CATALOG:
        f = firsts.get((pair, tf))
        if f is None:
            per_pair[pair] = {"trades": 0, "years": 0.0, "per_year": 0.0}
            continue
        start = max(E.RUN_START, f)
        years = (end_ms - start) / (365.25 * DAY)
        n = sum(1 for t in trades if t.pair == pair and start <= t.entry_ts < end_ms)
        per_pair[pair] = {"trades": n, "years": years, "per_year": (n / years) if years > 0 else 0.0}
    rate = lambda ps: sum(per_pair[p]["per_year"] for p in ps)  # noqa: E731
    return {"per_pair": per_pair, "universe_per_year": rate(pairs), "four_per_year": rate(FOUR), "one_pair_avg": rate(pairs) / len(pairs),
            "one_pair_min": min(per_pair[p]["per_year"] for p in pairs), "one_pair_max": max(per_pair[p]["per_year"] for p in pairs)}


def pilot_months(per_year: float, n: int = PILOT_TRADES) -> float | None:
    """Months until n trades have closed at `per_year` trades a year, or None for a rate of zero."""
    return (n / per_year * 12.0) if per_year > 0 else None


def concurrency(trades: list, start_ms: int, end_ms: int) -> dict:
    """How many positions the strategy would hold at once if every signal were taken: the time average, the share of time with at least 1 and at least 2, and the maximum. A trade occupies [entry, entry + hold) (to the end of
    its exit candle)."""
    events = []
    for t in trades:
        a, b = max(t.entry_ts, start_ms), min(t.entry_ts + t.hold_ms, end_ms)
        if b > a:
            events += [(a, 1), (b, -1)]
    events.sort()
    cur, last, area, ge1, ge2, peak = 0, start_ms, 0.0, 0.0, 0.0, 0
    for ts, d in events:
        dt = ts - last
        area += cur * dt
        ge1 += dt if cur >= 1 else 0
        ge2 += dt if cur >= 2 else 0
        cur += d
        peak = max(peak, cur)
        last = ts
    span = end_ms - start_ms
    return {"mean_open": area / span, "share_time_ge1": ge1 / span, "share_time_ge2": ge2 / span, "max_open": peak}


# ================================================================================ report
def _fmt(x, spec="+.3f"):
    return H._fmt(x, spec)


def report(title: str, rows: dict, bnb_rows: dict, tpy: dict, conc: dict, note: list[str]) -> str:
    L = [f"# {title}", "", f"Plan hash `{plan_hash()}`. 10 catalog pairs pooled, shipped stop and crossover exit, 5 bps taker per side, 11 bps round-trip slippage, historical funding, {int(LEVEL * 100)}% month-block bootstrap intervals.", "",
         "| cell | trades | trades per year, 10 pairs | per pair per year | avg stop distance | avg gross R | avg cost R | mean net R | total net R [interval] | windows > 0 | pairs > 0 | total without best pair | P1 | P2 | P3 | P4 | passes |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for cell, e in rows.items():
        t = tpy[cell]
        L.append(f"| {H.cell_name(cell)} | {e['trades']} | {t['universe_per_year']:.1f} | {t['one_pair_avg']:.1f} | {_fmt(e['avg_stop_pct'], '.2f')}% | {_fmt(e['avg_gross_r'])} | {_fmt(e['avg_cost_r'], '.3f')} | {_fmt(e['mean_net_r'])} | "
                 f"{_fmt(e['total_net_r'], '+.1f')} {H._ci(e['total_block_ci'])} | {e['positive_windows']} | {e['positive_pairs']} of {e['pairs_with_trades']} | {_fmt(e['total_without_best_pair'], '+.1f')} | "
                 + " | ".join("yes" if e[k] else "no" for k in ("P1", "P2", "P3", "P4")) + f" | {'YES' if dev_pass(e) else 'no'} |")
    L += ["", "With fees paid in BNB (10% off the fee, nothing else changed):", "", "| cell | mean net R | total net R [interval] | windows > 0 |", "|---|---|---|---|"]
    for cell, e in bnb_rows.items():
        L.append(f"| {H.cell_name(cell)} | {_fmt(e['mean_net_r'])} | {_fmt(e['total_net_r'], '+.1f')} {H._ci(e['total_block_ci'])} | {e['positive_windows']} |")
    L += ["", "Per window total net R (standard fees):", "", "| cell | " + " | ".join(f"w{k}" for k in sorted(set(w["window"] for e in rows.values() for w in e["per_window"]))) + " |", "|---|" + "---|" * len(next(iter(rows.values()))["per_window"])]
    for cell, e in rows.items():
        L.append(f"| {H.cell_name(cell)} | " + " | ".join(f"{w['net_total_r']:+.1f} ({w['trades']})" for w in e["per_window"]) + " |")
    if conc:
        L += ["", f"Positions wanted at once if every signal were taken, {CONCURRENCY_SPAN[0]} to {CONCURRENCY_SPAN[1]} (all 10 pairs active):", "", "| cell | mean open | share of time with 1 or more | with 2 or more | maximum |", "|---|---|---|---|---|"]
        for cell, c in conc.items():
            L.append(f"| {H.cell_name(cell)} | {c['mean_open']:.2f} | {c['share_time_ge1'] * 100:.0f}% | {c['share_time_ge2'] * 100:.0f}% | {c['max_open']} |")
    return "\n".join(L + [""] + note) + "\n"


class SealedError(Exception):
    pass


def ledger_path(root: Path) -> Path:
    return Path(root) / "sealed_looks.ema_universe.json"


def read_looks(root: Path, dh: str) -> list:
    p = ledger_path(root)
    if not p.exists():
        return []
    try:
        return list(json.loads(p.read_text()).get(dh, []))
    except (ValueError, AttributeError):
        raise SealedError("the one-look ledger beside the data is unreadable; refusing to open the confirmation windows")


def record_look(root: Path, dh: str, entry: dict) -> int:
    p = ledger_path(root)
    content = json.loads(p.read_text()) if p.exists() else {}
    content.setdefault(dh, []).append(entry)
    p.write_text(json.dumps(content, indent=1))
    return len(content[dh])


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def run_dev(root: Path, out_dir: Path, *, expect_plan_hash: str | None = None) -> dict:
    ph = plan_hash()
    if expect_plan_hash and expect_plan_hash != ph:
        raise SealedError(f"plan hash {ph} does not match the registered {expect_plan_hash}")
    qual = qualifying(root)
    if tuple(qual) != CATALOG:
        raise SealedError(f"the pairs that clear the registered liquidity rule ({', '.join(qual)}) are not the registered list")
    dh = data_hash(root)
    sims, firsts = simulate(root)
    dev_end = E.TEST_STARTS[CONFIRM_WINDOWS[0]]  # nothing from the confirmation windows is evaluated, counted or printed below
    rows, bnb_rows, tpy, conc, supp = {}, {}, {}, {}, {}
    c0, c1 = _ms(CONCURRENCY_SPAN[0]), _ms(CONCURRENCY_SPAN[1])
    assert c1 <= dev_end
    for cell, trades in sims.items():
        dev = [t for t in trades if t.entry_ts < dev_end]
        args = (DEV_WINDOWS, RULE["dev_min_trades"], RULE["dev_min_positive_windows"], RULE["dev_min_positive_pairs"])
        rows[cell] = evaluate(dev, *args)
        bnb_rows[cell] = evaluate(with_bnb(dev), *args)
        tpy[cell] = trades_per_year(dev, firsts, cell[1], dev_end)
        conc[cell] = concurrency(dev, c0, c1)
        supp[cell] = supplementary(dev)
    passing = [H.cell_name(c) for c, e in rows.items() if dev_pass(e)]
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    note = ["", f"Cells that pass development (P1 at least {RULE['dev_min_trades']} trades; P2 mean above zero and the {int(LEVEL * 100)}% interval of the total above zero; P3 positive in at least {RULE['dev_min_positive_windows']} of the 6 windows; "
            f"P4 at least {RULE['dev_min_positive_pairs']} pairs positive and the total still above zero without the best pair): {', '.join(passing) if passing else 'NONE'}.",
            "Trades per year are counted over 2020-04 (or the pair's first candle) to 2025-03, before the confirmation windows.", "",
            "Supplementary, not part of any rule: the history before the first window (2020-04 to 2022-03), pooled:", "", "| cell | trades | mean net R | total net R [interval] |", "|---|---|---|---|"]
    for cell, s in supp.items():
        note.append(f"| {H.cell_name(cell)} | {s['trades']} | {_fmt(s['mean_net_r'])} | {_fmt(s['total_net_r'], '+.1f')} {H._ci(s['total_block_ci'])} |")
    if not passing:
        note += ["", "Nothing passes development. The confirmation windows stay unread and no look is recorded."]
    liq = liquidity_table(root)
    note += ["", "Liquidity rule (registered): at least 3 years of 1h candles and a median daily quote volume of at least USD 50 million over the last 365 days.", "", "| pair | years of candles | median daily volume (USD m) |", "|---|---|---|"]
    note += [f"| {s} | {liq[s]['years']:.2f} | {liq[s]['median_daily_quote_volume'] / 1e6:.0f} |" for s in CATALOG]
    (out_dir / "dev_results.json").write_text(json.dumps({"plan_hash": ph, "data_hash": dh, "created": _stamp(), "rows": {H.cell_name(c): e for c, e in rows.items()}, "bnb_rows": {H.cell_name(c): e for c, e in bnb_rows.items()},
                                                          "tpy": {H.cell_name(c): t for c, t in tpy.items()}, "concurrency": {H.cell_name(c): v for c, v in conc.items()}, "supplementary": {H.cell_name(c): v for c, v in supp.items()},
                                                          "passing": passing, "liquidity": liq}, indent=1, default=str))
    (out_dir / "selected.json").write_text(json.dumps({"plan_hash": ph, "data_hash": dh, "passing": passing}, indent=1))
    (out_dir / "dev_report.md").write_text(report("EMA 4h and 1d on the catalog: development windows (2022-04 to 2025-03)", rows, bnb_rows, tpy, conc, note))
    return {"rows": rows, "bnb_rows": bnb_rows, "tpy": tpy, "concurrency": conc, "passing": passing}


def run_confirm(root: Path, out_dir: Path, *, reason: str, confirm: bool, expect_plan_hash: str | None = None, second_look: bool = False) -> dict:
    out_dir = Path(out_dir)
    sel = out_dir / "selected.json"
    if not sel.exists():
        raise SealedError("run the development first: there is no selected.json")
    if not confirm:
        raise SealedError("opening the confirmation windows needs an explicit --confirm")
    if not (reason or "").strip():
        raise SealedError("opening the confirmation windows needs a reason")
    chosen = json.loads(sel.read_text())
    ph, dh = plan_hash(), data_hash(root)
    if expect_plan_hash and expect_plan_hash != ph:
        raise SealedError(f"plan hash {ph} does not match the registered {expect_plan_hash}")
    if chosen["plan_hash"] != ph:
        raise SealedError("plan_hash differs from the development run: a pinned file or the plan changed")
    if chosen["data_hash"] != dh:
        raise SealedError("data_hash differs from the development run: the candle or funding files changed")
    if not chosen["passing"]:
        (out_dir / "final_report.md").write_text("# EMA 4h and 1d on the catalog: confirmation windows\n\nNo cell passed development, so the confirmation windows were NOT read (no look was recorded).\n")
        return {"opened": False, "rows": {}}
    looks = read_looks(root, dh)
    contaminated = bool(looks)
    if looks and not second_look:
        raise SealedError("the confirmation windows were already read once; a second look needs --second-look and is labelled contaminated")
    record_look(root, dh, {"ts": _stamp(), "reason": reason.strip(), "plan_hash": ph, "second_look": contaminated})  # BEFORE anything is read
    cells = [c for c in CELLS if H.cell_name(c) in chosen["passing"]]
    sims, firsts = simulate(root, cells)
    args = (CONFIRM_WINDOWS, RULE["confirm_min_trades"], RULE["confirm_min_positive_windows"], 0)
    rows = {c: evaluate(sims[c], *args) for c in cells}
    ok = {H.cell_name(c): bool(e["P1"] and e["mean_net_r"] is not None and e["mean_net_r"] > 0 and e["total_net_r"] > 0 and e["P3"]) for c, e in rows.items()}
    (out_dir / "confirm_results.json").write_text(json.dumps({"plan_hash": ph, "data_hash": dh, "contaminated": contaminated, "rows": {H.cell_name(c): e for c, e in rows.items()}, "confirmed": ok}, indent=1, default=str))
    lines = ["# EMA 4h and 1d on the catalog: confirmation windows (read once)", "", f"Plan hash `{ph}`. Rule: at least {RULE['confirm_min_trades']} trades, mean net R above zero, total above zero, positive in at least {RULE['confirm_min_positive_windows']} of the 3 windows."]
    if contaminated:
        lines.append("THIS IS A SECOND LOOK: it is not unseen evidence.")
    lines += ["", "| cell | trades | mean net R | total net R [interval] | windows > 0 | confirmed |", "|---|---|---|---|---|---|"]
    for c, e in rows.items():
        lines.append(f"| {H.cell_name(c)} | {e['trades']} | {_fmt(e['mean_net_r'])} | {_fmt(e['total_net_r'], '+.1f')} {H._ci(e['total_block_ci'])} | {e['positive_windows']} | {'YES' if ok[H.cell_name(c)] else 'no'} |")
    (out_dir / "final_report.md").write_text("\n".join(lines) + "\n")
    return {"opened": True, "rows": rows, "confirmed": ok, "contaminated": contaminated}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mba_bot.ema_universe")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan")
    for name in ("dev", "confirm"):
        sp = sub.add_parser(name)
        sp.add_argument("--root", required=True, type=Path)
        sp.add_argument("--out", required=True, type=Path)
        sp.add_argument("--expect-plan-hash")
        if name == "confirm":
            sp.add_argument("--confirm", action="store_true")
            sp.add_argument("--reason", default="")
            sp.add_argument("--second-look", action="store_true")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "plan":
            print(plan_hash())
            return 0
        if args.cmd == "dev":
            res = run_dev(args.root, args.out, expect_plan_hash=args.expect_plan_hash)
            print(f"development done; passing cells: {res['passing'] or 'none'}; report in {args.out}/dev_report.md")
            return 0
        res = run_confirm(args.root, args.out, reason=args.reason, confirm=args.confirm, expect_plan_hash=args.expect_plan_hash, second_look=args.second_look)
        print("confirm done; " + ("windows read" if res["opened"] else "windows NOT read (nothing passed development)"))
        return 0
    except SealedError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"ema universe failed ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
