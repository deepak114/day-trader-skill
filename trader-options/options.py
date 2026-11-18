#!/usr/bin/env python3
"""Options Strategist for trader-options (NFO / BFO). Standard library + ../trader-kite/kite.py.

Builds an options trade from the live chain and writes trader/plan.json for the
Time Machine (on the underlying), the Budget Manager and the Risk Manager:

    python options.py chain NIFTY [--expiry 2026-11-23] [--strikes 8]
    python options.py build NIFTY --strategy bull-call-spread              # nearest expiry, ATM, 1 step wide
    python options.py build NIFTY --strategy bull-put-spread --otm 2 --width 2 --write
    python options.py build BANKNIFTY --strategy iron-condor --short-delta 0.2 --width 2
    python options.py build RELIANCE --strategy long-call --otm 1 --stop 40 --target 2R
    python options.py build NIFTY --legs "BUY 26000 CE, SELL 26200 CE" --expiry 2026-12-01
    python options.py show
    ... offline: --inst-file NFO.csv --quotes quotes.json --spot 25980 --now "2026-11-18 10:00"

Strategies (strike steps from the at-the-money strike; --otm N steps out, --width N steps wide):
  debit (defined risk):  long-call, long-put, bull-call-spread, bear-put-spread  (--otm default 0: ATM)
  credit (defined risk): bull-put-spread, bear-call-spread, iron-condor, iron-fly (--otm default 1)
  credit (UNDEFINED risk, refused unless config allows naked shorts): short-straddle, short-strangle
  --short-delta D picks the short strike(s) nearest to that delta instead of --otm.

Prices are the touch: a BUY leg at the best ask, a SELL leg at the best bid (last
price when there is no depth), so the plan never assumes a fill better than the book.
Payoff is per unit at expiry; per lot = x lot size. Max loss / max profit / breakevens
come from the piecewise-linear payoff (0 to 3x spot, checked for unlimited ends).

Exit levels are on the strategy's NET VALUE (what it costs to close, per unit):
  debit: stop when the value falls --stop % (default 50) below the debit; target --target R
         (default 1R) above it, capped at the spread's width
  credit: stop when closing costs --stop-x times the credit (default 2.0); target when
         --capture % of the credit is kept (default 50)
Sizing uses the worst case at expiry for defined-risk trades (a gap can jump a stop);
set "size_spreads_by": "stop" in trader/config.json to size by the loss at the stop instead.
It never places an order.
"""

import argparse
import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trader-kite"))
import kite  # noqa: E402
from kite import KiteError  # noqa: E402

DEBIT = {"long-call", "long-put", "bull-call-spread", "bear-put-spread"}
CREDIT = {"bull-put-spread", "bear-call-spread", "iron-condor", "iron-fly"}
NAKED = {"short-straddle", "short-strangle"}
STRATEGIES = DEBIT | CREDIT | NAKED
FLAT_BY = "15:15"


def legs_for(strategy, strikes, atm, otm=1, width=1, short_by_delta=None):
    """-> [(side, strike, kind, ratio)]. strikes sorted; atm one of them."""
    i = strikes.index(atm)

    def at(j):
        if not 0 <= j < len(strikes):
            raise KiteError("the strikes for %s run off the chain (try a smaller --otm/--width)" % strategy)
        return strikes[j]
    sc = short_by_delta.get("CE") if short_by_delta else None
    sp = short_by_delta.get("PE") if short_by_delta else None
    ic, ip = (strikes.index(sc) if sc else i + otm), (strikes.index(sp) if sp else i - otm)
    s = strategy
    if s == "long-call":
        return [("BUY", at(i + otm), "CE", 1)]
    if s == "long-put":
        return [("BUY", at(i - otm), "PE", 1)]
    if s == "bull-call-spread":
        return [("BUY", at(i + otm), "CE", 1), ("SELL", at(i + otm + width), "CE", 1)]
    if s == "bear-put-spread":
        return [("BUY", at(i - otm), "PE", 1), ("SELL", at(i - otm - width), "PE", 1)]
    if s == "bull-put-spread":
        return [("SELL", at(ip), "PE", 1), ("BUY", at(ip - width), "PE", 1)]
    if s == "bear-call-spread":
        return [("SELL", at(ic), "CE", 1), ("BUY", at(ic + width), "CE", 1)]
    if s == "iron-condor":
        return [("SELL", at(ip), "PE", 1), ("BUY", at(ip - width), "PE", 1),
                ("SELL", at(ic), "CE", 1), ("BUY", at(ic + width), "CE", 1)]
    if s == "iron-fly":
        return [("SELL", at(i), "PE", 1), ("SELL", at(i), "CE", 1),
                ("BUY", at(i - width), "PE", 1), ("BUY", at(i + width), "CE", 1)]
    if s == "short-straddle":
        return [("SELL", at(i), "CE", 1), ("SELL", at(i), "PE", 1)]
    if s == "short-strangle":
        return [("SELL", at(ic), "CE", 1), ("SELL", at(ip), "PE", 1)]
    raise KiteError("unknown strategy %r" % strategy)


