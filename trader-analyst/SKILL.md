---
name: trader-analyst
description: >-
  Earnings Analyst for Indian listed companies. Reads a company's quarterly results
  (the results filing on NSE/BSE, or Moneycontrol's financials and results pages in the
  browser), records revenue, EBITDA, PAT, EPS and other income per quarter (Rs crore,
  Indian fiscal quarters Q1 = Apr-Jun), and computes the same way every time:
  quarter-on-quarter and year-on-year growth, EBITDA and PAT margins, the trailing
  twelve months, and plain-word flags (profit far ahead of revenue, other income a big
  share of profit). Keeps standalone and consolidated apart. Use when the user says
  "how were Reliance's results", "analyse the quarterly results", "Q2 results", "what
  changed this quarter", "is revenue growing", "compare margins", "results preview",
  or before trading a stock around its results date.
argument-hint: "[SYMBOL]"
---

# trader-analyst

`${CLAUDE_SKILL_DIR}` is this folder. Run Python as `python` on Windows (`python3`
elsewhere). Data: `trader/fundamentals/<SYMBOL>.json`.

```bash
python "${CLAUDE_SKILL_DIR}/facts.py" add RELIANCE --quarter Q2FY27 --revenue 258000 --ebitda 43800 \
    --pat 18200 --eps 13.45 --other-income 4100 --consolidated --source "NSE results filing, 2026-10-17"
python "${CLAUDE_SKILL_DIR}/facts.py" show RELIANCE --consolidated
python "${CLAUDE_SKILL_DIR}/facts.py" changes RELIANCE --consolidated
python "${CLAUDE_SKILL_DIR}/facts.py" list
```

## Steps

1. Find the numbers. Best source: the company's results filing (the PDF on
   nseindia.com or bseindia.com corporate announcements). Second: Moneycontrol's
   quarterly results page for the company, in the built-in browser (see
   `/trader-moneycontrol`). Use the same basis (consolidated or standalone) for every
   quarter, and at least the latest quarter, the one before, and the same quarter last
   year (5 quarters gives margins a year apart; 8 gives two years).
2. `add` each quarter with `--source` naming where the numbers came from. Revenue =
   revenue from operations; PAT = profit after tax attributable to owners if shown.
3. `show` and `changes`, then explain in plain words what changed and what to check
   (one-offs, exceptional items, tax, other income).
4. If results are due within the next session or two, say so: a day trade held into
   the announcement is exposed to the gap.

## Safety

These are reported numbers and arithmetic, not a valuation or a recommendation.
Check the filing before relying on any number. Not investment advice.
