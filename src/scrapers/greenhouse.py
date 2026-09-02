from __future__ import annotations

import asyncio
import logging
from typing import Any, Final, Iterable, Sequence

import httpx

from ..http_utils import (
    MaxRetriesExceeded,
    fetch_with_retries,
    jitter,
    load_cookies,
    sync_cookies_to_jar,
)
from ..schema import JobListing, SalaryType, utc_now_iso

log: logging.Logger = logging.getLogger(__name__)

SOURCE_NAME: Final[str] = "greenhouse"
BASE_API_URL: Final[str] = "https://boards-api.greenhouse.io/v1/boards"

DEFAULT_BOARD_TOKENS: Final[Sequence[str]] = (
    "stripe",
    "shopify",
    "datadog",
    "gitlab",
    "coinbase",
    "notion",
    "figma",
    "airtable",
    "discord",
    "khanacademy",
)


def _pick_salary(job_data: dict[str, Any]) -> SalaryType:
    return None


def _location_name(job_data: dict[str, Any]) -> str:
    location = job_data.get("location")
    if isinstance(location, dict):
        name = location.get("name")
        if isinstance(name, str):
            return name.strip()
    if isinstance(location, str):
        return location.strip()
    return "Unknown"


def _matches_query(query: str, job_data: dict[str, Any], company: str) -> bool:
    if query == "":
        return True
    needle = query.lower()
    haystacks: list[str] = [
        str(job_data.get("title", "")),
        company.lower(),
        _location_name(job_data).lower(),
    ]
    departments = job_data.get("departments")
    if isinstance(departments, list):
        for dep in departments:
            if isinstance(dep, dict):
                name = dep.get("name")
                if isinstance(name, str):
                    haystacks.append(name.lower())
    return any(needle in h for h in haystacks)


def _matches_location(location_filter: str, resolved_location: str) -> bool:
    if location_filter == "":
        return True
    needle = location_filter.lower()
    return needle in resolved_location.lower()


def _normalize_job(
    job_data: dict[str, Any], board_company_name: str, query: str, location: str
) -> JobListing | None:
    title = job_data.get("title")
    if not isinstance(title, str) or title.strip() == "":
        return None
    absolute_url = job_data.get("absolute_url")
    if not isinstance(absolute_url, str) or not (
        absolute_url.startswith("http://") or absolute_url.startswith("https://")
    ):
        return None
    company_name = job_data.get("company_name")
    if not isinstance(company_name, str) or company_name.strip() == "":
        company_name = board_company_name
    if not _matches_query(query, job_data, company_name):
        return None
    resolved_location = _location_name(job_data)
    if not _matches_location(location, resolved_location):
        return None
    return JobListing(
        title=title.strip(),
        company=company_name.strip(),
        location=resolved_location,
        salary=_pick_salary(job_data),
        url=absolute_url,
        source=SOURCE_NAME,
        scraped_at=utc_now_iso(),
    )


def _parse_payload(
    payload: dict[str, Any], board_token: str, query: str, location: str
) -> list[JobListing]:
    jobs_value = payload.get("jobs")
    if not isinstance(jobs_value, list):
        log.warning("Greenhouse board %s: 'jobs' field was not a list - skipping", board_token)
        return []
    results: list[JobListing] = []
    for item in jobs_value:
        if not isinstance(item, dict):
            continue
        normalized = _normalize_job(item, board_token, query, location)
        if normalized is not None:
            results.append(normalized)
    return results


async def scrape(
    client: httpx.AsyncClient,
    query: str,
    location: str,
    *,
    board_tokens: Iterable[str] = DEFAULT_BOARD_TOKENS,
) -> list[JobListing]:
    cookie_jar: dict[str, str] = load_cookies(SOURCE_NAME)
    if cookie_jar:
        client.cookies.update(cookie_jar)
    all_results: list[JobListing] = []
    tokens = list(board_tokens)
    for idx, token in enumerate(tokens):
        url = f"{BASE_API_URL}/{token}/jobs"
        params: dict[str, str] = {"content": "false"}
        try:
            response = await fetch_with_retries(
                client,
                url,
                SOURCE_NAME,
                referer=f"https://boards.greenhouse.io/{token}",
                params=params,
            )
        except MaxRetriesExceeded as exc:
            log.error("Greenhouse board %s failed permanently: %s", token, exc)
            continue
        try:
            payload: dict[str, Any] = response.json()
        except ValueError as exc:
            log.error(
                "Greenhouse board %s returned invalid JSON: %s (status=%s)",
                token,
                exc,
                response.status_code,
            )
            continue
        sync_cookies_to_jar(
            SOURCE_NAME,
            cookie_jar,
            {k: v for k, v in response.cookies.items()},
        )
        normalized = _parse_payload(payload, token, query, location)
        log.info(
            "Greenhouse board=%s status=%s jobs_matched=%d of %d raw",
            token,
            response.status_code,
            len(normalized),
            len(payload.get("jobs", [])) if isinstance(payload.get("jobs"), list) else 0,
        )
        all_results.extend(normalized)
        if idx < len(tokens) - 1:
            await asyncio.sleep(jitter())
    return all_results
