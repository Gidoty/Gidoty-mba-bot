"""Audit L2: every non-finite input (NaN or infinity) to a pure guard rule is a HIT (fail closed), when the rule is switched on. A rule that is switched off
(threshold 0) stays off. Finite inputs behave exactly as before (the existing boundary tests)."""
from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from mba_bot import strategy_guard_rules as R

NAN, INF = float("nan"), float("inf")
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("bad", [NAN, INF, -INF])
def test_ema_gap(bad):
    assert R.ema_gap_hit(bad, 0.2) is not None and R.ema_gap_hit(bad, 0.0) is None


@pytest.mark.parametrize("bad", [NAN, INF, -INF])
def test_fee_guard_each_input(bad):
    ok = dict(atr_pct=0.01, margin=1.0, fee_bps=5.0, slippage_bps=11.0, expected_move_atr=1.0)
    assert R.fee_guard_hit(**ok) is None
    for key in ("atr_pct", "margin", "fee_bps", "slippage_bps", "expected_move_atr"):
        assert R.fee_guard_hit(**{**ok, key: bad}) is not None, key
    assert R.fee_guard_hit(**{**ok, "margin": 0.0, "atr_pct": bad}) is None  # switched off


@pytest.mark.parametrize("bad", [NAN, INF])
def test_portfolio(bad):
    ok = dict(open_positions=1, open_risk_pct=1.0, new_risk_pct=0.5, max_positions=3, max_total_risk_pct=2.0)
    assert R.portfolio_hit(**ok) is None
    for key in ("open_risk_pct", "new_risk_pct"):
        assert R.portfolio_hit(**{**ok, key: bad}) is not None, key
    assert R.portfolio_hit(**{**ok, "open_risk_pct": bad, "max_total_risk_pct": 0.0}) is None  # the total-risk cap is off


@pytest.mark.parametrize("bad", [NAN, INF, -INF])
def test_daily_loss(bad):
    assert R.daily_loss_hit(net_pnl_today=bad, capital=1000.0, limit_pct=3.0) is not None
    assert R.daily_loss_hit(net_pnl_today=bad, capital=1000.0, limit_pct=0.0) is None
    assert R.daily_loss_hit(net_pnl_today=-1.0, capital=INF, limit_pct=3.0) is not None


@pytest.mark.parametrize("bad", [NAN, INF, -INF])
def test_loss_streak(bad):
    assert R.loss_streak_hit([1.0, bad, -1.0], 3, 24.0) is not None  # a NaN used to END the streak (and so fail open)
    assert R.loss_streak_hit([-1.0, bad], 2, 24.0).creates_pause
    assert R.loss_streak_hit([bad], 0, 24.0) is None
    assert R.loss_streak_hit([1.0, -1.0], 3, 24.0) is None


@pytest.mark.parametrize("bad", [NAN, INF, -INF])
def test_expectancy(bad):
    assert R.expectancy_hit([0.5] * 19 + [bad], 20) is not None
    assert R.expectancy_hit([0.5] * 19 + [bad], 0) is None
    assert R.expectancy_hit([0.5] * 19, 20) is None  # still not enough trades to judge
    assert R.expectancy_hit([bad] + [0.5] * 40, 20).creates_pause  # a bad value inside the window pauses until the owner releases it


@pytest.mark.parametrize("bad", [NAN, INF])
def test_cooldown_hours(bad):
    assert R.cooldown_hit(NOW, NOW, bad) is not None
    assert R.cooldown_hit(NOW, NOW, 0.0) is None and R.cooldown_hit(None, NOW, 4.0) is None


def test_consecutive_losses_counts_a_non_finite_result_as_a_loss():
    assert R.consecutive_losses([NAN, -1.0, 2.0]) == 2 and R.consecutive_losses([INF, -1.0]) == 2


def test_the_docs_name_the_current_hash():
    import hashlib
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    sha = hashlib.sha256((root / "src/mba_bot/strategy_guard_rules.py").read_bytes()).hexdigest()
    assert sha in (root / "docs/safety/strategy_guards.md").read_text()
