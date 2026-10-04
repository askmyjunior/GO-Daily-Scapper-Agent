#!/usr/bin/env python3
"""Read the GOIR listing over plain HTTP. No browser.

── WHY NO PLAYWRIGHT ─────────────────────────────────────────────────────────

The original bulk harvest needed a browser because its pagination control is
windowed and unreliable at depth -- 3,152 rows over 31 pages, going wrong
silently. A DAILY window is one or two pages, so none of that applies, and the
site turns out to be an ordinary ASP.NET WebForms postback: carry __VIEWSTATE
and the session cookie, POST the form, read the table. Measured 2026-10-04:
one POST returned 70 rows and `Found: 70` for RT on 01-07-2026.

That is what lets this run anywhere -- a GitHub Actions runner, a container,
anything with Python -- instead of only on a machine with a browser installed.

── THE RULE THIS FILE EXISTS TO ENFORCE ──────────────────────────────────────

The portal prints its own total. An earlier harvest reported "ALL 222 MONTHS
SECURED" and 420,921 rows while missing ~27,000, with ZERO duplicates -- which
looked like proof of correctness and was proof of the bug, because nothing was
re-read since whole pages were never visited.

So every window is checked against `lblCount` and a short read RAISES. A daily
agent that quietly records 60 of 70 orders is worse than one that fails, because
nobody goes looking for the ten that are missing.
"""
from __future__ import annotations

import datetime as dt
import gzip
import html as htmllib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://goir.ap.gov.in/"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

#: DDLGoType. -1 is both; the portal's own numbering, not ours.
GO_TYPE = {"MS": "1", "RT": "2", "ALL": "-1"}


class ShortRead(Exception):
    """The portal said N rows and gave fewer. Never swallowed."""


def _relay_conf() -> tuple[str, str] | None:
    """(url, token) when the portal must be reached through the Mumbai relay.

    Read at call time, not import time, so the secrets have already been
    tidied (pipeline.TIDIED) by whichever script imported this module first.
    """
    url = os.environ.get("GOIR_RELAY_URL", "").strip()
    token = os.environ.get("GOIR_RELAY_TOKEN", "").strip()
    return (url, token) if url and token else None


def _via_relay(conf: tuple[str, str], method: str, path: str, data: bytes | None,
               cookie: str | None, timeout: int) -> tuple[bytes, str | None]:
    """One portal request made from Mumbai (relay/main.py).

    The portal answers only connections from India, and the daily sync runs
    in the US. The relay returns the portal's body untouched, its status in
    x-goir-status and its session cookie in x-goir-set-cookie.
    """
    url, token = conf
    payload = json.dumps({"method": method, "path": path,
                          "body": data.decode() if data is not None else None,
                          "cookie": cookie}).encode()
    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json", "x-relay-token": token})
    with urllib.request.urlopen(req, timeout=timeout + 15) as resp:
        body = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        status = int(resp.headers.get("x-goir-status") or 0)
        if status >= 400:
            raise urllib.error.HTTPError(path, status, f"portal said {status} (via relay)",
                                         resp.headers, None)
        return body, resp.headers.get("x-goir-set-cookie")


def _fetch(url: str, data: bytes | None = None, cookie: str | None = None,
           timeout: int = 90) -> tuple[str, str | None]:
    conf = _relay_conf()
    if conf:
        assert url == BASE, f"only the listing page goes through the relay, not {url}"
        body, set_cookie = _via_relay(conf, "POST" if data is not None else "GET", "/",
                                      data, cookie, timeout)
        return body.decode("utf8", "replace"), set_cookie
    headers = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"}
    if cookie:
        headers["Cookie"] = cookie
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return raw.decode("utf8", "replace"), resp.headers.get("Set-Cookie")


def _hidden(page: str) -> dict[str, str]:
    """__VIEWSTATE and friends, carried forward verbatim.

    ASP.NET rejects a postback whose ViewState it did not issue, and it is
    opaque -- it must be echoed exactly, not reconstructed.
    """
    return {
        htmllib.unescape(k): htmllib.unescape(v)
        for k, v in re.findall(
            r'<input type="hidden" name="([^"]+)"[^>]*value="([^"]*)"', page)
    }


_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_GID = re.compile(r"downloadFile\(\s*['\"]?(\d+)['\"]?\s*,\s*['\"]([ET])['\"]", re.I)


def _text(cell: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(_TAG.sub(" ", cell))).strip()


