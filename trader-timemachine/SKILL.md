---
name: trader-timemachine
description: >-
  Time Machine. Replays every possible outcome of an NSE day trade before you
  risk a rupee: 10,000 simulated sessions plus every past time the setup
  fired. Takes the plan from trader-exit (symbol, entry, stop, target,
  flat-by), pulls 60 trading days of 5-minute candles from Zerodha Kite
  Connect, rebuilds the rest of the day 10,000 times from the stock's own
  bars at the same time of day, and re-runs the same opening-range rule on
  every one of those real sessions. Prints the odds of target first, stop
  first, or neither by flat-by (15:05 IST by default), where the price sits
  at flat-by in rupees, the average R, the history, and one honest verdict,
  then hands off to /trader-budget. Use when the user says "run the time
  machine", "replay this trade", "what are the odds", "simulate this trade",
  "monte carlo", "backtest this setup", "how often does this hit the target",
  "is this a coin flip", or after /trader-exit writes a plan, before
  /trader-budget.
argument-hint: "[path to plan.json, or SYMBOL]"
---

# trader-timemachine

The Time Machine sits between the plan and the money. trader-exit has set
the entry, the stop, the target and the flat-by time. Before anything is
sized or sent, this skill asks one question: if today plays out like the
stock's own last 60 NSE sessions, how often does this plan hit the target
first, the stop first, or neither by flat-by? It answers with 10,000 replays
and with the real record of the same rule on every one of those sessions. It
is a simulation built from the stock's own recent 5-minute candles, not a
prediction. Most people who day trade lose money, and a good-looking replay
does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).
Run it from the user's project folder: `trader/` and `.env` are relative to it.

```bash
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run                    # trader/plan.json, 10,000 replays, seed 19
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --write            # also save trader/timemachine.json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --until 15:00      # end each replay earlier than the plan's flat_by
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --symbol RELIANCE --date 2026-10-29 \
    --entry 1174.40 --stop 1163.00 --target 1185.80 --entry-time 09:40
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" chart --from           # ASCII fan chart of the saved run
```

Other flags: `--replays N` (or `--sims N`), `--seed N` (default 19),
`--window N` (default 2 bars), `--offline FILE` (a saved bar file, `.json` or
`.json.gz`, no network), `--now "2026-10-29 10:15"` (IST) to pin the clock,
`--flat-by` and `--tick` with `--symbol`. It needs today's Kite session; if it
prints "not logged in to Kite", run `/trader-kite` first.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the plan from trader-exit: symbol, date, entry, entry_time / entry_bar, stop, stop_rule, target, target_r, opening_range, flat_by, cas, tick_size |
| `trader/kite_session.json`, `.env` | today's Zerodha session and the API key, through `trader-kite/kite.py` (never read directly) |
| Kite Connect historical API | one call: 5-minute candles for the 60 NSE trading days before the plan's date plus that day (Kite's data plan) |
| `trader/cache/` | the day's instrument list, for the symbol's token and tick size |
| `trader/timemachine.json` | with `--write`: `{plan, mc, history, verdict}` plus the notes and warnings. `mc` holds the odds, percentiles, the fan-chart bands, 160 sample paths and a 24-bin histogram of the price at flat-by. Times IST, prices rupees |

## How it works

1. **The pool.** It keeps only FULL sessions before the plan's date: 72
   five-minute bars, 09:15 to 15:10, for a stock in the Closing Auction
   Session (CAS, the F&O stocks, whose continuous trading ends 15:15), or 75
   bars to 15:25 for any other stock. The plan's `cas` flag says which; the
   bars themselves are checked and win when they disagree. Gaps, short
   sessions and the plan's own day are left out. Every bar becomes four
   ratios to the bar before it: open, high, low and close over the previous close.
2. **One replay.** Start at the entry price. For each bar left until
   flat-by, pick a past bar from the same time of day (plus or minus 2 bars)
   at random, and scale it to the current price. Bar by bar, it checks the
   stop first, then the target. A bar that touches both counts as the stop.
3. **10,000 replays** give four odds. Target first. Stop first. Neither,
   which means the trade was still open at flat-by (the plan's `flat_by`,
   15:05 by default, safely before Zerodha's 15:12 MIS square-off on CAS
   stocks; the last bar used is the one that starts before it, so for 15:05
   that is the 15:00 bar and its close is the exit). Green, which means
   target first or above the entry at flat-by.
   R is +1 at a 1R target, -1 at the stop, and otherwise the distance from
   the entry at flat-by divided by the risk. A gap through the stop still
   counts as -1R here. A real fill can be worse.
4. **The history.** The plan's own rule (the same opening range, stop rule
   and target R, stop and target snapped to the stock's tick) runs again on
   every one of those past sessions, out to the same flat-by. That is what
   really happened, not a simulation.
5. **The verdict** is one line from the numbers. It looks only at the
   replays that hit one side, and asks how often the target came first. A
   bigger target needs to win less often to break even: 1 in (1 + R). That
   is 50 % at 1R, 33 % at 2R, 25 % at 3R. Less than 5 points from break-even:
   "Close to a coin flip" at 1R, "Close to break-even" at any other R. 5 to
   15 points: "Leans toward the target" or "Leans toward the stop". 15 or
   more: "Strongly toward" one of them. So a 2R plan with target first
   21.3 % and stop first 59.0 % needs 33 % of the replays that hit a side
   and got 27 %: it leans toward the stop, it is not a wipe-out.
