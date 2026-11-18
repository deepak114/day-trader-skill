---
name: trader-broker
description: >-
  Order Tickets for Zerodha Kite. Turns each trade that /trader-risk passed today into
  a plain ticket with every field Kite's order window asks for, in the order to place
  them: exchange and trading symbol, BUY/SELL, quantity in lots, product (Intraday MIS,
  Overnight NRML, Delivery CNC), order type (Market, Limit, SL stop-limit), price and
  trigger, validity, plus the GTT OCO or the alerts that handle the target and the
  flat-by time. The user places the orders in Kite themselves; Claude never places,
  modifies or cancels an order. Then it keeps the trade log: "placed" with the fill
  price, fills synced read-only from Kite, "closed" with the P&L, so the daily loss
  limit stays right; and builds exit tickets to flatten MIS positions before the
  auto square-off. Use when the user says "place the order", "buy it", "execute",
  "give me the ticket", "how do I enter this in Kite", "I placed it", "I got filled at",
  "I exited", "close my position", "flatten", "square off", or after /trader-risk passes.
argument-hint: "[show | placed --trade T1 | closed --trade T1 | flatten | log]"
---

# trader-broker

The user places every order in Kite (web or app). This skill prepares the ticket and
keeps the log. `${CLAUDE_SKILL_DIR}` is this folder; `kite.py` comes from
`../trader-kite`. Run Python as `python` on Windows (`python3` elsewhere).

```bash
python "${CLAUDE_SKILL_DIR}/ticket.py" show
python "${CLAUDE_SKILL_DIR}/ticket.py" placed --trade T1 --price 1512.60 [--order-id ID]
python "${CLAUDE_SKILL_DIR}/ticket.py" sync                       # today's fills from Kite, read only
python "${CLAUDE_SKILL_DIR}/ticket.py" closed --trade T1 --price 1529.20      # or --pnl -2554 for net P&L
python "${CLAUDE_SKILL_DIR}/ticket.py" flatten                    # exit tickets for open MIS positions
python "${CLAUDE_SKILL_DIR}/ticket.py" flatten --key NSE:RELIANCE --qty 297 --side sell
python "${CLAUDE_SKILL_DIR}/ticket.py" log
```

## Steps: the entry

1. `ticket.py show`. Only a READY ticket (a PASS from /trader-risk today for that exact
   trade) is shown with orders. NOT CHECKED goes back to `/trader-risk`; BLOCKED shows why.
2. Show the ticket as printed, one trade at a time. Say clearly: "Place these in Kite
   yourself, in this order, and check Kite's confirmation screen before you swipe."
3. When the user says they placed it, run `placed --trade T1 --price <their fill>`.
   `sync` can read the fill from Kite (read only) if the user prefers.
4. Then `/trader-alerts add --from-plan` for the target, stop and flat-by alerts.

## Steps: while it is open, and out by the close

- MIS single trade: the SL order protects it. When the target alert rings, the user
  modifies that SL order to a LIMIT at the target (or exits at market). Never leave a
  separate target LIMIT and an SL both live for the same quantity: if both fill, the
  position flips the other way.
- NRML/CNC: the GTT OCO holds both the stop and the target; it is cancelled
  automatically when one leg fires.
- Strategy: exits are on the net value (alerts). Close the short legs first, then the
  hedges, so margin never spikes.
- By flat-by: `flatten` writes exit tickets from Kite's open positions (read only), shorts
  bought back first; `/trader-risk` (exits always pass); `show`; the user cancels the
  open SL/target orders for that position in Kite, then places the exit.
- When it is closed: `closed --trade T1 --price P` (or `--pnl` from Kite's P&L). The
  daily loss limit uses it.

## What Claude never does

- Never places, modifies or cancels an order, a GTT or a basket, by any route: not with
  `kite.py` (it refuses), not with a Kite or broker MCP tool even if one is connected,
  not in a browser on kite.zerodha.com, not with any script. If a tool to do that is
  available, do not call it; give the ticket instead.
- Never asks for or handles the Zerodha password, PIN, TOTP, API secret or access token.
- Never turns a BLOCKED or unchecked trade into a ticket; never records a trade the user
  did not say they placed.
- Never promises a fill: a market order and a stop-limit fill at what the market gives,
  and a stop-limit can stay unfilled in a fast move or at a circuit limit.

## Safety

Tickets are the user's own plan, checked against the user's own rules, not advice.
Most intraday F&O traders lose money (SEBI's studies say so year after year). Not
investment advice.
