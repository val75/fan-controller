import json
import unittest

from fanloop.episodes import Episode, EpisodeTracker
from fanloop.levels import Level
from fanloop.readings import CardReading


def cards(temp0, temp1, sw0=False):
    return [CardReading(0, temp0, 69.0, 99.0, 1100.0, True, sw0, False),
            CardReading(1, temp1, 68.0, 98.0, 1100.0, True, False, False)]


def run_sequence(tracker):
    """Busy 0-395 s (every 5 s), idle at 400 s. Returns (episodes returned while busy, final)."""
    during = []
    for ts in range(0, 400, 5):
        level = Level.MEDIUM if ts < 200 else Level.HIGH
        inlet = 12 if ts < 130 else 14 if ts < 260 else 16
        temp0 = 60 + ts / 10  # rises to 99.5 at 395
        during.append(tracker.update(
            ts=ts, busy=True, cards=cards(temp0, temp0 - 4, sw0=300 <= ts < 320),
            inlet_c=inlet, level=level, version=3 if ts < 250 else 4,
            mark_active=(ts == 100), hold_level=Level.MEDIUM))
    final = tracker.update(ts=400, busy=False, cards=cards(50, 46), inlet_c=16, level=Level.HIGH,
                           version=4, mark_active=False, hold_level=Level.MEDIUM)
    return during, final


class TestEpisodeTracker(unittest.TestCase):
    def setUp(self):
        self.during, self.ep = run_sequence(EpisodeTracker())

    def test_nothing_returned_while_busy(self):
        self.assertTrue(all(e is None for e in self.during))
        self.assertIsInstance(self.ep, Episode)

    def test_times_and_levels(self):
        self.assertEqual((self.ep.start, self.ep.end, self.ep.duration_s), (0, 400, 400))
        self.assertEqual(self.ep.level_s, {"medium": 200, "high": 200})
        self.assertEqual(self.ep.level_changes, 1)

    def test_slowdown(self):
        self.assertEqual(self.ep.first_slowdown_s, 300)
        self.assertEqual(self.ep.throttled_s, 20)

    def test_inlet(self):
        self.assertEqual((self.ep.inlet_min, self.ep.inlet_max), (12, 16))
        self.assertTrue(12 < self.ep.inlet_mean < 16)

    def test_tags(self):
        self.assertTrue(self.ep.review_overlap)
        self.assertEqual(self.ep.hold_level, "medium")
        self.assertEqual(self.ep.versions, [3, 4])

    def test_peaks(self):
        self.assertEqual(self.ep.peak_c, {0: 99.5, 1: 95.5})

    def test_round_trip_through_json(self):
        again = Episode.from_dict(json.loads(json.dumps(self.ep.to_dict())))
        self.assertEqual(again, self.ep)


class TestEpisodeEdges(unittest.TestCase):
    def idle(self, tracker, ts):
        return tracker.update(ts=ts, busy=False, cards=cards(40, 36), inlet_c=None, level=Level.OFF,
                              version=1, mark_active=False, hold_level=None)

    def test_never_busy(self):
        tracker = EpisodeTracker()
        self.assertTrue(all(self.idle(tracker, ts) is None for ts in range(0, 100, 5)))

    def test_second_episode_starts_clean(self):
        tracker = EpisodeTracker()
        run_sequence(tracker)
        for ts in range(1000, 1060, 5):
            tracker.update(ts=ts, busy=True, cards=cards(60, 56), inlet_c=None, level=Level.LOW,
                           version=5, mark_active=False, hold_level=None)
        ep = self.idle(tracker, 1060)
        self.assertEqual((ep.start, ep.duration_s), (1000, 60))
        self.assertEqual(ep.level_s, {"low": 60})
        self.assertEqual((ep.versions, ep.review_overlap, ep.hold_level), ([5], False, None))
        self.assertIsNone(ep.first_slowdown_s)
        self.assertEqual(ep.throttled_s, 0)
        self.assertEqual((ep.inlet_min, ep.inlet_mean, ep.inlet_max), (None, None, None))

    def test_missing_card_still_tracks_the_other(self):
        tracker = EpisodeTracker()
        tracker.update(ts=0, busy=True, cards=[CardReading(1, 70, 60, 90, 1100, False, False, False)],
                       inlet_c=None, level=Level.MEDIUM, version=1, mark_active=False, hold_level=None)
        ep = self.idle(tracker, 5)
        self.assertEqual(ep.peak_c, {1: 70})


if __name__ == "__main__":
    unittest.main()
