#!/usr/bin/env python3
"""Earnings Analyst for trader-analyst (Indian quarterly results). Standard library.

There is no free machine-readable results API for NSE/BSE that a script may call,
so Claude reads the numbers (from the company's results filing on NSE/BSE, or
Moneycontrol's financials pages in the browser) and this script does the
arithmetic, so every growth rate and margin is computed the same way.

    python facts.py add RELIANCE --quarter Q2FY27 --revenue 258000 --ebitda 43800 --pat 18200 --eps 13.45 \\
        [--other-income 4100] [--source "NSE filing 2026-10-17"] [--consolidated]
    python facts.py show RELIANCE              # quarters, QoQ and YoY growth, margins, trailing 12 months
    python facts.py changes RELIANCE           # what changed in the latest quarter, in plain words
    python facts.py list

Amounts are in Rs crore (EPS in rupees). Indian fiscal quarters: Q1 = Apr-Jun,
Q2 = Jul-Sep, Q3 = Oct-Dec, Q4 = Jan-Mar; FY27 = April 2026 to March 2027.
Data lives in trader/fundamentals/<SYMBOL>.json. Keep standalone and consolidated
apart (--consolidated): mixing them makes every comparison wrong.
"""

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trader-kite"))
import kite  # noqa: E402
from kite import KiteError  # noqa: E402

FIELDS = ("revenue", "ebitda", "pat", "eps", "other_income")
DIR = "trader/fundamentals"


def parse_q(q):
    m = re.match(r"^Q([1-4])\s*FY\s*(\d{2}|\d{4})$", str(q).strip().upper())
    if not m:
        raise KiteError("quarter must look like Q2FY27, got %r" % q)
    n, fy = int(m.group(1)), int(m.group(2)) % 100
    return fy, n


def qname(fy, n):
    return "Q%dFY%02d" % (n, fy)


def prev_q(fy, n):
    return (fy, n - 1) if n > 1 else (fy - 1, 4)


def period_end(fy, n):
    year = 2000 + fy - (1 if n < 4 else 0)
    return {1: "%d-06-30", 2: "%d-09-30", 3: "%d-12-31", 4: "%d-03-31"}[n] % (year if n < 4 else 2000 + fy)


def path_for(sym, consolidated):
    return os.path.join(DIR, "%s%s.json" % (sym.upper(), "" if consolidated else "-standalone"))


def growth(a, b):
    if a is None or b in (None, 0):
        return None
    return 100.0 * (a / b - 1) if b > 0 else None


def margin(x, rev):
    return 100.0 * x / rev if x is not None and rev else None


def table(doc):
    qs = sorted(doc["quarters"].values(), key=lambda r: parse_q(r["quarter"]))
    by = {parse_q(r["quarter"]): r for r in qs}
    out = []
    for r in qs:
        k = parse_q(r["quarter"])
        p, y = by.get(prev_q(*k)), by.get((k[0] - 1, k[1]))
        row = dict(r)
        for f in ("revenue", "ebitda", "pat", "eps"):
            row[f + "_qoq"] = growth(r.get(f), p.get(f) if p else None)
            row[f + "_yoy"] = growth(r.get(f), y.get(f) if y else None)
        row["ebitda_margin"] = margin(r.get("ebitda"), r.get("revenue"))
        row["pat_margin"] = margin(r.get("pat"), r.get("revenue"))
        last4 = [by.get(k)]
        kk = k
        for _ in range(3):
            kk = prev_q(*kk)
            last4.append(by.get(kk))
        if all(last4):
            row["ttm_revenue"] = sum(x["revenue"] for x in last4)
            row["ttm_pat"] = sum(x["pat"] for x in last4)
            row["ttm_eps"] = sum(x["eps"] for x in last4) if all(x.get("eps") is not None for x in last4) else None
        out.append(row)
    return out


def _p(x):
    return "%+.1f%%" % x if x is not None else "-"


def _m(x):
    return "%.1f%%" % x if x is not None else "-"


def cmd_add(a):
    sym = a.symbol.upper()
    fy, n = parse_q(a.quarter)
    p = path_for(sym, a.consolidated)
    doc = kite.read_json(p, default=None) or {"symbol": sym, "basis": "consolidated" if a.consolidated else
                                               "standalone", "unit": "Rs crore", "quarters": {}}
    row = {"quarter": qname(fy, n), "period_end": period_end(fy, n), "source": a.source}
    for f in FIELDS:
        v = getattr(a, f)
        if v is not None:
            row[f] = v
    if "revenue" not in row or "pat" not in row:
        raise KiteError("give at least --revenue and --pat")
    doc["quarters"][row["quarter"]] = row
    kite.write_json(p, doc)
    print("saved %s %s (%s) -> %s" % (sym, row["quarter"], doc["basis"], p))
    return 0


