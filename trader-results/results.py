#!/usr/bin/env python3
"""Results reader for trader-results. Standard library only.

Reads the quarterly results an NSE or BSE listed company filed in XBRL (the
Ind AS financial-results instance document) and lays out what changed,
quarter by quarter: revenue from operations, total income, EBITDA (derived),
profit before tax, profit after tax, margins, basic and diluted EPS, each
quarter against the previous quarter and the same quarter a year earlier,
plus the trailing twelve months. It also sums up a pasted results commentary
or call transcript, and says whether the next result falls inside a day
trade's horizon.

    python3 results.py facts RELIANCE --file results.xml
    python3 results.py facts RELIANCE --file q2.xml --file q1.xml --quarters 8 --json
    python3 results.py facts RELIANCE --file results.xml --standalone --write
    python3 results.py facts RELIANCE --today                 # today's results PDF from BSE's feed
    python3 results.py facts RELIANCE --pdf https://www.bseindia.com/xml-data/corpfiling/AttachLive/<guid>.pdf
    python3 results.py facts RELIANCE --pdf ~/Downloads/results.pdf --standalone
    python3 results.py summary --paste < transcript.txt
    python3 results.py calendar RELIANCE
    python3 results.py calendar RELIANCE --date 2026-10-30

Where the XBRL file comes from: the exchanges do not allow scripted access to
their filing pages, so you download it yourself, in your browser. On NSE:
Corporates, Financial Results, the row for the company, the XBRL link. On
BSE: the result announcement, its XBRL attachment. Save it and pass the path
with --file (repeat --file for several quarters; later filings restate
earlier ones).

The PDF route: BSE's public announcements feed links every company's results
PDF the moment it is filed, and that PDF carries the SEBI LODR Regulation 33
"Statement of (un)audited financial results" table. --today finds today's
filing for the company in the feed (matched by Kite's instrument name, or
--bse-code), --pdf takes the link or a saved file; the PDF is cached under
trader/cache/bse-pdf/, turned into text with pdftotext -layout (poppler) and
the statement's table is read: the column dates (quarter ended, preceding
quarter, year-earlier quarter, year to date, year ended), the rows for
revenue, other income, total income, total expenses, finance costs,
depreciation, profit before exceptional items, exceptional items, PBT, tax,
PAT and EPS, and the unit line (Rs in lakhs / crore / million). Consolidated
is preferred when the PDF carries both statements (--standalone to switch).
The numbers then go through the same quarters table, growth, margins and
"what changed" as the XBRL route, marked source "bse-pdf". Layouts vary; the
tool says which rows and columns it could not read, and a scanned PDF (no
text layer) is reported as such with the link. Without poppler it says so in
one line (brew install poppler) and suggests the XBRL.

How the quarters are built:
  - Every filing carries the quarter, the previous quarter, the same quarter
    a year earlier, the year to date for both years and the last full year
    (the xbrli:context periods). A quarter is a period of 80 to 100 days.
  - When a period appears in more than one file, the file for the later
    reporting period wins, so restatements are used.
  - A quarter that is not reported on its own (the fourth quarter, when only
    the full year is in the file) is a year-to-date column minus the one
    before it, and is marked "derived".
  - EBITDA is not an Ind AS line. It is derived here as profit before tax
    plus finance costs plus depreciation, and marked so. For banks (whose
    revenue line is interest earned) it is left blank.
  - Rupees print in crore with two decimals. The filing's level of rounding
    (lakh, crore, million, actual) is read from the file; --scale overrides
    it when a file does not say.

Sources for the calendar (a plainly identified client, public feeds only):
  Google News RSS  https://news.google.com/rss/search?q=...&hl=en-IN&gl=IN&ceid=IN:en
  BSE announcements RSS  https://www.bseindia.com/data/xml/announcements.xml
    (the exchange's own feed; it covers only the current day's announcements)
No official results calendar may be read by a script; the tool says what the
headlines and today's exchange announcements say and asks you to confirm on
the exchange's page. Offline: --offline DIR reads googlenews-<SYMBOL>-results.xml,
googlenews-<SYMBOL>-board.xml and bse-announcements.xml from DIR.

Numbers only: this tool never says whether a stock is a buy.
"""

import argparse
import calendar as calendar_module
import datetime
import email.utils
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as etree

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trader-kite"))
import kite   # noqa: E402
import nse    # noqa: E402

USER_AGENT = "day-trader-skill-india/1.0 (+https://github.com/krishanag/day-trader-skill)"
GOOGLE_NEWS = "https://news.google.com/rss/search?"
BSE_ANNOUNCEMENTS = "https://www.bseindia.com/data/xml/announcements.xml"
RESULTS_FILE = os.path.join("trader", "results.json")
TIMEOUT = 30
CRORE = 1e7
SOURCE = "Ind AS XBRL financial results (NSE/BSE filing, downloaded by you)"

# the filing's "level of rounding" -> rupees per unit
SCALES = {"actual": 1.0, "absolute": 1.0, "rupees": 1.0, "rupee": 1.0, "units": 1.0, "unit": 1.0,
          "hundreds": 1e2, "thousands": 1e3, "thousand": 1e3, "lakhs": 1e5, "lakh": 1e5, "lacs": 1e5,
          "lac": 1e5, "millions": 1e6, "million": 1e6, "crores": 1e7, "crore": 1e7, "billions": 1e9,
          "billion": 1e9}

# Ind AS financial-results taxonomy element names (in-bse-fin), matched by local name,
# first one found wins. The bank line (InterestEarned) only stands in for revenue.
CONCEPTS = {
    "revenue": ["RevenueFromOperations", "InterestEarned"],
    "other_income": ["OtherIncome"],
    "total_income": ["Income", "TotalIncome"],
    "finance_costs": ["FinanceCosts"],
    "depreciation": ["DepreciationDepletionAndAmortisationExpense",
                     "DepreciationAndAmortisationExpense"],
    "employee": ["EmployeeBenefitExpense"],
    "expenses": ["Expenses", "TotalExpenses"],
    "exceptional": ["ExceptionalItemsBeforeTax"],
    "pbt_before_exceptional": ["ProfitBeforeExceptionalItemsAndTax"],
    "pbt": ["ProfitBeforeTax"],
    "tax": ["TaxExpense"],
    "pat": ["ProfitLossForPeriod", "ProfitLossForPeriodFromContinuingOperations", "ProfitLoss"],
    "eps_basic": ["BasicEarningsLossPerShareFromContinuingOperations", "BasicEarningsLossPerShare",
                  "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations"],
    "eps_diluted": ["DilutedEarningsLossPerShareFromContinuingOperations",
                    "DilutedEarningsLossPerShare",
                    "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations"],
}
PER_SHARE = ("eps_basic", "eps_diluted")
BANK_REVENUE = "InterestEarned"
INFO = {"name": "NameOfTheCompany", "symbol": "NSESymbol", "scrip": "ScripCode",
        "rounding": "LevelOfRoundingUsedInFinancialStatements",
        "audited": "WhetherResultsAreAuditedOrUnaudited",
        "nature": "NatureOfReportStandaloneConsolidated",
        "period_end": "DateOfEndOfReportingPeriod"}
QUARTER_DAYS = (80, 100)
YEAR_DAYS = (350, 380)
YEAR_SLACK = 10                # days either side when matching the same period a year earlier
OTHER_INCOME_FLAG_PCT = 25.0   # other income above this share of PBT is flagged
MAX_HEADLINES = 8
NEAR_DAYS = (-45, 120)         # a results date mentioned this far from today is believed
BSE_WEIGHT = 3                 # the exchange's own feed outweighs a headline


class ResultsError(Exception):
    pass


# ---------------------------------------------------------------- http (the honest client)

_urlopen = urllib.request.urlopen   # tests swap this


def fetch(url, offline=None, name=None):
    """GET url as text, plainly identified, no cookies, no retry. Offline: read DIR/name."""
    if offline:
        path = os.path.join(offline, name or "")
        if not os.path.isfile(path):
            raise ResultsError("offline: no file %s" % path)
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with _urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise ResultsError("HTTP %d for %s" % (exc.code, url.split("?")[0]))
    except urllib.error.URLError as exc:
        raise ResultsError("could not reach %s: %s" % (url.split("?")[0], exc.reason))
    except OSError as exc:
        raise ResultsError("could not read %s: %s" % (url.split("?")[0], exc))


# ---------------------------------------------------------------- the XBRL instance

