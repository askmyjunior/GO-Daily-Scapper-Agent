"""What a GOIR listing row becomes in the corpus.

Shared by the October catch-up (load_catchup.py) and the daily sync (sync.py),
so the two cannot build a row differently. Everything here was first run over
the 6,820 orders of the catch-up and checked by hand; the traps it guards are
recorded where they are guarded.

── NOTHING HERE IS A SECOND IMPLEMENTATION ────────────────────────────────────

Every decision about what a row contains is made by code the corpus was built
with, vendored from go-ingestion (see vendor/README.md) rather than rewritten:

  go_parser.parse_pdf / parse_text   the parse itself
  load_rt.row                        the 48-column row and its conventions
  clean_abstract                     abstract / abstract_source / status
  classify_no_text + PLAN            what a file with no body actually is
  rebuild_search_index.TSV_EXPR      the tsvector, with its canonical joins
  recover_ocr_text's gates           whether an OCR read is trusted

What this module adds is only what the portal knows and a file does not: the
gid, the portal's category, the wing a department belongs to, and the fact
that some listed orders have no document at all.

── THREE THINGS load_rt.row GETS WRONG FOR THESE ROWS, OVERRIDDEN HERE ────────

department_id   go_parser.parse_filename strips the wing suffix, so REV01-D
                would be filed under Revenue. The user's ruling (2026-10-04):
                file it where the portal files it, the wing. dept_code keeps the
                stripped value, as all 19,121 re-filed rows do.
goir_category   load_rt writes `apgo_category`, which migration 035 renamed.
date_uploaded_goir_portal
                load_rt leaves it NULL while writing go_date_source =
                'date_uploaded_goir_portal' — a source naming an empty column.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENDOR = HERE / "vendor"
sys.path.insert(0, str(VENDOR))

# Credentials pasted into GitHub's secret box arrive however they were copied.
# The first cloud run failed on each of the two usual ways: the whole line
# ("SUPABASE_DB_URL=postgresql://...") and then a trailing line break, which
# made the database name "postgres\n" — a database that does not exist. No
# value here legitimately carries its own name, quotes, or outer whitespace,
# so they are removed before anything reads them, and selftest.py says which
# secrets needed it (never what they contain).
SECRET_NAMES = ("SUPABASE_DB_URL", "R2_ACCOUNT_ID", "R2_BUCKET", "R2_ACCESS_KEY_ID",
                "R2_SECRET_ACCESS_KEY", "GEMINI_API_KEY", "GOIR_RELAY_TOKEN")
TIDIED: dict[str, str] = {}
for _name in SECRET_NAMES:
    _raw = os.environ.get(_name)
    if not _raw:
        continue
    _v, _why = _raw.strip(), []
    if _v != _raw:
        _why.append("spaces or a line break around it")
    if _v.startswith(_name + "="):
        _v = _v[len(_name) + 1:].strip()
        _why.append(f"its name pasted in front ({_name}=)")
    if len(_v) >= 2 and _v[0] == _v[-1] and _v[0] in "\"'":
        _v = _v[1:-1].strip()
        _why.append("quotation marks around it")
    if _why:
        os.environ[_name] = _v
        TIDIED[_name] = ", ".join(_why)

import go_parser as gp  # noqa: E402
import load_supabase as ls  # noqa: E402
import load_rt  # noqa: E402
import classify_no_text as nt  # noqa: E402
import classify_gemini as cg  # noqa: E402,F401  (re-exported for the scripts)
from clean_abstract import clean_abstract  # noqa: E402
from push_no_text_classification import PLAN, note_for  # noqa: E402
from recover_doc_text import RECOVERY_WARNING, plausible_text, scrub_filler  # noqa: E402
from rebuild_search_index import TSV_A_EXPR, TSV_EXPR  # noqa: E402
import ocr_recover as orc  # noqa: E402
import recover_ocr_text as rot  # noqa: E402

# The portal's seven categories, as backfill_goir.py checks them. Anything else
# is a truncated cell read mid-render and is stored as NULL, not as a guess.
KNOWN_CATEGORIES = {"Others", "Service Matter", "Budget Release Order", "Transfers",
                    "Tours", "General Provident Fund", "GO from eOffice"}

# Four letters exist (SGSW01) and so do wings (REV01-D). [A-Z]{3}\d{2} loses both.
DEPT_RE = re.compile(r"^\s*([A-Z]{3,4}\d{2}(?:-[A-Z])?)\b")

# Below this a body is a fragment of letterhead, not an order. recover_doc_text's floor.
MIN_BODY_CHARS = 40

# A document date further than this from the register's is a misread (a cited
# order's date), not the order being signed one day and listed the next.
DATE_GAP_DAYS = 31

# load_rt.GO_COLUMNS with migration 035's rename applied, plus the columns that
# exist on the table and that these rows can fill truthfully.
GO_COLUMNS = [("goir_category" if c == "apgo_category" else c) for c in load_rt.GO_COLUMNS] + [
    "goir_gid", "abstract_source", "abstract_clean_status", "text_recovery_note",
    "source_object_key", "source_sha256", "source_bytes", "source_uploaded_at",
    "ocr_quality",
]
REF_COLUMNS = ["seq", "ref_kind", "go_type", "go_number", "department_raw", "date_raw", "raw_text"]
RCP_COLUMNS = ["seq", "recipient"]

# The disk the database and its WAL may reach before anything here stops
# writing. The plan's cap is 8 GB; see project-amj-supabase-write-limits.
MAX_DISK = int(7.2 * 1024**3)


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------

def safe(name: str) -> str:
    """A filename the filesystem will take, changing as little as possible."""
    return name.replace("/", "-").replace("\0", "").strip()


def filename_for(row: dict, go_type: str) -> str | None:
    """`AHF01 - ANIMAL HUSBANDRY AND FISHERIES-RT-205-01_07_2026.pdf`

    Built from the portal's own organisation string, type, number and date —
    the convention the existing 521,878 names follow, and the natural key the
    user chose (2026-10-04).
    """
    org = row["organization"].strip()
    num = row["go_number_raw"].split("-")[-1].strip()
    try:
        d = dt.datetime.strptime(row["go_date_raw"].strip(), "%d/%m/%Y").date()
    except ValueError:
        return None
    if not org or not num.isdigit():
        return None
    return safe(f"{org}-{go_type}-{num}-{d:%d_%m_%Y}.pdf")


def source_file_for(go_type: str, day: dt.date, filename: str) -> str:
    """Unique, stable across re-runs, and not a path on anybody's laptop."""
    return f"goir-daily/{go_type}/{day.year}/{filename}"


