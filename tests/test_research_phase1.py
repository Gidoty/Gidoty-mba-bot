"""Phase 1: the registered plan, the statistics behind the survival screen, the descriptive studies, and the runner end to end
on a small synthetic store (so none of these numbers say anything about markets)."""
from __future__ import annotations

import dataclasses
import json
from collections import Counter

import pytest
from research_helpers import HOUR

from mba_bot.research import analysis, phase1, phase1_report, synthetic, tape, validation
from mba_bot.research.__main__ import main as cli
from mba_bot.research.engine import AccountParams, Policy, TradeRecord
from mba_bot.research.validation import Fold

DAY = 86_400_000


def tr(r, *, symbol="BTCUSDT", side="long", entry_ts=0, mfe=None, mae=None, hold=10.0, reason=None, features=None, confirmed=None, **kw):
    risk = 10.0
    net = r * risk
    return TradeRecord(
        symbol=symbol, side=side, entry_ts=entry_ts, exit_ts=entry_ts + int(hold * HOUR), signal_price=100.0, entry_price=100.0, exit_price=100.0, stop_price=95.0,
        quantity=1.0, notional=100.0, planned_risk=risk, gross_pnl=net, slippage_cost=0.0, fees=0.0, funding=0.0, net_pnl=net, r_net=r, r_gross=r,
        mfe_r=max(r, 0) if mfe is None else mfe, mae_r=max(-r, 0) if mae is None else mae, hold_hours=hold, exit_reason=reason or ("stop_loss" if r < 0 else "crossover_exit"),
        entry_signal="bullish_crossover", equity_before=1000.0, features=features or {}, signal_confirmed_at_close=confirmed, atr_pct=0.01, **kw,
    )


# ------------------------------------------------------------------ the registered plan


def test_plan_is_internally_consistent_and_hash_is_stable():
    ids = [c.id for c in phase1.PLAN]
    assert len(ids) == len(set(ids))
    assert phase1.plan_hash() == phase1.plan_hash()
    changed = tuple(dataclasses.replace(c, policy=dataclasses.replace(c.policy, confirm_polls=4)) if c.id == "H1-P3" else c for c in phase1.PLAN)
    assert phase1.plan_hash(plan=changed) != phase1.plan_hash()
    assert phase1.plan_hash(phase1.Phase1Options(train_days=300)) != phase1.plan_hash()


def test_every_candidate_changes_one_thing_and_families_vary_one_parameter():
    default = dataclasses.asdict(Policy())
    for c in phase1.PLAN:
        changed = {k for k, v in dataclasses.asdict(c.policy).items() if v != default[k]}
        assert changed, c.id
        assert len(changed) <= 2, f"{c.id} changes {changed}: experiments must be controlled"
    fams: dict[str, list] = {}
    for c in phase1.PLAN:
        fams.setdefault(c.family, []).append(c)
    for fam, members in fams.items():
        if len(members) > 1 and not fam.startswith(("diag", "H5")):
            diffs = set()
            for a in members[1:]:
                da, db = dataclasses.asdict(members[0].policy), dataclasses.asdict(a.policy)
                diffs |= {k for k in da if da[k] != db[k]}
            assert len(diffs) == 1, f"{fam} varies {diffs}"


def test_the_plan_covers_every_hypothesis_and_marks_diagnostics():
    hyp = Counter(c.hypothesis for c in phase1.PLAN)
    assert set(hyp) == {"H1", "H2", "H3", "H4", "H5"}
    assert {c.id for c in phase1.PLAN if c.diagnostic} == {"D-NOSHORT", "D-NOLONG", "H5-RND"}
    assert phase1.PLAN[0].policy.auto_score == "production"
    assert sum(max(1, len(c.seeds)) for c in phase1.PLAN) == 33


def test_the_registered_plan_hash_is_committed_in_the_preregistration_document():
    from pathlib import Path

    doc = (Path(__file__).parent.parent / "docs" / "research" / "phase1_preregistration.md").read_text()
    assert phase1.plan_hash() in doc, "the plan changed: update and re-commit the pre-registration BEFORE running it"


