# Job Board Aggregator

Scrape 3 job boards in parallel, normalize listings, dedupe across sources, persist to MongoDB, and export timestamped CSV/JSON. Built for Python 3.11+ with MongoDB storage, curl-cffi HTTP, and Patchright (patched Playwright) browser automation with real persistent Chrome profiles.

## Sources

| Source       | Transport          | Mechanism                                                               |
|--------------|--------------------|-------------------------------------------------------------------------|
| Greenhouse   | HTTP (curl-cffi fingerprint) | Direct boards-api.greenhouse.io JSON across multiple company boards    |
| Glassdoor    | Patchright + Chrome persistent profile  | Form fill, paginate via "Show more jobs", parse card DOM               |
| FlexJobs     | Patchright + Chrome                   | Direct encoded `/search?searchkeyword=&joblocations=` URL, wizard escape, pagination link |

## Install

Requires Python 3.11+ and a MongoDB instance (local or Atlas).

```
git clone https://github.com/fomativeh/job-board-aggregator.git
cd job-board-aggregator
python -m venv .venv
.venv\Scripts\activate    # Windows POSH
pip install -r requirements.txt
pip install curl-cffi==0.16.3 patchright==1.51.3
patchright install chrome
```

## Environment

Create a `.env` file in project root (gitignored):

```
MONGODB_URI=mongodb+srv://user:pass@cluster0.xxx.mongodb.net/
MONGO_DB=job_aggregator
MONGO_COLLECTION=job_listings
LOG_LEVEL=INFO
HTTP_USER_AGENT_OVERRIDE=
OUTPUT_DIR=
```

Lines:
1. MongoDB connection string. Wrap IPv6 literals in square brackets when required by the URI parser.
2. Database name.
3. Collection name (a unique index is enforced on `url_hash`).
4. One of `DEBUG`, `INFO`, `WARNING`, `ERROR` (case-insensitive, defaults to `INFO`).
5. Optional UA override string.
6. Optional absolute or relative output directory (resolved against project root, default `./output`).

## CLI Usage

Entry point: `python -m src <flags>`

```
$ python -m src --help
JOB AGGREGATOR CLI
Usage: python -m src --query QUERY --location LOCATION [options]

Required:
  --query STR             Keywords to search (role, stack, company)
  --location STR          Location filter (city/state/"Remote")

Optional:
  --max-listings N        Hard cap on final deduped rows (default: unbounded)
  --max-pages-per-source N Unused slot for future per-source paging limits
  --output-dir PATH       Override configured export directory
  --log-level LEVEL       Override .env LOG_LEVEL for this run only
  --no-mongo              Skip MongoDB storage step, only write CSV+JSON
```

Quick example:

```
python -m src --query "python developer" --location Remote --max-listings 30
```

## Architecture

```
src/
  schema.py      JobListing TypedDict, validation, dedup, url_hash
  config.py      .env loader, OUTPUT_DIR resolver
  http_utils.py  retry loop, contextual Sec-Fetch headers, cookie jar, UA rotation
  storage.py     Storage.connect / insert_many_unique, MongoConnectionError,
                 unique url_hash index for cross-run dedup
  export.py      timestamped CSV + JSON writers, RunOutput paths
  scrapers/
    greenhouse.py    8 company boards, JSON API, token overlap location filter
    glassdoor.py     Patchright persistent Chrome, form fill, load-more paginate
    flexjobs.py      Direct encoded URL, wizard escape loop, soft-reg modal dismiss
  pipeline.py    run_all_scrapers: initial round + selective holdback reruns
                 (pick_backup_scrapers_by_quota ranks under-performing scrapers
                  by per-source share ratio; satisfied sources are skipped)
  cli.py         argparse, logging, pipeline entrypoint
  __main__.py    `python -m src`
```

Orchestration round-trip:
1. Initial round runs all 3 scrapers with per-source cap `ceil(target / 3)`.
2. If total unique URLs < `--max-listings`, `pick_backup_scrapers_by_quota()` ranks scrapers by `delivered / per_source_share` and holdback rounds rerun ONLY sources below 1.0 ratio (zero-delivered always included, ranked worst first). Fully-satisfied sources are skipped entirely.
3. Final in-memory `url_hash` dedup, `--max-listings` trim, Mongo insert (ordered=False, `BulkWriteError` code 11000 counted as DB duplicates), CSV+JSON export.

