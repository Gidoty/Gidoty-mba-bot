"""The one-look test of EMA 4h 20/50 on the 18 months no study has read (docs/research/ema_onelook_preregistration.md). Research only: no network, no key, no order, no live effect.

ONE hypothesis, ONE cell, ONE window, opened ONCE: the EMA crossover as shipped (fast 20, slow 50) on 4h candles, with the live bot's stop and exit rules exactly as the universe study simulated them
(`htf_study.sim_ema`, unchanged), has a positive net edge after costs on the 10 pairs of the bot's own catalog, over 2025-04-01 to 2026-10-01.

Nothing is tuned and nothing can be re-run: the look is recorded in a ledger file beside the data BEFORE a single trade of the window is generated. A second run is refused whatever happened to the first, including a crash after
the read began (the ledger then says `crashed`: the look is spent). There is no flag to reopen it. The report is written once, at the end, from one pass. The verdict is one of exactly three words: PASS, FAIL, UNINFORMATIVE."""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import ema_universe as U
from . import entry_study as E
from . import htf_study as H
from . import portfolio_study as PS
from .research import bootstrap

PROTOCOL = "ema one-look v1: EMA 4h 20/50 as shipped, 10 catalog pairs, 2025-04-01 to 2026-10-01, one look"
CELL = ("ema", "4h", "s1")
PARAMS = (20, 50)
WINDOW_START = E._ms("2025-04-01")
WINDOW_END = E._ms("2026-10-01")  # exclusive: entries 2025-04-01 00:00 UTC up to and including 2026-09-30
BLOCK_STARTS = tuple(E._ms(d) for d in ("2025-04-01", "2025-07-01", "2025-10-01", "2026-01-01", "2026-04-01", "2026-07-01"))
BLOCK_LABELS = ("2025-04 to 06", "2025-07 to 09", "2025-10 to 12", "2026-01 to 03", "2026-04 to 06", "2026-07 to 09")
BOOT_ONE = {"reps": 2000, "level": 0.95, "seed": 20261125}
RULE = {"min_trades": 300, "min_positive_blocks": 4, "min_positive_pairs": 6}  # P1 at least 300 trades (fewer = UNINFORMATIVE, not FAIL); P2 mean > 0 and the 95% interval of the total above 0; P3; P4
EXPECTED_DATA_HASH = "be345d7847ab820d"  # ema_universe.data_hash of the 10 pairs' 1h candles and funding files, registered before the run
EXPECTED_TRADES = {"from": "universe study (#107): 4h 20/50, 355.8 entries a year over 10 pairs", "expected": 534, "surprise_below": 400, "surprise_above": 700}
PASS, FAIL, UNINFORMATIVE = "PASS", "FAIL", "UNINFORMATIVE"
RISK_PCT = 1.0

_PINNED = ("ema_onelook.py", "ema_universe.py", "portfolio_study.py", "htf_study.py", "cost_sensitivity.py", "entry_study.py", "profit_backtest_dr.py", "profit_backtest.py", "shortframe_backtest.py", "indicators.py", "strategy.py",
           "reporting.py", "research/bootstrap.py", "research/archive.py", "research/funding_archive.py")


def source_pins() -> dict:
    here = Path(__file__).parent
    return {n: hashlib.sha256((here / n).read_bytes()).hexdigest() for n in _PINNED}


def plan_blob() -> dict:
    return {"protocol": PROTOCOL, "cell": CELL, "params": PARAMS, "window": (WINDOW_START, WINDOW_END), "blocks": BLOCK_STARTS, "pairs": U.CATALOG, "liquidity": U.LIQUIDITY, "rule": RULE, "bootstrap": BOOT_ONE,
            "costs": {"fee_bps": U.P.FEE_BPS, "slippage_round_trip_bps": U.P.SLIP_ROUND_TRIP_BPS}, "bnb_reading": U.BNB, "data_hash": EXPECTED_DATA_HASH, "expected_trades": EXPECTED_TRADES, "risk_pct": RISK_PCT,
            "run_end": E.RUN_END, "sources_sha256": source_pins()}


def plan_hash() -> str:
    return hashlib.sha256(json.dumps(plan_blob(), sort_keys=True, default=str).encode()).hexdigest()[:16]


# ================================================================================ the verdict
def block_of(ts: int) -> int | None:
    for k in range(len(BLOCK_STARTS)):
        end = BLOCK_STARTS[k + 1] if k + 1 < len(BLOCK_STARTS) else WINDOW_END
        if BLOCK_STARTS[k] <= ts < end:
            return k
    return None


