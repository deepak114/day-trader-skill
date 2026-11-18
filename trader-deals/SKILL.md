---
name: trader-deals
description: >-
  Deals Tracker for NSE. Reads NSE's public end-of-day bulk deal and block deal files
  (who bought or sold, how many shares, at what price, value in Rs crore) and today's
  F&O ban list, filters by size, client name (a mutual fund, an FII, a promoter
  entity), symbol, side, or your watchlist, and shows only new deals on request. Points
  to SEBI insider (PIT) and promoter disclosures on NSE's filings pages for reading in
  the browser. Use when the user says "bulk deals today", "block deals", "what did
  mutual funds buy", "who bought RELIANCE", "big deals", "F&O ban list", "is X in ban",
  "promoter selling", "insider trades", or as part of a morning briefing.
argument-hint: "[bulk | block | ban | new]"
---

# trader-deals

`${CLAUDE_SKILL_DIR}` is this folder. Run Python as `python` on Windows (`python3`
elsewhere). No login needed: these are NSE's public archive files.

```bash
python "${CLAUDE_SKILL_DIR}/deals.py" bulk                         # biggest first
python "${CLAUDE_SKILL_DIR}/deals.py" block --min-cr 50
python "${CLAUDE_SKILL_DIR}/deals.py" bulk --client "MUTUAL FUND" --side buy
python "${CLAUDE_SKILL_DIR}/deals.py" bulk --watch                 # only watchlist / portfolio symbols
python "${CLAUDE_SKILL_DIR}/deals.py" ban
python "${CLAUDE_SKILL_DIR}/deals.py" new                          # bulk + block not shown before
```

## Steps

1. Run what the user asked for; show the table it prints, then a two-line summary
   (the biggest buyers and sellers by value, any watchlist names).
2. For insider / promoter trades (SEBI PIT disclosures, SAST), open the NSE corporate
   filings pages for the company in the built-in browser and summarise; the scripts do
   not read those pages.
3. A name in the F&O ban list: no new F&O positions in it today (only reductions);
   `/trader-risk` blocks new ones.

## Safety

Deals are disclosures after the fact, not inside information and not tips. A fund
buying today says nothing certain about tomorrow. Not investment advice.
