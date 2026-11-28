---
name: fan-controller
description: How hot is it now = t4 GPU temps, fans and fan reviews.
---

# t4 fan controller

## What this is

In this chat, "how hot is it", temperature, heat and fan questions mean t4's GPUs (the Dell R730
lab box with two passively cooled Tesla T4 cards), not the weather, unless the user names a place.

A loop on t4 sets the R730's fan offset (Off, Low, Medium, High, Max) every few seconds from the
hotter T4 card's temperature, the inlet temperature and its thresholds; at 80°C it goes to Max on its
own. The loop decides in real time; you review how its thresholds worked, tune them, and report.

## Commands

Run these exactly as written, one command per call. Never run inline Python code, heredocs or pipes:
scheduled runs refuse them.

| Command | What it prints |
|---|---|
| `python3 /home/hermes/.hermes/scripts/fan.py status` | the level now, card and inlet temperatures, thresholds, problems |
| `python3 /home/hermes/.hermes/scripts/fan.py episodes --days 7` | episode stats and how far above inlet the cards settle per level |
| `python3 /home/hermes/.hermes/scripts/fan.py compare 7 8` | v8 against v7, and a verdict: better, worse, same, not enough data |
| `python3 /home/hermes/.hermes/scripts/fan.py changes --days 7` | one line per threshold change: version, source, values, reason |
| `python3 /home/hermes/.hermes/scripts/fan.py write --source review --reason TEXT step_up.medium 65 63` | `Applied:`, or `Rejected (...)` with the current values |
| `python3 /home/hermes/.hermes/scripts/fan.py note /home/hermes/fan-controller/note.md` | saves your note and records the end of the review |

Parameters for `write` (PARAM FROM TO, FROM is always the current value):
- `step_up.low`, `step_up.medium`, `step_up.high`: the hotter card's temperature that steps up to that
  level, 45–78°C, each at least 3°C above the one below.
- `margin` (3–10°C): how far below a step-up the temperature must fall to step down.
- `hold_s` (60–900, whole seconds): how long a level is held before stepping down.
- `ambient.add none 30:high`, `ambient.remove 30:high none`, `ambient.replace 30:high 28:high`: a floor
  level while the inlet is at or above a temperature (15–40°C, at most 8 rules).

Exit codes: 0 done, 2 the loop rejected the write (nothing changed), 1 an error (one line: read it).

## Rules for numbers

Use only numbers printed by these commands or by the review data in your prompt. Never compute,
estimate or remember a number yourself, and never reuse one from an earlier conversation: run the
command again.

## A scheduled review

1. The review data is already in your prompt (fan_review.py printed it and sent the start mark).
   Don't run `status` again unless you need a value after a write.
2. Judge your last change first. If the data shows one since your last notes, run
   `compare <old> <new>`. If the verdict is `worse`, revert it, as one change, and make no other.
3. Otherwise decide whether **at most one** change is justified. If there was a thermal slowdown,
   prefer the safer, louder direction (a lower step-up, a longer hold, an ambient floor).
4. A quieter change (higher step-up, shorter hold, smaller margin, removed floor) needs 3 qualifying
   episodes on the current version. The loop enforces this and rejects it otherwise; don't argue.
5. "Better" means, in this order: no thermal slowdown; then less time at High and Max; then fewer
   level changes.
6. The data covers only the inlet temperatures measured so far. Anything about hotter inlets (summer)
   is an extrapolation: say so in the note and the message.
7. No change is a fine outcome. Most reviews end with no change.

## Writing a change

- `write --source review`, with the current value as FROM, exactly as printed.
- Keep `--reason` short and plain, without quotes or apostrophes: the loop announces it as written.
- If the reply is `Rejected (...)`, read the current values it prints and decide again from them. The
  loop won't take a second try with the same value. At most 2 review changes in 24 h.
- Never say a change was made unless `Applied:` was printed.

## The note

The last command of every review, even one with no change:
1. Write the note with the file tool to `/home/hermes/fan-controller/note.md`.
2. Run `python3 /home/hermes/.hermes/scripts/fan.py note /home/hermes/fan-controller/note.md`.

Format, four lines:

```
What: no change (or: Medium 65 → 63, v7 → v8)
Why: one or two sentences
Evidence: the numbers from the data, e.g. 4 episodes on v7, 6–22 min, inlet 12–15°C
Check next time: what the next review should look at
```

Your last three notes come back in the next review's data.

## The message

Your final reply is delivered to the user on Telegram.

- After a change or a revert, send:
  - what changed (`Medium 65 → 63°C, v7 → v8`) and why;
  - the evidence, e.g. `based on 4 episodes on v7 (6–22 min, inlet 12–15°C)`;
  - what you'll check next.
- Also send a message for a rejected write you couldn't resolve, a thermal slowdown in the data, or
  data suggesting Max won't be enough.
- Otherwise your whole reply is `[SILENT]`, with nothing before or after it: your reasoning is
  already in the note, and the daily summary covers quiet periods.
- If the review data couldn't be read, or `fan.py note` failed, put `[CRON_FAILURE]` alone on line 1,
  then one line saying what failed.

## In chat

- "How hot is it now?": `fan.py status`. "What changed this week?": `fan.py changes --days 7`.
- A change the user asks for: `write --source user-chat`, with `--reason` quoting what they asked.
  Values that only work together (moving Low and Medium because of the 3°C gap) go in one write as
  several PARAM FROM TO triples. At most 6 such writes a day; the loop enforces it.
- Before writing, say what you'll change; after, quote the `Applied:` line.
- Chat is never a review: no mark, no note.
- Never put the write token, or any address, in a reply.
