#!/usr/bin/env python3
"""Reads the disclosure PDFs that BSE's announcements feed links to. Standard library only.

insiders.py `today --parse` and `pdf PATH` use this module:

  - cached_pdf(link, fetch, cache_dir): the PDF through the pack's identified
    client (the caller's `fetch`), kept under trader/cache/bse-pdf/<guid>.pdf.
  - pdf_text(path): `pdftotext -layout` through subprocess.run (list
    arguments, captured output, a timeout, no shell). NoPdftotext when the
    binary is missing (brew install poppler), NoTextLayer when the PDF is a
    scanned image with no text to read.
  - read_disclosure(text, hint): the facts in the text, as plain dicts, for
    three layouts: SEBI's SAST Regulation 29 format (acquirer, target, shares
    before / acquired or sold / after with percentages, mode, date), the
    Regulation 31 encumbrance format (Annexure I: creation / release /
    invocation of a pledge, the lender, the numbers) and the PIT Regulation
    7(2) Form C table (name, category, held prior, acquired or disposed with
    value and transaction type, held post, dates, mode, exchange, the
    derivatives block). insiders.py turns the facts into its rows.

Tables in these PDFs are rendered by pdftotext as columns of text, cells
vertically centred, wrapped over lines, sometimes from an OCR layer with
junk characters. The readers here parse what they can and list what they
could not in `unread`; a PDF with no table they recognise comes back with a
one-line `problem` instead of rows. Never a guess: a number that is not
clean (2,39,92,27~) is left out.
"""

import datetime
import hashlib
import os
import re
import shutil
import subprocess

PDF_DIR_NAME = "bse-pdf"
PDFTOTEXT_TIMEOUT = 60
MIN_LETTERS = 200            # fewer letters than this: a scan with no text layer
VALUE_COL_TOLERANCE = 12     # a cell's text may start some columns left of the value column (another page's table)
DEFAULT_VALUE_COL = 40
CLUSTER = 2                  # header words this close start the same column
EVENT_WORDS = r"creation|created|release|released|invocation|invoked|revocation|revoked"
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


class PdfError(Exception):
    pass


class NoPdftotext(PdfError):
    pass


class NoTextLayer(PdfError):
    pass


# ---------------------------------------------------------------- the PDF itself

def guid(link):
    """The file name BSE gave the PDF (its AttachLive GUID), else a hash of the link."""
    m = re.search(r"/([0-9A-Za-z_-]+)\.pdf$", link or "", re.I)
    return m.group(1) if m else hashlib.sha1((link or "").encode("utf-8")).hexdigest()[:16]


def cached_pdf(link, fetch, cache_dir):
    """The PDF behind `link`, fetched once with the caller's client and kept under <cache_dir>/bse-pdf/."""
    folder = os.path.join(cache_dir, PDF_DIR_NAME)
    path = os.path.join(folder, guid(link) + ".pdf")
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return path
    data = fetch(link)
    if not data.lstrip().startswith(b"%PDF"):
        raise PdfError("%s did not return a PDF" % link)
    os.makedirs(folder, exist_ok=True)
    part = path + ".part"
    with open(part, "wb") as fh:
        fh.write(data)
    os.replace(part, path)
    return path


def pdftotext_path():
    return shutil.which("pdftotext")


def pdf_text(path, exe=None):
    """`pdftotext -layout <path> -` as text. NoPdftotext / NoTextLayer / PdfError say why not."""
    exe = exe or pdftotext_path()
    if not exe:
        raise NoPdftotext("pdftotext is not installed: brew install poppler, or open the PDF link")
    try:
        done = subprocess.run([exe, "-layout", path, "-"], capture_output=True, timeout=PDFTOTEXT_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        raise PdfError("pdftotext failed on %s: %s" % (os.path.basename(path), exc))
    if done.returncode != 0:
        err = done.stderr.decode("utf-8", "replace").strip().splitlines()
        raise PdfError("pdftotext could not read %s: %s" % (os.path.basename(path),
                                                            err[-1] if err else "exit %d" % done.returncode))
    text = done.stdout.decode("utf-8", "replace")
    if len(re.findall(r"[A-Za-z]", text)) < MIN_LETTERS:
        raise NoTextLayer("no text layer (a scanned image): open the PDF to read it")
    return text


# ---------------------------------------------------------------- small helpers

def number_tokens(text):
    """Clean numbers in reading order, with a flag for a percent sign on them: [(value, is_pct)].
    OCR tokens with junk on them (2,39,92,27~ or 45.5( ) are skipped. Nil reads as 0; a dash or N.A. is nothing."""
    out = []
    for m in re.finditer(r"(?<![\w.~,])(\d[\d,]*(?:\.\d+)?)(?![\w(~.,])\s*(%?)", text or ""):
        try:
            out.append((float(m.group(1).replace(",", "")), m.group(2) == "%"))
        except ValueError:
            continue
    if not out and re.search(r"\bnil\b", text or "", re.I):
        return [(0.0, False)]
    return out


def numbers(text):
    """Just the numbers of number_tokens()."""
    return [v for v, pct in number_tokens(text)]


def count_and_pct(tokens):
    """(the first whole number, the first percentage) of a cell's tokens: '500  0.002  0.002' -> (500, 0.002),
    '6.99%  44,84,838  -' -> (4484838, 6.99). A decimal is a percentage even without the sign."""
    counts = [v for v, pct in tokens if not pct and v == int(v)]
    pcts = [v for v, pct in tokens if pct or v != int(v)]
    qty = counts[0] if counts else None
    pct = pcts[0] if pcts else (counts[1] if len(counts) > 1 else None)
    return qty, pct


def find_dates(text):
    """ISO dates in `text`: 28-10-2026, 27/10/2026, 28.10.2026, October 26, 2026, 28th October, 2026, 2026-10-28."""
    out = []
    for m in re.finditer(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})|(\d{4})-(\d{2})-(\d{2})|"
                         r"(\d{1,2})(?:st|nd|rd|th)?[-\s]+([A-Za-z]{3,9})\.?,?[-\s]+(\d{4})|"
                         r"([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", text or ""):
        g = m.groups()
        if g[0]:
            d, mo, y = int(g[0]), int(g[1]), int(g[2])
        elif g[3]:
            y, mo, d = int(g[3]), int(g[4]), int(g[5])
        elif g[6]:
            d, mo, y = int(g[6]), MONTHS.get(g[7][:3].lower()), int(g[8])
        else:
            mo, d, y = MONTHS.get(g[9][:3].lower()), int(g[10]), int(g[11])
        if not mo:
            continue
        try:
            out.append(datetime.date(y, mo, d).isoformat())
        except ValueError:
            continue
    return out