6. **Below the verdict**, when it applies: a line when more than 40 % of the
   replays hit neither (the price at flat-by decides those), and the
   average R when its sign goes against the lean. Those can only point the
   other way because of the replays that hit neither.

**The seed.** The draws come from Python's `random.Random(seed)`, seed 19 by
default. Same plan, same data, same seed: the same numbers, on any machine.
Say the seed with every result so the user can repeat it.

**5-minute bars only.** The entry bar, the opening range and flat-by all sit
on the 5-minute grid, and 60 sessions of 5-minute candles is what the pool
is built for (Kite returns them in one call). It refuses, in one plain line,
when there are fewer than 20 full sessions, when the entry bar is not in the
data (wrong date, not closed yet, a holiday, a future day), when the plan
does not make sense (a stop above a buy's entry, an entry off the 5-minute
grid, `entry_bar` and `entry_time` that disagree), or when there is no Kite
session. The candle Kite is still forming is never used.

**Bars and ticks.** `entry_bar` counts from 1 (bar 1 opens 09:15) and
`entry_time` is that bar's close, so bar 5 closes 09:40 and bar 12 closes
10:15. Prices snap to the plan's `tick_size` (Kite's instrument row: 0.05 or
0.10 for most stocks, 0.01 under Rs 250): stops away from the entry,
targets toward it.

## How Claude runs it

1. Make sure `trader/plan.json` is today's plan from `/trader-exit`. If there
   is none, run that first, or pass the plan as flags.
2. Run `timemachine.py run --write` from the project folder. The plan's
   `flat_by` already ends each replay at the time the user would get out;
   use `--until` only when the user asks for a different time.
3. Tell the user, in this order: the four odds, the median and the 5 to 95
   range of the price at flat-by in rupees, the average R, the history line,
   then the verdict line and any line under it exactly as printed. Say the
   seed.
4. If the verdict is a coin flip or close to break-even, say so plainly. Do
   not dress it up. If the history and the replays disagree, say that too.
5. Say once that this is built from the last 60 sessions and is not a
   prediction. A results announcement, a headline or a circuit limit can do
   things those sessions never did. Check `/trader-news` and
   `/trader-results` for the stock before going on.
6. If the user still wants the trade, hand off to `/trader-budget` to size it.

## Example: RELIANCE, Thursday 2026-10-29

The 15-minute opening range is Rs 1,155.60 to Rs 1,170.50. The 09:35 bar
closes above it: entry Rs 1,174.40 at 09:40 (bar 5), stop Rs 1,163.00 (the
range midpoint, 1,163.05, snapped down to the 0.10 tick), target
Rs 1,185.80 (1R, risk Rs 11.40 a share), flat by 15:05. Real Kite data, the
test fixture in this repo.

```
10,000 replays of the rest of the session, 09:40 to 15:05 IST (65 five-minute bars), seed 19.
  target first   22.9 %
  stop first     28.0 %
  neither        49.0 %   still open at 15:05
  green          45.3 %   target first, or above the entry at 15:05
  Price at 15:05: median Rs 1,173.06, 90 % of replays between Rs 1,156.95 and Rs 1,189.91.
  Average R: -0.074
History: the same rule on each of those 60 real sessions fired 27 times:
13 target, 11 stop, 3 neither. Average R +0.065.
Leans toward the stop: target first 22.9 % vs stop first 28.0 %.
49.0 % of the replays hit neither by 15:05; the price at 15:05 decides those.
```

At 1R the target has to come first in half of the replays that hit one
side. It did in 45.0 %. The replays lean toward the stop, and the real
history came out slightly better than even. Both go to the user before
`/trader-budget` sizes anything.

## Rules

- Every number comes from the run. Never round a coin flip up into an edge.
- The verdict line and the lines under it are printed by the tool. Repeat
  them, do not rewrite them. Never judge a 2R or 3R plan by target first vs
  stop first alone: the verdict already weighs the target's R.
- Simulated odds are not predictions and not promises. Say it once, plainly.
- Bars are Kite's 5-minute candles, checked bar by bar, never tick by tick
  and never real time. Kite Connect is Zerodha's official API; it can still
  lag or rate-limit you.
- Always say the seed, so any run can be repeated.
- One plan per run. A changed entry, stop, target, flat-by or date is a new run.

## What it never does

It never places an order, never sizes a position, never changes the plan,
and never calls a result a sure thing. It never prints a P&L or a profit.
It never uses a candle that has not closed yet, and never mixes the plan
day's bars into the pool.

## Fine print

- Education and tooling, not financial advice. The verdict is arithmetic on
  the stock's own last 60 sessions, not a recommendation.
- This is a simulation, not a prediction. The future can do things those 60
  sessions never did: a results announcement, a headline, a circuit limit.
- Nothing is placed until the user says "yes". Only `/trader-broker` can lead
  to an order, and only after `/trader-risk` passes that exact order and the
  user types yes for it in the chat; Claude's own "Allow once / Deny" card on
  the Kite MCP order tool asks once more. Never set that tool to "Always allow".
- Paper by default (`"paper": true` in `trader/config.json`). Going live is the
  user's own decision, in their own config. MIS intraday only: no F&O, no
  short selling, no leverage unless the config allows it and the user confirms.
- Kite API keys live in `.env` or the environment, never in the chat.
