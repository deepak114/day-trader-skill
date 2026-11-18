#!/usr/bin/env python3
"""Order Tickets for trader-broker (Zerodha Kite). Standard library + ../trader-kite/kite.py.

YOU place every order in Kite (web or app). This script never places, modifies or
cancels anything: it turns each trade that /trader-risk passed today into a plain
ticket with every field Kite's order window asks for, then keeps the log.

    python ticket.py show                     # every trade, READY (risk PASS today) or not
    python ticket.py placed --trade T1 [--price 1512.6] [--order-id 2511180001234]
    python ticket.py sync                     # read today's fills from Kite (read only) into the log
    python ticket.py closed --trade T1 --price 1529.2       # you exited: records the P&L
    python ticket.py flatten                  # exit tickets for every open MIS position (from Kite,
                                              # read only), or --key NSE:RELIANCE --qty 297 --side sell
    python ticket.py log

The log (trader/state/broker.json) feeds /trader-risk: trades placed today, the
risk still open, the P&L closed. A ticket is shown as READY only when its exact
fingerprint got a PASS from /trader-risk today.
"""

import argparse
import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trader-kite"))
import kite  # noqa: E402
from kite import KiteError  # noqa: E402

PRODUCT_WORDS = {"MIS": "Intraday (MIS)", "CNC": "Longterm / Delivery (CNC)", "NRML": "Overnight (NRML)"}
TYPE_WORDS = {"MARKET": "Market", "LIMIT": "Limit", "SL": "SL (stop-limit)", "SL-M": "SL-M (stop-market)"}


def risk_verdict(risk, fp, today):
    rows = [v for v in (risk.get("verdicts") or []) if v.get("fingerprint") == fp and v.get("date") == today]
    return rows[-1] if rows else None


def status_of(t, risk, state, today):
    rv = risk_verdict(risk, t.get("fingerprint"), today)
    placed = [r for r in state.get("log", []) if r.get("fingerprint") == t.get("fingerprint")
              and str(r.get("at", "")).startswith(today) and r.get("event") == "placed"]
    if placed:
        return "PLACED", rv
    if not rv:
        return "NOT CHECKED", rv
    return ("READY" if rv["verdict"] == "PASS" else "BLOCKED"), rv


def order_lines(o, n):
    if o.get("kind") == "alert":
        return ["  %d. ALERT (not an order): %s" % (n, o["note"])]
    if o.get("kind") == "gtt":
        c = o["condition"]
        out = ["  %d. GTT, type OCO, on %s:%s, triggers %s (%s)" % (
            n, c["exchange"], c["tradingsymbol"], " and ".join("%.2f" % v for v in c["trigger_values"]), o["note"])]
        for leg in o["orders"]:
            out.append("       leg: %s %d, %s, Limit %.2f" % (leg["transaction_type"], leg["quantity"],
                                                             PRODUCT_WORDS.get(leg["product"], leg["product"]),
                                                             leg["price"]))
        return out
    px = ""
    if o["order_type"] == "LIMIT":
        px = ", price %.2f" % o["price"]
    elif o["order_type"] == "SL":
        px = ", trigger %.2f, price %.2f" % (o["trigger_price"], o["price"])
    elif o["order_type"] == "SL-M":
        px = ", trigger %.2f" % o["trigger_price"]
    out = ["  %d. %s %s:%s  qty %d  %s, %s%s, validity %s" % (
        n, o["transaction_type"], o["exchange"], o["tradingsymbol"], o["quantity"],
        PRODUCT_WORDS.get(o["product"], o["product"]), TYPE_WORDS.get(o["order_type"], o["order_type"]), px,
        o.get("validity", "DAY"))]
    if o.get("note"):
        out.append("       (%s)" % o["note"])
    return out


