from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Optional

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

from ..http_utils import jitter
from ..schema import JobListing, SalaryType, make_url_hash, utc_now_iso

log: logging.Logger = logging.getLogger(__name__)

SOURCE_NAME: Final[str] = "glassdoor"
START_URL: Final[str] = "https://www.glassdoor.com/Job/index.htm"
NAVIGATE_TIMEOUT_MS: Final[int] = 70_000
SCRAPER_TOTAL_TIMEOUT_SECONDS: Final[int] = 900
SEARCH_SETTLE_MS: Final[int] = 3500
PAGE_IDLE_MS: Final[int] = 900
MAX_LOAD_MORE_CLICKS: Final[int] = 80
LOAD_MORE_WAIT_MS_AFTER_CLICK: Final[int] = 2200
SAMPLE_LOG_FIRST_CARDS: Final[int] = 12
NAVIGATE_MAX_ATTEMPTS: Final[int] = 3

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent.parent
SESSION_DIR: Final[Path] = PROJECT_ROOT / "session"
PROFILE_DIR: Final[Path] = SESSION_DIR / "patchright_chrome_profile_glassdoor"
DEBUG_DIR: Final[Path] = PROJECT_ROOT / "debug" / "glassdoor"
PROFILE_DIR.mkdir(parents=True, exist_ok=True)
DEBUG_DIR.mkdir(parents=True, exist_ok=True)

VIEWPORT: Final[ViewportSize] = {"width": 1440, "height": 920}
LANGS: Final[str] = "en-US,en;q=0.7"

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


class GlassdoorScrapeError(Exception):
    pass


def _jitter(lo: float = 0.6, hi: float = 1.4) -> float:
    return lo + (hi - lo) * random.random()


def _tokens(s: str) -> set[str]:
    return {t.casefold() for t in TOKEN_RE.findall(s)}


