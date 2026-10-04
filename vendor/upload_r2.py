#!/usr/bin/env python3
"""The 73,954 source PDFs -> Cloudflare R2, with the key recorded per GO.

    python3 upload_r2.py check              # credentials, bucket, disk. Uploads nothing.
    python3 upload_r2.py upload             # does the work; safe to interrupt and re-run
    python3 upload_r2.py verify             # independent re-check against the bucket
    python3 upload_r2.py --year=2026 upload # just that year, for a pilot or a resume

WHY CONTENT-ADDRESSED KEYS

The key is pdf/<aa>/<bb>/<sha256>.pdf, derived from the bytes and from nothing
else. Three consequences, all of them load-bearing:

  * The upload is idempotent. Re-PUTting a key writes the same bytes it already
    holds, so a crash mid-run costs nothing and a re-run is not a duplicate.
  * It dedupes. §22 found a single Rt.797 PDF served under four different
    register numbers; those four rows now share one key and one stored object.
    So source_object_key is NOT unique, by design.
  * It is self-verifying. The name of the object is the checksum of the object,
    so nothing has to be trusted to notice corruption later.

A filename-derived key would have none of this, and would additionally carry
the register's numbering -- which §22 proved is sometimes wrong about which PDF
it points at -- into the storage layer.

WHAT IS STORED, AND WHAT IS NOT

The KEY goes in the database, never a URL. A URL bakes in the account id, the
bucket name, the custom domain and whether the bucket is public: four mutable
facts, none of them a property of the document. The reader composes or signs a
URL from the key.

THE DATABASE IS THE LEDGER

`upload` asks Postgres which rows still have no source_object_key, and that is
the whole work list. Results are written back in committed batches, so an
interrupted run resumes exactly where it stopped without consulting the bucket
at all. That makes resume a local, cheap, offline decision.

FAIL LOUDLY

A file that is missing from disk, or that R2 stores under a different checksum
than we computed, is collected and reported at the end with a non-zero exit. It
is never skipped quietly -- a 99.9% upload that prints "done" is the failure
mode this is written to prevent.
"""

import os
import sys
import time
import hashlib
import threading
import collections
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import psycopg

from load_supabase import dsn, session_setup

#: Same location and same rules as the Supabase credentials: outside the repo,
#: chmod 600, never printed, never committed. See the file's own header.
ENV_FILE = Path.home() / ".askmyjunior" / "r2.env"

#: Concurrency. These are ~197 KB objects, so the run is latency-bound: each
#: worker spends most of its time waiting for a round trip rather than pushing
#: bytes, and the win comes from having many requests in flight, not a bigger
#: pipe.
#:
#: MEASURED on an idle link, after a wrong guess in the other direction:
#:
#:     serial   1 KB PUT   median 1133 ms
#:     serial 200 KB PUT   median 1132 ms   <- size is free; ~1.1s is pure overhead
#:     burst 16 workers    1.06 MB/s
#:     burst 48 workers    1.09 MB/s        <- 3x the workers buys 3%
#:
#: So concurrency pays only up to the point where the uplink saturates, around
#: 1.07 MB/s here, and 16 workers already reaches it. 48 was tried and reverted.
#:
#: The mistake worth not repeating: raising 16 -> 48 was justified by the FILE
#: rate rising (11.4 -> 15.9/s), which looked like a win and was not. The two
#: measurements covered different slices of the corpus -- the resumed run hit
#: smaller files -- so files/s moved while bytes/s did not. Against a fixed
#: uplink, bytes/s is the only rate that means anything; files/s is a statement
#: about file sizes wearing the costume of a throughput number.
WORKERS = 16

#: Rows per committed database batch. Small enough that an interrupted run
#: repeats at most this many uploads, large enough that the commit overhead
#: disappears against the uploads themselves.
BATCH = 500