def departments() -> list[tuple[str, str]]:
    """The DDLDeptname options, excluding ---ALL---."""
    page, _ = _fetch(BASE)
    sel = re.search(
        r'name="ctl00\$ContentPlaceHolder1\$DDLDeptname".*?</select>', page, re.S)
    out = []
    for value, label in re.findall(
            r'<option[^>]*value="([^"]*)"[^>]*>(.*?)</option>', sel.group(0) if sel else "", re.S):
        if value and value != "-1":
            out.append((value, _text(label)))
    return out


def listing_complete(day: dt.date, go_type: str = "ALL", max_pages: int = 12) -> list[dict]:
    """Every order on a day, however many pages it takes.

    The page size maxes at 100 and a busy day exceeds it -- 14 July 2026 has
    191 RT orders. An earlier bulk harvest lost ~27,000 rows chasing this
    pager, but that was at THIRTY-ONE pages, where the control is windowed and
    the next number may not be rendered at all. A day is two or three pages,
    where every link is visible.

    What makes it safe either way is not the pager, it is the portal's own
    `Found:` count: pages are collected until the count is satisfied, and a
    short result RAISES rather than being recorded. Splitting by department
    was tried first and is not enough -- one department had 121 rows on that
    same day.
    """
    page, set_cookie = _fetch(BASE)
    cookie = (set_cookie or "").split(";")[0]
    form = _hidden(page)
    form.update({
        "ctl00$ContentPlaceHolder1$DDLDeptname": "-1",
        "ctl00$ContentPlaceHolder1$sectddl": "-1",
        "ctl00$ContentPlaceHolder1$DDLGoType": GO_TYPE[go_type],
        "ctl00$ContentPlaceHolder1$DdlGo_cat": "-1",
        "ctl00$ContentPlaceHolder1$ddlPages": "100",
        "ctl00$ContentPlaceHolder1$txtfrmdate": day.strftime("%d-%m-%Y"),
        "ctl00$ContentPlaceHolder1$txttodate": day.strftime("%d-%m-%Y"),
        "ctl00$ContentPlaceHolder1$BtnSearch": "Search",
    })
    res, _ = _fetch(BASE, urllib.parse.urlencode(form).encode(), cookie)

    claimed = _claimed(res)
    rows: dict[str, dict] = {}
    for r in _rows(res, day, go_type):
        rows[_key(r)] = r

    seen_targets: set[str] = set()
    for _ in range(max_pages):
        if claimed is None or len(rows) >= claimed:
            break
        # BY LABEL, NOT BY POSITION. The first pager control is "First",
        # which re-renders page one -- following it collected the same 100 rows
        # and looked like a pager that does not advance. The controls are
        # ["First", "2", "Last"], so only the ones whose text is a NUMBER are
        # pages, and they are visited in numeric order.
        #
        # Quotes are HTML-escaped in some of these anchors and raw in others,
        # so both forms are matched; looking for one found nothing at all.
        links = re.findall(
            r"<a[^>]*__doPostBack\((?:&#39;|')([^&']+)(?:&#39;|')[^>]*>(.*?)</a>",
            res, re.S)
        numbered = {}
        for target, label in links:
            text = _text(label)
            if text.isdigit() and "lnkPage" in target:
                numbered.setdefault(int(text), target)
        targets = [t for n, t in sorted(numbered.items()) if t not in seen_targets]
        if not targets:
            break
        target = targets[0]
        seen_targets.add(target)
        # Every postback carries the ViewState the LAST response issued; an
        # older one is rejected outright.
        nxt = _hidden(res)
        nxt.update({
            "__EVENTTARGET": target,
            "__EVENTARGUMENT": "",
            "ctl00$ContentPlaceHolder1$DDLDeptname": "-1",
            "ctl00$ContentPlaceHolder1$sectddl": "-1",
            "ctl00$ContentPlaceHolder1$DDLGoType": GO_TYPE[go_type],
            "ctl00$ContentPlaceHolder1$DdlGo_cat": "-1",
            "ctl00$ContentPlaceHolder1$ddlPages": "100",
            "ctl00$ContentPlaceHolder1$txtfrmdate": day.strftime("%d-%m-%Y"),
            "ctl00$ContentPlaceHolder1$txttodate": day.strftime("%d-%m-%Y"),
        })
        nxt.pop("ctl00$ContentPlaceHolder1$BtnSearch", None)
        res, _ = _fetch(BASE, urllib.parse.urlencode(nxt).encode(), cookie)
        before = len(rows)
        for r in _rows(res, day, go_type):
            rows[_key(r)] = r
        if len(rows) == before:
            # This control added nothing. Try the next one rather than giving
            # up: the count check below is what decides whether the day is
            # complete, not whether one link behaved.
            continue

    if claimed is not None and len(rows) < claimed:
        raise ShortRead(
            f"{day:%d-%m-%Y} {go_type}: portal says {claimed}, collected "
            f"{len(rows)} over {len(seen_targets) + 1} pages. "
            f"Refusing to record a short day.")
    return list(rows.values())


