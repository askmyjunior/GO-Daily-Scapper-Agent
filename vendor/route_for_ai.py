"""
Split go_parser.py JSONL output into queues, one per distinct remediation.

Routing is decided purely from flags the parser already emitted (`warnings`,
`has_table`). This performs NO extraction and calls no AI/LLM, per the
project's "never LLM in the core pipeline" rule — it only decides *where a
record goes next*, never what its fields are.

Buckets, in precedence order (a record lands in exactly one):

  1. unreadable_not_pdf.jsonl  — `pdf_open_failed`. The file is not a PDF at
     all; most of these are MS Word .doc files saved with a .pdf extension
     (OLE2 magic D0 CF 11 E0). Every field is either null or a filename-only
     guess. Owner: a format-conversion step, not an AI text pass.

  2. unreadable_no_text.jsonl  — `no_text_layer`. A real PDF, but image-only
     scans with no extractable characters. Owner: OCR.

  3. needs_ai_review.jsonl     — `has_table == True`. PyMuPDF flattened a
     table in order_text (row/column boundaries lost). Every non-table field
     on the record is still trustworthy; only the table portion of order_text
     is in question. Owner: table reconstruction.

  4. needs_review.jsonl        — readable and table-free, but the parse is
     partial: a CRITICAL_ANCHOR is missing, the body disagreed with the
     filename (`filename_body_mismatch`), or an anchor resolved but its value
     would not parse (`invalid_go_date` / `date_unparsed` /
     `go_number_unparsed`). Fields that were extracted are
     honest; the record is simply incomplete or self-contradictory. Owner:
     human or AI verification.

  5. resolved_by_sidecar.jsonl — was bucket 4, but every defect it was queued for is
     now demonstrably filled, and at least one of those values lives in a sidecar
     rather than in the record. Complete *after the join*, so it is kept out of
     clean.jsonl, whose guarantee is that it needs no join.

  6. clean.jsonl               — none of the above. Consumable as-is.

Buckets 1/2 are disjoint from 3/4 in practice (a record with no text layer
has no order_text to hold a table), but precedence is applied explicitly
rather than relying on that.

**Precedence decides where a record goes. It must not decide what is wrong
with it.** Bucket 3 is keyed on `has_table` alone and outranks bucket 4, so a
record that has a table *and* a missing anchor was filed under "table
reconstruction" and its missing field was invisible — 235 records were in that
state. Every record therefore also gets a row in `routing_index.jsonl`:

    {source_file, filename, bucket, also_applies[], problems[], stale[], notes[],
     extraction_confidence}

`bucket` is the file the record was written to. `also_applies` lists the other buckets
whose tests also fired. The bucket files stay disjoint and their totals still add up,
so downstream consumers are unaffected; the index is where you look to find a problem
precedence would otherwise hide.

`notes` is the one field here that does not name a defect. Some records have two
attested values for one field and the locked record can only hold one: the document
prints GO number 127, the GOIR register filed it under 129, and both readings are
real. Those records are correct, so nothing demotes them out of `clean` -- which is
exactly why they need announcing, because otherwise they look unremarkable and a
consumer never learns to call `portal_number_from` / `portal_date_from` for the
other half. `problems` is "something is wrong here"; `notes` is "there is a second
column and you are only seeing one".

**A warning is a record of what the parser saw, not a statement about today.**
`warnings` is part of the locked 26-field record and is never rewritten, so a later
deterministic pass that fills a gap cannot retract the warning that named it. 3,763
records sat in `needs_review` for fields that are now present: 2,521 for a department
`resolve_departments.py` has since resolved, and 1,242 for a `dated` anchor whose date
the parser had in fact extracted (`raw: "30 -4-2008"` -> `iso: 2008-04-30`). Routing
therefore asks *is the value on disk now?*, not *did a warning once fire?*, and records
the outdated warnings in the index as `stale`.

Only bucket 4 can be promoted out of. Buckets 1 and 2 are permanent facts about the
bytes on disk and bucket 3 is work owned elsewhere; none of them describes a value that
a later pass could supply. Two bucket-4 defects can never go stale either: a
`filename_body_mismatch` is a contradiction between two *populated* signals rather than
a gap, and an unparsed date or number would not have warned had the field been usable.

The department sidecar is an **optional** input. It can only ever clear a defect, never
create one, so when `out/departments/` is absent the router says so and falls back to
exactly its former behaviour rather than failing.

Usage:
    python3 route_for_ai.py out/corpus/2015.jsonl --outdir out/routed/
    python3 route_for_ai.py out/corpus/ --outdir out/routed/
"""

import argparse
import json
from collections import Counter
from pathlib import Path

from go_parser import CRITICAL_ANCHORS

BUCKETS = [
    "unreadable_not_pdf",
    "unreadable_no_text",
    "needs_ai_review",
    "needs_review",
    "resolved_by_sidecar",
    "clean",
]

DEPARTMENTS = Path("out/departments")
MISMATCHES = Path("out/mismatches")
GOVERNMENT = Path("out/government")
DATENUM = Path("out/datenum")
GODATE = Path("out/godate")
GONUMBER = Path("out/gonumber")


