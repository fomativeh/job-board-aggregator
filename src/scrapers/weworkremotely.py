from __future__ import annotations

import asyncio
import logging
import re
from typing import Final, Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup, Tag

from ..http_utils import (
    MaxRetriesExceeded,
    fetch_with_retries,
    jitter,
    load_cookies,
    sync_cookies_to_jar,
)
from ..schema import JobListing, SalaryType, utc_now_iso

log: logging.Logger = logging.getLogger(__name__)

SOURCE_NAME: Final[str] = "weworkremotely"
BASE_URL: Final[str] = "https://weworkremotely.com"
SEARCH_URL: Final[str] = f"{BASE_URL}/remote-jobs/search"

LISTING_SELECTOR: Final[str] = "li.new-listing-container:not(.listing-ad)"
TITLE_SPAN_SELECTOR: Final[str] = ".new-listing__header__title__text"
TITLE_FALLBACK_SELECTOR: Final[str] = "h3.new-listing__header__title"
COMPANY_SELECTOR: Final[str] = "p.new-listing__company-name"
HQ_SELECTOR: Final[str] = "p.new-listing__company-headquarters"
CATEGORY_SELECTOR: Final[str] = ".new-listing__categories__category"
LINK_SELECTOR: Final[str] = "a.listing-link, a.listing-link--unlocked"

SALARY_RE: Final[re.Pattern[str]] = re.compile(
    r"(\$[\d,]+\s*[-–—]\s*\$[\d,]+\s*(?:USD|EUR|GBP)?)\b|\$[\d,]+\s*or\s+more\s*(?:USD|EUR|GBP)?"
)
HOURLY_RE: Final[re.Pattern[str]] = re.compile(r"\$[\d.]+\s*/hr\s*\+?")


def _text(tag: Optional[Tag]) -> str:
    if tag is None:
        return ""
    return tag.get_text(" ", strip=True)


def _pick_location(li: Tag, hq_text: str) -> str:
    parts: list[str] = []
    if hq_text:
        parts.append(hq_text)
    cats = li.select(CATEGORY_SELECTOR)
    for c in cats:
        t = _text(c)
        if not t:
            continue
        lowered = t.lower()
        if "anywhere" in lowered or "only" in lowered or "world" in lowered or "europe" in lowered or "asia" in lowered or "america" in lowered:
            parts.append(t)
            break
    if not parts:
        return "Remote"
    return " — ".join(dict.fromkeys(parts))


def _pick_salary(li: Tag, hq_text: str) -> SalaryType:
    cats = [_text(c) for c in li.select(CATEGORY_SELECTOR)]
    haystacks = cats + [hq_text] + [_text(li)]
    for hay in haystacks:
        m = SALARY_RE.search(hay) or HOURLY_RE.search(hay)
        if m:
            return m.group(0).strip()
    return None


def _matches_query(query: str, title: str, company: str, location: str) -> bool:
    if query == "":
        return True
    needle = query.lower()
    return any(needle in h.lower() for h in (title, company, location))


def _matches_location(location_filter: str, location: str) -> bool:
    if location_filter == "":
        return True
    needle = location_filter.lower()
    return needle in location.lower()


def _parse_listing(li: Tag, query: str, location_filter: str) -> JobListing | None:
    link = li.select_one(LINK_SELECTOR)
    if not isinstance(link, Tag) or not link.has_attr("href"):
        return None
    rel_href = link["href"]
    if not isinstance(rel_href, str) or rel_href == "":
        return None
    absolute_url = urljoin(BASE_URL + "/", rel_href)

    title_span = li.select_one(TITLE_SPAN_SELECTOR)
    title_text = _text(title_span)
    if not title_text:
        title_text = _text(li.select_one(TITLE_FALLBACK_SELECTOR))
    if not title_text:
        return None

    company = _text(li.select_one(COMPANY_SELECTOR))
    if not company:
        return None
    hq = _text(li.select_one(HQ_SELECTOR))

    hq_clean = re.split(r"\s{2,}", hq, maxsplit=1)[0].strip()

    location = _pick_location(li, hq_clean)
    salary = _pick_salary(li, hq_clean)

    if not _matches_query(query, title_text, company, location):
        return None
    if not _matches_location(location_filter, location):
        return None

    return JobListing(
        title=title_text.strip(),
        company=company.strip(),
        location=location,
        salary=salary,
        url=absolute_url,
        source=SOURCE_NAME,
        scraped_at=utc_now_iso(),
    )


def parse_html(html: str, query: str, location_filter: str) -> list[JobListing]:
    soup = BeautifulSoup(html, "lxml")
    results: list[JobListing] = []
    for li in soup.select(LISTING_SELECTOR):
        if not isinstance(li, Tag):
            continue
        normalized = _parse_listing(li, query, location_filter)
        if normalized is not None:
            results.append(normalized)
    return results


async def scrape(
    client: httpx.AsyncClient,
    query: str,
    location: str,
) -> list[JobListing]:
    cookie_jar: dict[str, str] = load_cookies(SOURCE_NAME)
    if cookie_jar:
        client.cookies.update(cookie_jar)
    params: dict[str, str] = {}
    if query:
        params["term"] = query
    try:
        response = await fetch_with_retries(
            client,
            SEARCH_URL,
            SOURCE_NAME,
            referer=BASE_URL + "/",
            params=params or None,
        )
    except MaxRetriesExceeded as exc:
        log.error("WeWorkRemotely scraper failed permanently: %s", exc)
        return []
    if response.status_code != 200:
        log.error(
            "WeWorkRemotely search returned status=%s query=%r — skipping parse",
            response.status_code,
            query,
        )
        return []
    sync_cookies_to_jar(
        SOURCE_NAME,
        cookie_jar,
        {k: v for k, v in response.cookies.items()},
    )
    listings = parse_html(response.text, query, location)
    log.info(
        "WeWorkRemotely query=%r status=%s listings_parsed=%d",
        query,
        response.status_code,
        len(listings),
    )
    await asyncio.sleep(jitter())
    return listings
