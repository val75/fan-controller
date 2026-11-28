"""Tests for Hermes's scripts on hag (plan 2). They run against the real fanloop API, in-process."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from fanloop.alerts import Alerter
from fanloop.api import make_servers, serve
from fanloop.holds import Holds
from fanloop.levels import Level
from fanloop.marks import Marks
from fanloop.readings import CardReading, IpmiReading
from fanloop.service import Service
from fanloop.store import Store
from fanloop.thresholds import load_defaults
from fanloop.watchdog import ReviewWatchdog

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "hermes", "scripts")
sys.path.insert(0, SCRIPTS)

import fan  # noqa: E402
import fan_daily  # noqa: E402
import fan_review  # noqa: E402
import fanlib  # noqa: E402

TOKEN = "test-token-0123456789"

# What the live loop publishes (t4, 2026-11-27, idle at Off); the API adds the rest.
SNAPSHOT = {
    "at": 1795793859.56, "level": "off", "reason": "steady", "busy": False, "blind": False,
    "cards": [
        {"index": 0, "temp_c": 41.0, "power_w": 26.44, "util_pct": 0.0, "clock_mhz": 585.0,
         "sw_power_cap": False, "sw_thermal": False, "hw_thermal": False},
        {"index": 1, "temp_c": 41.0, "power_w": 27.58, "util_pct": 0.0, "clock_mhz": 585.0,
         "sw_power_cap": False, "sw_thermal": False, "hw_thermal": False},
    ],
    "inlet_c": 12.0, "exhaust_c": 18.0, "system_power_w": 237.0,
    "fans_rpm": {"Fan1": 7200, "Fan2": 7080, "Fan3": 7200, "Fan4": 7200, "Fan5": 7080, "Fan6": 7200},
    "health": {"nvidia_ok": True, "ipmi_ok": True, "racadm_ok": True, "store_ok": True,
               "last_poll_at": 1795793859.56, "set_failed": False},
}


class FakeSender:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


class LoopFixture(unittest.TestCase):
    """A real fanloop API on 127.0.0.1, plus the settings and token files the scripts read."""

    def setUp(self):
        self.loop_store = Store(":memory:", load_defaults())
        self.addCleanup(self.loop_store.close)
        alerter = Alerter(FakeSender(), self.loop_store)
        alerter.start()
        self.addCleanup(alerter.stop)
        service = Service(self.loop_store, alerter, Marks(self.loop_store), Holds(self.loop_store),
                          ReviewWatchdog(self.loop_store), dry_run=False)
        service.publish(SNAPSHOT)
        self.tmp = tempfile.mkdtemp(prefix="fh")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        tcp, unix = make_servers(service, self.loop_store, "127.0.0.1", 0,
                                 os.path.join(self.tmp, "ctl.sock"), TOKEN, None)
        serve((tcp, unix), poll_interval=0.05)
        for server in (tcp, unix):
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        self.token_path = self.write_file("write-token", TOKEN + "\n")
        self.state_dir = os.path.join(self.tmp, "state")
        self.settings_path = os.path.join(self.tmp, "hermes.json")
        self.point_settings_at(f"http://127.0.0.1:{tcp.server_address[1]}")

    def write_file(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def point_settings_at(self, url):
        with open(self.settings_path, "w") as f:
            json.dump({"loop_url": url, "token_file": self.token_path, "state_dir": self.state_dir,
                       "timeout_s": 5}, f)

    def loop(self):
        return fanlib.Loop(fanlib.load_settings(self.settings_path))


class TestClient(LoopFixture):
    def test_get_status(self):
        loop = fanlib.Loop(fanlib.load_settings(self.settings_path))
        self.assertEqual(loop.get("/status")["timezone"], "EET")

    def test_post_with_token_opens_a_mark(self):
        status, body = self.loop().post("/marks/start", {})
        self.assertEqual(status, 200)
        self.assertEqual(self.loop_store.marks_open()[0]["id"], body["id"])

    def test_rejected_write_returns_409_body(self):
        status, body = self.loop().post("/thresholds", {"source": "review", "reason": "t",
                                                         "changes": [{"param": "step_up.low", "from": 99, "to": 54}]})
        self.assertEqual((status, body["error"]), (409, "stale"))

    def test_unreachable_loop_is_one_line(self):
        s = fanlib.Settings("http://127.0.0.1:9", self.token_path, self.tmp, 2)
        with self.assertRaises(fanlib.LoopError) as e:
            fanlib.Loop(s).get("/status")
        self.assertTrue(str(e.exception).startswith("Can't reach the fan loop:"), str(e.exception))
        self.assertNotIn("\n", str(e.exception))

    def test_missing_token_only_blocks_writes(self):
        os.remove(self.token_path)
        loop = self.loop()
        loop.get("/status")
        with self.assertRaises(fanlib.LoopError) as e:
            loop.post("/marks/start", {})
        self.assertEqual(str(e.exception), f"No write token at {self.token_path}")

    def test_wrong_token_is_an_error_not_a_reply(self):
        with open(self.token_path, "w") as f:
            f.write("wrong")
        with self.assertRaises(fanlib.LoopError) as e:
            self.loop().post("/marks/start", {})
        self.assertEqual(str(e.exception), "The fan loop refused the write token")

    def test_bad_settings_file(self):
        with open(self.settings_path, "w") as f:
            f.write("{not json")
        with self.assertRaises(fanlib.LoopError) as e:
            fanlib.load_settings(self.settings_path)
        self.assertEqual(str(e.exception), f"Settings missing or invalid: {self.settings_path}")


class TestState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fh")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dir = os.path.join(self.tmp, "state")

    def test_round_trip_and_corrupt_file(self):
        state = fanlib.State(self.dir)
        self.assertEqual(state.load(), {})
        state.save({"mark_id": 3, "last_review_at": 12.5})
        self.assertEqual(fanlib.State(self.dir).load(), {"mark_id": 3, "last_review_at": 12.5})
        self.assertEqual(os.stat(self.dir).st_mode & 0o777, 0o700)
        with open(os.path.join(self.dir, "state.json"), "w") as f:
            f.write("garbage")
        self.assertEqual(state.load(), {})

    def test_notes_keep_order_and_skip_bad_lines(self):
        state = fanlib.State(self.dir)
        self.assertEqual(state.notes(3), [])
        for i in range(3):
            state.add_note({"at": i, "text": f"note {i}"})
        with open(os.path.join(self.dir, "notes.jsonl"), "a") as f:
            f.write("not json\n")
        self.assertEqual([n["text"] for n in state.notes(2)], ["note 1", "note 2"])


def ep(start, dur, peak=70, inlet=12, level_s=None, review=False, hold=None, thr=0, changes=2):
    level_s = level_s or {"medium": dur}
    return {"start": start, "end": start + dur, "duration_s": dur, "inlet_min": inlet, "inlet_mean": inlet,
            "inlet_max": inlet, "peak_c": {"0": peak, "1": peak - 4}, "first_slowdown_s": None, "throttled_s": thr,
            "level_s": level_s, "level_changes": changes, "review_overlap": review, "hold_level": hold, "versions": [1]}


NOW = 1795794210.0  # Fri 2026-11-27 17:43 EET
# /status and /summary as the loop answers them (captured from LoopFixture's API).
STATUS = {**SNAPSHOT, "dry_run": False, "timezone": "EET", "version": 1,
          "thresholds": {"low": 55, "medium": 65, "high": 78, "margin": 5, "hold_s": 180, "ambient_rules": []},
          "hold": None, "open_marks": [], "last_review_ok_at": None, "review_watchdog_muted": False}
SUMMARY_EMPTY = {"since": NOW - 86400, "until": NOW, "version": 1, "episodes": 0, "qualifying_on_current": 0,
                 "slowdown_s": 0, "level_s": {}, "peak_c": {}, "max_inlet": None}
CHANGES = [  # /changes "versions"
    {"version": 1, "created_at": 0.0, "source": "defaults", "reason": "starting thresholds (spec 5.3)",
     "thresholds": STATUS["thresholds"], "changes": []},
    {"version": 2, "created_at": NOW - 3600, "source": "review", "reason": "louder test",
     "thresholds": {**STATUS["thresholds"], "hold_s": 190}, "changes": [{"param": "hold_s", "from": 180, "to": 190}]},
]


class TestAnalysis(unittest.TestCase):
    def test_qualifies(self):
        self.assertTrue(fanlib.qualifies(ep(0, 360)))
        self.assertFalse(fanlib.qualifies(ep(0, 359)))
        self.assertFalse(fanlib.qualifies(ep(0, 900, review=True)))
        self.assertFalse(fanlib.qualifies(ep(0, 900, hold="medium")))

    def test_settle_by_level_uses_hotter_card_minus_inlet(self):
        s = fanlib.settle_by_level([ep(0, 900, peak=73, inlet=12), ep(1000, 900, peak=71, inlet=13), ep(3000, 60)])
        self.assertEqual(s["medium"], {"n": 2, "median": 59.5, "min": 58.0, "max": 61.0})

    def test_version_stats_and_compare_order(self):
        a = fanlib.version_stats([ep(0, 3600, thr=0, level_s={"medium": 3600})])
        b = fanlib.version_stats([ep(0, 3600, thr=0, level_s={"medium": 1800, "high": 1800})])
        self.assertEqual(fanlib.compare(a, b), "worse")          # louder, same slowdown
        c = fanlib.version_stats([ep(0, 3600, thr=30)])
        self.assertEqual(fanlib.compare(c, b), "better")         # no slowdown beats quieter
        self.assertEqual(fanlib.compare(a, a), "same")

    def test_compare_needs_qualifying_episodes(self):
        a = fanlib.version_stats([ep(0, 60)])
        self.assertEqual(fanlib.compare(a, fanlib.version_stats([])), "not enough data")

    def test_version_stats_with_no_episodes(self):
        s = fanlib.version_stats([])
        self.assertEqual((s["n"], s["slowdown_per_h"], s["loud_share"], s["inlet"]), (0, 0, 0, None))

    def test_step_response(self):
        samples = [{"ts": t, "temp0": 60 + t / 60, "temp1": 50} for t in range(0, 400, 5)]
        r = fanlib.step_response(samples, 100)
        self.assertEqual((r["temp_at"], r["delta"]), (61.7, 3.0))
        self.assertIsNone(fanlib.step_response([], 100))
        self.assertIsNone(fanlib.step_response([{"ts": 100, "temp0": 60, "temp1": None}], 100))  # no "after"

    def test_fmt_time(self):
        self.assertEqual(fanlib.fmt_time(NOW, "EET"), "Fri 17:43")

    def test_reports_with_no_episodes(self):
        text = fanlib.review_report(STATUS, SUMMARY_EMPTY, [], [], [], [], [], since=NOW - 21600, now=NOW)
        self.assertIn("Since last review (Fri 11:43): no episodes", text)
        self.assertIn("not enough data", text)
        self.assertIn("none yet", text)
        self.assertIn("Problems: none", text)
        self.assertIn("No threshold changes.", fanlib.daily_report(STATUS, SUMMARY_EMPTY, [], NOW))

    def test_review_report_numbers(self):
        eps = [ep(NOW - 5000, 900, peak=73, inlet=12), ep(NOW - 3000, 900, peak=71, inlet=13)]
        summary = {**SUMMARY_EMPTY, "episodes": 2, "qualifying_on_current": 2}
        steps = [{"at": NOW - 4900, "from": "low", "to": "medium", "temp_at": 63.0, "temp_after": 66.5, "delta": 3.5}]
        notes = [{"at": NOW - 21600, "text": "What: no change.\nCheck next time: Medium."}]
        text = fanlib.review_report(STATUS, summary, eps, eps, CHANGES, steps, notes, since=NOW - 21600, now=NOW,
                                    truncated=True)
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("Now: off (steady), cards 41.0/41.0°C, inlet 12.0°C, thresholds v1: "
                                            "Low 55, Medium 65, High 78, margin 5, hold 180 s, ambient none"), lines[0])
        self.assertIn("2 episodes, 30 min busy, slowdown 0 s, peak 73.0°C, inlet 12.0–13.0°C", text)
        self.assertIn("(list truncated)", text)
        self.assertIn("On v1: 2 qualifying episodes (loop's count: 2; a quieter review change needs 3)", text)
        self.assertIn("Medium +59.5°C (n 2, 58.0–61.0)", text)
        self.assertIn("Fri 16:21 low→medium: 63.0→66.5°C over 3 min", text)
        self.assertIn("v1 → v2 review: hold_s 180 → 190 (louder test)", text)
        self.assertIn("Review changes in the last 24 h: 1 of 2; user-chat: 0 of 6", text)
        self.assertIn("Fri 11:43: What: no change. Check next time: Medium.", text)

    def test_problems_and_daily_report(self):
        status = {**STATUS, "health": {**STATUS["health"], "racadm_ok": False, "set_failed": True},
                  "store_error": "OperationalError('disk full')"}
        summary = {**SUMMARY_EMPTY, "episodes": 3, "level_s": {"off": 60, "low": 120, "medium": 600},
                   "peak_c": {"0": 72.5, "1": 68.0}, "max_inlet": 13.0, "slowdown_s": 0}
        text = fanlib.review_report(status, summary, [], [], [], [], [], since=NOW - 21600, now=NOW)
        self.assertIn("Problems: racadm_ok false, set_failed, store_error OperationalError('disk full')", text)
        daily = fanlib.daily_report(status, summary, CHANGES, NOW).splitlines()
        self.assertEqual(daily[0], "Fans, last 24 h: 3 episodes, 13 min busy, time at Low/Medium/High/Max "
                                   "2/10/0/0 min, slowdown 0 s, peak 72.5°C, max inlet 13.0°C.")
        self.assertEqual(daily[1], "Now: off, cards 41.0/41.0°C, inlet 12.0°C, thresholds v1.")
        self.assertEqual(daily[2], "Changed: v1 → v2 review: hold_s 180 → 190 (louder test)")
        self.assertTrue(daily[3].startswith("Problems: racadm_ok false"))


class TestReviewScript(LoopFixture):
    def run_main(self, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = fan_review.main([], settings_path=self.settings_path, **kw)
        return code, out.getvalue()

    def test_mark_starts_before_anything_is_read(self):
        reads = []
        real_get = fanlib.Loop.get

        def spy(loop, path, **q):
            reads.append((path, len(self.loop_store.marks_open())))
            return real_get(loop, path, **q)
        with mock.patch.object(fanlib.Loop, "get", spy):
            code, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertTrue(reads and all(open_marks == 1 for _, open_marks in reads), reads)

    def test_report_and_state(self):
        now = time.time()
        code, out = self.run_main(now=now)
        self.assertEqual(code, 0)
        self.assertIn("thresholds v1", out)
        self.assertIn("Since last review", out)
        state = fanlib.State(self.state_dir).load()
        self.assertEqual(state["mark_id"], self.loop_store.marks_open()[0]["id"])
        self.assertEqual(state["last_review_started_at"], now)
        self.assertNotIn("last_review_at", state)  # only an ended review (fan.py note) moves it

    def test_since_is_the_last_ended_review(self):
        fanlib.State(self.state_dir).save({"last_review_at": NOW - 3600})
        _, out = self.run_main(now=NOW)
        self.assertIn("Since last review (Fri 16:43)", out)

    def test_step_ups_come_from_level_events_and_history(self):
        now = time.time()
        at = now - 600
        for t in range(int(at) - 30, int(at) + 211, 5):
            temp = 63.0 + max(0, t - at) / 60
            self.loop_store.add_sample(t, [CardReading(0, temp, 70, 100, 1100, True, False, False),
                                           CardReading(1, temp - 4, 70, 100, 1100, True, False, False)],
                                       IpmiReading(12, 20, {}, 340), Level.MEDIUM, True)
        self.loop_store.add_event(at, "level", {"from": "low", "to": "medium", "reason": "step-up"})
        self.loop_store.add_event(at + 400, "level", {"from": "medium", "to": "low", "reason": "step-down"})
        _, out = self.run_main(now=now)
        steps = [line for line in out.splitlines() if "→" in line and "over 3 min" in line]
        self.assertEqual(len(steps), 1, out)
        self.assertIn("low→medium: 63.0→66.0°C over 3 min", steps[0])

    def test_unreachable_loop_fails_the_run(self):
        self.point_settings_at("http://127.0.0.1:9")
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(out.strip().count("\n"), 0)
        self.assertTrue(out.startswith("Can't reach the fan loop:"), out)
        self.assertEqual(fanlib.State(self.state_dir).load(), {})

    def test_failed_read_after_mark_keeps_agent_asleep(self):
        with mock.patch.object(fanlib.Loop, "get", side_effect=fanlib.LoopError("Can't reach the fan loop: x")):
            code, out = self.run_main()
        self.assertEqual(code, 1)
        lines = out.strip().splitlines()
        self.assertEqual(lines[0], "Review data incomplete: Can't reach the fan loop: x")
        self.assertEqual(lines[-1], '{"wakeAgent": false}')


def fan_review_main_quietly(test):
    with contextlib.redirect_stdout(io.StringIO()):
        test.assertEqual(fan_review.main([], settings_path=test.settings_path), 0)


class TestFanCli(LoopFixture):
    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = fan.main(list(argv), settings_path=self.settings_path)
        return code, out.getvalue()

    def test_status(self):
        code, out = self.cli("status")
        self.assertEqual(code, 0)
        self.assertIn("thresholds v1", out)
        self.assertIn("Problems: none", out)

    def test_louder_review_write_applies(self):
        code, out = self.cli("write", "--source", "review", "--reason", "test", "hold_s", "180", "190")
        self.assertEqual(code, 0)
        self.assertIn("Applied: v1 → v2: hold_s 180 → 190", out)
        self.assertEqual(self.loop_store.current()[1].hold_s, 190)

    def test_rejected_write_shows_reason_and_current_values(self):
        code, out = self.cli("write", "--source", "review", "--reason", "t", "step_up.low", "50", "54")
        self.assertEqual(code, 2)
        self.assertIn("Rejected (stale)", out)
        self.assertIn("Current thresholds v1", out)
        self.assertIn("Nothing was changed", out)
        code, out = self.cli("write", "--source", "review", "--reason", "t", "step_up.low", "55", "57")
        self.assertIn("Rejected (evidence)", out)       # quieter, no qualifying episodes

    def test_user_chat_write_with_two_changes(self):
        code, out = self.cli("write", "--source", "user-chat", "--reason", "t",
                             "step_up.medium", "65", "60", "step_up.low", "55", "52")
        self.assertEqual(code, 0)
        self.assertEqual((self.loop_store.current()[1].medium, self.loop_store.current()[1].low), (60, 52))
        code, out = self.cli("write", "--source", "review", "--reason", "t", "margin", "5")
        self.assertEqual((code, out.startswith("Bad value")), (1, True))

    def test_ambient_add_and_bad_value(self):
        code, out = self.cli("write", "--source", "user-chat", "--reason", "t", "ambient.add", "none", "30:high")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.loop_store.current()[1].ambient_rules[0].to_dict(), {"inlet_min": 30, "level": "high"})
        code, out = self.cli("write", "--source", "review", "--reason", "t", "margin", "five", "6")
        self.assertEqual((code, out.startswith("Bad value")), (1, True))

    def test_write_without_token_is_one_line_error(self):
        os.remove(self.token_path)
        code, out = self.cli("write", "--source", "review", "--reason", "t", "hold_s", "180", "190")
        self.assertEqual((code, out.strip()), (1, f"No write token at {self.token_path}"))

    def test_note_ends_the_review(self):
        fan_review_main_quietly(self)                      # opens the mark, sets state
        started = fanlib.State(self.state_dir).load()["last_review_started_at"]
        path = self.write_file("note.md", "What: no change. Why: idle. Next: watch Medium.")
        code, out = self.cli("note", path)
        self.assertEqual(code, 0)
        self.assertIn("review end recorded", out)
        self.assertEqual(self.loop_store.marks_open(), [])
        self.assertFalse(os.path.exists(path))
        self.assertEqual(fanlib.State(self.state_dir).notes(1)[0]["text"][:5], "What:")
        self.assertEqual(fanlib.State(self.state_dir).load()["last_review_at"], started)

    def test_note_saved_even_when_mark_already_closed(self):
        fan_review_main_quietly(self)
        self.loop().post("/marks/start", {})               # supersedes the review's mark
        path = self.write_file("note.md", "late")
        code, out = self.cli("note", path)
        self.assertEqual(code, 0)
        self.assertIn("already closed", out)
        self.assertEqual(fanlib.State(self.state_dir).notes(1)[0]["text"], "late")
        self.assertNotIn("last_review_at", fanlib.State(self.state_dir).load())
        self.assertTrue(os.path.exists(path))

    def test_compare_and_changes_run(self):
        self.cli("write", "--source", "user-chat", "--reason", "louder", "hold_s", "180", "190")
        code, out = self.cli("compare", "1", "2")
        self.assertEqual(code, 0)
        self.assertIn("not enough data", out)
        code, out = self.cli("changes")
        self.assertEqual(code, 0)
        self.assertIn("v1 → v2 user-chat: hold_s 180 → 190 (louder)", out)
        code, out = self.cli("episodes", "--days", "3")
        self.assertEqual(code, 0)
        self.assertIn("not enough data", out)


class TestDaily(LoopFixture):
    def run_main(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = fan_daily.main([], settings_path=self.settings_path)
        return code, out.getvalue()

    def test_summary_lines(self):
        self.loop().post("/thresholds", {"source": "review", "reason": "t",
                                          "changes": [{"param": "hold_s", "from": 180, "to": 190}]})
        code, text = self.run_main()
        self.assertEqual(code, 0)
        self.assertTrue(text.startswith("Fans, last 24 h:"))
        self.assertIn("v1 → v2", text)
        self.assertNotIn("Problems", text.split("\n")[1])
        self.assertEqual(self.loop_store.marks_open(), [])  # no mark: not a model run

    def test_unreachable(self):
        self.point_settings_at("http://127.0.0.1:9")
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(out.strip().count("\n"), 0)
        self.assertTrue(out.startswith("Can't reach the fan loop:"), out)


class TestSkillAndScripts(unittest.TestCase):
    def test_skill_names_every_command_by_full_path(self):
        with open(os.path.join(REPO, "hermes/skill/fan-controller/SKILL.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(text.startswith("---\nname: fan-controller\ndescription:"))
        description = text.split("\n")[2]
        self.assertIn('"how hot is it now"', description)  # a bare "how hot" went to the weather (maps skill)
        self.assertIn("not the weather", description)
        for sub in ("status", "episodes", "compare", "changes", "write", "note"):
            self.assertIn(f"python3 /home/hermes/.hermes/scripts/fan.py {sub}", text)
        self.assertNotRegex(text, r"python3? -c|<<")
        self.assertLessEqual(text.count("\n"), 130)
        with open(os.path.join(REPO, "hermes/prompts/review.md"), encoding="utf-8") as f:
            self.assertIn("fan-controller skill", f.read())

    def test_scripts_run_isolated(self):
        # Hermes runs .py scripts with -I: the script's own folder isn't on sys.path.
        for script in ("fan.py", "fan_review.py", "fan_daily.py"):
            r = subprocess.run([sys.executable, "-I", os.path.join(SCRIPTS, script), "--help"],
                               capture_output=True, text=True, timeout=20)
            self.assertNotIn("ModuleNotFoundError", r.stderr, script)
            self.assertEqual(r.returncode, 0, script + r.stderr)


if __name__ == "__main__":
    unittest.main()
