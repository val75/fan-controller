"""Replay the 2026-11-09 sustained-load logs (nvidia-smi CSV) through the loop's logic."""
import csv
from datetime import datetime
from pathlib import Path
from typing import List, Tuple, Union

from fanloop.busy import BusyDetector
from fanloop.control import ControlInputs, Controller
from fanloop.levels import Level
from fanloop.readings import CardReading
from fanloop.thresholds import Thresholds

Row = Tuple[float, List[CardReading]]


def _number(text: str) -> float:
    """'585 MHz', '26.62 W', '0 %' -> the number."""
    return float(text.split()[0])


def read_fan_test_csv(path: Union[str, Path]) -> List[Row]:
    """One (unix timestamp, cards) per poll; a poll starts at each card-0 row."""
    rows: List[Row] = []
    with open(path, newline="") as f:
        reader = csv.reader(f, skipinitialspace=True)
        next(reader)  # header
        for rec in reader:
            if not rec:
                continue
            ts = datetime.strptime(rec[0], "%Y/%m/%d %H:%M:%S.%f").timestamp()
            reading = CardReading(
                index=int(rec[1]),
                temp_c=float(rec[3]),
                power_w=_number(rec[4]),
                util_pct=_number(rec[5]),
                clock_mhz=_number(rec[2]),
                sw_power_cap=rec[6] == "Active",
                sw_thermal=rec[7] == "Active",
                hw_thermal=rec[8] == "Active",
            )
            if reading.index == 0 or not rows:
                rows.append((ts, [reading]))
            else:
                rows[-1][1].append(reading)
    return rows


def run_control(rows: List[Row], thresholds: Thresholds) -> List[Tuple[float, Level, str]]:
    """Replay the control rule over a log, open loop: the log was recorded at a
    fixed offset, so this shows when the loop would step, not what a step does.
    Returns the level changes only, starting from Off."""
    controller = Controller(thresholds)
    busy = BusyDetector()
    level = Level.OFF
    changes: List[Tuple[float, Level, str]] = []
    for ts, cards in rows:
        inputs = ControlInputs(now=ts, cards=cards, busy=busy.update(ts, cards),
                               inlet_c=None, system_power_w=None, hold_min=None)
        decision = controller.decide(inputs, level)
        if decision.level != level:
            changes.append((ts, decision.level, decision.reason))
            level = decision.level
    return changes
