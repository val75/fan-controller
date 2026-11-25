"""One facade over the loop's state, shared by the daemon and both HTTP servers."""
import threading
import time
from typing import Dict, List, Optional

from fanloop.levels import Level
from fanloop.policy import WriteRequest, WriteResult, apply_write, qualifying_episodes
from fanloop.thresholds import AmbientRule, Change, Thresholds

_SCALARS = (("step_up.low", "low"), ("step_up.medium", "medium"), ("step_up.high", "high"),
            ("margin", "margin"), ("hold_s", "hold_s"))
_STEP_NAMES = {"step_up.low": "Low", "step_up.medium": "Medium", "step_up.high": "High"}


def _num(v) -> str:
    return f"{v:g}"


def _rule(r: AmbientRule) -> str:
    return f"inlet ≥{_num(r.inlet_min)}°C → {r.level.name.capitalize()}"


def describe_change(c: Change) -> str:
    """'Medium 65 → 63°C', 'margin 5 → 6°C', 'hold 180 → 240 s', 'ambient + inlet ≥28°C → High'."""
    if c.param in _STEP_NAMES:
        return f"{_STEP_NAMES[c.param]} {_num(c.from_)} → {_num(c.to)}°C"
    if c.param == "margin":
        return f"margin {_num(c.from_)} → {_num(c.to)}°C"
    if c.param == "hold_s":
        return f"hold {_num(c.from_)} → {_num(c.to)} s"
    if c.param == "ambient.add":
        return f"ambient + {_rule(c.to)}"
    if c.param == "ambient.remove":
        return f"ambient − {_rule(c.from_)}"
    if c.param == "ambient.replace":
        return f"ambient {_rule(c.from_)} becomes {_rule(c.to)}"
    return f"{c.param} {c.from_} → {c.to}"


def _when(ts: float, now: float) -> str:
    t, n = time.localtime(ts), time.localtime(now)
    return time.strftime("%H:%M" if t[:3] == n[:3] else "%a %H:%M", t)


class Service:
    def __init__(self, store, alerter, marks, holds, watchdog, dry_run: bool):
        self.store = store
        self.alerter = alerter
        self.marks = marks
        self.holds = holds
        self.watchdog = watchdog
        self.dry_run = dry_run
        self._lock = threading.RLock()
        self._snapshot: Dict = {}

    # -- thresholds ---------------------------------------------------------------

    def thresholds(self):
        with self._lock:
            return self.store.current()

    def write(self, req: WriteRequest, now: float) -> WriteResult:
        with self._lock:
            result = apply_write(self.store, req, now)
            if result.ok:
                self.store.add_event(now, "threshold-change", {
                    "old_version": result.old_version, "new_version": result.new_version,
                    "source": req.source, "reason": req.reason,
                    "changes": [describe_change(c) for c in req.changes]})
                text = (f"thresholds v{result.old_version} → v{result.new_version}: "
                        f"{', '.join(describe_change(c) for c in req.changes)} (source {req.source}: {req.reason})")
                if req.source == "user-chat":
                    text += f"\nHermes says you asked for this. If you didn't: fanctl revert v{result.old_version}"
                self.alerter.notify(text, now)
            return result

    def diff(self, target_version: int) -> List[Change]:
        """The changes that turn the current thresholds into `target_version`'s."""
        with self._lock:
            _, cur = self.store.current()
            target = self.store.get_version(target_version)
            changes = [Change(param, getattr(cur, f), getattr(target, f))
                       for param, f in _SCALARS if getattr(cur, f) != getattr(target, f)]
            changes += [Change("ambient.remove", r, None) for r in cur.ambient_rules if r not in target.ambient_rules]
            changes += [Change("ambient.add", None, r) for r in target.ambient_rules if r not in cur.ambient_rules]
            return changes

    def revert(self, target_version: int, now: float) -> WriteResult:
        with self._lock:
            version, cur = self.store.current()
            try:
                changes = self.diff(target_version)
            except KeyError:
                return WriteResult(False, "unknown-version", f"there is no v{target_version}", version, None, cur, None)
            if not changes:
                return WriteResult(False, "no-op", f"v{version} already equals v{target_version}", version, None, cur, None)
            return self.write(WriteRequest("user-cli", f"revert to v{target_version}", changes), now)

    # -- marks, holds, mute ----------------------------------------------------------

    def mark_start(self, now: float) -> int:
        with self._lock:
            return self.marks.start(now)

    def mark_end(self, mark_id: int, now: float) -> bool:
        with self._lock:
            return self.marks.end(mark_id, now)

    def hold_set(self, level: Level, until_ts: Optional[float], now: float):
        with self._lock:
            hold = self.holds.set(level, until_ts, now)
            name = level.name.capitalize()
            self.alerter.notify(f"Hold at {name}, until cancelled" if until_ts is None
                                else f"Hold at {name} until {_when(until_ts, now)}", now)
            return hold

    def hold_clear(self, now: float):
        with self._lock:
            hold = self.holds.clear(now)
            if hold is not None:
                self.alerter.notify("Hold ended", now)
            return hold

    def mute(self, until_ts: float, now: float) -> None:
        with self._lock:
            self.watchdog.mute(until_ts, now)
            t = time.localtime(until_ts)
            shown = time.strftime("%Y-%m-%d" if (t.tm_hour, t.tm_min) == (0, 0) else "%Y-%m-%d %H:%M", t)
            self.alerter.notify(f"Review watchdog muted until {shown}", now)

    def unmute(self, now: float) -> None:
        with self._lock:
            self.watchdog.unmute(now)
            self.alerter.notify("Review watchdog unmuted", now)

    def test_alert(self, now: float) -> None:
        self.alerter.notify("Test alert: the fan loop can reach you.", now)

    # -- status and summary -------------------------------------------------------------

    def publish(self, snapshot: Dict) -> None:
        with self._lock:
            self._snapshot = dict(snapshot)

    def status(self, now: Optional[float] = None) -> Dict:
        now = time.time() if now is None else now
        with self._lock:
            out = dict(self._snapshot)
            out["dry_run"] = self.dry_run
            try:  # the loop's own snapshot still answers when the database doesn't
                version, t = self.store.current()
                hold = self.holds.current(now)
                out.update({
                    "version": version,
                    "thresholds": t.to_dict(),
                    "hold": None if hold is None else
                    {"level": hold.level.name.lower(), "since": hold.since, "until": hold.until},
                    "open_marks": self.store.marks_open(),
                    "last_review_ok_at": self.store.kv_get("last_review_ok_at"),
                    "review_watchdog_muted": self.watchdog.muted(now),
                })
            except Exception as e:
                out["store_error"] = repr(e)[:300]
            return out

    def summary(self, since: float, until: float) -> Dict:
        with self._lock:
            version, _ = self.store.current()
            episodes = [e for e in self.store.episodes(since_ts=since, limit=100_000) if e.start <= until]
            level_s: Dict[str, float] = {}
            peak_c: Dict[int, float] = {}
            for e in episodes:
                for name, s in e.level_s.items():
                    level_s[name] = level_s.get(name, 0.0) + s
                for card, temp in e.peak_c.items():
                    peak_c[card] = max(peak_c.get(card, temp), temp)
            inlets = [e.inlet_max for e in episodes if e.inlet_max is not None]
            return {
                "since": since,
                "until": until,
                "version": version,
                "episodes": len(episodes),
                "qualifying_on_current": qualifying_episodes(self.store, version),
                "slowdown_s": sum(e.throttled_s for e in episodes),
                "level_s": level_s,
                "peak_c": peak_c,
                "max_inlet": max(inlets) if inlets else None,
            }
