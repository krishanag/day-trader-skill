---
name: trader-exit
description: >-
  Exit Planner. Sets your stop and your target before you enter an NSE
  stock, so you know when to get out. Reads the entry from trader-entry (or
  a price you give), puts the stop at the opening-range midpoint by default
  (or the range low, your price, N % under the entry, or N x the 14-day ATR
  from Kite's daily bars), rounds it DOWN to the stock's own tick from Kite's
  instrument row, and sets the target at 2R (or 1R, or your price), also
  rounded down. Refuses a stop at or above the entry, a stop more than 10 %
  under it (Zerodha's cover-order range), a stop closer than 2 ticks (the
  spread), a target at or below the entry, or a flat-by time past Zerodha's
  15:12 square-off, and says why. Writes trader/plan.json for the Time
  Machine, the Budget Manager and Price Alerts. Long side only. Use when the
  user says "where's my stop", "set my stop and target", "plan the exit",
  "when do I get out", "stop loss", "what's my R", "risk reward", or right
  after /trader-entry triggers, before /trader-timemachine.
argument-hint: "[--stop or-mid|or-low|PRICE|N%|NxATR] [--target 2R|1R|PRICE] [--flat-by HH:MM]"
---

# trader-exit

The Exit Planner decides how a trade ends before it starts. The stop is where
the setup has failed. The target is where you take what you came for. Both
are set BEFORE the entry, written down and handed on, so nobody has to decide
anything in the middle of a fast move. A stop is a plan, not a guarantee: a
gap or a fast market can fill past it. Day trading loses most people money,
and a neat plan does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).
Run it from the user's project folder: `trader/` and `.env` are relative to
it. It only talks to Kite when it needs the tick size, the CAS flag or daily
bars for an ATR stop.

```bash
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan                           # trader/entry.json: midpoint stop, 2R target
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop or-mid --target 1R --write
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop 1.5xATR            # 1.5 x ATR(14) from Kite's daily bars
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --symbol HINDUNILVR --entry 1832 --or-high 1829.50 --or-low 1804
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --symbol ONGC --entry 227.60 --setup level --stop 1%
python3 "${CLAUDE_SKILL_DIR}/exit.py" show                           # the current trader/plan.json
```

