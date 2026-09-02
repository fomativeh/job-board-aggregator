from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Optional, cast
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup, Tag
from playwright._impl._api_structures import SetCookieParam
from playwright.async_api import (
    Browser,
    BrowserContext,
    BrowserType,
    Cookie,
    Error as PlaywrightError,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    ViewportSize,
    async_playwright,
)

from ..http_utils import USER_AGENTS, jitter, load_cookies, save_cookies
from ..schema import JobListing, SalaryType, utc_now_iso

log: logging.Logger = logging.getLogger(__name__)

SOURCE_NAME: Final[str] = "weworkremotely"
BASE_URL: Final[str] = "https://weworkremotely.com"
SEARCH_URL: Final[str] = f"{BASE_URL}/remote-jobs/search"

NAVIGATE_TIMEOUT_MS: Final[int] = 60_000
VIEWPORT: Final[ViewportSize] = {"width": 1366, "height": 768}
LANGS: Final[str] = "en-US,en;q=0.7"

SESSION_DIR: Final[Path] = Path(__file__).resolve().parent.parent.parent / "session"

LISTING_SELECTOR: Final[str] = "li.new-listing-container:not(.listing-ad)"
TITLE_SPAN_SELECTOR: Final[str] = ".new-listing__header__title__text"
TITLE_FALLBACK_SELECTOR: Final[str] = "h3.new-listing__header__title"
COMPANY_SELECTOR: Final[str] = "p.new-listing__company-name"
HQ_SELECTOR: Final[str] = "p.new-listing__company-headquarters"
CATEGORY_SELECTOR: Final[str] = ".new-listing__categories__category"
LINK_SELECTOR: Final[str] = "a.listing-link, a.listing-link--unlocked"

TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"\w+")

SALARY_RE: Final[re.Pattern[str]] = re.compile(
    r"(\$[\d,]+\s*[-–-]\s*\$[\d,]+\s*(?:USD|EUR|GBP)?)\b|\$[\d,]+\s*or\s+more\s*(?:USD|EUR|GBP)?"
)
HOURLY_RE: Final[re.Pattern[str]] = re.compile(r"\$[\d.]+\s*/hr\s*\+?")


@dataclass(frozen=True)
class _StealthPayload:
    ua: str
    vendor_sub: str
    platform: str


def _pick_stealth() -> _StealthPayload:
    ua = random.choice(USER_AGENTS)
    return _StealthPayload(
        ua=ua,
        vendor_sub="Google Inc. (Intel)",
        platform="Win32",
    )


_STEALTH_INIT: Final[str] = r"""
// Applied before any page script runs so headless-chrome fingerprints are
// masked the moment WeWorkRemotely's first inline <script> executes.
(() => {
  const ua = "STEALTH_UA";
  const platform = "STEALTH_PLATFORM";
  const vendor = "Google Inc.";
  Object.defineProperty(navigator, "webdriver", {
    get() { return false; },
    configurable: true,
  });
  Object.defineProperty(navigator, "userAgent", {
    get() { return ua; },
    configurable: true,
  });
  Object.defineProperty(navigator, "platform", {
    get() { return platform; },
    configurable: true,
  });
  Object.defineProperty(navigator, "vendor", {
    get() { return vendor; },
    configurable: true,
  });
  Object.defineProperty(navigator, "languages", {
    get() { return ["en-US", "en"]; },
    configurable: true,
  });
  Object.defineProperty(navigator, "plugins", {
    get() {
      return [
        { name: "Chrome PDF Plugin", filename: "internal-pdf-viewer" },
        { name: "Chrome PDF Viewer", filename: "mhjfbmdgcfjbbpaeojofohoefgiehjai" },
        { name: "Native Client", filename: "internal-nacl-plugin" },
      ];
    },
    configurable: true,
  });
  Object.defineProperty(navigator, "maxTouchPoints", {
    get() { return 0; },
    configurable: true,
  });
  if (window.Permissions && window.navigator && typeof navigator.permissions !== "undefined") {
    const orig = window.Permissions.prototype.query;
    if (orig) {
      window.Permissions.prototype.query = function patchedQuery() {
        return orig.apply(this, arguments).then((res) => {
          if (res && res.name === "notifications") {
            Object.defineProperty(res, "state", { get() { return "default"; } });
          }
          return res;
        });
      };
    }
  }
  Object.defineProperty(navigator, "hardwareConcurrency", {
    get() { return 8; },
    configurable: true,
  });
  Object.defineProperty(navigator, "deviceMemory", {
    get() { return 8; },
    configurable: true,
  });
})();
"""


def _build_stealth_script(s: _StealthPayload) -> str:
    return (
        _STEALTH_INIT.replace("STEALTH_UA", s.ua)
        .replace("STEALTH_PLATFORM", s.platform)
    )


