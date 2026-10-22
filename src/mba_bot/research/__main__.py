"""Command line for the research tooling.   python -m mba_bot.research <command> --help

  download   fetch public Binance USD-M Futures history into a local store (read-only, no credentials)
  quality    check a store: gaps, duplicates, impossible candles, coverage
  baseline   backtest the current strategy on a store and write the report
  backtest   one quick run (single pair or Auto Select) printed to the terminal
  live       analyse the bot's own recorded trades and decisions (opens the database read-only)
  phase1     the pre-registered Phase 1 experiments (plan | run | lockbox)
  registry   show the experiment ledger
  synthetic  write a SYNTHETIC store for trying the tooling without market access

None of these commands read .env, load credentials, place orders or modify the trading database.
"""
from __future__ import annotations

import argparse
import json
import os
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
    d.add_argument("--hourly-only", action="store_true", help="skip 5-minute bars (production replay then needs them later)")
    d.add_argument("--no-funding", action="store_true")
    d.add_argument("--no-premium", action="store_true")

    q = sub.add_parser("quality", help="check a store")
    q.add_argument("--root", default="research_data")
    q.add_argument("--min-years", type=float, default=3.0)
    q.add_argument("--pairs")

    b = sub.add_parser("baseline", help="baseline report for the current strategy")
    b.add_argument("--root", default="research_data")
    b.add_argument("--out", default="research_out")
    b.add_argument("--capital", type=float, default=1000.0, help="allocated capital used for sizing")
    b.add_argument("--risk-pct", type=float, default=1.0)
    b.add_argument("--leverage", type=float, default=5.0, help="the account's symbol leverage setting")
    b.add_argument("--fee-bps", type=float, default=5.0)
    b.add_argument("--workers", type=int, default=1, help="parallel processes (default 1; each needs roughly 0.3 GB, so be modest on a shared server)")
    b.add_argument("--quick", action="store_true", help="1.0x costs only, skip Auto Select")
    b.add_argument("--min-years", type=float, default=3.0)
    b.add_argument("--pairs")

    r = sub.add_parser("backtest", help="one quick run")
    r.add_argument("--root", default="research_data")
    r.add_argument("--pair", required=True, help="BTC/USDT, or a comma list with --auto")
    r.add_argument("--mode", choices=["replay", "closed"], default="replay")
    r.add_argument("--auto", action="store_true")
    r.add_argument("--capital", type=float, default=1000.0)

    lv = sub.add_parser("live", help="analyse recorded trades (read-only)")
    lv.add_argument("--database", default=None, help="path to the SQLite file (default: the path in DATABASE_URL)")
    lv.add_argument("--fee-bps", type=float, default=5.0)
    lv.add_argument("--json", action="store_true")

    p1 = sub.add_parser("phase1", help="pre-registered Phase 1 experiments")
    p1.add_argument("action", choices=["plan", "run", "lockbox"])
    p1.add_argument("--root", default="research_data")
    p1.add_argument("--out", default="research_out/phase1")
    p1.add_argument("--baseline-json", default="research_out/baseline.json", help="the Phase 0 baseline.json, to prove this engine reproduces it")
    p1.add_argument("--capital", type=float, default=1000.0)
    p1.add_argument("--fee-bps", type=float, default=5.0)
    p1.add_argument("--expect-plan-hash", default=None, help="refuse to run if the plan differs from the registered one")
    p1.add_argument("--expect-data-fingerprint", default=None, help="refuse to run on a dataset other than the one the baseline used")
    p1.add_argument("--skip-repro", action="store_true")
    p1.add_argument("--only", default=None, help="comma separated candidate ids (test runs; marks the run PARTIAL)")
    p1.add_argument("--candidate", default=None, help="lockbox: the single candidate to score")
    p1.add_argument("--confirm", action="store_true", help="lockbox: required; opens the sealed final months, once")
    p1.add_argument("--reason", default="", help="lockbox: why it is being opened")

    rg = sub.add_parser("registry", help="show the experiment ledger")
    rg.add_argument("--path", default="research_out/trials.jsonl")

    s = sub.add_parser("synthetic", help="write a SYNTHETIC store")
    s.add_argument("--root", default="research_synthetic")
    s.add_argument("--days", type=int, default=400)
    s.add_argument("--pairs")
    s.add_argument("--seed", type=int, default=7)

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

    if args.cmd == "quality":
        from . import data as D, quality

        pairs = _pairs(args.pairs, D.DEFAULT_PAIRS)
        root = Path(args.root)
        has_5m = any(D.kline_path(root, "5m", D.symbol_for(p)).exists() for p in pairs)
        report = quality.check_store(root, pairs=list(pairs), intervals=("1h", "5m") if has_5m else ("1h",), min_years=args.min_years)
        print(quality.render(report))
        return 1 if report["summary"]["fail"] else 0

    if args.cmd == "baseline":
        from . import reporting as R
        from .engine import AccountParams, CostModel

        opts = R.BaselineOptions(
            pairs=_pairs(args.pairs, R.BaselineOptions().pairs),
            account=AccountParams(allocated_capital=args.capital, risk_pct=args.risk_pct, leverage=args.leverage),
            costs=CostModel(taker_fee_bps=args.fee_bps),
            cost_multipliers=(1.0,) if args.quick else (1.0, 1.5, 2.0), run_auto=not args.quick, workers=max(1, args.workers), min_years=args.min_years,
        )
        report = R.build_baseline(Path(args.root), Path(args.out), opts, progress=lambda m: print(m, flush=True))
        print(f"report: {Path(args.out) / 'baseline.md'}")
        print(f"data fingerprint: {report['data_hash']}   quality: {report['quality']}")
        return 0

    if args.cmd == "backtest":
        from . import data as D, reporting as R
        from .engine import MODE_CLOSED, MODE_REPLAY, AccountParams, BacktestConfig, run_backtest
        from .metrics import summarize

        root = Path(args.root)
        syms = [p.strip() for p in args.pair.split(",")]
        mode = MODE_REPLAY if args.mode == "replay" else MODE_CLOSED
        pairs = {D.symbol_for(p): R.load_pair_data(root, p, with_5m=(mode == MODE_REPLAY) or D.kline_path(root, "5m", D.symbol_for(p)).exists()) for p in syms}
        res = run_backtest(pairs, BacktestConfig(mode=mode, auto=args.auto, account=AccountParams(allocated_capital=args.capital)))
        s = summarize(res)
        for k in ("n_trades", "win_rate", "expectancy_r", "expectancy_r_gross", "profit_factor", "total_return_pct", "max_drawdown_pct", "sharpe", "avg_cost_r", "longest_losing_streak"):
            print(f"{k:>24}: {s.get(k)}")
        return 0

    if args.cmd == "live":
        from . import live_report as LR

        db = args.database
        if db is None:
            url = os.environ.get("DATABASE_URL", "")
            if not url.startswith("sqlite:///"):
                print("pass --database /path/to/file.db (DATABASE_URL is not a SQLite URL)", file=sys.stderr)
                return 2
            db = url.removeprefix("sqlite:///")
        rep = LR.live_report(db, fee_bps=args.fee_bps)
        print(json.dumps(rep, indent=1, default=str) if args.json else LR.render(rep))
        return 0

    if args.cmd == "phase1":
        from . import phase1 as P1, phase1_report as P1R
        from .engine import AccountParams, CostModel

        opts = P1.Phase1Options(account=AccountParams(allocated_capital=args.capital), costs=CostModel(taker_fee_bps=args.fee_bps))
        plan = P1.PLAN
        if args.only:
            wanted = {x.strip() for x in args.only.split(",")}
            plan = tuple(c for c in P1.PLAN if c.id in wanted)
        if args.action == "plan":
            print(f"plan hash {P1.plan_hash(opts)}  ({len(P1.PLAN)} candidates, {sum(max(1, len(c.seeds)) for c in P1.PLAN)} trials)")
            for c in P1.PLAN:
                print(f"  {c.id:<10} {c.hypothesis} {'diag ' if c.diagnostic else '     '}{c.scope:<5} {c.label}")
            return 0
        if args.expect_plan_hash and args.expect_plan_hash != P1.plan_hash(opts):
            print(f"plan hash {P1.plan_hash(opts)} does not match the registered {args.expect_plan_hash}; refusing to run", file=sys.stderr)
            return 3
        if args.action == "run":
            import json as _json

            baseline = Path(args.baseline_json) if args.baseline_json and Path(args.baseline_json).exists() else None
            res = P1.run_phase1(Path(args.root), Path(args.out), opts, plan=plan, baseline_json=baseline, skip_repro=args.skip_repro, expect_fingerprint=args.expect_data_fingerprint, progress=lambda m: print(m, flush=True))
            out = Path(args.out)
            (out / "phase1_results.json").write_text(_json.dumps(res, indent=1, default=str))
            (out / "phase1_report.md").write_text(P1R.render(res))
            print(f"report: {out / 'phase1_report.md'}")
            print(f"plan hash {res['meta']['plan_hash']} | survivors: {', '.join(res['survivors']) or 'none'}")
            return 0
        if args.action == "lockbox":
            import json as _json

            if not args.candidate:
                print("--candidate is required", file=sys.stderr)
                return 2
            res = P1.run_lockbox(Path(args.root), Path(args.out), args.candidate, opts, confirm=args.confirm, reason=args.reason, progress=lambda m: print(m, flush=True))
            print(_json.dumps(res, indent=1, default=str))
            return 0

    if args.cmd == "registry":
        from .validation import TrialRegistry

        for rec in TrialRegistry(Path(args.path)).records():
            print(json.dumps({k: rec.get(k) for k in ("ts", "kind", "family", "config_hash", "window", "code_version", "note")}))
        return 0

    if args.cmd == "synthetic":
        from . import data as D, synthetic

        pairs = _pairs(args.pairs, D.DEFAULT_PAIRS)
        synthetic.write_store(Path(args.root), pairs=list(pairs), days=args.days, seed=args.seed)
        print(f"SYNTHETIC store written to {args.root} (generated prices, not market data)")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
