#!/usr/bin/env python3
"""Insider tracker for trader-insiders. Standard library only.

Reads the CSV files NSE publishes on its Corporates pages, Insider Trading
(SEBI PIT Regulation 7(2) disclosures), Bulk Deals and Block Deals, and turns
them into clean rows: who, category, buy or sell, quantity, value in rupees,
mode, the trade dates, how long the disclosure took, flagged against what
you hold and what you watch.

    python3 insiders.py index --file ~/Downloads/CF-Insider-Trading-equities-29-10-2026.csv
    python3 insiders.py index --file bulk.csv --symbol RELIANCE --since 2026-10-20 --json
    python3 insiders.py alert --file insider.csv --file bulk.csv --file block.csv
    python3 insiders.py news RELIANCE                  # bulk deal / block deal / promoter stake headlines
    python3 insiders.py today                          # today's insider / SAST / pledge filings on BSE, PDF links
    python3 insiders.py today --parse                  # and read each PDF: the rows (needs pdftotext from poppler)
    python3 insiders.py today --symbol RELIANCE,INFY   # only your companies (needs the Kite session for the names)
    python3 insiders.py pdf https://www.bseindia.com/xml-data/corpfiling/AttachLive/<guid>.pdf   # read one filing
    python3 insiders.py pdf ~/Downloads/disclosure.pdf --symbol RELIANCE

Where the data comes from, and why you download it:
  - NSE's pages (Corporates > Insider Trading, Bulk Deals, Block Deals) carry
    a download icon that saves a CSV. NSE blocks scripted clients and its
    terms forbid scraping, so this tool never calls nseindia.com: you save
    the CSV in your own browser and pass the path with --file. The kind of
    file is detected from its headers (bulk and block deals share one layout,
    so a block-deals file is told apart by its name or --kind block).
  - BSE's corporate announcements RSS (the current day's filings from every
    listed company, a public feed) tells you that an insider-trading, SAST or
    pledge disclosure was filed today for a company, with the PDF link to
    open. Fetched once and reused for 10 minutes from
    trader/cache/bse-announcements-<date>-<HHMM>.xml.
  - `today --parse` and `pdf PATH` read the PDF itself (bsepdf.py): fetched
    with the same identified client, kept under trader/cache/bse-pdf/, turned
    into text by poppler's pdftotext (brew install poppler) and read for the
    SEBI SAST Regulation 29 table, the Regulation 31 encumbrance table or the
    PIT Regulation 7(2) Form C table, into the same rows the NSE CSV gives,
    with source "bse-pdf" and the link. A scanned PDF with no text layer, or
    a table the reader does not know, is reported in one line with the link.
    The complete official table is still the NSE CSV.
  - Headlines come from Google News RSS, read by a plainly identified client
    (the User-Agent names this tool, no cookies, no retry past a 403). The
    company name for the query comes from Kite's instrument list, so `news`
    needs today's Zerodha session (python3 trader-kite/login.py).

What an insider disclosure is, and is not:
  - Under SEBI's Prohibition of Insider Trading Regulations, 7(2), a
    promoter, director, KMP, designated person or their immediate relative
    tells the company within two trading days of a trade over Rs 10 lakh in
    a quarter, and the company tells the exchange within two trading days of
    that. So a row is a few days old when NSE posts it, and the tool counts
    the trading days at each step and flags the late ones.
  - It is public. Reading it fast is not inside information. There is none here.
  - Bulk deals (over 0.5 % of the shares in a day) and block deals (the
    exchange's block window) are the big tickets of the day: who bought,
    who sold, how many, at what average price.

State: `alert` remembers which rows it has shown in trader/insiders.json, so
each row alerts once. Dates in the files look like 29-OCT-2026 and
29-Oct-2026 17:42; the tool prints ISO dates and IST. Not financial advice.
"""

import argparse
import csv
import datetime as _dt
import email.utils
import glob
import hashlib
import html
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trader-kite"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kite   # noqa: E402
import nse    # noqa: E402
import bsepdf   # noqa: E402

GOOGLE = "https://news.google.com/rss/search?q=%s&hl=en-IN&gl=IN&ceid=IN:en"
DEAL_QUERY = '"%s" (bulk deal OR block deal OR insider OR promoter stake)'
BSE_ANNOUNCEMENTS = "https://www.bseindia.com/data/xml/announcements.xml"
BSE_CACHE_MINUTES = 10
CACHE_DIR = kite.CACHE_DIR
# what counts as an insider / SAST / pledge filing in a BSE announcement's text
INSIDER_FILING = re.compile(r"\bregulation 7\s*\(|\bregulation (?:29|31)\b|\bSAST\b|\binsider trading\b.{0,40}\bdisclos|"
                            r"\bdisclos.{0,40}\binsider trading\b|\bpledg(?:e|ed|es)\b|\bencumbrance\b", re.I)
BOARD_NOTICE = re.compile(r"\btrading window\b|\bis scheduled on\b", re.I)   # cites the code, discloses nothing
UA = "day-trader-skill-india/1.0 (+https://github.com/krishanag/day-trader-skill)"
IST = nse.IST
TIMEOUT = 30
LAKH = 100000.0
CRORE = 10000000.0
DAYS_TO_COMPANY = 2     # PIT Regulation 7(2): the insider tells the company within two trading days
DAYS_TO_EXCHANGE = 2    # and the company tells the exchange within two trading days of that
NEWS_HOURS = 168        # a week: deal headlines are rarer than news
NAME_DROP = {"LT", "LTD", "LTD.", "LIMITED"}
KITE_NAME_LIMIT = 22    # Kite truncates long names (TATA CONSULTANCY SERV LT): a short last word is a stub
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

