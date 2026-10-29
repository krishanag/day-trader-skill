#!/usr/bin/env python3
"""Order tickets, the paper ledger and the decision log for trader-broker.
Standard library only.

This tool never calls Zerodha's order endpoints and never places anything.
It does its work around the one moment that matters, your yes:

  show      renders each proposed order in trader/orders.json as a plain
            ticket: symbol, side, quantity, product MIS, the cover order's
            stop-loss trigger, the max loss in rupees at the stop, the target
            trader-alerts watches, the flat-by time, plus the exact JSON
            Claude passes to the Kite MCP tool. A ticket is READY only when
            trader/verdicts.json holds a PASS from today for this exact order
            (matched by the order hash) and nobody has decided on it yet.
            PAPER or LIVE comes from trader/config.json.
  approve   records your explicit yes for ONE order, word for word, in
            trader/log.jsonl with the max loss in rupees. It refuses "yes to
            all", "sure", "ok", a yes that names another stock or amount, and
            any order that is not READY.
  decline   records a no.
  fill      paper: a simulated fill at Kite's last price into
            trader/portfolio.json and trader/fills.json, nothing sent to the
            broker. Live: the fill from the MCP tool's result (--order-id,
            --avg, --qty, and --parent-order-id for a cover order).
  result    records what happened after the Allow once / Deny card:
            placed (with Kite's order id), denied, or failed.
  positions what trader/portfolio.json holds, with cash.
  exit      rewrites trader/orders.json with the exit ticket for one symbol:
            for a cover order the exit-cover-order call, for a regular order
            a MIS SELL MARKET plus cancelling the stop leg. flatten does it
            for every open position. Exits are never blocked by day limits,
            but they still go through trader-risk and your yes.
  log       prints today's decisions.

    python3 ticket.py show                         # every order, READY or not
    python3 ticket.py show --hash ab12cd34ef56 --json
    python3 ticket.py approve --hash ab12cd34ef56 --yes "yes"
    python3 ticket.py fill --hash ab12cd34ef56     # paper: simulated fill at Kite's last price
    python3 ticket.py fill --hash ab12cd34ef56 --order-id 251029000123 --avg 1174.60 --qty 43 --parent-order-id 251029000123
    python3 ticket.py result --hash ab12cd34ef56 --status placed --order-id 251029000123 --filled-qty 43 --avg 1174.60
    python3 ticket.py positions
    python3 ticket.py exit RELIANCE
    python3 ticket.py flatten
    python3 ticket.py log

Times are IST; --now "2026-10-29 10:15" pins the clock. The order hash is
the pack's: sha256 of the sorted, compact JSON of {"kite": the Kite body,
"stop_leg": the stop leg or null}, first 12 hex digits, the same function
trader-risk uses.
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

YES_WORDS = ("yes", "y")
BATCH_WORDS = {"all", "every", "everything", "rest", "remaining", "them", "both", "batch", "each"}
DOUBT_WORDS = {"no", "not", "dont", "don't", "wait", "cancel", "stop", "but", "maybe", "later",
               "hold", "unless", "if"}
PAPER_PREFIX = "PAPER-"
SQUARE_OFF_CHARGE = "Rs 50 + GST"
EXIT_TAG = "dtsexit"
QUOTE_FILE = "kite-quote-%s.json"


class TicketError(Exception):
    pass


# ---------------------------------------------------------------- time (IST)

def parse_now(text):
    """'2026-10-29 10:15' (IST) or an ISO time with an offset -> aware IST datetime."""
    if not text:
        return nse.now_ist().replace(microsecond=0)
    try:
        dt = datetime.datetime.fromisoformat(text.strip())
    except ValueError:
        raise TicketError("--now wants an IST time like \"2026-10-29 10:15\"")
    return nse.to_ist(dt).replace(microsecond=0)


def iso(now):
    return now.isoformat()


def flat_by_of(cfg, plan=None):
    """The flat-by time: the plan's, else the config's, else the pack default."""
    for src in (plan or {}, cfg or {}):
        if src.get("flat_by"):
            try:
                return nse.parse_hhmm(src["flat_by"])
            except ValueError:
                raise TicketError("flat_by %r is not a time like 15:05" % src["flat_by"])
    return nse.DEFAULT_FLAT_BY


# ---------------------------------------------------------------- files

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise TicketError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise TicketError("%s is not valid JSON: %s" % (path, e))


def write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def read_log(path):
    """trader/log.jsonl -> list of records (a bad line is skipped, not fatal)."""
    if not path or not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def append_log(path, rec):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")


def rec_day(rec):
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


# ---------------------------------------------------------------- the order

def order_hash(order):
    """The pack's order hash: the same function as trader-risk."""
    if not isinstance(order, dict) or not isinstance(order.get("kite"), dict):
        raise TicketError("an order needs a \"kite\" body (the Kite order parameters)")
    body = json.dumps({"kite": order["kite"], "stop_leg": order.get("stop_leg")}, sort_keys=True,
                      separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()[:12]


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _i(x):
    f = _f(x)
    return int(f) if f is not None and f == int(f) else f


def _p(v):
    return "%.2f" % float(v)


def rupees(v):
    """1167.4 -> 'Rs 1,167.40' (two decimals, Indian output stays simple)."""
    return "Rs {:,.2f}".format(float(v))


def kind_of(order):
    """entry or exit. The broker marks its exit tickets; a sell is otherwise an exit (no shorts)."""
    if order.get("kind") in ("entry", "exit"):
        return order["kind"]
    return "exit" if str(order.get("side", "")).lower() == "sell" else "entry"


def is_co(order):
    return str(order.get("order_style") or order.get("kite", {}).get("variety") or "").lower() == "co"


def is_co_exit(order):
    k = order.get("kite") or {}
    return k.get("variety") == "co" and "parent_order_id" in k


def symbol_of(order):
    return str(order.get("symbol") or order.get("kite", {}).get("tradingsymbol") or "?").upper()


def qty_of(order):
    k = order.get("kite") or {}
    return _i(order.get("qty", k.get("quantity")))


def stop_of(order):
    k = order.get("kite") or {}
    if k.get("variety") == "co" and _f(k.get("trigger_price")):
        return _f(k["trigger_price"])
    leg = order.get("stop_leg") or {}
    if _f(leg.get("trigger_price")):
        return _f(leg["trigger_price"])
    return _f(order.get("stop")) or _f((order.get("plan") or {}).get("stop"))


def entry_of(order):
    k = order.get("kite") or {}
    if str(k.get("order_type", "")).upper() == "LIMIT" and _f(k.get("price")):
        return _f(k["price"])
    return _f((order.get("plan") or {}).get("entry"))


def max_loss(order):
    """(rupees lost if the stop fills exactly at the stop, stop) or (None, stop)."""
    stop = stop_of(order)
    q = _f(qty_of(order))
    if not stop or not q:
        return None, stop
    plan = order.get("plan") or {}
    k = order.get("kite") or {}
    if str(k.get("order_type", "")).upper() == "LIMIT" and _f(k.get("price")):
        entry = _f(k["price"])
    elif _f(plan.get("max_loss_inr")) is not None:
        return round(_f(plan["max_loss_inr"]), 2), stop
    else:
        entry = _f(plan.get("entry"))
    if not entry:
        return None, stop
    risk = abs(entry - stop)
    return round(q * int(round(risk * 100)) / 100.0, 2), stop


def mcp_calls(order):
    """[(tool, arguments)] for Zerodha's kite-mcp-server, in the order Claude calls them.
    An entry is place_order with the kite body exactly (a regular order style adds the
    SL-M stop leg as a second place_order). A cover-order exit is cancel_order on the
    stop leg with variety co (the MCP tool takes no parent_order_id). A regular exit
    cancels the stop leg, then place_order the MIS SELL MARKET."""
    k = order["kite"]
    if is_co_exit(order):
        return [("cancel_order", {"variety": "co", "order_id": k.get("order_id")})]
    calls = []
    cancel = order.get("cancel_order")
    if isinstance(cancel, dict) and cancel.get("order_id"):
        calls.append(("cancel_order", {"variety": cancel.get("variety", "regular"),
                                       "order_id": cancel["order_id"]}))
    calls.append(("place_order", dict(k)))
    if isinstance(order.get("stop_leg"), dict):
        calls.append(("place_order", dict(order["stop_leg"])))
    return calls


def plain_parts(order, flat_by=None):
    """The ticket in plain words, one part per line."""
    k = order.get("kite") or {}
    sym = symbol_of(order)
    q = qty_of(order)
    if is_co_exit(order):
        return ["EXIT %s %s: exit the cover order (parent %s, stop leg %s), MIS"
                % (q if q is not None else "all", sym, k.get("parent_order_id"), k.get("order_id"))]
    side = str(k.get("transaction_type") or order.get("side", "")).upper()
    otype = str(k.get("order_type", "MARKET")).upper()
    how = "limit %s" % _p(k["price"]) if otype == "LIMIT" and _f(k.get("price")) else otype.lower()
    style = "cover order" if k.get("variety") == "co" else "regular order"
    product = str(k.get("product") or order.get("product") or "MIS").upper()
    parts = ["%s %s %s on %s, %s (intraday), %s, %s" % (side, q, sym, k.get("exchange", "NSE"),
                                                         product, style, how)]
    if kind_of(order) == "exit":
        cancel = order.get("cancel_order") or {}
        if cancel.get("order_id"):
            parts.append("cancel the stop-loss leg %s first" % cancel["order_id"])
        return parts
    if k.get("variety") == "co" and _f(k.get("trigger_price")):
        parts.append("stop-loss trigger %s (held by the exchange as the cover order's second leg)"
                     % _p(k["trigger_price"]))
    leg = order.get("stop_leg")
    if isinstance(leg, dict):
        parts.append("stop-loss leg: %s %s %s, trigger %s (a second order, placed right after the entry)"
                     % (leg.get("transaction_type", "SELL"), leg.get("quantity", q),
                        leg.get("order_type", "SL-M"), _p(leg.get("trigger_price", 0))))
    if _f(order.get("target")):
        parts.append("target %s (trader-alerts watches it; it is not an order)" % _p(order["target"]))
    loss, stop = max_loss(order)
    if loss is not None:
        parts.append("max loss about %s if the stop fills at %s (it can fill worse in a fast market)"
                     % (rupees(loss), _p(stop)))
    fb = flat_by or (order.get("plan") or {}).get("flat_by")
    if fb:
        parts.append("flat by %s IST" % (fb if isinstance(fb, str) else fb.strftime("%H:%M")))
    return parts


def plain(order, flat_by=None):
    return " / ".join(plain_parts(order, flat_by))


def amount_numbers(order):
    nums = []
    k = order.get("kite") or {}
    for v in (qty_of(order), _f(k.get("price")), _f(k.get("trigger_price")), _f(order.get("target")),
              _f((order.get("stop_leg") or {}).get("trigger_price")), _f(order.get("stop"))):
        if v is not None:
            nums.append(float(v))
    loss, _ = max_loss(order)
    if loss is not None:
        nums.append(loss)
    return nums


def parse_yes(reply, order, batch_symbols=()):
    """Is this reply an explicit yes for THIS one order? Returns (ok, why)."""
    raw = (reply or "").strip()
    if not raw:
        return False, "the reply is empty"
    if "?" in raw:
        return False, "the reply is a question, not a yes"
    words = re.findall(r"[a-z']+", raw.lower())
    if not words or words[0] not in YES_WORDS:
        return False, "only a reply that starts with \"yes\" counts (not ok, sure, go, do it)"
    if BATCH_WORDS & set(words):
        return False, "a yes covers one order at a time, not a batch"
    if DOUBT_WORDS & set(words[1:]):
        return False, "the reply has a condition or a doubt in it; ask again"
    sym = symbol_of(order)
    tokens = set(re.findall(r"[A-Z][A-Z0-9&\-]{0,19}", raw.upper()))
    others = {str(s).upper() for s in batch_symbols} - {sym}
    named = tokens & others
    if named:
        return False, "the reply names %s, but this order is for %s" % (", ".join(sorted(named)), sym)
    nums = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", raw)]
    if nums:
        want = amount_numbers(order)
        if not any(abs(n - w) < 0.005 for n in nums for w in want):
            return False, "the reply says %s, but this order is %s" % (
                ", ".join(_p(n) if n != int(n) else str(int(n)) for n in nums), plain(order))
    return True, "explicit yes for %s" % plain(order)


# ---------------------------------------------------------------- verdicts and the log

def latest(log, h, today):
    """The latest decision recorded today for this order hash, or None."""
    found = None
    for rec in log:
        if rec.get("order_hash") == h and rec_day(rec) == today:
            found = rec
    return found


def verdict_for(vdoc, h, today):
    if not vdoc:
        return None, "trader-risk has not checked these orders (run check.py)"
    date = str(vdoc.get("date") or str(vdoc.get("checked_at", ""))[:10])
    if date != today:
        return None, "the risk check is from %s, not today; run check.py again" % (date or "an unknown day")
    v = (vdoc.get("verdicts") or {}).get(h)
    if v is None:
        for r in vdoc.get("results") or []:
            if isinstance(r, dict) and r.get("order_hash") == h:
                v = r
    if v is None:
        return None, ("no verdict for this exact order (hash %s): it changed or is new; run check.py again"
                      % h)
    if isinstance(v, str):
        v = {"verdict": v}
    return v, None


def status_of(order, vdoc, log, today):
    """(ready, label, verdict)"""
    h = order_hash(order)
    v, why = verdict_for(vdoc, h, today)
    if v is None:
        return False, "NOT READY: " + why, None
    if str(v.get("verdict", "")).upper() != "PASS":
        return False, "BLOCKED by trader-risk: " + "; ".join(v.get("reasons") or ["no reason given"]), v
    rec = latest(log, h, today)
    if rec:
        st = rec.get("status")
        if st == "approved":
            return False, "APPROVED by you at %s, waiting for the fill" % rec.get("at"), v
        return False, "DONE: %s at %s" % (st, rec.get("at")), v
    return True, "READY: trader-risk PASS, waiting for your yes", v


# ---------------------------------------------------------------- rendering

def clock_notes(order, cfg, now, today):
    """What the IST clock says about this ticket right now."""
    notes = []
    cas = bool((order.get("plan") or {}).get("cas", order.get("cas", True)))
    phase = nse.phase(now, cas)
    ok, reason = nse.trading_day(now.date())
    kind = kind_of(order)
    if not ok:
        notes.append("the market is closed today (%s); nothing can be placed until %s IST"
                     % (reason, nse.next_open(now).strftime("%Y-%m-%d %H:%M")))
        return notes
    if reason:
        notes.append(reason)
    if phase != "open":
        notes.append("NSE is %s right now (%s IST); an order waits for the open at %s"
                     % (phase, now.strftime("%H:%M"), nse.next_open(now).strftime("%Y-%m-%d %H:%M")))
    fb = flat_by_of(cfg, order.get("plan"))
    if kind == "entry" and now.time() >= fb:
        notes.append("it is past your flat-by %s: a new MIS entry now would be squared off by Zerodha at "
                     "%s in F&O stocks (%s each time), 15:30 otherwise"
                     % (fb.strftime("%H:%M"), nse.MIS_SQUARE_OFF_CAS.strftime("%H:%M"), SQUARE_OFF_CHARGE))
    if kind == "exit" and cas and phase == "open" and now.time() >= nse.MIS_SQUARE_OFF_CAS:
        notes.append("it is past %s: Zerodha may already have squared off this MIS position (%s); check "
                     "get_positions before the yes" % (nse.MIS_SQUARE_OFF_CAS.strftime("%H:%M"),
                                                       SQUARE_OFF_CHARGE))
    return notes


def account_label(cfg):
    return "PAPER" if cfg.get("paper", True) else "LIVE ACCOUNT, REAL MONEY"


def render(order, idx, total, cfg, vdoc, log, now, today):
    ready, label, v = status_of(order, vdoc, log, today)
    paper = cfg.get("paper", True)
    h = order_hash(order)
    lines = ["ORDER %d of %d, %s, Zerodha Kite, hash %s" % (idx, total, account_label(cfg), h)]
    for part in plain_parts(order, flat_by_of(cfg, order.get("plan")).strftime("%H:%M")):
        lines.append("  " + part)
    if order.get("why"):
        lines.append("  why: " + order["why"])
    lines.append("  " + label)
    for w in (v or {}).get("warnings", []):
        lines.append("  note: " + w)
    for n in clock_notes(order, cfg, now, today):
        lines.append("  note: " + n)
    for tool, args in mcp_calls(order):
        if paper:
            lines.append("  Paper mode: nothing goes to Zerodha. The Kite MCP call it stands for, %s:" % tool)
        else:
            lines.append("  Claude will call the Kite MCP tool %s with exactly:" % tool)
        for l in json.dumps(args, indent=2).splitlines():
            lines.append("    " + l)
    if is_co(order) and kind_of(order) == "entry":
        lines.append("  A cover order is the entry plus a stop-loss leg the exchange holds. If the stop "
                     "trigger is hit, the leg sells at market.")
    if ready:
        lines.append("  Reply \"yes\" to place this one order, or \"no\" to skip it.")
        if paper:
            lines.append("  Paper: after the yes, ticket.py fill --hash %s records a simulated fill at "
                         "Kite's last price." % h)
        else:
            lines.append("  Claude's own card then asks again: Allow once places it, Deny stops it.")
    return "\n".join(lines), ready


def ticket_json(order, idx, cfg, vdoc, log, now, today):
    ready, label, v = status_of(order, vdoc, log, today)
    loss, stop = max_loss(order)
    return {"index": idx, "ready": ready, "status": label, "plain": plain(order),
            "order_hash": order_hash(order), "paper": bool(cfg.get("paper", True)), "broker": "zerodha",
            "kind": kind_of(order), "calls": [{"tool": t, "arguments": a} for t, a in mcp_calls(order)],
            "why": order.get("why", ""), "cover_order": is_co(order), "max_loss_inr": loss, "stop": stop,
            "target": _f(order.get("target")), "flat_by": flat_by_of(cfg, order.get("plan")).strftime("%H:%M"),
            "notes": clock_notes(order, cfg, now, today)}


# ---------------------------------------------------------------- fills -> portfolio

def new_portfolio(cfg):
    budget = _f(cfg.get("budget_inr"))
    if not budget:
        raise TicketError("no portfolio yet and no budget_inr in the config to start it from")
    return {"cash_inr": round(budget, 2), "positions": {}, "paper": bool(cfg.get("paper", True))}


def apply_fill(portfolio_path, cfg, symbol, side, qty, price, at, order_style="co",
               parent_order_id=None, stop_order_id=None):
    """Record a fill in trader/portfolio.json: cash and one position per symbol (MIS,
    intraday, so no lots). Returns (portfolio, realized rupees on a sell)."""
    pf = read_json(portfolio_path, "portfolio", required=False) or new_portfolio(cfg)
    symbol = symbol.upper()
    qty, price = _f(qty), _f(price)
    if not qty or qty <= 0 or not price or price <= 0:
        raise TicketError("a fill needs a positive quantity and price")
    if qty != int(qty):
        raise TicketError("NSE equities fill in whole shares, not %s" % qty)
    qty = int(qty)
    cost = round(qty * price, 2)
    positions = dict(pf.get("positions") or {})
    pos = dict(positions.get(symbol) or {})
    realized = 0.0
    if side == "buy":
        held = int(pos.get("qty") or 0)
        avg = _f(pos.get("avg")) or 0.0
        new_qty = held + qty
        new_avg = round((held * avg + qty * price) / new_qty, 2)
        pos.update({"qty": new_qty, "avg": new_avg, "product": "MIS", "order_style": order_style,
                    "entered_at": pos.get("entered_at") or at})
        if parent_order_id:
            pos["parent_order_id"] = parent_order_id
        if stop_order_id:
            pos["stop_order_id"] = stop_order_id
        positions[symbol] = pos
        cash = round(float(pf.get("cash_inr", 0)) - cost, 2)
    else:
        held = int(pos.get("qty") or 0)
        if held < qty:
            raise TicketError("cannot record selling %d %s: the portfolio holds %d" % (qty, symbol, held))
        realized = round((price - (_f(pos.get("avg")) or 0.0)) * qty, 2)
        left = held - qty
        if left:
            pos["qty"] = left
            positions[symbol] = pos
        else:
            positions.pop(symbol, None)
        cash = round(float(pf.get("cash_inr", 0)) + cost, 2)
    out = dict(pf, cash_inr=cash, positions=positions, paper=bool(cfg.get("paper", True)),
               updated_at=at)
    write_json(portfolio_path, out)
    return out, realized


def record_fill(a, cfg, order, h, now, today, kite_order_id, qty, price, parent_order_id=None,
                stop_order_id=None, paper=True):
    """portfolio.json, fills.json and a 'filled' line in log.jsonl, in that order."""
    sym = symbol_of(order)
    side = "sell" if kind_of(order) == "exit" else "buy"
    style = "co" if is_co(order) or is_co_exit(order) else "regular"
    pf, realized = apply_fill(a.portfolio, cfg, sym, side, qty, price, iso(now), style,
                              parent_order_id, stop_order_id)
    fill = {"order_hash": h, "kite_order_id": kite_order_id, "symbol": sym, "side": side,
            "filled_qty": int(_f(qty)), "average_price": round(_f(price), 2), "at": iso(now), "paper": paper}
    if parent_order_id:
        fill["parent_order_id"] = parent_order_id
    if side == "sell":
        fill["realized_inr"] = realized
    fills = read_json(a.fills, "fills", required=False) or []
    write_json(a.fills, fills + [fill])
    loss, stop = max_loss(order)
    append_log(a.log, {"at": iso(now), "date": today, "status": "filled", "order_hash": h, "symbol": sym,
                       "side": side, "qty": fill["filled_qty"], "price": fill["average_price"],
                       "kind": kind_of(order), "max_loss_inr": loss, "stop": stop, "paper": paper,
                       "kite_order_id": kite_order_id,
                       "plain": "%s %d %s at %s" % (side.upper(), fill["filled_qty"], sym, _p(price))})
    return pf, realized


# ---------------------------------------------------------------- quotes (paper fills)

def _connect():
    return kite.Kite.connect()


def last_price(symbol, offline=None):
    """Kite's last traded price for one symbol: from a saved quote file, or the session."""
    sym = kite.normalize_symbol(symbol)
    if offline:
        path = os.path.join(offline, QUOTE_FILE % sym)
        if not os.path.exists(path):
            raise TicketError("no offline quote file %s" % path)
        with open(path, encoding="utf-8") as fh:
            q = json.load(fh)
    else:
        q = _connect().quotes([sym]).get(sym)
    price = _f((q or {}).get("last_price"))
    if not price:
        raise TicketError("Kite has no last price for %s" % sym)
    return price, str((q or {}).get("last_trade_time") or "")


# ---------------------------------------------------------------- commands

def _clock(a):
    now = parse_now(getattr(a, "now", None))
    today = getattr(a, "today", None) or now.date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}\Z", today):
        raise TicketError("--today wants a date like 2026-10-29")
    return now, today


