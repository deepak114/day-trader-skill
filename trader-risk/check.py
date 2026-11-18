#!/usr/bin/env python3
"""Risk Manager for trader-risk (NSE equity, NFO futures and options). Standard library + kite.py.

Checks every trade in trader/orders.json against your limits BEFORE it can reach
a ticket, prints PASS or BLOCK with the reasons, and records the verdicts in
trader/state/risk.json so trader-broker only shows a trade that passed.

    python check.py                     # trader/orders.json + trader/config.json (+ Kite if signed in)
    python check.py --no-kite           # rules only, no live price / funds / positions
    python check.py --now "2026-11-18 09:52"

New entries (an exit that only closes what you hold is never blocked by a day limit):
  stop          a stop order / GTT stop, or a strategy stop value, is required
  max loss      loss at the stop (or a spread's worst case) <= max_loss_per_trade, re-measured from
                a fresh price when Kite is available
  reward:risk   >= min_reward_risk (single and debit trades), >= min_reward_risk_credit (credit spreads)
  daily limit   today's realised loss + the risk still open on today's trades + this trade
                <= daily_loss_limit
  trades/day    <= max_trades_per_day
  window        inside no_entries_before / no_entries_after (IST), market open, not an NSE holiday,
                and before flat_by for MIS
  permissions   allow.short_equity, allow.futures, allow.option_buying, allow.option_selling,
                allow.naked_short_options (default: all on except naked shorts)
  expiry day    no NEW short options on their expiry day (no_new_shorts_on_expiry_day, default true)
  F&O ban       no new F&O position in a stock in NSE's ban period (today's fo_secban.csv)
  blocked       symbols you never trade ("blocked": [...])
  size          whole lots, max_lots, quantity > 0, prices on the tick grid, CNC never short
  margin        required margin <= usable funds; option selling without a margin number is blocked
  duplicate     the same trade (fingerprint) already recorded as placed today
"""

import argparse
import datetime
import os
import re
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trader-kite"))
import kite  # noqa: E402
from kite import KiteError  # noqa: E402

DEFAULTS = {
    "max_loss_per_trade": 2000, "daily_loss_limit": 6000, "max_trades_per_day": 4,
    "min_reward_risk": 1.0, "min_reward_risk_credit": 0.3, "no_entries_before": "09:30",
    "no_entries_after": "14:30", "cash_buffer_pct": 10, "no_new_shorts_on_expiry_day": True,
    "max_lots": {"index": 2, "stock": 1}, "blocked": [], "respect_fo_ban": True,
}
ALLOW = {"short_equity": True, "futures": True, "option_buying": True, "option_selling": True,
         "naked_short_options": False}
BAN_URL = "https://nsearchives.nseindia.com/content/fo/fo_secban.csv"
PRODUCTS = {"equity": {"MIS", "CNC"}, "future": {"MIS", "NRML"}, "option": {"MIS", "NRML"}}
ORDER_TYPES = {"MARKET", "LIMIT", "SL", "SL-M"}
FRESH_MINUTES = 15


def c(cfg, k):
    v = cfg.get(k)
    return DEFAULTS.get(k) if v is None else v


def allowed(cfg, k):
    a = cfg.get("allow") or {}
    return bool(a.get(k, ALLOW[k]))


