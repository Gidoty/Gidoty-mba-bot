"""Phase 2: the frozen plan, its guards, the criteria, the Auto studies and one end-to-end run on SYNTHETIC stores.

No market data and no network. The end-to-end test builds two generated stores (an 'older' one ending where the 'Phase 1'
one begins) and a Phase 1 results file, runs the whole pipeline with test-sized options, and checks the one-shot rules."""
from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timedelta, timezone

import pytest

from mba_bot.research import confirm_stats as cs, data, phase1, phase2 as p2, synthetic
from mba_bot.research.engine import AccountParams, TradeRecord
from mba_bot.research.validation import LockboxError, LookLedger, RETROSPECTIVE_LOOKS

DAY = 86_400_000
T = 1_700_000_000_000


def trade(r, day, symbol="BTCUSDT", side="long", reason="stop", entry_ts=None):
    ts = entry_ts if entry_ts is not None else T + day * DAY
    return TradeRecord(symbol=symbol, side=side, entry_ts=ts, exit_ts=ts + DAY, signal_price=100, entry_price=100, exit_price=100, stop_price=99, quantity=1,
                       notional=100, planned_risk=1, gross_pnl=r, slippage_cost=0, fees=0, funding=0, net_pnl=r, r_net=r, r_gross=r, mfe_r=0, mae_r=0,
                       hold_hours=24, exit_reason=reason, entry_signal="x", equity_before=1000)


# ------------------------------------------------------------------ the frozen plan


def test_the_plan_contains_exactly_the_approved_arms_and_nothing_else():
    ids = [a.id for a in p2.ARMS]
    assert ids == ["BASE", "V67", "SM10", "XC", "COMBO", "SHORT_HALF", "V80", "NOSHORT"]
    roles = {a.id: a.role for a in p2.ARMS}
    assert roles["V80"] == "sensitivity" and roles["NOSHORT"] == "diagnostic" and roles["SHORT_HALF"] == "sizing"
    assert p2.PRIMARY == ("V67", "SM10", "XC", "COMBO")
    assert not any("cost" in a.id.lower() or "rank" in a.id.lower() for a in p2.ARMS)  # the cost-ranking Auto arm was dropped
    combo = p2.ARM_BY_ID["COMBO"].policy
    assert combo.vol_skip_above == 0.67 and combo.confirm_margin_atr == 0.10 and combo.confirm_sides == ("short",) and combo.exit_on_close_only
    assert p2.ARM_BY_ID["SHORT_HALF"].policy.short_risk_scale == 0.5 and p2.ARM_BY_ID["BASE"].policy == p2.ARM_BY_ID["BASE"].policy.__class__()
    assert len(p2.AUTO_SEEDS) == 20 and 3.0 in p2.COST_POINTS and 2.0 in p2.COST_POINTS


def test_plan_hash_is_stable_and_changes_with_any_arm_or_rule():
    base = p2.plan_hash()
    assert base == p2.plan_hash() and len(base) == 16
    tweaked = p2.Phase2Options(arms=tuple(a if a.id != "V67" else p2.Arm("V67", a.label, a.policy.__class__(vol_skip_above=0.70), a.role, a.hypothesis, a.phase1_id) for a in p2.ARMS))
    assert p2.plan_hash(tweaked) != base
    assert p2.plan_hash(p2.Phase2Options(criteria=p2.Criteria(holm_alpha=0.05))) != base
    assert p2.plan_hash(p2.Phase2Options(seeds=(1, 2, 3))) != base


def test_phase1_registration_is_untouched():
    assert phase1.plan_hash() == "6f29fad5f4cd6e33"


def test_code_fingerprint_covers_the_decisive_files():
    assert len(p2.code_fingerprint()) == 16
    assert {"engine.py", "confirm_stats.py", "phase2.py", "phase1.py"} <= set(p2.FINGERPRINTED_FILES)


def test_downloading_older_history_requires_the_registered_hash():
    with pytest.raises(RuntimeError, match="requires"):
        p2.guard_download(expect_plan_hash=None)
    with pytest.raises(RuntimeError, match="registered"):
        p2.guard_download(expect_plan_hash="0" * 16)
    p2.guard_download(expect_plan_hash=p2.plan_hash())