def strip_furniture(lines):
    """Lines that repeat verbatim (letterheads, page headers, footers) become blank so they do not
    land in a cell; short lines and number-only lines (a row of dashes) are left alone."""
    seen, out = {}, []
    for l in lines:
        key = l.strip()
        if len(key) >= 15 and len(re.findall(r"[A-Za-z]", key)) >= 3 and not re.match(r"(?i)(?:date|place)\b", key):
            seen[key] = seen.get(key, 0) + 1
    for l in lines:
        key = l.strip()
        out.append("" if seen.get(key, 0) > 1 else l)
    return out


def squash(text):
    return " ".join((text or "").split())


def segments(line):
    """[(start column, text)]: runs of text separated by two or more spaces."""
    return [(m.start(), m.group().strip()) for m in re.finditer(r"\S(?:\S| (?! ))*", line)]


def first_col(line):
    s = line.lstrip()
    return len(line) - len(s) if s else None


# ---------------------------------------------------------------- label / value tables (the SAST 29 format)

def label_cells(lines, labels, value_col=None):
    """Cells of a two-column form: {key: {"value": text, "numbers": [...], "line": i, ...}}.

    `labels` is [(key, regex, repeat)], the first one the anchor whose value
    starts the value column: a label's text sits in the left column
    (its regex matches at a column left of the value column); its value is the
    text in the right column on the same lines, and on the value-only lines
    around it (pdftotext centres each cell vertically, so a tall value starts
    one line above a short label). A repeat label collects a list of cells.
    The value column is where the right-hand segments of the label lines
    begin; text that pdftotext glued to a label with a single space (OCR
    layers do that) is cut at that column.
    """
    found, kinds, hits = {}, [], {}
    for i, line in enumerate(lines):
        if not line.strip():
            kinds.append(("blank",))
            continue
        hit = None
        for key, rx, repeat in labels:
            m = rx.search(line)
            if m and (repeat or key not in found) and (value_col is None or m.start() < value_col - VALUE_COL_TOLERANCE):
                hit = (key, m, repeat)
                break
        if not hit:
            kinds.append(None)
            continue
        key, m, repeat = hit
        kinds.append(("label", key))
        hits[i] = m
        cell = {"value": "", "numbers": [], "line": i, "lines": [i], "label": m.group()}
        if repeat:
            found.setdefault(key, []).append(cell)
        else:
            found[key] = cell
    if value_col is None:
        anchor = [c for i, m in hits.items() if kinds[i][1] == labels[0][0] for c in _right_columns(lines[i], m)]
        starts = [c for i, m in hits.items() for c in _right_columns(lines[i], m) if c >= MIN_VALUE_COL]
        value_col = anchor[0] if anchor else (min(starts) if starts else DEFAULT_VALUE_COL)
    cut = value_col - VALUE_COL_TOLERANCE
    inline = {}          # line -> [(text, glued)]
    for i, line in enumerate(lines):
        if i in hits:
            inline[i] = _right_segments(line, hits[i]) or [(t, False) for t, g in _glued_value(line, max(hits[i].end(), cut))]
        elif kinds[i] is None:
            kinds[i] = ("value",) if first_col(line) >= cut else ("cont",)
            if kinds[i] == ("cont",):
                inline[i] = [(t, False) for c, t in segments(line) if c >= cut] or _glued_value(line, cut)
    blocks = []          # a label line and its continuation lines up to the next label
    for i, k in enumerate(kinds):
        if k[0] == "label":
            blocks.append({"rows": [i], "cell": _cell_for(found, k[1], i)})
        elif k[0] == "cont" and blocks and not any(kinds[j][0] == "label" for j in range(blocks[-1]["rows"][-1] + 1, i)):
            blocks[-1]["rows"].append(i)
    owner = _assign_value_lines(kinds, blocks)
    pieces = {}
    for i in sorted(owner):
        cell = owner[i]["cell"]
        got = [(t, False) for c, t in segments(lines[i])] if kinds[i][0] == "value" else inline.get(i, [])
        if got:
            pieces.setdefault(id(cell), (cell, []))[1].append((i, got))
    for cell, lines_got in pieces.values():
        real = any(not glued for i, got in lines_got for t, glued in got)
        for i, got in lines_got:
            texts = [t for t, glued in got if not (real and glued)]
            if not texts:
                continue
            cell["value"] = clean_value(cell["value"] + " " + " ".join(texts))
            if not cell["numbers"]:
                cell["numbers"] = number_tokens(" ".join(texts))   # [(value, is_pct)] of the first numeric line
            if i not in cell["lines"]:
                cell["lines"].append(i)
    return found, value_col


def clean_value(text):
    return RIGHT_WORDS.sub(" ", RIGHT_WORDS.sub(" ", squash(text))).strip(" :,/|-")


