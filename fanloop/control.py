"""The control rule (spec 5.2), including the blind rule for nvidia-smi failures (spec 7).

Pure logic: the caller passes monotonic time and the current level, and applies
the returned decision. The only state kept here is timers.
"""
from dataclasses import dataclass
from typing import Optional, Sequence

from fanloop.levels import Level
from fanloop.readings import CardReading
from fanloop.thresholds import EMERGENCY_C, Thresholds

_STEP_LEVELS = (Level.LOW, Level.MEDIUM, Level.HIGH)


@dataclass(frozen=True)
class ControlConfig:
    single_card1_offset_c: float = 4.0  # card 0 runs ~3-4°C warmer than card 1
    blind_idle_power_w: float = 238.0
    blind_power_above_w: float = 50.0
    blind_hold_s: float = 30.0


@dataclass(frozen=True)
class ControlInputs:
    now: float  # monotonic seconds
    cards: Sequence[CardReading]
    busy: bool
    inlet_c: Optional[float]
    system_power_w: Optional[float]
    hold_min: Optional[Level]


@dataclass(frozen=True)
class Decision:
    level: Level
    reason: str  # emergency, step-up, floor, step-down, blind-load, blind-hold, steady


class Controller:
    def __init__(self, thresholds: Thresholds, cfg: ControlConfig = ControlConfig()):
        self.thresholds = thresholds
        self.cfg = cfg
        self._below_since: Optional[float] = None  # step-down condition true since
        self._timer_level: Optional[Level] = None  # the level that timer belongs to
        self._blind_held: Optional[Level] = None  # level when blindness began
        self._load_since: Optional[float] = None  # blind: system power high since

    @staticmethod
    def hotter_c(cards: Sequence[CardReading], offset_c: float) -> Optional[float]:
        """The hotter card's temperature; only card 1 readable adds `offset_c`."""
        temps = {c.index: c.temp_c for c in cards}
        if 0 in temps and 1 in temps:
            return max(temps[0], temps[1])
        if 0 in temps:
            return temps[0]
        if 1 in temps:
            return temps[1] + offset_c
        return None

    def decide(self, inp: ControlInputs, current: Level) -> Decision:
        t = self.thresholds
        hotter = self.hotter_c(inp.cards, self.cfg.single_card1_offset_c)
        if self._timer_level != current:
            self._reset_timer()

        if hotter is None:
            return self._blind(inp, current)
        self._blind_held = None
        self._load_since = None

        if hotter >= EMERGENCY_C or any(c.sw_thermal or c.hw_thermal for c in inp.cards):
            self._reset_timer()
            return Decision(Level.MAX, "emergency")

        up = max((lvl for lvl in _STEP_LEVELS if hotter >= t.step_up(lvl)), default=Level.OFF)
        ambient = t.ambient_min(inp.inlet_c) if inp.busy else Level.OFF
        floor = max(up, ambient, inp.hold_min or Level.OFF)

        if floor > current:
            self._reset_timer()
            return Decision(floor, "step-up" if up == floor else "floor")
        if floor == current:
            self._reset_timer()
            return Decision(current, "steady")

        threshold = self._step_down_threshold(current, inp.busy)
        if threshold is None or hotter >= threshold:
            self._reset_timer()
            return Decision(current, "steady")
        if self._below_since is None:
            self._below_since = inp.now
            self._timer_level = current
        if inp.now - self._below_since >= t.hold_s:
            self._reset_timer()
            return Decision(Level(current - 1), "step-down")
        return Decision(current, "steady")

    def _step_down_threshold(self, current: Level, busy: bool) -> Optional[float]:
        t = self.thresholds
        if current == Level.MAX:
            return EMERGENCY_C - t.margin  # normal rule, even while busy
        if not busy:
            return t.step_up(current) - t.margin
        if current == Level.LOW:
            return None  # never Low -> Off while busy
        return t.step_up(Level(current - 1)) - t.margin

    def _blind(self, inp: ControlInputs, current: Level) -> Decision:
        self._reset_timer()
        if self._blind_held is None:
            self._blind_held = current
        held = max(self._blind_held, inp.hold_min or Level.OFF)
        threshold = self.cfg.blind_idle_power_w + self.cfg.blind_power_above_w
        if inp.system_power_w is not None and inp.system_power_w >= threshold:
            if self._load_since is None:
                self._load_since = inp.now
        else:
            self._load_since = None
        if self._load_since is not None and inp.now - self._load_since >= self.cfg.blind_hold_s:
            ambient = self.thresholds.ambient_min(inp.inlet_c) if inp.busy else Level.OFF
            return Decision(max(held, Level.MEDIUM, ambient), "blind-load")
        return Decision(held, "blind-hold")

    def _reset_timer(self) -> None:
        self._below_since = None
        self._timer_level = None
