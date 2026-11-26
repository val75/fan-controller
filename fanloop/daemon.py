"""The fan loop (spec 5): every poll, read the GPUs, decide, set the iDRAC fan
offset, record, and alert. Run as `python3 -m fanloop.daemon --config PATH`."""
import argparse
import collections
import dataclasses
import os
import signal
import socket
import sys
import threading
import time
from typing import Callable, Deque, Dict, List, Optional, Tuple

from fanloop.control import ControlInputs
from fanloop.levels import Level
from fanloop.readings import CardReading, IpmiReading

_SLOWDOWN_CLEAR_S = 300.0
_MAX_WINDOW_S = 120.0
_MAX_RISE_C = 2.0

# systemd's WatchdogSec for the loop's unit. It must exceed the longest a poll can
# take with every command at its worst (worst_step_s), with room to spare.
WATCHDOG_SEC = 90


def worst_step_s(cfg) -> float:
    """The longest one poll can take: nvidia-smi, one IPMI read and one racadm set,
    each up to its limit plus the SIGTERM grace, and the llama-server health check."""
    from fanloop.commands import KILL_GRACE_S
    from fanloop.sensors import LLAMA_TIMEOUT_S
    racadm = max(cfg.racadm_set_budget_s, cfg.racadm_timeout_s)
    return (cfg.nvidia_timeout_s + cfg.ipmi_timeout_s + racadm + 3 * KILL_GRACE_S + LLAMA_TIMEOUT_S)


def build_io(cfg, runner, clock: Callable[[], float]):
    """The sensors and the offset, with the config's time limits."""
    from fanloop.offset import Offset
    from fanloop.sensors import Sensors
    return (Sensors(runner, nvidia_timeout_s=cfg.nvidia_timeout_s, ipmi_timeout_s=cfg.ipmi_timeout_s, clock=clock),
            Offset(runner, dry_run=cfg.dry_run, timeout_s=cfg.racadm_timeout_s,
                   budget_s=cfg.racadm_set_budget_s, clock=clock))


