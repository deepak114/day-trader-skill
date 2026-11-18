#!/usr/bin/env python3
"""Time Machine for trader-timemachine (NSE / NFO). Standard library + ../trader-kite/kite.py.

Replays a planned day trade before you risk a rupee: 10,000 simulated runs of the
rest of the session from your entry, plus the same rule on every past session.

    python timemachine.py run                          # trader/plan.json, 10,000 replays, seed 19
    python timemachine.py run --write                  # also save trader/timemachine.json
    python timemachine.py run --until 15:00            # stop each replay at your flat-by time
    python timemachine.py run --pool NSE:RELIANCE      # draw the bars from another instrument (the
                                                       # underlying of a future or option)
    python timemachine.py run --key NSE:SBIN --date 2026-11-18 --entry 842.5 --stop 838 --target 851.5 \\
        --entry-time 09:45
    python timemachine.py chart                        # ASCII fan chart of the replays
    ... --offline FILE --now "2026-11-18 09:52"

How the replays work:
  1. Kite 5-minute candles for the last ~90 calendar days. Keep only FULL sessions
     before the plan's date: 75 bars, 9:15 to 15:30 IST (special sessions such as
     Muhurat trading and days with gaps are left out).
  2. The pool: for every bar slot 1..74, each past session's bar as ratios to the
     previous bar's close: (open, high, low, close) / prev close.
  3. One replay starts at the entry price and walks the rest of the session. For
     bar i it picks a slot j = i + randint(-window, window), kept in 1..74, then
     one past bar from pool[j], scaled to the current price. The stop is checked
     before the target in every bar, so a bar that touches both counts as the
     stop (the conservative call).
  4. Outcomes: target, stop, or neither (still open at --until: default the plan's
     flat_by, else 15:30). R = the target's R, -1 at the stop, else (price - entry) / risk.
  5. History: the plan's own rule (opening-range breakout with the same range,
     stop rule and target R) re-run on every one of those past sessions.

Verdict: the target's share of the replays that hit one side, against the share
that breaks even at the plan's R (1 / (1 + R)): under 5 points "close to a coin
flip / break-even", 5 to 15 "leans", 15+ "strongly".

A simulation built from the instrument's own recent bars is not a prediction:
results, RBI policy, global cues, circuit limits and F&O ban periods can do what
those sessions never did. An option's own history mixes different strikes'
moneyness and time decay: for options, --pool the underlying and read the result
as the underlying's odds, not the premium's.
"""

import argparse
import datetime
import json
import math
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trader-kite"))
import kite  # noqa: E402
from kite import KiteError  # noqa: E402

SESSION_BARS = kite.SESSION_BARS_5M     # 75
OPEN_MIN, CLOSE_MIN = kite.OPEN_MIN, kite.CLOSE_MIN
MIN_SESSIONS = 20
SAMPLE_PATHS = 160
BAND_PATHS = 2000
BAND_Q = (0.05, 0.25, 0.5, 0.75, 0.95)
HIST_BINS = 24
COIN_FLIP, STRONG, MANY_NEITHER = 0.05, 0.15, 0.40
LOOKBACK_DAYS = 95
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


# ---------------------------------------------------------------- data

