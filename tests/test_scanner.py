import datetime
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
import urllib.parse
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

scan = load("trader-scanner", "scan")
kite = scan.kite   # the module scan.py imported, so the network fake reaches it
nse = scan.nse

SDIR = os.path.join(FIX, "trader-scanner")
BARS5 = os.path.join(SDIR, "bars5m-2026-10-29.json.gz")     # 14 real 5-minute charts, Thu Oct 29 2026
DAILY = os.path.join(SDIR, "daily-2026-10-29.json")         # 7 real daily charts, Aug 3 to Oct 29 2026
QUOTES = os.path.join(SDIR, "quotes-2026-10-29.json")       # one real quote call, 33 symbols, 17:27 IST Oct 29
NSE_CSV = os.path.join(SDIR, "instruments-NSE-excerpt.csv")  # 44 rows cut from Kite's Oct 29 NSE dump
NFO_CSV = os.path.join(SDIR, "instruments-NFO-excerpt.csv")  # 11 rows cut from the NFO dump (the CAS names)
DT = datetime.datetime
D = datetime.date
T = datetime.time
IST = nse.IST
UNTIL = T(14, 30)
CAS = {"RELIANCE", "ADANIENT", "ICICIBANK", "TCS", "BHARTIARTL", "LT", "BAJAJ-AUTO", "INFY", "YESBANK"}


def ist(text):
    return DT.fromisoformat(text).replace(tzinfo=IST)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = scan.main(argv)
    return code, out.getvalue(), err.getvalue()


def read_text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def load_bars5():
    with gzip.open(BARS5, "rt", encoding="utf-8") as fh:
        return {s: kite.parse_candles(d) for s, d in json.load(fh).items()}


def load_daily(through=None):
    """The daily charts, cut to the 60 sessions the tool fetches when `through` is given."""
    data = {s: kite.parse_candles(d) for s, d in json.loads(read_text(DAILY)).items()}
    if through is None:
        return data
    lo = scan.daily_range(through)[0].date()
    return {s: [c for c in cs if lo <= c[0].date() <= through] for s, cs in data.items()}


def load_quotes():
    return {k.split(":", 1)[1]: v for k, v in json.loads(read_text(QUOTES)).items()}


class FakeResponse(io.BytesIO):
    def __init__(self, body, ctype="application/json"):
        super().__init__(body.encode())
        self.headers = {"Content-Type": ctype}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def ok(data):
    return FakeResponse(json.dumps({"status": "success", "data": data}))


def http_error(code, etype, message):
    body = io.BytesIO(json.dumps({"status": "error", "error_type": etype, "message": message}).encode())
    return urllib.error.HTTPError("https://api.kite.trade/x", code, message, {}, body)


class FakeKite:
    """Answers Kite's URLs from the fixtures: the instrument dumps, quotes and candles."""

    def __init__(self):
        self.requests = []
        self.broken = {}          # symbol -> HTTPError for its candles
        self.nse_csv = read_text(NSE_CSV)
        self.nfo_csv = read_text(NFO_CSV)
        self.quotes = json.loads(read_text(QUOTES))
        with gzip.open(BARS5, "rt", encoding="utf-8") as fh:
            self.five = json.load(fh)
        self.daily = json.loads(read_text(DAILY))
        self.tokens = {str(row["instrument_token"]): sym
                       for sym, row in kite.parse_instruments(self.nse_csv).items()}

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.requests.append(url)
        parts = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parts.query)
        if parts.path == "/instruments/NSE":
            return FakeResponse(self.nse_csv, "text/csv")
        if parts.path == "/instruments/NFO":
            return FakeResponse(self.nfo_csv, "text/csv")
        if parts.path == "/quote":
            keys = qs.get("i", [])
            return ok({k: self.quotes[k] for k in keys if k in self.quotes})
        if parts.path.startswith("/instruments/historical/"):
            _, _, _, token, interval = parts.path.split("/")
            sym = self.tokens[token]
            if sym in self.broken:
                raise self.broken[sym]
            doc = (self.five if interval == "5minute" else self.daily).get(sym) or {"candles": []}
            lo, hi = qs["from"][0], qs["to"][0]
            rows = [c for c in doc["candles"] if lo <= c[0][:10] + " " + c[0][11:19] <= hi]
            return ok({"candles": rows})
        raise AssertionError("unexpected Kite call " + url)

    def calls(self, path):
        return [u for u in self.requests if urllib.parse.urlparse(u).path.startswith(path)]


