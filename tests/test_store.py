import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from fanloop.episodes import Episode
from fanloop.levels import Level
from fanloop.readings import CardReading, IpmiReading
from fanloop.store import Store, inspect
from fanloop.thresholds import AmbientRule, Change, Thresholds, load_defaults

DEFAULTS = load_defaults()
DAY = 86400.0
NOW = 1_800_000_000.0  # a fixed wall-clock time


def cards(temp=60.0, sw=False):
    return [CardReading(0, temp, 65.0, 95.0, 1100.0, True, sw, False),
            CardReading(1, temp - 4, 64.0, 94.0, 1100.0, True, False, False)]


def episode(start, versions=(1,), duration=400.0):
    return Episode(start=start, end=start + duration, duration_s=duration, inlet_min=14, inlet_mean=14,
                   inlet_max=14, peak_c={0: 74, 1: 70}, first_slowdown_s=None, throttled_s=0,
                   level_s={"medium": duration}, level_changes=1, review_overlap=False, hold_level=None,
                   versions=list(versions))


class TempDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "fan.db")

    def open(self, path=None):
        store = Store(path or self.path, DEFAULTS)
        self.addCleanup(store.close)
        return store


class TestVersions(TempDir):
    def test_fresh_store_seeds_defaults(self):
        store = self.open()
        self.assertEqual(store.current(), (1, DEFAULTS))
        self.assertEqual(store.versions()[0]["source"], "defaults")

    def test_add_version(self):
        store = self.open()
        rule = AmbientRule(28, Level.HIGH)
        new = Thresholds(55, 63, 78, 5, 180, (rule,))
        changes = [Change("step_up.medium", 65, 63), Change("ambient.add", None, rule)]
        self.assertEqual(store.add_version(new, "user-cli", "test", changes, NOW), 2)
        self.assertEqual(store.current(), (2, new))
        self.assertEqual(store.get_version(1), DEFAULTS)
        v2 = store.versions(since_version=1)
        self.assertEqual(len(v2), 1)
        self.assertEqual(v2[0]["version"], 2)
        self.assertEqual(v2[0]["source"], "user-cli")
        self.assertEqual(v2[0]["changes"], [
            {"param": "step_up.medium", "from": 65, "to": 63},
            {"param": "ambient.add", "from": None, "to": {"inlet_min": 28, "level": "high"}},
        ])
        self.assertEqual(v2[0]["thresholds"]["medium"], 63)

    def test_count_versions(self):
        store = self.open()
        t = DEFAULTS
        for i, (source, ts) in enumerate([("review", NOW - 2 * DAY), ("review", NOW - 3600),
                                          ("user-chat", NOW - 60), ("review", NOW)]):
            t = Thresholds(t.low, t.medium, t.high, t.margin, 181 + i, ())
            store.add_version(t, source, "x", [], ts)
        self.assertEqual(store.count_versions("review", NOW - DAY), 2)
        self.assertEqual(store.count_versions("user-chat", NOW - DAY), 1)
        self.assertEqual(len(store.versions(since_ts=NOW - 3600)), 3)

    def test_restart_keeps_current(self):
        first = Store(self.path, DEFAULTS)
        new = Thresholds(55, 65, 78, 6, 180, ())
        first.add_version(new, "user-cli", "x", [Change("margin", 5, 6)], NOW)
        first.close()
        self.assertEqual(self.open().current(), (2, new))


class TestSamples(unittest.TestCase):
    def test_downsampled_to_max_points(self):
        store = Store(":memory:", DEFAULTS)
        start = NOW - 30 * DAY
        ts = start
        while ts < NOW:
            store.add_sample(ts, cards(60 + (ts - start) / DAY), IpmiReading(14, 20, {"Fan1": 7200}, 238),
                             Level.MEDIUM, True)
            ts += 120
        rows = store.samples(start, NOW)
        self.assertLessEqual(len(rows), 300)
        self.assertGreater(len(rows), 250)
        first = rows[0]
        self.assertEqual(first["level"], "medium")
        self.assertTrue(first["busy"])
        self.assertAlmostEqual(first["inlet"], 14)
        self.assertLess(rows[0]["temp0"], rows[-1]["temp0"])
        self.assertIn("temp1_max", first)

    def test_level_is_loudest_in_bucket(self):
        store = Store(":memory:", DEFAULTS)
        store.add_sample(NOW, cards(), None, Level.LOW, False)
        store.add_sample(NOW + 5, cards(sw=True), None, Level.MAX, True)
        (row,) = store.samples(NOW, NOW + 10, max_points=1)
        self.assertEqual((row["level"], row["busy"], row["throttled"]), ("max", True, True))

    def test_missing_values(self):
        store = Store(":memory:", DEFAULTS)
        store.add_sample(NOW, [], None, None, False)
        (row,) = store.samples(NOW, NOW + 1)
        self.assertIsNone(row["temp0"])
        self.assertIsNone(row["level"])
        self.assertIsNone(row["inlet"])

    def test_prune_removes_old_samples_only(self):
        store = Store(":memory:", DEFAULTS)
        store.add_sample(NOW - 31 * DAY, cards(), None, Level.OFF, False)
        store.add_sample(NOW - 29 * DAY, cards(), None, Level.OFF, False)
        store.add_event(NOW - 40 * DAY, "level", {"to": "low"})
        store.add_episode(episode(NOW - 40 * DAY))
        self.assertEqual(store.prune(NOW), 1)
        self.assertEqual(len(store.samples(NOW - 60 * DAY, NOW)), 1)
        self.assertEqual(len(store.events(0)), 1)
        self.assertEqual(len(store.episodes()), 1)
        self.assertEqual(store.current()[0], 1)


