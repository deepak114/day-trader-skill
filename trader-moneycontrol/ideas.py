#!/usr/bin/env python3
"""Moneycontrol ideas -> watchlist, for trader-moneycontrol. Standard library + ../trader-kite/kite.py.

Claude reads Moneycontrol in the built-in browser (you sign in there yourself) and
records each trade suggestion it finds with `add`. This script checks the symbol
against Kite's instrument list, checks the numbers make sense, and keeps them in
trader/ideas.json; `to-watchlist` merges them into trader/watchlist.json so the
entry timer, the exit planner and the risk manager treat them like any other setup.

    python ideas.py add --symbol RELIANCE --side buy --entry 1512 --stop 1495 --target 1550 \\
        --horizon intraday --source "Moneycontrol <page title>" --url https://www.moneycontrol.com/... \\
        [--segment equity|future|option] [--strike 26000 --kind CE --expiry 2026-11-23] [--analyst NAME]
    python ideas.py list [--today]
    python ideas.py to-watchlist [--today] [--horizon intraday]
    python ideas.py clear --before 2026-11-18

A suggestion is someone else's opinion, recorded as data. It still goes through
/trader-entry (or your own judgment), /trader-exit, /trader-budget and /trader-risk.
"""

import argparse
import hashlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trader-kite"))
import kite  # noqa: E402
from kite import KiteError  # noqa: E402

HORIZONS = ("intraday", "btst", "swing", "positional", "long-term")


def validate(r):
    long = r["side"] == "buy"
    e, s, t = r.get("entry"), r.get("stop"), r.get("target")
    if s is not None and e is not None and ((long and s >= e) or (not long and s <= e)):
        raise KiteError("the stop %.2f is on the wrong side of the entry %.2f for a %s" % (s, e, r["side"]))
    if t is not None and e is not None and ((long and t <= e) or (not long and t >= e)):
        raise KiteError("the target %.2f is on the wrong side of the entry %.2f for a %s" % (t, e, r["side"]))
    if e and s and t:
        r["reward_risk"] = round(abs(t - e) / abs(e - s), 2)
    return r


def resolve(r, inst):
    sym = r["symbol"].upper()
    seg = r.get("segment") or "equity"
    if inst is None:
        r["key"] = "NSE:%s" % sym if seg == "equity" else r.get("key")
        return r
    if seg == "equity":
        ins = inst.get("NSE:%s" % sym)
    elif seg == "future":
        ins = inst.future(sym, r.get("expiry"))
    else:
        if not (r.get("strike") and r.get("kind")):
            raise KiteError("an option idea needs --strike and --kind CE|PE (and --expiry if not the nearest)")
        ex = r.get("expiry") or inst.expiries(sym, ("CE", "PE"))[0]
        ins = inst.option(sym, ex, r["strike"], r["kind"])
    r.update(key=ins.key, tradingsymbol=ins["tradingsymbol"], lot_size=ins["lot_size"], expiry=ins["expiry"])
    return r


def cmd_add(a):
    now = kite.now_ist(a.now)
    r = {"symbol": a.symbol.upper(), "side": a.side.lower(), "entry": a.entry, "stop": a.stop, "target": a.target,
         "segment": a.segment, "strike": a.strike, "kind": (a.kind or "").upper() or None, "expiry": a.expiry,
         "horizon": a.horizon, "source": a.source, "url": a.url, "analyst": a.analyst,
         "date": now.date().isoformat(), "recorded_at": kite.iso_ist(now)}
    if a.entry_low and a.entry_high:
        r["entry_range"] = [a.entry_low, a.entry_high]
        r["entry"] = r["entry"] or round((a.entry_low + a.entry_high) / 2, 2)
    validate(r)
    inst = None if a.no_instruments else kite.Instruments(("NSE", "NFO"))
    resolve(r, inst)
    r["id"] = hashlib.sha1(("%(date)s|%(key)s|%(side)s|%(entry)s|%(stop)s|%(target)s" % r).encode()).hexdigest()[:10]
    doc = kite.read_json(a.file, default={"ideas": []}) or {"ideas": []}
    if any(x["id"] == r["id"] for x in doc["ideas"]):
        print("already recorded: %s" % r["id"])
        return 0
    doc["ideas"].append({k: v for k, v in r.items() if v is not None})
    kite.write_json(a.file, doc)
    print("recorded %s %s %s entry %s stop %s target %s%s (%s)" % (
        r["id"], r["side"].upper(), r["key"], r["entry"], r["stop"], r["target"],
        ", reward:risk %.2f" % r["reward_risk"] if r.get("reward_risk") else "", r["horizon"]))
    return 0


