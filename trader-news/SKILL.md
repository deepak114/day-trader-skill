---
name: trader-news
description: >-
  News Monitor for NSE stocks. Tracks breaking news on every stock you watch: Google
  News (India edition) headlines per company, NSE's bulk and block deals for your
  symbols, and how far each stock is from yesterday's close on Zerodha Kite, flagged at
  3 %. Shows only what is new since the last check, newest first in IST, tags each item
  (results, corporate action, order win, deal/stake, analyst, regulatory, management,
  promoter/pledge, legal/probe, fund-raise, block/bulk deal, macro) and never repeats
  one. Moneycontrol's subscriber news is read through /trader-moneycontrol in the
  browser. Use when the user says "any news on RELIANCE", "why is this moving", "news on
  my watchlist", "morning briefing", "pre-market news", "did anything break", "results
  today", or after /trader-scanner finds setups.
argument-hint: "[--symbols A,B,C] [--hours 24]"
---

# trader-news

`${CLAUDE_SKILL_DIR}` is this folder; `kite.py` comes from `../trader-kite`. Run
Python as `python` on Windows (`python3` elsewhere). Headlines and deals need no login;
the price moves need today's Kite session (skipped with a note otherwise).

```bash
python "${CLAUDE_SKILL_DIR}/news.py"                          # watchlist + portfolio, last 24 h, new only
python "${CLAUDE_SKILL_DIR}/news.py" --symbols RELIANCE,SBIN --hours 12
python "${CLAUDE_SKILL_DIR}/news.py" --all                    # include items already shown
python "${CLAUDE_SKILL_DIR}/news.py" --no-moves --json
```

## Steps

1. Run it on the watchlist (or the names the user gives).
2. Report the movers first, then the items that matter for a day trade: results and
   corporate actions today, block/bulk deals, regulatory or legal news, management
   changes. Say when a stock has results due today or tomorrow if a headline shows it:
   a day trade around a results announcement can gap past any stop.
3. For depth, open the source link, or use `/trader-moneycontrol` to read Moneycontrol's
   page in the browser. Summarise in your own words; do not paste article text.

## Safety

Headlines are leads, not facts, and not tips: open the source before acting on one.
Claude never places orders. Not investment advice.
