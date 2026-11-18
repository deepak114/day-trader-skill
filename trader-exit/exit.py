#!/usr/bin/env python3
"""Exit Planner for trader-exit (NSE equity, NFO futures, a single option bought or sold).
Standard library + ../trader-kite/kite.py.

Sets the stop and the target BEFORE the entry and writes trader/plan.json:

    python exit.py plan                                  # from trader/entry.json: stop or-mid, target 2R
    python exit.py plan --stop or-far --target 1R --write
    python exit.py plan --stop pct:0.8 --target pct:1.6
    python exit.py plan --stop atr:1.5 --target 2R       # 1.5 x ATR(14) of the 5-minute bars
    python exit.py plan --key NSE:SBIN --side buy --entry 842.5 --stop price:836 --target 2R --write
    python exit.py plan --key "NIFTY 26000 CE" --side buy --entry 118 --stop pct:30 --target 2R --product MIS
    python exit.py show                                  # the saved plan in plain words

Stop rules (long; a short mirrors them):
  or-mid       the opening range's midpoint (default)
  or-far       the far side of the range: its low for a long, its high for a short
  price:X      your price             pct:X   X % from the entry          atr:X   X x ATR(14)
Target rules: NR (1R, 1.5R, 2R ...), price:X, pct:X.
Prices are rounded to the instrument's tick (0.05 for most NSE/NFO contracts), the
stop AWAY from the entry and the target TOWARD it, so neither is better than asked.
Refused: a stop on the wrong side, a target on the wrong side, a stop closer than
two ticks or 0.05 % (inside the spread), reward:risk under config min_reward_risk.

flat_by (the time you are out regardless) defaults by segment, earlier than
Zerodha's MIS auto square-off (about 3:10 PM for F&O stocks in the closing
auction, 3:25 PM otherwise): equity 15:00, futures and options 15:15. Override
with --flat-by or "flat_by": {"equity": "15:00", "future": "15:15", "option": "15:15"}.
It never places an order.
"""

import argparse
import datetime
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trader-kite"))
import kite  # noqa: E402
from kite import KiteError  # noqa: E402

FLAT_BY = {"equity": "15:00", "future": "15:15", "option": "15:15"}
DEFAULTS = {"min_reward_risk": 1.0}
MIN_RISK_PCT = 0.05
ATR_BARS = 14
NOTE = "A stop is a plan, not a guarantee: a gap, a circuit or a fast market can fill past it."


def parse_rule(text, kind):
    s = str(text).strip().lower()
    if kind == "stop" and s in ("or-mid", "or_mid", "mid"):
        return ("or-mid", None)
    if kind == "stop" and s in ("or-far", "or_far", "or-low", "or-high", "far"):
        return ("or-far", None)
    m = re.match(r"^(\d+(\.\d+)?)r$", s)
    if kind == "target" and m:
        return ("r", float(m.group(1)))
    m = re.match(r"^(price|pct|atr):(\d+(\.\d+)?)$", s)
    if m and not (kind == "target" and m.group(1) == "atr"):
        return (m.group(1), float(m.group(2)))
    raise KiteError("cannot read the %s rule %r (stop: or-mid, or-far, price:X, pct:X, atr:X; "
                    "target: 2R, price:X, pct:X)" % (kind, text))


def atr(sessions, n=ATR_BARS):
    """ATR(n) over the last n true ranges, each measured inside its own session so the
    overnight gap between two days never counts as one bar's range."""
    trs = []
    for bars in sessions:
        for prev, b in zip(bars[:-1], bars[1:]):
            trs.append(max(b["h"] - b["l"], abs(b["h"] - prev["c"]), abs(b["l"] - prev["c"])))
    if len(trs) < n:
        return None
    return sum(trs[-n:]) / n


