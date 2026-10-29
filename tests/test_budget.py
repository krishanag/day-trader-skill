import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX
from test_kite import FakeResponse, FakeNet, ok, INSTRUMENTS as KITE_INSTRUMENTS

size = load("trader-budget", "size")
kite = size.kite
nse = size.nse

CFG = os.path.join(ROOT, "trader-budget", "config.example.json")
PLAN = os.path.join(FIX, "trader-budget", "plan-reliance.json")
BARS = os.path.join(FIX, "trader-budget", "reliance-5minute-2026-10-29.json")
CSV = os.path.join(FIX, "trader-budget", "instruments-nse.csv")
PRICES = os.path.join(FIX, "trader-budget", "prices.json")
QUOTE = os.path.join(FIX, "trader-budget", "kite-quote-reliance.json")
ORDERS_CO = os.path.join(FIX, "trader-risk", "orders-reliance.json")
ORDERS_REGULAR = os.path.join(FIX, "trader-risk", "orders-regular.json")
NOW = "2026-10-29 09:45"
TODAY = "2026-10-29"
# the contract's config, key for key (FILES.md)
CONTRACT_CFG = {
    "budget_inr": 100000, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_inr": 75000,
    "min_order_inr": 500, "max_loss_per_trade_inr": 500, "max_daily_loss_inr": 1500, "max_trades_per_day": 3,
    "max_orders_per_day": 10, "min_reward_risk": 1.0, "require_stop": True,
    "no_entries_before": "09:30", "no_entries_after": "14:30", "flat_by": "15:05",
    "exchange": "NSE", "product": "MIS", "order_style": "co", "use_mis_leverage": False, "mis_leverage_x": 1.0,
    "broker": "zerodha", "paper": True, "blocked": [], "allow_bse": False, "allow_fno": False, "allow_short": False,
}
# the Kite cover order size.py writes for the fixture plan with the example config
CO_BODY = {"variety": "co", "exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "BUY",
           "order_type": "MARKET", "quantity": 43, "product": "MIS", "trigger_price": 1163.0,
           "validity": "DAY", "tag": "dtsorb"}
PLAN_BLOCK = {"entry": 1174.4, "stop": 1163.0, "target": 1185.8, "risk_per_share": 11.4, "max_loss_inr": 490.2,
              "setup": "orb", "entry_time": "09:40", "flat_by": "15:05"}


def ist(text):
    return datetime.datetime.fromisoformat(text).replace(tzinfo=nse.IST)


def cfg(**over):
    with open(CFG) as fh:
        c = json.load(fh)
    c.update(over)
    return c


def plan(**over):
    with open(PLAN) as fh:
        p = json.load(fh)
    p.update(over)
    return p


def row():
    with open(CSV) as fh:
        return kite.parse_instruments(fh.read())["RELIANCE"]


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = size.main(argv)
    return code, out.getvalue(), err.getvalue()


def prices_doc(price, quote_time="2026-10-29T09:52:10+05:30"):
    return {"as_of": quote_time, "prices": {"RELIANCE": {"last": price, "time": quote_time,
                                                         "prev_close": 1158.0, "source": "kite"}}}


def base_args(d, now=NOW, **extra):
    """CLI arguments that never reach Kite: a saved instrument CSV and no portfolio."""
    args = ["--plan", PLAN, "--config", CFG, "--portfolio", os.path.join(d, "none"), "--offline", CSV, "--now", now]
    for k, v in extra.items():
        args += ["--" + k, v]
    return args


class Offline(unittest.TestCase):
    """No test may reach Kite: any HTTP call the fake has no answer for fails the test."""

    def setUp(self):
        self.net = FakeNet()
        self.saved = (kite._urlopen, kite._sleep, dict(kite._last_call))
        kite._urlopen = self.net
        kite._sleep = lambda s: None
        kite._last_call.clear()

    def tearDown(self):
        kite._urlopen, kite._sleep, last = self.saved
        kite._last_call.clear()
        kite._last_call.update(last)


