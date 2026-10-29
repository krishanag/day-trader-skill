#!/usr/bin/env python3
"""News monitor for trader-news. Standard library only.

Watches headlines for the NSE stocks you watch or hold, plus the market-wide
feeds that carry RBI, SEBI and exchange news, and prints only what is new
since the last check, newest first, in IST, with how far each stock has moved
today and a flag on anything that touches your symbols.

    python3 news.py                                    # watchlist + plan + portfolio symbols
    python3 news.py --tickers RELIANCE,INFY --hours 24
    python3 news.py --tickers TCS --all --hours 72      # everything in the last 3 days, seen or not
    python3 news.py --no-market                         # per-symbol headlines only
    python3 news.py --bse-code RELIANCE=500325          # pin the BSE scrip code for a symbol
    python3 news.py --no-moves                          # skip the price line per symbol
    python3 news.py --now "2026-10-29 10:15" --json     # pin the clock (IST)

Your symbols, when --tickers is not given: every hit in trader/watchlist.json
(trader-scanner), the symbol in trader/plan.json (trader-exit) and the
positions in trader/portfolio.json (trader-broker).

Sources, each read once per check by a plainly identified client (the
User-Agent names this tool, no cookies, no browser impersonation, one
request per feed and no retry past a 403):
  - Google News RSS, one query per symbol built from the company name in
    Kite's instrument list plus the symbol, for example
    q="Infosys" OR INFY (or "Reliance Industries" OR "Reliance shares" when
    the symbol is an English word), with hl=en-IN&gl=IN&ceid=IN:en.
  - Market-wide: the Economic Times stocks feed, the Mint markets feed, the
    Moneycontrol market reports feed and SEBI's RSS (press releases, orders).
    A market-wide item is shown when it names one of your symbols or carries
    a market tag (macro, regulatory, halt, bulk-deal, promoter); another
    company's results or a broker's stock picks are dropped as noise.
  - BSE's corporate announcements RSS (the current day's filings from every
    listed company, about 9,000 items): the filings of your companies, matched
    by the company name (or --bse-code SYM=CODE to pin a scrip code), with the
    PDF link. Fetched once and reused for 10 minutes from
    trader/cache/bse-announcements-<date>-<HHMM>.xml.
  - The move: Kite's quote, the last price against yesterday's close.

Kite: the tool needs today's Zerodha session (python3 trader-kite/login.py)
for the company names and the moves; without one it says so and exits 1.
Times print in IST. State: trader/news_seen.json remembers what you have
seen (30 days), so an item shows once. The tests swap this module's
`_urlopen` for the feeds and `kite._urlopen` for Kite.

Headlines are leads, not facts: open the story before acting, and treat
nothing here as advice.
"""

import argparse
import datetime as _dt
import email.utils
import glob
import hashlib
import html
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
import kite   # noqa: E402
import nse    # noqa: E402

GOOGLE = "https://news.google.com/rss/search?q=%s&hl=en-IN&gl=IN&ceid=IN:en"
MARKET_FEEDS = [
    ("Economic Times", "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms"),
    ("Mint", "https://www.livemint.com/rss/markets"),
    ("Moneycontrol", "https://www.moneycontrol.com/rss/marketreports.xml"),
    ("SEBI", "https://www.sebi.gov.in/sebirss.xml"),
]
BSE_ANNOUNCEMENTS = "https://www.bseindia.com/data/xml/announcements.xml"
BSE_CACHE_MINUTES = 10
CACHE_DIR = kite.CACHE_DIR
UA = "day-trader-skill-india/1.0 (+https://github.com/krishanag/day-trader-skill)"
IST = nse.IST
KEEP_DAYS = 30
MOVE_PCT = 3.0            # a stock this far from yesterday's close is flagged as moving
PAUSE_SECONDS = 0.3       # between feed requests
TIMEOUT = 30
CLOCK_SLACK_MINUTES = 10  # a pinned --now: nothing from after it (plus a little feed clock drift)
NAME_DROP = {"LT", "LTD", "LTD.", "LIMITED"}
KITE_NAME_LIMIT = 22      # Kite truncates long names (TATA CONSULTANCY SERV LT); a short last word is a stub
SMALL_WORDS = {"of", "and", "the"}

