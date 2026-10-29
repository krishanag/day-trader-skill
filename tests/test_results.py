"""trader-results, offline.

The XBRL fixtures (sample-SAMPLE-*.xml) are SAMPLE instance documents for a
fictional company, SAMPLE INDUSTRIES LIMITED, written in the layout of the
BSE / NSE Ind AS financial-results taxonomy (in-bse-fin, in-capmkt), because
neither exchange serves a real XBRL file to a plainly identified script. The
RSS fixtures (Google News, a cut of BSE's announcements feed) and the Kite
instrument rows are real, saved on 2026-10-29. The bse-pdf/ fixtures are six
real results PDFs of that day from BSE's feed (see the fixtures README), with
their pdftotext -layout text: the PDF tests parse the saved text and run
without poppler; one test runs pdftotext on a saved PDF when it is installed.
"""

import datetime
import glob
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

results = load("trader-results", "results")
kite = results.kite
nse = results.nse
DIR = os.path.join(FIX, "trader-results")
Q3FY26 = os.path.join(DIR, "sample-SAMPLE-Q3FY26.xml")
Q1FY27 = os.path.join(DIR, "sample-SAMPLE-Q1FY27.xml")
Q2FY27 = os.path.join(DIR, "sample-SAMPLE-Q2FY27.xml")
NOW = "2026-10-29 10:15"
D = datetime.date


def ist(text):
    return datetime.datetime.fromisoformat(text).replace(tzinfo=nse.IST)


def run(args, stdin=None):
    out, err = io.StringIO(), io.StringIO()
    saved = results.sys.stdin
    if stdin is not None:
        results.sys.stdin = io.StringIO(stdin)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = results.main(args)
    finally:
        results.sys.stdin = saved
    return code, out.getvalue(), err.getvalue()


def instance(xml, path="inline.xml"):
    return results.parse_instance(xml, path)


