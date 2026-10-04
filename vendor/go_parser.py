#!/usr/bin/env python3
"""
GO parser — anchor-based extraction for Andhra Pradesh Government Order PDFs.

A GO is a fixed form. This does NOT understand the document — it finds eight
landmark anchors and takes whatever sits between them, then runs small
regexes inside each block. See CLAUDE.md for the full spec.

Six stages: normalise -> find anchors -> cut blocks -> regex inside each
block -> cross-validate against filename -> score and flag.
"""

import argparse
import difflib
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF


# ---------------------------------------------------------------------------
# Stage 1: normalise
# ---------------------------------------------------------------------------

# Matches letter-spaced runs like "O R D E R" or "A B S T R A C T" (3+ single
# uppercase letters separated by single spaces) so headings collapse back to
# real words before anything else looks at the text.
_LETTER_SPACED_RE = re.compile(r"\b(?:[A-Z]\s){2,}[A-Z]\b")


def _collapse_letter_spaced(line: str) -> str:
    return _LETTER_SPACED_RE.sub(lambda m: m.group(0).replace(" ", ""), line)


def normalize_text(raw: str) -> str:
    lines = [ln.rstrip() for ln in raw.split("\n")]
    lines = [_collapse_letter_spaced(ln) if ln.strip() else ln for ln in lines]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


# ---------------------------------------------------------------------------
# Stage 2: find anchors
# ---------------------------------------------------------------------------

SEPARATOR_RE = re.compile(r"^[\-=*&@#~_.\s]{2,}$")


def _fuzzy_heading(line: str, target: str, min_ratio: float = 0.78) -> bool:
    stripped = re.sub(r"[^A-Z]", "", line.upper())
    if not stripped or len(stripped) > len(target) + 4:
        return False
    return difflib.SequenceMatcher(None, stripped, target).ratio() >= min_ratio


@dataclass
class Anchor:
    name: str
    line_idx: int
    line_text: str


DEPT_LINE_RE = re.compile(r"\b(DEPARTMENT|DEPRTMENT|DEPATMENT|DEPARTMANT)\.?\s*$")
# The corpus spans two issuing governments (Andhra Pradesh, and Telangana for
# GOs issued after the 2014 bifurcation). This single pattern is used both to
# locate the gov_line anchor and to read the issuing government off that same
# matched line, so the two can never disagree. gov_line is a CRITICAL_ANCHOR:
# a pattern that only knew "ANDHRA PRADESH" would penalise every Telangana
# document -0.15 confidence and emit a false missing_anchor:gov_line warning,
# systematically misrouting an entire state's sub-corpus into review.
GOV_LINE_RE = re.compile(
    r"GOVERNMENT\s+OF\s+(?P<government>ANDHRA\s+PRA[DS]ESH|TELANGANA)",
    re.IGNORECASE,
)
GOV_CANON = {"ANDHRAPRADESH": "ANDHRA PRADESH", "ANDHRAPRASESH": "ANDHRA PRADESH", "TELANGANA": "TELANGANA"}
# AP GOs only ever carry two GO-type tokens in practice: "Ms" (Miscellaneous)
# and "Rt" (Routine). Both the anchor-detection pattern below and GO_NUM_RE
# further down must recognize both — an earlier version of this pattern only
# matched "M S", which silently failed to even locate the go_number anchor
# line on any Rt.-type GO, not just mis-extract its value. Shared here so the
# two can never drift back out of sync with each other.
GO_TYPE_ALT = r"(?:M\s*S|R\s*T)"
GO_ANCHOR_RE = r"G\.?\s*O\.?\s*[.,]?\s*" + GO_TYPE_ALT + r"\s*\.?\s*No\.?"
BY_ORDER_RE = re.compile(
    r"BY\s+ORDER\s+AND\s+IN\s+THE\s+NAME\s+OF\s+(?:THE\s+)?GOVERNOR",
    re.IGNORECASE,
)
# The heading that opens the operative section, alone on its own line. Anchored
# at both ends deliberately: "ORDER" occurs constantly inside prose ("orders are
# issued", "in supersession of the order"), and only a line that is nothing but
# the word is the heading.
ORDER_HEADING_RE = re.compile(r"ORDER\s*[:.\-]*\s*$", re.IGNORECASE)
# The same heading, tolerant of a glyph gap the PDF opened inside the word:
# "ORDE R:" and "O RDER:" both occur, where the spacing defeated
# _collapse_letter_spaced (which needs a longer run of single letters to fire).
# Used only by the unfloored pass 4 below, whose gate is what makes the looser
# match safe.
TOLERANT_ORDER_RE = re.compile(r"O\s*R\s*D\s*E\s*R\s*[:.\-]*\s*$", re.IGNORECASE)
# Boilerplate that closes the letter body ("SF/SCs" / "// FORWARDED BY ORDER //"
# — some scans render the slashes as backslashes — / "SECTION OFFICER"). Pages
# after this belong to an appended annexure/schedule, not the recipients list.
FOOTER_RE = re.compile(
    r"^(SF\s*/?\s*SCs?\.?$|[/\\]{2}\s*FORWARDED.*[/\\]{2}$|SECTION OFFICER\.?$)",
    re.IGNORECASE,
)


