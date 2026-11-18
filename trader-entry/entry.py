#!/usr/bin/env python3
"""Entry Timer for trader-entry (NSE / NFO via Kite Connect). Standard library + ../trader-kite/kite.py.

Watches one instrument bar by bar and says the moment to get in:

    python entry.py check NSE:RELIANCE                       # ORB long, 15-minute range, 5-minute bars
    python entry.py check "NIFTY FUT" --side sell            # ORB short on the near-month Nifty future
    python entry.py check NSE:SBIN --setup level --level 842.5
    python entry.py check NFO:NIFTY26NOV26000CE --or-minutes 30 --interval 3m
    python entry.py watch NSE:RELIANCE --every 30            # re-check every 30 s until triggered/expired
    python entry.py watchlist                                # every item in trader/watchlist.json
    ... --write                                              # save trader/entry.json for /trader-exit
    ... --now "2026-11-18 09:52" --offline FILE              # pin the clock, read saved candles

Rules (long; a short mirrors them):
  * opening range = the high and low of the first --or-minutes after 9:15 IST
  * TRIGGERED on the first COMPLETED bar after the range that CLOSES above the range
    high (or above --level), inside the entry window (config no_entries_before /
    no_entries_after, default 09:30 to 14:30 IST). Entry price = that bar's close,
    entry time = the bar's end.
  * --min-volume-x 1.5: the trigger bar's volume must also be at least 1.5x the
    average of the session's earlier bars (skipped for indices, which have no volume)
  * EXPIRED: no trigger by no_entries_after. WAITING: shows how far is left.
It never places an order.
"""

import argparse
import datetime
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trader-kite"))
import kite  # noqa: E402
from kite import KiteError  # noqa: E402

INTERVALS = {"1m": 1, "3m": 3, "5m": 5, "10m": 10, "15m": 15}
DEFAULTS = {"no_entries_before": "09:30", "no_entries_after": "14:30"}
GRACE = 3   # seconds after a bar ends before watch asks for it


def window(cfg):
    a = kite.hhmm(cfg.get("no_entries_before", DEFAULTS["no_entries_before"]), "no_entries_before")
    b = kite.hhmm(cfg.get("no_entries_after", DEFAULTS["no_entries_after"]), "no_entries_after")
    if not kite.OPEN_MIN <= a < b <= kite.CLOSE_MIN:
        raise KiteError("the entry window must sit inside 09:15 to 15:30 with before < after")
    return a, b


