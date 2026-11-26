"""Nightly backup (spec 5.5): a consistent copy of the loop's database, kept for a
week under a weekday name, then copied to docks with rsync over ssh.

Run by fan-controller-backup.timer as `python3 -m fanloop.backup --config PATH`.
It opens the database read-only, so it never writes to the loop's database."""
import argparse
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from fanloop.alerts import PREFIX, TelegramSender
from fanloop.clock import zone
from fanloop.commands import CommandRunner
from fanloop.config import Config, load_config, load_secrets

RSYNC_TIMEOUT_S = 300
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")  # not %a: that follows the locale


def local_copy_path(cfg: Config, now: datetime) -> str:
    return os.path.join(cfg.backup_dir, f"fan-{_WEEKDAYS[now.weekday()]}.db")


def rsync_argv(cfg: Config, local_copy: str) -> tuple:
    ssh = (f"ssh -i {cfg.backup_key} -o UserKnownHostsFile={cfg.backup_known_hosts} "
           "-o BatchMode=yes -o IdentitiesOnly=yes")
    return ("/usr/bin/rsync", "-t", "-e", ssh, local_copy, cfg.backup_target)


def copy_database(src_path: str, dest_path: str) -> None:
    """SQLite's backup API from a read-only connection, into a temporary file that
    replaces the old copy only once it is complete."""
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    tmp = dest_path + ".tmp"
    if os.path.exists(tmp):
        os.unlink(tmp)
    src = sqlite3.connect(Path(src_path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        dest = sqlite3.connect(tmp)
        try:
            src.backup(dest)
            dest.execute("PRAGMA journal_mode=DELETE")  # one self-contained file, no -wal/-shm
        finally:
            dest.close()
    finally:
        src.close()
    os.replace(tmp, dest_path)


def _detail(result) -> str:
    if result.timed_out:
        return f"rsync timed out after {RSYNC_TIMEOUT_S} s"
    if result.skipped:
        return "an earlier rsync is still stuck"
    lines = [line for line in (result.stderr or result.stdout or "").splitlines() if line.strip()]
    return (lines[-1] if lines else f"rsync exited with {result.returncode}")[:300]


def _tell(sender, text: str) -> None:
    try:
        sender.send(PREFIX + text)
    except Exception as e:  # the journal still has it
        print(f"could not send to Telegram: {e!r}", file=sys.stderr)


def run_backup(cfg: Config, now: datetime, runner, sender) -> bool:
    local = local_copy_path(cfg, now)
    try:
        copy_database(cfg.db_path, local)
    except (sqlite3.Error, OSError) as e:
        text = f"Backup failed: couldn't copy the database: {e}"
        print(text, file=sys.stderr)
        _tell(sender, text)
        return False
    print(f"local copy: {local}", file=sys.stderr)
    if not cfg.backup_target:
        return True
    result = runner.run(rsync_argv(cfg, local), RSYNC_TIMEOUT_S)
    if not result.ok:
        text = f"Backup to docks failed: {_detail(result)}"
        print(text, file=sys.stderr)
        _tell(sender, text)
        return False
    print(f"copied to {cfg.backup_target}", file=sys.stderr)
    return True


def main(argv) -> int:
    parser = argparse.ArgumentParser(prog="fanloop.backup", description="Back up the fan loop's database.")
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    secrets = load_secrets(cfg.secrets_dir)
    sender = TelegramSender(secrets.telegram_token, secrets.telegram_chat_id)
    return 0 if run_backup(cfg, datetime.now(zone(cfg.timezone)), CommandRunner(), sender) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
