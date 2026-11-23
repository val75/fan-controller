"""Busy detection (spec 5.2): is a load episode in progress?"""
from typing import Optional, Sequence

from fanloop.readings import CardReading


class BusyDetector:
    """Busy starts after `start_s` of activity on either card and ends after
    `end_s` of inactivity on both. Times are monotonic seconds."""

    def __init__(self, power_w: float = 45.0, util_pct: float = 50.0,
                 start_s: float = 10.0, end_s: float = 60.0):
        self.power_w = power_w
        self.util_pct = util_pct
        self.start_s = start_s
        self.end_s = end_s
        self._busy = False
        self._active_since: Optional[float] = None
        self._inactive_since: Optional[float] = None

    @property
    def busy(self) -> bool:
        return self._busy

    def is_active(self, cards: Sequence[CardReading]) -> bool:
        return any(c.power_w >= self.power_w or c.util_pct >= self.util_pct for c in cards)

    def update(self, now: float, cards: Sequence[CardReading]) -> bool:
        if not cards:  # no readings: keep the state and the timers
            return self._busy
        if self.is_active(cards):
            self._inactive_since = None
            if self._active_since is None:
                self._active_since = now
            if not self._busy and now - self._active_since >= self.start_s:
                self._busy = True
        else:
            self._active_since = None
            if self._inactive_since is None:
                self._inactive_since = now
            if self._busy and now - self._inactive_since >= self.end_s:
                self._busy = False
        return self._busy