def compute(entry, side, stop_rule, target_rule, tick=0.05, or_high=None, or_low=None, atr_value=None,
            min_rr=1.0):
    """-> (stop, target, risk, reward, rr). Raises KiteError with the reason when the plan is unsound."""
    long = side == "buy"
    sk, sp = stop_rule
    if sk == "or-mid":
        if or_high is None:
            raise KiteError("or-mid needs the opening range: run /trader-entry or pass --or-high --or-low")
        raw_stop = (or_high + or_low) / 2
    elif sk == "or-far":
        if or_high is None:
            raise KiteError("or-far needs the opening range: run /trader-entry or pass --or-high --or-low")
        raw_stop = or_low if long else or_high
    elif sk == "price":
        raw_stop = sp
    elif sk == "pct":
        raw_stop = entry * (1 - sp / 100) if long else entry * (1 + sp / 100)
    else:
        if not atr_value:
            raise KiteError("not enough bars for ATR(%d): use another stop rule" % ATR_BARS)
        raw_stop = entry - sp * atr_value if long else entry + sp * atr_value
    stop = kite.round_tick(raw_stop, tick, "down" if long else "up")
    if long and stop >= entry:
        raise KiteError("the stop %.2f is not below the entry %.2f: a long needs its stop underneath" % (stop, entry))
    if not long and stop <= entry:
        raise KiteError("the stop %.2f is not above the entry %.2f: a short needs its stop above" % (stop, entry))
    if stop <= 0:
        raise KiteError("the stop works out at %.2f, at or below zero" % stop)
    risk = round(abs(entry - stop), 2)
    if risk < max(2 * tick, entry * MIN_RISK_PCT / 100) - 1e-9:
        raise KiteError("a stop %.2f away is inside the spread's noise (under two ticks or %.2f%% of the price): "
                        "widen it" % (risk, MIN_RISK_PCT))
    tk, tp = target_rule
    if tk == "r":
        raw_t = entry + tp * risk if long else entry - tp * risk
    elif tk == "price":
        raw_t = tp
    else:
        raw_t = entry * (1 + tp / 100) if long else entry * (1 - tp / 100)
    target = kite.round_tick(raw_t, tick, "down" if long else "up")
    if long and target <= entry:
        raise KiteError("the target %.2f is not above the entry %.2f" % (target, entry))
    if not long and target >= entry:
        raise KiteError("the target %.2f is not below the entry %.2f" % (target, entry))
    reward = round(abs(target - entry), 2)
    rr = round(reward / risk, 2)
    if rr + 1e-9 < min_rr:
        raise KiteError("reward:risk is %.2f, under your minimum %.2f (min_reward_risk): move the target or "
                        "tighten the stop" % (rr, min_rr))
    return stop, target, risk, reward, rr


def resolve_flat_by(segment, explicit, cfg, entry_time=None):
    if explicit:
        t = explicit
    else:
        fb = cfg.get("flat_by")
        if isinstance(fb, dict):
            t = fb.get(segment) or FLAT_BY.get(segment, "15:00")
        elif isinstance(fb, str):
            t = fb
        else:
            t = FLAT_BY.get(segment, "15:00")
    m = kite.hhmm(t, "flat_by")
    if not kite.OPEN_MIN < m <= kite.CLOSE_MIN:
        raise KiteError("flat_by must be between 09:15 and 15:30 IST")
    if entry_time and kite.hhmm(entry_time) >= m:
        raise KiteError("the entry at %s is not before flat_by %s" % (entry_time, kite.clock(m)))
    return kite.clock(m)


def build_plan(a, now, cfg, inst=None):
    src = None
    if a.key:
        if a.entry is None:
            raise KiteError("with --key also give --entry")
        e = {"key": a.key, "side": a.side or "buy", "entry": a.entry, "entry_time": a.entry_time,
             "or_high": a.or_high, "or_low": a.or_low, "or_minutes": a.or_minutes, "date": a.date,
             "setup": "manual"}
        src = "flags"
    else:
        e = kite.read_json(a.entry_file, "entry (run /trader-entry first, or pass --key --entry)", required=True)
        if e.get("status") not in (None, "triggered"):
            raise KiteError("%s says %s, not triggered" % (a.entry_file, e.get("status")))
        src = a.entry_file
        for k in ("or_high", "or_low"):
            if getattr(a, k) is not None:
                e[k] = getattr(a, k)
        if a.side:
            e["side"] = a.side
    ins = None
    if inst is not None:
        ins = inst.resolve(e["key"])
    elif e.get("tradingsymbol"):
        ins = kite.Instrument(exchange=e["exchange"], tradingsymbol=e["tradingsymbol"], lot_size=e.get("lot_size", 1),
                              tick_size=e.get("tick_size", 0.05), instrument_type=None, segment=None,
                              expiry=e.get("expiry"), instrument_token=e.get("instrument_token"))
    key = ins.key if ins else kite.normalize_key(e["key"])
    segment = (kite.segment_of(ins) if ins and ins.get("segment") else e.get("segment")) or "equity"
    if segment == "index":
        raise KiteError("%s is an index: trade its future or an option (e.g. 'NIFTY FUT')" % key)
    tick = float((ins or {}).get("tick_size") or e.get("tick_size") or 0.05)
    lot = int((ins or {}).get("lot_size") or e.get("lot_size") or 1)
    side = e.get("side") or "buy"
    entry = float(e["entry"])
    atr_value = None
    stop_rule = parse_rule(a.stop, "stop")
    if stop_rule[0] == "atr":
        day = datetime.date.fromisoformat(e.get("date") or now.date().isoformat())
        frm = kite.prev_trading_day(day)
        candles = kite.get_candles(key, "5minute", frm, now, offline=a.offline, instruments=inst)
        atr_value = atr([kite.session_bars(candles, d, 5, now) for d in (frm.isoformat(), day.isoformat())])
    min_rr = float(a.min_rr if a.min_rr is not None else cfg.get("min_reward_risk", DEFAULTS["min_reward_risk"]))
    stop, target, risk, reward, rr = compute(entry, side, stop_rule, parse_rule(a.target, "target"), tick,
                                             e.get("or_high"), e.get("or_low"), atr_value, min_rr)
    product = (a.product or cfg.get("product", {}).get(segment) if isinstance(cfg.get("product"), dict)
               else a.product or cfg.get("product")) or "MIS"
    product = product.upper()
    if product == "CNC" and side == "sell":
        raise KiteError("CNC cannot short: use MIS for an intraday short")
    if product == "CNC" and segment != "equity":
        raise KiteError("CNC is for equity delivery; F&O uses MIS (intraday) or NRML (carry forward)")
    flat_by = resolve_flat_by(segment, a.flat_by, cfg, e.get("entry_time")) if product == "MIS" else None
    plan = {
        "created_by": "trader-exit", "created_at": kite.iso_ist(now), "from": src,
        "strategy": "single", "key": key, "exchange": key.split(":")[0], "tradingsymbol": key.split(":")[1],
        "segment": segment, "expiry": (ins or {}).get("expiry") or e.get("expiry"),
        "lot_size": lot, "tick_size": tick, "product": product, "side": side,
        "date": e.get("date") or now.date().isoformat(), "entry": entry, "entry_time": e.get("entry_time"),
        "stop": stop, "target": target, "risk_per_unit": risk, "reward_per_unit": reward, "reward_risk": rr,
        "risk_per_lot": round(risk * lot, 2), "stop_rule": a.stop, "target_rule": a.target,
        "setup": e.get("setup") or "orb", "flat_by": flat_by, "interval": e.get("interval", "5m"),
        "opening_range": {"minutes": e.get("or_minutes", 15), "high": e.get("or_high"), "low": e.get("or_low")},
        "atr": round(atr_value, 2) if atr_value else None, "note": NOTE,
    }
    if segment == "option" and side == "sell":
        plan["warning"] = ("a short option's loss is not capped by the stop: a gap can run far past it. "
                           "Prefer a spread (/trader-options) unless your rules allow naked shorts.")
    return plan


