"""
Recover the GOs in this corpus that are image-only scans — real PDFs holding a
photographed page and no text layer, so PyMuPDF extracts zero characters and
they land in the corpus as `no_text_layer` records.

OCR runs on macOS's built-in Vision framework (VNRecognizeTextRequest) via
pyobjc. Vision is an on-device OCR engine, not a language model: it is doing
the same job PyMuPDF's text extraction does on a born-digital PDF — turning
glyphs into characters. It never sees the GO's field structure and never
supplies a value. The OCR'd text is handed to go_parser.parse_text(), so every
field still comes from the same anchors and regexes as a native parse, and the
record has the identical 26-field shape. Tagged `recovered_from_ocr` so an
OCR'd document stays distinguishable from one read natively out of a PDF.

Chosen over tesseract/ocrmypdf because it needs no Homebrew and no system
packages — pyobjc is a pip install, and PyMuPDF already rasterises pages, so
there is no pdftoppm dependency either.

**OCR text is cached** (one .txt per source file under --cache). OCR costs
~0.6s/page and the corpus is ~13,000 pages; parsing that text costs
milliseconds. Caching means a later parser change can be re-applied to every
OCR'd record in seconds rather than re-running the whole scan. Delete the
cache dir to force a genuine re-OCR.

Not every scan is recoverable, and the residue has been measured rather than
assumed. Ink-coverage was sampled across every page of all 1,964 files this pass
could not recover: 1,914 (97.5%) are blank on every page (<0.5% ink), 47 are very
sparse, and just 3 carry real ink. Those 3 are Telugu-script GOs — Vision exposes
no Indic recognition language at all, so it transliterates the glyphs into Latin
noise. 14 further files are zero-page PDFs that cannot be rendered.

The unrecovered remainder is therefore a source-data defect — blank scans and an
unsupported script — not a gap in this tool. All of them keep their original
no_text_layer record and stay visibly unreadable.

Usage:
    python3 ocr_recover.py out/corpus/                      # dry run, reports only
    python3 ocr_recover.py out/corpus/ --apply              # rewrite the year files
    python3 ocr_recover.py out/corpus/ --years 2022 2023    # limit to given years
    python3 ocr_recover.py out/corpus/ --workers 12
"""

import argparse
import hashlib
import json
import multiprocessing as mp
import time
from pathlib import Path

import go_parser as gp

# Backstop floor only — NOT the primary test. Text this long is kept even with no
# anchors, so a readable scan in a layout the parser does not yet handle still
# surfaces for review instead of being silently dropped. The primary test is
# `is_useful_parse()`: whether parsing actually recovered a critical anchor.
MIN_USABLE_CHARS = 200
RECOVERY_WARNING = "recovered_from_ocr"
DEFAULT_DPI = 300
DEFAULT_CACHE = Path("out/ocr_cache")


def cache_path(cache_dir: Path, source_file: str) -> Path:
    """Content-independent, collision-safe name for a source path. The source
    paths contain spaces and non-ASCII, so they are hashed rather than escaped."""
    h = hashlib.sha1(source_file.encode("utf-8")).hexdigest()
    return cache_dir / h[:2] / f"{h}.txt"


def ocr_pdf(source_file: str, dpi: int) -> str:
    """Render every page and OCR it. Imports live in here so the module stays
    importable (and --help works) on a machine without pyobjc installed."""
    import fitz
    import Quartz
    import Vision

    out = []
    doc = fitz.open(source_file)
    try:
        for page in doc:
            png = page.get_pixmap(dpi=dpi).tobytes("png")
            data = Quartz.CFDataCreate(None, png, len(png))
            src = Quartz.CGImageSourceCreateWithData(data, None)
            cgimg = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
            if cgimg is None:
                continue
            req = Vision.VNRecognizeTextRequest.alloc().init()
            req.setRecognitionLevel_(0)          # 0 = accurate (vs 1 = fast)
            req.setUsesLanguageCorrection_(True)
            handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cgimg, None)
            ok, _ = handler.performRequests_error_([req], None)
            if not ok:
                continue
            out.append("\n".join(o.topCandidates_(1)[0].string() for o in (req.results() or [])))
    finally:
        doc.close()
    return "\n".join(out)


def _worker(job):
    """Returns (source_file, text, from_cache). Runs in a separate process:
    Vision is called per-process, and mp uses 'spawn' on macOS so each worker
    initialises its own framework state."""
    source_file, dpi, cache_dir = job
    cp = cache_path(Path(cache_dir), source_file)
    if cp.exists():
        return source_file, cp.read_text(encoding="utf-8", errors="ignore"), True
    try:
        text = ocr_pdf(source_file, dpi)
    except Exception:
        # A file that cannot be rendered keeps its original record; it must not
        # take the whole run down.
        text = ""
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text(text, encoding="utf-8")
    return source_file, text, False


