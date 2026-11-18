---
name: trader-kite
description: >-
  Zerodha Kite Connect setup and market data for the Indian trader skills. Signs you in
  to Kite Connect each morning (you run the login yourself; tokens expire at 6 AM IST),
  checks the session, shows the IST clock and NSE market phase with the 2026 NSE trading
  holidays, downloads Kite's daily instrument list (lot sizes, tick sizes, expiries,
  strikes for NSE, NFO, BSE, BFO), finds every contract for an underlying, prints live
  quotes and an option chain with OI, IV, delta, PCR and max pain, and shows funds. It is
  READ ONLY: it can never place, modify or cancel an order. Use when the user says
  "connect Kite", "set up Zerodha", "log in to Kite", "is my Kite session valid",
  "is the market open", "NSE holidays", "lot size of", "expiry dates", "option chain",
  "quote for", "how much margin do I have", or when another trader skill reports that
  the Kite session is missing or expired.
argument-hint: "[login | status | chain NIFTY | quote NSE:SYMBOL]"
---

# trader-kite

The shared layer under every Indian trader skill: one read-only Kite Connect client,
the IST clock, the NSE calendar and the instrument master. The other skills import
`kite.py` from this folder, so keep the folders side by side.

`${CLAUDE_SKILL_DIR}` is this folder. Run Python as `python` on Windows (`python3`
on macOS/Linux). Data goes in `trader/` in the current project.

```bash
python "${CLAUDE_SKILL_DIR}/kite.py" status                  # session valid? prints the user id only
python "${CLAUDE_SKILL_DIR}/kite.py" clock                   # IST now, phase, next open
python "${CLAUDE_SKILL_DIR}/kite.py" holidays --year 2026
python "${CLAUDE_SKILL_DIR}/kite.py" instruments --refresh   # today's NSE + NFO dumps (public, no login)
python "${CLAUDE_SKILL_DIR}/kite.py" find RELIANCE           # spot, futures, option expiries, lot sizes
python "${CLAUDE_SKILL_DIR}/kite.py" quote NSE:RELIANCE NFO:NIFTY26NOVFUT
python "${CLAUDE_SKILL_DIR}/kite.py" chain NIFTY --strikes 8
python "${CLAUDE_SKILL_DIR}/kite.py" funds
```

## One-time setup (the user does these steps, not Claude)

1. Create a Kite Connect app at developers.kite.trade (Zerodha charges for Kite
   Connect; the plan with market data and historical candles is needed here). Set the
   app's **Redirect URL** to `http://127.0.0.1:5678/`.
2. Set two environment variables on the computer (System Properties > Environment
   Variables on Windows, or the shell profile on macOS/Linux):
   `KITE_API_KEY` = the app's API key, `KITE_API_SECRET` = the app's API secret.
3. Copy `config.example.json` from this folder to `trader/config.json` in the project
   and edit the limits (rupees, IST). Optionally copy `holidays.example.json` to
   `trader/holidays.json`.

## Every trading morning

Tell the user to run this **in their own terminal** (the Terminal panel is fine):

```
python <path to this folder>/kite.py login
```

It opens Kite's sign-in page; the user signs in with their own Zerodha credentials
and 2FA there; the script saves the day's access token to `~/.kite/session.json`
(outside the project). Tokens expire at 6:00 AM IST.

## Rules for Claude

- Never ask for, read out, type, or store the API secret, the access token, the
  Zerodha password, PIN or TOTP. Never open `~/.kite/session.json` or print
  environment variables. Never run `kite.py login` yourself: sign-in is the user's.
  If the user pastes a secret or token into the chat, tell them to regenerate it
  in the Kite developer console and delete the message.
- This client refuses every order endpoint. Do not work around that, and do not
  call any other tool (a Kite MCP server, a browser on kite.zerodha.com, a script)
  to place, modify or cancel orders or GTTs. The user places orders in Kite.
- Kite's limits: quotes 1 request/second, candles 3/second. The client paces itself.
- NSE hours: pre-open 9:00-9:08, continuous 9:15-15:30 IST. The 2026 holiday list is
  built in; for 2027 the tools warn until `trader/holidays.json` lists NSE's dates.
  Muhurat trading (a special evening session) is not modelled.
- Prices from Kite are what the exchange printed; check anything that matters in
  Kite itself before acting.

Not investment advice: this is software that reads market data.