def sd_notify(msg: str) -> None:
    """Tell systemd something (READY=1, WATCHDOG=1). Does nothing outside systemd."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(msg.encode(), addr)
    except OSError:
        pass


def _name(level: Optional[Level]) -> str:
    return level.name.lower() if level is not None else "unknown"


class Loop:
    def __init__(self, cfg, service, store, sensors, offset, controller, busy, tracker, alerter,
                 marks, holds, watchdog, mono: Callable[[], float], wall: Callable[[], float],
                 llama: Callable[[], Optional[bool]]):
        self.cfg, self.service, self.store = cfg, service, store
        self.sensors, self.offset, self.controller = sensors, offset, controller
        self.busy, self.tracker, self.alerter = busy, tracker, alerter
        self.marks, self.holds, self.watchdog = marks, holds, watchdog
        self.mono, self.wall, self.llama = mono, wall, llama

        self.current_level: Optional[Level] = None
        self._set_failed = False
        self._logged_target: Optional[Level] = None
        self._last_reason = ""
        self._nvidia_bad = 0
        self._ipmi: Optional[IpmiReading] = None
        self._next_ipmi = self._next_offset_read = self._next_watchdog = self._next_prune = 0.0
        self._slowdown_seen: Optional[float] = None
        self._max_since: Optional[float] = None
        self._hot_history: Deque[Tuple[float, float]] = collections.deque()
        self._health = {"nvidia_ok": False, "ipmi_ok": False, "racadm_ok": False, "store_ok": True}
        # filled each step
        self._cards: List[CardReading] = []
        self._busy = False
        self._hold = None
        self._version = 1

    def _event(self, wall: float, kind: str, detail: dict) -> None:
        """Recording is best-effort: a failing database must never stop control or alerts.
        The sample written every step is what reports the database's health."""
        try:
            self.store.add_event(wall, kind, detail)
        except Exception:
            pass

    # -- startup -------------------------------------------------------------------------------

    def startup(self) -> None:
        mono, wall = self.mono(), self.wall()
        version, thresholds = self.store.current()
        self.controller.thresholds = thresholds
        self.watchdog.ensure_started(wall)
        level, error = self.offset.read()
        self.current_level = level
        self._health["racadm_ok"] = level is not None
        if level is None:
            self._event(wall, "racadm-read-error", {"error": (error or "")[:300]})
        self._next_offset_read = mono + self.cfg.offset_read_every_s
        self._slowdown_seen = mono  # a slowdown incident left open by an earlier run closes after 5 quiet minutes
        self._event(wall, "loop-start", {"offset": _name(level), "dry_run": self.cfg.dry_run,
                                                  "version": version})
        self.alerter.alert("restart", f"Fan loop started: offset {_name(level)}, "
                                      f"dry-run {'on' if self.cfg.dry_run else 'off'}, thresholds v{version}", wall)

    # -- one poll ---------------------------------------------------------------------------------

    def step(self) -> None:
        mono, wall = self.mono(), self.wall()
        for name, part in (("gpus", self._gpus), ("ipmi", self._read_ipmi), ("busy", self._update_busy),
                           ("hold", self._update_hold), ("control", self._control),
                           ("slowdown", self._check_slowdown), ("max", self._check_max),
                           ("alerts", lambda mono, wall: self.alerter.tick(wall)),
                           ("episodes", self._record_episode), ("sample", self._record_sample),
                           ("reviews", self._reviews), ("prune", self._prune), ("status", self._publish)):
            try:
                part(mono, wall)
            except Exception as e:
                self._event(wall, "loop-error", {"part": name, "error": repr(e)[:300]})
                self.alerter.alert("loop-error", f"Fan loop error in {name}: {e!r}"[:400], wall)

    def _gpus(self, mono, wall):
        self._cards = []
        cards, error = self.sensors.gpus()
        self._cards = cards
        if error or len(cards) < 2:
            self._nvidia_bad += 1
            self._health["nvidia_ok"] = False
            if self._nvidia_bad >= self.cfg.nvidia_fail_polls:
                held = _name(self.current_level)
                self.alerter.open_incident(
                    "nvidia", f"nvidia-smi problem for {self._nvidia_bad} polls: {(error or 'cards missing')[:300]}. "
                              f"Reading {len(cards)} of 2 cards; holding {held}, system power decides if both are "
                              "unreadable.", wall)
        else:
            self._nvidia_bad = 0
            self._health["nvidia_ok"] = True
            self.alerter.close_incident("nvidia", "nvidia-smi reads both cards again.", wall)

    def _read_ipmi(self, mono, wall):
        if mono >= self._next_ipmi:
            self._next_ipmi = mono + self.cfg.ipmi_every_s
            self._ipmi = self.sensors.ipmi()
            self._health["ipmi_ok"] = self._ipmi.inlet_c is not None
        elif not self._cards:  # blind: system power is the only load signal
            power = self.sensors.power()
            base = self._ipmi or IpmiReading(None, None, {}, None)
            self._ipmi = dataclasses.replace(base, power_w=power)

    def _update_busy(self, mono, wall):
        self._busy = self.busy.update(mono, self._cards)

    def _update_hold(self, mono, wall):
        if self.holds.expire(wall) is not None:
            self.alerter.notify("Hold ended (end time reached)", wall)
        self._hold = self.holds.current(wall)
        self._version, self.controller.thresholds = self.service.thresholds()

    def _control(self, mono, wall):
        inputs = ControlInputs(now=mono, cards=self._cards, busy=self._busy,
                               inlet_c=self._ipmi.inlet_c if self._ipmi else None,
                               system_power_w=self._ipmi.power_w if self._ipmi else None,
                               hold_min=self._hold.level if self._hold else None)
        current = self.current_level
        decision = self.controller.decide(inputs, current if current is not None else Level.OFF)
        self._last_reason = decision.reason
        target = decision.level

        if decision.reason == "emergency":  # also when the fans already are at Max (a hold, by hand)
            hotter = self.controller.hotter_c(self._cards, self.controller.cfg.single_card1_offset_c)
            flags = any(c.sw_thermal or c.hw_thermal for c in self._cards)
            why = "thermal slowdown" if flags else f"hotter card at {hotter:g}°C"
            action = "Dry-run: the fans were not changed." if self.cfg.dry_run else "Fans to Max."
            self.alerter.open_incident("emergency", f"Emergency: {why}. {action}", wall)  # once per incident

        if current is None or target != current or self._set_failed:
            first_try = target != self._logged_target or not self._set_failed
            # The set comes first: nothing about recording may delay or prevent it.
            result = self.offset.set(target)
            if first_try:
                self._event(wall, "level", {"from": _name(current), "to": _name(target), "reason": decision.reason})
                self._logged_target = target
            if result.ok:
                self.current_level = target
                self._set_failed = False
                self._health["racadm_ok"] = True
                self.alerter.close_incident("racadm", f"racadm is working again: offset {_name(target)}.", wall)
                self._next_offset_read = mono + self.cfg.offset_read_every_s
            else:
                self._set_failed = True
                self._health["racadm_ok"] = False
                if result.level is not None:
                    self.current_level = result.level
                self._event(wall, "racadm-error", {"target": _name(target), "error": (result.error or "")[:300]})
                self.alerter.open_incident("racadm", f"racadm couldn't set {_name(target)} after "
                                                     f"{result.attempts} tries: {(result.error or '')[:200]}. "
                                                     "Retrying every poll.", wall)
        elif not self.cfg.dry_run and mono >= self._next_offset_read:
            self._next_offset_read = mono + self.cfg.offset_read_every_s
            level, error = self.offset.read()
            self._health["racadm_ok"] = level is not None
            if level is None:
                self._event(wall, "racadm-read-error", {"error": (error or "")[:300]})
            elif level != current:
                self._event(wall, "manual-change", {"loop": _name(current), "found": _name(level)})
                self.alerter.alert("manual-change", f"Fan offset changed outside the loop: {_name(level)} "
                                                    f"(the loop had {_name(current)}). Taking control back. "
                                                    "Use fanctl hold to keep the fans higher.", wall)
                self.current_level = level

        # Closed by the state now, not by a transition this process saw: an incident
        # left open by an earlier run closes too (a no-op when none is open). Over means
        # the rule no longer asks for Max and the fans have left it, not a failed set.
        if (decision.reason != "emergency" and self.current_level is not None
                and self.current_level != Level.MAX):
            self.alerter.close_incident("emergency", f"Emergency over: fans down from Max to "
                                                     f"{self.current_level.name.capitalize()}.", wall)

    def _check_slowdown(self, mono, wall):
        slow = [c for c in self._cards if c.sw_thermal or c.hw_thermal]
        if slow:
            c = slow[0]
            self.alerter.open_incident("slowdown", f"Thermal slowdown on card {c.index} at {c.temp_c:g}°C.", wall)
            self._slowdown_seen = mono
        elif self._slowdown_seen is not None and mono - self._slowdown_seen >= _SLOWDOWN_CLEAR_S:
            self._slowdown_seen = None
            self.alerter.close_incident("slowdown", "No thermal slowdown for 5 minutes.", wall)

    def _check_max(self, mono, wall):
        hotter = self.controller.hotter_c(self._cards, self.controller.cfg.single_card1_offset_c)
        if self.current_level != Level.MAX:
            self._max_since = None
            self._hot_history.clear()
            self.alerter.close_incident("max-not-enough", "Left Max: the cards are cooling.", wall)
            return
        if self._max_since is None:
            self._max_since = mono
        if hotter is None:
            return
        self._hot_history.append((mono, hotter))
        while self._hot_history and self._hot_history[0][0] < mono - _MAX_WINDOW_S - 30:
            self._hot_history.popleft()
        if mono - self._max_since < _MAX_WINDOW_S:
            return
        earlier = [h for t, h in self._hot_history if t <= mono - _MAX_WINDOW_S]
        if earlier and hotter >= earlier[-1] + _MAX_RISE_C:
            self.alerter.open_incident(
                "max-not-enough", f"At Max and still rising: hotter card {hotter:g}°C, "
                                  f"+{hotter - earlier[-1]:.1f}°C in 2 min. The fans can't keep up; "
                                  "the load is your call.", wall)

    def _record_episode(self, mono, wall):
        ep = self.tracker.update(wall, self._busy, self._cards, self._ipmi.inlet_c if self._ipmi else None,
                                 self.current_level if self.current_level is not None else Level.OFF,
                                 self._version, self.marks.active(wall), self._hold.level if self._hold else None)
        if ep is not None:
            self.store.add_episode(ep)
            self._event(wall, "episode", {"start": ep.start, "duration_s": ep.duration_s,
                                                   "peak_c": max(ep.peak_c.values(), default=None)})

    def _record_sample(self, mono, wall):
        try:
            self.store.add_sample(wall, self._cards, self._ipmi, self.current_level, self._busy)
        except Exception as e:
            self._health["store_ok"] = False
            self.alerter.open_incident(
                "store", f"The loop can't use its database: {e!r}"[:300] + ". Fan control goes on with the "
                         "last thresholds it read; nothing is being recorded.", wall)
            return
        if not self._health["store_ok"]:
            self._health["store_ok"] = True
            self.alerter.close_incident("store", "The loop's database works again.", wall)

    def _reviews(self, mono, wall):
        self.marks.expire(wall)
        if mono >= self._next_watchdog:
            self._next_watchdog = mono + self.cfg.watchdog_every_s
            for msg in self.watchdog.check(wall, self.llama()):
                self.alerter.notify(msg.text, wall)
            reminder = self.holds.reminder(wall)
            if reminder is not None:
                self.alerter.notify(reminder.text, wall)

    def _prune(self, mono, wall):
        if mono >= self._next_prune:
            self._next_prune = mono + self.cfg.prune_every_s
            self.store.prune(wall)

    def _publish(self, mono, wall):
        ipmi = self._ipmi
        self.service.publish({
            "at": wall,
            "level": _name(self.current_level) if self.current_level is not None else None,
            "reason": self._last_reason,
            "busy": self._busy,
            "blind": not self._cards,
            "cards": [dataclasses.asdict(c) for c in self._cards],
            "inlet_c": ipmi.inlet_c if ipmi else None,
            "exhaust_c": ipmi.exhaust_c if ipmi else None,
            "system_power_w": ipmi.power_w if ipmi else None,
            "fans_rpm": ipmi.fans_rpm if ipmi else {},
            "health": {**self._health, "last_poll_at": wall, "set_failed": self._set_failed},
        })

    # -- running ----------------------------------------------------------------------------------------

    def run_forever(self, stop: threading.Event) -> None:
        """One step every poll_s (fixed rate), and a systemd watchdog ping after each."""
        next_at = self.mono()
        while not stop.is_set():
            self.step()
            sd_notify("WATCHDOG=1")
            next_at += self.cfg.poll_s
            delay = next_at - self.mono()
            if delay < -self.cfg.poll_s:  # far behind (e.g. a slow racadm): don't try to catch up
                next_at = self.mono()
                delay = 0
            stop.wait(max(0.0, delay))