def load_gonumber(path=GONUMBER):
    """`source_file` -> the `resolve_gonumber.py` (§21) row, or `{}` if absent.

    Optional like the rest. Dropping it restores 309 GO numbers the parser took
    out of citation lists and 84 it borrowed from them, so "optional" here means
    "the pipeline still runs", not "nothing much changes".

    It also carries `go_number_goir_register` for every record -- the number the
    GOIR register filed the document under -- which is a fact no other sidecar
    holds and which `portal_number_from` is the accessor for.
    """
    out = {}
    if not path.is_dir():
        return out
    for f in sorted(path.glob("*.jsonl")):
        with f.open() as fh:
            for line in fh:
                line = line.strip()
                if line:
                    r = json.loads(line)
                    out[r["source_file"]] = r
    return out


def load_godate(path=GODATE):
    """`source_file` -> the `resolve_godate.py` (§19) row, or `{}` if absent.

    Optional like the rest, but it is the only sidecar that can REMOVE a value: a
    GO date that is corrupt and that the document cannot replace is left empty on
    purpose. Dropping this sidecar therefore restores the impossible dates
    (`0202-12-28`, `3009-03-16`) rather than merely leaving a gap open -- worth
    knowing before anyone treats `out/godate/` as cosmetic.
    """
    out = {}
    if not path.is_dir():
        return out
    for f in sorted(path.glob("*.jsonl")):
        with f.open() as fh:
            for line in fh:
                line = line.strip()
                if line:
                    r = json.loads(line)
                    out[r["source_file"]] = r
    return out


def load_datenum(path=DATENUM):
    """`source_file` -> the `resolve_datenum.py` row, or `{}` if absent.

    Optional on the same terms as the other three. Without it the 3,339 date and
    2,436 go_number defects stay open, which is the router's behaviour before that
    pass existed.
    """
    out = {}
    if not path.is_dir():
        return out
    for f in sorted(path.glob("*.jsonl")):
        with f.open() as fh:
            for line in fh:
                line = line.strip()
                if line:
                    r = json.loads(line)
                    out[r["source_file"]] = r
    return out


def load_government(path=GOVERNMENT):
    """`source_file` -> the `resolve_government.py` row, or `{}` if absent.

    Optional on the same terms as the other two. Without it every
    `missing_anchor:gov_line` stays open and the 31 records whose `government` the
    parser drifted onto stay wrong -- which is exactly the state before this pass
    existed.
    """
    out = {}
    if not path.is_dir():
        return out
    for f in sorted(path.glob("*.jsonl")):
        with f.open() as fh:
            for line in fh:
                line = line.strip()
                if line:
                    r = json.loads(line)
                    out[r["source_file"]] = r
    return out


def load_departments(path=DEPARTMENTS):
    """`source_file` -> canonical department name, or `{}` if the sidecar is absent.

    Optional by design. The router's contract is that it runs on parser output alone,
    and a sidecar can only ever *clear* a defect, never create one -- so when it is
    missing the router degrades to exactly its former behaviour instead of failing.
    `main()` says so out loud rather than silently routing on less evidence.
    """
    out = {}
    if not path.is_dir():
        return out
    for f in sorted(path.glob("*.jsonl")):
        with f.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                if r.get("canonical_department"):
                    out[r["source_file"]] = r["canonical_department"]
    return out


def load_mismatches(path=MISMATCHES):
    """`source_file` -> the `resolve_mismatches.py` row, or `{}` if absent.

    Optional on the same terms as `load_departments`. Without it, every
    `filename_body_mismatch` stays open, which is precisely the router's
    behaviour before that pass existed.
    """
    out = {}
    if not path.is_dir():
        return out
    for f in sorted(path.glob("*.jsonl")):
        with f.open() as fh:
            for line in fh:
                line = line.strip()
                if line:
                    r = json.loads(line)
                    out[r["source_file"]] = r
    return out


def truncated_number(row: dict) -> bool:
    """Did §16 confirm a GO number that the page cuts off mid-digit-run?

    28 of §16's 702 `go_number` adjudications (4.0%) are truncations it certified
    as `body_corroborated`. The shape is always the same -- the stored evidence
    stops immediately before a letter that is an OCR misread of a digit:

        G.O.Ms.No.44  + `O`  -> really 440, filename says 440
        G.O.Ms.No.8   + `l`  -> really 81,  filename says 81
        G.O.Ms.No.2   + `S`  -> really 25,  filename says 25
        G.O.MS.No.0   + `Z`  -> really 02,  filename says 2

    In 23 of the 28 the filename value is exactly the body value with one more
    digit, which is what a truncation looks like from the outside.

    The underlying mistake is that `G.O.Ms.No.44` really does occur in a document
    reading `G.O.Ms.No.44O` -- so the evidence check passed on a *prefix* of the
    number. A prefix is not corroboration; it is the same string with information
    removed. §16's own hard-won lesson was "never seed a checker from the thing it
    checks", and this is its neighbour: never accept evidence that a shorter claim
    is contained in a longer one.

    This is detected rather than repaired. Rewriting the number would be inventing
    a value, and re-running §16 against a fixed rule is a separate decision. What
    the router can do without overstepping is refuse to call these clean.
    """
    for fd in row.get("fields") or []:
        if fd.get("field") != "go_number":
            continue
        if fd.get("field") not in (row.get("resolved_fields") or []):
            continue
        ev = fd.get("document_evidence") or ""
        header = row.get("header_text") or ""
        i = header.find(ev)
        if i < 0 or not ev:
            continue
        nxt = header[i + len(ev):i + len(ev) + 1]
        if nxt and nxt.isalpha():
            return True
    return False


