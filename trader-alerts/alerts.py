#!/usr/bin/env python3
"""Price Alerts for trader-alerts (NSE / NFO via Kite LTP). Standard library + ../trader-kite/kite.py.

    python alerts.py add --from-plan                 # stop, target and flat-by from trader/plan.json
                                                     # (a strategy: its net value vs stop/target)
    python alerts.py add --key NSE:SBIN --above 850
    python alerts.py add --key "NIFTY FUT" --below 25800
    python alerts.py add --key NSE:INFY --pct-move 2          # +-2 % from the previous close
    python alerts.py add --time 15:00 --note "flatten everything"
    python alerts.py add --opening-bell
    python alerts.py check                           # rings what hit, once each per day; writes trader/prices.json
    python alerts.py list | reset | clock
    python alerts.py quote NSE:RELIANCE NFO:NIFTY26NOVFUT
    ... --now "2026-11-18 10:20" --quotes FILE (a saved Kite /quote or /ltp JSON, offline)

Each alert rings once per trading day. Prices are Kite's last traded price (LTP), read
only; between checks a price can touch a level and come back, so a stop ORDER in Kite
is the real protection and an alert is the reminder. Checks run only in market hours
(09:15 to 15:30 IST, NSE holidays closed) except time and opening-bell alerts.
"""

import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trader-kite"))
import kite  # noqa: E402
from kite import KiteError  # noqa: E402

TYPES = {"above", "below", "stop", "target", "pct_move", "time", "opening_bell", "strategy_stop",
         "strategy_target"}
NO_QUOTE = {"time", "opening_bell"}


def rules_from_plan(plan):
    out = []
    if plan.get("strategy", "single") == "single":
        long = plan["side"] == "buy"
        k = plan["key"]
        out.append({"type": "stop", "key": k, "level": plan["stop"], "dir": "below" if long else "above",
                    "note": "STOP %.2f hit on %s: check your SL order filled" % (plan["stop"], k)})
        out.append({"type": "target", "key": k, "level": plan["target"], "dir": "above" if long else "below",
                    "note": "TARGET %.2f on %s: modify the SL order to LIMIT %.2f or exit (Kite has no intraday OCO)"
                            % (plan["target"], k, plan["target"])})
    else:
        legs = [{"key": "%s:%s" % (l["exchange"], l["tradingsymbol"]), "side": l["side"], "ratio": l["ratio"]}
                for l in plan["legs"]]
        credit = plan.get("net_premium", 0) < 0
        out.append({"type": "strategy_stop", "legs": legs, "level": plan["stop_value"], "credit": credit,
                    "note": "STRATEGY STOP: %s %s net value reached %.2f: exit every leg (buy back shorts first)"
                            % (plan["underlying"], plan["strategy"], plan["stop_value"])})
        out.append({"type": "strategy_target", "legs": legs, "level": plan["target_value"], "credit": credit,
                    "note": "STRATEGY TARGET: %s %s net value reached %.2f: consider closing every leg"
                            % (plan["underlying"], plan["strategy"], plan["target_value"])})
    if plan.get("flat_by"):
        out.append({"type": "time", "at": plan["flat_by"],
                    "note": "FLAT BY %s IST: exit what is still open (Zerodha's MIS auto square-off is soon after, "
                            "with a charge)" % plan["flat_by"]})
    return out


def net_value(legs, ltp):
    """Per-unit cost to close a strategy: what you'd pay for longs' value... as a signed sum:
    debit strategies are worth sum(long) - sum(short); credit ones cost sum(short) - sum(long) to close."""
    v = 0.0
    for l in legs:
        p = ltp.get(l["key"])
        if p is None:
            return None
        v += (1 if l["side"] == "BUY" else -1) * l.get("ratio", 1) * p
    return v


