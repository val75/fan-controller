"""Test doubles shared by several test modules."""
from typing import Callable, Dict, List, Tuple, Union

from fanloop.commands import CmdResult

Response = Union[CmdResult, Callable[[], CmdResult]]


def ok(stdout: str = "", stderr: str = "") -> CmdResult:
    return CmdResult(ok=True, returncode=0, stdout=stdout, stderr=stderr, timed_out=False, skipped=False)


def fail(stderr: str = "error", returncode: int = 1, stdout: str = "") -> CmdResult:
    return CmdResult(ok=False, returncode=returncode, stdout=stdout, stderr=stderr, timed_out=False, skipped=False)


class FakeRunner:
    """Answers commands from a script: argv -> list of responses, used in order
    (the last one repeats). Records every call."""

    def __init__(self, script: Dict[Tuple[str, ...], List[Response]] = None):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.calls: List[Tuple[str, ...]] = []

    def run(self, argv, timeout_s):
        self.calls.append(tuple(argv))
        responses = self.script.get(tuple(argv))
        if not responses:
            return fail(f"no scripted response for {argv}")
        response = responses.pop(0) if len(responses) > 1 else responses[0]
        return response() if callable(response) else response

    def stuck_count(self):
        return 0
