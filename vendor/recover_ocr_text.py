"""
Recover order text for the scanned cohort by OCR, without touching anything the
portal already told us.

2,771 orders carry text_recovery_status = OCR_REQUIRED and no order_text. They
are page images: PyMuPDF extracts nothing, or nothing but an e-office tracking
stamp, so no amount of re-parsing will ever help. OCR is the only route.

WHY THIS EXISTS ALONGSIDE ocr_recover.py
----------------------------------------
ocr_recover.py does the hard part — rasterise, call Vision, cache the text — and
this imports that machinery rather than restating it. What it does *not* do is
write conservatively: it ends `records[i] = new`, replacing the whole 26-field
record with a fresh parse of the OCR'd text. That was right when the corpus was
being built and wrong now. The abstracts have since been cleaned and their
verbatim originals kept in abstract_source, and go_number, go_date and
department hold portal-derived values that are more reliable than anything a
photographed page will yield. Replacing the record would silently trade all of
that for an OCR engine's best guess.

So this fills empty fields and only empty fields: order_text where it is NULL,
abstract where it is NULL or empty. Identity fields are never written.

THE QUALITY GATE, AND WHY IT IS A VOCABULARY TEST
-------------------------------------------------
Vision exposes no Indic recognition language. Handed a Telugu-script GO it does
not fail — it transliterates the glyphs into Latin noise:

    v03?  2.2. 00 50.005 30 559  38: 23.10. 2012  ...  ı{á0 a (eaf Sa, da:

That is 265 characters of confident, complete garbage, and any length-based
test admits it. Stored as order_text it would be unreadable to a person and
actively harmful in the search index.

The first gate tried was the share of tokens that look like words. It fails in
the other direction: a real English GO, correctly read, scored 0.44 because
Vision renders a ruled form as columns of numbers. Refusing it would have lost
a perfectly good order.

What separates the two cleanly is vocabulary. Every genuine AP government order
contains a handful of the same words — GOVERNMENT, ANDHRA PRADESH, ABSTRACT,
DEPARTMENT, ORDER, Dated, Sri. Measured across the whole cohort the split is
absolute and bimodal: of 757 OCR'd texts over 200 characters, 751 score 10 or
more distinct terms and the 5 unreadable ones score exactly 0. Nothing sits in
between. So the gate is "at least 3 of the 20", which has a wide margin on both
sides of every case actually observed.

A document that fails it is not discarded and not pretended about. It is
recorded OCR_LOW_QUALITY with its score, which says precisely what happened: the
page was rendered, OCR was attempted, and what came back could not be trusted as
this document's text. The scan is still there for a reader.

STATUSES
--------
    FIXED             OCR passed the gate and an order block was bounded
    NEEDS_MANUAL_REVIEW  OCR passed the gate, no boundable block (abstract kept)
    OCR_LOW_QUALITY   text came back but failed the vocabulary gate
    EXTRACTION_FAILED OCR returned nothing — a blank page

Resumable: one JSON object per file appended to the state file as it goes.
Re-running skips everything already decided. The OCR itself is cached by
ocr_recover.cache_path, so a second pass costs parsing time only.

Usage:
    python3 recover_ocr_text.py --limit 200      # validate on a sample
    python3 recover_ocr_text.py                  # whole cohort, dry run
    python3 recover_ocr_text.py --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

import go_parser as gp
from clean_abstract import clean_abstract
from ocr_recover import DEFAULT_CACHE, cache_path, ocr_pdf

CORPUS_DIR = Path("out/corpus")
STATE_FILE = Path("out/recover_ocr_text.state.jsonl")
REPORT_FILE = Path("out/recover_ocr_text.report.json")
COHORT_FILE = Path("out/ocr_cohort.json")

RECOVERY_WARNING = "recovered_from_ocr"
METHOD = "ocr_vision_on_device"

MIN_BODY_CHARS = 40
DPI = 300

# The vocabulary every genuine AP government order draws on. Deliberately
# generic — these are the words a GO cannot avoid, not words that would let a
# particular department's orders through and keep another's out.
GAZETTEER = (
    "government", "andhra", "pradesh", "abstract", "order", "department",
    "secretary", "governor", "dated", "read", "sri", "issued", "g.o",
    "rules", "section", "district", "service", "sanction", "notification",
    "shall",
)
MIN_GAZETTEER_HITS = 3

# Below this there is no document to judge — a blank page photographed. Vision
# still returns a line or two of speckle on such a page, which is why this is
# not simply "is the string empty".
MIN_OCR_CHARS = 50


def ocr_quality(text: str) -> float:
    """Share of the gazetteer this text contains, 0.0 to 1.0.

    Stored alongside the record, not just compared against a threshold, so a
    later reader can see how marginal a given recovery was rather than only
    whether it passed.
    """
    low = text.lower()
    return sum(1 for g in GAZETTEER if g in low) / len(GAZETTEER)


def gazetteer_hits(text: str) -> int:
    low = text.lower()
    return sum(1 for g in GAZETTEER if g in low)


def load_state() -> dict[str, dict]:
    if not STATE_FILE.exists():
        return {}
    state: dict[str, dict] = {}
    with STATE_FILE.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # truncated final line from a kill; redone this run
            state[rec["source_file"]] = rec
    return state


def ocr_text_for(source_file: str, cache_dir: Path) -> tuple[str, bool]:
    """The OCR text for one file, from the cache when it is there.

    Returns (text, from_cache). A miss is OCR'd and cached, so the expensive
    work happens at most once per document across every run of every script
    that uses this cache.
    """
    cp = cache_path(cache_dir, source_file)
    if cp.exists():
        return cp.read_text(encoding="utf-8", errors="ignore"), True
    try:
        text = ocr_pdf(source_file, DPI)
    except Exception:
        # A file that cannot be rendered is a verdict, not a crash.
        text = ""
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text(text, encoding="utf-8")
    return text, False


def recover_one(source_file: str, cache_dir: Path) -> dict:
    """OCR one scan and return the verdict for it."""
    base = {"source_file": source_file, "order_text": None, "abstract": None,
            "ocr_chars": 0, "ocr_quality": 0.0}
    path = Path(source_file)
    if not path.exists():
        return {**base, "status": "EXTRACTION_FAILED", "detail": "file missing on disk"}

    text, from_cache = ocr_text_for(source_file, cache_dir)
    base["from_cache"] = from_cache
    text = text.strip()
    base["ocr_chars"] = len(text)

    if len(text) < MIN_OCR_CHARS:
        return {**base, "status": "EXTRACTION_FAILED",
                "detail": f"OCR returned {len(text)} chars — blank page"}

    hits = gazetteer_hits(text)
    base["ocr_quality"] = round(ocr_quality(text), 3)
    if hits < MIN_GAZETTEER_HITS:
        # Almost always a Telugu-script GO transliterated into Latin noise.
        # Recorded, never stored as the document's text.
        return {**base, "status": "OCR_LOW_QUALITY",
                "detail": f"{len(text)} chars but only {hits} document terms — "
                          f"unreadable script or failed recognition"}

    try:
        rec = gp.parse_text(path, text, 1, extra_warnings=[RECOVERY_WARNING])
    except Exception as e:
        return {**base, "status": "EXTRACTION_FAILED",
                "detail": f"parse raised {type(e).__name__}: {e}"[:200]}

    body = (rec.get("order_text") or "").strip()
    abstract = (rec.get("abstract") or "").strip() or None

    if len(body) < MIN_BODY_CHARS:
        return {**base, "status": "NEEDS_MANUAL_REVIEW",
                "detail": f"OCR readable ({hits} terms), no boundable order block",
                "abstract": abstract}

    return {
        **base,
        "status": "FIXED",
        "detail": None,
        "order_text": body,
        "abstract": abstract,
        "has_table": rec.get("has_table"),
        "anchors_found": rec.get("anchors_found"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write the corpus")
    ap.add_argument("--limit", type=int, default=0, help="process at most N files")
    ap.add_argument("--fresh", action="store_true", help="ignore prior state and redo")
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    args = ap.parse_args()

    if not CORPUS_DIR.is_dir():
        sys.exit(f"no corpus at {CORPUS_DIR} — run this from the go-ingestion root")
    if not COHORT_FILE.exists():
        sys.exit(f"no {COHORT_FILE} — write the OCR_REQUIRED source_file list there first")

    want = json.load(COHORT_FILE.open(encoding="utf-8"))
    args.cache.mkdir(parents=True, exist_ok=True)

    if args.fresh and STATE_FILE.exists():
        STATE_FILE.unlink()
    state = {} if args.fresh else load_state()
    print(f"{len(state)} files already processed in a previous run")
    print(f"OCR_REQUIRED cohort: {len(want)} files")

    todo = [w for w in want if w not in state]
    print(f"to process: {len(todo)}")
    if args.limit:
        todo = todo[: args.limit]
        print(f"limited to {len(todo)}")

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    counts: Counter = Counter()
    fresh_ocr = 0
    with STATE_FILE.open("a", encoding="utf-8") as out:
        for n, sf in enumerate(todo, 1):
            verdict = recover_one(sf, args.cache)
            state[sf] = verdict
            counts[verdict["status"]] += 1
            fresh_ocr += not verdict.get("from_cache", True)
            out.write(json.dumps(verdict, ensure_ascii=False) + "\n")
            out.flush()
            if n % 250 == 0:
                done = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
                print(f"  {n}/{len(todo)}  {done}", flush=True)

    print(f"\nthis run ({fresh_ocr} OCR'd fresh, the rest served from cache):")
    for k, v in sorted(counts.items()):
        print(f"  {k}: {v}")

    overall = Counter(r["status"] for r in state.values())
    print("\nall processed files:")
    for k, v in sorted(overall.items()):
        print(f"  {k}: {v}")

    bodies = sum(len(r["order_text"] or "") for r in state.values() if r.get("order_text"))
    new_abs = sum(1 for r in state.values() if r.get("abstract"))
    print(f"\nrecovered body text: {bodies:,} characters")
    print(f"records offering an abstract: {new_abs}")

    REPORT_FILE.write_text(
        json.dumps({
            "cohort": len(want),
            "processed": len(state),
            "by_status": dict(overall),
            "recovered_body_chars": bodies,
            "records_with_abstract": new_abs,
        }, indent=2),
        encoding="utf-8",
    )
    print(f"report -> {REPORT_FILE}")

    if not args.apply:
        print("\ndry run — nothing written. Re-run with --apply.")
        return 0

    # --- write the corpus: empty fields only, on both sides ----------------
    wrote_body = wrote_abs = 0
    for yf in sorted(CORPUS_DIR.glob("*.jsonl")):
        tmp = yf.with_suffix(".jsonl.tmp")
        with yf.open(encoding="utf-8") as fin, tmp.open("w", encoding="utf-8") as fout:
            for line in fin:
                if not line.strip():
                    continue
                d = json.loads(line)
                v = state.get(d["source_file"])
                if v and v["status"] == "FIXED" and not (d.get("order_text") or "").strip():
                    d["order_text"] = v["order_text"]
                    wrote_body += 1
                    if v.get("has_table") is not None:
                        d["has_table"] = v["has_table"]
                    if v.get("anchors_found"):
                        d["anchors_found"] = v["anchors_found"]
                if v and v.get("abstract") and not (d.get("abstract") or "").strip():
                    cleaned, _status = clean_abstract(v["abstract"])
                    if cleaned:
                        d["abstract"] = cleaned
                        d.setdefault("abstract_source", v["abstract"])
                        wrote_abs += 1
                if v:
                    w = list(d.get("warnings") or [])
                    if v["status"] == "FIXED":
                        w = [x for x in w if x != "empty_order_block"]
                    if RECOVERY_WARNING not in w:
                        w.append(RECOVERY_WARNING)
                    d["warnings"] = w
                fout.write(json.dumps(d, ensure_ascii=False) + "\n")
        os.replace(tmp, yf)

    print(f"\ncorpus: {wrote_body} order bodies, {wrote_abs} abstracts written")
    print("now push with: python3 push_ocr_recovery.py --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
