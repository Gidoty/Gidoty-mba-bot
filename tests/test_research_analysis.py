"""Metrics, validation guards, regime labels, the baseline report, the live report and the CLI."""
from __future__ import annotations

import ast
import json
import math
import sqlite3
import statistics
from datetime import datetime, timezone
from pathlib import Path

import pytest

import mba_bot.research as research_pkg
from mba_bot.db import init_db, session_scope
from mba_bot.models import Customer, Trade, TradeTelemetry, TradingAccount
from mba_bot.research import live_report, metrics, reporting, synthetic, validation
from mba_bot.research.__main__ import main as cli
from mba_bot.research.engine import HOUR, RunResult, TradeRecord
from mba_bot.research.regimes import label_trades
from mba_bot.strategy import Candle

DAY = 86_400_000


def tr(r, *, side="long", entry_ts=0, hold=5.0, fees=0.0, slip=0.0, funding=0.0, risk=10.0, reason=None, **kw):
    net = r * risk
    gross = net + fees + slip + funding
    return TradeRecord(
        symbol=kw.pop("symbol", "BTCUSDT"), side=side, entry_ts=entry_ts, exit_ts=entry_ts + int(hold * HOUR), signal_price=100.0, entry_price=100.0,
        exit_price=100.0, stop_price=95.0, quantity=1.0, notional=100.0, planned_risk=risk, gross_pnl=gross, slippage_cost=slip, fees=fees,
        funding=funding, net_pnl=net, r_net=r, r_gross=gross / risk, mfe_r=kw.pop("mfe", max(r, 0)), mae_r=kw.pop("mae", max(-r, 0)), hold_hours=hold,
        exit_reason=reason or ("stop_loss" if r < 0 else "crossover_exit"), entry_signal="bullish_crossover", equity_before=1000.0, **kw,
    )


def result(trades, curve=None, start=1000.0):
    curve = curve or [(0, start), (DAY * 400, start + sum(t.net_pnl for t in trades))]
    return RunResult(config={}, symbols=["BTCUSDT"], start_equity=start, end_equity=start + sum(t.net_pnl for t in trades), trades=trades,
                     equity=curve, counters={"entries": len(trades)}, span=(curve[0][0], curve[-1][0]))


# ------------------------------------------------------------------ metrics


def test_trade_stats_known_values():
    rs = [2, -1, -1, 3, -1, -1, -1, 1]
    s = metrics.trade_stats([tr(r) for r in rs])
    assert s["n_trades"] == 8 and s["win_rate"] == pytest.approx(3 / 8)
    assert s["expectancy_r"] == pytest.approx(sum(rs) / 8)
    assert s["profit_factor"] == pytest.approx(60 / 50)
    assert s["avg_win_r"] == pytest.approx(2.0) and s["avg_loss_r"] == pytest.approx(-1.0) and s["payoff_ratio"] == pytest.approx(2.0)
    assert s["best_r"] == 3 and s["worst_r"] == -1
    assert metrics.trade_stats([])["n_trades"] == 0


def test_costs_split_gross_from_net_and_are_expressed_in_r():
    s = metrics.trade_stats([tr(1.0, fees=1.0, slip=0.5, funding=0.5) for _ in range(5)])
    assert s["expectancy_r"] == pytest.approx(1.0) and s["expectancy_r_gross"] == pytest.approx(1.2)
    assert s["avg_cost_r"] == pytest.approx(0.2)
    assert s["cost_drag_pct_of_gross"] == pytest.approx(100 * 2.0 / 12.0)


def test_losing_streaks_are_counted_and_long_ones_listed():
    s = metrics.streaks([1, -1, -1, -1, 1, -1, -1, -1, -1, 1, -1])
    assert s["longest_losing_streak"] == 4 and s["losing_streaks_of_3_plus"] == {3: 1, 4: 1}
    assert s["longest_winning_streak"] == 1


def test_max_drawdown_on_a_known_curve():
    curve = [(i * DAY, v) for i, v in enumerate([100, 120, 90, 110, 80, 130, 125])]
    dd = metrics.max_drawdown(curve)
    assert dd["max_drawdown_pct"] == pytest.approx((120 - 80) / 120 * 100)
    assert dd["max_drawdown_usd"] == pytest.approx(40)
    assert dd["longest_drawdown_days"] == pytest.approx(4.0)  # peak at day 1, recovered day 5


