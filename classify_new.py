#!/usr/bin/env python3
"""Classify newly loaded orders into the LIVE taxonomy, from the abstract alone.

    python3 classify_new.py estimate   # targets and projected cost. No paid calls.
    python3 classify_new.py run        # PAID: Gemini. Resumable, cached to JSONL.
    python3 classify_new.py load       # JSONL -> go_classification_ai, insert-only

The user's ruling (2026-10-04): new orders are classified by Gemini on the
abstract only, to keep the cost down. The body is sent only for an order that
has no abstract at all, exactly as `classify_gemini.py place --no-abstract` did.

── WHY NOT `classify_gemini.py place` ─────────────────────────────────────────

place numbers the sub-categories of out/gemini_cls/full.taxonomy.json — the
194-key consolidation from BEFORE the merges, the disciplinary split and the
budget-release split the user approved. Those keys are not what the site
filters on. The live taxonomy is go_subcategories (version 2.0, 119 keys), so
the list is read from there, and an answer can only ever be a key the site
already shows.

The one key under category 15 (`needs_review`, Others / Unclassified) is left
out of the list: the user does not want an "others" category, and an enum
cannot pick what it does not contain. That is also why the answer is an ENUM
of numbers here, where place sent free text and dropped what failed to map.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from pipeline import cg  # noqa: E402  (vendored classify_gemini: post, money, conn, MODEL)

LOAD = HERE / "out" / "load"
OUT_JSONL = HERE / "out" / "classify_new.jsonl"
BATCH = 25
WORKERS = int(os.environ.get("GEMINI_WORKERS", 8))
BODY_CHARS = 1500

# Measured, not list price: the abstract-only split run placed 40,500 orders
# for $6.53 (out_gemini_split.log) — $0.000161 an order, against a 194-key
# list. The live list is 118 keys, so this errs high.
USD_PER_ORDER = 6.53 / 40_500

# The register (or the document) saying there is no order to describe. Spelt
# however a clerk typed it: "cofidential", "Orders not issued.", "...".
NO_SUBJECT = re.compile(r"^\W*((strictly\s+)?con?fidential|(orders?\s+)?not\s+issued)?\W*$", re.I)

INSTR = ("You classify Andhra Pradesh Government Orders for a legal-research tool "
         "used by lawyers.\n\nFor each order, choose the ONE sub-category it "
         "belongs to from the numbered list, by what the order is ABOUT.\n\n"
         "Every order is about something. The list is complete and has no "
         "'other' or 'miscellaneous' entry -- choose the closest real subject.\n\n"
         "- Money sanctioned FOR something is filed under the something. Use a "
         "finance sub-category only when the money itself is the subject.\n"
         "- A department's name in the subject line is not the subject.\n"
         "- An action word (amendment, cancellation, revocation) names the subject "
         "only when a G.O., notification, Act or Rules is what is being acted on.\n\n"
         "Return id, sub (the NUMBER), conf (0.0-1.0).\n\nSUB-CATEGORIES:\n")


def new_ids() -> list[int]:
    """Every order the catch-up loaded: the 6,619 with documents and the
    file-less listings (held_orders.csv), whose portal subject is still a
    subject — a Budget Release Order with no file is still a budget release."""
    sys.path.insert(0, str(HERE))
    import load_catchup as lc
    sfs = lc.csv_column(LOAD / "ap_government_orders.csv", lc.GO_COLUMNS, "source_file")
    if (LOAD / "held_orders.csv").exists():
        sfs += lc.csv_column(LOAD / "held_orders.csv", lc.GO_COLUMNS, "source_file")

    def q():
        with cg.conn() as c, c.cursor() as cur:
            cur.execute("set statement_timeout = '5min'")
            cur.execute("select id from public.ap_government_orders where source_file = any(%s)", (sfs,))
            return sorted(r[0] for r in cur.fetchall())
    ids = cg.retry_db(q, what="new ids")
    assert len(ids) == len(sfs), f"{len(ids):,} of {len(sfs):,} orders are in the database — load first"
    return ids


def taxonomy() -> tuple[str, dict[str, tuple[str, str]], list[str]]:
    """-> (prompt block, number -> (sub key, category key), enum of numbers)."""
    def q():
        with cg.conn() as c, c.cursor() as cur:
            cur.execute("""select s.key, s.label, coalesce(s.definition, ''), c.key, c.number, c.label
                             from public.go_subcategories s
                             join public.go_categories c on c.key = s.category_key
                            where s.taxonomy_version = '2.0' and c.key <> 'others_unclassified'
                            order by c.number, s.sort_order, s.key""")
            return cur.fetchall()
    rows = cg.retry_db(q, what="taxonomy")
    num: dict[str, tuple[str, str]] = {}
    lines = []
    for i, (key, label, defn, ckey, cno, clabel) in enumerate(rows, 1):
        num[str(i)] = (key, ckey)
        lines.append(f"{i} | {label} | category {cno} {clabel} | {' '.join(defn.split())[:110]}")
    return "\n".join(lines), num, list(num)


def targets(ids: list[int]) -> list[tuple[int, str]]:
    """(id, text) for orders not yet classified. Abstract alone where there is one."""
    def q():
        with cg.conn() as c, c.cursor() as cur:
            cur.execute("set statement_timeout = '5min'")
            cur.execute("""select g.id, g.abstract,
                                  case when coalesce(g.abstract, '') = '' then left(g.order_text, %s) end
                             from public.ap_government_orders g
                            where g.id = any(%s)
                              and not exists (select 1 from public.go_classification_ai a
                                               where a.go_id = g.id)
                            order by g.id""", (BODY_CHARS, ids))
            return cur.fetchall()
    out = []
    for gid, abstract, body in cg.retry_db(q, what="targets"):
        # "Not Issued" and "Confidential" are the register saying there is no
        # order to describe. Any sub-category picked for them would be invented,
        # and the closed list has no honest answer to offer — so they are not
        # sent, and stay unclassified.
        if NO_SUBJECT.match(abstract or ""):
            continue
        if (abstract or "").strip():
            out.append((gid, "abstract: " + " ".join(abstract.split())))
        elif (body or "").strip():
            out.append((gid, "order: " + " ".join(body.split())))
    return out


def done_ids() -> set[int]:
    got = set()
    if OUT_JSONL.exists():
        for line in OUT_JSONL.open():
            try:
                got.add(json.loads(line)["id"])
            except (ValueError, KeyError):
                pass
    return got


def classify(chunk, system, enum, num):
    docs = [f"id: {gid}\n{text}" for gid, text in chunk]
    r = cg.post(f"{cg.ENDPOINT}/{cg.MODEL}:generateContent", {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": "\n\n---\n\n".join(docs)}]}],
        "generationConfig": {
            "temperature": 0, "responseMimeType": "application/json",
            "responseSchema": {"type": "ARRAY", "items": {
                "type": "OBJECT",
                "properties": {"id": {"type": "STRING"},
                               "sub": {"type": "STRING", "enum": enum},
                               "conf": {"type": "NUMBER"}},
                "required": ["id", "sub", "conf"],
                "propertyOrdering": ["id", "sub", "conf"]}},
            "maxOutputTokens": 8192},
    })
    usage = r.get("usageMetadata", {})
    model = r.get("modelVersion") or cg.MODEL
    try:
        answers = json.loads(r["candidates"][0]["content"]["parts"][0]["text"])
    except (KeyError, IndexError, ValueError):
        return [], usage
    want = {gid for gid, _ in chunk}
    out = []
    for a in answers if isinstance(answers, list) else []:
        try:
            gid = int(a["id"])
        except (KeyError, ValueError, TypeError):
            continue
        if gid in want and str(a.get("sub")) in num:
            key, ckey = num[str(a["sub"])]
            out.append({"id": gid, "sub": key, "cat": ckey,
                        "conf": float(a.get("conf") or 0), "model": model})
            want.discard(gid)
    return out, usage


def cmd_estimate() -> None:
    ids = new_ids()
    todo = [t for t in targets(ids) if t[0] not in done_ids()]
    block, _, enum = taxonomy()
    calls = -(-len(todo) // BATCH)
    usd = len(todo) * USD_PER_ORDER
    print(f"{len(ids):,} new orders; {len(todo):,} to classify "
          f"({sum(t[1].startswith('order:') for t in todo)} from the body, no abstract)")
    print(f"{len(enum)} sub-categories in the list ({len(block):,} characters)")
    print(f"{calls:,} calls of {BATCH}; measured ${USD_PER_ORDER:.6f}/order -> "
          f"${usd:.2f} (~Rs {usd * 88:,.0f} at Rs 88/$)")


def cmd_run() -> None:
    ids = new_ids()
    block, num, enum = taxonomy()
    have = done_ids()
    todo = [t for t in targets(ids) if t[0] not in have]
    print(f"{len(todo):,} to classify, {len(enum)} sub-categories, {WORKERS} workers")
    if not todo:
        return
    system = INSTR + block
    chunks = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
    n, spend, t0 = 0, 0.0, time.time()
    with OUT_JSONL.open("a") as fh, ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for i, (got, usage) in enumerate(pool.map(lambda ch: classify(ch, system, enum, num), chunks), 1):
            spend += cg.money(usage)
            for g in got:
                fh.write(json.dumps(g) + "\n")
            n += len(got)
            fh.flush()
            if i % 20 == 0 or i == len(chunks):
                print(f"  {n:,}/{len(todo):,}  ${spend:.2f}  {time.time() - t0:.0f}s", flush=True)
    print(f"classified {n:,} of {len(todo):,} for ${spend:.2f}"
          + ("" if n == len(todo) else f"; {len(todo) - n:,} got no answer — re-run to retry them"))


def cmd_load() -> None:
    from psycopg2.extras import execute_values
    ids = set(new_ids())
    rows = {}
    for line in OUT_JSONL.open():
        r = json.loads(line)
        if r["id"] in ids:
            rows[r["id"]] = r          # last answer wins on a retried id
    # Re-checked against the abstract the database holds NOW: the first run
    # predates the no-subject rule, and an abstract may have been corrected
    # since its answer was cached.
    def abstracts():
        with cg.conn() as c, c.cursor() as cur:
            cur.execute("select id, abstract from public.ap_government_orders where id = any(%s)",
                        (sorted(rows),))
            return dict(cur.fetchall())
    current = cg.retry_db(abstracts, what="abstracts")
    dropped = [i for i in rows if NO_SUBJECT.match(current.get(i) or "")]
    for i in dropped:
        del rows[i]
    print(f"{len(rows):,} classifications for {len(ids):,} new orders "
          f"({len(dropped)} withheld: no subject to classify)")

    def write():
        with cg.conn() as c, c.cursor() as cur:
            execute_values(cur, """
                insert into public.go_classification_ai
                    (go_id, primary_category, sub_category, confidence, model, go_date)
                select v.go_id, v.cat, v.sub, v.conf, v.model, g.go_date
                  from (values %s) v(go_id, cat, sub, conf, model)
                  join public.ap_government_orders g on g.id = v.go_id
                on conflict (go_id) do nothing""",
                [(r["id"], r["cat"], r["sub"], r["conf"], r["model"]) for r in rows.values()],
                template="(%s::bigint, %s, %s, %s::real, %s)", page_size=1000)
            c.commit()
            cur.execute("select count(*) from public.go_classification_ai where go_id = any(%s)",
                        (sorted(ids),))
            have = cur.fetchone()[0]
            cur.execute("select public.go_taxonomy_counts_refresh()")
            cur.execute("delete from public.go_facets_cache")
            c.commit()
            return have
    have = cg.retry_db(write, what="load")
    print(f"go_classification_ai now holds {have:,} of the {len(ids):,} new orders; "
          f"taxonomy counts refreshed, facet cache cleared")


if __name__ == "__main__":
    {"estimate": cmd_estimate, "run": cmd_run, "load": cmd_load}[sys.argv[1]]()