MIN_VALUE_COL = 25
RIGHT_WORDS = re.compile(r"\(tc\)|\bbelongs?\b(?:\s+to\b)?|^\s*to\b|^\s*(?:a[ce]quirers?|sellers?)\s*,?\s*$|promoter\s*/\s*promoter\s+group|as\s+follows|under\s+consideration|"
                         r"holding\s+of\s*:?|\(e\.g\.[^)]*\)?\.?|whichever\s+is\s+applicable|^[\s:,/|-]+|[\s:,/|-]+$", re.I)
MODE_LIST = re.compile(r"(?:open\s+market|off-?\s?market|public\s+issue|rights\s+issue|preferential\s+allotment|inter-?se\s+transfer|"
                       r"issue|allotment|transfer|market|sale|rights|public|inter-?se|preferential)\s*/|^\s*/\s*(?:sale|public|rights)?\s*", re.I)
GLUED_WORDS = re.compile(r"\band\b|\bpersons?\b|acting\s+in\s+concert|\bacting\s+in\b|\bin\s+concert\b|\bconcert\b|\(pac\)|with\s+the\s+a[ce]quirers?|"
                         r"public\s+issue|rights\s+issue|preferential\s+allotment|inter-?se\s+transfer|\betc\.?\)?|"
                         r"of\s+shares\s*/\s*vr|or\s+date\s+of\s+receipt|of\s+intimation\s+of\s+allotment|"
                         r"specify\s+holding\s+in\s+each\s+category\)?|a[ce]quired\s*/\s*sold", re.I)


def _right_columns(line, m):
    segs = segments(line)
    own = next((k for k, (c, t) in enumerate(segs) if c <= m.start() < c + len(t)), None)
    return [] if own is None else [c for c, t in segs[own + 1:] if c >= m.end()]


def _right_segments(line, m):
    """The segments to the right of the one holding the label match, the form's own words taken off."""
    segs = segments(line)
    own = next((k for k, (c, t) in enumerate(segs) if c <= m.start() < c + len(t)), None)
    if own is None:
        return []
    out = []
    for c, t in segs[own + 1:]:
        if c < m.end():
            continue
        t = clean_value(t)
        if re.search(r"[A-Za-z0-9]", t):
            out.append((t, False))
    return out


def _glued_value(line, col):
    """Text from `col` on, started at a word boundary, with the form's own wording taken off: the value
    an OCR layer glued to its label with a single space."""
    if len(line.rstrip()) <= col:
        return []
    while 0 < col < len(line) and line[col - 1] != " ":
        col += 1
    tail = clean_value(GLUED_WORDS.sub(" ", MODE_LIST.sub(" ", RIGHT_WORDS.sub(" ", line[col:]))))
    return [(tail, True)] if re.search(r"[A-Za-z0-9]", tail) else []


def _assign_value_lines(kinds, blocks):
    """Which block each value-only line belongs to: the block whose rows surround it, then symmetric
    growth one line above and below each block, then the nearest block (the one above on a tie)."""
    owner = {}
    for b in blocks:
        for r in b["rows"]:
            owner[r] = b
        lo, hi = b["rows"][0], b["rows"][-1]
        for r in range(lo, hi + 1):
            if kinds[r][0] == "value" and r not in owner:
                owner[r] = b
    for b in blocks:
        lo, hi, m = b["rows"][0], b["rows"][-1], 1
        while (lo - m >= 0 and hi + m < len(kinds) and kinds[lo - m][0] == "value" and kinds[hi + m][0] == "value"
               and (lo - m) not in owner and (hi + m) not in owner):
            owner[lo - m] = owner[hi + m] = b
            m += 1
    for i, k in enumerate(kinds):
        if k[0] == "value" and i not in owner and blocks:
            def distance(b):
                rows = b["rows"] + [r for r, o in owner.items() if o is b]
                return (min(abs(i - r) for r in rows), 0 if b["rows"][0] < i else 1)
            owner[i] = min(blocks, key=distance)
    return owner


def _cell_for(found, key, line):
    v = found[key]
    if isinstance(v, list):
        return next(c for c in v if c["line"] == line)
    return v


def _rx(pattern):
    return re.compile(pattern, re.I)


_ROW = r"^\s*(?:\(?[a-f@©¢]\)?|i{1,3}\)|\(?i{1,3}\))?\s*"
_MARKED = r"^\s*(?:\(?[a-f@©¢]\)|\(?[a-f]\)|i{1,3}\))\s*"
SAST29_LABELS = [
    ("target", _rx(r"name of the target company"), False),
    ("acquirer", _rx(r"name\(?s?\)? of the\s+(?:a[ce]quirers?|sellers?)"), False),
    ("promoter", _rx(r"\bwhether\s+the\s+(?:a[ce]quirers?|sellers?)"), False),
    ("exchanges", _rx(r"name\(?s?\)? of the stock exchange"), False),
    ("table", _rx(r"details of the a[ce]quisition\s*/?\s*(?:disposal)?\s*as\b"), False),
    ("before", _rx(r"^\s*before\s+the\s+(?:a[ce]quisition|disposal|sale)"), False),
    ("deal", _rx(r"^\s*details of (?:a[ce]quisition|sale|disposal)"), False),
    ("after", _rx(r"^\s*after\s+the\s+(?:a[ce]quisition|disposal|sale)"), False),
    ("mode", _rx(r"^\s*mode of (?:a[ce]quisition|sale|disposal)"), False),
    ("salient", _rx(r"salient features"), False),
    ("date", _rx(r"^\s*date of (?:a[ce]quisition|sale|disposal)"), False),
    ("capital", _rx(r"^\s*equity share capital|^\s*total voting capital"), True),
    ("diluted", _rx(r"^\s*total diluted share"), False),
    ("note", _rx(r"^\s*\(\*.*$|^\s*note\s*:.*$"), False),
    ("shares", _rx(_ROW + r"shares\s+carrying\s+voting\s+rights"), True),
    ("encumbered", _rx(_MARKED + r"shares\s+(?:in\s+the\s+nature\s+of\s+encumbrance|encumbered)"), True),
    ("vr", _rx(_MARKED + r"(?:voting\s+rights\s*\(vr\)|vrs?\s+(?:acquired|otherwise))"), True),
    ("warrants", _rx(_MARKED + r"warrants\s*/\s*convertible"), True),
    ("total", _rx(r"total\s*\(\s*a\s*\+"), True),
]
SAST29_SECTIONS = ("before", "deal", "after")
ACQUIRER_WORDS = _rx(r"^\s*(?:and\s+)?persons?\s+acting\s+in\s+concert\b|^\s*acting\s+in\s+concert\b|^\s*\(pac\)|"
                     r"^\s*with\s+the\s+(?:a[ce]quirers?\b)?|^\s*and[-\s]ps\b|^\s*acting\b|^\s*acquirers?\b|\|")
