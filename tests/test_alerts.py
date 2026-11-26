import http.server
import json
import threading
import time
import unittest
import urllib.parse

from fanloop.alerts import PREFIX, Alerter, TelegramSender
from fanloop.clock import local, zone
from fanloop.store import Store
from fanloop.thresholds import load_defaults

T0 = 1_800_000_000.0


class FakeSender:
    def __init__(self):
        self.sent = []
        self.failing = False
        self.lock = threading.Lock()

    def send(self, text):
        with self.lock:
            if self.failing:
                raise OSError("network is unreachable")
            self.sent.append(text)


class AlerterTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:", load_defaults())
        self.sender = FakeSender()
        self.alerter = self.make()

    def make(self, **kw):
        alerter = Alerter(self.sender, self.store, **kw)
        alerter.start()
        self.addCleanup(alerter.stop)
        return alerter

    def sent(self, alerter=None):
        self.assertTrue((alerter or self.alerter).drain(5))
        return list(self.sender.sent)


class TestRateLimit(AlerterTest):
    def test_a_clock_that_jumps_back_does_not_silence_alerts(self):
        self.assertTrue(self.alerter.alert("emergency", "80°C", T0))
        self.assertTrue(self.alerter.alert("emergency", "after the clock went back", T0 - 3600))
        self.assertFalse(self.alerter.alert("emergency", "within the limit again", T0 - 3500))

    def test_same_kind_within_window(self):
        self.assertTrue(self.alerter.alert("emergency", "80°C", T0))
        self.assertFalse(self.alerter.alert("emergency", "81°C", T0 + 1799))
        self.assertTrue(self.alerter.alert("slowdown", "slowdown", T0 + 10))
        self.assertTrue(self.alerter.alert("emergency", "82°C", T0 + 1800))
        self.assertEqual(self.sent(), [PREFIX + "80°C", PREFIX + "slowdown", PREFIX + "82°C"])

    def test_notify_never_limited(self):
        for i in range(5):
            self.alerter.notify(f"change {i}", T0)
        self.assertEqual(len(self.sent()), 5)

    def test_limit_survives_restart(self):
        self.alerter.alert("restart", "loop started", T0)
        again = self.make()
        self.assertFalse(again.alert("restart", "loop started", T0 + 60))

    def test_events_logged(self):
        self.alerter.alert("emergency", "80°C", T0)
        self.alerter.alert("emergency", "81°C", T0 + 1)
        self.alerter.notify("hello", T0 + 2)
        details = [e["detail"] for e in self.store.events(0, kinds=["alert"])]
        self.assertEqual(details, [
            {"kind": "emergency", "text": "80°C", "suppressed": False},
            {"kind": "emergency", "text": "81°C", "suppressed": True},
            {"kind": "notify", "text": "hello", "suppressed": False},
        ])


class TestIncidents(AlerterTest):
    def test_open_twice_alerts_once(self):
        self.alerter.open_incident("racadm", "racadm set failed", T0)
        self.alerter.open_incident("racadm", "racadm set failed", T0 + 3600)
        self.assertEqual(self.sent(), [PREFIX + "racadm set failed"])

    def test_close_sends_one_recovery(self):
        self.alerter.open_incident("nvidia", "nvidia-smi failing", T0)
        self.alerter.close_incident("nvidia", "nvidia-smi back", T0 + 60)
        self.alerter.close_incident("nvidia", "nvidia-smi back", T0 + 120)
        self.assertEqual(self.sent(), [PREFIX + "nvidia-smi failing", PREFIX + "nvidia-smi back"])

    def test_close_without_open_sends_nothing(self):
        self.alerter.close_incident("nvidia", "nvidia-smi back", T0)
        self.assertEqual(self.sent(), [])

    def test_incident_survives_restart(self):
        self.alerter.open_incident("slowdown", "thermal slowdown", T0)
        again = self.make()
        again.open_incident("slowdown", "thermal slowdown", T0 + 60)
        again.close_incident("slowdown", "slowdown over", T0 + 120)
        self.assertEqual(self.sent(again), [PREFIX + "thermal slowdown", PREFIX + "slowdown over"])

    def test_no_recovery_for_a_suppressed_alert(self):
        self.alerter.alert("slowdown", "earlier", T0)
        self.alerter.open_incident("slowdown", "thermal slowdown", T0 + 60)  # suppressed by the limit
        self.alerter.close_incident("slowdown", "slowdown over", T0 + 120)
        self.assertEqual(self.sent(), [PREFIX + "earlier"])


