#!/usr/bin/env python3
"""Shared Zerodha Kite Connect + NSE layer for the Indian trader skills. Standard library only.

Every other trader-* script imports this file from ../trader-kite/. It holds:

  * the clock: India Standard Time (UTC+5:30, no daylight saving)
  * the NSE calendar: 9:15 to 15:30 IST, weekends, the NSE trading holidays
    (2026 from NSE's list; later years from trader/holidays.json)
  * a READ-ONLY Kite Connect client: quotes, LTP, OHLC, historical candles,
    margin and charges calculators, funds, positions, holdings, orders, trades.
    It refuses every endpoint that places, modifies or cancels an order, so no
    script in this pack can ever send an order. You place orders in Kite yourself.
  * the instrument master (Kite's public daily CSV dump, cached per day) with
    lookups for equity, futures and options: lot size, tick size, expiry, strike
  * option-chain helpers and Black-Scholes IV / greeks

Command line:

    python kite.py login                    # YOU run this in your own terminal: browser sign-in,
                                            # saves the day's access token to ~/.kite/session.json
    python kite.py status                   # is today's session valid? (prints the user id only)
    python kite.py clock                    # IST now, market phase, next open
    python kite.py holidays [--year 2027]
    python kite.py instruments [--refresh]  # download today's instrument dumps
    python kite.py find RELIANCE            # every contract for an underlying (equity, FUT, near options)
    python kite.py quote NSE:RELIANCE NFO:NIFTY26NOVFUT
    python kite.py chain NIFTY [--expiry 2026-11-23] [--strikes 8]
    python kite.py funds                    # equity margin available / used

Credentials (never in chat, never in a file this pack writes into the project):
    KITE_API_KEY      your Kite Connect app's api_key   (env var, or "kite_api_key" in trader/config.json)
    KITE_API_SECRET   your app's api_secret              (env var only; used by `login` only)
    KITE_ACCESS_TOKEN optional; otherwise ~/.kite/session.json written by `login`
Kite access tokens expire at 6:00 AM IST the next day: run `login` once each trading morning.
"""

import argparse
import csv
import datetime
import hashlib
import io
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

API = "https://api.kite.trade"
LOGIN_URL = "https://kite.zerodha.com/connect/login?v=3&api_key=%s"
UA = "day-trader-skill-india/1.0"
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30), "IST")
UTC = datetime.timezone.utc

OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)
PREOPEN = datetime.time(9, 0)
OPEN_MIN = 9 * 60 + 15
CLOSE_MIN = 15 * 60 + 30
SESSION_MINUTES = CLOSE_MIN - OPEN_MIN          # 375
SESSION_BARS_5M = SESSION_MINUTES // 5          # 75

# NSE trading holidays (Equity, Equity Derivatives). 2026 from NSE's circular as
# published (cross-checked against Kite's expiry dates: 24-Nov and 26-Jan shift
# expiries to the Monday before). Weekend holidays are not listed: weekends are
# closed anyway. Add or correct years in trader/holidays.json:
#   {"2027": ["2027-01-26", "2027-03-22", ...], "2026": [...]}
NSE_HOLIDAYS = {
    2026: {
        "2026-01-15": "Municipal Corporation elections (Maharashtra)",
        "2026-01-26": "Republic Day",
        "2026-03-03": "Holi",
        "2026-03-26": "Shri Ram Navami",
        "2026-03-31": "Shri Mahavir Jayanti",
        "2026-04-03": "Good Friday",
        "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
        "2026-05-01": "Maharashtra Day",
        "2026-05-28": "Bakri Id",
        "2026-06-26": "Muharram",
        "2026-09-14": "Ganesh Chaturthi",
        "2026-10-02": "Mahatma Gandhi Jayanti",
        "2026-10-20": "Dussehra",
        "2026-11-10": "Diwali Balipratipada",
        "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
        "2026-12-25": "Christmas",
    },
    # 2027: NSE had not published the full list when this was written. Only the
    # date confirmed by Kite's expiry calendar is here; the tools warn that the
    # year is incomplete until trader/holidays.json lists it.
    2027: {
        "2027-01-26": "Republic Day",
    },
}
COMPLETE_YEARS = {2026}
HOLIDAYS_FILE = "trader/holidays.json"

# Kite rate limits: quote 1/s, historical 3/s, everything else 10/s.
MIN_GAP = {"quote": 1.05, "historical": 0.35, "other": 0.11}
QUOTE_MAX = {"quote": 500, "ohlc": 1000, "ltp": 1000}
# Max days per historical request by interval (Kite's documented limits).
HIST_DAYS = {"minute": 60, "3minute": 100, "5minute": 100, "10minute": 100, "15minute": 200,
             "30minute": 200, "60minute": 400, "day": 2000}
INTERVAL_ALIASES = {"1m": "minute", "3m": "3minute", "5m": "5minute", "10m": "10minute",
                    "15m": "15minute", "30m": "30minute", "60m": "60minute", "1h": "60minute",
                    "1d": "day", "d": "day"}

# The only endpoints this client will ever call. Nothing that places, modifies or
# cancels an order (or a GTT, or a basket, or a mutual fund order) is here.
READ_ONLY_GET = re.compile(r"^/(user/profile|user/margins(/equity|/commodity)?|quote|quote/ohlc|quote/ltp|"
                           r"instruments/historical/\d+/[a-z0-9]+|portfolio/positions|portfolio/holdings|"
                           r"orders|trades|gtt/triggers)$")
CALCULATOR_POST = {"/margins/orders", "/margins/basket", "/charges/orders", "/session/token"}

