#!/usr/bin/env python3
"""Exit Planner for trader-exit: sets the stop and the target BEFORE you
enter an NSE stock, so you know when to get out. Standard library only.
Long side only. It never places an order.

    python3 exit.py plan                                     # from trader/entry.json: or-mid stop, 2R target
    python3 exit.py plan --stop or-mid --target 1R --write   # stop 1816.70, target 1847.30 on the reel
    python3 exit.py plan --symbol HINDUNILVR --entry 1832 --or-high 1829.50 --or-low 1804
    python3 exit.py plan --stop 1.5xATR                      # 1.5 x the 14-day ATR from Kite's daily bars
    python3 exit.py plan --symbol ONGC --entry 227.60 --setup level --stop 1%
    python3 exit.py show                                     # the current trader/plan.json

Stops (a long's stop rounds DOWN to the instrument's tick, away from the entry):
  or-mid     the opening-range midpoint (the default for an orb entry)
  or-low     the opening-range low
  PRICE      your price (1816.70, or price:1816.70)
  N%         N percent under the entry (1%, or pct:1)
  NxATR      N times the 14-day ATR under the entry (1.5xATR, or atr:1.5), on
             Kite's daily bars up to the day before. With fewer than 14 days
             it averages what there is and says so.
  A level setup has no opening-range default: give a stop.

Targets (round DOWN to the tick too, never up):
  2R (default), 1R, 1.5R ...   entry + R x risk, where risk = entry - stop
  PRICE                        your price (1847.30, or price:1847.30)

The tick comes from Kite's instrument row (tick_size: 0.05 for most stocks,
0.01 for stocks under Rs 250, 0.10 for some large caps); it is never assumed.

Refused, with the reason: a stop at or above the entry; a stop more than 10 %
under the entry (outside Zerodha's cover-order range); a stop closer than
2 ticks to the entry (it will not survive the spread); a target at or below
the entry; an entry.json that has not triggered (unless you give --entry); a
short (this pack plans longs only).

flat_by: --flat-by, else "flat_by" in trader/config.json, else 15:05
(nse.DEFAULT_FLAT_BY). Out by then, win or lose. Refused at or after 15:12
for a Closing Auction Session stock (Zerodha's MIS auto square-off for F&O
stocks, Rs 50 + GST) and at or after 15:30 for any other stock (the close).
"min_reward_risk" in the config (default 1.0) only warns here; trader-risk
enforces it.

A stop is a plan, not a guarantee: a gap or a fast market can fill past it.
Exit codes: plan 0 = a plan, 1 = refused or an error. show 0 = a plan exists.
"""

import argparse
import datetime
import json
import os
import re
import sys
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trader-kite"))
import kite   # noqa: E402
import nse    # noqa: E402

INTERVALS = ("5minute", "minute")
DEFAULTS = {"flat_by": None, "min_reward_risk": 1.0}
ATR_DAYS = 14
ATR_LOOKBACK_DAYS = 45      # calendar days of daily bars to ask Kite for (about 30 sessions)
MIN_ATR_BARS = 3
MIN_TICKS = 2                # a stop closer than this to the entry is inside the spread
CO_RANGE_PCT = Decimal("10")   # Zerodha's cover-order trigger must be within 10 % of the price
PAISA = Decimal("0.01")
NOTE = "A stop is a plan, not a guarantee: a gap or a fast market can fill past it."
IST = nse.IST


class PlanError(Exception):
    pass


# ---------------------------------------------------------------- time

def parse_now(text):
    """--now -> aware IST datetime. A time without an offset is IST."""
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
        raise PlanError('--now wants an IST time like "2026-10-29 10:15"')
    return nse.to_ist(dt)


def parse_hhmm(text, what):
    try:
        return nse.parse_hhmm(text)
    except (ValueError, TypeError):
        raise PlanError("%s wants an IST time like 15:05, not %r" % (what, text))


def hm(t):
    return "%02d:%02d" % (t.hour, t.minute)


# ---------------------------------------------------------------- money math (Decimal, to the tick)

def D(x, what="a number"):
    try:
        if isinstance(x, float):
            return Decimal(repr(x))
        s = str(x).strip().replace(",", "")
        if s.lower().startswith("rs"):
            s = s[2:].strip()
        return Decimal(s)
    except (InvalidOperation, ValueError):
        raise PlanError("%s is not a number: %r" % (what, x))