def render(t, idx, total, status, rv):
    lot = int(t.get("lot_size") or 1)
    size = "%d lot%s x %d = %d" % (t["lots"], "s" if t["lots"] > 1 else "", lot, t["quantity"]) if lot > 1 \
        else "%d shares" % t["quantity"]
    head = "TICKET %d of %d  [%s]  %s" % (idx, total, status, t.get("id"))
    what = ("%s %s" % ((t.get("side") or "").upper(), t.get("key"))) if t.get("strategy") == "single" else \
        "%s %s, expiry %s" % (t["strategy"].upper(), t.get("underlying"), t.get("expiry"))
    lines = [head, "  %s, %s, %s" % (what, size, PRODUCT_WORDS.get(t["product"], t["product"]))]
    if t.get("strategy") == "single":
        lines.append("  entry ~%.2f, stop %.2f, target %.2f; max loss about %s at the stop (can be worse in a gap "
                     "or at a circuit)" % (t["entry"], t["stop"], t["target"], kite.inr(t["max_loss"])))
    else:
        lines.append("  net %s %.2f a unit; worst case %s; exits on the net value: stop %.2f, target %.2f" % (
            "debit" if (t.get("net_premium") or 0) > 0 else "credit", abs(t.get("net_premium") or 0),
            kite.inr(t["max_loss"]), t["stop_value"], t["target_value"]))
    if t.get("margin_required") is not None:
        lines.append("  margin %s (%s)%s" % (kite.inr(t["margin_required"]), t.get("margin_source"),
                                           ", charges about %s round trip" % kite.inr(t["charges_round_trip"])
                                           if t.get("charges_round_trip") is not None else ""))
    if t.get("flat_by"):
        lines.append("  flat by %s IST" % t["flat_by"])
    lines.append("  why: %s" % t.get("why", ""))
    if status == "READY":
        lines.append("  Enter these in Kite yourself, in this order:")
        for i, o in enumerate(t["orders"], 1):
            lines += order_lines(o, i)
        lines.append("  Check the symbol, quantity and price on Kite's confirmation before you swipe.")
        lines.append("  Then tell me \"placed\" (and the fill price) so the log and the daily limit stay right.")
    elif status == "BLOCKED":
        lines.append("  BLOCKED by /trader-risk: %s" % "; ".join(rv.get("reasons") or []))
    elif status == "NOT CHECKED":
        lines.append("  Not checked: run /trader-risk on this exact trade first.")
    else:
        lines.append("  Already recorded as placed today.")
    return "\n".join(lines)


def load(a):
    doc = kite.read_json(a.orders, "orders (run /trader-budget first)", required=True)
    risk = kite.read_json(a.risk, default={}) or {}
    state = kite.read_json(a.state, default={}) or {}
    return doc, risk, state


def pick(doc, tid):
    for t in doc.get("trades", []):
        if t.get("id") == tid:
            return t
    raise KiteError("no trade %s in the orders file" % tid)


def append(state_path, state, row):
    state.setdefault("log", []).append(row)
    kite.write_json(state_path, state)


def cmd_show(a):
    doc, risk, state = load(a)
    today = kite.now_ist(a.now).date().isoformat()
    trades = doc.get("trades", [])
    if not trades:
        print("no trades in %s" % a.orders)
        return 0
    for i, t in enumerate(trades, 1):
        st, rv = status_of(t, risk, state, today)
        print(render(t, i, len(trades), st, rv))
        print()
    return 0


