"""Who may change the thresholds, and when (spec 5.4, 5.9)."""
import math
from dataclasses import dataclass
from typing import List, Optional

from fanloop.thresholds import PARAMS, BadChange, Change, NoOpChange, StaleFrom, Thresholds, apply_changes, direction, validate

SOURCES = ("review", "user-chat", "user-cli")
REVIEW_LIMIT = 2
USER_CHAT_LIMIT = 6
WINDOW_S = 86400
GATE_EPISODES = 3
GATE_MIN_S = 360

_LIMITS = {"review": REVIEW_LIMIT, "user-chat": USER_CHAT_LIMIT}


@dataclass(frozen=True)
class WriteRequest:
    source: str
    reason: str
    changes: List[Change]


@dataclass(frozen=True)
class WriteResult:
    ok: bool
    error: Optional[str]  # bad-source, empty, review-multi, rate-limit, unknown-param, no-op, bad-value, stale, invalid, evidence
    detail: str
    old_version: int
    new_version: Optional[int]
    old: Thresholds
    new: Optional[Thresholds]


def qualifying_episodes(store, version: int) -> int:
    """Episodes that count as evidence for a quieter review change (spec 5.4)."""
    return sum(1 for e in store.episodes(version=version, limit=10_000)
               if e.duration_s >= GATE_MIN_S and not e.review_overlap and e.hold_level is None)


def apply_write(store, req: WriteRequest, now: float) -> WriteResult:
    old_version, old = store.current()

    def reject(error: str, detail: str) -> WriteResult:
        return WriteResult(False, error, detail, old_version, None, old, None)

    if req.source not in SOURCES:
        return reject("bad-source", f"source must be one of {', '.join(SOURCES)}")
    if not req.changes:
        return reject("empty", "no changes")
    if req.source == "review" and len(req.changes) > 1:
        return reject("review-multi", "a review write changes exactly one value")
    limit = _LIMITS.get(req.source)
    if limit is not None:
        # "In the last 24 h": strictly newer than now - 24 h.
        recent = store.count_versions(req.source, math.nextafter(now - WINDOW_S, math.inf))
        if recent >= limit:
            return reject("rate-limit", f"{req.source} already made {recent} changes in the last 24 h (limit {limit})")
    unknown = [c.param for c in req.changes if c.param not in PARAMS]
    if unknown:
        return reject("unknown-param", f"not a writable parameter: {', '.join(unknown)}")
    try:
        new = apply_changes(old, req.changes)
    except StaleFrom as e:
        return reject("stale", f"{e} (thresholds are at v{old_version}: read them again)")
    except NoOpChange as e:
        return reject("no-op", str(e))
    except BadChange as e:
        return reject("bad-value", str(e))
    errors = validate(new)
    if errors:
        return reject("invalid", "; ".join(errors))
    if req.source == "review" and direction(req.changes[0]) == "quieter":
        count = qualifying_episodes(store, old_version)
        if count < GATE_EPISODES:
            return reject("evidence", f"a quieter change needs {GATE_EPISODES} qualifying episodes on v{old_version} "
                                      f"(at least {GATE_MIN_S} s, no review, no hold); there are {count}")
    new_version = store.add_version(new, req.source, req.reason, req.changes, now)
    return WriteResult(True, None, f"v{old_version} -> v{new_version}", old_version, new_version, old, new)
