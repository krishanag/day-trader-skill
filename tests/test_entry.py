"""trader-entry tests. Fixtures in tests/fixtures/trader-entry/, cut from real Kite
Connect answers on 2026-10-29 (the wire shape: {"status": "success", "data": {"candles": [...]}}):

HINDUNILVR-2026-10-29-5minute.json  the reel: 72 five-minute bars, 09:15 to 15:10. The 15-minute
                                    opening range is 1804.00 to 1829.50; the 09:55 bar (bar 9,
                                    counted from 1 at 09:15) closed 1832.00, the first close
                                    above, and the stock never looked back (high of the day 1878.60).
HINDUNILVR-2026-10-29-minute.json   the same session in 360 one-minute bars, 09:15 to 15:14.
HINDUNILVR-2026-10-27-5minute.json  a day with no breakout: range 1777.00 to 1812.50, a wick to
                                    1813.40 and no close above. For waiting, expired and closed.
ONGC-2026-10-26-5minute.json        a stock under Rs 250 (tick 0.01): range 224.66 to 226.41,
                                    the 10:05 bar (bar 11) closed 227.60.
instruments-NSE-excerpt.csv         six real rows of Kite's NSE instrument dump (HINDUNILVR's
                                    tick is 0.10, ONGC's 0.01, INFY's 0.05).
instruments-NFO-excerpt.csv         the stock-futures rows that make HINDUNILVR and ONGC CAS stocks.

No network: entry.kite._urlopen (the kite module entry.py imported) is swapped for KiteFake,
which answers from the fixtures by URL, and Kite.connect is swapped so no session file is needed.
"""

import datetime
import io
import json
import os
import re
import shutil
import tempfile
import unittest
import urllib.error
import urllib.parse
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, ROOT

entry = load("trader-entry", "entry")
kite = entry.kite
nse = entry.nse

EDIR = os.path.join(FIX, "trader-entry")
XDIR = os.path.join(FIX, "trader-exit")
TOKENS = {"356865": "HINDUNILVR", "633601": "ONGC", "408065": "INFY", "738561": "RELIANCE",
          "415745": "IOC", "895745": "TATASTEEL"}
HUL = {"symbol": "HINDUNILVR", "name": "HINDUSTAN UNILEVER", "tick_size": 0.1, "cas": True}
ONGC = {"symbol": "ONGC", "name": "OIL AND NATURAL GAS CORP.", "tick_size": 0.01, "cas": True}
DT = datetime.datetime


def ist(text):
    return entry.parse_now(text)


def read(name, folder=EDIR):
    with open(os.path.join(folder, name), encoding="utf-8") as fh:
        return fh.read()


def candles(name, folder=EDIR):
    return json.loads(read(name, folder))["data"]["candles"]


def bars(name="HINDUNILVR-2026-10-29-5minute.json"):
    """The fixture's candles as entry.py's bar dicts (what fetch_bars returns)."""
    return [{"t": kite.parse_time(c[0]), "o": c[1], "h": c[2], "l": c[3], "c": c[4], "v": c[5]}
            for c in candles(name)]


def at(text, name="HINDUNILVR-2026-10-29-5minute.json", facts=HUL, **kw):
    return entry.evaluate(bars(name), ist(text), facts, **kw)


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
    return urllib.error.HTTPError("https://api.kite.trade/x", code, message, {}, body)


class KiteFake:
    """Answers Kite's URLs from the fixtures. `queue` holds exceptions or responses to
    serve first; `lag` is how far Kite's history runs behind the clock in the watch tests."""

    def __init__(self):
        self.requests = []
        self.queue = []
        self.lag = datetime.timedelta(0)

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if self.queue:
            answer = self.queue.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        url = urllib.parse.urlparse(req.full_url)
        q = urllib.parse.parse_qs(url.query)
        if url.path == "/instruments/NSE":
            return FakeResponse(read("instruments-NSE-excerpt.csv"), "text/csv")
        if url.path == "/instruments/NFO":
            return FakeResponse(read("instruments-NFO-excerpt.csv"), "text/csv")
        m = re.match(r"^/instruments/historical/(\d+)/(\w+)$", url.path)
        if not m:
            raise http_error(404, "InputException", "unknown path " + url.path)
        return FakeResponse(json.dumps({"status": "success", "data": {
            "candles": self.history(TOKENS.get(m.group(1), "?"), m.group(2), q["from"][0], q["to"][0])}}))

    def history(self, sym, interval, lo, hi):
        rows = []
        for folder in (EDIR, XDIR):
            for name in sorted(os.listdir(folder)):
                if name.startswith(sym + "-") and name.endswith("-" + interval + ".json"):
                    rows.extend(candles(name, folder))
        lo, hi = lo.replace(" ", "T"), hi.replace(" ", "T")
        return [c for c in rows if lo <= c[0][:19] <= hi]


class Base(unittest.TestCase):
    """Offline, in a scratch folder (the instrument cache and trader/ land there)."""

    def setUp(self):
        self.net = KiteFake()
        self.saved = (kite._urlopen, kite._sleep, dict(kite._last_call), kite.Kite.connect)
        kite._urlopen = self.net
        kite._sleep = lambda s: None
        kite._last_call.clear()
        kite.Kite.connect = classmethod(lambda cls, now=None, session_path=None, env=None: cls("key", "tok"))
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)
        kite._urlopen, kite._sleep, last, kite.Kite.connect = self.saved
        kite._last_call.clear()
        kite._last_call.update(last)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = entry.main(argv)
    return code, out.getvalue(), err.getvalue()


