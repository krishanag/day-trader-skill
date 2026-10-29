---
name: trader-entry
description: >-
  Entry Timer. Watches an NSE setup bar by bar on Zerodha Kite Connect and
  tells you the moment to get in. Reads Kite's 1-minute or 5-minute candles,
  builds the opening range (09:15 to 09:30 by default) and calls the entry on
  the first completed bar that CLOSES above the range high, or above a level
  you give it (like a 20-day high from the scanner), with an optional volume
  check. Says waiting and how far in rupees and %, triggered with the time and
  the price, expired, pre-open or closed, and writes trader/entry.json (with
  the stock's tick size and whether it closes at 15:15) for /trader-exit. Long
  side only. It never places an order. Use when the user says "tell me when to
  get in", "watch RELIANCE", "has it broken out", "did it break the opening
  range", "entry signal", "time my entry", "check my watchlist", or after
  /trader-scanner and /trader-news, before /trader-exit.
argument-hint: "[SYMBOL] [--setup orb|level] [--interval 5minute|minute] [--now 'YYYY-MM-DD HH:MM']"
---

# trader-entry

The Entry Timer turns a setup into one moment: the bar that confirms it. It
reads Kite's candles, 1-minute or 5-minute, and waits for each one to finish.
A price that pokes above the high in the middle of a bar is not an entry. A
bar that CLOSES above it is. Then it says the time and the price, writes them
down for the Exit Planner, and stops. It never buys anything. Day trading
loses most people money, and a clean signal does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).
Run it from the user's project folder: `trader/` and `.env` are relative to
it, and it needs today's Kite session from `/trader-kite`.

```bash
python3 "${CLAUDE_SKILL_DIR}/entry.py" check HINDUNILVR                 # 15-minute opening range, 5-minute bars
python3 "${CLAUDE_SKILL_DIR}/entry.py" check HINDUNILVR --write         # also save trader/entry.json
python3 "${CLAUDE_SKILL_DIR}/entry.py" check HINDUNILVR --setup level --level 1829.50
python3 "${CLAUDE_SKILL_DIR}/entry.py" check HINDUNILVR --interval minute --min-volume-x 1.5
python3 "${CLAUDE_SKILL_DIR}/entry.py" watch HINDUNILVR --every 30 --until 14:30   # one line per closed bar
python3 "${CLAUDE_SKILL_DIR}/entry.py" watchlist                        # every hit in trader/watchlist.json
```

