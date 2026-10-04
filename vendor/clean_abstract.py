"""
Strip the typesetting furniture out of an abstract without touching its words.

The GO masthead rules a line of dashes or equals signs under the abstract, and
sometimes above it too. PyMuPDF reads those rules as text, so 47% of the
abstracts in this corpus carry at least one line that is nothing but `-----`,
and 56% end mid-rule. Left in, they are indexed, shown in search results, and
read as if the document said them.

What this removes is only ever punctuation the printer drew:

    separator-only lines            "-------------------"
    leading and trailing rules      "...Orders Issued. ------"
    internal rules between words    "Births and Deaths --- Orders"  ->  " – "
    dot leaders                     "Subject ...... Orders"
    soft line wrapping              one abstract, one paragraph
    doubled spaces                  from the PDF's own justification
    control characters              NUL and friends, which Postgres rejects

What it must never touch, and is tested not to:

    single hyphens in compound words        non-teaching, sub-ordinates
    en dashes, which are the real separator PUBLIC SERVICES – Sanction – Issued
    GO numbers, dates, Acts, Rules, names   G.O.Ms.No.101, 22.11.2019, Rule 9(2)

Line wrapping is the subtle one. A hyphen at end-of-line in this corpus is
almost always a real hyphen in a compound word that happened to fall at the
margin ("non-\\nteaching"), not a typesetter's soft hyphen — so the newline is
closed up and *the hyphen is kept*. Dropping it would silently coin new words.

No model is called and no word is rewritten: every character in the output
appears in the input.
"""

from __future__ import annotations

import re
import unicodedata

# The rules the printer draws. ASCII only, and three or more in a row — a lone
# hyphen is a word's own punctuation and an en dash is the corpus's real
# separator, so neither can match here.
_RULE_CHARS = r"\-=_~*–—"
_SEPARATOR_ONLY_LINE = re.compile(rf"^[\s{_RULE_CHARS}]*[{_RULE_CHARS}]{{3,}}[\s{_RULE_CHARS}]*$")
_LEADING_RULE = re.compile(rf"^[\s{_RULE_CHARS}]*[\-=_~*]{{3,}}[\s{_RULE_CHARS}]*")
_TRAILING_RULE = re.compile(rf"[\s{_RULE_CHARS}]*[\-=_~*]{{3,}}[\s{_RULE_CHARS}]*$")
_INTERNAL_RULE = re.compile(r"\s*[\-=_~*]{3,}\s*")
_DOT_LEADER = re.compile(r"\s*\.{4,}\s*")
_SPACES = re.compile(r"[ \t ]+")

# A page header that bled into the abstract mid-sentence. The tell is a GO
# citation whose leading "G" was clipped by the crop box. Flagged, never
# repaired: a genuine abstract may legitimately cite another GO, and telling
# the two apart needs the document, not a regex.
_HEADER_BLEED = re.compile(r"(?<!G)\.O\.Ms\.No", re.IGNORECASE)
# The first character lost to the same crop ("UBLIC SERVICES", "OVERNMENT").
_CLIPPED_OPENING = re.compile(
    r"^\s*(UBLIC\s+SERVICES|OVERNMENT|BSTRACT|EVENUE|INANCE)\b", re.IGNORECASE
)

MIN_USEFUL_CHARS = 3

# Control characters the PDF text layer sometimes emits. NUL is the one that
# matters — Postgres rejects it in a text column outright — but none of these
# are content, and a stray \x0c (form feed) or \x07 would only ever show up in
# a search result as a box glyph. \n and \t are kept: the first carries the
# line structure this module reads, the second is collapsed later as spacing.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

# Typographic ligatures, which PDF text layers emit as single code points and
# no tokeniser will ever match against a typed query ("Oﬃcers" is one glyph and
# does not contain "ffi"). Expanded by name rather than by NFKC, because NFKC
# also decomposes vulgar fractions — and this corpus measures land in them, so
# it would rewrite "Ac.0.14½ cents" as "Ac.0.141⁄2 cents" and change what the
# document says about the size of somebody's land.
_LIGATURES = str.maketrans(
    {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"}
)


def clean_abstract(raw: str | None) -> tuple[str | None, str]:
    """Return (cleaned_abstract, status).

    Statuses:
        UNCHANGED            nothing needed removing
        CLEANED              furniture removed, words untouched
        EMPTY_AFTER_CLEAN    the abstract was nothing but rules
        NEEDS_MANUAL_REVIEW  cleaned, but a page header appears to have bled
                             into the text and only a human should cut it out
    """
    if raw is None:
        return None, "EMPTY_AFTER_CLEAN"

    # NUL bytes come out of some PDF text layers, and Postgres refuses them
    # outright in a text column. They carry no meaning — drop them, and the
    # other C0 controls with them, before anything else reads the string.
    # The untouched original is kept so the UNCHANGED verdict below still
    # compares against what the PDF actually gave us.
    original = raw
    raw = _CONTROL_CHARS.sub("", raw)

    text = unicodedata.normalize("NFC", raw).translate(_LIGATURES).replace(" ", " ")

    # 1. drop lines that are nothing but a rule
    lines = [ln for ln in text.split("\n") if not _SEPARATOR_ONLY_LINE.match(ln)]

    # 2. unwrap. A trailing hyphen keeps its hyphen and loses only the newline.
    out: list[str] = []
    for ln in lines:
        stripped = ln.strip()
        if not stripped:
            continue
        if out and re.search(r"[A-Za-z]-$", out[-1]) and re.match(r"[a-z]", stripped):
            out[-1] = out[-1] + stripped
        else:
            out.append(stripped)
    text = " ".join(out)

    # 3. rules that survived because they shared a line with real words
    text = _LEADING_RULE.sub("", text)
    text = _TRAILING_RULE.sub("", text)
    text = _INTERNAL_RULE.sub(" – ", text)
    text = _DOT_LEADER.sub(" ", text)

    # 4. the PDF's own justification spacing
    text = _SPACES.sub(" ", text).strip()
    text = re.sub(r"\s+([,.;:])", r"\1", text)
    text = re.sub(r"(\s*–\s*)+$", "", text).strip()

    if len(text) < MIN_USEFUL_CHARS:
        return None, "EMPTY_AFTER_CLEAN"

    if _HEADER_BLEED.search(text) or _CLIPPED_OPENING.match(text):
        return text, "NEEDS_MANUAL_REVIEW"

    return text, ("UNCHANGED" if text == original.strip() else "CLEANED")
