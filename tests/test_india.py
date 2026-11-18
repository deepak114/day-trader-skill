"""Offline tests for the India edition. Real Kite instrument rows, SYNTHETIC candles and quotes."""
import datetime
import json
import os
import re
import tempfile
import types
import unittest

from _load import FIX, ROOT, instruments, load
import kite

entry = load("trader-entry", "entry.py")
exitp = load("trader-exit", "exit.py")
tm = load("trader-timemachine", "timemachine.py")
size = load("trader-budget", "size.py")
risk = load("trader-risk", "check.py")
ticket = load("trader-broker", "ticket.py")
alerts = load("trader-alerts", "alerts.py")
options = load("trader-options", "options.py")
scan = load("trader-scanner", "scan.py")
news = load("trader-news", "news.py")
deals = load("trader-deals", "deals.py")
facts = load("trader-analyst", "facts.py")
ideas = load("trader-moneycontrol", "ideas.py")

CANDLES = os.path.join(FIX, "RELIANCE-5m-synthetic.json.gz")
QUOTES = os.path.join(FIX, "quotes-NIFTY-2026-11-23-synthetic.json")


def at(s):
    return kite.now_ist(s)


def candles():
    return kite.read_candles_file(CANDLES)


def reliance_plan(**kw):
    e = entry.evaluate(candles(), at("2026-11-18 09:52"), "NSE:RELIANCE")
    a = types.SimpleNamespace(key=None, entry_file=None, side=None, or_high=None, or_low=None, stop="or-mid",
                              target="2R", min_rr=None, product=None, flat_by=None, offline=CANDLES)
    for k, v in kw.items():
        setattr(a, k, v)
    with tempfile.TemporaryDirectory() as d:
        a.entry_file = os.path.join(d, "entry.json")
        kite.write_json(a.entry_file, entry.entry_doc(e, at("2026-11-18 09:52")))
        return exitp.build_plan(a, at("2026-11-18 09:52"), {}, None)


class Clock(unittest.TestCase):
    def test_phases_and_holidays(self):
        self.assertEqual(kite.market_status(at("2026-11-18 09:05"))["phase"], "pre-open")
        self.assertEqual(kite.market_status(at("2026-11-18 10:00"))["phase"], "open")
        self.assertEqual(kite.market_status(at("2026-11-18 15:30"))["phase"], "closed")
        st = kite.market_status(at("2026-11-24 10:00"))
        self.assertFalse(st["trading_day"])
        self.assertIn("Guru Nanak", st["why"])
        self.assertEqual(st["next_open"], "2026-11-25 09:15 IST")
        self.assertEqual(kite.market_status(at("2026-11-21 10:00"))["why"], "weekend")

    def test_2027_is_flagged_incomplete(self):
        self.assertIsNone(kite.calendar_warning(datetime.date(2026, 5, 4)))
        self.assertIn("2027", kite.calendar_warning(datetime.date(2027, 5, 4)))

    def test_parsing_and_money(self):
        self.assertEqual(kite.hhmm("3:10 PM"), 910)
        self.assertEqual(kite.hhmm("09:15"), 555)
        self.assertEqual(kite.parse_ts("2026-11-18T09:15:00+0530").hour, 9)
        self.assertEqual(kite.parse_ts("2026-11-18T03:45:00Z").hour, 9)
        self.assertEqual(kite.inr(1234567.5), "Rs 12,34,567.50")
        self.assertEqual(kite.inr(-950), "-Rs 950.00")
        self.assertEqual(kite.round_tick(358.37, 0.05, "down"), 358.35)
        self.assertEqual(kite.round_tick(358.31, 0.05, "up"), 358.35)
        self.assertEqual(kite.round_tick(358.32, 0.05), 358.30)