# F&O underlying name -> the spot/index instrument used for the underlying price.
INDEX_SPOT = {
    "NIFTY": "NSE:NIFTY 50", "BANKNIFTY": "NSE:NIFTY BANK", "FINNIFTY": "NSE:NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NSE:NIFTY MID SELECT", "NIFTYNXT50": "NSE:NIFTY NEXT 50",
    "SENSEX": "BSE:SENSEX", "BANKEX": "BSE:BANKEX", "SENSEX50": "BSE:SENSEX50",
}
INDEX_NAMES = set(INDEX_SPOT)
DEFAULT_RATE = 0.065   # risk-free rate for Black-Scholes; override "risk_free_rate" in trader/config.json


class KiteError(Exception):
    """A plain-language problem the scripts print as one line and exit 2."""


# ======================================================================= clock and calendar

def now_ist(text=None):
    """IST now (aware), or --now 'YYYY-MM-DD HH:MM' read as IST wall-clock."""
    if text:
        t = str(text).strip().replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.datetime.strptime(t, fmt).replace(tzinfo=IST)
            except ValueError:
                pass
        raise KiteError("--now must look like 2026-11-18 10:15 (IST), got %r" % text)
    return datetime.datetime.now(IST)


def to_ist(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(IST)


def iso_ist(dt):
    return to_ist(dt).strftime("%Y-%m-%dT%H:%M:%S+05:30")


def parse_ts(text):
    """Kite timestamps: '2026-11-18T09:15:00+0530', '2026-11-18 09:15:00' (IST)."""
    s = str(text).strip()
    m = re.match(r"^(\d{4}-\d\d-\d\d)[T ](\d\d:\d\d(?::\d\d)?)(?:\.\d+)?([+-]\d\d:?\d\d|Z)?$", s)
    if not m:
        raise KiteError("cannot read the time %r" % text)
    base = datetime.datetime.strptime(m.group(1) + " " + (m.group(2) + ":00")[:8], "%Y-%m-%d %H:%M:%S")
    tz = m.group(3)
    if not tz:
        return base.replace(tzinfo=IST)
    if tz == "Z":
        return base.replace(tzinfo=UTC).astimezone(IST)
    sign = 1 if tz[0] == "+" else -1
    hh, mm = int(tz[1:3]), int(tz[-2:])
    return base.replace(tzinfo=datetime.timezone(sign * datetime.timedelta(hours=hh, minutes=mm))).astimezone(IST)


def hhmm(text, what="time"):
    """'09:20', '9:20', '3:10 PM' -> minutes after midnight."""
    s = str(text).strip().upper().replace(".", "")
    m = re.match(r"^(\d{1,2}):(\d\d)\s*(AM|PM)?$", s)
    if not m:
        raise KiteError("%s must look like 09:20 or 3:10 PM, got %r" % (what, text))
    h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3)
    if ap:
        if not 1 <= h <= 12:
            raise KiteError("%s: bad hour in %r" % (what, text))
        h = h % 12 + (12 if ap == "PM" else 0)
    if h > 23 or mi > 59:
        raise KiteError("%s: bad time %r" % (what, text))
    return h * 60 + mi


