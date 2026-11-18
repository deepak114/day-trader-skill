---
name: trader-scanner
description: >-
  Chart Scanner for NSE. Scans the F&O stock universe (about 220 liquid names, or
  every NSE stock) on Zerodha Kite candles for setups, long and short: 20-day
  breakouts and breakdowns, bull and bear flags, reclaiming or losing the 20-day
  average, oversold / overbought RSI, oversold bounces; intraday opening-range
  breakouts and breakdowns on 5-minute bars from 9:15 IST; and an F&O open-interest
  scan of near-month futures (long buildup, short buildup, short covering, long
  unwinding). Writes trader/watchlist.json for trader-news and trader-entry. Use when
  the user says "scan the market", "find setups", "what is breaking out", "opening
  range breakouts", "ORB scan", "bull flags", "oversold stocks", "long buildup",
  "short buildup", "OI scan", "pre-market scan", "build my watchlist", or at the start
  of the trading day before /trader-news.
argument-hint: "[daily | orb | buildup]"
---

# trader-scanner

`${CLAUDE_SKILL_DIR}` is this folder; `kite.py` comes from `../trader-kite`. Run
Python as `python` on Windows (`python3` elsewhere). Needs today's Kite session
(`/trader-kite`); if a command says the session is missing, ask the user to run
`kite.py login` themselves.

```bash
python "${CLAUDE_SKILL_DIR}/scan.py" universe                     # F&O stocks -> trader/universe.txt
python "${CLAUDE_SKILL_DIR}/scan.py" daily --write                # before 9:15 IST
python "${CLAUDE_SKILL_DIR}/scan.py" daily --setup breakout_20d,bull_flag --write
python "${CLAUDE_SKILL_DIR}/scan.py" orb --write                  # after about 9:35 IST
python "${CLAUDE_SKILL_DIR}/scan.py" orb --at 10:15 --side buy --write
python "${CLAUDE_SKILL_DIR}/scan.py" buildup --write              # F&O OI vs yesterday
python "${CLAUDE_SKILL_DIR}/scan.py" daily --symbols RELIANCE,SBIN,INFY
```

## Steps

1. Morning (before 9:15): `daily --write`. About 220 symbols at Kite's 3 candle
   requests a second takes roughly 80 seconds; `--set all` (2,000+ stocks) takes
   about 12 minutes, so suggest it only when the user asks.
2. After the opening range (15 minutes by default, so from about 9:35): `orb --write`.
3. For F&O traders: `buildup --write` reads open interest of every near-month future.
4. Show the hits as a short table: symbol, long/short, setup, the numbers. Then hand
   off to `/trader-news` for the names the user picks, and `/trader-entry`.

## What the setups mean (from closes, oldest first)

- breakout_20d / breakdown_20d: last close above (below) every close of the 20 before it
- bull_flag / bear_flag: a >10 % run over 10 sessions, then the last 5 closes inside a 5 % range
- reclaim_ma20 / lose_ma20: crossed back above (below) the 20-day average today
- oversold / overbought: RSI 14 under 30 / over 70; oversold_bounce: under 30 yesterday, up today
- orb: the first completed 5-minute bar after the range that closes beyond it
- buildup: price change vs open-interest change on the near-month future

Floors: price >= Rs 50 and 20-day average turnover >= Rs 10 crore (`--min-price`,
`--min-turnover`). Today's daily candle is ignored until 15:30.

## Safety

A scan is a list of charts that match a rule, not a list of stocks to buy or sell, and
not a recommendation from Claude. Claude never places orders; nothing here can.
Not investment advice.