def parse_legs(text):
    """'BUY 26000 CE, SELL 26200 CE x2' -> [(side, strike, kind, ratio)]."""
    out = []
    for part in str(text).split(","):
        m = re.match(r"^\s*(BUY|SELL)\s+(\d+(?:\.\d+)?)\s*(CE|PE)(?:\s*X\s*(\d+))?\s*$", part.strip().upper())
        if not m:
            raise KiteError("cannot read the leg %r (use e.g. 'BUY 26000 CE, SELL 26200 CE')" % part.strip())
        out.append((m.group(1), float(m.group(2)), m.group(3), int(m.group(4) or 1)))
    if not out:
        raise KiteError("no legs")
    return out


def leg_price(q, side):
    """The touch: ask for a buy, bid for a sell; last price when the book is empty."""
    depth = (q or {}).get("depth") or {}
    book = depth.get("sell" if side == "BUY" else "buy") or []
    px = book[0].get("price") if book and book[0].get("price") else None
    return float(px) if px else (float(q["last_price"]) if q and q.get("last_price") else None)


def payoff(legs, S):
    """Per-unit P&L at expiry with the underlying at S (premiums included)."""
    tot = 0.0
    for l in legs:
        intr = max(0.0, S - l["strike"]) if l["kind"] == "CE" else max(0.0, l["strike"] - S)
        sign = 1 if l["side"] == "BUY" else -1
        tot += sign * l["ratio"] * (intr - l["price"])
    return tot


def analyse(legs, spot):
    ks = sorted({l["strike"] for l in legs})
    grid = sorted(set([0.0] + ks + [spot, spot * 3]))
    vals = [(S, payoff(legs, S)) for S in grid]
    call_net = sum((1 if l["side"] == "BUY" else -1) * l["ratio"] for l in legs if l["kind"] == "CE")
    put_net = sum((1 if l["side"] == "BUY" else -1) * l["ratio"] for l in legs if l["kind"] == "PE")
    unlimited_up_loss = call_net < 0
    naked_put = put_net < 0
    mx = max(v for _, v in vals)
    mn = min(v for _, v in vals)
    be = []
    for (a, va), (b, vb) in zip(vals[:-1], vals[1:]):
        if va == 0:
            be.append(a)
        elif va * vb < 0:
            be.append(a + (b - a) * (-va) / (vb - va))
    defined = not unlimited_up_loss and not naked_put
    return {
        "max_profit": None if call_net > 0 else round(mx, 2),
        "max_loss": None if not defined else round(-mn, 2) if mn < 0 else 0.0,
        "breakevens": sorted({round(x, 2) for x in be}),
        "defined_risk": defined,
        "unlimited": "upside" if unlimited_up_loss else ("downside to zero" if naked_put else None),
    }


def exit_levels(net, width_value, a, tick, worst_unit=None):
    """Stop/target on the net value per unit. net > 0 debit, < 0 credit. A credit stop is
    capped at the most the position can cost to close (credit + worst case), else it could never fire."""
    if net > 0:
        stop = kite.round_tick(net * (1 - a.stop / 100.0), tick, "down")
        risk = net - stop
        r = float(re.match(r"^(\d+(\.\d+)?)r$", a.target.lower()).group(1)) if re.match(r"^\d+(\.\d+)?r$",
                                                                                         a.target.lower()) else 1.0
        target = net + r * risk
        if width_value:
            target = min(target, width_value - tick)
        target = kite.round_tick(target, tick, "down")
        return stop, target, round(risk, 2), round(target - net, 2)
    c = -net
    stop = kite.round_tick(c * a.stop_x, tick, "up")
    if worst_unit is not None and stop > c + worst_unit:
        stop = kite.round_tick(c + worst_unit, tick, "down")
    target = kite.round_tick(c * (1 - a.capture / 100.0), tick, "down")
    return stop, target, round(stop - c, 2), round(c - target, 2)