def load(a):
    p = path_for(a.symbol, a.consolidated)
    doc = kite.read_json(p, default=None)
    if not doc:
        raise KiteError("no %s results for %s yet: add quarters with `facts.py add`" % (
            "consolidated" if a.consolidated else "standalone", a.symbol.upper()))
    return doc


def cmd_show(a):
    doc = load(a)
    rows = table(doc)[-a.quarters:]
    print("%s, %s, Rs crore" % (doc["symbol"], doc["basis"]))
    print("%-8s %10s %7s %7s %9s %6s %9s %7s %7s %7s %7s" % ("quarter", "revenue", "QoQ", "YoY", "EBITDA", "margin",
                                                             "PAT", "YoY", "margin", "EPS", "YoY"))
    for r in rows:
        print("%-8s %10.0f %7s %7s %9s %6s %9.0f %7s %7s %7s %7s" % (
            r["quarter"], r["revenue"], _p(r["revenue_qoq"]), _p(r["revenue_yoy"]),
            "%.0f" % r["ebitda"] if r.get("ebitda") is not None else "-", _m(r["ebitda_margin"]), r["pat"],
            _p(r["pat_yoy"]), _m(r["pat_margin"]), "%.2f" % r["eps"] if r.get("eps") is not None else "-",
            _p(r["eps_yoy"])))
    last = rows[-1]
    if last.get("ttm_revenue"):
        print("trailing 12 months to %s: revenue %.0f, PAT %.0f%s" % (
            last["quarter"], last["ttm_revenue"], last["ttm_pat"],
            ", EPS %.2f" % last["ttm_eps"] if last.get("ttm_eps") is not None else ""))
    return 0


def changes(doc):
    rows = table(doc)
    r = rows[-1]
    out = ["%s %s (%s):" % (doc["symbol"], r["quarter"], doc["basis"])]
    for f, word in (("revenue", "Revenue"), ("pat", "Net profit (PAT)"), ("eps", "EPS")):
        if r.get(f + "_yoy") is not None:
            out.append("  %s %s %.1f%% from a year ago%s" % (
                word, "rose" if r[f + "_yoy"] >= 0 else "fell", abs(r[f + "_yoy"]),
                " and %s %.1f%% from last quarter" % ("rose" if r[f + "_qoq"] >= 0 else "fell", abs(r[f + "_qoq"]))
                if r.get(f + "_qoq") is not None else ""))
    if len(rows) >= 5 and r.get("ebitda_margin") is not None and rows[-5].get("ebitda_margin") is not None:
        d = r["ebitda_margin"] - rows[-5]["ebitda_margin"]
        out.append("  EBITDA margin %.1f%%, %s %.1f points from a year ago" % (r["ebitda_margin"],
                                                                             "up" if d >= 0 else "down", abs(d)))
    if r.get("other_income") and r.get("pat"):
        share = 100 * r["other_income"] / r["pat"]
        if share >= 25:
            out.append("  Other income is %.0f%% of PAT: part of the profit is not from operations" % share)
    if r.get("revenue_yoy") is not None and r.get("pat_yoy") is not None:
        if r["pat_yoy"] > r["revenue_yoy"] + 10:
            out.append("  Profit grew much faster than revenue: check for one-offs, tax or other income")
        elif r["pat_yoy"] < r["revenue_yoy"] - 10:
            out.append("  Profit lagged revenue: costs or interest rose faster")
    out.append("  Numbers as entered from: %s" % (r.get("source") or "not recorded"))
    return out


def cmd_changes(a):
    print("\n".join(changes(load(a))))
    return 0


def cmd_list(a):
    if not os.path.isdir(DIR):
        print("nothing saved yet")
        return 0
    for f in sorted(os.listdir(DIR)):
        doc = kite.read_json(os.path.join(DIR, f), default={}) or {}
        qs = sorted(doc.get("quarters", {}), key=parse_q)
        print("%-24s %s quarters, latest %s" % (f, len(qs), qs[-1] if qs else "-"))
    return 0


def main(argv=None):
    kite.safe_stdout()
    ap = argparse.ArgumentParser(description="Earnings Analyst: Indian quarterly results arithmetic")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("add")
    s.add_argument("symbol")
    s.add_argument("--quarter", required=True)
    for f in FIELDS:
        s.add_argument("--" + f.replace("_", "-"), type=float)
    s.add_argument("--source")
    s.add_argument("--consolidated", action="store_true")
    for name in ("show", "changes"):
        s = sub.add_parser(name)
        s.add_argument("symbol")
        s.add_argument("--consolidated", action="store_true")
        s.add_argument("--quarters", type=int, default=8)
    sub.add_parser("list")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"add": cmd_add, "show": cmd_show, "changes": cmd_changes, "list": cmd_list}[a.cmd](a)
    except KiteError as e:
        print("analyst: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