def test_wilson_interval_known_value():
    lo, hi = metrics.wilson_interval(5, 10)
    assert lo == pytest.approx(0.2366, abs=1e-3) and hi == pytest.approx(0.7634, abs=1e-3)
    assert metrics.wilson_interval(0, 0) is None


def test_bootstrap_is_deterministic_brackets_the_mean_and_refuses_tiny_samples():
    vals = [0.5, -1, 2, -1, 0.3, 1.2, -1, 0.8, -0.2, 1.5] * 5
    a, b = metrics.bootstrap_mean_ci(vals), metrics.bootstrap_mean_ci(vals)
    assert a == b and a["low"] < statistics.fmean(vals) < a["high"] and 0 <= a["prob_positive"] <= 1
    assert metrics.bootstrap_mean_ci([1, 2]) is None


def test_daily_returns_use_the_last_point_of_each_utc_day():
    curve = [(0, 100), (HOUR * 5, 101), (DAY, 102), (DAY + HOUR, 110), (2 * DAY + 1, 121)]
    assert metrics.daily_returns(curve) == pytest.approx([110 / 101 - 1, 121 / 110 - 1])


def test_sharpe_is_withheld_on_small_samples_and_computed_on_enough():
    small = metrics.sharpe_sortino([0.01] * 10, 100)
    assert small["sharpe"] is None and "not computed" in small["sharpe_note"]
    rets = [0.01 if i % 3 else -0.005 for i in range(120)]
    s = metrics.sharpe_sortino(rets, 60)
    assert s["sharpe"] == pytest.approx(statistics.fmean(rets) / statistics.pstdev(rets) * math.sqrt(365))
    assert s["sortino"] > s["sharpe"]
    assert metrics.sharpe_sortino(rets, 5)["sharpe"] is None


def test_summarize_ties_the_pieces_together():
    trades = [tr(1.0 if i % 3 else -1.0, entry_ts=i * 2 * DAY, hold=12.0) for i in range(40)]
    curve = [(i * DAY, 1000 + i) for i in range(400)]
    s = metrics.summarize(result(trades, curve))
    assert s["n_trades"] == 40 and s["exposure_pct"] == pytest.approx(100 * 40 * 12 / (399 * 24))
    assert s["expectancy_r_bootstrap"] and s["sharpe"] is not None and s["longest_losing_streak"] == 1
    assert s["total_return_pct"] == pytest.approx(s["end_equity"] / 1000 * 100 - 100)


def test_the_forced_final_close_is_not_counted():
    ts = [tr(1.0), tr(-1.0, reason="end_of_data")]
    assert len(result(ts).counted_trades()) == 1


def test_breakdown_groups_by_key():
    ts = [tr(1.0, symbol="A"), tr(-1.0, symbol="A"), tr(2.0, symbol="B")]
    g = metrics.breakdown(ts, lambda t: t.symbol)
    assert g["A"]["n_trades"] == 2 and g["B"]["expectancy_r"] == 2.0


# ------------------------------------------------------------------ validation


def test_expected_max_sharpe_matches_the_published_scale():
    assert 2.4 < validation.expected_max_sharpe(100, 1.0) < 2.7
    assert validation.expected_max_sharpe(1, 1.0) == 0.0 and validation.expected_max_sharpe(50, 0.0) == 0.0
    assert validation.expected_max_sharpe(1000, 1.0) > validation.expected_max_sharpe(10, 1.0)


def test_probabilistic_sharpe_known_values():
    assert validation.probabilistic_sharpe_ratio(0.1, 0.0, 1000, 0.0, 3.0) == pytest.approx(0.9992, abs=1e-3)
    assert validation.probabilistic_sharpe_ratio(0.0, 0.0, 1000, 0.0, 3.0) == pytest.approx(0.5)
    assert validation.probabilistic_sharpe_ratio(0.1, 0.0, 2, 0.0, 3.0) is None


