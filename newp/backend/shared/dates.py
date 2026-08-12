"""Calendar helpers for month-to-date maths.

Every "so far this month" and "on pace to" number in the product resolves
through here, so there is exactly one definition of what a month is.
"""

from __future__ import annotations

import calendar
import datetime as dt
from typing import Optional

from .config import settings


def today() -> dt.date:
    return settings.today()


def parse_date(value: str) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(value[:10])
    except (ValueError, TypeError):
        return None


def month_key(value: str | dt.date) -> str:
    """'2026-08-05' or date(2026,8,5) -> '2026-08'."""
    if isinstance(value, dt.date):
        return f"{value.year:04d}-{value.month:02d}"
    return str(value)[:7]


def current_month() -> str:
    return month_key(today())


def month_bounds(month: str) -> tuple[str, str]:
    year, mon = int(month[:4]), int(month[5:7])
    last = calendar.monthrange(year, mon)[1]
    return f"{month}-01", f"{month}-{last:02d}"


def days_in_month(month: str) -> int:
    return calendar.monthrange(int(month[:4]), int(month[5:7]))[1]


def elapsed_days(month: str) -> int:
    """How much of `month` has actually happened, relative to 'today'.

    A past month is fully elapsed; a future month hasn't started. This is what
    keeps pace projections honest instead of extrapolating from a full month
    of data that doesn't exist yet.
    """
    now = today()
    total = days_in_month(month)
    if month < current_month():
        return total
    if month > current_month():
        return 0
    return min(now.day, total)


def month_progress(month: str) -> float:
    """0.0-1.0 — the fraction of the month that has elapsed."""
    total = days_in_month(month)
    return round(elapsed_days(month) / total, 4) if total else 0.0


def previous_months(month: str, count: int) -> list[str]:
    """['2026-07', '2026-06'] for previous_months('2026-08', 2)."""
    year, mon = int(month[:4]), int(month[5:7])
    out: list[str] = []
    for _ in range(count):
        mon -= 1
        if mon == 0:
            mon = 12
            year -= 1
        out.append(f"{year:04d}-{mon:02d}")
    return out


def month_label(month: str) -> str:
    """'2026-08' -> 'August 2026', for text a customer reads."""
    year, mon = int(month[:4]), int(month[5:7])
    return f"{calendar.month_name[mon]} {year}"