def paise(x):
    """Round half up to the paisa (prices are quoted to two decimals)."""
    return D(x).quantize(PAISA, rounding=ROUND_HALF_UP)


def floor_tick(x, tick):
    """Round down to the instrument's tick: a long's stop and target, away from the entry."""
    steps = (D(x) / tick).to_integral_value(rounding=ROUND_FLOOR)
    return (steps * tick).quantize(PAISA)


def money(x):
    return "Rs %s" % format(D(x).quantize(PAISA), ",f")


def num(x):
    """1, 1.5, 2.25: a Decimal without trailing zeros."""
    s = format(D(x).normalize(), "f")
    return s


def ratio(x):
    """reward:risk for people: 1.0, 1.5, 1.37."""
    s = "%.2f" % float(x)
    return s[:-1] if s.endswith("0") else s


def tick_of(value):
    tick = D(value, "tick_size")
    if tick <= 0:
        raise PlanError("the tick size must be above 0 (got %s)" % value)
    return tick


WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}
NUMBER = r"([0-9][0-9,]*\.?[0-9]*)"


def parse_stop(text):
    t = str(text).strip().lower().replace(" ", "")
    if t.startswith("rs"):
        t = t[2:]
    if t in ("or-mid", "or_mid", "mid", "or-midpoint"):
        return "or_mid", None
    if t in ("or-low", "or_low", "low"):
        return "or_low", None
    m = re.match(r"^(?:(price|pct|atr):)?" + NUMBER + r"(%|x?atr)?$", t)
    if not m or (m.group(1) and m.group(3)):
        raise PlanError("--stop is or-mid, or-low, a price, N%% or NxATR (got %r)" % text)
    kind = m.group(1) or {"%": "pct", "xatr": "atr", "atr": "atr"}.get(m.group(3) or "", "price")
    val = D(m.group(2), "--stop")
    if val <= 0:
        raise PlanError("--stop %s needs a number above 0" % kind)
    if kind == "pct" and val >= 100:
        raise PlanError("--stop N%% needs N under 100")
    return kind, val


def parse_target(text):
    t = str(text).strip().replace(" ", "")
    m = re.match(r"^([0-9]*\.?[0-9]+)[Rr]$", t)
    if m:
        r = D(m.group(1), "--target")
        if r <= 0:
            raise PlanError("--target needs an R multiple above 0, like 1R or 2R")
        return "r", r
    m = re.match(r"^(?:price:)?(?:rs)?" + NUMBER + "$", t.lower())
    if m:
        price = D(m.group(1), "--target")
        if price <= 0:
            raise PlanError("--target needs a price above 0")
        return "price", price
    raise PlanError("--target is 2R, 1R, 1.5R ... or a price (got %r)" % text)


def r_label(r):
    n = int(r) if r == r.to_integral_value() else None
    return "%sR (%s times the risk)" % (num(r), WORDS.get(n, num(r)))


def raw_stop(e, stop_rule, stop_param, or_high, or_low, atr, atr_bars):
    """(raw stop before rounding, label, detail) for one stop rule."""
    if stop_rule in ("or_mid", "or_low"):
        if or_high is None or or_low is None:
            raise PlanError("an %s stop needs the opening range (--or-high and --or-low)" % stop_rule.replace("_", "-"))
        oh, ol = paise(or_high), paise(or_low)
        if oh <= ol:
            raise PlanError("the opening-range high %s must be above its low %s" % (money(oh), money(ol)))
        if stop_rule == "or_mid":
            raw = (oh + ol) / 2
            return raw, "opening-range midpoint", "(%s + %s) / 2 = %s" % (money(oh), money(ol), num(raw))
        return ol, "opening-range low", ""
    if stop_rule == "price":
        return stop_param, "your price", ""
    if stop_rule == "pct":
        raw = e * (1 - stop_param / 100)
        return raw, "%s %% under the entry" % num(stop_param), "%s x (1 - %s %%) = %s" % (money(e), num(stop_param), num(raw))
    if stop_rule == "atr":
        if atr is None:
            raise PlanError("an ATR stop needs Kite's daily bars")
        a = D(round(atr, 6))
        raw = e - stop_param * a
        label = "%s x ATR(%d) under the entry" % (num(stop_param), atr_bars)
        detail = "ATR %s over the %d daily bars before the entry%s" % (
            money(a), atr_bars, " (fewer than %d so far)" % ATR_DAYS if atr_bars < ATR_DAYS else "")
        return raw, label, detail
    raise PlanError("unknown stop rule %r" % stop_rule)


