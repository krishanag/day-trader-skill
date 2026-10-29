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

insiders = load("trader-insiders", "insiders")
kite = insiders.kite   # the module insiders.py imported, so the Kite fakes reach it
DIR = os.path.join(FIX, "trader-insiders")
NEWS_DIR = os.path.join(FIX, "trader-news")
# The three CSVs are SAMPLES written in NSE's own format (real column headers, fictional rows):
# NSE blocks scripted downloads, so no real file could be fetched for the fixtures. See README.txt there.
INSIDER = os.path.join(DIR, "insider-trading-sample.csv")
BULK = os.path.join(DIR, "bulk-deals-sample.csv")
BLOCK = os.path.join(DIR, "block-deals-sample.csv")
IST = insiders.IST
NOW = "2026-10-29 18:30"


def raw(folder, name):
    with open(os.path.join(folder, name), "rb") as fh:
        return fh.read()


def ist(text):
    return datetime.datetime.fromisoformat(text).replace(tzinfo=IST)


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = insiders.main(args)
    return code, out.getvalue(), err.getvalue()


def no_lists(tmp):
    none = os.path.join(tmp, "none.json")
    return ["--portfolio", none, "--watchlist", none]


class FakeResponse(io.BytesIO):
    def __init__(self, body, ctype="application/xml"):
        super().__init__(body)
        self.headers = {"Content-Type": ctype}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeNet:
    """Google News from the fixture (or an HTTP error), Kite's instrument list from the news fixture."""

    def __init__(self, google_code=200):
        self.google_code = google_code
        self.urls = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.urls.append(url)
        if "bseindia.com" in url:
            assert req.get_header("User-agent") == insiders.UA
            return FakeResponse(raw(NEWS_DIR, "bse-announcements.xml"))
        if "news.google.com" in url:
            assert req.get_header("User-agent") == insiders.UA
            if self.google_code != 200:
                raise urllib.error.HTTPError(url, self.google_code, "nope", {}, io.BytesIO(b""))
            return FakeResponse(raw(DIR, "google-deals-RELIANCE.xml"))
        if "/instruments/NSE" in url:
            return FakeResponse(raw(NEWS_DIR, "instruments-NSE.csv"), "text/csv")
        raise AssertionError("unexpected call " + url)


class Helpers(unittest.TestCase):
    def test_nse_dates(self):
        self.assertEqual(insiders.parse_date("29-OCT-2026"), ("2026-10-29", None))
        self.assertEqual(insiders.parse_date("28-Oct-2026 17:42"), ("2026-10-28", "17:42"))
        self.assertEqual(insiders.parse_date("29/10/2026"), ("2026-10-29", None))
        self.assertEqual(insiders.parse_date("2026-10-29 09:05:10"), ("2026-10-29", "09:05"))
        self.assertEqual(insiders.parse_date("-"), (None, None))
        self.assertEqual(insiders.parse_date("31-Nov-2026"), (None, None))
        self.assertEqual(insiders.iso_stamp("2026-10-28", "17:42"), "2026-10-28T17:42:00+05:30")
        self.assertEqual(insiders.iso_stamp("2026-10-28", None), "2026-10-28")

    def test_trading_days_skip_weekends_and_nse_holidays(self):
        self.assertEqual(insiders.trading_days_between("2026-10-23", "2026-10-29"), 4)   # Fri -> Thu
        self.assertEqual(insiders.trading_days_between("2026-10-16", "2026-10-21"), 2)   # Dussehra (20 Oct) is out
        self.assertEqual(insiders.trading_days_between("2026-10-29", "2026-10-29"), 0)
        self.assertEqual(insiders.trading_days_between("2026-10-29", "2026-10-28"), 0)   # never negative
        self.assertIsNone(insiders.trading_days_between(None, "2026-10-28"))

    def test_rupees_and_numbers(self):
        self.assertEqual(insiders.inr(14040000), "Rs 1.40 cr")
        self.assertEqual(insiders.inr(3750000), "Rs 37.50 lakh")
        self.assertEqual(insiders.inr(85000), "Rs 85,000.00")
        self.assertEqual(insiders.inr(58500000000), "Rs 5,850.00 cr")
        self.assertEqual(insiders.inr(None), "value not given")
        self.assertEqual(insiders.number("1,40,40,000.00"), 14040000.0)   # Indian grouping
        self.assertEqual(insiders.count("7,250,000"), 7250000)
        self.assertIsNone(insiders.number("-"))
        self.assertIsNone(insiders.number("abc"))

    def test_labels(self):
        self.assertEqual(insiders.label("Promoters", insiders.CATEGORIES), "Promoter")
        self.assertEqual(insiders.label("Promoter Group", insiders.CATEGORIES), "Promoter Group")
        self.assertEqual(insiders.label("Employees/Designated Employees", insiders.CATEGORIES), "Employee")
        self.assertEqual(insiders.label("Immediate relative", insiders.CATEGORIES), "Immediate Relative")
        self.assertEqual(insiders.label("Trustee", insiders.CATEGORIES), "Trustee")   # unknown: kept as is
        self.assertEqual(insiders.label("Pledge Revoke", insiders.SIDES), "pledge released")
        self.assertEqual(insiders.label("Invocation", insiders.SIDES), "pledge invoked")
        self.assertEqual(insiders.label("Market Sale", insiders.MODES), "market")
        self.assertEqual(insiders.label("Off Market", insiders.MODES), "off-market")
        self.assertEqual(insiders.label("Inter-se Transfer", insiders.MODES), "inter-se transfer")
        self.assertEqual(insiders.clean_name("TATA CONSULTANCY SERV LT"), "Tata Consultancy")
        self.assertEqual(insiders.clean_name("HDFC BANK"), "HDFC Bank")