def _load(a):
    doc = read_json(a.orders, "orders file (run trader-budget, or ticket.py exit, first)")
    orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
    if not isinstance(orders, list) or not orders:
        raise TicketError("no orders in %s" % a.orders)
    for o in orders:
        order_hash(o)
    cfg = read_json(a.config, "config", required=False) or {}
    vdoc = read_json(a.verdicts, "verdicts", required=False)
    log = read_log(a.log)
    now, today = _clock(a)
    return orders, cfg, vdoc, log, now, today


def _pick(orders, h):
    if not h:
        raise TicketError("say which order: --hash H (ticket.py show prints each hash)")
    for o in orders:
        if order_hash(o) == h:
            return o
    raise TicketError("no order with hash %s in the orders file (hashes: %s)"
                      % (h, ", ".join(order_hash(o) for o in orders)))


def cmd_show(a):
    orders, cfg, vdoc, log, now, today = _load(a)
    if a.hash:
        picks = [(i, o) for i, o in enumerate(orders, 1) if order_hash(o) == a.hash]
        if not picks:
            _pick(orders, a.hash)
    else:
        picks = list(enumerate(orders, 1))
    if a.json:
        out = [ticket_json(o, i, cfg, vdoc, log, now, today) for i, o in picks]
        print(json.dumps(out[0] if a.hash else out, indent=2))
        return 0 if any(t["ready"] for t in out) else 1
    blocks, any_ready = [], False
    for i, o in picks:
        text, ready = render(o, i, len(orders), cfg, vdoc, log, now, today)
        blocks.append(text)
        any_ready = any_ready or ready
    print("%s IST, NSE %s." % (now.strftime("%Y-%m-%d %H:%M"), nse.phase(now)))
    print("\n\n".join(blocks))
    print("\nOne order at a time: a yes covers one ticket, never the batch. Education and tooling, "
          "not financial advice.")
    return 0 if any_ready else 1