def find_anchors(lines: list[str]) -> dict[str, Anchor]:
    """Two passes, not one.

    Pass 1 resolves gov_line -> abstract -> dept_department. Pass 2 resolves
    everything downstream of dept_department (go_number, dated, read,
    separator, order, by_order, to).

    This must NOT be a single forward scan with a "not found yet, so assume
    it's absent" fallback: the abstract paragraph routinely cites another
    GO's number in passing (e.g. "Amendment to G.O.Ms.No.582, Revenue Dept
    ... - Orders Issued"), which sits *before* the real dept_department
    heading in the raw text. A one-pass scan can't yet know dept_department
    will appear later, so a "not found yet -> treat as absent, fall back"
    rule ends up anchoring go_number to that abstract mention instead of the
    GO's own number line. Only after the whole document has been searched
    for dept_department can go_number safely be constrained to "after it".

    Within a pass, every applicable pattern is checked for a line (no
    first-match-wins short circuit) — a GO commonly puts the go_number and
    the "Dated:" text on the same physical line.
    """
    anchors: dict[str, Anchor] = {}

    def set_first(name: str, idx: int, text: str):
        if name not in anchors:
            anchors[name] = Anchor(name, idx, text)

    # --- pass 1: gov_line, abstract, dept_department ---
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue

        if "gov_line" not in anchors and GOV_LINE_RE.search(stripped):
            set_first("gov_line", i, stripped)

        if (
            "abstract" not in anchors
            and ("gov_line" not in anchors or i > anchors["gov_line"].line_idx)
            and len(stripped) <= 14
            and _fuzzy_heading(stripped, "ABSTRACT")
        ):
            set_first("abstract", i, stripped)

        # Heading lines are printed in caps in every sample seen; matching
        # only the uppercase spelling (no IGNORECASE) keeps this from firing
        # on mixed-case abstract prose that happens to end in "...Department".
        if (
            "dept_department" not in anchors
            and "abstract" in anchors
            and i > anchors["abstract"].line_idx
            and len(stripped) <= 110
            and DEPT_LINE_RE.search(stripped)
            and not SEPARATOR_RE.match(stripped)
        ):
            set_first("dept_department", i, stripped)

    # --- pass 2: go_number onward, now that dept_department is fully resolved ---
    dept_idx = anchors["dept_department"].line_idx if "dept_department" in anchors else None
    abstract_idx = anchors["abstract"].line_idx if "abstract" in anchors else None

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue

        # A GO number mentioned inside the abstract's own descriptive text
        # must not be picked up as *this* GO's own number — require the real
        # dept heading line to have passed first. Only fall back to "after
        # the abstract" when dept_department genuinely doesn't exist anywhere
        # in the document (checked now, not mid-scan).
        if (
            "go_number" not in anchors
            and re.search(GO_ANCHOR_RE, stripped, re.IGNORECASE)
            and (
                (dept_idx is not None and i > dept_idx)
                or (dept_idx is None and abstract_idx is not None and i > abstract_idx)
            )
        ):
            set_first("go_number", i, stripped)

        if (
            "dated" not in anchors
            and "go_number" in anchors
            and i >= anchors["go_number"].line_idx
            # No trailing \b: some scans run "Dated" straight into the digits
            # with no space ("Dated28022023."), and digits are word characters
            # too, so a trailing boundary would never match there.
            and re.search(r"\bDated", stripped, re.IGNORECASE)
        ):
            set_first("dated", i, stripped)

        if (
            "read" not in anchors
            and "go_number" in anchors
            and i > anchors["go_number"].line_idx
            and re.match(r"Read\b", stripped, re.IGNORECASE)
        ):
            set_first("read", i, stripped)

        if "by_order" not in anchors and BY_ORDER_RE.search(stripped):
            set_first("by_order", i, stripped)

        if (
            "to" not in anchors
            and "by_order" in anchors
            and i > anchors["by_order"].line_idx
            and re.match(r"To\s*:?\s*$", stripped, re.IGNORECASE)
        ):
            set_first("to", i, stripped)

        if (
            "footer" not in anchors
            and "by_order" in anchors
            and i > anchors["by_order"].line_idx
            and FOOTER_RE.match(stripped)
        ):
            set_first("footer", i, stripped)

    # --- pass 3: separator and order, now that `read` is fully resolved ---
    #
    # These two used to be gated on `read`, which cost the corpus dearly. The
    # "Read the following:" preamble is optional — plenty of GOs run straight
    # from the header into "ORDER:" — but with the gate in place such a
    # document could never match its own ORDER line, so cut_blocks found no
    # start for order_block and threw the entire body away. It was silent:
    # 10,373 records carried empty order_text, and *not one* record in the
    # corpus had order_text without a `read` anchor.
    #
    # The floor falls back through the header anchors instead, so the match is
    # still forbidden from reaching above the document's own heading. Kept as
    # a separate pass because `read` may be found at a later line than a stray
    # earlier "ORDER" — resolving it first lets the real preamble still win.
    #
    # gov_line and -1 are the last two rungs, for the documents that have no
    # ABSTRACT heading at all. Those lose dept_department, and with it
    # go_number and read, to the same cascade — so without a rung below the
    # four header anchors their ORDER line is unreachable even when it is
    # printed plainly on the page. Reaching down this far is safe because
    # ORDER_HEADING_RE matches only a line that is *nothing but* the word:
    # the top of a GO is its government heading and its subject, never a bare
    # "ORDER", so there is nothing up there for it to hit by mistake.
    #
    # Strictly additive, by construction rather than by hope: these rungs are
    # consulted only when every anchor above them is missing, and in that case
    # the previous code set neither `order` nor `separator` at all.
    # The two do NOT share a floor. `separator` is a rule of dashes, and a GO's
    # masthead rules one across the top of the page; let that match from line 0
    # and cut_blocks would start the order block above the abstract and take
    # the whole document with it. So separator keeps the header-anchor floor,
    # which is what puts it below the masthead, and only `order` reaches lower.
    header_floor = next(
        (
            anchors[name].line_idx
            for name in ("read", "go_number", "dept_department", "abstract")
            if name in anchors
        ),
        None,
    )
    order_floor = header_floor if header_floor is not None else (
        anchors["gov_line"].line_idx if "gov_line" in anchors else -1
    )

    for i in range(order_floor + 1, len(lines)):
        stripped = lines[i].strip()
        if not stripped:
            continue
        if (
            "separator" not in anchors
            and header_floor is not None
            and i > header_floor
            and SEPARATOR_RE.match(stripped)
        ):
            set_first("separator", i, stripped)
        if "order" not in anchors and ORDER_HEADING_RE.match(stripped):
            set_first("order", i, stripped)

    # --- pass 4: the order heading on a page whose text layer is not in
    # reading order ---
    #
    # Every pass above assumes the stream runs down the page. PyMuPDF reads a
    # page in *visual* order, and an e-office PDF's blocks frequently are not
    # laid out top-to-bottom. REVENUE-MS-328-20_07_2023 comes out as: subject,
    # "G.0.Ms.No. 328", "GOVERNMENT OF ANDHRA PRADESH", "ABSTRACT", department,
    # "ORDER:", the references, "Dated 20.07.2023", body. Two things then go
    # wrong at once. go_number does not match line 6 — that "G.0." is printed
    # with a zero where the O belongs — so it matches a citation at line 50
    # instead, and header_floor becomes 50. The ORDER heading, plainly printed
    # at line 10, is below nothing and above everything the pass above will
    # look at. 115 of the 323 documents measured go further still and print
    # ORDER *ahead of their own masthead* in the stream, where no floor derived
    # from a header anchor can ever reach it.
    #
    # So this pass has no floor. It pays for that with a gate: it fires only
    # when `order`, `separator` and `read` are all absent — precisely the case
    # in which cut_blocks has no start for order_block and returns an empty
    # body. A document that already yields a block cannot be touched by this,
    # which is what makes it additive by construction rather than by hope. Of
    # the 267 documents in the manual-review cohort printing a bare heading,
    # 234 pass the gate and 232 of those then produce a block.
    #
    # A body cut this way is recorded as such. `order_unfloored` is a marker,
    # not an anchor — nothing looks it up — but it rides into anchors_found and
    # so into the corpus, which is the point: a block bounded without a floor,
    # on a page whose stream is out of order, sometimes opens on a stray "To" or
    # a repeated "ABSTRACT" that visually belongs to the masthead. Every
    # character of it is still the document's own, and a reader who wants to
    # know why a body starts oddly can see from the record that it was cut this
    # way rather than having to guess.
    if not {"order", "separator", "read"} & set(anchors):
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped and TOLERANT_ORDER_RE.match(stripped):
                set_first("order", i, stripped)
                set_first("order_unfloored", i, stripped)
                break

    return anchors


