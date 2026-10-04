"""
Find out what the orders with no body actually are, and stop calling them scans.

The corpus has 5,562 records with no order_text, and the interface explains
them in terms of our pipeline's failure — "scanned document", "awaiting
review". For most of them that is untrue. This is the census that establishes
what they really are: the documents were read, and the great majority contain
no order because the government did not publish one.

A page bearing the single word "Confidential" is not a failed scan. It is the
State of Andhra Pradesh declining to publish the order, and describing that as
a technical defect misrepresents the record to anyone trying to establish
whether an order exists at all.

HOW A DOCUMENT IS JUDGED
-------------------------
On its whole content, never on one page. A cover sheet stamped CONFIDENTIAL in
front of a real order is a different thing from a one-page document whose only
word is Confidential, and only the second is a withholding. So a record is
classified only when *all* of its text, across every page, reduces to the
placeholder, and when no page carries meaningful ink.

The ink test is what stops a scanned order bearing a confidential stamp from
being written off as empty, and its threshold is measured rather than guessed:
pages of real orders in this corpus run 0.025-0.146 dark fraction at 50 dpi,
while the blank pages run 0.000-0.003. INK_BLANK sits in the gap, five times
above the inkiest blank page and well below the lightest real order.

The same test cuts the other way and finds work we owe. A page with ink on it
and no text to speak of is a photograph of an order nobody has read yet: the
clearest case is an eight-page General Administration order from 2016 whose
entire text layer is the eOffice routing stamp "9023694/2024/SKILLS-SDE&I".
Those become scanned_needs_ocr, the one verdict here that is a task rather
than a description.

Spelling is not assumed. The corpus contains CONFIDENTIONAL, CONFIDENTAIL and
CONFIDENTIALLY, all typed by a clerk meaning the same thing, so the match is
by similarity to the word rather than equality with it — after the letterhead
and citation tokens a placeholder page often carries are set aside.

EVERY FILE IS READ AS WHAT IT IS, NOT AS WHAT IT IS NAMED
----------------------------------------------------------
Each file in this corpus is named .pdf and only some of them are one. The
reader is chosen by magic bytes: PyMuPDF for real PDFs, textutil for OLE2 Word
and RTF, and a direct read of word/document.xml for .docx. That last case is
the one that was being missed — PyMuPDF opens a .docx without complaint and
reports a single blank page, so 1,231 documents were filed as scans that OCR
found nothing on when in truth nothing had ever read them.

Nothing here runs OCR or calls a model: this is a question about what a
document says, answered by reading it.

WHY PROCESSES AND NOT THREADS
------------------------------
PyMuPDF's renderer is not thread-safe. An earlier run of this census used a
thread pool and 1,265 perfectly readable PDFs came back as "unreadable" — a
silent wrong answer, not a crash, which would have mislabelled them. Each
worker gets its own process and its own MuPDF context.

Writes nothing to the database. Produces out/no_text_census.json for
push_no_text_classification.py to act on.

Usage:
    python3 classify_no_text.py
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import zipfile
import tempfile
from collections import Counter
from difflib import SequenceMatcher
from html import unescape
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor

import fitz

from rebuild_search_index import dsn

OUT_FILE = Path("out/no_text_census.json")
OLE2 = bytes.fromhex("d0cf11e0a1b11ae1")

# Measured, not guessed — see the module docstring.
INK_BLANK = 0.015

# Tokens a placeholder page carries besides the word itself: the departmental
# letterhead, the ABSTRACT rubric, and the G.O.'s own citation. None of them is
# content, so they are set aside before asking what the document says.
BOILERPLATE = {
    "government", "of", "andhra", "pradesh", "andhrapradesh", "abstract",
    "the", "department", "dept", "go", "g", "o", "ms", "rt", "no", "nos",
    "date", "dated", "sub", "subject", "from", "to", "copy", "order", "orders",
    # "strictly confidential" is the same withholding as "confidential".
    "strictly",
    # A .docx whose page-number field never rendered leaves the literal word
    # PAGE behind. It is a Word artifact, not something anyone wrote.
    "page",
}

CONFIDENTIAL_SIMILARITY = 0.80

# The shortest thing that could hold an operative order. Nothing shorter is
# sent on for order-block bounding; nothing longer is declared orderless by
# rule here. Deliberately generous: the 677 recoverable records have a p10 of
# 265 characters, so a document near this line gets read by the boundary pass
# rather than written off, and that pass can say there is no order in it.
MIN_ORDER_CHARS = 200


def letters(s: str) -> str:
    return re.sub(r"[^a-z]", "", s.lower())


def non_latin_letters(s: str) -> int:
    """Letters outside Latin — Telugu, or Latin text stored as Greek.

    `letters()` keeps only a-z, so without this a document written in Telugu
    reduces to the empty string and would be called empty. One record in this
    corpus is worse than that: REVENUE-MS-231/2012 is ordinary English typed in
    the Symbol typeface, so "GOVERNMENT OF ANDHRA PRADESH" was extracted as
    "ΓΟςΕΡΝΜΕΝΤ ΟΦ ΑΝΔΗΡΑ ΠΡΑΔΕΣΗ". Saying a document like that has nothing in
    it would be plainly false, so the count below keeps it out of every
    empty-or-placeholder verdict.
    """
    return sum(1 for ch in s if ch.isalpha() and ord(ch) > 0x24F)


def content_tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[A-Za-z]+", text.lower()) if t not in BOILERPLATE]


def is_confidential(text: str) -> bool:
    """Every content word is the word 'Confidential', however it was spelled."""
    toks = content_tokens(text)
    if not toks or len(toks) > 6:
        return False
    return all(
        SequenceMatcher(None, t, "confidential").ratio() >= CONFIDENTIAL_SIMILARITY
        for t in toks
    )


def read_pdf(path: Path) -> tuple[str | None, float]:
    """Full text across every page, and the inkiest page's dark fraction."""
    try:
        doc = fitz.open(path)
    except Exception:
        return None, -1.0
    try:
        text = "".join(p.get_text() for p in doc)
        worst = 0.0
        for page in doc:
            pm = page.get_pixmap(dpi=50, colorspace=fitz.csGRAY)
            buf = pm.samples
            if buf:
                worst = max(worst, sum(1 for b in buf if b < 200) / len(buf))
        return text, worst
    except Exception:
        return None, -1.0
    finally:
        doc.close()