def cmd_approve(a):
    orders, cfg, vdoc, log, now, today = _load(a)
    order = _pick(orders, a.hash)
    ready, label, v = status_of(order, vdoc, log, today)
    if not ready:
        raise TicketError("order %s is not ready: %s" % (a.hash, label))
    ok, why = parse_yes(a.reply, order, [symbol_of(o) for o in orders])
    if not ok:
        print("Not recorded: %s. Ask the user again with the ticket for order %s." % (why, a.hash))
        return 1
    loss, stop = max_loss(order)
    paper = bool(cfg.get("paper", True))
    rec = {"at": iso(now), "date": today, "status": "approved", "order_hash": a.hash,
           "symbol": symbol_of(order), "side": order.get("side"), "qty": qty_of(order),
           "kind": kind_of(order), "max_loss_inr": loss, "stop": stop, "plain": plain(order),
           "reply": a.reply, "paper": paper, "broker": "zerodha"}
    append_log(a.log, rec)
    print("Recorded your yes for %s (hash %s): %s." % (symbol_of(order), a.hash, plain(order)))
    if paper:
        print("Paper mode: nothing goes to Zerodha. Next: ticket.py fill --hash %s records a simulated "
              "fill at Kite's last price." % a.hash)
        return 0
    for tool, args in mcp_calls(order):
        print("Now call the Kite MCP tool %s with exactly:" % tool)
        print(json.dumps(args, indent=2))
    print("Claude's card shows Allow once / Deny. Then: ticket.py result --hash %s --status "
          "placed|denied|failed --order-id ID" % a.hash)
    return 0


