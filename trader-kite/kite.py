#!/usr/bin/env python3
"""Zerodha Kite Connect v3 client for the pack (standard library only).

Every data tool in the pack reads Zerodha through this file: the session,
the instrument list, historical candles and quotes. It never places,
modifies or cancels an order; orders go through Zerodha's Kite MCP server
after you say yes (see trader-broker).

Credentials (never in chat, never committed):
  KITE_API_KEY, KITE_API_SECRET, KITE_REDIRECT_URL from the environment, or
  from a .env file in the project folder (or the file KITE_ENV_FILE names).

Session: login.py writes trader/kite_session.json (mode 600). Kite access
tokens expire at 06:00 IST the next day, so a token issued before the most
recent 06:00 IST is treated as stale and you are asked to log in again.

Endpoints (https://kite.trade/docs/connect/v3/):
  POST /session/token                                    request_token -> access_token
  GET  /instruments/<exchange>                           CSV instrument dump
  GET  /instruments/historical/<token>/<interval>        candles, IST timestamps
  GET  /quote?i=NSE:INFY&i=...                           full quotes, 500 per call
Rate limits: quote 1/s, historical 3/s, everything else 10/s.
"""

import csv
import datetime
import hashlib
import io
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.kite.trade"
LOGIN = "https://kite.zerodha.com/connect/login?v=3&api_key=%s"
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30), "IST")
TOKEN_RESET = datetime.time(6, 0)
SESSION_FILE = os.path.join("trader", "kite_session.json")
CACHE_DIR = os.path.join("trader", "cache")
QUOTE_BATCH = 500
TIMEOUT = 30

# seconds between calls, per Kite's published limits (with a little slack)
MIN_GAP = {"quote": 1.05, "historical": 0.35, "other": 0.11}
# longest span Kite returns in one historical call, by interval
MAX_DAYS = {"minute": 60, "3minute": 100, "5minute": 100, "10minute": 100, "15minute": 200,
            "30minute": 200, "60minute": 400, "day": 2000}


class KiteError(Exception):
    """A Kite call failed; the message is safe to show the user."""


class LoginNeeded(KiteError):
    """No session, a stale one, or Kite rejected the token."""


# ---------------------------------------------------------------- credentials

def parse_env(text):
    """KEY=VALUE lines -> dict. Comments, blanks and `export ` prefixes are fine."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        out[key] = val.strip().strip("'\"")
    return out


def credentials(env=None, env_file=None):
    """api_key, api_secret, redirect_url: environment first, then the .env file."""
    env = os.environ if env is None else env
    path = env_file or env.get("KITE_ENV_FILE") or ".env"
    found = {}
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            found = parse_env(fh.read())
    pick = lambda k: env.get(k) or found.get(k) or ""
    creds = {"api_key": pick("KITE_API_KEY"), "api_secret": pick("KITE_API_SECRET"),
             "redirect_url": pick("KITE_REDIRECT_URL")}
    if not creds["api_key"]:
        raise KiteError("KITE_API_KEY is not set: put it in the environment or in .env "
                        "in your project folder")
    return creds


# ---------------------------------------------------------------- clock + session

def now_ist():
    return datetime.datetime.now(IST)


def token_is_fresh(created_at, now):
    """Both aware datetimes. Fresh if issued after the latest 06:00 IST."""
    now = now.astimezone(IST)
    boundary = datetime.datetime.combine(now.date(), TOKEN_RESET, tzinfo=IST)
    if now < boundary:
        boundary -= datetime.timedelta(days=1)
    return created_at.astimezone(IST) >= boundary


def checksum(api_key, request_token, api_secret):
    return hashlib.sha256((api_key + request_token + api_secret).encode()).hexdigest()


def save_session(data, now, path=SESSION_FILE):
    record = {"user_id": data.get("user_id"), "user_name": data.get("user_name"),
              "access_token": data["access_token"], "created_at": now.isoformat()}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2)
    return record


def load_session(now, path=SESSION_FILE):
    """The stored session if it is still today's, else LoginNeeded."""
    if not os.path.isfile(path):
        raise LoginNeeded("not logged in to Kite: run  python3 trader-kite/login.py")
    with open(path, encoding="utf-8") as fh:
        rec = json.load(fh)
    created = datetime.datetime.fromisoformat(rec["created_at"])
    if not token_is_fresh(created, now):
        raise LoginNeeded("the Kite session from %s expired at 06:00 IST: run  "
                          "python3 trader-kite/login.py" % created.strftime("%Y-%m-%d %H:%M"))
    return rec