class FakeResponse(io.BytesIO):
    def __init__(self, body, ctype="application/xml"):
        super().__init__(body.encode("utf-8") if isinstance(body, str) else body)
        self.headers = {"Content-Type": ctype}
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeNet:
    """Records requests and answers them from a queue (or a function of the URL)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []
        self.by_url = None

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        answer = self.by_url(req.full_url) if self.by_url else self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def http_error(code):
    return urllib.error.HTTPError("https://example.invalid/x", code, "blocked", {}, io.BytesIO(b""))


def fixture(name):
    with open(os.path.join(DIR, name), encoding="utf-8") as fh:
        return fh.read()


# a minimal instance builder for the edge cases
HEAD = ('<?xml version="1.0"?><xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
        'xmlns:xbrldi="http://xbrl.org/2006/xbrldi" xmlns:in-bse-fin="urn:fin" xmlns:in-capmkt="urn:cap">')


def ctx(cid, start, end, member="ConsolidatedMember"):
    seg = ('<xbrli:segment><xbrldi:explicitMember dimension="in-bse-fin:ConsolidatedAndStandaloneAxis">'
           'in-bse-fin:%s</xbrldi:explicitMember></xbrli:segment>' % member) if member else ""
    return ('<xbrli:context id="%s"><xbrli:entity><xbrli:identifier scheme="s">1</xbrli:identifier>%s'
            '</xbrli:entity><xbrli:period><xbrli:startDate>%s</xbrli:startDate><xbrli:endDate>%s'
            '</xbrli:endDate></xbrli:period></xbrli:context>' % (cid, seg, start, end))


def fact(name, cid, value, prefix="in-bse-fin"):
    return '<%s:%s contextRef="%s" unitRef="INR" decimals="2">%s</%s:%s>' % (prefix, name, cid, value, prefix, name)


def small_instance(rounding="Crores", revenue_name="RevenueFromOperations", member="ConsolidatedMember",
                   other_income=10, pbt=100, tax=25, extra=""):
    parts = [HEAD, ctx("OneD", "2026-07-01", "2026-09-30", member), ctx("TwoD", "2026-04-01", "2026-06-30", member),
             ctx("ThreeD", "2025-07-01", "2025-09-30", member)]
    if rounding:
        parts.append('<in-capmkt:LevelOfRoundingUsedInFinancialStatements contextRef="OneD">%s'
                     '</in-capmkt:LevelOfRoundingUsedInFinancialStatements>' % rounding)
    parts.append('<in-capmkt:NameOfTheCompany contextRef="OneD">SMALL LIMITED</in-capmkt:NameOfTheCompany>')
    for cid, scale in (("OneD", 1.0), ("TwoD", 0.9), ("ThreeD", 0.8)):
        parts += [fact(revenue_name, cid, 1000 * scale), fact("OtherIncome", cid, other_income * scale),
                  fact("FinanceCosts", cid, 20 * scale),
                  fact("DepreciationDepletionAndAmortisationExpense", cid, 30 * scale),
                  fact("ProfitBeforeTax", cid, pbt * scale), fact("TaxExpense", cid, tax * scale),
                  fact("ProfitLossForPeriod", cid, (pbt - tax) * scale),
                  fact("DilutedEarningsLossPerShareFromContinuingOperations", cid, 7.5 * scale)]
    parts.append(extra)
    parts.append("</xbrli:xbrl>")
    return "".join(parts)


# ---------------------------------------------------------------- the quarters table

class OneFiling(unittest.TestCase):
    """The Q2 FY27 filing alone: this quarter, last quarter, a year earlier, the YTD columns, FY26."""

    def setUp(self):
        self.r = results.analyse([results.read_file(Q2FY27)])

    def test_latest_quarter_in_crore(self):
        q = self.r["rows"][0]
        self.assertEqual((q["fiscal"], q["end"], q["derived"]), ("Q2 FY27", "2026-09-30", False))
        self.assertEqual(q["revenue"], 1200.0)          # 1,20,000 lakh
        self.assertEqual(q["total_income"], 1224.0)
        self.assertEqual(q["ebitda"], 271.0)            # PBT 190 + finance 27 + depreciation 54
        self.assertEqual(q["ebitda_margin"], 22.6)
        self.assertEqual(q["pbt"], 190.0)
        self.assertEqual(q["pat"], 142.5)
        self.assertEqual(q["pat_margin"], 11.9)
        self.assertEqual((q["eps_basic"], q["eps_diluted"]), (14.25, 14.2))

    def test_growth_against_the_quarter_before_and_a_year_earlier(self):
        q = self.r["rows"][0]
        self.assertEqual(q["revenue_qoq"], 14.3)        # vs 1,050
        self.assertEqual(q["revenue_yoy"], 22.4)        # vs 980
        self.assertEqual(q["pat_qoq"], 18.8)            # vs 120
        self.assertEqual(q["pat_yoy"], 31.0)            # vs 108.75
        self.assertEqual(q["eps_yoy"], 31.1)            # 14.20 vs 10.83
        self.assertEqual(q["total_income_yoy"], 22.4)

    def test_three_quarters_newest_first(self):
        self.assertEqual([r["fiscal"] for r in self.r["rows"]], ["Q2 FY27", "Q1 FY27", "Q2 FY26"])
        self.assertIsNone(self.r["rows"][1]["revenue_qoq"])   # Q4 FY26 is not in this file

    def test_ttm_from_the_year_to_date_columns(self):
        t = self.r["ttm"]
        self.assertEqual(t["revenue"], 4350.0)          # FY26 4,000 + H1 FY27 2,250 - H1 FY26 1,900
        self.assertEqual(t["pat"], 502.5)
        self.assertEqual(t["eps_diluted"], 50.05)
        self.assertEqual(t["method"], "FY26 plus H1 FY27 minus H1 FY26, from the year-to-date columns")

    def test_header_facts_come_from_the_file(self):
        self.assertEqual(self.r["entity"], "SAMPLE INDUSTRIES LIMITED")
        self.assertEqual(self.r["filed_symbol"], "SAMPLE")
        self.assertEqual(self.r["basis"], "consolidated")
        self.assertEqual(self.r["scale"], ["lakhs"])
        self.assertEqual(self.r["audited"], "Unaudited")
        self.assertEqual(self.r["concepts"]["revenue"], ["RevenueFromOperations"])

    def test_what_changed_in_words(self):
        lines = results.changes(self.r)
        self.assertIn("revenue Rs 1,200.00 crore (+22.4% vs a year earlier, +14.3% vs the quarter before), "
                      "PAT Rs 142.50 crore (+31.0% yoy, +18.8% qoq), diluted EPS Rs 14.20 (+31.1% yoy)", lines[0])
        self.assertIn("EBITDA margin (derived) 22.6% (-0.4 points vs the quarter before).", lines)
        self.assertIn("PAT margin 11.9% (+0.5 points vs the quarter before).", lines)
        self.assertTrue(any(l.startswith("Q1 FY27 carried exceptional items of Rs -15.00 crore") for l in lines))

    def test_render_says_basis_scale_and_source(self):
        text = results.render("SAMPLE", self.r)
        self.assertIn("consolidated. Rs crore except EPS (filed in lakhs, converted)", text)
        self.assertIn("Q2 FY27  2026-09-30      1,200.00     1,224.00     271.00     190.00     142.50", text)
        self.assertIn("+14.3%   +22.4%   +18.8%   +31.0%   +31.1%     22.6%   11.9%", text)
        self.assertIn("EBITDA is derived: PBT + finance costs + depreciation", text)
        self.assertNotIn(chr(0x2014), text)

    def test_render_warns_when_the_file_is_another_company(self):
        text = results.render("RELIANCE", self.r)
        self.assertIn("NOTE: the file names SAMPLE, not RELIANCE", text)


class SeveralFilings(unittest.TestCase):
    def test_eight_quarters_from_three_filings_with_a_derived_q4(self):
        r = results.analyse([results.read_file(p) for p in (Q2FY27, Q1FY27, Q3FY26)], quarters=8)
        self.assertEqual([x["fiscal"] for x in r["rows"]],
                         ["Q2 FY27", "Q1 FY27", "Q4 FY26", "Q3 FY26", "Q2 FY26", "Q1 FY26", "Q4 FY25", "Q3 FY25"])
        q4 = r["rows"][6]
        self.assertTrue(q4["derived"])                  # FY25 3,600 minus 9M FY25 2,650
        self.assertEqual((q4["revenue"], q4["pbt"], q4["eps_diluted"]), (950.0, 140.0, 10.4))
        self.assertFalse(r["rows"][2]["derived"])       # Q4 FY26 is reported in the Q1 FY27 file
        self.assertEqual(r["rows"][1]["revenue_qoq"], -4.5)
        self.assertEqual(r["rows"][1]["pat_yoy"], 18.5)
        self.assertEqual(r["files"], ["sample-SAMPLE-Q2FY27.xml", "sample-SAMPLE-Q1FY27.xml",
                                      "sample-SAMPLE-Q3FY26.xml"])

    def test_ttm_from_four_quarters_matches_the_ytd_identity(self):
        r = results.analyse([results.read_file(p) for p in (Q2FY27, Q1FY27, Q3FY26)])
        self.assertEqual((r["ttm"]["revenue"], r["ttm"]["pat"], r["ttm"]["eps_diluted"]), (4350.0, 502.5, 50.05))
        self.assertEqual(r["ttm"]["method"], "four quarters, Q3 FY26 to Q2 FY27")

    def test_q4_is_the_year_minus_nine_months_when_not_reported(self):
        r = results.analyse([results.read_file(Q3FY26), results.read_file(Q2FY27)], quarters=8)
        q4 = next(x for x in r["rows"] if x["end"] == "2026-03-31")
        self.assertTrue(q4["derived"])
        self.assertEqual((q4["revenue"], q4["pat"], q4["eps_diluted"]), (1100.0, 127.5, 12.7))
        self.assertIn("derived", results.render("SAMPLE", r))

    def test_later_filing_restates_an_earlier_one(self):
        early = small_instance()
        late = small_instance().replace('<in-capmkt:NameOfTheCompany', '<in-capmkt:DateOfEndOfReportingPeriod '
                                        'contextRef="OneD">2026-12-31</in-capmkt:DateOfEndOfReportingPeriod>'
                                        '<in-capmkt:NameOfTheCompany')
        late = late.replace(fact("RevenueFromOperations", "OneD", 1000.0), fact("RevenueFromOperations", "OneD", 1111.0))
        r = results.analyse([instance(late, "late.xml"), instance(early, "early.xml")])
        self.assertEqual(r["rows"][0]["revenue"], 1111.0)


class BasisAndScale(unittest.TestCase):
    def test_standalone_on_request(self):
        r = results.analyse([results.read_file(Q2FY27)], basis="standalone")
        self.assertEqual(r["basis"], "standalone")
        self.assertEqual(r["rows"][0]["revenue"], 960.0)
        self.assertTrue(any("Standalone figures" in l for l in results.changes(r)))

    def test_consolidated_is_the_default_and_a_missing_basis_is_an_error(self):
        self.assertEqual(results.analyse([results.read_file(Q1FY27)])["basis"], "consolidated")
        with self.assertRaisesRegex(results.ResultsError, "no standalone figures.*consolidated"):
            results.analyse([results.read_file(Q1FY27)], basis="standalone")

    def test_a_file_without_the_dimension_is_read_as_is(self):
        r = results.analyse([instance(small_instance(member=None))])
        self.assertEqual(r["basis"], "not stated in the file")
        self.assertEqual(r["rows"][0]["revenue"], 1000.0)

    def test_crore_files_are_not_converted_and_rupee_files_are(self):
        self.assertEqual(results.analyse([instance(small_instance("Crores"))])["rows"][0]["revenue"], 1000.0)
        r = results.analyse([instance(small_instance("Actual").replace(">1000.0<", ">12345678900<"))])
        self.assertEqual(r["rows"][0]["revenue"], 1234.57)
        self.assertEqual(results.analyse([instance(small_instance("Millions"))])["rows"][0]["revenue"], 100.0)

    def test_missing_or_odd_rounding_asks_for_scale(self):
        with self.assertRaisesRegex(results.ResultsError, "does not say its level of rounding.*--scale"):
            results.analyse([instance(small_instance(rounding=None))])
        with self.assertRaisesRegex(results.ResultsError, "unknown level of rounding 'dozens'"):
            results.analyse([instance(small_instance(rounding="Dozens"))])
        r = results.analyse([instance(small_instance(rounding=None))], scale="lakh")
        self.assertEqual((r["rows"][0]["revenue"], r["scale"]), (10.0, ["lakh"]))
        self.assertEqual(r["rows"][0]["eps_diluted"], 7.5)   # EPS is never scaled

    def test_fiscal_labels_for_an_april_year_and_a_january_year(self):
        self.assertEqual(results.fiscal_label("2026-09-30"), "Q2 FY27")
        self.assertEqual(results.fiscal_label("2026-03-31"), "Q4 FY26")
        self.assertEqual(results.fiscal_label("2025-12-31"), "Q3 FY26")
        self.assertEqual(results.fiscal_label("2026-06-30", fy_start_month=1), "Q2 FY26")


class Tolerance(unittest.TestCase):
    def test_elements_match_by_local_name_whatever_the_prefix(self):
        xml = fixture("sample-SAMPLE-Q2FY27.xml").replace("in-bse-fin:", "ind:").replace("in-capmkt:", "gen:")
        xml = xml.replace('xmlns:in-bse-fin=', 'xmlns:ind=').replace('xmlns:in-capmkt=', 'xmlns:gen=')
        r = results.analyse([instance(xml)])
        self.assertEqual(r["rows"][0]["revenue"], 1200.0)
        self.assertEqual(r["basis"], "consolidated")

    def test_a_bank_filing_uses_interest_earned_and_leaves_ebitda_blank(self):
        r = results.analyse([instance(small_instance(revenue_name="InterestEarned"))])
        self.assertEqual(r["concepts"]["revenue"], ["InterestEarned"])
        self.assertIsNone(r["rows"][0]["ebitda"])
        self.assertEqual(r["rows"][0]["revenue"], 1000.0)
        self.assertTrue(any("bank or lender" in l for l in results.changes(r)))

    def test_the_wrong_kind_of_xml_lists_what_it_found(self):
        xml = HEAD + ctx("c1", "2026-07-01", "2026-09-30") + fact("ShareholdingOfPromoter", "c1", 50) + "</xbrli:xbrl>"
        with self.assertRaisesRegex(results.ResultsError, "not an Ind AS financial-results instance.*ShareholdingOfPromoter"):
            results.analyse([instance(xml, "shp.xml")])
        with self.assertRaisesRegex(results.ResultsError, "no XBRL contexts or numeric facts"):
            instance("<root><a>1</a></root>", "junk.xml")
        with self.assertRaisesRegex(results.ResultsError, "not well-formed XML"):
            instance("<xbrl><unclosed>", "broken.xml")
        with self.assertRaisesRegex(results.ResultsError, "no such file"):
            results.read_file("/nonexistent/results.xml")

    def test_one_offs_are_flagged(self):
        r = results.analyse([instance(small_instance(other_income=40))])
        self.assertTrue(any(l.startswith("Other income is 40% of PBT") for l in results.changes(r)))
        r = results.analyse([instance(small_instance(tax=-5))])
        self.assertTrue(any("PAT is above PBT" in l for l in results.changes(r)))
        r = results.analyse([instance(small_instance(extra=fact("ExceptionalItemsBeforeTax", "OneD", -12.5)))])
        self.assertTrue(any(l.startswith("Exceptional items of Rs -12.50 crore are inside PBT") for l in results.changes(r)))


# ---------------------------------------------------------------- the pasted transcript

TRANSCRIPT = """Good evening everyone, and thank you for joining. Revenue grew 12% to Rs 1,200 crore this
quarter. EBITDA margin expanded 180 bps to 22.6% on better pricing. We expect double-digit growth
for the full year. Demand in rural markets remains weak. Capex for FY27 will be about Rs 300 crore,
funded from cash flows. We will not be able to comment on the pending litigation. The exceptional
item is a one-time impairment of the Pune plant. Thank you."""


class Summary(unittest.TestCase):
    def test_topics_are_pulled_from_the_text(self):
        s = results.summarise(TRANSCRIPT)
        self.assertEqual(s["topics"]["guidance"][0], "We expect double-digit growth for the full year.")
        self.assertEqual(s["topics"]["margins"], ["EBITDA margin expanded 180 bps to 22.6% on better pricing."])
        self.assertEqual(s["topics"]["demand"], ["Demand in rural markets remains weak."])
        self.assertIn("Capex for FY27 will be about Rs 300 crore, funded from cash flows.", s["topics"]["balance sheet"])
        self.assertEqual(s["topics"]["one-offs"], ["The exceptional item is a one-time impairment of the Pune plant."])
        self.assertEqual(s["topics"]["declined to answer"], ["We will not be able to comment on the pending litigation."])
        self.assertEqual(s["numbers"][0], "Revenue grew 12% to Rs 1,200 crore this quarter.")
        self.assertIn("not an understanding of it", s["caveat"])

    def test_cli_reads_stdin_and_json(self):
        code, out, err = run(["summary", "--paste"], stdin=TRANSCRIPT)
        self.assertEqual(code, 0, err)
        self.assertIn("Declined to answer:\n  - We will not be able to comment", out)
        self.assertIn("keyword pass", out)
        code, out, _ = run(["--json", "summary"], stdin=TRANSCRIPT)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["sentences"], 8)

    def test_empty_paste_is_exit_2(self):
        code, _, err = run(["summary", "--paste"], stdin="   \n")
        self.assertEqual(code, 2)
        self.assertIn("nothing to summarise", err)

    def test_text_without_the_topics_says_so(self):
        text = "The weather in Mumbai was pleasant on Tuesday morning. Traffic was lighter than usual."
        self.assertIn("No sentence on guidance", results.render_summary(results.summarise(text)))


# ---------------------------------------------------------------- the calendar

class Dates(unittest.TestCase):
    def test_dates_named_in_headlines(self):
        ref = D(2026, 10, 28)
        self.assertEqual(results.dates_in("Q2 Results on October 30", ref), ["2026-10-30"])
        self.assertEqual(results.dates_in("sets Oct 30 for Q2 FY27 results", ref), ["2026-10-30"])
        self.assertEqual(results.dates_in("results on 30 October 2026", ref), ["2026-10-30"])
        self.assertEqual(results.dates_in("board meeting on 06/11/2026", ref), ["2026-11-06"])
        self.assertEqual(results.dates_in("record date announcement today", ref), ["2026-10-28"])
        self.assertEqual(results.dates_in("Infosys Q2 Results on Friday", ref), ["2026-10-30"])
        self.assertEqual(results.dates_in("results on Jan 5", D(2026, 12, 20)), ["2027-01-05"])
        self.assertEqual(results.dates_in("Q2 profit triples", ref), [])

    def test_results_quarter_by_month(self):
        self.assertEqual(results.results_quarter(D(2026, 10, 29)), "Q2")
        self.assertEqual(results.results_quarter(D(2026, 5, 2)), "Q4")
        self.assertEqual(results.results_quarter(D(2026, 1, 15)), "Q3")

    def test_company_names_for_the_query_and_the_bse_match(self):
        self.assertEqual(results.clean_name("TATA CONSULTANCY SERV LT"), "Tata Consultancy Serv")
        self.assertEqual(results.clean_name("INTRASOFT TECH."), "Intrasoft Tech")
        self.assertEqual(results.clean_name("RELIANCE INDUSTRIES"), "Reliance Industries")
        self.assertTrue(results.name_matches("TATA CONSULTANCY SERV LT", "Tata Consultancy Services Ltd (532540)"))
        self.assertFalse(results.name_matches("TATA CONSULTANCY SERV LT", "Tata Consumer Products Ltd (500800)"))
        self.assertTrue(results.name_matches("INTRASOFT TECH.", "Intrasoft Technologies Ltd (533181)"))
        self.assertFalse(results.name_matches("RELIANCE INDUSTRIES", "Reliance Chemotex Industries Ltd (503162)"))
        self.assertFalse(results.name_matches("", "Anything Ltd"))

    def test_pubdates_from_google_and_bse_land_in_ist(self):
        self.assertEqual(results.parse_pubdate("Wed, 28 Oct 2026 03:59:08 GMT"), ist("2026-10-28T09:29:08"))
        self.assertEqual(results.parse_pubdate("29-Oct-2026 18:50:32"), ist("2026-10-29T18:50:32"))
        self.assertIsNone(results.parse_pubdate("yesterday"))


class Horizon(unittest.TestCase):
    def test_trading_days_skip_weekends_and_diwali(self):
        self.assertEqual(results.trading_days_between(D(2026, 10, 29), D(2026, 10, 30)), 1)
        self.assertEqual(results.trading_days_between(D(2026, 10, 29), D(2026, 11, 6)), 6)
        self.assertEqual(results.trading_days_between(D(2026, 10, 29), D(2026, 11, 11)), 8)   # Nov 10 is a holiday
        self.assertEqual(results.trading_days_between(D(2026, 10, 29), D(2026, 10, 23)), -4)

    def test_the_four_answers(self):
        now = ist("2026-10-29T10:15")
        kind, n, s = results.horizon(D(2026, 10, 29), now)
        self.assertEqual((kind, n), ("today", 0))
        self.assertIn("The result is TODAY", s)
        kind, n, s = results.horizon(D(2026, 10, 30), now)
        self.assertEqual((kind, n), ("this_week", 1))
        self.assertIn("1 trading day away (this week)", s)
        self.assertIn("the stock can open past it", s)
        kind, n, s = results.horizon(D(2026, 11, 4), now)
        self.assertEqual((kind, n), ("this_week", 4))     # next week, but inside five trading days
        kind, n, s = results.horizon(D(2026, 11, 6), now)
        self.assertEqual((kind, n), ("later", 6))
        self.assertIn("outside a day trade's horizon", s)
        kind, n, s = results.horizon(D(2026, 10, 23), now)
        self.assertEqual((kind, n), ("past", -4))
        self.assertIn("was 4 trading days ago", s)


class Feeds(unittest.TestCase):
    def test_google_headlines_name_the_date(self):
        items = results.parse_rss(fixture("googlenews-RELIANCE-results.xml"), "Google News")
        self.assertEqual(len(items), 33)
        rows = results.headline_rows(items, D(2026, 10, 29))
        named = {r["title"]: r for r in rows if r["dates"]}
        self.assertEqual(named["Reliance Industries Shares Fall 2% After Jio Platforms Listing; Q2 Results on October 30"]["dates"],
                         ["2026-10-30"])
        self.assertEqual(rows[0]["published"][:16], "2026-10-29T11:37")   # newest first, IST
        self.assertEqual(rows[0]["publisher"], "ET Now")
        self.assertEqual(results.tally(rows, D(2026, 10, 29)), {"2026-10-30": 3})

    def test_headlines_that_say_the_result_is_out(self):
        rows = results.headline_rows(results.parse_rss(fixture("googlenews-INFY-board.xml"), "Google News"),
                                     D(2026, 10, 29))
        kinds = {r["title"]: r["kind"] for r in rows}
        self.assertEqual(kinds["Infosys: Reports Q2 FY27 Revenue Growth of 1.5%, Declares ₹26 Dividend"], "out")
        self.assertEqual(kinds["Infosys Q2 results: Check IT firm's earnings date, plans to expand Indore development "
                               "centre; stock to be in focus"], "ahead")

    def test_bse_feed_board_meeting_intimation_and_result_out(self):
        items = results.parse_rss(fixture("bse-announcements.xml"), "BSE")
        self.assertEqual(len(items), 11)
        today = D(2026, 10, 29)
        isft = results.bse_rows(items, "INTRASOFT TECH.", today)
        self.assertEqual(len(isft), 1)
        self.assertEqual((isft[0]["kind"], isft[0]["dates"], isft[0]["scrip"]), ("ahead", ["2026-11-06"], "533181"))
        coal = results.bse_rows(items, "COAL INDIA", today)
        self.assertEqual([r["kind"] for r in coal], ["out"])
        self.assertEqual(coal[0]["dates"], ["2026-10-29"])
        self.assertIn("Investor Presentation on Unaudited financial results", coal[0]["text"])
        self.assertEqual(results.bse_rows(items, "RELIANCE INDUSTRIES", today), [])   # a strike-off notice is not a result

    def test_broken_feed_is_an_error_not_a_crash(self):
        with self.assertRaisesRegex(results.ResultsError, "not well-formed"):
            results.parse_rss("<rss><channel>", "BSE")


class CalendarOffline(unittest.TestCase):
    def test_reliance_most_mentioned_date_is_inside_the_week(self):
        cal = results.calendar("RELIANCE", "RELIANCE INDUSTRIES", ist("2026-10-29T10:15"), offline=DIR)
        self.assertEqual(cal["likely"]["date"], "2026-10-30")
        self.assertEqual(cal["likely"]["mentions"], 3)
        self.assertEqual(cal["likely"]["horizon"], "this_week")
        self.assertEqual(cal["news"]["queries"][0], '"Reliance Industries" Q2 results date')
        self.assertEqual(cal["bse"]["items"], [])
        self.assertEqual(len(cal["problems"]), 1)   # no board-query fixture saved for RELIANCE
        self.assertIn("offline: no file", cal["problems"][0])

    def test_bse_intimation_outweighs_headlines(self):
        cal = results.calendar("ISFT-BE", "INTRASOFT TECH.", ist("2026-10-29T10:15"), offline=DIR, use_news=False)
        self.assertEqual(cal["dates"], {"2026-11-06": 3})
        self.assertEqual(cal["likely"]["horizon"], "later")
        self.assertIsNone(cal["news"])

    def test_render_carries_the_caveat_and_the_sources(self):
        cal = results.calendar("RELIANCE", "RELIANCE INDUSTRIES", ist("2026-10-29T10:15"), offline=DIR)
        text = results.render_calendar(cal)
        self.assertTrue(text.startswith("RELIANCE INDUSTRIES (RELIANCE): the next result"))
        self.assertIn("BSE announcements today: nothing from this company", text)
        self.assertIn("Headlines (Google News, newest first, IST):", text)
        self.assertIn("Q2 Results on October 30 (India Infoline) -> 2026-10-30", text)
        self.assertIn("Most mentioned date: 2026-10-30 (3 mentions). The result is 2026-10-30, 1 trading day away", text)
        self.assertIn("No official results calendar may be read by a script", text)
        self.assertIn("Confirm on NSE", text)
        self.assertNotIn(chr(0x2014), text)

    def test_nothing_found_points_at_the_exchange_page(self):
        cal = results.calendar("HDFCBANK", "HDFC BANK", ist("2026-10-29T10:15"), offline=DIR)
        self.assertIsNone(cal["likely"])
        self.assertIn("No date found", results.render_calendar(cal))
        self.assertIn("calendar HDFCBANK --date YYYY-MM-DD", results.render_calendar(cal))


class CalendarNetwork(unittest.TestCase):
    """The honest client: identified, no cookies, one request per source, no retry past a 403."""

    def setUp(self):
        self.net = FakeNet()
        self.saved = results._urlopen
        results._urlopen = self.net

    def tearDown(self):
        results._urlopen = self.saved

    def test_requests_are_identified_and_cookie_free(self):
        self.net.by_url = lambda url: FakeResponse(fixture("googlenews-RELIANCE-results.xml")) \
            if "news.google.com" in url else FakeResponse(fixture("bse-announcements.xml"))
        cal = results.calendar("RELIANCE", "RELIANCE INDUSTRIES", ist("2026-10-29T10:15"))
        self.assertEqual(len(self.net.requests), 3)
        for req in self.net.requests:
            self.assertEqual(req.get_header("User-agent"), results.USER_AGENT)
            self.assertFalse(req.has_header("Cookie"))
        urls = [r.full_url for r in self.net.requests]
        self.assertIn("https://news.google.com/rss/search?q=%22Reliance+Industries%22+Q2+results+date&hl=en-IN&gl=IN&ceid=IN%3Aen", urls)
        self.assertIn(results.BSE_ANNOUNCEMENTS, urls)
        self.assertEqual(cal["likely"]["date"], "2026-10-30")

    def test_a_403_is_reported_once_and_the_other_source_still_runs(self):
        self.net.by_url = lambda url: http_error(403) if "google" in url else FakeResponse(fixture("bse-announcements.xml"))
        cal = results.calendar("ISFT-BE", "INTRASOFT TECH.", ist("2026-10-29T10:15"))
        self.assertEqual([r.full_url for r in self.net.requests].count(results.BSE_ANNOUNCEMENTS), 1)
        self.assertEqual(len(self.net.requests), 3)   # two Google queries, one BSE: no retries
        self.assertEqual(len(cal["problems"]), 2)
        self.assertIn("HTTP 403 for https://news.google.com/rss/search", cal["problems"][0])
        self.assertEqual(cal["likely"]["date"], "2026-11-06")
        self.assertIn("(source down: Google News (results): HTTP 403", results.render_calendar(cal))

    def test_network_down_is_one_line_per_source(self):
        self.net.by_url = lambda url: urllib.error.URLError("no route")
        cal = results.calendar("RELIANCE", "RELIANCE INDUSTRIES", ist("2026-10-29T10:15"))
        self.assertEqual(len(cal["problems"]), 3)
        self.assertIn("could not reach https://www.bseindia.com/data/xml/announcements.xml: no route", cal["problems"][2])
        self.assertIsNone(cal["likely"])


# ---------------------------------------------------------------- the command line, Kite and the files

class Workdir(unittest.TestCase):
    """Runs in a temporary project folder so trader/ files land there."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.net = FakeNet()
        self.saved = (results._urlopen, kite._urlopen, kite._sleep, dict(kite._last_call),
                      os.environ.get("KITE_API_KEY"), os.environ.get("KITE_ENV_FILE"))
        results._urlopen = self.net
        kite._urlopen = self.net
        kite._sleep = lambda s: None
        kite._last_call.clear()
        os.environ.pop("KITE_API_KEY", None)
        os.environ["KITE_ENV_FILE"] = os.path.join(self.tmp.name, "no.env")

    def tearDown(self):
        os.chdir(self.cwd)
        results._urlopen, kite._urlopen, kite._sleep, last, key, env_file = self.saved
        kite._last_call.clear()
        kite._last_call.update(last)
        for name, value in (("KITE_API_KEY", key), ("KITE_ENV_FILE", env_file)):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.tmp.cleanup()

    def log_in(self):
        os.environ["KITE_API_KEY"] = "key"
        kite.save_session({"access_token": "tok", "user_id": "AB1234"}, ist("2026-10-29T08:55"))


