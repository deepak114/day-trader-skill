---
name: trader-options
description: >-
  Options Strategist for NFO and BFO (Nifty, Bank Nifty, Fin Nifty, Midcap Nifty,
  Sensex and F&O stocks). Reads the live option chain from Zerodha Kite (OI, volume,
  IV, delta, PCR, max pain) and builds a trade: long call or put, bull call / bear put
  debit spreads, bull put / bear call credit spreads, iron condor, iron fly, or custom
  legs; picks strikes by steps from ATM or by delta; prices each leg at the touch (ask
  to buy, bid to sell); works out max profit, max loss and breakevens per lot at expiry;
  sets stop and target on the strategy's net value; gets Kite's basket margin with the
  hedge benefit; and writes trader/plan.json for the Budget and Risk Managers. Naked
  short straddles and strangles are refused unless the user's config allows naked
  shorts. Use when the user says "option chain", "build a spread", "bull call spread
  on Nifty", "sell a put spread", "iron condor", "which strike", "PCR", "max pain",
  "options strategy", "hedge my view", or "option selling".
argument-hint: "[chain NAME | build NAME --strategy ...]"
---

# trader-options

`${CLAUDE_SKILL_DIR}` is this folder; `kite.py` comes from `../trader-kite`. Run
Python as `python` on Windows (`python3` elsewhere). Needs today's Kite session for
live quotes and margins.

```bash
python "${CLAUDE_SKILL_DIR}/options.py" chain NIFTY --strikes 8
python "${CLAUDE_SKILL_DIR}/options.py" build NIFTY --strategy bull-call-spread --width 2 --write
python "${CLAUDE_SKILL_DIR}/options.py" build NIFTY --strategy bull-put-spread --otm 2 --width 2 --write
python "${CLAUDE_SKILL_DIR}/options.py" build BANKNIFTY --strategy iron-condor --short-delta 0.2 --width 2 --write
python "${CLAUDE_SKILL_DIR}/options.py" build RELIANCE --strategy long-call --stop 40 --target 2R --write
python "${CLAUDE_SKILL_DIR}/options.py" build NIFTY --legs "BUY 26000 CE, SELL 26200 CE" --expiry 2026-12-01 --write
python "${CLAUDE_SKILL_DIR}/options.py" build NIFTY --strategy bull-put-spread --product NRML --write
python "${CLAUDE_SKILL_DIR}/options.py" show
```

## Steps

1. If the user has a view but no strategy, show the chain first and lay out the
   choices that fit the view with their trade-offs (debit = pay to own the move,
   capped loss; credit = get paid if the move does not happen, capped by the hedge).
   Present choices, not a recommendation of what to trade.
2. `build ... --write` and show: legs with prices, net debit/credit, max profit /
   max loss / breakevens per lot, the exits on the net value, the margin.
3. Hand off to `/trader-budget` (lots), then `/trader-risk`.

## Strike selection

`--otm N` = N strikes out of the money from ATM (default 0 for debit trades, 1 for
credit), `--width N` = strikes between the short and the hedge. `--short-delta 0.2`
picks short strikes by delta (Black-Scholes IV from the last price; rate from
`risk_free_rate`, default 6.5 %). Strike gaps (50 for Nifty, 100 for Bank Nifty, and
so on) come from Kite's instrument list.

## Exits on net value (per unit)

- Debit: stop when the position is worth `--stop` % less than the debit (default 50),
  target at `--target` R (default 1R), capped below the spread's width.
- Credit: stop when closing costs `--stop-x` times the credit (default 2.0, capped at
  the worst case), target when `--capture` % of the credit is kept (default 50).
- `/trader-alerts add --from-plan` watches the net value from live leg prices.

## Indian F&O rules to keep in mind

- Lot sizes and expiries come from Kite's daily instrument list (Nifty is 65 a lot
  at the time of writing; NSE revises lots). Weekly index expiry: Nifty on Tuesday,
  Sensex on Thursday; a holiday moves it a day earlier.
- On expiry day: extra exposure margin on short options, and /trader-risk blocks NEW
  short options (`no_new_shorts_on_expiry_day`).
- Margin for selling is blocked upfront (SEBI peak margin). The Kite basket number
  includes the hedge benefit only when the hedge leg is placed first: /trader-budget
  orders BUY legs before SELL legs.
- Stock options are physically settled if held into expiry in the money: close them before.

## Safety

Option sellers can lose many times the premium they collect; a spread caps it at the
width less the credit. The worst case at expiry is the real risk, because a gap can
jump any stop. Claude never places orders; the user does, in Kite, leg by leg. Not
investment advice.
