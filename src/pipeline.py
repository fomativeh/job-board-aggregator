from __future__ import annotations

import asyncio
import csv
import logging
import math
from typing import Awaitable, Callable, Optional, Sequence, TypedDict

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

ScrapeBuilder = Callable[[httpx.AsyncClient, str, str, Optional[int]], Awaitable[list[JobListing]]]


class PipelineResult(TypedDict):
    listings: list[JobListing]
    exports: RunOutput | None


def _default_builders() -> list[tuple[str, ScrapeBuilder]]:
    def gh(client: httpx.AsyncClient, q: str, loc: str, cap: Optional[int]) -> Awaitable[list[JobListing]]:
        return greenhouse.scrape(client, q, loc, max_listings=cap)

    def gd(client: httpx.AsyncClient, q: str, loc: str, cap: Optional[int]) -> Awaitable[list[JobListing]]:
        _ = client
        return glassdoor.scrape(q, loc, max_listings=cap)

    def fj(client: httpx.AsyncClient, q: str, loc: str, cap: Optional[int]) -> Awaitable[list[JobListing]]:
        _ = client
        return flexjobs.scrape(q, loc, max_listings=cap)

    return [
        ("greenhouse", gh),
        ("glassdoor", gd),
        ("flexjobs", fj),
    ]


def pick_backup_scrapers_by_quota(
    delivered: dict[str, int],
    target_cap: int,
    *,
    n_sources: int = 3,
) -> list[str]:
    per_source_share = max(1, math.ceil(target_cap / n_sources))
    ranked: list[tuple[float, int, str]] = []
    for name, count in delivered.items():
        ratio: float = count / per_source_share if per_source_share else 0.0
        ranked.append((ratio, count, name))
    ranked.sort(key=lambda t: (t[0], -t[1], t[2]))
    return [name for ratio, _count, name in ranked if ratio < 1.0 or _count == 0]


async def _run_selected(
    client: httpx.AsyncClient,
    query: str,
    location: str,
    *,
    per_source_cap: Optional[int],
    round_label: str,
    scrape_builders: Sequence[tuple[str, ScrapeBuilder]],
) -> tuple[list[JobListing], dict[str, int]]:
    all_listings: list[JobListing] = []
    per_source_counts: dict[str, int] = {name: 0 for name, _ in scrape_builders}
    if not scrape_builders:
        return all_listings, per_source_counts
    tasks: list[Awaitable[list[JobListing]]] = []
    source_order: list[str] = []
    for name, builder in scrape_builders:
        tasks.append(builder(client, query, location, per_source_cap))
        source_order.append(name)
    results: tuple[object, ...] = await asyncio.gather(*tasks, return_exceptions=True)
    for name, result in zip(source_order, results, strict=True):
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
        per_source_counts[name] = len(result)
        kept = 0
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
            kept += 1
        if kept != len(result):
            log.info(
                "Pipeline round=%s scraper %s kept %d of %d raw listings after validation",
                round_label,
                name,
                kept,
                len(result),
            )
    return all_listings, per_source_counts


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
    builders = _default_builders()
    n_sources = len(builders)
    per_source_cap_initial: Optional[int] = None
    if target_cap is not None:
        per_source_cap_initial = max(1, math.ceil(target_cap / n_sources))

    all_listings: list[JobListing] = []
    async with httpx.AsyncClient(
        timeout=timeout, limits=limits, follow_redirects=True
    ) as client:
        first, delivered_initial = await _run_selected(
            client,
            query,
            location,
            per_source_cap=per_source_cap_initial,
            round_label="initial",
            scrape_builders=builders,
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
        running_counts: dict[str, int] = dict(delivered_initial)
        for round_idx in range(1, MAX_HOLDBACK_ROUNDS + 1):
            shortfall = target_cap - current
            if shortfall <= 0:
                break
            rerun_names = pick_backup_scrapers_by_quota(
                running_counts, target_cap, n_sources=n_sources
            )
            if not rerun_names:
                log.info(
                    "Pipeline holdback round=%s: all scrapers met their per-source quota; skipping redundant rerun",
                    round_idx,
                )
                break
            selected_builders: list[tuple[str, ScrapeBuilder]] = [
                (name, builder) for (name, builder) in builders if name in rerun_names
            ]
            headroom_per_source = max(
                per_source_cap_initial or 1,
                math.ceil(shortfall / max(1, len(selected_builders))) * 2,
            )
            log.info(
                "Pipeline holdback round=%s: shortfall=%d (have %d of %d target); "
                "rerunning ONLY scrapers=%s (skipping satisfied=%s) with per-source headroom cap=%d",
                round_idx,
                shortfall,
                current,
                target_cap,
                rerun_names,
                [name for name, _ in builders if name not in rerun_names],
                headroom_per_source,
            )
            extra, extra_counts = await _run_selected(
                client,
                query,
                location,
                per_source_cap=headroom_per_source,
                round_label=f"holdback-{round_idx}",
                scrape_builders=selected_builders,
            )
            for name, n in extra_counts.items():
                running_counts[name] = running_counts.get(name, 0) + n
            new_kept = 0
            for row in extra:
                u = row.get("url")
                if isinstance(u, str) and u in seen_urls:
                    continue
                all_listings.append(row)
                new_kept += 1
                if isinstance(u, str) and u:
                    seen_urls.add(u)
            log.info(
                "Pipeline holdback round=%s kept %d new unique listings of %d raw extra",
                round_idx,
                new_kept,
                len(extra),
            )
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
