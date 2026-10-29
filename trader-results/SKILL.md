---
name: trader-results
description: >-
  Results Analyst. Reads the quarterly results an NSE or BSE company filed,
  from the results PDF BSE's public announcements feed links the moment it is
  filed (facts SYMBOL --today, or --pdf LINK; read with pdftotext) or from
  the Ind AS XBRL file you download from the exchange (--file), and sums up
  what changed: revenue from operations, total income, EBITDA (derived), PBT,
  PAT, margins, basic and diluted EPS, quarter by quarter, each against the
  previous quarter and the same quarter a year earlier, with the trailing
  twelve months, in rupees crore. Sums up a pasted results commentary or
  earnings-call transcript. Says whether the next result falls inside a day
  trade's horizon, from Google News headlines and BSE's own announcements
  feed. Use when the user says "how were Reliance's results", "did it report
  today", "analyse the Q2 numbers", "what changed this quarter", "is revenue
  growing", "read the results filing", "compare margins", "sum up the call",
  "when are the results", "does it report this week", or before trading a
  stock around its results.
argument-hint: "[facts SYMBOL --today | --pdf LINK_OR_PATH | --file results.xml | summary --paste | calendar SYMBOL [--date YYYY-MM-DD]]"
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
python3 "${CLAUDE_SKILL_DIR}/results.py" facts INDIGO --today                       # today's results PDF from BSE's feed
python3 "${CLAUDE_SKILL_DIR}/results.py" facts INDIGO --today --standalone --write
python3 "${CLAUDE_SKILL_DIR}/results.py" facts INDIGO --pdf https://www.bseindia.com/xml-data/corpfiling/AttachLive/6898d04a-23b0-45d7-a70e-0645f44dafc9.pdf
python3 "${CLAUDE_SKILL_DIR}/results.py" facts INDIGO --pdf ~/Downloads/results.pdf --bse-code 539448
python3 "${CLAUDE_SKILL_DIR}/results.py" facts RELIANCE --file ~/Downloads/results.xml
python3 "${CLAUDE_SKILL_DIR}/results.py" facts RELIANCE --file q2.xml --file q1.xml --quarters 8 --write
python3 "${CLAUDE_SKILL_DIR}/results.py" summary --paste < transcript.txt
python3 "${CLAUDE_SKILL_DIR}/results.py" calendar RELIANCE
python3 "${CLAUDE_SKILL_DIR}/results.py" calendar RELIANCE --date 2026-10-30
```

Run it from the user's project folder: `trader/` and `.env` are relative to
it. `--json` prints everything machine-readable, `--now "2026-10-29 10:15"`
pins the clock (IST), `--offline DIR` reads saved feeds and saved PDF text
(the tests use it).

## Where the numbers come from

Two routes to the same table. Both carry this quarter, the previous quarter,
the same quarter a year earlier, both year-to-date columns and the last full
year, so one filing gives the quarters and a trailing twelve months.

**The results PDF, from BSE's public feed (`--today`, `--pdf`).** BSE's
corporate announcements RSS (`bseindia.com/data/xml/announcements.xml`, the
same feed `/trader-news` reads, cached ten minutes under `trader/cache/`)
links every company's results PDF the moment it is filed, and that PDF
carries the SEBI LODR Regulation 33 "Statement of (un)audited financial
results" table. `facts SYMBOL --today` finds today's results filing for the
company in the feed (matched by Kite's instrument name, as the news tool
does, or by `--bse-code CODE`), fetches the PDF with the pack's plainly
identified client, caches it under `trader/cache/bse-pdf/<guid>.pdf`, turns
it into text with poppler's `pdftotext -layout` and reads the statement.
`--pdf LINK` does the same for a link from the feed, `--pdf PATH` for a PDF
the user saved (a `.txt` made with `pdftotext -layout` is read as is). The
tool reads: the column dates (quarter ended, preceding quarter, year-earlier
quarter, half year or nine months, year ended, in the order the filing
prints them, dates split over two lines included); the rows for revenue from
operations (interest earned for a bank), other income, total income, total
expenses, finance costs, depreciation, profit before exceptional items and
tax, exceptional items, PBT, tax, net profit for the period and basic /
diluted EPS; and the unit line (Rs in lakhs / crore / million), converting
everything to crore. Consolidated is preferred when the PDF carries both
statements (`--standalone` to switch); standalone-and-consolidated side by
side is split by the column headers. The numbers go through the same
quarters table, growth, margins and "What changed" as the XBRL route, marked
`source: "bse-pdf"`, and the output adds a "From the PDF" block: the columns
read, the unit line, the other statements in the file, and every row or cell
it could not read and why (no row found, an unreadable number, an OCR slip).
A PDF that is a scanned image (no text layer) is reported as such with the
link. Needs poppler: without it the tool prints one line (`brew install
poppler`, Debian `apt install poppler-utils`) and suggests the XBRL.

**The Ind AS XBRL file (`--file`).** The exact route: the exchanges do not
allow scripted access to their filing pages, and this pack does not pretend
to be a browser to get round that. The user downloads the file in their own
browser and hands the tool the path:

- NSE: nseindia.com, Corporates, Financial Results, find the company, the
  row's XBRL link. Save the `.xml`.
- BSE: the result announcement on bseindia.com, its XBRL attachment.

Pass several filings with repeated `--file` for a longer table; a later
filing's numbers win where they overlap (restatements). When the XBRL and
the PDF disagree, the XBRL is the filed number.

### What the PDF route reads, and what it does not

Checked on 2026-10-29, a heavy Q2 results day, against nineteen real
filings from the feed (six are saved as test fixtures):

- Reads in full: text-born PDFs in the standard layout, whatever the unit
  and the date format (InterGlobe Aviation, Rupees in millions, consolidated
  and standalone on separate pages; Vaibhav Global, lakhs with Indian
  grouping "1,85,544.32"; Welspun Corp, crores, unlabelled statement read as
  consolidated from its non-controlling-interest line; Milky Mist, dates
  printed as "September" over "30,2026"; JSW Holdings; Symphony, which
  prints the quarter columns left of the row labels).
- Reads banks (ESAF Small Finance Bank, Fino Payments Bank): interest earned
  stands in for revenue, EBITDA is left blank. OCR text layers are read as
  far as the numbers prove themselves ("1, 17,471" is 1,17,471; "16,502 89"
  is not read); rows wrapped over two or three lines are joined; a cell that
  is hundreds of times off its row-mates is dropped as an OCR slip. Every
  dropped cell is listed under "Not read from the PDF".
- Rebuilds what it can: revenue as total income minus other income when the
  revenue cell is unreadable (Lakshmi Electrical, Voltamp, IEX), and the
  unit from another page of the same filing when the statement's own unit
  line is garbled.
- Does not read, and says so with the page and the reason: a header whose
  dates are OCR-garbled so that there are more number columns than readable
  dates (Transwarranty Finance, standalone and consolidated side by side;
  EPACK Durable; Kalpataru's consolidated page; ICRA), numbers scattered
  over separate lines (Kalpataru's standalone page), and scanned filings
  with no text layer (Deepak Nitrite, Banaras Beads, Anlon Healthcare). For
  those, open the link the tool prints, or download the XBRL and pass
  `--file`.

## What it reads and writes

| file or feed | what |
| --- | --- |
| `--file PATH` (one or more) | Ind AS XBRL financial-results instance files the user downloaded from NSE or BSE. Elements are matched by local name (`RevenueFromOperations`, `Income`, `FinanceCosts`, `DepreciationDepletionAndAmortisationExpense`, `ProfitBeforeTax`, `ProfitLossForPeriod`, the EPS lines); contexts give the periods and the consolidated / standalone dimension; `LevelOfRoundingUsedInFinancialStatements` gives lakh / crore / actual |
| BSE announcements RSS plus the results PDF it links (`--today`, `--pdf`) | the feed item for the company whose description says financial results (press releases, investor presentations and call recordings are passed over); the PDF, fetched once into `trader/cache/bse-pdf/<guid>.pdf`, read with `pdftotext -layout`; the Regulation 33 statement's column dates, rows and unit line as described above |
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

- **Filed numbers first.** The table is what the company filed with the
  exchange: the Regulation 33 statement in its results PDF, or the XBRL.
  Use the press release and the call for colour (guidance, segments), and
  say which numbers came from where. A number read from a PDF's text layer
  is a number read by a machine from a page: when it looks off, open the PDF
  (the tool prints the link) before quoting it.
- **Compare like with like.** Growth is shown against the same quarter a
  year earlier and against the quarter before; read the year-earlier number
  first, many businesses are seasonal (the December quarter carries the
  festive season, the March quarter the year-end push).
- **Say what is derived.** EBITDA is not an Ind AS line: the tool derives it
  as PBT plus finance costs plus depreciation and marks it. A quarter that is
  only in the file as a year-to-date column (the fourth quarter, in the
  annual filing) is the year minus the nine months and is marked "derived".
  Banks and lenders get interest earned as revenue and no EBITDA.
- **Say the unit.** Filings come in lakh, crore or million. The tool reads
  the level of rounding from the XBRL or the unit line of the PDF, prints
  everything in crore with two decimals, and refuses to guess: a file or
  PDF without it needs `--scale`.
- **Say the basis.** Consolidated when the filing has it, else standalone;
  `--consolidated` or `--standalone` to force it. The header says which; a
  PDF statement whose title names neither says "not stated in the PDF".
- **Say what was not read.** The PDF route lists every row it could not
  find and every cell it could not read, with the column and the reason.
  Read that block before the numbers.
- **Flag one-offs.** Exceptional items inside PBT, other income above a
  quarter of PBT, PAT above PBT (a tax credit): the tool says so. Check the
  notes before reading any of them as growth.
- **The calendar is unofficial.** No results calendar may be read by a
  script, so `calendar` reports what headlines and today's BSE announcements
  say, with the caveat, and asks the user to confirm on the exchange's
  board-meeting page. `--date` with a date the user has confirmed gives the
  plain answer: today, this week, later, or already out.

## How Claude runs it

1. On a results day, `facts SYMBOL --today` first: it finds the filing in
   BSE's feed and reads the PDF. If it says nothing was filed, the company
   has not reported yet today (or is listed under another name: pass
   `--bse-code`). If it reports a scanned PDF or an unreadable header, give
   the user the link it printed and ask for the XBRL.
2. For an older result, or when the PDF could not be read: ask for the XBRL
   and tell the user exactly where the link is (NSE: Corporates, Financial
   Results, the XBRL column; BSE: the announcement's XBRL attachment). Never
   fetch it for them from the exchange. Then `facts SYMBOL --file PATH`.
   Add `--write` on either route so the next skill can read it. If the tool
   prints a NOTE that the file names another company, stop and say so. If
   it asks for `--scale`, open the PDF, read the unit line and pass it.
3. Write the summary in plain words: one line on the quarter (revenue, PAT,
   EPS, growth), one on margins, one on anything unusual (an exceptional
   item, a derived quarter, standalone instead of consolidated, a bank), and
   the trailing twelve months. Real numbers in crore, no adjectives the
   numbers do not support. From a PDF, repeat the "Not read" lines: a
   missing prior-year cell means the growth figure next to it is missing
   too, not zero.
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
6. Exit codes: 0 a result, 1 a result with a source or file that failed,
   or the PDF route with nothing to show (nothing filed today, a 403, a
   scanned PDF, an unreadable header: the reason and the link are on
   stderr), 2 nothing (no readable file, empty paste, bad arguments, no
   company name for `--today`). Read stderr for the one-line reason.

## Example

`facts INDIGO --today` on 2026-10-29 (InterGlobe Aviation's Q2 FY27 result,
filed that afternoon; the PDF is a test fixture): consolidated, filed in
millions and converted; Q2 FY27 revenue Rs 23,363.80 crore (+25.9% against
a year earlier, -5.0% on the quarter before), PAT Rs -1,879.10 crore, diluted
EPS Rs -48.58; EBITDA margin (derived) 12.2%; trailing twelve months Rs
93,858.20 crore revenue and Rs -4,104.90 crore PAT from the year-to-date
columns; every row and column read. On Fino Payments Bank's filing the same
day (an OCR text layer) it read the three quarters and the current half
year, and listed the prior half year's PAT ("3,31 I") and the tax cells it
could not read.

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
  never spoofs a browser, never retries past a 403. The PDF comes from the
  link in BSE's public RSS feed, fetched once by a plainly identified
  client.
- Never fills a cell it could not read: an unreadable number is listed, not
  guessed.

## Fine print

Education and tooling, not financial advice. This skill only reads filings
and public feeds and does arithmetic; it cannot see the future and the
results calendar it reports is unofficial until the user confirms it on the
exchange's page. Nothing in this pack places an order except
`/trader-broker`, after `/trader-risk` passes and the user types an explicit
yes for that order.
