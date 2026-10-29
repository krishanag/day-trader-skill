#!/usr/bin/env python3
"""Time Machine for trader-timemachine, India edition. Standard library only.

Replays a planned NSE day trade before you risk a rupee: 10,000 simulated runs
of the rest of the session from your entry, plus the same rule on every past
session in the data. Bars come from Zerodha Kite Connect through the pack's
shared client (trader-kite/kite.py); times are IST, prices rupees.

    python3 timemachine.py run                          # trader/plan.json, 10,000 replays, seed 19
    python3 timemachine.py run --write                  # also save trader/timemachine.json
    python3 timemachine.py run --until 15:00            # stop each replay earlier than the plan's flat_by
    python3 timemachine.py run --seed 7 --replays 20000 --json
    python3 timemachine.py run --symbol RELIANCE --date 2026-10-29 --entry 1174.40 \\
        --stop 1163.00 --target 1185.80 --entry-time 09:40
    python3 timemachine.py chart                        # ASCII fan chart of the replays
    python3 timemachine.py chart --from trader/timemachine.json

How the replays work (the US original's method, same random draws, NSE clock):
  1. Fetch 5-minute candles for the 60 NSE trading days before the plan's date
     plus that day (one Kite historical call: Kite returns up to 100 days of
     5-minute bars at once). Keep only FULL sessions before the plan's date:
     72 bars, 09:15 to 15:10, for a stock in the Closing Auction Session (CAS,
     the F&O stocks, continuous trading ends 15:15), 75 bars to 15:25 for any
     other stock. The count comes from the plan's `cas` flag and is checked
     against the bars themselves. Short sessions, gaps and the plan's own day
     are left out of the pool.
  2. The pool: for every bar slot 1..71 (or 74) of the day, each past session's
     bar as ratios to the previous bar's close: (open, high, low, close) / prev close.
  3. One replay starts at the entry price and walks the rest of the session.
     For each bar i it picks a slot j = i + randint(-window, window), kept
     inside the session, then one past bar from pool[j] (random.choice), and
     scales it to the current price. The stop is checked before the target in
     every bar, so a bar that touches both counts as the stop (the conservative call).
  4. Outcomes: "target" or "stop" (whichever was touched first), or "neither"
     when the trade is still open at flat-by. Flat-by is the plan's flat_by
     (the pack default is 15:05, before Zerodha's 15:12 MIS square-off for CAS
     stocks); the last bar used is the one that starts before it, so for 15:05
     that is the 15:00 bar and its close is the exit. --until moves it.
     "green" = target first, or above the entry at flat-by (below for a sell).
     R = the target's R at the target, -1 at the stop, otherwise
     (price at flat-by - entry) / risk. A gap through the stop still counts
     -1R here; a real fill can be worse.
  5. The price at flat-by: median and the 5th to 95th percentile of all
     replays (pct(a, q) = sorted a at index int(q * n)). The fan chart uses a
     second run of 2,000 paths with the same seed. The histogram has 24 bins
     between the 1st and 99th percentile.
  6. History: the plan's own rule (an opening-range breakout with the same
     opening range, stop rule and target R, stop and target snapped to the
     tick) re-run on every one of those past full sessions, first 5-minute
     close beyond the range = entry, same stop-first rule, same flat-by.

The verdict line looks at the replays that hit one side (target or stop) and
compares the target's share of them, target / (target + stop), with the share
that breaks even at the plan's target R: 1 / (1 + R), so 50 % at 1R and 33 % at
2R. The gap, in percentage points:
  less than 5 points    "Close to a coin flip" (1R) / "Close to break-even" (other R)
  5 to 15 points        "Leans toward the target" / "Leans toward the stop"
  15 points or more     "Strongly toward the target" / "Strongly toward the stop"
Below it: a note when more than 40 % of the replays hit neither (the price at
flat-by decides those), and the average R when its sign goes against the lean.

5-minute bars only: the plan's entry bar, the opening range and flat-by are all
on the 5-minute grid, and 60 sessions of 5-minute bars is what the pool is
built for. The run refuses with a plain message when there are fewer than 20
full sessions, when the entry bar is not in the data, or when there is no Kite
session (run trader-kite/login.py). Same plan, same data and same --seed give
the same numbers on any machine. A simulation built from the stock's own
recent bars is not a prediction: results, news and circuit limits can do what
the last 60 days never did.

Plan bars: entry_bar counts from 1 (bar 1 opens 09:15), entry_time is that
bar's close, so bar 5 closes at 09:40 and bar 12 at 10:15. Prices snap to the
plan's tick_size (Kite's instrument row; 0.05 or 0.10 for most stocks, 0.01
under Rs 250): stops away from the entry, targets toward it, never past it.

--offline FILE reads a saved bar file instead of Kite: {"symbol", "interval",
"tick_size", "cas", "candles": [["2026-10-29T09:15:00+0530", o, h, l, c, v], ...]}
(.json or .json.gz). --now "2026-10-29 10:15" (IST) pins the clock.
"""

import argparse
import datetime
import gzip
import json
import math
import os
import random
import re
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trader-kite"))
import kite   # noqa: E402
import nse    # noqa: E402

INTERVAL = "5minute"
BAR_MINUTES = 5
OPEN_MIN = nse.OPEN.hour * 60 + nse.OPEN.minute          # 09:15
SESSION_BARS = {True: 72, False: 75}                       # CAS stocks end at 15:15, the rest at 15:30
LONGEST_SESSION = max(SESSION_BARS.values())
POOL_DAYS = 60
FETCH_FROM = datetime.time(9, 0)
FETCH_TO = datetime.time(15, 40)
MIN_SESSIONS = 20
DEFAULT_TICK = 0.05
SAMPLE_PATHS = 160
BAND_PATHS = 2000
BAND_Q = (0.05, 0.25, 0.5, 0.75, 0.95)
HIST_BINS = 24
COIN_FLIP = 0.05     # verdict thresholds, see the docstring
STRONG = 0.15
MANY_NEITHER = 0.40
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
STOP_WORDS = {"or_mid": "opening-range midpoint", "or_low": "opening-range low",
              "or_high": "opening-range high"}


