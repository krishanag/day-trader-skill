import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

ticket = load("trader-broker", "ticket")
kite = ticket.kite   # the module ticket.py imported, so the network fakes reach it

BFIX = os.path.join(FIX, "trader-broker")
QDIR = os.path.join(FIX, "trader-alerts")
CFG_PATH = os.path.join(BFIX, "config.json")
ORDERS_PATH = os.path.join(BFIX, "orders.json")
PLAN_PATH = os.path.join(BFIX, "plan.json")
NOW = "2026-10-29 10:15"
TODAY = "2026-10-29"
HASH = "e1bd9e7bd8d1"   # sha256 of {"kite": the fixture's Kite body, "stop_leg": null}, first 12 hex digits
RELIANCE_CALL = {"variety": "co", "exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "BUY",
                 "order_type": "MARKET", "quantity": 43, "product": "MIS", "trigger_price": 1163.0,
                 "validity": "DAY", "tag": "dtsorb"}
RELIANCE_PLAIN = ("BUY 43 RELIANCE on NSE, MIS (intraday), cover order, market / stop-loss trigger 1163.00 "
                  "(held by the exchange as the cover order's second leg) / target 1185.80 (trader-alerts "
                  "watches it; it is not an order) / max loss about Rs 490.20 if the stop fills at 1163.00 "
                  "(it can fill worse in a fast market) / flat by 15:05 IST")
INFY = {"symbol": "INFY", "side": "buy", "qty": 20, "order_style": "co",
        "kite": {"variety": "co", "exchange": "NSE", "tradingsymbol": "INFY", "transaction_type": "BUY",
                 "order_type": "MARKET", "quantity": 20, "product": "MIS", "trigger_price": 1120.0,
                 "validity": "DAY", "tag": "dtsorb"},
        "plan": {"entry": 1130.0, "stop": 1120.0}}
BATCH = ["RELIANCE", "INFY", "TCS", "SBIN", "TATASTEEL"]


def reliance(**over):
    with open(ORDERS_PATH) as fh:
        o = json.load(fh)["orders"][0]
    o.update(over)
    return o


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ticket.main(argv)
    return code, out.getvalue(), err.getvalue()


class FakeResponse(io.BytesIO):
    def __init__(self, body, ctype="application/json"):
        super().__init__(body.encode())
        self.headers = {"Content-Type": ctype}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeNet:
    """Answers Kite's /quote from the saved quote files, and records each request."""

    def __init__(self):
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        data = {}
        for key in req.full_url.split("?", 1)[1].split("&"):
            if key.startswith("i=NSE%3A"):
                sym = key[len("i=NSE%3A"):]
                path = os.path.join(QDIR, "kite-quote-%s.json" % sym)
                if os.path.exists(path):
                    with open(path) as fh:
                        data["NSE:" + sym] = json.load(fh)
        return FakeResponse(json.dumps({"status": "success", "data": data}))


class ExplicitYes(unittest.TestCase):
    def yes(self, reply, order=None, batch=BATCH):
        return ticket.parse_yes(reply, order or reliance(), batch)[0]

    def test_plain_yes_counts(self):
        for r in ("yes", "Yes.", "YES", "y", "yes place it", "Yes, buy it", "yes RELIANCE", "yes 43 RELIANCE",
                  "yes Rs 490.20", "yes trigger 1163", "yes target 1185.80", "yes 43"):
            self.assertTrue(self.yes(r), r)

    def test_anything_else_does_not(self):
        for r in ("", "sure", "ok", "okay", "go", "do it", "yeah", "yep", "absolutely", "send it", "haan",
                  "yes to all", "yes, all of them", "yes to the rest", "yes both",
                  "yes INFY", "yes 50", "yes but make it 50", "yes?", "no", "not yes",
                  "yes, wait", "yes if it is under 1170", "yes 44 RELIANCE", "yes, stop 1163"):
            self.assertFalse(self.yes(r), r)

    def test_amount_must_match_this_order(self):
        for r in ("yes", "yes 20 INFY", "yes 1120", "yes max loss 200"):
            self.assertTrue(self.yes(r, INFY, ["INFY", "RELIANCE"]), r)
        for r in ("yes 21", "yes RELIANCE", "yes 30 INFY"):
            self.assertFalse(self.yes(r, INFY, ["INFY", "RELIANCE"]), r)


