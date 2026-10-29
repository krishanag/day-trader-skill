# The Day Trader skill, India edition

13 Claude skills for day trading NSE stocks through Zerodha: scan every chart, track the news, time every entry and exit, replay every outcome before you risk a rupee, and run the whole loop unattended inside hard limits.

Everything reads Zerodha Kite Connect: the candles, the quotes, the
instrument list, your balance. Nothing else is scraped. The tools are plain
Python, standard library only, and every one of them runs offline in the
tests on data cut from real Kite candles.

One scans the NSE for setups. Another tracks the news and the day's exchange
filings on every stock you watch. One watches a setup bar by bar and tells
you the moment to get in, and another sets your stop and your target before
you do. The Time Machine replays the rest of the session 10,000 times from
the stock's own recent bars, plus every past day the same setup fired, and
tells you the honest odds. The Budget Manager sizes the trade to your max
loss, the Risk Manager checks it against your rules, and only then does the
Order Executor show you a ticket.

And no trade goes through until you say yes. Nothing in this pack can place
an order on its own. Paper trading is the default, and Zerodha squares off
every intraday position by the close anyway.

> **This is a port.** The original Day Trader skill by Jake Schincariol
> trades US stocks through Yahoo data and Alpaca:
> [github.com/Jakeschincariol/day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill).
> This edition keeps its method and its safety rules and moves everything
> to the NSE, IST, rupees and Zerodha. MIT, like the original.

## Install

Paste this into Claude:

```
https://github.com/krishanag/day-trader-skill

install skill
```

Or as a plugin, in Claude Code:

```
/plugin marketplace add krishanag/day-trader-skill
/plugin install day-trader-skill@day-trader-skill
```

Claude Code namespaces plugin skills, so installed as a plugin they show up
as `/day-trader-skill:trader-scanner` and so on. Copy the folders instead if
you want plain `/trader-scanner`:

```bash
git clone https://github.com/krishanag/day-trader-skill.git
cp -r day-trader-skill/trader-* ~/.claude/skills/
```

Copy all thirteen: the twelve tools import the Kite client and the NSE clock
from `trader-kite/`, which must sit beside them. Project-local instead of
global: copy the same folders into your repo's `.claude/skills/`.

## Zerodha, once