# ---------------------------------------------------------------------------
# Stage 3: cut blocks between anchors
# ---------------------------------------------------------------------------


def _slice(lines: list[str], start: Optional[int], end: Optional[int]) -> str:
    if start is None:
        start = 0
    if end is None:
        end = len(lines)
    return "\n".join(lines[start:end]).strip()


_BRACKETED_RE = re.compile(r"[(\[][^)\]]*[)\]]")
DEPT_HEADING_MAX_WRAP = 4


def _is_heading_cont(line: str) -> bool:
    """A wrapped piece of the department heading ("BACKWARD", "MUNICIPAL
    ADMINISTRATION & URBAN DEVELOPMENT (Vig.III-1)") versus the abstract prose
    that sits just above it ("...- Orders - Issued."). Headings are printed in
    caps, so any lowercase means prose — but the bracketed sub-department code
    is routinely mixed case ("(Vig.III)", "(Promotions)"), so brackets are
    excluded from that test rather than disqualifying the line.
    """
    return bool(re.search(r"[A-Z]", line)) and not re.search(r"[a-z]", _BRACKETED_RE.sub("", line))


def _dept_heading(lines: list[str], idx: int, stop_idx: Optional[int]) -> str:
    """The department heading wraps across printed lines often enough to
    matter: the anchor line can be the bare word "DEPARTMENT", or just
    "(PROMOTIONS) DEPARTMENT", with the actual name on the line(s) above
    ("BACKWARD" / "CLASSES" / "WELFARE" / "DEPARTMENT"). Taking only the
    anchor line yielded an empty department_name and a spurious
    filename_body_mismatch:department on ~473 records.

    Only walks back when the anchor line carries no name of its own, stops at
    the abstract anchor, a separator, or the first line that isn't
    heading-shaped, and caps the number of lines it will join. This rejoins
    text the document actually printed — it does not infer a name.
    """
    line = lines[idx].strip()
    core = DEPT_WORD_RE.sub("", DEPT_SUB_RE.sub("", line)).strip(" -.")
    if core:
        return line

    parts: list[str] = []
    j = idx - 1
    lower_bound = stop_idx if stop_idx is not None else -1
    while j > lower_bound and len(parts) < DEPT_HEADING_MAX_WRAP:
        prev = lines[j].strip()
        j -= 1
        if not prev:
            continue
        if SEPARATOR_RE.match(prev) or not _is_heading_cont(prev):
            break
        parts.insert(0, prev)

    return " ".join(parts + [line]) if parts else line


def cut_blocks(lines: list[str], anchors: dict[str, Anchor]) -> tuple[dict[str, str], list[str]]:
    """Cut the eight blocks. A block bounded by an anchor that wasn't found is
    left empty rather than guessed (e.g. defaulting to index 0 or end-of-file)
    — an unbounded slice has previously swallowed the entire document into
    the wrong block (a missing `by_order` anchor turned "signature_block"
    into the whole file, misreading the preamble as the signatory)."""
    warnings: list[str] = []

    def idx(name: str, offset: int = 0) -> Optional[int]:
        a = anchors.get(name)
        return None if a is None else a.line_idx + offset

    def bounded(name: str, start: Optional[int], end: Optional[int], cap: int = 400) -> str:
        if start is None or end is None:
            if start is not None:
                # No closing anchor found — cap the window instead of running
                # to end-of-file so one missing anchor can't swallow the doc.
                warnings.append(f"unbounded_block_capped:{name}")
                end = min(len(lines), start + cap)
            else:
                warnings.append(f"empty_block:{name}")
                return ""
        return _slice(lines, start, end)

    blocks = {
        "abstract_block": bounded("abstract_block", idx("abstract", 1), idx("dept_department")),
        "dept_block": _dept_heading(
            lines, anchors["dept_department"].line_idx, idx("abstract")
        )
        if "dept_department" in anchors
        else "",
        "go_meta_block": bounded(
            "go_meta_block", idx("dept_department", 1), idx("read") or idx("order"), cap=15
        ),
        "references_block": bounded(
            "references_block", idx("read", 1), idx("separator") or idx("order") or idx("by_order")
        ),
        "order_block": bounded(
            "order_block",
            idx("order", 1) if "order" in anchors else (idx("separator", 1) or idx("read", 1)),
            idx("by_order") or idx("to"),
        ),
        "signature_block": bounded("signature_block", idx("by_order", 1), idx("to"), cap=10),
        "recipients_block": bounded(
            "recipients_block", idx("to", 1), idx("footer") or len(lines)
        ),
    }
    return blocks, warnings


# ---------------------------------------------------------------------------
# Stage 4: small regex inside each block
# ---------------------------------------------------------------------------

MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

DATE_RE = re.compile(r"(\d{1,2})\s*[.\-/_]\s*(\d{1,2})\s*[.\-/_]\s*(\d{2,6})")
# Long-form dates, which older GOs (2008-2012 especially) use instead of the
# numeric form: "Dated: 23rd May, 2009.", "Dated the 8th day of October,
# 2010.", "Dated:7th March, 2012.", "Dated: 29TH October, 2012". The ordinal
# suffix, the "the", the "day of", and the spacing/comma placement all vary
# between documents, so each is independently optional here.
DATE_LONG_RE = re.compile(
    r"(\d{1,2})\s*(?:st|nd|rd|th)?\s*(?:day\s+of\s+)?\s*"
    r"(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\.?\s*,?\s*(\d{4})",
    re.IGNORECASE,
)
# Fallback for scans that drop all punctuation between day/month/year, e.g.
# "Dated28022023." with no separators at all.
DATE_COMPACT_RE = re.compile(r"Dated\s*:?\s*(\d{2})(\d{2})(\d{4})\b", re.IGNORECASE)
GO_NUM_RE = re.compile(
    r"G\.?\s*O\.?\s*[.,]?\s*(?P<go_type>" + GO_TYPE_ALT + r")\s*\.?\s*No\.?\s*[:.\-]?\s*"
    r"\(?(?P<go_number>\d{1,6}(?:[-/][A-Za-z])?)\)?",
    re.IGNORECASE,
)
# GO numbers can carry a letter suffix in the source ("118-A"); go_number is
# the plain leading integer for joins/sorting, go_number_raw preserves the
# suffix as printed. go_type is read directly off the same match ("Ms"/"Rt"),
# never guessed or defaulted from elsewhere.
GO_TYPE_CANON = {"MS": "MS", "RT": "RT"}
EOFFICE_RE = re.compile(r"e[-\s]?office|e[-\s]?file\s*No", re.IGNORECASE)


