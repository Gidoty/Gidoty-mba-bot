from datetime import datetime, timedelta, timezone

from mba_bot.onboarding import (
    TradingMode,
    accept_live_bypass,
    enable_live_trading,
    is_live_eligible,
    resolve_trading_mode,
    start_bot,
    start_trial,
)

NOW = datetime(2026, 1, 10, tzinfo=timezone.utc)
EXPIRED_TRIAL = datetime(2026, 1, 1, tzinfo=timezone.utc)  # already in the past relative to NOW
ACTIVE_TRIAL = NOW + timedelta(days=6)  # still running relative to NOW


def _resolve(**overrides):
    defaults = dict(
        trial_ends_at=ACTIVE_TRIAL,
        live_bypass_accepted_at=None,
        has_paid=False,
        live_trading_enabled_at=None,
        has_mainnet_credential=False,
        bot_started_at=None,
        now=NOW,
    )
    defaults.update(overrides)
    return resolve_trading_mode(**defaults)


def test_start_trial_is_n_days_from_now():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ends_at = start_trial(now=now, trial_days=7)
    assert ends_at == now + timedelta(days=7)


def test_accept_live_bypass_returns_a_timestamp():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert accept_live_bypass(now=now) == now


def test_enable_live_trading_returns_a_timestamp():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert enable_live_trading(now=now) == now


# --- is_live_eligible: trial clock / bypass, independent of everything else ---


def test_not_eligible_within_an_active_trial_with_no_bypass():
    assert is_live_eligible(trial_ends_at=ACTIVE_TRIAL, live_bypass_accepted_at=None, now=NOW) is False


def test_eligible_once_trial_has_expired():
    assert is_live_eligible(trial_ends_at=EXPIRED_TRIAL, live_bypass_accepted_at=None, now=NOW) is True


def test_eligible_via_bypass_even_during_an_active_trial():
    assert is_live_eligible(trial_ends_at=ACTIVE_TRIAL, live_bypass_accepted_at=NOW, now=NOW) is True


# --- resolve_trading_mode: the user's required scenario list ---


def test_unpaid_is_paper_regardless_of_anything_else():
    state = _resolve(
        has_paid=False,
        trial_ends_at=EXPIRED_TRIAL,
        live_bypass_accepted_at=NOW,
        live_trading_enabled_at=NOW,
        has_mainnet_credential=True,
    )
    assert state.mode == TradingMode.PAPER


def test_paid_but_still_within_trial_and_untouched_is_paper():
    state = _resolve(has_paid=True, trial_ends_at=ACTIVE_TRIAL)
    assert state.mode == TradingMode.PAPER


def test_paid_and_trial_expired_stays_paper_unless_explicitly_enabled():
    state = _resolve(has_paid=True, trial_ends_at=EXPIRED_TRIAL, live_trading_enabled_at=None, has_mainnet_credential=True)
    assert state.mode == TradingMode.PAPER


def test_paid_and_bypass_accepted_stays_paper_unless_explicitly_enabled():
    state = _resolve(
        has_paid=True,
        trial_ends_at=ACTIVE_TRIAL,
        live_bypass_accepted_at=NOW,
        live_trading_enabled_at=None,
        has_mainnet_credential=True,
    )
    assert state.mode == TradingMode.PAPER
    assert state.bypassed


def test_mainnet_credential_present_but_not_explicitly_authorized_stays_paper():
    state = _resolve(
        has_paid=True,
        trial_ends_at=EXPIRED_TRIAL,
        live_trading_enabled_at=None,
        has_mainnet_credential=True,
    )
    assert state.mode == TradingMode.PAPER


def test_explicit_authorization_without_a_valid_mainnet_credential_fails_closed():
    state = _resolve(
        has_paid=True,
        trial_ends_at=EXPIRED_TRIAL,
        live_trading_enabled_at=NOW,
        has_mainnet_credential=False,
    )
    assert state.mode == TradingMode.PAPER


def test_every_condition_satisfied_is_the_only_way_to_reach_live():
    state = _resolve(
        has_paid=True,
        trial_ends_at=EXPIRED_TRIAL,
        live_trading_enabled_at=NOW,
        has_mainnet_credential=True,
        bot_started_at=NOW,
    )
    assert state.mode == TradingMode.LIVE


def test_start_bot_returns_a_timestamp():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert start_bot(now=now) == now


def test_bot_not_started_stays_paper_even_when_every_other_condition_is_satisfied():
    # Everything the previous four-gate chain required is true - paid,
    # eligible, enabled, mainnet credential - but Start Bot was never
    # pressed. Must still be PAPER: this is the fifth gate, and it is
    # what makes a pre-existing (pre-migration) account fail closed
    # rather than silently staying live the moment this column ships.
    state = _resolve(
        has_paid=True,
        trial_ends_at=EXPIRED_TRIAL,
        live_trading_enabled_at=NOW,
        has_mainnet_credential=True,
        bot_started_at=None,
    )
    assert state.mode == TradingMode.PAPER


def test_trial_expiry_alone_can_never_reach_live():
    # Expired trial, paid, mainnet credential present - everything except
    # the one dedicated switch. Must still be PAPER.
    state = _resolve(has_paid=True, trial_ends_at=EXPIRED_TRIAL, live_trading_enabled_at=None, has_mainnet_credential=True)
    assert state.mode == TradingMode.PAPER


def test_bypass_alone_can_never_reach_live():
    state = _resolve(
        has_paid=True,
        trial_ends_at=ACTIVE_TRIAL,
        live_bypass_accepted_at=NOW,
        live_trading_enabled_at=None,
        has_mainnet_credential=True,
    )
    assert state.mode == TradingMode.PAPER


def test_live_requires_reaching_eligibility_too_not_just_paid_and_enabled():
    # Paid, explicitly enabled, valid credential - but still inside the
    # trial window with no bypass. The activation switch alone can't
    # skip eligibility.
    state = _resolve(
        has_paid=True,
        trial_ends_at=ACTIVE_TRIAL,
        live_bypass_accepted_at=None,
        live_trading_enabled_at=NOW,
        has_mainnet_credential=True,
    )
    assert state.mode == TradingMode.PAPER
