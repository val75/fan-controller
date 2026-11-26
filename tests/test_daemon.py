import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from fanloop.alerts import PREFIX, Alerter
from fanloop.busy import BusyDetector
from fanloop.config import Config, load_config, load_secrets
from fanloop.control import ControlConfig, Controller
from fanloop.daemon import Loop, sd_notify
from fanloop.episodes import EpisodeTracker
from fanloop.holds import Holds
from fanloop.levels import Level
from fanloop.marks import Marks
from fanloop.offset import SetResult
from fanloop.readings import CardReading, IpmiReading
from fanloop.service import Service
from fanloop.store import Store
from fanloop.thresholds import load_defaults
from fanloop.watchdog import ReviewWatchdog

ROOT = Path(__file__).parents[1]
T0 = 1_800_000_000.0


def cards(temp, busy=False, sw=False):
    power, util = (69.0, 99.0) if busy else (30.0, 0.0)
    return [CardReading(0, temp, power, util, 1100.0, busy, sw, False),
            CardReading(1, temp - 4, power, util, 1100.0, busy, False, False)]


class FakeSensors:
    def __init__(self):
        self.cards = cards(40)
        self.error = None
        self.raise_gpus = False
        self.ipmi_reading = IpmiReading(14.0, 20.0, {"Fan1": 7200}, 238.0)
        self.power_w = 238.0

    def gpus(self):
        if self.raise_gpus:
            raise RuntimeError("sensor bug")
        return list(self.cards), self.error

    def ipmi(self):  # the same power sensor as power()
        r = self.ipmi_reading
        return IpmiReading(r.inlet_c, r.exhaust_c, r.fans_rpm, self.power_w)

    def power(self):
        return self.power_w


class FakeOffset:
    def __init__(self, level=Level.OFF):
        self.level = level  # what the iDRAC really has
        self.fail_sets = 0
        self.sets = []
        self.reads = 0

    def read(self):
        self.reads += 1
        return self.level, None

    def set(self, level):
        self.sets.append(level)
        if self.fail_sets > 0:
            self.fail_sets -= 1
            return SetResult(False, self.level, 3, "racadm: ERROR")
        self.level = level
        return SetResult(True, level, 1, None)


class FakeSender:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


class Clock:
    def __init__(self):
        self.mono = 1000.0
        self.wall = T0

    def advance(self, s):
        self.mono += s
        self.wall += s


class LoopTest(unittest.TestCase):
    def make(self, start_level=Level.OFF, sensors=None, offset=None, llama=None, store=None, **cfg):
        if not hasattr(self, "clock"):
            self.clock = Clock()
        self.store = store or Store(":memory:", load_defaults())
        self.sender = FakeSender()
        self.alerter = Alerter(self.sender, self.store)
        self.alerter.start()
        self.addCleanup(self.alerter.stop)
        self.sensors = sensors or FakeSensors()
        self.offset = offset or FakeOffset(start_level)
        self.cfg = Config(**{"dry_run": False, **cfg})
        marks, holds, watchdog = Marks(self.store), Holds(self.store), ReviewWatchdog(self.store)
        self.service = Service(self.store, self.alerter, marks, holds, watchdog, dry_run=self.cfg.dry_run)
        self.loop = Loop(self.cfg, self.service, self.store, self.sensors, self.offset,
                         Controller(load_defaults(), ControlConfig()), BusyDetector(), EpisodeTracker(),
                         self.alerter, marks, holds, watchdog,
                         mono=lambda: self.clock.mono, wall=lambda: self.clock.wall, llama=llama or (lambda: True))
        self.loop.startup()
        return self.loop

    def run_for(self, seconds, step=5.0):
        end = self.clock.mono + seconds
        while self.clock.mono < end:
            self.loop.step()
            self.clock.advance(step)

    def messages(self):
        self.assertTrue(self.alerter.drain(5))
        return [m[len(PREFIX):] for m in self.sender.sent]

    def events(self, kind):
        return [e["detail"] for e in self.store.events(0, kinds=[kind], limit=5000)]


STORE_METHODS = ("add_event", "add_sample", "add_episode", "kv_get", "kv_set", "current", "prune",
                 "marks_open", "mark_close", "mark_get")


def break_store(store, names=STORE_METHODS):
    import sqlite3

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("database or disk is full")
    for name in names:
        setattr(store, name, fail)
    return lambda: [delattr(store, name) for name in names]


