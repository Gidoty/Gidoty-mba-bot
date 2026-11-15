"""The donchian_breakout_v1 backtest runner (src/mba_bot/donchian_backtest.py, docs/research/donchian_preregistration.md).

PROVES: the simulation rules (next-open entry, slippage, fees, stop and trailing logic shared with the live code, the cooldown), that VOLUME is read,
the metrics wiring, that tuning never touches the sealed months, that the sealed months open once only with the same plan and data, and that the
reports say what is and is not measured. The data used is SYNTHETIC (generated noise): it proves mechanics, it is NOT evidence about any strategy or market."""
from __future__ import annotations

import csv
import json
import math
import random
from pathlib import Path

import pytest
from test_donchian import base, cd

from mba_bot import donchian as D
from mba_bot import donchian_backtest as B
from mba_bot import rsi_backtest as RB
from mba_bot.indicators import atr
from mba_bot.strategy import Candle

HOUR = 3_600_000
SLIP = 5 / 1e4


def sim(cs, market="spot", p=D.DonchianParams(), cooldown_hours=4.0):
    return B.simulate("BTCUSDT", cs, p, market, cs[0].timestamp, cs[-1].timestamp + HOUR, cooldown_hours)


def breakout_then(rows):
    """40 flat candles, the LONG breakout candle, then `rows` (o, h, l, c, v)."""
    cs = base(40)
    cs.append(cd(40, 100.0, 103.5, 100.5, 103.0, 200.0))
    for i, r in enumerate(rows, start=41):
        cs.append(cd(i, *r))
    return cs


# ====================================================================== simulation, hand-built
def test_entry_is_the_next_open_with_slippage_and_a_stop_out_pays_fees_and_slippage():
    cs = breakout_then([(103.2, 103.4, 99.0, 99.5, 100.0), (99.5, 99.6, 99.4, 99.5, 100.0)])
    trades, unfinished = sim(cs, "spot")
    assert len(trades) == 1 and unfinished == 0
    t = trades[0]
    entry = 103.2 * (1 + SLIP)
    sig = D.signal_from_candles(cs[:41])
    stop = D.initial_stop(entry, "long", sig.mid, sig.atr)
    assert math.isclose(t.entry, entry) and math.isclose(t.stop, stop) and t.reason == D.EXIT_STOP
    assert math.isclose(t.exit, stop * (1 - SLIP))
    dist = entry - stop
    fees = 0.001 * (entry + t.exit)
    assert math.isclose(t.gross_r, (t.exit - entry) / dist) and math.isclose(t.net_r, (t.exit - entry - fees) / dist) and t.net_r < -1.0


def test_futures_fees_are_lower_than_spot_for_the_same_trade():
    cs = breakout_then([(103.2, 103.4, 99.0, 99.5, 100.0), (99.5, 99.6, 99.4, 99.5, 100.0)])
    spot, fut = sim(cs, "spot")[0][0], sim(cs, "futures")[0][0]
    assert fut.net_r > spot.net_r and math.isclose(fut.gross_r, spot.gross_r)


def test_volume_is_read_from_the_data():
    low = base(40)
    low.append(cd(40, 100.0, 103.5, 100.5, 103.0, 140.0))  # below 1.5 x the average of 100
    low += [cd(41, 103.2, 103.4, 99.0, 99.5), cd(42, 99.5, 99.6, 99.4, 99.5)]
    assert sim(low)[0] == []
    assert len(sim(breakout_then([(103.2, 103.4, 99.0, 99.5, 100.0), (99.5, 99.6, 99.4, 99.5, 100.0)]))[0]) == 1


def test_a_trailing_exit_reports_the_trailing_reason_and_a_profit():
    rows = [(103.2, 110.0, 103.0, 109.0, 100.0), (109.0, 115.0, 108.0, 114.0, 100.0), (114.0, 114.5, 100.5, 101.0, 100.0), (101.0, 101.5, 100.8, 101.0, 100.0)]
    trades, _ = sim(breakout_then(rows))
    assert len(trades) == 1 and trades[0].reason == D.EXIT_TRAIL and trades[0].net_r > 0