def _matches_query(query: str, title: str, company: str, location: str) -> bool:
    if query == "":
        return True
    qt = _tokens(query)
    if not qt:
        return True
    hay = f"{title} {company} {location}"
    ht = _tokens(hay)
    if qt.issubset(ht):
        return True
    overlap = len(qt & ht)
    need = max(1, len(qt) // 2)
    return overlap >= need


def _matches_location(location_filter: str, candidate_location: str) -> bool:
    if not location_filter:
        return True
    want = location_filter.strip()
    if not want:
        return True
    cand = (candidate_location or "").strip()
    if want.casefold() in REMOTE_SYNONYMS:
        cand_low = cand.casefold()
        for syn in REMOTE_SYNONYMS:
            if syn in cand_low:
                return True
        if cand == "" or cand.lower() in {"", "any location", "multiple"}:
            return True
        return False
    want_tok = _tokens(want)
    if not want_tok:
        return True
    cand_tok = _tokens(cand)
    if want_tok.issubset(cand_tok):
        return True
    wl = want.lower()
    cl = cand.lower()
    if wl in cl:
        return True
    return len(want_tok & cand_tok) >= max(1, len(want_tok) // 2)


async def _route_stealth(route, request) -> None:
    headers = dict(request.headers or {})
    extra = {
        "sec-ch-ua": '"Chromium";v="128", "Google Chrome";v="128", "Not.A/Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
        "dnt": "1",
    }
    for k, v in extra.items():
        if k.lower() not in headers:
            headers[k.lower()] = v
    await route.continue_(headers=headers)


async def _snapshot(page: Page, stem: str) -> None:
    try:
        png = DEBUG_DIR / f"{stem}.png"
        await page.screenshot(path=str(png), full_page=False)
    except Exception:
        pass
    try:
        html_file = DEBUG_DIR / f"{stem}.html"
        html_file.write_text(await page.content(), encoding="utf-8", errors="ignore")
    except Exception:
        pass


def _wall_signals(body_text: str) -> dict[str, list[str]]:
    low = body_text.lower()
    walls = {
        "cloudflare": ["cloudflare", "just a moment", "ray id", "challenge"],
        "captcha": ["captcha", "verify you are human", "not a robot", "security check"],
        "perimeterx": ["perimeterx", "px-captcha", "px_bm"],
    }
    hits: dict[str, list[str]] = {}
    for name, words in walls.items():
        found = [w for w in words if w in low]
        if found:
            hits[name] = found
    return hits


def _normalize_job(
    title: str,
    company: str,
    location: str,
    salary_raw: str,
    url: str,
    job_id: Optional[str],
    query: str,
    location_filter: str,
) -> Optional[JobListing]:
    title = " ".join(str(title or "").split()).strip()
    company = " ".join(str(company or "").split()).strip()
    location = " ".join(str(location or "").split()).strip()
    salary_text = " ".join(str(salary_raw or "").split()).strip()
    salary: SalaryType = salary_text or None
    url = (url or "").strip()
    if not (title and company and url):
        return None
    if not _matches_query(query, title, company, location):
        return None
    listing: JobListing = {
        "title": title,
        "company": company,
        "location": location,
        "salary": salary,
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
) -> tuple[int, int]:
    card_sel = (
        "ul[aria-label='Jobs List'] li[data-test='jobListing'],"
        "div[class*='JobsList_wrapper'] li[data-test='jobListing'],"
        "div.JobsList_wrapper__EyUF6 li[data-test='jobListing'],"
        "li[data-test='jobListing']"
    )
    try:
        cards = await page.query_selector_all(card_sel)
    except Exception as e:
        log.warning("Glassdoor cards query_selector_all failed: %s", e)
        return 0, 0
    total_seen = 0
    new_added = 0
    total_cards = len(cards)
    progress_step = 5 if total_cards <= 40 else max(5, total_cards // 6)
    title_sel = "a[data-test='job-title'], a[class*='JobCard_jobTitle'], a.JobCard_jobTitle__GLyJ1"
    link_sel = "a[data-test='job-link'], a[class*='JobCard_trackingLink'], a.JobCard_trackingLink__HMyun"
    company_sel = "span[class*='EmployerProfile_compactEmployerName'], span.EmployerProfile_compactEmployerName__9MGcV, span.EmployerProfile_compactEmployerName__LE242"
    location_sel = "div[data-test='emp-location'], div[class*='JobCard_location'], div.JobCard_location__Ds1fM"
    salary_sel = "div[data-test='detailSalary'], div[class*='JobCard_salaryEstimate'], div.JobCard_salaryEstimate__QpbTW"
    sample_logged = 0
    for el in cards:
        total_seen += 1
        try:
            job_id_raw = await el.get_attribute("data-jobid")
        except Exception:
            job_id_raw = None
        try:
            title_el = await el.query_selector(title_sel)
            title = (await title_el.inner_text()).strip() if title_el else ""
            href = (await title_el.get_attribute("href")).strip() if title_el else ""
        except Exception:
            title = ""
            href = ""
        if not href:
            try:
                link_el = await el.query_selector(link_sel)
                href = (await link_el.get_attribute("href")).strip() if link_el else ""
            except Exception:
                href = ""
        if not job_id_raw and href:
            m = re.search(r"jl=(\d+)", href)
            if m:
                job_id_raw = m.group(1)
        job_id = str(job_id_raw).strip() if job_id_raw else None
        card_key = job_id or href
        if not card_key:
            if total_seen % progress_step == 0 or total_seen == total_cards:
                log.info(
                    "Glassdoor parse progress: %d/%d cards seen, matched_post_filter_this_batch=%d, matched_after_filters_total=%d",
                    total_seen, total_cards, new_added, len(out_rows),
                )
            continue
        if card_key in seen_card_keys:
            if total_seen % progress_step == 0 or total_seen == total_cards:
                log.info(
                    "Glassdoor parse progress: %d/%d cards seen, matched_post_filter_this_batch=%d, matched_after_filters_total=%d",
                    total_seen, total_cards, new_added, len(out_rows),
                )
            continue
        seen_card_keys.add(card_key)
        try:
            company_el = await el.query_selector(company_sel)
            company = (await company_el.inner_text()).strip() if company_el else ""
        except Exception:
            company = ""
        try:
            loc_el = await el.query_selector(location_sel)
            location = (await loc_el.inner_text()).strip() if loc_el else ""
        except Exception:
            location = ""
        try:
            sal_el = await el.query_selector(salary_sel)
            salary = (await sal_el.inner_text()).strip() if sal_el else ""
        except Exception:
            salary = ""
        if sample_logged < SAMPLE_LOG_FIRST_CARDS:
            log.debug(
                "Glassdoor card id=%s title=%r company=%r location=%r salary=%r",
                job_id or href[:80],
                title,
                company,
                location,
                salary,
            )
            sample_logged += 1
        row = _normalize_job(title, company, location, salary, href, job_id, query, location_filter)
        if row is None:
            if total_seen % progress_step == 0 or total_seen == total_cards:
                log.info(
                    "Glassdoor parse progress: %d/%d cards seen, matched_post_filter_this_batch=%d, matched_after_filters_total=%d",
                    total_seen, total_cards, new_added, len(out_rows),
                )
            continue
        out_rows.append(row)
        new_added += 1
        if total_seen % progress_step == 0 or total_seen == total_cards:
            log.info(
                "Glassdoor parse progress: %d/%d cards seen, matched_post_filter_this_batch=%d, matched_after_filters_total=%d",
                total_seen, total_cards, new_added, len(out_rows),
            )
    return total_seen, new_added


async def _search_and_collect(
    page: Page,
    query: str,
    location_filter: str,
    stats: dict[str, Any],
    run_stamp: str,
    start_ts: float,
    target_cap: Optional[int] = None,
) -> list[JobListing]:
    out_rows: list[JobListing] = []
    seen_card_keys: set[str] = set()
    stats["start_url"] = str(page.url)
    await page.wait_for_timeout(int(_jitter(1.0, 2.0) * 1000))
    try:
        role = page.locator("#searchBar-jobTitle")
        await role.wait_for(state="visible", timeout=15_000)
        await role.click(timeout=4000, force=True)
        await page.wait_for_timeout(int(_jitter(300, 600)))
        await role.fill(query, timeout=4000)
        log.info("Glassdoor filled role field: %r", query)
    except Exception as e:
        log.warning("Glassdoor role field fill failed: %s", e)
    await page.wait_for_timeout(int(_jitter(400, 800)))
    loc = page.locator("#searchBar-location")
    try:
        await loc.wait_for(state="visible", timeout=12_000)
        await loc.click(timeout=4000, force=True)
        await page.wait_for_timeout(int(_jitter(300, 600)))
        try:
            await loc.click(click_count=3, timeout=2000)
        except Exception:
            pass
        await loc.fill(location_filter or "", timeout=4000)
        log.info("Glassdoor filled location field: %r", location_filter or "")
    except Exception as e:
        log.warning("Glassdoor location field fill failed: %s", e)
    await page.wait_for_timeout(int(_jitter(400, 800)))
    url_before_submit = str(page.url)
    stats["url_before_submit"] = url_before_submit
    log.info("Glassdoor URL before submit: %s", url_before_submit)

    async def _try_submit() -> bool:
        try:
            focused: bool = False
            try:
                await loc.focus(timeout=2000)
                focused = True
            except Exception:
                focused = False
            try:
                await loc.press("Enter", timeout=5000)
                log.info("Glassdoor submitted via loc.press(Enter) focused=%s", focused)
                return True
            except Exception as pe:
                log.warning("Glassdoor loc.press(Enter) failed: %s: fallback global keyboard", pe)
                try:
                    await page.keyboard.press("Enter")
                    log.info("Glassdoor submitted via global keyboard.Enter")
                    return True
                except Exception as ke:
                    log.warning("Glassdoor global keyboard.Enter failed: %s: try search button", ke)
                    sb = await page.query_selector(
                        "button[type='submit'], button[aria-label*='search' i], form button, "
                        "div[class*='SearchBar'] button, button[data-test='search-submit']"
                    )
                    if sb is not None:
                        try:
                            await sb.click(force=True, timeout=3000)
                            log.info("Glassdoor submitted via search button click(force=True)")
                            return True
                        except Exception as cbe:
                            try:
                                await page.evaluate(
                                    "(b) => { if (b && b.dispatchEvent) b.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,view:window})); }",
                                    sb,
                                )
                                log.info("Glassdoor submitted via search button dispatchEvent")
                                return True
                            except Exception:
                                pass
                    log.error("Glassdoor ALL submit strategies failed (loc Enter, global Enter, search button)")
                    return False
        except Exception as outer:
            log.error("Glassdoor _try_submit unexpected: %s", outer)
            return False

    async def _wait_url_change(prev_url: str, timeout_ms: int = 45_000) -> bool:
        deadline = time.monotonic() + timeout_ms / 1000.0
        slept = 0
        while time.monotonic() < deadline:
            cur = str(page.url)
            if cur != prev_url and ("job" in cur.lower() or "/Job/" in cur or "jobs" in cur.lower()):
                log.info("Glassdoor URL changed after submit (took %dms): %s", slept, cur)
                return True
            try:
                h = await page.evaluate("() => window.location.href")
                if isinstance(h, str) and h != prev_url and ("job" in h.lower() or "/Job/" in h):
                    log.info("Glassdoor location.href changed (took %dms): %s", slept, h)
                    return True
            except Exception:
                pass
            await page.wait_for_timeout(200)
            slept += 200
        return False

    submitted_ok = await _try_submit()
    if not submitted_ok:
        raise GlassdoorScrapeError("Failed to submit Glassdoor search form (all strategies failed)")
    url_changed = await _wait_url_change(url_before_submit, timeout_ms=45_000)
    if not url_changed:
        retry_submit = await _try_submit()
        log.warning("Glassdoor URL didn't change 45s after first submit; retry_submit=%s", retry_submit)
        url_changed = await _wait_url_change(url_before_submit, timeout_ms=30_000)
        if not url_changed:
            cur = str(page.url)
            log.error(
                "Glassdoor URL STILL same after submit (before=%s now=%s). Results will not load. "
                "Extra settle wait before proceeding.",
                url_before_submit, cur,
            )
            await page.wait_for_timeout(3000)
    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=8_000)
    except Exception:
        pass
    results_loc = page.locator(
        "ul[aria-label='Jobs List'], div[class*='JobsList_wrapper'], div.JobsList_wrapper__EyUF6, li[data-test='jobListing']"
    ).first
    try:
        await results_loc.wait_for(state="attached", timeout=35_000)
        log.info("Glassdoor results wrapper / first job listing attached on results page.")
    except Exception as rwe:
        log.warning(
            "Glassdoor results wrapper not attached after submit (timeout 35s): %s. "
            "Proceeding anyway: maybe empty results or wrapper changed.",
            rwe,
        )
    await page.wait_for_timeout(SEARCH_SETTLE_MS + int(_jitter(800, 1600)))
    await _snapshot(page, f"{run_stamp}_step1_after_submit")
    try:
        body_text = await page.inner_text("body", timeout=4000)
    except Exception:
        body_text = ""
    stats["wall_signals_after_submit"] = _wall_signals(body_text)
    try:
        wrapper_sel = "ul[aria-label='Jobs List'], div[class*='JobsList_wrapper'], div.JobsList_wrapper__EyUF6"
        list_wrapper = await page.query_selector(wrapper_sel)
        if list_wrapper is None:
            log.warning("Glassdoor no JobsList wrapper (aria-label/hashed) found after submit.")
    except Exception as e:
        log.warning("Glassdoor wrapper locate err: %s", e)
    batch_seen, batch_added = await _extract_cards(page, seen_card_keys, out_rows, query, location_filter)
    stats["initial_cards"] = int(batch_seen)
    url_after_submit = str(page.url)
    stats["url_after_submit"] = url_after_submit
    still_on_index = (
        url_before_submit == url_after_submit
        or "/Job/index.htm" in url_after_submit
        or ("/index.htm" in url_after_submit and "kw=" not in url_after_submit and "job/" not in url_after_submit.lower())
    )
    if still_on_index and batch_seen == 0:
        extra_wait = int(_jitter(5000, 9000))
        log.warning(
            "Glassdoor after submit: still on index page (no URL change) and 0 initial cards. "
            "Extra %dms settle + re-extract + one last Enter retry.",
            extra_wait,
        )
        await page.wait_for_timeout(extra_wait)
        try:
            await results_loc.wait_for(state="attached", timeout=25_000)
        except Exception:
            pass
        await page.wait_for_timeout(int(_jitter(2000, 4000)))
        batch2_seen, batch2_added = await _extract_cards(page, seen_card_keys, out_rows, query, location_filter)
        stats["initial_cards"] = int(stats.get("initial_cards", 0) or 0) + int(batch2_seen)
        if batch2_seen == 0:
            log.error("Glassdoor: second extract also 0 cards and still on index URL. Triggering one last Enter retry.")
            try:
                await loc.focus(timeout=2000)
            except Exception:
                pass
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(8000)
            url_third = str(page.url)
            log.info("Glassdoor post-retry-Enter URL: %s", url_third)
            try:
                await results_loc.wait_for(state="attached", timeout=30_000)
            except Exception as final_err:
                log.error(
                    "Glassdoor results wrapper still missing after final retry Enter: %s. url_before=%s url_after=%s",
                    final_err, url_before_submit, url_third,
                )
            batch3_seen, _ = await _extract_cards(page, seen_card_keys, out_rows, query, location_filter)
            stats["initial_cards"] = int(stats.get("initial_cards", 0) or 0) + int(batch3_seen)
    log.info(
        "Glassdoor initial batch: seen=%d matched_post_filter_this_batch=%d matched_after_filters_total=%d rows_now=%d url=%s",
        batch_seen,
        batch_added,
        len(out_rows),
        len(out_rows),
        url_after_submit,
    )
    load_more_clicks = 0
    stop_reason = ""
    consecutive_no_growth = 0
    for idx in range(1, MAX_LOAD_MORE_CLICKS + 1):
        if time.monotonic() - start_ts >= SCRAPER_TOTAL_TIMEOUT_SECONDS:
            stop_reason = "scraper_timeout"
            break
        if target_cap is not None and len(out_rows) >= target_cap:
            stop_reason = "max_jobs_met"
            log.info(
                "Glassdoor post-filter row count %d >= target_cap %d => stop condition 3 (max-count satisfied).",
                len(out_rows), target_cap,
            )
            break
        try:
            btn = await page.query_selector("button[data-test='load-more']")
            if btn is None:
                log.info("Glassdoor 'Show more jobs' button[data-test='load-more'] not found => all pages exhausted.")
                stop_reason = "all_pages_exhausted"
                break
            visible = await btn.is_visible()
            disabled = False
            try:
                disabled = await btn.is_disabled()
            except Exception:
                disabled = False
            loading = False
            try:
                dl = await btn.get_attribute("data-loading")
                loading = str(dl).lower() == "true"
            except Exception:
                loading = False
            if (not visible) or disabled or loading:
                log.info(
                    "Glassdoor button present but not clickable (visible=%s disabled=%s loading=%s) => all pages exhausted.",
                    visible,
                    disabled,
                    loading,
                )
                stop_reason = "all_pages_exhausted"
                break
        except Exception as e:
            log.warning("Glassdoor load-more locate err: %s => stop exhausted", e)
            stop_reason = "all_pages_exhausted"
            break
        try:
            await btn.scroll_into_view_if_needed()
            await page.wait_for_timeout(int(_jitter(200, 500)))
            try:
                close_btns = await page.query_selector_all(
                    "dialog[open] button[aria-label*='close' i], dialog[open] button svg, "
                    "div[role='dialog'] button[aria-label*='close' i], button[aria-label='Close modal'], "
                    "div[class*='Modal'] button[aria-label*='close' i]"
                )
                for cb in close_btns[:3]:
                    try:
                        cv = await cb.is_visible()
                        if cv:
                            await cb.click(force=True)
                            await page.wait_for_timeout(350)
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                await page.evaluate(
                    "() => { document.querySelectorAll('dialog[open]').forEach(d => d.close && d.close()); }"
                )
                await page.wait_for_timeout(250)
            except Exception:
                pass
            try:
                await btn.click(timeout=4000, force=True)
                load_more_clicks += 1
            except Exception as ce:
                try:
                    await page.evaluate(
                        "(b) => { b.dispatchEvent(new MouseEvent('click', {bubbles:true,cancelable:true,view:window})); }",
                        btn,
                    )
                    load_more_clicks += 1
                except Exception:
                    raise ce
        except Exception as e:
            log.warning("Glassdoor load-more click failed (iter %d): %s", idx, e)
            consecutive_no_growth += 1
            if consecutive_no_growth >= 3:
                stop_reason = "all_pages_exhausted"
                break
            await page.wait_for_timeout(int(_jitter(500, 1200)))
            continue
        try:
            await page.wait_for_timeout(LOAD_MORE_WAIT_MS_AFTER_CLICK + int(_jitter(200, 600)))
            prev_h = 0
            stable = 0
            for _ in range(5):
                try:
                    h = await page.evaluate(
                        "() => { window.scrollBy(0, Math.max(400, Math.floor(document.body.scrollHeight*0.18))); return document.body.scrollHeight; }"
                    )
                except Exception:
                    h = 0
                await page.wait_for_timeout(PAGE_IDLE_MS)
                if isinstance(h, int) and h <= prev_h:
                    stable += 1
                    if stable >= 2:
                        break
                prev_h = int(h or 0)
        except Exception:
            pass
        before = len(seen_card_keys)
        batch_seen, new_added = await _extract_cards(page, seen_card_keys, out_rows, query, location_filter)
        new_cards = len(seen_card_keys) - before
        log.info(
            "Glassdoor load-more iter=%d clicks_done=%d batch_seen=%d batch_matched_post_filter=%d new_cards=%d matched_after_filters_total=%d matched_rows=%d",
            idx,
            load_more_clicks,
            batch_seen,
            new_added,
            new_cards,
            len(out_rows),
            new_added,
        )
        if new_cards == 0:
            consecutive_no_growth += 1
            if consecutive_no_growth >= 3:
                log.info("Glassdoor 3 consecutive clicks with 0 new cards => all pages exhausted")
                stop_reason = "all_pages_exhausted"
                break
        else:
            consecutive_no_growth = 0
    if not stop_reason:
        stop_reason = "max_load_more_clicks"
    await _snapshot(page, f"{run_stamp}_step2_final")
    stats["load_more_clicks"] = int(load_more_clicks)
    stats["stop_reason"] = stop_reason
    stats["elapsed_s"] = round(time.monotonic() - start_ts, 2)
    stats["total_cards_seen"] = int(len(seen_card_keys))
    stats["total_cards_matched_precap"] = int(len(out_rows))
    stats["status"] = 200
    log.info(
        "Glassdoor final: seen=%d matched=%d clicks=%d stop=%s elapsed_s=%.2f",
        stats.get("total_cards_seen", 0),
        stats.get("total_cards_matched_precap", 0),
        stats.get("load_more_clicks", 0),
        stats.get("stop_reason", ""),
        float(stats.get("elapsed_s", 0.0)),
    )
    return out_rows


@dataclass(frozen=True)
class _BrowserHandles:
    ctx: BrowserContext
    browser: Browser | None = None


async def _launch_context(pw: Playwright) -> _BrowserHandles:
    ctx = await pw.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        channel="chrome",
        headless=False,
        viewport=VIEWPORT,
        locale="en-US",
        extra_http_headers={"Accept-Language": LANGS},
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
        ],
    )
    await ctx.route("**/*", _route_stealth)
    return _BrowserHandles(ctx=ctx, browser=None)