class TestStoreFailure(LoopTest):
    def test_control_and_alerts_go_on_when_the_store_fails(self):
        self.make(start_level=Level.LOW)
        self.messages()
        repair = break_store(self.store)
        self.sensors.cards = cards(85, busy=True, sw=True)
        self.run_for(60)
        self.assertEqual(self.offset.sets[:1], [Level.MAX])
        status = self.service.status()
        self.assertFalse(status["health"]["store_ok"])
        self.assertEqual(status["level"], "max")
        msgs = self.messages()
        self.assertTrue(any(m.startswith("Emergency") for m in msgs), msgs)
        self.assertTrue(any(m.startswith("The loop can't use its database") for m in msgs), msgs)
        repair()
        self.run_for(10)
        self.assertIn("The loop's database works again.", self.messages())
        self.assertTrue(self.service.status()["health"]["store_ok"])


class TestNoFalseAllClear(LoopTest):
    def test_a_second_slowdown_soon_after_a_recovery_is_reported(self):
        # The reviewer's probe: throttling, recovery, throttling again 7 minutes later.
        self.make()
        self.messages()
        self.sensors.cards = cards(76, busy=True, sw=True)
        self.run_for(30)
        self.sensors.cards = cards(60, busy=True)
        self.run_for(310)
        self.sensors.cards = cards(76, busy=True, sw=True)
        self.run_for(600)
        msgs = self.messages()
        slowdowns = [m for m in msgs if m.startswith("Thermal slowdown")]
        self.assertEqual(len(slowdowns), 2, msgs)
        self.assertIn("ongoing since", slowdowns[1])
        self.assertFalse(msgs[-1].startswith("No thermal slowdown"), msgs)


class TestRestartMidIncident(LoopTest):
    """Incidents survive a restart in the store; what closes them must not depend on
    memory that didn't survive, or that kind's next real alert is swallowed."""

    def restart(self, start_level):
        self.messages()  # let the old alerter finish
        self.make(start_level=start_level, store=self.store)
        self.messages()

    def test_slowdown_open_across_a_restart_closes_and_the_next_one_alerts(self):
        self.make()
        self.sensors.cards = cards(76, busy=True, sw=True)
        self.run_for(20)
        self.restart(Level.MAX)
        self.sensors.cards = cards(60, busy=True)
        self.run_for(310)
        self.assertIn("No thermal slowdown for 5 minutes.", self.messages())
        self.run_for(1800)
        self.sensors.cards = cards(76, busy=True, sw=True)
        self.run_for(10)
        self.assertEqual(sum(m.startswith("Thermal slowdown") for m in self.messages()), 1)

    def test_emergency_open_across_a_restart_closes_when_not_at_max(self):
        self.make()
        self.sensors.cards = cards(82, busy=True)
        self.run_for(10)
        self.restart(Level.HIGH)
        self.sensors.cards = cards(60, busy=True)
        self.run_for(10)
        self.assertTrue(any(m.startswith("Emergency over") for m in self.messages()))
        self.run_for(1800)
        self.sensors.cards = cards(82, busy=True)
        self.run_for(10)
        self.assertEqual(sum(m.startswith("Emergency:") for m in self.messages()), 1)

    def test_emergency_reached_while_already_at_max_alerts(self):
        self.make(start_level=Level.MAX)
        self.messages()
        self.sensors.cards = cards(82, busy=True)
        self.run_for(10)
        self.assertEqual(self.offset.sets, [])
        self.assertTrue(any(m.startswith("Emergency:") for m in self.messages()))

    def test_emergency_with_a_failing_set_is_not_called_over(self):
        self.make(start_level=Level.HIGH)
        self.messages()
        self.offset.fail_sets = 100
        self.sensors.cards = cards(82, busy=True)
        self.run_for(30)
        msgs = self.messages()
        self.assertTrue(any(m.startswith("Emergency:") for m in msgs))
        self.assertFalse(any(m.startswith("Emergency over") for m in msgs), msgs)

    def test_max_not_enough_open_across_a_restart_closes_when_not_at_max(self):
        self.make()
        self.alerter.open_incident("max-not-enough", "At Max and still rising", self.clock.wall)
        self.restart(Level.HIGH)
        self.sensors.cards = cards(60, busy=True)
        self.run_for(10)
        self.assertIn("Left Max: the cards are cooling.", self.messages())


