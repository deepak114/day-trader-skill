---
name: trader-risk
description: >-
  Risk Manager for NSE / NFO. Checks every trade in trader/orders.json against your
  limits before it can become a ticket, and prints PASS or BLOCK with the reasons: a
  stop is required; loss at the stop (a spread's worst case) within max loss per trade,
  re-measured from the live Kite price; reward:risk minimums (separate one for credit
  spreads); the daily loss limit (today's realised P&L plus risk still open); trades
  per day; the entry window in IST, market open, NSE holidays and MIS flat-by;
  segment permissions (equity shorts, futures, option buying, option selling, naked
  shorts); no new short options on expiry day; NSE's F&O ban list; blocked symbols;
  whole lots, max lots, tick grid, no CNC shorts, no SL-M on options; margin against
  your funds; and duplicates. Exits are never blocked by a day limit. Records the
  verdicts so trader-broker only shows tickets that passed. Use when the user says
  "check my order", "is this safe", "risk check", "does this break my rules", "can I
  take this trade", "how much have I risked today", or before /trader-broker, always.
argument-hint: ""
---

# trader-risk

`${CLAUDE_SKILL_DIR}` is this folder; `kite.py` comes from `../trader-kite`. Run
Python as `python` on Windows (`python3` elsewhere).

```bash
python "${CLAUDE_SKILL_DIR}/check.py"                 # orders + config + Kite (funds, positions, LTP)
python "${CLAUDE_SKILL_DIR}/check.py" --no-kite       # the rules only
```

Exit code 0 = every trade passed, 3 = something was blocked.

## Steps

1. Run `check.py`. Show each trade's PASS or BLOCK line, every `x` reason and every
   `!` warning, in plain words.
2. On PASS, hand off to `/trader-broker` (ticket). On BLOCK, say what would fix it
   (tighten the stop, fewer lots, wait for the window, a spread instead of a naked
   short) and that the user decides. Never edit the config or the orders to make a
   trade pass unless the user asks for that specific change.

## Where the numbers come from

- `trader/config.json`: max_loss_per_trade, daily_loss_limit, max_trades_per_day,
  min_reward_risk, min_reward_risk_credit, no_entries_before / no_entries_after,
  max_lots, allow {...}, no_new_shorts_on_expiry_day, respect_fo_ban, blocked,
  cash_buffer_pct.
- `trader/state/broker.json`: the trades the user reported placed today and the P&L
  closed (from `/trader-broker placed` / `closed`); with Kite signed in, today's
  positions P&L as well (read only).
- NSE's F&O ban list for today (nsearchives.nseindia.com/content/fo/fo_secban.csv).

## Safety

A PASS means the trade fits the user's own rules, not that it is a good trade.
Claude never places orders. Not investment advice.
