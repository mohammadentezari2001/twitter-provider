"""Timezone-aware time helpers. Everything stored is UTC; only display is local."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(UTC)


def ensure_aware(value: datetime) -> datetime:
    """Treat naive datetimes (e.g. from older DB rows) as UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def to_tz(value: datetime, tz: ZoneInfo | str) -> datetime:
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    return ensure_aware(value).astimezone(zone)


def format_dt(
    value: datetime | None,
    tz: ZoneInfo | str,
    fmt: str = "%Y-%m-%d %H:%M",
    *,
    with_tz: bool = True,
) -> str:
    if value is None:
        return "—"
    local = to_tz(value, tz)
    rendered = local.strftime(fmt)
    return f"{rendered} {local.tzname()}" if with_tz else rendered


def start_of_day(tz: ZoneInfo | str, *, now: datetime | None = None) -> datetime:
    """Midnight of the current local day, returned in UTC.

    Daily statistics must roll over at the operator's local midnight, not UTC's.
    """
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    local = to_tz(now or utcnow(), zone)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(UTC)


def days_ago(days: int, tz: ZoneInfo | str, *, now: datetime | None = None) -> datetime:
    return start_of_day(tz, now=now) - timedelta(days=days)


def humanize_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = max(float(seconds), 0.0)
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def humanize_ago(value: datetime | None, *, now: datetime | None = None) -> str:
    if value is None:
        return "never"
    delta = (now or utcnow()) - ensure_aware(value)
    seconds = delta.total_seconds()
    if seconds < 0:
        return f"in {humanize_duration(-seconds)}"
    if seconds < 5:
        return "just now"
    return f"{humanize_duration(seconds)} ago"


def ms_between(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return int((ensure_aware(end) - ensure_aware(start)).total_seconds() * 1000)
