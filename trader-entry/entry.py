#!/usr/bin/env python3
"""Entry Timer for trader-entry: watches an NSE setup bar by bar and says the
moment to get in. Standard library only. Long side only. It never places an order.

    python3 entry.py check HINDUNILVR                            # opening-range breakout, 5-minute bars
    python3 entry.py check HINDUNILVR --now "2026-10-29 10:00"   # replay that moment (IST)
    python3 entry.py check HINDUNILVR --setup level --level 1829.50   # first close above a price
    python3 entry.py check HINDUNILVR --interval minute --min-volume-x 1.5 --write
    python3 entry.py watch HINDUNILVR --every 30 --until 14:30   # one line per closed bar
    python3 entry.py watchlist                                   # every hit in trader/watchlist.json

Setups (longs only):
  orb    opening-range breakout. The range is the high and low of the first
         --or-minutes (default 15: the 09:15, 09:20 and 09:25 five-minute bars,
         or fifteen 1-minute bars). It triggers on the first COMPLETED bar
         after the range that CLOSES above the range high. A wick above the
         high is not a trigger. A close exactly at the high is not above it.
  level  the first completed bar that closes above --level (for example a
         20-day high from trader-scanner).
  --min-volume-x X  the trigger bar also needs at least X times the average
         volume of the opening-range bars (the 09:15 bar carries the opening
         auction's spill, so 1.0 is already a high bar).

A trigger's time is the trigger bar's CLOSE time (the 09:55 bar closes at
10:00) and its price is that close. Bars are numbered from 1 at 09:15, so bar
N completes at 09:15 + N x interval: the 09:55 five-minute bar is bar 9 and
its time is 10:00 (trader-timemachine checks the two agree). Kite's candles
are stamped by their open, and a bar counts only once it has closed: now >=
its start + the interval. Kite also sends the bar in progress; it is shown as
"forming" and never counts. Right after a bar's close Kite's history can still
show it as the bar in progress, so a closed bar counts once the next bar has
appeared or a minute has passed (SETTLE); until then it is "pending".

Statuses (trader/entry.json): triggered; waiting (range still forming, or no
close above the level yet, with the distance in rupees and percent); expired
(no trigger by no_entries_after, default 14:30); pre-open (09:00 to 09:15);
closed (weekend, NSE holiday, before 09:00, or after the stock's continuous
close with no trigger: 15:15 for Closing Auction Session stocks, the ones
with F&O contracts, 15:30 for the rest).

trader/config.json (optional): "no_entries_before" (09:30) and
"no_entries_after" (14:30), IST.

watch wakes just after each bar closes (bars only change then), re-polls every
--every seconds (default 30) while Kite has not posted the new bar, prints one
line per closed bar and stops on a trigger (exit 0, writes trader/entry.json),
on expiry, at the close or at --until (exit 1). With --now it runs on a
simulated clock and replays the day from Kite's history without sleeping.

Data: Zerodha Kite Connect through trader-kite/kite.py (today's session from
/trader-kite; the instrument row gives the tick size, the F&O list says
whether the stock closes at 15:15). Everything is IST.

Exit codes: check 0 = triggered, 1 = anything else or an error. watch 0 on a
trigger, 1 otherwise. watchlist 0 when at least one symbol triggered.
"""

import argparse
import datetime
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trader-kite"))
import kite   # noqa: E402
import nse    # noqa: E402

INTERVALS = {"5minute": 300, "minute": 60}
DEFAULTS = {"no_entries_before": "09:30", "no_entries_after": "14:30"}
GRACE = 5         # seconds after a bar closes before watch asks Kite for it
SETTLE = 60       # seconds a just-closed bar waits for Kite's history to move past it
PAUSE = 0.5       # seconds between symbols in watchlist, on top of the client's own throttle
MAX_ERRORS = 5    # failed reads in a row before watch gives up
IST = nse.IST


class EntryError(Exception):
    pass


# ---------------------------------------------------------------- time

def parse_now(text):
    """--now -> aware IST datetime. "2026-10-29 10:15" (or ...T10:15, or just
    10:15 for today) is IST; an explicit offset is converted to IST."""
    if not text:
        return nse.now_ist().replace(microsecond=0)
    t = text.strip()
    if t.upper().endswith(" IST"):
        t = t[:-4].strip()
    if len(t) <= 5 and ":" in t:
        t = "%s %s" % (nse.now_ist().date().isoformat(), t.zfill(5))
    try:
        dt = datetime.datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        raise EntryError('--now wants an IST time like "2026-10-29 10:15"')
    return nse.to_ist(dt)


