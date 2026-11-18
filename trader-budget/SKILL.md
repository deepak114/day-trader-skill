---
name: trader-budget
description: >-
  Budget Manager for NSE / NFO. Sizes the plan from trader-exit or trader-options in
  whole lots (shares for equity) to your rules: max loss per trade in rupees, max lots
  per trade (index vs stock), optional max quantity and notional, and the margin you
  have (Kite's own margin calculator and funds when signed in, with the hedge benefit
  for spreads; otherwise a flagged estimate), keeping a cash buffer. Says which limit
  decided the size, and refuses when even one lot risks more than your max loss. Gets
  the round-trip charges (brokerage, STT, exchange, SEBI, stamp duty, GST) from Kite's
  contract-note calculator. Writes the trade's Kite orders, in the order you place
  them, to trader/orders.json: entry, SL stop-limit, and the target as an alert (MIS)
  or a GTT OCO (NRML/CNC); for strategies the hedge legs before the short legs. Places
  nothing. Use when the user says "how many lots", "how many shares", "size this
  trade", "I have 3 lakh", "I only want to risk 2,000 a trade", "how much margin", or
  after /trader-exit or /trader-options.
argument-hint: "[--max-loss RUPEES] [--capital RUPEES]"
---

# trader-budget

`${CLAUDE_SKILL_DIR}` is this folder; `kite.py` comes from `../trader-kite`. Run
Python as `python` on Windows (`python3` elsewhere). Rules come from
`trader/config.json` (copy `../trader-kite/config.example.json`).

```bash
python "${CLAUDE_SKILL_DIR}/size.py" --write                   # trader/plan.json -> trader/orders.json
python "${CLAUDE_SKILL_DIR}/size.py" --max-loss 2500 --write
python "${CLAUDE_SKILL_DIR}/size.py" --limit 1512.5 --write    # enter with a LIMIT order
python "${CLAUDE_SKILL_DIR}/size.py" --no-kite                 # estimates only
```

## Steps

1. If `trader/config.json` is missing, ask the user for: capital (or read it from
   Kite), max loss per trade, daily loss limit, max trades a day, max lots, and which
   segments they trade. Write the file from `../trader-kite/config.example.json`.
2. Run `size.py --write`. Show the size, the max loss, the reward, the margin and its
   source, the charges, what decided it, and the orders in sequence.
3. On a refusal, say it plainly: there is no smaller order than one lot.
4. Hand off to `/trader-risk`.

## The limits (all must hold; the smallest wins)

| limit | from config | meaning |
| --- | --- | --- |
| max_loss_per_trade | rupees | loss at the stop (a spread: its worst case) |
| max_lots | {"index": 2, "stock": 1} | F&O lots per trade |
| max_quantity / max_notional | optional | equity shares / position value |
| margin | Kite funds less cash_buffer_pct | Kite's margin number for the trade |

## Orders it writes (Kite field names)

- Single MIS: 1 entry (MARKET or LIMIT), 2 stop as SL (stop-limit; NSE takes no SL-M on
  options) with the limit `sl_limit_buffer_pct` beyond the trigger, 3 target alert,
  4 flat-by alert. Kite has no OCO for intraday orders, so the target is an alert:
  when it rings, modify the stop order to a LIMIT at the target.
- Single NRML/CNC: 1 entry, 2 a GTT two-leg (OCO) with the stop and the target.
- Strategy: BUY legs first (hedge, lower margin), then SELL legs, LIMIT at the plan's
  prices; exits are alerts on the net value.

## Safety

Sizing is arithmetic on your own rules, not advice on what to trade. Nothing is placed:
the user places every order in Kite. Margin estimates are flagged as estimates; option
selling is not sized without Kite's number. Not investment advice.
