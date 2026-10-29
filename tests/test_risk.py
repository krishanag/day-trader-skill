import datetime
import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX
from test_kite import FakeResponse, FakeNet

check = load("trader-risk", "check")
kite = check.kite
nse = check.nse

CFG_PATH = os.path.join(ROOT, "trader-budget", "config.example.json")
PLAN_PATH = os.path.join(FIX, "trader-budget", "plan-reliance.json")
CSV = os.path.join(FIX, "trader-budget", "instruments-nse.csv")
PRICES_PATH = os.path.join(FIX, "trader-budget", "prices.json")
CO_PATH = os.path.join(FIX, "trader-risk", "orders-reliance.json")
REGULAR_PATH = os.path.join(FIX, "trader-risk", "orders-regular.json")
NOW = "2026-10-29 09:45"
TODAY = "2026-10-29"
QUOTE_TIME = "2026-10-29T09:52:10+05:30"     # inside the 09:50 bar (1175.50 to 1180.50): 1178.00 is a real tick


def ist(text):
    return datetime.datetime.fromisoformat(text).replace(tzinfo=nse.IST)


def at(text):
    return check.parse_now(text)


def cfg(**over):
    with open(CFG_PATH) as fh:
        c = json.load(fh)
    c.update(over)
    return c


def plan():
    with open(PLAN_PATH) as fh:
        return json.load(fh)


def rows():
    with open(CSV) as fh:
        return kite.parse_instruments(fh.read())


def reliance(qty=None, **over):
    """The fixture cover order; qty changes both the order and the Kite body; kite_ keys change the body."""
    with open(CO_PATH) as fh:
        o = json.load(fh)["orders"][0]
    if qty is not None:
        o["qty"] = qty
        o["kite"]["quantity"] = qty
    for k, v in over.items():
        if k.startswith("kite_"):
            o["kite"][k[5:]] = v
        else:
            o[k] = v
    return o


def regular():
    with open(REGULAR_PATH) as fh:
        return json.load(fh)["orders"][0]


def co_order(sym, qty, entry, stop, target):
    body = {"variety": "co", "exchange": "NSE", "tradingsymbol": sym, "transaction_type": "BUY",
            "order_type": "MARKET", "quantity": qty, "product": "MIS", "trigger_price": stop, "validity": "DAY",
            "tag": "dtsorb"}
    return {"symbol": sym, "exchange": "NSE", "side": "buy", "qty": qty, "product": "MIS", "order_style": "co",
            "kite": body, "stop_leg": None, "stop": stop, "target": target,
            "plan": {"entry": entry, "stop": stop, "target": target}}


def placed(sym, side="buy", day=TODAY, max_loss=490.2, kind=None, status="placed", ohash=None):
    """A trader/log.jsonl line in the broker's shape."""
    r = {"at": day + "T10:22:00+05:30", "date": day, "status": status, "order_hash": ohash or ("h" + sym.lower()),
         "symbol": sym, "side": side, "qty": 43, "paper": True, "reply": "yes"}
    if max_loss is not None:
        r["max_loss_inr"] = max_loss
    if kind:
        r["kind"] = kind
    return r


def verdicts(res):
    return [r["verdict"] for r in res["results"]]


def reasons(res, i=0):
    return " | ".join(res["results"][i]["reasons"])


def warnings(res, i=0):
    return " | ".join(res["results"][i]["warnings"])


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = check.main(argv)
    return code, out.getvalue(), err.getvalue()


def base_args(d, orders=CO_PATH, now=NOW, **extra):
    """CLI arguments that never reach Kite and read nothing but the fixtures."""
    none = os.path.join(d, "none")
    args = ["--orders", orders, "--config", CFG_PATH, "--plan", PLAN_PATH, "--portfolio", none, "--prices", none,
            "--log", none, "--fills", none, "--offline", CSV, "--now", now]
    for k, v in extra.items():
        args += ["--" + k.replace("_", "-"), v]
    return args


class Offline(unittest.TestCase):
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


