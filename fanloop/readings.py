"""Sensor readings shared by the control, recording and API code."""
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class CardReading:
    """One GPU's reading from nvidia-smi."""

    index: int
    temp_c: float
    power_w: float
    util_pct: float
    clock_mhz: float
    sw_power_cap: bool
    sw_thermal: bool
    hw_thermal: bool


@dataclass(frozen=True)
class IpmiReading:
    """iDRAC sensors read through local ipmitool."""

    inlet_c: Optional[float]
    exhaust_c: Optional[float]
    fans_rpm: Dict[str, int]
    power_w: Optional[float]