def creds():
    """Read the credentials file, or say precisely what is missing.

    Environment variables win, so a caller that already has them (CI, a shell
    with them exported) never needs the file to exist at all.
    """
    want = ["R2_ACCOUNT_ID", "R2_BUCKET",
            "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"]
    cfg = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k in want:
        cfg[k] = os.environ.get(k) or cfg.get(k, "")

    missing = [k for k in want if not cfg[k]]
    placeholder = [k for k in want if cfg[k] == "REPLACE_ME"]
    if missing or placeholder:
        sys.exit(f"{ENV_FILE} is not filled in yet.\n"
                 + (f"  missing:     {', '.join(missing)}\n" if missing else "")
                 + (f"  placeholder: {', '.join(placeholder)}\n"
                    if placeholder else "")
                 + "Open it in a text editor and paste the values there -- not "
                   "at a shell prompt, where ! and $ get mangled before they "
                   "reach the file.")
    return cfg


def client(cfg):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=f"https://{cfg['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=cfg["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=cfg["R2_SECRET_ACCESS_KEY"],
        # R2 has no regions, but SigV4 must sign SOME region, and both ends
        # have to agree on which. "auto" is the value Cloudflare documents.
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            # botocore >= 1.36 adds a CRC32 trailer to every PUT by default.
            # R2 is S3-compatible, not S3, and that trailer is the single most
            # common source of 400s against it. We verify integrity with the
            # ETag instead, below, which is stronger here anyway because the
            # object's NAME is its sha256.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            # Must be >= WORKERS or threads serialise on the pool instead of
            # on the network, silently capping throughput.
            max_pool_connections=WORKERS + 4,
            retries={"max_attempts": 5, "mode": "adaptive"},
        ),
    )


def sniff(head):
    """Content type from the file's first bytes, not from its extension.

    Every source_file in the corpus is named .pdf, but 2,525 of them failed to
    open as PDFs and a number are really Word documents that were renamed. If
    those are stored as application/pdf, every future reader -- a browser, a
    viewer, an OCR queue -- is told a lie by the storage layer and has to
    rediscover the truth. The bytes already know; ask them.
    """
    if head.startswith(b"%PDF"):
        return "application/pdf"
    if head.startswith(b"\xd0\xcf\x11\xe0"):          # OLE2 compound file
        return "application/msword"
    if head.startswith(b"PK\x03\x04"):                # zip: .docx and friends
        return ("application/vnd.openxmlformats-"
                "officedocument.wordprocessingml.document")
    if head.startswith(b"{\\rtf"):
        return "application/rtf"
    return "application/octet-stream"


def key_for(sha):
    # Two levels of two hex characters. Not required by R2, whose key space is
    # flat, but it keeps any later listing, mirroring or rsync to a filesystem
    # from putting 73,954 entries in one directory.
    return f"pdf/{sha[:2]}/{sha[2:4]}/{sha}.pdf"


def db(attempts=4):
    """A connection with TCP keepalives, retried on a failure to establish.

    Keepalives stop the socket being reaped by the pooler or by a NAT on the
    path while it sits idle -- the process would otherwise only find out at the
    next query, as `SSL connection has been closed unexpectedly`. This run is
    exactly the shape that provokes it: long stretches of pure network I/O with
    a database connection doing nothing in between.

    The retry is here because that drop was observed to be INTERMITTENT, not
    deterministic: the same preflight failed and then passed unchanged. An
    intermittent fault is the more dangerous kind, because it does not stop the
    run at second zero where it is cheap -- it waits until file 60,000. Every
    caller of this function is short and idempotent, so retrying is free.
    """
    for i in range(attempts):
        try:
            conn = psycopg.connect(dsn(), keepalives=1, keepalives_idle=30,
                                   keepalives_interval=10, keepalives_count=5,
                                   connect_timeout=15)
            with conn.cursor() as cur:
                session_setup(cur)
                # session_setup() disables statement_timeout, which is right
                # for the loader (one COPY of a 275 MB CSV) and wrong here.
                # Every statement this script runs is small, so an unbounded
                # one is a hang, not a long job. Put a deadline back.
                cur.execute("set statement_timeout = '120s'")
            return conn
        except psycopg.OperationalError:
            if i == attempts - 1:
                raise
            time.sleep(2 ** i)


