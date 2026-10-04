#!/usr/bin/env python3
"""Gate 3 loader: the on-disk pipeline outputs -> Supabase Postgres.

Three stages, deliberately decoupled (HANDOVER: "parse once to JSONL, reload
freely"):

    python3 load_supabase.py parse     # no DB, no credentials. Writes out/load/*.csv
    python3 load_supabase.py load      # COPYs those CSVs into Supabase
    python3 load_supabase.py wave2     # the GO-to-GO links, once every id exists

`wave2` is separate because resolved_go_id and amends_go_id point from one
corpus row to another, so they cannot be computed until the whole corpus has
ids. The MATCHING behind them is not done here either -- resolve_references.py
decides it offline and writes source_file pairs, so run that first:

    python3 resolve_references.py --validate   # measure, write nothing
    python3 resolve_references.py --apply      # writes the two link CSVs

`parse` is the one that can be wrong, and it is the one that needs no
credentials, so it is fully verifiable before anything touches the database.
`load` is mechanical: truncate-and-COPY, inside one transaction per table
group, recorded in `load_batches`.

Four rules carried in from the parser stage, all of them load-time invariants:

  1. THE EFFECTIVE VALUE IS NEVER RECOMPUTED HERE. Sidecar precedence lives in
     route_for_ai.Sidecars and is imported, never reimplemented. This loader
     asks `side.date_from(rec)` / `side.number_from(rec)` and writes what it is
     told, together with the tier that told it.
  2. TWO ATTESTED VALUES, TWO COLUMNS. The register's number/date/series and
     the document's are both written; neither is derived from the other and
     neither overwrites the other.
  3. CONTENT IS VERBATIM. abstract, order_text, cell text, reference raw_text
     and every amendment span are copied byte-for-byte. Nothing is stripped,
     normalised or re-encoded on the way in.
  4. FAIL LOUDLY. A value this loader cannot establish is NULL. Every
     assumption it makes is an assert, so a violated one stops the run instead
     of loading a guess. Counts are printed per table and checked in verify().

CSV dialect note: a field is written either as a quoted literal or as an
unquoted empty field, and nothing else. Postgres CSV COPY reads the unquoted
empty field as NULL and `""` as the empty string, which is the only way to keep
"no value" and "empty value" apart in a text column.
"""

import csv
import json
import os
import sys
import glob
import collections
from pathlib import Path

import route_for_ai as rfa

#         order_text holds whole GOs, far past the 128 KiB default field size.
csv.field_size_limit(10 ** 9)

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
LOAD = OUT / "load"
ROUTED = OUT / "routed"

BUCKETS = ["clean", "needs_review", "resolved_by_sidecar", "needs_ai_review",
           "unreadable_not_pdf", "unreadable_no_text"]

# Recipient positions above which a GO is queued for review as a To-block
# over-capture. See the emit site in parse() for why this flags and never drops.
RECIPIENT_OVERRUN = 100

EXPECTED_TOTAL = 73954


# ---------------------------------------------------------------------------
# CSV emission
# ---------------------------------------------------------------------------

#: NUL bytes removed on the way in, by source_file. See scrub().
NUL_STRIPPED = collections.Counter()


def scrub(s):
    """Remove U+0000, and only U+0000.

    This is the one place content is altered between disk and database, and it
    is not a judgement call: Postgres `text` cannot hold a NUL byte at all, so
    the alternative to removing it is not keeping it, it is failing the COPY.

    The blast radius is 15 records of 73,954 -- 13 of them abstracts that are
    raw RTF control words rather than prose (a separate, pre-existing defect),
    and 2 order_texts with 6 stray bytes each. Every other control character
    (0x01-0x1f) is left exactly as it is, because Postgres accepts those and
    rule 3 says content is immutable. There are no lone surrogates in the
    corpus; that was checked, not assumed.

    Nothing here is silent: each affected record gets a `nul_bytes_stripped`
    row in go_review_queue naming the count.
    """
    if "\x00" not in s:
        return s
    return s.replace("\x00", "")


def fld(v):
    """One CSV field: quoted literal, or unquoted empty meaning SQL NULL."""
    if v is None:
        return ""
    if v is True:
        return '"true"'
    if v is False:
        return '"false"'
    s = v if isinstance(v, str) else str(v)
    if "\x00" in s:
        s = scrub(s)
    return '"' + s.replace('"', '""') + '"'


def pg_array(vals):
    """A text[] literal. None stays None (the column defaults to '{}')."""
    if vals is None:
        return None
    out = []
    for v in vals:
        if v is None:
            out.append("NULL")
            continue
        s = str(v).replace("\\", "\\\\").replace('"', '\\"')
        out.append('"' + s + '"')
    return "{" + ",".join(out) + "}"


def pg_num_array(vals):
    if vals is None:
        return None
    return "{" + ",".join("NULL" if v is None else str(v) for v in vals) + "}"


class Table:
    """A CSV file being built, with its column list and a row counter."""

    def __init__(self, name, columns):
        self.name = name
        self.columns = columns
        LOAD.mkdir(parents=True, exist_ok=True)
        self.path = LOAD / f"{name}.csv"
        self.fh = self.path.open("w", encoding="utf-8", newline="")
        self.n = 0

    def row(self, *values):
        assert len(values) == len(self.columns), (
            f"{self.name}: {len(values)} values for {len(self.columns)} columns")
        self.fh.write(",".join(fld(v) for v in values) + "\n")
        self.n += 1

    def close(self):
        self.fh.close()


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def load_jsonl_by_source(pattern):
    """{source_file: row} for a sidecar directory. Asserts one row per file."""
    out = {}
    for f in sorted(glob.glob(str(HERE / pattern))):
        if "REPORT" in f:
            continue
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                sf = r.get("source_file")
                if sf:
                    out[sf] = r
    return out


