import datetime
import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

news = load("trader-news", "news")
kite = news.kite   # the module news.py imported, so the Kite fakes reach it
DIR = os.path.join(FIX, "trader-news")
IST = news.IST
# the feeds were fetched on 2026-10-29 at about 18:53 IST; the checks pin the clock just before
NOW = "2026-10-29 18:30"


def raw(name):
    with open(os.path.join(DIR, name), "rb") as fh:
        return fh.read()


def ist(text):
    return datetime.datetime.fromisoformat(text).replace(tzinfo=IST)


class FakeResponse(io.BytesIO):
    def __init__(self, body, ctype="application/xml"):
        super().__init__(body)
        self.headers = {"Content-Type": ctype}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(url, code):
    return urllib.error.HTTPError(url, code, "nope", {}, io.BytesIO(b""))


class FakeFeeds:
    """Answers the news feeds from the fixtures, by URL; `down` names feeds that answer an HTTP error."""

    def __init__(self, down=()):
        self.down = set(down)
        self.urls = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.urls.append(url)
        assert req.get_header("User-agent") == news.UA, req.get_header("User-agent")
        if "bseindia.com" in url:
            if "BSE" in self.down:
                raise http_error(url, 503)
            return FakeResponse(raw("bse-announcements.xml"))
        if "news.google.com" in url:
            q = urllib.parse.unquote(url.split("q=")[1].split("&")[0])
            sym = next((s for word, s in (("Reliance", "RELIANCE"), ("Infosys", "INFY"), ("Tata", "TCS"))
                        if word in q), q.split(" OR ")[-1])
            if sym in self.down:
                raise http_error(url, 503)
            return FakeResponse(raw("google-%s.xml" % sym))
        for key, name, file in (("economictimes", "Economic Times", "et-markets.xml"),
                                ("livemint", "Mint", "mint-markets.xml"),
                                ("moneycontrol", "Moneycontrol", "moneycontrol-marketreports.xml"),
                                ("sebi.gov.in", "SEBI", "sebi.xml")):
            if key in url:
                if name in self.down:
                    raise http_error(url, 503)
                return FakeResponse(raw(file))
        raise AssertionError("unexpected feed " + url)


class FakeKite:
    """Kite's instrument list and the quote call, from the fixtures."""

    def __init__(self):
        self.urls = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.urls.append(url)
        if "/instruments/NSE" in url:
            return FakeResponse(raw("instruments-NSE.csv"), "text/csv")
        if "/quote?" in url:
            return FakeResponse(raw("kite-quote.json"), "application/json")
        raise AssertionError("unexpected Kite call " + url)


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = news.main(args)
    return code, out.getvalue(), err.getvalue()


