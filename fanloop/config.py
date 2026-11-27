"""The loop's configuration (a JSON file, no secrets) and its secrets (one file each)."""
import dataclasses
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Union

from fanloop.clock import DEFAULT_TZ, zone


@dataclass(frozen=True)
class Config:
    # Paths and network
    db_path: str = "/var/lib/fan-controller/fan.db"
    listen_host: str = "0.0.0.0"
    listen_port: int = 8780
    socket_path: str = "/run/fan-controller/ctl.sock"
    socket_group: str = "fanctl"
    secrets_dir: str = "/etc/fan-controller/secrets"
    # Timing
    dry_run: bool = True
    poll_s: float = 5.0
    ipmi_every_s: float = 30.0
    offset_read_every_s: float = 60.0
    watchdog_every_s: float = 60.0
    prune_every_s: float = 3600.0
    # Command timeouts. Together they bound one poll: see daemon.worst_step_s and WATCHDOG_SEC.
    nvidia_timeout_s: float = 10.0
    ipmi_timeout_s: float = 15.0       # one whole IPMI read (up to three ipmitool calls)
    racadm_timeout_s: float = 10.0     # each racadm call
    racadm_set_budget_s: float = 25.0  # one set with its read-back and retries
    # Blind rule (spec 7), to be set from the live measurement
    blind_idle_power_w: float = 235.0  # measured 2026-11-27 (spec 3)
    blind_power_above_w: float = 50.0
    blind_hold_s: float = 30.0
    # Failure handling and health
    nvidia_fail_polls: int = 3
    api_retry_s: float = 60.0  # how often to retry starting the API if it couldn't
    llama_health_url: str = "http://127.0.0.1:8080/health"
    # Every clock time the loop shows or reads (holds, messages, fanctl); t4 runs in UTC
    timezone: str = DEFAULT_TZ
    # Backup to docks (an empty target disables the copy)
    backup_target: str = ""
    backup_key: str = "/etc/fan-controller/secrets/docks_ed25519"
    backup_known_hosts: str = "/etc/fan-controller/known_hosts"
    backup_dir: str = "/var/lib/fan-controller/backup"


def load_config(path: Union[str, Path]) -> Config:
    """Reads a JSON object; unknown keys and wrong types raise ValueError."""
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("the config must be a JSON object")
    fields = {f.name: f for f in dataclasses.fields(Config)}
    unknown = set(data) - set(fields)
    if unknown:
        raise ValueError(f"unknown config keys: {sorted(unknown)}")
    for key, value in data.items():
        default = getattr(Config, key)
        ok = (isinstance(value, bool) if isinstance(default, bool)
              else isinstance(value, int) and not isinstance(value, bool) if isinstance(default, int)
              else isinstance(value, (int, float)) and not isinstance(value, bool) if isinstance(default, float)
              else isinstance(value, str))
        if not ok:
            raise ValueError(f"config {key} must be a {type(default).__name__}, got {value!r}")
    cfg = Config(**{k: (float(v) if isinstance(getattr(Config, k), float) else v) for k, v in data.items()})
    zone(cfg.timezone)  # ValueError for an unknown zone
    return cfg


@dataclass(frozen=True)
class Secrets:
    telegram_token: str
    telegram_chat_id: str
    write_token: str

    def __repr__(self) -> str:  # never print the values
        return "Secrets(****)"


def load_secrets(directory: Union[str, Path]) -> Secrets:
    def read(name: str) -> str:
        return Path(directory, name).read_text().strip()
    return Secrets(read("telegram_token"), read("telegram_chat_id"), read("write_token"))


def main(argv) -> int:
    """`python3 -m fanloop.config PATH`: exit 0 if the loop would accept the file.
    The playbook uses it to validate a rendered config before installing it."""
    if len(argv) != 1:
        print("usage: python3 -m fanloop.config PATH", file=sys.stderr)
        return 2
    try:
        load_config(argv[0])
    except (OSError, ValueError, TypeError) as e:
        print(f"{argv[0]}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
