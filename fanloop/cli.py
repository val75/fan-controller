"""fanctl: the local command for the fan loop, over its Unix socket (spec 5.6).

Exit codes: 0 ok, 1 rejected by the loop, 2 usage error, 3 the loop can't be reached."""
import argparse
import http.client
import json
import os
import sys
import time
import urllib.parse
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from fanloop.api import UnixHTTPConnection
from fanloop.clock import DEFAULT_TZ, clock_text, local, zone
from fanloop.config import Config
from fanloop.holds import parse_until
from fanloop.levels import Level, level_from_name
from fanloop.service import describe_change
from fanloop.thresholds import AmbientRule, Change

OK, REJECTED, USAGE, UNREACHABLE = 0, 1, 2, 3
PARAM_NAMES = {"low": "step_up.low", "medium": "step_up.medium", "high": "step_up.high",
               "margin": "margin", "hold": "hold_s"}
_SHOW_FIELD = {"step_up.low": "low", "step_up.medium": "medium", "step_up.high": "high",
               "margin": "margin", "hold_s": "hold_s"}
STALE_TEXT = "Thresholds changed meanwhile; run the command again."


class SocketClient:
    def __init__(self, path: str, timeout_s: float = 10.0):
        self.path = path
        self.timeout_s = timeout_s

    def _request(self, method: str, url: str, body=None) -> Tuple[int, Dict]:
        conn = UnixHTTPConnection(self.path, timeout=self.timeout_s)
        try:
            data = None if body is None else json.dumps(body).encode()
            headers = {"Content-Type": "application/json"} if data is not None else {}
            conn.request(method, url, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
        finally:
            conn.close()

    def get(self, path: str, params: Optional[Dict] = None) -> Tuple[int, Dict]:
        return self._request("GET", path + ("?" + urllib.parse.urlencode(params) if params else ""))

    def post(self, path: str, body=None) -> Tuple[int, Dict]:
        return self._request("POST", path, body)


class Usage(Exception):
    pass


class Unreachable(Exception):
    pass


class Rejected(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Usage(f"{self.prog}: {message}")


def _parser() -> argparse.ArgumentParser:
    p = _Parser(prog="fanctl", description="Control the t4 fan loop.")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    sub.add_parser("status", help="the loop's current state")
    sub.add_parser("show", help="the thresholds and their version")
    h = sub.add_parser("history", help="threshold versions and their changes")
    h.add_argument("--days", type=int, default=30)
    s = sub.add_parser("set", help="set thresholds: low|medium|high|margin|hold VALUE ...")
    s.add_argument("pairs", nargs="+", metavar="PARAM VALUE")
    s.add_argument("--reason", required=True)
    s.add_argument("--yes", action="store_true")
    a = sub.add_parser("ambient", help="add or remove an ambient rule")
    asub = a.add_subparsers(dest="action", required=True, metavar="ACTION")
    add = asub.add_parser("add", help="ambient add INLET LEVEL")
    add.add_argument("inlet")
    add.add_argument("level")
    rm = asub.add_parser("remove", help="ambient remove INLET")
    rm.add_argument("inlet")
    for x in (add, rm):
        x.add_argument("--reason")
        x.add_argument("--yes", action="store_true")
    r = sub.add_parser("revert", help="return to an earlier version")
    r.add_argument("version")
    r.add_argument("--yes", action="store_true")
    hold = sub.add_parser("hold", help="hold LEVEL [until HH:MM | until YYYY-MM-DD HH:MM]; hold off")
    hold.add_argument("words", nargs="+")
    mute = sub.add_parser("mute", help="mute reviews until YYYY-MM-DD; mute off")
    mute.add_argument("words", nargs="+")
    sub.add_parser("test-alert", help="send a test message to Telegram")
    return p


# -- small helpers ---------------------------------------------------------------------------

def _number(text: str, name: str):
    try:
        v = float(text)
    except ValueError:
        raise Usage(f"{name} must be a number, got {text!r}") from None
    return int(v) if v.is_integer() else v


def _level(text: str) -> Level:
    try:
        return level_from_name(text)
    except ValueError as e:
        raise Usage(str(e)) from None


def _num(v) -> str:
    return f"{v:g}"


def _rule_text(r: Dict) -> str:
    return f"inlet ≥{_num(r['inlet_min'])}°C → {r['level'].capitalize()}"


def _to_change(d: Dict) -> Change:
    def value(v):
        return AmbientRule.from_dict(v) if isinstance(v, dict) else v
    return Change(d["param"], value(d["from"]), value(d["to"]))


def _when(ts: float, now: float, tz) -> str:
    return clock_text(ts, now, tz, other_day="%a %Y-%m-%d %H:%M")


class _Cli:
    def __init__(self, client, stdin, stdout, now: float):
        self.client, self.stdin, self.out, self.now = client, stdin, stdout, now
        self._tz = None

    def tz(self):
        """The loop's time zone, from /status: fanctl shows and reads times in it."""
        if self._tz is None:
            self._tz = zone(self.get_ok("/status").get("timezone") or DEFAULT_TZ)
        return self._tz

    def say(self, text: str = "") -> None:
        print(text, file=self.out)

    def _call(self, method: str, path: str, arg=None) -> Tuple[int, Dict]:
        try:
            return self.client.get(path, arg) if method == "GET" else self.client.post(path, arg)
        except PermissionError as e:
            raise Unreachable(f"Can't reach the fan loop: {e}. Are you in the fanctl group?") from None
        except (OSError, http.client.HTTPException, ValueError) as e:
            where = getattr(self.client, "path", "its socket")
            raise Unreachable(f"Can't reach the fan loop at {where}: {e}") from None

    def get_ok(self, path: str, params=None) -> Dict:
        status, body = self._call("GET", path, params)
        if status != 200:
            raise Rejected(f"{body.get('error', status)}: {body.get('detail', '')}")
        return body

    def post_ok(self, path: str, body=None) -> Dict:
        status, reply = self._call("POST", path, body)
        if status != 200:
            raise Rejected(f"{reply.get('error', status)}: {reply.get('detail', '')}")
        return reply

    def confirm(self, yes: bool) -> bool:
        if yes:
            return True
        self.out.write("Apply? [y/N] ")
        self.out.flush()
        answer = self.stdin.readline().strip().lower()
        if answer != "y":
            self.say("Nothing changed.")
            return False
        return True

    def write(self, path: str, body: Dict, preview: str, yes: bool) -> int:
        self.say(preview)
        if not self.confirm(yes):
            return OK
        status, reply = self._call("POST", path, body)
        if status == 200 and reply.get("ok"):
            self.say(f"Applied: thresholds v{reply.get('old_version')} → v{reply.get('new_version')}.")
            return OK
        if reply.get("error") == "stale":
            self.say(STALE_TEXT)
            return REJECTED
        raise Rejected(f"{reply.get('error', status)}: {reply.get('detail', '')}")

    def current(self) -> Tuple[int, Dict]:
        status = self.get_ok("/status")
        return status["version"], status["thresholds"]

    def preview(self, version: int, changes: List[Change], reason: str) -> str:
        return (f"thresholds v{version} → v{version + 1}: {', '.join(describe_change(c) for c in changes)} "
                f"(source user-cli: {reason})")

    # -- commands ------------------------------------------------------------------------

    def status(self, args) -> int:
        s = self.get_ok("/status")
        level = (s.get("level") or "unknown").capitalize()
        flags = ["busy" if s.get("busy") else "idle"]
        if s.get("blind"):
            flags.append("blind (no card readings)")
        flags.append("dry-run " + ("on" if s.get("dry_run") else "off"))
        self.say(f"Level {level}" + (f" ({s['reason']})" if s.get("reason") else "") + ", " + ", ".join(flags))
        for c in s.get("cards", []):
            flag = " slowdown" if c.get("sw_thermal") or c.get("hw_thermal") else ""
            self.say(f"  card {c.get('index')}: {_num(c['temp_c'])}°C, {_num(c.get('power_w') or 0)} W, "
                     f"{_num(c.get('util_pct') or 0)}%{flag}")
        env = [f"{name} {_num(s[key])}{unit}" for key, name, unit in
               (("inlet_c", "inlet", "°C"), ("exhaust_c", "exhaust", "°C"), ("system_power_w", "system", " W"))
               if s.get(key) is not None]
        if env:
            self.say("  " + ", ".join(env))
        hold = s.get("hold")
        if hold:
            until = ("until cancelled" if hold.get("until") is None
                     else f"until {_when(hold['until'], self.now, zone(s.get('timezone') or DEFAULT_TZ))}")
            self.say(f"Hold at {hold['level'].capitalize()}, {until}")
        if s.get("review_watchdog_muted"):
            self.say("Review watchdog muted")
        health = s.get("health") or {}
        bad = [k[:-3] for k in ("nvidia_ok", "ipmi_ok", "racadm_ok") if k in health and not health[k]]
        if bad:
            self.say("Problems: " + ", ".join(bad))
        self.say(f"Thresholds v{s.get('version')}")
        return OK

    def show(self, args) -> int:
        version, t = self.current()
        self.say(f"Thresholds v{version}")
        self.say(f"  Low      {_num(t['low'])}°C")
        self.say(f"  Medium   {_num(t['medium'])}°C")
        self.say(f"  High     {_num(t['high'])}°C")
        self.say(f"  margin   {_num(t['margin'])}°C")
        self.say(f"  hold     {_num(t['hold_s'])} s")
        rules = t.get("ambient_rules") or []
        self.say("  ambient  " + ("; ".join(_rule_text(r) for r in rules) if rules else "none"))
        return OK

    def history(self, args) -> int:
        versions = self.get_ok("/changes", {"since": self.now - args.days * 86400})["versions"]
        if not versions:
            self.say(f"No threshold changes in the last {args.days} days.")
        for v in versions:
            when = local(v["created_at"], self.tz()).strftime("%a %Y-%m-%d %H:%M")
            self.say(f"v{v['version']}  {when}  {v['source']}: {v['reason']}")
            for c in v.get("changes") or []:
                self.say(f"    {describe_change(_to_change(c))}")
        return OK

    def set(self, args) -> int:
        words = args.pairs
        if len(words) % 2:
            raise Usage("set takes PARAM VALUE pairs")
        version, t = self.current()
        changes = []
        for name, value in zip(words[::2], words[1::2]):
            if name not in PARAM_NAMES:
                raise Usage(f"unknown parameter {name!r}: use {', '.join(PARAM_NAMES)}")
            param = PARAM_NAMES[name]
            changes.append(Change(param, t[_SHOW_FIELD[param]], _number(value, name)))
        body = {"reason": args.reason,
                "changes": [{"param": c.param, "from": c.from_, "to": c.to} for c in changes]}
        return self.write("/thresholds", body, self.preview(version, changes, args.reason), args.yes)

    def ambient(self, args) -> int:
        inlet = _number(args.inlet, "INLET")
        if args.action == "add":
            rule = {"inlet_min": inlet, "level": _level(args.level).name.lower()}
        version, t = self.current()
        if args.action == "add":
            change = {"param": "ambient.add", "from": None, "to": rule}
            reason = args.reason or f"fanctl ambient add {_rule_text(rule)}"
        else:
            matches = [r for r in t.get("ambient_rules") or [] if r["inlet_min"] == inlet]
            if not matches:
                raise Rejected(f"there is no ambient rule at inlet {_num(inlet)}°C")
            change = {"param": "ambient.remove", "from": matches[0], "to": None}
            reason = args.reason or f"fanctl ambient remove {_rule_text(matches[0])}"
        preview = self.preview(version, [_to_change(change)], reason)
        return self.write("/thresholds", {"reason": reason, "changes": [change]}, preview, args.yes)

    def revert(self, args) -> int:
        text = args.version[1:] if args.version[:1] in ("v", "V") else args.version
        try:
            target = int(text)
        except ValueError:
            raise Usage(f"VERSION must be a version number like 3 or v3, got {args.version!r}") from None
        changes = [_to_change(c) for c in self.get_ok("/diff", {"version": target})["changes"]]
        if not changes:
            self.say(f"The thresholds already equal v{target}; nothing to revert.")
            return OK
        version, _ = self.current()
        self.say(f"Reverting to v{target} makes these changes, including any made after v{target}:")
        for c in changes:
            self.say(f"  {describe_change(c)}")
        return self.write("/revert", {"version": target},
                          self.preview(version, changes, f"revert to v{target}"), args.yes)

    def hold(self, args) -> int:
        words = args.words
        if words == ["off"]:
            ended = self.post_ok("/hold/off").get("ended")
            self.say("Hold ended." if ended else "There was no hold.")
            return OK
        level = _level(words[0])
        body: Dict = {"level": level.name.lower()}
        if len(words) > 1:
            if words[1] != "until" or len(words) < 3:
                raise Usage("use: hold LEVEL [until HH:MM | until YYYY-MM-DD HH:MM]")
            try:
                until = parse_until(" ".join(words[2:]), datetime.fromtimestamp(self.now, self.tz()))
            except ValueError as e:
                raise Usage(str(e)) from None
            body["until"] = until.timestamp()
        self.post_ok("/hold", body)
        end = "until cancelled" if "until" not in body else f"until {_when(body['until'], self.now, self.tz())}"
        self.say(f"Hold at {level.name.capitalize()}, {end}. fanctl hold off to end it.")
        return OK

    def mute(self, args) -> int:
        words = args.words
        if words == ["off"]:
            self.post_ok("/mute/off")
            self.say("Review watchdog unmuted.")
            return OK
        if len(words) not in (3, 4) or words[:2] != ["reviews", "until"]:
            raise Usage("use: mute reviews until YYYY-MM-DD [HH:MM]; mute off")
        text = " ".join(words[2:])
        try:
            until = datetime.strptime(text, "%Y-%m-%d %H:%M" if len(words) == 4 else "%Y-%m-%d").replace(
                tzinfo=self.tz())
        except ValueError:
            raise Usage(f"expected YYYY-MM-DD [HH:MM], got {text!r}") from None
        self.post_ok("/mute", {"until": until.timestamp()})
        self.say(f"Review watchdog muted until {text}.")
        return OK

    def test_alert(self, args) -> int:
        self.post_ok("/test-alert")
        self.say("Test alert sent.")
        return OK


def main(argv: List[str], client=None, stdin=sys.stdin, stdout=sys.stdout, now: Optional[float] = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except Usage as e:
        print(e, file=stdout)
        return USAGE
    except SystemExit as e:  # --help
        return e.code if isinstance(e.code, int) else OK
    if client is None:
        client = SocketClient(os.environ.get("FANCTL_SOCKET", Config.socket_path))
    cli = _Cli(client, stdin, stdout, time.time() if now is None else now)
    try:
        return getattr(cli, args.cmd.replace("-", "_"))(args)
    except Usage as e:
        print(f"fanctl: {e}", file=stdout)
        return USAGE
    except Rejected as e:
        print(f"Rejected: {e}", file=stdout)
        return REJECTED
    except Unreachable as e:
        print(e, file=stdout)
        return UNREACHABLE


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
