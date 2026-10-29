# The shared contract: trader/ files, the clock, the data, the rules

Every skill in this pack is one standalone Python script that reads what the
last skill wrote to the `trader/` folder in the user's project and writes one
file for the next. This document is the contract between them. Change it only
with a matching change to every reader and writer.

## Ground rules for every tool

- Standard-library Python, 3.8 syntax (no `list[int]`, no `removeprefix`, no
  `zoneinfo`, no `match`). Nothing to pip install.
- Indian market only: NSE equities, Zerodha Kite Connect for data and orders,
  rupees, IST. There is no US anything left: no Yahoo, no SEC, no NYSE, no
  dollars, no New York time, no US day-trading minimums.
- Shared code lives in `trader-kite/`: `kite.py` (the Kite client), `nse.py`
  (the market clock). Import it like this at the top of each script:

  ```python
  import os, sys
  sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trader-kite"))
  import kite   # noqa: E402
  import nse    # noqa: E402
  ```

  Never copy kite.py or nse.py into a skill folder. Never build a second
  holiday list or a second HTTP client.
- Data always comes from `kite.Kite.connect()` (today's session). When there
  is no session the tool prints `kite.LoginNeeded`'s message and exits 1; it
  does not fall back to any other source.
- Orders: no skill script places, modifies or cancels an order. Only
  `/trader-broker` writes a ticket, and Claude calls the Kite MCP server only
  after the user types yes. The two exceptions, trader-auto and the one-share
  probe, are spelled out in "Orders through kite.py" at the end of this file.
- Times: IST everywhere. `HH:MM` 24-hour strings in config and plans
  (`"15:05"`), ISO 8601 with `+05:30` for timestamps
  (`"2026-10-29T10:20:00+05:30"`). Dates `YYYY-MM-DD`. Use `nse.IST`,
  `nse.now_ist()`, `nse.parse_hhmm()`; never `datetime.now()` bare.
- Money: rupees as floats, two decimals when printed (`Rs 1,171.50`; the
  rupee sign is fine in prose, `Rs` in code output). Config keys end `_inr`.
- Prices snap to the instrument's tick size from Kite's instrument row
  (`tick_size`: RELIANCE is 0.10, many stocks 0.05, stocks under Rs 250 are 0.01; read the row, never assume). Stops round
  down, targets round down, never up.
- Symbols: Kite trading symbols, upper case, no suffix (`RELIANCE`, not
  `RELIANCE.NS`). Accept the suffixed and `NSE:` forms on input through
  `kite.normalize_symbol()`.
- Each tool keeps the original's shape: `argparse` subcommands, `--write` to
  write its file, `--json` to print JSON, `--now "2026-10-29 10:15"` (IST) to
  pin the clock, `--offline PATH` (or the same `_urlopen` swap the tests use)
  to read saved data, exit 0 on a result and 1 on nothing / cannot run with
  one plain line why.
- Tests: `unittest`, offline, on fixtures cut from real Kite data saved under
  `tests/fixtures/<skill>/`. Swap `yourmodule.kite._urlopen` (the `kite`
  module your script imported) with a fake that answers from fixtures; see
  `tests/test_kite.py` for `FakeResponse` / `FakeNet`. Tests run with
  `cd tests && python3 -m unittest test_<name>`.
- Prose (SKILL.md, docstrings, output): plain sentences, no em dashes (a
  test forbids the character), no hype. Every skill on the way to an order
  says "not financial advice" and reminds that nothing is placed until the
  user says yes.

## Other data sources: public feeds only

Kite has no news, no filings and no fundamentals. For those, a tool may read
only sources that answer a plainly identified client: the User-Agent is
`day-trader-skill-india/1.0 (+https://github.com/krishanag/day-trader-skill)`,
no cookies, no browser impersonation, no retry past a 403. Checked on
2026-10-29 with that client:

| source | status | use |
| --- | --- | --- |
| Google News RSS `https://news.google.com/rss/search?q=<query>&hl=en-IN&gl=IN&ceid=IN:en` | 200 | headlines per company |
| Economic Times markets RSS `https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms` | 200 | market headlines |
| Mint markets RSS `https://www.livemint.com/rss/markets` | 200 | market headlines |
| Moneycontrol `https://www.moneycontrol.com/rss/marketreports.xml` | 200 | market reports |
| BSE notices `https://www.bseindia.com/data/xml/notices.xml` | 200 | exchange notices |
| BSE corporate announcements `https://www.bseindia.com/data/xml/announcements.xml` | 200 | the current day's filings by every listed company (about 9,400 items, 3 MB): title "Company Ltd (scripcode)", description, category, a PDF link, pubDate IST. Results, board meetings, insider trading (SAST / PIT) disclosures, bulk deal notes. Match a company by Kite's instrument `name`, or by BSE scrip code when known |
| SEBI RSS `https://www.sebi.gov.in/sebirss.xml` | 200 | regulator press releases, orders |
| nseindia.com and nsearchives.nseindia.com (any path) | 403 / blocked | **out**. NSE's terms forbid scripted access. The tool asks the user to download the CSV or XBRL from NSE's public page in their browser and reads it with `--file PATH` |
| business-standard.com RSS | 403 | out |
| api.bseindia.com (the site's own JSON API) | not a public feed | out |

A tool never spoofs a browser or performs a cookie handshake to get past a
block. If a source is down, the tool says so in one line and goes on with
the rest.

## The order hash (trader-risk and trader-broker)

Both tools identify an order by the same hash so a PASS applies to that
exact order and nothing else:

```python
def order_hash(order):
    body = json.dumps({"kite": order["kite"], "stop_leg": order.get("stop_leg")},
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()[:12]
```

The hash covers the entry body and the stop leg (null for a cover order,
whose stop is already inside `kite.trigger_price`). Any change to either
(quantity, trigger, symbol, the stop) is a new order that needs a new PASS.

## The clock (nse.py)

| moment | IST | note |
| --- | --- | --- |
| pre-open | 09:00 to 09:15 | no trading |
| open | 09:15 | the opening range starts here |
| continuous end, CAS stocks | 15:15 | stocks with F&O contracts (`kite.Kite.cas_symbols()`), then the Closing Auction Session 15:15 to 15:35 |
| continuous end, other stocks | 15:30 | |
| Zerodha MIS auto square-off, CAS stocks | 15:12 | Rs 50 + GST each time |
| **pack default flat-by** | **15:05** | `nse.DEFAULT_FLAT_BY`, every stock |

`nse.trading_day(date)`, `nse.phase(now, cas)`, `nse.next_open(now)`,
`nse.previous_trading_days(day, n)`. Holidays are NSE's 2026 equity list;
dates after `nse.KNOWN_THROUGH` get a warning string, not a guess.

## The data (kite.py)

```python
k = kite.Kite.connect()                       # raises kite.LoginNeeded / kite.KiteError
k.instruments("NSE")                          # {tradingsymbol: {instrument_token, name, tick_size, lot_size, exchange}}
k.instrument("RELIANCE")                      # one row, KiteError if unknown
k.cas_symbols()                               # set of F&O-backed symbols (the 15:15 close)
k.historical("RELIANCE", "5minute", start, end)   # [(aware IST dt, o, h, l, c, v)], intervals: minute 3minute 5minute 10minute 15minute 30minute 60minute day
k.quotes(["RELIANCE", "INFY"])                # {sym: {last_price, volume, ohlc{open,high,low,close}, last_trade_time, ...}}
k.margins()                                   # equity margins: data["available"]["live_balance"] etc.
```

Bars are candle opens: the 09:15 5-minute bar covers 09:15:00 to 09:19:59.
Intraday bars for a CAS stock end at the 15:10 bar (5-minute). Rate limits
are inside the client (quote 1/s, historical 3/s), with retries on 429. Cache
anything per-day under `trader/cache/`.

## trader/config.json

`/trader-budget` ships `config.example.json`; `/trader-risk`, `/trader-exit`,
`/trader-entry`, `/trader-scanner`, `/trader-alerts`, `/trader-broker` read it.

```json
{
  "budget_inr": 100000,
  "cash_buffer_pct": 10,
  "max_position_pct": 75,
  "max_order_inr": 75000,
  "min_order_inr": 500,
  "max_loss_per_trade_inr": 500,
  "max_daily_loss_inr": 1500,
  "max_trades_per_day": 3,
  "max_orders_per_day": 10,
  "min_reward_risk": 1.0,
  "require_stop": true,
  "no_entries_before": "09:30",
  "no_entries_after": "14:30",
  "flat_by": "15:05",
  "exchange": "NSE",
  "product": "MIS",
  "order_style": "co",
  "use_mis_leverage": false,
  "mis_leverage_x": 1.0,
  "broker": "zerodha",
  "paper": true,
  "blocked": [],
  "allow_bse": false,
  "allow_fno": false,
  "allow_short": false
}
```

- `product` is always `MIS` in this pack (intraday, squared off). CNC is out.
- `order_style`: `co` (default) writes one Kite cover order: the entry plus
  the exchange-held stop-loss leg. `regular` writes a plain MIS entry and a
  separate MIS `SL-M` sell as `stop_leg`, for a stock where Zerodha does not
  allow CO.
- `use_mis_leverage`: false (default) sizes by cash as if paying the full
  price. true lets the position value reach `budget_inr` (less the buffer)
  times `mis_leverage_x`, the intraday leverage the user has confirmed
  Zerodha gives on that stock (Kite's margin calculator; up to about 5x on
  liquid equities). The risk tool caps it at the same number. Live cash comes
  from `k.margins()["available"]["live_balance"]` when there is a session.
- `paper`: Zerodha has no paper account. `true` (default) means
  `/trader-broker` records a simulated fill at Kite's last price into the
  local ledger and never calls the broker. `false` means Claude calls the
  Kite MCP `place_order` after the user's yes.
- `allow_fno`, `allow_short`, `allow_bse`: all false; the risk tool blocks
  anything that needs them.

## trader/watchlist.json (trader-scanner writes; trader-news, trader-entry read)

```json
{
  "created_by": "trader-scanner",
  "mode": "orb",
  "as_of": "2026-10-29T09:40:00+05:30",
  "universe": 1842, "scanned": 1842, "failed": 3,
  "hits": [
    {"symbol": "RELIANCE", "name": "RELIANCE INDUSTRIES", "setup": "orb",
     "price": 1174.4, "cas": true, "tick_size": 0.1,
     "opening_range": {"high": 1170.5, "low": 1155.6, "minutes": 15},
     "detail": "closed above the 15-minute range at 09:35 (+0.33 %)"}
  ]
}
```

Readers use `hits[].symbol`; everything else is for the user.

## trader/entry.json (trader-entry writes; trader-exit reads)

```json
{
  "created_by": "trader-entry",
  "symbol": "RELIANCE", "setup": "orb", "status": "triggered",
  "date": "2026-10-29", "interval": "5minute",
  "time": "09:40", "bar": 5, "price": 1174.4, "level": 1170.5,
  "opening_range": {"high": 1170.5, "low": 1155.6, "minutes": 15},
  "cas": true, "tick_size": 0.1
}
```

`status` is `waiting`, `triggered`, `expired`, `pre-open` or `closed`.
`price` is the close of the bar that triggered (the planned entry).

Bar numbering, everywhere (`bar` here, `entry_bar` in plan.json): 1-based,
counted in completed bars from the 09:15 open. Bar 1 is the 09:15 bar
(09:15:00 to 09:19:59), bar 12 is the 10:10 bar. `time` / `entry_time` is
the moment that bar completes: 09:15 + 5 x bar minutes, so bar 12 is
"10:15". The timemachine refuses a plan whose `entry_time` and `entry_bar`
disagree.

## trader/plan.json (trader-exit writes; timemachine, budget, alerts, risk read)

```json
{
  "created_by": "trader-exit",
  "symbol": "RELIANCE", "side": "buy", "date": "2026-10-29", "setup": "orb",
  "entry": 1174.4, "entry_time": "09:40", "entry_bar": 5, "interval": "5minute",
  "opening_range": {"high": 1170.5, "low": 1155.6, "minutes": 15},
  "stop": 1163.0, "stop_rule": "or_mid", "stop_label": "opening-range midpoint",
  "target": 1185.8, "target_r": 1.0, "target_label": "1R (one times the risk)",
  "risk_per_share": 11.4, "reward_risk": 1.0,
  "flat_by": "15:05", "cas": true, "tick_size": 0.1
}
```

Stop rules: `or_mid` (default), `or_low`, `price`, `pct`, `atr`. Target:
`2R` default, `1R`, or a price. The stop must be at least one tick below the
entry and within 10 % of it (Zerodha's cover-order range); the tool refuses
otherwise and says why.

## trader/orders.json (trader-budget writes; trader-risk, trader-broker read; broker's `flatten` rewrites)

```json
{
  "created_by": "trader-budget",
  "created_at": "2026-10-29T09:41:00+05:30",
  "status": "proposed",
  "paper": true,
  "note": "Proposals only. Nothing is placed until trader-risk passes each order and you say yes to it in the chat.",
  "source": "trader/plan.json (RELIANCE orb 2026-10-29)",
  "orders": [
    {
      "symbol": "RELIANCE", "exchange": "NSE", "side": "buy", "qty": 43,
      "product": "MIS", "order_style": "co",
      "kite": {
        "variety": "co", "exchange": "NSE", "tradingsymbol": "RELIANCE",
        "transaction_type": "BUY", "order_type": "MARKET", "quantity": 43,
        "product": "MIS", "trigger_price": 1163.0, "validity": "DAY", "tag": "dtsorb"
      },
      "stop_leg": null,
      "stop": 1163.0, "target": 1185.8,
      "why": "opening-range breakout on RELIANCE: 43 shares, bound by your Rs 500 max loss per trade (43 x 11.40 = Rs 490.20 at the stop)",
      "plan": {"entry": 1174.4, "stop": 1163.0, "target": 1185.8, "risk_per_share": 11.4,
               "max_loss_inr": 490.2, "setup": "orb", "entry_time": "09:40", "flat_by": "15:05"}
    }
  ]
}
```

- `kite` is exactly the body of Kite's `POST /orders/:variety`
  (https://kite.trade/docs/connect/v3/orders/). `tag` is alphanumeric, at
  most 20 characters.
- With `order_style: "co"`, `kite.variety` is `co` and `kite.trigger_price`
  is the stop; `stop_leg` is null. With `regular`, `kite.variety` is
  `regular` without a trigger, and `stop_leg` is a second full Kite body:
  `SELL`, `SL-M`, `MIS`, `trigger_price` = stop, same quantity.
- The target is never an order. `/trader-alerts` rings at it and
  `/trader-broker` builds the exit ticket when the user says so.
- An exit ticket (flatten, target hit, or the user's "get me out") is an
  order with `side: "sell"`, `order_style` of the open position, and for a
  CO position `kite` = `{"variety": "co", "parent_order_id": "...", "order_id": "..."}`
  for Kite's exit-cover-order call (`DELETE /orders/co/:order_id` with
  `parent_order_id`), or a plain MIS `SELL MARKET` for `regular` (then the
  `stop_leg` order is cancelled).

## trader/prices.json (trader-alerts writes; budget and risk read)

```json
{"as_of": "2026-10-29T09:41:30+05:30",
 "prices": {"RELIANCE": {"last": 1175.2, "time": "2026-10-29T09:41:28+05:30",
                         "prev_close": 1158.0, "source": "kite"}}}
```

A price is fresh for 15 minutes.

## trader/alerts.json (trader-alerts): rules, each rings once

Levels: `above`, `below`, `stop`, `target`, `pct` (from prev close), `time`
(`HH:MM` IST), `bell` (09:15 open), `flat_by`. `add --from-plan` writes the
stop, the target and `flat_by` from plan.json.

## trader/verdicts.json (trader-risk writes; trader-broker reads)

```json
{"created_by": "trader-risk", "date": "2026-10-29", "checked_at": "2026-10-29T10:21:00+05:30",
 "verdicts": {"ab12cd34ef56": {"verdict": "PASS", "reasons": [], "warnings": [],
                               "kind": "entry", "symbol": "RELIANCE"}}}
```

Keyed by the order hash above. `verdict` is `PASS` or `BLOCK`; `kind` is
`entry` or `exit`. A verdict counts only on its own `date` (IST). The broker
shows a ticket as READY only for a PASS on the exact same hash, today.

## trader/log.jsonl (trader-broker writes; trader-risk reads)

One JSON object per line:

```json
{"at": "2026-10-29T10:22:00+05:30", "date": "2026-10-29", "status": "approved",
 "order_hash": "ab12cd34ef56", "symbol": "RELIANCE", "side": "buy", "qty": 43, "kind": "entry",
 "max_loss_inr": 490.2, "stop": 1163.0, "paper": true, "reply": "yes", "plain": "BUY 43 RELIANCE ..."}
```

The first line for a hash is `approved` and carries the user's yes word for
word in `reply`. Later lines for the same hash carry `placed`, `denied`,
`failed` or `filled` (`filled` also has `kite_order_id`, `filled_qty`,
`average_price`). The risk tool takes the latest status per hash for today:
`placed` or `filled` entries count toward trades per day and their
`max_loss_inr` toward the daily loss; `denied` and `failed` do not; a hash
already `placed` or `filled` today is a duplicate.

## trader/portfolio.json and trader/fills.json (trader-broker)

`portfolio.json`: `{"cash_inr": 90000.0, "positions": {"RELIANCE": {"qty": 43, "avg": 1174.6, "product": "MIS", "parent_order_id": "...", "stop_order_id": "...", "entered_at": "..."}}, "paper": true}`.
`fills.json`: a list of fills with `order_hash`, `kite_order_id`,
`filled_qty`, `average_price`, `at`.

Exiting a cover order through Zerodha's kite-mcp-server: the stock server's
`cancel_order` tool drops `parent_order_id` (its Kite library never sends
it), so the pack runs the patched build in `../kite-mcp-server` (see its
PATCH.md): `cancel_order {"variety": "co", "order_id": "<the stop-leg order
id>", "parent_order_id": "<the entry order id>"}`. Kite exits the position
at market when the second leg is cancelled. The broker records both ids at
fill time for this. The same patch reads the pack's session file
(`KITE_SESSION_FILE`) instead of logging in a second time.

## trader/timemachine.json (trader-timemachine): `{plan, mc, history, verdict}`

Same shape as the original; the bars are Kite 5-minute candles over the last
60 trading days; `mc` has the odds, percentiles, fan bands, sample paths and
the close histogram.

## The two Indian skills that replace the US-only ones

- `trader-insiders` (replaces the US Congress tracker): NSE insider trading disclosures
  (SEBI PIT Regulation 7(2)), bulk deals and block deals from NSE's public
  pages, one alert per new filing, rows of who / category / buy or sell /
  quantity / value / mode, flagged against `portfolio.json` and
  `watchlist.json`. Writes `trader/insiders.json`.
- `trader-results` (replaces the US earnings analyst): quarterly results from NSE
  corporate filings (the financial results XBRL / announcements), revenue,
  EBITDA, PAT, EPS quarter on quarter and year on year, plus the upcoming
  results calendar for a symbol so a day trade is not held into a result.
  Writes `trader/results.json`.

## Orders through kite.py (trader-auto and the one-share probe)

`kite.py` carries Kite Connect's order calls so that exactly two callers can
use them without the MCP server in between:

- **trader-auto**, the unattended runner, which places the orders the rest of
  the pack has already proposed, risk-checked and the user has approved for
  the day, and exits them at the stop, the target or `flat_by`.
- **the explicit one-share probe command**, which the user runs by hand to
  prove the key, the session and the order path work with one share of a
  liquid stock before any size goes through.

Every other skill still never places, modifies or cancels an order. The
scanner, news, entry, exit, timemachine, budget, risk, alerts, insiders and
results tools read data only; `/trader-broker` writes tickets and ledgers and
Claude calls the Kite MCP tool after the user's yes. A pull request that adds
an order call anywhere else is wrong by definition.

```python
k = kite.Kite.connect()
k.place_order(order["kite"])                      # POST /orders/<variety>, the orders.json body as is -> order_id
k.modify_order("regular", oid, {"trigger_price": 1165.0})            # PUT /orders/<variety>/<id> -> order_id
k.modify_order("co", leg_id, {"trigger_price": 1165.0}, parent_order_id=oid)   # the CO second leg
k.cancel_order("regular", oid)                    # DELETE /orders/<variety>/<id> -> order_id
k.cancel_order("co", leg_id, parent_order_id=oid) # DELETE ...?parent_order_id=: exits the CO position at market
k.orders()                                        # today's order book, a list of order dicts
k.order_history(oid)                              # one order's states, oldest first, a list
k.order_trades(oid); k.trades()                   # fills for one order / the whole day
k.positions()                                     # {"net": [...], "day": [...]} from /portfolio/positions
k.cover_order_legs(oid)                           # {"parent": order or None, "second_leg": the OPEN second leg or None}
k.exit_cover_order(oid)                           # cancel the open second leg (with parent_order_id) -> leg order_id
k.wait_for_status(oid, timeout=30)                # poll order_history until COMPLETE / REJECTED / CANCELLED -> order dict
kite.estimate_charges(buy_value, sell_value)      # {"brokerage","stt","exchange","sebi","stamp","gst","total"}, an estimate
```

Rules the client enforces:

- `place_order` takes the `kite` dict exactly as `orders.json` holds it
  (`variety` inside) and checks it before anything is sent: variety in
  `regular co amo iceberg auction`, `transaction_type` BUY or SELL, product
  in `MIS CNC NRML MTF`, order type in `MARKET LIMIT SL SL-M`, a positive
  whole `quantity`, `trigger_price` for `co`, `SL` and `SL-M`, `price` for
  `LIMIT` and `SL`. Null fields are dropped. A bad body raises `KiteError`
  with a plain message and never reaches Kite.
- Placed is not filled. After `place_order`, call `wait_for_status` and read
  `status`, `filled_quantity`, `average_price` and `status_message` (the
  reason on a REJECTED order) from the dict it returns. It polls through the
  module's `_sleep`, so tests stub it.
- A cover order's exit is `exit_cover_order(parent_order_id)`: it reads the
  order book, finds the second leg whose `parent_order_id` matches and whose
  status is not COMPLETE / REJECTED / CANCELLED, and cancels it with
  `parent_order_id` in the query, which Kite documents as exiting the
  position at market. With no open leg it raises
  `KiteError("no open second leg for cover order ...")` and cancels nothing;
  check `positions()` before placing any other exit.
- Order calls run on the `other` throttle (10 a second). Kite also caps
  orders at 400 a minute, 5000 a day and 25 modifications per order; a 429
  is retried three times, every other rejection is raised at once with
  Kite's `error_type` and message (`InputException`, `OrderException`,
  `MarginException` ...). A `TokenException` is `LoginNeeded`.
- `estimate_charges(buy_value, sell_value, product="MIS")` is an estimate
  from the rates on https://zerodha.com/charges/ as read on 2026-10-29:
  brokerage 0.03 % or Rs 20 per executed order, whichever is lower; STT
  0.025 % on the sell side; NSE transaction charge 0.00307 % of turnover;
  SEBI Rs 10 per crore; stamp duty 0.003 % on the buy side; GST 18 % on
  brokerage + transaction + SEBI charges. For 8 RELIANCE bought at 1174.40
  and sold at 1185.80 that is Rs 10.04 against a gross of Rs 91.20. The
  contract note is the only exact figure.
- Tests for all of it live in `tests/test_kite.py` and are offline: a
  `FakeNet` answers every call, so the suite never places anything.