class TestSuppressedIncidents(AlerterTest):
    """A recovery followed by a rate-limited new incident would leave the user's last
    message saying all is well. A suppressed incident that lasts is sent late."""

    def since(self, ts):
        return local(ts, zone("EET")).strftime("%H:%M")

    def test_a_suppressed_incident_that_lasts_is_sent_late(self):
        self.alerter.open_incident("slowdown", "slowdown 1", T0)
        self.alerter.close_incident("slowdown", "over 1", T0 + 60)
        self.alerter.open_incident("slowdown", "slowdown 2", T0 + 420)  # inside the 30-min limit
        self.alerter.tick(T0 + 600)  # open for 3 min: still quiet
        self.alerter.tick(T0 + 720)  # open for 5 min: sent now
        self.alerter.tick(T0 + 780)
        self.alerter.close_incident("slowdown", "over 2", T0 + 900)
        self.assertEqual(self.sent(), [PREFIX + "slowdown 1", PREFIX + "over 1",
                                       PREFIX + f"slowdown 2 (ongoing since {self.since(T0 + 420)})",
                                       PREFIX + "over 2"])

    def test_a_short_suppressed_incident_stays_quiet(self):
        self.alerter.open_incident("nvidia", "nvidia 1", T0)
        self.alerter.close_incident("nvidia", "back 1", T0 + 20)
        self.alerter.open_incident("nvidia", "nvidia 2", T0 + 40)
        self.alerter.tick(T0 + 200)
        self.alerter.close_incident("nvidia", "back 2", T0 + 220)
        self.alerter.tick(T0 + 600)
        self.assertEqual(self.sent(), [PREFIX + "nvidia 1", PREFIX + "back 1"])

    def test_a_late_alert_survives_a_restart(self):
        self.alerter.alert("racadm", "earlier", T0)
        self.alerter.open_incident("racadm", "racadm failing", T0 + 60)
        again = self.make()
        again.tick(T0 + 400)
        self.assertEqual(self.sent(again), [PREFIX + "earlier",
                                            PREFIX + f"racadm failing (ongoing since {self.since(T0 + 60)})"])


def break_store(store, names=("add_event", "kv_get", "kv_set")):
    import sqlite3

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("database or disk is full")
    for name in names:
        setattr(store, name, fail)


class TestStoreDown(AlerterTest):
    def test_alerts_still_go_out_when_the_store_fails(self):
        self.alerter.alert("restart", "started", T0)
        break_store(self.store)
        self.assertTrue(self.alerter.alert("emergency", "hot", T0 + 10))
        self.assertFalse(self.alerter.alert("emergency", "hot again", T0 + 20))  # the limit holds in memory
        self.alerter.open_incident("racadm", "racadm failing", T0 + 30)
        self.alerter.open_incident("racadm", "racadm failing", T0 + 35)
        self.alerter.close_incident("racadm", "racadm back", T0 + 40)
        self.alerter.notify("note", T0 + 50)
        self.assertEqual(self.sent(), [PREFIX + "started", PREFIX + "hot", PREFIX + "racadm failing",
                                       PREFIX + "racadm back", PREFIX + "note"])

    def test_store_broken_from_the_start(self):
        break_store(self.store)
        alerter = self.make()
        self.assertTrue(alerter.alert("restart", "started", T0))
        alerter.open_incident("nvidia", "nvidia failing", T0 + 1)
        self.assertEqual(self.sent(alerter), [PREFIX + "started", PREFIX + "nvidia failing"])


class TestTelegramDown(AlerterTest):
    def test_bounded_queue_and_order(self):
        alerter = self.make(retry_s=0.01, max_queue=100)
        self.sender.failing = True
        for i in range(150):
            start = time.monotonic()
            alerter.notify(f"m{i:03d}", T0 + i)
            self.assertLess(time.monotonic() - start, 0.05)
        self.assertLessEqual(alerter.pending(), 100)
        self.assertEqual(alerter.dropped, 50)
        self.assertTrue(self.store.events(0, kinds=["telegram-dropped"]))
        time.sleep(0.05)
        self.assertTrue(self.store.events(0, kinds=["telegram-error"]))
        self.sender.failing = False
        sent = self.sent(alerter)
        self.assertEqual(len(sent), 100)
        self.assertEqual(sent, sorted(sent))
        self.assertEqual(sent[-1], PREFIX + "m149")
        self.assertEqual(len(self.store.events(0, kinds=["telegram-error"])), 1)  # one per outage
        self.assertTrue(self.store.events(0, kinds=["telegram-recovered"]))

    def test_never_raises(self):
        class Broken:
            def send(self, text):
                raise RuntimeError("unexpected")
        alerter = Alerter(Broken(), self.store, retry_s=0.01)
        alerter.start()
        self.addCleanup(alerter.stop)
        self.assertTrue(alerter.alert("x", "y", T0))
        alerter.notify("z", T0)
        time.sleep(0.05)


class _Telegram(http.server.BaseHTTPRequestHandler):
    reply = {"ok": True}
    requests = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        type(self).requests.append((self.path, urllib.parse.parse_qs(body)))
        data = json.dumps(type(self).reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class TestTelegramSender(unittest.TestCase):
    def serve(self, reply):
        handler = type("H", (_Telegram,), {"reply": reply, "requests": []})
        server = http.server.HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return handler, f"http://127.0.0.1:{server.server_port}"

    def test_posts_chat_id_and_text(self):
        handler, base = self.serve({"ok": True})
        TelegramSender("123:abc", "42", api_base=base).send("hello")
        path, form = handler.requests[0]
        self.assertEqual(path, "/bot123:abc/sendMessage")
        self.assertEqual((form["chat_id"], form["text"]), (["42"], ["hello"]))

    def test_raises_when_not_ok(self):
        _, base = self.serve({"ok": False, "description": "Bad Request: chat not found"})
        with self.assertRaises(ValueError):
            TelegramSender("123:abc", "42", api_base=base).send("hello")

    def test_raises_when_unreachable(self):
        with self.assertRaises(OSError):
            TelegramSender("123:abc", "42", api_base="http://127.0.0.1:9", timeout_s=1).send("hello")


if __name__ == "__main__":
    unittest.main()