class TMError(Exception):
    pass


# ---------------------------------------------------------------- time (IST, from nse.py)

def parse_now(text):
    """'2026-10-29 10:15' (IST) or ISO with an offset -> aware IST datetime. Empty: now."""
    if not text:
        return nse.now_ist().replace(microsecond=0)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise TMError("--now wants an IST time like \"2026-10-29 10:15\"")
    return nse.to_ist(dt)


def parse_clock(text, what):
    """'10:15', '15:05', '3:05 PM' (a bare 1 to 8 o'clock means afternoon) -> minutes after midnight."""
    raw = str(text or "").strip()
    try:
        t = nse.parse_hhmm(raw)
    except ValueError:
        raise TMError("%s wants an IST time like 10:15 or 15:05, not %r" % (what, text))
    h = t.hour
    if 1 <= h <= 8 and not raw.upper().endswith("M"):
        h += 12
    return h * 60 + t.minute


def clock24(minutes):
    return "%02d:%02d" % divmod(minutes, 60)


def bar_close(slot):
    """The clock time a bar closes: slot 0 is the 09:15 bar, closing 09:20."""
    return clock24(OPEN_MIN + BAR_MINUTES * (slot + 1))


def bar_start(slot):
    return clock24(OPEN_MIN + BAR_MINUTES * slot)


def session_close(nbars):
    return clock24(OPEN_MIN + BAR_MINUTES * nbars)


# ---------------------------------------------------------------- money and ticks

def inr(x):
    return "Rs {:,.2f}".format(x)


def tick_away(x, tick, long):
    """Snap a stop to the tick, away from the entry: down for a buy, up for a sell."""
    steps = round(x / tick, 6)
    n = math.floor(steps) if long else math.ceil(steps)
    return round(n * tick, 2)


def tick_toward(x, tick, long):
    """Snap a target to the tick, toward the entry: down for a buy, up for a sell. Never past it."""
    return tick_away(x, tick, long)


# ---------------------------------------------------------------- data

def fetch_window(plan_date, now):
    """(start, end) of the Kite call: 09:00 on the first of the 60 trading days before the
    plan's date, to 15:40 on the plan's date or now, whichever is earlier."""
    days = nse.previous_trading_days(plan_date, POOL_DAYS)
    start = nse.at(days[0], FETCH_FROM)
    end = min(nse.at(plan_date, FETCH_TO), now)
    if end <= start:
        raise TMError("nothing to fetch: --now %s is before the first pool day %s"
                      % (now.strftime("%Y-%m-%d %H:%M"), days[0].isoformat()))
    return start, end


def fetch_bars(k, plan, now):
    """One Kite historical call -> (candles, info). info carries the instrument's tick size."""
    row = k.instrument(plan["symbol"])
    start, end = fetch_window(datetime.date.fromisoformat(plan["date"]), now)
    candles = k.historical(plan["symbol"], INTERVAL, start, end)
    return candles, {"symbol": plan["symbol"], "interval": INTERVAL, "tick_size": row["tick_size"],
                     "cas": None, "from": kite.fmt(start), "to": kite.fmt(end)}


