"""Cost sensitivity (docs/research/cost_sensitivity_results.md). Research only: no network, no key, no order, no live effect.

The SAME trades as the cost split (PRs #101 and #102 windows), re-priced under other fee and fill assumptions. Entries and exits keep their prices; only the costs change. R (the distance to the initial stop) is unchanged.
    net R = (move - fee_entry x entry price - fee_exit x exit price) / R + funding R,  with the entry and exit prices after the slippage of each side.
Fee schedule (USD-M Futures, per side): regular taker 5.0 bps and maker 2.0 bps; VIP 1: 4.0 / 1.6; VIP 2: 3.5 / 1.4; VIP 3: 3.2 / 1.2; paying fees in BNB takes 10% off. ALL UNVERIFIED (from the published schedule as I know it, not read
from the exchange); the VIP tiers need 30-day volume far above a pilot account's.
Maker entries: a limit order at the next open's price, filled at that price with NO entry slippage and the maker fee; exits stay market orders (taker fee, 5.5 bps slippage), stops included. A limit entry is not always filled, so a missed-fill
rate m is assumed and varied (0%, 20%, 40%), under two readings of WHICH trades are missed: 'random' (the missed trades are a random subset: the average of the filled trades equals the average of all) and 'adverse' (the missed trades are the
m% with the best gross R: the price ran away without filling the order; the worst case). The truth is between them and depends on the market."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from . import cost_split as CS
from . import entry_study as E
from . import profit_backtest as P
from .research import bootstrap

PROTOCOL = "cost sensitivity v1: the trades of the cost split re-priced under fee tiers, BNB, maker entries and missed fills"
SLIP_SIDE_BPS = P.SLIP_PER_SIDE_BPS
FEES = {"regular": (5.0, 2.0), "vip1": (4.0, 1.6), "vip2": (3.5, 1.4), "vip3": (3.2, 1.2)}  # (taker, maker) bps per side
BNB_DISCOUNT = 0.10
MISS_RATES = (0.0, 0.2, 0.4)
BOOT = {"reps": 2000, "level": 0.95, "seed": 20260404}
SCENARIOS = (
    ("S0 baseline: taker both sides, regular tier", {"tier": "regular", "bnb": False, "maker_entry": False}),
    ("S1 pay fees in BNB (-10%)", {"tier": "regular", "bnb": True, "maker_entry": False}),
    ("S2 VIP 1 taker", {"tier": "vip1", "bnb": False, "maker_entry": False}),
    ("S3 VIP 2 taker", {"tier": "vip2", "bnb": False, "maker_entry": False}),
    ("S4 VIP 3 taker", {"tier": "vip3", "bnb": False, "maker_entry": False}),
    ("S5 VIP 3 and BNB", {"tier": "vip3", "bnb": True, "maker_entry": False}),
    ("S6 maker entry, regular tier", {"tier": "regular", "bnb": False, "maker_entry": True}),
    ("S7 maker entry and BNB", {"tier": "regular", "bnb": True, "maker_entry": True}),
    ("S8 maker entry, VIP 3 and BNB", {"tier": "vip3", "bnb": True, "maker_entry": True}),
)


_PINNED = ("cost_sensitivity.py", "cost_split.py", "entry_study.py", "profit_backtest_dr.py", "profit_backtest.py", "shortframe_backtest.py", "donchian.py", "rsi_mr.py", "indicators.py", "strategy.py", "reporting.py",
           "research/bootstrap.py", "research/archive.py", "research/funding_archive.py")


def source_pins() -> dict:
    here = Path(__file__).parent
    return {n: hashlib.sha256((here / n).read_bytes()).hexdigest() for n in _PINNED}


def plan_blob() -> dict:
    return {"protocol": PROTOCOL, "fees": FEES, "bnb_discount": BNB_DISCOUNT, "slippage_per_side_bps": SLIP_SIDE_BPS, "miss_rates": MISS_RATES, "scenarios": [(n, c) for n, c in SCENARIOS], "bootstrap": BOOT,
            "windows": {k: v for k, v in CS.WINDOWS.items()}, "test_starts": E.TEST_STARTS, "test_len": E.TEST_LEN, "sources_sha256": source_pins()}


def plan_hash() -> str:
    return hashlib.sha256(json.dumps(plan_blob(), sort_keys=True, default=str).encode()).hexdigest()[:16]


def fee_bps(cfg: dict) -> tuple[float, float]:
    taker, maker = FEES[cfg["tier"]]
    k = (1 - BNB_DISCOUNT) if cfg["bnb"] else 1.0
    return taker * k, maker * k


def recost(t, cfg: dict) -> float:
    """The net R of trade `t` under `cfg`. Entry: slipped taker fill, or a maker fill at the raw entry price. Exit: always a market order (taker fee, slippage)."""
    taker, maker = fee_bps(cfg)
    slip = SLIP_SIDE_BPS / 1e4
    raw_exit = t.exit_avg
    long_side = t.side == "long"
    if cfg["maker_entry"]:
        entry, f_in = t.raw_entry, maker / 1e4
    else:
        entry, f_in = (t.raw_entry * (1 + slip) if long_side else t.raw_entry * (1 - slip)), taker / 1e4
    ex = raw_exit * (1 - slip) if long_side else raw_exit * (1 + slip)
    move = (ex - entry) if long_side else (entry - ex)
    return (move - f_in * entry - (taker / 1e4) * ex) / t.dist + t.funding_r


def gross(t) -> float:
    s = 1.0 if t.side == "long" else -1.0
    return s * (t.exit_avg - t.raw_entry) / t.dist


def evaluate(trades: list, cfg: dict, miss: float, how: str) -> dict:
    """Average net R of the FILLED trades under `cfg`. With maker entries, `miss` of the trades are not filled: 'random' leaves the average of the filled trades equal to that of all trades; 'adverse' drops the `miss` share with the
    highest gross R. Without maker entries every trade fills."""
    if not trades:
        return {"trades": 0}
    nets = [(recost(t, cfg), gross(t)) for t in trades]
    if cfg["maker_entry"] and miss > 0 and how == "adverse":
        keep = sorted(nets, key=lambda x: x[1])[: int(round(len(nets) * (1 - miss)))]
    else:
        keep = nets
    values = [n for n, _ in keep]
    ci = bootstrap.mean_ci(values, **BOOT)
    n_kept = len(values) if how == "adverse" else int(round(len(values) * (1 - (miss if cfg["maker_entry"] else 0.0))))
    return {"trades": n_kept, "mean": sum(values) / len(values) if values else None, "ci": ci}


def run_all(root: Path) -> dict:
    trades = CS.collect(root)
    out: dict = {}
    for strat, per in trades.items():
        out[strat] = {}
        for w, tr in per.items():
            rows = {}
            for name, cfg in SCENARIOS:
                if cfg["maker_entry"]:
                    for m in MISS_RATES:
                        for how in (("random", "adverse") if m > 0 else ("random",)):
                            rows[f"{name} | miss {int(m * 100)}% {how}"] = evaluate(tr, cfg, m, how)
                else:
                    rows[name] = evaluate(tr, cfg, 0.0, "random")
            rows["zero cost (gross, funding kept)"] = {"trades": len(tr), "mean": sum(gross(t) + t.funding_r for t in tr) / len(tr) if tr else None, "ci": None}
            out[strat][w] = rows
    return out


def _f(x, spec="+.3f") -> str:
    return "n/a" if x is None else format(x, spec)


def report(res: dict) -> str:
    lines = ["# Cost sensitivity", "", f"Plan hash `{plan_hash()}`. The same trades as the cost split, re-priced. Average net R per FILLED trade [95% interval over trades]; fee schedule and BNB discount UNVERIFIED (see the module). Slippage 5.5 bps per side on every market fill; "
             "maker entries have none and exits stay market orders.", ""]
    for strat, per in res.items():
        for w, rows in per.items():
            lines += [f"## {strat} - {w}", "", "| scenario | filled trades | avg net R [95% CI] |", "|---|---|---|"]
            for name, r in rows.items():
                ci = r.get("ci")
                lines.append(f"| {name} | {r['trades']} | {_f(r.get('mean'))}{'' if ci is None else f' [{ci[1]:+.3f}, {ci[2]:+.3f}]'} |")
            lines.append("")
    return "\n".join(lines) + "\n"


def run(root: Path, out_dir: Path, *, expect_plan_hash: str | None = None) -> dict:
    ph = plan_hash()
    if expect_plan_hash and expect_plan_hash != ph:
        raise ValueError(f"plan hash {ph} does not match the registered {expect_plan_hash}")
    res = run_all(root)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "cost_sensitivity.json").write_text(json.dumps({"plan_hash": ph, "results": res}, indent=1, default=lambda o: list(o) if isinstance(o, tuple) else str(o)))
    (out_dir / "cost_sensitivity.md").write_text(report(res))
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mba_bot.cost_sensitivity")
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
        print(f"done; report in {args.out}/cost_sensitivity.md")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"cost sensitivity failed ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
