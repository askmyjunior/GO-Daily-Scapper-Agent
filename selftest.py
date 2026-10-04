#!/usr/bin/env python3
"""Check the machine before a sync touches anything.

Every module imports; Tesseract reads back a page it was given as an image — a
"scan" made here, with no text layer, so the OCR path is proven on the runner
itself rather than assumed; and each credential is tried for what the sync
needs it for, reporting only whether it worked. Writes nothing, costs nothing,
and never prints a credential: GitHub cannot show a saved secret back, so this
is the only way to know the six that were pasted in are right.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import urllib.error
import urllib.request
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fitz  # noqa: E402

import classify_new  # noqa: E402,F401
import goir_portal  # noqa: E402,F401
import pipeline as P  # noqa: E402
import sync  # noqa: E402,F401

WORDS = ("GOVERNMENT", "ANDHRA", "PRADESH", "ABSTRACT", "Revenue", "Sanction")

SECRETS = ("SUPABASE_DB_URL", "R2_ACCOUNT_ID", "R2_BUCKET", "R2_ACCESS_KEY_ID",
           "R2_SECRET_ACCESS_KEY", "GEMINI_API_KEY")

# An object known to be in the bucket (G.O.Rt.No.1290/2026, General
# Administration). Reading its metadata proves the R2 keys can read; nothing
# is downloaded or written.
R2_CANARY = "pdf/7f/b5/7fb53e8b2370374bdc356ec0af5cdb1423c3a69d0c7e3007ddffd1cd43015e13.pdf"


def first_line(e: BaseException) -> str:
    """The error, on one line. Line breaks are shown, not swallowed: the first
    cloud run's real fault was a line break inside a value, and cutting the
    message at it hid exactly that."""
    text = str(e).strip().replace("\n", " \u23ce ")
    return (text or type(e).__name__)[:300]


def check_credentials() -> int:
    """-> how many credentials failed. One attempt each, never retried: a
    repeated database authentication failure trips the pooler's circuit
    breaker and blocks every client for ~15 minutes."""
    bad = 0
    for name, why in P.TIDIED.items():
        print(f"  note {name}: had {why}; ignored")
    if os.environ.get("GITHUB_ACTIONS"):
        for name in SECRETS:
            if not os.environ.get(name):
                print(f"  x {name}: not set (Settings > Secrets and variables > Actions)")
                bad += 1

    try:
        import psycopg
        with psycopg.connect(P.ls.dsn(), connect_timeout=20) as conn, conn.cursor() as cur:
            cur.execute("select count(*) from public.departments")
            print(f"  ok SUPABASE_DB_URL: connects and reads ({cur.fetchone()[0]} departments)")
    except BaseException as e:  # noqa: BLE001 — dsn() exits with its own message
        print(f"  x SUPABASE_DB_URL: {first_line(e)}")
        bad += 1

    try:
        import upload_r2 as ur
        cfg = ur.creds()
        ur.client(cfg).head_object(Bucket=cfg["R2_BUCKET"], Key=R2_CANARY)
        print("  ok R2_ACCOUNT_ID, R2_BUCKET, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY: the bucket reads")
    except BaseException as e:  # noqa: BLE001
        print(f"  x R2 keys: {first_line(e)}")
        bad += 1

    try:
        req = urllib.request.Request(
            "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1",
            headers={"x-goog-api-key": P.cg.api_key()})
        with urllib.request.urlopen(req, timeout=30) as resp:
            json.loads(resp.read())
        print("  ok GEMINI_API_KEY: accepted (listing models is free)")
    except urllib.error.HTTPError as e:
        print(f"  x GEMINI_API_KEY: rejected, HTTP {e.code}")
        bad += 1
    except BaseException as e:  # noqa: BLE001
        print(f"  x GEMINI_API_KEY: {first_line(e)}")
        bad += 1
    return bad


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


def check_tesseract() -> int:
    if not shutil.which("tesseract"):
        print("  tesseract not installed here; OCR check skipped")
        return 0
    path = str(Path(tempfile.mkdtemp()) / "scan.pdf")
    make_scan(path)
    with fitz.open(path) as d:
        assert not d[0].get_text().strip(), "the test scan has a text layer; it proves nothing"
    text = P.tesseract_pdf(path)
    missed = [w for w in WORDS if w.lower() not in text.lower()]
    if missed:
        print(f"  x tesseract missed {missed}; it read: {text[:300]!r}")
        return 1
    print(f"  ok tesseract read all {len(WORDS)} words back from an image-only page")
    return 0


def main() -> int:
    print(f"python {sys.version.split()[0]}, pymupdf {fitz.VersionBind}")
    print(f"antiword: {'yes' if shutil.which('antiword') else 'no'}; "
          f"OCR engine: {P.ocr_engine() or 'none'}")
    bad = check_tesseract()
    print("credentials:")
    bad += check_credentials()
    print("self-test passed" if not bad else f"self-test FAILED: {bad} problem(s) above")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