class Sidecars:
    """The deterministic passes the router consults but never requires.

    Each one can only clear a defect the parser recorded, never create one, so a
    missing sidecar degrades the router to an earlier, more conservative version
    of itself rather than breaking it. Bundled into one object because there are
    now two of them and there will be more; threading them as separate positional
    arguments through `classify` and `route` was already getting silly.
    """

    def __init__(self, departments=None, mismatches=None, government=None,
                 datenum=None, godate=None, gonumber=None):
        self.departments = departments or {}
        self.mismatches = mismatches or {}
        self.government = government or {}
        self.datenum = datenum or {}
        self.godate = godate or {}
        self.gonumber = gonumber or {}

    # The one place the resolution order for go_date and go_number is written
    # down. Both helpers walk it and `validate_datenum.py` re-asserts it, so a
    # future pass cannot quietly acquire a fourth opinion.
    #
    #   0. out/gonumber/     §21, go_number ONLY. It re-reads the number the
    #                        document prints on itself with a reader that
    #                        survives the scan, and it is the only tier that has
    #                        ever re-read the WHOLE digit run -- which is what
    #                        §16's substring test could not do. It abstains where
    #                        it could not settle; it does not veto.
    #   0. out/godate/       §19, go_date ONLY. It outranks §16 for one reason:
    #                        it is the only pass that read the document while
    #                        blind to the filename, so it is the only one whose
    #                        answer cannot be the filename's date wearing a
    #                        document's label. It also rules a date UNUSABLE --
    #                        `0202-12-28` is deleted, not replaced -- and a veto
    #                        that anything else can outvote is not a veto.
    #                        It speaks for 218 records and abstains on the rest.
    #   1. out/mismatches/   §16 re-opened the document and ADJUDICATED this
    #                        field against the record's own value. It is the only
    #                        tier allowed to overrule the record, and it has to
    #                        be, because the record's value is the thing it
    #                        overruled. 2,835 dates and 702 numbers.
    #   2. the record        the parser's own reading, where nothing disputes it.
    #   3. out/datenum/      §18 fills only what is still empty, and by
    #                        construction never touches a field §16 settled
    #                        (`rd.load_settled` gates it), so 1 and 3 cannot
    #                        both answer.
    #
    # This order is a CORRECTION of earlier behaviour, not a refinement. Both
    # helpers read the record and then out/datenum/ and never out/mismatches/ at
    # all, so 2,835 dates and 702 numbers that §16 had already established --
    # including the 28 GO numbers verified by hand against the source PDFs --
    # sat on disk where no consumer could reach them. The routing was not wrong:
    # `self_sufficient` already keeps those records out of `clean.jsonl`. But
    # their bucket is `resolved_by_sidecar`, whose contract is "complete after
    # the join", and the function performing the join read the wrong sidecar.
    def settled_from(self, rec, field):
        """§16's adjudicated value for `field`, or None if it settled nothing.

        A field counts as settled only when it is named in `resolved_fields`. A
        row can carry a `fields` entry whose verdict is `undetermined` or
        `document_disagrees`, and reading `resolved_value` straight off that
        would promote a non-decision into an answer.
        """
        row = self.mismatches.get(rec.get("source_file")) or {}
        if field not in set(row.get("resolved_fields") or []):
            return None
        fd = next((f for f in (row.get("fields") or [])
                   if f.get("field") == field), None)
        return fd.get("resolved_value") if fd else None

    def date_from(self, rec):
        """The go_date this record has *today*, and where it came from.

        Returns (iso, source) with source in {"godate_sidecar", "record",
        "mismatch_sidecar", "sidecar", None}. The caller usually needs both:
        whether a value exists decides routing out of `needs_review`, and whether
        it exists *in the record* decides whether `clean.jsonl` can hold it
        standalone.

        The §19 branch has an early `return None` that the others do not. When
        §19 examined a record and could not establish its date, the record has NO
        go_date -- and must not fall through to the lower tiers, because the
        value waiting there is the corrupt one §19 just rejected. Falling through
        would restore `3009-03-16` and call it a success.
        """
        g = self.godate.get(rec.get("source_file")) or {}
        if g.get("go_date"):
            return g["go_date"], "godate_sidecar"
        if g.get("status") == "unresolved_manual_verification":
            return None, None
        settled = self.settled_from(rec, "go_date")
        if settled:
            return settled, "mismatch_sidecar"
        iso = ((rec.get("go_meta") or {}).get("go_date") or {}).get("iso")
        if iso:
            return iso, "record"
        row = self.datenum.get(rec.get("source_file")) or {}
        if row.get("go_date"):
            return row["go_date"], "sidecar"
        return None, None

    def portal_date_from(self, rec):
        """`Date Uploaded on GOIR Portal` -- the date in the source filename.

        A separate accessor from `date_from`, and deliberately not a tier inside
        it. These are two facts about two different events, and the whole point
        of §19 is that a consumer that can silently substitute one for the other
        will eventually do so. There is nothing to adjudicate here: the filename
        is on the record, and this is a pure function of it.
        """
        row = self.godate.get(rec.get("source_file")) or {}
        return row.get("date_uploaded_goir_portal")

    def portal_number_from(self, rec):
        """`GO Number on the GOIR register` -- the number in the source filename.

        A separate accessor, deliberately not a tier inside `number_from`. On 113
        records the document prints one number and the register filed it under
        another, both readings are attested, and neither is an error. One column
        asked to hold both facts will eventually be asked which one it is
        holding. Exactly the `portal_date_from` argument, for exactly the same
        reason.
        """
        row = self.gonumber.get(rec.get("source_file")) or {}
        return row.get("go_number_goir_register")

    def number_from(self, rec):
        """As `date_from`, for go_number.

        `is not None` throughout: two records in this corpus have go_number 0, and
        truthiness would report a present number as missing.

        §21 leads, and outranking §16 is the whole point of it. §16 verified GO
        numbers with a substring test, which truncates: it read `2` out of
        `G.O.Ms.No.2 1 1` and `1` out of `G.O. Ms. No.1 18`. 105 of the 113
        document-attested conflicts and 49 of the 309 corrections carry a
        `mismatch_sidecar` value, so a tier that could not outrank §16 could not
        reach them at all.

        Where §21 could not settle a record it ABSTAINS rather than vetoing --
        the one place this differs from `date_from`, and the difference is not an
        oversight. §19 vetoes because the value it rejected was a date that
        cannot exist, and letting `3009-03-16` fall through would restore it.
        §21's unresolved rows are the opposite situation: the stored number was
        never shown to be wrong, only unconfirmed. Dropping it would destroy 63
        readings to express uncertainty, which is not a trade this pipeline makes.
        """
        g = self.gonumber.get(rec.get("source_file")) or {}
        if g.get("go_number") is not None:
            return g["go_number"], "gonumber_sidecar"
        settled = self.settled_from(rec, "go_number")
        if settled is not None:
            return settled, "mismatch_sidecar"
        num = (rec.get("go_meta") or {}).get("go_number")
        if num is not None:
            return num, "record"
        row = self.datenum.get(rec.get("source_file")) or {}
        if row.get("go_number") is not None:
            return row["go_number"], "sidecar"
        return None, None