KEYWORDS = [
    ("results", r"\bresults?\b|\bearnings\b|\bQ[1-4]FY\d*\b|\bnet profit\b|\bEBITDA\b|\brevenue\b|"
                r"\bguidance\b|\bPAT\b"),
    ("bulk-deal", r"\bbulk deals?\b|\bblock deals?\b"),
    ("deal", r"\bacqui(?:re|res|red|sition)\b|\bmerger\b|\btakeover\b|\bbuyout\b|\bdemerger\b|"
             r"\bjoint venture\b|\bJV\b|(?<!bulk )(?<!block )\bdeals?\b|\bIPO\b|\blisting\b"),
    ("analyst", r"\bupgrade[sd]?\b|\bdowngrade[sd]?\b|\btarget price\b|\bprice target\b|\brating\b|"
                r"\boutperform\b|\bunderperform\b|\boverweight\b|\bunderweight\b|\bbrokerage\b"),
    ("regulatory", r"\bSEBI\b|\bshow[- ]cause\b|\bpenalt|\bprobe\b|\binvestigation\b|\bNCLT\b|"
                   r"\bCCI\b|\bcourt\b|\btribunal\b|\bEnforcement Directorate\b|\bfraud\b|"
                   r"\bcirculars?\b|\bconsultation paper\b|\bboard meeting\b|\bpress release\b"),
    ("macro", r"\bRBI\b|\brepo rate\b|\bMPC\b|\binflation\b|\bCPI\b|\bGDP\b|\bFIIs?\b|\bDIIs?\b|"
              r"\brupee\b|\bcrude\b|\btariffs?\b|\bGST\b|\bFed\b|\bFOMC\b|\bUnion Budget\b|"
              r"\bGift Nifty\b|\bVIX\b"),
    ("people", r"\bCEO\b|\bCFO\b|\bmanaging director\b|\bchairman\b|\bresign|\bsteps down\b|"
               r"\bappoint|\bquits\b"),
    ("halt", r"\bhalt(?:s|ed)?\b|\bcircuit\b|\bsuspen(?:d|ded|sion)\b|\bfreez(?:e|es|ing)\b|"
             r"\btrading ban\b|\bF&O ban\b"),
    ("promoter", r"\bpromoters?\b|\binsider\b|\bpledg(?:e|ed|es)\b|\bstake sale\b|\bopen offer\b|"
                 r"\b(?:buys?|sells?|raises?|cuts?) (?:\d+(?:\.\d+)?% )?stake\b"),
    ("board-meeting", r"\bboard meeting\b|\bmeeting of the board\b|\boutcome of (?:the )?board\b"),
    ("insider-sast", r"\bregulation 7\s*\(|\bregulation (?:29|31)\b|\bSAST\b|\binsider trading\b.{0,40}\bdisclos|"
                     r"\bdisclos.{0,40}\binsider trading\b|\bpledg(?:e|ed|es)\b|\bencumbrance\b"),
    ("dividend", r"\bdividend\b|\bbonus (?:issue|shares?)\b|\bstock split\b|\bsplit\b|\bbuyback\b|"
                 r"\brights issue\b|\brecord date\b|\bex-date\b|\bex-dividend\b"),
]
IMPORTANT_TAGS = {"results", "halt", "bulk-deal", "promoter", "insider-sast"}   # the ones that can gap a stock
MARKET_TAGS = {"macro", "regulatory", "halt", "bulk-deal", "promoter", "insider-sast"}
# a board-meeting notice cites the insider-trading code (the trading window closes); it is not a disclosure
BOARD_NOTICE = re.compile(r"\btrading window\b|\bis scheduled on\b", re.I)   # what earns a market-wide item a place


class NewsError(Exception):
    pass


