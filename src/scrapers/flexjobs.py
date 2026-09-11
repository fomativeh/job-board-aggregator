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
WIZARD_MAX_STEPS: Final[int] = 12

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


async def _prime_user_interaction(page: Page) -> None:
    try:
        vw = VIEWPORT["width"]
        vh = VIEWPORT["height"]
        for _ in range(3):
            x = random.randint(100, vw - 100)
            y = random.randint(100, vh - 150)
            await page.mouse.move(x, y, steps=random.randint(3, 7))
            await asyncio.sleep(random.uniform(0.05, 0.15))
        await page.mouse.move(vw // 2, vh // 2)
        await page.mouse.down()
        await asyncio.sleep(random.uniform(0.05, 0.1))
        await page.mouse.up()
        await page.mouse.wheel(delta_x=0, delta_y=random.randint(80, 220))
        await asyncio.sleep(0.25)
    except Exception:
        pass


async def _maybe_escape_job_wizard(page: Page, *, results_url: str, max_steps: int = WIZARD_MAX_STEPS) -> None:
    wiz_log = log.getChild("wizard")

    def _on_wizard(url: str) -> bool:
        return "/job_wizard/" in url or "/jobwizard/" in url.lower()

    def _on_results(url: str) -> bool:
        return "/search" in url or "/jobs" in url.lower()

    if not _on_wizard(page.url):
        return

    try:
        await _prime_user_interaction(page)
    except Exception:
        pass

    renavigated = False
    for step in range(1, max_steps + 1):
        current_url = page.url
        if not _on_wizard(current_url):
            if step > 1 or renavigated:
                wiz_log.info("FlexJobs wizard escaped after %d steps at url=%s", step - 1, current_url[:120])
            return
        cards_visible = 0
        try:
            cards_visible = await page.evaluate("() => document.querySelectorAll('div[data-index]').length")
            cards_visible = int(cards_visible or 0)
        except Exception:
            cards_visible = 0
        if cards_visible > 0 or _on_results(current_url):
            wiz_log.info("FlexJobs wizard: results DOM present at url=%s; treating escape done", current_url[:120])
            return

        if step == 2 and not renavigated:
            wiz_log.info("FlexJobs wizard step 2: re-navigate to results URL with cookie state")
            renavigated = True
            try:
                await page.goto(
                    results_url,
                    wait_until="domcontentloaded",
                    timeout=NAVIGATE_TIMEOUT_MS,
                    referer="https://www.flexjobs.com/",
                )
            except PlaywrightTimeoutError:
                pass
            except Exception as e:
                wiz_log.warning("FlexJobs wizard re-nav failed: %s", e)
            try:
                await page.wait_for_load_state("domcontentloaded")
            except Exception:
                pass
            await asyncio.sleep(_jitter(2200, 1000))
            if not _on_wizard(page.url):
                wiz_log.info("FlexJobs wizard escaped via re-navigate; url=%s", page.url[:120])
                return
            continue

        next_sel = (
            "a:has-text('Next'), "
            "button:has-text('Next'), "
            "a.active-btn, "
            ".active-btn"
        )
        btn_attached = False
        try:
            next_btn = page.locator(next_sel).first
            await next_btn.wait_for(state="attached", timeout=8000)
            btn_attached = True
        except PlaywrightTimeoutError:
            btn_attached = False

        if not btn_attached:
            if step >= max_steps - 2:
                wiz_log.warning("FlexJobs wizard step %d: Next not attached; forcing location.href", step)
                try:
                    await page.evaluate(f"window.location.href = {results_url!r};")
                except Exception:
                    pass
                await asyncio.sleep(_jitter(3000, 800))
                continue
            wiz_log.warning("FlexJobs wizard step %d: Next not attached at url=%s; retrying after primer", step, current_url[:120])
            try:
                await _prime_user_interaction(page)
            except Exception:
                pass
            await asyncio.sleep(0.8)
            continue

        clicked = False
        try:
            try:
                await next_btn.click(force=True, timeout=4000)
                clicked = True
            except Exception:
                pass
        except Exception:
            pass
        if not clicked:
            try:
                n = await page.evaluate("""
                    () => {
                        const nodes = Array.from(document.querySelectorAll('a, button'));
                        const el = nodes.find(n => /\\bNext\\b/i.test(n.textContent || ''));
                        if (el) { el.click(); return true; }
                        const act = document.querySelector('a.active-btn, .active-btn');
                        if (act) { act.click(); return true; }
                        return false;
                    }
                """)
                if n:
                    clicked = True
                    wiz_log.info("FlexJobs wizard step %d: Next clicked via evaluate()", step)
            except Exception as e:
                wiz_log.warning("FlexJobs wizard step %d evaluate click failed: %s", step, e)
        if not clicked:
            wiz_log.warning("FlexJobs wizard step %d: unable to click Next", step)
            await _snapshot(page, f"wizard_step{step}_no_click")

        url_changed = False
        try:
            await page.wait_for_function(
                expression="oldUrl => location.href !== oldUrl",
                arg=current_url,
                timeout=10000,
            )
            url_changed = True
        except PlaywrightTimeoutError:
            url_changed = False

        still_wizard = _on_wizard(page.url)
        if not still_wizard:
            wiz_log.info("FlexJobs wizard escaped after click at step %d; url=%s", step, page.url[:120])
            return
        if not url_changed and still_wizard:
            wiz_log.warning("FlexJobs wizard step %d: URL did not change after click", step)
            if step >= max_steps - 1:
                wiz_log.info("FlexJobs wizard step %d: forcing direct navigation to results_url", step)
                try:
                    await page.goto(
                        results_url,
                        wait_until="domcontentloaded",
                        timeout=NAVIGATE_TIMEOUT_MS,
                        referer="https://www.flexjobs.com/",
                    )
                except Exception:
                    pass
                await asyncio.sleep(_jitter(2500, 1200))
                return
            await asyncio.sleep(1.0)
    wiz_log.warning("FlexJobs wizard escape exceeded %d steps; falling through to results anyway", max_steps)


async def _dismiss_soft_reg_modal(page: Page) -> None:
    modal_log = log.getChild("modal")
    modal_selectors = (
        "div[class*='sc-34eca615-0']",
        "#soft-reg-continue-btn",
        "div:has(> div > h2:has-text('Success! We found'))",
    )
    modal_found = False
    for sel in modal_selectors:
        try:
            escaped = sel.replace("\\", "\\\\").replace("'", "\\'")
            n = await page.evaluate(f"() => document.querySelectorAll('{escaped}').length")
            n = int(n or 0)
            if n > 0:
                modal_found = True
                break
        except Exception:
            continue
    if not modal_found:
        return
    try:
        close_sel = (
            "button[aria-label='Close'], button[aria-label='close'], "
            "div[class*='sc-34eca615'] button:has-text('×'), "
            "div[class*='sc-34eca615'] svg[role='img'], "
            "div[class*='sc-34eca615'] button svg, "
            "div[class*='sc-34eca615'] button.close"
        )
        close_btn = page.locator(close_sel).first
        close_exists = 0
        try:
            close_exists = await page.evaluate("() => document.querySelectorAll(arguments[0]).length", close_sel)
            close_exists = int(close_exists or 0)
        except Exception:
            close_exists = 0
        if close_exists > 0:
            try:
                await close_btn.click(timeout=3000)
            except Exception:
                try:
                    await close_btn.click(force=True, timeout=3000)
                except Exception:
                    pass
            await asyncio.sleep(0.6)
    except Exception:
        pass
    try:
        await page.evaluate("""
            () => {
                const roots = [];
                for (const cls of ['sc-34eca615-0', 'iuDbli', 'jfHeUk']) {
                    for (const el of document.querySelectorAll('div[class*="' + cls + '"]')) {
                        if (el && el.parentNode) roots.push(el);
                    }
                }
                const byBtn = document.getElementById('soft-reg-continue-btn');
                if (byBtn) {
                    let n = byBtn;
                    for (let i = 0; i < 8 && n; i++) { n = n.parentElement; }
                    if (n) roots.push(n);
                }
                for (const el of new Set(roots)) {
                    try { el.remove(); } catch (_) {}
                }
                document.body.style.overflow = '';
                document.documentElement.style.overflow = '';
            }
        """)
        modal_log.info("FlexJobs soft-reg modal removed via evaluate")
    except Exception as e:
        modal_log.warning("FlexJobs modal evaluate-remove failed: %s", e)


def _normalize_job(
    title: str,
    company: str,
    location: str,
    salary: str,
    url: str,
    job_id: str,
    query: str,
    location_filter: str = "",
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
    if location_filter:
        want = location_filter.strip()
        if want and want.casefold() in REMOTE_SYNONYMS:
            cand_low = location.casefold()
            if not any(s in cand_low for s in REMOTE_SYNONYMS) and cand_low not in {"n/a", "any location", "multiple", ""}:
                return None
        elif want:
            want_tok = _tokenize(want)
            cand_tok = _tokenize(location)
            if not (want_tok and (want_tok.issubset(cand_tok) or len(want_tok & cand_tok) >= max(1, len(want_tok) // 2))):
                if want.lower() not in location.lower():
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
    location_filter: str,
    scrape_log: logging.Logger,
) -> dict[str, int]:
    card_sel = "div[data-index], div.search-job-result, div.job-result-item, article.search-result"
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
    title_sel = "a[id^='job-name-'], h2.job-title, a.job-title, div.job-title a, h2 a, a[class*='job-title']"
    salary_tag_sel = "ul li, div.tag, span.salary, div.salary-info, li.tag"
    loc_sel = "span.allowed-location, span[id^='allowedlocation-'], div.location, span.location, div[data-test='job-location'], span.job-location, div[class*='location']"
    company_sel = "span.company, h3.company, div.company-name, span[class*='company'], a.company-link, div[data-test='company-name']"
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
        company = ""
        try:
            comp_el = await el.query_selector(company_sel)
            if comp_el:
                try:
                    company = (await comp_el.inner_text()).strip()
                except Exception:
                    company = ""
            if not company:
                try:
                    all_text = await el.inner_text()
                    m = re.search(r"at ([A-Z][A-Za-z0-9&,.! ]{2,50})", all_text)
                    if m:
                        company = m.group(1).strip().rstrip(" ,")
                except Exception:
                    company = ""
        except Exception:
            company = ""
        if total_seen <= SAMPLE_LOG_FIRST_CARDS:
            scrape_log.info(
                "FlexJobs card id=%s title=%r company=%r location=%r salary=%r href=%r",
                card_uuid[:80],
                title,
                company,
                location,
                salary,
                href[:100] if href else href,
            )
        row = _normalize_job(title, company, location, salary, href, card_uuid, query, location_filter)
        if row is None:
            reasons: list[str] = []
            if not title or not href:
                reasons.append(f"missing_field(title={bool(title)} href={bool(href)})")
            else:
                if not _matches_query(query, title, company, location, salary):
                    reasons.append(
                        f"query_filter(query={query!r} tokens_hit={len(_tokenize(query) & _tokenize(f'{title} {company} {location} {salary}'))}/{len(_tokenize(query))})"
                    )
                if location_filter:
                    want = location_filter.strip()
                    cand_low = (location or "").casefold()
                    if want.casefold() in REMOTE_SYNONYMS:
                        if not any(s in cand_low for s in REMOTE_SYNONYMS) and cand_low not in {"n/a", "any location", "multiple", ""}:
                            reasons.append(f"location_filter(want=Remote got={location!r})")
                    else:
                        wt = _tokenize(want)
                        ct = _tokenize(location or "")
                        if not (wt and (wt.issubset(ct) or len(wt & ct) >= max(1, len(wt) // 2))) and want.lower() not in (location or "").lower():
                            reasons.append(f"location_filter(want={want!r} got={location!r})")
            if total_seen <= SAMPLE_LOG_FIRST_CARDS and reasons:
                scrape_log.info(
                    "FlexJobs card id=%s REJECTED: %s",
                    card_uuid[:80],
                    " AND ".join(reasons),
                )
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
    await _dismiss_soft_reg_modal(page)
    await _snapshot(page, "step1_results_page")
    scrape_log.info("FlexJobs results URL: %s", page.url)
    batch = await _extract_cards(page, seen_card_keys, out_rows, query, location, scrape_log)
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
            await _dismiss_soft_reg_modal(page)
            await page.goto(next_url, wait_until="domcontentloaded", timeout=NAVIGATE_TIMEOUT_MS)
        except PlaywrightTimeoutError as e:
            nav_log.warning("Pagination page load timeout page %d: %s; break exhausted", idx, e)
            break
        pages_walked += 1
        try:
            await page.wait_for_load_state("domcontentloaded")
        except Exception:
            pass
        await _dismiss_soft_reg_modal(page)
        await asyncio.sleep(_jitter(PAGE_WAIT_AFTER_LOAD_MS, 1400))
        prev_len = len(out_rows)
        batch = await _extract_cards(page, seen_card_keys, out_rows, query, location, scrape_log)
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
                await _maybe_escape_job_wizard(page, results_url=results_url)
                await _dismiss_soft_reg_modal(page)
                try:
                    title = (await page.title()) or ""
                    body_text = (await page.locator("body").inner_text(timeout=5000)) or ""
                except Exception:
                    title = ""
                    body_text = ""
                title_low = title.lower()
                body_low = body_text.lower()
                wall_hits = _wall_signals(title_low, body_low)
                try:
                    cards_count = await page.evaluate("() => document.querySelectorAll('div[data-index]').length")
                    cards_count = int(cards_count or 0)
                except Exception:
                    cards_count = 0
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