def test_a_short_needs_futures():
    cs = base(40)
    cs.append(cd(40, 100.0, 99.5, 96.5, 97.0, 200.0))
    cs += [cd(41, 96.8, 100.5, 96.5, 99.9, 100.0), cd(42, 99.9, 100.0, 99.8, 99.9, 100.0)]
    assert sim(cs, "spot")[0] == []
    fut = sim(cs, "futures")[0]
    assert len(fut) == 1 and fut[0].side == "short" and fut[0].entry < 96.8  # sold below the open by the slippage


def test_a_trade_still_open_at_the_end_is_not_counted_and_is_reported():
    trades, unfinished = sim(breakout_then([(103.2, 103.6, 102.8, 103.0, 100.0)]))
    assert trades == [] and unfinished == 1


def test_no_trade_is_taken_outside_the_window():
    cs = breakout_then([(103.2, 103.4, 99.0, 99.5, 100.0), (99.5, 99.6, 99.4, 99.5, 100.0)])
    sig_ts = cs[40].timestamp
    assert B.simulate("BTCUSDT", cs, D.DonchianParams(), "spot", sig_ts + HOUR, cs[-1].timestamp + HOUR)[0] == []
    assert B.simulate("BTCUSDT", cs, D.DonchianParams(), "spot", cs[0].timestamp, sig_ts)[0] == []


def second_breakout():
    # stop-out on candle 41, then a second breakout (beyond the 103.5 high, volume above 1.5 x the average) on candle 43, which closes inside the 4-hour cooldown
    rows = [(103.2, 103.4, 99.0, 99.5, 100.0), (99.5, 100.0, 99.0, 99.8, 100.0), (99.8, 104.5, 99.8, 104.0, 1000.0), (104.1, 104.3, 100.0, 100.5, 100.0)]
    return breakout_then(rows)


def test_the_cooldown_after_an_initial_stop_skips_a_breakout_on_the_same_pair():
    cs = second_breakout()
    assert len(sim(cs, cooldown_hours=4.0)[0]) == 1
    assert len(sim(cs, cooldown_hours=0.0)[0]) == 2  # without the cooldown the second breakout is traded


def test_the_cooldown_ends():
    cs = second_breakout()
    # stop exit on candle 41 closes at ts(42); 1 hour of cooldown ends at ts(43): candle 43 closes at ts(44) -> allowed
    assert len(sim(cs, cooldown_hours=1.0)[0]) == 2


def test_the_live_function_and_the_runner_agree_on_every_candle():
    rng = random.Random(4)
    x, cs = 100.0, []
    for i in range(1500):
        o = x
        x = max(1.0, x + rng.gauss(0.02, 1.0))
        cs.append(Candle(i * HOUR, o, max(o, x) + abs(rng.gauss(0, 0.3)), min(o, x) - abs(rng.gauss(0, 0.3)), x, rng.lognormvariate(0, 0.6) * 100))
    a = atr([c.high for c in cs], [c.low for c in cs], [c.close for c in cs], 14)
    seen = 0
    for i in range(D.min_candles() - 1, len(cs)):
        live = D.signal_from_candles(cs[: i + 1], allow_short=True)
        runner = D.signal_at(cs, a, i, allow_short=True)
        assert (live.side, live.reason, live.stop) == (runner.side, runner.reason, runner.stop)
        seen += live.side is not None
    assert seen > 0


# ====================================================================== the runner on a SYNTHETIC store
START = 1_700_000_000_000 // HOUR * HOUR


def write_store(root: Path, *, days=330, seed=1, pairs=RB.PAIRS):
    (Path(root) / "klines" / "1h").mkdir(parents=True, exist_ok=True)
    for k, sym in enumerate(pairs):
        rng = random.Random(seed * 100 + k)
        x, rows, drift = 100.0, [], 0.0
        for i in range(days * 24):
            if i % 200 == 0:
                drift = rng.gauss(0, 0.08)
            o = x
            x = max(1.0, x + drift + rng.gauss(0, 0.7))
            hi, lo = max(o, x) + abs(rng.gauss(0, 0.3)), min(o, x) - abs(rng.gauss(0, 0.3))
            rows.append([START + i * HOUR, o, hi, lo, x, rng.lognormvariate(0, 0.7) * 100, 0, 0, 0])
        with (Path(root) / "klines" / "1h" / f"{sym}.csv").open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["ts", "open", "high", "low", "close", "volume", "quote_volume", "trades", "taker_buy_volume"])
            w.writerows(rows)


