"""Paper-trading trial and the live-trading bypass disclosure.

Every account starts in a 7-day paper trial on testnet. It can move to live
trading two ways: the trial simply expires, or the customer explicitly
bypasses it by accepting a disclosure - which this module logs with a
timestamp and a copy of the disclosure text they agreed to, so there's a
durable record of informed consent if it's ever disputed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

LIVE_TRADING_DISCLOSURE = (
    "I am choosing to skip the recommended 7-day paper trading trial and "
    "start live trading immediately with real funds. I understand that "
    "trading carries the risk of financial loss, that MBA Bot's signals "
    "are not guaranteed to be profitable, and that I am solely "
    "responsible for any losses incurred."
)


class TradingMode(Enum):
    PAPER = "paper"
    LIVE = "live"


@dataclass(frozen=True)
class TrialState:
    mode: TradingMode
    trial_ends_at: datetime
    bypassed: bool
    bypass_accepted_at: datetime | None


def start_trial(*, now: datetime | None = None, trial_days: int) -> datetime:
    """Called once at signup. Returns the trial end timestamp to store on
    the new account (alongside `mode = paper`)."""
    now = now or datetime.now(timezone.utc)
    return now + timedelta(days=trial_days)


def accept_live_bypass(*, now: datetime | None = None) -> datetime:
    """Called when the customer checks the bypass box. Returns the
    timestamp to store as `live_bypass_accepted_at` on their account,
    alongside the exact disclosure text (`LIVE_TRADING_DISCLOSURE`) shown
    to them - store both so a later change to the disclosure wording
    doesn't retroactively alter what a past customer agreed to."""
    return now or datetime.now(timezone.utc)


def resolve_trading_mode(
    *,
    trial_ends_at: datetime,
    live_bypass_accepted_at: datetime | None,
    now: datetime | None = None,
) -> TrialState:
    """The single source of truth for "is this account allowed to place
    real orders right now". Fails safe: any ambiguity resolves to PAPER,
    never LIVE - an account only trades live when it has explicitly
    earned it, either by finishing the trial or by a logged bypass.
    """
    now = now or datetime.now(timezone.utc)
    # SQLite drops tzinfo on round-trip (it stores DateTime as a plain
    # string) even though the column is declared timezone-aware; a value
    # just loaded from the DB can come back naive. Every datetime this
    # module works with is UTC by convention, so treat a naive one as UTC
    # rather than letting the comparison below raise.
    if trial_ends_at.tzinfo is None:
        trial_ends_at = trial_ends_at.replace(tzinfo=timezone.utc)

    if live_bypass_accepted_at is not None:
        return TrialState(
            mode=TradingMode.LIVE,
            trial_ends_at=trial_ends_at,
            bypassed=True,
            bypass_accepted_at=live_bypass_accepted_at,
        )

    if now >= trial_ends_at:
        return TrialState(mode=TradingMode.LIVE, trial_ends_at=trial_ends_at, bypassed=False, bypass_accepted_at=None)

    return TrialState(mode=TradingMode.PAPER, trial_ends_at=trial_ends_at, bypassed=False, bypass_accepted_at=None)