STARTUP_ALERT_EVERY_S = 1800.0
_STARTUP_STAMP = "startup-failure-alerted"


class ApiStarter:
    """Starts the HTTP API without ever holding up fan control: if the port or the
    socket can't be had (taken, a missing group), the loop runs without its API,
    says so once, and keeps trying."""

    def __init__(self, make, serve, alerter, wall: Callable[[], float]):
        self.make, self.serve, self.alerter, self.wall = make, serve, alerter, wall
        self.servers: Tuple = ()
        self._lock = threading.Lock()
        self._closed = False

    def try_start(self) -> bool:
        with self._lock:
            if self._closed:
                return True
            try:
                servers = self.make()
            except Exception as e:
                self.alerter.open_incident(
                    "api", f"The loop's API couldn't start: {e!r}"[:300] + ". Fan control runs on; fanctl and "
                           "Hermes can't reach the loop until it does. Retrying every minute.", self.wall())
                return False
            self.serve(servers)
            self.servers = servers
        self.alerter.close_incident("api", "The loop's API is up again.", self.wall())
        return True

    def keep_trying(self, stop: threading.Event, retry_s: float) -> None:
        while not stop.wait(retry_s):
            if self.try_start():
                return

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for server in self.servers:
                server.shutdown()
                server.server_close()
            self.servers = ()