# ------------------------------------------------------------------ the Phase 1 reference


def _phase1_results(tmp_path, span, dev_end, lock_start, delta=0.05):
    res = {"meta": {"plan_hash": "6f29fad5f4cd6e33", "data_fingerprint": "abc", "span": list(span), "development_range": [span[0], dev_end], "lockbox_range": [lock_start, span[1]],
                    "dev_pairs": ["BTCUSDT"], "holdout_pairs": ["SOLUSDT"], "n_trials": 33},
           "candidates": {"H2-V67": {"compare": {"delta_r": delta, "candidate": {"expectancy_r": 0.2, "n_trades": 100}, "baseline": {"expectancy_r": 0.15, "n_trades": 150}, "per_pair": {"BTCUSDT": {"delta": 0.1}}}, "failed_checks": ["C2"]},
                          "H3-SM10": {"compare": {"delta_r": 0.04}}, "H4-XC": {"compare": {"delta_r": 0.03}}}}
    path = tmp_path / "phase1_results.json"
    path.write_text(json.dumps(res))
    return path


def test_the_phase1_reference_is_read_from_the_file_and_pinned_by_hash(tmp_path):
    path = _phase1_results(tmp_path, (T, T + 400 * DAY), T + 300 * DAY, T + 310 * DAY)
    ref = p2.load_phase1_reference(path, expect_sha256=p2.file_sha256(path))
    assert ref["deltas"]["H2-V67"]["delta_r"] == 0.05 and ref["deltas"]["H2-V67"]["failed_checks"] == ["C2"] and ref["n_trials"] == 33
    with pytest.raises(RuntimeError, match="refusing"):
        p2.load_phase1_reference(path, expect_sha256="0" * 64)


# ------------------------------------------------------------------ the one-shot rules


def test_the_older_store_can_be_rerun_under_the_same_plan_but_not_a_different_one(tmp_path):
    first = p2.claim_older_store(tmp_path, plan="aaa", fingerprint="f1")
    assert first["attempts"] == 1
    assert p2.claim_older_store(tmp_path, plan="aaa", fingerprint="f1")["attempts"] == 2  # a crashed run may be repeated: same test
    with pytest.raises(LockboxError, match="no longer unseen"):
        p2.claim_older_store(tmp_path, plan="bbb", fingerprint="f1")
    with pytest.raises(LockboxError):
        p2.claim_older_store(tmp_path, plan="aaa", fingerprint="f2")


def test_look_ledger_is_append_only_records_history_and_blocks_a_second_confirmation(tmp_path):
    led = p2.new_ledger(tmp_path / "l.jsonl")
    kinds = [e["kind"] for e in led.entries()]
    assert len(kinds) == len(RETROSPECTIVE_LOOKS) and "design" in kinds and "descriptive" in kinds
    sealed = [e for e in led.entries() if e["dataset"] == "phase1_lockbox"][0]
    assert sealed["note"] == "sealed"
    assert p2.new_ledger(tmp_path / "l.jsonl").entries() == led.entries()  # not re-seeded
    led.assert_unconsumed("older_history")
    led.record(dataset="older_history", pairs=["BTC/USDT"], period="x", kind="confirmation", purpose="t")
    with pytest.raises(LockboxError):
        led.assert_unconsumed("older_history")
    with pytest.raises(ValueError):
        led.record(dataset="d", pairs=[], period="x", kind="peek", purpose="t")


# ------------------------------------------------------------------ the criteria (hand-built evidence)


def _res(**over):
    res = {
        "delta_r": 0.06, "candidate_r": 0.2, "baseline_r": 0.14, "phase1_delta_r": 0.08, "retention": {"big_winner_ratio": 0.95, "top_decile_ratio": 0.97},
        "worst_dd_candidate": 20.0, "worst_dd_baseline": 22.0,
        "per_pair": {s: {"delta_r": 0.05} for s in ("BTC", "ETH", "BNB", "XRP")},
        "segment_B": {"delta_r": 0.04, "n_baseline": 300}, "segment_C": {"delta_r": 0.03, "n_baseline": 400},
        "regimes": {"bull": {"n_baseline": 100, "delta_r": 0.05}, "bear": {"n_baseline": 80, "delta_r": 0.02}, "range": {"n_baseline": 90, "delta_r": 0.01}},
        "costs": {"2.0": {"delta_r": 0.04}}, "event_days_excluded": {"delta_r": 0.05},
    }
    res.update(over)
    return res


