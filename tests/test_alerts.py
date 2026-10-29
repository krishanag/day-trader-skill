import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

alerts = load("trader-alerts", "alerts")
kite = alerts.kite   # the module alerts.py imported, so the network fakes reach it
nse = alerts.nse

QDIR = os.path.join(FIX, "trader-alerts")
PLAN = os.path.join(FIX, "trader-broker", "plan.json")
NOW = "2026-10-29 10:15"
# the saved Kite quotes: last, previous close (ohlc.close), last trade time; read 2026-10-29 after the close
QUOTES = {"RELIANCE": (1167.4, 1158.0, "2026-10-29T15:59:57+05:30"),
          "INFY": (1146.5, 1123.15, "2026-10-29T15:59:53+05:30"),
          "TCS": (2478.0, 2460.0, "2026-10-29T15:58:52+05:30"),
          "SBIN": (971.9, 956.8, "2026-10-29T15:59:38+05:30"),
          "TATASTEEL": (169.46, 169.27, "2026-10-29T15:59:41+05:30")}


def ist(text):
    return alerts.parse_now(text)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = alerts.main(argv)
    return code, out.getvalue(), err.getvalue()


def plan_rules():
    with open(PLAN) as fh:
        return alerts.rules_from_plan(json.load(fh))


class FakeResponse(io.BytesIO):
    def __init__(self, body, ctype="application/json"):
        super().__init__(body.encode())
        self.headers = {"Content-Type": ctype}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, etype, message):
    body = io.BytesIO(json.dumps({"status": "error", "error_type": etype, "message": message}).encode())
    return urllib.error.HTTPError("https://api.kite.trade/quote", code, message, {}, body)


class FakeNet:
    """Answers Kite's /quote from the saved quote files; `codes` queues errors first."""

    def __init__(self, codes=()):
        self.codes, self.requests = list(codes), []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if self.codes:
            code = self.codes.pop(0)
            if code != 200:
                raise http_error(code, "NetworkException" if code == 429 else "TokenException", "Kite said %d" % code)
        data = {}
        for key in req.full_url.split("?", 1)[1].split("&"):
            if key.startswith("i=NSE%3A"):
                sym = key[len("i=NSE%3A"):]
                path = os.path.join(QDIR, "kite-quote-%s.json" % sym)
                if os.path.exists(path):
                    with open(path) as fh:
                        data["NSE:" + sym] = json.load(fh)
        return FakeResponse(json.dumps({"status": "success", "data": data}))


class Calendar(unittest.TestCase):
    def test_hours_and_holidays_come_from_nse_only(self):
        for name in ("NSE_HOLIDAYS", "HOLIDAYS", "OPEN", "CLOSE"):
            self.assertFalse(hasattr(alerts, name), name)
        self.assertEqual(nse.NSE_HOLIDAYS["2026-10-20"], "Dussehra")
        self.assertEqual(alerts.flat_by_time({}), nse.DEFAULT_FLAT_BY)
        self.assertEqual(alerts.flat_by_time({"flat_by": "15:00"}), datetime.time(15, 0))
        self.assertEqual(alerts.flat_by_time({"flat_by": "15:00"}, {"flat_by": "14:50"}), datetime.time(14, 50))
        with self.assertRaises(alerts.AlertError):
            alerts.flat_by_time({"flat_by": "soon"})

    def test_parse_now_is_ist(self):
        self.assertEqual(ist(NOW).isoformat(), "2026-10-29T10:15:00+05:30")
        self.assertEqual(ist("2026-10-29T04:45:00+00:00").isoformat(), "2026-10-29T10:15:00+05:30")
        self.assertEqual(ist("2026-10-29T10:15:00+05:30").tzinfo.utcoffset(None), datetime.timedelta(hours=5, minutes=30))
        with self.assertRaises(alerts.AlertError):
            ist("tomorrow")

    def test_the_clock_through_the_day(self):
        st = lambda t: alerts.market_status(ist(t))
        self.assertEqual((st("2026-10-29 08:59")["phase"], st("2026-10-29 08:59")["open"]), ("closed", False))
        self.assertEqual(st("2026-10-29 08:59")["reason"], "before the pre-open")
        self.assertEqual(st("2026-10-29 08:59")["next_open"], "2026-10-29 09:15")
        self.assertEqual(st("2026-10-29 09:00")["phase"], "pre-open")
        self.assertEqual(st("2026-10-29 09:14")["open"], False)
        s = st("2026-10-29 09:15")
        self.assertEqual((s["phase"], s["open"], s["reason"]), ("open", True, "continuous trading"))
        self.assertEqual(st("2026-10-29 15:14")["open"], True)
        s = st("2026-10-29 15:15")
        self.assertEqual((s["phase"], s["open"]), ("after-close", True))
        self.assertIn("closing auction", s["reason"])
        self.assertEqual(st("2026-10-29 15:29")["open"], True)
        s = st("2026-10-29 15:30")
        self.assertEqual((s["phase"], s["open"], s["reason"], s["next_open"]),
                         ("after-close", False, "after the close", "2026-10-30 09:15"))
        self.assertEqual((s["opens"], s["continuous_end_cas"], s["continuous_end"], s["square_off_cas"], s["flat_by"]),
                         ("09:15", "15:15", "15:30", "15:12", "15:05"))
        sat = st("2026-10-31 10:00")
        self.assertEqual((sat["trading_day"], sat["reason"], sat["next_open"]), (False, "weekend", "2026-11-02 09:15"))
        self.assertIn("Muhurat", st("2026-11-08 18:30")["reason"])
        dus = st("2026-10-20 10:00")
        self.assertEqual((dus["open"], dus["reason"], dus["next_open"]), (False, "NSE holiday: Dussehra", "2026-10-21 09:15"))
        self.assertEqual(st("2026-11-10 10:00")["reason"], "NSE holiday: Diwali Balipratipada")
        self.assertTrue(st("2026-12-24 10:00")["open"])
        self.assertFalse(st("2026-12-25 10:00")["open"])
        self.assertTrue(st("2026-10-29 10:00")["calendar_known"])
        self.assertFalse(st("2027-01-04 10:00")["calendar_known"])
        self.assertEqual(alerts.market_status(ist(NOW), datetime.time(14, 50))["flat_by"], "14:50")


