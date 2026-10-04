"""
Recover the GOs that are Microsoft Word documents saved with a .pdf extension.

PyMuPDF cannot open them, so they were ingested from their filenames alone and
recover_order_text.py could only mark them EXTRACTION_FAILED. Of those 3,123
records, all 3,123 carry a GO number, date and department taken from the portal
listing — but only 382 have an abstract and none has an order body. They are
very nearly invisible to search: findable by their citation or department, and
by nothing they actually say.

Their text is not lost. The files are OLE2 Word documents (a handful are RTF or
zipped .docx), and macOS `textutil` reads all three natively. This converts each
one and runs the result through the same go_parser anchors as a native parse.

Calls no model and runs no OCR — the brief asks for direct extraction here, and
OCR on a document that has real text would be both slower and worse.

WHAT IT WRITES, AND WHAT IT REFUSES TO TOUCH
--------------------------------------------
recover_doc.py, which this supersedes for the database path, replaced the whole
26-field record with a freshly parsed one. That is too blunt now: it would
discard the cleaned abstracts and the abstract_source provenance written by the
abstract cleanup, and it would overwrite go_number, go_date and department —
identity fields whose portal-derived values are more trustworthy than anything
recovered from a converted document's text.

So only empty derived fields are filled:

    order_text    written only where it is currently NULL
    abstract      written only where it is currently NULL or empty, and passed
                  through clean_abstract first so it matches the rest of the
                  corpus; the verbatim text is kept in abstract_source

Identity fields are never written. An existing abstract is never replaced.

STATUSES  (written to text_recovery_status)
-------------------------------------------
FIXED                 text recovered from the document's own content
NEEDS_MANUAL_REVIEW   converted, but too little text to be an order
EXTRACTION_FAILED     textutil cannot read this format (Lotus WordPro, empty
                      files, truncated stubs) — the verdict stands unchanged

Usage:
    python3 recover_doc_text.py                 # dry run, reports only
    python3 recover_doc_text.py --apply         # write corpus + database
    python3 recover_doc_text.py --limit 200     # try a sample first
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

import go_parser as gp
from clean_abstract import clean_abstract
from recover_doc import convert_to_text, convertible_ext

CORPUS_DIR = Path("out/corpus")
STATE_FILE = Path("out/recover_doc_text.state.jsonl")
REPORT_FILE = Path("out/recover_doc_text.report.json")
PRIOR_STATE = Path("out/recover_order_text.state.jsonl")

RECOVERY_WARNING = "recovered_from_doc_conversion"
METHOD = "doc_conversion_textutil"

# Below this a "recovered" body is a fragment of letterhead, not an order.
# Same floor recover_order_text.py uses, for the same reason.
MIN_BODY_CHARS = 40


def plausible_text(s: str) -> bool:
    """Is this textutil's reading of the document, or the document's own bytes?

    textutil does not always fail loudly. Handed a .doc whose content it cannot
    interpret it exits 0, writes nothing to stderr, and copies the input to
    stdout — so the caller receives 108,544 characters beginning with the OLE2
    signature and containing RTF control words, and every length check and
    exit-code check says the conversion succeeded. Without this guard that
    binary would be parsed for anchors and could be stored as an order's text.

    Real converted text is overwhelmingly printable. Binary passed through is
    not, so the ratio decides it.
    """
    if not s:
        return False
    if s.startswith("ÐÏà") or s.lstrip().startswith("{\\rtf"):
        return False  # OLE2 signature or raw RTF markup, not converted text
    sample = s[:4000]
    printable = sum(1 for ch in sample if ch.isprintable() or ch in "\n\r\t")
    return printable / len(sample) >= 0.90


# Runs of 0xFF / replacement / NUL that these conversions leave where the
# document had a form field, a rule or an inline image.
_FILLER_RUN = re.compile(r"[ÿ�\x00]{8,}")


def scrub_filler(s: str) -> str:
    """Replace runs of filler bytes with a line break.

    Written first as "drop everything from the first filler run onwards", on
    the assumption that the run was trailing noise. It is not. In the file this
    was tested against the run sits at offset 447, immediately after the
    ABSTRACT, and the 5,066 characters *following* it are the order itself —
    `Read the following`, the six cited G.O.s and the operative text. Truncating
    there threw the whole order away and left a record that looked recovered
    because it still had a masthead and an abstract.

    So the run is replaced, not cut at: it is a page artifact between two pieces
    of real text, and the newline keeps the line structure go_parser reads.
    """
    return _FILLER_RUN.sub("\n", s).strip()


def embedded_rtf(raw: bytes) -> bytes | None:
    """The RTF document some of these OLE2 files carry inside them.

    A Word file that textutil cannot read may still hold a complete RTF
    rendition of the same order, starting at the first `{\\rtf` and running to
    the last closing brace. Converting that gives the real text — masthead,
    ABSTRACT and all — where converting the container gave binary.
    """
    i = raw.find(b"{\\rtf")
    if i < 0:
        return None
    j = raw.rfind(b"}")
    return raw[i : j + 1] if j > i else None


def convert_bytes(data: bytes, ext: str) -> str:
    """Run textutil over a blob held in a temp file with the right extension."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / f"in.{ext}"
        tmp.write_bytes(data)
        try:
            out = subprocess.run(
                ["textutil", "-convert", "txt", "-stdout", str(tmp)],
                capture_output=True,
                timeout=60,
            )
        except (subprocess.SubprocessError, OSError):
            return ""
    return out.stdout.decode("utf-8", "ignore")


def load_state() -> dict[str, dict]:
    """Resume support: one JSON object per processed file, appended as we go,
    so a killed run loses at most one record. Last line wins."""
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


