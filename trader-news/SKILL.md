---
name: trader-news
description: >-
  News Monitor. Tracks the news on every NSE stock you watch and flags what
  moves it: Google News headlines per company, the Economic Times and Mint
  market feeds, Moneycontrol's market reports, SEBI's own RSS and the day's
  BSE corporate filings for each company (results, board meetings, insider
  and SAST disclosures, with the PDF link), plus how far each stock is from
  yesterday's close on Kite. Shows only what is new since
  the last check, newest first, in IST, tags each item (results, deal,
  analyst, regulatory, macro, people, bulk-deal, halt, promoter, dividend)
  and never repeats one. Use when the user says "any news on RELIANCE", "why
  is this moving", "what's the news on my watchlist", "morning briefing",
  "pre-market news", "monitor the news", "did anything break", "anything from
  SEBI or RBI today", or after /trader-scanner finds setups.
argument-hint: "[symbols]"
---

# trader-news

A chart can look perfect and still be about to gap on a results date, a
circuit, a SEBI order or an RBI surprise. Before a setup becomes a trade,
this skill reads the feeds that carry those first, for every stock on the
watchlist, and tells you only what is new. It also prints how far each stock
has moved today, so a headline and a move sit side by side.

Tool in this folder (standard library Python; needs today's Kite session
from `/trader-kite` for the company names and the moves):

```bash
python3 "${CLAUDE_SKILL_DIR}/news.py"                                   # watchlist + plan + portfolio
python3 "${CLAUDE_SKILL_DIR}/news.py" --tickers RELIANCE,INFY --hours 12
python3 "${CLAUDE_SKILL_DIR}/news.py" --no-market --json                # per-symbol headlines only
python3 "${CLAUDE_SKILL_DIR}/news.py" --tickers TCS --all --hours 72    # seen items too
python3 "${CLAUDE_SKILL_DIR}/news.py" --bse-code RELIANCE=500325        # pin a BSE scrip code; --no-bse skips the filings
```

Run it from the user's project folder: `trader/` and `.env` are relative to
it. Symbols are Kite trading symbols (`RELIANCE`, not `RELIANCE.NS`; the
suffixed and `NSE:` forms are accepted).

## What it reads and writes