class Quotes(unittest.TestCase):
    def test_kite_quote_fixtures(self):
        now = ist("2026-10-29 18:00")
        for sym, (last, prev, when) in QUOTES.items():
            with open(os.path.join(QDIR, "kite-quote-%s.json" % sym)) as fh:
                q = alerts.parse_quote(sym, json.load(fh), now)
            self.assertEqual((q["last"], q["prev_close"], q["time"], q["source"]), (last, prev, when, "kite"), sym)
        with open(os.path.join(QDIR, "kite-quote-RELIANCE.json")) as fh:
            q = alerts.parse_quote("RELIANCE", json.load(fh), now)
        self.assertEqual((q["open"], q["high"], q["low"]), (1159.0, 1182.1, 1155.6))
        with self.assertRaises(alerts.AlertError):
            alerts.parse_quote("ZZZZ", {}, now)
        self.assertEqual(alerts.parse_quote("X", {"last_price": 10, "ohlc": {"close": 0}}, now)["prev_close"], None)
        self.assertEqual(alerts.parse_quote("X", {"last_price": 10}, now)["time"], "2026-10-29T18:00:00+05:30")

    def test_offline_quotes_and_errors(self):
        q, errors = alerts.get_quotes(["RELIANCE", "reliance.ns", "NSE:INFY", "ZZZZ"], ist(NOW), QDIR)
        self.assertEqual(sorted(q), ["INFY", "RELIANCE"])
        self.assertEqual(q["INFY"]["last"], 1146.5)
        self.assertEqual(len(errors), 1)
        self.assertIn("no offline quote file", errors[0])
        self.assertIn("kite-quote-ZZZZ.json", errors[0])

    def test_write_prices_is_the_contract_shape_and_merges(self):
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "prices.json")
            q, _ = alerts.get_quotes(["RELIANCE"], ist("2026-10-29 10:21"), QDIR)
            doc = alerts.write_prices(path, q, ist("2026-10-29 10:21:30"))
            self.assertEqual(doc, {"as_of": "2026-10-29T10:21:30+05:30",
                                   "prices": {"RELIANCE": {"last": 1167.4, "time": "2026-10-29T15:59:57+05:30",
                                                           "prev_close": 1158.0, "source": "kite"}}})
            q, _ = alerts.get_quotes(["TCS"], ist("2026-10-29 10:22"), QDIR)
            alerts.write_prices(path, q, ist("2026-10-29 10:22"))
            with open(path) as fh:
                doc = json.load(fh)
            self.assertEqual(sorted(doc), ["as_of", "prices"])
            self.assertEqual(sorted(doc["prices"]), ["RELIANCE", "TCS"])
            self.assertEqual(doc["prices"]["TCS"]["prev_close"], 2460.0)
        finally:
            shutil.rmtree(d)