def canonical_departments():
    """The canonical department list and its code aliases, read from the pass
    that owns it. Ids are assigned by sorted name, matching the seeded table.

    IMPORTED, not text-scraped. An earlier version regex-matched the CANONICAL
    literal and silently lost GWS01, whose name is written as two implicitly
    concatenated string literals -- 42 records then had a canonical department
    with no id. The dict is Python; the way to read Python is to run it.
    """
    import resolve_departments as rd
    raw = rd.CANONICAL
    codes = {}
    for code, val in raw.items():
        codes[code] = val[0] if isinstance(val, (tuple, list)) else val
    names = sorted(set(codes.values()))
    ids = {n: i + 1 for i, n in enumerate(names)}
    return ids, codes


def read_records():
    """Every routed record, with the bucket it was filed under.

    The bucket files are disjoint and their union is the corpus, which is
    asserted here rather than assumed: a silently short load is the failure
    mode this whole stage exists to prevent.
    """
    recs = {}
    per_bucket = collections.Counter()
    for b in BUCKETS:
        p = ROUTED / f"{b}.jsonl"
        assert p.exists(), f"missing bucket file {p}"
        with p.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                sf = r["source_file"]
                assert sf not in recs, f"record in two buckets: {sf}"
                r["_bucket"] = b
                recs[sf] = r
                per_bucket[b] += 1
    assert len(recs) == EXPECTED_TOTAL, f"{len(recs)} records, expected {EXPECTED_TOTAL}"
    return recs, per_bucket


def read_routing_index():
    idx = {}
    p = ROUTED / "routing_index.jsonl"
    with p.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            idx[r["source_file"]] = r
    assert len(idx) == EXPECTED_TOTAL, f"routing_index has {len(idx)} rows"
    return idx


# ---------------------------------------------------------------------------
# Effective-value helpers. These map a tier name onto the schema's vocabulary;
# they do not decide anything.
# ---------------------------------------------------------------------------

def date_and_source(rec, side):
    """(iso, source) for go_date. The §19 tier reports its own label, because
    'the document's header, with the year repaired' is a different provenance
    from 'the document's header' and the column exists to say so."""
    iso, tier = side.date_from(rec)
    if iso is None:
        return None, None
    if tier == "godate_sidecar":
        row = side.godate.get(rec["source_file"]) or {}
        src = row.get("go_date_source")
        assert src, f"godate sidecar gave a date with no source: {rec['source_file']}"
        return iso, src
    if tier == "sidecar":
        return iso, "datenum_sidecar"
    assert tier in ("record", "mismatch_sidecar"), tier
    return iso, tier


def number_and_source(rec, side):
    num, tier = side.number_from(rec)
    if num is None:
        return None, None
    if tier == "gonumber_sidecar":
        row = side.gonumber.get(rec["source_file"]) or {}
        src = row.get("go_number_source")
        assert src, f"gonumber sidecar gave a number with no source: {rec['source_file']}"
        return num, src
    if tier == "sidecar":
        return num, "datenum_sidecar"
    assert tier in ("record", "mismatch_sidecar"), tier
    return num, tier


def iso_or_none(s):
    """A date the database will accept, or None. A malformed date is dropped
    with a NULL rather than coerced -- rule 4."""
    if not s or not isinstance(s, str):
        return None
    parts = s.split("-")
    if len(parts) != 3:
        return None
    try:
        y, m, d = (int(x) for x in parts)
    except ValueError:
        return None
    if not (1900 <= y <= 2100 and 1 <= m <= 12 and 1 <= d <= 31):
        return None
    return f"{y:04d}-{m:02d}-{d:02d}"


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------

GO_COLUMNS = [
    "source_file", "filename", "go_year",
    "go_number", "go_number_goir_register", "go_number_source", "go_number_remark",
    "go_date", "date_uploaded_goir_portal", "go_date_source", "go_date_remark",
    "go_type", "go_type_document",
    "department_id", "department_raw", "sub_department", "dept_code",
    "cross_filed_department_id",
    "government", "government_source",
    "abstract", "order_text", "signature_name", "signature_designation",
    "employee_name",
    "is_amendment", "amends_go_number", "amendment_keyword",
    "apgo_amendment_type", "apgo_category",
    "go_category", "go_sub_category", "secondary_category_tags",
    "category_status", "category_evidence", "category_rule", "category_basis",
    "extraction_confidence", "text_quality", "page_count", "is_eoffice",
    "has_table", "anchors_found", "warnings", "routing_bucket",
]


