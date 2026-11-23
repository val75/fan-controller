"""The review watchdog (spec 5.8): alert when Hermes reviews stop completing."""
from typing import List, Optional

from fanloop.messages import Message

_LAST_OK = "last_review_ok_at"
_FIRST_START = "first_start_at"
_INCIDENT = "watchdog_incident"
_MUTE = "watchdog_mute_until"


def _llama_word(llama_ok: Optional[bool]) -> str:
    return {True: "running", False: "not running"}.get(llama_ok, "unknown")


class ReviewWatchdog:
    """All state lives in the store's kv, so a restart neither resets the clock
    nor repeats an alert. Times are wall-clock seconds."""

    def __init__(self, store, limit_s: float = 13 * 3600, remind_s: float = 86400):
        self.store = store
        self.limit_s = limit_s
        self.remind_s = remind_s

    def ensure_started(self, now: float) -> None:
        if self.store.kv_get(_FIRST_START) is None:
            self.store.kv_set(_FIRST_START, now)

    def mute(self, until_ts: float, now: float) -> None:
        self.store.kv_set(_MUTE, until_ts)

    def unmute(self, now: float) -> None:
        self.store.kv_set(_MUTE, None)

    def muted(self, now: float) -> bool:
        until = self.store.kv_get(_MUTE)
        return until is not None and now < until

    def check(self, now: float, llama_ok: Optional[bool]) -> List[Message]:
        last = self.store.kv_get(_LAST_OK)
        if last is None:
            last = self.store.kv_get(_FIRST_START)
        if last is None:
            self.ensure_started(now)
            return []
        if now < last:  # the clock went backwards
            return []

        incident = self.store.kv_get(_INCIDENT)
        if incident is not None and last > incident["opened_at"]:
            self.store.kv_set(_INCIDENT, None)
            if incident["last_msg_at"] is not None:  # only if the user was told
                return [Message("review-watchdog-ok", "Hermes reviews are completing again.")]
            return []

        if now - last < self.limit_s:
            return []
        if incident is None:
            incident = {"opened_at": now, "last_msg_at": None}
        messages = []
        if not self.muted(now):
            hours = int((now - last) // 3600)
            if incident["last_msg_at"] is None:
                messages.append(Message("review-watchdog", f"No completed Hermes review for {hours} h. "
                                                           f"llama-server on t4: {_llama_word(llama_ok)}"))
            elif now - incident["last_msg_at"] >= self.remind_s:
                messages.append(Message("review-watchdog", f"Still no completed Hermes review, for {hours} h now. "
                                                           f"llama-server on t4: {_llama_word(llama_ok)}"))
            if messages:
                incident["last_msg_at"] = now
        self.store.kv_set(_INCIDENT, incident)
        return messages