def clock(minutes):
    return "%02d:%02d" % (minutes // 60, minutes % 60)


def clock12(minutes):
    h, m = minutes // 60, minutes % 60
    return "%d:%02d %s" % ((h - 1) % 12 + 1, m, "AM" if h < 12 else "PM")


_EXTRA = None


def _load_extra(path=None):
    global _EXTRA
    if _EXTRA is not None and path is None:
        return _EXTRA
    p = path or HOLIDAYS_FILE
    extra = {}
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as fh:
                doc = json.load(fh)
        except ValueError as e:
            raise KiteError("%s is not valid JSON: %s" % (p, e))
        for y, days in (doc or {}).items():
            if not re.match(r"^\d{4}$", str(y)):
                continue
            rows = days.items() if isinstance(days, dict) else ((d, "holiday (trader/holidays.json)") for d in days)
            extra[int(y)] = {str(d): str(n) for d, n in rows}
    if path is None:
        _EXTRA = extra
    return extra


def holidays(year, path=None):
    """{iso date: name} for a year: built-in list merged with trader/holidays.json."""
    out = dict(NSE_HOLIDAYS.get(year, {}))
    out.update(_load_extra(path).get(year, {}))
    return out


def calendar_complete(year, path=None):
    return year in COMPLETE_YEARS or year in _load_extra(path)


def calendar_warning(d):
    if calendar_complete(d.year):
        return None
    return ("NSE's %d holiday list is not in this pack yet: add it to %s (\"%d\": [\"%d-01-26\", ...]) "
            "or a holiday may be treated as a trading day" % (d.year, HOLIDAYS_FILE, d.year, d.year))


def is_trading_day(d):
    if isinstance(d, datetime.datetime):
        d = to_ist(d).date()
    return d.weekday() < 5 and d.isoformat() not in holidays(d.year)


def holiday_name(d):
    return holidays(d.year).get(d.isoformat())


def next_trading_day(d, include=False):
    x = d if include else d + datetime.timedelta(days=1)
    for _ in range(30):
        if is_trading_day(x):
            return x
        x += datetime.timedelta(days=1)
    raise KiteError("no trading day within 30 days of %s" % d)


def prev_trading_day(d, include=False):
    x = d if include else d - datetime.timedelta(days=1)
    for _ in range(30):
        if is_trading_day(x):
            return x
        x -= datetime.timedelta(days=1)
    raise KiteError("no trading day within 30 days before %s" % d)


def trading_days_back(d, n):
    """The n trading days ending at d (inclusive if d trades), oldest first."""
    out, x = [], d
    while len(out) < n:
        if is_trading_day(x):
            out.append(x)
        x -= datetime.timedelta(days=1)
    return out[::-1]


def market_status(now):
    """Phase of the NSE cash/F&O session at `now` (IST aware)."""
    n = to_ist(now)
    d, mins = n.date(), n.hour * 60 + n.minute
    st = {"now": n.strftime("%Y-%m-%d %H:%M IST"), "date": d.isoformat(), "minutes": mins,
          "trading_day": is_trading_day(d), "warning": calendar_warning(d)}
    if d.weekday() >= 5:
        st["phase"], st["why"] = "closed", "weekend"
    elif not st["trading_day"]:
        st["phase"], st["why"] = "closed", "NSE holiday: %s" % holiday_name(d)
    elif mins < 9 * 60:
        st["phase"], st["why"] = "pre-market", "opens 9:15 AM (pre-open 9:00 to 9:08)"
    elif mins < OPEN_MIN:
        st["phase"], st["why"] = "pre-open", "pre-open session; continuous trading from 9:15 AM"
    elif mins < CLOSE_MIN:
        st["phase"], st["why"] = "open", "continuous trading until 3:30 PM"
    else:
        st["phase"], st["why"] = "closed", "closed for the day at 3:30 PM"
    nd = d if (st["trading_day"] and mins < OPEN_MIN) else next_trading_day(d)
    st["next_open"] = "%s 09:15 IST" % nd.isoformat()
    return st


def slot_of(dt, minutes=5):
    """Bar index in the session for a bar starting at dt: 9:15 -> 0."""
    t = to_ist(dt)
    return (t.hour * 60 + t.minute - OPEN_MIN) // minutes


# ======================================================================= config and credentials

def read_json(path, what="file", required=False, default=None):
    if not os.path.exists(path):
        if required:
            raise KiteError("no %s at %s" % (what, path))
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise KiteError("%s is not valid JSON: %s" % (path, e))


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def load_config(path="trader/config.json"):
    cfg = read_json(path, default={}) or {}
    if not isinstance(cfg, dict):
        raise KiteError("%s must be a JSON object" % path)
    return cfg


def session_file():
    return os.environ.get("KITE_SESSION_FILE") or os.path.join(os.path.expanduser("~"), ".kite", "session.json")


def last_6am(now=None):
    n = to_ist(now or now_ist())
    six = n.replace(hour=6, minute=0, second=0, microsecond=0)
    return six if n >= six else six - datetime.timedelta(days=1)


def credentials(config="trader/config.json", now=None):
    """(api_key, access_token, source). Raises KiteError with the fix when missing or stale."""
    cfg = load_config(config)
    api_key = os.environ.get("KITE_API_KEY") or cfg.get("kite_api_key")
    token = os.environ.get("KITE_ACCESS_TOKEN")
    src = "KITE_ACCESS_TOKEN"
    if not token:
        sess = read_json(session_file(), default=None)
        if sess:
            src = session_file()
            when = sess.get("login_time")
            try:
                stale = bool(when) and parse_ts(when) < last_6am(now)
            except KiteError:
                stale = False
            if stale:
                raise KiteError("today's Kite session has expired (tokens end at 6:00 AM IST). "
                                "Run `python trader-kite/kite.py login` in your own terminal.")
            token = sess.get("access_token")
            api_key = api_key or sess.get("api_key")
    if not api_key:
        raise KiteError("no Kite api_key: set the KITE_API_KEY environment variable "
                        "(or \"kite_api_key\" in trader/config.json)")
    if not token:
        raise KiteError("not signed in to Kite today: run `python trader-kite/kite.py login` in your own "
                        "terminal (it opens Kite's sign-in page; nothing goes through the chat)")
    return api_key, token, src


# ======================================================================= the read-only client

def _check_allowed(method, path):
    bare = path.split("?", 1)[0]
    if method == "GET" and READ_ONLY_GET.match(bare):
        return
    if method == "POST" and bare in CALCULATOR_POST:
        return
    raise KiteError("refused: %s %s is not a read-only Kite endpoint. This pack never places, modifies or "
                    "cancels orders; place them in Kite yourself." % (method, bare))


class Kite(object):
    """Read-only Kite Connect client. `transport(method, url, headers, body) -> (status, text)`
    can be swapped in tests; `sleep` too."""

    def __init__(self, api_key=None, access_token=None, transport=None, sleep=time.sleep,
                 config="trader/config.json"):
        self.api_key, self.access_token = api_key, access_token
        self._config = config
        self.transport = transport or self._urllib
        self.sleep = sleep
        self._last = {}

    def _auth(self):
        if not (self.api_key and self.access_token):
            self.api_key, self.access_token, _ = credentials(self._config)
        return {"Authorization": "token %s:%s" % (self.api_key, self.access_token)}

    @staticmethod
    def _urllib(method, url, headers, body):
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as e:
            raise KiteError("could not reach Kite (%s)" % getattr(e, "reason", e))

    def _pace(self, kind):
        gap = MIN_GAP[kind]
        last = self._last.get(kind)
        if last is not None:
            wait = gap - (time.monotonic() - last)
            if wait > 0:
                self.sleep(wait)
        self._last[kind] = time.monotonic()

    def request(self, method, path, params=None, body=None, form=None, auth=True):
        _check_allowed(method, path)
        kind = "quote" if path.startswith("/quote") else "historical" if path.startswith("/instruments/hist") \
            else "other"
        url = API + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        headers = {"X-Kite-Version": "3", "User-Agent": UA}
        if auth:
            headers.update(self._auth())
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif form is not None:
            data = urllib.parse.urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        for attempt in (1, 2):
            self._pace(kind)
            status, text = self.transport(method, url, headers, data)
            if status == 429 and attempt == 1:
                self.sleep(2.0)
                continue
            break
        try:
            doc = json.loads(text) if text else {}
        except ValueError:
            raise KiteError("Kite answered HTTP %s with something that is not JSON" % status)
        if status == 403 or doc.get("error_type") == "TokenException":
            raise KiteError("Kite says the session is invalid or expired: run `python trader-kite/kite.py login` "
                            "in your own terminal")
        if status >= 400 or doc.get("status") == "error":
            raise KiteError("Kite error (HTTP %s, %s): %s" % (status, doc.get("error_type", "?"),
                                                              doc.get("message", text[:200])))
        return doc.get("data")

    # ---- market data
    def _quote_kind(self, kind, keys):
        keys = list(dict.fromkeys(keys))
        out = {}
        path = {"quote": "/quote", "ohlc": "/quote/ohlc", "ltp": "/quote/ltp"}[kind]
        for i in range(0, len(keys), QUOTE_MAX[kind]):
            part = keys[i:i + QUOTE_MAX[kind]]
            out.update(self.request("GET", path, params=[("i", k) for k in part]) or {})
        return out

    def quote(self, keys):
        return self._quote_kind("quote", keys)

    def ohlc(self, keys):
        return self._quote_kind("ohlc", keys)

    def ltp(self, keys):
        return self._quote_kind("ltp", keys)

    def historical(self, token, interval, frm, to, oi=False, continuous=False):
        """Candles [{t (aware IST), o, h, l, c, v, oi?}] between frm and to (dates or datetimes, IST),
        split into requests inside Kite's per-interval day limits."""
        interval = INTERVAL_ALIASES.get(interval, interval)
        if interval not in HIST_DAYS:
            raise KiteError("unknown candle interval %r" % interval)
        if isinstance(frm, datetime.date) and not isinstance(frm, datetime.datetime):
            frm = datetime.datetime.combine(frm, datetime.time(0, 0), IST)
        if isinstance(to, datetime.date) and not isinstance(to, datetime.datetime):
            to = datetime.datetime.combine(to, datetime.time(23, 59, 59), IST)
        frm, to = to_ist(frm), to_ist(to)
        step = datetime.timedelta(days=HIST_DAYS[interval] - 1)
        rows, a = {}, frm
        while a <= to:
            b = min(to, a + step)
            params = {"from": a.strftime("%Y-%m-%d %H:%M:%S"), "to": b.strftime("%Y-%m-%d %H:%M:%S")}
            if oi:
                params["oi"] = 1
            if continuous:
                params["continuous"] = 1
            data = self.request("GET", "/instruments/historical/%d/%s" % (int(token), interval), params=params)
            for c in (data or {}).get("candles", []):
                rows[c[0]] = candle(c)
            a = b + datetime.timedelta(seconds=1)
        return [rows[k] for k in sorted(rows, key=lambda s: parse_ts(s))]

    # ---- account (read only)
    def profile(self):
        return self.request("GET", "/user/profile")

    def funds(self, segment="equity"):
        return self.request("GET", "/user/margins/%s" % segment)

    def positions(self):
        return self.request("GET", "/portfolio/positions")

    def holdings(self):
        return self.request("GET", "/portfolio/holdings")

    def orders(self):
        return self.request("GET", "/orders")

    def trades(self):
        return self.request("GET", "/trades")

    # ---- calculators (no order is placed)
    def order_margins(self, orders):
        return self.request("POST", "/margins/orders", body=[margin_row(o) for o in orders])

    def basket_margins(self, orders, consider_positions=True):
        return self.request("POST", "/margins/basket",
                            params={"consider_positions": "true" if consider_positions else "false"},
                            body=[margin_row(o) for o in orders])

    def charges(self, orders):
        body = []
        for i, o in enumerate(orders):
            body.append({"order_id": str(i + 1), "exchange": o["exchange"], "tradingsymbol": o["tradingsymbol"],
                         "transaction_type": o["transaction_type"], "variety": o.get("variety", "regular"),
                         "product": o["product"], "order_type": o.get("order_type", "MARKET"),
                         "quantity": int(o["quantity"]), "average_price": float(o["average_price"])})
        return self.request("POST", "/charges/orders", body=body)


def candle(c):
    row = {"t": parse_ts(c[0]), "o": float(c[1]), "h": float(c[2]), "l": float(c[3]), "c": float(c[4]),
           "v": int(c[5] or 0)}
    if len(c) > 6:
        row["oi"] = int(c[6] or 0)
    return row


def margin_row(o):
    return {"exchange": o["exchange"], "tradingsymbol": o["tradingsymbol"],
            "transaction_type": o["transaction_type"], "variety": o.get("variety", "regular"),
            "product": o["product"], "order_type": o.get("order_type", "MARKET"),
            "quantity": int(o["quantity"]), "price": float(o.get("price") or 0),
            "trigger_price": float(o.get("trigger_price") or 0)}


def candles_from_doc(doc):
    """Saved historical JSON ({"data": {"candles": [...]}} or {"candles": [...]}) -> candle rows."""
    if isinstance(doc, dict) and "data" in doc:
        doc = doc["data"]
    rows = doc.get("candles") if isinstance(doc, dict) else doc
    if not isinstance(rows, list):
        raise KiteError("no candles in the saved data")
    return [candle(c) if isinstance(c, list) else dict(c, t=parse_ts(c["t"])) for c in rows]


def session_bars(candles, day, minutes=5, now=None):
    """Completed in-session bars of one IST day: [{slot, start 'HH:MM', end_min, o, h, l, c, v}].
    A bar counts once its end (start + minutes) is at or before `now`."""
    day = day if isinstance(day, str) else day.isoformat()
    out = {}
    for b in candles:
        t = to_ist(b["t"])
        if t.date().isoformat() != day:
            continue
        m = t.hour * 60 + t.minute
        if m < OPEN_MIN or m >= CLOSE_MIN:
            continue
        if now is not None and t + datetime.timedelta(minutes=minutes) > to_ist(now):
            continue
        s = (m - OPEN_MIN) // minutes
        out[s] = dict(b, slot=s, start=clock(m), end_min=m + minutes)
    return [out[k] for k in sorted(out)]


def split_days(candles):
    days = {}
    for b in candles:
        days.setdefault(to_ist(b["t"]).date().isoformat(), []).append(b)
    return days


def get_candles(key, interval, frm, to, offline=None, client=None, instruments=None, oi=False):
    """Candles for an instrument key ('NSE:RELIANCE'): from a saved file (--offline) or Kite."""
    if offline:
        rows = read_candles_file(offline)
        lo = frm if isinstance(frm, datetime.datetime) else datetime.datetime.combine(frm, datetime.time(0), IST)
        hi = to if isinstance(to, datetime.datetime) else datetime.datetime.combine(to, datetime.time(23, 59), IST)
        return [r for r in rows if lo <= to_ist(r["t"]) <= hi]
    inst = instruments or Instruments(exchanges_for(key))
    ins = inst.get(key)
    return (client or Kite()).historical(ins["instrument_token"], interval, frm, to, oi=oi)


def exchanges_for(key):
    ex = normalize_key(key).split(":")[0]
    return {"NSE": ("NSE",), "NFO": ("NSE", "NFO"), "BSE": ("BSE",), "BFO": ("BSE", "BFO")}.get(ex, (ex,))


def read_candles_file(path):
    import gzip
    if not os.path.exists(path):
        raise KiteError("no file %s" % path)
    raw = open(path, "rb").read()
    if path.endswith(".gz"):
        raw = gzip.decompress(raw)
    try:
        return candles_from_doc(json.loads(raw.decode("utf-8")))
    except ValueError as e:
        raise KiteError("%s is not valid JSON: %s" % (path, e))


# ======================================================================= instruments

INSTRUMENT_URL = API + "/instruments/%s"     # public: no sign-in needed
CACHE_DIR = "trader/cache/instruments"


class Instrument(dict):
    """One row of Kite's instrument dump, typed."""
    __getattr__ = dict.get

    @property
    def key(self):
        return "%s:%s" % (self["exchange"], self["tradingsymbol"])


def parse_instruments(text):
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        try:
            out.append(Instrument(
                instrument_token=int(r["instrument_token"]), exchange_token=r.get("exchange_token"),
                tradingsymbol=r["tradingsymbol"], name=(r.get("name") or "").strip('"'),
                expiry=r.get("expiry") or None, strike=float(r.get("strike") or 0),
                tick_size=float(r.get("tick_size") or 0.05), lot_size=int(float(r.get("lot_size") or 1)),
                instrument_type=r.get("instrument_type"), segment=r.get("segment"), exchange=r["exchange"]))
        except (KeyError, ValueError):
            continue
    return out


def _download(url, timeout=90):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "X-Kite-Version": "3"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        raise KiteError("could not download %s (%s)" % (url, getattr(e, "reason", e)))


class Instruments(object):
    """Kite's instrument master for some exchanges, cached under trader/cache/instruments/ per IST day
    (Kite regenerates the dump every morning)."""

    def __init__(self, exchanges=("NSE", "NFO"), today=None, cache_dir=CACHE_DIR, refresh=False,
                 texts=None, fetch=_download):
        self.rows, self.by_key, self.by_token = [], {}, {}
        today = today or now_ist().date()
        for ex in exchanges:
            if texts is not None:
                text = texts.get(ex, "")
            else:
                path = os.path.join(cache_dir, "%s-%s.csv" % (ex, today.isoformat()))
                if os.path.exists(path) and not refresh:
                    with open(path, encoding="utf-8") as fh:
                        text = fh.read()
                else:
                    text = fetch(INSTRUMENT_URL % ex)
                    os.makedirs(cache_dir, exist_ok=True)
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write(text)
                    for old in os.listdir(cache_dir):
                        if old.startswith(ex + "-") and old != os.path.basename(path):
                            try:
                                os.remove(os.path.join(cache_dir, old))
                            except OSError:
                                pass
            for ins in parse_instruments(text):
                self.rows.append(ins)
                self.by_key[ins.key] = ins
                self.by_token[ins["instrument_token"]] = ins

    def get(self, key):
        """'NSE:RELIANCE', 'NFO:NIFTY26NOVFUT' -> Instrument (KiteError if unknown)."""
        k = normalize_key(key)
        ins = self.by_key.get(k)
        if not ins:
            raise KiteError("%s is not in today's Kite instrument list (check the symbol and exchange)" % k)
        return ins

    def underlyings(self, segment="NFO-FUT"):
        return sorted({r["name"] for r in self.rows if r["segment"] == segment})

    def expiries(self, name, kinds=("FUT", "CE", "PE"), exchange=None, today=None):
        today = (today or now_ist().date()).isoformat()
        return sorted({r["expiry"] for r in self.rows if r["name"] == name and r["instrument_type"] in kinds
                       and r["expiry"] and r["expiry"] >= today and (exchange is None or r["exchange"] == exchange)})

    def future(self, name, expiry=None, today=None, exchange=None):
        """Nearest (or given) future for an underlying."""
        today = (today or now_ist().date()).isoformat()
        futs = sorted((r for r in self.rows if r["name"] == name and r["instrument_type"] == "FUT"
                       and r["expiry"] and r["expiry"] >= today and (exchange is None or r["exchange"] == exchange)),
                      key=lambda r: r["expiry"])
        if expiry:
            futs = [r for r in futs if r["expiry"] == expiry]
        if not futs:
            raise KiteError("no live future for %s%s" % (name, " expiring %s" % expiry if expiry else ""))
        return futs[0]

    def options(self, name, expiry, exchange=None):
        return sorted((r for r in self.rows if r["name"] == name and r["expiry"] == expiry
                       and r["instrument_type"] in ("CE", "PE") and (exchange is None or r["exchange"] == exchange)),
                      key=lambda r: (r["strike"], r["instrument_type"]))

    def option(self, name, expiry, strike, kind):
        kind = kind.upper()
        for r in self.options(name, expiry):
            if abs(r["strike"] - float(strike)) < 1e-6 and r["instrument_type"] == kind:
                return r
        raise KiteError("no %s %s %g %s in today's instrument list" % (name, expiry, float(strike), kind))

    def spot_key(self, name):
        """The instrument that carries the underlying's price: an index or the NSE stock."""
        if name in INDEX_SPOT:
            return INDEX_SPOT[name]
        return "NSE:%s" % name

    def resolve(self, spec, today=None):
        """Friendly spec -> Instrument.
             NSE:RELIANCE | RELIANCE (-> NSE equity) | NFO:NIFTY26NOVFUT
             'NIFTY FUT' (nearest) | 'RELIANCE FUT 2026-12-29'
             'NIFTY 26000 CE' (nearest expiry) | 'NIFTY 26000 PE 2026-12-01'"""
        s = " ".join(str(spec).upper().split())
        if ":" in s:
            return self.get(s)
        parts = s.split(" ")
        if len(parts) == 1:
            for ex in ("NSE", "BSE"):
                k = "%s:%s" % (ex, s)
                if k in self.by_key:
                    return self.by_key[k]
            for r in self.rows:
                if r["tradingsymbol"] == s:
                    return r
            raise KiteError("%s is not in today's instrument list" % s)
        name = parts[0]
        expiry = next((p for p in parts if re.match(r"^\d{4}-\d\d-\d\d$", p)), None)
        if "FUT" in parts:
            return self.future(name, expiry, today)
        kind = next((p for p in parts if p in ("CE", "PE")), None)
        strike = next((p for p in parts[1:] if re.match(r"^\d+(\.\d+)?$", p)), None)
        if kind and strike:
            if not expiry:
                ex = self.expiries(name, ("CE", "PE"), today=today)
                if not ex:
                    raise KiteError("no live options for %s" % name)
                expiry = ex[0]
            return self.option(name, expiry, strike, kind)
        raise KiteError("cannot read %r: use NSE:SYMBOL, 'NAME FUT [expiry]' or 'NAME STRIKE CE|PE [expiry]'" % spec)


def normalize_key(key):
    k = str(key).strip().upper()
    if ":" not in k:
        k = "NSE:" + k
    ex, sym = k.split(":", 1)
    return "%s:%s" % (ex.strip(), sym.strip())


def segment_of(ins):
    """'equity' | 'future' | 'option' | 'index'."""
    if ins["segment"] == "INDICES":
        return "index"
    t = ins["instrument_type"]
    if t == "FUT":
        return "future"
    if t in ("CE", "PE"):
        return "option"
    return "equity"


def round_tick(x, tick, mode="nearest"):
    """Round a price to the instrument's tick: nearest, down, or up."""
    tick = float(tick or 0.05)
    q = x / tick
    n = math.floor(q + 1e-9) if mode == "down" else math.ceil(q - 1e-9) if mode == "up" else round(q)
    return round(n * tick, 2)


def days_to_expiry(expiry, now):
    """Calendar time to 15:30 IST on expiry, in years (floor of one hour)."""
    e = datetime.datetime.combine(datetime.date.fromisoformat(expiry), datetime.time(15, 30), IST)
    secs = max(3600.0, (e - to_ist(now)).total_seconds())
    return secs / (365.0 * 86400)


# ======================================================================= Black-Scholes

def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _npdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def bs_price(S, K, T, r, vol, kind):
    if vol <= 0 or T <= 0:
        return max(0.0, S - K) if kind == "CE" else max(0.0, K - S)
    d1 = (math.log(S / K) + (r + 0.5 * vol * vol) * T) / (vol * math.sqrt(T))
    d2 = d1 - vol * math.sqrt(T)
    if kind == "CE":
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def implied_vol(price, S, K, T, r, kind):
    """Bisection IV; None when the price is below intrinsic or above any sane vol."""
    intrinsic = max(0.0, S - K * math.exp(-r * T)) if kind == "CE" else max(0.0, K * math.exp(-r * T) - S)
    if price is None or price <= 0 or price < intrinsic - 1e-6:
        return None
    lo, hi = 1e-4, 5.0
    if bs_price(S, K, T, r, hi, kind) < price:
        return None
    for _ in range(100):
        mid = (lo + hi) / 2
        if bs_price(S, K, T, r, mid, kind) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def greeks(S, K, T, r, vol, kind):
    """delta, gamma, theta (per calendar day), vega (per 1 vol point)."""
    if not vol or T <= 0:
        return {"delta": None, "gamma": None, "theta": None, "vega": None}
    sq = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * vol * vol) * T) / (vol * sq)
    d2 = d1 - vol * sq
    gamma = _npdf(d1) / (S * vol * sq)
    vega = S * _npdf(d1) * sq / 100.0
    if kind == "CE":
        delta = _ncdf(d1)
        theta = (-S * _npdf(d1) * vol / (2 * sq) - r * K * math.exp(-r * T) * _ncdf(d2)) / 365.0
    else:
        delta = _ncdf(d1) - 1
        theta = (-S * _npdf(d1) * vol / (2 * sq) + r * K * math.exp(-r * T) * _ncdf(-d2)) / 365.0
    return {"delta": round(delta, 4), "gamma": round(gamma, 6), "theta": round(theta, 2), "vega": round(vega, 2)}