def sessions_from(candles, before):
    """Full 75-bar sessions strictly before `before` (iso), oldest first, plus the short ones."""
    days = {}
    for b in candles:
        t = kite.to_ist(b["t"])
        m = t.hour * 60 + t.minute
        if m < OPEN_MIN or m >= CLOSE_MIN or (m - OPEN_MIN) % 5:
            continue
        days.setdefault(t.date().isoformat(), {})[(m - OPEN_MIN) // 5] = b
    full, short = [], []
    for d in sorted(days):
        if d >= before:
            continue
        s = days[d]
        if len(s) == SESSION_BARS:
            full.append((d, [s[i] for i in range(SESSION_BARS)]))
        else:
            short.append({"date": d, "bars": len(s)})
    return full, short, days


def build_pool(sessions):
    pool = [[] for _ in range(SESSION_BARS)]
    for _, D in sessions:
        for i in range(1, SESSION_BARS):
            pc = D[i - 1]["c"]
            pool[i].append((D[i]["o"] / pc, D[i]["h"] / pc, D[i]["l"] / pc, D[i]["c"] / pc))
    return pool


def pct(a, q):
    return a[min(len(a) - 1, int(q * len(a)))]


# ---------------------------------------------------------------- the simulation

def simulate(pool, entry, stop, target, k, last, replays=10000, seed=19, window=2, side="buy"):
    long = side == "buy"
    risk = (entry - stop) if long else (stop - entry)
    r_target = round(((target - entry) if long else (entry - target)) / risk, 4)

    def slot(rng, i):
        return i if window == 0 else min(SESSION_BARS - 1, max(1, i + rng.randint(-window, window)))

    rng = random.Random(seed)
    choice = rng.choice
    outcomes, closes, paths = [], [], []
    for n in range(replays):
        p, outcome = entry, None
        keep = n < SAMPLE_PATHS
        path = [entry]
        for i in range(k + 1, last + 1):
            o, h, l, c = choice(pool[slot(rng, i)])
            hi, lo, cl = p * h, p * l, p * c
            if outcome is None:
                if long:
                    if lo <= stop:
                        outcome = "stop"
                    elif hi >= target:
                        outcome = "target"
                else:
                    if hi >= stop:
                        outcome = "stop"
                    elif lo <= target:
                        outcome = "target"
            p = cl
            if keep:
                path.append(round(p, 2))
        outcomes.append(outcome or "close")
        closes.append(p)
        if keep:
            paths.append(path)
    cnt = {x: outcomes.count(x) for x in ("target", "stop", "close")}
    green = sum(1 for o, p in zip(outcomes, closes)
                if o == "target" or (o == "close" and (p > entry if long else p < entry)))
    Rs = [r_target if o == "target" else -1.0 if o == "stop" else ((p - entry) if long else (entry - p)) / risk
          for o, p in zip(outcomes, closes)]
    srt = sorted(closes)
    rng = random.Random(seed)
    choice = rng.choice
    allp = []
    for n in range(BAND_PATHS):
        p, path = entry, [entry]
        for i in range(k + 1, last + 1):
            o, h, l, c = choice(pool[slot(rng, i)])
            p = p * c
            path.append(p)
        allp.append(path)
    bands = []
    for j in range(last - k + 1):
        col = sorted(pp[j] for pp in allp)
        bands.append([round(pct(col, q), 2) for q in BAND_Q])
    lo_, hi_ = pct(srt, 0.01), pct(srt, 0.99)
    W = (hi_ - lo_) / HIST_BINS
    bins = [0] * HIST_BINS
    for c in srt:
        b = int((c - lo_) / W) if W > 0 else 0
        if 0 <= b < HIST_BINS:
            bins[b] += 1
    return {
        "replays": replays, "seed": seed, "window": window, "bars_left": last - k,
        "p_target": round(cnt["target"] / replays, 4), "p_stop": round(cnt["stop"] / replays, 4),
        "p_neither": round(cnt["close"] / replays, 4), "p_green": round(green / replays, 4),
        "median_close": round(pct(srt, 0.5), 2), "p5_close": round(pct(srt, 0.05), 2),
        "p95_close": round(pct(srt, 0.95), 2), "avg_R": round(statistics.mean(Rs), 3),
        "r_at_target": r_target, "band_paths": BAND_PATHS, "bands": bands, "sample_paths": paths,
        "hist": {"lo": round(lo_, 2), "hi": round(hi_, 2), "bins": bins},
    }


def opening_range(D, n):
    rows = [D[i] for i in range(n)]
    return max(b["h"] for b in rows), min(b["l"] for b in rows)


def rule_stop(rule, oh, ol, entry, long, tick):
    r = rule["stop_rule"]
    mode = "down" if long else "up"
    if r == "or_mid":
        return kite.round_tick((oh + ol) / 2, tick, mode)
    if r == "or_far":
        return kite.round_tick(ol if long else oh, tick, mode)
    frac = rule["stop_pct"] / 100.0
    return kite.round_tick(entry * (1 - frac) if long else entry * (1 + frac), tick, mode)


def history_replay(sessions, rule, last, side="buy", tick=0.05):
    long = side == "buy"
    n = rule["or_minutes"] // 5
    trades = []
    for d, D in sessions:
        oh, ol = opening_range(D, n)
        kk = next((i for i in range(n, last + 1) if (D[i]["c"] > oh if long else D[i]["c"] < ol)), None)
        if kk is None:
            continue
        e = round(D[kk]["c"], 2)
        s = rule_stop(rule, oh, ol, e, long, tick)
        risk = (e - s) if long else (s - e)
        if risk <= 0:
            continue
        t = kite.round_tick(e + rule["target_r"] * risk if long else e - rule["target_r"] * risk, tick,
                            "down" if long else "up")
        oc = None
        for b in D[kk + 1:last + 1]:
            if (b["l"] <= s) if long else (b["h"] >= s):
                oc = "stop"
                break
            if (b["h"] >= t) if long else (b["l"] <= t):
                oc = "target"
                break
        cl = D[last]["c"]
        R = ((t - e) if long else (e - t)) / risk if oc == "target" else -1.0 if oc == "stop" else \
            ((cl - e) if long else (e - cl)) / risk
        trades.append({"date": d, "time": kite.clock(OPEN_MIN + 5 * (kk + 1)), "entry": e, "stop": s, "target": t,
                       "outcome": oc or "close", "R": round(R, 2)})
    return {
        "sessions": len(sessions), "triggered": len(trades),
        "target": sum(1 for x in trades if x["outcome"] == "target"),
        "stop": sum(1 for x in trades if x["outcome"] == "stop"),
        "close": sum(1 for x in trades if x["outcome"] == "close"),
        "green": sum(1 for x in trades if x["R"] > 0),
        "avg_R": round(statistics.mean(x["R"] for x in trades), 3) if trades else None,
        "rule": rule, "trades": trades,
    }


# ---------------------------------------------------------------- the plan

def _num(v, what):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise KiteError("the plan needs a number for %s, got %r" % (what, v))
    if not x > 0 or math.isinf(x):
        raise KiteError("the plan's %s must be a positive price, got %r" % (what, v))
    return x


def normalize_plan(raw, src="flags"):
    if raw.get("strategy") not in (None, "single"):
        raise KiteError("the time machine replays a single-instrument plan; for an options strategy "
                        "replay the underlying with --key NSE:<underlying> and its own prices")
    key = kite.normalize_key(raw.get("key") or raw.get("symbol") or "")
    side = str(raw.get("side") or "buy").lower()
    if side not in ("buy", "sell"):
        raise KiteError("side must be buy or sell")
    try:
        date = datetime.date.fromisoformat(str(raw.get("date") or "")).isoformat()
    except ValueError:
        raise KiteError("the plan needs a date like 2026-11-18, got %r" % raw.get("date"))
    entry, stop, target = (_num(raw.get(k), k) for k in ("entry", "stop", "target"))
    long = side == "buy"
    if long and not stop < entry < target:
        raise KiteError("a buy needs stop < entry < target")
    if not long and not target < entry < stop:
        raise KiteError("a sell needs target < entry < stop")
    if not raw.get("entry_time"):
        raise KiteError("the plan needs entry_time (the entry bar's end, e.g. 09:45)")
    close_min = kite.hhmm(raw["entry_time"], "entry_time")
    if (close_min - OPEN_MIN) % 5:
        raise KiteError("entry_time %s is not on the 5-minute grid from 9:15" % raw["entry_time"])
    k = (close_min - OPEN_MIN) // 5 - 1
    if not 0 <= k <= SESSION_BARS - 2:
        raise KiteError("the entry must close between 9:20 and 15:25 to leave bars to replay")
    orng = raw.get("opening_range") or {}
    or_min = int(orng.get("minutes", 15) if isinstance(orng, dict) else 15)
    if or_min < 5 or or_min % 5 or or_min >= 375:
        raise KiteError("opening_range.minutes must be a multiple of 5")
    risk = (entry - stop) if long else (stop - entry)
    return {
        "key": key, "segment": raw.get("segment") or "equity", "side": side, "date": date,
        "setup": str(raw.get("setup") or "orb"), "entry": entry, "entry_time": kite.clock(close_min),
        "entry_bar": k, "stop": stop, "target": target, "risk_per_unit": round(risk, 2),
        "target_r": round(((target - entry) if long else (entry - target)) / risk, 2),
        "opening_range": {"minutes": or_min}, "stop_rule": str(raw.get("stop_rule") or "auto"),
        "tick_size": float(raw.get("tick_size") or 0.05), "flat_by": raw.get("flat_by"), "from": src,
    }


def resolve_rule(plan, entry_day):
    if plan["setup"].lower() not in ("orb",):
        return None
    long = plan["side"] == "buy"
    n = plan["opening_range"]["minutes"] // 5
    tick = plan["tick_size"]
    rule = {"setup": "orb", "or_minutes": plan["opening_range"]["minutes"], "target_r": plan["target_r"],
            "stop_pct": round(100.0 * plan["risk_per_unit"] / plan["entry"], 4)}
    sr = plan["stop_rule"].replace("-", "_")
    if sr in ("or_mid", "or_far"):
        rule["stop_rule"] = sr
    else:
        rule["stop_rule"] = "pct"
        if sr == "auto" and entry_day and all(i in entry_day for i in range(n)):
            oh, ol = opening_range(entry_day, n)
            if abs(kite.round_tick((oh + ol) / 2, tick, "down" if long else "up") - plan["stop"]) < 1e-6:
                rule["stop_rule"] = "or_mid"
            elif abs(kite.round_tick(ol if long else oh, tick, "down" if long else "up") - plan["stop"]) < 1e-6:
                rule["stop_rule"] = "or_far"
    if rule["stop_rule"] != "pct":
        del rule["stop_pct"]
    return rule


def lean(mc):
    pt, ps = mc["p_target"], mc["p_stop"]
    if pt + ps <= 0:
        return None
    r = mc.get("r_at_target") or 1.0
    share, even = pt / (pt + ps), 1.0 / (1.0 + r)
    return share - even, share, even


def verdict(mc):
    pt, ps = mc["p_target"], mc["p_stop"]
    lv = lean(mc)
    until = mc.get("until", "15:30")
    if lv is None:
        return "No replay reached the target or the stop by %s: the price at %s decides every one." % (until, until)
    d, share, even = lv
    r = mc.get("r_at_target") or 1.0
    one_r = abs(r - 1.0) < 0.005
    if abs(d) < COIN_FLIP:
        head = "Close to a coin flip" if one_r else "Close to break-even"
    elif abs(d) < STRONG:
        head = "Leans toward the target" if d > 0 else "Leans toward the stop"
    else:
        head = "Strongly toward the target" if d > 0 else "Strongly toward the stop"
    odds = "target first %.1f %% vs stop first %.1f %%" % (100 * pt, 100 * ps)
    if one_r:
        return "%s: %s." % (head, odds)
    return ("%s: at %gR the target has to come first in %.0f %% of the replays that hit one side; "
            "it did in %.0f %% (%s)." % (head, round(r, 2), 100 * even, 100 * share, odds))


def verdict_notes(mc):
    notes = []
    lv = lean(mc)
    if mc.get("p_neither", 0) > MANY_NEITHER and lv is not None:
        notes.append("%.1f %% of the replays hit neither by %s; the price then decides those."
                     % (100 * mc["p_neither"], mc.get("until")))
    avg = mc.get("avg_R")
    if lv and abs(lv[0]) >= COIN_FLIP and avg and (avg > 0) != (lv[0] > 0):
        notes.append("Average R is %+.3f all the same: the replays that hit neither pull it the other way." % avg)
    return notes


def time_machine(candles, plan, now, replays=10000, seed=19, window=2, until=None, pool_candles=None,
                 pool_key=None):
    if not 1 <= replays <= 1000000:
        raise KiteError("--replays must be between 1 and 1,000,000")
    if not 0 <= window <= 10:
        raise KiteError("--window must be between 0 and 10 bars")
    k = plan["entry_bar"]
    until = until or plan.get("flat_by") or "15:30"
    until_min = kite.hhmm(until, "--until")
    if (until_min - OPEN_MIN) % 5 or until_min > CLOSE_MIN:
        raise KiteError("--until must be on the 5-minute grid from 9:15 and no later than 15:30")
    last = (until_min - OPEN_MIN) // 5 - 1
    if last <= k:
        raise KiteError("--until %s leaves nothing to replay after the %s entry" % (until, plan["entry_time"]))
    full, short, days = sessions_from(candles, plan["date"])
    entry_day = days.get(plan["date"], {})
    warn = []
    if pool_candles is None:
        if k not in entry_day:
            raise KiteError("the entry is not in the data: no %s bar on %s for %s"
                            % (kite.clock(OPEN_MIN + 5 * k), plan["date"], plan["key"]))
        eb = entry_day[k]
        if not eb["l"] - plan["tick_size"] <= plan["entry"] <= eb["h"] + plan["tick_size"]:
            warn.append("the plan's entry %.2f is outside that bar's range %.2f to %.2f"
                        % (plan["entry"], eb["l"], eb["h"]))
        sessions = full
    else:
        sessions, short, _ = sessions_from(pool_candles, plan["date"])
        warn.append("the bars are drawn from %s, not %s itself" % (pool_key, plan["key"]))
    if plan["segment"] == "option" and pool_candles is None:
        warn.append("an option's own history mixes moneyness and time decay; --pool the underlying for a cleaner read")
    if len(sessions) < MIN_SESSIONS:
        raise KiteError("only %d full session(s) before %s in the data; the time machine needs at least %d "
                        "(a new contract? try --pool with the underlying)" % (len(sessions), plan["date"],
                                                                            MIN_SESSIONS))
    mc = simulate(build_pool(sessions), plan["entry"], plan["stop"], plan["target"], k, last, replays, seed,
                  window, plan["side"])
    mc = dict(mc, until=kite.clock(until_min), pool_sessions=len(sessions), pool_from=sessions[0][0],
              pool_to=sessions[-1][0], band_times=[kite.clock(OPEN_MIN + 5 * (j + 1)) for j in range(k, last + 1)])
    rule = resolve_rule(plan, entry_day if pool_candles is None else None)
    if rule:
        history = history_replay(sessions, rule, last, plan["side"], plan["tick_size"])
    else:
        history = {"sessions": len(sessions), "triggered": 0, "rule": None, "trades": [],
                   "note": "skipped: the history replay knows the opening-range breakout (orb) only, "
                           "this plan's setup is %r" % plan["setup"]}
    return {
        "created_by": "trader-timemachine", "created_at": kite.iso_ist(now),
        "data": {"key": pool_key or plan["key"], "interval": "5minute", "short_sessions_left_out": short},
        "plan": plan, "mc": mc, "history": history, "verdict": verdict(mc), "verdict_notes": verdict_notes(mc),
        "warnings": warn,
        "note": ("A simulation built from the last %d full NSE sessions. It is not a prediction: results, "
                 "policy, global cues and circuits can do what those sessions never did. Same seed, same numbers."
                 % len(sessions)),
    }


# ---------------------------------------------------------------- output

def pc(x):
    return "%.1f %%" % (100.0 * x)


def report(doc):
    p, m, h = doc["plan"], doc["mc"], doc["history"]
    long = p["side"] == "buy"
    wd = WEEKDAYS[datetime.date.fromisoformat(p["date"]).weekday()]
    lines = [
        "%s %s, %s %s: entry %.2f at %s IST, stop %.2f, target %.2f (%gR, risk %.2f a unit)."
        % (p["key"], "LONG" if long else "SHORT", wd, p["date"], p["entry"], p["entry_time"], p["stop"],
           p["target"], p["target_r"], p["risk_per_unit"]),
        "%s replays of the rest of the session, %s to %s IST (%d five-minute bars), seed %d."
        % ("{:,}".format(m["replays"]), p["entry_time"], m["until"], m["bars_left"], m["seed"]),
        "Each bar is drawn from %s's own 5-minute bars at the same time of day (+-%d bars) over its last "
        "%d full sessions (%s to %s)." % (doc["data"]["key"], m["window"], m["pool_sessions"], m["pool_from"],
                                         m["pool_to"]),
        "",
        "  target first   %s" % pc(m["p_target"]),
        "  stop first     %s" % pc(m["p_stop"]),
        "  neither        %-8s still open at %s" % (pc(m["p_neither"]), m["until"]),
        "  green          %-8s target first, or %s the entry at %s" % (pc(m["p_green"]),
                                                                       "above" if long else "below", m["until"]),
        "",
        "  Price at %s: median %.2f, 90 %% of replays between %.2f and %.2f." % (
            m["until"], m["median_close"], m["p5_close"], m["p95_close"]),
        "  Average R: %+.3f (+%gR at the target, -1R at the stop)." % (m["avg_R"], m["r_at_target"]),
        "",
    ]
    if h.get("rule"):
        ru = h["rule"]
        words = {"or_mid": "opening-range midpoint", "or_far": "far side of the range"}.get(
            ru["stop_rule"], "%.2f %% from the entry" % ru.get("stop_pct", 0))
        if h["triggered"]:
            lines.append("History: the same rule (%d-min opening range, stop at the %s, %gR target) on those %d "
                         "real sessions fired %d times: %d target, %d stop, %d neither. Average R %+.3f."
                         % (ru["or_minutes"], words, ru["target_r"], h["sessions"], h["triggered"], h["target"],
                            h["stop"], h["close"], h["avg_R"]))
        else:
            lines.append("History: the same rule never fired in those %d sessions." % h["sessions"])
    else:
        lines.append("History: " + h.get("note", "skipped."))
    lines += ["", doc["verdict"]] + list(doc.get("verdict_notes") or []) + [""]
    for w in doc.get("warnings") or []:
        lines.append("Note: " + w + ".")
    if doc["data"]["short_sessions_left_out"]:
        lines.append("Left out %d short session(s): %s." % (
            len(doc["data"]["short_sessions_left_out"]),
            ", ".join(x["date"] for x in doc["data"]["short_sessions_left_out"][:10])))
    lines.append(doc["note"] + " Repeat with --seed %d." % m["seed"])
    return lines


def render_chart(doc, height=17):
    p, m = doc["plan"], doc["mc"]
    bands, times = m["bands"], m["band_times"]
    lo = min([b[0] for b in bands] + [p["stop"], p["target"]])
    hi = max([b[4] for b in bands] + [p["stop"], p["target"]])
    pad = (hi - lo) * 0.04 or 0.01
    lo, hi = lo - pad, hi + pad
    step = (hi - lo) / (height - 1)

    def row(price):
        return max(0, min(height - 1, int(round((hi - price) / step))))
    grid = [[" "] * len(bands) for _ in range(height)]
    for j, b in enumerate(bands):
        r5, r25, r50, r75, r95 = [row(x) for x in b]
        for r in range(r95, r5 + 1):
            grid[r][j] = "."
        for r in range(r75, r25 + 1):
            grid[r][j] = ":"
        grid[r50][j] = "*"
    marks = {row(p["target"]): "target", row(p["stop"]): "stop"}
    if row(p["entry"]) not in marks:
        marks[row(p["entry"])] = "entry"
    for r in (row(p["target"]), row(p["stop"])):
        grid[r] = ["-" if ch == " " else ch for ch in grid[r]]
    out = ["%s %s, %s replay paths from the %s entry (seed %d)" % (
        p["key"], p["date"], "{:,}".format(m.get("band_paths", BAND_PATHS)), p["entry_time"], m["seed"])]
    for r in range(height):
        label = marks.get(r)
        price = {"target": p["target"], "stop": p["stop"], "entry": p["entry"]}.get(label, hi - r * step)
        left = ("%9.2f |" % price) if (label or r in (0, height - 1)) else "          |"
        out.append(left + "".join(grid[r]) + ("  " + label if label else ""))
    axis = [" "] * len(bands)
    for j in sorted({0, len(bands) // 2, len(bands) - 1}):
        lab = times[j]
        s = max(0, min(j, len(bands) - len(lab)))
        axis[s:s + len(lab)] = list(lab)
    out.append("          +" + "-" * len(bands))
    out.append("           " + "".join(axis))
    out.append("* median   : middle half of the paths   . 90 % of the paths   - stop and target")
    return out


# ---------------------------------------------------------------- commands

def load_plan(a, now):
    if a.key:
        missing = [f for f in ("entry", "stop", "target", "entry_time") if getattr(a, f) is None]
        if missing:
            raise KiteError("with --key also give %s" % ", ".join("--" + m.replace("_", "-") for m in missing))
        raw = {"key": a.key, "side": a.side, "date": a.date or now.date().isoformat(), "entry": a.entry,
               "stop": a.stop, "target": a.target, "entry_time": a.entry_time, "setup": a.setup,
               "opening_range": {"minutes": a.or_minutes}, "stop_rule": a.stop_rule, "tick_size": a.tick}
        return normalize_plan(raw, "flags")
    raw = kite.read_json(a.plan, "plan (run /trader-exit first, or pass --key ...)", required=True)
    return normalize_plan(raw, a.plan)


def compute(a):
    now = kite.now_ist(a.now)
    plan = load_plan(a, now)
    day = datetime.date.fromisoformat(plan["date"])
    frm = day - datetime.timedelta(days=LOOKBACK_DAYS)
    to = datetime.datetime.combine(day, datetime.time(15, 30), kite.IST)
    if to > now:
        to = now
    inst = None if a.offline else kite.Instruments(kite.exchanges_for(plan["key"]))
    candles = kite.get_candles(plan["key"], "5minute", frm, to, offline=a.offline, instruments=inst)
    pool = None
    if a.pool:
        pk = kite.normalize_key(a.pool)
        pinst = None if a.pool_offline else kite.Instruments(kite.exchanges_for(pk))
        pool = kite.get_candles(pk, "5minute", frm, to, offline=a.pool_offline, instruments=pinst)
    return time_machine(candles, plan, now, a.replays, a.seed, a.window, a.until, pool,
                        kite.normalize_key(a.pool) if a.pool else None)


def cmd_run(a):
    doc = compute(a)
    if a.write:
        kite.write_json(a.write, doc)
    if a.json:
        print(json.dumps(dict(doc, mc={k: v for k, v in doc["mc"].items() if k != "sample_paths"}), indent=2))
    else:
        print("\n".join(report(doc)))
        if a.write:
            print("Wrote %s." % a.write)
        print("Next: /trader-budget sizes it in lots to your budget and your max loss per trade.")
    return 0


def cmd_chart(a):
    if a.from_file:
        doc = kite.read_json(a.from_file, "time machine result", required=True)
    else:
        doc = compute(a)
    print("\n".join(render_chart(doc)))
    print("\n".join([doc.get("verdict", "")] + list(doc.get("verdict_notes") or [])))
    return 0


def main(argv=None):
    kite.safe_stdout()
    ap = argparse.ArgumentParser(description="Time Machine: replay a planned day trade on NSE bars")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--plan", default="trader/plan.json")
        p.add_argument("--key")
        p.add_argument("--side", default="buy", choices=["buy", "sell"])
        p.add_argument("--date")
        p.add_argument("--entry", type=float)
        p.add_argument("--stop", type=float)
        p.add_argument("--target", type=float)
        p.add_argument("--entry-time")
        p.add_argument("--tick", type=float, default=0.05)
        p.add_argument("--setup", default="orb")
        p.add_argument("--or-minutes", type=int, default=15)
        p.add_argument("--stop-rule", default="auto", choices=["auto", "or_mid", "or_far", "pct"])
        p.add_argument("--replays", type=int, default=10000)
        p.add_argument("--seed", type=int, default=19)
        p.add_argument("--window", type=int, default=2)
        p.add_argument("--until")
        p.add_argument("--pool", help="draw bars from this instrument instead (e.g. the underlying)")
        p.add_argument("--offline", metavar="FILE")
        p.add_argument("--pool-offline", metavar="FILE")
        p.add_argument("--now")

    p = sub.add_parser("run")
    common(p)
    p.add_argument("--json", action="store_true")
    p.add_argument("--write", nargs="?", const="trader/timemachine.json", metavar="PATH")
    p = sub.add_parser("chart")
    common(p)
    p.add_argument("--from", dest="from_file", nargs="?", const="trader/timemachine.json", metavar="PATH")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"run": cmd_run, "chart": cmd_chart}[a.cmd](a)
    except KiteError as e:
        print("timemachine: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
