"""Month arithmetic for crypto subscriptions (billing/periods.py): the only place it lives."""
from datetime import datetime, timedelta, timezone

import pytest

from mba_bot.billing.periods import add_months, to_utc

U = timezone.utc


def d(*a):
    return datetime(*a, tzinfo=U)


@pytest.mark.parametrize("start,months,expected", [
    (d(2026, 1, 31, 10, 5, 7), 1, d(2026, 2, 28, 10, 5, 7)),      # 31 Jan -> end of a short February
    (d(2028, 1, 31, 10), 1, d(2028, 2, 29, 10)),                  # leap year: 29 Feb
    (d(2028, 2, 29, 23, 59, 59), 12, d(2029, 2, 28, 23, 59, 59)),  # 29 Feb + a year
    (d(2026, 3, 31), 1, d(2026, 4, 30)),                          # 31 Mar -> 30 Apr
    (d(2026, 4, 30, 8), 1, d(2026, 5, 30, 8)),                    # 30 Apr -> 30 May (not 31)
    (d(2026, 5, 31), 1, d(2026, 6, 30)),
    (d(2026, 12, 15, 1, 2, 3), 1, d(2027, 1, 15, 1, 2, 3)),       # year rollover
    (d(2026, 12, 31), 2, d(2027, 2, 28)),
    (d(2026, 1, 29), 1, d(2026, 2, 28)),
    (d(2026, 1, 30), 1, d(2026, 2, 28)),
    (d(2026, 6, 15), 0, d(2026, 6, 15)),
    (d(2026, 3, 31), -1, d(2026, 2, 28)),
    (d(2026, 1, 15, 12), 24, d(2028, 1, 15, 12)),
])
def test_calendar_month_arithmetic(start, months, expected):
    assert add_months(start, months) == expected


def test_one_month_is_not_thirty_days():
    for start in (d(2026, 1, 31), d(2026, 2, 1), d(2026, 3, 15), d(2026, 7, 31), d(2026, 12, 1)):
        assert add_months(start, 1) - start in (timedelta(days=28), timedelta(days=29), timedelta(days=30), timedelta(days=31))
    assert add_months(d(2026, 2, 1), 1) - d(2026, 2, 1) == timedelta(days=28)
    assert add_months(d(2026, 7, 1), 1) - d(2026, 7, 1) == timedelta(days=31)


def test_time_of_day_and_microseconds_are_kept():
    t = datetime(2026, 1, 31, 10, 5, 7, 123456, tzinfo=U)
    assert add_months(t, 1) == datetime(2026, 2, 28, 10, 5, 7, 123456, tzinfo=U)


def test_naive_is_utc_and_other_zones_are_converted():
    assert add_months(datetime(2026, 1, 31, 10), 1) == d(2026, 2, 28, 10)
    plus2 = timezone(timedelta(hours=2))
    # 00:30 on 1 Feb in UTC+2 is 22:30 on 31 Jan UTC; the arithmetic is in UTC
    assert add_months(datetime(2026, 2, 1, 0, 30, tzinfo=plus2), 1) == d(2026, 2, 28, 22, 30)
    assert to_utc(datetime(2026, 1, 1)).tzinfo is U


def test_years_out_of_range_raise():
    with pytest.raises(ValueError):
        add_months(d(9999, 12, 1), 1)
    with pytest.raises(ValueError):
        add_months(d(1, 1, 1), -1)


def test_month_arithmetic_lives_only_here():
    """The Paystack path must not compute months (its end is Paystack's own date); only periods.py and its caller do."""
    from pathlib import Path
    src = Path(__file__).resolve().parent.parent / "src" / "mba_bot" / "billing"
    for name in ("subscriptions.py", "paystack_sub.py", "notices.py", "entitlement.py"):
        assert "add_months" not in (src / name).read_text()
    text = (src / "nowpayments.py").read_text()
    for banned in ("relativedelta", "monthrange", "days=30", "days=31", "timedelta(weeks"):
        assert banned not in text
    assert text.count("add_months(") == 1  # one use: the new period end
