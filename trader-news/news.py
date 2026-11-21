#!/usr/bin/env python3
"""News Monitor for trader-news (NSE stocks and indices). Standard library + ../trader-kite/kite.py.

    python news.py                                 # the watchlist + portfolio: new items, last 24 h
    python news.py --symbols RELIANCE,SBIN --hours 12
    python news.py --all                           # include items already shown
    python news.py --no-moves                      # skip the price move (no Kite needed)
    python news.py --json

Sources, all public and read without a login:
  * Google News RSS, India edition, one query per company (its name from Kite's instrument list)
  * NSE's bulk and block deal files for today (nsearchives.nseindia.com, the same files as on
    nseindia.com), matched to your symbols
  * the move from yesterday's close (Kite /quote/ohlc, read only, when you are signed in);
    a stock >= --move-pct (3 %) away is flagged
Moneycontrol news (behind your subscription) is read in the browser by /trader-moneycontrol,
not by this script.

Each item is tagged (results, dividend/corporate action, order win, deal/stake, analyst,
regulatory, management, promoter/pledge, legal/probe, fund-raise, block/bulk deal, macro),
shown newest first in IST, and remembered in trader/state/news.json so it is not shown twice.
"""

import argparse
import datetime
import email.utils
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trader-kite"))
import kite  # noqa: E402
from kite import KiteError  # noqa: E402

GNEWS = "https://news.google.com/rss/search?q=%s&hl=en-IN&gl=IN&ceid=IN:en"
BULK = "https://nsearchives.nseindia.com/content/equities/bulk.csv"
BLOCK = "https://nsearchives.nseindia.com/content/equities/block.csv"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) day-trader-skill-india/1.0"
PAUSE = 0.6
KEEP_DAYS = 30
MOVE_PCT = 3.0
TAGS = [
    ("results", r"\b(q[1-4]|quarter(ly)?|results?|earnings|net profit|revenue|ebitda|pat\b|margin|guidance)"),
    ("corporate action", r"\b(dividend|bonus|split|buy ?back|record date|rights issue)"),
    ("order win", r"\b(order (win|worth|from)|bags? (an? )?order|contract (worth|from|win)|wins? (an? )?order|"
                  r"letter of award|loa\b)"),
    ("deal/stake", r"\b(acqui(re|sition)|merger|demerger|stake|takeover|joint venture|jv\b|divest)"),
    ("analyst", r"\b(target price|price target|upgrade|downgrade|buy rating|sell rating|overweight|underweight|"
                r"initiat|brokerage|neutral|target (of )?(rs\.?|inr|₹)\s?\d|"
                r"(buy|sell|accumulate|add|reduce|hold)\b[^.|]{0,60}\btarget\b)"),
    ("regulatory", r"\b(sebi|rbi|nclt|cci\b|trai|dgca|usfda|fda|licen[cs]e|approval|ban\b|penalty|gst)"),
    ("management", r"\b(ceo|md\b|managing director|cfo|chairman|resign|appoint|steps down)"),
    ("promoter/pledge", r"\b(promoter|pledge|insider)"),
    ("legal/probe", r"\b(probe|raid|ed\b|cbi|fraud|lawsuit|court|arbitration|default|insolvency)"),
    ("fund-raise", r"\b(qip|ipo|ofs\b|offer for sale|fund ?rais|ncd|preferential)"),
    ("block/bulk deal", r"\b(block deal|bulk deal)"),
    ("macro", r"\b(nifty|sensex|fii|dii|inflation|repo rate|rupee|crude|fed\b|budget)"),
]


def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        raise KiteError("could not read %s (%s)" % (url.split("?")[0], getattr(e, "reason", e)))


def company_query(sym, name):
    nm = re.sub(r"\b(LIMITED|LTD\.?|INDUSTRIES?|CORPORATION|CORP\.?|COMPANY|CO\.?)\b", " ", (name or "").upper())
    nm = " ".join(nm.split()).title() or sym
    if sym in kite.INDEX_NAMES:
        return '"%s" market' % sym.title()
    return '"%s" OR "%s share" OR "%s stock"' % (nm, sym, sym)


