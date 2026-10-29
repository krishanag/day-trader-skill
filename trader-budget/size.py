#!/usr/bin/env python3
"""Position sizing for trader-budget. Standard library only.

Sizes ONE day trade from trader/plan.json (written by trader-exit) to your
budget and your max loss per trade, then writes one Zerodha MIS order to
trader/orders.json in the exact shape of Kite's place_order body: a cover
order (the entry plus the exchange-held stop-loss leg) by default, or with
"order_style": "regular" a plain MIS entry and a separate SL-M stop leg. It
never places anything: trader-risk checks the order and trader-broker asks
you yes or no.

    python3 size.py                                  # trader/plan.json + trader/config.json
    python3 size.py --write                          # write trader/orders.json
    python3 size.py --limit                          # a limit entry at the plan's entry
    python3 size.py --limit 1174.00 --write          # a limit entry at 1174.00
    python3 size.py --now "2026-10-29 09:45" --json  # pin the clock (IST)
    python3 size.py --offline instruments.csv        # tick sizes from a saved Kite instrument CSV

How many shares. Whole shares only: NSE equities trade one share at a time
and Kite takes whole quantities.

    shares = floor of the smallest of
      max loss       max_loss_per_trade_inr / risk per share (entry minus stop)
      position cap   budget_inr x max_position_pct / entry
      max order      max_order_inr / entry
      cash           (budget_inr minus the cash buffer) / entry
                     (and no more than the cash in trader/portfolio.json, or
                     your live Kite balance when there is a session and no
                     portfolio file; times mis_leverage_x with
                     "use_mis_leverage": true)

The entry it sizes at: a limit order's limit price; for a market order the
plan's entry, or the price in trader/prices.json if that is fresh (under 15
minutes old) and higher, because a price that ran up since the plan means a
bigger loss at the same stop. Zero shares (the stop is too far away for your
max loss) is a plain refusal: no order is written. So is a reward:risk under
min_reward_risk (the price ran toward the target since the plan), a stop
more than 10 percent under the entry (Zerodha's cover-order range), or an
order under min_order_inr.

Prices snap to the stock's tick size (Kite's instrument row, else the plan's
tick_size, else 0.05): the stop and the target round down, never up.

Kite is optional here. With today's session (trader-kite) the tool reads the
instrument row for the tick size and, when there is no trader/portfolio.json,
your live balance from Kite's margins call. Without a session it sizes from
the plan and the config alone.
"""

import argparse
import datetime
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trader-kite"))
import kite   # noqa: E402
import nse    # noqa: E402

# the contract's config.example.json. Missing keys fall back to these.
DEFAULTS = {
    "budget_inr": None,
    "cash_buffer_pct": 10,
    "max_position_pct": 75,
    "max_order_inr": 75000,
    "min_order_inr": 500,
    "max_loss_per_trade_inr": 500,
    "max_daily_loss_inr": 1500,
    "max_trades_per_day": 3,
    "max_orders_per_day": 10,
    "min_reward_risk": 1.0,
    "require_stop": True,
    "no_entries_before": "09:30",
    "no_entries_after": "14:30",
    "flat_by": "15:05",
    "exchange": "NSE",
    "product": "MIS",
    "order_style": "co",
    "use_mis_leverage": False,
    "mis_leverage_x": 1.0,
    "broker": "zerodha",
    "paper": True,
    "blocked": [],
    "allow_bse": False,
    "allow_fno": False,
    "allow_short": False,
}
EPS = 1e-9
FRESH_MINUTES = 15
DEFAULT_TICK = 0.05
CO_RANGE_PCT = 10.0          # Zerodha takes a cover-order trigger within 10 percent of the price
TAG_PREFIX = "dts"
TAG_MAX = 20
SETUP_WORDS = {"orb": "opening-range breakout", "flag": "flag", "bounce": "oversold bounce"}
STYLES = ("co", "regular")


class SizeError(Exception):
    pass


# ---------------------------------------------------------------- time (IST)

