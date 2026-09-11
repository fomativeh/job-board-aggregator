from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from pathlib import Path
from typing import Any, Final, Optional
from urllib.parse import quote_plus

import patchright.async_api as pw_api
from patchright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    ViewportSize,
    async_playwright,
)

from ..schema import JobListing, SalaryType, make_url_hash, utc_now_iso

log: logging.Logger = logging.getLogger(__name__)

SOURCE_NAME: Final[str] = "flexjobs"
START_URL_TEMPLATE: Final[str] = "https://www.flexjobs.com/search?searchkeyword={kw}&joblocations={loc}&fromHeader=true"
NAVIGATE_TIMEOUT_MS: Final[int] = 70_000
SCRAPER_TOTAL_TIMEOUT_SECONDS: Final[int] = 600
SEARCH_SETTLE_MS: Final[int] = 3500
PAGE_WAIT_AFTER_LOAD_MS: Final[int] = 2200
NAVIGATE_MAX_ATTEMPTS: Final[int] = 3
MAX_PAGES: Final[int] = 60

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent.parent
DEBUG_DIR: Final[Path] = PROJECT_ROOT / "debug" / "flexjobs"
DEBUG_DIR.mkdir(parents=True, exist_ok=True)

VIEWPORT: Final[ViewportSize] = {"width": 1440, "height": 920}

BAD_TITLE_MARKERS: tuple[str, ...] = (
    "502",
    "503",
    "504",
    "403",
    "404",
    "bad gateway",
    "cloudflare",
    "access denied",
    "blocked",
    "challenge",
)
HARD_WALL_KEYWORDS: tuple[str, ...] = (
    "cloudflare",
    "captcha",
    "perimeterx",
    "are you a robot",
    "attention required",
    "verifying you are human",
)


class FlexJobsScrapeError(Exception):
    pass


def _jitter(base_ms: int, jitter_ms: int) -> float:
    return (base_ms + random.randint(0, jitter_ms)) / 1000.0


def _tokenize(s: str) -> set[str]:
    return {m.group(0).lower() for m in re.finditer(r"\w+", s or "")}