class InsiderFile(unittest.TestCase):
    def setUp(self):
        self.kind, self.rows = insiders.parse_file(INSIDER)

    def test_detects_the_file_and_reads_every_row(self):
        self.assertEqual(self.kind, "insider")
        self.assertEqual(len(self.rows), 8)
        self.assertEqual({r["kind"] for r in self.rows}, {"insider"})
        self.assertEqual(sorted({r["symbol"] for r in self.rows}), ["HDFCBANK", "INFY", "RELIANCE", "TCS"])

    def test_a_promoter_market_buy_on_time(self):
        r = next(r for r in self.rows if r["person"] == "Sample Promoter Holdings LLP" and r["side"] == "buy")
        self.assertEqual(r["company"], "Reliance Industries Limited")
        self.assertEqual(r["regulation"], "7(2)")
        self.assertEqual(r["category"], "Promoter Group")
        self.assertEqual((r["qty"], r["value_inr"]), (12000, 14040000.0))   # "1,40,40,000.00"
        self.assertEqual(r["mode"], "market")
        self.assertEqual(r["mode_raw"], "Market Purchase")
        self.assertEqual((r["trade_from"], r["trade_to"], r["intimated"]), ("2026-10-26", "2026-10-26", "2026-10-27"))
        self.assertEqual(r["broadcast"], "2026-10-28T17:42:00+05:30")
        self.assertEqual((r["days_to_company"], r["days_to_exchange"]), (1, 1))
        self.assertFalse(r["late"])
        self.assertEqual((r["pct_prior"], r["pct_post"]), (4.62, 4.62))
        self.assertEqual(r["exchange"], "NSE")
        self.assertIsNone(r["remark"])
        self.assertIsNone(r["xbrl"])
        self.assertEqual(len(r["id"]), 16)

    def test_late_to_the_company_and_late_to_the_exchange(self):
        sale = next(r for r in self.rows if r["symbol"] == "INFY" and r["category"] == "Designated Person")
        self.assertEqual(sale["side"], "sell")
        self.assertEqual((sale["trade_from"], sale["trade_to"]), ("2026-10-22", "2026-10-23"))
        self.assertEqual(sale["days_to_company"], 4)   # Fri 23 -> Thu 29
        self.assertTrue(sale["late_to_company"])
        self.assertFalse(sale["late_to_exchange"])
        pledge = next(r for r in self.rows if r["side"] == "pledge")
        self.assertEqual(pledge["category"], "Promoter")
        self.assertEqual(pledge["mode"], "off-market")
        self.assertEqual(pledge["value_inr"], 58500000000.0)
        self.assertEqual(pledge["days_to_company"], 1)    # 20 Oct (Dussehra) to 21 Oct
        self.assertEqual(pledge["days_to_exchange"], 4)   # 21 Oct to 27 Oct
        self.assertTrue(pledge["late_to_exchange"])
        self.assertTrue(pledge["late"])
        self.assertEqual(pledge["remark"], "Pledge in favour of lender")

    def test_esop_gift_inter_se_and_a_derivative(self):
        by = {(r["symbol"], r["person"]): r for r in self.rows}
        esop = by[("TCS", "B. Sample")]
        self.assertEqual((esop["category"], esop["mode"], esop["qty"]), ("Employee", "ESOP", 1500))   # "1,500"
        gift = by[("HDFCBANK", "C. Sample")]
        self.assertEqual((gift["category"], gift["mode"], gift["value_inr"]), ("Immediate Relative", "gift", None))
        trust = by[("INFY", "Sample Family Trust")]
        self.assertEqual((trust["side"], trust["mode"], trust["qty_post"]), ("sell", "inter-se transfer", 0))
        self.assertEqual(trust["days_to_company"], 0)
        fut = by[("RELIANCE", "E. Sample")]
        self.assertIsNone(fut["qty"])
        self.assertEqual(fut["derivative"], {"type": "Stock Futures", "contract": "RELIANCE 24-Nov-2026 FUT",
                                             "notional_buy_inr": 5857500.0, "units_buy": 1,
                                             "notional_sell_inr": None, "units_sell": None})
        same_day = by[("TCS", "D. Sample")]
        self.assertEqual((same_day["days_to_company"], same_day["days_to_exchange"]), (0, 0))

    def test_select_newest_first_by_symbol_and_since(self):
        rows = insiders.select(self.rows)
        self.assertEqual(rows[0]["broadcast"], "2026-10-29T19:10:00+05:30")
        self.assertEqual(rows[-1]["broadcast"], "2026-10-27T12:15:00+05:30")
        self.assertEqual([r["person"] for r in insiders.select(self.rows, {"RELIANCE"})],
                         ["E. Sample", "Sample Promoter Holdings LLP", "Sample Promoter Holdings LLP"])
        self.assertEqual(len(insiders.select(self.rows, since="2026-10-29")), 4)

    def test_headers_are_matched_by_meaning(self):
        # the correct spellings, lower case, stray spaces, a BOM: the same rows come out
        with open(INSIDER, encoding="utf-8") as fh:
            text = fh.read()
        head, body = text.split("\n", 1)
        head = (head.replace("DISPLOSED", "disposed").replace("INITMATION", "Intimation")
                .replace("BROADCATE", "Broadcast").lower().replace(",", " , "))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "CF-Insider-Trading-equities.csv")
            with open(path, "w", encoding="utf-8-sig") as fh:
                fh.write(head + "\n" + body)
            kind, rows = insiders.parse_file(path)
        self.assertEqual(kind, "insider")
        self.assertEqual([r["id"] for r in rows], [r["id"] for r in self.rows])

    def test_a_file_that_is_not_nse_lists_its_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.csv")
            with open(path, "w") as fh:
                fh.write("Date,Open,High,Low,Close\n2026-10-29,1,2,0,1\n")
            with self.assertRaisesRegex(insiders.InsidersError, "its headers are: Date, Open, High, Low, Close"):
                insiders.parse_file(path)
            with open(path, "w") as fh:
                fh.write("SYMBOL,CATEGORY OF PERSON,REMARK\nINFY,Director,-\n")
            with self.assertRaisesRegex(insiders.InsidersError, "no column for person, qty.*Headers found: SYMBOL"):
                insiders.parse_file(path)
            with open(path, "w") as fh:
                fh.write("\n")
            with self.assertRaisesRegex(insiders.InsidersError, "is empty"):
                insiders.parse_file(path)