def retry_db(fn, attempts=4, timeout=150):
    """Run a short database call, surviving both errors AND hangs.

    Only for operations safe to repeat whole -- reads, and the idempotent
    update in write_back(). Never wrap something that is not.

    THE HANG IS THE POINT. An earlier version retried on OperationalError and
    was not enough, because the failure that actually happened never raised:
    the laptop moved between networks, the TCP connection was left half-open,
    and the client blocked forever reading a socket whose reply had already
    been sent into a black hole. Postgres showed the session `idle in
    transaction / ClientRead` for 56 minutes while this process sat waiting for
    bytes that no longer existed. Nothing timed out, nothing raised, nothing
    retried -- it just stopped, quietly, looking exactly like slow progress.

    Hence a thread with a deadline rather than a bigger try/except. TCP
    keepalives are configured too, but they are a best-effort kernel setting
    (and the keepalive knobs differ across platforms), so they are the second
    line of defence, not the first.

    An abandoned call is LEAKED, deliberately: the thread is blocked in a
    syscall and cannot be killed, and conn.cancel() would not free it either
    -- the cancellation is delivered over the same dead socket. The thread is
    a daemon, so it dies with the process, and the connection is reaped by the
    pooler. Leaking a socket beats hanging a four-hour job.
    """
    last = None
    for i in range(attempts):
        box = {}

        def run():
            try:
                with db() as conn, conn.cursor() as cur:
                    box["r"] = fn(cur)
            except BaseException as e:                # noqa: BLE001
                box["e"] = e

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout)

        if "r" in box:
            return box["r"]
        if t.is_alive():
            last = TimeoutError(f"no response in {timeout}s; abandoning socket")
        else:
            last = box.get("e")
            # Only connection-level faults are worth retrying. A constraint
            # violation or a typo in the SQL will fail identically four times
            # and then be reported four times later than it should have been.
            if not isinstance(last, psycopg.OperationalError):
                raise last
        if i == attempts - 1:
            break
        print(f"  db call failed ({last}); retrying in {2 ** i}s")
        time.sleep(2 ** i)
    raise last


# Set by `--year YYYY` on the command line. None means the whole backlog.
#
# Added for the RT leg: 447,925 files is about 19 hours of saturated uplink,
# and committing to that before anything had been proved end to end would be
# the expensive way to discover a mistake. One year is ~25,000 files and ~25
# minutes, enough to see keys written, objects verified and a G.O. actually
# open in the browser.
#
# It is a filter on the work list and nothing else -- the run stays idempotent,
# resumable and content-addressed, so a year uploaded now is simply absent from
# the next run's backlog.
YEAR = None


def pending():
    """The work list, read over a connection that is then closed.

    Deliberately its own short-lived connection. The first version held ONE
    connection open across the whole upload and used it once every 500 files,
    which meant a socket sitting idle for minutes at a time for half an hour --
    the pooler dropped it, and the run died before uploading anything.

    The work list is small (73,954 ids and paths) and the upload does not need
    the database again until it has results, so there is no reason to hold a
    connection while the network does the actual work.
    """
    # BATCHED BY ID, not one query.
    #
    # The first version asked for the whole work list at once. That was fine
    # at 73,954 rows and does not survive 447,925: the RT leg timed out at the
    # 150 s deadline in retry_db, every time, so the upload could not START --
    # `check` died on the same call. Supabase's pooler is the constraint, and
    # the fix is the same one the corpus loader already uses: keyset pages of a
    # few thousand, each on its own short-lived connection.
    #
    # Keyed on `id > last` rather than OFFSET, so a page costs the same at row
    # 440,000 as at row 1 -- and so a retried page cannot skip or repeat rows
    # if another process keys a row in between.
    rows = []
    last = 0
    while True:
        def read(cur, _last=last):
            if YEAR is None:
                cur.execute("select id, source_file from ap_government_orders "
                            "where source_object_key is null and id > %s "
                            "order by id limit 5000", (_last,))
            else:
                cur.execute("select id, source_file from ap_government_orders "
                            "where source_object_key is null and id > %s "
                            "  and extract(year from go_date) = %s "
                            "order by id limit 5000", (_last, YEAR))
            return cur.fetchall()
        page = retry_db(read)
        if not page:
            break
        rows.extend(page)
        last = page[-1][0]
    return rows