def targets() -> set[str]:
    """The files recover_order_text.py gave up on as unopenable."""
    if not PRIOR_STATE.exists():
        sys.exit(f"no {PRIOR_STATE} — run recover_order_text.py first")
    out: set[str] = set()
    with PRIOR_STATE.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec["status"] == "EXTRACTION_FAILED":
                out.add(rec["source_file"])
            else:
                # Last-wins: a file later resolved some other way is not ours.
                out.discard(rec["source_file"])
    return out


def recover_one(source_file: str) -> dict:
    """Convert one document and return the verdict for it."""
    base = {"source_file": source_file, "order_text": None, "abstract": None}
    path = Path(source_file)
    if not path.exists():
        return {**base, "status": "EXTRACTION_FAILED", "detail": "file missing on disk"}

    ext = convertible_ext(path)
    if ext is None:
        # Lotus WordPro, zero-byte files, stubs. Reading the magic bytes rather
        # than trusting the .pdf extension is the whole point; a format textutil
        # cannot read is left exactly as it was rather than guessed at.
        return {**base, "status": "EXTRACTION_FAILED", "detail": "format textutil cannot read"}

    try:
        text = convert_to_text(path, ext)
        route = "textutil"
        if not plausible_text(text):
            # textutil handed back the file's own bytes. Some of these OLE2
            # containers hold a complete RTF rendition of the same order, so
            # try that before giving up; anything else is a real failure.
            text = ""
            rtf = embedded_rtf(path.read_bytes())
            if rtf:
                candidate = convert_bytes(rtf, "rtf")
                if plausible_text(candidate):
                    text, route = candidate, "textutil_embedded_rtf"
    except Exception as e:
        return {**base, "status": "EXTRACTION_FAILED",
                "detail": f"conversion raised {type(e).__name__}: {e}"[:200]}

    text = scrub_filler(text)
    if len(text.strip()) < MIN_BODY_CHARS:
        return {**base, "status": "EXTRACTION_FAILED",
                "detail": f"converted to {len(text.strip())} chars"}
    base["route"] = route

    try:
        rec = gp.parse_text(path, text, 1, extra_warnings=[RECOVERY_WARNING])
    except Exception as e:
        return {**base, "status": "EXTRACTION_FAILED",
                "detail": f"parse raised {type(e).__name__}: {e}"[:200]}

    body = (rec.get("order_text") or "").strip()
    abstract = (rec.get("abstract") or "").strip() or None

    if len(body) < MIN_BODY_CHARS:
        # The conversion worked; the parse could not bound an order block. The
        # abstract is still worth keeping if one was found — it is the single
        # most searchable field this record could gain.
        return {
            **base,
            "status": "NEEDS_MANUAL_REVIEW",
            "detail": f"converted {len(text.strip())} chars, no boundable order block",
            "abstract": abstract,
        }

    return {
        "source_file": source_file,
        "status": "FIXED",
        "detail": None,
        "route": base.get("route"),
        "order_text": body,
        "abstract": abstract,
        "has_table": rec.get("has_table"),
        "anchors_found": rec.get("anchors_found"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write corpus and database")
    ap.add_argument("--limit", type=int, default=0, help="process at most N files")
    ap.add_argument("--fresh", action="store_true", help="ignore prior state and redo")
    args = ap.parse_args()

    if not CORPUS_DIR.is_dir():
        sys.exit(f"no corpus at {CORPUS_DIR} — run this from the go-ingestion root")

    if args.fresh and STATE_FILE.exists():
        STATE_FILE.unlink()
    state = {} if args.fresh else load_state()
    print(f"{len(state)} files already processed in a previous run")

    want = targets()
    print(f"EXTRACTION_FAILED cohort: {len(want)} files")
    todo = sorted(w for w in want if w not in state)
    print(f"to process: {len(todo)}")
    if args.limit:
        todo = todo[: args.limit]
        print(f"limited to {len(todo)}")

    # --- pass 1: convert ---------------------------------------------------
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    counts: Counter = Counter()
    with STATE_FILE.open("a", encoding="utf-8") as out:
        for n, sf in enumerate(todo, 1):
            verdict = recover_one(sf)
            state[sf] = verdict
            counts[verdict["status"]] += 1
            out.write(json.dumps(verdict, ensure_ascii=False) + "\n")
            out.flush()
            if n % 250 == 0:
                done = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
                print(f"  {n}/{len(todo)}  {done}", flush=True)

    print("\nthis run:")
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
        json.dumps(
            {
                "cohort": len(want),
                "processed": len(state),
                "by_status": dict(overall),
                "recovered_body_chars": bodies,
                "records_with_abstract": new_abs,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"report -> {REPORT_FILE}")

    if not args.apply:
        print("\ndry run — nothing written. Re-run with --apply.")
        return 0

    # --- pass 2: write the corpus -----------------------------------------
    # Empty fields only, on both sides. A record that already has an abstract
    # keeps the one the cleanup settled; identity fields are not touched at all.
    wrote_body = wrote_abs = 0
    for yf in sorted(CORPUS_DIR.glob("*.jsonl")):
        tmp = yf.with_suffix(".jsonl.tmp")
        with yf.open(encoding="utf-8") as fin, tmp.open("w", encoding="utf-8") as fout:
            for line in fin:
                if not line.strip():
                    continue
                d = json.loads(line)
                v = state.get(d["source_file"])
                if v and v["status"] == "FIXED":
                    if not (d.get("order_text") or "").strip():
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
    print("now push with: python3 push_doc_recovery.py --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