def ocr_batch(jobs, workers, timeout):
    """OCR a list of jobs, returning {source_file: (text, from_cache)}.

    (This said `{source_file: text}` until 2026-10-01 and had done since it was
    written — the callers unpack the tuple, so only a NEW caller trusting the
    docstring would find out, which is what happened.)

    Deliberately defensive about worker death. A 300-dpi render of a legal-size
    page is ~10.7 megapixels, and each worker additionally holds Vision's model
    state; at high worker counts the machine runs out of memory and the OS kills
    a worker outright. `mp.Pool` does not recover from that — `imap_unordered`
    simply blocks forever, which is exactly how the first full run stalled at 0%
    CPU with its workers gone.

    Two guards: `maxtasksperchild` recycles workers so memory cannot creep, and
    every result is awaited with a timeout so a dead worker surfaces as an
    exception instead of a hang. On breakage the pool is rebuilt and the
    remaining jobs continue, so one bad file costs one file, not the run.

    Results are cached per file by _worker(), so re-running picks up where this
    left off regardless of how it ended.
    """
    results = {}
    pending = list(jobs)
    while pending:
        done_this_round = 0
        try:
            with mp.Pool(workers, maxtasksperchild=8) as pool:
                it = pool.imap_unordered(_worker, pending, chunksize=1)
                for _ in range(len(pending)):
                    src, text, from_cache = it.next(timeout=timeout)
                    results[src] = (text, from_cache)
                    done_this_round += 1
            break
        except Exception as e:
            remaining = [j for j in pending if j[0] not in results]
            print(f"    ! OCR pool broke ({type(e).__name__}); "
                  f"{done_this_round} done, {len(remaining)} left — restarting",
                  flush=True)
            if not remaining or done_this_round == 0:
                # No forward progress: the head job is the poison pill. Cache an
                # empty result for it so it is not retried forever, and move on.
                if remaining:
                    bad = remaining[0][0]
                    cp = cache_path(Path(remaining[0][2]), bad)
                    cp.parent.mkdir(parents=True, exist_ok=True)
                    cp.write_text("", encoding="utf-8")
                    results[bad] = ("", False)
                    print(f"    ! giving up on {Path(bad).name[:60]}", flush=True)
                    remaining = remaining[1:]
                if not remaining:
                    break
            pending = remaining
    return results


# A scan carrying an e-office stamp yields a few hundred characters across a
# whole document. Native GOs run ~900 chars/page, so anything under this is not a
# document with a thin text layer — it is an image with a label stuck on it.
TEXT_STARVED_QUALITY = 0.15

RECOVERY_TAGS = {"recovered_from_ocr", "recovered_from_doc_conversion"}


def is_image_only(rec: dict) -> bool:
    """Image-only scans that actually have a page to render. The 14 zero-page
    PDFs in this corpus carry the same no_text_layer warning but cannot be
    rasterised, so they are excluded rather than attempted and failed."""
    return "no_text_layer" in (rec.get("warnings") or []) and (rec.get("page_count") or 0) > 0


def is_text_starved(rec: dict) -> bool:
    """Scans that hid from the `no_text_layer` test by carrying a text layer that
    holds no document text.

    `no_text_layer` fires only at *zero* extractable characters. Around 1,459 GOs
    are page images overlaid with an e-office tracking stamp — the sole extractable
    text being a repeated file reference like `8089189/2023/REGN-I-REV01 606`.
    That is enough to keep the warning from firing, so these were never even
    considered for OCR despite being exactly as unreadable as the scans that were.
    They surfaced as records missing all five critical anchors with an empty
    `order_text`.

    Three conditions keep this narrow. The record must be starved of text
    (`text_quality` below ~1/6th of a normal page), it must actually be *failing*
    (at least one critical anchor missing — a sparse GO that parsed fine is left
    alone), and it must not already be the output of a recovery pass, which would
    otherwise make recovery re-entrant and re-OCR its own results.
    """
    w = set(rec.get("warnings") or [])
    if (rec.get("page_count") or 0) <= 0:
        return False
    if w & RECOVERY_TAGS:
        return False
    if "no_text_layer" in w:
        return False          # already handled by is_image_only()
    if (rec.get("text_quality") or 0.0) >= TEXT_STARVED_QUALITY:
        return False
    return any(f"missing_anchor:{a}" in w for a in gp.CRITICAL_ANCHORS)