def cmd_decline(a):
    orders, cfg, vdoc, log, now, today = _load(a)
    order = _pick(orders, a.hash)
    rec = {"at": iso(now), "date": today, "status": "declined", "order_hash": a.hash,
           "symbol": symbol_of(order), "side": order.get("side"), "kind": kind_of(order),
           "plain": plain(order), "reply": a.reply or "no", "paper": bool(cfg.get("paper", True))}
    append_log(a.log, rec)
    print("Recorded: you said no to %s (%s). Nothing was sent." % (symbol_of(order), plain(order)))
    return 0


def cmd_result(a):
    orders, cfg, vdoc, log, now, today = _load(a)
    order = _pick(orders, a.hash)
    rec = latest(log, a.hash, today)
    if not rec or rec.get("status") != "approved":
        raise TicketError("order %s has no yes recorded today; a result can only follow an approval" % a.hash)
    new = {"at": iso(now), "date": today, "status": a.status, "order_hash": a.hash, "symbol": rec["symbol"],
           "side": rec.get("side"), "plain": rec.get("plain"), "paper": rec.get("paper")}
    for k in ("qty", "kind", "max_loss_inr", "stop"):
        if rec.get(k) is not None:
            new[k] = rec[k]
    if a.order_id:
        new["kite_order_id"] = a.order_id
    if a.note:
        new["note"] = a.note
    append_log(a.log, new)
    print("Recorded order %s as %s." % (a.hash, a.status))
    if a.status == "placed" and a.filled_qty and a.avg:
        parent = a.parent_order_id or (a.order_id if is_co(order) and kind_of(order) == "entry" else None)
        record_fill(a, cfg, order, a.hash, now, today, a.order_id or "", a.filled_qty, a.avg, parent,
                    a.stop_order_id, paper=bool(cfg.get("paper", True)))
        print("Added the fill to %s." % a.portfolio)
    elif a.status == "placed":
        print("When it fills (get_order_history %s), record it: ticket.py fill --hash %s --order-id %s "
              "--avg P --qty N%s" % (a.order_id or "ID", a.hash, a.order_id or "ID",
                                     " --parent-order-id %s" % a.order_id if is_co(order) else ""))
    return 0


