import unittest
from pathlib import Path

from fanloop.control import ControlConfig, ControlInputs, Controller, Decision
from fanloop.levels import Level
from fanloop.readings import CardReading
from fanloop.replay import read_fan_test_csv, run_control
from fanloop.thresholds import AmbientRule, Thresholds, load_defaults

DATA = Path(__file__).parents[1] / "data" / "fan-tests-2026-11-09"
D = Thresholds(55, 65, 78, 5, 180, ())


def card(index, temp, sw=False, hw=False):
    return CardReading(index, temp, 60.0, 90.0, 1200.0, False, sw, hw)


def t(temp, card1=None, sw=False, hw=False):
    """Two cards: card 0 at `temp`, card 1 4°C cooler unless given."""
    return [card(0, temp, sw, hw), card(1, temp - 4 if card1 is None else card1)]


def inputs(now, cards, busy=False, inlet=None, power=None, hold=None):
    return ControlInputs(now=now, cards=cards, busy=busy, inlet_c=inlet, system_power_w=power, hold_min=hold)


class Sim:
    """Feeds a Controller over time and applies each decision as the new level."""

    def __init__(self, level=Level.OFF, thresholds=D, **cfg):
        self.ctl = Controller(thresholds, ControlConfig(**cfg))
        self.level = level
        self.now = 0.0
        self.last = None

    def step(self, cards, **kw):
        self.last = self.ctl.decide(inputs(self.now, cards, **kw), self.level)
        self.level = self.last.level
        return self.last

    def run(self, seconds, cards, step=5.0, **kw):
        """Run for `seconds` (inclusive of the end), one decision every `step`."""
        end = self.now + seconds
        while self.now <= end + 1e-9:
            self.step(cards, **kw)
            self.now += step
        self.now -= step
        return self.level

    def at(self, offset, cards, **kw):
        """One decision at now + offset."""
        self.now += offset
        return self.step(cards, **kw)


class TestStepUp(unittest.TestCase):
    def test_idle(self):
        self.assertEqual(Sim().step(t(40)), Decision(Level.OFF, "steady"))

    def test_thresholds(self):
        for temp, level in ((54.9, Level.OFF), (55, Level.LOW), (65, Level.MEDIUM), (78, Level.HIGH)):
            with self.subTest(temp=temp):
                self.assertEqual(Sim().step(t(temp)).level, level)

    def test_several_steps_at_once(self):
        self.assertEqual(Sim().step(t(70)), Decision(Level.MEDIUM, "step-up"))


class TestEmergency(unittest.TestCase):
    def test_80(self):
        self.assertEqual(Sim().step(t(80)), Decision(Level.MAX, "emergency"))

    def test_flags(self):
        self.assertEqual(Sim().step(t(60, sw=True)), Decision(Level.MAX, "emergency"))
        self.assertEqual(Sim().step(t(60, hw=True)), Decision(Level.MAX, "emergency"))

    def test_card1_flag(self):
        cards = [card(0, 60), card(1, 56, sw=True)]
        self.assertEqual(Sim().step(cards).level, Level.MAX)


class TestStepDownNotBusy(unittest.TestCase):
    def test_high_after_hold(self):
        sim = Sim(Level.HIGH)
        sim.run(175, t(72.9))
        self.assertEqual(sim.level, Level.HIGH)
        sim.at(4, t(72.9))  # 179 s
        self.assertEqual(sim.level, Level.HIGH)
        self.assertEqual(sim.at(1, t(72.9)), Decision(Level.MEDIUM, "step-down"))  # 180 s

    def test_not_below_threshold(self):
        sim = Sim(Level.HIGH)
        self.assertEqual(sim.run(1000, t(73.0)), Level.HIGH)

    def test_each_step_needs_its_own_hold(self):
        sim = Sim(Level.HIGH)
        sim.run(180, t(59))
        self.assertEqual(sim.level, Level.MEDIUM)
        sim.run(175, t(59))  # Medium's own timer started at the step
        self.assertEqual(sim.level, Level.MEDIUM)
        sim.run(10, t(59))
        self.assertEqual(sim.level, Level.LOW)

    def test_interruption_restarts_timer(self):
        sim = Sim(Level.HIGH)
        sim.run(150, t(72))
        sim.at(5, t(74))  # condition fails once
        sim.run(170, t(72))
        self.assertEqual(sim.level, Level.HIGH)
        sim.run(20, t(72))
        self.assertEqual(sim.level, Level.MEDIUM)

    def test_low_to_off(self):
        sim = Sim(Level.LOW)
        self.assertEqual(sim.run(180, t(30)), Level.OFF)


class TestStepDownBusy(unittest.TestCase):
    def test_high_holds_while_busy(self):
        sim = Sim(Level.HIGH)
        self.assertEqual(sim.run(600, t(70), busy=True), Level.HIGH)
        sim.now += 5
        self.assertEqual(sim.run(180, t(59.9), busy=True), Level.MEDIUM)

    def test_medium_to_low_while_busy(self):
        sim = Sim(Level.MEDIUM)
        self.assertEqual(sim.run(175, t(49.9), busy=True), Level.MEDIUM)
        self.assertEqual(sim.run(5, t(49.9), busy=True), Level.LOW)

    def test_low_never_off_while_busy(self):
        self.assertEqual(Sim(Level.LOW).run(900, t(30), busy=True), Level.LOW)

    def test_leaving_max_uses_normal_rule_while_busy(self):
        sim = Sim(Level.MAX)
        self.assertEqual(sim.run(175, t(74.9), busy=True), Level.MAX)
        self.assertEqual(sim.at(5, t(74.9), busy=True), Decision(Level.HIGH, "step-down"))


