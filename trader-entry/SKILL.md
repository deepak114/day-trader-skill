---
name: trader-entry
description: >-
  Entry Timer for NSE and NFO. Watches one stock, future or option bar by bar on
  Zerodha Kite candles (1, 3, 5, 10 or 15 minutes) and says the moment to get in: an
  opening-range breakout (long) or breakdown (short) from 9:15 IST, or a close beyond a
  level you give it (a scanner's 20-day high, or a Moneycontrol idea's entry), inside
  your entry window (default 9:30 to 14:30 IST), with an optional volume check. Says
  waiting and how far, triggered with the time and price, expired, forming, or closed
  for an NSE holiday, and writes trader/entry.json for /trader-exit. Never places an
  order. Use when the user says "tell me when to get in", "watch RELIANCE", "has it
  broken out", "did Nifty futures break the range", "entry signal", "time my entry",
  "check my watchlist", or after /trader-scanner, /trader-moneycontrol and /trader-news.
argument-hint: "[symbol, e.g. NSE:RELIANCE or 'NIFTY FUT']"
---

# trader-entry

`${CLAUDE_SKILL_DIR}` is this folder; `kite.py` comes from `../trader-kite`. Run
Python as `python` on Windows (`python3` elsewhere). Needs today's Kite session.

Symbols: `NSE:RELIANCE`, `RELIANCE` (NSE equity), `"NIFTY FUT"` (nearest future),
`"RELIANCE FUT 2026-12-29"`, `"NIFTY 26000 CE"` (nearest expiry), or a Kite key like
`NFO:NIFTY26NOV26000CE`. An index itself (NIFTY 50) cannot be traded: use its future
or an option.

```bash
python "${CLAUDE_SKILL_DIR}/entry.py" check NSE:RELIANCE                      # ORB long, 15 min, 5-min bars
python "${CLAUDE_SKILL_DIR}/entry.py" check "NIFTY FUT" --side sell           # ORB short
python "${CLAUDE_SKILL_DIR}/entry.py" check NSE:SBIN --setup level --level 842.5 --write
python "${CLAUDE_SKILL_DIR}/entry.py" check NSE:TATASTEEL --min-volume-x 1.5
python "${CLAUDE_SKILL_DIR}/entry.py" watch NSE:RELIANCE --every 30 --write   # until triggered / expired
python "${CLAUDE_SKILL_DIR}/entry.py" watchlist --write                       # every trader/watchlist.json item
```

## Steps

1. `check` the symbol. Report the one line it prints: TRIGGERED (with the bar, price
   and IST time), WAITING (and how far), FORMING, EXPIRED or CLOSED.
2. For a live wait, `watch` (run it in the background for long waits, and tell the
   user it is running). It checks each bar a few seconds after it completes.
3. On TRIGGERED with `--write`, `trader/entry.json` is ready: hand off to `/trader-exit`.

## Rules it uses

- Opening range = high and low of the first `--or-minutes` after 9:15 IST.
- Trigger = the first COMPLETED bar after the range whose CLOSE is beyond the range
  (or `--level`), and whose end time is inside `no_entries_before`/`no_entries_after`
  from `trader/config.json`. A wick through the level is not a trigger.
- `--min-volume-x 1.5`: the trigger bar must also trade 1.5x the session's average bar
  volume so far (ignored for indices).

## Safety

A trigger is a rule firing, not a recommendation. Claude never places orders: the
user does, in Kite, after /trader-exit, /trader-budget and /trader-risk. Not
investment advice.