Other flags: `--or-minutes N` (default 15), `--json`, `--now "2026-10-29
10:00"` (IST) to replay a moment from Kite's history. `watch` with `--now`
runs on a simulated clock and replays the day bar by bar without waiting.
Symbols are Kite trading symbols (`RELIANCE`, `INFY`); `RELIANCE.NS` and
`NSE:RELIANCE` are accepted and trimmed.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/kite_session.json`, `.env` | today's Kite session and the API key, through `trader-kite/kite.py`. No session: it says so and exits 1 |
| `trader/watchlist.json` | the scanner's hits (`hits[].symbol`), for `watchlist` |
| `trader/config.json` | `no_entries_before` (09:30) and `no_entries_after` (14:30), IST. Optional |
| Kite Connect | the day's candles (`/instruments/historical`), the instrument row (tick size) and the F&O list (which stocks close at 15:15), cached per day under `trader/cache/` |
| `trader/entry.json` | with `check --write`, or when `watch` triggers: symbol, setup, status, date, interval, time, bar, price, level, opening range, cas, tick_size |

## The rule

- **Opening range (orb).** The high and low of the first 15 minutes: the
  09:15, 09:20 and 09:25 five-minute bars, or fifteen 1-minute bars. The
  level to beat is the range high.
- **Level.** A price you give, like a 20-day high from `/trader-scanner`.
- **The trigger** is the first completed bar after the range that CLOSES
  above the level. A wick above is not a trigger. A close exactly at the
  level is not above it.
- **The time** is that bar's close time and the price is its close. Bars are
  numbered from 1 at 09:15, so bar N completes at 09:15 + N x 5 minutes: the
  09:55 bar is bar 9 and its time is 10:00. `/trader-timemachine` checks that
  the bar and the time agree.
- **Only closed bars count.** A bar is closed when the clock passes its
  start plus 5 (or 1) minutes. Kite also sends the bar in progress; it is
  shown as "forming" and never counts. Right after a close Kite's history
  can still be filling that bar in, so it counts once the next bar appears
  or a minute has passed.
- **Volume, if you want it.** `--min-volume-x 1.5` also needs the trigger
  bar's volume at 1.5 times the range bars' average. The 09:15 bar carries
  the opening auction's spill, so 1.0 is already strict.
- **Time limits.** No entries before 09:30 or after 14:30 (config).
- **The close.** Stocks with F&O contracts are in the Closing Auction
  Session: continuous trading ends 15:15, their last 5-minute bar is 15:10,
  and Zerodha squares off MIS positions in them at 15:12. Every other stock
  trades until 15:30. The tool reads which is which from Kite.
- **What it says.** Waiting (and how far the last close is from the level,
  in rupees and %), triggered, expired, pre-open (09:00 to 09:15) or closed
  (weekend, NSE holiday, before 09:00, or after the stock's continuous close
  with no trigger).

## How Claude runs it

1. Get the symbol from the user, or run `entry.py watchlist` after
   `/trader-scanner`. Check `/trader-news` for it first.
2. Run `entry.py check SYMBOL`. Tell the user the status in one line:
   waiting and how far, triggered at what time and price, or expired. If it
   says the Kite session is missing, run `/trader-kite` first.
3. To wait for it, Claude can run `entry.py watch SYMBOL --until 14:30` in
   the background, or `entry.py check SYMBOL --write` on a `/loop` every 5
   minutes. `watch` wakes just after each bar closes, prints one line per
   bar, and exits 0 on the trigger (it writes `trader/entry.json`) or 1 when
   the setup expires or the session closes. Keep the computer awake while it
   watches: a sleeping laptop pauses it, and it catches up, late, when it
   wakes.
4. When it triggers, say the time and the price, and that nothing was
   placed. If the trigger is more than a bar old, say that too: a late entry
   is a different trade.
5. Hand off to `/trader-exit` to set the stop and the target BEFORE anything
   is bought.

## Example: HINDUNILVR, Thursday 2026-10-29

5-minute bars. The opening range is Rs 1,804.00 to Rs 1,829.50. At 09:58 the
09:55 bar is still open, so it waits:

```
WAITING  last close Rs 1,827.60 (09:50 bar), Rs 1.90 under the range high Rs 1,829.50 (-0.10 %).
The 09:55 bar is still forming (Rs 1,832.00 so far, closes 10:00); it does not count until it closes.
```

At 10:00 the bar has closed:

```
HINDUNILVR  Thu 2026-10-29 10:00 IST  opening-range breakout (15-minute range) on 5-minute bars (Kite)
Opening range 09:15-09:30: high Rs 1,829.50, low Rs 1,804.00 (3 of 3 bars).
TRIGGERED 10:00 at Rs 1,832.00: the 09:55 bar (bar 9) closed above the range high Rs 1,829.50 (+0.14 %).
This is the moment to get in, if you take the trade. Nothing was placed.
Next: set the stop and target BEFORE you buy: /trader-exit (exit.py plan).
HINDUNILVR is a Closing Auction Session stock: continuous trading ends 15:15 and Zerodha squares off MIS at 15:12. Tick Rs 0.10.
```

With `--min-volume-x 1` the 09:55 bar is too thin (0.41x the range's volume)
and the trigger moves to 10:05 at Rs 1,841.30 (the 10:00 bar, bar 10, 1.02x).

## Rules

- Bar by bar, on Kite Connect candles. They are Zerodha's own data, but the
  history can lag the tape by a few seconds; say so when a minute matters.
- A signal is information, not an order, and not a promise. Nothing here
  promises winning trades or money.
- Long side only. This pack plans longs only. If the user wants a short,
  say that.
- One trigger per setup per day: the first close above. Later closes do not
  reset it.
- Never call an entry early because a bar looks like it will close above.

## What it never does

It never places an order, never sizes a position, never sets the stop (that
is `/trader-exit`), never moves the level after the fact, and never counts a
bar that has not closed. It never runs `/trader-broker` from a background
watch: nobody is there to say yes.

## Fine print

This is software that reads market data and does arithmetic. It is not
financial advice, and a trigger is not a recommendation. Most people who day
trade lose money. Nothing in this pack places an order except
`/trader-broker`, and only after `/trader-risk` has passed that exact order
and the user says yes to it in the chat; nothing is placed until the user
says yes. Paper mode is the default. Kite keys live in `.env` or the
environment, never in the chat.
