#!/usr/bin/env python3
"""Price alerts and the NSE clock for trader-alerts. Standard library only.

    python3 alerts.py clock                          # IST now, NSE phase, next open, flat-by, holiday
    python3 alerts.py quote RELIANCE INFY --write    # Kite quotes -> trader/prices.json
    python3 alerts.py quote --holdings --write       # every symbol in your portfolio and plan
    python3 alerts.py add --from-plan                # stop, target and flat-by from trader/plan.json
    python3 alerts.py add --symbol RELIANCE --stop 1163
    python3 alerts.py add --symbol RELIANCE --target 1185.8
    python3 alerts.py add --symbol SBIN --pct 3
    python3 alerts.py add --time 14:30 --label "last entry window"
    python3 alerts.py add --bell
    python3 alerts.py add --flat-by                  # at the config's flat_by (default 15:05)
    python3 alerts.py check                          # rings what hit, once; exit 1 if nothing rang
    python3 alerts.py check --now "2026-10-29 15:05" --offline tests/fixtures/trader-alerts
    python3 alerts.py list
    python3 alerts.py reset reliance-plan-stop

Rule types (trader/alerts.json):
  above / target  the last price is at or above "price"
  below / stop    the last price is at or below "price"
  pct             the last price moved "pct" percent or more from the previous
                  close (Kite's ohlc.close, or "ref" if you give one);
                  "direction": "up" or "down"
  time            the IST clock passed "at" (HH:MM) on a trading day
  bell            NSE opened today (09:15; rings once per trading day)
  flat_by         the flat-by time passed (HH:MM, default the config's
                  flat_by, else 15:05): close what is still open before
                  Zerodha's 15:12 MIS square-off in F&O stocks

A rule with "date" is only watched on that date (rules from a plan are for
the plan's day only). A price alert rings ONCE and then stays quiet until you
reset it, unless the rule has "rearm": true (it re-arms when the price
crosses back). pct, time, bell and flat_by ring at most once per trading day.

Quotes come from Zerodha Kite Connect through trader-kite/kite.py (today's
session from trader-kite/login.py; the Connect plan's market data). One quote
call covers every symbol, at most one call a second. --offline DIR reads
DIR/kite-quote-<SYMBOL>.json instead (the tests' fixtures).

Hours and holidays come from trader-kite/nse.py only: 09:15 open, continuous
trading to 15:15 for F&O stocks (then the closing auction to 15:35) and 15:30
for the rest, Zerodha's MIS square-off at 15:12 in F&O stocks, NSE's 2026
holiday list. Times are IST; --now "2026-10-29 10:15" pins the clock.
"""

import argparse
import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trader-kite"))
import kite   # noqa: E402
import nse    # noqa: E402

QUOTE_FILE = "kite-quote-%s.json"
SOURCE = "kite"
SQUARE_OFF_CHARGE = "Rs 50 + GST"
PRICE_TYPES = {"above", "below", "stop", "target", "pct"}
CLOCK_TYPES = {"time", "bell", "flat_by"}
RULE_TYPES = PRICE_TYPES | CLOCK_TYPES


class AlertError(Exception):
    pass


# ---------------------------------------------------------------- time (IST)

def parse_now(text):
    """'2026-10-29 10:15' (IST) or an ISO time with an offset -> aware IST datetime."""
    if not text:
        return nse.now_ist().replace(microsecond=0)
    try:
        dt = datetime.datetime.fromisoformat(text.strip())
    except ValueError:
        raise AlertError("--now wants an IST time like \"2026-10-29 10:15\"")
    return nse.to_ist(dt).replace(microsecond=0)


def hhmm(text):
    """'15:05' -> time, or None if it is not a time."""
    try:
        return nse.parse_hhmm(text)
    except (ValueError, AttributeError, TypeError):
        return None


def _t(t):
    return t.strftime("%H:%M")