class TestStartup(LoopTest):
    def test_one_restart_alert(self):
        self.make()
        self.assertEqual(self.messages(), ["Fan loop started: offset off, dry-run off, thresholds v1"])

    def test_restart_at_max_steps_down_by_the_rule(self):
        self.make(start_level=Level.MAX)
        self.sensors.cards = cards(70, busy=True)
        self.run_for(175)
        self.assertEqual(self.offset.sets, [])
        self.run_for(10)
        self.assertEqual(self.offset.sets, [Level.HIGH])
        self.run_for(600)
        self.assertEqual(self.offset.sets, [Level.HIGH])  # busy at 70°C: High holds

    def test_unreadable_offset_sets_on_first_step(self):
        loop = self.make()
        self.offset.read = lambda: (None, "racadm: ERROR")
        loop.startup()  # as if the loop had started with racadm failing
        self.assertIsNone(loop.current_level)
        loop.step()
        self.assertEqual(self.offset.sets, [Level.OFF])
        restart_texts = [e["text"] for e in self.events("alert") if e["kind"] == "restart"]
        self.assertIn("offset unknown", restart_texts[-1])  # sent or held back by the rate limit, it's logged


class TestControl(LoopTest):
    def test_heating_steps_up_without_alerts(self):
        self.make()
        for temp, level in ((40, None), (55, Level.LOW), (65, Level.MEDIUM), (78, Level.HIGH)):
            self.sensors.cards = cards(temp)
            self.loop.step()
            self.clock.advance(5)
        self.assertEqual(self.offset.sets, [Level.LOW, Level.MEDIUM, Level.HIGH])
        self.assertEqual([e["to"] for e in self.events("level")], ["low", "medium", "high"])
        self.assertEqual(len(self.messages()), 1)  # just the restart alert

    def test_emergency_alerts_once(self):
        self.make()
        self.sensors.cards = cards(80)
        self.loop.step()
        self.clock.advance(5)
        self.loop.step()
        self.assertEqual(self.offset.sets, [Level.MAX])
        emergencies = [m for m in self.messages() if m.startswith("Emergency")]
        self.assertEqual(len(emergencies), 1)

    def test_emergency_over_when_leaving_max(self):
        self.make()
        self.sensors.cards = cards(80)
        self.loop.step()
        self.clock.advance(5)
        self.sensors.cards = cards(60)
        self.run_for(200)
        self.assertEqual(self.offset.sets[:2], [Level.MAX, Level.HIGH])
        self.assertTrue(any(m.startswith("Emergency over") for m in self.messages()))


class TestOffsetProblems(LoopTest):
    def test_failed_set_alerts_and_retries(self):
        self.make()
        self.offset.fail_sets = 1
        self.sensors.cards = cards(66)
        self.loop.step()
        self.clock.advance(5)
        self.loop.step()
        self.assertEqual(self.offset.sets, [Level.MEDIUM, Level.MEDIUM])
        self.assertEqual(self.offset.level, Level.MEDIUM)
        msgs = self.messages()
        self.assertEqual(len([m for m in msgs if m.startswith("racadm couldn't set")]), 1)
        self.assertEqual(len([m for m in msgs if m.startswith("racadm is working again")]), 1)
        self.assertEqual(len(self.events("level")), 1)  # the retry isn't a new level change

    def test_manual_change_is_alerted_and_corrected(self):
        self.make()
        self.sensors.cards = cards(66)
        self.loop.step()
        self.clock.advance(5)
        self.offset.level = Level.LOW  # someone lowered it in the iDRAC UI
        self.run_for(70)
        self.assertEqual(len(self.events("manual-change")), 1)
        self.assertEqual(self.offset.sets, [Level.MEDIUM, Level.MEDIUM])  # Low is below the rule: raised at once
        self.assertEqual(len([m for m in self.messages() if m.startswith("Fan offset changed outside")]), 1)

    def test_manual_raise_comes_down_after_hold(self):
        self.make()
        self.sensors.cards = cards(66)
        self.loop.step()
        self.clock.advance(5)
        self.offset.level = Level.HIGH
        self.run_for(70)
        self.assertEqual(self.offset.sets, [Level.MEDIUM])
        self.run_for(200)
        self.assertEqual(self.offset.sets, [Level.MEDIUM, Level.MEDIUM])

    def test_dry_run_does_not_read_for_manual_changes(self):
        self.make(dry_run=True)
        self.sensors.cards = cards(66)
        self.loop.step()
        self.offset.level = Level.HIGH
        self.run_for(200)
        self.assertEqual(self.events("manual-change"), [])
        self.assertEqual(self.offset.reads, 1)  # only at startup

    def test_dry_run_emergency_says_nothing_was_set(self):
        self.make(dry_run=True)
        self.messages()
        self.sensors.cards = cards(82, busy=True)
        self.run_for(10)
        (alert,) = [m for m in self.messages() if m.startswith("Emergency:")]
        self.assertIn("Dry-run: the fans were not changed.", alert)
        self.assertNotIn("Fans to Max", alert)


