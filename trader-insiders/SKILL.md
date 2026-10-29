---
name: trader-insiders
description: >-
  Insider Tracker. Reads NSE's insider trading disclosures (SEBI PIT
  Regulation 7(2)), bulk deals and block deals from the CSV files you
  download on NSE's Corporates pages, alerts once per new row, and turns each
  row into plain facts: who, category (promoter, promoter group, director,
  KMP, designated person, relative), buy or sell or pledge, quantity, value
  in rupees, mode (market, off-market, ESOP, gift, inter-se), the trade
  dates, and how many trading days the disclosure took, with late ones
  flagged. Flags symbols you hold or watch, reads BSE's live announcements
  feed for today's insider, SAST and pledge filings (with the PDF link), and
  watches Google News for bulk deal, block deal and promoter-stake headlines. Use when the user says "any
  insider buying in RELIANCE", "promoter bought shares", "bulk deals today",
  "block deals", "who sold a stake", "pledged shares", "check the insider
  trading file", "alert me on new disclosures", or "run the insider skill".
argument-hint: "[index | alert | today | news SYM] [--file PATH]"
---

# trader-insiders

Promoters, directors, key managerial personnel and designated persons have
to disclose their trades under SEBI's insider trading regulations, and NSE
publishes every disclosure, every bulk deal and every block deal on its
website. This skill reads those files, notices which rows are new, and lays
each one out in plain words.

It is fast. It is not early. Under Regulation 7(2) the insider has two
trading days to tell the company and the company two more to tell the
exchange, so a row is days old when it appears, and a bulk deal shows after
the close. There is no inside information here: only public disclosures,
read quickly. Say that every time.

Tool in this folder (standard library Python; `news` needs today's Kite
session from `/trader-kite` for the company name):

```bash
python3 "${CLAUDE_SKILL_DIR}/insiders.py" index --file ~/Downloads/CF-Insider-Trading-equities-29-10-2026.csv
python3 "${CLAUDE_SKILL_DIR}/insiders.py" index --file bulk.csv --symbol RELIANCE,INFY --since 2026-10-20 --json
python3 "${CLAUDE_SKILL_DIR}/insiders.py" alert --file insider.csv --file bulk.csv --file block.csv
python3 "${CLAUDE_SKILL_DIR}/insiders.py" today                        # today's insider / SAST / pledge filings on BSE
python3 "${CLAUDE_SKILL_DIR}/insiders.py" today --symbol RELIANCE,INFY  # only your companies
python3 "${CLAUDE_SKILL_DIR}/insiders.py" news RELIANCE --hours 168
```

Run it from the user's project folder: `trader/` and `.env` are relative to
it.

## The manual route, and why it is manual

NSE blocks scripted clients (every request from a script, however honest its
User-Agent, gets a 403) and its terms of use forbid scraping. This skill does
not and will not get around that: no browser headers, no cookie handshake,
no third-party mirror. The user downloads the file, the tool reads it.

1. The user opens nseindia.com in their own browser and goes to Corporates
   (under Market Data or Companies): **Insider Trading**, **Bulk Deals** or
   **Block Deals**. Each page has a date range and a download icon that
   saves a CSV.
2. They pass the saved path to the tool with `--file`. The kind of file is
   detected from its headers. Bulk and block deals share one layout, so a
   block-deals file is told apart by "block" in its name, or `--kind block`.
3. The tool reads NSE's real column names, in NSE's own spelling
   (`DISPLOSED`, `INITMATION`, `BROADCATE`) or the correct one, in any case
   and spacing. A file it does not recognise is refused with the list of
   headers it found, so the user can see what they downloaded.

Ask the user for the file; never tell them a script will fetch it.

## What it reads and writes

| file / feed | what |
| --- | --- |
| `--file PATH` (repeatable) | an NSE CSV the user downloaded: insider trading, bulk deals or block deals |
| `trader/portfolio.json` | `positions` (trader-broker): a row in a stock you hold is flagged `in your portfolio` |
| `trader/watchlist.json` | `hits[].symbol` (trader-scanner): flagged `in your watchlist` |
| `trader/insiders.json` | written by `alert`: the row ids already shown and `last_check`, so each row alerts once |
| BSE announcements RSS (`bseindia.com/data/xml/announcements.xml`) | the current day's filings from every listed company, a public feed read by the identified client; `today` keeps the insider-trading (Regulation 7), SAST (Regulation 29 / 31) and pledge disclosures, each with its PDF link, and alerts once per filing through the same `trader/insiders.json`. Fetched once and reused for 10 minutes from `trader/cache/bse-announcements-<date>-<HHMM>.xml`, shared with `/trader-news` |
| Kite instrument list (`trader/cache/`) | the company name for the `news` query and for `today --symbol` |
| Google News RSS | `"Reliance Industries" (bulk deal OR block deal OR insider OR promoter stake)`, `hl=en-IN&gl=IN&ceid=IN:en`, read by the identified client, one request, no retry past a 403 |

## The rules

- **Quote the file, not a tracker.** Every row comes from NSE's own CSV, with
  the broadcast time NSE stamped on it. Third-party sites are fine for
  browsing; the CSV wins.