class TheFixtures(unittest.TestCase):
    """The plan is built from RELIANCE's real 5-minute candles on 2026-10-29 (Kite)."""

    def test_config_example_is_the_contract(self):
        with open(CFG) as fh:
            self.assertEqual(json.load(fh), CONTRACT_CFG)
        self.assertEqual(set(size.DEFAULTS), set(CONTRACT_CFG))

    def test_the_plan_follows_from_the_candles(self):
        with open(BARS) as fh:
            bars = kite.parse_candles(json.load(fh))
        self.assertEqual(len(bars), 72)                                   # 09:15 to 15:10, a CAS stock
        self.assertEqual(bars[0][0], ist("2026-10-29T09:15"))
        self.assertEqual(bars[-1][0], ist("2026-10-29T15:10"))
        p = plan()
        orh, orl = max(b[2] for b in bars[:3]), min(b[3] for b in bars[:3])
        self.assertEqual((orh, orl), (p["opening_range"]["high"], p["opening_range"]["low"]))
        self.assertEqual((orh, orl), (1170.5, 1155.6))
        first = next(i for i, b in enumerate(bars) if i >= 3 and b[4] > orh)
        self.assertEqual(first + 1, p["entry_bar"])                       # bars count from 1 at 09:15
        self.assertEqual(bars[first][4], p["entry"])
        self.assertEqual(bars[first][0] + datetime.timedelta(minutes=5), ist("2026-10-29T" + p["entry_time"]))
        tick = row()["tick_size"]
        self.assertEqual(tick, 0.1)
        self.assertEqual(p["tick_size"], tick)
        self.assertEqual(size.snap_down((orh + orl) / 2, tick), p["stop"])     # 1163.05 -> 1163.00
        self.assertEqual(p["stop"], 1163.0)
        self.assertEqual(round(p["entry"] - p["stop"], 2), p["risk_per_share"])
        self.assertEqual(size.snap_down(p["entry"] + p["risk_per_share"], tick), p["target"])
        self.assertEqual((p["entry"], p["risk_per_share"], p["target"]), (1174.4, 11.4, 1185.8))
        self.assertTrue(p["cas"])

    def test_prices_fixture_comes_from_the_kite_quote(self):
        with open(QUOTE) as fh:
            q = json.load(fh)["RELIANCE"]
        with open(PRICES) as fh:
            doc = json.load(fh)
        r = doc["prices"]["RELIANCE"]
        self.assertEqual(r["last"], q["last_price"])
        self.assertEqual(r["prev_close"], q["ohlc"]["close"])
        self.assertEqual(r["time"], q["last_trade_time"].replace(" ", "T") + "+05:30")
        self.assertEqual(doc["as_of"], q["timestamp"].replace(" ", "T") + "+05:30")
        self.assertEqual(r["source"], "kite")

    def test_instrument_rows_are_real_kite_rows(self):
        with open(CSV) as fh:
            table = kite.parse_instruments(fh.read())
        self.assertEqual(table["RELIANCE"]["instrument_token"], 738561)
        self.assertEqual(table["RELIANCE"]["lot_size"], 1)
        self.assertEqual(table["SUZLON"]["tick_size"], 0.01)       # under Rs 250: a one-paisa tick
        self.assertEqual(table["INFY"]["tick_size"], 0.05)


