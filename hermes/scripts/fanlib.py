"""Shared code for Hermes's fan scripts on hag (plan 2): settings, the loop's API, state and notes.

Standard library only: these scripts run under Hermes's own Python (cron) and the system
python3 (the model's commands). Nothing here prints the write token or the loop's address."""
import json
import os
import statistics
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None

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


# -- analysis -----------------------------------------------------------------------------------
# Episode dicts are /episodes rows (fanloop/episodes.py); samples are /history rows.

SETTLE_MIN_S = 360  # spec 5.5: sustained load settles in 4.5-6 min
LOUD = ("high", "max")
LEVELS = ("off", "low", "medium", "high", "max")


def hotter_peak(ep: dict) -> Optional[float]:
    values = [v for v in (ep.get("peak_c") or {}).values() if v is not None]
    return max(values) if values else None


def qualifies(ep: dict) -> bool:
    """The loop's evidence rule (spec 5.4), for display only: the loop's own count decides."""
    return ep.get("duration_s", 0) >= SETTLE_MIN_S and not ep.get("review_overlap") and ep.get("hold_level") is None


def dominant_level(ep: dict) -> Optional[str]:
    # shortcut: dominant level = most seconds, not the level the episode settled at; use samples if estimates look off
    level_s = ep.get("level_s") or {}
    return max(level_s, key=level_s.get) if level_s else None


def settle_by_level(eps: List[dict]) -> Dict[str, dict]:
    """How far the hotter card ends up above inlet, per level (spec 6.3), from qualifying episodes."""
    rises: Dict[str, List[float]] = {}
    for e in eps:
        peak, level = hotter_peak(e), dominant_level(e)
        if qualifies(e) and peak is not None and e.get("inlet_mean") is not None and level:
            rises.setdefault(level, []).append(peak - e["inlet_mean"])
    return {level: {"n": len(v), "median": round(statistics.median(v), 1), "min": round(min(v), 1),
                    "max": round(max(v), 1)}
            for level, v in rises.items()}


def _range(values) -> Optional[Tuple[float, float]]:
    values = [v for v in values if v is not None]
    return (min(values), max(values)) if values else None


def version_stats(eps: List[dict]) -> dict:
    busy_s = sum(e.get("duration_s", 0) for e in eps)
    slowdown_s = sum(e.get("throttled_s", 0) for e in eps)
    level_total = sum(sum((e.get("level_s") or {}).values()) for e in eps)
    loud = sum((e.get("level_s") or {}).get(name, 0) for e in eps for name in LOUD)
    changes = sum(e.get("level_changes", 0) for e in eps)
    hours = busy_s / 3600
    return {
        "n": len(eps),
        "qualifying": sum(1 for e in eps if qualifies(e)),
        "busy_s": busy_s,
        "slowdown_s": slowdown_s,
        "slowdown_per_h": slowdown_s / hours if hours else 0,
        "loud_share": loud / level_total if level_total else 0,
        "changes_per_h": changes / hours if hours else 0,
        "inlet": _range([e.get("inlet_min") for e in eps] + [e.get("inlet_max") for e in eps]),
        "duration": _range([e.get("duration_s") for e in eps]),
    }


def compare(a: dict, b: dict) -> str:
    """b against a, in spec 6.2's order: no slowdown, then less time loud, then fewer level changes."""
    if not a["qualifying"] or not b["qualifying"]:
        return "not enough data"
    for key, margin in (("slowdown_per_h", 1.0), ("loud_share", 0.05), ("changes_per_h", 0.5)):
        diff = b[key] - a[key]
        if diff < -margin:
            return "better"
        if diff > margin:
            return "worse"
    return "same"


def _hotter(sample: dict) -> Optional[float]:
    values = [sample.get(k) for k in ("temp0", "temp1") if sample.get(k) is not None]
    return max(values) if values else None