def evaluate(rules, quotes, now, state):
    """quotes {key: {last, prev_close}} -> list of rung (rule, message)."""
    today = now.date().isoformat()
    mins = now.hour * 60 + now.minute
    st = kite.market_status(now)
    rung = []
    ltp = {k: q["last"] for k, q in quotes.items()}
    for i, r in enumerate(rules):
        rid = r.get("id") or str(i)
        if state.get(rid) == today:
            continue
        hit, msg = False, None
        t = r["type"]
        if t == "time":
            hit = st["trading_day"] and mins >= kite.hhmm(r["at"])
            msg = r.get("note") or "it is %s IST" % r["at"]
        elif t == "opening_bell":
            hit = st["trading_day"] and mins >= kite.OPEN_MIN
            msg = "the NSE session is open (9:15 AM IST)"
        elif st["phase"] != "open":
            continue
        elif t in ("above", "below", "stop", "target"):
            q = quotes.get(r["key"])
            if not q:
                continue
            d = r.get("dir") or t
            hit = q["last"] >= r["level"] if d == "above" else q["last"] <= r["level"]
            msg = r.get("note") or "%s at %.2f, %s %.2f" % (r["key"], q["last"], d, r["level"])
            msg = "%s (last %.2f)" % (msg, q["last"])
        elif t == "pct_move":
            q = quotes.get(r["key"])
            if not q or not q.get("prev_close"):
                continue
            chg = 100 * (q["last"] / q["prev_close"] - 1)
            hit = abs(chg) >= r["pct"]
            msg = "%s is %+.2f%% from the previous close (%.2f)" % (r["key"], chg, q["last"])
        elif t in ("strategy_stop", "strategy_target"):
            v = net_value(r["legs"], ltp)
            if v is None:
                continue
            close_cost = -v if r.get("credit") else v
            if t == "strategy_stop":
                hit = close_cost >= r["level"] if r.get("credit") else close_cost <= r["level"]
            else:
                hit = close_cost <= r["level"] if r.get("credit") else close_cost >= r["level"]
            msg = "%s (net value now %.2f)" % (r["note"], close_cost)
        if hit:
            rung.append((rid, r, msg))
    return rung


def keys_of(rules):
    ks = []
    for r in rules:
        if r.get("key"):
            ks.append(r["key"])
        for l in r.get("legs") or []:
            ks.append(l["key"])
    return list(dict.fromkeys(ks))


def get_quotes(keys, offline=None):
    if not keys:
        return {}
    if offline:
        doc = kite.read_json(offline, "quotes", required=True)
        raw = doc.get("data", doc)
    else:
        raw = kite.Kite().ohlc(keys)
    out = {}
    for k, v in raw.items():
        if k in keys or not keys:
            out[k] = {"last": float(v["last_price"]), "prev_close": float((v.get("ohlc") or {}).get("close") or 0)}
    return out


def load_rules(path):
    doc = kite.read_json(path, default={"rules": []}) or {"rules": []}
    return doc


def cmd_add(a):
    doc = load_rules(a.file)
    rules = doc.setdefault("rules", [])
    new = []
    if a.from_plan:
        new = rules_from_plan(kite.read_json(a.plan, "plan", required=True))
    elif a.opening_bell:
        new = [{"type": "opening_bell"}]
    elif a.time:
        kite.hhmm(a.time)
        new = [{"type": "time", "at": a.time, "note": a.note}]
    else:
        if not a.key:
            raise KiteError("give --key with --above / --below / --pct-move, or --from-plan / --time / --opening-bell")
        key = kite.normalize_key(a.key) if ":" in a.key else kite.Instruments(("NSE", "NFO")).resolve(a.key).key
        if a.above is not None:
            new.append({"type": "above", "key": key, "level": a.above, "note": a.note})
        if a.below is not None:
            new.append({"type": "below", "key": key, "level": a.below, "note": a.note})
        if a.pct_move is not None:
            new.append({"type": "pct_move", "key": key, "pct": a.pct_move})
        if not new:
            raise KiteError("--key needs --above, --below or --pct-move")
    n0 = max([int(r.get("id", 0)) for r in rules if str(r.get("id", "")).isdigit()] + [0])
    for i, r in enumerate(new, 1):
        r["id"] = str(n0 + i)
        r = {k: v for k, v in r.items() if v is not None}
        rules.append(r)
        print("added #%s %s" % (r["id"], describe(r)))
    kite.write_json(a.file, doc)
    return 0


