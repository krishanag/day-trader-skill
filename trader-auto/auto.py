#!/usr/bin/env python3
"""Auto Trader (trader-auto): an unattended intraday loop over the pack's own tools.

One `tick` every minute from a scheduler. Each tick reads the NSE clock, keeps
one state file for the day (trader/auto/<date>.json), and does one of:

  before 09:15      checks the Kite session and the live balance, nothing else
  09:15 to 09:35    waits for the 15-minute opening range and its first bar
  09:35 to 14:30    hunts: scan.py orb -> the skip rules -> entry.py check ->
                    exit.py plan -> timemachine.py run (the edge gate) ->
                    size.py --write -> check.py (PASS) -> the guards here ->
                    k.place_order(order["kite"]) -> the broker's ledger
  while holding     one quote a tick: target, flat-by, the kill switch, or the
                    cover order's stop leg filling at the exchange
  15:05 onward      exits anything still open; after 15:15 writes the summary

Every decision is one the tested skills already make; this file only adds the
orchestration, the hard limits, the order calls, the monitoring and the
report. It places orders WITHOUT a per-order yes: the limits in
trader/config.json replace it, and only when the user has set
"auto": {"enabled": true} and "paper": false themselves.

Subcommands: tick, status, report, probe, stop, go. Standard library only.
"""

import argparse
import datetime
import hashlib
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trader-kite"))
import kite   # noqa: E402
import nse    # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PACK = os.path.dirname(HERE)
TRADER = "trader"
AUTO_DIR = os.path.join(TRADER, "auto")
DEFAULT_CONFIG = os.path.join(TRADER, "config.json")
JOURNAL = os.path.join(AUTO_DIR, "journal.jsonl")
LEDGER = {"portfolio": os.path.join(TRADER, "portfolio.json"), "fills": os.path.join(TRADER, "fills.json"),
          "log": os.path.join(TRADER, "log.jsonl")}
WATCHLIST = os.path.join(TRADER, "watchlist.json")

TOOLS = {"scan": ("trader-scanner", "scan.py"), "entry": ("trader-entry", "entry.py"),
         "exit": ("trader-exit", "exit.py"), "timemachine": ("trader-timemachine", "timemachine.py"),
         "size": ("trader-budget", "size.py"), "check": ("trader-risk", "check.py"),
         "alerts": ("trader-alerts", "alerts.py"), "news": ("trader-news", "news.py"),
         "results": ("trader-results", "results.py")}
TOOL_TIMEOUT = 240            # seconds for one sub-tool (a whole-universe orb stage 2 is about 70 s)
BAR_MINUTES = 5
FIRST_HUNT = datetime.time(9, 35)      # the opening range plus its first bar
HARD_OPEN = nse.OPEN                   # 09:15: no order call before it
HARD_CLOSE = nse.CAS_CONTINUOUS_END    # 15:15: no order call from it
MAX_BAR_AGE = 2                        # a trigger older than this many bars is a different trade
ORDER_WAIT = 30                        # seconds to wait for a fill
PROBE_HOLD = 10                        # seconds the probe holds its share
PAPER_PREFIX = "PAPER-"
FINAL = ("COMPLETE", "REJECTED", "CANCELLED")
REAL_FLAG = "--i-understand-this-is-real"

AUTO_DEFAULTS = {"enabled": False, "target": "1R", "stop": "or-mid", "min_edge": 0.0, "max_candidates": 8,
                 "max_confirm": 60, "tick_seconds": 60, "kill_file": os.path.join(AUTO_DIR, "STOP")}
CFG_DEFAULTS = {"budget_inr": 0.0, "cash_buffer_pct": 10, "max_loss_per_trade_inr": 0.0,
                "max_daily_loss_inr": 0.0, "max_trades_per_day": 1, "min_order_inr": 500,
                "no_entries_after": "14:30", "flat_by": "15:05", "use_mis_leverage": False,
                "mis_leverage_x": 1.0, "paper": True, "blocked": []}

_sleep = time.sleep   # tests swap it


class AutoError(Exception):
    """Cannot run; the message is one plain line for the user."""


# ---------------------------------------------------------------- clock + files

def parse_now(text):
    """'2026-10-29 10:15' (IST) or ISO with an offset -> aware IST datetime. None = now."""
    if not text:
        return nse.now_ist().replace(microsecond=0)
    try:
        dt = datetime.datetime.fromisoformat(text.strip())
    except ValueError:
        raise AutoError('--now wants an IST time like "2026-10-29 10:15"')
    return nse.to_ist(dt).replace(microsecond=0)


def iso(now):
    return now.isoformat()


def hhmm(t):
    return "%02d:%02d" % (t.hour, t.minute)


def now_arg(now):
    return now.strftime("%Y-%m-%d %H:%M")


def parse_hhmm(text, what):
    try:
        return nse.parse_hhmm(text)
    except ValueError:
        raise AutoError("%s %r is not a time like 15:05" % (what, text))


def read_json(path, required=False, what="file"):
    if not os.path.exists(path):
        if required:
            raise AutoError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise AutoError("%s is not valid JSON: %s" % (path, e))


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def append_line(path, rec):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def r2(x):
    return round(float(x) + 0.0, 2)


def money(v):
    return "Rs {:,.2f}".format(float(v))


# ---------------------------------------------------------------- config

def load_config(path, need_auto=True):
    """trader/config.json with the auto block filled in. Refuses unless auto.enabled is true."""
    raw = read_json(path, required=True, what="config (copy trader-auto/config.auto.example.json)")
    if not isinstance(raw, dict):
        raise AutoError("%s is not a JSON object" % path)
    cfg = dict(CFG_DEFAULTS)
    cfg.update(raw)
    auto = dict(AUTO_DEFAULTS)
    auto.update(raw.get("auto") or {})
    cfg["auto"] = auto
    if need_auto and auto.get("enabled") is not True:
        raise AutoError("%s: \"auto\": {\"enabled\": true} is not set, so the Auto Trader will not run. "
                        "Set it yourself when you mean it." % path)
    for key in ("budget_inr", "max_loss_per_trade_inr", "max_daily_loss_inr"):
        if not (_f(cfg.get(key)) or 0) > 0:
            raise AutoError("%s: %s must be a positive number of rupees" % (path, key))
    if int(cfg.get("max_trades_per_day") or 0) < 1:
        raise AutoError("%s: max_trades_per_day must be at least 1" % path)
    cfg["_no_entries_after"] = parse_hhmm(cfg["no_entries_after"], "no_entries_after")
    cfg["_flat_by"] = parse_hhmm(cfg["flat_by"], "flat_by")
    if cfg["_flat_by"] >= nse.MIS_SQUARE_OFF_CAS:
        raise AutoError("%s: flat_by %s must be before Zerodha's %s MIS square-off"
                        % (path, cfg["flat_by"], hhmm(nse.MIS_SQUARE_OFF_CAS)))
    return cfg


def leverage(cfg):
    if cfg.get("use_mis_leverage"):
        return max(_f(cfg.get("mis_leverage_x")) or 1.0, 1.0)
    return 1.0


def limits_of(cfg):
    return {"max_loss_per_trade_inr": _f(cfg["max_loss_per_trade_inr"]),
            "max_daily_loss_inr": _f(cfg["max_daily_loss_inr"]),
            "max_trades_per_day": int(cfg["max_trades_per_day"]), "one_position": True,
            "mis_leverage_x": leverage(cfg), "no_entries_after": cfg["no_entries_after"],
            "flat_by": cfg["flat_by"], "kill_file": cfg["auto"]["kill_file"], "paper": bool(cfg.get("paper"))}


# ---------------------------------------------------------------- state

def state_path(day):
    return os.path.join(AUTO_DIR, "%s.json" % day.isoformat())


