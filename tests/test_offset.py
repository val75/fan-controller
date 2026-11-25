import unittest

from fanloop.commands import RACADM_GET, CmdResult, racadm_set
from fanloop.levels import Level
from fanloop.offset import Offset, SetResult
from tests.fakes import FakeRunner, fail, ok

MODIFIED = "Object value modified successfully\n"


def get(level_text):
    return ok(f"[Key=system.Embedded.1#ThermalSettings.1]\nFanSpeedOffset={level_text}\n")


class TestOffsetRead(unittest.TestCase):
    def test_read(self):
        runner = FakeRunner({RACADM_GET: [get("Medium Fan Speed")]})
        self.assertEqual(Offset(runner, dry_run=False).read(), (Level.MEDIUM, None))

    def test_read_error_text(self):
        runner = FakeRunner({RACADM_GET: [ok("ERROR: Insufficient privilege level - You do not have\n")]})
        level, error = Offset(runner, dry_run=False).read()
        self.assertIsNone(level)
        self.assertIn("Insufficient privilege", error)

    def test_read_command_fails(self):
        runner = FakeRunner({RACADM_GET: [fail("sudo: a password is required")]})
        level, error = Offset(runner, dry_run=False).read()
        self.assertIsNone(level)
        self.assertIn("password is required", error)


class TestOffsetSet(unittest.TestCase):
    def test_set_and_verify(self):
        runner = FakeRunner({racadm_set(Level.MEDIUM): [ok(MODIFIED)], RACADM_GET: [get("Medium Fan Speed")]})
        result = Offset(runner, dry_run=False).set(Level.MEDIUM)
        self.assertEqual(result, SetResult(True, Level.MEDIUM, 1, None))
        self.assertEqual(runner.calls, [racadm_set(Level.MEDIUM), RACADM_GET])

    def test_read_back_wrong_level_retries_then_fails(self):
        runner = FakeRunner({racadm_set(Level.MEDIUM): [ok(MODIFIED)], RACADM_GET: [get("Low Fan Speed")]})
        result = Offset(runner, dry_run=False, retries=3).set(Level.MEDIUM)
        self.assertFalse(result.ok)
        self.assertEqual(result.attempts, 3)
        self.assertEqual(result.level, Level.LOW)
        self.assertTrue(result.error)
        self.assertEqual(runner.calls.count(racadm_set(Level.MEDIUM)), 3)

    def test_error_text_with_exit_0_is_not_success(self):
        runner = FakeRunner({racadm_set(Level.MEDIUM): [ok("ERROR: Unable to perform the requested operation\n")],
                             RACADM_GET: [get("Off")]})
        result = Offset(runner, dry_run=False).set(Level.MEDIUM)
        self.assertFalse(result.ok)
        self.assertIn("Unable to perform", result.error)

    def test_second_attempt_succeeds(self):
        runner = FakeRunner({racadm_set(Level.HIGH): [ok(MODIFIED)],
                             RACADM_GET: [get("Off"), get("High Fan Speed")]})
        self.assertEqual(Offset(runner, dry_run=False).set(Level.HIGH), SetResult(True, Level.HIGH, 2, None))

    def test_a_hung_set_is_not_retried_or_read_back(self):
        hung = CmdResult(False, None, "", "timed out after 10 s", True, False)
        runner = FakeRunner({racadm_set(Level.MAX): [hung], RACADM_GET: [get("Off")]})
        result = Offset(runner, dry_run=False).set(Level.MAX)
        self.assertFalse(result.ok)
        self.assertEqual(runner.calls, [racadm_set(Level.MAX)])
        self.assertIn("timed out", result.error)

    def test_a_hung_read_back_ends_the_set(self):
        hung = CmdResult(False, None, "", "timed out after 10 s", True, False)
        runner = FakeRunner({racadm_set(Level.MAX): [ok(MODIFIED)], RACADM_GET: [hung]})
        result = Offset(runner, dry_run=False).set(Level.MAX)
        self.assertFalse(result.ok)
        self.assertEqual(runner.calls, [racadm_set(Level.MAX), RACADM_GET])

    def test_slow_racadm_stays_inside_the_time_budget(self):
        clock = [0.0]
        timeouts = []

        class SlowRunner(FakeRunner):
            def run(self, argv, timeout_s):
                timeouts.append(timeout_s)
                clock[0] += min(9.0, timeout_s)  # slow, but never quite timing out
                return super().run(argv, timeout_s)

        runner = SlowRunner({racadm_set(Level.HIGH): [ok(MODIFIED)], RACADM_GET: [get("Off")]})
        offset = Offset(runner, dry_run=False, retries=3, timeout_s=10, budget_s=20, clock=lambda: clock[0])
        result = offset.set(Level.HIGH)
        self.assertFalse(result.ok)
        self.assertLessEqual(clock[0], 20.0)
        self.assertTrue(all(t > 0 for t in timeouts))

    def test_dry_run_runs_nothing(self):
        runner = FakeRunner({})
        self.assertEqual(Offset(runner, dry_run=True).set(Level.MEDIUM), SetResult(True, Level.MEDIUM, 0, None))
        self.assertEqual(runner.calls, [])


if __name__ == "__main__":
    unittest.main()
