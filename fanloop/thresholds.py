"""Tunable thresholds, their hard limits (spec 5.4), and changes to them."""
import dataclasses
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from fanloop.levels import Level, level_from_name

EMERGENCY_C = 80.0  # fixed, not part of the thresholds (spec 5.2)

STEP_UP_MIN_C = 45.0
STEP_UP_MAX_C = 78.0
STEP_UP_GAP_C = 3.0
MARGIN_MIN_C, MARGIN_MAX_C = 3.0, 10.0
HOLD_MIN_S, HOLD_MAX_S = 60, 900
INLET_MIN_C, INLET_MAX_C = 15.0, 40.0
MAX_AMBIENT_RULES = 8

PARAMS = (
    "step_up.low",
    "step_up.medium",
    "step_up.high",
    "margin",
    "hold_s",
    "ambient.add",
    "ambient.remove",
    "ambient.replace",
)
_SCALAR_FIELDS = {
    "step_up.low": "low",
    "step_up.medium": "medium",
    "step_up.high": "high",
    "margin": "margin",
    "hold_s": "hold_s",
}
_KEYS = {"low", "medium", "high", "margin", "hold_s", "ambient_rules"}


def _is_number(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


@dataclass(frozen=True)
class AmbientRule:
    """While busy and inlet >= inlet_min, use at least `level`."""

    inlet_min: float
    level: Level

    def to_dict(self) -> dict:
        return {"inlet_min": self.inlet_min, "level": self.level.name.lower()}

    @classmethod
    def from_dict(cls, d: dict) -> "AmbientRule":
        if not isinstance(d, dict) or set(d) != {"inlet_min", "level"}:
            raise ValueError(f"ambient rule needs exactly inlet_min and level: {d!r}")
        if not _is_number(d["inlet_min"]) or not isinstance(d["level"], str):
            raise ValueError(f"bad ambient rule: {d!r}")
        return cls(d["inlet_min"], level_from_name(d["level"]))


@dataclass(frozen=True)
class Thresholds:
    low: float
    medium: float
    high: float
    margin: float
    hold_s: int
    ambient_rules: Tuple[AmbientRule, ...] = ()

    def step_up(self, level: Level) -> Optional[float]:
        return {
            Level.OFF: None,
            Level.LOW: self.low,
            Level.MEDIUM: self.medium,
            Level.HIGH: self.high,
            Level.MAX: EMERGENCY_C,
        }[level]

    def ambient_min(self, inlet_c: Optional[float]) -> Level:
        if inlet_c is None:
            return Level.OFF
        matching = [r.level for r in self.ambient_rules if inlet_c >= r.inlet_min]
        return max(matching, default=Level.OFF)

    def to_dict(self) -> dict:
        return {
            "low": self.low,
            "medium": self.medium,
            "high": self.high,
            "margin": self.margin,
            "hold_s": self.hold_s,
            "ambient_rules": [r.to_dict() for r in self.ambient_rules],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Thresholds":
        if not isinstance(d, dict) or set(d) != _KEYS:
            raise ValueError(f"thresholds need exactly {sorted(_KEYS)}, got {sorted(d) if isinstance(d, dict) else d!r}")
        if not isinstance(d["ambient_rules"], list):
            raise ValueError("ambient_rules must be a list")
        rules = tuple(AmbientRule.from_dict(r) for r in d["ambient_rules"])
        return cls(d["low"], d["medium"], d["high"], d["margin"], d["hold_s"], _sorted(rules))


def _sorted(rules) -> Tuple[AmbientRule, ...]:
    return tuple(sorted(rules, key=lambda r: (r.inlet_min, r.level)))


def load_defaults() -> Thresholds:
    """The starting thresholds (spec 5.3), stored as version 1 on first start."""
    with open(Path(__file__).with_name("defaults.json")) as f:
        return Thresholds.from_dict(json.load(f))


def validate(t: Thresholds) -> List[str]:
    """Hard limits (spec 5.4). An empty list means valid."""
    errors = []
    for name in ("low", "medium", "high"):
        value = getattr(t, name)
        if not _is_number(value) or not STEP_UP_MIN_C <= value <= STEP_UP_MAX_C:
            errors.append(f"{name} must be {STEP_UP_MIN_C:g}-{STEP_UP_MAX_C:g}°C, got {value!r}")
    if not errors:
        for lower, upper in (("low", "medium"), ("medium", "high")):
            if getattr(t, upper) - getattr(t, lower) < STEP_UP_GAP_C:
                errors.append(f"{upper} must be at least {STEP_UP_GAP_C:g}°C above {lower}")
    if not _is_number(t.margin) or not MARGIN_MIN_C <= t.margin <= MARGIN_MAX_C:
        errors.append(f"margin must be {MARGIN_MIN_C:g}-{MARGIN_MAX_C:g}°C, got {t.margin!r}")
    if not isinstance(t.hold_s, int) or isinstance(t.hold_s, bool) or not HOLD_MIN_S <= t.hold_s <= HOLD_MAX_S:
        errors.append(f"hold_s must be a whole number of seconds {HOLD_MIN_S}-{HOLD_MAX_S}, got {t.hold_s!r}")
    if len(t.ambient_rules) > MAX_AMBIENT_RULES:
        errors.append(f"ambient rules: at most {MAX_AMBIENT_RULES}, got {len(t.ambient_rules)}")
    inlets = [r.inlet_min for r in t.ambient_rules]
    if len(set(inlets)) != len(inlets):
        errors.append("ambient rules: two rules with the same inlet")
    for r in t.ambient_rules:
        if not _is_number(r.inlet_min) or not INLET_MIN_C <= r.inlet_min <= INLET_MAX_C:
            errors.append(f"ambient rule inlet must be {INLET_MIN_C:g}-{INLET_MAX_C:g}°C, got {r.inlet_min!r}")
        if not isinstance(r.level, Level) or r.level == Level.OFF:
            errors.append(f"ambient rule level must be Low to Max, got {r.level!r}")
    return errors


@dataclass(frozen=True)
class Change:
    """One change in a write: `param` goes from `from_` (its current value) to `to`."""

    param: str
    from_: object
    to: object


class StaleFrom(ValueError):
    """A change's `from_` isn't the current value: the writer must re-read."""

    def __init__(self, param: str, message: str):
        super().__init__(message)
        self.param = param


class BadChange(ValueError):
    """Unknown parameter, no-op change, or a value of the wrong type."""


def apply_changes(t: Thresholds, changes: Sequence[Change]) -> Thresholds:
    """Apply changes in order; each `from_` must equal the value at that point."""
    for change in changes:
        t = _apply_one(t, change)
    return t


def _apply_one(t: Thresholds, c: Change) -> Thresholds:
    if c.param in _SCALAR_FIELDS:
        field = _SCALAR_FIELDS[c.param]
        if not _is_number(c.from_) or not _is_number(c.to):
            raise BadChange(f"{c.param}: values must be numbers")
        if c.from_ == c.to:
            raise BadChange(f"{c.param}: no-op change")
        current = getattr(t, field)
        if current != c.from_:
            raise StaleFrom(c.param, f"{c.param} is {current!r}, not {c.from_!r}")
        return dataclasses.replace(t, **{field: c.to})

    rules = list(t.ambient_rules)
    if c.param == "ambient.add":
        if c.from_ is not None or not isinstance(c.to, AmbientRule):
            raise BadChange("ambient.add: from must be empty and to a rule")
        rules.append(c.to)
    elif c.param == "ambient.remove":
        if not isinstance(c.from_, AmbientRule) or c.to is not None:
            raise BadChange("ambient.remove: from must be a rule and to empty")
        if c.from_ not in rules:
            raise StaleFrom(c.param, f"no ambient rule {c.from_.to_dict()}")
        rules.remove(c.from_)
    elif c.param == "ambient.replace":
        if not isinstance(c.from_, AmbientRule) or not isinstance(c.to, AmbientRule):
            raise BadChange("ambient.replace: from and to must be rules")
        if c.from_ == c.to:
            raise BadChange("ambient.replace: no-op change")
        if c.from_ not in rules:
            raise StaleFrom(c.param, f"no ambient rule {c.from_.to_dict()}")
        rules[rules.index(c.from_)] = c.to
    else:
        raise BadChange(f"unknown parameter: {c.param!r}")
    return dataclasses.replace(t, ambient_rules=_sorted(rules))


def direction(c: Change) -> str:
    """'louder' or 'quieter' (spec 5.4 table). Anything mixed counts as quieter."""
    if c.param.startswith("step_up."):
        return "louder" if c.to < c.from_ else "quieter"
    if c.param in ("margin", "hold_s"):
        return "louder" if c.to > c.from_ else "quieter"
    if c.param == "ambient.add":
        return "louder"
    if c.param == "ambient.remove":
        return "quieter"
    if c.param == "ambient.replace":
        louder = c.to.inlet_min <= c.from_.inlet_min and c.to.level >= c.from_.level
        return "louder" if louder and c.to != c.from_ else "quieter"
    raise BadChange(f"unknown parameter: {c.param!r}")