class Cli(Workdir):
    def test_facts_json_and_quarters(self):
        code, out, err = run(["--json", "facts", "sample.ns", "--file", Q2FY27, "--file", Q1FY27, "--quarters", "4"])
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertEqual(doc["symbol"], "SAMPLE")
        self.assertEqual(len(doc["facts"]["rows"]), 4)
        self.assertEqual(doc["facts"]["rows"][0]["revenue"], 1200.0)
        self.assertEqual(doc["facts"]["concepts"]["eps_diluted"], ["DilutedEarningsLossPerShareFromContinuingOperations"])
        self.assertTrue(doc["changes"][0].startswith("Q2 FY27 (quarter ended 2026-09-30)"))
        self.assertEqual(len(self.net.requests), 0)   # no network at all for facts

    def test_facts_writes_results_json_and_metrics(self):
        code, out, err = run(["--now", NOW, "--write", "facts", "SAMPLE", "--file", Q2FY27, "--metrics", "trader/metrics.json"])
        self.assertEqual(code, 0, err)
        self.assertIn("no Kite session", err)
        self.assertIn("the company name comes from the file", err)
        self.assertIn("wrote trader/results.json", out)
        with open("trader/results.json") as fh:
            doc = json.load(fh)
        self.assertEqual((doc["created_by"], doc["symbol"], doc["as_of"]), ("trader-results", "SAMPLE", "2026-10-29T10:15:00+05:30"))
        self.assertEqual(doc["facts"]["ttm"]["pat"], 502.5)
        with open("trader/metrics.json") as fh:
            m = json.load(fh)
        self.assertEqual(m["as_of"], "2026-10-29")
        row = m["companies"][0]
        self.assertEqual((row["symbol"], row["revenue_ttm_cr"], row["eps_ttm"], row["revenue_growth_yoy"]),
                         ("SAMPLE", 4350.0, 50.05, 22.4))
        self.assertEqual(row["ebitda_margin"], 22.6)
        # a second run merges, it does not duplicate
        run(["--now", NOW, "facts", "SAMPLE", "--file", Q2FY27, "--metrics", "trader/metrics.json", "--name", "X"])
        with open("trader/metrics.json") as fh:
            self.assertEqual(len(json.load(fh)["companies"]), 1)

    def test_facts_takes_the_name_from_kite_when_logged_in(self):
        self.log_in()
        self.net.answers.append(FakeResponse(fixture("instruments-NSE.csv"), "text/csv"))
        code, out, err = run(["--now", NOW, "facts", "RELIANCE", "--file", Q2FY27])
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("RELIANCE INDUSTRIES (RELIANCE), consolidated."))
        self.assertIn("NOTE: the file names SAMPLE, not RELIANCE", out)
        self.assertEqual(len(self.net.requests), 1)
        self.assertIn("/instruments/NSE", self.net.requests[0].full_url)
        self.assertEqual(self.net.requests[0].get_header("Authorization"), "token key:tok")

    def test_bad_files_set_the_exit_code(self):
        code, out, err = run(["facts", "SAMPLE", "--file", Q2FY27, "--file", "/nonexistent.xml"])
        self.assertEqual(code, 1)        # a result printed, one file failed
        self.assertIn("no such file: /nonexistent.xml", err)
        self.assertIn("Q2 FY27", out)
        code, out, err = run(["facts", "SAMPLE", "--file", "/nonexistent.xml"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        bad = os.path.join(self.tmp.name, "shp.xml")
        with open(bad, "w") as fh:
            fh.write(HEAD + ctx("c1", "2026-07-01", "2026-09-30") + fact("ShareholdingOfPromoter", "c1", 50) + "</xbrli:xbrl>")
        code, _, err = run(["facts", "SAMPLE", "--file", bad])
        self.assertEqual(code, 2)
        self.assertIn("Elements found: ShareholdingOfPromoter", err)
        code, _, err = run(["facts", "SAMPLE", "--file", Q1FY27, "--standalone"])
        self.assertEqual(code, 2)
        self.assertIn("no standalone figures", err)

    def test_usage_errors(self):
        self.assertEqual(run([])[0], 2)
        code, _, err = run(["--now", "yesterday", "facts", "SAMPLE", "--file", Q2FY27])
        self.assertEqual(code, 2)
        self.assertIn("--now wants 'YYYY-MM-DD HH:MM'", err)
        code, _, err = run(["--offline", DIR, "calendar", "RELIANCE", "--name", "Reliance", "--date", "30-10-2026"])
        self.assertEqual(code, 2)
        self.assertIn("--date wants YYYY-MM-DD", err)

    def test_calendar_needs_a_kite_session_for_the_name(self):
        code, out, err = run(["--now", NOW, "--offline", DIR, "calendar", "RELIANCE"])
        self.assertEqual(code, 1)
        self.assertIn("KITE_API_KEY is not set", err)
        self.log_in()
        self.net.answers.append(FakeResponse(fixture("instruments-NSE.csv"), "text/csv"))
        code, out, err = run(["--now", NOW, "--offline", DIR, "calendar", "NSE:RELIANCE"])
        self.assertEqual(code, 1, err)   # the board-query fixture is missing: partial
        self.assertTrue(out.startswith("RELIANCE INDUSTRIES (RELIANCE): the next result"))
        self.assertIn("Most mentioned date: 2026-10-30", out)
        self.assertEqual(len(self.net.requests), 1)   # Kite once; the feeds came from --offline

    def test_calendar_with_a_date_answers_even_when_the_feeds_are_down(self):
        self.net.by_url = lambda url: http_error(403)
        code, out, err = run(["--now", NOW, "calendar", "COALINDIA", "--name", "COAL INDIA", "--date", "2026-10-29"])
        self.assertEqual(code, 0, err)
        self.assertIn("Your date 2026-10-29: The result is TODAY.", out)
        self.assertIn("(source down: BSE announcements: HTTP 403", out)

    def test_calendar_write_merges_into_the_facts_document(self):
        run(["--now", NOW, "--write", "facts", "SAMPLE", "--file", Q2FY27])
        code, out, err = run(["--now", NOW, "--write", "--offline", DIR, "calendar", "SAMPLE", "--name", "SAMPLE INDUSTRIES",
                              "--date", "2026-11-06"])
        self.assertEqual(code, 0, err)
        with open("trader/results.json") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["facts"]["rows"][0]["pat"], 142.5)
        self.assertEqual(doc["calendar"]["pinned"]["horizon"], "later")
        run(["--now", NOW, "--write", "--offline", DIR, "calendar", "INFY", "--name", "INFOSYS"])
        with open("trader/results.json") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["symbol"], "INFY")
        self.assertNotIn("facts", doc)   # another company: a fresh document

    def test_calendar_json(self):
        code, out, err = run(["--now", NOW, "--json", "--offline", DIR, "calendar", "ISFT-BE", "--name", "INTRASOFT TECH.", "--no-news"])
        self.assertEqual(code, 0, err)
        cal = json.loads(out)
        self.assertEqual(cal["likely"]["date"], "2026-11-06")
        self.assertEqual(cal["bse"]["items"][0]["scrip"], "533181")