def flat_by_time(cfg=None, plan=None):
    for src in (plan or {}, cfg or {}):
        if src.get("flat_by"):
            t = hhmm(src["flat_by"])
            if t is None:
                raise AlertError("flat_by %r is not a time like 15:05" % src["flat_by"])
            return t
    return nse.DEFAULT_FLAT_BY


def market_status(now, flat_by=None):
    """The NSE clock at `now` (aware IST): phase, session times and the next open."""
    now = nse.to_ist(now)
    d = now.date()
    ok, reason = nse.trading_day(d)
    phase = nse.phase(now, True)
    st = {"now_ist": now.strftime("%Y-%m-%d %H:%M"), "date": d.isoformat(), "weekday": now.strftime("%A"),
          "trading_day": ok, "calendar_known": d <= nse.KNOWN_THROUGH, "phase": phase,
          "open": phase == "open" or (ok and phase == "after-close" and now.time() < nse.CLOSE),
          "reason": reason, "pre_open": _t(nse.PRE_OPEN), "opens": _t(nse.OPEN),
          "continuous_end_cas": _t(nse.CAS_CONTINUOUS_END), "continuous_end": _t(nse.CLOSE),
          "square_off_cas": _t(nse.MIS_SQUARE_OFF_CAS), "flat_by": _t(flat_by or nse.DEFAULT_FLAT_BY),
          "next_open": nse.next_open(now).strftime("%Y-%m-%d %H:%M")}
    if ok and not reason:
        if phase == "closed":
            st["reason"] = "before the pre-open" if now.time() < nse.PRE_OPEN else "closed"
        elif phase == "pre-open":
            st["reason"] = "pre-open session, no trading until 09:15"
        elif phase == "open":
            st["reason"] = "continuous trading"
        elif now.time() < nse.CLOSE:
            st["reason"] = "closing auction for F&O stocks; other stocks trade to 15:30"
        else:
            st["reason"] = "after the close"
    return st


# ---------------------------------------------------------------- quotes

def _connect():
    return kite.Kite.connect()


def kite_time(text, now):
    """Kite's '2026-10-29 15:59:57' (IST) -> '2026-10-29T15:59:57+05:30'."""
    if not text:
        return now.isoformat()
    try:
        return nse.to_ist(datetime.datetime.fromisoformat(str(text).replace(" ", "T"))).isoformat()
    except ValueError:
        return now.isoformat()


def parse_quote(sym, q, now):
    """One Kite quote dict -> the pack's quote: last, prev_close (Kite's ohlc.close), time."""
    if not isinstance(q, dict) or q.get("last_price") is None:
        raise AlertError("Kite has no quote for %s (not an NSE equity symbol?)" % sym)
    ohlc = q.get("ohlc") or {}
    return {"symbol": sym, "last": float(q["last_price"]), "prev_close": _num(ohlc.get("close")),
            "open": _num(ohlc.get("open")), "high": _num(ohlc.get("high")), "low": _num(ohlc.get("low")),
            "volume": q.get("volume"), "time": kite_time(q.get("last_trade_time") or q.get("timestamp"), now),
            "source": SOURCE}


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f else None


def _offline_quote(folder, sym):
    path = os.path.join(folder, QUOTE_FILE % sym)
    if not os.path.exists(path):
        raise AlertError("no offline quote file %s" % path)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def get_quotes(symbols, now, offline=None):
    """{SYMBOL: quote} and [errors]. One Kite call for every symbol (500 per request)."""
    syms = _dedupe([kite.normalize_symbol(s) for s in symbols])
    quotes, errors = {}, []
    if not syms:
        return quotes, errors
    if offline:
        raw = {}
        for s in syms:
            try:
                raw[s] = _offline_quote(offline, s)
            except AlertError as e:
                errors.append(str(e))
    else:
        raw = _connect().quotes(syms)
    for s in syms:
        if s not in raw:
            if offline:
                continue
            errors.append("Kite has no quote for %s (not an NSE equity symbol?)" % s)
            continue
        try:
            quotes[s] = parse_quote(s, raw[s], now)
        except AlertError as e:
            errors.append(str(e))
    return quotes, errors