# ---------------------------------------------------------------- time

def to_ist(dt):
    """Aware datetime -> aware IST (a naive one is taken as IST already)."""
    return nse.to_ist(dt)


def parse_now(text):
    """'2026-10-29 10:15' (IST) or ISO 8601 with an offset -> aware IST datetime."""
    s = text.strip()
    if "T" not in s:
        s = s.replace(" ", "T", 1)
    try:
        return to_ist(_dt.datetime.fromisoformat(s))
    except ValueError:
        raise NewsError("--now wants '2026-10-29 10:15' (IST) or an ISO time, not %r" % text)


DAY_MONTH_YEAR = re.compile(r"^\s*(\d{1,2})[\s-]+([A-Za-z]{3})[a-z]*,?[\s-]+(\d{4})"
                            r"(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?(?:\s+[+-]\d{4})?\s*$")
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_time(value):
    """RFC 822 (RSS), ISO 8601, SEBI's '29 Oct, 2026 +0530' or BSE's '29-Oct-2026 19:32:24' (IST)
    -> aware IST datetime, or None."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        d = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            d = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            d = None
    if d is None:
        m = DAY_MONTH_YEAR.match(value)
        if not m or m.group(2).lower() not in MONTHS:
            return None
        try:
            d = _dt.datetime(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)),
                             int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0), tzinfo=IST)
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(IST)


# ---------------------------------------------------------------- inputs

def load_json(path):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def watch_from_files(portfolio="trader/portfolio.json", watchlist="trader/watchlist.json",
                     plan="trader/plan.json"):
    """{symbol: name or None}: the scanner's hits, the planned trade and your positions."""
    out = {}
    wl = load_json(watchlist) or {}
    for h in wl.get("hits", []):
        sym = h.get("symbol")
        if sym:
            out[kite.normalize_symbol(sym)] = h.get("name")
    pl = load_json(plan) or {}
    if pl.get("symbol"):
        out.setdefault(kite.normalize_symbol(pl["symbol"]), pl.get("name"))
    pf = load_json(portfolio) or {}
    positions = pf.get("positions") or {}
    if isinstance(positions, dict):
        for sym in positions:
            out.setdefault(kite.normalize_symbol(sym), None)
    else:
        for p in positions:
            sym = p.get("symbol")
            if sym:
                out.setdefault(kite.normalize_symbol(sym), p.get("name"))
    return out


def split_tickers(values):
    """['RELIANCE,INFY', 'TCS'] -> ['RELIANCE', 'INFY', 'TCS'] (Kite spellings)."""
    out = []
    for v in values or []:
        for part in v.split(","):
            if part.strip():
                sym = kite.normalize_symbol(part)
                if sym not in out:
                    out.append(sym)
    return out


def clean_name(raw):
    """Kite's 'TATA CONSULTANCY SERV LT' -> 'Tata Consultancy'; 'RELIANCE INDUSTRIES' -> 'Reliance Industries'."""
    words = [w for w in (raw or "").split() if w.upper() not in NAME_DROP]
    if len(raw or "") >= KITE_NAME_LIMIT and len(words) > 1 and len(words[-1]) <= 4:
        words = words[:-1]   # a truncated stub like SERV
    out = []
    for i, w in enumerate(words):
        low = w.lower()
        out.append(low if (low in SMALL_WORDS and i) else _word_case(w))
    return " ".join(out)


def _word_case(w):
    """HDFC, TCS, L&T stay as they are; RELIANCE -> Reliance."""
    if not w.isalpha() or len(w) <= 3 or not re.search(r"[AEIOUaeiou]", w):
        return w
    return w.capitalize()


def company_names(k, watch, errors):
    """{symbol: display name}: Kite's instrument list first, then the watchlist's name, then the symbol."""
    names = {}
    table = {}
    if k is not None:
        try:
            table = k.instruments("NSE")
        except kite.KiteError as exc:
            errors.append("Kite instrument list: %s (querying by symbol only)" % exc)
    for sym, fallback in watch.items():
        row = table.get(sym)
        if row is not None:
            names[sym] = clean_name(row.get("name")) or sym
        elif table:
            errors.append("%s is not listed on NSE (check the spelling); skipped" % sym)
        else:
            names[sym] = clean_name(fallback) if fallback else sym
    return names