def parse_rss(text, sym):
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        src = it.find("source")
        when = it.findtext("pubDate")
        try:
            t = kite.to_ist(email.utils.parsedate_to_datetime(when)) if when else None
        except (TypeError, ValueError):
            t = None
        out.append({"symbol": sym, "title": title, "link": link, "source": src.text if src is not None else "Google News",
                    "time": t.isoformat() if t else None, "kind": "headline"})
    return out


def parse_deals(text, kind, symbols):
    rows = []
    lines = text.strip().splitlines()
    if not lines:
        return rows
    import csv
    for r in csv.DictReader(lines):
        r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        sym = r.get("Symbol", "").upper()
        if sym not in symbols:
            continue
        try:
            d = datetime.datetime.strptime(r.get("Date", ""), "%d-%b-%Y").replace(hour=15, minute=30,
                                                                                tzinfo=kite.IST)
        except ValueError:
            d = None
        qty = r.get("Quantity Traded", "")
        px = r.get("Trade Price / Wght. Avg. Price", "")
        rows.append({"symbol": sym, "kind": kind, "source": "NSE %s deals" % kind,
                     "title": "%s deal: %s %s %s shares at %s" % (kind.title(), r.get("Client Name", "?"),
                                                                 r.get("Buy/Sell", "").lower(), qty, px),
                     "link": BULK if kind == "bulk" else BLOCK, "time": d.isoformat() if d else None})
    return rows


def tag(item, names):
    t = item["title"].lower()
    tags = [k for k, rx in TAGS if re.search(rx, t)]
    if item["kind"] in ("bulk", "block"):
        tags = ["block/bulk deal"] + [x for x in tags if x != "block/bulk deal"]
    item["tags"] = tags
    mentions = [s for s, n in names.items() if s.lower() in t or (n and n.lower() in t)]
    item["mentions"] = mentions
    return item


def fingerprint(it):
    base = re.sub(r"\s+-\s+[^-]+$", "", it["title"].lower())   # drop " - Publisher": syndication is one item
    base = re.sub(r"\W+", " ", base).strip()
    return hashlib.sha1((it["symbol"] + "|" + base).encode("utf-8")).hexdigest()[:16]


def dedupe(items):
    seen, out = set(), []
    for it in items:
        fp = fingerprint(it)
        if fp in seen:
            continue
        seen.add(fp)
        it["id"] = fp
        out.append(it)
    return out


def select(items, state, now, hours, show_all=False):
    cut = now - datetime.timedelta(hours=hours)
    shown = state.get("seen", {})
    out = []
    for it in items:
        if it.get("time") and kite.parse_ts(it["time"]) < cut:
            continue
        if not show_all and it["id"] in shown:
            continue
        out.append(it)
    out.sort(key=lambda x: x.get("time") or "", reverse=True)
    return out