def cmd_placed(a):
    doc, risk, state = load(a)
    now = kite.now_ist(a.now)
    t = pick(doc, a.trade)
    st, rv = status_of(t, risk, state, now.date().isoformat())
    if st == "PLACED":
        raise KiteError("%s is already recorded as placed today" % a.trade)
    if st != "READY" and not t.get("exit_only") and not a.anyway:
        raise KiteError("%s is %s: it was not passed by /trader-risk today. If you placed it anyway, record it "
                        "with --anyway so the daily limit counts it" % (a.trade, st))
    entry = a.price or t.get("entry")
    loss = t.get("max_loss")
    if a.price and t.get("strategy") == "single" and t.get("stop"):
        loss = round(abs(a.price - t["stop"]) * t["quantity"], 2)
    append(a.state, state, {"at": kite.iso_ist(now), "event": "placed", "trade": t["id"],
                            "fingerprint": t.get("fingerprint"), "role": "exit" if t.get("exit_only") else "entry",
                            "key": t.get("key"), "side": t.get("side"), "quantity": t.get("quantity"),
                            "price": entry, "order_id": a.order_id, "max_loss": 0 if t.get("exit_only") else loss,
                            "risk_status": st})
    print("Recorded %s as placed%s. Max loss now counted for today's limit: %s." % (
        t["id"], " at %.2f" % a.price if a.price else "", kite.inr(loss)))
    cap = kite.load_config(a.config).get("max_loss_per_trade")
    if cap and loss and loss > float(cap) + 0.005:
        print("Note: the fill moved the loss at the stop to %s, over your max loss per trade %s (slippage)."
              % (kite.inr(loss), kite.inr(float(cap))))
    if t.get("strategy") == "single" and t.get("product") == "MIS":
        print("Next: place the stop order (ticket step 2) now if you have not, then "
              "`alerts.py add --from-plan` for the target and flat-by alerts.")
    return 0


def cmd_closed(a):
    doc, risk, state = load(a)
    now = kite.now_ist(a.now)
    t = pick(doc, a.trade)
    today = now.date().isoformat()
    rows = [r for r in state.get("log", []) if r.get("trade") == t["id"] and r.get("event") == "placed"
            and str(r.get("at", "")).startswith(today)]
    if not rows:
        raise KiteError("%s is not recorded as placed today" % a.trade)
    if a.pnl is not None:
        pnl = a.pnl
    elif a.price is not None and t.get("strategy") == "single":
        e = rows[-1].get("price") or t["entry"]
        pnl = (a.price - e) * t["quantity"] * (1 if t["side"] == "buy" else -1)
    else:
        raise KiteError("give --price (single) or --pnl (strategies, or to include charges)")
    append(a.state, state, {"at": kite.iso_ist(now), "event": "closed", "trade": t["id"], "key": t.get("key"),
                            "price": a.price, "pnl": round(pnl, 2)})
    print("Recorded %s closed: P&L %s (before charges unless you passed --pnl net)." % (t["id"], kite.inr(pnl)))
    return 0


def cmd_sync(a):
    doc, risk, state = load(a)
    now = kite.now_ist(a.now)
    client = kite.Kite()
    fills = client.trades() or []
    syms = {}
    for t in doc.get("trades", []):
        for o in t.get("orders", []):
            if o.get("tradingsymbol"):
                syms.setdefault(o["tradingsymbol"], t["id"])
    seen = {r.get("kite_trade_id") for r in state.get("log", [])}
    n = 0
    for f in fills:
        if f.get("trade_id") in seen or f.get("tradingsymbol") not in syms:
            continue
        append(a.state, state, {"at": kite.iso_ist(now), "event": "fill", "trade": syms[f["tradingsymbol"]],
                                "kite_trade_id": f.get("trade_id"), "order_id": f.get("order_id"),
                                "key": "%s:%s" % (f.get("exchange"), f.get("tradingsymbol")),
                                "side": f.get("transaction_type"), "quantity": f.get("quantity"),
                                "price": f.get("average_price"), "time": f.get("fill_timestamp")})
        n += 1
    print("Synced %d new fill(s) from Kite (read only)." % n)
    return 0


