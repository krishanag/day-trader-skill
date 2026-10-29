---
name: trader-auto
description: >-
  Auto Trader. Runs the pack's whole day-trade chain unattended on a real
  Zerodha account, one tick a minute from a scheduler, inside hard limits the
  user sets once: scans the NSE for opening-range breakouts through
  trader-scanner, skips a stock with a results or board-meeting filing today, a
  results date today or tomorrow, a halt or circuit headline, a price the cash
  cannot buy one share of, a blocked ticker or one already traded today, then
  runs trader-entry (a trigger not older than two bars), trader-exit (stop at
  the opening-range midpoint, target 1R), trader-timemachine (an edge gate),
  trader-budget and trader-risk, and only then places the ONE Kite cover order
  itself through trader-kite's order calls, holds it against the target, the
  stop leg at the exchange, the flat-by time and a kill-switch file, exits it,
  writes the broker's ledger and a day report with P&L, charges, slippage and
  every skip and refusal. Max loss a trade, a daily loss cap counting charges,
  one position at a time, max trades a day, MIS leverage up to the configured
  multiple, flat by 15:05, never a sell except its own exit, never outside
  09:15 to 15:15. Ships a one-share live probe, a run-day loop and a launchd
  plist. Use when the user says "run the auto trader", "start the loop",
  "trade it for me today", "what did the bot do", "auto trader report", "stop
  the auto trader", "kill switch", "probe one share", "set up the scheduler",
  or asks why the loop skipped or took a trade.
argument-hint: "[tick | status | report [--date YYYY-MM-DD] | probe SYMBOL | stop | go]"
---

# trader-auto

The Auto Trader is the one skill in this pack that places an order without
a per-order yes. The hard limits in `trader/config.json` replace that yes,
and the runner only runs when the user has set `"auto": {"enabled": true}`
and `"paper": false` in that file themselves. Everything it decides is a
decision the tested skills already make; it adds the orchestration, the
guards, the order calls, the monitoring and the report. Most people who day
trade lose money. An unattended loop loses it faster when its limits are
wrong, so the limits are small, and the report exists so the pack can be
fixed from the evidence.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).
Run it from the user's project folder: `trader/`, `.env` and today's Kite
session are relative to it.

```bash
python3 "${CLAUDE_SKILL_DIR}/auto.py" tick                                   # one pass; the scheduler runs this every minute
python3 "${CLAUDE_SKILL_DIR}/auto.py" tick --dry-run                         # the whole chain, never an order call
python3 "${CLAUDE_SKILL_DIR}/auto.py" tick --now "2026-10-29 10:16" --dry-run  # replay a moment
python3 "${CLAUDE_SKILL_DIR}/auto.py" status                                 # today's state in plain words
python3 "${CLAUDE_SKILL_DIR}/auto.py" report                                 # the day's trades, P&L, charges, slippage, skips
python3 "${CLAUDE_SKILL_DIR}/auto.py" report --date 2026-10-29 --json
python3 "${CLAUDE_SKILL_DIR}/auto.py" probe RELIANCE --dry-run               # the one-share test: print the bodies, send nothing
python3 "${CLAUDE_SKILL_DIR}/auto.py" probe RELIANCE --i-understand-this-is-real   # the real one-share round trip
python3 "${CLAUDE_SKILL_DIR}/auto.py" stop                                   # the kill switch: trader/auto/STOP
python3 "${CLAUDE_SKILL_DIR}/auto.py" go                                     # remove it
bash "${CLAUDE_SKILL_DIR}/run-day.sh"                                        # the loop, 09:10 to 15:20 IST
```