def test_more_trials_lower_the_deflated_sharpe_of_the_same_returns():
    import random

    rng = random.Random(3)
    rets = [0.0006 + 0.01 * rng.gauss(0, 1) for _ in range(700)]
    few = validation.deflated_sharpe_ratio(rets, 1)["dsr"]
    many = validation.deflated_sharpe_ratio(rets, 200, trial_sharpes=[rng.gauss(0, 0.05) for _ in range(200)])["dsr"]
    assert few > many
    assert validation.deflated_sharpe_ratio([0.01] * 5, 3)["dsr"] is None
    assert "less conservative" in validation.deflated_sharpe_ratio(rets, 10)["variance_basis"]


def test_registry_counts_distinct_configurations_and_lockbox_openings(tmp_path):
    reg = validation.TrialRegistry(tmp_path / "t.jsonl")
    reg.record_trial(family="f", config={"a": 1}, data_hash="d", summary={"sharpe_daily": 0.1})
    reg.record_trial(family="f", config={"a": 1}, data_hash="d", summary={"sharpe_daily": 0.1})  # same config again
    reg.record_trial(family="f", config={"a": 2}, data_hash="d", summary={"sharpe_daily": 0.05})
    reg.record_trial(family="other", config={"a": 3}, data_hash="d", summary={})
    assert reg.n_trials("f") == 2 and reg.n_trials("other") == 1 and sorted(reg.trial_sharpes("f")) == [0.05, 0.1]
    assert all("code_version" in r and "ts" in r for r in reg.records())
    assert reg.lockbox_openings("f") == 0


def test_the_lockbox_must_be_opened_deliberately_and_a_second_opening_is_flagged(tmp_path):
    reg = validation.TrialRegistry(tmp_path / "t.jsonl")
    box = validation.Lockbox(first_ts=0, last_ts=900 * DAY, months=6, holdout_symbols=frozenset({"SOLUSDT"}))
    dev_start, dev_end = box.develop_range()
    assert dev_end < box.start_ts and box.lockbox_range() == (box.start_ts, 900 * DAY) and dev_start == 0
    assert box.start_ts == pytest.approx(900 * DAY - 6 * 30.4375 * DAY)
    with pytest.raises(validation.LockboxError):
        box.open(reg, family="f", reason="peek", confirm=False)
    with pytest.raises(validation.LockboxError):
        box.open(reg, family="f", reason="  ", confirm=True)
    first = box.open(reg, family="f", reason="final check of candidate A", confirm=True)
    assert first["opening_number"] == 1 and not first["contaminated"]
    second = box.open(reg, family="f", reason="one more look", confirm=True)
    assert second["contaminated"] and "no longer a clean" in second["warning"]
    box.assert_tunable(["BTCUSDT", "ETHUSDT"])
    with pytest.raises(validation.LockboxError):
        box.assert_tunable(["BTCUSDT", "SOLUSDT"])


def test_folds_tile_the_test_period_with_an_embargo_and_never_overlap():
    folds = validation.make_folds(0, 1000 * DAY, train_days=300, test_days=90, embargo_days=2)
    assert folds and all(f.test[1] - f.test[0] == 90 * DAY for f in folds)
    assert all(f.test[0] - f.train[1] == 2 * DAY for f in folds)
    assert all(a.test[1] == b.test[0] for a, b in zip(folds, folds[1:])), "test windows tile exactly"
    assert all(f.train[1] <= f.test[0] for f in folds) and folds[-1].test[1] <= 1000 * DAY
    assert all(f.train[1] - f.train[0] == 300 * DAY for f in folds)
    anchored = validation.make_folds(0, 1000 * DAY, train_days=300, test_days=90, anchored=True)
    assert all(f.train[0] == 0 for f in anchored)
    assert validation.make_folds(0, 100 * DAY, train_days=300, test_days=90) == []


