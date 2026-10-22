"""Markdown rendering of a Phase 1 results bundle. Pure formatting: no number is computed here that is not already in
the results dict, and nothing is hidden: failed candidates are listed with the checks they failed."""
from __future__ import annotations

from datetime import datetime, timezone


def _f(x, d=3, plus=False, pct=False) -> str:
    if x is None:
        return "n/a"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x)
    if pct:
        v *= 100
    return f"{v:{'+' if plus else ''}.{d}f}" + ("%" if pct else "")


def _d(ts) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d") if ts else "n/a"


def _row(*cells) -> str:
    return "| " + " | ".join(str(c) for c in cells) + " |"


def _table(header: list[str], rows: list[list]) -> list[str]:
    return [_row(*header), _row(*["---"] * len(header))] + [_row(*r) for r in rows] + [""]


CHECK_CODES = {
    "C1 improves expectancy": "C1", "C2 improvement unlikely to be noise": "C2", "C3 not worse on enough pairs": "C3", "C4 stable across time": "C4",
    "C5 keeps enough trades": "C5", "C6 drawdown not materially worse": "C6", "C7 profit factor not worse": "C7", "C8 large winners preserved": "C8",
    "C9 improves out of sample": "C9",
}


def render(results: dict) -> str:
    m = results["meta"]
    L: list[str] = []
    if m["synthetic"]:
        L += ["> **SYNTHETIC DATA.** These numbers come from generated prices and demonstrate the pipeline only.", ""]
    if m["partial"]:
        L += ["> **PARTIAL PLAN.** This run used a subset of the pre-registered candidates. Its plan hash differs from the registered one and it cannot be used for the Phase 1 decision.", ""]
    L += ["# Phase 1 research results", "",
          f"- Plan hash `{m['plan_hash']}` (registered full plan `{m['full_plan_hash']}`) | code `{m['code_version']}` | data fingerprint `{m['data_fingerprint']}`",
          f"- Development pairs: {', '.join(m['dev_pairs'])} | holdout pairs (never used to choose anything): {', '.join(m['holdout_pairs'])}"
          + (f" | missing data: {', '.join(m['missing_pairs'])}" if m["missing_pairs"] else ""),
          f"- Data span {_d(m['span'][0])} to {_d(m['span'][1])} | development period {_d(m['development_range'][0])} to {_d(m['development_range'][1])} | "
          f"**lockbox {_d(m['lockbox_range'][0])} to {_d(m['lockbox_range'][1])} was not touched**",
          f"- Trials registered: {m['n_trials']} (this is what the deflated Sharpe ratio is deflated by)",
          f"- Walk-forward folds: {len(m['folds'])} (train {int((m['folds'][0]['train'][1] - m['folds'][0]['train'][0]) / 86_400_000) if m['folds'] else 0} days, test windows tile the development period)",
          f"- Account: {m['account']['allocated_capital']} capital, {m['account']['risk_pct']}% risk, base costs {m['costs']['taker_fee_bps']} bp fee / {m['costs']['market_slip_bps']} bp slippage / {m['costs']['stop_slip_bps']} bp stop slippage", ""]

    # --- S0
    rep = results.get("repro") or []
    L += ["## S0. Phase 0 baseline reproduced by the Phase 1 engine", ""]
    if rep:
        ok = all(r["identical"] for r in rep)
        L.append(f"**{'All ' + str(len(rep)) + ' runs identical' if ok else 'MISMATCH: do not trust anything below until explained'}.**")
        L.append("")
        L += _table(["Run", "Phase 0 trades", "Phase 1 trades", "Phase 0 E[R]", "Phase 1 E[R]", "Identical"],
                    [[r["run"], r["phase0_trades"], r["phase1_trades"], _f(r["phase0_expectancy_r"], 4, True), _f(r["phase1_expectancy_r"], 4, True), "yes" if r["identical"] else "NO"] for r in rep])
    else:
        L += ["Not run (no baseline.json supplied or reproduction skipped).", ""]

    # --- S1
    dg = results["diagnostics"]
    L += ["## S1. What the baseline looks like on the development period", ""]
    L += ["### Replay (today's behaviour) against the closed-candle comparison, per pair", ""]
    L += _table(["Pair", "Replay trades", "Replay E[R]", "Replay PF", "Replay max DD", "Closed trades", "Closed E[R]", "Closed PF", "Closed max DD"],
                [[s, v["replay"]["n_trades"], _f(v["replay"]["expectancy_r"], 3, True), _f(v["replay"]["profit_factor"], 2), _f(v["replay_dd"], 1) + "%",
                  v["closed"]["n_trades"], _f(v["closed"]["expectancy_r"], 3, True), _f(v["closed"]["profit_factor"], 2), _f(v["closed_dd"], 1) + "%"] for s, v in dg["baseline_vs_closed_candle"].items()])
    if "auto_baseline" in dg:
        a = dg["auto_baseline"]
        L.append(f"Auto Select (original four): replay {a['replay']['n_trades']} trades, E[R] {_f(a['replay']['expectancy_r'], 3, True)}, max DD {_f(a['replay_dd'], 1)}% | "
                 f"closed {a['closed']['n_trades']} trades, E[R] {_f(a['closed']['expectancy_r'], 3, True)}, max DD {_f(a['closed_dd'], 1)}%")
        L.append("")

    L += ["### Sides: is the long/short gap stable?", ""]
    for label, key in (("Development pairs", "sides_development"), ("Holdout pairs (descriptive only)", "sides_holdout")):
        s = dg.get(key)
        if not s:
            continue
        p = s["pooled"]
        gb = p.get("gap_bootstrap")
        L.append(f"**{label}:** long {p['long']['n_trades']} trades E[R] {_f(p['long']['expectancy_r'], 3, True)} | short {p['short']['n_trades']} trades E[R] {_f(p['short']['expectancy_r'], 3, True)} | "
                 f"gap (long minus short) {_f(p.get('gap_long_minus_short'), 3, True)}" + (f", 90% interval {_f(gb['low'], 2)} to {_f(gb['high'], 2)}" if gb else "")
                 + f" | long better in {s['half_years_long_better'][0]} of {s['half_years_long_better'][1]} half-years with enough trades")
        L.append("")
        L += _table(["Pair", "Long n", "Long E[R]", "Short n", "Short E[R]", "Gap"],
                    [[sym, v["long"]["n_trades"], _f(v["long"]["expectancy_r"], 3, True), v["short"]["n_trades"], _f(v["short"]["expectancy_r"], 3, True), _f(v["gap"], 3, True)] for sym, v in s["by_pair"].items()])
        L += _table(["Half-year", "Long E[R]", "Short E[R]", "Gap", "n long / short"], [[h, _f(v["long_r"], 3, True), _f(v["short_r"], 3, True), _f(v["gap"], 3, True), f"{v['n_long']} / {v['n_short']}"] for h, v in s["by_half_year"].items()])
        if key == "sides_development" and dg.get("sides_by_test_fold"):
            L += _table(["Walk-forward test window", "Long n", "Long E[R]", "Short n", "Short E[R]", "Gap"],
                        [[f"{_d(r['test'][0])} to {_d(r['test'][1])}", r["n_long"], _f(r["long_r"], 3, True), r["n_short"], _f(r["short_r"], 3, True), _f(r["gap"], 3, True)] for r in dg["sides_by_test_fold"]])
        L += _table(["Side / 30-day market", "n", "E[R]", "PF"], [[k, v["n_trades"], _f(v["expectancy_r"], 3, True), _f(v["profit_factor"], 2)] for k, v in s["by_market_context"].items()])

    L += ["### Volatility: entry by the pair's own trailing 90-day ATR% percentile", ""]
    for label, key in (("Development", "volatility_development"), ("Holdout (descriptive)", "volatility_holdout")):
        v = dg.get(key)
        if not v:
            continue
        L.append(f"**{label}, pooled**")
        L.append("")
        L += _table(["Percentile bucket", "n", "Win %", "E[R]", "PF"], [[b["bucket"], b["n_trades"], _f(b["win_rate"], 1, pct=True), _f(b["expectancy_r"], 3, True), _f(b["profit_factor"], 2)] for b in v["pooled"]])
        rows = []
        for sym, d in v["by_pair"].items():
            rows.append([sym] + [f"{_f(b['expectancy_r'], 2, True)} (n={b['n_trades']})" for b in d["buckets"]] + [_f(d["median_atr_pct_at_entry"], 4, pct=True), _f(d["median_stop_distance_pct"], 2, pct=True)])
        L += _table(["Pair"] + [b["bucket"] for b in v["pooled"]] + ["Median ATR% at entry", "Median stop distance"], rows)

    L += ["### ADX at entry (is the edge in established trends or in transitions?)", ""]
    for label, key in (("Development", "adx_development"), ("Holdout (descriptive)", "adx_holdout")):
        v = dg.get(key)
        if not v:
            continue
        L.append(f"**{label}, pooled**")
        L.append("")
        L += _table(["ADX bucket", "n", "Win %", "E[R]", "PF"], [[b["bucket"], b["n_trades"], _f(b["win_rate"], 1, pct=True), _f(b["expectancy_r"], 3, True), _f(b["profit_factor"], 2)] for b in v["pooled"]])
        rows = [[sym] + [f"{_f(b['expectancy_r'], 2, True)} (n={b['n_trades']})" for b in bs] for sym, bs in v["by_pair"].items()]
        L += _table(["Pair"] + [b["bucket"] for b in v["pooled"]], rows)

    ex = dg.get("exits_development") or {}
    if ex:
        L += ["### Exit anatomy (development, replay)", "",
              f"- {ex['n_trades']} trades; {ex['n_big_winners_ge_3r']} reached +3R or better, and they supply {_f(ex['share_of_total_positive_r_from_big_winners'], 1, pct=True)} of all positive R",
              f"- Big winners' median worst dip before winning: {_f(ex['big_winner_median_mae_r'], 2)}R; {_f(ex['big_winner_share_with_mae_over_1r'], 1, pct=True)} of them dipped more than 1R first",
              "- Share of all trades whose best excursion reached: " + ", ".join(f"{k.replace('mfe_ge_', '+').replace('r', 'R')} {_f(v, 1, pct=True)}" for k, v in ex["reach_fractions"].items()),
              f"- Winners gave back on average {_f(ex['winner_avg_giveback_r'], 2)}R from their best point",
              f"- Median hold of big winners: {_f(ex['hold_hours_big_winners_median'], 1)} hours", ""]
        L += _table(["Exit reason", "n", "Win %", "E[R]", "Avg win R", "Avg loss R"], [[k, v["n_trades"], _f(v["win_rate"], 1, pct=True), _f(v["expectancy_r"], 3, True), _f(v["avg_win_r"], 2), _f(v["avg_loss_r"], 2)] for k, v in ex["by_exit_reason"].items()])

    fl = dg.get("cross_event_flips") or {}
    if fl:
        L += ["### Crossover flips (every hour in which the forming candle showed a cross, development period)", ""]
        tot = sum(v["events"] for v in fl.values())
        conf = sum(v["events"] * (v["confirmed_share"] or 0) for v in fl.values())
        L.append(f"{tot} cross events, {_f(conf / tot if tot else None, 1, pct=True)} still valid at the hour's close.")
        L.append("")
        agg: dict = {}
        for v in fl.values():
            for key, label in (("by_first_gap", "first_gap_atr"), ("by_persistence", "visible_at_least_polls"), ("by_first_seen", "first_seen_by_poll")):
                for row in v[key]:
                    a = agg.setdefault((key, row[label]), [0, 0.0])
                    a[0] += row["events"]
                    a[1] += row["events"] * (row["confirmed_share"] or 0)
        for key, title in (("by_first_gap", "EMA gap (ATR) when the cross first appeared"), ("by_persistence", "Polls the cross stayed visible in a row"), ("by_first_seen", "Poll in the hour when it first appeared")):
            rows = [[lab, n, _f(c / n if n else None, 1, pct=True)] for (k, lab), (n, c) in agg.items() if k == key]
            L += _table([title, "events", "still valid at close"], rows)

    cf = dg.get("filter_counterfactuals") or {}
    if cf:
        L += ["### What each entry filter would have removed from the baseline trades (counterfactual, ignoring path effects)", ""]
        L += _table(["Candidate", "Share removed", "Removed: n", "Removed: E[R]", "Kept: n", "Kept: E[R]"],
                    [[cid, _f(v["share_rejected"], 1, pct=True), v["rejected"]["n_trades"], _f(v["rejected"]["expectancy_r"], 3, True), v["kept"]["n_trades"], _f(v["kept"]["expectancy_r"], 3, True)] for cid, v in cf.items()])

    # --- S2
    L += ["## S2. Candidates against the baseline (development pairs, development period, base costs)", "",
          "Delta = candidate minus baseline. Checks: C1 improvement of at least the registered minimum; C2 unlikely to be noise; C3 not worse on enough pairs; C4 not worse in enough half-years; "
          "C5 keeps enough trades; C6 drawdown not materially worse; C7 profit factor not worse; C8 large winners preserved; C9 improves in out-of-sample test windows.", ""]
    rows = []
    for cid, c in results["candidates"].items():
        if c.get("null_benchmark"):
            continue
        cmp_ = c["compare"]
        if "delta_r" not in cmp_:
            rows.append([cid, c["label"], "no trades", "", "", "", "", "", "", "", "", ""])
            continue
        b = cmp_.get("bootstrap") or {}
        wf = c["walk_forward"]
        halves = cmp_["half_years_not_worse"]
        verdict = "diagnostic" if c["diagnostic"] else ("**PASS**" if c["passed_screen"] else "fail: " + ", ".join(CHECK_CODES.get(x, x) for x in c["failed_checks"]))
        rows.append([cid, c["label"], _f(cmp_["candidate"]["n_trades"], 0), _f(cmp_["retention"], 0, pct=True), _f(cmp_["candidate"]["expectancy_r"], 3, True), _f(cmp_["delta_r"], 3, True),
                     _f(b.get("prob_positive"), 2), _f(cmp_["candidate"]["profit_factor"], 2), f"{_f(cmp_['worst_drawdown_candidate'], 1)}% ({_f(cmp_['worst_drawdown_candidate'] - cmp_['worst_drawdown_baseline'], 1, True)})",
                     f"{cmp_['pairs_not_worse']}/{len(cmp_['per_pair'])}", f"{halves[0]}/{halves[1]}", _f(cmp_["top_decile_ratio"], 2),
                     f"{_f(wf.get('oos_delta'), 3, True)} ({_f(wf.get('fold_fraction_positive'), 0, pct=True)} folds+)", verdict])
    if results["candidates"]:
        any_c = next(c for c in results["candidates"].values() if "compare" in c and "delta_r" in c["compare"])
        bs = any_c["compare"]["baseline"]
        L.append(f"Baseline (fixed pairs, development): {bs['n_trades']} trades, E[R] {_f(bs['expectancy_r'], 3, True)}, PF {_f(bs['profit_factor'], 2)}, win rate {_f(bs['win_rate'], 1, pct=True)}")
        L.append("")
    L += _table(["ID", "Rule", "Trades", "Kept", "E[R]", "Delta E[R]", "P(delta>0)", "PF", "Worst DD (vs base)", "Pairs not worse", "Half-years not worse", "Top-decile R ratio", "OOS delta (folds+)", "Verdict"], rows)

    null = next((c for c in results["candidates"].values() if c.get("null_benchmark")), None)
    if null:
        L += ["### Auto Select null benchmark (random choice among signalling pairs)", "",
              f"Production score E[R] {_f(null['production_score_expectancy_r'], 3, True)} | random choice, mean over {len(null['per_seed'])} seeds {_f(null['mean_expectancy_r'], 3, True)} (range across seeds {_f(null['spread_expectancy_r'], 3)}). "
              "If the production score does not beat random, its ranking adds nothing.", ""]
        L += _table(["Seed", "Trades", "E[R]", "Delta vs production score", "Max DD"], [[x["seed"], x["n_trades"], _f(x["expectancy_r"], 3, True), _f(x["delta_vs_production_score"], 3, True), _f(x["max_drawdown_pct"], 1) + "%"] for x in null["per_seed"]])

    L += ["### Parameter families: walk-forward selection and neighbour stability", ""]
    for fam, v in results["families"].items():
        wf = v["walk_forward"]
        ns = v["neighbour_stability"]
        L.append(f"**{fam}** members {', '.join(v['members'])} | selection on training windows, scored on unseen windows: selected E[R] {_f(v['selected_oos_r'], 3, True)} vs baseline {_f(v['baseline_oos_r'], 3, True)} "
                 f"(delta {_f(v['selected_oos_delta'], 3, True)}) | folds used {wf['n_folds_used']}/{wf['n_folds']} | distinct choices {wf['parameter_stability']['distinct_choices']} | neighbour check: {ns.get('verdict')}")
    L.append("")

    # --- S3
    L += ["## S3. Survivors", ""]
    if not results["survivors"]:
        L += ["**No candidate passed the pre-registered screen.** That is the result: on this evidence nothing here robustly improves the baseline.", ""]
    for cid in results["survivors"]:
        c = results["candidates"][cid]
        t = results["tiers"].get(cid, {})
        L += [f"### {cid}: {c['label']}", "", f"Tier: **{t.get('tier')}**" + ("".join(f" | {r}" for r in t.get("reasons", [])) if t.get("reasons") else ""), ""]
        st = results["stress"].get(cid, {})
        L += _table(["Costs", "Candidate E[R]", "Baseline E[R]", "Delta", "Candidate PF", "Baseline PF"],
                    [[f"{m}x", _f(v["candidate"]["expectancy_r"], 3, True), _f(v["baseline"]["expectancy_r"], 3, True), _f(None if v["candidate"]["expectancy_r"] is None or v["baseline"]["expectancy_r"] is None else v["candidate"]["expectancy_r"] - v["baseline"]["expectancy_r"], 3, True),
                      _f(v["candidate"]["profit_factor"], 2), _f(v["baseline"]["profit_factor"], 2)] for m, v in st.items()])
        h = results["holdout"].get(cid, {})
        if "per_pair" in h:
            L.append(f"Holdout pairs: pooled E[R] {_f(h['pooled_candidate_r'], 3, True)} vs baseline {_f(h['pooled_baseline_r'], 3, True)}; not worse on {h['pairs_not_worse']} of {len(h['per_pair'])} pairs; holdout check {'PASS' if h['pass'] else 'FAIL'}")
            L.append("")
            L += _table(["Pair", "Candidate n", "Candidate E[R]", "Baseline n", "Baseline E[R]", "Delta"], [[s, v["n_candidate"], _f(v["candidate_r"], 3, True), v["n_baseline"], _f(v["baseline_r"], 3, True), _f(v["delta"], 3, True)] for s, v in h["per_pair"].items()])
        d = results["dsr"].get(cid, {})
        L.append(f"Deflated Sharpe ratio {_f(d.get('dsr'), 3)} (annualised Sharpe {_f(d.get('sharpe_annualised'), 2)} against a luck benchmark of {_f(d.get('benchmark_annualised'), 2)} after {d.get('n_trials')} trials).")
        L.append("")
        cmp_ = c["compare"]
        L += _table(["Pair", "Candidate n", "Candidate E[R]", "Baseline n", "Baseline E[R]", "Delta"], [[s, v["n_candidate"], _f(v["candidate_r"], 3, True), v["n_baseline"], _f(v["baseline_r"], 3, True), _f(v["delta"], 3, True)] for s, v in cmp_["per_pair"].items()])
        L += _table(["Half-year", "Candidate n", "Candidate E[R]", "Baseline n", "Baseline E[R]", "Delta"], [[hy, v["n_candidate"], _f(v["candidate_r"], 3, True), v["n_baseline"], _f(v["baseline_r"], 3, True), _f(v["delta"], 3, True)] for hy, v in cmp_["half_years"].items()])
        L += _table(["Fold", "Test window", "Candidate n", "Candidate E[R]", "Baseline E[R]", "Delta"], [[r["fold"], f"{_d(r['test'][0])} to {_d(r['test'][1])}", r["n_candidate"], _f(r["candidate_r"], 3, True), _f(r["baseline_r"], 3, True), _f(r["delta"], 3, True)] for r in c["walk_forward"]["rows"]])

    L += ["## Ideas that failed the screen", ""]
    failed = [(cid, c) for cid, c in results["candidates"].items() if not c["passed_screen"] and not c.get("diagnostic")]
    if failed:
        L += _table(["ID", "Rule", "Failed checks"], [[cid, c["label"], ", ".join(CHECK_CODES.get(x, x) for x in c["failed_checks"]) or "no trades"] for cid, c in failed])
    else:
        L += ["None."] + [""]

    L += ["## Overfitting evidence", "",
          f"- {m['n_trials']} configurations were evaluated. Expect several to look better than the baseline by chance; the deflated Sharpe ratio and the out-of-sample folds are the guard.",
          "- A rule that improves the development period but not the walk-forward windows, or only one parameter value in a family, is treated as noise (see C9 and the neighbour check).",
          "- The hypotheses were formed after seeing the full-sample Phase 0 baseline, which included the lockbox months. Time-based out-of-sample evidence is therefore weaker than it looks; the cleanest remaining evidence is a forward shadow period.",
          "", "## Lockbox", "", "Not opened by this run. To open it once, for one chosen candidate: `python -m mba_bot.research phase1 lockbox --candidate ID --confirm --reason \"...\"`.", ""]
    return "\n".join(L)
