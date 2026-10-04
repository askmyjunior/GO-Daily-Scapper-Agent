"""
Rebuild go_search_index.tsv / tsv_a from the current ap_government_orders.

Run this after anything that changes abstract, order_text or department. The
columns are plain tsvector, not generated, and there is no trigger — so the
index goes stale silently and a search keeps returning yesterday's text.

WHAT GOES IN, AND WHY IT IS WEIGHTED THAT WAY
---------------------------------------------
    A   abstract          what the order says it is about, in the drafter's
                          own words. The densest signal per character in the
                          corpus, and what a reader sees in the result list.
    B   order_text        the operative body. Long, so a term here is worth
                          less per occurrence than one in the abstract.
    C   department        canonical name, the raw string as printed on the
                          order, and the sub-department. "Revenue
                          (Assignment-I)" is how people cite it, so the raw
                          form has to be searchable and not just filterable.
    D   the citation      "G.O.Ms.No.571", built from the structured columns,
                          plus category and sub-category.

Weight D is the important addition. Until now the GO's own number lived only
in a structured column, so a search for `G.O.Ms.No.571` could only find orders
that *mentioned* 571 — never GO 571 itself, which is the one thing the person
typing it wanted. The citation string is synthesised in exactly the form
Postgres tokenises a typed query into ('g.o.ms.no' <-> '571'), so the two meet.

tsv_a stays abstract-only. go_search uses it to ask a narrower question — did
the exact phrase land in the abstract — and that has to stay separable from
the everything-vector.

Nothing here reads a PDF or writes to ap_government_orders. It is derived data
only, recomputed from what the table already holds, and is safe to re-run.

Usage:
    python3 rebuild_search_index.py            # report, change nothing
    python3 rebuild_search_index.py --apply
    python3 rebuild_search_index.py --apply --only-stale
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

ENV_FILE = Path.home() / ".askmyjunior" / "supabase.env"

# Small enough that one batch stays well inside the server's statement
# timeout. A whole-table UPDATE in one transaction was tried first and was
# cancelled at 24,000 rows, taking every earlier row down with it — hence the
# commit per batch below, which also makes the run resumable with --from-id.
BATCH = 1000
STATEMENT_TIMEOUT_MS = 600_000

# The one definition of the vector. Kept as a single string so that the full
# rebuild here and any incremental refresh elsewhere cannot drift apart.
TSV_EXPR = """
    setweight(to_tsvector('english', coalesce(g.abstract, '')), 'A')
 || setweight(to_tsvector('english', coalesce(g.order_text, '')), 'B')
 || setweight(to_tsvector('english',
        coalesce(d.name, '') || ' ' ||
        coalesce(g.department_raw, '') || ' ' ||
        coalesce(g.sub_department, '')), 'C')
 || setweight(to_tsvector('english',
        'G.O.' || coalesce(g.go_type_document, g.go_type, 'Ms') ||
        '.No.' || coalesce(g.go_number::text, '') || ' ' ||
        coalesce(g.go_category, '') || ' ' ||
        coalesce(g.go_sub_category, '') || ' ' ||
        coalesce(cat.label, '') || ' ' ||
        coalesce(tg.labels, '')), 'D')
