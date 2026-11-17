"""The customer's daily loss limit in dollars (docs/safety/account_daily_loss.md). Pure rules first, then the small amount of database work.

  * The customer types an amount in USDT. The screen suggests about SUGGESTED_PCT percent of the account value and shows the percent next to the amount.
  * The amount must be above 0 and below the dollar level of the automatic total stop (risk_limits.TOTAL_STOP_PCT of the reference value), so the daily limit can never be higher than the total stop.
  * Lowering the amount applies at once. RAISING it is stored as pending and starts at the next midnight of the account day (the pending day key), so a customer cannot raise the limit in
    the middle of a bad day. Setting the same amount cancels a pending raise.
  * An existing live account with no amount gets a one-time default of DEFAULT_PCT percent of its last stored account value (or of its allocated capital), marked as a default until the
    customer changes it.
Numbers only; nothing here reads or writes an exchange."""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import risk_limits
from .models import AccountDayEquity, TradingAccount

logger = logging.getLogger(__name__)

SUGGESTED_PCT = 5.0
DEFAULT_PCT = 10.0
_EPS = 1e-9


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def total_stop_level_usd(reference_value) -> float | None:
    """The dollar loss at which the total stop acts: TOTAL_STOP_PCT of the reference value. None when there is no usable reference."""
    if not (_finite(reference_value) and reference_value > 0):
        return None
    return float(reference_value) * risk_limits.TOTAL_STOP_PCT / 100.0


def suggested_usd(account_value) -> float | None:
    if not (_finite(account_value) and account_value > 0):
        return None
    return round(float(account_value) * SUGGESTED_PCT / 100.0, 2)


def pct_of(amount, account_value) -> float | None:
    if not (_finite(amount) and _finite(account_value) and account_value > 0):
        return None
    return float(amount) / float(account_value) * 100.0


def validate_amount(amount, *, reference_value) -> str | None:
    """A plain customer-safe problem text, or None. The amount must be a number above 0 and below the total stop level of the reference value (when there is one)."""
    if not _finite(amount) or amount <= 0:
        return "Your daily loss limit must be an amount above 0."
    level = total_stop_level_usd(reference_value)
    if level is not None and amount >= level - _EPS:
        return f"Your daily loss limit must be below {level:.2f} USDT, the level where the automatic total stop acts ({risk_limits.TOTAL_STOP_PCT:g}% of your account value)."
    return None


def one_trade_can_exceed(risk_pct, capital, amount) -> bool:
    """True when one trade can lose more than the daily limit: risk % x trading capital > amount. Unknown numbers give False (nothing to warn about yet)."""
    if not (_finite(risk_pct) and _finite(capital) and _finite(amount) and capital > 0 and amount > 0):
        return False
    return float(risk_pct) / 100.0 * float(capital) > float(amount) + _EPS


def default_amount(last_value, allocated_capital) -> float | None:
    """DEFAULT_PCT of the last stored account value, else of the allocated capital, else None (the customer must then set one)."""
    for base in (last_value, allocated_capital):
        if _finite(base) and base > 0:
            return round(float(base) * DEFAULT_PCT / 100.0, 2)
    return None


@dataclass(frozen=True)
class Resolved:
    amount: float | None
    pending_amount: float | None
    pending_from: str | None


def resolve(amount, pending_amount, pending_from, today_key: str) -> Resolved:
    """The amount in force for the local day `today_key`: a pending raise whose start day has arrived replaces the amount. Day keys are YYYY-MM-DD, so string order is date order.
    Does not write anything."""
    if _finite(pending_amount) and pending_amount > 0 and pending_from and pending_from <= today_key:
        return Resolved(float(pending_amount), None, None)
    pend = (float(pending_amount), pending_from) if _finite(pending_amount) and pending_amount > 0 and pending_from else (None, None)
    return Resolved(float(amount) if _finite(amount) and amount > 0 else None, pend[0], pend[1])


