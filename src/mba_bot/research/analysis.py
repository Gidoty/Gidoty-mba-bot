"""Pure statistics for Phase 1: comparing a candidate with the baseline, the pre-registered survival screen,
and the descriptive studies (sides, volatility, ADX, cross-event flips, exits).

Everything takes plain lists of TradeRecord (or tapes) and returns plain dicts, so it is cheap to test and nothing
here runs a backtest or touches a database. Numbers are never rounded here; rendering does that."""
from __future__ import annotations

import random
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable

from . import tape as tapelib
from .engine import TradeRecord
from .metrics import trade_stats

DAY_MS = 86_400_000


# ------------------------------------------------------------------ small helpers


def half_year(ts_ms: int) -> str:
    d = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return f"{d.year}-H{1 if d.month <= 6 else 2}"


def year(ts_ms: int) -> int:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).year


def in_window(trades: Iterable[TradeRecord], start: int, end: int) -> list[TradeRecord]:
    """Trades that ENTERED inside [start, end). For a rule with nothing fitted this is exactly 'what the rule
    did during that period'."""
    return [t for t in trades if start <= t.entry_ts < end and t.exit_reason != "end_of_data"]


def mean_r(trades: list[TradeRecord]) -> float | None:
    return statistics.fmean(t.r_net for t in trades) if trades else None