def parse():
    print("reading sidecars ...")
    side = rfa.Sidecars(
        departments=rfa.load_departments(),
        mismatches=rfa.load_mismatches(),
        government=rfa.load_government(),
        datenum=rfa.load_datenum(),
        godate=rfa.load_godate(),
        gonumber=rfa.load_gonumber(),
    )
    # Sidecars.departments holds only the canonical NAME (that is all the
    # router needs). The department columns need the whole adjudication row --
    # the body string, the code's answer and the conflict flag -- so the rows
    # are read separately rather than by widening the router's loader.
    dept_rows = load_jsonl_by_source("out/departments/*.jsonl")
    category = load_jsonl_by_source("out/category/*.jsonl")
    gotype = load_jsonl_by_source("out/gotype/*.jsonl")
    dept_ids, dept_codes = canonical_departments()
    print(f"  departments {len(dept_ids)} | dept rows {len(dept_rows)} | "
          f"category {len(category)} | gotype {len(gotype)}")

    recs, per_bucket = read_records()
    idx = read_routing_index()
    print(f"records {len(recs)}  " + "  ".join(f"{b}={per_bucket[b]}" for b in BUCKETS))

    go = Table("ap_government_orders", GO_COLUMNS)
    refs = Table("go_references", ["source_file", "seq", "ref_kind", "go_type",
                                   "go_number", "department_raw", "date_raw",
                                   "raw_text"])
    recips = Table("go_recipients", ["source_file", "seq", "recipient"])
    queue = Table("go_review_queue", ["source_file", "problem", "detail"])
    prov = Table("go_field_provenance",
                 ["source_file", "field", "pass", "status", "value",
                  "previous_value", "previous_tier", "status_before_fallback",
                  "source", "remark", "how", "evidence", "evidence_line"])
    alias = Table("department_aliases_body",
                  ["alias", "department_id", "alias_kind"])

    body_alias = {}
    unmapped_dept = collections.Counter()
    stats = collections.Counter()

    for sf, rec in recs.items():
        fp = rec.get("filename_parsed") or {}
        gm = rec.get("go_meta") or {}
        dep = rec.get("department") or {}
        dsc = dept_rows.get(sf) or {}
        gov = side.government.get(sf) or {}
        cat = category.get(sf) or {}
        gt = gotype.get(sf) or {}
        gd = side.godate.get(sf) or {}
        gn = side.gonumber.get(sf) or {}

        # --- identity pair 1: number -------------------------------------
        num, num_src = number_and_source(rec, side)
        reg_num = side.portal_number_from(rec)
        if reg_num is None:
            reg_num = fp.get("go_number")
        num_remark = gd_remark = None
        if gn.get("go_number_remark"):
            num_remark = gn["go_number_remark"]
            assert num_src == "goir_register", (
                f"remark without register fallback: {sf}")

        # --- identity pair 2: date ---------------------------------------
        iso, date_src = date_and_source(rec, side)
        iso = iso_or_none(iso)
        if iso is None:
            date_src = None
        portal = iso_or_none(side.portal_date_from(rec) or fp.get("date_iso"))
        if gd.get("go_date_remark"):
            gd_remark = gd["go_date_remark"]
            assert date_src == "date_uploaded_goir_portal", (
                f"remark without portal fallback: {sf}")

        # --- identity pair 3: series -------------------------------------
        go_type = fp.get("go_type") or rec.get("go_type")
        go_type_doc = gt.get("go_type_document")

        # --- department ---------------------------------------------------
        canon = dsc.get("canonical_department")
        dept_id = dept_ids.get(canon) if canon else None
        if canon and dept_id is None:
            unmapped_dept[canon] += 1
        cross_id = None
        if dsc.get("conflict"):
            other = dsc.get("canonical_by_code")
            if other and other != canon:
                cross_id = dept_ids.get(other)
        body_raw = dsc.get("department_name_body") or dep.get("department_name")
        by_body = dsc.get("canonical_by_body")
        if body_raw and by_body and by_body in dept_ids:
            body_alias[body_raw] = dept_ids[by_body]

        # --- government ----------------------------------------------------
        government = gov.get("government") or rec.get("government")
        gov_src = gov.get("government_source")

        # --- classification -------------------------------------------------
        cat_status = cat.get("status")
        cat_cat = cat.get("go_category")
        if cat_status:
            assert (cat_cat is None) == (cat_status == "no_text_to_classify"), (
                f"category null-contract violated: {sf}")

        anchors = rec.get("anchors_found")
        anchors_n = len(anchors) if isinstance(anchors, list) else anchors

        amd = rec.get("amendment") or {}
        sig = rec.get("signature") or {}

        go.row(
            sf, rec.get("filename"), rec.get("go_year"),
            num, reg_num, num_src, num_remark,
            iso, portal, date_src, gd_remark,
            go_type, go_type_doc,
            dept_id, body_raw, dep.get("sub_department_name"),
            rec.get("dept_code"), cross_id,
            government, gov_src,
            rec.get("abstract"), rec.get("order_text"),
            sig.get("name"), sig.get("designation"), rec.get("employee_name"),
            bool(amd.get("is_amendment")), amd.get("amends_go_number"),
            amd.get("keyword"),
            rec.get("apgo_amendment_type"), rec.get("apgo_category"),
            cat_cat, cat.get("go_sub_category"),
            pg_array(cat.get("secondary_tags") or []),
            cat_status, cat.get("evidence"), cat.get("rule"), cat.get("basis"),
            rec.get("extraction_confidence"), rec.get("text_quality"),
            rec.get("page_count"), rec.get("is_eoffice"), rec.get("has_table"),
            anchors_n, pg_array(rec.get("warnings") or []),
            rec["_bucket"],
        )
        stats["go"] += 1

        # --- references ------------------------------------------------------
        seen_seq = set()
        for i, r in enumerate(rec.get("references") or []):
            seq = r.get("seq")
            if seq is None or seq in seen_seq:
                seq = max(seen_seq) + 1 if seen_seq else i
            seen_seq.add(seq)
            refs.row(sf, seq, r.get("ref_kind") or "unknown", r.get("go_type"),
                     r.get("go_number"), r.get("department"), r.get("date_raw"),
                     r.get("raw_text") or "")
            stats["refs"] += 1

        # --- recipients ------------------------------------------------------
        n_recips = 0
        for i, rcp in enumerate(rec.get("recipients") or []):
            if rcp is None:
                continue
            recips.row(sf, i, rcp)
            stats["recips"] += 1
            n_recips += 1

        # --- review queue: EVERY applicable problem, not just the first ------
        ix = idx.get(sf) or {}
        problems = []
        # The To-block parser can walk past the addressee list into a table and
        # emit one row per cell. Two Finance GOs reach 318,044 and 148,948
        # positions -- which is what overflowed smallint and killed the first
        # load -- and between them they are 39% of the whole recipients table.
        #
        # Flagged, never dropped. A count cannot separate the defect from a
        # real distribution list: the Finance giants are ~94% junk, but a
        # 6,223-line Health list is only 3.7% junk and is genuinely a list of
        # addressees. So the threshold buys a review queue entry, not a delete.
        #
        # 100 is deliberately far above the corpus, not fitted to it: p50 is 9,
        # p90 is 19, p99 is 38. It flags 142 GOs (0.23%) which between them hold
        # 44.9% of the rows -- so a small, readable queue covers nearly half the
        # table's mass. RECIPIENT_OVERRUN is a constant so the schema comment,
        # this rule and any later query cannot drift apart.
        #
        # Emitted below as its own row rather than pushed into `problems`, so
        # its `detail` can carry the actual count instead of the router's
        # generic notes. A reviewer needs the number to triage it.
        overrun = n_recips if n_recips > RECIPIENT_OVERRUN else 0
        for p in ix.get("problems") or []:
            problems.append(p if isinstance(p, str) else p.get("problem"))
        for p in ix.get("also_applies") or []:
            problems.append(p if isinstance(p, str) else p.get("problem"))
        for p in sorted({p for p in problems if p}):
            queue.row(sf, p, ix.get("notes"))
        if overrun:
            queue.row(sf, "recipient_block_overrun",
                      f"{overrun} recipient positions parsed "
                      f"(p99 of the corpus is 38)")
            stats["recipient_block_overrun"] += 1
            stats["queue"] += 1

        # A NUL strip is a load-time alteration of immutable content, so it is
        # declared in the queue rather than buried in a log. `warnings` is a
        # parse-time record and is never rewritten, so it is not the place.
        nuls = {f: (rec.get(f) or "").count("\x00")
                for f in ("abstract", "order_text")
                if isinstance(rec.get(f), str) and "\x00" in rec[f]}
        if nuls:
            detail = ", ".join(f"{f}: {n} removed" for f, n in sorted(nuls.items()))
            queue.row(sf, "nul_bytes_stripped", detail)
            NUL_STRIPPED[sf] = sum(nuls.values())
            stats["queue"] += 1

        # --- provenance: the passes that actually decided something ----------
        if gn.get("status") and gn["status"] != "agrees_with_register":
            prov.row(sf, "go_number", "s21_gonumber", gn["status"],
                     gn.get("go_number"), gn.get("previous_go_number"),
                     gn.get("previous_go_number_tier"),
                     gn.get("status_before_fallback"),
                     gn.get("go_number_source"), gn.get("go_number_remark"),
                     gn.get("go_number_how"), gn.get("go_number_evidence"),
                     gn.get("go_number_evidence_line"))
            stats["prov"] += 1
        if gd.get("status") and gd["status"] != "retained_existing":
            prov.row(sf, "go_date", "s19_godate", gd["status"],
                     gd.get("go_date"), gd.get("previous_go_date"),
                     gd.get("previous_go_date_tier"),
                     gd.get("status_before_fallback"),
                     gd.get("go_date_source"), gd.get("go_date_remark"),
                     gd.get("go_date_how"), gd.get("go_date_evidence"),
                     gd.get("go_date_evidence_line"))
            stats["prov"] += 1
        mm = side.mismatches.get(sf) or {}
        for f in mm.get("fields") or []:
            field = f.get("field")
            if field not in ("go_date", "go_number", "go_type"):
                continue
            prov.row(sf, field, "s16_mismatches", f.get("verdict") or
                     mm.get("status") or "adjudicated",
                     f.get("resolved_value"), f.get("body_value"), "body",
                     None, mm.get("text_route"), None,
                     f.get("verdict"), f.get("document_evidence"), None)
            stats["prov"] += 1
        if gov.get("government_source") and gov["government_source"] != "parser_masthead":
            prov.row(sf, "government", "s17_government",
                     gov.get("government_source"), gov.get("government"),
                     gov.get("parser_government"), "parser", None,
                     gov.get("government_source"), None,
                     f"similarity={gov.get('similarity')}"
                     if gov.get("similarity") is not None else None,
                     gov.get("evidence"), gov.get("evidence_line_no"))
            stats["prov"] += 1
        if dsc.get("resolution") and dsc["resolution"] != "agree":
            prov.row(sf, "department", "s15_departments", dsc["resolution"],
                     canon, dsc.get("canonical_by_code"), "filename_code", None,
                     dsc.get("canonical_department_source"), None,
                     dsc.get("body_match_method"),
                     dsc.get("department_name_body"), None)
            stats["prov"] += 1
        if gt:
            prov.row(sf, "go_type", "s22_gotype", gt.get("identity") or "kept_both",
                     gt.get("go_type_document"), gt.get("go_type_goir_register"),
                     "goir_register", None, "document_header", None,
                     gt.get("status"), gt.get("evidence"), gt.get("evidence_line"))
            stats["prov"] += 1

    for a, did in sorted(body_alias.items()):
        alias.row(a, did, "body_text")

    for t in (go, refs, recips, queue, prov, alias):
        t.close()
        print(f"  {t.name:28s} {t.n:>8,} rows  -> {t.path.name}")

    assert not unmapped_dept, f"canonical names with no id: {dict(unmapped_dept)}"
    assert go.n == EXPECTED_TOTAL, go.n
    if NUL_STRIPPED:
        print(f"  NUL bytes stripped from {len(NUL_STRIPPED)} records "
              f"({sum(NUL_STRIPPED.values())} bytes); each is flagged "
              f"'nul_bytes_stripped' in go_review_queue")

    parse_tables()
    parse_amendments(recs)
    print("parse: OK")