def parse_hhmm(text, what):
    try:
        return nse.parse_hhmm(text)
    except (ValueError, TypeError):
        raise EntryError("%s wants an IST time like 14:30, not %r" % (what, text))


def hm(t):
    """10:10, 09:45: the contract's wall-clock form."""
    return "%02d:%02d" % (t.hour, t.minute)


def at(day, t):
    return nse.at(day, t)


def rupees(x):
    return "Rs {:,.2f}".format(float(x))


def pct(x):
    return "%+.2f %%" % x


def r2(x):
    return None if x is None else round(float(x), 2) + 0.0   # + 0.0 turns -0.0 into 0.0


def iso(dt):
    return dt.astimezone(IST).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- Kite

def connect():
    try:
        return kite.Kite.connect()
    except kite.KiteError as e:
        raise EntryError(str(e))


def instrument_facts(k, sym):
    """tick size, name and whether the stock is in the Closing Auction Session."""
    try:
        row = k.instrument(sym)
        cas = sym in k.cas_symbols()
    except kite.KiteError as e:
        raise EntryError(str(e))
    tick = float(row.get("tick_size") or 0)
    if tick <= 0:
        raise EntryError("Kite's instrument row for %s has no tick size" % sym)
    return {"symbol": sym, "name": row.get("name") or sym, "tick_size": tick, "cas": cas}


def fetch_bars(k, sym, interval, day):
    """One session's candles from Kite as [{t, o, h, l, c, v}], t aware IST."""
    try:
        rows = k.historical(sym, interval, at(day, nse.OPEN), at(day, nse.CLOSE))
    except kite.KiteError as e:
        raise EntryError(str(e))
    return [{"t": t, "o": o, "h": h, "l": l, "c": c, "v": v} for t, o, h, l, c, v in rows]


# ---------------------------------------------------------------- the rule

def read_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except ValueError as e:
            raise EntryError("%s is not valid JSON: %s" % (path, e))
        for k in DEFAULTS:
            if doc.get(k):
                cfg[k] = doc[k]
    for k in DEFAULTS:
        parse_hhmm(cfg[k], k)
    return cfg


def check_opts(setup, or_minutes, interval, level):
    if interval not in INTERVALS:
        raise EntryError("--interval is 5minute or minute")
    step = INTERVALS[interval]
    if or_minutes <= 0 or (or_minutes * 60) % step:
        raise EntryError("--or-minutes must be a whole number of %s bars" % interval)
    if setup not in ("orb", "level"):
        raise EntryError("--setup is orb or level")
    if setup == "level" and level is None:
        raise EntryError("--setup level needs --level PRICE (for example a 20-day high)")
    return step


