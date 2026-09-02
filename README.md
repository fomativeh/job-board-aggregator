# Multi-Source Job Board Aggregator

Scraper that pulls software-job listings from Greenhouse, WeWorkRemotely, and Remotive into a normalized dataset backed by MongoDB.

## What It Does

Runs a single CLI command to pull fresh job listings from three independent sources in parallel, normalize to a shared schema, deduplicate by URL, and persist to MongoDB. Per-run CSV/JSON export is added in the next milestone.

Static scrapers are implemented for Greenhouse and WeWorkRemotely. Remotive uses a Playwright-based JS-rendered scraper.

### Sources

| Source | Method |
|--------|--------|
| **Greenhouse** | `httpx.AsyncClient` → JSON REST API. Queries 10 public board tokens per run and filters by CLI `--query`. |
| **WeWorkRemotely** | `httpx.AsyncClient` → static HTML parsed with `BeautifulSoup4` + `lxml`. |
| **Remotive** | async Playwright Chromium context (JS-rendered). |

### Fetch pipeline

- All three sources run concurrently via `asyncio.gather()`.
- Static sources share a single `httpx.AsyncClient` for connection reuse. Requests carry randomized User-Agents and Chrome-like headers, with retries on network/429/5xx failures.
- Inter-request delays use `random.uniform(MIN_DELAY, MAX_DELAY)` jitter.
- Per-source cookies persist to `session/cookies_{source}.json` and replay on subsequent runs.
- Raw output normalizes to `JobListing` immediately after parse and validates before cross-source merging.

## Data Schema

Every listing normalizes to the same 7-field record before dedup and storage. `salary` is the only nullable field.

| Field        | Type    | Notes |
|--------------|---------|-------|
| `title`      | string  | `"Senior Backend Engineer, Payments"` |
| `company`    | string  | `"Stripe"` |
| `location`   | string  | `"Remote — EMEA"` or `"New York, NY"` |
| `salary`     | string \| null | `"$160,000 – $210,000 USD"`; `null` when undisclosed |
| `url`        | string  | Absolute HTTP(S) URL to the job detail page |
| `source`     | string  | `"greenhouse"`, `"weworkremotely"`, or `"remotive"` |
| `scraped_at` | string  | ISO-8601 UTC timestamp |

### Derived fields

- **`url_hash`** — SHA-256 hex digest of `url`. Primary dedup key; a MongoDB unique index guarantees each URL is stored once. Used instead of raw URLs for fixed-length index keys and fast in-memory set lookups.

### Validation

All records pass through a runtime validator in [`src/schema.py`](./src/schema.py) before any write, checking required fields, non-empty strings, HTTP(S) URLs, timestamp format, allowed sources, and `salary` types.

## Usage

Run via the module entrypoint (`python -m src`) or the script invocation path. Both paths produce the same output because `src/__main__.py` delegates to `src/cli.py main()`.

```bash
python -m src --query python --location Remote
```

Both flags are optional:

| Flag | Type | Default | Effect |
|------|------|---------|--------|
| `--query` | string | `""` (empty) | Keyword filter applied client-side to every source's `title`, `company`, `location`, and any source-specific tags/departments. Case-insensitive substring match. Empty string pulls each source's default/latest listing set (no keyword filter). |
| `--location` | string | `""` (empty) | Location filter applied client-side to each source's `location` column. Case-insensitive substring match. Typical values: `"Remote"`, `"New York"`, `"UK"`. Empty string disables the filter (useful when the source itself labels rows as "Remote" in a `tags` column instead of the `location` field). |

### Example command and expected output

Output numbers here are illustrative; real counts depend on what each source publishes on run day. The key structural log lines (INFO-level scraper counts, dedup summary, persist summary) are what to compare against.

```console
$ python -m src --query python --location Remote
2026-09-01 14:20:31,012 INFO CLI args: query='python' location='Remote'
2026-09-01 14:20:33,000 INFO Scraper greenhouse returned 31 listings
2026-09-01 14:20:34,728 INFO Scraper weworkremotely returned 46 listings
2026-09-01 14:20:40,418 INFO Scraper remotive returned 18 listings
2026-09-01 14:20:40,420 INFO Pipeline raw=95 deduped=92 dropped=3
2026-09-01 14:20:40,811 INFO Mongo persist complete: inserted=85 db_duplicates=7
2026-09-01 14:20:40,812 INFO Pipeline complete. Final listing count returned to caller: 92
```

Notes on the above transcript:

- `Pipeline raw=95 deduped=92 dropped=3` — 3 listings appeared on two sources (e.g. a company posts the same job to both Greenhouse and Remotive). `dropped=3` is the cross-source in-memory dedup before any DB call.
- `inserted=85 db_duplicates=7` — MongoDB's unique `url_hash` index catches 7 URLs already persisted from a previous run. The in-memory dedup only sees *this run's* batch; DB dedup catches duplicates *across runs*.
- "Final listing count returned to caller: 92" — exports (CSV/JSON, Milestone 6) are written from this deduped set, not the raw source set.

Exit codes:

| Code | Meaning |
|------|---------|
| `0` | Pipeline completed without fatal errors. |
| `2` | Configuration error — required environment variable missing (check `.env` against `.env.example`). |
| `3` | Network/retry failure — at least one source exceeded MAX_RETRIES on every request attempt. |
| `4` | Remotive scraper failure — Playwright context or in-browser fetch raised. |
| `130` | Interrupted by user (Ctrl-C). |