def cmd_fill(a):
    orders, cfg, vdoc, log, now, today = _load(a)
    order = _pick(orders, a.hash)
    rec = latest(log, a.hash, today)
    if not rec or rec.get("status") not in ("approved", "placed"):
        raise TicketError("order %s has no yes recorded today; a fill can only follow an approval" % a.hash)
    if rec.get("status") == "placed" and not a.order_id:
        a.order_id = rec.get("kite_order_id")
    paper = bool(cfg.get("paper", True))
    sym = symbol_of(order)
    if paper:
        if a.order_id or a.avg or a.qty:
            raise TicketError("paper mode: the fill is simulated at Kite's last price; do not pass "
                              "--order-id, --avg or --qty (set \"paper\": false to go live)")
        try:
            price, when = last_price(sym, a.offline)
        except kite.LoginNeeded as e:
            raise TicketError(str(e))
        except kite.KiteError as e:
            raise TicketError("cannot read the last price: %s" % e)
        qty = qty_of(order)
        oid = PAPER_PREFIX + a.hash
        parent = oid if is_co(order) and kind_of(order) == "entry" else None
        stop_id = oid + "-SL" if parent else None
        pf, realized = record_fill(a, cfg, order, a.hash, now, today, oid, qty, price, parent, stop_id, True)
        print("Paper fill: %s %d %s at Kite's last price %s%s. Nothing was sent to Zerodha."
              % ("SELL" if kind_of(order) == "exit" else "BUY", qty, sym, rupees(price),
                 (" (last trade %s)" % when) if when else ""))
    else:
        if not (a.order_id and a.avg and a.qty):
            raise TicketError("live mode: pass the fill from the MCP result: --order-id ID --avg P --qty N "
                              "(and --parent-order-id for a cover order)")
        parent = a.parent_order_id or (a.order_id if is_co(order) and kind_of(order) == "entry" else None)
        pf, realized = record_fill(a, cfg, order, a.hash, now, today, a.order_id, a.qty, a.avg, parent,
                                   a.stop_order_id, False)
        print("Recorded the fill: %s %d %s at %s, Kite order %s."
              % ("SELL" if kind_of(order) == "exit" else "BUY", int(_f(a.qty)), sym, rupees(a.avg), a.order_id))
    if kind_of(order) == "exit":
        print("Realized %s%s on %s." % ("+" if realized >= 0 else "-", rupees(abs(realized)), sym))
    print("Cash now %s; %d open position(s)." % (rupees(pf["cash_inr"]), len(pf["positions"])))
    if kind_of(order) == "entry":
        print("Next: trader-alerts add --from-plan, and alerts.py check every few minutes. By the flat-by: "
              "ticket.py exit %s." % sym)
    return 0


