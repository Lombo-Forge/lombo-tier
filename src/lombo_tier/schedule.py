"""When the next run is. Pure: the caller passes the current time and the time zone name."""

from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def seconds_until(hour: int, now_utc: float, tz_name: str = "UTC") -> float:
    """Seconds from `now_utc` to the next HH:00 in the named time zone (daylight saving aware).

    An unknown zone name falls back to UTC rather than stopping the loop."""
    try:
        tz: tzinfo = ZoneInfo(tz_name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        tz = UTC
    now = datetime.fromtimestamp(now_utc, tz)
    nxt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if nxt <= now:
        nxt = (nxt.replace(tzinfo=None) + timedelta(days=1)).replace(tzinfo=tz)
    return nxt.timestamp() - now_utc