# Each field is matched against a lower-cased, whitespace-collapsed header, so
# NSE's spellings (DISPLOSED, INITMATION, BROADCATE), the correct ones, case and
# stray spaces all read the same. Order matters: the first header a pattern
# matches is taken, and a header serves one field.
INSIDER_FIELDS = [
    ("symbol", r"^symbol$"),
    ("company", r"^company"),
    ("regulation", r"regulation"),
    ("person", r"name of the acquirer|acquirer\s*/\s*disposer|^name$"),
    ("category", r"category"),
    ("security_prior", r"type of securit.*prior"),
    ("qty_prior", r"no\.? of securit.*prior"),
    ("pct_prior", r"%.*prior"),
    ("security", r"type of securit.*(acquired|dis)"),
    ("qty", r"no\.? of securit.*(acquired|dis)"),
    ("value", r"value of securit"),
    ("side", r"transaction type|acquisition\s*/\s*disposal"),
    ("security_post", r"type of securit.*post"),
    ("qty_post", r"no\.? of securit.*post"),
    ("pct_post", r"%.*post"),
    ("trade_from", r"(acquisition|allotment).*from"),
    ("trade_to", r"(acquisition|allotment).*\bto$"),
    ("intimated", r"in[a-z]*mation"),
    ("mode", r"^mode"),
    ("derivative_type", r"derivative type"),
    ("derivative_spec", r"derivative contract"),
    ("notional_buy", r"notional.*buy"),
    ("units_buy", r"(units|lot size).*buy"),
    ("notional_sell", r"notional.*sell"),
    ("units_sell", r"(units|lot size).*sell"),
    ("exchange", r"^exchange"),
    ("remark", r"^remark"),
    ("broadcast", r"broadca"),
    ("xbrl", r"xbrl"),
]
INSIDER_REQUIRED = ["symbol", "person", "category", "qty", "value", "side", "trade_to", "intimated", "broadcast"]
DEAL_FIELDS = [
    ("date", r"^date"),
    ("symbol", r"^symbol"),
    ("company", r"security name|company"),
    ("client", r"client"),
    ("side", r"buy\s*/\s*sell|buy or sell|^side"),
    ("qty", r"quantity"),
    ("price", r"price"),
    ("remarks", r"remark"),
]
DEAL_REQUIRED = ["date", "symbol", "client", "side", "qty", "price"]

CATEGORIES = [   # (what the header value contains, the clean label); first match wins
    ("relative", "Immediate Relative"),
    ("promoter group", "Promoter Group"),
    ("promoter", "Promoter"),
    ("key managerial", "Key Managerial Personnel"),
    ("kmp", "Key Managerial Personnel"),
    ("director", "Director"),
    ("employee", "Employee"),
    ("designated", "Designated Person"),
]
SIDES = [
    ("pledge creat", "pledge"),
    ("pledge invo", "pledge invoked"),
    ("invocation", "pledge invoked"),
    ("invok", "pledge invoked"),
    ("revo", "pledge released"),
    ("releas", "pledge released"),
    ("pledge", "pledge"),
    ("buy", "buy"), ("acqui", "buy"), ("purchase", "buy"), ("allot", "buy"),
    ("sell", "sell"), ("sale", "sell"), ("dispos", "sell"),
]
MODES = [
    ("off market", "off-market"), ("off-market", "off-market"),
    ("market", "market"),
    ("esop", "ESOP"), ("esps", "ESOP"), ("stock option", "ESOP"),
    ("gift", "gift"),
    ("inter", "inter-se transfer"),
    ("pledge", "pledge"),
    ("preferential", "preferential allotment"),
    ("rights", "rights issue"),
    ("bonus", "bonus"),
    ("conversion", "conversion"),
    ("public", "public issue"),
    ("buyback", "buyback"),
    ("open offer", "open offer"),
    ("block", "block deal"),
]
DEAL_TAGS = [
    ("bulk-deal", r"\bbulk deals?\b|\bblock deals?\b"),
    ("promoter", r"\bpromoters?\b|\binsider\b|\bpledg(?:e|ed|es)\b|\bstake\b|\bopen offer\b"),
]


class InsidersError(Exception):
    pass


# ---------------------------------------------------------------- small helpers

def norm_header(text):
    return " ".join((text or "").replace("﻿", "").lower().split())


def blank(text):
    s = (text or "").strip()
    return s in ("", "-", "--", "nil", "NIL", "Nil", "NA", "N.A.", "n.a.", "null")


def number(text):
    """'1,40,40,000.00', '7,250,000', '1171.5' -> float; blanks and junk -> None."""
    if blank(text):
        return None
    s = re.sub(r"[,\s]", "", str(text))
    try:
        return float(s)
    except ValueError:
        return None


def count(text):
    v = number(text)
    return int(round(v)) if v is not None else None


def parse_date(text):
    """'29-OCT-2026', '29-Oct-2026 17:42', '29/10/2026', '2026-10-29' -> (ISO date, 'HH:MM' or None)."""
    s = (text or "").strip()
    if blank(s):
        return None, None
    m = re.match(r"^(\d{1,2})[-/ ]([A-Za-z]{3})[A-Za-z]*[-/ ](\d{4})(?:[ T](\d{1,2}):(\d{2})(?::\d{2})?)?$", s)
    if m and m.group(2).lower() in MONTHS:
        y, mo, d = int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1))
    else:
        m = re.match(r"^(\d{1,2})[-/](\d{1,2})[-/](\d{4})(?:[ T](\d{1,2}):(\d{2})(?::\d{2})?)?$", s)
        if m:
            y, mo, d = int(m.group(3)), int(m.group(2)), int(m.group(1))
        else:
            m = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{1,2}):(\d{2})(?::\d{2})?)?$", s)
            if not m:
                return None, None
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        day = _dt.date(y, mo, d).isoformat()
    except ValueError:
        return None, None
    hhmm = "%02d:%02d" % (int(m.group(4)), int(m.group(5))) if m.group(4) else None
    return day, hhmm