def option_chain(inst, name, expiry, quotes, spot, now, strikes=8, rate=DEFAULT_RATE):
    """Rows around the ATM strike: strike, CE/PE last, OI, change in OI %, volume, IV, delta.
    `quotes` is Kite /quote output keyed 'NFO:SYMBOL'."""
    opts = inst.options(name, expiry)
    all_strikes = sorted({o["strike"] for o in opts})
    if not all_strikes:
        raise KiteError("no options for %s %s" % (name, expiry))
    atm = min(all_strikes, key=lambda k: abs(k - spot))
    i = all_strikes.index(atm)
    pick = all_strikes[max(0, i - strikes): i + strikes + 1]
    T = days_to_expiry(expiry, now)
    rows = []
    for k in pick:
        row = {"strike": k, "atm": k == atm}
        for kind in ("CE", "PE"):
            o = next((x for x in opts if x["strike"] == k and x["instrument_type"] == kind), None)
            if not o:
                continue
            q = quotes.get(o.key) or {}
            lp = q.get("last_price")
            iv = implied_vol(lp, spot, k, T, rate, kind) if lp else None
            g = greeks(spot, k, T, rate, iv, kind) if iv else {}
            row[kind] = {"tradingsymbol": o["tradingsymbol"], "last": lp, "oi": q.get("oi"),
                         "volume": q.get("volume"), "iv": round(100 * iv, 2) if iv else None,
                         "delta": g.get("delta"), "theta": g.get("theta"),
                         "bid": _best(q, "buy"), "ask": _best(q, "sell")}
        rows.append(row)
    ce_oi = sum((r.get("CE") or {}).get("oi") or 0 for r in rows)
    pe_oi = sum((r.get("PE") or {}).get("oi") or 0 for r in rows)
    return {"name": name, "expiry": expiry, "spot": spot, "atm": atm, "lot_size": opts[0]["lot_size"],
            "rows": rows, "pcr_oi": round(pe_oi / ce_oi, 2) if ce_oi else None,
            "max_pain": max_pain(rows)}