def new_state(day, cfg, now):
    return {"created_by": "trader-auto", "date": day.isoformat(), "created_at": iso(now), "updated_at": iso(now),
            "phase": "pre-open", "paper": bool(cfg.get("paper")), "limits": limits_of(cfg),
            "live_balance_inr": None, "kill": False, "login_needed": False,
            "candidates": [], "skipped": {}, "position": None, "trades": [], "traded": [],
            "trades_done": 0, "realised_inr": 0.0, "charges_inr": 0.0, "net_inr": 0.0,
            "decisions": [], "summary": None, "journal_written": False}


def load_state(day, cfg, now):
    st = read_json(state_path(day))
    if st is None:
        return new_state(day, cfg, now)
    if st.get("date") != day.isoformat():
        raise AutoError("%s is for %s, not %s" % (state_path(day), st.get("date"), day.isoformat()))
    return st


def save_state(st, now):
    st["updated_at"] = iso(now)
    write_json(state_path(datetime.date.fromisoformat(st["date"])), st)


def decide(st, now, event, why, symbol=None, **numbers):
    rec = {"at": iso(now), "phase": st["phase"], "event": event, "why": why}
    if symbol:
        rec["symbol"] = symbol
    if numbers:
        rec["numbers"] = numbers
    st["decisions"].append(rec)
    print("%s  %s%s: %s" % (now.strftime("%H:%M:%S"), event, (" " + symbol) if symbol else "", why))
    return rec


def skip(st, now, symbol, reason, retry=False, **numbers):
    """Record a skipped candidate once per reason; the decisions log gets it when the reason changes."""
    prev = st["skipped"].get(symbol)
    if prev and prev["reason"] == reason:
        prev["times"] += 1
        prev["last_at"] = iso(now)
        prev["retry"] = retry
        return
    st["skipped"][symbol] = {"reason": reason, "first_at": iso(now), "last_at": iso(now), "times": 1,
                             "retry": retry}
    decide(st, now, "skip", reason, symbol=symbol, **numbers)


def net_so_far(st):
    return r2(st["realised_inr"] - st["charges_inr"])


# ---------------------------------------------------------------- the sub-tools

def run_tool(tool, args, timeout=TOOL_TIMEOUT):
    """python3 <sibling>/<tool>.py args -> (exit code, stdout, stderr). Tests swap this."""
    folder, script = TOOLS[tool]
    cmd = [sys.executable, os.path.join(PACK, folder, script)] + [str(x) for x in args]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", "%s timed out after %d s" % (script, timeout)
    except OSError as e:
        return 127, "", "%s could not start: %s" % (script, e)
    return p.returncode, p.stdout, p.stderr


def tool_json(tool, args):
    """(code, parsed JSON or None, one line of the tool's own words)."""
    code, out, err = run_tool(tool, args)
    doc = None
    text = out.strip()
    if text.startswith(("{", "[")):
        try:
            doc = json.loads(text)
        except ValueError:
            doc = None
    words = (err.strip() or (text if doc is None else "")).strip()
    last = words.splitlines()[-1] if words else ""
    return code, doc, last[:300]


def connect_kite(now):
    """Today's session. Tests swap this for a fake."""
    return kite.Kite.connect(now)


# ---------------------------------------------------------------- the ledger (trader-broker's shapes)