def _matches_query(query: str, *texts: Optional[str]) -> bool:
    query_tokens = _tokenize(query)
    if not query_tokens:
        return True
    haystack = " ".join(str(t or "") for t in texts if t is not None)
    haystack_tokens = _tokenize(haystack)
    if not haystack_tokens:
        return False
    matched_exact = query_tokens & haystack_tokens
    if len(matched_exact) == len(query_tokens):
        return True
    if len(matched_exact) >= max(1, (len(query_tokens) + 1) // 2):
        return True
    return False


async def _route_stealth(route: pw_api.Route) -> None:
    hdrs = dict(route.request.headers)
    hdrs["sec-ch-ua"] = '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"'
    hdrs["sec-ch-ua-mobile"] = "?0"
    hdrs["sec-ch-ua-platform"] = '"Windows"'
    hdrs["sec-fetch-dest"] = "document"
    hdrs["sec-fetch-mode"] = "navigate"
    hdrs["sec-fetch-site"] = "same-origin"
    hdrs["sec-fetch-user"] = "?1"
    hdrs["upgrade-insecure-requests"] = "1"
    await route.continue_(headers=hdrs)


def _snapshot_stem(tag: str) -> str:
    stamp = time.strftime("%Y%m%d_%H%M%S_") + f"{random.randint(100,999):03d}"
    return f"{stamp}_{tag}"


async def _snapshot(page: Page, tag: str, *, save_html: bool = True, save_screenshot: bool = True) -> None:
    try:
        stem = _snapshot_stem(tag)
        if save_html:
            try:
                html = await page.content()
                (DEBUG_DIR / f"{stem}.html").write_text(html, encoding="utf-8", errors="replace")
            except Exception:
                pass
        if save_screenshot:
            try:
                await page.screenshot(path=str(DEBUG_DIR / f"{stem}.png"), full_page=False)
            except Exception:
                pass
    except Exception:
        pass


def _wall_signals(title_lowcase: str, body_text_lowcase: str) -> list[str]:
    hits: list[str] = []
    for m in BAD_TITLE_MARKERS:
        if m in title_lowcase:
            hits.append(f"title:{m}")
    for m in HARD_WALL_KEYWORDS:
        if m in body_text_lowcase:
            hits.append(f"body:{m}")
    return hits


def _results_url(query: str, location: str) -> str:
    kw = query if query else "software"
    loc = location if location else "remote"
    return START_URL_TEMPLATE.format(kw=quote_plus(kw), loc=quote_plus(loc))


def _normalize_job(
    title: str,
    company: str,
    location: str,
    salary: str,
    url: str,
    job_id: str,
    query: str,
) -> Optional[JobListing]:
    title = " ".join(str(title or "").split()).strip()
    company = " ".join(str(company or "").split()).strip()
    location = " ".join(str(location or "").split()).strip()
    salary_text = " ".join(str(salary or "").split()).strip()
    salary_val: SalaryType = salary_text or None
    url = (url or "").strip()
    job_id = str(job_id or "").strip()
    if not (title and url):
        return None
    if not company:
        company = "N/A - FlexJobs"
    if not location:
        location = "N/A"
    if not _matches_query(query, title, company, location, salary_text):
        return None
    listing: JobListing = {
        "title": title,
        "company": company,
        "location": location,
        "salary": salary_val,
        "url": url,
        "source": SOURCE_NAME,
        "scraped_at": utc_now_iso(),
        "url_hash": make_url_hash(url),
    }
    _ = job_id
    return listing


async def _extract_cards(
    page: Page,
    seen_card_keys: set[str],
    out_rows: list[JobListing],
    query: str,
    scrape_log: logging.Logger,
) -> dict[str, int]:
    card_sel = "div[data-index]"
    try:
        cards = await page.query_selector_all(card_sel)
    except Exception as e:
        scrape_log.warning("FlexJobs cards query_selector_all failed: %s", e)
        return {"batch_seen": 0, "batch_matched": 0, "batch_new_rows": 0}
    total_seen = 0
    matched_kept = 0
    new_rows = 0
    total_cards = len(cards)
    progress_step = 5 if total_cards <= 40 else max(5, total_cards // 6)
    title_sel = "a[id^='job-name-']"
    salary_tag_sel = "ul li"
    loc_sel = "span.allowed-location, span[id^='allowedlocation-']"
    for el in cards:
        total_seen += 1
        try:
            title_a = await el.query_selector(title_sel)
            title = ""
            href = ""
            if title_a:
                try:
                    h2_el = await title_a.query_selector("h2")
                    title = (await h2_el.inner_text()).strip() if h2_el else ""
                    if not title:
                        title = (await title_a.inner_text()).strip()
                except Exception:
                    title = ""
                try:
                    href = (await title_a.get_attribute("href")).strip() or ""
                except Exception:
                    href = ""
        except Exception:
            title = ""
            href = ""
        card_uuid = ""
        try:
            card_id = await el.get_attribute("id")
            if card_id and len(card_id) > 8:
                card_uuid = card_id
        except Exception:
            card_uuid = ""
        if not card_uuid and href:
            m = re.search(r"/publicjobs/[^/]+-(.{8}-.{4}-.{4}-.{4}-.{12})", href)
            if m:
                card_uuid = m.group(1)
        if not card_uuid:
            card_uuid = href
        if not card_uuid:
            if total_seen % progress_step == 0 or total_seen == total_cards:
                scrape_log.info(
                    "FlexJobs parse progress: %d/%d cards seen, matched_post_filter_this_batch=%d, matched_after_filters_total=%d",
                    total_seen, total_cards, matched_kept, len(out_rows),
                )
            continue
        if card_uuid in seen_card_keys:
            if total_seen % progress_step == 0 or total_seen == total_cards:
                scrape_log.info(
                    "FlexJobs parse progress: %d/%d cards seen, matched_post_filter_this_batch=%d, matched_after_filters_total=%d",
                    total_seen, total_cards, matched_kept, len(out_rows),
                )
            continue
        seen_card_keys.add(card_uuid)
        salary = ""
        try:
            tag_lis = await el.query_selector_all(salary_tag_sel)
            tag_texts: list[str] = []
            for li in tag_lis:
                try:
                    t = (await li.inner_text()).strip()
                except Exception:
                    t = ""
                if t:
                    tag_texts.append(t)
            for t in tag_texts:
                t_low = t.lower()
                if "usd" in t_low and ("hourly" in t_low or "annually" in t_low or "monthly" in t_low):
                    salary = t
                    break
            if not salary:
                for t in tag_texts:
                    if re.search(r"\d", t) and ("$" in t or "USD" in t or "annually" in t.lower() or "hourly" in t.lower()):
                        salary = t
                        break
        except Exception:
            pass
        location = ""
        try:
            loc_el = await el.query_selector(loc_sel)
            if loc_el:
                location = (await loc_el.inner_text()).strip()
        except Exception:
            location = ""
        if href and href.startswith("/"):
            href = "https://www.flexjobs.com" + href
        row = _normalize_job(title, "", location, salary, href, card_uuid, query)
        if row is None:
            if total_seen % progress_step == 0 or total_seen == total_cards:
                scrape_log.info(
                    "FlexJobs parse progress: %d/%d cards seen, matched_post_filter_this_batch=%d, matched_after_filters_total=%d",
                    total_seen, total_cards, matched_kept, len(out_rows),
                )
            continue
        matched_kept += 1
        out_rows.append(row)
        new_rows += 1
        if total_seen % progress_step == 0 or total_seen == total_cards:
            scrape_log.info(
                "FlexJobs parse progress: %d/%d cards seen, matched_post_filter_this_batch=%d, matched_after_filters_total=%d",
                total_seen, total_cards, matched_kept, len(out_rows),
            )
    return {"batch_seen": total_seen, "batch_matched": matched_kept, "batch_new_rows": new_rows}


async def _collect_results(
    page: Page,
    query: str,
    location: str,
    max_listings: Optional[int],
) -> list[JobListing]:
    scrape_log = log.getChild("scrape")
    nav_log = log.getChild("nav")
    out_rows: list[JobListing] = []
    seen_card_keys: set[str] = set()
    target_cap = max_listings
    try:
        results_anchor = page.locator("div[data-index], #search-pagination")
        await results_anchor.first.wait_for(state="attached", timeout=40_000)
    except PlaywrightTimeoutError:
        nav_log.warning("Results anchor not visible after load; proceeding anyway")
    await asyncio.sleep(_jitter(SEARCH_SETTLE_MS, 1200))
    await _snapshot(page, "step1_results_page")
    scrape_log.info("FlexJobs results URL: %s", page.url)
    batch = await _extract_cards(page, seen_card_keys, out_rows, query, scrape_log)
    nav_log.info(
        "FlexJobs initial page: seen=%d matched_post_filter_this_batch=%d matched_after_filters_total=%d kept_rows=%d cap=%s url=%s",
        batch["batch_seen"], batch["batch_matched"], len(out_rows), len(out_rows), max_listings, page.url,
    )
    if target_cap and len(out_rows) >= target_cap:
        nav_log.info(
            "FlexJobs post-filter row count %d >= target_cap %d => stop condition 3 (max-count satisfied).",
            len(out_rows), target_cap,
        )
        return out_rows
    consecutive_no_growth = 0
    pages_walked = 1
    total_start = time.monotonic()
    for idx in range(2, MAX_PAGES + 2):
        if time.monotonic() - total_start > SCRAPER_TOTAL_TIMEOUT_SECONDS:
            nav_log.info("FlexJobs stop condition 2: total timeout %ds reached", SCRAPER_TOTAL_TIMEOUT_SECONDS)
            break
        if target_cap and len(out_rows) >= target_cap:
            nav_log.info(
                "FlexJobs post-filter row count %d >= target_cap %d => stop condition 3 (max-count satisfied).",
                len(out_rows), target_cap,
            )
            break
        next_href: Optional[str] = None
        try:
            next_li_sel = "#search-pagination ul.pagination li.next a"
            next_a = page.locator(next_li_sel).first
            try:
                parent_class = await page.locator("#search-pagination ul.pagination li.next").first.get_attribute("class")
                if isinstance(parent_class, str) and ("disabled" in parent_class.lower()):
                    next_href = None
                else:
                    href_attr = await next_a.get_attribute("href")
                    if isinstance(href_attr, str) and href_attr and href_attr != "#":
                        next_href = href_attr
            except Exception:
                try:
                    href_attr = await next_a.get_attribute("href")
                    if isinstance(href_attr, str) and href_attr and href_attr != "#":
                        next_href = href_attr
                except Exception:
                    next_href = None
        except Exception:
            next_href = None
        if not next_href:
            nav_log.info(
                "FlexJobs stop condition 1: next link absent/disabled => pages exhausted"
            )
            break
        if next_href.startswith("/"):
            next_url = "https://www.flexjobs.com" + next_href
        elif next_href.startswith("http"):
            next_url = next_href
        else:
            next_url = "https://www.flexjobs.com/" + next_href.lstrip("/")
        try:
            await page.goto(next_url, wait_until="domcontentloaded", timeout=NAVIGATE_TIMEOUT_MS)
        except PlaywrightTimeoutError as e:
            nav_log.warning("Pagination page load timeout page %d: %s; break exhausted", idx, e)
            break
        pages_walked += 1
        try:
            await page.wait_for_load_state("domcontentloaded")
        except Exception:
            pass
        await asyncio.sleep(_jitter(PAGE_WAIT_AFTER_LOAD_MS, 1400))
        prev_len = len(out_rows)
        batch = await _extract_cards(page, seen_card_keys, out_rows, query, scrape_log)
        new_cards = len(out_rows) - prev_len
        if new_cards == 0:
            consecutive_no_growth += 1
        else:
            consecutive_no_growth = 0
        if batch["batch_seen"] == 0 and consecutive_no_growth >= 3:
            nav_log.info("FlexJobs stop condition 1: 3 consecutive pages with 0 cards parsed => pages exhausted")
            break
        nav_log.info(
            "FlexJobs page=%d (walked %d) batch_seen=%d batch_matched_post_filter=%d new_cards=%d matched_after_filters_total=%d rows_now=%d cap=%s consec_no_growth=%d",
            idx, pages_walked, batch["batch_seen"], batch["batch_matched"], new_cards, len(out_rows), len(out_rows), max_listings, consecutive_no_growth,
        )
        if target_cap and len(out_rows) >= target_cap:
            nav_log.info(
                "FlexJobs post-filter row count %d >= target_cap %d => stop condition 3 (max-count satisfied).",
                len(out_rows), target_cap,
            )
            break
    return out_rows


async def _launch_standard(pw: Playwright) -> tuple[Browser, BrowserContext]:
    browser = await pw.chromium.launch(
        channel="chrome",
        headless=False,
        args=[
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
        ],
    )
    ctx = await browser.new_context(
        viewport=VIEWPORT,
        locale="en-US",
        accept_downloads=True,
    )
    return browser, ctx


async def scrape(
    query: str,
    location: str,
    *,
    playwright: Optional[Playwright] = None,
    max_listings: Optional[int] = None,
) -> list[JobListing]:
    external_pw = playwright is not None
    nav_log = log.getChild("nav")
    collected: list[JobListing] = []
    last_err: Optional[Exception] = None
    results_url = _results_url(query, location)
    try:
        if not external_pw:
            playwright = await async_playwright().start()
        assert playwright is not None
        for attempt in range(1, NAVIGATE_MAX_ATTEMPTS + 1):
            nav_log.info("FlexJobs open attempt %d/%d results URL=%s", attempt, NAVIGATE_MAX_ATTEMPTS, results_url)
            browser: Optional[Browser] = None
            ctx: Optional[BrowserContext] = None
            page: Optional[Page] = None
            try:
                browser, ctx = await _launch_standard(playwright)
                try:
                    await ctx.route("**/*", _route_stealth)
                except Exception:
                    pass
                try:
                    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                except Exception:
                    page = await ctx.new_page()
                try:
                    resp = await page.goto(results_url, wait_until="domcontentloaded", timeout=NAVIGATE_TIMEOUT_MS)
                except PlaywrightTimeoutError as e:
                    last_err = e
                    nav_log.warning("FlexJobs goto attempt %d timeout: %s", attempt, e)
                    try:
                        await ctx.close()
                        await browser.close()
                    except Exception:
                        pass
                    ctx = None
                    browser = None
                    await asyncio.sleep(2.5 * attempt)
                    continue
                except PlaywrightError as e:
                    last_err = e
                    nav_log.warning("FlexJobs goto attempt %d error: %s", attempt, e)
                    try:
                        await ctx.close()
                        await browser.close()
                    except Exception:
                        pass
                    ctx = None
                    browser = None
                    await asyncio.sleep(2.5 * attempt)
                    continue
                try:
                    await page.wait_for_load_state("domcontentloaded")
                except Exception:
                    pass
                await asyncio.sleep(_jitter(2500, 1500))
                try:
                    title = (await page.title()) or ""
                    body_text = (await page.locator("body").inner_text(timeout=5000)) or ""
                except Exception:
                    title = ""
                    body_text = ""
                title_low = title.lower()
                body_low = body_text.lower()
                wall_hits = _wall_signals(title_low, body_low)
                cards_count = await page.locator("div[data-index]").count()
                nav_log.info(
                    "FlexJobs attempt %d title=%r cards=%d wall=%s resp_status=%s",
                    attempt, title[:80], cards_count, wall_hits,
                    resp.status if resp else "n/a",
                )
                if wall_hits:
                    nav_log.warning("FlexJobs attempt %d hit wall signals %s; retrying", attempt, wall_hits)
                    last_err = FlexJobsScrapeError(f"Wall: {wall_hits}")
                    await _snapshot(page, f"attempt{attempt}_wall")
                    try:
                        await ctx.close()
                        await browser.close()
                    except Exception:
                        pass
                    ctx = None
                    browser = None
                    await asyncio.sleep(3.0 * attempt)
                    continue
                await _snapshot(page, f"attempt{attempt}_initial_load")
                rows = await _collect_results(page, query, location, max_listings)
                collected.extend(rows)
                await _snapshot(page, "final")
                break
            except Exception as e:
                last_err = e
                nav_log.warning("FlexJobs launch err (attempt %d): %s", attempt, last_err)
                await asyncio.sleep(1.5 * attempt)
            finally:
                try:
                    if page:
                        await page.close()
                except Exception:
                    pass
                try:
                    if ctx is not None:
                        await ctx.close()
                except Exception:
                    pass
                try:
                    if browser is not None:
                        await browser.close()
                except Exception:
                    pass
    finally:
        if not external_pw and playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                pass
    if not collected and last_err:
        log.warning("FlexJobs: no rows collected, last_err=%s", last_err)
    return collected