def check_stop(e, stop, tick, label):
    if stop >= e:
        raise PlanError("the stop %s (%s) is at or above the entry %s: a long's stop goes under the entry"
                        % (money(stop), label, money(e)))
    if stop <= 0:
        raise PlanError("the stop works out to %s: pick a stop above Rs 0" % money(stop))
    risk = e - stop
    if risk < MIN_TICKS * tick:
        raise PlanError("the stop is %s under the entry, closer than %d ticks (%s): it will not survive the "
                        "spread, normal wiggle would stop you out. Widen it."
                        % (money(risk), MIN_TICKS, money(MIN_TICKS * tick)))
    if risk / e * 100 > CO_RANGE_PCT:
        raise PlanError("the stop %s is %s %% under the entry %s, more than %s %%: outside Zerodha's cover-order "
                        "range (the stop-loss trigger must be within %s %% of the price). Tighten it."
                        % (money(stop), ratio(risk / e * 100), money(e), num(CO_RANGE_PCT), num(CO_RANGE_PCT)))
    return risk


def compute(entry, stop_rule, stop_param, target_rule, target_param, tick, or_high=None, or_low=None,
            atr=None, atr_bars=None):
    """The stop, the target and the risk, on the tick. Raises PlanError with the reason."""
    e = paise(entry)
    if e <= 0:
        raise PlanError("the entry price must be above Rs 0")
    tick = tick_of(tick)
    raw, label, detail = raw_stop(e, stop_rule, stop_param, or_high, or_low, atr, atr_bars)
    stop = floor_tick(raw, tick)
    if stop != D(raw):
        detail = (detail + ", " if detail else "%s " % num(raw)) + "rounded down to the %s tick" % money(tick)
    risk = check_stop(e, stop, tick, label)
    if target_rule == "r":
        target = floor_tick(e + target_param * risk, tick)
        t_label = r_label(target_param)
    else:
        target = floor_tick(target_param, tick)
        t_label = None
    if target <= e:
        raise PlanError("the target %s is at or below the entry %s: a long's target goes above it"
                        % (money(target), money(e)))
    rr = ((target - e) / risk).quantize(PAISA, rounding=ROUND_HALF_UP)
    if t_label is None:
        t_label = "your price (%sR)" % ratio(rr)
    return {"entry": e, "stop": stop, "stop_label": label, "stop_detail": detail, "target": target,
            "target_r": target_param if target_rule == "r" else rr, "target_label": t_label,
            "risk": risk, "reward_risk": rr, "tick": tick}


# ---------------------------------------------------------------- Kite: the instrument row and daily bars

def connect():
    try:
        return kite.Kite.connect()
    except kite.KiteError as e:
        raise PlanError(str(e))


def instrument_facts(k, sym):
    try:
        row = k.instrument(sym)
        cas = sym in k.cas_symbols()
    except kite.KiteError as e:
        raise PlanError(str(e))
    return {"tick_size": float(row.get("tick_size") or 0), "cas": cas}


def daily_bars(k, sym, day):
    """Kite's daily candles for the sessions before `day`, oldest first."""
    start = nse.at(day - datetime.timedelta(days=ATR_LOOKBACK_DAYS), datetime.time(0, 0))
    end = nse.at(day - datetime.timedelta(days=1), datetime.time(23, 59))
    try:
        rows = k.historical(sym, "day", start, end)
    except kite.KiteError as e:
        raise PlanError(str(e))
    return [{"t": t, "h": h, "l": l, "c": c} for t, o, h, l, c, v in rows if t.date() < day]


def atr_of(bars, n=ATR_DAYS):
    """Average true range of the last n daily bars (all of them if fewer). The
    true range uses the previous close, so the first bar only supplies it."""
    trs = []
    for prev, b in zip(bars, bars[1:]):
        trs.append(max(b["h"] - b["l"], abs(b["h"] - prev["c"]), abs(b["l"] - prev["c"])))
    use = trs[-n:]
    return sum(use) / len(use), len(use)


# ---------------------------------------------------------------- plan