class Network(unittest.TestCase):
    def setUp(self):
        self.saved = (kite._urlopen, kite._sleep, kite.load_session, dict(os.environ))
        kite._sleep = lambda s: None
        kite.load_session = lambda now, path=None: {"access_token": "tok", "user_id": "AB1234"}
        kite._last_call.clear()
        os.environ["KITE_API_KEY"] = "key"

    def tearDown(self):
        kite._urlopen, kite._sleep, kite.load_session, env = self.saved
        kite._last_call.clear()
        os.environ.clear()
        os.environ.update(env)

    def net(self, codes=()):
        f = FakeNet(codes)
        kite._urlopen = f
        return f

    def test_one_kite_call_for_every_symbol_with_the_session(self):
        f = self.net()
        q, errors = alerts.get_quotes(["RELIANCE", "infy", "ZZZZ"], ist(NOW))
        self.assertEqual((sorted(q), q["RELIANCE"]["last"], q["INFY"]["prev_close"]), (["INFY", "RELIANCE"], 1167.4, 1123.15))
        self.assertEqual(errors, ["Kite has no quote for ZZZZ (not an NSE equity symbol?)"])
        self.assertEqual(len(f.requests), 1)
        req = f.requests[0]
        self.assertIn("/quote?i=NSE%3ARELIANCE&i=NSE%3AINFY&i=NSE%3AZZZZ", req.full_url)
        self.assertEqual(req.get_header("Authorization"), "token key:tok")
        self.assertEqual(req.get_header("X-kite-version"), "3")

    def test_a_429_is_retried_by_the_shared_client(self):
        f = self.net([429, 200])
        q, errors = alerts.get_quotes(["SBIN"], ist(NOW))
        self.assertEqual((q["SBIN"]["last"], errors, len(f.requests)), (971.9, [], 2))

    def test_no_session_means_exit_1_and_no_other_source(self):
        self.net()
        kite.load_session = lambda now, path=None: (_ for _ in ()).throw(
            kite.LoginNeeded("not logged in to Kite: run  python3 trader-kite/login.py"))
        d = tempfile.mkdtemp()
        try:
            prices = os.path.join(d, "prices.json")
            code, text, err = run(["quote", "RELIANCE", "--write", prices, "--now", NOW])
            self.assertEqual(code, 1)
            self.assertIn("not logged in to Kite: run  python3 trader-kite/login.py", err)
            self.assertNotIn("Traceback", err)
            self.assertFalse(os.path.exists(prices))
            rules = os.path.join(d, "alerts.json")
            run(["add", "--rules", rules, "--symbol", "RELIANCE", "--above", "1160"])
            code, _, err = run(["check", "--rules", rules, "--state", os.path.join(d, "s.json"), "--now", NOW])
            self.assertEqual(code, 1)
            self.assertIn("login.py", err)
        finally:
            shutil.rmtree(d)

    def test_a_rejected_token_asks_for_login(self):
        self.net([403])
        code, _, err = run(["quote", "RELIANCE", "--now", NOW])
        self.assertEqual(code, 1)
        self.assertIn("Kite rejected the session", err)