def _parse_date(raw_day: str, raw_month: str, raw_year: str) -> tuple[Optional[str], bool]:
    """Returns (iso_date_or_None, was_valid). Never guesses a corrected value."""
    year = raw_year
    if len(year) == 2:
        year = ("20" if int(year) < 50 else "19") + year
    if len(year) > 4:
        # e.g. the known typo "31.08.20206" -> reject, don't silently truncate
        return None, False
    try:
        d = date(int(year), int(raw_month), int(raw_day))
        return d.isoformat(), True
    except ValueError:
        return None, False


def extract_go_number(text: str) -> dict:
    m = GO_NUM_RE.search(text)
    if not m:
        return {"go_number": None, "go_number_raw": None, "go_type": None}
    raw = m.group("go_number")
    digits = re.match(r"\d+", raw)
    go_type_key = re.sub(r"\s+", "", m.group("go_type")).upper()
    return {
        "go_number": int(digits.group(0)) if digits else None,
        "go_number_raw": raw,
        "go_type": GO_TYPE_CANON.get(go_type_key),
    }


def extract_go_year(go_date: dict, fn: Optional[dict]) -> Optional[int]:
    """Prefer the body-extracted date; fall back to the filename's year only
    when the body date is missing or failed validation."""
    iso = go_date.get("iso")
    if iso:
        return int(iso[:4])
    if fn and fn.get("date_iso"):
        return int(fn["date_iso"][:4])
    return None


def is_eoffice(raw_text: str) -> bool:
    return bool(EOFFICE_RE.search(raw_text))


def extract_go_date(text: str) -> dict:
    m = DATE_RE.search(text)
    if m:
        day, month, year = m.groups()
        raw = m.group(0).strip()
    else:
        m = DATE_LONG_RE.search(text)
        if m:
            day, month_name, year = m.groups()
            month = MONTHS[month_name.lower().rstrip(".")]
            # Collapse the run-together / multi-space spacing variants so raw
            # reads back as the source printed it, minus the stray whitespace.
            raw = re.sub(r"\s+", " ", m.group(0).strip())
        else:
            m = DATE_COMPACT_RE.search(text)
            if not m:
                return {"raw": None, "iso": None, "valid": False}
            day, month, year = m.groups()
            # Group(0) here also swallows the "Dated" label the regex anchors
            # on ("Dated28022023") — raw should be just the date, in the same
            # unpunctuated style the source used, not the label plus the date.
            raw = "".join(m.groups())
    iso, valid = _parse_date(day, month, year)
    return {"raw": raw, "iso": iso, "valid": valid}


DEPT_WORD_RE = re.compile(r"DEPARTMENT|DEPRTMENT|DEPATMENT|DEPARTMANT", re.IGNORECASE)
# Matches a bracketed sub-department immediately followed by the DEPARTMENT
# suffix word at the end of the line, e.g. "(COOP.I) DEPARTMENT" at the tail
# of "AGRICULTURE & COOPERATION (COOP.I) DEPARTMENT".
DEPT_SUB_RE = re.compile(
    r"(?P<bracket>[(\[][^)\]]+[)\]])\s*(?P<suffix>" + DEPT_WORD_RE.pattern + r")\.?\s*$",
    re.IGNORECASE,
)


def extract_department(dept_line: str) -> dict:
    """Splits the department anchor line into three views, per project
    decision: nothing here is inferred beyond what's literally printed.

      - full_department_name : the line exactly as printed, sub-department
        bracket included, e.g. "AGRICULTURE & COOPERATION (COOP.I) DEPARTMENT"
      - department_name       : the same line with the bracketed
        sub-department removed but the trailing "DEPARTMENT" word kept,
        e.g. "AGRICULTURE & COOPERATION DEPARTMENT"
      - sub_department_name   : a raw bracket capture (parentheses included,
        exactly as printed), e.g. "(COOP.I)", or None when the line has no
        bracketed sub-department. This is a raw capture only — resolving a
        bracket code to the organizational unit it names is deferred to a
        later lookup/AI stage, never guessed here.
    """
    if not dept_line:
        return {"full_department_name": None, "department_name": None, "sub_department_name": None}
    full_name = dept_line.strip()
    m = DEPT_SUB_RE.search(full_name)
    if m:
        sub_department_name = m.group("bracket")
        department_name = full_name[: m.start("bracket")].strip(" -") + " " + m.group("suffix")
        department_name = re.sub(r"\s{2,}", " ", department_name).strip()
    else:
        sub_department_name = None
        department_name = full_name
    return {
        "full_department_name": full_name,
        "department_name": department_name,
        "sub_department_name": sub_department_name,
    }


REF_LINE_RE = re.compile(r"^\s*\(?(\d{1,2})[.\)]\s*(.+)$")
# Same two GO types as GO_NUM_RE (Ms/Rt) — a cited reference is just as likely
# to be a Rt.-type GO as the document's own number is. "Dept" alternation also
# covers "Deptt." (a confirmed real spelling in the sample data), and the
# department group is optional: a reference that omits the department
# entirely ("vide G.O.Ms.No.100 dt.29.03.2008") must still match on
# number+date rather than failing the whole reference and being silently
# dropped.
REF_GO_RE = re.compile(
    r"G\.{0,2}\s*O\.{0,2}\s*(?P<go_type>Ms|Rt)\.?\s*No\.?\s*(?P<go_number>\d+)\s*,?\s*"
    r"(?:(?P<department>.+?)\s*Dep(?:artment|tt|t)\.?,?\s*)?"
    r"(?:dt|dated)\.?:?\s*(?P<date_raw>[\d.\-/_]+)",
    re.IGNORECASE,
)


