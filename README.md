# The Day Trader skill pack, India edition

Fourteen Claude skills for trading NSE equity, futures and options with Zerodha
Kite: scan the F&O universe for setups, read the news and Moneycontrol's ideas,
time the entry, plan the stop and target, replay the trade 10,000 times, size it
in lots to your rules, check it against your limits, and get a ticket you place
in Kite yourself.

Adapted from [Jakeschincariol/day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill)
(MIT): the US data sources (Yahoo, SEC, House Clerk), the New York clock and the
Alpaca order route are replaced with Zerodha Kite Connect, the NSE calendar in
IST, NSE's public files and Moneycontrol in the browser.

**No order is ever placed by this pack.** The Kite client is read only: it
refuses every order, GTT and basket endpoint. Claude prepares the ticket; you
place it in Kite.

## The fourteen

| command | job |
| --- | --- |
| `/trader-kite` | Kite Connect sign-in (you run it), IST clock, NSE holidays, instruments, quotes, option chain, funds |
| `/trader-scanner` | Daily setups long and short, opening-range breaks, F&O OI buildup, on the F&O universe |
| `/trader-news` | Google News (India) per company, NSE bulk/block deals, moves from yesterday's close |
| `/trader-moneycontrol` | Reads your Moneycontrol subscription in the built-in browser; records its trade ideas |
| `/trader-entry` | Bar-by-bar opening-range or level break, long or short, inside your entry window |
| `/trader-exit` | Stop and target before the entry, tick-rounded, with a flat-by time before MIS square-off |
| `/trader-options` | Option chain, spreads, condors, option buys; max profit/loss, breakevens, basket margin |
| `/trader-timemachine` | 10,000 replays of the rest of the session plus the same rule on past sessions |
| `/trader-budget` | Size in lots to max loss, max lots and Kite margin; charges; the Kite orders in sequence |
| `/trader-risk` | PASS or BLOCK: stop, max loss, daily limit, window, F&O ban, expiry day, naked shorts, margin |
| `/trader-broker` | The ticket you place in Kite, then the log: placed, fills (read only), closed, flatten |
| `/trader-alerts` | Stop, target, flat-by and net-value alerts on Kite LTP, once a day each |
| `/trader-analyst` | Quarterly results arithmetic: QoQ, YoY, margins, TTM, flags |
| `/trader-deals` | NSE bulk deals, block deals and the F&O ban list |

## One trade, end to end

```
kite login (you) -> scanner / moneycontrol -> news -> entry -> exit or options
   -> timemachine -> budget -> risk -> broker ticket -> YOU place it in Kite
   -> broker "placed" -> alerts (stop, target, flat-by) -> broker "closed"
```

## Install

```bash
git clone <this repo>
cp -r trader-* ~/.claude/skills/        # keep the folders side by side: every script imports trader-kite/kite.py
```

On Windows (PowerShell): `Copy-Item -Recurse trader-* $HOME\.claude\skills\`.

Then:

1. A Kite Connect app at developers.kite.trade (paid; you need market data and
   historical candles). Redirect URL: `http://127.0.0.1:5678/`.
2. Environment variables `KITE_API_KEY` and `KITE_API_SECRET` (never in the chat,
   never in a project file).
3. `trader/config.json` in your project from `trader-kite/config.example.json`:
   capital, max loss per trade, daily loss limit, max lots, which segments.
4. Each trading morning, in your own terminal: `python ~/.claude/skills/trader-kite/kite.py login`.

Python 3.8+, standard library only. Run as `python` on Windows (`python3` on
macOS/Linux).

## Data sources

| what | where | login |
| --- | --- | --- |
| quotes, candles, OI, option prices, margins, charges, funds, positions | Kite Connect (read only) | yours, daily |
| instrument list (lot size, tick size, expiry, strike) | Kite's public daily dump | none |
| bulk deals, block deals, F&O ban list | nsearchives.nseindia.com public files | none |
| headlines | Google News RSS, India edition | none |
| Moneycontrol ideas, news, financials | moneycontrol.com in Claude's browser | yours |

NSE's other JSON APIs need browser cookies and are not used.

## Indian market rules built in

- 9:15 to 15:30 IST, NSE trading holidays for 2026 (add 2027 in `trader/holidays.json`).
- Zerodha's MIS auto square-off (about 3:10 PM for F&O stocks in the closing auction,
  3:25 PM otherwise): default flat-by 15:00 for equity, 15:15 for F&O.
- Whole lots from Kite's list; tick-size rounding; SL (stop-limit) stops, since NSE
  takes no SL-M orders on options; GTT OCO for NRML/CNC; no OCO for intraday, so the
  target is an alert.
- No new short options on their expiry day; F&O ban period; naked short options off
  by default; option selling needs Kite's margin number.

## Tests

```bash
python -m unittest discover -s tests -v
```

Offline, on real Kite instrument rows and SYNTHETIC candles and quotes
(`tests/fixtures/india/make_fixtures.py`), so no login is needed.

## Fine print

**Not investment advice.** Software that reads market data and does arithmetic.
SEBI's studies find most individual intraday and F&O traders lose money. You can
lose more than you planned: a gap, a circuit limit or a fast market can fill past a
stop, and short options can lose many times the premium. Decide for yourself; talk
to a SEBI-registered adviser if you need advice. Moneycontrol's ideas are its
opinions. Day-trading profits are taxed as business income (speculative for
intraday equity); nothing here is tax advice.

MIT. Original by Jake Schincariol.