class TestEventsEpisodesKv(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:", DEFAULTS)

    def test_events(self):
        self.store.add_event(NOW, "level", {"from": "off", "to": "low"})
        self.store.add_event(NOW + 5, "alert", {"kind": "restart"})
        self.assertEqual(self.store.events(NOW)[0], {"ts": NOW, "kind": "level", "detail": {"from": "off", "to": "low"}})
        self.assertEqual([e["kind"] for e in self.store.events(NOW, kinds=["alert"])], ["alert"])
        self.assertEqual(len(self.store.events(NOW + 1)), 1)
        self.assertEqual(len(self.store.events(0, limit=1)), 1)

    def test_episodes(self):
        self.store.add_episode(episode(NOW, versions=[3]))
        self.store.add_episode(episode(NOW + 1000, versions=[3, 4]))
        self.store.add_episode(episode(NOW + 2000, versions=[4]))
        self.assertEqual(len(self.store.episodes()), 3)
        self.assertEqual([e.start for e in self.store.episodes(version=4)], [NOW + 2000])
        self.assertEqual(len(self.store.episodes(since_ts=NOW + 500)), 2)
        self.assertEqual(self.store.episodes()[0], episode(NOW, versions=[3]))

    def test_marks(self):
        a = self.store.mark_insert(NOW)
        b = self.store.mark_insert(NOW + 10)
        self.store.mark_close(a, NOW + 5, "ended")
        self.assertEqual(self.store.mark_get(a), {"id": a, "started_at": NOW, "ended_at": NOW + 5, "outcome": "ended"})
        self.assertEqual([m["id"] for m in self.store.marks_open()], [b])
        self.assertEqual(len(self.store.marks(NOW)), 2)
        self.assertIsNone(self.store.mark_get(999))

    def test_kv(self):
        self.store.kv_set("hold", {"level": "medium", "until": None})
        self.store.kv_set("last_review_ok_at", NOW + 0.5)
        self.assertEqual(self.store.kv_get("hold"), {"level": "medium", "until": None})
        self.assertEqual(self.store.kv_get("last_review_ok_at"), NOW + 0.5)
        self.assertEqual(self.store.kv_get("missing", "x"), "x")
        self.store.kv_set("hold", None)
        self.assertIsNone(self.store.kv_get("hold", "default"))

    def test_threads(self):
        def add():
            for i in range(500):
                self.store.add_event(NOW + i, "t", {"i": i})
        threads = [threading.Thread(target=add) for _ in range(2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(len(self.store.events(0, limit=5000)), 1000)


class TestBackupAndInspect(TempDir):
    def test_backup(self):
        store = self.open()
        store.add_version(Thresholds(55, 65, 78, 6, 180, ()), "user-cli", "x", [Change("margin", 5, 6)], NOW)
        copy = str(Path(self.tmp.name) / "copy.db")
        store.backup_to(copy)
        self.assertEqual(self.open(copy).current(), store.current())

    def test_inspect_is_read_only(self):
        store = self.open()
        store.add_event(NOW, "level", {})
        store.close()
        summary = inspect(self.path)
        self.assertEqual(summary, {"current_version": 1, "versions": 1, "episodes": 0, "events": 1, "samples": 0})

    def test_module_main(self):
        self.open().add_event(NOW, "level", {})
        out = subprocess.run([sys.executable, "-m", "fanloop.store", self.path], capture_output=True, text=True,
                             cwd=Path(__file__).parents[1], check=True).stdout
        self.assertEqual(json.loads(out)["current_version"], 1)


if __name__ == "__main__":
    unittest.main()
