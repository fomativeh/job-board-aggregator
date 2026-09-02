from __future__ import annotations

import asyncio
import csv
import logging
from typing import Awaitable, Optional, TypedDict

import httpx

from .config import Config, load_config
from .export import DEFAULT_OUTPUT_DIR, RunOutput, write_both
from .schema import (
    JobListing,
    ValidationError,
    dedup_in_memory,
    validate_listing,
)
from .scrapers import greenhouse, remotive, weworkremotely
from .storage import Storage, MongoConnectionError

log: logging.Logger = logging.getLogger(__name__)

HTTP_TIMEOUT_SECONDS: int = 15
HTTP_CONNECT_TIMEOUT_SECONDS: float = 10.0
HTTP_MAX_CONNECTIONS: int = 8
HTTP_MAX_KEEPALIVE_CONNECTIONS: int = 4


class PipelineResult(TypedDict):
    listings: list[JobListing]
    exports: RunOutput | None


async def run_all_scrapers(
    query: str,
    location: str,
    *,
    httpx_timeout: Optional[httpx.Timeout] = None,
    httpx_limits: Optional[httpx.Limits] = None,
) -> list[JobListing]:
    all_listings: list[JobListing] = []
    timeout = httpx_timeout or httpx.Timeout(
        HTTP_TIMEOUT_SECONDS, connect=HTTP_CONNECT_TIMEOUT_SECONDS
    )
    limits = httpx_limits or httpx.Limits(
        max_connections=HTTP_MAX_CONNECTIONS,
        max_keepalive_connections=HTTP_MAX_KEEPALIVE_CONNECTIONS,
    )
    async with httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=True) as client:
        gh_task: Awaitable[list[JobListing]] = greenhouse.scrape(client, query, location)
        wwr_task: Awaitable[list[JobListing]] = weworkremotely.scrape(client, query, location)
        rem_task: Awaitable[list[JobListing]] = remotive.scrape(query, location)
        results: tuple[list[JobListing], list[JobListing], list[JobListing]] = await asyncio.gather(
            gh_task, wwr_task, rem_task, return_exceptions=False
        )
    source_names: tuple[str, str, str] = ("greenhouse", "weworkremotely", "remotive")
    for name, source_listings in zip(source_names, results, strict=True):
        if not isinstance(source_listings, list):
            log.error("Scraper %s returned non-list result - skipping", name)
            continue
        log.info("Scraper %s returned %d listings", name, len(source_listings))
        for idx, listing in enumerate(source_listings):
            try:
                validate_listing(listing)
            except ValidationError as exc:
                log.warning(
                    "Scraper %s row %d failed validation (%s) - skipping row",
                    name,
                    idx,
                    exc,
                )
                continue
            all_listings.append(listing)
    return all_listings


async def run_pipeline(
    query: str,
    location: str,
    *,
    config: Optional[Config] = None,
    output_dir: Optional[str] = None,
) -> PipelineResult:
    loaded_config = config or load_config()
    all_listings = await run_all_scrapers(query, location)
    deduped, dropped = dedup_in_memory(all_listings)
    log.info(
        "Pipeline raw=%d deduped=%d dropped=%d",
        len(all_listings),
        len(deduped),
        dropped,
    )
    storage = Storage(loaded_config)
    connected = False
    try:
        storage.connect()
        connected = True
    except MongoConnectionError as exc:
        log.error("Skipping MongoDB persist: %s", exc)
    if connected:
        try:
            inserted, db_dupes = storage.insert_many_unique(deduped)
            log.info(
                "Mongo persist complete: inserted=%d db_duplicates=%d",
                inserted,
                db_dupes,
            )
        finally:
            storage.close()
    if output_dir is None:
        output_dir = DEFAULT_OUTPUT_DIR
    try:
        exports: RunOutput | None = write_both(deduped, output_dir)
    except OSError as exc:
        log.error("Failed to write CSV/JSON export files: %s", exc)
        exports = None
    except (csv.Error, TypeError, ValueError) as exc:
        log.error("Export serialization failed, output files incomplete: %s", exc)
        exports = None
    return PipelineResult(listings=deduped, exports=exports)