def test_cli_plan_prints_the_hash_and_a_wrong_expected_hash_refuses(capsys, tmp_path):
    assert cli(["phase1", "plan"]) == 0
    out = capsys.readouterr().out
    assert phase1.plan_hash() in out and "H4-T3" in out
    assert cli(["phase1", "run", "--root", str(tmp_path), "--out", str(tmp_path / "o"), "--expect-plan-hash", "deadbeef"]) == 3


# ------------------------------------------------------------------ statistics


def test_window_slicing_uses_entry_time_and_drops_the_forced_close():
    ts = [tr(1.0, entry_ts=5), tr(1.0, entry_ts=10), tr(1.0, entry_ts=15, reason="end_of_data"), tr(1.0, entry_ts=20)]
    assert [t.entry_ts for t in analysis.in_window(ts, 5, 20)] == [5, 10]


def test_bootstrap_difference_is_deterministic_and_has_the_right_sign():
    good, bad = [1.0, 2.0, 0.5, 1.5, 1.2, 0.8, 1.1, 0.9] * 4, [0.0, -0.5, 0.2, -0.1, 0.1, -0.3, 0.0, 0.1] * 4
    a, b = analysis.bootstrap_difference(good, bad), analysis.bootstrap_difference(good, bad)
    assert a == b and a["low"] > 0 and a["prob_positive"] > 0.99
    assert analysis.bootstrap_difference(bad, good)["prob_positive"] < 0.01
    assert analysis.bootstrap_difference([1, 2], [1, 2, 3, 4, 5, 6]) is None


def _set(n, mean, *, symbol="BTCUSDT", start=0, spacing=2 * DAY, spread=1.0):
    out = []
    for i in range(n):
        out.append(tr(mean + (spread if i % 2 else -spread), symbol=symbol, entry_ts=start + i * spacing))
    return out


def test_compare_passes_a_clear_improvement_and_names_what_fails_otherwise():
    screen = analysis.Screen()
    start = 1_700_000_000_000
    base = {s: _set(60, 0.05, symbol=s, start=start) for s in ("A", "B", "C", "D")}
    better = {s: _set(50, 0.45, symbol=s, start=start) for s in base}
    out = analysis.compare(better, base, {s: 10.0 for s in base}, {s: 10.0 for s in base}, screen)
    assert out["delta_r"] == pytest.approx(0.40, abs=0.01) and out["pairs_not_worse"] == 4
    assert all(out["checks"].values()), out["checks"]
    worse_dd = analysis.compare(better, base, {s: 25.0 for s in base}, {s: 10.0 for s in base}, screen)
    assert not worse_dd["checks"]["C6 drawdown not materially worse"]
    few = analysis.compare({s: better[s][:10] for s in base}, base, {s: 10.0 for s in base}, {s: 10.0 for s in base}, screen)
    assert not few["checks"]["C5 keeps enough trades"]
    flat = analysis.compare(base, base, {s: 10.0 for s in base}, {s: 10.0 for s in base}, screen)
    assert not flat["checks"]["C1 improves expectancy"] and not flat["checks"]["C2 improvement unlikely to be noise"]


def test_the_screen_catches_a_rule_that_wins_by_cutting_the_big_winners():
    screen = analysis.Screen()
    start = 1_700_000_000_000
    base = {"A": [tr(r, entry_ts=start + i * DAY) for i, r in enumerate([-1, -1, -1, 6, -1, -1, 5, -1, -1, 7] * 6)]}
    clipped = {"A": [tr(min(r, 1.5), entry_ts=start + i * DAY) for i, r in enumerate([-1, -1, -1, 6, -1, -1, 5, -1, -1, 7] * 6)]}
    out = analysis.compare(clipped, base, {"A": 5.0}, {"A": 5.0}, screen)
    assert out["top_decile_ratio"] < 0.8 and not out["checks"]["C8 large winners preserved"]


