"""Short-timeframe research (docs/research/shortframe_preregistration.md): the simulators, the cost model, the metrics, the pass rule, the plan hash, the one-look ledger and the public archive reader.

PROVES: the vectorised EMA signal equals strategy.generate_signal on the same candles; the Donchian and RSI simulators give the same trades as the existing 1h runners when fed 1h candles (so only the timeframe
and the costs differ); the slices used for speed do not change a result; costs, metrics and the rule are computed as registered; the plan hash moves when anything it covers moves; the confirmation window cannot be
read without --confirm, twice, after a hash change, or when nothing qualified; the archive reader only reads one public host. DOES NOT PROVE: that any strategy works on any timeframe (that is what the run measures)."""
from __future__ import annotations

import io
import json
import random
import zipfile
from pathlib import Path

import pytest

from mba_bot import donchian_backtest as DB
from mba_bot import rsi_backtest as RB
from mba_bot import shortframe_backtest as SB
from mba_bot.research import archive
from mba_bot.research.data import KLINE_FIELDS
from mba_bot.strategy import Candle, SignalType, generate_signal

HOUR = 3_600_000


def walk(n, tf_ms=HOUR, seed=1, start=1_700_000_000_000, vol=100.0, regimes=False):
    rnd = random.Random(seed)
    price, out = 100.0, []
    for i in range(n):
        o = price
        sigma = 0.004 * ((0.15 if (i // 90) % 2 else 1.0) if regimes else 1.0)  # with regimes: calm and wild stretches, so the flat-market filter actually binds
        c = o * (1 + rnd.gauss(0, sigma))
        h, lo = max(o, c) * (1 + abs(rnd.gauss(0, 0.002))), min(o, c) * (1 - abs(rnd.gauss(0, 0.002)))
        out.append(Candle(start + i * tf_ms, o, h, lo, c, vol * (1 + abs(rnd.gauss(0, 1.2)))))
        price = c
    return out


# ================================================================================ the simulators agree with the live and existing code
def test_the_vectorised_ema_signal_matches_generate_signal_on_every_candle():
    kw = dict(ema_fast_period=20, ema_slow_period=50, atr_period=14, atr_flat_threshold=0.75, atr_rolling_window=50)
    flat_seen = signals_seen = 0
    for seed in (3, 4, 5):
        cs = walk(900, seed=seed, regimes=True)
        sigs, _ = SB.ema_signal_series(cs)
        for i in range(60, len(cs)):
            ref = generate_signal(cs[: i + 1], **kw)
            want = {SignalType.BULLISH_CROSSOVER: "long", SignalType.BEARISH_CROSSOVER: "short"}.get(ref.signal)
            assert sigs[i] == want, (seed, i)
            signals_seen += want is not None
            flat_seen += "flat" in (ref.reason or "")
    assert signals_seen > 20 and flat_seen > 0, "the series must include signals AND crossovers the flat-market filter removed"
    trades, _ = SB.simulate_ema("X", walk(900, seed=3, regimes=True), "1h", 0, 10**18)
    assert trades


def test_donchian_trades_equal_the_existing_hourly_runner_apart_from_costs():
    cs = walk(2500, seed=5, vol=100.0)
    ours, u1 = SB.simulate_donchian("X", cs, "1h", cs[0].timestamp, cs[-1].timestamp + HOUR)
    ref, u2 = DB.simulate("X", cs, DB.DN.DonchianParams(), "futures", cs[0].timestamp, cs[-1].timestamp + HOUR)
    assert ours and [(t.side, t.entry_ts, t.exit_ts, t.reason) for t in ours] == [(t.side, t.entry_ts, t.exit_ts, t.reason) for t in ref] and u1 == u2


def test_rsi_trades_equal_the_existing_hourly_runner_apart_from_costs():
    cs = walk(4000, seed=1)  # this series includes an RSI-line exit, the one exit that depends on the RSI value itself
    ours, u1 = SB.simulate_rsi("X", cs, "1h", cs[0].timestamp, cs[-1].timestamp + HOUR)
    ref, u2 = RB.simulate("X", cs, RB.R.RsiParams(), "futures", cs[0].timestamp, cs[-1].timestamp + HOUR)
    assert any(t.reason == "rsi_exit" for t in ours)
    assert ours and [(t.side, t.entry_ts, t.exit_ts, t.reason) for t in ours] == [(t.side, t.entry_ts, t.exit_ts, t.reason) for t in ref] and u1 == u2


def test_the_speed_slice_does_not_change_a_donchian_result(monkeypatch):
    cs = walk(2500, seed=11)
    full = SB.simulate_donchian("X", cs, "1h", cs[0].timestamp, cs[-1].timestamp + HOUR)
    monkeypatch.setattr(SB, "EXIT_SLICE", 40)  # a tiny slice: trades that outlive it are re-read on the whole series
    assert SB.simulate_donchian("X", cs, "1h", cs[0].timestamp, cs[-1].timestamp + HOUR) == full


def test_the_timeframe_changes_only_the_candle_length_not_the_rules(monkeypatch):
    a = walk(1800, tf_ms=300_000, seed=2)
    b = [Candle(c.timestamp // 300_000 * 3_600_000, c.open, c.high, c.low, c.close, c.volume) for c in a]  # the same shapes stamped as 1h
    for sim in (SB.simulate_ema, SB.simulate_donchian, SB.simulate_rsi):
        monkeypatch.setattr(SB, "DONCHIAN_COOLDOWN_MS", 4 * 300_000)  # the Donchian cooldown is in real time (4 hours); here it is set to the same 4 CANDLES in both runs
        t5, _ = sim("X", a, "5m", a[0].timestamp, a[-1].timestamp + 300_000)
        monkeypatch.setattr(SB, "DONCHIAN_COOLDOWN_MS", 4 * HOUR)
        t1, _ = sim("X", b, "1h", b[0].timestamp, b[-1].timestamp + HOUR)
        assert [(t.side, round(t.net_r, 9), t.reason) for t in t5] == [(t.side, round(t.net_r, 9), t.reason) for t in t1]


# ================================================================================ costs, metrics, the rule
def test_the_cost_model_charges_slippage_against_the_trade_and_the_taker_fee_on_both_sides():
    t = SB._cost_trade("X", "long", 1, 2, 100.0, 110.0, lambda e: e - 5.0, "x", HOUR)
    slip, fee = SB.SLIP_BPS_PER_SIDE / 1e4, SB.FEE_BPS_PER_SIDE / 1e4
    entry, exit_ = 100 * (1 + slip), 110 * (1 - slip)
    dist = 5.0
    assert t.entry == pytest.approx(entry) and t.exit == pytest.approx(exit_) and t.gross_r == pytest.approx(10.0 / dist)
    assert t.net_r == pytest.approx(((exit_ - entry) - fee * (entry + exit_)) / dist) and t.net_r < t.gross_r
    s = SB._cost_trade("X", "short", 1, 2, 100.0, 90.0, lambda e: e + 5.0, "x", HOUR)
    assert s.entry == pytest.approx(100 * (1 - slip)) and s.exit == pytest.approx(90 * (1 + slip)) and s.net_r < s.gross_r
    assert SB._cost_trade("X", "long", 1, 2, 100.0, 110.0, lambda e: e + 5.0, "x", HOUR) is None  # a stop on the wrong side is refused
    assert SB.SLIPPAGE_ROUND_TRIP_BPS == 11.0 and SB.FEE_BPS_PER_SIDE == 5.0


def _t(net, gross=None, exit_ts=0, hold=3_600_000):
    return SB.SimTrade("X", "long", 0, exit_ts, 100, 101, 99, "r", net + 0.2 if gross is None else gross, net, hold)


def test_metrics_report_expectancy_costs_streaks_trades_per_day_and_holding_time():
    trades = [_t(1.0, gross=1.8, exit_ts=1), _t(-1.0, exit_ts=2), _t(-0.5, exit_ts=3), _t(-0.5, exit_ts=4), _t(2.0, exit_ts=5)]  # gross 2.6 in total, net 1.0, costs 1.6
    m = SB.metrics(trades, 0, 10 * SB.DAY_MS)
    assert m["trades"] == 5 and m["trades_per_day"] == pytest.approx(0.5) and m["win_rate"] == pytest.approx(0.4)
    assert m["net_expectancy_r"] == pytest.approx(0.2) and m["gross_expectancy_r"] == pytest.approx(0.52) and m["cost_r_per_trade"] == pytest.approx(0.32)
    assert m["cost_share_of_gross"] == pytest.approx(1.6 / 2.6) and m["worst_losing_streak"] == 3 and m["avg_hold_minutes"] == pytest.approx(60.0)
    none = SB.metrics([], 0, SB.DAY_MS)
    assert none["trades"] == 0 and none["net_expectancy_r"] is None
    nogross = SB.metrics([_t(-1.0, gross=-0.5, exit_ts=1)], 0, SB.DAY_MS)
    assert nogross["cost_share_of_gross"] is None  # no gross edge: the share is undefined and the rule cannot be met


def _cell(trades=200, exp=0.1, share=0.3, positive=4):
    cell = {s: {"total_net_r": 1.0 if k < positive else -1.0} for k, s in enumerate(SB.PAIRS)}
    cell["ALL"] = {"trades": trades, "net_expectancy_r": exp, "cost_share_of_gross": share}
    return cell


def test_the_pass_rule_needs_every_part():
    assert SB.gate(_cell())["passes"]
    for bad, part in ((_cell(trades=99), "enough_trades"), (_cell(exp=0.049), "net_expectancy"), (_cell(share=0.51), "costs_leave_the_edge"), (_cell(share=None), "costs_leave_the_edge"),
                      (_cell(positive=2), "positive_pairs")):
        g = SB.gate(bad)
        assert not g["passes"] and not g["parts"][part]
    assert SB.gate(_cell(trades=100, exp=0.05, share=0.5, positive=3))["passes"]  # the edges are inclusive


def test_the_windows_do_not_overlap_have_an_embargo_and_stop_before_the_old_sealed_months():
    assert SB.CONFIRM[0] - SB.TUNE[1] == SB.EMBARGO_DAYS * SB.DAY_MS and SB.TUNE[0] < SB.TUNE[1] < SB.CONFIRM[0] < SB.CONFIRM[1]
    assert SB.CONFIRM[1] <= SB._ms("2026-09-15")  # nothing from the earlier backtests' sealed months is read


# ================================================================================ the plan hash
def test_the_plan_hash_moves_with_the_rule_the_costs_the_windows_and_the_pinned_sources(monkeypatch):
    base = SB.plan_hash()
    assert len(base) == 16 and SB.plan_hash() == base
    monkeypatch.setitem(SB.GATE, "min_trades", 99)
    assert SB.plan_hash() != base
    monkeypatch.setitem(SB.GATE, "min_trades", 100)
    assert SB.plan_hash() == base
    monkeypatch.setattr(SB, "FEE_BPS_PER_SIDE", 4.0)
    assert SB.plan_hash() != base
    monkeypatch.setattr(SB, "FEE_BPS_PER_SIDE", 5.0)
    monkeypatch.setattr(SB, "TUNE", (SB.TUNE[0], SB.TUNE[1] + 1))
    assert SB.plan_hash() != base
    monkeypatch.undo()
    real = SB.source_pins
    monkeypatch.setattr(SB, "source_pins", lambda: {**real(), "donchian.py": "changed"})
    assert SB.plan_hash() != base  # a strategy file edited after registration changes the hash


# ================================================================================ tune, confirm and the one-look ledger
def _store(tmp_path, n=1500):
    for tf, ms in SB.TF_MS.items():
        d = tmp_path / "klines" / tf
        d.mkdir(parents=True)
        for k, sym in enumerate(SB.PAIRS):
            cs = walk(n, tf_ms=ms, seed=20 + k, start=SB.TUNE[0] - 200 * ms)
            with open(d / f"{sym}.csv", "w", newline="") as fh:
                fh.write(",".join(KLINE_FIELDS) + "\n")
                for c in cs:
                    fh.write(f"{c.timestamp},{c.open},{c.high},{c.low},{c.close},{c.volume},0,0,0\n")
    return tmp_path


@pytest.fixture
def small(monkeypatch, tmp_path):
    root = _store(tmp_path / "data")
    monkeypatch.setattr(SB, "TUNE", (SB.TUNE[0], SB.TUNE[0] + 200 * 300_000))  # small windows so the synthetic files suffice
    monkeypatch.setattr(SB, "CONFIRM", (SB.TUNE[1] + SB.EMBARGO_DAYS * SB.DAY_MS, SB.TUNE[1] + SB.EMBARGO_DAYS * SB.DAY_MS + 300 * 300_000))
    return root, tmp_path / "out"


def _everything_qualifies(monkeypatch):
    monkeypatch.setitem(SB.GATE, "min_trades", 0)
    monkeypatch.setitem(SB.GATE, "min_net_expectancy_r", -1e9)
    monkeypatch.setitem(SB.GATE, "max_cost_share_of_gross", 1e9)
    monkeypatch.setitem(SB.GATE, "min_positive_pairs", 0)


def test_nothing_qualifies_then_the_confirmation_window_is_not_read_and_no_look_is_recorded(small, monkeypatch):
    root, out = small
    monkeypatch.setitem(SB.GATE, "min_trades", 10**9)
    res = SB.run_tune(root, out)
    assert res["qualifying"] == [] and "NONE" in (out / "tune_report.md").read_text()
    opened = SB.run_confirm(root, out, reason="why", confirm=True)
    assert opened["opened"] is False and not SB.ledger_path(root).exists()
    assert "NOT read" in (out / "final_report.md").read_text() and "no timeframe" in (out / "final_report.md").read_text().lower()


def test_the_confirmation_window_needs_confirm_a_reason_the_same_plan_and_the_same_data_and_is_read_once(small, monkeypatch):
    root, out = small
    _everything_qualifies(monkeypatch)
    SB.run_tune(root, out, expect_plan_hash=SB.plan_hash())
    with pytest.raises(SB.SealedError, match="--confirm"):
        SB.run_confirm(root, out, reason="x", confirm=False)
    with pytest.raises(SB.SealedError, match="reason"):
        SB.run_confirm(root, out, reason=" ", confirm=True)
    with pytest.raises(SB.SealedError, match="does not match"):
        SB.run_confirm(root, out, reason="x", confirm=True, expect_plan_hash="0" * 16)
    first = SB.run_confirm(root, out, reason="the planned look", confirm=True, expect_plan_hash=SB.plan_hash())
    assert first["opened"] and not first["contaminated"] and (out / "final_report.md").exists()
    assert {"ema|1h", "donchian|1h", "rsi|1h"} <= set(first["rows"])  # the 1h controls are always shown
    ledger = json.loads(SB.ledger_path(root).read_text())
    assert len(next(iter(ledger.values()))) == 1
    with pytest.raises(SB.SealedError, match="already read once"):
        SB.run_confirm(root, out, reason="again", confirm=True)
    again = SB.run_confirm(root, out, reason="again", confirm=True, second_look=True)
    assert again["contaminated"] and "SECOND LOOK" in (out / "final_report.md").read_text()


def test_a_changed_plan_or_changed_candles_after_tuning_refuse_the_confirmation(small, monkeypatch):
    root, out = small
    _everything_qualifies(monkeypatch)
    SB.run_tune(root, out)
    monkeypatch.setattr(SB, "FEE_BPS_PER_SIDE", 4.0)
    with pytest.raises(SB.SealedError, match="plan_hash differs"):
        SB.run_confirm(root, out, reason="x", confirm=True)
    monkeypatch.setattr(SB, "FEE_BPS_PER_SIDE", 5.0)
    with open(root / "klines" / "5m" / "BTCUSDT.csv", "a") as fh:
        fh.write("9999999999999,1,1,1,1,1,0,0,0\n")
    with pytest.raises(SB.SealedError, match="data_hash differs"):
        SB.run_confirm(root, out, reason="x", confirm=True)
    assert not SB.ledger_path(root).exists()  # a refusal never records a look


def test_the_report_states_the_rule_the_costs_and_the_upper_bound_caveat(small):
    root, out = small
    SB.run_tune(root, out)
    text = (out / "tune_report.md").read_text()
    for needle in ("Plan hash", "5 bps per side", "11 bps round-trip", "UPPER BOUND", "cost share of gross", "worst losing streak", "avg hold", "per day"):
        assert needle in text


# ================================================================================ the public archive reader
def _month_zip(rows):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("X.csv", "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore\n" + "\n".join(rows))
    return buf.getvalue()


def test_the_archive_reader_writes_the_store_layout_skips_stored_bars_and_reports_a_missing_month(tmp_path):
    jan = _month_zip(["1000,1,2,0.5,1.5,10,1299,15,5,4,6,0", "2000,1.5,2,1,1.6,11,2299,16,6,5,7,0"])
    feb = _month_zip(["2000,1.5,2,1,1.6,11,2299,16,6,5,7,0", "3000,1.6,2,1,1.7,12,3299,17,7,6,8,0"])
    seen = []

    def fetch(url):
        seen.append(url)
        return {"2026-01": jan, "2026-02": feb}.get(url.split("-")[-2] + "-" + url.split("-")[-1][:2])

    out = archive.download(tmp_path, ["BTCUSDT"], "5m", ["2026-01", "2026-02", "2026-03"], fetch=fetch)
    assert out["added"] == {"BTCUSDT": 3} and out["missing"] == ["BTCUSDT 5m 2026-03"]
    lines = (tmp_path / "klines" / "5m" / "BTCUSDT.csv").read_text().splitlines()
    assert lines[0].split(",") == KLINE_FIELDS and [l.split(",")[0] for l in lines[1:]] == ["1000", "2000", "3000"] and lines[1].split(",")[6] == "15"
    again = archive.download(tmp_path, ["BTCUSDT"], "5m", ["2026-01", "2026-02"], fetch=fetch)
    assert again["added"] == {"BTCUSDT": 0}  # resumable: nothing duplicated
    assert all(u.startswith("https://data.binance.vision/data/futures/um/monthly/klines/BTCUSDT/5m/") for u in seen)


def test_the_archive_reader_refuses_other_hosts_intervals_symbols_and_months():
    with pytest.raises(ValueError):
        archive._default_fetch("https://example.com/x.zip")
    with pytest.raises(ValueError):
        archive._default_fetch("http://data.binance.vision/x.zip")
    for args in (("BTCUSDT", "7s", "2026-01"), ("btc usdt", "5m", "2026-01"), ("BTCUSDT", "5m", "2026-13"), ("BTCUSDT", "5m", "../x")):
        with pytest.raises(ValueError):
            archive.archive_url(*args)
    src = Path(archive.__file__).read_text()
    assert "POST" not in src and "os.environ" not in src and "apiKey" not in src


def test_the_archive_loaded_store_is_readable_by_the_runner(tmp_path):
    rows = [f"{1_700_000_000_000 + i * 300_000},100,101,99,100,5,{1_700_000_000_000 + i * 300_000 + 299_999},500,10,2,200,0" for i in range(5)]
    archive.download(tmp_path, ["ETHUSDT"], "5m", ["2026-01"], fetch=lambda u: _month_zip(rows))
    cs = SB._store(tmp_path, "ETHUSDT", "5m")
    assert len(cs) == 5 and cs[0].open == 100 and cs[0].volume == 5


def test_nothing_in_the_trading_service_imports_the_research_runner():
    src = Path(SB.__file__).parent
    offenders = [p.name for p in src.rglob("*.py") if ("shortframe_backtest" in p.read_text() or "research.archive" in p.read_text() or "research import archive" in p.read_text())
                 and p.name not in ("shortframe_backtest.py", "archive.py", "profit_backtest.py", "profit_backtest_dr.py", "entry_study.py")]  # profit_backtest.py is a research runner too (the sealed test of profit protection v2)
    assert offenders == []
    trading = [p.name for p in src.glob("*.py") if p.name not in ("shortframe_backtest.py", "profit_backtest.py", "profit_backtest_dr.py", "entry_study.py") and "profit_backtest" in p.read_text()]
    assert trading == []  # nothing in the trading service imports the profit-protection research runner either


def test_the_donchian_cooldown_after_a_stop_exit_skips_the_signals_inside_it(monkeypatch):
    cs = [Candle(1_700_000_000_000 + i * 300_000, 100, 101, 99, 100, 1.0) for i in range(120)]
    sig = SB.DN.DonchianSignal("long", "x", close=100.0, upper=99.0, lower=95.0, mid=97.0, volume=2.0, avg_volume=1.0, atr=1.0, atr_pct=0.01, atr_pct_avg=0.01, stop=None)
    monkeypatch.setattr(SB.DN, "signal_at", lambda closed, atr_values, i, p, allow_short=False: sig)
    monkeypatch.setattr(SB.DN, "exit_check", lambda **k: SB.DN.Exit(SB.DN.EXIT_STOP, k["entry"] - 1.0, k["opened_ms"]))  # every trade is stopped out on its entry candle
    monkeypatch.setattr(SB, "DONCHIAN_COOLDOWN_MS", 0)
    free, _ = SB.simulate_donchian("X", cs, "5m", cs[0].timestamp, cs[-1].timestamp + 300_000)
    monkeypatch.setattr(SB, "DONCHIAN_COOLDOWN_MS", 10 * 300_000)
    cooled, _ = SB.simulate_donchian("X", cs, "5m", cs[0].timestamp, cs[-1].timestamp + 300_000)
    assert len(cooled) < len(free) // 2 and all(b.entry_ts - a.exit_ts >= 10 * 300_000 for a, b in zip(cooled, cooled[1:]))


def test_the_rsi_warm_up_slice_gives_the_same_exit_as_the_whole_history():
    cs = walk(2500, seed=13)
    p = SB.R.RsiParams()
    checked = 0
    for i in range(900, 2300, 7):
        entry = cs[i + 1].open
        stop = SB.R.stop_price(entry, 1.0, "long", p)
        horizon = min(len(cs), i + 2 + p.time_stop_candles + 1)
        full = SB.R.exit_check(side="long", entry=entry, stop=stop, opened_ms=cs[i + 1].timestamp, closed=cs[:horizon], tf_ms=HOUR, p=p)
        part = SB.R.exit_check(side="long", entry=entry, stop=stop, opened_ms=cs[i + 1].timestamp, closed=cs[max(0, i + 1 - SB.RSI_WARMUP):horizon], tf_ms=HOUR, p=p)
        assert (full and (full.reason, full.candle_ts)) == (part and (part.reason, part.candle_ts))
        assert full is None or abs(full.price - part.price) < 1e-6
        checked += 1
    assert checked > 150


def test_the_rsi_exit_always_gets_the_full_warm_up_before_the_entry(monkeypatch):
    cs = walk(3000, seed=1)
    seen = []
    real = SB.R.exit_check

    def spy(**k):
        seen.append((k["opened_ms"], k["closed"][0].timestamp))
        return real(**k)

    monkeypatch.setattr(SB.R, "exit_check", spy)
    SB.simulate_rsi("X", cs, "1h", cs[0].timestamp, cs[-1].timestamp + HOUR)
    assert seen and all(opened - first == SB.RSI_WARMUP * HOUR for opened, first in seen if opened - cs[0].timestamp >= SB.RSI_WARMUP * HOUR)