def iso_stamp(day, hhmm):
    """('2026-10-28', '17:42') -> '2026-10-28T17:42:00+05:30'; a date alone stays a date."""
    if not day:
        return None
    return "%sT%s:00+05:30" % (day, hhmm) if hhmm else day


def trading_days_between(start, end):
    """Trading days after `start` up to and including `end` (both ISO dates); 0 when end is not after start.
    Weekends and the pack's NSE holiday list are skipped; a date outside the list counts as a trading day."""
    if not start or not end:
        return None
    a, b = _dt.date.fromisoformat(start), _dt.date.fromisoformat(end)
    n, d = 0, a
    while d < b:
        d += _dt.timedelta(days=1)
        if nse.trading_day(d)[0]:
            n += 1
    return n


def label(text, table, default=None):
    low = (text or "").lower()
    for key, clean in table:
        if key in low:
            return clean
    return default if default is not None else (" ".join((text or "").split()) or None)


def inr(value):
    """Rupees the way the papers print them: Rs 1.40 cr, Rs 37.50 lakh, Rs 85,000.00."""
    if value is None:
        return "value not given"
    a = abs(value)
    if a >= CRORE:
        return "Rs {:,.2f} cr".format(value / CRORE)
    if a >= LAKH:
        return "Rs {:,.2f} lakh".format(value / LAKH)
    return "Rs {:,.2f}".format(value)