def describe(r):
    t = r["type"]
    if t == "time":
        return "at %s IST: %s" % (r["at"], r.get("note") or "")
    if t == "opening_bell":
        return "the 9:15 AM opening bell"
    if t == "pct_move":
        return "%s moves %g%% from the previous close" % (r["key"], r["pct"])
    if t.startswith("strategy"):
        return "%s: net value %.2f" % (t.replace("_", " "), r["level"])
    return "%s %s %.2f" % (r["key"], r.get("dir") or t, r["level"])


def cmd_check(a):
    now = kite.now_ist(a.now)
    doc = load_rules(a.file)
    rules = doc.get("rules", [])
    if not rules:
        print("no alerts: `alerts.py add --from-plan` after /trader-exit or /trader-options")
        return 0
    st = kite.market_status(now)
    quotes = {}
    if st["phase"] == "open" and keys_of(rules):
        quotes = get_quotes(keys_of(rules), a.quotes)
        kite.write_json(a.prices, {"at": kite.iso_ist(now), "source": "offline" if a.quotes else "kite",
                                   "prices": {k: v["last"] for k, v in quotes.items()}})
    state = doc.setdefault("rang", {})
    rung = evaluate(rules, quotes, now, state)
    for rid, r, msg in rung:
        print("RING  #%s  %s" % (rid, msg))
        state[rid] = now.date().isoformat()
    if not rung:
        print("%s  nothing hit (%d alert%s, market %s)" % (now.strftime("%H:%M"), len(rules),
                                                          "s" if len(rules) != 1 else "", st["phase"]))
    kite.write_json(a.file, doc)
    return 0


def cmd_list(a):
    doc = load_rules(a.file)
    for r in doc.get("rules", []):
        print("#%s  %s%s" % (r["id"], describe(r), "  (rang %s)" % doc.get("rang", {}).get(r["id"])
                             if doc.get("rang", {}).get(r["id"]) else ""))
    return 0


def cmd_reset(a):
    kite.write_json(a.file, {"rules": []})
    print("cleared %s" % a.file)
    return 0


def cmd_quote(a):
    q = get_quotes([kite.normalize_key(k) for k in a.keys], a.quotes)
    for k, v in q.items():
        chg = 100 * (v["last"] / v["prev_close"] - 1) if v["prev_close"] else 0
        print("%-26s %10.2f  %+.2f%% vs prev close %.2f" % (k, v["last"], chg, v["prev_close"]))
    return 0


def main(argv=None):
    kite.safe_stdout()
    ap = argparse.ArgumentParser(description="Price alerts on Kite LTP, in IST, with the NSE calendar")
    sub = ap.add_subparsers(dest="cmd")

    def common(s):
        s.add_argument("--file", default="trader/alerts.json")
        s.add_argument("--now")
        s.add_argument("--quotes")
    s = sub.add_parser("add")
    common(s)
    s.add_argument("--from-plan", action="store_true")
    s.add_argument("--plan", default="trader/plan.json")
    s.add_argument("--key")
    s.add_argument("--above", type=float)
    s.add_argument("--below", type=float)
    s.add_argument("--pct-move", type=float)
    s.add_argument("--time")
    s.add_argument("--opening-bell", action="store_true")
    s.add_argument("--note")
    s = sub.add_parser("check")
    common(s)
    s.add_argument("--prices", default="trader/prices.json")
    for n in ("list", "reset"):
        common(sub.add_parser(n))
    s = sub.add_parser("clock")
    s.add_argument("--now")
    s = sub.add_parser("quote")
    common(s)
    s.add_argument("keys", nargs="+")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        if a.cmd == "clock":
            return kite.main(["clock"] + (["--now", a.now] if a.now else []))
        return {"add": cmd_add, "check": cmd_check, "list": cmd_list, "reset": cmd_reset,
                "quote": cmd_quote}[a.cmd](a)
    except KiteError as e:
        print("alerts: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