SECURITY_OF = {"shares": "Equity Shares", "warrants": "Warrants / convertible securities",
               "encumbered": "Encumbered shares", "total": "Equity Shares"}


def _section_of(cell, cells):
    """Which of before / deal / after a numeric row belongs to: the nearest section header above it."""
    best = None
    for name in SAST29_SECTIONS:
        head = cells.get(name)
        if head and head["line"] < cell["line"] and (best is None or head["line"] > cells[best]["line"]):
            best = name
    return best


def _rows_by_section(cells):
    out = {s: {} for s in SAST29_SECTIONS}
    for key in ("shares", "encumbered", "vr", "warrants", "total"):
        for cell in cells.get(key, []):
            s = _section_of(cell, cells)
            if s and key not in out[s]:
                out[s][key] = cell
    return out


def _holding(section, header):
    """(number, percent, the row that carried the number) for one section of the SAST 29 table.
    The total row is preferred (it is the one filled in when the shares row is a dash); pdftotext
    sometimes puts row a)'s numbers on the section's header line, so that line counts as row a)."""
    rows = {k: list(c.get("numbers") or []) for k, c in section.items()}
    if not rows.get("shares") and header and header.get("numbers"):
        rows["shares"] = list(header["numbers"])
    which = next((k for k in ("shares", "warrants", "encumbered") if count_and_pct(rows.get(k, []))[0] is not None), None)
    qty, pct = count_and_pct(rows.get("total") or [])
    if qty is None:
        qty, pct = count_and_pct(rows.get(which or "", []))
    return qty, pct, which or ("total" if qty is not None else None)


def parse_sast29(text):
    """Facts of a SEBI SAST Regulation 29(1)/(2) disclosure (one acquirer or seller, one transaction)."""
    lines = strip_furniture(text.splitlines())
    head = next((i for i, l in enumerate(lines) if SAST29_LABELS[0][1].search(l)), None)
    if head is None:
        return {"form": "sast-29", "rows": [], "problem": "no Regulation 29 table found in the text"}
    cells, _ = label_cells(lines[head:], SAST29_LABELS)
    sections = _rows_by_section(cells)
    prior_qty, prior_pct, _ = _holding(sections["before"], cells.get("before"))
    deal_qty, deal_pct, which = _holding(sections["deal"], cells.get("deal"))
    post_qty, post_pct, _ = _holding(sections["after"], cells.get("after"))
    acquirer = _acquirer(cells.get("acquirer", {}).get("value", ""))
    mode = mode_phrase(cells.get("mode", {}).get("value", ""))
    date_text = cells.get("date", {}).get("value", "")
    dates = find_dates(date_text)
    unread = [name for name, ok in (("acquirer", acquirer), ("shares acquired or sold", deal_qty is not None),
                                    ("holding after", post_qty is not None), ("mode", mode),
                                    ("date of the trade", dates)) if not ok]
    row = {
        "form": "sast-29", "regulation": "SAST 29", "company": squash(cells["target"]["value"]) or None,
        "person": acquirer, "category_raw": squash(cells.get("promoter", {}).get("value", "")) or None,
        "side_raw": _sast_side(prior_qty, post_qty, cells, mode, which), "security": SECURITY_OF.get(which),
        "qty": deal_qty, "pct": deal_pct, "value": None,
        "qty_prior": prior_qty, "pct_prior": prior_pct, "qty_post": post_qty, "pct_post": post_pct,
        "mode_raw": mode or None, "trade_from": dates[0] if dates else None, "trade_to": _trade_to(date_text, dates),
        "intimated": letter_date(lines), "exchange": exchanges(cells.get("exchanges", {}).get("value", "")),
        "remark": None, "derivative": None, "unread": unread,
    }
    return {"form": "sast-29", "rows": [row], "problem": None}


MODE_PHRASE = _rx(r"(open\s+market(?:\s+(?:sale|purchase))?|off-?\s?market(?:\s+(?:sale|purchase))?[^/]*?(?=\s*/|\s*$)|"
                  r"market\s+(?:sale|purchase)|public\s+issue|rights\s+issue|preferential\s+allotment|inter-?se\s+transfer|"
                  r"gift|esops?|block\s+deal|bulk\s+deal|conversion[^/]*?(?=\s*/|\s*$)|buy-?back|open\s+offer|"
                  r"invocation[^/]*?(?=\s*/|\s*$)|pledge[^/]*?(?=\s*/|\s*$)|allotment[^/]*?(?=\s*/|\s*$))")


def mode_phrase(value):
    """The mode named in the cell: the first known phrase that is not part of the form's own list
    (those are each followed by a slash), else the cleaned text."""
    text = clean_value(MODE_LIST.sub(" ", clean_value(value)))
    for m in MODE_PHRASE.finditer(text):
        if not re.match(r"\s*/", text[m.end():]):
            return m.group(1).strip()
    return text


