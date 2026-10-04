#!/usr/bin/env python3
"""The GOIR catch-up -> Supabase, built exactly the way the corpus was built.

    python3 load_catchup.py prepare   # parse + build CSVs. Reads the DB once, writes nothing.
    python3 load_catchup.py upload    # files -> R2, content-addressed, verified by ETag
    python3 load_catchup.py load      # orders, references, recipients, search index
    python3 load_catchup.py load_held # the listings with no document (see below)
    python3 load_catchup.py verify    # independent re-count

The record of the October 2026 catch-up (6,820 orders, 20 Jun - 4 Oct). What a
row contains is decided in pipeline.py, which the daily sync shares, so the
two cannot build an order differently; this file is only the batch around it.

Input is what fetch_catchup.py left in out/: fetched.jsonl (one line per portal
listing row) and out/pdfs/<filename>.

── NOTHING HERE IS A SECOND IMPLEMENTATION ────────────────────────────────────

Every decision about what a row contains is made by code the corpus was already
built with, vendored from go-ingestion rather than rewritten (pipeline.py):

  go_parser.parse_pdf / parse_text   the parse itself
  load_rt.row                        the 48-column row, its conventions and its
                                     comments about what got this wrong before
  clean_abstract                     abstract / abstract_source / status
  classify_no_text + PLAN            what a file with no body actually is
  rebuild_search_index.TSV_EXPR      the tsvector, with its canonical joins
  upload_r2.key_for / sniff          the content-addressed R2 key

What this file adds is only what the portal knows and a file on disk does not:
the gid, the portal's category, the wing a department belongs to, and the fact
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
                Here it holds the listing's date, which is the filename's date,
                which is what schema.sql says the column means.

── ORDERS THE PORTAL LISTS WITHOUT A DOCUMENT WERE HELD, THEN LOADED ──────────

201 listing rows have no file: no English document listed, or a 0-byte reply.
The order page fetched the file unconditionally and told the reader to "Try
again" for a file that does not exist, so they were held until askmyjunior-web
9804eab taught the page to say why there is no file, then loaded by
`load_held` (2026-10-04, the user's choice).

── WHY THE R2 UPLOAD COMES BEFORE THE LOAD ────────────────────────────────────

A row's source_object_key is a promise that the object exists. `load` refuses
to run unless every key it is about to write is in the upload ledger, verified
by ETag, so no row is ever visible on the site pointing at nothing.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

from pipeline import (  # noqa: F401  (the batch shares every rule with sync.py)
    DEPT_RE, GO_COLUMNS, INDEX_SQL, MAX_DISK, RCP_COLUMNS, REF_COLUMNS,
    build_row, cg, disk, held_row, key_for, ls, ocr_scans, parse_many,
)

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
PDFS = OUT / "pdfs"
FETCHED = OUT / "fetched.jsonl"
LOAD = OUT / "load"
PARSED = LOAD / "parsed.jsonl"
HELD = LOAD / "held_no_document.jsonl"
LEDGER = LOAD / "r2_ledger.jsonl"
REPORT = LOAD / "prepare_report.json"
OCR_CACHE = OUT / "ocr_cache"


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------

def existing(listings: list[dict]) -> tuple[set[str], set[str]]:
    """gids and filenames already in the corpus. One read, nothing written."""
    gids = [str(r["gid_english"]) for r in listings if r.get("gid_english")]
    names = [r["filename"] for r in listings]
    with cg.conn() as c, c.cursor() as cur:
        cur.execute("set statement_timeout = '10min'")
        cur.execute("select goir_gid from public.ap_government_orders where goir_gid = any(%s)", (gids,))
        have_gid = {r[0] for r in cur.fetchall()}
        # No index on filename; one sequential read of the table, deliberately.
        cur.execute("select filename from public.ap_government_orders where filename = any(%s)", (names,))
        have_name = {r[0] for r in cur.fetchall()}
    return have_gid, have_name


def prepare(workers: int) -> None:
    LOAD.mkdir(parents=True, exist_ok=True)
    ls.LOAD = LOAD
    listings = [json.loads(l) for l in FETCHED.open() if l.strip()]
    print(f"fetched.jsonl: {len(listings):,} listing rows")

    names = Counter(r["filename"] for r in listings)
    dup = [n for n, k in names.items() if k > 1]
    assert not dup, f"{len(dup)} filenames repeat in the fetch: {dup[:3]}"

    have_gid, have_name = existing(listings)
    print(f"already in the corpus: {len(have_gid):,} by gid, {len(have_name):,} by filename")

    dept_ids, dept_codes = ls.canonical_departments()
    counts: Counter[str] = Counter()
    todo: list[dict] = []
    held: list[dict] = []
    for r in listings:
        present_gid = r.get("gid_english") and str(r["gid_english"]) in have_gid
        present_name = r["filename"] in have_name
        if present_gid or present_name:
            counts["already_present"] += 1
            if bool(present_gid) != bool(present_name):
                counts["present_by_one_key_only"] += 1
            continue
        if r.get("pdf") is None:
            held.append(r)
            continue
        m = DEPT_RE.match(r.get("organization") or "")
        if not m or m.group(1) not in dept_codes:
            sys.exit(f"unresolvable department {r.get('organization')!r} on {r['filename']}")
        todo.append(r)

    with HELD.open("w") as fh:
        for r in held:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"to load: {len(todo):,}   held (no document on the portal): {len(held):,}")

    paths = [str(PDFS / r["filename"]) for r in todo]
    missing = [p for p in paths if not Path(p).exists()]
    assert not missing, f"{len(missing)} files missing from {PDFS}: {missing[:3]}"

    t0 = time.time()
    parsed = parse_many(paths, workers)
    with PARSED.open("w") as fh:
        for res in parsed.values():
            fh.write(json.dumps({k: v for k, v in res.items() if k != "rec"}
                                | {"order_text_chars": len(res["rec"].get("order_text") or "")},
                                ensure_ascii=False) + "\n")
    print(f"  parsed {len(parsed):,}/{len(paths):,}  {time.time() - t0:5.0f}s", flush=True)

    # The catch-up ran on the Mac, with the corpus's own Vision OCR.
    ocr_tally = ocr_scans(list(parsed.values()), workers, "vision", OCR_CACHE)

    go = ls.Table("ap_government_orders", GO_COLUMNS)
    refs = ls.Table("go_references", ["source_file"] + REF_COLUMNS)
    recips = ls.Table("go_recipients", ["source_file"] + RCP_COLUMNS)
    anomalies: list[dict] = []

    for r, path in zip(todo, paths):
        row, ref_rows, rcp_rows = build_row(r, parsed[path], dept_ids, dept_codes, counts, anomalies)
        go.row(*[row[c] for c in GO_COLUMNS])
        for x in ref_rows:
            refs.row(*x)
        for x in rcp_rows:
            recips.row(*x)

    for t in (go, refs, recips):
        t.close()
    counts.update(ocr_tally)

    print(f"\n  ap_government_orders  {go.n:,}")
    print(f"  go_references         {refs.n:,}")
    print(f"  go_recipients         {recips.n:,}")
    print(f"  held, no document     {len(held):,}")
    print()
    for k in sorted(counts):
        print(f"  {k:<58}{counts[k]:>7,}")
    if anomalies:
        print(f"\n  !! {len(anomalies)} filename/listing disagreements — see {REPORT.name}")
    json.dump({"rows": go.n, "refs": refs.n, "recipients": recips.n, "held": len(held),
               "counts": dict(counts), "anomalies": anomalies},
              REPORT.open("w"), indent=1, ensure_ascii=False)


# ---------------------------------------------------------------------------
# upload — every file a row will reference, into R2, before any row exists
# ---------------------------------------------------------------------------

def read_ledger() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if LEDGER.exists():
        for line in LEDGER.open():
            if line.strip():
                e = json.loads(line)
                out[e["sha"]] = e
    return out


def upload(workers: int) -> None:
    """Content-addressed PUTs through upload_r2's own _put, which checks the
    stored object's ETag against the MD5 of the bytes sent. Resumable: the
    ledger is appended per object, and a re-PUT of a known key writes the same
    bytes it already holds."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    import upload_r2 as ur

    cfg = ur.creds()
    s3 = ur.client(cfg)
    bucket = cfg["R2_BUCKET"]
    parsed = [json.loads(line) for line in PARSED.open() if line.strip()]
    done = read_ledger()
    todo: dict[str, dict] = {}
    for p in parsed:
        if p["sha"] not in done:
            todo.setdefault(p["sha"], p)
    distinct = len({p["sha"] for p in parsed})
    print(f"{len(parsed):,} files, {distinct:,} distinct objects; "
          f"{len(done):,} already uploaded; {len(todo):,} to upload")

    def one(p: dict):
        data = Path(p["path"]).read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        if sha != p["sha"]:
            return ("fail", p["path"], "file changed on disk since prepare")
        res = ur._put(s3, bucket, key_for(sha), data, sha, Path(p["path"]).name)
        if res is not None:
            return ("fail", p["path"], res[1])
        return ("ok", sha, key_for(sha), len(data))

    fails = []
    lock = threading.Lock()
    t0, sent = time.time(), 0
    with ThreadPoolExecutor(max_workers=workers) as pool, LEDGER.open("a") as fh:
        for i, res in enumerate(pool.map(one, list(todo.values())), 1):
            if res[0] == "ok":
                with lock:
                    fh.write(json.dumps({"sha": res[1], "key": res[2], "bytes": res[3],
                                         "uploaded_at": dt.datetime.now(dt.timezone.utc).isoformat()})
                             + "\n")
                    fh.flush()
                sent += res[3]
            else:
                fails.append(res)
            if i % 250 == 0 or i == len(todo):
                el = time.time() - t0
                print(f"  {i:,}/{len(todo):,}  {sent / 1e6:7.1f} MB  {sent / 1e6 / max(el, 1e-9):5.2f} MB/s  "
                      f"{len(fails)} failed", flush=True)
    if fails:
        for f in fails[:20]:
            print("  FAIL", f)
        sys.exit(f"{len(fails)} uploads failed; nothing is loaded until every object is in R2. Re-run.")
    have = read_ledger()
    missing = {p["sha"] for p in parsed} - set(have)
    assert not missing, f"{len(missing)} objects absent from the ledger"
    print(f"all {distinct:,} objects in R2 and in the ledger")


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------