class Deduper:
    """Ensures a given sha256 is PUT at most once, ever.

    Content-addressing makes byte-identical PDFs share one key, and R2 refuses
    concurrent writes to a single object: `ServiceUnavailable: Reduce your
    concurrent request rate for the same object`. With 2,368 duplicates in one
    leg of this corpus and 16 workers, that raced constantly and failed 423
    uploads -- a defect created by the dedup design, not by the network.

    Retrying those collisions would work and would still be wrong: the second
    PUT of an identical object is pure waste, of bandwidth we measured at
    ~1.1 MB/s. So the duplicate is not retried, it is not sent at all.

    `seen` is primed from the database, so duplicates are also skipped ACROSS
    runs -- a resumed run reuses keys uploaded hours earlier. The per-sha lock
    then serialises first-writers within a run, which is what makes "at most
    once" true under concurrency rather than merely likely.
    """

    def __init__(self, known):
        self.seen = dict(known)               # sha -> key, already in R2
        self.locks = collections.defaultdict(threading.Lock)
        self.guard = threading.Lock()
        self.skipped = 0

    def lock_for(self, sha):
        with self.guard:                      # defaultdict is not atomic
            return self.locks[sha]

    def known(self, sha):
        with self.guard:
            return self.seen.get(sha)

    def record(self, sha, key):
        with self.guard:
            self.seen[sha] = key

    def note_skip(self):
        with self.guard:
            self.skipped += 1


def one(s3, bucket, row, dedup):
    """Hash, upload and verify a single file. Returns a result tuple.

    Never raises for an expected condition -- a missing file and a failed
    upload both come back as ('miss'/'fail', ...) so the pool keeps running and
    the whole set of problems is reported at once instead of one per re-run.
    """
    go_id, path = row
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError as e:
        return ("miss", go_id, path, str(e))

    sha = hashlib.sha256(data).hexdigest()
    key = key_for(sha)

    # One writer per key. Held across the PUT so a second thread holding the
    # same sha waits and then finds it already done, rather than colliding
    # with it inside R2. Contention is limited to genuine duplicates.
    with dedup.lock_for(sha):
        if dedup.known(sha):
            # Already in the bucket, this run or an earlier one. The row still
            # gets the key -- it is the same object, which is the entire point
            # of addressing by content.
            dedup.note_skip()
            return ("ok", go_id, key, sha, len(data))
        res = _put(s3, bucket, key, data, sha, p.name)
        if res is not None:
            return (res[0], go_id, path, res[1])
        dedup.record(sha, key)
    return ("ok", go_id, key, sha, len(data))


def _put(s3, bucket, key, data, sha, name, attempts=4):
    """PUT one object. Returns None on success, or ('fail', message).

    Retries the throttling responses R2 returns under load -- a 503 here means
    "later", not "no". Everything else fails immediately: retrying a 403 just
    turns one clear error into four slow ones.
    """
    for i in range(attempts):
        try:
            r = s3.put_object(
                Bucket=bucket, Key=key, Body=data,
                ContentType=sniff(data[:8]),
                # Survives the round trip in object metadata, so the bucket
                # alone can be traced back to the corpus if the database is
                # ever rebuilt from scratch.
                Metadata={"sha256": sha, "source-filename": name[:1024]})
        except Exception as e:                        # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"
            transient = any(s in msg for s in
                            ("ServiceUnavailable", "SlowDown",
                             "InternalError", "RequestTimeout", "503"))
            if not transient or i == attempts - 1:
                return ("fail", msg)
            time.sleep(2 ** i)
            continue

        # R2 returns the MD5 of the stored object as its ETag for a
        # single-part PUT. Comparing it to the MD5 of what we sent proves the
        # bytes that landed are the bytes we read -- an end-to-end check,
        # not a claim of success.
        etag = (r.get("ETag") or "").strip('"')
        if etag and "-" not in etag and etag != hashlib.md5(data).hexdigest():
            return ("fail", f"etag mismatch: stored {etag}")
        return None