def read_json(path, what):
    if not path or not os.path.exists(path):
        raise PlanError("no %s at %s" % (what, path))
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise PlanError("%s is not valid JSON: %s" % (path, e))


def read_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        doc = read_json(path, "config")
        for k in DEFAULTS:
            if doc.get(k) is not None:
                cfg[k] = doc[k]
    return cfg


def resolve_flat_by(explicit, cfg, cas, entry_time, symbol):
    """HH:MM. --flat-by, else the config's flat_by, else the pack default (15:05)."""
    if explicit:
        source, t = "--flat-by", parse_hhmm(explicit, "--flat-by")
    elif cfg.get("flat_by"):
        source, t = "flat_by in trader/config.json", parse_hhmm(cfg["flat_by"], "flat_by in the config")
    else:
        source, t = "the default", nse.DEFAULT_FLAT_BY
    if cas and t >= nse.MIS_SQUARE_OFF_CAS:
        raise PlanError("%s %s is at or after %s, when Zerodha squares off MIS positions in %s (a Closing Auction "
                        "Session stock, Rs 50 + GST each time): pick an earlier time"
                        % (source, hm(t), hm(nse.MIS_SQUARE_OFF_CAS), symbol))
    if not cas and t >= nse.CLOSE:
        raise PlanError("%s %s is at or after the %s close: pick an earlier time" % (source, hm(t), hm(nse.CLOSE)))
    if entry_time and entry_time >= t:
        raise PlanError("the entry at %s is at or after your flat-by %s: there is no time left to hold it"
                        % (hm(entry_time), hm(t)))
    return hm(t)


def gather_flags(a):
    if a.entry is None:
        raise PlanError("--symbol needs --entry PRICE")
    if (a.or_high is None) != (a.or_low is None):
        raise PlanError("give both --or-high and --or-low, or neither")
    return {"symbol": kite.normalize_symbol(a.symbol), "entry": a.entry,
            "setup": a.setup or ("orb" if a.or_high is not None else "level"),
            "date": a.date, "entry_time": a.time, "entry_bar": a.entry_bar, "interval": a.interval or "5minute",
            "or_high": a.or_high, "or_low": a.or_low, "or_minutes": a.or_minutes,
            "cas": None, "tick_size": None, "source": "flags"}


def gather_entry_file(a):
    path = a.from_path or "trader/entry.json"
    doc = read_json(path, "entry (run /trader-entry first, or give --symbol and --entry)")
    if str(doc.get("side") or "buy").lower() not in ("buy", "long"):
        raise PlanError("%s is a %s: this pack plans longs only" % (path, doc.get("side")))
    if doc.get("status") != "triggered" and a.entry is None:
        raise PlanError("%s says %s is %s, not triggered: plan once it triggers, or give --entry PRICE to plan ahead"
                        % (path, doc.get("symbol"), doc.get("status") or "unknown"))
    orr = doc.get("opening_range") or {}
    planned = a.entry is None
    bar = doc.get("bar") if doc.get("bar") is not None else doc.get("bar_index")
    info = {"symbol": kite.normalize_symbol(str(doc.get("symbol") or "")),
            "entry": doc.get("price") if planned else a.entry,
            "setup": a.setup or doc.get("setup") or "orb", "date": a.date or doc.get("date"),
            "entry_time": a.time or (doc.get("time") if planned else None),
            "entry_bar": a.entry_bar if a.entry_bar is not None else (bar if planned else None),
            "interval": a.interval or doc.get("interval") or "5minute",
            "or_high": a.or_high if a.or_high is not None else orr.get("high"),
            "or_low": a.or_low if a.or_low is not None else orr.get("low"),
            "or_minutes": orr.get("minutes") or a.or_minutes,
            "cas": doc.get("cas"), "tick_size": doc.get("tick_size"), "source": path}
    if not info["symbol"] or info["entry"] is None:
        raise PlanError("%s has no symbol or price" % path)
    return info


def gather(a):
    """The entry facts, from trader/entry.json or the flags."""
    if a.symbol and a.from_path:
        raise PlanError("use --from or --symbol, not both")
    if a.side in ("sell", "short"):
        raise PlanError("this pack plans longs only: no stops above a short entry. Plan a buy instead.")
    info = gather_flags(a) if a.symbol else gather_entry_file(a)
    if info["interval"] not in INTERVALS:
        raise PlanError("interval is 5minute or minute")
    return info