class DealFiles(unittest.TestCase):
    def test_bulk_deals(self):
        kind, rows = insiders.parse_file(BULK)
        self.assertEqual(kind, "bulk")
        self.assertEqual(len(rows), 6)
        r = rows[0]
        self.assertEqual((r["date"], r["symbol"], r["side"], r["qty"], r["price"]),
                         ("2026-10-29", "RELIANCE", "buy", 7250000, 1171.5))
        self.assertEqual(r["value_inr"], 8493375000.0)
        self.assertEqual(r["client"], "SAMPLE ALPHA CAPITAL FUND")
        self.assertEqual(r["company"], "Reliance Industries Limited")
        self.assertIsNone(r["remarks"])
        self.assertNotEqual(rows[0]["id"], rows[1]["id"])   # the same client's buy and sell are two rows

    def test_block_deals_by_file_name_or_kind(self):
        kind, rows = insiders.parse_file(BLOCK)
        self.assertEqual(kind, "block")
        self.assertEqual(len(rows), 3)
        self.assertEqual(insiders.parse_file(BULK, kind="block")[0], "block")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "deals.csv")
            with open(path, "w") as fh, open(BLOCK) as src:
                fh.write(src.read())
            self.assertEqual(insiders.parse_file(path)[0], "bulk")   # nothing says block: bulk is the default

    def test_index_cli_text_and_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = run(["index", "--file", BULK, "--file", BLOCK] + no_lists(tmp))
            self.assertEqual(code, 0)
            self.assertIn("2026-10-29  RELIANCE   BUY   7,250,000 at Rs 1,171.50 avg = Rs 849.34 cr, "
                          "SAMPLE ALPHA CAPITAL FUND", out)
            self.assertIn("      block deal", out)
            self.assertIn("Not inside information, not financial advice.", out)
            self.assertNotIn(chr(0x2014), out)
            code, out, _ = run(["index", "--file", BULK, "--symbol", "infy,TCS", "--json"] + no_lists(tmp))
            self.assertEqual(code, 0)
            self.assertEqual([r["symbol"] for r in json.loads(out)], ["INFY", "TCS"])
            code, out, _ = run(["index", "--file", BULK, "--since", "2026-10-30"] + no_lists(tmp))
            self.assertEqual(code, 1)
            self.assertIn("No matching rows", out)


