#!/usr/bin/env python3
"""Check the machine before a sync touches anything.

Every module imports, and Tesseract reads back a page it was given as an image
— a "scan" made here, with no text layer, so the OCR path is proven on the
runner itself rather than assumed. Writes nothing anywhere.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fitz  # noqa: E402

import classify_new  # noqa: E402,F401
import goir_portal  # noqa: E402,F401
import pipeline as P  # noqa: E402
import sync  # noqa: E402,F401

WORDS = ("GOVERNMENT", "ANDHRA", "PRADESH", "ABSTRACT", "Revenue", "Sanction")


def make_scan(path: str) -> None:
    """Text drawn on a page, rasterised, and wrapped back up as an image-only PDF."""
    src = fitz.open()
    page = src.new_page()
    page.insert_text((72, 100), "GOVERNMENT OF ANDHRA PRADESH", fontsize=18)
    page.insert_text((72, 150), "ABSTRACT", fontsize=16)
    page.insert_text((72, 200), "Revenue Department - Sanction of funds - Orders - Issued.", fontsize=13)
    png = page.get_pixmap(dpi=200).tobytes("png")
    scan = fitz.open()
    sp = scan.new_page(width=page.rect.width, height=page.rect.height)
    sp.insert_image(sp.rect, stream=png)
    scan.save(path)


def main() -> int:
    print(f"python {sys.version.split()[0]}, pymupdf {fitz.VersionBind}")
    print(f"antiword: {'yes' if shutil.which('antiword') else 'no'}; "
          f"OCR engine: {P.ocr_engine() or 'none'}")
    if not shutil.which("tesseract"):
        print("tesseract not installed here; OCR check skipped")
        return 0
    path = str(Path(tempfile.mkdtemp()) / "scan.pdf")
    make_scan(path)
    with fitz.open(path) as d:
        assert not d[0].get_text().strip(), "the test scan has a text layer; it proves nothing"
    text = P.tesseract_pdf(path)
    missed = [w for w in WORDS if w.lower() not in text.lower()]
    if missed:
        print(f"Tesseract missed {missed}; it read: {text[:300]!r}")
        return 1
    print(f"tesseract read all {len(WORDS)} words back from an image-only page")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