class TheReel(unittest.TestCase):
    def test_triggers_at_1000_on_the_0955_bar(self):
        r = at("2026-10-29 10:00")
        self.assertEqual(r["status"], "triggered")
        self.assertEqual(r["phase"], "open")
        self.assertEqual(r["opening_range"], {"high": 1829.5, "low": 1804.0, "minutes": 15, "bars": 3, "of": 3,
                                              "complete": True, "avg_volume": 50825})
        t = r["trigger"]
        self.assertEqual(t["time"], "10:00")
        self.assertEqual(t["bar_start"], "09:55")
        self.assertEqual(t["bar"], 9)
        self.assertEqual(t["price"], 1832.0)
        self.assertEqual(t["volume"], 21031)
        self.assertEqual(t["volume_x"], 0.41)
        self.assertEqual(t["pct_over"], 0.14)
        self.assertEqual(r["level"], 1829.5)
        self.assertEqual((r["cas"], r["tick_size"], r["closes"]), (True, 0.1, "15:15"))
        self.assertEqual(r["minutes_since"], 0)
        self.assertEqual(r["bars_closed"], 9)
        self.assertEqual(r["forming"]["bar_start"], "10:00")       # Kite's bar in progress never counts

    def test_bars_count_from_one_at_0915_and_the_time_is_the_bar_end(self):
        # the pack's convention: bar N completes at 09:15 + N x 5 minutes, so the 10:10 bar is bar 12 at 10:15
        cfg = {"no_entries_before": "10:15", "no_entries_after": "14:30"}
        r = at("2026-10-29 10:15", cfg=cfg)
        t = r["trigger"]
        self.assertEqual((t["bar_start"], t["bar"], t["time"], t["price"]), ("10:10", 12, "10:15", 1837.7))
        for b in r["_bars"]:
            self.assertEqual(b["end"], nse.at(b["t"].date(), nse.OPEN) + datetime.timedelta(minutes=5 * (b["slot"] + 1)))

    def test_at_0958_the_0955_bar_is_still_forming(self):
        r = at("2026-10-29 09:58")
        self.assertEqual(r["status"], "waiting")
        self.assertIsNone(r["trigger"])
        self.assertEqual(r["last"], {"bar_start": "09:50", "close": 1827.6, "vs_level_pct": -0.1})
        self.assertEqual(r["forming"], {"bar_start": "09:55", "closes": "10:00", "price": 1832.0})
        self.assertEqual(r["reason"], "last close Rs 1,827.60 (09:50 bar), Rs 1.90 under the range high "
                                      "Rs 1,829.50 (-0.10 %)")
        self.assertEqual(at("2026-10-29 09:59:59")["status"], "waiting")
        text = "\n".join(entry.describe(r))
        self.assertIn("WAITING  last close Rs 1,827.60", text)
        self.assertIn("The 09:55 bar is still forming (Rs 1,832.00 so far, closes 10:00)", text)

    def test_before_the_open_and_while_the_range_forms(self):
        r = at("2026-10-29 08:30")
        self.assertEqual((r["status"], r["phase"]), ("closed", "closed"))
        self.assertEqual(r["next_open"], "2026-10-29 09:15")
        r = at("2026-10-29 09:10")
        self.assertEqual((r["status"], r["phase"]), ("pre-open", "pre-open"))
        self.assertIn("range is set at 09:30", r["reason"])
        r = at("2026-10-29 09:22")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "forming"))
        self.assertEqual(r["opening_range"]["bars"], 1)
        self.assertEqual(r["opening_range"]["high"], 1824.3)       # the 09:15 bar alone
        self.assertFalse(r["opening_range"]["complete"])
        r = at("2026-10-29 09:29:59")
        self.assertEqual((r["phase"], r["opening_range"]["bars"]), ("forming", 2))
        r = at("2026-10-29 09:30")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "open"))
        self.assertEqual(r["opening_range"]["bars"], 3)
        self.assertTrue(r["opening_range"]["complete"])
        self.assertIn("Rs 7.40 under the range high", r["reason"])  # the 09:25 bar closed 1822.10

    def test_after_the_close_it_stays_triggered_and_says_it_is_late(self):
        r = at("2026-10-29 15:20")
        self.assertEqual((r["status"], r["phase"]), ("triggered", "after-close"))
        self.assertEqual(r["trigger"]["time"], "10:00")
        self.assertEqual(r["last"]["bar_start"], "15:10")           # a CAS stock's last bar
        text = "\n".join(entry.describe(r))
        self.assertIn("That was 320 minutes ago", text)
        self.assertIn("gone for today", text)
        self.assertNotIn("This is the moment", text)

    def test_the_words_on_a_trigger(self):
        text = "\n".join(entry.describe(at("2026-10-29 10:00")))
        self.assertIn("HINDUNILVR  Thu 2026-10-29 10:00 IST  opening-range breakout (15-minute range) on 5-minute bars (Kite)", text)
        self.assertIn("Opening range 09:15-09:30: high Rs 1,829.50, low Rs 1,804.00 (3 of 3 bars).", text)
        self.assertIn("TRIGGERED 10:00 at Rs 1,832.00: the 09:55 bar (bar 9) closed above the range high Rs 1,829.50 (+0.14 %).", text)
        self.assertIn("This is the moment to get in, if you take the trade. Nothing was placed.", text)
        self.assertIn("/trader-exit", text)
        self.assertIn("Closing Auction Session stock: continuous trading ends 15:15", text)


