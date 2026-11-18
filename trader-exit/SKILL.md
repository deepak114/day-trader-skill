---
name: trader-exit
description: >-
  Exit Planner for NSE equity, NFO futures and a single option. Sets the stop and the
  target BEFORE the entry: stop at the opening-range midpoint by default (or the far
  side of the range, your price, a percent, or a multiple of the 5-minute ATR), target
  at 2R by default (or 1R, 1.5R, a price, a percent), both rounded to the contract's
  tick size away from you, and a flat-by time ahead of Zerodha's MIS auto square-off
  (equity 15:00, F&O 15:15 IST by default). Works long or short; MIS, NRML or CNC.
  Refuses a stop on the wrong side, a target on the wrong side, a stop inside the
  spread, or reward:risk under your minimum, and says why. Writes trader/plan.json for
  the Time Machine, the Budget Manager and Price Alerts. Use when the user says
  "where's my stop", "set my stop and target", "plan the exit", "stop loss for this
  trade", "what's my R", "risk reward", or right after /trader-entry triggers. For
  option spreads and option selling use /trader-options instead.
argument-hint: "[--stop or-mid|or-far|price:X|pct:X|atr:X] [--target 2R|price:X|pct:X]"
---

# trader-exit

`${CLAUDE_SKILL_DIR}` is this folder; `kite.py` comes from `../trader-kite`. Run
Python as `python` on Windows (`python3` elsewhere).

```bash
python "${CLAUDE_SKILL_DIR}/exit.py" plan --write                       # from trader/entry.json
python "${CLAUDE_SKILL_DIR}/exit.py" plan --stop or-far --target 1.5R --write
python "${CLAUDE_SKILL_DIR}/exit.py" plan --stop atr:1.5 --target 2R --write
python "${CLAUDE_SKILL_DIR}/exit.py" plan --key NSE:SBIN --side buy --entry 842.5 --stop price:836 --target 2R --write
python "${CLAUDE_SKILL_DIR}/exit.py" plan --key "NIFTY FUT" --side sell --entry 25950 --stop price:25990 --target 2R --product NRML --write
python "${CLAUDE_SKILL_DIR}/exit.py" plan --key "NIFTY 26000 CE" --side buy --entry 118 --stop pct:30 --target 2R --write
python "${CLAUDE_SKILL_DIR}/exit.py" show
```

## Steps

1. Run `plan` (from `trader/entry.json`, or the flags). If the user has not said where
   the stop goes, use the default (opening-range midpoint) and say so.
2. Show the plan: entry, stop (risk per share / per lot), target, reward:risk, flat-by.
3. If it refuses, say the reason in plain words and offer the fix it names.
4. With `--write`, hand off to `/trader-timemachine` (optional) and `/trader-budget`.

## Indian market notes

- Stops are rounded to the tick (0.05 for most NSE/NFO contracts, 0.01 or 0.10 for some
  stocks) down for a long, up for a short; targets toward the entry.
- Product: MIS = intraday (auto squared off by Zerodha around 3:10 PM for F&O stocks in
  the closing auction, about 3:25 PM otherwise, with a charge); NRML = carry F&O
  overnight; CNC = equity delivery (cannot short).
- An option bought: the plan is on the premium, and the most you lose is the premium.
  An option sold alone is a naked short: the plan warns, and /trader-risk blocks it
  unless the config allows naked shorts. Use `/trader-options` for spreads.

## Safety

A stop is a plan, not a guarantee: a gap, a circuit limit or a fast market can fill past
it. Claude never places orders; the user does, in Kite. Not investment advice.