def rel(source_file: str) -> str:
    """source_path, migration 032's natural key: '<TYPE>/goir-daily/<YYYY>/<filename>'.

    Starts with the order type like every other row ('MS/…', 'RT/…'), so the
    loaders' `source_path like 'RT/%'` counts keep meaning "every RT order".
    """
    _, go_type, year, filename = source_file.split("/", 3)
    return f"{go_type}/goir-daily/{year}/{filename}"


# load_rt.row calls rel() on source_file; its own resolves against RT_ROOT.
load_rt.rel = rel


def key_for(sha: str) -> str:
    """upload_r2.key_for: content-addressed, so a re-upload is a no-op."""
    return f"pdf/{sha[:2]}/{sha[2:4]}/{sha}.pdf"


def sniff(head: bytes) -> str:
    """upload_r2.sniff: the content type from the bytes, never the extension."""
    if head.startswith(b"%PDF"):
        return "application/pdf"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "application/msword"
    if head.startswith(b"PK\x03\x04"):
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if head.startswith(b"{\\rtf"):
        return "application/rtf"
    return "application/octet-stream"


def listing_day(r: dict) -> dt.date:
    return dt.datetime.strptime(r["go_date_raw"], "%d/%m/%Y").date()


def listing_number(r: dict) -> int | None:
    m = re.search(r"(\d+)\s*$", r.get("go_number_raw") or "")
    return int(m.group(1)) if m else None


_HEADER_TOKENS = re.compile(r"(?i)\b(g\.?\s*o\.?\s*(ms|rt|p)?\.?\s*no\.?|dated|date)\b|[\d:./\-]+")


def header_only(abstract: str | None) -> bool:
    """A 'subject' that is nothing but the order's number and date line."""
    a = " ".join((abstract or "").split())
    return bool(a) and not _HEADER_TOKENS.sub(" ", a).strip(" .,:-")


# ---------------------------------------------------------------------------
# the order's own number and date: the register's, the header's only to fill
# ---------------------------------------------------------------------------
#
# The parser's anchors can land on the first document an order cites. Row
# 445791 was stored as G.O.Rt.No.165 of 22-04-2021; it is G.O.Rt.No.500 of
# 24-06-2022, and 165 / 22-04-2021 is item 1 of its "Read the following" list.
# 4,783 older RT rows were corrected for this on 2026-10-07. The register
# listing names every order the sync loads, with its number and date, so those
# are stored; the header only fills what the register lacks, and only from
# text printed before the Read list begins.

