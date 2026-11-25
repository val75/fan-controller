"""Reading and setting the iDRAC fan offset with local racadm, verified by read-back."""
import time
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from fanloop.commands import RACADM_GET, racadm_set
from fanloop.levels import Level, parse_racadm_offset


@dataclass(frozen=True)
class SetResult:
    ok: bool
    level: Optional[Level]  # what the read-back showed (the target in dry-run)
    attempts: int
    error: Optional[str]


_MIN_CALL_S = 1.0  # don't start a racadm call with less time left than this


class Offset:
    """`timeout_s` limits each racadm call; `budget_s` limits one whole set with its
    retries, so a slow iDRAC can't hold a poll past systemd's watchdog."""

    def __init__(self, runner, dry_run: bool, retries: int = 3, timeout_s: float = 10.0,
                 budget_s: float = 25.0, clock: Callable[[], float] = time.monotonic):
        self.runner = runner
        self.dry_run = dry_run
        self.retries = retries
        self.timeout_s = timeout_s
        self.budget_s = budget_s
        self.clock = clock

    def _read(self, timeout_s: float) -> Tuple[Optional[Level], Optional[str], bool]:
        r = self.runner.run(RACADM_GET, timeout_s)
        text = (r.stdout + r.stderr).strip()
        if not r.ok:
            return None, text or f"racadm get failed (exit {r.returncode})", r.timed_out or r.skipped
        try:
            return parse_racadm_offset(r.stdout), None, False
        except ValueError:
            return None, text or "racadm get printed nothing", False

    def read(self) -> Tuple[Optional[Level], Optional[str]]:
        level, error, _ = self._read(self.timeout_s)
        return level, error

    def set(self, level: Level) -> SetResult:
        """Set the offset, then read it back. Only the read-back counts: racadm
        can print an error and still exit 0. Retries only after a quick failure: a
        racadm that hung once will hang again, and each try costs a timeout."""
        if self.dry_run:
            return SetResult(True, level, 0, None)
        deadline = self.clock() + self.budget_s
        last_level, error, attempt = None, "no time left to try", 0

        def left() -> float:
            return min(self.timeout_s, deadline - self.clock())

        while attempt < self.retries and left() >= _MIN_CALL_S:
            attempt += 1
            r = self.runner.run(racadm_set(level), left())
            set_text = (r.stdout + r.stderr).strip()
            if r.timed_out or r.skipped:
                return SetResult(False, None, attempt, f"set {level.name.lower()}: {set_text}")
            if left() < _MIN_CALL_S:
                return SetResult(False, None, attempt, f"set {level.name.lower()}: no time left to read it back")
            last_level, read_error, hung = self._read(left())
            if last_level == level:
                return SetResult(True, level, attempt, None)
            shown = last_level.name.lower() if last_level is not None else read_error
            error = f"set {level.name.lower()}: {set_text or 'no output'}; read back: {shown}"
            if hung:
                break
        return SetResult(False, last_level, attempt, error)