def google_query(symbol, name):
    """'"Infosys" OR INFY'; a symbol that is a word of the name (RELIANCE) would match the English word,
    so it becomes '"Reliance Industries" OR "Reliance shares"'."""
    if not name or name.upper() == symbol:
        return symbol
    if symbol.lower() in name.lower().split():
        return '"%s" OR "%s shares"' % (name, symbol.capitalize())
    return '"%s" OR %s' % (name, symbol)


# ---------------------------------------------------------------- fetching

_urlopen = urllib.request.urlopen   # tests swap this
_sleep = time.sleep


class Fetcher(object):
    def __init__(self, pause=PAUSE_SECONDS):
        self.pause = pause
        self._last = None

    def get(self, url):
        if self._last is not None:
            gap = time.monotonic() - self._last
            if gap < self.pause:
                _sleep(self.pause - gap)
        self._last = time.monotonic()
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with _urlopen(req, timeout=TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            raise NewsError("HTTP %s for %s" % (exc.code, url.split("?")[0]))
        except urllib.error.URLError as exc:
            raise NewsError("could not reach %s: %s" % (url.split("?")[0], exc.reason))


# ---------------------------------------------------------------- parsing

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _text(el, name):
    for c in el:
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return ""


def strip_html(text):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text or "")).split())


