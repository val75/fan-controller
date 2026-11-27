"""The daily fan summary (Hermes cron `--no-agent`): its stdout is the Telegram message.

Computed here, not by the model, so the numbers are the loop's own. It sends no mark (no
model run). If the loop can't be reached it prints one line and exits 1: Hermes delivers
that as an error notice, which is worth having."""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # Hermes runs scripts with -I

import fanlib  # noqa: E402


def main(argv=None, settings_path: str = fanlib.DEFAULT_SETTINGS, now=None) -> int:
    argparse.ArgumentParser(description="Print the daily fan summary (last 24 h).").parse_args(argv)
    now = time.time() if now is None else now
    try:
        loop = fanlib.Loop(fanlib.load_settings(settings_path))
        status = loop.get("/status")
        summary = loop.get("/summary", since=now - 86400, until=now)
        changes = loop.get("/changes", since=now - 86400)["versions"]
    except (fanlib.LoopError, KeyError, TypeError) as e:
        print(e if isinstance(e, fanlib.LoopError) else f"Unexpected reply from the fan loop: {e!r}")
        return 1
    print(fanlib.daily_report(status, summary, changes, now))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
