# Runbook

Commands run on `t4` unless they say otherwise. `fanctl` needs the `fanctl` group: it
applies from your next login, and `sg fanctl -c "fanctl …"` works until then. All clock
times the loop shows or reads are in its own zone (config `timezone`, EET); `t4` itself
runs in UTC.

## Checking on it

```
fanctl status                      # level, cards, inlet, hold, problems, dry-run
systemctl status fan-controller
journalctl -u fan-controller -e
```

## Holding the fans higher

A hold is a minimum level: the loop can still go above it.

```
fanctl hold medium                 # until cancelled (a reminder every day)
fanctl hold high until 18:00       # or: until 2026-12-01 09:00 (at most 24 h ahead)
fanctl hold off
```

Don't set the offset with racadm while the loop runs: it sees a manual change and
takes control back.

## A fixed-offset test

1. Ask whether a VoiceProfile lab test is running.
2. `sudo systemctl stop fan-controller`
3. `sudo racadm set system.thermalsettings.FanSpeedOffset <255|0|2|1|3>`
   (Off, Low, Medium, High, Max)

**While it's stopped there is no emergency rule.** Nothing raises the fans if the
cards get hot; they only protect themselves by throttling.

4. Afterwards: `sudo systemctl start fan-controller`

## Dry-run

From `~/ansible` on the laptop:

```
ansible-playbook fan-controller.yml -e fan_dry_run=true    # reads and decides, never sets the fans
ansible-playbook fan-controller.yml -e fan_dry_run=false   # real control
```

Leaving out `-e fan_dry_run` means dry-run. In dry-run the emergency rule only logs
and alerts ("Dry-run: the fans were not changed."). The restart message says which
mode it's in.

## Thresholds

```
fanctl show
fanctl set medium 63 --reason "quieter at idle"      # several: low 54 hold 240 …
fanctl ambient add 30 high                            # ambient remove 30
fanctl history [--days N]
fanctl revert v3                                      # lists every change it undoes
```

Every change shows first and asks `Apply? [y/N]` (`--yes` skips that), and is
announced on Telegram with its version numbers. Hard limits are enforced by the loop.

## The review watchdog

It alerts when no Hermes review has finished properly for 13 h, then once a day, and
sends "Hermes reviews are completing again." after the next review that ends properly.
Reviews run every 6 h, so one alert means about two missed reviews: check `hermes cron
list` on `hag` (see Hermes below) and the model server on `t4`. A review whose data
couldn't be read ends silently, without waking the model (Hermes drops the error text):
`python3 ~/.hermes/scripts/fan.py status` as `hermes` on `hag` prints the same error. While Hermes is stopped
on purpose (`hermes cron pause fan-review`):

```
fanctl mute reviews until 2026-12-31    # ends by itself
fanctl mute off
```

## Hermes

Hermes runs on `hag` as the `hermes` user (`ssh hag`, then `sudo -u hermes -i`). Its
model runs on `t4`'s GPUs, so each review heats them for a minute or two; the loop marks
that time and leaves it out of the evidence.

| Job | When (EET) | What |
|---|---|---|
| `fan-review` | 03, 09, 15, 21 h | `fan_review.py` sends the review's start mark and prints the data; the model decides on at most one change, writes a note (`fan.py note` sends the end mark) and replies |
| `fan-daily` | 09:00 | `fan_daily.py` prints the 24 h summary; no model run |

```
hermes cron list                     # both jobs, active or paused, next run
hermes cron pause fan-review         # stop reviews (mute the watchdog too, above)
hermes cron resume fan-review
hermes cron run fan-review           # one review now; the job must be active
hermes cron edit fan-review ...      # change schedule or prompt (see hermes cron edit --help)
```

The playbook creates the jobs only if they're missing: to recreate one from the repo,
`hermes cron remove <id>` and rerun the playbook. Scripts and skill are redeployed on
every run. A Telegram conversation keeps the skill list it started with: after a new or
changed skill, send Hermes `/new` before asking about the fans. In chat, Hermes asks
before running a command; allow `python3 /home/hermes/.hermes/scripts/fan.py …`, and
deny anything else for a fan question.

**What Hermes's messages mean** (from its own bot, not `[t4 fans]`):
- A change or revert: what changed (`Medium 65 → 63°C, v7 → v8`), why, the evidence, and
  what it will check next. The loop also announces it, as `source review`.
- A slowdown, a rejected change it couldn't resolve, or a warning that Max may not be
  enough: read it, and look at `fanctl history`.
- The daily summary at 09:00: episodes, time at each level, slowdown, peak, threshold
  changes, problems.
- A review with no change sends nothing; its note is saved on `hag`.
- An error notice from `fan-daily`: `hag` can't reach the loop.