class KiteClient(unittest.TestCase):
    def test_order_endpoints_are_refused(self):
        for m, p in (("POST", "/orders/regular"), ("PUT", "/orders/regular/1"), ("DELETE", "/orders/regular/1"),
                     ("POST", "/gtt/triggers"), ("POST", "/orders/amo"), ("DELETE", "/gtt/triggers/5")):
            with self.assertRaises(kite.KiteError):
                kite._check_allowed(m, p)
        kite._check_allowed("GET", "/quote")
        kite._check_allowed("POST", "/margins/basket")
        k = kite.Kite("k", "t", transport=lambda *a: self.fail("must not be sent"))
        with self.assertRaises(kite.KiteError):
            k.request("POST", "/orders/regular", body={})

    def test_quote_chunks_and_auth(self):
        calls = []

        def tr(method, url, headers, body):
            calls.append((url, headers))
            n = url.count("i=")
            return 200, json.dumps({"status": "success", "data": {"K%d" % len(calls): {"n": n}}})
        k = kite.Kite("key", "tok", transport=tr, sleep=lambda s: None)
        out = k.quote(["NSE:S%d" % i for i in range(1200)])
        self.assertEqual(len(calls), 3)
        self.assertEqual([v["n"] for v in out.values()], [500, 500, 200])
        self.assertEqual(calls[0][1]["Authorization"], "token key:tok")

    def test_historical_splits_and_403(self):
        seen = []

        def tr(method, url, headers, body):
            seen.append(url)
            return 200, json.dumps({"status": "success", "data": {"candles": [
                ["2026-11-18T09:15:00+0530", 1, 2, 0.5, 1.5, 10]]}})
        k = kite.Kite("key", "tok", transport=tr, sleep=lambda s: None)
        rows = k.historical(738561, "5m", datetime.date(2026, 1, 1), datetime.date(2026, 11, 18))
        self.assertEqual(len(seen), 4)          # 322 days at 100 a request
        self.assertEqual(len(rows), 1)          # same candle every time: de-duplicated
        k2 = kite.Kite("key", "tok", transport=lambda *a: (403, '{"status":"error","error_type":"TokenException"}'),
                       sleep=lambda s: None)
        with self.assertRaises(kite.KiteError) as e:
            k2.profile()
        self.assertIn("login", str(e.exception))


class InstrumentsAndChain(unittest.TestCase):
    def test_resolve(self):
        inst = instruments()
        d = datetime.date(2026, 11, 18)
        self.assertEqual(inst.resolve("RELIANCE").key, "NSE:RELIANCE")
        self.assertEqual(inst.resolve("NIFTY FUT", today=d).key, "NFO:NIFTY26NOVFUT")
        self.assertEqual(inst.resolve("RELIANCE FUT 2026-12-29", today=d)["lot_size"], 500)
        o = inst.resolve("NIFTY 26000 CE", today=d)
        self.assertEqual((o["expiry"], o["lot_size"]), ("2026-11-23", 65))
        self.assertEqual(inst.spot_key("NIFTY"), "NSE:NIFTY 50")
        self.assertEqual(kite.segment_of(inst.get("NSE:NIFTY 50")), "index")

    def test_iv_round_trip_and_chain(self):
        T = 5 / 365.0
        p = kite.bs_price(26000, 26100, T, 0.065, 0.14, "CE")
        self.assertAlmostEqual(kite.implied_vol(p, 26000, 26100, T, 0.065, "CE"), 0.14, places=4)
        inst = instruments()
        q = json.load(open(QUOTES))["data"]
        ch = kite.option_chain(inst, "NIFTY", "2026-11-23", q, 25980, at("2026-11-18 10:00"), strikes=4)
        self.assertEqual(ch["atm"], 26000)
        self.assertEqual(len(ch["rows"]), 9)
        self.assertAlmostEqual(ch["rows"][4]["CE"]["iv"], 12.0, delta=0.6)
        self.assertIsNotNone(ch["max_pain"])


