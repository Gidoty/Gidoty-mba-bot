"""What can a pilot of N trades show? (docs/research/owner_summary.md). Research only: no network, no key, no order, no live effect.

From the trades the studies already produced: the spread of one trade's net R, the standard error of an N-trade average, and (by resampling N trades from the real trades with replacement, seeded) the chance that an N-trade pilot ends with
a positive or a clearly negative total even though the long-run average is what the history says. Also how long N trades take on one pair. Nothing here is tuned and no trade is added."""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

from . import cost_split as CS
from . import entry_study as E
from . import htf_study as H

N_PILOT = 100
REPS = 20000
SEED = 20260606


def pilot(nets: list[float], n: int = N_PILOT, reps: int = REPS, seed: int = SEED, bad: float = -10.0) -> dict:
    """Resample `n` trades with replacement `reps` times. Returns the mean and spread of one trade, the standard error of the n-trade mean, and the shares of resampled pilots whose TOTAL net R is above zero, below -10 R, and below `bad`."""
    m = len(nets)
    if m < 2:
        return {"trades": m}
    mean = sum(nets) / m
    sd = (sum((x - mean) ** 2 for x in nets) / (m - 1)) ** 0.5
    rng = random.Random(seed)
    pos = neg = 0
    for _ in range(reps):
        total = sum(nets[rng.randrange(m)] for _ in range(n))
        pos += total > 0
        neg += total < bad
    return {"trades": m, "mean": mean, "sd": sd, "se_at_n": sd / n ** 0.5, "n": n, "interval_95_of_the_mean": (mean - 1.96 * sd / n ** 0.5, mean + 1.96 * sd / n ** 0.5), "p_total_positive": pos / reps, "p_total_below_bad": neg / reps, "bad": bad}


def trades_per_pair_year(trades: list, years: float, pairs: int = 4) -> float:
    return len(trades) / years / pairs


def months_for(n: int, per_pair_year: float) -> float | None:
    return None if per_pair_year <= 0 else n / per_pair_year * 12.0


def run(root: Path) -> dict:
    one_h = CS.collect(root)
    years_c = len(E.TEST_STARTS) * (E.TEST_LEN / (365.25 * 86_400_000))
    out: dict = {}
    c_label = next(k for k in CS.WINDOWS if k.startswith("C:"))
    for strat in CS.STRATEGIES:
        tr = one_h[strat][c_label]
        out[f"{strat} 1h"] = {**pilot([t.net_r for t in tr]), "per_pair_year": trades_per_pair_year(tr, years_c)}
    sims = H.simulate_cells(root, [("ema", "4h", "s1"), ("ema", "1d", "s1"), ("donchian", "4h", "s2")])
    dev_end = E.TEST_STARTS[H.CONFIRM_WINDOWS[0]]
    years = (dev_end - E.RUN_START) / (365.25 * 86_400_000)
    for cell, tr in sims.items():
        dev = [t for t in tr if t.entry_ts < dev_end]
        out[H.cell_name(cell)] = {**pilot([t.net_r for t in dev]), "per_pair_year": trades_per_pair_year(dev, years)}
    for v in out.values():
        v["months_for_100_on_one_pair"] = months_for(N_PILOT, v["per_pair_year"])
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mba_bot.pilot_math")
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args(argv)
    try:
        res = run(args.root)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(res, indent=1, default=str))
        print(json.dumps(res, indent=1, default=str))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"pilot math failed ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
