import unittest
from pathlib import Path

from fanloop.busy import BusyDetector
from fanloop.readings import CardReading
from fanloop.replay import read_fan_test_csv

DATA = Path(__file__).parents[1] / "data" / "fan-tests-2026-11-09"


def card(index=0, power=28.0, util=0.0, temp=45.0):
    return CardReading(index, temp, power, util, 585.0, False, False, False)


IDLE = [card(0), card(1)]
LOAD = [card(0, power=69.0, util=99.0), card(1, power=68.0, util=98.0)]


def feed(det, start, end, cards, step=1.0):
    """Feed samples at start, start+step, ... up to and including end."""
    t = start
    while t <= end + 1e-9:
        det.update(t, cards)
        t += step
    return det.busy


class TestBusyDetector(unittest.TestCase):
    def test_active_9s_never_busy(self):
        det = BusyDetector()
        self.assertFalse(feed(det, 0, 9, LOAD))
        self.assertFalse(feed(det, 10, 100, IDLE))

    def test_active_10s_busy_at_10(self):
        det = BusyDetector()
        self.assertFalse(feed(det, 0, 9, LOAD))
        self.assertTrue(det.update(10, LOAD))

    def test_gap_of_59s_keeps_busy(self):
        det = BusyDetector()
        feed(det, 0, 20, LOAD)
        self.assertTrue(feed(det, 21, 79, IDLE))  # inactive from 21 to 79: 58 s
        self.assertTrue(det.update(80, IDLE))  # 59 s
        self.assertTrue(feed(det, 81, 90, LOAD))

    def test_inactive_60s_ends_busy(self):
        det = BusyDetector()
        feed(det, 0, 20, LOAD)
        self.assertTrue(feed(det, 21, 80, IDLE))  # 59 s since first inactive sample
        self.assertFalse(det.update(81, IDLE))  # 60 s

    def test_one_card_over_power_is_active(self):
        det = BusyDetector()
        self.assertTrue(feed(det, 0, 10, [card(0, power=46.0), card(1)]))

    def test_util_exactly_50_is_active(self):
        det = BusyDetector()
        self.assertTrue(feed(det, 0, 10, [card(0), card(1, util=50.0)]))

    def test_empty_cards_change_nothing(self):
        det = BusyDetector()
        feed(det, 0, 20, LOAD)
        self.assertTrue(feed(det, 21, 200, []))  # blind for 180 s: still busy
        self.assertTrue(feed(det, 201, 259, IDLE))  # the end timer starts only now
        self.assertFalse(det.update(261, IDLE))


def busy_trace(rows):
    det = BusyDetector()
    return [(ts, det.update(ts, cards)) for ts, cards in rows]


def transitions(trace):
    ons, offs, prev = [], [], False
    for ts, busy in trace:
        if busy and not prev:
            ons.append(ts)
        if prev and not busy:
            offs.append(ts)
        prev = busy
    return ons, offs


def is_active(cards):
    return any(c.power_w >= 45.0 or c.util_pct >= 50.0 for c in cards)


class TestReplay(unittest.TestCase):
    def test_reader(self):
        rows = read_fan_test_csv(DATA / "gpu-fanlow.csv")
        self.assertEqual(len(rows), 701)
        ts, cards = rows[0]
        self.assertEqual([c.index for c in cards], [0, 1])
        self.assertEqual((cards[0].temp_c, cards[0].power_w, cards[0].util_pct, cards[0].clock_mhz), (44, 26.62, 0, 585))
        self.assertFalse(cards[0].sw_thermal)
        self.assertTrue(any(c.sw_thermal for _, cs in rows for c in cs))
        self.assertTrue(all(b > a for (a, _), (b, _) in zip(rows, rows[1:])))

    def test_replay_low(self):
        rows = read_fan_test_csv(DATA / "gpu-fanlow.csv")
        ons, offs = transitions(busy_trace(rows))
        first_load = next(ts for ts, cards in rows if any(c.util_pct > 0 for c in cards))
        last_active = max(ts for ts, cards in rows if is_active(cards))
        self.assertEqual(len(ons), 1)
        self.assertLessEqual(ons[0] - first_load, 15)
        self.assertEqual(len(offs), 1)
        self.assertGreaterEqual(offs[0] - last_active, 60)
        self.assertLessEqual(offs[0] - last_active, 62)

    def test_replay_medium(self):
        rows = read_fan_test_csv(DATA / "gpu-fanmedium.csv")
        trace = busy_trace(rows)
        ons, offs = transitions(trace)
        self.assertEqual(len(ons), 1)
        self.assertEqual(offs, [])
        self.assertTrue(trace[-1][1])


if __name__ == "__main__":
    unittest.main()