# Where the Read list begins: its heading on a line of its own ("Read the
# following:-", "Read:", "Ref:"), or run on after the number line.
READ_HEADING_RE = re.compile(
    r"(?im)^[ \t]*(?:Read|Refs?|References?)\b|\bRead\s+the\s+following\b|\bRead\s*:")
# No heading at all: the list's first item ("1. G.O.Rt.No. 165, ... Dated...").
FIRST_ITEM_RE = re.compile(r"(?m)^[ \t]*\(?1[ \t]*[.)][ \t]*\S")
# Nothing of the header comes after the order begins.
ORDER_LINE_RE = re.compile(r"(?m)^[ \t]*O\s*R\s*D\s*E\s*R\b")
# Enough for the masthead, subject and Read list of any page one.
HEAD_CHARS = 6000


def head_text(text: str | None) -> str:
    return gp.normalize_text(text or "")[:HEAD_CHARS]


def pdf_head(p: Path) -> str:
    """The first two pages' text, read the way parse_pdf reads them."""
    try:
        with gp.fitz.open(p) as doc:
            return head_text("\n".join(doc[i].get_text() for i in range(min(2, len(doc)))))
    except Exception:
        return ""


def read_list_start(head: str) -> int | None:
    """Offset of the first document cited, or None when there is no Read list.

    Whichever comes first, the heading or item 1: a text layer out of reading
    order can print the items above their heading (HMF01-MS-91-10_07_2026).
    """
    heading, item, order = (rx.search(head) for rx in (READ_HEADING_RE, FIRST_ITEM_RE, ORDER_LINE_RE))
    if item and order and item.start() > order.start():
        item = None    # a "1." below the ORDER line is the body's own paragraph
    starts = [m.start() for m in (heading, item) if m]
    return min(starts) if starts else None


def _dates_in(text: str) -> set[str]:
    found = set()
    for rx in (gp.DATE_RE, gp.DATE_COMPACT_RE):
        for m in rx.finditer(text):
            found.add(gp._parse_date(*m.groups())[0])
    for m in gp.DATE_LONG_RE.finditer(text):
        day, month, year = m.groups()
        found.add(gp._parse_date(day, str(gp.MONTHS[month.lower().rstrip(".")]), year)[0])
    found.discard(None)
    return found


def _numbers_in(text: str) -> set[int]:
    return {int(re.match(r"\d+", m.group("go_number")).group(0)) for m in gp.GO_NUM_RE.finditer(text)}


def where_printed(value, head: str, find) -> str:
    """Where the parser's value is printed: 'header' (before the Read list),
    'read_list' (only from the first cited document on), 'unplaced' (no text
    to look in, or not found as printed), or 'absent' (the parser found none)."""
    if value is None:
        return "absent"
    if not head:
        return "unplaced"
    cut = read_list_start(head)
    if cut is None:
        order = ORDER_LINE_RE.search(head)
        cut = order.start() if order else len(head)
    if value in find(head[:cut]):
        return "header"
    if value in find(head[cut:]):
        return "read_list"
    return "unplaced"