def parse_tables():
    tabs = Table("go_tables",
                 ["table_id", "source_file", "logical_table_id", "table_index",
                  "page_number", "pages", "bbox", "n_rows", "n_cols",
                  "is_continuation", "continuation_of", "part_index",
                  "part_count", "column_labels", "column_headers",
                  "headers_identified", "headers_inherited",
                  "detection_strategy", "structure_confidence",
                  "review_required", "review_reasons"])
    cells = Table("go_table_cells",
                  ["cell_id", "table_id", "row_index", "column_index",
                   "column_label", "column_header", "is_header", "text",
                   "row_span", "col_span", "bbox"])
    seen_t, seen_c = set(), set()
    for f in sorted(glob.glob(str(OUT / "tables" / "*.jsonl"))):
        if "REPORT" in f:
            continue
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                sf = r["source_file"]
                for t in r.get("tables") or []:
                    tid = t["table_id"]
                    if tid in seen_t:
                        continue          # same table listed twice; first wins
                    seen_t.add(tid)
                    tabs.row(tid, sf, t.get("logical_table_id") or tid,
                             t.get("table_index"), t.get("page_number"),
                             pg_num_array(t.get("pages")),
                             pg_num_array(t.get("bbox")),
                             t.get("n_rows"), t.get("n_cols"),
                             bool(t.get("is_continuation")),
                             t.get("continuation_of"), t.get("part_index"),
                             t.get("part_count"),
                             pg_array(t.get("column_labels") or []),
                             pg_array(t.get("column_headers")),
                             t.get("headers_identified"),
                             t.get("headers_inherited"),
                             t.get("detection_strategy"),
                             t.get("structure_confidence"),
                             bool(t.get("review_required")),
                             pg_array(t.get("review_reasons") or []))
                    for row in t.get("rows") or []:
                        for c in row.get("cells") or []:
                            cid = c.get("cell_id")
                            if not cid or cid in seen_c:
                                continue
                            seen_c.add(cid)
                            cells.row(cid, tid, c.get("row_index"),
                                      c.get("column_index"),
                                      c.get("column_label") or "column_1",
                                      c.get("column_header"),
                                      bool(c.get("is_header")),
                                      c.get("text") or "",
                                      c.get("row_span") or 1,
                                      c.get("col_span") or 1,
                                      pg_num_array(c.get("bbox")))
    for t in (tabs, cells):
        t.close()
        print(f"  {t.name:28s} {t.n:>8,} rows  -> {t.path.name}")