def in_window(trades: list) -> list:
    return [t for t in trades if WINDOW_START <= t.entry_ts < WINDOW_END]


def _month(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m")


def evaluate(trades: list) -> dict:
    """The numbers and the four parts of the rule for the trades of the window. Pure; used for the standard fees and, separately, for the BNB reading."""
    sel = in_window(trades)
    n = len(sel)
    nets = [t.net_r for t in sel]
    total = sum(nets)
    mean = (total / n) if n else None
    months: dict[str, float] = {}
    for t in sel:
        months[_month(t.entry_ts)] = months.get(_month(t.entry_ts), 0.0) + t.net_r
    total_ci = bootstrap.block_sum_ci(months, **BOOT_ONE)
    mean_ci = bootstrap.mean_ci(nets, **BOOT_ONE)
    blocks = []
    for k, label in enumerate(BLOCK_LABELS):
        b = [t for t in sel if block_of(t.entry_ts) == k]
        blocks.append({"block": label, "trades": len(b), "total_net_r": sum(t.net_r for t in b), "mean_net_r": (sum(t.net_r for t in b) / len(b)) if b else None})
    pairs = {}
    for p in U.CATALOG:
        b = [t for t in sel if t.pair == p]
        pairs[p] = {"trades": len(b), "total_net_r": sum(t.net_r for t in b), "mean_net_r": (sum(t.net_r for t in b) / len(b)) if b else None}
    ordered = sorted(nets, reverse=True)
    top5 = sum(ordered[:5])
    pos_blocks = sum(1 for b in blocks if b["trades"] and b["total_net_r"] > 0)
    pos_pairs = sum(1 for v in pairs.values() if v["trades"] and v["total_net_r"] > 0)
    p1 = n >= RULE["min_trades"]
    p2 = bool(n and mean > 0 and total_ci is not None and total_ci[1] > 0)
    p3 = pos_blocks >= RULE["min_positive_blocks"]
    p4 = pos_pairs >= RULE["min_positive_pairs"]
    stops = [t.dist / t.entry * 100.0 for t in sel]
    reasons: dict[str, int] = {}
    for t in sel:
        reasons[t.reason] = reasons.get(t.reason, 0) + 1
    dd = PS.drawdowns(sel, RISK_PCT)
    return {"trades": n, "mean_net_r": mean, "total_net_r": total, "total_ci": total_ci, "mean_ci": mean_ci, "avg_gross_r": (sum(t.gross_r for t in sel) / n) if n else None,
            "avg_cost_r": (sum(t.gross_r - t.net_r for t in sel) / n) if n else None, "avg_stop_pct": (sum(stops) / n) if n else None, "win_rate": (sum(1 for x in nets if x > 0) / n) if n else None,
            "median_net_r": (statistics.median(nets) if n else None), "best_r": ordered[0] if n else None, "worst_r": ordered[-1] if n else None, "top5_sum_r": top5, "top5_share_of_total": (top5 / total) if total > 0 else None,
            "total_without_top5": total - top5, "longest_losing_streak": PS.losing_streak(sel), "max_drawdown_r": dd["max_dd_r"], "max_drawdown_equity": dd["max_dd_equity"], "lowest_equity": dd["lowest_equity"],
            "final_equity": dd["final_equity"], "blocks": blocks, "pairs": pairs, "positive_blocks": pos_blocks, "positive_pairs": pos_pairs, "exit_reasons": reasons,
            "P1": p1, "P2": p2, "P3": p3, "P4": p4, "verdict": verdict(n, p2, p3, p4)}


def verdict(n: int, p2: bool, p3: bool, p4: bool) -> str:
    """Fewer than the minimum number of trades is UNINFORMATIVE whatever else is true. Otherwise PASS only if P2, P3 and P4 all hold, else FAIL. No other outcome exists."""
    if n < RULE["min_trades"]:
        return UNINFORMATIVE
    return PASS if (p2 and p3 and p4) else FAIL


# ================================================================================ the ledger
def ledger_path(root: Path) -> Path:
    return Path(root) / "sealed_looks.ema_onelook.json"


class LookSpent(Exception):
    pass


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _read_ledger(root: Path) -> dict:
    p = ledger_path(root)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        if not isinstance(data, dict):
            raise ValueError
        return data
    except ValueError:
        raise LookSpent("the one-look ledger is unreadable; the window is treated as already opened")


def open_look(root: Path, dh: str, ph: str) -> None:
    """Refuses if ANY entry exists for this data (whatever its state), then writes the entry BEFORE anything is read. There is no way to reopen."""
    ledger = _read_ledger(root)
    if ledger.get(dh):
        raise LookSpent("the window was already opened once on this data; the look is spent and cannot be repeated")
    ledger.setdefault(dh, []).append({"ts": _stamp(), "plan_hash": ph, "state": "opened", "window": [WINDOW_START, WINDOW_END], "cell": list(CELL)})
    ledger_path(root).write_text(json.dumps(ledger, indent=1))


def close_look(root: Path, dh: str, state: str, extra: dict | None = None) -> None:
    ledger = _read_ledger(root)
    entry = ledger[dh][-1]
    entry.update({"state": state, "closed": _stamp(), **(extra or {})})
    ledger_path(root).write_text(json.dumps(ledger, indent=1))


# ================================================================================ the run
def simulate(root: Path) -> list:
    sims, _ = U.simulate(root, cells=(CELL,))
    return sims[CELL]


def run_once(root: Path, out_dir: Path, *, expect_plan_hash: str) -> dict:
    ph = plan_hash()
    if expect_plan_hash != ph:
        raise LookSpent(f"plan hash {ph} does not match the registered {expect_plan_hash}; nothing was read")
    dh = U.data_hash(root)
    if dh != EXPECTED_DATA_HASH:
        raise LookSpent(f"data hash {dh} is not the registered {EXPECTED_DATA_HASH}; nothing was read")
    if tuple(U.qualifying(root)) != U.CATALOG:
        raise LookSpent("the pairs that clear the registered liquidity rule are not the registered list; nothing was read")
    out_dir = Path(out_dir)
    open_look(root, dh, ph)  # the look is spent from this line on
    try:
        trades = simulate(root)
        res = evaluate(trades)
        sel = in_window(trades)
        res["bnb_reading"] = evaluate(U.with_bnb(sel))
        span = WINDOW_END - WINDOW_START
        res["descriptive_slots"] = {PS.slot_name(k): PS.summarize(sel, k, span_ms=span) for k in (1, 3, None)}
        res["trades_per_year"] = n_per_year(res["trades"], span)
        res["plan_hash"], res["data_hash"] = ph, dh
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "onelook_results.json").write_text(json.dumps(res, indent=1, default=str))
        (out_dir / "onelook_report.md").write_text(report(res))
    except BaseException as exc:  # the look is spent whatever happens now
        close_look(root, dh, "crashed", {"error_class": type(exc).__name__})
        raise
    close_look(root, dh, "completed", {"verdict": res["verdict"], "trades": res["trades"]})
    return res


