import http.client
import json
import os
import shutil
import stat
import tempfile
import time
import unittest

from fanloop.alerts import PREFIX, Alerter
from fanloop.api import UnixHTTPConnection, make_servers, serve
from fanloop.episodes import Episode
from fanloop.holds import Holds
from fanloop.levels import Level
from fanloop.marks import Marks
from fanloop.readings import CardReading, IpmiReading
from fanloop.service import Service
from fanloop.store import Store
from fanloop.thresholds import load_defaults
from fanloop.watchdog import ReviewWatchdog

TOKEN = "test-token-0123456789"


class FakeSender:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


class ApiTest(unittest.TestCase):
    service_class = Service

    def setUp(self):
        self.store = Store(":memory:", load_defaults())
        self.sender = FakeSender()
        self.alerter = Alerter(self.sender, self.store)
        self.alerter.start()
        self.addCleanup(self.alerter.stop)
        self.service = self.service_class(self.store, self.alerter, Marks(self.store), Holds(self.store),
                                          ReviewWatchdog(self.store), dry_run=True)
        self.service.publish({"level": "off", "busy": False})
        self.tmp = tempfile.mkdtemp(prefix="fl")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sock_path = os.path.join(self.tmp, "ctl.sock")
        self.tcp, self.unix = make_servers(self.service, self.store, "127.0.0.1", 0, self.sock_path, TOKEN, None)
        serve((self.tcp, self.unix), poll_interval=0.05)
        for server in (self.tcp, self.unix):
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

    def request(self, method, path, body=None, token=None, local=False, raw=None):
        if local:
            conn = UnixHTTPConnection(self.sock_path, timeout=5)
        else:
            conn = http.client.HTTPConnection("127.0.0.1", self.tcp.server_address[1], timeout=5)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            payload = resp.read()
            return resp.status, json.loads(payload) if payload else None
        finally:
            conn.close()

    def write(self, changes, source="review", reason="test", token=TOKEN):
        return self.request("POST", "/thresholds", {"source": source, "reason": reason, "changes": changes}, token=token)

    def sent(self):
        self.assertTrue(self.alerter.drain(5))
        return list(self.sender.sent)


LOW_54 = [{"param": "step_up.low", "from": 55, "to": 54}]