def write_prices(path, quotes, now):
    """trader/prices.json per the pack's contract, merged with what was there."""
    old = _read(path, {}) if os.path.exists(path) else {}
    prices = dict((old or {}).get("prices") or {})
    for s, q in quotes.items():
        prices[s] = {"last": q["last"], "time": q["time"], "prev_close": q.get("prev_close"), "source": SOURCE}
    doc = {"as_of": now.isoformat(), "prices": prices}
    _write(path, doc)
    return doc


def _write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def _read(path, default):
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise AlertError("%s is not valid JSON: %s" % (path, e))


def rupees(v):
    return "Rs {:,.2f}".format(float(v))


# ---------------------------------------------------------------- rules

def validate(rule):
    t = rule.get("type")
    if t not in RULE_TYPES:
        raise AlertError("rule %s: unknown type %r (one of %s)" % (rule.get("id"), t, ", ".join(sorted(RULE_TYPES))))
    if t in PRICE_TYPES and not rule.get("symbol"):
        raise AlertError("rule %s needs a symbol" % rule.get("id"))
    if t in ("above", "below", "stop", "target") and not isinstance(rule.get("price"), (int, float)):
        raise AlertError("rule %s needs a numeric price" % rule.get("id"))
    if t == "pct" and not isinstance(rule.get("pct"), (int, float)):
        raise AlertError("rule %s needs a numeric pct" % rule.get("id"))
    if t in ("time", "flat_by") and hhmm(rule.get("at")) is None:
        raise AlertError("rule %s needs \"at\" as an IST time like \"15:05\"" % rule.get("id"))
    if rule.get("date") and not re.match(r"^\d{4}-\d{2}-\d{2}\Z", str(rule["date"])):
        raise AlertError("rule %s: date must be YYYY-MM-DD" % rule.get("id"))


def active(rule, today):
    """A rule with a "date" is watched on that date only."""
    return not rule.get("date") or str(rule["date"]) == today


def _rang_today(stamp, today):
    return bool(stamp) and str(stamp)[:10] == today


def rule_id(rule):
    return rule.get("id") or "%s-%s" % (str(rule.get("symbol") or "market").lower(), rule["type"])


def _exit_hint(sym):
    if not sym:
        return ""
    return (" If %s is still open, close it: trader-broker ticket.py exit %s builds the exit ticket, then "
            "trader-risk and your yes." % (sym, sym))


