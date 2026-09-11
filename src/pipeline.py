from __future__ import annotations

import asyncio
import csv
import logging
import math
from typing import Awaitable, Optional, TypedDict

import httpx

from .config import Config, DEFAULT_OUTPUT_DIR, load_config
from .export import RunOutput, write_both
from .schema import (
    JobListing,
    ValidationError,
    dedup_in_memory,
    validate_listing,
)
from .scrapers import flexjobs, glassdoor, greenhouse
from .storage import Storage, MongoConnectionError

log: logging.Logger = logging.getLogger(__name__)

HTTP_TIMEOUT_SECONDS: int = 15
HTTP_CONNECT_TIMEOUT_SECONDS: float = 10.0
HTTP_MAX_CONNECTIONS: int = 8
HTTP_MAX_KEEPALIVE_CONNECTIONS: int = 4

MAX_HOLDBACK_ROUNDS: int = 2


class PipelineResult(TypedDict):
    listings: list[JobListing]
    exports: RunOutput | None


def _build_source_tasks(
    client: httpx.AsyncClient,
    query: str,
    location: str,
    *,
    per_source_cap: Optional[int],
) -> tuple[tuple[Awaitable[list[JobListing]], ...], tuple[str, ...]]:
    gh_task: Awaitable[list[JobListing]] = greenhouse.scrape(
        client, query, location, max_listings=per_source_cap
    )
    gd_task: Awaitable[list[JobListing]] = glassdoor.scrape(
        query, location, max_listings=per_source_cap
    )
    fj_task: Awaitable[list[JobListing]] = flexjobs.scrape(
        query, location, max_listings=per_source_cap
    )
    tasks: tuple[Awaitable[list[JobListing]], ...] = (gh_task, gd_task, fj_task)
    sources: tuple[str, ...] = ("greenhouse", "glassdoor", "flexjobs")
    return tasks, sources


async def _run_one_round(
    client: httpx.AsyncClient,
    query: str,
    location: str,
    *,
    per_source_cap: Optional[int],
    round_label: str,
) -> list[JobListing]:
    all_listings: list[JobListing] = []
    tasks, source_names = _build_source_tasks(
        client,
        query,
        location,
        per_source_cap=per_source_cap,
    )
    results: tuple[object, ...] = await asyncio.gather(*tasks, return_exceptions=True)
    for name, result in zip(source_names, results, strict=True):
        if isinstance(result, BaseException):
            log.error(
                "Pipeline round=%s scraper %s raised %s: %s",
                round_label,
                name,
                type(result).__name__,
                str(result),
            )
            continue
        if not isinstance(result, list):
            log.error(
                "Pipeline round=%s scraper %s returned non-list result - skipping",
                round_label,
                name,
            )
            continue
        log.info(
            "Pipeline round=%s scraper %s returned %d listings",
            round_label,
            name,
            len(result),
        )
        for idx, listing in enumerate(result):
            try:
                validate_listing(listing)
            except ValidationError as exc:
                log.warning(
                    "Pipeline round=%s scraper %s row %d failed validation (%s) - skipping row",
                    round_label,
                    name,
                    idx,
                    exc,
                )
                continue
            all_listings.append(listing)
    return all_listings


async def run_all_scrapers(
    query: str,
    location: str,
    *,
    httpx_timeout: Optional[httpx.Timeout] = None,
    httpx_limits: Optional[httpx.Limits] = None,
    max_pages_per_source: int = 0,
    max_listings: Optional[int] = None,
) -> list[JobListing]:
    timeout = httpx_timeout or httpx.Timeout(
        HTTP_TIMEOUT_SECONDS, connect=HTTP_CONNECT_TIMEOUT_SECONDS
    )
    limits = httpx_limits or httpx.Limits(
        max_connections=HTTP_MAX_CONNECTIONS,
        max_keepalive_connections=HTTP_MAX_KEEPALIVE_CONNECTIONS,
    )
    _ = max_pages_per_source
    target_cap: Optional[int] = (
        int(max_listings) if isinstance(max_listings, int) and max_listings > 0 else None
    )
    n_sources = 3
    per_source_cap_initial: Optional[int] = None
    if target_cap is not None:
        per_source_cap_initial = max(1, math.ceil(target_cap / n_sources))

    all_listings: list[JobListing] = []
    async with httpx.AsyncClient(
        timeout=timeout, limits=limits, follow_redirects=True
    ) as client:
        first = await _run_one_round(
            client,
            query,
            location,
            per_source_cap=per_source_cap_initial,
            round_label="initial",
        )
        all_listings.extend(first)
        if target_cap is None:
            return all_listings
        seen_urls: set[str] = set()
        for row in all_listings:
            u = row.get("url")
            if isinstance(u, str) and u:
                seen_urls.add(u)
        current = len(seen_urls)
        if current >= target_cap:
            return all_listings
        for round_idx in range(1, MAX_HOLDBACK_ROUNDS + 1):
            shortfall = target_cap - current
            if shortfall <= 0:
                break
            log.info(
                "Pipeline holdback round=%s: shortfall=%d (have %d of %d target); rerunning scrapers with headroom cap=%d",
                round_idx,
                shortfall,
                current,
                target_cap,
                shortfall * n_sources,
            )
            holdback_cap = shortfall * n_sources
            extra = await _run_one_round(
                client,
                query,
                location,
                per_source_cap=holdback_cap,
                round_label=f"holdback-{round_idx}",
            )
            for row in extra:
                u = row.get("url")
                if isinstance(u, str) and u in seen_urls:
                    continue
                all_listings.append(row)
                if isinstance(u, str) and u:
                    seen_urls.add(u)
            current = len(seen_urls)
            if current >= target_cap:
                break
    return all_listings


async def run_pipeline(
    query: str,
    location: str,
    *,
    config: Optional[Config] = None,
    output_dir: Optional[str] = None,
    max_listings: Optional[int] = None,
    max_pages_per_source: int = 0,
) -> PipelineResult:
    loaded_config = config or load_config()
    all_listings = await run_all_scrapers(
        query,
        location,
        max_pages_per_source=max_pages_per_source,
        max_listings=max_listings,
    )
    deduped, dropped = dedup_in_memory(all_listings)
    if isinstance(max_listings, int) and max_listings > 0 and len(deduped) > max_listings:
        before_trim = len(deduped)
        deduped = deduped[:max_listings]
        log.info(
            "Pipeline applied --max-listings cap: kept %d of %d deduped rows",
            max_listings,
            before_trim,
        )
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
        final_output_dir: str = str(loaded_config.output_dir)
    elif isinstance(output_dir, str):
        final_output_dir = output_dir
    else:
        final_output_dir = DEFAULT_OUTPUT_DIR
    try:
        exports: RunOutput | None = write_both(deduped, final_output_dir)
    except OSError as exc:
        log.error("Failed to write CSV/JSON export files: %s", exc)
        exports = None
    except (csv.Error, TypeError, ValueError) as exc:
        log.error("Export serialization failed, output files incomplete: %s", exc)
        exports = None
    return PipelineResult(listings=deduped, exports=exports)