# Is the field a missing anchor was supposed to yield actually present *today*?
#
# A warning records what the parser saw at parse time. It is never retracted, because
# `warnings` is part of the locked 26-field record and later passes do not rewrite it.
# So a record can name a missing field that a subsequent deterministic pass has since
# filled -- 2,521 records were still queued for a department the sidecar resolved, and
# 1,242 for a `dated` anchor whose date the parser had extracted anyway (`raw:
# "30 -4-2008"` -> `iso: 2008-04-30`). The anchor genuinely was not found; the value
# genuinely is there. Routing should answer the second question, not the first.
#
# Each predicate takes the record and the `Sidecars` bundle, because "is it there
# today" is a question two of them can answer.
ANCHOR_FILLED = {
    "dept_department": lambda r, s: bool(
        (r.get("department") or {}).get("department_name")
        or s.departments.get(r.get("source_file"))),
    # Via the bundle, so the date/number sidecar counts as "filled today" on the
    # same footing as the department one -- and so go_number 0 is not read as absent.
    "go_number": lambda r, s: s.number_from(r)[0] is not None,
    "dated": lambda r, s: s.date_from(r)[0] is not None,
    "gov_line": lambda r, s: bool(
        r.get("government")
        or (s.government.get(r.get("source_file")) or {}).get("government")),
    "abstract": lambda r, s: bool(r.get("subject")),
}


# Every routing test, in precedence order: (bucket, problem name, predicate).
# `classify()` still returns the first match and the bucket files stay disjoint,
# so nothing downstream changes. What this restructuring buys is `problems()`,
# which evaluates ALL of them.
#
# Why that matters: `has_table` outranks every `needs_review` test, so a record
# that has a table *and* a missing anchor was filed under "table reconstruction"
# and its missing field became invisible. 235 records were in exactly that state
# -- 69 with no department, 54 with no order block, 48 with no gov_line, 113 with
# a filename/body mismatch. Precedence decides where a record goes; it should
# never decide what is wrong with it.
TESTS = [
    ("unreadable_not_pdf", "pdf_open_failed",
     lambda r, w: any(x.startswith("pdf_open_failed") for x in w)),
    ("unreadable_no_text", "no_text_layer",
     lambda r, w: "no_text_layer" in w),
    ("needs_ai_review", "table_in_order_text",
     lambda r, w: r.get("has_table") is True),
    ("needs_review", "missing_critical_anchor",
     lambda r, w: any(f"missing_anchor:{a}" in w for a in CRITICAL_ANCHORS)),
    ("needs_review", "filename_body_mismatch",
     lambda r, w: any(x.startswith("filename_body_mismatch") for x in w)),
    ("needs_review", "unparsed_date",
     lambda r, w: "invalid_go_date" in w or "date_unparsed" in w),
    ("needs_review", "unparsed_go_number",
     lambda r, w: "go_number_unparsed" in w),
]


def problems(rec: dict) -> list:
    """Every applicable (bucket, problem), not just the first."""
    warnings = rec.get("warnings") or []
    return [(bucket, name) for bucket, name, test in TESTS if test(rec, warnings)]