class TestNvidiaProblems(LoopTest):
    def test_alert_after_three_polls_and_recovery(self):
        self.make()
        self.sensors.cards, self.sensors.error = [], "NVIDIA-SMI has failed"
        self.run_for(10)  # 2 polls
        self.assertFalse([m for m in self.messages() if m.startswith("nvidia-smi")])
        self.run_for(20)
        self.sensors.cards, self.sensors.error = cards(40), None
        self.run_for(10)
        msgs = self.messages()
        self.assertEqual(len([m for m in msgs if m.startswith("nvidia-smi problem")]), 1)
        self.assertEqual(len([m for m in msgs if m.startswith("nvidia-smi reads both cards again")]), 1)

    def test_one_card_missing_counts(self):
        self.make()
        self.sensors.cards, self.sensors.error = cards(40)[1:], "0, [Unknown Error] ..."
        self.run_for(20)
        self.assertEqual(len([m for m in self.messages() if m.startswith("nvidia-smi problem")]), 1)

    def test_blind_with_power_goes_medium(self):
        self.make()
        self.sensors.cards, self.sensors.error = [], "NVIDIA-SMI has failed"
        self.sensors.power_w = 300.0
        self.run_for(25)
        self.assertEqual(self.offset.sets, [])
        self.run_for(10)
        self.assertEqual(self.offset.sets, [Level.MEDIUM])


class TestMaxNotEnough(LoopTest):
    def test_rising_at_max_alerts_once(self):
        self.make(start_level=Level.MAX)
        temp = 81.0
        end = self.clock.mono + 200
        while self.clock.mono < end:
            self.sensors.cards = cards(temp, busy=True)
            self.loop.step()
            self.clock.advance(5)
            temp += 0.1  # +2.4°C per 2 min
        self.assertEqual(len([m for m in self.messages() if m.startswith("At Max and still rising")]), 1)

    def test_steady_at_max_no_alert(self):
        self.make(start_level=Level.MAX)
        self.sensors.cards = cards(82, busy=True)
        self.run_for(300)
        self.assertFalse([m for m in self.messages() if m.startswith("At Max")])


class TestRecording(LoopTest):
    def test_episode_and_samples(self):
        self.make()
        self.sensors.cards = cards(60, busy=True)
        self.run_for(60)
        self.sensors.cards = cards(45)
        self.run_for(70)
        episodes = self.store.episodes()
        self.assertEqual(len(episodes), 1)
        self.assertGreaterEqual(episodes[0].duration_s, 60)
        steps = 26  # 130 s at 5 s
        self.assertEqual(len(self.store.samples(T0 - 1, T0 + 200, max_points=1000)), steps)

    def test_status_is_published(self):
        self.make()
        self.loop.step()
        status = self.service.status(T0)
        self.assertEqual(status["level"], "off")
        self.assertFalse(status["blind"])
        self.assertTrue(status["health"]["nvidia_ok"])
        self.assertEqual(status["inlet_c"], 14.0)


class TestHolds(LoopTest):
    def test_timed_hold_end_is_announced(self):
        self.make()
        self.service.hold_set(Level.MEDIUM, T0 + 30, T0)
        self.run_for(20)
        self.assertEqual(self.offset.sets, [Level.MEDIUM])
        self.run_for(20)
        self.assertEqual(self.messages().count("Hold ended (end time reached)"), 1)


class TestWatchdogInLoop(LoopTest):
    def test_watchdog_message_goes_out(self):
        self.make()
        self.clock.advance(13 * 3600)
        self.loop.step()
        self.assertTrue(any(m.startswith("No completed Hermes review") for m in self.messages()))


