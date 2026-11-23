import unittest

from fanloop.marks import Marks
from fanloop.messages import Message
from fanloop.store import Store
from fanloop.thresholds import load_defaults
from fanloop.watchdog import ReviewWatchdog

T0 = 1_800_000_000.0
H = 3600.0


class WatchdogTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:", load_defaults())
        self.wd = ReviewWatchdog(self.store)
        self.wd.ensure_started(T0)
        self.marks = Marks(self.store)

    def review(self, at, ok=True):
        mid = self.marks.start(at)
        if ok:
            self.marks.end(mid, at + 180)
        else:
            self.marks.expire(at + 900)


class TestAlerting(WatchdogTest):
    def test_fresh_install(self):
        self.assertEqual(self.wd.check(T0 + 13 * H - 1, True), [])
        msgs = self.wd.check(T0 + 13 * H, False)
        self.assertEqual(msgs, [Message("review-watchdog",
                                        "No completed Hermes review for 13 h. llama-server on t4: not running")])

    def test_ensure_started_keeps_the_first_time(self):
        self.wd.ensure_started(T0 + 5 * H)  # a restart later
        self.assertEqual(len(self.wd.check(T0 + 13 * H, True)), 1)

    def test_good_review_resets_clock(self):
        self.review(T0 + 10 * H)
        self.assertEqual(self.wd.check(T0 + 20 * H, True), [])
        self.assertEqual(len(self.wd.check(T0 + 10 * H + 180 + 13 * H, True)), 1)

    def test_timed_out_review_does_not_reset_clock(self):
        self.review(T0 + 10 * H, ok=False)
        self.assertEqual(len(self.wd.check(T0 + 13 * H, True)), 1)

    def test_restart_mid_incident(self):
        self.wd.check(T0 + 13 * H, True)
        restarted = ReviewWatchdog(self.store)
        restarted.ensure_started(T0 + 14 * H)
        self.assertEqual(restarted.check(T0 + 14 * H, True), [])

    def test_daily_reminder(self):
        self.wd.check(T0 + 13 * H, True)
        self.assertEqual(self.wd.check(T0 + 13 * H + 24 * H - 60, True), [])
        (reminder,) = self.wd.check(T0 + 13 * H + 24 * H, True)
        self.assertEqual(reminder.kind, "review-watchdog")
        self.assertIn("37 h", reminder.text)
        self.assertEqual(self.wd.check(T0 + 13 * H + 24 * H + 60, True), [])

    def test_back_to_normal_once(self):
        self.wd.check(T0 + 13 * H, True)
        self.review(T0 + 15 * H)
        self.assertEqual(self.wd.check(T0 + 15 * H + 300, True),
                         [Message("review-watchdog-ok", "Hermes reviews are completing again.")])
        self.assertEqual(self.wd.check(T0 + 15 * H + 600, True), [])

    def test_llama_text(self):
        for llama, word in ((True, "running"), (False, "not running"), (None, "unknown")):
            store = Store(":memory:", load_defaults())
            wd = ReviewWatchdog(store)
            wd.ensure_started(T0)
            (msg,) = wd.check(T0 + 13 * H, llama)
            self.assertTrue(msg.text.endswith(f"llama-server on t4: {word}"), msg.text)

    def test_clock_backwards(self):
        self.review(T0 + 10 * H)
        self.assertEqual(self.wd.check(T0 + 5 * H, True), [])


class TestMute(WatchdogTest):
    def test_muted_then_alert_after_expiry(self):
        until = T0 + 20 * H
        self.wd.mute(until, T0)
        self.assertTrue(self.wd.muted(T0 + 19 * H))
        self.assertEqual(self.wd.check(T0 + 13 * H, True), [])
        self.assertEqual(self.wd.check(until - 1, True), [])
        self.assertFalse(self.wd.muted(until))
        self.assertEqual(len(self.wd.check(until, True)), 1)

    def test_no_back_to_normal_after_a_silent_incident(self):
        self.wd.mute(T0 + 30 * H, T0)
        self.wd.check(T0 + 13 * H, True)
        self.review(T0 + 15 * H)
        self.assertEqual(self.wd.check(T0 + 16 * H, True), [])

    def test_unmute(self):
        self.wd.mute(T0 + 30 * H, T0)
        self.wd.unmute(T0 + 1)
        self.assertEqual(len(self.wd.check(T0 + 13 * H, True)), 1)


if __name__ == "__main__":
    unittest.main()
