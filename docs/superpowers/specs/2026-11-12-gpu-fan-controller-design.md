# GPU fan controller for the T4 lab box, with Hermes

**Date:** 2026-11-12. **Status:** approved by the user on 2026-11-23, after the review of
2026-11-13 to 11-23.
**Decided by the user** unless marked *(my call)*. *(My call)* items are reasonable defaults
the user hasn't explicitly agreed to; change them freely.

## 1. Intent

**Names, not addresses:** this repo is public, so it uses SSH aliases (`t4`, `hag`, `docks`)
and `r730-bmc` for the iDRAC. The real addresses live in a local, untracked `.env`
(template: `.env.example`).

The lab GPU box `t4` (Dell PowerEdge R730) has two passively cooled Tesla T4s.
Under sustained load they overheat and throttle unless the chassis fans run faster. The only
fan control used here is the iDRAC **fan speed offset**, which raises Dell's own fan curve by
a step. A fixed offset is a poor compromise:
- **Low** throttles after ~5.5 min of sustained load.
- **Medium** holds, but is loud all the time, including while idle.
- **Max** is louder still.

The user wants the fans to **follow the load**: Off while idle, stepping up as the cards
heat, and back down once they've cooled. A system that **learns how the parameters relate**
(card temperature, load, garage ambient, fan step) is wanted too.

The box sits in an unconditioned garage: about 8°C outside now, up to 45°C ambient in summer.

This is also the user's **first use of Hermes-agent** (Nous Research). They plan to run more
things through Hermes later, so its setup should suit more than this job.

## 2. Goals, non-goals, anti-goals

**Goals**
- No GPU thermal slowdown under sustained load, with as little fan noise as that allows.
- Fans react within seconds of a temperature change.
- Thresholds tuned over time from measured data, by Hermes, without the user's approval.
  Hermes must tell the user on Telegram what it changed and why.
- Urgent alerts reach the user's phone (Telegram) even when Hermes or its VM is down.
- The user can chat with Hermes from their phone (Telegram): "how hot is it now", "what
  changed this week".

**Non-goals (for now)**
- Direct fan PWM control. No raw `ipmitool raw 0x30 0x30 …` manual fan mode: it's
  undocumented, and it bypasses Dell's fan curve and safety behaviour. Only the offset is used.
- Re-adding the third T4. A second llama-server slot. Using the DGX-2's models or a hosted
  model for Hermes. All of these may come later.
- Limiting or pausing the GPU workloads themselves. If the fans can't keep up, the system
  alerts and leaves the decision to the user.

**Anti-goals** (the design has failed if any of these happen, even if it "works")
- An AI is in the real-time path. The fast loop must be plain, deterministic code. Hermes
  is slow (minutes per review) and may be down.
