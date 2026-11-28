"""Pre-run script of the scheduled fan review (Hermes cron `--script`, spec 6.2 step 1).

It runs before the model, outside it: it sends the review's start mark first (spec 5.7),
then prints the review data, which Hermes puts into the prompt. Only `fan.py note` (the
review's last step) moves `last_review_at` on, so a review that fails isn't skipped over."""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # Hermes runs scripts with -I

import fanlib  # noqa: E402

MAX_STEPS = 5


def _step_ups(loop: fanlib.Loop, events: list) -> list:
    """The newest step-ups, each with the hotter card at the step and 3 min later."""
    order = fanlib.LEVELS
    ups = [e for e in events
           if (e.get("detail") or {}).get("from") in order and (e.get("detail") or {}).get("to") in order
           and order.index(e["detail"]["to"]) > order.index(e["detail"]["from"])][-MAX_STEPS:]
    steps = []
    for e in ups:
        samples = loop.get("/history", start=e["ts"] - 30, end=e["ts"] + 210)["samples"]
        r = fanlib.step_response(samples, e["ts"])
        if r is not None:
            steps.append({"at": e["ts"], "from": e["detail"]["from"], "to": e["detail"]["to"], **r})
    return steps


SLEEP = json.dumps({"wakeAgent": False})


def main(argv=None, settings_path: str = fanlib.DEFAULT_SETTINGS, now=None) -> int:
    """Always exits 0: Hermes obeys the wakeAgent gate only after exit 0, and wakes the model on any
    other exit, which would then end the mark with a note and count a failed review as a good one.
    A failure keeps the agent asleep; the loop's review watchdog reports missed reviews."""
    argparse.ArgumentParser(description="Start a fan review: send the start mark, print the review data.") \
        .parse_args(argv)
    now = time.time() if now is None else now
    try:
        settings = fanlib.load_settings(settings_path)
        loop, state = fanlib.Loop(settings), fanlib.State(settings.state_dir)
        _, mark = loop.post("/marks/start", {})  # first, before any read
    except fanlib.LoopError as e:
        print(e)
        print(SLEEP)
        return 0
    try:
        saved = state.load()
        state.save({**saved, "mark_id": mark.get("id"), "last_review_started_at": now})
        since = saved.get("last_review_at") or now - 86400
        status = loop.get("/status")
        summary = loop.get("/summary", since=since, until=now)
        eps_since = loop.get("/episodes", since=since)
        eps_current = loop.get("/episodes", version=status.get("version"))
        changes = loop.get("/changes", since=min(since, now - 86400))["versions"]
        events = loop.get("/events", since=since, kinds="level")["events"]
        report = fanlib.review_report(
            status, summary, eps_since["episodes"], eps_current["episodes"], changes, _step_ups(loop, events),
            state.notes(3), since=since, now=now,
            truncated=bool(eps_since.get("truncated") or eps_current.get("truncated")))
    except Exception as e:  # anything: the mark is open and the model must not run on half the data
        detail = e if isinstance(e, fanlib.LoopError) else f"{type(e).__name__}: {fanlib._one_line(e)}"
        print(f"Review data incomplete: {detail}")
        print(SLEEP)  # the loop times the mark out: a failed review
        return 0
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