def parse_now(text):
    """"2026-10-29 09:45" (IST) or ISO with an offset -> aware IST datetime."""
    if not text:
        return nse.now_ist().replace(microsecond=0)
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        raise SizeError("--now wants a time like \"2026-10-29 09:45\" (IST)")
    return nse.to_ist(dt)


def parse_time(text):
    """An ISO timestamp (with +05:30, or naive = IST) -> aware IST datetime, or None."""
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return nse.to_ist(dt)


def iso(dt):
    return dt.astimezone(nse.IST).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- inputs

def read_json(path, what):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise SizeError("no %s at %s" % (what, path))
    except json.JSONDecodeError as e:
        raise SizeError("%s is not valid JSON: %s" % (path, e))


def load_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        doc = read_json(path, "config")
        if not isinstance(doc, dict):
            raise SizeError("%s is not a JSON object" % path)
        cfg.update(doc)
    return cfg


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fresh_price(doc, sym, now, minutes=FRESH_MINUTES):
    """(price, time, fresh) for sym from a prices.json doc, or (None, None, False).

    The contract's shape is prices[SYM] = {"last", "time", "prev_close", "source"};
    a bare number is taken as the price with the file's as_of as its time."""
    if not isinstance(doc, dict):
        return None, None, False
    entry = (doc.get("prices") or {}).get(sym)
    if isinstance(entry, dict):
        p, t = _f(entry.get("last")), parse_time(entry.get("time")) or parse_time(doc.get("as_of"))
    else:
        p, t = _f(entry), parse_time(doc.get("as_of"))
    if p is None or p <= 0:
        return None, None, False
    if t is None:
        return p, None, False
    age = (now - t).total_seconds()
    return p, t, -60 <= age <= minutes * 60


def instrument_row(sym, offline=None, k=None):
    """Kite's instrument row for sym: from a saved CSV (--offline), else today's session, else None."""
    if offline:
        try:
            with open(offline, encoding="utf-8") as fh:
                table = kite.parse_instruments(fh.read())
        except OSError as e:
            raise SizeError("cannot read %s: %s" % (offline, e))
        row = table.get(sym)
        if row is None:
            raise SizeError("%s is not in %s (an NSE equity row is needed for the tick size)" % (sym, offline))
        return row
    if k is not None:
        try:
            return k.instrument(sym)
        except kite.KiteError as e:
            raise SizeError(str(e))
    return None


def kite_session():
    """Today's Kite client, or (None, why). No session is fine for this tool."""
    try:
        return kite.Kite.connect(), ""
    except kite.KiteError as e:
        return None, str(e)


def live_cash(k):
    """The equity segment's live balance from Kite's margins call."""
    data = k.margins() or {}
    bal = _f((data.get("available") or {}).get("live_balance"))
    if bal is None:
        raise kite.KiteError("Kite's margins call had no available.live_balance")
    return bal


# ---------------------------------------------------------------- money helpers

def round_paise(x):
    """Round a price to the paisa (half up)."""
    return math.floor(float(x) * 100 + 0.5 + EPS) / 100.0


def snap_down(x, tick):
    """Round a price down to the tick: 1163.05 at tick 0.10 -> 1163.00."""
    units = int(math.floor(round(float(x) / tick, 6) + EPS))
    return round_paise(units * tick)


def on_tick(x, tick):
    q = float(x) / tick
    return abs(q - round(q)) < 1e-6


def shares_for(inr, per_share):
    """Whole shares that fit in inr at per_share, in exact integer paise."""
    u, p = int(round(float(inr) * 100)), int(round(float(per_share) * 100))
    if p <= 0 or u <= 0:
        return 0
    return u // p


def _price(x):
    return "%.2f" % round_paise(x)


def _num(x):
    """1250 -> "1250", 1118.13 -> "1118.13"."""
    x = round(float(x), 2)
    if abs(x - round(x)) < 0.005:
        return str(int(round(x)))
    return "%.2f" % x


def money(x):
    x = float(x)
    if abs(x - round(x)) < 0.005:
        return "Rs " + "{:,.0f}".format(round(x))
    return "Rs " + "{:,.2f}".format(x)