class TheReel(Offline):
    """Rs 100,000, 10% buffer, Rs 500 max loss, RELIANCE risk 11.40 -> 43 shares, Rs 50,499.20, max loss Rs 490.20."""

    def test_reliance_is_43_shares_bound_by_the_max_loss(self):
        s = size.size_trade(plan(), cfg(), today=TODAY, row=row())
        self.assertEqual(s["qty"], 43)
        self.assertEqual(s["bound_by"], "max loss")
        self.assertEqual(s["risk_per_share"], 11.4)
        self.assertEqual(s["cost"], 50499.2)
        self.assertEqual(s["max_loss_inr"], 490.2)
        self.assertEqual(s["reward_risk"], 1.0)
        self.assertEqual(s["cash_buffer"], 10000)
        self.assertEqual(s["cash_available"], 90000)
        self.assertEqual([(l["name"], l["shares"]) for l in s["limits"]],
                         [("max loss", 43), ("position cap", 63), ("max order", 63), ("cash", 76)])
        self.assertEqual([l["name"] for l in s["limits"] if l["binds"]], ["max loss"])
        self.assertEqual((s["tick_size"], s["tick_from"], s["snapped"]), (0.1, "kite", []))
        self.assertIsNone(s["refusal"])

    def test_the_cover_order(self):
        o = size.kite_order(size.size_trade(plan(), cfg(), today=TODAY, row=row()))
        why = o.pop("why")
        self.assertEqual(o, {
            "symbol": "RELIANCE", "exchange": "NSE", "side": "buy", "qty": 43, "product": "MIS", "order_style": "co",
            "kite": CO_BODY, "stop_leg": None, "stop": 1163.0, "target": 1185.8, "plan": PLAN_BLOCK})
        self.assertEqual(why, "opening-range breakout on RELIANCE: 43 shares, bound by your Rs 500 max loss per "
                              "trade (43 x 11.40 = Rs 490.20 at the stop)")
        self.assertIsInstance(o["kite"]["quantity"], int)
        self.assertNotIn("price", o["kite"])

    def test_regular_style_writes_an_entry_and_a_stop_leg(self):
        o = size.kite_order(size.size_trade(plan(), cfg(order_style="regular"), today=TODAY, row=row()))
        body = dict(CO_BODY, variety="regular")
        body.pop("trigger_price")
        self.assertEqual(o["kite"], body)
        self.assertEqual(o["stop_leg"], {
            "variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "SELL",
            "order_type": "SL-M", "quantity": 43, "product": "MIS", "trigger_price": 1163.0, "validity": "DAY",
            "tag": "dtsorb"})
        self.assertEqual(o["order_style"], "regular")

    def test_the_fixture_orders_files_are_what_it_writes(self):
        for path, style in ((ORDERS_CO, "co"), (ORDERS_REGULAR, "regular")):
            with open(path) as fh:
                doc = json.load(fh)
            want = size.kite_order(size.size_trade(plan(), cfg(order_style=style), today=TODAY, row=row()))
            self.assertEqual(doc["orders"], [want], style)
            self.assertEqual(doc["created_at"], "2026-10-29T09:45:00+05:30")
            self.assertEqual(doc["source"], "trader/plan.json (RELIANCE orb 2026-10-29)")
            self.assertEqual((doc["created_by"], doc["status"], doc["paper"]), ("trader-budget", "proposed", True))

    def test_a_limit_order(self):
        o = size.kite_order(size.size_trade(plan(), cfg(), limit=1174.4, today=TODAY, row=row()))
        self.assertEqual((o["kite"]["order_type"], o["kite"]["price"], o["kite"]["quantity"]), ("LIMIT", 1174.4, 43))
        self.assertEqual(o["kite"]["trigger_price"], 1163.0)
        s = size.size_trade(plan(), cfg(), limit=1174.0, today=TODAY, row=row())
        self.assertEqual((s["risk_per_share"], s["qty"], s["max_loss_inr"], s["entry_from"]), (11.0, 45, 495.0, "limit"))
        s = size.size_trade(plan(), cfg(), limit=1174.07, today=TODAY, row=row())
        self.assertEqual(s["entry"], 1174.0)                            # a limit snaps down to the tick

    def test_cli_writes_one_cover_order(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "trader", "orders.json")
            code, text, _ = run(base_args(d, write=out))
            self.assertEqual(code, 0)
            self.assertIn("Size: 43 shares of RELIANCE = Rs 50,499.20 at 1174.40", text)
            self.assertIn("43 x 11.40 = Rs 490.20", text)
            self.assertIn("<- binds", text)
            self.assertIn("BUY 43 RELIANCE, NSE MIS, variety co, MARKET, trigger (stop) 1163.00, DAY, tag dtsorb", text)
            self.assertIn("Flat by 15:05 IST", text)
            self.assertIn("15:12", text)
            self.assertIn("not financial advice", text)
            self.assertIn("Wrote 1 proposed cover order", text)
            with open(out) as fh:
                doc = json.load(fh)
            self.assertEqual(doc["status"], "proposed")
            self.assertIs(doc["paper"], True)
            self.assertEqual(doc["created_at"], "2026-10-29T09:45:00+05:30")
            self.assertEqual(len(doc["orders"]), 1)
            self.assertEqual(doc["orders"][0]["kite"], CO_BODY)
            self.assertEqual(doc["orders"][0]["plan"]["max_loss_inr"], 490.2)
        self.assertEqual(self.net.requests, [])

    def test_cli_json_and_limit(self):
        with tempfile.TemporaryDirectory() as d:
            code, text, _ = run(base_args(d) + ["--limit", "--json"])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual(out["sizing"]["qty"], 43)
        self.assertEqual((out["orders"][0]["kite"]["order_type"], out["orders"][0]["kite"]["price"]), ("LIMIT", 1174.4))


