from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Final, Iterable, Optional, Sequence

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

HTTP_TIMEOUT_TOTAL: Final[float] = 20.0
HTTP_TIMEOUT_CONNECT: Final[float] = 10.0
BOARD_MAX_PAGES_PER_BOARD: Final[int] = 50
BOARD_PAGE_SIZE: Final[int] = 500
BOARD_SCRAPER_TOTAL_TIMEOUT_SECONDS: Final[int] = 600

DEFAULT_BOARD_TOKENS: Final[Sequence[str]] = (
    "stripe",
    "datadog",
    "gitlab",
    "coinbase",
    "figma",
    "airtable",
    "discord",
    "khanacademy",
)


def _pick_salary(job_data: dict[str, Any]) -> SalaryType:
    salary_range = job_data.get("salary_range")
    if not isinstance(salary_range, dict):
        return None
    min_val = salary_range.get("min")
    max_val = salary_range.get("max")
    currency = salary_range.get("currency") or ""
    if not isinstance(min_val, (int, float)) or not isinstance(max_val, (int, float)):
        return None

    def _fmt(v: int | float) -> str:
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        if isinstance(v, int):
            return f"${v:,}"
        return f"${v:,.2f}"

    parts: list[str] = [f"{_fmt(min_val)} - {_fmt(max_val)}"]
    if isinstance(currency, str) and currency:
        parts.append(currency)
    return " ".join(parts)


def _location_name(job_data: dict[str, Any]) -> str:
    location = job_data.get("location")
    if isinstance(location, dict):
        name = location.get("name")
        if isinstance(name, str):
            return name.strip()
    if isinstance(location, str):
        return location.strip()
    return "Unknown"


TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"\w+")

REMOTE_SYNONYMS: Final[frozenset[str]] = frozenset(
    s.casefold()
    for s in (
        "remote",
        "anywhere",
        "worldwide",
        "global",
        "distributed",
        "telecommute",
        "telecommuting",
        "virtual",
        "work from anywhere",
        "wfa",
        "wfh",
        "fully remote",
        "100% remote",
        "100 remote",
        "any location",
    )
)


def _tokens(s: str) -> set[str]:
    return {t.casefold() for t in TOKEN_RE.findall(s)}