class Entry(unittest.TestCase):
    def test_statuses(self):
        c = candles()
        s = lambda t, **kw: entry.evaluate(c, at(t), "NSE:RELIANCE", **kw)["status"]
        self.assertEqual(s("2026-11-18 09:10"), "pre-open")
        self.assertEqual(s("2026-11-18 09:25"), "forming")
        self.assertEqual(s("2026-11-18 09:44"), "waiting")
        self.assertEqual(s("2026-11-24 10:00"), "closed")
        r = entry.evaluate(c, at("2026-11-18 09:52"), "NSE:RELIANCE")
        self.assertEqual((r["status"], r["entry"], r["entry_time"]), ("triggered", 1512.4, "09:45"))
        self.assertEqual((r["or_high"], r["or_low"]), (1510.0, 1498.0))
        self.assertEqual(s("2026-11-18 09:52", side="sell"), "waiting")
        self.assertEqual(s("2026-11-18 09:52", setup="level", level=1520.0), "waiting")
        r = entry.evaluate(c, at("2026-11-18 09:52"), "NSE:RELIANCE", min_volume_x=3.0)
        self.assertNotEqual(r["status"], "triggered")


class Exit(unittest.TestCase):
    def test_default_plan(self):
        p = reliance_plan()
        self.assertEqual((p["stop"], p["target"], p["risk_per_unit"], p["reward_risk"]), (1504.0, 1529.2, 8.4, 2.0))
        self.assertEqual(p["flat_by"], "15:00")
        self.assertEqual(p["product"], "MIS")

    def test_rules_and_refusals(self):
        c = exitp.compute
        self.assertEqual(c(100, "buy", ("pct", 1.0), ("r", 2), 0.05)[:2], (99.0, 102.0))
        self.assertEqual(c(100, "sell", ("price", 101.03), ("r", 1), 0.05)[:2], (101.05, 98.95))
        with self.assertRaises(kite.KiteError):
            c(100, "buy", ("price", 100.5), ("r", 2), 0.05)       # stop above entry
        with self.assertRaises(kite.KiteError):
            c(100, "buy", ("price", 99.95), ("r", 2), 0.05)       # inside the spread
        with self.assertRaises(kite.KiteError):
            c(100, "buy", ("pct", 1.0), ("price", 100.5), 0.05, min_rr=1.0)
        self.assertEqual(exitp.parse_rule("atr:1.5", "stop"), ("atr", 1.5))
        with self.assertRaises(kite.KiteError):
            exitp.parse_rule("atr:1", "target")

    def test_atr_ignores_the_overnight_gap(self):
        day1 = [{"h": 101, "l": 99, "c": 100}] * 10
        day2 = [{"h": 151, "l": 149, "c": 150}] * 10
        self.assertAlmostEqual(exitp.atr([day1, day2], n=14), 2.0)


class TimeMachine(unittest.TestCase):
    def test_seeded_numbers(self):
        plan = tm.normalize_plan(dict(reliance_plan(), tick_size=0.1))
        doc = tm.time_machine(candles(), plan, at("2026-11-18 09:52"), replays=2000)
        m, h = doc["mc"], doc["history"]
        self.assertEqual(m["pool_sessions"], 60)
        self.assertEqual(m["until"], "15:00")
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=3)
        again = tm.time_machine(candles(), plan, at("2026-11-18 09:52"), replays=2000)["mc"]
        self.assertEqual(m["p_target"], again["p_target"])
        self.assertEqual(h["rule"]["stop_rule"], "or_mid")
        self.assertGreater(h["triggered"], 0)

    def test_refuses_short_history(self):
        plan = tm.normalize_plan(reliance_plan())
        few = [b for b in candles() if b["t"].date() >= datetime.date(2026, 11, 1)]
        with self.assertRaises(kite.KiteError):
            tm.time_machine(few, plan, at("2026-11-18 09:52"), replays=100)