class PlanRules(Offline):
    def test_a_stop_too_far_for_the_max_loss_is_a_plain_refusal(self):
        s = size.size_trade(plan(), cfg(max_loss_per_trade_inr=10), today=TODAY, row=row())
        self.assertEqual(s["qty"], 0)
        self.assertEqual(s["bound_by"], "max loss")
        self.assertIn("one share risks Rs 11.40 (entry 1174.40 minus stop 1163.00), more than your Rs 10 max loss "
                      "per trade", s["refusal"])
        self.assertIsNone(size.kite_order(s))
        with tempfile.TemporaryDirectory() as d:
            c = os.path.join(d, "config.json")
            with open(c, "w") as fh:
                json.dump(cfg(max_loss_per_trade_inr=10), fh)
            out = os.path.join(d, "orders.json")
            code, text, _ = run(["--plan", PLAN, "--config", c, "--portfolio", os.path.join(d, "none"),
                                 "--offline", CSV, "--now", NOW, "--write", out])
            self.assertEqual(code, 1)
            self.assertIn("No order:", text)
            self.assertIn("not financial advice", text)
            self.assertFalse(os.path.exists(out))

    def test_a_share_too_dear_for_a_limit_is_a_refusal(self):
        s = size.size_trade(plan(), cfg(max_order_inr=1000), today=TODAY, row=row())
        self.assertEqual((s["qty"], s["bound_by"]), (0, "max order"))
        self.assertIn("one share of RELIANCE costs Rs 1,174.40, more than your max order limit", s["refusal"])

    def test_each_limit_can_bind(self):
        s = size.size_trade(plan(), cfg(max_order_inr=20000), today=TODAY, row=row())
        self.assertEqual((s["qty"], s["bound_by"]), (17, "max order"))
        s = size.size_trade(plan(), cfg(max_position_pct=20), today=TODAY, row=row())
        self.assertEqual((s["qty"], s["bound_by"]), (17, "position cap"))
        s = size.size_trade(plan(), cfg(), cash=15000, today=TODAY, row=row())
        self.assertEqual((s["qty"], s["bound_by"], s["cash_available"]), (4, "cash", 5000))
        self.assertIn("cash Rs 15,000 minus the Rs 10,000 cash buffer", s["limits"][-1]["how"])
        s = size.size_trade(plan(), cfg(max_loss_per_trade_inr=1000), today=TODAY, row=row())
        self.assertEqual((s["qty"], s["bound_by"]), (63, "position cap"))
        more = size.size_trade(plan(), cfg(), cash=250000, today=TODAY, row=row())
        self.assertEqual(more["cash_available"], 90000)                 # cash above the budget never adds

    def test_mis_leverage_widens_the_cash_limit_only(self):
        s = size.size_trade(plan(), cfg(use_mis_leverage=True, mis_leverage_x=5), today=TODAY, row=row())
        self.assertEqual((s["qty"], s["bound_by"], s["cash_available"], s["mis_leverage_x"]), (43, "max loss", 450000, 5.0))
        self.assertEqual(s["limits"][-1]["shares"], 383)
        self.assertIn("x 5 MIS leverage", s["limits"][-1]["how"])
        s = size.size_trade(plan(), cfg(use_mis_leverage=True, mis_leverage_x=5), cash=15000, today=TODAY, row=row())
        self.assertEqual((s["qty"], s["bound_by"], s["cash_available"]), (21, "cash", 25000))
        off = size.size_trade(plan(), cfg(use_mis_leverage=False, mis_leverage_x=5), today=TODAY, row=row())
        self.assertEqual(off["cash_available"], 90000)
        with self.assertRaisesRegex(size.SizeError, "mis_leverage_x must be at least 1"):
            size.size_trade(plan(), cfg(use_mis_leverage=True, mis_leverage_x=0.5), today=TODAY, row=row())

    def test_a_fresh_higher_price_means_a_bigger_risk(self):
        s = size.size_trade(plan(), cfg(min_reward_risk=0), price=1178.0, today=TODAY, row=row())
        self.assertEqual((s["entry"], s["entry_from"], s["risk_per_share"]), (1178.0, "price", 15.0))
        self.assertEqual((s["qty"], s["max_loss_inr"], s["reward_risk"]), (33, 495.0, 0.52))
        self.assertIsNone(s["refusal"])
        s = size.size_trade(plan(), cfg(), price=1178.0, today=TODAY, row=row())
        self.assertIn("reward:risk 0.52 (target 1185.80, entry 1178.00, stop 1163.00) is under your minimum 1",
                      s["refusal"])
        self.assertIsNone(size.kite_order(s))
        lower = size.size_trade(plan(), cfg(), price=1170.0, today=TODAY, row=row())
        self.assertEqual((lower["entry"], lower["entry_from"], lower["qty"]), (1174.4, "plan", 43))

    def test_prices_file_is_used_only_when_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            pp = os.path.join(d, "prices.json")
            with open(pp, "w") as fh:
                json.dump(prices_doc(1178.0), fh)
            code, text, _ = run(base_args(d, now="2026-10-29 09:55", prices=pp) + ["--json"])
            out = json.loads(text)
            self.assertEqual((code, out["sizing"]["qty"], out["orders"]), (1, 33, []))
            self.assertIn("RELIANCE last 1178.00 (as of 09:52 IST) is above the plan's entry 1174.40", out["notes"][0])
            out = json.loads(run(base_args(d, now="2026-10-29 10:30", prices=pp) + ["--json"])[1])
            self.assertEqual(out["sizing"]["qty"], 43)
            self.assertIn("is from 2026-10-29 09:52 IST: not used (older than 15 minutes)", out["notes"][0])
            # the real quote fixture is from after the close: in the future at 09:45, so not fresh
            out = json.loads(run(base_args(d, prices=PRICES) + ["--json"])[1])
            self.assertEqual(out["sizing"]["qty"], 43)
            self.assertIn("not used", out["notes"][0])

    def test_the_portfolio_cash_caps_the_size(self):
        with tempfile.TemporaryDirectory() as d:
            pf = os.path.join(d, "portfolio.json")
            with open(pf, "w") as fh:
                json.dump({"cash_inr": 15000, "positions": {}, "paper": True}, fh)
            out = json.loads(run(["--plan", PLAN, "--config", CFG, "--portfolio", pf, "--offline", CSV,
                                  "--now", NOW, "--json"])[1])
            self.assertEqual((out["sizing"]["qty"], out["sizing"]["bound_by"]), (4, "cash"))
            with open(pf, "w") as fh:
                json.dump({"cash": 15000}, fh)
            code, _, err = run(["--plan", PLAN, "--config", CFG, "--portfolio", pf, "--offline", CSV, "--now", NOW])
            self.assertEqual(code, 1)
            self.assertIn("cash_inr", err)

    def test_bad_plans(self):
        for p, word in ((plan(stop=1180.0), "not below the entry"),
                        (plan(stop=1050.0), "more than 10 percent under the entry"),
                        (plan(target=1170.0), "already happened"),
                        (plan(target=None), "positive target"),
                        (plan(side="sell"), "long (buy) day trades only"),
                        (plan(symbol=""), "no symbol")):
            with self.assertRaises(size.SizeError) as e:
                size.size_trade(p, cfg(), today=TODAY, row=row())
            self.assertIn(word, str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(), price=1186.0, today=TODAY, row=row())
        self.assertIn("already happened", str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(blocked=["reliance"]), today=TODAY, row=row())
        self.assertIn("blocked list", str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(), today="2026-10-30", row=row())
        self.assertIn("make a new plan", str(e.exception))

    def test_bad_config(self):
        for over, word in (({"order_style": "oco"}, "order_style must be"),
                           ({"product": "CNC"}, "product must be MIS"),
                           ({"exchange": "BSE"}, "allow_bse"),
                           ({"exchange": "NFO"}, "exchange must be NSE"),
                           ({"budget_inr": 0}, "no budget"),
                           ({"cash_buffer_pct": 100}, "cash_buffer_pct")):
            with self.assertRaises(size.SizeError) as e:
                size.size_trade(plan(), cfg(**over), today=TODAY, row=row())
            self.assertIn(word, str(e.exception), over)
        s = size.size_trade(plan(), cfg(exchange="BSE", allow_bse=True), today=TODAY, row=row())
        self.assertEqual(size.kite_order(s)["kite"]["exchange"], "BSE")

    def test_prices_snap_down_to_the_tick(self):
        s = size.size_trade(plan(stop=1163.05, target=1185.87), cfg(), today=TODAY, row=row())
        self.assertEqual((s["stop"], s["target"], s["snapped"]), (1163.0, 1185.8, ["stop", "target"]))
        s = size.size_trade(plan(tick_size=0.05), cfg(), today=TODAY, row=row())
        self.assertEqual((s["tick_size"], s["tick_from"]), (0.1, "kite"))        # Kite's row beats the plan
        s = size.size_trade(plan(), cfg(), today=TODAY)
        self.assertEqual((s["tick_size"], s["tick_from"]), (0.1, "plan"))
        p = plan()
        p.pop("tick_size")
        s = size.size_trade(p, cfg(), today=TODAY)
        self.assertEqual((s["tick_size"], s["tick_from"]), (0.05, "default"))
        self.assertEqual(size.snap_down(60.009, 0.01), 60.0)
        self.assertTrue(size.on_tick(1163.0, 0.1))
        self.assertFalse(size.on_tick(1163.05, 0.1))

    def test_under_the_minimum_order(self):
        cheap = plan(symbol="SUZLON", entry=60.0, stop=59.5, target=60.5, tick_size=0.01)
        s = size.size_trade(cheap, cfg(max_loss_per_trade_inr=2), today=TODAY)
        self.assertEqual(s["qty"], 4)
        self.assertIn("4 shares of SUZLON is Rs 240, under your Rs 500 minimum order", s["refusal"])
        self.assertIsNone(size.kite_order(s))

    def test_the_tag_is_kite_safe(self):
        self.assertEqual(size.order_tag("orb"), "dtsorb")
        self.assertEqual(size.order_tag("flag-2 x"), "dtsflag2x")
        self.assertEqual(len(size.order_tag("a" * 40)), 20)
        self.assertEqual(size.order_tag(""), "dts")

    def test_missing_plan_and_bad_flags(self):
        with tempfile.TemporaryDirectory() as d:
            code, _, err = run(["--plan", "/nonexistent/plan.json", "--config", CFG, "--offline", CSV, "--now", NOW])
            self.assertEqual(code, 1)
            self.assertIn("no plan", err)
            code, _, err = run(base_args(d) + ["--limit", "abc"])
            self.assertEqual(code, 1)
            self.assertIn("--limit wants a price", err)
            code, _, err = run(base_args(d, now="tomorrow"))
            self.assertEqual(code, 1)
            self.assertIn("--now wants", err)
            self.assertNotIn("Traceback", err)
            code, _, err = run(base_args(d, offline=os.path.join(d, "none.csv")))
            self.assertEqual(code, 1)
            self.assertIn("cannot read", err)
            code, _, err = run(base_args(d) + ["--budget", "0"])
            self.assertEqual(code, 1)
            self.assertIn("no budget", err)

    def test_the_time_helpers_are_ist(self):
        self.assertEqual(size.parse_now("2026-10-29 09:45"), ist("2026-10-29T09:45"))
        self.assertEqual(size.parse_now("2026-10-29T04:15:00Z"), ist("2026-10-29T09:45"))
        self.assertEqual(size.iso(ist("2026-10-29T09:45")), "2026-10-29T09:45:00+05:30")
        p, t, fresh = size.fresh_price(prices_doc(1178.0), "RELIANCE", ist("2026-10-29T09:55"))
        self.assertEqual((p, t, fresh), (1178.0, ist("2026-10-29T09:52:10"), True))
        self.assertFalse(size.fresh_price(prices_doc(1178.0), "RELIANCE", ist("2026-10-29T10:08"))[2])
        self.assertFalse(size.fresh_price(prices_doc(1178.0), "RELIANCE", ist("2026-10-29T09:50"))[2])
        bare = {"as_of": "2026-10-29T09:52:10+05:30", "prices": {"RELIANCE": 1178.0}}
        self.assertEqual(size.fresh_price(bare, "RELIANCE", ist("2026-10-29T09:55")), (1178.0, t, True))
        self.assertEqual(size.money(500), "Rs 500")
        self.assertEqual(size.money(50499.2), "Rs 50,499.20")


