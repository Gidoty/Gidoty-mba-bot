import pytest

from mba_bot.risk import InvalidRiskParameters, clamp_risk_pct, compute_position_size, compute_stop_loss


def test_clamp_risk_pct_caps_at_max():
    assert clamp_risk_pct(5.0, max_risk_pct=2.0) == 2.0
    assert clamp_risk_pct(1.5, max_risk_pct=2.0) == 1.5


def test_clamp_risk_pct_rejects_non_positive():
    with pytest.raises(InvalidRiskParameters):
        clamp_risk_pct(0, max_risk_pct=2.0)


def test_compute_stop_loss_long_is_below_entry():
    stop = compute_stop_loss(entry_price=100.0, atr=2.0, side="long", atr_multiplier=2.0)
    assert stop == 96.0


def test_compute_stop_loss_short_is_above_entry():
    stop = compute_stop_loss(entry_price=100.0, atr=2.0, side="short", atr_multiplier=2.0)
    assert stop == 104.0


def test_compute_stop_loss_rejects_bad_side():
    with pytest.raises(InvalidRiskParameters):
        compute_stop_loss(entry_price=100.0, atr=2.0, side="sideways", atr_multiplier=2.0)


def test_compute_stop_loss_rejects_stop_going_non_positive():
    with pytest.raises(InvalidRiskParameters):
        compute_stop_loss(entry_price=10.0, atr=20.0, side="long", atr_multiplier=1.0)


def test_position_size_risks_exactly_the_target_amount():
    size = compute_position_size(
        account_balance=10_000.0, risk_pct=1.0, entry_price=100.0, stop_price=96.0, max_risk_pct=2.0
    )
    assert size.risk_amount == 100.0  # 1% of 10,000
    assert size.stop_distance == 4.0
    assert size.quantity == pytest.approx(25.0)  # 100 / 4
    # if the stop is hit, loss == quantity * stop_distance == risk_amount
    assert size.quantity * size.stop_distance == pytest.approx(size.risk_amount)


def test_position_size_clamps_risk_pct_before_sizing():
    uncapped = compute_position_size(
        account_balance=10_000.0, risk_pct=10.0, entry_price=100.0, stop_price=96.0, max_risk_pct=2.0
    )
    capped = compute_position_size(
        account_balance=10_000.0, risk_pct=2.0, entry_price=100.0, stop_price=96.0, max_risk_pct=2.0
    )
    assert uncapped.quantity == capped.quantity


def test_position_size_rejects_equal_entry_and_stop():
    with pytest.raises(InvalidRiskParameters):
        compute_position_size(account_balance=10_000.0, risk_pct=1.0, entry_price=100.0, stop_price=100.0, max_risk_pct=2.0)