def _matches_query(query: str, job_data: dict[str, Any], company: str) -> bool:
    if query == "":
        return True
    query_tokens = _tokens(query)
    if not query_tokens:
        return True
    haystack_parts: list[str] = [
        str(job_data.get("title", "")),
        company,
        _location_name(job_data),
    ]
    departments = job_data.get("departments")
    if isinstance(departments, list):
        for dep in departments:
            if isinstance(dep, dict):
                name = dep.get("name")
                if isinstance(name, str):
                    haystack_parts.append(name)
    metadata = job_data.get("metadata")
    if isinstance(metadata, list):
        for item in metadata:
            if isinstance(item, dict):
                for key in ("name", "value"):
                    value = item.get(key)
                    if isinstance(value, str):
                        haystack_parts.append(value)
    content_html = job_data.get("content")
    if isinstance(content_html, str) and content_html:
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(content_html, "lxml")
            text = soup.get_text(" ", strip=True)
            if text:
                haystack_parts.append(text)
        except Exception:
            haystack_parts.append(content_html)
    haystack_tokens = _tokens(" ".join(haystack_parts))
    if query_tokens.issubset(haystack_tokens):
        return True
    return len(query_tokens & haystack_tokens) >= max(1, len(query_tokens) // 2)


def _matches_location(location_filter: str, resolved_location: str) -> bool:
    if location_filter == "":
        return True
    filter_tokens = _tokens(location_filter)
    if not filter_tokens:
        return True
    location_tokens = _tokens(resolved_location)
    if filter_tokens.issubset(location_tokens):
        return True
    haystack_lower = resolved_location.lower()
    filter_is_remote = bool(REMOTE_SYNONYMS & filter_tokens) or any(
        tok in "remote" for tok in filter_tokens
    )
    if filter_is_remote:
        for token in REMOTE_SYNONYMS:
            if token in haystack_lower:
                return True
    if location_filter.lower() in resolved_location.lower():
        return True
    return False


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


async def fetch_json_with_retries(
    client: httpx.AsyncClient,
    board_token: str,
    *,
    page: int = 1,
) -> tuple[int, dict[str, Any]]:
    url = f"{BASE_API_URL}/{board_token}/jobs"
    params: dict[str, str] = {"content": "true"}
    if page and int(page) > 1:
        params["page"] = str(int(page))
    response = await fetch_with_retries(
        client,
        url,
        SOURCE_NAME,
        referer=f"https://boards.greenhouse.io/{board_token}",
        params=params,
    )
    try:
        payload: dict[str, Any] = response.json()
    except ValueError as exc:
        raise MaxRetriesExceeded(
            f"board={board_token} page={page} returned invalid JSON: {exc} (status={response.status_code})"
        ) from exc
    return int(response.status_code), payload


async def scrape(
    client: httpx.AsyncClient,
    query: str,
    location: str,
    *,
    board_tokens: Iterable[str] = DEFAULT_BOARD_TOKENS,
    max_listings: Optional[int] = None,
) -> list[JobListing]:
    cookie_jar: dict[str, str] = load_cookies(SOURCE_NAME)
    if cookie_jar:
        client.cookies.update(cookie_jar)
    all_results: list[JobListing] = []
    tokens = list(board_tokens)
    target_cap: Optional[int] = int(max_listings) if isinstance(max_listings, int) and max_listings > 0 else None
    start_ts = time.monotonic()
    total_fetches = 0
    total_pages_walked = 0
    stop_reason = "all_pages_exhausted"

    def _stop_now() -> Optional[str]:
        if target_cap is not None and len(all_results) >= target_cap:
            return "max_listings_satisfied"
        if time.monotonic() - start_ts >= BOARD_SCRAPER_TOTAL_TIMEOUT_SECONDS:
            return "scraper_timeout"
        return None

    for idx, token in enumerate(tokens):
        early = _stop_now()
        if early is not None:
            stop_reason = early
            log.info(
                "Greenhouse stop at board=%s/%s: stop_reason=%s rows=%d cap=%s",
                idx,
                len(tokens),
                early,
                len(all_results),
                target_cap,
            )
            break
        board_raw_total = 0
        board_matched_total = 0
        board_pages_fetched = 0
        board_exhausted = False
        for page in range(1, BOARD_MAX_PAGES_PER_BOARD + 1):
            if board_exhausted:
                break
            early = _stop_now()
            if early is not None:
                stop_reason = early
                break
            try:
                status, payload = await fetch_json_with_retries(client, token, page=page)
            except MaxRetriesExceeded as exc:
                log.error("Greenhouse board=%s page=%s failed permanently: %s", token, page, exc)
                break
            total_fetches += 1
            board_pages_fetched += 1
            total_pages_walked += 1
            sync_cookies_to_jar(
                SOURCE_NAME,
                cookie_jar,
                {k: v for k, v in (getattr(getattr(payload, "cookies", None) or {}, "items", lambda: [])())},
            )
            cookies_from_response = getattr(client.cookies, "jar", None)
            if cookies_from_response is not None:
                try:
                    from http.cookiejar import CookieJar

                    if isinstance(cookies_from_response, CookieJar):
                        snapshot: dict[str, str] = {}
                        for c in cookies_from_response:
                            try:
                                if isinstance(c.name, str) and isinstance(c.value, str):
                                    snapshot[c.name] = c.value
                            except Exception:
                                pass
                        if snapshot:
                            sync_cookies_to_jar(SOURCE_NAME, cookie_jar, snapshot)
                except Exception:
                    pass
            jobs = payload.get("jobs") if isinstance(payload, dict) else None
            meta = payload.get("meta") if isinstance(payload, dict) else None
            if not isinstance(jobs, list):
                log.warning(
                    "Greenhouse board=%s page=%s status=%s jobs field missing/non-list - stopping board pagination",
                    token,
                    page,
                    status,
                )
                jobs = []
                board_exhausted = True
            raw_len = len(jobs)
            board_raw_total += raw_len
            matched_this_page = 0
            for item in jobs:
                early = _stop_now()
                if early is not None:
                    stop_reason = early
                    break
                if not isinstance(item, dict):
                    continue
                normalized = _normalize_job(item, token, query, location)
                if normalized is not None:
                    all_results.append(normalized)
                    matched_this_page += 1
                    board_matched_total += 1
            log.info(
                "Greenhouse board=%s page=%s/%s status=%s raw_batch=%d batch_matched=%d running_total=%d cap=%s",
                token,
                page,
                BOARD_MAX_PAGES_PER_BOARD,
                status,
                raw_len,
                matched_this_page,
                len(all_results),
                target_cap,
            )
            if raw_len < BOARD_PAGE_SIZE and page > 1:
                board_exhausted = True
            elif raw_len == 0:
                board_exhausted = True
            if isinstance(meta, dict):
                total = meta.get("total")
                if isinstance(total, int) and board_raw_total >= total:
                    board_exhausted = True
                for k in ("total_pages", "page_total", "pages"):
                    cand = meta.get(k)
                    if isinstance(cand, int) and page >= cand:
                        board_exhausted = True
                        break
            if idx < len(tokens) - 1 or page < BOARD_MAX_PAGES_PER_BOARD:
                await asyncio.sleep(jitter())
            if _stop_now() is not None:
                break
        if idx < len(tokens) - 1:
            early = _stop_now()
            if early is not None:
                stop_reason = early
                break
    elapsed_s = round(time.monotonic() - start_ts, 2)
    log.info(
        "Greenhouse aggregate: rows=%d cap=%s stop_reason=%s elapsed_s=%s fetches=%s pages_walked=%s boards_total=%s",
        len(all_results),
        target_cap,
        stop_reason,
        elapsed_s,
        total_fetches,
        total_pages_walked,
        len(tokens),
    )
    return all_results