@pytest.fixture(scope="module")
def master_store(tmp_path_factory):
    root = tmp_path_factory.mktemp("store")
    write_store(root)
    return root


@pytest.fixture
def store(master_store, tmp_path):
    """A private copy per test: the one-look ledger now lives beside the data (audit M4), so tests that open the sealed months must not share a folder."""
    import shutil
    root = tmp_path / "data"
    shutil.copytree(master_store, root)
    return root


def test_plan_hash_changes_with_the_strategy_file_the_arms_the_costs_and_the_cooldown(monkeypatch):
    base_hash = B.plan_hash()
    assert base_hash == B.plan_hash()
    monkeypatch.setattr(RB, "_sha", lambda p: "deadbeef")
    assert B.plan_hash() != base_hash  # the strategy file's bytes are part of the registration
    monkeypatch.undo()
    monkeypatch.setattr(B, "ARMS", B.ARMS[:-1])
    assert B.plan_hash() != base_hash
    monkeypatch.undo()
    monkeypatch.setattr(B, "COOLDOWN_HOURS", 5.0)
    assert B.plan_hash() != base_hash
    monkeypatch.undo()
    monkeypatch.setattr(B, "SLIPPAGE_BPS", 6.0)
    assert B.plan_hash() != base_hash
    assert B.plan_hash() != RB.plan_hash()  # its own registration, not the RSI one


def test_the_synthetic_store_produces_trades_so_the_runner_is_exercised(store):
    data = RB.load_all(store)
    w = RB.windows(data)
    t0, t1 = w["tune"]
    total = sum(len(B.simulate(sym, cs, D.DonchianParams(), "futures", t0, t1)[0]) for sym, cs in data.items())
    assert total > 0


def test_tuning_never_asks_for_the_sealed_months_and_writes_its_outputs(store, tmp_path, monkeypatch):
    seen = []
    real = B.run_arm

    def spy(data, arm, s, e):
        seen.append((s, e))
        return real(data, arm, s, e)

    monkeypatch.setattr(B, "run_arm", spy)
    res = B.run_tune(store, tmp_path / "out")
    w = res["windows"]
    assert seen and all(s == w["tune"][0] and e == w["tune"][1] for s, e in seen) and w["tune"][1] < w["sealed"][0]
    for f in ("tune_results.json", "selected.json", "tune_report.md"):
        assert (tmp_path / "out" / f).exists()
    assert res["sealed_months_read"] is False and set(res["rows"]) == {a.name for a in B.ARMS}
    text = (tmp_path / "out" / "tune_report.md").read_text()
    assert "sealed months NOT read" in text and "NOT evidence that the strategy works" in text
    assert not list(store.glob("*sealed*"))


def test_the_tuning_window_cuts_trades_at_its_end(store):
    data = RB.load_all(store)
    t0, t1 = RB.windows(data)["tune"]
    for sym, cs in data.items():
        trades, _ = B.simulate(sym, cs, D.DonchianParams(), "spot", t0, t1)
        assert all(x.exit_ts + HOUR <= t1 and x.entry_ts >= t0 for x in trades)


def test_expect_plan_hash_guards_the_run(store, tmp_path):
    with pytest.raises(ValueError, match="differs from the registered"):
        B.run_tune(store, tmp_path / "o", expect_plan_hash="0000000000000000")
    assert not (tmp_path / "o").exists()


def test_the_sealed_months_open_once_and_only_with_the_same_plan_and_data(store, tmp_path, monkeypatch):
    out = tmp_path / "out"
    B.run_tune(store, out)
    with pytest.raises(B.SealedError, match="explicit"):
        B.run_confirm(store, out, reason="x", confirm=False)
    with pytest.raises(B.SealedError, match="reason"):
        B.run_confirm(store, out, reason="  ", confirm=True)
    monkeypatch.setattr(B, "COOLDOWN_HOURS", 9.0)
    with pytest.raises(B.SealedError, match="plan_hash differs"):
        B.run_confirm(store, out, reason="try", confirm=True)
    monkeypatch.undo()
    assert not list(store.glob("*sealed*"))  # every refusal happened before the sealed months were read (no ledger beside the data)
    res = B.run_confirm(store, out, reason="registered one-shot", confirm=True)
    assert res["contaminated"] is False and res["looks"] == 1 and (out / "final_report.md").exists()
    with pytest.raises(B.SealedError, match="already read once"):
        B.run_confirm(store, out, reason="again", confirm=True)
    second = B.run_confirm(store, out, reason="forced", confirm=True, second_look=True)
    assert second["contaminated"] is True and "NOT the first look" in (out / "final_report.md").read_text()