def cmd_positions(a):
    cfg = read_json(a.config, "config", required=False) or {}
    now, today = _clock(a)
    pf = read_json(a.portfolio, "portfolio", required=False)
    if pf is None:
        print("No portfolio yet (%s): the first paper fill starts it from budget_inr." % a.portfolio)
        return 1
    prices = (read_json(a.prices, "prices", required=False) or {}).get("prices") or {}
    positions = pf.get("positions") or {}
    print("%s, cash %s, %s." % (account_label(cfg), rupees(pf.get("cash_inr", 0)),
                                 "%d open position(s)" % len(positions) if positions else "no open positions"))
    rows = []
    for sym in sorted(positions):
        p = positions[sym]
        qty, avg = int(p.get("qty") or 0), _f(p.get("avg")) or 0.0
        line = "  %-12s %5d @ %s  %s, %s" % (sym, qty, rupees(avg), p.get("product", "MIS"),
                                             "cover order" if p.get("order_style", "co") == "co" else "regular order")
        if p.get("parent_order_id"):
            line += ", parent %s" % p["parent_order_id"]
        if p.get("stop_order_id"):
            line += ", stop leg %s" % p["stop_order_id"]
        q = prices.get(sym) or {}
        last = _f(q.get("last") if isinstance(q, dict) else q)
        if last and fresh(q.get("time") if isinstance(q, dict) else None, now):
            pnl = round((last - avg) * qty, 2)
            line += ", last %s, %s%s" % (rupees(last), "+" if pnl >= 0 else "", rupees(pnl))
        rows.append((sym, line))
    for _, line in rows:
        print(line)
    if positions:
        print("Flat by %s IST (Zerodha squares off MIS in F&O stocks at %s, %s). To close one: ticket.py "
              "exit SYMBOL; all: ticket.py flatten." % (flat_by_of(cfg).strftime("%H:%M"),
                                                        nse.MIS_SQUARE_OFF_CAS.strftime("%H:%M"),
                                                        SQUARE_OFF_CHARGE))
    if a.json:
        print(json.dumps(pf, indent=2))
    return 0 if positions else 1


def fresh(stamp, now, minutes=15):
    if not stamp:
        return False
    try:
        t = nse.to_ist(datetime.datetime.fromisoformat(str(stamp).replace(" ", "T")))
    except ValueError:
        return False
    return abs((now - t).total_seconds()) <= minutes * 60