class OrderShape(unittest.TestCase):
    def test_the_order_hash_is_the_packs_function(self):
        o = reliance()
        self.assertEqual(ticket.order_hash(o), HASH)
        self.assertEqual(ticket.order_hash({"kite": RELIANCE_CALL}), HASH)
        self.assertEqual(ticket.order_hash(reliance(why="edited", plan={})), HASH)   # why and plan never count
        changed = reliance(kite=dict(RELIANCE_CALL, quantity=44))
        self.assertNotEqual(ticket.order_hash(changed), HASH)
        leg = {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "SELL",
               "order_type": "SL-M", "quantity": 43, "product": "MIS", "trigger_price": 1163.0, "validity": "DAY"}
        regular = reliance(order_style="regular", kite=dict(RELIANCE_CALL, variety="regular"), stop_leg=leg)
        edited_leg = reliance(order_style="regular", kite=dict(RELIANCE_CALL, variety="regular"),
                              stop_leg=dict(leg, trigger_price=1160.0))
        self.assertNotEqual(ticket.order_hash(regular), ticket.order_hash(edited_leg))   # the stop leg counts too
        self.assertNotEqual(ticket.order_hash(regular), HASH)
        with self.assertRaises(ticket.TicketError):
            ticket.order_hash({"symbol": "RELIANCE", "side": "buy"})

    def test_the_mcp_call_is_the_kite_body_exactly(self):
        self.assertEqual(ticket.mcp_calls(reliance()), [("place_order", RELIANCE_CALL)])
        regular = reliance(order_style="regular",
                           kite=dict(RELIANCE_CALL, variety="regular"),
                           stop_leg={"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE",
                                     "transaction_type": "SELL", "order_type": "SL-M", "quantity": 43,
                                     "product": "MIS", "trigger_price": 1163.0, "validity": "DAY", "tag": "dtsorb"})
        del regular["kite"]["trigger_price"]
        calls = ticket.mcp_calls(regular)
        self.assertEqual([t for t, _ in calls], ["place_order", "place_order"])
        self.assertEqual(calls[1][1]["order_type"], "SL-M")
        co_exit = {"symbol": "RELIANCE", "side": "sell", "qty": 43, "kind": "exit", "order_style": "co",
                   "kite": {"variety": "co", "parent_order_id": "251029000111", "order_id": "251029000112"}}
        self.assertEqual(ticket.mcp_calls(co_exit),
                         [("cancel_order", {"variety": "co", "order_id": "251029000112"})])

    def test_plain_english_and_the_max_loss_in_rupees(self):
        o = reliance()
        self.assertEqual(ticket.plain(o), RELIANCE_PLAIN)
        self.assertEqual(ticket.max_loss(o), (490.2, 1163.0))
        no_plan = reliance(plan=None)
        self.assertEqual(ticket.max_loss(no_plan), (None, 1163.0))
        from_entry = reliance(plan={"entry": 1174.4})
        self.assertEqual(ticket.max_loss(from_entry), (490.2, 1163.0))
        limit = reliance(kite=dict(RELIANCE_CALL, order_type="LIMIT", price=1175.0))
        self.assertEqual(ticket.plain(limit).split(" / ")[0],
                         "BUY 43 RELIANCE on NSE, MIS (intraday), cover order, limit 1175.00")
        self.assertEqual(ticket.max_loss(limit), (516.0, 1163.0))
        self.assertEqual(ticket.rupees(1167.4), "Rs 1,167.40")
        self.assertEqual(ticket.rupees(50198.2), "Rs 50,198.20")

    def test_kind_entry_or_exit(self):
        self.assertEqual(ticket.kind_of(reliance()), "entry")
        self.assertEqual(ticket.kind_of({"side": "sell", "kite": {}}), "exit")
        self.assertEqual(ticket.kind_of({"side": "buy", "kind": "exit", "kite": {}}), "exit")

    def test_the_clock_is_ist(self):
        now = ticket.parse_now(NOW)
        self.assertEqual(ticket.iso(now), "2026-10-29T10:15:00+05:30")
        self.assertEqual(ticket.iso(ticket.parse_now("2026-10-29T04:45:00+00:00")), "2026-10-29T10:15:00+05:30")
        with self.assertRaises(ticket.TicketError):
            ticket.parse_now("tomorrow")
        self.assertEqual(ticket.flat_by_of({}), datetime.time(15, 5))
        self.assertEqual(ticket.flat_by_of({"flat_by": "14:50"}, {"flat_by": "15:00"}), datetime.time(15, 0))


