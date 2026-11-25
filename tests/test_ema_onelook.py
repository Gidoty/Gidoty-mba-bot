"""The one-look test of EMA 4h 20/50 (src/mba_bot/ema_onelook.py). Research only. These tests use SYNTHETIC candles and made-up trades in temporary folders; none of them touches the real archive or the real window.

PROVES: the registered window, blocks, pairs, parameters, rule and costs are what the pre-registration says; each part of the pass rule both ways and the three-word verdict (fewer than 300 trades is UNINFORMATIVE, never FAIL;
PASS needs P2, P3 and P4 together); trades outside the window are ignored and the end is exclusive; block and pair counting (a pair with no trades is not positive); the summary numbers (win rate, median, top 5 share,
drawdown); the BNB reading is separate; the ledger is written BEFORE any trade is generated, a second run is refused after a success and after a crash, an unreadable ledger or a wrong plan or data hash refuses before anything
is read; the report states the verdict and each part plainly. DOES NOT PROVE anything about profit."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from mba_bot import ema_onelook as O
from mba_bot import ema_universe as U
from mba_bot import entry_study as E
from mba_bot import shortframe_backtest as SF
from test_ema_universe import T, write_root

DAY = 86_400_000


def at(day: str, hours=0) -> int:
    return E._ms(day) + hours * 3_600_000


def mk(net, ts, pair="BTCUSDT"):
    return T(net, ts, pair=pair, hold=4 * 3_600_000)


def spread(n, net_fn, pairs=U.CATALOG):
    """n trades spread evenly over the 18 months and the pairs."""
    step = (O.WINDOW_END - O.WINDOW_START - DAY) // n
    return [mk(net_fn(i), O.WINDOW_START + i * step, pairs[i % len(pairs)]) for i in range(n)]


# ---------------------------------------------------------------- the registration
def test_the_registration_is_what_the_owner_decided():
    assert O.CELL == ("ema", "4h", "s1") and O.PARAMS == (SF.EMA_PARAMS["fast"], SF.EMA_PARAMS["slow"]) == (20, 50)
    assert O.U.EMA_SETS["s1"] == (20, 50) and O.U.H.GROUP["4h"] == 4 * 3_600_000
    assert O.WINDOW_START == at("2025-04-01") and O.WINDOW_END == at("2026-10-01")
    assert O.RULE == {"min_trades": 300, "min_positive_blocks": 4, "min_positive_pairs": 6}
    assert O.BOOT_ONE["level"] == 0.95 and O.PROTOCOL.startswith("ema one-look")
    assert O.U.CATALOG == ("BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT", "DOGEUSDT", "SUIUSDT", "LINKUSDT", "ADAUSDT", "LTCUSDT")
    assert O.U.P.FEE_BPS == 5.0 and O.U.P.SLIP_ROUND_TRIP_BPS == 11.0 and O.U.BNB == {"tier": "regular", "bnb": True, "maker_entry": False}
    assert len(O.EXPECTED_DATA_HASH) == 16 and O.EXPECTED_TRADES["expected"] == 534


def test_the_six_blocks_tile_the_window_in_calendar_quarters():
    starts = list(O.BLOCK_STARTS) + [O.WINDOW_END]
    months = [datetime.fromtimestamp(s / 1000, timezone.utc).strftime("%Y-%m-%d") for s in starts]
    assert months == ["2025-04-01", "2025-07-01", "2025-10-01", "2026-01-01", "2026-04-01", "2026-07-01", "2026-10-01"] and len(O.BLOCK_LABELS) == 6
    assert [O.block_of(s) for s in O.BLOCK_STARTS] == [0, 1, 2, 3, 4, 5]
    assert O.block_of(O.WINDOW_START - 1) is None and O.block_of(O.WINDOW_END) is None and O.block_of(O.WINDOW_END - 1) == 5 and O.block_of(O.BLOCK_STARTS[1] - 1) == 0


def test_the_plan_hash_moves_with_the_rule_the_window_and_the_data_hash(monkeypatch):
    base = O.plan_hash()
    assert O.plan_hash() == base
    for attr, val in (("RULE", {**O.RULE, "min_trades": 299}), ("WINDOW_END", O.WINDOW_END + DAY), ("EXPECTED_DATA_HASH", "0" * 16), ("BOOT_ONE", {**O.BOOT_ONE, "seed": 1})):
        monkeypatch.setattr(O, attr, val)
        assert O.plan_hash() != base, attr
        monkeypatch.undo()
    assert O.plan_hash() == base


# ---------------------------------------------------------------- the verdict
@pytest.mark.parametrize("n,p2,p3,p4,want", [
    (534, True, True, True, "PASS"), (300, True, True, True, "PASS"), (299, True, True, True, "UNINFORMATIVE"), (0, False, False, False, "UNINFORMATIVE"), (10, True, True, True, "UNINFORMATIVE"),
    (534, False, True, True, "FAIL"), (534, True, False, True, "FAIL"), (534, True, True, False, "FAIL"), (534, False, False, False, "FAIL"), (300, False, True, True, "FAIL")])
def test_the_verdict_truth_table(n, p2, p3, p4, want):
    assert O.verdict(n, p2, p3, p4) == want


def good_trades():
    return spread(360, lambda i: -1.0 if (i // 10) % 4 == 3 else 0.6)  # a loss run in every fourth group of ten: every month, block and pair stays positive


def test_a_clearly_positive_spread_passes_all_four_parts():
    r = O.evaluate(good_trades())
    assert (r["P1"], r["P2"], r["P3"], r["P4"], r["verdict"]) == (True, True, True, True, "PASS") and r["positive_blocks"] == 6 and r["positive_pairs"] == 10 and r["trades"] == 360


def test_fewer_than_300_trades_is_uninformative_even_when_everything_else_is_perfect():
    r = O.evaluate(spread(299, lambda i: 0.8))
    assert r["verdict"] == "UNINFORMATIVE" and r["P1"] is False and r["P2"] and r["P3"] and r["P4"]
    assert O.evaluate(spread(300, lambda i: 0.8))["verdict"] == "PASS"


def test_a_loss_is_a_fail_not_uninformative():
    r = O.evaluate(spread(400, lambda i: -0.2))
    assert r["verdict"] == "FAIL" and r["P1"] is True and r["P2"] is False


def test_p2_needs_the_interval_of_the_total_above_zero_not_just_a_positive_mean():
    noisy = spread(360, lambda i: 1.0 if i % 2 else -0.97)
    r = O.evaluate(noisy)
    assert r["mean_net_r"] > 0 and r["total_ci"][1] <= 0 and r["P2"] is False and r["verdict"] == "FAIL"


def test_p3_counts_blocks_and_four_is_enough_three_is_not():
    def total_by_block(good_blocks):
        out = []
        for i, t in enumerate(spread(360, lambda i: 0.0)):
            k = O.block_of(t.entry_ts)
            out.append(mk(0.5 if k in good_blocks else -0.1, t.entry_ts, t.pair))
        return out
    four = O.evaluate(total_by_block({0, 1, 2, 3}))
    three = O.evaluate(total_by_block({0, 1, 2}))
    assert four["positive_blocks"] == 4 and four["P3"] is True
    assert three["positive_blocks"] == 3 and three["P3"] is False and three["verdict"] == "FAIL"


def test_p4_counts_pairs_and_a_pair_with_no_trades_is_not_positive():
    def only(pairs_good, pairs_all=U.CATALOG):
        trades = spread(360, lambda i: 0.0, pairs=pairs_all)
        return [mk(0.4 if t.pair in pairs_good else -0.05, t.entry_ts, t.pair) for t in trades]
    six = O.evaluate(only(set(U.CATALOG[:6])))
    five = O.evaluate(only(set(U.CATALOG[:5])))
    assert six["positive_pairs"] == 6 and six["P4"] and five["positive_pairs"] == 5 and not five["P4"]
    missing = O.evaluate(only(set(U.CATALOG[:6]), pairs_all=U.CATALOG[:9]))  # the 10th pair has no trade
    assert missing["pairs"]["LTCUSDT"]["trades"] == 0 and missing["positive_pairs"] == 6 and missing["P4"]
    assert O.evaluate(only(set(U.CATALOG[:5]), pairs_all=U.CATALOG[:9]))["P4"] is False


def test_trades_outside_the_window_are_ignored_and_the_end_is_exclusive():
    inside = good_trades()
    extra = [mk(50.0, O.WINDOW_START - 1), mk(50.0, O.WINDOW_END), mk(50.0, O.WINDOW_END + 5 * DAY), mk(-50.0, at("2024-06-01"))]
    a, b = O.evaluate(inside), O.evaluate(inside + extra)
    assert a["trades"] == b["trades"] == 360 and a["total_net_r"] == b["total_net_r"]
    assert O.evaluate(inside + [mk(1.0, O.WINDOW_START), mk(1.0, O.WINDOW_END - 1)])["trades"] == 362


def test_the_summary_numbers():
    nets = [10.0, 5.0, 3.0, 2.0, 1.0, -1.0, -1.0, -1.0, -1.0, -1.0, -1.0, -1.0]
    trades = [mk(x, O.WINDOW_START + i * 10 * DAY, U.CATALOG[i % 10]) for i, x in enumerate(nets)]
    r = O.evaluate(trades)
    assert r["win_rate"] == pytest.approx(5 / 12) and r["median_net_r"] == -1.0 and r["best_r"] == 10.0 and r["worst_r"] == -1.0
    assert r["top5_sum_r"] == 21.0 and r["top5_share_of_total"] == pytest.approx(21 / 14) and r["total_without_top5"] == -7.0 and r["total_net_r"] == 14.0
    assert r["longest_losing_streak"] == 7 and r["max_drawdown_r"] == 7.0 and r["max_drawdown_equity"] == pytest.approx(1 - 0.99 ** 7, rel=0.05) and r["avg_cost_r"] == pytest.approx(0.2)
    assert O.evaluate([mk(x, O.WINDOW_START + i * DAY) for i, x in enumerate([1.0, 2.0, 3.0, 4.0])])["median_net_r"] == 2.5  # even count: the mean of the two middle trades
    neg = O.evaluate([mk(-1.0, O.WINDOW_START + i * DAY) for i in range(5)])
    assert neg["top5_share_of_total"] is None and neg["total_without_top5"] == 0.0


def test_the_bnb_reading_is_the_same_trades_with_only_the_fee_changed():
    from mba_bot import cost_sensitivity as CSN

    t = good_trades()
    std_total = sum(CSN.recost(x, {"tier": "regular", "bnb": False, "maker_entry": False}) for x in t)  # the synthetic trades carry made-up net R, so compare like with like: both recomputed from prices
    bnb = O.evaluate(U.with_bnb(t))
    assert bnb["trades"] == 360 and 0 < bnb["total_net_r"] - std_total < 0.05 * 360
    assert bnb["total_net_r"] == pytest.approx(sum(CSN.recost(x, O.U.BNB) for x in t))  # exactly the BNB recomputation, not the stored net R


# ---------------------------------------------------------------- the ledger and the single look
@pytest.fixture()
def run(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr(O.U, "data_hash", lambda r: O.EXPECTED_DATA_HASH)
    monkeypatch.setattr(O.U, "qualifying", lambda r: list(U.CATALOG))

    class R:
        pass

    r = R()
    r.root, r.out, r.mp, r.seen = root, tmp_path / "out", monkeypatch, []

    def fake(root_):
        r.seen.append(json.loads(O.ledger_path(root_).read_text()) if O.ledger_path(root_).exists() else None)
        return good_trades()

    monkeypatch.setattr(O, "simulate", fake)
    r.go = lambda: O.run_once(root, r.out, expect_plan_hash=O.plan_hash())
    return r


def test_the_ledger_is_written_before_any_trade_is_generated(run):
    res = run.go()
    before = run.seen[0]
    assert before is not None and before[O.EXPECTED_DATA_HASH][0]["state"] == "opened" and before[O.EXPECTED_DATA_HASH][0]["plan_hash"] == O.plan_hash()
    after = json.loads(O.ledger_path(run.root).read_text())[O.EXPECTED_DATA_HASH][0]
    assert after["state"] == "completed" and after["verdict"] == res["verdict"] == "PASS" and after["trades"] == 360


def test_the_run_publishes_the_bnb_reading_next_to_the_standard_one(run):
    res = run.go()
    assert res["bnb_reading"]["total_net_r"] == pytest.approx(O.evaluate(U.with_bnb(good_trades()))["total_net_r"])
    assert res["bnb_reading"]["total_net_r"] != res["total_net_r"] and res["verdict"] == "PASS"


def test_a_second_run_is_refused_after_a_success(run):
    run.go()
    run.seen.clear()
    with pytest.raises(O.LookSpent, match="already opened once"):
        run.go()
    assert run.seen == []  # no trade was generated by the refused run


def test_a_crash_after_the_read_began_spends_the_look_and_it_cannot_be_repeated(run):
    def boom(root_):
        raise RuntimeError("disk error")

    run.mp.setattr(O, "simulate", boom)
    with pytest.raises(RuntimeError):
        run.go()
    entry = json.loads(O.ledger_path(run.root).read_text())[O.EXPECTED_DATA_HASH][0]
    assert entry["state"] == "crashed" and entry["error_class"] == "RuntimeError"
    assert not (run.out / "onelook_report.md").exists()  # nothing partial was published
    run.mp.setattr(O, "simulate", lambda r: good_trades())
    with pytest.raises(O.LookSpent):
        run.go()


def test_a_crash_while_evaluating_also_spends_the_look(run):
    run.mp.setattr(O, "evaluate", lambda trades: (_ for _ in ()).throw(ValueError("x")))
    with pytest.raises(ValueError):
        run.go()
    assert json.loads(O.ledger_path(run.root).read_text())[O.EXPECTED_DATA_HASH][0]["state"] == "crashed"


def test_an_unreadable_ledger_counts_as_spent(run):
    O.ledger_path(run.root).write_text("{broken")
    with pytest.raises(O.LookSpent, match="unreadable"):
        run.go()
    assert run.seen == []


def test_a_wrong_plan_hash_a_wrong_data_hash_or_a_wrong_pair_list_refuse_before_anything_is_read(run):
    with pytest.raises(O.LookSpent, match="plan hash"):
        O.run_once(run.root, run.out, expect_plan_hash="0000")
    run.mp.setattr(O.U, "data_hash", lambda r: "1" * 16)
    with pytest.raises(O.LookSpent, match="data hash"):
        run.go()
    run.mp.setattr(O.U, "data_hash", lambda r: O.EXPECTED_DATA_HASH)
    run.mp.setattr(O.U, "qualifying", lambda r: list(U.CATALOG[:9]))
    with pytest.raises(O.LookSpent, match="liquidity"):
        run.go()
    assert not O.ledger_path(run.root).exists() and run.seen == []  # the look was not spent by any refusal


def test_there_is_no_way_to_reopen_the_window():
    import inspect

    src = inspect.getsource(O)
    for flag in ("second_look", "second-look", "--force", "--reopen", "--again", "unlink(", "os.remove"):
        assert flag not in src, flag


def test_the_command_line_codes(run, capsys):
    assert O.main([]) == 2 and "required" in capsys.readouterr().err
    assert O.main(["--root", str(run.root), "--out", str(run.out), "--expect-plan-hash", "bad"]) == 2
    assert O.main(["--plan"]) == 0 and capsys.readouterr().out.strip() == O.plan_hash()
    run.mp.setattr(O, "simulate", lambda r: (_ for _ in ()).throw(OSError("x")))
    assert O.main(["--root", str(run.root), "--out", str(run.out), "--expect-plan-hash", O.plan_hash()]) == 3
    assert "THE LOOK IS SPENT" in capsys.readouterr().err
    assert O.main(["--root", str(run.root), "--out", str(run.out), "--expect-plan-hash", O.plan_hash()]) == 2


# ---------------------------------------------------------------- the report
def test_the_report_states_the_verdict_each_part_and_the_labels_without_softening(run):
    res = run.go()
    text = (run.out / "onelook_report.md").read_text()
    assert "## RESULT: PASS" in text and "P1 at least 300 trades" in text and "P2 mean net R above zero AND the 95% month-block interval" in text and "P3 positive in at least 4 of the 6" in text and "P4 positive on at least 6 of the 10" in text
    assert "DESCRIPTIVE ONLY" in text and "1 slot" in text and "3 slots" in text and "Fees paid in BNB (reported separately" in text and "Expected before the run: about 534 trades" in text
    import inspect

    src = inspect.getsource(O.report).lower()
    for soft in ("almost", "nearly", "marginal", "close to", "narrowly", "just missed", "promising", "encouraging", "suggests"):
        assert soft not in src, soft
    assert set(res["descriptive_slots"]) == {"1 slot", "3 slots", "unlimited"}
    fail = O.report({**res, "verdict": "FAIL", "P2": False})
    assert "## RESULT: FAIL" in fail and "## RESULT: PASS" not in fail


def test_the_module_is_research_only():
    import inspect

    doc = inspect.getsource(O).split('"""')[1]
    assert "Research only: no network, no key, no order, no live effect." in doc
    src = inspect.getsource(O)
    for forbidden in ("httpx", "urllib", "socket", "ccxt", "requests."):
        assert forbidden not in src


def test_end_to_end_on_synthetic_candles_runs_once_and_publishes_one_report(tmp_path, monkeypatch):
    root = tmp_path / "synthetic"
    write_root(root, years=7.0, seed=11)
    dh = O.U.data_hash(root)
    monkeypatch.setattr(O, "EXPECTED_DATA_HASH", dh)
    res = O.run_once(root, tmp_path / "out", expect_plan_hash=O.plan_hash())
    assert res["verdict"] in ("PASS", "FAIL", "UNINFORMATIVE") and res["trades"] >= 0 and res["data_hash"] == dh
    assert (tmp_path / "out" / "onelook_report.md").exists() and json.loads(O.ledger_path(root).read_text())[dh][0]["state"] == "completed"
    with pytest.raises(O.LookSpent):
        O.run_once(root, tmp_path / "out2", expect_plan_hash=O.plan_hash())