class TestRobustness(LoopTest):
    def test_sensor_exception_is_logged_and_step_finishes(self):
        self.make()
        self.sensors.raise_gpus = True
        self.loop.step()
        self.assertTrue(self.events("loop-error"))
        self.assertEqual(len(self.store.samples(T0 - 1, T0 + 10, max_points=10)), 1)


class TestRunForever(unittest.TestCase):
    def test_runs_at_fixed_rate_and_notifies(self):
        steps = []

        class Stub(Loop):
            def __init__(self):
                self.cfg = Config(poll_s=0.02)
                self.mono = time.monotonic

            def step(self):
                steps.append(time.monotonic())

        stop = threading.Event()
        threading.Timer(0.25, stop.set).start()
        Stub().run_forever(stop)
        self.assertGreaterEqual(len(steps), 5)


class TestStepBudget(LoopTest):
    """With every command at its worst, one step must still end well inside systemd's
    WatchdogSec, or the watchdog restarts a loop that is only waiting on a slow BMC."""

    def make_with(self, behave):
        from fanloop.daemon import build_io
        from fanloop.sensors import LLAMA_TIMEOUT_S
        test = self

        class Runner:
            def run(self, argv, timeout_s):
                return behave(test.clock, timeout_s)

            def stuck_count(self):
                return 0

        sensors, offset = build_io(Config(dry_run=False), Runner(), lambda: self.clock.mono)
        self.make(sensors=sensors, offset=offset,
                  llama=lambda: self.clock.advance(LLAMA_TIMEOUT_S) or False)

    def first_step_takes(self):
        start = self.clock.mono
        self.loop.step()  # the first step: offset unknown, so it sets; IPMI and llama are due
        return self.clock.mono - start

    def test_every_command_hangs(self):
        from fanloop.commands import KILL_GRACE_S, CmdResult
        from fanloop.daemon import worst_step_s

        def hang(clock, timeout_s):
            clock.advance(timeout_s + KILL_GRACE_S)
            return CmdResult(False, None, "", "timed out", True, False)
        self.make_with(hang)
        self.assertLessEqual(self.first_step_takes(), worst_step_s(self.cfg))

    def test_every_command_is_slow_but_answers(self):
        from fanloop.commands import CmdResult
        from fanloop.daemon import worst_step_s

        def slow(clock, timeout_s):
            clock.advance(timeout_s * 0.99)
            return CmdResult(True, 0, "nothing useful", "", False, False)
        self.make_with(slow)
        self.assertLessEqual(self.first_step_takes(), worst_step_s(self.cfg))

    def test_the_budget_fits_the_unit_watchdog(self):
        from fanloop.daemon import WATCHDOG_SEC, worst_step_s
        self.assertLessEqual(worst_step_s(Config()) + 30, WATCHDOG_SEC)