def write_back(rows, attempts=4):
    """Commit one batch of results. The commit IS the resume point.

    Opens its own connection, writes, closes. That costs a connect per 500
    files -- nothing against 500 uploads -- and in exchange there is never an
    idle connection for the pooler to reap.

    Retries on a dropped connection specifically, because the alternative is
    throwing away 500 genuinely-completed uploads over a transient socket. The
    update is idempotent (it sets the same four values from the same bytes), so
    a retry cannot double-apply. A failure that is NOT a connection problem is
    re-raised immediately -- retrying a constraint violation just hides it.
    """
    vals = [(key, sha, n, go_id) for (go_id, key, sha, n) in rows]

    def write(cur):
        cur.executemany(
            "update ap_government_orders "
            "set source_object_key = %s, source_sha256 = %s, "
            "    source_bytes = %s, source_uploaded_at = now() "
            "where id = %s", vals)
        cur.connection.commit()
    retry_db(write, attempts)


# ---------------------------------------------------------------------------
# stages
# ---------------------------------------------------------------------------

def check():
    cfg = creds()
    s3 = client(cfg)
    bucket = cfg["R2_BUCKET"]

    # A list, not a head_bucket: it proves the credentials can actually READ
    # this bucket's contents, which is the permission the upload needs. A
    # bucket that merely exists tells us nothing about our access to it.
    try:
        r = s3.list_objects_v2(Bucket=bucket, MaxKeys=1)
    except Exception as e:                            # noqa: BLE001
        sys.exit(f"cannot reach bucket {bucket!r}: {type(e).__name__}: {e}\n"
                 f"Check the bucket name and that the API token is scoped to "
                 f"it with Object Read & Write.")
    print(f"bucket {bucket!r} reachable, "
          f"currently holds {'objects' if r.get('KeyCount') else 'nothing'}")

    with db() as conn, conn.cursor() as cur:
        cur.execute("select count(*), count(source_object_key) "
                    "from ap_government_orders")
        total, done = cur.fetchone()
    todo = pending()

    print(f"corpus {total:,}  uploaded {done:,}  to do {len(todo):,}")

    # Disk is checked BEFORE anything is uploaded, because a wrong corpus path
    # is the one failure that should stop the run at second zero rather than at
    # minute forty. Sampled, not exhaustive: 73,954 stats are slow and the
    # question here is "is the corpus where we think it is", not "is every
    # single file present" -- which upload() answers for real, per file.
    sample = todo[::max(1, len(todo) // 500)][:500] if todo else []
    absent = [p for _, p in sample if not Path(p).exists()]
    if absent:
        sys.exit(f"{len(absent)} of {len(sample)} sampled source files are not "
                 f"on disk. First: {absent[0]}")
    size = sum(Path(p).stat().st_size for _, p in sample)
    if sample:
        est = size / len(sample) * len(todo)
        print(f"sampled {len(sample)} files, all present, "
              f"mean {size / len(sample) / 1024:.0f} KB "
              f"-> about {est / 1024**3:.1f} GiB to upload")
    print("ok. run: python3 -u upload_r2.py upload")


def upload():
    cfg = creds()
    s3 = client(cfg)
    bucket = cfg["R2_BUCKET"]

    todo = pending()
    if not todo:
        print("nothing to upload; every GO already has an object key")
        return
    # Total bytes up front, so progress can be reported against BYTES.
    #
    # An ETA derived from files/s is a lie whenever file sizes are uneven, and
    # here they are wildly uneven: an early slice of this corpus averages 64 KB
    # while the rest averages 226 KB, so a file-based ETA read 65 minutes when
    # the true answer was nearly four hours. Worse, it does not just mislead
    # the reader -- it misled ME into raising the worker count on the strength
    # of a files/s rise that was really a file-size fall.
    #
    # Against a fixed uplink, bytes/s is the only rate with meaning. 73,954
    # stat calls cost a couple of seconds against a four-hour transfer.
    total_bytes = 0
    for _, p in todo:
        try:
            total_bytes += os.path.getsize(p)
        except OSError:
            pass                      # counted as a failure later, by one()
    print(f"uploading {len(todo):,} files, {total_bytes / 1024**3:.2f} GiB, "
          f"with {WORKERS} workers")

    # Every sha already in the bucket, so a resumed run never re-uploads an
    # object a previous run stored. Costs one indexed query; saves both the
    # bandwidth and the same-key collisions that failed 423 uploads.
    def known_shas(cur):
        cur.execute("select source_sha256, source_object_key "
                    "from ap_government_orders "
                    "where source_sha256 is not null")
        return dict(cur.fetchall())
    dedup = Deduper(retry_db(known_shas))
    print(f"{len(dedup.seen):,} objects already in the bucket "
          f"will not be re-uploaded")

    stats = collections.Counter()
    problems = []
    seen_sha = {}
    lock = threading.Lock()
    batch, t0 = [], time.time()

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for res in pool.map(lambda r: one(s3, bucket, r, dedup), todo):
            if res[0] != "ok":
                problems.append(res)
                stats[res[0]] += 1
                continue
            _, go_id, key, sha, n = res
            with lock:
                # Counted, not prevented. A repeat sha is a genuine duplicate
                # PDF in the corpus, which we WANT collapsed onto one object.
                if sha in seen_sha:
                    stats["dup"] += 1
                seen_sha.setdefault(sha, key)
            stats["ok"] += 1
            stats["bytes"] += n
            batch.append((go_id, key, sha, n))

            if len(batch) >= BATCH:
                write_back(batch)
                batch = []
                el = time.time() - t0
                rate = stats["bytes"] / el                    # bytes/s, the
                left_b = max(0, total_bytes - stats["bytes"])  # only honest one
                print(f"  {stats['ok']:>6,}/{len(todo):,} files  "
                      f"{stats['bytes'] / 1024**3:5.2f}/{total_bytes / 1024**3:.2f} GiB  "
                      f"{rate / 1024**2:4.2f} MB/s  "
                      f"eta {left_b / rate / 60:4.0f} min")

    if batch:
        write_back(batch)

    el = time.time() - t0
    print(f"\nuploaded {stats['ok']:,} objects, {stats['bytes'] / 1024**3:.2f} GiB, "
          f"in {el / 60:.1f} min")
    print(f"distinct objects {len(seen_sha):,} "
          f"({stats['dup']:,} byte-identical duplicates collapsed, "
          f"{dedup.skipped:,} of them skipped without a PUT)")

    # Asked of the database, not of `stats`. The point of this number is to
    # catch the case where the uploader's own bookkeeping and the committed
    # rows disagree, so deriving it from the bookkeeping would defeat it.
    # Scoped the same way the work list was. The first version counted the
    # WHOLE corpus, which was right while the only runs were whole-corpus ones
    # and became a false alarm the moment --year existed: the 2026 pilot
    # uploaded all 9,621 of its files without a single failure and then
    # announced "no failures reported, yet 438,304 GOs still have no key.
    # That is a bug in this script" -- about 438,304 rows from other years that
    # the run was never asked to touch.
    #
    # A check that fires on a successful run teaches you to ignore it, which is
    # worse than not having it: the whole point is to catch the day the
    # uploader's bookkeeping and the committed rows disagree.
    def count_left(cur):
        if YEAR is None:
            cur.execute("select count(*) from ap_government_orders "
                        "where source_object_key is null")
        else:
            cur.execute("select count(*) from ap_government_orders "
                        "where source_object_key is null "
                        "  and extract(year from go_date) = %s", (YEAR,))
        return cur.fetchone()[0]
    left = retry_db(count_left)

    if problems:
        # Written to a file as well as printed: 2,525 of these documents are
        # known-broken, so a residue here is plausible enough that it needs to
        # be re-readable tomorrow rather than scrolled past today.
        out = Path(__file__).resolve().parent / "out" / "r2_upload_problems.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join("\t".join(map(str, p)) for p in problems),
                       encoding="utf-8")
        print(f"\n{len(problems):,} FAILED "
              f"({stats['miss']:,} missing from disk, {stats['fail']:,} upload "
              f"errors) -- listed in {out}")
        for p in problems[:5]:
            print(f"  {p[0]} {p[2]}: {p[3]}")
        sys.exit(f"{left:,} GOs still have no object key"
                 f"{'' if YEAR is None else f' in {YEAR}'}. Re-run to retry "
                 f"only those; everything uploaded is already committed.")

    if left:
        sys.exit(f"no failures reported, yet {left:,} GOs still have no key"
                 f"{'' if YEAR is None else f' in {YEAR}'}. "
                 f"That is a bug in this script, not a data problem.")
    print("every GO now has an object key")


def verify():
    """Re-check against the bucket itself, not against our own write-back.

    upload() records what it BELIEVES it stored. This asks R2. The two are
    independent, which is the only reason this stage is worth running.
    """
    cfg = creds()
    s3 = client(cfg)
    bucket = cfg["R2_BUCKET"]
    fails = []

    with db() as conn, conn.cursor() as cur:
        cur.execute("select count(*), count(source_object_key), "
                    "count(distinct source_object_key), "
                    "coalesce(sum(source_bytes), 0) "
                    "from ap_government_orders")
        total, keyed, distinct, nbytes = cur.fetchone()
        print(f"rows {total:,}  keyed {keyed:,}  distinct objects {distinct:,}  "
              f"{nbytes / 1024**3:.2f} GiB")
        if keyed != total:
            fails.append(f"{total - keyed:,} rows have no object key")

        # The key must equal the hash it claims, or content-addressing is a
        # story we tell rather than a property we have.
        cur.execute("select count(*) from ap_government_orders "
                    "where source_object_key is not null "
                    "and source_object_key <> "
                    "  'pdf/' || substr(source_sha256, 1, 2) || '/' || "
                    "  substr(source_sha256, 3, 2) || '/' || source_sha256 "
                    "  || '.pdf'")
        n = cur.fetchone()[0]
        if n:
            fails.append(f"{n:,} keys do not match their own sha256")

        # Sample the bucket. HEAD is a Class B operation (10M/month free), so
        # a wide sample is free; a full sweep is left for a deliberate audit.
        cur.execute("select source_object_key, source_sha256, source_bytes "
                    "from ap_government_orders "
                    "where source_object_key is not null "
                    "order by random() limit 300")
        rows = cur.fetchall()

    for key, sha, n in rows:
        try:
            h = s3.head_object(Bucket=bucket, Key=key)
        except Exception as e:                        # noqa: BLE001
            fails.append(f"{key}: {type(e).__name__}")
            continue
        if h["ContentLength"] != n:
            fails.append(f"{key}: {h['ContentLength']} bytes stored, {n} recorded")
        if (h.get("Metadata") or {}).get("sha256") != sha:
            fails.append(f"{key}: stored sha256 metadata does not match")
    print(f"sampled {len(rows)} objects in the bucket")

    if fails:
        for f in fails[:20]:
            print(f"  FAIL {f}")
        sys.exit(f"{len(fails)} problems")
    print("PASS")


if __name__ == "__main__":
    argv = [a for a in sys.argv[1:]]
    for a in list(argv):
        if a.startswith("--year="):
            YEAR = int(a.split("=", 1)[1])
            argv.remove(a)
    stage = argv[0] if argv else "check"
    fn = {"check": check, "upload": upload, "verify": verify}.get(stage)
    if not fn:
        sys.exit(__doc__)
    if YEAR is not None:
        print(f"scoped to {YEAR}")
    fn()
