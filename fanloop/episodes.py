"""Load episodes (spec 5.5): one record per stretch of busy, from start to end."""
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Sequence

from fanloop.levels import Level
from fanloop.readings import CardReading


@dataclass
class Episode:
    start: float  # wall-clock seconds
    end: float
    duration_s: float
    inlet_min: Optional[float]
    inlet_mean: Optional[float]
    inlet_max: Optional[float]
    peak_c: Dict[int, float]  # card index -> peak temperature
    first_slowdown_s: Optional[float]  # from the episode's start
    throttled_s: float
    level_s: Dict[str, float]  # level name -> seconds
    level_changes: int
    review_overlap: bool
    hold_level: Optional[str]  # highest level held during the episode
    versions: List[int]  # threshold versions seen, in order

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Episode":
        d = dict(d)
        d["peak_c"] = {int(k): v for k, v in d["peak_c"].items()}  # JSON keys are strings
        return cls(**d)


@dataclass
class _Open:
    start: float
    prev_ts: float
    prev_level: Level
    prev_throttled: bool
    level_s: Dict[str, float] = field(default_factory=dict)
    level_changes: int = 0
    first_slowdown_s: Optional[float] = None
    throttled_s: float = 0.0
    inlets: List[float] = field(default_factory=list)
    peak_c: Dict[int, float] = field(default_factory=dict)
    review_overlap: bool = False
    hold_level: Optional[Level] = None
    versions: List[int] = field(default_factory=list)


class EpisodeTracker:
    """Feed it every sample; it returns the finished Episode when busy ends."""

    def __init__(self) -> None:
        self._open: Optional[_Open] = None

    def update(self, ts: float, busy: bool, cards: Sequence[CardReading], inlet_c: Optional[float],
               level: Level, version: int, mark_active: bool, hold_level: Optional[Level]) -> Optional[Episode]:
        ep = self._open
        if ep is not None:
            # This sample's dt belongs to the previous sample's state.
            dt = ts - ep.prev_ts
            name = ep.prev_level.name.lower()
            ep.level_s[name] = ep.level_s.get(name, 0.0) + dt
            if ep.prev_throttled:
                ep.throttled_s += dt
        if not busy:
            self._open = None
            return self._finish(ep, ts) if ep is not None else None

        throttled = any(c.sw_thermal or c.hw_thermal for c in cards)
        if ep is None:
            ep = self._open = _Open(start=ts, prev_ts=ts, prev_level=level, prev_throttled=throttled)
        elif level != ep.prev_level:
            ep.level_changes += 1
        if throttled and ep.first_slowdown_s is None:
            ep.first_slowdown_s = ts - ep.start
        if inlet_c is not None:
            ep.inlets.append(inlet_c)
        for c in cards:
            ep.peak_c[c.index] = max(ep.peak_c.get(c.index, c.temp_c), c.temp_c)
        ep.review_overlap = ep.review_overlap or mark_active
        if hold_level is not None and (ep.hold_level is None or hold_level > ep.hold_level):
            ep.hold_level = hold_level
        if not ep.versions or ep.versions[-1] != version:
            ep.versions.append(version)
        ep.prev_ts, ep.prev_level, ep.prev_throttled = ts, level, throttled
        return None

    @staticmethod
    def _finish(ep: _Open, end: float) -> Episode:
        inlets = ep.inlets
        return Episode(
            start=ep.start,
            end=end,
            duration_s=end - ep.start,
            inlet_min=min(inlets) if inlets else None,
            inlet_mean=sum(inlets) / len(inlets) if inlets else None,
            inlet_max=max(inlets) if inlets else None,
            peak_c=dict(ep.peak_c),
            first_slowdown_s=ep.first_slowdown_s,
            throttled_s=ep.throttled_s,
            level_s=dict(ep.level_s),
            level_changes=ep.level_changes,
            review_overlap=ep.review_overlap,
            hold_level=ep.hold_level.name.lower() if ep.hold_level is not None else None,
            versions=list(ep.versions),
        )