def slot_bars(bars, day, step, close_time, now):
    """Closed, on-grid bars of the session keyed by slot from 09:15, plus the one forming
    and the one pending (closed by the clock, but Kite may still be updating it)."""
    open_dt = at(day, nse.OPEN)
    close_dt = at(day, close_time)
    closed, forming, pending = {}, None, None
    for b in bars:
        t = nse.to_ist(b["t"])
        if t.date() != day or not (open_dt <= t < close_dt) or t.second:
            continue
        offset = int((t - open_dt).total_seconds())
        if offset % step:
            continue
        bar = dict(b, t=t, end=t + datetime.timedelta(seconds=step), slot=offset // step)
        if bar["end"] <= now:
            closed[bar["slot"]] = bar
        elif t <= now:
            forming = bar
    out = [closed[s] for s in sorted(closed)]
    if out and forming is None and now < out[-1]["end"] + datetime.timedelta(seconds=SETTLE):
        pending = out.pop()      # no later bar yet: Kite may still be filling this one in
    return out, forming, pending


def closed_reason(facts, lvl, setup):
    what = ("the range high %s" % rupees(lvl)) if setup == "orb" else ("the level %s" % rupees(lvl))
    return "continuous trading in %s ended at %s (%s) with no close above %s" % (
        facts["symbol"], hm(nse.continuous_end(facts["cas"])),
        "Closing Auction Session stock" if facts["cas"] else "no CAS", what)


def evaluate(bars, now, facts, setup="orb", or_minutes=15, interval="5minute", level=None,
             min_volume_x=None, cfg=None):
    """Pure function: the setup's status at `now` (aware IST) from one session's bars."""
    cfg = cfg or dict(DEFAULTS)
    step = check_opts(setup, or_minutes, interval, level)
    n_or = or_minutes * 60 // step
    now = nse.to_ist(now)
    day = now.date()
    close_time = nse.continuous_end(facts["cas"])
    res = {"symbol": facts["symbol"], "name": facts.get("name"), "setup": setup, "side": "buy",
           "date": day.isoformat(), "interval": interval, "or_minutes": or_minutes,
           "now": now.strftime("%Y-%m-%d %H:%M:%S"), "status": "waiting", "phase": "", "reason": "",
           "opening_range": None, "level": None, "trigger": None, "last": None, "forming": None,
           "min_volume_x": min_volume_x, "cas": facts["cas"], "tick_size": facts["tick_size"],
           "opens": hm(nse.OPEN), "closes": hm(close_time), "calendar_note": "", "_bars": []}
    ok, why = nse.trading_day(day)
    if not ok:
        res.update(status="closed", phase="closed", reason="NSE is closed today (%s)" % why,
                   next_open=nse.next_open(now).strftime("%Y-%m-%d %H:%M"))
        return res
    res["calendar_note"] = why
    before = at(day, parse_hhmm(cfg["no_entries_before"], "no_entries_before"))
    cutoff = at(day, parse_hhmm(cfg["no_entries_after"], "no_entries_after"))
    open_dt = at(day, nse.OPEN)
    or_end = open_dt + datetime.timedelta(minutes=or_minutes)
    res.update(no_entries_before=hm(before), no_entries_after=hm(cutoff))
    phase = nse.phase(now, facts["cas"])
    if phase == "closed":
        res.update(status="closed", phase="closed", reason="NSE opens at 09:15 (pre-open from 09:00)",
                   next_open=nse.next_open(now).strftime("%Y-%m-%d %H:%M"))
        return res
    if phase == "pre-open":
        res.update(status="pre-open", phase="pre-open", reason="pre-open session; continuous trading starts at 09:15%s" % (
            " and the %d-minute range is set at %s" % (or_minutes, hm(or_end)) if setup == "orb" else ""))
        return res

    closed, forming, pending = slot_bars(bars, day, step, close_time, now)
    res["_bars"] = closed
    res["bars_closed"] = len(closed)
    if forming:
        res["forming"] = {"bar_start": hm(forming["t"]), "closes": hm(forming["end"]), "price": r2(forming["c"])}
    if pending:
        res["forming"] = {"bar_start": hm(pending["t"]), "closes": hm(pending["end"]), "price": r2(pending["c"])}
        res["lag_note"] = ("the %s bar closed at %s but Kite has not posted the %s bar yet: it counts once "
                           "the next bar appears or a minute passes" % (hm(pending["t"]), hm(pending["end"]),
                                                                        hm(pending["end"])))

    or_bars = [b for b in closed if b["slot"] < n_or]
    avg_vol = (sum(b["v"] for b in or_bars) / float(len(or_bars))) if or_bars else None
    if or_bars:
        res["opening_range"] = {"high": r2(max(b["h"] for b in or_bars)), "low": r2(min(b["l"] for b in or_bars)),
                                "minutes": or_minutes, "bars": len(or_bars), "of": n_or,
                                "complete": now >= or_end, "avg_volume": int(round(avg_vol))}
    if setup == "orb":
        if now < or_end:
            res.update(phase="forming", reason="%d of %d range bars closed; the range is set at %s" % (
                len(or_bars), n_or, hm(or_end)))
            return res
        if not or_bars:
            if phase == "after-close":
                res.update(status="closed", phase="after-close", reason="Kite sent no bars for %s on %s" % (
                    facts["symbol"], day.isoformat()))
            else:
                res.update(phase="open", reason="Kite has sent no bars for the opening range yet")
            return res
        lvl = max(b["h"] for b in or_bars)
    else:
        lvl = float(level)
    res["level"] = r2(lvl)

    first_slot = n_or if setup == "orb" else 0
    trig, thin = None, []
    for b in closed:
        if b["slot"] < first_slot or b["end"] < before or b["end"] > cutoff:
            continue
        if b["c"] > lvl:
            vx = (b["v"] / avg_vol) if avg_vol else None
            if min_volume_x and (vx is None or vx < min_volume_x):
                thin.append(b)
                continue
            trig = b
            break
    last = closed[-1] if closed else None
    if last:
        res["last"] = {"bar_start": hm(last["t"]), "close": r2(last["c"]),
                       "vs_level_pct": r2(100.0 * (last["c"] / lvl - 1))}

    if trig:
        vx = (trig["v"] / avg_vol) if avg_vol else None
        res.update(status="triggered", phase=phase,
                   reason="the %s bar closed above %s" % (hm(trig["t"]), "the range high" if setup == "orb" else "the level"))
        res["trigger"] = {"time": hm(trig["end"]), "bar_start": hm(trig["t"]), "bar": trig["slot"] + 1,
                          "price": r2(trig["c"]), "volume": trig["v"], "volume_x": r2(vx),
                          "pct_over": r2(100.0 * (trig["c"] / lvl - 1))}
        res["minutes_since"] = int((now - trig["end"]).total_seconds() // 60)
        return res
    if phase == "after-close":
        res.update(status="closed", phase="after-close", reason=closed_reason(facts, lvl, setup))
        return res
    what = "close above the range high %s" % rupees(lvl) if setup == "orb" else "close above %s" % rupees(lvl)
    if now >= cutoff and not (pending and pending["end"] <= cutoff):
        res.update(status="expired", phase="after-cutoff",
                   reason="no %s by %s, the last time for a new entry (no_entries_after)" % (what, hm(cutoff)))
        return res
    res["phase"] = "open"
    if not last:
        res["reason"] = "Kite has sent no bars for %s yet" % day.isoformat()
    elif last["c"] > lvl and last in thin:
        res["reason"] = "the %s bar closed above at %s but on %.2fx the range's volume, under your %.2fx" % (
            hm(last["t"]), rupees(last["c"]), last["v"] / avg_vol if avg_vol else 0, min_volume_x)
    elif last["c"] > lvl and last["end"] < before:
        res["reason"] = "the %s bar closed above at %s, but no entries before %s (config)" % (
            hm(last["t"]), rupees(last["c"]), hm(before))
    else:
        gap = lvl - last["c"]
        res["reason"] = "last close %s (%s bar), %s %s %s (%s)" % (
            rupees(last["c"]), hm(last["t"]), rupees(abs(gap)), "under" if gap >= 0 else "over",
            "the range high %s" % rupees(lvl) if setup == "orb" else "the level %s" % rupees(lvl),
            pct(100.0 * (last["c"] / lvl - 1)))
    return res


def public(res):
    """The result without the internal bar list, ready for JSON."""
    return {k: v for k, v in res.items() if not k.startswith("_")}


def entry_doc(res, now):
    """trader/entry.json, the contract shape (plus a plain-words reason)."""
    t = res.get("trigger") or {}
    orr = res.get("opening_range")
    return {"created_by": "trader-entry", "symbol": res["symbol"], "setup": res["setup"], "side": "buy",
            "status": res["status"], "date": res["date"], "interval": res["interval"],
            "time": t.get("time"), "bar": t.get("bar"), "bar_start": t.get("bar_start"),
            "price": t.get("price"), "level": res.get("level"),
            "opening_range": {"high": orr["high"], "low": orr["low"], "minutes": orr["minutes"]} if orr else None,
            "cas": res["cas"], "tick_size": res["tick_size"],
            "source": "kite", "checked_at": iso(now), "reason": res.get("reason")}


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


# ---------------------------------------------------------------- words

LABEL = {"triggered": "TRIGGERED", "waiting": "WAITING", "expired": "EXPIRED", "pre-open": "PRE-OPEN",
         "closed": "CLOSED"}
PHASE_LABEL = {"forming": "RANGE FORMING", "closed": "MARKET CLOSED", "after-close": "CLOSED"}
SHORT = {"forming": "forming", "pre-open": "pre-open", "closed": "closed", "after-close": "closed",
         "after-cutoff": "expired"}


def setup_name(res):
    if res["setup"] == "orb":
        return "opening-range breakout (%d-minute range)" % res["or_minutes"]
    return "close above %s" % rupees(res["level"]) if res["level"] is not None else "close above a level"


def bars_name(interval):
    return "5-minute" if interval == "5minute" else "1-minute"


def describe(res):
    """Plain lines for a person."""
    when = datetime.datetime.strptime(res["now"], "%Y-%m-%d %H:%M:%S")
    out = ["%s  %s %s IST  %s on %s bars (Kite)" % (
        res["symbol"], when.strftime("%a %Y-%m-%d"), when.strftime("%H:%M"), setup_name(res), bars_name(res["interval"]))]
    orr = res.get("opening_range")
    if orr:
        start = datetime.datetime.strptime(res["opens"], "%H:%M")
        end = start + datetime.timedelta(minutes=orr["minutes"])
        out.append("Opening range %s-%s: high %s, low %s (%d of %d bars%s)." % (
            hm(start), hm(end), rupees(orr["high"]), rupees(orr["low"]), orr["bars"], orr["of"],
            "" if orr["complete"] else ", still forming"))
    label = PHASE_LABEL.get(res["phase"]) or LABEL.get(res["status"], res["status"].upper())
    t = res.get("trigger")
    if t:
        out.append("TRIGGERED %s at %s: the %s bar (bar %d) closed above %s %s (%s%s)." % (
            t["time"], rupees(t["price"]), t["bar_start"], t["bar"],
            "the range high" if res["setup"] == "orb" else "the level", rupees(res["level"]), pct(t["pct_over"]),
            (", %.2fx the range's volume" % t["volume_x"]) if res.get("min_volume_x") and t.get("volume_x") else ""))
        step = INTERVALS[res["interval"]] // 60
        if res.get("minutes_since", 0) > step and res.get("last"):
            out.append("That was %d minutes ago; the last close is %s (%s bar). A late entry is a different "
                       "trade: the price, the stop distance and the odds have moved." % (
                           res["minutes_since"], rupees(res["last"]["close"]), res["last"]["bar_start"]))
        if res["phase"] == "after-close":
            out.append("Continuous trading ended at %s; this entry is gone for today." % res["closes"])
        if res.get("minutes_since", 0) > step:
            out.append("Nothing was placed.")
        else:
            out.append("This is the moment to get in, if you take the trade. Nothing was placed.")
        out.append("Next: set the stop and target BEFORE you buy: /trader-exit (exit.py plan).")
    else:
        out.append("%s  %s." % (label, res["reason"]))
        f = res.get("forming")
        if res.get("lag_note") and res["status"] == "waiting":
            out.append(res["lag_note"][0].upper() + res["lag_note"][1:] + ".")
        elif f and res["status"] == "waiting" and res["phase"] in ("open", "forming"):
            out.append("The %s bar is still forming (%s so far, closes %s); it does not count until it closes." % (
                f["bar_start"], rupees(f["price"]), f["closes"]))
        if res["status"] == "expired":
            out.append("No entry on this setup today. Nothing was placed.")
        if res.get("next_open"):
            out.append("Next open: %s IST." % res["next_open"])
    if res.get("cas"):
        out.append("%s is a Closing Auction Session stock: continuous trading ends 15:15 and Zerodha squares off MIS "
                   "at 15:12. Tick %s." % (res["symbol"], rupees(res["tick_size"])))
    else:
        out.append("%s trades until 15:30. Tick %s." % (res["symbol"], rupees(res["tick_size"])))
    if res.get("calendar_note"):
        out.append(res["calendar_note"] + ".")
    return out


def bar_line(b, res):
    """One watch line for a bar that just closed."""
    start = hm(b["t"])
    orr = res.get("opening_range") or {}
    lvl = res.get("level")
    t = res.get("trigger")
    n_or = orr.get("of") or 0
    if res["setup"] == "orb" and b["slot"] < n_or:
        seen = [x for x in res["_bars"] if x["slot"] <= b["slot"]]
        note = "range so far %s to %s" % (rupees(min(x["l"] for x in seen)), rupees(max(x["h"] for x in seen)))
        if b["slot"] == n_or - 1:
            note = "opening range set: high %s, low %s" % (rupees(orr["high"]), rupees(orr["low"]))
    elif t and t["bar"] == b["slot"] + 1:
        note = "TRIGGERED: closed above %s (%s). Get in now if you take it: %s, %s" % (
            rupees(lvl), pct(t["pct_over"]), t["time"], rupees(t["price"]))
    elif lvl is None:
        note = ""
    elif b["c"] > lvl:
        note = "above %s" % rupees(lvl)
        if res["setup"] == "level" or b["slot"] >= n_or:
            avg = orr.get("avg_volume")
            if res.get("min_volume_x") and avg:
                note += ", but volume %.2fx is under %.2fx" % (b["v"] / float(avg), res["min_volume_x"])
            elif b["end"] < at(b["t"].date(), parse_hhmm(res["no_entries_before"], "no_entries_before")):
                note += ", but no entries before %s" % res["no_entries_before"]
    else:
        note = "%s under %s" % (rupees(lvl - b["c"]), rupees(lvl))
    return "%s bar  closed %s  %s" % (start, rupees(b["c"]), note)


# ---------------------------------------------------------------- watch

class SimClock(object):
    """A clock that moves only when it sleeps: replays a day without waiting."""

    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += datetime.timedelta(seconds=max(1, int(round(seconds))))


def watch_loop(provider, clock, sleep, emit, facts, opts, every=30, until=None, max_errors=MAX_ERRORS,
               on_trigger=None):
    """Poll bar by bar until a trigger (0), expiry, the close or --until (1).
    provider(now) -> bars, clock() -> aware IST datetime, sleep(seconds), emit(line).
    It sleeps in steps of at most `every` seconds and re-reads the clock each time,
    so a computer that dozed off catches up as soon as it wakes."""
    step = INTERVALS[opts.get("interval", "5minute")]
    close_time = nse.continuous_end(facts["cas"])
    printed, errors, said_preopen, first = -1, 0, False, True
    next_fetch = None
    while True:
        now = nse.to_ist(clock())
        if next_fetch is not None and now < next_fetch:
            sleep(max(1, min(every, (next_fetch - now).total_seconds())))
            continue
        day = now.date()
        ok, why = nse.trading_day(day)
        if not ok:
            emit("%s: NSE is closed today (%s). Nothing to watch." % (day.isoformat(), why))
            return 1, None
        open_dt = at(day, nse.OPEN)
        close_dt = at(day, close_time)
        if now < open_dt + datetime.timedelta(seconds=step):
            if not said_preopen:
                emit("%s IST  %s: watching from the first bar's close at %s." % (
                    now.strftime("%H:%M"), "not open yet" if now < open_dt else "open, no bar closed yet",
                    hm(open_dt + datetime.timedelta(seconds=step))))
                said_preopen = True
            next_fetch = open_dt + datetime.timedelta(seconds=step + GRACE)
            continue
        try:
            bars = provider(now)
            errors = 0
        except EntryError as e:
            errors += 1
            emit("%s IST  could not read bars: %s" % (now.strftime("%H:%M:%S"), e))
            if errors >= max_errors:
                emit("Stopping after %d failed reads in a row." % errors)
                return 1, None
            next_fetch = now + datetime.timedelta(seconds=every)
            continue
        res = evaluate(bars, now, facts, **opts)
        closed = res["_bars"]
        lines = describe(res)
        fresh = first
        if first:
            emit(lines[0])
            if len(lines) > 1 and lines[1].startswith("Opening range"):
                emit(lines[1])
            if closed and not res.get("trigger"):
                emit(bar_line(closed[-1], res))
            printed = closed[-1]["slot"] if closed else -1
            first = False
        else:
            for b in closed:
                if b["slot"] > printed:
                    emit(bar_line(b, res))
                    printed = b["slot"]
        if res["status"] == "triggered":
            for line in lines[1:]:
                if line.startswith("Opening range") or (line.startswith("TRIGGERED") and not fresh):
                    continue
                emit(line)
            if on_trigger:
                on_trigger(res, now)
            return 0, res
        if res["status"] in ("expired", "closed") or now >= close_dt:
            emit("%s  %s." % ("EXPIRED" if res["status"] == "expired" else "CLOSED",
                              res["reason"] or "continuous trading ended without a trigger"))
            return 1, res
        if until and now >= until:
            emit("Stopped at --until %s without a trigger. %s." % (hm(until), res["reason"]))
            return 1, res
        # sleep until just after the next bar closes; poll every `every` seconds if Kite is behind
        cur_slot = int((now - open_dt).total_seconds()) // step
        expected_last = cur_slot - 1
        behind = (not closed) or closed[-1]["slot"] < expected_last
        if behind:
            wait = every
        else:
            nxt = open_dt + datetime.timedelta(seconds=(cur_slot + 1) * step + GRACE)
            wait = (nxt - now).total_seconds()
        cutoff = at(day, parse_hhmm(res.get("no_entries_after") or DEFAULTS["no_entries_after"], "cutoff"))
        for stop_at in (until, cutoff, close_dt):
            if stop_at and stop_at > now:
                wait = min(wait, (stop_at - now).total_seconds())
        next_fetch = now + datetime.timedelta(seconds=max(1, wait))


# ---------------------------------------------------------------- commands

def _opts(a, cfg, need_level=True):
    if a.side in ("sell", "short"):
        raise EntryError("this pack plans longs only: no short entries. Watch for a buy (--side buy) instead.")
    if a.setup == "orb" and a.level is not None:
        raise EntryError("--level is for --setup level (an orb's level is its range high)")
    if a.min_volume_x is not None and a.min_volume_x <= 0:
        raise EntryError("--min-volume-x must be above 0")
    if a.level is not None and a.level <= 0:
        raise EntryError("--level must be a price above 0")
    check_opts(a.setup, a.or_minutes, a.interval, a.level if need_level else (a.level or 0))
    return {"setup": a.setup, "or_minutes": a.or_minutes, "interval": a.interval, "level": a.level,
            "min_volume_x": a.min_volume_x, "cfg": cfg}


def _symbol(text):
    sym = kite.normalize_symbol(text)
    if not sym:
        raise EntryError("give a Kite trading symbol, like RELIANCE or INFY")
    return sym


def session_needed(now):
    """Bars are worth asking for only on a trading day from 09:15."""
    return nse.trading_day(now.date())[0] and nse.to_ist(now).time() >= nse.OPEN


def load_session(k, sym, interval, now):
    facts = instrument_facts(k, sym)
    bars = fetch_bars(k, sym, interval, now.date()) if session_needed(now) else []
    return facts, bars


def cmd_check(a):
    cfg = read_config(a.config)
    opts = _opts(a, cfg)
    sym = _symbol(a.symbol)
    now = parse_now(a.now)
    k = connect()
    facts, bars = load_session(k, sym, a.interval, now)
    res = evaluate(bars, now, facts, **opts)
    if a.write:
        write_json(a.write, entry_doc(res, now))
    if a.json:
        print(json.dumps(public(res), indent=2))
    else:
        for line in describe(res):
            print(line)
        if a.write:
            print("Wrote %s (status %s)." % (a.write, res["status"]))
    return 0 if res["status"] == "triggered" else 1


def cmd_watch(a):
    cfg = read_config(a.config)
    opts = _opts(a, cfg)
    sym = _symbol(a.symbol)
    if a.every <= 0:
        raise EntryError("--every must be above 0 seconds")
    k = connect()
    facts = instrument_facts(k, sym)
    if a.now:
        clock = SimClock(parse_now(a.now))
        now_fn, sleep_fn = clock.now, clock.sleep
    else:
        now_fn, sleep_fn = nse.now_ist, time.sleep

    def provider(now):
        return fetch_bars(k, sym, a.interval, now.date())

    until = None
    if a.until:
        until = at(nse.to_ist(now_fn()).date(), parse_hhmm(a.until, "--until"))

    def on_trigger(res, now):
        write_json(a.out, entry_doc(res, now))
        print("Wrote %s." % a.out, flush=True)

    def emit(line):
        print(line, flush=True)

    code, _ = watch_loop(provider, now_fn, sleep_fn, emit, facts, opts, every=a.every, until=until,
                         on_trigger=on_trigger)
    return code


def read_watchlist(path):
    if not os.path.exists(path):
        raise EntryError("no watchlist at %s: run /trader-scanner first (it writes the hits there)" % path)
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except ValueError as e:
        raise EntryError("%s is not valid JSON: %s" % (path, e))
    hits = [h for h in (doc.get("hits") or []) if isinstance(h, dict) and h.get("symbol")]
    if not hits:
        raise EntryError("no hits in %s: the scanner found nothing to watch" % path)
    return hits


def watchlist_table(rows):
    out = ["%-12s  %-9s  %-6s  %-12s  %-12s  %-12s  %s" % ("SYMBOL", "STATUS", "TIME", "PRICE", "LEVEL", "LAST", "VS LEVEL")]
    for s, r, e in rows:
        if e:
            out.append("%-12s  error: %s" % (s, e))
            continue
        t = r.get("trigger") or {}
        last = r.get("last") or {}
        status = SHORT.get(r["phase"], r["status"]) if r["status"] != "triggered" else "triggered"
        out.append("%-12s  %-9s  %-6s  %-12s  %-12s  %-12s  %s" % (
            s, status[:9], t.get("time") or "-", rupees(t["price"]) if t else "-",
            rupees(r["level"]) if r.get("level") is not None else "-",
            rupees(last["close"]) if last else "-",
            pct(last["vs_level_pct"]) if last.get("vs_level_pct") is not None else "-"))
    return out


def cmd_watchlist(a):
    cfg = read_config(a.config)
    opts = _opts(a, cfg, need_level=False)
    hits = read_watchlist(a.watchlist)
    now = parse_now(a.now)
    k = connect()
    rows = []
    for i, h in enumerate(hits):
        o = dict(opts)
        if a.setup == "level" and o["level"] is None:
            o["level"] = h.get("level")
        try:
            sym = _symbol(str(h["symbol"]))
            if a.setup == "level" and o["level"] is None:
                raise EntryError("needs a level: --level PRICE or a \"level\" in the watchlist")
            if i:
                time.sleep(PAUSE)
            facts, bars = load_session(k, sym, a.interval, now)
            rows.append((sym, evaluate(bars, now, facts, **o), None))
        except EntryError as e:
            rows.append((str(h["symbol"]).upper(), None, str(e)))
    if a.json:
        print(json.dumps([{"symbol": s, "result": public(r) if r else None, "error": e} for s, r, e in rows], indent=2))
    else:
        for line in watchlist_table(rows):
            print(line)
        n = sum(1 for _, r, _ in rows if r and r["status"] == "triggered")
        print("%d of %d triggered. Pick one, then: entry.py check <SYMBOL> --write, and /trader-exit." % (n, len(rows)))
        print("Kite candles, IST. Nothing was placed.")
    return 0 if any(r and r["status"] == "triggered" for _, r, _ in rows) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Entry Timer (trader-entry): watches an NSE setup bar by bar.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--setup", choices=["orb", "level"], default="orb")
        p.add_argument("--or-minutes", type=int, default=15, help="opening range length (default 15)")
        p.add_argument("--interval", choices=["5minute", "minute"], default="5minute")
        p.add_argument("--level", type=float, help="the price to close above (--setup level)")
        p.add_argument("--min-volume-x", "--volume", dest="min_volume_x", type=float,
                       help="trigger bar volume vs the range bars' average")
        p.add_argument("--side", default="buy", choices=["buy", "long", "sell", "short"], help="buy only")
        p.add_argument("--now", help='pretend it is this IST time, e.g. "2026-10-29 10:15"')
        p.add_argument("--config", default="trader/config.json")

    p = sub.add_parser("check", help="the setup's status right now")
    p.add_argument("symbol")
    common(p)
    p.add_argument("--write", nargs="?", const="trader/entry.json", metavar="PATH",
                   help="save the result (default trader/entry.json)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("watch", help="poll bar by bar until it triggers or expires")
    p.add_argument("symbol")
    common(p)
    p.add_argument("--every", type=float, default=30, help="seconds between polls while Kite is behind")
    p.add_argument("--until", help="stop at this IST time (HH:MM)")
    p.add_argument("--out", default="trader/entry.json", help="where a trigger is written")
    p = sub.add_parser("watchlist", help="check every hit in trader/watchlist.json")
    common(p)
    p.add_argument("--watchlist", default="trader/watchlist.json")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"check": cmd_check, "watch": cmd_watch, "watchlist": cmd_watchlist}[a.cmd](a)
    except EntryError as e:
        print("entry.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("entry.py: stopped.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