def register_identity(reg_no: int | None, reg_date: dt.date | None, gm: dict, head: str) -> dict:
    """The number and date to store, their sources, and what the header said.

    The register's value always wins. The header's is used only where the
    register has none, and then only if it is printed before the Read list.
    A header that agrees with the register makes the source 'document_header'
    wherever it sits on the page: two independent records agree.
    """
    hdr_no = gm.get("go_number")
    hdr_date = (gm.get("go_date") or {}).get("iso")
    out = {"warnings": [], "go_date_remark": None,
           "number_at": where_printed(hdr_no, head, _numbers_in),
           "date_at": where_printed(hdr_date, head, _dates_in)}

    if reg_no is not None:
        out["go_number"] = reg_no
        out["go_number_source"] = "document_header" if hdr_no == reg_no else "goir_register"
    elif out["number_at"] == "header":
        out["go_number"], out["go_number_source"] = hdr_no, "document_header"
    else:
        out["go_number"], out["go_number_source"] = None, None
    if hdr_no is not None and hdr_no != out["go_number"] and out["number_at"] == "read_list":
        out["warnings"].append("document_number_from_read_list")

    reg_iso = reg_date.isoformat() if reg_date else None
    if reg_iso:
        out["go_date"] = reg_iso
        out["go_date_source"] = ("document_header" if hdr_date == reg_iso
                                 else "date_uploaded_goir_portal")
    elif out["date_at"] == "header":
        out["go_date"], out["go_date_source"] = hdr_date, "document_header"
    else:
        out["go_date"], out["go_date_source"] = None, None

    if hdr_date and reg_iso and hdr_date != reg_iso:
        gap = abs((dt.date.fromisoformat(hdr_date) - reg_date).days)
        if out["date_at"] == "read_list":
            out["warnings"].append("document_date_from_read_list")
            out["go_date_remark"] = (f"Document header date read as {hdr_date}, the date of a document "
                                     f"in its Read list; the GOIR register's {reg_iso} is used.")
        else:
            out["warnings"].append("document_date_differs_from_register")
            out["go_date_remark"] = (f"Document header date read as {hdr_date}, {gap} days from the "
                                     f"GOIR register's {reg_iso}; the register's date is used.")
        if gap > DATE_GAP_DAYS:
            # The token the site's provenance reads (src/lib/provenance.ts).
            # Its sentence says "more than a month from the date the GOIR
            # portal gives it", so it is set only past that gap.
            out["warnings"].append("document_date_disagrees_with_register")
    return out


# ---------------------------------------------------------------------------
# reading a file as what it is
# ---------------------------------------------------------------------------

_read_doc_textutil = nt.read_doc


def read_word(p: Path) -> str | None:
    """A Word file's text on any machine.

    classify_no_text.read_doc reads .docx straight out of document.xml and
    hands .doc and .rtf to macOS `textutil`. A GitHub runner is Linux and has
    no textutil, so there a .doc goes to `antiword` (installed by the
    workflow) and an .rtf is left unread — none has been seen since 2026.
    """
    head = p.read_bytes()[:8]
    if head[:4] == b"PK\x03\x04":
        return nt.read_docx(p)
    if shutil.which("textutil"):
        return _read_doc_textutil(p)
    if head == nt.OLE2 and shutil.which("antiword"):
        try:
            r = subprocess.run(["antiword", "-w", "0", str(p)], capture_output=True, timeout=60)
        except (subprocess.SubprocessError, OSError):
            return None
        return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None
    return None


# The census calls read_doc by name; on Linux it must get the portable reader,
# or a short Word file it could have read is judged "inconclusive".
nt.read_doc = read_word


def parse_one(path_str: str) -> dict:
    """One file -> its parse, its bytes' identity, and — if it has no body —
    the census's verdict on what it actually is. Run in a process pool, never
    threads: PyMuPDF is not thread-safe, and a thread pool once returned 1,265
    readable PDFs as "unreadable" without raising."""
    p = Path(path_str)
    raw = p.read_bytes()
    head = raw[:8]
    out: dict = {"path": path_str, "sha": hashlib.sha256(raw).hexdigest(),
                 "bytes": len(raw), "ctype": sniff(head), "is_pdf": head[:4] == b"%PDF"}

    if out["is_pdf"]:
        rec = gp.parse_pdf(p)
        out["route"] = "pdf"
        out["head_text"] = pdf_head(p)
    else:
        # Read as what it is, never as what it is named. PyMuPDF opens a .docx
        # without complaint as one blank page, which is how 1,231 Word files
        # were once filed as scans nobody could read.
        text = read_word(p)
        if text and plausible_text(text):
            text = scrub_filler(text)
        out["head_text"] = head_text(text)
        if text and len(text.strip()) >= MIN_BODY_CHARS:
            rec = gp.parse_text(p, text, 1, extra_warnings=[RECOVERY_WARNING])
            out["route"] = "docx" if head[:4] == b"PK\x03\x04" else "doc"
        elif text is not None:
            # Read, and short. Three of these on 03-09-2026 say only "Sub:
            # CONFIDENTIAL" — the census below says what they are. Calling them
            # an unreadable format would be a false statement about the file.
            rec = gp.parse_text(p, text, 1)
            out["route"] = "doc_short"
        else:
            rec = gp.parse_text(p, "", 1, extra_warnings=["legacy_format:unreadable"])
            out["route"] = "unreadable"

    body = (rec.get("order_text") or "").strip()
    if len(body) < MIN_BODY_CHARS:
        rec["order_text"] = None
        # What the file actually is, read in full. Not a guess from the parser.
        out["census"] = nt.one(path_str)
    out["rec"] = rec
    return out


