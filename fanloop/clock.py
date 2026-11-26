"""The loop's own time zone (config `timezone`), used for every clock time it shows
or reads: hold end times, "since 14:05" in messages, fanctl's output. t4 itself
runs in UTC; the user reads and types times in this zone. EET includes summer
time (EEST)."""
from datetime import datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TZ = "EET"


def zone(name: str) -> tzinfo:
    """The named IANA zone. ValueError if there is no such zone."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"unknown time zone {name!r}") from None


def local(ts: float, tz: tzinfo) -> datetime:
    return datetime.fromtimestamp(ts, tz)


def clock_text(ts: float, now: float, tz: tzinfo, other_day: str = "%a %H:%M") -> str:
    """'18:00' when ts falls on the same day as now (in tz), else e.g. 'Fri 09:00'."""
    t, n = local(ts, tz), local(now, tz)
    return t.strftime("%H:%M" if t.date() == n.date() else other_day)
