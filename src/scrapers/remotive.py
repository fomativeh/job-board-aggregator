from __future__ import annotations

import json
import logging
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Optional, cast

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

from playwright._impl._api_structures import SetCookieParam

from ..http_utils import USER_AGENTS, jitter, load_cookies, save_cookies
from ..schema import JobListing, SalaryType, utc_now_iso

log: logging.Logger = logging.getLogger(__name__)

SOURCE_NAME: Final[str] = "remotive"
LANDING_URL: Final[str] = "https://remotive.com/remote-jobs/software-dev"
SEARCH_URL_TEMPLATE: Final[str] = "https://remotive.com/remote-jobs/search?query={query}"
PUBLIC_API_URL: Final[str] = "https://remotive.com/api/remote-jobs"

NAVIGATE_TIMEOUT_MS: Final[int] = 60_000
API_LIMIT_PER_SEARCH: Final[int] = 150

VIEWPORT: Final[ViewportSize] = {"width": 1366, "height": 768}
LANGS: Final[str] = "en-US,en;q=0.7"

SESSION_DIR: Final[Path] = Path(__file__).resolve().parent.parent.parent / "session"


class RemotiveScrapeError(Exception):
    pass


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
// masked the moment Remotive's first inline <script> executes.
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
  // Headless Playwright defaults Permissions API notifications to "granted",
  // which trivially matches a known bot baseline. Override to "default".
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
  // Default to 8 (modern laptop) rather than Playwright's default, which
  // leaks the container's core count.
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
    remotive_cookies: list[SetCookieParam] = [
        _cookie(k, v, domain=".remotive.com")
        for k, v in cookies_jar.items()
        if isinstance(k, str) and isinstance(v, str)
    ]
    remotive_cookies.extend(
        _cookie(k, v, domain="remotive.com")
        for k, v in cookies_jar.items()
        if isinstance(k, str) and isinstance(v, str)
    )
    if remotive_cookies:
        await ctx.add_cookies(remotive_cookies)


def _cookies_from_context(cookies: list[Cookie]) -> dict[str, str]:
    result: dict[str, str] = {}
    for c in cookies:
        name = c.get("name")
        value = c.get("value")
        if isinstance(name, str) and isinstance(value, str):
            result[name] = value
    return result


def _matches_query(query: str, title: str, company: str, location: str, tags: list[str]) -> bool:
    if query == "":
        return True
    needle = query.lower()
    haystacks = [title.lower(), company.lower(), location.lower()]
    haystacks.extend(t.lower() for t in tags)
    return any(needle in h for h in haystacks)


def _matches_location(location_filter: str, resolved_location: str) -> bool:
    if location_filter == "":
        return True
    needle = location_filter.lower()
    return needle in resolved_location.lower()


def _pick_salary(raw_salary: Any, description: Any) -> SalaryType:
    if isinstance(raw_salary, str) and raw_salary.strip() != "":
        return raw_salary.strip()
    if isinstance(description, str) and description:
        m = re.search(
            r"(\$[\d,]+\s*[-–-]\s*\$[\d,]+\s*(?:USD|EUR|GBP)?)\b"
            r"|\$[\d,]+\s*(?:per year|p\.a\.|a year)",
            description,
        )
        if m:
            return m.group(0).strip()
    return None