def csv_column(path: Path, columns: list[str], name: str) -> list[str]:
    import csv
    csv.field_size_limit(10**9)
    i = columns.index(name)
    with path.open(newline="", encoding="utf-8") as fh:
        return [row[i] for row in csv.reader(fh)]


def load() -> None:
    """APPEND. Never updates or deletes an existing row.

    Resume is by counting these rows by source_file (unique, indexed), so an
    interrupted run re-runs and continues from whatever committed. The orders
    commit as one statement; children and index commit after them, each
    guarded by its own count.
    """
    import psycopg

    ls.LOAD = LOAD
    go_csv = LOAD / "ap_government_orders.csv"
    expect = ls.csv_rows(go_csv)
    sfs = csv_column(go_csv, GO_COLUMNS, "source_file")
    shas = set(csv_column(go_csv, GO_COLUMNS, "source_sha256"))
    assert len(sfs) == expect == len(set(sfs))
    ledger = read_ledger()
    missing = shas - set(ledger)
    if missing:
        sys.exit(f"{len(missing):,} objects not yet in R2 — run `upload` first. "
                 f"No row is written that points at a file the bucket does not hold.")
    print(f"CSV holds {expect:,} orders; all {len(shas):,} objects are in R2")

    with psycopg.connect(ls.dsn(), autocommit=False) as conn:
        with conn.cursor() as cur:
            ls.session_setup(cur)
            start = disk(cur)
            print(f"disk {start / 1024**3:.2f} GB (db + WAL), stop at {MAX_DISK / 1024**3:.2f}")
            if start > MAX_DISK:
                sys.exit("over the disk line before starting; not loading")

            from pipeline import check_departments
            check_departments(cur)

            cur.execute("select count(*) from public.ap_government_orders where source_file = any(%s)", (sfs,))
            have = cur.fetchone()[0]
            if have == expect:
                print(f"  ap_government_orders already holds all {have:,}, skipping")
            elif have:
                sys.exit(f"ap_government_orders holds {have:,} of these {expect:,}: half-applied. "
                         f"It was one INSERT, so this should be impossible — investigate before re-running.")
            else:
                cur.execute("select count(*) from public.ap_government_orders")
                before = cur.fetchone()[0]
                ls.staging(cur, "_cgo", "public.ap_government_orders", drop=("id", "loaded_at", "status"))
                ls.copy_csv(cur, "_cgo", GO_COLUMNS, go_csv)
                cur.execute("create temp table _led (sha text primary key, uploaded_at timestamptz not null) "
                            "on commit drop")
                with cur.copy("copy _led (sha, uploaded_at) from stdin") as cp:
                    for e in ledger.values():
                        cp.write_row((e["sha"], e["uploaded_at"]))
                cur.execute("update _cgo s set source_uploaded_at = l.uploaded_at "
                            "from _led l where l.sha = s.source_sha256")
                cur.execute("select count(*) from _cgo where source_uploaded_at is null")
                assert cur.fetchone()[0] == 0, "a row's object is not in the ledger"
                cols = ", ".join(GO_COLUMNS)
                cur.execute(f"insert into public.ap_government_orders ({cols}) select {cols} from _cgo")
                assert cur.rowcount == expect, f"inserted {cur.rowcount:,}, expected {expect:,}"
                conn.commit()
                cur.execute("select count(*) from public.ap_government_orders")
                after = cur.fetchone()[0]
                print(f"  ap_government_orders {before:,} -> {after:,}")
                assert after - before == expect

            cur.execute("select id from public.ap_government_orders where source_file = any(%s)", (sfs,))
            ids = [r[0] for r in cur.fetchall()]
            assert len(ids) == expect

            for table, cols in (("go_references", REF_COLUMNS), ("go_recipients", RCP_COLUMNS)):
                path = LOAD / f"{table}.csv"
                want = ls.csv_rows(path)
                cur.execute(f"select count(*) from public.{table} where go_id = any(%s)", (ids,))
                got = cur.fetchone()[0]
                if got == want:
                    print(f"  {table} already holds all {got:,}, skipping")
                    continue
                if got:
                    sys.exit(f"{table} holds {got:,} of {want:,} for these orders: half-applied")
                drop = ["go_id", "id"] if table == "go_references" else ["go_id"]
                ls.staging(cur, "_cch", f"public.{table}", drop=drop, add=[("source_file", "text")])
                ls.copy_csv(cur, "_cch", ["source_file"] + cols, path)
                sel = ", ".join(f"c.{c}" for c in cols)
                cur.execute(f"insert into public.{table} (go_id, {', '.join(cols)}) "
                            f"select g.id, {sel} from _cch c "
                            f"join public.ap_government_orders g on g.source_file = c.source_file")
                assert cur.rowcount == want, f"{table}: {cur.rowcount:,} of {want:,} resolved"
                conn.commit()
                print(f"  {table} +{want:,}")

            cur.execute(INDEX_SQL, (ids,))
            added = cur.rowcount
            conn.commit()
            cur.execute("select count(*), count(tsv), count(tsv_a) from public.go_search_index "
                        "where go_id = any(%s)", (ids,))
            n, n_tsv, n_tsva = cur.fetchone()
            print(f"  go_search_index +{added:,}  (now {n:,} of {expect:,}; tsv {n_tsv:,}, tsv_a {n_tsva:,})")
            assert n == n_tsv == n_tsva == expect

            # The corpus counts move the moment the corpus does (migration 009).
            t0 = time.time()
            cur.execute("select public.go_taxonomy_counts_refresh()")
            cur.execute("delete from public.go_facets_cache")
            conn.commit()
            print(f"  taxonomy counts refreshed, facet cache cleared ({time.time() - t0:.1f}s)")
            end = disk(cur)
            print(f"disk {end / 1024**3:.2f} GB (+{(end - start) / 1024**2:,.0f} MB)")