def cmd_flatten(a):
    now = kite.now_ist(a.now)
    rows = []
    if a.key:
        if not a.qty or not a.side:
            raise KiteError("with --key also give --qty and --side (the side that closes it)")
        inst = kite.Instruments(kite.exchanges_for(a.key))
        ins = inst.get(a.key)
        rows.append((ins, a.qty, a.side.upper(), a.product.upper()))
    else:
        client = kite.Kite()
        pos = (client.positions() or {}).get("net") or []
        inst = kite.Instruments(("NSE", "NFO", "BSE", "BFO"))
        for p in pos:
            q = int(p.get("quantity") or 0)
            if q == 0 or (p.get("product") != "MIS" and not a.all_products):
                continue
            ins = inst.get("%s:%s" % (p["exchange"], p["tradingsymbol"]))
            rows.append((ins, abs(q), "SELL" if q > 0 else "BUY", p.get("product")))
    if not rows:
        print("nothing open to flatten")
        return 0
    trades = []
    # buy back shorts first (frees margin, removes the open-ended side), then sell longs
    rows.sort(key=lambda r: 0 if r[2] == "BUY" else 1)
    for i, (ins, q, side, prod) in enumerate(rows, 1):
        t = {"id": "X%d" % i, "strategy": "single", "role": "exit", "exit_only": True, "key": ins.key,
             "segment": kite.segment_of(ins), "side": side.lower(), "product": prod, "lots": q // ins["lot_size"],
             "lot_size": ins["lot_size"], "quantity": q, "entry": None, "stop": None, "target": None,
             "max_loss": 0, "why": "flatten by the close", "orders": [
                 {"step": 1, "role": "exit", "variety": "regular", "exchange": ins["exchange"],
                  "tradingsymbol": ins["tradingsymbol"], "transaction_type": side, "quantity": q, "product": prod,
                  "order_type": "MARKET", "price": 0, "trigger_price": 0, "validity": "DAY",
                  "note": "cancel this position's open SL / target orders in Kite first, or they may fill after"}]}
        import hashlib
        import json
        t["fingerprint"] = hashlib.sha256(json.dumps(t["orders"], sort_keys=True).encode()).hexdigest()[:16]
        trades.append(t)
    kite.write_json(a.orders, {"created_by": "trader-broker flatten", "created_at": kite.iso_ist(now),
                               "trades": trades})
    print("Wrote %d exit trade(s) to %s. Next: /trader-risk (exits always pass the day limits), then "
          "`ticket.py show`." % (len(trades), a.orders))
    return 0


def cmd_log(a):
    state = kite.read_json(a.state, default={}) or {}
    today = kite.now_ist(a.now).date().isoformat()
    rows = [r for r in state.get("log", []) if str(r.get("at", "")).startswith(today)]
    if not rows:
        print("nothing logged today")
        return 0
    pnl = 0.0
    for r in rows:
        if r.get("event") == "closed":
            pnl += r.get("pnl") or 0
        print("%s  %-7s %-4s %-28s %s%s" % (r["at"][11:16], r["event"], r.get("trade", ""), r.get("key", ""),
                                           "qty %s " % r["quantity"] if r.get("quantity") else "",
                                           "@ %.2f" % r["price"] if r.get("price") else ""))
    print("closed P&L today (before charges): %s" % kite.inr(pnl))
    return 0


def main(argv=None):
    kite.safe_stdout()
    ap = argparse.ArgumentParser(description="Order tickets you place in Kite yourself, and the trade log")
    sub = ap.add_subparsers(dest="cmd")

    def common(s):
        s.add_argument("--orders", default="trader/orders.json")
        s.add_argument("--risk", default="trader/state/risk.json")
        s.add_argument("--state", default="trader/state/broker.json")
        s.add_argument("--config", default="trader/config.json")
        s.add_argument("--now")
    s = sub.add_parser("show")
    common(s)
    s = sub.add_parser("placed")
    common(s)
    s.add_argument("--trade", required=True)
    s.add_argument("--price", type=float)
    s.add_argument("--order-id")
    s.add_argument("--anyway", action="store_true")
    s = sub.add_parser("closed")
    common(s)
    s.add_argument("--trade", required=True)
    s.add_argument("--price", type=float)
    s.add_argument("--pnl", type=float)
    s = sub.add_parser("sync")
    common(s)
    s = sub.add_parser("flatten")
    common(s)
    s.add_argument("--key")
    s.add_argument("--qty", type=int)
    s.add_argument("--side", choices=("buy", "sell", "BUY", "SELL"))
    s.add_argument("--product", default="MIS")
    s.add_argument("--all-products", action="store_true")
    s = sub.add_parser("log")
    common(s)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"show": cmd_show, "placed": cmd_placed, "closed": cmd_closed, "sync": cmd_sync,
                "flatten": cmd_flatten, "log": cmd_log}[a.cmd](a)
    except KiteError as e:
        print("ticket: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
