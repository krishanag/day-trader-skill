import copy
import datetime
import gzip
import hashlib
import io
import json
import os
import random
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

tm = load("trader-timemachine", "timemachine")
kite = tm.kite   # the module timemachine.py imported, so the network fakes reach it
IST = kite.IST

DIR = os.path.join(FIX, "trader-timemachine")
# Kite 5minute candles for RELIANCE, 2026-08-03 to 2026-10-29 (the 60 NSE trading days before
# 2026-10-29 plus that day), fetched 2026-10-29 18:53 IST after the close: 61 sessions of 72 bars
BARS = os.path.join(DIR, "RELIANCE-60d5m.json.gz")
with gzip.open(BARS, "rt", encoding="utf-8") as _fh:
    SAVED = json.load(_fh)
CANDLES, INFO = tm.parse_saved(json.dumps(SAVED))
# the plan trader-exit would write for that day (FILES.md): the 15-minute range 1155.60 to 1170.50,
# the 09:35 bar closed above it at 1174.40 (bar 5, closing 09:40), stop at the midpoint 1163.05
# snapped down to the 0.10 tick, 1R target
with open(os.path.join(DIR, "plan.json"), encoding="utf-8") as _fh:
    PLAN = json.load(_fh)
NOW = "2026-10-29 10:50"
NOW_IST = tm.parse_now(NOW)

# fingerprints of the seed-19 run on the fixture: proves the draws are bit for bit the same everywhere
SIM_BANDS = "6095fa4a3cb55b7b"
SIM_PATHS = "1a727a086edee937"
SIM_TRADES = "8a10d37ee9ad9f53"
SIM_HIST = {"lo": 1149.45, "hi": 1196.99,
            "bins": [97, 80, 122, 193, 251, 379, 451, 519, 644, 697, 772, 788, 821, 737, 667, 563, 520, 453,
                     311, 274, 199, 136, 103, 62]}
VERDICT = "Leans toward the stop: target first 22.9 % vs stop first 28.0 %."
NOTE = "49.0 % of the replays hit neither by 15:05; the price at 15:05 decides those."

INSTRUMENTS = (
    "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,"
    "lot_size,instrument_type,segment,exchange\n"
    "738561,2885,RELIANCE,RELIANCE INDUSTRIES,0,,0,0.1,1,EQ,NSE,NSE\n"
    "408065,1594,INFY,INFOSYS,0,,0,0.05,1,EQ,NSE,NSE\n")


def sha(x):
    return hashlib.sha256(json.dumps(x).encode("utf-8")).hexdigest()[:16]


def ist(text):
    return datetime.datetime.fromisoformat(text).replace(tzinfo=IST)


def plan(**kw):
    raw = dict(PLAN)
    raw.update(kw)
    return tm.normalize_plan(raw)


def machine(p=None, candles=CANDLES, now=NOW_IST, info=INFO, source="offline", **kw):
    return tm.time_machine(candles, p or plan(), now, info=info, source=source, **kw)


def edited(fn):
    """The fixture's rows, changed by fn(rows) -> (candles, info) as the offline reader gives them."""
    doc = copy.deepcopy(SAVED)
    fn(doc["candles"])
    return tm.parse_saved(json.dumps(doc))


def keep_rows(rows, keep):
    """Keep only the rows whose '2026-10-29T09:15' stamp passes keep(stamp)."""
    rows[:] = [r for r in rows if keep(r[0][:16])]


