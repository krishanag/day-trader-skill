#!/usr/bin/env python3
"""Chart Scanner for trader-scanner: scan NSE charts for setups through Zerodha Kite. Standard library only.

    python3 scan.py universe                              # Kite's NSE list + one quote pass -> trader/universe.txt
    python3 scan.py daily                                 # before the open: daily setups -> trader/watchlist.json
    python3 scan.py daily --symbols RELIANCE,INFY,SBIN    # or --watchlist FILE, or --limit 200
    python3 scan.py orb                                   # after 09:35 IST: opening-range breakouts so far
    python3 scan.py orb --at 10:15 --or-minutes 15        # cut the clock at 10:15 IST
    python3 scan.py orb --now "2026-10-29 10:15" --symbols RELIANCE,SJVN   # replay a past session from Kite

universe  Reads Kite's NSE instrument list (k.instruments, cached per day) and
          keeps a row when, in this order:
            1. it is an NSE equity row (indices are not; kite.py drops them)
            2. the trading symbol has no two-character series suffix: XYZ-BE,
               -BZ, -SM, -ST, -IT, -SG, -N0 and the rest are the trade-for-
               trade, SME, bond and gilt series, out (7,173 of the 10,251 rows
               on 2026-10-29). BAJAJ-AUTO and NAM-INDIA keep their hyphen (the
               part after it is not a series code)
            3. the name does not mark a fund unit: the word ETF or NAV (the
               iNAV ticks), or an AMC's scheme listing ("DSPAMC - DSPGOLD",
               "BFAM - ...")
          then one quote pass (k.quotes, 500 symbols a call, 1 call a second,
          cached per day under trader/cache/quotes-<date>.json) keeps a stock
          when last_price >= --min-price (Rs 50) and the traded value, volume
          times last_price, is at least --min-value (Rs 5 crore = 50000000).
          Before the open the quote carries the previous session's figures;
          during the session it is the day so far.

daily     Pre-market scan of daily CLOSES: one "day" historical call per
          symbol for the last 60 sessions (3 calls a second, so 1,000 symbols
          take about six minutes), cached per symbol per session under
          trader/cache/daily/ so a re-run the same day is free. Liquidity from
          the last complete daily bar: close >= --min-price (Rs 50) and close
          times volume >= --min-value (Rs 5 crore). Setups, from closes only:
            breakout_20d     the last close is above every close of the 20 before it
            bull_flag        up more than 10 % over the 10 sessions before the
                             last 5, then the last 5 closes inside a 5 % range
            reclaim_ma20     closed under the 20-day average, then closed above it
            oversold         RSI 14 (Wilder) under 30
            oversold_bounce  RSI 14 was under 30 yesterday and today closed up
          Today's daily bar is dropped until 15:35 IST, when the closing
          auction has set the day's close.

orb       Opening-range breakouts on 5-minute bars. The range is the first
          --or-minutes (15) of the session: the 09:15, 09:20 and 09:25 bars.
          Stage 1, when the scan day is today: one quote pass for every
          symbol (500 a call). A chart goes on only if the day's high is
          above the day's open (a necessary condition: a close above the
          range high is above the open) and it clears the price floor.
          Stage 2, each candidate: the 5-minute candles from Kite, one call
          each at 3 a second. Range high = the highest high of the range
          bars, a hit = the first completed bar to CLOSE above it, at that
          bar's close time (bars are numbered from 1 at 09:15, so the 09:35
          bar is bar 5 and it closes at 09:40). Stage 2 fetches at most --max-confirm (200)
          charts, the most traded candidates first (price x volume so far),
          and says so when it stops short. A chart is usable only when every
          5-minute bar from 09:15 to the cutoff is there (a missing bar is a
          halt or no trades). Floors: --min-price 50, --min-value 0.
          Replaying a past day (--now with another date) skips stage 1 and
          fetches candles for every symbol given.

Bars: a bar counts only once it is complete (start + 5 minutes <= the
clock). Candles for a stock in the Closing Auction Session (the F&O names,
k.cas_symbols) end with the 15:10 bar; every other stock runs to 15:25.
Times are IST. --now pins the clock ("2026-10-29 10:15", IST).

Data comes from kite.Kite.connect(): today's session in trader/kite_session.json.
Without one the tool prints the login message and exits 1.

A scan is a list of charts that match a rule, not a list of stocks to buy.
Exit code 0 when the scan found something, 1 when it did not or could not run.
"""

import argparse
import datetime
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trader-kite"))
import kite   # noqa: E402
import nse    # noqa: E402