def top_decile_mean(trades: list[TradeRecord]) -> float | None:
    """Average R of the best 10% of trades (at least one trade): a direct measure of whether the large winners survive."""
    if not trades:
        return None
    r = sorted((t.r_net for t in trades), reverse=True)
    k = max(1, len(r) // 10)
    return statistics.fmean(r[:k])


def bootstrap_difference(cand: list[float], base: list[float], *, iterations: int = 4000, seed: int = 17, alpha: float = 0.10) -> dict | None:
    """Difference of mean R (candidate minus baseline) with independent resampling of each side. Trades are not
    paired between a candidate and the baseline (the path differs), and they cluster in time, so this is a rough
    guide, not a test."""
    if len(cand) < 5 or len(base) < 5:
        return None
    rng = random.Random(seed)
    nc, nb = len(cand), len(base)
    diffs = []
    for _ in range(iterations):
        mc = sum(cand[rng.randrange(nc)] for _ in range(nc)) / nc
        mb = sum(base[rng.randrange(nb)] for _ in range(nb)) / nb
        diffs.append(mc - mb)
    diffs.sort()
    return {
        "low": diffs[int(alpha / 2 * iterations)], "high": diffs[int((1 - alpha / 2) * iterations) - 1],
        "prob_positive": sum(1 for d in diffs if d > 0) / iterations,
    }


# ------------------------------------------------------------------ candidate versus baseline


@dataclass(frozen=True)
class Screen:
    """The pre-registered survival screen. Written before any real result was seen; do not edit afterwards."""

    min_delta_r: float = 0.03
    min_prob_positive: float = 0.80
    min_pairs_not_worse: int = 3  # of the four development pairs
    min_halfyear_fraction: float = 0.60
    min_halfyear_trades: int = 10  # a half-year needs this many baseline trades to count
    min_retention: float = 0.50
    max_drawdown_worsening_pts: float = 2.0
    min_top_decile_ratio: float = 0.80
    min_oos_delta_r: float = 0.0
    min_oos_fold_fraction: float = 0.50
    stress_min_delta_r: float = 0.02
    holdout_min_pairs_not_worse: int = 4  # of the six holdout pairs
    min_dsr: float = 0.95


def compare(cand_by_pair: dict[str, list[TradeRecord]], base_by_pair: dict[str, list[TradeRecord]], cand_dd: dict[str, float], base_dd: dict[str, float], screen: Screen) -> dict:
    """Candidate against baseline on the same pairs and period."""
    cand = [t for ts in cand_by_pair.values() for t in ts if t.exit_reason != "end_of_data"]
    base = [t for ts in base_by_pair.values() for t in ts if t.exit_reason != "end_of_data"]
    cs, bs = trade_stats(cand), trade_stats(base)
    out: dict = {"candidate": _brief(cs), "baseline": _brief(bs)}
    if not cand or not base:
        out["error"] = "no trades on one side"
        return out
    delta = cs["expectancy_r"] - bs["expectancy_r"]
    boot = bootstrap_difference([t.r_net for t in cand], [t.r_net for t in base])
    per_pair = {}
    for sym in base_by_pair:
        cm = mean_r([t for t in cand_by_pair.get(sym, []) if t.exit_reason != "end_of_data"])
        bm = mean_r([t for t in base_by_pair[sym] if t.exit_reason != "end_of_data"])
        per_pair[sym] = {"candidate_r": cm, "baseline_r": bm, "delta": (cm - bm) if (cm is not None and bm is not None) else None,
                         "n_candidate": len([t for t in cand_by_pair.get(sym, []) if t.exit_reason != "end_of_data"]),
                         "n_baseline": len([t for t in base_by_pair[sym] if t.exit_reason != "end_of_data"])}
    hy_c, hy_b = _group_means(cand, half_year), _group_means(base, half_year)
    halves = {}
    for h in sorted(hy_b):
        if hy_b[h][1] >= screen.min_halfyear_trades and h in hy_c:
            halves[h] = {"candidate_r": hy_c[h][0], "baseline_r": hy_b[h][0], "delta": hy_c[h][0] - hy_b[h][0], "n_candidate": hy_c[h][1], "n_baseline": hy_b[h][1]}
    td_c, td_b = top_decile_mean(cand), top_decile_mean(base)
    dd_worst_c = max(cand_dd.values()) if cand_dd else None
    dd_worst_b = max(base_dd.values()) if base_dd else None
    out.update({
        "delta_r": delta, "bootstrap": boot, "per_pair": per_pair, "half_years": halves,
        "retention": len(cand) / len(base), "top_decile_candidate": td_c, "top_decile_baseline": td_b,
        "top_decile_ratio": (td_c / td_b) if (td_b and td_b > 0 and td_c is not None) else None,
        "worst_drawdown_candidate": dd_worst_c, "worst_drawdown_baseline": dd_worst_b,
        "big_winners_candidate": sum(1 for t in cand if t.r_net >= 3.0), "big_winners_baseline": sum(1 for t in base if t.r_net >= 3.0),
    })
    pairs_ok = sum(1 for v in per_pair.values() if v["delta"] is not None and v["delta"] >= 0)
    halves_ok = sum(1 for v in halves.values() if v["delta"] >= 0)
    out["pairs_not_worse"] = pairs_ok
    out["half_years_not_worse"] = (halves_ok, len(halves))
    checks = {
        "C1 improves expectancy": delta >= screen.min_delta_r,
        "C2 improvement unlikely to be noise": boot is not None and boot["prob_positive"] >= screen.min_prob_positive,
        "C3 not worse on enough pairs": pairs_ok >= min(screen.min_pairs_not_worse, len(per_pair)),
        "C4 stable across time": bool(halves) and halves_ok / len(halves) >= screen.min_halfyear_fraction,
        "C5 keeps enough trades": out["retention"] >= screen.min_retention,
        "C6 drawdown not materially worse": dd_worst_c is not None and dd_worst_b is not None and dd_worst_c <= dd_worst_b + screen.max_drawdown_worsening_pts,
        "C7 profit factor not worse": (cs.get("profit_factor") or 0) >= (bs.get("profit_factor") or 0),
        "C8 large winners preserved": out["top_decile_ratio"] is not None and out["top_decile_ratio"] >= screen.min_top_decile_ratio,
    }
    out["checks"] = checks
    return out


def _brief(s: dict) -> dict:
    return {k: s.get(k) for k in ("n_trades", "win_rate", "expectancy_r", "expectancy_r_gross", "profit_factor", "avg_cost_r", "net_pnl", "avg_win_r", "avg_loss_r", "best_r")}


def _group_means(trades: list[TradeRecord], key: Callable[[int], object]) -> dict[str, tuple[float, int]]:
    groups: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        groups[str(key(t.entry_ts))].append(t.r_net)
    return {k: (statistics.fmean(v), len(v)) for k, v in groups.items()}


# ------------------------------------------------------------------ walk-forward on a continuous run


def fold_table(cand: list[TradeRecord], base: list[TradeRecord], folds) -> dict:
    """Out-of-sample behaviour of a rule with nothing fitted: its trades inside each test window against the baseline's."""
    rows = []
    for f in folds:
        c, b = in_window(cand, *f.test), in_window(base, *f.test)
        cm, bm = mean_r(c), mean_r(b)
        rows.append({"fold": f.index, "test": list(f.test), "n_candidate": len(c), "n_baseline": len(b), "candidate_r": cm, "baseline_r": bm,
                     "delta": (cm - bm) if (cm is not None and bm is not None) else None})
    oos_c = [t for f in folds for t in in_window(cand, *f.test)]
    oos_b = [t for f in folds for t in in_window(base, *f.test)]
    cm, bm = mean_r(oos_c), mean_r(oos_b)
    deltas = [r["delta"] for r in rows if r["delta"] is not None]
    return {
        "rows": rows, "oos_candidate_r": cm, "oos_baseline_r": bm, "oos_delta": (cm - bm) if (cm is not None and bm is not None) else None,
        "oos_trades": len(oos_c), "fold_fraction_positive": (sum(1 for d in deltas if d > 0) / len(deltas)) if deltas else None,
    }


def apply_oos_checks(compare_out: dict, folds_out: dict, screen: Screen) -> None:
    d = folds_out.get("oos_delta")
    f = folds_out.get("fold_fraction_positive")
    compare_out["checks"]["C9 improves out of sample"] = d is not None and d > screen.min_oos_delta_r and f is not None and f >= screen.min_oos_fold_fraction


# ------------------------------------------------------------------ descriptive studies (baseline trades)


def _stats_row(ts: list[TradeRecord]) -> dict:
    s = trade_stats(ts)
    return {k: s.get(k) for k in ("n_trades", "win_rate", "expectancy_r", "profit_factor", "avg_win_r", "avg_loss_r")}


def side_persistence(trades_by_pair: dict[str, list[TradeRecord]]) -> dict:
    """Is the long/short gap a stable feature of the data or a feature of a few periods?"""
    out: dict = {"pooled": {}, "by_pair": {}, "by_half_year": {}, "gap_by_pair_halfyear": {}}
    allt = [t for ts in trades_by_pair.values() for t in ts if t.exit_reason != "end_of_data"]
    for side in ("long", "short"):
        out["pooled"][side] = _stats_row([t for t in allt if t.side == side])
    lr = [t.r_net for t in allt if t.side == "long"]
    sr = [t.r_net for t in allt if t.side == "short"]
    out["pooled"]["gap_long_minus_short"] = (statistics.fmean(lr) - statistics.fmean(sr)) if lr and sr else None
    out["pooled"]["gap_bootstrap"] = bootstrap_difference(lr, sr)
    for sym, ts in trades_by_pair.items():
        ts = [t for t in ts if t.exit_reason != "end_of_data"]
        l = [t.r_net for t in ts if t.side == "long"]
        s = [t.r_net for t in ts if t.side == "short"]
        out["by_pair"][sym] = {"long": _stats_row([t for t in ts if t.side == "long"]), "short": _stats_row([t for t in ts if t.side == "short"]),
                               "gap": (statistics.fmean(l) - statistics.fmean(s)) if l and s else None, "gap_bootstrap": bootstrap_difference(l, s)}
    halves = sorted({half_year(t.entry_ts) for t in allt})
    longer = 0
    counted = 0
    for h in halves:
        l = [t.r_net for t in allt if half_year(t.entry_ts) == h and t.side == "long"]
        s = [t.r_net for t in allt if half_year(t.entry_ts) == h and t.side == "short"]
        if len(l) >= 10 and len(s) >= 10:
            counted += 1
            gap = statistics.fmean(l) - statistics.fmean(s)
            longer += gap > 0
            out["by_half_year"][h] = {"long_r": statistics.fmean(l), "short_r": statistics.fmean(s), "gap": gap, "n_long": len(l), "n_short": len(s)}
    out["half_years_long_better"] = (longer, counted)
    # same question in the 30-day market context: do shorts do better when the market was actually falling?
    ctx = {}
    for side in ("long", "short"):
        for label, lo, hi in (("down (<-10%)", -9, -0.10), ("flat", -0.10, 0.10), ("up (>+10%)", 0.10, 9)):
            ts = [t for t in allt if t.side == side and t.features.get("ret30d") is not None and lo <= t.features["ret30d"] < hi]
            ctx[f"{side} / 30d {label}"] = _stats_row(ts)
    out["by_market_context"] = ctx
    return out


def side_by_fold(trades_by_pair: dict[str, list[TradeRecord]], folds) -> list[dict]:
    """The same long/short question, window by window, over the walk-forward test windows."""
    allt = [t for ts in trades_by_pair.values() for t in ts]
    rows = []
    for f in folds:
        win = in_window(allt, *f.test)
        l = [t.r_net for t in win if t.side == "long"]
        s = [t.r_net for t in win if t.side == "short"]
        rows.append({"fold": f.index, "test": list(f.test), "n_long": len(l), "n_short": len(s), "long_r": statistics.fmean(l) if l else None,
                     "short_r": statistics.fmean(s) if s else None, "gap": (statistics.fmean(l) - statistics.fmean(s)) if l and s else None})
    return rows


VOL_BUCKETS = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0001)]
ADX_BUCKETS = [(0, 15), (15, 20), (20, 25), (25, 30), (30, 40), (40, 200)]


