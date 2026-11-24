"""Where does the loss come from, costs or the signals? (docs/research/cost_split_results.md). Research only: no network, no key, no order, no live effect.

It re-uses the SAME trades as PR #101 (Donchian and RSI on the 57-month and 10-month windows) and PR #102 (the nine walk-forward test windows, unfiltered), plus the EMA strategy on the PR #101 windows, and takes each trade's
net R apart: net = gross - taker fees - slippage - funding, in R (R = the distance from the entry to the initial stop). Nothing is tuned and no trade is added or removed. The identity is checked trade by trade.

    gross R     = (raw exit - raw entry) / R, long;  the mirror for a short   (before any cost)
    fee R       = taker fee x (slipped entry + slipped exit) / R
    slippage R  = slippage per side x (raw entry + raw exit) / R
    funding R   = - (funding R of the trade): positive when funding was paid
    net R       = gross - fee - slippage - funding"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import entry_study as E
from . import profit_backtest as P
from . import profit_backtest_dr as D
from . import shortframe_backtest as SF
from .research import bootstrap

PROTOCOL = "cost split v1: gross, fees, slippage, funding and exit reasons of the trades of PRs #101 and #102"
PAIRS = P.PAIRS
STRATEGIES = ("ema", "donchian", "rsi")
FEE = P.FEE_BPS / 1e4
SLIP = P.SLIP_PER_SIDE_BPS / 1e4
BOOT = {"reps": 2000, "level": 0.95, "seed": 20260303}
WINDOWS = {"A: 57 months (2020-04 to 2024-12)": D.TUNE, "B: 10 months (2025-01 to 2025-10)": D.CONFIRM, "C: the nine test windows of #102 (2022-04 to 2026-09)": None}


# ================================================================================ plan
_PINNED = ("cost_split.py", "entry_study.py", "profit_backtest_dr.py", "profit_backtest.py", "shortframe_backtest.py", "donchian.py", "rsi_mr.py", "indicators.py", "strategy.py", "reporting.py", "research/bootstrap.py",
           "research/archive.py", "research/funding_archive.py")


def source_pins() -> dict:
    here = Path(__file__).parent
    return {n: hashlib.sha256((here / n).read_bytes()).hexdigest() for n in _PINNED}


def plan_blob() -> dict:
    return {"protocol": PROTOCOL, "pairs": PAIRS, "windows": {k: v for k, v in WINDOWS.items()}, "fee_bps": P.FEE_BPS, "slippage_bps_per_side": P.SLIP_PER_SIDE_BPS, "bootstrap": BOOT, "test_starts": E.TEST_STARTS, "test_len": E.TEST_LEN,
            "sources_sha256": source_pins(), "rules": "same trades as #101 and #102; descriptive; nothing tuned"}


def plan_hash() -> str:
    return hashlib.sha256(json.dumps(plan_blob(), sort_keys=True, default=str).encode()).hexdigest()[:16]


# ================================================================================ the split
def split(t) -> dict:
    """One trade's net R taken apart. Single-leg trades only (every trade of these studies has one leg)."""
    side_sign = 1.0 if t.side == "long" else -1.0
    raw_exit = t.exit_avg
    ex = raw_exit * (1 - SLIP) if t.side == "long" else raw_exit * (1 + SLIP)
    fee_r = FEE * (t.entry + ex) / t.dist
    slip_r = SLIP * (t.raw_entry + raw_exit) / t.dist
    fund_r = -t.funding_r
    gross = side_sign * (raw_exit - t.raw_entry) / t.dist
    return {"gross": gross, "fee": fee_r, "slip": slip_r, "funding": fund_r, "net": t.net_r, "stop_pct": t.dist / t.entry * 100.0, "pair": t.pair, "reason": t.reason, "check": gross - fee_r - slip_r - fund_r - t.net_r}


def summarize(trades: list) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0}
    parts = [split(t) for t in trades]
    avg = {k: sum(p[k] for p in parts) / n for k in ("gross", "fee", "slip", "funding", "net", "stop_pct")}
    cost = avg["fee"] + avg["slip"] + avg["funding"]
    loss = -avg["net"]
    reasons: dict = {}
    for p in parts:
        reasons[p["reason"]] = reasons.get(p["reason"], 0) + 1
    per_pair = {}
    for pair in PAIRS:
        pp = [p for p in parts if p["pair"] == pair]
        if pp:
            per_pair[pair] = {"trades": len(pp), **{k: sum(p[k] for p in pp) / len(pp) for k in ("gross", "fee", "slip", "funding", "net", "stop_pct")}}
    gci = bootstrap.mean_ci([p["gross"] for p in parts], **BOOT)
    return {"trades": n, **avg, "cost": cost, "loss": loss, "gross_ci": gci, "cost_share_of_loss": (cost / loss) if loss > 0 else None, "signal_share_of_loss": (-avg["gross"] / loss) if loss > 0 else None,
            "reasons": {k: v for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])}, "per_pair": per_pair, "max_identity_error": max(abs(p["check"]) for p in parts), "stds": _std([p["net"] for p in parts])}


def _std(v):
    m = sum(v) / len(v)
    return (sum((x - m) ** 2 for x in v) / max(1, len(v) - 1)) ** 0.5