def unresolved(rec: dict, side: "Sidecars") -> list:
    """The `needs_review` defects that are still genuinely open.

    Two ways a defect stops being open, and they are not the same thing:

      * a *gap* gets filled. `missing_anchor:X` says the parser never found the
        anchor; if the field it was supposed to yield is populated today, by the
        record itself or by the department sidecar, the gap is closed.
      * a *contradiction* gets settled. `filename_body_mismatch` is not a gap --
        both signals are populated, they disagree, and no later pass can fill
        anything in. It takes an adjudication, which is what `resolve_mismatches.py`
        performs by reading the value off the document's own header. It is open
        until every mismatched field on the record has a verdict.

    `unparsed_date` / `unparsed_go_number` used to be in neither class. The comment
    here read "self-clearing by definition and never stale", on the reasoning that
    the anchor resolved but its value would not parse, so a populated field would
    mean no warning. That held only while the parser was the sole reader of the
    page. `resolve_datenum.py` re-reads the document and the filename, so these
    two are now gaps like any other -- 2,621 dates and 2,454 numbers are populated
    today on records whose warning still says they are not. A comment asserting
    that a class of warning cannot go stale is a claim with a shelf life.
    """
    warnings = rec.get("warnings") or []
    still = []
    for a in CRITICAL_ANCHORS:
        if f"missing_anchor:{a}" in warnings and not ANCHOR_FILLED[a](rec, side):
            still.append(f"missing_anchor:{a}")
    if any(x.startswith("filename_body_mismatch") for x in warnings):
        row = side.mismatches.get(rec.get("source_file"))
        if not (row and row.get("fully_resolved")):
            still.append("filename_body_mismatch")
    if ("invalid_go_date" in warnings or "date_unparsed" in warnings) \
            and side.date_from(rec)[0] is None:
        still.append("unparsed_date")
    if "go_number_unparsed" in warnings and side.number_from(rec)[0] is None:
        still.append("unparsed_go_number")
    # §19's own defect, and the only one here that no `warnings` entry announces.
    # It cannot: the parser recorded `0202-12-28` without complaint, so the record
    # looks flawless and the defect is only visible to the pass that judged the
    # date impossible. A review queue driven purely by warnings would never show
    # these, which is the same shape as the 31 silently-wrong TELANGANA records.
    g = side.godate.get(rec.get("source_file")) or {}
    if g.get("status") == "unresolved_manual_verification":
        still.append("go_date_unestablished")
    return still


def self_sufficient(rec: dict, side: "Sidecars") -> bool:
    """Can `clean.jsonl` hold this record on its own?

    The clean bucket's invariant is that every record in it carries a `go_number`, a
    `go_date` and a `department_name` **in the record itself**, and that those values
    are the right ones. A record can fail that in two ways:

      * the value is missing from the record and lives in a sidecar. 2,521 records
        have a department that only `out/departments/` knows. Sending those to
        `clean.jsonl` would hand anyone reading it standalone a null
        `department_name` and quietly turn a documented guarantee into "complete,
        after a join you were not told about".
      * the value is present but the document says otherwise. On 1,900 records the
        adjudicator found the GO's own header printing the *filename's* date, not
        the one the parser recorded -- the parser read a cited order's date. The
        record is populated and wrong, which is worse than populated and absent,
        because nothing about it looks like it needs checking.

    Both go to `resolved_by_sidecar`, whose contract is "complete after the join".
    """
    meta = rec.get("go_meta") or {}
    # `is not None` on go_number: 0 is a value the parser really does produce.
    if not (meta.get("go_number") is not None
            and (meta.get("go_date") or {}).get("iso")
            and (rec.get("department") or {}).get("department_name")):
        return False
    # `government` was never named in the invariant, because until the government
    # sidecar existed every record that reached clean happened to have one -- the
    # bucket carried an implicit 100% guarantee (measured: 0 nulls in 57,445). The
    # first routing run after that sidecar landed put 1,272 null-government records
    # into clean and nothing complained, which is precisely how an implicit guarantee
    # dies. It is checked explicitly now.
    if not rec.get("government"):
        return False
    row = side.mismatches.get(rec.get("source_file"))
    if row:
        # `resolved_source == "filename"` means the document backed the filename
        # against the record. The corrected value exists only in the sidecar.
        if any(f.get("resolved_source") == "filename" for f in row.get("fields") or []):
            return False
        if truncated_number(row):
            # §16 certified a number the page cuts off before an OCR-misread
            # digit. The record is populated and wrong, which is the one thing
            # this function exists to keep out of clean.
            return False
    gov = side.government.get(rec.get("source_file"))
    if gov and gov.get("corrects_parser"):
        # 31 records carry `government: "TELANGANA"` because the unbounded gov_line
        # scan missed a misspelled masthead and ran on into Reorganisation-Act prose
        # about employees who retired from the Government of Telangana. Their own
        # line 0 reads ANDHRA PRADESH. The record is populated and wrong, so it is
        # complete only after the join -- the same test as above, different pass.
        return False
    # §19 re-read the document blind to the filename. Where it corrected the date,
    # the record still holds the old one -- populated and wrong, the same test as
    # the two above. Where it could not establish one, the record holds a date the
    # document cannot support, which is worse: `unresolved` routes that record to
    # needs_review, and this keeps the other route into clean shut behind it.
    gd = side.godate.get(rec.get("source_file")) or {}
    if gd.get("status") in ("corrected_from_document",
                            "unresolved_manual_verification"):
        return False
    dn = side.datenum.get(rec.get("source_file"))
    if dn and dn.get("go_date_source") == "header_corroborated" \
            and dn.get("parser_go_date") not in (None, dn.get("go_date")):
        # The parser dated REV01-MS-968-14_09_2009 as 2004-06-01 and
        # REV01-MS-102-06_02_2019 as 2016-08-30 -- citation dates, reached by the
        # same unbounded scan that produced the government drift. The filename and
        # the document's own header both say otherwise and agree with each other.
        # Populated and wrong, so: complete only after the join.
        #
        # This is the whole demotion set, and it is deliberately this narrow. A
        # `date_conflict` row is NOT demoted: there the parser and the filename
        # agree and only this pass's single reading of the header dissents, which
        # is one signal against two and not enough to move a record.
        return False
    # §21, and this gate is not optional. Without it 17 records sit in clean
    # carrying a `go_number` of 1, 2 or 3 -- read out of `G.O.Ms.No.1 8 2` and
    # `G.O.Ms.No.2 1 1` by a parser that stopped at the first space -- with no
    # warning on them at all. Populated and wrong, the same test as the four
    # above, and the same failure this project has now made four times: a pass
    # proves a stored value wrong and the very run that proves it writes the
    # record to clean anyway, because the clean test did not know the pass exists.
    #
    # `document_states_a_different_number` is deliberately NOT here. There the
    # record's value is what the document prints; the register disagrees, and a
    # register disagreeing with an attested document reading is a fact to record,
    # not a defect in the record. Demoting those would empty a tenth of clean to
    # express something that is not wrong with it.
    gn = side.gonumber.get(rec.get("source_file")) or {}
    if gn.get("status") in ("corrected_from_document", "demoted_to_register",
                            "unresolved_manual_verification"):
        return False
    return True