def parse_many(paths: list[str], workers: int) -> dict[str, dict]:
    if not paths:
        return {}
    with ProcessPoolExecutor(max_workers=max(1, min(workers, len(paths)))) as pool:
        return {res["path"]: res for res in pool.map(parse_one, paths, chunksize=4)}


# ---------------------------------------------------------------------------
# OCR — scans only, with the gates the corpus's own OCR passes used
# ---------------------------------------------------------------------------

#: text_recovery_method for a scan OCR read successfully, by engine.
OCR_METHOD = {"vision": "ocr_vision_on_device", "tesseract": "ocr_tesseract"}


def ocr_engine() -> str | None:
    """Tesseract where it is installed — the GitHub runner, by the user's
    choice (2026-10-04). Otherwise none, and a scan is loaded truthfully as
    "text recognition not yet run" for a later run to finish. The catch-up
    asked for macOS Vision explicitly; nothing chooses it implicitly."""
    want = os.environ.get("GOIR_OCR", "auto")
    if want == "none":
        return None
    if want in ("tesseract", "auto") and shutil.which("tesseract"):
        return "tesseract"
    if want == "vision":
        return "vision"
    return None


def tesseract_pdf(path: str) -> str:
    """Every page rendered at the same 300 dpi the Vision pass used, read by
    Tesseract in English. Telugu pages come back as Latin noise, which the
    gazetteer gate below refuses — exactly as it refuses Vision's."""
    import fitz
    out = []
    with fitz.open(path) as doc:
        for page in doc:
            png = page.get_pixmap(dpi=rot.DPI).tobytes("png")
            try:
                r = subprocess.run(["tesseract", "stdin", "stdout", "-l", "eng", "--psm", "3"],
                                   input=png, capture_output=True, timeout=300)
            except (subprocess.SubprocessError, OSError):
                continue
            out.append(r.stdout.decode("utf-8", "replace"))
    return "\n".join(out)


def _tesseract_job(path: str) -> tuple[str, str]:
    try:
        return path, tesseract_pdf(path)
    except Exception:
        # A file that cannot be rendered is a verdict, not a crash.
        return path, ""


def ocr_scans(results: list[dict], workers: int, engine: str | None,
              cache_dir: Path | None = None) -> Counter:
    """OCR the files the census found INKED with no text layer — a blank page
    is not a scan — and judge each read with recover_ocr_text's gates: under
    50 characters is nothing, under 3 of its 20 document words is an unreadable
    script, and a parse that cannot bound an order keeps its abstract only."""
    todo = [r for r in results
            if r["is_pdf"] and (r.get("census") or {}).get("verdict") == "scanned_needs_ocr"]
    tally: Counter = Counter()
    if not todo or engine is None:
        return tally
    t0 = time.time()
    if engine == "vision":
        cache = cache_dir or (HERE / "out" / "ocr_cache")
        cache.mkdir(parents=True, exist_ok=True)
        got = orc.ocr_batch([(r["path"], rot.DPI, str(cache)) for r in todo], min(6, workers), 300.0)
        texts = {p: t for p, (t, _) in got.items()}
    else:
        with ProcessPoolExecutor(max_workers=max(1, min(workers, len(todo)))) as pool:
            texts = dict(pool.map(_tesseract_job, [r["path"] for r in todo]))

    for r in todo:
        text = (texts.get(r["path"]) or "").strip()
        if len(text) < rot.MIN_OCR_CHARS:
            # The census saw ink; OCR read nothing. One of them is wrong and
            # nothing here can say which — push_no_text's wording exactly.
            r["ocr"] = ("NEEDS_MANUAL_REVIEW", "ocr_returned_nothing_on_inked_page", None)
        elif rot.gazetteer_hits(text) < rot.MIN_GAZETTEER_HITS:
            r["ocr"] = ("OCR_LOW_QUALITY", "ocr_attempted_unreadable_script",
                        round(rot.ocr_quality(text), 3))
        else:
            q = round(rot.ocr_quality(text), 3)
            # The PDF's real page count, not the 1 that ocr_rt_queue.py passes:
            # parse_text stores it as page_count, and the reader prints
            # "Page 1 of 1" over a 16-page scan otherwise.
            pages = r["rec"].get("page_count") or 1
            rec = gp.parse_text(Path(r["path"]), text, pages, extra_warnings=[orc.RECOVERY_WARNING])
            if len((rec.get("order_text") or "").strip()) >= MIN_BODY_CHARS:
                r["ocr"] = ("FIXED", OCR_METHOD[engine], q)
            else:
                rec["order_text"] = None     # the abstract it found is kept
                r["ocr"] = ("NEEDS_MANUAL_REVIEW", "ocr_readable_no_boundable_order_block", q)
            r["rec"] = rec
            r["head_text"] = head_text(text)
            r["route"] = "ocr"
        tally[f"ocr_{r['ocr'][0]}"] += 1
    print(f"  OCR ({engine}): {len(todo)} scans in {time.time() - t0:.0f}s -> "
          + ", ".join(f"{k[4:]} {v}" for k, v in sorted(tally.items())), flush=True)
    return tally


