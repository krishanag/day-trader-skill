---
name: trader-scanner
description: >-
  Chart Scanner. Scans NSE charts for setups through Zerodha Kite Connect:
  opening-range breakouts, flags, oversold bounces. Builds the list of liquid
  NSE stocks from Kite's instrument dump and one quote pass (plain EQ series,
  Rs 50 and up, Rs 5 crore a day traded), runs a pre-market scan of daily
  closes (20-day closing-high breakout, bull flag, reclaimed the 20-day
  average, oversold, oversold bounce) and an intraday opening-range breakout
  scan on 5-minute bars (the 09:15, 09:20 and 09:25 bars), then writes
  trader/watchlist.json for trader-news and trader-entry. Rupees, IST, bar by
  bar. Use when the user says "scan the market", "find me setups", "what is
  breaking out", "opening range breakouts", "scan for bull flags", "oversold
  stocks", "pre-market scan", "build my watchlist", "what should I look at
  today", or at the start of a trading day before /trader-news.
argument-hint: "[universe | daily | orb] [--at HH:MM]"
---

# trader-scanner

The Chart Scanner is the first step of a day trade. It reads NSE charts
through your Zerodha Kite session and lists the ones that match a rule. That
is all it does. A scan is a list of charts that match a rule, not a list of
stocks to buy, and a setup on a chart is not a prediction. Most people who
day trade lose money; the scanner does not change that. It only saves you
from flipping through 3,000 charts by hand.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).
Run it from the user's project folder: `trader/` and `.env` are relative to it,
and it needs today's Kite session (`/trader-kite`).

```bash
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe                                # -> trader/universe.txt
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily                                   # before the open -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily --symbols RELIANCE,INFY,SBIN      # or --watchlist FILE, --limit 200
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb                                     # after 09:35 IST, the daily hits -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --at 10:15 --universe               # cut the clock at 10:15, the whole universe
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --now "2026-10-29 10:15" --symbols RELIANCE,SJVN   # replay a past session
```

