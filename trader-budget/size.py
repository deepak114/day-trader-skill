#!/usr/bin/env python3
"""Budget Manager for trader-budget (NSE equity, NFO futures and options). Standard library + kite.py.

Sizes the plan in trader/plan.json to your rules and writes ONE trade (its Kite
orders in sequence) to trader/orders.json. Nothing is placed.

    python size.py                         # trader/plan.json + trader/config.json
    python size.py --write                 # also write trader/orders.json for /trader-risk
    python size.py --max-loss 2500 --capital 300000
    python size.py --no-kite               # skip Kite's margin/charges calculators (estimates only)

Quantity is whole lots (equity: lot 1). The lots are the most that fit inside ALL of:
  max_loss_per_trade   loss at the stop (risk per lot x lots) <= this, in rupees
  max_lots             per trade, {"index": 4, "stock": 2} (F&O only)
  max_quantity         equity shares per trade (optional)
  max_notional         rupee value of the position (optional; F&O uses contract value)
  margin               required margin <= funds available minus cash_buffer_pct
                       (margin from Kite's /margins calculator when signed in, else an estimate,
                       flagged as such: equity MIS ~20 % of value, futures ~15 % index / 20 % stock,
                       option buying = the full premium; option selling needs Kite's number)
It says which limit decided it. A stop too far for your max loss on even ONE lot is a
plain refusal, not a smaller order. Charges (brokerage, STT, exchange, SEBI, stamp, GST)
come from Kite's virtual contract note when signed in.

The orders, in the sequence you place them in Kite:
  single:   1 entry (MARKET, or LIMIT at --limit)   2 stop: SL order, trigger at the stop,
            limit sl_limit_buffer_pct beyond it      3 target: MIS -> an alert (Kite has no OCO
            for intraday orders); NRML/CNC -> a GTT OCO (stop + target) after the entry fills
  strategy: hedge (BUY) legs first, then SELL legs, LIMIT at the plan's leg prices;
            exits are watched by /trader-alerts on the strategy's net value
"""

import argparse
import hashlib
import json
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trader-kite"))
import kite  # noqa: E402
from kite import KiteError  # noqa: E402

DEFAULTS = {
    "capital": None, "max_loss_per_trade": 2000, "cash_buffer_pct": 10, "max_lots": {"index": 2, "stock": 1},
    "max_quantity": None, "max_notional": None,
    "sl_limit_buffer_pct": {"equity": 0.5, "future": 0.1, "option": 2.0},
}
MIN_BUFFER_TICKS = 4
EST_MARGIN = {"equity": 0.20, "future_index": 0.15, "future_stock": 0.20}


def cfg_get(cfg, k):
    v = cfg.get(k)
    return DEFAULTS.get(k) if v is None else v


def fingerprint(trade):
    core = [{k: o.get(k) for k in ("exchange", "tradingsymbol", "transaction_type", "quantity", "product",
                                    "order_type", "price", "trigger_price")} for o in trade["orders"]]
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def underlying_kind(plan):
    name = plan["tradingsymbol"]
    for n in kite.INDEX_NAMES:
        if name.startswith(n):
            return "index"
    return "stock"


def opposite(side):
    return "SELL" if side.upper() == "BUY" else "BUY"


def buffer_pct(cfg, segment):
    b = cfg_get(cfg, "sl_limit_buffer_pct")
    if isinstance(b, dict):
        b = b.get(segment, DEFAULTS["sl_limit_buffer_pct"].get(segment, 0.5))
    return float(b)


def sl_limit(trigger, exit_side, pct, tick):
    """Stop-limit's limit price: beyond the trigger by pct (at least MIN_BUFFER_TICKS ticks), so it
    still fills in a fast move. NSE does not take SL-M orders on options, so every stop here is SL."""
    gap = max(trigger * pct / 100.0, MIN_BUFFER_TICKS * float(tick or 0.05))
    if exit_side == "SELL":
        return kite.round_tick(trigger - gap, tick, "down")
    return kite.round_tick(trigger + gap, tick, "up")


