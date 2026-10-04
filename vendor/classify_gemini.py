#!/usr/bin/env python3
"""Classify G.O.s into the 1.1 taxonomy by reading them, not by matching cues.

WHY THIS EXISTS
───────────────
`classify_taxonomy.py` matches regex cues against the abstract. A blind audit of
150 orders put it at 64% agreement (96/150), with six systematic error classes
that are all the same mistake underneath: a cue cannot tell what a sentence is
ABOUT. "Sanction of Rs. 2.50 lakhs for the Endowments Commissioner's vehicle"
is a vehicle purchase, not a financial sanction; "classification of institutions
under section 6" is a notification, not a schools matter. A reader gets these
right instantly. So: let a model read it.

WHAT IT DOES NOT DO
───────────────────
It does not touch `go_classification`. The rules' output stays exactly where it
is, serving the live site, and the model's answers land in
`go_classification_ai`. Nothing swaps over until the pilot numbers justify it
and the user says so.

CREDENTIAL
──────────
Read from ~/.askmyjunior/gemini.env (chmod 600, outside the repo -- the same
shape as r2.env), or $GEMINI_API_KEY. Never passed on the command line, never
printed, never logged. A urllib error carrying a key in its URL is caught and
re-raised without it.

USAGE
─────
  python3 classify_gemini.py models            # what the key can actually reach
  python3 classify_gemini.py pilot             # the 150 audit orders, both input
                                               #   shapes, accuracy + real cost
  python3 classify_gemini.py run --limit 1000  # classify, cache to JSONL
  python3 classify_gemini.py load              # JSONL -> go_classification_ai
"""
import json, os, pathlib, random, re, sys, time, urllib.error, urllib.request
from collections import Counter

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "out" / "gemini_cls"; OUT.mkdir(parents=True, exist_ok=True)
TAXONOMY = next(
    (p for p in [
        HERE.parent.parent / "GO Categorisation/FF-GO Categorisation-withGOIR/go_taxonomy.json",
        HERE / "go_taxonomy.json",
    ] if p.exists()), None)

MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest")
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"
BATCH = 25
BODY_CHARS = 0          # 0 = abstract only; >0 prepends that many chars of body

# USD per million tokens, EMPIRICAL -- not list price.
#
# These were originally 0.10/0.40, the published Flash-Lite rates, and that was
# wrong by 4.4x. `gemini-flash-lite-latest` bills at Flash rates: 450,735 orders
# consumed about 97.3M input and 17.5M output tokens, which is $16.72 at
# Flash-Lite and $72.89 at Flash -- and the account burned through roughly
# Rs 6,000 (~$68) doing it. The spend is the measurement; the list price was an
# assumption reported back as though it were one.
#
# Check this against the real bill before trusting any projection built on it.
PRICE_IN, PRICE_OUT = 0.30, 2.50


# ── credential ───────────────────────────────────────────────────────────────

def env_file(name: str, *keys: str) -> str:
    """Read one value out of ~/.askmyjunior/<name>.env, chmod 600, never logged."""
    v = next((os.environ[k] for k in keys if os.environ.get(k)), None)
    if v:
        return v
    f = pathlib.Path.home() / ".askmyjunior" / f"{name}.env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, _, val = line.partition("=")
            if k.strip().lstrip("export ").strip() in keys:
                return val.strip().strip('"').strip("'")
    return ""


def api_key() -> str:
    k = env_file("gemini", "GEMINI_API_KEY", "GOOGLE_API_KEY")
    if not k:
        sys.exit(
            "No Gemini key. Create ~/.askmyjunior/gemini.env containing one line\n"
            "    GEMINI_API_KEY=...\n"
            "then: chmod 600 ~/.askmyjunior/gemini.env\n"
            "(Same pattern as r2.env -- the key stays out of the repo and out of chat.)")
    return k


def _scrub(s: str) -> str:
    """Never let a key reach a log, a traceback or the terminal."""
    return re.sub(r"key=[\w\-]+", "key=REDACTED", str(s))


# ── taxonomy, as the prompt sees it ──────────────────────────────────────────

def load_taxonomy() -> tuple[dict, str, dict]:
    """-> (key -> (cat_key, cat_no, cat_label, sub_label), prompt block, number -> key).

    The third return is what the model actually answers with. The response
    schema's enum is capped at roughly 1,750 CHARACTERS of values -- not a count
    of them: 102 short values pass, 94 real keys (1,745 chars) pass, 95 (1,757)
    fail with a bare `400 INVALID_ARGUMENT` naming nothing. So the model emits
    the taxonomy number, `3.7`, and we map it back to `preventive_detention`
    here. The enum guarantee survives intact at a tenth of the characters, and
    the replies are shorter to pay for too.
    """
    if not TAXONOMY:
        sys.exit("go_taxonomy.json not found")
    d = json.loads(TAXONOMY.read_text())
    cats = d["categories"]
    sub_to_cat, num_to_key, lines = {}, {}, []
    for c in cats:
        # Category 15 is Others / Unclassified. It is deliberately absent from
        # BOTH the prompt and the enum: an order that does not fit must propose
        # a new sub-category, not fall into a bucket. Instructing the model not
        # to use it was not enough -- 19 of 1,200 went there anyway. Removing
        # it from the enum makes it unreachable rather than discouraged.
        if str(c["number"]) == "15":
            continue
        lines.append(f'\n# {c["number"]} {c["label"]}')
        for i, s in enumerate(c["subcategories"], 1):
            num = f'{c["number"]}.{i}'
            sub_to_cat[s["key"]] = (c["key"], c["number"], c["label"], s["label"])
            num_to_key[num] = s["key"]
            defn = re.sub(r"\s+", " ", s.get("definition", "")).strip().rstrip(".")
            lines.append(f'{num} {s["key"]} — {defn}')
    # Category 15's keys still need to resolve to a category if one ever
    # appears in old data, so keep them in sub_to_cat but out of num_to_key.
    for c in cats:
        if str(c["number"]) == "15":
            for s in c["subcategories"]:
                sub_to_cat[s["key"]] = (c["key"], c["number"], c["label"], s["label"])
    return sub_to_cat, "\n".join(lines), num_to_key