| file / feed | what |
| --- | --- |
| `--tickers`, else `trader/watchlist.json` | the scanner's hits (`hits[].symbol`, trader-scanner) |
| `trader/plan.json` | the trade you are planning (trader-exit) |
| `trader/portfolio.json` | what you hold (`positions`, trader-broker) |
| Kite instrument list (`trader/cache/`) | the company name behind each symbol, for the query and the mentions |
| Google News RSS | one query per symbol: `"Reliance Industries" OR RELIANCE`, `hl=en-IN&gl=IN&ceid=IN:en` |
| Economic Times stocks RSS, Mint markets RSS | market-wide headlines |
| Moneycontrol market reports RSS | market reports (the feed answers, but it has not moved since 2024; it costs one request and nothing else) |
| SEBI RSS (`sebi.gov.in/sebirss.xml`) | press releases, circulars, orders; dated to the day, shown at 00:00 IST |
| BSE announcements RSS (`bseindia.com/data/xml/announcements.xml`) | the current day's filings from every listed company (about 9,000 items, 3.5 MB): your companies' filings matched by name (Kite's `Reliance Industries` against `Reliance Industries Ltd (500325)`; the debt scrip codes of the same company merge into one item), or by `--bse-code SYM=CODE`; each with its PDF link. Fetched once per run and reused for 10 minutes |
| `trader/cache/bse-announcements-<date>-<HHMM>.xml` | the raw BSE feed, shared with `/trader-insiders` |
| Kite quote | the last price against yesterday's close (`ohlc.close`), per symbol, one call |
| `trader/news_seen.json` | what has been shown (kept 30 days), so an item shows once |

Every feed is read by a plainly identified client (`day-trader-skill-india/1.0`,
no cookies, no browser impersonation, one request per feed, no retry past an
error). nseindia.com is not read at all: NSE's terms forbid scripted access.

## The rules

- **Headlines are leads, not facts.** Before telling the user something
  happened, open the story and read it. Quote the source and link it. Never
  summarise a paywalled story you could not read: say so.
- **The exchange filing beats the headline.** If a story and the company's
  own filing disagree, the filing is what the company told the exchange.
  `/trader-insiders` reads the disclosure CSVs; `/trader-results` reads the
  results.
- **Say what it touches.** Every item lists the symbols it touches and its
  tags. Items marked `!` carry a tag that can gap a stock: results, halt
  (circuit, suspension, F&O ban), bulk-deal, promoter or insider-sast.
- **A filing is the company's own words.** `BSE filing` items are what the
  company sent the exchange today: results (`results`), a board meeting
  notice or outcome (`board-meeting`), an insider-trading or SAST
  disclosure or a pledge (`insider-sast`), or anything else. A board
  meeting notice cites the insider-trading code because the trading window
  closes; that is not a disclosure and is not tagged as one. Open the PDF
  before quoting it; a bland description ("Enclosed") says nothing.
- **Market-wide items earn their place.** A story from the Economic Times,
  Mint, Moneycontrol or SEBI is shown when it names one of your symbols or
  carries a market tag (macro: RBI, repo rate, inflation, FII/DII flows, the
  Fed; regulatory: SEBI, a probe, a court; halt; bulk-deal; promoter).
  Another company's results and a broker's stock picks are noise and stay
  out.
- **MOVING** marks a stock 3 % or more from yesterday's close (`--move-pct`).
  A move with no headline is worth saying out loud too. After 15:30 the line
  reads today's close against yesterday's.
- **No predictions.** Report what was published and how far the price is from
  yesterday's close. Never say what the stock will do next.

## How Claude runs it

1. Make sure today's Kite session exists (`/trader-kite` status). Without
   one the tool prints the login line and exits 1; run the login, then this.
2. Run the check: `python3 "${CLAUDE_SKILL_DIR}/news.py"`. Exit code 0 means
   something new, 1 nothing new, 2 no symbols to watch. `--all` shows items
   already seen, `--json` gives everything machine readable, `--dry-run`
   leaves the state alone, `--now "2026-10-29 10:15"` pins the clock (IST).
3. Read the items that matter: every `!` item, anything tagged halt,
   regulatory, macro or promoter, and every MOVING symbol first.
4. Write a short brief, per symbol: what happened, the source and link, the
   move, and one line on why it matters to the setup (for example "results
   are after the close today: a day trade is out by 15:05 either way").
5. If a story changes the picture, say so before `/trader-entry` and
   `/trader-exit`: a results date inside the trade, a circuit, an open
   offer, a SEBI order. Never act on a headline alone.
6. A `note:` line means a feed did not answer; the rest of the check still
   ran. Say which feed was down and move on.
7. A `BSE filing` line is today's exchange filing for that company: say what
   it is from the PDF, not from the one-line description. If the name match
   picks the wrong company (two companies with the same first words), pin the
   scrip code with `--bse-code SYM=CODE` (the number in the BSE title).

During the session: `/loop 15m /trader-news`, or a scheduled task each
morning before 09:15 IST.

## Example: Thursday 29 October 2026

The check at 18:30 IST for RELIANCE, INFY and TCS (real feeds, saved as the
test fixtures): 38 new items in 24 hours, 21 from Google News, 9 from the
Economic Times, 8 from Mint. The moves: `INFY 1,146.50 +2.08% vs yesterday's
close 1,123.15 at 15:59 IST`, RELIANCE +0.81 %, TCS +0.73 %. The items worth
the brief: "RIL Q2 Results Preview: Profit outlook brightens as O2C business
set to drive growth" (Economic Times, 14:54, results, marked `!`), "TCS,
Infosys, HCL Tech: Anthropic revenue soars 14-fold in H1 2026" (Mint, 17:18,
touches INFY and TCS), "US Fed FOMC pauses interest rates: Can Nifty bulls
take charge or will RBI hike spoil the rally?" (Mint, 14:37, macro). The
brief: "Reliance reports tomorrow after the close, so Friday's trade is in
and out before 15:05; no circuit, no SEBI item on any of the three." When a
symbol has no news, say so rather than padding the brief.

## What it never does

- Never invents or embellishes a story, a quote or a number.
- Never trades on news, and never tells the user to buy or sell because of a
  headline.
- Never reads nseindia.com, never spoofs a browser, never retries past a 403.
  One request per feed per check, a pause between calls, and the 3.5 MB BSE
  feed at most once every ten minutes.

## Fine print

Education and tooling, not financial advice. This skill only reads and
reports; the feeds can lag, drop a story or go down. Nothing places an order
except `/trader-broker`, after `/trader-risk` passes and the user types an
explicit yes for that order in the chat.