def evaluate(rules, quotes, status, state, now):
    """Pure function. Returns (rings, new_state). Each ring: {id, symbol, message}."""
    state = json.loads(json.dumps(state or {}))
    rung = state.setdefault("rung", {})
    today = status["date"]
    now = nse.to_ist(now)
    stamp = now.isoformat()
    rings = []
    for r in rules:
        validate(r)
        rid = rule_id(r)
        if not active(r, today):
            continue
        typ = {"stop": "below", "target": "above"}.get(r["type"], r["type"])
        sym = str(r.get("symbol") or "").upper()
        if typ == "bell":
            if status["phase"] == "open" and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": "", "message":
                              "Opening bell: NSE is open (%s IST). F&O stocks trade to %s, the rest to %s; flat by "
                              "%s." % (status["now_ist"], status["continuous_end_cas"], status["continuous_end"],
                                       status["flat_by"])})
                rung[rid] = stamp
            continue
        if typ in ("time", "flat_by"):
            at = hhmm(r.get("at"))
            if status["trading_day"] and now.time() >= at and not _rang_today(rung.get(rid), today):
                if typ == "flat_by":
                    msg = ("Flat by %s: it is %s IST. Zerodha squares off MIS in F&O stocks at %s (%s each time); "
                           "continuous trading ends %s for them, %s for the rest.%s"
                           % (_t(at), now.strftime("%H:%M"), status["square_off_cas"], SQUARE_OFF_CHARGE,
                              status["continuous_end_cas"], status["continuous_end"], _exit_hint(sym)))
                else:
                    label = r.get("label") or "time alert"
                    msg = "%s%s: it is %s IST (set for %s), NSE is %s.%s" % (
                        label[:1].upper(), label[1:], now.strftime("%H:%M"), _t(at), status["phase"], _exit_hint(sym))
                rings.append({"id": rid, "symbol": sym, "message": msg})
                rung[rid] = stamp
            continue
        q = quotes.get(sym)
        if not q:
            continue
        p = q["last"]
        if typ in ("above", "below"):
            hit = p >= r["price"] if typ == "above" else p <= r["price"]
            if hit and rid not in rung:
                word = "at or above" if typ == "above" else "at or below"
                label = {"stop": "stop", "target": "target"}.get(r["type"], "alert level")
                rings.append({"id": rid, "symbol": sym, "message": "%s %s %s: %s your %s %s%s."
                              % (sym, "is" if status["open"] else "last", rupees(p), word, label, rupees(r["price"]),
                                 (" (" + r["note"] + ")") if r.get("note") else "")})
                rung[rid] = stamp
            elif not hit and rid in rung and r.get("rearm"):
                del rung[rid]
        elif typ == "pct":
            ref = r.get("ref") or q.get("prev_close")
            if not ref:
                continue
            move = 100.0 * (p / float(ref) - 1)
            want = r.get("direction")
            hit = abs(move) >= r["pct"] and (not want or (want == "up") == (move > 0))
            if hit and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": sym, "message": "%s moved %+.2f%% to %s (from %s %s)."
                              % (sym, move, rupees(p), "your ref" if r.get("ref") else "the previous close",
                                 rupees(ref))})
                rung[rid] = stamp
    state["last_check"] = stamp
    return rings, state


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.2f" % x).rstrip("0").rstrip(".")


def rules_from_plan(plan, cfg_flat_by=None):
    """trader/plan.json -> a stop alert, a target alert and a flat-by alert, for the plan's day."""
    if not isinstance(plan, dict) or not plan.get("symbol"):
        raise AlertError("the plan has no symbol (run trader-exit first)")
    sym = kite.normalize_symbol(str(plan["symbol"]))
    out = []
    for key, word in (("stop", "plan stop"), ("target", "plan target")):
        v = plan.get(key)
        if not isinstance(v, (int, float)) or v <= 0:
            raise AlertError("the plan has no %s (run trader-exit)" % key)
        label = plan.get("%s_label" % key)
        out.append({"id": "%s-plan-%s" % (sym.lower(), key), "symbol": sym, "type": key, "price": float(v),
                    "note": word + (": " + label if label else "")})
    flat = flat_by_time({"flat_by": cfg_flat_by} if cfg_flat_by else None, plan)
    out.append({"id": "%s-flat-by" % sym.lower(), "symbol": sym, "type": "flat_by", "at": _t(flat),
                "label": "flat by"})
    if plan.get("date"):
        for r in out:
            r["date"] = str(plan["date"])
    return out


# ---------------------------------------------------------------- commands

def holdings(a):
    syms = []
    pf = _read(a.portfolio, {})
    positions = (pf or {}).get("positions") if isinstance(pf, dict) else None
    if isinstance(positions, dict):
        syms.extend(str(s).upper() for s in positions)
    elif isinstance(positions, list):
        syms.extend(str(p.get("symbol") or "").upper() for p in positions if isinstance(p, dict))
    plan = _read(a.plan, {})
    if isinstance(plan, dict) and plan.get("symbol"):
        syms.append(kite.normalize_symbol(str(plan["symbol"])))
    return [s for s in syms if s]


def _dedupe(items):
    out = []
    for s in items:
        if s and s not in out:
            out.append(s)
    return out