def step_response(samples: List[dict], at: float) -> Optional[dict]:
    """The hotter card at a step and 3 minutes later (spec 6.3: how fast temperatures respond)."""
    usable = [s for s in samples if _hotter(s) is not None]
    before = [s for s in usable if s["ts"] <= at + 90]
    after = [s for s in usable if s["ts"] > at + 90]
    if not before or not after:
        return None
    t_at = _hotter(min(before, key=lambda s: abs(s["ts"] - at)))
    t_after = _hotter(min(after, key=lambda s: abs(s["ts"] - (at + 180))))
    return {"temp_at": round(t_at, 1), "temp_after": round(t_after, 1), "delta": round(t_after - t_at, 1)}


# -- report text -----------------------------------------------------------------------------------

def fmt_time(ts: float, tz_name: str) -> str:
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    return datetime.fromtimestamp(ts, tz).strftime("%a %H:%M")


def _t(v) -> str:
    return "?" if v is None else f"{v:.1f}"


def _n(v) -> str:
    return "?" if v is None else f"{v:g}"


def _rule(r) -> str:
    return "none" if r is None else f"{_n(r.get('inlet_min'))}:{r.get('level')}"


def _cards(status: dict) -> str:
    temps = {c.get("index"): c.get("temp_c") for c in status.get("cards") or []}
    if not temps:
        return "cards unreadable"
    return f"cards {_t(temps.get(0))}/{_t(temps.get(1))}°C"


def _thresholds(status: dict) -> str:
    t = status.get("thresholds") or {}
    rules = ", ".join(f"≥{_n(r.get('inlet_min'))}°C {str(r.get('level')).capitalize()}"
                      for r in t.get("ambient_rules") or []) or "none"
    return (f"thresholds v{status.get('version', '?')}: Low {_n(t.get('low'))}, Medium {_n(t.get('medium'))}, "
            f"High {_n(t.get('high'))}, margin {_n(t.get('margin'))}, hold {_n(t.get('hold_s'))} s, ambient {rules}")


def _problems(status: dict) -> List[str]:
    health = status.get("health") or {}
    out = [f"{k} false" for k in ("nvidia_ok", "ipmi_ok", "racadm_ok", "store_ok") if health.get(k) is False]
    if health.get("set_failed"):
        out.append("set_failed")
    if status.get("store_error"):
        out.append(f"store_error {_one_line(status['store_error'], 300)}")
    return out


def _change(c: dict) -> str:
    def value(v):
        return _rule(v) if isinstance(v, dict) or (v is None and c["param"].startswith("ambient")) else _n(v)
    return f"{c['param']} {value(c.get('from'))} → {value(c.get('to'))}"


def _version_line(v: dict) -> str:
    changes = ", ".join(_change(c) for c in v.get("changes") or [])
    return f"v{v['version'] - 1} → v{v['version']} {v.get('source')}: {changes} ({_one_line(v.get('reason') or '', 200)})"


def _stats_line(label: str, s: dict) -> str:
    dur = "" if s["duration"] is None else f", {s['duration'][0] / 60:.0f}–{s['duration'][1] / 60:.0f} min"
    inlet = "" if s["inlet"] is None else f", inlet {_t(s['inlet'][0])}–{_t(s['inlet'][1])}°C"
    return (f"{label}: {s['n']} episodes ({s['qualifying']} qualifying){dur}{inlet}, "
            f"slowdown {s['slowdown_per_h']:.0f} s per busy hour, loud (High/Max) {s['loud_share'] * 100:.0f}% "
            f"of the time, {s['changes_per_h']:.1f} level changes per hour")


def now_lines(status: dict) -> List[str]:
    """The report's first two lines: what the fans do now, and any problems."""
    return [f"Now: {status.get('level') or 'unknown'} ({status.get('reason') or '?'}), {_cards(status)}, "
            f"inlet {_t(status.get('inlet_c'))}°C, {_thresholds(status)}",
            "Problems: " + (", ".join(_problems(status)) or "none")]


def settle_line(settle: Dict[str, dict]) -> str:
    return "Settles above inlet (qualifying episodes): " + ("; ".join(
        f"{level.capitalize()} +{v['median']:.1f}°C (n {v['n']}, {v['min']:.1f}–{v['max']:.1f})"
        for level, v in sorted(settle.items(), key=lambda kv: LEVELS.index(kv[0]) if kv[0] in LEVELS else 9))
        or "not enough data")