def order_tag(setup):
    """"dts" + the setup, letters and digits only, at most 20 characters (Kite's tag rule)."""
    return re.sub(r"[^A-Za-z0-9]", "", TAG_PREFIX + str(setup or ""))[:TAG_MAX] or TAG_PREFIX


# ---------------------------------------------------------------- the sizing

def _check_plan(plan, c, today):
    sym = str(plan.get("symbol") or "").strip().upper()
    if not sym:
        raise SizeError("the plan has no symbol (run trader-exit)")
    side = str(plan.get("side") or "buy").strip().lower()
    if side != "buy":
        raise SizeError("this pack sizes long (buy) day trades only; the plan says %r" % side)
    if today and plan.get("date") and str(plan["date"]) != today:
        raise SizeError("the plan is for %s and today is %s (IST): make a new plan with trader-exit"
                        % (plan["date"], today))
    entry, stop, target = _f(plan.get("entry")), _f(plan.get("stop")), _f(plan.get("target"))
    for name, v in (("entry", entry), ("stop", stop), ("target", target)):
        if v is None or v <= 0:
            raise SizeError("the plan needs a positive %s (run trader-exit); a cover order needs the entry "
                            "and the stop, and the target is what trader-alerts rings at" % name)
    blocked = {str(b).upper() for b in (c.get("blocked") or [])}
    if sym in blocked:
        raise SizeError("%s is on your blocked list" % sym)
    return sym, entry, stop, target