class TestReads(ApiTest):
    def test_status(self):
        status, body = self.request("GET", "/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], 1)
        self.assertEqual(body["thresholds"]["high"], 78)
        self.assertEqual(body["level"], "off")
        self.assertTrue(body["dry_run"])
        self.assertIsNone(body["hold"])

    def test_status_over_socket(self):
        status, body = self.request("GET", "/status", local=True)
        self.assertEqual((status, body["version"]), (200, 1))

    def test_unknown_route(self):
        self.assertEqual(self.request("GET", "/nope")[0], 404)

    def test_bad_query(self):
        self.assertEqual(self.request("GET", "/history?start=yesterday")[0], 400)

    def test_history_is_downsampled(self):
        now = time.time()
        ts = now - 30 * 86400
        card = [CardReading(0, 50, 30, 0, 585, False, False, False)]
        while ts < now:
            self.store.add_sample(ts, card, IpmiReading(14, 20, {}, 238), Level.OFF, False)
            ts += 120
        status, body = self.request("GET", f"/history?start={now - 30 * 86400}&end={now}")
        self.assertEqual(status, 200)
        self.assertLessEqual(len(body["samples"]), 300)
        self.assertGreater(len(body["samples"]), 250)

    def test_episodes_changes_events_summary(self):
        now = time.time()
        self.store.add_episode(Episode(now - 1000, now - 500, 500, 14, 14, 14, {0: 74, 1: 70}, None, 0,
                                       {"medium": 500}, 1, False, None, [1]))
        self.write(LOW_54)
        _, episodes = self.request("GET", "/episodes?since=0")
        self.assertEqual(episodes["episodes"][0]["peak_c"], {"0": 74, "1": 70})
        _, changes = self.request("GET", "/changes?since_version=1")
        self.assertEqual(changes["versions"][0]["version"], 2)
        _, events = self.request("GET", "/events?since=0&kinds=threshold-change")
        self.assertEqual(len(events["events"]), 1)
        status, summary = self.request("GET", f"/summary?since={now - 86400}&until={now + 10}")
        self.assertEqual(status, 200)
        self.assertEqual(summary["episodes"], 1)
        self.assertEqual(summary["level_s"], {"medium": 500})
        self.assertEqual(summary["peak_c"], {"0": 74, "1": 70})
        self.assertEqual(summary["qualifying_on_current"], 0)  # the episode is on v1, current is v2


class TestTcpWrites(ApiTest):
    def test_token_required(self):
        self.assertEqual(self.write(LOW_54, token=None)[0], 401)
        self.assertEqual(self.write(LOW_54, token="wrong")[0], 401)
        self.assertEqual(self.store.current()[0], 1)

    def test_review_write_and_announcement(self):
        status, body = self.write(LOW_54)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["new_version"], 2)
        self.assertEqual(self.store.current()[1].low, 54)
        sent = self.sent()
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0].startswith(PREFIX + "thresholds v1 → v2: Low 55 → 54°C"), sent[0])
        self.assertIn("(source review: test)", sent[0])

    def test_stale_write(self):
        status, body = self.write([{"param": "step_up.low", "from": 50, "to": 54}])
        self.assertEqual((status, body["error"]), (409, "stale"))

    def test_user_chat_announcement_offers_revert(self):
        self.write([{"param": "margin", "from": 5, "to": 6}], source="user-chat", reason="user asked")
        (text,) = self.sent()
        self.assertIn("margin 5 → 6°C", text)
        self.assertIn("Hermes says you asked for this. If you didn't: fanctl revert v1", text)

    def test_ambient_write(self):
        status, body = self.write([{"param": "ambient.add", "from": None, "to": {"inlet_min": 28, "level": "high"}}])
        self.assertEqual(status, 200, body)
        self.assertIn("ambient + inlet ≥28°C → High", self.sent()[0])

    def test_marks(self):
        status, body = self.request("POST", "/marks/start", token=TOKEN)
        self.assertEqual(status, 200)
        mark_id = body["id"]
        self.assertEqual(self.request("POST", "/marks/end", {"id": mark_id}, token=TOKEN), (200, {"ended": True}))
        self.assertEqual(self.request("POST", "/marks/end", {"id": mark_id}, token=TOKEN), (200, {"ended": False}))
        self.assertEqual(self.request("POST", "/marks/start")[0], 401)


class TestHostileInput(ApiTest):
    def test_rejected_and_nothing_changes(self):
        cases = [
            (400, dict(raw=b"{")),
            (400, dict(raw=b"x" * 20000)),
            (400, dict(body={"source": "review", "reason": "x", "changes": LOW_54, "extra": 1})),
            (400, dict(body={"source": "review", "reason": "x", "changes": [{"param": "step_up.low", "from": "55", "to": 54}]})),
            (400, dict(body={"source": "review", "reason": "x", "changes": [{"param": "step_up.low", "from": True, "to": 54}]})),
            (400, dict(body={"source": "review", "reason": "x", "changes": [{"param": "step_up.low", "from": 55}]})),
            (400, dict(body={"source": "user-cli", "reason": "x", "changes": LOW_54})),
            (400, dict(body={"source": "review", "reason": 5, "changes": LOW_54})),
            (400, dict(body=["not", "an", "object"])),
            (400, dict(body={"source": "review", "reason": "x", "changes": [
                {"param": "ambient.add", "from": None, "to": {"inlet_min": 28, "level": "loud"}}]})),
        ]
        for expected, kw in cases:
            with self.subTest(kw=str(kw)[:80]):
                status, body = self.request("POST", "/thresholds", token=TOKEN, **kw)
                self.assertEqual(status, expected, body)
        self.assertEqual(self.request("POST", "/hold", {"level": "medium"}, token=TOKEN)[0], 404)
        self.assertEqual(self.request("POST", "/revert", {"version": 1}, token=TOKEN)[0], 404)
        self.assertEqual(self.request("GET", "/diff?version=1")[0], 404)
        self.assertEqual(self.request("GET", "/status")[0], 200)
        self.assertEqual(self.store.current()[0], 1)

    def test_unknown_param_is_a_policy_rejection(self):
        status, body = self.write([{"param": "emergency", "from": 80, "to": 85}])
        self.assertEqual((status, body["error"]), (409, "unknown-param"))


