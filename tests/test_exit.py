"""trader-exit tests. The entry comes from tests/fixtures/trader-entry/ (HINDUNILVR's real
2026-10-29 five-minute bars, see test_entry.py); the ATR stop reads tests/fixtures/trader-exit/:

HINDUNILVR-day.json  30 real Kite daily candles, 2026-09-15 to 2026-10-28 (no candle on the
                     2026-10-02 and 2026-10-20 holidays). ATR(14) through 10-28 is 46.7214.
ONGC-day.json        29 daily candles, 2026-09-10 to 2026-10-23. ATR(14) is 3.7136.

The reel: entry 1832.00 at 10:00 (the 09:55 bar, bar 9), range 1804.00 to 1829.50, tick 0.10
(Kite's row for HINDUNILVR, not the usual 0.05). The midpoint 1816.75 rounds DOWN to 1816.70,
risk 15.30, 1R 1847.30, 2R 1862.60. ONGC (tick 0.01): entry 227.60, midpoint 225.535 -> 225.53.
No network: the fake Kite from test_entry answers from the fixtures.
"""

import io
import json
import os
import unittest
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal

from _load import load, FIX, ROOT

import test_entry
from test_entry import Base, run as run_entry

exitp = load("trader-exit", "exit")
entry = test_entry.entry

XDIR = os.path.join(FIX, "trader-exit")

# trader/plan.json for the reel, exactly as the contract writes it
REEL_PLAN = {"created_by": "trader-exit", "symbol": "HINDUNILVR", "side": "buy", "date": "2026-10-29", "setup": "orb",
             "entry": 1832.0, "entry_time": "10:00", "entry_bar": 9, "interval": "5minute",
             "opening_range": {"high": 1829.5, "low": 1804.0, "minutes": 15},
             "stop": 1816.7, "stop_rule": "or_mid", "stop_label": "opening-range midpoint",
             "target": 1847.3, "target_r": 1.0, "target_label": "1R (one times the risk)",
             "risk_per_share": 15.3, "reward_risk": 1.0, "flat_by": "15:05", "cas": True, "tick_size": 0.1}
REEL_ENTRY = {"created_by": "trader-entry", "symbol": "HINDUNILVR", "setup": "orb", "side": "buy",
              "status": "triggered", "date": "2026-10-29", "interval": "5minute", "time": "10:00", "bar": 9,
              "bar_start": "09:55", "price": 1832.0, "level": 1829.5,
              "opening_range": {"high": 1829.5, "low": 1804.0, "minutes": 15}, "cas": True, "tick_size": 0.1,
              "source": "kite", "checked_at": "2026-10-29T10:00:00+05:30"}


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = exitp.main(argv)
    return code, out.getvalue(), err.getvalue()


def c(entry_px, stop, target="2R", tick=0.1, oh=1829.5, ol=1804.0, atr=None, n=None):
    sr, sp = exitp.parse_stop(stop)
    tr, tp = exitp.parse_target(target)
    return exitp.compute(entry_px, sr, sp, tr, tp, tick, oh, ol, atr, n)


