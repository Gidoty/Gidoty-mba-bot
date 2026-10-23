"""Risk study: the simulation compounds correctly, preserves order within blocks and scales drawdown with risk; the grid shows caps and minimums."""
from __future__ import annotations

import pytest

from mba_bot.research import risk_study as rs, synthetic
from mba_bot.research.engine import TradeRecord
from mba_bot.research.phase1 import Lab, Phase1Options

DAY = 86_400_000
T = 1_700_000_000_000


def trade(r, day):
    ts = T + day * DAY
    return TradeRecord(symbol="BTCUSDT", side="long", entry_ts=ts, exit_ts=ts + DAY, signal_price=100, entry_price=100, exit_price=100, stop_price=99, quantity=1, notional=100,
                       planned_risk=1, gross_pnl=r, slippage_cost=0, fees=0, funding=0, net_pnl=r, r_net=r, r_gross=r, mfe_r=0, mae_r=0, hold_hours=24, exit_reason="stop",
                       entry_signal="x", equity_before=1000)


def test_a_constant_r_compounds_exactly_and_never_draws_down_when_every_trade_wins():
    mc = rs.monte_carlo([trade(2.0, d) for d in range(0, 300, 3)], risk_pcts=(1.0, 2.0), horizon_trades=50, sims=50)
    assert mc["by_risk"]["1.0"]["median_final_multiple"] == pytest.approx(1.02 ** 50)
    assert mc["by_risk"]["2.0"]["median_final_multiple"] == pytest.approx(1.04 ** 50)
    assert mc["by_risk"]["2.0"]["max_drawdown_pct"][99] == 0 and mc["by_risk"]["2.0"]["prob_lose_half"] == 0


def test_a_run_of_losses_hurts_more_at_two_percent_and_ruin_needs_far_more_losses_at_one():
    trades = [trade(-1.0, d) for d in range(0, 300, 3)]  # every trade loses 1R
    mc = rs.monte_carlo(trades, risk_pcts=(1.0, 2.0), horizon_trades=50, sims=20)
    one, two = mc["by_risk"]["1.0"], mc["by_risk"]["2.0"]
    assert one["max_drawdown_pct"][50] == pytest.approx((1 - 0.99 ** 50) * 100) and two["max_drawdown_pct"][50] == pytest.approx((1 - 0.98 ** 50) * 100)
    # 50 straight losses: 0.99**50 = 0.605 (lost 40%), 0.98**50 = 0.364 (lost 64%)
    assert one["prob_lose_half"] == 0 and two["prob_lose_half"] == 1.0 and two["prob_lose_three_quarters"] == 0


def test_blocks_keep_the_order_and_clustering_of_losses():
    trades = [trade(-1.0, d) for d in range(0, 30)] + [trade(1.0, d) for d in range(60, 90)]
    blocks = rs.block_sequences(trades, block_days=30)
    assert [set(b) for b in blocks] == [{-1.0}, {1.0}]
    assert rs.monte_carlo([], horizon_trades=5, sims=5) == {"error": "no trades"}


def test_the_engine_grid_shows_exchange_minimums_and_caps_on_small_accounts(tmp_path):
    synthetic.write_store(tmp_path / "s", pairs=["BTC/USDT", "ETH/USDT"], days=120, seed=5)
    lab = Lab(tmp_path / "s", tmp_path / "o", Phase1Options())
    grid = rs.engine_grid(lab, ["BTCUSDT"], tiers=(50.0, 5000.0), risks=(1.0, 2.0))
    assert set(grid) == {"50|1", "50|2", "5000|1", "5000|2"}
    small, large = grid["50|1"], grid["5000|1"]
    assert small["blocked_exchange_filters"] + small["blocked_size_zero"] >= large["blocked_exchange_filters"] + large["blocked_size_zero"]
    assert large["entries"] > 0
    text = rs.render(rs.monte_carlo([trade(0.5, d) for d in range(0, 300, 3)], horizon_trades=20, sims=20), {"BTC": grid})
    assert "Risk study" in text and "P(lose half)" in text and "5000" in text


def test_the_risk_command_stays_inside_the_development_range(tmp_path, capsys):
    import json

    from mba_bot.research.__main__ import main

    store = tmp_path / "s"
    synthetic.write_store(store, pairs=["BTC/USDT", "ETH/USDT"], days=140, seed=8)
    from mba_bot.research import data

    h = data.load_candles(store, "BTCUSDT", "1h")
    first, last = h[0].timestamp, h[-1].timestamp + 3_600_000
    dev_end = last - 30 * DAY
    res = {"meta": {"plan_hash": "x", "span": [first, last], "development_range": [first, dev_end], "lockbox_range": [dev_end + 3 * DAY, last]}, "candidates": {}}
    (tmp_path / "p1.json").write_text(json.dumps(res))
    assert main(["risk", "--root", str(store), "--out", str(tmp_path / "o"), "--phase1-results", str(tmp_path / "p1.json"), "--pairs", "BTC/USDT,ETH/USDT", "--sims", "50", "--horizon-trades", "20"]) == 0
    out = json.loads((tmp_path / "o" / "risk_results.json").read_text())
    assert out["development_range"] == [first, dev_end] and out["n_trades"] > 0
    assert (tmp_path / "o" / "risk_report.md").exists() and len(out["grids"]) == 2


def test_telemetry_check_is_read_only_and_reports_state(tmp_path, capsys):
    from sqlalchemy import create_engine

    from mba_bot.models import Base
    from mba_bot.research import telemetry_check as tc
    from mba_bot.research.__main__ import main

    db = tmp_path / "t.db"
    Base.metadata.create_all(create_engine(f"sqlite:///{db}"))
    before = db.read_bytes()
    res = tc.check(str(db))
    assert res["tables"]["trade_telemetry"] and res["tables"]["decision_log"] and res["trades"] == 0 and res["telemetry_rows"] == 0 and res["alembic_revision"] is None
    assert main(["telemetry-check", "--database", str(db)]) == 0 and "Telemetry check" in capsys.readouterr().out
    assert db.read_bytes() == before
