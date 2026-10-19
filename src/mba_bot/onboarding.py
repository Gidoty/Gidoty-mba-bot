"""Paper-trading trial, the trial-bypass disclosure, and the separate
live-trading activation gate.

Every account starts in a 7-day paper trial on testnet - and stays there
indefinitely, free, with no expiry, until the customer pays. But paying,
finishing the trial, and accepting the bypass are ALL just eligibility -
none of them, alone or together, is allowed to put real money at risk.
`resolve_trading_mode` only ever returns LIVE when a customer has
explicitly flipped a separate, dedicated switch
(`live_trading_enabled_at`) AND a credential genuinely tagged "mainnet"
is on file. This split exists because of a real incident: an account
reached LIVE with a Binance *testnet* key still on file (payment and
trial expiry alone used to be sufficient), and Binance production
correctly rejected it. Eligibility and environment are now two separate
questions this module keeps two separate answers for.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

LIVE_TRADING_DISCLOSURE = (
    "I am choosing to skip the recommended 7-day paper trading trial and "
    "become eligible to trade live sooner. I understand that trading "
    "carries the risk of financial loss, that MBA Bot's signals are not "
    "guaranteed to be profitable, and that I am solely responsible for "
    "any losses incurred. I understand this does not by itself turn on "
    "live trading - I still have to explicitly enable it separately, "
    "with real Binance account credentials on file."
)

# Shown at the dedicated "Enable Live Trading" step - distinct from the
# disclosure above, which is only about skipping the wait. This one is
# about the one action that actually connects the bot to Binance
# production with real funds.
LIVE_TRADING_ACTIVATION_DISCLOSURE = (
    "I am enabling live trading on this account. I understand that from "
    "this point forward, MBA Bot will place real orders on Binance "
    "production using my own funds and the Binance API credentials I "
    "have provided. I understand that trading carries the risk of "
    "financial loss, that MBA Bot's signals are not guaranteed to be "
    "profitable, and that I am solely responsible for any losses "
    "incurred."
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
    """Called when the customer checks the trial-bypass box. Returns the
    timestamp to store as `live_bypass_accepted_at` on their account,
    alongside the exact disclosure text (`LIVE_TRADING_DISCLOSURE`) shown
    to them - store both so a later change to the disclosure wording
    doesn't retroactively alter what a past customer agreed to. This only
    ever grants *eligibility* to go live sooner than the 7-day trial -
    see `enable_live_trading` for the separate switch that actually does."""
    return now or datetime.now(timezone.utc)


def enable_live_trading(*, now: datetime | None = None) -> datetime:
    """Called when the customer confirms the dedicated "Enable Live
    Trading" step. Returns the timestamp to store as
    `live_trading_enabled_at`, alongside `LIVE_TRADING_ACTIVATION_DISCLOSURE`
    - the one and only thing that can ever make `resolve_trading_mode`
    return LIVE. The caller (api/routers/account.py) is responsible for
    checking eligibility and a valid mainnet credential *before* calling
    this; this function itself does no such check, same division of
    responsibility as `accept_live_bypass`."""
    return now or datetime.now(timezone.utc)


def start_bot(*, now: datetime | None = None) -> datetime:
    """Called when the customer confirms the dedicated "Start Bot" step
    - the sixth and final thing resolve_trading_mode checks, distinct
    from `enable_live_trading` above. Returns the timestamp to store as
    `bot_started_at`. The caller (api/routers/account.py) is
    responsible for checking every prerequisite (paid, eligible,
    mainnet credential, live trading enabled, trading capital
    configured, no open trade) *before* calling this; this function
    itself does no such check, same division of responsibility as
    `accept_live_bypass`/`enable_live_trading`."""
    return now or datetime.now(timezone.utc)


def is_live_eligible(*, trial_ends_at: datetime, live_bypass_accepted_at: datetime | None, now: datetime | None = None) -> bool:
    """Has the trial run its course, or did the customer explicitly
    accept the bypass? Pulled out of resolve_trading_mode so
    api/routers/account.py's enable-live-trading endpoint can check the
    same condition the same way, including the same tzinfo-naive-from-
    SQLite handling, rather than re-deriving it and risking drift."""
    now = now or datetime.now(timezone.utc)
    if trial_ends_at.tzinfo is None:
        trial_ends_at = trial_ends_at.replace(tzinfo=timezone.utc)
    return live_bypass_accepted_at is not None or now >= trial_ends_at


def resolve_trading_mode(
    *,
    trial_ends_at: datetime,
    live_bypass_accepted_at: datetime | None,
    has_paid: bool,
    live_trading_enabled_at: datetime | None,
    has_mainnet_credential: bool,
    bot_started_at: datetime | None,
    now: datetime | None = None,
) -> TrialState:
    """The single source of truth for "is this account allowed to place
    real orders right now". Fails safe: any ambiguity resolves to PAPER,
    never LIVE. Five independent conditions must ALL hold, checked in
    order, any one of them missing stops here at PAPER:

      1. `has_paid` - payment buys the *right* to use live trading, not
         live trading itself.
      2. Eligibility - the trial has run its course, or the customer
         explicitly accepted the bypass to skip waiting for it.
      3. `live_trading_enabled_at` is set - a dedicated, explicit action
         distinct from both of the above. Paying and/or finishing the
         trial NEVER sets this by themselves; nothing in billing.py or
         the trial-expiry check does either.
      4. `has_mainnet_credential` - re-checked every single call, not
         just when live trading was enabled. If the stored credential is
         later downgraded or removed, this makes LIVE fail closed again
         immediately on the account's very next cycle, with no separate
         "did someone remember to also clear live_trading_enabled_at"
         bookkeeping required (api/routers/account.py does that too, as
         a second independent safeguard - this one doesn't depend on it).
      5. `bot_started_at` is set - the explicit "Start Bot" action,
         distinct from `live_trading_enabled_at`. Accepting the risk
         disclosure and enabling live trading authorizes the account to
         go live; it does NOT itself start anything. Nothing except the
         dedicated start-bot endpoint (api/routers/account.py) ever sets
         this. A pre-existing account from before this condition existed
         has this as None, so it fails closed to PAPER the moment this
         check ships, regardless of how long it was already LIVE under
         the old four-condition chain - see the migration that adds this
         column.
    """
    now = now or datetime.now(timezone.utc)
    # SQLite drops tzinfo on round-trip (it stores DateTime as a plain
    # string) even though the column is declared timezone-aware; a value
    # just loaded from the DB can come back naive. Every datetime this
    # module works with is UTC by convention, so treat a naive one as UTC
    # rather than letting a comparison raise - is_live_eligible does the
    # same normalization for the same reason.
    if trial_ends_at.tzinfo is None:
        trial_ends_at = trial_ends_at.replace(tzinfo=timezone.utc)

    bypassed = live_bypass_accepted_at is not None
    paper = TrialState(mode=TradingMode.PAPER, trial_ends_at=trial_ends_at, bypassed=bypassed, bypass_accepted_at=live_bypass_accepted_at)

    if not has_paid:
        return paper

    if not is_live_eligible(trial_ends_at=trial_ends_at, live_bypass_accepted_at=live_bypass_accepted_at, now=now):
        return paper

    if live_trading_enabled_at is None:
        return paper

    if not has_mainnet_credential:
        return paper

    if bot_started_at is None:
        return paper

    return TrialState(mode=TradingMode.LIVE, trial_ends_at=trial_ends_at, bypassed=bypassed, bypass_accepted_at=live_bypass_accepted_at)