INSTRUCTION = """You are reading Andhra Pradesh Government Orders (G.O.s) for a legal-research tool used by lawyers.

For each order, name the sub-category it belongs to: a short snake_case key for WHAT THE ORDER IS ABOUT -- the thing it actually does.

A reference taxonomy follows. It is foundational, but it is NOT exhaustive and NOT a set of boxes every order must be forced into:

- If one of its sub-categories genuinely fits, answer with that key spelled EXACTLY as it appears there.
- If none fits, coin a new snake_case key naming what the order really is about, at the same level of generality as the existing ones -- `government_quarters_allotment`, `vehicle_purchase`, `honorarium_fixation`, `temple_administration`. Never a name for one order's specifics.
- Coin the SAME key every time the same kind of order comes up. These keys are pooled across the whole corpus afterwards, so consistency is what makes them usable.
- NEVER answer `others_unclassified`, `needs_review`, `miscellaneous` or anything of that shape. Every order is about something; if you are reaching for those, name the subject instead.

Decide by what the order is ABOUT, not by any single word it happens to contain. These distinctions are the ones most often got wrong, so apply them deliberately:

- An order that sanctions money FOR something is filed under the something. "Sanction of Rs. 2.50 lakhs for purchase of a vehicle" is a vehicle purchase. Use a finance sub-category only when the money itself is the subject: a budget release, a re-appropriation, a loan, a guarantee.
- A department's name in the subject line is not the subject. "School Education Department -- Sri X, Deputy Director -- transfer" is a transfer, not a schools matter.
- An action word (amendment, cancellation, revocation, supersession) names the subject ONLY when the thing amended, cancelled, revoked or superseded is itself a G.O., a notification, an Act or a set of Rules. Revoking a preventive-detention order, cancelling a licence, cancelling a tender, cancelling an allotment: name each by its own subject instead.
- Words have senses. "Award" in an industrial dispute is a labour award; in a contract it is a tender award; in a scheme it is a prize. "Act" is legislation only when the order concerns the statute itself.
- Prefer the specific over the generic. Reach for permission/NOC only when permission really is the operative act.

Return one entry per input order, in the same order, with:
  id   - the id exactly as given
  sub  - the snake_case sub-category key, existing or newly coined
  conf - your confidence from 0.0 to 1.0

REFERENCE TAXONOMY:
"""

def schema_for(_numbers=None, open_taxonomy=True) -> dict:
    """`sub` is free text now, deliberately.

    An enum could only ever offer what the taxonomy already has, which is the
    constraint this design removes: the corpus decides the sub-categories and
    the hierarchy is settled afterwards, once every name is visible at once.
    Variant spellings are expected here and are resolved by consolidation --
    they are the raw material, not a defect.
    """
    return {
        "type": "ARRAY",
        "items": {"type": "OBJECT", "properties": {
            "id": {"type": "STRING"},
            "sub": {"type": "STRING"},
            "conf": {"type": "NUMBER"}},
            "required": ["id", "sub", "conf"],
            "propertyOrdering": ["id", "sub", "conf"]},
    }
    order = ["id", "sub", "conf"]
    if open_taxonomy:
        props["new_sub"] = {"type": "STRING"}
        props["new_cat"] = {"type": "STRING",
                            "enum": [str(i) for i in range(0, 15)]}
        props["new_cat_label"] = {"type": "STRING"}
        order = ["id", "sub", "new_sub", "new_cat", "new_cat_label", "conf"]
    return {
        "type": "ARRAY",
        "items": {"type": "OBJECT", "properties": props,
                  "required": ["id", "sub", "conf"], "propertyOrdering": order},
    }


# ── the call ─────────────────────────────────────────────────────────────────

def post(url: str, body: dict, tries: int = 6) -> dict:
    data = json.dumps(body).encode()
    for attempt in range(tries):
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json",
                     "x-goog-api-key": api_key()})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            code, detail = e.code, e.read()[:400].decode("utf8", "replace")
            # 429 rate limit, 500/503 transient. Everything else is our bug.
            if code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                wait = min(60, 2 ** attempt * 3) + random.random() * 2
                print(f"    {code}, retry in {wait:.0f}s", flush=True)
                time.sleep(wait); continue
            if code == 402:
                # Out of credit, not a fault. Retrying would never clear it,
                # and everything classified so far is already in the JSONL, so
                # say plainly what happened and where to resume from.
                raise SystemExit(
                    "\nSTOPPED: the Gemini account is out of prepaid credit (HTTP 402).\n"
                    "Work already done is cached -- classification in its JSONL, and\n"
                    "consolidation in its .partial.json -- so re-running this same\n"
                    "command resumes rather than repeating it.\n"
                    "Top up at https://ai.studio/projects first.")
            raise SystemExit(f"HTTP {code}: {_scrub(detail)}")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < tries - 1:
                time.sleep(min(60, 2 ** attempt * 3)); continue
            raise SystemExit(f"network: {_scrub(e)}")
    raise SystemExit("unreachable")


