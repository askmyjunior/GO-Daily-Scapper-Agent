# goir-daily

Brings new Andhra Pradesh Government Orders from the GOIR portal
(goir.ap.gov.in) into AskMyJunior every evening, on GitHub Actions, so the
update never depends on a laptop being switched on.

**Schedule:** 19:00 IST daily (`.github/workflows/goir-daily.yml`).

## What a run does

1. **Window:** the last 15 days by G.O. date (60 on Sundays), because
   departments upload late. It never reaches back before the October 2026
   catch-up (MS 20 Jun, RT 30 Jun 2026).
2. **Listing:** every MS and RT order the portal lists, each day checked against
   the portal's own count. A day that can't be read in full is not recorded,
   and the run fails so you hear about it.
3. **Diff:** an order is new unless its file name or portal ID is already in the
   corpus. Orders that were loaded incomplete are finished when they can be: a
   listing that had no document and now has one, or a scan that has not been
   OCR'd yet.
4. **Download, parse, OCR:** the corpus's own parser (`vendor/`). Scans go
   through **Tesseract**, with the same quality gates as the corpus's earlier OCR.
5. **R2:** every file is uploaded and verified *before* any row points at it.
6. **Load:** orders, references, recipients and the search index go in **one
   transaction**, so a run lands whole or not at all.
7. **Classify:** Gemini reads the abstract only and picks from the live
   taxonomy. If the Gemini credit runs out, the orders still go live,
   uncategorised, and the next run classifies them.
8. **Refresh:** category counts and the facet cache, so the site's numbers move
   with the corpus.

## Setup (once)

Add these under **Settings → Secrets and variables → Actions → New repository
secret**. Each value is in a file on the laptop: open the file in a text editor
and copy the value after the `=`. Don't paste them in a terminal or a chat.

| Secret | Where the value is |
|---|---|
| `SUPABASE_DB_URL` | `~/.askmyjunior/supabase.env` (the session pooler, port **5432**) |
| `R2_ACCOUNT_ID` | `~/.askmyjunior/r2.env` |
| `R2_BUCKET` | `~/.askmyjunior/r2.env` |
| `R2_ACCESS_KEY_ID` | `~/.askmyjunior/r2.env` |
| `R2_SECRET_ACCESS_KEY` | `~/.askmyjunior/r2.env` |
| `GEMINI_API_KEY` | `~/.askmyjunior/gemini.env` |

Then go to **Actions → GOIR daily sync → Run workflow** and choose
**mode: dry-run** first. A dry run reads the portal and the database and parses
documents, but writes nothing. Check its summary, then run once with
**mode: full**. After that the schedule takes over.

## Running by hand

The **Run workflow** button takes three inputs:

- `mode`: `dry-run` or `full`
- `lookback`: days to re-check
- `limit`: at most this many new orders

Locally:

```sh
python3 sync.py --mode dry-run --lookback 3
python3 sync.py                      # a real run
python3 selftest.py                  # imports; Tesseract reads a scan
```

## When a run fails

GitHub emails the repository owner when a run ends non-zero. The run's summary
page says why, and `sync-report` (an artifact kept for 30 days) has the details.

| Exit | Meaning |
|---|---|
| 0 | Clean |
| 1 | Something wasn't done: a day unread, a download failed, an unknown department. Everything that *was* done is committed, and the next run's window covers the rest. |
| 2 | Orders loaded, but classification stopped (usually Gemini credit). Top up, and the next run classifies them. |

## Cost

Measured on the October catch-up:

- **GitHub:** about 5 minutes a run, well inside the free 2,000 minutes a month for private repositories.
- **Gemini:** about $0.29 a month (around ₹25), at roughly 1,950 orders a month and $0.000146 per order.
- **R2:** about 9 MB of new files a day, which is negligible.

## Code

| File | What it is |
|---|---|
| `sync.py` | The daily run |
| `pipeline.py` | What a listing row becomes in the corpus, shared with the catch-up so the two can't diverge |
| `goir_portal.py` | Reads the portal over HTTP (no browser) and checks every read against the portal's count |
| `classify_new.py` | Gemini classification into the live taxonomy |
| `selftest.py` | Checks run before each sync |
| `vendor/` | The corpus's own parser and loaders, copied from go-ingestion (see `vendor/README.md`) |
| `fetch_catchup.py`, `load_catchup.py` | The October 2026 catch-up, kept as the record of it |

## Known limits

- Tesseract runs in English only. A Telugu scan is recorded as "text could not
  be recognised" and its PDF still opens. That is about one order in several
  hundred.
- On Linux, old `.doc` files are read with antiword. `.rtf` files are not read;
  none has appeared since 2026.
- GitHub can start a scheduled run some minutes late.
