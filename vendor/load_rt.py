#!/usr/bin/env python3
"""The RT corpus -> Supabase, APPENDED to the MS corpus already there.

    python3 load_rt.py prepare    # no DB, no credentials. Writes out/rt/load/*.csv
    python3 load_rt.py load       # APPENDS those CSVs (needs an explicit go-ahead)
    python3 load_rt.py verify     # independent re-check

── WHY NOT load_supabase.py ───────────────────────────────────────────────────

That loader is a one-shot bulk load of a fresh corpus and says so: its `done()`
guard skips a table holding exactly EXPECTED_TOTAL (73,954) and EXITS on any
other non-zero count. Pointed at a database that already holds MS, it stops —
correctly, because appending was never its job.

Its `parse()` stage is also unusable here: it reads six sidecars and three
adjudication directories (out/departments, out/category, out/gotype) that exist
for MS and not for RT.

So this is a separate loader that reuses what applies and supplies the rest.
The pieces imported rather than reimplemented:

  * `load_supabase.canonical_departments()` — the department name -> id map, so
    the two loaders cannot disagree about which id a department has.
  * `classify_category.classify_record()` — a pure function, deterministic
    phrase rules, no LLM and no cost. MS's categories came from it; RT's come
    from the same code rather than from a second implementation.

── WHAT THIS SUPPLIES THAT MS GOT FROM A SIDECAR ──────────────────────────────

department_id   From the FILENAME's department code, not the body. Measured:
                447,100 of 447,100 RT records carry a dept_code that resolves
                against canonical_departments(). The body's department string
                is kept verbatim in department_raw, as MS does.

routing_bucket  NOT NULL with a CHECK. Mapped onto the existing six values —
                no new ones — from what each pass found:
                  clean               parsed, has a body, confidence >= 0.5
                  needs_review        parsed, has a body, confidence < 0.5
                  unreadable_no_text  no body: empty, withheld, not issued…
                  unreadable_not_pdf  no body: Word 6.0, Lotus, an executable
                MS's own distribution has 2,525 in unreadable_not_pdf, which is
                precisely the class of the 368 RT files named .pdf that are not
                PDFs at all.

source_path     The natural key from migration 032: 'RT/' + the path below the
                corpus root. source_file keeps the absolute path because it is
                still NOT NULL and because `_child` joins on it.

── WHAT IS DELIBERATELY ABSENT ────────────────────────────────────────────────

No sidecar precedence. MS has six sidecars adjudicating number/date/government
disputes, built over months of review; RT has none, so `go_number_source` and
`go_date_source` record where the value actually came from ('document_header'
or 'goir_register') and nothing claims an adjudication that did not happen.

No GO-to-GO link resolution. `resolve_references.py` matches citations across
the whole corpus and must run after every id exists, exactly as `wave2` does
for MS. It is a separate step and not this script's job.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import pathlib
import sys
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import load_supabase as ls  # noqa: E402
from classify_category import classify_record  # noqa: E402

RT_ROOT = pathlib.Path(
    "/Users/kushdudeja/Desktop/kush 2026 mac/AskMyJunior/AP GOs Download Code/AP GO RT type"
)
OUT = pathlib.Path("out/rt")
LOAD = OUT / "load"

# GO_COLUMNS from load_supabase, plus the three columns that did not exist when
# it was written. Imported rather than retyped so a change there reaches here.
GO_COLUMNS = ls.GO_COLUMNS + ["source_path", "text_recovery_status", "text_recovery_method"]

# Which files hold what. Absences carry no body and no children.
WITH_TEXT = ["rt_2*.jsonl", "rt_2*_ocr.jsonl", "rt_leftovers_records.jsonl"]
ABSENT = ["rt_2*_absent.jsonl", "rt_leftovers_absent.jsonl"]


def rel(source_file: str) -> str:
    """'RT/Patel/2019/FIN01 - FINANCE-RT-492-26_03_2019.pdf'"""
    return "RT/" + str(pathlib.Path(source_file).relative_to(RT_ROOT))


def bucket(rec: dict, has_text: bool) -> str:
    if has_text:
        return "clean" if (rec.get("extraction_confidence") or 0) >= 0.5 else "needs_review"
    # No body. Two reasons, and they are different facts about the file.
    for w in rec.get("warnings") or []:
        if str(w).startswith("legacy_format:"):
            return "unreadable_not_pdf"
    return "unreadable_no_text"


def row(rec: dict, has_text: bool, dept_ids: dict, dept_codes: dict) -> dict | None:
    fp = rec.get("filename_parsed") or {}
    code = fp.get("dept_code")
    dept_name = dept_codes.get(code)
    if dept_name is None:
        return None  # counted by the caller; never silently dropped
    dep = rec.get("department") or {}
    gm = rec.get("go_meta") or {}
    gd = gm.get("go_date") or {}
    sig = rec.get("signature") or {}
    am = rec.get("amendment") or {}

    # The document's value where the parser found one, else the register's.
    # `_source` says which, and neither overwrites the other: both columns are
    # written, as rule 2 of load_supabase requires.
    body_no, reg_no = gm.get("go_number"), fp.get("go_number")
    body_dt, reg_dt = gd.get("iso"), fp.get("date_iso")
    cat = classify_record(rec) if has_text else {}

    return {
        "source_file": rec["source_file"],
        "source_path": rel(rec["source_file"]),
        "filename": rec["filename"],
        "go_year": rec.get("go_year") or fp.get("go_year"),
        "go_number": body_no if body_no is not None else reg_no,
        "go_number_goir_register": reg_no,
        "go_number_source": "document_header" if body_no is not None else "goir_register",
        "go_number_remark": None,
        "go_date": body_dt or reg_dt,
        "date_uploaded_goir_portal": None,
        "go_date_source": "document_header" if body_dt else "date_uploaded_goir_portal",
        "go_date_remark": None,
        # MS's convention, and getting this backwards cost a correction pass:
        # go_type holds the PORTAL's filing (from the filename, which says RT
        # on all 447,925), go_type_document the document's own, set only where
        # it differs. The first version put the document's type in BOTH, which
        # lost the portal's RT from 14,530 rows entirely — exactly the defect
        # migration 031 exists to prevent, reintroduced by the loader.
        "go_type": fp.get("go_type"),
        "go_type_document": rec.get("go_type") if rec.get("go_type") != fp.get("go_type") else None,
        "department_id": dept_ids[dept_name],
        "department_raw": dep.get("full_department_name") or dep.get("department_name"),
        "sub_department": dep.get("sub_department"),
        "dept_code": code,
        "cross_filed_department_id": None,
        "government": rec.get("government"),
        "government_source": "document_header" if rec.get("government") else None,
        "abstract": rec.get("abstract"),
        "order_text": rec.get("order_text"),
        "signature_name": sig.get("name"),
        "signature_designation": sig.get("designation"),
        "employee_name": rec.get("employee_name"),
        "is_amendment": bool(am.get("is_amendment")),
        "amends_go_number": am.get("amends_go_number"),
        "amendment_keyword": am.get("keyword"),
        "apgo_amendment_type": rec.get("apgo_amendment_type"),
        "apgo_category": rec.get("apgo_category"),
        "go_category": cat.get("go_category"),
        "go_sub_category": cat.get("go_sub_category"),
        "secondary_category_tags": ls.pg_array(cat.get("secondary_tags") or []),
        "category_status": cat.get("status"),
        "category_evidence": cat.get("evidence"),
        "category_rule": cat.get("rule"),
        "category_basis": cat.get("basis"),
        "extraction_confidence": rec.get("extraction_confidence"),
        "text_quality": rec.get("text_quality"),
        "page_count": rec.get("page_count") or 0,
        "is_eoffice": bool(rec.get("is_eoffice")),
        "has_table": bool(rec.get("has_table")),
        # anchors_found is a smallint COUNT, not a list — MS stores 11, not
        # the names. Passing the parser's list made COPY reject the row.
        "anchors_found": len(rec.get("anchors_found") or []) or None,
        # text[] columns need a Postgres array literal. `fld` stringifies a
        # Python list as "['Budget Release']", which COPY refuses with
        # "malformed array literal: [ must introduce explicitly-specified
        # array dimensions". pg_array is the helper that already exists.
        # NOT NULL with a '{}' default, and COPY's explicit NULL beats
        # the default — so an empty list must become an empty ARRAY, not None.
        "warnings": ls.pg_array(rec.get("warnings") or []),
        "routing_bucket": bucket(rec, has_text),
        "text_recovery_status": rec.get("text_recovery_status"),
        "text_recovery_method": rec.get("text_recovery_method"),
    }


def files_for(patterns: list[str]) -> list[str]:
    out: list[str] = []
    for p in patterns:
        for f in sorted(glob.glob(str(OUT / p))):
            if any(x in f for x in ("sha256", "_absent", "census")) and "_absent" not in p:
                continue
            out.append(f)
    return sorted(set(out))


def prepare() -> None:
    LOAD.mkdir(parents=True, exist_ok=True)
    # Table writes to load_supabase.LOAD, a module constant pointing at the MS
    # load directory. Repointed so RT's CSVs land beside RT's other output and
    # cannot overwrite MS's — which are still on disk and still the record of
    # what was loaded.
    ls.LOAD = LOAD
    dept_ids, dept_codes = ls.canonical_departments()
    print(f"departments: {len(dept_ids)} canonical, {len(dept_codes)} codes")

    go = ls.Table("ap_government_orders", GO_COLUMNS)
    refs = ls.Table("go_references", ["source_file", "seq", "ref_kind", "go_type",
                                      "go_number", "department_raw", "date_raw", "raw_text"])
    recips = ls.Table("go_recipients", ["source_file", "seq", "recipient"])

    counts: Counter[str] = Counter()
    seen_paths: set[str] = set()
    unresolved: list[str] = []

    for has_text, pats in ((True, WITH_TEXT), (False, ABSENT)):
        for f in files_for(pats):
            for line in open(f):
                if not line.strip():
                    continue
                rec = json.loads(line)
                r = row(rec, has_text, dept_ids, dept_codes)
                if r is None:
                    unresolved.append(rec["source_file"])
                    continue
                # source_path is the natural key and the load will reject a
                # duplicate. Catching it here names the file instead.
                if r["source_path"] in seen_paths:
                    counts["duplicate_source_path"] += 1
                    continue
                seen_paths.add(r["source_path"])
                go.row(*[r[c] for c in GO_COLUMNS])
                counts["with_text" if has_text else "absent"] += 1
                counts[r["routing_bucket"]] += 1
                if has_text:
                    # The parser's own key names, checked against a record
                    # rather than guessed: ref_kind (not kind), date_raw (not
                    # date), and it supplies its own seq. The first version
                    # used kind/date and wrote NULL into ref_kind, which is
                    # NOT NULL — the COPY refused it, correctly.
                    for i, ref in enumerate(rec.get("references") or []):
                        refs.row(rec["source_file"], ref.get("seq", i),
                                 ref.get("ref_kind"), ref.get("go_type"),
                                 ref.get("go_number"), ref.get("department"),
                                 ref.get("date_raw"), ref.get("raw_text"))
                    for i, rcp in enumerate(rec.get("recipients") or []):
                        recips.row(rec["source_file"], i, rcp)

    for t in (go, refs, recips):
        t.close()

    if unresolved:
        (LOAD / "unresolved_dept_code.txt").write_text("\n".join(unresolved) + "\n")

    print(f"\n  ap_government_orders  {go.n:,}")
    print(f"  go_references         {refs.n:,}")
    print(f"  go_recipients         {recips.n:,}")
    print(f"\n  with text {counts['with_text']:,}   absent {counts['absent']:,}")
    print("  routing buckets:")
    for b in ("clean", "needs_review", "unreadable_no_text", "unreadable_not_pdf"):
        print(f"     {b:<22}{counts[b]:>8,}")
    if unresolved:
        print(f"\n  !! {len(unresolved):,} records had an unresolvable dept_code "
              f"-> {LOAD/'unresolved_dept_code.txt'}")
    if counts["duplicate_source_path"]:
        print(f"  !! {counts['duplicate_source_path']:,} duplicate source_path, skipped")
    json.dump({"rows": go.n, "refs": refs.n, "recipients": recips.n,
               "counts": dict(counts), "unresolved": len(unresolved)},
              (LOAD / "prepare_report.json").open("w"), indent=1)


def rt_child(cur, conn, table: str, columns: list[str]) -> None:
    """Like load_supabase._child, but counting only the RT rows.

    _child CANNOT be reused here and the reason is worth stating, because the
    failure is silent. Its resume guard is `select count(*) from <table>`: a
    non-zero count means done, and it skips. That is correct for a fresh
    corpus and catastrophic here — go_references already holds MS's 185,949
    rows, so _child would print "already holds 185,949, skipping" and load
    none of RT's 824,285. The run would report success and do nothing.

    So the guard counts children belonging to RT rows instead. Everything else
    — the staging table, the inner join through source_file, the dropped-row
    check — is the same, and the helpers are imported rather than copied.
    """
    path = LOAD / f"{table}.csv"
    cur.execute(f"select count(*) from {table} c "
                f"join ap_government_orders g on g.id = c.go_id "
                f"where g.source_path like 'RT/%'")
    have = cur.fetchone()[0]
    if have:
        print(f"loading {table} ... already holds {have:,} RT rows, skipping")
        return
    print(f"loading {table} ...")
    ls.staging(cur, "_rtc", table, drop=["go_id", "id"],
               add=[("source_file", "text")])
    ls.copy_csv(cur, "_rtc", ["source_file"] + columns, path)
    cur.execute("select count(*) from _rtc")
    staged = cur.fetchone()[0]
    sel = ", ".join(f"c.{c}" for c in columns)
    cur.execute(f"insert into {table} (go_id, {', '.join(columns)}) "
                f"select g.id, {sel} from _rtc c "
                f"join ap_government_orders g on g.source_file = c.source_file "
                f"on conflict do nothing")
    cur.execute(f"select count(*) from {table} c "
                f"join ap_government_orders g on g.id = c.go_id "
                f"where g.source_path like 'RT/%'")
    loaded = cur.fetchone()[0]
    print(f"  staged {staged:,}  loaded {loaded:,}"
          + ("" if staged == loaded else f"  ** {staged - loaded:,} DROPPED **"))
    # A drop is a bug, not a tolerance: every source_file in these CSVs came
    # from a record this pipeline also wrote to ap_government_orders.
    assert staged == loaded, f"{staged - loaded:,} {table} rows did not resolve"
    conn.commit()


def load() -> None:
    """APPEND the prepared CSVs. Never truncates — MS is in this table.

    Resume is by COUNTING RT ROWS, not by a flag: go_type='RT' with an 'RT/'
    source_path is the whole test, so an interrupted run can be re-run and
    picks up from whatever actually committed. Each table commits on its own,
    exactly as load_supabase does, so a failure part way leaves a valid prefix.
    """
    import psycopg
    # _child, staging and copy_csv all read load_supabase's module-level LOAD.
    # prepare() repoints it; load() runs as a separate invocation and must too.
    ls.LOAD = LOAD
    # COUNTED WITH THE CSV READER, NOT BY NEWLINES. order_text carries embedded
    # newlines inside its quoted field, so physical lines run far ahead of rows
    # — load_supabase._child's own comment warns about exactly this and the
    # first version of this line made the mistake anyway.
    expect = ls.csv_rows(LOAD / "ap_government_orders.csv")
    print(f"CSV holds {expect:,} rows")

    with psycopg.connect(ls.dsn(), autocommit=False) as conn:
        with conn.cursor() as cur:
            ls.session_setup(cur)

            # The department map this CSV was built against must still be the
            # one in the database, or department_id means something else now.
            ids, _ = ls.canonical_departments()
            cur.execute("select id, name from departments order by id")
            assert {n: i for i, n in cur.fetchall()} == ids, \
                "departments table no longer matches canonical_departments()"

            cur.execute("select count(*) from ap_government_orders where source_path like 'RT/%'")
            have = cur.fetchone()[0]
            if have == expect:
                print(f"  ap_government_orders already holds {have:,} RT rows, skipping")
            elif have:
                sys.exit(f"ap_government_orders holds {have:,} RT rows, expected 0 or "
                         f"{expect:,}. Half-applied; delete where source_path like 'RT/%' "
                         f"and re-run.")
            else:
                cur.execute("select count(*) from ap_government_orders")
                before = cur.fetchone()[0]
                print(f"  MS rows present: {before:,} — these are NOT touched")
                print("  loading ap_government_orders (1.2 GB) ...")
                ls.staging(cur, "_rtgo", "ap_government_orders",
                           drop=("id", "loaded_at", "status"))
                ls.copy_csv(cur, "_rtgo", GO_COLUMNS, LOAD / "ap_government_orders.csv")
                cols = ", ".join(GO_COLUMNS)
                cur.execute(f"insert into ap_government_orders ({cols}) "
                            f"select {cols} from _rtgo")
                cur.execute("select count(*) from ap_government_orders")
                after = cur.fetchone()[0]
                assert after - before == expect, \
                    f"added {after-before:,}, expected {expect:,}"
                print(f"  ap_government_orders {before:,} -> {after:,}")
                conn.commit()

            # Children join on source_file, which is still unique. They are
            # restricted to RT rows by the join itself: an MS source_file is
            # not in these CSVs.
            for table, cols in (("go_references",
                                 ["seq", "ref_kind", "go_type", "go_number",
                                  "department_raw", "date_raw", "raw_text"]),
                                ("go_recipients", ["seq", "recipient"])):
                rt_child(cur, conn, table, cols)

    print("\nloaded. NEXT, and neither is optional:")
    print("  1. populate go_search_index for the RT rows (it has no trigger)")
    print("  2. re-run migration 031's backfill, or RT orders whose two")
    print("     identities disagree are findable by one and not the other")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["prepare", "load"])
    a = ap.parse_args()
    {"prepare": prepare, "load": load}[a.stage]()


if __name__ == "__main__":
    main()