# ---------------------------------------------------------------- http

_urlopen = urllib.request.urlopen   # tests swap this
_sleep = time.sleep
_clock = time.monotonic
_last_call = {}


def _throttle(kind):
    gap = MIN_GAP[kind]
    wait = _last_call.get(kind, -gap) + gap - _clock()
    if wait > 0:
        _sleep(wait)
    _last_call[kind] = _clock()


def _error_message(body):
    try:
        doc = json.loads(body)
        return doc.get("error_type", ""), doc.get("message", body[:200])
    except ValueError:
        return "", body[:200]


RETRIES_ON_429 = 3
BACKOFF_SECONDS = 1.0


def http(method, path, api_key, access_token=None, params=None, data=None, kind="other"):
    """One Kite call -> the decoded body (JSON `data` field, or raw text for CSV).

    A 429 (too many requests, e.g. two tools sharing one key) is retried a few
    times with a short pause; every other error is raised at once.
    """
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    headers = {"X-Kite-Version": "3", "User-Agent": "day-trader-skill-india/1.0"}
    if access_token:
        headers["Authorization"] = "token %s:%s" % (api_key, access_token)
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    for attempt in range(RETRIES_ON_429 + 1):
        _throttle(kind)
        try:
            with _urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read().decode("utf-8")
                ctype = r.headers.get("Content-Type", "")
            break
        except urllib.error.HTTPError as e:
            etype, msg = _error_message(e.read().decode("utf-8", "replace"))
            if e.code == 429 and attempt < RETRIES_ON_429:
                _sleep(BACKOFF_SECONDS * (attempt + 1))
                continue
            if e.code == 403 or etype == "TokenException":
                raise LoginNeeded("Kite rejected the session (%s): run  python3 trader-kite/login.py"
                                  % msg)
            raise KiteError("Kite %s %s: HTTP %d %s %s" % (method, path, e.code, etype, msg))
        except urllib.error.URLError as e:
            raise KiteError("cannot reach Kite (%s)" % e.reason)
    if "json" not in ctype:
        return raw
    doc = json.loads(raw)
    if doc.get("status") != "success":
        raise KiteError("Kite %s: %s" % (path, doc.get("message", "unknown error")))
    return doc.get("data")


# ---------------------------------------------------------------- client