def plan_day(info, now):
    if not info["date"]:
        return now.date()
    try:
        return datetime.date.fromisoformat(str(info["date"]))
    except ValueError:
        raise PlanError("date wants YYYY-MM-DD, not %r" % info["date"])


def build_plan(a, now):
    info = gather(a)
    cfg = read_config(a.config)
    stop_text = a.stop
    if not stop_text:
        if info["setup"] != "orb":
            raise PlanError("a %s setup has no opening-range stop to default to: give --stop PRICE, N%% or NxATR"
                            % info["setup"])
        stop_text = "or-mid"
    stop_rule, stop_param = parse_stop(stop_text)
    target_rule, target_param = parse_target(a.target or "2R")
    day = plan_day(info, now)
    entry_time = parse_hhmm(info["entry_time"], "the entry time") if info["entry_time"] else None
    k = None
    if info["tick_size"] is None or info["cas"] is None:
        k = connect()
        info.update(instrument_facts(k, info["symbol"]))
    atr = atr_n = None
    if stop_rule == "atr":
        k = k or connect()
        bars = daily_bars(k, info["symbol"], day)
        if len(bars) < MIN_ATR_BARS + 1:
            raise PlanError("an ATR stop needs at least %d daily bars of %s before %s; Kite sent %d"
                            % (MIN_ATR_BARS + 1, info["symbol"], day, len(bars)))
        atr, atr_n = atr_of(bars)
    c = compute(info["entry"], stop_rule, stop_param, target_rule, target_param, info["tick_size"],
                info["or_high"], info["or_low"], atr, atr_n)
    flat_by = resolve_flat_by(a.flat_by, cfg, bool(info["cas"]), entry_time, info["symbol"])
    orr = None
    if info["or_high"] is not None and info["or_low"] is not None:
        orr = {"high": float(paise(info["or_high"])), "low": float(paise(info["or_low"])),
               "minutes": int(info["or_minutes"] or 15)}
    plan = {"created_by": "trader-exit", "symbol": info["symbol"], "side": "buy", "date": day.isoformat(),
            "setup": info["setup"], "entry": float(c["entry"]),
            "entry_time": hm(entry_time) if entry_time else None,
            "entry_bar": info["entry_bar"], "interval": info["interval"], "opening_range": orr,
            "stop": float(c["stop"]), "stop_rule": stop_rule, "stop_label": c["stop_label"],
            "target": float(c["target"]), "target_r": float(c["target_r"]), "target_label": c["target_label"],
            "risk_per_share": float(c["risk"]), "reward_risk": float(c["reward_risk"]), "flat_by": flat_by,
            "cas": bool(info["cas"]), "tick_size": float(c["tick"]),
            "stop_detail": c["stop_detail"], "created_at": now.isoformat(), "note": NOTE}
    if atr is not None:
        plan["atr"] = round(atr, 4)
        plan["atr_bars"] = atr_n
    return plan, plan_warnings(c, cfg, day, now)


def plan_warnings(c, cfg, day, now):
    warnings = []
    mrr = cfg.get("min_reward_risk")
    try:
        if mrr is not None and c["reward_risk"] < D(mrr):
            warnings.append("Reward:risk %s is under your min_reward_risk %s in the config: trader-risk checks that."
                            % (ratio(c["reward_risk"]), num(D(mrr))))
    except PlanError:
        pass
    if day != now.date():
        warnings.append("This plan is for %s, not today (%s)." % (day.isoformat(), now.date().isoformat()))
    ok, why = nse.trading_day(day)
    if not ok:
        warnings.append("%s is not an NSE trading day (%s)." % (day.isoformat(), why))
    elif why:
        warnings.append(why + ".")
    return warnings