def _best(q, side):
    d = ((q.get("depth") or {}).get(side) or [])
    return d[0]["price"] if d and d[0].get("price") else None


def max_pain(rows):
    """Strike where option writers lose least at expiry, over the strikes shown."""
    best, best_k = None, None
    for r in rows:
        k = r["strike"]
        pain = 0.0
        for x in rows:
            ce, pe = (x.get("CE") or {}).get("oi") or 0, (x.get("PE") or {}).get("oi") or 0
            pain += ce * max(0.0, k - x["strike"]) + pe * max(0.0, x["strike"] - k)
        if best is None or pain < best:
            best, best_k = pain, k
    return best_k


# ======================================================================= money

def inr(x, dec=2):
    """Indian digit grouping: 1234567.5 -> 'Rs 12,34,567.50'."""
    if x is None:
        return "-"
    neg = x < 0
    s = ("%." + str(dec) + "f") % abs(x)
    whole, _, frac = s.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
        whole = head + "," + tail
    return ("-" if neg else "") + "Rs " + whole + ("." + frac if frac else "")


def safe_stdout():
    """Windows consoles default to cp1252: never crash on a character they cannot show."""
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


# ======================================================================= login (run by the user)

def login(port=5678, open_browser=True, request_token=None):
    """Browser sign-in. Your Kite Connect app's redirect URL must be http://127.0.0.1:<port>/ .
    Saves {api_key, access_token, user_id, login_time} to ~/.kite/session.json (only you can read it)."""
    api_key = os.environ.get("KITE_API_KEY") or load_config().get("kite_api_key")
    secret = os.environ.get("KITE_API_SECRET")
    if not api_key or not secret:
        raise KiteError("set KITE_API_KEY and KITE_API_SECRET as environment variables first")
    if not request_token:
        import http.server
        got = {}

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if "request_token" in q:
                    got["token"] = q["request_token"][0]
                    got["status"] = (q.get("status") or ["?"])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<h3>Kite sign-in received. You can close this tab.</h3>")

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", port), H)
        url = LOGIN_URL % api_key
        print("Opening Kite's sign-in page. If it does not open, visit:\n  %s" % url)
        if open_browser:
            webbrowser.open(url)
        t0 = time.time()
        srv.timeout = 1
        while "token" not in got and time.time() - t0 < 300:
            srv.handle_request()
        srv.server_close()
        if "token" not in got:
            raise KiteError("no sign-in within 5 minutes (is the app's redirect URL http://127.0.0.1:%d/ ?)" % port)
        request_token = got["token"]
    checksum = hashlib.sha256((api_key + request_token + secret).encode("utf-8")).hexdigest()
    k = Kite(api_key=api_key, access_token="-")
    data = k.request("POST", "/session/token", form={"api_key": api_key, "request_token": request_token,
                                                     "checksum": checksum}, auth=False)
    path = session_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    doc = {"api_key": api_key, "access_token": data["access_token"], "user_id": data.get("user_id"),
           "login_time": iso_ist(now_ist())}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    print("Signed in as %s. Session saved to %s (valid until 6:00 AM IST tomorrow)." % (data.get("user_id"), path))