class Budget(unittest.TestCase):
    CFG = {"capital": 300000, "max_loss_per_trade": 2500, "max_lots": {"index": 3, "stock": 1}}

    def test_equity_mis(self):
        t = size.size_trade(reliance_plan(), self.CFG)
        self.assertEqual((t["quantity"], t["decided_by"]), (297, "max_loss_per_trade"))
        roles = [o["role"] for o in t["orders"]]
        self.assertEqual(roles, ["entry", "stop", "target", "flat_by"])
        sl = t["orders"][1]
        self.assertEqual((sl["order_type"], sl["trigger_price"], sl["transaction_type"]), ("SL", 1504.0, "SELL"))
        self.assertLess(sl["price"], 1504.0)

    def test_refuses_when_one_lot_is_too_much(self):
        with self.assertRaises(kite.KiteError) as e:
            size.size_trade(reliance_plan(), dict(self.CFG, max_loss_per_trade=5))
        self.assertIn("no smaller order", str(e.exception))

    def test_future_nrml_uses_gtt_and_margin_limits(self):
        p = {"strategy": "single", "key": "NFO:NIFTY26NOVFUT", "exchange": "NFO", "tradingsymbol": "NIFTY26NOVFUT",
             "segment": "future", "lot_size": 65, "tick_size": 0.1, "product": "NRML", "side": "sell",
             "entry": 25950.0, "stop": 25985.0, "target": 25880.0, "risk_per_unit": 35.0, "reward_per_unit": 70.0,
             "reward_risk": 2.0, "flat_by": None, "setup": "orb"}
        t = size.size_trade(p, dict(self.CFG, max_loss_per_trade=5000))
        self.assertEqual(t["lots"], 1)
        self.assertEqual(t["decided_by"], "margin")
        self.assertEqual([o.get("kind") for o in t["orders"]], [None, "gtt"])

    def test_strategy_buys_first(self):
        plan = self._spread()
        t = size.size_trade(plan, dict(self.CFG, max_loss_per_trade=10000))
        legs = [o["transaction_type"] for o in t["orders"] if o.get("role") == "leg"]
        self.assertEqual(legs, sorted(legs))       # BUY before SELL

    def _spread(self):
        a = types.SimpleNamespace(name="NIFTY", expiry=None, legs=None, strategy="bull-put-spread", otm=None, width=2,
                                  short_delta=None, stop=50.0, target="1R", stop_x=2.0, capture=50.0, product=None,
                                  flat_by=None, force_naked=False)
        return options.build(a, instruments(), json.load(open(QUOTES))["data"], 25980.0, at("2026-11-18 10:00"), {})


class Options(unittest.TestCase):
    def build(self, **kw):
        a = types.SimpleNamespace(name="NIFTY", expiry=None, legs=None, strategy="bull-put-spread", otm=None,
                                  width=2, short_delta=None, stop=50.0, target="1R", stop_x=2.0, capture=50.0,
                                  product=None, flat_by=None, force_naked=False)
        for k, v in kw.items():
            setattr(a, k, v)
        return options.build(a, instruments(), json.load(open(QUOTES))["data"], 25980.0, at("2026-11-18 10:00"),
                             kw.pop("cfg", {}) if "cfg" in kw else {})

    def test_credit_spread_math(self):
        p = self.build()
        sell = [l for l in p["legs"] if l["side"] == "SELL"][0]
        buy = [l for l in p["legs"] if l["side"] == "BUY"][0]
        self.assertEqual((sell["strike"], buy["strike"]), (25950.0, 25850.0))
        credit = -p["net_premium"]
        self.assertAlmostEqual(p["worst_case_per_lot"], round((100 - credit) * 65, 2), places=1)
        self.assertAlmostEqual(p["breakevens"][0], 25950 - credit, places=1)
        self.assertTrue(p["defined_risk"])
        self.assertLessEqual(p["stop_value"], credit + (100 - credit) + 1e-9)

    def test_touch_prices(self):
        p = self.build(strategy="long-call")
        q = json.load(open(QUOTES))["data"]["NFO:" + p["legs"][0]["tradingsymbol"]]
        self.assertEqual(p["legs"][0]["price"], q["depth"]["sell"][0]["price"])

    def test_naked_refused_and_condor(self):
        with self.assertRaises(kite.KiteError):
            self.build(strategy="short-straddle")
        p = self.build(strategy="iron-condor", otm=2, width=2)
        self.assertEqual(len(p["legs"]), 4)
        self.assertEqual(len(p["breakevens"]), 2)
        self.assertLess(p["breakevens"][0], 25980)
        self.assertGreater(p["breakevens"][1], 25980)

    def test_custom_legs(self):
        legs = options.parse_legs("BUY 26000 CE, SELL 26100 CE x2")
        self.assertEqual(legs[1], ("SELL", 26100.0, "CE", 2))
        with self.assertRaises(kite.KiteError):
            options.parse_legs("BUY 26000")


