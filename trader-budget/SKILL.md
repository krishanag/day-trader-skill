---
name: trader-budget
description: >-
  Budget Manager. Sizes every position to your budget and your max loss per
  trade, in rupees. Reads the trade plan from trader-exit (entry, stop,
  target), finds the whole shares that fit inside four limits (max loss per
  trade, position cap, max order, cash after your buffer), says which limit
  decided it, and writes ONE Zerodha MIS order to trader/orders.json in the
  exact shape of Kite's place_order body: a cover order with the stop as its
  trigger, or a plain entry plus an SL-M stop leg. A stop too far away for
  your max loss is a plain refusal, not a smaller order. Nothing is placed.
  Use when the user says "how many shares", "size this trade", "how much
  should I buy", "I have one lakh", "I only want to risk 500 rupees a
  trade", "set my rules", "keep 10% in cash", or after /trader-exit writes a
  plan.
argument-hint: "[budget in rupees]"
---

# trader-budget

The Budget Manager turns a plan ("buy RELIANCE at 1174.40, stop 1163.00")
into an exact order ("buy 43 shares"), inside rules the user sets once. The
stop decides the size: the further away the stop, the fewer shares. It
writes a proposal only. trader-risk checks it and trader-broker asks the
user yes or no. Day trading loses most people money. Sizing to a max loss
keeps each loss small; it does not make a trade win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py"                         # trader/plan.json + trader/config.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --write                 # write trader/orders.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit --write         # a LIMIT entry at the plan's entry
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit 1174.00 --json  # a LIMIT entry at 1174.00
python3 "${CLAUDE_SKILL_DIR}/size.py" --offline saved-instruments.csv   # tick size from a saved Kite CSV
```

Run it from the user's project folder: `trader/` and `.env` are relative to
it. `--now "2026-10-29 09:45"` pins the clock (IST). A plan for another day
is refused: make a new one with trader-exit.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/config.json` | the rules (start from `config.example.json` in this folder) |
| `trader/plan.json` | the trade: entry, stop, target, setup, tick size, flat-by time (trader-exit writes it) |
| `trader/prices.json` | optional: a fresh price above the plan's entry means a bigger risk (trader-alerts writes it) |
| `trader/portfolio.json` | optional: its `cash_inr` caps the size (trader-broker keeps it) |
| Kite, through today's session | optional: the stock's instrument row (tick size) and, with no portfolio file, your live balance from the margins call. Without a session it sizes from the plan and the config |
| `trader/orders.json` | the one proposed order it writes with `--write` |

## Step 1: set the rules once

If `trader/config.json` does not exist, copy `config.example.json` into
`trader/config.json` and ask the user for each value in plain words:

- **budget_inr**: how much money this is for, in rupees. Only money they can afford to lose.
- **cash_buffer_pct**: how much always stays in cash (default 10).
- **max_loss_per_trade_inr**: the most one trade may lose if the stop fills at the stop (default Rs 500).
- **max_position_pct**, **max_order_inr**, **min_order_inr**: the biggest position (75% of the
  budget), the biggest order (Rs 75,000) and the smallest order (Rs 500).
- The day limits trader-risk enforces: **max_daily_loss_inr** (1,500), **max_trades_per_day** (3),
  **max_orders_per_day** (10), **min_reward_risk** (1.0), **require_stop** (true),
  **no_entries_before** / **no_entries_after** ("09:30" / "14:30") and **flat_by** ("15:05"), all IST.
  Keep flat_by before 15:12: that is when Zerodha squares off MIS positions in CAS stocks itself,
  for Rs 50 plus GST each time.
- **order_style**: `co` (default) is one Kite cover order, the entry plus the stop-loss leg the
  exchange holds. `regular` is a plain MIS entry and a separate SL-M stop, for a stock where
  Zerodha does not allow cover orders.
