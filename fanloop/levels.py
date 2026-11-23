"""Fan offset levels and the racadm values for them."""
import enum


class Level(enum.IntEnum):
    """iDRAC fan speed offset levels, in order of loudness."""

    OFF = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    MAX = 4


# racadm's values are not in order (spec section 3).
RACADM_CODE = {
    Level.OFF: "255",
    Level.LOW: "0",
    Level.MEDIUM: "2",
    Level.HIGH: "1",
    Level.MAX: "3",
}

_PREFIX = "FanSpeedOffset="


def level_from_name(name: str) -> Level:
    """Map 'off', 'low', 'medium', 'high' or 'max' (any case) to a Level."""
    try:
        return Level[name.strip().upper()]
    except KeyError:
        raise ValueError(f"unknown fan level: {name!r}") from None


def parse_racadm_offset(output: str) -> Level:
    """Read the level from `racadm get system.thermalsettings.FanSpeedOffset` output."""
    for line in output.splitlines():
        line = line.strip()
        if line.startswith(_PREFIX):
            words = line[len(_PREFIX):].split()
            if words:
                return level_from_name(words[0])
    raise ValueError(f"no FanSpeedOffset in racadm output: {output.strip()[:200]!r}")