def is_ocr_candidate(rec: dict) -> bool:
    return is_image_only(rec) or is_text_starved(rec)


def critical_anchor_count(rec: dict) -> int:
    return sum(1 for a in gp.CRITICAL_ANCHORS if a in (rec.get("anchors_found") or []))


def is_useful_parse(new: dict, text: str, old: dict = None) -> bool:
    """Whether an OCR'd page is worth keeping, decided from what parsing actually
    recovered rather than from how long the text is.

    A raw character count is a proxy, and it was measurably the wrong one: six
    complete GOs (correct number, date and department, all five critical anchors)
    were being discarded for sitting ~18 characters under a hand-picked 200-char
    floor, while 1,719 files of stamp text — "CONFIDENTIAL", "Cancelled",
    "Scanned with OKEN Scanner" — differ from them only in length, not in worth.

    So the real question is asked directly: did this text yield a critical anchor?
    The length floor is kept as an OR-backstop, never as the sole gate, which makes
    this test strictly additive — nothing that previously qualified is now refused.

    `old` adds a no-regression floor for text-starved candidates. An image-only
    record has nothing to lose, but a text-starved one still holds whatever its
    stamp layer parsed to, so an OCR result is refused if it would find *fewer*
    critical anchors than the record it replaces. Recovery may only ever improve
    a record.
    """
    if old is not None and critical_anchor_count(new) < critical_anchor_count(old):
        return False
    return critical_anchor_count(new) >= 1 or len(text.strip()) >= MIN_USABLE_CHARS


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("corpus", type=Path, help="Directory of <year>.jsonl files")
    ap.add_argument("--apply", action="store_true", help="Rewrite the year files in place")
    ap.add_argument("--years", nargs="*", default=None)
    # Worker count is memory-bound, not CPU-bound: each holds a ~10.7MP pixmap
    # plus Vision model state, and Vision already threads internally, so a high
    # count buys little and risks OOM-killed workers. Capped well below cpu_count.
    ap.add_argument("--workers", type=int, default=min(6, max(1, mp.cpu_count() - 2)))
    ap.add_argument("--dpi", type=int, default=DEFAULT_DPI)
    ap.add_argument("--timeout", type=float, default=300.0,
                    help="Seconds to wait for any single file before treating the pool as broken")
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    args = ap.parse_args()

    args.cache.mkdir(parents=True, exist_ok=True)
    year_files = sorted(args.corpus.glob("*.jsonl"))
    if args.years:
        year_files = [p for p in year_files if p.stem in set(args.years)]

    attempted = recovered = well_parsed = cached = 0
    t0 = time.time()

    for year_file in year_files:
        records = [json.loads(l) for l in year_file.open() if l.strip()]
        todo = [(i, r) for i, r in enumerate(records) if is_ocr_candidate(r)]
        if not todo:
            continue

        jobs = [(r["source_file"], args.dpi, str(args.cache)) for _, r in todo]
        batch = ocr_batch(jobs, args.workers, args.timeout)
        texts = {src: t for src, (t, _) in batch.items()}
        cached += sum(1 for _, (_, fc) in batch.items() if fc)

        changed = 0
        for i, rec in todo:
            attempted += 1
            text = texts.get(rec["source_file"], "")
            if not text.strip():
                continue
            # Parse first, then judge: the keep/drop decision is made from the
            # resulting record, not from the length of its input. Parsing is
            # milliseconds against ~0.6s/page for the OCR itself, so doing it
            # before the gate costs nothing measurable.
            path = Path(rec["source_file"])
            new = gp.parse_text(path, text, rec.get("page_count") or 1,
                                extra_warnings=[RECOVERY_WARNING])
            if not is_useful_parse(new, text, rec):
                continue
            recovered += 1
            if critical_anchor_count(new) >= 4:
                well_parsed += 1
            records[i] = new
            changed += 1

        if changed and args.apply:
            tmp = year_file.with_suffix(".jsonl.partial")
            with tmp.open("w") as f:
                for r in records:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            tmp.replace(year_file)
        print(f"  {year_file.stem}: {changed}/{len(todo)} recovered "
              f"[{time.time() - t0:.0f}s elapsed]", flush=True)

    print()
    print(f"no_text_layer records attempted   : {attempted}")
    print(f"  served from OCR cache           : {cached}")
    print(f"OCR'd to usable text              : {recovered}")
    print(f"  of which parse well (4-5 anchors): {well_parsed}")
    print(f"not recoverable                   : {attempted - recovered}")
    print(f"elapsed                           : {time.time() - t0:.0f}s")
    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to rewrite the year files.")


if __name__ == "__main__":
    main()