# ---------------------------------------------------------------------------
# what the row says about its text
# ---------------------------------------------------------------------------

def text_state(parsed: dict) -> tuple[str | None, str | None, str | None]:
    """(text_recovery_status, text_recovery_method, text_recovery_note).
    Every method here is one the site words (askmyjunior-web src/lib/textState.ts)."""
    rec = parsed["rec"]
    if parsed.get("ocr"):
        return parsed["ocr"][0], parsed["ocr"][1], None
    if rec.get("order_text"):
        if parsed["route"] in ("docx", "doc"):
            return None, f"doc_conversion_{parsed['route']}", None
        return None, None, None
    census = parsed.get("census") or {"verdict": "inconclusive"}
    v = census["verdict"]
    if v == "scanned_needs_ocr":
        # OCR has never been tried on it, so "not yet attempted" is the true
        # sentence; a later run with an OCR engine finishes it.
        return ("OCR_REQUIRED", "ocr_not_yet_attempted",
                "Scanned document with no usable text layer. Text recognition "
                "has not yet been run over it.")
    if v == "recoverable_text":
        return ("NEEDS_MANUAL_REVIEW", "triage_no_body",
                "The document has readable text, but the operative order block "
                "could not be bounded automatically.")
    if v == "inconclusive":
        return None, None, None
    status, method, const = PLAN[v]
    return status or None, method or None, note_for(census, const)


def bucket_for(parsed: dict) -> str:
    rec = parsed["rec"]
    if rec.get("order_text"):
        return "clean" if (rec.get("extraction_confidence") or 0) >= 0.5 else "needs_review"
    return "unreadable_no_text" if parsed["is_pdf"] else "unreadable_not_pdf"


# ---------------------------------------------------------------------------
# building a row
# ---------------------------------------------------------------------------