def second_facts(rec: dict, side: "Sidecars") -> list:
    """Records where a second attested value exists that the record cannot hold.

    Not defects, and deliberately not `problems`. A defect is a reason to route a
    record somewhere; this is a reason to read a second column, and conflating the
    two would demote a tenth of `clean` to express something that is not wrong
    with it.

    The need is specific and measured. 87 of the 113 records where the document
    prints one GO number and the GOIR register filed it under another sit in
    `clean.jsonl` with `problems: []` and `also_applies: []`. Both readings are
    attested, the record's own value is the one the document prints, and the
    bucket's contract -- present and right -- holds. But a consumer reading the
    bucket standalone sees `go_number: 127`, has nothing to tell it the register
    says 129, and so cannot know to go looking. `portal_number_from` has held that
    fact since §21 landed; an accessor nobody knows to call is not a column.

    Two inhabitants, from two independent passes, which is why this is a general
    slot and not a flag invented for one status:

        go_number_differs_from_goir_register        113  (§21)
        go_date_differs_from_portal_upload_date      23  (§19)

    Both name a pair, not a doubt. `portal_number_from` and `portal_date_from` are
    the accessors for the other half of each.
    """
    out = []
    gn = side.gonumber.get(rec.get("source_file")) or {}
    if gn.get("status") == "document_states_a_different_number":
        out.append("go_number_differs_from_goir_register")
    gd = side.godate.get(rec.get("source_file")) or {}
    # Gated on a document-attested status on purpose. Where §19 only retained what
    # was already stored, a difference from the portal date is the ordinary state
    # of the corpus -- a GO is issued on one day and uploaded on another -- and
    # flagging that would put a note on tens of thousands of unremarkable records.
    # The note means "the document itself says otherwise", which needs §19 to have
    # actually read it.
    if gd.get("status") in ("corrected_from_document", "confirmed_from_document") \
            and gd.get("go_date") and gd.get("date_uploaded_goir_portal") \
            and gd["go_date"] != gd["date_uploaded_goir_portal"]:
        out.append("go_date_differs_from_portal_upload_date")
    return out


def classify(rec: dict, side: "Sidecars" = None) -> str:
    """The single bucket a record is written to. Precedence order, first match.

    `needs_review` is the one bucket a record can be promoted *out* of, because it is
    the only one whose tests describe a defect a later pass can close. The higher-
    precedence buckets are not re-examined: `pdf_open_failed` and `no_text_layer` are
    permanent facts about the bytes on disk, and `has_table` describes work that is
    owned elsewhere.
    """
    side = side if side is not None else Sidecars()
    found = problems(rec)
    bucket = found[0][0] if found else "clean"
    if bucket == "needs_review" and not unresolved(rec, side):
        bucket = "clean" if self_sufficient(rec, side) else "resolved_by_sidecar"
    # The mirror of the promotion above, and it has to exist for the same reason
    # that one does. `problems()` reads `warnings`, so a defect that no warning
    # announces cannot put a record into needs_review -- and `go_date_unestablished`
    # is exactly that defect. Without this line a record whose date §19 proved
    # impossible, and which the parser never complained about, would be routed by
    # `self_sufficient` alone into `resolved_by_sidecar`: a bucket that promises
    # the record is complete after the join, about a record that has no GO date at
    # all. Demotion is not the same as queueing for review.
    elif bucket == "clean" and unresolved(rec, side):
        bucket = "needs_review"
    # A record can also reach clean without ever being a `needs_review` candidate --
    # no warnings fired, so `problems()` is empty. Those were never re-examined, and
    # 29 of them sat in clean labelled TELANGANA because the gov_line scan drifted
    # into body prose: no warning, no defect, nothing to promote, and wrong. The
    # self-sufficiency test has to gate *every* route into clean, not just the
    # promotion path, or a sidecar can only ever fix records that were already flagged.
    if bucket == "clean" and not self_sufficient(rec, side):
        bucket = "resolved_by_sidecar"
    return bucket


def input_files(path: Path) -> list:
    return sorted(path.glob("*.jsonl")) if path.is_dir() else [path]