def test_an_arm_that_meets_every_criterion_passes():
    j = p2.judge_primary(_res(), p2.CRITERIA, holm_p=0.04)
    assert j["verdict"] == "PASS" and not j["failed"] and len(j["criteria"]) == 11


@pytest.mark.parametrize("over,holm,failed_prefix,verdict", [
    ({"delta_r": -0.01}, 0.04, "K1", "FAIL"),
    ({}, 0.30, "K2", "INCONCLUSIVE"),
    ({"delta_r": 0.02}, 0.04, "K3", "FAIL"),
    ({"retention": {"big_winner_ratio": 0.7, "top_decile_ratio": 0.97}}, 0.04, "K4", "INCONCLUSIVE"),
    ({"worst_dd_candidate": 30.0}, 0.04, "K5", "INCONCLUSIVE"),
    ({"per_pair": {s: {"delta_r": (0.05 if i < 2 else -0.01)} for i, s in enumerate(("BTC", "ETH", "BNB", "XRP"))}}, 0.04, "K6", "INCONCLUSIVE"),
    ({"segment_B": {"delta_r": -0.02, "n_baseline": 300}}, 0.04, "K7", "FAIL"),
    ({"regimes": {"bull": {"n_baseline": 100, "delta_r": 0.2}, "bear": {"n_baseline": 80, "delta_r": -0.3}, "range": {"n_baseline": 90, "delta_r": 0.1}}}, 0.04, "K8", "INCONCLUSIVE"),
    ({"costs": {"2.0": {"delta_r": -0.01}}}, 0.04, "K9", "INCONCLUSIVE"),
    ({"event_days_excluded": {"delta_r": -0.02}}, 0.04, "K10", "INCONCLUSIVE"),
    ({"candidate_r": -0.01}, 0.04, "K0", "INCONCLUSIVE"),
])
def test_each_criterion_can_fail_and_the_verdict_rule_is_fixed(over, holm, failed_prefix, verdict):
    j = p2.judge_primary(_res(**over), p2.CRITERIA, holm_p=holm)
    assert any(n.startswith(failed_prefix + " ") for n in j["failed"]), j["failed"]
    assert j["verdict"] == verdict


def test_small_segments_and_missing_references_are_not_applicable_rather_than_passing_silently():
    j = p2.judge_primary(_res(segment_C={"delta_r": -0.5, "n_baseline": 10}, phase1_delta_r=None), p2.CRITERIA, holm_p=0.04)
    assert j["criteria"]["K3 keeps a fair share of the Phase 1 improvement"] is None
    assert j["criteria"]["K7 replicates on the six added pairs (segments B, C)"] is True  # C ignored: fewer than 100 baseline trades
    assert "not judged" in j["detail"]["K7 replicates on the six added pairs (segments B, C)"]


# ------------------------------------------------------------------ Auto studies


def test_oracle_study_compares_the_choice_with_random_and_with_the_best_pair():
    fixed = {"BTCUSDT": [trade(1.0, 0, "BTCUSDT", entry_ts=1000), trade(-1.0, 1, "BTCUSDT", entry_ts=2000)],
             "ETHUSDT": [trade(3.0, 0, "ETHUSDT", entry_ts=1000), trade(0.0, 1, "ETHUSDT", entry_ts=2000)]}
    decisions = [{"ts": 1000, "chosen": "BTCUSDT", "candidates": ["BTCUSDT", "ETHUSDT"], "entered": True},
                 {"ts": 2000, "chosen": "ETHUSDT", "candidates": ["BTCUSDT", "ETHUSDT"], "entered": True},
                 {"ts": 3000, "chosen": "ETHUSDT", "candidates": ["BTCUSDT", "ETHUSDT"], "entered": True},  # no counterfactual: unresolved
                 {"ts": 4000, "chosen": "ETHUSDT", "candidates": ["BTCUSDT", "ETHUSDT"], "entered": False}]
    out = p2.oracle_study(decisions, fixed)
    assert out["decisions_logged"] == 4 and out["decisions_resolved"] == 2
    assert out["chosen_mean_r"] == pytest.approx((1.0 + 0.0) / 2) and out["random_expected_mean_r"] == pytest.approx((2.0 + -0.5) / 2)
    assert out["oracle_mean_r"] == pytest.approx((3.0 + 0.0) / 2) and out["headroom_r"] == pytest.approx(1.5 - 0.75)
    assert out["captured_share"] == pytest.approx((0.5 - 0.75) / 0.75)  # chose worse than random here


