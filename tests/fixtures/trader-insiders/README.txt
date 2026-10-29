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

bse-pdf/: the disclosure PDFs behind BSE's announcements feed, for `today --parse`
and `pdf PATH` (trader-insiders/bsepdf.py).

  REAL PUBLIC FILINGS, fetched on 2026-10-29 from the AttachLive links in BSE's
  announcements feed with the pack's identified client (User-Agent
  day-trader-skill-india/1.0, no cookies; every one answered 200). The .txt next
  to a .pdf is exactly `pdftotext -layout <pdf> -` (poppler 25.x). The PDF is
  kept only when it is under about 300 KB; the larger ones are kept as text.

    umiya-sast-29.pdf / .txt        SAST Regulation 29(2), Umiya Buildcon (MRO-TEK Networks), promoter buy of 500 shares, the plain template
    mukta-sast-29.pdf / .txt        Regulation 29(2), Mukta Agriculture, promoter sale of 2,00,000 shares ("Details of sale", "Open Market Sale")
    quint-sast-29.pdf / .txt        Regulation 29(2), Quint Digital, 44,84,838 CCPS acquired (the shares row is a dash, the convertibles row carries the number), footnotes, two annexure tables
    skm-sast-29-ocr.pdf / .txt      Regulation 29(2), SKM Egg Products, a scan WITH an OCR text layer: junk tokens (2,39,92,27~, 45.5(, 10,00() that the reader leaves out
    jindal-sast-29.txt              Regulation 29(2), Jindal Steel, acquirer Jindal Power (promoter group), percentages to three decimals, a 49-row promoter annexure; PDF 330 KB, text only
    bajaj-sast-29.txt               Regulation 29(2), Bajaj Electricals, seller HDFC Mutual Fund (not a promoter), a table split over two pages with a repeated header, a footnote on the holding; PDF 1.4 MB, text only
    kkalpana-sast-29-ocr.txt        Regulation 29(2), Kkalpana Plastick, two sellers listed i) and ii), an off-market sale under a share purchase agreement, "Nil" after; OCR layer; PDF 1.0 MB, text only
    callista-sast-29-scanned-p1.pdf Regulation 29(2), Callista Industries: a scanned image with NO text layer (page 1 of the 4-page PDF, cut with pdfseparate to stay small); pdftotext gives nothing and the tool says so
    bse-announcements-filings.xml   the ten feed items of 2026-10-29 that pointed at these PDFs (the feed's own XML, items only), used by the `today --parse` test

  TRANSCRIBED FROM A REAL FILING: camlin-sast-31-transcribed.txt is the Camlin
  Fine Sciences Regulation 31(1)/(2) pledge disclosure of 2026-10-29 (feed GUID
  381CA894_8360_49FF_B8EF_F3748DAD8472_155003, a 2.3 MB scan with no text
  layer). Its two tables (Annexure I, the events; Annexure II, the reasons) were
  read off the scanned pages and typed in the layout pdftotext gives such a
  table, every number and name as filed. The scan itself is not kept (size).

  SAMPLE TEXT, NOT A FILING: formc-pit-7-sample-1.txt and formc-pit-7-sample-2.txt
  are SEBI's Form C (PIT Regulation 7(2) read with 6(2)) in the layout pdftotext
  gives it, with fictional companies, people and numbers. BSE's announcements
  feed carried no Regulation 7(2) filing on 2026-10-29 (companies file those
  through BSE's separate insider-trading module, whose pages are not a public
  feed), so no real one could be fetched the honest way. Sample 1 has the
  numbered columns (1 to 15) and "No. and %" in one cell; sample 2 has a cover
  letter, split "No." and "%" columns, two persons and a futures row in the
  derivatives block.
