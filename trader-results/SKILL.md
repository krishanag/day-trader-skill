---
name: trader-results
description: >-
  Results Analyst. Reads the quarterly results an NSE or BSE company filed
  (the Ind AS XBRL financial-results file you download from the exchange) and
  sums up what changed: revenue from operations, total income, EBITDA
  (derived), PBT, PAT, margins, basic and diluted EPS, quarter by quarter,
  each against the previous quarter and the same quarter a year earlier, with
  the trailing twelve months, in rupees crore. Sums up a pasted results
  commentary or earnings-call transcript. Says whether the next result falls
  inside a day trade's horizon, from Google News headlines and BSE's own
  announcements feed. Use when the user says "how were Reliance's results",
  "analyse the Q2 numbers", "what changed this quarter", "is revenue
  growing", "read the results filing", "compare margins", "sum up the call",
  "when are the results", "does it report this week", or before trading a
  stock around its results.
argument-hint: "[facts SYMBOL --file results.xml | summary --paste | calendar SYMBOL [--date YYYY-MM-DD]]"
---

# trader-results

Results move an Indian stock more than anything else on its calendar, and
the press release is written to put the best number first. This skill reads
the numbers the company filed with the exchange, lines them up quarter by
quarter and says plainly what changed. For a day trader it answers two
questions before a trade: what did the last result say, and is the next one
inside this trade?

Tool in this folder (standard library Python, no keys beyond the Kite
session the pack already has):

```bash
python3 "${CLAUDE_SKILL_DIR}/results.py" --help
python3 "${CLAUDE_SKILL_DIR}/results.py" facts RELIANCE --file ~/Downloads/results.xml
python3 "${CLAUDE_SKILL_DIR}/results.py" facts RELIANCE --file q2.xml --file q1.xml --quarters 8 --write
python3 "${CLAUDE_SKILL_DIR}/results.py" summary --paste < transcript.txt
python3 "${CLAUDE_SKILL_DIR}/results.py" calendar RELIANCE
python3 "${CLAUDE_SKILL_DIR}/results.py" calendar RELIANCE --date 2026-10-30
```

Run it from the user's project folder: `trader/` and `.env` are relative to
it. `--json` prints everything machine-readable, `--now "2026-10-29 10:15"`
pins the clock (IST), `--offline DIR` reads saved feeds (the tests use it).

## Where the filing comes from

NSE and BSE do not allow scripted access to their filing pages, and this
pack does not pretend to be a browser to get round that. The user downloads
the file in their own browser and hands the tool the path:

- NSE: nseindia.com, Corporates, Financial Results, find the company, the
  row's XBRL link. Save the `.xml`.
- BSE: the result announcement on bseindia.com, its XBRL attachment.

One file carries this quarter, the previous quarter, the same quarter a year
earlier, both year-to-date columns and the last full year, so one download
gives the table and a trailing twelve months. Pass several filings with
repeated `--file` for a longer table; a later filing's numbers win where
they overlap (restatements). The exchange's result PDF is for context only;
the numbers come from the XBRL.

## What it reads and writes

| file or feed | what |
| --- | --- |
| `--file PATH` (one or more) | Ind AS XBRL financial-results instance files the user downloaded from NSE or BSE. Elements are matched by local name (`RevenueFromOperations`, `Income`, `FinanceCosts`, `DepreciationDepletionAndAmortisationExpense`, `ProfitBeforeTax`, `ProfitLossForPeriod`, the EPS lines); contexts give the periods and the consolidated / standalone dimension; `LevelOfRoundingUsedInFinancialStatements` gives lakh / crore / actual |
| stdin (`summary --paste`) or `--file` | the results commentary or call transcript the user pasted |
| Google News RSS (`news.google.com/rss/search`, `hl=en-IN`) | headlines naming the results date, for `calendar` |
| BSE announcements RSS (`bseindia.com/data/xml/announcements.xml`) | the exchange's own feed of the current day's announcements: board-meeting intimations ("scheduled on 06/11/2026 ... to consider ... Financial Results") and results filed today |
| Kite instrument list (`trader/cache/`) | the company name for the symbol; `facts` falls back to the name inside the filing when there is no session, `calendar` needs the session or `--name` |
| `trader/results.json` (`--write`) | `created_by`, `as_of` (+05:30), `symbol`, `facts` (rows, TTM, basis, scale, files), `changes`, `calendar`, `summary`; a `calendar` run adds to the same company's `facts` run |
| `--metrics FILE` | one row per company (TTM revenue, PAT and EPS, the latest growth and margins) merged into a JSON file for notes or a sheet; `metrics.example.json` here is a run on the test fixture, a fictional company |

The client identifies itself as `day-trader-skill-india/1.0`, sends no
cookies, never impersonates a browser and never retries past a 403. If a
feed is down the tool says so in one line and goes on with the rest.

