import time
import unittest

from fanloop.commands import (
    IPMI_FANS,
    IPMI_POWER,
    IPMI_TEMPS,
    NVIDIA_QUERY,
    RACADM_GET,
    ROOT_COMMAND_LINES,
    CommandRunner,
    racadm_set,
)
from fanloop.levels import Level

# The 9 exact lines from spec 5.10.
SPEC_LINES = {
    "/usr/bin/racadm get system.thermalsettings.FanSpeedOffset",
    "/usr/bin/racadm set system.thermalsettings.FanSpeedOffset 255",
    "/usr/bin/racadm set system.thermalsettings.FanSpeedOffset 0",
    "/usr/bin/racadm set system.thermalsettings.FanSpeedOffset 2",
    "/usr/bin/racadm set system.thermalsettings.FanSpeedOffset 1",
    "/usr/bin/racadm set system.thermalsettings.FanSpeedOffset 3",
    "/usr/bin/ipmitool sdr type temperature",
    "/usr/bin/ipmitool sdr type fan",
    "/usr/bin/ipmitool dcmi power reading",
}


class TestCommandLines(unittest.TestCase):
    def test_root_commands_are_exactly_the_spec_lines(self):
        argvs = [RACADM_GET, IPMI_TEMPS, IPMI_FANS, IPMI_POWER] + [racadm_set(level) for level in Level]
        for argv in argvs:
            self.assertEqual(argv[:2], ("/usr/bin/sudo", "-n"))
        self.assertEqual({" ".join(argv[2:]) for argv in argvs}, SPEC_LINES)
        self.assertEqual(set(ROOT_COMMAND_LINES), SPEC_LINES)
        self.assertEqual(len(ROOT_COMMAND_LINES), 9)

    def test_racadm_set_codes(self):
        self.assertEqual(racadm_set(Level.HIGH)[-1], "1")
        self.assertEqual(racadm_set(Level.OFF)[-1], "255")

    def test_nvidia_query_uses_driver_535_field_names(self):
        self.assertIn("clocks_event_reasons.sw_thermal_slowdown", NVIDIA_QUERY[1])
        self.assertEqual(NVIDIA_QUERY[0], "/usr/bin/nvidia-smi")


class TestCommandRunner(unittest.TestCase):
    def setUp(self):
        self.leftovers = []

    def tearDown(self):
        for proc in self.leftovers:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            for pipe in (proc.stdout, proc.stderr):
                if pipe:
                    pipe.close()

    def unkillable_runner(self):
        """Simulates a process stuck in the driver: kill() has no effect."""
        def no_kill(proc):
            self.leftovers.append(proc)
        return CommandRunner(kill=no_kill)

    def test_ok(self):
        r = CommandRunner().run(("/bin/echo", "hi"), 5)
        self.assertTrue(r.ok)
        self.assertEqual(r.stdout, "hi\n")
        self.assertEqual(r.returncode, 0)

    def test_exit_code(self):
        r = CommandRunner().run(("/bin/sh", "-c", "exit 3"), 5)
        self.assertFalse(r.ok)
        self.assertEqual(r.returncode, 3)

    def test_missing_program(self):
        r = CommandRunner().run(("/nonexistent/prog",), 5)
        self.assertFalse(r.ok)
        self.assertIn("nonexistent", r.stderr)

    def test_timeout_returns_without_waiting(self):
        runner = self.unkillable_runner()
        start = time.monotonic()
        r = runner.run(("/bin/sleep", "5"), 0.2)
        self.assertLess(time.monotonic() - start, 1.0)
        self.assertTrue(r.timed_out)
        self.assertFalse(r.ok)
        self.assertEqual(runner.stuck_count(), 1)

    def test_no_second_copy_while_stuck(self):
        runner = self.unkillable_runner()
        runner.run(("/bin/sleep", "5"), 0.2)
        r = runner.run(("/bin/sleep", "5"), 0.2)
        self.assertTrue(r.skipped)
        self.assertFalse(r.ok)
        self.assertEqual(len(self.leftovers), 1)  # nothing new was started

    def test_finished_stuck_process_is_reaped(self):
        runner = self.unkillable_runner()
        runner.run(("/bin/sleep", "0.3"), 0.1)
        time.sleep(0.6)
        self.assertEqual(runner.stuck_count(), 0)
        r = runner.run(("/bin/sleep", "0.3"), 2)
        self.assertFalse(r.skipped)
        self.assertTrue(r.ok)

    def test_default_kill_really_kills(self):
        runner = CommandRunner()
        r = runner.run(("/bin/sleep", "5"), 0.2)
        self.assertTrue(r.timed_out)
        time.sleep(0.2)
        self.assertEqual(runner.stuck_count(), 0)


if __name__ == "__main__":
    unittest.main()