1. A Zerodha account and a Kite Connect app from
   [developers.kite.trade](https://developers.kite.trade). The **Connect**
   plan (Rs 500 a month per app) is what gives historical candles and
   quotes; the free Personal plan only places orders, and this pack reads
   data all day. Set the app's redirect URL to `http://127.0.0.1:5000/callback`.
2. In your project folder, a `.env` with `KITE_API_KEY`, `KITE_API_SECRET`
   and `KITE_REDIRECT_URL`. `chmod 600 .env`. The pack's `.gitignore` keeps
   `.env` and `trader/` out of git. Never paste a key into the chat.
3. `python3 trader-kite/login.py`. Your browser opens Zerodha's own login
   page; you sign in there, with your TOTP, never in the chat. The script
   catches the redirect, swaps the one-time token for a session and saves
   it to `trader/kite_session.json`, mode 600.
4. Copy `trader-budget/config.example.json` to `trader/config.json` and set
   your budget and your max loss per trade. The defaults are a Rs 1,00,000
   budget, Rs 500 max loss a trade, Rs 1,500 a day, three trades a day,
   entries between 09:30 and 14:30, flat by 15:05, paper on.

Kite sessions die at 06:00 IST, so the first thing each trading day is
`/trader-kite` (or `python3 trader-kite/login.py` again).

## The thirteen

| command | job | what it does |
| --- | --- | --- |
| `/trader-kite` | Zerodha Login | Signs you in to Kite Connect once a day and keeps the session every other skill reads through. |
| `/trader-scanner` | Chart Scanner | Builds the universe of liquid NSE stocks and scans it for setups: opening-range breakouts, 20-day breakouts, flags, oversold bounces. |
| `/trader-news` | News Monitor | Headlines per stock (Google News India, Economic Times, Mint, Moneycontrol, SEBI) and the day's BSE filings for your companies, tagged, new since the last check. |
| `/trader-entry` | Entry Timer | Watches a setup bar by bar on Kite 5-minute or 1-minute candles and calls the entry on the first close above the range. |
| `/trader-exit` | Exit Planner | Sets your stop and your target before you enter, snapped to the stock's tick, inside Zerodha's cover-order range. |
| `/trader-timemachine` | Time Machine | 10,000 replays of the rest of the session from the stock's own last 60 sessions, plus the same rule on every one of those sessions. |
| `/trader-budget` | Budget Manager | Sizes the trade to your max loss, position cap, max order and cash, and writes one Kite cover order. |
| `/trader-risk` | Risk Manager | Checks every order against your limits, the clock and the market, and records PASS or BLOCK. |
| `/trader-broker` | Order Executor | Shows the ticket in plain words, records your yes, and hands the exact Kite body to Zerodha's MCP server. Paper by default. |
| `/trader-alerts` | Price Alerts | Rings once at the stop, the target, the flat-by, the bell, a price or a time; knows the NSE clock and holidays. |
| `/trader-insiders` | Insider Tracker | Reads NSE's insider-trading, bulk-deal and block-deal CSVs you download, and today's SAST and pledge filings on BSE. |
| `/trader-results` | Results Analyst | Reads the Ind AS XBRL results you download and sums up what changed; finds the next results date in the news. |
| `/trader-auto` | Auto Trader | Runs the whole chain unattended every minute of the session inside hard limits (max loss a trade, daily cap, one position, flat by 15:05, a kill switch), places the cover orders itself, and writes the day's report for review. The one skill that trades without a per-order yes, and only when you turn it on. |

## How to use it

Everything lands in a `trader/` folder in your project, and each skill reads
what the last one wrote:

```
kite login -> scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> broker   (one trade)
alerts: stop, target, flat by 15:05                                                       (while it is open)
news / insiders / results                                                                 (on a schedule)
```

An example, real data: Reliance Industries, Thursday October 29 2026. Every
number below came out of the tools in one run, in paper mode.

1. **`/trader-scanner`** at 10:15 IST: of six charts in the test scan, five
   had closed above their 15-minute opening range. RELIANCE is one: range
   Rs 1,155.60 to 1,170.50, the 09:35 candle closed at Rs 1,174.40, 0.33 %
   over the top. (A whole-universe scan is 951 liquid stocks; `universe`
   takes 5 seconds, `daily` about 6 minutes at Kite's 3 calls a second.)
2. **`/trader-news`**: RELIANCE's headlines in IST, and the company's own
   BSE filing that morning (a UK subsidiary being dissolved, 08:55). The
   Results Analyst's calendar says RELIANCE reports Q2 tomorrow, October 30:
   a day trade is flat by the close, so it is not exposed to it.
3. **`/trader-entry`** watched RELIANCE bar by bar. At 09:40 it said
   triggered: the 09:35 bar (bar 5) closed above the range high at
   Rs 1,174.40. It also said this is a Closing Auction Session stock, so
   continuous trading ends at 15:15 and Zerodha squares off MIS at 15:12.
4. **`/trader-exit`** set the plan before the entry: stop at the range
   midpoint, 1,163.05 rounded down to RELIANCE's Rs 0.10 tick, so
   **Rs 1,163.00**, Rs 11.40 a share at risk; target one times that risk,
   **Rs 1,185.80**; flat by 15:05 either way.
5. **`/trader-timemachine`** replayed the rest of the day 10,000 times, each
   5-minute candle drawn from RELIANCE's own candles at that time of day
   over the previous 60 sessions (seed 19, so anyone can repeat it): target
   first 22.9 %, stop first 28.0 %, still open at 15:05 49.0 %. It also
   re-ran the same rule on those 60 real sessions: it fired 27 times, 13
   hit the target, 11 the stop, 3 neither. Its verdict: leans toward the
   stop. That is the point of looking before you risk a rupee.
6. **`/trader-budget`** with a Rs 1,00,000 budget and a Rs 500 max loss per
   trade: 500 / 11.40 = **43 shares** (Rs 50,499.20). If the stop fills at
   1,163.00, the loss is Rs 490.20. It writes one cover order: buy 43
   RELIANCE, MIS, market, stop-loss trigger 1,163.00. The target is not an
   order; the alerts watch it.
7. **`/trader-risk`** checks it against your limits: the max loss, the daily
   loss limit, trades per day, reward to risk, the entry window, the
   cover-order range, the tick, position size and cash. PASS, with the
   numbers.
8. **`/trader-broker`** shows you the ticket in plain words with the exact
   Kite `place_order` body. You type yes for that exact order. In paper mode
   it records a simulated fill at Kite's last price into a local ledger.
   Live, Claude calls your self-hosted Kite MCP server, and Claude's own
   "Allow once / Deny" card asks you one more time.
9. **`/trader-alerts`** rings at the stop, the target and at 15:05 to
   flatten whatever is still open, before Zerodha's own 15:12 square-off
   and its Rs 50 + GST charge. `ticket.py exit RELIANCE` writes the exit
   ticket; the risk check never blocks an exit.

## The tools

All standard-library Python, 3.8 or newer. Run them from your project
folder: `trader/`, `.env` and the Kite session are relative to it.

```bash
python3 trader-kite/login.py                                            # once a day, before 09:15
python3 trader-scanner/scan.py universe                                 # liquid NSE stocks -> trader/universe.txt
python3 trader-scanner/scan.py daily                                    # pre-market setups -> trader/watchlist.json
python3 trader-scanner/scan.py orb --at 10:15                           # opening-range breakouts so far
python3 trader-news/news.py --tickers RELIANCE --hours 12               # headlines, BSE filings, today's move
python3 trader-entry/entry.py check RELIANCE                            # waiting / triggered / expired
python3 trader-entry/entry.py watch RELIANCE --every 30                 # bar by bar until it triggers
python3 trader-exit/exit.py plan --stop or-mid --target 1R --write      # -> trader/plan.json
python3 trader-timemachine/timemachine.py run --write                   # 10,000 replays + the history
python3 trader-budget/size.py --write                                   # -> trader/orders.json
python3 trader-risk/check.py                                            # PASS / BLOCK per order
python3 trader-broker/ticket.py show                                    # the tickets, READY or not
python3 trader-alerts/alerts.py add --from-plan                         # stop, target, flat by 15:05
python3 trader-alerts/alerts.py check                                   # rings what hit, once
python3 trader-insiders/insiders.py today                               # today's SAST / pledge filings on BSE
python3 trader-insiders/insiders.py index --file insider-trading.csv    # NSE's CSV, downloaded by you
python3 trader-results/results.py facts RELIANCE --file results.xml     # the XBRL, downloaded by you
python3 trader-results/results.py calendar RELIANCE                     # does it report this week
python3 trader-auto/auto.py probe SBIN --qty 1 --dry-run                # the one-share live test, bodies only
python3 trader-auto/auto.py tick --dry-run                              # one pass of the unattended loop, nothing sent
bash trader-auto/run-day.sh                                             # the loop, every 60 s until 15:20
python3 trader-auto/auto.py status                                      # phase, position, P&L so far
python3 trader-auto/auto.py report                                      # the day's trades, charges, slippage, skips
python3 trader-auto/auto.py stop                                        # kill switch: no entries, exit what is open
```

```bash
python3 -m unittest discover -s tests -v
```

The tests run offline, on fixtures cut from real Kite data: RELIANCE's
5-minute bars for the 61 sessions to October 29 2026, the 10:15 scan,
HINDUNILVR's breakout the same morning, real quotes, real instrument rows,
real RSS and BSE feeds. They reproduce every number in the example above.
446 tests.

## The unattended loop, for real money

`/trader-auto` is the one skill that places orders without a per-order
yes. It runs the same chain every minute of the session (scanner, results
and news checks, entry, exit plan, Time Machine gate, budget, risk) and
places the cover order itself through Kite, inside limits you set once in
`config.auto.example.json`: max loss a trade, a daily cap that counts
charges, one open position, trades a day, flat by 15:05, and a kill-switch
file (`auto.py stop`). It runs only when you have set `auto.enabled` and
`paper` yourself. Every decision, refusal and skip is written with its
numbers, and `auto.py report` is the day's record to review. Run
`auto.py probe SYM --qty 1` once, in market hours, before the first loop:
it places and exits one share so the real path is proven on your account.

## Zerodha's MCP server, for the manual flow

`/trader-broker` never places an order itself. When `paper` is false, Claude
calls Zerodha's official
[kite-mcp-server](https://github.com/zerodha/kite-mcp-server), run on your
own machine with your own API key (the free Personal plan is enough for
orders), because the hosted mcp.kite.trade leaves out some trading
operations. The broker skill's SKILL.md walks through the setup for Claude
Desktop and Claude Code and the example config ships with placeholders. Set
the order tools to "Needs approval", never "Always allow".

Zerodha's stock server has two gaps the pack needed closed: it cannot open
a session in stdio mode, and its `cancel_order` drops `parent_order_id`,
which the cover-order exit requires. A patched build lives beside the repo
(`../kite-mcp-server`, see its `PATCH.md`): it fixes both and reads the
pack's own session file, so one morning login serves the tools and the MCP.
Exiting a cover order is proven on a real account by `auto.py probe`, not
by the MCP; if the MCP call is ever rejected, exit from the Kite app's
order book and record the fill with `ticket.py result`.

## Fine print

**Not financial advice.** This is software that reads market data and does
arithmetic. It does not know your situation and it cannot see the future.
Most people who day trade lose money. You can lose money. Decide for
yourself, and talk to a SEBI-registered adviser if you need advice.

**No trade goes through until you say yes.** Only `/trader-broker` can lead
to an order, and only after `/trader-risk` passes that exact order and you
type yes for it in the chat. A yes covers one ticket, never a batch. With
the Kite MCP tool set to "Needs approval", Claude's own "Allow once / Deny"
card is a second check. No F&O, no shorts, no BSE unless you turn them on.

**Paper first.** `"paper": true` is the default. Zerodha has no paper
account, so paper mode fills at Kite's live last price into a local ledger
and never calls the broker. Brokerage, STT and GST are not modelled.

**Intraday only.** Every order is MIS. Stocks with F&O contracts stop
continuous trading at 15:15 for the closing auction and Zerodha squares off
their MIS positions at 15:12 (Rs 50 + GST each time); other stocks trade to
15:30. The pack's flat-by is 15:05 for everything.

**A cover order's stop is a plan, not a guarantee.** The stop leg sits at
the exchange from the moment you are in, which is the point of using one,
but in a fast market or across a circuit it can fill well past its price.
The max loss shown is the loss at the stop price.

**The Time Machine is a simulation, not a prediction.** It is built from the
stock's own last 60 sessions of 5-minute bars. The future can do things
those sessions never did: a result, a circuit, a headline.

**NSE and BSE are read the honest way.** The exchanges block scripted
access to nseindia.com and their terms forbid scraping, so no tool in this
pack impersonates a browser or performs a cookie handshake. The news, the
insider filings and the results calendar come from public RSS feeds (Google
News India, the Economic Times, Mint, Moneycontrol, SEBI, BSE's own
announcements feed). Insider CSVs and XBRL results you download yourself
from the exchange's page and hand to the tool with `--file`. No real XBRL
result file can be fetched by a script, so the results tests run on labelled
sample filings in the real taxonomy; the first real file you feed it is its
first real test.

**Kite Connect is the only data source.** It is Zerodha's official API and
its data is Kite's, but it is rate limited (quotes once a second, candles
three a second), the session dies at 06:00 IST, and one app allows one
session at a time: logging in here ends any other program's session on the
same key. The price that fills is your broker's; check anything that
matters in Kite before you act on it.

**Holidays.** NSE's 2026 equity holiday list is built in and was checked
against Kite's candles. NSE publishes each year's list in December; until
2027's is added, the clock warns instead of guessing.

**Taxes.** Intraday equity gains are speculative business income in India,
with STT on both legs. Nothing here is tax advice.

## Files

```
trader-kite/        SKILL.md, kite.py, nse.py, login.py, FILES.md (the contract every skill follows)
trader-scanner/     SKILL.md, scan.py
trader-news/        SKILL.md, news.py
trader-entry/       SKILL.md, entry.py
trader-exit/        SKILL.md, exit.py
trader-timemachine/ SKILL.md, timemachine.py
trader-budget/      SKILL.md, size.py, config.example.json
trader-risk/        SKILL.md, check.py
trader-broker/      SKILL.md, ticket.py, claude_desktop_config.example.json
trader-alerts/      SKILL.md, alerts.py, alerts.example.json
trader-insiders/    SKILL.md, insiders.py
trader-results/     SKILL.md, results.py, metrics.example.json
trader-auto/        SKILL.md, auto.py, config.auto.example.json, run-day.sh, com.day-trader.auto.plist.example
tests/              unittest suite + offline fixtures cut from real Kite data
```

MIT. The original Day Trader skill is by
[Jake Schincariol](https://github.com/Jakeschincariol); this India edition
is by [Krishan Agarwal](https://github.com/krishanag).