def extract_references(block: str) -> list[dict]:
    if not block:
        return []
    # Rejoin continuation lines (a ref item can wrap across several raw lines).
    # A wrapped date like "31.1.2018." at the start of a line also matches the
    # numbered-item pattern, so only accept a match as a new item if its number
    # is the next expected sequential value (1, 2, 3, ...) — otherwise treat it
    # as a continuation of the previous item.
    raw_items: list[str] = []
    expected_next = 1
    for line in block.split("\n"):
        m = REF_LINE_RE.match(line)
        if m and int(m.group(1)) == expected_next:
            raw_items.append(line.strip())
            expected_next += 1
        elif raw_items:
            raw_items[-1] += " " + line.strip()
        # else: leading noise before item 1, drop it

    refs = []
    for item in raw_items:
        m = REF_LINE_RE.match(item)
        seq, body = int(m.group(1)), m.group(2)
        go_m = REF_GO_RE.search(body)
        if go_m:
            dept = go_m.group("department")
            refs.append(
                {
                    "seq": seq,
                    "ref_kind": "go",
                    "go_type": GO_TYPE_CANON.get(go_m.group("go_type").upper()),
                    "go_number": int(go_m.group("go_number")),
                    "department": dept.strip(" ,") if dept else None,
                    "date_raw": go_m.group("date_raw"),
                    "raw_text": body,
                }
            )
        else:
            refs.append(
                {
                    "seq": seq,
                    "ref_kind": "letter" if re.search(r"\bLr\.?No|Letter\b", body, re.IGNORECASE) else "other",
                    "go_type": None,
                    "go_number": None,
                    "department": None,
                    "date_raw": None,
                    "raw_text": body,
                }
            )
    return refs


AMENDMENT_KEYWORDS = re.compile(
    r"\b(amend(?:ment|ed|ing)?|in\s+partial\s+modification|in\s+supersession|"
    r"corrigend[au]m)\b",
    re.IGNORECASE,
)


def detect_amendment(abstract_text: str, order_text: str, refs: list[dict]) -> dict:
    hit = AMENDMENT_KEYWORDS.search(abstract_text) or AMENDMENT_KEYWORDS.search(order_text)
    if not hit:
        return {"is_amendment": False, "amends_go_number": None, "keyword": None}
    first_go_ref = next((r for r in refs if r["ref_kind"] == "go"), None)
    return {
        "is_amendment": True,
        "amends_go_number": first_go_ref["go_number"] if first_go_ref else None,
        "keyword": hit.group(0),
    }


NOISE_LINE_RE = re.compile(
    r"^(SF\s*/?\s*SCs?\.?$|[/\\]{2}\s*FORWARDED.*[/\\]{2}$|SECTION OFFICER\.?$|Copy to:?$)",
    re.IGNORECASE,
)


# Headings that mark the start of appended matter rather than a continuation of
# the signatory's designation. Needed because on documents where FOOTER_RE never
# matched, the signature block runs to EOF — without this stop, an appended
# Gazette appendix gets absorbed wholesale into `designation` (observed on
# LAW01 - LAW-MS-12-22_04_2015.pdf).
SIG_STOP_RE = re.compile(
    r"^(APPENDIX|ANNEXURES?|SCHEDULE|NOTIFICATION|FORM\b|Copy\s+to|To\b|The\s+following)",
    re.IGNORECASE,
)
# Real designations wrap across at most two or three printed lines; anything
# beyond that is appended matter, not a job title.
SIG_DESIGNATION_MAX_LINES = 3


def extract_signature(block: str) -> dict:
    lines = [l.strip() for l in block.split("\n") if l.strip()]
    lines = [l for l in lines if not NOISE_LINE_RE.match(l)]
    name = lines[0] if len(lines) > 0 else None
    # Designations routinely wrap across more than one printed line
    # ("COMMISSIONER FOR DISASTER MANAGEMENT &" / "EX-OFFICIO PRINCIPAL
    # SECRETARY TO GOVERNMENT"). Taking only lines[1] silently truncated the
    # value mid-phrase with no warning — a loss of source text. Join the
    # continuation lines, but stop at an appended-matter heading and cap the
    # span, so an unbounded block can never swallow an entire appendix.
    tail = []
    for l in lines[1 : 1 + SIG_DESIGNATION_MAX_LINES]:
        if SIG_STOP_RE.match(l):
            break
        tail.append(l)
    designation = " ".join(tail) if tail else None
    return {"name": name, "designation": designation}


def extract_government(gov_line: Optional[str]) -> Optional[str]:
    """Reads the issuing government off the gov_line anchor text itself.
    Returns "ANDHRA PRADESH" or "TELANGANA" exactly as matched — never
    inferred from the filename, the year, or the department."""
    if not gov_line:
        return None
    m = GOV_LINE_RE.search(gov_line)
    if not m:
        return None
    key = re.sub(r"\s+", "", m.group("government")).upper()
    return GOV_CANON.get(key)


def extract_recipients(block: str) -> list[str]:
    out = []
    for line in block.split("\n"):
        line = line.strip()
        if not line or NOISE_LINE_RE.match(line):
            continue
        out.append(line)
    return out


# ---------------------------------------------------------------------------
# Table detector (deterministic FLAG, not an extractor)
#
# PyMuPDF's get_text() reads a page in visual order with no notion of "this
# is a table cell" — a grid collapses into a flat sequence of lines with row
# and column boundaries gone. There is no regex that can safely reconstruct
# arbitrary row/column pairings from that (a wrong guess here is silently
# wrong data, which this project's rules forbid). So this never attempts
# extraction — it only recognizes two structural fingerprints that real AP
# GO tables reliably leave behind, and sets a warning so a separately-scoped
# process (manual review, or an explicit LLM pass over just this subset) can
# pick the record up before other modules consume order_text.
# ---------------------------------------------------------------------------

# Column-index header row, e.g. "(1)" "(2)" "(3)" each alone on its own line —
# the standard AP GO convention for labeling table columns. Two or more in a
# row is a strong, low-false-positive signal (ordinary paragraph numbering in
# these documents is "1." / "2." with a trailing period, not "(1)").
TABLE_COLUMN_INDEX_RE = re.compile(r"^\(\d{1,2}\)\s*$")
TABLE_KEYWORD_RE = re.compile(r"\bTABLE\b", re.IGNORECASE)
# "...shall be as follows:-" / "as per the Table below:-" — the standard
# lead-in line immediately before a table in these documents.
TABLE_INTRO_RE = re.compile(r":-\s*$")


