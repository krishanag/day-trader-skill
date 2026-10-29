Fixtures for tests/test_insiders.py.

SAMPLE FILES, NOT NSE DATA: insider-trading-sample.csv, bulk-deals-sample.csv and
block-deals-sample.csv carry NSE's real column headers (as NSE's own CSV downloads
spell them, DISPLOSED and INITMATION and BROADCATE included) with made-up rows:
the symbols and company names are real, every person, client and number is
fictional. NSE blocks scripted downloads and its terms forbid scraping, so these
files were written by hand in NSE's format; the tool reads the real files the user
downloads from nseindia.com in their browser.

google-deals-RELIANCE.xml is a real Google News RSS response (the first 30 of 62
items) fetched on 2026-10-29 with the pack's identified client for the query
"Reliance Industries" (bulk deal OR block deal OR insider OR promoter stake).

bse-announcements.xml for the `today` tests lives in tests/fixtures/trader-news/ (shared).
