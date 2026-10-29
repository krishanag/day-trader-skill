# trader-results fixtures

Saved on 2026-10-29 with the pack's plainly identified client
(`day-trader-skill-india/1.0`), except the three XBRL files, which are
SAMPLES.

| file | what |
| --- | --- |
| `sample-SAMPLE-Q3FY26.xml`, `sample-SAMPLE-Q1FY27.xml`, `sample-SAMPLE-Q2FY27.xml` | SAMPLE Ind AS financial-results XBRL instance documents for SAMPLE INDUSTRIES LIMITED, a fictional company; every number is made up (rupees lakh, consolidated, plus standalone in the Q2 FY27 file). The element names (`in-bse-fin:RevenueFromOperations`, `Income`, `FinanceCosts`, `DepreciationDepletionAndAmortisationExpense`, `ProfitBeforeExceptionalItemsAndTax`, `ProfitBeforeTax`, `TaxExpense`, `ProfitLossForPeriod`, the EPS lines, `in-capmkt:LevelOfRoundingUsedInFinancialStatements` and the rest of the header), the `OneD` to `SixD` contexts (quarter, previous quarter, year-earlier quarter, year to date both years, last full year) and the `ConsolidatedAndStandaloneAxis` dimension follow the BSE / NSE Ind AS results taxonomy. They are samples because nseindia.com / nsearchives.nseindia.com do not answer the honest client and BSE's result announcements link PDFs, not XBRL. |
| `googlenews-RELIANCE-results.xml` | real: Google News RSS for `"Reliance Industries" Q2 results date` (33 items) |
| `googlenews-INFY-board.xml` | real: Google News RSS for `Infosys board meeting financial results` (69 items) |
| `bse-announcements.xml` | real, cut: 11 of the 9,427 items in `https://www.bseindia.com/data/xml/announcements.xml` that day (Reliance Industries, Reliance Chemotex, Infosys, Coal India, Intrasoft Technologies, Accedere, Systematic Industries), the feed's own header kept |
| `instruments-NSE.csv` | real: the header and six rows of Kite's NSE instrument dump (RELIANCE, INFY, TCS, HDFCBANK, ISFT-BE, COALINDIA) |

## bse-pdf/: real results PDFs from BSE's announcements feed

Six real public filings of 29 October 2026 (a heavy Q2 FY27 results day),
fetched from the links in `https://www.bseindia.com/data/xml/announcements.xml`
with the pack's plainly identified client (`day-trader-skill-india/1.0`, no
cookies, no retry). They are public documents filed under SEBI LODR
Regulation 33; the companies are named below. Each `.txt` is the whole
filing's `pdftotext -layout` output, exactly what `results.py facts --today`
sees live; each `.pdf` is cut to the results-statement page(s) of the same
filing with poppler's `pdfseparate` / `pdfunite` (page numbers below are the
filing's own), so the fixtures stay small while the one test that runs
`pdftotext` itself still reads the real table.

| file | company (BSE code, Kite symbol) | layout | what the tool reads |
| --- | --- | --- | --- |
| `INDIGO-6898d04a-....pdf` (pages 3 and 10) / `.txt` | InterGlobe Aviation Ltd (539448, INDIGO) | a large non-financial company, Rupees in millions, consolidated statement on page 3 and standalone on page 10, dates split over two lines ("30 September" over "2026"), losses in parentheses | everything, both bases |
| `ESAFSFB-0466d0c6-....pdf` (page 5) / `.txt` | ESAF Small Finance Bank Ltd (544020, ESAFSFB) | a bank: interest earned, operating profit, provisions; Rs in lakhs; OCR text layer ("1, 17,471", "Profitl(Loss)"); the title names no basis | everything a bank has (no EBITDA); the OCR'd numbers are repaired where the grouping proves them |
| `FINOPB-31008381-....pdf` (page 2) / `.txt` | Fino Payments Bank Ltd (543386, FINOPB) | a bank with a rough OCR layer: row labels and numbers wrap over two or three lines, "3,31 I" for 3,311, "l ,274" for 1,274 | the quarters and the half year; the tool lists the cells it could not read (prior half year and year of PBT and PAT, EPS before extraordinary items) |
| `LECSL-1b7a1665-....pdf` (page 3) / `.txt` | Lakshmi Electrical Control Systems Ltd (504258, BSE only) | a small company, Rs in lakhs, "Statement of Unaudited Results" with no basis word, OCR slips in the total income row ("9,508.S7") | revenue, PBT, PAT, EPS, the year-to-date columns; total income is rebuilt as revenue plus other income |
| `TFL-6eb23ba4-....pdf` (page 2) / `.txt` | Transwarranty Finance Ltd (532812, TFL) | standalone and consolidated side by side (twelve columns) with an OCR layer that garbles the dates ("30(09/2026", "31/03/2426") and the profit rows | nothing: reported as "a results statement was found but not read" with the page and the reason |
| `DEEPAKNTR-a59f1b7a-....pdf` (pages 2 and 3) / `.txt` | Deepak Nitrite Ltd (506401, DEEPAKNTR) | a scanned filing: only the cover letter has a text layer, the statement pages are images | nothing: reported as "no text layer (39 lines of text over 10 pages)" with the link |
| `bse-announcements.xml` | the feed's own header and the 17 items of that day for these six companies (results, press releases, investor presentations, board-meeting outcomes, a postal ballot), cut from the 9,775-item feed | `facts SYMBOL --today --offline tests/fixtures/trader-results/bse-pdf` picks the results filing among them |