def read_offline(path, sym):
    """A saved bar file (.json or .json.gz), or a folder holding <SYM>-60d5m.json(.gz)."""
    if os.path.isdir(path):
        for name in ("%s-60d5m.json" % sym, "%s-60d5m.json.gz" % sym):
            if os.path.exists(os.path.join(path, name)):
                path = os.path.join(path, name)
                break
        else:
            raise TMError("no %s-60d5m.json(.gz) in %s" % (sym, path))
    if not os.path.exists(path):
        raise TMError("no offline bar file %s" % path)
    opener = gzip.open if path.endswith(".gz") else open
    try:
        with opener(path, "rt", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as e:
        raise TMError("could not read %s: %s" % (path, e))
    return parse_saved(text, path)


def parse_saved(text, where="the offline file"):
    """Saved bar JSON text -> (candles as kite tuples, info)."""
    try:
        doc = json.loads(text)
    except ValueError:
        raise TMError("%s is not JSON" % where)
    if not isinstance(doc, dict) or not isinstance(doc.get("candles"), list):
        raise TMError("%s is not a saved bar file (it needs a \"candles\" list)" % where)
    try:
        candles = kite.parse_candles({"candles": doc["candles"]})
    except (TypeError, ValueError, IndexError) as e:
        raise TMError("%s has a bad candle row: %s" % (where, e))
    info = {"symbol": kite.normalize_symbol(str(doc.get("symbol") or "")) or None,
            "interval": doc.get("interval"), "tick_size": doc.get("tick_size"), "cas": doc.get("cas"),
            "from": doc.get("from"), "to": doc.get("to")}
    return candles, info


def parse_bars(candles, now):
    """Kite candles [(aware dt, o, h, l, c, v)] -> (bars, dropped). Session 5-minute bars only,
    oldest first. Dropped: bars off the 5-minute grid, bars not complete yet (now < start + 5 min,
    so the forming candle Kite returns during the day is never used), and bars outside 09:15 to
    15:30 IST."""
    dropped = {"off_grid": 0, "not_complete": 0, "outside_hours": 0}
    bars = {}
    for t, o, h, l, c, v in candles:
        t = nse.to_ist(t)
        if t.minute % BAR_MINUTES or t.second:
            dropped["off_grid"] += 1
            continue
        mins = t.hour * 60 + t.minute - OPEN_MIN
        if mins < 0 or mins >= LONGEST_SESSION * BAR_MINUTES:
            dropped["outside_hours"] += 1
            continue
        if t + datetime.timedelta(minutes=BAR_MINUTES) > now:
            dropped["not_complete"] += 1
            continue
        bars[t] = {"date": t.date().isoformat(), "slot": mins // BAR_MINUTES, "start": t.strftime("%H:%M"),
                   "o": o, "h": h, "l": l, "c": c, "v": v}
    return [bars[t] for t in sorted(bars)], dropped


def split_sessions(bars):
    """{date: {slot: bar}}."""
    days = {}
    for b in bars:
        days.setdefault(b["date"], {})[b["slot"]] = b
    return days


def session_length(days, before, cas):
    """(bars per session, cas, note). The plan's cas flag says 72 or 75; the past sessions'
    own lengths decide when the flag is missing or plainly wrong, with a note saying so."""
    counts = {n: 0 for n in SESSION_BARS.values()}
    for d, s in days.items():
        if d < before and len(s) in counts:
            counts[len(s)] += 1
    seen_cas = counts[SESSION_BARS[True]] >= counts[SESSION_BARS[False]]
    if cas is None:
        return SESSION_BARS[seen_cas], seen_cas, None
    cas = bool(cas)
    if counts[SESSION_BARS[not cas]] > counts[SESSION_BARS[cas]]:
        note = ("the plan says cas %s but %d of the past sessions have %d bars and only %d have %d; "
                "going by the bars" % (str(cas).lower(), counts[SESSION_BARS[not cas]], SESSION_BARS[not cas],
                                       counts[SESSION_BARS[cas]], SESSION_BARS[cas]))
        return SESSION_BARS[not cas], not cas, note
    return SESSION_BARS[cas], cas, None


def full_sessions(days, before, nbars):
    """Full sessions (all nbars bars) strictly before the plan's date, oldest first, the short
    ones that were left out (gaps, half sessions), and the count of bars past the close."""
    full, short, after = [], [], 0
    for d in sorted(days):
        if d >= before:
            continue
        s = days[d]
        after += sum(1 for i in s if i >= nbars)
        if all(i in s for i in range(nbars)):
            full.append((d, [s[i] for i in range(nbars)]))
        else:
            short.append({"date": d, "bars": sum(1 for i in s if i < nbars)})
    return full, short, after


# ---------------------------------------------------------------- the simulation

def build_pool(sessions, nbars):
    """pool[i] = every past session's bar i as (o, h, l, c) / bar i-1's close, in date order."""
    pool = [[] for _ in range(nbars)]
    for _, D in sessions:
        for i in range(1, nbars):
            pc = D[i - 1]["c"]
            pool[i].append((D[i]["o"] / pc, D[i]["h"] / pc, D[i]["l"] / pc, D[i]["c"] / pc))
    return pool


def pct(a, q):
    return a[min(len(a) - 1, int(q * len(a)))]


def simulate(pool, entry, stop, target, k, last, replays=10000, seed=19, window=2, side="buy"):
    """Monte Carlo replays of bars k+1..last. Same draws and order as the original."""
    long = side == "buy"
    nbars = len(pool)
    risk = (entry - stop) if long else (stop - entry)
    r_target = round(((target - entry) if long else (entry - target)) / risk, 4)

    def slot(rng, i):
        return i if window == 0 else min(nbars - 1, max(1, i + rng.randint(-window, window)))

    rng = random.Random(seed)
    choice = rng.choice
    outcomes, closes, paths = [], [], []
    for n in range(replays):
        p = entry
        outcome = None
        keep = n < SAMPLE_PATHS
        path = [entry]
        for i in range(k + 1, last + 1):
            o, h, l, c = choice(pool[slot(rng, i)])
            hi, lo, cl = p * h, p * l, p * c
            if outcome is None:
                if long:
                    if lo <= stop:
                        outcome = "stop"        # both inside one bar: count the loss (conservative)
                    elif hi >= target:
                        outcome = "target"
                else:
                    if hi >= stop:
                        outcome = "stop"
                    elif lo <= target:
                        outcome = "target"
            p = cl
            if keep:
                path.append(round(p, 2))
        outcomes.append(outcome or "close")
        closes.append(p)
        if keep:
            paths.append(path)
    cnt = {x: outcomes.count(x) for x in ("target", "stop", "close")}
    green = sum(1 for o, p in zip(outcomes, closes)
                if o == "target" or (o == "close" and (p > entry if long else p < entry)))
    Rs = [r_target if o == "target" else -1.0 if o == "stop" else ((p - entry) if long else (entry - p)) / risk
          for o, p in zip(outcomes, closes)]
    srt = sorted(closes)
    # percentile bands per bar for the fan chart: a second run of 2,000 paths, same seed
    rng = random.Random(seed)
    choice = rng.choice
    allp = []
    for n in range(BAND_PATHS):
        p = entry
        path = [entry]
        for i in range(k + 1, last + 1):
            o, h, l, c = choice(pool[slot(rng, i)])
            p = p * c
            path.append(p)
        allp.append(path)
    bands = []
    for j in range(last - k + 1):
        col = sorted(pp[j] for pp in allp)
        bands.append([round(pct(col, q), 2) for q in BAND_Q])
    lo_, hi_ = pct(srt, 0.01), pct(srt, 0.99)
    W = (hi_ - lo_) / HIST_BINS
    bins = [0] * HIST_BINS
    for c in srt:
        b = int((c - lo_) / W) if W > 0 else 0
        if 0 <= b < HIST_BINS:
            bins[b] += 1
    return {
        "replays": replays, "seed": seed, "window": window, "bars_left": last - k,
        "p_target": round(cnt["target"] / replays, 4), "p_stop": round(cnt["stop"] / replays, 4),
        "p_neither": round(cnt["close"] / replays, 4), "p_green": round(green / replays, 4),
        "median_close": round(pct(srt, 0.5), 2), "p5_close": round(pct(srt, 0.05), 2),
        "p95_close": round(pct(srt, 0.95), 2), "avg_R": round(statistics.mean(Rs), 3),
        "r_at_target": r_target, "band_paths": BAND_PATHS, "bands": bands, "sample_paths": paths,
        "hist": {"lo": round(lo_, 2), "hi": round(hi_, 2), "bins": bins},
    }


# ---------------------------------------------------------------- stop rules and the history replay

def opening_range(D, n):
    """(high, low) of the first n bars of a session given as {slot: bar} or a list."""
    rows = [D[i] for i in range(n)]
    return max(b["h"] for b in rows), min(b["l"] for b in rows)


def rule_stop(rule, oh, ol, entry, long, tick):
    r = rule["stop_rule"]
    if r == "or_mid":
        return tick_away((oh + ol) / 2, tick, long)
    if r in ("or_low", "or_high"):
        return tick_away(ol if long else oh, tick, long)
    frac = rule["stop_pct"] / 100.0
    return tick_away(entry * (1 - frac) if long else entry * (1 + frac), tick, long)


def history_replay(sessions, rule, last, side="buy", tick=DEFAULT_TICK):
    """The plan's rule on every past full session: entry = the first bar after the opening
    range to close beyond it (up to the flat-by bar), then stop first, then target, bar by
    bar, else the close at flat-by."""
    long = side == "buy"
    n = rule["or_minutes"] // BAR_MINUTES
    trades = []
    for d, D in sessions:
        oh, ol = opening_range(D, n)
        kk = next((i for i in range(n, last + 1) if (D[i]["c"] > oh if long else D[i]["c"] < ol)), None)
        if kk is None:
            continue
        e = round(D[kk]["c"], 2)
        s = rule_stop(rule, oh, ol, e, long, tick)
        risk = (e - s) if long else (s - e)
        if risk <= 0:
            continue
        t = tick_toward(e + rule["target_r"] * risk if long else e - rule["target_r"] * risk, tick, long)
        oc = None
        for b in D[kk + 1:last + 1]:
            if (b["l"] <= s) if long else (b["h"] >= s):
                oc = "stop"
                break
            if (b["h"] >= t) if long else (b["l"] <= t):
                oc = "target"
                break
        cl = D[last]["c"]
        R = ((t - e) if long else (e - t)) / risk if oc == "target" else -1.0 if oc == "stop" else \
            ((cl - e) if long else (e - cl)) / risk
        trades.append({"date": d, "time": bar_close(kk), "entry": e, "stop": s, "target": t,
                       "outcome": oc or "close", "R": round(R, 2)})
    return {
        "sessions": len(sessions), "triggered": len(trades),
        "target": sum(1 for x in trades if x["outcome"] == "target"),
        "stop": sum(1 for x in trades if x["outcome"] == "stop"),
        "close": sum(1 for x in trades if x["outcome"] == "close"),
        "green": sum(1 for x in trades if x["R"] > 0),
        "avg_R": round(statistics.mean(x["R"] for x in trades), 3) if trades else None,
        "rule": rule, "trades": trades,
    }


# ---------------------------------------------------------------- the plan

def _num(v, what):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise TMError("the plan needs a number for %s, got %r" % (what, v))
    if not x > 0 or math.isinf(x):
        raise TMError("the plan's %s must be a positive price, got %r" % (what, v))
    return x


def load_plan(a, now):
    """The plan from --plan (trader/plan.json) or from the flags, checked and normalized."""
    if a.symbol:
        missing = [f for f in ("entry", "stop", "target", "entry_time") if getattr(a, f) is None]
        if missing:
            raise TMError("with --symbol also give %s" % ", ".join("--" + m.replace("_", "-") for m in missing))
        raw = {"symbol": a.symbol, "side": a.side, "date": a.date or now.date().isoformat(),
               "entry": a.entry, "stop": a.stop, "target": a.target, "entry_time": a.entry_time,
               "setup": a.setup, "opening_range": {"minutes": a.or_minutes}, "stop_rule": a.stop_rule,
               "interval": INTERVAL, "flat_by": a.flat_by, "tick_size": a.tick}
        src = "flags"
    else:
        if not os.path.exists(a.plan):
            raise TMError("no plan at %s: run /trader-exit first, or pass --symbol --date --entry --stop "
                          "--target --entry-time" % a.plan)
        try:
            with open(a.plan, encoding="utf-8") as fh:
                raw = json.load(fh)
        except ValueError as e:
            raise TMError("%s is not valid JSON: %s" % (a.plan, e))
        if not isinstance(raw, dict):
            raise TMError("%s is not a plan" % a.plan)
        src = a.plan
    return normalize_plan(raw, src)


def _entry_slot(raw):
    """The entry bar as a 0-based slot from entry_time (the bar's close) and/or entry_bar (from 1)."""
    k = None
    if raw.get("entry_time"):
        close_min = parse_clock(raw["entry_time"], "entry_time")
        if (close_min - OPEN_MIN) % BAR_MINUTES:
            raise TMError("entry_time %s is not on the 5-minute grid (use the bar's close, e.g. 10:15)"
                          % raw["entry_time"])
        k = (close_min - OPEN_MIN) // BAR_MINUTES - 1
    if raw.get("entry_bar") is not None:
        try:
            kb = int(raw["entry_bar"]) - 1
        except (TypeError, ValueError):
            raise TMError("entry_bar must be a bar number counted from 1, got %r" % raw["entry_bar"])
        if k is not None and kb != k:
            raise TMError("the plan disagrees with itself: entry_bar %d closes at %s but entry_time is %s"
                          % (kb + 1, bar_close(kb), raw["entry_time"]))
        k = kb
    if k is None:
        raise TMError("the plan needs entry_time (the entry bar's close, e.g. 10:15) or entry_bar")
    if not 0 <= k <= LONGEST_SESSION - 2:
        raise TMError("the entry must close between %s and %s to leave bars to replay (got %s)"
                      % (bar_close(0), bar_close(LONGEST_SESSION - 2), bar_close(k)))
    return k


def normalize_plan(raw, src="flags"):
    """A plan dict (trader/plan.json shape) -> the checked plan the time machine runs."""
    sym = kite.normalize_symbol(str(raw.get("symbol") or ""))
    if not re.match(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$", sym):
        raise TMError("the plan needs an NSE trading symbol like RELIANCE, got %r" % raw.get("symbol"))
    side = str(raw.get("side") or "buy").lower()
    if side not in ("buy", "sell"):
        raise TMError("side must be buy or sell, got %r" % raw.get("side"))
    interval = str(raw.get("interval") or INTERVAL)
    if interval not in (INTERVAL, "5m"):
        raise TMError("5-minute bars only (the plan says %s): the entry bar, the opening range and flat-by "
                      "are all on the 5-minute grid" % interval)
    try:
        day = datetime.date.fromisoformat(str(raw.get("date") or ""))
    except ValueError:
        raise TMError("the plan needs a date like 2026-10-29, got %r" % raw.get("date"))
    open_day, why = nse.trading_day(day)
    if not open_day:
        raise TMError("%s is not an NSE trading day (%s)" % (day.isoformat(), why))
    entry, stop, target = (_num(raw.get(k), k) for k in ("entry", "stop", "target"))
    long = side == "buy"
    if long and not stop < entry < target:
        raise TMError("a buy needs stop < entry < target (got stop %s, entry %s, target %s)" % (stop, entry, target))
    if not long and not target < entry < stop:
        raise TMError("a sell needs target < entry < stop (got target %s, entry %s, stop %s)" % (target, entry, stop))
    k = _entry_slot(raw)
    orng = raw.get("opening_range") or {}
    or_min = orng.get("minutes", 15) if isinstance(orng, dict) else 15
    try:
        or_min = int(or_min)
    except (TypeError, ValueError):
        raise TMError("opening_range.minutes must be a number")
    if or_min < 5 or or_min % 5 or or_min >= LONGEST_SESSION * BAR_MINUTES:
        raise TMError("opening_range.minutes must be a multiple of 5 (got %s)" % or_min)
    flat_by = clock24(parse_clock(raw["flat_by"], "flat_by")) if raw.get("flat_by") else None
    tick = raw.get("tick_size")
    if tick is not None:
        tick = _num(tick, "tick_size")
    cas = raw.get("cas")
    if cas is not None and not isinstance(cas, bool):
        raise TMError("cas must be true or false, got %r" % cas)
    risk = (entry - stop) if long else (stop - entry)
    return {
        "symbol": sym, "side": side, "date": day.isoformat(), "setup": str(raw.get("setup") or "orb"),
        "entry": entry, "entry_time": bar_close(k), "entry_bar": k + 1,
        "stop": stop, "target": target, "risk_per_share": round(risk, 2),
        "target_r": round(((target - entry) if long else (entry - target)) / risk, 2),
        "opening_range": dict(orng, minutes=or_min) if isinstance(orng, dict) else {"minutes": or_min},
        "stop_rule": str(raw.get("stop_rule") or "auto"), "interval": INTERVAL,
        "flat_by": flat_by, "cas": cas, "tick_size": tick, "calendar_note": why or None, "from": src,
    }


def resolve_rule(plan, entry_day, tick):
    """The rule the history replay uses: orb, the plan's opening range, its stop rule
    (or_mid, or_low / or_high, or 'pct' = the plan's stop distance as a percent of
    the entry), and the plan's target R. 'auto' works the stop rule out from the
    plan day's own opening range. None when the setup is not orb."""
    if plan["setup"].lower() != "orb":
        return None
    long = plan["side"] == "buy"
    n = plan["opening_range"]["minutes"] // BAR_MINUTES
    rule = {"setup": "orb", "or_minutes": plan["opening_range"]["minutes"], "stop_rule": plan["stop_rule"],
            "target_r": plan["target_r"],
            "stop_pct": round(100.0 * plan["risk_per_share"] / plan["entry"], 4)}
    far = "or_low" if long else "or_high"
    if rule["stop_rule"] == "auto":
        rule["stop_rule"] = "pct"
        if all(i in entry_day for i in range(n)):
            oh, ol = opening_range(entry_day, n)
            if abs(tick_away((oh + ol) / 2, tick, long) - plan["stop"]) < tick / 2:
                rule["stop_rule"] = "or_mid"
            elif abs(tick_away(ol if long else oh, tick, long) - plan["stop"]) < tick / 2:
                rule["stop_rule"] = far
    elif rule["stop_rule"] in ("or_low", "or_high") and rule["stop_rule"] != far:
        rule["stop_rule"] = "pct"
    elif rule["stop_rule"] not in ("or_mid", far):
        rule["stop_rule"] = "pct"
    if rule["stop_rule"] != "pct":
        del rule["stop_pct"]
    return rule


# ---------------------------------------------------------------- the run

def _until(mc):
    return mc.get("until") or clock24(nse.DEFAULT_FLAT_BY.hour * 60 + nse.DEFAULT_FLAT_BY.minute)


def lean(mc):
    """(gap, share, break_even): the target's share of the replays that hit one side,
    minus the share that breaks even at the target's R. None when nothing hit a side."""
    pt, ps = mc["p_target"], mc["p_stop"]
    if pt + ps <= 0:
        return None
    r = mc.get("r_at_target") or 1.0
    share, even = pt / (pt + ps), 1.0 / (1.0 + r)
    return share - even, share, even


def verdict(mc):
    """One line from the numbers, see the docstring for the thresholds."""
    pt, ps = mc["p_target"], mc["p_stop"]
    lv = lean(mc)
    if lv is None:
        return "No replay reached the target or the stop by %s: the price at %s decides every one." % (
            _until(mc), _until(mc))
    d, share, even = lv
    r = mc.get("r_at_target") or 1.0
    one_r = abs(r - 1.0) < 0.005
    if abs(d) < COIN_FLIP:
        head = "Close to a coin flip" if one_r else "Close to break-even"
    elif abs(d) < STRONG:
        head = "Leans toward the target" if d > 0 else "Leans toward the stop"
    else:
        head = "Strongly toward the target" if d > 0 else "Strongly toward the stop"
    odds = "target first %.1f %% vs stop first %.1f %%" % (100 * pt, 100 * ps)
    if one_r:
        return "%s: %s." % (head, odds)
    return ("%s: at %gR the target has to come first in %.0f %% of the replays that hit one side; "
            "it did in %.0f %% (%s)." % (head, round(r, 2), 100 * even, 100 * share, odds))


def verdict_notes(mc):
    """The lines printed right below the verdict, so nothing reads as one-sided."""
    notes = []
    pn = mc.get("p_neither", 0)
    if pn > MANY_NEITHER and lean(mc) is not None:
        notes.append("%.1f %% of the replays hit neither by %s; the price at %s decides those."
                     % (100 * pn, _until(mc), _until(mc)))
    lv = lean(mc)
    avg = mc.get("avg_R")
    if lv and abs(lv[0]) >= COIN_FLIP and avg is not None and avg != 0 and (avg > 0) != (lv[0] > 0):
        notes.append("Average R is %+.3f all the same: the replays that hit neither pull it the other way." % avg)
    return notes


def _check_data(info, plan):
    sym = plan["symbol"]
    if info.get("symbol") and info["symbol"] != sym:
        raise TMError("the data is for %s but the plan is for %s" % (info["symbol"], sym))
    if info.get("interval") and info["interval"] not in (INTERVAL, "5m"):
        raise TMError("the data has %s bars; the time machine needs 5-minute bars" % info["interval"])


def _until_slot(plan, nbars, until):
    """The last slot a replay walks: the bar that starts before flat-by (or --until)."""
    end_min = OPEN_MIN + BAR_MINUTES * nbars
    if until is not None:
        until_min = parse_clock(until, "--until")
    elif plan.get("flat_by"):
        until_min = parse_clock(plan["flat_by"], "flat_by")
    else:
        until_min = nse.DEFAULT_FLAT_BY.hour * 60 + nse.DEFAULT_FLAT_BY.minute
    if (until_min - OPEN_MIN) % BAR_MINUTES or until_min > end_min:
        raise TMError("%s must be on the 5-minute grid and no later than %s (the close for %s)"
                      % ("--until" if until is not None else "flat_by", clock24(end_min),
                         "a CAS stock" if nbars == SESSION_BARS[True] else "a non-CAS stock"))
    last = (until_min - OPEN_MIN) // BAR_MINUTES - 1
    if last <= plan["entry_bar"] - 1:
        raise TMError("%s %s leaves nothing to replay after the %s entry"
                      % ("--until" if until is not None else "flat_by", clock24(until_min), plan["entry_time"]))
    return last, until_min


def time_machine(candles, plan, now, replays=10000, seed=19, window=2, until=None, source="kite", info=None):
    """Kite candles + a normalized plan -> the timemachine.json document."""
    if not 1 <= replays <= 1000000:
        raise TMError("--replays must be between 1 and 1,000,000")
    if not 0 <= window <= 10:
        raise TMError("--window must be between 0 and 10 bars")
    info = info or {}
    _check_data(info, plan)
    sym = plan["symbol"]
    tick = plan["tick_size"] or info.get("tick_size") or DEFAULT_TICK
    bars, dropped = parse_bars(candles, now)
    if not bars:
        raise TMError("no complete 5-minute bars for %s in the data" % sym)
    days = split_sessions(bars)
    cas_flag = plan["cas"] if plan["cas"] is not None else info.get("cas")
    nbars, cas, cas_note = session_length(days, plan["date"], cas_flag)
    last, until_min = _until_slot(plan, nbars, until)
    k = plan["entry_bar"] - 1
    entry_day = {i: b for i, b in days.get(plan["date"], {}).items() if i < nbars}
    if k not in entry_day:
        reason = ("no bars for that day" if not entry_day else
                  "the data stops at the %s bar" % entry_day[max(entry_day)]["start"])
        raise TMError("the entry is not in the data: no complete %s bar (closing %s) on %s for %s (%s)"
                      % (bar_start(k), plan["entry_time"], plan["date"], sym, reason))
    eb = entry_day[k]
    warn = []
    if cas_note:
        warn.append(cas_note)
    if plan.get("calendar_note"):
        warn.append(plan["calendar_note"])
    if not eb["l"] - tick <= plan["entry"] <= eb["h"] + tick:
        warn.append("the plan's entry %.2f is outside that bar's range %.2f to %.2f"
                    % (plan["entry"], eb["l"], eb["h"]))
    sessions, short, after_close = full_sessions(days, plan["date"], nbars)
    dropped = dict(dropped, after_close=after_close)
    if len(sessions) < MIN_SESSIONS:
        raise TMError("only %d full session(s) of %s before %s in the data; the time machine needs at least %d"
                      % (len(sessions), sym, plan["date"], MIN_SESSIONS))
    pool = build_pool(sessions, nbars)
    mc = simulate(pool, plan["entry"], plan["stop"], plan["target"], k, last, replays, seed, window, plan["side"])
    mc = dict(mc, until=clock24(until_min), pool_sessions=len(sessions), pool_from=sessions[0][0],
              pool_to=sessions[-1][0], band_times=[bar_close(j) for j in range(k, last + 1)])
    rule = resolve_rule(plan, entry_day, tick)
    plan_out = {kk: v for kk, v in plan.items() if v is not None and kk != "calendar_note"}
    plan_out.update(cas=cas, tick_size=tick, flat_by=plan.get("flat_by") or clock24(until_min))
    if rule:
        plan_out["stop_rule"] = rule["stop_rule"]
        history = history_replay(sessions, rule, last, plan["side"], tick)
    else:
        history = {"sessions": len(sessions), "triggered": 0, "rule": None, "trades": [],
                   "note": "skipped: the history replay knows the opening-range breakout (orb) only, "
                           "this plan's setup is %r" % plan["setup"]}
    last_bar = bars[-1]
    return {
        "created_by": "trader-timemachine",
        "created_at": now.replace(microsecond=0).isoformat(),
        "source": source,
        "data": {"symbol": sym, "exchange": "NSE", "interval": INTERVAL, "cas": cas, "session_bars": nbars,
                 "session_close": session_close(nbars), "pool_days_asked": POOL_DAYS,
                 "from": info.get("from"), "to": info.get("to"),
                 "last_bar": "%s %s IST" % (last_bar["date"], last_bar["start"]),
                 "dropped": dropped, "short_sessions_left_out": short},
        "plan": plan_out,
        "mc": mc,
        "history": history,
        "verdict": verdict(mc),
        "verdict_notes": verdict_notes(mc),
        "warnings": warn,
        "note": ("A simulation built from %s's own last %d full NSE sessions. It is not a prediction: results, "
                 "news and circuit limits can do things those sessions never did. Same seed, same numbers."
                 % (sym, len(sessions))),
    }


# ---------------------------------------------------------------- output

def pc(x):
    return "%.1f %%" % (100.0 * x)


def report(doc):
    p, m, h = doc["plan"], doc["mc"], doc["history"]
    long = p["side"] == "buy"
    until = m["until"]
    wd = WEEKDAYS[datetime.date.fromisoformat(p["date"]).weekday()]
    r = p["target_r"]
    lines = [
        "%s %s, %s %s: entry %s at %s (bar %d), stop %s, target %s (%sR, risk %s a share)."
        % (p["symbol"], p["side"], wd, p["date"], inr(p["entry"]), p["entry_time"], p["entry_bar"], inr(p["stop"]),
           inr(p["target"]), ("%g" % r), inr(p["risk_per_share"])),
        "%s replays of the rest of the session, %s to %s IST (%d five-minute bars), seed %d."
        % ("{:,}".format(m["replays"]), p["entry_time"], until, m["bars_left"], m["seed"]),
        "Each bar is drawn whole from %s's own 5-minute bars at the same time of day (+-%d bars) over its last "
        "%d full NSE sessions (%s to %s)." % (p["symbol"], m["window"], m["pool_sessions"], m["pool_from"],
                                             m["pool_to"]),
        "",
        "  target first   %s" % pc(m["p_target"]),
        "  stop first     %s" % pc(m["p_stop"]),
        "  neither        %-8s still open at %s" % (pc(m["p_neither"]), until),
        "  green          %-8s target first, or %s the entry at %s" % (pc(m["p_green"]),
                                                                       "above" if long else "below", until),
        "",
        "  Price at %s: median %s, 90 %% of replays between %s and %s."
        % (until, inr(m["median_close"]), inr(m["p5_close"]), inr(m["p95_close"])),
        "  Average R: %+.3f (R = the %s risk: %sR at the target, -1R at the stop)."
        % (m["avg_R"], inr(p["risk_per_share"]), ("+%g" % m["r_at_target"]), ),
        "",
    ]
    if h.get("rule"):
        ru = h["rule"]
        stop_words = STOP_WORDS.get(ru["stop_rule"], "%.2f %% from the entry" % ru.get("stop_pct", 0))
        if h["triggered"]:
            lines.append("History: the same rule (%d-minute opening range, stop at the %s, %gR target, flat by %s) "
                         "on each of those %d real sessions fired %d times: %d target, %d stop, %d neither. "
                         "Average R %+.3f."
                         % (ru["or_minutes"], stop_words, ru["target_r"], until, h["sessions"], h["triggered"],
                            h["target"], h["stop"], h["close"], h["avg_R"]))
        else:
            lines.append("History: the same rule never fired in those %d sessions." % h["sessions"])
    else:
        lines.append("History: " + h.get("note", "skipped."))
    lines += ["", doc["verdict"]] + list(doc.get("verdict_notes") or []) + [""]
    for w in doc.get("warnings") or []:
        lines.append("Note: " + w + ".")
    if doc["data"]["short_sessions_left_out"]:
        lines.append("Left out %d short session(s) (gaps or early ends): %s."
                     % (len(doc["data"]["short_sessions_left_out"]),
                        ", ".join(x["date"] for x in doc["data"]["short_sessions_left_out"])))
    lines.append(doc["note"] + " Repeat this run with --seed %d." % m["seed"])
    return lines


def render_chart(doc, height=17):
    """ASCII fan chart of the bands: one column per 5-minute bar."""
    p, m = doc["plan"], doc["mc"]
    bands, times = m["bands"], m["band_times"]
    lo = min([b[0] for b in bands] + [p["stop"], p["target"]])
    hi = max([b[4] for b in bands] + [p["stop"], p["target"]])
    pad = (hi - lo) * 0.04 or 0.01
    lo, hi = lo - pad, hi + pad
    step = (hi - lo) / (height - 1)

    def row(price):
        return max(0, min(height - 1, int(round((hi - price) / step))))
    grid = [[" "] * len(bands) for _ in range(height)]
    for j, b in enumerate(bands):
        r5, r25, r50, r75, r95 = [row(x) for x in b]
        for r in range(r95, r5 + 1):
            grid[r][j] = "."
        for r in range(r75, r25 + 1):
            grid[r][j] = ":"
        grid[r50][j] = "*"
    marks = {row(p["target"]): "target", row(p["stop"]): "stop"}
    if row(p["entry"]) not in marks:
        marks[row(p["entry"])] = "entry"
    for r in (row(p["target"]), row(p["stop"])):
        grid[r] = ["-" if ch == " " else ch for ch in grid[r]]
    out = ["%s %s, %s replay paths from the %s entry (seed %d), prices in rupees" % (
        p["symbol"], p["date"], "{:,}".format(m.get("band_paths", BAND_PATHS)), p["entry_time"], m["seed"])]
    for r in range(height):
        label = marks.get(r)
        price = {"target": p["target"], "stop": p["stop"], "entry": p["entry"]}.get(label, hi - r * step)
        left = ("%9.2f |" % price) if (label or r in (0, height - 1)) else "          |"
        out.append(left + "".join(grid[r]) + ("  " + label if label else ""))
    axis = [" "] * len(bands)
    for j in sorted({0, len(bands) // 2, len(bands) - 1}):
        lab = times[j]
        s = max(0, min(j, len(bands) - len(lab)))
        axis[s:s + len(lab)] = list(lab)
    out.append("          +" + "-" * len(bands))
    out.append("           " + "".join(axis))
    out.append("* median   : middle half of the paths   . 90 % of the paths   - stop and target")
    return out


# ---------------------------------------------------------------- commands

def _write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
        fh.write("\n")


def compute(a):
    now = parse_now(a.now)
    plan = load_plan(a, now)
    if a.offline:
        candles, info = read_offline(a.offline, plan["symbol"])
        source = "offline"
    else:
        k = kite.Kite.connect()
        candles, info = fetch_bars(k, plan, now)
        source = "kite"
    return time_machine(candles, plan, now, a.replays, a.seed, a.window, a.until, source, info)


def cmd_run(a):
    doc = compute(a)
    if a.write:
        _write(a.write, doc)
    if a.json:
        slim = dict(doc, mc={k: v for k, v in doc["mc"].items() if k != "sample_paths"})
        print(json.dumps(slim, indent=2))
    else:
        print("\n".join(report(doc)))
        if a.write:
            print("Wrote %s." % a.write)
        print("Next: /trader-budget sizes it to your budget and your max loss per trade.")
    return 0


def cmd_chart(a):
    if a.from_file:
        if not os.path.exists(a.from_file):
            raise TMError("no %s: run timemachine.py run --write first" % a.from_file)
        try:
            with open(a.from_file, encoding="utf-8") as fh:
                doc = json.load(fh)
            doc["mc"]["bands"]
        except (ValueError, KeyError, TypeError):
            raise TMError("%s is not a time machine result" % a.from_file)
    else:
        doc = compute(a)
    print("\n".join(render_chart(doc)))
    print("\n".join([doc.get("verdict", "")] + list(doc.get("verdict_notes") or [])))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Time Machine: replay a planned NSE day trade (trader-timemachine).")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--plan", default="trader/plan.json", help="the plan from trader-exit")
        p.add_argument("--symbol", help="or give the plan as flags: NSE trading symbol, e.g. RELIANCE")
        p.add_argument("--side", default="buy", choices=["buy", "sell"])
        p.add_argument("--date", help="the plan's day, YYYY-MM-DD (default today, IST)")
        p.add_argument("--entry", type=float)
        p.add_argument("--stop", type=float)
        p.add_argument("--target", type=float)
        p.add_argument("--entry-time", help="the entry bar's close, IST, e.g. 10:15")
        p.add_argument("--setup", default="orb")
        p.add_argument("--or-minutes", type=int, default=15)
        p.add_argument("--stop-rule", default="auto", choices=["auto", "or_mid", "or_low", "or_high", "pct"])
        p.add_argument("--flat-by", help="with --symbol: the plan's flat-by time (default 15:05)")
        p.add_argument("--tick", type=float, help="with --symbol: the tick size (default Kite's instrument row)")
        p.add_argument("--replays", "--sims", dest="replays", type=int, default=10000)
        p.add_argument("--seed", type=int, default=19)
        p.add_argument("--window", type=int, default=2, help="draw from the same time of day +- this many bars")
        p.add_argument("--until", help="end each replay at this time instead of the plan's flat_by")
        p.add_argument("--offline", metavar="FILE", help="a saved bar file (.json or .json.gz), no network")
        p.add_argument("--now", help='pretend it is this time ("2026-10-29 10:15" IST)')

    p = sub.add_parser("run", help="replay the plan: 10,000 simulated sessions plus the history")
    common(p)
    p.add_argument("--json", action="store_true")
    p.add_argument("--write", nargs="?", const="trader/timemachine.json", metavar="PATH",
                   help="save the result (default trader/timemachine.json)")
    p = sub.add_parser("chart", help="ASCII fan chart of the replays")
    common(p)
    p.add_argument("--from", dest="from_file", nargs="?", const="trader/timemachine.json", metavar="PATH",
                   help="draw a saved result instead of running again")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"run": cmd_run, "chart": cmd_chart}[a.cmd](a)
    except (TMError, kite.KiteError) as e:
        print("timemachine.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