**`/hot` in Telegram** prints `fan.py status` without the model (about a second). It's a
Hermes quick command, added by hand to `/home/hermes/.hermes/config.yaml` (the playbook
doesn't manage that file; re-add it on a rebuilt `hag`), then `systemctl --user restart
hermes-gateway.service` as `hermes`:

```yaml
quick_commands:
  hot:
    type: exec
    command: /usr/bin/python3 /home/hermes/.hermes/scripts/fan.py status
```

It works when typed; Telegram's menu is full (60), so it isn't listed there.

**Undoing a Hermes change:** `fanctl revert v<N>` on `t4`, like any other change. In
chat you can also ask Hermes ("set Medium back to 65"); it writes as `user-chat`, at
most 6 times a day.

| What, on `hag` | Where |
|---|---|
| Scripts | `/home/hermes/.hermes/scripts/fan*.py` |
| Skill | `/home/hermes/.hermes/skills/devops/fan-controller/SKILL.md` |
| Settings (loop URL, paths) | `/home/hermes/.config/fan-controller/hermes.json` |
| Write token | `/home/hermes/.config/fan-controller/write-token` (from the Ansible vault) |
| Notes and review state | `/home/hermes/fan-controller/notes.jsonl`, `state.json` |
| Jobs, run output, logs | `/home/hermes/.hermes/cron/jobs.json`, `cron/output/<job id>/`, `logs/agent.log` |
| Hermes's own bot | set up by hand with `hermes gateway setup`; its token is in `/home/hermes/.hermes/.env` |

## Restoring from docks

Backups run nightly at 03:30 (± 10 min): `/var/lib/fan-controller/backup/fan-<Day>.db`
on `t4`, copied to `/srv/fan-backup/` on `docks`, one per weekday.

1. `sudo systemctl stop fan-controller` (no emergency rule while stopped)
2. Copy `fan-<Day>.db` from `docks:/srv/fan-backup/` to `/var/lib/fan-controller/fan.db`,
   then `sudo chown fanloop:fanloop /var/lib/fan-controller/fan.db` and remove any
   `fan.db-wal` and `fan.db-shm` next to it.
3. `sudo systemctl start fan-controller`
4. `fanctl show` shows the restored version.

To look inside a copy first:
`sudo -u fanloop env PYTHONPATH=/opt/fan-controller python3 -m fanloop.store <file>`.

## Messages

Every message starts with `[t4 fans]`. Problems send one alert per incident and one
message when it's over. A repeat of the same kind within 30 minutes stays quiet unless
it lasts 5 minutes, then it's sent marked "(ongoing since HH:MM)".

| Message | Meaning | What to do |
|---|---|---|
| Fan loop started: … | the loop (re)started; shows offset, mode, thresholds version | nothing, unless it repeats: `journalctl -u fan-controller` |
| Fan loop stopping. … | stopped on purpose (systemd stop, a redeploy) | nothing; until it starts again, nothing raises the fans |
| Emergency: … Fans to Max. | ≥ 80°C or a thermal slowdown flag | the loop handles it; check the load if it repeats |
| Thermal slowdown on card N | a card is throttling | as above |
| At Max and still rising | the fans can't keep up | reduce the load |
| racadm couldn't set … | the offset didn't take or didn't verify | it retries every poll; check the iDRAC |
| Fan offset changed outside the loop | someone set the offset by hand | use `fanctl hold` instead |
| nvidia-smi problem for N polls | GPU readings missing | with one card, it controls on the other; with none, system power decides (Medium at most) |
| The loop can't use its database | full or read-only disk, or a broken file | control goes on; fix the disk, see Restoring |
| The loop's API couldn't start | port or socket taken | control goes on; it retries every minute |
| The fan loop can't start | startup failed (at most every 30 min) | `journalctl -u fan-controller` |
| No completed Hermes review for 13 h | see The review watchdog | mute it, or check Hermes and llama-server |
| Backup to docks failed | the nightly copy didn't reach docks | check `docks` and the `fanbackup` account |
| Fan loop error in … | a bug in one part of a poll (at most every 30 min) | `journalctl -u fan-controller`; report it |

## Where things are

| What | Where |
|---|---|
| Code | `/opt/fan-controller` (`fanctl` linked in `/usr/local/bin`) |
| Units | `/etc/systemd/system/fan-controller.service`, `fan-controller-backup.{service,timer}` |
| Config | `/etc/fan-controller/config.json` |
| Secrets | `/etc/fan-controller/secrets/` (Telegram token and chat id, write token, backup key) |
| Database and local backups | `/var/lib/fan-controller/` |
| Root commands allowed | `/etc/sudoers.d/fan-controller` |
| Logs | `journalctl -u fan-controller`, `journalctl -u fan-controller-backup` |
| Local socket for `fanctl` | `/run/fan-controller/ctl.sock` |
| API | port 8780 on `t4`: reads open, writes need the token |