# ======================================================================= CLI

def _cmd_clock(a):
    st = market_status(now_ist(a.now))
    print("%s  %s (%s). Next open %s." % (st["now"], st["phase"].upper(), st["why"], st["next_open"]))
    if st["warning"]:
        print("note: " + st["warning"])


def _cmd_holidays(a):
    y = a.year or now_ist().year
    h = holidays(y)
    for d in sorted(h):
        print("%s %s  %s" % (d, datetime.date.fromisoformat(d).strftime("%a"), h[d]))
    if not calendar_complete(y):
        print("note: %d is incomplete here; add NSE's list to %s" % (y, HOLIDAYS_FILE))


def _cmd_status(a):
    k = Kite()
    p = k.profile()
    print("Kite session OK: user %s (%s). Exchanges: %s" % (p.get("user_id"), p.get("broker", "ZERODHA"),
                                                           ", ".join(p.get("exchanges") or [])))


def _cmd_instruments(a):
    exch = a.exchanges.split(",")
    inst = Instruments(exch, refresh=a.refresh)
    print("%d instruments for %s cached in %s" % (len(inst.rows), ", ".join(exch), CACHE_DIR))


def _cmd_find(a):
    inst = Instruments(("NSE", "NFO", "BSE", "BFO") if a.bse else ("NSE", "NFO"))
    name = a.name.upper()
    try:
        eq = inst.get(inst.spot_key(name))
        print("spot   %-28s token %d  tick %g" % (eq.key, eq["instrument_token"], eq["tick_size"]))
    except KiteError:
        pass
    futs = sorted((r for r in inst.rows if r["name"] == name and r["instrument_type"] == "FUT"),
                  key=lambda r: r["expiry"])
    for f in futs:
        print("future %-28s expiry %s  lot %d  tick %g" % (f.key, f["expiry"], f["lot_size"], f["tick_size"]))
    ex = inst.expiries(name, ("CE", "PE"))
    if ex:
        print("option expiries: %s" % ", ".join(ex[:8]) + (" ..." if len(ex) > 8 else ""))