def _acquirer(value):
    s = squash(value)
    for _ in range(3):
        s = squash(ACQUIRER_WORDS.sub(" ", s))
    s = re.sub(r"^[\s|:,-]*(?:sellers?|acquirers?)\s*\(?s?\)?\s*:?\s*", "", squash(s), flags=re.I)
    s = re.sub(r"(?<![\w.])[il|](?![\w.])", " ", s)          # OCR specks
    s = re.sub(r"\s*\b\d+\.\s+", "; ", " " + s).strip("; ")    # "1. A Ltd 2. Mrs B" -> "A Ltd; Mrs B"
    return squash(s.strip(" |:,-")) or None


def _sast_side(prior, post, cells, mode, which):
    if prior is not None and post is not None and prior != post:
        return "Acquisition" if post > prior else "Sale"
    if which == "encumbered":
        return "Pledge"
    words = " ".join([mode] + [cells.get(k, {}).get("label", "") for k in ("deal", "date", "before")]).lower()
    if re.search(r"\bsale\b|\bsold\b|dispos", words):
        return "Sale"
    if re.search(r"acqui|purchas|allot", words):
        return "Acquisition"
    return None


DATE_RANGE = _rx(r"(\d{1,2}[-/.]\d{1,2}[-/.]\d{4}|[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9},?\s+\d{4})"
                 r"\s*(?:to|till|upto|until|-|and)\s*(\d{1,2}[-/.]\d{1,2}[-/.]\d{4}|[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9},?\s+\d{4})")


def _trade_to(value, dates):
    """The second date of 'from X to Y' when the cell gives a range; a note that mentions another date is not one."""
    if not dates:
        return None
    m = DATE_RANGE.search(squash(value))
    return find_dates(m.group(2))[0] if m and find_dates(m.group(2)) else dates[0]


DATE_LINE = _rx(r"^\s*dated?\s*[:.-]?\s*(?:\d{1,2}[-/.]\d{1,2}[-/.]\d{4}|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})")
STAMP = re.compile(r"\d{4}\.\d{2}\.\d{2}")      # a digital-signature time stamp, not a date of anything


def letter_date(lines):
    """The date the disclosure was signed: the last Date: line, else the first date in the letter head."""
    for l in reversed(lines):
        if DATE_LINE.search(l) and not STAMP.search(l):
            d = find_dates(l)
            if d:
                return d[-1]
    for l in lines[:30]:
        d = find_dates(l) if not STAMP.search(l) else []
        if d:
            return d[0]
    return None


def exchanges(value):
    """'BSE Limited and National Stock Exchange of India Limited' -> 'BSE, NSE'; unknown text is kept."""
    low = (value or "").lower()
    out = [name for name, rx in (("BSE", r"\bbse\b|bombay stock"), ("NSE", r"\bnse\b|national stock|stock exchange of india"),
                                 ("CSE", r"calcutta|\bcse\b"), ("MSE", r"metropolitan|\bmsei?\b")) if re.search(rx, low)]
    return ", ".join(out) if out else (squash(value) or None)


# ---------------------------------------------------------------- column tables (Regulation 31, Form C)

def column_starts(header_lines):
    """Start columns of a table's columns from where its header words begin (wrapped header
    lines start at their column), clustered within CLUSTER characters."""
    out = []
    for c in sorted({c for line in header_lines for c, t in segments(line)}):
        if not out or c - out[-1] > CLUSTER:
            out.append(c)
    return out


SNAP = 4


def _snap(line, col):
    """A column cut that lands inside a word moves to the nearest word start within SNAP characters."""
    if col <= 0 or col >= len(line) or line[col - 1] == " " or line[col] == " ":
        return col
    for d in range(1, SNAP + 1):
        for c in (col - d, col + d):
            if 0 < c < len(line) and line[c - 1] == " " and line[c] != " ":
                return c
    return col


def split_cells(line, starts):
    cuts = [_snap(line, c) for c in starts]
    return [line[a:b].strip() for a, b in zip(cuts, cuts[1:] + [None])]


def column_headers(header_lines, starts):
    return [squash(" ".join(split_cells(l, starts)[k] for l in header_lines)) for k in range(len(starts))]


def _merge(a, b):
    return [squash(x + " " + y) for x, y in zip(a, b)]


def table_records(data_lines, starts, is_start):
    """Group sliced lines into records: a line where `is_start(cells)` holds opens one; the others
    continue the open record (lines before the first start are merged into it)."""
    records, pending = [], None
    for line in data_lines:
        if not line.strip():
            continue
        cells = split_cells(line, starts)
        if is_start(cells):
            records.append(_merge(pending, cells) if pending else cells)
            pending = None
        elif records:
            records[-1] = _merge(records[-1], cells)
        else:
            pending = _merge(pending, cells) if pending else cells
    return records


def table_end(lines, start, stop_rx, blank_run=3):
    """Index of the first line after `start` that ends a table: a stop pattern or a run of blank lines."""
    blanks = 0
    for i in range(start, len(lines)):
        if not lines[i].strip():
            blanks += 1
            if blanks >= blank_run:
                return i - blanks + 1
            continue
        blanks = 0
        if stop_rx.search(lines[i]):
            return i
    return len(lines)


# ---------------------------------------------------------------- Regulation 31 (Annexure I, encumbrance)