class Rules(unittest.TestCase):
    def test_volume_confirmation_moves_the_trigger(self):
        r = at("2026-10-29 10:05", min_volume_x=1)
        t = r["trigger"]
        self.assertEqual((t["time"], t["bar"], t["price"], t["volume_x"]), ("10:05", 10, 1841.3, 1.02))
        self.assertIn("1.02x the range's volume", "\n".join(entry.describe(r)))
        r = at("2026-10-29 10:01", min_volume_x=1)
        self.assertEqual(r["status"], "waiting")
        self.assertIn("closed above at Rs 1,832.00 but on 0.41x the range's volume, under your 1.00x", r["reason"])

    def test_level_setup(self):
        r = at("2026-10-29 10:05", setup="level", level=1835)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar"], r["trigger"]["price"]), ("10:05", 10, 1841.3))
        self.assertEqual(r["level"], 1835.0)
        # a level under the open: the first eligible close is the 09:25 bar's, at 09:30 (no_entries_before)
        r = at("2026-10-29 10:05", setup="level", level=1800)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar"]), ("09:30", 3))
        # a close exactly at the level is not above it: the 09:50 bar closed 1827.60
        r = at("2026-10-29 09:55", setup="level", level=1827.6)
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(r["last"]["close"], 1827.6)
        self.assertEqual(at("2026-10-29 10:00", setup="level", level=1827.6)["trigger"]["bar"], 9)
        with self.assertRaises(entry.EntryError):
            at("2026-10-29 10:30", setup="level")

    def test_no_breakout_waits_then_expires_then_closes(self):
        day = "HINDUNILVR-2026-10-27-5minute.json"
        r = at("2026-10-27 10:49", day)
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(r["opening_range"]["high"], 1812.5)
        self.assertEqual(r["opening_range"]["low"], 1777.0)
        self.assertEqual(at("2026-10-27 14:29", day)["status"], "waiting")
        r = at("2026-10-27 14:30", day)
        self.assertEqual((r["status"], r["phase"]), ("expired", "after-cutoff"))
        self.assertIn("by 14:30, the last time for a new entry", r["reason"])
        self.assertIn("No entry on this setup today. Nothing was placed.", "\n".join(entry.describe(r)))
        r = at("2026-10-27 15:15", day)
        self.assertEqual((r["status"], r["phase"]), ("closed", "after-close"))
        self.assertEqual(r["reason"], "continuous trading in HINDUNILVR ended at 15:15 (Closing Auction Session "
                                      "stock) with no close above the range high Rs 1,812.50")
        # the wick to 1813.40 (above the high) never triggered anything
        self.assertTrue(all(b["c"] <= 1812.5 for b in r["_bars"][3:]))
        self.assertGreater(max(b["h"] for b in r["_bars"][3:]), 1812.5)

    def test_a_stock_without_cas_trades_until_1530(self):
        day = "HINDUNILVR-2026-10-27-5minute.json"
        plain = dict(HUL, cas=False)
        r = at("2026-10-27 15:20", day, plain)
        self.assertEqual((r["status"], r["closes"]), ("expired", "15:30"))
        r = at("2026-10-27 15:30", day, plain)
        self.assertEqual((r["status"], r["phase"]), ("closed", "after-close"))
        self.assertIn("ended at 15:30 (no CAS)", r["reason"])
        self.assertIn("trades until 15:30", "\n".join(entry.describe(r)))

    def test_config_cutoffs(self):
        cfg = {"no_entries_before": "09:30", "no_entries_after": "09:55"}
        r = at("2026-10-29 10:00", cfg=cfg)
        self.assertEqual(r["status"], "expired")
        cfg = {"no_entries_before": "10:05", "no_entries_after": "14:30"}
        r = at("2026-10-29 10:10", cfg=cfg)
        self.assertEqual(r["trigger"]["time"], "10:05")             # the 10:00 bar, first to close at or after 10:05
        r = at("2026-10-29 10:01", cfg=cfg)
        self.assertIn("but no entries before 10:05 (config)", r["reason"])

    def test_bad_inputs(self):
        with self.assertRaises(entry.EntryError):
            at("2026-10-29 10:15", or_minutes=7)
        with self.assertRaises(entry.EntryError):
            at("2026-10-29 10:15", interval="15minute")
        with self.assertRaises(entry.EntryError):
            at("2026-10-29 10:15", setup="short")
        with self.assertRaises(entry.EntryError):
            entry.parse_now("tomorrow")
        with self.assertRaises(entry.EntryError):
            entry.parse_hhmm("25:00", "x")
        with self.assertRaises(entry.EntryError):
            at("2026-10-29 10:15", cfg={"no_entries_before": "half past nine", "no_entries_after": "14:30"})
        self.assertEqual(ist("2026-10-29 10:15"), DT(2026, 10, 29, 10, 15, tzinfo=nse.IST))
        self.assertEqual(ist("2026-10-29 10:15 IST"), DT(2026, 10, 29, 10, 15, tzinfo=nse.IST))
        self.assertEqual(ist("2026-10-29T04:45:00Z"), DT(2026, 10, 29, 10, 15, tzinfo=nse.IST))
        self.assertEqual(ist("2026-10-29T10:15:00+05:30").utcoffset(), datetime.timedelta(hours=5, minutes=30))
        self.assertEqual(entry.parse_now("").tzinfo, nse.IST)

    def test_or_minutes_30(self):
        r = at("2026-10-29 10:00", or_minutes=30)
        self.assertEqual(r["opening_range"]["bars"], 6)
        self.assertEqual(r["opening_range"]["high"], 1829.5)
        self.assertEqual(r["trigger"]["time"], "10:00")
        r = at("2026-10-29 09:40", or_minutes=30)
        self.assertEqual((r["phase"], r["reason"]), ("forming", "5 of 6 range bars closed; the range is set at 09:45"))

    def test_empty_and_off_grid_bars(self):
        r = entry.evaluate([], ist("2026-10-29 10:00"), HUL)
        self.assertEqual((r["status"], r["phase"]), ("waiting", "open"))
        self.assertIn("no bars", r["reason"])
        r = entry.evaluate([], ist("2026-10-29 15:20"), HUL)
        self.assertEqual((r["status"], r["phase"]), ("closed", "after-close"))
        odd = bars()
        odd.append(dict(odd[-1], t=odd[-1]["t"] + datetime.timedelta(minutes=2)))        # 15:12, off the grid
        odd.append(dict(odd[0], t=odd[0]["t"] - datetime.timedelta(days=1)))              # yesterday
        r = entry.evaluate(odd, ist("2026-10-29 15:20"), HUL)
        self.assertEqual(r["bars_closed"], 72)


