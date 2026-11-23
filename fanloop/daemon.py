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
        self._health = {"nvidia_ok": False, "ipmi_ok": False, "racadm_ok": False}
        # filled each step
        self._cards: List[CardReading] = []
        self._busy = False
        self._hold = None
        self._version = 1

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
            self.store.add_event(wall, "racadm-read-error", {"error": (error or "")[:300]})
        self._next_offset_read = mono + self.cfg.offset_read_every_s
        self.store.add_event(wall, "loop-start", {"offset": _name(level), "dry_run": self.cfg.dry_run,
                                                  "version": version})
        self.alerter.alert("restart", f"Fan loop started: offset {_name(level)}, "
                                      f"dry-run {'on' if self.cfg.dry_run else 'off'}, thresholds v{version}", wall)

    # -- one poll ---------------------------------------------------------------------------------

    def step(self) -> None:
        mono, wall = self.mono(), self.wall()
        for name, part in (("gpus", self._gpus), ("ipmi", self._read_ipmi), ("busy", self._update_busy),
                           ("hold", self._update_hold), ("control", self._control),
                           ("slowdown", self._check_slowdown), ("max", self._check_max),
                           ("episodes", self._record_episode), ("sample", self._record_sample),
                           ("reviews", self._reviews), ("prune", self._prune), ("status", self._publish)):
            try:
                part(mono, wall)
            except Exception as e:
                try:
                    self.store.add_event(wall, "loop-error", {"part": name, "error": repr(e)[:300]})
                    self.alerter.alert("loop-error", f"Fan loop error in {name}: {e!r}"[:400], wall)
                except Exception:
                    pass

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

        if current is None or target != current or self._set_failed:
            if target != self._logged_target or not self._set_failed:
                self.store.add_event(wall, "level", {"from": _name(current), "to": _name(target),
                                                     "reason": decision.reason})
                self._logged_target = target
            if decision.reason == "emergency" and current != Level.MAX:
                hotter = self.controller.hotter_c(self._cards, self.controller.cfg.single_card1_offset_c)
                flags = any(c.sw_thermal or c.hw_thermal for c in self._cards)
                why = "thermal slowdown" if flags else f"hotter card at {hotter:g}°C"
                self.alerter.open_incident("emergency", f"Emergency: {why}. Fans to Max.", wall)
            result = self.offset.set(target)
            if result.ok:
                if current == Level.MAX and target != Level.MAX:
                    self.alerter.close_incident("emergency", f"Emergency over: fans down from Max to "
                                                             f"{target.name.capitalize()}.", wall)
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
                self.store.add_event(wall, "racadm-error", {"target": _name(target), "error": (result.error or "")[:300]})
                self.alerter.open_incident("racadm", f"racadm couldn't set {_name(target)} after "
                                                     f"{result.attempts} tries: {(result.error or '')[:200]}. "
                                                     "Retrying every poll.", wall)
        elif not self.cfg.dry_run and mono >= self._next_offset_read:
            self._next_offset_read = mono + self.cfg.offset_read_every_s
            level, error = self.offset.read()
            self._health["racadm_ok"] = level is not None
            if level is None:
                self.store.add_event(wall, "racadm-read-error", {"error": (error or "")[:300]})
            elif level != current:
                self.store.add_event(wall, "manual-change", {"loop": _name(current), "found": _name(level)})
                self.alerter.alert("manual-change", f"Fan offset changed outside the loop: {_name(level)} "
                                                    f"(the loop had {_name(current)}). Taking control back. "
                                                    "Use fanctl hold to keep the fans higher.", wall)
                self.current_level = level

    def _check_slowdown(self, mono, wall):
        slow = [c for c in self._cards if c.sw_thermal or c.hw_thermal]
        if slow:
            if self._slowdown_seen is None:
                c = slow[0]
                self.alerter.open_incident("slowdown", f"Thermal slowdown on card {c.index} at {c.temp_c:g}°C.", wall)
            self._slowdown_seen = mono
        elif self._slowdown_seen is not None and mono - self._slowdown_seen >= _SLOWDOWN_CLEAR_S:
            self._slowdown_seen = None
            self.alerter.close_incident("slowdown", "No thermal slowdown for 5 minutes.", wall)

    def _check_max(self, mono, wall):
        hotter = self.controller.hotter_c(self._cards, self.controller.cfg.single_card1_offset_c)
        if self.current_level != Level.MAX:
            if self._max_since is not None:
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
            self.store.add_event(wall, "episode", {"start": ep.start, "duration_s": ep.duration_s,
                                                   "peak_c": max(ep.peak_c.values(), default=None)})

    def _record_sample(self, mono, wall):
        self.store.add_sample(wall, self._cards, self._ipmi, self.current_level, self._busy)

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


def main(argv: Optional[List[str]] = None) -> int:
    from fanloop.alerts import Alerter, TelegramSender
    from fanloop.api import make_servers, serve
    from fanloop.busy import BusyDetector
    from fanloop.commands import CommandRunner
    from fanloop.config import load_config, load_secrets
    from fanloop.control import ControlConfig, Controller
    from fanloop.episodes import EpisodeTracker
    from fanloop.holds import Holds
    from fanloop.marks import Marks
    from fanloop.offset import Offset
    from fanloop.sensors import Sensors, llama_health
    from fanloop.service import Service
    from fanloop.store import Store
    from fanloop.thresholds import load_defaults
    from fanloop.watchdog import ReviewWatchdog

    parser = argparse.ArgumentParser(prog="python3 -m fanloop.daemon")
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    secrets = load_secrets(cfg.secrets_dir)

    store = Store(cfg.db_path, load_defaults())
    runner = CommandRunner()
    alerter = Alerter(TelegramSender(secrets.telegram_token, secrets.telegram_chat_id), store)
    marks, holds, watchdog = Marks(store), Holds(store), ReviewWatchdog(store)
    service = Service(store, alerter, marks, holds, watchdog, dry_run=cfg.dry_run)
    controller = Controller(store.current()[1], ControlConfig(
        blind_idle_power_w=cfg.blind_idle_power_w, blind_power_above_w=cfg.blind_power_above_w,
        blind_hold_s=cfg.blind_hold_s))
    loop = Loop(cfg, service, store,
                Sensors(runner, nvidia_timeout_s=cfg.nvidia_timeout_s, ipmi_timeout_s=cfg.ipmi_timeout_s),
                Offset(runner, dry_run=cfg.dry_run, timeout_s=cfg.racadm_timeout_s),
                controller, BusyDetector(), EpisodeTracker(), alerter, marks, holds, watchdog,
                mono=time.monotonic, wall=time.time, llama=lambda: llama_health(cfg.llama_health_url))

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    alerter.start()
    loop.startup()
    servers = make_servers(service, store, cfg.listen_host, cfg.listen_port, cfg.socket_path,
                           secrets.write_token, cfg.socket_group or None)
    serve(servers)
    sd_notify("READY=1")
    try:
        loop.run_forever(stop)
    finally:
        sd_notify("STOPPING=1")
        for server in servers:
            server.shutdown()
            server.server_close()
        alerter.drain(5)
        alerter.stop()
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