## The rules

- **Filed numbers first.** The table is the XBRL the company filed. Use the
  press release and the call for colour (guidance, segments), and say which
  numbers came from where.
- **Compare like with like.** Growth is shown against the same quarter a
  year earlier and against the quarter before; read the year-earlier number
  first, many businesses are seasonal (the December quarter carries the
  festive season, the March quarter the year-end push).
- **Say what is derived.** EBITDA is not an Ind AS line: the tool derives it
  as PBT plus finance costs plus depreciation and marks it. A quarter that is
  only in the file as a year-to-date column (the fourth quarter, in the
  annual filing) is the year minus the nine months and is marked "derived".
  Banks and lenders get interest earned as revenue and no EBITDA.
- **Say the unit.** Filings come in lakh or crore. The tool reads the level
  of rounding from the file, prints everything in crore with two decimals,
  and refuses to guess: a file without the rounding element needs `--scale`.
- **Say the basis.** Consolidated when the file has it, else standalone;
  `--consolidated` or `--standalone` to force it. The header says which.
- **Flag one-offs.** Exceptional items inside PBT, other income above a
  quarter of PBT, PAT above PBT (a tax credit): the tool says so. Check the
  notes before reading any of them as growth.
- **The calendar is unofficial.** No results calendar may be read by a
  script, so `calendar` reports what headlines and today's BSE announcements
  say, with the caveat, and asks the user to confirm on the exchange's
  board-meeting page. `--date` with a date the user has confirmed gives the
  plain answer: today, this week, later, or already out.

## How Claude runs it

1. Ask for the filing if there is none yet: tell the user exactly where the
   XBRL link is (NSE: Corporates, Financial Results, the XBRL column; BSE:
   the announcement's XBRL attachment) and to save it anywhere. Never fetch
   it for them from the exchange.
2. `facts SYMBOL --file PATH` (add `--write` so the next skill can read it).
   If the tool prints a NOTE that the file names another company, stop and
   say so. If it asks for `--scale`, open the PDF, read the unit line and
   pass it.
3. Write the summary in plain words: one line on the quarter (revenue, PAT,
   EPS, growth), one on margins, one on anything unusual (an exceptional
   item, a derived quarter, standalone instead of consolidated, a bank), and
   the trailing twelve months. Real numbers in crore, no adjectives the
   numbers do not support.
4. If the user pastes the commentary or the call, `summary --paste` sorts
   the sentences by topic (guidance, margins, demand, balance sheet,
   one-offs, headwinds, what management declined to answer). It is a
   keyword pass, not an understanding: read the lines it picks in context
   before quoting them.
5. `calendar SYMBOL` before any trade that could be held into a result.
   If the most-mentioned date is today or inside the week, say it before
   `/trader-exit` sets the stop: a stop does not protect against a gap, and
   Indian companies publish during market hours as often as after the close.
   When the user already knows the date, `calendar SYMBOL --date YYYY-MM-DD`.
6. Exit codes: 0 a result, 1 a result with a source or file that failed, 2
   nothing (no readable file, empty paste, bad arguments). Read stderr for
   the one-line reason.

## Example

`facts SAMPLE --file sample-SAMPLE-Q2FY27.xml` on the test fixture (a
fictional company, filed in lakh): Q2 FY27 revenue Rs 1,200.00 crore, +22.4%
against a year earlier and +14.3% on the quarter before, PAT Rs 142.50 crore
(+31.0% yoy), diluted EPS Rs 14.20; EBITDA margin (derived) 22.6%, down 0.4
points on the quarter; trailing twelve months Rs 4,350.00 crore revenue and
Rs 50.05 EPS, built as FY26 plus H1 FY27 minus H1 FY26 from the year-to-date
columns. The tool flags that Q1 FY27 carried an exceptional item of Rs
-15.00 crore, so the quarter-on-quarter comparison is distorted.

`calendar RELIANCE` on 2026-10-29: three headlines named October 30 for the
Q2 result, nothing from the company in BSE's feed that day; the answer was
"1 trading day away (this week): a day trade closed by flat-by before that
day is not exposed to it; nothing held overnight into it is protected by a
stop", with the caveat to confirm on the exchange's page.

## What it never does

- Never says a stock is cheap, expensive, a buy or a sell. It reports what
  the company filed.
- Never invents a number the filing does not have, never guesses the unit,
  never estimates a quarter it cannot derive.
- Never scrapes nseindia.com, nsearchives.nseindia.com or BSE's site API,
  never spoofs a browser, never retries past a 403.

## Fine print

Education and tooling, not financial advice. This skill only reads filings
and public feeds and does arithmetic; it cannot see the future and the
results calendar it reports is unofficial until the user confirms it on the
exchange's page. Nothing in this pack places an order except
`/trader-broker`, after `/trader-risk` passes and the user types an explicit
yes for that order.