def review_report(status: dict, summary: dict, eps_since: List[dict], eps_current: List[dict],
                  changes: List[dict], steps: List[dict], notes: List[dict], since: float, now: float,
                  truncated: bool = False) -> str:
    tz = status.get("timezone") or "EET"
    version = status.get("version", "?")
    lines = now_lines(status)

    head = f"Since last review ({fmt_time(since, tz)}): "
    if eps_since:
        s = version_stats(eps_since)
        peaks = [p for p in map(hotter_peak, eps_since) if p is not None]
        inlet = "" if s["inlet"] is None else f", inlet {_t(s['inlet'][0])}–{_t(s['inlet'][1])}°C"
        lines.append(head + f"{s['n']} episodes, {s['busy_s'] / 60:.0f} min busy, slowdown {_n(s['slowdown_s'])} s, "
                            f"peak {_t(max(peaks) if peaks else None)}°C{inlet}")
    else:
        lines.append(head + "no episodes")
    if truncated:
        lines.append("(list truncated)")

    cur = version_stats(eps_current)
    lines.append(f"On v{version}: {cur['qualifying']} qualifying episodes (loop's count: "
                 f"{summary.get('qualifying_on_current', '?')}; a quieter review change needs 3)")
    lines.append(_stats_line(f"v{version} stats", cur))

    seen = {e.get("start") for e in eps_current}
    lines.append(settle_line(settle_by_level(eps_current + [e for e in eps_since if e.get("start") not in seen])))

    lines.append("After step-ups:" + ("" if steps else " none"))
    for st in steps[-5:]:
        lines.append(f"  {fmt_time(st['at'], tz)} {st['from']}→{st['to']}: "
                     f"{_t(st['temp_at'])}→{_t(st['temp_after'])}°C over 3 min")

    recent = [v for v in changes if v.get("created_at", 0) >= since and v.get("changes")]
    lines.append("Changes since last review:" + ("" if recent else " none"))
    lines += ["  " + _version_line(v) for v in recent]
    day = [v for v in changes if v.get("created_at", 0) > now - 86400]
    lines.append(f"Review changes in the last 24 h: {sum(v.get('source') == 'review' for v in day)} of 2; "
                 f"user-chat: {sum(v.get('source') == 'user-chat' for v in day)} of 6")

    lines.append("Your last notes:" + ("" if notes else " none yet"))
    lines += [f"  {fmt_time(n.get('at', 0), tz)}: {_one_line(n.get('text', ''), 400)}" for n in notes[-3:]]
    return "\n".join(lines)


def daily_report(status: dict, summary: dict, changes: List[dict], now: float) -> str:
    level_s = summary.get("level_s") or {}
    peaks = [v for v in (summary.get("peak_c") or {}).values() if v is not None]
    per_level = "/".join(f"{level_s.get(k, 0) / 60:.0f}" for k in ("low", "medium", "high", "max"))
    lines = [f"Fans, last 24 h: {summary.get('episodes', 0)} episodes, {sum(level_s.values()) / 60:.0f} min busy, "
             f"time at Low/Medium/High/Max {per_level} min, slowdown {_n(summary.get('slowdown_s', 0))} s, "
             f"peak {_t(max(peaks) if peaks else None)}°C, max inlet {_t(summary.get('max_inlet'))}°C.",
             f"Now: {status.get('level') or 'unknown'}, {_cards(status)}, inlet {_t(status.get('inlet_c'))}°C, "
             f"thresholds v{status.get('version', '?')}."]
    day = [v for v in changes if v.get("created_at", 0) > now - 86400 and v.get("changes")]
    lines += [f"Changed: {_version_line(v)}" for v in day] or ["No threshold changes."]
    problems = _problems(status)
    if problems:
        lines.append("Problems: " + ", ".join(problems))
    return "\n".join(lines)
