"""Messages to the user's phone through the loop's own Telegram bot (spec 5.8).

Sending happens on a background thread from a bounded queue, so a slow or
unreachable Telegram never holds up the control loop."""
import collections
import json
import threading
import time
import urllib.parse
import urllib.request
from typing import Deque, Dict, Optional

PREFIX = "[t4 fans] "
_ALERT_LAST = "alert_last"
_INCIDENTS = "incidents"


class TelegramSender:
    def __init__(self, token: str, chat_id: str, timeout_s: float = 10.0,
                 api_base: str = "https://api.telegram.org"):
        self._url = f"{api_base}/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._timeout_s = timeout_s

    def send(self, text: str) -> None:
        """Raises OSError (network, HTTP errors) or ValueError (Telegram said no)."""
        data = urllib.parse.urlencode({"chat_id": self._chat_id, "text": text,
                                       "disable_web_page_preview": "true"}).encode()
        with urllib.request.urlopen(urllib.request.Request(self._url, data=data), timeout=self._timeout_s) as resp:
            reply = json.loads(resp.read().decode() or "{}")
        if not reply.get("ok"):
            raise ValueError(f"Telegram refused the message: {reply.get('description', reply)}")


class Alerter:
    """Rate-limited alerts, unlimited notifications and incidents, all queued.

    Rate limits and open incidents live in memory and are written through to the
    store, so they survive restarts. The store is never needed to send: a message
    is queued before anything is written, and a failing store only loses the record.

    An incident whose alert the rate limit suppressed is sent late by tick() once it
    has lasted `persist_s`: otherwise a recovery message followed by a suppressed new
    incident would leave the user believing all is well. Short repeats stay quiet.
    None of the public methods raise."""

    def __init__(self, sender, store, rate_limit_s: float = 1800, max_queue: int = 100, retry_s: float = 60,
                 persist_s: float = 300):
        self.sender = sender
        self.store = store
        self.rate_limit_s = rate_limit_s
        self.max_queue = max_queue
        self.retry_s = retry_s
        self.persist_s = persist_s
        self._queue: Deque[str] = collections.deque()
        self._in_flight: Optional[str] = None
        self._cond = threading.Condition()
        self._stopping = False
        self._thread: Optional[threading.Thread] = None
        self._dropped = 0
        self._failures = 0
        self._state_lock = threading.RLock()
        self._last: Optional[Dict[str, float]] = None  # loaded from the store on first use
        self._incidents: Dict[str, dict] = {}

    # -- the background sender ------------------------------------------------------

    def start(self) -> None:
        if self._thread is None:
            self._stopping = False
            self._thread = threading.Thread(target=self._run, name="telegram", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def dropped(self) -> int:
        return self._dropped

    def pending(self) -> int:
        with self._cond:
            return len(self._queue) + (self._in_flight is not None)

    def drain(self, timeout_s: float) -> bool:
        """Wait until everything queued has been sent. False on timeout."""
        with self._cond:
            return self._cond.wait_for(lambda: not self._queue and self._in_flight is None, timeout=timeout_s)

    def _run(self) -> None:
        while True:
            with self._cond:
                self._cond.wait_for(lambda: self._stopping or self._queue or self._in_flight)
                if self._stopping:
                    return
                if self._in_flight is None:
                    self._in_flight = self._queue.popleft()
                text = self._in_flight
            try:
                self.sender.send(text)
            except Exception as e:  # keep the message and retry it, in order
                self._failures += 1
                if self._failures == 1:
                    self._event("telegram-error", {"error": str(e)[:300]})
                with self._cond:
                    self._cond.wait_for(lambda: self._stopping, timeout=self.retry_s)
                continue
            if self._failures:
                self._event("telegram-recovered", {"failed_attempts": self._failures})
                self._failures = 0
            with self._cond:
                self._in_flight = None
                self._cond.notify_all()

    def _enqueue(self, text: str) -> None:
        dropped = False
        with self._cond:
            # The message being sent counts toward the limit; the oldest waiting one goes.
            while self._queue and len(self._queue) + (self._in_flight is not None) >= self.max_queue:
                self._queue.popleft()
                self._dropped += 1
                dropped = True
            self._queue.append(PREFIX + text)
            self._cond.notify_all()
        if dropped:
            self._event("telegram-dropped", {"total_dropped": self._dropped})

    def _event(self, kind: str, detail: dict, now: Optional[float] = None) -> None:
        try:  # the sender thread has no caller's `now`, so it reads the clock itself
            self.store.add_event(time.time() if now is None else now, kind, detail)
        except Exception:
            pass

    def _load(self) -> None:
        """The persisted state, read once. A store that can't be read starts us empty."""
        if self._last is not None:
            return
        try:
            last = dict(self.store.kv_get(_ALERT_LAST, {}) or {})
            incidents = dict(self.store.kv_get(_INCIDENTS, {}) or {})
        except Exception:
            last, incidents = {}, {}
        self._last, self._incidents = last, incidents

    def _save(self, key: str, value: dict) -> None:
        try:
            self.store.kv_set(key, value)
        except Exception:
            pass

    # -- what callers use ---------------------------------------------------------

    def alert(self, kind: str, text: str, now: float) -> bool:
        """Send unless the same kind was sent within rate_limit_s. False if suppressed."""
        try:
            with self._state_lock:
                self._load()
                # A clock that jumped back (now before the last send) never suppresses.
                suppressed = kind in self._last and 0 <= now - self._last[kind] < self.rate_limit_s
                if not suppressed:
                    self._last[kind] = now
                    self._enqueue(text)
                    self._save(_ALERT_LAST, self._last)
            self._event("alert", {"kind": kind, "text": text, "suppressed": suppressed}, now)
            return not suppressed
        except Exception:
            return False

    def notify(self, text: str, now: float) -> None:
        """Always sent: threshold changes, holds, reminders, recoveries."""
        try:
            self._enqueue(text)
            self._event("alert", {"kind": "notify", "text": text, "suppressed": False}, now)
        except Exception:
            pass

    def open_incident(self, kind: str, text: str, now: float) -> None:
        """One alert per incident, however often the problem is seen."""
        try:
            with self._state_lock:
                self._load()
                if kind in self._incidents:
                    return
                self._incidents[kind] = {"opened_at": now, "alerted": self.alert(kind, text, now), "text": text}
                self._save(_INCIDENTS, self._incidents)
        except Exception:
            pass

    def tick(self, now: float) -> None:
        """Send the alert of every suppressed incident that has now lasted persist_s."""
        try:
            with self._state_lock:
                self._load()
                due = [(kind, inc) for kind, inc in self._incidents.items()
                       if not inc.get("alerted") and now - inc.get("opened_at", now) >= self.persist_s]
                for kind, inc in due:
                    since = time.strftime("%H:%M", time.localtime(inc["opened_at"]))
                    text = f"{inc.get('text') or kind} (ongoing since {since})"
                    self._enqueue(text)
                    inc["alerted"] = True
                    self._last[kind] = now
                    self._event("alert", {"kind": kind, "text": text, "suppressed": False, "late": True}, now)
                if due:
                    self._save(_INCIDENTS, self._incidents)
                    self._save(_ALERT_LAST, self._last)
        except Exception:
            pass

    def close_incident(self, kind: str, text: str, now: float) -> None:
        """One recovery message, and only if the user was told about the incident."""
        try:
            with self._state_lock:
                self._load()
                incident = self._incidents.pop(kind, None)
                if incident is None:
                    return
                self._save(_INCIDENTS, self._incidents)
            if incident.get("alerted"):
                self.notify(text, now)
        except Exception:
            pass