def pick(doc, a, now):
    rows = doc.get("ideas", [])
    if getattr(a, "today", False):
        rows = [r for r in rows if r.get("date") == now.date().isoformat()]
    if getattr(a, "horizon", None) and a.cmd != "add":
        rows = [r for r in rows if r.get("horizon") == a.horizon]
    return rows


def cmd_list(a):
    now = kite.now_ist(a.now)
    rows = pick(kite.read_json(a.file, default={}) or {}, a, now)
    if not rows:
        print("no ideas recorded")
        return 0
    for r in rows:
        print("%s %s  %-4s %-26s entry %-8s stop %-8s target %-8s %-10s %s" % (
            r["date"], r["id"], r["side"].upper(), r["key"], r.get("entry"), r.get("stop"), r.get("target"),
            r.get("horizon"), r.get("source") or ""))
    return 0


def cmd_to_watchlist(a):
    now = kite.now_ist(a.now)
    rows = pick(kite.read_json(a.file, default={}) or {}, a, now)
    wl = kite.read_json(a.watchlist, default=None) or {"created_by": "trader-moneycontrol", "items": []}
    have = {(i.get("key"), i.get("idea_id")) for i in wl.get("items", [])}
    n = 0
    for r in rows:
        if (r["key"], r["id"]) in have:
            continue
        wl.setdefault("items", []).append({
            "key": r["key"], "symbol": r["symbol"], "side": r["side"], "setup": "moneycontrol idea",
            "entry_setup": "level" if r.get("entry") else "orb", "level": r.get("entry"),
            "idea_stop": r.get("stop"), "idea_target": r.get("target"), "horizon": r.get("horizon"),
            "idea_id": r["id"], "source": r.get("source"), "url": r.get("url")})
        n += 1
    wl["updated_at"] = kite.iso_ist(now)
    kite.write_json(a.watchlist, wl)
    print("added %d idea(s) to %s -> next: /trader-news, /trader-entry watchlist" % (n, a.watchlist))
    return 0


def cmd_clear(a):
    doc = kite.read_json(a.file, default={"ideas": []}) or {"ideas": []}
    keep = [r for r in doc["ideas"] if r.get("date", "") >= a.before]
    print("removed %d idea(s) dated before %s" % (len(doc["ideas"]) - len(keep), a.before))
    doc["ideas"] = keep
    kite.write_json(a.file, doc)
    return 0


def main(argv=None):
    kite.safe_stdout()
    ap = argparse.ArgumentParser(description="Record Moneycontrol trade ideas and feed them to the watchlist")
    sub = ap.add_subparsers(dest="cmd")

    def common(s):
        s.add_argument("--file", default="trader/ideas.json")
        s.add_argument("--now")
    s = sub.add_parser("add")
    common(s)
    s.add_argument("--symbol", required=True)
    s.add_argument("--side", required=True, choices=("buy", "sell", "BUY", "SELL"))
    s.add_argument("--entry", type=float)
    s.add_argument("--entry-low", type=float)
    s.add_argument("--entry-high", type=float)
    s.add_argument("--stop", type=float)
    s.add_argument("--target", type=float)
    s.add_argument("--segment", choices=("equity", "future", "option"), default="equity")
    s.add_argument("--strike", type=float)
    s.add_argument("--kind", choices=("CE", "PE", "ce", "pe"))
    s.add_argument("--expiry")
    s.add_argument("--horizon", choices=HORIZONS, default="intraday")
    s.add_argument("--source")
    s.add_argument("--url")
    s.add_argument("--analyst")
    s.add_argument("--no-instruments", action="store_true", help=argparse.SUPPRESS)
    for name in ("list", "to-watchlist"):
        s = sub.add_parser(name)
        common(s)
        s.add_argument("--today", action="store_true")
        s.add_argument("--horizon", choices=HORIZONS)
        if name == "to-watchlist":
            s.add_argument("--watchlist", default="trader/watchlist.json")
    s = sub.add_parser("clear")
    common(s)
    s.add_argument("--before", required=True)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"add": cmd_add, "list": cmd_list, "to-watchlist": cmd_to_watchlist, "clear": cmd_clear}[a.cmd](a)
    except KiteError as e:
        print("ideas: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