def classify(rows: list[tuple], tax_block: str, num_to_key: dict) -> tuple[dict, dict]:
    """rows -> {id: (sub_key, conf)}, plus the API's own token usage.

    The model answers with taxonomy NUMBERS; they are translated back to keys
    here, so every caller still sees `preventive_detention` rather than `3.7`.
    """
    docs = []
    for gid, abstract, body in rows:
        t = f"id: {gid}\nabstract: {(abstract or '').strip()}"
        if BODY_CHARS and body:
            t += f"\norder: {re.sub(chr(10)+'+', ' ', body.strip())[:BODY_CHARS]}"
        docs.append(t)
    r = post(f"{ENDPOINT}/{MODEL}:generateContent", {
        "systemInstruction": {"parts": [{"text": INSTRUCTION + tax_block}]},
        "contents": [{"role": "user",
                      "parts": [{"text": "\n\n---\n\n".join(docs)}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json",
                             "responseSchema": schema_for(num_to_key),
                             "maxOutputTokens": 8192},
    })
    usage = r.get("usageMetadata", {})
    try:
        text = r["candidates"][0]["content"]["parts"][0]["text"]
        answers = json.loads(text)
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        fin = (r.get("candidates") or [{}])[0].get("finishReason")
        print(f"    unusable reply ({e}; finishReason={fin})")
        return {}, usage
    out, refused, unparsable = {}, Counter(), 0
    known = set(num_to_key.values())
    for a in answers if isinstance(answers, list) else []:
        sub = _slug(a.get("sub"))
        if not sub:
            unparsable += 1
            continue
        # The one answer that is not allowed. A bucket name is the model
        # declining to read, and the whole point here is that nothing ends up
        # in "others" -- so count it and keep the name for the consolidation
        # pass to place, rather than letting it stand as a category.
        if sub in ("others_unclassified", "needs_review", "miscellaneous",
                   "other", "others", "unclassified", "general"):
            refused[sub] += 1
        try:
            out[int(a["id"])] = (sub, float(a.get("conf", 0)), sub not in known)
        except (KeyError, ValueError, TypeError):
            unparsable += 1
    if refused:
        print(f"    {sum(refused.values())} bucket answers: "
              f"{', '.join(k for k, _ in refused.most_common(3))}")
    if unparsable:
        print(f"    {unparsable} answers had an unusable id")
    missing = [r[0] for r in rows if r[0] not in out]
    if missing:
        print(f"    {len(missing)} of {len(rows)} orders got no answer")
    return out, usage


def _slug(v) -> str:
    """Normalise a proposed name so near-identical proposals pool together."""
    s = re.sub(r"[^a-z0-9]+", "_", str(v or "").strip().lower()).strip("_")
    return s[:48]


def money(u: dict) -> float:
    cached = u.get("cachedContentTokenCount", 0)
    fresh = max(u.get("promptTokenCount", 0) - cached, 0)
    return (fresh / 1e6 * PRICE_IN
            + cached / 1e6 * PRICE_IN * 0.25
            + u.get("candidatesTokenCount", 0) / 1e6 * PRICE_OUT)


# ── db ───────────────────────────────────────────────────────────────────────

def conn():
    import psycopg2
    dsn = env_file("supabase", "SUPABASE_DB_URL")
    if not dsn:
        sys.exit("SUPABASE_DB_URL not set and not in ~/.askmyjunior/supabase.env")
    # The pooler drops long reads, so keepalives, same as every other script here.
    return psycopg2.connect(dsn, keepalives=1, keepalives_idle=30,
                            keepalives_interval=10, keepalives_count=5)


def retry_db(fn, tries=6, what="query"):
    """Reconnect and retry. The CONNECT is inside the loop, deliberately.

    The pooler drops every client at once now and then -- it took out this run
    and the reflow simultaneously at 18:50 on 2026-10-03, with Postgres itself
    untouched (uptime unbroken, read_only off, 1.4 GB of disk headroom spare).
    A dropped connection is a transient to ride out, not a reason to lose
    three hours of work.
    """
    import psycopg2
    for a in range(tries):
        try:
            return fn()
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            if a == tries - 1:
                raise
            wait = min(60, 2 ** a * 4)
            print(f"    db {what} dropped ({str(e).splitlines()[0][:60]}); "
                  f"reconnecting in {wait}s", flush=True)
            time.sleep(wait)


def fetch(ids=None, limit=None, after=0, want_body=None):
    # Only drag the order body across the wire when it is actually going into
    # the prompt. The pilot settled that question -- abstract alone matched
    # abstract + 1,200 body characters at 92.7%, so BODY_CHARS is 0 and this
    # column is 792 MB of pure waste on a ~1 MB/s link, slowing the run and
    # starving the reflow it shares that link with.
    # The pilot passes want_body=True explicitly: it fetches once and then
    # tests both input shapes, so it cannot rely on the global.
    body = "g.order_text" if (BODY_CHARS if want_body is None else want_body) else "null"
    sql = f"""select g.id, g.abstract, {body}
               from public.ap_government_orders g
              where {{where}} order by g.id {{lim}}"""
    with conn() as c, c.cursor() as cur:
        cur.execute("set statement_timeout='10min'")
        if ids:
            cur.execute(sql.format(where="g.id = any(%s)", lim=""), (list(ids),))
        else:
            cur.execute(sql.format(where="g.id > %s and g.abstract is not null",
                                   lim="limit %s"), (after, limit or BATCH))
        return cur.fetchall()


# ── commands ─────────────────────────────────────────────────────────────────

def cmd_models():
    req = urllib.request.Request(f"{ENDPOINT}?pageSize=200",
                                 headers={"x-goog-api-key": api_key()})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            ms = json.loads(resp.read()).get("models", [])
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {_scrub(e.read()[:300].decode('utf8','replace'))}")
    usable = [m for m in ms if "generateContent" in m.get("supportedGenerationMethods", [])]
    print(f"{len(usable)} models support generateContent:")
    for m in sorted(usable, key=lambda m: m["name"]):
        if "flash" in m["name"] or "pro" in m["name"]:
            print(f"  {m['name'].split('/')[-1]:38s} in={m.get('inputTokenLimit')}")
    print(f"\ndefault MODEL = {MODEL}")


def cmd_pilot():
    """The decisive experiment: the same 150 orders I judged by hand."""
    global BODY_CHARS
    scratch = next((p for p in pathlib.Path("/private/tmp/claude-501").rglob(
        "scratchpad/audit_sample.json")), None)
    if not scratch:
        sys.exit("audit_sample.json not found -- the 150-order sample is the baseline")
    ids = json.loads(scratch.read_text())
    mine = (scratch.parent / "my_audit_calls.txt").read_text().split()
    truth = dict(zip(ids, mine))
    sub_to_cat, tax_block, num_to_key = load_taxonomy()
    rows = fetch(ids=ids, want_body=True)
    print(f"{len(rows)} of {len(ids)} audit orders fetched\n")

    for body_chars in (0, 1200):
        BODY_CHARS = body_chars
        shape = "abstract only" if not body_chars else f"abstract + {body_chars} body chars"
        got, spend, usage_tot = {}, 0.0, Counter()
        t0 = time.time()
        for i in range(0, len(rows), BATCH):
            a, u = classify(rows[i:i + BATCH], tax_block, num_to_key)
            got.update(a); usage_tot.update(u); spend += money(u)
        hit = miss = 0
        wrong = []
        for gid, abstract, _b in rows:
            want = truth.get(gid)
            sub, _conf, _coined = got.get(gid, ("<none>", 0, False))
            cat = sub_to_cat.get(sub, (sub,))[0]
            if cat == want:
                hit += 1
            else:
                miss += 1
                if len(wrong) < 8:
                    wrong.append((gid, want, cat, sub, (abstract or "")[:90]))
        n = hit + miss
        per_order = spend / max(n, 1)
        print(f"── {shape} ──")
        print(f"  agreement with my hand calls: {hit}/{n} = {hit/max(n,1):.1%}"
              f"   (rules scored 64%)")
        print(f"  tokens in/out {usage_tot['promptTokenCount']:,}/"
              f"{usage_tot['candidatesTokenCount']:,}"
              f"  (cached {usage_tot['cachedContentTokenCount']:,})"
              f"  spend ${spend:.4f}  {time.time()-t0:.0f}s")
        print(f"  extrapolated to 521,878 orders: ${per_order*521878:,.2f}")
        for gid, want, cat, sub, ab in wrong:
            print(f"    {gid} mine={want} ai={cat}/{sub}  {ab}")
        print()


def cmd_run():
    global BODY_CHARS
    args = sys.argv[2:]
    limit = next((int(a.split("=")[1]) for a in args if a.startswith("--limit=")), None)
    BODY_CHARS = next((int(a.split("=")[1]) for a in args if a.startswith("--body-chars=")), BODY_CHARS)
    tag = next((a.split("=")[1] for a in args if a.startswith("--tag=")), "run")
    jl = OUT / f"{tag}.jsonl"
    done = set()
    if jl.exists():
        for line in jl.open():
            try: done.add(json.loads(line)["id"])
            except Exception: pass
        print(f"resuming: {len(done):,} already in {jl.name}")
    sub_to_cat, tax_block, num_to_key = load_taxonomy()

    # A call takes ~3.8 s, nearly all of it waiting on Gemini. Run serially and
    # 20,875 calls is 22 hours of mostly-idle time; the work is embarrassingly
    # parallel, so a pool of WORKERS turns that into hours. Kept modest because
    # this uplink is ~1 MB/s and is already shared with the reflow.
    WORKERS = int(os.environ.get("GEMINI_WORKERS", 8))
    from concurrent.futures import ThreadPoolExecutor

    # Start past the work already done rather than paging from 0 and throwing
    # each page away. The JSONL is still the authority on what is finished --
    # this only decides where to begin looking.
    #
    # It does leave a gap: an order the model gave no answer for sits BELOW
    # this point and would be skipped for good. Those happen -- a reply comes
    # back short, or a batch fails mid-flight. So the run finishes with a sweep
    # from 0, which re-pages the whole corpus and picks up anything missing.
    # That pass is cheap now that order_text is not being fetched, and the
    # JSONL filter means it pays Gemini only for genuinely unclassified orders.
    after = 0 if os.environ.get("GEMINI_FROM") == "0" else (max(done) if done else 0)
    n, spend, t0 = 0, 0.0, time.time()
    if after:
        print(f"starting from id {after:,}  (GEMINI_FROM=0 to sweep from the beginning)")
    elif done:
        print(f"sweeping from id 0 for orders missed earlier")
    print(f"{WORKERS} workers, {BATCH} orders per call")
    with jl.open("a") as fh, ThreadPoolExecutor(max_workers=WORKERS) as pool:
        while True:
            rows = retry_db(lambda: fetch(limit=BATCH * WORKERS * 3, after=after),
                            what="fetch")
            if not rows:
                break
            after = rows[-1][0]
            rows = [r for r in rows if r[0] not in done]
            chunks = [rows[i:i + BATCH] for i in range(0, len(rows), BATCH)]
            # Results are written as each call lands. The JSONL is the resume
            # point, so a crash costs at most the calls still in flight.
            for chunk, (got, u) in zip(
                    chunks, pool.map(lambda ch: classify(ch, tax_block, num_to_key), chunks)):
                spend += money(u)
                for gid, _a, _b in chunk:
                    if gid in got:
                        sub, cf, coined = got[gid]
                        # `cat` is deliberately left for consolidation: the
                        # hierarchy is decided once, with every name in view,
                        # not guessed one batch at a time.
                        fh.write(json.dumps({
                            "id": gid, "sub": sub, "conf": cf,
                            "cat": sub_to_cat.get(sub, (None,))[0],
                            "coined": coined}) + "\n")
                        n += 1
                fh.flush()
                if n and n % 2000 < BATCH:
                    rate = n / max(time.time() - t0, 1)
                    left = (484573 - n) / max(rate, 0.1) / 3600
                    print(f"  {n:,} classified  ${spend:.2f}  {rate:.0f}/s  "
                          f"{left:.1f} h left", flush=True)
            if limit and n >= limit:
                break
    print(f"\n{n:,} classified, ${spend:.2f}, {(time.time()-t0)/60:.0f} min -> {jl}")


def cmd_consolidate():
    """Decide the hierarchy once, with every observed name in view.

    The classification pass names what each order is about and deliberately
    leaves `cat` null. Nothing about the hierarchy can be decided one batch at
    a time: whether `vehicle_purchase` deserves its own sub-category, and what
    it sits under, is only answerable once you can see it occurs 4,000 times
    across eleven departments. So the names are pooled here and placed in one
    reckoning.

    It runs incrementally, most frequent names first, carrying the decisions
    already made into each later chunk. That ordering matters: the big, clearly
    real sub-categories get to establish themselves first, and the long tail of
    one-off names is then merged INTO them rather than minting near-duplicates.

    The existing 14 categories are foundational -- they are passed in every
    chunk and reused by default. A new top-level category is allowed, but it
    has to be asked for explicitly, which keeps them rare.
    """
    tag = next((a.split("=")[1] for a in sys.argv[2:] if a.startswith("--tag=")), "full")
    # 250 names overran maxOutputTokens and came back as truncated JSON --
    # and because that chunk is sorted most-frequent-first, the one failure
    # took 99.88% of the corpus with it while the run still "succeeded".
    CHUNK = int(os.environ.get("CONSOLIDATE_CHUNK", 90))
    jl = OUT / f"{tag}.jsonl"
    recs = [json.loads(l) for l in jl.open() if l.strip()]
    tally = Counter(r["sub"] for r in recs if r.get("sub"))
    print(f"{len(recs):,} orders, {len(tally):,} distinct sub-category names")

    d = json.loads(TAXONOMY.read_text())
    cats = {str(c["number"]): c["label"] for c in d["categories"]
            if str(c["number"]) != "15"}
    # An example abstract per name: a bare name like `allocation_of_business`
    # is ambiguous, and one real order disambiguates it cheaply.
    #
    # ONE representative id per name, chosen here, then a single query for those
    # few hundred abstracts. The first version fetched abstracts for all 484,572
    # classified orders -- 97 round trips carrying the whole corpus -- to fill a
    # dictionary with 665 entries, and hung doing it.
    rep = {}
    for r in recs:
        sub = r.get("sub")
        if sub and sub not in rep:
            rep[sub] = r["id"]
    want = {v: k for k, v in rep.items()}

    def _examples():
        with conn() as c, c.cursor() as cur:
            cur.execute("set statement_timeout='120s'")
            cur.execute("""select id, left(abstract, 150) from
                           public.ap_government_orders where id = any(%s)""",
                        (list(want),))
            return cur.fetchall()

    example = {}
    for gid, ab in retry_db(_examples, what="examples"):
        example[want[gid]] = ab or ""
    print(f"  fetched {len(example)} example abstracts, one per name")

    schema = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "canonical": {"type": "STRING"},
        "cat": {"type": "STRING"}, "cat_label": {"type": "STRING"},
        "label": {"type": "STRING"}, "definition": {"type": "STRING"},
        "merges": {"type": "ARRAY", "items": {"type": "STRING"}}},
        "required": ["canonical", "cat", "merges"],
        "propertyOrdering": ["canonical", "cat", "cat_label", "label", "definition", "merges"]}}

    SYS = ("You are building the sub-category taxonomy for a corpus of 521,878 Andhra "
           "Pradesh Government Orders, used by lawyers.\n\n"
           "Each input line is a sub-category name observed in the corpus, how many "
           "orders carry it, and one example abstract.\n\n"
           "Group them. Return one entry per GROUP:\n"
           "  canonical - the group's sub-category name. Reuse an ALREADY SETTLED "
           "canonical whenever the group means the same thing, even when worded "
           "differently; otherwise use the group's clearest input name.\n"
           "  merges    - EVERY input name in this group, including the canonical "
           "itself when it was one of the inputs. This is the critical field.\n"
           "  cat       - the number of the head category the group sits under\n"
           "  cat_label - only when proposing a head category that does not exist yet; "
           "then set cat to the next free number\n"
           "  label, definition - Title Case name and one sentence\n\n"
           "EVERY input name must appear in exactly one group's `merges`. A name you "
           "leave out is an order that ends up filed nowhere.\n\n"
           "Merge aggressively where names mean the same thing: variants, singular and "
           "plural, and narrower wordings of a settled sub-category all fold in. Do NOT "
           "merge things lawyers would search for separately.\n\n"
           "A name appearing in only a handful of orders is usually a wording variant of "
           "something settled, not a new sub-category -- fold it in unless it is "
           "genuinely a distinct subject.\n\n"
           "Never produce a canonical meaning 'other', 'miscellaneous', 'general' or "
           "'needs review'. Every order is about something; place it by subject.\n\n"
           "HEAD CATEGORIES (foundational -- reuse these; propose a new one only if a "
           "subject genuinely has no home):\n")

    # Resume point. Unlike the classification, consolidation used to start from
    # scratch every time -- so an interrupted run threw away chunks that had
    # already been paid for, and the 402 message cheerfully claimed otherwise.
    part = OUT / f"{tag}.partial.json"
    # Category each reference-taxonomy name already lives under, for the net.
    orig_cat = {}
    for c in d["categories"]:
        for sub in c["subcategories"]:
            orig_cat[sub["key"]] = str(c["number"])
    dropped = {}

    settled, mapping, new_cats = {}, {}, {}
    if part.exists():
        P = json.loads(part.read_text())
        settled, mapping, new_cats = P["settled"], P["mapping"], P["new_cats"]
        print(f"  resuming: {len(settled)} canonicals, {len(mapping)} names already placed")
    names = [n for n, _ in tally.most_common()]
    for i in range(0, len(names), CHUNK):
        chunk = [n for n in names[i:i + CHUNK] if n not in mapping]
        if not chunk:
            continue
        known = "\n".join(f'{k} (cat {v["cat"]})' for k, v in settled.items())
        catlines = "\n".join(f"{k} {v}" for k, v in {**cats, **new_cats}.items())
        listing = "\n".join(
            f'{tally[n]} | {n} | {re.sub(chr(115)+"+", " ", (example.get(n) or ""))[:110]}'
            for n in chunk)
        def _ask(names_subset):
            text = "\n".join(
                f'{tally[n]} | {n} | '
                f'{re.sub(chr(115)+"+", " ", (example.get(n) or ""))[:110]}'
                for n in names_subset)
            rr = post(f"{ENDPOINT}/{MODEL}:generateContent", {
                "systemInstruction": {"parts": [{"text": SYS + catlines +
                    ("\n\nALREADY SETTLED sub-categories -- reuse these names:\n" + known
                     if known else "")}]},
                "contents": [{"role": "user", "parts": [{"text": text}]}],
                "generationConfig": {"temperature": 0,
                                     "responseMimeType": "application/json",
                                     "responseSchema": schema,
                                     "maxOutputTokens": 32768}})
            try:
                return json.loads(rr["candidates"][0]["content"]["parts"][0]["text"])
            except (KeyError, IndexError, json.JSONDecodeError):
                return None

        r = post(f"{ENDPOINT}/{MODEL}:generateContent", {
            "systemInstruction": {"parts": [{"text": SYS + catlines +
                ("\n\nALREADY SETTLED sub-categories -- reuse these names:\n" + known
                 if known else "")}]},
            "contents": [{"role": "user", "parts": [{"text": listing}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json",
                                 "responseSchema": schema, "maxOutputTokens": 32768}})
        try:
            rows = json.loads(r["candidates"][0]["content"]["parts"][0]["text"])
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            # Truncated JSON means the reply outgrew maxOutputTokens. Halving
            # and retrying costs one extra call; skipping silently cost the
            # whole corpus last time.
            print(f"  chunk {i//CHUNK+1}: unusable reply ({e}); halving and retrying")
            rows = []
            for half in (chunk[:len(chunk)//2], chunk[len(chunk)//2:]):
                if not half:
                    continue
                rr = _ask(half)
                if rr is None:
                    print(f"    sub-chunk of {len(half)} still unusable; left unmapped")
                else:
                    rows.extend(rr)
            if not rows:
                continue
        for a in rows:
            canon = _slug(a.get("canonical"))
            if not canon:
                continue
            cat = str(a.get("cat", "")).strip()
            if a.get("cat_label") and cat not in cats and cat not in new_cats:
                new_cats[cat] = a["cat_label"].strip()
            settled.setdefault(canon, {"cat": cat,
                                       "label": (a.get("label") or canon).strip(),
                                       "definition": (a.get("definition") or "").strip()})
            for nm in (a.get("merges") or []) + [a.get("canonical")]:
                nm = _slug(nm)
                if nm:
                    mapping[nm] = canon

        # Safety net. A name the model forgot is an order filed nowhere, and the
        # biggest names are the ones that hurt -- 480,393 orders went missing to
        # exactly this. Anything still unplaced keeps its own name, and takes its
        # category from the reference taxonomy when it came from there.
        for nm in chunk:
            if nm not in mapping:
                home = orig_cat.get(nm)
                mapping[nm] = nm
                settled.setdefault(nm, {"cat": home or "14", "label": nm,
                                        "definition": ""})
                dropped[nm] = tally[nm]
        part.write_text(json.dumps({"settled": settled, "mapping": mapping,
                                    "new_cats": new_cats}))
        done = sum(tally[n] for n in chunk)
        print(f"  chunk {i//CHUNK+1}/{(len(names)+CHUNK-1)//CHUNK}: "
              f"{len(chunk)} names ({done:,} orders) -> {len(settled)} canonicals so far",
              flush=True)

    unmapped = [n for n in names if n not in mapping]
    orders_mapped = sum(tally[n] for n in names if n in mapping)
    if dropped:
        tot = sum(dropped.values())
        print(f"\n  {len(dropped)} names the model left out of its groups "
              f"({tot:,} orders) were kept under their own names")
        for k, v in sorted(dropped.items(), key=lambda kv: -kv[1])[:8]:
            print(f"    {v:7,}  {k}")
    if orders_mapped < 0.95 * sum(tally.values()):
        print(f"\n*** ONLY {orders_mapped:,} of {sum(tally.values()):,} ORDERS PLACED "
              f"({orders_mapped/sum(tally.values()):.1%}) -- this taxonomy is NOT usable. "
              f"Re-run; the partial file means finished chunks are not repeated. ***")
    print(f"\n{len(tally):,} names -> {len(settled)} canonical sub-categories")
    print(f"  orders placed : {orders_mapped:,} of {sum(tally.values()):,}")
    print(f"  names unmapped: {len(unmapped)}"
          + (": " + ", ".join(unmapped[:8]) if unmapped else ""))
    if new_cats:
        print(f"  NEW head categories proposed: {new_cats}")
    sizes = Counter()
    for n, c in mapping.items():
        sizes[c] += tally[n]
    print("\n  largest sub-categories:")
    for k, v in sizes.most_common(15):
        print(f"    {v:7,}  cat {settled[k]['cat']:>2}  {k}")
    out = OUT / f"{tag}.taxonomy.json"
    out.write_text(json.dumps({"categories": {**cats, **new_cats},
                               "subcategories": settled, "mapping": mapping,
                               "unmapped": unmapped}, indent=2))
    print(f"\n-> {out}")


def cmd_sweep():
    """Classify exactly the orders that are missing, and nothing else.

    The old sweep re-paged the whole corpus from id 0 and discarded every row
    already done: 323 round trips to find a few thousand gaps, and it hung on a
    half-open pooler connection 27 minutes in. Asking the database which ids are
    missing is one query, and then only the gaps are fetched at all.

    Gaps are real and accumulate: a reply comes back short, a batch is in flight
    when the process dies, or a resume skips past an order that never got an
    answer. Without this pass the corpus quietly ends up short.
    """
    tag = next((a.split("=")[1] for a in sys.argv[2:] if a.startswith("--tag=")), "full")
    jl = OUT / f"{tag}.jsonl"
    done = set()
    for line in jl.open():
        try: done.add(json.loads(line)["id"])
        except Exception: pass

    def _all_ids():
        with conn() as c, c.cursor() as cur:
            cur.execute("set statement_timeout='5min'")
            cur.execute("""select id from public.ap_government_orders
                            where abstract is not null order by id""")
            return [r[0] for r in cur.fetchall()]

    allids = retry_db(_all_ids, what="id list")
    missing = [i for i in allids if i not in done]
    print(f"{len(allids):,} orders with an abstract, {len(done):,} classified, "
          f"{len(missing):,} missing")
    if not missing:
        print("nothing to sweep")
        return

    sub_to_cat, tax_block, num_to_key = load_taxonomy()
    WORKERS = int(os.environ.get("GEMINI_WORKERS", 20))
    from concurrent.futures import ThreadPoolExecutor
    n, spend, t0 = 0, 0.0, time.time()
    with jl.open("a") as fh, ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for lo in range(0, len(missing), BATCH * WORKERS):
            block = missing[lo:lo + BATCH * WORKERS]
            rows = retry_db(lambda: fetch(ids=block), what="fetch")
            chunks = [rows[i:i + BATCH] for i in range(0, len(rows), BATCH)]
            for chunk, (got, u) in zip(chunks, pool.map(
                    lambda ch: classify(ch, tax_block, num_to_key), chunks)):
                spend += money(u)
                for gid, _a, _b in chunk:
                    if gid in got:
                        sub, cf, coined = got[gid]
                        fh.write(json.dumps({
                            "id": gid, "sub": sub, "conf": cf,
                            "cat": sub_to_cat.get(sub, (None,))[0],
                            "coined": coined}) + "\n")
                        n += 1
                fh.flush()
            print(f"  swept {n:,} of {len(missing):,}  ${spend:.2f}", flush=True)
    still = len(missing) - n
    print(f"\nswept {n:,}, ${spend:.2f}, {(time.time()-t0)/60:.0f} min")
    if still:
        print(f"{still:,} orders STILL have no answer -- re-run sweep to retry them")


def cmd_refine():
    """A second pass over the CANONICALS, not the raw names.

    Consolidation works chunk by chunk in frequency order, carrying the settled
    names forward as text. That works while the settled list is short: the first
    chunk turned 90 names covering 480,393 orders into 88 canonicals. By the
    later chunks there were 300+ settled names in the prompt, the model stopped
    merging into them, and started minting instead -- `act_rules_ammendment`
    beside `act_rules_amendment`, `institution_establishment_general` beside
    `institution_establishment`, and 81 sub-categories for the 7,968 orders in
    category 14.

    There are only ~312 canonicals, so they all fit in ONE call. No chunking
    means no horizon, and the model can see every name while deciding what
    merges with what.
    """
    tag = next((a.split("=")[1] for a in sys.argv[2:] if a.startswith("--tag=")), "full")
    tx = OUT / f"{tag}.taxonomy.json"
    T = json.loads(tx.read_text())
    mapping, subs, cats = T["mapping"], T["subcategories"], T["categories"]
    recs = [json.loads(l) for l in (OUT / f"{tag}.jsonl").open() if l.strip()]
    tally = Counter(r["sub"] for r in recs if r.get("sub"))
    size = Counter()
    for n, c in mapping.items():
        size[c] += tally[n]

    d = json.loads(TAXONOMY.read_text())
    cat_lines = "\n".join(f'{c["number"]} {c["label"]}' for c in d["categories"]
                          if str(c["number"]) != "15")
    listing = "\n".join(
        f'{size[k]} | {k} | currently under category {v["cat"]}'
        for k, v in sorted(subs.items(), key=lambda kv: -size[kv[0]]) if size[k])
    print(f"{len(subs)} canonicals, {sum(size.values()):,} orders -> one call")

    schema = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "canonical": {"type": "STRING"}, "cat": {"type": "STRING"},
        "label": {"type": "STRING"}, "definition": {"type": "STRING"},
        "merges": {"type": "ARRAY", "items": {"type": "STRING"}}},
        "required": ["canonical", "cat", "merges"],
        "propertyOrdering": ["canonical", "cat", "label", "definition", "merges"]}}

    SYS = ("You are tidying the sub-category taxonomy for 484,572 Andhra Pradesh "
           "Government Orders, used by lawyers. It was built bottom-up from the corpus "
           "and has fragmented: near-duplicates, misspellings minted as separate "
           "sub-categories, and one-order entries that belong inside bigger ones.\n\n"
           "Each line is a current sub-category, how many orders it holds, and the head "
           "category it sits under.\n\n"
           "Return the CLEANED taxonomy, one entry per final sub-category:\n"
           "  canonical  - its name\n"
           "  merges     - EVERY current name that folds into it, including itself\n"
           "  cat        - the head category number it belongs under\n"
           "  label, definition - Title Case name and one sentence\n\n"
           "Rules:\n"
           "- EVERY current name must appear in exactly one `merges`. One left out is "
           "orders filed nowhere.\n"
           "- Merge misspellings and near-duplicates into the larger entry "
           "(`act_rules_ammendment` into `act_rules_amendment`, "
           "`institution_establishment_general` into `institution_establishment`).\n"
           "- Fold tiny entries into the bigger sub-category they are a special case "
           "of, unless a lawyer would genuinely search for them separately.\n"
           "- NEVER output a sub-category meaning 'other', 'miscellaneous', 'general' "
           "or 'needs review'. Fold those names into whatever real subject fits best; "
           "they are a symptom of giving up, not a category.\n"
           "- Keep a sub-category separate when it is a real subject with real volume, "
           "even if small. Do not over-merge distinct subjects.\n"
           "- Move an entry to a better head category where it is clearly misfiled.\n\n"
           "Aim for roughly 150-200 final sub-categories.\n\n"
           "HEAD CATEGORIES:\n")

    r = post(f"{ENDPOINT}/{MODEL}:generateContent", {
        "systemInstruction": {"parts": [{"text": SYS + cat_lines}]},
        "contents": [{"role": "user", "parts": [{"text": listing}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json",
                             "responseSchema": schema, "maxOutputTokens": 60000}})
    try:
        groups = json.loads(r["candidates"][0]["content"]["parts"][0]["text"])
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        fin = (r.get("candidates") or [{}])[0].get("finishReason")
        sys.exit(f"unusable reply ({e}; finishReason={fin}). Nothing written.")

    remap, final = {}, {}
    for g in groups:
        canon = _slug(g.get("canonical"))
        if not canon:
            continue
        final[canon] = {"cat": str(g.get("cat", "")).strip(),
                        "label": (g.get("label") or canon).strip(),
                        "definition": (g.get("definition") or "").strip()}
        for nm in (g.get("merges") or []) + [g.get("canonical")]:
            nm = _slug(nm)
            if nm:
                remap[nm] = canon
    # Same net as before: anything the model left out keeps its own entry.
    left = [k for k in subs if size[k] and k not in remap]
    for k in left:
        remap[k] = k
        final.setdefault(k, subs[k])
    if left:
        print(f"  {len(left)} canonicals left out of the groups, kept as-is: "
              f"{', '.join(sorted(left, key=lambda x: -size[x])[:6])}")

    new_mapping = {n: remap.get(c, c) for n, c in mapping.items()}
    placed = sum(tally[n] for n in new_mapping)
    newsize = Counter()
    for n, c in new_mapping.items():
        newsize[c] += tally[n]
    print(f"\n{len(subs)} -> {len([k for k in final if newsize[k]])} sub-categories, "
          f"{placed:,} of {sum(tally.values()):,} orders placed")
    buckets = [k for k in final if any(w in k for w in
               ("miscellan", "needs_review", "unclassified", "_general", "other"))]
    if buckets:
        print(f"  bucket-ish names remaining: "
              f"{', '.join(f'{k} ({newsize[k]:,})' for k in buckets if newsize[k])}")
    tx.rename(tx.with_suffix(".json.pass1"))
    tx.write_text(json.dumps({"categories": cats, "subcategories": final,
                              "mapping": new_mapping, "unmapped": []}, indent=2))
    print(f"-> {tx}  (previous kept as {tx.name}.pass1)")


def cmd_place():
    """Classify specific orders against the FINAL taxonomy.

    Two jobs need this. The orders sitting in `miscellaneous` / `needs_review`
    cannot be rescued by tidying names -- a bucket name carries no information,
    so the only fix is to read the order again. And the 9,060 orders with no
    abstract were never eligible for the main run, though they do have bodies.

    The taxonomy is settled now, so this is a closed choice: the model picks a
    NUMBER from the final list. Numbers keep the enum under its ~1,750-character
    ceiling where 197 real keys would not fit, and an enum means it cannot
    invent an answer or reach for a bucket -- there is none to reach for.

      --buckets       re-read the orders stuck in bucket sub-categories
      --no-abstract   the orders that have an order body but no abstract
    """
    args = sys.argv[2:]
    tag = next((a.split("=")[1] for a in args if a.startswith("--tag=")), "full")
    T = json.loads((OUT / f"{tag}.taxonomy.json").read_text())
    mapping, subs, cats = T["mapping"], T["subcategories"], T["categories"]
    recs = [json.loads(l) for l in (OUT / f"{tag}.jsonl").open() if l.strip()]

    BUCKET = ("miscellan", "needs_review", "unclassified", "_general", "others")
    final = {k: v for k, v in subs.items() if not any(b in k for b in BUCKET)}
    num_to_key = {str(i): k for i, k in enumerate(sorted(final), 1)}
    block = "\n".join(
        f'{i} | {k} | category {final[k]["cat"]} {cats.get(str(final[k]["cat"]), "")}'
        f' | {final[k].get("definition", "")[:90]}'
        for i, k in num_to_key.items())

    resub = next((a.split("=")[1] for a in args if a.startswith("--resub=")), None)
    if resub:
        fin = json.load(open(OUT / f"{tag}.final.json"))
        targets = [int(g) for g, k in fin.items() if k == resub]
        use_body, out_tag = False, f"resub_{resub}"
    elif "--reread" in args:
        ids = json.loads((OUT / "reread_ids.json").read_text())
        targets, use_body, out_tag = ids, False, "reread"
    elif "--no-abstract" in args:
        def _ids():
            with conn() as c, c.cursor() as cur:
                cur.execute("set statement_timeout='5min'")
                cur.execute("""select id from public.ap_government_orders
                                where abstract is null and order_text is not null
                                  and length(order_text) > 200 order by id""")
                return [r[0] for r in cur.fetchall()]
        targets, use_body, out_tag = retry_db(_ids, what="no-abstract ids"), True, "bodies"
    else:
        targets = [r["id"] for r in recs
                   if any(b in (mapping.get(r.get("sub") or "") or "") for b in BUCKET)]
        use_body, out_tag = False, "rebucket"
    jl = OUT / f"{tag}.{out_tag}.jsonl"
    done = set()
    if jl.exists():
        for line in jl.open():
            try: done.add(json.loads(line)["id"])
            except Exception: pass
    targets = [i for i in targets if i not in done]
    print(f"{len(final)} sub-categories to choose from; {len(targets):,} orders to place"
          + (f" ({len(done):,} already done)" if done else ""))
    if not targets:
        return

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

    WORKERS = int(os.environ.get("GEMINI_WORKERS", 20))
    from concurrent.futures import ThreadPoolExecutor
    global INSTRUCTION, BODY_CHARS
    saved_instr, saved_body = INSTRUCTION, BODY_CHARS
    INSTRUCTION = INSTR
    BODY_CHARS = 1500 if use_body else 0
    n, spend, t0 = 0, 0.0, time.time()
    try:
        with jl.open("a") as fh, ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for lo in range(0, len(targets), BATCH * WORKERS):
                blk = targets[lo:lo + BATCH * WORKERS]
                rows = retry_db(lambda: fetch(ids=blk, want_body=use_body), what="fetch")
                if use_body:
                    # No abstract: the body stands in for it.
                    rows = [(i, (b or "")[:1500], None) for i, _a, b in rows]
                chunks = [rows[i:i + BATCH] for i in range(0, len(rows), BATCH)]
                for chunk, (got, u) in zip(chunks, pool.map(
                        lambda ch: classify(ch, block, num_to_key), chunks)):
                    spend += money(u)
                    for gid, _a, _b in chunk:
                        if gid in got:
                            num, cf, _c = got[gid]
                            # classify() hands back the model's literal answer;
                            # here that is a NUMBER, so translate it.
                            sub = num_to_key.get(num)
                            if sub is None:
                                continue
                            fh.write(json.dumps({"id": gid, "sub": sub, "conf": cf,
                                                 "cat": final.get(sub, {}).get("cat")}) + "\n")
                            n += 1
                    fh.flush()
                print(f"  placed {n:,} of {len(targets):,}  ${spend:.2f}", flush=True)
    finally:
        INSTRUCTION, BODY_CHARS = saved_instr, saved_body
    print(f"\nplaced {n:,}, ${spend:.2f}, {(time.time()-t0)/60:.0f} min -> {jl.name}")
    if n < len(targets):
        print(f"{len(targets)-n:,} got no answer -- re-run to retry them")


def cmd_load():
    """JSONL + consolidated taxonomy -> go_classification_ai.

    Never go_classification: the rules' output stays where it is, serving the
    live site, until the new taxonomy is reviewed and the switch is deliberate.

    Refuses to run without the consolidation, because the raw JSONL carries the
    observed name and a null category by design -- loading it alone would put
    wording variants into the corpus as though each were a real sub-category.
    """
    from psycopg2.extras import execute_values
    tag = next((a.split("=")[1] for a in sys.argv[2:] if a.startswith("--tag=")), "full")
    jl, tx = OUT / f"{tag}.jsonl", OUT / f"{tag}.taxonomy.json"
    if not tx.exists():
        sys.exit(f"{tx.name} not found -- run `consolidate --tag={tag}` first")
    T = json.loads(tx.read_text())
    mapping, subs, cats = T["mapping"], T["subcategories"], T["categories"]
    recs = [json.loads(l) for l in jl.open() if l.strip()]
    print(f"{len(recs):,} records, {len(subs)} canonical sub-categories, "
          f"{len(cats)} head categories")

    rows, unplaced = [], Counter()
    for r in recs:
        canon = mapping.get(r.get("sub") or "")
        if not canon or canon not in subs:
            unplaced[r.get("sub")] += 1
            continue
        cat = subs[canon]["cat"]
        rows.append((r["id"], cats.get(cat, cat), canon, r.get("conf"), MODEL))
    if unplaced:
        print(f"  {sum(unplaced.values()):,} orders have a name the consolidation "
              f"did not place: {', '.join(k or '<none>' for k, _ in unplaced.most_common(5))}")
        print("  -> these are NOT loaded. Re-run consolidate, or they stay out.")
    print(f"  loading {len(rows):,}")

    with conn() as c, c.cursor() as cur:
        cur.execute("""
            create table if not exists public.go_classification_ai (
                go_id bigint primary key,
                primary_category text not null,
                sub_category text not null,
                confidence real,
                model text not null,
                classified_at timestamptz not null default now())""")
        cur.execute("alter table public.go_classification_ai enable row level security")
        cur.execute("alter table public.go_classification_ai force row level security")
        for i in range(0, len(rows), 5000):
            execute_values(cur, """
                insert into public.go_classification_ai
                    (go_id, primary_category, sub_category, confidence, model)
                values %s
                on conflict (go_id) do update set
                    primary_category = excluded.primary_category,
                    sub_category = excluded.sub_category,
                    confidence = excluded.confidence,
                    model = excluded.model,
                    classified_at = now()""", rows[i:i + 5000])
            c.commit()
            if (i // 5000) % 10 == 0:
                print(f"  {min(i+5000, len(rows)):,}", flush=True)
        cur.execute("""select count(*), count(distinct primary_category),
                              count(distinct sub_category)
                         from public.go_classification_ai""")
        n, nc, ns = cur.fetchone()
        print(f"go_classification_ai: {n:,} rows, {nc} categories, {ns} sub-categories")
        cur.execute("""select count(*) from public.go_classification_ai
                        where sub_category in ('others_unclassified','needs_review')""")
        print(f"  orders in an 'others' bucket: {cur.fetchone()[0]}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "pilot"
    {"models": cmd_models, "pilot": cmd_pilot, "run": cmd_run, "sweep": cmd_sweep,
     "consolidate": cmd_consolidate, "refine": cmd_refine, "place": cmd_place, "load": cmd_load}.get(cmd, cmd_pilot)()