def parse_rss(data, source, symbol="market", feed=None):
    """An RSS 2.0 feed -> items. Google News items carry the publisher in <source>."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("%s feed is not valid XML: %s" % (source, exc))
    out = []
    for it in root.iter("item"):
        title = html.unescape(_text(it, "title"))
        link = _text(it, "link")
        guid = _text(it, "guid") or link
        publisher = _text(it, "source")
        if publisher and title.endswith(" - " + publisher):
            title = title[: -len(" - " + publisher)].rstrip()
        out.append({"id": "%s:%s" % (feed or source, guid), "feed": feed or source,
                    "source": publisher or source, "kind": "headline", "symbol": symbol,
                    "title": " ".join(title.split()), "summary": strip_html(_text(it, "description")),
                    "link": link, "time": parse_time(_text(it, "pubDate"))})
    return out


def bse_feed(fetcher, now, cache_dir=CACHE_DIR):
    """The day's BSE announcements XML, fetched once and reused for BSE_CACHE_MINUTES
    (trader/cache/bse-announcements-<date>-<HHMM>.xml; every tool in the pack shares it)."""
    fresh = sorted(p for p in glob.glob(os.path.join(cache_dir, "bse-announcements-*.xml"))
                   if time.time() - os.path.getmtime(p) < BSE_CACHE_MINUTES * 60)
    if fresh:
        with open(fresh[-1], "rb") as fh:
            return fh.read()
    data = fetcher.get(BSE_ANNOUNCEMENTS)
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
        raise NewsError("BSE announcements feed is not valid XML: %s" % exc)
    out = []
    for it in root.iter("item"):
        title = html.unescape(_text(it, "title"))
        m = re.match(r"^(.*?)\s*\((\d+)\)\s*$", title)
        company, code = (m.group(1), m.group(2)) if m else (title, _text(it, "scripcode"))
        if not company:
            continue
        out.append({"company": " ".join(company.split()), "code": code,
                    "description": " ".join(html.unescape(_text(it, "description")).replace("''", "'").split()),
                    "link": _text(it, "link"), "time": parse_time(_text(it, "pubDate"))})
    return out


def _company_key(text):
    """'Reliance Industries Ltd-$' / 'Reliance Industries' -> 'reliance industries'."""
    words = [w for w in re.sub(r"[^A-Za-z0-9& ]+", " ", text or "").lower().split() if w not in NAME_DROP_LOW]
    return " ".join(words)


NAME_DROP_LOW = {w.lower().strip(".") for w in NAME_DROP}


def match_bse(entries, names, codes=None):
    """Your companies' filings as items: by scrip code when --bse-code pinned one, else by the name prefix
    (Kite's 'Tata Consultancy' matches 'Tata Consultancy Services Ltd')."""
    codes = codes or {}
    items = []
    for sym, name in names.items():
        key = _company_key(name) if name and name.upper() != sym else ""
        for e in entries:
            if sym in codes:
                hit = e["code"] == codes[sym]
            else:
                company = _company_key(e["company"])
                hit = bool(key) and (company.startswith(key) or key.startswith(company))
            if not hit:
                continue
            title = e["description"] or "filing"
            items.append({"id": "BSE:" + (e["link"] or title), "feed": "BSE", "source": "BSE filing",
                          "kind": "filing", "symbol": sym, "title": title[:160], "summary": "",
                          "company": e["company"], "code": e["code"], "link": e["link"], "time": e["time"]})
    return items


def parse_bse_codes(values):
    """['RELIANCE=500325', 'INFY=500209'] -> {symbol: code}."""
    out = {}
    for v in values or []:
        sym, _, code = v.partition("=")
        if not code.strip().isdigit():
            raise NewsError("--bse-code wants SYMBOL=SCRIPCODE, for example RELIANCE=500325, not %r" % v)
        out[kite.normalize_symbol(sym)] = code.strip()
    return out


def parse_quotes(quotes, symbols):
    """Kite quote dicts -> [{"symbol", "price", "prev_close", "pct", "time"}], skipping blanks."""
    out = []
    for sym in symbols:
        q = quotes.get(sym) or {}
        price = q.get("last_price")
        prev = (q.get("ohlc") or {}).get("close")
        if not price or not prev:
            continue
        when = q.get("last_trade_time") or q.get("timestamp")
        try:
            t = to_ist(_dt.datetime.fromisoformat(str(when).replace(" ", "T"))) if when else None
        except ValueError:
            t = None
        out.append({"symbol": sym, "price": round(float(price), 2), "prev_close": round(float(prev), 2),
                    "pct": round((float(price) / float(prev) - 1) * 100, 2), "time": t})
    return out


def collect_moves(k, symbols, errors):
    if k is None or not symbols:
        return []
    try:
        return parse_quotes(k.quotes(symbols), symbols)
    except kite.KiteError as exc:
        errors.append("Kite quotes: %s" % exc)
        return []


def mention_patterns(names):
    pats = {}
    for sym, name in names.items():
        alts = []
        if len(sym) >= 3:
            alts.append(r"\b%s\b" % re.escape(sym))
        if name and name.upper() != sym and len(name) >= 4:
            alts.append(r"(?i)\b%s\b" % re.escape(name).replace(r"\ ", r"[\s-]+"))
        if alts:
            pats[sym] = [re.compile(a) for a in alts]
    return pats


def tag(items, names):
    """Which of your symbols each item touches, and why it matters."""
    pats = mention_patterns(names)
    for it in items:
        hay = "%s %s" % (it["title"], it.get("summary") or "")
        touches = {it["symbol"]} if it["symbol"] in names else set()
        for sym, rxs in pats.items():
            if any(rx.search(hay) for rx in rxs):
                touches.add(sym)
        it["touches"] = sorted(touches)
        it["tags"] = [label for label, rx in KEYWORDS if re.search(rx, hay, re.I)]
        if "insider-sast" in it["tags"] and BOARD_NOTICE.search(hay):
            it["tags"].remove("insider-sast")
        it["important"] = bool(set(it["tags"]) & IMPORTANT_TAGS)
    return items


def dedupe(items):
    """One item per story: same id, or same link, or same title the same day."""
    seen, out = {}, []
    for it in items:
        keys = [it["id"], it["link"]]
        if it["kind"] != "filing":   # two filings can share a bland description ("Enclosed") and still be two
            keys.append("%s|%s" % (re.sub(r"\W+", " ", it["title"].lower()).strip(),
                                   it["time"].date() if it["time"] else ""))
        hit = next((seen[k] for k in keys if k and k in seen), None)
        if hit is not None:
            for t in it.get("touches", []):
                if t not in hit["touches"]:
                    hit["touches"].append(t)
            hit["touches"].sort()
            continue
        for k in keys:
            if k:
                seen[k] = it
        out.append(it)
    return out


def fingerprint(it):
    return hashlib.sha1(it["id"].encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- the check

def collect(fetcher, names, market=True, errors=None, bse=True, now=None, codes=None):
    errors = [] if errors is None else errors
    items = []
    if bse and names:
        try:
            items += match_bse(parse_bse(bse_feed(fetcher, now or nse.now_ist())), names, codes)
        except NewsError as exc:
            errors.append("BSE announcements: %s" % exc)
    for sym in sorted(names):
        url = GOOGLE % urllib.parse.quote(google_query(sym, names[sym]))
        try:
            items += parse_rss(fetcher.get(url), "Google News", sym, feed="Google News")
        except NewsError as exc:
            errors.append("headlines for %s: %s" % (sym, exc))
    if market:
        for source, url in MARKET_FEEDS:
            try:
                items += parse_rss(fetcher.get(url), source, feed=source)
            except NewsError as exc:
                errors.append("%s feed: %s" % (source, exc))
    return items


def select(items, state, now, hours=48, show_all=False):
    """Items within the window; only unseen ones unless show_all. A market-wide
    item needs to touch one of your symbols or carry a market tag (macro,
    regulatory, halt, bulk-deal, promoter) to earn a place; another company's
    results or a broker's stock picks are noise."""
    seen = state.setdefault("seen", {})
    cutoff = now - _dt.timedelta(hours=hours)
    ahead = now + _dt.timedelta(minutes=CLOCK_SLACK_MINUTES)
    out = []
    for it in items:
        if it["time"] is not None and (it["time"] < cutoff or it["time"] > ahead):
            continue
        if it["symbol"] == "market" and not it["touches"] and not (set(it["tags"]) & MARKET_TAGS):
            continue
        fp = fingerprint(it)
        it["new"] = fp not in seen
        if it["new"] or show_all:
            out.append(it)
    out.sort(key=lambda i: (i["time"] or now), reverse=True)
    return out


def remember(items, state, now):
    seen = state.setdefault("seen", {})
    stamp = now.isoformat()
    for it in items:
        seen.setdefault(fingerprint(it), stamp)
    cutoff = (now - _dt.timedelta(days=KEEP_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    state["last_check"] = stamp


def render_moves(moves, move_pct=MOVE_PCT):
    out = []
    for m in moves:
        flag = "MOVING" if abs(m["pct"]) >= move_pct else ""
        when = (" at %s IST" % to_ist(m["time"]).strftime("%H:%M")) if m.get("time") else ""
        out.append("%-10s %s %+.2f%% vs yesterday's close %s%s  %s" % (
            m["symbol"], "{:,.2f}".format(m["price"]), m["pct"], "{:,.2f}".format(m["prev_close"]),
            when, flag))
    return out


def render(items, errors, now, moves=None, move_pct=MOVE_PCT):
    out = render_moves(moves or [], move_pct)
    if out:
        out.append("")
    if not items:
        out.append("Nothing new.")
    for it in items:
        when = to_ist(it["time"]).strftime("%Y-%m-%d %H:%M IST") if it["time"] else "time unknown"
        mark = "!" if it.get("important") else " "
        who = ",".join(it["touches"]) or it["symbol"]
        out.append("%s %s  %-12s %-16s %s" % (mark, when, who, it["source"][:16], it["title"]))
        extra = []
        if it["tags"]:
            extra.append("tags: " + ", ".join(it["tags"]))
        extra.append(it["link"])
        out.append("      " + "  ".join(extra))
    for e in errors:
        out.append("note: %s" % e)
    out.append("Checked %s. Headlines are leads, not facts; open the story before acting. "
               "Not financial advice." % to_ist(now).strftime("%Y-%m-%d %H:%M IST"))
    return "\n".join(out) + "\n"


def _json_ready(items):
    out = []
    for it in items:
        d = dict(it)
        d["time"] = it["time"].isoformat() if it["time"] else None
        out.append(d)
    return out


def connect(errors):
    """Today's Kite session, or None with a note when Kite itself is down. No session: LoginNeeded."""
    try:
        return kite.Kite.connect()
    except kite.LoginNeeded:
        raise
    except kite.KiteError as exc:
        errors.append("Kite: %s (company names and moves skipped)" % exc)
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tickers", nargs="*", help="symbols, comma or space separated; default: "
                    "watchlist + plan + portfolio")
    ap.add_argument("--portfolio", default="trader/portfolio.json")
    ap.add_argument("--watchlist", default="trader/watchlist.json")
    ap.add_argument("--plan", default="trader/plan.json")
    ap.add_argument("--state", default="trader/news_seen.json")
    ap.add_argument("--hours", type=float, default=48, help="ignore items older than this")
    ap.add_argument("--all", action="store_true", help="show seen items too")
    ap.add_argument("--no-market", action="store_true", help="per-symbol headlines only")
    ap.add_argument("--no-bse", action="store_true", help="skip the day's BSE filings")
    ap.add_argument("--bse-code", action="append", metavar="SYM=CODE",
                    help="pin a symbol to a BSE scrip code (RELIANCE=500325); repeatable")
    ap.add_argument("--no-moves", action="store_true", help="skip the price line per symbol")
    ap.add_argument("--move-pct", type=float, default=MOVE_PCT, help="flag a move this big (percent)")
    ap.add_argument("--now", help="'2026-10-29 10:15' (IST), for reproducible runs")
    ap.add_argument("--dry-run", action="store_true", help="do not update the state file")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.tickers:
        watch = {s: None for s in split_tickers(args.tickers)}
        known = watch_from_files(args.portfolio, args.watchlist, args.plan)
        watch.update({s: known[s] for s in watch if known.get(s)})
    else:
        watch = watch_from_files(args.portfolio, args.watchlist, args.plan)
    if not watch:
        print("news: no symbols. Pass --tickers, or run /trader-scanner first (it writes "
              "trader/watchlist.json).", file=sys.stderr)
        return 2
    try:
        now = parse_now(args.now) if args.now else nse.now_ist()
        codes = parse_bse_codes(args.bse_code)
    except NewsError as exc:
        print("news: %s" % exc, file=sys.stderr)
        return 2
    errors = []
    try:
        k = connect(errors)
    except kite.LoginNeeded as exc:
        print("news: %s" % exc, file=sys.stderr)
        return 1
    names = company_names(k, watch, errors)
    state = load_json(args.state) or {}
    fetcher = Fetcher()
    items = collect(fetcher, names, market=not args.no_market, errors=errors, bse=not args.no_bse, now=now,
                    codes=codes)
    items = dedupe(tag(items, names))
    shown = select(items, state, now, hours=args.hours, show_all=args.all)
    moves = [] if args.no_moves else collect_moves(k, sorted(names), errors)
    if args.json:
        mv = [dict(m, time=m["time"].isoformat() if m.get("time") else None,
                   moving=abs(m["pct"]) >= args.move_pct) for m in moves]
        print(json.dumps({"checked": now.isoformat(), "moves": mv, "items": _json_ready(shown),
                          "notes": errors}, indent=2))
    else:
        sys.stdout.write(render(shown, errors, now, moves, args.move_pct))
    if not args.dry_run:
        remember(items, state, now)
        folder = os.path.dirname(args.state)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(args.state, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
            fh.write("\n")
    return 0 if any(i["new"] for i in shown) else 1


if __name__ == "__main__":
    sys.exit(main())