def test_candidate_set_frequencies_count_the_pairs_that_signalled_together():
    d = [{"candidates": ["BTCUSDT", "ETHUSDT"]}, {"candidates": ["ETHUSDT", "BTCUSDT"]}, {"candidates": ["BNBUSDT", "XRPUSDT", "ETHUSDT"]}]
    assert p2.candidate_set_frequencies(d) == {"BTCUSDT+ETHUSDT": 2, "BNBUSDT+ETHUSDT+XRPUSDT": 1}


# ------------------------------------------------------------------ the range downloader (fake exchange)


class _Fake:
    def __init__(self, tmp_path):
        self.src = tmp_path / "src"
        synthetic.write_store(self.src, pairs=["BTC/USDT"], days=20, seed=3, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.urls = []

    def __call__(self, url, timeout):
        self.urls.append(url)
        parsed = urllib.parse.urlparse(url)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        import csv
        if parsed.path == "/fapi/v1/exchangeInfo":
            return 200, {}, json.dumps(synthetic.exchange_info_for(["BTCUSDT"])).encode()
        if parsed.path == "/fapi/v1/klines":
            step = data.INTERVAL_MS[q["interval"]]
            out = []
            with open(data.kline_path(self.src, q["interval"], q["symbol"])) as fh:
                for r in csv.DictReader(fh):
                    ts = int(r["ts"])
                    if ts >= int(q["startTime"]) and ("endTime" not in q or ts <= int(q["endTime"])):
                        out.append([ts, r["open"], r["high"], r["low"], r["close"], r.get("volume", "0"), ts + step - 1, r.get("quote_volume", "0"), 0, "0", "0", "0"])
                    if len(out) >= int(q["limit"]):
                        break
            return 200, {}, json.dumps(out).encode()
        if parsed.path == "/fapi/v1/fundingRate":
            out = []
            with open(data.funding_path(self.src, q["symbol"])) as fh:
                for r in csv.DictReader(fh):
                    if int(r["ts"]) >= int(q["startTime"]) and ("endTime" not in q or int(r["ts"]) <= int(q["endTime"])):
                        out.append({"symbol": q["symbol"], "fundingTime": int(r["ts"]), "fundingRate": r["rate"], "markPrice": r["mark_price"]})
            return 200, {}, json.dumps(out).encode()
        return 404, {}, b"{}"


def test_range_download_stays_inside_its_window_and_only_calls_public_market_data(tmp_path):
    fake = _Fake(tmp_path)
    since, until = datetime(2025, 12, 15, tzinfo=timezone.utc), datetime(2025, 12, 20, tzinfo=timezone.utc)
    out = data.download_range(tmp_path / "old", pairs=["BTC/USDT"], since=since, until=until, intervals=("1h", "5m"), http=data.PublicHTTP(opener=fake, sleep=lambda s: None, min_interval=0))
    assert out["new_rows"]["klines/1h/BTCUSDT"] == 5 * 24 and out["new_rows"]["klines/5m/BTCUSDT"] == 5 * 288
    hourly = data.load_candles(tmp_path / "old", "BTCUSDT", "1h")
    assert hourly[0].timestamp == int(since.timestamp() * 1000) and hourly[-1].timestamp == int(until.timestamp() * 1000) - 3_600_000
    funding = data.load_funding(tmp_path / "old", "BTCUSDT")
    assert funding and all(int(since.timestamp() * 1000) <= ts < int(until.timestamp() * 1000) for ts, _ in funding)
    paths = {urllib.parse.urlparse(u).path for u in fake.urls}
    assert paths <= {"/fapi/v1/exchangeInfo", "/fapi/v1/klines", "/fapi/v1/fundingRate"}
    with pytest.raises(data.DownloadError):
        data.download_range(tmp_path / "x", pairs=["BTC/USDT"], since=until, until=since, http=data.PublicHTTP(opener=fake, sleep=lambda s: None, min_interval=0))


# ------------------------------------------------------------------ end to end on synthetic stores


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    root = tmp_path_factory.mktemp("p2")
    p1_end = datetime(2026, 1, 1, tzinfo=timezone.utc)
    days = 170
    p1_root, old_root = root / "p1", root / "old"
    synthetic.write_store(p1_root, pairs=["BTC/USDT", "ETH/USDT", "SOL/USDT"], days=days, seed=21, now=p1_end)
    p1_first = int((p1_end - timedelta(days=days)).timestamp() * 1000)
    synthetic.write_store(old_root, pairs=["BTC/USDT", "ETH/USDT", "SOL/USDT"], days=days, seed=22, now=p1_end - timedelta(days=days))
    last = int(p1_end.timestamp() * 1000)
    dev_end = last - 50 * DAY
    results = _phase1_results(root, (p1_first, last), dev_end, dev_end + 3 * DAY)
    opts = p2.Phase2Options(a_pairs=("BTC/USDT", "ETH/USDT"), b_pairs=("SOL/USDT",), c_pairs=("SOL/USDT",), seeds=(1, 2), cost_points=(2.0, 3.0),
                            criteria=p2.Criteria(bootstrap_iterations=200))
    out = root / "out"
    res = p2.run_phase2(old_root, p1_root, results, out, opts, expect_plan_hash=None, expect_code_fingerprint=None, expect_phase1_sha256=None, registered=False)
    return {"root": root, "old": old_root, "p1": p1_root, "results_path": results, "opts": opts, "out": out, "res": res, "p1_first": p1_first, "dev_end": dev_end, "last": last}


def test_the_pipeline_produces_every_deliverable(world):
    res, out = world["res"], world["out"]
    assert (out / "phase2_results.json").exists() and (out / "phase2_report.md").exists() and (out / "phase2_trades.csv").exists()
    assert set(res["primary"]) == set(p2.PRIMARY) and set(res["sizing"]) == {"SHORT_HALF"} and set(res["context"]) == {"V80", "NOSHORT"}
    assert set(res["verdicts"].values()) <= {"PASS", "FAIL", "INCONCLUSIVE"}
    for v in res["primary"].values():
        assert v["holm_p"] is None or 0 <= v["holm_p"] <= 1
        assert len(v["judgement"]["criteria"]) == 11
    au = res["auto"]
    assert au["random"]["seeds"] == 2 and au["simultaneous_signals"]["decision_polls"] >= 0 and "oracle" in au
    report = (out / "phase2_report.md").read_text()
    assert "Survivor bias" in report and "UNREGISTERED" in report and "lockbox was not opened" in report


def test_segments_never_reach_into_the_lockbox_or_the_other_store(world):
    res = world["res"]
    seg = res["meta"]["segments"]
    assert seg["A"]["range"][1] <= world["p1_first"] and seg["C"]["range"][1] <= world["p1_first"]
    assert seg["B"]["range"] == [world["p1_first"], world["dev_end"]] and res["meta"]["lockbox_untouched"]
    rows = [l.split(",") for l in (world["out"] / "phase2_trades.csv").read_text().splitlines()[1:]]
    assert rows
    for name, _arm, _sym, _side, entry, exit_, *_ in rows:
        if name == "B":
            assert int(exit_) <= world["dev_end"] + 1  # closed inside the development window (or at its forced end)
        else:
            assert int(entry) < world["p1_first"]


def test_one_shot_rules_hold_after_the_run(world):
    assert (world["old"] / p2.MARKER).exists()
    led = LookLedger(world["out"] / "look_ledger.jsonl")
    assert [e["dataset"] for e in led.entries() if e["kind"] == "confirmation"][:2] == ["older_history", "phase1_store_extra_pairs"]
    again = p2.run_phase2(world["old"], world["p1"], world["results_path"], world["out"], world["opts"], expect_plan_hash=None, expect_code_fingerprint=None, expect_phase1_sha256=None, registered=False)
    assert again["meta"]["attempt"] == 2 and again["verdicts"] == world["res"]["verdicts"]  # the same frozen test gives the same answer
    other = p2.Phase2Options(a_pairs=world["opts"].a_pairs, b_pairs=world["opts"].b_pairs, c_pairs=world["opts"].c_pairs, seeds=(1, 2, 3), cost_points=(2.0, 3.0), criteria=world["opts"].criteria)
    with pytest.raises(LockboxError, match="no longer unseen"):
        p2.run_phase2(world["old"], world["p1"], world["results_path"], world["out"], other, expect_plan_hash=None, expect_code_fingerprint=None, expect_phase1_sha256=None, registered=False)


def test_a_registered_run_needs_all_three_pins_and_they_must_match(world, tmp_path):
    args = dict(old_root=world["old"], phase1_root=world["p1"], phase1_results=world["results_path"], out_dir=tmp_path / "o")
    with pytest.raises(RuntimeError, match="all required"):
        p2.run_phase2(**args, expect_plan_hash=p2.plan_hash(), expect_code_fingerprint=None, expect_phase1_sha256=None)
    with pytest.raises(RuntimeError, match="plan hash"):
        p2.run_phase2(**args, expect_plan_hash="0" * 16, expect_code_fingerprint=p2.code_fingerprint(), expect_phase1_sha256=p2.file_sha256(world["results_path"]))
    with pytest.raises(RuntimeError, match="fingerprint"):
        p2.run_phase2(**args, expect_plan_hash=p2.plan_hash(), expect_code_fingerprint="0" * 16, expect_phase1_sha256=p2.file_sha256(world["results_path"]))
    with pytest.raises(RuntimeError, match="refusing"):
        p2.run_phase2(**args, expect_plan_hash=p2.plan_hash(), expect_code_fingerprint=p2.code_fingerprint(), expect_phase1_sha256="0" * 64)
    assert not (tmp_path / "o" / "phase2_results.json").exists()


def test_the_baseline_arm_in_phase2_is_the_production_engine(world):
    """BASE must equal what the Phase 0 engine produces for the same window: Phase 2 adds no new execution path."""
    from mba_bot.research.engine import MODE_REPLAY, BacktestConfig, Policy
    lab = phase1.Lab(world["old"], world["root"] / "chk", phase1.Phase1Options())
    a0, a1 = world["res"]["meta"]["segments"]["A"]["range"]
    d = lab.run(["BTCUSDT"], BacktestConfig(mode=MODE_REPLAY, account=AccountParams(allocated_capital=1000.0), policy=Policy(), start_ts=a0, end_ts=a1))
    row = world["res"]["baseline"]["segment_A"]["per_pair"]["BTCUSDT"]
    assert row["n_trades"] == len(d.counted())
    assert cs.RegimeIndex  # imported for the regime annotation used above


def test_a_corrupt_older_store_stops_the_run_before_any_analysis(world, tmp_path):
    import shutil

    bad = tmp_path / "old"
    shutil.copytree(world["old"], bad)
    (bad / p2.MARKER).unlink()
    path = data.kline_path(bad, "1h", "BTCUSDT")
    lines = path.read_text().splitlines()
    for i in range(10, 400):  # break the OHLC relation on many rows
        f = lines[i].split(",")
        f[2], f[3] = f[3], f[2]
        f[2] = str(float(f[3]) * 0.5)
        lines[i] = ",".join(f)
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(RuntimeError, match="quality gate"):
        p2.run_phase2(bad, world["p1"], world["results_path"], tmp_path / "o", world["opts"], expect_plan_hash=None, expect_code_fingerprint=None, expect_phase1_sha256=None, registered=False)
    assert not (tmp_path / "o" / "phase2_results.json").exists()
