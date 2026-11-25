import dataclasses
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from fanloop import backup
from fanloop.config import Config
from fanloop.store import Store
from fanloop.thresholds import Change, load_defaults
from tests.fakes import FakeRunner, fail, ok

MONDAY = datetime(2026, 11, 23, 3, 30)
TARGET = "fanbackup@docks:fan-controller/"


class FakeSender:
    def __init__(self, error=None):
        self.sent = []
        self.error = error

    def send(self, text):
        self.sent.append(text)
        if self.error:
            raise self.error


class TimeoutRecordingRunner(FakeRunner):
    def run(self, argv, timeout_s):
        self.timeouts = getattr(self, "timeouts", []) + [timeout_s]
        return super().run(argv, timeout_s)


class BackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="flb")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "fan.db")
        store = Store(self.db, load_defaults())
        store.add_version(dataclasses.replace(store.current()[1], medium=63), "user-cli", "test",
                          [Change("step_up.medium", 65, 63)], 1000.0)
        store.close()
        self.cfg = Config(db_path=self.db, backup_dir=os.path.join(self.tmp, "backup"),
                          backup_target="", backup_key="/k/docks_ed25519", backup_known_hosts="/k/known_hosts")

    def local_copy(self):
        return os.path.join(self.tmp, "backup", "fan-Mon.db")

    def rsync_argv(self):
        return ("/usr/bin/rsync", "-t", "-e",
                "ssh -i /k/docks_ed25519 -o UserKnownHostsFile=/k/known_hosts -o BatchMode=yes -o IdentitiesOnly=yes",
                self.local_copy(), TARGET)

    def test_local_copy_is_named_for_the_weekday_and_holds_the_data(self):
        self.assertEqual(MONDAY.strftime("%A"), "Monday")
        runner, sender = FakeRunner(), FakeSender()
        self.assertTrue(backup.run_backup(self.cfg, MONDAY, runner, sender))
        self.assertTrue(os.path.exists(self.local_copy()))
        copy = Store(self.local_copy(), load_defaults())
        self.addCleanup(copy.close)
        self.assertEqual(copy.current()[0], 2)
        self.assertEqual(copy.current()[1].medium, 63)

    def test_empty_target_runs_no_rsync(self):
        runner, sender = FakeRunner(), FakeSender()
        self.assertTrue(backup.run_backup(self.cfg, MONDAY, runner, sender))
        self.assertEqual(runner.calls, [])
        self.assertEqual(sender.sent, [])

    def test_rsync_argv_and_timeout(self):
        cfg = dataclasses.replace(self.cfg, backup_target=TARGET)
        runner = TimeoutRecordingRunner({self.rsync_argv(): [ok()]})
        sender = FakeSender()
        self.assertTrue(backup.run_backup(cfg, MONDAY, runner, sender))
        self.assertEqual(runner.calls, [self.rsync_argv()])
        self.assertEqual(runner.timeouts, [300])
        self.assertEqual(sender.sent, [])

    def test_rsync_failure_sends_one_message_and_returns_false(self):
        cfg = dataclasses.replace(self.cfg, backup_target=TARGET)
        runner = FakeRunner({self.rsync_argv(): [fail("Permission denied (publickey)", returncode=255)]})
        sender = FakeSender()
        self.assertFalse(backup.run_backup(cfg, MONDAY, runner, sender))
        self.assertEqual(len(sender.sent), 1)
        self.assertTrue(sender.sent[0].startswith("[t4 fans] Backup to docks failed"))
        self.assertIn("Permission denied", sender.sent[0])

    def test_missing_database_fails_without_creating_it(self):
        cfg = dataclasses.replace(self.cfg, db_path=os.path.join(self.tmp, "nothere.db"))
        sender = FakeSender()
        self.assertFalse(backup.run_backup(cfg, MONDAY, FakeRunner(), sender))
        self.assertFalse(os.path.exists(cfg.db_path))
        self.assertEqual(len(sender.sent), 1)
        self.assertIn("failed", sender.sent[0])

    def test_backup_does_not_write_to_the_loop_database(self):
        before = {p.name: p.stat().st_mtime_ns for p in Path(self.tmp).glob("fan.db*")}
        backup.run_backup(self.cfg, MONDAY, FakeRunner(), FakeSender())
        after = {p.name: p.stat().st_mtime_ns for p in Path(self.tmp).glob("fan.db*")}
        self.assertEqual(before.get("fan.db"), after.get("fan.db"))

    def test_last_weeks_copy_is_replaced(self):
        os.makedirs(os.path.dirname(self.local_copy()))
        Path(self.local_copy()).write_bytes(b"old, not a database")
        self.assertTrue(backup.run_backup(self.cfg, MONDAY, FakeRunner(), FakeSender()))
        self.assertEqual(os.listdir(os.path.dirname(self.local_copy())), ["fan-Mon.db"])
        copy = Store(self.local_copy(), load_defaults())
        self.addCleanup(copy.close)
        self.assertEqual(copy.current()[0], 2)

    def test_copy_while_the_loop_has_the_database_open(self):
        live = Store(self.db, load_defaults())
        self.addCleanup(live.close)
        live.add_version(dataclasses.replace(live.current()[1], high=76), "user-cli", "live",
                         [Change("step_up.high", 78, 76)], 2000.0)
        self.assertTrue(backup.run_backup(self.cfg, MONDAY, FakeRunner(), FakeSender()))
        live.add_version(dataclasses.replace(live.current()[1], high=75), "user-cli", "after",
                         [Change("step_up.high", 76, 75)], 3000.0)  # the loop can still write
        copy = Store(self.local_copy(), load_defaults())
        self.addCleanup(copy.close)
        self.assertEqual(copy.current()[0], 3)

    def test_the_copy_is_one_self_contained_file(self):
        import sqlite3
        backup.run_backup(self.cfg, MONDAY, FakeRunner(), FakeSender())
        conn = sqlite3.connect(self.local_copy())
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_a_telegram_failure_does_not_raise(self):
        cfg = dataclasses.replace(self.cfg, backup_target=TARGET)
        runner = FakeRunner({self.rsync_argv(): [fail()]})
        sender = FakeSender(error=OSError("network down"))
        self.assertFalse(backup.run_backup(cfg, MONDAY, runner, sender))


class UnitsTest(unittest.TestCase):
    def read(self, name):
        return (Path(__file__).parent.parent / "deploy" / name).read_text()

    def test_service_unit(self):
        text = self.read("fan-controller-backup.service")
        for line in ("Type=oneshot", "User=fanloop", "Environment=PYTHONPATH=/opt/fan-controller",
                     "ExecStart=/usr/bin/python3 -m fanloop.backup --config /etc/fan-controller/config.json"):
            self.assertIn(line + "\n", text)

    def test_timer_unit(self):
        text = self.read("fan-controller-backup.timer")
        for line in ("OnCalendar=*-*-* 03:30", "RandomizedDelaySec=10m", "Persistent=true"):
            self.assertIn(line + "\n", text)


if __name__ == "__main__":
    unittest.main()