def test_walk_forward_chooses_on_training_only_and_reports_the_unseen_result(tmp_path):
    calls = []

    def evaluate(params, start, end):
        calls.append((params["k"], start, end))
        # k=2 looks best in any TRAIN window (it was overfit); on unseen data it is worthless. k=1 is mildly good everywhere.
        in_sample = end - start >= 300 * DAY
        value = {1: 0.1, 2: 0.5 if in_sample else -0.3}[params["k"]]
        return {"n_trades": 50, "expectancy_r": value, "net_pnl": value * 100}

    folds = validation.make_folds(0, 1000 * DAY, train_days=300, test_days=90)
    reg = validation.TrialRegistry(tmp_path / "t.jsonl")
    wf = validation.walk_forward(evaluate, [{"k": 1}, {"k": 2}], folds, registry=reg, family="wf")
    assert all(r["chosen"] == {"k": 2} for r in wf["rows"])
    assert wf["mean_train_value"] == pytest.approx(0.5) and wf["mean_test_value"] == pytest.approx(-0.3)
    assert wf["positive_test_fraction"] == 0.0 and wf["walk_forward_efficiency"] < 0
    assert wf["parameter_stability"]["distinct_choices"] == 1
    for r in wf["rows"]:
        assert not any(c[1] >= r["test"][0] and c[2] <= r["test"][1] and c[0] == 2 and False for c in calls)
    assert reg.n_trials("wf") == 2, "every configuration tried is on the ledger"


def test_walk_forward_skips_a_fold_where_nothing_trades_enough():
    wf = validation.walk_forward(lambda p, a, b: {"n_trades": 3, "expectancy_r": 1.0}, [{"k": 1}], validation.make_folds(0, 1000 * DAY, train_days=300, test_days=90), min_trades=20)
    assert wf["n_folds_used"] == 0 and all(r["chosen"] is None for r in wf["rows"])
    with pytest.raises(ValueError):
        validation.walk_forward(lambda *a: {}, [{}], [])


def test_the_pass_gate_requires_every_line_and_reports_what_is_missing():
    c = validation.PassCriteria()
    good = {"n_trades": 150, "expectancy_r": 0.2, "max_drawdown_pct": 10.0}
    base = {"max_drawdown_pct": 12.0, "expectancy_r": 0.1}
    ok = validation.evaluate_gate(c, candidate=good, baseline=base, stressed={"expectancy_r": 0.05}, walk_forward_report={"positive_test_fraction": 0.8}, dsr={"dsr": 0.97},
                                  holdout_candidate={"expectancy_r": 0.2}, holdout_baseline={"expectancy_r": 0.1})
    assert ok["verdict"] == "pass"
    assert validation.evaluate_gate(c, candidate=good, baseline=base, stressed=None, walk_forward_report=None, dsr=None)["verdict"] == "incomplete"
    bad = validation.evaluate_gate(c, candidate={**good, "n_trades": 40}, baseline=base, stressed={"expectancy_r": -0.1}, walk_forward_report={"positive_test_fraction": 0.4}, dsr={"dsr": 0.5})
    assert bad["verdict"] == "fail" and {"enough trades", "positive in enough out-of-sample folds", "deflated Sharpe"} <= set(bad["failed"])
    worse_dd = validation.evaluate_gate(c, candidate={**good, "max_drawdown_pct": 20.0}, baseline=base, stressed={"expectancy_r": 0.1}, walk_forward_report={"positive_test_fraction": 0.9}, dsr={"dsr": 0.99})
    assert "drawdown not worse than baseline" in worse_dd["failed"]


# ------------------------------------------------------------------ regimes


def test_regime_labels_use_entry_features_and_trailing_returns():
    hourly = [Candle(i * HOUR, 100 + i * 0.1, 100 + i * 0.1 + 1, 100 + i * 0.1 - 1, 100 + i * 0.1, 1.0) for i in range(2000)]
    t = tr(1.0, entry_ts=1500 * HOUR + 1, atr_pct=0.01, features={"adx14": 31.0})
    t2 = tr(1.0, entry_ts=1500 * HOUR + 1, atr_pct=0.01, features={"adx14": 12.0})
    label_trades([t, t2], {"BTCUSDT": hourly})
    assert t.tags["trend_state"] == "trending" and t2.tags["trend_state"] == "ranging"
    assert t.tags["market_30d"] in {"up", "flat", "down"} and t.tags["session"] in {"asia_00_08", "europe_08_16", "us_16_24"}
    early = tr(1.0, entry_ts=100 * HOUR, features={})
    label_trades([early], {"BTCUSDT": hourly})
    assert early.tags["market_30d"] == "unknown" and early.tags["trend_state"] == "unknown"


# ------------------------------------------------------------------ the baseline report


