from datetime import datetime, timedelta, timezone

from mba_bot.onboarding import TradingMode, accept_live_bypass, resolve_trading_mode, start_trial


def test_start_trial_is_n_days_from_now():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ends_at = start_trial(now=now, trial_days=7)
    assert ends_at == now + timedelta(days=7)


def test_within_trial_window_is_paper_mode():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    trial_ends_at = now + timedelta(days=3)
    state = resolve_trading_mode(trial_ends_at=trial_ends_at, live_bypass_accepted_at=None, now=now)
    assert state.mode == TradingMode.PAPER
    assert not state.bypassed


def test_trial_expiry_moves_to_live_without_bypass():
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    trial_ends_at = datetime(2026, 1, 8, tzinfo=timezone.utc)
    state = resolve_trading_mode(trial_ends_at=trial_ends_at, live_bypass_accepted_at=None, now=now)
    assert state.mode == TradingMode.LIVE
    assert not state.bypassed


def test_explicit_bypass_is_live_even_during_trial_window():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    trial_ends_at = now + timedelta(days=6)  # trial not over yet
    bypass_at = now
    state = resolve_trading_mode(trial_ends_at=trial_ends_at, live_bypass_accepted_at=bypass_at, now=now)
    assert state.mode == TradingMode.LIVE
    assert state.bypassed
    assert state.bypass_accepted_at == bypass_at


def test_accept_live_bypass_returns_a_timestamp():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert accept_live_bypass(now=now) == now
