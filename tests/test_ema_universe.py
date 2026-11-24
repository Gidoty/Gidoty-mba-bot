"""The EMA universe study (src/mba_bot/ema_universe.py). Research only.

PROVES: the registered catalog equals the bot's own pair list; the liquidity rule (years, median volume, a spike does not count); the BNB reading is the same trades with only the fee changed and the standard reading
reproduces each trade's net R; each part of the development rule both ways (trade count, mean and interval, windows, pairs, concentration); trades per year use each pair's own data span; pilot months; positions held at
once; that the development run neither counts nor evaluates a confirmation-window trade; that nothing passing leaves the confirmation windows unread and unrecorded; the one-look ledger (written before the read) and every
refusal; the plan hash moves with the rule.
DOES NOT PROVE anything about profit."""
from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from mba_bot import config
from mba_bot import cost_sensitivity as CSN
from mba_bot import ema_universe as U
from mba_bot import entry_study as E
from mba_bot import htf_study as H
from mba_bot import profit_backtest as P

HOUR, DAY = 3_600_000, 86_400_000


def T(net, entry_ts, pair="BTCUSDT", dist=10.0, entry=100.0, hold=DAY):
    return P.SimTrade(pair, "long", entry_ts, entry_ts + hold, entry, entry, dist, entry - dist, entry, [P.Leg(1.0, entry, entry_ts, "x")], "x", net + 0.2, net, 0.0, 0.0, False, None, hold)


def in_window(k, offset_days=10):
    return E.TEST_STARTS[k] + offset_days * DAY