class Feeds(unittest.TestCase):
    def test_google_news_rss(self):
        items = news.parse_rss(raw("google-RELIANCE.xml"), "Google News", "RELIANCE", feed="Google News")
        self.assertEqual(len(items), 30)
        first = items[0]
        self.assertEqual(first["title"], "Jio Platforms becomes 12th Reliance Group company to list on NSE, "
                                         "says CEO Ashishkumar Chauhan")   # the " - Moneycontrol.com" suffix is gone
        self.assertEqual(first["source"], "Moneycontrol.com")
        self.assertEqual(first["feed"], "Google News")
        self.assertEqual(first["symbol"], "RELIANCE")
        self.assertEqual(first["time"], ist("2026-10-28T09:45:50"))   # 04:15:50 GMT
        self.assertTrue(first["link"].startswith("https://news.google.com/rss/articles/"))
        self.assertTrue(first["id"].startswith("Google News:CBMi"))
        self.assertNotIn("<", first["summary"])   # the HTML description is flattened

    def test_market_feeds(self):
        self.assertEqual(len(news.parse_rss(raw("et-markets.xml"), "Economic Times")), 50)
        mint = news.parse_rss(raw("mint-markets.xml"), "Mint")
        self.assertEqual(len(mint), 35)
        self.assertEqual(mint[0]["symbol"], "market")
        self.assertEqual(mint[0]["time"], ist("2026-10-29T18:24:52"))   # published with +0530
        stale = news.parse_rss(raw("moneycontrol-marketreports.xml"), "Moneycontrol")
        self.assertEqual(len(stale), 4)
        self.assertEqual(stale[0]["time"].year, 2024)   # the feed answers 200 but has not moved since 2024
        sebi = news.parse_rss(raw("sebi.xml"), "SEBI")
        self.assertEqual(len(sebi), 30)
        self.assertEqual(sebi[0]["time"], ist("2026-10-29T00:00:00"))   # "29 Oct, 2026 +0530": a date, so midnight IST

    def test_times_land_in_ist(self):
        self.assertEqual(news.parse_time("Thu, 29 Oct 2026 13:23:21 GMT"), ist("2026-10-29T18:53:21"))
        self.assertEqual(news.parse_time("2026-10-29T10:00:00+00:00"), ist("2026-10-29T15:30:00"))
        self.assertEqual(news.parse_time("28 Oct, 2026 +0530"), ist("2026-10-28T00:00:00"))
        self.assertIsNone(news.parse_time("soon"))
        self.assertIsNone(news.parse_time(""))

    def test_now_is_ist_unless_an_offset_says_otherwise(self):
        self.assertEqual(news.parse_now("2026-10-29 10:15"), ist("2026-10-29T10:15"))
        self.assertEqual(news.parse_now("2026-10-29T04:45:00+00:00"), ist("2026-10-29T10:15"))
        with self.assertRaises(news.NewsError):
            news.parse_now("tomorrow")

    def test_company_names_from_kite_rows(self):
        self.assertEqual(news.clean_name("RELIANCE INDUSTRIES"), "Reliance Industries")
        self.assertEqual(news.clean_name("TATA CONSULTANCY SERV LT"), "Tata Consultancy")   # Kite's truncation
        self.assertEqual(news.clean_name("STATE BANK OF INDIA"), "State Bank of India")
        self.assertEqual(news.clean_name("HDFC BANK"), "HDFC Bank")
        self.assertEqual(news.clean_name("MAHINDRA & MAHINDRA"), "Mahindra & Mahindra")
        self.assertEqual(news.google_query("INFY", "Infosys"), '"Infosys" OR INFY')
        # RELIANCE is an English word: the bare symbol would match "reduce reliance on China"
        self.assertEqual(news.google_query("RELIANCE", "Reliance Industries"),
                         '"Reliance Industries" OR "Reliance shares"')
        self.assertEqual(news.google_query("ZZZZ", None), "ZZZZ")

    def test_tickers_in_any_spelling(self):
        self.assertEqual(news.split_tickers(["RELIANCE,INFY", "tcs", "NSE:INFY", "RELIANCE.NS"]),
                         ["RELIANCE", "INFY", "TCS"])

    def test_moves_from_kite_quotes(self):
        data = json.loads(raw("kite-quote.json"))["data"]
        quotes = {k.split(":", 1)[1]: v for k, v in data.items()}
        moves = news.parse_quotes(quotes, ["INFY", "RELIANCE", "TCS", "ZZZZ"])
        self.assertEqual([m["symbol"] for m in moves], ["INFY", "RELIANCE", "TCS"])   # no quote, no line
        infy = moves[0]
        self.assertEqual((infy["price"], infy["prev_close"], infy["pct"]), (1146.5, 1123.15, 2.08))
        self.assertEqual(infy["time"], ist("2026-10-29T15:59:53"))
        self.assertNotIn("MOVING", news.render_moves([infy])[0])
        self.assertIn("MOVING", news.render_moves([infy], move_pct=2.0)[0])
        self.assertRegex(news.render_moves([infy])[0],
                         r"INFY\s+1,146\.50 \+2\.08% vs yesterday's close 1,123\.15 at 15:59 IST")