class TestSocket(ApiTest):
    def test_write_is_user_cli(self):
        status, body = self.request("POST", "/thresholds", {"reason": "cli", "changes": LOW_54}, local=True)
        self.assertEqual(status, 200, body)
        self.assertEqual(self.store.versions(since_version=1)[0]["source"], "user-cli")

    def test_source_not_accepted_over_socket(self):
        status, _ = self.request("POST", "/thresholds", {"source": "review", "reason": "x", "changes": LOW_54}, local=True)
        self.assertEqual(status, 400)

    def test_hold(self):
        status, body = self.request("POST", "/hold", {"level": "medium"}, local=True)
        self.assertEqual(status, 200, body)
        self.assertEqual(self.request("GET", "/status")[1]["hold"]["level"], "medium")
        self.assertEqual(self.request("POST", "/hold/off", local=True)[0], 200)
        self.assertIsNone(self.request("GET", "/status")[1]["hold"])
        sent = self.sent()
        self.assertIn(PREFIX + "Hold at Medium, until cancelled", sent)
        self.assertIn(PREFIX + "Hold ended", sent)

    def test_timed_hold_too_far(self):
        status, body = self.request("POST", "/hold", {"level": "medium", "until": time.time() + 3 * 86400}, local=True)
        self.assertEqual(status, 400, body)

    def test_diff_and_revert(self):
        self.request("POST", "/thresholds", {"reason": "a", "changes": LOW_54}, local=True)
        self.request("POST", "/thresholds", {"reason": "b", "changes": [{"param": "margin", "from": 5, "to": 6}]}, local=True)
        status, body = self.request("GET", "/diff?version=1", local=True)
        self.assertEqual(status, 200)
        self.assertEqual(sorted(c["param"] for c in body["changes"]), ["margin", "step_up.low"])
        status, body = self.request("POST", "/revert", {"version": 1}, local=True)
        self.assertEqual(status, 200, body)
        self.assertEqual(self.store.current(), (4, load_defaults()))
        self.assertEqual(self.store.versions(since_version=3)[0]["reason"], "revert to v1")
        self.assertEqual(self.request("POST", "/revert", {"version": 1}, local=True)[1]["error"], "no-op")
        self.assertEqual(self.request("POST", "/revert", {"version": 99}, local=True)[1]["error"], "unknown-version")

    def test_mute_and_test_alert(self):
        self.assertEqual(self.request("POST", "/mute", {"until": time.time() + 86400}, local=True)[0], 200)
        self.assertEqual(self.request("POST", "/mute/off", local=True)[0], 200)
        self.assertEqual(self.request("POST", "/test-alert", local=True)[0], 200)
        sent = self.sent()
        self.assertTrue(any("Review watchdog muted until" in s for s in sent))
        self.assertTrue(any("Test alert" in s for s in sent))

    def test_socket_file_mode(self):
        self.assertEqual(stat.S_IMODE(os.stat(self.sock_path).st_mode), 0o660)


class BrokenStatus(Service):
    def status(self, now=None):
        raise RuntimeError("boom")


class TestResilience(ApiTest):
    service_class = BrokenStatus

    def test_handler_exception_is_500_and_server_keeps_going(self):
        status, body = self.request("GET", "/status")
        self.assertEqual(status, 500)
        self.assertEqual(self.request("GET", "/episodes")[0], 200)
        self.assertTrue(self.store.events(0, kinds=["api-error"]))


if __name__ == "__main__":
    unittest.main()