def _key(r: dict) -> str:
    """The portal's gid where there is one -- pages overlap and must dedupe."""
    return r["gid_english"] or r["gid_telugu"] or \
        f'{r["go_number_raw"]}|{r["go_date_raw"]}|{r["organization"]}'


def _claimed(res: str) -> int | None:
    m = re.search(r'lblCount[^>]*>\s*([\d,]+)', res)
    return int(m.group(1).replace(",", "")) if m else None


def _rows(res: str, day: dt.date, go_type: str) -> list[dict]:
    """Parse the result table. Shared by `listing` and `listing_complete`."""
    expected = ["S.No", "G.O N.O", "Date", "G.O Category", "Abstract",
                "Organization", "Section"]
    header = re.search(r"<tr[^>]*>(.*?)</tr>", res, re.S | re.I)
    seen = [_text(t) for t in re.findall(r"<th[^>]*>(.*?)</th>",
                                         header.group(1) if header else "", re.S | re.I)]
    if not seen and (_claimed(res) or 0) == 0:
        return []
    if seen[:len(expected)] != expected:
        raise ShortRead(
            f"the listing's columns have moved: expected {expected}, saw {seen[:8]}. "
            f"Refusing to parse rows by position against a table that changed.")
    out = []
    for raw_row in _ROW.findall(res):
        cells = _CELL.findall(raw_row)
        if len(cells) < 8:
            continue
        gids = dict((kind.upper(), gid) for gid, kind in _GID.findall(raw_row))
        serial = _text(cells[0])
        if not serial.isdigit():
            continue
        out.append({
            "serial": int(serial),
            "go_number_raw": _text(cells[1]),
            "go_date_raw": _text(cells[2]),
            "goir_category": _text(cells[3]),
            "abstract": _text(cells[4]),
            "organization": _text(cells[5]),
            "section": _text(cells[6]),
            "amount_raw": _text(cells[7]) if len(cells) > 7 else "",
            "gid_english": gids.get("E"),
            "gid_telugu": gids.get("T"),
        })
    return out


def listing(day_from: dt.date, day_to: dt.date, go_type: str = "ALL",
            dept: str = "-1") -> list[dict]:
    """Every order the portal lists in [day_from, day_to], or raise.

    Dates are free text dd-mm-yyyy and the range is inclusive; from == to is a
    single day, which is what the daily run asks for.
    """
    page, set_cookie = _fetch(BASE)
    cookie = (set_cookie or "").split(";")[0]
    form = _hidden(page)
    form.update({
        "ctl00$ContentPlaceHolder1$DDLDeptname": dept,
        "ctl00$ContentPlaceHolder1$sectddl": "-1",
        "ctl00$ContentPlaceHolder1$DDLGoType": GO_TYPE[go_type],
        "ctl00$ContentPlaceHolder1$DdlGo_cat": "-1",
        "ctl00$ContentPlaceHolder1$txtfrmdate": day_from.strftime("%d-%m-%Y"),
        "ctl00$ContentPlaceHolder1$txttodate": day_to.strftime("%d-%m-%Y"),
        "ctl00$ContentPlaceHolder1$BtnSearch": "Search",
    })
    res, _ = _fetch(BASE, urllib.parse.urlencode(form).encode(), cookie)

    # The portal prints its own header. Read the columns from it rather than
    # trusting fixed indices: the first version had goir_category at index 6
    # when it is at 3, so every order would have carried the section as its
    # category and the category as its organization.
    found = re.search(r'lblCount[^>]*>\s*([\d,]+)', res)
    claimed = int(found.group(1).replace(",", "")) if found else None

    expected = ["S.No", "G.O N.O", "Date", "G.O Category", "Abstract",
                "Organization", "Section"]
    header = re.search(r"<tr[^>]*>(.*?)</tr>", res, re.S | re.I)
    seen = [_text(t) for t in re.findall(r"<th[^>]*>(.*?)</th>",
                                         header.group(1) if header else "", re.S | re.I)]
    # A day with no orders has no table and therefore no header -- a Sunday or
    # a holiday, not a schema change. Only an ABSENT header on a page that
    # claims rows is evidence that the listing moved.
    if not seen and (claimed or 0) == 0:
        return []
    if seen[:len(expected)] != expected:
        raise ShortRead(
            f"the listing's columns have moved: expected {expected}, saw {seen[:8]}. "
            f"Refusing to parse rows by position against a table that changed."
        )

    rows: list[dict] = []
    for raw_row in _ROW.findall(res):
        cells = _CELL.findall(raw_row)
        if len(cells) < 8:
            continue
        gids = dict((kind.upper(), gid) for gid, kind in _GID.findall(raw_row))
        serial = _text(cells[0])
        if not serial.isdigit():
            continue
        rows.append({
            "serial": int(serial),
            "go_number_raw": _text(cells[1]),
            "go_date_raw": _text(cells[2]),
            "goir_category": _text(cells[3]),
            "abstract": _text(cells[4]),
            "organization": _text(cells[5]),
            "section": _text(cells[6]),
            "amount_raw": _text(cells[7]) if len(cells) > 7 else "",
            # A row with no English link is not an error: Finance's Budget
            # Release Orders are listed without a file at all.
            "gid_english": gids.get("E"),
            "gid_telugu": gids.get("T"),
        })

    if claimed is not None and len(rows) < claimed:
        raise ShortRead(
            f"{day_from:%d-%m-%Y}..{day_to:%d-%m-%Y} {go_type}: portal says "
            f"{claimed} rows, parsed {len(rows)}. Refusing to record a short day."
        )
    return rows


