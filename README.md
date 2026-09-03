# Multi-Source Job Board Aggregator

Scraper that pulls software-job listings from Greenhouse, WeWorkRemotely, and Remotive into a normalized dataset backed by MongoDB.

## Features

- Scrapes **three independent job boards** in parallel: Greenhouse (static JSON API), WeWorkRemotely (Playwright HTML parse), and Remotive (Playwright-warmed in-browser JSON fetch)
- **Strictly typed** `JobListing` schema validated before every write, with runtime field-checking + null rules
- **Two-layer deduplication** by SHA-256 URL hash: in-memory first occurrence wins, then a MongoDB unique index guarantees each URL persists once across runs
- **MongoDB Atlas / local** persistence compatible; BulkWriteError codes categorized to count duplicates vs real failures
- **CSV + JSON exports ALWAYS written**, even when MongoDB connect/persist fails or the dedup pass returns 0 new rows
- Shared wall-clock **timestamp stem** between matching CSV/JSON files so exports from one invocation stay paired
- JSON rows include a recomputed `url_hash_verified` boolean to catch post-dedup data drift
- **Realistic anti-bot behavior**: rotating user-agents, Chrome-look headers, contextual Sec-Fetch-Site for same- vs cross-origin requests, random jitter on every delay
- Playwright **navigator-stealth** headless Chromium contexts (webdriver mask, UA spoofing, disable-blink AutomationControlled, permissions/hardwareConcurrency masks) for Cloudflare-protected sources
- Structured logging via `logging` module; stderr + optional file logger
- CLI flag precedence over `.env` values; log-level, output-dir, query and location all override env when supplied
- Exit codes (0/2/3/4/130) for scripting pipelines and CI

## What It Does

Runs a single CLI command to pull fresh job listings from three independent sources in parallel, normalize to a shared schema, deduplicate by URL, persist to MongoDB, and export per-run output to CSV and JSON.

Static HTTP fetch is used for Greenhouse. WeWorkRemotely and Remotive are Playwright Chromium scrapers. WWR HTML is parsed with BeautifulSoup4 + lxml; Remotive calls its public JSON endpoint from inside the warmed browser context. The pipeline is organized so new sources or output formats can be added without touching the core deduplication, storage, or export logic.

### Sources

| Source | Method |
|--------|--------|
| **Greenhouse** | `httpx.AsyncClient` to JSON REST API. Queries 10 public board tokens per run and filters by CLI `--query`. |
| **WeWorkRemotely** | async Playwright Chromium context (headless, stealth masks) to page HTML parsed with `BeautifulSoup4` + `lxml`. |
| **Remotive** | async Playwright Chromium context (headless, stealth masks) to in-browser JSON fetch. |

### Architecture

```
python -m src --query X --location Y
  │
  └─ cli.parse_args + logging.configure
      │
      └─ config.load_config (dotenv < overrides < CLI args) builds Config (absolute output_dir)
          │
          └─ pipeline.run_pipeline (query, location, config)
              │
              ├─ asyncio.gather(greenhouse, weworkremotely, remotive)
              │        │                    │                    │
              │        └─ httpx / Playwright stealth per source
              │
              ├─ validate_listing row-by-row drops invalid
              ├─ dedup_in_memory by url_hash (SHA-256)
              ├─ Storage.connect (MongoDB)
              ├─ Storage.insert_many_unique (unique url_hash index)
              │     └─ catches DuplicateKeyError + BulkWriteError code 11000 as duplicates_skipped
              └─ export.write_both(deduped, output_dir)
                    ├─ CSV: fixed column order, shared timestamp stem
                    └─ JSON: recomputed url_hash_verified boolean per row
```

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

## Requirements

- Python **3.10+** (PEP 604 `X | Y` unions used; `from __future__ import annotations` shim on every module so 3.10 works)
- MongoDB **4.4+** local install (default) OR a MongoDB Atlas cluster URI
- Playwright Chromium browser binaries (one-time install via `playwright install chromium`)
- On Windows: PowerShell 5.1+ recommended for the commands below

## Setup

### 1. Clone and create a virtual environment

```powershell
git clone https://github.com/fomativeh/job-board-aggregator.git
cd job-board-aggregator
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure environment

Copy `.env.example` to `.env` and fill in the required MongoDB trio:

```powershell
Copy-Item .env.example .env
notepad .env
```

**Required (all three must be set - MissingConfigError otherwise):**
- `MONGO_URI` - MongoDB connection string (local default `mongodb://localhost:27017` or Atlas `mongodb+srv://user:pw@cluster0.mongodb.net/`)
- `MONGO_DB` - database name (default: `job_aggregator`)
- `MONGO_COLLECTION` - collection name (default: `job_listings`)

**Optional (fallbacks apply when missing):**
- `LOG_LEVEL` - `DEBUG | INFO | WARNING | ERROR | CRITICAL`. Invalid -> `INFO`. Default: `INFO`
- `LOG_FILE` - optional absolute/relative path for additive file log in addition to stderr. Parent dirs are created automatically. Default: unset (no file log)

### 3. Verify MongoDB connectivity

Uses the same `python-dotenv` parser the pipeline does, so quoted values, inline comments, and leading whitespace all behave identically to a real run.

