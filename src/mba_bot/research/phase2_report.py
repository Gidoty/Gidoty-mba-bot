"""Markdown for the Phase 2 results. Reads only the results dict; computes nothing new."""
from __future__ import annotations

import datetime as dt


def _f(x, d=3, pct=False, plus=False):
    if x is None:
        return "n/a"
    if isinstance(x, bool):
        return "yes" if x else "no"
    v = x * 100 if pct else x
    return f"{v:+.{d}f}{'%' if pct else ''}" if plus else f"{v:.{d}f}{'%' if pct else ''}"


def _day(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d") if ms else "n/a"


def _tick(v):
    return "pass" if v is True else "FAIL" if v is False else "n/a"


def render(r: dict) -> str:
    m = r["meta"]
    out = [
        "# Phase 2 confirmation report", "",
        f"Plan hash `{m['plan_hash']}`, code fingerprint `{m['code_fingerprint']}`, code version `{m['code_version']}`, "
        f"{'REGISTERED run' if m['registered'] else 'UNREGISTERED run (tests only)'}{', SYNTHETIC data' if m['synthetic'] else ''}.",
        f"Phase 1 results pinned by SHA-256 `{m['phase1_results_sha256']}` (Phase 1 plan `{m['phase1_plan_hash']}`). Attempt {m['attempt']} on the older store (`{m['older_store_fingerprint']}`).",
        "The Phase 1 lockbox was not opened. No arm reaches production from this report alone: a forward shadow period is still required.", "",
        "## Segments", "",
    ]
    for name, seg in m["segments"].items():
        rng = seg["range"]
        out.append(f"- **{name}**: {', '.join(seg['pairs']) or 'no pairs'}"
                   f"{' (skipped, no usable history: ' + ', '.join(seg['missing']) + ')' if seg['missing'] else ''}"
                   f"; {_day(rng[0]) if rng else 'n/a'} to {_day(rng[1]) if rng else 'n/a'}")
    base = r["baseline"]["segment_A"]
    s = base["stats"]
    out += ["", "## Baseline on the older history (segment A)", "",
            f"{s.get('n_trades')} trades, net expectancy {_f(s.get('expectancy_r'), plus=True)} R, gross {_f(s.get('expectancy_r_gross'), plus=True)} R, "
            f"profit factor {_f(s.get('profit_factor'), 2)}, average cost {_f(s.get('avg_cost_r'))} R per trade.",
            f"Effective sample size: {_f((base.get('design_effect') or {}).get('effective_n'), 0)} of {s.get('n_trades')} trades (clustering in time). "
            f"Top 5% of trades carry {_f(base['robust'].get('top_5pct_share_of_positive_r'), 0, pct=True)} of the positive R.", "",
            "| regime | trades | mean R |", "|---|---|---|"]
    for n, v in base["by_regime"].items():
        out.append(f"| {n} | {v['n']} | {_f(v['mean_r'], plus=True)} |")
    out += ["", "## Primary arms (segment A, paired block bootstrap vs the baseline)", "",
            "| arm | verdict | delta R | 80% interval | Holm p | trades | Phase 1 delta | B delta | C delta |", "|---|---|---|---|---|---|---|---|---|"]
    for k, v in r["primary"].items():
        b = v.get("boot") or {}
        out.append(f"| {k} | **{v['judgement']['verdict']}** | {_f(v['delta_r'], plus=True)} | {_f(b.get('low'), plus=True)} to {_f(b.get('high'), plus=True)} | "
                   f"{_f(v.get('holm_p'), 3)} | {v['n_candidate']} vs {v['n_baseline']} | {_f(v.get('phase1_delta_r'), plus=True)} | "
                   f"{_f((v.get('segment_B') or {}).get('delta_r'), plus=True)} | {_f((v.get('segment_C') or {}).get('delta_r'), plus=True)} |")
    for k, v in r["primary"].items():
        out += ["", f"### {k}: {v['label']}", "", "| criterion | result | detail |", "|---|---|---|"]
        for name, ok in v["judgement"]["criteria"].items():
            out.append(f"| {name} | {_tick(ok)} | {v['judgement']['detail'][name]} |")
        be = v["break_even"]
        out.append("")
        out.append(f"Cost break-even multiple: arm {_f(be['candidate']['multiplier'], 2)}x, baseline {_f(be['baseline']['multiplier'], 2)}x. "
                   "Delta by cost multiple: " + ", ".join(f"{mm}x {_f(x['delta_r'], plus=True)}" for mm, x in v["costs"].items()) + ".")
        kr = v.get("kept_removed")
        if kr:
            out.append(f"Kept vs removed baseline trades: {kr['n_kept']} kept ({_f(kr['kept_mean'], plus=True)} R), {kr['n_removed']} removed ({_f(kr['removed_mean'], plus=True)} R), "
                       f"{kr['n_new']} new trades from path effects.")
    for k, v in r["sizing"].items():
        a = v["segment_A"]
        out += ["", f"## Sizing arm {k}: {v['label']}", "", "| criterion | result | detail |", "|---|---|---|"]
        for name, ok in v["criteria"].items():
            out.append(f"| {name} | {_tick(ok)} | {v['detail'][name]} |")
        out += ["", f"Verdict **{v['verdict']}**. Segment A: baseline mean return {_f(a['baseline']['mean_return_pct'], 1)}%, max drawdown {_f(a['baseline']['mean_max_dd_pct'], 1)}%; "
                    f"arm mean return {_f(a['arm']['mean_return_pct'], 1)}%, max drawdown {_f(a['arm']['mean_max_dd_pct'], 1)}%. "
                    f"Short trades: mean R {_f(a['short_mean_r_baseline'], plus=True)} (baseline)."]
    out += ["", "## Sensitivity and diagnostic arms (reported, never judged)", "", "| arm | delta R | trades | long mean R | short mean R |", "|---|---|---|---|---|"]
    for k, v in r["context"].items():
        out.append(f"| {k} | {_f(v['delta_r'], plus=True)} | {v['n_candidate']} | {_f(v['by_side']['long']['mean_r'], plus=True)} | {_f(v['by_side']['short']['mean_r'], plus=True)} |")
    au = r.get("auto")
    if au:
        o, ss, rd = au["oracle"], au["simultaneous_signals"], au["random"]
        out += ["", "## Auto Select (segment A, four pairs, one position)", "",
                f"Production ranking: {au['production']['n_trades']} trades, {_f(au['production']['mean_r'], plus=True)} R, return {_f(au['production']['return_pct'], 1)}%, max drawdown {_f(au['production']['max_dd_pct'], 1)}%.",
                f"Random ranking ({rd['seeds']} seeds): mean {_f(rd['mean_of_seeds'], plus=True)} R (sd {_f(rd['sd_of_seeds'])}), return {_f(rd['mean_return_pct'], 1)}%, max drawdown {_f(rd['mean_max_dd_pct'], 1)}%. "
                f"Production beats {_f(au['production_rank_among_random'], 0, pct=True)} of the random seeds; the {_f(0.9, 0, pct=True)} threshold is {_f(rd['percentile_threshold'], plus=True)} R. "
                f"Ranking adds value: **{_f(au['ranking_adds_value'])}**.",
                f"Simultaneous signals: {ss['with_two_or_more']} of {ss['decision_polls']} entry decisions had two or more signalling pairs ({_f(ss['share'], 1, pct=True)}).",
                f"Oracle bound: {o['decisions_resolved']} of {o['decisions_logged']} multi-candidate decisions resolved. "
                f"Chosen {_f(o.get('chosen_mean_r'), plus=True)} R, random expectation {_f(o.get('random_expected_mean_r'), plus=True)} R, best-of {_f(o.get('oracle_mean_r'), plus=True)} R; "
                f"headroom {_f(o.get('headroom_r'))} R per decision, captured {_f(o.get('captured_share'), 0, pct=True)}."]
    out += ["", "## Data-look ledger", "", "| when | dataset | kind | pairs | purpose |", "|---|---|---|---|---|"]
    for e in r.get("ledger", []):
        out.append(f"| {e['ts'][:19]} | {e['dataset']} | {e['kind']} | {len(e['pairs'])} | {e['purpose']} |")
    out += ["", "## Limits that apply to every number above", "",
            "- Survivor bias: only pairs that still trade today exist in the data; delisted contracts are absent.",
            "- Market structure, liquidity, fees, funding and contract specifications differed in the older years; today's exchange rules and the production cost model are applied to them.",
            "- Extreme-event slippage cannot be recovered from candles; the 3x cost multiple and the event-day exclusion are sensitivities, not a model of it.",
            "- The older history is one-shot evidence. It is consumed by this run."]
    return "\n".join(out) + "\n"
