from __future__ import annotations

from enum import StrEnum
from datetime import datetime, timedelta, timezone


class TimeRange(StrEnum):
    HOUR = "hour"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"


def to_utc_naive(dt: datetime) -> datetime:
    """
    Convert an aware datetime to naive UTC. If naive, assume it's already UTC.
    """
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _last_day_of_month(year: int, month: int) -> int:
    if month == 12:
        next_month = datetime(year + 1, 1, 1)
    else:
        next_month = datetime(year, month + 1, 1)
    return (next_month - timedelta(days=1)).day


def _subtract_months_calendar(dt: datetime, months: int) -> datetime:
    """
    Subtract `months` calendar months from `dt`, clamping the day if needed.
    """
    year = dt.year
    month = dt.month - months
    while month <= 0:
        month += 12
        year -= 1

    day = min(dt.day, _last_day_of_month(year, month))
    return dt.replace(year=year, month=month, day=day)


def compute_datetime_range(range_key: str | TimeRange, at: datetime) -> tuple[datetime, datetime]:
    """
    Compute a rolling inclusive [start, end] datetime window ending at `at`.

    Supported range_key: hour, day, week, month

    Notes:
    - Returned datetimes are naive UTC (to match DB timestamp columns).
    - If `at` is aware, it is converted to UTC naive; if naive, it is treated as UTC.
    - Returned `end` is inclusive and equals `at`.
    - "month" means 1 calendar month before `at` (same day/time when possible).
    """
    at = to_utc_naive(at)

    raw_key = range_key.value if isinstance(range_key, TimeRange) else (range_key or "")
    key = str(raw_key).strip().lower()
    if key not in {r.value for r in TimeRange}:
        raise ValueError("range must be one of: hour, day, week, month")

    end = at

    if key == "hour":
        return at - timedelta(hours=1), end

    if key == "day":
        return at - timedelta(days=1), end

    if key == "week":
        return at - timedelta(days=7), end

    # month (calendar month)
    return _subtract_months_calendar(at, 1), end