def _stamp_paths(cfg) -> List[str]:
    # /run first (tmpfs, writable even when the disk isn't), then the state directory
    return [os.path.join(os.path.dirname(cfg.socket_path), _STARTUP_STAMP),
            os.path.join(os.path.dirname(cfg.db_path), _STARTUP_STAMP)]


def report_startup_failure(cfg, sender, error: BaseException, now: float) -> None:
    """One direct Telegram message per STARTUP_ALERT_EVERY_S while startup keeps failing:
    systemd restarts the loop every few seconds, and the Alerter may be what failed.
    A stamp file remembers the last message across those restarts."""
    from fanloop.alerts import PREFIX
    print(f"fan loop can't start: {error!r}", file=sys.stderr)
    for path in _stamp_paths(cfg):
        try:
            if now - os.path.getmtime(path) < STARTUP_ALERT_EVERY_S:
                return
        except OSError:
            pass
    for path in _stamp_paths(cfg):
        try:
            with open(path, "w") as f:
                f.write(f"{now}\n")
            os.utime(path, (now, now))
            break
        except OSError:
            continue
    else:  # nowhere to remember it: stay quiet rather than message on every restart
        print("no stamp file could be written; not messaging", file=sys.stderr)
        return
    text = (f"The fan loop can't start: {error!r}"[:400] + ". systemd keeps retrying; the fans stay where they "
            "are. Details: journalctl -u fan-controller")
    try:
        sender.send(PREFIX + text)
    except Exception as e:
        print(f"could not send to Telegram: {e!r}", file=sys.stderr)


