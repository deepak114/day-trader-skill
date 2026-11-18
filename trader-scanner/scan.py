#!/usr/bin/env python3
"""Chart Scanner for trader-scanner (NSE cash + NFO). Standard library + ../trader-kite/kite.py.

    python scan.py universe                       # F&O stocks (default), or --set all / nifty50 / FILE
    python scan.py daily                          # before 9:15: daily setups -> trader/watchlist.json
    python scan.py orb                            # after the opening range: breakouts and breakdowns so far
    python scan.py orb --at 10:15 --or-minutes 15
    python scan.py buildup                        # F&O: price vs open interest on the near-month futures
    ... --symbols RELIANCE,SBIN,INFY | --limit 50 | --now "2026-11-18 10:15" | --offline DIR

universe  default "fno": every stock with live NFO futures (about 220, the liquid end of NSE)
          plus nothing else. "all": every NSE EQ-series stock in Kite's instrument list (2,000+;
          slow: Kite allows 3 candle requests a second). "nifty50": the 50 largest F&O names by
          lot value is NOT computed here; pass --symbols or a FILE with one NSE symbol per line.

daily     Kite day candles, last ~100 sessions, today's candle dropped until 15:30. Setups:
            breakout_20d     the last close is above every close of the 20 before it      (long)
            breakdown_20d    the last close is below every close of the 20 before it      (short)
            bull_flag        up > 10 % over the 10 sessions before the last 5, then 5 closes in a 5 % range
            bear_flag        down > 10 % the same way, then a tight 5-day range            (short)
            reclaim_ma20     closed under the 20-day average, then above it
            lose_ma20        closed above the 20-day average, then below it                (short)
            oversold         RSI 14 (Wilder) under 30;  overbought: over 70
            oversold_bounce  RSI 14 under 30 yesterday and today closed up
          Floors: --min-price 50, --min-turnover 10 crore (close x volume, 20-day average).

orb       5-minute Kite candles for today. The range = the first --or-minutes (15) after 9:15.
          A hit = the first completed bar after the range to CLOSE above the range high (long)
          or below the range low (short), up to --at. Every bar from 9:15 must be present.

buildup   One Kite /quote call for every near-month future (500 per call) and one day-candle
          call with OI per future for yesterday's open interest:
            long buildup      price up,   OI up        short buildup   price down, OI up
            short covering    price up,   OI down      long unwinding  price down, OI down
          Floors: |price change| >= --min-move 1 %, |OI change| >= --min-oi 5 %.

A scan is a list of charts that match a rule, not a list of stocks to buy or sell.
Exit code 0 when the scan found something, 1 when it did not.
"""

import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trader-kite"))
import kite  # noqa: E402
from kite import KiteError  # noqa: E402

NOTE = "A scan is a list of charts that match a rule, not a list of stocks to buy or sell."
LONG_SETUPS = {"breakout_20d", "bull_flag", "reclaim_ma20", "oversold", "oversold_bounce"}
SHORT_SETUPS = {"breakdown_20d", "bear_flag", "lose_ma20", "overbought"}
LABEL = {"breakout_20d": "20-day closing-high breakout", "breakdown_20d": "20-day closing-low breakdown",
         "bull_flag": "bull flag", "bear_flag": "bear flag", "reclaim_ma20": "reclaimed the 20-day average",
         "lose_ma20": "lost the 20-day average", "oversold": "oversold (RSI < 30)",
         "overbought": "overbought (RSI > 70)", "oversold_bounce": "oversold bounce"}
CRORE = 1e7


def rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    g = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    lo = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(lo[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + lo[i]) / n
    return 100 - 100 / (1 + ag / al) if al else 100.0


def find_setups(c):
    """closes oldest first (>= 30) -> (tags, numbers)."""
    last = c[-1]
    hi20, lo20 = max(c[-21:-1]), min(c[-21:-1])
    ma20, ma20_prev = sum(c[-20:]) / 20.0, sum(c[-21:-1]) / 20.0
    run = c[-6] / c[-16] - 1
    rng5 = (max(c[-5:]) - min(c[-5:])) / last
    r, r_prev = rsi(c), rsi(c[:-1])
    tags = []
    if last > hi20:
        tags.append("breakout_20d")
    if last < lo20:
        tags.append("breakdown_20d")
    if run > 0.10 and rng5 < 0.05:
        tags.append("bull_flag")
    if run < -0.10 and rng5 < 0.05:
        tags.append("bear_flag")
    if c[-2] < ma20_prev and last > ma20:
        tags.append("reclaim_ma20")
    if c[-2] > ma20_prev and last < ma20:
        tags.append("lose_ma20")
    if r is not None and r < 30:
        tags.append("oversold")
    if r is not None and r > 70:
        tags.append("overbought")
    if r_prev is not None and r_prev < 30 and last > c[-2]:
        tags.append("oversold_bounce")
    return tags, {"rsi": r, "high20": hi20, "low20": lo20, "ma20": ma20, "run_pct": run * 100,
                  "range5_pct": rng5 * 100}


def daily_rows(candles, now):
    """[(date, close, volume)] of complete day candles (today's dropped until 15:30)."""
    now = kite.to_ist(now)
    done = now.hour * 60 + now.minute >= kite.CLOSE_MIN or not kite.is_trading_day(now.date())
    out = {}
    for b in candles:
        d = kite.to_ist(b["t"]).date()
        if d > now.date() or (d == now.date() and not done):
            continue
        out[d] = (b["c"], b["v"])
    return [(d, c, v) for d, (c, v) in sorted(out.items())]


def scan_daily(data, now, min_price=50.0, min_turnover_cr=10.0):
    hits, skipped = [], {"short_history": 0, "price": 0, "turnover": 0}
    for sym, candles in data.items():
        rows = daily_rows(candles, now)
        if len(rows) < 30:
            skipped["short_history"] += 1
            continue
        closes = [c for _, c, _ in rows]
        turnover = sum(c * v for _, c, v in rows[-20:]) / 20.0
        if closes[-1] < min_price:
            skipped["price"] += 1
            continue
        if turnover < min_turnover_cr * CRORE:
            skipped["turnover"] += 1
            continue
        tags, nums = find_setups(closes)
        if tags:
            hits.append({"symbol": sym, "close": round(closes[-1], 2), "date": rows[-1][0].isoformat(),
                         "setups": tags, "side": "sell" if set(tags) <= SHORT_SETUPS else "buy",
                         "rsi": round(nums["rsi"], 1) if nums["rsi"] is not None else None,
                         "high20": round(nums["high20"], 2), "low20": round(nums["low20"], 2),
                         "ma20": round(nums["ma20"], 2), "turnover_cr": round(turnover / CRORE, 1)})
    hits.sort(key=lambda h: -h["turnover_cr"])
    return hits, skipped


def orb_hit(bars, n, cutoff_min):
    """bars: today's completed 5-minute session bars. -> dict or None."""
    if len(bars) < n or any(b["slot"] != i for i, b in enumerate(bars)):
        return {"usable": False}
    oh, ol = max(b["h"] for b in bars[:n]), min(b["l"] for b in bars[:n])
    for b in bars[n:]:
        if b["end_min"] > cutoff_min:
            break
        if b["c"] > oh:
            return {"usable": True, "side": "buy", "or_high": oh, "or_low": ol, "close": b["c"],
                    "time": kite.clock(b["end_min"]), "beyond_pct": 100 * (b["c"] / oh - 1)}
        if b["c"] < ol:
            return {"usable": True, "side": "sell", "or_high": oh, "or_low": ol, "close": b["c"],
                    "time": kite.clock(b["end_min"]), "beyond_pct": 100 * (1 - b["c"] / ol)}
    return {"usable": True, "side": None, "or_high": oh, "or_low": ol}


def classify_buildup(px_chg, oi_chg):
    if px_chg >= 0 and oi_chg >= 0:
        return "long buildup"
    if px_chg < 0 and oi_chg >= 0:
        return "short buildup"
    if px_chg >= 0:
        return "short covering"
    return "long unwinding"


# ---------------------------------------------------------------- inputs

def universe(inst, which="fno"):
    if which == "fno":
        names = [n for n in inst.underlyings("NFO-FUT") if n not in kite.INDEX_NAMES]
        return [n for n in names if "NSE:%s" % n in inst.by_key]
    if which == "all":
        return sorted(r["tradingsymbol"] for r in inst.rows if r["exchange"] == "NSE" and r["segment"] == "NSE"
                      and r["instrument_type"] == "EQ" and "-" not in r["tradingsymbol"] and r["lot_size"] == 1)
    if os.path.exists(which):
        return [l.strip().upper() for l in open(which, encoding="utf-8") if l.strip() and not l.startswith("#")]
    raise KiteError("--set must be fno, all, or a file of NSE symbols")


def symbols_for(a, inst):
    if a.symbols:
        return [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    path = a.universe_file
    if os.path.exists(path) and a.set == "fno" and not a.refresh:
        syms = [l.strip() for l in open(path, encoding="utf-8") if l.strip()]
    else:
        syms = universe(inst, a.set)
    return syms[: a.limit] if a.limit else syms


def offline_candles(folder, sym):
    for name in ("%s.json.gz" % sym, "%s.json" % sym):
        p = os.path.join(folder, name)
        if os.path.exists(p):
            return kite.read_candles_file(p)
    return None


def progress(a, msg):
    if not a.quiet:
        print(msg, file=sys.stderr, flush=True)


def fetch_each(a, syms, interval, frm, to, inst, client):
    data, errors = {}, []
    for i, s in enumerate(syms, 1):
        try:
            if a.offline:
                c = offline_candles(a.offline, s)
                if c is None:
                    continue
            else:
                c = client.historical(inst.get("NSE:%s" % s)["instrument_token"], interval, frm, to)
            data[s] = c
        except KiteError as e:
            errors.append("%s: %s" % (s, e))
            if "session" in str(e).lower():
                raise
        if i % 25 == 0:
            progress(a, "  %d / %d" % (i, len(syms)))
    return data, errors


def write_watchlist(path, items, kind, now):
    kite.write_json(path, {"created_by": "trader-scanner %s" % kind, "created_at": kite.iso_ist(now),
                           "note": NOTE, "items": items})


# ---------------------------------------------------------------- commands

def cmd_universe(a):
    inst = kite.Instruments(("NSE", "NFO"))
    syms = universe(inst, a.set)
    d = os.path.dirname(a.universe_file)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(a.universe_file, "w", encoding="utf-8") as fh:
        fh.write("\n".join(syms) + "\n")
    print("%d symbols (%s) -> %s" % (len(syms), a.set, a.universe_file))
    return 0


def cmd_daily(a):
    now = kite.now_ist(a.now)
    inst = None if a.offline else kite.Instruments(("NSE", "NFO"))
    syms = symbols_for(a, inst) if inst else [os.path.basename(f).split(".")[0] for f in sorted(os.listdir(a.offline))]
    client = None if a.offline else kite.Kite()
    progress(a, "daily scan of %d symbols (Kite day candles)..." % len(syms))
    data, errors = fetch_each(a, syms, "day", now.date() - datetime.timedelta(days=150), now, inst, client)
    hits, skipped = scan_daily(data, now, a.min_price, a.min_turnover)
    if a.setup:
        want = set(a.setup.split(","))
        hits = [h for h in hits if want & set(h["setups"])]
    print("Daily setups, %d of %d charts (skipped: %s)%s" % (
        len(hits), len(data), ", ".join("%s %d" % kv for kv in skipped.items()),
        "; %d errors" % len(errors) if errors else ""))
    for h in hits[: a.show]:
        print("  %-12s %9.2f  %-5s %s  (RSI %s, 20d %.2f-%.2f, avg turnover Rs %.1f cr)" % (
            h["symbol"], h["close"], "SHORT" if h["side"] == "sell" else "LONG",
            ", ".join(LABEL[t] for t in h["setups"]), h["rsi"], h["low20"], h["high20"], h["turnover_cr"]))
    if a.write:
        items = [{"key": "NSE:%s" % h["symbol"], "symbol": h["symbol"], "side": h["side"], "setup": h["setups"][0],
                  "setups": h["setups"], "level": h["high20"] if "breakout_20d" in h["setups"] else
                  h["low20"] if "breakdown_20d" in h["setups"] else None, "close": h["close"],
                  "source": "scanner daily"} for h in hits]
        write_watchlist(a.watchlist, items, "daily", now)
        print("wrote %d to %s -> next: /trader-news, then /trader-entry watchlist" % (len(items), a.watchlist))
    print(NOTE)
    return 0 if hits else 1


def cmd_orb(a):
    now = kite.now_ist(a.now)
    cutoff = kite.hhmm(a.at) if a.at else now.hour * 60 + now.minute
    if cutoff < kite.OPEN_MIN + a.or_minutes + 5:
        raise KiteError("too early: the %d-minute range ends at %s and needs one more bar" % (
            a.or_minutes, kite.clock(kite.OPEN_MIN + a.or_minutes)))
    if not kite.is_trading_day(now.date()):
        raise KiteError("%s is not a trading day" % now.date())
    inst = None if a.offline else kite.Instruments(("NSE", "NFO"))
    syms = symbols_for(a, inst) if inst else [os.path.basename(f).split(".")[0] for f in sorted(os.listdir(a.offline))]
    client = None if a.offline else kite.Kite()
    clock_end = datetime.datetime.combine(now.date(), datetime.time(cutoff // 60, cutoff % 60), kite.IST)
    progress(a, "ORB scan of %d symbols to %s IST..." % (len(syms), kite.clock(cutoff)))
    data, errors = fetch_each(a, syms, "5minute", datetime.datetime.combine(now.date(), datetime.time(9, 15),
                                                                           kite.IST), clock_end, inst, client)
    n = a.or_minutes // 5
    hits, unusable, quiet = [], 0, 0
    for s, c in data.items():
        bars = kite.session_bars(c, now.date(), 5, clock_end)
        r = orb_hit(bars, n, cutoff)
        if not r["usable"]:
            unusable += 1
            continue
        if not r["side"] or (a.side and r["side"] != a.side):
            quiet += 1
            continue
        if r["close"] < a.min_price:
            continue
        hits.append(dict(r, symbol=s))
    hits.sort(key=lambda h: -h["beyond_pct"])
    longs = sum(1 for h in hits if h["side"] == "buy")
    print("Opening-range breaks at %s IST (%d-min range): %d of %d charts (%d up, %d down; %d unusable)%s" % (
        kite.clock(cutoff), a.or_minutes, len(hits), len(data), longs, len(hits) - longs, unusable,
        "; %d errors" % len(errors) if errors else ""))
    for h in hits[: a.show]:
        print("  %-12s %-5s range %.2f-%.2f, the %s close %.2f is %.2f%% %s" % (
            h["symbol"], "LONG" if h["side"] == "buy" else "SHORT", h["or_low"], h["or_high"], h["time"], h["close"],
            h["beyond_pct"], "above" if h["side"] == "buy" else "below"))
    if a.write:
        items = [{"key": "NSE:%s" % h["symbol"], "symbol": h["symbol"], "side": h["side"], "setup": "orb",
                  "entry_setup": "orb", "or_high": round(h["or_high"], 2), "or_low": round(h["or_low"], 2),
                  "trigger_close": h["close"], "trigger_time": h["time"], "source": "scanner orb"} for h in hits]
        write_watchlist(a.watchlist, items, "orb", now)
        print("wrote %d to %s -> next: /trader-news, /trader-entry check" % (len(items), a.watchlist))
    print(NOTE)
    return 0 if hits else 1


def cmd_buildup(a):
    now = kite.now_ist(a.now)
    inst = kite.Instruments(("NSE", "NFO"))
    client = kite.Kite()
    names = [s for s in (a.symbols.split(",") if a.symbols else inst.underlyings("NFO-FUT"))]
    futs = {}
    for nm in names:
        try:
            futs[nm] = inst.future(nm.strip().upper(), today=now.date())
        except KiteError:
            pass
    q = client.quote([f.key for f in futs.values()])
    rows = []
    prev_day = kite.prev_trading_day(now.date())
    for i, (nm, f) in enumerate(sorted(futs.items()), 1):
        v = q.get(f.key)
        if not v or not v.get("oi"):
            continue
        try:
            c = client.historical(f["instrument_token"], "day", prev_day - datetime.timedelta(days=7), prev_day,
                                  oi=True)
        except KiteError:
            continue
        if not c or not c[-1].get("oi"):
            continue
        prev_oi, prev_close = c[-1]["oi"], (v.get("ohlc") or {}).get("close") or c[-1]["c"]
        px = 100 * (v["last_price"] / prev_close - 1)
        oi = 100 * (v["oi"] / prev_oi - 1)
        if abs(px) >= a.min_move and abs(oi) >= a.min_oi:
            rows.append({"symbol": nm, "future": f["tradingsymbol"], "last": v["last_price"], "price_pct": round(px, 2),
                         "oi": v["oi"], "oi_pct": round(oi, 2), "kind": classify_buildup(px, oi)})
        if i % 25 == 0:
            progress(a, "  %d / %d" % (i, len(futs)))
    rows.sort(key=lambda r: -abs(r["oi_pct"]))
    print("F&O buildup vs %s (near-month futures, |price| >= %g%%, |OI| >= %g%%): %d" % (
        prev_day, a.min_move, a.min_oi, len(rows)))
    for r in rows[: a.show]:
        print("  %-12s %-15s %10.2f  price %+6.2f%%  OI %+6.2f%%  %s" % (r["symbol"], r["future"], r["last"],
                                                                        r["price_pct"], r["oi_pct"], r["kind"]))
    if a.write:
        items = [{"key": "NSE:%s" % r["symbol"] if not r["symbol"] in kite.INDEX_NAMES else "NFO:%s" % r["future"],
                  "symbol": r["symbol"], "side": "buy" if r["kind"] in ("long buildup", "short covering") else "sell",
                  "setup": r["kind"].replace(" ", "_"), "source": "scanner buildup"} for r in rows]
        write_watchlist(a.watchlist, items, "buildup", now)
        print("wrote %d to %s" % (len(items), a.watchlist))
    print(NOTE)
    return 0 if rows else 1


def main(argv=None):
    kite.safe_stdout()
    ap = argparse.ArgumentParser(description="Chart Scanner for NSE: daily setups, ORB, F&O buildup")
    sub = ap.add_subparsers(dest="cmd")

    def common(s):
        s.add_argument("--set", default="fno")
        s.add_argument("--universe-file", default="trader/universe.txt")
        s.add_argument("--refresh", action="store_true")
        s.add_argument("--symbols")
        s.add_argument("--limit", type=int)
        s.add_argument("--now")
        s.add_argument("--offline", help="a folder of <SYMBOL>.json(.gz) saved Kite candles")
        s.add_argument("--watchlist", default="trader/watchlist.json")
        s.add_argument("--write", action="store_true")
        s.add_argument("--show", type=int, default=40)
        s.add_argument("--quiet", action="store_true")
        s.add_argument("--min-price", type=float, default=50.0)
    s = sub.add_parser("universe")
    common(s)
    s = sub.add_parser("daily")
    common(s)
    s.add_argument("--min-turnover", type=float, default=10.0, help="crore rupees, 20-day average")
    s.add_argument("--setup", help="only these setups, comma separated")
    s = sub.add_parser("orb")
    common(s)
    s.add_argument("--at")
    s.add_argument("--or-minutes", type=int, default=15)
    s.add_argument("--side", choices=("buy", "sell"))
    s = sub.add_parser("buildup")
    common(s)
    s.add_argument("--min-move", type=float, default=1.0)
    s.add_argument("--min-oi", type=float, default=5.0)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"universe": cmd_universe, "daily": cmd_daily, "orb": cmd_orb, "buildup": cmd_buildup}[a.cmd](a)
    except KiteError as e:
        print("scan: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
