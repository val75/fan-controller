"""Review marks (spec 5.7): the minutes a Hermes review ran, so its own GPU load can be told apart."""
from typing import List


class Marks:
    """Marks live in the store, so a restart keeps them. Times are wall-clock seconds."""

    def __init__(self, store, timeout_s: float = 900):
        self.store = store
        self.timeout_s = timeout_s

    def start(self, now: float) -> int:
        for mark in self.store.marks_open():
            self.store.mark_close(mark["id"], now, "superseded")
            self.store.add_event(now, "mark-superseded", {"id": mark["id"]})
        mark_id = self.store.mark_insert(now)
        self.store.add_event(now, "mark-start", {"id": mark_id})
        return mark_id

    def end(self, mark_id: int, now: float) -> bool:
        """True if this closed an open mark properly; that counts as a completed review."""
        mark = self.store.mark_get(mark_id)
        if mark is None or mark["outcome"] != "open":
            self.store.add_event(now, "mark-end-unknown", {"id": mark_id})
            return False
        if now - mark["started_at"] >= self.timeout_s:  # too late: expire() just hadn't run yet
            self._time_out(mark)
            return False
        self.store.mark_close(mark_id, now, "ended")
        self.store.kv_set("last_review_ok_at", now)
        self.store.add_event(now, "mark-end", {"id": mark_id, "duration_s": now - mark["started_at"]})
        return True

    def expire(self, now: float) -> List[int]:
        expired = []
        for mark in self.store.marks_open():
            if now - mark["started_at"] >= self.timeout_s:
                self._time_out(mark)
                expired.append(mark["id"])
        return expired

    def active(self, now: float) -> bool:
        return any(now - m["started_at"] < self.timeout_s for m in self.store.marks_open())

    def _time_out(self, mark: dict) -> None:
        # The end time is only an upper limit: the review ended at some point before it.
        ended_at = mark["started_at"] + self.timeout_s
        self.store.mark_close(mark["id"], ended_at, "timed_out")
        self.store.add_event(ended_at, "mark-timeout", {"id": mark["id"]})
