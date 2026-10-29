#!/usr/bin/env python3
"""The NSE equity clock for the pack (standard library only).

Hours (IST, no daylight saving):
  09:00-09:08  pre-open order entry, 09:08-09:15 matching
  09:15        continuous trading opens
  15:15        continuous trading ends for stocks in the Closing Auction
               Session (CAS: stocks with F&O contracts), which then runs
               15:15-15:35 (NSE circular of 29 May 2026, live from 3 Aug 2026)
  15:30        continuous trading ends for every other stock

Zerodha squares off MIS positions in CAS stocks at 15:12 (Zerodha product
update, 10 Aug 2026; was 15:10). The pack's default flat-by is 15:05 for every
stock, safely before that, and Zerodha charges Rs 50 + GST per auto square-off.

Holidays: NSE's 2026 equity list. The thirteen closures from Jan to Oct were
checked against Kite's daily candles (every weekday with no candle). NSE
publishes each year's list in December, so a date past KNOWN_THROUGH is
reported as unknown instead of guessed.
"""

import datetime

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30), "IST")

PRE_OPEN = datetime.time(9, 0)
OPEN = datetime.time(9, 15)
CAS_CONTINUOUS_END = datetime.time(15, 15)
CAS_END = datetime.time(15, 35)   # the closing auction ends; every day's close is final
CLOSE = datetime.time(15, 30)
MIS_SQUARE_OFF_CAS = datetime.time(15, 12)
DEFAULT_FLAT_BY = datetime.time(15, 5)

NSE_HOLIDAYS = {
    "2026-01-15": "Municipal Corporation elections (Maharashtra)",
    "2026-01-26": "Republic Day",
    "2026-03-03": "Holi",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-10": "Diwali Balipratipada",
    "2026-11-24": "Guru Nanak Jayanti",
    "2026-12-25": "Christmas",
}
# a one-hour evening session on a Sunday; the pack does not day-trade it
MUHURAT = {"2026-11-08": "Diwali Laxmi Pujan (Muhurat trading)"}
KNOWN_THROUGH = datetime.date(2026, 12, 31)


def now_ist():
    return datetime.datetime.now(IST)


def to_ist(dt):
    """Aware -> IST; naive is taken to be IST already."""
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt.astimezone(IST)


def at(day, t):
    return datetime.datetime.combine(day, t, tzinfo=IST)


def trading_day(day):
    """(is_trading_day, reason). Reason is '' on a normal trading day."""
    key = day.isoformat()
    if day.weekday() >= 5:
        return False, "weekend" + (" (%s)" % MUHURAT[key] if key in MUHURAT else "")
    if key in NSE_HOLIDAYS:
        return False, "NSE holiday: " + NSE_HOLIDAYS[key]
    if day > KNOWN_THROUGH:
        return True, ("NSE's holiday list for %d is not in the pack yet: check "
                      "nseindia.com before trading" % day.year)
    return True, ""


def continuous_end(cas):
    """When continuous trading stops: 15:15 for CAS stocks, 15:30 otherwise."""
    return CAS_CONTINUOUS_END if cas else CLOSE


def phase(now, cas=True):
    """'closed', 'pre-open', 'open' or 'after-close' for this moment in IST."""
    now = to_ist(now)
    ok, _ = trading_day(now.date())
    if not ok:
        return "closed"
    t = now.time()
    if t < PRE_OPEN:
        return "closed"
    if t < OPEN:
        return "pre-open"
    if t < continuous_end(cas):
        return "open"
    return "after-close"


def next_open(now):
    """The next 09:15 IST open after `now` (today's, if it is still to come)."""
    now = to_ist(now)
    day = now.date()
    if trading_day(day)[0] and now.time() < OPEN:
        return at(day, OPEN)
    for _ in range(30):
        day += datetime.timedelta(days=1)
        if trading_day(day)[0]:
            return at(day, OPEN)
    raise ValueError("no NSE trading day in the next 30 days of the calendar")


def previous_trading_days(day, n):
    """The n trading days before `day`, oldest first."""
    out, d = [], day
    while len(out) < n:
        d -= datetime.timedelta(days=1)
        if trading_day(d)[0]:
            out.append(d)
    return out[::-1]


def parse_hhmm(text):
    """'15:05' or '3:05 PM' -> time. ValueError on anything else."""
    s = str(text).strip().upper().replace(".", ":")
    pm, am = s.endswith("PM"), s.endswith("AM")
    s = s[:-2].strip() if (pm or am) else s
    h, _, m = s.partition(":")
    h, m = int(h), int(m or 0)
    if pm and h < 12:
        h += 12
    if am and h == 12:
        h = 0
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError("not a time: %r" % text)
    return datetime.time(h, m)
