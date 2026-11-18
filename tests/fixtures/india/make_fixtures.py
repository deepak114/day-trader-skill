#!/usr/bin/env python3
"""Builds the SYNTHETIC candle fixtures the offline tests use. Deterministic (seeded).

These are made-up 5-minute NSE sessions in Kite's historical-candle JSON shape,
not market data. They exist so the tests can check the arithmetic exactly
without a Kite login. Run from the repo root:

    python tests/fixtures/india/make_fixtures.py
"""
import datetime
import gzip
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "..", "trader-kite"))
import kite  # noqa: E402

LAST_DAY = datetime.date(2026, 11, 18)


def ts(day, minutes):
    return "%sT%02d:%02d:00+0530" % (day.isoformat(), minutes // 60, minutes % 60)


def session(rng, day, start, drift=0.0, vol=0.0016):
    rows, p = [], start
    for i in range(kite.SESSION_BARS_5M):
        o = p
        c = o * (1 + rng.gauss(drift, vol))
        h = max(o, c) * (1 + abs(rng.gauss(0, vol / 2)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, vol / 2)))
        v = int(rng.uniform(20000, 60000) * (2.5 if i < 6 or i > 70 else 1))
        rows.append([ts(day, kite.OPEN_MIN + 5 * i), round(o, 2), round(h, 2), round(lo, 2), round(c, 2), v])
        p = c
    return rows, p


def breakout_day(day, start):
    """The test day: a 15-minute range 1498.00-1510.00, the 9:45 bar closes 1512.40 above it."""
    script = [
        (1500.0, 1506.0, 1498.0, 1504.0), (1504.0, 1510.0, 1501.5, 1507.0), (1507.0, 1509.0, 1502.0, 1505.5),
        (1505.5, 1508.5, 1503.0, 1508.0), (1508.0, 1509.8, 1504.6, 1509.2), (1509.2, 1513.0, 1508.5, 1512.4),
        (1512.4, 1516.0, 1511.0, 1515.1), (1515.1, 1518.2, 1513.0, 1517.6),
    ]
    rows = []
    for i, (o, h, lo, c) in enumerate(script):
        v = 90000 if i == 5 else 40000
        rows.append([ts(day, kite.OPEN_MIN + 5 * i), o, h, lo, c, v])
    return rows


def build():
    rng = random.Random(7)
    days = kite.trading_days_back(LAST_DAY, 61)
    candles, p = [], 1450.0
    for d in days[:-1]:
        rows, p = session(rng, d, p * (1 + rng.gauss(0, 0.006)))
        candles += rows
    candles += breakout_day(days[-1], 1500.0)
    doc = {"status": "success", "data": {"candles": candles},
           "note": "SYNTHETIC test data in Kite's candle shape (seed 7), not market data"}
    path = os.path.join(HERE, "RELIANCE-5m-synthetic.json.gz")
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(doc, fh)
    print("wrote %s: %d candles, %d sessions" % (path, len(candles), len(days)))


def build_quotes():
    """SYNTHETIC Kite /quote output for the NIFTY 2026-11-23 chain at spot 25980, priced by
    Black-Scholes at a 12 % vol smile-free, with a one-tick-wide-ish book. Not market data."""
    with open(os.path.join(HERE, "instruments-NFO.csv"), encoding="utf-8") as fh:
        inst = kite.Instruments(("NFO",), texts={"NFO": fh.read()}, today=LAST_DAY)
    now = datetime.datetime(2026, 11, 18, 10, 0, tzinfo=kite.IST)
    spot, T = 25980.0, kite.days_to_expiry("2026-11-23", now)
    out = {}
    for o in inst.options("NIFTY", "2026-11-23"):
        px = round(max(0.05, kite.bs_price(spot, o["strike"], T, 0.065, 0.12, o["instrument_type"])), 2)
        bid, ask = round(max(0.05, px - 0.5), 2), round(px + 0.5, 2)
        oi = int(1e6 * (1.0 / (1 + abs(o["strike"] - spot) / 200)))
        out[o.key] = {"instrument_token": o["instrument_token"], "last_price": px, "volume": oi // 3, "oi": oi,
                      "ohlc": {"open": px, "high": px, "low": px, "close": px},
                      "depth": {"buy": [{"price": bid, "quantity": 650, "orders": 3}],
                                "sell": [{"price": ask, "quantity": 650, "orders": 4}]}}
    path = os.path.join(HERE, "quotes-NIFTY-2026-11-23-synthetic.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"status": "success", "data": out, "note": "SYNTHETIC (Black-Scholes, 12% vol)"}, fh, indent=1)
    print("wrote %s: %d options" % (path, len(out)))


if __name__ == "__main__":
    build()
    build_quotes()