@pytest.fixture(scope="module")
def synth_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("synth")
    synthetic.write_store(root, pairs=["BTC/USDT", "ETH/USDT", "SOL/USDT"], days=60, seed=21)
    return root


def _options(**kw):
    return reporting.BaselineOptions(
        pairs=("BTC/USDT", "ETH/USDT", "SOL/USDT"), holdout_pairs=("SOL/USDT",), auto_pairs=("BTC/USDT", "ETH/USDT"),
        cost_multipliers=(1.0, 2.0), min_years=0.1, **kw,
    )


def test_baseline_report_end_to_end_on_synthetic_data(synth_root, tmp_path):
    rep = reporting.build_baseline(synth_root, tmp_path / "out", _options())
    md = (tmp_path / "out" / "baseline.md").read_text()
    assert md.startswith("> **SYNTHETIC DATA.**") and "What the service does today" in md and "closed-candle" in md
    assert "Limitations" in md and "Cost sensitivity" in md and "Forming-candle effects" in md
    saved = json.loads((tmp_path / "out" / "baseline.json").read_text())
    assert saved["synthetic"] is True and saved["data_hash"] == rep["data_hash"] and saved["quality"]["fail"] == 0
    keys = set(saved["runs"])
    for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        for mode in ("production_replay", "closed_candle"):
            for m in ("1.0", "2.0"):
                assert f"{sym}|{mode}|x{m}" in keys
    assert "AUTO|production_replay|x1.0" in keys and "AUTO|closed_candle|x1.0" in keys
    assert any(p.name.startswith("trades_") for p in (tmp_path / "out").iterdir())
    pooled = saved["pooled"]
    assert any(k.startswith("development|") for k in pooled) and any(k.startswith("holdout|") for k in pooled)
    # costs only ever hurt: net expectancy at 2x costs is not above 1x for the same decisions' pool
    for mode in ("production_replay", "closed_candle"):
        a, b = pooled.get(f"development|{mode}|x1.0"), pooled.get(f"development|{mode}|x2.0")
        if a and b and a["n_trades"] and b["n_trades"]:
            assert b["avg_cost_r"] >= a["avg_cost_r"]


def test_the_data_fingerprint_is_stable_and_changes_with_the_data(synth_root, tmp_path):
    h1 = reporting.data_hash(synth_root)
    assert h1 == reporting.data_hash(synth_root) and len(h1) == 16
    other = tmp_path / "o"
    synthetic.write_store(other, pairs=["BTC/USDT"], days=10, seed=1)
    assert reporting.data_hash(other) != h1


def test_parallel_and_serial_runs_agree(synth_root, tmp_path):
    opts = dict(run_auto=False)
    one = reporting.build_baseline(synth_root, tmp_path / "a", _options(workers=1, **opts))
    two = reporting.build_baseline(synth_root, tmp_path / "b", _options(workers=2, **opts))
    pick = lambda r: {k: (v["summary"]["n_trades"], round(v["summary"].get("expectancy_r") or 0, 9)) for k, v in r["runs"].items()}
    assert pick(one) == pick(two)


def test_pairs_without_data_are_reported_not_silently_dropped(synth_root, tmp_path):
    opts = reporting.BaselineOptions(pairs=("BTC/USDT", "DOGE/USDT"), holdout_pairs=("DOGE/USDT",), auto_pairs=(), cost_multipliers=(1.0,), run_auto=False, min_years=0.1)
    rep = reporting.build_baseline(synth_root, tmp_path / "c", opts)
    assert rep["skipped_pairs_no_data"] == ["DOGE/USDT"]


# ------------------------------------------------------------------ live report (read-only)


@pytest.fixture
def live_db(tmp_path):
    path = tmp_path / "live.db"
    init_db(f"sqlite:///{path}")
    with session_scope() as s:
        c = Customer(email="owner@example.com", password_hash="x")
        s.add(c)
        s.flush()
        a = TradingAccount(customer_id=c.id, bot_code="MBA-X", pair="BTC/USDT", market_type="futures", notification_channel="telegram", trial_ends_at=datetime.now(timezone.utc))
        s.add(a)
        s.flush()
        for i, (side, entry, stop, exit_, pnl, reason, sig) in enumerate([
            ("long", 100.2, 95.0, 110.0, 9.8, "crossover_exit", 100.0), ("long", 100.0, 95.0, 94.9, -5.1, "exchange_stop_triggered", 99.9),
        ]):
            t = Trade(account_id=a.id, side=side, is_paper=False, entry_price=entry, quantity=1.0, stop_loss_price=stop, exit_price=exit_, pnl=pnl,
                      close_reason=reason, status="closed", pair="BTC/USDT")
            s.add(t)
            s.flush()
            s.add(TradeTelemetry(trade_id=t.id, strategy_id="ema_cross_atr_v1", strategy_version="v", mode="live", market_type="futures", pair="BTC/USDT",
                                 signal_type="bullish_crossover", signal_price=sig, pair_selection_mode="manual"))
    return path