def _clear_startup_stamps(cfg) -> None:
    for path in _stamp_paths(cfg):
        try:
            os.unlink(path)
        except OSError:
            pass


def main(argv: Optional[List[str]] = None, sender=None, runner=None,
         stop: Optional[threading.Event] = None) -> int:
    """`sender`, `runner` and `stop` are for tests; systemd runs it with none of them."""
    from fanloop.alerts import Alerter, TelegramSender
    from fanloop.api import make_servers, serve
    from fanloop.busy import BusyDetector
    from fanloop.clock import zone
    from fanloop.commands import CommandRunner
    from fanloop.config import load_config, load_secrets
    from fanloop.control import ControlConfig, Controller
    from fanloop.episodes import EpisodeTracker
    from fanloop.holds import Holds
    from fanloop.marks import Marks
    from fanloop.sensors import llama_health
    from fanloop.service import Service
    from fanloop.store import Store
    from fanloop.thresholds import load_defaults
    from fanloop.watchdog import ReviewWatchdog

    parser = argparse.ArgumentParser(prog="python3 -m fanloop.daemon")
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
        secrets = load_secrets(cfg.secrets_dir)
    except Exception as e:  # nothing to message with yet: the journal has it
        print(f"fan loop can't start: {e!r}", file=sys.stderr)
        return 1
    sender = sender or TelegramSender(secrets.telegram_token, secrets.telegram_chat_id)

    try:
        store = Store(cfg.db_path, load_defaults())
        runner = runner or CommandRunner()
        tz = zone(cfg.timezone)
        alerter = Alerter(sender, store, tz=tz)
        marks, holds, watchdog = Marks(store), Holds(store, tz=tz), ReviewWatchdog(store)
        service = Service(store, alerter, marks, holds, watchdog, dry_run=cfg.dry_run, tz=tz)
        controller = Controller(store.current()[1], ControlConfig(
            blind_idle_power_w=cfg.blind_idle_power_w, blind_power_above_w=cfg.blind_power_above_w,
            blind_hold_s=cfg.blind_hold_s))
        sensors, offset = build_io(cfg, runner, time.monotonic)
        loop = Loop(cfg, service, store, sensors, offset,
                    controller, BusyDetector(), EpisodeTracker(), alerter, marks, holds, watchdog,
                    mono=time.monotonic, wall=time.time, llama=lambda: llama_health(cfg.llama_health_url))
        alerter.start()
        loop.startup()
    except Exception as e:
        report_startup_failure(cfg, sender, e, time.time())
        return 1
    _clear_startup_stamps(cfg)

    stop = stop or threading.Event()
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, lambda *_: stop.set())

    api = ApiStarter(lambda: make_servers(service, store, cfg.listen_host, cfg.listen_port, cfg.socket_path,
                                          secrets.write_token, cfg.socket_group or None),
                     serve, alerter, wall=time.time)
    if not api.try_start():
        threading.Thread(target=api.keep_trying, args=(stop, cfg.api_retry_s), name="api-start",
                         daemon=True).start()
    sd_notify("READY=1")
    try:
        loop.run_forever(stop)
    finally:
        sd_notify("STOPPING=1")
        stop.set()
        api.close()
        alerter.drain(5)
        alerter.stop()
        store.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0

if __name__ == "__main__":
    sys.exit(main())