SAST31_HEAD = _rx(r"promoter\s+holding|details of events|post event|type of event")
SAST31_STOP = _rx(r"^\s*(?:annexure|place\s*:|date\s*:|signature|note\s*:|\*|for\s+\w)")
SAST31_LABELS = [
    ("target", _rx(r"name of the target company|name of (?:the )?listed company"), False),
    ("exchanges", _rx(r"names? of the (?:recognised )?stock exchanges?"), False),
    ("reported", _rx(r"^\s*date of reporting"), False),
    ("promoter", _rx(r"name of the promoter(?:\(s\))? or pac|name of the promoter\(s\)\s*/\s*pacs? whose"), False),
    ("created", _rx(r"^\s*date of (?:creation|release|invocation|revocation) of encumbrance"), False),
]
EVENT_RX = re.compile(r"(?<![(/\w])(%s)(?![\w/)])(?!\s*/)(?!\s+of\b)" % EVENT_WORDS, re.I)


def parse_sast31(text):
    """Facts of a SEBI SAST Regulation 31 disclosure (Annexure I): one row per encumbrance event."""
    lines = strip_furniture(text.splitlines())
    first_label = next((i for i, l in enumerate(lines) if SAST31_LABELS[0][1].search(l)), 0)
    cells, _ = label_cells(lines[first_label:], SAST31_LABELS)
    lines = lines[first_label:]
    head = next((i for i, l in enumerate(lines) if re.match(r"^\s*name\s+of\b", l, re.I) and len(segments(l)) >= 3
                 and any(SAST31_HEAD.search(lines[j]) for j in range(i, min(i + 3, len(lines))))), None)
    if head is None:
        return {"form": "sast-31", "rows": [], "problem": "no Regulation 31 events table found in the text"}
    first = next((i for i in range(head, len(lines)) if EVENT_RX.search(lines[i])
                  and not re.search(r"type of event|\(creation", lines[i], re.I)), None)
    if first is None:
        return {"form": "sast-31", "rows": [], "problem": "no creation / release / invocation row in the Regulation 31 table"}
    header = [l for l in lines[head:first] if l.strip()]
    starts = column_starts(header)
    fields = _sast31_fields(column_headers(header, starts))
    event_col = fields.get("event")
    records = table_records(lines[first:table_end(lines, first, SAST31_STOP)], starts,
                            lambda c: event_col is not None and bool(EVENT_RX.search(c[event_col])))
    context = {"company": squash(cells.get("target", {}).get("value", "")) or None,
               "who": squash(cells.get("promoter", {}).get("value", "")) or None,
               "reported": find_dates(cells.get("reported", {}).get("value", "")),
               "created": find_dates(cells.get("created", {}).get("value", "")),
               "exchange": exchanges(cells.get("exchanges", {}).get("value", "")),
               "letter": letter_date(lines)}
    rows = [_sast31_row(rec, fields, context, k) for k, rec in enumerate(records)]
    if not rows:
        return {"form": "sast-31", "rows": [], "problem": "the Regulation 31 events table has no row the reader understands"}
    return {"form": "sast-31", "rows": rows, "problem": None}


def _sast31_row(rec, fields, ctx, k):
    g = lambda f: rec[fields[f]] if f in fields and fields[f] < len(rec) else ""
    nums = lambda f: (numbers(g(f)) or [None])[0]
    event = (EVENT_RX.search(g("event")) or EVENT_RX.search(" ".join(rec))).group(1).lower()
    kind = squash(g("enc_type")) or "encumbrance"
    qty, hold, hold_pct = nums("event_qty"), nums("hold_qty"), nums("hold_pct")
    post_enc, post_pct = nums("post_qty"), nums("post_pct")
    dates = find_dates(g("date")) if "date" in fields else find_dates(" ".join(rec))
    if not dates and k < len(ctx["created"]):
        dates = [ctx["created"][k]]          # Annexure II lists the events' dates in the same order
    lender, reason = squash(g("lender")) or None, squash(g("reason")) or None
    bits = []
    if lender:
        bits.append("%s with %s" % ("pledged" if kind.lower().startswith("pledge") else kind.lower(), lender))
    if post_enc is not None:
        bits.append("%s%s of the holding encumbered after the event" % (
            "{:,}".format(int(post_enc)), " (%.2f %%)" % post_pct if post_pct is not None else ""))
    if reason:
        bits.append(reason.rstrip("."))
    unread = [name for name, ok in (("shares encumbered", qty is not None), ("date of the event", dates)) if not ok]
    return {
        "form": "sast-31", "regulation": "SAST 31", "company": ctx["company"],
        "person": squash(g("name")) or ctx["who"], "category_raw": "Promoter (Regulation 31)",
        "side_raw": "%s %s" % (kind.split("/")[0].strip().capitalize(), event), "security": "Equity Shares",
        "qty": qty, "pct": nums("event_pct"), "value": None,
        "qty_prior": hold, "pct_prior": hold_pct, "qty_post": hold, "pct_post": hold_pct,
        "mode_raw": kind, "trade_from": dates[0] if dates else None, "trade_to": dates[-1] if dates else None,
        "intimated": ctx["reported"][0] if ctx["reported"] else ctx["letter"], "exchange": ctx["exchange"],
        "remark": "; ".join(bits) or None, "derivative": None, "unread": unread,
        "event": event, "lender": lender, "encumbered_post": post_enc, "encumbered_post_pct": post_pct,
    }


def _sast31_fields(names):
    """Column index per field from the header words: the four Number / % pairs in order are the
    promoter holding, the part already encumbered, the event, and the post-event encumbrance."""
    fields, pairs = {}, []
    for k, h in enumerate(names):
        low = h.lower()
        if k == 0 or re.match(r"name of (?:the )?promoter", low):
            fields.setdefault("name", k)
        elif re.search(r"type of event", low):
            fields["event"] = k
        elif re.search(r"^date of", low):
            fields["date"] = k
        elif re.search(r"type of encumbrance", low):
            fields["enc_type"] = k
        elif re.search(r"reasons?", low):
            fields["reason"] = k
        elif re.search(r"name of the entity|whose fav", low):
            fields["lender"] = k
        elif re.search(r"\bnumber\b|^no\.", low):
            pairs.append([k, None])
        elif "%" in low and pairs and pairs[-1][1] is None:
            pairs[-1][1] = k
    for name, pair in zip(("hold", "already", "event", "post"), pairs):
        fields[name + "_qty"] = pair[0]
        if pair[1] is not None:
            fields[name + "_pct"] = pair[1]
    return fields