def remember(items, state, now):
    seen = state.setdefault("seen", {})
    for it in items:
        seen[it["id"]] = now.date().isoformat()
    cut = (now.date() - datetime.timedelta(days=KEEP_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in seen.items() if v >= cut}


def watch_symbols(a):
    if a.symbols:
        return [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    out = []
    for path, key in ((a.watchlist, "items"), (a.portfolio, "holdings")):
        doc = kite.read_json(path, default={}) or {}
        for it in doc.get(key) or []:
            s = (it.get("symbol") or it.get("key", "").split(":")[-1] or "").upper()
            if s:
                out.append(s)
    return list(dict.fromkeys(out))


def render(items, moves, errors, now):
    lines = ["News at %s (%d new item%s)" % (now.strftime("%Y-%m-%d %H:%M IST"), len(items),
                                             "s" if len(items) != 1 else "")]
    if moves:
        lines.append("Moves from yesterday's close:")
        for s, m in sorted(moves.items(), key=lambda kv: -abs(kv[1]["pct"])):
            lines.append("  %-12s %+6.2f%%  %.2f%s" % (s, m["pct"], m["last"], "  <- MOVING" if m["flag"] else ""))
    for it in items:
        t = kite.parse_ts(it["time"]).strftime("%d-%b %H:%M") if it.get("time") else "      ?     "
        lines.append("%s  %-10s [%s] %s  (%s)" % (t, it["symbol"], ", ".join(it["tags"]) or "general", it["title"],
                                                   it["source"]))
    for e in errors:
        lines.append("note: " + e)
    lines.append("Headlines are leads, not facts: open the source before you act on one.")
    return "\n".join(lines)


def main(argv=None):
    kite.safe_stdout()
    ap = argparse.ArgumentParser(description="News Monitor: Google News (India), NSE bulk/block deals, moves")
    ap.add_argument("--symbols")
    ap.add_argument("--watchlist", default="trader/watchlist.json")
    ap.add_argument("--portfolio", default="trader/portfolio.json")
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--no-moves", action="store_true")
    ap.add_argument("--no-deals", action="store_true")
    ap.add_argument("--move-pct", type=float, default=MOVE_PCT)
    ap.add_argument("--per", type=int, default=8, help="headlines kept per symbol")
    ap.add_argument("--state", default="trader/state/news.json")
    ap.add_argument("--now")
    ap.add_argument("--offline", help="folder with <SYM>.xml, bulk.csv, block.csv (tests)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        now = kite.now_ist(a.now)
        syms = watch_symbols(a)
        if not syms:
            raise KiteError("no symbols: pass --symbols, or run /trader-scanner or /trader-moneycontrol first")
        names = {}
        if not a.offline:
            try:
                inst = kite.Instruments(("NSE",))
                for s in syms:
                    r = inst.by_key.get("NSE:%s" % s)
                    names[s] = r["name"] if r else s
            except KiteError:
                names = {s: s for s in syms}
        else:
            names = {s: s for s in syms}
        items, errors = [], []
        for s in syms:
            try:
                if a.offline:
                    p = os.path.join(a.offline, "%s.xml" % s)
                    text = open(p, encoding="utf-8").read() if os.path.exists(p) else ""
                else:
                    text = http_get(GNEWS % urllib.parse.quote(company_query(s, names.get(s))))
                    time.sleep(PAUSE)
                items += parse_rss(text, s)[: a.per]
            except KiteError as e:
                errors.append(str(e))
        if not a.no_deals:
            for kind, url in (("bulk", BULK), ("block", BLOCK)):
                try:
                    p = os.path.join(a.offline, "%s.csv" % kind) if a.offline else None
                    text = (open(p, encoding="utf-8").read() if p and os.path.exists(p) else "") if a.offline \
                        else http_get(url)
                    items += parse_deals(text, kind, set(syms))
                except KiteError as e:
                    errors.append(str(e))
        items = [tag(it, names) for it in dedupe(items)]
        state = kite.read_json(a.state, default={}) or {}
        new = select(items, state, now, a.hours, a.all)
        moves = {}
        if not a.no_moves and not a.offline:
            try:
                kite.credentials()
                keys = ["NSE:%s" % s if s not in kite.INDEX_NAMES else kite.INDEX_SPOT[s] for s in syms]
                q = kite.Kite().ohlc(keys)
                for s, k in zip(syms, keys):
                    v = q.get(k)
                    if v and (v.get("ohlc") or {}).get("close"):
                        pct = 100 * (v["last_price"] / v["ohlc"]["close"] - 1)
                        moves[s] = {"pct": round(pct, 2), "last": v["last_price"], "flag": abs(pct) >= a.move_pct}
            except KiteError as e:
                errors.append("moves skipped: %s" % e)
        if a.json:
            print(json.dumps({"items": new, "moves": moves, "errors": errors}, indent=2, ensure_ascii=False))
        else:
            print(render(new, moves, errors, now))
        remember(new, state, now)
        kite.write_json(a.state, state)
    except KiteError as e:
        print("news: %s" % e, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
