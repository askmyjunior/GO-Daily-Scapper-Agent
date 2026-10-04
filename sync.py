#!/usr/bin/env python3
"""GOIR -> AskMyJunior, every day. Runs on GitHub Actions at 19:00 IST.

    python3 sync.py --mode dry-run      # read the portal, parse; write nothing anywhere
    python3 sync.py                     # the daily run
    python3 sync.py --lookback 60       # a deeper sweep (Sundays do this by themselves)
    python3 sync.py --limit 5           # at most 5 new orders, for a first careful run

What a run does, in order, and why each step is where it is:

 1. WINDOW    The last 15 days by G.O. date, 60 on Sundays. Departments upload
              late, so reading only "yesterday" would miss an order for good.
              Never earlier than where the catch-up began (MS 20 Jun, RT 30 Jun
              2026): before that, orders live in the original corpus under its
              own paths, and 220 of them have no portal gid to match on.
 2. LISTING   Every MS and RT row the portal lists, each day read in full and
              checked against the portal's own count. A day that cannot be read
              in full is not recorded, and fails the run.
 3. DIFF      A listing is NEW unless its source_file or its portal gid is
              already in the corpus. One already loaded is UPGRADED when it was
              loaded incomplete and can now be finished: a listing that had no
              document and now has one, or a scan loaded before OCR could run.
 4. DOWNLOAD  The English document. A 0-byte reply is the portal saying "no
              file", not a failure.
 5. PARSE     pipeline.parse_one — the corpus's own parser — and Tesseract for
              scans, behind the same gates the corpus's Vision OCR passed.
 6. R2        Every file, content-addressed and ETag-verified, BEFORE any row
              refers to it. A row whose file did not land is not written.
 7. LOAD      One transaction: orders, references, recipients, search index,
              upgrades. The day's orders arrive whole or not at all.
 8. CLASSIFY  Gemini, abstract only, into the live taxonomy. Out of credit is
              not a failure to load: the orders are already live, uncategorised,
              and the next run classifies them.
 9. REFRESH   Taxonomy counts and the facet cache, so the site's numbers move
              the moment the corpus does.

Exit status: 0 clean. 1 something was not done — a day unread, a download
failed, a department unknown — and everything that WAS done is committed; the
next run's window covers it again. 2 orders loaded but classification stopped
(usually Gemini credit). GitHub emails the repository owner on non-zero.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import goir_portal as G  # noqa: E402
import pipeline as P  # noqa: E402
from pipeline import ls  # noqa: E402

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover - zoneinfo data missing
    IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

TYPES = ("MS", "RT")
#: Where the catch-up began, per type (fetch_catchup.START). Nothing earlier.
FLOOR = {"MS": dt.date(2026, 6, 20), "RT": dt.date(2026, 6, 30)}
DAILY_LOOKBACK, SUNDAY_LOOKBACK = 15, 60
#: How far back classification looks for this pipeline's unclassified orders.
CLASSIFY_DAYS = 45


class Run:
    def __init__(self) -> None:
        self.counts: Counter = Counter()
        self.problems: list[str] = []
        self.anomalies: list[dict] = []
        self.classification_stopped: str | None = None
        self.spend = 0.0

    def problem(self, msg: str) -> None:
        print(f"  !! {msg}", flush=True)
        self.problems.append(msg)


# ---------------------------------------------------------------------------
# 1-2. window and listing
# ---------------------------------------------------------------------------

def portal_reachable(run: Run) -> bool:
    """One short look at the portal before anything else.

    goir.ap.gov.in answers only connections from India: tested 2026-10-04
    from 40 locations, Mumbai answered in 0.27 s and all 39 elsewhere timed
    out. Without this check a machine outside India spends three 90-second
    timeouts on every one of 120 day-listings and is killed at 45 minutes —
    which is exactly what the first GitHub run did.
    """
    err = None
    for _ in range(2):
        try:
            G._fetch(G.BASE, timeout=25)
            return True
        except Exception as e:  # noqa: BLE001
            err = e
    run.problem(f"the GOIR portal did not answer from this machine ({type(err).__name__}: {err}). "
                f"It accepts connections only from India.")
    return False


def window(until: dt.date, lookback: int) -> list[dt.date]:
    return [until - dt.timedelta(days=i) for i in range(lookback - 1, -1, -1)]


def read_listings(days: list[dt.date], run: Run) -> list[dict]:
    out: list[dict] = []
    seen: dict[str, str] = {}
    for go_type in TYPES:
        for day in days:
            if day < FLOOR[go_type]:
                continue
            rows, err = None, None
            for attempt in range(3):
                try:
                    rows = G.listing_complete(day, go_type)
                    break
                except Exception as e:  # noqa: BLE001 — a flaky portal, retried
                    err = e
                    time.sleep(5 * (attempt + 1))
            if rows is None:
                run.problem(f"{go_type} {day}: not read in full ({type(err).__name__}: {err})")
                continue
            run.counts[f"listed_{go_type}"] += len(rows)
            for r in rows:
                fn = P.filename_for(r, go_type)
                if fn is None:
                    run.problem(f"{go_type} {day}: unparsable listing row {r.get('go_number_raw')!r}")
                    continue
                key = r.get("gid_english") or r.get("gid_telugu") or ""
                if fn in seen and seen[fn] != key:
                    # Two portal rows that would get the same name. The natural
                    # key cannot hold both; a human decides which is the order.
                    run.problem(f"two listings share the name {fn!r}; the second is not loaded")
                    continue
                seen[fn] = key
                out.append({**r, "go_type": go_type, "filename": fn,
                            "source_file": P.source_file_for(go_type, P.listing_day(r), fn)})
    return out


# ---------------------------------------------------------------------------
# 3. diff
# ---------------------------------------------------------------------------

def diff(conn, listings: list[dict], run: Run) -> tuple[list[dict], list[dict]]:
    """-> (new listings, listings whose loaded row can now be completed)."""
    sfs = [r["source_file"] for r in listings]
    gids = [str(r["gid_english"]) for r in listings if r.get("gid_english")]
    with conn.cursor() as cur:
        cur.execute("""select source_file, goir_gid, text_recovery_status
                         from public.ap_government_orders
                        where source_file = any(%s) or goir_gid = any(%s)""", (sfs, gids))
        found = cur.fetchall()
    by_sf = {f[0]: f for f in found}
    by_gid = {f[1]: f for f in found if f[1]}
    engine = P.ocr_engine()
    new, upgrade = [], []
    for r in listings:
        gid = str(r["gid_english"]) if r.get("gid_english") else None
        have = by_sf.get(r["source_file"]) or (by_gid.get(gid) if gid else None)
        if have is None:
            new.append(r)
        elif have[0] != r["source_file"]:
            run.counts["present_under_another_path"] += 1
        elif have[2] == "NO_DOCUMENT_ON_PORTAL" and gid:
            upgrade.append(r)        # listed without a file before; it has a gid now
        elif have[2] == "OCR_REQUIRED" and engine:
            upgrade.append(r)        # a scan loaded before OCR could run
        else:
            run.counts["already_loaded"] += 1
    return new, upgrade


# ---------------------------------------------------------------------------
# 4. download
# ---------------------------------------------------------------------------

def download(rows: list[dict], workdir: Path, run: Run, delay: float = 0.3) -> list[tuple[dict, str | None]]:
    """-> (listing, local path or None). None is a listing with no document."""
    out = []
    for r in rows:
        if not r.get("gid_english"):
            out.append((r, None))
            continue
        data, err = None, None
        for attempt in range(3):
            try:
                data = G.pdf(r["gid_english"], "E")
                break
            except FileNotFoundError:
                data = b""           # 0-byte 200: the portal has no file
                break
            except Exception as e:  # noqa: BLE001
                err = e
                time.sleep(3 * (attempt + 1))
        if data is None:
            run.problem(f"download failed: {r['filename']} ({type(err).__name__}: {err})")
            continue
        if not data:
            out.append((r, None))
            continue
        path = workdir / r["filename"]
        path.write_bytes(data)
        out.append((r, str(path)))
        run.counts["downloaded"] += 1
        time.sleep(delay)
    return out


# ---------------------------------------------------------------------------
# 5. parse and build
# ---------------------------------------------------------------------------

def build(got: list[tuple[dict, str | None]], upgrade_sfs: set[str], workers: int, run: Run):
    """-> (rows, references, recipients, sha -> path of every file a row refers to)."""
    dept_ids, dept_codes = ls.canonical_departments()
    paths = [p for _, p in got if p]
    parsed = P.parse_many(paths, workers)
    run.counts.update(P.ocr_scans(list(parsed.values()), workers, P.ocr_engine()))

    rows, refs, recips, files = [], [], [], {}
    for r, path in got:
        m = P.DEPT_RE.match(r.get("organization") or "")
        if not m or m.group(1) not in dept_codes:
            run.problem(f"unknown department {r.get('organization')!r} on {r['filename']} — "
                        f"add it to resolve_departments.CANONICAL and the departments table")
            continue
        upgrading = r["source_file"] in upgrade_sfs
        if path is None:
            if upgrading:
                run.counts["upgrade_still_no_document"] += 1
                continue
            rows.append(P.held_row(r, dept_ids, dept_codes))
            run.counts["new_without_document"] += 1
            continue
        res = parsed[path]
        row, ref_rows, rcp_rows = P.build_row(r, res, dept_ids, dept_codes, run.counts, run.anomalies)
        if upgrading and row["text_recovery_status"] == "OCR_REQUIRED":
            run.counts["upgrade_ocr_still_pending"] += 1
            continue
        rows.append(row)
        refs += ref_rows
        recips += rcp_rows
        files[res["sha"]] = path
        run.counts["upgraded" if upgrading else "new_with_document"] += 1
    return rows, refs, recips, files


# ---------------------------------------------------------------------------
# 6. R2
# ---------------------------------------------------------------------------

def upload(files: dict[str, str], run: Run) -> dict[str, str]:
    """sha -> upload time, for every object R2 confirmed by ETag."""
    if not files:
        return {}
    import upload_r2 as ur
    cfg = ur.creds()
    s3 = ur.client(cfg)

    def one(item):
        sha, path = item
        data = Path(path).read_bytes()
        res = ur._put(s3, cfg["R2_BUCKET"], P.key_for(sha), data, sha, Path(path).name)
        return sha, res

    ledger = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for sha, res in pool.map(one, files.items()):
            if res is None:
                ledger[sha] = dt.datetime.now(dt.timezone.utc).isoformat()
            else:
                run.problem(f"R2 upload failed for {Path(files[sha]).name}: {res[1]}")
    run.counts["uploaded_objects"] = len(ledger)
    return ledger


# ---------------------------------------------------------------------------
# 7. load — one transaction
# ---------------------------------------------------------------------------

def connect():
    """Retry a dropped connection; NEVER retry an authentication failure —
    repeated ones trip the pooler's circuit breaker and block every client
    for ~15 minutes (project memory, 2026-10-03)."""
    import psycopg
    for attempt in range(4):
        try:
            return psycopg.connect(ls.dsn(), autocommit=False, connect_timeout=20)
        except psycopg.OperationalError as e:
            msg = str(e)
            if "authentication" in msg or "ECIRCUITBREAKER" in msg or attempt == 3:
                raise
            time.sleep(10 * (attempt + 1))


def load(conn, rows: list[dict], refs: list[tuple], recips: list[tuple],
         ledger: dict[str, str], upgrade_sfs: set[str], run: Run) -> list[int]:
    """Insert new orders, complete upgraded ones, their children and index rows."""
    # A row whose file did not reach R2 is not written: its key would point at nothing.
    keep = [r for r in rows if r["source_object_key"] is None or r["source_sha256"] in ledger]
    dropped = {r["source_file"] for r in rows} - {r["source_file"] for r in keep}
    refs = [x for x in refs if x[0] not in dropped]
    recips = [x for x in recips if x[0] not in dropped]
    if not keep:
        return []
    sfs = [r["source_file"] for r in keep]
    upgrades = [sf for sf in sfs if sf in upgrade_sfs]

    work = Path(tempfile.mkdtemp(prefix="goir-load-"))
    ls.LOAD = work
    t = ls.Table("orders", P.GO_COLUMNS)
    for r in keep:
        t.row(*[r[c] for c in P.GO_COLUMNS])
    t.close()
    tr = ls.Table("refs", ["source_file"] + P.REF_COLUMNS)
    for x in refs:
        tr.row(*x)
    tr.close()
    tc = ls.Table("recips", ["source_file"] + P.RCP_COLUMNS)
    for x in recips:
        tc.row(*x)
    tc.close()

    cols = ", ".join(P.GO_COLUMNS)
    with conn.cursor() as cur:
        ls.session_setup(cur)
        used = P.disk(cur)
        if used > P.MAX_DISK:
            raise RuntimeError(f"database + WAL at {used / 1024**3:.2f} GB, over the "
                               f"{P.MAX_DISK / 1024**3:.1f} GB line; not writing")
        P.check_departments(cur)

        ls.staging(cur, "_sgo", "public.ap_government_orders", drop=("id", "loaded_at", "status"))
        ls.copy_csv(cur, "_sgo", P.GO_COLUMNS, work / "orders.csv")
        cur.execute("create temp table _led (sha text primary key, uploaded_at timestamptz not null) "
                    "on commit drop")
        with cur.copy("copy _led (sha, uploaded_at) from stdin") as cp:
            for sha, ts in ledger.items():
                cp.write_row((sha, ts))
        cur.execute("update _sgo s set source_uploaded_at = l.uploaded_at "
                    "from _led l where l.sha = s.source_sha256")
        cur.execute("select count(*) from _sgo where source_object_key is not null "
                    "and source_uploaded_at is null")
        assert cur.fetchone()[0] == 0, "a row's object is not in the upload ledger"

        cur.execute(f"insert into public.ap_government_orders ({cols}) "
                    f"select {cols} from _sgo s where not (s.source_file = any(%s)) "
                    f"on conflict (source_file) do nothing", (upgrades,))
        run.counts["inserted"] = cur.rowcount

        if upgrades:
            # Everything but the identity: these rows exist and keep their id,
            # their URL and any category already given to them.
            upd = [c for c in P.GO_COLUMNS if c not in ("source_file", "source_path", "filename")]
            cur.execute(f"update public.ap_government_orders g set "
                        + ", ".join(f"{c} = s.{c}" for c in upd)
                        + " from _sgo s where g.source_file = s.source_file"
                        + " and g.source_file = any(%s)", (upgrades,))
            run.counts["updated"] = cur.rowcount

        cur.execute("select id, source_file from public.ap_government_orders where source_file = any(%s)",
                    (sfs,))
        ids_by_sf = dict((sf, i) for i, sf in cur.fetchall())
        ids = list(ids_by_sf.values())
        upgraded_ids = [ids_by_sf[sf] for sf in upgrades if sf in ids_by_sf]

        for table, ccols, path in (("go_references", P.REF_COLUMNS, work / "refs.csv"),
                                   ("go_recipients", P.RCP_COLUMNS, work / "recips.csv")):
            if upgraded_ids:
                cur.execute(f"delete from public.{table} where go_id = any(%s)", (upgraded_ids,))
            drop = ["go_id", "id"] if table == "go_references" else ["go_id"]
            ls.staging(cur, "_sch", f"public.{table}", drop=drop, add=[("source_file", "text")])
            ls.copy_csv(cur, "_sch", ["source_file"] + ccols, path)
            sel = ", ".join(f"c.{c}" for c in ccols)
            cur.execute(f"insert into public.{table} (go_id, {', '.join(ccols)}) "
                        f"select g.id, {sel} from _sch c "
                        f"join public.ap_government_orders g on g.source_file = c.source_file "
                        f"where not exists (select 1 from public.{table} x where x.go_id = g.id)")
            cur.execute("drop table _sch")

        if upgraded_ids:
            cur.execute("delete from public.go_search_index where go_id = any(%s)", (upgraded_ids,))
        cur.execute(P.INDEX_SQL, (ids,))
        cur.execute("select count(*) from public.go_search_index where go_id = any(%s) and tsv is not null",
                    (ids,))
        indexed = cur.fetchone()[0]
        assert indexed == len(ids), f"{indexed} of {len(ids)} orders indexed"
    conn.commit()
    run.counts["indexed"] = len(ids)
    return ids


# ---------------------------------------------------------------------------
# 8. classify
# ---------------------------------------------------------------------------

def classify(conn, run: Run, dry: bool) -> None:
    """This pipeline's recent orders that have a subject and no category."""
    import classify_new as C
    with conn.cursor() as cur:
        cur.execute(f"""select g.id, g.abstract,
                               case when coalesce(g.abstract, '') = '' then left(g.order_text, %s) end
                          from public.ap_government_orders g
                         where g.loaded_at > now() - interval '{CLASSIFY_DAYS} days'
                           and g.source_file like 'goir-daily/%%'
                           and not exists (select 1 from public.go_classification_ai a
                                            where a.go_id = g.id)""", (C.BODY_CHARS,))
        found = cur.fetchall()
    todo = []
    for gid, abstract, body in found:
        if C.NO_SUBJECT.match(abstract or ""):
            continue
        if (abstract or "").strip():
            todo.append((gid, "abstract: " + " ".join(abstract.split())))
        elif (body or "").strip():
            todo.append((gid, "order: " + " ".join(body.split())))
    run.counts["to_classify"] = len(todo)
    if not todo or dry:
        return

    block, num, enum = C.taxonomy()
    system = C.INSTR + block
    chunks = [todo[i:i + C.BATCH] for i in range(0, len(todo), C.BATCH)]
    answers = []
    try:
        with ThreadPoolExecutor(max_workers=C.WORKERS) as pool:
            for got, usage in pool.map(lambda ch: C.classify(ch, system, enum, num), chunks):
                run.spend += P.cg.money(usage)
                answers += got
    except SystemExit as e:
        # classify_gemini.post stops with SystemExit on HTTP 402 (no credit)
        # and on any non-transient error. The orders are already loaded.
        run.classification_stopped = str(e).strip().splitlines()[0] if str(e).strip() else "stopped"
        print(f"  !! classification stopped: {run.classification_stopped}", flush=True)
    if answers:
        with conn.cursor() as cur:
            cur.executemany("""insert into public.go_classification_ai
                                   (go_id, primary_category, sub_category, confidence, model, go_date)
                               select %s, %s, %s, %s, %s, g.go_date
                                 from public.ap_government_orders g where g.id = %s
                               on conflict (go_id) do nothing""",
                            [(a["id"], a["cat"], a["sub"], a["conf"], a["model"], a["id"]) for a in answers])
        conn.commit()
    run.counts["classified"] = len(answers)


