import pytest

from mba_bot.risk import (
    InvalidRiskParameters,
    apply_exposure_ceiling,
    apply_margin_ceiling,
    clamp_risk_pct,
    compute_position_size,
    compute_stop_loss,
)


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


# --- apply_exposure_ceiling: caps notional, never raises it ---


def test_apply_exposure_ceiling_caps_oversized_notional_from_a_tight_stop():
    # A tight stop (0.1 away) blows the quantity way up for the same
    # dollar risk - exactly the unbounded-notional scenario this ceiling
    # exists to catch.
    size = compute_position_size(account_balance=1000.0, risk_pct=1.0, entry_price=100.0, stop_price=99.9, max_risk_pct=2.0)
    assert size.quantity == pytest.approx(100.0)  # notional = 10,000, 10x effective_capital

    capped = apply_exposure_ceiling(size, entry_price=100.0, effective_capital=1000.0, max_notional_multiplier=1.0)
    assert capped.quantity == pytest.approx(10.0)  # notional capped at 1000 * 1.0
    assert capped.risk_amount == pytest.approx(1.0)  # recomputed from the capped quantity, not the original 10


def test_apply_exposure_ceiling_is_a_noop_when_already_within_the_ceiling():
    size = compute_position_size(account_balance=1000.0, risk_pct=1.0, entry_price=100.0, stop_price=96.0, max_risk_pct=2.0)
    result = apply_exposure_ceiling(size, entry_price=100.0, effective_capital=1000.0, max_notional_multiplier=1.0)
    assert result == size


def test_apply_exposure_ceiling_rejects_non_positive_multiplier():
    size = compute_position_size(account_balance=1000.0, risk_pct=1.0, entry_price=100.0, stop_price=96.0, max_risk_pct=2.0)
    with pytest.raises(InvalidRiskParameters):
        apply_exposure_ceiling(size, entry_price=100.0, effective_capital=1000.0, max_notional_multiplier=0)


# --- apply_margin_ceiling: Futures-only, uses the real read-only leverage ---


def test_apply_margin_ceiling_caps_when_required_margin_exceeds_capital():
    size = compute_position_size(account_balance=1000.0, risk_pct=1.0, entry_price=100.0, stop_price=99.9, max_risk_pct=2.0)
    # notional = 10,000; at 5x leverage that needs 2,000 margin - more
    # than the 1,000 the customer actually allocated.
    capped = apply_margin_ceiling(size, entry_price=100.0, effective_capital=1000.0, leverage=5.0)
    assert capped.quantity == pytest.approx(50.0)  # capped notional 5,000 / price 100
    assert capped.risk_amount == pytest.approx(50.0 * size.stop_distance)


def test_apply_margin_ceiling_is_a_noop_when_required_margin_fits():
    size = compute_position_size(account_balance=1000.0, risk_pct=1.0, entry_price=100.0, stop_price=96.0, max_risk_pct=2.0)
    result = apply_margin_ceiling(size, entry_price=100.0, effective_capital=1000.0, leverage=1.0)
    assert result == size


def test_apply_margin_ceiling_rejects_non_positive_leverage():
    size = compute_position_size(account_balance=1000.0, risk_pct=1.0, entry_price=100.0, stop_price=96.0, max_risk_pct=2.0)
    with pytest.raises(InvalidRiskParameters):
        apply_margin_ceiling(size, entry_price=100.0, effective_capital=1000.0, leverage=0)