def describe(p):
    long = p["side"] == "buy"
    unit = "share" if p["segment"] == "equity" else "unit"
    lines = [
        "PLAN  %s %s %s (%s%s)" % ("BUY" if long else "SELL", p["key"], p["product"], p["segment"],
                                   ", lot %d" % p["lot_size"] if p["lot_size"] > 1 else ""),
        "  entry   %.2f%s" % (p["entry"], " at %s IST" % p["entry_time"] if p.get("entry_time") else ""),
        "  stop    %.2f  (%s; %.2f a %s at risk%s)" % (p["stop"], p["stop_rule"], p["risk_per_unit"], unit,
                                                      ", %s a lot" % kite.inr(p["risk_per_lot"]) if p["lot_size"] > 1
                                                      else ""),
        "  target  %.2f  (%s; reward:risk %.2f)" % (p["target"], p["target_rule"], p["reward_risk"]),
    ]
    if p.get("flat_by"):
        lines.append("  flat by %s IST either way (before Zerodha's MIS auto square-off)" % p["flat_by"])
    if p.get("warning"):
        lines.append("  WARNING: " + p["warning"])
    lines.append("  " + p["note"])
    return "\n".join(lines)


def cmd_plan(a):
    now = kite.now_ist(a.now)
    cfg = kite.load_config(a.config)
    inst = None if a.no_instruments else kite.Instruments(("NSE", "NFO"))
    p = build_plan(a, now, cfg, inst)
    print(describe(p))
    if a.write:
        kite.write_json(a.out, p)
        print("wrote %s -> next: /trader-timemachine, then /trader-budget" % a.out)
    return 0


def cmd_show(a):
    p = kite.read_json(a.out, "plan", required=True)
    if p.get("strategy") != "single":
        print("this plan is an options strategy: see /trader-options show")
        return 0
    print(describe(p))
    return 0


def main(argv=None):
    kite.safe_stdout()
    ap = argparse.ArgumentParser(description="Exit Planner: stop, target and flat-by before the entry")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("plan")
    s.add_argument("--entry-file", default="trader/entry.json")
    s.add_argument("--key")
    s.add_argument("--side", choices=("buy", "sell"))
    s.add_argument("--entry", type=float)
    s.add_argument("--entry-time")
    s.add_argument("--date")
    s.add_argument("--or-high", type=float)
    s.add_argument("--or-low", type=float)
    s.add_argument("--or-minutes", type=int, default=15)
    s.add_argument("--stop", default="or-mid")
    s.add_argument("--target", default="2R")
    s.add_argument("--min-rr", type=float)
    s.add_argument("--product", choices=("MIS", "NRML", "CNC", "mis", "nrml", "cnc"))
    s.add_argument("--flat-by")
    s.add_argument("--now")
    s.add_argument("--offline")
    s.add_argument("--no-instruments", action="store_true", help=argparse.SUPPRESS)
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
        return cmd_plan(a) if a.cmd == "plan" else cmd_show(a)
    except KiteError as e:
        print("exit: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