Flags: `--min-price 50` (rupees) and `--min-value` (rupees traded, price times
volume: 50000000 = Rs 5 crore for `universe` and `daily`, 0 for `orb`),
`--or-minutes 15`, `--max-confirm 200` (orb stage 2), `--now "2026-10-29 10:15"`
(IST) to pin the clock, `--no-write`, `--show N`, `--json`. Exit code 0 when
the scan found something, 1 when it found nothing or could not run (the
reason is one plain line). Without a Kite session it prints the login line
from `/trader-kite` and stops.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/kite_session.json` | today's Kite session, through `kite.Kite.connect()`; `/trader-kite` writes it |
| `trader/universe.txt` | written by `universe`: one `SYMBOL\|Name` a line, read by `daily` (and by `orb --universe`) |
| `trader/watchlist.json` | written by `daily` and `orb`: `mode`, `as_of` (ISO, +05:30), `universe`, `scanned`, `failed`, `hits[]` with `symbol`, `name`, `setup`, `price`, `cas`, `tick_size`, `opening_range` (orb), `detail`. trader-news and trader-entry read `hits[].symbol`. `orb` reads the daily hits by default; an orb file keeps the symbols it scanned under `input.symbols`, so a re-run sees every chart again |
| `trader/config.json` | read by `orb`: `no_entries_after` (default 14:30) |
| `trader/cache/` | the instrument dump and the F&O (CAS) list per day (kite.py); `quotes-<date>.json`, the universe's quote pass; `daily/<SYMBOL>-<session>.json`, one file per symbol per session |

## The universe

`universe` reads Kite's NSE instrument list (10,251 equity rows on
2026-10-29) and keeps a row when, in this order:

1. The trading symbol has no two-character series suffix. `XYZ-BE`, `-BZ`
   (trade for trade), `-SM`, `-ST` (SME), `-SG`, `-GS`, `-GB` (gilts and
   gold bonds), `-N0` to `-NZ`, `-Y*`, `-Z*` (bonds) and the rest are out:
   7,173 rows. `BAJAJ-AUTO` and `NAM-INDIA` keep their hyphen; what follows
   it is not a series code.
2. The name does not mark a fund unit: the word ETF or NAV (the iNAV
   ticks), or a fund house's scheme listing ("DSPAMC - DSPGOLD",
   "BFAM - BANKBETF"): 701 rows. A liquid ETF with a plain name would slip
   through; none did on Oct 29.
3. One quote pass over what is left (2,377 symbols, 500 a call, one call a
   second, 5 calls): `last_price` at least `--min-price` (Rs 50) and
   `volume` times `last_price` at least `--min-value` (Rs 5 crore). Before
   the open the quote carries the previous session's figures; during the
   session it is the day so far, so run it before 09:15 or after the close.

On Oct 29 2026 at 19:26 IST that kept 951 stocks in 4.7 seconds (456 under
Rs 50, 970 under Rs 5 crore). The quote pass is saved under
`trader/cache/quotes-<date>.json`, so a second run the same day is free. Run
it once a week or so; the F&O list and the instrument dump refresh daily on
their own.

## The daily scan (before the open)

`daily` fetches one "day" historical call per symbol, the last 60 sessions
(`nse.previous_trading_days`, so holidays are skipped), at Kite's 3 calls a
second: 1,000 symbols take about six minutes the first time. Every chart is
cached under `trader/cache/daily/<SYMBOL>-<session>.json`, keyed by the last
complete session, so a re-run that morning is free and a run during the
session uses yesterday's bar: today's daily bar counts only after 15:35 IST,
when the closing auction has set the close. Liquidity comes from the last
complete bar: close at least `--min-price` and close times volume at least
`--min-value`.

The setups are computed from daily closes only:

| setup | the rule on closes |
| --- | --- |
| `breakout_20d` | the last close is above every close of the 20 sessions before it |
| `bull_flag` | up more than 10 % over the 10 sessions before the last 5, then the last 5 closes inside a 5 % range |
| `reclaim_ma20` | the close before was under its 20-day average, the last close is above it |
| `oversold` | RSI 14 (Wilder) under 30 |
| `oversold_bounce` | RSI 14 was under 30 yesterday and today closed up |

A closing-high breakout fires more often than an intraday-high one. Hits
are listed most traded first (price times volume of the last bar), with
`setup` the first rule that matched and `setups` all of them.

## The opening-range breakout scan (during the session)

The opening range is the first `--or-minutes` (15) of the session: the
09:15, 09:20 and 09:25 five-minute bars. A hit is the first completed
5-minute bar to CLOSE above the range high, at that bar's close time. Bars
are numbered from 1 at 09:15, so the 09:35 bar is bar 5 and it closes at
09:40.

1. Stage 1, every symbol: one quote pass (500 a call, never cached). A chart
   goes on only if it clears the price floor and the day's high is above the
   day's open. That is a necessary condition (a close above the range high is
   above the open), so no breakout is lost; on a strong morning most charts
   are candidates.
2. Stage 2, each candidate: the day's 5-minute candles, one call each at 3 a
   second. Range high = the highest high of the range bars, range low = the
   lowest low. Stage 2 checks at most `--max-confirm` (200) candidates, the
   most traded first (price times volume so far), about 70 seconds, and
   prints PARTIAL with the count it skipped. `--max-confirm 0` checks them
   all; the 951-stock universe is then about six minutes.

By default `orb` scans the daily watchlist (a handful of names, a few
seconds), `--universe` the whole file, `--watchlist FILE` or `--symbols` a
list of your own. A chart is usable only when every 5-minute bar from 09:15
to the cutoff is there; a missing bar means a halt or a thin stock. A bar
counts only once it is complete (start + 5 minutes). Stocks in the Closing
Auction Session (the F&O names, `cas: true` in the hits) stop at the 15:10
bar; every other stock runs to 15:25. Before 09:35, on a weekend or holiday,
after the close, or after `no_entries_after` (an expired setup) it prints one
plain line and writes nothing. A past day replays live from Kite with
`--now "YYYY-MM-DD HH:MM"`; stage 1 is skipped then (quotes are today's).

## Data and limits

- Kite Connect is Zerodha's official API; its candles are the exchange's.
  Rate limits are in the shared client: quotes one call a second, candles
  three a second, with retries on a 429. If another tool shares your API
  key at the same time both slow down.
- Partial results are said as partial: "Fetched 940 of 951 charts (11 failed
  or had no data)". Never round a partial scan up to "the whole market".
- NSE's 2026 holiday list lives in `trader-kite/nse.py`; a date past it is
  flagged, not guessed.

## How Claude runs it

1. Once a week or so: `scan.py universe`. Say how many stocks it kept and
   how many it dropped under each floor.
2. Before 09:15: `scan.py daily`. Tell the user the counts (fetched, liquid,
   scanned, hits) and the top few by setup, with the session the closes run
   through.
3. After 09:35 (10:15 is the classic): `scan.py orb`. Tell the user how many
   usable charts broke their range and when, that a later hit is closer to
   the price now, and whether the scan was PARTIAL.
4. The user picks one chart. Hand it on: `/trader-news SYMBOL` (what is
   moving it), then `/trader-entry SYMBOL` (the entry bar by bar). Never
   pick for them.
5. If it prints "not logged in to Kite" or "expired at 06:00 IST", run
   `/trader-kite` first and come back.

## Example: Thursday Oct 29 2026

The 10:15 scan over six names, live from Kite (the fixtures in
`tests/fixtures/trader-scanner/` are these candles):

```
python3 scan.py orb --at 10:15 --symbols RELIANCE,SJVN,ZEEL,TCS,ICICIBANK,HUDCO
Stage 1: 6 of 6 quoted; 0 under the price / value floor, 0 never traded above the open; 6 candidates.
Stage 2: fetched 6 charts; 6 usable, checked with real highs and lows.
5 of 6 charts closed above their opening-range high by 10:15:
  09:35  SJVN               Rs 52.60  range 50.50 to 52.21  +0.75% over
  09:35  ZEEL               Rs 70.46  range 67.80 to 70.24  +0.31% over
  09:40  RELIANCE        Rs 1,174.40  range 1,155.60 to 1,170.50  +0.33% over  CAS
  09:45  HUDCO             Rs 148.75  range 147.05 to 148.70  +0.03% over
  10:10  ICICIBANK       Rs 1,378.90  range 1,367.50 to 1,377.00  +0.14% over  CAS
