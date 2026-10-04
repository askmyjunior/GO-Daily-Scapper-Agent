#!/usr/bin/env python3
"""Resolve every GO to a canonical department. Sidecar output; nothing is overwritten.

WHY THIS IS RESOLUTION, NOT INVENTION
-------------------------------------
9,237 records have no body-extracted department, because the `dept_department`
anchor was missing from the page. The name is not actually unknown for any of
them: `filename_parsed.dept_name` carries it, and every record in the corpus has
a `dept_code`. This module maps those codes onto the authoritative 45-department
list and records where each answer came from.

WHY THE BODY VALUE IS NEVER OVERWRITTEN
---------------------------------------
219 files coded `HOM01` (Home) print `LAW (LA&J-HOME-COURTS.A1) DEPARTMENT` on
the page, and their `go_citation_raw` agrees. They are Law Department orders
concerning Home-Courts, filed under Home by the download portal's folder
taxonomy. 175 `SOW01` files print `TRIBAL WELFARE`. Filling `department` from the
filename code would have replaced ~400 *correct* extractions with the wrong
department. The printed page wins on accuracy; the code wins on stable grouping;
both are kept and the disagreement is flagged rather than resolved away.

TWO CORRECTIONS TO THE SUPPLIED LIST
------------------------------------
The reference list gave `ESO01 - School Education` and `PI G01 - Planning`.
Neither code exists in the corpus. The real codes are `ESE01` (1,809 records,
filenames read "SCHOOL EDUCATION") and `PLG01` (332 records, "PLANNING"). Both
are transcription slips and are corrected here, evidenced by the filenames.

THE SUFFIXED CODES ARE HISTORICAL AND WING NAMES
------------------------------------------------
`AGC01-R`, `ICD01-P`, `ENE01-E`, `SEI01-S` and the rest never appear as a
dept_code on any file, which at first looks like dead reference data. They are
not: they are what the *body* text says. 1,800 `ICD01` orders print "IRRIGATION
AND CAD" — the pre-rename name of Water Resources — and 25 `AGC01` orders print
"RAIN SHADOW AREAS DEVELOPMENT". So all 45 entries serve as body-match targets,
while only the 35 unsuffixed ones can be reached from a filename code. A suffixed
match shares its base code with the file's own code and is therefore *not* a
conflict; it is the same department under an older or narrower name.

CONTENT RULE
------------
Identical to the table job: structure and classification may be derived, content
may not be changed. `department_name_body` and `sub_department` are copied
verbatim, misspellings included ("MUNICIPAL ADMINSTRATION", "SOCILA WELFARE" and
the rest survive untouched). The canonical name is an *added* grouping label, not
a correction of the source.

Usage:
    python3 resolve_departments.py out/corpus --apply
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from collections import Counter
from pathlib import Path

DEFAULT_OUTDIR = Path("out/departments")

# The authoritative list. `ESE01` and `PLG01` corrected per the filenames; every
# other entry verbatim as supplied.
CANONICAL = {
    "AGC01":   "Agriculture and Cooperation",
    "AGC01-R": "Rain Shadow Areas Development",
    "AHF01":   "Animal Husbandry and Fisheries",
    "BCW01":   "Backward Classes Welfare",
    "EFS01":   "Environment, Forest, Science and Technology",
    "EHE01":   "Higher Education",
    "ENE01":   "Energy, Infrastructure and Investment",
    "ENE01-E": "Energy",
    "ESE01":   "School Education",
    "EWS01":   "Economically Weaker Sections Welfare",
    "FCS01":   "Consumer Affairs, Food and Civil Supplies",
    "FIN01":   "Finance",
    "FIN01-P": "Finance PMU",
    "FIN01-W": "Finance Works and Projects",
    "GAD01":   "General Administration",
    "GWS01":   ("Grama Volunteers, Ward Volunteers and Grama Sachivalayams, "
                "Ward Sachivalayams"),
    "HMF01":   "Health, Medical and Family Welfare",
    "HOM01":   "Home",
    "HOU01":   "Housing",
    "ICD01":   "Water Resources",
    "ICD01-P": "Irrigation and CAD PW Wing",
    "INC01":   "Industries and Commerce",
    "INI01":   "Infrastructure and Investment",
    "ITC01":   "Information Technology, Electronics and Communications",
    "LAE01":   "Labour, Employment, Training and Factories",
    "LAE01-L": "Labour, Factories, Boilers, Insurance Medical Service",
    "LAW01":   "Law",
    "LEG01":   "Legislature",
    "MAU01":   "Municipal Administration and Urban Development",
    "MNW01":   "Minorities Welfare",
    "PBE01":   "Public Enterprise",
    "PLG01":   "Planning",
    "PRR01":   "Panchayat Raj and Rural Development",
    "REV01":   "Revenue",
    "REV01-D": "Disaster Management",
    "RTG01":   "Real Time Governance",
    "SEI01":   "Skill Development, Entrepreneurship and Innovation",
    "SEI01-S": "Skills Development Training Department",
    "SGSW01":  "Swarna Gramam and Swarna Wardu",
    "SOW01":   "Social Welfare",
    "STI01":   "Dept. of Science, Technology and Innovation",
    "TBW01":   "Tribal Welfare",
    # Singular "Building". The register's own dropdown reads TRANSPORT ROADS AND
    # BUILDING and so do all 1,875 TRB01 filenames in the corpus. The typed list
    # this table was first built from said "Buildings"; two independent sources
    # outrank one transcription.
    "TRB01":   "Transport, Roads and Building",
    "WDC01":   "Women Development, Child and Disabled Welfare",
    "YTC01":   "Youth Advancement, Tourism and Culture",

    # 46th entry in the register's dropdown, and not a department: it is the
    # portal's test account. Kept so the table is a faithful copy of the source
    # and so a ZZZ01 file would be named rather than silently unresolved. Zero
    # corpus records today. Excluded from body-name matching below -- no printed
    # heading should ever be allowed to fuzzy-match "Test User".
    "ZZZ01":   "Test User",
}

# Printed names that no string algorithm can reach, because the difference is
# domain knowledge rather than spelling: an acronym expanded ("CAD" = Command
# Area Development, "IMS" = Insurance Medical Service), a word abbreviated, or a
# pre-rename name sharing no vocabulary with the current one. Each entry is a
# name actually observed in the corpus, with its record count, so every mapping
# can be checked against the source. Keys are `normalise()` output.
#
# This is a lookup table, not a runtime guess: a name either appears here or is
# reported as `body_unresolved`. Nothing is approximated into place.
BODY_ALIASES = {
    "IRRIGATION AND COMMAND AREA DEVELOPMENT": "ICD01-P",   # 231
    "EDUCATION SE VIG I":                      "ESE01",     # 117
    "ITE AND C":                               "ITC01",     #  45
    "IRRIGATION AND CADA":                     "ICD01-P",   #  38
    "AGRICULTURAL MARKETING AND COOPERATION":  "AGC01",     #  32
    "REVEUNEU":                                "REV01",     #  31  (printed typo)
    "WOMEN DEV CHILD WELFARE AND DISABLED WELFARE": "WDC01",#  21
    "LABOUR FACTORIES BOILERS AND IMS":        "LAE01-L",   #  20
    "MUNICIPAL ADMN AND URBAN DEVELOPMENT":    "MAU01",     #  18
    "ENVIRONMENT AND FORESTS":                 "EFS01",     #  18
    "EDUCATION SE PS":                         "ESE01",     #  16
    "SECONDARY EDUCATION":                     "ESE01",     #  15
    "EDUCATION SE SER":                        "ESE01",     #  10
    "IRRGATION AND CADA":                      "ICD01-P",   #   9
    "BC WELFARE":                              "BCW01",     #   7
}

# Accepted on a normalised character ratio. Tuned against the observed corpus
# spellings: "MUNICIPAL ADMINSTRATION AND URBAN DEVELOPMENT" scores 0.99 against
# its canonical, while "HOME" against "HOUSING" scores 0.55, so the gap is wide
# and the threshold is not delicately placed.
FUZZY_MIN = 0.88

# `ICD01-P- IRRIGATION AND CAD PW WING-MS-143-...pdf` -> `ICD01-P`. Anchored at
# the start and requiring a single trailing capital so it cannot swallow part of
# a department name.
FILENAME_CODE_RE = re.compile(r"([A-Z]{2,5}\d+-[A-Z])-")

# Drafters bracket the wing with either round or square brackets, and they nest:
# `EDUCATION [SE- Vig.I(1) ] DEPARTMENT`. Searching only for `(...)` found the
# inner `(1)` and recorded the wing as "1" on 141 records, and missed the wing
# entirely on the 693 that use `[...]` alone. Matching the outermost bracket of
# either kind gets `SE- Vig.I(1)` and `AM III`. This is still a verbatim slice of
# the printed name -- nothing is rewritten, only a different span is quoted.
SUB_RE = re.compile(r"\(([^()]*(?:\([^()]*\)[^()]*)*)\)|\[([^\[\]]*)\]")
DEPT_WORD_RE = re.compile(r"\bDEPARTMENTS?\b")


def normalise(s):
    """Uppercase, expand '&', drop bracketed wings and the word DEPARTMENT.

    Used for *comparison only* -- the stored text is always the original.

    Consecutive single letters are glued back together, because the PDFs space
    acronyms out inconsistently: "IRRIGATION AND C A D" and "IT E AND C" are the
    same names as "IRRIGATION AND CAD" and "ITE AND C". Without this, 481 records
    fail to match purely on letter spacing.
    """
    if not s:
        return ""
    s = s.upper().replace("&", " AND ")
    # Strip the bracketed wing whichever bracket the drafter reached for. 693
    # records write it as `REVENUE[D.M.II]`, and leaving "D M II" in the string
    # pushes them off the exact match and down onto prefix or fuzzy.
    s = SUB_RE.sub(" ", s)
    s = DEPT_WORD_RE.sub(" ", s)
    s = re.sub(r"[^A-Z ]", " ", s)
    out, run = [], []
    for t in s.split():
        if len(t) == 1:
            run.append(t)
            continue
        if run:
            out.append("".join(run) if len(run) > 1 else run[0])
            run = []
        out.append(t)
    if run:
        out.append("".join(run) if len(run) > 1 else run[0])
    return " ".join(out)


STOPWORDS = {"AND", "OF", "THE", "DEPT", "DEPARTMENT", "WING", "PW"}


def tokens(n):
    """Significant tokens of an already-normalised name."""
    return {t for t in n.split() if t not in STOPWORDS}


# The body-matching table, which is NOT the same as the canonical table: the
# test account is a valid destination for a filename code but must never be a
# destination for a printed heading.
NOT_MATCHABLE_FROM_BODY = {"ZZZ01"}

NORMALISED = [(code, name, normalise(name)) for code, name in CANONICAL.items()
              if code not in NOT_MATCHABLE_FROM_BODY]


def base_code(code):
    """`ICD01-P` -> `ICD01`. A suffix is a wing of the same department."""
    return code.split("-")[0] if code else None


def match_body(body_name, dept_code=None):
    """(code, canonical_name, method, score) for a printed department name.

    `dept_code` is consulted *only* to break a tie between equally good matches --
    "EDUCATION" is a subset of both School Education and Higher Education, and the
    code the file is filed under is the only evidence available to choose. It
    never overrides a unique match, so cross-filings still surface as conflicts.
    """
    n = normalise(body_name)
    if not n:
        return None, None, None, None

    for code, name, cn in NORMALISED:
        if n == cn:
            return code, name, "exact", 1.0

    alias = BODY_ALIASES.get(n)
    if alias:
        return alias, CANONICAL[alias], "alias", 1.0

    # A printed name that merely extends the canonical one -- "ANIMAL HUSBANDRY
    # DAIRY DEVELOPMENT AND FISHERIES" against "ANIMAL HUSBANDRY AND FISHERIES"
    # -- is the same department described more fully. Longest wins so that
    # "FINANCE WORKS AND PROJECTS" does not settle for "FINANCE".
    prefix = [(code, name, cn) for code, name, cn in NORMALISED
              if cn and (n.startswith(cn) or cn.startswith(n))]
    if prefix:
        code, name, cn = max(prefix, key=lambda t: len(t[2]))
        return code, name, "prefix", 1.0

    # Word-set containment. A printed name that adds words ("ANIMAL HUSBANDRY
    # DAIRY DEVELOPMENT AND FISHERIES") or drops them ("EDUCATION" for School
    # Education) is the same department, and neither case is reachable by string
    # prefix or by edit distance -- 739 and 460 records respectively.
    bt = tokens(n)
    if bt:
        subset = [(code, name) for code, name, cn in NORMALISED
                  if tokens(cn) and (tokens(cn) <= bt or bt <= tokens(cn))]
        if len(subset) == 1:
            return subset[0][0], subset[0][1], "token_subset", 1.0
        if len(subset) > 1:
            same = [s for s in subset if base_code(s[0]) == base_code(dept_code)]
            if len(same) == 1:
                return same[0][0], same[0][1], "token_subset_by_code", 1.0
            # Genuinely ambiguous. Say so rather than pick one.
            return None, None, None, None

    best, best_score = None, 0.0
    for code, name, cn in NORMALISED:
        if not cn:
            continue
        score = difflib.SequenceMatcher(None, n, cn).ratio()
        if score > best_score:
            best, best_score = (code, name), score
    if best and best_score >= FUZZY_MIN:
        return best[0], best[1], "fuzzy", round(best_score, 3)
    return None, None, None, round(best_score, 3) if best else None


def resolve(rec):
    dept_code = rec.get("dept_code") or (rec.get("filename_parsed") or {}).get("dept_code")
    body_raw = (rec.get("department") or {}).get("full_department_name")

    # The filename declares the wing; `dept_code` does not. 2,270 files are named
    # `ICD01-P-IRRIGATION AND CAD PW WING-...` or `ENE01-E-ENERGY-...`, but the
    # parser's `dept_code` keeps only the base (`ICD01`, `ENE01`). The body text
    # recovers the wing on its own in 1,750 of those, but on 183 there is no
    # usable heading and the code path is all there is -- so those would fall back
    # to the parent department when the file itself says otherwise. Recover the
    # suffix here. `dept_code` is still copied through untouched; this is a second,
    # more precise field beside it, never a rewrite of the parser's output.
    filed_code = dept_code
    m = FILENAME_CODE_RE.match(rec.get("filename") or "")
    if m and m.group(1) in CANONICAL and base_code(m.group(1)) == dept_code:
        filed_code = m.group(1)

    # Sub-department is preserved exactly as printed, brackets stripped only.
    m = SUB_RE.search(body_raw or "")
    sub_department = ((m.group(1) if m.group(1) is not None else m.group(2)).strip()
                      if m else None) or None

    code_name = CANONICAL.get(filed_code)
    body_code, body_name, method, score = match_body(body_raw, dept_code)

    conflict = bool(
        body_code and dept_code and base_code(body_code) != base_code(dept_code)
    )

    if body_name:
        canonical, source = body_name, "body"
    elif code_name:
        canonical, source = code_name, "filename_code"
    else:
        canonical, source = None, None

    if not body_raw:
        status = "body_missing"
    elif body_code is None:
        status = "body_unresolved"
    elif conflict:
        status = "conflict"
    elif base_code(body_code) == body_code:
        status = "agree"
    else:
        status = "agree_wing"      # same department, older or narrower name

    return {
        "source_file": rec.get("source_file"),
        "filename": rec.get("filename"),
        "go_year": rec.get("go_year"),

        # `dept_code` is the parser's value, copied through unchanged.
        # `filed_code` is the same code with the wing suffix the filename carries
        # and the parser drops. They differ on 2,270 records.
        "dept_code": dept_code,
        "filed_code": filed_code,

        "canonical_department": canonical,
        "canonical_department_source": source,

        # Always present when the code is known -- the stable grouping key.
        "canonical_by_code": code_name,
        "canonical_by_code_id": filed_code if code_name else None,

        # What the page itself said, verbatim.
        "department_name_body": body_raw,
        "sub_department": sub_department,

        "canonical_by_body": body_name,
        "canonical_by_body_id": body_code,
        "body_match_method": method,
        "body_match_score": score,

        "conflict": conflict,
        "resolution": status,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus", nargs="?", default="out/corpus")
    ap.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    files = sorted(Path(args.corpus).glob("*.jsonl"))
    if not files:
        sys.exit(f"no .jsonl under {args.corpus}")

    stats = Counter()
    unresolved = Counter()
    conflicts = Counter()
    by_year = {}

    for f in files:
        with f.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                out = resolve(json.loads(line))
                stats["records"] += 1
                stats[f"resolution:{out['resolution']}"] += 1
                if out["canonical_department"]:
                    stats[f"source:{out['canonical_department_source']}"] += 1
                else:
                    stats["UNRESOLVED (no canonical at all)"] += 1
                if out["resolution"] == "body_unresolved":
                    unresolved[normalise(out["department_name_body"])] += 1
                if out["conflict"]:
                    conflicts[f"{out['dept_code']} filed -> body says "
                              f"{out['canonical_by_body_id']}"] += 1
                if out["body_match_method"]:
                    stats[f"body_match:{out['body_match_method']}"] += 1
                # Shard by the corpus file the record came from, NOT by
                # `go_year`. `out/corpus/<year>.jsonl` is named after the
                # on-disk download folder; `go_year` is parsed out of the page
                # body and is legitimately noisy -- 2021.jsonl alone holds 22
                # distinct go_year values including `202`, `2999` and `482`.
                # Sharding on it produced 71 files that no longer lined up with
                # the 19 corpus files they are supposed to partner. Same key,
                # same filename, one-to-one join.
                out["corpus_shard"] = f.stem
                by_year.setdefault(f.stem, []).append(out)
                if args.limit and stats["records"] >= args.limit:
                    break

    for k, v in sorted(stats.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {v:>7}  {k}")

    print("\n=== cross-filed (body department differs from the code it is filed under) ===")
    for k, v in conflicts.most_common(15):
        print(f"  {v:>6}  {k}")

    print("\n=== printed names that matched no canonical department ===")
    print(f"  {sum(unresolved.values())} records, {len(unresolved)} distinct")
    for k, v in unresolved.most_common(20):
        print(f"  {v:>6}  {k[:70]}")

    if args.apply:
        args.outdir.mkdir(parents=True, exist_ok=True)
        for year, rows in by_year.items():
            p = args.outdir / f"{year}.jsonl"
            tmp = p.with_suffix(".jsonl.partial")
            with tmp.open("w") as fh:
                for r in rows:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            tmp.replace(p)
        stale = [p for p in args.outdir.glob("*.jsonl") if p.stem not in by_year]
        for p in stale:
            p.unlink()
        print(f"\nWritten to {args.outdir}/ ({len(by_year)} year files)"
              + (f", removed {len(stale)} stale" if stale else ""))
    else:
        print("\nDRY RUN — nothing written. Re-run with --apply.")


if __name__ == "__main__":
    main()