def build_row(r: dict, res: dict, dept_ids: dict, dept_codes: dict,
              counts: Counter, anomalies: list) -> tuple[dict, list[tuple], list[tuple]]:
    """A listing row with a document -> (orders row, references, recipients).

    `r` carries the portal's listing fields plus `go_type` and `filename`;
    `res` is parse_one's result, after ocr_scans.
    """
    rec = res["rec"]
    day = listing_day(r)
    go_type = r["go_type"]
    reg_no = listing_number(r)
    sf = source_file_for(go_type, day, r["filename"])
    rec["source_file"] = sf
    rec["filename"] = r["filename"]

    # The register's identity comes from the listing, which IS the register.
    # parse_filename reads the same facts back out of a name built from it; a
    # disagreement would mean the name was built wrong, so it is recorded.
    fp = gp.parse_filename(r["filename"]) or {}
    want = {"go_type": go_type, "go_number": reg_no, "date_iso": day.isoformat()}
    diff = {k: (fp.get(k), v) for k, v in want.items() if fp.get(k) != v}
    if diff:
        anomalies.append({"filename": r["filename"], "filename_parse_disagrees": diff})
    fp = {**fp, **want, "go_year": day.year}
    code = DEPT_RE.match(r["organization"]).group(1)
    parent = fp.get("dept_code") or code.split("-")[0]
    fp["dept_code"] = parent
    rec["filename_parsed"] = fp
    has_text = bool(rec.get("order_text"))

    # The register's number and date, never the header's over them. Until
    # 2026-10-07 the header's won, guarded only for dates more than a month
    # out (79 of 6,619 in the catch-up), so a Read-list date within the month,
    # and every Read-list number, went in as the order's own. See
    # register_identity.
    ident = register_identity(reg_no, day, rec.get("go_meta") or {}, res.get("head_text") or "")
    rec["warnings"] = list(rec.get("warnings") or []) + ident["warnings"]
    if ident["number_at"] == "read_list" and ident["go_number"] != (rec.get("go_meta") or {}).get("go_number"):
        # The parser read the type off the same cited G.O. as the number, so
        # "the document calls itself a different kind of order" would be false.
        rec["go_type"] = go_type
        rec["warnings"] = [w for w in rec["warnings"]
                           if not w.startswith("filename_body_mismatch:go_type(")]
    for w in ident["warnings"]:
        counts[w] += 1
    counts[f"date_source_{ident['go_date_source']}"] += 1
    counts[f"number_source_{ident['go_number_source']}"] += 1
    rec["go_year"] = dt.date.fromisoformat(ident["go_date"]).year

    # Abstract: the document's own, as for every other row; the listing's only
    # where the document yields nothing usable, and then said so.
    raw_abs = rec.get("abstract")
    abstract, abs_status = clean_abstract(raw_abs) if raw_abs else (None, "EMPTY_AFTER_CLEAN")
    warnings = list(rec.get("warnings") or [])
    if header_only(abstract):
        # 28 of 6,619 in the catch-up: the parser bounded the G.O.'s own number
        # line ("2211 G.O.Rt.No: 02-07-2026 Dated:") as its subject, and the
        # site printed that as the order's heading.
        abstract = None
        warnings.append("document_abstract_was_header_line")
    if not abstract and (r.get("abstract") or "").strip():
        raw_abs = r["abstract"].strip()
        abstract, abs_status = clean_abstract(raw_abs)
        warnings.append("abstract_from_goir_listing")
    rec["warnings"] = warnings
    rec["abstract"] = abstract

    row = load_rt.row(rec, has_text, dept_ids, {parent: dept_codes[code]})
    assert row is not None, r["filename"]

    status, method, note = text_state(res)
    # No go_number_remark: the site reads one as "this number is not stated in
    # the document", false whenever the header printed it.
    row.update({k: ident[k] for k in ("go_number", "go_number_source", "go_date",
                                      "go_date_source", "go_date_remark")})
    row["go_number_goir_register"] = reg_no
    row.update({
        "goir_category": r["goir_category"] if r.get("goir_category") in KNOWN_CATEGORIES else None,
        "date_uploaded_goir_portal": day.isoformat(),
        "routing_bucket": bucket_for(res),
        "text_recovery_status": status,
        "text_recovery_method": method,
        "goir_gid": str(r["gid_english"]),
        "abstract": abstract,
        "abstract_source": raw_abs,
        "abstract_clean_status": abs_status,
        "text_recovery_note": note,
        "source_object_key": key_for(res["sha"]),
        "source_sha256": res["sha"],
        "source_bytes": res["bytes"],
        "source_uploaded_at": None,   # filled from the upload ledger at load
        "ocr_quality": res["ocr"][2] if res.get("ocr") else None,
    })
    assert row["department_id"] == dept_ids[dept_codes[code]]

    counts[f"type_{go_type}"] += 1
    counts[f"route_{res['route']}"] += 1
    counts[f"bucket_{row['routing_bucket']}"] += 1
    counts[f"text_{status or '-'}/{method or '-'}"] += 1
    counts["abstract_from_listing"] += "abstract_from_goir_listing" in warnings
    counts["no_abstract"] += not abstract
    counts["wing_filed"] += "-" in code

    refs: list[tuple] = []
    recips: list[tuple] = []
    if has_text:
        for i, ref in enumerate(rec.get("references") or []):
            refs.append((sf, ref.get("seq", i), ref.get("ref_kind"), ref.get("go_type"),
                         ref.get("go_number"), ref.get("department"),
                         ref.get("date_raw"), ref.get("raw_text")))
        for i, rcp in enumerate(rec.get("recipients") or []):
            recips.append((sf, i, rcp))
    return row, refs, recips


# ---------------------------------------------------------------------------
# listings with no document
# ---------------------------------------------------------------------------

NOT_ISSUED = re.compile(r"^\s*not\s+issued\W*$", re.I)
CONFIDENTIAL = re.compile(r"^\s*(strictly\s+)?confidential\W*$", re.I)


