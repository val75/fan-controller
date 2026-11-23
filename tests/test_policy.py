import unittest

from fanloop.episodes import Episode
from fanloop.policy import (
    GATE_EPISODES,
    GATE_MIN_S,
    REVIEW_LIMIT,
    USER_CHAT_LIMIT,
    WINDOW_S,
    WriteRequest,
    apply_write,
)
from fanloop.store import Store
from fanloop.thresholds import AmbientRule, Change, load_defaults
from fanloop.levels import Level

NOW = 1_800_000_000.0
D = load_defaults()


def episode(version, duration=400.0, overlap=False, hold=None, start=NOW - 10_000):
    return Episode(start=start, end=start + duration, duration_s=duration, inlet_min=14, inlet_mean=14,
                   inlet_max=14, peak_c={0: 74}, first_slowdown_s=None, throttled_s=0,
                   level_s={"medium": duration}, level_changes=0, review_overlap=overlap, hold_level=hold,
                   versions=[version] if isinstance(version, int) else list(version))


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:", D)

    def write(self, source, *changes, now=NOW, reason="test"):
        return apply_write(self.store, WriteRequest(source, reason, list(changes)), now)

    def versions(self):
        return len(self.store.versions())


class TestConstants(unittest.TestCase):
    def test_values_from_spec(self):
        self.assertEqual((REVIEW_LIMIT, USER_CHAT_LIMIT, WINDOW_S, GATE_EPISODES, GATE_MIN_S), (2, 6, 86400, 3, 360))


class TestMechanics(PolicyTest):
    def test_stale_from(self):
        r = self.write("user-cli", Change("step_up.medium", 64, 63))
        self.assertEqual((r.ok, r.error, r.new_version), (False, "stale", None))
        self.assertEqual(r.old_version, 1)
        self.assertEqual(self.versions(), 1)

    def test_review_with_two_changes(self):
        r = self.write("review", Change("margin", 5, 6), Change("hold_s", 180, 240))
        self.assertEqual(r.error, "review-multi")
        self.assertEqual(self.versions(), 1)

    def test_user_write_with_two_changes(self):
        r = self.write("user-cli", Change("margin", 5, 6), Change("hold_s", 180, 240))
        self.assertTrue(r.ok)
        self.assertEqual((r.old_version, r.new_version), (1, 2))
        self.assertEqual((r.new.margin, r.new.hold_s), (6, 240))
        self.assertEqual(len(self.store.versions(since_version=1)[0]["changes"]), 2)

    def test_invalid_second_change_applies_nothing(self):
        r = self.write("user-cli", Change("margin", 5, 6), Change("step_up.low", 55, 63))
        self.assertEqual(r.error, "invalid")
        self.assertIn("medium", r.detail)
        self.assertEqual(self.store.current(), (1, D))

    def test_unnamed_values_unchanged(self):
        r = self.write("user-cli", Change("step_up.medium", 65, 63))
        self.assertEqual(r.new, D.__class__(55, 63, 78, 5, 180, ()))

    def test_one_version_per_accepted_write(self):
        for i, change in enumerate([Change("margin", 5, 6), Change("margin", 6, 7), Change("hold_s", 180, 200)]):
            self.assertTrue(self.write("user-cli", change).ok)
            self.assertEqual(self.versions(), i + 2)

    def test_bad_source_and_empty(self):
        self.assertEqual(self.write("hermes", Change("margin", 5, 6)).error, "bad-source")
        self.assertEqual(self.write("user-cli").error, "empty")

    def test_not_writable(self):
        self.assertEqual(self.write("user-cli", Change("emergency", 80, 85)).error, "unknown-param")
        self.assertEqual(self.write("user-cli", Change("step_up.max", 80, 85)).error, "unknown-param")

    def test_noop_and_bad_value(self):
        self.assertEqual(self.write("user-cli", Change("margin", 5, 5)).error, "no-op")
        self.assertEqual(self.write("user-cli", Change("margin", 5, "6")).error, "bad-value")
        self.assertEqual(self.versions(), 1)


class TestLimits(PolicyTest):
    def test_review_limit(self):
        self.assertTrue(self.write("review", Change("step_up.low", 55, 54), now=NOW).ok)
        self.assertTrue(self.write("review", Change("step_up.low", 54, 53), now=NOW + 60).ok)
        r = self.write("review", Change("step_up.low", 53, 52), now=NOW + 3600)
        self.assertEqual(r.error, "rate-limit")
        self.assertEqual(self.write("review", Change("step_up.low", 53, 52), now=NOW + 86400 - 1).error, "rate-limit")
        self.assertTrue(self.write("review", Change("step_up.low", 53, 52), now=NOW + 86400).ok)  # the first just left

    def test_user_chat_limit(self):
        for i in range(6):
            r = self.write("user-chat", Change("hold_s", 180 + i, 181 + i), now=NOW + i)
            self.assertTrue(r.ok, r.detail)
        self.assertEqual(self.write("user-chat", Change("hold_s", 186, 187), now=NOW + 10).error, "rate-limit")

    def test_user_cli_unlimited(self):
        for i in range(50):
            self.assertTrue(self.write("user-cli", Change("hold_s", 180 + i, 181 + i), now=NOW + i).ok)

    def test_limits_are_per_source(self):
        self.write("review", Change("step_up.low", 55, 54))
        self.write("review", Change("step_up.low", 54, 53))
        self.assertTrue(self.write("user-chat", Change("margin", 5, 6)).ok)


class TestEvidenceGate(PolicyTest):
    QUIETER = Change("step_up.medium", 65, 66)

    def add(self, *episodes):
        for ep in episodes:
            self.store.add_episode(ep)

    def test_two_qualifying_is_not_enough(self):
        self.add(episode(1), episode(1))
        r = self.write("review", self.QUIETER)
        self.assertEqual(r.error, "evidence")
        self.assertIn("2", r.detail)

    def test_three_qualifying_passes(self):
        self.add(episode(1), episode(1), episode(1, duration=360))
        self.assertTrue(self.write("review", self.QUIETER).ok)

    def test_non_qualifying_episodes_dont_count(self):
        self.add(episode(1), episode(1),
                 episode(1, duration=359), episode(1, overlap=True), episode(1, hold="medium"),
                 episode(2), episode([1, 2]))
        self.assertEqual(self.write("review", self.QUIETER).error, "evidence")

    def test_louder_needs_no_evidence(self):
        for change in (Change("step_up.medium", 65, 64), Change("margin", 5, 6),
                       Change("ambient.add", None, AmbientRule(28, Level.HIGH))):
            store = Store(":memory:", D)
            r = apply_write(store, WriteRequest("review", "x", [change]), NOW)
            self.assertTrue(r.ok, f"{change}: {r.detail}")

    def test_user_writes_not_gated(self):
        self.assertTrue(self.write("user-chat", self.QUIETER).ok)
        self.assertTrue(self.write("user-cli", Change("step_up.medium", 66, 67)).ok)

    def test_evidence_counts_only_current_version(self):
        self.add(episode(1), episode(1), episode(1))
        self.assertTrue(self.write("user-cli", Change("margin", 5, 6)).ok)  # now on version 2
        self.assertEqual(self.write("review", self.QUIETER).error, "evidence")


if __name__ == "__main__":
    unittest.main()