# ---------------------------------------------------------------- the BSE results PDF

PDFDIR = os.path.join(DIR, "bse-pdf")


def pdf_fixture(prefix, ext=".txt"):
    paths = sorted(glob.glob(os.path.join(PDFDIR, prefix + "-*" + ext)))
    assert paths, "no %s fixture for %s" % (ext, prefix)
    return paths[0]


def pdf_text(prefix):
    with open(pdf_fixture(prefix), encoding="utf-8", errors="replace") as fh:
        return fh.read()


def parsed_pdf(prefix):
    return results.parse_results_pdf(pdf_text(prefix))


def columns_text(label, cells, width=12, label_width=40):
    """One aligned pdftotext-style line: a label and right-aligned cells."""
    return label.ljust(label_width) + "".join(str(c).rjust(width) for c in cells)


INDIGO_NAME = "INTERGLOBE AVIATION"
ESAF_NAME = "ESAF SMALL FINANCE BANK L"       # Kite's instrument name, suffix and all
LECSL_NAME = "LAKSHMI ELECTRICAL CONTROL SYSTEMS"


class PdfCells(unittest.TestCase):
    def test_numbers_in_indian_and_western_grouping_and_parentheses(self):
        c = results.classify_cell
        self.assertEqual(c("1,85,544.32"), ("num", 185544.32))
        self.assertEqual(c("233,638"), ("num", 233638.0))
        self.assertEqual(c("(2,090)"), ("num", -2090.0))
        self.assertEqual(c("-15.00"), ("num", -15.0))
        self.assertEqual(c("0.00"), ("num", 0.0))
        self.assertEqual(c("69 211.84"), ("num", 69211.84))      # a space for a comma

    def test_ocr_slips_are_repaired_only_when_the_grouping_proves_them(self):
        c = results.classify_cell
        self.assertEqual(c("24,8 18"), ("num", 24818.0))
        self.assertEqual(c("1, 17,471"), ("num", 117471.0))
        self.assertEqual(c("(4. 16)"), ("num", -4.16))
        self.assertEqual(c("16,502 89"), ("bad", None))          # 16,502.89 with the point lost
        self.assertEqual(c("212 17"), ("bad", None))
        self.assertEqual(c("8,02"), ("bad", None))
        self.assertEqual(c("(2.5 I)"), ("bad", None))
        self.assertEqual(c("3,31 I"), ("bad", None))
        self.assertEqual(c("l ,274"), ("bad", None))

    def test_references_percentages_and_nils(self):
        c = results.classify_cell
        self.assertEqual(c("(1-2)"), ("text", None))
        self.assertEqual(c("(7)-(8)-(9)"), ("text", None))
        self.assertEqual(c("24.14%"), ("text", None))
        self.assertEqual(c("-"), ("nil", 0.0))
        self.assertEqual(c("Nil"), ("nil", 0.0))
        self.assertEqual(c("Particulars"), ("text", None))

    def test_serial_numbers_and_normalised_labels(self):
        self.assertEqual(results.strip_serial("a. Revenue from operations"), "Revenue from operations")
        self.assertEqual(results.strip_serial("(iii) Earnings Per Share (EPS)"), "Earnings Per Share (EPS)")
        self.assertEqual(results.strip_serial("- Basic (Rs.)"), "Basic (Rs.)")
        self.assertEqual(results.strip_serial("Ill"), "")
        self.assertEqual(results.strip_serial("(a)"), "")
        self.assertEqual(results.norm_label("VII Profit/(Loss) for the period (V-VI)"), "profit loss for the period v vi")
        self.assertEqual(results.norm_label("Net profit for the period"), "net profit for the period")

    def test_row_labels_as_the_filings_print_them(self):
        m = results.match_row
        self.assertEqual(m(results.norm_label("Interest Earned (a)+(b)+(c)+(d)"), False), ["revenue"])
        self.assertEqual(m(results.norm_label("Total Revenue from Operations"), False), ["revenue"])
        self.assertEqual(m(results.norm_label("fa] Revenue From Oper~tlons"), False), ["revenue"])
        self.assertEqual(m(results.norm_label("Total income (1+2)"), False), ["total_income"])
        self.assertEqual(m(results.norm_label("Total Expenditure (4)+(5)"), False), ["expenses"])
        self.assertEqual(m(results.norm_label("Profitl(Loss) from ordinary activities before tax (7)-(8)-(9)"), False), ["pbt"])
        self.assertEqual(m(results.norm_label("Profit I (Loss) from ordinary activities before exceptional items (1-11)"), False),
                         ["pbt_before_exceptional"])
        self.assertEqual(m(results.norm_label("Profit / (loss) before exceptional items and tax for the period / year (1-2)"), False),
                         ["pbt_before_exceptional"])
        self.assertEqual(m(results.norm_label("profit before share of profit of Joint venture, associates and tax (3+4)"), False), [])
        self.assertEqual(m(results.norm_label("Net Profit(+)/ Loss(-) from Ordinary Activities after tax (10- 11)"), False), ["pat"])
        self.assertEqual(m(results.norm_label("Profit / (loss) for the period / year (5-6)"), False), ["pat"])
        self.assertEqual(m(results.norm_label("Total tax expense / (credit)"), False), ["tax"])
        self.assertEqual(m(results.norm_label("Current tax"), False), [])
        self.assertEqual(m(results.norm_label("Basic and Diluted (in Rs.)"), True), ["eps_basic", "eps_diluted"])
        self.assertEqual(m(results.norm_label("- Diluted (Rs.)"), True), ["eps_diluted"])
        self.assertEqual(m(results.norm_label("Basic EPS before and after extraordinary items, net of tax"), False), ["eps_basic"])
        self.assertEqual(m(results.norm_label("Basic"), False), [])      # only inside the EPS block