def count(day_from: dt.date, day_to: dt.date, go_type: str = "ALL") -> int:
    """How many orders the portal SAYS are in a window, without reading rows.

    `listing()` refuses a window it cannot read in full, which is right for
    ingestion and useless for surveying: a 95-day gap is thousands of rows over
    dozens of pages. This asks only for `Found:`, which the portal prints on
    the first page however many pages follow.
    """
    page, set_cookie = _fetch(BASE)
    cookie = (set_cookie or "").split(";")[0]
    form = _hidden(page)
    form.update({
        "ctl00$ContentPlaceHolder1$DDLDeptname": "-1",
        "ctl00$ContentPlaceHolder1$sectddl": "-1",
        "ctl00$ContentPlaceHolder1$DDLGoType": GO_TYPE[go_type],
        "ctl00$ContentPlaceHolder1$DdlGo_cat": "-1",
        "ctl00$ContentPlaceHolder1$txtfrmdate": day_from.strftime("%d-%m-%Y"),
        "ctl00$ContentPlaceHolder1$txttodate": day_to.strftime("%d-%m-%Y"),
        "ctl00$ContentPlaceHolder1$BtnSearch": "Search",
    })
    res, _ = _fetch(BASE, urllib.parse.urlencode(form).encode(), cookie)
    m = re.search(r'lblCount[^>]*>\s*([\d,]+)', res)
    return int(m.group(1).replace(",", "")) if m else 0


def pdf(gid: str, lang: str = "E", timeout: int = 90) -> bytes:
    """The order's PDF. A 0-byte 200 is the portal saying 'no file'.

    It does NOT 404 for a missing document -- it returns HTTP 200 with an empty
    body, so a caller that only checks the status code stores an empty file and
    calls it an order.
    """
    conf = _relay_conf()
    if conf:
        data, _ = _via_relay(conf, "GET", f"/dgo.ashx?gid={gid}&fileType={lang}", None, None, timeout)
    else:
        req = urllib.request.Request(
            f"{BASE}dgo.ashx?gid={gid}&fileType={lang}", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    if not data:
        raise FileNotFoundError(f"gid {gid} ({lang}): 0-byte 200, no file on the portal")
    return data


def is_pdf(data: bytes) -> bool:
    """Whether the bytes are actually a PDF.

    The portal serves Word documents under a .pdf name -- 16 of the orders in
    this catch-up window, and 2,893 already in the corpus, which is why
    `routing_bucket` has an `unreadable_not_pdf` value. They are REAL orders
    with a real abstract; refusing them loses records the corpus already knows
    how to hold, so the fetcher keeps them and the loader marks the bucket.
    """
    return data.startswith(b"%PDF")


if __name__ == "__main__":
    import sys
    d = dt.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else dt.date.today()
    rows = listing(d, d)
    print(f"{d}: {len(rows)} orders listed")
    for r in rows[:5]:
        print(f"  {r['go_number_raw']:<12} {r['go_date_raw']:<12} "
              f"gid={r['gid_english'] or '-':<8} {r['organization'][:34]}")
