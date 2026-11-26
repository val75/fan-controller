import unittest
from datetime import datetime, timezone

from fanloop.clock import DEFAULT_TZ, clock_text, local, zone

EET = zone("EET")


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc).timestamp()


class TestClock(unittest.TestCase):
    def test_default_is_eet(self):
        self.assertEqual(DEFAULT_TZ, "EET")
        self.assertEqual(str(zone(DEFAULT_TZ)), "EET")

    def test_unknown_zone_is_refused(self):
        for name in ("Mars/Olympus", "", "../etc/passwd"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                zone(name)

    def test_local_time_in_winter_and_summer(self):
        self.assertEqual(local(utc(2026, 11, 26, 9, 51), EET).strftime("%H:%M %Z"), "11:51 EET")
        self.assertEqual(local(utc(2026, 7, 1, 12, 0), EET).strftime("%H:%M %Z"), "15:00 EEST")

    def test_clock_text_same_day_and_other_day(self):
        now = utc(2026, 11, 26, 9, 0)  # Thu 11:00 EET
        self.assertEqual(clock_text(utc(2026, 11, 26, 16, 0), now, EET), "18:00")
        self.assertEqual(clock_text(utc(2026, 11, 27, 7, 0), now, EET), "Fri 09:00")
        # 23:30 UTC on Thursday is already Friday 01:30 in EET
        self.assertEqual(clock_text(utc(2026, 11, 26, 23, 30), now, EET), "Fri 01:30")


if __name__ == "__main__":
    unittest.main()
