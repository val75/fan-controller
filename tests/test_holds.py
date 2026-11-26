import unittest
from datetime import datetime, timezone

from fanloop.clock import zone
from fanloop.holds import Hold, Holds, parse_until
from fanloop.levels import Level
from fanloop.store import Store
from fanloop.thresholds import load_defaults

EET = zone("EET")
T0 = datetime(2026, 11, 23, 14, 0, tzinfo=EET).timestamp()  # a Monday, 14:00 in the loop's zone
H = 3600.0


class TestParseUntil(unittest.TestCase):
    def test_clock_time_later_today(self):
        self.assertEqual(parse_until("18:00", datetime(2026, 11, 23, 17, 0, tzinfo=EET)),
                         datetime(2026, 11, 23, 18, 0, tzinfo=EET))

    def test_clock_time_already_passed_means_tomorrow(self):
        self.assertEqual(parse_until("18:00", datetime(2026, 11, 23, 19, 0, tzinfo=EET)),
                         datetime(2026, 11, 24, 18, 0, tzinfo=EET))

    def test_full_date(self):
        self.assertEqual(parse_until("2026-12-01 08:30", datetime(2026, 11, 23, 9, 0, tzinfo=EET)),
                         datetime(2026, 12, 1, 8, 30, tzinfo=EET))

    def test_times_are_in_the_zone_of_now(self):
        # 18:00 EET in November is 16:00 UTC; in July (EEST) it is 15:00 UTC
        winter = parse_until("18:00", datetime(2026, 11, 26, 11, 0, tzinfo=EET))
        self.assertEqual(winter.timestamp(), datetime(2026, 11, 26, 16, 0, tzinfo=timezone.utc).timestamp())
        summer = parse_until("2026-07-01 18:00", datetime(2026, 6, 30, 9, 0, tzinfo=EET))
        self.assertEqual(summer.timestamp(), datetime(2026, 7, 1, 15, 0, tzinfo=timezone.utc).timestamp())

    def test_bad_text(self):
        for text in ("tomorrow", "25:00", "18", "2026-13-01 08:00", ""):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_until(text, datetime(2026, 11, 23, 9, 0, tzinfo=EET))


class HoldsTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:", load_defaults())
        self.holds = Holds(self.store, tz=EET)

    def kinds(self):
        return [e["kind"] for e in self.store.events(0)]


class TestSet(HoldsTest):
    def test_until_in_the_past(self):
        with self.assertRaises(ValueError):
            self.holds.set(Level.MEDIUM, T0 - 60, T0)
        with self.assertRaises(ValueError):
            self.holds.set(Level.MEDIUM, T0, T0)

    def test_at_most_24h_ahead(self):
        with self.assertRaises(ValueError):
            self.holds.set(Level.MEDIUM, T0 + 24 * H + 60, T0)
        self.assertEqual(self.holds.set(Level.MEDIUM, T0 + 24 * H, T0), Hold(Level.MEDIUM, T0, T0 + 24 * H))

    def test_off_is_not_a_hold(self):
        with self.assertRaises(ValueError):
            self.holds.set(Level.OFF, None, T0)

    def test_set_and_clear(self):
        hold = self.holds.set(Level.HIGH, None, T0)
        self.assertEqual(self.holds.current(T0 + 10), hold)
        self.assertEqual(self.holds.clear(T0 + 20), hold)
        self.assertIsNone(self.holds.current(T0 + 30))
        self.assertIsNone(self.holds.clear(T0 + 40))
        self.assertEqual(self.kinds(), ["hold-set", "hold-cleared"])

    def test_new_hold_replaces_old(self):
        self.holds.set(Level.HIGH, None, T0)
        self.holds.set(Level.MEDIUM, T0 + 2 * H, T0 + H)
        self.assertEqual(self.holds.current(T0 + H), Hold(Level.MEDIUM, T0 + H, T0 + 2 * H))


class TestExpiry(HoldsTest):
    def test_current_and_expire(self):
        hold = self.holds.set(Level.MEDIUM, T0 + H, T0)
        self.assertEqual(self.holds.current(T0 + H - 1), hold)
        self.assertIsNone(self.holds.current(T0 + H))
        self.assertEqual(self.holds.expire(T0 + H), hold)
        self.assertIsNone(self.holds.expire(T0 + H + 5))
        self.assertIn("hold-expired", self.kinds())

    def test_expire_before_until_does_nothing(self):
        self.holds.set(Level.MEDIUM, T0 + H, T0)
        self.assertIsNone(self.holds.expire(T0 + H - 1))
        self.assertIsNotNone(self.holds.current(T0 + H - 1))

    def test_untimed_hold_never_expires(self):
        self.holds.set(Level.MEDIUM, None, T0)
        self.assertIsNone(self.holds.expire(T0 + 30 * 24 * H))
        self.assertIsNotNone(self.holds.current(T0 + 30 * 24 * H))

    def test_restart_keeps_hold(self):
        hold = self.holds.set(Level.MEDIUM, None, T0)
        self.assertEqual(Holds(self.store).current(T0 + 5 * H), hold)


class TestReminder(HoldsTest):
    def test_daily_for_untimed_hold(self):
        self.holds.set(Level.MEDIUM, None, T0)
        self.assertIsNone(self.holds.reminder(T0 + 24 * H - 60))
        msg = self.holds.reminder(T0 + 24 * H)
        self.assertEqual(msg.kind, "hold-reminder")
        self.assertEqual(msg.text, "Hold at Medium since Mon 14:00, until cancelled. fanctl hold off to end it")
        self.assertIsNone(self.holds.reminder(T0 + 24 * H + 60))
        self.assertIsNotNone(self.holds.reminder(T0 + 48 * H))

    def test_timed_hold_gets_none(self):
        self.holds.set(Level.MEDIUM, T0 + 24 * H, T0)
        self.assertIsNone(self.holds.reminder(T0 + 24 * H - 1))

    def test_no_hold_no_reminder(self):
        self.assertIsNone(self.holds.reminder(T0 + 48 * H))

    def test_new_hold_restarts_reminders(self):
        self.holds.set(Level.MEDIUM, None, T0)
        self.holds.reminder(T0 + 24 * H)
        self.holds.set(Level.HIGH, None, T0 + 30 * H)
        self.assertIsNone(self.holds.reminder(T0 + 48 * H))
        self.assertIsNotNone(self.holds.reminder(T0 + 54 * H))


if __name__ == "__main__":
    unittest.main()
