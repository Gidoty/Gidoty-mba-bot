"""Repeat-failure limits and alert/log de-duplication for live entries (no schema change).

A live entry whose protective stop cannot be established is closed again straight away by an emergency exit. While
the cause persists, the next scheduler round re-enters, so an account can loop (23 such round trips on one pair were
seen in under two hours). The emergency exit records WHY in the trade's `close_reason`
(lifecycle.classify_protection_failure), and this module counts those exits from the trades table so the entry path
can refuse a further entry once a limit is reached. It only reads; it never closes, cancels or changes anything.

Three categories, three scopes (see Settings for the numbers and why):

* structural: the stop cannot be placed. Deterministic, so a small count per account AND pair.
* transient: exchange/API trouble. Not pair-specific, so counted per ACCOUNT across pairs, over a short window.
* unclassified: unknown cause, often an unmapped rejection tied to one symbol: per account AND pair.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from mba_bot.models import Trade

# The single source of the close_reason strings (lifecycle imports them).
EMERGENCY_REASON_STRUCTURAL = "protection_failed_emergency_exit"  # unchanged: historical rows carry it
EMERGENCY_REASON_TRANSIENT = "protection_transient_emergency_exit"
EMERGENCY_REASON_OTHER = "protection_unclassified_emergency_exit"

# Kept for callers that only care about the structural category.
EMERGENCY_EXIT_REASON = EMERGENCY_REASON_STRUCTURAL

_ALERTED: dict[tuple[str, str, str], datetime] = {}


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def count_failures(
    session, account_id: str, *, reason: str, now: datetime, window_hours: float, pair: str | None = None
) -> int:
    """Live trades on this account (and on `pair`, when given) closed with exactly `reason` inside the window."""
    since = _aware(now) - timedelta(hours=window_hours)
    conditions = [
        Trade.account_id == account_id,
        Trade.is_paper.is_(False),
        Trade.close_reason == reason,
        Trade.closed_at >= since,
    ]
    if pair is not None:
        conditions.append(Trade.pair == pair)
    return int(session.execute(select(func.count()).select_from(Trade).where(*conditions)).scalar() or 0)


def recent_protection_failures(session, account_id: str, pair: str, *, now: datetime, window_hours: float) -> int:
    """Structural failures on this account and pair inside the window."""
    return count_failures(session, account_id, reason=EMERGENCY_REASON_STRUCTURAL, now=now, window_hours=window_hours, pair=pair)


def limit_reached(session, account_id: str, pair: str, *, now: datetime, limit: int, window_hours: float) -> int | None:
    """The structural failure count if the limit is on and reached, else None. A broken lookup never blocks trading."""
    if limit <= 0:
        return None
    try:
        count = recent_protection_failures(session, account_id, pair, now=now, window_hours=window_hours)
    except Exception:  # noqa: BLE001
        return None
    return count if count >= limit else None


@dataclass(frozen=True)
class LimitHit:
    kind: str  # "structural" | "transient" | "unclassified"
    reason_code: str  # the decision-log reason code
    failures: int
    limit: int
    window_hours: float
    scope: str  # "account_pair" | "account"


def limit_hits(session, account_id: str, pair: str, *, now: datetime, settings, codes: dict[str, str]) -> list[LimitHit]:
    """Every limit currently reached for this account/pair, structural first. A broken lookup of one category never
    blocks trading and never hides the others. `codes` maps kind -> decision-log reason code."""
    specs = (
        ("structural", EMERGENCY_REASON_STRUCTURAL, settings.protection_fail_limit, settings.protection_fail_window_hours, pair, "account_pair"),
        ("transient", EMERGENCY_REASON_TRANSIENT, settings.protection_transient_limit, settings.protection_transient_window_hours, None, "account"),
        ("unclassified", EMERGENCY_REASON_OTHER, settings.protection_unclassified_limit, settings.protection_unclassified_window_hours, pair, "account_pair"),
    )
    hits: list[LimitHit] = []
    for kind, reason, limit, window, scope_pair, scope in specs:
        if limit <= 0:
            continue
        try:
            count = count_failures(session, account_id, reason=reason, now=now, window_hours=window, pair=scope_pair)
        except Exception:  # noqa: BLE001
            continue
        if count >= limit:
            hits.append(LimitHit(kind, codes[kind], count, limit, window, scope))
    return hits


def should_alert(account_id: str, pair: str, reason: str, *, now: datetime, repeat_hours: float) -> bool:
    """True at most once per (account, pair, reason) per `repeat_hours`. In memory: one extra alert after a restart.
    Also used (with an "observe:" reason prefix) to keep observe-mode journal lines from repeating every round."""
    key = (account_id, pair, reason)
    last = _ALERTED.get(key)
    now = _aware(now)
    if last is not None and now - last < timedelta(hours=repeat_hours):
        return False
    _ALERTED[key] = now
    return True


def reset_for_tests() -> None:
    _ALERTED.clear()