# ---------------------------------------------------------------- Form C (PIT Regulation 7(2))

FORMC_HEAD = _rx(r"^\s*name\b.*\bcategory\b|^\s*name,?\s*pan")
FORMC_STOP = _rx(r"^\s*(?:details of trading in derivatives|note\s*:|name\s*&|signature|date\s*:|place\s*:|designation\s*:)")
DERIV_HEAD = _rx(r"details of trading in derivatives")
DERIV_STOP = _rx(r"^\s*(?:note\s*:|name\s*&|signature|date\s*:|place\s*:|designation\s*:)")
DERIV_WORDS = _rx(r"type of contract|specifications|notional|contracts\s*\*|lot size|\bbuy\b|\bsell\b|size\)|^\s*(?:\d+\s+){3,}")
NIL = _rx(r"\s*(?:nil|na|n\.a\.?|-+|none|not applicable)?\s*")
TRANSACTION = _rx(r"^(?:buy|purchase|sale|sell|pledge|revoke|revocation|invoke|invocation|acquisition|disposal|allotment|gift|esop)")
GROUPS = [("post", _rx(r"held post|post acquisition")), ("prior", _rx(r"held prior|prior to")),
          ("deal", _rx(r"acquired|disposed")), ("dates", _rx(r"date of allotment|allotment advice|acquisition of shares"))]


def parse_formc(text):
    """Facts of a PIT Regulation 7(2) Form C: one row per person and transaction, plus the derivatives block."""
    lines = strip_furniture(text.splitlines())
    head = next((i for i, l in enumerate(lines) if FORMC_HEAD.search(l)), None)
    if head is None:
        return {"form": "pit-7", "rows": [], "problem": "no Form C table (name / category / securities held) found in the text"}
    numbered = next((i for i in range(head, min(head + 40, len(lines))) if re.match(r"^\s*1\s+2\s+3\s+4\b", lines[i])), None)
    if numbered is not None:
        header, first = [l for l in lines[head:numbered] if l.strip()], numbered + 1
    else:
        first = next((i for i in range(head + 1, len(lines)) if _looks_like_formc_row(lines[i])), None)
        if first is None:
            return {"form": "pit-7", "rows": [], "problem": "a Form C header but no data row under it"}
        header = [l for l in lines[head:first] if l.strip()]
    starts = column_starts(header)
    names = column_headers(header, starts)
    fields = _formc_fields(names, starts, header)
    if "name" not in fields or not any(k in fields for k in ("deal_qty", "value", "side")):
        return {"form": "pit-7", "rows": [], "problem": "Form C columns not recognised: %s" % "; ".join(n[:30] for n in names if n)}
    end = table_end(lines, first, FORMC_STOP)
    side_col, cat_col = fields.get("side"), fields.get("category")

    def starts_record(cells):
        if side_col is not None:
            return bool(cells[side_col]) and bool(TRANSACTION.search(cells[side_col]))
        return cat_col is not None and bool(cells[cat_col]) and bool(cells[fields["name"]])

    records = table_records(lines[first:end], starts, starts_record)
    context = {"company": _formc_company(lines[:head]), "derivative": _derivatives(lines, end)}
    rows = [_formc_row(rec, fields, context) for rec in records]
    if not rows:
        return {"form": "pit-7", "rows": [], "problem": "the Form C table has no row the reader understands"}
    return {"form": "pit-7", "rows": rows, "problem": None}


def _formc_row(rec, fields, ctx):
    g = lambda f: rec[fields[f]] if f in fields and fields[f] < len(rec) else ""
    prior_qty, prior_pct = _qty_pct(g("prior_qty"), g("prior_pct"))
    post_qty, post_pct = _qty_pct(g("post_qty"), g("post_pct"))
    deal_qty, _ = _qty_pct(g("deal_qty"), "")
    value = None if NIL.fullmatch(g("value")) else (numbers(g("value")) or [None])[0]
    side = squash(g("side")) or None
    frm = find_dates(g("from")) or find_dates(g("dates"))
    to = find_dates(g("to"))
    intimated = find_dates(g("intimated"))
    unread = [name for name, ok in (("number and value of securities", deal_qty is not None or value is not None),
                                    ("transaction type", side), ("date of the trade", frm or to)) if not ok]
    return {
        "form": "pit-7", "regulation": "7(2)", "company": ctx["company"], "person": _person(g("name")),
        "category_raw": squash(g("category")) or None, "side_raw": side,
        "security": squash(g("deal_type")) or squash(g("prior_type")) or None,
        "qty": deal_qty, "pct": None, "value": value,
        "qty_prior": prior_qty, "pct_prior": prior_pct, "qty_post": post_qty, "pct_post": post_pct,
        "mode_raw": squash(g("mode")) or None,
        "trade_from": (frm or to or [None])[0], "trade_to": (to or frm or [None])[-1],
        "intimated": intimated[0] if intimated else None, "exchange": squash(g("exchange")) or None,
        "remark": None, "derivative": ctx["derivative"], "unread": unread,
    }


def _looks_like_formc_row(line):
    return bool(re.search(r"\s{2,}(?:buy|purchase|sale|sell|pledge|revoke|invoke)\s{2,}", line, re.I))