def parse_amendments(recs):
    """out/model_amendments/outputs/ -- task #23, the validated generation.

    One extraction row per GO, then targets in file order and provisions in
    target order. `ord` is positional and carries the document's own sequence;
    it is the only thing the unique keys rest on.
    """
    # The text the spans were anchored against, read from the shard inputs the
    # extraction pass actually consumed. For 886 GOs this is NOT the corpus
    # order_text -- it is a fuller reading (full-PDF re-read, OCR, .doc
    # conversion) -- so without it 2,093 spans would be unfindable in the
    # database. Loaded from the shards rather than reconstructed.
    anchor, anchor_kind = {}, {}
    for f in sorted(glob.glob(str(OUT / "model_amendments" / "shards" /
                                  "shard_*.jsonl"))):
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                s = json.loads(line)
                anchor[s["source_file"]] = s.get("order_text") or ""
                anchor_kind[s["source_file"]] = s.get("text_source")

    ext = Table("go_amendment_extractions",
                ["source_file", "go_class", "confidence", "flags",
                 "executive_label", "amendment_summary", "instrument_count",
                 "provision_count", "source_text", "source_text_kind"])
    tgt = Table("go_amendment_targets",
                ["source_file", "ord", "name_verbatim", "name_best",
                 "name_best_source", "name_abstract", "instr_type", "kind"])
    pr = Table("go_amendment_provisions",
               ["source_file", "target_ord", "ord", "provision", "action",
                "action_code", "previous_text", "new_text", "clause_quote",
                "effective_date", "spans_anchored"])
    unanchored = 0
    seen = set()
    files = sorted(glob.glob(str(OUT / "model_amendments" / "outputs" /
                                 "shard_*.out.jsonl")))
    for f in files:
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                sf = r["source_file"]
                assert sf not in seen, f"amendment row twice: {sf}"
                assert sf in recs, f"amendment row for unknown GO: {sf}"
                seen.add(sf)
                insts = r.get("instruments") or []
                nprov = sum(len(i.get("provisions") or []) for i in insts)
                body = anchor.get(sf)
                ext.row(sf, r["go_class"], r.get("confidence") or "medium",
                        pg_array(r.get("flags") or []), r.get("executive_label"),
                        r.get("amendment_summary"), len(insts), nprov,
                        body, anchor_kind.get(sf))
                for oi, inst in enumerate(insts):
                    tgt.row(sf, oi, inst.get("name_verbatim"),
                            inst.get("name_best") or inst.get("name_verbatim")
                            or "UNCLEAR",
                            inst.get("name_best_source") or "text",
                            inst.get("name_abstract"),
                            inst.get("type") or "UNCLEAR",
                            inst.get("kind") or "amended")
                    for oj, p in enumerate(inst.get("provisions") or []):
                        spans = [p.get("previous_text"), p.get("new_text"),
                                 p.get("clause_quote")]
                        anchored = all(s in (body or "") for s in spans if s)
                        if not anchored:
                            unanchored += 1
                        pr.row(sf, oi, oj, p.get("provision"),
                               p.get("action") or "UNCLEAR",
                               p.get("action_code") or "UNC",
                               p.get("previous_text"), p.get("new_text"),
                               p.get("clause_quote"), p.get("effective_date"),
                               anchored)
    print(f"  amendment shards: {len(files)}, GOs: {len(seen)}, "
          f"provisions with an unanchored span: {unanchored}")
    assert set(anchor) >= seen, "an extraction has no shard input to anchor to"
    for t in (ext, tgt, pr):
        t.close()
        print(f"  {t.name:28s} {t.n:>8,} rows  -> {t.path.name}")


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------

#: Credentials live here, outside the repo, chmod 600. Never in the tree, never
#: printed. The loader reads the file and nothing echoes the value.
ENV_FILE = Path.home() / ".askmyjunior" / "supabase.env"


def dsn():
    url = os.environ.get("SUPABASE_DB_URL")
    if not url and ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("SUPABASE_DB_URL="):
                url = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not url:
        sys.exit(f"no connection string. Put SUPABASE_DB_URL in {ENV_FILE} "
                 f"(session pooler, port 5432) or set it in the environment.")
    if ":6543" in url:
        sys.exit("refusing to load over the transaction pooler (6543): COPY "
                 "misbehaves there. Use the session pooler on 5432.")
    if "YOUR_PASSWORD" in url:
        sys.exit(f"{ENV_FILE} still has the placeholder password in it.")
    return url