- **use_mis_leverage** / **mis_leverage_x**: off by default, so the tool sizes as if paying the
  full price. Turn it on only after the user has checked the intraday leverage Zerodha gives on
  that stock (Kite's margin calculator) and confirmed the multiple.
- **blocked**: tickers they never want to trade. **exchange** stays NSE and **product** stays MIS.
- Keep **paper** true until they have watched the pack work on simulated fills.

## Step 2: size the trade

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py" --write
```

Whole shares only: NSE equities trade one share at a time. The size is the
smallest of four limits:

| limit | shares |
| --- | --- |
| max loss | `max_loss_per_trade_inr` / risk a share (entry minus stop) |
| position cap | `budget_inr` x `max_position_pct` / entry |
| max order | `max_order_inr` / entry |
| cash | (budget minus the cash buffer) / entry, never more than the cash in `trader/portfolio.json` or your live Kite balance; times `mis_leverage_x` with `use_mis_leverage` on |

The entry it sizes at: the limit price with `--limit`. For a market order,
the plan's entry, or the price in `trader/prices.json` if it is fresh (under
15 minutes old) and higher. A price that ran up since the plan means a
bigger loss at the same stop, so fewer shares. A lower price never shrinks
the risk. The stop and the target round down to the stock's tick (0.10 for
RELIANCE, 0.05 for most stocks, 0.01 under Rs 250), never up.

It refuses, and writes no order, when: the size is zero shares, the order is
under the minimum, reward:risk is under `min_reward_risk`, the stop is not
below the entry, the stop is more than 10 percent under the entry (Zerodha's
cover-order range), the target is not above the entry (the move already
happened), the plan is for another day, the ticker is blocked, or the plan
is a short. Say the reason to the user in one line.

## Step 3: show the user

Show the four limits, which one decided the size, the shares, the rupees
and the max loss at the stop. Say plainly that a stop is an order, not a
promise: in a fast market it can fill below the stop. Then hand over to
`/trader-risk`.

## How Claude runs it

1. Make sure `trader/plan.json` is today's (trader-exit). If not, stop and
   say so: the tool refuses a plan for another day.
2. If the price may have moved since the plan, refresh it first
   (trader-alerts: `alerts.py quote RELIANCE --write`).
3. Run `size.py --write`. Exit code 0 means an order was written, 1 means a
   refusal or a missing file, with one line why on stderr.
4. Read the report back to the user in plain words: the four limits, the one
   that binds, the shares, the cost, the max loss at the stop, and the Kite
   body it wrote. Never change a limit yourself; if the user wants a bigger
   size, the answer is a closer stop (trader-exit) or a change they make to
   their own config.
5. Hand over to `/trader-risk`. Nothing has been placed.

## Example: the reel

RELIANCE on Thursday 2026-10-29, real Kite candles. Opening range 09:15 to
09:30: 1155.60 to 1170.50. The 09:35 bar closed at 1174.40, the first close
above the range, so the entry is 1174.40 at 09:40. Stop at the range's
midpoint, 1163.05 rounded down to the 0.10 tick: 1163.00. Risk 11.40 a
share. Target 1R: 1185.80. The example config: Rs 1,00,000 budget, 10% cash
buffer, Rs 500 max loss.

| limit | the math | shares |
| --- | --- | --- |
| max loss | Rs 500 / 11.40 | **43 (decides it)** |
| position cap | 75% of Rs 100,000 = Rs 75,000 / 1174.40 | 63 |
| max order | Rs 75,000 / 1174.40 | 63 |
| cash | Rs 100,000 minus Rs 10,000 = Rs 90,000 / 1174.40 | 76 |

43 shares = Rs 50,499.20. Max loss at the stop: 43 x 11.40 = Rs 490.20. It writes:

```json
{"symbol": "RELIANCE", "exchange": "NSE", "side": "buy", "qty": 43, "product": "MIS", "order_style": "co",
 "kite": {"variety": "co", "exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "BUY",
          "order_type": "MARKET", "quantity": 43, "product": "MIS", "trigger_price": 1163.0,
          "validity": "DAY", "tag": "dtsorb"},
 "stop_leg": null, "stop": 1163.0, "target": 1185.8,
 "why": "opening-range breakout on RELIANCE: 43 shares, bound by your Rs 500 max loss per trade (43 x 11.40 = Rs 490.20 at the stop)",
 "plan": {"entry": 1174.4, "stop": 1163.0, "target": 1185.8, "risk_per_share": 11.4, "max_loss_inr": 490.2,
          "setup": "orb", "entry_time": "09:40", "flat_by": "15:05"}}
```

`kite` is the body of Kite's place_order call, nothing more. `why` and
`plan` are notes for trader-risk and trader-broker; they are never sent to
Zerodha. The target is not an order: trader-alerts rings at it. With
`"order_style": "regular"` the body is `variety: regular` without a trigger
and `stop_leg` is a second body: SELL, SL-M, MIS, trigger 1163.00, 43 shares.
With a Rs 10 max loss instead, one share would risk 11.40: no order. At
09:55 with the 09:52 price of 1178.00 in `trader/prices.json`, the same plan
sizes to 33 shares at 15.00 a share, and reward:risk falls to 0.52: refused,
because the move it was waiting for has mostly happened.

## Rules

- Proposals only. This skill never calls a broker tool.
- Never size money the user has not said is for this. Ask for the budget and the max loss.
- Every number comes from the config, the plan, the prices or Kite. Nothing is guessed.
- No projections: never say what a trade will make. A target is a plan, not a forecast.
- Kite's price can lag by a moment; the price that fills is Zerodha's.

## What it never does

It never places, approves or schedules an order, never raises a limit on its
own, never shrinks a stop to fit more shares, and never sizes futures,
options, BSE, short or CNC positions.

## Fine print

- Education and tooling, not financial advice. A size is arithmetic on the user's own rules, not a
  recommendation.
- Paper by default (`"paper": true`): trader-broker records simulated fills and never calls Zerodha.
  Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold:
  trader-risk passed that exact order today, the user typed an explicit yes for that one order in
  the chat, and the user clicked Allow once on Claude's own card because the Kite MCP order tool is
  set to "Needs approval". Never set an order tool to "Always allow".
- No futures, options, BSE, short selling or MIS leverage unless the config allows it and the user
  confirms.
- Kite keys live in `.env` or the environment, never in the chat.