def exit_ticket(sym, pos, qty, cfg, flat_by, source, stop_order_id=None):
    style = str(pos.get("order_style") or "co")
    avg = _f(pos.get("avg")) or 0.0
    why = ("exit %s: closes the %d-share MIS position (avg %s) before the %s flat-by (qty from %s)"
           % (sym, qty, rupees(avg), flat_by.strftime("%H:%M"), source))
    order = {"symbol": sym, "exchange": "NSE", "side": "sell", "qty": qty, "product": "MIS",
             "order_style": style, "kind": "exit", "stop_leg": None, "stop": None, "target": None,
             "why": why, "plan": {"entry": avg, "flat_by": flat_by.strftime("%H:%M"), "max_loss_inr": 0.0,
                                  "cas": pos.get("cas", True)}}
    if style == "co":
        parent = pos.get("parent_order_id")
        leg = stop_order_id or pos.get("stop_order_id")
        if not parent:
            raise TicketError("%s: the cover order's parent order id is not recorded; record the entry fill "
                              "with --parent-order-id first" % sym)
        if not leg:
            raise TicketError("%s: the cover order's stop leg id is not recorded. get_orders, find the order "
                              "whose parent_order_id is %s, then: ticket.py exit %s --stop-order-id ID"
                              % (sym, parent, sym))
        order["kite"] = {"variety": "co", "parent_order_id": parent, "order_id": leg}
    else:
        order["kite"] = {"variety": "regular", "exchange": "NSE", "tradingsymbol": sym,
                         "transaction_type": "SELL", "order_type": "MARKET", "quantity": qty,
                         "product": "MIS", "validity": "DAY", "tag": EXIT_TAG}
        leg = stop_order_id or pos.get("stop_order_id")
        order["cancel_order"] = {"variety": "regular", "order_id": leg} if leg else None
    return order


def _write_exits(a, cfg, orders, now, today, source):
    old = read_json(a.orders, "orders", required=False)
    replaced = len(old.get("orders", [])) if isinstance(old, dict) else 0
    doc = {"created_by": "trader-broker", "created_at": iso(now), "status": "proposed",
           "paper": bool(cfg.get("paper", True)),
           "note": "Exit tickets. Nothing is placed until trader-risk passes each one and you say yes to it "
                   "in the chat.",
           "source": source, "orders": orders}
    write_json(a.orders, doc)
    return replaced


def _exit_notes(cfg, now):
    notes = []
    ok, reason = nse.trading_day(now.date())
    phase = nse.phase(now, True)
    if not ok or phase in ("closed", "after-close"):
        notes.append("NSE is %s (%s IST%s): an MIS position never carries overnight, Zerodha squares it off; "
                     "check get_positions and record what happened with ticket.py fill"
                     % (phase, now.strftime("%H:%M"), (", " + reason) if reason else ""))
    elif now.time() >= nse.MIS_SQUARE_OFF_CAS:
        notes.append("it is past %s: Zerodha may already have squared off F&O stocks (%s); check "
                     "get_positions before the yes" % (nse.MIS_SQUARE_OFF_CAS.strftime("%H:%M"), SQUARE_OFF_CHARGE))
    return notes


def _print_exit_next(a, cfg, orders, replaced, flat_by, now):
    for o in orders:
        print("Exit %s: %s (flat by %s IST)." % (o["symbol"], plain(o), flat_by.strftime("%H:%M")))
    print("Wrote %d ticket(s) to %s%s." % (len(orders), a.orders, (" (replaced %d earlier proposal%s)"
                                                                   % (replaced, "" if replaced == 1 else "s"))
                                            if replaced else ""))
    for n in _exit_notes(cfg, now):
        print("note: " + n)
    print("Next:")
    print("  1. trader-risk: check.py. An exit that only closes what you hold is never blocked by the daily")
    print("     limits or the entry window.")
    print("  2. ticket.py show, then the user's yes for each ticket, one at a time.")
    if cfg.get("paper", True):
        print("  3. Paper: ticket.py fill --hash H records the simulated exit at Kite's last price.")
    else:
        print("  3. Claude calls the Kite MCP tool the ticket shows (cancel_order with variety co exits a cover")
        print("     order; a regular position cancels its stop leg, then place_order the sell), Needs approval")
        print("     each time. Then ticket.py result and fill with the ids from the result.")


def cmd_exit(a):
    cfg = read_json(a.config, "config", required=False) or {}
    now, today = _clock(a)
    sym = kite.normalize_symbol(a.symbol)
    pf = read_json(a.portfolio, "portfolio", required=False) or {}
    pos = (pf.get("positions") or {}).get(sym)
    source = a.portfolio
    if a.qty is not None:
        qty = _i(a.qty)
        if not qty or qty <= 0 or qty != int(qty):
            raise TicketError("--qty wants the whole number of shares you hold, like --qty 43")
        pos = dict(pos or {"order_style": cfg.get("order_style", "co")})
        source = "your --qty (check it against get_positions)"
    else:
        if not pos or int(pos.get("qty") or 0) <= 0:
            raise TicketError("nothing to exit: no %s position in %s. Check the broker (get_positions) and "
                              "pass --qty if you do hold some" % (sym, a.portfolio))
        qty = int(pos["qty"])
    plan = read_json(a.plan, "plan", required=False) or {}
    flat_by = flat_by_of(cfg, plan if str(plan.get("symbol", "")).upper() == sym else None)
    order = exit_ticket(sym, pos, qty, cfg, flat_by, source, a.stop_order_id)
    replaced = _write_exits(a, cfg, [order], now, today, "ticket.py exit %s on %s" % (sym, today))
    _print_exit_next(a, cfg, [order], replaced, flat_by, now)
    if a.qty is not None:
        print("The qty is your --qty. Check it against get_positions %s before you say yes." % sym)
    return 0