def copy_csv(cur, table, columns, path):
    """Stream a CSV straight into a table with COPY, in 1 MiB chunks.

    Bytes go through untouched -- no decode, no re-encode, no line splitting --
    so whatever `parse` wrote is exactly what Postgres parses.
    """
    stmt = (f'copy {table} ({", ".join(columns)}) '
            f"from stdin with (format csv, null '')")
    with cur.copy(stmt) as cp, path.open("rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            cp.write(chunk)


def csv_rows(path):
    """Row count of a generated CSV, counted with the csv parser.

    Deliberately NOT a newline count. recipient, raw_text and order_text all
    carry embedded newlines inside their quoted fields, so physical lines run
    far ahead of rows -- go_recipients alone is 712k rows across many more
    lines. A line count here would declare every finished table half-applied
    and abort the resume it is supposed to enable.
    """
    with path.open(newline="", encoding="utf-8") as fh:
        return sum(1 for _ in csv.reader(fh))


def staging(cur, name, table, drop=(), add=()):
    """A temp table shaped LIKE the real one, so COPY does the type parsing.

    This is the whole reason for LIKE rather than a hand-written all-text
    table: staging everything as text and then INSERT ... SELECT into typed
    columns does not work, because Postgres will not implicitly cast text to
    integer, boolean, date or smallint[] in an INSERT. With LIKE, COPY parses
    each value into its real type on the way in and the INSERT is a straight
    copy. Constraints are deliberately NOT included -- they are enforced once,
    by the real INSERT, instead of twice.
    """
    cur.execute(f"drop table if exists {name}")
    # INCLUDING DEFAULTS is load-bearing, not tidiness. Plain LIKE copies NOT
    # NULL but NOT the DEFAULT, so a column declared `not null default 'open'`
    # arrives here as `not null` with nothing to fill it -- and since these
    # columns are workflow state rather than content, the CSV does not supply
    # them. COPY then fails on row 1 with a null violation, which is exactly
    # how go_review_queue.review_status died. go_amendment_extractions
    # (verification_status, extracted_at) is the same shape and was next.
    #
    # Safe here because the schema uses `generated always as identity`, never
    # serial: INCLUDING DEFAULTS does not copy an identity, so no staging table
    # can attach itself to a real table's sequence and burn ids. Were any
    # column a serial, this would silently start consuming the live sequence.
    cur.execute(f"create temp table {name} "
                f"(like {table} including defaults) on commit drop")
    for col in drop:
        cur.execute(f"alter table {name} drop column if exists {col}")
    for col, typ in add:
        cur.execute(f"alter table {name} add column {col} {typ}")


def session_setup(cur):
    """Lift the timeouts that kill a bulk COPY.

    Supabase ships statement_timeout = 2min. A 275 MB COPY is ONE statement, so
    it does not creep up on the limit, it sails straight through it -- the first
    attempt at this load died at line 832,660 of 73,954 records' worth of CSV,
    with the transaction rolled back whole. Raising the timeout is the fix
    rather than splitting the COPY into chunks, because one COPY per table is
    what makes the load atomic: either the table arrives or nothing does.

    idle_in_transaction_session_timeout matters for the same reason -- the
    child-table loads hold a transaction open while a large INSERT ... SELECT
    runs, and the pooler is entitled to cut an idle transaction otherwise.

    Both are SESSION-scoped, so this changes nothing for any other client, and
    nothing persists after the connection closes. It also only works on the
    SESSION pooler (5432); the transaction pooler would not keep these.
    """
    cur.execute("set statement_timeout = 0")
    cur.execute("set idle_in_transaction_session_timeout = 0")


def batch(cur, stage, input_path, rows_read, rows_written, ok, notes=None):
    cur.execute("insert into load_batches (stage, finished_at, input_path, "
                "rows_read, rows_written, ok, notes) "
                "values (%s, now(), %s, %s, %s, %s, %s)",
                (stage, input_path, rows_read, rows_written, ok, notes))


def load():
    import psycopg
    url = dsn()
    print("connecting (session pooler) ...")
    with psycopg.connect(url, autocommit=False) as conn:
        with conn.cursor() as cur:
            session_setup(cur)
            # The seeded table and this loader must agree on the id scheme,
            # since department_id is assigned here by sorted name.
            ids, _ = canonical_departments()
            cur.execute("select id, name from departments order by id")
            seeded = {n: i for i, n in cur.fetchall()}
            assert seeded == ids, (
                "departments table does not match canonical_departments(); "
                "reseed before loading")

            # A table that already holds its full complement is SKIPPED, not
            # reloaded. Each table commits on its own, so a failure part-way
            # through (the first run died on go_recipients, with GOs and
            # references already committed) leaves a valid prefix of the load
            # rather than a mess. Re-running then resumes instead of demanding a
            # truncate and another 275 MB COPY.
            #
            # "Full complement" means the exact expected count, so a table that
            # is merely NON-empty -- a half-applied table -- is not mistaken for
            # a finished one. It stops the run instead.
            def done(table, expect):
                cur.execute(f"select count(*) from {table}")
                n = cur.fetchone()[0]
                if n == expect:
                    print(f"  {table} already holds {n:,}, skipping")
                    return True
                if n:
                    sys.exit(f"{table} holds {n:,} rows, expected 0 or {expect:,}. "
                             f"Half-applied; truncate it and re-run.")
                return False

            # 1. body aliases (department_aliases already holds the 46 codes).
            # No skip guard: the insert is `on conflict (alias) do nothing`, so
            # it is already idempotent and costs a 433 KB COPY to repeat.
            print("loading department_aliases (body_text) ...")
            staging(cur, "_al", "department_aliases")
            copy_csv(cur, "_al", ["alias", "department_id", "alias_kind"],
                     LOAD / "department_aliases_body.csv")
            cur.execute("insert into department_aliases "
                        "(alias, department_id, alias_kind) "
                        "select alias, department_id, alias_kind from _al "
                        "on conflict (alias) do nothing")
            cur.execute("select count(*) from department_aliases "
                        "where alias_kind = 'body_text'")
            print("  body aliases", cur.fetchone()[0])
            conn.commit()

            # 2. the GOs themselves
            if not done("ap_government_orders", EXPECTED_TOTAL):
                print("loading ap_government_orders (275 MB, this takes a while) ...")
                staging(cur, "_go", "ap_government_orders",
                        drop=("id", "loaded_at", "status"))
                copy_csv(cur, "_go", GO_COLUMNS, LOAD / "ap_government_orders.csv")
                cols = ", ".join(GO_COLUMNS)
                cur.execute(f"insert into ap_government_orders ({cols}) "
                            f"select {cols} from _go")
                cur.execute("select count(*) from ap_government_orders")
                n = cur.fetchone()[0]
                print("  ap_government_orders", f"{n:,}")
                assert n == EXPECTED_TOTAL, f"loaded {n}, expected {EXPECTED_TOTAL}"
                batch(cur, "stage", "out/load/ap_government_orders.csv", n, n, True)
                conn.commit()

            # 3. children, resolved through source_file -> the generated go_id
            _child(cur, conn, "go_references",
                   ["seq", "ref_kind", "go_type", "go_number",
                    "department_raw", "date_raw", "raw_text"])
            _child(cur, conn, "go_recipients", ["seq", "recipient"])
            _child(cur, conn, "go_review_queue", ["problem", "detail"])
            _child(cur, conn, "go_field_provenance",
                   ["field", "pass", "status", "value", "previous_value",
                    "previous_tier", "status_before_fallback", "source",
                    "remark", "how", "evidence", "evidence_line"])
            # csv_order is REQUIRED here: parse_tables() writes table_id first,
            # source_file second. It must stay identical to the Table(...)
            # declaration in parse_tables(); COPY matches by position, and both
            # columns are text, so a mismatch does not raise, it transposes.
            go_tables_cols = [
                "table_id", "logical_table_id", "table_index",
                "page_number", "pages", "bbox", "n_rows", "n_cols",
                "is_continuation", "continuation_of", "part_index",
                "part_count", "column_labels", "column_headers",
                "headers_identified", "headers_inherited",
                "detection_strategy", "structure_confidence",
                "review_required", "review_reasons"]
            _child(cur, conn, "go_tables", go_tables_cols,
                   csv_order=["table_id", "source_file"] + go_tables_cols[1:])

            # cells hang off go_tables, not off the GO, so no id resolution
            print("loading go_table_cells ...")
            staging(cur, "_ce", "go_table_cells")
            copy_csv(cur, "_ce",
                     ["cell_id", "table_id", "row_index", "column_index",
                      "column_label", "column_header", "is_header", "text",
                      "row_span", "col_span", "bbox"],
                     LOAD / "go_table_cells.csv")
            cur.execute("insert into go_table_cells select * from _ce "
                        "on conflict do nothing")
            cur.execute("select count(*) from go_table_cells")
            print("  go_table_cells", f"{cur.fetchone()[0]:,}")
            conn.commit()

            _child(cur, conn, "go_amendment_extractions",
                   ["go_class", "confidence", "flags", "executive_label",
                    "amendment_summary", "instrument_count", "provision_count",
                    "source_text", "source_text_kind"], pk="go_id")
            _amendment_children(cur, conn)
            batch(cur, "promote", "out/load/", None, None, True,
                  "children resolved through source_file")
            conn.commit()
    print("load: OK")


def _child(cur, conn, table, columns, pk=None, csv_order=None):
    """COPY a child CSV, then resolve its source_file to the generated go_id.

    `columns` is what gets INSERTed. `csv_order` is the PHYSICAL column order
    of the file, which must mirror the Table(...) declaration in the generator
    that wrote it; it defaults to source_file first because that is what most
    of the generators do -- but go_tables writes table_id first, and COPY
    matches by position, not by name.

    That default used to be an assumption rather than a parameter, and it cost
    a whole table: go_tables loaded the table_id hash into source_file and the
    path into table_id. Every column is text and every OTHER column still lined
    up, so nothing raised -- the join simply compared a 16-hex hash to a
    filesystem path, matched nothing, and dropped all 4,292 rows.

    The join is an inner join on a unique key, so a staged row whose
    source_file is not in the corpus cannot be loaded orphaned. No generator
    here emits such a row, so a drop is not a tolerance, it is a BUG -- and
    this refuses to commit rather than printing a warning into a log.
    """
    path = LOAD / f"{table}.csv"
    # Resume support. Each child table is one COPY plus one INSERT inside a
    # single transaction that is committed here and nowhere else, so the table
    # is either empty or complete -- there is no half-applied state to detect.
    # A non-zero count therefore means done, and the table is skipped.
    #
    # The CSV row count is reported alongside it but deliberately NOT used as
    # the test, because the resolving join is an INNER join: a staged row whose
    # source_file is not in the corpus is dropped on purpose, so a complete
    # table can legitimately hold fewer rows than its CSV. Requiring equality
    # would abort the resume it exists to enable.
    #
    # And it is counted with the csv reader, not by counting newlines:
    # recipient, raw_text and order_text carry embedded newlines inside their
    # quoted fields, so physical lines run well ahead of rows.
    cur.execute(f"select count(*) from {table}")
    have = cur.fetchone()[0]
    if have:
        print(f"loading {table} ... already holds {have:,} "
              f"(csv has {csv_rows(path):,}), skipping")
        return
    print(f"loading {table} ...")
    drop = ["go_id"] + ([] if pk == "go_id" else ["id"])
    staging(cur, "_c", table, drop=drop, add=[("source_file", "text")])
    copy_csv(cur, "_c", csv_order or (["source_file"] + columns), path)
    cur.execute("select count(*) from _c")
    staged = cur.fetchone()[0]
    sel = ", ".join(f"c.{c}" for c in columns)
    cur.execute(f"insert into {table} (go_id, {', '.join(columns)}) "
                f"select g.id, {sel} from _c c "
                f"join ap_government_orders g on g.source_file = c.source_file "
                f"on conflict do nothing")
    cur.execute(f"select count(*) from {table}")
    loaded = cur.fetchone()[0]
    print(f"  staged {staged:,}  loaded {loaded:,}"
          + ("" if staged == loaded else f"  ** {staged - loaded:,} DROPPED **"))
    # Refuse the commit. Printing the drop and carrying on is what let an empty
    # go_tables be committed and the run continue -- it was a foreign key on
    # go_table_cells, not this function, that eventually objected, and only
    # because the cells happen to reference the tables. A child table with no
    # such referent would have been silently left empty.
    if staged != loaded:
        conn.rollback()
        sys.exit(f"{table}: {staged - loaded:,} of {staged:,} staged rows did "
                 f"not resolve to a GO. Every generator emits only corpus "
                 f"source_files, so this is a column-order or content bug, not "
                 f"a tolerable shortfall. Nothing was committed.")
    batch(cur, "promote", str(path), staged, loaded, staged == loaded)
    conn.commit()


def _amendment_children(cur, conn):
    print("loading go_amendment_targets ...")
    staging(cur, "_t", "go_amendment_targets",
            drop=["id", "go_id"], add=[("source_file", "text")])
    tcols = ["ord", "name_verbatim", "name_best", "name_best_source",
             "name_abstract", "instr_type", "kind"]
    copy_csv(cur, "_t", ["source_file"] + tcols,
             LOAD / "go_amendment_targets.csv")
    cur.execute(f"insert into go_amendment_targets (go_id, {', '.join(tcols)}) "
                f"select g.id, {', '.join('t.' + c for c in tcols)} from _t t "
                f"join ap_government_orders g on g.source_file = t.source_file "
                f"on conflict do nothing")
    cur.execute("select count(*) from go_amendment_targets")
    print("  go_amendment_targets", f"{cur.fetchone()[0]:,}")
    conn.commit()

    print("loading go_amendment_provisions ...")
    # target_ord is the position within the GO, which is how a provision finds
    # its target: (go_id, ord) is unique on go_amendment_targets.
    staging(cur, "_pv", "go_amendment_provisions",
            drop=["id", "target_id"],
            add=[("source_file", "text"), ("target_ord", "int")])
    pcols = ["ord", "provision", "action", "action_code", "previous_text",
             "new_text", "clause_quote", "effective_date", "spans_anchored"]
    copy_csv(cur, "_pv", ["source_file", "target_ord"] + pcols,
             LOAD / "go_amendment_provisions.csv")
    cur.execute("select count(*) from _pv")
    staged = cur.fetchone()[0]
    cur.execute(f"insert into go_amendment_provisions "
                f"(target_id, {', '.join(pcols)}) "
                f"select t.id, {', '.join('p.' + c for c in pcols)} from _pv p "
                f"join ap_government_orders g on g.source_file = p.source_file "
                f"join go_amendment_targets t "
                f"  on t.go_id = g.id and t.ord = p.target_ord "
                f"on conflict do nothing")
    cur.execute("select count(*) from go_amendment_provisions")
    loaded = cur.fetchone()[0]
    print(f"  staged {staged:,}  loaded {loaded:,}")
    assert staged == loaded, f"{staged - loaded} provisions failed to resolve"
    conn.commit()


def wave2():
    """The columns that cannot exist until every GO has an id.

    go_references.resolved_go_id and go_amendments.amends_go_id are joins from
    one corpus row to another, so they are meaningless until the whole corpus is
    loaded -- hence a separate stage rather than more work inside load().

    WHO DECIDES WHAT: the matching is NOT done here. resolve_references.py works
    it out offline against the CSVs, where it can be re-run and re-measured
    without a database, and writes source_file pairs. This stage only turns
    those pairs into ids. That keeps the one piece of judgement in the pass that
    documents and validates it, and keeps the SQL a join.

    `status` is deliberately left NULL except where it is known. A GO becomes
    'amended' on the evidence of a resolved amendment pointing at it; nothing in
    this corpus establishes supersession or repeal, so nothing is written for
    those. Marking the remaining 70k 'in_force' would be an assertion the data
    does not support, and 'unknown' says no more than NULL already does.
    """
    import psycopg
    refs = LOAD / "go_reference_links.csv"
    amds = LOAD / "go_amendment_links.csv"
    for p in (refs, amds):
        if not p.exists():
            sys.exit(f"{p.name} missing. Run: python3 resolve_references.py --apply")

    with psycopg.connect(dsn(), autocommit=False) as conn:
        with conn.cursor() as cur:
            session_setup(cur)
            cur.execute("select count(*) from ap_government_orders")
            total = cur.fetchone()[0]
            if total != EXPECTED_TOTAL:
                sys.exit(f"corpus holds {total:,}, expected {EXPECTED_TOTAL:,}. "
                         f"Run the load stage first.")

            # 1. citation -> corpus GO
            print("resolving go_references.resolved_go_id ...")
            cur.execute("drop table if exists _rl")
            cur.execute("create temp table _rl (source_file text, seq smallint, "
                        "target text, how text) on commit drop")
            copy_csv(cur, "_rl", ["source_file", "seq", "target", "how"], refs)
            cur.execute("select count(*) from _rl")
            staged = cur.fetchone()[0]
            cur.execute("update go_references r set resolved_go_id = t.id "
                        "from _rl l "
                        "join ap_government_orders c on c.source_file = l.source_file "
                        "join ap_government_orders t on t.source_file = l.target "
                        "where r.go_id = c.id and r.seq = l.seq")
            done = cur.rowcount
            print(f"  staged {staged:,}  linked {done:,}")
            assert staged == done, f"{staged - done} links did not land"
            batch(cur, "wave2", str(refs), staged, done, True,
                  "go_references.resolved_go_id")
            conn.commit()

            # 2. amendment -> the GO it amends
            print("resolving go_amendments.amends_go_id ...")
            cur.execute("drop table if exists _am")
            cur.execute("create temp table _am (source_file text, target text) "
                        "on commit drop")
            copy_csv(cur, "_am", ["source_file", "target"], amds)
            cur.execute("select count(*) from _am")
            staged = cur.fetchone()[0]
            cur.execute("insert into go_amendments (go_id, amends_go_id) "
                        "select c.id, t.id from _am a "
                        "join ap_government_orders c on c.source_file = a.source_file "
                        "join ap_government_orders t on t.source_file = a.target "
                        "on conflict (go_id) do update "
                        "set amends_go_id = excluded.amends_go_id")
            done = cur.rowcount
            print(f"  staged {staged:,}  linked {done:,}")
            assert staged == done, f"{staged - done} amendment links did not land"
            batch(cur, "wave2", str(amds), staged, done, True,
                  "go_amendments.amends_go_id")

            # 3. the one status the evidence supports
            cur.execute("update ap_government_orders g set status = 'amended' "
                        "where exists (select 1 from go_amendments a "
                        "              where a.amends_go_id = g.id)")
            print(f"  marked 'amended' {cur.rowcount:,}  "
                  f"(every other status left NULL: not established)")
            batch(cur, "wave2", "-", cur.rowcount, cur.rowcount, True,
                  "ap_government_orders.status = amended")
            conn.commit()
    print("WAVE 2 COMPLETE")


def main():
    stages = {"parse": parse, "load": load, "wave2": wave2}
    if len(sys.argv) < 2 or sys.argv[1] not in stages:
        sys.exit(__doc__)
    stages[sys.argv[1]]()


if __name__ == "__main__":
    main()
