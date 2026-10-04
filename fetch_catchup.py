#!/usr/bin/env python3
"""Download every order the portal has that our database does not.

    python3 fetch_catchup.py --survey     # what is missing, no downloads
    python3 fetch_catchup.py              # fetch, resumable

── THE FILENAME IS THE KEY ───────────────────────────────────────────────────

`source_file` is the only unique key on ap_government_orders, and it is a path
whose BASENAME carries the convention the whole corpus follows:

    HOM01 - HOME-RT-822-30_06_2026.pdf
    <org code> - <ORG NAME>-<TYPE>-<NUMBER>-<DD_MM_YYYY>.pdf

The portal's listing gives the organisation as "HOM01 - HOME" verbatim, so a
new order's filename is built from the same parts the existing 521,878 were,
not from a new scheme. That is what lets a new row sit beside an old one
without anything downstream having to know which pass produced it.

── WHAT IS DELIBERATELY NOT HERE ─────────────────────────────────────────────

No parsing, no database writes. This stage only puts bytes on disk under the
right name, so it can be re-run, interrupted, and checked by eye before
anything touches the corpus. The loader is a separate step for the same reason
load_rt.py separates `prepare` from `load`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import goir_portal as G
from pipeline import filename_for  # one naming rule, shared with sync.py

OUT = pathlib.Path(__file__).resolve().parent / "out"
PDFS = OUT / "pdfs"
STATE = OUT / "fetched.jsonl"

#: Where our corpus ends, per type, verified against the portal on 2026-10-04.
#: RT starts on the 30th, NOT the 1st: the portal had 79 orders that day and we
#: had 76. A catch-up that began at "the next day" would have silently dropped
#: three, which is exactly the class of loss this whole stage exists to avoid.
START = {"MS": dt.date(2026, 6, 20), "RT": dt.date(2026, 6, 30)}


def already() -> set[str]:
    done = set()
    if STATE.exists():
        for line in STATE.open():
            try:
                done.add(json.loads(line)["filename"])
            except Exception:
                pass
    return done


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--survey", action="store_true", help="list what is missing, fetch nothing")
    ap.add_argument("--until", default=str(dt.date.today()))
    ap.add_argument("--delay", type=float, default=0.4)
    a = ap.parse_args()
    until = dt.date.fromisoformat(a.until)

    PDFS.mkdir(parents=True, exist_ok=True)
    done = already()
    print(f"{len(done):,} already fetched\n")

    listed = fetched = skipped = failed = nofile = 0
    t0 = time.time()
    with STATE.open("a") as state:
        for go_type, start in START.items():
            day = start
            while day <= until:
                try:
                    rows = G.listing_complete(day, go_type)
                except G.ShortRead as e:
                    # A day we cannot read in full is a day we do not record.
                    print(f"  !! {e}")
                    failed += 1
                    day += dt.timedelta(days=1)
                    continue
                except Exception as e:
                    print(f"  !! {go_type} {day}: {type(e).__name__}: {e}")
                    failed += 1
                    day += dt.timedelta(days=1)
                    continue
                listed += len(rows)
                for r in rows:
                    fn = filename_for(r, go_type)
                    if fn is None:
                        print(f"  ?? {go_type} {day}: unparsable row {r['go_number_raw']!r}")
                        failed += 1
                        continue
                    if fn in done:
                        skipped += 1
                        continue
                    if not r["gid_english"]:
                        # Listed with no English file -- Finance's Budget
                        # Release Orders do this. Recorded, not counted as a
                        # failure, so the loader can still create the row from
                        # the listing's own abstract.
                        state.write(json.dumps({**r, "go_type": go_type,
                                                "filename": fn, "pdf": None,
                                                "note": "no english file listed"}) + "\n")
                        done.add(fn)
                        continue
                    if a.survey:
                        fetched += 1
                        continue
                    try:
                        data = G.pdf(r["gid_english"], "E")
                    except FileNotFoundError:
                        # The portal LISTS a gid and serves nothing for it.
                        # Finance's Budget Release Orders are the bulk of
                        # these. It is not an error -- the order exists and the
                        # listing gives us its abstract -- so it is recorded
                        # like any other, with no file, and the loader builds
                        # the row from the abstract alone. Counting these as
                        # failures would make a healthy run look broken and
                        # train us to ignore the count that matters.
                        nofile += 1
                        state.write(json.dumps({**r, "go_type": go_type,
                                                "filename": fn, "pdf": None,
                                                "note": "0-byte 200: no document on the portal"}) + "\n")
                        state.flush()
                        done.add(fn)
                        continue
                    except Exception as e:
                        print(f"  !! {fn}: {type(e).__name__}: {e}")
                        failed += 1
                        continue
                    (PDFS / fn).write_bytes(data)
                    # A Word document served as .pdf is still the order. Recorded
                    # with what it actually is, so the loader can put it in
                    # unreadable_not_pdf rather than trying to parse it as a PDF.
                    state.write(json.dumps({**r, "go_type": go_type, "filename": fn,
                                            "pdf": fn, "bytes": len(data),
                                            "is_pdf": G.is_pdf(data)}) + "\n")
                    state.flush()
                    done.add(fn)
                    fetched += 1
                    if fetched % 100 == 0:
                        rate = fetched / max(time.time() - t0, 1)
                        print(f"  {fetched:,} fetched  {rate:.1f}/s  (at {go_type} {day})",
                              flush=True)
                    time.sleep(a.delay)
                day += dt.timedelta(days=1)
            print(f"{go_type}: through {until}")

    print(f"\nlisted {listed:,}  fetched {fetched:,}  already had {skipped:,}  "
          f"no document on the portal {nofile:,}  failed {failed}  "
          f"in {(time.time()-t0)/60:.0f} min")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