def bucket_table(trades: list[TradeRecord], getter: Callable[[TradeRecord], float | None], buckets: list[tuple[float, float]]) -> list[dict]:
    rows = []
    ts = [t for t in trades if t.exit_reason != "end_of_data"]
    for lo, hi in buckets:
        sel = [t for t in ts if getter(t) is not None and lo <= getter(t) < hi]
        row = {"bucket": f"{lo:g}-{min(hi, 1.0) if hi < 2 else hi:g}", **_stats_row(sel)}
        rows.append(row)
    return rows


def volatility_study(trades_by_pair: dict[str, list[TradeRecord]]) -> dict:
    out = {"by_pair": {}, "pooled": bucket_table([t for ts in trades_by_pair.values() for t in ts], lambda t: t.features.get("vol_pct_90d"), VOL_BUCKETS)}
    for sym, ts in trades_by_pair.items():
        ts = [t for t in ts if t.exit_reason != "end_of_data"]
        apct = [t.atr_pct for t in ts if t.atr_pct]
        out["by_pair"][sym] = {
            "buckets": bucket_table(ts, lambda t: t.features.get("vol_pct_90d"), VOL_BUCKETS),
            "median_atr_pct_at_entry": statistics.median(apct) if apct else None,
            "share_of_entries_in_top_quintile": (sum(1 for t in ts if (t.features.get("vol_pct_90d") or 0) >= 0.8) / len(ts)) if ts else None,
            "median_stop_distance_pct": statistics.median([abs(t.signal_price - t.stop_price) / t.signal_price for t in ts]) if ts else None,
        }
    return out