class WithKite(Offline):
    """The optional Kite reads: the instrument row and the live balance, answered by a fake network."""

    def test_live_cash_is_the_live_balance_from_margins(self):
        self.net.answers.append(ok({"enabled": True, "net": 42000.5,
                                    "available": {"cash": 42000.5, "live_balance": 42000.5, "collateral": 0}}))
        self.assertEqual(size.live_cash(kite.Kite("key", "tok")), 42000.5)
        self.assertIn("/user/margins/equity", self.net.requests[0].full_url)
        self.net.answers.append(ok({"available": {}}))
        with self.assertRaisesRegex(kite.KiteError, "live_balance"):
            size.live_cash(kite.Kite("key", "tok"))

    def test_instrument_row_from_the_session_or_the_csv(self):
        self.assertEqual(size.instrument_row("RELIANCE", CSV)["tick_size"], 0.1)
        with self.assertRaisesRegex(size.SizeError, "ZZZZ is not in"):
            size.instrument_row("ZZZZ", CSV)
        self.assertIsNone(size.instrument_row("RELIANCE"))
        with tempfile.TemporaryDirectory() as d:
            cwd = os.getcwd()
            os.chdir(d)
            try:
                self.net.answers.append(FakeResponse(KITE_INSTRUMENTS, "text/csv"))
                k = kite.Kite("key", "tok")
                self.assertEqual(size.instrument_row("INFY", None, k)["instrument_token"], 408065)
                with self.assertRaisesRegex(size.SizeError, "not listed on NSE"):
                    size.instrument_row("ZZZZ", None, k)
            finally:
                os.chdir(cwd)

    def test_cli_with_a_session_reads_the_row_and_the_balance(self):
        with open(CSV) as fh:
            csv_text = fh.read()
        with tempfile.TemporaryDirectory() as d:
            cwd, env = os.getcwd(), os.environ.get("KITE_API_KEY")
            os.chdir(d)
            os.environ["KITE_API_KEY"] = "key"
            try:
                kite.save_session({"access_token": "tok", "user_id": "AB1234"}, nse.now_ist())
                self.net.answers.extend([FakeResponse(csv_text, "text/csv"),
                                         ok({"available": {"live_balance": 15000.0}})])
                code, text, _ = run(["--plan", PLAN, "--config", CFG, "--now", NOW, "--json"])
            finally:
                os.chdir(cwd)
                if env is None:
                    os.environ.pop("KITE_API_KEY", None)
                else:
                    os.environ["KITE_API_KEY"] = env
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual((out["sizing"]["qty"], out["sizing"]["bound_by"], out["sizing"]["tick_from"]), (4, "cash", "kite"))
        self.assertIn("Cash Rs 15,000 is your live Kite balance", out["notes"][0])
        self.assertEqual(len(self.net.requests), 2)

    def test_no_session_is_a_note_not_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            cwd, env = os.getcwd(), os.environ.get("KITE_API_KEY")
            os.chdir(d)
            os.environ.pop("KITE_API_KEY", None)
            try:
                code, text, _ = run(["--plan", PLAN, "--config", CFG, "--now", NOW, "--json"])
            finally:
                os.chdir(cwd)
                if env is not None:
                    os.environ["KITE_API_KEY"] = env
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual((out["sizing"]["qty"], out["sizing"]["tick_from"]), (43, "plan"))
        self.assertIn("No Kite session", out["notes"][0])
        self.assertEqual(self.net.requests, [])


if __name__ == "__main__":
    unittest.main()
