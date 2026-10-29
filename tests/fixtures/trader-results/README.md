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