def adx_study(trades_by_pair: dict[str, list[TradeRecord]]) -> dict:
    out = {"pooled": bucket_table([t for ts in trades_by_pair.values() for t in ts], lambda t: t.features.get("adx14"), ADX_BUCKETS), "by_pair": {}}
    for sym, ts in trades_by_pair.items():
        out["by_pair"][sym] = bucket_table(ts, lambda t: t.features.get("adx14"), ADX_BUCKETS)
    return out


def exit_study(trades_by_pair: dict[str, list[TradeRecord]]) -> dict:
    """How the winners and losers look, so exit changes can be judged against what they could break."""
    allt = [t for ts in trades_by_pair.values() for t in ts if t.exit_reason != "end_of_data"]
    if not allt:
        return {}
    big = [t for t in allt if t.r_net >= 3.0]
    winners = [t for t in allt if t.net_pnl > 0]
    reached = {f"mfe_ge_{k}r": sum(1 for t in allt if t.mfe_r >= k) / len(allt) for k in (1, 2, 3, 5)}
    by_exit = {}
    for reason in sorted({t.exit_reason for t in allt}):
        by_exit[reason] = _stats_row([t for t in allt if t.exit_reason == reason])
    giveback = [t.mfe_r - t.r_net for t in winners]
    # of the trades that ended as big winners, how deep did they dip first (MAE in R) and how often did they touch +1R early?
    return {
        "n_trades": len(allt), "n_big_winners_ge_3r": len(big), "share_of_total_positive_r_from_big_winners": (
            sum(t.r_net for t in big) / sum(t.r_net for t in allt if t.r_net > 0)) if any(t.r_net > 0 for t in allt) else None,
        "big_winner_median_mae_r": statistics.median([t.mae_r for t in big]) if big else None,
        "big_winner_share_with_mae_over_1r": (sum(1 for t in big if t.mae_r > 1.0) / len(big)) if big else None,
        "reach_fractions": reached, "winner_avg_giveback_r": statistics.fmean(giveback) if giveback else None,
        "by_exit_reason": by_exit,
        "mfe_distribution_r": {f"p{p}": _pct([t.mfe_r for t in allt], p) for p in (50, 75, 90, 95, 99)},
        "hold_hours_big_winners_median": statistics.median([t.hold_hours for t in big]) if big else None,
    }


def _pct(values: list[float], p: int) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(p / 100 * len(s)))]


# ------------------------------------------------------------------ filter counterfactuals (no backtest needed)


def filter_counterfactual(trades: list[TradeRecord], predicate_reject: Callable[[TradeRecord], bool]) -> dict:
    """Which BASELINE trades a filter would have rejected, and how those trades actually did. Path effects (a rejected
    trade lets a later one start earlier) are ignored here; the full backtest of the candidate includes them."""
    ts = [t for t in trades if t.exit_reason != "end_of_data"]
    rejected = [t for t in ts if predicate_reject(t)]
    kept = [t for t in ts if not predicate_reject(t)]
    return {"rejected": _stats_row(rejected), "kept": _stats_row(kept), "share_rejected": (len(rejected) / len(ts)) if ts else None}