def order_hash(order):
    body = json.dumps({"kite": order["kite"], "stop_leg": order.get("stop_leg")}, sort_keys=True,
                      separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()[:12]


def apply_fill(cfg, symbol, side, qty, price, at, parent_order_id=None, stop_order_id=None):
    """trader/portfolio.json exactly as trader-broker keeps it. Returns (portfolio, realised)."""
    pf = read_json(LEDGER["portfolio"]) or {"cash_inr": r2(cfg["budget_inr"]), "positions": {},
                                            "paper": bool(cfg.get("paper", True))}
    qty = int(qty)
    cost = r2(qty * price)
    positions = dict(pf.get("positions") or {})
    pos = dict(positions.get(symbol) or {})
    realised = 0.0
    if side == "buy":
        held = int(pos.get("qty") or 0)
        avg = _f(pos.get("avg")) or 0.0
        new_qty = held + qty
        pos.update({"qty": new_qty, "avg": r2((held * avg + qty * price) / new_qty), "product": "MIS",
                    "order_style": "co", "entered_at": pos.get("entered_at") or at})
        if parent_order_id:
            pos["parent_order_id"] = parent_order_id
        if stop_order_id:
            pos["stop_order_id"] = stop_order_id
        positions[symbol] = pos
        cash = r2(float(pf.get("cash_inr", 0)) - cost)
    else:
        held = int(pos.get("qty") or 0)
        if held < qty:
            raise AutoError("cannot record selling %d %s: the ledger holds %d" % (qty, symbol, held))
        realised = r2((price - (_f(pos.get("avg")) or 0.0)) * qty)
        left = held - qty
        if left:
            pos["qty"] = left
            positions[symbol] = pos
        else:
            positions.pop(symbol, None)
        cash = r2(float(pf.get("cash_inr", 0)) + cost)
    out = dict(pf, cash_inr=cash, positions=positions, paper=bool(cfg.get("paper", True)), updated_at=at)
    write_json(LEDGER["portfolio"], out)
    return out, realised


def log_line(now, today, status, h, order, cfg, **extra):
    rec = {"at": iso(now), "date": today, "status": status, "order_hash": h, "symbol": order["symbol"],
           "side": order["side"], "qty": int(order["qty"]), "kind": order.get("kind", "entry"),
           "max_loss_inr": (order.get("plan") or {}).get("max_loss_inr"), "stop": order.get("stop"),
           "paper": bool(cfg.get("paper", True)), "broker": "zerodha", "auto": True}
    rec.update(extra)
    append_line(LEDGER["log"], rec)
    return rec


def record_fill(cfg, now, today, order, h, kite_order_id, qty, price, parent_order_id=None,
                stop_order_id=None, note=None):
    """portfolio.json, fills.json and a 'filled' log line, in trader-broker's order and shapes."""
    sym = order["symbol"]
    side = order["side"]
    pf, realised = apply_fill(cfg, sym, side, qty, price, iso(now), parent_order_id, stop_order_id)
    fill = {"order_hash": h, "kite_order_id": kite_order_id, "symbol": sym, "side": side,
            "filled_qty": int(qty), "average_price": r2(price), "at": iso(now), "paper": bool(cfg.get("paper", True)),
            "auto": True}
    if parent_order_id:
        fill["parent_order_id"] = parent_order_id
    if side == "sell":
        fill["realized_inr"] = realised
    fills = read_json(LEDGER["fills"]) or []
    write_json(LEDGER["fills"], fills + [fill])
    extra = {"price": r2(price), "kite_order_id": kite_order_id,
             "plain": "%s %d %s at %.2f" % (side.upper(), int(qty), sym, price)}
    if note:
        extra["note"] = note
    log_line(now, today, "filled", h, order, cfg, **extra)
    return pf, realised


def exit_order(pos):
    return {"symbol": pos["symbol"], "exchange": "NSE", "side": "sell", "qty": int(pos["qty"]), "product": "MIS",
            "order_style": "co", "kind": "exit", "stop_leg": None, "stop": None, "target": None,
            "plan": {"entry": pos["entry_price"], "flat_by": pos["flat_by"], "max_loss_inr": 0.0},
            "kite": {"variety": "co", "parent_order_id": pos["parent_order_id"], "order_id": pos["stop_order_id"]}}


def estimate_charges(buy_value, sell_value):
    fn = getattr(kite, "estimate_charges", None)
    if fn is None:
        return None
    try:
        return fn(buy_value, sell_value, product="MIS")
    except Exception as e:   # the estimate must never stop an exit from being recorded
        return {"error": str(e)}


# ---------------------------------------------------------------- the session and the balance

def live_balance(k):
    data = k.margins() or {}
    bal = _f((data.get("available") or {}).get("live_balance"))
    if bal is None:
        raise kite.KiteError("Kite's margins call had no available.live_balance")
    return r2(bal)


def session(st, now, cfg):
    """The Kite client, or None after writing why. Checks the balance on the first tick of the day."""
    try:
        k = connect_kite(now)
    except kite.LoginNeeded as e:
        if not st.get("login_needed"):
            decide(st, now, "login-needed", str(e))
        st["login_needed"] = True
        return None
    st["login_needed"] = False
    if st.get("live_balance_inr") is None:
        try:
            st["live_balance_inr"] = live_balance(k)
        except kite.LoginNeeded as e:
            decide(st, now, "login-needed", str(e))
            st["login_needed"] = True
            return None
        except kite.KiteError as e:
            decide(st, now, "balance", "could not read the live balance: %s" % e)
            return k
        decide(st, now, "session", "Kite session is live; %s balance %s" % (
            "paper mode, the Kite" if cfg.get("paper") else "live", money(st["live_balance_inr"])),
            live_balance_inr=st["live_balance_inr"])
        if not cfg.get("paper") and st["live_balance_inr"] < float(cfg["min_order_inr"]):
            decide(st, now, "refuse", "live balance %s is under the %s minimum order: no entries today"
                   % (money(st["live_balance_inr"]), money(cfg["min_order_inr"])))
            st["phase"] = "done"
    return k


def within_hours(now):
    t = nse.to_ist(now).time()
    return HARD_OPEN <= t < HARD_CLOSE


# ---------------------------------------------------------------- the hunt

def bars_done(now):
    day = nse.to_ist(now).date()
    return int((nse.to_ist(now) - nse.at(day, nse.OPEN)).total_seconds() // (BAR_MINUTES * 60))


def scanner_args(cfg, now, config_path):
    """orb over the daily watchlist when today's daily run exists, else the universe (stage 2 capped)."""
    args = ["orb", "--json", "--no-write", "--now", now_arg(now), "--config", config_path]
    wl = read_json(WATCHLIST)
    if isinstance(wl, dict) and wl.get("mode") == "daily" and str(wl.get("as_of", ""))[:10] == now.date().isoformat():
        return args, "today's daily watchlist (%d hits)" % len(wl.get("hits") or [])
    return args + ["--universe", "--max-confirm", int(cfg["auto"]["max_confirm"])], \
        "the universe, stage 2 capped at %d" % int(cfg["auto"]["max_confirm"])


def affordable(cfg, st):
    """Rupees one order may reach: cash after the buffer, times the MIS leverage."""
    budget = float(cfg["budget_inr"])
    cash = budget
    if not cfg.get("paper") and st.get("live_balance_inr") is not None:
        cash = min(budget, float(st["live_balance_inr"]))
    pf = read_json(LEDGER["portfolio"])
    if cfg.get("paper") and isinstance(pf, dict) and _f(pf.get("cash_inr")) is not None:
        cash = min(cash, float(pf["cash_inr"]))
    buffer = budget * float(cfg.get("cash_buffer_pct") or 0) / 100.0
    return r2(max(cash - buffer, 0.0) * leverage(cfg))


def cheap_skip(sym, hit, st, cfg, now):
    """The reasons that need no network call: blocked, traded today, stale, unaffordable."""
    if sym in [str(s).upper() for s in cfg.get("blocked") or []]:
        return "on your blocked list"
    if sym in st["traded"]:
        return "already traded today (one trade per symbol per day)"
    at = hit.get("at")
    if at:
        try:
            trig = nse.at(now.date(), nse.parse_hhmm(at))
        except ValueError:
            trig = None
        if trig is not None:
            age = bars_done(now) - bars_done(trig)
            if age > MAX_BAR_AGE:
                return "the breakout bar closed at %s, %d bars ago (more than %d): a late entry is a different trade" \
                    % (at, age, MAX_BAR_AGE)
    price = _f(hit.get("price"))
    cap = affordable(cfg, st)
    if price and price > cap:
        return "one share at %s is more than the %s your cash allows (after the buffer, x%s leverage)" \
            % (money(price), money(cap), ("%g" % leverage(cfg)))
    return None


def results_skip(sym, now):
    """results.py calendar: a filing today, or a results date today or tomorrow."""
    code, cal, words = tool_json("results", ["calendar", sym, "--json", "--now", now_arg(now)])
    if not isinstance(cal, dict):
        return None, "results calendar could not run (%s): %s" % (code, words or "no output")
    today = now.date()
    tomorrow = today + datetime.timedelta(days=1)
    items = ((cal.get("bse") or {}).get("items") or [])
    if items:
        return "a results / board-meeting filing on BSE today (%s)" % (items[0].get("text") or "")[:80], None
    likely = cal.get("likely") or {}
    when = likely.get("date")
    if when in (today.isoformat(), tomorrow.isoformat()):
        return "results headlines name %s (%d mentions): not held into a result" % (when, likely.get("mentions") or 0), None
    return None, None


def news_skip(sym, now):
    """news.py: an item tagged halt (circuit, suspension, F&O ban) that touches the symbol."""
    code, doc, words = tool_json("news", ["--tickers", sym, "--json", "--dry-run", "--no-market", "--no-moves",
                                          "--hours", "24", "--now", now_arg(now)])
    if not isinstance(doc, dict):
        return None, "news check could not run (%s): %s" % (code, words or "no output")
    for it in doc.get("items") or []:
        if "halt" in (it.get("tags") or []) and (it.get("symbol") == sym or sym in (it.get("touches") or [])):
            return "news tagged halt: %s" % (it.get("title") or "")[:100], None
    return None, None


def entry_check(sym, now, config_path):
    code, res, words = tool_json("entry", ["check", sym, "--json", "--write", "--now", now_arg(now),
                                           "--config", config_path])
    if not isinstance(res, dict):
        return None, "entry check could not run (%s): %s" % (code, words or "no output"), True
    if res.get("status") != "triggered":
        return None, "entry %s: %s" % (res.get("status"), res.get("reason") or ""), False
    trig = res.get("trigger") or {}
    age = bars_done(now) - int(trig.get("bar") or 0)
    if age > MAX_BAR_AGE:
        return None, "triggered at %s, %d bars ago (more than %d)" % (trig.get("time"), age, MAX_BAR_AGE), False
    return res, None, False


def exit_plan(cfg, now, config_path):
    auto = cfg["auto"]
    code, plan, words = tool_json("exit", ["plan", "--stop", auto["stop"], "--target", auto["target"], "--json",
                                           "--write", "--now", now_arg(now), "--config", config_path])
    if code != 0 or not isinstance(plan, dict):
        return None, "exit plan refused: %s" % (words or "no plan")
    return plan, None


def odds_of(doc):
    mc = doc.get("mc") or {}
    return {"p_target": mc.get("p_target"), "p_stop": mc.get("p_stop"), "p_neither": mc.get("p_neither"),
            "p_green": mc.get("p_green"), "avg_R": mc.get("avg_R"), "seed": mc.get("seed"),
            "replays": mc.get("replays"), "verdict": doc.get("verdict"),
            "history": {k: (doc.get("history") or {}).get(k) for k in ("sessions", "triggered", "target", "stop")}}


def timemachine_gate(cfg, now):
    code, doc, words = tool_json("timemachine", ["run", "--json", "--write", "--now", now_arg(now)])
    if code != 0 or not isinstance(doc, dict):
        return None, "time machine could not run: %s" % (words or "no result")
    odds = odds_of(doc)
    pt, ps = _f(odds["p_target"]), _f(odds["p_stop"])
    if pt is None or ps is None:
        return None, "time machine gave no odds"
    edge = round(pt - ps, 4)
    odds["edge"] = edge
    if edge < float(cfg["auto"]["min_edge"]):
        return odds, "time machine gate: target first %.1f %% minus stop first %.1f %% = %+.1f points, under min_edge %+.1f (%s)" \
            % (100 * pt, 100 * ps, 100 * edge, 100 * float(cfg["auto"]["min_edge"]), odds["verdict"])
    return odds, None


def size_order(now, config_path):
    code, doc, words = tool_json("size", ["--write", "--json", "--now", now_arg(now), "--config", config_path])
    orders = (doc or {}).get("orders") if isinstance(doc, dict) else None
    if code != 0 or not orders:
        why = ((doc or {}).get("sizing") or {}).get("refusal") if isinstance(doc, dict) else None
        return None, "budget refused: %s" % (why or words or "no order")
    return orders[0], None


def risk_check(now, config_path):
    code, res, words = tool_json("check", ["--json", "--now", now_arg(now), "--config", config_path])
    if not isinstance(res, dict):
        return None, "risk check could not run (%s): %s" % (code, words or "no output")
    first = (res.get("results") or [{}])[0]
    if code != 0 or first.get("verdict") != "PASS" or res.get("block"):
        return res, "risk BLOCK: %s" % "; ".join(first.get("reasons") or [words or "blocked"])
    return res, None


def guard_order(order, st, cfg, k, now):
    """The limits enforced here as well, with the numbers. Returns the reason, or None."""
    body = order.get("kite") or {}
    plan = order.get("plan") or {}
    qty = int(order.get("qty") or 0)
    lim = st["limits"]
    if body.get("transaction_type") != "BUY" or order.get("side") != "buy":
        return "not a BUY: the runner never sells except to exit what it holds"
    if body.get("variety") != "co" or body.get("product") != "MIS" or not _f(body.get("trigger_price")):
        return "not an MIS cover order with a stop trigger"
    if qty < 1:
        return "quantity %d" % qty
    if st["position"]:
        return "a position is already open (%s): one at a time" % st["position"]["symbol"]
    if st["trades_done"] >= lim["max_trades_per_day"]:
        return "%d of %d trades done today" % (st["trades_done"], lim["max_trades_per_day"])
    loss = _f(plan.get("max_loss_inr"))
    if loss is None:
        return "the order carries no max loss"
    if loss > lim["max_loss_per_trade_inr"] + 0.005:
        return "max loss %s is over the %s per trade" % (money(loss), money(lim["max_loss_per_trade_inr"]))
    worst = r2(net_so_far(st) - loss)
    if worst < -lim["max_daily_loss_inr"] - 0.005:
        return "net so far %s minus this trade's max loss %s = %s would breach the %s daily cap" \
            % (money(net_so_far(st)), money(loss), money(worst), money(lim["max_daily_loss_inr"]))
    if st["kill"]:
        return "kill switch %s is present" % lim["kill_file"]
    if not within_hours(now):
        return "outside %s to %s" % (hhmm(HARD_OPEN), hhmm(HARD_CLOSE))
    if not cfg.get("paper"):
        need = r2(qty * float(plan.get("entry") or 0) / leverage(cfg))
        try:
            bal = live_balance(k)
        except kite.KiteError as e:
            return "could not read the live balance before the order: %s" % e
        st["live_balance_inr"] = bal
        if bal < need:
            return "live balance %s is under the %s this order needs (%d x %.2f / %gx)" \
                % (money(bal), money(need), qty, float(plan.get("entry") or 0), leverage(cfg))
    return None


def place_entry(k, order, st, cfg, now, dry_run, odds, entry_res, plan):
    """The order call, the wait, the ledger and the state. Returns True when a position is on."""
    sym = order["symbol"]
    h = order_hash(order)
    today = now.date().isoformat()
    body = order["kite"]
    if dry_run:
        decide(st, now, "dry-run", "would place %s; nothing sent" % json.dumps(body, sort_keys=True), symbol=sym,
               order_hash=h, max_loss_inr=order["plan"].get("max_loss_inr"))
        return True   # the pass ends here: one "would place" per tick, nothing held
    paper = bool(cfg.get("paper"))
    log_line(now, today, "approved", h, order, cfg, reply="auto", limits=st["limits"],
             plain="BUY %d %s, cover order, stop %.2f, target %.2f" % (order["qty"], sym, order["stop"], order["target"]))
    if paper:
        q = (k.quotes([sym]) or {}).get(sym) or {}
        price = _f(q.get("last_price"))
        if not price:
            decide(st, now, "failed", "paper fill: Kite has no last price", symbol=sym)
            log_line(now, today, "failed", h, order, cfg, note="no last price")
            return False
        parent, leg, qty = PAPER_PREFIX + h, PAPER_PREFIX + h + "-SL", int(order["qty"])
        log_line(now, today, "placed", h, order, cfg, kite_order_id=parent)
    else:
        try:
            parent = str(k.place_order(body))
        except kite.KiteError as e:
            decide(st, now, "failed", "place_order: %s" % e, symbol=sym, order_hash=h)
            log_line(now, today, "failed", h, order, cfg, note=str(e))
            skip(st, now, sym, "place_order failed: %s" % e)     # final for the day: never re-sent each tick
            return False
        log_line(now, today, "placed", h, order, cfg, kite_order_id=parent)
        decide(st, now, "placed", "order %s sent: %s" % (parent, json.dumps(body, sort_keys=True)), symbol=sym)
        rec = wait(k, parent)
        status = (rec or {}).get("status")
        if status in ("REJECTED", "CANCELLED"):
            why = "order %s %s: %s" % (parent, status, (rec or {}).get("status_message") or "")
            decide(st, now, "failed", why, symbol=sym)
            log_line(now, today, "failed", h, order, cfg, kite_order_id=parent, note=status)
            skip(st, now, sym, why)                                # final for the day
            return False
        if status != "COMPLETE":
            st["position"] = pending_position(order, h, parent, now, odds, entry_res, plan, cfg)
            decide(st, now, "pending", "order %s is %s after %d s; the next tick checks it" % (
                parent, status or "unknown", ORDER_WAIT), symbol=sym)
            st["phase"] = "holding"
            return True
        price = _f(rec.get("average_price"))
        qty = int(_f(rec.get("filled_quantity")) or order["qty"])
        leg = second_leg_id(k, parent)
    open_position(k, st, cfg, now, order, h, parent, leg, qty, price, odds, entry_res, plan)
    return True


def pending_position(order, h, parent, now, odds, entry_res, plan, cfg):
    return {"symbol": order["symbol"], "qty": int(order["qty"]), "pending": True, "order_hash": h,
            "parent_order_id": parent, "stop_order_id": None, "planned_entry": plan.get("entry"),
            "entry_price": None, "stop": order["stop"], "target": order["target"], "flat_by": cfg["flat_by"],
            "entered_at": iso(now), "trigger_time": (entry_res.get("trigger") or {}).get("time"),
            "entry_bar": (entry_res.get("trigger") or {}).get("bar"), "setup": plan.get("setup"),
            "max_loss_inr": order["plan"].get("max_loss_inr"), "odds": odds, "order": order, "paper": False}


def open_position(k, st, cfg, now, order, h, parent, leg, qty, price, odds, entry_res, plan):
    sym = order["symbol"]
    today = now.date().isoformat()
    record_fill(cfg, now, today, order, h, parent, qty, price, parent, leg)
    pos = pending_position(order, h, parent, now, odds, entry_res, plan, cfg)
    pos.update({"pending": False, "stop_order_id": leg, "entry_price": r2(price), "qty": qty,
                "paper": bool(cfg.get("paper")), "filled_at": iso(now)})
    st["position"] = pos
    st["traded"].append(sym)
    st["phase"] = "holding"
    slip = r2(price - float(plan.get("entry") or price))
    decide(st, now, "entry", "%s %d %s at %.2f (plan %.2f, slippage %+.2f a share), stop %.2f, target %.2f, "
           "parent %s, stop leg %s" % ("PAPER" if cfg.get("paper") else "FILLED", qty, sym, price,
                                       float(plan.get("entry") or price), slip, order["stop"], order["target"],
                                       parent, leg or "?"), symbol=sym,
           qty=qty, price=r2(price), stop=order["stop"], target=order["target"],
           max_loss_inr=order["plan"].get("max_loss_inr"), p_target=odds.get("p_target"), p_stop=odds.get("p_stop"))
    arm_alerts(st, now)


def arm_alerts(st, now):
    code, out, err = run_tool("alerts", ["add", "--from-plan", "--now", now_arg(now)])
    if code != 0:
        decide(st, now, "alerts", "alerts add --from-plan failed: %s" % (err.strip() or out.strip())[-200:])


def wait(k, order_id):
    try:
        return k.wait_for_status(order_id, wanted=FINAL, timeout=ORDER_WAIT, every=1.0) or {}
    except kite.KiteError as e:
        return {"status": "UNKNOWN", "status_message": str(e)}


def second_leg_id(k, parent):
    try:
        legs = k.cover_order_legs(parent) or {}
    except kite.KiteError:
        return None
    leg = legs.get("second_leg") or {}
    return str(leg.get("order_id")) if leg.get("order_id") else None


def hunt(k, st, cfg, now, a):
    """One pass over the scanner's hits, in its order, until an order is placed."""
    args, where = scanner_args(cfg, now, a.config)
    code, doc, words = tool_json("scan", args)
    hits = (doc or {}).get("hits") if isinstance(doc, dict) else None
    if hits is None:
        decide(st, now, "scan", "scanner gave nothing (%s): %s" % (code, words or "no output"))
        return
    st["candidates"] = [{"symbol": h["symbol"], "at": h.get("at"), "price": h.get("price")} for h in hits]
    limit = int(cfg["auto"]["max_candidates"])
    todo = [h for h in hits if not (h["symbol"] in st["skipped"] and not st["skipped"][h["symbol"]]["retry"])]
    if not hits or not todo:
        if st.get("_said_empty") != len(hits):
            decide(st, now, "scan", "%d hit(s) from %s, none left to try" % (len(hits), where))
            st["_said_empty"] = len(hits)
        return
    tried = 0
    for h in todo:
        sym = h["symbol"]
        reason = cheap_skip(sym, h, st, cfg, now)   # free: every hit gets these
        if reason:
            skip(st, now, sym, reason, price=h.get("price"))
            continue
        if tried >= limit:                           # the network-heavy checks: at most max_candidates a tick
            break
        tried += 1
        if try_candidate(k, st, cfg, now, a, sym):
            return
    decide(st, now, "hunt", "%d of %d hit(s) tried, no entry" % (tried, len(hits)))


def try_candidate(k, st, cfg, now, a, sym):
    """Entry -> filings -> news -> plan -> time machine -> size -> risk -> guards -> place."""
    res, reason, retry = entry_check(sym, now, a.config)
    if reason:
        skip(st, now, sym, reason, retry=retry)
        return False
    for check in (results_skip, news_skip):
        reason, problem = check(sym, now)
        if problem:
            decide(st, now, "note", problem, symbol=sym)
        if reason:
            skip(st, now, sym, reason)
            return False
    plan, reason = exit_plan(cfg, now, a.config)
    if reason:
        skip(st, now, sym, reason)
        return False
    odds, reason = timemachine_gate(cfg, now)
    if reason:
        skip(st, now, sym, reason, **({"edge": odds["edge"], "verdict": odds["verdict"]} if odds else {}))
        return False
    order, reason = size_order(now, a.config)
    if reason:
        skip(st, now, sym, reason)
        return False
    if order.get("symbol") != sym:
        skip(st, now, sym, "the sized order is for %s, not %s" % (order.get("symbol"), sym))
        return False
    risk, reason = risk_check(now, a.config)
    if reason:
        skip(st, now, sym, reason, retry=risk is None)
        return False
    reason = guard_order(order, st, cfg, k, now)
    if reason:
        decide(st, now, "refuse", reason, symbol=sym, qty=order.get("qty"),
               max_loss_inr=(order.get("plan") or {}).get("max_loss_inr"))
        skip(st, now, sym, "guard: " + reason)
        return False
    return place_entry(k, order, st, cfg, now, a.dry_run, odds, res, plan)


# ---------------------------------------------------------------- holding

def manage(k, st, cfg, now, dry_run):
    pos = st["position"]
    if pos.get("pending"):
        return resolve_pending(k, st, cfg, now)
    sym = pos["symbol"]
    if not pos.get("paper"):
        done = exchange_side_exit(k, st, cfg, now)
        if done:
            return
    try:
        q = (k.quotes([sym]) or {}).get(sym) or {}
    except kite.KiteError as e:
        decide(st, now, "quote", "no quote for %s: %s" % (sym, e), symbol=sym)
        return
    last = _f(q.get("last_price"))
    if not last:
        decide(st, now, "quote", "Kite has no last price for %s" % sym, symbol=sym)
        return
    pos["last"] = last
    pos["last_at"] = iso(now)
    reason = None
    if st["kill"]:
        reason = "kill switch"
    elif last >= float(pos["target"]):
        reason = "target"
    elif pos.get("paper") and last <= float(pos["stop"]):
        reason = "stop"
    elif now.time() >= cfg["_flat_by"]:
        reason = "flat-by"
    if not reason:
        return
    if dry_run:
        decide(st, now, "dry-run", "would exit %s (%s) at about %.2f; nothing sent" % (sym, reason, last), symbol=sym)
        return
    if pos.get("paper"):
        close_position(st, cfg, now, pos, last, reason, pos["stop_order_id"], trigger=last)
        return
    if not within_hours(now):
        decide(st, now, "refuse", "exit wanted (%s) but it is outside %s to %s: Zerodha squares off MIS itself"
               % (reason, hhmm(HARD_OPEN), hhmm(HARD_CLOSE)), symbol=sym)
        return
    try:
        exit_id = str(k.exit_cover_order(pos["parent_order_id"]))
    except kite.KiteError as e:
        decide(st, now, "failed", "exit_cover_order: %s" % e, symbol=sym)
        return
    decide(st, now, "exit-sent", "%s: exit_cover_order(%s) -> %s" % (reason, pos["parent_order_id"], exit_id),
           symbol=sym, last=last)
    rec = wait(k, exit_id)
    if rec.get("status") == "COMPLETE":
        close_position(st, cfg, now, pos, _f(rec.get("average_price")) or last, reason, exit_id, trigger=last,
                       qty=int(_f(rec.get("filled_quantity")) or pos["qty"]))
    else:
        pos["exit_pending"] = {"order_id": exit_id, "reason": reason, "trigger": last, "status": rec.get("status")}
        decide(st, now, "pending", "exit %s is %s; the next tick checks it" % (exit_id, rec.get("status")), symbol=sym)


def resolve_pending(k, st, cfg, now):
    pos = st["position"]
    sym = pos["symbol"]
    try:
        hist = k.order_history(pos["parent_order_id"]) or []
    except kite.KiteError as e:
        decide(st, now, "pending", "order_history: %s" % e, symbol=sym)
        return
    rec = hist[-1] if hist else {}
    status = rec.get("status")
    if status == "COMPLETE":
        leg = second_leg_id(k, pos["parent_order_id"])
        open_position(k, st, cfg, now, pos["order"], pos["order_hash"], pos["parent_order_id"], leg,
                      int(_f(rec.get("filled_quantity")) or pos["qty"]), _f(rec.get("average_price")),
                      pos["odds"], {"trigger": {"time": pos["trigger_time"], "bar": pos["entry_bar"]}},
                      {"entry": pos["planned_entry"], "setup": pos["setup"]})
    elif status in ("REJECTED", "CANCELLED"):
        decide(st, now, "failed", "order %s %s: %s" % (pos["parent_order_id"], status, rec.get("status_message") or ""),
               symbol=sym)
        log_line(now, now.date().isoformat(), "failed", pos["order_hash"], pos["order"], cfg,
                 kite_order_id=pos["parent_order_id"], note=status)
        st["position"] = None
        st["phase"] = "hunting"
    else:
        decide(st, now, "pending", "order %s still %s" % (pos["parent_order_id"], status or "unknown"), symbol=sym)


def exchange_side_exit(k, st, cfg, now):
    """The stop leg filled, our pending exit filled, or Zerodha squared it off: record it. True when closed.

    cover_order_legs() shows the second leg only while it is open, so the leg's
    own order_history is what says COMPLETE (the stop hit, or our exit filled)
    or CANCELLED (Zerodha's square-off cancels the leg and sells at market)."""
    pos = st["position"]
    sym = pos["symbol"]
    if not pos.get("stop_order_id"):
        pos["stop_order_id"] = second_leg_id(k, pos["parent_order_id"])
        if not pos["stop_order_id"]:
            decide(st, now, "legs", "the cover order's second leg is not in the order book yet", symbol=sym)
            return False
    leg_id = str(pos["stop_order_id"])
    try:
        hist = k.order_history(leg_id) or []
    except kite.KiteError as e:
        decide(st, now, "legs", "order_history(%s): %s" % (leg_id, e), symbol=sym)
        return False
    leg = hist[-1] if hist else {}
    status = str(leg.get("status") or "").upper()
    pend = pos.get("exit_pending")
    if status == "COMPLETE":
        price = _f(leg.get("average_price")) or float(pos["stop"])
        qty = int(_f(leg.get("filled_quantity")) or pos["qty"])
        if pend:
            close_position(st, cfg, now, pos, price, pend["reason"], leg_id, trigger=pend["trigger"], qty=qty)
        else:
            close_position(st, cfg, now, pos, price, "stop", leg_id, trigger=float(pos["stop"]), qty=qty)
        return True
    if status == "CANCELLED" and not pend:
        return broker_square_off(k, st, cfg, now, pos, leg)
    return False


def broker_square_off(k, st, cfg, now, pos, leg):
    sym = pos["symbol"]
    try:
        rows = (k.positions() or {}).get("day") or []
    except kite.KiteError:
        return False
    for row in rows:
        if row.get("tradingsymbol") == sym and int(_f(row.get("quantity")) or 0) == 0 \
                and int(_f(row.get("sell_quantity")) or 0) >= int(pos["qty"]):
            close_position(st, cfg, now, pos, _f(row.get("sell_price")) or float(pos["stop"]), "broker square-off",
                           str(leg.get("order_id") or ""), trigger=_f(pos.get("last")) or float(pos["stop"]))
            return True
    return False


def close_position(st, cfg, now, pos, price, reason, exit_order_id, trigger, qty=None):
    """The exit fill into the ledger, the trade record with P&L, charges and slippage."""
    qty = int(qty or pos["qty"])
    sym = pos["symbol"]
    today = now.date().isoformat()
    xo = exit_order(pos)
    h = order_hash(xo)
    note = {"stop": "the exchange's stop leg filled", "broker square-off": "Zerodha squared off the MIS position",
            "target": "target reached", "flat-by": "flat by %s" % cfg["flat_by"], "kill switch": "kill switch"}[reason]
    log_line(now, today, "approved", h, xo, cfg, reply="auto", note=note, plain="SELL %d %s (%s)" % (qty, sym, reason))
    if reason not in ("stop", "broker square-off"):
        log_line(now, today, "placed", h, xo, cfg, kite_order_id=exit_order_id)
    record_fill(cfg, now, today, xo, h, exit_order_id, qty, price, note=note)
    entry_price = float(pos["entry_price"])
    gross = r2((price - entry_price) * qty)
    charges = estimate_charges(r2(entry_price * qty), r2(price * qty))
    charges_inr = r2((charges or {}).get("total") or 0.0) if isinstance(charges, dict) else 0.0
    net = r2(gross - charges_inr)
    planned = float(pos.get("planned_entry") or entry_price)
    trade = {"symbol": sym, "qty": qty, "setup": pos.get("setup"), "paper": pos.get("paper"),
             "entry": {"time": pos.get("filled_at") or pos["entered_at"], "price": entry_price,
                       "planned": pos.get("planned_entry"), "trigger_time": pos.get("trigger_time"),
                       "bar": pos.get("entry_bar"), "order_id": pos["parent_order_id"],
                       "stop_order_id": pos.get("stop_order_id")},
             "stop": pos["stop"], "target": pos["target"], "max_loss_inr": pos.get("max_loss_inr"),
             "exit": {"time": iso(now), "price": r2(price), "reason": reason, "order_id": exit_order_id,
                      "trigger": r2(trigger) if trigger is not None else None},
             "gross_inr": gross, "charges": charges, "charges_inr": charges_inr, "net_inr": net,
             "slippage": {"entry_per_share": r2(entry_price - planned), "entry_inr": r2((entry_price - planned) * qty),
                          "exit_per_share": r2(price - float(trigger)) if trigger is not None else None,
                          "exit_inr": r2((price - float(trigger)) * qty) if trigger is not None else None},
             "odds": pos.get("odds")}
    st["trades"].append(trade)
    st["trades_done"] = len(st["trades"])
    st["realised_inr"] = r2(st["realised_inr"] + gross)
    st["charges_inr"] = r2(st["charges_inr"] + charges_inr)
    st["net_inr"] = net_so_far(st)
    st["position"] = None
    st["phase"] = "hunting"
    decide(st, now, "exit", "%s: SELL %d %s at %.2f (%s %.2f, slippage %+.2f a share); gross %s, charges %s, net %s; "
           "day net %s" % (reason, qty, sym, price, "trigger" if trigger is not None else "ref",
                           float(trigger) if trigger is not None else price,
                           (price - float(trigger)) if trigger is not None else 0.0, money(gross),
                           money(charges_inr), money(net), money(st["net_inr"])),
           symbol=sym, price=r2(price), gross_inr=gross, charges_inr=charges_inr, net_inr=net)


# ---------------------------------------------------------------- the tick

def entries_allowed(st, cfg, now):
    """Why no new entry now, or None."""
    lim = st["limits"]
    if st["kill"]:
        return "kill switch %s is present" % lim["kill_file"]
    if st["trades_done"] >= lim["max_trades_per_day"]:
        return "%d of %d trades done" % (st["trades_done"], lim["max_trades_per_day"])
    if net_so_far(st) <= -lim["max_daily_loss_inr"]:
        return "day net %s has reached the %s daily cap (realised %s, charges %s)" % (
            money(net_so_far(st)), money(lim["max_daily_loss_inr"]), money(st["realised_inr"]), money(st["charges_inr"]))
    if now.time() >= cfg["_no_entries_after"]:
        return "past no_entries_after %s" % cfg["no_entries_after"]
    return None


def say_once(st, now, key, event, why):
    if st.get("_said", {}).get(key) == why:
        return
    st.setdefault("_said", {})[key] = why
    decide(st, now, event, why)


def write_summary(st, now):
    if st.get("summary"):
        return
    st["summary"] = {"date": st["date"], "trades": st["trades_done"], "realised_inr": st["realised_inr"],
                     "charges_inr": st["charges_inr"], "net_inr": net_so_far(st),
                     "skipped": len(st["skipped"]), "decisions": len(st["decisions"]), "kill": st["kill"],
                     "paper": st["paper"], "written_at": iso(now)}
    st["phase"] = "done"
    decide(st, now, "summary", "day done: %d trade(s), net %s after %s charges" % (
        st["trades_done"], money(net_so_far(st)), money(st["charges_inr"])))
    journal(st)


def journal(st):
    if st.get("journal_written"):
        return
    line = dict(st["summary"], symbols=[t["symbol"] for t in st["trades"]],
                exits=[t["exit"]["reason"] for t in st["trades"]])
    append_line(JOURNAL, line)
    st["journal_written"] = True


def tick(a):
    cfg = load_config(a.config)
    now = parse_now(a.now)
    day = now.date()
    st = load_state(day, cfg, now)
    kill_file = cfg["auto"]["kill_file"]
    kill = os.path.exists(kill_file)
    if kill and not st["kill"]:
        decide(st, now, "kill", "kill switch %s found: no new entries; any open position is exited" % kill_file)
    st["kill"] = kill
    ok, why = nse.trading_day(day)
    if not ok:
        st["phase"] = "closed"
        say_once(st, now, "closed", "closed", "NSE is closed today (%s)" % why)
        save_state(st, now)
        return 1
    if why:
        say_once(st, now, "calendar", "note", why)
    t = now.time()
    if t < HARD_OPEN:
        k = session(st, now, cfg)
        save_state(st, now)
        return 0 if k is not None else 1
    if st.get("summary"):
        save_state(st, now)
        return 0
    k = session(st, now, cfg)
    if k is None:
        save_state(st, now)
        return 1
    if st["position"]:
        manage(k, st, cfg, now, a.dry_run)
    if st["position"] is None and t < HARD_CLOSE:
        blocked = entries_allowed(st, cfg, now)
        if blocked:
            if st["phase"] != "done":
                st["phase"] = "done"
                decide(st, now, "done", "no more entries: %s" % blocked)
        elif t < FIRST_HUNT:
            st["phase"] = "opening-range"
            say_once(st, now, "range", "wait", "opening range forms 09:15 to 09:30; hunting starts %s" % hhmm(FIRST_HUNT))
        else:
            st["phase"] = "hunting"
            hunt(k, st, cfg, now, a)
    if t >= HARD_CLOSE and st["position"] is None:
        write_summary(st, now)
    elif t >= HARD_CLOSE:
        say_once(st, now, "after", "note", "continuous trading ended for CAS stocks at %s with %s still open; "
                 "Zerodha squares off MIS at %s; the ledger is updated when the fill shows"
                 % (hhmm(HARD_CLOSE), st["position"]["symbol"], hhmm(nse.MIS_SQUARE_OFF_CAS)))
    save_state(st, now)
    return 0


# ---------------------------------------------------------------- status and report

def _pick_state(a):
    now = parse_now(getattr(a, "now", None))
    day = datetime.date.fromisoformat(a.date) if getattr(a, "date", None) else now.date()
    st = read_json(state_path(day))
    if st is None:
        raise AutoError("no Auto Trader state for %s (%s): no tick has run that day" % (day.isoformat(), state_path(day)))
    return st, now


def cmd_status(a):
    st, now = _pick_state(a)
    lines = ["Auto Trader %s, %s, phase %s%s." % (st["date"], "PAPER" if st["paper"] else "LIVE", st["phase"],
                                                    ", KILL SWITCH ON" if st["kill"] else "")]
    pos = st["position"]
    if pos:
        lines.append("Open: %d %s at %s, stop %.2f, target %.2f, last %s, parent %s, stop leg %s%s." % (
            pos["qty"], pos["symbol"], ("%.2f" % pos["entry_price"]) if pos.get("entry_price") else "pending",
            float(pos["stop"]), float(pos["target"]), ("%.2f" % pos["last"]) if pos.get("last") else "?",
            pos["parent_order_id"], pos.get("stop_order_id") or "?", " (fill pending)" if pos.get("pending") else ""))
    else:
        lines.append("No open position.")
    lines.append("Trades %d of %d, realised %s, charges %s, net %s (daily cap %s)." % (
        st["trades_done"], st["limits"]["max_trades_per_day"], money(st["realised_inr"]), money(st["charges_inr"]),
        money(net_so_far(st)), money(st["limits"]["max_daily_loss_inr"])))
    if st.get("live_balance_inr") is not None:
        lines.append("Balance seen: %s." % money(st["live_balance_inr"]))
    if st["candidates"]:
        lines.append("Candidates: " + ", ".join("%s %s" % (c["symbol"], c.get("at") or "") for c in st["candidates"][:10]))
    if st["skipped"]:
        lines.append("Skipped: " + "; ".join("%s (%s)" % (s, v["reason"][:60]) for s, v in sorted(st["skipped"].items())))
    for d in st["decisions"][-8:]:
        lines.append("  %s  %s%s: %s" % (d["at"][11:19], d["event"], (" " + d["symbol"]) if d.get("symbol") else "",
                                        d["why"]))
    if a.json:
        print(json.dumps(st, indent=2))
    else:
        print("\n".join(lines))
    return 0


def report_doc(st):
    return {"created_by": "trader-auto", "date": st["date"], "paper": st["paper"], "limits": st["limits"],
            "trades": st["trades"], "totals": {"trades": st["trades_done"], "gross_inr": st["realised_inr"],
                                                "charges_inr": st["charges_inr"], "net_inr": net_so_far(st)},
            "skipped": st["skipped"], "candidates": st["candidates"], "decisions": st["decisions"],
            "kill": st["kill"], "summary": st.get("summary")}


def cmd_report(a):
    st, now = _pick_state(a)
    doc = report_doc(st)
    if st.get("summary"):
        journal(st)
        save_state(st, now)
    if a.json:
        print(json.dumps(doc, indent=2))
        return 0
    out = ["Auto Trader report, %s (%s). Not financial advice; this is what the loop did and why." % (
        st["date"], "paper" if st["paper"] else "LIVE")]
    if not st["trades"]:
        out.append("No trades.")
    for i, tr in enumerate(st["trades"], 1):
        e, x, s, o = tr["entry"], tr["exit"], tr["slippage"], tr.get("odds") or {}
        out.append("%d. %s x%d  entry %s at %.2f (plan %.2f, %+.2f a share)  stop %.2f  target %.2f" % (
            i, tr["symbol"], tr["qty"], e["time"][11:16], e["price"], e.get("planned") or e["price"],
            s.get("entry_per_share") or 0.0, float(tr["stop"]), float(tr["target"])))
        out.append("   exit %s at %.2f, %s (trigger %s, %s a share)" % (
            x["time"][11:16], x["price"], x["reason"], ("%.2f" % x["trigger"]) if x.get("trigger") is not None else "?",
            ("%+.2f" % s["exit_per_share"]) if s.get("exit_per_share") is not None else "?"))
        out.append("   gross %s, charges %s, net %s   odds at entry: target %s %%, stop %s %%, neither %s %% (%s)" % (
            money(tr["gross_inr"]), money(tr["charges_inr"]), money(tr["net_inr"]), _pc(o.get("p_target")),
            _pc(o.get("p_stop")), _pc(o.get("p_neither")), o.get("verdict") or "no verdict"))
    out.append("Totals: %d trade(s), gross %s, charges %s, net %s." % (
        st["trades_done"], money(st["realised_inr"]), money(st["charges_inr"]), money(net_so_far(st))))
    if st["skipped"]:
        out.append("Skipped:")
        for sym, v in sorted(st["skipped"].items(), key=lambda kv: kv[1]["first_at"]):
            out.append("  %s  %-12s %s" % (v["first_at"][11:16], sym, v["reason"]))
    refusals = [d for d in st["decisions"] if d["event"] in ("refuse", "kill", "login-needed", "failed")]
    if refusals:
        out.append("Refusals and failures:")
        for d in refusals:
            out.append("  %s  %s%s: %s" % (d["at"][11:16], d["event"], (" " + d["symbol"]) if d.get("symbol") else "", d["why"]))
    if st.get("summary"):
        out.append("Journal: one line for %s in %s." % (st["date"], JOURNAL))
    else:
        out.append("The day is not summarised yet (that happens on the first tick after %s)." % hhmm(HARD_CLOSE))
    print("\n".join(out))
    return 0


def _pc(x):
    return "?" if x is None else "%.1f" % (100 * float(x))


# ---------------------------------------------------------------- the probe

def snap_down(price, tick):
    steps = int((price + 1e-9) / tick)
    return round(steps * tick, 2)


def cmd_probe(a):
    now = parse_now(a.now)
    cfg = load_config(a.config, need_auto=False)
    kill_file = cfg["auto"]["kill_file"]
    sym = kite.normalize_symbol(a.symbol)
    if os.path.exists(kill_file):
        raise AutoError("kill switch %s is present: remove it (auto.py go) before a probe" % kill_file)
    if nse.phase(now) != "open" or not within_hours(now):
        raise AutoError("the probe runs only while NSE is open, %s to %s IST (now %s)" % (
            hhmm(HARD_OPEN), hhmm(HARD_CLOSE), now.strftime("%Y-%m-%d %H:%M")))
    if now.time() >= cfg["_flat_by"]:
        raise AutoError("past flat_by %s: run the probe earlier in the session" % cfg["flat_by"])
    if a.qty < 1 or a.qty > 5:
        raise AutoError("--qty is 1 to 5 shares; the probe is a test, not a trade")
    if not 0.2 <= a.stop_pct <= 9.0:
        raise AutoError("--stop-pct is 0.2 to 9.0 percent (Zerodha's cover order holds a stop within 10 %)")
    try:
        k = connect_kite(now)
    except kite.LoginNeeded as e:
        raise AutoError(str(e))
    row = k.instrument(sym)
    tick = _f(row.get("tick_size")) or 0.05
    q = (k.quotes([sym]) or {}).get(sym) or {}
    last = _f(q.get("last_price"))
    if not last:
        raise AutoError("Kite has no last price for %s" % sym)
    trigger = snap_down(last * (1 - a.stop_pct / 100.0), tick)
    body = {"variety": "co", "exchange": "NSE", "tradingsymbol": sym, "transaction_type": "BUY",
            "order_type": "MARKET", "quantity": int(a.qty), "product": "MIS", "trigger_price": trigger,
            "validity": "DAY", "tag": "dtsprobe"}
    print("PROBE %s: %d share(s), last %.2f, tick %.2f, stop trigger %.2f (%.1f %% under), %s." % (
        sym, a.qty, last, tick, trigger, a.stop_pct, "PAPER config" if cfg.get("paper") else "LIVE account, REAL MONEY"))
    print("Body for k.place_order:")
    print(json.dumps(body, indent=2, sort_keys=True))
    print("Then exit_cover_order(parent) after %d s, which cancels the stop leg so Kite sells at market." % PROBE_HOLD)
    if a.dry_run:
        print("Dry run: nothing sent.")
        return 0
    if not a.real:
        print("Not sent. To send it for real, add the literal argument %s." % REAL_FLAG)
        return 1
    bal = live_balance(k)
    need = r2(last * a.qty)
    print("Live balance %s; the order needs about %s." % (money(bal), money(need)))
    if bal < need:
        raise AutoError("live balance %s is under the %s this probe needs" % (money(bal), money(need)))
    parent = str(k.place_order(body))
    print("Placed. Parent order id %s. Waiting up to %d s for the fill..." % (parent, ORDER_WAIT))
    rec = wait(k, parent)
    print("Parent: " + json.dumps(_slim(rec), sort_keys=True))
    if rec.get("status") != "COMPLETE":
        print("The entry did not complete (%s). Check the Kite app's order book before doing anything else." % rec.get("status"))
        return 1
    legs = k.cover_order_legs(parent) or {}
    print("cover_order_legs: parent %s / second leg %s" % (json.dumps(_slim(legs.get("parent") or {}), sort_keys=True),
                                                           json.dumps(_slim(legs.get("second_leg") or {}), sort_keys=True)))
    entry_price = _f(rec.get("average_price")) or last
    qty = int(_f(rec.get("filled_quantity")) or a.qty)
    print("Holding %d %s at %.2f for %d s..." % (qty, sym, entry_price, PROBE_HOLD))
    _sleep(PROBE_HOLD)
    exit_id = str(k.exit_cover_order(parent))
    print("exit_cover_order(%s) -> %s. Waiting for the exit fill..." % (parent, exit_id))
    xrec = wait(k, exit_id)
    print("Exit: " + json.dumps(_slim(xrec), sort_keys=True))
    if xrec.get("status") != "COMPLETE":
        print("The exit did not complete (%s). Exit from the Kite app (Positions > Exit) now." % xrec.get("status"))
        return 1
    exit_price = _f(xrec.get("average_price")) or last
    gross = r2((exit_price - entry_price) * qty)
    charges = estimate_charges(r2(entry_price * qty), r2(exit_price * qty))
    total = r2((charges or {}).get("total") or 0.0) if isinstance(charges, dict) else 0.0
    print("Fills: BUY %d at %.2f, SELL %d at %.2f. Realised %s, charges about %s (%s), net %s." % (
        qty, entry_price, qty, exit_price, money(gross), money(total), json.dumps(charges, sort_keys=True),
        money(r2(gross - total))))
    net_qty = None
    for row in (k.positions() or {}).get("net") or []:
        if row.get("tradingsymbol") == sym:
            net_qty = int(_f(row.get("quantity")) or 0)
    print("positions(): %s net quantity %s." % (sym, "0, flat" if not net_qty else "%s, NOT FLAT: exit it in the Kite app" % net_qty))
    append_line(os.path.join(AUTO_DIR, "probes.jsonl"),
                {"at": iso(now), "symbol": sym, "qty": qty, "entry": entry_price, "exit": exit_price,
                 "parent_order_id": parent, "exit_order_id": exit_id, "gross_inr": gross, "charges": charges,
                 "net_qty_after": net_qty})
    return 0 if not net_qty else 1


def _slim(rec):
    keep = ("order_id", "parent_order_id", "status", "status_message", "average_price", "filled_quantity",
            "trigger_price", "transaction_type", "order_type", "exchange_update_timestamp")
    return {k: rec.get(k) for k in keep if rec.get(k) not in (None, "")}


# ---------------------------------------------------------------- stop / go

def cmd_stop(a):
    cfg = load_config(a.config, need_auto=False)
    path = cfg["auto"]["kill_file"]
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("stopped at %s\n" % iso(parse_now(a.now)))
    print("Kill switch set: %s. The next tick exits any open position and takes no new entry. auto.py go removes it."
          % path)
    return 0


def cmd_go(a):
    cfg = load_config(a.config, need_auto=False)
    path = cfg["auto"]["kill_file"]
    if os.path.exists(path):
        os.remove(path)
        print("Kill switch removed: %s. The loop may enter again on its next tick." % path)
    else:
        print("No kill switch at %s." % path)
    return 0


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description="Auto Trader (trader-auto): the unattended intraday loop.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--now", help='pretend it is this IST time, e.g. "2026-10-29 10:15"')
        p.add_argument("--config", default=DEFAULT_CONFIG)

    p = sub.add_parser("tick", help="one pass: the clock, the hunt or the hold, the state file")
    common(p)
    p.add_argument("--dry-run", action="store_true", help="run the whole chain but never call an order method")
    p = sub.add_parser("status", help="today's state in plain words")
    common(p)
    p.add_argument("--date")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("report", help="the day's trades, P&L, charges, slippage, odds and skips")
    common(p)
    p.add_argument("--date")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("probe", help="the one-share live test of the real order path")
    common(p)
    p.add_argument("symbol")
    p.add_argument("--qty", type=int, default=1)
    p.add_argument("--stop-pct", type=float, default=1.0)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(REAL_FLAG, dest="real", action="store_true")
    p = sub.add_parser("stop", help="create the kill-switch file")
    common(p)
    p = sub.add_parser("go", help="remove the kill-switch file")
    common(p)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"tick": tick, "status": cmd_status, "report": cmd_report, "probe": cmd_probe,
                "stop": cmd_stop, "go": cmd_go}[a.cmd](a)
    except (AutoError, kite.KiteError) as e:
        print("auto.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("auto.py: stopped", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
