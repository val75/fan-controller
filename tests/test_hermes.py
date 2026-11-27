"""Tests for Hermes's scripts on hag (plan 2). They run against the real fanloop API, in-process."""
import json
import os
import shutil
import sys
import tempfile
import unittest

from fanloop.alerts import Alerter
from fanloop.api import make_servers, serve
from fanloop.holds import Holds
from fanloop.marks import Marks
from fanloop.service import Service
from fanloop.store import Store
from fanloop.thresholds import load_defaults
from fanloop.watchdog import ReviewWatchdog

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "hermes", "scripts")
sys.path.insert(0, SCRIPTS)

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


if __name__ == "__main__":
    unittest.main()