def evaluate(candles, now, key, setup="orb", side="buy", or_minutes=15, interval="5m", level=None,
             min_volume_x=None, cfg=None, is_index=False):
    """Candles (kite rows) + IST now -> result dict."""
    cfg = cfg or {}
    step = INTERVALS[interval]
    if or_minutes % step or not step <= or_minutes <= 120:
        raise KiteError("--or-minutes must be a multiple of the %s bar (and at most 120)" % interval)
    if setup == "level" and level is None:
        raise KiteError("--setup level needs --level PRICE")
    long = side == "buy"
    now = kite.to_ist(now)
    day = now.date()
    st = kite.market_status(now)
    w0, w1 = window(cfg)
    res = {"key": key, "setup": setup, "side": side, "interval": interval, "or_minutes": or_minutes,
           "date": day.isoformat(), "now": now.strftime("%H:%M"), "level": level,
           "window": [kite.clock(w0), kite.clock(w1)], "calendar_warning": st["warning"]}
    if not st["trading_day"]:
        return dict(res, status="closed", why=st["why"], next_open=st["next_open"])
    mins = now.hour * 60 + now.minute
    if mins < kite.OPEN_MIN:
        return dict(res, status="pre-open", why="the session opens at 9:15 AM IST")
    bars = kite.session_bars(candles, day, step, now)
    if not bars:
        return dict(res, status="no-data", why="no completed %s bars yet today" % interval)
    n = or_minutes // step
    rng = [b for b in bars if b["slot"] < n]
    if len(rng) < n:
        if mins < kite.OPEN_MIN + or_minutes:
            return dict(res, status="forming", why="the %d-minute opening range ends at %s" % (
                or_minutes, kite.clock(kite.OPEN_MIN + or_minutes)), bars=len(bars))
        return dict(res, status="no-data", why="the opening range has gaps (%d of %d bars)" % (len(rng), n))
    oh, ol = max(b["h"] for b in rng), min(b["l"] for b in rng)
    res.update(or_high=round(oh, 2), or_low=round(ol, 2))
    trig = level if setup == "level" else (oh if long else ol)
    res["trigger_level"] = round(trig, 2)
    after = [b for b in bars if b["slot"] >= n]
    vols = []
    for b in bars:
        if b["slot"] < n:
            vols.append(b["v"])
    for b in after:
        if b["end_min"] > w1:
            break
        crossed = b["c"] > trig if long else b["c"] < trig
        avg = (sum(vols) / len(vols)) if vols else 0
        vol_ok = True
        if min_volume_x and not is_index and avg > 0:
            vol_ok = b["v"] >= min_volume_x * avg
        if crossed and b["end_min"] >= w0 and vol_ok:
            res.update(status="triggered", entry=round(b["c"], 2), entry_time=kite.clock(b["end_min"]),
                       trigger_bar={"start": b["start"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"],
                                    "v": b["v"]},
                       volume_x=round(b["v"] / avg, 2) if avg else None)
            return res
        if crossed and b["end_min"] < w0:
            res.setdefault("early_crosses", []).append(b["start"])
        if crossed and not vol_ok:
            res.setdefault("low_volume_crosses", []).append("%s (%.1fx)" % (b["start"], b["v"] / avg))
        vols.append(b["v"])
    last = bars[-1]
    res["last"] = {"time": kite.clock(last["end_min"]), "close": last["c"]}
    if mins >= w1 or last["end_min"] >= w1:
        return dict(res, status="expired", why="no close %s %.2f by %s" % ("above" if long else "below",
                                                                              trig, kite.clock(w1)))
    gap = (trig - last["c"]) if long else (last["c"] - trig)
    res.update(status="waiting", distance=round(gap, 2), distance_pct=round(100 * gap / last["c"], 2))
    return res


def describe(r):
    k, side = r["key"], "LONG" if r["side"] == "buy" else "SHORT"
    head = "%s %s %s" % (k, side, "ORB %dm" % r["or_minutes"] if r["setup"] == "orb" else "level %.2f" % r["level"])
    s = r["status"]
    lines = []
    if s == "triggered":
        lines.append("TRIGGERED  %s: the %s bar closed %.2f %s %.2f; entry %.2f at %s IST" % (
            head, r["trigger_bar"]["start"], r["entry"], "above" if r["side"] == "buy" else "below",
            r["trigger_level"], r["entry"], r["entry_time"]) +
            (" (volume %.1fx)" % r["volume_x"] if r.get("volume_x") else ""))
    elif s == "waiting":
        lines.append("WAITING    %s: last close %.2f at %s, needs a close %s %.2f (%.2f away, %.2f%%)" % (
            head, r["last"]["close"], r["last"]["time"], "above" if r["side"] == "buy" else "below",
            r["trigger_level"], r["distance"], r["distance_pct"]))
    elif s == "expired":
        lines.append("EXPIRED    %s: %s" % (head, r["why"]))
    else:
        lines.append("%-10s %s: %s" % (s.upper(), head, r.get("why", "")))
    if r.get("or_high") is not None:
        lines.append("           opening range %.2f - %.2f (first %d min)" % (r["or_low"], r["or_high"],
                                                                            r["or_minutes"]))
    if r.get("early_crosses"):
        lines.append("           closes beyond the trigger before %s were ignored: %s" % (
            r["window"][0], ", ".join(r["early_crosses"])))
    if r.get("low_volume_crosses"):
        lines.append("           crosses on low volume ignored: %s" % ", ".join(r["low_volume_crosses"]))
    if r.get("calendar_warning"):
        lines.append("           note: " + r["calendar_warning"])
    return "\n".join(lines)


def entry_doc(r, now, ins=None):
    doc = {"created_by": "trader-entry", "created_at": kite.iso_ist(now)}
    doc.update({k: v for k, v in r.items() if k not in ("calendar_warning",)})
    if ins:
        doc.update(exchange=ins["exchange"], tradingsymbol=ins["tradingsymbol"], segment=kite.segment_of(ins),
                   lot_size=ins["lot_size"], tick_size=ins["tick_size"], expiry=ins["expiry"],
                   instrument_token=ins["instrument_token"])
    return doc


def _load(a, key, now, inst):
    step = INTERVALS[a.interval]
    frm = datetime.datetime.combine(now.date(), datetime.time(9, 0), kite.IST)
    return kite.get_candles(key, "%dminute" % step if step > 1 else "minute", frm, now, offline=a.offline,
                            instruments=inst)


def _instruments(key, a):
    if a.offline and a.no_instruments:
        return None
    return kite.Instruments(kite.exchanges_for(key) if ":" in key else ("NSE", "NFO"))


def _check_one(a, spec, now, cfg, inst, level=None, setup=None, side=None):
    ins = inst.resolve(spec) if inst else None
    key = ins.key if ins else kite.normalize_key(spec)
    candles = _load(a, key, now, inst)
    return evaluate(candles, now, key, setup or a.setup, side or a.side, a.or_minutes, a.interval,
                    level if level is not None else a.level, a.min_volume_x, cfg,
                    is_index=bool(ins and kite.segment_of(ins) == "index")), ins


def cmd_check(a):
    now = kite.now_ist(a.now)
    cfg = kite.load_config(a.config)
    inst = _instruments(a.symbol, a)
    r, ins = _check_one(a, a.symbol, now, cfg, inst)
    print(describe(r))
    if a.write and r["status"] == "triggered":
        kite.write_json(a.out, entry_doc(r, now, ins))
        print("wrote %s -> next: /trader-exit" % a.out)
    return 0


def cmd_watch(a):
    cfg = kite.load_config(a.config)
    inst = _instruments(a.symbol, a)
    step = INTERVALS[a.interval]
    errors = 0
    while True:
        now = kite.now_ist(a.now)
        try:
            r, ins = _check_one(a, a.symbol, now, cfg, inst)
            errors = 0
        except KiteError as e:
            errors += 1
            print("%s  error: %s" % (now.strftime("%H:%M:%S"), e))
            if errors >= 5:
                return 2
            time.sleep(a.every)
            continue
        print("%s  %s" % (now.strftime("%H:%M:%S"), describe(r).splitlines()[0]))
        if r["status"] in ("triggered", "expired", "closed") or a.now:
            if r["status"] == "triggered" and a.write:
                kite.write_json(a.out, entry_doc(r, now, ins))
                print("wrote %s -> next: /trader-exit" % a.out)
            return 0
        mins = now.hour * 60 + now.minute
        nxt = (mins // step + 1) * step
        wait = max(a.every, (nxt - mins) * 60 - now.second + GRACE) if a.align else a.every
        time.sleep(wait)


def cmd_watchlist(a):
    now = kite.now_ist(a.now)
    cfg = kite.load_config(a.config)
    wl = kite.read_json(a.watchlist, "watchlist", required=True)
    items = wl.get("items") or []
    if not items:
        print("the watchlist is empty: run /trader-scanner or /trader-moneycontrol first")
        return 0
    inst = kite.Instruments(("NSE", "NFO"))
    hits = []
    for it in items[: a.max]:
        try:
            r, ins = _check_one(a, it["key"], now, cfg, inst, level=it.get("level"),
                                setup=it.get("entry_setup") or ("level" if it.get("level") else a.setup),
                                side=it.get("side") or a.side)
        except KiteError as e:
            print("%-24s error: %s" % (it.get("key"), e))
            continue
        print(describe(r).splitlines()[0])
        if r["status"] == "triggered":
            hits.append((r, ins))
    if a.write and len(hits) == 1:
        kite.write_json(a.out, entry_doc(hits[0][0], now, hits[0][1]))
        print("wrote %s -> next: /trader-exit" % a.out)
    elif a.write and hits:
        print("%d triggered: re-run `check` on the one you want with --write" % len(hits))
    return 0


def main(argv=None):
    kite.safe_stdout()
    ap = argparse.ArgumentParser(description="Entry Timer: opening-range and level breaks on Kite candles")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("check", "watch", "watchlist"):
        s = sub.add_parser(name)
        if name != "watchlist":
            s.add_argument("symbol", help="NSE:RELIANCE, 'NIFTY FUT', 'NIFTY 26000 CE', NFO:...")
        s.add_argument("--setup", choices=("orb", "level"), default="orb")
        s.add_argument("--side", choices=("buy", "sell"), default="buy")
        s.add_argument("--level", type=float)
        s.add_argument("--or-minutes", type=int, default=15)
        s.add_argument("--interval", choices=sorted(INTERVALS), default="5m")
        s.add_argument("--min-volume-x", type=float)
        s.add_argument("--now")
        s.add_argument("--offline")
        s.add_argument("--no-instruments", action="store_true", help=argparse.SUPPRESS)
        s.add_argument("--config", default="trader/config.json")
        s.add_argument("--out", default="trader/entry.json")
        s.add_argument("--write", action="store_true")
        if name == "watch":
            s.add_argument("--every", type=int, default=30)
            s.add_argument("--align", action="store_true", default=True)
        if name == "watchlist":
            s.add_argument("--watchlist", default="trader/watchlist.json")
            s.add_argument("--max", type=int, default=25)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"check": cmd_check, "watch": cmd_watch, "watchlist": cmd_watchlist}[a.cmd](a)
    except KiteError as e:
        print("entry: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