def longer_sessions(rows, days=None):
    """Give every session (or the ones in days) the 15:15, 15:20 and 15:25 bars of a non-CAS stock."""
    out = []
    for r in rows:
        out.append(r)
        if r[0][11:16] == "15:10" and (days is None or r[0][:10] in days):
            for t in ("15:15", "15:20", "15:25"):
                out.append([r[0][:11] + t + r[0][16:]] + r[1:])
    rows[:] = out


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = tm.main(argv)
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
    """Records requests and answers them from a queue."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def ok(data):
    return FakeResponse(json.dumps({"status": "success", "data": data}))


def http_error(code, etype, message):
    body = io.BytesIO(json.dumps({"status": "error", "error_type": etype, "message": message}).encode())
    return urllib.error.HTTPError("https://api.kite.trade/x", code, message, {}, body)


class Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = machine()

    def test_the_odds(self):
        m = self.doc["mc"]
        self.assertEqual((m["replays"], m["seed"], m["window"], m["until"]), (10000, 19, 2, "15:05"))
        self.assertEqual(m["pool_sessions"], 60)
        self.assertEqual(m["bars_left"], 65)
        self.assertEqual((m["pool_from"], m["pool_to"]), ("2026-08-03", "2026-10-28"))
        self.assertEqual(m["p_target"], 0.2292)
        self.assertEqual(m["p_stop"], 0.2804)
        self.assertEqual(m["p_neither"], 0.4904)
        self.assertEqual(m["p_green"], 0.4527)
        self.assertEqual(m["median_close"], 1173.06)
        self.assertEqual(m["p5_close"], 1156.95)
        self.assertEqual(m["p95_close"], 1189.91)
        self.assertEqual(m["avg_R"], -0.074)
        self.assertEqual(m["r_at_target"], 1.0)

    def test_bit_for_bit_the_same_draws(self):
        m = self.doc["mc"]
        self.assertEqual(sha(m["bands"]), SIM_BANDS)
        self.assertEqual(len(m["sample_paths"]), 160)
        self.assertEqual(sha(m["sample_paths"]), SIM_PATHS)
        self.assertEqual(m["hist"], SIM_HIST)
        self.assertEqual(m["bands"][-1], [1156.81, 1166.4, 1173.05, 1179.87, 1189.93])

    def test_the_history_replay(self):
        h = self.doc["history"]
        self.assertEqual((h["sessions"], h["triggered"]), (60, 27))
        self.assertEqual((h["target"], h["stop"], h["close"], h["green"]), (13, 11, 3, 15))
        self.assertEqual(h["avg_R"], 0.065)
        self.assertEqual(h["rule"], {"setup": "orb", "or_minutes": 15, "stop_rule": "or_mid", "target_r": 1.0})
        slim = [{"date": t["date"], "entry": t["entry"], "outcome": t["outcome"], "R": t["R"]} for t in h["trades"]]
        self.assertEqual(sha(slim), SIM_TRADES)
        first = h["trades"][0]
        self.assertEqual((first["date"], first["time"], first["entry"], first["stop"], first["target"],
                          first["outcome"]), ("2026-08-06", "09:50", 1303.5, 1291.1, 1315.9, "target"))
        for t in h["trades"]:   # stops and targets sit on the 0.10 tick
            self.assertAlmostEqual(round(t["stop"] * 10) / 10, t["stop"], places=6, msg=t)
            self.assertAlmostEqual(round(t["target"] * 10) / 10, t["target"], places=6, msg=t)

    def test_the_verdict_leans_to_the_stop(self):
        self.assertEqual(self.doc["verdict"], VERDICT)
        # 1R breaks even at 50 % of the replays that hit one side; this target got 45.0 %
        gap, share, even = tm.lean(self.doc["mc"])
        self.assertEqual((round(share, 3), even), (0.45, 0.5))
        self.assertEqual(self.doc["verdict_notes"], [NOTE])

    def test_bands_start_at_the_entry_and_end_at_flat_by(self):
        m = self.doc["mc"]
        self.assertEqual(m["bands"][0], [1174.4] * 5)
        self.assertEqual(len(m["bands"]), 66)
        self.assertEqual((m["band_times"][0], m["band_times"][-1], len(m["band_times"])), ("09:40", "15:05", 66))
        self.assertTrue(all(p[0] == 1174.4 and len(p) == 66 for p in m["sample_paths"]))

    def test_the_plan_is_carried_through(self):
        p = self.doc["plan"]
        self.assertEqual((p["symbol"], p["entry"], p["stop"], p["target"], p["entry_bar"], p["entry_time"]),
                         ("RELIANCE", 1174.4, 1163.0, 1185.8, 5, "09:40"))
        self.assertEqual((p["risk_per_share"], p["target_r"], p["flat_by"]), (11.4, 1.0, "15:05"))
        self.assertEqual((p["cas"], p["tick_size"], p["interval"]), (True, 0.1, "5minute"))
        self.assertNotIn("calendar_note", p)
        self.assertEqual(self.doc["warnings"], [])

    def test_the_document_is_ist_and_nse(self):
        d = self.doc["data"]
        self.assertEqual(self.doc["created_at"], "2026-10-29T10:50:00+05:30")
        self.assertEqual((d["symbol"], d["exchange"], d["interval"], d["cas"]), ("RELIANCE", "NSE", "5minute", True))
        self.assertEqual((d["session_bars"], d["session_close"], d["pool_days_asked"]), (72, "15:15", 60))
        self.assertEqual(d["last_bar"], "2026-10-29 10:45 IST")
        self.assertEqual(d["dropped"], {"off_grid": 0, "not_complete": 53, "outside_hours": 0, "after_close": 0})
        self.assertEqual(d["short_sessions_left_out"], [])
        self.assertEqual(self.doc["source"], "offline")

    def test_the_text_report(self):
        text = "\n".join(tm.report(self.doc))
        for line in ("RELIANCE buy, Thursday 2026-10-29: entry Rs 1,174.40 at 09:40 (bar 5), stop Rs 1,163.00, "
                     "target Rs 1,185.80 (1R, risk Rs 11.40 a share).",
                     "10,000 replays", "09:40 to 15:05 IST (65 five-minute bars)", "seed 19", "60 full NSE sessions",
                     "target first   22.9 %", "stop first     28.0 %", "neither        49.0 %   still open at 15:05",
                     "green          45.3 %", "median Rs 1,173.06, 90 % of replays between Rs 1,156.95 and Rs 1,189.91",
                     "Average R: -0.074", "flat by 15:05", "fired 27 times: 13 target, 11 stop, 3 neither. "
                     "Average R +0.065", VERDICT + "\n" + NOTE, "not a prediction", "--seed 19"):
            self.assertIn(line, text)
        for word in ("profit", "P&L", "guarantee", "real time", "tick by tick", "$", "Yahoo", " ET", "PM", "AM"):
            self.assertNotIn(word, text)


class Seeds(unittest.TestCase):
    def test_same_seed_same_numbers(self):
        a = machine(replays=2000, seed=5)["mc"]
        b = machine(replays=2000, seed=5)["mc"]
        self.assertEqual(a, b)

    def test_another_seed_gives_other_numbers(self):
        m = machine(seed=7)["mc"]
        self.assertNotEqual((m["p_target"], m["p_stop"], m["median_close"]), (0.2292, 0.2804, 1173.06))
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=6)
        self.assertNotEqual(sha(m["bands"]), SIM_BANDS)

    def test_the_draws_are_stable_across_pythons(self):
        # random.Random(seed), randint and choice are stable since Python 3.2; the fixture numbers above
        # are the real proof, this pins the first draws so a failure points at the RNG
        r = random.Random(19)
        self.assertEqual([r.randint(-2, 2) for _ in range(8)], [-2, 2, -2, 2, -1, 1, 0, 2])
        self.assertEqual(r.choice(range(60)), 18)

    def test_window_and_replays(self):
        a = machine(replays=500, window=0)["mc"]
        self.assertEqual((a["replays"], a["window"]), (500, 0))
        self.assertNotEqual(a["bands"], machine(replays=500)["mc"]["bands"])
        with self.assertRaises(tm.TMError):
            machine(replays=0)
        with self.assertRaises(tm.TMError):
            machine(window=11)


class Bars(unittest.TestCase):
    def test_the_forming_candle_is_dropped(self):
        bars, dropped = tm.parse_bars(CANDLES, NOW_IST)
        self.assertEqual(dropped, {"off_grid": 0, "not_complete": 53, "outside_hours": 0})
        last = bars[-1]
        self.assertEqual((last["date"], last["start"], last["slot"]), ("2026-10-29", "10:45", 18))
        entry = [b for b in bars if b["date"] == "2026-10-29" and b["slot"] == 4][0]
        self.assertEqual((entry["start"], entry["c"], entry["h"], entry["l"]), ("09:35", 1174.4, 1175.8, 1169.0))

    def test_the_real_clock_after_the_close_gives_the_same_odds(self):
        doc = machine(now=tm.parse_now(None))
        self.assertEqual(doc["data"]["dropped"]["not_complete"], 0)
        self.assertEqual(doc["data"]["last_bar"], "2026-10-29 15:10 IST")
        self.assertEqual((doc["mc"]["p_target"], doc["mc"]["p_stop"]), (0.2292, 0.2804))
        self.assertEqual(doc["history"]["triggered"], 27)

    def test_now_pins_the_clock(self):
        bars = tm.parse_bars(CANDLES, tm.parse_now("2026-10-29 09:37"))[0]
        self.assertEqual(len([b for b in bars if b["date"] == "2026-10-29"]), 4)
        with self.assertRaises(tm.TMError) as cm:
            machine(now=tm.parse_now("2026-10-29 09:37"))
        self.assertIn("not in the data", str(cm.exception))
        self.assertIn("the data stops at the 09:30 bar", str(cm.exception))
        self.assertEqual(machine(now=tm.parse_now("2026-10-29 09:40"), replays=200)["mc"]["pool_sessions"], 60)

    def test_now_accepts_ist_and_utc(self):
        self.assertEqual(tm.parse_now("2026-10-29T05:20:00Z"), ist("2026-10-29T10:50"))
        self.assertEqual(tm.parse_now("2026-10-29 10:50").utcoffset(), datetime.timedelta(hours=5, minutes=30))
        with self.assertRaisesRegex(tm.TMError, "IST time like"):
            tm.parse_now("half past ten")

    def test_bars_off_the_grid_or_outside_hours(self):
        rows = [(ist("2026-10-28T09:10"), 1, 1, 1, 1, 1),      # pre-open: outside hours
                (ist("2026-10-28T09:17"), 1, 1, 1, 1, 1),      # off the 5-minute grid
                (ist("2026-10-28T09:15:30"), 1, 1, 1, 1, 1),   # off the grid by seconds
                (ist("2026-10-28T15:25"), 1, 1, 1, 1, 1),      # a non-CAS stock's last bar: kept as slot 74
                (ist("2026-10-28T15:30"), 1, 1, 1, 1, 1),      # after every close
                (datetime.datetime(2026, 10, 28, 3, 45, tzinfo=datetime.timezone.utc), 1, 1, 1, 1, 1)]  # 09:15 IST
        bars, dropped = tm.parse_bars(rows, NOW_IST)
        self.assertEqual(dropped, {"off_grid": 2, "not_complete": 0, "outside_hours": 2})
        self.assertEqual([(b["start"], b["slot"]) for b in bars], [("09:15", 0), ("15:25", 74)])

    def test_a_short_session_is_left_out(self):
        def cut(rows):  # 2026-09-04 stops at 13:55: 57 bars
            keep_rows(rows, lambda s: not (s[:10] == "2026-09-04" and s[11:] >= "14:00"))
        doc = machine(candles=edited(cut)[0], replays=300)
        self.assertEqual(doc["mc"]["pool_sessions"], 59)
        self.assertEqual(doc["data"]["short_sessions_left_out"], [{"date": "2026-09-04", "bars": 57}])
        self.assertEqual(doc["history"]["sessions"], 59)
        self.assertNotIn("2026-09-04", [t["date"] for t in doc["history"]["trades"]])
        self.assertIn("Left out 1 short session", "\n".join(tm.report(doc)))

    def test_today_is_not_in_the_pool(self):
        doc = machine(replays=200)
        self.assertEqual(doc["mc"]["pool_to"], "2026-10-28")
        self.assertNotIn("2026-10-29", [t["date"] for t in doc["history"]["trades"]])

    def test_a_missing_bar_makes_a_gap(self):
        def hole(rows):
            keep_rows(rows, lambda s: s != "2026-08-05T10:00")
        doc = machine(candles=edited(hole)[0], replays=200)
        self.assertEqual(doc["mc"]["pool_sessions"], 59)
        self.assertEqual(doc["data"]["short_sessions_left_out"], [{"date": "2026-08-05", "bars": 71}])

    def test_empty_and_broken_data(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(candles=[])
        self.assertIn("no complete 5-minute bars", str(cm.exception))
        with self.assertRaisesRegex(tm.TMError, "not JSON"):
            tm.parse_saved("<html>rate limited</html>")
        with self.assertRaisesRegex(tm.TMError, "saved bar file"):
            tm.parse_saved(json.dumps({"status": "success", "data": {"candles": []}}))
        with self.assertRaisesRegex(tm.TMError, "bad candle row"):
            tm.parse_saved(json.dumps({"candles": [["2026-10-29T09:15:00+0530", 1, 2]]}))

    def test_fewer_than_20_sessions_is_refused(self):
        def trim(rows):
            keep_rows(rows, lambda s: s[:10] >= "2026-09-30")
        with self.assertRaises(tm.TMError) as cm:
            machine(candles=edited(trim)[0])
        self.assertIn("only 19 full session(s)", str(cm.exception))
        self.assertIn("at least 20", str(cm.exception))

        def twenty(rows):
            keep_rows(rows, lambda s: s[:10] >= "2026-09-29")
        self.assertEqual(machine(candles=edited(twenty)[0], replays=200)["mc"]["pool_sessions"], 20)

    def test_a_non_cas_stock_runs_to_15_30(self):
        candles, _ = edited(longer_sessions)
        doc = machine(plan(cas=False), candles=candles, replays=300)
        self.assertEqual((doc["data"]["cas"], doc["data"]["session_bars"], doc["data"]["session_close"]),
                         (False, 75, "15:30"))
        self.assertEqual((doc["mc"]["until"], doc["mc"]["bars_left"]), ("15:05", 65))   # flat_by still rules
        self.assertEqual(doc["mc"]["pool_sessions"], 60)
        late = machine(plan(cas=False), candles=candles, replays=300, until="15:30")
        self.assertEqual((late["mc"]["until"], late["mc"]["bars_left"], late["mc"]["band_times"][-1]),
                         ("15:30", 70, "15:30"))
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(cas=False), candles=candles, replays=10, until="15:35")
        self.assertIn("no later than 15:30 (the close for a non-CAS stock)", str(cm.exception))
        with self.assertRaises(tm.TMError) as cm:
            machine(replays=10, until="15:20")
        self.assertIn("no later than 15:15 (the close for a CAS stock)", str(cm.exception))

    def test_the_cas_flag_is_checked_against_the_bars(self):
        doc = machine(plan(cas=False), replays=200)   # 72-bar sessions say CAS, whatever the plan says
        self.assertEqual((doc["data"]["cas"], doc["data"]["session_bars"], doc["plan"]["cas"]), (True, 72, True))
        self.assertEqual(doc["warnings"], ["the plan says cas false but 60 of the past sessions have 72 bars "
                                           "and only 0 have 75; going by the bars"])
        self.assertIn("Note: the plan says cas false", "\n".join(tm.report(doc)))
        raw = dict(PLAN)
        del raw["cas"]
        doc = machine(tm.normalize_plan(raw), info=dict(INFO, cas=None), replays=200)   # no flag anywhere
        self.assertEqual((doc["data"]["cas"], doc["plan"]["cas"], doc["warnings"]), (True, True, []))
        candles, _ = edited(longer_sessions)
        doc = machine(tm.normalize_plan(raw), candles=candles, info=dict(INFO, cas=None), replays=200)
        self.assertEqual((doc["data"]["cas"], doc["data"]["session_bars"]), (False, 75))

    def test_bars_after_a_cas_stocks_close_are_dropped_not_the_session(self):
        # sessions from before the CAS started (3 Aug 2026) ran to 15:25: the extra bars go, the day stays
        early = {"2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"}
        candles, _ = edited(lambda rows: longer_sessions(rows, early))
        doc = machine(candles=candles, replays=200)
        self.assertEqual((doc["data"]["cas"], doc["data"]["session_bars"]), (True, 72))
        self.assertEqual(doc["data"]["dropped"]["after_close"], 15)
        self.assertEqual(doc["mc"]["pool_sessions"], 60)
        self.assertEqual(doc["data"]["short_sessions_left_out"], [])

    def test_the_data_must_be_the_plans_symbol(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(symbol="INFY"))
        self.assertIn("data is for RELIANCE but the plan is for INFY", str(cm.exception))
        with self.assertRaises(tm.TMError) as cm:
            machine(info=dict(INFO, interval="minute"))
        self.assertIn("needs 5-minute bars", str(cm.exception))

    def test_fetch_window_is_60_trading_days_to_the_plan_day(self):
        start, end = tm.fetch_window(datetime.date(2026, 10, 29), NOW_IST)
        self.assertEqual((start, end), (ist("2026-08-03T09:00"), ist("2026-10-29T10:50")))
        start, end = tm.fetch_window(datetime.date(2026, 10, 29), ist("2026-10-30T11:00"))
        self.assertEqual((start, end), (ist("2026-08-03T09:00"), ist("2026-10-29T15:40")))
        self.assertEqual(tm.fetch_window(datetime.date(2026, 10, 21), ist("2026-10-21T16:00"))[0],
                         ist("2026-07-24T09:00"))   # 2026-10-20 (Dussehra) and 2026-10-02 are skipped
        with self.assertRaisesRegex(tm.TMError, "before the first pool day"):
            tm.fetch_window(datetime.date(2026, 10, 29), ist("2026-08-01T10:00"))


class Plans(unittest.TestCase):
    def test_an_entry_not_in_the_data_is_refused(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(date="2026-10-30"))
        self.assertIn("not in the data", str(cm.exception))
        self.assertIn("no bars for that day", str(cm.exception))
        with self.assertRaises(tm.TMError) as cm:      # the 10:50 bar has not closed at 10:50
            machine(plan(entry_time="10:55", entry_bar=None))
        self.assertIn("not in the data", str(cm.exception))
        self.assertIn("no complete 10:50 bar (closing 10:55)", str(cm.exception))

    def test_plan_checks(self):
        cases = [
            (dict(entry_time="09:42", entry_bar=None), "5-minute grid"),
            (dict(entry_time="15:30", entry_bar=None), "between 09:20 and 15:25"),
            (dict(entry_bar=6), "disagrees with itself"),
            (dict(entry_bar="five", entry_time=None), "counted from 1"),
            (dict(entry_time=None, entry_bar=None), "entry_time"),
            (dict(interval="minute"), "5-minute bars only"),
            (dict(stop=1175.0), "stop < entry < target"),
            (dict(side="short"), "buy or sell"),
            (dict(symbol=""), "trading symbol"),
            (dict(symbol="RELIANCE INDUSTRIES"), "trading symbol"),
            (dict(entry="abc"), "number for entry"),
            (dict(tick_size=0), "tick_size"),
            (dict(date="Oct 29"), "date like"),
            (dict(date="2026-10-20"), "not an NSE trading day (NSE holiday: Dussehra)"),
            (dict(date="2026-10-31"), "not an NSE trading day (weekend)"),
            (dict(opening_range={"minutes": 7}), "multiple of 5"),
            (dict(cas="yes"), "true or false"),
            (dict(flat_by="noon"), "flat_by wants an IST time"),
        ]
        for change, words in cases:
            with self.assertRaises(tm.TMError, msg=change) as cm:
                plan(**change)
            self.assertIn(words, str(cm.exception), change)

    def test_entry_bar_or_entry_time_alone(self):
        self.assertEqual(plan(entry_time=None)["entry_time"], "09:40")
        self.assertEqual(plan(entry_bar=None)["entry_bar"], 5)
        self.assertEqual(plan(entry_time="9:40", entry_bar=None)["entry_bar"], 5)
        self.assertEqual(plan(entry_time="9:40 AM", entry_bar=None)["entry_bar"], 5)
        self.assertEqual(plan(entry_time="2:15 pm", entry_bar=None)["entry_bar"], 60)
        self.assertEqual(plan(entry_time="2:15", entry_bar=None)["entry_bar"], 60)   # a bare 2 is afternoon
        self.assertEqual(plan(entry_time="10:15", entry_bar=12)["entry_bar"], 12)    # FILES.md's example pair

    def test_symbol_spellings_and_interval_alias(self):
        for raw in ("reliance", "RELIANCE.NS", "NSE:RELIANCE"):
            self.assertEqual(plan(symbol=raw)["symbol"], "RELIANCE")
        self.assertEqual(plan(symbol="M&M", stop=1163.0)["symbol"], "M&M")
        self.assertEqual(plan(interval="5m")["interval"], "5minute")

    def test_a_date_past_the_known_calendar_is_a_warning(self):
        p = plan(date="2027-01-04")
        self.assertIn("2027", p["calendar_note"])
        with self.assertRaises(tm.TMError) as cm:   # no bars for it, but the note rides along until then
            machine(p)
        self.assertIn("no bars for that day", str(cm.exception))

    def test_until_cuts_the_replay(self):
        doc = machine(until="15:00", replays=500)
        m = doc["mc"]
        self.assertEqual((m["until"], m["bars_left"], m["band_times"][-1]), ("15:00", 64, "15:00"))
        self.assertIn("still open at 15:00", "\n".join(tm.report(doc)))
        self.assertEqual(machine(until="3:00 PM", replays=500)["mc"], m)
        for bad, words in (("09:40", "nothing to replay"), ("15:20", "no later than 15:15"),
                           ("15:02", "5-minute grid"), ("noon", "IST time like")):
            with self.assertRaises(tm.TMError) as cm:
                machine(until=bad, replays=10)
            self.assertIn(words, str(cm.exception), bad)

    def test_flat_by_comes_from_the_plan(self):
        m = machine(plan(flat_by="14:00"), replays=300)["mc"]
        self.assertEqual((m["until"], m["bars_left"], m["band_times"][-1]), ("14:00", 52, "14:00"))
        doc = machine(plan(flat_by=None), replays=300)
        self.assertEqual((doc["mc"]["until"], doc["plan"]["flat_by"]), ("15:05", "15:05"))   # the pack default
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(flat_by="09:40"), replays=10)
        self.assertIn("flat_by 09:40 leaves nothing to replay after the 09:40 entry", str(cm.exception))
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(flat_by="15:30"), replays=10)
        self.assertIn("flat_by must be on the 5-minute grid and no later than 15:15", str(cm.exception))

    def test_an_entry_outside_its_bar_is_flagged(self):
        doc = machine(plan(entry=1180.0, stop=1170.0, target=1190.0), replays=200)
        self.assertIn("outside that bar's range 1169.00 to 1175.80", doc["warnings"][0])
        self.assertIn("Note: the plan's entry 1180.00 is outside", "\n".join(tm.report(doc)))

    def test_prices_snap_to_the_tick(self):
        self.assertEqual(tm.tick_away(1163.05, 0.1, True), 1163.0)     # a buy's stop goes down
        self.assertEqual(tm.tick_away(1163.05, 0.1, False), 1163.1)    # a sell's stop goes up
        self.assertEqual(tm.tick_away(1163.0, 0.1, True), 1163.0)      # already on the tick
        self.assertEqual(tm.tick_away(1163.05, 0.05, True), 1163.05)
        self.assertEqual(tm.tick_away(249.126, 0.01, True), 249.12)
        self.assertEqual(tm.tick_away(1162.99999999999, 0.1, True), 1163.0)   # float noise, not a tick below
        self.assertEqual(tm.tick_toward(1185.87, 0.1, True), 1185.8)   # a buy's target goes down
        self.assertEqual(tm.tick_toward(1150.03, 0.1, False), 1150.1)  # a sell's target goes up

    def test_the_stop_rule_is_worked_out_when_not_given(self):
        rule = machine(plan(stop_rule=None), replays=100)["history"]["rule"]
        self.assertEqual(rule["stop_rule"], "or_mid")
        rule = machine(plan(stop_rule=None, stop=1155.6, target=1193.2), replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["target_r"]), ("or_low", 1.0))
        rule = machine(plan(stop_rule="atr", stop=1168.0, target=1187.2), replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["stop_pct"], rule["target_r"]), ("pct", 0.545, 2.0))
        rule = machine(plan(stop_rule="price", stop=1168.0, target=1187.2), replays=100)["history"]["rule"]
        self.assertEqual(rule["stop_rule"], "pct")

    def test_a_setup_other_than_orb_skips_the_history(self):
        doc = machine(plan(setup="flag"), replays=200)
        self.assertIsNone(doc["history"]["rule"])
        self.assertIn("orb", doc["history"]["note"])
        self.assertEqual(doc["mc"]["pool_sessions"], 60)
        self.assertIn("History: skipped", "\n".join(tm.report(doc)))

    def test_a_sell_plan_mirrors_the_rules(self):
        doc = machine(plan(side="sell", stop=1185.8, target=1163.0, stop_rule="or_high"), replays=2000)
        m = doc["mc"]
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=6)
        self.assertEqual(m["r_at_target"], 1.0)
        self.assertEqual(doc["history"]["rule"]["stop_rule"], "or_high")
        self.assertEqual(len(doc["history"]["trades"]), 40)
        for t in doc["history"]["trades"]:
            self.assertTrue(t["target"] < t["entry"] < t["stop"], t)
        self.assertIn("below the entry", "\n".join(tm.report(doc)))

    def test_the_verdict_thresholds_at_1r(self):
        # 1R: the target's share of the replays that hit one side against 50 %
        self.assertTrue(tm.verdict({"p_target": 0.30, "p_stop": 0.28}).startswith("Close to a coin flip"))
        self.assertTrue(tm.verdict({"p_target": 0.40, "p_stop": 0.30}).startswith("Leans toward the target"))
        self.assertTrue(tm.verdict({"p_target": 0.20, "p_stop": 0.30}).startswith("Leans toward the stop"))
        self.assertTrue(tm.verdict({"p_target": 0.60, "p_stop": 0.30}).startswith("Strongly toward the target"))
        self.assertEqual(tm.verdict({"p_target": 0.10, "p_stop": 0.60, "r_at_target": 1.0}),
                         "Strongly toward the stop: target first 10.0 % vs stop first 60.0 %.")
        self.assertEqual(tm.verdict({"p_target": 0.0, "p_stop": 0.0, "until": "15:00"}),
                         "No replay reached the target or the stop by 15:00: the price at 15:00 decides every one.")
        self.assertIn("by 15:05", tm.verdict({"p_target": 0.0, "p_stop": 0.0}))

    def test_a_2r_target_is_judged_against_its_break_even(self):
        mc = {"p_target": 0.241, "p_stop": 0.505, "p_neither": 0.254, "r_at_target": 2.0, "avg_R": 0.095}
        self.assertEqual(tm.verdict(mc), "Close to break-even: at 2R the target has to come first in 33 % of the "
                                         "replays that hit one side; it did in 32 % (target first 24.1 % vs stop "
                                         "first 50.5 %).")
        self.assertEqual(tm.verdict_notes(mc), [])
        lean = dict(mc, p_target=0.15, p_stop=0.55)
        self.assertTrue(tm.verdict(lean).startswith("Leans toward the stop: at 2R the target has to come first in "
                                                    "33 % of the replays that hit one side; it did in 21 %"))
        self.assertEqual(tm.verdict_notes(lean), ["Average R is +0.095 all the same: the replays that hit neither "
                                                  "pull it the other way."])
        self.assertTrue(tm.verdict(dict(mc, p_target=0.30, p_stop=0.40)).startswith("Leans toward the target"))
        self.assertTrue(tm.verdict(dict(mc, p_target=0.40, p_stop=0.40)).startswith("Strongly toward the target"))
        self.assertIn("at 1.5R the target has to come first in 40 %", tm.verdict(dict(mc, r_at_target=1.5)))

    def test_2r_on_the_fixture_with_a_tight_stop(self):
        # stop 1168.70 (Rs 5.70 at risk), 2R target 1185.80: 21.3 % vs 59.0 % looks lopsided, 2R needs a third
        doc = machine(plan(stop=1168.7, target=1185.8, stop_rule="pct"))
        m = doc["mc"]
        self.assertEqual((m["r_at_target"], m["p_target"], m["p_stop"], m["p_neither"], m["avg_R"]),
                         (2.0, 0.2135, 0.5902, 0.1963, -0.077))
        self.assertEqual(doc["verdict"], "Leans toward the stop: at 2R the target has to come first in 33 % of the "
                                         "replays that hit one side; it did in 27 % (target first 21.3 % vs stop "
                                         "first 59.0 %).")
        self.assertEqual(doc["verdict_notes"], [])
        self.assertEqual(doc["history"]["rule"]["stop_pct"], 0.4854)

    def test_2r_on_the_fixture_a_clear_lean(self):
        # the plan's entry and stop with a 2R target 1197.20: target first only 2.1 % vs stop first 28.1 %
        doc = machine(plan(target=1197.2))
        m = doc["mc"]
        self.assertEqual((m["r_at_target"], m["p_target"], m["p_stop"], m["p_neither"], m["avg_R"]),
                         (2.0, 0.0207, 0.2813, 0.698, -0.086))
        self.assertEqual(doc["verdict"], "Strongly toward the stop: at 2R the target has to come first in 33 % of "
                                         "the replays that hit one side; it did in 7 % (target first 2.1 % vs stop "
                                         "first 28.1 %).")
        self.assertEqual(doc["verdict_notes"],
                         ["69.8 % of the replays hit neither by 15:05; the price at 15:05 decides those."])
        text = "\n".join(tm.report(doc))
        self.assertIn(doc["verdict"] + "\n" + doc["verdict_notes"][0], text)


class Network(unittest.TestCase):
    """The live path through the shared Kite client, with the network faked."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        self.env = os.environ.get("KITE_API_KEY")
        os.environ["KITE_API_KEY"] = "key"
        os.makedirs("trader")
        kite.save_session({"access_token": "tok", "user_id": "AB1234"}, kite.now_ist())
        with open(os.path.join("trader", "plan.json"), "w") as fh:
            json.dump(PLAN, fh)
        self.net = FakeNet()
        self.saved = (kite._urlopen, kite._sleep, dict(kite._last_call))
        kite._urlopen = self.net
        kite._sleep = lambda s: None
        kite._last_call.clear()

    def tearDown(self):
        kite._urlopen, kite._sleep, last = self.saved
        kite._last_call.clear()
        kite._last_call.update(last)
        if self.env is None:
            del os.environ["KITE_API_KEY"]
        else:
            os.environ["KITE_API_KEY"] = self.env
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def answer(self, *more):
        self.net.answers.extend([FakeResponse(INSTRUMENTS, "text/csv")] + list(more))

    def test_run_over_kite_is_one_historical_call(self):
        self.answer(ok({"candles": SAVED["candles"]}))
        code, text, err = run(["run", "--now", NOW, "--json"])
        self.assertEqual(code, 0, err)
        self.assertEqual([r.full_url.split("?")[0] for r in self.net.requests],
                         ["https://api.kite.trade/instruments/NSE",
                          "https://api.kite.trade/instruments/historical/738561/5minute"])
        url = self.net.requests[1].full_url
        self.assertIn("from=2026-08-03+09%3A00%3A00", url)
        self.assertIn("to=2026-10-29+10%3A50%3A00", url)
        self.assertEqual(self.net.requests[1].get_header("Authorization"), "token key:tok")
        doc = json.loads(text)
        self.assertEqual(doc["source"], "kite")
        self.assertEqual((doc["mc"]["p_target"], doc["mc"]["p_stop"]), (0.2292, 0.2804))
        self.assertEqual((doc["data"]["from"], doc["data"]["to"]), ("2026-08-03 09:00:00", "2026-10-29 10:50:00"))
        self.assertEqual(doc["history"]["triggered"], 27)
        self.assertTrue(os.path.exists(os.path.join("trader", "cache", "instruments-NSE-%s.csv"
                                                    % kite.now_ist().date().isoformat())))

    def test_the_tick_and_cas_come_from_kite_and_the_bars_when_the_plan_has_none(self):
        self.answer(ok({"candles": SAVED["candles"]}))
        code, text, err = run(["run", "--symbol", "reliance", "--date", "2026-10-29", "--entry", "1174.40",
                               "--stop", "1163.00", "--target", "1185.80", "--entry-time", "09:40",
                               "--now", NOW, "--json", "--replays", "200"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        p = doc["plan"]
        self.assertEqual((p["symbol"], p["tick_size"], p["cas"], p["flat_by"], p["stop_rule"], p["from"]),
                         ("RELIANCE", 0.1, True, "15:05", "or_mid", "flags"))
        self.assertEqual(doc["history"]["rule"]["stop_rule"], "or_mid")

    def test_no_session_is_one_plain_line_and_no_call(self):
        os.remove(os.path.join("trader", "kite_session.json"))
        code, text, err = run(["run", "--now", NOW])
        self.assertEqual(code, 1)
        self.assertEqual(err.strip().splitlines(), ["timemachine.py: not logged in to Kite: run  python3 "
                                                    "trader-kite/login.py"])
        self.assertEqual(self.net.requests, [])

    def test_a_rejected_token_asks_for_login(self):
        self.net.answers.append(http_error(403, "TokenException", "Incorrect api_key or token"))
        code, text, err = run(["run", "--now", NOW])
        self.assertEqual(code, 1)
        self.assertIn("Kite rejected the session", err)
        self.assertIn("login.py", err)

    def test_a_429_is_retried_by_the_shared_client(self):
        self.answer(http_error(429, "NetworkException", "Too many requests"), ok({"candles": SAVED["candles"]}))
        code, text, err = run(["run", "--now", NOW, "--replays", "200"])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.net.requests), 3)
        self.assertIn("target first", text)

    def test_network_down_is_one_plain_line(self):
        self.net.answers.append(urllib.error.URLError("no route"))
        code, text, err = run(["run", "--now", NOW])
        self.assertEqual(code, 1)
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertIn("cannot reach Kite", err)

    def test_an_unknown_symbol_is_a_clear_error(self):
        self.answer()
        code, text, err = run(["run", "--symbol", "RELIANC", "--entry", "1174.4", "--stop", "1163",
                               "--target", "1185.8", "--entry-time", "09:40", "--date", "2026-10-29", "--now", NOW])
        self.assertEqual(code, 1)
        self.assertIn("RELIANC is not listed on NSE", err)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.plan = os.path.join(self.d, "trader", "plan.json")
        os.makedirs(os.path.dirname(self.plan))
        with open(self.plan, "w") as fh:
            json.dump(PLAN, fh, indent=2)
        self.out = os.path.join(self.d, "trader", "timemachine.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_run_with_the_plan_end_to_end(self):
        code, text, err = run(["run", "--plan", self.plan, "--offline", BARS, "--now", NOW, "--write", self.out])
        self.assertEqual(code, 0, err)
        self.assertIn("target first   22.9 %", text)
        self.assertIn(VERDICT, text)
        self.assertIn("Wrote %s." % self.out, text)
        self.assertIn("/trader-budget", text)
        with open(self.out) as fh:
            doc = json.load(fh)
        self.assertEqual(sorted(k for k in ("plan", "mc", "history", "verdict") if k in doc),
                         ["history", "mc", "plan", "verdict"])
        self.assertEqual(doc["created_by"], "trader-timemachine")
        self.assertEqual(doc["created_at"], "2026-10-29T10:50:00+05:30")
        self.assertEqual(doc["source"], "offline")
        self.assertEqual((doc["mc"]["p_target"], doc["mc"]["avg_R"]), (0.2292, -0.074))
        self.assertEqual(sha(doc["mc"]["sample_paths"]), SIM_PATHS)
        self.assertEqual((doc["history"]["triggered"], doc["history"]["avg_R"]), (27, 0.065))
        self.assertEqual(doc["plan"]["from"], self.plan)

    def test_json_output(self):
        code, text, _ = run(["run", "--plan", self.plan, "--offline", BARS, "--now", NOW, "--json"])
        self.assertEqual(code, 0)
        doc = json.loads(text)
        self.assertEqual(doc["mc"]["p_stop"], 0.2804)
        self.assertNotIn("sample_paths", doc["mc"])
        self.assertEqual(doc["verdict"][:21], "Leans toward the stop")
        self.assertFalse(os.path.exists(self.out))

    def test_the_plan_as_flags_matches_the_file(self):
        code, text, err = run(["run", "--symbol", "reliance", "--date", "2026-10-29", "--entry", "1174.40",
                               "--stop", "1163.00", "--target", "1185.80", "--entry-time", "09:40",
                               "--offline", BARS, "--now", NOW, "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual((doc["mc"]["p_target"], doc["mc"]["p_green"]), (0.2292, 0.4527))
        self.assertEqual((doc["plan"]["stop_rule"], doc["plan"]["tick_size"], doc["plan"]["cas"]),
                         ("or_mid", 0.1, True))
        self.assertEqual((doc["history"]["target"], doc["history"]["stop"], doc["history"]["avg_R"]), (13, 11, 0.065))
        code, _, err = run(["run", "--symbol", "RELIANCE", "--entry", "1174.40", "--offline", BARS])
        self.assertEqual(code, 1)
        self.assertIn("--stop", err)

    def test_sims_is_another_name_for_replays(self):
        code, text, err = run(["run", "--plan", self.plan, "--offline", BARS, "--now", NOW, "--sims", "300", "--json"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(text)["mc"]["replays"], 300)

    def test_offline_folder_and_default_paths(self):
        shutil.copy(BARS, self.d)
        cwd = os.getcwd()
        os.chdir(self.d)
        try:
            code, text, err = run(["run", "--offline", ".", "--now", NOW, "--replays", "500", "--write"])
            self.assertEqual(code, 0, err)
            self.assertTrue(os.path.exists(os.path.join("trader", "timemachine.json")))
            code, text, err = run(["chart", "--from"])
            self.assertEqual(code, 0, err)
            self.assertIn("* median", text)
            with open(os.path.join("trader", "timemachine.json")) as fh:
                self.assertEqual(json.load(fh)["mc"]["replays"], 500)
        finally:
            os.chdir(cwd)

    def test_errors_are_one_plain_line(self):
        for argv in (["run", "--plan", os.path.join(self.d, "nope.json")],
                     ["run", "--plan", self.plan, "--offline", os.path.join(self.d, "missing.json.gz")],
                     ["run", "--plan", self.plan, "--offline", BARS, "--until", "9:00"],
                     ["run", "--plan", self.plan, "--offline", BARS, "--now", "yesterday"],
                     ["chart", "--from", os.path.join(self.d, "nope.json")]):
            code, text, err = run(argv)
            self.assertEqual(code, 1, argv)
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertTrue(err.startswith("timemachine.py: "), err)
            self.assertNotIn("Traceback", err)
        self.assertEqual(run([])[0], 1)

    def test_chart(self):
        run(["run", "--plan", self.plan, "--offline", BARS, "--now", NOW, "--write", self.out])
        code, text, _ = run(["chart", "--from", self.out])
        self.assertEqual(code, 0)
        lines = text.splitlines()
        self.assertIn("2,000 replay paths from the 09:40 entry (seed 19), prices in rupees", lines[0])
        self.assertTrue(any(l.startswith("  1185.80 |") and l.endswith("  target") for l in lines))
        self.assertTrue(any(l.startswith("  1163.00 |") and l.endswith("  stop") for l in lines))
        self.assertTrue(any(l.startswith("  1174.40 |*") and l.endswith("  entry") for l in lines))
        self.assertIn("09:40", text)
        self.assertIn("15:05", text)
        self.assertEqual(lines[-2:], [VERDICT, NOTE])
        code, again, _ = run(["chart", "--plan", self.plan, "--offline", BARS, "--now", NOW])
        self.assertEqual(again, text)


if __name__ == "__main__":
    unittest.main()
