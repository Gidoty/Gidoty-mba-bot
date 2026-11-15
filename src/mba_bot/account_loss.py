"""Account-wide daily loss pause (docs/safety/account_daily_loss.md).

A LIVE account stops opening NEW trades for the rest of its local calendar day once its equity is `ACCOUNT_DAILY_LOSS_PCT` percent below the equity it started that
day with. Equity is the exchange balance plus open positions at mark (realised and unrealised), see `exchange.fetch_account_equity`. Open trades are never touched:
their stops and exits keep running. The pause ends by itself at the next local midnight and is separate from the per-strategy daily loss guard and from any
cumulative stop.

Pure rules (`day_key`, `next_day_start`, `daily_loss_hit`) first, then the small amount of database work. Fail closed: an unreadable equity, a non-finite number or an
invalid time zone name means "no new entry", never "assume it is fine"."""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AccountDayEquity, AccountLossPause

logger = logging.getLogger(__name__)

KIND_DAILY_LOSS = "daily_loss"
R_ACCOUNT_DAILY_PAUSE = "account_daily_loss_pause"
R_EQUITY_UNREADABLE = "account_equity_unreadable"
R_TIMEZONE_INVALID = "account_day_timezone_invalid"

# The start of a day is the last equity sample before midnight only if it is this recent; otherwise it is the first read of the day (flagged approximate).
SAMPLE_MAX_AGE = timedelta(hours=1)

CUSTOMER_MESSAGE_PAUSE = (
    "New trades are paused for the rest of today because your account is down by the daily loss limit since the start of the day. "
    "Open positions keep their protective stops and exits. Trading resumes at the start of the next day."
)
CUSTOMER_MESSAGE_UNREADABLE = "Could not read your account balance just now, so no new trade was opened. This will retry automatically on the next check."
CUSTOMER_MESSAGE_TIMEZONE = "No new trade was opened because the service's day-boundary setting is invalid. The operator has been alerted."


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def zone(name: str) -> ZoneInfo | None:
    """The time zone, or None if the name is invalid or the system has no tz database for it."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError, TypeError):
        return None


def local_date(now: datetime, tz: ZoneInfo) -> date:
    return _aware(now).astimezone(tz).date()


def day_key(now: datetime, tz: ZoneInfo) -> str:
    return local_date(now, tz).isoformat()


def day_start(now: datetime, tz: ZoneInfo) -> datetime:
    """Local 00:00 of `now`'s local day, as an aware UTC instant."""
    return datetime.combine(local_date(now, tz), time.min, tzinfo=tz).astimezone(timezone.utc)


def next_day_start(now: datetime, tz: ZoneInfo) -> datetime:
    """Local 00:00 of the next local day, as an aware UTC instant. Computed from the calendar date, so it is right on a day of any length."""
    return datetime.combine(local_date(now, tz) + timedelta(days=1), time.min, tzinfo=tz).astimezone(timezone.utc)


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


@dataclass(frozen=True)
class DailyLossHit:
    loss_pct: float
    start_equity: float
    equity: float


def daily_loss_hit(*, start_equity, equity, limit_pct) -> DailyLossHit | None:
    """None when the day's loss is under the limit or the guard is off (limit_pct <= 0). A hit when equity is `limit_pct` percent or more below the start.
    A non-finite or non-positive start, or a non-finite equity, is a HIT (fail closed). The comparison tolerates float noise (1e-9)."""
    if not _finite(limit_pct) or limit_pct <= 0:
        return None
    if not (_finite(start_equity) and start_equity > 0 and _finite(equity)):
        return DailyLossHit(loss_pct=float("nan"), start_equity=float(start_equity) if _finite(start_equity) else float("nan"),
                            equity=float(equity) if _finite(equity) else float("nan"))
    loss_pct = (start_equity - equity) / start_equity * 100.0
    if loss_pct >= limit_pct - 1e-9:
        return DailyLossHit(loss_pct=loss_pct, start_equity=float(start_equity), equity=float(equity))
    return None


# ---------------------------------------------------------------------------------------------------- database


def active_pause(session: Session, account_id: str, now: datetime, tz: ZoneInfo) -> AccountLossPause | None:
    """The pause written for the account's current local day, if any. A pause row stays in force until its `ends_at`, whatever equity does meanwhile."""
    row = session.execute(
        select(AccountLossPause).where(
            AccountLossPause.account_id == account_id, AccountLossPause.kind == KIND_DAILY_LOSS, AccountLossPause.day_key == day_key(now, tz)
        )
    ).scalar_one_or_none()
    if row is not None and _aware(row.ends_at) > _aware(now):
        return row
    return None


def record_sample(session: Session, account_id: str, equity: float, now: datetime, tz: ZoneInfo) -> AccountDayEquity:
    """Store this equity reading and return the day's reference row, creating it on the first reading of the local day.

    A new day starts from the previous day's last sample if it is recent (within SAMPLE_MAX_AGE of the boundary), so a loss that happened while the service was
    down just after midnight is still measured from where the day began. Otherwise the first reading of the day is the start and the row is flagged approximate."""
    key = day_key(now, tz)
    row = session.execute(
        select(AccountDayEquity).where(AccountDayEquity.account_id == account_id, AccountDayEquity.day_key == key)
    ).scalar_one_or_none()
    if row is None:
        boundary = day_start(now, tz)
        prev = session.execute(
            select(AccountDayEquity)
            .where(AccountDayEquity.account_id == account_id, AccountDayEquity.day_key < key)
            .order_by(AccountDayEquity.day_key.desc())
            .limit(1)
        ).scalar_one_or_none()
        if (prev is not None and prev.last_equity is not None and prev.last_equity_at is not None and _finite(prev.last_equity) and prev.last_equity > 0
                and boundary - SAMPLE_MAX_AGE <= _aware(prev.last_equity_at) <= boundary):
            start, approximate = float(prev.last_equity), False
        else:
            start, approximate = float(equity), True
        row = AccountDayEquity(account_id=account_id, day_key=key, start_equity=start, approximate=approximate, captured_at=_aware(now))
        session.add(row)
    row.last_equity = float(equity)
    row.last_equity_at = _aware(now)
    session.flush()
    return row


def write_pause(session: Session, account_id: str, hit: DailyLossHit, *, limit_pct: float, now: datetime, tz: ZoneInfo, tz_name: str) -> AccountLossPause:
    """The audit row for today's pause. Idempotent: a second call the same local day returns the existing row."""
    key = day_key(now, tz)
    row = session.execute(
        select(AccountLossPause).where(AccountLossPause.account_id == account_id, AccountLossPause.kind == KIND_DAILY_LOSS, AccountLossPause.day_key == key)
    ).scalar_one_or_none()
    if row is not None:
        return row
    row = AccountLossPause(
        account_id=account_id, kind=KIND_DAILY_LOSS, day_key=key, started_at=_aware(now), ends_at=next_day_start(now, tz),
        start_equity=hit.start_equity if _finite(hit.start_equity) else 0.0, equity_at_hit=hit.equity if _finite(hit.equity) else 0.0,
        loss_pct=hit.loss_pct if _finite(hit.loss_pct) else 100.0, limit_pct=float(limit_pct), timezone_name=tz_name[:40],
    )
    session.add(row)
    session.flush()
    return row
