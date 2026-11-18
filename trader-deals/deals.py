#!/usr/bin/env python3
"""Deals Tracker for trader-deals (NSE bulk deals, block deals, F&O ban list). Standard library.

    python deals.py bulk                       # today's NSE bulk deals, biggest first
    python deals.py block --min-cr 50          # block deals worth >= Rs 50 crore
    python deals.py bulk --client "MUTUAL FUND"     # only clients whose name matches
    python deals.py bulk --watch               # only symbols in trader/watchlist.json / portfolio.json
    python deals.py ban                        # today's F&O ban list (no new F&O positions in these)
    python deals.py new                        # deals not shown before (remembers trader/state/deals.json)
    ... --offline FILE

Sources: nsearchives.nseindia.com/content/equities/bulk.csv and block.csv, and
content/fo/fo_secban.csv (NSE's public end-of-day files). A bulk deal is a trade of
more than 0.5 % of a company's shares in one day; a block deal is a single trade of at
least Rs 10 crore in the block window. Both are disclosures, not tips: who bought tells
you nothing certain about tomorrow. Promoter and insider trades (SEBI PIT disclosures)
are on nseindia.com's corporate filings pages, read in the browser.
"""

import argparse
import csv
import datetime
import hashlib
import io
import os
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trader-kite"))
import kite  # noqa: E402
from kite import KiteError  # noqa: E402

URLS = {"bulk": "https://nsearchives.nseindia.com/content/equities/bulk.csv",
        "block": "https://nsearchives.nseindia.com/content/equities/block.csv",
        "ban": "https://nsearchives.nseindia.com/content/fo/fo_secban.csv"}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) day-trader-skill-india/1.0"
CRORE = 1e7


def fetch(kind, offline=None):
    if offline:
        return open(offline, encoding="utf-8").read()
    req = urllib.request.Request(URLS[kind], headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        raise KiteError("could not read NSE's %s file (%s)" % (kind, getattr(e, "reason", e)))


def _num(s):
    try:
        return float(str(s).replace(",", "").strip())
    except ValueError:
        return 0.0


def parse(text, kind):
    rows = []
    for r in csv.DictReader(io.StringIO(text.strip())):
        r = {(k or "").strip(): (v or "").strip() for k, v in r.items()}
        if not r.get("Symbol"):
            continue
        qty, px = _num(r.get("Quantity Traded")), _num(r.get("Trade Price / Wght. Avg. Price"))
        rows.append({"kind": kind, "date": r.get("Date"), "symbol": r["Symbol"].upper(), "name": r.get("Security Name"),
                     "client": r.get("Client Name"), "side": r.get("Buy/Sell", "").upper(), "quantity": int(qty),
                     "price": px, "value_cr": round(qty * px / CRORE, 2), "remarks": r.get("Remarks")})
    rows.sort(key=lambda x: -x["value_cr"])
    return rows


def parse_ban(text):
    m = re.search(r"Trade Date\s+([0-9A-Za-z\-]+)", text)
    syms = [x.group(1) for x in re.finditer(r"^\s*\d+\s*,\s*([A-Z0-9&\-]+)\s*$", text, re.M)]
    return (m.group(1) if m else None), syms


def watch_set():
    out = set()
    for path, key in (("trader/watchlist.json", "items"), ("trader/portfolio.json", "holdings")):
        doc = kite.read_json(path, default={}) or {}
        for it in doc.get(key) or []:
            s = (it.get("symbol") or it.get("key", "").split(":")[-1]).upper()
            if s:
                out.add(s)
    return out


def fp(r):
    return hashlib.sha1(("%(kind)s|%(date)s|%(symbol)s|%(client)s|%(side)s|%(quantity)s" % r).encode()).hexdigest()[:14]


def show(rows, limit):
    for r in rows[:limit]:
        print("  %-11s %-4s %-12s %12s sh @ %10.2f = Rs %8.2f cr  %s" % (
            r["date"], r["side"], r["symbol"], "{:,}".format(r["quantity"]), r["price"], r["value_cr"],
            r["client"]))


def main(argv=None):
    kite.safe_stdout()
    ap = argparse.ArgumentParser(description="NSE bulk / block deals and the F&O ban list")
    ap.add_argument("cmd", choices=("bulk", "block", "ban", "new"))
    ap.add_argument("--min-cr", type=float, default=0.0)
    ap.add_argument("--client")
    ap.add_argument("--symbol")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--side", choices=("BUY", "SELL", "buy", "sell"))
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--offline")
    ap.add_argument("--state", default="trader/state/deals.json")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "ban":
            date, syms = parse_ban(fetch("ban", a.offline))
            print("F&O ban for %s: %s" % (date or "today", ", ".join(syms) if syms else "none"))
            print("In the ban period only trades that reduce open positions are allowed; new F&O positions "
                  "draw a penalty.")
            return 0
        kinds = ("bulk", "block") if a.cmd == "new" else (a.cmd,)
        rows = []
        for k in kinds:
            rows += parse(fetch(k, a.offline if a.cmd != "new" else None), k)
        if a.min_cr:
            rows = [r for r in rows if r["value_cr"] >= a.min_cr]
        if a.client:
            rows = [r for r in rows if a.client.upper() in (r["client"] or "").upper()]
        if a.symbol:
            rows = [r for r in rows if r["symbol"] == a.symbol.upper()]
        if a.side:
            rows = [r for r in rows if r["side"] == a.side.upper()]
        if a.watch:
            ws = watch_set()
            rows = [r for r in rows if r["symbol"] in ws]
        if a.cmd == "new":
            st = kite.read_json(a.state, default={}) or {}
            seen = st.setdefault("seen", {})
            rows = [r for r in rows if fp(r) not in seen]
            today = kite.now_ist().date().isoformat()
            for r in rows:
                seen[fp(r)] = today
            cut = (kite.now_ist().date() - datetime.timedelta(days=30)).isoformat()
            st["seen"] = {k: v for k, v in seen.items() if v >= cut}
            kite.write_json(a.state, st)
        print("%s deals: %d%s" % ("New bulk + block" if a.cmd == "new" else a.cmd.title(), len(rows),
                                  " (showing %d)" % a.limit if len(rows) > a.limit else ""))
        show(rows, a.limit)
        print("Disclosures, not tips: a fund buying today says nothing certain about tomorrow.")
    except KiteError as e:
        print("deals: %s" % e, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