def ban_list(now, path_dir="trader/cache", fetch=True, text=None):
    """Symbols in NSE's F&O ban period today. Cached per day; None when it cannot be read."""
    if text is None:
        p = os.path.join(path_dir, "fo_secban-%s.csv" % now.date().isoformat())
        if os.path.exists(p):
            text = open(p, encoding="utf-8").read()
        elif fetch:
            try:
                req = urllib.request.Request(BAN_URL, headers={"User-Agent": "Mozilla/5.0 (day-trader-skill-india)"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    text = r.read().decode("utf-8", "replace")
                os.makedirs(path_dir, exist_ok=True)
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write(text)
            except OSError:
                return None
        else:
            return None
    out = set()
    for line in text.splitlines():
        m = re.match(r"^\s*\d+\s*,\s*([A-Z0-9&\-]+)\s*$", line.strip())
        if m:
            out.add(m.group(1))
    return out


def underlying_of(trade):
    if trade.get("underlying"):
        return trade["underlying"]
    sym = (trade.get("key") or "").split(":")[-1]
    m = re.match(r"^([A-Z&\-]+?)(\d{2}[A-Z0-9]{3}|\d{2}[1-9OND]\d{2})", sym)
    return m.group(1) if m and trade.get("segment") in ("future", "option") else sym


def is_index(name):
    return name in kite.INDEX_NAMES


def today_state(state, today):
    rows = [r for r in (state.get("log") or []) if str(r.get("at", "")).startswith(today)]
    placed = [r for r in rows if r.get("event") == "placed" and r.get("role", "entry") == "entry"]
    realised = sum(float(r.get("pnl") or 0) for r in rows if r.get("event") == "closed")
    open_risk = sum(float(r.get("max_loss") or 0) for r in placed
                    if not any(x.get("event") == "closed" and x.get("trade") == r.get("trade") for x in rows))
    return placed, realised, open_risk


def check_trade(t, cfg, now, state=None, bans=None, funds=None, fresh=None, kite_pnl=None):
    """-> (verdict, reasons, warnings)."""
    reasons, warns = [], []
    today = now.date().isoformat()
    seg, strat = t.get("segment"), t.get("strategy", "single")
    single = strat == "single"
    exit_only = t.get("role") == "exit" or t.get("exit_only")
    orders = [o for o in t.get("orders") or [] if o.get("kind") not in ("alert", "gtt")]
    lot = int(t.get("lot_size") or 1)
    und = underlying_of(t)

    # --- structure
    if not orders:
        reasons.append("no orders in the trade")
    for o in orders:
        miss = [k for k in ("exchange", "tradingsymbol", "transaction_type", "quantity", "product", "order_type")
                if not o.get(k)]
        if miss:
            reasons.append("order %s is missing %s" % (o.get("step"), ", ".join(miss)))
            continue
        if o["order_type"] not in ORDER_TYPES:
            reasons.append("order %s: unknown order type %s" % (o["step"], o["order_type"]))
        if o["transaction_type"] not in ("BUY", "SELL"):
            reasons.append("order %s: transaction_type must be BUY or SELL" % o["step"])
        q = int(o["quantity"])
        if q <= 0 or q % lot:
            reasons.append("order %s: quantity %d is not a whole number of lots (lot %d)" % (o["step"], q, lot))
        if seg in PRODUCTS and o["product"] not in PRODUCTS[seg]:
            reasons.append("order %s: product %s is not valid for %s" % (o["step"], o["product"], seg))
        if o["product"] == "CNC" and o["transaction_type"] == "SELL" and o.get("role") == "entry":
            reasons.append("CNC cannot open a short: an intraday short needs MIS")
        if o["order_type"] in ("SL-M",) and seg == "option":
            reasons.append("order %s: NSE does not take SL-M on options; use SL (stop-limit)" % o["step"])
    if blocked(t, cfg):
        reasons.append("%s is on your blocked list" % und)
    if exit_only:
        return ("PASS" if not reasons else "BLOCK"), reasons, ["an exit: day limits and the entry window "
                                                               "do not apply"]

    # --- permissions
    side = (t.get("side") or "").lower()
    if seg == "equity" and side == "sell" and not allowed(cfg, "short_equity"):
        reasons.append("intraday equity shorts are off (allow.short_equity)")
    if seg == "future" and not allowed(cfg, "futures"):
        reasons.append("futures are off (allow.futures)")
    if seg == "option":
        sells = [o for o in orders if o["transaction_type"] == "SELL"]
        buys = [o for o in orders if o["transaction_type"] == "BUY"]
        if buys and not allowed(cfg, "option_buying"):
            reasons.append("option buying is off (allow.option_buying)")
        if sells and not allowed(cfg, "option_selling"):
            reasons.append("option selling is off (allow.option_selling)")
        if sells and not t.get("defined_risk", True) and not allowed(cfg, "naked_short_options"):
            reasons.append("naked short options are off (allow.naked_short_options): use a defined-risk spread")
        if single and side == "sell" and not allowed(cfg, "naked_short_options"):
            reasons.append("a single short option is a naked short (allow.naked_short_options is off)")
        if sells and t.get("expiry") == today and c(cfg, "no_new_shorts_on_expiry_day"):
            reasons.append("no new short options on their expiry day (no_new_shorts_on_expiry_day): extra ELM "
                           "margin and gamma risk")
        elif t.get("expiry") == today:
            warns.append("expiry day: the premium can decay to nothing by 3:30 PM")

    # --- stop
    if single:
        has_stop = any(o.get("role") in ("stop",) for o in orders) or any(
            o.get("kind") == "gtt" for o in t.get("orders") or [])
        if not has_stop or not t.get("stop"):
            reasons.append("no stop: every entry needs a stop order or a GTT stop")
    elif t.get("stop_value") is None:
        reasons.append("no stop value for the strategy")

    # --- max loss
    ml = float(c(cfg, "max_loss_per_trade"))
    loss = float(t.get("max_loss") or 0)
    if single and fresh and t.get("stop"):
        ref = max(fresh, t["entry"]) if side == "buy" else min(fresh, t["entry"])
        loss2 = abs(ref - t["stop"]) * t["quantity"]
        if loss2 > loss + 0.005:
            warns.append("from the live price %.2f the loss at the stop is %s (plan said %s)" % (
                fresh, kite.inr(loss2), kite.inr(loss)))
            loss = loss2
    if not t.get("defined_risk", True) and loss <= 0:
        reasons.append("the loss is not capped")
    if loss > ml + 0.005:
        reasons.append("max loss %s is over your max loss per trade %s" % (kite.inr(loss), kite.inr(ml)))

    # --- reward:risk
    rr = t.get("reward_risk")
    credit = strat != "single" and (t.get("net_premium") or 0) < 0
    need = float(c(cfg, "min_reward_risk_credit" if credit else "min_reward_risk"))
    if rr is None:
        warns.append("no reward:risk in the trade")
    elif rr + 1e-9 < need:
        reasons.append("reward:risk %.2f is under your minimum %.2f%s" % (rr, need, " (credit)" if credit else ""))

    # --- size
    ml_cfg = c(cfg, "max_lots")
    if seg in ("future", "option"):
        cap = ml_cfg.get("index" if is_index(und) else "stock") if isinstance(ml_cfg, dict) else ml_cfg
        if cap and t.get("lots", 0) > int(cap):
            reasons.append("%d lots is over max_lots %s for %s" % (t["lots"], cap, "an index" if is_index(und) else
                                                                     "a stock"))
    tick = 0.05
    for o in orders:
        for f in ("price", "trigger_price"):
            v = float(o.get(f) or 0)
            if v and abs(round(v / tick) * tick - v) > 1e-6 and abs(round(v / 0.01) * 0.01 - v) > 1e-6:
                reasons.append("order %s: %s %.4f is off the tick grid" % (o["step"], f, v))

    # --- F&O ban
    if seg in ("future", "option") and c(cfg, "respect_fo_ban") and not is_index(und):
        if bans is None:
            warns.append("could not read NSE's F&O ban list today")
        elif und in bans:
            reasons.append("%s is in NSE's F&O ban period: no new positions (only exits)" % und)

    # --- window and market
    st = kite.market_status(now)
    if st["warning"]:
        warns.append(st["warning"])
    mins = now.hour * 60 + now.minute
    w0 = kite.hhmm(c(cfg, "no_entries_before"))
    w1 = kite.hhmm(c(cfg, "no_entries_after"))
    if not st["trading_day"]:
        reasons.append("the market is closed today (%s)" % st["why"])
    elif st["phase"] != "open":
        reasons.append("the market is not open (%s)" % st["why"])
    elif not w0 <= mins <= w1:
        reasons.append("outside your entry window %s to %s IST" % (kite.clock(w0), kite.clock(w1)))
    if t.get("product") == "MIS" and t.get("flat_by") and mins >= kite.hhmm(t["flat_by"]):
        reasons.append("past flat_by %s: an MIS entry now would be squared off" % t["flat_by"])
    if t.get("date") and t["date"] != today:
        warns.append("the trade was sized for %s, not today" % t["date"])

    # --- day limits
    placed, realised, open_risk = today_state(state or {}, today)
    if kite_pnl is not None:
        realised = min(realised, kite_pnl)
    n = len(placed)
    if any(r.get("fingerprint") == t.get("fingerprint") for r in placed):
        reasons.append("this exact trade is already recorded as placed today")
    if n >= int(c(cfg, "max_trades_per_day")):
        reasons.append("%d trades placed today, your limit is %s" % (n, c(cfg, "max_trades_per_day")))
    dl = float(c(cfg, "daily_loss_limit"))
    used = max(0.0, -realised) + open_risk
    if used + loss > dl + 0.005:
        reasons.append("daily loss limit: %s lost or at risk today + %s here is over %s" % (
            kite.inr(used), kite.inr(loss), kite.inr(dl)))

    # --- margin
    m = t.get("margin_required")
    sells_opt = seg == "option" and any(o["transaction_type"] == "SELL" for o in orders)
    if m is None:
        if sells_opt:
            reasons.append("option selling needs Kite's margin number: sign in to Kite and re-run /trader-budget")
        else:
            warns.append("margin not checked (no number from Kite)")
    elif funds is not None:
        usable = funds * (1 - float(c(cfg, "cash_buffer_pct")) / 100.0)
        if m > usable + 0.005:
            reasons.append("margin %s is over your usable funds %s" % (kite.inr(m), kite.inr(usable)))
    if t.get("margin_source") == "estimate":
        warns.append("margin is an estimate, not Kite's number")
    return ("PASS" if not reasons else "BLOCK"), reasons, warns


def blocked(t, cfg):
    b = {str(x).upper() for x in (c(cfg, "blocked") or [])}
    return underlying_of(t) in b or (t.get("key") or "").split(":")[-1] in b


def report(results):
    lines = []
    for t, v, rs, ws in results:
        size = ("%d lot%s" % (t.get("lots", 0), "s" if t.get("lots", 0) != 1 else "")) if int(
            t.get("lot_size") or 1) > 1 else "%d shares" % t.get("quantity", 0)
        lines.append("%s  %s %s %s, %s, max loss %s" % (
            v, t.get("id"), (t.get("side") or "").upper() if t.get("strategy") == "single" else t.get("strategy"),
            t.get("key"), size, kite.inr(t.get("max_loss"))))
        for r in rs:
            lines.append("   x " + r)
        for w in ws:
            lines.append("   ! " + w)
    return "\n".join(lines)


def main(argv=None):
    kite.safe_stdout()
    ap = argparse.ArgumentParser(description="Risk Manager: PASS or BLOCK every trade before a ticket")
    ap.add_argument("--orders", default="trader/orders.json")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--state", default="trader/state/broker.json")
    ap.add_argument("--out", default="trader/state/risk.json")
    ap.add_argument("--now")
    ap.add_argument("--no-kite", action="store_true")
    ap.add_argument("--ban-file", help="a saved fo_secban.csv (offline)")
    a = ap.parse_args(argv)
    try:
        now = kite.now_ist(a.now)
        cfg = kite.load_config(a.config)
        doc = kite.read_json(a.orders, "orders (run /trader-budget first)", required=True)
        state = kite.read_json(a.state, default={}) or {}
        bans = ban_list(now, text=open(a.ban_file, encoding="utf-8").read() if a.ban_file else None,
                        fetch=not a.no_kite)
        funds = fresh_map = kite_pnl = None
        client = None
        if not a.no_kite:
            try:
                kite.credentials(a.config)
                client = kite.Kite()
            except KiteError as e:
                print("note: %s; checking the rules without live data" % e)
        if client:
            try:
                funds = float(client.funds("equity").get("net") or 0)
            except KiteError:
                funds = None
            try:
                pos = client.positions().get("day") or []
                kite_pnl = sum(float(p.get("pnl") or 0) for p in pos)
            except KiteError:
                kite_pnl = None
            keys = [t["key"] for t in doc.get("trades", []) if t.get("strategy") == "single"]
            try:
                fresh_map = {k: v["last_price"] for k, v in client.ltp(keys).items()} if keys else {}
            except KiteError:
                fresh_map = None
        results = []
        for t in doc.get("trades", []):
            fresh = (fresh_map or {}).get(t.get("key"))
            v, rs, ws = check_trade(t, cfg, now, state, bans, funds, fresh, kite_pnl)
            results.append((t, v, rs, ws))
        print(report(results))
        rec = kite.read_json(a.out, default={}) or {}
        verdicts = [x for x in rec.get("verdicts", []) if x.get("date") == now.date().isoformat()]
        for t, v, rs, ws in results:
            verdicts.append({"date": now.date().isoformat(), "at": kite.iso_ist(now), "trade": t.get("id"),
                             "fingerprint": t.get("fingerprint"), "verdict": v, "reasons": rs, "warnings": ws})
        kite.write_json(a.out, {"verdicts": verdicts})
        ok = all(v == "PASS" for _, v, _, _ in results)
        print("recorded in %s -> %s" % (a.out, "next: /trader-broker shows the ticket" if ok else
                                         "fix the BLOCK reasons, re-run /trader-budget, then check again"))
        return 0 if ok else 3
    except KiteError as e:
        print("risk: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