class Risk(unittest.TestCase):
    CFG = {"max_loss_per_trade": 2500, "daily_loss_limit": 6000}

    def trade(self, **kw):
        t = size.size_trade(reliance_plan(), {"capital": 300000, "max_loss_per_trade": 2500})
        t.update(kw)
        return t

    def test_pass_and_window(self):
        v, rs, ws = risk.check_trade(self.trade(), self.CFG, at("2026-11-18 09:52"), {}, set())
        self.assertEqual(v, "PASS", rs)
        v, rs, _ = risk.check_trade(self.trade(), self.CFG, at("2026-11-18 14:40"), {}, set())
        self.assertTrue(any("window" in r for r in rs))
        v, rs, _ = risk.check_trade(self.trade(), self.CFG, at("2026-11-24 10:00"), {}, set())
        self.assertTrue(any("closed" in r for r in rs))

    def test_limits(self):
        v, rs, _ = risk.check_trade(self.trade(), dict(self.CFG, max_loss_per_trade=1000), at("2026-11-18 09:52"))
        self.assertTrue(any("max loss" in r for r in rs))
        state = {"log": [{"at": "2026-11-18T09:40:00+05:30", "event": "closed", "trade": "T0", "pnl": -5000}]}
        v, rs, _ = risk.check_trade(self.trade(), self.CFG, at("2026-11-18 09:52"), state, set())
        self.assertTrue(any("daily loss" in r for r in rs))
        t = self.trade()
        state = {"log": [{"at": "2026-11-18T09:50:00+05:30", "event": "placed", "trade": "T1",
                          "fingerprint": t["fingerprint"], "max_loss": 100}]}
        v, rs, _ = risk.check_trade(t, dict(self.CFG, daily_loss_limit=99999), at("2026-11-18 09:52"), state, set())
        self.assertTrue(any("already" in r for r in rs))

    def test_fo_rules(self):
        sp = Budget()._spread()
        t = size.size_trade(sp, {"capital": 300000, "max_loss_per_trade": 10000})
        cfg = dict(self.CFG, max_loss_per_trade=10000, daily_loss_limit=20000)
        v, rs, _ = risk.check_trade(t, cfg, at("2026-11-18 10:05"), {}, set())
        self.assertTrue(any("margin number" in r for r in rs))          # selling needs Kite's margin
        t["margin_required"] = 50000.0
        v, rs, _ = risk.check_trade(t, cfg, at("2026-11-18 10:05"), {}, set(), funds=300000)
        self.assertEqual(v, "PASS", rs)
        v, rs, _ = risk.check_trade(dict(t, expiry="2026-11-18"), cfg, at("2026-11-18 10:05"), {}, set(), 300000)
        self.assertTrue(any("expiry day" in r for r in rs))
        v, rs, _ = risk.check_trade(dict(t, defined_risk=False), cfg, at("2026-11-18 10:05"), {}, set(), 300000)
        self.assertTrue(any("naked" in r for r in rs))
        fut = dict(t, segment="future", key="NFO:KAYNES26NOVFUT", underlying=None, strategy="single", side="buy",
                   stop=1, orders=[dict(t["orders"][0], role="entry"), dict(t["orders"][0], role="stop")])
        v, rs, _ = risk.check_trade(fut, cfg, at("2026-11-18 10:05"), {}, {"KAYNES"}, 300000)
        self.assertTrue(any("ban" in r for r in rs))

    def test_exits_always_pass_day_limits(self):
        t = {"id": "X1", "role": "exit", "exit_only": True, "segment": "equity", "key": "NSE:RELIANCE",
             "lot_size": 1, "orders": [{"step": 1, "exchange": "NSE", "tradingsymbol": "RELIANCE",
                                        "transaction_type": "SELL", "quantity": 297, "product": "MIS",
                                        "order_type": "MARKET"}]}
        v, rs, _ = risk.check_trade(t, {"daily_loss_limit": 1}, at("2026-11-18 15:20"))
        self.assertEqual(v, "PASS", rs)

    def test_ban_file(self):
        self.assertEqual(risk.ban_list(at("2026-11-18 09:00"), text="Securities in Ban For Trade Date 18-NOV-2026:\n"
                                                                  "1,KAYNES\n2,IDEA\n"), {"KAYNES", "IDEA"})