```

RELIANCE's range is the 09:15 to 09:25 bars, high 1,170.50, low 1,155.60;
bar 5 (09:35) closed at 1,174.40, 0.33 % over the high, at 09:40. TCS never
closed above its range (2,480.90 to 2,509.50). The user picks RELIANCE and
Claude hands it to `/trader-news RELIANCE`, then `/trader-entry RELIANCE`.

The pre-market scan that morning (closes through Wed Oct 28) tags LT a
20-day closing-high breakout and a reclaim of the 20-day average, INFY a
bull flag, HUDCO oversold and an oversold bounce, ZEEL an oversold bounce.
YESBANK was oversold too, but at Rs 15.61 it is under the price floor.

## Rules

- Say what was scanned: how many symbols, how many charts came back, how many
  were usable, and the time of the data.
- Say "matched a rule", never "a buy", "a winner" or "a good trade".
- Never print a P&L, a return or a profit figure.
- A hit is stale once the price has moved on. Re-run before acting on it.

## What it never does

It never places, sizes or checks an order, never says which stock to buy,
never calls data real time, and never promises that a setup works.

## Fine print

This is software that reads market data and does arithmetic. It is not
financial advice. Most people who day trade lose money. Nothing in this pack
places an order except `/trader-broker`, through Zerodha's Kite MCP server,
and only after `/trader-risk` passed that exact order and the user typed yes
for it in the chat; nothing is placed until the user says "yes". Paper
trading (`"paper": true`) is the default. No F&O, no short selling, no MIS
leverage unless the config allows it and the user confirms. Keys live in
`.env` or the environment, never in the chat.