def describe(plan):
    day = datetime.date.fromisoformat(plan["date"])
    setup = {"orb": "opening-range breakout", "level": "close above a level"}.get(plan["setup"], plan["setup"])
    e = D(plan["entry"])
    risk = D(plan["risk_per_share"])
    pct = (risk / e * 100).quantize(PAISA, rounding=ROUND_HALF_UP)
    when = ""
    if plan.get("entry_time"):
        when = " at %s" % plan["entry_time"]
        if plan.get("entry_bar") is not None:
            when += " (bar %d)" % plan["entry_bar"]
    out = ["Exit plan: %s long, %s, %s on %s bars (MIS, intraday)" % (
        plan["symbol"], day.strftime("%a %Y-%m-%d"), setup, "5-minute" if plan["interval"] == "5minute" else "1-minute")]
    out.append("  Entry         %s%s" % (money(plan["entry"]), when))
    out.append("  Stop          %s  %s%s" % (money(plan["stop"]), plan["stop_label"],
                                             (": " + plan["stop_detail"]) if plan.get("stop_detail") else ""))
    out.append("  Target        %s  %s" % (money(plan["target"]), plan["target_label"]))
    out.append("  Risk a share  %s (%s %% of the entry, tick %s)" % (money(plan["risk_per_share"]), pct,
                                                                     money(plan.get("tick_size") or 0)))
    out.append("  Reward:risk   %s" % ratio(plan["reward_risk"]))
    out.append("  Flat by       %s IST, win or lose: no overnight hold%s" % (
        plan["flat_by"], " (Zerodha squares off this CAS stock's MIS at 15:12)" if plan.get("cas") else ""))
    out.append("Set before the entry. If price hits the stop, you are out; never move it further away.")
    out.append(NOTE)
    return out


def cmd_plan(a):
    now = parse_now(a.now)
    plan, warnings = build_plan(a, now)
    if a.write:
        d = os.path.dirname(a.write)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.write, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, indent=2)
            fh.write("\n")
    if a.json:
        print(json.dumps(dict(plan, warnings=warnings), indent=2))
        return 0
    for line in describe(plan):
        print(line)
    for w in warnings:
        print("Note: " + w)
    if a.write:
        print("Wrote %s." % a.write)
    print("Next: /trader-timemachine replays it, then /trader-budget sizes it to your max loss. Nothing was placed.")
    return 0


def cmd_show(a):
    if not os.path.exists(a.plan):
        print("No plan at %s yet. Make one: exit.py plan --write" % a.plan)
        return 1
    plan = read_json(a.plan, "plan")
    for k in ("symbol", "date", "entry", "stop", "target", "risk_per_share", "reward_risk", "flat_by"):
        if plan.get(k) is None:
            raise PlanError("%s is missing %r: make a new plan with exit.py plan --write" % (a.plan, k))
    if a.json:
        print(json.dumps(plan, indent=2))
        return 0
    for line in describe(dict({"setup": "orb", "interval": "5minute", "stop_label": "", "target_label": ""}, **plan)):
        print(line)
    today = parse_now(a.now).date().isoformat()
    if plan["date"] != today:
        print("Note: this plan is for %s, not today (%s)." % (plan["date"], today))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Exit Planner (trader-exit): the stop and target before the entry.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("plan", help="set the stop and target")
    p.add_argument("--from", dest="from_path", metavar="PATH", help="entry.json to plan from (default trader/entry.json)")
    p.add_argument("--symbol", help="Kite trading symbol, e.g. HINDUNILVR (with --entry)")
    p.add_argument("--entry", type=float, help="entry price (or a plan-ahead price with --from)")
    p.add_argument("--or-high", type=float)
    p.add_argument("--or-low", type=float)
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--setup", choices=["orb", "level"])
    p.add_argument("--date", help="session date YYYY-MM-DD (default: the entry's, or today)")
    p.add_argument("--time", help="entry time, e.g. 10:00 (IST)")
    p.add_argument("--entry-bar", type=int)
    p.add_argument("--interval", choices=list(INTERVALS))
    p.add_argument("--side", default="buy", choices=["buy", "long", "sell", "short"], help="buy only")
    p.add_argument("--stop", help="or-mid (orb default), or-low, PRICE, N%%, NxATR")
    p.add_argument("--target", help="2R (default), 1R, 1.5R, PRICE")
    p.add_argument("--flat-by", help="HH:MM IST (default: config flat_by, else 15:05)")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--now", help='pretend it is this IST time, e.g. "2026-10-29 10:15"')
    p.add_argument("--write", nargs="?", const="trader/plan.json", metavar="PATH",
                   help="save the plan (default trader/plan.json)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("show", help="print the current plan")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--now")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"plan": cmd_plan, "show": cmd_show}[a.cmd](a)
    except PlanError as e:
        print("exit.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