def build(a, inst, quotes, spot, now, cfg):
    name = a.name.upper()
    expiry = a.expiry or (inst.expiries(name, ("CE", "PE"), today=now.date()) or [None])[0]
    if not expiry:
        raise KiteError("no live option expiry for %s" % name)
    opts = inst.options(name, expiry)
    if not opts:
        raise KiteError("no options for %s expiring %s" % (name, expiry))
    strikes = sorted({o["strike"] for o in opts})
    atm = min(strikes, key=lambda k: abs(k - spot))
    lot, tick = opts[0]["lot_size"], opts[0]["tick_size"]
    if a.legs:
        raw = parse_legs(a.legs)
        strategy = "custom"
    else:
        if a.strategy not in STRATEGIES:
            raise KiteError("--strategy must be one of: %s" % ", ".join(sorted(STRATEGIES)))
        sbd = None
        if a.short_delta:
            rate = float(cfg.get("risk_free_rate", kite.DEFAULT_RATE))
            T = kite.days_to_expiry(expiry, now)
            sbd = {}
            for kind in ("CE", "PE"):
                best, bd = None, None
                for o in opts:
                    if o["instrument_type"] != kind:
                        continue
                    q = quotes.get(o.key) or {}
                    iv = kite.implied_vol(q.get("last_price"), spot, o["strike"], T, rate, kind)
                    if not iv:
                        continue
                    d = abs(kite.greeks(spot, o["strike"], T, rate, iv, kind)["delta"])
                    if (kind == "CE" and o["strike"] < atm) or (kind == "PE" and o["strike"] > atm):
                        continue
                    if bd is None or abs(d - a.short_delta) < bd:
                        best, bd = o["strike"], abs(d - a.short_delta)
                if best is not None:
                    sbd[kind] = best
        otm = a.otm if a.otm is not None else (0 if a.strategy in DEBIT or a.strategy == "short-straddle" else 1)
        raw = legs_for(a.strategy, strikes, atm, otm, a.width, sbd)
        strategy = a.strategy
    legs = []
    for side, k, kind, ratio in raw:
        o = inst.option(name, expiry, k, kind)
        q = quotes.get(o.key)
        px = leg_price(q, side)
        if px is None:
            raise KiteError("no price for %s (is the market open, and are you signed in to Kite?)" % o.key)
        legs.append({"side": side, "strike": k, "kind": kind, "ratio": ratio, "exchange": o["exchange"],
                     "tradingsymbol": o["tradingsymbol"], "price": round(px, 2), "expiry": expiry,
                     "last": (q or {}).get("last_price"), "oi": (q or {}).get("oi")})
    net = round(sum((1 if l["side"] == "BUY" else -1) * l["ratio"] * l["price"] for l in legs), 2)
    an = analyse(legs, spot)
    if strategy in NAKED or not an["defined_risk"]:
        allow = (cfg.get("allow") or {}).get("naked_short_options", False)
        if not allow and not a.force_naked:
            raise KiteError("%s has %s risk (%s). Refused: set \"allow\": {\"naked_short_options\": true} in "
                            "trader/config.json if you accept that, or use a defined-risk spread "
                            "(bull-put-spread, bear-call-spread, iron-condor)" % (
                                strategy, "unlimited" if an["unlimited"] == "upside" else "undefined",
                                an["unlimited"]))
    width_value = None
    if an["defined_risk"] and net > 0 and an["max_profit"] is not None:
        width_value = net + an["max_profit"]
    stop_v, target_v, risk_u, reward_u = exit_levels(net, width_value, a, tick, an["max_loss"])
    size_by = cfg.get("size_spreads_by", "max_loss")
    loss_stop_lot = round(risk_u * lot, 2)
    worst_lot = round(an["max_loss"] * lot, 2) if an["max_loss"] is not None else None
    if an["defined_risk"]:
        max_loss_lot = loss_stop_lot if size_by == "stop" else worst_lot
    else:
        max_loss_lot = loss_stop_lot if (cfg.get("allow") or {}).get("naked_short_options") or a.force_naked \
            else None
    product = (a.product or "MIS").upper()
    plan = {
        "created_by": "trader-options", "created_at": kite.iso_ist(now), "strategy": strategy,
        "underlying": name, "spot": spot, "atm": atm, "expiry": expiry, "date": now.date().isoformat(),
        "lot_size": lot, "tick_size": tick, "product": product, "legs": legs,
        "net_premium": net, "kind": "debit" if net > 0 else "credit",
        "max_profit_per_lot": round(an["max_profit"] * lot, 2) if an["max_profit"] is not None else None,
        "worst_case_per_lot": worst_lot, "loss_at_stop_per_lot": loss_stop_lot,
        "max_loss_per_lot": max_loss_lot, "sized_by": size_by if an["defined_risk"] else "stop (undefined risk)",
        "breakevens": an["breakevens"], "defined_risk": an["defined_risk"], "unlimited": an["unlimited"],
        "stop_value": stop_v, "target_value": target_v, "risk_per_unit": risk_u, "reward_per_unit": reward_u,
        "reward_risk": round(reward_u / risk_u, 2) if risk_u else None,
        "flat_by": (a.flat_by or (cfg.get("flat_by") or {}).get("option", FLAT_BY)
                    if isinstance(cfg.get("flat_by"), dict) else a.flat_by or FLAT_BY) if product == "MIS" else None,
        "segment": "option", "side": "buy" if net > 0 else "sell",
        "key": "%s %s %s" % (name, strategy, expiry), "setup": strategy,
        "note": "Prices are the touch (ask to buy, bid to sell). A gap can jump the stop: the worst case at "
                "expiry is the true risk of a defined-risk spread.",
    }
    plan["expiry_day"] = expiry == now.date().isoformat()
    return plan


