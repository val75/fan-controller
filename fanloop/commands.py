"""External commands: the only lines run as root (spec 5.10), and a runner that
never lets a hanging command stall the loop (spec 7)."""
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from fanloop.levels import RACADM_CODE, Level

SUDO = ("/usr/bin/sudo", "-n")
RACADM = "/usr/bin/racadm"
IPMITOOL = "/usr/bin/ipmitool"
_OFFSET_ATTR = "system.thermalsettings.FanSpeedOffset"

RACADM_GET = SUDO + (RACADM, "get", _OFFSET_ATTR)
IPMI_TEMPS = SUDO + (IPMITOOL, "sdr", "type", "temperature")
IPMI_FANS = SUDO + (IPMITOOL, "sdr", "type", "fan")
IPMI_POWER = SUDO + (IPMITOOL, "dcmi", "power", "reading")


def racadm_set(level: Level) -> Tuple[str, ...]:
    return SUDO + (RACADM, "set", _OFFSET_ATTR, RACADM_CODE[level])


# The exact command lines the loop's user may run as root: the sudoers file
# (deploy/sudoers-fan-controller) must allow these and nothing else.
ROOT_COMMAND_LINES = tuple(
    " ".join(argv[len(SUDO):])
    for argv in [RACADM_GET] + [racadm_set(level) for level in Level] + [IPMI_TEMPS, IPMI_FANS, IPMI_POWER]
)

NVIDIA_QUERY = (
    "/usr/bin/nvidia-smi",
    "--query-gpu=index,temperature.gpu,power.draw,utilization.gpu,clocks.current.graphics,"
    "clocks_event_reasons.sw_power_cap,clocks_event_reasons.sw_thermal_slowdown,"
    "clocks_event_reasons.hw_thermal_slowdown",
    "--format=csv,noheader,nounits",
)


@dataclass(frozen=True)
class CmdResult:
    ok: bool
    returncode: Optional[int]
    stdout: str
    stderr: str
    timed_out: bool
    skipped: bool


KILL_GRACE_S = 2.0  # between SIGTERM and SIGKILL on a timeout
KILLED_COOLDOWN_S = 60.0


def _kill(proc: subprocess.Popen) -> None:
    try:
        proc.kill()
    except OSError:
        pass


def _terminate(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
    except OSError:
        pass


class CommandRunner:
    """Runs a command with a timeout and never waits for a process that won't die.

    On a timeout it sends SIGTERM first: sudo passes SIGTERM on to the root command
    it runs, but SIGKILL only kills sudo itself and leaves the root command orphaned,
    where the loop can no longer see or signal it. After KILL_GRACE_S it sends SIGKILL.

    A process stuck in the kernel (e.g. nvidia-smi when a GPU fell off the bus)
    ignores even SIGKILL: it stays on a stuck list, and no second copy starts while
    it lives. A sudo command that needed SIGKILL may have left an invisible root
    orphan behind, so that command line waits `cooldown_s` before it runs again:
    at most one orphan per command line per cooldown."""

    def __init__(self, kill: Callable[[subprocess.Popen], None] = _kill,
                 terminate: Callable[[subprocess.Popen], None] = _terminate,
                 grace_s: float = KILL_GRACE_S, cooldown_s: float = KILLED_COOLDOWN_S,
                 clock: Callable[[], float] = time.monotonic,
                 can_orphan: Callable[[Tuple[str, ...]], bool] = lambda argv: argv[:1] == SUDO[:1]):
        self._kill = kill
        self._terminate = terminate
        self._grace_s = grace_s
        self._cooldown_s = cooldown_s
        self._clock = clock
        self._can_orphan = can_orphan
        self._stuck: Dict[Tuple[str, ...], List[subprocess.Popen]] = {}
        self._cooldown: Dict[Tuple[str, ...], float] = {}
        self._lock = threading.Lock()
        self._env = {**os.environ, "LC_ALL": "C"}

    def _reap(self) -> None:
        for argv in list(self._stuck):
            alive = []
            for proc in self._stuck[argv]:
                if proc.poll() is None:
                    alive.append(proc)
                else:
                    for pipe in (proc.stdout, proc.stderr):
                        if pipe:
                            pipe.close()
            if alive:
                self._stuck[argv] = alive
            else:
                del self._stuck[argv]

    def stuck_count(self) -> int:
        with self._lock:
            self._reap()
            return sum(len(procs) for procs in self._stuck.values())

    def run(self, argv: Tuple[str, ...], timeout_s: float) -> CmdResult:
        argv = tuple(argv)
        with self._lock:
            self._reap()
            if argv in self._stuck:
                return CmdResult(False, None, "", "previous run still stuck", False, True)
            until = self._cooldown.get(argv)
            if until is not None:
                if self._clock() < until:
                    return CmdResult(False, None, "", "the last run ignored SIGTERM and was killed; "
                                                      "waiting before trying again", False, True)
                del self._cooldown[argv]
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, errors="replace", env=self._env)
        except OSError as e:
            return CmdResult(False, None, "", f"cannot run {argv[0]}: {e}", False, False)
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            self._terminate(proc)
            try:
                proc.communicate(timeout=self._grace_s)  # it ended on SIGTERM; this also closes the pipes
            except subprocess.TimeoutExpired:
                self._kill(proc)
                with self._lock:
                    if self._can_orphan(argv):
                        self._cooldown[argv] = self._clock() + self._cooldown_s
                    self._stuck.setdefault(argv, []).append(proc)
            return CmdResult(False, None, "", f"timed out after {timeout_s:g} s", True, False)
        return CmdResult(proc.returncode == 0, proc.returncode, stdout, stderr, False, False)