# ================================================================================ the trades
def _load(root: Path):
    candles = {pair: sorted(SF._store(Path(root), pair, "1h"), key=lambda c: c.timestamp) for pair in PAIRS}
    return candles, P.load_funding(root)


def collect(root: Path) -> dict:
    candles, funding = _load(root)
    feats = {pair: E.build_feat(candles[pair]) for pair in PAIRS}
    out: dict = {}
    for strat in STRATEGIES:
        per: dict = {}
        for label, win in WINDOWS.items():
            if win is None:
                trades = []
                for pair in PAIRS:
                    trades += E.SIM[strat](pair, feats[pair], None, E.RUN_START, E.RUN_END, funding[pair])
                per[label] = [t for t in trades if E.window_of(t.entry_ts) is not None]
            else:
                trades = []
                for pair in PAIRS:
                    trades += E.SIM[strat](pair, feats[pair], None, win[0], win[1], funding[pair])
                per[label] = trades
        out[strat] = per
    return out


def run_all(root: Path) -> dict:
    trades = collect(root)
    return {s: {w: summarize(tr) for w, tr in per.items()} for s, per in trades.items()}, trades


# ================================================================================ report
def _f(x, spec="+.3f", none="n/a") -> str:
    return none if x is None else format(x, spec)


def report(res: dict) -> str:
    lines = ["# Cost split: where the loss comes from", "", f"Plan hash `{plan_hash()}`. The same trades as PRs #101 and #102 (1h, BTC, ETH, BNB, XRP pooled), each trade's net R taken apart in R (R = distance from the entry to the initial stop): gross (before any cost) "
             f"minus taker fees ({P.FEE_BPS:g} bps per side), slippage ({P.SLIP_PER_SIDE_BPS:g} bps per side, {P.SLIP_ROUND_TRIP_BPS:g} round trip) and funding. The identity net = gross - fee - slippage - funding holds trade by trade (largest error shown).", "",
             "| strategy | window | trades | gross R | fees R | slippage R | funding R | net R | costs as share of the loss | signal as share of the loss | largest identity error |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for strat, per in res.items():
        for w, s in per.items():
            if not s["trades"]:
                continue
            lines.append(f"| {strat} | {w} | {s['trades']} | {_f(s['gross'])} [{_f(s['gross_ci'][1])}, {_f(s['gross_ci'][2])}] | {_f(-s['fee'])} | {_f(-s['slip'])} | {_f(-s['funding'])} | {_f(s['net'])} | "
                         f"{_f(None if s['cost_share_of_loss'] is None else s['cost_share_of_loss'] * 100, '.0f')}% | {_f(None if s['signal_share_of_loss'] is None else s['signal_share_of_loss'] * 100, '.0f')}% | {s['max_identity_error']:.1e} |")
    lines += ["", "Gross, fees, slippage and funding are shown with the sign they have in net R (a cost is negative). 'Costs as share of the loss' is (fees + slippage + funding) / (-net) and 'signal as share' is (-gross) / (-net): when gross is positive the signal share is negative and the costs are more than the whole loss.", ""]
    lines += ["## Exit reasons (share of trades)", "", "| strategy | window | " + " | ".join(["reasons (count, share)"]) + " |", "|---|---|---|"]
    for strat, per in res.items():
        for w, s in per.items():
            if s["trades"]:
                lines.append(f"| {strat} | {w} | " + ", ".join(f"{k} {v} ({v / s['trades'] * 100:.0f}%)" for k, v in s["reasons"].items()) + " |")
    lines += ["", "## Stop distance and cost per pair (window A, the 57 months)", "", "| strategy | pair | trades | stop distance (% of price) | gross R | fees R | slippage R | funding R | net R |", "|---|---|---|---|---|---|---|---|---|"]
    wA = next(iter(WINDOWS))
    for strat, per in res.items():
        for pair, v in per[wA].get("per_pair", {}).items():
            lines.append(f"| {strat} | {pair} | {v['trades']} | {v['stop_pct']:.2f}% | {_f(v['gross'])} | {_f(-v['fee'])} | {_f(-v['slip'])} | {_f(-v['funding'])} | {_f(v['net'])} |")
    return "\n".join(lines) + "\n"


def run(root: Path, out_dir: Path, *, expect_plan_hash: str | None = None):
    ph = plan_hash()
    if expect_plan_hash and expect_plan_hash != ph:
        raise ValueError(f"plan hash {ph} does not match the registered {expect_plan_hash}")
    res, trades = run_all(root)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "cost_split.json").write_text(json.dumps({"plan_hash": ph, "results": res}, indent=1, default=lambda o: list(o) if isinstance(o, tuple) else str(o)))
    (out_dir / "cost_split.md").write_text(report(res))
    return res, trades


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mba_bot.cost_split")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan")
    r = sub.add_parser("run")
    r.add_argument("--root", required=True, type=Path)
    r.add_argument("--out", required=True, type=Path)
    r.add_argument("--expect-plan-hash")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "plan":
            print(plan_hash())
            return 0
        run(args.root, args.out, expect_plan_hash=args.expect_plan_hash)
        print(f"done; report in {args.out}/cost_split.md")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"cost split failed ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
