"""
Replace the corpus's account of *why* 5,562 orders have no body with a true one.

Reads out/no_text_census.json — written by classify_no_text.py, which opened
every one of these files and read it — and rewrites three derived fields:

    text_recovery_status   what kind of record this is
    text_recovery_method   how that was established
    text_recovery_note     the specific fact, where there is one worth keeping

order_text is not touched. Neither are go_number, go_date, department, the
abstract, or any source file. Nothing here reads a PDF; the reading was done
by the census and this script only carries its verdicts to the database.

WHY A NEW STATUS
-----------------
The brief named five statuses: FIXED, VERIFIED, NEEDS_MANUAL_REVIEW,
OCR_REQUIRED, EXTRACTION_FAILED. None of them is true of a document whose
entire content is the word "Confidential". It is not fixed or verified; there
is nothing to review, because we know exactly what it is; OCR would find the
same one word; and EXTRACTION_FAILED — what the corpus says today — asserts a
defect in our pipeline that did not occur. 4,579 records are in this position,
so the gap is not a rounding error.

NO_ORDER_IN_SOURCE is therefore added, and it makes a claim about the document
rather than about us: the file was read successfully and contains no order.
That distinction is the entire point of the exercise. Somebody trying to
establish whether an order exists is badly served by being told our extractor
failed, when what actually happened is that the State of Andhra Pradesh
declined to publish it.

WHAT IS DELIBERATELY LEFT ALONE
--------------------------------
Records the boundary-recovery pass already read and ruled on keep its ruling.
The census works from the extracted text; that pass worked from the document
and gave a reason ("legacy Telugu font, glyphs decode to mojibake"). Where the
two disagree the better-informed verdict wins, and it is never overwritten
here — see KEEP_PRIOR.

RUN THIS LAST
--------------
push_ocr_recovery.py rewrites status and method for every record in its state
file, not only the ones it recovered, and 2,005 of those carry its verdict
EXTRACTION_FAILED / ocr_attempted_page_blank — the exact sentence this script
exists to remove. Running the two in the wrong order silently reinstates it on
2,005 records. Observed, not theorised: it happened once.

Re-running this afterwards is the fix, and is safe. The write is idempotent,
and the `order_text is null or order_text = ''` guard means a record that has
gained a body in the meantime is skipped rather than told it has no order.

Usage:
    python3 push_no_text_classification.py            # report, write nothing
    python3 push_no_text_classification.py --apply    # after any OCR push
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from rebuild_search_index import dsn

CENSUS_FILE = Path("out/no_text_census.json")

# verdict -> (status, method, note). The note is a constant where the fact is
# the same for every record in the class, and None where the census carries a
# per-record fact worth keeping instead (see note_for).
PLAN: dict[str, tuple[str, str, str | None]] = {
    "withheld_confidential": (
        "NO_ORDER_IN_SOURCE",
        "source_withheld_confidential",
        "The published document's entire content is the word CONFIDENTIAL; "
        "no order text was released.",
    ),
    "source_document_empty": (
        "NO_ORDER_IN_SOURCE",
        "source_document_empty",
        "The published file contains no text and no page content.",
    ),
    "no_order_content": (
        "NO_ORDER_IN_SOURCE",
        "source_no_order_content",
        "The published file carries only a letterhead or a subject line; "
        "no operative order was written into it.",
    ),
    "go_not_issued": (
        "NO_ORDER_IN_SOURCE",
        "source_go_not_issued",
        "The published document states that the G.O. number was allotted but "
        "no order was issued against it.",
    ),
    "order_cancelled": (
        "NO_ORDER_IN_SOURCE",
        "source_order_cancelled",
        "The published document states only that the order is cancelled.",
    ),
    "portal_test_document": (
        "NO_ORDER_IN_SOURCE",
        "source_portal_test_document",
        "The published file is a portal test document, not a Government Order.",
    ),
    "awaiting_upload": (
        "NO_ORDER_IN_SOURCE",
        "source_awaiting_upload",
        "The published file is a placeholder reading 'Text to be uploaded'; "
        "the department has not yet published the order.",
    ),
    # Not a description but a task: the page has ink on it and nobody has read
    # it. These are real orders — one is an eight-page General Administration
    # order whose only text layer is an eOffice routing stamp.
    #
    # The census measures ink and text; it cannot see whether OCR has been
    # tried. Only the prior method knows that, and 70 of these 172 have been
    # through OCR already, so a blanket "not yet attempted" would be a fresh
    # false statement in place of the one being removed. ocr_split() decides.
    "scanned_needs_ocr": ("", "", None),
    # Identifiable, and unreadable by anything available. note_for names the
    # format, because that is the whole of the useful answer.
    "unreadable_legacy_format": (
        "NEEDS_MANUAL_REVIEW",
        "none_legacy_format_unreadable",
        None,
    ),
    # Text is present and an order block has to be found in it. Status and
    # method are left exactly as they are: this pass has nothing to add, and
    # the boundary-recovery run is what will change them.
    "recoverable_text": ("", "", None),
}

# Methods set by a pass that read the document and gave its reason. Better
# evidence than the census, so the census does not get to overrule them.
KEEP_PRIOR = {
    "llm_read_unreliable_encoding",
    "llm_read_not_an_order",
    "llm_read_document_mismatch",
    "llm_read_no_order_block",
}


def ocr_split(prior: str) -> tuple[str, str, str | None] | None:
    """What to say about a scanned page with ink on it and no text layer.

    Whether OCR is owed depends on whether OCR has run, which the census cannot
    see and the prior method can. Returning None means leave the record alone.
    """
    if prior.startswith("triage_"):
        # Never in the OCR cohort at all. 102 records, and the reason the
        # eight-page GAD order from 2016 reads as "no text found on the page".
        return (
            "OCR_REQUIRED",
            "ocr_not_yet_attempted",
            "Scanned document with no usable text layer — the only extractable "
            "text is an eOffice routing stamp. Text recognition has not yet "
            "been run over it.",
        )
    if prior == "ocr_attempted_page_blank":
        # OCR found nothing, yet the page is visibly inked. One of the two is
        # wrong and this pass cannot say which, so it says exactly that.
        return (
            "NEEDS_MANUAL_REVIEW",
            "ocr_returned_nothing_on_inked_page",
            "Text recognition returned nothing, but the page carries printed "
            "content. The page is not blank; why OCR read nothing is unresolved.",
        )
    # ocr_readable_no_boundable_order_block: OCR text exists and the order
    # block has to be found in it — the boundary pass's job, not this one.
    # ocr_attempted_unreadable_script: already accurate.
    return None


def note_for(rec: dict, const: str | None) -> str | None:
    if rec["verdict"] == "unreadable_legacy_format":
        return (
            f"Source file is {rec['format']}, despite its .pdf name. "
            "Text is present in the file but cannot be extracted faithfully; "
            "the original is preserved and can be opened with the right tool."
        )
    return const


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    import psycopg

    if not CENSUS_FILE.exists():
        raise SystemExit(f"no {CENSUS_FILE} — run classify_no_text.py first")
    census = json.loads(CENSUS_FILE.read_text(encoding="utf-8"))
    print(f"census: {len(census)} records")

    rows: list[tuple[str, str, str, str | None]] = []
    skipped = Counter()
    for rec in census:
        status, method, const = PLAN[rec["verdict"]]
        if rec["verdict"] == "scanned_needs_ocr":
            split = ocr_split(rec["prior_method"])
            if split is None:
                skipped["inked scan — OCR text already exists, left for "
                        "boundary recovery"] += 1
                continue
            status, method, const = split
        if not status:
            skipped["recoverable — left for boundary recovery"] += 1
            continue
        if rec["prior_method"] in KEEP_PRIOR:
            skipped["already ruled on by the boundary pass"] += 1
            continue
        rows.append((rec["source_file"], status, method, note_for(rec, const)))

    print(f"\nto write: {len(rows)}")
    for k, v in Counter((r[1], r[2]) for r in rows).most_common():
        print(f"  {v:>6}  {k[0]:<20} {k[1]}")
    print("\nleft alone:")
    for k, v in skipped.most_common():
        print(f"  {v:>6}  {k}")

    if not args.apply:
        print("\ndry run — pass --apply to write.")
        return 0

    files = [r[0] for r in rows]
    with psycopg.connect(dsn(), autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute("set statement_timeout = 600000")

            # The guard that makes this safe to re-run and impossible to get
            # wrong: a row that has gained a body since the census was taken is
            # no longer a no-text record, and must not be told it has no order.
            cur.execute(
                """
                update ap_government_orders g
                   set text_recovery_status = v.st,
                       text_recovery_method = v.me,
                       text_recovery_note   = v.nt
                  from (select unnest(%s::text[]) sf,
                               unnest(%s::text[]) st,
                               unnest(%s::text[]) me,
                               unnest(%s::text[]) nt) v
                 where g.source_file = v.sf
                   and (g.order_text is null or g.order_text = '')
                """,
                (files, [r[1] for r in rows], [r[2] for r in rows],
                 [r[3] for r in rows]),
            )
            wrote = cur.rowcount
            print(f"\nrows written: {wrote}")
            if wrote != len(rows):
                print(f"  note: {len(rows) - wrote} row(s) gained a body since "
                      f"the census and were left alone")
        conn.commit()

    print("\ndone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