# ---------------------------------------------------------------------------
# the held listings: orders the portal lists with no document
# ---------------------------------------------------------------------------

def held_rows() -> list[dict]:
    dept_ids, dept_codes = ls.canonical_departments()
    return [held_row(json.loads(line), dept_ids, dept_codes) for line in HELD.open() if line.strip()]


def load_held() -> None:
    """Insert the held listings. Append-only, and only after the order page
    learnt to show them (askmyjunior-web 9804eab, deployed 2026-10-04)."""
    import psycopg
    rows = held_rows()
    sfs = [r["source_file"] for r in rows]
    assert len(set(sfs)) == len(sfs)
    print(f"{len(rows)} held listings:",
          ", ".join(f"{m} {n}" for m, n in Counter(r["text_recovery_method"] for r in rows).most_common()))
    ls.LOAD = LOAD
    held_csv = LOAD / "held_orders.csv"
    t = ls.Table("held_orders", GO_COLUMNS)
    for r in rows:
        t.row(*[r[c] for c in GO_COLUMNS])
    t.close()

    with psycopg.connect(ls.dsn(), autocommit=False) as conn, conn.cursor() as cur:
        ls.session_setup(cur)
        start = disk(cur)
        if start > MAX_DISK:
            sys.exit("over the disk line; not loading")
        cur.execute("select count(*) from public.ap_government_orders where source_file = any(%s)", (sfs,))
        have = cur.fetchone()[0]
        if have and have != len(rows):
            sys.exit(f"{have} of {len(rows)} already present: half-applied, investigate")
        if not have:
            # A listing the earlier load already carried, under its gid, would
            # be a duplicate order. The held rows have no file, so this cannot
            # happen by construction — checked anyway.
            gids = [r["goir_gid"] for r in rows if r["goir_gid"]]
            cur.execute("select count(*) from public.ap_government_orders where goir_gid = any(%s)", (gids,))
            assert cur.fetchone()[0] == 0, "a held gid is already in the corpus"
            ls.staging(cur, "_chd", "public.ap_government_orders", drop=("id", "loaded_at", "status"))
            ls.copy_csv(cur, "_chd", GO_COLUMNS, held_csv)
            cols = ", ".join(GO_COLUMNS)
            cur.execute(f"insert into public.ap_government_orders ({cols}) select {cols} from _chd")
            assert cur.rowcount == len(rows)
            conn.commit()
            print(f"  ap_government_orders +{len(rows)}")
        cur.execute("select id from public.ap_government_orders where source_file = any(%s)", (sfs,))
        ids = [r[0] for r in cur.fetchall()]
        cur.execute(INDEX_SQL, (ids,))
        added = cur.rowcount
        conn.commit()
        cur.execute("select count(*), count(tsv) from public.go_search_index where go_id = any(%s)", (ids,))
        n, n_tsv = cur.fetchone()
        print(f"  go_search_index +{added} (now {n} of {len(rows)}, tsv {n_tsv})")
        assert n == n_tsv == len(rows)
        cur.execute("select public.go_taxonomy_counts_refresh()")
        cur.execute("delete from public.go_facets_cache")
        conn.commit()
        print(f"  counts refreshed, facet cache cleared; disk {disk(cur) / 1024**3:.2f} GB")