class PdfHeader(unittest.TestCase):
    def test_dates_split_over_two_lines_as_indigo_prints_them(self):
        lines = pdf_text("INDIGO").split("\f")[2].splitlines()[:12]
        cols = results.header_columns(lines)
        self.assertEqual([str(d) for x, d in cols],
                         ["2026-09-30", "2026-06-30", "2025-09-30", "2026-09-30", "2025-09-30", "2026-03-31"])

    def test_one_line_dd_mm_yyyy_and_mixed_headers(self):
        fino = pdf_text("FINOPB").split("\f")[1].splitlines()[:12]
        self.assertEqual(len(results.header_columns(fino)), 6)
        esaf = pdf_text("ESAFSFB").split("\f")[4].splitlines()[:12]
        self.assertEqual([str(d) for x, d in results.header_columns(esaf)],
                         ["2026-09-30", "2026-06-30", "2025-09-30", "2026-09-30", "2025-09-30", "2026-03-31"])

    def test_month_over_day_and_year_and_a_group_label_with_its_own_date(self):
        lines = [columns_text("", ["Quarter ended 30.09.2026", "", "", "Half year ended 30.09.2026", "", "Year ended 31.03.2026"], 24),
                 columns_text("Particulars", ["September", "June", "September", "September", "September", "March 31,"], 24),
                 columns_text("", ["30,2026", "30,2026", "30,2025", "30,2026", "30,2025", "2026"], 24)]
        cols = results.header_columns(lines)
        self.assertEqual([str(d) for x, d in cols],
                         ["2026-09-30", "2026-06-30", "2025-09-30", "2026-09-30", "2025-09-30", "2026-03-31"])
        self.assertEqual(results.header_columns(["Statement of results for the quarter ended 30 September 2026"]), [])

    def test_regulation_33_column_layouts(self):
        D = datetime.date
        q2 = results.column_spans([D(2026, 9, 30), D(2026, 6, 30), D(2025, 9, 30), D(2026, 9, 30), D(2025, 9, 30), D(2026, 3, 31)])
        self.assertEqual([c["kind"] for c in q2], ["quarter"] * 3 + ["cumulative"] * 3)
        self.assertEqual((q2[0]["start"], q2[3]["start"], q2[4]["start"], q2[5]["start"]),
                         ("2026-07-01", "2026-04-01", "2025-04-01", "2025-04-01"))
        q1 = results.column_spans([D(2026, 6, 30), D(2026, 3, 31), D(2025, 6, 30), D(2026, 3, 31)])
        self.assertEqual([c["kind"] for c in q1], ["quarter", "quarter", "quarter", "cumulative"])
        self.assertEqual((q1[1]["start"], q1[3]["start"]), ("2026-01-01", "2025-04-01"))
        q4 = results.column_spans([D(2026, 3, 31), D(2025, 12, 31), D(2025, 3, 31), D(2026, 3, 31), D(2025, 3, 31)])
        self.assertEqual([(c["start"], c["end"]) for c in q4[3:]], [("2025-04-01", "2026-03-31"), ("2024-04-01", "2025-03-31")])
        dec = results.column_spans([D(2026, 9, 30), D(2026, 6, 30), D(2025, 9, 30), D(2026, 9, 30), D(2025, 9, 30), D(2025, 12, 31)])
        self.assertEqual((dec[3]["start"], dec[5]["start"]), ("2026-01-01", "2025-01-01"))
        self.assertEqual(results.span_label(q2[3]), "half year to 2026-09-30")
        self.assertEqual(results.span_label(q2[5]), "year to 2026-03-31")

    def test_a_fourth_quarter_filing_uses_the_year_column_as_ttm(self):
        cr = 1e7
        periods = {"revenue": {("2026-01-01", "2026-03-31"): 100 * cr, ("2025-10-01", "2025-12-31"): 90 * cr,
                               ("2025-01-01", "2025-03-31"): 80 * cr, ("2025-04-01", "2026-03-31"): 370 * cr,
                               ("2024-04-01", "2025-03-31"): 300 * cr}}
        core = results.analyse_periods(periods)
        self.assertEqual(core["rows"][0]["fiscal"], "Q4 FY26")
        self.assertEqual((core["ttm"]["revenue"], core["ttm"]["method"]), (370.0, "the full year FY26, from the year column"))

    def test_unit_lines(self):
        u = results.unit_of
        self.assertEqual(u(["(Rupees in millions, except for share data and if otherwise stated)"])[:2], (1e6, "millions"))
        self.assertEqual(u(["Rs. In Lakhs"])[:2], (1e5, "lakhs"))
        self.assertEqual(u(["( ~ in lakhs)"])[:2], (1e5, "lakhs"))
        self.assertEqual(u(["(Rs. in crores)"])[:2], (1e7, "crores"))
        self.assertEqual(u(["(Rs. in Lacs)"])[:2], (1e5, "lakhs"))
        self.assertEqual(u(["(Amount in Rs.)"])[:2], (1.0, "rupees"))
        self.assertEqual(u(["(All amounts in Indian Rupees (2) laklis, except share data)"])[:2], (None, None))


