"""The fan commands the review model (and a chat request) runs on hag: read, compare, write, note.

Exit codes: 0 done, 1 error (one line), 2 a write the loop rejected (nothing was changed)."""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # Hermes runs scripts with -I

import fanlib  # noqa: E402

NOTE_MAX_CHARS = 4000


class BadValue(Exception):
    pass


def _value(param: str, text: str):
    """`none`, a rule `INLET:LEVEL` for ambient.*, otherwise a number."""
    if param.startswith("ambient."):
        if text == "none":
            return None
        inlet, sep, level = text.partition(":")
        if not sep or not level:
            raise BadValue(f"{param}: {text!r} isn't a rule like 30:high")
        return {"inlet_min": _number(param, inlet), "level": level.lower()}
    return _number(param, text)


def _number(param: str, text: str):
    try:
        x = float(text)
    except ValueError:
        raise BadValue(f"{param}: {text!r} isn't a number") from None
    return int(x) if x.is_integer() else x


def _changes(args: list) -> list:
    if not args or len(args) % 3:
        raise BadValue(f"changes come as PARAM FROM TO, got {len(args)} values: {' '.join(args)}")
    return [{"param": p, "from": _value(p, f), "to": _value(p, t)}
            for p, f, t in (args[i:i + 3] for i in range(0, len(args), 3))]


def cmd_status(loop, state, a, now):
    print("\n".join(fanlib.now_lines(loop.get("/status"))))


def cmd_episodes(loop, state, a, now):
    eps = loop.get("/episodes", since=now - a.days * 86400)
    print(fanlib._stats_line(f"Last {a.days:g} days", fanlib.version_stats(eps["episodes"]))
          + (" (list truncated)" if eps.get("truncated") else ""))
    print(fanlib.settle_line(fanlib.settle_by_level(eps["episodes"])))


def cmd_compare(loop, state, a, now):
    stats = [fanlib.version_stats(loop.get("/episodes", version=v)["episodes"]) for v in (a.a, a.b)]
    print(fanlib._stats_line(f"v{a.a}", stats[0]))
    print(fanlib._stats_line(f"v{a.b}", stats[1]))
    print(f"v{a.b} against v{a.a}: {fanlib.compare(stats[0], stats[1])}")


def cmd_changes(loop, state, a, now):
    versions = [v for v in loop.get("/changes", since=now - a.days * 86400)["versions"] if v.get("changes")]
    print("\n".join(fanlib._version_line(v) for v in versions) or f"No threshold changes in the last {a.days:g} days.")


def cmd_write(loop, state, a, now):
    changes = _changes(a.changes)
    status, body = loop.post("/thresholds", {"source": a.source, "reason": a.reason, "changes": changes})
    if status == 200:
        print(f"Applied: v{body.get('old_version')} → v{body.get('new_version')}: "
              + ", ".join(fanlib._change(c) for c in changes))
        return 0
    print(f"Rejected ({body.get('error')}): {fanlib._one_line(body.get('detail') or '', 500)}")
    print("Current " + fanlib._thresholds({"thresholds": body.get("thresholds"), "version": body.get("old_version")}))
    print("Nothing was changed. Read the values above and decide again.")
    return 2


def cmd_note(loop, state, a, now):
    try:
        with open(a.file, encoding="utf-8") as f:
            text = f.read(NOTE_MAX_CHARS)
    except (OSError, UnicodeDecodeError) as e:
        raise fanlib.LoopError(f"Can't read the note: {fanlib._one_line(e)}") from None
    saved = state.load()
    mark_id = saved.get("mark_id")
    state.add_note({"at": now, "mark_id": mark_id, "text": text})
    ended = mark_id is not None and loop.post("/marks/end", {"id": mark_id})[1].get("ended") is True
    if not ended:
        state.save({**saved, "mark_id": None})
        print("Note saved; the loop had already closed this review (over 15 min or superseded)")
        return 0
    state.save({**saved, "mark_id": None, "last_review_at": saved.get("last_review_started_at")})
    os.remove(a.file)
    print("Note saved; review end recorded")
    return 0


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fan.py", description="Read the fan loop, write thresholds, save review notes.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="what the fans do now, and any problems").set_defaults(fn=cmd_status)
    for name, fn, text in (("episodes", cmd_episodes, "episode stats and settling over the last N days"),
                           ("changes", cmd_changes, "threshold changes over the last N days")):
        s = sub.add_parser(name, help=text)
        s.add_argument("--days", type=float, default=7)
        s.set_defaults(fn=fn)
    s = sub.add_parser("compare", help="version B against version A")
    s.add_argument("a", type=int)
    s.add_argument("b", type=int)
    s.set_defaults(fn=cmd_compare)
    s = sub.add_parser("write", help="change thresholds: PARAM FROM TO [PARAM FROM TO ...], all or nothing")
    s.add_argument("--source", choices=("review", "user-chat"), required=True)
    s.add_argument("--reason", required=True)
    s.add_argument("changes", nargs="+", metavar="PARAM FROM TO")
    s.set_defaults(fn=cmd_write)
    s = sub.add_parser("note", help="save the review note in FILE and record the review's end")
    s.add_argument("file")
    s.set_defaults(fn=cmd_note)
    return p


def main(argv=None, settings_path: str = fanlib.DEFAULT_SETTINGS, now=None) -> int:
    a = _parser().parse_args(argv)
    now = time.time() if now is None else now
    try:
        if a.cmd == "write":
            _changes(a.changes)  # bad values never reach the loop, nor need the settings
        settings = fanlib.load_settings(settings_path)
        code = a.fn(fanlib.Loop(settings), fanlib.State(settings.state_dir), a, now)
    except BadValue as e:
        print(f"Bad value: {e}")
        return 1
    except (fanlib.LoopError, KeyError, TypeError) as e:
        print(e if isinstance(e, fanlib.LoopError) else f"Unexpected reply from the fan loop: {e!r}")
        return 1
    return code or 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