def verify() -> None:
    """Re-count from the database alone, and ask R2 for every object."""
    import psycopg
    import upload_r2 as ur
    from concurrent.futures import ThreadPoolExecutor

    go_csv = LOAD / "ap_government_orders.csv"
    sfs = csv_column(go_csv, GO_COLUMNS, "source_file")
    fails = []
    with psycopg.connect(ls.dsn()) as conn, conn.cursor() as cur:
        cur.execute("""select count(*), count(distinct g.goir_gid), count(s.go_id),
                              count(*) filter (where g.source_object_key is null),
                              count(*) filter (where g.source_uploaded_at is null),
                              min(g.go_date), max(g.go_date),
                              count(*) filter (where g.go_type = 'MS'),
                              count(*) filter (where g.go_type = 'RT')
                         from public.ap_government_orders g
                         left join public.go_search_index s on s.go_id = g.id
                        where g.source_file = any(%s)""", (sfs,))
        n, gids, idx, nokey, noup, lo, hi, ms, rt = cur.fetchone()
        print(f"orders {n:,} (MS {ms:,}, RT {rt:,}); distinct gids {gids:,}; indexed {idx:,}; "
              f"dates {lo} .. {hi}")
        if not (n == gids == idx == len(sfs)):
            fails.append("orders / gids / index rows disagree")
        if nokey or noup:
            fails.append(f"{nokey} rows without an object key, {noup} without an upload time")
        cur.execute("""select d.name, count(*) from public.ap_government_orders g
                         join public.departments d on d.id = g.department_id
                        where g.source_file = any(%s) group by 1 order by 2 desc limit 8""", (sfs,))
        print("top departments:", ", ".join(f"{a} {b:,}" for a, b in cur.fetchall()))
        cur.execute("""select count(*) from public.ap_government_orders g
                        where g.source_file = any(%s)
                          and exists (select 1 from public.ap_government_orders o
                                       where o.goir_gid = g.goir_gid and o.id <> g.id)""", (sfs,))
        dup = cur.fetchone()[0]
        print(f"gids shared with another row: {dup}")
        if dup:
            fails.append(f"{dup} gids duplicated")
        cur.execute("select source_object_key from public.ap_government_orders where source_file = any(%s)",
                    (sfs,))
        keys = sorted({r[0] for r in cur.fetchall()})

    cfg = ur.creds()
    s3 = ur.client(cfg)

    def head(k):
        try:
            s3.head_object(Bucket=cfg["R2_BUCKET"], Key=k)
            return None
        except Exception as e:  # noqa: BLE001
            return f"{k}: {type(e).__name__}"

    with ThreadPoolExecutor(max_workers=16) as pool:
        bad = [r for r in pool.map(head, keys) if r]
    print(f"R2: {len(keys) - len(bad):,} of {len(keys):,} objects present")
    if bad:
        fails.append(f"{len(bad)} objects missing from R2: {bad[:3]}")
    if fails:
        for f in fails:
            print("FAIL", f)
        sys.exit(1)
    print("verified")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["prepare", "upload", "load", "load_held", "verify"])
    ap.add_argument("--workers", type=int, default=max(2, (os.cpu_count() or 4) - 2))
    a = ap.parse_args()
    if a.stage == "prepare":
        prepare(a.workers)
    elif a.stage == "upload":
        upload(16)
    elif a.stage == "load":
        load()
    elif a.stage == "load_held":
        load_held()
    else:
        verify()


if __name__ == "__main__":
    main()