class Alerts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state", "insiders.json")
        self.pf = os.path.join(self.tmp.name, "portfolio.json")
        with open(self.pf, "w") as fh:
            json.dump({"cash_inr": 90000.0, "positions": {"RELIANCE": {"qty": 64, "avg": 1171.6}}, "paper": True}, fh)
        self.wl = os.path.join(self.tmp.name, "watchlist.json")
        with open(self.wl, "w") as fh:
            json.dump({"created_by": "trader-scanner", "hits": [{"symbol": "INFY"}, {"symbol": "RELIANCE"}]}, fh)
        self.base = ["--state", self.state, "--now", NOW, "--portfolio", self.pf, "--watchlist", self.wl]

    def tearDown(self):
        self.tmp.cleanup()

    def test_alerts_once_and_flags_your_symbols(self):
        code, out, _ = run(["alert", "--file", INSIDER] + self.base)
        self.assertEqual(code, 0)
        self.assertEqual(out.count("NEW "), 8)
        self.assertIn("<- RELIANCE is in your portfolio", out)   # the portfolio wins over the watchlist
        self.assertIn("<- INFY is in your watchlist", out)
        self.assertNotIn("TCS is in your", out)
        self.assertIn("told the company 4 trading days later (LATE: over 2)", out)
        self.assertIn("on NSE 4 trading days after that (LATE: over 2)", out)
        with open(self.state) as fh:
            saved = json.load(fh)
        self.assertEqual(len(saved["seen"]), 8)
        self.assertEqual(saved["last_check"], "2026-10-29T18:30:00+05:30")
        code, out, _ = run(["alert", "--file", INSIDER] + self.base)
        self.assertEqual(code, 1)
        self.assertIn("Nothing new since 2026-10-29T18:30:00+05:30.", out)

    def test_a_new_row_in_a_fresh_download_is_new(self):
        run(["alert", "--file", BULK] + self.base)
        with open(BULK) as fh:
            lines = fh.read().splitlines()
        path = os.path.join(self.tmp.name, "bulk-deals-later.csv")
        with open(path, "w") as fh:
            fh.write("\n".join(lines + ['30-OCT-2026,TCS,Tata Consultancy Services Limited,SAMPLE IOTA FUND,BUY,"900,000",2480.00,-']) + "\n")
        code, out, _ = run(["alert", "--file", path, "--json"] + self.base)
        self.assertEqual(code, 0)
        new = json.loads(out)["new"]
        self.assertEqual(len(new), 1)
        self.assertEqual((new[0]["symbol"], new[0]["client"], new[0]["flag"]), ("TCS", "SAMPLE IOTA FUND", None))

    def test_three_files_in_one_alert_and_dry_run(self):
        code, out, _ = run(["alert", "--file", INSIDER, "--file", BULK, "--file", BLOCK, "--dry-run", "--json"]
                           + self.base)
        self.assertEqual(code, 0)
        new = json.loads(out)["new"]
        self.assertEqual(len(new), 17)
        self.assertEqual({r["kind"] for r in new}, {"insider", "bulk", "block"})
        self.assertEqual([r["flag"] for r in new if r["symbol"] == "INFY"][0], "watchlist")
        self.assertFalse(os.path.exists(self.state))

    def test_lists_shapes(self):
        lists = insiders.your_lists(self.pf, self.wl)
        self.assertEqual(lists, {"INFY": "watchlist", "RELIANCE": "portfolio"})
        with open(self.pf, "w") as fh:
            json.dump({"positions": [{"symbol": "TCS", "qty": 1}]}, fh)   # a list shape is read too
        self.assertEqual(insiders.your_lists(self.pf, "/nonexistent.json"), {"TCS": "portfolio"})

    def test_missing_file_is_a_plain_error(self):
        code, _, err = run(["alert", "--file", os.path.join(self.tmp.name, "nope.csv")] + self.base)
        self.assertEqual(code, 2)
        self.assertIn("insiders: ", err)
        self.assertIn("nope.csv", err)
        code, _, _ = run([])
        self.assertEqual(code, 2)


