"""trader-auto, fully offline: the sub-tools are faked at auto.run_tool with canned
--json outputs in the real tools' shapes, and Kite's order methods at a FakeKite."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

auto = load("trader-auto", "auto")
ticket = load("trader-broker", "ticket")
kite = auto.kite

AFIX = os.path.join(FIX, "trader-auto")
EXAMPLE = os.path.join(ROOT, "trader-auto", "config.auto.example.json")
TODAY = "2026-10-29"
LEDGER_LOG_KEYS = {"at", "date", "status", "order_hash", "symbol", "side", "qty", "kind", "max_loss_inr", "stop",
                   "paper", "reply"}


def fx(name):
    with open(os.path.join(AFIX, name), encoding="utf-8") as fh:
        return json.load(fh)


def triggered(symbol, price, bar, time_):
    doc = fx("entry-reliance.json")
    doc.update(symbol=symbol, name=symbol)
    doc["trigger"] = dict(doc["trigger"], price=price, bar=bar, time=time_)
    return doc


def fake_charges(buy_value, sell_value, product="MIS"):
    stt = round(sell_value * 0.00025, 2)
    return {"brokerage": 40.0, "stt": stt, "exchange": 0.0, "sebi": 0.0, "stamp": 0.0, "gst": 0.0,
            "total": round(40.0 + stt, 2)}


def tm(p_target, p_stop):
    doc = fx("timemachine-reliance.json")
    doc["mc"] = dict(doc["mc"], p_target=p_target, p_stop=p_stop, p_neither=round(1 - p_target - p_stop, 4))
    doc["verdict"] = "test verdict %.3f vs %.3f" % (p_target, p_stop)
    return doc


def halt_for_sjvn(args):
    return 0, fx("news-halt.json" if args[1] == "SJVN" else "news-clear.json"), ""


class FakeTools:
    """auto.run_tool: answers from canned outputs per tool (a callable gets the args)."""

    def __init__(self):
        self.calls = []
        self.answers = {
            "scan": (0, fx("scan-orb.json"), ""),
            "entry": lambda args: (0, triggered(args[1], {"RELIANCE": 1174.4, "SJVN": 52.6, "HUDCO": 148.75}[args[1]],
                                                  6 if args[1] == "HUDCO" else 5,
                                                  "09:45" if args[1] == "HUDCO" else "09:40"), ""),
            "results": (0, fx("results-clear.json"), ""),
            "news": (1, fx("news-clear.json"), ""),
            "exit": (0, fx("plan-reliance.json"), ""),
            "timemachine": (0, tm(0.31, 0.27), ""),
            "size": (0, fx("size-reliance.json"), ""),
            "check": (0, fx("check-pass.json"), ""),
            "alerts": (0, "Added reliance-plan-stop", ""),
        }

    def __call__(self, tool, args, timeout=None):
        args = [str(x) for x in args]
        self.calls.append((tool, args))
        ans = self.answers.get(tool)
        if callable(ans):
            ans = ans(args)
        if ans is None:
            return 1, "", "%s: not faked" % tool
        code, out, err = ans
        if not isinstance(out, str):
            out = json.dumps(out)
        return code, out, err

    def tools(self):
        return [t for t, _ in self.calls]


class FakeKite:
    """The order methods with the signatures auto.py codes against; every call is recorded."""

    def __init__(self, last=1180.0, balance=9980.0):
        self.calls = []
        self.last = last
        self.balance = balance
        self.placed = []
        self.next_id = 251029000100
        self.fill_status = "COMPLETE"
        self.fill_price = 1174.6
        self.exit_status = "COMPLETE"
        self.exit_price = 1185.9
        self.legs = {}
        self.history = {}
        self.day_positions = []
        self.net_positions = []

    def margins(self):
        self.calls.append(("margins",))
        return {"available": {"live_balance": self.balance, "cash": self.balance}, "net": self.balance}

    def quotes(self, symbols):
        self.calls.append(("quotes", list(symbols)))
        return {s: {"last_price": self.last, "last_trade_time": "%s 09:41:00" % TODAY} for s in symbols}

    def instrument(self, symbol):
        return {"tradingsymbol": symbol, "tick_size": 0.1, "lot_size": 1, "instrument_token": 738561}

    def place_order(self, body):
        self.calls.append(("place_order", dict(body)))
        self.placed.append(dict(body))
        self.next_id += 1
        oid = str(self.next_id)
        self.legs[oid] = {
            "parent": {"order_id": oid, "status": self.fill_status, "average_price": self.fill_price,
                       "filled_quantity": body["quantity"], "transaction_type": "BUY"},
            "second_leg": {"order_id": oid + "9", "parent_order_id": oid, "status": "TRIGGER PENDING",
                           "trigger_price": body["trigger_price"], "transaction_type": "SELL",
                           "quantity": body["quantity"]}}
        return oid

    def wait_for_status(self, order_id, wanted=("COMPLETE", "REJECTED", "CANCELLED"), timeout=30, every=1.0):
        self.calls.append(("wait_for_status", order_id))
        for legs in self.legs.values():
            if legs["parent"]["order_id"] == order_id:
                return dict(legs["parent"])
            if legs["second_leg"]["order_id"] == order_id:
                return dict(legs["second_leg"])
        raise kite.KiteError("unknown order %s" % order_id)

    def cover_order_legs(self, parent_order_id):
        """Like kite.py: the second leg is shown only while it is still open."""
        self.calls.append(("cover_order_legs", parent_order_id))
        legs = self.legs.get(parent_order_id)
        if not legs:
            return {"parent": None, "second_leg": None}
        leg = legs["second_leg"]
        is_open = leg["status"] not in ("COMPLETE", "REJECTED", "CANCELLED")
        return {"parent": dict(legs["parent"]), "second_leg": dict(leg) if is_open else None}

    def exit_cover_order(self, parent_order_id):
        self.calls.append(("exit_cover_order", parent_order_id))
        leg = self.legs[parent_order_id]["second_leg"]
        leg.update(status=self.exit_status, average_price=self.exit_price, filled_quantity=leg["quantity"])
        return leg["order_id"]

    def cancel_order(self, variety, order_id, parent_order_id=None):
        self.calls.append(("cancel_order", variety, order_id, parent_order_id))
        return order_id

    def order_history(self, order_id):
        self.calls.append(("order_history", order_id))
        if order_id in self.history:
            return self.history[order_id]
        for legs in self.legs.values():
            if legs["parent"]["order_id"] == order_id:
                return [dict(legs["parent"])]
            if legs["second_leg"]["order_id"] == order_id:
                return [dict(legs["second_leg"])]
        return []

    def orders(self):
        return [l["parent"] for l in self.legs.values()] + [l["second_leg"] for l in self.legs.values()]

    def positions(self):
        self.calls.append(("positions",))
        return {"net": list(self.net_positions), "day": list(self.day_positions)}

    def names(self):
        return [c[0] for c in self.calls]

    def stop_leg_fills(self, parent, price):
        self.legs[parent]["second_leg"].update(status="COMPLETE", average_price=price,
                                               filled_quantity=self.legs[parent]["parent"]["filled_quantity"])


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = auto.main(argv)
    return code, out.getvalue(), err.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.makedirs("trader", exist_ok=True)
        with open(EXAMPLE, encoding="utf-8") as fh:
            self.cfg = json.load(fh)
        self.write_cfg()
        self.tools = FakeTools()
        self.k = FakeKite()
        self.saved = (auto.run_tool, auto.connect_kite, auto._sleep, getattr(kite, "estimate_charges", None))
        auto.run_tool = self.tools
        auto.connect_kite = lambda now: self.k
        auto._sleep = lambda s: None
        kite.estimate_charges = fake_charges

    def tearDown(self):
        auto.run_tool, auto.connect_kite, auto._sleep, est = self.saved
        if est is None:
            if hasattr(kite, "estimate_charges"):
                del kite.estimate_charges
        else:
            kite.estimate_charges = est
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def write_cfg(self, **over):
        auto_over = over.pop("auto", None)
        self.cfg.update(over)
        if auto_over:
            self.cfg["auto"] = dict(self.cfg["auto"], **auto_over)
        auto.write_json(os.path.join("trader", "config.json"), self.cfg)

    def tick(self, when, *extra):
        return run(["tick", "--now", "%s %s" % (TODAY, when)] + list(extra))

    def state(self, day=TODAY):
        with open(os.path.join("trader", "auto", "%s.json" % day), encoding="utf-8") as fh:
            return json.load(fh)

    def reset_skips(self):
        st = self.state()
        st["skipped"] = {}
        auto.write_json("trader/auto/%s.json" % TODAY, st)

    def ledger(self):
        pf = auto.read_json("trader/portfolio.json")
        fills = auto.read_json("trader/fills.json")
        log = []
        if os.path.exists("trader/log.jsonl"):
            with open("trader/log.jsonl", encoding="utf-8") as fh:
                log = [json.loads(l) for l in fh if l.strip()]
        return pf, fills, log

    def events(self, st=None):
        st = st or self.state()
        return [(d["event"], d.get("symbol")) for d in st["decisions"]]

    def refusals(self):
        return [d for d in self.state()["decisions"] if d["event"] == "refuse"]

    def enter(self, when="09:41"):
        """A clean entry on RELIANCE: SJVN is halted, RELIANCE passes everything."""
        self.tools.answers["news"] = halt_for_sjvn
        code, text, err = self.tick(when)
        self.assertEqual(code, 0, text + err)
        st = self.state()
        self.assertIsNotNone(st["position"], text)
        return st


# ---------------------------------------------------------------- config and the clock

class Config(Base):
    def test_refuses_unless_auto_enabled(self):
        self.write_cfg(auto={"enabled": False})
        code, _, err = self.tick("09:41")
        self.assertEqual(code, 1)
        self.assertIn('"auto": {"enabled": true} is not set', err)
        self.assertFalse(os.path.exists("trader/auto"))
        self.assertEqual(self.tools.calls, [])

    def test_example_config_carries_the_users_limits(self):
        self.assertEqual((self.cfg["budget_inr"], self.cfg["max_loss_per_trade_inr"], self.cfg["max_daily_loss_inr"],
                          self.cfg["max_trades_per_day"], self.cfg["mis_leverage_x"], self.cfg["paper"]),
                         (10000, 200, 500, 3, 3.0, False))
        self.assertIs(self.cfg["use_mis_leverage"], True)
        self.assertEqual((self.cfg["max_position_pct"], self.cfg["max_order_inr"], self.cfg["min_order_inr"]),
                         (100, 30000, 500))
        self.assertEqual(self.cfg["auto"], {"enabled": True, "target": "1R", "stop": "or-mid", "min_edge": 0.0,
                                            "max_candidates": 8, "max_confirm": 60, "tick_seconds": 60,
                                            "kill_file": "trader/auto/STOP"})
        self.assertEqual((self.cfg["no_entries_after"], self.cfg["flat_by"]), ("14:30", "15:05"))

    def test_bad_limits_are_refused(self):
        self.write_cfg(max_daily_loss_inr=0)
        code, _, err = self.tick("09:41")
        self.assertEqual((code, "max_daily_loss_inr" in err), (1, True))
        self.write_cfg(max_daily_loss_inr=500, flat_by="15:12")
        code, _, err = self.tick("09:41")
        self.assertIn("15:12", err)


class Clock(Base):
    def test_before_the_open_only_checks_the_session_and_balance(self):
        code, text, _ = self.tick("08:50")
        self.assertEqual(code, 0, text)
        st = self.state()
        self.assertEqual(st["phase"], "pre-open")
        self.assertEqual(st["live_balance_inr"], 9980.0)
        self.assertEqual(self.k.names(), ["margins"])
        self.assertEqual(self.tools.calls, [])
        self.assertIn("Kite session is live", text)

    def test_login_needed_is_written_and_exit_1(self):
        def no_session(now):
            raise kite.LoginNeeded("not logged in to Kite: run  python3 trader-kite/login.py")
        auto.connect_kite = no_session
        code, text, _ = self.tick("08:50")
        self.assertEqual(code, 1)
        self.assertIn("not logged in to Kite", text)
        st = self.state()
        self.assertIs(st["login_needed"], True)
        self.assertEqual(self.events(st), [("login-needed", None)])
        code, text, _ = self.tick("09:41")
        self.assertEqual(code, 1)
        self.assertEqual(self.tools.calls, [])              # nothing runs without a session

    def test_opening_range_waits_without_scanning(self):
        code, text, _ = self.tick("09:20")
        self.assertEqual(code, 0)
        self.assertEqual(self.state()["phase"], "opening-range")
        self.assertEqual(self.tools.calls, [])
        self.tick("09:34")
        self.assertEqual(self.tools.calls, [])

    def test_hunting_starts_at_0935_and_ends_at_no_entries_after(self):
        self.tools.answers["scan"] = (1, {"hits": []}, "")
        self.tick("09:35")
        self.assertEqual(self.tools.tools(), ["scan"])
        self.assertEqual(self.state()["phase"], "hunting")
        self.tools.calls = []
        self.tick("14:30")
        self.assertEqual(self.tools.calls, [])
        st = self.state()
        self.assertEqual(st["phase"], "done")
        self.assertIn("past no_entries_after 14:30", st["decisions"][-1]["why"])

    def test_after_1515_writes_the_summary_and_the_journal_once(self):
        self.tick("15:16")
        st = self.state()
        self.assertEqual(st["phase"], "done")
        self.assertEqual(st["summary"]["trades"], 0)
        self.assertIs(st["journal_written"], True)
        self.tick("15:17")
        with open("trader/auto/journal.jsonl", encoding="utf-8") as fh:
            lines = [json.loads(l) for l in fh]
        self.assertEqual(len(lines), 1)
        self.assertEqual((lines[0]["date"], lines[0]["net_inr"], lines[0]["symbols"]), (TODAY, 0.0, []))

    def test_a_closed_day_does_nothing(self):
        code, text, _ = run(["tick", "--now", "2026-10-31 10:00"])     # Saturday
        self.assertEqual(code, 1)
        self.assertIn("NSE is closed today (weekend)", text)
        self.assertEqual(self.state("2026-10-31")["phase"], "closed")
        self.assertEqual(self.k.calls, [])
        code, text, _ = run(["tick", "--now", "2026-11-10 10:00"])     # Diwali Balipratipada
        self.assertIn("NSE holiday", text)

    def test_the_whole_day_in_order(self):
        self.tools.answers["scan"] = (1, {"hits": []}, "")
        phases = []
        for when in ("08:55", "09:16", "09:36", "12:00", "14:31", "15:06", "15:16"):
            self.tick(when)
            phases.append(self.state()["phase"])
        self.assertEqual(phases, ["pre-open", "opening-range", "hunting", "hunting", "done", "done", "done"])
        self.assertEqual(self.tools.tools(), ["scan", "scan"])


# ---------------------------------------------------------------- the hunt and the skip rules

class Hunt(Base):
    def test_the_chain_runs_in_order_and_places_the_sized_order(self):
        st = self.enter()
        self.assertEqual(self.tools.tools(),
                         ["scan", "entry", "results", "news",                       # SJVN: halted
                          "entry", "results", "news", "exit", "timemachine", "size", "check", "alerts"])
        scan_args = self.tools.calls[0][1]
        self.assertEqual(scan_args[:4], ["orb", "--json", "--no-write", "--now"])
        self.assertIn("--universe", scan_args)
        self.assertEqual(scan_args[scan_args.index("--max-confirm") + 1], "60")
        exit_args = self.tools.calls[7][1]
        self.assertEqual(exit_args[:5], ["plan", "--stop", "or-mid", "--target", "1R"])
        self.assertIn("--write", exit_args)
        self.assertIn("--write", self.tools.calls[9][1])                          # size.py --write
        self.assertEqual(self.tools.calls[11][1][:2], ["add", "--from-plan"])
        body = fx("size-reliance.json")["orders"][0]["kite"]
        self.assertEqual(self.k.placed, [body])
        self.assertEqual(st["phase"], "holding")
        pos = st["position"]
        self.assertEqual((pos["symbol"], pos["qty"], pos["entry_price"], pos["planned_entry"], pos["stop"], pos["target"]),
                         ("RELIANCE", 17, 1174.6, 1174.4, 1163.0, 1185.8))
        self.assertEqual((pos["parent_order_id"], pos["stop_order_id"]), ("251029000101", "2510290001019"))
        self.assertEqual(pos["odds"]["p_target"], 0.31)
        self.assertEqual(st["traded"], ["RELIANCE"])
        self.assertEqual(st["skipped"]["SJVN"]["reason"], "news tagged halt: SJVN hits upper circuit after order win")
        self.assertIn(("entry", "RELIANCE"), self.events(st))

    def test_uses_todays_daily_watchlist_when_there_is_one(self):
        auto.write_json("trader/watchlist.json", {"mode": "daily", "as_of": "%sT08:50:00+05:30" % TODAY,
                                                  "hits": [{"symbol": "LT"}]})
        self.tools.answers["scan"] = (1, {"hits": []}, "")
        self.tick("09:41")
        self.assertNotIn("--universe", self.tools.calls[0][1])
        auto.write_json("trader/watchlist.json", {"mode": "orb", "as_of": "%sT10:15:00+05:30" % TODAY, "hits": []})
        self.tick("09:42")
        self.assertIn("--universe", self.tools.calls[1][1])

    def test_skip_blocked(self):
        self.write_cfg(blocked=["sjvn"])
        self.tools.answers["results"] = (0, fx("results-tomorrow.json"), "")
        self.tick("09:41")
        st = self.state()
        self.assertEqual(st["skipped"]["SJVN"]["reason"], "on your blocked list")
        self.assertEqual(self.tools.tools()[:2], ["scan", "entry"])     # straight to RELIANCE
        self.assertEqual(self.tools.calls[1][1][1], "RELIANCE")

    def test_skip_results_tomorrow_and_filing_today(self):
        self.tools.answers["results"] = lambda args: (
            0, fx({"RELIANCE": "results-tomorrow.json", "HUDCO": "results-filing-today.json"}.get(args[1], "results-clear.json")), "")
        self.tools.answers["news"] = halt_for_sjvn
        self.tick("09:46")
        st = self.state()
        self.assertIsNone(st["position"])
        self.assertIn("results headlines name 2026-10-30 (3 mentions)", st["skipped"]["RELIANCE"]["reason"])
        self.assertIn("a results / board-meeting filing on BSE today (Board Meeting Intimation", st["skipped"]["HUDCO"]["reason"])
        self.assertEqual(self.k.placed, [])

    def test_skip_halt_news(self):
        self.tools.answers["news"] = (0, fx("news-halt.json"), "")       # tagged halt, touches SJVN only
        self.tools.answers["results"] = lambda args: (0, fx("results-tomorrow.json" if args[1] == "RELIANCE"
                                                            else "results-clear.json"), "")
        self.tick("09:41")
        st = self.state()
        self.assertIn("news tagged halt", st["skipped"]["SJVN"]["reason"])
        self.assertIn("results headlines", st["skipped"]["RELIANCE"]["reason"])   # a halt on SJVN does not touch RELIANCE

    def test_skip_unaffordable(self):
        self.write_cfg(budget_inr=1200, use_mis_leverage=False, mis_leverage_x=1.0)
        self.tools.answers["news"] = (0, fx("news-halt.json"), "")
        self.tick("09:41")
        st = self.state()
        self.assertEqual(st["skipped"]["RELIANCE"]["reason"],
                         "one share at Rs 1,174.40 is more than the Rs 1,080.00 your cash allows (after the buffer, x1 leverage)")
        self.assertEqual(auto.affordable(auto.load_config("trader/config.json"), st), 1080.0)
        self.write_cfg(use_mis_leverage=True, mis_leverage_x=3.0)
        self.assertEqual(auto.affordable(auto.load_config("trader/config.json"), st), 3240.0)

    def test_skip_already_traded_today(self):
        st = self.enter()
        self.k.stop_leg_fills(st["position"]["parent_order_id"], 1162.7)
        self.tick("09:50")
        self.tools.calls = []
        self.tools.answers["news"] = (0, fx("news-halt.json"), "")
        self.tick("09:51")
        st = self.state()
        self.assertEqual(st["skipped"]["RELIANCE"]["reason"], "already traded today (one trade per symbol per day)")
        self.assertEqual(self.tools.tools(), ["scan"])

    def test_skip_stale_breakout(self):
        self.tick("09:56")                                              # 8 bars done; the 09:40 hits are 3 bars old
        st = self.state()
        self.assertIn("the breakout bar closed at 09:40, 3 bars ago (more than 2)", st["skipped"]["RELIANCE"]["reason"])
        self.assertIn("3 bars ago", st["skipped"]["SJVN"]["reason"])
        self.assertEqual(self.tools.tools()[:2], ["scan", "entry"])     # HUDCO at 09:45 is 2 bars old: tried
        self.assertEqual(self.tools.calls[1][1][1], "HUDCO")

    def test_skip_entry_not_triggered_or_old(self):
        waiting = dict(fx("entry-reliance.json"), status="waiting", trigger=None, reason="last close under the range high")
        self.tools.answers["entry"] = lambda args: (1, waiting if args[1] == "SJVN" else
                                                    triggered("RELIANCE", 1174.4, 2, "09:25"), "")
        self.tick("09:41")
        st = self.state()
        self.assertEqual(st["skipped"]["SJVN"]["reason"], "entry waiting: last close under the range high")
        self.assertEqual(st["skipped"]["RELIANCE"]["reason"], "triggered at 09:25, 3 bars ago (more than 2)")
        self.assertEqual(self.k.placed, [])

    def test_skip_exit_plan_budget_and_risk_refusals(self):
        self.tools.answers["news"] = halt_for_sjvn
        self.tools.answers["exit"] = (1, "", "exit.py: the stop 1170.00 is above the entry")
        self.tick("09:41")
        self.assertEqual(self.state()["skipped"]["RELIANCE"]["reason"],
                         "exit plan refused: exit.py: the stop 1170.00 is above the entry")
        self.tools.answers["exit"] = (0, fx("plan-reliance.json"), "")
        sizing = fx("size-reliance.json")
        sizing["orders"] = []
        sizing["sizing"]["refusal"] = "one share risks Rs 11.40, more than your Rs 10.00 max loss per trade. No order."
        self.tools.answers["size"] = (1, sizing, "")
        self.reset_skips()
        self.tick("09:42")
        self.assertIn("budget refused: one share risks Rs 11.40", self.state()["skipped"]["RELIANCE"]["reason"])
        self.tools.answers["size"] = (0, fx("size-reliance.json"), "")
        self.tools.answers["check"] = (1, fx("check-block.json"), "")
        self.reset_skips()
        self.tick("09:43")
        self.assertEqual(self.state()["skipped"]["RELIANCE"]["reason"],
                         "risk BLOCK: window: no new entries after 14:30 (config)")
        self.assertEqual(self.k.placed, [])

    def test_timemachine_gate_tries_the_next_hit(self):
        self.tools.answers["timemachine"] = (0, fx("timemachine-reliance.json"), "")   # 22.9 vs 28.0: edge -5.1
        self.tools.answers["news"] = halt_for_sjvn
        self.tools.answers["results"] = lambda args: (0, fx("results-filing-today.json" if args[1] == "HUDCO"
                                                            else "results-clear.json"), "")
        self.tick("09:46")
        st = self.state()
        self.assertIsNone(st["position"])
        self.assertEqual(st["skipped"]["RELIANCE"]["reason"],
                         "time machine gate: target first 22.9 % minus stop first 28.0 % = -5.1 points, under min_edge "
                         "+0.0 (Leans toward the stop: target first 22.9 % vs stop first 28.0 %.)")
        self.assertEqual(self.tools.calls[-2][0], "entry")                   # HUDCO was tried next
        self.assertEqual(self.tools.calls[-2][1][1], "HUDCO")
        self.assertEqual(self.events(st)[-1], ("hunt", None))
        self.write_cfg(auto={"min_edge": -0.1})
        self.reset_skips()
        self.tick("09:47")
        self.assertEqual(self.state()["position"]["odds"]["edge"], -0.051)

    def test_a_skipped_symbol_is_not_rechecked_next_tick(self):
        self.tools.answers["news"] = (0, fx("news-halt.json"), "")
        self.tools.answers["results"] = (0, fx("results-tomorrow.json"), "")
        self.tick("09:41")
        n = len(self.tools.calls)
        self.tick("09:42")
        self.assertEqual(self.tools.tools()[n:], ["scan"])
        self.assertEqual(self.state()["skipped"]["SJVN"]["times"], 1)

    def test_a_tool_error_is_retried_next_tick(self):
        self.tools.answers["entry"] = (1, "", "entry.py: cannot reach Kite (timed out)")
        self.tick("09:41")
        st = self.state()
        self.assertIn("entry check could not run (1): entry.py: cannot reach Kite", st["skipped"]["SJVN"]["reason"])
        self.assertIs(st["skipped"]["SJVN"]["retry"], True)
        n = len(self.tools.calls)
        self.tick("09:42")
        self.assertEqual(self.tools.tools()[n:n + 2], ["scan", "entry"])


# ---------------------------------------------------------------- the guards in auto.py itself

class Guards(Base):
    def seed(self, **over):
        self.tick("09:20")
        st = self.state()
        st.update(over)
        auto.write_json("trader/auto/%s.json" % TODAY, st)

    def test_one_position_at_a_time(self):
        self.enter()
        self.tools.calls = []
        self.tick("09:42")
        self.assertEqual(self.tools.calls, [])                 # holding: no scan at all
        self.assertEqual(self.k.names()[-1], "quotes")
        self.assertEqual(len(self.k.placed), 1)
        st = self.state()
        self.assertEqual(auto.guard_order(fx("size-reliance.json")["orders"][0], st, self.cfg, self.k,
                                          auto.parse_now("%s 09:42" % TODAY)),
                         "a position is already open (RELIANCE): one at a time")

    def test_max_trades_per_day(self):
        self.seed(trades_done=3, trades=[{}, {}, {}])
        self.tick("09:41")
        st = self.state()
        self.assertEqual(st["phase"], "done")
        self.assertIn("3 of 3 trades done", st["decisions"][-1]["why"])
        self.assertEqual(self.tools.calls, [])

    def test_daily_cap_counts_charges(self):
        self.seed(realised_inr=-470.0, charges_inr=31.0)
        self.tick("09:41")
        st = self.state()
        self.assertEqual(st["phase"], "done")
        self.assertIn("day net Rs -501.00 has reached the Rs 500.00 daily cap (realised Rs -470.00, charges Rs 31.00)",
                      st["decisions"][-1]["why"])
        self.assertEqual(self.tools.calls, [])

    def test_daily_cap_refuses_an_order_that_could_breach_it(self):
        self.seed(realised_inr=-300.0, charges_inr=20.0)
        self.tools.answers["news"] = (0, fx("news-halt.json"), "")
        self.tick("09:41")
        refusal = self.refusals()[0]
        self.assertEqual(refusal["why"], "net so far Rs -320.00 minus this trade's max loss Rs 193.80 = Rs -513.80 "
                                         "would breach the Rs 500.00 daily cap")
        self.assertEqual(refusal["numbers"], {"qty": 17, "max_loss_inr": 193.8})
        self.assertEqual(self.k.placed, [])

    def test_max_loss_per_trade(self):
        self.write_cfg(max_loss_per_trade_inr=150)
        self.tools.answers["news"] = (0, fx("news-halt.json"), "")
        self.tick("09:41")
        self.assertEqual(self.refusals()[0]["why"], "max loss Rs 193.80 is over the Rs 150.00 per trade")
        self.assertEqual(self.k.placed, [])

    def test_live_balance_below_the_cash_need(self):
        self.k.balance = 6000.0                                   # 17 x 1174.40 / 3 = 6654.93 needed
        self.tools.answers["news"] = (0, fx("news-halt.json"), "")
        self.tick("09:41")
        self.assertEqual(self.refusals()[0]["why"], "live balance Rs 6,000.00 is under the Rs 6,654.93 this order "
                                                     "needs (17 x 1174.40 / 3x)")
        self.assertEqual(self.k.placed, [])

    def test_balance_under_the_minimum_order_stops_the_day(self):
        self.k.balance = 400.0
        self.tick("08:50")
        st = self.state()
        self.assertEqual(st["phase"], "done")
        self.assertIn("live balance Rs 400.00 is under the Rs 500.00 minimum order", st["decisions"][-1]["why"])

    def test_never_a_sell_and_only_co_mis(self):
        sized = fx("size-reliance.json")
        sized["orders"][0]["kite"]["transaction_type"] = "SELL"
        sized["orders"][0]["side"] = "sell"
        self.tools.answers["size"] = (0, sized, "")
        self.tools.answers["news"] = (0, fx("news-halt.json"), "")
        self.tick("09:41")
        self.assertIn("never sells except to exit", self.refusals()[0]["why"])
        self.assertEqual(self.k.placed, [])
        self.assertEqual(auto.guard_order({"kite": {"transaction_type": "BUY", "variety": "regular", "product": "MIS"},
                                           "side": "buy", "qty": 1, "plan": {}}, self.state(), self.cfg, self.k,
                                          auto.parse_now("%s 09:41" % TODAY)),
                         "not an MIS cover order with a stop trigger")

    def test_never_outside_market_hours(self):
        st = {"position": None, "trades_done": 0, "realised_inr": 0.0, "charges_inr": 0.0, "kill": False,
              "limits": auto.limits_of(auto.load_config("trader/config.json"))}
        order = fx("size-reliance.json")["orders"][0]
        for when in ("09:14", "15:15", "15:30"):
            self.assertEqual(auto.guard_order(order, st, self.cfg, self.k, auto.parse_now("%s %s" % (TODAY, when))),
                             "outside 09:15 to 15:15", when)
        self.assertIsNone(auto.guard_order(order, st, self.cfg, self.k, auto.parse_now("%s 09:41" % TODAY)))


# ---------------------------------------------------------------- the kill switch

class KillSwitch(Base):
    def test_stop_and_go_commands(self):
        code, text, _ = run(["stop", "--now", "%s 10:00" % TODAY])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists("trader/auto/STOP"))
        self.assertIn("Kill switch set", text)
        code, text, _ = run(["go"])
        self.assertFalse(os.path.exists("trader/auto/STOP"))
        self.assertIn("removed", text)

    def test_no_entry_while_the_file_is_present(self):
        run(["stop"])
        self.tick("09:41")
        st = self.state()
        self.assertIs(st["kill"], True)
        self.assertEqual(st["phase"], "done")
        self.assertEqual([e for e, _ in self.events(st)][:2], ["kill", "session"])
        self.assertEqual(self.tools.calls, [])
        run(["go"])
        self.tools.answers["news"] = (0, fx("news-halt.json"), "")
        self.tick("09:42")
        self.assertIsNotNone(self.state()["position"])      # entries resume once it is removed

    def test_exits_an_open_position(self):
        st = self.enter()
        parent = st["position"]["parent_order_id"]
        self.k.last = 1178.0
        self.k.exit_price = 1177.8
        run(["stop"])
        self.tick("09:50")
        st = self.state()
        self.assertIsNone(st["position"])
        self.assertIn(("exit_cover_order", parent), self.k.calls)
        tr = st["trades"][0]
        self.assertEqual((tr["exit"]["reason"], tr["exit"]["price"], tr["exit"]["trigger"]), ("kill switch", 1177.8, 1178.0))
        self.assertEqual(st["phase"], "done")


# ---------------------------------------------------------------- holding: the exits

class Exits(Base):
    def test_target_exit_with_the_numbers(self):
        st = self.enter()
        parent = st["position"]["parent_order_id"]
        self.k.last = 1183.0
        self.tick("09:50")
        self.assertIsNotNone(self.state()["position"])
        self.assertNotIn("exit_cover_order", self.k.names())
        self.k.last = 1186.0
        self.k.exit_price = 1185.9
        self.tick("09:55")
        st = self.state()
        self.assertIsNone(st["position"])
        self.assertIn(("exit_cover_order", parent), self.k.calls)
        self.assertIn(("wait_for_status", parent + "9"), self.k.calls)
        tr = st["trades"][0]
        self.assertEqual(tr["exit"]["reason"], "target")
        self.assertEqual((tr["entry"]["price"], tr["entry"]["planned"], tr["exit"]["price"], tr["exit"]["trigger"]),
                         (1174.6, 1174.4, 1185.9, 1186.0))
        self.assertEqual(tr["gross_inr"], 192.1)                          # (1185.90 - 1174.60) x 17
        self.assertEqual(tr["charges"]["total"], 45.04)                   # 40 + 0.025 % of 20,160.30
        self.assertEqual((tr["charges_inr"], tr["net_inr"]), (45.04, 147.06))
        self.assertEqual(tr["slippage"], {"entry_per_share": 0.2, "entry_inr": 3.4, "exit_per_share": -0.1, "exit_inr": -1.7})
        self.assertEqual((st["realised_inr"], st["charges_inr"], st["net_inr"], st["trades_done"]), (192.1, 45.04, 147.06, 1))
        self.assertEqual(st["phase"], "hunting")                          # one trade done, two allowed: hunting resumes

    def test_stop_detected_through_the_second_leg(self):
        st = self.enter()
        parent = st["position"]["parent_order_id"]
        self.k.stop_leg_fills(parent, 1162.7)
        self.k.last = 1161.0
        self.tick("10:03")
        st = self.state()
        self.assertIsNone(st["position"])
        self.assertNotIn("exit_cover_order", self.k.names())
        self.assertIn(("order_history", parent + "9"), self.k.calls)      # the leg's own history said COMPLETE
        tr = st["trades"][0]
        self.assertEqual((tr["exit"]["reason"], tr["exit"]["price"], tr["exit"]["trigger"], tr["exit"]["order_id"]),
                         ("stop", 1162.7, 1163.0, parent + "9"))
        self.assertEqual(tr["gross_inr"], -202.3)
        self.assertEqual(tr["slippage"]["exit_per_share"], -0.3)
        _, fills, log = self.ledger()
        self.assertEqual(fills[-1]["realized_inr"], -202.3)
        self.assertEqual([l["status"] for l in log if l["kind"] == "exit"], ["approved", "filled"])
        self.assertEqual(log[-1]["note"], "the exchange's stop leg filled")

    def test_flat_by_exit(self):
        self.enter()
        self.k.last = 1180.0
        self.k.exit_price = 1179.9
        self.tick("15:04")
        self.assertIsNotNone(self.state()["position"])
        self.tick("15:05")
        st = self.state()
        tr = st["trades"][0]
        self.assertEqual((tr["exit"]["reason"], tr["exit"]["price"], tr["exit"]["time"][11:16]), ("flat-by", 1179.9, "15:05"))
        self.assertEqual(st["phase"], "done")                             # past no_entries_after
        self.tick("15:16")
        st = self.state()
        self.assertEqual(st["summary"]["trades"], 1)
        with open("trader/auto/journal.jsonl", encoding="utf-8") as fh:
            line = json.loads(fh.readline())
        self.assertEqual((line["symbols"], line["exits"], line["net_inr"]), (["RELIANCE"], ["flat-by"], st["net_inr"]))

    def test_exit_still_open_is_pending_then_recorded(self):
        st = self.enter()
        parent = st["position"]["parent_order_id"]
        self.k.last = 1186.0
        self.k.exit_status = "OPEN"
        self.tick("09:55")
        st = self.state()
        self.assertIsNotNone(st["position"])
        self.assertEqual(st["position"]["exit_pending"]["reason"], "target")
        self.k.legs[parent]["second_leg"].update(status="COMPLETE", average_price=1185.7, filled_quantity=17)
        self.tick("09:56")
        st = self.state()
        self.assertIsNone(st["position"])
        self.assertEqual((st["trades"][0]["exit"]["reason"], st["trades"][0]["exit"]["price"]), ("target", 1185.7))
        self.assertEqual(self.k.names().count("exit_cover_order"), 1)

    def test_broker_square_off_is_recorded(self):
        st = self.enter()
        parent = st["position"]["parent_order_id"]
        self.k.legs[parent]["second_leg"]["status"] = "CANCELLED"
        self.k.day_positions = [{"tradingsymbol": "RELIANCE", "quantity": 0, "sell_quantity": 17, "sell_price": 1176.2}]
        self.tick("15:13")
        st = self.state()
        self.assertIsNone(st["position"])
        self.assertEqual((st["trades"][0]["exit"]["reason"], st["trades"][0]["exit"]["price"]), ("broker square-off", 1176.2))

    def test_pending_fill_resolves_next_tick(self):
        self.k.fill_status = "OPEN"
        st = self.enter()
        self.assertIs(st["position"]["pending"], True)
        self.assertEqual(st["position"]["entry_price"], None)
        pf, fills, log = self.ledger()
        self.assertIsNone(pf)                                             # nothing filled yet
        self.assertEqual([l["status"] for l in log], ["approved", "placed"])
        parent = st["position"]["parent_order_id"]
        self.k.legs[parent]["parent"].update(status="COMPLETE", average_price=1175.0, filled_quantity=17)
        self.tick("09:42")
        st = self.state()
        self.assertIs(st["position"]["pending"], False)
        self.assertEqual((st["position"]["entry_price"], st["position"]["stop_order_id"]), (1175.0, parent + "9"))
        self.assertEqual(self.ledger()[0]["positions"]["RELIANCE"]["qty"], 17)

    def test_rejected_order_is_logged_and_nothing_is_held(self):
        self.k.fill_status = "REJECTED"
        self.tools.answers["news"] = halt_for_sjvn
        self.tick("09:41")
        st = self.state()
        self.assertIsNone(st["position"])
        _, fills, log = self.ledger()
        self.assertIsNone(fills)
        self.assertEqual([l["status"] for l in log], ["approved", "placed", "failed"])
        self.assertIn(("failed", "RELIANCE"), self.events(st))
        self.assertIn("REJECTED", st["skipped"]["RELIANCE"]["reason"])
        self.tick("09:42")                                                # a rejection is never re-sent each tick
        self.assertEqual(len(self.k.placed), 1)


# ---------------------------------------------------------------- paper and dry-run

class PaperAndDryRun(Base):
    def test_paper_mode_never_calls_an_order_method(self):
        self.write_cfg(paper=True)
        self.k.last = 1174.4
        st = self.enter()
        self.assertNotIn("place_order", self.k.names())
        pos = st["position"]
        self.assertEqual((pos["entry_price"], pos["paper"]), (1174.4, True))
        self.assertTrue(pos["parent_order_id"].startswith("PAPER-"))
        self.assertEqual(pos["stop_order_id"], pos["parent_order_id"] + "-SL")
        pf, fills, log = self.ledger()
        self.assertIs(pf["paper"], True)
        self.assertEqual(pf["cash_inr"], round(10000.0 - 17 * 1174.4, 2))
        self.assertEqual(fills[0]["kite_order_id"], pos["parent_order_id"])
        self.k.last = 1186.2
        self.tick("10:00")
        st = self.state()
        self.assertIsNone(st["position"])
        self.assertNotIn("exit_cover_order", self.k.names())
        self.assertNotIn("cover_order_legs", self.k.names())
        self.assertEqual((st["trades"][0]["exit"]["reason"], st["trades"][0]["gross_inr"]), ("target", 200.6))
        self.assertEqual(self.ledger()[0]["positions"], {})

    def test_paper_stop_fills_at_the_last_price(self):
        self.write_cfg(paper=True)
        self.enter()
        self.k.last = 1162.5
        self.tick("10:00")
        tr = self.state()["trades"][0]
        self.assertEqual((tr["exit"]["reason"], tr["exit"]["price"]), ("stop", 1162.5))

    def test_dry_run_runs_the_chain_and_never_places(self):
        self.tools.answers["news"] = halt_for_sjvn
        code, text, _ = self.tick("09:41", "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(self.tools.tools()[-1], "check")                 # the chain ran, alerts were not armed
        self.assertEqual(self.tools.tools().count("scan"), 1)             # and the pass ended at "would place"
        self.assertNotIn("place_order", self.k.names())
        st = self.state()
        self.assertIsNone(st["position"])
        self.assertEqual(st["traded"], [])
        dry = [d for d in st["decisions"] if d["event"] == "dry-run"][0]
        self.assertIn('"tradingsymbol": "RELIANCE"', dry["why"])
        self.assertIn("nothing sent", dry["why"])
        self.assertEqual(self.ledger(), (None, None, []))                 # no ledger line either
        self.assertIn("dry-run RELIANCE", text)


# ---------------------------------------------------------------- the ledger, status, report

class Ledger(Base):
    def test_the_broker_shapes(self):
        st = self.enter()
        order = fx("size-reliance.json")["orders"][0]
        h = ticket.order_hash(order)
        pf, fills, log = self.ledger()
        self.assertEqual(pf["positions"], {"RELIANCE": {"qty": 17, "avg": 1174.6, "product": "MIS", "order_style": "co",
                                                        "entered_at": "%sT09:41:00+05:30" % TODAY,
                                                        "parent_order_id": "251029000101", "stop_order_id": "2510290001019"}})
        self.assertEqual((pf["cash_inr"], pf["paper"]), (round(10000 - 17 * 1174.6, 2), False))
        self.assertEqual(fills, [{"order_hash": h, "kite_order_id": "251029000101", "symbol": "RELIANCE", "side": "buy",
                                  "filled_qty": 17, "average_price": 1174.6, "at": "%sT09:41:00+05:30" % TODAY,
                                  "paper": False, "auto": True, "parent_order_id": "251029000101"}])
        self.assertEqual([(l["status"], l["order_hash"]) for l in log], [("approved", h), ("placed", h), ("filled", h)])
        self.assertTrue(LEDGER_LOG_KEYS <= set(log[0]))
        self.assertEqual(log[0]["reply"], "auto")
        self.assertEqual(log[0]["limits"]["max_loss_per_trade_inr"], 200.0)
        self.assertEqual((log[1]["kite_order_id"], log[2]["price"], log[2]["max_loss_inr"], log[2]["stop"]),
                         ("251029000101", 1174.6, 193.8, 1163.0))
        self.assertEqual(ticket.latest(log, h, TODAY)["status"], "filled")   # the broker reads it as filled
        out = io.StringIO()
        with redirect_stdout(out):
            code = ticket.main(["positions", "--now", "%s 09:45" % TODAY])
        self.assertEqual(code, 0)
        self.assertIn("RELIANCE        17 @ Rs 1,174.60  MIS, cover order, parent 251029000101, stop leg 2510290001019",
                      out.getvalue())
        self.k.last = 1186.0
        self.tick("09:55")
        pf, fills, log = self.ledger()
        xo = auto.exit_order(st["position"])
        self.assertEqual(xo["kite"], {"variety": "co", "parent_order_id": "251029000101", "order_id": "2510290001019"})
        xh = ticket.order_hash(xo)
        self.assertEqual([(l["status"], l["order_hash"], l["kind"]) for l in log[3:]],
                         [("approved", xh, "exit"), ("placed", xh, "exit"), ("filled", xh, "exit")])
        self.assertEqual((fills[1]["side"], fills[1]["realized_inr"], fills[1]["kite_order_id"]), ("sell", 192.1, "2510290001019"))
        self.assertEqual((pf["positions"], pf["cash_inr"]), ({}, round(10000 - 17 * 1174.6 + 17 * 1185.9, 2)))

    def test_charges_fall_back_when_kite_has_no_estimator(self):
        del kite.estimate_charges
        self.enter()
        self.k.last = 1186.0
        self.tick("09:55")
        tr = self.state()["trades"][0]
        self.assertEqual((tr["charges"], tr["charges_inr"], tr["net_inr"]), (None, 0.0, 192.1))

    def test_status_and_report(self):
        code, _, err = run(["status", "--now", "%s 09:00" % TODAY])
        self.assertEqual(code, 1)
        self.assertIn("no Auto Trader state", err)
        self.enter()
        code, text, _ = run(["status", "--now", "%s 09:42" % TODAY])
        self.assertEqual(code, 0)
        self.assertIn("Auto Trader 2026-10-29, LIVE, phase holding.", text)
        self.assertIn("Open: 17 RELIANCE at 1174.60, stop 1163.00, target 1185.80", text)
        self.assertIn("Trades 0 of 3, realised Rs 0.00, charges Rs 0.00, net Rs 0.00 (daily cap Rs 500.00).", text)
        self.assertIn("Skipped: SJVN (news tagged halt", text)
        self.k.last = 1186.0
        self.tick("09:55")
        code, text, _ = run(["report", "--now", "%s 15:00" % TODAY])
        self.assertEqual(code, 0, text)
        self.assertIn("1. RELIANCE x17  entry 09:41 at 1174.60 (plan 1174.40, +0.20 a share)  stop 1163.00  target 1185.80", text)
        self.assertIn("exit 09:55 at 1185.90, target (trigger 1186.00, -0.10 a share)", text)
        self.assertIn("gross Rs 192.10, charges Rs 45.04, net Rs 147.06   odds at entry: target 31.0 %, stop 27.0 %, "
                      "neither 42.0 % (test verdict 0.310 vs 0.270)", text)
        self.assertIn("Totals: 1 trade(s), gross Rs 192.10, charges Rs 45.04, net Rs 147.06.", text)
        self.assertIn("09:41  SJVN         news tagged halt", text)
        self.assertIn("not summarised yet", text)
        code, text, _ = run(["report", "--json", "--now", "%s 15:00" % TODAY])
        doc = json.loads(text)
        self.assertEqual(doc["totals"], {"trades": 1, "gross_inr": 192.1, "charges_inr": 45.04, "net_inr": 147.06})
        self.assertEqual(doc["trades"][0]["slippage"]["exit_inr"], -1.7)
        self.assertEqual(doc["trades"][0]["odds"]["p_stop"], 0.27)
        # after the exit the same tick hunts again: RELIANCE is traded, HUDCO's sized order is not HUDCO's
        self.assertEqual(sorted(doc["skipped"]), ["HUDCO", "RELIANCE", "SJVN"])
        self.assertEqual(doc["skipped"]["RELIANCE"]["reason"], "already traded today (one trade per symbol per day)")
        self.tick("15:16")
        code, text, _ = run(["report", "--date", TODAY])
        self.assertIn("Journal: one line for 2026-10-29", text)
        with open("trader/auto/journal.jsonl", encoding="utf-8") as fh:
            self.assertEqual(len(fh.read().splitlines()), 1)


# ---------------------------------------------------------------- the probe

class Probe(Base):
    def probe(self, *extra):
        return run(["probe", "RELIANCE", "--now", "%s 10:16" % TODAY] + list(extra))

    def test_refuses_without_the_literal_flag(self):
        self.k.last = 1174.4
        code, text, _ = self.probe()
        self.assertEqual(code, 1)
        self.assertIn('"trigger_price": 1162.6', text)                  # 1174.40 x 0.99 = 1162.656 -> 1162.6 (tick 0.10)
        self.assertIn('"quantity": 1', text)
        self.assertIn("add the literal argument --i-understand-this-is-real", text)
        self.assertNotIn("place_order", self.k.names())
        self.assertFalse(os.path.exists("trader/auto/probes.jsonl"))

    def test_dry_run_prints_the_body_and_stops(self):
        code, text, _ = self.probe("--dry-run", "--stop-pct", "2")
        self.assertEqual(code, 0)
        self.assertIn('"trigger_price": 1156.4', text)
        self.assertIn("Dry run: nothing sent.", text)
        self.assertNotIn("place_order", self.k.names())

    def test_the_real_path_places_waits_exits_and_checks_flat(self):
        self.k.fill_price = 1180.2
        self.k.exit_price = 1180.0
        self.k.net_positions = [{"tradingsymbol": "RELIANCE", "quantity": 0}]
        code, text, _ = self.probe("--i-understand-this-is-real")
        self.assertEqual(code, 0, text)
        self.assertEqual(self.k.placed, [{"variety": "co", "exchange": "NSE", "tradingsymbol": "RELIANCE",
                                          "transaction_type": "BUY", "order_type": "MARKET", "quantity": 1,
                                          "product": "MIS", "trigger_price": 1168.2, "validity": "DAY", "tag": "dtsprobe"}])
        self.assertEqual(self.k.names(), ["quotes", "margins", "place_order", "wait_for_status", "cover_order_legs",
                                          "exit_cover_order", "wait_for_status", "positions"])
        self.assertNotIn("cancel_order", self.k.names())                  # the exit goes through exit_cover_order only
        self.assertIn("Fills: BUY 1 at 1180.20, SELL 1 at 1180.00. Realised Rs -0.20, charges about Rs 40.29", text)
        self.assertIn("net Rs -40.49", text)
        self.assertIn("positions(): RELIANCE net quantity 0, flat.", text)
        with open("trader/auto/probes.jsonl", encoding="utf-8") as fh:
            rec = json.loads(fh.readline())
        self.assertEqual((rec["entry"], rec["exit"], rec["gross_inr"], rec["net_qty_after"]), (1180.2, 1180.0, -0.2, 0))

    def test_refuses_outside_hours_and_with_the_kill_switch(self):
        code, _, err = run(["probe", "RELIANCE", "--now", "%s 08:50" % TODAY, "--dry-run"])
        self.assertEqual((code, "runs only while NSE is open" in err), (1, True))
        run(["stop"])
        code, _, err = self.probe("--dry-run")
        self.assertIn("kill switch", err)
        self.assertNotIn("place_order", self.k.names())


if __name__ == "__main__":
    unittest.main()
