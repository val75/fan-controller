"""Reading the GPUs (nvidia-smi), the iDRAC sensors (ipmitool) and llama-server's health."""
import re
import urllib.request
from typing import Dict, List, Optional, Tuple

from fanloop.commands import IPMI_FANS, IPMI_POWER, IPMI_TEMPS, NVIDIA_QUERY, CmdResult
from fanloop.readings import CardReading, IpmiReading

_FLAGS = {"Active": True, "Not Active": False}
_POWER_RE = re.compile(r"Instantaneous power reading:\s*([0-9.]+)\s*Watts")


def parse_nvidia(stdout: str) -> Tuple[List[CardReading], List[str]]:
    """Cards from `--format=csv,noheader,nounits` output, plus the lines that
    couldn't be read (e.g. a card showing [Unknown Error] or [N/A])."""
    cards, errors = [], []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        fields = [f.strip() for f in line.split(",")]
        try:
            if len(fields) != 8:
                raise ValueError("expected 8 fields")
            flags = [_FLAGS[f] for f in fields[5:8]]
            cards.append(CardReading(
                index=int(fields[0]),
                temp_c=float(fields[1]),
                power_w=float(fields[2]),
                util_pct=float(fields[3]),
                clock_mhz=float(fields[4]),
                sw_power_cap=flags[0],
                sw_thermal=flags[1],
                hw_thermal=flags[2],
            ))
        except (ValueError, KeyError) as e:
            errors.append(f"{line.strip()} ({e})")
    return cards, errors


def _sdr_rows(stdout: str):
    for line in stdout.splitlines():
        cols = [c.strip() for c in line.split("|")]
        if len(cols) == 5:
            yield cols[0], cols[4]


def _degrees(value: str) -> Optional[float]:
    m = re.match(r"(-?[0-9.]+) degrees C", value)
    return float(m.group(1)) if m else None


def parse_sdr_temps(stdout: str) -> Tuple[Optional[float], Optional[float]]:
    """(inlet, exhaust) from `ipmitool sdr type temperature`."""
    inlet = exhaust = None
    for name, value in _sdr_rows(stdout):
        if name.startswith("Inlet Temp"):
            inlet = _degrees(value)
        elif name.startswith("Exhaust Temp"):
            exhaust = _degrees(value)
    return inlet, exhaust


def parse_sdr_fans(stdout: str) -> Dict[str, int]:
    """Fan name -> RPM from `ipmitool sdr type fan` (rows without RPM are skipped)."""
    fans = {}
    for name, value in _sdr_rows(stdout):
        m = re.match(r"([0-9]+) RPM", value)
        if m:
            fans[name] = int(m.group(1))
    return fans


def parse_dcmi_power(stdout: str) -> Optional[float]:
    """System power in watts from `ipmitool dcmi power reading`."""
    m = _POWER_RE.search(stdout)
    return float(m.group(1)) if m else None


def _failure_text(r: CmdResult) -> str:
    return (r.stderr or r.stdout).strip() or f"exit status {r.returncode}"


class Sensors:
    def __init__(self, runner, nvidia_timeout_s: float = 10.0, ipmi_timeout_s: float = 15.0):
        self.runner = runner
        self.nvidia_timeout_s = nvidia_timeout_s
        self.ipmi_timeout_s = ipmi_timeout_s

    def gpus(self) -> Tuple[List[CardReading], Optional[str]]:
        """The readable cards, and an error text if anything was missing."""
        r = self.runner.run(NVIDIA_QUERY, self.nvidia_timeout_s)
        if not r.ok:
            return [], _failure_text(r)
        cards, errors = parse_nvidia(r.stdout)
        if errors:
            return cards, "; ".join(errors)
        if not cards:
            return [], "nvidia-smi printed no GPUs"
        return cards, None

    def _ipmi(self, argv) -> str:
        r = self.runner.run(argv, self.ipmi_timeout_s)
        return r.stdout if r.ok else ""

    def ipmi(self) -> IpmiReading:
        inlet, exhaust = parse_sdr_temps(self._ipmi(IPMI_TEMPS))
        return IpmiReading(inlet, exhaust, parse_sdr_fans(self._ipmi(IPMI_FANS)), self.power())

    def power(self) -> Optional[float]:
        return parse_dcmi_power(self._ipmi(IPMI_POWER))


def llama_health(url: str, timeout_s: float = 3.0) -> bool:
    """True if llama-server answers its health URL with HTTP 200."""
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            return resp.status == 200
    except Exception:
        return False