class OneMinute(unittest.TestCase):
    NAME = "HINDUNILVR-2026-10-29-minute.json"

    def test_fifteen_bars_make_the_range_and_the_0958_bar_triggers(self):
        r = at("2026-10-29 10:00", self.NAME, interval="minute")
        orr = r["opening_range"]
        self.assertEqual((orr["high"], orr["low"], orr["bars"], orr["of"]), (1829.5, 1804.0, 15, 15))
        self.assertEqual(orr["avg_volume"], 10165)
        t = r["trigger"]
        self.assertEqual((t["time"], t["bar_start"], t["bar"], t["price"]), ("09:59", "09:58", 44, 1834.7))
        self.assertEqual(t["pct_over"], 0.28)
        r = at("2026-10-29 09:58:59", self.NAME, interval="minute")
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(r["forming"]["bar_start"], "09:58")
        self.assertEqual(r["last"]["bar_start"], "09:57")
        self.assertEqual(r["bars_closed"], 43)

    def test_the_minute_session_ends_at_1514_for_a_cas_stock(self):
        r = at("2026-10-29 15:20", self.NAME, interval="minute")
        self.assertEqual(r["bars_closed"], 360)
        self.assertEqual(r["last"]["bar_start"], "15:14")
        self.assertEqual(r["trigger"]["bar"], 44)
        self.assertIn("on 1-minute bars", entry.describe(r)[0])

    def test_or_minutes_must_fit_the_interval(self):
        self.assertEqual(at("2026-10-29 10:00", self.NAME, interval="minute", or_minutes=7)["opening_range"]["bars"], 7)
        with self.assertRaises(entry.EntryError):
            at("2026-10-29 10:00", interval="5minute", or_minutes=7)


class Calendar(unittest.TestCase):
    def test_weekend_and_holiday_are_closed_with_the_next_open(self):
        r = at("2026-10-31 10:30")
        self.assertEqual((r["status"], r["phase"]), ("closed", "closed"))
        self.assertEqual(r["reason"], "NSE is closed today (weekend)")
        self.assertEqual(r["next_open"], "2026-11-02 09:15")
        self.assertIn("Next open: 2026-11-02 09:15 IST.", "\n".join(entry.describe(r)))
        r = at("2026-11-10 10:30")
        self.assertEqual(r["status"], "closed")
        self.assertIn("Diwali Balipratipada", r["reason"])
        self.assertEqual(r["next_open"], "2026-11-11 09:15")

    def test_a_date_past_the_known_calendar_is_flagged(self):
        r = at("2027-01-04 10:00")
        self.assertEqual(r["status"], "waiting")
        self.assertIn("2027", r["calendar_note"])
        self.assertIn("2027", "\n".join(entry.describe(r)))
        self.assertEqual(at("2026-10-29 10:00")["calendar_note"], "")

    def test_bars_from_another_day_do_not_count(self):
        r = at("2026-10-28 10:00")                                 # the 10-29 bars on the 28th
        self.assertEqual((r["status"], r["bars_closed"]), ("waiting", 0))
        self.assertIn("no bars", r["reason"])