def local(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag.split(":")[-1]


def _date(s):
    return datetime.date.fromisoformat(s)


def basis_of(context):
    """'consolidated', 'standalone' or None from the context's dimension members."""
    for el in context.iter():
        if local(el.tag) == "explicitMember":
            low = (el.text or "").lower()
            if "consolidated" in low:
                return "consolidated"
            if "standalone" in low:
                return "standalone"
    return None


def parse_instance(text, path=None):
    """One XBRL results file -> contexts, numeric facts, header info and every element name."""
    try:
        root = etree.fromstring(text)
    except etree.ParseError as exc:
        raise ResultsError("%s is not well-formed XML (%s)" % (path or "the file", exc))
    contexts, facts, info, names = {}, [], {}, set()
    for el in root:
        name = local(el.tag)
        if name == "context":
            period = {}
            for sub in el.iter():
                sub_name = local(sub.tag)
                if sub_name in ("startDate", "endDate", "instant"):
                    period[sub_name] = (sub.text or "").strip()
            contexts[el.get("id")] = {"start": period.get("startDate"), "end": period.get("endDate"),
                                      "instant": period.get("instant"), "basis": basis_of(el)}
            continue
        if name in ("unit", "schemaRef"):
            continue
        names.add(name)
        text_value = (el.text or "").strip()
        for key, info_name in INFO.items():
            if name == info_name and text_value and key not in info:
                info[key] = text_value
        if el.get("contextRef") is None or not text_value:
            continue
        try:
            value = float(text_value.replace(",", ""))
        except ValueError:
            continue
        facts.append({"name": name, "context": el.get("contextRef"), "value": value,
                      "unit": el.get("unitRef"), "decimals": el.get("decimals")})
    if not contexts or not facts:
        raise ResultsError("%s has no XBRL contexts or numeric facts; elements found: %s"
                           % (path or "the file", ", ".join(sorted(names)[:30]) or "none"))
    return {"path": path, "contexts": contexts, "facts": facts, "info": info, "names": names}


def read_file(path):
    if not os.path.isfile(path):
        raise ResultsError("no such file: %s" % path)
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    return parse_instance(text, path)


def scale_of(instance, override=None):
    """(rupees per unit, label) from --scale or the file's level of rounding."""
    word = (override or instance["info"].get("rounding") or "").strip().lower()
    word = re.sub(r"^in\s+|^rs\.?\s*|^inr\s*", "", word).strip()
    if word in SCALES:
        return SCALES[word], word
    if not word:
        raise ResultsError("%s does not say its level of rounding (no %s element): pass "
                           "--scale lakh, --scale crore or --scale rupees after checking the PDF"
                           % (instance["path"] or "the file", INFO["rounding"]))
    raise ResultsError("%s reports an unknown level of rounding %r: pass --scale lakh, crore, "
                       "million or rupees" % (instance["path"] or "the file", word))


def bases_in(instance):
    return {c["basis"] for c in instance["contexts"].values() if c["basis"]}


def choose_basis(instances, wanted=None):
    """Consolidated when any file has it, else standalone; --consolidated/--standalone force it."""
    available = set()
    for inst in instances:
        available |= bases_in(inst)
    if wanted:
        if not available:
            return None
        if wanted in available:
            return wanted
        raise ResultsError("no %s figures in the file(s); they carry: %s"
                           % (wanted, ", ".join(sorted(available))))
    if "consolidated" in available:
        return "consolidated"
    if "standalone" in available:
        return "standalone"
    return None


def facts_table(instance, basis):
    """{key: ({(start, end): value}, concept used)} for one file and one basis, in the file's unit."""
    by_name = {}
    for f in instance["facts"]:
        ctx = instance["contexts"].get(f["context"])
        if not ctx or not ctx["start"] or not ctx["end"]:
            continue
        if basis and ctx["basis"] and ctx["basis"] != basis:
            continue
        by_name.setdefault(f["name"], {}).setdefault((ctx["start"], ctx["end"]), f["value"])
    out = {}
    for key, concepts in CONCEPTS.items():
        for concept in concepts:
            if by_name.get(concept):
                out[key] = (by_name[concept], concept)
                break
    return out


def merge_files(instances, basis, scale_override=None):
    """Later filings win. -> ({key: {(start, end): rupees (EPS as is)}}, concepts, scale labels)."""
    ordered = sorted(instances, key=lambda i: (i["info"].get("period_end") or "",))
    tables = [(inst, facts_table(inst, basis)) for inst in ordered]
    if not any(k in table for _, table in tables for k in ("revenue", "pat", "pbt")):
        found = set()
        for inst in instances:
            found |= inst["names"]
        raise ResultsError("no revenue, profit before tax or profit after tax in the file(s): not an "
                           "Ind AS financial-results instance? Elements found: %s"
                           % (", ".join(sorted(found)[:40]) or "none"))
    merged, concepts, labels = {}, {}, []
    for inst, table in tables:
        factor, label = scale_of(inst, scale_override)
        labels.append(label)
        for key, (periods, concept) in table.items():
            concepts.setdefault(key, [])
            if concept not in concepts[key]:
                concepts[key].append(concept)
            bucket = merged.setdefault(key, {})
            for span, value in periods.items():
                bucket[span] = value if key in PER_SHARE else value * factor
    return merged, concepts, labels


# ---------------------------------------------------------------- quarters

def days(span):
    return (_date(span[1]) - _date(span[0])).days


def is_quarter(span):
    return QUARTER_DAYS[0] <= days(span) <= QUARTER_DAYS[1]


def quarterly(periods):
    """{end: {value, start, end, derived}}: reported quarters, plus year-to-date differences."""
    q = {}
    for span, value in periods.items():
        if is_quarter(span):
            q[span[1]] = {"value": value, "start": span[0], "end": span[1], "derived": False}
    cumulative = [s for s in periods if days(s) > QUARTER_DAYS[1]]
    for span in sorted(cumulative, key=lambda s: s[1]):
        if span[1] in q:
            continue
        earlier = [s for s in periods if s[0] == span[0] and s != span
                   and QUARTER_DAYS[0] <= (_date(span[1]) - _date(s[1])).days <= QUARTER_DAYS[1] + 1]
        if not earlier:
            continue
        prev = max(earlier, key=lambda s: s[1])
        start = _date(prev[1]) + datetime.timedelta(days=1)
        q[span[1]] = {"value": periods[span] - periods[prev], "start": start.isoformat(),
                      "end": span[1], "derived": True}
    return q


def fiscal_year_start_month(periods_by_key):
    """The month the company's year starts (April for nearly every Indian company)."""
    for periods in periods_by_key.values():
        for span in periods:
            if YEAR_DAYS[0] <= days(span) <= YEAR_DAYS[1]:
                return _date(span[0]).month
    return 4


def fy_of(end, fy_start_month=4):
    """The two-digit fiscal year a period ending `end` belongs to (2026-09-30 -> 27)."""
    d = _date(end)
    return (d.year + (1 if d.month >= fy_start_month and fy_start_month != 1 else 0)) % 100


def fiscal_label(end, fy_start_month=4):
    """'2026-09-30' with an April year -> 'Q2 FY27'."""
    d = _date(end)
    months = (d.month - fy_start_month) % 12 + 1
    return "Q%d FY%02d" % ((months + 2) // 3, fy_of(end, fy_start_month))


def _match(series_, end, back_days, slack=YEAR_SLACK):
    target = _date(end) - datetime.timedelta(days=back_days)
    best = None
    for e in series_:
        gap = abs((_date(e) - target).days)
        if gap <= slack and (best is None or gap < best[0]):
            best = (gap, e)
    return series_[best[1]] if best else None


def _match_span(periods, span):
    """The same span a year earlier, within YEAR_SLACK days at both ends."""
    for s in periods:
        if abs((_date(span[0]) - _date(s[0])).days - 365) <= YEAR_SLACK and \
                abs((_date(span[1]) - _date(s[1])).days - 365) <= YEAR_SLACK:
            return s
    return None


def ytd_label(span):
    n = days(span)
    if n <= QUARTER_DAYS[1]:
        return "Q1"
    if n <= 190:
        return "H1"
    if n <= 280:
        return "9M"
    return "the year"


def pct(a, b):
    if a is None or b in (None, 0):
        return None
    return round(100.0 * (a - b) / abs(b), 1)


def margin(part, whole):
    if part is None or not whole:
        return None
    return round(100.0 * part / whole, 1)


def crore(v):
    return None if v is None else round(v / CRORE, 2)


def ttm_of(quarters_by_key, periods_by_key, fy_start_month):
    """Trailing twelve months: four consecutive quarters, else last year + YTD - prior YTD."""
    out, method = {}, None
    keys = ("revenue", "pat", "eps_basic", "eps_diluted")
    for key in keys:
        q = quarters_by_key.get(key) or {}
        last4 = sorted(q)[-4:]
        if len(last4) == 4 and (_date(last4[-1]) - _date(last4[0])).days < 300:
            out[key] = sum(q[e]["value"] for e in last4)
            method = method or "four quarters, %s to %s" % (fiscal_label(last4[0], fy_start_month),
                                                            fiscal_label(last4[-1], fy_start_month))
    if out:
        return out, method
    for key in keys:
        periods = periods_by_key.get(key) or {}
        years = sorted((s for s in periods if YEAR_DAYS[0] <= days(s) <= YEAR_DAYS[1]),
                       key=lambda s: s[1])
        if not years:
            continue
        year = years[-1]
        latest = max(quarters_by_key.get(key) or {"": None})
        if latest and latest == year[1]:          # a fourth-quarter filing: the year column is the TTM
            out[key] = periods[year]
            method = method or "the full year FY%02d, from the year column" % fy_of(year[1], fy_start_month)
            continue
        next_start = (_date(year[1]) + datetime.timedelta(days=1)).isoformat()
        ytd = [s for s in periods if s[0] == next_start]
        if not ytd:
            continue
        cur = max(ytd, key=lambda s: s[1])
        prior = _match_span(periods, cur)
        if prior is None:
            continue
        out[key] = periods[year] + periods[cur] - periods[prior]
        method = method or ("FY%02d plus %s FY%02d minus %s FY%02d, from the year-to-date columns"
                            % (fy_of(year[1], fy_start_month), ytd_label(cur), fy_of(cur[1], fy_start_month),
                               ytd_label(prior), fy_of(prior[1], fy_start_month)))
    return out, method


def analyse_periods(periods_by_key, quarters=6, bank=False):
    """The quarters table and the trailing twelve months from {key: {(start, end): rupees}} (EPS in rupees)."""
    fy_month = fiscal_year_start_month(periods_by_key)
    q = {key: quarterly(periods) for key, periods in periods_by_key.items()}
    rev, pat = q.get("revenue", {}), q.get("pat", {})
    ends = sorted(set(rev) | set(pat), reverse=True)[:quarters]
    if not ends:
        spans = periods_by_key.get("revenue") or periods_by_key.get("pat") or {}
        raise ResultsError("no three-month periods in the file(s): the contexts run %s"
                           % ", ".join(sorted("%s to %s" % s for s in spans)))

    def val(key, end):
        return (q.get(key) or {}).get(end, {}).get("value")

    compare = ("revenue", "pat", "eps_diluted", "total_income")
    rows = []
    for end in ends:
        r, oi, ti = val("revenue", end), val("other_income", end), val("total_income", end)
        if ti is None and r is not None and oi is not None:
            ti = r + oi
        fin, dep, pbt, pat_v = val("finance_costs", end), val("depreciation", end), val("pbt", end), val("pat", end)
        ebitda = None
        if not bank and None not in (pbt, fin, dep):
            ebitda = pbt + fin + dep
        eps_b, eps_d = val("eps_basic", end), val("eps_diluted", end)
        prev = {k: _match(q.get(k, {}), end, 91) for k in compare}
        year = {k: _match(q.get(k, {}), end, 365) for k in compare}
        base = rev.get(end) or pat.get(end) or {}
        rows.append({
            "end": end, "start": base.get("start"), "fiscal": fiscal_label(end, fy_month),
            "derived": bool(base.get("derived")),
            "revenue": crore(r), "other_income": crore(oi), "total_income": crore(ti),
            "ebitda": crore(ebitda), "ebitda_margin": margin(ebitda, r),
            "pbt": crore(pbt), "pat": crore(pat_v), "pat_margin": margin(pat_v, r),
            "exceptional": crore(val("exceptional", end)), "tax": crore(val("tax", end)),
            "eps_basic": None if eps_b is None else round(eps_b, 2),
            "eps_diluted": None if eps_d is None else round(eps_d, 2),
            "revenue_qoq": pct(r, (prev["revenue"] or {}).get("value")),
            "revenue_yoy": pct(r, (year["revenue"] or {}).get("value")),
            "total_income_yoy": pct(ti, (year["total_income"] or {}).get("value")),
            "pat_qoq": pct(pat_v, (prev["pat"] or {}).get("value")),
            "pat_yoy": pct(pat_v, (year["pat"] or {}).get("value")),
            "eps_qoq": pct(eps_d, (prev["eps_diluted"] or {}).get("value")),
            "eps_yoy": pct(eps_d, (year["eps_diluted"] or {}).get("value")),
        })
    ttm_raw, method = ttm_of(q, periods_by_key, fy_month)
    ttm = {}
    for key, value in ttm_raw.items():
        ttm[key] = round(value, 2) if key in PER_SHARE else crore(value)
    if ttm:
        ttm["method"] = method
    return {"rows": rows, "ttm": ttm}


def analyse(instances, basis=None, scale=None, quarters=6, name=None):
    """Every number in the report, as a dict (rupees in crore, EPS in rupees), from XBRL files."""
    if not instances:
        raise ResultsError("no file could be read")
    chosen = choose_basis(instances, basis)
    periods_by_key, concepts, labels = merge_files(instances, chosen, scale)
    bank = concepts.get("revenue") == [BANK_REVENUE]
    core = analyse_periods(periods_by_key, quarters, bank)
    info = {}
    for inst in instances:
        for k, v in inst["info"].items():
            info.setdefault(k, v)
    result = {"entity": name or info.get("name"), "filed_symbol": info.get("symbol"),
              "scrip_code": info.get("scrip"), "basis": chosen or "not stated in the file",
              "scale": sorted(set(labels)), "audited": info.get("audited"), "bank": bank,
              "files": [os.path.basename(i["path"] or "") for i in instances],
              "concepts": concepts, "source": SOURCE}
    result.update(core)
    return result


def _cr(v):
    return "Rs %s crore" % "{:,.2f}".format(v)


def changes(result):
    """Plain sentences: what changed in the latest quarter, and what to check."""
    rows = result["rows"]
    if not rows:
        return []
    cur = rows[0]
    prev = rows[1] if len(rows) > 1 else None
    out = []
    label = "%s (quarter ended %s%s)" % (cur["fiscal"], cur["end"],
                                          ", derived from the year-to-date columns" if cur["derived"] else "")
    line = "%s: revenue %s" % (label, "n/a" if cur["revenue"] is None else _cr(cur["revenue"]))
    if cur["revenue_yoy"] is not None:
        line += " (%+.1f%% vs a year earlier" % cur["revenue_yoy"]
        line += ", %+.1f%% vs the quarter before)" % cur["revenue_qoq"] if cur["revenue_qoq"] is not None else ")"
    line += ", PAT %s" % ("n/a" if cur["pat"] is None else _cr(cur["pat"]))
    if cur["pat_yoy"] is not None:
        line += " (%+.1f%% yoy" % cur["pat_yoy"]
        line += ", %+.1f%% qoq)" % cur["pat_qoq"] if cur["pat_qoq"] is not None else ")"
    if cur["eps_diluted"] is not None:
        line += ", diluted EPS Rs %.2f" % cur["eps_diluted"]
        if cur["eps_yoy"] is not None:
            line += " (%+.1f%% yoy)" % cur["eps_yoy"]
    out.append(line + ".")
    for key, name in (("ebitda_margin", "EBITDA margin (derived)"), ("pat_margin", "PAT margin")):
        if cur[key] is None:
            continue
        s = "%s %.1f%%" % (name, cur[key])
        if prev and prev[key] is not None:
            s += " (%+.1f points vs the quarter before)" % (cur[key] - prev[key])
        out.append(s + ".")
    if cur.get("exceptional"):
        out.append("Exceptional items of %s are inside PBT and PAT this quarter: read the notes to the "
                   "results before reading the change as business." % _cr(cur["exceptional"]))
    for r in rows[1:3]:
        if r.get("exceptional"):
            out.append("%s carried exceptional items of %s, so the comparison with it is distorted."
                       % (r["fiscal"], _cr(r["exceptional"])))
    if cur["other_income"] is not None and cur["pbt"]:
        share = 100.0 * cur["other_income"] / cur["pbt"]
        if share > OTHER_INCOME_FLAG_PCT:
            out.append("Other income is %.0f%% of PBT (%s): treasury gains and the like, not operations."
                       % (share, _cr(cur["other_income"])))
    if cur["pat"] is not None and cur["pbt"] is not None and cur["pat"] > cur["pbt"]:
        out.append("PAT is above PBT: a tax credit or write-back is inside it. Check the tax line before "
                   "reading it as growth.")
    if result.get("bank"):
        out.append("A bank or lender: revenue here is interest earned and EBITDA is left blank, since "
                   "finance costs are its cost of goods.")
    if prev and cur["revenue_qoq"] is not None and cur["revenue_yoy"] is not None:
        out.append("Compare with a year earlier first: many businesses are seasonal (the December quarter "
                   "carries the festive season, the March quarter the year-end push).")
    if result["basis"] == "standalone":
        out.append("Standalone figures: subsidiaries are not in them. Pass --consolidated when the file has it.")
    if result.get("source") == PDF_SOURCE:
        pdf = result["pdf"]
        out.append("Read from the exchange PDF's text layer with pdftotext, not from XBRL: check any figure that "
                   "looks off against the PDF itself%s." % (" (%d row/column gaps are listed above)" % len(pdf["unread"])
                                                           if pdf["unread"] else ""))
    return out


# ---------------------------------------------------------------- output

def _m(v):
    return "-" if v is None else "{:,.2f}".format(v)


def _p(v):
    return "-" if v is None else ("%+.1f%%" % v if v else "0.0%")


def _pm(v):
    return "-" if v is None else "%.1f%%" % v


def _eps(v):
    return "-" if v is None else "%.2f" % v


def source_line(result):
    if result.get("source") == PDF_SOURCE:
        pdf = result["pdf"]
        return "BSE results PDF, page %d (%s): %s" % (pdf["page"], pdf["title"] or "statement of financial results",
                                                      pdf["url"] or pdf["path"] or "text passed in")
    return "%s: %s" % (SOURCE, ", ".join(result["files"]))


def render_pdf_notes(result):
    """What was read from the PDF and what was not."""
    pdf = result["pdf"]
    out = ["From the PDF: columns read: %s; unit line: %s." % (
        ", ".join(span_label(c) for c in pdf["columns"]), pdf["unit_line"] or "none (--scale used)")]
    if pdf.get("filing"):
        out.append("  BSE feed item: %s, %s: %s" % (pdf["filing"]["company"], (pdf["filing"]["published"] or "")[:16].replace("T", " "),
                                                   pdf["filing"]["description"][:120]))
    others = [s for s in pdf["statements"] if s["page"] != pdf["page"]]
    if others:
        out.append("  other statements in the PDF: %s." % "; ".join(
            "page %d (%s%s)" % (s["page"], s["basis"], ", not read: " + s["problem"] if s["problem"] else "") for s in others))
    for n in pdf["notes"]:
        out.append("  note: %s." % n)
    if pdf["unread"]:
        out.append("  Not read from the PDF (a cell listed here can still show in the table when the year-to-date "
                   "columns let the tool derive it, as the XBRL route does):")
        for u in pdf["unread"]:
            out.append("    - " + u)
    else:
        out.append("  Every row and column the tool looks for was read.")
    return out


def render(symbol, result):
    scale = " / ".join(result["scale"]) or "unknown"
    out = ["%s (%s), %s. Rs crore except EPS (filed in %s%s). Source: %s." % (
        result["entity"] or symbol, symbol, result["basis"], scale,
        "" if scale in ("crore", "crores") else ", converted", source_line(result)), ""]
    if result.get("filed_symbol") and result["filed_symbol"].upper() != symbol.upper():
        out.append("NOTE: the file names %s, not %s. Check you opened the right company's filing."
                   % (result["filed_symbol"], symbol))
        out.append("")
    out.append("%-8s %-11s %12s %12s %10s %10s %10s %10s %9s" % (
        "quarter", "ended", "revenue", "total inc", "EBITDA*", "PBT", "PAT", "EPS basic", "EPS dil"))
    for r in result["rows"]:
        out.append("%-8s %-11s %12s %12s %10s %10s %10s %10s %9s%s" % (
            r["fiscal"], r["end"], _m(r["revenue"]), _m(r["total_income"]), _m(r["ebitda"]),
            _m(r["pbt"]), _m(r["pat"]), _eps(r["eps_basic"]), _eps(r["eps_diluted"]),
            "  derived" if r["derived"] else ""))
    out.append("")
    out.append("%-8s %8s %8s %8s %8s %8s %9s %7s" % (
        "quarter", "rev qoq", "rev yoy", "PAT qoq", "PAT yoy", "EPS yoy", "EBITDA %", "PAT %"))
    for r in result["rows"]:
        out.append("%-8s %8s %8s %8s %8s %8s %9s %7s" % (
            r["fiscal"], _p(r["revenue_qoq"]), _p(r["revenue_yoy"]), _p(r["pat_qoq"]),
            _p(r["pat_yoy"]), _p(r["eps_yoy"]), _pm(r["ebitda_margin"]), _pm(r["pat_margin"])))
    t = result["ttm"]
    out.append("")
    if t:
        out.append("Trailing twelve months: revenue %s, PAT %s, diluted EPS %s (%s)." % (
            _cr(t["revenue"]) if t.get("revenue") is not None else "-",
            _cr(t["pat"]) if t.get("pat") is not None else "-",
            "Rs %.2f" % t["eps_diluted"] if t.get("eps_diluted") is not None else "-", t["method"]))
    else:
        out.append("Trailing twelve months: not enough in these files (four quarters, or a full year "
                   "with both year-to-date columns). Pass more filings with --file.")
    if result.get("source") == PDF_SOURCE:
        out.append("")
        out.extend(render_pdf_notes(result))
    out.append("")
    out.append("What changed:")
    for line in changes(result):
        out.append("  - " + line)
    out.append("")
    out.append("* EBITDA is derived: PBT + finance costs + depreciation, not a reported line. Derived "
               "quarters are a year-to-date column minus the one before it.")
    if result.get("audited"):
        out.append("The filing says: %s." % result["audited"])
    return "\n".join(out) + "\n"


def metrics_row(symbol, result, as_of):
    rows = result["rows"]
    cur = rows[0] if rows else {}
    t = result["ttm"]
    return {"symbol": symbol, "name": result["entity"], "basis": result["basis"],
            "period_end": cur.get("end"), "fiscal": cur.get("fiscal"),
            "revenue_ttm_cr": t.get("revenue"), "pat_ttm_cr": t.get("pat"), "eps_ttm": t.get("eps_diluted"),
            "revenue_growth_yoy": cur.get("revenue_yoy"), "pat_growth_yoy": cur.get("pat_yoy"),
            "eps_growth_yoy": cur.get("eps_yoy"), "ebitda_margin": cur.get("ebitda_margin"),
            "pat_margin": cur.get("pat_margin"), "as_of": as_of,
            "source": result.get("source") if result.get("source") == PDF_SOURCE else SOURCE}


def write_metrics(path, rows, as_of):
    existing = {}
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            existing = {r["symbol"]: r for r in json.load(fh).get("companies", [])}
    merged = dict(existing)
    for r in rows:
        merged[r["symbol"]] = r
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"as_of": as_of, "companies": sorted(merged.values(), key=lambda r: r["symbol"])},
                  fh, indent=2)
        fh.write("\n")
    return len(merged)