def read_docx(path: Path) -> str | None:
    """Text of a .docx, read out of word/document.xml.

    These files matter more than their count suggests. 1,231 of them are named
    .pdf, and PyMuPDF opens one without complaint as a single blank page — so
    they were recorded as scanned documents that OCR found nothing on, when in
    fact nothing had ever read them. A silent wrong answer, which is why the
    dispatch below goes by magic bytes and not by the extension.
    """
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
    except Exception:
        return None
    # Paragraph breaks first, so words from adjacent paragraphs do not merge.
    xml = re.sub(r"</w:p\s*>", "\n", xml)
    xml = re.sub(r"<w:tab\b[^>]*/?>", "\t", xml)
    parts = re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>|\n", xml, flags=re.S)
    return unescape("".join(p if p else "\n" for p in parts))


def legacy_format(head: bytes) -> str | None:
    """Name the format of a file nothing available here can read.

    These are the last 31 records, and every one is identifiable. Naming the
    format is the whole of the answer: "Lotus WordPro" tells a reader why the
    text is missing and what would be needed to get it, where "inconclusive"
    tells them only that we gave up.

    The WordPro files are the interesting case, because the order text *is*
    visible inside them as ASCII runs and it is tempting to scrape it out. It
    must not be scraped. The runs are cut at record boundaries mid-word, and
    numbers held in separate fields drop out entirely — one of these reads
    "the 1 th July, 2009" and carries a bare "G.O.Ms.No." with no number.
    Every body in this corpus is a verbatim substring of a faithful read of
    its source, and a reconstruction that silently loses digits out of a date
    and a citation cannot meet that standard. So the format is recorded, the
    record goes to manual review, and the file is left alone.
    """
    if head[:8] == b"WordPro\x00":
        return "Lotus WordPro"
    if head[:8] == b"Men At W":  # "Men At Work, Copyright 1995" + "LEAP"
        return "LEAP Office"
    if head[:4] == b"EP*\x00":  # zlib-wrapped Windows Enhanced Metafile
        return "Windows Enhanced Metafile (print spool)"
    if head[:4] == b"L\x00\x00\x00":
        return "Windows shortcut (.lnk), not a document"
    # Added 2026-10-01 from the RT corpus, which carries formats the MS corpus
    # did not. Same principle as above: naming the format is the answer, and a
    # named format is not "inconclusive".
    if head[:4] == b"\xdb\xa5\x2d\x00":
        return "Word 6.0/95 binary"          # 338 RT files; textutil cannot read it
    if head[:4] == b"\x00\x00\x1a\x00":
        return "Lotus 1-2-3 / legacy binary"  # 24 RT files
    if head[:2] == b"MZ":
        return "Windows executable, not a document"  # 6 RT files
    return None