- Hermes (or its VM) can reach the iDRAC with admin credentials, or get a shell or sudo on t4.
  The boundary is **no iDRAC credentials on `hag`**, not a network block. The iDRAC stays
  reachable from the lab network, and IPMI over LAN stays enabled. The user accepted that
  risk (2026-11-12: they're the only person on the lab network), and the iDRAC password is no
  longer Dell's default. No firewall work is in scope.
  The loop's own user may run only 9 exact commands as root (5.10, decided 2026-11-22).
  So even a bug in the loop's HTTP endpoint gives `hag` no more than the fan offsets and the
  sensor readings: no root on t4, and no iDRAC admin.
- Fans flap: frequent step changes that are noisier than a fixed offset would be.
- A threshold change makes things hotter or louder, and nobody notices or undoes it.

## 3. Facts this design rests on (measured)

**Hardware and temperatures**
- 2 × Tesla T4, index 0 = PCI `04:00.0`, 1 = `82:00.0`, on different CPU sockets. Card 0 runs
  ~3–4°C warmer than card 1.
- 70 W power cap per card, active from the first second of load.
- T4 limits: software thermal slowdown at ~84–85°C (max operating temperature 85°C);
  hardware slowdown at 93°C; shutdown at 96°C.
- Under load, the cards pass 55°C ~15–25 s after load starts (cold start, fan offset Off or
  Medium).
- Idle: both cards read 53°C (0% utilization, ~28 W) at a 17°C inlet (one reading,
  2026-11-13). **Measured over 25 h of dry-run (2026-11-26/27):** 40–41°C at an 11–12°C
  inlet, fans Off (see 5.3).
- A driver update can leave `nvidia-smi` failing ("Driver/library version mismatch") until a
  reboot, while running CUDA processes such as llama-server keep working. On 2026-11-16
  unattended-upgrades had already tried to install a newer driver build from
  `jammy-security`. Since then the 16 NVIDIA `*-535` packages are on `apt-mark hold` and
  unattended-upgrades is off on t4 (done by hand; to go into the t4 playbook, see 5.10).
  The failure rule in 7 still applies: a driver update can happen by hand.
- In `nvidia-smi dmon`, `pviol`/`tviol` read 0 even while throttling. Use
  `nvidia-smi --query-gpu=…,clocks_event_reasons.sw_thermal_slowdown,…` (or `-q -d
  PERFORMANCE`). Driver 535 already uses the `clocks_event_reasons.*` names, as the headers
  of the 2026-11-09 CSVs show.

**Sustained-load tests** (2 cards, inlet ~14°C, workload: back-to-back LLM generation)

| Fan offset | Peak temp (card 0 / 1) | Thermal slowdown | Idle fan RPM |
|---|---|---|---|
| Off | 84 / — | yes | ~7,200 |
| Low | 85 / 85 | yes: card 0 at 5 min 24 s, card 1 at 6 min 22 s, both at 84°C, then switching on and off repeatedly (7× / 9×) | ~7,200 (same as Off at idle) |
| Medium | 74 / 70 | none | ~9,400 |
| Max | 58 / 55 | none | — |

So with Medium, the hotter card settled about **60°C above inlet** under sustained load.
The per-second logs from the Low and Medium tests are in this repo at
`data/fan-tests-2026-11-09/gpu-fanlow.csv` and `gpu-fanmedium.csv` (`nvidia-smi` CSV format),
next to `sustained_load.py`, the load generator used for the tests.

**iDRAC and racadm**
- iDRAC8 firmware 2.86 (`r730-bmc`, address in `.env` as `IDRAC_HOST`; the name doesn't
  resolve, and there's no SSH). Reachable from the whole lab network, but this design reaches it only locally from t4.
- Dell racadm (iDRAC Tools 11.3.0.0) is installed on t4 and works in **local mode** over
  `/dev/ipmi0`. It needs sudo and no password.
  - Read: `sudo racadm get system.thermalsettings.FanSpeedOffset`, ~2.4 s. Output contains
    `FanSpeedOffset=Off` (or `Low Fan Speed`, `Medium Fan Speed`, …).
  - Set: `sudo racadm set system.thermalsettings.FanSpeedOffset <value>`, ~1.7 s. It takes
    effect at once, and the output contains `Object value modified successfully`.
  - **Values are not in order:** `255` = Off, `0` = Low, `2` = Medium, `1` = High, `3` = Max.
- At idle, Off and Low give the same RPM, so **check by reading the setting back**, not by
  fan RPM.
- Local sensors: `sudo ipmitool sdr type temperature` (inlet, exhaust, CPU) and `sdr type fan`.
  The iDRAC has **no GPU temperatures** (CPU only).
- System power: `sudo ipmitool sdr type Current` (`Pwr Consumption`) or `sudo ipmitool dcmi
  power reading`: 238 W with both cards idle (2026-11-13). **Measured 2026-11-27:** idle
  231–243 W (median 235 over 24 h); both cards under sustained load at Medium 320–351 W
  (median 349), about +115 W. This is a load signal that doesn't depend on the NVIDIA driver
  (used in 7).
- Fan speeds (2026-11-27, 11–12°C inlet): Off and Low both ~7,200 RPM, idle and under load;
  Medium ~9,500 RPM, reached about a minute after the set. So at a cold inlet Low adds no
  airflow; it only raises the iDRAC's floor.
- Redfish has no settings endpoint on this firmware. Config export/import jobs take 60–80 s,
  so they're not usable here.
- Other iDRAC thermal settings (`ThermalProfile`, `MinimumFanSpeed`,
  `ThirdPartyPCIFanResponse=Enabled`) stay untouched.
- Dell rates the R730 for 35°C inlet (GPU configurations are excluded from the extended
  range). Expect summer inlets above what the fans can handle: that's an alert case.

**Hermes and the model** (trial run 2026-11-11/12)
- Hermes runs on the VM `hag` (Ubuntu 24.04) as the `hermes` user (no sudo, linger
  enabled). It was installed with Blank Slate setup (model, file
  operations, terminal only), terminal backend local, and `approvals: {mode: manual,
  cron_mode: deny}`. Its endpoint `t4-lab` is still in the old `custom_providers:` config
  format (`hermes doctor` warns; to be migrated before the first `hermes update`).
- Model: the lab llama-server on t4, port 8080 (`/v1`; base URL in `.env` as `LLM_URL`), model
  `qwen3.8-27b-iq3` (Qwen3.8-27B IQ3_S GGUF), thinking off, **`-c 65536 -np 1`**. It's
  **started by hand in tmux** and is down after any reboot of t4.
- One slot: Hermes and the VoiceProfile lab app (VM `vp2`) take turns.
- A fresh Hermes session sends ~7.3k tokens (~16 s). Prompt reading runs at ~380–440 tok/s;
  generation at ~17 tok/s.
- Trial result: correct tool calls. It analysed the two CSVs above (larger than its context)
  with commands and got every computed number right in 2.5–4 min. Two weak spots: a number
  it wrote from memory was garbled (an impossible timestamp), and it ignored a "five words"
  instruction.
- Approvals only cover commands matching Hermes's dangerous-pattern list
  (`~/.hermes/hermes-agent/tools/approval.py`). Inline interpreter code (`python3 -c …`,
  `python3 <<EOF`) is flagged; `python3 /path/script.py` is not. Under `cron_mode: deny`,
  flagged commands are refused immediately. A 5-minute timeout applies only in interactive
  sessions.

## 4. Architecture

```
            Telegram (user's phone)
              ▲ alerts            ▲▼ reports, changes, chat
              │                   │
 t4 ───────────────────────┐   hag (Proxmox VM) ─────────────────┐
 │ fan loop (systemd)      │   │ Hermes gateway (user service)   │
 │  nvidia-smi ──┐         │   │  cron: scheduled reviews        │
 │  ipmitool  ───┼→ decide │   │  fixed analysis scripts         │
 │  racadm (local) ←───────┤   │  Telegram chat                  │
 │  log + summaries        │◄──┤ HTTP: read status/history/      │
 │  HTTP endpoint (lab LAN)│   │       summaries, write thresholds│
 │  Telegram alert bot ────┘   └──────────┬──────────────────────┘
 │ llama-server :8080 ◄───────────────────┘ (model for Hermes)
 └─────────────────────────
```

**Split (decided):** a small deterministic **fan loop on t4** does all real-time control,
logging and urgent alerts. **Hermes on `hag`** reviews, tunes thresholds, reports, and chats.
Hermes never gets a shell on t4 or any iDRAC credentials. Its only access to the loop is the
loop's HTTP endpoint.

**Fan control stays on t4 with local racadm (decided 2026-11-12).** The GPU temperatures
only exist on t4. A remote set would put the iDRAC `root` login on Hermes's VM. And there
are fewer things that can fail.

## 5. The fan loop (on t4)

### 5.1 Inputs, every poll
- Per card: temperature, graphics clock, power, utilization, and the throttle flags
  `sw_power_cap`, `sw_thermal_slowdown` and `hw_thermal_slowdown` (`nvidia-smi --query-gpu`).
- iDRAC sensors: inlet and exhaust temperature, fan RPMs (local `ipmitool`). These can be
  read less often than GPU temps.
- The current offset: read **at startup**, **after every set** (to verify it), and
  periodically to notice manual changes (e.g. from the iDRAC web UI).

Poll interval: **5 s** *(my call)*. A racadm read takes ~2.4 s, so don't read the offset on
every poll.

### 5.2 Control rule
- Levels in order: **Off < Low < Medium < High < Max**. Decisions use the **hotter card**.
- **Step up** to the highest level whose step-up temperature the hotter card has reached.
  Several steps at once are fine.
- **Step down** one level at a time, only when the hotter card has stayed below
  (the current level's step-up temperature − margin) for the whole hold time. That's the
  normal rule, used when not busy; while busy the stricter rule below applies.
- **Busy** *(decided 2026-11-22)*: busy starts when either card has been above **45 W or 50%
  utilization for 10 s**, and ends when both cards have stayed below both for **60 s**.
  Power ties busy to heat; utilization catches loads that draw little power. Calibrated on
  the 2026-11-09 logs: idle 26–32 W (short spikes to 48 W right after load) and 0%;
  sustained load ~69 W median and 98–99%, with dips between requests of up to 17 s, which
  the 60 s end delay bridges. **Live test 2026-11-27:** a 15-min load gave exactly one
  episode, starting 20 s after the load began and ending 64 s after it stopped, as these
  numbers predict, so they stay. The numbers are a starting point, to be checked by replay (8)
  and the live test.
- **Stepping down while busy is stricter** *(decided 2026-11-22)*: while busy, step down from
  a level only when the hotter card has stayed below **the step-up temperature of the level
  below, minus the margin**, for the hold time. Never step from Low to Off while busy.
  With the starting thresholds:

  | Level | Step down when not busy | Step down while busy |
  |---|---|---|
  | High | below 73°C (78 − 5) | below 60°C (65 − 5) |
  | Medium | below 60°C | below 50°C |
  | Low | below 50°C | not while busy |

  Why: with the normal rule, a level whose steady temperature sits just above the next
  step-up point flaps under sustained load. Example: at a ~18°C inlet, Medium settles at
  ~78°C, steps up to High, High cools the card to ~70°C (an estimate: High wasn't tested),
  that's below 73°C so it steps back to Medium, and the cycle repeats every few minutes.
  With the stricter rule, High holds at ~70°C. But under a
  light load that keeps busy on while the cards cool down, the fans can still come down. It
  would flap only if neighbouring levels differed by 16°C or more under the same load; the
  tests measured 16°C between Medium and Max, two levels apart. That's a prediction, checked
  in the live test. Leaving Max (after the emergency rule) uses the normal step-down rule
  even while busy. Hermes can't change these rules.
- **Emergency rule (fixed, not tunable):** at ≥ 80°C on either card, or any thermal
  slowdown, go to **Max** immediately. Stepping down from Max uses the normal rule.
- **Ambient rule** (tunable, empty at start): rules of the form "if inlet ≥ X°C, use at
  least level L while the cards are busy". These may only **raise** the level, never lower
  it below what the temperature rule requires.
- If the loop stops or crashes, the offset stays where it was (decided). The cards protect
  themselves.
- **Manual hold** *(decided 2026-11-23)*: the user can set a **minimum level** with the CLI
  (5.9), e.g. "at least Medium during this VoiceProfile eval". Step-up and the emergency
  rule keep working; the level just never goes below the held one.
  - **Timed:** until a given time, at most **24 h** ahead.
  - **Until cancelled:** no end time and **no timeout**; it lasts until `hold off`. A daily
    reminder (5.8) keeps it from being forgotten. A hold is only a minimum, so a forgotten
    one costs noise and learning data, not safety.
  - A hold is stored, so a loop restart doesn't drop it.
  - Not supported, on purpose: a maximum ("keep it quiet"). It would work against the
    emergency rule and could swing between Off and Max.
- A change made outside the loop (e.g. in the iDRAC UI) is logged and alerted, then the loop
  takes control back. The runbook (5.10) says to use a hold, not the iDRAC UI. For a test at
  a fixed offset, stop the service: then there's no emergency rule either.

### 5.3 Starting thresholds *(my call)*

Based on the user's sketch: Low at ~55°C, Medium as the normal top, emergency at ~80°C.

| Level | Step up at (hotter card) |
|---|---|
| Low | 55°C |
| Medium | 65°C |
| High | 78°C *(decided 2026-11-23; was 76°C)* |
| Max | 80°C (emergency rule, fixed) |

Step-down margin **5°C**, hold time **3 min**.

**Why High at 78°C:** under sustained load at Medium the hotter card settles ~60°C above
inlet: 74°C at the tests' 14°C inlet, so ~77°C at the 17°C measured on 2026-11-13. With
High at 76°C, every sustained load would already end at High, and stay there until the load
ends (5.2). At 78°C, Medium stays the top level up to an inlet of about 18°C, instead of
about 16°C. It's safe: near its top, Medium climbs only ~1°C per minute (72 → 74°C took
131 s), and even at Low 78 → 80°C took 55 s, so High has minutes to act before the
emergency rule at 80°C; throttling starts at 84°C. 78°C is the hard-limit maximum, so
Hermes can only lower it, which is the direction warmer weather needs anyway.

Expected behaviour: idle Off (but see the open item below); under load, Low within ~20 s,
then Medium. Up to an inlet of about 18°C, Medium holds below High (at ~17°C, just: ~77°C).
Above that, sustained loads step up to High and stay there until the load ends. A short
burst steps back down a few minutes after it ends.

**Idle temperature (closed 2026-11-27, with the user).** 25 h of dry-run at Off: the hotter
card 40–41°C at an 11–12°C inlet, 14°C below Low's 55°C. No threshold changes. The earlier
53°C reading (2026-11-13) wasn't the steady idle. Only cold inlets are measured: at a summer
inlet idle may come close to 55°C; the ambient rules and Hermes's reviews cover that.

**Live test (2026-11-27, real control, 12°C inlet, 15-min sustained load):** Off → Low 20 s
into the episode (55°C), → Medium 55 s later (65°C); card 0 settled at 72–73°C (card 1
68–69°C) on Medium, 5°C below High; no thermal slowdown, decode steady at ~17.1 t/s; two
level changes under load. After the load: busy ended after 60 s, then Medium → Low → Off,
one level every 3 min 5 s. No alerts. Medium is near its limit at this inlet: a warmer inlet
takes the same load to High.

### 5.4 Hard limits (enforced by the loop, not by Hermes) *(my call)*
- Step-up temperatures between **45 and 78°C**, strictly increasing, at least **3°C** apart.
- Margin **3–10°C**. Hold time **60–900 s**.
- Ambient rules: inlet threshold 15–40°C, and they can only raise the level.
- The emergency rule (80°C or any thermal slowdown → Max) is not part of the thresholds
  and can't be changed through the endpoint.
- At most **2 threshold changes per 24 h from Hermes's own reviews**. **At most 6 `user-chat`
  writes per 24 h** *(decided 2026-11-22)*, so a misbehaving session can't churn the
  thresholds. `user-cli` writes have no limit. All sources go through the same hard limits.
- **These limits only guard against noise** *(decided 2026-11-22)*. Hermes labels its own
  writes, so a review change labelled `user-chat` passes as one (5.9). What actually bounds
  a bad change is the hard limits, the loop's own announcement of every change, and the
  one-command revert (5.9). The daily limits keep changes few enough for before/after
  comparisons to mean something.
- **How a write works** *(decided 2026-11-16)*:
  - A write is a list of changes. Each change names the parameter, its **current value
    (`from`)** and the new value (`to`), e.g. `step_up.medium`, 65 → 63. For ambient rules, a
    change adds, removes or replaces one rule.
  - If any `from` isn't the current value, the whole write is rejected. The writer must read
    the current values again and decide again. This stops lost updates (two writers working
    from the same old values) and catches a misremembered value.
  - A **`review` write contains exactly one change**, and counts as one toward the daily
    limit. User writes (`user-chat`, `user-cli`) may contain several changes, for changes
    that only work together (e.g. moving Medium before Low because of the 3°C gap).
  - Values the write doesn't name stay as they are.
- **Evidence gate for reviews** *(decided 2026-11-23)*: a `review` change toward **quieter** is
  rejected unless the current threshold version has at least **3 qualifying episodes**:
  each at least **6 min** long (sustained load settles within 2°C of its peak in 4.5–6 min
  in the 2026-11-09 logs), not overlapping a Hermes review mark (5.7), and not during a
  manual hold (5.2). A change toward
  **louder** needs no evidence; only the daily limit applies. Reason: too quiet means hotter,
  the anti-goal, so it waits for evidence; too loud only costs noise, so it can happen at
  once (e.g. reacting sooner after a thermal slowdown). Directions:

  | Parameter | Louder | Quieter |
  |---|---|---|
  | step-up temperature | lower | higher |
  | margin | larger | smaller |
  | hold time | longer | shorter |
  | ambient rule | add one, lower its inlet, raise its level | remove one, raise its inlet, lower its level |

  A revert follows the same rule: undoing a quieter change is louder (no gate); undoing a
  louder change is quieter (gated). `user-chat` and `user-cli` writes aren't gated.
- A write is checked as a whole against the hard limits, using the values that would result.
  An invalid write is rejected as a whole, with the reason; the previous thresholds stay.
- Every accepted write is logged with its timestamp, source, the reason text sent, and old →
  new for each change.

### 5.5 Recording
- Raw samples, every poll.
- Events: every level change (from, to, why), slowdown start and end, alerts, threshold
  changes, manual changes noticed, racadm errors.
- **Load episodes**, from the start of busy to its end (definition in 5.2). Every episode is
  recorded, even a ~20 s chat reply. The analysis skips episodes shorter than **6 min** when
  estimating how far the cards settle above inlet: in the 2026-11-09 logs, sustained load
  took 4.5–6 min to settle within 2°C of its peak. Each episode records:
  - start, duration, inlet (min/mean/max), peak temperature per card;
  - time to the first slowdown, seconds throttled;
  - time spent at each level, number of level changes;
  - whether the episode includes Hermes's own reviews (see 5.7);
  - whether a manual hold (5.2) was active during it, and at which level. A hold distorts
    level changes and time at loud levels, so the learning leaves these episodes out or
    weighs them separately;
  - and the **threshold version** in effect (see below). If a change lands during an
    episode, the episode records both versions.
- **Threshold versions** *(decided 2026-11-16)*: every accepted write creates a new numbered
  version holding the full set of values. Old versions are never edited. The highest
  version is the set in effect. Comparing before and after a change (6.2) means comparing
  the episodes of two versions. A revert is a new version with the old values.
- **The starting thresholds (5.3) live in the repo** and are deployed with the loop. On
  first start, with no stored thresholds, the loop stores them as version 1. From then on,
  the stored versions are the only source of truth; redeploying doesn't reset them.
- Retention: raw samples for **30 days**; events, episodes and threshold versions
  **indefinitely** *(my call)*. The learning needs a full summer.
- **Backup** *(decided 2026-11-16)*: a nightly copy of the loop's stored data to `docks`. The
  copy must be consistent even while the loop is writing (e.g. SQLite's backup API, not a
  plain file copy). If t4 is rebuilt, restoring it brings back the learned thresholds and
  the history. A failed backup is logged and alerted, rate-limited like other alerts.
  On `docks` the copy goes to a **dedicated backup account** (decided 2026-11-16). The loop
  user's SSH key there can only write into the backup folder: no shell, no forwarding
  (`restrict,command=…` in `authorized_keys`, e.g. with `rrsync`) *(my call)*.

### 5.6 HTTP endpoint (lab network)
- **Read:** current status (temperatures, level, inlet, current thresholds and their
  version, loop health), history over a time range (downsampled as needed), events,
  episodes, threshold changes since a given time or version, and summaries over a period.
  Hermes's scripts consume these. Output must be compact enough for the model's context
  (well under ~20k tokens per call).
- **Write:** apply a list of threshold changes (rules in 5.4); mark the start and end of a
  Hermes review (5.7).
- **The loop is the only writer of the thresholds** *(decided 2026-11-16)*. Every change,
  from Hermes, the CLI (5.9) or the starting values, goes through the loop and the same
  validation. The loop keeps the current thresholds in memory and stores each new version
  together with its change log entry, so the two can't disagree after a crash.
- Writes require a token *(my call)*. On `hag` it lives in a file readable only by `hermes`;
  on t4, in the loop's secrets (5.10). Never in a repo. Reads need no token *(my call)*: the lab network has a single user.

### 5.7 Hermes's own load
Hermes's reviews run on the same GPUs and heat them, and the loop can't tell whose load it
sees. So each review tells the loop when it starts and ends (the write call in 5.6). The loop
stores that interval as a **mark**, and episodes that overlap a mark are tagged, so the
learning can leave them out or weigh them separately. *(My call; the user agreed only that
these minutes should be tagged.)*

**Marks** *(decided 2026-11-22)*:
- A start returns a mark ID; the end names it.
- **The loop closes a mark by itself after 15 min** (reviews took 2.5–4 min in the trial),
  recorded as **timed out**, so the analysis knows its end time is only an upper limit.
  A missing end can then damage at most one review's worth of data.
- A start while a mark is open closes the open one (recorded as **superseded**) and opens
  the new one. An end with an unknown or already closed ID is logged and ignored.
- Each mark records how it ended (ended, timed out, superseded). A mark that didn't end
  properly counts as a failed review.
- **Who sends the calls:** not the model as separate steps. The first analysis script a review
  runs sends the start before anything else, and the script that writes Hermes's note
  (6.2 step 6) sends the end after saving it. The model can't do a review without the first
  script, so the start is always sent. **The plan first checks whether Hermes's cron can run
  commands before and after a job**, outside the model; if it can, those send the marks
  instead, and the timeout stays as a backstop.
- Chat with Hermes isn't marked: replies are short, and short episodes are already skipped
  for settling estimates (5.5).

### 5.8 Alerts to Telegram (sent directly by the loop)
Its own bot (token in a file owned by the loop user, mode 600; see "Secrets" in 5.10). It
messages only the user's chat.
t4 reaches `https://api.telegram.org` directly (checked 2026-11-12: HTTP 302 in 0.2 s), and
the user chose Telegram, so no relay is needed.
- **Alerts:** emergency rule triggered; thermal slowdown; a racadm set or verify failure (after
  retries); a manual change noticed; Max reached while the cards keep rising (the fans can't
  keep up: the user decides on load); the loop restarting; no completed Hermes review (below).
- **Review watchdog** *(decided 2026-11-22)*: alert when **no review has ended properly for
  13 h** (two missed 6-hourly reviews plus slack; one missed review can be harmless, e.g.
  llama-server's slot busy with a VoiceProfile test). "Ended properly" means its mark was
  closed by an end call, not timed out or superseded (5.7).
  - The time of the last properly ended review is stored, so restarts don't reset the clock:
    a reboot of t4 is exactly the case to catch, since llama-server is started by hand and
    is down after it. With no review yet (fresh install), the clock starts at the loop's
    first start.
  - The alert includes llama-server's health on t4 (`:8080`), its most likely cause, e.g.
    "No completed Hermes review for 13 h. llama-server on t4: not running".
  - One alert per incident, then one reminder a day while it lasts (not the 30-minute rate
    limit below). When a review ends properly again: one "back to normal" message.
  - The CLI (5.9) can mute it until a given date, for when Hermes is stopped on purpose. The
    mute expires by itself.
- **Manual holds** (5.2): a message when a hold starts and when it ends (cancelled or end
  time reached). While a hold without an end time is active, one reminder a day, e.g. "Hold
  at Medium since Mon 14:00, until cancelled. `fanctl hold off` to end it".
- **Threshold changes:** every accepted change, from any source (5.9), with the version
  numbers (e.g. "thresholds v7 → v8: Medium 65 → 63°C, source review: …").
- **Recovery:** one "back to normal" message per incident.
- **Clean stop** *(decided 2026-11-27)*: one message when the loop stops on purpose (systemd stop,
  a redeploy): the fans stay where they are and nothing watches the cards until it starts again.
  A crash or power cut can't send one; the next "started" message shows it.
- **No message per ordinary step change.** Repeated alerts of the same kind are rate-limited
  (e.g. at most one per 30 min per kind) *(my call)*.
- If Telegram can't be reached: log it and retry. Never block the control loop.

### 5.9 Who can change thresholds (decided 2026-11-12)
Three sources, each recorded with the change:
1. **`review`:** Hermes's scheduled reviews (6.2). Within the hard limits and the daily limit.
2. **`user-chat`:** the user asks Hermes on Telegram (e.g. "set Low to 50°C"). Hermes applies
   it through the same write endpoint. Hard limits apply, and its own limit of 6 writes per
   24 h (5.4); the review limit doesn't.
3. **`user-cli`:** a small command on t4 for the user to change thresholds without Hermes.
   It talks to the loop locally (no token needed) and never writes the stored data itself.
   It fills in `from` from the current values, so the user only gives the new value. It
   also shows the current thresholds and the change history, and mutes the review watchdog
   until a given date (5.8). There's no file to hand-edit.
   **`revert <version>`** *(decided 2026-11-22)* writes a new version with that version's
   values. It's a normal `user-cli` write: it shows which values it will change, including
   any later changes it undoes, and asks before applying.
   **`hold <level> [until <time>]`** sets a manual hold (5.2): until the given time (at most
   24 h ahead), or until cancelled when no time is given. `hold off` ends it, and the status
   shows the active hold.

The loop can't verify that a change labelled `user-chat` really came from the user: Hermes
holds the write token in every session. So **the loop's own Telegram bot announces every
accepted threshold change, whatever its source** (old → new, source, reason text). That's an
independent confirmation that doesn't depend on Hermes reporting honestly or correctly
*(my call)*. Hermes's chat reply and the loop's announcement will overlap; that's intended.
For `user-chat` changes the announcement says so plainly, with the way back *(decided
2026-11-22)*: "Hermes says you asked for this. If you didn't: `fanctl revert v7`".

Not in v1: the loop's bot asking the user to confirm a `user-chat` change before it applies
*(decided 2026-11-22)*. It would make those changes verified, but the loop would have to
receive Telegram messages (a new way in), and every chat change would need a second step.

### 5.10 Running it
- Language and storage are left to the builder. Prefer whatever t4 has: Ubuntu 22.04,
  Python 3.10, no Docker.
- It runs as a systemd service that starts at boot, as a **dedicated user, never the admin
  account on t4** (which has passwordless sudo for everything).
- **The service restarts forever** *(decided 2026-11-22)*: systemd must never give up after
  repeated crashes (by default it stops after a few quick ones, and then nothing alerts).
  The "loop restarted" alert is rate-limited, so a crash loop doesn't flood the chat.
- **Only `racadm` and `ipmitool` need root** (checked 2026-11-22: without sudo, racadm
  refuses with "Insufficient privilege level", and ipmitool can't open the root-only
  `/dev/ipmi0`; `nvidia-smi` works). The loop user may run exactly these 9 command lines
  with passwordless sudo, with full paths, no wildcards, and nothing else
  *(decided 2026-11-22)*:
  ```
  /usr/bin/racadm get system.thermalsettings.FanSpeedOffset
  /usr/bin/racadm set system.thermalsettings.FanSpeedOffset 255
  /usr/bin/racadm set system.thermalsettings.FanSpeedOffset 0
  /usr/bin/racadm set system.thermalsettings.FanSpeedOffset 2
  /usr/bin/racadm set system.thermalsettings.FanSpeedOffset 1
  /usr/bin/racadm set system.thermalsettings.FanSpeedOffset 3
  /usr/bin/ipmitool sdr type temperature
  /usr/bin/ipmitool sdr type fan
  /usr/bin/ipmitool dcmi power reading
  ```
  `/usr/bin/racadm` is a link to Dell's wrapper script
  (`/opt/dell/srvadmin/sbin/racadm-wrapper-idrac7`). The loop calls the commands exactly as
  listed. A test checks that each line works and that the same commands with other
  arguments are refused. If the loop later needs another command as root, it's added here
  as one more exact line.
- Not used: full sudo (root on t4 and iDRAC admin for anyone who compromises the loop user),
  access to `/dev/ipmi0` through a group (any IPMI command, so iDRAC admin), or running as
  root.
- With sudo in use, the service can't set systemd's `NoNewPrivileges=yes`. Any other
  hardening options have to be tested together with sudo.
- Deploy with **Ansible** in `~/ansible` (rule from `~/infra/CLAUDE.md`), so a rebuilt box
  can be set up again. This covers **both sides**: the loop on t4, and this project's pieces
  on `hag` (analysis scripts, Hermes cron jobs, the write-token file, which must match the
  loop's) *(my call, from that rule)*. **Hermes itself was installed by hand on purpose**
  (the user wanted to learn it), so the playbook assumes a working Hermes install and doesn't
  install or reconfigure it. The playbook also sets up the nightly backup to `docks` (5.5),
  including the backup account there.
- **Secrets** *(decided 2026-11-22)*:
  - On t4, the loop's secrets are files **owned by the loop user, mode 600**, in one config
    folder (e.g. `/etc/fan-controller/`): the Telegram bot token, the user's chat ID, the
    write token, and the SSH key for `docks`. Not root-only: the loop has to read them, and
    its sudo lines don't allow reading files.
  - The playbook takes them from the existing **Ansible vault**, `~/ansible/vars/secrets.yml`
    (vault password in `~/ansible/.avp`):
    - The bot token and chat ID: the user puts them there after creating the bot.
    - The write token: generated once and stored there. The playbook writes the same value
      to t4 and to `hag`, so the two always match.
  - The **SSH key for `docks`** is generated on t4 by the playbook. The private half never
    leaves t4 and isn't in the vault. The playbook installs the public half, with its
    restriction (5.5), in the backup account on `docks`. A rebuilt t4 gets a new key, and
    the playbook installs it again.
  - Hermes's own Telegram bot token stays in Hermes's config on `hag`, where the user put
    it. The playbook doesn't touch it.
- t4 isn't in `~/ansible/inventory.yml` yet; the playbook adds it. It must also apply the
  NVIDIA package holds and the disabled unattended-upgrades (section 3), so a rebuilt t4
  doesn't silently go back to automatic driver updates.
- **Dry-run mode:** decide and log, but don't set the offset. In dry-run even the emergency
  rule only logs; the runbook says so.
- **Runbook** *(decided 2026-11-23)*, written during the build, in this repo as
  `docs/runbook.md` (the user's choice for now, over `~/infra/docs/`; it holds no
  addresses). It covers: holding the fans higher (`fanctl hold`); a
  fixed-offset test (stop the service, set the offset with `racadm`, start the service
  afterwards; **while it's stopped there's no emergency rule**); switching dry-run on and
  off; changing and reverting thresholds; muting the review watchdog; restoring from the
  `docks` backup.

## 6. Hermes's part (on `hag`)

### 6.1 Setup
- The `hermes` user, gateway installed as a user service (`hermes gateway install`), linger on.
- **Telegram:** Hermes's own bot, **locked to the user's Telegram account only**. Get the
  exact setting from Hermes's Telegram gateway docs before setting it up; don't guess.
- Two-way chat from the phone (decided). Expect ~16 s for the first reply of a new
  conversation. Bot chats aren't end-to-end encrypted, so no secrets in them.
- Approvals stay `cron_mode: deny`. The review jobs must never need a flagged command, so:
  - **fixed analysis scripts**, stored on `hag` and called by path;
  - they read the loop's endpoint and print compact, pre-computed results;
  - Hermes's numbers come from their output, never from memory.

### 6.2 Scheduled reviews
- Every **6 h**, plus a **daily summary** message *(my call)*.
- Each review:
  1. Run the analysis scripts on the episodes and events since the last review (and longer
     windows where useful). The first one sends the start mark (5.7).
  2. Read their output; the numbers come from it, never from memory.
  3. Decide whether one threshold change is justified.
  4. If yes, read the current thresholds again and write the change to the loop, with the
     current value as `from`. If the write is rejected because a value changed meanwhile,
     go back to step 3 with the new values.
  5. Message the user on Telegram: what changed, the before/after values, and the evidence
     (decided: **applied without approval, then reported with reasoning**). The evidence
     includes the number of qualifying episodes, their length and their inlet range, e.g.
     "based on 4 episodes on v7 (6–22 min, inlet 12–15°C)", taken from the analysis script's
     output *(decided 2026-11-23)*.
  6. Write a note to its change log: what, why, when, and what to check next time. The
     script that saves the note sends the end mark (5.7).
- **Judging a change:** later reviews compare the episodes of the threshold version before
  it with those of the version it created (5.5). If it made things worse, Hermes reverts it,
  which is itself a change (reported, and counted in the daily limit).
- **What counts as better, in order:**
  1. no thermal slowdown;
  2. less time at loud levels;
  3. fewer level changes.

### 6.3 What "learning" means here
Hermes's model weights never change. The learning is measured experience, written down and
applied:
- For each level, estimate **how far the hotter card settles above inlet** under sustained
  load (about 60°C at Medium so far). Also estimate **how fast** temperatures respond after
  a step.
- From those, set the step-up temperatures and ambient rules so the right level is reached
  **early enough to avoid overshoot**. For example, at a 28°C inlet Medium would settle near
  88°C, so add an ambient rule to start at High.
- Predict when even Max won't be enough for the inlet temperature, and tell the user.
- **Mark estimates outside measured conditions as extrapolation.** Until summer data exists,
  nothing about 35–45°C inlets is measured, so keep hot-inlet behaviour conservative.
- Separate background knowledge (e.g. typical T4 limits) from what the logs show.
- Hermes's notes and change log are its memory between reviews.

## 7. Failure modes

| What fails | Effect | Handling |
|---|---|---|
| Fan loop crashes | Offset stays where it was | systemd restarts it, forever (5.10); "loop restarted" alert, rate-limited; cards self-protect |
| racadm set fails or doesn't verify | Level wrong | Retry, then alert. The emergency path keeps retrying Max |
| `nvidia-smi` fails for one card | Half the GPU temps | Control on the card that still reads. If only card 1 reads, add ~4°C (card 0 runs warmer). Alert |
| `nvidia-smi` fails for both cards | No GPU temps | Hold the current level and send one alert with the error text (rate-limited). While blind, if iDRAC system power stays above idle by a set amount for a set time, go to **Medium** (or higher if an ambient rule requires it). When power drops back, return to the held level. **Not Max:** `nvidia-smi` can fail with no load at all, and the cards protect themselves (slowdown, then shutdown), so the worst case is a throttled or crashed job, not a damaged card *(decided 2026-11-16)* |
| An external command hangs (`nvidia-smi`, `racadm`, `ipmitool`) | Without a limit, the whole loop stalls silently: no sets, no alerts, no logging. systemd still sees a live process | No call may block the loop: each has a timeout, and the loop doesn't wait for a stuck process to exit. A systemd watchdog restarts a stalled loop, which sends the "loop restarted" alert *(decided 2026-11-16)* |
| t4 down, or its network | Nothing to cool; no alerts either (the loop can't send, and Hermes has no model) | Not covered in v1: no thermal risk, the cards are off. A watcher on `hag` was considered and left out *(decided 2026-11-22)* |
| `hag`, Hermes or llama-server down | No reviews or chat | The loop and its alerts keep working. The review watchdog (5.8) alerts after 13 h without a completed review, with llama-server's health |
| Telegram unreachable | No messages | Log and retry, never block |
| Bad threshold change | Hotter or louder | Hard limits, one change at a time, before/after check and revert, user told every time |
| Inlet beyond the fans' capacity (summer) | Throttling even at Max | Alert; the user decides on load |

## 8. Testing
- **Replay:** run the control rule over the two recorded CSVs in dry-run and check the
  decisions. For example, on the Low log, Medium should be reached long before 84°C.
  The logs were recorded at fixed offsets, so a replay only shows when the loop would
  step up, not what happens after a step; the live test checks that.
- **Busy replay:** busy detection doesn't depend on the fan level, so replay is a full test
  of it. Each of the two logs must give exactly one episode, with no break in the middle.
- **Validation:** unit tests for the hard limits (each bound, ordering, rate limit, emergency
  rule not writable) and for writes: a stale `from` rejects the whole write, a `review`
  write with more than one change is rejected, a multi-change user write applies all or
  nothing, values not named stay unchanged, and each accepted write creates exactly one new
  version. The 7th `user-chat` write within 24 h is rejected, and `user-cli` writes aren't
  limited. Evidence gate: a quieter `review` change with fewer than 3 qualifying episodes is
  rejected; episodes under 6 min or overlapping a review mark don't count; a louder change
  passes without episodes; each parameter's direction (including each kind of ambient rule
  change) matches the table in 5.4; user writes aren't gated. Manual hold: the level never
  goes below the held one, step-up and the emergency rule still work, a timed hold ends at
  its time and can't be set more than 24 h ahead, a hold without an end time lasts until
  `hold off`, a restart keeps the hold, the daily reminder is sent, and episodes during a
  hold are tagged and don't count for the evidence gate. `revert <version>` creates a new version with exactly that version's values. For marks: a mark with no end closes after 15 min as timed out, a second start
  supersedes an open mark, and an end with an unknown ID changes nothing. For the review
  watchdog: it alerts after 13 h without a properly ended review (timed-out and superseded
  marks don't count), a restart doesn't reset its clock, it reminds once a day, it sends one
  "back to normal", and a mute stops it until its date and then expires.
- **Backup:** restore a backup from `docks` onto a scratch location and check that the loop
  starts from it with the same thresholds version and history.
- **Live:** dry-run first, then real control under a sustained load test
  (`data/fan-tests-2026-11-09/sustained_load.py`: back-to-back 300-token generations against
  llama-server). Check: no thermal
  slowdown, level changes as expected, the alerts arrive.
- **Hermes:** run one review by hand and check that its numbers match the endpoint's data.
  Then apply a deliberately harmless threshold change and check that the Telegram report
  arrives with its reasoning.
- On t4, tell the user before load tests: a VoiceProfile lab test may be running.

## 9. Left to the builder
- Language, storage format, HTTP framework, exact endpoint paths and payloads.
- How often iDRAC sensors are read. Adjusting the busy numbers (5.2) after replay and the
  live test, with the reason recorded.
- Analysis script design, and how Hermes's change log is stored.
- For "blind with load" (7): the power threshold above idle and how long it must last. Set
  from the live test (2026-11-27): idle 235 W, threshold +50 W (285 W), halfway between
  idle's highest reading (243 W) and load's lowest (320 W); held for 30 s.
- Timeout values, and how stuck child processes are cleaned up.
- How the CLI talks to the loop locally, and its command names.
- For the backup: the account and folder names on `docks`, how many copies to keep, and how
  a restore is done.

## 10. Related, outside this project
- llama-server has **no systemd unit** (VoiceProfile `tasks/lab-vm-setup.md`, item B). Hermes
  depends on it, so a unit with `-c 65536` would make Hermes survive reboots of t4.
- Migrate Hermes's `t4-lab` endpoint from `custom_providers:` to `providers:` before the first
  `hermes update`. Read the conversion code in the Hermes install first.
- Machine facts: the `gpu-inventory` skill, `servers/t4-lab.md`. Shared rules:
  `~/infra/CLAUDE.md`.