class TheReel(Offline):
    def one(self, order, now=NOW, **over):
        return check.check([order], cfg(**over), plan=plan(), rows=rows(), now=at(now))

    def test_the_reliance_cover_order_passes_every_check(self):
        res = self.one(reliance())
        r = res["results"][0]
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertEqual(r["reasons"], [])
        self.assertEqual(r["warnings"], [])
        self.assertEqual((r["kind"], r["entry_ref"], r["stop"], r["target"]), ("entry", 1174.4, 1163.0, 1185.8))
        self.assertEqual((r["risk_per_share"], r["max_loss_inr"], r["reward_risk"]), (11.4, 490.2, 1.0))
        self.assertEqual((r["qty"], r["value_inr"]), (43, 50499.2))
        self.assertEqual(res["cash_after"], 49500.8)
        self.assertEqual(res["date"], TODAY)
        self.assertEqual(res["now_ist"], "2026-10-29 09:45")
        self.assertIs(res["paper"], True)
        self.assertEqual(res["warnings"], [])

    def test_forty_four_shares_is_blocked_by_the_max_loss_only(self):
        res = self.one(reliance(qty=44))
        self.assertEqual(verdicts(res), ["BLOCK"])
        self.assertEqual(res["results"][0]["reasons"],
                         ["loss: 44 shares x 11.40 risk a share = Rs 501.60 at the stop, over your Rs 500 max loss "
                          "per trade"])

    def test_the_regular_style_entry_with_its_stop_leg_passes(self):
        res = self.one(regular())
        r = res["results"][0]
        self.assertEqual((r["verdict"], r["stop"], r["max_loss_inr"]), ("PASS", 1163.0, 490.2))
        self.assertNotEqual(r["order_hash"], check.order_hash(reliance()))

    def test_order_hash_is_the_contract_function(self):
        o = reliance()

        def contract(order):
            body = json.dumps({"kite": order["kite"], "stop_leg": order.get("stop_leg")}, sort_keys=True,
                              separators=(",", ":"))
            return hashlib.sha256(body.encode()).hexdigest()[:12]

        self.assertEqual(check.order_hash(o), contract(o))
        self.assertEqual(len(check.order_hash(o)), 12)
        self.assertEqual(check.order_hash(dict(o, why="anything", plan={}, stop=1.0)), check.order_hash(o))
        o.pop("stop_leg")                                                   # a missing stop_leg is a null one
        self.assertEqual(check.order_hash(o), check.order_hash(reliance()))
        self.assertNotEqual(check.order_hash(reliance(qty=44)), check.order_hash(o))
        self.assertNotEqual(check.order_hash(reliance(kite_trigger_price=1162.9)), check.order_hash(o))
        self.assertNotEqual(check.order_hash(reliance(kite_tradingsymbol="INFY")), check.order_hash(o))

    def test_a_changed_stop_leg_is_a_new_order(self):
        r = regular()
        self.assertEqual(check.order_hash(r), check.order_hash(regular()))
        moved = regular()
        moved["stop_leg"]["trigger_price"] = 1162.9
        self.assertNotEqual(check.order_hash(moved), check.order_hash(r))
        fewer = regular()
        fewer["stop_leg"]["quantity"] = 40
        self.assertNotEqual(check.order_hash(fewer), check.order_hash(r))
        gone = regular()
        gone["stop_leg"] = None
        self.assertNotEqual(check.order_hash(gone), check.order_hash(r))
        self.assertEqual(check.order_hash(gone), check.order_hash(dict(regular(), stop_leg=None, why="x")))

    def test_cli_writes_the_verdicts_keyed_by_hash(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "trader", "verdicts.json")
            code, text, _ = run(base_args(d, out=out))
            self.assertEqual(code, 0)
            self.assertIn("PASS  #1 buy RELIANCE 43 shares, entry", text)
            self.assertIn("entry 1174.40 (the plan's entry), stop 1163.00, target 1185.80: risk 11.40 a share", text)
            self.assertIn("max loss Rs 490.20 (limit Rs 500), reward:risk 1.00 (min 1)", text)
            self.assertIn("today before this: 0 of 3 trades, Rs 0 of Rs 1,500 daily loss", text)
            self.assertIn("1 passed, 0 blocked at 2026-10-29 09:45 IST. Cash after the passed orders: Rs 49,500.80 "
                          "(paper account).", text)
            self.assertIn("not financial advice", text)
            self.assertNotIn("WARNING", text)
            with open(out) as fh:
                rec = json.load(fh)
            h = check.order_hash(reliance())
            self.assertEqual(rec["created_by"], "trader-risk")
            self.assertEqual(rec["date"], TODAY)
            self.assertEqual(rec["checked_at"], "2026-10-29T09:45:00+05:30")
            self.assertEqual(list(rec["verdicts"]), [h])
            v = rec["verdicts"][h]
            self.assertEqual((v["verdict"], v["reasons"], v["warnings"], v["kind"], v["symbol"]),
                             ("PASS", [], [], "entry", "RELIANCE"))
            self.assertEqual((v["side"], v["qty"], v["max_loss_inr"], v["value_inr"]), ("buy", 43, 490.2, 50499.2))
        self.assertEqual(self.net.requests, [])

    def test_cli_json_and_no_write(self):
        with tempfile.TemporaryDirectory() as d:
            code, text, _ = run(base_args(d) + ["--json", "--no-write"])
            self.assertEqual(code, 0)
            out = json.loads(text)
            self.assertEqual(out["results"][0]["order_hash"], check.order_hash(reliance()))
            self.assertFalse(os.path.exists(os.path.join(d, "trader")))