def cmd_clock(a):
    cfg = _read(a.config, {}) or {}
    now = parse_now(a.now)
    st = market_status(now, flat_by_time(cfg))
    if a.json:
        print(json.dumps(st, indent=2))
        return 0 if st["open"] else 1
    print("%s IST (%s): NSE %s, %s." % (st["now_ist"], st["weekday"], "OPEN" if st["open"] else st["phase"].upper(),
                                        st["reason"]))
    if st["trading_day"]:
        print("Today's session: pre-open %s, open %s, continuous trading to %s for F&O stocks (closing auction "
              "to 15:35) and %s for the rest." % (st["pre_open"], st["opens"], st["continuous_end_cas"],
                                                  st["continuous_end"]))
        print("Flat by %s. Zerodha squares off MIS positions in F&O stocks at %s (%s each time)."
              % (st["flat_by"], st["square_off_cas"], SQUARE_OFF_CHARGE))
        if flat_by_time(cfg) >= nse.MIS_SQUARE_OFF_CAS:
            print("Warning: your flat_by %s is not before Zerodha's %s square-off; set it earlier in "
                  "trader/config.json." % (st["flat_by"], st["square_off_cas"]))
    if not st["open"]:
        print("Next open: %s IST." % st["next_open"])
    if not st["calendar_known"]:
        print("NSE's holiday list for %s is not in the pack yet: check nseindia.com before trading."
              % st["date"][:4])
    return 0 if st["open"] else 1


def _print_quotes(syms, quotes, errors, a):
    for s in syms:
        q = quotes.get(s)
        if q:
            chg = ""
            if q.get("prev_close"):
                chg = " (%+.2f%% vs prev close %s)" % (100.0 * (q["last"] / q["prev_close"] - 1), rupees(q["prev_close"]))
            print("%-10s %s%s  %s  [%s]" % (s, rupees(q["last"]), chg, q.get("time", ""), q["source"]))
    for e in errors:
        print("error: " + e)
    if a.write and quotes:
        print("Wrote %d price(s) to %s." % (len(quotes), a.write))
    print("Kite quotes are your broker's own, fresh for 15 minutes in trader/prices.json. The price that fills "
          "is still whatever the market gives.")


def cmd_quote(a):
    syms = _dedupe([kite.normalize_symbol(s) for s in a.symbols] + (holdings(a) if a.holdings else []))
    if not syms:
        raise AlertError("which symbols? alerts.py quote RELIANCE INFY, or --holdings")
    now = parse_now(a.now)
    quotes, errors = get_quotes(syms, now, a.offline)
    if a.write and quotes:
        write_prices(a.write, quotes, now)
    if a.json:
        print(json.dumps({"as_of": now.isoformat(), "quotes": quotes, "errors": errors}, indent=2))
    else:
        _print_quotes(syms, quotes, errors, a)
    return 0 if quotes else 1


def cmd_check(a):
    doc = _read(a.rules, None)
    if not doc or not doc.get("rules"):
        raise AlertError("no rules in %s: add some with alerts.py add --from-plan" % a.rules)
    rules = doc["rules"]
    cfg = _read(a.config, {}) or {}
    now = parse_now(a.now)
    status = market_status(now, flat_by_time(cfg))
    watched = [r for r in rules if active(r, status["date"])]
    syms = _dedupe([kite.normalize_symbol(str(r.get("symbol") or "")) for r in watched
                    if r.get("type") in PRICE_TYPES and r.get("symbol")])
    quotes, errors = (get_quotes(syms, now, a.offline) if syms else ({}, []))
    state = _read(a.state, {})
    rings, state = evaluate(rules, quotes, status, state, now)
    if not a.no_write:
        _write(a.state, state)
        if quotes:
            write_prices(a.prices_out, quotes, now)
    if a.json:
        print(json.dumps({"market": status, "rings": rings, "errors": errors,
                          "quotes": {s: q["last"] for s, q in quotes.items()}}, indent=2))
        return 0 if rings else 1
    print("NSE %s (%s IST, %s)." % ("OPEN" if status["open"] else status["phase"].upper(), status["now_ist"],
                                    status["reason"]))
    for r in rings:
        print("ALERT " + r["message"])
    if not rings:
        print("Nothing hit. %d rule(s) watched today%s." % (
            len(watched), (", " + ", ".join("%s %s" % (s, rupees(quotes[s]["last"])) for s in syms if s in quotes))
            if quotes else ""))
    for e in errors:
        print("error: " + e)
    if rings:
        print("An alert is information, not an order. A cover order's stop leg already sits at the exchange: "
              "check get_positions and record any fill (ticket.py fill). To close a position yourself: "
              "ticket.py exit SYMBOL, then trader-risk and your yes.")
    return 0 if rings else 1