def detect_table(order_text: Optional[str]) -> bool:
    if not order_text:
        return False
    lines = order_text.split("\n")
    consecutive_idx = 0
    for line in lines:
        if TABLE_COLUMN_INDEX_RE.match(line.strip()):
            consecutive_idx += 1
            if consecutive_idx >= 2:
                return True
        else:
            consecutive_idx = 0
    if TABLE_KEYWORD_RE.search(order_text) and any(TABLE_INTRO_RE.search(l) for l in lines):
        return True
    return False


# A GO is usually "about" one person (an HBA sanction, a transfer, a
# promotion) and names them right in the abstract with an honorific: "Sri
# D.Ramulu", "Smt. S. Kanaka Durga Devi", "Dr./Kum./Mrs./Ms. <name>". The
# length cap on the name group is doing real work here — it's what keeps this
# from matching into an institution's name that happens to start with an
# honorific (e.g. "Dr. Marri Channa Reddy Human Resource Development
# Institute..."): a real person's name plus honorific is almost always under
# ~40 characters, an institute name chained onto it is not, so the lazy
# quantifier simply fails to find a valid stopping point and the whole match
# is (correctly) rejected rather than capturing garbage.
EMPLOYEE_NAME_RE = re.compile(
    r"\b(?:Sri|Smt\.?|Kum\.?|Dr\.?|Mrs\.?|Ms\.?)\s+([A-Z][A-Za-z.\s]{2,40}?)(?=\s*[,\-–—(]|$)"
)


def extract_employee_name(abstract_text: str, order_text: str) -> Optional[str]:
    """Single primary subject only (per project decision) — not a full name
    list. Looked up in the abstract first since that's where a GO states who
    it's about; multi-subject GOs (transfer/promotion panels) typically name
    people only in a body table, not the abstract, so this correctly returns
    None for those rather than guessing which one is "primary"."""
    if not abstract_text:
        return None
    m = EMPLOYEE_NAME_RE.search(abstract_text)
    if not m:
        return None
    # \s in the name group also matches the newline where a block's original
    # lines got joined — a name that happens to wrap mid-line in the source
    # PDF must not surface with a raw "\n" in it.
    return re.sub(r"\s+", " ", m.group(0)).strip(" .")


def count_named_subjects(text: str) -> int:
    """Rough count of honorific-prefixed names in a block, used only to flag
    likely multi-subject GOs (panels, transfer lists) — not itself a field."""
    if not text:
        return 0
    return len(EMPLOYEE_NAME_RE.findall(text))


GO_TYPE_DISPLAY = {"MS": "Ms", "RT": "Rt"}


def build_go_citation_raw(
    go_number_raw: Optional[str], go_type: Optional[str], department: dict, go_date: dict
) -> Optional[str]:
    """Reconstructs this GO's own citation in the same convention already
    used to parse *other* GOs' references to it elsewhere in the corpus (see
    REF_GO_RE: "G.O.Ms.No.<num>, <full dept line>, dated <date>") — built
    only from fields already extracted from this document, nothing invented.
    Uses full_department_name (the department line exactly as printed,
    bracketed sub-department included) rather than reassembling name +
    sub-department, so this can never drift out of sync with what the
    document actually says."""
    full_dept = department.get("full_department_name")
    go_type_display = GO_TYPE_DISPLAY.get(go_type)
    if not go_number_raw or not go_type_display or not full_dept or not go_date.get("raw"):
        return None
    return f"G.O.{go_type_display}.No.{go_number_raw}, {full_dept}, dt.{go_date['raw']}"


# ---------------------------------------------------------------------------
# Stage 5: cross-validate against filename
# ---------------------------------------------------------------------------

FILENAME_RE = re.compile(
    r"^(?P<dept_code>[A-Z]{2,6}\d{2})(?:-(?P<letter>[A-Z]))?\s*-\s*"
    r"(?P<dept_name>.+?)-(?P<go_type>MS|RT|P|D)-(?P<go_number>\d+(?:-[A-Za-z])?)-"
    r"(?P<day>\d{2})_(?P<month>\d{2})_(?P<year>\d{4})\.pdf$",
    re.IGNORECASE,
)


def parse_filename(filename: str) -> Optional[dict]:
    m = FILENAME_RE.match(filename)
    if not m:
        return None
    gd = m.groupdict()
    iso, valid = _parse_date(gd["day"], gd["month"], gd["year"])
    go_number_digits = re.match(r"\d+", gd["go_number"])
    return {
        "dept_code": gd["dept_code"],
        "letter": gd["letter"],
        "dept_name": gd["dept_name"].strip(),
        "go_type": gd["go_type"].upper(),
        "go_number": int(go_number_digits.group(0)) if go_number_digits else None,
        "go_number_raw": gd["go_number"],
        "date_iso": iso,
        "date_valid": valid,
    }


def _name_similarity(a: str, b: str) -> float:
    norm = lambda s: re.sub(r"[^A-Z]", "", s.upper())
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def cross_validate(fields: dict, fn: Optional[dict], go_type: Optional[str]) -> list[str]:
    warnings = []
    if fn is None:
        warnings.append("filename_unparseable")
        return warnings

    body_go_number = fields["go_meta"]["go_number"]
    if body_go_number is not None and body_go_number != fn["go_number"]:
        warnings.append(
            f"filename_body_mismatch:go_number(body={body_go_number},filename={fn['go_number']})"
        )

    if go_type is not None and fn.get("go_type") is not None and go_type != fn["go_type"]:
        warnings.append(
            f"filename_body_mismatch:go_type(body={go_type},filename={fn['go_type']})"
        )

    body_date_iso = fields["go_meta"]["go_date"]["iso"]
    if body_date_iso is not None and fn["date_iso"] is not None and body_date_iso != fn["date_iso"]:
        warnings.append(
            f"filename_body_mismatch:go_date(body={body_date_iso},filename={fn['date_iso']})"
        )

    body_dept = fields["department"]["department_name"]
    if body_dept:
        # Filenames encode the department name without the trailing
        # "...DEPARTMENT" word or the bracketed sub-department, so strip both
        # before comparing — otherwise a genuinely correct match would read
        # as a mismatch purely because of text the filename never carried.
        body_dept_core = DEPT_WORD_RE.sub("", body_dept).strip(" -")
        if _name_similarity(body_dept_core, fn["dept_name"]) < 0.55:
            warnings.append(
                f"filename_body_mismatch:department(body='{body_dept}',filename='{fn['dept_name']}')"
            )

    return warnings


# ---------------------------------------------------------------------------
# Stage 6: score and flag
# ---------------------------------------------------------------------------

CRITICAL_ANCHORS = ["gov_line", "abstract", "dept_department", "go_number", "dated"]