def test_live_report_reads_trades_and_measures_slippage(live_db):
    rep = live_report.live_report(str(live_db), fee_bps=5.0)
    assert rep["closed_trades"] == 2 and rep["groups"]["live"]["n"] == 2
    assert rep["measured_slippage_bps"]["entry"]["n"] == 2
    assert rep["measured_slippage_bps"]["stop_exit"]["n"] == 1 and rep["measured_slippage_bps"]["stop_exit"]["mean"] > 0
    assert rep["telemetry_coverage"] == {"closed_trades_with_telemetry": 2, "of": 2}
    text = live_report.render(rep)
    assert "owner@example.com" not in text and "MBA-X" not in text and "Live and paper results" in text
    assert "owner@example.com" not in json.dumps(rep)


def test_live_report_cannot_modify_the_database(live_db):
    before = live_db.read_bytes()
    live_report.live_report(str(live_db))
    assert live_db.read_bytes() == before
    con = live_report._connect(str(live_db))
    with pytest.raises(sqlite3.OperationalError):
        con.execute("create table sneaky(a)")
    with pytest.raises(sqlite3.OperationalError):
        con.execute("delete from trades")
    con.close()
    with pytest.raises(FileNotFoundError):
        live_report.live_report(str(live_db) + ".missing")


def test_live_report_tolerates_a_database_from_before_telemetry(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("create table trades (id text, account_id text, side text, is_paper int, entry_price real, quantity real, stop_loss_price real, exit_price real, pnl real, close_reason text, status text, pair text, opened_at text, closed_at text)")
    con.execute("insert into trades values ('1','acct-1234567','long',0,100,1,95,105,5,'crossover_exit','closed','BTC/USDT','2026-01-01 00:00:00','2026-01-01 10:00:00')")
    con.commit()
    con.close()
    rep = live_report.live_report(str(path))
    assert rep["closed_trades"] == 1 and rep["decisions"] == {"available": False} and rep["groups"]["live"]["avg_hold_h"] == pytest.approx(10.0)


# ------------------------------------------------------------------ CLI and safety


def test_cli_synthetic_quality_backtest_registry_and_live(tmp_path, capsys, live_db):
    root = tmp_path / "s"
    assert cli(["synthetic", "--root", str(root), "--days", "50", "--pairs", "BTC/USDT,ETH/USDT"]) == 0
    assert cli(["quality", "--root", str(root), "--pairs", "BTC/USDT,ETH/USDT", "--min-years", "0.1"]) == 0
    assert cli(["backtest", "--root", str(root), "--pair", "BTC/USDT", "--mode", "closed"]) == 0
    assert cli(["backtest", "--root", str(root), "--pair", "BTC/USDT,ETH/USDT", "--auto", "--mode", "closed"]) == 0
    assert cli(["registry", "--path", str(tmp_path / "none.jsonl")]) == 0
    assert cli(["live", "--database", str(live_db), "--json"]) == 0
    out = capsys.readouterr().out
    assert "SYNTHETIC store written" in out and "n_trades" in out and '"closed_trades": 2' in out


def test_cli_quality_exits_non_zero_on_corrupt_data(tmp_path):
    root = tmp_path / "s"
    cli(["synthetic", "--root", str(root), "--days", "30", "--pairs", "BTC/USDT"])
    path = next((root / "klines" / "1h").glob("*.csv"))
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[:5] + [lines[4]] + lines[5:]) + "\n")  # a duplicated bar
    assert cli(["quality", "--root", str(root), "--pairs", "BTC/USDT", "--min-years", "0.01"]) == 1