class Network(Base):
    """Kite is never reached: the fake answers from the fixtures and records the URLs."""

    def test_check_asks_for_the_instruments_the_cas_list_and_the_day(self):
        code, text, err = run(["check", "hindunilvr.ns", "--now", "2026-10-29 10:00"])
        self.assertEqual(code, 0, err)
        paths = [urllib.parse.urlparse(r.full_url).path for r in self.net.requests]
        self.assertEqual(paths, ["/instruments/NSE", "/instruments/NFO", "/instruments/historical/356865/5minute"])
        url = self.net.requests[-1].full_url
        self.assertIn("from=2026-10-29+09%3A15%3A00", url)
        self.assertIn("to=2026-10-29+15%3A30%3A00", url)
        self.assertEqual(self.net.requests[0].get_header("Authorization"), "token key:tok")
        self.assertIn("TRIGGERED 10:00 at Rs 1,832.00", text)

    def test_instruments_and_cas_are_cached_for_the_day(self):
        run(["check", "HINDUNILVR", "--now", "2026-10-29 10:00"])
        run(["check", "ONGC", "--now", "2026-10-26 10:10"])
        paths = [urllib.parse.urlparse(r.full_url).path for r in self.net.requests]
        self.assertEqual(paths.count("/instruments/NSE"), 1)
        self.assertEqual(paths.count("/instruments/NFO"), 1)
        self.assertTrue(os.path.isdir(os.path.join("trader", "cache")))

    def test_no_bars_are_asked_for_on_a_closed_day_or_before_the_open(self):
        code, text, _ = run(["check", "HINDUNILVR", "--now", "2026-10-31 10:00"])
        self.assertEqual(code, 1)
        self.assertIn("MARKET CLOSED", text)
        run(["check", "HINDUNILVR", "--now", "2026-10-29 09:10"])
        paths = [urllib.parse.urlparse(r.full_url).path for r in self.net.requests]
        self.assertFalse(any(p.startswith("/instruments/historical") for p in paths))

    def test_no_session_is_one_plain_line(self):
        kite.Kite.connect = self.saved[3]
        code, text, err = run(["check", "HINDUNILVR", "--now", "2026-10-29 10:00"])
        self.assertEqual(code, 1)
        self.assertIn("KITE_API_KEY is not set", err)                   # a bare folder: no .env at all
        os.environ["KITE_API_KEY"] = "key"
        try:
            code, text, err = run(["check", "HINDUNILVR", "--now", "2026-10-29 10:00"])
        finally:
            del os.environ["KITE_API_KEY"]
        self.assertEqual(code, 1)
        self.assertIn("not logged in to Kite", err)
        self.assertIn("login.py", err)
        self.assertNotIn("Traceback", err)

    def test_a_rejected_token_asks_for_login(self):
        self.net.queue.append(http_error(403, "TokenException", "Incorrect api_key or token"))
        code, _, err = run(["check", "HINDUNILVR", "--now", "2026-10-29 10:00"])
        self.assertEqual(code, 1)
        self.assertIn("Kite rejected the session", err)
        self.assertIn("login.py", err)

    def test_429_is_retried_then_succeeds(self):
        self.net.queue.append(http_error(429, "NetworkException", "Too many requests"))
        code, text, _ = run(["check", "HINDUNILVR", "--now", "2026-10-29 10:00"])
        self.assertEqual(code, 0)
        self.assertEqual(len(self.net.requests), 4)

    def test_unknown_symbol_and_network_down(self):
        code, _, err = run(["check", "ZZZZ", "--now", "2026-10-29 10:00"])
        self.assertEqual(code, 1)
        self.assertIn("ZZZZ is not listed on NSE", err)
        self.net.queue.append(urllib.error.URLError("no route"))
        code, _, err = run(["check", "ONGC", "--now", "2026-10-26 10:10"])
        self.assertEqual(code, 1)
        self.assertIn("cannot reach Kite", err)


def live_provider(net, name="HINDUNILVR-2026-10-29-5minute.json", calls=None, spike=1830.0):
    """Bars the way Kite's history looks at `now`: closed bars final, the bar in progress
    showing a spike to `spike` (above the range high, then it closes under)."""
    base = bars(name)

    def provider(now):
        if calls is not None:
            calls.append(now)
        out = []
        for b in base:
            if b["t"] > now - net.lag:                                       # Kite has not started it yet
                continue
            if b["t"] + datetime.timedelta(minutes=5) > now - net.lag:      # Kite has not finalised it yet
                b = dict(b, c=spike, h=max(b["h"], spike))
            out.append(b)
        return out
    return provider