def cmd_flatten(a):
    cfg = read_json(a.config, "config", required=False) or {}
    now, today = _clock(a)
    pf = read_json(a.portfolio, "portfolio", required=False) or {}
    positions = {s: p for s, p in (pf.get("positions") or {}).items() if int(p.get("qty") or 0) > 0}
    if not positions:
        raise TicketError("nothing to flatten: no open positions in %s (check get_positions)" % a.portfolio)
    flat_by = flat_by_of(cfg)
    orders = [exit_ticket(sym, positions[sym], int(positions[sym]["qty"]), cfg, flat_by, a.portfolio)
              for sym in sorted(positions)]
    replaced = _write_exits(a, cfg, orders, now, today, "ticket.py flatten on %s" % today)
    _print_exit_next(a, cfg, orders, replaced, flat_by, now)
    return 0


def cmd_log(a):
    log = read_log(a.log)
    now, today = _clock(a)
    rows = [r for r in log if a.all or rec_day(r) == today]
    if not rows:
        print("No decisions recorded%s." % ("" if a.all else " today"))
        return 1
    for r in rows:
        extra = (" id " + str(r["kite_order_id"])) if r.get("kite_order_id") else ""
        reply = (' reply "%s"' % r["reply"]) if r.get("reply") and r["status"] in ("approved", "declined") else ""
        loss = (" max loss %s" % rupees(r["max_loss_inr"])) if r.get("max_loss_inr") is not None \
            and r["status"] in ("placed", "filled") else ""
        print("%s  %s %-9s %s%s%s%s" % (r.get("at"), r.get("order_hash", "?"), r.get("status"), r.get("plain"),
                                        reply, loss, extra))
    placed = sum(1 for r in rows if r.get("status") in ("placed", "filled") and r.get("kind") == "entry")
    print("%d entr%s placed%s." % (placed, "y" if placed == 1 else "ies", "" if a.all else " today"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Order tickets and your yes (trader-broker). Never places orders.")
    sub = ap.add_subparsers(dest="cmd")

    def clock(p):
        p.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-10-29 10:15\"")
        p.add_argument("--today", help="YYYY-MM-DD (default: today in IST)")

    def common(p):
        p.add_argument("--orders", default="trader/orders.json")
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--verdicts", default="trader/verdicts.json")
        p.add_argument("--log", default="trader/log.jsonl")
        p.add_argument("--portfolio", default="trader/portfolio.json")
        p.add_argument("--fills", default="trader/fills.json")
        clock(p)

    def fill_args(p):
        p.add_argument("--order-id", help="Kite's order id from the MCP result (live)")
        p.add_argument("--avg", help="average fill price (live)")
        p.add_argument("--parent-order-id", help="the cover order's first-leg id (live; defaults to --order-id)")
        p.add_argument("--stop-order-id", help="the cover order's stop leg id, from get_orders (live)")

    p = sub.add_parser("show", help="render the tickets")
    common(p)
    p.add_argument("--hash", help="one order by its hash")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("approve", help="record the user's explicit yes for one order")
    common(p)
    p.add_argument("--hash", required=True)
    p.add_argument("--yes", "--reply", dest="reply", required=True, help="the user's reply, word for word")
    p = sub.add_parser("decline", help="record a no")
    common(p)
    p.add_argument("--hash", required=True)
    p.add_argument("--reply")
    p = sub.add_parser("result", help="record what happened after the Allow once / Deny card")
    common(p)
    p.add_argument("--hash", required=True)
    p.add_argument("--status", required=True, choices=["placed", "denied", "failed"])
    p.add_argument("--note")
    p.add_argument("--filled-qty")
    fill_args(p)
    p = sub.add_parser("fill", help="record a fill (paper: simulated at Kite's last price)")
    common(p)
    p.add_argument("--hash", required=True)
    p.add_argument("--qty", help="filled quantity (live)")
    fill_args(p)
    p.add_argument("--offline", metavar="DIR", help="read DIR/kite-quote-SYMBOL.json instead of Kite")
    p = sub.add_parser("positions", help="what the portfolio holds")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--prices", default="trader/prices.json")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--json", action="store_true")
    clock(p)
    for name, help_text in (("exit", "an exit ticket that closes one symbol's position"),
                            ("flatten", "exit tickets for every open position")):
        p = sub.add_parser(name, help=help_text)
        if name == "exit":
            p.add_argument("symbol")
            p.add_argument("--qty", help="shares you hold, from get_positions (default: trader/portfolio.json)")
            p.add_argument("--stop-order-id", help="the cover order's stop leg id if the portfolio lacks it")
        p.add_argument("--orders", default="trader/orders.json", help="where the ticket is written")
        p.add_argument("--portfolio", default="trader/portfolio.json")
        p.add_argument("--plan", default="trader/plan.json")
        p.add_argument("--config", default="trader/config.json")
        clock(p)
    p = sub.add_parser("log", help="today's decisions")
    p.add_argument("--log", default="trader/log.jsonl")
    clock(p)
    p.add_argument("--all", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"show": cmd_show, "approve": cmd_approve, "decline": cmd_decline, "result": cmd_result,
                "fill": cmd_fill, "positions": cmd_positions, "exit": cmd_exit, "flatten": cmd_flatten,
                "log": cmd_log}[a.cmd](a)
    except TicketError as e:
        print("ticket.py: %s" % e, file=sys.stderr)
        return 1
    except kite.LoginNeeded as e:
        print("ticket.py: %s" % e, file=sys.stderr)
        return 1
    except kite.KiteError as e:
        print("ticket.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