Other flags: `--flat-by 15:05`, `--json`, `--now "2026-10-29 10:15"` (IST),
`--entry PRICE` with a waiting entry.json to plan ahead, `--setup level`,
`--date`, `--time`, `--entry-bar`, `--interval`. The old spellings
`price:X`, `pct:X` and `atr:K` still work.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/entry.json` | the trigger from trader-entry: symbol, entry price, time, bar, opening range, cas, tick_size |
| `trader/config.json` | `flat_by` (else 15:05) and `min_reward_risk` (1.0: a note here, trader-risk enforces it) |
| Kite Connect | the instrument row (tick size) and the F&O list (CAS) when entry.json lacks them or you give `--symbol`; 14 daily bars for an `NxATR` stop. Needs today's session from `/trader-kite` |
| `trader/plan.json` | with `--write`: entry, entry_time, entry_bar, stop and its rule, target and its R, risk a share, reward:risk, flat_by, cas, tick_size. trader-timemachine, trader-budget and trader-alerts read it |

## Stops and targets

| stop | where it goes |
| --- | --- |
| `or-mid` | the opening-range midpoint (the default for an opening-range breakout) |
| `or-low` | the opening-range low: wider, so fewer shares for the same max loss |
| `PRICE` | your price, e.g. `1816.70` |
| `N%` | N % under the entry, e.g. `1%` |
| `NxATR` | N times the 14-day ATR under the entry, e.g. `1.5xATR`, on Kite's daily bars up to the day before. With fewer than 14 days it averages what there is and says so |

A level setup has no opening range to lean on, so it needs a stop you name.

- **Ticks.** Prices snap to the stock's tick from Kite's instrument row:
  0.05 for most stocks, 0.01 under Rs 250, 0.10 for some large caps
  (HINDUNILVR and RELIANCE are 0.10). It is read, never assumed. The stop
  rounds DOWN to the tick (away from the entry); the target rounds DOWN too,
  never up. Risk = entry minus stop. Target = entry + R x risk. `2R` by
  default; `1R`, `1.5R` or a price also work.
- **Refused, with the reason.** A stop at or above the entry. A target at or
  below it. A stop closer than 2 ticks to the entry: it will not survive the
  spread. A stop more than 10 % under the entry: Zerodha's cover order holds
  the stop-loss leg and needs it within 10 % of the price. An entry.json
  that has not triggered (unless you give `--entry`). A short: this pack
  plans longs only.
- **Flat by.** `--flat-by`, else `flat_by` in the config, else 15:05. Out by
  then, win or lose, no overnight hold: these are MIS positions. For a
  Closing Auction Session stock (one with F&O contracts) it must be before
  15:12, when Zerodha squares off MIS for Rs 50 + GST; for any other stock
  before the 15:30 close. An entry at or after the flat-by time is refused.

## How Claude runs it

1. Run it right after `/trader-entry` triggers. Before that, `--entry PRICE`
   plans ahead at the level you expect.
2. Use the default stop for an opening-range breakout unless the user names
   another. A level setup needs one: ask.
3. Run `exit.py plan --write`. Read back the entry, the stop and its rule,
   the target and its rule, the risk a share, reward:risk, the tick and flat
   by.
4. If it refuses, say the reason and the fix (a wider stop, a farther
   target, an earlier flat-by). Never loosen anything on your own.
5. Say once that the stop is a plan, not a guarantee.
6. Hand off to `/trader-timemachine` to replay the plan, then `/trader-budget`
   to size it to the max loss per trade. Offer `/trader-alerts` for the stop,
   the target and the flat-by time. Claude can keep `entry.py watch` running
   in the background or on a `/loop` until a setup triggers, then come here.

## Example: HINDUNILVR, Thursday 2026-10-29

The entry is Rs 1,832.00 at 10:00 (the 09:55 bar, bar 9) and the opening
range is Rs 1,804.00 to Rs 1,829.50. Kite's row says the tick is Rs 0.10.
`exit.py plan --stop or-mid --target 1R --write`:

```
Exit plan: HINDUNILVR long, Thu 2026-10-29, opening-range breakout on 5-minute bars (MIS, intraday)
  Entry         Rs 1,832.00 at 10:00 (bar 9)
  Stop          Rs 1,816.70  opening-range midpoint: (Rs 1,829.50 + Rs 1,804.00) / 2 = 1816.75, rounded down to the Rs 0.10 tick
  Target        Rs 1,847.30  1R (one times the risk)
  Risk a share  Rs 15.30 (0.84 % of the entry, tick Rs 0.10)
  Reward:risk   1.0
  Flat by       15:05 IST, win or lose: no overnight hold (Zerodha squares off this CAS stock's MIS at 15:12)
```

The default 2R target would be 1832.00 + 2 x 15.30 = Rs 1,862.60.
trader-budget then sizes it: a Rs 500 max loss per trade buys
floor(500 / 15.30) = 32 shares. On ONGC (tick 0.01) the same rule puts the
midpoint 225.535 at Rs 225.53.

## Rules

- The stop and the target are set before the entry, never after.
- Never move a stop further from the entry once the trade is on.
- Plain numbers only: entry, stop, target, risk a share, reward:risk. It
  never prints a P&L or a return.
- Nothing here promises winning trades or money. A 1R target is not a
  prediction that the price gets there.
- Long side only. This pack plans longs only.

## What it never does

It never places an order, never sizes a position, never widens a stop to
make a plan pass, and never calls a stop a guarantee.

## Fine print

This is software that reads market data and does arithmetic. It is not
financial advice, and a plan is not a recommendation. Most people who day
trade lose money. Nothing in this pack places an order except
`/trader-broker`, and only after `/trader-risk` has passed that exact order
and the user says yes to it in the chat; nothing is placed until the user
says yes. Paper mode is the default. Kite keys live in `.env` or the
environment, never in the chat.