def _check_config(c):
    budget = _f(c.get("budget_inr"))
    if not budget or budget <= 0:
        raise SizeError("no budget: pass --budget or set \"budget_inr\" in trader/config.json")
    buf_pct = float(c.get("cash_buffer_pct") or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")
    style = str(c.get("order_style") or "co").lower()
    if style not in STYLES:
        raise SizeError("order_style must be \"co\" or \"regular\", got %r" % c.get("order_style"))
    if str(c.get("product") or "MIS").upper() != "MIS":
        raise SizeError("product must be MIS: this pack trades intraday only, got %r" % c.get("product"))
    exchange = str(c.get("exchange") or "NSE").upper()
    if exchange == "BSE" and not c.get("allow_bse"):
        raise SizeError("exchange BSE needs \"allow_bse\": true")
    if exchange not in ("NSE", "BSE"):
        raise SizeError("exchange must be NSE (or BSE with allow_bse), got %r" % c.get("exchange"))
    lev = 1.0
    if c.get("use_mis_leverage"):
        lev = _f(c.get("mis_leverage_x")) or 0.0
        if lev < 1:
            raise SizeError("mis_leverage_x must be at least 1 when use_mis_leverage is true")
    return budget, buf_pct, style, exchange, lev


def size_trade(plan, cfg, budget=None, limit=None, price=None, cash=None, today=None, row=None):
    """Pure function: one plan + your rules -> the sizing (a dict). qty may be 0.

    limit  the limit price for a limit entry (None = market)
    price  a FRESH latest price; a market entry is sized at the higher of it and the plan's entry
    cash   the cash in trader/portfolio.json or your Kite balance, if known
    today  IST date (YYYY-MM-DD); a plan for another day is refused
    row    Kite's instrument row (tick_size, lot_size); else the plan's tick_size, else 0.05"""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    if not isinstance(plan, dict):
        raise SizeError("the plan is not a JSON object")
    sym, entry_plan, stop_raw, target_raw = _check_plan(plan, c, today)
    if budget is not None:
        c["budget_inr"] = budget
    budget, buf_pct, style, exchange, lev = _check_config(c)
    tick = _f((row or {}).get("tick_size")) or _f(plan.get("tick_size")) or DEFAULT_TICK
    tick_from = "kite" if row else ("plan" if _f(plan.get("tick_size")) else "default")
    lot = int((row or {}).get("lot_size") or 1)

    if limit is not None:
        ref, ref_from = snap_down(limit, tick), "limit"
    elif price is not None and round_paise(price) > round_paise(entry_plan):
        ref, ref_from = round_paise(price), "price"
    else:
        ref, ref_from = round_paise(entry_plan), "plan"
    stop, target = snap_down(stop_raw, tick), snap_down(target_raw, tick)
    snapped = [w for w, a, b in (("stop", stop_raw, stop), ("target", target_raw, target))
               if abs(round_paise(a) - b) > EPS]
    if stop >= ref:
        raise SizeError("the stop %s is not below the entry %s; a long trade needs its stop underneath"
                        % (_price(stop), _price(ref)))
    if stop < ref * (1 - CO_RANGE_PCT / 100.0) - EPS:
        raise SizeError("the stop %s is more than %d percent under the entry %s: Zerodha's cover order will not "
                        "take it. Plan a closer stop with trader-exit" % (_price(stop), int(CO_RANGE_PCT), _price(ref)))
    if target <= ref:
        raise SizeError("the target %s is not above the entry %s: the move has already happened. "
                        "Skip it or make a new plan with trader-exit" % (_price(target), _price(ref)))
    risk_p = int(round((ref - stop) * 100))
    reward_p = int(round((target - ref) * 100))
    risk = risk_p / 100.0

    cash_buffer = round(budget * buf_pct / 100.0, 2)
    base, base_note = budget, "budget %s" % money(budget)
    if cash is not None and cash < budget - 0.005:
        base, base_note = cash, "cash %s" % money(cash)
    available = max(round((base - cash_buffer) * lev, 2), 0.0)
    cash_note = "%s minus the %s cash buffer" % (base_note, money(cash_buffer))
    if lev > 1:
        cash_note += " x %s MIS leverage" % _num(lev)

    limits = []
    max_loss = _f(c.get("max_loss_per_trade_inr"))
    if max_loss:
        limits.append({"name": "max loss", "inr": max_loss, "per": risk, "shares": shares_for(max_loss, risk),
                       "how": "%s max loss / %s risk a share" % (money(max_loss), _price(risk))})
    cap_pct = _f(c.get("max_position_pct"))
    if cap_pct:
        cap = round(budget * cap_pct / 100.0, 2)
        limits.append({"name": "position cap", "inr": cap, "per": ref, "shares": shares_for(cap, ref),
                       "how": "%s%% of %s = %s / %s" % (_num(cap_pct), money(budget), money(cap), _price(ref))})
    max_order = _f(c.get("max_order_inr"))
    if max_order:
        limits.append({"name": "max order", "inr": max_order, "per": ref, "shares": shares_for(max_order, ref),
                       "how": "%s / %s" % (money(max_order), _price(ref))})
    limits.append({"name": "cash", "inr": available, "per": ref, "shares": shares_for(available, ref),
                   "how": "%s = %s / %s" % (cash_note, money(available), _price(ref))})

    qty = max(0, min(l["shares"] for l in limits))
    qty -= qty % lot
    bound = [l["name"] for l in limits if l["shares"] == qty] or [min(limits, key=lambda l: l["shares"])["name"]]
    for l in limits:
        l["binds"] = l["name"] == bound[0]
    cost = round(qty * ref, 2)
    loss = round(qty * risk_p / 100.0, 2)
    refusal = None
    if qty == 0:
        if bound[0] == "max loss":
            refusal = ("one share risks %s (entry %s minus stop %s), more than your %s max loss per trade. "
                       "No order. Skip this trade, or plan a closer stop with trader-exit."
                       % (money(risk), _price(ref), _price(stop), money(max_loss)))
        else:
            how = [l["how"] for l in limits if l["name"] == bound[0]][0]
            refusal = ("one share of %s costs %s, more than your %s limit (%s). No order."
                       % (sym, money(ref), bound[0], how))
    elif _f(c.get("min_reward_risk")) and reward_p < _f(c["min_reward_risk"]) * risk_p - EPS:
        refusal = ("reward:risk %.2f (target %s, entry %s, stop %s) is under your minimum %s, so trader-risk "
                   "would block it. No order. Skip it, or make a new plan with trader-exit."
                   % (reward_p / float(risk_p), _price(target), _price(ref), _price(stop), _num(c["min_reward_risk"])))
    elif cost < float(c.get("min_order_inr") or 0) - 0.005:
        refusal = ("%d share%s of %s is %s, under your %s minimum order. No order."
                   % (qty, "" if qty == 1 else "s", sym, money(cost), money(c["min_order_inr"])))
    return {
        "symbol": sym, "exchange": exchange, "side": "buy", "setup": plan.get("setup", ""), "date": plan.get("date"),
        "entry_time": plan.get("entry_time", ""), "entry_plan": round_paise(entry_plan),
        "entry": ref, "entry_from": ref_from, "stop": stop, "target": target, "tick_size": tick,
        "tick_from": tick_from, "snapped": snapped, "cas": bool(plan.get("cas", True)),
        "risk_per_share": risk, "reward_risk": round(reward_p / float(risk_p), 2),
        "stop_label": plan.get("stop_label", ""), "target_label": plan.get("target_label", ""),
        "budget_inr": budget, "cash_buffer_pct": buf_pct, "cash_buffer": cash_buffer, "cash_available": available,
        "mis_leverage_x": lev, "max_loss_per_trade_inr": max_loss, "limits": limits, "qty": qty,
        "bound_by": bound[0], "cost": cost, "max_loss_inr": loss, "flat_by": plan.get("flat_by") or c.get("flat_by"),
        "order_style": style, "order_type": "LIMIT" if limit is not None else "MARKET", "refusal": refusal,
    }


def kite_order(s):
    """The one proposed order: a Kite place_order body (plus stop_leg for regular style), why and plan."""
    if s["refusal"] or s["qty"] <= 0:
        return None
    qty = int(s["qty"])
    tag = order_tag(s["setup"])
    body = {"variety": "co" if s["order_style"] == "co" else "regular", "exchange": s["exchange"],
            "tradingsymbol": s["symbol"], "transaction_type": "BUY", "order_type": s["order_type"],
            "quantity": qty, "product": "MIS"}
    if s["order_type"] == "LIMIT":
        body["price"] = s["entry"]
    if s["order_style"] == "co":
        body["trigger_price"] = s["stop"]
        stop_leg = None
    else:
        stop_leg = {"variety": "regular", "exchange": s["exchange"], "tradingsymbol": s["symbol"],
                    "transaction_type": "SELL", "order_type": "SL-M", "quantity": qty, "product": "MIS",
                    "trigger_price": s["stop"], "validity": "DAY", "tag": tag}
    body.update({"validity": "DAY", "tag": tag})
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "day trade")
    bound_words = {"max loss": "%s max loss per trade" % money(s["max_loss_per_trade_inr"] or 0),
                   "position cap": "position cap", "max order": "max order", "cash": "cash after the buffer"}
    why = ("%s on %s: %d shares, bound by your %s (%d x %s = %s at the stop)"
           % (setup, s["symbol"], qty, bound_words[s["bound_by"]], qty, _price(s["risk_per_share"]),
              money(s["max_loss_inr"])))
    return {
        "symbol": s["symbol"], "exchange": s["exchange"], "side": "buy", "qty": qty,
        "product": "MIS", "order_style": s["order_style"],
        "kite": body,
        "stop_leg": stop_leg,
        "stop": s["stop"], "target": s["target"],
        "why": why,
        "plan": {"entry": s["entry"], "stop": s["stop"], "target": s["target"],
                 "risk_per_share": s["risk_per_share"], "max_loss_inr": s["max_loss_inr"], "setup": s["setup"],
                 "entry_time": s["entry_time"], "flat_by": s["flat_by"]},
    }