def write_root(root: Path, *, years=7.0, volume=1e8, seed=3, only=None, short=()):
    rng = random.Random(seed)
    (root / "klines" / "1h").mkdir(parents=True)
    (root / "funding").mkdir()
    end = E._ms("2026-11-01")
    for k, sym in enumerate(U.CATALOG):
        if only is not None and sym not in only:
            continue
        n = int((2.0 if sym in short else years) * 365.25 * 24)
        price, lines = 100.0, ["ts,open,high,low,close,volume,quote_volume,trades,taker_buy_volume"]
        t0 = end - n * HOUR
        for i in range(n):
            o = price
            price *= 1 + rng.gauss(0.0004 * (1 if (i // 500) % 2 == 0 else -1), 0.006)
            lines.append(f"{t0 + i * HOUR},{o},{max(o, price) * 1.001},{min(o, price) * 0.999},{price},1,{volume},1,1")
        (root / "klines" / "1h" / f"{sym}.csv").write_text("\n".join(lines) + "\n")
        (root / "funding" / f"{sym}.csv").write_text("calc_time,funding_interval_hours,last_funding_rate\n" + f"{t0},8,0.0001\n")


# ---------------------------------------------------------------- registration
def test_the_registered_catalog_is_the_bots_own_pair_list():
    assert U.CATALOG == tuple(p.replace("/", "") for p in config.ALL_KNOWN_PAIRS)
    assert U.FOUR == tuple(p.replace("/", "") for p in config.SUPPORTED_PAIRS)


def test_the_candidates_are_the_shipped_periods_and_one_classic_pair_on_4h_and_1d_only():
    assert U.EMA_SETS == {"s1": (20, 50), "s2": (12, 26)} and (U.EMA_SETS["s1"][0], U.EMA_SETS["s1"][1]) == (H.SF.EMA_PARAMS["fast"], H.SF.EMA_PARAMS["slow"])
    assert {(c[1], c[2]) for c in U.CELLS} == {("4h", "s1"), ("4h", "s2"), ("1d", "s1"), ("1d", "s2")} and all(c[0] == "ema" for c in U.CELLS)


def test_the_plan_hash_moves_with_the_rule_and_the_universe(monkeypatch):
    base = U.plan_hash()
    assert U.plan_hash() == base
    monkeypatch.setitem(U.RULE, "dev_min_trades", 151)
    assert U.plan_hash() != base
    monkeypatch.setitem(U.RULE, "dev_min_trades", 150)
    assert U.plan_hash() == base
    monkeypatch.setattr(U, "CATALOG", U.CATALOG[:9])
    assert U.plan_hash() != base


# ---------------------------------------------------------------- liquidity
def test_the_liquidity_rule_needs_years_and_a_median_volume_and_a_spike_does_not_count(tmp_path):
    write_root(tmp_path, years=4.0, volume=1e8, only=("BTCUSDT", "ETHUSDT", "SUIUSDT"), short=("SUIUSDT",))
    t = U.liquidity_table(tmp_path)
    assert t["BTCUSDT"]["ok"] and t["ETHUSDT"]["ok"]
    assert not t["SUIUSDT"]["ok"] and t["SUIUSDT"]["years"] < 3.0  # too little history
    assert not t["SOLUSDT"]["ok"] and t["SOLUSDT"]["years"] == 0.0  # no file
    low = tmp_path / "low"
    write_root(low, years=4.0, volume=1e5 / 24, only=("BTCUSDT",))
    assert not U.liquidity_table(low)["BTCUSDT"]["ok"]  # USD 100 thousand a day
    spike = tmp_path / "spike"
    write_root(spike, years=4.0, volume=1e5 / 24, only=("BTCUSDT",))
    p = spike / "klines" / "1h" / "BTCUSDT.csv"
    rows = p.read_text().splitlines()
    for i in range(len(rows) - 24 * 5, len(rows) - 24 * 2):  # three huge days inside the 365-day window
        parts = rows[i].split(",")
        parts[6] = "1e12"
        rows[i] = ",".join(parts)
    p.write_text("\n".join(rows) + "\n")
    assert not U.liquidity_table(spike)["BTCUSDT"]["ok"]


# ---------------------------------------------------------------- costs
def test_the_standard_reading_reproduces_net_r_and_the_bnb_reading_changes_only_the_fee():
    rng = random.Random(1)
    cs, price = [], 100.0
    t0 = E._ms("2021-01-01")
    for i in range(24 * 900):
        o = price
        price *= 1 + rng.gauss(0.0006 * (1 if (i // 700) % 2 == 0 else -1), 0.006)
        cs.append(H.Candle(t0 + i * HOUR, o, max(o, price) * 1.001, min(o, price) * 0.999, price, 1.0))
    d = H.aggregate(cs, DAY)
    trades = H.sim_ema("BTCUSDT", d, DAY, (12, 26), t0, t0 + 900 * DAY, [(t0 + 8 * HOUR * k, 0.0001) for k in range(3000)])
    assert len(trades) >= 5
    cheap = U.with_bnb(trades)
    for a, b in zip(trades, cheap):
        assert CSN.recost(a, {"tier": "regular", "bnb": False, "maker_entry": False}) == pytest.approx(a.net_r, abs=1e-9)
        fees = 5.0 / 1e4 * (a.entry + a.exit_avg * 0) + 0  # entry fee in price terms, exit fee below
        saved = (b.net_r - a.net_r) * a.dist
        expect = 0.10 * 5.0 / 1e4 * (a.raw_entry * (1 + 0.00055) if a.side == "long" else a.raw_entry * (1 - 0.00055))
        assert saved > 0 and saved == pytest.approx(expect + 0.10 * 5.0 / 1e4 * (a.exit_avg * (1 - 0.00055) if a.side == "long" else a.exit_avg * (1 + 0.00055)), rel=1e-6)
        assert (b.pair, b.entry_ts, b.exit_ts, b.dist, b.funding_r, b.gross_r) == (a.pair, a.entry_ts, a.exit_ts, a.dist, a.funding_r, a.gross_r) and fees is not None


# ---------------------------------------------------------------- the rule
def good_trades(n_per_window=8, pairs=U.CATALOG, net=0.4):
    out = []
    for k in U.DEV_WINDOWS:
        for j in range(n_per_window):
            out.append(T(-0.5 if j % 7 == 0 else net, in_window(k, 3 + j), pair=pairs[j % len(pairs)]))
    return out


def ev(trades, **kw):
    r = dict(U.RULE)
    r.update(kw)
    return U.evaluate(trades, U.DEV_WINDOWS, r["dev_min_trades"], r["dev_min_positive_windows"], r["dev_min_positive_pairs"])


def test_a_clearly_positive_spread_passes_every_part():
    e = ev(good_trades(n_per_window=40), dev_min_trades=150)
    assert e["P1"] and e["P2"] and e["P3"] and e["P4"] and U.dev_pass(e)


def test_each_part_of_the_development_rule_can_fail_alone():
    ok = good_trades(n_per_window=40)
    few = ev(ok[:100])
    assert not few["P1"] and not U.dev_pass(few)
    neg = ev([T(-0.2, in_window(k, 3 + j), pair=U.CATALOG[j % 10]) for k in U.DEV_WINDOWS for j in range(30)])
    assert not neg["P2"] and not U.dev_pass(neg)
    noisy = [T(0.1 if (i % 2) else -0.099, in_window(i % 6, 3 + (i // 6) % 150), pair=U.CATALOG[i % 10]) for i in range(180)]  # mean just above zero, interval spans zero
    e = ev(noisy)
    assert e["mean_net_r"] > 0 and not e["P2"] and not U.dev_pass(e)
    one_window = [T(1.0, in_window(0, 3 + j % 150), pair=U.CATALOG[j % 10]) for j in range(200)] + [T(-0.01, in_window(k, 5), pair="BTCUSDT") for k in range(1, 6)]
    e = ev(one_window)
    assert e["positive_windows"] == 1 and not e["P3"] and not U.dev_pass(e)
    one_pair = [T(1.0, in_window(k, 3 + j), pair="SOLUSDT") for k in U.DEV_WINDOWS for j in range(40)] + [T(-0.01, in_window(k, 100 + j), pair=U.CATALOG[j % 4]) for k in U.DEV_WINDOWS for j in range(2)]
    e = ev(one_pair)
    assert e["P1"] and e["P2"] and e["P3"] and not e["P4"] and e["best_pair"] == "SOLUSDT" and e["total_without_best_pair"] < 0 and not U.dev_pass(e)


def test_concentration_needs_the_total_to_stay_positive_without_the_best_pair():
    trades = []
    for k in U.DEV_WINDOWS:
        for j, pair in enumerate(U.CATALOG):
            trades += [T(0.05 if pair != "BTCUSDT" else 1.0, in_window(k, 2 + j * 3 + i), pair=pair) for i in range(3)]
    e = ev(trades)
    assert e["positive_pairs"] == 10 and e["total_without_best_pair"] > 0 and e["P4"]
    trades2 = [t if t.pair == "BTCUSDT" else T(-0.04, t.entry_ts, pair=t.pair) for t in trades]
    e2 = ev(trades2)
    assert e2["positive_pairs"] == 1 and not e2["P4"]


def test_a_window_without_trades_never_counts_as_positive():
    trades = [T(1.0, in_window(0, 3 + j % 150), pair=U.CATALOG[j % 10]) for j in range(200)]
    e = ev(trades)
    assert e["positive_windows"] == 1 and not e["P3"]


# ---------------------------------------------------------------- counts
def test_trades_per_year_uses_each_pairs_own_data_span_and_reports_the_four_and_the_universe():
    end = E.TEST_STARTS[6]
    firsts = {(p, "4h"): E._ms("2020-04-01") for p in U.CATALOG}
    firsts[("SUIUSDT", "4h")] = end - 2 * 365 * DAY
    firsts[("LTCUSDT", "4h")] = None
    years = (end - E._ms("2020-04-01")) / (365.25 * DAY)
    trades = [T(0.1, E._ms("2021-01-01") + i * DAY, pair="BTCUSDT") for i in range(int(years * 10))]
    trades += [T(0.1, end - 100 * DAY + i * HOUR, pair="SUIUSDT") for i in range(20)]
    t = U.trades_per_year(trades, firsts, "4h", end)
    assert t["per_pair"]["BTCUSDT"]["per_year"] == pytest.approx(int(years * 10) / years)
    sui_years = 2 * 365 / 365.25
    assert t["per_pair"]["SUIUSDT"]["years"] == pytest.approx(sui_years, rel=1e-3) and t["per_pair"]["SUIUSDT"]["per_year"] == pytest.approx(20 / sui_years, rel=1e-3)
    assert t["per_pair"]["LTCUSDT"]["per_year"] == 0.0
    assert t["universe_per_year"] == pytest.approx(sum(v["per_year"] for v in t["per_pair"].values()))
    assert t["four_per_year"] == pytest.approx(t["per_pair"]["BTCUSDT"]["per_year"]) and t["one_pair_max"] == t["per_pair"]["SUIUSDT"]["per_year"] and t["one_pair_min"] == 0.0
    late = [T(0.1, end + DAY, pair="BTCUSDT")]  # a confirmation-window entry is not counted
    assert U.trades_per_year(late, firsts, "4h", end)["per_pair"]["BTCUSDT"]["trades"] == 0


def test_pilot_months():
    assert U.pilot_months(12.0) == pytest.approx(100.0) and U.pilot_months(100.0) == pytest.approx(12.0) and U.pilot_months(0.0) is None


def test_concurrency_time_average_overlap_and_maximum():
    a = 1_000_000 * DAY // 1000
    trades = [T(0.1, a, hold=4 * DAY), T(0.1, a + 2 * DAY, pair="ETHUSDT", hold=4 * DAY), T(0.1, a + 3 * DAY, pair="XRPUSDT", hold=DAY)]
    c = U.concurrency(trades, a, a + 10 * DAY)
    assert c["max_open"] == 3 and c["mean_open"] == pytest.approx((4 + 4 + 1) / 10) and c["share_time_ge1"] == pytest.approx(0.6) and c["share_time_ge2"] == pytest.approx(0.2)
    assert U.concurrency([], a, a + DAY) == {"mean_open": 0.0, "share_time_ge1": 0.0, "share_time_ge2": 0.0, "max_open": 0}
    clipped = U.concurrency([T(0.1, a - DAY, hold=3 * DAY)], a, a + 10 * DAY)
    assert clipped["mean_open"] == pytest.approx(0.2)


# ---------------------------------------------------------------- the run
def fake_sims(passing: bool, confirm_value=5.0):
    def f(root, cells=U.CELLS):
        out = {}
        for cell in cells:
            tr = good_trades(n_per_window=40) if (passing and cell == U.CELLS[0]) else [T(-0.2, in_window(k, 3 + j), pair=U.CATALOG[j % 10]) for k in U.DEV_WINDOWS for j in range(30)]
            tr += [T(confirm_value if passing and cell == U.CELLS[0] else -0.3, in_window(k, 3 + j), pair=U.CATALOG[j % 10]) for k in U.CONFIRM_WINDOWS for j in range(25)]
            out[cell] = sorted(tr, key=U.order_key)
        return out, {(p, tf): E.RUN_START for p in U.CATALOG for tf in ("4h", "1d")}
    return f


@pytest.fixture()
def root(tmp_path, monkeypatch):
    write_root(tmp_path, years=3.5)
    monkeypatch.setattr(U, "data_hash", lambda r: "datahash0000")
    monkeypatch.setattr(U, "qualifying", lambda r: list(U.CATALOG))
    monkeypatch.setattr(U, "liquidity_table", lambda r: {s: {"years": 6.0, "median_daily_quote_volume": 1e9, "ok": True} for s in U.CATALOG})
    return tmp_path


def test_the_development_run_ignores_confirmation_window_trades(root, tmp_path_factory, monkeypatch):
    monkeypatch.setattr(U, "simulate", fake_sims(passing=False, confirm_value=-9.0))
    a = U.run_dev(root, tmp_path_factory.mktemp("a"))
    monkeypatch.setattr(U, "simulate", fake_sims(passing=False, confirm_value=+9.0))
    # fake_sims uses -0.3 for non-passing confirm trades; swap in a different value by rebuilding with passing cells irrelevant
    def other(root_, cells=U.CELLS):
        out, firsts = fake_sims(False)(root_, cells)
        return {c: [T(99.0, t.entry_ts, pair=t.pair) if E.window_of(t.entry_ts) in U.CONFIRM_WINDOWS else t for t in tr] for c, tr in out.items()}, firsts
    monkeypatch.setattr(U, "simulate", other)
    b = U.run_dev(root, tmp_path_factory.mktemp("b"))
    for cell in U.CELLS:
        assert a["rows"][cell]["trades"] == b["rows"][cell]["trades"] and a["rows"][cell]["total_net_r"] == b["rows"][cell]["total_net_r"]
        assert all(w["window"] in U.DEV_WINDOWS for w in a["rows"][cell]["per_window"])
    assert a["concurrency"][U.CELLS[0]] == b["concurrency"][U.CELLS[0]]


def test_nothing_passing_leaves_the_confirmation_windows_unread_and_unrecorded(root, tmp_path_factory, monkeypatch):
    monkeypatch.setattr(U, "simulate", fake_sims(passing=False))
    out = tmp_path_factory.mktemp("o")
    assert U.run_dev(root, out)["passing"] == []
    called = []
    monkeypatch.setattr(U, "simulate", lambda *a, **k: called.append(1))
    res = U.run_confirm(root, out, reason="x", confirm=True)
    assert res["opened"] is False and not called and not U.ledger_path(root).exists()
    assert "NOT read" in (out / "final_report.md").read_text()


def test_a_passing_cell_opens_the_confirmation_windows_once_and_records_the_look_first(root, tmp_path_factory, monkeypatch):
    monkeypatch.setattr(U, "simulate", fake_sims(passing=True))
    out = tmp_path_factory.mktemp("o")
    res = U.run_dev(root, out)
    assert res["passing"] == [H.cell_name(U.CELLS[0])]
    seen = {}

    def spy(r, cells=U.CELLS):
        seen["ledger_before_read"] = U.ledger_path(root).exists()
        seen["cells"] = [H.cell_name(c) for c in cells]
        return fake_sims(True)(r, cells)

    monkeypatch.setattr(U, "simulate", spy)
    c = U.run_confirm(root, out, reason="one look", confirm=True)
    assert c["opened"] and seen["ledger_before_read"] and seen["cells"] == [H.cell_name(U.CELLS[0])] and c["confirmed"][H.cell_name(U.CELLS[0])] is True
    assert len(json.loads(U.ledger_path(root).read_text())["datahash0000"]) == 1
    with pytest.raises(U.SealedError, match="already read once"):
        U.run_confirm(root, out, reason="again", confirm=True)
    c2 = U.run_confirm(root, out, reason="again", confirm=True, second_look=True)
    assert c2["contaminated"] and "SECOND LOOK" in (out / "final_report.md").read_text()


def test_every_refusal(root, tmp_path_factory, monkeypatch):
    monkeypatch.setattr(U, "simulate", fake_sims(passing=True))
    out = tmp_path_factory.mktemp("o")
    with pytest.raises(U.SealedError, match="no selected.json"):
        U.run_confirm(root, out, reason="x", confirm=True)
    with pytest.raises(U.SealedError, match="does not match"):
        U.run_dev(root, out, expect_plan_hash="0000")
    U.run_dev(root, out, expect_plan_hash=U.plan_hash())
    with pytest.raises(U.SealedError, match="--confirm"):
        U.run_confirm(root, out, reason="x", confirm=False)
    with pytest.raises(U.SealedError, match="needs a reason"):
        U.run_confirm(root, out, reason=" ", confirm=True)
    with pytest.raises(U.SealedError, match="does not match"):
        U.run_confirm(root, out, reason="x", confirm=True, expect_plan_hash="0000")
    monkeypatch.setattr(U, "data_hash", lambda r: "other")
    with pytest.raises(U.SealedError, match="data_hash differs"):
        U.run_confirm(root, out, reason="x", confirm=True)
    monkeypatch.setattr(U, "data_hash", lambda r: "datahash0000")
    monkeypatch.setitem(U.RULE, "dev_min_trades", 151)
    with pytest.raises(U.SealedError, match="plan_hash differs"):
        U.run_confirm(root, out, reason="x", confirm=True)
    monkeypatch.setitem(U.RULE, "dev_min_trades", 150)
    U.ledger_path(root).write_text("{not json")
    with pytest.raises(U.SealedError, match="unreadable"):
        U.run_confirm(root, out, reason="x", confirm=True)


def test_the_run_refuses_when_the_qualifying_list_is_not_the_registered_list(tmp_path, monkeypatch):
    monkeypatch.setattr(U, "qualifying", lambda r: list(U.CATALOG[:9]))
    with pytest.raises(U.SealedError, match="not the registered list"):
        U.run_dev(tmp_path, tmp_path / "o")


def test_the_real_simulation_runs_end_to_end_on_synthetic_candles(tmp_path):
    write_root(tmp_path, years=7.0, seed=11)
    sims, firsts = U.simulate(tmp_path)
    assert set(sims) == set(U.CELLS) and all(firsts[(p, "4h")] is not None for p in U.CATALOG)
    n4, n1 = len(sims[U.CELLS[0]]), len(sims[U.CELLS[2]])
    assert n4 > n1 > 0
    assert all(sims[c] == sorted(sims[c], key=U.order_key) for c in U.CELLS) and {t.pair for t in sims[U.CELLS[0]]} <= set(U.CATALOG)


def test_concentration_boundary_is_exactly_the_registered_number_of_positive_pairs():
    def build(positive):
        out = []
        for k in U.DEV_WINDOWS:
            for j, pair in enumerate(U.CATALOG):
                out += [T(0.5 if j < positive else -0.01, in_window(k, 2 + j * 3 + i), pair=pair) for i in range(3)]
        return out
    assert ev(build(6))["P4"] and not ev(build(5))["P4"]


def test_positions_held_are_clipped_at_both_ends_of_the_span():
    a = 1_000_000 * DAY // 1000
    c = U.concurrency([T(0.1, a + 8 * DAY, hold=5 * DAY)], a, a + 10 * DAY)
    assert c["mean_open"] == pytest.approx(0.2) and c["share_time_ge1"] == pytest.approx(0.2)


def test_the_first_signal_wins_a_tie_by_catalog_order():
    t = E._ms("2023-01-01")
    trades = [T(0.1, t, pair="SOLUSDT"), T(0.1, t, pair="BTCUSDT"), T(0.1, t - HOUR, pair="LTCUSDT")]
    assert [x.pair for x in sorted(trades, key=U.order_key)] == ["LTCUSDT", "BTCUSDT", "SOLUSDT"]


def test_volume_is_read_over_the_last_365_days_only(tmp_path):
    write_root(tmp_path, years=4.0, volume=1e5 / 24, only=("BTCUSDT",))
    p = tmp_path / "klines" / "1h" / "BTCUSDT.csv"
    rows = p.read_text().splitlines()
    for i in range(1, len(rows) - 24 * 400):  # everything older than 400 days is huge
        parts = rows[i].split(",")
        parts[6] = "1e9"
        rows[i] = ",".join(parts)
    p.write_text("\n".join(rows) + "\n")
    assert not U.liquidity_table(tmp_path)["BTCUSDT"]["ok"]