class TestMain(unittest.TestCase):
    """main() as systemd runs it, with a fake Telegram and fake commands."""

    def setUp(self):
        from tests.fakes import FakeRunner
        self.dir = tempfile.mkdtemp(prefix="flm")
        self.addCleanup(shutil.rmtree, self.dir, True)
        secrets = os.path.join(self.dir, "secrets")
        os.mkdir(secrets)
        for name, value in (("telegram_token", "1:x"), ("telegram_chat_id", "42"), ("write_token", "t" * 20)):
            Path(secrets, name).write_text(value)
        self.blocker = socket.socket()
        self.blocker.bind(("127.0.0.1", 0))
        self.addCleanup(self.blocker.close)
        self.port = self.blocker.getsockname()[1]
        self.settings = {"db_path": os.path.join(self.dir, "fan.db"), "listen_host": "127.0.0.1",
                         "listen_port": self.port, "socket_path": os.path.join(self.dir, "ctl.sock"),
                         "socket_group": "", "secrets_dir": secrets, "dry_run": True, "poll_s": 0.05,
                         "llama_health_url": "http://127.0.0.1:9/health"}
        self.sender = FakeSender()
        self.runner = FakeRunner()

    def run_main(self, seconds=0.5, **overrides):
        from fanloop.daemon import main
        path = os.path.join(self.dir, "config.json")
        Path(path).write_text(json.dumps({**self.settings, **overrides}))
        stop = threading.Event()
        threading.Timer(seconds, stop.set).start()
        return main(["--config", path], sender=self.sender, runner=self.runner, stop=stop)

    def texts(self):
        return [t[len(PREFIX):] for t in self.sender.sent]

    def test_control_runs_when_the_api_cannot_bind(self):
        self.blocker.listen()  # the port is taken
        self.assertEqual(self.run_main(), 0)
        import sqlite3
        conn = sqlite3.connect(self.settings["db_path"])
        self.addCleanup(conn.close)
        self.assertGreater(conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 3)
        texts = self.texts()
        self.assertTrue(any(t.startswith("Fan loop started") for t in texts), texts)
        self.assertTrue(any(t.startswith("The loop's API couldn't start") for t in texts), texts)

    def test_a_startup_failure_is_reported_once_not_on_every_restart(self):
        self.blocker.close()
        bad_db = os.path.join(self.dir, "no-such-dir", "fan.db")
        self.assertEqual(self.run_main(db_path=bad_db), 1)
        self.assertEqual(self.run_main(db_path=bad_db), 1)  # systemd restarting it
        texts = self.texts()
        self.assertEqual(len(texts), 1, texts)
        self.assertTrue(texts[0].startswith("The fan loop can't start"), texts)

    def test_the_api_starts_when_the_port_frees(self):
        from fanloop.daemon import ApiStarter
        attempts = []

        class Server:
            is_local = False

            def shutdown(self):
                pass

            def server_close(self):
                pass

        def make():
            attempts.append(1)
            if len(attempts) < 3:
                raise OSError(48, "Address already in use")
            return (Server(),)
        store = Store(":memory:", load_defaults())
        alerter = Alerter(self.sender, store)
        alerter.start()
        self.addCleanup(alerter.stop)
        starter = ApiStarter(make, serve=lambda servers: None, alerter=alerter, wall=time.time)
        self.assertFalse(starter.try_start())
        stop = threading.Event()
        starter.keep_trying(stop, retry_s=0.01)
        self.assertEqual(len(attempts), 3)
        self.assertTrue(alerter.drain(5))
        texts = self.texts()
        self.assertEqual(len(texts), 2, texts)
        self.assertTrue(texts[0].startswith("The loop's API couldn't start"))
        self.assertEqual(texts[1], "The loop's API is up again.")


class TestConfig(unittest.TestCase):
    def test_example_config(self):
        cfg = load_config(ROOT / "deploy" / "config.example.json")
        self.assertTrue(cfg.dry_run)
        self.assertEqual((cfg.listen_port, cfg.poll_s, cfg.backup_target), (8780, 5.0, ""))

    def test_unknown_key(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"dry_run": True, "fan_speed": 3}, f)
        self.addCleanup(os.unlink, f.name)
        with self.assertRaises(ValueError):
            load_config(f.name)

    def test_timezone_default_and_unknown(self):
        self.assertEqual(Config().timezone, "EET")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"timezone": "Mars/Olympus"}, f)
        self.addCleanup(os.unlink, f.name)
        with self.assertRaises(ValueError):
            load_config(f.name)

    def test_wrong_type(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"dry_run": "no", "listen_port": "8780"}, f)
        self.addCleanup(os.unlink, f.name)
        with self.assertRaises(ValueError):
            load_config(f.name)

    def test_secrets(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        for name, value in (("telegram_token", "123:abc\n"), ("telegram_chat_id", " 42 \n"), ("write_token", "tok\n")):
            Path(d, name).write_text(value)
        s = load_secrets(d)
        self.assertEqual((s.telegram_token, s.telegram_chat_id, s.write_token), ("123:abc", "42", "tok"))


class TestSdNotify(unittest.TestCase):
    def test_sends_datagram(self):
        d = tempfile.mkdtemp(prefix="sd")
        self.addCleanup(shutil.rmtree, d)
        path = os.path.join(d, "n")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.bind(path)
        self.addCleanup(sock.close)
        old = os.environ.get("NOTIFY_SOCKET")
        os.environ["NOTIFY_SOCKET"] = path
        try:
            sd_notify("READY=1")
        finally:
            if old is None:
                os.environ.pop("NOTIFY_SOCKET")
            else:
                os.environ["NOTIFY_SOCKET"] = old
        sock.settimeout(1)
        self.assertEqual(sock.recv(100), b"READY=1")

    def test_no_socket_is_fine(self):
        old = os.environ.pop("NOTIFY_SOCKET", None)
        try:
            sd_notify("WATCHDOG=1")
        finally:
            if old is not None:
                os.environ["NOTIFY_SOCKET"] = old


if __name__ == "__main__":
    unittest.main()