def _formc_fields(names, starts, header):
    """Column index per field. The groups (held prior / acquired or disposed / held post / the dates)
    are the titles on the top header line, each spanning the sub-columns up to the next title;
    a sub-column's own words (type of, no., %, value, transaction, from, to) say which field it is."""
    anchors = []
    for line in header[:3]:
        for name, rx in GROUPS:
            m = rx.search(line)
            if m and name not in [n for n, c in anchors]:
                anchors.append((name, m.start()))
    anchors.sort(key=lambda a: a[1])
    fields = {"name": 0}
    for k, h in enumerate(names):
        low = h.lower()
        if k == 0:
            continue
        if "category" in low and "category" not in fields:
            fields["category"] = k
            continue
        group = next((n for n, c in reversed(anchors) if c <= starts[k] + CLUSTER), None)
        if re.search(r"intimation", low):
            fields["intimated"] = k
        elif re.search(r"^mode|mode of", low):
            fields["mode"] = k
        elif re.search(r"^exchange|exchange on", low):
            fields["exchange"] = k
        elif group == "dates":
            if re.search(r"\bfrom\b", low):
                fields["from"] = k
            elif re.search(r"\bto\b(?!\s+company)", low) and "from" in fields:
                fields["to"] = k
            else:
                fields.setdefault("dates", k)
        elif group:
            if re.search(r"type of", low):
                fields[group + "_type"] = k
            elif re.search(r"value", low):
                fields["value"] = k
            elif re.search(r"transaction", low):
                fields["side"] = k
            elif re.search(r"\bno\.?\b|number", low):
                fields[group + "_qty"] = k
            elif "%" in low:
                fields[group + "_pct"] = k
    return fields


def _qty_pct(qty_text, pct_text):
    """'12,50,000 (4.17%)' or separate cells -> (12500000, 4.17)."""
    qty, pct = count_and_pct(number_tokens(qty_text))
    if pct is None:
        pct = (numbers(pct_text) or [None])[0]
    return qty, pct


def _person(text):
    s = squash(text)
    s = re.split(r"\b(?:PAN|DIN|CIN|Address)\b\s*[:.-]?", s, maxsplit=1, flags=re.I)[0]
    s = re.split(r"\s(?=\d{1,4}[,/ ]\s*\w)", s, maxsplit=1)[0]     # a street address starts with a number
    return squash(s) or None


def _formc_company(lines):
    for l in lines:
        m = re.search(r"name of the company\s*[:.-]?\s*(.+)$", l, re.I)
        if m and squash(m.group(1)):
            return squash(m.group(1))
    return None


def _derivatives(lines, start):
    """The Form C derivatives block as a dict, or None when it is Nil / NA / absent."""
    head = next((i for i in range(start, len(lines)) if DERIV_HEAD.search(lines[i])), None)
    if head is None:
        return None
    first = next((i for i in range(head + 1, len(lines)) if lines[i].strip() and not DERIV_WORDS.search(lines[i])), None)
    if first is None:
        return None
    header = [l for l in lines[head + 1:first] if l.strip() and not re.match(r"^\s*(?:\d+\s+){3,}", l)]
    starts = column_starts(header)
    names = column_headers(header, starts)
    data = [split_cells(l, starts) for l in lines[first:table_end(lines, first, DERIV_STOP, blank_run=2)] if l.strip()]
    cells = [squash(" ".join(col)) for col in zip(*data)] if data else []
    if not cells or all(re.fullmatch(r"nil|na|n\.a\.?|-+|not|applicable|none", w, re.I) for c in cells for w in c.split()):
        return None
    out = {"type": None, "contract": None, "notional_buy_inr": None, "units_buy": None,
           "notional_sell_inr": None, "units_sell": None}
    side = None
    for h, c in zip(names, cells):
        low = h.lower()
        side = "sell" if "sell" in low else ("buy" if "buy" in low else side)
        if "exchange" in low:
            continue
        if "type of contract" in low:
            out["type"] = c or None
        elif "specification" in low:
            out["contract"] = c or None
        elif "notional" in low and side:
            out["notional_%s_inr" % side] = None if NIL.fullmatch(c) else (numbers(c) or [None])[0]
        elif "units" in low and side:
            n = None if NIL.fullmatch(c) else (numbers(c) or [None])[0]
            out["units_%s" % side] = int(n) if n is not None else None
    return out


# ---------------------------------------------------------------- dispatch

READERS = {"sast-29": parse_sast29, "sast-31": parse_sast31, "pit-7": parse_formc}


def classify(text, hint=None):
    """Which layout the text carries, from its own words; the feed's description breaks a tie."""
    low = (text or "").lower()
    if re.search(r"\bform\s*c\b|regulation\s*7\s*\(\s*2\s*\)|prohibition of insider trading|continual disclosure", low):
        return "pit-7"
    if re.search(r"regulation\s*31\s*\(|creation of encumbrance|release of encumbrance|invocation of encumbrance|"
                 r"encumbrance has been created|disclosure of reasons for encumbrance", low):
        return "sast-31"
    if re.search(r"regulation\s*\(?29\s*\)?\s*\(|substantial acquisition|target company", low):
        return "sast-29"
    return hint


def hint_from(description):
    low = (description or "").lower()
    if re.search(r"regulation 31|pledg|encumb", low):
        return "sast-31"
    if re.search(r"regulation 29|sast", low):
        return "sast-29"
    if re.search(r"regulation 7|insider", low):
        return "pit-7"
    return None


def read_disclosure(text, hint=None):
    """{"form", "rows": [facts], "problem"}: the facts of whichever disclosure layout the text carries."""
    form = classify(text, hint)
    if form is None:
        return {"form": None, "rows": [], "problem": "not a Regulation 29 / 31 / 7(2) disclosure the reader knows"}
    result = READERS[form](text)
    if result["problem"] and hint in READERS and hint != form:
        other = READERS[hint](text)
        if other["rows"]:
            return other
    return result
