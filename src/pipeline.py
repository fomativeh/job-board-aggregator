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
    return (gh_task, gd_task, fj_task), ("greenhouse", "glassdoor", "flexjobs")


async def _run_all_sources(
    tasks: tuple[Awaitable[list[JobListing]], ...],
    names: tuple[str, ...],
) -> list[JobListing]:
    pending: list[Awaitable[list[JobListing]]] = list(tasks)
    pending_names: list[str] = list(names)
    all_results: list[JobListing] = []
    while pending:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        finished = list(done)
        for fut in finished:
            idx = list(pending).index(fut) if fut in pending else -1
            name = pending_names[idx] if idx >= 0 and idx < len(pending_names) else "unknown"
            try:
                rows = fut.result()
            except Exception as exc:  # noqa: BLE001
                log.error("Scraper %s raised %s: %s — skipping", name, type(exc).__name__, exc)
                continue
            log.info("Scraper %s returned %d rows", name, len(rows))
            all_rows = list(rows)
            for r in all_rows:
                try:
                    validate_listing(r)
                except ValidationError as ve:
                    log.warning("Invalid row from %s dropped: %s -> %r", name, ve, r)
                    all_rows = [x for x in all_rows if x is not r]
            all_results.extend(all_rows)
    return all_results


async def run_pipeline(
    query: str,
    location: str,
    *,
    config: Config | None = None,
    max_listings: Optional[int] = None,
    max_pages_per_source: int = 0,
) -> PipelineResult:
    _ = csv
    _ = math
    _ = max_pages_per_source
    if config is None:
        config = load_config()
    target_cap: Optional[int] = int(max_listings) if isinstance(max_listings, int) and max_listings > 0 else None
    per_source_cap: Optional[int] = None
    if target_cap is not None:
        per_source_cap = max(1, int(math.ceil(target_cap * MAX_HOLDBACK_ROUNDS / 3)))
    transport = httpx.AsyncHTTPTransport(
        limits=httpx.Limits(
            max_connections=HTTP_MAX_CONNECTIONS,
            max_keepalive_connections=HTTP_MAX_KEEPALIVE_CONNECTIONS,
        )
    )
    timeout = httpx.Timeout(
        HTTP_TIMEOUT_SECONDS,
        connect=HTTP_CONNECT_TIMEOUT_SECONDS,
    )
    listings: list[JobListing] = []
    exports: RunOutput | None = None
    async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
        tasks, names = _build_source_tasks(
            client,
            query,
            location,
            per_source_cap=per_source_cap,
        )
        log.info(
            "Running 3 scrapers (sources=%s target_cap=%s per_source_cap=%s)",
            names, target_cap, per_source_cap,
        )
        all_rows = await _run_all_sources(tasks, names)
        if not all_rows:
            log.warning("Pipeline got 0 rows from all 3 sources")
        deduped, dropped = dedup_in_memory(all_rows)
        if target_cap is not None and len(deduped) > target_cap:
            log.info("Truncating %d deduped rows to target_cap=%d", len(deduped), target_cap)
            deduped = deduped[:target_cap]
        inserted = 0
        duplicates = 0
        store: Storage | None = None
        try:
            store = Storage(config)
            store.connect()
            inserted, duplicates = store.insert_many_unique(deduped)
        except MongoConnectionError as exc:
            log.warning("MongoDB unavailable, skipping persistence: %s", exc)
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:
                    pass
        try:
            output_dir_arg = str(getattr(config, "output_dir", DEFAULT_OUTPUT_DIR))
            exports = write_both(deduped, output_dir=output_dir_arg)
        except Exception as exc:  # noqa: BLE001
            log.error("Export failed: %s: %s", type(exc).__name__, exc)
            exports = None
        log.info(
            "Pipeline complete: total_rows=%d deduped=%d inserted=%s duplicates=%s exported=%s dropped=%s",
            len(all_rows),
            len(deduped),
            inserted,
            duplicates,
            exports is not None,
            dropped,
        )
    return {"listings": deduped, "exports": exports}