def single_orders(plan, qty, cfg, limit=None):
    side = plan["side"].upper()
    ex, sym, prod, tick = plan["exchange"], plan["tradingsymbol"], plan["product"], plan["tick_size"]
    base = {"variety": "regular", "exchange": ex, "tradingsymbol": sym, "product": prod, "validity": "DAY",
            "quantity": qty}
    entry = dict(base, step=1, role="entry", transaction_type=side,
                 order_type="LIMIT" if limit else "MARKET", price=limit or 0, trigger_price=0)
    xs = opposite(side)
    bp = buffer_pct(cfg, plan["segment"])
    stop = dict(base, step=2, role="stop", transaction_type=xs, order_type="SL", trigger_price=plan["stop"],
                price=sl_limit(plan["stop"], xs, bp, tick), note="place right after the entry fills")
    out = [entry]
    if prod == "MIS":
        out.append(stop)
        out.append({"step": 3, "role": "target", "kind": "alert", "price": plan["target"],
                    "note": "Kite has no OCO for intraday orders: /trader-alerts rings at %.2f; then modify the "
                            "stop order to LIMIT %.2f (or exit at market) so you never hold two exit orders"
                            % (plan["target"], plan["target"])})
        out.append({"step": 4, "role": "flat_by", "kind": "alert", "time": plan.get("flat_by"),
                    "note": "exit whatever is open by %s IST, before Zerodha's auto square-off" % plan.get("flat_by")})
    else:
        out.append({"step": 2, "role": "gtt", "kind": "gtt", "type": "two-leg",
                    "condition": {"exchange": ex, "tradingsymbol": sym, "trigger_values": sorted(
                        [plan["stop"], plan["target"]])},
                    "orders": [
                        {"exchange": ex, "tradingsymbol": sym, "transaction_type": xs, "quantity": qty,
                         "order_type": "LIMIT", "product": prod, "price": sl_limit(plan["stop"], xs, bp, tick)},
                        {"exchange": ex, "tradingsymbol": sym, "transaction_type": xs, "quantity": qty,
                         "order_type": "LIMIT", "product": prod, "price": plan["target"]}],
                    "note": "GTT OCO: stop and target, one cancels the other; place it right after the entry fills "
                            "(a GTT stays live across days, unlike a DAY order)"})
    return out


def strategy_orders(plan, lots):
    legs = sorted(plan["legs"], key=lambda l: 0 if l["side"].upper() == "BUY" else 1)
    out = []
    for i, l in enumerate(legs, 1):
        out.append({"step": i, "role": "leg", "variety": "regular", "exchange": l["exchange"],
                    "tradingsymbol": l["tradingsymbol"], "transaction_type": l["side"].upper(),
                    "quantity": lots * plan["lot_size"] * int(l.get("ratio", 1)), "product": plan["product"],
                    "order_type": "LIMIT", "price": l["price"], "trigger_price": 0, "validity": "DAY"})
    out.append({"step": len(out) + 1, "role": "exits", "kind": "alert",
                "note": "/trader-alerts watches the strategy's net value: stop at %.2f, target at %.2f per unit%s"
                        % (plan["stop_value"], plan["target_value"],
                           ", flat by %s IST" % plan["flat_by"] if plan.get("flat_by") else "")})
    return out