class BseAnnouncements(unittest.TestCase):
    def setUp(self):
        self.entries = news.parse_bse(raw("bse-announcements.xml"))

    def test_parse(self):
        self.assertEqual(len(self.entries), 30)
        first = self.entries[0]
        self.assertEqual((first["company"], first["code"]), ("Reliance Industries Ltd", "750634"))
        self.assertEqual(first["time"], ist("2026-10-29T08:55:13"))   # "29-Oct-2026 08:55:13" is IST
        self.assertEqual(first["description"], "Voluntary strike off of Hamleys (Franchising) Limited.")
        self.assertTrue(first["link"].startswith("https://www.bseindia.com/xml-data/corpfiling/AttachLive/"))

    def test_match_by_name_prefix_and_merge_the_scrip_codes(self):
        names = {"RELIANCE": "Reliance Industries", "INFY": "Infosys", "TCS": "Tata Consultancy"}
        items = news.match_bse(self.entries, names)
        self.assertEqual([i["symbol"] for i in items], ["RELIANCE"] * 3 + ["INFY"] * 2)   # equity + debt codes
        merged = news.dedupe(news.tag(items, names))
        self.assertEqual([i["symbol"] for i in merged], ["RELIANCE", "INFY", "INFY"])   # one PDF, three codes
        self.assertEqual(merged[0]["source"], "BSE filing")
        self.assertEqual(merged[0]["kind"], "filing")
        self.assertEqual(merged[0]["title"], "Voluntary strike off of Hamleys (Franchising) Limited.")

    def test_pin_a_scrip_code(self):
        self.assertEqual(news.parse_bse_codes(["reliance=500325", "INFY=500209"]),
                         {"RELIANCE": "500325", "INFY": "500209"})
        with self.assertRaises(news.NewsError):
            news.parse_bse_codes(["RELIANCE"])
        items = news.match_bse(self.entries, {"RELIANCE": "Reliance Industries"}, {"RELIANCE": "978265"})
        self.assertEqual([i["code"] for i in items], ["978265"])

    def test_insider_sast_tag_and_not_the_board_notice(self):
        items = news.match_bse(self.entries, {"CAMLINFINE": "Camlin Fine Sciences", "SKMEGGPROD": "SKM Egg Products",
                                              "OVOBEL": "Ovobel Foods"})
        news.tag(items, {})
        by = {i["symbol"]: i for i in items}
        self.assertIn("insider-sast", by["CAMLINFINE"]["tags"])   # Regulation 31 of SAST: a pledge disclosure
        self.assertTrue(by["CAMLINFINE"]["important"])
        self.assertIn("insider-sast", by["SKMEGGPROD"]["tags"])   # Regulation 29(2)
        self.assertNotIn("insider-sast", by["OVOBEL"]["tags"])    # a board meeting notice citing the PIT code
        self.assertIn("board-meeting", by["OVOBEL"]["tags"])


class Tagging(unittest.TestCase):
    def test_touches_and_tags(self):
        items = news.parse_rss(raw("google-TCS.xml"), "Google News", "TCS", feed="Google News")
        names = {"TCS": "Tata Consultancy", "INFY": "Infosys", "RELIANCE": "Reliance Industries"}
        news.tag(items, names)
        by = {i["title"][:30]: i for i in items}
        both = by["TCS, Infosys, other IT stocks "]
        self.assertEqual(both["touches"], ["INFY", "TCS"])
        self.assertIn("deal", both["tags"])   # the IPO
        self.assertTrue(all("TCS" in i["touches"] for i in items))   # the query was for TCS

    def test_day_trading_tags(self):
        items = [{"id": "x", "feed": "Google News", "source": "x", "kind": "headline", "symbol": "RELIANCE",
                  "title": "Trading halted in XYZ after SEBI order; RBI holds repo rate; promoter sells "
                           "2% stake via bulk deal, board declares dividend and bonus issue",
                  "summary": "", "link": "", "time": None}]
        tags = news.tag(items, {"RELIANCE": "Reliance Industries"})[0]["tags"]
        for t in ("halt", "regulatory", "macro", "promoter", "bulk-deal", "dividend"):
            self.assertIn(t, tags)
        self.assertNotIn("deal", tags)   # "bulk deal" is its own tag, not a merger
        self.assertTrue(items[0]["important"])

    def test_results_and_analyst_tags(self):
        items = [{"id": "y", "feed": "Economic Times", "source": "Economic Times", "kind": "headline",
                  "symbol": "market", "title": "Infosys Q2 results: net profit up 5%, Jefferies downgrades "
                                                 "to hold, cuts target price", "summary": "", "link": "", "time": None}]
        it = news.tag(items, {"INFY": "Infosys"})[0]
        self.assertEqual(it["touches"], ["INFY"])
        self.assertIn("results", it["tags"])
        self.assertIn("analyst", it["tags"])

    def test_dedupe_merges_the_same_story(self):
        a = news.parse_rss(raw("google-INFY.xml"), "Google News", "INFY", feed="Google News")
        b = news.parse_rss(raw("google-TCS.xml"), "Google News", "TCS", feed="Google News")
        names = {"INFY": "Infosys", "TCS": "Tata Consultancy"}
        items = news.dedupe(news.tag(a + b, names))
        self.assertLess(len(items), 60)   # the same stories come back for both queries
        shared = [i for i in items if i["touches"] == ["INFY", "TCS"]]
        self.assertTrue(shared)
        self.assertEqual(len({i["id"] for i in items}), len(items))

    def test_market_items_need_a_touch_or_a_market_tag(self):
        now = ist(NOW)
        mk = lambda title, sym="market": {"id": title, "feed": "Mint", "source": "Mint", "kind": "headline",
                                          "symbol": sym, "title": title, "summary": "", "link": "",
                                          "time": now - datetime.timedelta(hours=1)}
        items = news.tag([mk("Zensar Q2 results: profit jumps 20%"),
                          mk("RBI holds the repo rate at 5.25%"),
                          mk("Infosys shares slip after guidance cut"),
                          mk("Top 3 stocks to buy today", "INFY")], {"INFY": "Infosys"})
        shown = news.select(items, {}, now, hours=24)
        self.assertEqual(sorted(i["title"][:8] for i in shown), ["Infosys ", "RBI hold", "Top 3 st"])