def score(anchors: dict, fields: dict, warnings: list[str], text_quality: float) -> tuple[float, list[str]]:
    confidence = 1.0
    for name in CRITICAL_ANCHORS:
        if name not in anchors:
            confidence -= 0.15
            warnings.append(f"missing_anchor:{name}")
    if "order" not in anchors:
        confidence -= 0.05
        warnings.append("missing_anchor:order")
    go_date = fields["go_meta"]["go_date"]
    if go_date["raw"] and not go_date["valid"]:
        confidence -= 0.15
        warnings.append("invalid_go_date")
    elif go_date["raw"] is None and "dated" in anchors:
        # The date line was located but no pattern matched anything on it.
        # Without this the record carried a null go_date, an empty warnings
        # list and confidence 1.0 — silent data loss, which the project's
        # "fail loudly, never guess" rule forbids. A missing `dated` anchor
        # is already penalised as a CRITICAL_ANCHOR, so only the
        # anchored-but-unparsed case is handled here.
        confidence -= 0.15
        warnings.append("date_unparsed")
    if fields["go_meta"]["go_number"] is None and "go_number" in anchors:
        # Same silent-loss shape as date_unparsed above: the GO-number line was
        # located but GO_NUM_RE matched nothing on it, leaving a null go_number
        # on a record that still scored 1.0 with an empty warnings list. The
        # filename cross-check cannot catch this one either — it is guarded on
        # `body_go_number is not None`, so a null is never compared to anything.
        # A missing `go_number` anchor is already penalised as a CRITICAL_ANCHOR,
        # so only the anchored-but-unparsed case is handled here.
        confidence -= 0.15
        warnings.append("go_number_unparsed")
    for w in warnings:
        if w.startswith("filename_body_mismatch"):
            confidence -= 0.2
    if text_quality < 0.3:
        confidence -= 0.3
    confidence = max(0.0, min(1.0, round(confidence, 2)))
    return confidence, warnings


def text_quality_score(raw_text: str, page_count: int) -> float:
    if page_count == 0:
        return 0.0
    chars_per_page = len(raw_text) / page_count
    return round(min(1.0, chars_per_page / 900), 2)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _default_fields() -> dict:
    """Canonical shape for every parse_pdf() return value.

    Every record — whether the PDF opened cleanly, had no text layer, or
    failed to open at all — must carry the same set of keys. Without this,
    downstream consumers (a DB loader with fixed columns, pandas, jq with a
    template) hit KeyErrors or silently-shifted columns on the ~1-in-20 files
    that bail out early. A "failed to parse" record should look like a
    mostly-null row of the same shape, not a truncated dict with different
    keys entirely.
    """
    return {
        "page_count": None,
        "text_quality": 0.0,
        "filename_parsed": None,
        "government": None,
        "abstract": None,
        "department": None,
        "go_meta": {
            "go_number": None,
            "go_number_raw": None,
            "go_date": {"raw": None, "iso": None, "valid": False},
        },
        "order_text": None,
        "references": [],
        "amendment": {"is_amendment": None, "amends_go_number": None, "keyword": None},
        "signature": {"name": None, "designation": None},
        "recipients": [],
        "anchors_found": [],
        "go_type": "OTHER",
        "go_year": None,
        "dept_code": None,
        "is_eoffice": None,
        "employee_name": None,
        "go_citation_raw": None,
        "apgo_category": None,
        "apgo_amendment_type": None,
        "has_table": None,
        "warnings": [],
        "extraction_confidence": 0.0,
    }


def _filename_fallback_fields(fn: Optional[dict]) -> dict:
    """Best-effort fields derived only from the filename, used when the body
    text can't be read at all (open failure or no text layer). Always paired
    with a fields_from_filename_only_unverified warning by the caller so
    downstream consumers know these are unverified guesses, not
    body-extracted facts.
    """
    fallback_go_date = {"raw": None, "iso": fn["date_iso"] if fn else None, "valid": bool(fn and fn["date_valid"])}
    return {
        "department": fn
        and {
            "full_department_name": None,
            "department_name": fn["dept_name"],
            "sub_department_name": None,
        },
        "go_meta": {
            "go_number": fn["go_number"] if fn else None,
            "go_number_raw": fn["go_number_raw"] if fn else None,
            "go_date": fallback_go_date,
        },
        "go_type": fn["go_type"] if fn else "OTHER",
        "go_year": extract_go_year(fallback_go_date, fn),
        "dept_code": fn["dept_code"] if fn else None,
    }


def parse_pdf(path: Path) -> dict:
    result: dict = {"source_file": str(path), "filename": path.name, **_default_fields()}

    try:
        doc = fitz.open(path)
        raw_text = "\n".join(p.get_text() for p in doc)
        page_count = len(doc)
    except Exception as e:
        warnings = [f"pdf_open_failed:{e}"]
        fn = parse_filename(path.name)
        result["filename_parsed"] = fn
        result.update(_filename_fallback_fields(fn))
        if fn:
            warnings.append("fields_from_filename_only_unverified")
        result["warnings"] = warnings
        return result

    return parse_text(path, raw_text, page_count)