def n_per_year(n: int, span_ms: int) -> float:
    return n / (span_ms / (365.25 * 86_400_000))


# ================================================================================ the report
def _f(x, spec="+.3f"):
    return "n/a" if x is None else format(x, spec)


def _ci(ci, spec="+.1f"):
    return "n/a" if ci is None else f"[{ci[1]:{spec}}, {ci[2]:{spec}}]"


def report(res: dict) -> str:
    yn = lambda b: "yes" if b else "no"  # noqa: E731
    r = RULE
    L = [f"# EMA 4h 20/50, one look: 2025-04-01 to 2026-10-01 (plan hash `{res['plan_hash']}`, data hash `{res['data_hash']}`)", "",
         f"## RESULT: {res['verdict']}", "",
         "| part of the pass rule (fixed before the run) | value | holds |", "|---|---|---|",
         f"| P1 at least {r['min_trades']} trades (fewer = UNINFORMATIVE, not FAIL) | {res['trades']} | {yn(res['P1'])} |",
         f"| P2 mean net R above zero AND the 95% month-block interval of the total above zero | mean {_f(res['mean_net_r'])}, total {_f(res['total_net_r'], '+.1f')} {_ci(res['total_ci'])} | {yn(res['P2'])} |",
         f"| P3 positive in at least {r['min_positive_blocks']} of the 6 three-month blocks | {res['positive_blocks']} of 6 | {yn(res['P3'])} |",
         f"| P4 positive on at least {r['min_positive_pairs']} of the 10 pairs | {res['positive_pairs']} of 10 | {yn(res['P4'])} |", "",
         f"Expected before the run: about {EXPECTED_TRADES['expected']} trades (a count below {EXPECTED_TRADES['surprise_below']} or above {EXPECTED_TRADES['surprise_above']} would be a surprise). Observed: {res['trades']}.", "",
         "## Numbers (standard fees: 5 bps taker per side, 11 bps round-trip slippage, historical funding)", "",
         f"- trades {res['trades']} ({res['trades_per_year']:.0f} a year over the 10 pairs); mean net R {_f(res['mean_net_r'])} (95% interval of the mean {_ci(res['mean_ci'], '+.3f')}); total net R {_f(res['total_net_r'], '+.1f')} {_ci(res['total_ci'])}",
         f"- average gross R {_f(res['avg_gross_r'])}; average cost R {_f(res['avg_cost_r'], '.3f')}; average stop distance {_f(res['avg_stop_pct'], '.2f')}%",
         f"- win rate {_f(res['win_rate'] and res['win_rate'] * 100, '.0f')}%; median trade {_f(res['median_net_r'], '+.2f')} R; best {_f(res['best_r'], '+.1f')} R; worst {_f(res['worst_r'], '+.1f')} R",
         f"- the 5 best trades: {_f(res['top5_sum_r'], '+.1f')} R" + (f" ({res['top5_share_of_total'] * 100:.0f}% of the total)" if res["top5_share_of_total"] is not None else " (the total is not positive, so no share)") + f"; total without them {_f(res['total_without_top5'], '+.1f')} R",
         f"- longest run of losing trades {res['longest_losing_streak']}; at 1% risk per trade: maximum drawdown {res['max_drawdown_equity'] * 100:.0f}% of equity ({_f(res['max_drawdown_r'], '.1f')} R), lowest equity {res['lowest_equity']:.2f}, final equity {res['final_equity']:.2f} (closed-trade equity, in sample)",
         f"- exits: {', '.join(f'{k} {v}' for k, v in sorted(res['exit_reasons'].items()))}", "",
         "### Three-month blocks", "", "| block | trades | total net R | mean net R |", "|---|---|---|---|"]
    L += [f"| {b['block']} | {b['trades']} | {_f(b['total_net_r'], '+.1f')} | {_f(b['mean_net_r'])} |" for b in res["blocks"]]
    L += ["", "### Pairs", "", "| pair | trades | total net R | mean net R |", "|---|---|---|---|"]
    L += [f"| {p} | {v['trades']} | {_f(v['total_net_r'], '+.1f')} | {_f(v['mean_net_r'])} |" for p, v in res["pairs"].items()]
    bn = res["bnb_reading"]
    L += ["", "## Fees paid in BNB (reported separately; the verdict above uses the standard fees)", "",
          f"mean net R {_f(bn['mean_net_r'])}, total {_f(bn['total_net_r'], '+.1f')} {_ci(bn['total_ci'])}, blocks positive {bn['positive_blocks']} of 6, pairs positive {bn['positive_pairs']} of 10 (the same rule would read: {bn['verdict']}).", "",
          "## One slot and three slots (DESCRIPTIVE ONLY: not part of the rule; first signal wins, ties by catalog order; closed-trade equity at 1% risk)", "",
          "| slots | signals | taken | total net R | mean net R | max drawdown (equity) | longest losing streak |", "|---|---|---|---|---|---|---|"]
    for name, s in res["descriptive_slots"].items():
        d = s["dd"][str(PS.PRIMARY_RISK)]
        L.append(f"| {name} | {s['signals']} | {s['taken']} | {_f(s['total_net_r'], '+.1f')} | {_f(s['mean_net_r'])} | {d['max_dd_equity'] * 100:.0f}% | {s['losing_streak']} |")
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mba_bot.ema_onelook")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--root", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--expect-plan-hash")
    a = ap.parse_args(argv)
    if a.plan:
        print(plan_hash())
        return 0
    if not (a.root and a.out and a.expect_plan_hash):
        print("refused: --root, --out and --expect-plan-hash are all required", file=sys.stderr)
        return 2
    try:
        res = run_once(a.root, a.out, expect_plan_hash=a.expect_plan_hash)
    except LookSpent as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"the run failed after the window was opened ({type(exc).__name__}): THE LOOK IS SPENT. It is recorded as crashed and will not be repeated.", file=sys.stderr)
        return 3
    print(f"RESULT: {res['verdict']}; report in {a.out}/onelook_report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
