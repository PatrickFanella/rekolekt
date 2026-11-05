"""Period-domain coercion, bucketing, and range matching."""

import calendar
from datetime import date, datetime, timedelta, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def coerce_datetime(value: date | datetime | None, *, end: bool = False) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.combine(value, datetime.min.time())
        if end:
            result += timedelta(days=1)
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result


def period_key(value: datetime | date | None, granularity: str) -> str | None:
    if value is None:
        return None
    result = value if isinstance(value, datetime) else datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.strftime("%G-W%V" if granularity == "week" else "%Y-%m")


def period_start(period: str, granularity: str) -> datetime | None:
    try:
        if granularity == "week":
            return datetime.strptime(f"{period}-1", "%G-W%V-%u").replace(tzinfo=timezone.utc)
        return datetime.strptime(period, "%Y-%m").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def period_label(period: str, granularity: str) -> str:
    start = period_start(period, granularity)
    if start is None:
        return period
    if granularity == "week":
        return f"Week of {start.strftime('%Y-%m-%d')}"
    return start.strftime("%B %Y")


def as_date(value: date | datetime | None) -> date | None:
    if value is None:
        return None
    return value.date() if isinstance(value, datetime) else value


def month_bounds(value: date) -> tuple[date, date]:
    last_day = calendar.monthrange(value.year, value.month)[1]
    return value.replace(day=1), value.replace(day=last_day)


def week_bounds(value: date) -> tuple[date, date]:
    monday = value - timedelta(days=value.weekday())
    return monday, monday + timedelta(days=6)


def period_end(period: str, granularity: str) -> date | None:
    """Inclusive last day of a week or month period key."""

    start = period_start(period, granularity)
    if start is None:
        return None
    return bucket_bounds(start.date(), granularity)[1]


def within_period_range(
    period: str,
    granularity: str,
    date_from: date | datetime | None,
    date_to: date | datetime | None,
) -> bool:
    """True when the period overlaps the inclusive range, not only when it starts inside it."""

    start = period_start(period, granularity)
    end = period_end(period, granularity)
    if start is None or end is None:
        return True
    lower = as_date(date_from)
    upper = as_date(date_to)
    return not ((lower is not None and end < lower) or (upper is not None and start.date() > upper))


def bucket_bounds(bucket_start: date, granularity: str) -> tuple[date, date]:
    """Inclusive first and last day of the week, month or year containing ``bucket_start``."""

    if granularity == "year":
        return date(bucket_start.year, 1, 1), date(bucket_start.year, 12, 31)
    if granularity == "week":
        return week_bounds(bucket_start)
    return month_bounds(bucket_start)


def bucket_period_and_label(bucket_start: date, granularity: str) -> tuple[str, str]:
    """Stable key and display label; week labels match named week periods."""

    if granularity == "year":
        return bucket_start.strftime("%Y"), bucket_start.strftime("%Y")
    if granularity == "week":
        monday = week_bounds(bucket_start)[0]
        return monday.strftime("%G-W%V"), f"Week of {monday.isoformat()}"
    return bucket_start.strftime("%Y-%m"), bucket_start.strftime("%B %Y")
