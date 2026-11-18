---
name: trader-alerts
description: >-
  Price Alerts for NSE / NFO on Zerodha Kite's last traded price. Turns the plan into
  alerts in one go: the stop, the target and the flat-by time for a single trade, or
  the stop and target on a spread's net value from live leg prices; plus above / below
  levels, a percent move from the previous close, any IST time, and the 9:15 opening
  bell, each ringing once a day. Knows NSE hours and holidays, writes trader/prices.json
  for the risk check. Use when the user says "alert me when", "tell me if Nifty hits",
  "watch my stop", "watch my trade", "remind me to square off by 3", "watch my spread",
  "is the market open", "get me a price", or right after /trader-broker records a trade.
argument-hint: "[add --from-plan | check | list]"
---

# trader-alerts

`${CLAUDE_SKILL_DIR}` is this folder; `kite.py` comes from `../trader-kite`. Run
Python as `python` on Windows (`python3` elsewhere).

```bash
python "${CLAUDE_SKILL_DIR}/alerts.py" add --from-plan               # stop, target, flat-by (or net value)
python "${CLAUDE_SKILL_DIR}/alerts.py" add --key NSE:SBIN --above 850
python "${CLAUDE_SKILL_DIR}/alerts.py" add --key "NIFTY FUT" --below 25800
python "${CLAUDE_SKILL_DIR}/alerts.py" add --key NSE:INFY --pct-move 2
python "${CLAUDE_SKILL_DIR}/alerts.py" add --time 15:00 --note "square off everything"
python "${CLAUDE_SKILL_DIR}/alerts.py" add --opening-bell
python "${CLAUDE_SKILL_DIR}/alerts.py" check
python "${CLAUDE_SKILL_DIR}/alerts.py" list
python "${CLAUDE_SKILL_DIR}/alerts.py" reset
python "${CLAUDE_SKILL_DIR}/alerts.py" quote NSE:RELIANCE NFO:NIFTY26NOVFUT
python "${CLAUDE_SKILL_DIR}/alerts.py" clock
```

## Steps

1. After a trade is placed: `add --from-plan`.
2. `check` once now. To keep watching, use `/loop` (for example `/loop 2m` running
   `alerts.py check`) during market hours, and stop it when the trade is closed.
3. When something rings, say what rang and what the plan says to do next (the note on
   the alert), and that the user acts in Kite.

## Notes

- An alert is a reminder, not protection: between checks the price can touch a level
  and come back. The SL order in Kite is the protection.
- Price alerts only check during 09:15 to 15:30 IST on trading days; time alerts and the
  opening bell ring on trading days only.

Claude never places orders. Not investment advice.