def route(paths: list, outdir: Path, side: "Sidecars" = None) -> tuple:
    """Write the six disjoint bucket files, plus `routing_index.jsonl`.

    One record, one file -- downstream tools read the buckets directly
    (`reconstruct_tables.py` consumes `needs_ai_review.jsonl`) and the totals have to
    keep adding up. The index is a sidecar keyed by `source_file` recording *all*
    applicable problems, so the ones precedence hides stay queryable, plus `stale`:
    the defects the record is still warned about but demonstrably no longer has.
    """
    side = side if side is not None else Sidecars()
    # `demoted` is the mirror of `promoted`, and it exists because a sidecar can now
    # do more than clear a defect the parser flagged: it can contradict a value the
    # parser was confident enough about to raise no warning at all. Those records
    # carry no warnings, so precedence puts them in `clean` -- they have to be counted
    # on the way *out* of it, or the correction lands silently.
    counts, masked, promoted, demoted = Counter(), Counter(), Counter(), Counter()
    noted = Counter()
    handles = {b: (outdir / f"{b}.jsonl").open("w") for b in BUCKETS}
    index = (outdir / "routing_index.jsonl").open("w")
    try:
        for p in paths:
            with p.open() as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    found = problems(rec)
                    first = found[0][0] if found else "clean"
                    # Delegate to `classify` rather than repeating its rule. This
                    # function used to carry its own copy, the two drifted, and the
                    # copy here missed the clean-path self-sufficiency check that
                    # `classify` had gained -- so 29 records with a known-wrong
                    # government were written to clean.jsonl by the very run that
                    # detected they were wrong.
                    bucket = classify(rec, side)

                    stale = []
                    if first == "needs_review" and bucket != "needs_review":
                        stale = [n for _, n in found]
                        promoted[f"needs_review -> {bucket}"] += 1
                    elif first == "clean" and bucket != "clean":
                        demoted[f"clean -> {bucket}"] += 1

                    handles[bucket].write(json.dumps(rec, ensure_ascii=False) + "\n")
                    counts[bucket] += 1

                    other = sorted({b for b, _ in found} - {first})
                    for b in other:
                        masked[f"{first} also has {b}-class problems"] += 1
                    notes = second_facts(rec, side)
                    for nt in notes:
                        noted[f"{nt}  (in {bucket})"] += 1
                    index.write(json.dumps({
                        "source_file": rec.get("source_file"),
                        "filename": rec.get("filename"),
                        "bucket": bucket,
                        "also_applies": other,
                        "problems": [n for _, n in found],
                        "stale": stale,
                        "notes": notes,
                        "extraction_confidence": rec.get("extraction_confidence"),
                    }, ensure_ascii=False) + "\n")
    finally:
        for h in handles.values():
            h.close()
        index.close()
    return counts, masked, promoted, demoted, noted


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", type=Path, help="JSONL file, or a directory of them")
    ap.add_argument("--outdir", type=Path, default=None)
    ap.add_argument("--departments", type=Path, default=DEPARTMENTS,
                    help="department sidecar dir (optional; clears stale defects)")
    ap.add_argument("--mismatches", type=Path, default=MISMATCHES,
                    help="mismatch adjudication dir (optional; settles contradictions)")
    ap.add_argument("--government", type=Path, default=GOVERNMENT,
                    help="government sidecar dir (optional; fills and corrects gov_line)")
    ap.add_argument("--datenum", type=Path, default=DATENUM,
                    help="date/number sidecar dir (optional; fills go_date and go_number)")
    ap.add_argument("--godate", type=Path, default=GODATE,
                    help="GO-date/portal-date sidecar dir (optional; purges "
                         "impossible go_dates and owns date_uploaded_goir_portal)")
    ap.add_argument("--gonumber", type=Path, default=GONUMBER,
                    help="GO-number sidecar dir (optional; re-reads the number "
                         "the document prints and owns go_number_goir_register)")
    args = ap.parse_args()

    outdir = args.outdir or (args.input if args.input.is_dir() else args.input.parent)
    outdir.mkdir(parents=True, exist_ok=True)

    dept = load_departments(args.departments)
    if dept:
        print(f"Department sidecar: {len(dept)} resolved names from {args.departments}/")
    else:
        print(f"WARNING: {args.departments}/ not found -- routing on parser flags alone.")
        print("         Records whose only remaining defect is a department the sidecar")
        print("         has already resolved will stay in needs_review.")
        print("         Run resolve_departments.py first to clear them.")

    mism = load_mismatches(args.mismatches)
    if mism:
        settled = sum(1 for r in mism.values() if r.get("fully_resolved"))
        print(f"Mismatch sidecar  : {len(mism)} adjudicated rows from {args.mismatches}/ "
              f"({settled} fully settled)")
    else:
        print(f"WARNING: {args.mismatches}/ not found -- filename/body contradictions")
        print("         will all stay in needs_review, unadjudicated.")
        print("         Run resolve_mismatches.py first to settle them.")

    gov = load_government(args.government)
    if gov:
        fixed = sum(1 for r in gov.values() if r.get("corrects_parser"))
        read = sum(1 for r in gov.values()
                   if r.get("government_source") in ("masthead", "by_order_attestation"))
        print(f"Government sidecar: {len(gov)} rows from {args.government}/ "
              f"({read} re-read from the page, {fixed} correcting a wrong value)")
    else:
        print(f"WARNING: {args.government}/ not found -- gov_line gaps stay in")
        print("         needs_review, and the records whose government drifted onto")
        print("         body prose stay wrong in clean.jsonl.")
        print("         Run resolve_government.py first.")

    dnum = load_datenum(args.datenum)
    if dnum:
        # Count every tier the sidecar actually emits, rather than three
        # hand-picked ones. This banner named `header_corroborated` alone, so
        # when §18b added two date tiers it went on reporting 2,621 corroborated
        # dates and said nothing about the 858 records it had just filled. The
        # number it printed stayed true, which is exactly what made it dangerous:
        # a hand-written summary goes stale the moment a tier is added, and does
        # it silently. Derived from the data, it cannot drift again.
        dsrc = Counter(r.get("go_date_source") for r in dnum.values()
                       if r.get("go_date_source"))
        nsrc = Counter(r.get("go_number_source") for r in dnum.values()
                       if r.get("go_number_source"))
        nodate = sum(1 for r in dnum.values() if not r.get("go_date"))
        print(f"Date/number sidecar: {len(dnum)} rows from {args.datenum}/")
        print("    dates  : " + ", ".join(f"{dsrc[k]} {k}" for k in sorted(dsrc))
              + f", {nodate} with no date from this sidecar")
        print("    numbers: " + ", ".join(f"{nsrc[k]} {k}" for k in sorted(nsrc)))
        # Note the wording: "from this sidecar", not "from any source". It used
        # to say the latter, which is a claim about the CORPUS made by counting
        # one file. That was true only while this sidecar was the last word, and
        # it stopped being true the moment out/mismatches/ was wired into
        # `date_from` -- §16 owns 2,835 dates and 702 numbers that never appear
        # in these counts. The corpus-level figure is deliberately not computed
        # here: it needs a second full pass over the records, and
        # `verify_against_filename.py` already makes it by walking the same
        # resolution order. One owner per number, same rule as one owner per fact.
    else:
        print(f"WARNING: {args.datenum}/ not found -- unparsed dates and GO numbers")
        print("         stay in needs_review, and the two records whose date the")
        print("         parser read off a citation stay wrong in clean.jsonl.")
        print("         Run resolve_datenum.py first.")

    gdate = load_godate(args.godate)
    if gdate:
        st = Counter(r.get("status") for r in gdate.values())
        gsrc = Counter(r.get("go_date_source") for r in gdate.values()
                       if r.get("go_date_source"))
        portal = sum(1 for r in gdate.values()
                     if r.get("date_uploaded_goir_portal"))
        print(f"GO-date sidecar   : {len(gdate)} rows from {args.godate}/ "
              f"({portal} carrying a portal upload date)")
        print("    read from the document: "
              + ", ".join(f"{gsrc[k]} {k}" for k in sorted(gsrc)))
        print("    status: " + ", ".join(f"{st[k]} {k}" for k in sorted(st)))
    else:
        print(f"WARNING: {args.godate}/ not found -- GO Date and Date Uploaded on")
        print("         GOIR Portal are NOT separated, and the impossible dates")
        print("         (0202-12-28, 3009-03-16) are back in the routed output.")
        print("         Run resolve_godate.py first.")

    gnum = load_gonumber(args.gonumber)
    if gnum:
        st = Counter(r.get("status") for r in gnum.values())
        nsrc = Counter(r.get("go_number_source") for r in gnum.values()
                       if r.get("go_number_source"))
        reg = sum(1 for r in gnum.values() if r.get("go_number_goir_register")
                  is not None)
        print(f"GO-number sidecar : {len(gnum)} rows from {args.gonumber}/ "
              f"({reg} carrying a GOIR register number)")
        print("    accepted from: "
              + ", ".join(f"{nsrc[k]} {k}" for k in sorted(nsrc)))
        print("    status: " + ", ".join(f"{st[k]} {k}" for k in sorted(st)))
    else:
        print(f"WARNING: {args.gonumber}/ not found -- 309 GO numbers the parser")
        print("         took out of citation lists, and 84 it borrowed from them,")
        print("         are back in the routed output, and the GOIR register")
        print("         number is not available as a separate fact.")
        print("         Run resolve_gonumber.py first.")

    paths = input_files(args.input)
    counts, masked, promoted, demoted, noted = route(
        paths, outdir, Sidecars(dept, mism, gov, dnum, gdate, gnum))
    total = sum(counts.values())

    print(f"\nRead {total} records from {len(paths)} file(s)")
    for b in BUCKETS:
        pct = counts[b] / total * 100 if total else 0.0
        print(f"  -> {b + '.jsonl':28s} {counts[b]:7d}  ({pct:5.1f}%)")

    if promoted:
        print("\nPromoted: every defect these were queued for is demonstrably filled")
        print("(see routing_index.jsonl -> `stale` for which warnings are outdated):")
        for k, v in promoted.most_common():
            print(f"  {v:7d}  {k}")

    if demoted:
        print("\nDemoted: the parser raised no warning on these, but a sidecar read the")
        print("document back and found the stored value wrong. They are NOT clean:")
        for k, v in demoted.most_common():
            print(f"  {v:7d}  {k}")

    if masked:
        print("\nRecords whose bucket hides a second, unrelated problem")
        print("(see routing_index.jsonl -> `also_applies`):")
        for k, v in masked.most_common():
            print(f"  {v:7d}  {k}")

    if noted:
        print("\nTwo attested values, one column. NOT defects -- these records are")
        print("correct and most of them are in clean; the second value lives in the")
        print("sidecar and the record cannot hold it. Read both.")
        print("(see routing_index.jsonl -> `notes`):")
        for k, v in noted.most_common():
            print(f"  {v:7d}  {k}")
    print(f"\nWritten to: {outdir}/  (+ routing_index.jsonl)")


if __name__ == "__main__":
    main()