BAR_MIN = 5
MAX_CONFIRM = 200        # stage-2 charts per orb scan, most traded first
MIN_DAILY_BARS = 30
DAILY_SESSIONS = 60      # sessions of daily bars fetched per symbol
DAILY_BAR_DONE = nse.CAS_END   # the closing auction ends; the day's close is final
CRORE = 1e7
DEFAULT_MIN_PRICE = 50.0
DEFAULT_MIN_VALUE = 5 * CRORE
DEFAULT_NO_ENTRIES_AFTER = "14:30"
DEFAULT_UNIVERSE = os.path.join("trader", "universe.txt")
DEFAULT_WATCHLIST = os.path.join("trader", "watchlist.json")
DEFAULT_CONFIG = os.path.join("trader", "config.json")
DAILY_CACHE = os.path.join(kite.CACHE_DIR, "daily")
NOTE = "A scan is a list of charts that match a rule, not a list of stocks to buy."

SETUPS = [
    ("breakout_20d", "20-day closing-high breakout"),
    ("bull_flag", "bull flag"),
    ("reclaim_ma20", "reclaimed the 20-day average"),
    ("oversold", "oversold (RSI under 30)"),
    ("oversold_bounce", "oversold bounce"),
]
LABEL = dict(SETUPS)


class ScanError(Exception):
    pass


# ---------------------------------------------------------------- time

def real_today():
    """The machine's date in IST (tests pin it)."""
    return nse.now_ist().date()


def parse_now(text):
    """'2026-10-29 10:15' (IST) or ISO with an offset -> aware IST datetime. None = now."""
    if not text:
        return nse.now_ist().replace(microsecond=0)
    try:
        dt = datetime.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ScanError('--now wants a time like "2026-10-29 10:15" (IST)')
    return nse.to_ist(dt)


def parse_hhmm(text, flag="--at"):
    try:
        return nse.parse_hhmm(text)
    except ValueError:
        raise ScanError("%s wants an IST time like 10:15" % flag)


def clock_label(t):
    """datetime or time -> '10:15' (24-hour, IST)."""
    return "%02d:%02d" % (t.hour, t.minute)


def day_label(d):
    return "%s %s" % (d.strftime("%a"), d.isoformat())


def iso(dt):
    return dt.replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- universe

SERIES_SUFFIX = re.compile(r"-[A-Z0-9]{2}$")
FUND_NAME = re.compile(r"\bETF\b|\bI?NAV\b|AMC -|BFAM -")


def classify(symbol, row):
    """One Kite instrument row -> 'keep' or the rule that drops it (before the quote pass)."""
    if SERIES_SUFFIX.search(symbol):
        return "other series"
    if FUND_NAME.search((row.get("name") or "").upper()):
        return "fund unit"
    return "keep"


def liquidity(quote):
    """(last price, rupees traded) from a Kite quote; (None, None) when it has no trade."""
    price = quote.get("last_price")
    if not price:
        return None, None
    return float(price), float(price) * float(quote.get("volume") or 0)


def build_universe(table, quotes, min_price, min_value):
    """Pure. table = k.instruments(), quotes = {sym: quote} -> (kept [(SYM, name)], counts)."""
    kept, counts = [], {"rows": len(table)}
    for sym in sorted(table):
        why = classify(sym, table[sym])
        if why != "keep":
            counts[why] = counts.get(why, 0) + 1
            continue
        q = quotes.get(sym)
        price, value = liquidity(q) if q else (None, None)
        if price is None:
            why = "no trade"
        elif price < min_price:
            why = "under %s" % money(min_price)
        elif value < min_value:
            why = "under %s traded" % crore(min_value)
        else:
            kept.append((sym, table[sym].get("name") or sym))
            continue
        counts[why] = counts.get(why, 0) + 1
    counts["keep"] = len(kept)
    return kept, counts


