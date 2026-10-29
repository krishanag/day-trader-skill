---
name: trader-alerts
description: >-
  Price Alerts. Rings when a price, a stop, the opening bell or the flat-by
  time hits. Turns the trade plan into three alerts in one go (the stop, the
  target and the flat-by), plus above / below / stop / target levels, percent
  moves from the previous close, any time of day and the 09:15 bell, each
  ringing once so you are not spammed. Knows the NSE clock from the pack's
  shared calendar (09:15 open, 15:15 close for F&O stocks with the closing
  auction after it, 15:30 for the rest, NSE's 2026 holidays, Zerodha's
  15:12 MIS square-off), reads Kite quotes through today's session and
  writes trader/prices.json for sizing and the risk check. Use when the
  user says "alert me when", "tell me if RELIANCE hits", "watch my stop",
  "watch my trade", "remind me to square off by 3:05", "is the market open",
  "when does NSE open", "is today a holiday", "get me a price", or right
  after /trader-exit writes a plan.
argument-hint: "[symbol] [price]"
---

# trader-alerts

Price Alerts watches the numbers so the user does not have to stare at a
chart. An alert is information, never an order: when a stop alert rings,
nothing is sold by this skill. With a cover order the stop-loss leg already
sits at the exchange and does the work; the alerts tell the user what
happened and when it is time to be flat, before Zerodha's own square-off.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).
Run the tools from the user's project folder: `trader/` and `.env` are
relative to it.

```bash
python3 "${CLAUDE_SKILL_DIR}/alerts.py" clock                            # IST now, NSE phase, next open, flat-by, holiday
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --from-plan                  # stop, target, flat-by from trader/plan.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --bell
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol RELIANCE --stop 1163
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol RELIANCE --target 1185.8
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol SBIN --pct 3
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --time 14:30 --label "last entry window closes"
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --flat-by                    # at the config's flat_by (default 15:05)
python3 "${CLAUDE_SKILL_DIR}/alerts.py" check                            # exit 0 = something rang
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote RELIANCE INFY --write      # -> trader/prices.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote --holdings --write         # portfolio + plan symbols
python3 "${CLAUDE_SKILL_DIR}/alerts.py" list
python3 "${CLAUDE_SKILL_DIR}/alerts.py" reset reliance-plan-stop
```

`--now "2026-10-29 15:05"` pins the clock (IST); `--offline DIR` reads saved
`kite-quote-SYMBOL.json` files instead of Kite.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the trade from trader-exit, for `add --from-plan` |
| `trader/alerts.json` | the rules (`alerts.example.json` in this folder) |
| `trader/state/alerts.json` | which rules rang and when, so each rings once |
| `trader/prices.json` | the latest Kite quotes, merged in: `last`, `time`, `prev_close`, `source: kite`, for trader-budget and trader-risk (fresh for 15 minutes) |
| `trader/portfolio.json` | the symbols held, for `quote --holdings` |
| `trader/config.json` | `flat_by`, for the flat-by alert and the clock |
| `trader/kite_session.json`, `.env` | today's Kite session and the API key, through `trader-kite/kite.py` |

## Rule types

