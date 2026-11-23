import unittest

from fanloop.marks import Marks
from fanloop.store import Store
from fanloop.thresholds import load_defaults

T0 = 1_800_000_000.0


class TestMarks(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:", load_defaults())
        self.marks = Marks(self.store)

    def kinds(self):
        return [e["kind"] for e in self.store.events(0)]

    def test_start_then_end(self):
        mid = self.marks.start(T0)
        self.assertTrue(self.marks.active(T0 + 60))
        self.assertTrue(self.marks.end(mid, T0 + 200))
        self.assertEqual(self.store.mark_get(mid)["outcome"], "ended")
        self.assertEqual(self.store.kv_get("last_review_ok_at"), T0 + 200)
        self.assertFalse(self.marks.active(T0 + 201))
        self.assertEqual(self.kinds(), ["mark-start", "mark-end"])

    def test_timeout_at_900s(self):
        mid = self.marks.start(T0)
        self.assertEqual(self.marks.expire(T0 + 899), [])
        self.assertEqual(self.marks.expire(T0 + 900), [mid])
        mark = self.store.mark_get(mid)
        self.assertEqual((mark["outcome"], mark["ended_at"]), ("timed_out", T0 + 900))
        self.assertIsNone(self.store.kv_get("last_review_ok_at"))
        self.assertIn("mark-timeout", self.kinds())

    def test_second_start_supersedes(self):
        first = self.marks.start(T0)
        second = self.marks.start(T0 + 100)
        self.assertEqual(self.store.mark_get(first)["outcome"], "superseded")
        self.assertEqual([m["id"] for m in self.store.marks_open()], [second])
        self.assertIn("mark-superseded", self.kinds())

    def test_unknown_id(self):
        self.marks.start(T0)
        self.assertFalse(self.marks.end(999, T0 + 10))
        self.assertEqual(len(self.store.marks_open()), 1)
        self.assertIsNone(self.store.kv_get("last_review_ok_at"))
        self.assertIn("mark-end-unknown", self.kinds())

    def test_end_after_timeout(self):
        mid = self.marks.start(T0)
        self.marks.expire(T0 + 900)
        self.assertFalse(self.marks.end(mid, T0 + 950))
        self.assertEqual(self.store.mark_get(mid)["outcome"], "timed_out")

    def test_late_end_before_expire_ran_counts_as_timed_out(self):
        mid = self.marks.start(T0)
        self.assertFalse(self.marks.end(mid, T0 + 1000))
        self.assertEqual(self.store.mark_get(mid)["outcome"], "timed_out")
        self.assertIsNone(self.store.kv_get("last_review_ok_at"))

    def test_restart_still_expires_old_mark(self):
        mid = self.marks.start(T0)
        restarted = Marks(self.store)
        self.assertTrue(restarted.active(T0 + 10))
        self.assertEqual(restarted.expire(T0 + 900), [mid])


if __name__ == "__main__":
    unittest.main()