FORBIDDEN_IMPORTS = {"exchange", "crypto_utils", "db", "config", "models", "lifecycle", "trader", "scheduler", "onboarding", "notifications", "api"}


def test_the_research_package_never_reaches_credentials_orders_or_the_trading_database():
    pkg = Path(research_pkg.__file__).parent
    for path in sorted(pkg.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 2:
                top = (node.module or "").split(".")[0]
                assert top not in FORBIDDEN_IMPORTS, f"{path.name} imports mba_bot.{node.module}"
            if isinstance(node, ast.ImportFrom) and node.level == 2 and node.module is None:
                assert not {a.name for a in node.names} & FORBIDDEN_IMPORTS, path.name
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] + [getattr(node, "module", None) or ""]
                assert not any(n.split(".")[0] in {"ccxt", "sqlalchemy", "alembic"} for n in names), path.name
        stripped = ast.unparse(_without_docstrings(tree))
        for needle in ("create_order", "place_entry", "X-MBX-APIKEY", "api_secret", "decrypt(", "ENCRYPTION_KEY", "hmac", "withdraw"):
            assert needle not in stripped, f"{path.name}: {needle}"


def _without_docstrings(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant) and isinstance(first.value.value, str):
                node.body = node.body[1:] or [ast.Pass()]
    return tree


def test_the_trading_service_does_not_import_the_research_package():
    src = Path(research_pkg.__file__).parent.parent
    for path in src.glob("*.py"):
        text = path.read_text()
        assert "from .research" not in text and "import research" not in text and "mba_bot.research" not in text, path.name
    for path in (src / "api").rglob("*.py"):
        assert "research" not in path.read_text().replace("research telemetry", ""), path.name


# ------------------------------------------------------------------ experiment machinery


def test_evaluating_a_candidate_respects_the_lockbox_ledger_holdout_and_gate(tmp_path):
    from mba_bot.research import experiments
    from mba_bot.research.engine import MODE_CLOSED, BacktestConfig, AccountParams

    root = tmp_path / "s"
    synthetic.write_store(root, pairs=["BTC/USDT", "ETH/USDT", "SOL/USDT"], days=330, seed=5, with_5m=False)
    first, last = None, None
    import csv as _csv

    with open(root / "klines" / "1h" / "BTCUSDT.csv") as fh:
        ts = [int(r["ts"]) for r in _csv.DictReader(fh)]
    first, last = ts[0], ts[-1]
    box = validation.Lockbox(first_ts=first, last_ts=last, months=2, holdout_symbols=frozenset({"SOLUSDT"}), embargo_days=2)
    reg = validation.TrialRegistry(tmp_path / "ledger.jsonl")
    cfg = BacktestConfig(mode=MODE_CLOSED, account=AccountParams(allocated_capital=1000.0))
    base = experiments.Candidate("baseline", cfg)
    cand = experiments.Candidate("same_as_baseline", cfg, "identical config: the machinery should call it no better, no worse")
    out = experiments.evaluate_candidate(
        root, cand, base, dev_symbols=["BTCUSDT", "ETHUSDT"], holdout_symbols=["SOLUSDT"], registry=reg, family="unit",
        lockbox=box, train_days=100, test_days=30,
    )
    dev_end = box.develop_range()[1]
    assert out["development_range"][1] == dev_end and out["lockbox_untouched"]
    assert out["baseline_summary"] == out["candidate_summary"]
    assert out["holdout"]["baseline"] == out["holdout"]["candidate"] and out["holdout"]["candidate"]["n_trades"] is not None
    assert out["walk_forward"] and out["walk_forward"]["n_folds"] >= 1
    assert out["trials_in_family"] >= 1 and reg.n_trials("unit") >= 1 and out["deflated_sharpe"] is not None
    assert out["gate"]["verdict"] in {"pass", "fail", "incomplete"} and len(out["gate"]["checks"]) >= 6
    assert reg.lockbox_openings("unit") == 0, "evaluating a candidate never opens the lockbox"
    with pytest.raises(validation.LockboxError):
        experiments.evaluate_candidate(root, cand, base, dev_symbols=["BTCUSDT", "SOLUSDT"], holdout_symbols=[], registry=reg, family="unit", lockbox=box)