class Math(unittest.TestCase):
    def test_the_reel_or_mid_1r(self):
        p = c(1832, "or-mid", "1R")
        self.assertEqual(p["stop"], Decimal("1816.70"))          # floor(1816.75) on the 0.10 tick
        self.assertEqual(p["risk"], Decimal("15.30"))
        self.assertEqual(p["target"], Decimal("1847.30"))
        self.assertEqual(p["reward_risk"], Decimal("1.00"))
        self.assertEqual(p["target_label"], "1R (one times the risk)")
        self.assertEqual(p["stop_detail"], "(Rs 1,829.50 + Rs 1,804.00) / 2 = 1816.75, rounded down to the Rs 0.10 tick")

    def test_the_default_2r(self):
        p = c(1832, "or-mid")
        self.assertEqual(p["target"], Decimal("1862.60"))
        self.assertEqual(p["target_label"], "2R (two times the risk)")
        self.assertEqual(p["target_r"], Decimal("2"))

    def test_kite_floats_give_the_same_paise(self):
        p = c(1832.0, "or-mid", "1R", 0.1, 1829.5, 1804.0)
        self.assertEqual((p["entry"], p["stop"], p["target"]), (Decimal("1832.00"), Decimal("1816.70"), Decimal("1847.30")))

    def test_the_tick_is_read_never_assumed(self):
        # the same range on a 0.05 tick: 1816.75 is on the grid, nothing to round
        p = c(1832, "or-mid", "1R", 0.05)
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("1816.75"), Decimal("15.25"), Decimal("1847.25")))
        self.assertNotIn("rounded", p["stop_detail"])
        # ONGC under Rs 250, tick 0.01: 225.535 floors to 225.53
        p = c(227.60, "or-mid", "1R", 0.01, 226.41, 224.66)
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("225.53"), Decimal("2.07"), Decimal("229.67")))
        self.assertEqual(c(227.60, "or-mid", "2R", 0.01, 226.41, 224.66)["target"], Decimal("231.74"))
        self.assertIn("rounded down to the Rs 0.01 tick", p["stop_detail"])
        # a 0.50 tick: both ends snap down
        p = c(1000, "995.3", "1.5R", 0.5)
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("995.00"), Decimal("5.00"), Decimal("1007.50")))
        with self.assertRaises(exitp.PlanError):
            c(1832, "or-mid", "1R", 0)

    def test_other_stops(self):
        p = c(1832, "or-low")
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("1804.00"), Decimal("28.00"), Decimal("1888.00")))
        p = c(1832, "1816.79")
        self.assertEqual(p["stop"], Decimal("1816.70"))             # a price rounds down to the tick too
        self.assertEqual(p["stop_label"], "your price")
        p = c(1832, "1%")
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("1813.60"), Decimal("18.40"), Decimal("1868.80")))
        self.assertEqual(p["stop_label"], "1 % under the entry")
        p = c(1832, "1xATR", atr=46.721429, n=14)
        self.assertEqual(p["stop"], Decimal("1785.20"))             # 1832 - 46.721429 = 1785.278571
        self.assertEqual(p["stop_label"], "1 x ATR(14) under the entry")
        self.assertIn("ATR Rs 46.72 over the 14 daily bars", p["stop_detail"])
        p = c(1832, "1.5xATR", atr=46.721429, n=9)
        self.assertEqual(p["stop"], Decimal("1761.90"))
        self.assertIn("fewer than 14", p["stop_detail"])

    def test_stop_and_target_spellings(self):
        self.assertEqual(exitp.parse_stop("or-mid"), ("or_mid", None))
        self.assertEqual(exitp.parse_stop("OR_LOW"), ("or_low", None))
        for text in ("1816.70", "price:1816.70", "Rs 1,816.70"):
            self.assertEqual(exitp.parse_stop(text), ("price", Decimal("1816.70")), text)
        for text in ("1%", "pct:1", "1 %"):
            self.assertEqual(exitp.parse_stop(text), ("pct", Decimal("1")), text)
        for text in ("1.5xATR", "atr:1.5", "1.5 x ATR", "1.5atr"):
            self.assertEqual(exitp.parse_stop(text), ("atr", Decimal("1.5")), text)
        self.assertEqual(exitp.parse_target("2R"), ("r", Decimal("2")))
        self.assertEqual(exitp.parse_target("1.5r"), ("r", Decimal("1.5")))
        self.assertEqual(exitp.parse_target("1847.30"), ("price", Decimal("1847.30")))
        self.assertEqual(exitp.parse_target("price:1,847.30"), ("price", Decimal("1847.30")))
        self.assertEqual(exitp.parse_target("Rs 1,847.30"), ("price", Decimal("1847.30")))

    def test_target_rounds_down_and_price_targets(self):
        p = c(100, "99", "1.33R", 0.05, None, None)
        self.assertEqual(p["target"], Decimal("101.30"))            # 100 + 1.33, down to the 0.05 tick, never up
        self.assertEqual(p["target_label"], "1.33R (1.33 times the risk)")
        p = c(1832, "or-mid", "1850")
        self.assertEqual(p["reward_risk"], Decimal("1.18"))         # (1850 - 1832) / 15.30
        self.assertEqual(p["target_label"], "your price (1.18R)")
        self.assertEqual(p["target_r"], Decimal("1.18"))
        self.assertEqual(c(1832, "or-mid", "1850.07")["target"], Decimal("1850.00"))

    def test_refusals_say_why(self):
        cases = [("1832", "2R", "at or above the entry"),
                 ("1840", "2R", "at or above the entry"),
                 ("or-mid", "1832", "at or below the entry"),
                 ("or-mid", "1800", "at or below the entry"),
                 ("1831.90", "2R", "closer than 2 ticks (Rs 0.20)"),
                 ("0.005%", "2R", "closer than 2 ticks"),
                 ("1600", "2R", "outside Zerodha's cover-order range"),
                 ("12%", "2R", "more than 10 %")]
        for stop, target, why in cases:
            with self.assertRaises(exitp.PlanError) as cm:
                c(1832, stop, target)
            self.assertIn(why, str(cm.exception), stop)
        # two ticks of 0.10 is Rs 0.20: a 0.20 risk is allowed, 0.10 is not; 10 % exactly is allowed
        self.assertEqual(c(1832, "1831.80")["risk"], Decimal("0.20"))
        self.assertEqual(c(1832, "1648.80")["risk"], Decimal("183.20"))
        with self.assertRaises(exitp.PlanError):
            c(1832, "1648.70")
        with self.assertRaises(exitp.PlanError):
            c(1810, "or-mid")                                       # the midpoint 1816.75 is above this entry
        for bad in ("mid-ish", "100%", "0", "-1xATR", "price:1%", "atr:1%", "2R"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_stop(bad)
        for bad in ("0R", "R", "lots", "price:x", "0", "or-mid"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_target(bad)
        with self.assertRaises(exitp.PlanError):
            c(1832, "or-mid", oh=None, ol=None)
        with self.assertRaises(exitp.PlanError):
            c(1832, "or-mid", oh=1804.0, ol=1829.5)
        with self.assertRaises(exitp.PlanError):
            c(1832, "1xATR")                                        # no ATR given
        with self.assertRaises(exitp.PlanError):
            c(0, "or-mid")


class Atr(Base):
    def test_atr_on_kite_daily_bars(self):
        k = exitp.connect()
        day = exitp.datetime.date(2026, 10, 29)
        bars = exitp.daily_bars(k, "HINDUNILVR", day)
        self.assertEqual(len(bars), 30)
        self.assertEqual(bars[-1]["t"].date().isoformat(), "2026-10-28")   # never the entry day itself
        self.assertEqual(bars[0]["t"].date().isoformat(), "2026-09-15")
        url = self.net.requests[-1].full_url
        self.assertIn("/instruments/historical/356865/day?", url)
        self.assertIn("from=2026-09-14+00%3A00%3A00", url)
        self.assertIn("to=2026-10-28+23%3A59%3A00", url)
        # by hand: the true range of each bar from the previous close, the last 14 averaged
        trs = []
        for prev, b in zip(bars, bars[1:]):
            trs.append(max(b["h"] - b["l"], abs(b["h"] - prev["c"]), abs(b["l"] - prev["c"])))
        atr, n = exitp.atr_of(bars)
        self.assertEqual(n, 14)
        self.assertAlmostEqual(atr, sum(trs[-14:]) / 14, places=9)
        self.assertAlmostEqual(atr, 46.7214, places=4)
        # with fewer bars it averages what there is
        atr, n = exitp.atr_of(bars[:6])
        self.assertEqual(n, 5)
        self.assertAlmostEqual(atr, sum(trs[:5]) / 5, places=9)
        atr, n = exitp.atr_of(exitp.daily_bars(k, "ONGC", exitp.datetime.date(2026, 10, 26)))
        self.assertEqual(n, 14)
        self.assertAlmostEqual(atr, 3.7136, places=4)


class Cli(Base):
    def setUp(self):
        Base.setUp(self)
        os.mkdir("trader")

    def write(self, path, doc):
        with open(path, "w") as fh:
            json.dump(doc, fh)

    def plan(self, *args):
        return run(["plan", "--now", "2026-10-29 10:01"] + list(args))

    def read_plan(self):
        with open(os.path.join("trader", "plan.json")) as fh:
            return json.load(fh)

    def test_entry_to_plan_end_to_end(self):
        code, _, err = run_entry(["check", "HINDUNILVR", "--now", "2026-10-29 10:00", "--write"])
        self.assertEqual(code, 0, err)
        calls = len(self.net.requests)
        code, text, err = self.plan("--stop", "or-mid", "--target", "1R", "--write")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.net.requests), calls)            # tick and CAS came from entry.json: no call
        p = self.read_plan()
        for k, v in REEL_PLAN.items():
            self.assertEqual(p[k], v, k)
        self.assertEqual(p["created_at"], "2026-10-29T10:01:00+05:30")
        self.assertIn("Exit plan: HINDUNILVR long, Thu 2026-10-29, opening-range breakout on 5-minute bars", text)
        self.assertIn("Entry         Rs 1,832.00 at 10:00 (bar 9)", text)
        self.assertIn("Stop          Rs 1,816.70  opening-range midpoint: (Rs 1,829.50 + Rs 1,804.00) / 2 = 1816.75, "
                      "rounded down to the Rs 0.10 tick", text)
        self.assertIn("Target        Rs 1,847.30  1R (one times the risk)", text)
        self.assertIn("Risk a share  Rs 15.30 (0.84 % of the entry, tick Rs 0.10)", text)
        self.assertIn("Reward:risk   1.0", text)
        self.assertIn("Flat by       15:05 IST", text)
        self.assertIn("squares off this CAS stock's MIS at 15:12", text)
        self.assertIn("not a guarantee", text)
        self.assertIn("/trader-timemachine", text)
        self.assertIn("Nothing was placed", text)
        self.assertNotIn("pro" + "fit", text.lower())   # never on screen
        code, text, _ = self.plan("--write")                       # the defaults: or-mid, 2R
        self.assertEqual(self.read_plan()["target"], 1862.6)
        self.assertEqual(self.read_plan()["target_r"], 2.0)
        code, text, _ = run(["show", "--now", "2026-10-29 10:20"])
        self.assertEqual(code, 0)
        self.assertIn("Rs 1,862.60", text)
        self.assertNotIn("not today", text)
        code, text, _ = run(["show", "--now", "2026-10-30 10:20"])
        self.assertIn("not today", text)
        code, text, _ = run(["show", "--json"])
        self.assertEqual(json.loads(text)["stop"], 1816.7)

    def test_manual_flags_fetch_the_tick_and_cas_from_kite(self):
        code, text, err = self.plan("--symbol", "hindunilvr", "--entry", "1832", "--or-high", "1829.5", "--or-low", "1804",
                                    "--time", "10:00", "--entry-bar", "9", "--date", "2026-10-29", "--target", "1R",
                                    "--json")
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        for k, v in REEL_PLAN.items():
            self.assertEqual(p[k], v, k)
        self.assertEqual(p["warnings"], [])
        paths = [test_entry.urllib.parse.urlparse(r.full_url).path for r in self.net.requests]
        self.assertEqual(paths, ["/instruments/NSE", "/instruments/NFO"])

    def test_the_under_250_stock_on_the_one_paisa_tick(self):
        code, text, err = run(["plan", "--now", "2026-10-26 10:11", "--symbol", "ONGC", "--entry", "227.60",
                               "--or-high", "226.41", "--or-low", "224.66", "--time", "10:10", "--entry-bar", "11",
                               "--date", "2026-10-26", "--json"])
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        self.assertEqual((p["tick_size"], p["cas"]), (0.01, True))
        self.assertEqual((p["stop"], p["risk_per_share"], p["target"], p["reward_risk"]), (225.53, 2.07, 231.74, 2.0))
        self.assertIn("rounded down to the Rs 0.01 tick", p["stop_detail"])
        code, text, _ = run(["plan", "--now", "2026-10-26 10:11", "--symbol", "ONGC", "--entry", "227.60",
                             "--setup", "level", "--stop", "1.5xATR", "--date", "2026-10-26", "--json"])
        self.assertEqual(code, 0)
        p = json.loads(text)
        self.assertEqual((p["stop"], p["risk_per_share"], p["target"]), (222.02, 5.58, 238.76))
        self.assertEqual((p["atr_bars"], p["stop_rule"], p["opening_range"]), (14, "atr", None))

    def test_atr_stop_reads_daily_bars(self):
        self.write(os.path.join("trader", "entry.json"), REEL_ENTRY)
        code, text, err = self.plan("--stop", "1xATR", "--json")
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        self.assertEqual((p["stop"], p["risk_per_share"], p["target"]), (1785.2, 46.8, 1925.6))
        self.assertEqual((p["stop_rule"], p["atr_bars"]), ("atr", 14))
        self.assertAlmostEqual(p["atr"], 46.7214, places=4)
        self.assertEqual(p["stop_label"], "1 x ATR(14) under the entry")
        paths = [test_entry.urllib.parse.urlparse(r.full_url).path for r in self.net.requests]
        self.assertEqual(paths, ["/instruments/NSE", "/instruments/historical/356865/day"])
        # 2 x ATR is 93.50 under the entry, 5.1 %: fine. 4 x ATR would be 10.2 %: outside the cover-order range
        self.assertEqual(json.loads(self.plan("--stop", "2xATR", "--json")[1])["stop"], 1738.5)
        code, _, err = self.plan("--stop", "4xATR")
        self.assertEqual(code, 1)
        self.assertIn("cover-order range", err)
        # too few daily bars before the date
        code, _, err = self.plan("--stop", "1xATR", "--date", "2026-09-17")
        self.assertEqual(code, 1)
        self.assertIn("at least 4 daily bars", err)

    def test_missing_tick_and_cas_in_the_entry_come_from_kite(self):
        old = {k: v for k, v in REEL_ENTRY.items() if k not in ("cas", "tick_size")}
        self.write(os.path.join("trader", "entry.json"), old)
        code, text, err = self.plan("--json")
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        self.assertEqual((p["tick_size"], p["cas"], p["stop"]), (0.1, True, 1816.7))
        self.assertEqual(len(self.net.requests), 2)

    def test_refusals_from_the_cli(self):
        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, status="waiting", time=None, price=None,
                                                              bar=None, bar_start=None))
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("HINDUNILVR is waiting, not triggered", err)
        code, text, _ = self.plan("--entry", "1829.60", "--json")    # plan ahead at the level
        self.assertEqual(code, 0)
        p = json.loads(text)
        self.assertEqual((p["entry"], p["stop"], p["entry_time"], p["entry_bar"]), (1829.6, 1816.7, None, None))

        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, setup="level", level=1829.0))
        code, _, err = self.plan()
        self.assertIn("level setup has no opening-range stop", err)
        self.assertEqual(self.plan("--stop", "1%")[0], 0)

        code, _, err = self.plan("--side", "sell")
        self.assertIn("longs only", err)
        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, side="sell"))
        code, _, err = self.plan()
        self.assertIn("longs only", err)
        code, _, err = self.plan("--stop", "1831.90")
        self.assertIn("longs only", err)
        self.write(os.path.join("trader", "entry.json"), REEL_ENTRY)
        for stop, why in (("1831.90", "closer than 2 ticks"), ("1600", "cover-order range"), ("1840", "at or above")):
            code, _, err = self.plan("--stop", stop)
            self.assertEqual(code, 1)
            self.assertIn(why, err)
        code, _, err = self.plan("--target", "1800")
        self.assertIn("at or below the entry", err)
        code, _, err = self.plan("--stop", "soon")
        self.assertIn("--stop is or-mid, or-low, a price, N% or NxATR", err)
        os.remove(os.path.join("trader", "entry.json"))
        code, _, err = self.plan()
        self.assertIn("no entry", err)
        code, _, err = self.plan("--symbol", "HINDUNILVR")
        self.assertIn("needs --entry", err)
        code, _, err = self.plan("--symbol", "HINDUNILVR", "--from", "x.json", "--entry", "1")
        self.assertIn("not both", err)
        code, _, err = self.plan("--symbol", "HINDUNILVR", "--entry", "1832", "--or-high", "1829.5")
        self.assertIn("both --or-high and --or-low", err)
        code, _, err = self.plan("--symbol", "ZZZZ", "--entry", "100", "--stop", "1%")
        self.assertIn("ZZZZ is not listed on NSE", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(run([])[0], 1)

    def test_no_session_is_one_plain_line(self):
        test_entry.kite.Kite.connect = self.saved[3]
        os.environ["KITE_API_KEY"] = "key"
        try:
            code, _, err = self.plan("--symbol", "HINDUNILVR", "--entry", "1832", "--or-high", "1829.5", "--or-low", "1804")
        finally:
            del os.environ["KITE_API_KEY"]
        self.assertEqual(code, 1)
        self.assertIn("not logged in to Kite", err)
        self.assertNotIn("Traceback", err)

    def test_flat_by_config_cas_square_off_and_late_entries(self):
        self.write(os.path.join("trader", "entry.json"), REEL_ENTRY)
        self.write(os.path.join("trader", "config.json"), {"flat_by": "15:00", "min_reward_risk": 1.5})
        code, text, _ = self.plan("--target", "1R", "--json")
        p = json.loads(text)
        self.assertEqual(p["flat_by"], "15:00")
        self.assertIn("under your min_reward_risk 1.5", p["warnings"][0])
        self.write(os.path.join("trader", "config.json"), {"flat_by": "15:55"})      # a leftover from elsewhere
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("flat_by in trader/config.json 15:55 is at or after 15:12", err)
        self.assertIn("squares off MIS positions in HINDUNILVR", err)
        self.assertEqual(self.plan("--flat-by", "15:05")[0], 0)                       # the flag wins
        os.remove(os.path.join("trader", "config.json"))

        code, _, err = self.plan("--flat-by", "15:12")
        self.assertEqual(code, 1)
        self.assertIn("--flat-by 15:12 is at or after 15:12", err)
        self.assertIn("Rs 50 + GST", err)
        self.assertEqual(json.loads(self.plan("--flat-by", "15:11", "--json")[1])["flat_by"], "15:11")
        code, _, err = self.plan("--flat-by", "three")
        self.assertIn("--flat-by wants an IST time", err)

        plain = dict(REEL_ENTRY, cas=False)                                        # not a CAS stock: 15:30 close
        self.write(os.path.join("trader", "entry.json"), plain)
        self.assertEqual(json.loads(self.plan("--flat-by", "15:29", "--json")[1])["flat_by"], "15:29")
        code, _, err = self.plan("--flat-by", "15:30")
        self.assertIn("at or after the 15:30 close", err)
        code, text, _ = self.plan()
        self.assertIn("Flat by       15:05 IST, win or lose: no overnight hold\n", text)

        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, time="15:04", bar=70))
        self.assertEqual(self.plan()[0], 0)
        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, time="15:05", bar=70))
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("no time left to hold it", err)

    def test_warnings_for_other_days(self):
        self.write(os.path.join("trader", "entry.json"), REEL_ENTRY)
        code, text, _ = run(["plan", "--now", "2026-10-30 09:00", "--json"])
        self.assertIn("This plan is for 2026-10-29, not today (2026-10-30).", json.loads(text)["warnings"])
        code, text, _ = self.plan("--date", "2026-10-31", "--json")
        self.assertIn("2026-10-31 is not an NSE trading day (weekend).", json.loads(text)["warnings"])
        code, _, err = self.plan("--date", "yesterday")
        self.assertIn("date wants YYYY-MM-DD", err)

    def test_show_without_a_plan_or_with_a_broken_one(self):
        code, text, _ = run(["show"])
        self.assertEqual(code, 1)
        self.assertIn("No plan", text)
        self.write(os.path.join("trader", "plan.json"), {"symbol": "HINDUNILVR"})
        code, _, err = run(["show"])
        self.assertEqual(code, 1)
        self.assertIn("is missing 'date'", err)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-exit", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\nname: trader-exit\n"))
        fm = test_entry.re.match(r"^---\n(.*?)\n---\n", text, test_entry.re.S).group(1)
        desc = " ".join(fm[fm.index("description:"):fm.index("argument-hint:")].split())
        self.assertGreater(len(desc), 200)
        self.assertIn("Exit Planner.", desc)
        self.assertIn("Use when the user says", desc)
        self.assertGreater(len(text.splitlines()), 60)
        for must in ('"${CLAUDE_SKILL_DIR}/exit.py"', "## What it reads and writes", "## How Claude runs it",
                     "## Fine print"):
            self.assertIn(must, text)
        low = " ".join(text.lower().split()).replace("${claude_skill_dir}", "")
        for must in ("before the entry", "a stop is a plan, not a guarantee", "day trading loses most people money",
                     "/trader-timemachine", "/trader-budget", "longs only", "not financial advice", "says yes",
                     "rs 1,816.70", "rs 1,847.30", "rs 15.30", "1,862.60", "tick", "10 %", "15:12", "15:05",
                     "cover order", "2 ticks"):
            self.assertIn(must, low, must)
        for never in ("\u2014", "yahoo", "nyse", "alpaca", "$", " et ", "new york", "cent", "tick " + "by tick",
                      "real " + "time", "pro" + "fit"):
            self.assertNotIn(never, low, never)


if __name__ == "__main__":
    unittest.main()
