"""Manual holds (spec 5.2): a minimum fan level set by the user with fanctl."""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Optional

from fanloop.clock import DEFAULT_TZ, local, zone
from fanloop.levels import Level, level_from_name
from fanloop.messages import Message

_HOLD = "hold"
_LAST_REMINDER = "hold_last_reminder"


@dataclass(frozen=True)
class Hold:
    level: Level
    since: float  # wall-clock seconds
    until: Optional[float]  # None: until cancelled


def parse_until(text: str, now: datetime) -> datetime:
    """'HH:MM' is the next time that clock time comes round; 'YYYY-MM-DD HH:MM' is
    a full date and time. Both are in now's time zone (the loop's, see clock.py)."""
    text = text.strip()
    if re.fullmatch(r"\d{1,2}:\d{2}", text):
        t = datetime.strptime(text, "%H:%M")
        candidate = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        return candidate if candidate > now else candidate + timedelta(days=1)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{1,2}:\d{2}", text):
        return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=now.tzinfo)
    raise ValueError(f"expected HH:MM or YYYY-MM-DD HH:MM, got {text!r}")


class Holds:
    """The hold lives in the store's kv, so a restart keeps it."""

    def __init__(self, store, max_ahead_s: float = 86400, remind_s: float = 86400, tz: Optional[tzinfo] = None):
        self.store = store
        self.tz = tz or zone(DEFAULT_TZ)
        self.max_ahead_s = max_ahead_s
        self.remind_s = remind_s

    def _load(self) -> Optional[Hold]:
        d = self.store.kv_get(_HOLD)
        return Hold(level_from_name(d["level"]), d["since"], d["until"]) if d else None

    def _save(self, hold: Optional[Hold]) -> None:
        self.store.kv_set(_HOLD, None if hold is None else
                          {"level": hold.level.name.lower(), "since": hold.since, "until": hold.until})

    def set(self, level: Level, until_ts: Optional[float], now: float) -> Hold:
        if level == Level.OFF:
            raise ValueError("a hold is a minimum level: Low, Medium, High or Max")
        if until_ts is not None:
            if until_ts <= now:
                raise ValueError("the end time has already passed")
            if until_ts > now + self.max_ahead_s:
                raise ValueError(f"a timed hold can end at most {self.max_ahead_s / 3600:g} h ahead; "
                                 "leave the end time out to hold until cancelled")
        hold = Hold(level, now, until_ts)
        self._save(hold)
        self.store.kv_set(_LAST_REMINDER, None)
        self.store.add_event(now, "hold-set", {"level": level.name.lower(), "until": until_ts})
        return hold

    def clear(self, now: float) -> Optional[Hold]:
        hold = self.current(now)
        if hold is None:
            return None
        self._save(None)
        self.store.add_event(now, "hold-cleared", {"level": hold.level.name.lower()})
        return hold

    def current(self, now: float) -> Optional[Hold]:
        hold = self._load()
        if hold is None or (hold.until is not None and now >= hold.until):
            return None
        return hold

    def expire(self, now: float) -> Optional[Hold]:
        """Clear a timed hold whose end time has come, and return it so it can be announced."""
        hold = self._load()
        if hold is None or hold.until is None or now < hold.until:
            return None
        self._save(None)
        self.store.add_event(now, "hold-expired", {"level": hold.level.name.lower()})
        return hold

    def reminder(self, now: float) -> Optional[Message]:
        """Once a day for a hold with no end time, so it can't be forgotten."""
        hold = self.current(now)
        if hold is None or hold.until is not None:
            return None
        last = self.store.kv_get(_LAST_REMINDER) or hold.since
        if now - last < self.remind_s:
            return None
        self.store.kv_set(_LAST_REMINDER, now)
        since = local(hold.since, self.tz).strftime("%a %H:%M")
        return Message("hold-reminder", f"Hold at {hold.level.name.capitalize()} since {since}, until cancelled. "
                                        "fanctl hold off to end it")