## Schema

### JobListing TypedDict

```python
{
  "title": str,                        # non-empty
  "company": str,                      # non-empty
  "location": str,                     # raw location string
  "salary": str | None,                # "$120,000 - $160,000 USD" style
  "url": str,                          # absolute listing URL
  "source": Literal["greenhouse","glassdoor","flexjobs"],
  "scraped_at": str,                   # UTC ISO-8601
  "url_hash": str,                     # SHA-256 hexdigest of normalized url
}
```

### Public module surfaces

- **storage.Storage(config: Config)**
  - `connect() -> None` — raises `MongoConnectionError` if ping/admin command fails.
  - `insert_many_unique(listings: list[JobListing]) -> tuple[int, int]` — `(inserted, duplicates_skipped)`. Cross-run dedup via `unique=True` `url_hash` MongoDB index + ordered=False insert + BulkWriteError 11000 tally.
  - `close() -> None`.

- **export.write_both(listings, output_dir) -> RunOutput**
  - `RunOutput.csv_path` / `.json_path` absolute paths. Timestamp format `YYYYMMDD_HHMMSS`. CSV header order matches the TypedDict key declaration order.

- **pipeline.run_pipeline(query, location, ...)** — top-level runner returns `PipelineResult`: `{listings, exports}`. Returns empty exports on filesystem write errors (ERROR log line emitted).

## Troubleshooting

**Patchright / Chrome crashing on first run.** Run `patchright install chrome` once. Verify `chrome://version` in the launched profile matches expected channel.

**Glassdoor `Recommended Jobs For You` loads but 0 cards parse.** Glassdoor renames class hashes monthly. Debug dumps are written to `./debug/glassdoor/*.html` each run. Update the selector tuples in `src/scrapers/glassdoor.py` `_extract_cards()`. INFO-level sample logs dump the first 12 parsed card fields plus REJECTED reason lines (missing_field / query_filter tokens_hit / location_filter want vs got).

**FlexJobs landing on `/job_wizard/remote/why_remote`.** Server-side 302 gate on first `/search` hit. `_maybe_escape_job_wizard()` handles this: step 1 sets cookie state by letting the redirect land; step 2 re-navigates to the results URL with explicit `Referer: https://www.flexjobs.com/` and `Sec-Fetch-Site: same-origin` headers. If still stuck, `page.evaluate()` scans all `<a>`/`<button>` for regex `/\bNext\b/i` and calls native `click()` bypassing Playwright visibility checks.

**FlexJobs "Success! We found N job matches" soft-reg modal blocks pagination.** `_dismiss_soft_reg_modal()` detects by class markers `sc-34eca615-0` / `iuDbli` + id `soft-reg-continue-btn`. Attempts close-button click (force=True), then falls back to `page.evaluate()` removing the root modal DOM nodes plus clearing `body.style.overflow` scroll locks. Card extraction still works even if removal fails because cards underlay the overlay in DOM and `querySelectorAll()` walks by selector regardless of visual z-index.

**Mongo `insert_many_unique` inserts 0 new listings because URLs already stored from earlier runs.** Expected behavior for idempotent repeat runs; dedup works correctly.

**Holdback rounds look like they "reopen browsers for no reason".** After the selective-holdback fix, only under-quota scrapers are reranked for reruns. If a scraper delivered >= its per-source share it is skipped by name and its browser never reopens (INFO log line `skipping satisfied=[...]` confirms). If all three scrapers meet their share the holdback round exits early with an INFO log line.

## Tests

29 tests cover:
- schema deterministic `url_hash`
- `validate_listing` rejects invalid sources (source-lock check for 3-source-only enum, M10 fix)
- `build_headers` contextual 3-way `Sec-Fetch-Site` by referer origin (M13 fix)
- `load_config` resolves relative OUTPUT_DIR against PROJECT_ROOT not CWD (M12 fix)
- Storage.connect raises before connect and bad URIs raise MongoConnectionError
- Storage unique url_hash index correctly deduplicates (M14 fix)
- In-memory dedup first-occurrence-wins ordering
- Export CSV/JSON same filename timestamp + column ordering
- CLI log-level coerce case, invalid values default to INFO

Run with:
```
pytest -q test/
```

## License

Project code only; scrapers respect robots.txt limits and rate-limiting via per-request jitter.