class WithKiteAndFeeds(unittest.TestCase):
    """A scratch cwd with a fresh session file; the feeds and Kite answer from the fixtures."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.env = os.environ.get("KITE_API_KEY")
        os.environ["KITE_API_KEY"] = "test-key"
        kite.save_session({"access_token": "t0k", "user_id": "AB1234"}, kite.now_ist())
        self.net = FakeNet()
        self.saved = (insiders._urlopen, kite._urlopen, kite._sleep, dict(kite._last_call))
        insiders._urlopen = self.net
        kite._urlopen, kite._sleep = self.net, lambda s: None
        kite._last_call.clear()

    def tearDown(self):
        insiders._urlopen, kite._urlopen, kite._sleep, last = self.saved
        kite._last_call.clear()
        kite._last_call.update(last)
        if self.env is None:
            os.environ.pop("KITE_API_KEY", None)
        else:
            os.environ["KITE_API_KEY"] = self.env
        os.chdir(self.cwd)
        self.tmp.cleanup()



class DealNews(WithKiteAndFeeds):
    """`news SYM`: Google News through the identified client, the company name through Kite."""

    def test_parse_and_tag(self):
        items = insiders.parse_rss(raw(DIR, "google-deals-RELIANCE.xml"), "RELIANCE")
        self.assertEqual(len(items), 30)
        self.assertEqual(sum(1 for i in items if i["tags"]), 22)
        self.assertEqual(sum(1 for i in items if "bulk-deal" in i["tags"]), 12)
        it = next(i for i in items if i["title"].startswith("Ramco Systems"))
        self.assertEqual(it["source"], "Business Standard")
        self.assertEqual(it["tags"], ["bulk-deal", "promoter"])
        self.assertEqual(it["time"], ist("2026-02-04T13:30:00"))   # 08:00 GMT

    def test_news_cli_window_and_query(self):
        code, out, err = run(["news", "reliance", "--hours", "8760", "--now", NOW])
        self.assertEqual(code, 0, err)
        self.assertEqual(len([l for l in out.splitlines() if l.startswith("202")]), 4)   # 7 name Reliance, 4 in the year
        self.assertIn("2025-10-30 12:30 IST  ", out)   # the RIL block deal, just inside the window
        self.assertIn("2026-07-17 12:30 IST  CNBC TV18        Reliance Industries promoters raise stake", out)
        self.assertIn("tags: promoter", out)
        self.assertNotIn("Molbio", out)   # Google's OR query returns it; it does not name the company
        self.assertIn("Checked 2026-10-29 18:30 IST", out)
        self.assertIn("Not financial advice", out)
        google = [u for u in self.net.urls if "news.google.com" in u][0]
        self.assertIn(urllib.parse.quote('"Reliance Industries" (bulk deal OR block deal OR insider OR promoter stake)'),
                      google)
        self.assertIn("hl=en-IN&gl=IN&ceid=IN:en", google)
        # the default week has nothing in the fixture: exit 1 and a plain line
        code, out, _ = run(["news", "RELIANCE", "--now", NOW, "--json"])
        self.assertEqual(code, 1)
        data = json.loads(out)
        self.assertEqual(data["items"], [])
        self.assertEqual(data["company"], "Reliance Industries")

    def test_no_session_and_a_feed_down(self):
        os.remove(os.path.join("trader", "kite_session.json"))
        code, out, err = run(["news", "RELIANCE", "--now", NOW])
        self.assertEqual(code, 1)
        self.assertIn("login.py", err)
        self.assertEqual(self.net.urls, [])
        kite.save_session({"access_token": "t0k"}, kite.now_ist())
        self.net.google_code = 403
        code, _, err = run(["news", "RELIANCE", "--now", NOW])
        self.assertEqual(code, 2)
        self.assertIn("HTTP 403 for https://news.google.com/rss/search", err)
        self.assertEqual(len([u for u in self.net.urls if "news.google.com" in u]), 1)   # no retry past a 403

    def test_unknown_symbol_searches_by_symbol(self):
        code, out, err = run(["news", "ZZZZ", "--now", NOW, "--hours", "8760"])
        self.assertEqual(code, 1, err)   # nothing in the feed names ZZZZ
        self.assertIn("note: Kite: ZZZZ is not listed on NSE", out)
        self.assertIn("No bulk deal, block deal, insider or promoter-stake headline for ZZZZ", out)
        self.assertIn(urllib.parse.quote('"ZZZZ" (bulk deal'), self.net.urls[-1])


class Today(WithKiteAndFeeds):
    """`today`: insider / SAST / pledge filings on BSE's announcements feed (the fixture excerpt)."""

    def test_filings_from_the_feed(self):
        rows = insiders.insider_filings(insiders.parse_bse(raw(NEWS_DIR, "bse-announcements.xml")))
        self.assertEqual([(r["company"], r["time"]) for r in rows],
                         [("Camlin Fine Sciences Ltd", "15:50"), ("SKM Egg Products Export (India) Ltd", "11:38")])
        self.assertTrue(rows[0]["title"].startswith("The Exchange has received Disclosure under Regulation 31(1)"))
        self.assertEqual(rows[0]["date"], "2026-10-29")
        self.assertTrue(rows[0]["link"].endswith(".pdf"))
        # board-meeting notices cite the insider-trading code and are not disclosures
        self.assertNotIn("Ovobel Foods Ltd", [r["company"] for r in rows])
        only = insiders.insider_filings(insiders.parse_bse(raw(NEWS_DIR, "bse-announcements.xml")),
                                        {"CAMLINFINE": "Camlin Fine Sciences", "INFY": "Infosys"})
        self.assertEqual([r["symbol"] for r in only], ["CAMLINFINE"])

    def test_today_cli_alerts_once_and_shares_the_cache(self):
        state = os.path.join(self.tmp.name, "insiders.json")
        code, out, err = run(["today", "--now", NOW, "--state", state])
        self.assertEqual(code, 0, err)
        self.assertIn("NEW 2026-10-29 15:50 IST  Camlin Fine Sciences Ltd (532834)", out)
        self.assertIn("NEW 2026-10-29 11:38 IST  SKM Egg Products Export (India) Ltd (532143)", out)
        self.assertIn("https://www.bseindia.com/xml-data/corpfiling/AttachLive/", out)
        self.assertIn("not financial advice", out)
        self.assertEqual(self.net.urls, [insiders.BSE_ANNOUNCEMENTS])   # no Kite call without --symbol
        self.assertTrue(os.path.isfile(os.path.join("trader", "cache", "bse-announcements-2026-10-29-1830.xml")))
        code, out, _ = run(["today", "--now", NOW, "--state", state])
        self.assertEqual(code, 1)
        self.assertIn("No new insider, SAST or pledge filing", out)
        self.assertEqual(len(self.net.urls), 1)   # the cache answered the second run
        # --symbol narrows to your companies through Kite's names: nothing for RELIANCE today
        code, out, _ = run(["today", "--now", NOW, "--state", state, "--symbol", "RELIANCE", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["new"], [])
        self.assertTrue(any("/instruments/NSE" in u for u in self.net.urls))


if __name__ == "__main__":
    unittest.main()
