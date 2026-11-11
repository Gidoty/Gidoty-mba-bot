"""The ONLY place calendar-month arithmetic lives (designs/03 addendum 5, section 5).

Paystack subscriptions never use this: their period end is Paystack's own `next_payment_date`, and a source test forbids month
arithmetic in `subscriptions.py` and `paystack_sub.py`. A NOWPayments subscription has no provider date (one invoice per month), so
the next period end is computed here, from an anchor, by exactly one calendar month. Pure functions, UTC only, no I/O."""
from __future__ import annotations

import calendar
from datetime import datetime, timezone


def to_utc(instant: datetime) -> datetime:
    """A naive datetime is UTC (everything is stored in UTC; SQLite hands datetimes back without a zone)."""
    return instant.replace(tzinfo=timezone.utc) if instant.tzinfo is None else instant.astimezone(timezone.utc)


def add_months(instant: datetime, months: int) -> datetime:
    """The same day of the month and time of day, `months` calendar months later, in UTC. If the target month is shorter, the last
    day of that month (31 Jan + 1 month is 28 Feb, or 29 Feb in a leap year; 31 Mar + 1 month is 30 Apr). Raises ValueError outside
    years 1 to 9999."""
    base = to_utc(instant)
    total = base.year * 12 + (base.month - 1) + int(months)
    year, month0 = divmod(total, 12)
    month = month0 + 1
    if not 1 <= year <= 9999:
        raise ValueError("year out of range")
    day = min(base.day, calendar.monthrange(year, month)[1])
    return base.replace(year=year, month=month, day=day)