async def _apply_cookies_to_context(ctx: BrowserContext, cookies_jar: dict[str, str]) -> None:
    if not cookies_jar:
        return
    def _cookie(name: str, value: str, *, domain: str) -> SetCookieParam:
        return cast(SetCookieParam, {"name": name, "value": value, "domain": domain, "path": "/"})
    wwr_cookies: list[SetCookieParam] = [
        _cookie(k, v, domain=".weworkremotely.com")
        for k, v in cookies_jar.items()
        if isinstance(k, str) and isinstance(v, str)
    ]
    wwr_cookies.extend(
        _cookie(k, v, domain="weworkremotely.com")
        for k, v in cookies_jar.items()
        if isinstance(k, str) and isinstance(v, str)
    )
    if wwr_cookies:
        await ctx.add_cookies(wwr_cookies)


def _cookies_from_context(cookies: list[Cookie]) -> dict[str, str]:
    result: dict[str, str] = {}
    for c in cookies:
        name = c.get("name")
        value = c.get("value")
        if isinstance(name, str) and isinstance(value, str):
            result[name] = value
    return result


async def _launch_context(playwright: Playwright) -> tuple[Browser, BrowserContext]:
    chromium: BrowserType = playwright.chromium
    stealth = _pick_stealth()
    browser: Browser = await chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
        ],
    )
    ctx: BrowserContext = await browser.new_context(
        user_agent=stealth.ua,
        viewport=VIEWPORT,
        locale="en-US",
        extra_http_headers={
            "Accept-Language": LANGS,
            "Sec-CH-UA": '"Chromium";v="128", "Not A(Brand";v="24", "Google Chrome";v="128"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        },
    )
    await ctx.add_init_script(_build_stealth_script(stealth))
    return browser, ctx


def _tokens(s: str) -> set[str]:
    return {t.casefold() for t in TOKEN_RE.findall(s)}


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
    return " - ".join(dict.fromkeys(parts))


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
    query_tokens = _tokens(query)
    if not query_tokens:
        return True
    haystack_tokens = _tokens(f"{title} {company} {location}")
    return query_tokens.issubset(haystack_tokens)


def _matches_location(location_filter: str, location: str) -> bool:
    if location_filter == "":
        return True
    filter_tokens = _tokens(location_filter)
    if not filter_tokens:
        return True
    location_tokens = _tokens(location)
    return filter_tokens.issubset(location_tokens)


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
    query: str,
    location: str,
    *,
    playwright: Optional[Playwright] = None,
) -> list[JobListing]:
    cookie_jar: dict[str, str] = load_cookies(SOURCE_NAME)
    external_pw = playwright is not None
    try:
        if not external_pw:
            playwright = await async_playwright().start()
        assert playwright is not None
        browser, ctx = await _launch_context(playwright)
        try:
            await _apply_cookies_to_context(ctx, cookie_jar)
            page: Page = await ctx.new_page()
            params: dict[str, str] = {}
            if query:
                params["term"] = query
            search_url = SEARCH_URL + ("?" + urlencode(params) if params else "")
            status_code: int = 0
            try:
                resp = await page.goto(
                    search_url,
                    wait_until="domcontentloaded",
                    timeout=NAVIGATE_TIMEOUT_MS,
                )
                status_code = resp.status if resp is not None else 0
            except PlaywrightTimeoutError as exc:
                log.error(
                    "WeWorkRemotely Playwright goto %r timed out: %s - skipping",
                    search_url,
                    exc,
                )
                return []
            except PlaywrightError as exc:
                log.error(
                    "WeWorkRemotely Playwright goto %r failed: %s - skipping",
                    search_url,
                    exc,
                )
                return []
            await page.wait_for_timeout(int(jitter() * 1000))
            await page.mouse.move(200 + int(jitter() * 120), 200 + int(jitter() * 160))
            await page.keyboard.press("PageDown")
            await page.wait_for_timeout(int(jitter() * 1000))
            html = await page.content()
            cookies_from_ctx = await ctx.cookies(BASE_URL)
            new_cookie_jar = _cookies_from_context(cookies_from_ctx)
            if new_cookie_jar:
                save_cookies(SOURCE_NAME, new_cookie_jar)
            if status_code != 200:
                log.error(
                    "WeWorkRemotely search returned status=%s query=%r - skipping parse",
                    status_code,
                    query,
                )
                return []
            listings = parse_html(html, query, location)
            log.info(
                "WeWorkRemotely query=%r status=%s listings_parsed=%d",
                query,
                status_code,
                len(listings),
            )
            return listings
        finally:
            await ctx.close()
            await browser.close()
    finally:
        if not external_pw and playwright is not None:
            await playwright.stop()
