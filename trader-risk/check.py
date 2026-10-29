#!/usr/bin/env python3
"""Pre-trade risk check for trader-risk. Standard library only.

Reads the proposed orders (trader/orders.json) and checks every one against
your rules (trader/config.json), your account (trader/portfolio.json), prices
(trader/prices.json), the plan (trader/plan.json, for the tick size), what
was placed today (trader/log.jsonl) and what filled (trader/fills.json).
Each order gets PASS or BLOCK with the reasons. Orders are checked in file
order as if each PASS had filled, so buys that are fine one by one but break
a limit together are caught.

    python3 check.py                                 # the trader/ files, writes trader/verdicts.json
    python3 check.py --now "2026-10-29 09:45"        # pin the clock (IST)
    python3 check.py --orders trader/orders.json --json
    python3 check.py --no-write                      # check without recording the verdicts
    python3 check.py --offline instruments.csv       # tick sizes from a saved Kite instrument CSV

Entries and exits. An ENTRY opens a position: a buy, or a sell of something
you do not hold (a short). An EXIT is a sell of an open position in
trader/portfolio.json. Getting out must always be possible: an exit is
checked for format, exchange, product and short sales only; every other rule
turns into a warning for it, and an exit after flat_by is fine.

What blocks an order (every order):

  format        no kite body, or one that is not a Kite place_order body:
                missing fields, a quantity that is not a whole number, a
                symbol or side that disagrees with the order, a LIMIT with no
                price, validity not DAY, a bad tag, a broken stop_leg
  product       anything but MIS (CNC and NRML are out of this pack)
  exchange      BSE unless "allow_bse": true; NFO, BFO, MCX, CDS never
  asset         a futures or options contract unless "allow_fno": true; a
                symbol that is not in Kite's NSE equity list (with a session)
  short         a sell of what you do not hold, unless "allow_short": true

What blocks an entry (on top):

  blocked       a ticker on your "blocked" list
  size          under min_order_inr, over max_order_inr
  position      the stock would be over max_position_pct of the account
  cash          the buy would dip into your cash buffer (times mis_leverage_x
                with "use_mis_leverage": true)
  stop          no stop at the broker while "require_stop": true (a cover
                order's trigger_price or a stop_leg); a stop not below the
                entry and the latest price; more than 10 percent under the
                entry (Zerodha's cover-order range); not on the tick
  loss          qty x (entry minus stop) over max_loss_per_trade_inr. The entry
                is the limit price for a LIMIT order, else the HIGHER of the
                plan's entry and a fresh price (under 15 minutes old), so a
                stale plan cannot understate the risk
  reward:risk   (target minus entry) / (entry minus stop) under min_reward_risk
  daily loss    the max losses of today's placed entries plus this one over
                max_daily_loss_inr (win or lose, each counts in full)
  trades        max_trades_per_day entries already placed today
  window        before no_entries_before, after no_entries_after or past
                flat_by (IST), or the market is not open (weekend, NSE
                holiday, pre-open, after 15:15 for a CAS stock, 15:30 otherwise)
  stale         the orders file was written on another day
  duplicate     the same order twice in this batch, or the same order already
                placed or filled today (trader/log.jsonl, trader/fills.json)
  daily limit   max_orders_per_day orders placed today

Today's orders come from trader/log.jsonl: the latest line per order hash,
counted when its status is placed or filled (approved, denied or failed
lines do not count).

Exit code 0 when every order passes, 1 when any order is blocked.
The verdicts go to trader/verdicts.json keyed by the order hash (the same
function trader-broker uses, over the Kite body and the stop leg), so the
broker only shows an order as READY if THIS exact order passed today.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trader-kite"))
import kite   # noqa: E402
import nse    # noqa: E402

DEFAULTS = {
    "budget_inr": None, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_inr": 75000,
    "min_order_inr": 500, "max_loss_per_trade_inr": 500, "max_daily_loss_inr": 1500, "max_trades_per_day": 3,
    "max_orders_per_day": 10, "min_reward_risk": 1.0, "require_stop": True,
    "no_entries_before": "09:30", "no_entries_after": "14:30", "flat_by": "15:05",
    "exchange": "NSE", "product": "MIS", "order_style": "co", "use_mis_leverage": False, "mis_leverage_x": 1.0,
    "broker": "zerodha", "paper": True, "blocked": [], "allow_bse": False, "allow_fno": False, "allow_short": False,
}
BODY_FIELDS = ("variety", "exchange", "tradingsymbol", "transaction_type", "order_type", "quantity", "product",
               "validity")
ORDER_TYPES = {"MARKET", "LIMIT", "SL", "SL-M"}
VARIETIES = {"co", "regular"}
EQUITY_EXCHANGES = {"NSE", "BSE"}
FNO_EXCHANGES = {"NFO", "BFO"}
FNO_SYMBOL = re.compile(r"(FUT|\d+(CE|PE))$")
TAG = re.compile(r"^[A-Za-z0-9]{1,20}$")
PAISA = 0.005
FRESH_MINUTES = 15
DEFAULT_TICK = 0.05
CO_RANGE_PCT = 10.0
REF_WORDS = {"plan": "the plan's entry", "price": "the latest price", "limit": "the limit price",
             "stale price": "an old price"}
COUNTED = {"placed", "filled"}


class RiskError(Exception):
    pass


# ---------------------------------------------------------------- the hash (FILES.md, shared with trader-broker)

def order_hash(order):
    body = json.dumps({"kite": order["kite"], "stop_leg": order.get("stop_leg")}, sort_keys=True,
                      separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()[:12]


# ---------------------------------------------------------------- time (IST)

def parse_now(text):
    """"2026-10-29 09:45" (IST) or ISO with an offset -> aware IST datetime."""
    if not text:
        return nse.now_ist().replace(microsecond=0)
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        raise RiskError("--now wants a time like \"2026-10-29 09:45\" (IST)")
    return nse.to_ist(dt)


def parse_time(text):
    """An ISO timestamp (+05:30, or naive = IST) -> aware IST datetime, or None."""
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return nse.to_ist(dt)


def iso(dt):
    return dt.astimezone(nse.IST).replace(microsecond=0).isoformat()


def hhmm(value, key):
    """"09:30" -> time(9, 30); None or "" -> None (that bound is off)."""
    if value in (None, "", False):
        return None
    try:
        return nse.parse_hhmm(value)
    except ValueError:
        raise RiskError("%s must be an IST time like \"09:30\", got %r" % (key, value))


def window_check(now, c, cas=True):
    """Reasons an entry cannot be placed now (IST), and warnings. Empty when the window is open."""
    before = hhmm(c.get("no_entries_before"), "no_entries_before")
    after = hhmm(c.get("no_entries_after"), "no_entries_after")
    flat = hhmm(c.get("flat_by"), "flat_by")
    warns = []
    d, t = now.date(), now.time().replace(second=0, microsecond=0)
    stamp = now.strftime("%a %Y-%m-%d %H:%M")
    ok, why = nse.trading_day(d)
    if not ok:
        return ["window: the market is closed today (%s, %s IST)" % (why, stamp)], warns
    if why:
        warns.append(why)
    ph = nse.phase(now, cas)
    if ph != "open":
        end = nse.continuous_end(cas).strftime("%H:%M")
        return ["window: the market is %s (%s IST; continuous trading is %s to %s%s)"
                % ("in pre-open" if ph == "pre-open" else "closed", stamp, nse.OPEN.strftime("%H:%M"), end,
                   " for a CAS stock" if cas else "")], warns
    if before is not None and t < before:
        return ["window: no new entries before %s IST (it is %s)" % (before.strftime("%H:%M"),
                                                                     t.strftime("%H:%M"))], warns
    if after is not None and t > after:
        return ["window: no new entries after %s IST (it is %s)" % (after.strftime("%H:%M"),
                                                                    t.strftime("%H:%M"))], warns
    if flat is not None and t >= flat:
        return ["window: it is past your flat-by time %s IST (it is %s): no new entries"
                % (flat.strftime("%H:%M"), t.strftime("%H:%M"))], warns
    return [], warns


# ---------------------------------------------------------------- files and helpers

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise RiskError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise RiskError("%s is not valid JSON: %s" % (path, e))


def read_log(path):
    """trader/log.jsonl -> (records, warnings). A bad line is reported, not fatal."""
    if not path or not os.path.exists(path):
        return [], []
    out, warns = [], []
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                warns.append("%s line %d is not JSON: skipped" % (path, n))
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out, warns


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def money(x):
    x = float(x)
    if abs(x - round(x)) < PAISA:
        return "Rs " + "{:,.0f}".format(round(x))
    return "Rs " + "{:,.2f}".format(x)


def _paise(x):
    """A price to the paisa, half up."""
    return int(float(x) * 100 + 0.5 + 1e-9) / 100.0


def _p(x):
    return "%.2f" % float(x)


def _pct(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.2f" % x)


def _num(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.4f" % x).rstrip("0")


def on_tick(x, tick):
    q = float(x) / tick
    return abs(q - round(q)) < 1e-6


def account(cfg, portfolio, prices):
    """Starting account: cash and position values. No portfolio = a fresh
    account funded with the budget."""
    warnings = []
    if portfolio is None:
        budget = _f(cfg.get("budget_inr"))
        if not budget:
            raise RiskError("no trader/portfolio.json and no budget_inr in the config: "
                            "nothing to check the orders against")
        return {"cash": budget, "shares": {}, "values": {}}, warnings
    cash = _f(portfolio.get("cash_inr"))
    if cash is None:
        raise RiskError("portfolio.json needs a \"cash_inr\" number")
    positions = portfolio.get("positions")
    positions = {} if positions is None else positions
    if not isinstance(positions, dict):
        raise RiskError("portfolio.json positions must be an object keyed by symbol")
    shares, values = {}, {}
    for sym, p in positions.items():
        sym = kite.normalize_symbol(str(sym))
        n = _f((p or {}).get("qty")) or 0.0
        if n == 0:
            continue
        shares[sym] = shares.get(sym, 0.0) + n
        if prices.get(sym):
            values[sym] = shares[sym] * prices[sym]
        else:
            avg = _f((p or {}).get("avg")) or 0.0
            values[sym] = shares[sym] * avg
            warnings.append("no price for %s: valued at its average cost %s" % (sym, money(values[sym])))
    return {"cash": cash, "shares": shares, "values": values}, warnings


def rec_day(rec):
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


def rec_is_entry(rec):
    """trader-broker logs the kind trader-risk gave; without it, a buy is an entry."""
    k = rec.get("kind")
    if k:
        return k == "entry"
    return str(rec.get("side", "")).lower() == "buy"


def placed_today(log, today):
    """Today's orders that went to the broker (trader/log.jsonl): the LATEST line per order hash,
    kept when its status is placed or filled. An approved-but-never-placed, denied or failed
    order does not count. Lines without a hash count one each."""
    latest, order = {}, []
    for n, rec in enumerate(log or []):
        if not isinstance(rec, dict) or rec_day(rec) != today:
            continue
        key = str(rec.get("order_hash") or "line-%d" % n)
        if key not in latest:
            order.append(key)
        latest[key] = rec
    return [latest[k] for k in order if str(latest[k].get("status") or "").lower() in COUNTED]


def filled_today(fills, today):
    """Order hashes with a fill today (trader/fills.json)."""
    out = set()
    for f in fills or []:
        if isinstance(f, dict) and str(f.get("at", ""))[:10] == today and f.get("order_hash"):
            out.add(str(f["order_hash"]))
    return out


# ---------------------------------------------------------------- format

def order_format(o, sym, side):
    """Format reasons for one order, plus the parsed quantity and whether it is a cover-order exit."""
    kb = o.get("kite")
    if not isinstance(kb, dict) or not kb:
        return ["format: no kite body (the Kite place_order fields); run trader-budget again"], None, False
    reasons = []
    if side not in ("buy", "sell"):
        reasons.append("format: side must be buy or sell, got %r" % o.get("side"))
    if "parent_order_id" in kb:
        if kb.get("variety") != "co" or not kb.get("order_id"):
            reasons.append("format: an exit of a cover order needs variety co, order_id and parent_order_id")
        q = _f(o.get("qty"))
        if q is not None and (q <= 0 or abs(q - round(q)) > 1e-9):
            reasons.append("format: qty must be a whole number of shares, got %r" % o.get("qty"))
        return reasons, q, True
    for k in BODY_FIELDS:
        if kb.get(k) in (None, ""):
            reasons.append("format: the kite body has no %s" % k)
    if kb.get("tradingsymbol") and kite.normalize_symbol(str(kb["tradingsymbol"])) != sym:
        reasons.append("format: the order says %s but the kite body says %s" % (sym, kb["tradingsymbol"]))
    tt = str(kb.get("transaction_type") or "").upper()
    if tt and side in ("buy", "sell") and tt != side.upper():
        reasons.append("format: the order says %s but the kite body says %s" % (side, tt))
    q = _f(kb.get("quantity"))
    if q is None or q <= 0 or abs(q - round(q)) > 1e-9:
        reasons.append("format: quantity must be a whole number of shares, got %r" % kb.get("quantity"))
        q = None
    elif o.get("qty") is not None and _f(o.get("qty")) != q:
        reasons.append("format: the order says qty %s but the kite body says %s" % (o.get("qty"), kb["quantity"]))
    variety = str(kb.get("variety") or "")
    if variety and variety not in VARIETIES:
        reasons.append("format: variety must be co or regular, got %r (no amo, iceberg or auction orders here)"
                       % variety)
    ot = str(kb.get("order_type") or "").upper()
    if ot and ot not in ORDER_TYPES:
        reasons.append("format: unknown order_type %r" % kb.get("order_type"))
    if ot == "LIMIT" and not (_f(kb.get("price")) or 0) > 0:
        reasons.append("format: a LIMIT order needs a positive price")
    if ot in ("SL", "SL-M") and not (_f(kb.get("trigger_price")) or 0) > 0:
        reasons.append("format: a %s order needs a trigger_price" % ot)
    if kb.get("validity") and str(kb["validity"]).upper() != "DAY":
        reasons.append("format: validity must be DAY for an intraday order, got %r" % kb["validity"])
    if kb.get("tag") is not None and not TAG.match(str(kb["tag"])):
        reasons.append("format: tag must be letters and digits, at most 20, got %r" % kb["tag"])
    leg = o.get("stop_leg")
    if leg is not None:
        reasons.extend(stop_leg_format(leg, sym, q))
    return reasons, q, False


def stop_leg_format(leg, sym, q):
    if not isinstance(leg, dict):
        return ["format: stop_leg must be a Kite place_order body or null"]
    reasons = []
    if str(leg.get("transaction_type") or "").upper() != "SELL" or str(leg.get("order_type") or "").upper() != "SL-M":
        reasons.append("format: the stop_leg must be a SELL SL-M order")
    if str(leg.get("product") or "").upper() != "MIS":
        reasons.append("format: the stop_leg must be MIS")
    if not (_f(leg.get("trigger_price")) or 0) > 0:
        reasons.append("format: the stop_leg needs a trigger_price")
    if q is not None and _f(leg.get("quantity")) != q:
        reasons.append("format: the stop_leg quantity %r is not the entry's %s" % (leg.get("quantity"), _num(q)))
    if leg.get("tradingsymbol") and kite.normalize_symbol(str(leg["tradingsymbol"])) != sym:
        reasons.append("format: the stop_leg is for %s, not %s" % (leg["tradingsymbol"], sym))
    return reasons


def asset_check(o, kb, sym, c, rows):
    """Reasons and warnings about the product, the exchange and the instrument."""
    reasons, warns = [], []
    exch = str(kb.get("exchange") or o.get("exchange") or "").upper()
    product = str(kb.get("product") or o.get("product") or "").upper()
    fno = exch in FNO_EXCHANGES or bool(FNO_SYMBOL.search(sym)) or bool(
        FNO_SYMBOL.search(str(kb.get("tradingsymbol") or "").upper()))
    if product and product != "MIS":
        reasons.append("product: %s is not MIS; this pack trades intraday MIS only (no CNC, no NRML)" % product)
    if fno:
        if not c.get("allow_fno"):
            reasons.append("asset: %s is a futures or options contract; F&O is off (\"allow_fno\": false)" % sym)
        else:
            warns.append("F&O order: a contract can lose far more than a share")
    elif exch == "BSE":
        if not c.get("allow_bse"):
            reasons.append("exchange: BSE is off (\"allow_bse\": false); this pack trades NSE")
    elif exch and exch not in EQUITY_EXCHANGES:
        reasons.append("exchange: %s is not an equity exchange; this pack trades NSE (or BSE with allow_bse)" % exch)
    elif exch == "NSE" and rows is not None and sym and sym not in rows:
        reasons.append("asset: %s is not an NSE equity in Kite's instrument list (check the spelling)" % sym)
    return reasons, warns


# ---------------------------------------------------------------- the check

def check(orders, cfg, portfolio=None, prices=None, log=None, fills=None, plan=None, rows=None, cas=None,
          today=None, now=None, price_times=None, created_at=None):
    """Pure function. Returns {"results": [...], "pass": n, "block": n, "warnings": [...]}.

    now          aware IST datetime (default: the real clock); its date is "today"
    price_times  {SYM: aware time of that price}; None = every price counts as fresh
    log          records from trader/log.jsonl; fills from trader/fills.json
    plan         trader/plan.json (tick size and CAS flag for its symbol)
    rows         Kite's instrument table {SYM: row} (tick sizes), or None
    cas          the set of CAS symbols, or None (then the plan's flag, else CAS assumed)
    created_at   the orders file's created_at; another day's file blocks its entries"""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    prices = {kite.normalize_symbol(str(k)): float(v) for k, v in (prices or {}).items() if _f(v)}
    now = now or parse_now(None)
    today = today or now.date().isoformat()
    acct, top_warn = account(c, portfolio, prices)
    blocked = {str(b).upper() for b in c.get("blocked") or []}
    done_today = placed_today(log, today)
    count_today = len(done_today)
    entries_today = [r for r in done_today if rec_is_entry(r)]
    loss_today = 0.0
    for r in entries_today:
        ml = _f(r.get("max_loss_inr"))
        if ml is None:
            top_warn.append("an entry placed today (%s %s) has no max_loss_inr in the log; not counted in "
                            "the daily loss" % (r.get("side"), r.get("symbol")))
        else:
            loss_today += ml
    placed_hashes = {str(r.get("order_hash")) for r in done_today if r.get("order_hash")}
    filled = filled_today(fills, today) | placed_hashes
    lev = 1.0
    if c.get("use_mis_leverage"):
        lev = _f(c.get("mis_leverage_x")) or 1.0
        if lev < 1:
            raise RiskError("mis_leverage_x must be at least 1 when use_mis_leverage is true")
    plan_sym = kite.normalize_symbol(str((plan or {}).get("symbol") or ""))
    file_day = created_at.date().isoformat() if created_at else None
    seen = set()
    results = []
    batch_entries, batch_loss = 0, 0.0
    if not c.get("paper", True):
        top_warn.append("LIVE account (\"paper\": false): these orders use real money")

    def fresh(sym):
        if sym not in prices:
            return None
        if price_times is None:
            return prices[sym]
        t = price_times.get(sym)
        if t is None:
            return None
        age = (now - t).total_seconds()
        return prices[sym] if -60 <= age <= FRESH_MINUTES * 60 else None

    def tick_for(sym, warns):
        if rows is not None and sym in rows and _f(rows[sym].get("tick_size")):
            return float(rows[sym]["tick_size"])
        if plan and plan_sym == sym and _f(plan.get("tick_size")):
            return float(plan["tick_size"])
        warns.append("tick size for %s unknown (no Kite session, not in the plan): %s assumed" % (sym, _p(DEFAULT_TICK)))
        return DEFAULT_TICK

    def cas_for(sym):
        if cas is not None:
            return sym in cas
        if plan and plan_sym == sym and "cas" in plan:
            return bool(plan["cas"])
        return True

    for i, o in enumerate(orders, 1):
        reasons, warns, info = [], [], []
        later = []                       # entry-only reasons, turned into warnings for an exit
        if not isinstance(o, dict):
            results.append(_result(i, "", "", "entry", None, None, "", None, None, None, None, None,
                                   "malformed-%d" % i, ["format: order #%d is not a JSON object" % i], [], []))
            continue
        sym = kite.normalize_symbol(str(o.get("symbol") or ""))
        side = str(o.get("side", "")).strip().lower()
        kb = o.get("kite") if isinstance(o.get("kite"), dict) else {}
        plan_block = o.get("plan") if isinstance(o.get("plan"), dict) else {}
        leg = o.get("stop_leg") if isinstance(o.get("stop_leg"), dict) else None
        ohash = order_hash(o) if kb else "malformed-%d" % i
        if not sym:
            reasons.append("format: no symbol")
        fmt, q, exit_co = order_format(o, sym, side)
        reasons.extend(fmt)
        a_reasons, a_warns = asset_check(o, kb, sym, c, rows)
        reasons.extend(a_reasons)
        warns.extend(a_warns)
        variety = str(kb.get("variety") or o.get("order_style") or "")
        ot = str(kb.get("order_type") or "").upper()

        # the reference price: what one share costs to get in
        plan_entry = _f(plan_block.get("entry"))
        live = fresh(sym)
        ref, ref_from = None, None
        if ot == "LIMIT" and _f(kb.get("price")):
            ref, ref_from = _paise(kb["price"]), "limit"
        if ref is None:
            cands = [(p, w) for p, w in ((plan_entry, "plan"), (live, "price")) if p]
            if cands:
                ref, ref_from = max(cands, key=lambda x: x[0])
                ref = _paise(ref)
                if live and ref_from == "price" and plan_entry and live > plan_entry + PAISA:
                    info.append("%s last %s is above the plan's entry %s: the risk is measured from %s"
                                % (sym, _p(ref), _p(plan_entry), _p(ref)))
            elif sym in prices:
                ref, ref_from = _paise(prices[sym]), "stale price"
        if plan_entry and live is None and sym in prices and price_times is not None and ot != "LIMIT":
            warns.append("the %s price in trader/prices.json is older than %d minutes: not used"
                         % (sym, FRESH_MINUTES))
        held = acct["shares"].get(sym, 0.0)
        held_value = acct["values"].get(sym, 0.0)
        if exit_co and q is None:
            q = held if held > 0 else None
        value = q * ref if (q and ref) else None

        # entry or exit
        exiting = side == "sell" and held > 1e-9 and q is not None and q <= held + 1e-9
        kind = "exit" if exiting else "entry"
        if kind == "entry" and value is None and q and not reasons:
            later.append("price: no price for %s to size a %s-share order; add it to trader/prices.json "
                         "(trader-alerts: alerts.py quote %s --write)" % (sym, _num(q), sym))
        if side == "sell" and kind == "entry" and sym and not c.get("allow_short"):
            reasons.append("short: selling %s you do not hold (%s shares in trader/portfolio.json); short selling "
                           "is off (\"allow_short\": false). If you bought it, record the fill first (trader-broker)"
                           % (sym, _num(held)))

        # blocked list and size
        if sym in blocked:
            later.append("blocked: %s is on your blocked list" % sym)
        if value is not None:
            if value < float(c.get("min_order_inr") or 0) - PAISA:
                later.append("size: %s is under your %s minimum order" % (money(value), money(c["min_order_inr"])))
            if c.get("max_order_inr") and value > float(c["max_order_inr"]) + PAISA:
                later.append("size: %s is over your %s max order" % (money(value), money(c["max_order_inr"])))

        # account effects, simulated in order
        cash = acct["cash"]
        total = cash + sum(acct["values"].values())
        buffer_inr = total * float(c.get("cash_buffer_pct") or 0) / 100.0
        if value is not None and side == "buy" and kind == "entry":
            room = max(cash - buffer_inr, 0.0) * lev
            if value > room + PAISA:
                later.append("cash: %s is more than the %s you can use (cash %s minus the %s%% buffer %s%s)"
                             % (money(value), money(room), money(cash), _pct(c["cash_buffer_pct"]), money(buffer_inr),
                                (" x %s MIS leverage" % _num(lev)) if lev > 1 else ""))
            pos_after = held_value + value
            limit = total * float(c.get("max_position_pct") or 100) / 100.0
            if total > 0 and pos_after > limit + PAISA:
                later.append("position: %s would be %s, %.1f%% of the %s account, over your %s%% cap"
                             % (sym, money(pos_after), 100.0 * pos_after / total, money(total),
                                _pct(c["max_position_pct"])))

        # day-trade checks: stop, loss, reward:risk, daily loss, trades, window
        max_loss = risk_ps = rr = stop = target = None
        if kind == "entry" and side in ("buy", "sell") and not exit_co:
            long = side == "buy"
            tick = tick_for(sym, warns) if sym else DEFAULT_TICK
            co_stop = _f(kb.get("trigger_price")) if variety == "co" else None
            leg_stop = _f(leg.get("trigger_price")) if leg else None
            broker_stop = co_stop or leg_stop
            plan_stop = _f(plan_block.get("stop"))
            stop = broker_stop or plan_stop
            target = _f(plan_block.get("target")) or _f(o.get("target"))
            if broker_stop and plan_stop and abs(broker_stop - plan_stop) > PAISA:
                warns.append("the broker's stop %s differs from the plan's stop %s: the broker's is the one "
                             "Zerodha uses" % (_p(broker_stop), _p(plan_stop)))
            if not broker_stop:
                if c.get("require_stop", True):
                    later.append("stop: no stop at the broker. Every entry needs one (\"require_stop\": true): a "
                                 "cover order's trigger_price or a stop_leg, from trader-exit and trader-budget")
                elif stop:
                    warns.append("the stop %s is in the plan only, not at the broker: set a stop alert "
                                 "(trader-alerts: alerts.py add --from-plan)" % _p(stop))
                else:
                    warns.append("no stop: the max loss is not capped, so it is not counted in the daily loss")
            if stop and ref:
                if (long and stop >= ref - PAISA) or (not long and stop <= ref + PAISA):
                    later.append("stop: the stop %s must be %s %s %s for a %s"
                                 % (_p(stop), "below" if long else "above", REF_WORDS.get(ref_from, ref_from),
                                    _p(ref), side))
                else:
                    if abs(ref - stop) > ref * CO_RANGE_PCT / 100.0 + PAISA:
                        later.append("stop: the stop %s is more than %d percent from the entry %s; Zerodha's cover "
                                     "order takes a trigger within %d percent"
                                     % (_p(stop), int(CO_RANGE_PCT), _p(ref), int(CO_RANGE_PCT)))
                    if not on_tick(stop, tick):
                        later.append("stop: the stop %s is not on %s's %s tick" % (_p(stop), sym, _p(tick)))
                    risk_p = int(round(abs(ref - stop) * 100))
                    risk_ps = risk_p / 100.0
                    shares = q if q is not None else 0
                    max_loss = round(shares * risk_ps, 2)
                    cap = _f(c.get("max_loss_per_trade_inr"))
                    if cap and max_loss > cap + PAISA:
                        later.append("loss: %s shares x %s risk a share = %s at the stop, over your %s max loss "
                                     "per trade" % (_num(shares), _p(risk_ps), money(max_loss), money(cap)))
                    min_rr = _f(c.get("min_reward_risk"))
                    if target:
                        if (long and target <= ref + PAISA) or (not long and target >= ref - PAISA):
                            later.append("target: the target %s must be %s the entry %s"
                                         % (_p(target), "above" if long else "below", _p(ref)))
                        else:
                            rr = round(int(round(abs(target - ref) * 100)) / float(risk_p), 2)
                            if min_rr and rr < min_rr - 1e-9:
                                later.append("reward:risk: %.2f (target %s, entry %s, stop %s) is under your "
                                             "minimum %s" % (rr, _p(target), _p(ref), _p(stop), _num(min_rr)))
                            if not on_tick(target, tick):
                                warns.append("the target %s is not on the %s tick" % (_p(target), _p(tick)))
                    elif min_rr:
                        later.append("reward:risk: no target to measure it against (min_reward_risk %s)"
                                     % _num(min_rr))
            daily = _f(c.get("max_daily_loss_inr"))
            if daily and max_loss is not None:
                used = loss_today + batch_loss
                if used + max_loss > daily + PAISA:
                    later.append("daily loss: %s already at risk today plus %s for this one = %s, over your %s "
                                 "daily loss limit" % (money(used), money(max_loss), money(used + max_loss),
                                                       money(daily)))
            cap_t = int(c.get("max_trades_per_day") or 0)
            if cap_t and len(entries_today) + batch_entries >= cap_t:
                later.append("trades: %d entries placed today plus %d passed earlier in this batch, your limit "
                             "is %d a day" % (len(entries_today), batch_entries, cap_t))
            win_reasons, win_warns = window_check(now, c, cas_for(sym))
            later.extend(win_reasons)
            warns.extend(win_warns)
            if file_day and file_day != today:
                later.append("stale: the orders file was written on %s, not today (%s); run trader-budget again"
                             % (file_day, today))
            if stop and ref and risk_ps:
                info.append("entry %s (%s), stop %s, target %s: risk %s a share"
                            % (_p(ref), REF_WORDS.get(ref_from, ref_from), _p(stop),
                               _p(target) if target else "none", _p(risk_ps)))
                info.append("max loss %s%s, reward:risk %s%s"
                            % (money(max_loss), (" (limit %s)" % money(c["max_loss_per_trade_inr"]))
                               if _f(c.get("max_loss_per_trade_inr")) else "",
                               ("%.2f" % rr) if rr is not None else "n/a",
                               (" (min %s)" % _num(c["min_reward_risk"])) if _f(c.get("min_reward_risk")) else ""))
            if daily or cap_t:
                info.append("today before this: %d of %s trades, %s of %s daily loss"
                            % (len(entries_today) + batch_entries, cap_t or "unlimited",
                               money(loss_today + batch_loss), money(daily) if daily else "no limit on"))

        # duplicates and the daily order limit
        if ohash in seen:
            later.append("duplicate: the same order twice in this batch")
        if ohash in filled:
            later.append("duplicate: this exact order was already placed today (trader/log.jsonl, trader/fills.json)")
        passed_so_far = sum(1 for r in results if r["verdict"] == "PASS")
        cap_n = int(c.get("max_orders_per_day") or 0)
        if cap_n and count_today + passed_so_far >= cap_n:
            later.append("daily limit: %d placed today plus %d passed earlier in this batch, "
                         "your limit is %d a day" % (count_today, passed_so_far, cap_n))
        seen.add(ohash)

        if kind == "entry":
            reasons.extend(later)
        else:
            warns.extend("exit, not blocked: " + x for x in later)

        verdict = "BLOCK" if reasons else "PASS"
        if verdict == "PASS":
            if kind == "entry" and side in ("buy", "sell"):
                batch_entries += 1
                batch_loss += max_loss or 0.0
            if side == "buy" and q is not None:
                if value is not None:
                    acct["cash"] -= value
                    acct["values"][sym] = held_value + value
                acct["shares"][sym] = held + q
            elif side == "sell" and q is not None:
                if value is not None:
                    acct["cash"] += value
                    acct["values"][sym] = max(held_value - value, 0.0)
                acct["shares"][sym] = held - q
        results.append(_result(i, sym, side, kind, q, value, ref_from, ref, stop, target, risk_ps, max_loss,
                               ohash, reasons, warns, info, rr))

    return {
        "date": today,
        "now_ist": now.strftime("%Y-%m-%d %H:%M"),
        "paper": bool(c.get("paper", True)),
        "results": results,
        "pass": sum(1 for r in results if r["verdict"] == "PASS"),
        "block": sum(1 for r in results if r["verdict"] == "BLOCK"),
        "warnings": top_warn,
        "cash_after": round(acct["cash"], 2),
        "today": {"orders_placed": count_today, "entries_placed": len(entries_today),
                  "loss_at_risk": round(loss_today, 2)},
    }


def _result(i, sym, side, kind, q, value, ref_from, ref, stop, target, risk_ps, max_loss, ohash, reasons, warns,
            info, rr=None):
    return {
        "index": i, "symbol": sym, "side": side, "kind": kind,
        "qty": int(q) if q is not None else None,
        "amount": ("%s shares" % _num(q)) if q is not None else "no quantity",
        "value_inr": round(value, 2) if value is not None else None,
        "entry_ref": round(ref, 2) if ref else None, "entry_ref_from": ref_from,
        "stop": stop, "target": target, "risk_per_share": risk_ps, "max_loss_inr": max_loss,
        "reward_risk": rr, "order_hash": ohash, "verdict": "BLOCK" if reasons else "PASS",
        "reasons": reasons, "warnings": warns, "info": info,
    }


def report(res):
    lines = []
    for w in res["warnings"]:
        lines.append("WARNING: " + w)
    if res["warnings"]:
        lines.append("")
    for r in res["results"]:
        lines.append("%-5s #%d %s %s %s, %s" % (r["verdict"], r["index"], r["side"], r["symbol"], r["amount"],
                                                r["kind"]))
        for x in r.get("info", []):
            lines.append("          " + x)
        for x in r["reasons"]:
            lines.append("        - " + x)
        for x in r["warnings"]:
            lines.append("        ~ " + x)
    lines.append("")
    lines.append("%d passed, %d blocked at %s IST. Cash after the passed orders: %s (%s account)."
                 % (res["pass"], res["block"], res["now_ist"], money(res["cash_after"]),
                    "paper" if res["paper"] else "LIVE"))
    if res["block"]:
        lines.append("Blocked orders do not go to the broker. Fix the order or your rules, then check again.")
    else:
        lines.append("Every order passed. trader-broker still asks you yes or no for each one.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


def load_prices(pdoc):
    """prices.json -> ({SYM: price}, {SYM: aware IST time or None}).

    The contract's shape is prices[SYM] = {"last", "time", "prev_close", "source"};
    a bare number is taken as the price with the file's as_of as its time."""
    if not isinstance(pdoc, dict):
        return {}, {}
    prices = pdoc.get("prices", pdoc)
    if not isinstance(prices, dict):
        return {}, {}
    out, times = {}, {}
    as_of = parse_time(pdoc.get("as_of"))
    for s, p in prices.items():
        s = kite.normalize_symbol(str(s))
        if isinstance(p, dict):
            last, t = _f(p.get("last")), parse_time(p.get("time")) or as_of
        else:
            last, t = _f(p), as_of
        if last is None:
            continue
        out[s] = last
        times[s] = t
    return out, times


def load_instruments(offline):
    """(rows, cas set, warnings): a saved CSV with --offline, else today's Kite session, else nothing."""
    if offline:
        try:
            with open(offline, encoding="utf-8") as fh:
                return kite.parse_instruments(fh.read()), None, []
        except OSError as e:
            raise RiskError("cannot read %s: %s" % (offline, e))
    try:
        k = kite.Kite.connect()
    except kite.KiteError as e:
        return None, None, ["no Kite session (%s): tick sizes from the plan, %s if unknown"
                            % (e, _p(DEFAULT_TICK))]
    try:
        return k.instruments("NSE"), k.cas_symbols(), []
    except kite.KiteError as e:
        return None, None, ["could not read Kite's instrument list (%s): tick sizes from the plan" % e]


def verdicts_doc(res, orders_file, now):
    """trader/verdicts.json: one entry per order under "verdicts", keyed by the order hash."""
    verdicts = {}
    for r in res["results"]:
        verdicts[r["order_hash"]] = {
            "verdict": r["verdict"], "reasons": r["reasons"], "warnings": r["warnings"],
            "kind": r["kind"], "symbol": r["symbol"], "side": r["side"], "qty": r["qty"],
            "max_loss_inr": r["max_loss_inr"], "value_inr": r["value_inr"],
        }
    return {"created_by": "trader-risk", "date": res["date"], "checked_at": iso(now),
            "orders_file": orders_file, "verdicts": verdicts}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check proposed orders against your limits (trader-risk).")
    ap.add_argument("--orders", default="trader/orders.json")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--plan", default="trader/plan.json", help="the plan (tick size and CAS flag)")
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="your account; if missing, a fresh account funded with budget_inr")
    ap.add_argument("--prices", default="trader/prices.json")
    ap.add_argument("--log", default="trader/log.jsonl", help="today's tickets and their status (trader-broker)")
    ap.add_argument("--fills", default="trader/fills.json", help="today's fills (trader-broker)")
    ap.add_argument("--offline", metavar="PATH",
                    help="a saved Kite instrument CSV for tick sizes, instead of today's session")
    ap.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-10-29 09:45\"")
    ap.add_argument("--today", help="YYYY-MM-DD (default: today in IST)")
    ap.add_argument("--out", default="trader/verdicts.json", help="where the verdicts are recorded")
    ap.add_argument("--no-write", action="store_true", help="do not record the verdicts")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        now = parse_now(a.now)
        if a.today and not re.match(r"^\d{4}-\d{2}-\d{2}$", a.today):
            raise RiskError("--today wants a date like 2026-10-29")
        doc = read_json(a.orders, "orders file (run trader-budget first)")
        orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
        if not isinstance(orders, list) or not orders:
            raise RiskError("no orders in %s" % a.orders)
        created = parse_time(doc.get("created_at")) if isinstance(doc, dict) else None
        cfg = read_json(a.config, "config", required=False) or {}
        plan = read_json(a.plan, "plan", required=False)
        portfolio = read_json(a.portfolio, "portfolio", required=False)
        pdoc = read_json(a.prices, "prices", required=False) or {}
        prices, times = load_prices(pdoc)
        log, log_warns = read_log(a.log)
        fills = read_json(a.fills, "fills", required=False) or []
        rows, cas, inst_warns = load_instruments(a.offline)
        res = check(orders, cfg, portfolio, prices, log, fills, plan, rows, cas, a.today, now, times, created)
        res["warnings"] = inst_warns + log_warns + res["warnings"]
        if pdoc.get("as_of"):
            res["prices_as_of"] = pdoc["as_of"]
        if not a.no_write:
            d = os.path.dirname(a.out)
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = a.out + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(verdicts_doc(res, a.orders, now), fh, indent=2)
                fh.write("\n")
            os.replace(tmp, a.out)
    except RiskError as e:
        print("check.py: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps(res, indent=2) if a.json else report(res))
    return 1 if res["block"] else 0


if __name__ == "__main__":
    sys.exit(main())
