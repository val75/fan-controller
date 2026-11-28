# fan-controller

Automatic iDRAC fan control for a Dell PowerEdge R730 lab box with two passively
cooled Tesla T4 cards. A small loop on the box reads the GPUs (`nvidia-smi`) and the
iDRAC sensors (`ipmitool`) every 5 s, and sets the iDRAC fan speed offset with local
`racadm` (Off, Low, Medium, High, Max) so the fans follow the GPU load. It alerts on
Telegram, keeps its history in SQLite, and serves an HTTP API for the `fanctl`
command and for a reviewing agent (Hermes) that tunes the thresholds.

- Design: [`docs/superpowers/specs/2026-11-12-gpu-fan-controller-design.md`](docs/superpowers/specs/2026-11-12-gpu-fan-controller-design.md)
- Implementation plans: [`docs/superpowers/plans/2026-11-23-fan-loop.md`](docs/superpowers/plans/2026-11-23-fan-loop.md) (the loop),
  [`docs/superpowers/plans/2026-11-27-hermes.md`](docs/superpowers/plans/2026-11-27-hermes.md) (Hermes reviews on `hag`)
- Operating it: [`docs/runbook.md`](docs/runbook.md)

Python 3.10, standard library only.

## Layout

| Path | What |
|---|---|
| `fanloop/control.py`, `busy.py`, `thresholds.py`, `defaults.json` | the control rule, busy detection, thresholds and their hard limits |
| `fanloop/commands.py`, `sensors.py`, `offset.py` | the 9 root command lines, sensor readers, racadm set with read-back |
| `fanloop/daemon.py`, `config.py`, `clock.py` | the loop, systemd notify, config and secrets, the loop's time zone |
| `fanloop/store.py`, `episodes.py`, `backup.py` | SQLite history, busy episodes, the nightly backup |
| `fanloop/alerts.py`, `watchdog.py`, `holds.py`, `marks.py` | Telegram alerts and incidents, review watchdog, manual holds, review marks |
| `fanloop/service.py`, `api.py`, `policy.py` | shared facade, HTTP API (TCP and Unix socket), threshold write policy |
| `fanloop/cli.py`, `bin/fanctl` | the `fanctl` command |
| `fanloop/replay.py`, `data/fan-tests-2026-11-09/` | replaying the recorded fan tests |
| `deploy/` | systemd units, sudoers file, example config |
| `hermes/scripts/`, `hermes/skill/`, `hermes/prompts/` | Hermes's side on `hag`: the review and daily scripts, `fan.py` commands, the skill, the review prompt |
| `tests/` | unit tests, with real command output in `tests/fixtures/` |

## Tests

```
python3 -m unittest discover -s tests
```

## Deploying

With Ansible, from `~/ansible` (outside this repo):

```
ansible-playbook fan-controller.yml --check --diff
ansible-playbook fan-controller.yml -e fan_dry_run=true    # or false for real control
```

The Telegram token, chat id and API write token live in the Ansible vault.

## Addresses

This repo is public. It names machines, never addresses: real addresses live in a
local, untracked `.env` (template: `.env.example`) and in the Ansible inventory.