class Kite:
    """A logged-in session. Build it with Kite.connect()."""

    def __init__(self, api_key, access_token, user_id=None):
        self.api_key = api_key
        self.access_token = access_token
        self.user_id = user_id
        self._instruments = {}

    @classmethod
    def connect(cls, now=None, session_path=SESSION_FILE, env=None):
        creds = credentials(env)
        rec = load_session(now or now_ist(), session_path)
        return cls(creds["api_key"], rec["access_token"], rec.get("user_id"))

    def get(self, path, params=None, kind="other"):
        return http("GET", path, self.api_key, self.access_token, params, kind=kind)

    def profile(self):
        return self.get("/user/profile")

    def margins(self):
        return self.get("/user/margins/equity")

    # -- instruments

    def instruments(self, exchange="NSE", today=None, cache_dir=CACHE_DIR):
        """tradingsymbol -> row dict for one exchange, cached once per day."""
        if exchange in self._instruments:
            return self._instruments[exchange]
        day = (today or now_ist().date()).isoformat()
        path = os.path.join(cache_dir, "instruments-%s-%s.csv" % (exchange, day))
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        else:
            text = self.get("/instruments/%s" % exchange)
            os.makedirs(cache_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        table = parse_instruments(text)
        self._instruments[exchange] = table
        return table

    def cas_symbols(self, today=None, cache_dir=CACHE_DIR):
        """NSE stocks with F&O contracts: the ones in the Closing Auction Session."""
        day = (today or now_ist().date()).isoformat()
        path = os.path.join(cache_dir, "cas-%s.json" % day)
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                return set(json.load(fh))
        names = parse_fno_underlyings(self.get("/instruments/NFO"))
        os.makedirs(cache_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(sorted(names), fh)
        return names

    def instrument(self, symbol, exchange="NSE"):
        sym = normalize_symbol(symbol)
        row = self.instruments(exchange).get(sym)
        if row is None:
            raise KiteError("%s is not listed on %s (check the spelling, e.g. RELIANCE, INFY)"
                            % (sym, exchange))
        return row

    # -- market data

    def historical(self, symbol, interval, start, end, exchange="NSE"):
        """Candles [(aware IST datetime, o, h, l, c, v)] from start to end (aware datetimes)."""
        if interval not in MAX_DAYS:
            raise KiteError("unknown interval %r (one of %s)" % (interval, ", ".join(MAX_DAYS)))
        token = self.instrument(symbol, exchange)["instrument_token"]
        out = []
        for lo, hi in chunks(start, end, MAX_DAYS[interval]):
            data = self.get("/instruments/historical/%s/%s" % (token, interval),
                            {"from": fmt(lo), "to": fmt(hi)}, kind="historical")
            out.extend(parse_candles(data))
        return dedupe(out)

    def quotes(self, symbols, exchange="NSE"):
        """{SYMBOL: quote dict} for many symbols, 500 per call."""
        keys = ["%s:%s" % (exchange, normalize_symbol(s)) for s in symbols]
        out = {}
        for i in range(0, len(keys), QUOTE_BATCH):
            data = self.get("/quote", {"i": keys[i:i + QUOTE_BATCH]}, kind="quote") or {}
            out.update({k.split(":", 1)[1]: v for k, v in data.items()})
        return out


# ---------------------------------------------------------------- pure helpers

def normalize_symbol(symbol):
    """'reliance', 'RELIANCE.NS', 'NSE:RELIANCE' -> 'RELIANCE'."""
    s = symbol.strip().upper()
    if ":" in s:
        s = s.split(":", 1)[1]
    for suffix in (".NS", ".BO"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return s


def parse_instruments(text):
    table = {}
    for row in csv.DictReader(io.StringIO(text)):
        if row.get("segment") not in ("NSE", "BSE") or row.get("instrument_type") != "EQ":
            continue
        table[row["tradingsymbol"]] = {
            "instrument_token": int(row["instrument_token"]),
            "tradingsymbol": row["tradingsymbol"], "name": row.get("name", ""),
            "exchange": row.get("exchange", ""), "tick_size": float(row.get("tick_size") or 0.05),
            "lot_size": int(row.get("lot_size") or 1)}
    return table


def parse_fno_underlyings(text):
    """Underlying names of NSE stock futures (index futures like NIFTY are dropped)."""
    indices = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
    return {row["name"].strip('"') for row in csv.DictReader(io.StringIO(text))
            if row.get("segment") == "NFO-FUT" and row.get("name", "").strip('"') not in indices}


def fmt(dt):
    return dt.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")


def chunks(start, end, max_days):
    """Split [start, end] into spans Kite accepts in one call."""
    step = datetime.timedelta(days=max_days)
    lo = start
    while lo <= end:
        hi = min(lo + step - datetime.timedelta(seconds=1), end)
        yield lo, hi
        lo = hi + datetime.timedelta(seconds=1)


def parse_time(text):
    """'2026-10-29T09:15:00+0530' -> aware datetime."""
    if len(text) >= 5 and text[-5] in "+-" and text[-3] != ":":
        text = text[:-2] + ":" + text[-2:]
    return datetime.datetime.fromisoformat(text)


def parse_candles(data):
    rows = (data or {}).get("candles") or []
    return [(parse_time(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), int(c[5]))
            for c in rows]


def dedupe(candles):
    seen, out = set(), []
    for c in sorted(candles, key=lambda c: c[0]):
        if c[0] not in seen:
            seen.add(c[0])
            out.append(c)
    return out