def test_out_of_sample_check_reads_the_test_windows_only():
    start = 1_700_000_000_000
    folds = [Fold(0, (start, start + 100 * DAY), (start + 101 * DAY, start + 160 * DAY)), Fold(1, (start, start + 160 * DAY), (start + 161 * DAY, start + 220 * DAY))]
    cand = _set(40, 1.0, start=start + 101 * DAY, spacing=DAY // 2) + _set(40, 5.0, start=start, spacing=DAY // 5)[:20]  # in-sample trades are great but ignored
    base = _set(40, 0.0, start=start + 101 * DAY, spacing=DAY // 2)
    ft = analysis.fold_table(cand, base, folds)
    assert ft["oos_delta"] == pytest.approx(1.0, abs=0.1) and ft["rows"][0]["n_candidate"] > 0
    cmp_ = {"checks": {}}
    analysis.apply_oos_checks(cmp_, ft, analysis.Screen())
    assert cmp_["checks"]["C9 improves out of sample"]
    bad = analysis.fold_table(base, cand, folds)
    cmp2 = {"checks": {}}
    analysis.apply_oos_checks(cmp2, bad, analysis.Screen())
    assert not cmp2["checks"]["C9 improves out of sample"]


def test_side_persistence_reports_the_gap_and_how_often_it_holds():
    start = 1_700_000_000_000
    longs = [tr(0.5 + (0.4 if i % 2 else -0.4), side="long", entry_ts=start + i * 3 * DAY) for i in range(60)]
    shorts = [tr(-0.3 + (0.4 if i % 2 else -0.4), side="short", entry_ts=start + i * 3 * DAY + 1) for i in range(60)]
    s = analysis.side_persistence({"BTCUSDT": longs + shorts})
    assert s["pooled"]["gap_long_minus_short"] == pytest.approx(0.8, abs=0.05)
    done, counted = s["half_years_long_better"]
    assert counted >= 1 and done == counted
    assert s["pooled"]["gap_bootstrap"]["low"] > 0
    assert "short / 30d down (<-10%)" in s["by_market_context"]


def test_volatility_adx_and_exit_studies_bucket_correctly():
    ts = [tr(1.0, features={"vol_pct_90d": 0.1, "adx14": 12}), tr(-1.0, features={"vol_pct_90d": 0.9, "adx14": 27}), tr(4.0, features={"vol_pct_90d": 0.85, "adx14": 35}, mfe=6.0, mae=0.4, hold=100)]
    v = analysis.volatility_study({"X": ts})
    assert [b["n_trades"] for b in v["pooled"]] == [1, 0, 0, 0, 2]
    a = analysis.adx_study({"X": ts})
    assert [b["n_trades"] for b in a["pooled"]] == [1, 0, 0, 1, 1, 0]
    e = analysis.exit_study({"X": ts})
    assert e["n_big_winners_ge_3r"] == 1 and e["share_of_total_positive_r_from_big_winners"] == pytest.approx(4.0 / 5.0)
    assert e["big_winner_share_with_mae_over_1r"] == 0.0 and e["reach_fractions"]["mfe_ge_5r"] == pytest.approx(1 / 3)


def test_filter_counterfactual_splits_removed_from_kept():
    ts = [tr(-1.0, features={"vol_pct_90d": 0.95}), tr(-1.0, features={"vol_pct_90d": 0.9}), tr(2.0, features={"vol_pct_90d": 0.3}), tr(1.0, features={"vol_pct_90d": 0.5})]
    out = analysis.filter_counterfactual(ts, lambda t: t.features["vol_pct_90d"] > 0.8)
    assert out["share_rejected"] == 0.5 and out["rejected"]["expectancy_r"] == -1.0 and out["kept"]["expectancy_r"] == 1.5


def test_cross_event_study_counts_flicker_persistence_and_confirmation():
    HOUR_POLLS = 12
    codes = []
    # hour 0: BULL from poll 2 through the close (confirmed, persisted 10)
    codes += [tape.CODE_NONE] * 2 + [tape.CODE_BULL] * 10
    # hour 1: BULL polls 3-5 then gone (flicker, not confirmed)
    codes += [tape.CODE_NONE] * 3 + [tape.CODE_BULL] * 3 + [tape.CODE_NONE] * 6
    # hour 2: nothing
    codes += [tape.CODE_NONE] * HOUR_POLLS
    t = tape.SignalTape("X", "replay", tape.array("b", codes), tape.array("d", [1.1] * len(codes)), tape.array("d", [1.0] * len(codes)), tape.array("d", [1.0] * len(codes)), tape.array("d", [0.01] * len(codes)))
    timeline = [i * 300_000 for i in range(len(codes))]
    out = analysis.cross_event_study(t, timeline, 0, len(codes))
    assert out["events"] == 2 and out["confirmed_share"] == 0.5
    assert [r["events"] for r in out["by_persistence"] if r["visible_at_least_polls"] == 6] == [1]
    assert next(r for r in out["by_persistence"] if r["visible_at_least_polls"] == 9)["confirmed_share"] == 1.0


def test_neighbour_stability_flags_an_isolated_peak():
    assert analysis.neighbour_stability([("a", 0.0), ("b", 0.2), ("c", 0.0)])["isolated_peak"]
    assert not analysis.neighbour_stability([("a", 0.0), ("b", 0.1), ("c", 0.2)])["isolated_peak"]
    assert analysis.neighbour_stability([("a", 0.1)])["verdict"].startswith("too few")


# ------------------------------------------------------------------ the runner, end to end on synthetic data


SMALL_IDS = {"H1-P3", "H1-M10", "H1-WC", "H2-V80", "H3-ST0", "H4-T4", "H4-B10", "H5-CALM", "H5-RND", "D-NOSHORT"}


@pytest.fixture(scope="module")
def small(tmp_path_factory):
    base = tmp_path_factory.mktemp("p1")
    root = base / "data"
    synthetic.write_store(root, pairs=["BTC/USDT", "ETH/USDT", "SOL/USDT"], days=150, seed=21)
    opts = phase1.Phase1Options(
        dev_pairs=("BTC/USDT", "ETH/USDT"), holdout_pairs=("SOL/USDT",), auto_pairs=("BTC/USDT", "ETH/USDT"), lockbox_months=1.0, lockbox_embargo_days=2,
        train_days=45, test_days=15, fold_embargo_days=1, account=AccountParams(allocated_capital=1000.0),
    )
    plan = tuple(c for c in phase1.PLAN if c.id in SMALL_IDS)
    return {"root": root, "out": base / "out", "opts": opts, "plan": plan}


def test_a_run_produces_the_whole_bundle_and_never_reads_the_lockbox(small):
    res = phase1.run_phase1(small["root"], small["out"], small["opts"], plan=small["plan"], skip_repro=True)
    m = res["meta"]
    assert m["partial"] and m["plan_hash"] != m["full_plan_hash"] and m["synthetic"]
    assert m["development_range"][1] < m["lockbox_range"][0]
    assert set(res["candidates"]) == SMALL_IDS
    assert m["n_trials"] == len(SMALL_IDS) - 1 + 5
    reg = validation.TrialRegistry(small["out"] / "trials.jsonl")
    assert reg.n_trials("phase1") >= len(SMALL_IDS), "every candidate (and every random seed) is on the ledger"
    assert reg.lockbox_openings("phase1_lockbox") == 0
    for cid, c in res["candidates"].items():
        if "compare" in c and "delta_r" in c["compare"]:
            assert c["compare"]["candidate"]["n_trades"] is not None
        if "walk_forward" in c:
            assert c["walk_forward"]["rows"]
    d = res["diagnostics"]
    assert d["sides_development"]["pooled"]["long"]["n_trades"] > 0 and d["sides_holdout"] and d["cross_event_flips"]
    assert d["baseline_vs_closed_candle"]["BTCUSDT"]["replay"]["n_trades"] > 0
    assert "ref-entries-not-valid-at-close" in d["filter_counterfactuals"] and "H1-P3" not in d["filter_counterfactuals"]
    null = res["candidates"]["H5-RND"]
    assert null["null_benchmark"] and len(null["per_seed"]) == 5 and not null["passed_screen"]
    assert res["candidates"]["D-NOSHORT"]["diagnostic"] and not res["candidates"]["D-NOSHORT"]["passed_screen"]
    md = phase1_report.render(res)
    assert md.startswith("> **SYNTHETIC DATA.**") and "PARTIAL PLAN" in md and "was not touched" in md and "S3. Survivors" in md
    json.dumps(res, default=str)


def test_survivors_get_cost_stress_holdout_and_a_deflated_sharpe(small):
    lenient = dataclasses.replace(
        small["opts"], screen=analysis.Screen(min_delta_r=-99, min_prob_positive=0.0, min_pairs_not_worse=0, min_halfyear_fraction=0.0, min_retention=0.0,
                                              max_drawdown_worsening_pts=999, min_top_decile_ratio=-99, min_oos_delta_r=-99, min_oos_fold_fraction=0.0, min_dsr=0.0)
    )
    res = phase1.run_phase1(small["root"], small["out"], lenient, plan=small["plan"], skip_repro=True)
    surv = res["survivors"]
    assert {"H1-P3", "H2-V80", "H5-CALM"} <= set(surv) and "D-NOSHORT" not in surv and "H5-RND" not in surv
    for cid in surv:
        assert set(res["stress"][cid]) == {"1.5", "2.0"}
        assert res["tiers"][cid]["tier"] in {"ROBUST", "SUGGESTIVE", "REJECTED_AFTER_STRESS"}
        assert "dsr" in res["dsr"][cid]
        if res["candidates"][cid]["scope"] == "fixed":
            assert "SOLUSDT" in res["holdout"][cid]["per_pair"]
    md = phase1_report.render(res)
    assert "Deflated Sharpe ratio" in md and "Holdout pairs" in md
    # a stricter screen rejects, and says so
    strict = dataclasses.replace(small["opts"], screen=analysis.Screen(min_delta_r=50.0))
    none = phase1.run_phase1(small["root"], small["out"], strict, plan=small["plan"], skip_repro=True)
    assert none["survivors"] == [] and "No candidate passed" in phase1_report.render(none)


def test_the_lockbox_opens_only_deliberately_once_and_never_for_a_diagnostic(small):
    opts, out = small["opts"], small["out"]
    with pytest.raises(validation.LockboxError):
        phase1.run_lockbox(small["root"], out, "H1-P3", opts, plan=small["plan"], confirm=False)
    with pytest.raises(ValueError):
        phase1.run_lockbox(small["root"], out, "D-NOSHORT", opts, plan=small["plan"], confirm=True)
    first = phase1.run_lockbox(small["root"], out, "H1-P3", opts, plan=small["plan"], confirm=True, reason="unit test")
    assert first["opening"]["opening_number"] == 1 and not first["opening"]["contaminated"]
    assert first["range"][0] > 0 and first["range"][1] > first["range"][0]
    assert set(first["per_pair"]) == {"BTCUSDT", "ETHUSDT", "SOLUSDT"} and first["pooled"]["candidate"]["n_trades"] is not None
    assert first["per_pair"]["SOLUSDT"]["role"] == "holdout"
    second = phase1.run_lockbox(small["root"], out, "H2-V80", opts, plan=small["plan"], confirm=True, reason="a second look")
    assert second["opening"]["contaminated"]
    auto = phase1.run_lockbox(small["root"], out, "H5-CALM", opts, plan=small["plan"], confirm=True, reason="auto")
    assert "auto" in auto and auto["opening"]["contaminated"]


def test_reproduction_check_flags_a_mismatch(tmp_path, small):
    lab = phase1.Lab(small["root"], tmp_path, small["opts"])
    lab.load("BTCUSDT")
    d = lab.run(["BTCUSDT"], lab.config(Policy(), "production_replay"))
    from mba_bot.research.metrics import trade_stats

    s = trade_stats(d.counted())
    good = {"runs": {"BTCUSDT|production_replay|x1.0": {"summary": {"n_trades": s["n_trades"], "expectancy_r": s["expectancy_r"]}}}}
    f = tmp_path / "baseline.json"
    f.write_text(json.dumps(good))
    rows = phase1._reproduction(f, {"BTCUSDT|production_replay": d})
    assert rows and rows[0]["identical"]
    good["runs"]["BTCUSDT|production_replay|x1.0"]["summary"]["expectancy_r"] += 0.001
    f.write_text(json.dumps(good))
    assert not phase1._reproduction(f, {"BTCUSDT|production_replay": d})[0]["identical"]


def test_the_run_refuses_a_dataset_other_than_the_one_the_baseline_used(small, tmp_path):
    with pytest.raises(RuntimeError, match="not the dataset the baseline was run on"):
        phase1.run_phase1(small["root"], tmp_path, small["opts"], plan=small["plan"][:1], skip_repro=True, expect_fingerprint="0000000000000000")
