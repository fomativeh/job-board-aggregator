# Multi-Source Job Board Aggregator

Production-quality scraper that pulls software-job listings from Greenhouse, WeWorkRemotely, and Remotive into a single normalized dataset backed by MongoDB. Designed as a portfolio piece targeting data-collection / automation hiring managers.

## Data Schema

Every listing, regardless of source board, is normalized to the same 7-field record before dedup and storage. Fields that a source does not disclose are stored as `null` (the `salary` column is the only nullable field in v1).

| Field        | Type    | Example / Notes |
|--------------|---------|-----------------|
| `title`      | string  | `"Senior Backend Engineer, Payments"` |
| `company`    | string  | `"Stripe"` |
| `location`   | string  | `"Remote — EMEA"` or `"New York, NY"` |
| `salary`     | string \| null | `"$160,000 – $210,000 USD"`; `null` when the source does not publish compensation |
| `url`        | string  | Absolute HTTP(S) URL to the single-job detail page — the unit of deduplication |
| `source`     | string  | One of `"greenhouse"`, `"weworkremotely"`, `"remotive"` |
| `scraped_at` | string  | ISO-8601 UTC timestamp of when the record entered the pipeline, e.g. `"2026-09-01T14:02:55Z"` |

### Derived fields

- **`url_hash`** — SHA-256 hex digest of the canonical `url`. This is the primary dedup key: a MongoDB unique index on `url_hash` guarantees the same listing URL (whether re-appearing on the same board on a later run, or surfaced by two boards pointing at the same ATS page) is stored exactly once. A hash is used instead of the raw URL because:
  1. MongoDB unique-index size is more predictable on fixed-length 64-char hex strings than on arbitrarily-long URLs with query strings.
  2. Python-side in-memory dedup before a bulk write is a simple set-lookup over 64-byte strings rather than a comparison over arbitrary-length URLs.

### Validation rules

All records pass through a runtime validator ([`src/schema.py`](./src/schema.py)) before any write. The validator rejects:
- Missing required fields.
- Empty strings for non-nullable text fields.
- Non-HTTP(S) URLs.
- `scraped_at` values that do not match the `%Y-%m-%dT%H:%M:%SZ` UTC format.
- `source` values outside the fixed board whitelist.
- `salary` values that are neither `None` nor a string (sentinel integers, for example, fail early).