def _normalize_job(raw: dict[str, Any], query: str, location_filter: str) -> JobListing | None:
    title = raw.get("title")
    company = raw.get("company_name")
    url = raw.get("url")
    if isinstance(url, str):
        url = url.strip(" \t\r\n`")
    if not (isinstance(title, str) and title.strip()):
        return None
    if not (isinstance(company, str) and company.strip()):
        return None
    if not (isinstance(url, str) and (url.startswith("http://") or url.startswith("https://"))):
        return None
    location_val = raw.get("candidate_required_location")
    if not isinstance(location_val, str) or location_val.strip() == "":
        location_val = "Remote"
    tags = raw.get("tags")
    tags_list: list[str] = (
        [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else []
    )
    if not _matches_query(query, title, company, location_val, tags_list):
        return None
    if not _matches_location(location_filter, location_val):
        return None
    salary = _pick_salary(raw.get("salary"), raw.get("description"))
    return JobListing(
        title=title.strip(),
        company=company.strip(),
        location=location_val.strip(),
        salary=salary,
        url=url,
        source=SOURCE_NAME,
        scraped_at=utc_now_iso(),
    )


def parse_api_payload(payload: dict[str, Any], query: str, location_filter: str) -> list[JobListing]:
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        log.warning("Remotive API returned non-list jobs field - skipping parse")
        return []
    results: list[JobListing] = []
    for item in jobs:
        if not isinstance(item, dict):
            continue
        normalized = _normalize_job(item, query, location_filter)
        if normalized is not None:
            results.append(normalized)
    return results


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
            first_url = (
                SEARCH_URL_TEMPLATE.format(query=query) if query else LANDING_URL
            )
            try:
                await page.goto(
                    first_url,
                    wait_until="domcontentloaded",
                    timeout=NAVIGATE_TIMEOUT_MS,
                )
            except PlaywrightTimeoutError as exc:
                log.warning(
                    "Remotive goto %s timed out: %s - retrying landing page",
                    first_url,
                    exc,
                )
                await page.goto(
                    LANDING_URL,
                    wait_until="domcontentloaded",
                    timeout=NAVIGATE_TIMEOUT_MS,
                )
            except PlaywrightError as exc:
                raise RemotiveScrapeError(
                    f"Remotive goto {first_url} failed with PlaywrightError: {exc}"
                ) from exc
            await page.wait_for_timeout(int(jitter() * 1000))
            await page.mouse.move(200 + int(jitter() * 120), 200 + int(jitter() * 160))
            await page.keyboard.press("PageDown")
            await page.wait_for_timeout(int(jitter() * 1000))

            # Call the public JSON API from inside the warmed Playwright
            # page (page.evaluate) rather than a direct httpx request. As of
            # 2026-08 Remotive returns empty/challenge-blocked responses to
            # raw Python TLS fingerprints on some residential IPs. The
            # in-browser call carries Chrome's TLS stack, the warmed session
            # cookie, matching Sec-CH-UA headers, and a real browser origin.
            params_obj: dict[str, Any] = {"limit": API_LIMIT_PER_SEARCH}
            if query:
                params_obj["query"] = query

            fetch_expr = f"""
(async () => {{
  const u = new URL({PUBLIC_API_URL!r});
  Object.entries({json.dumps(params_obj)}).forEach(([k, v]) => u.searchParams.set(k, v));
  const resp = await fetch(u.toString(), {{
    credentials: 'include',
    headers: {{
      'Accept': 'application/json,*/*',
      'X-Requested-With': 'XMLHttpRequest',
    }},
  }});
  const text = await resp.text();
  return {{ status: resp.status, text }};
}})()
"""
            try:
                result: Any = await page.evaluate(fetch_expr)
            except (PlaywrightError, PlaywrightTimeoutError) as exc:
                raise RemotiveScrapeError(
                    f"Remotive page.evaluate fetch failed: {exc}"
                ) from exc
            if not (isinstance(result, dict) and "status" in result and "text" in result):
                raise RemotiveScrapeError(
                    f"Unexpected shape returned from page fetch: {result!r}"
                )
            status = int(result["status"])
            body = str(result["text"])
            if status != 200:
                log.error(
                    "Remotive API returned status=%s body_preview=%s",
                    status,
                    body[:300],
                )
                return []
            try:
                payload: dict[str, Any] = json.loads(body)
            except json.JSONDecodeError as exc:
                log.error(
                    "Remotive API returned non-JSON body (status=%s) %s",
                    status,
                    exc,
                )
                return []
            new_cookies = await ctx.cookies()
            merged_cookie_jar = dict(cookie_jar)
            merged_cookie_jar.update(_cookies_from_context(new_cookies))
            save_cookies(SOURCE_NAME, merged_cookie_jar)

            listings = parse_api_payload(payload, query, location)
            log.info(
                "Remotive api_status=%s jobs_raw=%d jobs_matched=%d query=%r",
                status,
                len(payload.get("jobs", [])) if isinstance(payload.get("jobs"), list) else 0,
                len(listings),
                query,
            )
            return listings
        finally:
            await ctx.close()
            await browser.close()
    finally:
        if not external_pw and playwright is not None:
            await playwright.stop()
    return []