def write_results(update, now, path=RESULTS_FILE):
    """trader/results.json: one document per symbol; a calendar run adds to a facts run."""
    doc = {}
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            doc = {}
    if doc.get("symbol") != update.get("symbol"):
        doc = {}
    doc = dict(doc, **update)
    doc["created_by"] = "trader-results"
    doc["as_of"] = now.replace(microsecond=0).isoformat()
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    return doc


# ---------------------------------------------------------------- the pasted transcript

TOPICS = [
    ("guidance", r"\bguidance\b|\boutlook\b|\bexpect(?:s|ed|ing)?\b|\bforecast\b|\btarget(?:s|ing)?\b|"
                 r"\bfor the (?:full |financial )?year\b|\bFY ?\d{2}\b|\bby (?:FY|20)\d{2}\b"),
    ("margins", r"\bmargins?\b|\bEBITDA\b|\bprofitab\w+|\brealisations?\b|\bpricing\b|\binput costs?\b|"
                r"\bcost pressures?\b|\bgross margin\b"),
    ("demand", r"\bdemand\b|\bvolumes?\b|\border (?:book|inflows?|intake|wins?)\b|\bpipeline\b|"
               r"\butili[sz]ation\b|\bfootfalls?\b|\bsubscribers?\b|\bdeal wins?\b|\bmarket share\b"),
    ("balance sheet", r"\bcapex\b|\bcapital expenditure\b|\bdebt\b|\bnet cash\b|\bborrowings?\b|"
                      r"\bleverage\b|\bworking capital\b|\bcash flows?\b|\bdividend\b|\bbuyback\b"),
    ("one-offs", r"\bexceptional\b|\bone[- ]?(?:off|time)\b|\bimpairment\b|\bwrite[- ]?(?:offs?|downs?|backs?)\b|"
                 r"\bprovision(?:s|ing)?\b|\bdeferred tax\b|\btax (?:credit|reversal)\b|\bESOP\b"),
    ("headwinds", r"\bheadwinds?\b|\bchalleng\w+|\bweak(?:ness|er)?\b|\bslow(?:down|er)?\b|\bpressure\b|"
                  r"\bdelay(?:s|ed)?\b|\bmonsoon\b|\btariffs?\b|\bregulat\w+|\bcompetiti\w+"),
    ("declined to answer", r"\b(?:not|don't|do not|cannot|can't|won't|will not|unable to)\s+(?:be able to\s+)?"
                           r"(?:comment|disclose|share|give|guide|quantify|break)|\bno comment\b|"
                           r"\bdecline[sd]? to\b|\bprefer not\b|\bconfidential\b"),
]
NUMBER_RX = re.compile(r"(?:Rs\.?|INR)\s?[\d,]+(?:\.\d+)?\s?(?:crores?|cr\b|lakhs?|bn|billion|mn|million)?|"
                       r"[\d,]+(?:\.\d+)?\s?(?:crores?|cr\b|lakhs?|million|billion)|"
                       r"\d+(?:\.\d+)?\s?(?:%|per ?cent|bps|basis points)", re.I)
MAX_PER_TOPIC = 4
MIN_SENTENCE = 20


def sentences(text):
    text = re.sub(r"\s+", " ", text.replace("\r", "\n")).strip()
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])", text)
    return [p.strip() for p in parts if len(p.strip()) > MIN_SENTENCE]


def summarise(text):
    """A keyword pass over a pasted commentary or transcript: what management said, by topic."""
    sents = sentences(text)
    if not sents:
        raise ResultsError("nothing to summarise: paste the commentary or transcript on stdin")
    topics = {}
    for topic, rx in TOPICS:
        hits = [s for s in sents if re.search(rx, s, re.I)]
        if hits:
            topics[topic] = hits[:MAX_PER_TOPIC]
    with_numbers = [s for s in sents if len(NUMBER_RX.findall(s)) >= 2][:MAX_PER_TOPIC]
    return {"sentences": len(sents), "words": len(text.split()), "topics": topics,
            "numbers": with_numbers,
            "caveat": "A keyword pass over the text you pasted, not an understanding of it: read each line "
                      "in its context, and check every number against the filed results."}


def render_summary(summary):
    out = ["Pasted text: %d words, %d sentences." % (summary["words"], summary["sentences"]), ""]
    for topic, _ in TOPICS:
        if topic in summary["topics"]:
            out.append("%s:" % topic.capitalize())
            for s in summary["topics"][topic]:
                out.append("  - " + s)
    if not summary["topics"]:
        out.append("No sentence on guidance, margins, demand, the balance sheet, one-offs or headwinds "
                   "was found. Is this the commentary or transcript?")
    if summary["numbers"]:
        out.append("")
        out.append("Lines with numbers:")
        for s in summary["numbers"]:
            out.append("  - " + s)
    out.append("")
    out.append(summary["caveat"])
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- the calendar

MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep",
                                            "oct", "nov", "dec"])}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTH_RX = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
DATE_RX = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + MONTH_RX + r"(?:,?\s+(\d{4}))?|"
                     r"\b" + MONTH_RX + r"\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b|"
                     r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b", re.I)
RESULT_WORDS = re.compile(r"\bresults?\b|\bearnings\b|\bQ[1-4]\b|\bquarter", re.I)
OUT_WORDS = re.compile(r"\bbeats?\b|\bmiss(?:es|ed)?\b|\bprofit (?:up|down|rises|falls|jumps|slips|drops)|"
                       r"\breports?\b|\bposts?\b|\bannounce[sd]\b|\bdeclares?\b|\bnet profit\b|\boutcome of board\b|"
                       r"\binvestor presentation\b|\bpress release\b|\bnewspaper publication", re.I)
AHEAD_WORDS = re.compile(r"\bdate\b|\bwhen\b|\bschedule|\bto announce\b|\bwill announce\b|\bpreview\b|"
                         r"\bexpectations?\b|\bto consider\b", re.I)
BOARD_WORDS = re.compile(r"board meeting|meeting of the board|\bto consider\b|\bto approve\b", re.I)
NAME_SUFFIXES = ("LT", "LTD", "LIMITED", "L")


def clean_name(kite_name):
    """Kite's 'TATA CONSULTANCY SERV LT' -> 'Tata Consultancy Serv' (a query, not a legal name)."""
    words = re.sub(r"[^A-Za-z0-9&' ]+", " ", kite_name or "").split()
    while words and words[-1].upper() in NAME_SUFFIXES:
        words.pop()
    return " ".join(w.capitalize() for w in words)


def norm_name(text):
    """Upper-case letters and digits only, for prefix matching ('Reliance Industries Ltd (500325)')."""
    text = re.sub(r"\(.*?\)", " ", text or "")
    return " ".join(re.sub(r"[^A-Z0-9 ]+", " ", text.upper()).split())


def name_matches(kite_name, title):
    want = norm_name(kite_name)
    for suffix in NAME_SUFFIXES:
        if want.endswith(" " + suffix):
            want = want[: -(len(suffix) + 1)]
    return bool(want) and norm_name(title).startswith(want)


def results_quarter(today):
    """The quarter whose results are due around this date (April to June: last year's Q4)."""
    return ("Q3", "Q3", "Q3", "Q4", "Q4", "Q4", "Q1", "Q1", "Q1", "Q2", "Q2", "Q2")[today.month - 1]