def refresh(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("select public.go_taxonomy_counts_refresh()")
        cur.execute("delete from public.go_facets_cache")
    conn.commit()


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def report(run: Run, days: list[dt.date], mode: str, started: float) -> str:
    c = run.counts
    lines = [
        f"## GOIR sync — {mode}",
        "",
        f"Window **{days[0]:%d %b} – {days[-1]:%d %b %Y}** ({len(days)} days), "
        f"{(time.time() - started) / 60:.1f} min.",
        "",
        "| | |", "|---|---:|",
        f"| Listed on the portal (MS / RT) | {c['listed_MS']:,} / {c['listed_RT']:,} |",
        f"| Already loaded | {c['already_loaded'] + c['present_under_another_path']:,} |",
        f"| **New orders with a document** | **{c['new_with_document']:,}** |",
        f"| New listings without a document | {c['new_without_document']:,} |",
        f"| Completed (document appeared / scan OCR'd) | {c['upgraded']:,} |",
        f"| Scans OCR'd (Tesseract) | {sum(v for k, v in c.items() if k.startswith('ocr_')):,} |",
        f"| Classified | {c['classified']:,} of {c['to_classify']:,} (${run.spend:.2f}) |",
    ]
    if run.classification_stopped:
        lines += ["", f"**Classification stopped:** {run.classification_stopped}. "
                      "The orders are live and uncategorised; the next run classifies them."]
    if run.problems:
        lines += ["", f"### {len(run.problems)} problem(s)", ""] + [f"- {p}" for p in run.problems[:40]]
    if run.anomalies:
        lines += ["", f"{len(run.anomalies)} filename/listing disagreement(s) recorded in the JSON report."]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["full", "dry-run"], default="full")
    ap.add_argument("--lookback", type=int, default=None, help="days by G.O. date (default 15; 60 on Sundays)")
    ap.add_argument("--until", default=None, help="last day of the window, YYYY-MM-DD (default today, IST)")
    ap.add_argument("--limit", type=int, default=None, help="at most this many NEW orders this run")
    ap.add_argument("--workers", type=int, default=max(2, (os.cpu_count() or 2)))
    a = ap.parse_args()

    started = time.time()
    run = Run()
    dry = a.mode == "dry-run"
    today = dt.datetime.now(IST).date()
    until = dt.date.fromisoformat(a.until) if a.until else today
    lookback = a.lookback or (SUNDAY_LOOKBACK if today.weekday() == 6 else DAILY_LOOKBACK)
    days = window(until, lookback)
    print(f"GOIR sync ({a.mode}): {days[0]} .. {days[-1]}, OCR engine: {P.ocr_engine() or 'none'}", flush=True)

    if not portal_reachable(run):
        text = report(run, days, a.mode, started)
        print("\n" + text)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
                fh.write(text)
        return 1

    listings = read_listings(days, run)
    print(f"  listed {len(listings):,} (MS {run.counts['listed_MS']:,}, RT {run.counts['listed_RT']:,})", flush=True)

    conn = connect()
    try:
        new, upgrade = diff(conn, listings, run)
        if a.limit is not None and len(new) > a.limit:
            run.counts["deferred_by_limit"] = len(new) - a.limit
            new = new[:a.limit]
        print(f"  new {len(new):,}, to complete {len(upgrade):,}", flush=True)

        workdir = Path(tempfile.mkdtemp(prefix="goir-"))
        got = download(new + upgrade, workdir, run)
        upgrade_sfs = {r["source_file"] for r in upgrade}
        rows, refs, recips, files = build(got, upgrade_sfs, a.workers, run)

        if not dry and rows:
            ledger = upload(files, run)
            load(conn, rows, refs, recips, ledger, upgrade_sfs, run)
            print(f"  loaded: inserted {run.counts['inserted']:,}, completed {run.counts['updated']:,}, "
                  f"indexed {run.counts['indexed']:,}", flush=True)
        classify(conn, run, dry)
        if not dry and (rows or run.counts["classified"]):
            refresh(conn)
    finally:
        conn.close()

    text = report(run, days, a.mode, started)
    print("\n" + text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write(text)
    out = HERE / "out"
    out.mkdir(exist_ok=True)
    (out / "sync_report.json").write_text(json.dumps(
        {"mode": a.mode, "window": [str(days[0]), str(days[-1])], "counts": dict(run.counts),
         "problems": run.problems, "anomalies": run.anomalies,
         "classification_stopped": run.classification_stopped, "spend_usd": round(run.spend, 4)},
        indent=1, ensure_ascii=False))

    if run.problems:
        return 1
    if run.classification_stopped:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