def test_changed_data_or_a_missing_tuning_is_refused(store, tmp_path):
    with pytest.raises(B.SealedError, match="tuning first"):
        B.run_confirm(store, tmp_path / "none", reason="x", confirm=True)
    other = tmp_path / "other"
    write_store(other, seed=2)
    out = tmp_path / "out"
    B.run_tune(store, out)
    with pytest.raises(B.SealedError, match="data_hash differs"):
        B.run_confirm(other, out, reason="x", confirm=True)


def test_the_final_report_says_plainly_when_the_untouched_months_are_negative(store, tmp_path, monkeypatch):
    out = tmp_path / "out"
    B.run_tune(store, out)
    real = B.metrics

    def negative(trades, s, e):
        m = real(trades, s, e)
        return {**m, "expectancy_r": -0.1} if m["trades"] else m

    monkeypatch.setattr(B, "metrics", negative)
    B.run_confirm(store, out, reason="one shot", confirm=True)
    text = (out / "final_report.md").read_text()
    assert "NEGATIVE after fees and slippage" in text and "The strategy lost money there" in text
    assert "NOT MEASURED" in text and "cannot be added into a measured combined figure" in text
    for sym in RB.PAIRS:
        assert sym in text
    assert "spot" in text and "futures" in text


def test_the_final_report_never_claims_a_result_it_did_not_measure(store, tmp_path):
    out = tmp_path / "out"
    B.run_tune(store, out)
    B.run_confirm(store, out, reason="one shot", confirm=True)
    text = (out / "final_report.md").read_text()
    assert "can be luck" in text or "NEGATIVE" in text
    assert "NOT MEASURED" in text


def test_the_cli_plan_prints_the_hash(capsys):
    assert B.main(["plan"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["plan_hash"] == B.plan_hash() and out["arms"][0] == "SPEC" and out["months"] == [6.0, 2.0, 3.0] and out["cooldown_hours"] == 4.0
    assert B.main(["tune", "--root", "/nonexistent-dir"]) == 2


def test_the_runner_has_no_network_database_or_secret_access():
    import ast
    tree = ast.parse(Path(B.__file__).read_text())
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    banned = ("requests", "httpx", "urllib", "socket", "ccxt", "sqlalchemy", "trader", "exchange", "config", "models", "db", "crypto")
    assert not [m for m in mods if any(b == m.split(".")[-1] or b == m for b in banned)], mods


def test_only_an_initial_stop_starts_the_cooldown_not_a_trailing_exit():
    rows = [(103.2, 110.0, 103.0, 109.0, 100.0), (109.0, 115.0, 108.0, 114.0, 100.0), (114.0, 114.5, 100.5, 101.0, 100.0),  # trailing exit on candle 43
            (101.0, 120.0, 101.0, 119.0, 1000.0), (119.2, 119.3, 90.0, 95.0, 100.0), (95.0, 95.5, 94.5, 95.0, 100.0)]  # a second breakout on 44, stopped out on 45
    cs = breakout_then(rows)
    trades, _ = sim(cs, cooldown_hours=4.0)
    assert [t.reason for t in trades][:1] == [D.EXIT_TRAIL] and len(trades) == 2  # the trailing exit did not block the next signal


def test_a_gap_that_puts_the_entry_on_the_wrong_side_of_the_stop_is_not_traded():
    cs = breakout_then([(99.5, 99.6, 99.0, 99.2, 100.0), (99.2, 99.3, 99.1, 99.2, 100.0)])  # opens below the 100 midline: the stop would sit above the entry
    assert sim(cs)[0] == []
