"""Shared code for Hermes's fan scripts on hag (plan 2): settings, the loop's API, state and notes.

Standard library only: these scripts run under Hermes's own Python (cron) and the system
python3 (the model's commands). Nothing here prints the write token or the loop's address."""
import json
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import List, Tuple

DEFAULT_SETTINGS = os.path.expanduser("~/.config/fan-controller/hermes.json")


class LoopError(Exception):
    """One line, safe to print and to deliver to Telegram."""


def _one_line(text, limit: int = 200) -> str:
    return " ".join(str(text).split())[:limit]


@dataclass
class Settings:
    loop_url: str
    token_file: str
    state_dir: str
    timeout_s: float = 20.0


def load_settings(path: str = DEFAULT_SETTINGS) -> Settings:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return Settings(str(d["loop_url"]).rstrip("/"), str(d["token_file"]), str(d["state_dir"]),
                        float(d.get("timeout_s", 20.0)))
    except (OSError, ValueError, KeyError, TypeError):
        raise LoopError(f"Settings missing or invalid: {path}") from None


class Loop:
    """The fan loop's HTTP API: reads are open, writes carry the token."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def _token(self) -> str:
        try:
            with open(self.settings.token_file, encoding="utf-8") as f:
                token = f.read().strip()
        except OSError:
            token = ""
        if not token:
            raise LoopError(f"No write token at {self.settings.token_file}")
        return token

    def _call(self, req: urllib.request.Request, ok: Tuple[int, ...]) -> Tuple[int, dict]:
        try:
            with urllib.request.urlopen(req, timeout=self.settings.timeout_s) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
            if status in (401, 403):
                raise LoopError("The fan loop refused the write token") from None
            if status not in ok:
                try:
                    detail = json.loads(raw).get("detail") or ""
                except (ValueError, AttributeError):
                    detail = ""
                raise LoopError(_one_line(f"The fan loop answered {status}: {detail}")) from None
        except urllib.error.URLError as e:
            raise LoopError(f"Can't reach the fan loop: {_one_line(e.reason)}") from None
        except OSError as e:  # timeouts, resets
            raise LoopError(f"Can't reach the fan loop: {_one_line(e) or type(e).__name__}") from None
        try:
            body = json.loads(raw)
        except ValueError:
            raise LoopError("The fan loop's reply isn't JSON") from None
        if not isinstance(body, dict):
            raise LoopError("The fan loop's reply isn't JSON") from None
        return status, body

    def get(self, path: str, **query) -> dict:
        q = urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        url = self.settings.loop_url + path + ("?" + q if q else "")
        return self._call(urllib.request.Request(url), (200,))[1]

    def post(self, path: str, body: dict) -> Tuple[int, dict]:
        """200 and 409 (a rejected write) come back as (status, body); anything else raises."""
        req = urllib.request.Request(self.settings.loop_url + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {self._token()}"})
        return self._call(req, (200, 409))


class State:
    """state.json (last review, open mark) and notes.jsonl (Hermes's change log) in one folder."""

    def __init__(self, state_dir: str):
        self.dir = state_dir

    def _ensure(self) -> None:
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        os.chmod(self.dir, 0o700)

    def load(self) -> dict:
        try:
            with open(os.path.join(self.dir, "state.json"), encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self, d: dict) -> None:
        self._ensure()
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".state-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(d, f)
            os.replace(tmp, os.path.join(self.dir, "state.json"))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def add_note(self, entry: dict) -> None:
        self._ensure()
        with open(os.path.join(self.dir, "notes.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def notes(self, n: int) -> List[dict]:
        try:
            with open(os.path.join(self.dir, "notes.jsonl"), encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict):
                out.append(d)
        return out[-n:] if n > 0 else []