def _add_from_plan(a, doc):
    plan = _read(a.plan, None)
    if plan is None:
        raise AlertError("no plan at %s (run trader-exit first)" % a.plan)
    cfg = _read(a.config, {}) if a.config else {}
    new = rules_from_plan(plan, (cfg or {}).get("flat_by"))
    ids = {r["id"] for r in new}
    doc["rules"] = [r for r in doc["rules"] if r.get("id") not in ids] + new
    state = _read(a.state, {})
    if state.get("rung"):
        for i in ids:
            state["rung"].pop(i, None)
        _write(a.state, state)
    _write(a.rules, doc)
    for r in new:
        print("Added %s: %s" % (r["id"], json.dumps(r)))
    print("Run alerts.py check every few minutes while the trade is open (a scheduled task or /loop). The "
          "cover order's stop leg at the exchange is what actually stops you out; the flat-by alert is your "
          "cue to exit before Zerodha's %s square-off." % _t(nse.MIS_SQUARE_OFF_CAS))
    return 0


def _new_rule(a):
    cfg = _read(a.config, {}) if a.config else {}
    sym = kite.normalize_symbol(a.symbol) if a.symbol else ""
    if a.bell:
        rule = {"id": a.id or "bell", "type": "bell"}
    elif a.flat_by is not None:
        at = flat_by_time(cfg) if a.flat_by == "config" else hhmm(a.flat_by)
        if at is None:
            raise AlertError("--flat-by wants an IST time like 15:05")
        rule = {"id": a.id or (("%s-flat-by" % sym.lower()) if sym else "flat-by"), "type": "flat_by",
                "at": _t(at), "label": a.label or "flat by"}
        if sym:
            rule["symbol"] = sym
    elif a.time:
        at = hhmm(a.time)
        if at is None:
            raise AlertError("--time wants an IST time like 14:30")
        rule = {"id": a.id or ("time-%s" % _t(at).replace(":", "")), "type": "time", "at": _t(at),
                "label": a.label or "time alert"}
        if sym:
            rule["symbol"] = sym
    else:
        if not sym:
            raise AlertError("--symbol is required (or --from-plan, --time, --bell, --flat-by)")
        if a.above is not None:
            rule = {"type": "above", "price": a.above}
        elif a.below is not None:
            rule = {"type": "below", "price": a.below}
        elif a.stop is not None:
            rule = {"type": "stop", "price": a.stop}
        elif a.target is not None:
            rule = {"type": "target", "price": a.target}
        elif a.pct is not None:
            rule = {"type": "pct", "pct": a.pct}
            if a.direction:
                rule["direction"] = a.direction
        else:
            raise AlertError("give one of --above, --below, --stop, --target or --pct")
        val = rule.get("price", rule.get("pct"))
        rule = dict({"id": a.id or ("%s-%s-%s" % (sym.lower(), rule["type"], _n(val)))}, symbol=sym, **rule)
        if a.rearm:
            rule["rearm"] = True
        if a.note:
            rule["note"] = a.note
    if a.date:
        rule["date"] = a.date
    return rule


