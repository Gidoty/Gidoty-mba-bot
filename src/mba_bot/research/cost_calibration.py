"""Measured costs from the trade ledger, with a time-ordered validation. Research only.

Turns reconciled live trades into the numbers the backtester's CostModel assumes (taker fee, market and stop slippage,
funding), says how many observations each rests on, and replaces an assumption ONLY where the registered minimum is met.
Everything else stays at the assumed default and is labelled as assumed.

Validation is time-ordered: the model is fitted on the earlier 60% of trades and compared with the later 40%. If the later
part is too small to judge, that is reported as "not validated", never as "validated".
"""
from __future__ import annotations

import statistics
from dataclasses import replace

from .engine import CostModel
from .ledger import Economics, usable
from .metrics import bootstrap_mean_ci

# Registered minimums (docs/research/phase3_ledger_preregistration.md carries the same numbers; a test compares them).
MIN_OBS_COST = 30          # observations needed before a measured cost replaces an assumed one
MIN_TRADES_HOLDOUT = 20    # trades needed in the later 40% for the drift check to mean anything
FIT_FRACTION = 0.6


def _summ(values: list[float]) -> dict:
    v = [x for x in values if x is not None]
    if not v:
        return {"n": 0}
    s = sorted(v)
    out = {"n": len(v), "mean": statistics.fmean(v), "median": statistics.median(v), "p90": s[min(len(s) - 1, int(0.9 * len(s)))]}
    ci = bootstrap_mean_ci(v)
    if ci:
        out["ci_low"], out["ci_high"] = ci["low"], ci["high"]
    return out


def _leg_fee_bps(rows: list[Economics]) -> tuple[list[float], int, int]:
    taker, legs_total, legs_maker = [], 0, 0
    for r in rows:
        for bps, is_taker in ((r.entry_fee_bps, r.entry_taker), (r.exit_fee_bps, r.exit_taker)):
            if bps is None or is_taker is None:
                continue
            legs_total += 1
            if is_taker:
                taker.append(bps)
            else:
                legs_maker += 1
    return taker, legs_total, legs_maker


def _funding_bps_per_8h(rows: list[Economics]) -> dict:
    """Funding paid as basis points of notional per 8 hours held, pooled (paid positive, to match the engine)."""
    num = sum(-(r.funding_usdt or 0.0) for r in rows if r.notional and r.hold_minutes)
    den = sum(r.notional * (r.hold_minutes / 60.0 / 8.0) for r in rows if r.notional and r.hold_minutes)
    return {"n": len([r for r in rows if r.notional and r.hold_minutes]), "bps_per_8h": (num / den * 1e4) if den else None}


def measure(rows: list[Economics]) -> dict:
    live = usable(rows)  # reconciled, live (paper rows never reconcile)
    taker, legs, maker_legs = _leg_fee_bps(live)
    by_pair: dict[str, dict] = {}
    for pair in sorted({r.pair for r in live}):
        sub = [r for r in live if r.pair == pair]
        by_pair[pair] = {"entry_slip_bps": _summ([r.entry_slip_bps for r in sub]), "stop_slip_bps": _summ([r.stop_slip_bps for r in sub])}
    return {
        "n_live_reconciled": len(live),
        "taker_fee_bps": _summ(taker),
        "maker_leg_share": (maker_legs / legs) if legs else None,
        "entry_slip_bps_all": _summ([r.entry_slip_bps for r in live]),
        "stop_slip_bps_all": _summ([r.stop_slip_bps for r in live]),
        "funding": _funding_bps_per_8h(live),
        "fees_in_other_assets_trades": len([r for r in rows if r.fees_other_assets]),
        "by_pair": by_pair,
    }


def build_cost_model(m: dict, base: CostModel | None = None) -> tuple[CostModel, dict]:
    """Returns (CostModel, provenance). A field changes only if its observations reach MIN_OBS_COST."""
    base = base or CostModel()
    prov = {"taker_fee_bps": "assumed", "market_slip_bps": "assumed", "stop_slip_bps": "assumed", "per_symbol_slip_bps": {}}
    kw = {}
    fee = m["taker_fee_bps"]
    if fee.get("n", 0) >= MIN_OBS_COST:
        kw["taker_fee_bps"] = fee["mean"]
        prov["taker_fee_bps"] = f"measured (n={fee['n']})"
    es, ss = m["entry_slip_bps_all"], m["stop_slip_bps_all"]
    if es.get("n", 0) >= MIN_OBS_COST:
        kw["market_slip_bps"] = max(0.0, es["mean"])
        prov["market_slip_bps"] = f"measured (n={es['n']})"
    if ss.get("n", 0) >= MIN_OBS_COST:
        kw["stop_slip_bps"] = max(0.0, ss["mean"])
        prov["stop_slip_bps"] = f"measured (n={ss['n']})"
    per = dict(base.per_symbol_slip_bps)
    for pair, v in m["by_pair"].items():
        sym = pair.replace("/", "")
        e, s = v["entry_slip_bps"], v["stop_slip_bps"]
        if e.get("n", 0) >= MIN_OBS_COST and s.get("n", 0) >= MIN_OBS_COST:
            per[sym] = (max(0.0, e["mean"]), max(0.0, s["mean"]))
            prov["per_symbol_slip_bps"][sym] = f"measured (n={e['n']}/{s['n']})"
    kw["per_symbol_slip_bps"] = per
    return replace(base, **kw), prov


def validate_in_time(rows: list[Economics]) -> dict:
    """Fit on the earlier 60%, compare with the later 40%. Never claims validation it cannot support."""
    live = sorted(usable(rows), key=lambda r: r.opened_ms or 0)
    cut = int(len(live) * FIT_FRACTION)
    early, late = live[:cut], live[cut:]
    if len(late) < MIN_TRADES_HOLDOUT or len(early) < MIN_TRADES_HOLDOUT:
        return {"status": "not validated", "reason": f"needs at least {MIN_TRADES_HOLDOUT} trades in each part; have {len(early)} and {len(late)}",
                "n_early": len(early), "n_late": len(late)}
    out = {"status": "checked", "n_early": len(early), "n_late": len(late), "metrics": {}}
    for name, getter in (("entry_slip_bps", lambda r: r.entry_slip_bps), ("net_cost_share", None)):
        if getter is None:
            continue
        e = [getter(r) for r in early if getter(r) is not None]
        l = [getter(r) for r in late if getter(r) is not None]
        if len(e) < 5 or len(l) < 5:
            out["metrics"][name] = {"status": "too few observations"}
            continue
        ci = bootstrap_mean_ci(e)
        lm = statistics.fmean(l)
        inside = ci is not None and ci["low"] <= lm <= ci["high"]
        out["metrics"][name] = {"early_mean": statistics.fmean(e), "late_mean": lm, "early_ci": [ci["low"], ci["high"]] if ci else None,
                                "late_inside_early_ci": inside}
    drift = [k for k, v in out["metrics"].items() if v.get("late_inside_early_ci") is False]
    out["drift"] = drift
    return out
