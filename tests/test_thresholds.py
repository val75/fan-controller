import dataclasses
import unittest

from fanloop.levels import Level
from fanloop.thresholds import (
    EMERGENCY_C,
    AmbientRule,
    BadChange,
    Change,
    StaleFrom,
    Thresholds,
    apply_changes,
    direction,
    load_defaults,
    validate,
)

D = Thresholds(55, 65, 78, 5, 180, ())


def with_(**kw):
    return dataclasses.replace(D, **kw)


class TestDefaults(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(load_defaults(), D)
        self.assertEqual(validate(load_defaults()), [])

    def test_step_up(self):
        self.assertIsNone(D.step_up(Level.OFF))
        self.assertEqual(D.step_up(Level.LOW), 55)
        self.assertEqual(D.step_up(Level.MEDIUM), 65)
        self.assertEqual(D.step_up(Level.HIGH), 78)
        self.assertEqual(D.step_up(Level.MAX), EMERGENCY_C)
        self.assertEqual(EMERGENCY_C, 80.0)

    def test_dict_round_trip(self):
        t = with_(ambient_rules=(AmbientRule(28, Level.HIGH),))
        d = t.to_dict()
        self.assertEqual(d["ambient_rules"], [{"inlet_min": 28, "level": "high"}])
        self.assertEqual(Thresholds.from_dict(d), t)

    def test_from_dict_rejects_bad_keys(self):
        d = D.to_dict()
        with self.assertRaises(ValueError):
            Thresholds.from_dict({**d, "emergency": 85})
        d.pop("margin")
        with self.assertRaises(ValueError):
            Thresholds.from_dict(d)


class TestBounds(unittest.TestCase):
    def assert_rejected(self, t, param):
        errors = validate(t)
        self.assertTrue(errors, f"{t} should be invalid")
        self.assertTrue(any(param in e for e in errors), f"{errors} should name {param}")

    def test_step_ups(self):
        self.assert_rejected(with_(low=44.9), "low")
        self.assert_rejected(with_(high=78.1), "high")
        self.assert_rejected(with_(medium=55 + 2.9), "medium")
        self.assert_rejected(with_(low=66, medium=65), "medium")

    def test_margin_and_hold(self):
        self.assert_rejected(with_(margin=2.9), "margin")
        self.assert_rejected(with_(margin=10.1), "margin")
        self.assert_rejected(with_(hold_s=59), "hold_s")
        self.assert_rejected(with_(hold_s=901), "hold_s")
        self.assert_rejected(with_(hold_s=120.5), "hold_s")

    def test_ambient(self):
        self.assert_rejected(with_(ambient_rules=(AmbientRule(14.9, Level.HIGH),)), "ambient")
        self.assert_rejected(with_(ambient_rules=(AmbientRule(40.1, Level.HIGH),)), "ambient")
        self.assert_rejected(with_(ambient_rules=(AmbientRule(28, Level.OFF),)), "ambient")
        nine = tuple(AmbientRule(15 + i, Level.MEDIUM) for i in range(9))
        self.assert_rejected(with_(ambient_rules=nine), "ambient")
        dup = (AmbientRule(28, Level.MEDIUM), AmbientRule(28, Level.HIGH))
        self.assert_rejected(with_(ambient_rules=dup), "ambient")

    def test_limits_themselves_are_valid(self):
        self.assertEqual(validate(Thresholds(45, 48, 78, 3, 60, ())), [])
        self.assertEqual(validate(Thresholds(55, 65, 78, 10, 900, (AmbientRule(15, Level.LOW), AmbientRule(40, Level.MAX)))), [])


class TestApply(unittest.TestCase):
    def test_apply_stale(self):
        with self.assertRaises(StaleFrom) as cm:
            apply_changes(D, [Change("step_up.medium", 64, 63)])
        self.assertEqual(cm.exception.param, "step_up.medium")

    def test_apply_keeps_unnamed(self):
        t = apply_changes(D, [Change("margin", 5, 6)])
        self.assertEqual(t, with_(margin=6))

    def test_apply_sequential(self):
        t = apply_changes(D, [Change("step_up.medium", 65, 70), Change("step_up.low", 55, 66)])
        self.assertEqual((t.low, t.medium), (66, 70))

    def test_noop_and_unknown(self):
        for change in (Change("margin", 5, 5), Change("emergency", 80, 85), Change("step_up.max", 80, 85)):
            with self.subTest(change=change), self.assertRaises(BadChange):
                apply_changes(D, [change])

    def test_wrong_value_types(self):
        for change in (Change("margin", 5, "6"), Change("margin", 5, True), Change("ambient.add", None, 28)):
            with self.subTest(change=change), self.assertRaises(BadChange):
                apply_changes(D, [change])

    def test_ambient_ops(self):
        rule = AmbientRule(28, Level.HIGH)
        added = apply_changes(D, [Change("ambient.add", None, rule)])
        self.assertEqual(added.ambient_rules, (rule,))
        with self.assertRaises(StaleFrom):
            apply_changes(D, [Change("ambient.remove", rule, None)])
        replaced = apply_changes(added, [Change("ambient.replace", rule, AmbientRule(25, Level.HIGH))])
        self.assertEqual(replaced.ambient_rules, (AmbientRule(25, Level.HIGH),))
        removed = apply_changes(added, [Change("ambient.remove", rule, None)])
        self.assertEqual(removed.ambient_rules, ())

    def test_ambient_rules_kept_sorted(self):
        t = apply_changes(D, [Change("ambient.add", None, AmbientRule(30, Level.MAX)),
                              Change("ambient.add", None, AmbientRule(20, Level.MEDIUM))])
        self.assertEqual([r.inlet_min for r in t.ambient_rules], [20, 30])


class TestDirection(unittest.TestCase):
    def test_scalars(self):
        self.assertEqual(direction(Change("step_up.low", 55, 54)), "louder")
        self.assertEqual(direction(Change("step_up.high", 78, 76)), "louder")
        self.assertEqual(direction(Change("step_up.medium", 65, 66)), "quieter")
        self.assertEqual(direction(Change("margin", 5, 6)), "louder")
        self.assertEqual(direction(Change("margin", 5, 4)), "quieter")
        self.assertEqual(direction(Change("hold_s", 180, 240)), "louder")
        self.assertEqual(direction(Change("hold_s", 180, 120)), "quieter")

    def test_ambient(self):
        r = AmbientRule(28, Level.MEDIUM)
        self.assertEqual(direction(Change("ambient.add", None, r)), "louder")
        self.assertEqual(direction(Change("ambient.remove", r, None)), "quieter")
        self.assertEqual(direction(Change("ambient.replace", r, AmbientRule(25, Level.MEDIUM))), "louder")
        self.assertEqual(direction(Change("ambient.replace", r, AmbientRule(28, Level.HIGH))), "louder")
        self.assertEqual(direction(Change("ambient.replace", r, AmbientRule(30, Level.MEDIUM))), "quieter")
        self.assertEqual(direction(Change("ambient.replace", r, AmbientRule(28, Level.LOW))), "quieter")
        self.assertEqual(direction(Change("ambient.replace", r, AmbientRule(25, Level.LOW))), "quieter")


class TestAmbientMin(unittest.TestCase):
    def test_ambient_min(self):
        t = with_(ambient_rules=(AmbientRule(20, Level.MEDIUM), AmbientRule(28, Level.HIGH)))
        self.assertEqual(t.ambient_min(25), Level.MEDIUM)
        self.assertEqual(t.ambient_min(28), Level.HIGH)
        self.assertEqual(t.ambient_min(19.9), Level.OFF)
        self.assertEqual(t.ambient_min(None), Level.OFF)


if __name__ == "__main__":
    unittest.main()