class Base(unittest.TestCase):
    """A temp project folder with a fresh session, the network swapped for the fixtures."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        self.t = os.path.join(self.d, "trader")
        self.wl = os.path.join(self.t, "watchlist.json")
        self.net = FakeKite()
        self.saved = (kite._urlopen, kite._sleep, dict(kite._last_call), scan.real_today,
                      os.environ.get("KITE_API_KEY"))
        kite._urlopen = self.net
        kite._sleep = lambda s: None
        kite._last_call.clear()
        scan.real_today = lambda: D(2026, 10, 29)
        os.environ["KITE_API_KEY"] = "key"
        kite.save_session({"access_token": "t0k", "user_id": "AB1234"}, nse.now_ist())

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)
        kite._urlopen, kite._sleep, last, scan.real_today, key = self.saved
        kite._last_call.clear()
        kite._last_call.update(last)
        if key is None:
            os.environ.pop("KITE_API_KEY", None)
        else:
            os.environ["KITE_API_KEY"] = key

    def read(self, path):
        return json.loads(read_text(path))


class Universe(Base):
    def setUp(self):
        super().setUp()
        self.table = kite.parse_instruments(self.net.nse_csv)
        self.quotes = load_quotes()

    def test_kite_drops_the_index_rows(self):
        self.assertEqual(len(self.table), 42)              # 44 rows, NIFTY 50 and NIFTY BANK out
        self.assertNotIn("NIFTY 50", self.table)
        self.assertEqual(self.table["BAJAJ-AUTO"]["tick_size"], 1.0)

    def test_one_case_per_rule(self):
        cases = {
            "GATECHDVR-BE": "other series", "SANWARIA-BZ": "other series", "OMFURN-SM": "other series",
            "GOLDSTAR-ST": "other series", "656KA30-SG": "other series", "965SCL25C-N0": "other series",
            "NIFTYBEES": "fund unit", "GOLDBEINAV": "fund unit", "GOLDADD": "fund unit",
            "BAJAJ-AUTO": "keep", "RELIANCE": "keep", "IITL": "keep",
        }
        for sym, want in cases.items():
            self.assertEqual(scan.classify(sym, self.table[sym]), want, sym)
        self.assertEqual(scan.classify("M&M", {"name": "MAHINDRA & MAHINDRA"}), "keep")
        self.assertEqual(scan.classify("HDFCAMC", {"name": "HDFC AMC"}), "keep")   # the company, not a scheme

    def test_liquidity_from_the_real_quotes(self):
        self.assertEqual(scan.liquidity(self.quotes["RELIANCE"]), (1167.4, 1167.4 * 20939623))
        self.assertEqual(scan.liquidity({"last_price": 0, "volume": 0}), (None, None))
        kept, counts = scan.build_universe(self.table, self.quotes, 50, 5e7)
        self.assertEqual([s for s, _ in kept],
                         ["ADANIENT", "AXISBANK", "BAJAJ-AUTO", "BAJAJHFL", "BEL", "BHARTIARTL", "HDFCBANK",
                          "HUDCO", "ICICIBANK", "INFY", "IRCTC", "ITC", "LT", "NTPCGREEN", "RAILTEL", "RELIANCE",
                          "SBIN", "SJVN", "SUNPHARMA", "TATASTEEL", "TCS", "ZEEL"])
        self.assertEqual(dict(kept)["TCS"], "TATA CONSULTANCY SERV LT")
        self.assertEqual(counts, {"rows": 42, "keep": 22, "fund unit": 3, "under Rs 50.00": 7,
                                  "under Rs 5 crore traded": 4, "other series": 6})
        # IDEA at Rs 12.12 is the most traded name in the file (Rs 290 crore) and still out
        kept, counts = scan.build_universe(self.table, self.quotes, 0, 5e7)
        self.assertIn("IDEA", dict(kept))
        self.assertEqual(counts["under Rs 5 crore traded"], 5)   # IITL, ITI, MASTERTR, SILVERTUC, UCOBANK (4.93)
        kept, _ = scan.build_universe(self.table, {}, 50, 5e7)
        self.assertEqual(kept, [])

    def test_universe_end_to_end_with_a_daily_quote_cache(self):
        uni = os.path.join(self.t, "universe.txt")
        code, text, err = run(["universe", "--now", "2026-10-29 17:30", "--out", uni])
        self.assertEqual(code, 0, err)
        self.assertIn("42 equity rows, 33 in a plain series, 33 quoted", text)
        self.assertIn("Kept 22 NSE stocks at Rs 50.00+ with Rs 5 crore+ traded.", text)
        self.assertIn("Dropped: 7 under Rs 50.00, 6 other series, 4 under Rs 5 crore traded, 3 fund unit.", text)
        lines = read_text(uni).splitlines()
        self.assertEqual(len(lines), 22)
        self.assertIn("BAJAJ-AUTO|BAJAJ AUTO", lines)
        self.assertIn("RELIANCE|RELIANCE INDUSTRIES", lines)
        self.assertEqual(len(self.net.calls("/instruments/NSE")), 1)
        quote_calls = self.net.calls("/quote")
        self.assertEqual(len(quote_calls), 1)
        self.assertEqual(quote_calls[0].count("i=NSE"), 33)
        self.assertTrue(os.path.isfile(os.path.join(self.t, "cache", "quotes-2026-10-29.json")))
        # the same day again: the instrument list and the quotes come from the cache
        code, text, _ = run(["universe", "--now", "2026-10-29 17:40", "--out", uni, "--min-price", "0",
                             "--min-value", "0", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)["count"], 33)
        self.assertEqual(len(self.net.calls("/quote")), 1)
        self.assertEqual(len(self.net.calls("/instruments/NSE")), 1)

    def test_no_session_prints_the_login_line(self):
        os.remove(os.path.join(self.t, "kite_session.json"))
        code, _, err = run(["universe", "--now", "2026-10-29 17:30"])
        self.assertEqual(code, 1)
        self.assertIn("login.py", err)
        self.assertNotIn("Traceback", err)
        kite.save_session({"access_token": "t0k"}, ist("2026-10-28T09:00"))
        code, _, err = run(["universe", "--now", "2026-10-29 17:30"])
        self.assertEqual(code, 1)
        self.assertIn("expired at 06:00 IST", err)


class Time(unittest.TestCase):
    def test_clock(self):
        self.assertEqual(scan.parse_now("2026-10-29 10:15"), ist("2026-10-29T10:15"))
        self.assertEqual(scan.parse_now("2026-10-29T04:45:00Z"), ist("2026-10-29T10:15"))
        self.assertEqual(scan.parse_now("2026-10-29T10:15:00+05:30"), ist("2026-10-29T10:15"))
        self.assertEqual(scan.clock_label(ist("2026-10-29T10:15")), "10:15")
        self.assertEqual(scan.clock_label(T(9, 5)), "09:05")
        self.assertEqual(scan.day_label(D(2026, 10, 29)), "Thu 2026-10-29")
        self.assertEqual(scan.parse_hhmm("10:15"), T(10, 15))
        with self.assertRaises(scan.ScanError):
            scan.parse_now("tomorrow")
        with self.assertRaises(scan.ScanError):
            scan.parse_hhmm("25:00")

    def test_the_session_whose_daily_bar_is_complete(self):
        self.assertEqual(scan.through_session(ist("2026-10-29T08:30")), D(2026, 10, 28))
        self.assertEqual(scan.through_session(ist("2026-10-29T15:34")), D(2026, 10, 28))
        self.assertEqual(scan.through_session(ist("2026-10-29T15:35")), D(2026, 10, 29))
        self.assertEqual(scan.through_session(ist("2026-10-31T10:00")), D(2026, 10, 30))   # Saturday
        self.assertEqual(scan.through_session(ist("2026-10-21T08:00")), D(2026, 10, 19))   # Oct 20 was Dussehra

    def test_sixty_sessions_of_daily_bars(self):
        start, end = scan.daily_range(D(2026, 10, 28))
        self.assertEqual((start, end), (ist("2026-08-03T00:00"), ist("2026-10-28T23:59:59")))
        self.assertEqual(scan.daily_range(D(2026, 10, 29))[0], ist("2026-08-04T00:00"))

    def test_money(self):
        self.assertEqual(scan.money(1167.4), "Rs 1,167.40")
        self.assertEqual(scan.crore(5e7), "Rs 5 crore")
        self.assertEqual(scan.crore(2.5e7), "Rs 2.5 crore")
        self.assertEqual(scan.crore(1e10), "Rs 1,000 crore")


class Daily(unittest.TestCase):
    # Hand-checked from the closes through Wed Oct 28 2026 (the pre-market run on Oct 29).
    EXPECT = {
        "LT": ["breakout_20d", "reclaim_ma20"],   # 3801.30 over 3770.00, the best close of the 20 before;
                                                  # under the 20-day average the day before, now over 3684.28
        "INFY": ["bull_flag"],                    # up 15.2 % over the 10 sessions, last 5 closes in a 3.8 % range
        "ZEEL": ["oversold_bounce"],              # RSI 28.1 on Tuesday, closed up Wednesday, RSI 41.0
        "YESBANK": ["oversold"],                  # RSI 17.8
        "HUDCO": ["oversold", "oversold_bounce"],  # RSI 28.5, 20.3 the day before, closed up
        "ITI": ["oversold", "oversold_bounce"],    # RSI 29.6, 24.5 the day before, closed up
    }                                              # RELIANCE: nothing (RSI 38.0, 1158.00 under its 20-day high 1218.00)

    def setUp(self):
        self.data = load_daily(D(2026, 10, 28))   # the 60 sessions Aug 3 to Oct 28
        self.syms = sorted(self.data)

    def test_setups_before_the_open(self):
        self.assertEqual(len(self.syms), 7)
        hits, counts = scan.scan_daily(self.data, self.syms, D(2026, 10, 28), 0, 0)
        self.assertEqual({h["symbol"]: h["setups"] for h in hits}, self.EXPECT)
        self.assertEqual(counts, {"requested": 7, "fetched": 7, "illiquid": 0, "short": 0, "scanned": 7, "hits": 6})
        self.assertEqual([h["symbol"] for h in hits], ["LT", "INFY", "ZEEL", "YESBANK", "HUDCO", "ITI"])  # most traded
        lt = hits[0]
        self.assertEqual((lt["setup"], lt["price"], lt["rsi"], lt["close_date"], lt["volume"]),
                         ("breakout_20d", 3801.3, 56.2, "2026-10-28", 6174490))
        self.assertEqual(lt["detail"], "closed Rs 3,801.30 over the 20-day closing high Rs 3,770.00; "
                                       "closed back above the 20-day average Rs 3,684.28")
        self.assertEqual((hits[1]["rsi"], hits[1]["detail"]), (55.1, "up 15.2 % then 3.8 % range over 5 sessions"))
        self.assertEqual(hits[2]["detail"], "RSI 28.1 yesterday, closed up")
        self.assertEqual(hits[4]["detail"], "RSI 28.5; RSI 20.3 yesterday, closed up")

    def test_liquidity_filter(self):
        hits, counts = scan.scan_daily(self.data, self.syms, D(2026, 10, 28), 50, 5e7)
        self.assertEqual([h["symbol"] for h in hits], ["LT", "INFY", "ZEEL", "HUDCO"])
        self.assertEqual(counts["illiquid"], 2)     # YESBANK at Rs 15.61; ITI Rs 3.5 crore traded
        self.assertEqual(counts["scanned"], 5)

    def test_todays_bar_counts_only_after_the_closing_auction(self):
        bars = scan.daily_bars(self.data["RELIANCE"], D(2026, 10, 28))
        self.assertEqual((bars[-1], len(bars)), ((D(2026, 10, 28), 1158.0, 21866598), 60))
        data = load_daily(D(2026, 10, 29))                       # Aug 4 to Oct 29, fetched after 15:35
        bars = scan.daily_bars(data["RELIANCE"], D(2026, 10, 29))
        self.assertEqual((bars[-1], len(bars)), ((D(2026, 10, 29), 1165.0, 19826591), 60))
        self.assertEqual(scan.daily_bars(data["RELIANCE"], D(2026, 10, 28))[-1][0], D(2026, 10, 28))
        hits, _ = scan.scan_daily(data, self.syms, D(2026, 10, 29), 0, 0)
        self.assertEqual({h["symbol"]: h["setups"] for h in hits},
                         {"INFY": ["bull_flag"], "LT": ["breakout_20d"], "YESBANK": ["oversold"],
                          "HUDCO": ["oversold", "oversold_bounce"], "ITI": ["oversold", "oversold_bounce"]})
        self.assertEqual([h["rsi"] for h in hits if h["symbol"] == "LT"], [59.2])

    def test_short_and_empty_charts(self):
        ten = [(ist("2026-10-%02dT00:00" % d), 10.0, 10.0, 10.0, 10.0, 5000000) for d in range(1, 11)]
        hits, counts = scan.scan_daily({"X": ten, "E": []}, ["X", "E", "GONE"], D(2026, 10, 28), 5, 0)
        self.assertEqual(hits, [])
        self.assertEqual((counts["fetched"], counts["short"], counts["illiquid"], counts["scanned"]), (1, 1, 0, 0))

    def test_rsi_is_wilders(self):
        self.assertIsNone(scan.rsi([1.0] * 10))
        self.assertEqual(scan.rsi([float(x) for x in range(1, 40)]), 100.0)
        closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61,
                  46.28, 46.28]
        self.assertAlmostEqual(scan.rsi(closes), 70.46, places=1)  # Wilder's textbook example


class DailyFetch(Base):
    def test_one_call_per_symbol_cached_per_session(self):
        code, text, err = run(["daily", "--symbols", "LT,INFY,ZEEL", "--now", "2026-10-29 08:30", "--out", self.wl])
        self.assertEqual(code, 0, err)
        calls = self.net.calls("/instruments/historical/")
        self.assertEqual(len(calls), 3)
        self.assertIn("/instruments/historical/2939649/day?", calls[0])            # LT's token
        self.assertIn("from=2026-08-03+00%3A00%3A00&to=2026-10-28+23%3A59%3A59", calls[0])
        self.assertTrue(os.path.isfile(os.path.join(self.t, "cache", "daily", "LT-2026-10-28.json")))
        self.assertIn("Daily scan, Thu 2026-10-29 08:30 IST, closes through Wed 2026-10-28", text)
        self.assertIn("Fetched 3 of 3 charts. 3 passed the liquidity filter (Rs 50.00+ and Rs 5 crore+ traded); "
                      "3 scanned.", text)
        self.assertIn("3 charts matched a setup: 1 20-day closing-high breakout, 1 bull flag, "
                      "1 reclaimed the 20-day average, 1 oversold bounce.", text)
        self.assertIn("  LT              Rs 3,801.30  RSI  56.2  20-day closing-high breakout, "
                      "reclaimed the 20-day average", text)
        self.assertIn("Wrote %s (mode daily, 3 hits)." % self.wl, text)
        doc = self.read(self.wl)
        self.assertEqual({k: doc[k] for k in ("created_by", "mode", "as_of", "closes_through", "universe",
                                              "scanned", "failed", "source")},
                         {"created_by": "trader-scanner", "mode": "daily", "as_of": "2026-10-29T08:30:00+05:30",
                          "closes_through": "2026-10-28", "universe": 3, "scanned": 3, "failed": 0,
                          "source": "kite"})
        self.assertEqual(list(doc["hits"][0])[:8],
                         ["symbol", "name", "setup", "setups", "price", "cas", "tick_size", "detail"])
        self.assertEqual({k: doc["hits"][0][k] for k in ("symbol", "name", "setup", "price", "cas", "tick_size")},
                         {"symbol": "LT", "name": "LARSEN & TOUBRO", "setup": "breakout_20d", "price": 3801.3,
                          "cas": True, "tick_size": 0.1})
        self.assertEqual((doc["hits"][2]["symbol"], doc["hits"][2]["cas"], doc["hits"][2]["tick_size"]),
                         ("ZEEL", False, 0.01))
        # the same morning again: every chart comes from trader/cache/daily, no Kite call
        code, _, _ = run(["daily", "--symbols", "LT,INFY,ZEEL", "--now", "2026-10-29 09:05", "--out", self.wl])
        self.assertEqual(code, 0)
        self.assertEqual(len(self.net.calls("/instruments/historical/")), 3)

    def test_during_the_session_yesterday_is_the_last_bar_and_after_the_auction_today_is(self):
        code, text, _ = run(["daily", "--symbols", "LT,ZEEL", "--now", "2026-10-29 10:50", "--json", "--no-write"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)["closes_through"], "2026-10-28")
        self.assertIn("to=2026-10-28+23%3A59%3A59", self.net.calls("/instruments/historical/")[0])
        code, text, _ = run(["daily", "--symbols", "LT,ZEEL,HUDCO", "--now", "2026-10-29 15:35", "--json",
                             "--no-write"])
        out = json.loads(text)
        self.assertEqual(out["closes_through"], "2026-10-29")
        self.assertEqual([(h["symbol"], h["setups"]) for h in out["hits"]],
                         [("LT", ["breakout_20d"]), ("HUDCO", ["oversold", "oversold_bounce"])])
        self.assertTrue(os.path.isfile(os.path.join(self.t, "cache", "daily", "LT-2026-10-29.json")))

    def test_unknown_and_failed_symbols_are_counted_not_fatal(self):
        self.net.broken["ZEEL"] = http_error(500, "GeneralException", "Something went wrong")
        code, text, _ = run(["daily", "--symbols", "LT,NOPE,ZEEL", "--now", "2026-10-29 08:30", "--json",
                             "--no-write"])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual((out["universe"], out["scanned"], out["failed"]), (3, 1, 2))
        self.assertEqual(out["errors"]["NOPE"], "NOPE is not listed on NSE")
        self.assertIn("HTTP 500", out["errors"]["ZEEL"])
        self.assertEqual([h["symbol"] for h in out["hits"]], ["LT"])

    def test_a_rejected_token_stops_the_scan_with_the_login_line(self):
        self.net.broken["LT"] = http_error(403, "TokenException", "Incorrect api_key or token")
        code, _, err = run(["daily", "--symbols", "LT,ZEEL", "--now", "2026-10-29 08:30", "--out", self.wl])
        self.assertEqual(code, 1)
        self.assertIn("login.py", err)
        self.assertNotIn("Traceback", err)
        self.assertFalse(os.path.exists(self.wl))


class Orb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_bars5()
        cls.syms = sorted(cls.data)
        cls.quotes = load_quotes()
        cls.day = D(2026, 10, 29)

    def scan_at(self, now, syms=None, min_price=0, or_minutes=15, quotes=None, max_confirm=None, bars_for=None):
        day, cutoff = scan.orb_window(scan.parse_now(now), None, or_minutes, UNTIL)
        return scan.scan_orb(syms or self.syms, day, cutoff, or_minutes, bars_for or (lambda s: self.data[s]),
                             quotes, min_price, 0, max_confirm, CAS)

    def test_the_scan_at_1015(self):
        hits, counts, failed = self.scan_at("2026-10-29 10:15")
        self.assertEqual(len(self.syms), 14)
        self.assertEqual(failed, {})
        self.assertEqual(counts, {"requested": 14, "illiquid": 0, "flat": 0, "candidates": 14, "unconfirmed": 0,
                                  "fetched": 14, "unusable": 0, "scanned": 14, "confirmed": 14, "hits": 6})
        # range = the 09:15, 09:20 and 09:25 bars; RELIANCE's high 1170.50, low 1155.60; bar 5 (09:35)
        # closed 1174.40, 0.33 % over, at 09:40
        reliance = [h for h in hits if h["symbol"] == "RELIANCE"][0]
        self.assertEqual(reliance, {"symbol": "RELIANCE", "setup": "orb", "price": 1174.4, "at": "09:40",
                                    "bar_start": "09:35", "bar": 5, "or_high": 1170.5, "or_low": 1155.6,
                                    "pct_over": 0.33})
        self.assertEqual([(h["symbol"], h["at"]) for h in hits],      # by time, then symbol
                         [("SJVN", "09:35"), ("ZEEL", "09:35"), ("ADANIENT", "09:40"), ("RELIANCE", "09:40"),
                          ("HUDCO", "09:45"), ("ICICIBANK", "10:10")])
        sjvn = hits[0]
        self.assertEqual((sjvn["price"], sjvn["or_high"], sjvn["or_low"], sjvn["pct_over"], sjvn["bar"]),
                         (52.6, 52.21, 50.5, 0.75, 4))
        zeel = hits[1]
        self.assertEqual((zeel["price"], zeel["or_high"], zeel["or_low"], zeel["pct_over"]), (70.46, 70.24, 67.8, 0.31))

    def test_later_in_the_day_more_charts_break_out_and_the_first_hit_stays(self):
        hits, counts, _ = self.scan_at("2026-10-29 14:30")
        self.assertEqual([(h["symbol"], h["at"], h["price"]) for h in hits],
                         [("SJVN", "09:35", 52.6), ("ZEEL", "09:35", 70.46), ("ADANIENT", "09:40", 2512.7),
                          ("RELIANCE", "09:40", 1174.4), ("HUDCO", "09:45", 148.75), ("ICICIBANK", "10:10", 1378.9),
                          ("UCOBANK", "10:20", 22.21), ("LT", "10:40", 3815.0), ("INFY", "13:20", 1139.95)])
        for sym in ("TCS", "BHARTIARTL", "BAJAJ-AUTO", "OLAELEC"):
            self.assertNotIn(sym, [h["symbol"] for h in hits])   # never closed above the range
        self.assertEqual(hits[-2]["pct_over"], 0.0)              # LT closed 3815.00 on a 3814.90 range high

    def test_price_floor_on_a_replay_uses_the_last_close(self):
        hits, counts, _ = self.scan_at("2026-10-29 10:15", min_price=55)
        self.assertEqual(counts["illiquid"], 3)                  # SJVN 51.82 at 10:15, UCOBANK, OLAELEC
        self.assertEqual([h["symbol"] for h in hits], ["ZEEL", "ADANIENT", "RELIANCE", "HUDCO", "ICICIBANK"])

    def test_in_progress_bar_and_no_breakout_yet(self):
        hits, _, _ = self.scan_at("2026-10-29 10:09", syms=["ICICIBANK"])
        self.assertEqual(hits, [])                               # the 10:05 bar is not done until 10:10
        hits, _, _ = self.scan_at("2026-10-29 10:10", syms=["ICICIBANK"])
        self.assertEqual((hits[0]["at"], hits[0]["bar"]), ("10:10", 11))
        bars = scan.session_bars(self.data["ICICIBANK"], self.day, ist("2026-10-29T10:09"), True)
        self.assertEqual([b["start"].strftime("%H:%M") for b in bars][-2:], ["09:55", "10:00"])

    def test_cas_charts_end_at_1510_and_the_others_at_1525(self):
        close = nse.at(self.day, nse.CLOSE)
        bars = scan.session_bars(self.data["RELIANCE"], self.day, close, cas=True)
        self.assertEqual((len(bars), bars[0]["start"].strftime("%H:%M"), bars[-1]["start"].strftime("%H:%M")),
                         (72, "09:15", "15:10"))
        self.assertTrue(scan.usable(bars, self.day, close, 3, cas=True))
        self.assertFalse(scan.usable(bars, self.day, close, 3, cas=False))   # 75 wanted, 72 there
        bars = scan.session_bars(self.data["RAILTEL"], self.day, close, cas=False)
        self.assertEqual((len(bars), bars[-1]["start"].strftime("%H:%M")), (75, "15:25"))
        self.assertTrue(scan.usable(bars, self.day, close, 3, cas=False))

    def test_a_missing_bar_makes_a_chart_unusable(self):
        candles = [c for c in self.data["RELIANCE"] if c[0] != ist("2026-10-29T09:45")]
        cutoff = ist("2026-10-29T10:15")
        bars = scan.session_bars(candles, self.day, cutoff, True)
        self.assertEqual(len(bars), 11)
        self.assertFalse(scan.usable(bars, self.day, cutoff, 3, True))
        hits, counts, _ = self.scan_at("2026-10-29 10:15", syms=["RELIANCE"], bars_for=lambda s: candles)
        self.assertEqual((hits, counts["unusable"], counts["scanned"]), ([], 1, 0))

    def test_stage_one_is_a_necessary_condition(self):
        for sym in self.syms:
            if any(h["symbol"] == sym for h in self.scan_at("2026-10-29 14:30")[0]):
                self.assertTrue(scan.orb_candidate(self.quotes[sym]), sym)
        self.assertFalse(scan.orb_candidate({"ohlc": {"open": 100, "high": 100, "low": 99, "close": 99}}))
        self.assertFalse(scan.orb_candidate({"ohlc": {"open": 0, "high": 0}}))
        self.assertFalse(scan.orb_candidate({}))

    def test_stage_one_quotes_filter_and_rank_the_candidates(self):
        asked = []

        def bars_for(sym):
            asked.append(sym)
            return self.data[sym]
        quotes = dict(self.quotes, FLAT={"last_price": 100, "volume": 1000000,
                                         "ohlc": {"open": 100, "high": 100, "low": 98, "close": 99}})
        hits, counts, failed = self.scan_at("2026-10-29 10:15", syms=self.syms + ["FLAT", "NOQUOTE"], min_price=50,
                                            quotes=quotes, bars_for=bars_for)
        self.assertEqual((counts["illiquid"], counts["flat"], counts["candidates"]), (2, 1, 12))  # UCOBANK, OLAELEC out
        self.assertEqual(failed, {"NOQUOTE": "no quote from Kite"})
        self.assertEqual(len(asked), 12)
        self.assertEqual([h["symbol"] for h in hits], ["SJVN", "ZEEL", "ADANIENT", "RELIANCE", "HUDCO", "ICICIBANK"])
        # the cap keeps the most traded: RELIANCE (Rs 2,444 crore), INFY, ICICIBANK
        del asked[:]
        hits, counts, _ = self.scan_at("2026-10-29 10:15", min_price=50, quotes=quotes, max_confirm=3,
                                       bars_for=bars_for)
        self.assertEqual(asked, ["RELIANCE", "INFY", "ICICIBANK"])
        self.assertEqual((counts["confirmed"], counts["unconfirmed"]), (3, 9))
        self.assertEqual([h["symbol"] for h in hits], ["RELIANCE", "ICICIBANK"])

    def test_a_failed_fetch_is_counted_not_fatal(self):
        def broken(sym):
            if sym == "RELIANCE":
                raise kite.KiteError("Kite GET /instruments/historical/738561/5minute: HTTP 429 NetworkException "
                                     "Too many requests")
            return self.data[sym]
        hits, counts, failed = self.scan_at("2026-10-29 10:15", syms=["RELIANCE", "ZEEL"], bars_for=broken)
        self.assertEqual([h["symbol"] for h in hits], ["ZEEL"])
        self.assertIn("429", failed["RELIANCE"])
        self.assertEqual(counts["fetched"], 1)

    def test_a_30_minute_range(self):
        hits, counts, _ = self.scan_at("2026-10-29 10:15", or_minutes=30)
        self.assertEqual(counts["hits"], 3)
        self.assertEqual(hits[0], {"symbol": "RELIANCE", "setup": "orb", "price": 1175.9, "at": "09:50",
                                   "bar_start": "09:45", "bar": 7, "or_high": 1175.8, "or_low": 1155.6,
                                   "pct_over": 0.01})   # range 09:15 to 09:40 (six bars), high 1175.80
        self.assertEqual([h["symbol"] for h in hits], ["RELIANCE", "ADANIENT", "ICICIBANK"])

    def test_window_messages(self):
        def msg(now, at=None, until=UNTIL, or_minutes=15):
            with self.assertRaises(scan.ScanError) as cm:
                scan.orb_window(scan.parse_now(now), at, or_minutes, until)
            return str(cm.exception)
        self.assertIn("No session on Sat 2026-10-31 (weekend)", msg("2026-10-31 10:15"))
        self.assertIn("Diwali", msg("2026-11-10 10:15"))
        self.assertIn("It is 08:30 IST and the market opens at 09:15", msg("2026-10-29 08:30"))
        self.assertIn("Run orb again after 09:35", msg("2026-10-29 08:30"))
        self.assertIn("It is 09:10 IST, the pre-open", msg("2026-10-29 09:10"))
        self.assertIn("Run orb again after 09:35", msg("2026-10-29 09:34"))
        self.assertIn("Run orb again after 09:50", msg("2026-10-29 09:40", or_minutes=30))
        self.assertIn("expired", msg("2026-10-29 14:45"))
        self.assertIn("expired", msg("2026-10-29 11:30", until=T(11, 0)))
        self.assertIn("closed at 15:30 IST on Thu 2026-10-29", msg("2026-10-29 15:30"))
        self.assertIn("later than now", msg("2026-10-29 10:00", at=T(10, 15)))
        self.assertIn("after the 15:30 close", msg("2026-10-29 17:00", at=T(15, 45)))
        self.assertIn("multiple of 5", msg("2026-10-29 10:15", or_minutes=7))
        self.assertEqual(scan.orb_window(scan.parse_now("2026-10-29 17:00"), T(10, 15), 15, UNTIL),
                         (D(2026, 10, 29), ist("2026-10-29T10:15")))
        self.assertEqual(scan.orb_window(scan.parse_now("2026-10-29 09:35"), None, 15, UNTIL)[1],
                         ist("2026-10-29T09:35"))
        self.assertEqual(scan.orb_window(scan.parse_now("2026-10-29 14:30"), None, 15, UNTIL)[1],
                         ist("2026-10-29T14:30"))


class Cli(Base):
    def test_daily_then_orb_end_to_end(self):
        code, text, err = run(["daily", "--symbols", "LT,INFY,ZEEL,HUDCO,RELIANCE", "--now", "2026-10-29 08:30"])
        self.assertEqual(code, 0, err)
        self.assertEqual([h["symbol"] for h in self.read(self.wl)["hits"]], ["LT", "INFY", "ZEEL", "HUDCO"])

        # 10:15: orb reads the daily watchlist by default
        code, text, err = run(["orb", "--now", "2026-10-29 10:15"])
        self.assertEqual(code, 0, err)
        self.assertIn("Opening-range breakout scan, Thu 2026-10-29 10:15 IST: 15-minute range (09:15 to 09:30), "
                      "5-minute bars, Kite.", text)
        self.assertIn("Stage 1: 4 of 4 quoted; 0 under the price / value floor, 0 never traded above the open; "
                      "4 candidates.", text)
        self.assertIn("Stage 2: fetched 4 charts; 4 usable, checked with real highs and lows.", text)
        self.assertIn("2 of 4 charts closed above their opening-range high by 10:15:", text)
        self.assertIn("  09:35  ZEEL               Rs 70.46  range 67.80 to 70.24  +0.31% over\n", text)
        self.assertIn("  09:45  HUDCO             Rs 148.75  range 147.05 to 148.70  +0.03% over\n", text)
        self.assertIn("not a list of stocks to buy", text)
        doc = self.read(self.wl)
        self.assertEqual({k: doc[k] for k in ("created_by", "created_at", "mode", "as_of", "universe", "scanned",
                                              "failed", "source", "or_minutes", "interval")},
                         {"created_by": "trader-scanner", "created_at": "2026-10-29T10:15:00+05:30", "mode": "orb",
                          "as_of": "2026-10-29T10:15:00+05:30", "universe": 4, "scanned": 4, "failed": 0,
                          "source": "kite", "or_minutes": 15, "interval": "5minute"})
        self.assertEqual(doc["input"]["symbols"], ["LT", "INFY", "ZEEL", "HUDCO"])
        self.assertEqual(doc["hits"][0], {
            "symbol": "ZEEL", "name": "ZEE ENTERTAINMENT ENT", "setup": "orb", "price": 70.46, "cas": False,
            "tick_size": 0.01, "opening_range": {"high": 70.24, "low": 67.8, "minutes": 15},
            "detail": "closed above the 15-minute range at 09:35 (+0.31 %)", "at": "09:35", "bar_start": "09:30",
            "bar": 4, "pct_over": 0.31})
        self.assertEqual(len(self.net.calls("/quote")), 1)
        self.assertEqual(len(self.net.calls("/instruments/historical/")), 4 + 5)   # 4 orb, 5 daily

        # 10:45: the orb watchlist gives back the same four, so LT's 10:35 bar is seen
        code, text, _ = run(["orb", "--now", "2026-10-29 10:45"])
        self.assertEqual(code, 0)
        self.assertIn("3 of 4 charts closed above their opening-range high by 10:45:", text)
        self.assertIn("  10:40  LT              Rs 3,815.00  range 3,776.90 to 3,814.90  +0.00% over  CAS\n", text)
        doc = self.read(self.wl)
        self.assertEqual(doc["input"]["symbols"], ["LT", "INFY", "ZEEL", "HUDCO"])
        self.assertEqual([(h["symbol"], h["cas"], h["tick_size"]) for h in doc["hits"]],
                         [("ZEEL", False, 0.01), ("HUDCO", False, 0.01), ("LT", True, 0.1)])
        self.assertEqual(len(self.net.calls("/quote")), 2)   # orb never reuses a cached quote pass

        # the hits read back as a watchlist, with --at instead of --now
        code, text, _ = run(["orb", "--now", "2026-10-29 12:00", "--at", "10:15", "--watchlist", self.wl, "--json",
                             "--no-write"])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual((out["universe"], out["scanned"], len(out["hits"]), out["as_of"]),
                         (4, 4, 2, "2026-10-29T10:15:00+05:30"))

    def test_partial_stage_two_is_said(self):
        syms = ",".join(sorted(self.net.five))
        code, text, err = run(["orb", "--symbols", syms, "--now", "2026-10-29 10:15", "--max-confirm", "3",
                               "--no-write"])
        self.assertEqual(code, 0, err)
        self.assertIn("Stage 1: 14 of 14 quoted; 2 under the price / value floor, 0 never traded above the open; "
                      "12 candidates.", text)
        self.assertIn("PARTIAL: 9 more candidates were not checked (only the 3 most traded are; --max-confirm 0 "
                      "checks all).", text)
        self.assertIn("2 of 3 charts closed above their opening-range high by 10:15:", text)
        self.assertEqual(len(self.net.calls("/instruments/historical/")), 3)
        self.assertEqual(len(self.net.calls("/quote")), 1)
        self.assertTrue(all("from=2026-10-29+09%3A15%3A00&to=2026-10-29+15%3A30%3A00" in c
                            for c in self.net.calls("/instruments/historical/")))
        code, text, _ = run(["orb", "--symbols", syms, "--now", "2026-10-29 10:15", "--max-confirm", "0",
                             "--no-write"])
        self.assertIn("6 of 12 charts closed above", text)
        self.assertNotIn("PARTIAL", text)

    def test_a_past_day_is_replayed_without_quotes(self):
        scan.real_today = lambda: D(2026, 10, 30)
        code, text, err = run(["orb", "--symbols", "RELIANCE,TCS,SJVN", "--now", "2026-10-29 10:15", "--no-write"])
        self.assertEqual(code, 0, err)
        self.assertIn("Replaying 2026-10-29: 5-minute candles for 3 symbols", err)
        self.assertEqual(self.net.calls("/quote"), [])
        self.assertEqual(len(self.net.calls("/instruments/historical/")), 3)
        self.assertNotIn("Stage 1", text)
        self.assertIn("Stage 2: fetched 3 charts; 3 usable", text)
        self.assertIn("2 of 3 charts closed above their opening-range high by 10:15:", text)
        self.assertIn("  09:35  SJVN               Rs 52.60  range 50.50 to 52.21  +0.75% over\n", text)
        self.assertIn("  09:40  RELIANCE        Rs 1,174.40  range 1,155.60 to 1,170.50  +0.33% over  CAS\n", text)

    def test_plain_messages_and_exit_codes(self):
        code, _, err = run(["orb", "--symbols", "RELIANCE", "--now", "2026-10-31 10:15"])
        self.assertEqual(code, 1)
        self.assertIn("No session on Sat 2026-10-31 (weekend)", err)
        self.assertNotIn("Traceback", err)
        self.assertFalse(os.path.exists(self.wl))
        code, _, err = run(["orb", "--symbols", "RELIANCE", "--now", "2026-10-29 09:30"])
        self.assertEqual(code, 1)
        self.assertIn("Run orb again after 09:35", err)
        cfg = os.path.join(self.t, "config.json")
        with open(cfg, "w") as fh:
            json.dump({"no_entries_after": "10:00"}, fh)
        code, _, err = run(["orb", "--symbols", "RELIANCE", "--now", "2026-10-29 10:15"])
        self.assertEqual(code, 1)
        self.assertIn("after 10:00, your last time for new entries", err)
        code, _, err = run(["orb", "--symbols", "RELIANCE", "--now", "2026-10-29 14:45", "--config", "none.json"])
        self.assertIn("after 14:30", err)
        code, _, err = run(["orb", "--now", "2026-10-29 10:15", "--config", "none.json"])
        self.assertEqual(code, 1)
        self.assertIn("no %s yet: run `scan.py daily` first" % os.path.join("trader", "watchlist.json"), err)
        code, text, _ = run(["orb", "--symbols", "TCS,BHARTIARTL", "--now", "2026-10-29 10:15", "--config", "none.json"])
        self.assertEqual(code, 1)                          # no breakout yet, but the empty list is written
        self.assertIn("No chart closed above its opening-range high by 10:15.", text)
        self.assertEqual(self.read(self.wl)["hits"], [])
        self.assertEqual(len(self.net.calls("/instruments/historical/")), 2)
        code, _, err = run(["daily", "--now", "2026-10-29 08:30"])
        self.assertEqual(code, 1)
        self.assertIn("scan.py universe", err)
        code, _, err = run(["daily", "--universe", os.path.join(self.d, "nope.txt"), "--now", "2026-10-29 08:30"])
        self.assertIn("no file at", err)
        code, _, err = run(["orb", "--symbols", "RELIANCE", "--now", "2026-10-29 10:15", "--at", "9x"])
        self.assertIn("--at wants an IST time", err)
        code, _, err = run(["orb", "--symbols", "RELIANCE", "--now", "yesterday"])
        self.assertIn("--now wants a time", err)

    def test_symbol_lists(self):
        txt = os.path.join(self.d, "list.txt")
        with open(txt, "w") as fh:
            fh.write("# my list\nRELIANCE|RELIANCE INDUSTRIES\nzeel\n\nNSE:RELIANCE\nSJVN.NS\n")
        self.assertEqual(scan.symbols_from_file(txt), ["RELIANCE", "ZEEL", "RELIANCE", "SJVN"])
        js = os.path.join(self.d, "wl.json")
        with open(js, "w") as fh:
            json.dump({"hits": [{"symbol": "reliance"}, {"symbol": "ZEEL"}]}, fh)
        self.assertEqual(scan.symbols_from_file(js), ["RELIANCE", "ZEEL"])
        with open(js, "w") as fh:
            json.dump({"mode": "orb", "input": {"symbols": ["LT", "TCS"]}, "hits": [{"symbol": "LT"}]}, fh)
        self.assertEqual(scan.symbols_from_file(js), ["LT", "TCS"])
        code, text, _ = run(["orb", "--watchlist", txt, "--now", "2026-10-29 10:15", "--json", "--no-write"])
        out = json.loads(text)
        self.assertEqual((out["universe"], out["scanned"], [h["symbol"] for h in out["hits"]]),
                         (3, 3, ["SJVN", "ZEEL", "RELIANCE"]))
        code, text, _ = run(["orb", "--watchlist", txt, "--limit", "2", "--now", "2026-10-29 10:15", "--json",
                             "--no-write"])
        self.assertEqual(json.loads(text)["universe"], 2)
        uni = os.path.join(self.t, "universe.txt")
        with open(uni, "w") as fh:
            fh.write("TCS|TATA CONSULTANCY SERV LT\nZEEL|ZEE ENTERTAINMENT ENT\n")
        code, text, _ = run(["orb", "--universe", "--now", "2026-10-29 10:15", "--json", "--no-write"])
        self.assertEqual([h["symbol"] for h in json.loads(text)["hits"]], ["ZEEL"])
        code, text, _ = run(["daily", "--now", "2026-10-29 08:30", "--json", "--no-write"])
        self.assertEqual(json.loads(text)["universe"], 2)   # daily reads trader/universe.txt by default

    def test_daily_json(self):
        code, text, _ = run(["daily", "--symbols", "LT,YESBANK,ITI", "--now", "2026-10-29 08:30", "--json",
                             "--no-write", "--min-price", "0", "--min-value", "0"])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual([h["symbol"] for h in out["hits"]], ["LT", "YESBANK", "ITI"])
        self.assertEqual(out["note"], "A scan is a list of charts that match a rule, not a list of stocks to buy.")
        self.assertEqual(out["counts"]["scanned"], 3)
        self.assertFalse(os.path.exists(self.wl))


if __name__ == "__main__":
    unittest.main()