```powershell
python -c "from src.config import load_config; from pymongo import MongoClient; c=MongoClient(load_config().mongo_uri, serverSelectionTimeoutMS=5000); c.admin.command('ping'); print('MongoDB ping OK'); print('db list sample:', c.list_database_names()[:3])"
```

Expected output is `MongoDB ping OK` plus the first 3 database names (or `[]` on a brand-new cluster). If this step fails, fix the `.env` `MONGO_URI` line (or start a local dev `mongod` on 27017 if you're not using Atlas) before running the Usage command below.

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
| `--output-dir` | string | `"output"` | Directory where CSV and JSON exports go. Created if missing. Resolved relative to the repo root when not absolute. |
| `--log-level` | string | unset | Overrides `LOG_LEVEL` in `.env`. Values: `DEBUG INFO WARNING ERROR CRITICAL`. Invalid values fall back to `.env`'s level. |
| `--log-file` | string | unset | Overrides `LOG_FILE` in `.env`. Additive file log with stderr console log still attached. Parent dirs auto-created. |

### Example command and expected output

Output counts are illustrative. The key log lines are what to compare against.
File paths shown are relative to `<repo>/`; the actual run prints absolute,
resolved paths (your local repo root replaces `<repo>`).

```console
$ python -m src --query python --location Remote
2026-09-01 14:20:31,012 INFO CLI args: query='python' location='Remote' output_dir='<repo>/output' log_level='INFO' log_file=None
2026-09-01 14:20:33,000 INFO Scraper greenhouse returned 31 listings
2026-09-01 14:20:34,728 INFO Scraper weworkremotely returned 46 listings
2026-09-01 14:20:40,418 INFO Scraper remotive returned 18 listings
2026-09-01 14:20:40,420 INFO Pipeline raw=95 deduped=92 dropped=3
2026-09-01 14:20:40,811 INFO Mongo persist complete: inserted=85 db_duplicates=7
2026-09-01 14:20:40,902 INFO CSV written: <repo>/output/job_listings_20260901_142040.csv (92 rows)
2026-09-01 14:20:40,908 INFO JSON written: <repo>/output/job_listings_20260901_142040.json (92 rows)
2026-09-01 14:20:40,909 INFO CSV  -> <repo>/output/job_listings_20260901_142040.csv
2026-09-01 14:20:40,909 INFO JSON -> <repo>/output/job_listings_20260901_142040.json
2026-09-01 14:20:40,910 INFO Pipeline complete. Final listing count returned to caller: 92
```

## Anti-bot notes

**Greenhouse** — plain httpx with rotating desktop UAs and Chrome-style Accept/Sec-Fetch-* headers. Retries on network errors, 429, and 5xx; 4xx client errors are logged and skipped.

**WeWorkRemotely / Remotive** — Playwright Chromium with headless-stealth masks (webdriver, UA, platform, permissions, hardwareConcurrency). Session cookies replay from `session/` after the first successful run so subsequent invocations skip re-solving the JS challenge.

Remotive's API is called from inside the warmed page context rather than a raw httpx request, since it otherwise returns empty on some residential IPs.

All inter-request waits use random jitter (0.8–2.4 s).

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
| `2` | Configuration error - check `.env` against `.env.example`. |
| `3` | At least one source exceeded MAX_RETRIES on every request attempt. |
| `4` | Remotive scraper failure - Playwright or in-browser fetch raised. |
| `130` | Interrupted by user (Ctrl-C). |

## Running tests

Tests live under `test/` and use pytest with coverage. Mypy strict mode is enabled across `src/` and `test/`.

Globally installed third-party pytest plugins (for unrelated packages) can break imports on machines that share a system-level site-packages; the commands below disable plugin autoload so the suite is reproducible. On clean pip-installed machines you can drop the environment variables.

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
$env:PYTEST_ADDOPTS="-p no:cacheprovider"
python -m pytest test/ -v -p pytest_cov
python -m pytest test/ -v -p pytest_cov --cov=src --cov-report=term-missing
python -m mypy --strict src/ test/
```

Configuration is in `pytest.ini`.

## Project layout

```
<repo>/
├── src/
│   ├── __main__.py          # python -m src entrypoint, delegates to cli.main()
│   ├── cli.py               # argparse, configure_logging, main()
│   ├── config.py            # frozen Config, load_config, MissingConfigError
│   ├── export.py            # CSV + JSON writers
│   ├── http_utils.py        # rotating UAs, jitter, retry, build_headers
│   ├── pipeline.py          # gather fan-out, dedup, MongoDB persist
│   ├── schema.py            # JobListing TypedDict, URL hashing, dedup, validate
│   ├── storage.py           # MongoDB connect, insert_many_unique with dedup
│   └── scrapers/
│       ├── greenhouse.py
│       ├── remotive.py
│       └── weworkremotely.py
├── test/
│   └── test_dedup_and_export.py
├── session/                 # Playwright cookie stores (per-source)
├── output/                  # Per-run CSV/JSON
├── pytest.ini
├── requirements.txt
├── .env.example
└── .gitignore
```

## License

MIT. Respect robots.txt and rate limits when running the scrapers.
