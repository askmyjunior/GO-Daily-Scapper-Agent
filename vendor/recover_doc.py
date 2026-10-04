"""
Recover the GOs in this corpus that are word-processor documents saved with a
.pdf extension. PyMuPDF cannot open them, so they land in the corpus as
`pdf_open_failed` records carrying filename-only fields.

The real format is read from each file's magic bytes, not its extension. Of
the 4,043 such files: 3,909 are MS Word (OLE2), 5 are RTF and 1 is a zipped
.docx — all formats macOS `textutil` reads natively. This converts each one to
text and runs it through go_parser.parse_text(), producing a record of the
identical 26-field shape, tagged with a `recovered_from_doc_conversion`
warning so a converted document stays distinguishable from one read natively
out of a PDF.

Calls no model. Conversion is a format change only — no field is invented;
every value still comes from go_parser's anchors and regexes.

Not every file is recoverable, and the unrecoverable ones keep their original
pdf_open_failed record: 97 are zero-byte, 18 are Lotus WordPro (which textutil
cannot read), ~13 are truncated stubs, and a large share of the Word documents
are genuinely empty — 26KB of Word metadata with no order text in them.

Usage:
    python3 recover_doc.py out/corpus/            # dry run, reports only
    python3 recover_doc.py out/corpus/ --apply    # rewrite the year files
"""

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

import go_parser as gp

MIN_USABLE_CHARS = 200
RECOVERY_WARNING = "recovered_from_doc_conversion"

# Magic-byte prefix -> the extension textutil must see to read that format.
# These files are overwhelmingly OLE2 Word, but a handful were saved as RTF or
# as modern zipped .docx, and textutil reads all three. Anything not listed
# (Lotus WordPro "WordPro\0", zero-byte files, Windows .lnk stubs) is left
# alone rather than guessed at.
MAGIC_EXT = {
    b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1": "doc",   # OLE2 compound document
    b"{\\rtf": "rtf",
    b"PK\x03\x04": "docx",                        # zip container
}


def convertible_ext(path: Path):
    """The extension to convert this file as, or None if its format is not one
    textutil can read. Decided from the file's own magic bytes — never from the
    (wrong) .pdf extension it was saved with."""
    try:
        with path.open("rb") as f:
            head = f.read(8)
    except OSError:
        return None
    for magic, ext in MAGIC_EXT.items():
        if head.startswith(magic):
            return ext
    return None


def convert_to_text(path: Path, ext: str) -> str:
    """textutil dispatches on file extension, so the .pdf-named original is
    copied to a temp name carrying its real extension before conversion."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / f"in.{ext}"
        tmp.write_bytes(path.read_bytes())
        try:
            out = subprocess.run(
                ["textutil", "-convert", "txt", "-stdout", str(tmp)],
                capture_output=True,
                timeout=60,
            )
        except (subprocess.SubprocessError, OSError):
            return ""
    return out.stdout.decode("utf-8", "ignore")


def recover(rec: dict):
    """Returns a replacement record, or None if the file is not recoverable."""
    path = Path(rec["source_file"])
    ext = convertible_ext(path)
    if ext is None:
        return None
    text = convert_to_text(path, ext)
    if len(text.strip()) < MIN_USABLE_CHARS:
        return None
    # page_count=1: converted text has no page structure. text_quality is a
    # chars-per-page proxy for a bad OCR layer, which does not apply here —
    # the text is native and exact, not scanned.
    return gp.parse_text(path, text, 1, extra_warnings=[RECOVERY_WARNING])


def critical_anchor_count(rec: dict) -> int:
    return sum(1 for a in gp.CRITICAL_ANCHORS if a in (rec.get("anchors_found") or []))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("corpus", type=Path, help="Directory of <year>.jsonl files")
    ap.add_argument("--apply", action="store_true", help="Rewrite the year files in place")
    args = ap.parse_args()

    attempted = recovered = well_parsed = 0

    for year_file in sorted(args.corpus.glob("*.jsonl")):
        records = [json.loads(l) for l in year_file.open() if l.strip()]
        changed = 0

        for i, rec in enumerate(records):
            if not any(w.startswith("pdf_open_failed") for w in rec.get("warnings", [])):
                continue
            attempted += 1
            new = recover(rec)
            if new is None:
                continue
            recovered += 1
            if critical_anchor_count(new) >= 4:
                well_parsed += 1
            records[i] = new
            changed += 1

        if changed and args.apply:
            tmp = year_file.with_suffix(".jsonl.partial")
            with tmp.open("w") as f:
                for r in records:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            tmp.replace(year_file)
        if changed:
            print(f"  {year_file.stem}: {changed} recovered", flush=True)

    print()
    print(f"pdf_open_failed records attempted : {attempted}")
    print(f"converted to usable text          : {recovered}")
    print(f"  of which parse well (4-5 anchors): {well_parsed}")
    print(f"not recoverable                   : {attempted - recovered}")
    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to rewrite the year files.")


if __name__ == "__main__":
    main()
