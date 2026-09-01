from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional, TypedDict, Union

SalaryType = Optional[str]

NULLABLE_FIELDS: set[str] = {"salary"}

REQUIRED_FIELDS: set[str] = {
    "title",
    "company",
    "location",
    "salary",
    "url",
    "source",
    "scraped_at",
}

ALLOWED_SOURCES: set[str] = {"greenhouse", "weworkremotely", "remotive"}


class JobListing(TypedDict):
    title: str
    company: str
    location: str
    salary: SalaryType
    url: str
    source: str
    scraped_at: str


class ValidationError(Exception):
    pass


def utc_now_iso() -> str:
    # ISO-8601 UTC without timezone abbreviation; strftime gives a deterministic
    # format so scraped_at values are sortable strings across runs.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_url_hash(url: str) -> str:
    # SHA-256 over the canonical URL string. Using a cryptographic hash here
    # (rather than a short/fast one like xxhash) means accidental collisions
    # across millions of URLs are statistically impossible; MongoDB index
    # uniqueness on this field is then a correctness guarantee, not a heuristic.
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def validate_listing(listing: Union[dict[str, object], JobListing]) -> None:
    missing: set[str] = REQUIRED_FIELDS - set(listing.keys())
    if missing:
        raise ValidationError(f"Missing required fields: {sorted(missing)}")

    for field in REQUIRED_FIELDS:
        value = listing.get(field)
        if field in NULLABLE_FIELDS:
            # Nullable fields accept None or the expected type, never a sentinel string.
            if value is None:
                continue
            if not isinstance(value, str):
                raise ValidationError(
                    f"Field '{field}' must be None or str, got {type(value).__name__}"
                )
            continue
        if not isinstance(value, str):
            raise ValidationError(
                f"Field '{field}' must be str, got {type(value).__name__}"
            )
        if value == "":
            # Empty string is a different bug than missing data; fail loudly
            # so the parser that emitted it gets fixed rather than silently
            # polluting the dataset.
            raise ValidationError(f"Field '{field}' must be non-empty str")

    source_value = listing["source"]
    if isinstance(source_value, str) and source_value not in ALLOWED_SOURCES:
        raise ValidationError(
            f"Unknown source '{source_value}'; expected one of {sorted(ALLOWED_SOURCES)}"
        )

    url_value = listing["url"]
    if isinstance(url_value, str) and not (
        url_value.startswith("http://") or url_value.startswith("https://")
    ):
        raise ValidationError(f"Field 'url' must be an absolute HTTP(S) URL, got {url_value!r}")

    scraped_at_value = listing["scraped_at"]
    if isinstance(scraped_at_value, str):
        try:
            datetime.strptime(scraped_at_value, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError as exc:
            raise ValidationError(
                f"Field 'scraped_at' must match ISO-8601 UTC format %Y-%m-%dT%H:%M:%SZ, got {scraped_at_value!r}"
            ) from exc
