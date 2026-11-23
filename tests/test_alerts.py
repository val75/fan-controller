import http.server
import json
import threading
import time
import unittest
import urllib.parse

from fanloop.alerts import PREFIX, Alerter, TelegramSender
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