def google_url(query):
    return GOOGLE_NEWS + urllib.parse.urlencode({"q": query, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"})


def parse_pubdate(text):
    """RFC 822 (Google) or BSE's '29-Oct-2026 18:50:32' (already IST) -> aware IST datetime."""
    text = (text or "").strip()
    if not text:
        return None
    m = re.match(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?", text)
    if m and m.group(2).lower() in MONTHS:
        return datetime.datetime(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)),
                                 int(m.group(4)), int(m.group(5)), int(m.group(6) or 0), tzinfo=nse.IST)
    try:
        dt = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(nse.IST)


def parse_rss(text, source):
    try:
        root = etree.fromstring(text.lstrip("﻿").encode("utf-8"))
    except etree.ParseError as exc:
        raise ResultsError("%s feed is not well-formed XML (%s)" % (source, exc))
    items = []
    for item in root.iter("item"):
        def get(tag, item=item):
            return (item.findtext(tag) or "").strip()
        items.append({"title": get("title"), "link": get("link"), "description": get("description"),
                      "published": parse_pubdate(get("pubDate")), "scrip": get("scripcode"),
                      "source": source})
    return items


def dates_in(text, ref):
    """Dates a headline names, resolved against its publication date (a date), as ISO strings."""
    found = []
    low = (text or "").lower()
    for m in DATE_RX.finditer(text or ""):
        try:
            if m.group(1):
                day, mon, year = int(m.group(1)), MONTHS[m.group(2).lower()[:3]], m.group(3)
            elif m.group(4):
                day, mon, year = int(m.group(5)), MONTHS[m.group(4).lower()[:3]], m.group(6)
            else:
                day, mon, year = int(m.group(7)), int(m.group(8)), m.group(9)
            if year:
                d = datetime.date(int(year), mon, day)
            else:
                d = datetime.date(ref.year, mon, day)
                if (d - ref).days < -180:
                    d = datetime.date(ref.year + 1, mon, day)
                elif (d - ref).days > 180:
                    d = datetime.date(ref.year - 1, mon, day)
        except ValueError:
            continue
        found.append(d.isoformat())
    if re.search(r"\btoday\b", low):
        found.append(ref.isoformat())
    if re.search(r"\btomorrow\b", low):
        found.append((ref + datetime.timedelta(days=1)).isoformat())
    for i, name in enumerate(WEEKDAYS):
        if re.search(r"\b(?:on|this|next)\s+%s\b" % name, low):
            found.append((ref + datetime.timedelta(days=(i - ref.weekday()) % 7)).isoformat())
    return sorted(set(found))


def headline_rows(items, today):
    rows = []
    for it in items:
        pub = it["published"]
        ref = pub.date() if pub else today
        title = re.sub(r"\s+-\s+[^-]+\Z", "", it["title"])   # Google appends " - Publisher"
        publisher = it["title"][len(title):].strip(" -") if it["title"].startswith(title) else ""
        kind = "out" if OUT_WORDS.search(title) and not AHEAD_WORDS.search(title) else "ahead"
        rows.append({"title": title, "publisher": publisher, "link": it["link"],
                     "published": pub.isoformat() if pub else None,
                     "dates": dates_in(title, ref), "kind": kind})
    rows.sort(key=lambda r: r["published"] or "", reverse=True)
    return rows


def tally(rows, today, weight=1):
    counts = {}
    for r in rows:
        for d in r["dates"]:
            gap = (_date(d) - today).days
            if NEAR_DAYS[0] <= gap <= NEAR_DAYS[1]:
                counts[d] = counts.get(d, 0) + weight
    return counts


def bse_rows(items, kite_name, today):
    """Today's BSE announcements for this company that touch results or a board meeting."""
    rows, seen = [], set()
    for it in items:
        if not name_matches(kite_name, it["title"]):
            continue
        key = (it["link"], it["description"])
        if key in seen:
            continue
        seen.add(key)
        text = it["description"]
        if not (RESULT_WORDS.search(text) or BOARD_WORDS.search(text)):
            continue
        pub = it["published"]
        ref = pub.date() if pub else today
        m = re.search(r"scheduled (?:on|for)\s+(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", text, re.I)
        kind = "ahead" if (BOARD_WORDS.search(text) and "scheduled" in text.lower()) else "out"
        dates = [ref.isoformat()] if kind == "out" else []
        if m:
            try:
                dates = [datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()]
            except ValueError:
                dates = []
        rows.append({"company": it["title"], "scrip": it["scrip"], "text": text, "link": it["link"],
                     "published": pub.isoformat() if pub else None, "dates": dates, "kind": kind})
    return rows


def trading_days_between(a, b):
    """Trading days after a up to and including b (negative when b is before a)."""
    sign = 1 if b >= a else -1
    lo, hi = (a, b) if b >= a else (b, a)
    n, d = 0, lo
    while d < hi:
        d += datetime.timedelta(days=1)
        if nse.trading_day(d)[0]:
            n += 1
    return sign * n


def horizon(date, now):
    """('past' | 'today' | 'this_week' | 'later', trading days away, one plain sentence)."""
    today = now.date()
    n = trading_days_between(today, date)
    if date < today:
        return "past", n, ("%s was %d trading day%s ago: the result is out and the reaction is in the "
                           "price. Read the filed numbers (facts --file) before trading the follow-through."
                           % (date, -n, "" if n == -1 else "s"))
    if date == today:
        return "today", 0, ("The result is TODAY. Indian companies publish any time of the trading day or "
                            "after the close, and the stock moves within minutes of the filing. A day trade "
                            "in it today is a bet on the result, not on the chart.")
    same_week = date.isocalendar()[:2] == today.isocalendar()[:2]
    if same_week or n <= 5:
        return "this_week", n, ("The result is %s, %d trading day%s away%s. A day trade closed by flat-by "
                                "before that day is not exposed to it; nothing held overnight into it is "
                                "protected by a stop, the stock can open past it."
                                % (date, n, "" if n == 1 else "s", " (this week)" if same_week else ""))
    return "later", n, ("The result is %s, %d trading days away: outside a day trade's horizon. Check "
                        "again the week before." % (date, n))


CAVEAT = ("No official results calendar may be read by a script (the exchanges block them), so this is "
          "what headlines and today's BSE announcements say. Confirm on NSE (Corporates, Board Meetings) "
          "or BSE (Corporates, Board Meetings) before relying on it.")


def calendar(symbol, kite_name, now, offline=None, use_news=True, use_bse=True):
    """What the headlines and today's exchange announcements say about the next result."""
    today = now.date()
    name = clean_name(kite_name)
    out = {"symbol": symbol, "name": kite_name, "query_name": name, "as_of": now.isoformat(),
           "news": None, "bse": None, "problems": [], "dates": {}, "likely": None, "caveat": CAVEAT}
    counts = {}
    if use_news:
        queries = [("results", '"%s" %s results date' % (name, results_quarter(today))),
                   ("board", '"%s" board meeting financial results' % name)]
        rows = []
        for tag, query in queries:
            try:
                text = fetch(google_url(query), offline, "googlenews-%s-%s.xml" % (symbol, tag))
                rows.extend(headline_rows(parse_rss(text, "Google News"), today))
            except ResultsError as exc:
                out["problems"].append("Google News (%s): %s" % (tag, exc))
        seen, unique = set(), []
        for r in rows:
            if r["title"] not in seen:
                seen.add(r["title"])
                unique.append(r)
        unique.sort(key=lambda r: r["published"] or "", reverse=True)
        out["news"] = {"queries": [q for _, q in queries], "headlines": unique}
        counts = tally(unique, today)
    if use_bse:
        try:
            text = fetch(BSE_ANNOUNCEMENTS, offline, "bse-announcements.xml")
            items = bse_rows(parse_rss(text, "BSE"), kite_name, today)
            out["bse"] = {"items": items}
            for d, n in tally(items, today, BSE_WEIGHT).items():
                counts[d] = counts.get(d, 0) + n
        except ResultsError as exc:
            out["problems"].append("BSE announcements: %s" % exc)
    out["dates"] = counts
    if counts:
        best = max(sorted(counts), key=lambda d: counts[d])
        kind, n, sentence = horizon(_date(best), now)
        out["likely"] = {"date": best, "mentions": counts[best], "horizon": kind,
                         "trading_days": n, "answer": sentence}
    return out


def render_calendar(cal, pinned=None):
    out = ["%s (%s): the next result" % (cal["name"], cal["symbol"]), ""]
    if pinned:
        out.append("Your date %s: %s" % (pinned["date"], pinned["answer"]))
        out.append("")
    bse = cal.get("bse")
    if bse and bse["items"]:
        out.append("BSE announcements today (the exchange's own feed):")
        for r in bse["items"]:
            when = r["published"][11:16] if r["published"] else "    "
            out.append("  %s  %s: %s" % (when, r["company"], r["text"][:160]))
            if r["dates"]:
                out.append("         -> %s %s" % ("board meeting on" if r["kind"] == "ahead" else "result out",
                                                 ", ".join(r["dates"])))
    elif bse is not None:
        out.append("BSE announcements today: nothing from this company about results or a board meeting.")
    news = cal.get("news")
    if news and news["headlines"]:
        out.append("")
        out.append("Headlines (Google News, newest first, IST):")
        for r in news["headlines"][:MAX_HEADLINES]:
            when = r["published"][:16].replace("T", " ") if r["published"] else "undated"
            tag = " [out]" if r["kind"] == "out" else ""
            dates = " -> " + ", ".join(r["dates"]) if r["dates"] else ""
            out.append("  %s  %s (%s)%s%s" % (when, r["title"], r["publisher"] or "?", tag, dates))
    elif news is not None:
        out.append("")
        out.append("Headlines: none for %s." % " / ".join(news["queries"]))
    for p in cal["problems"]:
        out.append("  (source down: %s)" % p)
    out.append("")
    if cal.get("likely"):
        lk = cal["likely"]
        out.append("Most mentioned date: %s (%d mention%s). %s"
                   % (lk["date"], lk["mentions"], "" if lk["mentions"] == 1 else "s", lk["answer"]))
    elif not pinned:
        out.append("No date found. Look at the company's investor page or the exchange's board-meeting "
                   "list, then run  calendar %s --date YYYY-MM-DD." % cal["symbol"])
    out.append("")
    out.append(cal["caveat"])
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- the BSE results PDF (SEBI LODR Regulation 33 statement)

BSE_PDF_CACHE = os.path.join("trader", "cache", "bse-pdf")
FEED_CACHE = os.path.join("trader", "cache")
BSE_CACHE_MINUTES = 10         # the announcements feed is reused this long (news.py keeps the same cache)
PDFTOTEXT_TIMEOUT = 60
PDF_SOURCE = "bse-pdf"
PDF_MAX_TRIES = 3              # results filings tried from the feed before giving up
SPARSE_LINES_PER_PAGE = 8      # fewer text lines per page than this: a scanned PDF
PLAUSIBLE_RATIO = 25.0         # a revenue or expense figure this far from its row-mates is an OCR slip
PLAUSIBLE_KEYS = ("revenue", "total_income", "expenses")
YEAR_PAIR_CHARS = 10           # a bare year this close (columns) under a day-month belongs to it
YEAR_PAIR_LINES = 3
MAX_BLOCK_PAGES = 2            # a statement may run over the page
CUMULATIVE_DAYS = (150, 380)   # a half-year, nine-month or full-year column
TITLE_RX = re.compile(r"statement\s+of|financial\s+results|results\s+for\s+the|(?:un)?audited\s+results", re.I)
GROUP_LABEL_RX = re.compile(r"(?:quarter|half|nine|months?|year)[\s\w/]*ended", re.I)
RESULTS_FILING_RX = re.compile(r"financial\s+results?|un-?\s?audited|audited\s+results|results\s+for\s+the", re.I)
NOT_RESULTS_RX = re.compile(r"presentation|conference|earnings\s+call|transcript|recording|newspaper|advertisement|"
                            r"publication|scheduled|to\s+consider|corrigendum|record\s+date|press\s+release|"
                            r"clarification|trading\s+window|credit\s+rating|investor\s+meet|analyst", re.I)
CONSOLIDATED_HINT_RX = re.compile(r"non-?\s?controlling\s+interest|minority\s+interest|"
                                  r"share\s+(?:of|in)\s+(?:net\s+)?profit.{0,40}(?:joint\s+venture|associate)", re.I)
PDF_KEYS = ("revenue", "other_income", "total_income", "expenses", "finance_costs", "depreciation",
            "pbt_before_exceptional", "exceptional", "pbt", "tax", "pat", "eps_basic", "eps_diluted")
PDF_ROW_NAMES = {"revenue": "revenue from operations", "other_income": "other income", "total_income": "total income",
                 "expenses": "total expenses", "finance_costs": "finance costs", "depreciation": "depreciation",
                 "pbt_before_exceptional": "profit before exceptional items and tax", "exceptional": "exceptional items",
                 "pbt": "profit before tax", "tax": "tax expense", "pat": "net profit for the period",
                 "eps_basic": "basic EPS", "eps_diluted": "diluted EPS"}
PDF_SCALES = dict(SCALES, lacs=1e5, lac=1e5, cr=1e7, crs=1e7, mn=1e6, mio=1e6)
UNIT_WORDS = {"lac": "lakhs", "lacs": "lakhs", "lakh": "lakhs", "crore": "crores", "cr": "crores", "crs": "crores",
              "million": "millions", "mn": "millions", "mio": "millions", "thousand": "thousands",
              "'000": "thousands", "000s": "thousands"}
UNIT_RX = re.compile(r"\b(lakhs?|lacs?|crores?|crs?|millions?|mn|mio|thousands?)[a-z]?\b|('000|000s)", re.I)
RUPEE_RX = re.compile("\\brs\\b|rupees|\\binr\\b|\\bre\\b|\\u20b9|~|amount|\\bin\\b", re.I)
RUPEES_ONLY_RX = re.compile(r"(?:amounts?\s+)?in\s+(?:rs\.?|rupees|inr|₹)\s*\)?\s*$", re.I)
DMY_RX = re.compile(r"(?<![\d.])(\d{1,2})[./-](\d{1,2})[./-](\d{4}|\d{2})(?![\d.])")
DMON_RX = re.compile(r"(?<![\w])(\d{1,2})(?:st|nd|rd|th)?[\s.,-]*" + MONTH_RX +
                     r"(?:[\s.,-]*(\d{4})(?!\d)|[-.](\d{2})(?!\d))?", re.I)
MOND_RX = re.compile(r"(?<![\w])" + MONTH_RX + r"[\s.,-]*(\d{1,2})(?:st|nd|rd|th)?(?:[\s.,-]*(\d{4})(?!\d))?(?!\d)", re.I)
MONTH_ONLY_RX = re.compile(r"(?<![\w])" + MONTH_RX + r"(?![\w\d])", re.I)
BARE_YEAR_RX = re.compile(r"(?<![\d.,/-])(20\d{2})(?![\d.,/-])")
DAY_YEAR_RX = re.compile(r"(?<![\d.,/-])(\d{1,2})\s*,\s*(20\d{2})(?![\d])")
NUM_RX = re.compile(r"^\(?-?(?:\d{1,3}(?:,\d{2,3})*|\d+)(?:\.\d+)?\)?$")
NIL_RX = re.compile("^[-\\u2013\\u2014.]{1,3}$|^nil$", re.I)
REF_RX = re.compile(r"^\(?[\divxl]{1,4}\)?(?:\s*[-+]\s*\(?[\divxl]{1,4}\)?)+$", re.I)
SERIAL_RX = re.compile("^\\s*(?:(?:[-\u2022\u2013*]|\\(?(?:\\d{1,2}|[ivxl]{1,4}|[a-z])[.):]?\\)?)(?:\\s+|$))+", re.I)
CELL_RX = re.compile(r"\S+(?: \S+)*")
EPS_CONTEXT_RX = re.compile(r"earning|\beps\b|per\s+(?:equity\s+)?share")
PROFIT = r"^(?:net )?profit\w*(?: [a-z]{1,2})?(?: loss)?"   # 'Profit / (Loss)', OCR'd 'Profitl(Loss)', 'Profit I (Loss)'
ROW_RX = [   # (key, regex on the normalised label); the first key that matches wins, so the order matters
    ("revenue", re.compile(r"^(?:[a-z]{1,3} )?(?:total |net )?(?:revenue|income|sales|turnover)\w*(?: turnover)? from oper|"
                           r"^interest earned")),
    ("other_income", re.compile(r"^other income")),
    ("total_income", re.compile(r"^total (?:income|revenue)(?! from)")),
    ("expenses", re.compile(r"^total expen")),
    ("finance_costs", re.compile(r"^finance costs?")),
    ("depreciation", re.compile(r"^depreciation")),
    ("pbt_before_exceptional", re.compile(PROFIT + r"(?: from ordinary activities)? before (?:exceptional|extraordinary)|" +
                                          PROFIT + r" before tax and (?:exceptional|extraordinary)")),
    ("exceptional", re.compile(r"^(?:add |less |add less )?exceptional items?")),
    ("pbt", re.compile(PROFIT + r"(?: from (?:ordinary activities|continuing operations))? before tax")),
    ("tax", re.compile(r"^(?:less )?(?:total |net )?(?:income )?tax expenses?|^total tax|^tax expenses? credit")),
    ("pat", re.compile(PROFIT + r"(?: from (?:ordinary activities|continuing operations))? "
                       r"(?:for the (?:period|quarter|year)|after tax)")),
]
EPS_BASIC_RX = re.compile(r"^basic\b|(?:eps|earnings? per (?:equity )?share)\b[^a-z]*(?:\w+ )?basic\b")
EPS_DILUTED_RX = re.compile(r"^diluted\b|(?:eps|earnings? per (?:equity )?share)\b[^a-z]*(?:\w+ )?diluted\b")


def pdftotext_exe():
    return shutil.which("pdftotext")


def _pdf_to_text(path):
    """pdftotext -layout on a PDF -> text. Tests swap this."""
    exe = pdftotext_exe()
    if not exe:
        raise ResultsError("pdftotext is not installed: brew install poppler (Debian: apt install poppler-utils), "
                           "or download the XBRL from the exchange and pass --file")
    try:
        done = subprocess.run([exe, "-layout", path, "-"], capture_output=True, timeout=PDFTOTEXT_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ResultsError("pdftotext failed on %s: %s" % (path, exc))
    if done.returncode != 0:
        raise ResultsError("pdftotext could not read %s: %s" % (
            path, done.stderr.decode("utf-8", "replace").strip()[:200] or "exit %d" % done.returncode))
    return done.stdout.decode("utf-8", "replace")


def fetch_bytes(url):
    """GET url as bytes with the plainly identified client: no cookies, no retry, nseindia.com refused."""
    host = urllib.parse.urlsplit(url).hostname or ""
    if host == "nseindia.com" or host.endswith(".nseindia.com"):
        raise ResultsError("nseindia.com is out: NSE's terms forbid scripted access. Download the file in your "
                           "browser and pass --pdf PATH or --file PATH")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with _urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        hint = " (not retried; open the link in your browser and pass the saved file with --pdf PATH)" \
            if exc.code in (401, 403, 406, 429) else ""
        raise ResultsError("HTTP %d for %s%s" % (exc.code, url, hint))
    except urllib.error.URLError as exc:
        raise ResultsError("could not reach %s: %s" % (url, exc.reason))
    except OSError as exc:
        raise ResultsError("could not read %s: %s" % (url, exc))


def pdf_guid(url):
    """The filing's id from BSE's link (.../AttachLive/<guid>.pdf), or a hash of any other URL."""
    name = os.path.basename(urllib.parse.urlsplit(url).path)
    if name.lower().endswith(".pdf"):
        name = name[:-4]
    if re.fullmatch(r"[A-Za-z0-9._-]{8,120}", name or ""):
        return name
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def cached_pdf(url, cache_dir=BSE_PDF_CACHE):
    """trader/cache/bse-pdf/<guid>.pdf: fetched once, then read from disk. -> (path, fetched now?)"""
    path = os.path.join(cache_dir, pdf_guid(url) + ".pdf")
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return path, False
    data = fetch_bytes(url)
    if not data.startswith(b"%PDF"):
        raise ResultsError("%s is not a PDF (it starts with %r)" % (url, data[:12]))
    os.makedirs(cache_dir, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path, True


def bse_feed_cached(now, offline=None, cache_dir=FEED_CACHE):
    """The day's BSE announcements XML: --offline DIR/bse-announcements.xml, else the file every tool in the
    pack shares (trader/cache/bse-announcements-<date>-<HHMM>.xml, fresh for BSE_CACHE_MINUTES), else fetched."""
    if offline:
        return fetch(None, offline, "bse-announcements.xml")
    fresh = sorted(p for p in glob.glob(os.path.join(cache_dir, "bse-announcements-*.xml"))
                   if time.time() - os.path.getmtime(p) < BSE_CACHE_MINUTES * 60)
    if fresh:
        with open(fresh[-1], encoding="utf-8", errors="replace") as fh:
            return fh.read()
    text = fetch(BSE_ANNOUNCEMENTS)
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, "bse-announcements-%s.xml" % now.strftime("%Y-%m-%d-%H%M"))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return text


def results_filings(items, kite_name, code=None):
    """Today's filings by this company (by scrip code when given, else by Kite's name, as the news tool matches)
    -> (the ones that look like a results PDF, best first; every filing of the company)."""
    mine, seen = [], set()
    for it in items:
        hit = (it["scrip"] == str(code)) if code else name_matches(kite_name, it["title"])
        if not hit or not it["link"] or it["link"] in seen:
            continue
        seen.add(it["link"])
        mine.append(it)
    scored = []
    for it in mine:
        text = it["description"]
        score = 0
        if RESULTS_FILING_RX.search(text):
            score += 3
        if re.search(r"statement|regulation 33|reg\.? 33", text, re.I):
            score += 1
        if re.search(r"outcome", text, re.I):
            score -= 1
        if NOT_RESULTS_RX.search(text):
            score -= 5
        if it["link"].lower().endswith(".pdf"):
            score += 1
        scored.append((score, it))
    floor = datetime.datetime.min.replace(tzinfo=nse.IST)
    scored.sort(key=lambda s: (-s[0], -(s[1]["published"] or floor).timestamp()))
    return [it for score, it in scored if score > 2], mine


# ---- reading the statement out of pdftotext -layout text

def _cells(line):
    """[(start, end, text)] for the runs of text separated by two or more spaces (pdftotext -layout columns)."""
    return [(m.start(), m.end(), m.group(0)) for m in CELL_RX.finditer(line)]


def _valid_grouping(digits):
    groups = digits.split(",")
    if len(groups) == 1:
        return True
    if len(groups[-1]) != 3 or not 1 <= len(groups[0]) <= 3:
        return False
    return all(len(g) in (2, 3) for g in groups[1:-1])


def classify_cell(text):
    """('num', value) | ('nil', 0.0) | ('bad', None) for an unreadable number | ('text', None)."""
    s = text.strip()
    if NIL_RX.match(s):
        return "nil", 0.0
    if REF_RX.match(s) or s.endswith("%"):
        return "text", None
    compact = s.replace(" ", "")
    if NUM_RX.match(compact) and ("." in compact or "," in compact or " " not in s):
        body = compact.strip("()")
        if _valid_grouping(body.lstrip("-").split(".")[0]):
            value = float(body.replace(",", "").lstrip("-") or 0)
            if compact.startswith("(") or body.startswith("-"):
                value = -value
            return "num", value
    digits, letters = len(re.findall(r"\d", s)), len(re.findall(r"[A-Za-z]", s))
    if digits and len(compact) <= 16:
        junk = len(re.findall(r"[^\dA-Za-z,.()\s-]", s))
        if letters <= 2 and junk <= 2 and digits >= letters:
            return "bad", None
    return "text", None


def split_row(line):
    """A statement line -> (label, [(center, kind, value, text)]): the text cells make the label, the number
    cells (to the right of it, or to the left in the rare layout that prints them first) are the values."""
    cells = [(a, b, t) + classify_cell(t) for a, b, t in _cells(line)]
    label = " ".join(c[2] for c in cells if c[3] == "text")
    values = [((c[0] + c[1]) / 2.0, c[3], c[4], c[2]) for c in cells if c[3] != "text"]
    return label, values


def strip_serial(label):
    return SERIAL_RX.sub("", label, count=1).strip()


def norm_label(label):
    return " ".join(re.sub(r"[^a-z ]+", " ", strip_serial(label).lower()).split())


def match_row(norm, eps_context):
    if eps_context or EPS_CONTEXT_RX.search(norm):
        if EPS_BASIC_RX.search(norm):
            return ["eps_basic", "eps_diluted"] if "diluted" in norm else ["eps_basic"]
        if EPS_DILUTED_RX.search(norm):
            return ["eps_diluted"]
    for key, rx in ROW_RX:
        if rx.search(norm):
            return [key]
    return []


def _month_end(year, month):
    year += (month - 1) // 12
    month = (month - 1) % 12 + 1
    return datetime.date(year, month, calendar_module.monthrange(year, month)[1])


def _make_date(day, month, year):
    year = int(year)
    if year < 100:
        year += 2000
    if not 1990 <= year <= 2099:
        return None
    try:
        return datetime.date(year, int(month), int(day))
    except ValueError:
        return None


def dates_on_line(line):
    """Every date on the line as (center, day, month, year, span); day or year is None when the header prints
    them on another line ('30 September' over '2026', 'September' over '30,2026')."""
    out, spans = [], []

    def free(m):
        return not any(m.start() < e and m.end() > s for s, e in spans)

    for m in DMY_RX.finditer(line):
        if free(m) and _make_date(m.group(1), m.group(2), m.group(3)):
            spans.append((m.start(), m.end()))
            out.append(((m.start() + m.end()) / 2.0, int(m.group(1)), int(m.group(2)), int(m.group(3)) + (2000 if len(m.group(3)) == 2 else 0), spans[-1]))
    for m in DMON_RX.finditer(line):
        day, month, year = int(m.group(1)), MONTHS[m.group(2).lower()[:3]], m.group(3) or m.group(4)
        if free(m) and 1 <= day <= 31 and (not year or _make_date(day, month, year)):
            spans.append((m.start(), m.end()))
            out.append(((m.start() + m.end()) / 2.0, day, month, int(year) + (2000 if year and len(year) == 2 else 0) if year else None, spans[-1]))
    for m in MOND_RX.finditer(line):
        month, day, year = MONTHS[m.group(1).lower()[:3]], int(m.group(2)), m.group(3)
        if free(m) and 1 <= day <= 31 and (not year or _make_date(day, month, year)):
            spans.append((m.start(), m.end()))
            out.append(((m.start() + m.end()) / 2.0, day, month, int(year) if year else None, spans[-1]))
    months_only = [m for m in MONTH_ONLY_RX.finditer(line) if free(m)]
    if len(months_only) >= 2:
        for m in months_only:
            spans.append((m.start(), m.end()))
            out.append(((m.start() + m.end()) / 2.0, None, MONTHS[m.group(1).lower()[:3]], None, spans[-1]))
    out.sort(key=lambda d: d[0])
    return out


def header_columns(lines):
    """Column dates from a statement's header lines, left to right, as [(center, date)]. A 'Quarter ended
    30.09.2026' group label is ignored when the columns carry their own dates."""
    per_line, completions = [], []     # completions: [line, center, day or None, year, used]
    for i, line in enumerate(lines):
        if TITLE_RX.search(line):
            per_line.append([])
            continue
        dated = dates_on_line(line)
        per_line.append(dated)
        taken = [d[4] for d in dated]
        for m in DAY_YEAR_RX.finditer(line):
            completions.append([i, (m.start() + m.end()) / 2.0, int(m.group(1)), int(m.group(2)), False])
        for m in BARE_YEAR_RX.finditer(line):
            if not any(m.start() < e and m.end() > s for s, e in taken):
                completions.append([i, (m.start() + m.end()) / 2.0, None, int(m.group(1)), False])
    most = max([len(d) for d in per_line] or [0])
    found = []
    for i, dated in enumerate(per_line):
        if most >= 3 and len(dated) <= 2 and GROUP_LABEL_RX.search(lines[i]):
            continue
        for center, day, month, year, span in dated:
            if year is None or day is None:
                best = None
                for c in completions:
                    if c[4] or not i < c[0] <= i + YEAR_PAIR_LINES or abs(c[1] - center) > YEAR_PAIR_CHARS:
                        continue
                    if day is None and c[2] is None:
                        continue
                    if best is None or abs(c[1] - center) < abs(best[1] - center):
                        best = c
                if best is None:
                    continue
                best[4] = True
                day, year = day if day is not None else best[2], best[3]
            d = _make_date(day, month, year)
            if d is not None:
                found.append((center, d))
    found.sort(key=lambda f: f[0])
    columns = []
    for center, d in found:
        if columns and abs(center - columns[-1][0]) < 3:
            continue
        columns.append((center, d))
    return columns


def column_spans(dates, anchor=None):
    """Column dates in order -> [{'date', 'start', 'end', 'kind'}]. The first time a date appears it is a
    quarter (this one, the preceding, the year-earlier); the second time a year-to-date column; a date that
    is none of those is the full year. This is SEBI's Regulation 33 column layout."""
    if not dates:
        return []
    q = anchor if anchor in dates else dates[0]
    q_prev, q_year = _month_end(q.year, q.month - 3), _month_end(q.year - 1, q.month)
    extras = sorted({d for d in dates if d not in (q, q_prev, q_year)}, reverse=True)
    if extras:
        fy_end_month = extras[0].month
    elif dates.count(q_prev) > 1:
        fy_end_month = q_prev.month
    elif dates.count(q) > 1:
        fy_end_month = q.month
    else:
        fy_end_month = 3
    seen, out = {}, []
    for d in dates:
        n = seen.get(d, 0)
        seen[d] = n + 1
        if n == 0 and d in (q, q_prev, q_year):
            start, kind = _month_end(d.year, d.month - 3) + datetime.timedelta(days=1), "quarter"
        else:
            fy_start_month = fy_end_month % 12 + 1
            start_year = d.year if (d.month > fy_end_month or fy_end_month == 12) else d.year - 1
            start, kind = datetime.date(start_year, fy_start_month, 1), "cumulative"
        out.append({"date": d.isoformat(), "start": start.isoformat(), "end": d.isoformat(), "kind": kind})
    return out


def span_label(col):
    if col["kind"] == "quarter":
        return "quarter to %s" % col["end"]
    words = {"Q1": "quarter", "H1": "half year", "9M": "nine months", "the year": "year"}
    return "%s to %s" % (words[ytd_label((col["start"], col["end"]))], col["end"])


def unit_of(lines):
    """(factor, word, the line) from a 'Rs. in lakhs' style line, or (None, None, None)."""
    for line in lines:
        s = " ".join(line.split())
        if len(s) > 120 or not RUPEE_RX.search(s):
            continue
        m = UNIT_RX.search(s)
        if m:
            word = (m.group(1) or m.group(2)).lower()
            word = UNIT_WORDS.get(word, word)
            return PDF_SCALES[word], word, s
        if RUPEES_ONLY_RX.search(s) and len(s) < 60:
            return 1.0, "rupees", s
    return None, None, None


def document_unit(pages):
    """The unit every page of the filing agrees on, as (factor, word, line, page), else (None,) * 4."""
    found = {}
    for n, page in enumerate(pages):
        factor, word, line = unit_of(page.splitlines())
        if word and word not in found:
            found[word] = (factor, word, line, n + 1)
    if len(found) == 1:
        return list(found.values())[0]
    return None, None, None, None


def assign_values(values, centers, zone=(None, None)):
    """Number cells -> one per column (None where unread): by order when the counts match, else by position.
    Cells left of the first column (serial numbers) and outside the zone (the other basis, side by side)
    are ignored."""
    n = len(centers)
    gaps = [b - a for a, b in zip(centers, centers[1:])] or [16.0]
    spacing = sorted(gaps)[len(gaps) // 2]
    limits = [max(4.0, min(gaps[max(0, i - 1)], gaps[min(i, len(gaps) - 1)]) / 2.0) for i in range(n)]
    lo = centers[0] - spacing if zone[0] is None else max(zone[0], centers[0] - spacing)
    inside = [v for v in values if v[0] >= lo and (zone[1] is None or v[0] < zone[1])]
    out = [None] * n
    if len(inside) == n:
        return [v for v in inside], len(inside)
    claims = {}
    for v in inside:
        gap, i = min((abs(v[0] - c), i) for i, c in enumerate(centers))
        if gap > limits[i] or (i in claims and claims[i][0] <= gap):
            continue
        claims[i] = (gap, v)
    for i, (gap, v) in claims.items():
        out[i] = v
    return out, len(inside)


def _decimals(text):
    m = re.search(r"\.(\d+)\)?$", text.replace(" ", ""))
    return len(m.group(1)) if m else 0


def implausible(assigned, key):
    """Column indexes whose number is an OCR slip: more decimals than the rest of the row (12.16135 for
    12,161.35), or, for revenue and expense rows, many times off the row's other figures (34638 for 346.38)."""
    cells = [(i, a) for i, a in enumerate(assigned) if a is not None and a[1] == "num"]
    out = []
    decs = [_decimals(a[3]) for i, a in cells]
    if decs and max(decs) > 2 and sorted(decs)[len(decs) // 2] <= 2:
        out += [i for (i, a), d in zip(cells, decs) if d > 2]
    if key in PLAUSIBLE_KEYS:
        nums = [abs(a[2]) for i, a in cells if a[2] and i not in out]
        if len(nums) >= 3:
            med = sorted(nums)[len(nums) // 2]
            out += [i for i, a in cells if a[2] and i not in out
                    and (abs(a[2]) < med / PLAUSIBLE_RATIO or abs(a[2]) > med * PLAUSIBLE_RATIO)]
    return out


def _continues(last, assigned):
    """A line without a label of its own whose numbers fall only under the columns the row above left blank."""
    landed = [j for j, a in enumerate(assigned) if a is not None]
    return bool(landed) and all(last["assigned"][j] is None for j in landed)


def _basis_words(text):
    low = text.lower()
    return {w for w in ("consolidated", "standalone") if w in low}


def _read_rows(parsed, lines, header_end, centers, zone=(None, None)):
    """The key rows of one column group -> ({key: {label, line, assigned}}, number cells per row (mode))."""
    rows, eps_context, last, widths = {}, False, None, []
    for i in range(header_end, len(parsed)):
        label, values = parsed[i]
        if not values:
            if EPS_CONTEXT_RX.search(norm_label(label)):
                eps_context = True
            if label.strip():
                last = None
            continue
        own = strip_serial(label)
        if not own:   # the numbers sit on their own line: the label is the line above (and below)
            before = strip_serial(lines[i - 1]) if i > 0 and not parsed[i - 1][1] else ""
            after = strip_serial(parsed[i + 1][0]) if i + 1 < len(parsed) and not parsed[i + 1][1] else ""
            own = (before + " " + after).strip()
        norm = norm_label(own)
        if EPS_CONTEXT_RX.search(norm):
            eps_context = True
        keys = [k for k in match_row(norm, eps_context) if k not in rows]
        assigned, width = assign_values(values, centers, zone)
        if not keys:
            if last is not None and _continues(last, assigned):
                for j, a in enumerate(assigned):
                    if a is not None:
                        last["assigned"][j] = a
            continue
        widths.append(width)
        last = {"assigned": assigned, "label": " ".join(own.split()), "line": i}
        for key in keys:
            rows[key] = last
    mode = max(set(widths), key=widths.count) if widths else 0
    return rows, mode


def _finish_rows(rows, spans):
    """Drop unreadable and implausible cells; -> ({key: {label, line, values}}, [unread notes])."""
    done, unread = {}, []
    for key in PDF_KEYS:
        row = rows.get(key)
        if row is None:
            unread.append({"row": key, "label": None, "columns": [], "why": "no row found"})
            continue
        assigned = row["assigned"]
        bad = [j for j, a in enumerate(assigned) if a is not None and a[1] == "bad"]
        odd = implausible(assigned, key)
        cells = [None if (a is None or j in bad or j in odd) else a[2] for j, a in enumerate(assigned)]
        missing = [j for j, c in enumerate(cells) if c is None]
        done[key] = {"label": row["label"], "line": row["line"], "values": cells}
        if missing and not (key == "exceptional" and not bad and not odd):
            why = "no number under the column"
            if bad and set(missing) <= set(bad):
                why = "unreadable number"
            elif odd and set(missing) <= set(odd):
                why = "looks wrong against the rest of the row (an OCR slip)"
            unread.append({"row": key, "label": row["label"], "columns": [span_label(spans[j]) for j in missing], "why": why})
    return done, unread


def parse_statement(lines, page, doc_unit=(None, None, None, None)):
    """One statement's lines (a page, maybe two) -> [statement dicts], one per basis found in the columns.
    A statement whose columns or profit rows could not be read comes back with 'problem' set."""
    parsed = [split_row(line) for line in lines]
    header_end = None
    for i, (label, values) in enumerate(parsed):
        if len(values) >= 2 and match_row(norm_label(label), False):
            header_end = i
            break
    if header_end is None:
        return []
    header = lines[:header_end]
    title_idx = next((i for i, l in enumerate(header) if TITLE_RX.search(l)), None)
    title = " ".join(header[title_idx].split()) if title_idx is not None else ""
    title_text = title + " " + (" ".join(header[title_idx + 1].split())
                                if title_idx is not None and title_idx + 1 < len(header) else "")
    columns = header_columns(header)
    anchor = next((_make_date(day, month, year) for center, day, month, year, span in dates_on_line(title_text)
                   if day and year), None)
    factor, unit, unit_line, unit_page = unit_of(header) + (page,)
    if factor is None:
        factor, unit, unit_line, unit_page = unit_of(lines) + (page,)
    if factor is None:
        factor, unit, unit_line, unit_page = doc_unit
    low = title_text.lower()
    audited = "unaudited" if "unaudited" in low else ("audited" if "audited" in low else None)
    groups = [("single", columns, (None, None))]   # side by side: a header line naming both bases splits the columns
    for line in header:
        if TITLE_RX.search(line):
            continue
        words = sorted((m.start(), m.group(0).lower()) for m in re.finditer(r"standalone|consolidated", line, re.I))
        if len(words) >= 2 and len(columns) >= 6 and len(columns) % 2 == 0:
            boundary = (words[0][0] + words[1][0]) / 2.0 + len(words[0][1]) / 2.0
            left = [c for c in columns if c[0] < boundary]
            right = [c for c in columns if c[0] >= boundary]
            if len(left) == len(right):
                groups = [(words[0][1], left, (None, boundary)), (words[1][1], right, (boundary, None))]
                break
    title_basis = _basis_words(title_text)
    body = "\n".join(lines[header_end:])
    out = []
    for gname, cols, zone in groups:
        if gname == "single":
            basis = list(title_basis)[0] if len(title_basis) == 1 else None
        else:
            basis = gname
        inferred = False
        if basis is None and CONSOLIDATED_HINT_RX.search(body):
            basis, inferred = "consolidated", True
        stmt = {"page": page, "title": title or None, "basis": basis, "basis_inferred": inferred, "audited": audited,
                "unit": unit, "unit_factor": factor, "unit_line": unit_line, "unit_page": unit_page,
                "columns": [], "rows": {}, "unread": [], "bank": False, "problem": None,
                "side_by_side": gname != "single"}
        centers = [c[0] for c in cols]
        if not centers:
            stmt["problem"] = "no column dates could be read in the header"
            out.append(stmt)
            continue
        spans = column_spans([c[1] for c in cols], anchor)
        rows, mode = _read_rows(parsed, lines, header_end, centers, zone)
        stmt["columns"] = spans
        dates_read = ", ".join(c["date"] for c in spans)
        if mode > len(spans):
            stmt["problem"] = ("%d columns of numbers under %d readable column dates (%s): the header could not be "
                               "read" % (mode, len(spans), dates_read))
        elif not any(c["kind"] == "quarter" for c in spans) or any(
                c["kind"] == "cumulative" and not CUMULATIVE_DAYS[0] <= days((c["start"], c["end"])) <= CUMULATIVE_DAYS[1]
                for c in spans):
            stmt["problem"] = "the column dates (%s) do not form a Regulation 33 layout" % dates_read
        elif not (("revenue" in rows or "total_income" in rows) and ("pat" in rows or "pbt" in rows)):
            stmt["problem"] = "its profit rows could not be read (rows found: %s)" % (
                ", ".join(PDF_ROW_NAMES[k] for k in PDF_KEYS if k in rows) or "none")
        stmt["rows"], stmt["unread"] = _finish_rows(rows, spans)
        stmt["bank"] = bool(rows.get("revenue")) and norm_label(rows["revenue"]["label"]).startswith("interest earned")
        out.append(stmt)
    return out


def parse_results_pdf(text):
    """pdftotext -layout output -> {'statements': usable ones, 'unusable': the rest, 'pages', 'text_lines',
    'scanned', 'entity'}."""
    pages = text.split("\f")
    if pages and not pages[-1].strip():
        pages = pages[:-1]
    text_lines = sum(1 for p in pages for l in p.splitlines() if l.strip())
    doc_unit = document_unit(pages)
    statements, unusable, entity = [], [], None
    for i, page in enumerate(pages):
        lines = page.splitlines()
        for extra in range(1, MAX_BLOCK_PAGES):
            nxt = pages[i + extra].splitlines() if i + extra < len(pages) else None
            if nxt and not any(TITLE_RX.search(l) for l in nxt[:30]) and not header_columns(nxt[:40]):
                lines = lines + nxt
        for stmt in parse_statement(lines, i + 1, doc_unit):
            if stmt["problem"] and not ("result" in (stmt["title"] or "").lower() or "revenue" in stmt["rows"]
                                        or "total_income" in stmt["rows"]):
                continue      # a cash-flow or balance-sheet page, not a results table
            (unusable if stmt["problem"] else statements).append(stmt)
            if entity is None:
                for l in page.splitlines()[:12]:
                    if re.search(r"\b(limited|ltd\.?|bank)\b", l, re.I) and len(l.strip()) < 80 \
                            and not TITLE_RX.search(l) and not re.search(r"\d{3}", l):
                        entity = " ".join(l.split())
                        break
    scanned = not statements and not unusable and text_lines < SPARSE_LINES_PER_PAGE * max(1, len(pages))
    return {"statements": statements, "unusable": unusable, "pages": len(pages), "text_lines": text_lines,
            "scanned": scanned, "entity": entity}


def describe_problems(parsed, link=None):
    """One plain sentence on why nothing could be read, for the error line."""
    where = " Open it yourself: %s" % link if link else ""
    if parsed["scanned"]:
        return ("the PDF has no text layer (%d lines of text over %d pages: a scanned filing); the numbers "
                "cannot be read by a script.%s" % (parsed["text_lines"], parsed["pages"], where))
    if parsed["unusable"]:
        bits = ["page %d (%s): %s" % (s["page"], s["basis"] or "basis not stated", s["problem"]) for s in parsed["unusable"]]
        return "a results statement was found but not read: %s.%s" % ("; ".join(bits), where)
    return ("no results statement found in the PDF's text (%d pages, %d lines): looked for a page with a revenue "
            "or interest-earned row and a profit row under column dates.%s" % (parsed["pages"], parsed["text_lines"], where))


def choose_statement(parsed, wanted=None):
    """The statement to use: --consolidated/--standalone, else consolidated, else standalone, else the first."""
    stmts = parsed["statements"]
    if not stmts:
        raise ResultsError(describe_problems(parsed))
    labelled = {s["basis"] for s in stmts if s["basis"]}
    if wanted:
        for s in stmts:
            if s["basis"] == wanted:
                return s
        if labelled:
            raise ResultsError("no %s statement in the PDF; it carries: %s" % (
                wanted, ", ".join("%s (page %d)" % (s["basis"] or "basis not stated", s["page"]) for s in stmts)))
        return stmts[0]
    for basis in ("consolidated", "standalone"):
        for s in stmts:
            if s["basis"] == basis:
                return s
    return stmts[0]


def periods_from_statement(stmt, scale_override=None):
    """{key: {(start, end): rupees (EPS as is)}} plus the unit label and derivation notes."""
    factor, unit = stmt["unit_factor"], stmt["unit"]
    if scale_override:
        word = re.sub(r"^in\s+|^rs\.?\s*|^inr\s*", "", scale_override.strip().lower())
        if word not in PDF_SCALES:
            raise ResultsError("unknown --scale %r: pass lakh, crore, million or rupees" % scale_override)
        factor, unit = PDF_SCALES[word], word
    if factor is None:
        raise ResultsError("the PDF's statement (page %d) does not say its unit (no 'Rs in lakhs / crore' line could "
                           "be read anywhere in the filing): open it, read the unit line and pass --scale lakh, "
                           "--scale crore or --scale rupees" % stmt["page"])
    spans = [(c["start"], c["end"]) for c in stmt["columns"]]
    out, notes = {}, []
    for key, row in stmt["rows"].items():
        bucket = {}
        for span, value in zip(spans, row["values"]):
            if value is None or span in bucket:
                continue
            bucket[span] = value if key in PER_SHARE else value * factor
        if bucket:
            out[key] = bucket
    rev, ti, oi = out.get("revenue", {}), out.get("total_income", {}), out.get("other_income", {})
    derived = []
    for span in spans:
        if span not in rev and span in ti and span in oi:
            rev[span] = ti[span] - oi[span]
            derived.append(span[1])
    if derived:
        notes.append("revenue for %s is total income minus other income (the revenue cell was not read)"
                     % ", ".join(sorted(set(derived))))
    if rev:
        out["revenue"] = rev
    if stmt.get("unit_page") and stmt["unit_page"] != stmt["page"] and not scale_override:
        notes.append("the unit line (%s) was read on page %d, not on the statement's page" % (stmt["unit_line"], stmt["unit_page"]))
    return out, unit, notes


def analyse_pdf(parsed, basis=None, scale=None, quarters=6, name=None, source_path=None, url=None):
    """The same report as analyse(), from a results PDF's text."""
    stmt = choose_statement(parsed, basis)
    periods_by_key, unit, notes = periods_from_statement(stmt, scale)
    if not ("revenue" in periods_by_key or "pat" in periods_by_key):
        raise ResultsError("the statement on page %d: neither the revenue nor the net profit row could be read "
                           "(rows read: %s)" % (stmt["page"], ", ".join(PDF_ROW_NAMES[k] for k in PDF_KEYS if k in stmt["rows"]) or "none"))
    core = analyse_periods(periods_by_key, quarters, stmt["bank"])
    unread = []
    for u in stmt["unread"]:
        if u["why"] == "no row found":
            unread.append("%s: no row found" % PDF_ROW_NAMES[u["row"]])
        else:
            unread.append("%s (%s): %s not read (%s)" % (PDF_ROW_NAMES[u["row"]], u["label"], ", ".join(u["columns"]), u["why"]))
    if stmt["basis_inferred"]:
        notes.append("the statement's title does not say standalone or consolidated; read as consolidated because "
                     "it carries a non-controlling interest or share-of-joint-venture line")
    pdf = {"url": url, "path": source_path, "page": stmt["page"], "title": stmt["title"], "unit_line": stmt["unit_line"],
           "columns": stmt["columns"], "unread": unread, "notes": notes, "side_by_side": stmt["side_by_side"],
           "statements": [{"page": s["page"], "basis": s["basis"] or "not stated", "title": s["title"],
                           "problem": s["problem"]} for s in parsed["statements"] + parsed["unusable"]]}
    result = {"entity": name or parsed.get("entity"), "filed_symbol": None, "scrip_code": None,
              "basis": stmt["basis"] or "not stated in the PDF", "scale": [unit], "audited": stmt["audited"],
              "bank": stmt["bank"], "files": [os.path.basename(source_path or url or "")],
              "concepts": {k: [stmt["rows"][k]["label"]] for k in stmt["rows"]},
              "source": PDF_SOURCE, "pdf": pdf}
    result.update(core)
    return result


# ---- the command: --pdf PATH_OR_URL and --today

def pdf_text_for(source, offline=None):
    """A path, a URL or (--offline DIR) DIR/*<guid>.txt -> (text, local path, url). A .txt is pdftotext output."""
    url = source if re.match(r"https?://", source, re.I) else None
    if url and offline:
        saved = sorted(glob.glob(os.path.join(offline, "*%s.txt" % pdf_guid(url))))
        if saved:
            with open(saved[0], encoding="utf-8", errors="replace") as fh:
                return fh.read(), saved[0], url
        raise ResultsError("offline: no saved text for %s in %s" % (pdf_guid(url), offline))
    if url:
        path, fetched = cached_pdf(url)
    else:
        path = source
        if not os.path.isfile(path):
            raise ResultsError("no such file: %s" % path)
    if path.lower().endswith(".txt"):
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(), path, url
    return _pdf_to_text(path), path, url


def today_filings(symbol, name, code, now, offline=None):
    """Today's results filings for the company from BSE's feed -> (candidates best first, all of its filings)."""
    text = bse_feed_cached(now, offline)
    items = parse_rss(text, "BSE")
    return results_filings(items, name, code)


def facts_from_pdf(args, symbol, name, basis, now):
    """-> (result or None, [stderr lines]). Reads --pdf, or today's results filing from BSE's feed (--today)."""
    lines, sources = [], []
    if args.today:
        try:
            candidates, mine = today_filings(symbol, name, args.bse_code, now, args.offline)
        except ResultsError as exc:
            return None, ["BSE announcements: %s" % exc]
        if not candidates:
            who = ("scrip code %s" % args.bse_code) if args.bse_code else name
            return None, ["nothing from %s in today's BSE announcements looks like a results filing (%d other "
                          "filing%s from it today%s)" % (who, len(mine), "" if len(mine) == 1 else "s",
                                                        ": " + "; ".join(it["description"][:70] for it in mine[:3]) if mine else "")]
        sources = [(it["link"], it) for it in candidates[:PDF_MAX_TRIES]]
    else:
        sources = [(args.pdf, None)]
    for source, item in sources:
        try:
            text, path, url = pdf_text_for(source, args.offline)
        except ResultsError as exc:
            lines.append("%s" % exc)
            continue
        parsed = parse_results_pdf(text)
        if not parsed["statements"]:
            lines.append("%s: %s" % (os.path.basename(path), describe_problems(parsed, url or path)))
            continue
        try:
            result = analyse_pdf(parsed, basis, args.scale, args.quarters, name, path, url)
        except ResultsError as exc:
            lines.append("%s: %s" % (os.path.basename(path), exc))
            continue
        if item is not None:
            result["pdf"]["filing"] = {"company": item["title"], "description": item["description"],
                                       "published": item["published"].isoformat() if item["published"] else None,
                                       "scrip": item["scrip"]}
            if not name and result["entity"] is None:
                result["entity"] = re.sub(r"\s*\(\d+\)\s*$", "", item["title"])
        return result, lines
    return None, lines


# ---------------------------------------------------------------- Kite (the company name)

def company_name(symbol, now, fallback=None):
    """Kite's instrument name for the symbol -> (name, None), or (None, why) without a session."""
    if fallback:
        return fallback, None
    try:
        return kite.Kite.connect(now).instrument(symbol)["name"], None
    except kite.KiteError as exc:      # LoginNeeded is a KiteError
        return None, str(exc)


def parse_now(text):
    if not text:
        return nse.now_ist()
    try:
        return datetime.datetime.strptime(text.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=nse.IST)
    except ValueError:
        raise ResultsError("--now wants 'YYYY-MM-DD HH:MM' in IST, not %r" % text)


# ---------------------------------------------------------------- main

def cmd_facts(args, now):
    symbol = kite.normalize_symbol(args.symbol)
    if sum(1 for x in (args.file, args.pdf, args.today) if x) != 1:
        print("results: facts wants exactly one of --file PATH (XBRL), --pdf PATH_OR_URL or --today", file=sys.stderr)
        return 2
    name, note = company_name(symbol, now, args.name)
    if args.today and not (name or args.bse_code):
        print("results: --today needs the company name to match BSE's feed: log in to Kite (%s), or pass --name "
              "or --bse-code CODE" % (note or "").split(":")[0], file=sys.stderr)
        return 2
    basis = "consolidated" if args.consolidated else ("standalone" if args.standalone else None)
    failed = 0
    if args.file:
        instances = []
        for path in args.file:
            try:
                instances.append(read_file(path))
            except ResultsError as exc:
                print("results: %s" % exc, file=sys.stderr)
                failed += 1
        if not instances:
            return 2
        try:
            result = analyse(instances, basis, args.scale, args.quarters, name)
        except ResultsError as exc:
            print("results: %s" % exc, file=sys.stderr)
            return 2
    else:
        result, problems = facts_from_pdf(args, symbol, name, basis, now)
        for line in problems:
            print("results: %s" % line, file=sys.stderr)
        if result is None:
            return 2 if not problems else 1
        failed = len(problems)
    if note:
        print("results: no Kite session (%s); the company name comes from the %s"
              % (note.split(":")[0], "file" if args.file else "PDF or the feed"), file=sys.stderr)
    doc = {"symbol": symbol, "facts": result, "changes": changes(result)}
    if args.json:
        print(json.dumps(doc, indent=2))
    else:
        sys.stdout.write(render(symbol, result))
    if args.write:
        write_results(doc, now)
        if not args.json:
            print("wrote %s" % RESULTS_FILE)
    if args.metrics:
        day = now.date().isoformat()
        n = write_metrics(args.metrics, [metrics_row(symbol, result, day)], day)
        if not args.json:
            print("wrote %s (%d companies)" % (args.metrics, n))
    return 1 if failed else 0


def cmd_summary(args, now):
    if args.file:
        try:
            with open(args.file, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as exc:
            print("results: cannot read %s: %s" % (args.file, exc), file=sys.stderr)
            return 2
    else:
        text = sys.stdin.read()
    try:
        summary = summarise(text)
    except ResultsError as exc:
        print("results: %s" % exc, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        sys.stdout.write(render_summary(summary))
    if args.write:
        write_results({"symbol": kite.normalize_symbol(args.symbol) if args.symbol else None,
                       "summary": summary}, now)
        if not args.json:
            print("wrote %s" % RESULTS_FILE)
    return 0


def cmd_calendar(args, now):
    symbol = kite.normalize_symbol(args.symbol)
    pinned = None
    if args.date:
        try:
            kind, n, sentence = horizon(_date(args.date), now)
        except ValueError:
            print("results: --date wants YYYY-MM-DD, not %r" % args.date, file=sys.stderr)
            return 2
        pinned = {"date": args.date, "horizon": kind, "trading_days": n, "answer": sentence}
    name, note = company_name(symbol, now, args.name)
    if name is None:
        print("results: %s" % note, file=sys.stderr)
        return 1
    cal = calendar(symbol, name, now, args.offline, use_news=not args.no_news, use_bse=not args.no_bse)
    if pinned:
        cal["pinned"] = pinned
    if args.json:
        print(json.dumps(cal, indent=2))
    else:
        sys.stdout.write(render_calendar(cal, pinned))
    if args.write:
        write_results({"symbol": symbol, "calendar": cal}, now)
        if not args.json:
            print("wrote %s" % RESULTS_FILE)
    if pinned:
        return 0
    if cal["news"] is None and cal["bse"] is None:
        return 2
    return 1 if cal["problems"] else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    common = argparse.ArgumentParser(add_help=False)   # accepted before or after the subcommand
    for parser, default in ((ap, None), (common, argparse.SUPPRESS)):
        parser.add_argument("--now", default=default, help="pin the clock, 'YYYY-MM-DD HH:MM' IST")
        parser.add_argument("--json", action="store_true", default=default)
        parser.add_argument("--write", action="store_true", default=default, help="write trader/results.json")
        parser.add_argument("--offline", metavar="DIR", default=default,
                            help="read the feeds from saved files in DIR")
    sub = ap.add_subparsers(dest="cmd")
    f = sub.add_parser("facts", parents=[common], help="the quarters table from downloaded XBRL results files")
    f.add_argument("symbol", help="Kite trading symbol, e.g. RELIANCE")
    f.add_argument("--file", action="append", metavar="PATH",
                   help="an Ind AS XBRL results file you downloaded from NSE or BSE (repeat for more quarters)")
    f.add_argument("--pdf", metavar="PATH_OR_URL",
                   help="the results PDF from BSE's announcements feed (its link, or a saved file; a .txt from "
                        "pdftotext -layout is read as is)")
    f.add_argument("--today", action="store_true",
                   help="fetch today's results filing for the company from BSE's announcements feed")
    f.add_argument("--bse-code", metavar="CODE", help="match the BSE feed by scrip code instead of the company name")
    g = f.add_mutually_exclusive_group()
    g.add_argument("--consolidated", action="store_true")
    g.add_argument("--standalone", action="store_true")
    f.add_argument("--scale", help="lakh, crore, million or rupees, when the file or PDF does not say")
    f.add_argument("--quarters", type=int, default=6)
    f.add_argument("--metrics", metavar="FILE", help="merge a row for this company into this JSON")
    f.add_argument("--name", help="company name to print (skips the Kite lookup)")
    s = sub.add_parser("summary", parents=[common], help="sum up a pasted results commentary or call transcript")
    s.add_argument("--paste", action="store_true", help="read the text from stdin (the default)")
    s.add_argument("--file", metavar="PATH", help="read the text from a file instead")
    s.add_argument("--symbol", help="the company, for trader/results.json")
    c = sub.add_parser("calendar", parents=[common], help="is the next result inside a day trade's horizon?")
    c.add_argument("symbol")
    c.add_argument("--date", help="the result date you know, YYYY-MM-DD")
    c.add_argument("--name", help="company name for the search (skips the Kite lookup)")
    c.add_argument("--no-news", action="store_true", help="skip Google News")
    c.add_argument("--no-bse", action="store_true", help="skip the BSE announcements feed")
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 2
    args.json, args.write = bool(args.json), bool(args.write)
    try:
        now = parse_now(args.now)
    except ResultsError as exc:
        print("results: %s" % exc, file=sys.stderr)
        return 2
    return {"facts": cmd_facts, "summary": cmd_summary, "calendar": cmd_calendar}[args.cmd](args, now)


if __name__ == "__main__":
    sys.exit(main())
