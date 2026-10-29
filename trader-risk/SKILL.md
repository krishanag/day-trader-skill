---
name: trader-risk
description: >-
  Risk Manager. Checks every trade against your limits and stops anything
  over them, before it can reach Zerodha. For a day trade: a stop at the
  broker is required (a cover-order trigger or an SL-M stop leg), it must sit
  below the entry within 10 percent and on the tick, the loss at the stop
  must fit your max loss per trade (measured from the higher of the plan's
  entry and a fresh price), reward:risk must meet your minimum, the day's
  total risk must fit your daily loss limit, no more than your trades per
  day, and no new entries outside your entry window, past flat-by, or when
  NSE is not open. It also keeps the basics: MIS only, NSE only, no F&O, no
  shorts, blocked tickers, order size, position cap, cash buffer, malformed
  Kite bodies, duplicates and a daily order limit. Getting out is never
  blocked by a day limit. Prints PASS or BLOCK with the reason and records
  the verdicts so trader-broker only shows an order that passed. Use when the
  user says "check my order", "is this safe", "risk check", "does this break
  my rules", "can I take this trade", "how much have I risked today", or
  before /trader-broker, always.
argument-hint: "[path to orders.json]"
---

# trader-risk

The Risk Manager is the gate between a proposal and the broker. Every order
written by trader-budget or trader-broker's flatten passes through it. A
BLOCK is final for that order: it does not go to the user for a yes, and it
does not go to Zerodha. Most people who day trade lose money. These limits
keep each loss and each day's losses small. They do not make a trade win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/check.py"                            # trader/ files; writes trader/verdicts.json
python3 "${CLAUDE_SKILL_DIR}/check.py" --json
python3 "${CLAUDE_SKILL_DIR}/check.py" --no-write                 # look without recording
python3 "${CLAUDE_SKILL_DIR}/check.py" --now "2026-10-29 09:45"   # pin the clock (IST)
python3 "${CLAUDE_SKILL_DIR}/check.py" --offline saved-instruments.csv   # tick sizes from a saved Kite CSV
```

Run it from the user's project folder: `trader/` and `.env` are relative to it.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed orders (trader-budget, or trader-broker's flatten) |
| `trader/config.json` | the limits (trader-budget ships `config.example.json`) |
| `trader/plan.json` | the tick size and the CAS flag for the plan's stock |
| `trader/portfolio.json` | `cash_inr` and open positions; if missing, a fresh account funded with `budget_inr` |
| `trader/prices.json` | a fresh price raises the entry it measures risk from; also values holdings |
| `trader/log.jsonl` | what went to the broker today: trades per day, daily loss, the order limit, duplicates |
| `trader/fills.json` | today's fills: duplicates |
| Kite, through today's session | optional: the NSE instrument list (tick sizes, is the symbol real) and the CAS list. Without a session it uses the plan's tick size and says so |
| `trader/verdicts.json` | the verdicts it writes, keyed by the hash of each exact Kite body |

## Entries and exits

An **entry** opens a position: a buy, or a sell of something you do not hold
(a short). An **exit** is a sell of an open position in
`trader/portfolio.json`: the cover order's exit call, or a plain MIS sell.
Getting out must always be possible, so an exit is checked for format,
product, exchange and short sales only. Every other rule (day limits, entry
window, flat-by, loss, reward:risk, blocked list, size, duplicates) turns
into a warning for an exit, never a BLOCK.

## The checks

Orders are checked in file order, as if each passed order had filled, so a
batch that is fine order by order but breaks a limit together is caught.

| check | blocks when |
| --- | --- |
| format | no `kite` body; a body missing a field, a quantity that is not a whole number, a symbol or side that disagrees with the order, a LIMIT with no price, validity not DAY, a bad tag, a variety other than co or regular, a broken `stop_leg` |
| product | anything but MIS (no CNC, no NRML) |
| exchange | BSE while `allow_bse` is false; NFO, BFO, MCX or CDS always |
| asset | a futures or options contract while `allow_fno` is false; a symbol that is not in Kite's NSE equity list |
| short | a sell of what you do not hold (`allow_short` false) |
| blocked | (entries) the ticker is on the `blocked` list |
| size | (entries) under `min_order_inr` or over `max_order_inr` |
| position | (entries) the stock would be over `max_position_pct` of the account |
| cash | (entries) the buy needs more than cash minus the buffer (times `mis_leverage_x` with `use_mis_leverage` on) |
| stop | (entries) no stop at the broker while `require_stop` is true; a stop not below the entry and the latest price; more than 10 percent from the entry (Zerodha's cover-order range); not on the tick |
| loss | (entries) qty x (entry minus stop) over `max_loss_per_trade_inr` |
| reward:risk | (entries) (target minus entry) / (entry minus stop) under `min_reward_risk` |
| daily loss | (entries) the max losses of today's placed entries, plus this one, over `max_daily_loss_inr` |
| trades | (entries) `max_trades_per_day` entries already placed today |
| window | (entries) before `no_entries_before`, after `no_entries_after` or past `flat_by` (IST); the market is closed, in pre-open, or past 15:15 for a CAS stock (15:30 otherwise) |
| stale | (entries) the orders file was written on another day |
| duplicate | (entries) the same order twice in one batch, or already placed or filled today |
| daily limit | (entries) `max_orders_per_day` orders already placed today |

How the numbers work:

- **The entry it measures from**: the limit price for a LIMIT order. For a
  market order, the HIGHER of the plan's entry and a fresh price from
  `trader/prices.json` (under 15 minutes old). A plan written at 09:40
  cannot understate the risk at 09:55 after the price ran up. An older price
  is not used, and it says so.
- **Daily loss** counts the full max loss of every entry placed today, win
  or lose, as trader-broker logged it. Three full-size trades use up a
  Rs 1,500 day.
- **Today's orders** come from `trader/log.jsonl`: the latest line per
  order hash, counted when its status is placed or filled. An approved
  ticket that was never placed, a denied one or a failed one does not count.
- **The window** is IST, from the pack's own NSE clock (`trader-kite/nse.py`),
  never the machine's zone. Weekends and NSE holidays are closed; pre-open
  is 09:00 to 09:15; continuous trading ends 15:15 for CAS stocks (the ones
  with F&O contracts) and 15:30 for the rest. Set both `no_entries_*` and
  `flat_by` to null to switch the user's window off; a closed market still
  blocks.
- **The tick**: from Kite's instrument row when there is a session, else
  the plan's `tick_size`, else 0.05 with a warning.

Warnings do not block, but say them to the user: a LIVE account, no Kite
session, a stop that lives only in the plan, a price too old to use, the
broker's stop differing from the plan's, a holiday list that does not cover
the date yet.

## How Claude runs it

1. If the price may have moved since the plan, refresh it first
   (trader-alerts: `alerts.py quote RELIANCE --write`).
2. Run `check.py`. Exit code 0 means every order passed, 1 means at least one
   was blocked (or a file is missing, with one line why on stderr).
3. Tell the user the result in plain words, order by order: the max loss at
   the stop, reward:risk, and how much of today's limits it uses. For a
   BLOCK, give the reason and the fix (fewer shares, a closer stop from
   trader-exit, wait for the window, or skip the trade). Never change a
   limit yourself.
4. If the order changes in any way, run the check again. trader-broker
   matches each order to its verdict by the hash of the Kite body, so an
   edited order is not ready until it passes again, and a verdict from
   yesterday does not count today.

## Example: the reel

The RELIANCE cover order from trader-budget at 09:45 on Thursday 2026-10-29,
on a fresh Rs 1,00,000 paper account with the example config:

```
PASS  #1 buy RELIANCE 43 shares, entry
          entry 1174.40 (the plan's entry), stop 1163.00, target 1185.80: risk 11.40 a share
          max loss Rs 490.20 (limit Rs 500), reward:risk 1.00 (min 1)
          today before this: 0 of 3 trades, Rs 0 of Rs 1,500 daily loss

1 passed, 0 blocked at 2026-10-29 09:45 IST. Cash after the passed orders: Rs 49,500.80 (paper account).
```

One more share and it is blocked:

```
BLOCK #1 buy RELIANCE 44 shares, entry
          entry 1174.40 (the plan's entry), stop 1163.00, target 1185.80: risk 11.40 a share
          max loss Rs 501.60 (limit Rs 500), reward:risk 1.00 (min 1)
          today before this: 0 of 3 trades, Rs 0 of Rs 1,500 daily loss
        - loss: 44 shares x 11.40 risk a share = Rs 501.60 at the stop, over your Rs 500 max loss per trade
```

The same 43 shares checked at 09:55 with the 09:52 price of 1178.00 in
`trader/prices.json` are blocked twice: 43 x 15.00 = Rs 645 at the stop, and
reward:risk 0.52. At 09:29 they are blocked by the window (no new entries
before 09:30), at 14:31 too (none after 14:30), and on 2026-11-10 by the
Diwali Balipratipada holiday. The 15:08 sell that closes the position
passes, even though it is after flat-by: it is an exit.

## Rules

- A PASS only means the order is allowed to be shown to the user. It is not a yes.
- Every number comes from the config, the order, the portfolio, the prices or the broker log.
- Kite's price can lag by a moment; Zerodha's fill is the one that counts, and a stop can fill
  worse than its trigger in a fast market.

## What it never does

It never places an order, never edits an order to make it pass, never
loosens a limit, never blocks a sell that only gets the user out, and never
treats a PASS as the user's yes.

## Fine print

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold:
  trader-risk passed that exact order today, the user typed an explicit yes for that one order in
  the chat, and the user clicked Allow once on Claude's own card because the Kite MCP order tool is
  set to "Needs approval". Never set an order tool to "Always allow".
- No futures, options, BSE, short selling or MIS leverage unless the config allows it and the user
  confirms.
- Kite keys live in `.env` or the environment, never in the chat.