class TestAmbient(unittest.TestCase):
    T = Thresholds(55, 65, 78, 5, 180, (AmbientRule(28, Level.HIGH),))

    def test_busy_hot_inlet_raises(self):
        self.assertEqual(Sim(thresholds=self.T).step(t(50), busy=True, inlet=28), Decision(Level.HIGH, "floor"))

    def test_not_busy_no_raise(self):
        self.assertEqual(Sim(thresholds=self.T).step(t(50), inlet=28).level, Level.OFF)

    def test_inlet_below_rule(self):
        self.assertEqual(Sim(thresholds=self.T).step(t(50), busy=True, inlet=27.9).level, Level.OFF)

    def test_ambient_blocks_step_down(self):
        sim = Sim(Level.HIGH, thresholds=self.T)
        self.assertEqual(sim.run(600, t(40), busy=True, inlet=30), Level.HIGH)


class TestHold(unittest.TestCase):
    def test_hold_raises(self):
        self.assertEqual(Sim().step(t(30), hold=Level.MEDIUM), Decision(Level.MEDIUM, "floor"))

    def test_hold_blocks_step_down(self):
        self.assertEqual(Sim(Level.MEDIUM).run(1000, t(30), hold=Level.MEDIUM), Level.MEDIUM)

    def test_emergency_beats_hold(self):
        self.assertEqual(Sim(Level.MEDIUM).step(t(80), hold=Level.MEDIUM).level, Level.MAX)

    def test_step_up_above_hold(self):
        self.assertEqual(Sim(Level.MEDIUM).step(t(78), hold=Level.MEDIUM), Decision(Level.HIGH, "step-up"))


class TestOneCard(unittest.TestCase):
    def test_only_card1(self):
        self.assertEqual(Controller.hotter_c([card(1, 75)], 4.0), 79)
        self.assertEqual(Sim().step([card(1, 75)]).level, Level.HIGH)

    def test_only_card0(self):
        self.assertEqual(Controller.hotter_c([card(0, 75)], 4.0), 75)
        self.assertEqual(Sim().step([card(0, 75)]).level, Level.MEDIUM)

    def test_none(self):
        self.assertIsNone(Controller.hotter_c([], 4.0))


class TestBlind(unittest.TestCase):
    def test_idle_power_holds(self):
        self.assertEqual(Sim(Level.LOW).step([], power=238), Decision(Level.LOW, "blind-hold"))

    def test_load_after_30s(self):
        sim = Sim(Level.LOW)
        sim.run(25, [], power=300)
        sim.at(4, [], power=300)  # 29 s
        self.assertEqual(sim.level, Level.LOW)
        self.assertEqual(sim.at(1, [], power=300), Decision(Level.MEDIUM, "blind-load"))  # 30 s
        self.assertEqual(sim.at(5, [], power=238), Decision(Level.LOW, "blind-hold"))

    def test_load_never_lowers_held(self):
        sim = Sim(Level.HIGH)
        self.assertEqual(sim.run(60, [], power=300), Level.HIGH)

    def test_unknown_power_is_no_load(self):
        self.assertEqual(Sim(Level.LOW).run(120, [], power=None), Level.LOW)

    def test_hold_applies_while_blind(self):
        self.assertEqual(Sim(Level.LOW).step([], power=238, hold=Level.MEDIUM).level, Level.MEDIUM)

    def test_cards_back_ends_blind(self):
        sim = Sim(Level.LOW)
        sim.run(60, [], power=300)
        self.assertEqual(sim.level, Level.MEDIUM)
        self.assertEqual(sim.at(5, t(40)).reason, "steady")  # normal rule again; Medium needs its hold time
        sim.run(180, t(40))
        self.assertLess(sim.level, Level.MEDIUM)


class TestExternalLevelChange(unittest.TestCase):
    def test_timer_restarts_when_level_changes_outside(self):
        sim = Sim(Level.HIGH)
        sim.run(170, t(72))
        sim.level = Level.MEDIUM  # e.g. a manual change or a failed set
        sim.at(5, t(72))
        sim.level = Level.HIGH
        sim.run(170, t(72))
        self.assertEqual(sim.level, Level.HIGH)  # the earlier 170 s don't count


class TestReplay(unittest.TestCase):
    @staticmethod
    def hotter(cards):
        return max(c.temp_c for c in cards)

    def test_replay_low(self):
        rows = read_fan_test_csv(DATA / "gpu-fanlow.csv")
        changes = run_control(rows, load_defaults())
        first_medium = next(ts for ts, level, _ in changes if level == Level.MEDIUM)
        first_max_ts, _, reason = next(c for c in changes if c[1] == Level.MAX)
        self.assertLess(first_medium, next(ts for ts, cards in rows if self.hotter(cards) >= 84))
        self.assertEqual(first_max_ts, next(ts for ts, cards in rows if self.hotter(cards) >= 80))
        self.assertEqual(reason, "emergency")

    def test_replay_medium(self):
        rows = read_fan_test_csv(DATA / "gpu-fanmedium.csv")
        changes = run_control(rows, load_defaults())
        self.assertEqual(max(level for _, level, _ in changes), Level.MEDIUM)
        self.assertEqual(max(self.hotter(cards) for _, cards in rows), 74)


if __name__ == "__main__":
    unittest.main()