def cmd_add(a):
    doc = _read(a.rules, {"rules": []})
    doc.setdefault("rules", [])
    if a.from_plan:
        return _add_from_plan(a, doc)
    rule = _new_rule(a)
    validate(rule)
    if any(r.get("id") == rule["id"] for r in doc["rules"]):
        raise AlertError("a rule with id %s already exists" % rule["id"])
    doc["rules"].append(rule)
    _write(a.rules, doc)
    print("Added %s: %s" % (rule["id"], json.dumps(rule)))
    return 0


def cmd_list(a):
    doc = _read(a.rules, {"rules": []})
    state = _read(a.state, {})
    if not doc.get("rules"):
        print("No rules yet.")
        return 1
    for r in doc["rules"]:
        rid = r.get("id")
        rung = state.get("rung", {}).get(rid)
        print("%-24s %s%s" % (rid, json.dumps({k: v for k, v in r.items() if k != "id"}),
                              ("  (rang %s)" % rung) if rung else ""))
    return 0


def cmd_reset(a):
    state = _read(a.state, {})
    if a.id not in state.get("rung", {}):
        print("%s has not rung (nothing to reset)." % a.id)
        return 1
    del state["rung"][a.id]
    _write(a.state, state)
    print("Re-armed %s." % a.id)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Price alerts and the NSE clock (trader-alerts).")
    sub = ap.add_subparsers(dest="cmd")

    def net(p):
        p.add_argument("--offline", metavar="DIR", help="read kite-quote-SYMBOL.json files, no network")
        p.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-10-29 10:15\"")
        p.add_argument("--json", action="store_true")
        p.add_argument("--config", default="trader/config.json", help="for flat_by")

    p = sub.add_parser("clock", help="IST now, is NSE open, next open, flat-by")
    p.add_argument("--now")
    p.add_argument("--json", action="store_true")
    p.add_argument("--config", default="trader/config.json")
    p = sub.add_parser("quote", help="Kite quotes")
    net(p)
    p.add_argument("symbols", nargs="*")
    p.add_argument("--holdings", action="store_true", help="add the symbols in your portfolio and plan")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--write", nargs="?", const="trader/prices.json", metavar="PATH",
                   help="save to prices.json (default trader/prices.json)")
    p = sub.add_parser("check", help="check every rule, ring what hit")
    net(p)
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p.add_argument("--prices-out", default="trader/prices.json")
    p.add_argument("--no-write", action="store_true")
    p = sub.add_parser("add", help="add a rule")
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p.add_argument("--symbol")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--above", type=float)
    g.add_argument("--below", type=float)
    g.add_argument("--stop", type=float)
    g.add_argument("--target", type=float)
    g.add_argument("--pct", type=float)
    g.add_argument("--bell", action="store_true", help="the 09:15 open")
    g.add_argument("--time", metavar="HH:MM", help="a time alert, IST")
    g.add_argument("--flat-by", nargs="?", const="config", metavar="HH:MM",
                   help="the flat-by alert (default: the config's flat_by, else 15:05)")
    g.add_argument("--from-plan", action="store_true", help="stop, target and flat-by from the plan")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--config", default="trader/config.json", help="flat_by when the plan has none")
    p.add_argument("--label", help="what a time alert says")
    p.add_argument("--date", help="watch this rule on this date only (YYYY-MM-DD)")
    p.add_argument("--direction", choices=["up", "down"])
    p.add_argument("--rearm", action="store_true")
    p.add_argument("--note")
    p.add_argument("--id")
    p = sub.add_parser("list", help="list the rules")
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p = sub.add_parser("reset", help="re-arm a rule that rang")
    p.add_argument("id")
    p.add_argument("--state", default="trader/state/alerts.json")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"clock": cmd_clock, "quote": cmd_quote, "check": cmd_check, "add": cmd_add,
                "list": cmd_list, "reset": cmd_reset}[a.cmd](a)
    except AlertError as e:
        print("alerts.py: %s" % e, file=sys.stderr)
        return 1
    except kite.KiteError as e:      # LoginNeeded included: no session, no other source
        print("alerts.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