def estimate_margin(plan, qty):
    if plan.get("strategy", "single") != "single":
        per = plan.get("margin_per_lot")
        return per * (qty // int(plan.get("lot_size") or 1)) if per else None
    seg, price = plan["segment"], plan["entry"]
    if seg == "equity":
        return price * qty * (EST_MARGIN["equity"] if plan["product"] == "MIS" else 1.0)
    if seg == "future":
        return price * qty * EST_MARGIN["future_%s" % underlying_kind(plan)]
    if seg == "option" and plan["side"] == "buy":
        return price * qty
    return None


def margin_for(plan, orders, client):
    """Kite's number for the entry legs, or None."""
    rows = [o for o in orders if o.get("role") in ("entry", "leg")]
    if not client or not rows:
        return None
    rows = [dict(o, price=o.get("price") or (plan["entry"] if plan.get("strategy") == "single" else o["price"]))
            for o in rows]
    if len(rows) == 1:
        data = client.order_margins(rows)
        return float(data[0]["total"])
    data = client.basket_margins(rows, consider_positions=False)
    return float((data.get("final") or data.get("initial"))["total"])


def charges_for(plan, orders, client):
    if not client:
        return None
    rows = []
    for o in orders:
        if o.get("role") not in ("entry", "leg"):
            continue
        px = o.get("price") or plan["entry"]
        rows.append(dict(o, average_price=px))
        # the exit at the stop (the conservative case)
        xp = plan["stop"] if plan.get("strategy") == "single" else px
        rows.append(dict(o, transaction_type=opposite(o["transaction_type"]), average_price=xp))
    data = client.charges(rows)
    return round(sum(float(d["charges"]["total"]) for d in data), 2)


def size_trade(plan, cfg, capital=None, max_loss=None, client=None, limit=None):
    """-> trade dict. Raises KiteError when not even one lot fits."""
    lot = int(plan.get("lot_size") or 1)
    single = plan.get("strategy", "single") == "single"
    seg = plan["segment"]
    if single:
        risk_lot = plan["risk_per_unit"] * lot
        price = limit or plan["entry"]
    else:
        risk_lot = plan["max_loss_per_lot"]
        price = None
        if risk_lot is None:
            raise KiteError("this strategy's loss is not capped (naked short options): size it with "
                            "max loss at the stop only if your rules allow naked shorts; refusing by default")
    max_loss = float(max_loss if max_loss is not None else cfg_get(cfg, "max_loss_per_trade"))
    fund_src = "config capital"
    funds = capital
    if funds is None and client:
        try:
            f = client.funds("equity")
            funds, fund_src = float(f.get("net") or 0), "Kite funds (equity net)"
        except KiteError:
            funds = None
    if funds is None:
        funds = cfg_get(cfg, "capital")
    if funds is None:
        raise KiteError("no capital: sign in to Kite (funds are read automatically) or set \"capital\" in "
                        "trader/config.json")
    funds = float(funds)
    usable = funds * (1 - float(cfg_get(cfg, "cash_buffer_pct")) / 100.0)
    limits = {}
    if risk_lot <= 0:
        raise KiteError("the plan has no risk per lot")
    limits["max_loss_per_trade"] = int(math.floor(max_loss / risk_lot + 1e-9))
    if limits["max_loss_per_trade"] < 1:
        raise KiteError("one %s risks %s %s, more than your max loss per trade %s: %s or skip this trade "
                        "(no smaller order exists)" % (
                            "lot" if lot > 1 else "share", kite.inr(risk_lot),
                            "at the stop" if single else "in the worst case at expiry (%s)" % plan.get("sized_by"),
                            kite.inr(max_loss), "tighten the stop" if single else "narrow the spread"))
    if seg in ("future", "option") or not single:
        ml = cfg_get(cfg, "max_lots")
        cap = ml.get(underlying_kind(plan) if single else ("index" if plan.get("underlying") in kite.INDEX_NAMES
                                                            else "stock")) if isinstance(ml, dict) else ml
        if cap:
            limits["max_lots"] = int(cap)
    elif cfg_get(cfg, "max_quantity"):
        limits["max_quantity"] = int(cfg_get(cfg, "max_quantity"))
    if cfg_get(cfg, "max_notional") and single:
        limits["max_notional"] = int(float(cfg_get(cfg, "max_notional")) // (price * lot))
    lots = max(1, min(limits.values()))
    # margin: the trial size first, then shrink while it does not fit
    margin_src = "estimate"
    for _ in range(64):
        qty = lots * lot
        orders = single_orders(plan, qty, cfg, limit) if single else strategy_orders(plan, lots)
        m = None
        if client:
            try:
                m = margin_for(plan, orders, client)
                margin_src = "kite"
            except KiteError:
                m = None
        if m is None:
            m = estimate_margin(plan, qty)
            margin_src = "estimate" if m is not None else "unknown"
        if m is None or m <= usable or lots == 1:
            break
        per = m / lots
        lots = max(1, int(usable // per)) if int(usable // per) < lots else lots - 1
    if m is not None and m > usable:
        raise KiteError("one %s needs %s margin (%s), more than your usable funds %s (%s less a %s%% buffer)"
                        % ("lot" if lot > 1 else "share", kite.inr(m), margin_src, kite.inr(usable), kite.inr(funds),
                           cfg_get(cfg, "cash_buffer_pct")))
    if m is not None and lots < min(limits.values()):
        limits["margin"] = lots
    decided = min(limits, key=lambda k: limits[k]) if lots == min(limits.values()) else "margin"
    qty = lots * lot
    charges = None
    if client:
        try:
            charges = charges_for(plan, orders, client)
        except KiteError:
            charges = None
    loss = round(risk_lot * lots, 2)
    trade = {
        "id": "T1", "strategy": plan.get("strategy", "single"), "key": plan.get("key"),
        "segment": seg, "side": plan.get("side"), "product": plan["product"], "date": plan.get("date"),
        "lots": lots, "lot_size": lot, "quantity": qty, "entry": plan.get("entry"),
        "stop": plan.get("stop"), "target": plan.get("target"),
        "max_loss": loss, "max_loss_with_charges": round(loss + (charges or 0), 2) if charges is not None else None,
        "reward_at_target": round(plan["reward_per_unit"] * lot * lots, 2) if plan.get("reward_per_unit") else None,
        "loss_at_stop": round(plan["risk_per_unit"] * lot * lots, 2) if plan.get("risk_per_unit") else None,
        "reward_risk": plan.get("reward_risk"), "defined_risk": single or plan.get("defined_risk", False),
        "margin_required": round(m, 2) if m is not None else None, "margin_source": margin_src,
        "funds": round(funds, 2), "funds_source": fund_src, "usable_funds": round(usable, 2),
        "charges_round_trip": charges, "decided_by": decided, "limits": limits,
        "flat_by": plan.get("flat_by"), "expiry": plan.get("expiry"),
        "why": "%s %s: %d %s, bound by %s (%s at the stop)" % (
            plan.get("setup") or plan.get("strategy"), plan.get("key") or plan.get("underlying"), lots,
            "lot%s" % ("s" if lots > 1 else "") if lot > 1 else "share%s" % ("s" if qty > 1 else ""),
            decided.replace("_", " "), kite.inr(loss)),
        "orders": orders,
    }
    if not single:
        trade.update(underlying=plan.get("underlying"), legs=plan["legs"], stop_value=plan.get("stop_value"),
                     target_value=plan.get("target_value"), net_premium=plan.get("net_premium"),
                     breakevens=plan.get("breakevens"))
    trade["fingerprint"] = fingerprint(trade)
    return trade


def report(t):
    unit = "lot" if t["lot_size"] > 1 else "share"
    lines = ["SIZE  %s %s %s: %d %s%s%s = quantity %d" % (
        (t["side"] or "").upper() if t["strategy"] == "single" else t["strategy"].upper(),
        t.get("key") or t.get("underlying"), t["product"], t["lots"], unit, "s" if t["lots"] > 1 else "",
        " (lot %d)" % t["lot_size"] if t["lot_size"] > 1 else "", t["quantity"])]
    lines.append("  max loss %s %s%s" % (kite.inr(t["max_loss"]), "at the stop" if t["strategy"] == "single" else
                                        "(worst case at expiry; %s at the stop)" % kite.inr(t.get("loss_at_stop")), (
        ", %s with charges" % kite.inr(t["max_loss_with_charges"])) if t.get("max_loss_with_charges") else ""))
    if t.get("reward_at_target"):
        lines.append("  reward at the target %s (reward:risk %s)" % (kite.inr(t["reward_at_target"]),
                                                                     t.get("reward_risk")))
    if t.get("margin_required") is not None:
        lines.append("  margin %s (%s) of usable funds %s (%s)" % (kite.inr(t["margin_required"]),
                                                                  t["margin_source"], kite.inr(t["usable_funds"]),
                                                                  t["funds_source"]))
    else:
        lines.append("  margin: not known (sign in to Kite for its calculator)")
    if t.get("charges_round_trip") is not None:
        lines.append("  charges, entry + exit at the stop: %s (Kite contract-note estimate)" %
                     kite.inr(t["charges_round_trip"]))
    lines.append("  limits (%s): %s" % ("most that fit", ", ".join("%s %d" % (k, v) for k, v in t["limits"].items())))
    lines.append("  decided by: %s" % t["decided_by"].replace("_", " "))
    lines.append("  orders, in sequence:")
    for o in t["orders"]:
        if o.get("kind") == "alert":
            lines.append("    %d. ALERT  %s" % (o["step"], o["note"]))
        elif o.get("kind") == "gtt":
            lines.append("    %d. GTT OCO %s triggers %s  (%s)" % (o["step"], o["condition"]["tradingsymbol"],
                                                                  " / ".join("%.2f" % v for v in
                                                                             o["condition"]["trigger_values"]),
                                                                  o["note"]))
        else:
            px = "MARKET" if o["order_type"] == "MARKET" else "%s %.2f" % (o["order_type"], o["price"])
            if o["order_type"] in ("SL", "SL-M"):
                px = "%s trigger %.2f limit %.2f" % (o["order_type"], o["trigger_price"], o["price"])
            lines.append("    %d. %s %d %s:%s %s %s" % (o["step"], o["transaction_type"], o["quantity"], o["exchange"],
                                                       o["tradingsymbol"], o["product"], px))
    return "\n".join(lines)


def main(argv=None):
    kite.safe_stdout()
    ap = argparse.ArgumentParser(description="Budget Manager: size the plan in lots and write the Kite orders")
    ap.add_argument("--plan", default="trader/plan.json")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--out", default="trader/orders.json")
    ap.add_argument("--capital", type=float)
    ap.add_argument("--max-loss", type=float)
    ap.add_argument("--limit", type=float, help="enter with a LIMIT order at this price instead of MARKET")
    ap.add_argument("--no-kite", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args(argv)
    try:
        plan = kite.read_json(a.plan, "plan (run /trader-exit or /trader-options first)", required=True)
        cfg = kite.load_config(a.config)
        client = None
        if not a.no_kite:
            try:
                kite.credentials(a.config)
                client = kite.Kite()
            except KiteError as e:
                print("note: %s; using estimates" % e)
        t = size_trade(plan, cfg, a.capital, a.max_loss, client, a.limit)
        print(json.dumps(t, indent=2) if a.json else report(t))
        if a.write:
            kite.write_json(a.out, {"created_by": "trader-budget", "created_at": kite.iso_ist(kite.now_ist()),
                                    "plan": a.plan, "trades": [t]})
            print("wrote %s -> next: /trader-risk" % a.out)
    except KiteError as e:
        print("budget: %s" % e, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
