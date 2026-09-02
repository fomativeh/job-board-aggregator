from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Iterable, NotRequired, Optional, TypedDict, Union

SalaryType = Optional[str]

log: logging.Logger = logging.getLogger(__name__)

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

URL_HASH_FIELD: str = "url_hash"

class JobListing(TypedDict):
    title: str
    company: str
    location: str
    salary: SalaryType
    url: str
    source: str
    scraped_at: str
    url_hash: NotRequired[str]


class ValidationError(Exception):
    pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def validate_listing(listing: Union[dict[str, object], JobListing]) -> None:
    missing: set[str] = REQUIRED_FIELDS - set(listing.keys())
    if missing:
        raise ValidationError(f"Missing required fields: {sorted(missing)}")

    for field in REQUIRED_FIELDS:
        value = listing.get(field)
        if field in NULLABLE_FIELDS:
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


def apply_url_hashes(listings: list[JobListing]) -> list[JobListing]:
    for row in listings:
        if URL_HASH_FIELD in row:
            continue
        if "url" in row:
            row["url_hash"] = make_url_hash(row["url"])
    return listings


def dedup_in_memory(listings: Iterable[JobListing]) -> tuple[list[JobListing], int]:
    seen_hashes: set[str] = set()
    deduped: list[JobListing] = []
    dropped = 0
    for listing in listings:
        url_hash = make_url_hash(listing["url"])
        if url_hash in seen_hashes:
            dropped += 1
            continue
        seen_hashes.add(url_hash)
        listing["url_hash"] = url_hash
        deduped.append(listing)
    if dropped:
        log.warning("Pre-Mongo dedup dropped %d cross-source URL duplicates", dropped)
    return deduped, dropped
