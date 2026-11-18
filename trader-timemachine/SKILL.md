---
name: trader-timemachine
description: >-
  Time Machine for NSE day trades. Takes the plan from trader-exit (entry, stop,
  target, flat-by), rebuilds the rest of the session 10,000 times from the
  instrument's own 5-minute Kite candles over its last 60 or so full NSE sessions
  (9:15 to 15:30 IST, 75 bars), and re-runs the same opening-range rule on every past
  session. Prints the odds of target first, stop first, or neither by the flat-by time,
  where the price sits then, the average R, the history, and one plain verdict. Can
  draw the bars from the underlying for a future or option (--pool). Use when the user
  says "run the time machine", "replay this trade", "what are the odds", "simulate this
  trade", "monte carlo", "backtest this setup", "how often does this hit the target",
  "is this a coin flip", or after /trader-exit writes a plan, before /trader-budget.
argument-hint: "[run | chart]"
---

# trader-timemachine

`${CLAUDE_SKILL_DIR}` is this folder; `kite.py` comes from `../trader-kite`. Run
Python as `python` on Windows (`python3` elsewhere). Needs today's Kite session.

```bash
python "${CLAUDE_SKILL_DIR}/timemachine.py" run --write                 # trader/plan.json
python "${CLAUDE_SKILL_DIR}/timemachine.py" run --until 15:00
python "${CLAUDE_SKILL_DIR}/timemachine.py" run --pool NSE:RELIANCE      # a RELIANCE future: use the stock's bars
python "${CLAUDE_SKILL_DIR}/timemachine.py" run --seed 7 --replays 20000
python "${CLAUDE_SKILL_DIR}/timemachine.py" chart --from                 # ASCII fan chart of the saved run
python "${CLAUDE_SKILL_DIR}/timemachine.py" run --key NSE:SBIN --date 2026-11-18 --entry 842.5 --stop 838 --target 851.5 --entry-time 09:45
```

## Steps

1. `run --write` on the plan. It stops at the plan's flat-by time unless `--until`.
2. Report: target first %, stop first %, neither %, the price range at the end, the
   average R, the history line, and the verdict line word for word. Then the note.
3. Hand off to `/trader-budget`.

## How to read it

- The verdict compares how often the target came first (among replays that hit a side)
  with the share that breaks even at the plan's R: 50 % at 1R, 33 % at 2R.
- A new futures contract or an option has a short or mixed history: use `--pool` with
  the underlying (`NSE:<stock>`, or the near future for an index) and say that the
  odds are the underlying's, not the premium's.
- Option strategies (spreads, condors) are not replayed here: their value depends on
  IV and time as well as price.

## Safety

A simulation built from recent bars is not a prediction: results, RBI policy, global
cues, circuit limits and F&O ban periods can do what those sessions never did. Same
seed, same numbers. Not investment advice; Claude never places orders.