| type | rings when | rings again |
| --- | --- | --- |
| `above` / `target` | last price at or above `price` | after `reset`, or on its own if `"rearm": true` and the price fell back |
| `below` / `stop` | last price at or below `price` | same |
| `pct` | moved `pct` percent from Kite's previous close (or `ref`), optional `direction` up or down | once per day at most |
| `time` | the IST clock passed `at` ("14:30") on a trading day | once per trading day |
| `bell` | NSE is open (09:15) | once per trading day |
| `flat_by` | the flat-by time passed (`at`, default the config's `flat_by`, else 15:05) | once per trading day |

A rule with `"date"` is watched on that date only; `--from-plan` rules carry
the plan's date, so yesterday's stop never rings today. A `flat_by` or
`time` rule with a `symbol` says which position to close.

## Quotes

- Every quote comes from Zerodha Kite Connect through `trader-kite/kite.py`:
  today's session (`trader-kite/login.py`), the Connect plan's market data.
  One `/quote` call covers every symbol in the rules, at most one call a
  second; the client retries a 429 when another tool shares the key.
- `prev_close` is Kite's `ohlc.close`; `time` is the last trade time with
  `+05:30`. A price in `trader/prices.json` is fresh for 15 minutes:
  trader-risk only uses one that young.
- No session, no quote: the tool prints "not logged in to Kite: run
  python3 trader-kite/login.py" and exits 1. It never falls back to another
  source.
- A Kite quote is the broker's own, but the price that fills is still
  whatever the market gives.

## The NSE clock

Hours and holidays come from `trader-kite/nse.py` only; this skill keeps no
list of its own. Pre-open 09:00 to 09:15; continuous trading from 09:15 to
15:15 for stocks with F&O contracts (then the Closing Auction Session to
15:35) and to 15:30 for the rest; Zerodha squares off MIS positions in the
F&O stocks at 15:12 and charges Rs 50 + GST each time; the pack's flat-by
is 15:05. Weekends and NSE's 2026 holiday list are closed days; the Diwali
Muhurat session (a Sunday evening) is reported as a weekend. A date after
the known list says the holiday list is not in the pack yet. `clock` warns
when the config's `flat_by` is not before 15:12.

## Steps

1. Right after trader-exit writes the plan, run `add --from-plan`. Confirm
   the three alerts back to the user: the stop, the target and the flat-by.
2. Run `check` while the trade is open: every few minutes from a Claude
   scheduled task or `/loop`. Each run fetches only the symbols in today's
   rules and rings each alert once. A check reads the latest quote; it is
   not a live feed, and a price can pass a level between two checks.
3. Tell the user what rang in one line each. A stop alert means the cover
   order's stop leg has probably filled: check `get_positions` or
   `get_order_history` and record the fill with trader-broker. A target
   alert means it is time to ask whether to take it: trader-broker `exit`,
   then trader-risk, then the user's yes. "Flat by" means close anything
   still open the same way, before Zerodha does it at 15:12. Never act for
   them.

## Example

RELIANCE on Thursday 2026-10-29: the 15-minute opening range was 1155.6 to
1170.5, the 09:35 candle closed above it at 1174.4, stop 1163.0 (the range
midpoint, snapped down to the 0.10 tick), target 1185.8 (1R), flat by
15:05. `add --from-plan` writes `reliance-plan-stop`, `reliance-plan-target`
and `reliance-flat-by` for that day. A `check` at 09:41 reads Kite's quote
(the saved one is the day's last, 1167.40, between the stop and the target):

```
NSE OPEN (2026-10-29 09:41 IST, continuous trading).
Nothing hit. 4 rule(s) watched today, RELIANCE Rs 1,167.40.
```

At 15:05:

```
ALERT Flat by 15:05: it is 15:05 IST. Zerodha squares off MIS in F&O stocks at 15:12 (Rs 50 + GST each time);
continuous trading ends 15:15 for them, 15:30 for the rest. If RELIANCE is still open, close it: trader-broker
ticket.py exit RELIANCE builds the exit ticket, then trader-risk and your yes.
```

It rings once. The next morning the plan's three alerts are no longer
watched; with `add --bell`, the bell rings at 09:15.

## How Claude runs it

1. `clock` first when the user asks about the market or before the first
   check of the day; say the phase, the next open and the flat-by in one line.
2. `add --from-plan` right after a plan exists; read the three rules back.
3. `check` on a schedule while a position is open; relay each ALERT line as
   a fact, then the one next step (record a fill, or build the exit ticket).
4. `quote --holdings --write` before trader-budget or trader-risk runs, so
   `trader/prices.json` is fresh.
5. On "not logged in to Kite", run `/trader-kite` and come back.

## Rules

- An alert is a fact about a price or the clock. Say it as a fact.
- Never present a quote as a live tick; it is the last trade when asked.
- Confirm every new rule back to the user: symbol, type, level, day.

## What it never does

It never places, cancels or changes an order, never claims a quote is a
live feed, and never predicts where a price is going.

## Fine print

- Education and tooling, not financial advice. A PASS is a rules check, not
  a recommendation. Most people who day trade lose money.
- Paper by default (`"paper": true`). Going live is the user's own decision,
  in their own config.
- Nothing in this pack places an order except trader-broker, and nothing is
  placed until the user says "yes" to that exact ticket: trader-risk passed
  it today, the user typed yes in the chat, and the user clicked Allow once
  on Claude's own card because the Kite MCP order tools are set to "Needs
  approval". Never set an order tool to "Always allow".
- No F&O, no short selling, no BSE and no MIS leverage unless the config
  allows it and the user confirms.
- Keys live in `.env` or the environment, never in the chat.