class Watch(unittest.TestCase):
    OPTS = {"setup": "orb", "or_minutes": 15, "interval": "5minute", "level": None, "min_volume_x": None, "cfg": None}

    def setUp(self):
        self.net = KiteFake()

    def loop(self, provider, start, facts=HUL, **kw):
        clock = entry.SimClock(ist(start))
        lines, fired = [], []
        code, res = entry.watch_loop(provider, clock.now, clock.sleep, lines.append, facts, dict(self.OPTS),
                                     on_trigger=lambda r, now: fired.append((r, now)), **kw)
        return code, res, lines, fired, clock

    def test_fires_on_the_first_closed_bar_not_the_spike_in_progress(self):
        calls = []
        code, res, lines, fired, clock = self.loop(live_provider(self.net, calls=calls), "2026-10-29 09:52", every=60)
        self.assertEqual(code, 0)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0][0]["trigger"]["time"], "10:00")
        self.assertEqual(fired[0][0]["trigger"]["price"], 1832.0)
        # it looked at 09:52 (09:50 bar spiking to 1830 in progress), 09:55:05 (that bar closed 1827.60), 10:00:05
        self.assertEqual([c.strftime("%H:%M:%S") for c in calls], ["09:52:00", "09:55:05", "10:00:05"])
        text = "\n".join(lines)
        self.assertIn("09:50 bar  closed Rs 1,827.60  Rs 1.90 under Rs 1,829.50", text)
        self.assertIn("09:55 bar  closed Rs 1,832.00  TRIGGERED: closed above Rs 1,829.50 (+0.14 %). "
                      "Get in now if you take it: 10:00, Rs 1,832.00", text)
        self.assertEqual(text.count("TRIGGERED"), 1)
        self.assertIn("Nothing was placed", text)

    def test_waits_for_kite_to_post_the_bar(self):
        self.net.lag = datetime.timedelta(seconds=20)
        calls = []
        code, res, lines, fired, clock = self.loop(live_provider(self.net, calls=calls), "2026-10-29 09:57", every=30)
        self.assertEqual(code, 0)
        times = [c.strftime("%H:%M:%S") for c in calls]
        self.assertEqual(times, ["09:57:00", "10:00:05", "10:00:35"])   # 10:00:05: Kite still at 09:59:45
        self.assertEqual(fired[0][0]["trigger"]["time"], "10:00")
        self.assertEqual("\n".join(lines).count("TRIGGERED"), 1)       # the spike at 10:00:05 was not one

    def test_a_bar_just_closed_by_the_clock_is_pending_until_kite_moves_on(self):
        # at 10:00:05 the history ends with the 09:55 bar: closed by the clock, but Kite may still be filling it in
        upto = [b for b in bars() if b["t"] <= ist("2026-10-29 09:55")]
        r = entry.evaluate(upto, ist("2026-10-29 10:00:05"), HUL)
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(r["bars_closed"], 8)
        self.assertEqual(r["forming"]["bar_start"], "09:55")
        self.assertIn("Kite has not posted the 10:00 bar yet", r["lag_note"])
        self.assertIn("Kite has not posted the 10:00 bar yet", "\n".join(entry.describe(r)))
        # a minute later it counts even with no later bar; and with the 10:00 bar present it counts at once
        self.assertEqual(entry.evaluate(upto, ist("2026-10-29 10:01"), HUL)["trigger"]["time"], "10:00")
        self.assertEqual(at("2026-10-29 10:00:05")["trigger"]["time"], "10:00")
        # the bar closing at the cutoff still counts when Kite lags
        cfg = {"no_entries_before": "09:30", "no_entries_after": "10:00"}
        self.assertEqual(entry.evaluate(upto, ist("2026-10-29 10:00:05"), HUL, cfg=cfg)["status"], "waiting")
        self.assertEqual(entry.evaluate(upto, ist("2026-10-29 10:01"), HUL, cfg=cfg)["status"], "triggered")

    def test_from_before_the_open_prints_the_range_bar_by_bar(self):
        code, res, lines, fired, clock = self.loop(live_provider(self.net), "2026-10-29 09:00", every=30)
        self.assertEqual(code, 0)
        text = "\n".join(lines)
        self.assertIn("not open yet", lines[0])
        self.assertIn("09:15 bar  closed Rs 1,807.60  range so far Rs 1,804.00 to Rs 1,824.30", text)
        self.assertIn("09:25 bar  closed Rs 1,822.10  opening range set: high Rs 1,829.50, low Rs 1,804.00", text)
        self.assertEqual(sum(1 for x in lines if " bar  closed " in x), 9)    # 09:15 ... 09:55, one line each

    def test_a_computer_that_dozes_off_catches_up_when_it_wakes(self):
        class Dozing(entry.SimClock):
            naps = 0

            def sleep(self, seconds):
                entry.SimClock.sleep(self, seconds)
                self.naps += 1
                if self.naps == 3:                      # the laptop sleeps for 17 minutes mid-wait
                    self.t += datetime.timedelta(minutes=17)
        clock = Dozing(ist("2026-10-29 09:31"))
        calls, lines = [], []
        code, res = entry.watch_loop(live_provider(self.net, calls=calls), clock.now, clock.sleep, lines.append,
                                     HUL, dict(self.OPTS), every=30)
        self.assertEqual(code, 0)
        times = [c.strftime("%H:%M:%S") for c in calls]
        self.assertEqual(times[:2], ["09:31:00", "09:49:30"])   # straight after waking, not a long sleep later
        text = "\n".join(lines)
        for start in ("09:30", "09:35", "09:40", "09:45"):
            self.assertIn(start + " bar  closed", text)          # the bars it slept through, one line each
        self.assertEqual(res["trigger"]["time"], "10:00")

    def test_expires_closes_and_until(self):
        quiet = "HINDUNILVR-2026-10-27-5minute.json"
        code, res, lines, fired, clock = self.loop(live_provider(self.net, quiet), "2026-10-27 14:00", every=300)
        self.assertEqual(code, 1)
        self.assertEqual(res["status"], "expired")
        self.assertEqual(fired, [])
        self.assertEqual(clock.now().strftime("%H:%M"), "14:30")
        self.assertTrue(lines[-1].startswith("EXPIRED  no close above the range high Rs 1,812.50 by 14:30"))
        until = ist("2026-10-27 11:00")
        code, res, lines, fired, clock = self.loop(live_provider(self.net, quiet), "2026-10-27 10:30", every=300,
                                                   until=until)
        self.assertEqual(code, 1)
        self.assertIn("--until 11:00", lines[-1])
        # a late cutoff: the close ends the watch for a CAS stock at 15:15
        cfg = {"no_entries_before": "09:30", "no_entries_after": "15:30"}
        opts = dict(self.OPTS, cfg=cfg)
        clock = entry.SimClock(ist("2026-10-27 15:00"))
        lines = []
        code, res = entry.watch_loop(live_provider(self.net, quiet), clock.now, clock.sleep, lines.append, HUL, opts)
        self.assertEqual((code, res["status"]), (1, "closed"))
        self.assertTrue(lines[-1].startswith("CLOSED  continuous trading in HINDUNILVR ended at 15:15"))

    def test_closed_day_and_repeated_errors(self):
        code, _, lines, _, _ = self.loop(live_provider(self.net), "2026-10-31 10:00")
        self.assertEqual(code, 1)
        self.assertIn("weekend", lines[0])

        def broken(now):
            raise entry.EntryError("cannot reach Kite (no route)")
        code, _, lines, _, _ = self.loop(broken, "2026-10-29 10:00", every=30)
        self.assertEqual(code, 1)
        self.assertIn("Stopping after 5 failed reads", lines[-1])
        self.assertIn("could not read bars: cannot reach Kite", lines[0])