def row_id(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- the CSV files

def read_csv(path):
    """Header list and row dicts (keyed by the raw header) from an NSE download."""
    with open(path, "rb") as fh:
        data = fh.read()
    text = data.decode("utf-8-sig", "replace")
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        raise InsidersError("%s is empty" % path)
    headers = [h.strip() for h in rows[0]]
    out = []
    for r in rows[1:]:
        r = list(r) + [""] * (len(headers) - len(r))
        out.append(dict(zip(headers, [c.strip() for c in r])))
    return headers, out


def map_headers(headers, fields):
    """{field: raw header} by meaning. Each header serves one field."""
    normal = [(h, norm_header(h)) for h in headers]
    taken, mapping = set(), {}
    for field, pattern in fields:
        rx = re.compile(pattern)
        for raw, low in normal:
            if raw not in taken and rx.search(low):
                mapping[field] = raw
                taken.add(raw)
                break
    return mapping


def detect_kind(headers, path="", kind=None):
    """'insider', 'bulk' or 'block' from the headers (bulk and block share a layout: the file name or --kind decides)."""
    if kind:
        return kind
    low = [norm_header(h) for h in headers]
    if any("category" in h or "acquirer" in h or "regulation" in h for h in low):
        return "insider"
    if any("client" in h for h in low):
        return "block" if "block" in os.path.basename(path).lower() else "bulk"
    raise InsidersError("%s is not an NSE insider-trading, bulk-deals or block-deals CSV; its headers are: %s"
                        % (path or "the file", ", ".join(headers)))


def _require(mapping, required, headers, path, what):
    missing = [f for f in required if f not in mapping]
    if missing:
        raise InsidersError("%s does not look like an NSE %s CSV: no column for %s. Headers found: %s"
                            % (path, what, ", ".join(missing), ", ".join(headers)))


def insider_row(raw, m, path):
    g = lambda f: raw.get(m.get(f, ""), "") if f in m else ""
    trade_from, _ = parse_date(g("trade_from"))
    trade_to, _ = parse_date(g("trade_to"))
    trade_to = trade_to or trade_from
    trade_from = trade_from or trade_to
    intimated, _ = parse_date(g("intimated"))
    bday, btime = parse_date(g("broadcast"))
    to_company = trading_days_between(trade_to, intimated)
    to_exchange = trading_days_between(intimated, bday)
    derivative = None
    if not blank(g("derivative_type")) or not blank(g("derivative_spec")):
        derivative = {"type": " ".join(g("derivative_type").split()) or None,
                      "contract": " ".join(g("derivative_spec").split()) or None,
                      "notional_buy_inr": number(g("notional_buy")), "units_buy": count(g("units_buy")),
                      "notional_sell_inr": number(g("notional_sell")), "units_sell": count(g("units_sell"))}
    symbol = kite.normalize_symbol(g("symbol")) if g("symbol") else ""
    row = {
        "kind": "insider", "symbol": symbol, "company": " ".join(g("company").split()) or None,
        "regulation": " ".join(g("regulation").split()) or None,
        "person": " ".join(g("person").split()), "category": label(g("category"), CATEGORIES),
        "category_raw": g("category") or None,
        "side": label(g("side"), SIDES), "side_raw": g("side") or None,
        "security": None if blank(g("security")) else " ".join(g("security").split()),
        "qty": count(g("qty")), "value_inr": number(g("value")),
        "qty_prior": count(g("qty_prior")), "pct_prior": number(g("pct_prior")),
        "qty_post": count(g("qty_post")), "pct_post": number(g("pct_post")),
        "mode": label(g("mode"), MODES), "mode_raw": g("mode") or None,
        "trade_from": trade_from, "trade_to": trade_to, "intimated": intimated,
        "broadcast": iso_stamp(bday, btime),
        "days_to_company": to_company, "days_to_exchange": to_exchange,
        "late_to_company": to_company is not None and to_company > DAYS_TO_COMPANY,
        "late_to_exchange": to_exchange is not None and to_exchange > DAYS_TO_EXCHANGE,
        "derivative": derivative,
        "exchange": g("exchange") or None,
        "remark": None if blank(g("remark")) else " ".join(g("remark").split()),
        "xbrl": None if blank(g("xbrl")) else g("xbrl"),
        "file": os.path.basename(path), "source": "nse-csv", "link": None, "posted_on": "NSE",
    }
    row["late"] = row["late_to_company"] or row["late_to_exchange"]
    row["id"] = row_id("insider", symbol, row["person"], row["side_raw"], row["qty"], row["value_inr"],
                       trade_from, trade_to, row["broadcast"], derivative and derivative["contract"])
    return row


def deal_row(raw, m, kind, path):
    g = lambda f: raw.get(m.get(f, ""), "") if f in m else ""
    day, _ = parse_date(g("date"))
    qty, price = count(g("qty")), number(g("price"))
    symbol = kite.normalize_symbol(g("symbol")) if g("symbol") else ""
    row = {
        "kind": kind, "symbol": symbol, "company": " ".join(g("company").split()) or None,
        "client": " ".join(g("client").split()),
        "side": label(g("side"), SIDES), "side_raw": g("side") or None,
        "qty": qty, "price": price,
        "value_inr": round(qty * price, 2) if qty is not None and price is not None else None,
        "date": day, "remarks": None if blank(g("remarks")) else " ".join(g("remarks").split()),
        "file": os.path.basename(path),
    }
    row["id"] = row_id(kind, day, symbol, row["client"], row["side_raw"], qty, price)
    return row


def parse_file(path, kind=None):
    """Every row of one NSE CSV as a clean dict, plus the kind of file it was."""
    headers, raws = read_csv(path)
    kind = detect_kind(headers, path, kind)
    if kind == "insider":
        m = map_headers(headers, INSIDER_FIELDS)
        _require(m, INSIDER_REQUIRED, headers, path, "insider-trading")
        rows = [insider_row(r, m, path) for r in raws]
    else:
        m = map_headers(headers, DEAL_FIELDS)
        _require(m, DEAL_REQUIRED, headers, path, "%s-deals" % kind)
        rows = [deal_row(r, m, kind, path) for r in raws]
    return kind, [r for r in rows if r["symbol"]]


def load_files(paths, kind=None):
    rows = []
    for p in paths:
        _, got = parse_file(p, kind)
        rows += got
    return rows


def sort_key(row):
    if row["kind"] == "insider":
        return (row.get("broadcast") or "", row.get("trade_to") or "", row["symbol"])
    return (row.get("date") or "", row["symbol"], row["client"])


def select(rows, symbols=None, since=None):
    """Newest first; only these symbols; only rows dated (broadcast or deal date) on or after `since`."""
    out = []
    for r in rows:
        if symbols and r["symbol"] not in symbols:
            continue
        when = (r.get("broadcast") or r.get("trade_to") or "") if r["kind"] == "insider" else (r.get("date") or "")
        if since and when[:10] < since:
            continue
        out.append(r)
    out.sort(key=sort_key, reverse=True)
    return out


# ---------------------------------------------------------------- state and your lists

def load_json(path):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def load_state(path):
    return load_json(path) or {"seen": [], "last_check": None}


def save_state(path, state):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
        fh.write("\n")


def new_since(rows, state):
    seen = set(state.get("seen", []))
    return [r for r in rows if r["id"] not in seen]


def your_lists(portfolio="trader/portfolio.json", watchlist="trader/watchlist.json"):
    """{symbol: 'portfolio' | 'watchlist'}: what you hold wins over what you watch."""
    out = {}
    wl = load_json(watchlist) or {}
    for h in wl.get("hits", []):
        if h.get("symbol"):
            out[kite.normalize_symbol(h["symbol"])] = "watchlist"
    pf = load_json(portfolio) or {}
    positions = pf.get("positions") or {}
    syms = positions.keys() if isinstance(positions, dict) else [p.get("symbol") for p in positions]
    for s in syms:
        if s:
            out[kite.normalize_symbol(s)] = "portfolio"
    return out


# ---------------------------------------------------------------- output

def describe_derivative(d):
    what = "%s %s" % (d.get("type") or "derivative", d.get("contract") or "")
    if d.get("notional_buy_inr") is not None:
        what += ", %s notional bought" % inr(d["notional_buy_inr"])
    if d.get("notional_sell_inr") is not None:
        what += ", %s notional sold" % inr(d["notional_sell_inr"])
    return what.strip()


def describe_insider(r):
    what = "%s shares" % "{:,}".format(r["qty"]) if r["qty"] is not None else "shares"
    if r.get("derivative") and r["qty"] is None:          # a derivative row (the NSE CSV has those)
        what = describe_derivative(r["derivative"])
    else:
        what += ", " + inr(r["value_inr"])
        if r.get("derivative"):                           # a Form C's derivatives block beside the share row
            what += ", plus derivatives: " + describe_derivative(r["derivative"])
    when = r["trade_from"] or "?"
    if r["trade_to"] and r["trade_to"] != r["trade_from"]:
        when += " to " + r["trade_to"]
    day = (r.get("broadcast") or "")[:10] or r.get("intimated") or r.get("trade_to") or "?"
    who = r["symbol"] or r.get("code") or (r.get("company") or "?")[:10]
    head = "%s  %-10s %-5s %s, %s, %s (%s)" % (
        day, who, (r["side"] or "?"), what, r["mode"] or "?", r["person"], r["category"] or "?")
    bits = ["traded %s" % when]
    if r["days_to_company"] is not None:
        bits.append("told the company %d trading day%s later%s" % (
            r["days_to_company"], "" if r["days_to_company"] == 1 else "s",
            " (LATE: over %d)" % DAYS_TO_COMPANY if r["late_to_company"] else ""))
    if r["days_to_exchange"] is not None:
        bits.append("on %s %d trading day%s after that%s" % (
            r.get("posted_on") or "NSE", r["days_to_exchange"], "" if r["days_to_exchange"] == 1 else "s",
            " (LATE: over %d)" % DAYS_TO_EXCHANGE if r["late_to_exchange"] else ""))
    if r.get("pct_post") is not None:
        bits.append("holds %.2f %% after" % r["pct_post"])
    if r.get("remark"):
        bits.append(r["remark"])
    return head, ", ".join(bits)


def describe_deal(r):
    qty = "{:,}".format(r["qty"]) if r["qty"] is not None else "?"
    price = "Rs {:,.2f}".format(r["price"]) if r["price"] is not None else "?"
    head = "%s  %-10s %-5s %s at %s avg = %s, %s" % (
        r["date"] or "?", r["symbol"], (r["side"] or "?").upper(), qty, price, inr(r["value_inr"]), r["client"])
    tail = "%s deal" % r["kind"]
    if r.get("remarks"):
        tail += ", " + r["remarks"]
    return head, tail


def flag_line(r, lists):
    where = lists.get(r["symbol"])
    return "  <- %s is in your %s" % (r["symbol"], where) if where else ""


def render_rows(rows, lists=None, new=False):
    lists = lists or {}
    out = []
    for r in rows:
        head, tail = describe_insider(r) if r["kind"] == "insider" else describe_deal(r)
        out.append("%s%s%s" % ("NEW " if new else "", head, flag_line(r, lists)))
        out.append("      %s" % tail)
    return out


FOOTER = ("Public disclosures from NSE's own CSV (SEBI PIT Regulation 7(2); bulk deals over 0.5 %; "
          "the block window). Days are trading days. Not inside information, not financial advice.")


# ---------------------------------------------------------------- headlines

_urlopen = urllib.request.urlopen   # tests swap this


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with _urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise InsidersError("HTTP %s for %s" % (exc.code, url.split("?")[0]))
    except urllib.error.URLError as exc:
        raise InsidersError("could not reach %s: %s" % (url.split("?")[0], exc.reason))


def parse_time(value):
    """RFC 822 -> aware IST datetime, or None."""
    try:
        d = email.utils.parsedate_to_datetime((value or "").strip())
    except (TypeError, ValueError, IndexError):
        return None
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(IST)


def _text(el, name):
    for c in el:
        if c.tag.rsplit("}", 1)[-1] == name:
            return (c.text or "").strip()
    return ""


def parse_rss(data, symbol):
    """Google News RSS -> items, publisher split off the title, tagged bulk-deal / promoter."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise InsidersError("the Google News feed for %s is not valid XML: %s" % (symbol, exc))
    out = []
    for it in root.iter("item"):
        title = html.unescape(_text(it, "title"))
        publisher = _text(it, "source")
        if publisher and title.endswith(" - " + publisher):
            title = title[: -len(" - " + publisher)].rstrip()
        title = " ".join(title.split())
        summary = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", _text(it, "description"))).split())
        out.append({"symbol": symbol, "title": title, "summary": summary, "source": publisher or "Google News",
                    "link": _text(it, "link"), "time": parse_time(_text(it, "pubDate")),
                    "tags": [t for t, rx in DEAL_TAGS if re.search(rx, title, re.I)]})
    return out


def mentions(name, symbol):
    """A pattern for the company: its name (any spacing) or its symbol as a word."""
    alts = [r"\b%s\b" % re.escape(symbol)]
    if name and name.upper() != symbol:
        alts.append(r"\b%s\b" % re.escape(name).replace(r"\ ", r"[\s-]+"))
    return re.compile("|".join(alts), re.I)


def bse_feed(now, cache_dir=CACHE_DIR):
    """The day's BSE announcements XML, fetched once and reused for BSE_CACHE_MINUTES
    (trader/cache/bse-announcements-<date>-<HHMM>.xml, shared with trader-news)."""
    fresh = sorted(p for p in glob.glob(os.path.join(cache_dir, "bse-announcements-*.xml"))
                   if time.time() - os.path.getmtime(p) < BSE_CACHE_MINUTES * 60)
    if fresh:
        with open(fresh[-1], "rb") as fh:
            return fh.read()
    data = fetch(BSE_ANNOUNCEMENTS)
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, "bse-announcements-%s.xml" % now.strftime("%Y-%m-%d-%H%M"))
    with open(path, "wb") as fh:
        fh.write(data)
    return data


def parse_bse(data):
    """BSE announcements -> [{"company", "code", "description", "link", "time"}]; the title is 'Company Ltd (code)'."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise InsidersError("the BSE announcements feed is not valid XML: %s" % exc)
    out = []
    for it in root.iter("item"):
        title = html.unescape(_text(it, "title"))
        m = re.match(r"^(.*?)\s*\((\d+)\)\s*$", title)
        company, code = (m.group(1), m.group(2)) if m else (title, _text(it, "scripcode"))
        if not company:
            continue
        day, hhmm = parse_date(_text(it, "pubDate"))
        out.append({"company": " ".join(company.split()), "code": code,
                    "description": " ".join(html.unescape(_text(it, "description")).replace("''", "'").split()),
                    "link": _text(it, "link"), "date": day, "time": hhmm})
    return out


def _company_key(text):
    words = [w for w in re.sub(r"[^A-Za-z0-9& ]+", " ", text or "").lower().split()
             if w not in {n.lower().strip(".") for n in NAME_DROP}]
    return " ".join(words)


def insider_filings(entries, names=None):
    """The insider / SAST / pledge filings, newest first; only your companies when `names` ({symbol: name}) is given."""
    out = []
    for e in entries:
        if not INSIDER_FILING.search(e["description"]) or BOARD_NOTICE.search(e["description"]):
            continue
        symbol = None
        if names is not None:
            company = _company_key(e["company"])
            for sym, name in names.items():
                key = _company_key(name)
                if key and (company.startswith(key) or key.startswith(company)):
                    symbol = sym
                    break
            if symbol is None:
                continue
        out.append({"kind": "filing", "symbol": symbol, "company": e["company"], "code": e["code"],
                    "title": e["description"], "link": e["link"], "date": e["date"], "time": e["time"],
                    "id": row_id("bse", e["link"] or e["description"], e["date"], e["time"])})
    out.sort(key=lambda r: ((r["date"] or ""), (r["time"] or "")), reverse=True)
    return out


def render_filings(rows, new=False, lists=None):
    out = []
    for r in rows:
        when = "%s %s IST" % (r["date"] or "?", r["time"] or "")
        who = "%s (%s)" % (r["company"], r["code"]) + (", %s" % r["symbol"] if r.get("symbol") else "")
        out.append("%s%s  %s" % ("NEW " if new else "", when, who))
        out.append("      %s" % r["title"][:200])
        out.append("      %s" % r["link"])
        if r.get("parse"):
            out += ["      " + line for line in render_parsed(r, lists)]
    return out


def render_parsed(filing, lists=None):
    """The rows read from a filing's PDF, or the one line that says why there are none."""
    p = filing["parse"]
    if p["status"] != "ok":
        return ["PDF not read (%s): open the link" % p["note"]]
    return render_rows(filing["rows"], lists) + ["read from the PDF (%s); the NSE CSV is the complete record" % p["form"]]


# ---------------------------------------------------------------- the PDF behind a filing

PDF_CATEGORY = [   # the SAST forms ask "does the acquirer belong to the promoter / promoter group": the answer
    ("promoter group", "Promoter Group"),
    ("yes", "Promoter"),
    ("promoter", "Promoter"),
    ("no", "Non-promoter"),
]
NOT_READ = "(not read from the PDF)"


def pdf_category(facts):
    raw = facts.get("category_raw") or ""
    if facts.get("form") == "pit-7":
        return label(raw, CATEGORIES)
    if facts.get("form") == "sast-31":
        return "Promoter"
    low = raw.lower()
    for key, clean in PDF_CATEGORY:
        if re.search(r"\b%s\b" % re.escape(key), low):
            return clean
    return " ".join(raw.split()) or None


def pdf_row(f, filing):
    """A bsepdf facts dict -> the row shape insider_row() makes from the NSE CSV, source "bse-pdf".
    The BSE posting time is the broadcast; the letter's date is the intimation."""
    bday, btime = filing.get("date"), filing.get("time")
    trade_from = f.get("trade_from") or f.get("trade_to")
    trade_to = f.get("trade_to") or trade_from
    intimated = f.get("intimated")
    to_company = trading_days_between(trade_to, intimated)
    to_exchange = trading_days_between(intimated, bday)
    remark = f.get("remark")
    if f.get("unread"):
        remark = ", ".join(x for x in [remark, "not read from the PDF: " + ", ".join(f["unread"])] if x)
    symbol = kite.normalize_symbol(filing["symbol"]) if filing.get("symbol") else ""
    link = filing.get("link") or ""
    row = {
        "kind": "insider", "symbol": symbol, "code": filing.get("code"),
        "company": f.get("company") or filing.get("company"),
        "regulation": f.get("regulation"), "form": f.get("form"),
        "person": f.get("person") or NOT_READ, "category": pdf_category(f), "category_raw": f.get("category_raw"),
        "side": label(f.get("side_raw"), SIDES) if f.get("side_raw") else None, "side_raw": f.get("side_raw"),
        "security": f.get("security"),
        "qty": int(f["qty"]) if f.get("qty") is not None else None, "value_inr": f.get("value"),
        "qty_prior": int(f["qty_prior"]) if f.get("qty_prior") is not None else None, "pct_prior": f.get("pct_prior"),
        "qty_post": int(f["qty_post"]) if f.get("qty_post") is not None else None, "pct_post": f.get("pct_post"),
        "mode": label(f.get("mode_raw"), MODES) if f.get("mode_raw") else None, "mode_raw": f.get("mode_raw"),
        "trade_from": trade_from, "trade_to": trade_to, "intimated": intimated,
        "broadcast": iso_stamp(bday, btime),
        "days_to_company": to_company, "days_to_exchange": to_exchange,
        "late_to_company": to_company is not None and to_company > DAYS_TO_COMPANY,
        "late_to_exchange": to_exchange is not None and to_exchange > DAYS_TO_EXCHANGE,
        "derivative": f.get("derivative"),
        "exchange": f.get("exchange"), "remark": remark, "xbrl": None,
        "file": bsepdf.guid(link) + ".pdf" if link.startswith("http") else os.path.basename(link),
        "source": "bse-pdf", "link": link, "posted_on": "BSE",
    }
    row["late"] = row["late_to_company"] or row["late_to_exchange"]
    row["id"] = row_id("bse-pdf", link, row["person"], row["side_raw"], row["qty"], row["value_inr"],
                       trade_from, trade_to, row["remark"])
    return row


def read_filing(filing, cache_dir=CACHE_DIR, text=None):
    """Fetch (once), convert and read one filing's PDF.
    {"status": ok | no-pdftotext | fetch-failed | no-text | unreadable, "note", "form", "rows", "path"}."""
    out = {"status": "ok", "note": None, "form": None, "rows": [], "path": None}
    if text is None:
        try:
            out["path"] = bsepdf.cached_pdf(filing["link"], fetch, cache_dir)
            text = bsepdf.pdf_text(out["path"])
        except bsepdf.NoPdftotext as exc:
            return dict(out, status="no-pdftotext", note=str(exc))
        except bsepdf.NoTextLayer as exc:
            return dict(out, status="no-text", note=str(exc))
        except (InsidersError, OSError) as exc:
            return dict(out, status="fetch-failed", note="could not fetch the PDF: %s" % exc)
        except bsepdf.PdfError as exc:
            return dict(out, status="unreadable", note=str(exc))
    result = bsepdf.read_disclosure(text, bsepdf.hint_from(filing.get("title")))
    out["form"] = result["form"]
    if result["problem"]:
        return dict(out, status="unreadable", note=result["problem"])
    out["rows"] = [pdf_row(f, filing) for f in result["rows"]]
    return out


def parse_filings(filings, cache_dir=CACHE_DIR):
    """Each filing gets "parse" and "rows". Without pdftotext nothing is fetched: one note for all."""
    if not bsepdf.pdftotext_path():
        note = "pdftotext is not installed: brew install poppler, or open the PDF links"
        return [dict(f, parse={"status": "no-pdftotext", "note": note, "form": None}, rows=[]) for f in filings], note
    out = []
    for f in filings:
        got = read_filing(f, cache_dir)
        out.append(dict(f, parse={"status": got["status"], "note": got["note"], "form": got["form"]}, rows=got["rows"]))
    return out, None


def clean_name(raw):
    """Kite's 'TATA CONSULTANCY SERV LT' -> 'Tata Consultancy'; 'RELIANCE INDUSTRIES' -> 'Reliance Industries'."""
    words = [w for w in (raw or "").split() if w.upper() not in NAME_DROP]
    if len(raw or "") >= KITE_NAME_LIMIT and len(words) > 1 and len(words[-1]) <= 4:
        words = words[:-1]
    return " ".join(_word_case(w) for w in words)


def _word_case(w):
    """HDFC, TCS, L&T stay as they are; RELIANCE -> Reliance."""
    if not w.isalpha() or len(w) <= 3 or not re.search(r"[AEIOUaeiou]", w):
        return w
    return w.capitalize()


def company_name(symbol, notes):
    """The company's name from Kite's instrument list; the symbol itself when Kite is down."""
    k = kite.Kite.connect()
    try:
        return clean_name(k.instrument(symbol)["name"]) or symbol
    except kite.KiteError as exc:
        notes.append("Kite: %s (searching by symbol only)" % exc)
        return symbol


def deal_news(symbol, name, hours, now):
    """Google's OR query is loose: only items that name the company (or its symbol) are kept."""
    url = GOOGLE % urllib.parse.quote(DEAL_QUERY % name)
    about = mentions(name, symbol)
    items = [i for i in parse_rss(fetch(url), symbol) if about.search(i["title"] + " " + i["summary"])]
    cutoff = now - _dt.timedelta(hours=hours)
    items = [i for i in items if i["time"] is None or (cutoff <= i["time"] <= now + _dt.timedelta(minutes=10))]
    items.sort(key=lambda i: i["time"] or now, reverse=True)
    return items


def render_news(items, symbol, name, notes, now):
    out = []
    if not items:
        out.append("No bulk deal, block deal, insider or promoter-stake headline for %s (%s) in the window."
                   % (symbol, name))
    for it in items:
        when = it["time"].strftime("%Y-%m-%d %H:%M IST") if it["time"] else "time unknown"
        out.append("%s  %-16s %s" % (when, it["source"][:16], it["title"]))
        tail = (("tags: " + ", ".join(it["tags"]) + "  ") if it["tags"] else "") + it["link"]
        out.append("      " + tail)
    for n in notes:
        out.append("note: %s" % n)
    out.append("Checked %s. Headlines are leads: the NSE CSV is the record. Not financial advice."
               % now.strftime("%Y-%m-%d %H:%M IST"))
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- CLI

def parse_now(text):
    s = text.strip()
    if "T" not in s:
        s = s.replace(" ", "T", 1)
    try:
        return nse.to_ist(_dt.datetime.fromisoformat(s))
    except ValueError:
        raise InsidersError("--now wants '2026-10-29 10:15' (IST) or an ISO time, not %r" % text)


def _parser():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")

    def files(p):
        p.add_argument("--file", action="append", required=True, metavar="PATH",
                       help="an NSE CSV you downloaded; repeat for more files")
        p.add_argument("--kind", choices=["insider", "bulk", "block"],
                       help="override the detection (bulk and block files share a layout)")
        p.add_argument("--symbol", help="only these symbols, comma separated")
        p.add_argument("--since", help="only rows dated on or after YYYY-MM-DD")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("index", help="every row in the file(s), newest first")
    files(p)
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--watchlist", default="trader/watchlist.json")

    p = sub.add_parser("alert", help="rows not shown before, flagged against your positions and watchlist")
    files(p)
    p.add_argument("--state", default="trader/insiders.json")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--watchlist", default="trader/watchlist.json")
    p.add_argument("--now", help="'2026-10-29 10:15' (IST), for reproducible runs")
    p.add_argument("--dry-run", action="store_true", help="do not update the state file")

    p = sub.add_parser("today", help="insider / SAST / pledge filings on BSE's announcements feed today")
    p.add_argument("--symbol", help="only these symbols, comma separated (needs the Kite session for the names)")
    p.add_argument("--parse", action="store_true",
                   help="fetch each new filing's PDF and read its table (needs pdftotext: brew install poppler)")
    p.add_argument("--state", default="trader/insiders.json")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--watchlist", default="trader/watchlist.json")
    p.add_argument("--now", help="'2026-10-29 10:15' (IST), for reproducible runs")
    p.add_argument("--dry-run", action="store_true", help="do not update the state file")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("pdf", help="read one disclosure PDF: a BSE AttachLive link, a saved PDF, or its pdftotext text")
    p.add_argument("source", help="https://www.bseindia.com/xml-data/corpfiling/AttachLive/<guid>.pdf, or a local .pdf / .txt")
    p.add_argument("--symbol", help="the Kite symbol to tag the rows with")
    p.add_argument("--company", help="the company name, when the PDF does not say")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("news", help="bulk deal / block deal / insider / promoter stake headlines for a symbol")
    p.add_argument("symbol")
    p.add_argument("--hours", type=float, default=NEWS_HOURS)
    p.add_argument("--now", help="'2026-10-29 10:15' (IST), for reproducible runs")
    p.add_argument("--json", action="store_true")
    return ap


def _symbols(text):
    return {kite.normalize_symbol(s) for s in (text or "").split(",") if s.strip()} or None


def main(argv=None):
    args = _parser().parse_args(argv)
    if not args.cmd:
        _parser().print_help()
        return 2
    try:
        return _run(args)
    except kite.LoginNeeded as exc:
        print("insiders: %s" % exc, file=sys.stderr)
        return 1
    except (InsidersError, OSError) as exc:
        print("insiders: %s" % exc, file=sys.stderr)
        return 2


def _today(args):
    now = parse_now(args.now) if args.now else nse.now_ist()
    names = None
    if args.symbol:
        k = kite.Kite.connect()
        names = {}
        for sym in sorted(_symbols(args.symbol)):
            try:
                names[sym] = clean_name(k.instrument(sym)["name"]) or sym
            except kite.KiteError:
                names[sym] = sym
    rows = insider_filings(parse_bse(bse_feed(now)), names)
    state = load_state(args.state)
    fresh = new_since(rows, state)
    note = None
    if args.parse and fresh:
        fresh, note = parse_filings(fresh)
    lists = your_lists(args.portfolio, args.watchlist)
    if args.json:
        print(json.dumps({"new": fresh, "checked": now.isoformat(), "note": note}, indent=2))
    else:
        if not fresh:
            print("No new insider, SAST or pledge filing on BSE's feed since %s."
                  % (state.get("last_check") or "the last check"))
        else:
            print("\n".join(render_filings(fresh, new=True, lists=lists)))
        if note:
            print("note: %s" % note)
        print("BSE's announcements feed, today only; %s. Not inside information, not financial advice."
              % ("rows read from the PDFs where the text allows; the NSE CSV is the complete record" if args.parse
                 else "open the PDF for the rows, or run with --parse"))
    if not args.dry_run:
        seen = set(state.get("seen", []))
        seen.update(r["id"] for r in rows)
        state["seen"] = sorted(seen)
        state["last_check"] = now.replace(microsecond=0).isoformat()
        save_state(args.state, state)
    return 0 if fresh else 1


def _pdf(args):
    """`pdf SOURCE`: one disclosure read on its own; a .txt is pdftotext's output (no poppler needed)."""
    src = args.source
    filing = {"company": args.company, "code": None, "title": "", "link": src if src.startswith("http") else "",
              "date": None, "time": None, "symbol": args.symbol}
    text = None
    if not src.startswith("http"):
        if not os.path.isfile(src):
            raise InsidersError("%s is not a file" % src)
        if src.lower().endswith(".txt"):
            with open(src, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        else:
            try:
                text = bsepdf.pdf_text(src)
            except bsepdf.PdfError as exc:
                raise InsidersError(str(exc))
        filing["link"] = src
    got = read_filing(filing, text=text)
    if args.json:
        print(json.dumps({"source": src, "status": got["status"], "note": got["note"], "form": got["form"],
                          "rows": got["rows"]}, indent=2))
    elif got["status"] != "ok":
        print("insiders: %s (%s)" % (got["note"], src), file=sys.stderr)
    else:
        print("\n".join(render_rows(got["rows"], your_lists())))
        print("Read from the PDF (%s); the NSE CSV is the complete record. Not inside information, "
              "not financial advice." % got["form"])
    return 0 if got["rows"] else 1


def _json_items(items):
    return [dict(i, time=i["time"].isoformat() if i["time"] else None) for i in items]


def _run(args):
    if args.cmd == "news":
        now = parse_now(args.now) if args.now else nse.now_ist()
        symbol = kite.normalize_symbol(args.symbol)
        notes = []
        name = company_name(symbol, notes)
        items = deal_news(symbol, name, args.hours, now)
        if args.json:
            print(json.dumps({"symbol": symbol, "company": name, "checked": now.isoformat(),
                              "items": _json_items(items), "notes": notes}, indent=2))
        else:
            sys.stdout.write(render_news(items, symbol, name, notes, now))
        return 0 if items else 1

    if args.cmd == "today":
        return _today(args)

    if args.cmd == "pdf":
        return _pdf(args)

    rows = select(load_files(args.file, args.kind), _symbols(args.symbol), args.since)
    lists = your_lists(args.portfolio, args.watchlist)

    if args.cmd == "index":
        rows = rows[:args.limit]
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            if rows:
                print("\n".join(render_rows(rows, lists)))
            else:
                print("No matching rows in %s." % ", ".join(args.file))
            print(FOOTER)
        return 0 if rows else 1

    # alert
    now = parse_now(args.now) if args.now else nse.now_ist()
    state = load_state(args.state)
    fresh = new_since(rows, state)
    if args.json:
        print(json.dumps({"new": [dict(r, flag=lists.get(r["symbol"])) for r in fresh],
                          "checked": now.isoformat()}, indent=2))
    else:
        if not fresh:
            print("Nothing new since %s." % (state.get("last_check") or "the last check"))
        else:
            print("\n".join(render_rows(fresh, lists, new=True)))
        print(FOOTER)
    if not args.dry_run:
        seen = set(state.get("seen", []))
        seen.update(r["id"] for r in rows)
        state["seen"] = sorted(seen)
        state["last_check"] = now.replace(microsecond=0).isoformat()
        save_state(args.state, state)
    return 0 if fresh else 1


if __name__ == "__main__":
    sys.exit(main())