class Check(unittest.TestCase):
    """main() end to end: the feeds and Kite are fakes, the session file is fresh, cwd is a scratch folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.env = os.environ.get("KITE_API_KEY")
        os.environ["KITE_API_KEY"] = "test-key"
        kite.save_session({"access_token": "t0k", "user_id": "AB1234"}, kite.now_ist())
        self.feeds, self.kite = FakeFeeds(), FakeKite()
        self.saved = (news._urlopen, news._sleep, kite._urlopen, kite._sleep, dict(kite._last_call))
        news._urlopen, news._sleep = self.feeds, lambda s: None
        kite._urlopen, kite._sleep = self.kite, lambda s: None
        kite._last_call.clear()
        self.state = os.path.join(self.tmp.name, "state", "news_seen.json")
        none = os.path.join(self.tmp.name, "none.json")
        self.base = ["--state", self.state, "--now", NOW, "--portfolio", none, "--watchlist", none, "--plan", none]

    def tearDown(self):
        news._urlopen, news._sleep, kite._urlopen, kite._sleep, last = self.saved
        kite._last_call.clear()
        kite._last_call.update(last)
        if self.env is None:
            os.environ.pop("KITE_API_KEY", None)
        else:
            os.environ["KITE_API_KEY"] = self.env
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def test_new_items_once_newest_first(self):
        code, out, err = run(["--tickers", "RELIANCE,INFY", "TCS", "--hours", "24", "--json"] + self.base)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        times = [i["time"] for i in data["items"]]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertEqual(len(data["items"]), 41)   # 21 Google News, 9 Economic Times, 8 Mint, 3 BSE filings
        self.assertEqual(sum(1 for i in data["items"] if i["feed"] == "Google News"), 21)
        self.assertEqual(sum(1 for i in data["items"] if i["feed"] == "BSE"), 3)   # RELIANCE once, INFY twice
        self.assertEqual(sum(1 for i in data["items"] if i["important"]), 7)
        self.assertEqual([m["symbol"] for m in data["moves"]], ["INFY", "RELIANCE", "TCS"])
        self.assertEqual(data["notes"], [])
        self.assertEqual(data["checked"], "2026-10-29T18:30:00+05:30")
        # one Google query per symbol, with the company name from Kite's instrument row
        google = [u for u in self.feeds.urls if "news.google.com" in u]
        self.assertEqual(len(google), 3)
        self.assertIn(urllib.parse.quote('"Reliance Industries" OR "Reliance shares"'), google[1])
        self.assertIn(urllib.parse.quote('"Tata Consultancy" OR TCS'), google[2])
        self.assertIn("hl=en-IN&gl=IN&ceid=IN:en", google[0])
        # Kite: the instrument list once, the quote once
        self.assertEqual(len(self.kite.urls), 2)
        # the second check: everything has been seen
        code, out, _ = run(["--tickers", "RELIANCE,INFY,TCS", "--hours", "24"] + self.base)
        self.assertEqual(code, 1)
        self.assertIn("Nothing new.", out)
        with open(self.state) as fh:
            saved = json.load(fh)
        self.assertEqual(saved["last_check"], "2026-10-29T18:30:00+05:30")
        self.assertGreater(len(saved["seen"]), 38)   # older items are remembered too

    def test_text_report_in_ist(self):
        code, out, err = run(["--tickers", "RELIANCE", "--hours", "6"] + self.base)
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertRegex(lines[0], r"RELIANCE\s+1,167\.40 \+0\.81% vs yesterday's close 1,158\.00 at 15:59 IST")
        self.assertIn("! 2026-10-29 14:54 IST  RELIANCE     Economic Times   RIL Q2 Results Preview", out)
        self.assertIn("  2026-10-29 16:10 IST  RELIANCE     TradingView      Reliance Industries to dissolve", out)
        self.assertEqual(len([l for l in lines if l.startswith(("  2026-", "! 2026-"))]), 13)
        tags = lines[lines.index([l for l in lines if "RIL Q2 Results Preview" in l][0]) + 1]
        self.assertIn("tags: results", tags)
        self.assertIn("Checked 2026-10-29 18:30 IST", out)
        self.assertIn("Not financial advice", out)
        self.assertNotIn(chr(0x2014), out)

    def test_no_market_and_the_pinned_clock(self):
        code, out, err = run(["--tickers", "RELIANCE", "--hours", "24", "--no-market", "--json", "--dry-run"]
                             + self.base)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(len(data["items"]), 8)   # 7 headlines and the day's BSE filing
        self.assertTrue(all(i["symbol"] == "RELIANCE" for i in data["items"]))
        self.assertEqual(len(self.feeds.urls), 2)   # Google News and the BSE feed; no market feeds
        # pin the clock at noon: the afternoon's stories are not there yet
        args = list(self.base)
        args[args.index("--now") + 1] = "2026-10-29 12:00"
        code, out, _ = run(["--tickers", "RELIANCE", "--hours", "24", "--no-market", "--json"] + args)
        data = json.loads(out)
        self.assertEqual(len(data["items"]), 12)   # the window moves back with the clock
        self.assertTrue(all(i["time"] <= "2026-10-29T12:10" for i in data["items"]))
        self.assertEqual(data["checked"], "2026-10-29T12:00:00+05:30")

    def test_symbols_from_watchlist_plan_and_portfolio(self):
        wl = os.path.join(self.tmp.name, "watchlist.json")
        with open(wl, "w") as fh:
            json.dump({"created_by": "trader-scanner", "hits": [{"symbol": "INFY", "setup": "orb"}]}, fh)
        plan = os.path.join(self.tmp.name, "plan.json")
        with open(plan, "w") as fh:
            json.dump({"created_by": "trader-exit", "symbol": "TCS"}, fh)
        pf = os.path.join(self.tmp.name, "portfolio.json")
        with open(pf, "w") as fh:
            json.dump({"cash_inr": 90000.0, "positions": {"RELIANCE": {"qty": 64, "avg": 1171.6}}}, fh)
        self.assertEqual(sorted(news.watch_from_files(pf, wl, plan)), ["INFY", "RELIANCE", "TCS"])
        args = list(self.base)
        args[args.index("--watchlist") + 1] = wl
        code, out, err = run(["--json", "--dry-run", "--hours", "24", "--no-market"] + args)
        self.assertEqual(code, 0, err)
        self.assertEqual({t for i in json.loads(out)["items"] for t in i["touches"]}, {"INFY"})
        self.assertFalse(os.path.exists(self.state))

    def test_no_symbols_is_an_error(self):
        code, _, err = run(self.base)
        self.assertEqual(code, 2)
        self.assertIn("no symbols", err)

    def test_no_kite_session_says_so_and_exits_1(self):
        os.remove(os.path.join("trader", "kite_session.json"))
        code, out, err = run(["--tickers", "RELIANCE"] + self.base)
        self.assertEqual(code, 1)
        self.assertIn("login.py", err)
        self.assertEqual(out, "")
        self.assertEqual(self.feeds.urls, [])

    def test_a_feed_down_is_one_note_and_the_rest_goes_on(self):
        self.feeds.down = {"Economic Times", "INFY"}
        code, out, err = run(["--tickers", "RELIANCE,INFY", "--hours", "24"] + self.base)
        self.assertEqual(code, 0, err)
        self.assertIn("note: Economic Times feed: HTTP 503 for https://economictimes.indiatimes.com", out)
        self.assertIn("note: headlines for INFY: HTTP 503", out)
        self.assertIn("Mint", out)
        self.assertEqual(len([u for u in self.feeds.urls if "economictimes" in u]), 1)   # no retry past an error

    def test_unknown_symbol_is_a_note(self):
        code, out, err = run(["--tickers", "RELIANCE,ZZZZ", "--hours", "6", "--no-market"] + self.base)
        self.assertEqual(code, 0, err)
        self.assertIn("note: ZZZZ is not listed on NSE", out)
        self.assertEqual(len([u for u in self.feeds.urls if "google" in u]), 1)
        self.assertIn("i=NSE%3ARELIANCE", self.kite.urls[-1])
        self.assertNotIn("ZZZZ", self.kite.urls[-1])

    def test_bse_filings_in_the_report_cached_ten_minutes(self):
        code, out, err = run(["--tickers", "RELIANCE,INFY", "--hours", "24", "--no-market", "--dry-run"] + self.base)
        self.assertEqual(code, 0, err)
        self.assertIn("  2026-10-29 08:55 IST  RELIANCE     BSE filing       Voluntary strike off of Hamleys", out)
        self.assertIn("  2026-10-29 15:14 IST  INFY         BSE filing       Enclosed", out)
        self.assertIn("/xml-data/corpfiling/AttachLive/", out)
        cached = os.listdir(os.path.join("trader", "cache"))
        self.assertIn("bse-announcements-2026-10-29-1830.xml", cached)
        # the second check within ten minutes reads the cache, not BSE
        run(["--tickers", "RELIANCE", "--hours", "24", "--no-market", "--dry-run"] + self.base)
        self.assertEqual(len([u for u in self.feeds.urls if "bseindia" in u]), 1)
        # --no-bse skips it; a BSE outage is one note
        self.feeds.urls, self.feeds.down = [], {"BSE"}
        code, out, _ = run(["--tickers", "RELIANCE", "--hours", "24", "--no-market", "--no-bse", "--dry-run"] + self.base)
        self.assertEqual([u for u in self.feeds.urls if "bseindia" in u], [])
        self.assertNotIn("BSE filing", out)
        os.remove(os.path.join("trader", "cache", "bse-announcements-2026-10-29-1830.xml"))
        code, out, _ = run(["--tickers", "RELIANCE", "--hours", "24", "--no-market", "--dry-run"] + self.base)
        self.assertIn("note: BSE announcements: HTTP 503", out)
        self.assertIn("Hamleys Franchising", out)   # the headlines still came

    def test_moving_flag_and_no_moves(self):
        code, out, _ = run(["--tickers", "INFY", "--hours", "6", "--no-market", "--move-pct", "2"] + self.base)
        self.assertIn("INFY       1,146.50 +2.08% vs yesterday's close 1,123.15 at 15:59 IST  MOVING", out)
        code, out, _ = run(["--tickers", "INFY", "--hours", "6", "--no-market", "--no-moves", "--all"] + self.base)
        self.assertNotIn("vs yesterday's close", out)
        self.assertEqual(len([u for u in self.kite.urls if "/quote?" in u]), 1)

    def test_all_shows_seen_items_again(self):
        run(["--tickers", "RELIANCE", "--hours", "24", "--no-market"] + self.base)
        code, out, _ = run(["--tickers", "RELIANCE", "--hours", "24", "--no-market", "--all", "--json"] + self.base)
        self.assertEqual(code, 1)   # nothing new, but
        data = json.loads(out)
        self.assertEqual(len(data["items"]), 8)
        self.assertTrue(all(i["new"] is False for i in data["items"]))

    def test_state_forgets_after_30_days(self):
        state = {"seen": {"old": "2026-08-01T00:00:00+05:30", "fresh": "2026-10-28T09:00:00+05:30"}}
        now = ist(NOW)
        news.remember([], state, now)
        self.assertEqual(sorted(state["seen"]), ["fresh"])
        self.assertEqual(state["last_check"], now.isoformat())


if __name__ == "__main__":
    unittest.main()
