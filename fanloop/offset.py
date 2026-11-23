"""Reading and setting the iDRAC fan offset with local racadm, verified by read-back."""
from dataclasses import dataclass
from typing import Optional, Tuple

from fanloop.commands import RACADM_GET, racadm_set
from fanloop.levels import Level, parse_racadm_offset


@dataclass(frozen=True)
class SetResult:
    ok: bool
    level: Optional[Level]  # what the read-back showed (the target in dry-run)
    attempts: int
    error: Optional[str]


class Offset:
    def __init__(self, runner, dry_run: bool, retries: int = 3, timeout_s: float = 20.0):
        self.runner = runner
        self.dry_run = dry_run
        self.retries = retries
        self.timeout_s = timeout_s

    def read(self) -> Tuple[Optional[Level], Optional[str]]:
        r = self.runner.run(RACADM_GET, self.timeout_s)
        text = (r.stdout + r.stderr).strip()
        if not r.ok:
            return None, text or f"racadm get failed (exit {r.returncode})"
        try:
            return parse_racadm_offset(r.stdout), None
        except ValueError:
            return None, text or "racadm get printed nothing"

    def set(self, level: Level) -> SetResult:
        """Set the offset, then read it back. Only the read-back counts: racadm
        can print an error and still exit 0."""
        if self.dry_run:
            return SetResult(True, level, 0, None)
        last_level, error = None, None
        for attempt in range(1, self.retries + 1):
            r = self.runner.run(racadm_set(level), self.timeout_s)
            set_text = (r.stdout + r.stderr).strip()
            last_level, read_error = self.read()
            if last_level == level:
                return SetResult(True, level, attempt, None)
            shown = last_level.name.lower() if last_level is not None else read_error
            error = f"set {level.name.lower()}: {set_text or 'no output'}; read back: {shown}"
        return SetResult(False, last_level, self.retries, error)