def _cmd_quote(a):
    q = Kite().quote([normalize_key(k) for k in a.keys])
    for k, v in q.items():
        o = v.get("ohlc") or {}
        prev = o.get("close") or 0
        chg = (v["last_price"] / prev - 1) * 100 if prev else 0
        print("%-26s %10.2f  %+6.2f%%  O %.2f H %.2f L %.2f  prev %.2f  vol %s%s" % (
            k, v["last_price"], chg, o.get("open", 0), o.get("high", 0), o.get("low", 0), prev,
            v.get("volume", "-"), "  OI %s" % v["oi"] if v.get("oi") else ""))


def _cmd_chain(a):
    now = now_ist(a.now)
    name = a.name.upper()
    exch = ("NSE", "NFO", "BSE", "BFO") if name in ("SENSEX", "BANKEX", "SENSEX50") else ("NSE", "NFO")
    inst = Instruments(exch)
    expiry = a.expiry or (inst.expiries(name, ("CE", "PE"), today=now.date()) or [None])[0]
    if not expiry:
        raise KiteError("no live option expiry for %s" % name)
    k = Kite()
    spot_key = inst.spot_key(name)
    spot = k.ltp([spot_key])[spot_key]["last_price"]
    opts = inst.options(name, expiry)
    strikes = sorted({o["strike"] for o in opts})
    atm = min(strikes, key=lambda s: abs(s - spot))
    i = strikes.index(atm)
    pick = set(strikes[max(0, i - a.strikes): i + a.strikes + 1])
    keys = [o.key for o in opts if o["strike"] in pick]
    rate = float(load_config().get("risk_free_rate", DEFAULT_RATE))
    ch = option_chain(inst, name, expiry, k.quote(keys), spot, now, a.strikes, rate)
    if a.json:
        print(json.dumps(ch, indent=2))
        return
    print("%s options, expiry %s, spot %.2f, ATM %g, lot %d, PCR(OI) %s, max pain %s" % (
        name, expiry, spot, ch["atm"], ch["lot_size"], ch["pcr_oi"], ch["max_pain"]))
    print("%9s %8s %6s %5s %11s | %-8s | %11s %5s %6s %8s %9s" % ("CE OI", "CE vol", "CE IV", "delta", "CE last",
                                                                  "strike", "PE last", "delta", "PE IV", "PE vol",
                                                                  "PE OI"))
    for r in ch["rows"]:
        c, p = r.get("CE") or {}, r.get("PE") or {}
        f = lambda v, fmt: (fmt % v) if v is not None else "-"
        print("%9s %8s %6s %5s %11s | %-8s | %11s %5s %6s %8s %9s" % (
            f(c.get("oi"), "%d"), f(c.get("volume"), "%d"), f(c.get("iv"), "%.1f"), f(c.get("delta"), "%.2f"),
            f(c.get("last"), "%.2f"), ("%g" % r["strike"]) + (" *" if r["atm"] else ""),
            f(p.get("last"), "%.2f"), f(p.get("delta"), "%.2f"), f(p.get("iv"), "%.1f"),
            f(p.get("volume"), "%d"), f(p.get("oi"), "%d")))