def parse_text(
    path: Path, raw_text: str, page_count: int, extra_warnings: Optional[list] = None
) -> dict:
    """Parse already-extracted text. Split out of parse_pdf() so text recovered
    from a non-PDF source can go through the identical pipeline and produce a
    record of the identical shape — see recover_doc.py, which converts the
    ~4,000 MS Word files in this corpus that were saved with a .pdf extension.

    `extra_warnings` is carried onto the record so a converted document stays
    distinguishable from one read natively out of a PDF.
    """
    result: dict = {"source_file": str(path), "filename": path.name, **_default_fields()}
    warnings: list[str] = list(extra_warnings or [])

    tq = text_quality_score(raw_text, page_count)
    result["page_count"] = page_count
    result["text_quality"] = tq

    fn = parse_filename(path.name)
    result["filename_parsed"] = fn

    if tq == 0.0 or len(raw_text.strip()) < 20:
        warnings.append("no_text_layer")
        result.update(_filename_fallback_fields(fn))
        if fn:
            warnings.append("fields_from_filename_only_unverified")
        result["warnings"] = warnings
        return result

    normalized = normalize_text(raw_text)
    lines = normalized.split("\n")
    anchors = find_anchors(lines)
    blocks, block_warnings = cut_blocks(lines, anchors)
    warnings.extend(block_warnings)

    government = extract_government(anchors["gov_line"].line_text if "gov_line" in anchors else None)
    department = extract_department(blocks["dept_block"])
    # Prefer the exact anchor line — a generic block-wide search can still
    # pick up a GO number mentioned in a reference within the same block.
    go_number_src = anchors["go_number"].line_text if "go_number" in anchors else blocks["go_meta_block"]
    go_date_src = anchors["dated"].line_text if "dated" in anchors else blocks["go_meta_block"]
    go_num_info = extract_go_number(go_number_src)
    go_date = extract_go_date(go_date_src)
    references = extract_references(blocks["references_block"])
    amendment = detect_amendment(blocks["abstract_block"], blocks["order_block"], references)
    signature = extract_signature(blocks["signature_block"])
    recipients = extract_recipients(blocks["recipients_block"])
    employee_name = extract_employee_name(blocks["abstract_block"], blocks["order_block"])

    fields = {
        "government": government,
        "abstract": blocks["abstract_block"] or None,
        "department": department,
        "go_meta": {
            "go_number": go_num_info["go_number"],
            "go_number_raw": go_num_info["go_number_raw"],
            "go_date": go_date,
        },
        "order_text": blocks["order_block"] or None,
        "references": references,
        "amendment": amendment,
        "signature": signature,
        "recipients": recipients,
    }

    # Body-extracted go_type (read directly off the same GO_NUM_RE match as
    # go_number, see extract_go_number) takes priority over the filename's;
    # the filename is only a fallback for documents whose go_number anchor
    # couldn't be matched in the body at all.
    resolved_go_type = go_num_info.get("go_type") or (fn["go_type"] if fn else None) or "OTHER"

    warnings.extend(cross_validate(fields, fn, resolved_go_type))
    if not blocks["order_block"]:
        warnings.append("empty_order_block")

    # Flag-only, never extracted (see detect_table docstring): downstream
    # modules that consume order_text need to know when its table content
    # has been flattened into unstructured lines, so they can route this
    # subset to manual review or a separately-scoped AI extraction pass
    # instead of trusting order_text as-is.
    has_table = detect_table(blocks["order_block"])
    if has_table:
        warnings.append("table_detected_in_order_text")

    # A GO with more than one honorific-prefixed name mentioned across the
    # abstract+order is likely a multi-subject document (transfer list,
    # promotion panel) — employee_name intentionally stays a single value
    # (per project decision), so flag rather than silently pick one.
    if count_named_subjects((blocks["abstract_block"] or "") + " " + (blocks["order_block"] or "")) > 1:
        warnings.append("multi_subject_go")

    confidence, warnings = score(anchors, fields, warnings, tq)

    result.update(fields)
    result["anchors_found"] = sorted(anchors.keys())
    result["go_type"] = resolved_go_type
    result["go_year"] = extract_go_year(go_date, fn)
    result["dept_code"] = fn["dept_code"] if fn else None
    result["is_eoffice"] = is_eoffice(raw_text)
    result["employee_name"] = employee_name
    result["go_citation_raw"] = build_go_citation_raw(
        go_num_info["go_number_raw"], go_num_info.get("go_type"), department, go_date
    )
    # Deferred by explicit decision: these are semantic/subject classification,
    # not anchor-based extraction, so they stay null until a separate,
    # scoped classification stage is designed (see conversation record).
    result["apgo_category"] = None
    result["apgo_amendment_type"] = None
    result["has_table"] = has_table
    result["warnings"] = warnings
    result["extraction_confidence"] = confidence
    return result


def iter_pdfs(root: Path):
    yield from sorted(root.rglob("*.pdf"))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("inputs", nargs="+", help="PDF files, or directories to walk")
    ap.add_argument("-o", "--out", type=Path, help="Write JSONL output here")
    ap.add_argument("--pretty", action="store_true", help="Print human-readable summaries to stdout")
    args = ap.parse_args()

    paths: list[Path] = []
    for inp in args.inputs:
        p = Path(inp)
        if p.is_dir():
            paths.extend(iter_pdfs(p))
        else:
            paths.append(p)

    out_f = args.out.open("w") if args.out else None
    for p in paths:
        rec = parse_pdf(p)
        if out_f:
            out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if args.pretty:
            print_summary(rec)
    if out_f:
        out_f.close()


def print_summary(rec: dict):
    print("=" * 100)
    print(f"FILE: {rec['filename']}")
    fn = rec.get("filename_parsed")
    if fn:
        print(f"  filename says : dept={fn['dept_code']} '{fn['dept_name']}' | type={fn.get('go_type')} | go_number={fn['go_number']} | date={fn['date_iso']}")
    gm = rec.get("go_meta", {})
    if gm:
        gd = gm.get("go_date", {})
        print(f"  body says     : go_number={gm.get('go_number')} (raw={gm.get('go_number_raw')}) | date={gd.get('iso')} (raw='{gd.get('raw')}', valid={gd.get('valid')})")
    print(f"  government    : {rec.get('government')}")
    print(f"  go_type       : {rec.get('go_type')}  |  go_year: {rec.get('go_year')}  |  dept_code: {rec.get('dept_code')}  |  is_eoffice: {rec.get('is_eoffice')}")
    dept = rec.get("department")
    if dept:
        print(f"  department    : {dept.get('department_name')} | sub_department={dept.get('sub_department_name')}")
        if dept.get("full_department_name") and dept.get("full_department_name") != dept.get("department_name"):
            print(f"  full_dept_name: {dept.get('full_department_name')}")
    print(f"  employee_name : {rec.get('employee_name')}")
    print(f"  citation_raw  : {rec.get('go_citation_raw')}")
    if rec.get("abstract"):
        abs_short = re.sub(r"\s+", " ", rec["abstract"])[:160]
        print(f"  abstract      : {abs_short}...")
    refs = rec.get("references") or []
    print(f"  references    : {len(refs)} found")
    for r in refs[:3]:
        print(f"    [{r['seq']}] {r['ref_kind']:6s} go_type={r.get('go_type')} go_number={r.get('go_number')} raw='{r['raw_text'][:80]}'")
    am = rec.get("amendment") or {}
    if am.get("is_amendment"):
        print(f"  amendment     : YES (amends GO {am.get('amends_go_number')}, keyword='{am.get('keyword')}')")
    if rec.get("has_table"):
        print("  has_table     : YES — order_text contains a flattened table, route to AI/manual review")
    sig = rec.get("signature") or {}
    print(f"  signed by     : {sig.get('name')} / {sig.get('designation')}")
    print(f"  recipients    : {len(rec.get('recipients') or [])} lines")
    print(f"  text_quality  : {rec.get('text_quality')}  |  extraction_confidence: {rec.get('extraction_confidence')}")
    warnings = rec.get("warnings") or []
    if warnings:
        print(f"  WARNINGS      : {warnings}")
    else:
        print("  WARNINGS      : none")


if __name__ == "__main__":
    main()