def apply_pending(account: TradingAccount, today_key: str) -> bool:
    """Promote a due pending raise on the account row. Returns True if the row changed."""
    r = resolve(account.daily_loss_limit_usd, account.daily_loss_limit_pending_usd, account.daily_loss_limit_pending_from, today_key)
    if account.daily_loss_limit_pending_usd is not None and r.pending_amount is None and r.amount is not None:
        account.daily_loss_limit_usd = r.amount
        account.daily_loss_limit_pending_usd = None
        account.daily_loss_limit_pending_from = None
        return True
    return False


def set_amount(account: TradingAccount, new_amount: float, *, today_key: str, next_day_key: str) -> str:
    """Record the customer's amount. Returns "set" (first amount, or lower: in force now), "unchanged" (same as in force; any pending raise is cancelled) or "pending" (a raise: starts on
    next_day_key). The caller validates the number first."""
    apply_pending(account, today_key)
    new_amount = float(new_amount)
    current = account.daily_loss_limit_usd
    account.daily_loss_limit_is_default = False  # the customer has now chosen (or confirmed) it
    if not _finite(current) or current <= 0 or new_amount < current - _EPS:
        account.daily_loss_limit_usd = new_amount
        account.daily_loss_limit_pending_usd = None
        account.daily_loss_limit_pending_from = None
        return "set"
    if abs(new_amount - current) <= _EPS:
        account.daily_loss_limit_pending_usd = None
        account.daily_loss_limit_pending_from = None
        return "unchanged"
    account.daily_loss_limit_pending_usd = new_amount
    account.daily_loss_limit_pending_from = next_day_key
    return "pending"


def reference_value(account: TradingAccount, session: Session | None = None) -> float | None:
    """The account value the percentages and the total stop level are measured against: the total stop's reference (balance when first checked or after a reset), else the last stored equity
    sample, else the allocated capital. None when none of them is a usable number."""
    if _finite(account.loss_reference_equity) and account.loss_reference_equity > 0:
        return float(account.loss_reference_equity)
    last = last_stored_value(session, account.id) if session is not None else None
    if last is not None:
        return last
    if _finite(account.allocated_capital) and account.allocated_capital > 0:
        return float(account.allocated_capital)
    return None


def last_stored_value(session: Session, account_id: str) -> float | None:
    row = session.execute(
        select(AccountDayEquity).where(AccountDayEquity.account_id == account_id, AccountDayEquity.last_equity.is_not(None)).order_by(AccountDayEquity.day_key.desc()).limit(1)
    ).scalar_one_or_none()
    if row is not None and _finite(row.last_equity) and row.last_equity > 0:
        return float(row.last_equity)
    return None


def ensure_defaults(session: Session) -> int:
    """One-time default for every live-authorised account that has no amount (the same rule for every account): DEFAULT_PCT of its last stored account value, else of its allocated
    capital. Idempotent: an account with an amount is never touched, so a second run changes nothing. An account with neither number stays without an amount (it then takes no new entry
    until the customer sets one). Logs one line per account it changed, with the account's first 8 characters and the amount."""
    changed = 0
    rows = session.execute(
        select(TradingAccount).where(TradingAccount.live_trading_enabled_at.is_not(None), TradingAccount.daily_loss_limit_usd.is_(None))
    ).scalars().all()
    for account in rows:
        amount = default_amount(last_stored_value(session, account.id), account.allocated_capital)
        if amount is None:
            logger.warning("daily loss limit: account %s has no stored value or capital, no default set", account.id[:8])
            continue
        account.daily_loss_limit_usd = amount
        account.daily_loss_limit_is_default = True
        changed += 1
        logger.warning("daily loss limit: default of %.2f USDT set for account %s (one time)", amount, account.id[:8])
    session.flush()
    return changed


def run_startup_default() -> int:
    """The startup step: open a session, run ensure_defaults, commit. Never raises (a database that has not been migrated yet must not stop the service from starting): a failure is one
    warning line with the exception class only."""
    from .db import session_scope

    try:
        with session_scope() as session:
            return ensure_defaults(session)
    except Exception as exc:  # noqa: BLE001
        logger.warning("daily loss limit: the one-time default step did not run (%s); run the migration, then restart", type(exc).__name__)
        return 0