def _cmd_funds(a):
    f = Kite().funds("equity")
    av, ut = f.get("available") or {}, f.get("utilised") or {}
    print("Equity segment: net %s | cash %s | collateral %s | used: span %s, exposure %s, premium %s" % (
        inr(f.get("net")), inr(av.get("cash")), inr(av.get("collateral")), inr(ut.get("span")),
        inr(ut.get("exposure")), inr(ut.get("option_premium"))))


def main(argv=None):
    safe_stdout()
    ap = argparse.ArgumentParser(description="Kite Connect (read-only) + NSE calendar for the trader skills")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("login")
    s.add_argument("--port", type=int, default=5678)
    s.add_argument("--no-browser", action="store_true")
    s.add_argument("--request-token")
    sub.add_parser("status")
    s = sub.add_parser("clock")
    s.add_argument("--now")
    s = sub.add_parser("holidays")
    s.add_argument("--year", type=int)
    s = sub.add_parser("instruments")
    s.add_argument("--exchanges", default="NSE,NFO")
    s.add_argument("--refresh", action="store_true")
    s = sub.add_parser("find")
    s.add_argument("name")
    s.add_argument("--bse", action="store_true")
    s = sub.add_parser("quote")
    s.add_argument("keys", nargs="+")
    s = sub.add_parser("chain")
    s.add_argument("name")
    s.add_argument("--expiry")
    s.add_argument("--strikes", type=int, default=8)
    s.add_argument("--now")
    s.add_argument("--json", action="store_true")
    sub.add_parser("funds")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "login":
            login(a.port, not a.no_browser, a.request_token)
        elif a.cmd is None:
            ap.print_help()
        else:
            globals()["_cmd_" + a.cmd](a)
    except KiteError as e:
        print("kite: %s" % e, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