def quote_pass(k, symbols, day, cache_dir=kite.CACHE_DIR, progress=None):
    """{sym: trimmed quote} for many symbols, 500 a call. With a cache_dir the pass is saved
    once per day (the universe); None fetches fresh quotes every time (orb's stage 1)."""
    path = os.path.join(cache_dir, "quotes-%s.json" % day.isoformat()) if cache_dir else None
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            cached = json.load(fh)
        if all(s in cached for s in symbols):
            return cached
    if progress:
        progress("Quotes for %s symbols: %d calls at one a second..."
                 % ("{:,}".format(len(symbols)), (len(symbols) + kite.QUOTE_BATCH - 1) // kite.QUOTE_BATCH))
    raw = k.quotes(symbols)
    out = {s: {"last_price": q.get("last_price"), "volume": q.get("volume"), "ohlc": q.get("ohlc"),
               "last_trade_time": q.get("last_trade_time")} for s, q in raw.items()}
    if path:
        os.makedirs(cache_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(out, fh)
    return out


# ---------------------------------------------------------------- daily setups

def rsi(closes, n=14):
    """Wilder's RSI on closes."""
    if len(closes) < n + 1:
        return None
    g = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    lo = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(lo[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + lo[i]) / n
    return 100 - 100 / (1 + ag / al) if al else 100.0


def through_session(now):
    """The last session whose daily bar is complete at `now` (IST)."""
    day = now.date()
    if nse.trading_day(day)[0] and now.time() >= DAILY_BAR_DONE:
        return day
    return nse.previous_trading_days(day, 1)[0]


def daily_range(through):
    """(start, end) aware IST datetimes covering DAILY_SESSIONS sessions up to `through`."""
    days = nse.previous_trading_days(through + datetime.timedelta(days=1), DAILY_SESSIONS)
    return nse.at(days[0], datetime.time(0, 0)), nse.at(through, datetime.time(23, 59, 59))


def daily_bars(candles, through):
    """[(date, close, volume)] of complete daily bars: anything after `through` dropped."""
    out = {}
    for t, _o, _h, _l, c, v in candles:
        d = nse.to_ist(t).date()
        if d <= through:
            out[d] = (float(c), int(v))   # one bar a day; a repeat at a chunk edge wins
    return [(d, c, v) for d, (c, v) in sorted(out.items())]


def find_setups(closes):
    """closes (oldest first, >= 30) -> (setup keys, numbers)."""
    c = closes
    last = c[-1]
    hi20 = max(c[-21:-1])
    ma20 = sum(c[-20:]) / 20.0
    ma20_prev = sum(c[-21:-1]) / 20.0
    run = c[-6] / c[-16] - 1
    rng5 = (max(c[-5:]) - min(c[-5:])) / last
    r = rsi(c)
    r_prev = rsi(c[:-1])
    tags = []
    if last > hi20:
        tags.append("breakout_20d")
    if run > 0.10 and rng5 < 0.05:
        tags.append("bull_flag")
    if c[-2] < ma20_prev and last > ma20:
        tags.append("reclaim_ma20")
    if r is not None and r < 30:
        tags.append("oversold")
    if r_prev is not None and r_prev < 30 and last > c[-2]:
        tags.append("oversold_bounce")
    return tags, {"rsi": r, "rsi_prev": r_prev, "high20": hi20, "ma20": ma20,
                  "run_pct": run * 100, "range5_pct": rng5 * 100}


def daily_detail(tags, nums, last):
    parts = []
    for t in tags:
        if t == "breakout_20d":
            parts.append("closed %s over the 20-day closing high %s" % (money(last), money(nums["high20"])))
        elif t == "bull_flag":
            parts.append("up %.1f %% then %.1f %% range over 5 sessions" % (nums["run_pct"], nums["range5_pct"]))
        elif t == "reclaim_ma20":
            parts.append("closed back above the 20-day average %s" % money(nums["ma20"]))
        elif t == "oversold":
            parts.append("RSI %.1f" % nums["rsi"])
        elif t == "oversold_bounce":
            parts.append("RSI %.1f yesterday, closed up" % nums["rsi_prev"])
    return "; ".join(parts)


def scan_daily(data, symbols, through, min_price, min_value):
    """Pure. data = {SYM: candles}. Returns (hits, counts)."""
    hits = []
    counts = {"requested": len(symbols), "fetched": 0, "illiquid": 0, "short": 0, "scanned": 0}
    for sym in symbols:
        candles = data.get(sym)
        if not candles:
            continue
        counts["fetched"] += 1
        bars = daily_bars(candles, through)
        if len(bars) < MIN_DAILY_BARS:
            counts["short"] += 1
            continue
        _d, price, vol = bars[-1]
        if price < min_price or price * vol < min_value:
            counts["illiquid"] += 1
            continue
        counts["scanned"] += 1
        closes = [c for _, c, _ in bars]
        tags, nums = find_setups(closes)
        if tags:
            hits.append({"symbol": sym, "setup": tags[0], "setups": tags, "price": round(price, 2),
                         "close_date": bars[-1][0].isoformat(), "rsi": round(nums["rsi"], 1),
                         "volume": vol, "value_inr": round(price * vol, 2),
                         "detail": daily_detail(tags, nums, price)})
    hits.sort(key=lambda h: (-h["value_inr"], h["symbol"]))   # most rupees traded first
    counts["hits"] = len(hits)
    return hits, counts


# ---------------------------------------------------------------- opening-range breakout

def slot(day, k):
    return nse.at(day, nse.OPEN) + datetime.timedelta(minutes=BAR_MIN * k)


def session_bars(candles, day, cutoff, cas=False):
    """5-minute bars of `day` that are complete at `cutoff` (aware IST), on the grid,
    inside continuous trading. Sorted, one per slot."""
    end = nse.at(day, nse.continuous_end(cas))
    out = {}
    for t, o, h, l, c, v in candles:
        start = nse.to_ist(t)
        if start.date() != day or start.second or start.minute % BAR_MIN:
            continue   # another day, or off the grid
        if start < slot(day, 0) or start >= end:
            continue   # pre-open print or the closing auction
        if start + datetime.timedelta(minutes=BAR_MIN) > cutoff:
            continue   # still in progress at the cutoff
        out[start] = {"start": start, "o": float(o), "h": float(h), "l": float(l), "c": float(c), "v": int(v)}
    return [out[k] for k in sorted(out)]


def usable(bars, day, cutoff, n_range, cas=False):
    """Every slot from 09:15 to the cutoff is there, and at least the range."""
    want = int((cutoff - slot(day, 0)).total_seconds() // (BAR_MIN * 60))
    end = nse.at(day, nse.continuous_end(cas))
    want = min(want, int((end - slot(day, 0)).total_seconds() // (BAR_MIN * 60)))
    if want < n_range or len(bars) != want:
        return False
    return all(b["start"] == slot(day, k) for k, b in enumerate(bars))


def orb_candidate(quote):
    """Stage 1, from a quote: the day's high is above the day's open. Necessary for a
    breakout (a close above the range high is above the open)."""
    ohlc = quote.get("ohlc") or {}
    high, opn = float(ohlc.get("high") or 0), float(ohlc.get("open") or 0)
    return high > opn > 0


def orb_hit(bars, n_range):
    """Stage 2, real highs and lows: the first completed bar to CLOSE above the range high."""
    rng = bars[:n_range]
    oh = max(b["h"] for b in rng)
    ol = min(b["l"] for b in rng)
    for k in range(n_range, len(bars)):
        b = bars[k]
        if b["c"] > oh:
            closes_at = b["start"] + datetime.timedelta(minutes=BAR_MIN)
            return {"price": round(b["c"], 2), "at": clock_label(closes_at),
                    "bar_start": clock_label(b["start"]), "bar": k + 1,   # 1-based, bar 1 = the 09:15 bar
                    "or_high": round(oh, 2), "or_low": round(ol, 2),
                    "pct_over": round((b["c"] / oh - 1) * 100, 2), "_t": closes_at}
    return None


def orb_window(now, at, or_minutes, until):
    """-> (day, cutoff aware IST) or raise ScanError with the plain message."""
    now = nse.to_ist(now)
    day = now.date()
    if or_minutes < BAR_MIN or or_minutes % BAR_MIN or or_minutes > 120:
        raise ScanError("--or-minutes wants a multiple of 5 from 5 to 120 (15 is the classic)")
    ok, why = nse.trading_day(day)
    if not ok:
        raise ScanError("No session on %s (%s): the market is closed. Run orb on a trading day after the "
                        "opening range, or replay a past day with --now \"YYYY-MM-DD HH:MM\"."
                        % (day_label(day), why))
    range_end = slot(day, or_minutes // BAR_MIN)
    first_close = range_end + datetime.timedelta(minutes=BAR_MIN)
    close_dt = nse.at(day, nse.CLOSE)
    if at is not None:
        cutoff = nse.at(day, at)
        if cutoff > now + datetime.timedelta(seconds=59):
            raise ScanError("--at %s is later than now (%s IST)." % (clock_label(at), clock_label(now)))
        if cutoff > close_dt:
            raise ScanError("--at %s is after the %s close on %s." % (clock_label(at), clock_label(close_dt),
                                                                       day_label(day)))
    else:
        cutoff = now.replace(second=0, microsecond=0)
        if cutoff >= close_dt:
            raise ScanError("The market closed at %s IST on %s. Replay the session with --at 10:15."
                            % (clock_label(close_dt), day_label(day)))
    if cutoff < first_close:
        phase = nse.phase(cutoff)
        lead = "It is %s IST" % clock_label(cutoff)
        if phase == "closed":
            lead += " and the market opens at 09:15"
        elif phase == "pre-open":
            lead += ", the pre-open"
        raise ScanError("%s: the %d-minute opening range runs 09:15 to %s and the first bar after it closes "
                        "at %s. Run orb again after %s."
                        % (lead, or_minutes, clock_label(range_end), clock_label(first_close),
                           clock_label(first_close)))
    if until is not None and cutoff.time() > until:
        raise ScanError("It is %s IST, after %s, your last time for new entries (no_entries_after): an "
                        "opening-range breakout found now is expired. Run orb tomorrow after %s, or replay "
                        "today with --at 10:15." % (clock_label(cutoff), clock_label(until),
                                                     clock_label(first_close)))
    return day, cutoff


def scan_orb(symbols, day, cutoff, or_minutes, bars_for, quotes=None, min_price=0.0, min_value=0.0,
             max_confirm=None, cas=frozenset()):
    """bars_for(sym) -> candles (raises kite.KiteError). quotes = {sym: quote} for stage 1,
    or None when every symbol goes straight to stage 2 (a past day). With max_confirm, only
    the max_confirm most traded candidates are fetched; the rest are counted as unconfirmed.
    Returns (hits, counts, failed {sym: why})."""
    n_range = or_minutes // BAR_MIN
    counts = {"requested": len(symbols), "illiquid": 0, "flat": 0, "candidates": 0, "unconfirmed": 0,
              "fetched": 0, "unusable": 0, "scanned": 0, "confirmed": 0}
    hits, failed, cands = [], {}, []
    for sym in symbols:
        if quotes is None:
            cands.append((sym, 0.0))
            continue
        q = quotes.get(sym)
        price, value = liquidity(q) if q else (None, None)
        if price is None:
            failed[sym] = "no quote from Kite"
            continue
        if price < min_price or value < min_value:
            counts["illiquid"] += 1
            continue
        if not orb_candidate(q):
            counts["flat"] += 1
            continue
        cands.append((sym, value))
    counts["candidates"] = len(cands)
    if max_confirm and len(cands) > max_confirm:
        cands.sort(key=lambda x: -x[1])
        counts["unconfirmed"] = len(cands) - max_confirm
        cands = cands[:max_confirm]
    for sym, _ in cands:
        try:
            candles = bars_for(sym)
        except kite.KiteError as e:
            failed[sym] = str(e)
            continue
        counts["fetched"] += 1
        is_cas = sym in cas
        bars = session_bars(candles, day, cutoff, is_cas)
        if not usable(bars, day, cutoff, n_range, is_cas):
            counts["unusable"] += 1
            continue
        if quotes is None:
            value = bars[-1]["c"] * sum(b["v"] for b in bars)
            if bars[-1]["c"] < min_price or value < min_value:
                counts["illiquid"] += 1
                continue
        counts["scanned"] += 1
        counts["confirmed"] += 1
        h = orb_hit(bars, n_range)
        if h:
            hits.append(dict({"symbol": sym, "setup": "orb"}, **h))
    hits.sort(key=lambda x: (x["_t"], x["symbol"]))
    for h in hits:
        del h["_t"]
    counts["hits"] = len(hits)
    return hits, counts, failed


# ---------------------------------------------------------------- fetching

def progress(a, msg):
    if not getattr(a, "json", False) and not getattr(a, "quiet", False):
        print(msg, file=sys.stderr)


def connect():
    """Today's Kite session, or ScanError with kite's own login message."""
    try:
        return kite.Kite.connect(nse.now_ist())
    except kite.LoginNeeded as e:
        raise ScanError(str(e))


def cache_read(path):
    with open(path, encoding="utf-8") as fh:
        rows = json.load(fh)
    return [(kite.parse_time(r[0]), r[1], r[2], r[3], r[4], r[5]) for r in rows]


def cache_write(path, candles):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump([[iso(t), o, h, l, c, v] for t, o, h, l, c, v in candles], fh)


def fetch_daily(k, symbols, through, report=None, cache_dir=DAILY_CACHE):
    """{sym: candles} of daily bars through `through`, cached per symbol per session."""
    start, end = daily_range(through)
    data, failed, calls = {}, {}, 0
    for i, sym in enumerate(symbols, 1):
        path = os.path.join(cache_dir, "%s-%s.json" % (sym.replace("&", "_"), through.isoformat()))
        if os.path.isfile(path):
            data[sym] = cache_read(path)
        else:
            try:
                candles = k.historical(sym, "day", start, end)
            except kite.LoginNeeded:
                raise
            except kite.KiteError as e:
                failed[sym] = str(e)
                continue
            calls += 1
            data[sym] = candles
            cache_write(path, candles)
        if report and len(symbols) > 100 and i % 100 == 0:
            report("  daily bars: %s of %s (%s from the cache)" % (
                "{:,}".format(i), "{:,}".format(len(symbols)), "{:,}".format(i - calls - len(failed))))
    return data, failed


def read_symbols(a, default, hint):
    """--symbols, --watchlist, --universe, or the default file; deduped, --limit applied."""
    if a.symbols:
        syms = [kite.normalize_symbol(s) for s in re.split(r"[,\s]+", a.symbols) if s]
    elif a.watchlist:
        syms = symbols_from_file(a.watchlist)
    elif a.universe:
        syms = symbols_from_file(a.universe)
    else:
        if not os.path.exists(default):
            raise ScanError("no %s yet: %s" % (default, hint))
        syms = symbols_from_file(default)
    out = []
    for s in syms:
        if s not in out:
            out.append(s)
    if a.limit:
        out = out[:a.limit]
    if not out:
        raise ScanError("no symbols to scan")
    return out


def symbols_from_file(path):
    """A universe.txt (SYMBOL|Name a line), a text list, or a watchlist.json. An orb
    watchlist gives back the symbols it was run over, so a re-run sees every chart."""
    if not os.path.exists(path):
        raise ScanError("no file at %s" % path)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if text.lstrip().startswith(("{", "[")):
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        if isinstance(doc, dict):
            items = (doc.get("input") or {}).get("symbols") or doc.get("hits") or []
        else:
            items = doc
        return [kite.normalize_symbol(str(x.get("symbol") if isinstance(x, dict) else x)) for x in items if x]
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            out.append(kite.normalize_symbol(ln.split("|")[0]))
    return out


def read_config(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except ValueError as e:
        raise ScanError("%s is not valid JSON: %s" % (path, e))
    return cfg if isinstance(cfg, dict) else {}


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


def money(x):
    return "Rs {:,.2f}".format(x)


def crore(x):
    """50000000 -> 'Rs 5 crore', 25000000 -> 'Rs 2.5 crore'."""
    v = x / CRORE
    return "Rs %s crore" % ("{:,.0f}".format(v) if v == int(v) else "{:,.2f}".format(v).rstrip("0"))


def _n(x):
    x = float(x)
    return "{:,.0f}".format(x) if x == int(x) else "{:,.2f}".format(x)


def _p(x):
    return "{:,.2f}".format(x)


def decorate(hits, table, cas):
    """Add name, cas and tick_size from the instrument row to every hit."""
    for h in hits:
        row = table.get(h["symbol"]) or {}
        h["name"] = row.get("name") or h["symbol"]
        h["cas"] = h["symbol"] in cas
        h["tick_size"] = row.get("tick_size", 0.05)
    return hits


def ordered(h, keys):
    """A hit with the contract's keys first, the rest after."""
    out = {k: h[k] for k in keys if k in h}
    out.update({k: v for k, v in h.items() if k not in out})
    return out


# ---------------------------------------------------------------- commands

def cmd_universe(a):
    t0 = time.time()
    now = parse_now(a.now)
    k = connect()
    table = k.instruments("NSE", now.date())
    plain = [s for s in sorted(table) if classify(s, table[s]) == "keep"]
    quotes = quote_pass(k, plain, now.date(), progress=lambda m: progress(a, m))
    kept, counts = build_universe(table, quotes, a.min_price, a.min_value)
    if not a.no_write:
        d = os.path.dirname(a.out)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            for sym, name in kept:
                fh.write("%s|%s\n" % (sym, name))
    dropped = {k_: v for k_, v in counts.items() if k_ not in ("keep", "rows")}
    if a.json:
        print(json.dumps({"count": len(kept), "rows": counts["rows"], "quoted": len(plain), "dropped": dropped,
                          "filters": {"min_price": a.min_price, "min_value": a.min_value},
                          "out": None if a.no_write else a.out, "seconds": round(time.time() - t0, 1)}, indent=2))
    else:
        print("Kite NSE instrument list, %s: %s equity rows, %s in a plain series, %s quoted." % (
            now.date().isoformat(), "{:,}".format(counts["rows"]), "{:,}".format(len(plain)),
            "{:,}".format(len(quotes))))
        print("Kept %s NSE stocks at %s+ with %s+ traded." % ("{:,}".format(len(kept)), money(a.min_price),
                                                              crore(a.min_value)))
        print("Dropped: " + ", ".join("%s %s" % ("{:,}".format(v), k_)
                                      for k_, v in sorted(dropped.items(), key=lambda kv: -kv[1])) + ".")
        if not a.no_write:
            print("Wrote %s (one SYMBOL|Name a line)." % a.out)
    return 0 if kept else 1


def cmd_daily(a):
    t0 = time.time()
    now = parse_now(a.now)
    through = through_session(now)
    symbols = read_symbols(a, DEFAULT_UNIVERSE, "run `scan.py universe` first, or pass --symbols")
    k = connect()
    table = k.instruments("NSE", now.date())
    cas = k.cas_symbols(now.date())
    unknown = {s: "%s is not listed on NSE" % s for s in symbols if s not in table}
    wanted = [s for s in symbols if s in table]
    progress(a, "Daily bars for %s symbols through %s: one call each at 3 a second, about %d s uncached..."
             % ("{:,}".format(len(wanted)), through.isoformat(), len(wanted) * kite.MIN_GAP["historical"]))
    data, failed = fetch_daily(k, wanted, through, lambda m: progress(a, m))
    failed.update(unknown)
    hits, counts = scan_daily(data, symbols, through, a.min_price, a.min_value)
    decorate(hits, table, cas)
    keys = ("symbol", "name", "setup", "setups", "price", "cas", "tick_size", "detail")
    hits = [ordered(h, keys) for h in hits]
    doc = {"created_by": "trader-scanner", "created_at": iso(now), "mode": "daily", "as_of": iso(now),
           "closes_through": through.isoformat(), "universe": len(symbols), "scanned": counts["scanned"],
           "failed": len(failed), "source": "kite", "interval": "day",
           "filters": {"min_price": a.min_price, "min_value": a.min_value}, "note": NOTE, "hits": hits}
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, errors=failed, seconds=round(time.time() - t0, 1)), indent=2))
        return 0 if hits else 1
    print("Daily scan, %s IST, closes through %s (Kite daily candles, closes only)." % (
        now.strftime("%a %Y-%m-%d %H:%M"), day_label(through)))
    print("Fetched %s of %s charts%s. %s passed the liquidity filter (%s+ and %s+ traded)%s; %s scanned."
          % ("{:,}".format(counts["fetched"]), "{:,}".format(len(symbols)),
             (" (%s failed or had no data)" % "{:,}".format(len(failed))) if failed else "",
             "{:,}".format(counts["fetched"] - counts["illiquid"] - counts["short"]), money(a.min_price),
             crore(a.min_value),
             (", %d had under %d sessions" % (counts["short"], MIN_DAILY_BARS)) if counts["short"] else "",
             "{:,}".format(counts["scanned"])))
    if hits:
        per = [(k_, sum(1 for h in hits if k_ in h["setups"])) for k_, _ in SETUPS]
        print("%d chart%s matched a setup: %s." % (len(hits), "" if len(hits) == 1 else "s",
                                                  ", ".join("%d %s" % (n, LABEL[k_]) for k_, n in per if n)))
        print("Most traded first (price x volume):")
    else:
        print("No setups.")
    for h in hits[:a.show]:
        print("  %-12s %14s  RSI %5.1f  %s" % (h["symbol"], money(h["price"]), h["rsi"],
                                              ", ".join(LABEL[t] for t in h["setups"])))
    if len(hits) > a.show:
        print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    if not a.no_write:
        print("Wrote %s (mode daily, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " Next: pick one chart, then /trader-news SYMBOL.")
    return 0 if hits else 1


def cmd_orb(a):
    t0 = time.time()
    now = parse_now(a.now)
    at = parse_hhmm(a.at) if a.at else None
    cfg = read_config(a.config)
    until = parse_hhmm(a.until or cfg.get("no_entries_after") or DEFAULT_NO_ENTRIES_AFTER, "--until")
    day, cutoff = orb_window(now, at, a.or_minutes, until)
    symbols = read_symbols(a, DEFAULT_WATCHLIST,
                           "run `scan.py daily` first, or pass --symbols, --watchlist or --universe")
    k = connect()
    table = k.instruments("NSE", now.date())
    cas = k.cas_symbols(now.date())
    unknown = {s: "%s is not listed on NSE" % s for s in symbols if s not in table}
    wanted = [s for s in symbols if s in table]
    quotes = None
    if day == real_today():
        quotes = quote_pass(k, wanted, day, None, lambda m: progress(a, m)) if wanted else {}
        progress(a, "Stage 1: quotes for %s symbols, stage 2: 5-minute candles for at most %s candidates "
                 "at 3 a second..." % ("{:,}".format(len(wanted)), "{:,}".format(a.max_confirm or len(wanted))))
    else:
        progress(a, "Replaying %s: 5-minute candles for %s symbols, one call each at 3 a second..."
                 % (day.isoformat(), "{:,}".format(len(wanted))))
    done = []

    def bars_for(sym):
        done.append(sym)
        if len(done) % 50 == 0:
            progress(a, "  stage 2: %d charts so far" % len(done))
        return k.historical(sym, "5minute", slot(day, 0), nse.at(day, nse.CLOSE))
    hits, counts, failed = scan_orb(wanted, day, cutoff, a.or_minutes, bars_for, quotes, a.min_price,
                                    a.min_value, a.max_confirm, cas)
    failed.update(unknown)
    decorate(hits, table, cas)
    for h in hits:
        h["opening_range"] = {"high": h.pop("or_high"), "low": h.pop("or_low"), "minutes": a.or_minutes}
        h["detail"] = "closed above the %d-minute range at %s (+%.2f %%)" % (a.or_minutes, h["at"], h["pct_over"])
    keys = ("symbol", "name", "setup", "price", "cas", "tick_size", "opening_range", "detail")
    hits = [ordered(h, keys) for h in hits]
    doc = {"created_by": "trader-scanner", "created_at": iso(now), "mode": "orb", "as_of": iso(cutoff),
           "universe": len(symbols), "scanned": counts["scanned"], "failed": len(failed),
           "unconfirmed": counts["unconfirmed"], "source": "kite", "or_minutes": a.or_minutes,
           "interval": "%dminute" % BAR_MIN, "filters": {"min_price": a.min_price, "min_value": a.min_value},
           "input": {"symbols": symbols}, "note": NOTE, "hits": hits}
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, errors=failed, seconds=round(time.time() - t0, 1)), indent=2))
        return 0 if hits else 1
    print("Opening-range breakout scan, %s %s IST: %d-minute range (09:15 to %s), 5-minute bars, Kite."
          % (day_label(day), clock_label(cutoff), a.or_minutes, clock_label(slot(day, a.or_minutes // BAR_MIN))))
    nfailed = "{:,}".format(len(failed))
    if quotes is not None:
        print("Stage 1: %s of %s quoted%s; %s under the price / value floor, %s never traded above the open; "
              "%s candidates." % ("{:,}".format(len(wanted) - len(failed) + len(unknown)), "{:,}".format(len(symbols)),
                                  (" (%s failed or had no data)" % nfailed) if failed else "",
                                  "{:,}".format(counts["illiquid"]), "{:,}".format(counts["flat"]),
                                  "{:,}".format(counts["candidates"])))
    print("Stage 2: fetched %s charts%s; %s usable%s, checked with real highs and lows."
          % ("{:,}".format(counts["fetched"]),
             (" (%s failed or had no data)" % nfailed) if failed and quotes is None else "",
             "{:,}".format(counts["scanned"]),
             (" (%s with a missing bar)" % "{:,}".format(counts["unusable"])) if counts["unusable"] else ""))
    if counts["unconfirmed"]:
        print("PARTIAL: %s more candidates were not checked (only the %s most traded are; --max-confirm 0 "
              "checks all)." % ("{:,}".format(counts["unconfirmed"]), "{:,}".format(a.max_confirm)))
    if hits:
        print("%d of %s charts closed above their opening-range high by %s:"
              % (len(hits), "{:,}".format(counts["scanned"]), clock_label(cutoff)))
        for h in hits[:a.show]:
            print("  %s  %-12s %14s  range %s to %s  %+.2f%% over%s" % (
                h["at"], h["symbol"], money(h["price"]), _p(h["opening_range"]["low"]),
                _p(h["opening_range"]["high"]), h["pct_over"], "  CAS" if h["cas"] else ""))
        if len(hits) > a.show:
            print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    else:
        print("No chart closed above its opening-range high by %s." % clock_label(cutoff))
    if not a.no_write:
        print("Wrote %s (mode orb, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " Next: pick one chart, then /trader-news SYMBOL and /trader-entry SYMBOL.")
    return 0 if hits else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Chart Scanner (trader-scanner): NSE daily setups and "
                                             "opening-range breakouts through Zerodha Kite.")
    sub = ap.add_subparsers(dest="cmd")

    def floors(p, min_value):
        p.add_argument("--min-price", type=float, default=DEFAULT_MIN_PRICE,
                       help="last price floor in rupees (%s)" % _n(DEFAULT_MIN_PRICE))
        p.add_argument("--min-value", type=float, default=min_value,
                       help="rupees traded floor, price x volume (%s = %s)" % (_n(min_value), crore(min_value)))

    def common(p):
        p.add_argument("--now", help='pretend it is this IST time ("2026-10-29 10:15")')
        p.add_argument("--no-write", action="store_true")
        p.add_argument("--quiet", action="store_true")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("universe", help="Kite's NSE list, filtered by one quote pass -> trader/universe.txt")
    p.add_argument("--out", default=DEFAULT_UNIVERSE)
    floors(p, DEFAULT_MIN_VALUE)
    common(p)

    def lists(p):
        g = p.add_mutually_exclusive_group()
        g.add_argument("--symbols", help="comma-separated symbols instead of the default list")
        g.add_argument("--watchlist", metavar="FILE", help="a watchlist.json (hits) or a text file of symbols")
        g.add_argument("--universe", metavar="FILE", nargs="?", const=DEFAULT_UNIVERSE,
                       help="the universe file, one SYMBOL|Name a line (default %s)" % DEFAULT_UNIVERSE)
        p.add_argument("--limit", type=int, help="scan only the first N symbols")
        p.add_argument("--out", default=DEFAULT_WATCHLIST)
        p.add_argument("--show", type=int, default=50, help="hits to print (all are written)")

    p = sub.add_parser("daily", help="pre-market setups from daily closes (default: the universe)")
    lists(p)
    floors(p, DEFAULT_MIN_VALUE)
    common(p)
    p = sub.add_parser("orb", help="opening-range breakouts so far today (default: the daily watchlist)")
    lists(p)
    floors(p, 0.0)
    common(p)
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--max-confirm", type=int, default=MAX_CONFIRM,
                   help="charts to fetch in stage 2, most traded first (%d; 0 = all)" % MAX_CONFIRM)
    p.add_argument("--at", help="cut the clock at this IST time, HH:MM")
    p.add_argument("--until", help="no new entries after HH:MM (default: config no_entries_after, %s)"
                   % DEFAULT_NO_ENTRIES_AFTER)
    p.add_argument("--config", default=DEFAULT_CONFIG)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"universe": cmd_universe, "daily": cmd_daily, "orb": cmd_orb}[a.cmd](a)
    except (ScanError, kite.KiteError) as e:
        print("scan.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("scan.py: stopped", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