class Cli(Base):
    def test_check_write_is_the_contract_shape(self):
        code, text, _ = run(["check", "HINDUNILVR", "--now", "2026-10-29 10:00", "--write"])
        self.assertEqual(code, 0)
        self.assertIn("TRIGGERED 10:00 at Rs 1,832.00", text)
        self.assertIn("Wrote trader/entry.json (status triggered).", text)
        with open(os.path.join("trader", "entry.json")) as fh:
            e = json.load(fh)
        want = {"created_by": "trader-entry", "symbol": "HINDUNILVR", "setup": "orb", "side": "buy",
                "status": "triggered", "date": "2026-10-29", "interval": "5minute", "time": "10:00", "bar": 9,
                "bar_start": "09:55", "price": 1832.0, "level": 1829.5,
                "opening_range": {"high": 1829.5, "low": 1804.0, "minutes": 15}, "cas": True, "tick_size": 0.1,
                "source": "kite", "checked_at": "2026-10-29T10:00:00+05:30"}
        for k, v in want.items():
            self.assertEqual(e[k], v, k)

    def test_check_waiting_json_and_exit_codes(self):
        code, text, _ = run(["check", "HINDUNILVR", "--now", "2026-10-29 09:58", "--json"])
        self.assertEqual(code, 1)
        r = json.loads(text)
        self.assertEqual(r["status"], "waiting")
        self.assertNotIn("_bars", r)
        self.assertEqual(r["last"]["close"], 1827.6)
        code, text, _ = run(["check", "HINDUNILVR", "--now", "2026-10-29 09:58", "--write", "e.json"])
        with open("e.json") as fh:
            e = json.load(fh)
        self.assertEqual((e["status"], e["time"], e["price"], e["cas"]), ("waiting", None, None, True))
        self.assertIn("still forming", text)
        code, _, _ = run(["check", "HINDUNILVR", "--now", "2026-10-29 09:10"])
        self.assertEqual(code, 1)

    def test_the_under_250_stock_has_a_one_paisa_tick(self):
        code, text, _ = run(["check", "ONGC", "--now", "2026-10-26 10:10", "--json"])
        self.assertEqual(code, 0)
        r = json.loads(text)
        self.assertEqual((r["tick_size"], r["cas"]), (0.01, True))
        self.assertEqual(r["opening_range"]["high"], 226.41)
        self.assertEqual(r["opening_range"]["low"], 224.66)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar"], r["trigger"]["price"]), ("10:10", 11, 227.6))
        self.assertEqual(r["trigger"]["pct_over"], 0.53)

    def test_longs_only_and_flag_errors(self):
        code, _, err = run(["check", "HINDUNILVR", "--side", "short"])
        self.assertEqual(code, 1)
        self.assertIn("longs only", err)
        code, _, err = run(["check", "HINDUNILVR", "--level", "300"])
        self.assertIn("--level is for --setup level", err)
        code, _, err = run(["check", "HINDUNILVR", "--setup", "level"])
        self.assertIn("needs --level", err)
        code, _, err = run(["check", "HINDUNILVR", "--min-volume-x", "0"])
        self.assertIn("--min-volume-x must be above 0", err)
        code, _, err = run(["check", "HINDUNILVR", "--or-minutes", "7"])
        self.assertIn("whole number of 5minute bars", err)
        code, _, err = run(["check", "HINDUNILVR", "--now", "yesterday"])
        self.assertIn("--now wants an IST time", err)
        code, _, err = run(["watch", "HINDUNILVR", "--every", "0"])
        self.assertIn("--every must be above 0", err)
        self.assertEqual(self.net.requests, [])                       # refused before any call
        self.assertEqual(run([])[0], 1)

    def test_volume_alias(self):
        code, text, _ = run(["check", "HINDUNILVR", "--now", "2026-10-29 10:05", "--volume", "1"])
        self.assertEqual(code, 0)
        self.assertIn("the 10:00 bar (bar 10)", text)

    def test_config_is_read(self):
        os.mkdir("trader")
        with open(os.path.join("trader", "config.json"), "w") as fh:
            json.dump({"no_entries_after": "09:55", "budget_inr": 100000}, fh)
        code, text, _ = run(["check", "HINDUNILVR", "--now", "2026-10-29 10:00"])
        self.assertEqual(code, 1)
        self.assertIn("EXPIRED", text)
        with open(os.path.join("trader", "config.json"), "w") as fh:
            fh.write("{nope")
        code, _, err = run(["check", "HINDUNILVR", "--now", "2026-10-29 10:00"])
        self.assertIn("not valid JSON", err)
        with open(os.path.join("trader", "config.json"), "w") as fh:
            json.dump({"no_entries_after": "half past two"}, fh)
        code, _, err = run(["check", "HINDUNILVR", "--now", "2026-10-29 10:00"])
        self.assertIn("no_entries_after wants an IST time", err)

    def test_watch_replays_kite_history_and_writes_the_entry(self):
        code, text, _ = run(["watch", "HINDUNILVR", "--now", "2026-10-29 09:40", "--every", "30"])
        self.assertEqual(code, 0)
        self.assertIn("09:55 bar  closed Rs 1,832.00  TRIGGERED", text)
        self.assertIn("Wrote trader/entry.json.", text)
        with open(os.path.join("trader", "entry.json")) as fh:
            e = json.load(fh)
        self.assertEqual((e["bar"], e["time"], e["price"], e["tick_size"]), (9, "10:00", 1832.0, 0.1))
        code, text, _ = run(["watch", "HINDUNILVR", "--now", "2026-10-29 09:40", "--until", "09:50", "--out", "x.json"])
        self.assertEqual(code, 1)
        self.assertIn("Stopped at --until 09:50", text)
        self.assertFalse(os.path.exists("x.json"))

    def test_watch_on_a_closed_day_and_with_no_session(self):
        code, text, _ = run(["watch", "HINDUNILVR", "--now", "2026-10-31 10:00"])
        self.assertEqual(code, 1)
        self.assertIn("NSE is closed today (weekend)", text)
        kite.Kite.connect = self.saved[3]
        os.environ["KITE_API_KEY"] = "key"
        try:
            code, _, err = run(["watch", "HINDUNILVR", "--now", "2026-10-29 09:40"])
        finally:
            del os.environ["KITE_API_KEY"]
        self.assertEqual(code, 1)
        self.assertIn("not logged in", err)

    def test_watchlist(self):
        os.mkdir("trader")
        with open(os.path.join("trader", "watchlist.json"), "w") as fh:
            json.dump({"created_by": "trader-scanner", "mode": "orb", "hits": [
                {"symbol": "HINDUNILVR", "setup": "orb", "cas": True, "tick_size": 0.1,
                 "opening_range": {"high": 1829.5, "low": 1804.0, "minutes": 15}},
                {"symbol": "ZZZZ", "setup": "orb"}, {"symbol": "ONGC", "setup": "orb"}]}, fh)
        code, text, _ = run(["watchlist", "--now", "2026-10-29 10:00"])
        self.assertEqual(code, 0)
        rows = text.splitlines()
        self.assertTrue(rows[0].startswith("SYMBOL"))
        self.assertTrue(rows[1].startswith("HINDUNILVR    triggered  10:00   Rs 1,832.00   Rs 1,829.50"), rows[1])
        self.assertIn("ZZZZ          error: ZZZZ is not listed on NSE", rows[2])
        self.assertTrue(rows[3].startswith("ONGC          waiting"), rows[3])       # no ONGC bars for the 29th
        self.assertIn("1 of 3 triggered", text)
        self.assertIn("Nothing was placed", text)
        code, text, _ = run(["watchlist", "--now", "2026-10-29 09:58", "--json"])
        self.assertEqual(code, 1)
        doc = json.loads(text)
        self.assertEqual(doc[0]["result"]["status"], "waiting")
        self.assertIsNone(doc[1]["result"])
        self.assertIn("not listed", doc[1]["error"])
        code, _, err = run(["watchlist", "--watchlist", "missing.json"])
        self.assertIn("run /trader-scanner first", err)
        with open(os.path.join("trader", "watchlist.json"), "w") as fh:
            json.dump({"hits": []}, fh)
        code, _, err = run(["watchlist"])
        self.assertIn("no hits", err)

    def test_watchlist_level_setup_takes_the_hit_level(self):
        os.mkdir("trader")
        with open(os.path.join("trader", "watchlist.json"), "w") as fh:
            json.dump({"hits": [{"symbol": "HINDUNILVR", "level": 1835}, {"symbol": "ONGC"}]}, fh)
        code, text, _ = run(["watchlist", "--setup", "level", "--now", "2026-10-29 10:05"])
        self.assertEqual(code, 0)
        rows = text.splitlines()
        self.assertIn("triggered  10:05   Rs 1,841.30   Rs 1,835.00", rows[1])
        self.assertIn("ONGC          error: needs a level", rows[2])


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-entry", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\nname: trader-entry\n"))
        fm = re.match(r"^---\n(.*?)\n---\n", text, re.S).group(1)
        desc = " ".join(fm[fm.index("description:"):fm.index("argument-hint:")].split())
        self.assertGreater(len(desc), 200)
        self.assertIn("Entry Timer.", desc)
        self.assertIn("Use when the user says", desc)
        self.assertIn("argument-hint:", fm)
        self.assertGreater(len(text.splitlines()), 60)
        for must in ('"${CLAUDE_SKILL_DIR}/entry.py"', "## What it reads and writes", "## How Claude runs it",
                     "## Fine print"):
            self.assertIn(must, text)
        low = " ".join(text.lower().split()).replace("${claude_skill_dir}", "")
        for must in ("bar by bar", "day trading loses most people money", "/trader-exit", "longs only",
                     "in the background", "/loop", "nothing was placed", "not financial advice", "says yes",
                     "10:00", "rs 1,832.00", "bar 9", "1,829.50", "15:15", "15:12", "kite", "ist"):
            self.assertIn(must, low)
        for never in ("\u2014", "yahoo", "nyse", "alpaca", "$", " et ", "new york", "tick " + "by tick",
                      "real " + "time", "pro" + "fit"):
            self.assertNotIn(never, low, never)


if __name__ == "__main__":
    unittest.main()