class DesktopConfig(unittest.TestCase):
    def test_example_is_self_hosted_stdio_with_placeholder_keys(self):
        with open(os.path.join(ROOT, "trader-broker", "claude_desktop_config.example.json")) as fh:
            server = json.load(fh)["mcpServers"]["kite"]
        self.assertIn("kite-mcp-server", server["command"])
        self.assertEqual(server["env"]["APP_MODE"], "stdio")
        for k in ("KITE_API_KEY", "KITE_API_SECRET"):
            self.assertIn("NEVER_IN_CHAT", server["env"][k])
        self.assertNotIn("args", server)


class Flow(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.orders = os.path.join(self.d, "orders.json")
        shutil.copy(ORDERS_PATH, self.orders)
        self.cfg = os.path.join(self.d, "config.json")
        shutil.copy(CFG_PATH, self.cfg)
        self.verdicts = os.path.join(self.d, "verdicts.json")
        self.log = os.path.join(self.d, "log.jsonl")
        self.pf = os.path.join(self.d, "portfolio.json")
        self.fills = os.path.join(self.d, "fills.json")
        self.none = os.path.join(self.d, "none")
        self.saved = (kite._urlopen, kite._sleep, kite.load_session, dict(os.environ))
        self.net = FakeNet()
        kite._urlopen = self.net
        kite._sleep = lambda s: None
        kite.load_session = lambda now, path=None: {"access_token": "tok", "user_id": "AB1234"}
        os.environ["KITE_API_KEY"] = "key"

    def tearDown(self):
        kite._urlopen, kite._sleep, kite.load_session, env = self.saved
        os.environ.clear()
        os.environ.update(env)
        shutil.rmtree(self.d)

    def args(self, *extra, **kw):
        return list(extra) + ["--orders", self.orders, "--config", self.cfg, "--verdicts", self.verdicts,
                              "--log", self.log, "--portfolio", self.pf, "--fills", self.fills,
                              "--now", kw.get("now", NOW)]

    def set_cfg(self, **over):
        with open(self.cfg) as fh:
            c = json.load(fh)
        c.update(over)
        with open(self.cfg, "w") as fh:
            json.dump(c, fh)

    def verdict(self, h=HASH, verdict="PASS", date=TODAY, reasons=(), kind="entry", **extra):
        doc = {"created_by": "trader-risk", "date": date, "checked_at": date + "T10:14:00+05:30",
               "verdicts": {h: dict({"verdict": verdict, "reasons": list(reasons), "warnings": [], "kind": kind},
                                    **extra)}}
        with open(self.verdicts, "w") as fh:
            json.dump(doc, fh)

    def current_hash(self):
        with open(self.orders) as fh:
            return ticket.order_hash(json.load(fh)["orders"][0])

    def log_lines(self):
        return ticket.read_log(self.log)

    def portfolio(self):
        with open(self.pf) as fh:
            return json.load(fh)

    def test_nothing_is_ready_before_the_risk_check(self):
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 1)
        self.assertIn("NOT READY: trader-risk has not checked", text)
        self.assertNotIn("Reply \"yes\"", text)
        code, _, err = run(self.args("approve", "--hash", HASH, "--yes", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("not ready", err)
        self.assertFalse(os.path.exists(self.log))

    def test_the_full_two_yes_paper_flow(self):
        self.verdict()
        code, text, _ = run(self.args("show", "--hash", HASH))
        self.assertEqual(code, 0)
        self.assertIn("2026-10-29 10:15 IST, NSE open.", text)
        self.assertIn("ORDER 1 of 1, PAPER, Zerodha Kite, hash " + HASH, text)
        for part in RELIANCE_PLAIN.split(" / "):
            self.assertIn("  " + part + "\n", text)
        self.assertIn("READY: trader-risk PASS", text)
        self.assertIn('"variety": "co"', text)
        self.assertIn('"trigger_price": 1163.0', text)
        self.assertNotIn('"plan"', text)
        self.assertNotIn('"why"', text)
        self.assertIn("Paper mode: nothing goes to Zerodha", text)
        self.assertIn("Reply \"yes\"", text)
        self.assertIn("not financial advice", text)

        code, text, _ = run(self.args("approve", "--hash", HASH, "--yes", "sure"))
        self.assertEqual(code, 1)
        self.assertIn("Not recorded", text)
        self.assertFalse(os.path.exists(self.log))

        code, text, _ = run(self.args("approve", "--hash", HASH, "--yes", "yes"))
        self.assertEqual(code, 0)
        self.assertIn("Recorded your yes for RELIANCE", text)
        self.assertIn("fill --hash " + HASH, text)
        appr = self.log_lines()[0]
        self.assertEqual((appr["status"], appr["reply"], appr["kind"], appr["max_loss_inr"], appr["date"]),
                         ("approved", "yes", "entry", 490.2, TODAY))
        self.assertEqual((appr["symbol"], appr["side"], appr["qty"], appr["paper"], appr["stop"]),
                         ("RELIANCE", "buy", 43, True, 1163.0))
        self.assertEqual(appr["at"], "2026-10-29T10:15:00+05:30")
        code, _, err = run(self.args("approve", "--hash", HASH, "--yes", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("APPROVED", err)

        # the paper fill: Kite's last price (the saved 15:59 quote, 1167.40) through the fake network
        code, text, _ = run(self.args("fill", "--hash", HASH, now="2026-10-29 10:16"))
        self.assertEqual(code, 0, text)
        self.assertIn("Paper fill: BUY 43 RELIANCE at Kite's last price Rs 1,167.40", text)
        self.assertIn("Nothing was sent to Zerodha", text)
        self.assertIn("Cash now Rs 49,801.80; 1 open position(s).", text)
        req = self.net.requests[0]
        self.assertIn("/quote?i=NSE%3ARELIANCE", req.full_url)
        self.assertEqual(req.get_header("Authorization"), "token key:tok")
        pf = self.portfolio()
        self.assertEqual(pf["cash_inr"], 49801.8)
        self.assertEqual(pf["positions"]["RELIANCE"],
                         {"qty": 43, "avg": 1167.4, "product": "MIS", "order_style": "co",
                          "entered_at": "2026-10-29T10:16:00+05:30", "parent_order_id": "PAPER-" + HASH,
                          "stop_order_id": "PAPER-" + HASH + "-SL"})
        self.assertIs(pf["paper"], True)
        with open(self.fills) as fh:
            fills = json.load(fh)
        self.assertEqual(fills, [{"order_hash": HASH, "kite_order_id": "PAPER-" + HASH, "symbol": "RELIANCE",
                                  "side": "buy", "filled_qty": 43, "average_price": 1167.4,
                                  "at": "2026-10-29T10:16:00+05:30", "paper": True,
                                  "parent_order_id": "PAPER-" + HASH}])
        filled = self.log_lines()[-1]
        self.assertEqual((filled["status"], filled["max_loss_inr"], filled["kind"], filled["qty"], filled["price"]),
                         ("filled", 490.2, "entry", 43, 1167.4))

        code, text, _ = run(self.args("show", "--hash", HASH))
        self.assertEqual(code, 1)
        self.assertIn("DONE: filled", text)
        code, text, _ = run(["log", "--log", self.log, "--now", NOW])
        self.assertEqual(code, 0)
        self.assertIn("1 entry placed today", text)
        self.assertIn("max loss Rs 490.20", text)
        self.assertIn('reply "yes"', text)
        code, text, _ = run(["positions", "--portfolio", self.pf, "--config", self.cfg, "--now", NOW])
        self.assertEqual(code, 0)
        self.assertIn("PAPER, cash Rs 49,801.80, 1 open position(s).", text)
        self.assertIn("RELIANCE        43 @ Rs 1,167.40  MIS, cover order, parent PAPER-" + HASH, text)
        self.assertIn("Flat by 15:05 IST (Zerodha squares off MIS in F&O stocks at 15:12, Rs 50 + GST)", text)

    def test_exit_by_the_flat_by_paper(self):
        self.verdict()
        run(self.args("approve", "--hash", HASH, "--yes", "yes"))
        run(self.args("fill", "--hash", HASH, now="2026-10-29 10:16"))
        late = "2026-10-29 15:05"
        code, text, _ = run(["exit", "RELIANCE.NS", "--orders", self.orders, "--portfolio", self.pf,
                             "--plan", PLAN_PATH, "--config", self.cfg, "--now", late])
        self.assertEqual(code, 0)
        self.assertIn("Exit RELIANCE: EXIT 43 RELIANCE: exit the cover order (parent PAPER-%s, stop leg PAPER-%s-SL), "
                      "MIS (flat by 15:05 IST)." % (HASH, HASH), text)
        self.assertIn("replaced 1 earlier proposal", text)
        self.assertIn("never blocked by the daily", text)
        self.assertIn("Paper: ticket.py fill --hash H", text)
        with open(self.orders) as fh:
            doc = json.load(fh)
        self.assertEqual((doc["created_by"], doc["status"], doc["paper"]), ("trader-broker", "proposed", True))
        order = doc["orders"][0]
        self.assertEqual(order["kite"], {"variety": "co", "parent_order_id": "PAPER-" + HASH,
                                         "order_id": "PAPER-" + HASH + "-SL"})
        self.assertEqual((order["side"], order["qty"], order["kind"], order["order_style"], order["product"]),
                         ("sell", 43, "exit", "co", "MIS"))
        self.assertEqual(order["plan"]["flat_by"], "15:05")
        h = ticket.order_hash(order)
        self.assertNotEqual(h, HASH)

        code, text, _ = run(self.args("show", now=late))
        self.assertEqual(code, 1)
        self.assertIn("NOT READY", text)                       # the exit still needs its own PASS
        self.verdict(h, kind="exit")
        code, text, _ = run(self.args("show", now=late))
        self.assertEqual(code, 0)
        self.assertIn("READY", text)
        self.assertIn("The Kite MCP call it stands for, cancel_order:", text)
        self.assertIn('"order_id": "PAPER-%s-SL"' % HASH, text)
        self.assertNotIn("past your flat-by", text)            # that note is for entries only
        code, text, _ = run(self.args("approve", "--hash", h, "--yes", "yes sell 43 RELIANCE", now=late))
        self.assertEqual(code, 0, text)
        code, text, _ = run(self.args("fill", "--hash", h, now="2026-10-29 15:06"))
        self.assertEqual(code, 0, text)
        self.assertIn("Paper fill: SELL 43 RELIANCE at Kite's last price Rs 1,167.40", text)
        self.assertIn("Realized +Rs 0.00 on RELIANCE.", text)
        pf = self.portfolio()
        self.assertEqual((pf["cash_inr"], pf["positions"]), (100000.0, {}))
        with open(self.fills) as fh:
            self.assertEqual(json.load(fh)[1]["realized_inr"], 0.0)
        statuses = [r["status"] for r in self.log_lines()]
        self.assertEqual(statuses, ["approved", "filled", "approved", "filled"])
        self.assertEqual(self.log_lines()[-1]["kind"], "exit")
        code, _, err = run(["exit", "RELIANCE", "--orders", self.orders, "--portfolio", self.pf, "--now", late])
        self.assertEqual(code, 1)
        self.assertIn("nothing to exit", err)
        code, _, err = run(["flatten", "--orders", self.orders, "--portfolio", self.pf, "--now", late])
        self.assertEqual(code, 1)
        self.assertIn("nothing to flatten", err)
        code, text, _ = run(["positions", "--portfolio", self.pf, "--config", self.cfg, "--now", late])
        self.assertEqual(code, 1)
        self.assertIn("no open positions", text)

    def test_flatten_every_position_and_the_clock_notes(self):
        pf = {"cash_inr": 1000.0, "paper": True, "positions": {
            "TCS": {"qty": 10, "avg": 2470.0, "product": "MIS", "order_style": "regular", "stop_order_id": "251029000777"},
            "INFY": {"qty": 20, "avg": 1130.0, "product": "MIS", "order_style": "co",
                     "parent_order_id": "251029000555", "stop_order_id": "251029000556"}}}
        with open(self.pf, "w") as fh:
            json.dump(pf, fh)
        code, text, _ = run(["flatten", "--orders", self.orders, "--portfolio", self.pf, "--config", self.cfg,
                             "--now", "2026-10-29 15:13"])
        self.assertEqual(code, 0)
        self.assertIn("Wrote 2 ticket(s)", text)
        self.assertIn("note: it is past 15:12: Zerodha may already have squared off", text)
        with open(self.orders) as fh:
            orders = json.load(fh)["orders"]
        self.assertEqual([o["symbol"] for o in orders], ["INFY", "TCS"])
        self.assertEqual(orders[0]["kite"], {"variety": "co", "parent_order_id": "251029000555",
                                             "order_id": "251029000556"})
        tcs = orders[1]
        self.assertEqual(tcs["kite"], {"variety": "regular", "exchange": "NSE", "tradingsymbol": "TCS",
                                       "transaction_type": "SELL", "order_type": "MARKET", "quantity": 10,
                                       "product": "MIS", "validity": "DAY", "tag": "dtsexit"})
        self.assertEqual(ticket.mcp_calls(tcs), [("cancel_order", {"variety": "regular", "order_id": "251029000777"}),
                                                 ("place_order", tcs["kite"])])
        self.assertEqual(ticket.plain(tcs), "SELL 10 TCS on NSE, MIS (intraday), regular order, market / "
                                            "cancel the stop-loss leg 251029000777 first")
        # after the close: an MIS position does not survive the day
        code, text, _ = run(["flatten", "--orders", self.orders, "--portfolio", self.pf, "--config", self.cfg,
                             "--now", "2026-10-29 15:40"])
        self.assertIn("never carries overnight", text)
        # a cover order without its stop leg id cannot be exited blind
        pf["positions"]["INFY"].pop("stop_order_id")
        with open(self.pf, "w") as fh:
            json.dump(pf, fh)
        code, _, err = run(["exit", "INFY", "--orders", self.orders, "--portfolio", self.pf, "--now", NOW])
        self.assertEqual(code, 1)
        self.assertIn("stop leg id is not recorded", err)
        self.assertIn("--stop-order-id", err)
        code, _, _ = run(["exit", "INFY", "--stop-order-id", "251029000556", "--orders", self.orders,
                          "--portfolio", self.pf, "--now", NOW])
        self.assertEqual(code, 0)

    def test_exit_with_the_broker_qty(self):
        code, text, _ = run(["exit", "reliance", "--qty", "43", "--orders", self.orders, "--portfolio", self.none,
                             "--plan", self.none, "--config", self.cfg, "--now", NOW])
        self.assertEqual(code, 1)      # no parent order id to exit a cover order with
        self.set_cfg(order_style="regular")
        code, text, _ = run(["exit", "reliance", "--qty", "43", "--orders", self.orders, "--portfolio", self.none,
                             "--plan", self.none, "--config", self.cfg, "--now", NOW])
        self.assertEqual(code, 0, text)
        self.assertIn("Check it against get_positions RELIANCE", text)
        self.assertEqual(run(["exit", "RELIANCE", "--qty", "0", "--orders", self.orders])[0], 1)
        self.assertEqual(run(["exit", "RELIANCE", "--qty", "2.5", "--orders", self.orders])[0], 1)

    def test_an_edited_order_is_not_ready(self):
        self.verdict()
        with open(self.orders) as fh:
            doc = json.load(fh)
        doc["orders"][0]["kite"]["quantity"] = 44
        with open(self.orders, "w") as fh:
            json.dump(doc, fh)
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 1)
        self.assertIn("no verdict for this exact order", text)
        code, _, err = run(self.args("approve", "--hash", HASH, "--yes", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("no order with hash", err)

    def test_yesterdays_check_does_not_count(self):
        self.verdict(date="2026-10-28")
        code, text, _ = run(self.args("show", "--hash", HASH))
        self.assertEqual(code, 1)
        self.assertIn("not today", text)

    def test_blocked_orders_are_never_ready(self):
        self.verdict(verdict="BLOCK", reasons=["window: no new entries before 09:30"])
        code, text, _ = run(self.args("show", "--hash", HASH, now="2026-10-29 09:20"))
        self.assertEqual(code, 1)
        self.assertIn("BLOCKED by trader-risk: window: no new entries before 09:30", text)
        code, _, err = run(self.args("approve", "--hash", HASH, "--yes", "yes", now="2026-10-29 09:20"))
        self.assertEqual(code, 1)
        self.assertIn("BLOCKED", err)

    def test_the_results_list_shape_is_read_too(self):
        with open(self.verdicts, "w") as fh:
            json.dump({"checked_at": TODAY + "T10:14:00+05:30",
                       "results": [{"order_hash": HASH, "verdict": "PASS", "warnings": ["cash: tight"]}]}, fh)
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 0)
        self.assertIn("note: cash: tight", text)

    def test_the_clock_through_the_day(self):
        self.verdict()
        code, text, _ = run(self.args("show", now="2026-10-29 09:05"))
        self.assertIn("NSE pre-open", text)
        self.assertIn("note: NSE is pre-open right now (09:05 IST); an order waits for the open at 2026-10-29 09:15", text)
        code, text, _ = run(self.args("show", now="2026-10-29 15:06"))
        self.assertIn("note: it is past your flat-by 15:05: a new MIS entry now would be squared off by Zerodha "
                      "at 15:12 in F&O stocks (Rs 50 + GST each time), 15:30 otherwise", text)
        code, text, _ = run(self.args("show", now="2026-10-29 15:20"))
        self.assertIn("NSE after-close", text)
        self.verdict(date="2026-10-20")
        code, text, _ = run(self.args("show", now="2026-10-20 11:00"))
        self.assertIn("note: the market is closed today (NSE holiday: Dussehra); nothing can be placed until "
                      "2026-10-21 09:15 IST", text)
        self.verdict(date="2026-10-31")
        code, text, _ = run(self.args("show", now="2026-10-31 11:00"))
        self.assertIn("weekend", text)
        code, text, _ = run(self.args("show", "--json", now="2026-10-29 15:06"))
        doc = json.loads(text)
        self.assertEqual((doc[0]["order_hash"], doc[0]["max_loss_inr"], doc[0]["flat_by"], doc[0]["paper"]),
                         (HASH, 490.2, "15:05", True))
        self.assertEqual(doc[0]["calls"], [{"tool": "place_order", "arguments": RELIANCE_CALL}])
        self.assertTrue(any("past your flat-by" in n for n in doc[0]["notes"]))

    def test_result_needs_an_approval_and_decline_is_logged(self):
        self.verdict()
        code, _, err = run(self.args("result", "--hash", HASH, "--status", "placed"))
        self.assertEqual(code, 1)
        self.assertIn("no yes recorded", err)
        code, _, err = run(self.args("fill", "--hash", HASH))
        self.assertEqual(code, 1)
        self.assertIn("no yes recorded", err)
        code, text, _ = run(self.args("decline", "--hash", HASH, "--reply", "no thanks"))
        self.assertEqual(code, 0)
        self.assertIn("Nothing was sent", text)
        code, text, _ = run(self.args("show", "--hash", HASH))
        self.assertIn("DONE: declined", text)
        self.assertEqual(self.log_lines()[0]["reply"], "no thanks")

    def test_the_live_flow_records_kites_ids(self):
        self.set_cfg(paper=False)
        self.verdict()
        code, text, _ = run(self.args("show", "--hash", HASH))
        self.assertIn("LIVE ACCOUNT, REAL MONEY", text)
        self.assertIn("Claude will call the Kite MCP tool place_order with exactly:", text)
        self.assertEqual(json.loads(text[text.index("{"):text.index("}\n") + 1]), RELIANCE_CALL)
        self.assertIn("Allow once", text)
        code, text, _ = run(self.args("approve", "--hash", HASH, "--yes", "yes"))
        self.assertEqual(code, 0)
        self.assertIn("Now call the Kite MCP tool place_order with exactly:", text)
        self.assertIn("result --hash " + HASH, text)
        self.assertIs(self.log_lines()[0]["paper"], False)
        code, _, err = run(self.args("fill", "--hash", HASH))
        self.assertEqual(code, 1)
        self.assertIn("live mode: pass the fill from the MCP result", err)
        self.assertEqual(self.net.requests, [])            # live never reads a quote for the fill
        code, text, _ = run(self.args("result", "--hash", HASH, "--status", "placed", "--order-id", "251029000123",
                                      now="2026-10-29 10:16"))
        self.assertEqual(code, 0)
        self.assertIn("get_order_history 251029000123", text)
        self.assertIn("--parent-order-id 251029000123", text)
        code, text, _ = run(self.args("fill", "--hash", HASH, "--order-id", "251029000123", "--avg", "1174.60",
                                      "--qty", "43", "--stop-order-id", "251029000124", now="2026-10-29 10:17"))
        self.assertEqual(code, 0, text)
        self.assertIn("Recorded the fill: BUY 43 RELIANCE at Rs 1,174.60, Kite order 251029000123.", text)
        pf = self.portfolio()
        self.assertEqual(pf["cash_inr"], 49492.2)
        self.assertEqual(pf["positions"]["RELIANCE"]["parent_order_id"], "251029000123")
        self.assertEqual(pf["positions"]["RELIANCE"]["stop_order_id"], "251029000124")
        self.assertIs(pf["paper"], False)
        statuses = [(r["status"], r.get("kite_order_id")) for r in self.log_lines()]
        self.assertEqual(statuses, [("approved", None), ("placed", "251029000123"), ("filled", "251029000123")])
        self.assertEqual(self.log_lines()[1]["max_loss_inr"], 490.2)
        code, text, _ = run(["exit", "RELIANCE", "--orders", self.orders, "--portfolio", self.pf, "--config", self.cfg,
                             "--plan", PLAN_PATH, "--now", "2026-10-29 15:05"])
        self.assertEqual(code, 0)
        self.assertIn("cancel_order with variety co exits a cover", text)
        with open(self.orders) as fh:
            order = json.load(fh)["orders"][0]
        self.assertEqual(order["kite"], {"variety": "co", "parent_order_id": "251029000123", "order_id": "251029000124"})
        h = ticket.order_hash(order)
        self.verdict(h, kind="exit")
        code, text, _ = run(self.args("show", now="2026-10-29 15:05"))
        self.assertIn("Claude will call the Kite MCP tool cancel_order with exactly:", text)
        self.assertIn('"order_id": "251029000124"', text)
        run(self.args("approve", "--hash", h, "--yes", "yes", now="2026-10-29 15:05"))
        code, text, _ = run(self.args("result", "--hash", h, "--status", "placed", "--order-id", "251029000124",
                                      "--filled-qty", "43", "--avg", "1180.00", now="2026-10-29 15:06"))
        self.assertEqual(code, 0, text)
        pf = self.portfolio()
        self.assertEqual((pf["cash_inr"], pf["positions"]), (100232.2, {}))
        with open(self.fills) as fh:
            self.assertEqual(json.load(fh)[1]["realized_inr"], 232.2)

    def test_fill_refusals(self):
        self.verdict()
        run(self.args("approve", "--hash", HASH, "--yes", "yes"))
        code, _, err = run(self.args("fill", "--hash", HASH, "--avg", "1170"))
        self.assertEqual(code, 1)
        self.assertIn("paper mode", err)
        code, _, err = run(self.args("fill", "--hash", HASH, "--offline", self.d))
        self.assertEqual(code, 1)
        self.assertIn("no offline quote file", err)
        kite.load_session = lambda now, path=None: (_ for _ in ()).throw(
            kite.LoginNeeded("not logged in to Kite: run  python3 trader-kite/login.py"))
        code, _, err = run(self.args("fill", "--hash", HASH))
        self.assertEqual(code, 1)
        self.assertIn("not logged in to Kite", err)
        self.assertNotIn("Traceback", err)
        code, text, _ = run(self.args("fill", "--hash", HASH, "--offline", QDIR))
        self.assertEqual(code, 0, text)
        self.assertIn("Rs 1,167.40", text)

    def test_apply_fill_arithmetic(self):
        cfg = {"budget_inr": 100000, "paper": True}
        at = "2026-10-29T10:16:00+05:30"
        pf, _ = ticket.apply_fill(self.pf, cfg, "TATASTEEL", "buy", 100, 169.46, at, "regular")
        self.assertEqual(pf["cash_inr"], 83054.0)
        pf, _ = ticket.apply_fill(self.pf, cfg, "TATASTEEL", "buy", 50, 169.0, at, "regular")
        self.assertEqual(pf["positions"]["TATASTEEL"]["qty"], 150)
        self.assertEqual(pf["positions"]["TATASTEEL"]["avg"], 169.31)
        self.assertEqual(pf["cash_inr"], 74604.0)
        pf, realized = ticket.apply_fill(self.pf, cfg, "TATASTEEL", "sell", 150, 170.0, at)
        self.assertEqual((pf["cash_inr"], pf["positions"], realized), (100104.0, {}, 103.5))
        with self.assertRaisesRegex(ticket.TicketError, "holds 0"):
            ticket.apply_fill(self.pf, cfg, "TATASTEEL", "sell", 1, 170.0, at)
        with self.assertRaisesRegex(ticket.TicketError, "whole shares"):
            ticket.apply_fill(self.pf, cfg, "TATASTEEL", "buy", 2.5, 170.0, at)
        with self.assertRaisesRegex(ticket.TicketError, "budget_inr"):
            ticket.apply_fill(self.none, {}, "TATASTEEL", "buy", 1, 170.0, at)


if __name__ == "__main__":
    unittest.main()