# ------------------------------------------------------------------ cross-event study (flip behaviour)


def cross_event_study(tape: tapelib.SignalTape, timeline: list[int], k0: int, k1: int, *, hour_ms: int = 3_600_000) -> dict:
    """Every hour in which the forming candle ever showed an actionable crossover, regardless of any account.
    For each, when did it first appear, how decisive was it then, how long did it persist, and was it still there
    at the hour's close? This is the evidence for choosing a persistence or margin rule rather than guessing one."""
    events = []
    cur_hs = None
    cur: list[tuple[int, int, float]] = []  # (poll index within hour, code, gap_atr)

    def flush():
        if not cur:
            return
        codes = [c for _, c, _ in cur if c in (tapelib.CODE_BULL, tapelib.CODE_BEAR)]
        if not codes:
            return
        first_i, first_code, first_gap = next((i, c, g) for i, c, g in cur if c in (tapelib.CODE_BULL, tapelib.CODE_BEAR))
        run = 0  # consecutive polls the same signal stayed visible, counting from its first appearance
        for i, c, _ in cur:
            if i < first_i:
                continue
            if c != first_code:
                break
            run += 1
        last_i, last_c, _ = cur[-1]
        events.append({"first_poll": first_i + 1, "first_gap_atr": first_gap, "polls_visible": run, "side": "long" if first_code == tapelib.CODE_BULL else "short",
                       "confirmed": last_i == 11 and last_c == first_code})

    for k in range(k0, k1):
        ts = timeline[k]
        hs = ts - ts % hour_ms
        if hs != cur_hs:
            flush()
            cur_hs, cur = hs, []
        c = tape.code(k)
        if c == tapelib.CODE_NA:
            continue
        gap = 0.0
        if c in (tapelib.CODE_BULL, tapelib.CODE_BEAR) and tape.atr[k] == tape.atr[k] and tape.atr[k]:
            gap = abs(tape.fast[k] - tape.slow[k]) / tape.atr[k]
        cur.append(((ts - hs) // 300_000, c, gap))
    flush()
    complete = events
    out: dict = {"events": len(complete), "confirmed_share": (sum(1 for e in complete if e["confirmed"]) / len(complete)) if complete else None}
    gap_rows = []
    for lo, hi in ((0.0, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 0.40), (0.40, 99.0)):
        sel = [e for e in complete if lo <= e["first_gap_atr"] < hi]
        gap_rows.append({"first_gap_atr": f"{lo:g}-{hi:g}", "events": len(sel), "confirmed_share": (sum(1 for e in sel if e["confirmed"]) / len(sel)) if sel else None})
    out["by_first_gap"] = gap_rows
    persist_rows = []
    for n in (1, 2, 3, 6, 9, 11):
        sel = [e for e in complete if e["polls_visible"] >= n]
        persist_rows.append({"visible_at_least_polls": n, "events": len(sel), "confirmed_share": (sum(1 for e in sel if e["confirmed"]) / len(sel)) if sel else None})
    out["by_persistence"] = persist_rows
    by_first = []
    for m in (1, 3, 6, 9, 12):
        sel = [e for e in complete if e["first_poll"] <= m]
        by_first.append({"first_seen_by_poll": m, "events": len(sel), "confirmed_share": (sum(1 for e in sel if e["confirmed"]) / len(sel)) if sel else None})
    out["by_first_seen"] = by_first
    return out


# ------------------------------------------------------------------ overfitting evidence


def neighbour_stability(family_deltas: list[tuple[str, float]]) -> dict:
    """A real effect usually varies smoothly across neighbouring parameter values. One good value amid poor ones
    is the signature of noise. `family_deltas` is ordered by parameter."""
    vals = [d for _, d in family_deltas]
    if len(vals) < 3:
        return {"verdict": "too few members to judge"}
    best = max(range(len(vals)), key=lambda i: vals[i])
    neighbours = [vals[j] for j in (best - 1, best + 1) if 0 <= j < len(vals)]
    spread = max(vals) - min(vals)
    monotone = all(b >= a for a, b in zip(vals, vals[1:])) or all(b <= a for a, b in zip(vals, vals[1:]))
    isolated = bool(neighbours) and all(n < vals[best] - 0.6 * spread for n in neighbours) and spread > 0
    return {"best": family_deltas[best][0], "values": family_deltas, "monotone": monotone, "isolated_peak": isolated,
            "verdict": "isolated peak (fragile)" if isolated else ("smooth" if monotone else "uneven")}