"""
# WEIGHT D CARRIES LABELS, NOT KEYS.
#
# The taxonomy stores keys and the spec forbids storing labels (§0.6), but
# weight D exists so a reader can find an order by typing its category, and
# nobody types `service_personnel`. So the label is resolved from the reference
# tables at index time: the column keeps the key, the vector gets the words.
#
# The OLD go_category / go_sub_category stay in the expression alongside the
# new ones. Dropping them would break every search that works today -- "Service
# Matters" is what the live site has always indexed -- while the new
# classification is still unreviewed. Both cost a handful of lexemes at the
# lowest weight, and nothing is foreclosed.

TSV_A_EXPR = "setweight(to_tsvector('english', coalesce(g.abstract, '')), 'A')"


def dsn() -> str:
    # The environment first. The credential now lives in ~/.zshenv, and copying
    # it into a second file would mean two places to rotate and one to forget.
    env = os.environ.get("SUPABASE_DB_URL")
    if env:
        if ":6543" in env:
            raise SystemExit("refusing the transaction pooler (6543); use 5432")
        return env
    if not ENV_FILE.exists():
        raise SystemExit("SUPABASE_DB_URL is not set and "
                         f"{ENV_FILE} does not exist")
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith("SUPABASE_DB_URL="):
            url = line.split("=", 1)[1].strip().strip("\"'")
            if ":6543" in url:
                raise SystemExit("refusing the transaction pooler (6543); use 5432")
            return url
    raise SystemExit(f"SUPABASE_DB_URL not found in {ENV_FILE}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument(
        "--only-stale",
        action="store_true",
        help="skip rows whose vector already equals what we would write",
    )
    ap.add_argument(
        "--from-id",
        type=int,
        default=0,
        help="resume at this go_id (batches commit as they go, so the last "
             "printed id is a safe restart point)",
    )
    args = ap.parse_args()

    import psycopg

    with psycopg.connect(dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            # Raised BEFORE the diagnostics, not after. The default is 2
            # minutes and the count below outgrew it when the corpus went from
            # 73,953 rows to 521,878 — the script failed on a query whose only
            # job is to print a number, before reaching the work.
            cur.execute(f"set statement_timeout = {STATEMENT_TIMEOUT_MS}")

            cur.execute("select count(*), min(go_id), max(go_id) from go_search_index")
            n, lo, hi = cur.fetchone()
            print(f"go_search_index: {n} rows, go_id {lo}..{hi}")

            # A tell that the vector predates the metadata weights: no row can
            # carry a C or D lexeme under the old recipe.
            #
            # SAMPLED, because it stopped being cheap. It casts every tsvector
            # to text and regex-matches it, which at 521,878 rows is minutes of
            # work to print one diagnostic line. A sample answers the same
            # question — "is this corpus on the old recipe" — and the number is
            # labelled as a sample so nobody reads it as a total.
            cur.execute(
                "select count(*) from (select tsv from go_search_index limit 20000) t"
                " where t.tsv::text ~ ':[0-9]+[CD]' or t.tsv::text ~ ':[CD]'"
            )
            print(f"of a 20,000-row sample, carrying department/citation lexemes: "
                  f"{cur.fetchone()[0]}")

            if not args.apply:
                cur.execute(
                    f"""
                    select g.id, {TSV_EXPR} = s.tsv
                      from ap_government_orders g
                      join departments d on d.id = g.department_id
                      join go_search_index s on s.go_id = g.id
                     limit 500
                    """
                )
                same = sum(1 for _, eq in cur.fetchall() if eq)
                print(f"sample of 500: {same} already current, {500 - same} would change")
                print("\ndry run — pass --apply to write.")
                return 0

            # (already set above, before the diagnostics)

            stale = " and s.tsv is distinct from " + f"({TSV_EXPR})" if args.only_stale else ""
            t0 = time.time()
            done = 0
            lo = max(lo, args.from_id)
            for start in range(lo, hi + 1, BATCH):
                cur.execute(
                    f"""
                    update go_search_index s
                       set tsv   = {TSV_EXPR},
                           tsv_a = {TSV_A_EXPR},
                           -- go_search_index keeps its OWN copies of these and
                           -- the filters read them, not ap_government_orders.
                           -- Without this the year facet keeps offering 7072,
                           -- 7024, 5010 ... long after the dates were repaired,
                           -- which is what the live site was still showing.
                           go_date         = g.go_date,
                           go_year         = extract(year from g.go_date)::smallint,
                           date_year       = extract(year from g.go_date)::smallint,
                           go_category     = g.go_category,
                           go_sub_category = g.go_sub_category,
                           department      = d.name,
                           status          = g.status
                      from ap_government_orders g
                      join departments d on d.id = g.department_id
                      left join go_classification c on c.go_id = g.id
                      left join go_categories cat on cat.key = c.primary_category
                      left join lateral (
                          select string_agg(sc.label, ' ') as labels
                            from unnest(coalesce(c.tags, '{{}}'::text[])) t
                            join go_subcategories sc on sc.key = t
                      ) tg on true
                     where s.go_id = g.id
                       and g.id >= %s and g.id < %s
                       {stale}
                    """,
                    (start, start + BATCH),
                )
                done += cur.rowcount
                if (start - lo) // BATCH % 5 == 0:
                    pct = 100 * (start - lo + BATCH) / max(1, hi - lo + 1)
                    print(f"  {done} rows written, through go_id {start + BATCH} "
                          f"({pct:.0f}%, {time.time() - t0:.0f}s)", flush=True)
            print(f"\nrebuilt: {done} rows in {time.time() - t0:.0f}s")

        with conn.cursor() as cur:
            # Asked as a question about behaviour, not about the vector's
            # printed form. An earlier version of this check counted rows
            # matching ':[0-9]+D' in tsv::text and reported 0 for a rebuild
            # that had in fact worked perfectly: Postgres prints A, B and C
            # but leaves D — the default weight — off entirely, so 'foo':330
            # *is* the D lexeme. The regex could never have matched. What
            # actually matters is whether a reader who types a GO's citation
            # finds that GO, so that is what is measured.
            cur.execute("""
                select count(*) filter (where hit), count(*)
                  from (
                    select s.tsv @@ websearch_to_tsquery('english',
                             'G.O.' || coalesce(g.go_type_document, g.go_type, 'Ms')
                             || '.No.' || g.go_number) as hit
                      from ap_government_orders g
                      join go_search_index s on s.go_id = g.id
                     where g.go_number is not null
                     order by g.id desc limit 500
                  ) t
            """)
            hit, n = cur.fetchone()
            print(f"citation self-match: {hit}/{n} of the newest orders are "
                  f"found by their own G.O. number")

            cur.execute("""
                select count(*) from go_search_index s
                  join ap_government_orders g on g.id = s.go_id
                  join departments d on d.id = g.department_id
                 where s.tsv @@ plainto_tsquery('english', d.name)
            """)
            print(f"rows searchable by their own department name: {cur.fetchone()[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