async def scrape(
    query: str,
    location: str,
    *,
    playwright: Optional[Playwright] = None,
    max_listings: Optional[int] = None,
) -> list[JobListing]:
    external_pw = playwright is not None
    target_cap: Optional[int] = int(max_listings) if isinstance(max_listings, int) and max_listings > 0 else None
    last_err: Optional[str] = None
    collected: list[JobListing] = []
    stats: dict[str, Any] = {}
    run_stamp = time.strftime("%Y%m%d_%H%M%S") + f"_{int((time.time_ns() % 1_000_000) // 1000):03d}"
    start_ts = time.monotonic()
    try:
        if not external_pw:
            playwright = await async_playwright().start()
        assert playwright is not None
        for attempt in range(1, NAVIGATE_MAX_ATTEMPTS + 1):
            log.info("Glassdoor launch attempt %d/%d (headless=False, real Chrome channel=chrome, persistent profile)", attempt, NAVIGATE_MAX_ATTEMPTS)
            ctx: BrowserContext | None = None
            handles: _BrowserHandles | None = None
            page: Page | None = None
            try:
                handles = await _launch_context(playwright)
                ctx = handles.ctx
                pages = ctx.pages
                page = pages[0] if pages else await ctx.new_page()
                try:
                    await page.goto(START_URL, wait_until="domcontentloaded", timeout=NAVIGATE_TIMEOUT_MS)
                except PlaywrightTimeoutError as e:
                    if attempt < NAVIGATE_MAX_ATTEMPTS:
                        log.warning("Glassdoor initial goto timeout (attempt %d): %s", attempt, e)
                        try:
                            await ctx.close()
                        except Exception:
                            pass
                        ctx = None
                        handles = None
                        await _sleep(1.5, 2.5)
                        continue
                    else:
                        last_err = f"nav timeout after {NAVIGATE_MAX_ATTEMPTS} attempts: {e}"
                        break
                except PlaywrightError as e:
                    if attempt < NAVIGATE_MAX_ATTEMPTS:
                        log.warning("Glassdoor nav error (attempt %d): %s", attempt, e)
                        try:
                            await ctx.close()
                        except Exception:
                            pass
                        ctx = None
                        handles = None
                        await _sleep(1.5, 2.5)
                        continue
                    else:
                        last_err = f"nav error after {NAVIGATE_MAX_ATTEMPTS} attempts: {e}"
                        break
                assert page is not None
                await _snapshot(page, f"{run_stamp}_attempt{attempt}_initial_load")
                try:
                    body_text = await page.inner_text("body", timeout=3500)
                except Exception:
                    body_text = ""
                walls = _wall_signals(body_text)
                stats["wall_signals_initial"] = walls
                try:
                    title_text = await page.title()
                except Exception:
                    title_text = ""
                log.info("Glassdoor loaded: title=%r signals=%s", title_text, list(walls.keys()))
                bad_title_markers = (
                    "502",
                    "503",
                    "403",
                    "bad gateway",
                    "access denied",
                    "error code",
                    "cloudflare",
                )
                t_low = title_text.lower()
                bad_title = any(m in t_low for m in bad_title_markers)
                hard_walls = [k for k in ("captcha_wall", "cloudflare_wall") if k in walls]
                if bad_title or hard_walls:
                    err = f"bad glassdoor landing (title={title_text!r} hard_walls={hard_walls})"
                    if attempt < NAVIGATE_MAX_ATTEMPTS:
                        log.warning("Glassdoor %s: retry %d/%d", err, attempt, NAVIGATE_MAX_ATTEMPTS)
                        try:
                            await ctx.close()
                        except Exception:
                            pass
                        ctx = None
                        handles = None
                        await _sleep(2.0, 3.5)
                        continue
                    else:
                        last_err = err
                        break
                role_loc = page.locator("#searchBar-jobTitle")
                loc_loc = page.locator("#searchBar-location")
                role_cnt = await role_loc.count()
                loc_cnt = await loc_loc.count()
                if role_cnt == 0 or loc_cnt == 0:
                    err = f"searchBar inputs missing (role={role_cnt} location={loc_cnt}) title={title_text!r}"
                    if attempt < NAVIGATE_MAX_ATTEMPTS:
                        log.warning("Glassdoor %s: retry %d/%d", err, attempt, NAVIGATE_MAX_ATTEMPTS)
                        try:
                            await ctx.close()
                        except Exception:
                            pass
                        ctx = None
                        handles = None
                        await _sleep(2.0, 3.5)
                        continue
                    else:
                        last_err = err
                        break
                rows = await _search_and_collect(
                    page=page,
                    query=query,
                    location_filter=location,
                    stats=stats,
                    run_stamp=run_stamp,
                    start_ts=start_ts,
                    target_cap=target_cap,
                )
                collected.extend(rows)
                break
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                log.warning("Glassdoor launch err (attempt %d): %s", attempt, last_err)
                await _sleep(1.5, 2.5)
            finally:
                if ctx is not None:
                    try:
                        await ctx.close()
                    except Exception:
                        pass
                if handles is not None and handles.browser is not None:
                    try:
                        await handles.browser.close()
                    except Exception:
                        pass
    finally:
        if not external_pw and playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                pass
    if not collected and last_err:
        log.warning("Glassdoor: no rows collected, last_err=%s", last_err)
    return collected


async def _sleep(lo: float, hi: float) -> None:
    import asyncio

    await asyncio.sleep(_jitter(lo, hi))