def describe(p):
    lines = ["%s %s, expiry %s%s, spot %.2f, ATM %g, lot %d, %s" % (
        p["underlying"], p["strategy"].upper(), p["expiry"], " (EXPIRY DAY)" if p.get("expiry_day") else "",
        p["spot"], p["atm"], p["lot_size"], p["product"])]
    for l in p["legs"]:
        lines.append("  %-4s %s%s %-22s %g %s @ %.2f%s" % (l["side"], "%dx " % l["ratio"] if l["ratio"] > 1 else "",
                                                         l["exchange"], l["tradingsymbol"], l["strike"], l["kind"],
                                                         l["price"], "  OI %s" % l["oi"] if l.get("oi") else ""))
    net = p["net_premium"]
    lines.append("  net %s %.2f a unit (%s a lot)" % ("debit" if net > 0 else "credit", abs(net),
                                                    kite.inr(abs(net) * p["lot_size"])))
    lines.append("  at expiry, a lot: max profit %s, max loss %s, breakeven %s" % (
        kite.inr(p["max_profit_per_lot"]) if p["max_profit_per_lot"] is not None else "open-ended",
        kite.inr(p["worst_case_per_lot"]) if p["worst_case_per_lot"] is not None else "UNLIMITED (%s)" % p["unlimited"],
        " / ".join("%.2f" % b for b in p["breakevens"]) or "-"))
    lines.append("  exits on the net value: stop %.2f, target %.2f (loss at stop %s a lot, reward:risk %s)%s" % (
        p["stop_value"], p["target_value"], kite.inr(p["loss_at_stop_per_lot"]), p["reward_risk"],
        ", flat by %s IST" % p["flat_by"] if p.get("flat_by") else ""))
    lines.append("  sized by: %s" % p["sized_by"])
    if not p["defined_risk"]:
        lines.append("  WARNING: undefined risk. A gap or an expiry-day move can lose far more than the stop.")
    lines.append("  " + p["note"])
    return "\n".join(lines)


def _offline_inst(a, now):
    texts = {}
    for f in a.inst_file:
        ex = os.path.basename(f).split("-")[0].split(".")[0].upper()
        texts[ex if ex in ("NFO", "BFO", "NSE", "BSE") else "NFO"] = open(f, encoding="utf-8").read()
    return kite.Instruments(tuple(texts), texts=texts, today=now.date())


