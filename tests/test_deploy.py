"""The deploy files: the loop's systemd unit, its sudoers file, and the config check
the playbook runs before installing a rendered config."""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from fanloop.commands import ROOT_COMMAND_LINES
from fanloop.config import Config
from fanloop.daemon import WATCHDOG_SEC, worst_step_s

DEPLOY = Path(__file__).parents[1] / "deploy"


def unit_keys(text):
    keys = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith(("#", "[")):
            key, _, value = line.partition("=")
            keys.setdefault(key, []).append(value)
    return keys


class TestLoopUnit(unittest.TestCase):
    def setUp(self):
        self.keys = unit_keys((DEPLOY / "fan-controller.service").read_text())

    def test_runs_the_daemon_as_fanloop_under_systemd_notify(self):
        for key, value in (("Type", "notify"), ("NotifyAccess", "main"), ("User", "fanloop"),
                           ("Group", "fanloop"), ("SupplementaryGroups", "fanctl"),
                           ("Environment", "PYTHONPATH=/opt/fan-controller"),
                           ("ExecStart", "/usr/bin/python3 -m fanloop.daemon --config /etc/fan-controller/config.json"),
                           ("WantedBy", "multi-user.target")):
            self.assertEqual(self.keys.get(key), [value], key)

    def test_restarts_forever(self):
        self.assertEqual(self.keys["Restart"], ["always"])
        self.assertEqual(self.keys["StartLimitIntervalSec"], ["0"])

    def test_watchdog_matches_the_loop_budget(self):
        self.assertEqual(self.keys["WatchdogSec"], [str(WATCHDOG_SEC)])
        self.assertLess(worst_step_s(Config()), WATCHDOG_SEC)

    def test_runtime_directory_survives_restarts(self):
        # The startup-failure stamp lives there; without this, a crash loop messages every restart.
        self.assertEqual(self.keys["RuntimeDirectory"], ["fan-controller"])
        self.assertEqual(self.keys["RuntimeDirectoryPreserve"], ["restart"])

    def test_hardening_leaves_sudo_and_the_ipmi_device_working(self):
        self.assertNotIn("NoNewPrivileges", self.keys)  # sudo can't work with it
        self.assertNotIn("PrivateDevices", self.keys)  # racadm and ipmitool need /dev/ipmi0
        self.assertEqual(self.keys["ProtectSystem"], ["full"])
        self.assertEqual(self.keys["ProtectHome"], ["yes"])
        self.assertEqual(self.keys["LimitNOFILE"], ["4096"])

    def test_waits_for_the_clock_and_the_network(self):
        after = " ".join(self.keys["After"]).split()
        self.assertIn("time-sync.target", after)
        self.assertIn("network-online.target", after)


class TestSudoers(unittest.TestCase):
    def setUp(self):
        self.text = (DEPLOY / "sudoers-fan-controller").read_text()

    def alias(self, name):
        joined = re.sub(r"\\\n\s*", " ", self.text)
        (alias,) = re.findall(rf"^Cmnd_Alias {name} = (.+)$", joined, re.M)
        return [c.strip() for c in alias.split(",")]

    def test_exactly_the_nine_root_command_lines(self):
        reads, sets = self.alias("FANLOOP_READS"), self.alias("FANLOOP_SETS")
        self.assertEqual(sorted(reads + sets), sorted(ROOT_COMMAND_LINES))
        self.assertEqual(len(reads + sets), 9)
        self.assertTrue(all(" set " in c for c in sets) and len(sets) == 5)
        self.assertTrue(all(" set " not in c for c in reads) and len(reads) == 4)

    def test_no_wildcards_or_extra_grants(self):
        self.assertNotIn("*", self.text)
        rules = [line for line in self.text.splitlines() if line.startswith("fanloop ")]
        self.assertEqual(rules, ["fanloop ALL=(root) NOPASSWD: FANLOOP_READS, FANLOOP_SETS"])
        self.assertIn("Defaults:fanloop !requiretty", self.text)

    def test_reads_are_not_logged_but_sets_are(self):
        # Each sudo call writes three log lines; the reads run ~8,600 times a day.
        defaults = [line for line in self.text.splitlines() if line.startswith("Defaults!")]
        self.assertEqual(defaults, ["Defaults!FANLOOP_READS !syslog, !pam_session"])


class TestConfigCheck(unittest.TestCase):
    """`python3 -m fanloop.config PATH`: the playbook's `validate:` for the rendered config."""

    def check(self, data):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(data, f)
        self.addCleanup(os.unlink, f.name)
        return subprocess.run([sys.executable, "-m", "fanloop.config", f.name], capture_output=True, text=True,
                              cwd=Path(__file__).parents[1])

    def test_the_example_passes(self):
        r = subprocess.run([sys.executable, "-m", "fanloop.config", str(DEPLOY / "config.example.json")],
                           capture_output=True, text=True, cwd=Path(__file__).parents[1])
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_dry_run_as_text_is_refused(self):
        # What a template without `| bool` renders: the loop would refuse to start.
        r = self.check({"dry_run": "false"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("dry_run", r.stderr)


if __name__ == "__main__":
    unittest.main()