class PdfStatements(unittest.TestCase):
    """Real filings of 2026-10-29, parsed from their saved pdftotext -layout text."""

    def test_interglobe_aviation_consolidated_in_millions(self):
        parsed = parsed_pdf("INDIGO")
        self.assertEqual([(s["page"], s["basis"]) for s in parsed["statements"]], [(3, "consolidated"), (10, "standalone")])
        self.assertEqual(parsed["entity"], "InterGlobe Aviation Limited")
        r = results.analyse_pdf(parsed)
        self.assertEqual((r["basis"], r["scale"], r["source"], r["audited"], r["bank"]),
                         ("consolidated", ["millions"], "bse-pdf", "unaudited", False))
        q = r["rows"][0]
        self.assertEqual((q["fiscal"], q["end"], q["derived"]), ("Q2 FY27", "2026-09-30", False))
        self.assertEqual(q["revenue"], 23363.8)                 # 233,638 million
        self.assertEqual(q["total_income"], 24311.9)
        self.assertEqual(q["ebitda"], 2855.5)                   # PBT -18,765 + finance 16,578 + depreciation 30,742
        self.assertEqual((q["pbt"], q["pat"], q["tax"]), (-1876.5, -1879.1, 2.6))
        self.assertEqual((q["eps_basic"], q["eps_diluted"]), (-48.58, -48.58))
        self.assertEqual((q["revenue_qoq"], q["revenue_yoy"], q["pat_yoy"]), (-5.0, 25.9, 27.2))
        self.assertEqual([x["fiscal"] for x in r["rows"]], ["Q2 FY27", "Q1 FY27", "Q2 FY26"])
        self.assertEqual(r["rows"][2]["exceptional"], 0.0)
        self.assertEqual((r["ttm"]["revenue"], r["ttm"]["pat"], r["ttm"]["eps_diluted"]), (93858.2, -4104.9, -106.12))
        self.assertEqual(r["ttm"]["method"], "FY26 plus H1 FY27 minus H1 FY26, from the year-to-date columns")
        self.assertEqual(r["pdf"]["unread"], [])
        self.assertEqual([c["kind"] for c in r["pdf"]["columns"]], ["quarter"] * 3 + ["cumulative"] * 3)
        self.assertIn("Rupees in millions", r["pdf"]["unit_line"])

    def test_interglobe_standalone_on_request(self):
        r = results.analyse_pdf(parsed_pdf("INDIGO"), basis="standalone")
        self.assertEqual((r["basis"], r["pdf"]["page"]), ("standalone", 10))
        self.assertEqual((r["rows"][0]["pat"], r["rows"][0]["eps_diluted"]), (-2055.2, -53.15))
        self.assertTrue(any("Standalone figures" in l for l in results.changes(r)))

    def test_esaf_small_finance_bank_in_lakhs_with_an_ocr_layer(self):
        parsed = parsed_pdf("ESAFSFB")
        r = results.analyse_pdf(parsed)
        self.assertEqual((r["basis"], r["scale"], r["bank"], r["pdf"]["page"]), ("not stated in the PDF", ["lakhs"], True, 5))
        q = r["rows"][0]
        self.assertEqual(q["revenue"], 1174.71)                 # 1, 17,471 lakh, repaired
        self.assertEqual((q["other_income"], q["total_income"]), (213.33, 1388.04))
        self.assertEqual((q["pbt"], q["pat"], q["tax"]), (214.5, 160.17, 54.33))
        self.assertIsNone(q["ebitda"])
        self.assertEqual((q["eps_basic"], q["eps_diluted"]), (3.1, 3.1))
        self.assertEqual((q["revenue_yoy"], q["pat_yoy"], q["pat_qoq"]), (43.3, 238.3, 100.0))
        self.assertEqual(r["rows"][2]["eps_diluted"], -2.24)
        self.assertEqual((r["ttm"]["revenue"], r["ttm"]["pat"], r["ttm"]["eps_diluted"]), (4161.61, 270.88, 5.24))
        self.assertEqual(r["pdf"]["unread"], ["finance costs: no row found", "depreciation: no row found",
                                              "profit before exceptional items and tax: no row found"])
        lines = results.changes(r)
        self.assertTrue(any("bank or lender" in l for l in lines))
        self.assertTrue(any(l.startswith("Read from the exchange PDF's text layer") for l in lines))

    def test_fino_payments_bank_is_read_partly_and_says_what_is_missing(self):
        r = results.analyse_pdf(parsed_pdf("FINOPB"))
        q = r["rows"][0]
        self.assertEqual((q["revenue"], q["pat"], q["eps_diluted"], q["pbt"]), (74.46, -20.9, -2.51, -20.9))
        self.assertEqual((r["rows"][1]["pat"], r["rows"][2]["pat"], r["rows"][2]["eps_diluted"]), (-13.72, 15.35, 1.84))
        self.assertEqual(r["rows"][0]["total_income"], 333.62)   # interest earned plus other income, rebuilt
        self.assertEqual(r["ttm"]["revenue"], 273.75)
        self.assertNotIn("pat", r["ttm"])                        # the prior half year's PAT cell is '3,31 I'
        unread = r["pdf"]["unread"]
        self.assertIn("total income: no row found", unread)
        self.assertTrue(any(u.startswith("net profit for the period (") and "half year to 2025-09-30 not read (unreadable number)" in u
                            for u in unread), unread)
        self.assertTrue(any(u.startswith("tax expense (") and "quarter to 2026-09-30" in u for u in unread), unread)
        text = results.render("FINOPB", r)
        self.assertIn("Not read from the PDF (", text)
        self.assertIn("(7 row/column gaps are listed above)", text)
        self.assertIn("- basic EPS (Basic EPS before and after extraordinary items, net of tax): quarter to 2026-09-30 not read "
                      "(unreadable number)", text)
        self.assertEqual((r["rows"][0]["eps_basic"], r["rows"][1]["eps_basic"]), (-2.51, -1.65))   # '(2.5 I)' unread, derived as H1 minus Q1
        self.assertIn("a cell listed here can still show in the table", text)

    def test_lakshmi_electrical_small_company_with_no_basis_word(self):
        parsed = parsed_pdf("LECSL")
        r = results.analyse_pdf(parsed)
        self.assertEqual((r["entity"], r["basis"], r["scale"]), ("LAKSHMI ELECTRICAL CONTROL SYSTEMS LIMITED", "not stated in the PDF", ["lakhs"]))
        q = r["rows"][0]
        self.assertEqual((q["revenue"], q["other_income"], q["total_income"]), (94.3, 0.79, 95.09))   # total income rebuilt
        self.assertEqual((q["pbt"], q["pat"], q["eps_diluted"]), (3.61, 2.87, 11.69))
        self.assertEqual((q["revenue_yoy"], q["pat_yoy"]), (61.7, 495.3))
        self.assertEqual((r["ttm"]["revenue"], r["ttm"]["pat"]), (287.08, 3.53))
        self.assertTrue(any(u.startswith("total income (Total Income): quarter to 2026-09-30") and "unreadable number" in u
                            for u in r["pdf"]["unread"]), r["pdf"]["unread"])

    def test_transwarranty_side_by_side_ocr_is_reported_not_guessed(self):
        parsed = parsed_pdf("TFL")
        self.assertEqual(parsed["statements"], [])
        self.assertFalse(parsed["scanned"])
        self.assertEqual(parsed["unusable"][0]["page"], 2)
        why = results.describe_problems(parsed, "https://example.invalid/tfl.pdf")
        self.assertIn("a results statement was found but not read: page 2", why)
        self.assertIn("columns of numbers under 4 readable column dates", why)
        self.assertTrue(why.endswith("Open it yourself: https://example.invalid/tfl.pdf"))
        with self.assertRaisesRegex(results.ResultsError, "found but not read: page 2"):
            results.analyse_pdf(parsed)

    def test_deepak_nitrite_is_a_scanned_filing(self):
        parsed = parsed_pdf("DEEPAKNTR")
        self.assertTrue(parsed["scanned"])
        self.assertEqual((parsed["statements"], parsed["unusable"], parsed["pages"]), ([], [], 10))
        why = results.describe_problems(parsed, "https://www.bseindia.com/x.pdf")
        self.assertIn("no text layer (39 lines of text over 10 pages: a scanned filing)", why)
        self.assertIn("https://www.bseindia.com/x.pdf", why)

    def test_standalone_and_consolidated_side_by_side_split_the_columns(self):
        dates = ["30.09.2026", "30.06.2026", "30.09.2025", "30.09.2026", "30.09.2025", "31.03.2026"]
        sa = [100.0, 90.0, 80.0, 190.0, 150.0, 340.0]
        co = [120.0, 108.0, 96.0, 228.0, 180.0, 408.0]
        head = "STATEMENT OF STANDALONE AND CONSOLIDATED UNAUDITED FINANCIAL RESULTS FOR THE QUARTER AND HALF YEAR ENDED 30 SEPTEMBER 2026"
        labels = " " * (40 + 6 * 12 // 2 - 5) + "STANDALONE" + " " * (6 * 12 - 12) + "CONSOLIDATED"
        lines = [head, " " * 100 + "(Rs. in crores)", labels, columns_text("Particulars", dates + dates),
                 columns_text("1 Revenue from operations", ["%.2f" % v for v in sa] + ["%.2f" % v for v in co]),
                 columns_text("2 Other income", ["5.00"] * 6 + ["6.00"] * 6),
                 columns_text("3 Total income (1+2)", ["%.2f" % (v + 5) for v in sa] + ["%.2f" % (v + 6) for v in co]),
                 columns_text("4 Total expenses", ["85.00"] * 6 + ["100.00"] * 6),
                 columns_text("5 Profit before tax (3-4)", ["20.00"] * 6 + ["26.00"] * 6),
                 columns_text("6 Tax expense", ["5.00"] * 6 + ["6.50"] * 6),
                 columns_text("7 Net profit for the period (5-6)", ["15.00"] * 6 + ["19.50"] * 6),
                 columns_text("8 Earnings per share (of Rs 10 each)", []),
                 columns_text("  Basic", ["1.50"] * 6 + ["1.95"] * 6),
                 columns_text("  Diluted", ["1.50"] * 6 + ["1.95"] * 6)]
        parsed = results.parse_results_pdf("\n".join(lines) + "\n")
        self.assertEqual([s["basis"] for s in parsed["statements"]], ["standalone", "consolidated"])
        self.assertTrue(all(s["side_by_side"] for s in parsed["statements"]))
        r = results.analyse_pdf(parsed)
        self.assertEqual((r["basis"], r["rows"][0]["revenue"], r["rows"][0]["pat"], r["rows"][0]["eps_diluted"]),
                         ("consolidated", 120.0, 19.5, 1.95))
        self.assertEqual(r["ttm"]["revenue"], 456.0)             # 408 + 228 - 180
        s = results.analyse_pdf(parsed, basis="standalone")
        self.assertEqual((s["rows"][0]["revenue"], s["ttm"]["revenue"]), (100.0, 380.0))
        self.assertTrue(all(u.endswith(": no row found") for u in s["pdf"]["unread"]), s["pdf"]["unread"])   # no cell gaps
        with self.assertRaisesRegex(results.ResultsError, "no consolidated statement"):
            results.analyse_pdf(parsed_pdf("LECSL") if False else {"statements": parsed["statements"][:1], "unusable": [],
                                                                       "scanned": False, "pages": 1, "text_lines": 14, "entity": None},
                                basis="consolidated")

    def test_scale_override_and_a_missing_unit(self):
        parsed = parsed_pdf("LECSL")
        self.assertEqual(results.analyse_pdf(parsed, scale="crore")["rows"][0]["revenue"], 9429.55)
        stmt = dict(parsed["statements"][0], unit=None, unit_factor=None, unit_line=None)
        bare = dict(parsed, statements=[stmt])
        with self.assertRaisesRegex(results.ResultsError, "does not say its unit.*--scale lakh"):
            results.analyse_pdf(bare)
        with self.assertRaisesRegex(results.ResultsError, "unknown --scale 'dozens'"):
            results.analyse_pdf(bare, scale="dozens")

    def test_results_filings_from_the_feed_cut(self):
        items = results.parse_rss(fixture(os.path.join("bse-pdf", "bse-announcements.xml")), "BSE")
        self.assertEqual(len(items), 17)
        cands, mine = results.results_filings(items, INDIGO_NAME)
        self.assertEqual(len(mine), 3)
        self.assertEqual([c["link"][-40:] for c in cands], ["6898d04a-23b0-45d7-a70e-0645f44dafc9.pdf"])   # not the press release or the deck
        cands, mine = results.results_filings(items, "", code="544020")
        self.assertEqual((len(mine), [c["link"][-40:] for c in cands]), (4, ["0466d0c6-92bb-49f2-8f03-d4e21d9165dc.pdf"]))
        self.assertEqual(results.results_filings(items, "RELIANCE INDUSTRIES"), ([], []))
        self.assertEqual(results.pdf_guid("https://www.bseindia.com/xml-data/corpfiling/AttachLive/6898d04a-23b0-45d7-a70e-0645f44dafc9.pdf"),
                         "6898d04a-23b0-45d7-a70e-0645f44dafc9")
        self.assertEqual(len(results.pdf_guid("https://example.invalid/a b/c?d=1")), 16)

    @unittest.skipUnless(shutil.which("pdftotext"), "poppler is not installed")
    def test_pdftotext_on_the_saved_pdf(self):
        text = results._pdf_to_text(pdf_fixture("LECSL", ".pdf"))
        r = results.analyse_pdf(results.parse_results_pdf(text))
        self.assertEqual((r["rows"][0]["revenue"], r["rows"][0]["pat"], r["rows"][0]["eps_diluted"]), (94.3, 2.87, 11.69))
        self.assertEqual(results.parse_results_pdf(results._pdf_to_text(pdf_fixture("DEEPAKNTR", ".pdf")))["scanned"], True)


class PdfCli(Workdir):
    """facts --pdf and --today: the honest client, the caches, a 403, a scanned PDF, no poppler."""

    def setUp(self):
        super().setUp()
        self.saved_pdf = (results._pdf_to_text, results.pdftotext_exe)

    def tearDown(self):
        results._pdf_to_text, results.pdftotext_exe = self.saved_pdf
        super().tearDown()

    def serve(self, prefix):
        """The BSE feed and the fixture PDF from the fake network; pdftotext answers with the saved text."""
        pdf_bytes = open(pdf_fixture(prefix, ".pdf"), "rb").read()
        feed = fixture(os.path.join("bse-pdf", "bse-announcements.xml"))
        self.net.by_url = lambda url: FakeResponse(feed) if url == results.BSE_ANNOUNCEMENTS \
            else FakeResponse(pdf_bytes, "application/pdf")
        results._pdf_to_text = lambda path: pdf_text(prefix)

    def test_pdf_text_file_gives_the_same_table_as_xbrl_would(self):
        code, out, err = run(["--now", NOW, "facts", "INDIGO", "--pdf", pdf_fixture("INDIGO"), "--name", INDIGO_NAME])
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("INTERGLOBE AVIATION (INDIGO), consolidated. Rs crore except EPS (filed in millions, "
                                       "converted). Source: BSE results PDF, page 3 (Statement of unaudited consolidated"), out[:300])
        self.assertIn("Q2 FY27  2026-09-30     23,363.80    24,311.90   2,855.50  -1,876.50  -1,879.10     -48.58    -48.58", out)
        self.assertIn("Q2 FY27     -5.0%   +25.9%  -689.5%   +27.2%   +27.3%     12.2%   -8.0%", out)
        self.assertIn("Trailing twelve months: revenue Rs 93,858.20 crore, PAT Rs -4,104.90 crore, diluted EPS Rs -106.12", out)
        self.assertIn("From the PDF: columns read: quarter to 2026-09-30, quarter to 2026-06-30, quarter to 2025-09-30, "
                      "half year to 2026-09-30, half year to 2025-09-30, year to 2026-03-31; unit line: (Rupees in millions", out)
        self.assertIn("other statements in the PDF: page 10 (standalone).", out)
        self.assertIn("Every row and column the tool looks for was read.", out)
        self.assertIn("Read from the exchange PDF's text layer with pdftotext, not from XBRL", out)
        self.assertNotIn(chr(0x2014), out)
        self.assertEqual(len(self.net.requests), 0)
        code, out, err = run(["--now", NOW, "--json", "--write", "facts", "INDIGO", "--pdf", pdf_fixture("INDIGO"),
                              "--name", INDIGO_NAME, "--standalone", "--metrics", "trader/metrics.json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertEqual((doc["facts"]["source"], doc["facts"]["basis"], doc["facts"]["pdf"]["page"]), ("bse-pdf", "standalone", 10))
        self.assertEqual(doc["facts"]["rows"][0]["pat"], -2055.2)
        self.assertEqual([s["page"] for s in doc["facts"]["pdf"]["statements"]], [3, 10])
        with open("trader/results.json") as fh:
            self.assertEqual(json.load(fh)["facts"]["source"], "bse-pdf")
        with open("trader/metrics.json") as fh:
            self.assertEqual(json.load(fh)["companies"][0]["source"], "bse-pdf")

    def test_today_offline_reads_the_feed_cut_and_the_saved_text(self):
        code, out, err = run(["--now", NOW, "--json", "--offline", PDFDIR, "facts", "INDIGO", "--today", "--name", INDIGO_NAME])
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        pdf = doc["facts"]["pdf"]
        self.assertEqual(pdf["filing"]["company"], "InterGlobe Aviation Ltd (539448)")
        self.assertTrue(pdf["url"].endswith("/6898d04a-23b0-45d7-a70e-0645f44dafc9.pdf"), pdf["url"])
        self.assertEqual(doc["facts"]["rows"][0]["revenue"], 23363.8)
        self.assertEqual(len(self.net.requests), 0)
        # by scrip code, without a Kite session or a name
        code, out, err = run(["--now", NOW, "--json", "--offline", PDFDIR, "facts", "ESAFSFB", "--today", "--bse-code", "544020"])
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertEqual((doc["facts"]["rows"][0]["revenue"], doc["facts"]["bank"], doc["facts"]["pdf"]["filing"]["scrip"]),
                         (1174.71, True, "544020"))
        self.assertIn("no Kite session", err)
        self.assertIn("comes from the PDF or the feed", err)

    def test_today_when_the_company_filed_nothing_or_only_a_scanned_pdf(self):
        code, out, err = run(["--now", NOW, "--offline", PDFDIR, "facts", "RELIANCE", "--today", "--name", "RELIANCE INDUSTRIES"])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("nothing from RELIANCE INDUSTRIES in today's BSE announcements looks like a results filing", err)
        code, out, err = run(["--now", NOW, "--offline", PDFDIR, "facts", "DEEPAKNTR", "--today", "--name", "DEEPAK NITRITE"])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("no text layer (39 lines of text over 10 pages: a scanned filing)", err)
        self.assertIn("Open it yourself: https://www.bseindia.com/xml-data/corpfiling/AttachLive/a59f1b7a-6692-4d45-9886-f842d7faf839.pdf", err)
        code, out, err = run(["--now", NOW, "--offline", PDFDIR, "facts", "TFL", "--today", "--name", "TRANSWARRANTY FIN."])
        self.assertEqual(code, 1)
        self.assertIn("a results statement was found but not read: page 2", err)
        code, out, err = run(["--now", NOW, "--offline", PDFDIR, "facts", "INDIGO", "--today"])
        self.assertEqual(code, 2)
        self.assertIn("--today needs the company name", err)

    def test_honest_client_and_the_two_caches(self):
        self.serve("LECSL")
        code, out, err = run(["--now", NOW, "facts", "LECSL", "--today", "--name", LECSL_NAME])
        self.assertEqual(code, 0, err)
        self.assertIn("LAKSHMI ELECTRICAL CONTROL SYSTEMS (LECSL), not stated in the PDF.", out)
        self.assertIn("Q2 FY27  2026-09-30         94.30        95.09", out)
        self.assertIn("BSE feed item: Lakshmi Electrical Control Systems Ltd (504258)", out)
        self.assertEqual(len(self.net.requests), 2)             # the feed, then one PDF
        for req in self.net.requests:
            self.assertEqual(req.get_header("User-agent"), results.USER_AGENT)
            self.assertFalse(req.has_header("Cookie"))
        self.assertEqual(self.net.requests[0].full_url, results.BSE_ANNOUNCEMENTS)
        self.assertIn("/xml-data/corpfiling/AttachLive/", self.net.requests[1].full_url)
        self.assertEqual(glob.glob("trader/cache/bse-announcements-*.xml"), ["trader/cache/bse-announcements-2026-10-29-1015.xml"])
        cached = glob.glob("trader/cache/bse-pdf/*.pdf")
        self.assertEqual(len(cached), 1)
        self.assertTrue(cached[0].endswith(results.pdf_guid(self.net.requests[1].full_url) + ".pdf"))
        with open(cached[0], "rb") as fh:
            self.assertTrue(fh.read(4) == b"%PDF")
        code, out, err = run(["--now", NOW, "facts", "LECSL", "--today", "--name", LECSL_NAME])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.net.requests), 2)             # both answered from the caches
        code, out, err = run(["--now", NOW, "facts", "LECSL", "--pdf", self.net.requests[1].full_url, "--name", LECSL_NAME])
        self.assertEqual((code, len(self.net.requests)), (0, 2))  # --pdf URL hits the same PDF cache

    def test_a_403_is_not_retried_and_says_what_to_do(self):
        feed = fixture(os.path.join("bse-pdf", "bse-announcements.xml"))
        self.net.by_url = lambda url: FakeResponse(feed) if url == results.BSE_ANNOUNCEMENTS else http_error(403)
        code, out, err = run(["--now", NOW, "facts", "ESAFSFB", "--today", "--name", ESAF_NAME])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("HTTP 403 for https://www.bseindia.com/xml-data/corpfiling/AttachLive/0466d0c6-92bb-49f2-8f03-d4e21d9165dc.pdf "
                      "(not retried; open the link in your browser and pass the saved file with --pdf PATH)", err)
        self.assertEqual(len(self.net.requests), 2)
        self.assertEqual(glob.glob("trader/cache/bse-pdf/*"), [])
        self.net.by_url = lambda url: http_error(403)
        code, out, err = run(["--now", NOW, "facts", "ESAFSFB", "--today", "--name", ESAF_NAME])
        self.assertEqual(code, 1)
        self.assertEqual(len(self.net.requests), 3)             # the feed came from the 10-minute cache
        self.assertNotIn(results.BSE_ANNOUNCEMENTS, [r.full_url for r in self.net.requests[2:]])

    def test_a_pdf_without_a_text_layer_is_reported_with_its_link(self):
        self.serve("DEEPAKNTR")
        code, out, err = run(["--now", NOW, "facts", "DEEPAKNTR", "--today", "--name", "DEEPAK NITRITE"])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("a59f1b7a-6692-4d45-9886-f842d7faf839.pdf: the PDF has no text layer", err)
        self.assertIn("Open it yourself: https://www.bseindia.com/xml-data/corpfiling/AttachLive/a59f1b7a", err)

    def test_without_poppler_one_line_says_so(self):
        results.pdftotext_exe = lambda: None
        with open("filing.pdf", "wb") as fh:
            fh.write(b"%PDF-1.4\n")
        code, out, err = run(["--now", NOW, "facts", "INDIGO", "--pdf", "filing.pdf", "--name", INDIGO_NAME])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("pdftotext is not installed: brew install poppler", err)
        self.assertIn("download the XBRL from the exchange and pass --file", err)

    def test_usage_nseindia_and_a_missing_file(self):
        code, _, err = run(["facts", "INDIGO", "--file", Q2FY27, "--pdf", "x.pdf"])
        self.assertEqual(code, 2)
        self.assertIn("exactly one of --file PATH (XBRL), --pdf PATH_OR_URL or --today", err)
        code, _, err = run(["facts", "INDIGO"])
        self.assertEqual(code, 2)
        code, _, err = run(["--now", NOW, "facts", "INDIGO", "--pdf", "https://nsearchives.nseindia.com/x.pdf", "--name", INDIGO_NAME])
        self.assertEqual(code, 1)
        self.assertIn("nseindia.com is out", err)
        self.assertEqual(len(self.net.requests), 0)
        code, _, err = run(["--now", NOW, "facts", "INDIGO", "--pdf", "/nonexistent/results.pdf", "--name", INDIGO_NAME])
        self.assertEqual(code, 1)
        self.assertIn("no such file: /nonexistent/results.pdf", err)


if __name__ == "__main__":
    unittest.main()
