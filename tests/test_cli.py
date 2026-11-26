import io
import os
import tempfile
import time
import unittest
from datetime import datetime

from fanloop import cli
from fanloop.clock import zone
from fanloop.holds import parse_until

EET = zone("EET")
NOW = datetime(2026, 11, 24, 10, 0, tzinfo=EET).timestamp()

THRESHOLDS = {"low": 55, "medium": 65, "high": 78, "margin": 5, "hold_s": 180,
              "ambient_rules": [{"inlet_min": 28, "level": "medium"}]}


class FakeClient:
    """Scripted responses per (method, path); records every call."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = {("GET", "/status"): (200, {"version": 1, "thresholds": THRESHOLDS, "timezone": "EET"})}
        self.responses.update(responses or {})

    def get(self, path, params=None):
        self.calls.append(("GET", path, params))
        return self.responses.get(("GET", path), (404, {"error": "not-found", "detail": ""}))

    def post(self, path, body=None):
        self.calls.append(("POST", path, body))
        return self.responses.get(("POST", path), (200, {"ok": True}))

    def posts(self):
        return [(p, b) for m, p, b in self.calls if m == "POST"]


def run(argv, client, answer=""):
    out = io.StringIO()
    code = cli.main(argv, client=client, stdin=io.StringIO(answer), stdout=out, now=NOW)
    return code, out.getvalue()


WRITE_OK = (200, {"ok": True, "error": None, "detail": "", "old_version": 1, "new_version": 2,
                  "thresholds": THRESHOLDS})


class SetTest(unittest.TestCase):
    def test_set_confirmed_posts_the_change_with_from_taken_from_show(self):
        client = FakeClient({("POST", "/thresholds"): WRITE_OK})
        code, out = run(["set", "medium", "63", "--reason", "x"], client, "y\n")
        self.assertEqual(code, 0)
        self.assertEqual(client.posts(), [("/thresholds", {
            "reason": "x", "changes": [{"param": "step_up.medium", "from": 65, "to": 63}]})])
        self.assertIn("Medium 65 → 63°C", out)
        self.assertIn("Apply? [y/N]", out)

    def test_set_declined_posts_nothing(self):
        client = FakeClient()
        code, out = run(["set", "medium", "63", "--reason", "x"], client, "n\n")
        self.assertEqual(code, 0)
        self.assertEqual(client.posts(), [])

    def test_set_with_empty_answer_posts_nothing(self):
        client = FakeClient()
        code, _ = run(["set", "medium", "63", "--reason", "x"], client, "")
        self.assertEqual(code, 0)
        self.assertEqual(client.posts(), [])

    def test_set_yes_skips_the_question(self):
        client = FakeClient({("POST", "/thresholds"): WRITE_OK})
        code, out = run(["set", "medium", "63", "--reason", "x", "--yes"], client)
        self.assertEqual(code, 0)
        self.assertEqual(len(client.posts()), 1)
        self.assertNotIn("Apply?", out)

    def test_set_several_params(self):
        client = FakeClient({("POST", "/thresholds"): WRITE_OK})
        run(["set", "low", "50", "hold", "240", "margin", "6", "--reason", "x", "--yes"], client)
        self.assertEqual(client.posts()[0][1]["changes"], [
            {"param": "step_up.low", "from": 55, "to": 50},
            {"param": "hold_s", "from": 180, "to": 240},
            {"param": "margin", "from": 5, "to": 6}])

    def test_set_stale_prints_the_message_and_exits_1(self):
        client = FakeClient({("POST", "/thresholds"): (409, {"ok": False, "error": "stale", "detail": "d"})})
        code, out = run(["set", "medium", "63", "--reason", "x", "--yes"], client)
        self.assertEqual(code, 1)
        self.assertIn("Thresholds changed meanwhile; run the command again.", out)

    def test_set_rejected_exits_1_with_the_reason(self):
        client = FakeClient({("POST", "/thresholds"): (409, {"ok": False, "error": "invalid",
                                                              "detail": "medium must be at least 3°C above low"})})
        code, out = run(["set", "medium", "56", "--reason", "x", "--yes"], client)
        self.assertEqual(code, 1)
        self.assertIn("medium must be at least 3°C above low", out)

    def test_set_unknown_param_is_a_usage_error(self):
        client = FakeClient()
        code, _ = run(["set", "fast", "63", "--reason", "x"], client)
        self.assertEqual(code, 2)
        self.assertEqual(client.posts(), [])

    def test_set_without_reason_is_a_usage_error(self):
        code, _ = run(["set", "medium", "63"], FakeClient())
        self.assertEqual(code, 2)

    def test_set_odd_number_of_words_is_a_usage_error(self):
        code, _ = run(["set", "medium", "63", "low", "--reason", "x"], FakeClient())
        self.assertEqual(code, 2)

    def test_set_non_number_is_a_usage_error(self):
        code, _ = run(["set", "medium", "hot", "--reason", "x"], FakeClient())
        self.assertEqual(code, 2)


class AmbientTest(unittest.TestCase):
    def test_ambient_add(self):
        client = FakeClient({("POST", "/thresholds"): WRITE_OK})
        code, out = run(["ambient", "add", "30", "high", "--yes"], client)
        self.assertEqual(code, 0)
        body = client.posts()[0][1]
        self.assertEqual(body["changes"], [{"param": "ambient.add", "from": None,
                                            "to": {"inlet_min": 30, "level": "high"}}])
        self.assertTrue(body["reason"])

    def test_ambient_remove_takes_the_rule_from_show(self):
        client = FakeClient({("POST", "/thresholds"): WRITE_OK})
        code, _ = run(["ambient", "remove", "28", "--reason", "too loud", "--yes"], client)
        self.assertEqual(code, 0)
        self.assertEqual(client.posts()[0][1], {"reason": "too loud", "changes": [
            {"param": "ambient.remove", "from": {"inlet_min": 28, "level": "medium"}, "to": None}]})

    def test_ambient_remove_unknown_inlet_exits_1(self):
        client = FakeClient()
        code, out = run(["ambient", "remove", "31", "--yes"], client)
        self.assertEqual(code, 1)
        self.assertEqual(client.posts(), [])

    def test_ambient_bad_level_is_a_usage_error(self):
        code, _ = run(["ambient", "add", "30", "loud", "--yes"], FakeClient())
        self.assertEqual(code, 2)


class RevertTest(unittest.TestCase):
    DIFF = (200, {"changes": [
        {"param": "step_up.medium", "from": 63, "to": 65},
        {"param": "hold_s", "from": 240, "to": 180},
        {"param": "ambient.add", "from": None, "to": {"inlet_min": 30, "level": "high"}}]})

    def test_revert_prints_every_change_from_the_diff(self):
        client = FakeClient({("GET", "/diff"): self.DIFF, ("POST", "/revert"): WRITE_OK})
        code, out = run(["revert", "1"], client, "y\n")
        self.assertEqual(code, 0)
        self.assertIn(("GET", "/diff", {"version": 1}), client.calls)
        for text in ("Medium 63 → 65°C", "hold 240 → 180 s", "ambient + inlet ≥30°C → High"):
            self.assertIn(text, out)
        self.assertEqual(client.posts(), [("/revert", {"version": 1})])

    def test_revert_accepts_a_v_prefix(self):
        client = FakeClient({("GET", "/diff"): self.DIFF, ("POST", "/revert"): WRITE_OK})
        code, _ = run(["revert", "v1", "--yes"], client)
        self.assertEqual(code, 0)
        self.assertEqual(client.posts(), [("/revert", {"version": 1})])

    def test_revert_declined_posts_nothing(self):
        client = FakeClient({("GET", "/diff"): self.DIFF})
        code, _ = run(["revert", "1"], client, "n\n")
        self.assertEqual(code, 0)
        self.assertEqual(client.posts(), [])

    def test_revert_to_an_unknown_version_exits_1(self):
        client = FakeClient({("GET", "/diff"): (404, {"error": "unknown-version", "detail": "there is no v9"})})
        code, out = run(["revert", "9", "--yes"], client)
        self.assertEqual(code, 1)
        self.assertIn("there is no v9", out)
        self.assertEqual(client.posts(), [])

    def test_revert_with_nothing_to_undo_posts_nothing(self):
        client = FakeClient({("GET", "/diff"): (200, {"changes": []})})
        code, out = run(["revert", "1", "--yes"], client)
        self.assertEqual(code, 0)
        self.assertEqual(client.posts(), [])


class HoldMuteTest(unittest.TestCase):
    def test_hold_until_a_clock_time(self):
        client = FakeClient({("POST", "/hold"): (200, {"hold": {}})})
        code, _ = run(["hold", "high", "until", "18:00"], client)
        self.assertEqual(code, 0)
        until = parse_until("18:00", datetime.fromtimestamp(NOW, EET)).timestamp()
        self.assertEqual(client.posts(), [("/hold", {"level": "high", "until": until})])

    def test_hold_until_a_date_and_time(self):
        client = FakeClient({("POST", "/hold"): (200, {"hold": {}})})
        run(["hold", "medium", "until", "2026-11-25", "09:30"], client)
        self.assertEqual(client.posts(), [("/hold", {"level": "medium",
                                                     "until": datetime(2026, 11, 25, 9, 30, tzinfo=EET).timestamp()})])

    def test_hold_until_cancelled(self):
        client = FakeClient({("POST", "/hold"): (200, {"hold": {}})})
        run(["hold", "medium"], client)
        self.assertEqual(client.posts(), [("/hold", {"level": "medium"})])

    def test_hold_off(self):
        client = FakeClient({("POST", "/hold/off"): (200, {"ended": True})})
        code, _ = run(["hold", "off"], client)
        self.assertEqual(code, 0)
        self.assertEqual(client.posts(), [("/hold/off", None)])

    def test_hold_rejected_by_the_loop_exits_1(self):
        client = FakeClient({("POST", "/hold"): (400, {"error": "bad-request", "detail": "too far ahead"})})
        code, out = run(["hold", "high", "until", "2026-11-27", "09:00"], client)
        self.assertEqual(code, 1)
        self.assertIn("too far ahead", out)

    def test_hold_bad_time_is_a_usage_error(self):
        client = FakeClient()
        code, _ = run(["hold", "high", "until", "soon"], client)
        self.assertEqual(code, 2)
        self.assertEqual(client.posts(), [])

    def test_mute_reviews_until_a_date(self):
        client = FakeClient({("POST", "/mute"): (200, {})})
        code, _ = run(["mute", "reviews", "until", "2026-12-01"], client)
        self.assertEqual(code, 0)
        self.assertEqual(client.posts(), [("/mute", {"until": datetime(2026, 12, 1, tzinfo=EET).timestamp()})])

    def test_mute_off(self):
        client = FakeClient({("POST", "/mute/off"): (200, {})})
        run(["mute", "off"], client)
        self.assertEqual(client.posts(), [("/mute/off", None)])

    def test_test_alert(self):
        client = FakeClient({("POST", "/test-alert"): (200, {"sent": True})})
        code, _ = run(["test-alert"], client)
        self.assertEqual(code, 0)
        self.assertEqual(client.posts(), [("/test-alert", None)])


class ReadingTest(unittest.TestCase):
    def test_show_prints_version_and_thresholds(self):
        code, out = run(["show"], FakeClient())
        self.assertEqual(code, 0)
        for text in ("v1", "55", "65", "78", "180", "inlet ≥28°C → Medium"):
            self.assertIn(text, out)

    def test_status_prints_level_and_cards(self):
        status = {"version": 1, "thresholds": THRESHOLDS, "level": "medium", "reason": "step-up",
                  "busy": True, "blind": False, "dry_run": False, "hold": None,
                  "cards": [{"index": 0, "temp_c": 61.0, "power_w": 40.0, "util_pct": 80},
                            {"index": 1, "temp_c": 66.0, "power_w": 42.0, "util_pct": 85}],
                  "inlet_c": 18.0, "health": {"nvidia_ok": True}}
        code, out = run(["status"], FakeClient({("GET", "/status"): (200, status)}))
        self.assertEqual(code, 0)
        self.assertIn("Medium", out)
        self.assertIn("66", out)

    def test_history_lists_versions_with_their_changes(self):
        versions = [{"version": 2, "created_at": NOW - 3600, "source": "user-cli", "reason": "quieter",
                     "thresholds": THRESHOLDS,
                     "changes": [{"param": "step_up.medium", "from": 63, "to": 65}]}]
        client = FakeClient({("GET", "/changes"): (200, {"versions": versions})})
        code, out = run(["history", "--days", "7"], client)
        self.assertEqual(code, 0)
        self.assertIn(("GET", "/changes", {"since": NOW - 7 * 86400}), client.calls)
        self.assertIn("v2", out)
        self.assertIn("Medium 63 → 65°C", out)
        self.assertIn("quieter", out)


class ErrorsTest(unittest.TestCase):
    def test_missing_socket_exits_3(self):
        with tempfile.TemporaryDirectory() as d:
            client = cli.SocketClient(os.path.join(d, "missing.sock"))
            code, out = run(["status"], client)
        self.assertEqual(code, 3)
        self.assertIn("loop", out)

    def test_unknown_command_exits_2(self):
        code, _ = run(["fly"], FakeClient())
        self.assertEqual(code, 2)

    def test_no_command_exits_2(self):
        code, _ = run([], FakeClient())
        self.assertEqual(code, 2)


class SocketClientTest(unittest.TestCase):
    """The real client against the real API server on a temporary socket."""

    def test_get_and_post_over_a_real_socket(self):
        import threading
        from fanloop.api import Handler, UnixHTTPServer

        class FakeService:
            def status(self, now):
                return {"version": 7}

            def test_alert(self, now):
                self.alerted = True

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ctl.sock")
            server = UnixHTTPServer(path, Handler)
            server.service, server.store, server.token = FakeService(), None, ""
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
            thread.start()
            try:
                client = cli.SocketClient(path)
                self.assertEqual(client.get("/status"), (200, {"version": 7}))
                self.assertEqual(client.post("/test-alert"), (200, {"sent": True}))
                self.assertEqual(client.get("/diff")[0], 400)  # version is required
                self.assertEqual(client.get("/nowhere")[0], 404)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


class EndToEndTest(unittest.TestCase):
    """fanctl against the real Service and Store behind the real socket server."""

    def setUp(self):
        import shutil
        from fanloop.alerts import Alerter
        from fanloop.api import make_servers, serve
        from fanloop.holds import Holds
        from fanloop.marks import Marks
        from fanloop.service import Service
        from fanloop.store import Store
        from fanloop.thresholds import load_defaults
        from fanloop.watchdog import ReviewWatchdog

        class Sender:
            sent = []

            def send(self, text):
                self.sent.append(text)

        self.store = Store(":memory:", load_defaults())
        alerter = Alerter(Sender(), self.store)
        alerter.start()
        self.addCleanup(alerter.stop)
        service = Service(self.store, alerter, Marks(self.store), Holds(self.store),
                          ReviewWatchdog(self.store), dry_run=True)
        tmp = tempfile.mkdtemp(prefix="fl")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "ctl.sock")
        tcp, unix = make_servers(service, self.store, "127.0.0.1", 0, path, "token-0123456789", None)
        serve((tcp, unix), poll_interval=0.05)
        for server in (tcp, unix):
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        self.client = cli.SocketClient(path)

    def fanctl(self, *argv):
        out = io.StringIO()
        code = cli.main(list(argv), client=self.client, stdin=io.StringIO(""), stdout=out)
        return code, out.getvalue()

    def test_set_show_history_revert(self):
        self.assertEqual(self.fanctl("set", "medium", "63", "hold", "240", "--reason", "quieter", "--yes")[0], 0)
        self.assertEqual(self.store.current()[1].medium, 63)
        code, out = self.fanctl("show")
        self.assertIn("v2", out)
        self.assertIn("63°C", out)
        code, out = self.fanctl("history")
        self.assertIn("Medium 65 → 63°C", out)
        self.assertIn("hold 180 → 240 s", out)
        code, out = self.fanctl("revert", "v1", "--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("Medium 63 → 65°C", out)
        version, t = self.store.current()
        self.assertEqual((version, t.medium, t.hold_s), (3, 65, 180))

    def test_set_rejected_by_validation(self):
        code, out = self.fanctl("set", "medium", "56", "--reason", "x", "--yes")
        self.assertEqual(code, 1, out)
        self.assertEqual(self.store.current()[0], 1)

    def test_ambient_add_and_remove(self):
        self.assertEqual(self.fanctl("ambient", "add", "30", "high", "--yes")[0], 0)
        self.assertEqual(len(self.store.current()[1].ambient_rules), 1)
        code, out = self.fanctl("ambient", "remove", "30", "--yes")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.store.current()[1].ambient_rules, ())

    def test_hold_status_and_off(self):
        self.assertEqual(self.fanctl("hold", "high")[0], 0)
        code, out = self.fanctl("status")
        self.assertEqual(code, 0, out)
        self.assertIn("Hold at High, until cancelled", out)
        code, out = self.fanctl("hold", "off")
        self.assertIn("Hold ended.", out)

    def test_mute_and_test_alert(self):
        future = datetime.fromtimestamp(time.time() + 3 * 86400).strftime("%Y-%m-%d")
        self.assertEqual(self.fanctl("mute", "reviews", "until", future)[0], 0)
        self.assertEqual(self.fanctl("mute", "off")[0], 0)
        self.assertEqual(self.fanctl("test-alert")[0], 0)


if __name__ == "__main__":
    unittest.main()
