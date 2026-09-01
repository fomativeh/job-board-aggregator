# Multi-Source Job Board Aggregator

Scraper that pulls software-job listings from Greenhouse, WeWorkRemotely, and Remotive into a normalized dataset backed by MongoDB.

## What It Does

Runs a single CLI command to pull fresh job listings from three independent sources in parallel, normalize to a shared schema, deduplicate by URL, persist to MongoDB, and export per-run output to CSV and JSON.

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