Flags: `--now "YYYY-MM-DD HH:MM"` (IST) pins the clock on every command and
is passed to every sub-tool, `--config PATH` (default `trader/config.json`),
`--json` on `status` and `report`, `--qty` (1 to 5) and `--stop-pct` (0.2 to
9.0) on `probe`. Exit codes: 0 did its pass, 1 cannot run (no session, the
market is closed, `auto.enabled` is not true, a probe without the flag) with
one plain line why.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/config.json` | the limits (start from `config.auto.example.json` here: Rs 10,000 budget, Rs 200 max loss a trade, Rs 500 a day, 3 trades, 3x MIS leverage, flat by 15:05, `paper` false) and the `auto` block: `enabled`, `target` (1R), `stop` (or-mid), `min_edge` (0.0), `max_candidates` (8), `max_confirm` (60), `tick_seconds` (60), `kill_file` |
| `trader/kite_session.json`, `.env` | today's Kite session through `trader-kite/kite.py`; the order calls `place_order`, `wait_for_status`, `cover_order_legs`, `exit_cover_order`, `order_history`, `positions`, `margins`, `quotes` and `estimate_charges` |
| `trader/auto/<date>.json` | the day's state: `phase`, `candidates`, `skipped` (symbol: reason, first and last time, times), `position` (symbol, qty, entry price, planned entry, stop, target, parent and stop-leg order ids, the time machine's odds), `trades`, `realised_inr`, `charges_inr`, `decisions` (every decision with its reason and numbers), `summary` |
| `trader/auto/STOP` | the kill switch; present means no new entry and any open position is exited on the next tick |
| `trader/auto/journal.jsonl` | one line per day once the summary is written: trades, gross, charges, net, symbols, exit reasons |
| `trader/auto/<date>.log`, `probes.jsonl` | the loop's output (run-day.sh), the probe's record |
| `trader/watchlist.json`, `trader/universe.txt` | the scanner runs `orb` over today's daily watchlist when one exists, else over the universe with stage 2 capped at `max_confirm` |
| `trader/entry.json`, `plan.json`, `timemachine.json`, `orders.json`, `verdicts.json`, `alerts.json` | written by the sub-tools it runs (`--write`), exactly as the manual chain writes them; a `--dry-run` tick writes these too |
| `trader/portfolio.json`, `trader/fills.json`, `trader/log.jsonl` | the broker's ledger in trader-broker's shapes, with `reply: "auto"` and the limits on the approval line, so `ticket.py positions` and trader-risk stay truthful |

## The tick

Every tick reads the NSE clock from `trader-kite/nse.py`, the state file and
the kill switch, then:

1. **Before 09:15.** Checks the Kite session and reads the live balance
   (`margins`). No session: writes `login-needed` and exits 1. A balance
   under `min_order_inr` on a live account: no entries today.
2. **09:15 to 09:35.** Waits: the 15-minute range forms to 09:30 and its
   first bar closes at 09:35.
3. **09:35 to `no_entries_after` (14:30), nothing open.** The hunt:
   `scan.py orb --json --no-write`, hits in the scanner's order, at most
   `max_candidates` a tick. A hit is skipped, with the reason written, when
   it is on `blocked`, already traded today, its breakout bar is more than
   two bars old, or one share costs more than the cash after the buffer times
   the leverage. The first survivor goes through `entry.py check --write`
   (must be `triggered`, not more than two bars ago), `results.py calendar`
   (skip on a results or board-meeting filing today, or a results date today
   or tomorrow), `news.py` (skip on an item tagged halt), `exit.py plan
   --stop or-mid --target 1R --write`, `timemachine.py run --write` (skip
   when target-first minus stop-first is under `min_edge`), `size.py
   --write`, `check.py` (must PASS), then the guards below. A skip is final
   for that symbol for the day (an opening-range breakout triggers once); a
   tool that failed to run is retried next tick. If it all holds, the order
   is placed: `k.place_order(order["kite"])`, `wait_for_status` up to 30 s,
   the fill price and quantity, the stop leg's id from `cover_order_legs`,
   the ledger, then `alerts.py add --from-plan`. A fill still open after
   30 s is "pending" and resolved from `order_history` on the next tick.
4. **Holding.** One quote a tick. The stop leg's own `order_history` says
   COMPLETE: recorded as a stop exit at its fill price. Last at or above the
   target, the clock at or past `flat_by`, or the kill switch present:
   `exit_cover_order(parent)` (Kite cancels the stop leg and sells at
   market), wait, record the fill, the realised P&L and
   `estimate_charges`. The leg CANCELLED with the position flat in
   `positions()` is Zerodha's own square-off and is recorded as such.
5. **After an exit.** One position at a time, so hunting resumes only if
   trades today are under `max_trades_per_day` and realised P&L minus
   charges is above minus `max_daily_loss_inr`; otherwise the phase is done.
6. **15:05 onward** exits anything open (rule 4). **After 15:15** the day
   summary is written once and one line goes to the journal.

## The guards (enforced here, as well as by trader-risk)

- Max loss per trade: the order's max loss at the stop must fit
  `max_loss_per_trade_inr`.
- Daily cap: net so far (realised minus charges) must be above minus
  `max_daily_loss_inr`, and net so far minus this order's max loss must not
  breach it either.
- One open position; at most `max_trades_per_day`; one trade per symbol a day.
- Never a SELL except the exit of what it holds; only an MIS cover order
  with a stop trigger; never an order call outside 09:15 to 15:15 IST.
- Live account: the balance from `margins` must cover the order's cash need
  (quantity x entry / leverage) at the moment of the order.
- No Kite session, a closed day, `auto.enabled` not true, or the kill switch:
  no entry. Every refusal goes to the decisions log with its numbers.

## The probe (the one-share live test)

`probe SYMBOL` places a 1-share MIS cover order at market with the stop
trigger `--stop-pct` (1.0) under the last price, waits for the fill, prints
the parent and the second leg from `cover_order_legs`, holds 10 s, exits
with `exit_cover_order`, waits, prints both fills, the realised P&L, the
charge estimate, and confirms `positions()` shows no net quantity. It prints
every Kite body before sending and sends nothing without the literal
argument `--i-understand-this-is-real`; `--dry-run` prints the bodies and
stops. Run it once, during market hours, before the first scheduled day.

## The scheduler

`run-day.sh` loops `auto.py tick` every `tick_seconds` until 15:20 IST or
until the state says done, logging to `trader/auto/<date>.log`.
`com.day-trader.auto.plist.example` starts it at 09:10 IST Monday to Friday
through launchd; copy it with the paths filled in. The Kite login
(`python3 trader-kite/login.py`) is still a human step each morning before
09:10, because it needs the user's password and TOTP on Zerodha's page.
Without it every tick logs `login-needed` and does nothing.

## How Claude runs it

1. Claude's job is the review. Run `auto.py report` (and `status` during
   the day) and read it back plainly: each trade's entry and exit, why it
   exited, gross, charges, net, slippage against the plan, the odds at entry,
   and every skip and refusal with its reason. Say what the evidence
   suggests should change in the pack or the limits; never change a limit.
2. Never run `tick` by hand during market hours while the scheduler is
   running: two runners would read and write the same state. `tick --dry-run
   --now ...` outside market hours is the way to replay a moment.
3. To halt it, `auto.py stop`: no new entries, the open position is exited
   on the next tick. `auto.py go` lets it enter again. Never remove the kill
   file for the user without being asked.
4. Setup, once: copy `config.auto.example.json` to `trader/config.json`
   and have the user confirm every limit; `probe SYMBOL --dry-run`, then
   the user runs the real probe themselves; fill in the plist paths.
5. If the report shows `login-needed`, the morning login was missed; if it
   shows the same skip every day, say so: that is the pack telling on itself.

## Example: the reel on the example limits

RELIANCE, Thursday 2026-10-29, the pack's own numbers: the 09:35 bar closed
above the 15-minute range at Rs 1,174.40, stop Rs 1,163.00 (the range
midpoint on the 0.10 tick), target 1R at Rs 1,185.80, risk Rs 11.40 a
share. Rs 200 max loss buys 17 shares (Rs 19,964.80, inside 3x the Rs 9,000
cash after the buffer). The time machine on that plan said target first
22.9 %, stop first 28.0 %: an edge of -5.1 points, under `min_edge` 0.0, so
the runner skips it and tries the next hit; the test fixture that enters
uses 31 % against 27 %. With a fill at Rs 1,174.60 and a target exit at
Rs 1,185.90 the report reads: gross Rs 192.10, charges Rs 21.35 on
Zerodha's listed MIS rates (`kite.estimate_charges`: brokerage 12.04, STT
5.04, exchange 1.23, SEBI 0.04, stamp 0.60, GST 2.40), net Rs 170.75,
slippage +0.20 a share on the entry and -0.10 on the exit.

## What it never does

It never widens a limit, never places a second order while one is open,
never sells anything it does not hold, never runs before 09:15 or after
15:15, never places anything with `auto.enabled` false, with the kill switch
present or without today's session, and never sends an order from `--dry-run`
or from a probe without the literal flag.

## Fine print

- Not financial advice. This is software that runs the pack's own rules on
  the user's own limits; it cannot see the future, and a stop at the
  exchange can fill worse than its price. Most people who day trade lose
  money, and an unattended loop can lose the whole daily cap on a bad day.
- **This skill DOES place orders without a per-order yes.** The hard limits
  in `trader/config.json` (max loss a trade, daily cap, one position, max
  trades, the entry window, flat by 15:05, the kill switch) replace that
  yes, and it only runs when the user has set `"auto": {"enabled": true}`
  and `"paper": false` themselves. Claude never sets either. Every other
  skill still places nothing until the user says yes to that exact ticket.
- Kite keys live in `.env` or the environment, never in the chat or the
  logs. The session is the user's own login each morning.