- **Category matters.** A promoter group entity buying in the market is a
  different fact from an employee receiving ESOP shares, a relative
  receiving a gift, or an inter-se transfer between promoter entities that
  changes nothing. The row says which, and the mode: market, off-market,
  ESOP, gift, inter-se transfer, pledge, preferential allotment.
- **A pledge is not a sale.** `pledge`, `pledge released` and `pledge
  invoked` are their own sides. A pledge invoked is shares sold by the
  lender.
- **Say the delay.** Each row shows the trading days from the trade to the
  company and from the company to NSE; over two at either step is flagged
  LATE (Regulation 7(2)). Weekends and NSE holidays are skipped; a date
  outside the pack's holiday list counts as a trading day.
- **Rupees the way the papers print them.** `Rs 1.40 cr`, `Rs 37.50 lakh`,
  `Rs 85,000.00`; the JSON carries the raw number. A value NSE left blank
  (a gift, a derivative row) is "value not given", never a guess.
- **Bulk and block deals are the day's big tickets.** Who bought, who sold,
  how many, at what weighted average price, and the value that makes. The
  same client on both sides of a bulk deal is intraday churn, not a stake.

## How Claude runs it

1. `index --file PATH` lists every row, newest first. `--symbol`, `--since`
   and `--json` narrow it. Exit code 1 means no matching rows, 2 a file the
   tool could not read (the message says why and lists the headers).
2. `alert --file PATH [--file PATH ...]` shows only rows not shown before,
   flags the symbols you hold or watch, and records them in
   `trader/insiders.json`. Exit code 0 means something new, 1 nothing new.
   `--dry-run` leaves the state alone. Each time the user downloads a fresh
   file, run `alert` on it: only the rows added since last time show.
3. `today` lists the insider, SAST and pledge filings BSE has posted today,
   newest first, with the PDF to open; `--symbol A,B` keeps only your
   companies (the names come from Kite, so that form needs today's session).
   It says a disclosure was filed, not what is in it: the rows come from the
   NSE CSV, or from the PDF when the user opens it. Exit 0 when something is
   new. Board meeting notices mention the insider-trading code (the trading
   window closes) and are not counted.
4. `news SYM` lists the week's bulk deal, block deal, insider and
   promoter-stake headlines for that company (`--hours` widens it). It needs
   today's Kite session for the company name; without one it prints the
   login line and exits 1.
5. Write it up in plain words: who, what, how much, when, how late, and
   whether the stock is on the user's list. Then the caveat: a public
   disclosure, days old, says what someone did, not what anyone will do.
6. If the user wants to act on it, it goes the long way: a disclosure is a
   reason to look at a chart, not a setup. `/trader-scanner` and
   `/trader-entry` decide if there is a trade today, `/trader-exit` sets the
   stop, `/trader-budget` sizes it, `/trader-risk` checks it, and
   `/trader-broker` places it only after the user says yes.

## Example: an alert

```
NEW 2026-10-28  RELIANCE   buy   12,000 shares, Rs 1.40 cr, market, Sample Promoter Holdings LLP (Promoter Group)  <- RELIANCE is in your portfolio
      traded 2026-10-26, told the company 1 trading day later, on NSE 1 trading day after that, holds 4.62 % after
NEW 2026-10-29  INFY       sell  50,000 shares, Rs 5.70 cr, market, A. Sample (Designated Person)  <- INFY is in your watchlist
      traded 2026-10-22 to 2026-10-23, told the company 4 trading days later (LATE: over 2), on NSE 0 trading days after that
NEW 2026-10-29  RELIANCE   BUY   7,250,000 at Rs 1,171.50 avg = Rs 849.34 cr, SAMPLE ALPHA CAPITAL FUND
      bulk deal
```

Those rows are the pack's sample file (`tests/fixtures/trader-insiders/`,
NSE's headers, fictional rows); a real download reads the same way. Then:
"A promoter group entity bought Rs 1.4 crore of Reliance in the market on
Monday and NSE posted it Wednesday evening. It is public and two days old.
It says what they did, not what the stock does tomorrow."

## What it never does

- Never says or implies anyone has inside information, or that copying a
  promoter beats the market. No return claims.
- Never fetches from nseindia.com, spoofs a browser, or works around the
  block. The user downloads the file. BSE's feed is a public RSS file and
  is read as one, at most once every ten minutes.
- Never presents a blank value as a number, or a tracker's date as NSE's.
- Never places an order.

## Fine print

Education and tooling, not financial advice. Nothing in this pack is a
recommendation to buy or sell anything. Paper trading is the default
(`"paper": true` in `trader/config.json`). Nothing places an order except
`/trader-broker`, and only after `/trader-risk` passes AND the user types an
explicit yes for that order in the chat, AND the Kite MCP order tool is set
to "Needs approval" in Claude so its own "Allow once / Deny" card appears
too. Never "Always allow" an order tool. Insider disclosures, bulk deals and
block deals are PUBLIC records that NSE posts after the fact; this skill
reads them fast, has no inside information, and says so.
