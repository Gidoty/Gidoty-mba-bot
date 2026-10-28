"""Repeat-failure limit and alert de-duplication for live entries (no schema change).

A live entry whose protective stop cannot be placed is closed again straight away
("protection_failed_emergency_exit"). While the cause persists, the next scheduler round re-enters, so
an account can loop (23 such round trips on one pair were seen in under two hours). This module counts
those exits from the trades table and lets the entry path refuse a further entry on the same account and
pair once the limit is reached. It only reads; it never closes, cancels or changes anything.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from mba_bot.models import Trade

# Only the STRUCTURAL category (lifecycle.EMERGENCY_REASON_STRUCTURAL): the stop cannot be placed (unavailable, beyond
# liquidation, rejected by Binance). Transient exchange/API failures and unclassified ones close with different
# reasons and are deliberately not counted.
EMERGENCY_EXIT_REASON = "protection_failed_emergency_exit"

_ALERTED: dict[tuple[str, str, str], datetime] = {}


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def recent_protection_failures(session, account_id: str, pair: str, *, now: datetime, window_hours: float) -> int:
    """Live trades on this account and pair closed by a protection-failure emergency exit inside the window."""
    since = _aware(now) - timedelta(hours=window_hours)
    stmt = select(func.count()).select_from(Trade).where(
        Trade.account_id == account_id,
        Trade.pair == pair,
        Trade.is_paper.is_(False),
        Trade.close_reason == EMERGENCY_EXIT_REASON,
        Trade.closed_at >= since,
    )
    return int(session.execute(stmt).scalar() or 0)


def limit_reached(session, account_id: str, pair: str, *, now: datetime, limit: int, window_hours: float) -> int | None:
    """The failure count if the limit is on and reached, else None. A broken lookup never blocks trading."""
    if limit <= 0:
        return None
    try:
        count = recent_protection_failures(session, account_id, pair, now=now, window_hours=window_hours)
    except Exception:  # noqa: BLE001
        return None
    return count if count >= limit else None


def should_alert(account_id: str, pair: str, reason: str, *, now: datetime, repeat_hours: float) -> bool:
    """True at most once per (account, pair, reason) per `repeat_hours`. In memory: one extra alert after a restart."""
    key = (account_id, pair, reason)
    last = _ALERTED.get(key)
    now = _aware(now)
    if last is not None and now - last < timedelta(hours=repeat_hours):
        return False
    _ALERTED[key] = now
    return True


def reset_for_tests() -> None:
    _ALERTED.clear()