def held_state(r: dict) -> tuple[str, str]:
    """(text_recovery_method, text_recovery_note) for a listing with no file.

    The register's own words are the only evidence of WHY there is no
    document, so a reason is given only where the abstract states one. The
    site words each method (src/lib/textState.ts, status NO_DOCUMENT_ON_PORTAL);
    none of them claims a published document.
    """
    abstract = (r.get("abstract") or "").strip()
    if NOT_ISSUED.match(abstract):
        return "portal_register_not_issued", "The GOIR register entry reads 'Not Issued'; no document is listed."
    if CONFIDENTIAL.match(abstract):
        return "portal_register_confidential", "The GOIR register entry reads 'Confidential'; no document is listed."
    if r.get("gid_telugu") and not r.get("gid_english"):
        return "portal_telugu_only", f"Only a Telugu document is listed (portal gid {r['gid_telugu']})."
    if r.get("gid_english"):
        return "portal_zero_byte_file", f"The listed English document (gid {r['gid_english']}) is a zero-byte file."
    return "portal_no_document", "No English or Telugu document is listed on the GOIR portal."


def held_row(r: dict, dept_ids: dict, dept_codes: dict) -> dict:
    """A listing with no document -> its orders row, from the register alone."""
    day = listing_day(r)
    go_type = r["go_type"]
    code = DEPT_RE.match(r["organization"]).group(1)
    sf = source_file_for(go_type, day, r["filename"])
    raw_abs = (r.get("abstract") or "").strip() or None
    abstract, abs_status = clean_abstract(raw_abs) if raw_abs else (None, "EMPTY_AFTER_CLEAN")
    method, note = held_state(r)
    num = listing_number(r)
    row = {c: None for c in GO_COLUMNS}
    row.update({
        "source_file": sf, "source_path": rel(sf), "filename": r["filename"],
        "go_year": day.year,
        "go_number": num, "go_number_goir_register": num, "go_number_source": "goir_register",
        "go_date": day.isoformat(), "date_uploaded_goir_portal": day.isoformat(),
        "go_date_source": "date_uploaded_goir_portal",
        "go_type": go_type,
        "department_id": dept_ids[dept_codes[code]], "dept_code": code.split("-")[0],
        "abstract": abstract, "abstract_source": raw_abs, "abstract_clean_status": abs_status,
        "is_amendment": False, "is_eoffice": False, "has_table": False,
        "secondary_category_tags": "{}",
        "warnings": ls.pg_array(["abstract_from_goir_listing", "no_document_on_portal"]),
        "routing_bucket": "unreadable_no_text",
        "text_recovery_status": "NO_DOCUMENT_ON_PORTAL",
        "text_recovery_method": method, "text_recovery_note": note,
        "goir_gid": str(r["gid_english"]) if r.get("gid_english") else None,
        "goir_category": r["goir_category"] if r.get("goir_category") in KNOWN_CATEGORIES else None,
    })
    return row


# ---------------------------------------------------------------------------
# the database
# ---------------------------------------------------------------------------

INDEX_SQL = """
insert into public.go_search_index
    (go_id, go_date, go_year, go_number, department_id, department,
     go_category, go_sub_category, go_type, status, date_year,
     go_number_register, go_type_register, tsv, tsv_a)
select g.id, g.go_date, g.go_year, g.go_number, g.department_id, d.name,
       g.go_category, g.go_sub_category,
       coalesce(g.go_type_document, g.go_type),
       g.status,
       extract(year from g.go_date)::int,
       case when g.go_number_goir_register is not null
             and g.go_number_goir_register <> g.go_number
            then g.go_number_goir_register end,
       case when g.go_type is not null
             and g.go_type <> coalesce(g.go_type_document, g.go_type)
            then g.go_type end,
       """ + TSV_EXPR + """,
       """ + TSV_A_EXPR + """
  from public.ap_government_orders g
  join public.departments d on d.id = g.department_id
  left join public.go_classification c on c.go_id = g.id
  left join public.go_categories cat on cat.key = c.primary_category
  left join lateral (
      select string_agg(sc.label, ' ') as labels
        from unnest(coalesce(c.tags, '{}'::text[])) t
        join public.go_subcategories sc on sc.key = t
  ) tg on true
 where g.id = any(%s)
   and not exists (select 1 from public.go_search_index s where s.go_id = g.id)
"""


def disk(cur) -> float:
    cur.execute("select pg_database_size(current_database()) + "
                "(select coalesce(sum(size), 0) from pg_ls_waldir())")
    return float(cur.fetchone()[0])


def check_departments(cur) -> None:
    """The map rows are built against must still be the database's, or a
    department_id would mean something else."""
    ids, _ = ls.canonical_departments()
    cur.execute("select id, name from public.departments order by id")
    assert {n: i for i, n in cur.fetchall()} == ids, \
        "departments table no longer matches canonical_departments()"