def _quotes_and_spot(a, inst, now, name, expiry, client):
    if a.quotes:
        doc = kite.read_json(a.quotes, "quotes", required=True)
        q = doc.get("data", doc)
        if a.spot is None:
            raise KiteError("offline quotes need --spot")
        return q, a.spot
    sk = inst.spot_key(name)
    spot = a.spot or client.ltp([sk])[sk]["last_price"]
    expiry = expiry or (inst.expiries(name, ("CE", "PE"), today=now.date()) or [None])[0]
    opts = inst.options(name, expiry)
    strikes = sorted({o["strike"] for o in opts})
    atm = min(strikes, key=lambda k: abs(k - spot))
    i = strikes.index(atm)
    span = set(strikes[max(0, i - 20): i + 21])
    return client.quote([o.key for o in opts if o["strike"] in span]), spot


def cmd_build(a):
    now = kite.now_ist(a.now)
    cfg = kite.load_config(a.config)
    name = a.name.upper()
    bse = name in ("SENSEX", "BANKEX", "SENSEX50")
    inst = _offline_inst(a, now) if a.inst_file else kite.Instruments(("BSE", "BFO") if bse else ("NSE", "NFO"))
    client = None if a.quotes else kite.Kite()
    quotes, spot = _quotes_and_spot(a, inst, now, name, a.expiry, client)
    p = build(a, inst, quotes, spot, now, cfg)
    if client:
        try:
            rows = [{"exchange": l["exchange"], "tradingsymbol": l["tradingsymbol"], "transaction_type": l["side"],
                     "product": p["product"], "order_type": "LIMIT", "quantity": p["lot_size"] * l["ratio"],
                     "price": l["price"]} for l in sorted(p["legs"], key=lambda l: l["side"] != "BUY")]
            m = client.basket_margins(rows, consider_positions=False)
            p["margin_per_lot"] = round(float((m.get("final") or m["initial"])["total"]), 2)
        except KiteError as e:
            p["margin_per_lot"], p["margin_note"] = None, str(e)
    print(describe(p))
    if p.get("margin_per_lot"):
        print("  margin a lot (Kite basket, hedge benefit included): %s" % kite.inr(p["margin_per_lot"]))
    if a.write:
        kite.write_json(a.out, p)
        print("wrote %s -> next: /trader-budget, then /trader-risk" % a.out)
    return 0


def cmd_chain(a):
    return kite.main(["chain", a.name] + (["--expiry", a.expiry] if a.expiry else []) +
                     ["--strikes", str(a.strikes)] + (["--now", a.now] if a.now else []))


def cmd_show(a):
    p = kite.read_json(a.out, "plan", required=True)
    if p.get("strategy") == "single":
        print("this plan is a single instrument: see /trader-exit show")
        return 0
    print(describe(p))
    return 0


def main(argv=None):
    kite.safe_stdout()
    ap = argparse.ArgumentParser(description="Options Strategist: spreads, condors and option buys from the chain")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("chain")
    s.add_argument("name")
    s.add_argument("--expiry")
    s.add_argument("--strikes", type=int, default=8)
    s.add_argument("--now")
    s = sub.add_parser("build")
    s.add_argument("name")
    s.add_argument("--strategy", default="bull-call-spread")
    s.add_argument("--legs")
    s.add_argument("--expiry")
    s.add_argument("--otm", type=int, help="strike steps out of the money (default 0 for debit, 1 for credit)")
    s.add_argument("--width", type=int, default=1)
    s.add_argument("--short-delta", type=float)
    s.add_argument("--stop", type=float, default=50.0, help="debit: stop at this %% loss of the debit")
    s.add_argument("--target", default="1R", help="debit: target in R")
    s.add_argument("--stop-x", type=float, default=2.0, help="credit: stop when closing costs Nx the credit")
    s.add_argument("--capture", type=float, default=50.0, help="credit: target when this %% of the credit is kept")
    s.add_argument("--product", choices=("MIS", "NRML", "mis", "nrml"))
    s.add_argument("--flat-by")
    s.add_argument("--force-naked", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--now")
    s.add_argument("--inst-file", action="append")
    s.add_argument("--quotes")
    s.add_argument("--spot", type=float)
    s.add_argument("--config", default="trader/config.json")
    s.add_argument("--out", default="trader/plan.json")
    s.add_argument("--write", action="store_true")
    s = sub.add_parser("show")
    s.add_argument("--out", default="trader/plan.json")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"build": cmd_build, "chain": cmd_chain, "show": cmd_show}[a.cmd](a)
    except KiteError as e:
        print("options: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