class Broker(unittest.TestCase):
    def test_ticket_flow(self):
        t = size.size_trade(reliance_plan(), {"capital": 300000, "max_loss_per_trade": 2500})
        today = "2026-11-18"
        st, rv = ticket.status_of(t, {}, {}, today)
        self.assertEqual(st, "NOT CHECKED")
        riskdoc = {"verdicts": [{"date": today, "fingerprint": t["fingerprint"], "verdict": "PASS"}]}
        st, rv = ticket.status_of(t, riskdoc, {}, today)
        self.assertEqual(st, "READY")
        text = ticket.render(t, 1, 1, st, rv)
        self.assertIn("Enter these in Kite yourself", text)
        self.assertIn("SL (stop-limit), trigger 1504.00", text)
        state = {"log": [{"at": today + "T09:53:00+05:30", "event": "placed", "fingerprint": t["fingerprint"]}]}
        self.assertEqual(ticket.status_of(t, riskdoc, state, today)[0], "PLACED")
        blocked = {"verdicts": [{"date": today, "fingerprint": t["fingerprint"], "verdict": "BLOCK",
                                 "reasons": ["x"]}]}
        self.assertNotIn("Enter these in Kite", ticket.render(t, 1, 1, "BLOCKED", blocked["verdicts"][0]))


class Alerts(unittest.TestCase):
    def test_plan_alerts_ring_once(self):
        rules = alerts.rules_from_plan(reliance_plan())
        for i, r in enumerate(rules):
            r["id"] = str(i)
        q = {"NSE:RELIANCE": {"last": 1503.5, "prev_close": 1490}}
        state = {}
        rung = alerts.evaluate(rules, q, at("2026-11-18 10:20"), state)
        self.assertEqual([r[1]["type"] for r in rung], ["stop"])
        state[rung[0][0]] = "2026-11-18"
        self.assertEqual(alerts.evaluate(rules, q, at("2026-11-18 10:25"), state), [])
        self.assertEqual([r[1]["type"] for r in alerts.evaluate(rules, q, at("2026-11-18 15:01"), state)], ["time"])
        self.assertEqual(alerts.evaluate(rules, q, at("2026-11-24 10:20"), {}), [])   # holiday

    def test_strategy_net_value(self):
        sp = Budget()._spread()
        rules = alerts.rules_from_plan(sp)
        legs = rules[0]["legs"]
        ltp = {l["key"]: 0 for l in legs}
        short = [l for l in legs if l["side"] == "SELL"][0]["key"]
        ltp[short] = 500.0
        q = {k: {"last": v, "prev_close": v} for k, v in ltp.items()}
        rung = alerts.evaluate(rules, q, at("2026-11-18 11:00"), {})
        self.assertEqual(rung[0][1]["type"], "strategy_stop")


class Scanner(unittest.TestCase):
    def test_setups(self):
        up = [100 + i * 0.1 for i in range(40)] + [110.0]
        self.assertIn("breakout_20d", scan.find_setups(up)[0])
        down = [100 - i * 0.1 for i in range(40)] + [90.0]
        self.assertIn("breakdown_20d", scan.find_setups(down)[0])
        self.assertEqual(scan.classify_buildup(1.2, 6), "long buildup")
        self.assertEqual(scan.classify_buildup(-1.2, 6), "short buildup")
        self.assertEqual(scan.classify_buildup(1.2, -6), "short covering")
        self.assertEqual(scan.classify_buildup(-1.2, -6), "long unwinding")

    def test_orb(self):
        bars = kite.session_bars(candles(), "2026-11-18", 5, at("2026-11-18 10:15"))
        r = scan.orb_hit(bars, 3, kite.hhmm("10:15"))
        self.assertEqual((r["side"], r["time"], r["close"]), ("buy", "09:45", 1512.4))


