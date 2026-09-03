# Multi-Source Job Board Aggregator

Scraper that pulls software-job listings from Greenhouse, WeWorkRemotely, and Remotive into a normalized dataset backed by MongoDB.

## What It Does

Runs a single CLI command to pull fresh job listings from three independent sources in parallel, normalize to a shared schema, deduplicate by URL, persist to MongoDB, and export per-run output to CSV and JSON.

Static HTTP fetch is used for Greenhouse. WeWorkRemotely and Remotive are Playwright Chromium scrapers. WWR HTML is parsed with BeautifulSoup4 + lxml; Remotive calls its public JSON endpoint from inside the warmed browser context.

### Sources

| Source | Method |
|--------|--------|
| **Greenhouse** | `httpx.AsyncClient` → JSON REST API. Queries 10 public board tokens per run and filters by CLI `--query`. |
| **WeWorkRemotely** | async Playwright Chromium context (headless, stealth masks) → page HTML parsed with `BeautifulSoup4` + `lxml`. |
| **Remotive** | async Playwright Chromium context (headless, stealth masks) → in-browser JSON fetch. |

### Fetch pipeline

- All three sources run concurrently via `asyncio.gather()`.
- Greenhouse uses a single `httpx.AsyncClient` with randomized User-Agents, Chrome-like headers, and retries on network/429/5xx.
- WeWorkRemotely and Remotive each open their own Playwright Chromium context per run with stealth masks, UA spoofing, and `--disable-blink-features=AutomationControlled`.
- Inter-request delays use `random.uniform(MIN_DELAY, MAX_DELAY)` jitter.
- Per-source cookies persist to `session/cookies_{source}.json` and replay on subsequent runs.
- Raw output normalizes to `JobListing` immediately after parse and validates before cross-source merging.

## Data Schema

Every listing normalizes to the same 7-field record before dedup and storage. `salary` is the only nullable field.

| Field        | Type    | Notes |
|--------------|---------|-------|
| `title`      | string  | `"Senior Backend Engineer, Payments"` |
| `company`    | string  | `"Stripe"` |
| `location`   | string  | `"Remote - EMEA"` or `"New York, NY"` |
| `salary`     | string \| null | `"$160,000 - $210,000 USD"`; `null` when undisclosed |
| `url`        | string  | Absolute HTTP(S) URL to the job detail page |
| `source`     | string  | `"greenhouse"`, `"weworkremotely"`, or `"remotive"` |
| `scraped_at` | string  | ISO-8601 UTC timestamp |

### Derived fields

- **`url_hash`** - SHA-256 hex digest of `url`. Primary dedup key; a MongoDB unique index guarantees each URL is stored once. Used instead of raw URLs for fixed-length index keys and fast in-memory set lookups.

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
| `--query` | string | `""` | Case-insensitive keyword filter on title, company, location, and tags/departments. Empty pulls the default/latest listing set from each source. |
| `--location` | string | `""` | Case-insensitive substring match on each source's location column. Bare `--location` (no value) is shorthand for empty, skipping the filter. |
| `--output-dir` | string | `"output"` | Directory where CSV and JSON exports go. Created if missing. |

### Example command and expected output

Output counts are illustrative. The key log lines are what to compare against.

```console
$ python -m src --query python --location Remote
2026-09-01 14:20:31,012 INFO CLI args: query='python' location='Remote' output_dir='output'
2026-09-01 14:20:33,000 INFO Scraper greenhouse returned 31 listings
2026-09-01 14:20:34,728 INFO Scraper weworkremotely returned 46 listings
2026-09-01 14:20:40,418 INFO Scraper remotive returned 18 listings
2026-09-01 14:20:40,420 INFO Pipeline raw=95 deduped=92 dropped=3
2026-09-01 14:20:40,811 INFO Mongo persist complete: inserted=85 db_duplicates=7
2026-09-01 14:20:40,902 INFO CSV written: output/job_listings_20260901_142040.csv (92 rows)
2026-09-01 14:20:40,908 INFO JSON written: output/job_listings_20260901_142040.json (92 rows)
2026-09-01 14:20:40,909 INFO CSV  -> output/job_listings_20260901_142040.csv
2026-09-01 14:20:40,909 INFO JSON -> output/job_listings_20260901_142040.json
2026-09-01 14:20:40,910 INFO Pipeline complete. Final listing count returned to caller: 92
```

## Output

Per-run export files go to `output/` (configurable with `--output-dir`). Two files per invocation:

| File | Description |
|------|-------------|
| `job_listings_<UTC_TIMESTAMP>.csv` | Flat spreadsheet. Fixed column order. |
| `job_listings_<UTC_TIMESTAMP>.json` | Array of JSON objects. Includes a `url_hash_verified` boolean per row. |

### Directory layout

```
<repo>/
├── output/
│   ├── job_listings_20260901_142040.csv
│   ├── job_listings_20260901_142040.json
│   ├── job_listings_20260902_081102.csv
│   └── job_listings_20260902_081102.json
├── session/
├── src/
└── …
```

### CSV column order

```
title, company, location, salary, url, source, scraped_at, url_hash
```

- `salary` - blank when undisclosed. Numeric values are plain digits; range strings stay as strings.
- `url_hash` - SHA-256 hex of `url`. Matches the dedup key stored in MongoDB.

### JSON shape (one row, trimmed)

```json
[
  {
    "title": "Senior Engineer - Backend Platform",
    "company": "Stripe",
    "location": "Remote - EMEA",
    "salary": null,
    "url": "https://boards.greenhouse.io/stripe/jobs/5000000006",
    "source": "greenhouse",
    "scraped_at": "2026-09-01T14:20:33.123456+00:00",
    "url_hash": "a1b2c3d4e5f6…",
    "url_hash_verified": true
  }
]
```

`url_hash_verified` recomputes the SHA-256 inline during export and compares it to `url_hash`. False indicates hash mismatch between schema enrichment and export.

Exit codes:

| Code | Meaning |
|------|---------|
| `0` | Pipeline completed without fatal errors. |
| `2` | Configuration error — check `.env` against `.env.example`. |
| `3` | At least one source exceeded MAX_RETRIES on every request attempt. |
| `4` | Remotive scraper failure — Playwright or in-browser fetch raised. |
| `130` | Interrupted by user (Ctrl-C). |

## Running tests

Unit tests live under `test/` and use `pytest`. They cover URL hashing, the in-memory dedup pass, export path timestamping, and storage-layer validation paths. The storage connect test spins briefly on a bogus host so it does not require a live MongoDB instance.

Globally installed third-party pytest plugins (for unrelated packages) can break imports on machines that share a system-level site-packages; the commands below disable plugin autoload so the suite is reproducible. On clean pip-installed machines you can drop the environment variables.

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
$env:PYTEST_ADDOPTS="-p no:cacheprovider"
python -m pytest test/ -v -p pytest_cov
python -m pytest test/ -v -p pytest_cov --cov=src --cov-report=term-missing
```

Configuration is in `pytest.ini`.