def plan_report(s, order, notes=()):
    lines = []
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "setup")
    lines.append("%s buy, %s, entry %s%s" % (s["symbol"], setup, _price(s["entry_plan"]),
                                            (" at " + s["entry_time"]) if s["entry_time"] else ""))
    lines.append("Stop %s%s. Target %s%s." % (_price(s["stop"]), (": " + s["stop_label"]) if s["stop_label"] else "",
                                             _price(s["target"]),
                                             (": " + s["target_label"]) if s["target_label"] else ""))
    for n in notes:
        lines.append(n)
    if s["snapped"]:
        lines.append("The %s rounded down to the %s tick." % (" and ".join(s["snapped"]), _price(s["tick_size"])))
    src = {"plan": "the plan's entry", "price": "the latest price, above the plan's entry",
           "limit": "your limit price"}[s["entry_from"]]
    lines.append("Sized at %s (%s). Risk %s a share (entry %s minus stop %s). Reward:risk %.2f."
                 % (_price(s["entry"]), src, _price(s["risk_per_share"]), _price(s["entry"]), _price(s["stop"]),
                    s["reward_risk"]))
    lines.append("")
    lines.append("Limits (whole shares; the smallest one wins):")
    for l in s["limits"]:
        lines.append("  %-13s %-70s %3d shares%s" % (l["name"], l["how"], l["shares"],
                                                      "  <- binds" if l["binds"] else ""))
    lines.append("")
    if order is None:
        lines.append("No order: " + s["refusal"])
        lines.append("Education and tooling, not financial advice.")
        return "\n".join(lines)
    lines.append("Size: %d shares of %s = %s at %s. Max loss at the stop: %d x %s = %s."
                 % (s["qty"], s["symbol"], money(s["cost"]), _price(s["entry"]), s["qty"],
                    _price(s["risk_per_share"]), money(s["max_loss_inr"])))
    lines.append("A stop is an order, not a promise: in a fast market it can fill below %s." % _price(s["stop"]))
    lines.append("")
    k = order["kite"]
    how = "%s %s" % (k["order_type"], _price(k["price"])) if k["order_type"] == "LIMIT" else "MARKET"
    if order["order_style"] == "co":
        lines.append("Proposed cover order (Kite place_order body, nothing has been placed):")
        lines.append("  BUY %d %s, %s MIS, variety co, %s, trigger (stop) %s, DAY, tag %s"
                     % (k["quantity"], k["tradingsymbol"], k["exchange"], how, _price(k["trigger_price"]), k["tag"]))
    else:
        sl = order["stop_leg"]
        lines.append("Proposed orders (Kite place_order bodies, nothing has been placed):")
        lines.append("  entry: BUY %d %s, %s MIS, variety regular, %s, DAY, tag %s"
                     % (k["quantity"], k["tradingsymbol"], k["exchange"], how, k["tag"]))
        lines.append("  stop:  SELL %d %s, %s MIS, SL-M trigger %s, DAY (placed right after the entry fills)"
                     % (sl["quantity"], sl["tradingsymbol"], sl["exchange"], _price(sl["trigger_price"])))
    lines.append("The target %s is not an order: trader-alerts rings at it." % _price(s["target"]))
    if s["flat_by"]:
        lines.append("Flat by %s IST: anything still open then gets closed (trader-alerts rings, "
                     "trader-broker flatten). Zerodha squares off MIS itself at %s%s, Rs 50 + GST each time."
                     % (s["flat_by"], nse.MIS_SQUARE_OFF_CAS.strftime("%H:%M"),
                        " for a CAS stock like this one" if s["cas"] else " for CAS stocks"))
    lines.append("")
    lines.append("Next: trader-risk checks it, then trader-broker asks you yes or no.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------- orders file

def write_orders(path, orders, cfg, source="", now=None):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    doc = {
        "created_by": "trader-budget",
        "created_at": iso(now or nse.now_ist()),
        "status": "proposed",
        "paper": bool(cfg.get("paper", True)),
        "note": ("Proposals only. Nothing is placed until trader-risk passes each order "
                 "and you say yes to it in the chat."),
        "source": source,
        "orders": orders,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- main

def _price_note(plan, sym, ppath, now, notes):
    """A fresh price above the plan's entry raises the risk; an old one is reported and ignored."""
    pdoc = read_json(ppath, "prices")
    p, t, fresh = fresh_price(pdoc, sym, now)
    if p is not None and fresh:
        if round_paise(p) > round_paise(_f(plan.get("entry")) or 0):
            notes.append("%s last %s (as of %s IST) is above the plan's entry %s, so the risk is bigger: "
                         "sized at %s." % (sym, _price(p), t.strftime("%H:%M"), _price(plan.get("entry")), _price(p)))
        return p
    if p is not None:
        notes.append("%s in %s is %s: not used (older than %d minutes). Sized at the plan's entry."
                     % (sym, ppath, ("from " + t.strftime("%Y-%m-%d %H:%M") + " IST") if t else "undated",
                        FRESH_MINUTES))
    return None


def _cash_and_row(a, sym, notes):
    """Cash (portfolio.json, else the live Kite balance) and the instrument row (CSV, else the session)."""
    cash, row = None, None
    if a.portfolio and os.path.exists(a.portfolio):
        pf = read_json(a.portfolio, "portfolio")
        cash = _f(pf.get("cash_inr")) if isinstance(pf, dict) else None
        if cash is None:
            raise SizeError("%s needs a \"cash_inr\" number" % a.portfolio)
    if a.offline:
        return cash, instrument_row(sym, a.offline)
    k, why = kite_session()
    if k is None:
        notes.append("No Kite session (%s): tick size from the plan, cash from the config." % why)
        return cash, None
    row = instrument_row(sym, None, k)
    if cash is None:
        try:
            cash = live_cash(k)
            notes.append("Cash %s is your live Kite balance (no trader/portfolio.json)." % money(cash))
        except kite.KiteError as e:
            notes.append("Could not read your Kite balance (%s): sized to the budget." % e)
    return cash, row


def run_plan(a, cfg):
    now = parse_now(a.now)
    today = now.date().isoformat()
    plan = read_json(a.plan, "plan (run trader-exit first)")
    if not isinstance(plan, dict):
        raise SizeError("the plan is not a JSON object")
    limit = None
    if a.limit is not None:
        if a.limit == "plan":
            limit = _f(plan.get("entry"))
        else:
            limit = _f(a.limit)
            if limit is None or limit <= 0:
                raise SizeError("--limit wants a price, like --limit 1174.00")
    sym = kite.normalize_symbol(str(plan.get("symbol") or ""))
    notes, price = [], None
    ppath = a.prices or ("trader/prices.json" if os.path.exists("trader/prices.json") else None)
    if ppath and limit is None and sym:
        price = _price_note(plan, sym, ppath, now, notes)
    cash, row = _cash_and_row(a, sym, notes) if sym else (None, None)
    s = size_trade(plan, cfg, a.budget, limit, price, cash, today, row)
    order = kite_order(s)
    if order and a.write:
        src = "trader/plan.json (%s %s %s)" % (s["symbol"], s["setup"] or "plan", s["date"] or today)
        write_orders(a.write, [order], cfg, src, now)
    if a.json:
        print(json.dumps({"sizing": s, "orders": [order] if order else [], "notes": notes}, indent=2))
    else:
        print(plan_report(s, order, notes))
        if order and a.write:
            print("Wrote 1 proposed %s order to %s" % ("cover" if order["order_style"] == "co" else "MIS",
                                                        a.write))
    return 0 if order else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Size a day trade to your budget and max loss (trader-budget).")
    ap.add_argument("--plan", default="trader/plan.json", help="the trade plan (default trader/plan.json)")
    ap.add_argument("--config", default="trader/config.json", help="rules (default trader/config.json)")
    ap.add_argument("--budget", type=float, help="override budget_inr in the config")
    ap.add_argument("--limit", nargs="?", const="plan", metavar="PRICE",
                    help="a LIMIT entry instead of MARKET, at the plan's entry or at PRICE")
    ap.add_argument("--prices", help="prices JSON (default trader/prices.json if it exists)")
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="your account; its cash_inr caps the size if it exists")
    ap.add_argument("--offline", metavar="PATH",
                    help="a saved Kite instrument CSV for the tick size, instead of today's session")
    ap.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-10-29 09:45\"")
    ap.add_argument("--write", nargs="?", const="trader/orders.json", metavar="PATH",
                    help="write the proposed order (default trader/orders.json)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of the report")
    a = ap.parse_args(argv)
    try:
        cfg = load_config(a.config)
        return run_plan(a, cfg)
    except SizeError as e:
        print("size.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