class NewsDeals(unittest.TestCase):
    RSS = ("<rss><channel><item><title>Reliance Q2 results: net profit rises 9% - Moneycontrol</title>"
           "<link>https://x</link><pubDate>Wed, 18 Nov 2026 04:30:00 GMT</pubDate>"
           "<source url='https://m'>Moneycontrol</source></item>"
           "<item><title>Reliance Q2 results: net profit rises 9% - Economic Times</title><link>https://y</link>"
           "<pubDate>Wed, 18 Nov 2026 04:40:00 GMT</pubDate></item></channel></rss>")
    CSV = ("Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price\n"
           "17-NOV-2026,ALKEM,Alkem Laboratories Ltd.,DSP MUTUAL FUND,BUY,160265,5050.00\n"
           "17-NOV-2026,GROWW,Groww,RIBBIT CAPITAL V L.P.,SELL,48586888,219.00\n")

    def test_rss_tag_dedupe(self):
        items = [news.tag(i, {"RELIANCE": "Reliance"}) for i in news.dedupe(news.parse_rss(self.RSS, "RELIANCE"))]
        self.assertEqual(len(items), 1)
        self.assertIn("results", items[0]["tags"])
        self.assertTrue(items[0]["time"].startswith("2026-11-18T10:00"))

    def test_deals(self):
        rows = deals.parse(self.CSV, "block")
        self.assertEqual(rows[0]["symbol"], "GROWW")
        self.assertAlmostEqual(rows[0]["value_cr"], 1064.05, places=2)
        d = news.parse_deals(self.CSV, "block", {"ALKEM"})
        self.assertEqual(len(d), 1)
        self.assertEqual(deals.parse_ban("Securities in Ban For Trade Date 18-NOV-2026:\n1,KAYNES\n"),
                         ("18-NOV-2026", ["KAYNES"]))


class AnalystIdeas(unittest.TestCase):
    def test_growth(self):
        doc = {"symbol": "X", "basis": "consolidated", "quarters": {}}
        for q, rev, pat in (("Q2FY26", 1000, 100), ("Q3FY26", 1050, 105), ("Q4FY26", 1080, 110),
                            ("Q1FY27", 1100, 118), ("Q2FY27", 1150, 140)):
            doc["quarters"][q] = {"quarter": q, "revenue": rev, "pat": pat, "eps": pat / 20.0, "ebitda": rev / 5.0}
        last = facts.table(doc)[-1]
        self.assertAlmostEqual(last["revenue_yoy"], 15.0)
        self.assertAlmostEqual(last["pat_qoq"], 100 * (140 / 118 - 1))
        self.assertEqual(last["ttm_revenue"], 1050 + 1080 + 1100 + 1150)
        self.assertEqual(facts.period_end(27, 2), "2026-09-30")
        self.assertEqual(facts.period_end(27, 4), "2027-03-31")

    def test_idea_validation(self):
        with self.assertRaises(kite.KiteError):
            ideas.validate({"side": "buy", "entry": 800, "stop": 810, "target": 850})
        r = ideas.validate({"side": "sell", "entry": 840, "stop": 851, "target": 818})
        self.assertEqual(r["reward_risk"], 2.0)


class Repo(unittest.TestCase):
    def test_skill_folders(self):
        folders = sorted(d for d in os.listdir(ROOT) if d.startswith("trader-"))
        self.assertEqual(len(folders), 14)
        self.assertNotIn("trader-pelosi", folders)
        for d in folders:
            text = open(os.path.join(ROOT, d, "SKILL.md"), encoding="utf-8").read()
            m = re.search(r"^name:\s*(\S+)", text, re.M)
            self.assertEqual(m.group(1), d)
            self.assertIn("not investment advice", " ".join(text.lower().split()))

    def test_no_script_calls_an_order_endpoint(self):
        for d in os.listdir(ROOT):
            if not d.startswith("trader-"):
                continue
            for f in os.listdir(os.path.join(ROOT, d)):
                if f.endswith(".py"):
                    src = open(os.path.join(ROOT, d, f), encoding="utf-8").read()
                    self.assertNotRegex(src, r"request\(\s*\"(POST|PUT|DELETE)\",\s*\"/(orders|gtt)", f)
                    self.assertNotIn("alpaca", src.lower(), f)


if __name__ == "__main__":
    unittest.main()