class Rules(unittest.TestCase):
    def setUp(self):
        self.q, _ = alerts.get_quotes(list(QUOTES), ist(NOW), QDIR)
        self.now = ist(NOW)
        self.open = alerts.market_status(self.now)

    def at(self, text, rules, state=None, quotes=None):
        now = ist(text)
        return alerts.evaluate(rules, self.q if quotes is None else quotes, alerts.market_status(now), state or {}, now)

    def test_a_target_rings_once(self):
        rules = [{"id": "rel-1160", "symbol": "RELIANCE", "type": "above", "price": 1160}]
        rings, st = alerts.evaluate(rules, self.q, self.open, {}, self.now)
        self.assertEqual(len(rings), 1)
        self.assertEqual(rings[0]["message"], "RELIANCE is Rs 1,167.40: at or above your alert level Rs 1,160.00.")
        self.assertEqual(st["rung"]["rel-1160"], "2026-10-29T10:15:00+05:30")
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(rings, [])
        closed = alerts.market_status(ist("2026-10-29 18:00"))
        rings, _ = alerts.evaluate(rules, self.q, closed, {}, ist("2026-10-29 18:00"))
        self.assertIn("RELIANCE last Rs 1,167.40", rings[0]["message"])

    def test_stop_and_rearm(self):
        rules = [{"id": "sbin-stop", "symbol": "SBIN", "type": "stop", "price": 975, "rearm": True}]
        rings, st = alerts.evaluate(rules, self.q, self.open, {}, self.now)
        self.assertIn("SBIN is Rs 971.90: at or below your stop Rs 975.00", rings[0]["message"])
        self.q["SBIN"]["last"] = 976.0
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(rings, [])
        self.assertNotIn("sbin-stop", st["rung"])
        self.q["SBIN"]["last"] = 971.0
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(len(rings), 1)
        no_rearm = [{"id": "sbin-stop", "symbol": "SBIN", "type": "stop", "price": 975}]
        self.q["SBIN"]["last"] = 976.0
        self.assertIn("sbin-stop", alerts.evaluate(no_rearm, self.q, self.open, st, self.now)[1]["rung"])

    def test_pct_from_kites_previous_close(self):
        big = [{"id": "infy-3", "symbol": "INFY", "type": "pct", "pct": 3}]
        self.assertEqual(alerts.evaluate(big, self.q, self.open, {}, self.now)[0], [])
        up = [{"id": "infy-2", "symbol": "INFY", "type": "pct", "pct": 2, "direction": "up"}]
        rings, st = alerts.evaluate(up, self.q, self.open, {}, self.now)
        self.assertEqual(rings[0]["message"], "INFY moved +2.08% to Rs 1,146.50 (from the previous close Rs 1,123.15).")
        self.assertEqual(alerts.evaluate(up, self.q, self.open, st, self.now)[0], [])          # once a day
        self.assertEqual(len(self.at("2026-10-30 10:00", up, st)[0]), 1)                         # again tomorrow
        down = [{"id": "infy-down", "symbol": "INFY", "type": "pct", "pct": 2, "direction": "down"}]
        self.assertEqual(alerts.evaluate(down, self.q, self.open, {}, self.now)[0], [])
        ref = [{"id": "sbin-ref", "symbol": "SBIN", "type": "pct", "pct": 1, "ref": 960}]
        rings, _ = alerts.evaluate(ref, self.q, self.open, {}, self.now)
        self.assertEqual(rings[0]["message"], "SBIN moved +1.24% to Rs 971.90 (from your ref Rs 960.00).")
        for sym, pct in (("RELIANCE", "+0.81%"), ("SBIN", "+1.58%"), ("TCS", "+0.73%"), ("TATASTEEL", "+0.11%")):
            rings, _ = alerts.evaluate([{"id": "x", "symbol": sym, "type": "pct", "pct": 0.1}], self.q, self.open, {}, self.now)
            self.assertIn(pct, rings[0]["message"], sym)

    def test_the_bell_once_per_trading_day(self):
        rules = [{"id": "bell", "type": "bell"}]
        self.assertEqual(self.at("2026-10-29 09:14", rules)[0], [])
        rings, st = self.at("2026-10-29 09:15", rules)
        self.assertEqual(rings[0]["message"], "Opening bell: NSE is open (2026-10-29 09:15 IST). F&O stocks trade to "
                                              "15:15, the rest to 15:30; flat by 15:05.")
        self.assertEqual(self.at("2026-10-29 11:00", rules, st)[0], [])
        self.assertEqual(self.at("2026-10-29 15:20", rules)[0], [])           # the auction is not a bell
        self.assertEqual(len(self.at("2026-10-30 09:16", rules, st)[0]), 1)
        self.assertEqual(self.at("2026-10-31 09:16", rules)[0], [])           # Saturday
        self.assertEqual(self.at("2026-11-10 09:16", rules)[0], [])           # Diwali Balipratipada

    def test_the_plan_gives_a_stop_a_target_and_the_flat_by(self):
        rules = plan_rules()
        self.assertEqual([(r["id"], r["type"]) for r in rules],
                         [("reliance-plan-stop", "stop"), ("reliance-plan-target", "target"), ("reliance-flat-by", "flat_by")])
        self.assertEqual((rules[0]["price"], rules[1]["price"]), (1163.0, 1185.8))
        self.assertEqual((rules[2]["at"], rules[2]["label"], rules[2]["symbol"]), ("15:05", "flat by", "RELIANCE"))
        self.assertTrue(all(r["date"] == "2026-10-29" for r in rules))
        rings, st = self.at("2026-10-29 10:52", rules)                      # the real quote, 1167.40: in between
        self.assertEqual(rings, [])
        q = json.loads(json.dumps(self.q))
        q["RELIANCE"]["last"] = 1162.9
        rings, st = self.at("2026-10-29 11:30", rules, st, q)
        self.assertEqual([r["id"] for r in rings], ["reliance-plan-stop"])
        self.assertEqual(rings[0]["message"], "RELIANCE is Rs 1,162.90: at or below your stop Rs 1,163.00 "
                                              "(plan stop: opening-range midpoint).")
        q["RELIANCE"]["last"] = 1185.8
        rings, _ = self.at("2026-10-29 12:00", rules, st, q)
        self.assertEqual([r["id"] for r in rings], ["reliance-plan-target"])
        self.assertIn("(plan target: 1R (one times the risk))", rings[0]["message"])
        self.assertEqual(self.at("2026-10-30 12:00", rules, {}, q)[0], [])    # the plan's day only
        other = alerts.rules_from_plan({"symbol": "infy.ns", "stop": 1120, "target": 1150}, "14:50")
        self.assertEqual((other[2]["at"], other[0]["symbol"], "date" in other[0]), ("14:50", "INFY", False))
        self.assertEqual(alerts.rules_from_plan({"symbol": "INFY", "stop": 1, "target": 2, "flat_by": "15:00"}, "14:50")[2]["at"],
                         "15:00")

    def test_flat_by_rings_once_per_trading_day(self):
        rules = plan_rules()[2:]
        self.assertEqual(self.at("2026-10-29 15:04", rules)[0], [])
        rings, st = self.at("2026-10-29 15:05", rules)
        self.assertEqual(len(rings), 1)
        self.assertEqual(rings[0]["message"], "Flat by 15:05: it is 15:05 IST. Zerodha squares off MIS in F&O stocks at "
                                              "15:12 (Rs 50 + GST each time); continuous trading ends 15:15 for them, 15:30 "
                                              "for the rest. If RELIANCE is still open, close it: trader-broker ticket.py "
                                              "exit RELIANCE builds the exit ticket, then trader-risk and your yes.")
        self.assertEqual(st["rung"]["reliance-flat-by"], "2026-10-29T15:05:00+05:30")
        self.assertEqual(self.at("2026-10-29 15:11", rules, st)[0], [])
        self.assertEqual(len(self.at("2026-10-29 15:40", rules)[0]), 1)     # late is still once
        self.assertEqual(self.at("2026-10-30 15:06", rules, st)[0], [])      # plan rules: their day only
        undated = [{"id": "flat", "type": "flat_by", "at": "15:05"}]
        rings, _ = self.at("2026-10-30 15:05", undated)
        self.assertNotIn("If", rings[0]["message"])
        self.assertEqual(self.at("2026-10-31 15:06", undated)[0], [])        # Saturday
        self.assertEqual(self.at("2026-10-20 15:06", undated)[0], [])        # Dussehra

    def test_a_time_alert(self):
        rule = [{"id": "t", "type": "time", "at": "14:30", "label": "last entry window closes", "symbol": "TCS"}]
        self.assertEqual(self.at("2026-10-29 14:29", rule)[0], [])
        rings, st = self.at("2026-10-29 14:30", rule)
        self.assertEqual(rings[0]["message"], "Last entry window closes: it is 14:30 IST (set for 14:30), NSE is open. "
                                              "If TCS is still open, close it: trader-broker ticket.py exit TCS builds the "
                                              "exit ticket, then trader-risk and your yes.")
        self.assertEqual(st["rung"]["t"], "2026-10-29T14:30:00+05:30")
        self.assertEqual(self.at("2026-10-29 14:45", rule, st)[0], [])
        rings, _ = self.at("2026-10-29 16:00", rule)
        self.assertIn("NSE is after-close", rings[0]["message"])
        self.assertEqual(self.at("2026-10-31 14:31", rule)[0], [])          # Saturday
        self.assertEqual(self.at("2026-10-20 14:31", rule)[0], [])          # Dussehra

    def test_the_example_rules_are_valid(self):
        with open(os.path.join(ROOT, "trader-alerts", "alerts.example.json")) as fh:
            rules = json.load(fh)["rules"]
        for r in rules:
            alerts.validate(r)
        self.assertEqual([r for r in rules if r["id"].startswith("reliance-")],
                         [dict(r, date="2026-10-29") for r in plan_rules()])
        self.assertEqual(sorted(r["type"] for r in rules), ["bell", "flat_by", "pct", "stop", "target", "time"])

    def test_bad_rules(self):
        for bad in ({"id": "x", "type": "moon"}, {"id": "x", "type": "above", "symbol": "INFY"},
                    {"id": "x", "type": "above", "price": 1}, {"id": "x", "type": "time", "at": "half four"},
                    {"id": "x", "type": "flat_by"}, {"id": "x", "type": "pct", "symbol": "INFY", "pct": "3"},
                    {"id": "x", "type": "bell", "date": "29/10/2026"}):
            with self.assertRaises(alerts.AlertError, msg=json.dumps(bad)):
                alerts.evaluate([bad], {}, self.open, {}, self.now)
        with self.assertRaises(alerts.AlertError):
            alerts.rules_from_plan({"symbol": "RELIANCE", "stop": 1163.0})
        with self.assertRaises(alerts.AlertError):
            alerts.rules_from_plan({})


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.rules = os.path.join(self.d, "alerts.json")
        self.state = os.path.join(self.d, "state", "alerts.json")
        self.prices = os.path.join(self.d, "prices.json")
        self.none = os.path.join(self.d, "none")

    def tearDown(self):
        shutil.rmtree(self.d)

    def check(self, now, cfg=None):
        return run(["check", "--rules", self.rules, "--state", self.state, "--prices-out", self.prices,
                    "--offline", QDIR, "--now", now, "--config", cfg or self.none])

    def rules_doc(self):
        with open(self.rules) as fh:
            return json.load(fh)["rules"]

    def test_the_reliance_trade_from_the_plan(self):
        code, text, _ = run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state,
                             "--config", self.none])
        self.assertEqual(code, 0)
        self.assertIn("Added reliance-plan-stop", text)
        self.assertIn("Added reliance-flat-by", text)
        self.assertIn("15:12 square-off", text)
        self.assertEqual(run(["add", "--rules", self.rules, "--bell"])[0], 0)

        code, text, _ = self.check("2026-10-29 09:40")
        self.assertEqual(code, 0)                                      # the bell rings first
        self.assertIn("ALERT Opening bell", text)
        code, text, _ = self.check("2026-10-29 09:41")
        self.assertEqual(code, 1)
        self.assertIn("NSE OPEN (2026-10-29 09:41 IST, continuous trading).", text)
        self.assertIn("Nothing hit. 4 rule(s) watched today, RELIANCE Rs 1,167.40.", text)
        with open(self.prices) as fh:
            doc = json.load(fh)
        self.assertEqual(doc, {"as_of": "2026-10-29T09:41:00+05:30",
                               "prices": {"RELIANCE": {"last": 1167.4, "time": "2026-10-29T15:59:57+05:30",
                                                       "prev_close": 1158.0, "source": "kite"}}})

        code, text, _ = self.check("2026-10-29 15:05")
        self.assertEqual(code, 0)
        self.assertIn("ALERT Flat by 15:05: it is 15:05 IST.", text)
        self.assertIn("ticket.py exit RELIANCE", text)
        self.assertIn("not an order", text)
        self.assertEqual(self.check("2026-10-29 15:06")[0], 1)
        code, text, _ = self.check("2026-10-30 10:00")
        self.assertIn("ALERT Opening bell", text)
        code, text, _ = self.check("2026-10-30 10:05")
        self.assertIn("Nothing hit. 1 rule(s) watched today.", text)

        # a new plan replaces the plan rules and re-arms them
        code, text, _ = run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state,
                             "--config", self.none])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(r["id"] for r in self.rules_doc()),
                         ["bell", "reliance-flat-by", "reliance-plan-stop", "reliance-plan-target"])
        with open(self.state) as fh:
            self.assertNotIn("reliance-flat-by", json.load(fh)["rung"])

    def test_the_morning_watchlist(self):
        for args in (["--bell"], ["--symbol", "INFY", "--above", "1140"], ["--symbol", "sbin", "--stop", "950"]):
            code, _, _ = run(["add", "--rules", self.rules] + args)
            self.assertEqual(code, 0)
        code, _, err = run(["add", "--rules", self.rules, "--symbol", "INFY", "--above", "1140"])
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)
        self.assertEqual([r["id"] for r in self.rules_doc()], ["bell", "infy-above-1140", "sbin-stop-950"])

        code, text, _ = self.check("2026-10-29 18:00")
        self.assertEqual(code, 0)
        self.assertIn("NSE AFTER-CLOSE (2026-10-29 18:00 IST, after the close).", text)
        self.assertIn("ALERT INFY last Rs 1,146.50: at or above your alert level Rs 1,140.00.", text)
        with open(self.prices) as fh:
            self.assertEqual(sorted(json.load(fh)["prices"]), ["INFY", "SBIN"])
        code, text, _ = self.check("2026-10-30 09:16")
        self.assertEqual(code, 0)
        self.assertIn("ALERT Opening bell", text)
        self.assertNotIn("INFY last", text)
        self.assertEqual(self.check("2026-10-30 09:45")[0], 1)

        code, text, _ = run(["list", "--rules", self.rules, "--state", self.state])
        self.assertEqual(code, 0)
        self.assertIn("(rang 2026-10-30T09:16:00+05:30)", text)
        self.assertEqual(run(["reset", "bell", "--state", self.state])[0], 0)
        self.assertEqual(run(["reset", "bell", "--state", self.state])[0], 1)
        self.assertEqual(run(["list", "--rules", self.none])[0], 1)

    def test_add_time_flat_by_and_target(self):
        code, text, _ = run(["add", "--rules", self.rules, "--time", "14:30", "--label", "last entry window closes"])
        self.assertEqual(code, 0)
        code, text, _ = run(["add", "--rules", self.rules, "--flat-by", "--symbol", "reliance.ns", "--config", self.none])
        self.assertEqual(code, 0)
        code, text, _ = run(["add", "--rules", self.rules, "--flat-by", "15:00", "--id", "early"])
        self.assertEqual(code, 0)
        cfg = os.path.join(self.d, "config.json")
        with open(cfg, "w") as fh:
            json.dump({"flat_by": "14:50"}, fh)
        code, text, _ = run(["add", "--rules", self.rules, "--flat-by", "--config", cfg])
        self.assertEqual(code, 0)
        code, text, _ = run(["add", "--rules", self.rules, "--symbol", "RELIANCE", "--target", "1185.8",
                             "--date", "2026-10-29", "--note", "1R"])
        self.assertEqual(code, 0)
        code, text, _ = run(["add", "--rules", self.rules, "--symbol", "TCS", "--pct", "3", "--direction", "down",
                             "--rearm"])
        self.assertEqual(code, 0)
        rules = self.rules_doc()
        self.assertEqual(rules[0], {"id": "time-1430", "type": "time", "at": "14:30", "label": "last entry window closes"})
        self.assertEqual(rules[1], {"id": "reliance-flat-by", "type": "flat_by", "at": "15:05", "label": "flat by",
                                    "symbol": "RELIANCE"})
        self.assertEqual((rules[2]["id"], rules[2]["at"]), ("early", "15:00"))
        self.assertEqual((rules[3]["id"], rules[3]["at"]), ("flat-by", "14:50"))
        self.assertEqual(rules[4], {"id": "reliance-target-1185.8", "symbol": "RELIANCE", "type": "target",
                                    "price": 1185.8, "date": "2026-10-29", "note": "1R"})
        self.assertEqual(rules[5], {"id": "tcs-pct-3", "symbol": "TCS", "type": "pct", "pct": 3.0, "direction": "down",
                                    "rearm": True})
        self.assertEqual(run(["add", "--rules", self.rules, "--time", "25:00"])[0], 1)
        self.assertEqual(run(["add", "--rules", self.rules, "--flat-by", "late"])[0], 1)
        self.assertEqual(run(["add", "--rules", self.rules, "--above", "10"])[0], 1)           # no symbol
        self.assertEqual(run(["add", "--rules", self.rules, "--symbol", "TCS"])[0], 1)         # no level
        code, _, err = run(["add", "--rules", self.rules, "--from-plan", "--plan", self.none])
        self.assertEqual(code, 1)
        self.assertIn("no plan", err)

    def test_quote_write_merges(self):
        code, text, _ = run(["quote", "RELIANCE", "infy", "--offline", QDIR, "--write", self.prices,
                             "--now", "2026-10-29 16:05"])
        self.assertEqual(code, 0)
        self.assertIn("RELIANCE   Rs 1,167.40 (+0.81% vs prev close Rs 1,158.00)  2026-10-29T15:59:57+05:30  [kite]", text)
        self.assertIn("INFY       Rs 1,146.50 (+2.08% vs prev close Rs 1,123.15)", text)
        self.assertIn("Wrote 2 price(s)", text)
        self.assertIn("fresh for 15 minutes", text)
        code, text, _ = run(["quote", "SBIN", "ZZZZ", "--offline", QDIR, "--write", self.prices, "--now", "2026-10-29 16:06"])
        self.assertEqual(code, 0)
        self.assertIn("error: no offline quote file", text)
        with open(self.prices) as fh:
            doc = json.load(fh)
        self.assertEqual(sorted(doc["prices"]), ["INFY", "RELIANCE", "SBIN"])
        self.assertEqual(doc["as_of"], "2026-10-29T16:06:00+05:30")
        self.assertEqual(doc["prices"]["SBIN"], {"last": 971.9, "time": "2026-10-29T15:59:38+05:30",
                                                 "prev_close": 956.8, "source": "kite"})
        self.assertEqual(run(["quote", "ZZZZ", "--offline", QDIR])[0], 1)
        code, _, err = run(["quote", "--offline", QDIR])
        self.assertEqual(code, 1)
        self.assertIn("which symbols", err)

    def test_quote_holdings_from_portfolio_and_plan(self):
        pf = os.path.join(self.d, "portfolio.json")
        with open(pf, "w") as fh:
            json.dump({"cash_inr": 1000.0, "positions": {"TCS": {"qty": 10, "avg": 2470.0, "product": "MIS"}},
                       "paper": True}, fh)
        code, text, _ = run(["quote", "--holdings", "--portfolio", pf, "--plan", PLAN, "--offline", QDIR, "--json",
                             "--now", NOW])
        self.assertEqual(code, 0)
        doc = json.loads(text)
        self.assertEqual(sorted(doc["quotes"]), ["RELIANCE", "TCS"])
        self.assertEqual(doc["as_of"], "2026-10-29T10:15:00+05:30")

    def test_clock_exit_codes_and_words(self):
        code, text, _ = run(["clock", "--now", NOW, "--config", self.none])
        self.assertEqual(code, 0)
        self.assertIn("2026-10-29 10:15 IST (Thursday): NSE OPEN, continuous trading.", text)
        self.assertIn("continuous trading to 15:15 for F&O stocks (closing auction to 15:35) and 15:30 for the rest", text)
        self.assertIn("Flat by 15:05. Zerodha squares off MIS positions in F&O stocks at 15:12 (Rs 50 + GST each time).", text)
        self.assertNotIn("Warning", text)
        code, text, _ = run(["clock", "--now", "2026-11-10 10:00", "--config", self.none])
        self.assertEqual(code, 1)
        self.assertIn("NSE CLOSED, NSE holiday: Diwali Balipratipada.", text)
        self.assertIn("Next open: 2026-11-11 09:15 IST.", text)
        code, text, _ = run(["clock", "--now", "2027-01-04 10:00", "--config", self.none])
        self.assertIn("holiday list for 2027 is not in the pack yet", text)
        code, text, _ = run(["clock", "--now", "2026-10-29 15:20", "--json", "--config", self.none])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)["phase"], "after-close")
        cfg = os.path.join(self.d, "config.json")
        with open(cfg, "w") as fh:
            json.dump({"flat_by": "15:55"}, fh)
        code, text, _ = run(["clock", "--now", NOW, "--config", cfg])
        self.assertIn("Warning: your flat_by 15:55 is not before Zerodha's 15:12 square-off", text)

    def test_check_without_rules(self):
        code, _, err = run(["check", "--rules", self.rules, "--offline", QDIR])
        self.assertEqual(code, 1)
        self.assertIn("no rules", err)
        code, text, _ = run(["check", "--rules", os.path.join(ROOT, "trader-alerts", "alerts.example.json"),
                             "--state", self.state, "--prices-out", self.prices, "--offline", QDIR,
                             "--now", "2026-10-29 10:15", "--no-write", "--json"])
        self.assertEqual(code, 0)
        doc = json.loads(text)
        self.assertEqual([r["id"] for r in doc["rings"]], ["bell"])
        self.assertEqual(doc["quotes"], {"RELIANCE": 1167.4, "SBIN": 971.9})
        self.assertFalse(os.path.exists(self.state))


if __name__ == "__main__":
    unittest.main()
