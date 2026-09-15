from __future__ import annotations

import asyncio
import csv
import logging
import math
import random
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
from .scrapers import glassdoor, greenhouse
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

    return [
        ("greenhouse", gh),
        ("glassdoor", gd),
    ]


def pick_backup_scrapers_by_quota(
    delivered: dict[str, int],
    target_cap: int,
    *,
    n_sources: int = 2,
    exhausted: Optional[set[str]] = None,
    top_k: Optional[int] = 1,
) -> list[str]:
    per_source_share = max(1, math.ceil(target_cap / n_sources))
    ranked: list[tuple[float, int, str]] = []
    skip = exhausted if isinstance(exhausted, set) else set()
    for name, count in delivered.items():
        if name in skip:
            continue
        ratio: float = count / per_source_share if per_source_share else 0.0
        ranked.append((ratio, count, name))
    ranked.sort(key=lambda t: (t[0], -t[1], t[2]))
    eligible = [name for ratio, _count, name in ranked if ratio < 1.0 or _count == 0]
    if isinstance(top_k, int) and top_k > 0 and len(eligible) > top_k:
        return eligible[:top_k]
    return eligible


async def _run_selected(
    client: httpx.AsyncClient,
    query: str,
    location: str,
    *,
    per_source_cap: Optional[int] | dict[str, int],
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
        cap = per_source_cap
        if isinstance(cap, dict):
            cap = cap.get(name, None)
        coro = builder(client, query, location, cap)
        task = asyncio.create_task(coro, name=f"scraper:{name}:{round_label}")
        tasks.append(task)
        source_order.append(name)
    results: tuple[object, ...] = await asyncio.gather(*tasks, return_exceptions=True)
    for name, result in zip(source_order, results, strict=True):
        if isinstance(result, BaseException):
            log.error(
                "[ERR] round=%s src=%s %s: %s",
                round_label,
                name,
                type(result).__name__,
                str(result),
            )
            continue
        if not isinstance(result, list):
            log.error(
                "[ERR] round=%s src=%s returned non-list - skip",
                round_label,
                name,
            )
            continue
        log.info(
            "round=%s src=%s => %d rows",
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
                    "[WARN] round=%s src=%s row=%d invalid: %s - skip",
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
                "round=%s src=%s kept %d/%d after validation",
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
    per_source_cap_initial: Optional[int] | dict[str, int] = None
    if target_cap is not None:
        base = target_cap // n_sources
        remainder = target_cap % n_sources
        names = [name for name, _ in builders]
        caps: dict[str, int] = {n: base for n in names}
        if remainder > 0:
            lucky = random.sample(names, k=remainder)
            for n in lucky:
                caps[n] = base + 1
        for n in names:
            if caps[n] < 1:
                caps[n] = 1
        per_source_cap_initial = caps
        log.info(
            "Quota split target=%d n_sources=%d base=%d remainder=%d -> per-source caps=%s",
            target_cap, n_sources, base, remainder, caps,
        )

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
        exhausted_sources: set[str] = {n for n, c in delivered_initial.items() if c <= 0}
        if exhausted_sources:
            log.info(
                "Pipeline holdback: marking scrapers exhausted (initial 0 rows, never re-open): %s",
                sorted(exhausted_sources),
            )
        for round_idx in range(1, MAX_HOLDBACK_ROUNDS + 1):
            shortfall = target_cap - current
            if shortfall <= 0:
                break
            rerun_names = pick_backup_scrapers_by_quota(
                running_counts, target_cap, n_sources=n_sources,
                exhausted=exhausted_sources, top_k=1,
            )
            if not rerun_names:
                log.info(
                    "Pipeline holdback round=%s: no eligible non-exhausted scraper (all satisfied/empty); skip",
                    round_idx,
                )
                break
            selected_builders: list[tuple[str, ScrapeBuilder]] = [
                (name, builder) for (name, builder) in builders if name in rerun_names
            ]
            sel_count = max(1, len(selected_builders))
            headroom_base = shortfall // sel_count
            headroom_rem = shortfall % sel_count
            headroom_caps: dict[str, int] = {}
            sel_names = [name for name, _ in selected_builders]
            for n in sel_names:
                headroom_caps[n] = headroom_base
            if headroom_rem > 0 and sel_names:
                lucky2 = random.sample(sel_names, k=headroom_rem)
                for n in lucky2:
                    headroom_caps[n] = headroom_caps[n] + 1
            per_source_headroom_floor = (
                per_source_cap_initial
                if isinstance(per_source_cap_initial, int) and per_source_cap_initial > 0
                else max(per_source_cap_initial.values()) if isinstance(per_source_cap_initial, dict) and per_source_cap_initial else 1
            )
            for n in sel_names:
                headroom_caps[n] = max(
                    per_source_headroom_floor,
                    headroom_caps[n] * 2,
                )
            headroom_per_source: Optional[int] | dict[str, int] = headroom_caps
            log.info(
                "Pipeline holdback round=%s: shortfall=%d (have %d of %d target); "
                "rerunning ONLY scrapers=%s (skipping satisfied=%s) with per-source headroom caps=%s",
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
            if new_kept == 0 and len(selected_builders) == 1:
                only = selected_builders[0][0]
                exhausted_sources.add(only)
                log.info(
                    "Pipeline holdback round=%s: %s rerun delivered 0 new uniques -> mark exhausted (never re-opens)",
                    round_idx, only,
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
        "raw=%d deduped=%d dropped=%d",
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
                "mongo write: inserted=%d already_stored=%d",
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