class DayTradeChecks(Offline):
    def one(self, order, now=NOW, **over):
        return check.check([order], cfg(**over), plan=plan(), rows=rows(), now=at(now))

    def test_a_stop_at_the_broker_is_required(self):
        o = reliance()
        o["kite"].pop("trigger_price")
        res = self.one(o)
        self.assertIn("stop: no stop at the broker. Every entry needs one (\"require_stop\": true)", reasons(res))
        res = self.one(o, require_stop=False)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("the stop 1163.00 is in the plan only, not at the broker", warnings(res))
        self.assertEqual(res["results"][0]["max_loss_inr"], 490.2)
        o["plan"] = {"entry": 1174.4}
        res = self.one(o, require_stop=False, min_reward_risk=0)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("no stop: the max loss is not capped", warnings(res))
        r = regular()
        r["stop_leg"] = None
        self.assertIn("stop: no stop at the broker", reasons(self.one(r)))

    def test_the_stop_must_sit_below_the_entry_within_ten_percent_on_the_tick(self):
        self.assertIn("stop: the stop 1175.00 must be below the plan's entry 1174.40 for a buy",
                      reasons(self.one(reliance(kite_trigger_price=1175.0))))
        self.assertIn("stop: the stop 1050.00 is more than 10 percent from the entry 1174.40; Zerodha's cover order "
                      "takes a trigger within 10 percent", reasons(self.one(reliance(kite_trigger_price=1050.0))))
        self.assertIn("stop: the stop 1163.05 is not on RELIANCE's 0.10 tick",
                      reasons(self.one(reliance(kite_trigger_price=1163.05))))
        self.assertIn("the broker's stop 1162.90 differs from the plan's stop 1163.00",
                      warnings(self.one(reliance(kite_trigger_price=1162.9))))

    def test_the_tick_comes_from_kite_then_the_plan_then_a_default(self):
        o = reliance(kite_trigger_price=1163.05)
        res = check.check([o], cfg(), plan=plan(), now=at(NOW))                 # no Kite rows: the plan's 0.10
        self.assertIn("not on RELIANCE's 0.10 tick", reasons(res))
        res = check.check([o], cfg(), now=at(NOW))                              # neither: 0.05 assumed, and said
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("tick size for RELIANCE unknown (no Kite session, not in the plan): 0.05 assumed", warnings(res))

    def test_target_and_reward_risk(self):
        res = self.one(reliance(plan=dict(reliance()["plan"], target=1170.0)))
        self.assertIn("target: the target 1170.00 must be above the entry 1174.40", reasons(res))
        res = self.one(reliance(plan=dict(reliance()["plan"], target=1180.0)))
        self.assertIn("reward:risk: 0.49 (target 1180.00, entry 1174.40, stop 1163.00) is under your minimum 1",
                      reasons(res))
        self.assertEqual(verdicts(self.one(reliance(plan=dict(reliance()["plan"], target=1180.0)), min_reward_risk=0.4)),
                         ["PASS"])
        p = reliance()["plan"]
        p.pop("target")
        res = self.one(reliance(plan=p, target=None))
        self.assertIn("reward:risk: no target to measure it against (min_reward_risk 1)", reasons(res))
        self.assertIn("the target 1185.87 is not on the 0.10 tick",
                      warnings(self.one(reliance(plan=dict(reliance()["plan"], target=1185.87)))))

    def test_a_fresh_higher_price_is_the_entry(self):
        later = at("2026-10-29 09:55")
        res = check.check([reliance()], cfg(), prices={"RELIANCE": 1178.0}, plan=plan(), rows=rows(), now=later,
                          price_times={"RELIANCE": ist("2026-10-29T09:52:10")})
        r = res["results"][0]
        self.assertEqual((r["entry_ref"], r["entry_ref_from"], r["risk_per_share"]), (1178.0, "price", 15.0))
        self.assertIn("loss: 43 shares x 15.00 risk a share = Rs 645 at the stop, over your Rs 500", reasons(res))
        self.assertIn("reward:risk: 0.52", reasons(res))
        self.assertIn("RELIANCE last 1178.00 is above the plan's entry 1174.40: the risk is measured from 1178.00",
                      " ".join(r["info"]))
        res = check.check([reliance(kite_trigger_price=1179.0)], cfg(), prices={"RELIANCE": 1178.0}, plan=plan(),
                          rows=rows(), now=later, price_times={"RELIANCE": ist("2026-10-29T09:52:10")})
        self.assertIn("stop: the stop 1179.00 must be below the latest price 1178.00 for a buy", reasons(res))

    def test_a_stale_or_future_price_is_not_used(self):
        for now in ("2026-10-29 10:30", "2026-10-29 09:45"):
            res = check.check([reliance()], cfg(), prices={"RELIANCE": 1178.0}, plan=plan(), rows=rows(), now=at(now),
                              price_times={"RELIANCE": ist("2026-10-29T09:52:10")})
            self.assertEqual(verdicts(res), ["PASS"], now)
            self.assertEqual(res["results"][0]["entry_ref"], 1174.4)
            self.assertIn("the RELIANCE price in trader/prices.json is older than 15 minutes: not used", warnings(res))

    def test_a_lower_price_does_not_shrink_the_risk(self):
        res = check.check([reliance()], cfg(), prices={"RELIANCE": 1170.0}, plan=plan(), rows=rows(), now=at(NOW))
        self.assertEqual((res["results"][0]["entry_ref"], res["results"][0]["entry_ref_from"]), (1174.4, "plan"))

    def test_a_limit_order_uses_its_limit_price(self):
        o = reliance(kite_order_type="LIMIT", kite_price=1174.0)
        res = check.check([o], cfg(), prices={"RELIANCE": 1178.0}, plan=plan(), rows=rows(), now=at(NOW))
        r = res["results"][0]
        self.assertEqual((r["entry_ref"], r["entry_ref_from"], r["max_loss_inr"], r["verdict"]),
                         (1174.0, "limit", 473.0, "PASS"))
        self.assertIn("format: a LIMIT order needs a positive price", reasons(self.one(reliance(kite_order_type="LIMIT"))))

    def test_daily_loss_and_trades_per_day_from_the_log(self):
        two = [placed("INFY"), placed("SBIN")]
        res = check.check([reliance()], cfg(), log=two, plan=plan(), rows=rows(), now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("today before this: 2 of 3 trades, Rs 980.40 of Rs 1,500 daily loss", res["results"][0]["info"][-1])
        three = two + [placed("TCS")]
        res = check.check([reliance()], cfg(), log=three, plan=plan(), rows=rows(), now=at(NOW))
        why = reasons(res)
        self.assertIn("trades: 3 entries placed today plus 0 passed earlier in this batch, your limit is 3 a day", why)
        self.assertIn("daily loss: Rs 1,470.60 already at risk today plus Rs 490.20 for this one = Rs 1,960.80, "
                      "over your Rs 1,500 daily loss limit", why)
        self.assertEqual(res["today"], {"orders_placed": 3, "entries_placed": 3, "loss_at_risk": 1470.6})
        yesterday = [placed(s, day="2026-10-28") for s in ("INFY", "SBIN", "TCS")]
        self.assertEqual(verdicts(check.check([reliance()], cfg(), log=yesterday, plan=plan(), rows=rows(),
                                              now=at(NOW))), ["PASS"])

    def test_only_the_latest_status_per_hash_counts(self):
        log = [placed("INFY", status="approved"), placed("INFY", status="placed"), placed("INFY", status="filled"),
               placed("SBIN", status="approved"),                                   # never placed
               placed("TCS", status="placed"), placed("TCS", status="failed"),      # failed after placing
               placed("HDFCBANK", status="approved"), placed("HDFCBANK", status="denied"),
               placed("INFY", side="sell", kind="exit", max_loss=None, ohash="hinfyexit", status="filled")]
        res = check.check([reliance()], cfg(), log=log, plan=plan(), rows=rows(), now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertEqual(res["today"], {"orders_placed": 2, "entries_placed": 1, "loss_at_risk": 490.2})
        self.assertEqual(check.placed_today(log, TODAY)[0]["status"], "filled")
        no_loss = [placed("INFY", max_loss=None)]
        res = check.check([reliance()], cfg(), log=no_loss, plan=plan(), rows=rows(), now=at(NOW))
        self.assertIn("has no max_loss_inr in the log", res["warnings"][0])

    def test_the_batch_counts_toward_the_daily_limits(self):
        orders = [co_order(s, 10, 100.0, 95.0, 120.0) for s in ("INFY", "SBIN", "TCS", "HDFCBANK")]
        res = check.check(orders, cfg(max_daily_loss_inr=160), rows=rows(), now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS", "PASS", "BLOCK"])
        why = reasons(res, 3)
        self.assertIn("trades: 0 entries placed today plus 3 passed earlier in this batch, your limit is 3 a day", why)
        self.assertIn("daily loss: Rs 150 already at risk today plus Rs 50 for this one = Rs 200, over your Rs 160 "
                      "daily loss limit", why)

    def test_entry_window_ist(self):
        cases = [("2026-10-29 09:29", "no new entries before 09:30 IST (it is 09:29)"),
                 ("2026-10-29 14:31", "no new entries after 14:30 IST (it is 14:31)"),
                 ("2026-10-29 09:10", "the market is in pre-open (Thu 2026-10-29 09:10 IST; continuous trading is "
                                      "09:15 to 15:15 for a CAS stock)"),
                 ("2026-10-29 08:00", "the market is closed (Thu 2026-10-29 08:00 IST"),
                 ("2026-10-29 15:16", "the market is closed (Thu 2026-10-29 15:16 IST; continuous trading is 09:15 to "
                                      "15:15 for a CAS stock)"),
                 ("2026-10-31 10:00", "the market is closed today (weekend, Sat 2026-10-31 10:00 IST)"),
                 ("2026-11-10 10:00", "the market is closed today (NSE holiday: Diwali Balipratipada"),
                 ("2026-10-29T03:59:00Z", "no new entries before 09:30 IST (it is 09:29)")]
        for now, want in cases:
            self.assertIn("window: " + want, reasons(self.one(reliance(), now=now)), now)
        for now in ("2026-10-29 09:30", "2026-10-29 14:30", "2026-10-29T04:00:00Z"):
            self.assertEqual(verdicts(self.one(reliance(), now=now)), ["PASS"], now)
        res = self.one(reliance(), now="2026-10-29 15:06", no_entries_after=None)
        self.assertIn("window: it is past your flat-by time 15:05 IST (it is 15:06): no new entries", reasons(res))
        off = dict(no_entries_before=None, no_entries_after=None, flat_by=None)
        self.assertEqual(verdicts(self.one(reliance(), now="2026-10-29 09:16", **off)), ["PASS"])
        self.assertIn("the market is closed today (weekend", reasons(self.one(reliance(), now="2026-10-31 10:00", **off)))
        res = self.one(reliance(), now="2027-01-05 10:00")
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("NSE's holiday list for 2027 is not in the pack yet", warnings(res))
        self.assertEqual(verdicts(self.one(reliance(), no_entries_after="2:30 PM", now="2026-10-29 14:30")), ["PASS"])
        with self.assertRaisesRegex(check.RiskError, "no_entries_before must be an IST time"):
            self.one(reliance(), no_entries_before="9.45xx")

    def test_a_non_cas_stock_trades_until_fifteen_thirty(self):
        o = co_order("SUZLON", 10, 60.0, 59.5, 61.0)
        off = dict(no_entries_after=None, flat_by=None)
        res = check.check([o], cfg(**off), rows=rows(), cas=set(), now=at("2026-10-29 15:20"))
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        res = check.check([o], cfg(**off), rows=rows(), cas={"SUZLON"}, now=at("2026-10-29 15:20"))
        self.assertIn("continuous trading is 09:15 to 15:15 for a CAS stock", reasons(res))
        res = check.check([o], cfg(**off), rows=rows(), plan=dict(plan(), symbol="SUZLON", cas=False),
                          now=at("2026-10-29 15:20"))
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        res = check.check([o], cfg(**off), rows=rows(), now=at("2026-10-29 15:20"))     # unknown: CAS assumed
        self.assertIn("for a CAS stock", reasons(res))

    def test_the_window_uses_ist_not_the_machine(self):
        self.assertEqual(at("2026-10-29 09:45"), ist("2026-10-29T09:45"))
        self.assertEqual(at("2026-10-29T04:15:00Z"), ist("2026-10-29T09:45"))
        self.assertEqual(at("2026-10-29T09:45:00+05:30").utcoffset(), datetime.timedelta(hours=5, minutes=30))
        self.assertEqual(check.iso(ist("2026-10-29T09:45")), "2026-10-29T09:45:00+05:30")

    def test_a_stale_orders_file_blocks_its_entries(self):
        res = check.check([reliance()], cfg(), plan=plan(), rows=rows(), now=at(NOW), created_at=ist("2026-10-28T10:20"))
        self.assertIn("stale: the orders file was written on 2026-10-28, not today (2026-10-29); run trader-budget again",
                      reasons(res))
        res = check.check([reliance()], cfg(), plan=plan(), rows=rows(), now=at(NOW), created_at=ist("2026-10-29T09:40"))
        self.assertEqual(verdicts(res), ["PASS"])

    def test_a_short_entry_only_when_allowed(self):
        short = reliance(side="sell", kite_transaction_type="SELL", kite_trigger_price=1185.8,
                         plan=dict(reliance()["plan"], stop=1185.8, target=1163.0))
        res = self.one(short)
        self.assertIn("short: selling RELIANCE you do not hold (0 shares in trader/portfolio.json); short selling is "
                      "off (\"allow_short\": false)", reasons(res))
        res = self.one(short, allow_short=True)
        r = res["results"][0]
        self.assertEqual((r["verdict"], r["kind"], r["max_loss_inr"], r["reward_risk"]), ("PASS", "entry", 490.2, 1.0))
        res = self.one(reliance(side="sell", kite_transaction_type="SELL"), allow_short=True)
        self.assertIn("stop: the stop 1163.00 must be above the plan's entry 1174.40 for a sell", reasons(res))


class FormatAndAsset(Offline):
    def one(self, order, **over):
        return check.check([order], cfg(**over), plan=plan(), rows=rows(), now=at(NOW))

    def test_the_kite_body(self):
        o = reliance()
        o.pop("kite")
        self.assertIn("format: no kite body (the Kite place_order fields); run trader-budget again", reasons(self.one(o)))
        self.assertIn("format: quantity must be a whole number of shares, got 43.5",
                      reasons(self.one(reliance(kite_quantity=43.5))))
        self.assertIn("format: quantity must be a whole number of shares, got 0", reasons(self.one(reliance(kite_quantity=0))))
        o = reliance()
        o["qty"] = 40
        self.assertIn("format: the order says qty 40 but the kite body says 43", reasons(self.one(o)))
        self.assertIn("format: the order says RELIANCE but the kite body says INFY",
                      reasons(self.one(reliance(kite_tradingsymbol="INFY"))))
        self.assertIn("format: the order says buy but the kite body says SELL",
                      reasons(self.one(reliance(kite_transaction_type="SELL"))))
        self.assertIn("format: validity must be DAY for an intraday order, got 'IOC'",
                      reasons(self.one(reliance(kite_validity="IOC"))))
        self.assertIn("format: tag must be letters and digits, at most 20, got 'dts orb!'",
                      reasons(self.one(reliance(kite_tag="dts orb!"))))
        self.assertIn("format: variety must be co or regular, got 'amo'", reasons(self.one(reliance(kite_variety="amo"))))
        self.assertIn("format: unknown order_type 'FOO'", reasons(self.one(reliance(kite_order_type="FOO"))))
        o = reliance()
        o["kite"].pop("exchange")
        self.assertIn("format: the kite body has no exchange", reasons(self.one(o)))
        self.assertIn("format: side must be buy or sell, got 'hold'", reasons(self.one(reliance(side="hold"))))
        res = check.check(["junk"], cfg(), now=at(NOW))
        self.assertIn("format: order #1 is not a JSON object", reasons(res))
        self.assertEqual(res["results"][0]["order_hash"], "malformed-1")

    def test_the_stop_leg(self):
        r = regular()
        r["stop_leg"]["quantity"] = 40
        self.assertIn("format: the stop_leg quantity 40 is not the entry's 43", reasons(self.one(r)))
        r = regular()
        r["stop_leg"]["order_type"] = "SL"
        self.assertIn("format: the stop_leg must be a SELL SL-M order", reasons(self.one(r)))
        r = regular()
        r["stop_leg"]["product"] = "CNC"
        self.assertIn("format: the stop_leg must be MIS", reasons(self.one(r)))
        r = regular()
        r["stop_leg"]["tradingsymbol"] = "INFY"
        self.assertIn("format: the stop_leg is for INFY, not RELIANCE", reasons(self.one(r)))
        r = regular()
        r["stop_leg"] = "yes"
        self.assertIn("format: stop_leg must be a Kite place_order body or null", reasons(self.one(r)))

    def test_product_exchange_and_fno(self):
        self.assertIn("product: CNC is not MIS; this pack trades intraday MIS only (no CNC, no NRML)",
                      reasons(self.one(reliance(kite_product="CNC"))))
        bse = reliance(exchange="BSE", kite_exchange="BSE")
        self.assertIn("exchange: BSE is off (\"allow_bse\": false); this pack trades NSE", reasons(self.one(bse)))
        self.assertEqual(verdicts(self.one(bse, allow_bse=True)), ["PASS"])
        self.assertIn("exchange: MCX is not an equity exchange", reasons(self.one(reliance(kite_exchange="MCX"))))
        fut = reliance(symbol="RELIANCE26NOVFUT", kite_tradingsymbol="RELIANCE26NOVFUT", kite_exchange="NFO")
        self.assertIn("asset: RELIANCE26NOVFUT is a futures or options contract; F&O is off (\"allow_fno\": false)",
                      reasons(self.one(fut)))
        res = self.one(fut, allow_fno=True)
        self.assertNotIn("asset:", reasons(res))
        self.assertIn("F&O order", warnings(res))
        opt = reliance(symbol="RELIANCE26NOV1200CE", kite_tradingsymbol="RELIANCE26NOV1200CE")
        self.assertIn("futures or options contract", reasons(self.one(opt)))
        self.assertIn("asset: RELIANC is not an NSE equity in Kite's instrument list",
                      reasons(self.one(reliance(symbol="RELIANC", kite_tradingsymbol="RELIANC"))))
        res = check.check([reliance(symbol="RELIANC", kite_tradingsymbol="RELIANC")], cfg(), now=at(NOW))
        self.assertNotIn("asset:", reasons(res))                                  # no Kite rows: no list to check

    def test_blocked_size_cash_and_position(self):
        self.assertIn("blocked: RELIANCE is on your blocked list", reasons(self.one(reliance(), blocked=["reliance"])))
        res = self.one(reliance(qty=64))
        self.assertIn("size: Rs 75,161.60 is over your Rs 75,000 max order", reasons(res))
        small = co_order("SUZLON", 1, 60.0, 59.5, 61.0)
        self.assertIn("size: Rs 60 is under your Rs 500 minimum order", reasons(self.one(small)))
        # Rs 40,000 cash and Rs 40,000 of INFY: the position cap (75% of Rs 80,000) holds, the cash does not
        pf = {"cash_inr": 40000, "positions": {"INFY": {"qty": 40, "avg": 1000.0, "product": "MIS"}}, "paper": True}
        res = check.check([reliance()], cfg(), portfolio=pf, prices={"INFY": 1000.0}, plan=plan(), rows=rows(),
                          now=at(NOW))
        self.assertEqual(reasons(res), "cash: Rs 50,499.20 is more than the Rs 32,000 you can use (cash Rs 40,000 minus "
                                       "the 10% buffer Rs 8,000)")
        res = check.check([reliance()], cfg(use_mis_leverage=True, mis_leverage_x=2), portfolio=pf,
                          prices={"INFY": 1000.0}, plan=plan(), rows=rows(), now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        res = check.check([reliance()], cfg(use_mis_leverage=True, mis_leverage_x=1.5), portfolio=pf,
                          prices={"INFY": 1000.0}, plan=plan(), rows=rows(), now=at(NOW))
        self.assertIn("the Rs 48,000 you can use (cash Rs 40,000 minus the 10% buffer Rs 8,000 x 1.5 MIS leverage)",
                      reasons(res))
        res = self.one(reliance(), max_position_pct=20)
        self.assertIn("position: RELIANCE would be Rs 50,499.20, 50.5% of the Rs 100,000 account, over your 20% cap",
                      reasons(res))
        with self.assertRaisesRegex(check.RiskError, "mis_leverage_x"):
            self.one(reliance(), use_mis_leverage=True, mis_leverage_x=0.5)

    def test_duplicates_and_the_daily_order_limit(self):
        res = check.check([reliance(), reliance()], cfg(), plan=plan(), rows=rows(), now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "BLOCK"])
        self.assertIn("duplicate: the same order twice in this batch", reasons(res, 1))
        h = check.order_hash(reliance())
        fills = [{"order_hash": h, "kite_order_id": "251029000012345", "filled_qty": 43, "average_price": 1174.6,
                  "at": "2026-10-29T09:41:02+05:30"}]
        res = check.check([reliance()], cfg(), fills=fills, plan=plan(), rows=rows(), now=at(NOW))
        self.assertIn("duplicate: this exact order was already placed today", reasons(res))
        res = check.check([reliance()], cfg(), log=[placed("RELIANCE", ohash=h)], plan=plan(), rows=rows(), now=at(NOW))
        self.assertIn("duplicate: this exact order was already placed today", reasons(res))
        res = check.check([reliance()], cfg(), log=[placed("RELIANCE", ohash=h, status="denied")], plan=plan(),
                          rows=rows(), now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS"])
        ten = [placed("S%d" % i, max_loss=1) for i in range(10)]
        res = check.check([reliance()], cfg(max_trades_per_day=0, max_daily_loss_inr=0), log=ten, plan=plan(),
                          rows=rows(), now=at(NOW))
        self.assertIn("daily limit: 10 placed today plus 0 passed earlier in this batch, your limit is 10 a day",
                      reasons(res))
        old = [placed("S%d" % i, day="2026-10-28") for i in range(10)]
        self.assertEqual(verdicts(check.check([reliance()], cfg(), log=old, plan=plan(), rows=rows(), now=at(NOW))),
                         ["PASS"])

    def test_live_account_warns(self):
        res = self.one(reliance(), paper=False)
        self.assertIn("LIVE account (\"paper\": false): these orders use real money", res["warnings"][0])

    def test_no_price_for_an_entry_is_a_block(self):
        o = co_order("INFY", 10, 100.0, 95.0, 120.0)
        o["plan"] = {}
        res = self.one(o)
        self.assertIn("price: no price for INFY to size a 10-share order", reasons(res))
        res = check.check([o], cfg(), prices={"INFY": 100.0}, rows=rows(), now=at(NOW))
        self.assertEqual((res["results"][0]["entry_ref_from"], res["results"][0]["verdict"]), ("price", "PASS"))


class Exits(Offline):
    PF = {"cash_inr": 49500.8, "paper": True,
          "positions": {"RELIANCE": {"qty": 43, "avg": 1174.4, "product": "MIS", "parent_order_id": "251029000012345",
                                     "entered_at": "2026-10-29T09:41:02+05:30"}}}
    CO_EXIT = {"symbol": "RELIANCE", "exchange": "NSE", "side": "sell", "qty": 43, "product": "MIS",
               "order_style": "co", "kite": {"variety": "co", "parent_order_id": "251029000012345",
                                             "order_id": "251029000012346"}, "stop_leg": None}
    SELL = {"symbol": "RELIANCE", "exchange": "NSE", "side": "sell", "qty": 43, "product": "MIS",
            "order_style": "regular", "kite": {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE",
                                               "transaction_type": "SELL", "order_type": "MARKET", "quantity": 43,
                                               "product": "MIS", "validity": "DAY", "tag": "dtsflat"}, "stop_leg": None}

    def test_getting_out_is_never_blocked_by_the_day_limits(self):
        full = [placed("RELIANCE", ohash="hrel"), placed("INFY"), placed("SBIN")] + \
               [placed("T%d" % i, max_loss=1) for i in range(7)]
        c = cfg(blocked=["RELIANCE"], max_order_inr=100, max_daily_loss_inr=50)
        for order in (self.CO_EXIT, self.SELL):
            res = check.check([order], c, portfolio=self.PF, log=full, prices={"RELIANCE": 1180.0}, plan=plan(),
                              rows=rows(), now=at("2026-10-29 15:08"))
            r = res["results"][0]
            self.assertEqual((r["verdict"], r["kind"], r["qty"]), ("PASS", "exit", 43), r["reasons"])
            why = warnings(res)
            for word in ("blocked:", "size:", "daily limit:"):
                self.assertIn("exit, not blocked: " + word, why)
            self.assertNotIn("window", why)
            self.assertEqual(res["cash_after"], 100240.8)
        res = check.check([self.SELL], cfg(), portfolio=self.PF, now=at("2026-10-31 12:00"))
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("no price for RELIANCE: valued at its average cost Rs 50,499.20", res["warnings"][0])

    def test_a_partial_exit_and_the_rest(self):
        half = dict(self.SELL, qty=20, kite=dict(self.SELL["kite"], quantity=20))
        res = check.check([half, half], cfg(), portfolio=self.PF, prices={"RELIANCE": 1180.0}, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS"])
        self.assertEqual([r["kind"] for r in res["results"]], ["exit", "exit"])
        self.assertIn("exit, not blocked: duplicate: the same order twice in this batch", warnings(res, 1))
        res = check.check([half, half, half], cfg(), portfolio=self.PF, prices={"RELIANCE": 1180.0}, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS", "BLOCK"])
        self.assertIn("short: selling RELIANCE you do not hold (3 shares in trader/portfolio.json)", reasons(res, 2))

    def test_format_product_and_short_still_apply_to_a_sell(self):
        bad = dict(self.SELL, kite=dict(self.SELL["kite"], product="CNC"))
        res = check.check([bad], cfg(), portfolio=self.PF, now=at(NOW))
        self.assertIn("product: CNC is not MIS", reasons(res))
        more = dict(self.SELL, qty=50, kite=dict(self.SELL["kite"], quantity=50))
        res = check.check([more], cfg(), portfolio=self.PF, now=at(NOW))
        self.assertIn("short: selling RELIANCE you do not hold (43 shares in trader/portfolio.json)", reasons(res))
        res = check.check([self.SELL], cfg(), now=at(NOW))
        self.assertIn("record the fill first (trader-broker)", reasons(res))
        res = check.check([dict(self.CO_EXIT, kite={"variety": "co", "parent_order_id": "1"})], cfg(),
                          portfolio=self.PF, now=at(NOW))
        self.assertIn("format: an exit of a cover order needs variety co, order_id and parent_order_id", reasons(res))
        res = check.check([dict(self.CO_EXIT, kite={})], cfg(), portfolio=self.PF, now=at(NOW))
        self.assertIn("format: no kite body", reasons(res))

    def test_buy_then_sell_in_one_batch(self):
        res = check.check([reliance(), self.SELL], cfg(), prices={"RELIANCE": 1174.4}, plan=plan(), rows=rows(),
                          now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS"])
        self.assertEqual(res["results"][1]["kind"], "exit")
        self.assertEqual(res["cash_after"], 100000.0)

    def test_the_portfolio_must_be_the_contract_shape(self):
        with self.assertRaisesRegex(check.RiskError, "cash_inr"):
            check.check([self.SELL], cfg(), portfolio={"cash": 1}, now=at(NOW))
        with self.assertRaisesRegex(check.RiskError, "keyed by symbol"):
            check.check([self.SELL], cfg(), portfolio={"cash_inr": 1, "positions": []}, now=at(NOW))
        with self.assertRaisesRegex(check.RiskError, "no trader/portfolio.json and no budget_inr"):
            check.check([self.SELL], {"budget_inr": None}, now=at(NOW))


class Cli(Offline):
    def test_prices_file_freshness(self):
        with tempfile.TemporaryDirectory() as d:
            pp = os.path.join(d, "prices.json")
            with open(pp, "w") as fh:
                json.dump({"as_of": "2026-10-29T09:53:00+05:30",
                           "prices": {"RELIANCE": {"last": 1178.0, "time": QUOTE_TIME, "prev_close": 1158.0,
                                                   "source": "kite"}}}, fh)
            code, text, _ = run(base_args(d, now="2026-10-29 09:55", prices=pp) + ["--no-write"])
            self.assertEqual(code, 1)
            self.assertIn("43 shares x 15.00 risk a share = Rs 645", text)
            self.assertEqual(run(base_args(d, now="2026-10-29 10:30", prices=pp) + ["--no-write"])[0], 0)
            code, text, _ = run(base_args(d, prices=PRICES_PATH) + ["--no-write"])      # after-close quote: future
            self.assertEqual(code, 0)
            self.assertIn("older than 15 minutes", text)

    def test_the_regular_fixture_and_a_log_file(self):
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "log.jsonl")
            with open(log, "w") as fh:
                fh.write(json.dumps(placed("INFY")) + "\n")
                fh.write("not json\n")
                fh.write(json.dumps(placed("SBIN")) + "\n")
            out = os.path.join(d, "verdicts.json")
            code, text, _ = run(base_args(d, orders=REGULAR_PATH, log=log, out=out))
            self.assertEqual(code, 0)
            self.assertIn("WARNING: %s line 2 is not JSON: skipped" % log, text)
            self.assertIn("today before this: 2 of 3 trades, Rs 980.40 of Rs 1,500 daily loss", text)
            with open(out) as fh:
                rec = json.load(fh)
            self.assertEqual(list(rec["verdicts"]), [check.order_hash(regular())])
            with open(log, "a") as fh:
                fh.write(json.dumps(placed("TCS")) + "\n")
            code, text, _ = run(base_args(d, orders=REGULAR_PATH, log=log) + ["--no-write"])
            self.assertEqual(code, 1)
            self.assertIn("- trades: 3 entries placed today", text)

    def test_no_session_warns_and_reads_the_plan_tick(self):
        with tempfile.TemporaryDirectory() as d:
            cwd, env = os.getcwd(), os.environ.get("KITE_API_KEY")
            os.chdir(d)
            os.environ.pop("KITE_API_KEY", None)
            try:
                args = base_args(d) + ["--no-write"]
                i = args.index("--offline")
                del args[i:i + 2]
                code, text, _ = run(args)
            finally:
                os.chdir(cwd)
                if env is not None:
                    os.environ["KITE_API_KEY"] = env
        self.assertEqual(code, 0)
        self.assertIn("WARNING: no Kite session (", text)
        self.assertIn("PASS  #1 buy RELIANCE 43 shares, entry", text)
        self.assertEqual(self.net.requests, [])

    def test_instruments_from_a_session(self):
        with open(CSV) as fh:
            csv_text = fh.read()
        nfo = ("instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,"
               "tick_size,lot_size,instrument_type,segment,exchange\n"
               '1,1,RELIANCE26NOVFUT,"RELIANCE",0,2026-11-24,0,0.1,500,FUT,NFO-FUT,NFO\n')
        with tempfile.TemporaryDirectory() as d:
            cwd, env = os.getcwd(), os.environ.get("KITE_API_KEY")
            os.chdir(d)
            os.environ["KITE_API_KEY"] = "key"
            try:
                kite.save_session({"access_token": "tok", "user_id": "AB1234"}, nse.now_ist())
                self.net.answers.extend([FakeResponse(csv_text, "text/csv"), FakeResponse(nfo, "text/csv")])
                rows_, cas, warns = check.load_instruments(None)
            finally:
                os.chdir(cwd)
                if env is None:
                    os.environ.pop("KITE_API_KEY", None)
                else:
                    os.environ["KITE_API_KEY"] = env
        self.assertEqual((rows_["RELIANCE"]["tick_size"], cas, warns), (0.1, {"RELIANCE"}, []))
        self.assertEqual(len(self.net.requests), 2)
        rows_, cas, warns = check.load_instruments(CSV)
        self.assertEqual((sorted(rows_)[:2], cas, warns), (["HDFCBANK", "ICICIBANK"], None, []))

    def test_errors_are_one_line(self):
        with tempfile.TemporaryDirectory() as d:
            code, _, err = run(["--orders", "/nonexistent.json", "--no-write"])
            self.assertEqual(code, 1)
            self.assertIn("no orders file", err)
            code, _, err = run(base_args(d, now="noon") + ["--no-write"])
            self.assertEqual(code, 1)
            self.assertIn("--now wants", err)
            self.assertNotIn("Traceback", err)
            code, _, err = run(base_args(d, today="monday") + ["--no-write"])
            self.assertEqual(code, 1)
            self.assertIn("--today wants", err)
            empty = os.path.join(d, "empty.json")
            with open(empty, "w") as fh:
                json.dump({"orders": []}, fh)
            code, _, err = run(base_args(d, orders=empty) + ["--no-write"])
            self.assertEqual(code, 1)
            self.assertIn("no orders in", err)
            code, _, err = run(base_args(d, offline=os.path.join(d, "none.csv")) + ["--no-write"])
            self.assertEqual(code, 1)
            self.assertIn("cannot read", err)


if __name__ == "__main__":
    unittest.main()