def read_doc(path: Path) -> str | None:
    head = path.read_bytes()[:8]
    if head[:4] == b"PK\x03\x04":
        return read_docx(path)
    ext = "doc" if head == OLE2 else "rtf" if head[:5] == b"{\\rtf" else None
    if ext is None:
        return None
    scratch = Path(tempfile.mkdtemp())
    tmp = scratch / f"d.{ext}"
    shutil.copy(path, tmp)
    try:
        r = subprocess.run(
            ["textutil", "-convert", "txt", "-stdout", str(tmp)],
            capture_output=True, timeout=60,
        )
        return r.stdout.decode("utf-8", "replace")
    except Exception:
        return None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def judge(text: str | None, ink: float) -> str:
    """ink < 0 means the file is not a PDF, so there is no page to look at."""
    if text is None:
        return "inconclusive"
    core = letters(text)
    blank = ink < 0 or ink < INK_BLANK

    # Writing in a script this function cannot reduce is still writing. Checked
    # before anything else so no amount of blankness can call it empty.
    if non_latin_letters(text) >= 100:
        return "recoverable_text"

    # Real text wins outright. Nothing below may relabel a document that has an
    # order in it, whatever words happen to appear at the top of the page.
    if len(text.strip()) >= MIN_ORDER_CHARS and not blank:
        return "recoverable_text"

    # Ink on the page and no text to speak of. The page was photographed, not
    # typed, and nothing has ever read it — the archetype is an eight-page
    # General Administration order whose entire text layer is the eOffice
    # routing stamp. This is the one verdict here that means work is owed: the
    # order exists and is legible, we simply have not run OCR over it.
    if not blank:
        return "scanned_needs_ocr"

    if not core:
        return "source_document_empty"
    if is_confidential(text):
        return "withheld_confidential"
    if re.search(r"not(allocated|allotted|issued|issue)", core) and len(core) < 120:
        return "go_not_issued"
    if re.fullmatch(r"cancell?ed\.?", core):
        return "order_cancelled"
    if core.startswith("testdoc") and len(core) < 80:
        return "portal_test_document"
    if "texttobeuploaded" in core and len(core) < 80:
        return "awaiting_upload"

    # Below here the page is blank and carries under MIN_ORDER_CHARS, so
    # whatever it says, it is not an order. Two things are worth telling apart.
    if len(text.strip()) < MIN_ORDER_CHARS:
        # A withholding that came with company — a name, a computer number, a
        # citation — which is why the all-tokens test above let it through.
        # Safe only inside this branch: a real order long enough to discuss
        # confidentiality cannot fit in 200 letters on a blank page.
        if any(
            SequenceMatcher(None, t, "confidential").ratio() >= CONFIDENTIAL_SIMILARITY
            for t in content_tokens(text)
        ):
            return "withheld_confidential"
        # Everything else: a letterhead, a subject line with no order under it,
        # or a clerk's keystrokes. The file is not empty in the byte sense, but
        # nobody wrote an order into it.
        return "no_order_content"

    # Blank, yet carrying 200+ letters. Almost always a word-processor file,
    # which has no page to render and so reaches here with ink = -1; the text
    # is real and an order block has to be found in it.
    if len(text.strip()) >= MIN_ORDER_CHARS:
        return "recoverable_text"

    return "inconclusive"


# Below this a file is a stub, not a document: the eight in this corpus are
# 162 bytes holding a name like "so_irr9_icd" and nothing else. No order fits.
STUB_BYTES = 512


def one(sf: str) -> dict:
    p = Path(sf)
    if not p.exists():
        return {"source_file": sf, "verdict": "inconclusive", "chars": 0,
                "ink": -1.0, "format": None}
    raw = p.read_bytes()
    if not raw:
        return {"source_file": sf, "verdict": "source_document_empty",
                "chars": 0, "ink": -1.0, "format": None}
    # Dispatch on magic bytes. The extension is .pdf on every file in this
    # corpus and is true of only some of them.
    if raw[:4] == b"%PDF":
        text, ink = read_pdf(p)
    else:
        text, ink = read_doc(p), -1.0

    fmt = None
    verdict = judge(text, ink)
    if text is None:
        fmt = legacy_format(raw[:8])
        if fmt:
            verdict = "unreadable_legacy_format"
        elif len(raw) < STUB_BYTES:
            verdict = "source_document_empty"

    return {"source_file": sf, "verdict": verdict,
            "chars": len((text or "").strip()), "ink": round(ink, 5),
            "format": fmt}


def main() -> int:
    import psycopg

    with psycopg.connect(dsn()) as conn, conn.cursor() as cur:
        cur.execute(
            """select source_file, text_recovery_method
                 from ap_government_orders
                where order_text is null or order_text = ''"""
        )
        rows = cur.fetchall()
    prior = dict(rows)
    print(f"records with no body: {len(rows)}")

    with ProcessPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(one, [sf for sf, _ in rows], chunksize=16))
    for r in results:
        r["prior_method"] = prior[r["source_file"]]

    counts = Counter(r["verdict"] for r in results)
    print()
    for k, v in counts.most_common():
        print(f"  {v:>6}  {k}")

    OUT_FILE.parent.mkdir(exist_ok=True)
    OUT_FILE.write_text(json.dumps(results, indent=1), encoding="utf-8")
    print(f"\ncensus -> {OUT_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
