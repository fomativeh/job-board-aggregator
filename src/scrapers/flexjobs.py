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
START_URL: Final[str] = "https://www.flexjobs.com/homevariant/t9"
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


async def _wait_url_change(page: Page, prev_url: str, timeout_ms: int = 45000) -> bool:
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        try:
            cur = page.url
            if cur != prev_url:
                if "/search" in cur.lower() or "/jobs" in cur.lower() or "searchkeyword" in cur.lower():
                    return True
        except Exception:
            pass
        try:
            href = await page.evaluate("() => location.href")
            if isinstance(href, str) and href != prev_url:
                if "/search" in href.lower() or "/jobs" in href.lower() or "searchkeyword" in href.lower():
                    return True
        except Exception:
            pass
        await asyncio.sleep(0.2)
    return False


async def _blacklist_wizard_links(page: Page) -> int:
    nav_log = log.getChild("nav")
    try:
        count = await page.evaluate(
            r"""() => {
  const badHref = (el) => {
    try {
      const href = (el.getAttribute && el.getAttribute('href')) || '';
      if (typeof href === 'string' && /\/job_wizard(\/|$)/i.test(href)) return true;
    } catch {}
    return false;
  };
  const badClass = (el) => {
    try {
      const cls = ((el && el.className && typeof el.className === 'string') ? el.className : (el.getAttribute && el.getAttribute('class')) || '').toLowerCase();
      if (!cls) return false;
      if (cls.includes('get_started')) return true;
      if (cls.includes('signup-button')) return true;
      if (cls.includes('signup_button')) return true;
    } catch {}
    return false;
  };
  const badText = (el) => {
    try {
      const txt = ((el.innerText || '') + ' ' + (el.textContent || '')).replace(/\s+/g,' ').trim().toLowerCase();
      if (!txt) return false;
      if (/^get\s*started\s*$/i.test(txt)) return true;
      if (/find\s+your\s+(next\s+)?remote\s+job/i.test(txt)) return true;
      if (/find\s+your\s+next\s+job/i.test(txt)) return true;
    } catch {}
    return false;
  };
  const candidates = Array.from(document.querySelectorAll('a, button, div[role="button"], span[role="button"]'));
  let touched = 0;
  for (const el of candidates) {
    const hit = badHref(el) || badClass(el) || badText(el);
    if (!hit) continue;
    try { el.removeAttribute('href'); } catch {}
    try {
      if ('setAttribute' in el) {
        el.setAttribute('href', 'javascript:void(0)');
      }
    } catch {}
    try { el.removeAttribute('role'); } catch {}
    try { el.removeAttribute('rel'); } catch {}
    try { el.removeAttribute('data-action'); } catch {}
    try { el.removeAttribute('onclick'); } catch {}
    try { el.addEventListener('click', (e) => { e.stopImmediatePropagation(); e.preventDefault(); return false; }, true); } catch {}
    try { el.addEventListener('mousedown', (e) => { e.stopImmediatePropagation(); e.preventDefault(); return false; }, true); } catch {}
    try { el.addEventListener('mouseup', (e) => { e.stopImmediatePropagation(); e.preventDefault(); return false; }, true); } catch {}
    try {
      if (el.style) {
        el.style.pointerEvents = 'none';
        el.style.visibility = 'hidden';
        el.style.display = 'none';
        el.style.opacity = '0';
      }
    } catch {}
    try { if ('disabled' in el) el.disabled = true; } catch {}
    touched += 1;
  }
  return touched;
}"""
        )
        if isinstance(count, int) and count > 0:
            nav_log.warning(
                "FlexJobs neutralized %d wizard-link/CTA elements (get_started / signup-button / /job_wizard href / Get Started text)",
                count,
            )
        return count if isinstance(count, int) else 0
    except Exception as e:
        nav_log.warning("FlexJobs _blacklist_wizard_links threw %s: %s", type(e).__name__, e)
        return 0


async def _try_submit(page: Page, loc_input: pw_api.Locator, submit_btn: Optional[pw_api.Locator]) -> None:
    nav_log = log.getChild("nav")
    if submit_btn is not None:
        used_submit: Optional[str] = None
        try:
            tag_ok = await page.evaluate(
                r"""() => {
  const b = document.getElementById('submit-search');
  if (!b) return false;
  return (b.tagName || '').toLowerCase() === 'button';
}"""
            )
            if not bool(tag_ok):
                nav_log.warning("submit-search element not a <button> (found %s); skipping button branch", await page.evaluate("() => { const b = document.getElementById('submit-search'); return b ? b.tagName : null; }"))
            else:
                try:
                    visible = await submit_btn.is_visible(timeout=2500)
                except Exception:
                    visible = False
                if visible:
                    try:
                        await submit_btn.click(force=True, timeout=6000)
                        used_submit = "btn-click-force"
                    except Exception:
                        try:
                            rv = await page.evaluate(
                                r"""() => {
  const b = document.getElementById('submit-search');
  if (!b) return null;
  if ((b.tagName || '').toLowerCase() !== 'button') return null;
  const evt = new MouseEvent('click', {bubbles:true, cancelable:true, view: window, button:0});
  b.dispatchEvent(evt);
  try { b.click(); } catch {}
  return 'btn-js-mouse-click';
}"""
                            )
                            if isinstance(rv, str):
                                used_submit = rv
                        except Exception:
                            pass
        except Exception:
            pass
        if used_submit is None:
            try:
                rv = await page.evaluate(
                    r"""() => {
  const b = document.getElementById('submit-search');
  if (!b) return null;
  if ((b.tagName || '').toLowerCase() !== 'button') return null;
  const evt = new MouseEvent('click', {bubbles:true, cancelable:true, view: window, button:0});
  b.dispatchEvent(evt);
  try { b.click(); } catch {}
  return 'btn-js-mouse-click-no-visibility';
}"""
                )
                if isinstance(rv, str):
                    used_submit = rv
            except Exception:
                pass
        if used_submit is not None:
            nav_log.info("FlexJobs submit used %s", used_submit)
            return
    loc_tag_ok = False
    try:
        loc_tag_ok = bool(await page.evaluate(
            r"""() => {
  const inp = document.getElementById('search-by-location');
  return inp && (inp.tagName || '').toLowerCase() === 'input';
}"""
        ))
    except Exception:
        loc_tag_ok = False
    if loc_tag_ok:
        try:
            await loc_input.focus(timeout=3000)
            await asyncio.sleep(_jitter(250, 150))
            await loc_input.press("Enter")
            nav_log.info("FlexJobs submit used loc_input.Enter (id=search-by-location INPUT)")
            return
        except Exception:
            pass
    raise FlexJobsScrapeError("Failed to submit search form")


async def _maybe_handle_job_wizard(
    page: Page,
    query: str,
    location_filter: str,
    *,
    skip_3rd_click: bool = True,
) -> bool:
    nav_log = log.getChild("nav")
    url_low = (page.url or "").lower()
    if "/job_wizard/" not in url_low and "why_remote" not in url_low:
        return False
    nav_log.warning(
        "Detected FlexJobs job_wizard page (%s). Skipping ALL overlay interactions, direct /search fallback only.",
        page.url,
    )
    try:
        await page.wait_for_load_state("domcontentloaded")
    except Exception:
        pass
    title_sel_val: str = query or ""
    try:
        maybe_val = await page.locator("input#search-by-param").input_value(timeout=1500)
        if maybe_val and isinstance(maybe_val, str) and maybe_val.strip():
            title_sel_val = maybe_val.strip()
    except Exception:
        pass
    direct = (
        "https://www.flexjobs.com/search?searchkeyword="
        + quote_plus(title_sel_val or "software")
        + "&joblocations="
        + quote_plus(location_filter or "remote")
        + "&usecLocation=true&Loc.LatLng=0%2C0&Loc.Radius=30&sortbyposteddate=true&fromHeader=true"
    )
    try:
        await page.goto(direct, wait_until="domcontentloaded", timeout=NAVIGATE_TIMEOUT_MS)
        try:
            await page.wait_for_load_state("domcontentloaded")
        except Exception:
            pass
        await _blacklist_wizard_links(page)
    except PlaywrightTimeoutError:
        pass
    return True


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


async def _search_and_collect(
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
    initial_url = START_URL

    kw_loc = page.locator("input#search-by-param")
    loc_loc = page.locator("input#search-by-location")
    submit_btn = page.locator("button#submit-search")

    current_url_low = (page.url or "").lower()
    if "/job_wizard/" in current_url_low or "why_remote" in current_url_low:
        nav_log.warning(
            "Page already on wizard %s before any fill; direct /search fallback NOW.",
            page.url,
        )
        await _maybe_handle_job_wizard(page, query, location, skip_3rd_click=True)

    async def _set_value_only(placeholder_contains: str, id_sel: str, value: str, label: str) -> str:
        async with asyncio.timeout(10):
            used = await page.evaluate(
                r"""([p, idSel, val]) => {
  const lc = (p || '').toLowerCase();
  const inputs = Array.from(document.querySelectorAll('input'));
  let target = null;
  for (const inp of inputs) {
    const ph = (inp.getAttribute('placeholder') || '').toLowerCase();
    const id = (inp.id || '').toLowerCase();
    if (lc && ph.indexOf(lc) >= 0 && inp.offsetParent !== null) { target = inp; break; }
  }
  if (!target) {
    for (const inp of inputs) {
      const id = (inp.id || '').toLowerCase();
      if (idSel && id === idSel.toLowerCase() && inp.offsetParent !== null) { target = inp; break; }
    }
  }
  if (!target) { target = inputs[0] || null; }
  if (!target) return 'no-inputs-found';
  target.value = val;
  return { 'target.id': target.id, 'placeholder': target.getAttribute('placeholder'), 'type': target.type, 'value': target.value };
}""",
                [placeholder_contains, id_sel, value],
            )
            nav_log.info("FlexJobs %s field set (value only, no focus / no events): %s", label, used)
            if isinstance(used, dict) and isinstance(used.get("target.id"), str):
                return used["target.id"]
            return ""

    async def _fill_field_with_events(placeholder_contains: str, id_sel: str, value: str, label: str) -> str:
        async with asyncio.timeout(10):
            used = await page.evaluate(
                r"""([p, idSel, val]) => {
  const lc = (p || '').toLowerCase();
  const inputs = Array.from(document.querySelectorAll('input'));
  let target = null;
  for (const inp of inputs) {
    const ph = (inp.getAttribute('placeholder') || '').toLowerCase();
    const id = (inp.id || '').toLowerCase();
    if (lc && ph.indexOf(lc) >= 0 && inp.offsetParent !== null) { target = inp; break; }
  }
  if (!target) {
    for (const inp of inputs) {
      const id = (inp.id || '').toLowerCase();
      if (idSel && id === idSel.toLowerCase() && inp.offsetParent !== null) { target = inp; break; }
    }
  }
  if (!target) { target = inputs[0] || null; }
  if (!target) return 'no-inputs-found';
  try { target.focus({preventScroll: true}); } catch {}
  target.value = val;
  target.dispatchEvent(new Event('input', {bubbles:true, cancelable:true}));
  target.dispatchEvent(new Event('change', {bubbles:true, cancelable:true}));
  return { 'target.id': target.id, 'placeholder': target.getAttribute('placeholder'), 'type': target.type, 'value': target.value };
}""",
                [placeholder_contains, id_sel, value],
            )
            nav_log.info("FlexJobs %s field assigned (with input/change events): %s", label, used)
            if isinstance(used, dict) and isinstance(used.get("target.id"), str):
                return used["target.id"]
            return ""

    async def _focus_field(id_sel: str, placeholder_contains: str) -> bool:
        async with asyncio.timeout(8):
            ok = await page.evaluate(
                r"""([idSel, phText]) => {
  const idLower = (idSel || '').toLowerCase();
  const phLower = (phText || '').toLowerCase();
  const inputs = Array.from(document.querySelectorAll('input'));
  let target = null;
  if (idLower) {
    for (const inp of inputs) {
      const id = (inp.id || '').toLowerCase();
      if (id === idLower && inp.offsetParent !== null) { target = inp; break; }
    }
  }
  if (!target && phLower) {
    for (const inp of inputs) {
      const ph = (inp.getAttribute('placeholder') || '').toLowerCase();
      if (ph.indexOf(phLower) >= 0 && inp.offsetParent !== null) { target = inp; break; }
    }
  }
  if (!target) return false;
  try { target.focus({preventScroll: false}); } catch { return false; }
  return true;
}""",
                [id_sel, placeholder_contains],
            )
            return bool(ok)

    try:
        await _set_value_only("Search by job title", "search-by-param", query, "keyword")
    except Exception as e:
        try:
            async with asyncio.timeout(8):
                fallback_id = await page.evaluate(
                    [r"""([q]) => {
  const inputs = Array.from(document.querySelectorAll('input'));
  let target = null;
  for (const inp of inputs) {
    const ph = (inp.getAttribute('placeholder') || '').toLowerCase();
    const id = (inp.id || '').toLowerCase();
    if (ph.indexOf('search by job title') >= 0 && inp.offsetParent !== null) { target = inp; break; }
  }
  if (!target) {
    for (const inp of inputs) {
      const id = (inp.id || '').toLowerCase();
      if (id === 'search-by-param' && inp.offsetParent !== null) { target = inp; break; }
    }
  }
  if (!target) target = inputs[0] || null;
  if (!target) return null;
  target.value = q;
  return target.id || '';
}""", [query]],
                )
                if not isinstance(fallback_id, str) or not fallback_id:
                    raise FlexJobsScrapeError(f"Keyword fallback no-inputs: primary={e}")
        except FlexJobsScrapeError:
            raise
        except Exception as e2:
            raise FlexJobsScrapeError(f"Failed to set keyword field value: primary={e} fallback={e2}")

    try:
        focused = await _focus_field("search-by-location", "Search by location")
        if not focused:
            try:
                await loc_loc.focus(timeout=3000)
            except Exception:
                pass
    except Exception:
        try:
            await loc_loc.focus(timeout=3000)
        except Exception:
            pass

    await asyncio.sleep(_jitter(200, 200))
    try:
        await _fill_field_with_events("Search by location", "search-by-location", location, "location")
    except Exception as e:
        try:
            async with asyncio.timeout(8):
                filled_ok = await page.evaluate(
                    [r"""([loc]) => {
  const inputs = Array.from(document.querySelectorAll('input'));
  let target = null;
  for (const inp of inputs) {
    const id = (inp.id || '').toLowerCase();
    if (id === 'search-by-location' && inp.offsetParent !== null) { target = inp; break; }
  }
  if (!target) {
    for (const inp of inputs) {
      const ph = (inp.getAttribute('placeholder') || '').toLowerCase();
      if (ph.indexOf('search by location') >= 0 && inp.offsetParent !== null) { target = inp; break; }
    }
  }
  if (!target) return false;
  if ((target.tagName || '').toLowerCase() !== 'input') return false;
  try { target.focus({preventScroll: true}); } catch {}
  target.value = loc;
  target.dispatchEvent(new Event('input', {bubbles:true, cancelable:true}));
  target.dispatchEvent(new Event('change', {bubbles:true, cancelable:true}));
  return true;
}""", [location]],
                )
                if not bool(filled_ok):
                    raise FlexJobsScrapeError(f"Failed to fill location field: primary={e} fallback=no valid <input id=search-by-location>")
        except FlexJobsScrapeError:
            raise
        except Exception as e2:
            raise FlexJobsScrapeError(f"Failed to fill location field: primary={e} fallback={e2}")

    prev_url = page.url
    try:
        async with asyncio.timeout(12):
            await _try_submit(page, loc_loc, submit_btn)
    except FlexJobsScrapeError:
        raise
    except Exception as e:
        raise FlexJobsScrapeError(f"Submit block exceeded timeout: {e}")

    changed = await _wait_url_change(page, prev_url, timeout_ms=50000)
    if not changed:
        nav_log.warning("First submit did not change URL; retrying submit once more after extra 3s settle")
        await asyncio.sleep(3.0)
        try:
            await _try_submit(page, loc_loc, submit_btn)
        except FlexJobsScrapeError:
            pass
        changed = await _wait_url_change(page, prev_url, timeout_ms=35000)
    await _maybe_handle_job_wizard(page, query, location, skip_3rd_click=True)
    try:
        results_anchor = page.locator("div[data-index], #search-pagination")
        await results_anchor.first.wait_for(state="attached", timeout=40_000)
    except PlaywrightTimeoutError:
        nav_log.warning("Results anchor not visible after submit; proceeding anyway")
    await asyncio.sleep(_jitter(SEARCH_SETTLE_MS, 1200))
    await _snapshot(page, "step1_after_submit")
    scrape_log.info("FlexJobs after submit URL: %s", page.url)
    still_on_index = (page.url.rstrip("/") == initial_url.rstrip("/"))

    batch = await _extract_cards(page, seen_card_keys, out_rows, query, scrape_log)
    initial_cards = batch["batch_seen"]
    if "/job_wizard/" in page.url.lower() or "why_remote" in page.url.lower():
        nav_log.warning("Still on job wizard after initial parse; handle again")
        await _maybe_handle_job_wizard(page, query, location, skip_3rd_click=True)
        batch = await _extract_cards(page, seen_card_keys, out_rows, query, scrape_log)
        initial_cards = batch["batch_seen"]
    if still_on_index and initial_cards == 0:
        nav_log.warning("Still on index page, 0 cards; extra settle + reparse")
        await asyncio.sleep(_jitter(5000, 4000))
        try:
            results_loc = page.locator("div[data-index]")
            await results_loc.first.wait_for(state="attached", timeout=20_000)
        except PlaywrightTimeoutError:
            pass
        batch = await _extract_cards(page, seen_card_keys, out_rows, query, scrape_log)
        if batch["batch_seen"] == 0:
            scrape_log.warning("2 attempts, 0 cards; re-submit via submit-search button + wait")
            try:
                await _blacklist_wizard_links(page)
                try:
                    async with asyncio.timeout(10):
                        await _try_submit(page, loc_loc, submit_btn)
                except Exception:
                    pass
                await _wait_url_change(page, page.url, timeout_ms=30000)
                await asyncio.sleep(_jitter(4500, 2000))
                batch = await _extract_cards(page, seen_card_keys, out_rows, query, scrape_log)
            except Exception:
                pass

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
            if consecutive_no_growth >= 3:
                nav_log.info(
                    "FlexJobs stop condition 1: next link absent/disabled AND 3 consecutive 0-new-cards => pages exhausted"
                )
                break
            nav_log.info("FlexJobs next link absent/disabled; checking 3-consecutive threshold (consec_no_growth=%d)", consecutive_no_growth)
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
        await _blacklist_wizard_links(page)
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
    try:
        if not external_pw:
            playwright = await async_playwright().start()
        assert playwright is not None

        for attempt in range(1, NAVIGATE_MAX_ATTEMPTS + 1):
            nav_log.info("FlexJobs open attempt %d/%d start URL=%s", attempt, NAVIGATE_MAX_ATTEMPTS, START_URL)
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
                    resp = await page.goto(START_URL, wait_until="domcontentloaded", timeout=NAVIGATE_TIMEOUT_MS)
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
                await _blacklist_wizard_links(page)
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
                kw_count = await page.locator("input#search-by-param").count()
                loc_count = await page.locator("input#search-by-location").count()
                btn_count = await page.locator("button#submit-search").count()
                nav_log.info(
                    "FlexJobs attempt %d title=%r kw_inputs=%d loc_inputs=%d submit_btns=%d wall=%s resp_status=%s",
                    attempt, title[:80], kw_count, loc_count, btn_count, wall_hits,
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
                if kw_count < 1 or loc_count < 1:
                    nav_log.warning("FlexJobs attempt %d missing kw/loc inputs; bad page; retry", attempt)
                    last_err = FlexJobsScrapeError(f"Bad load: kw={kw_count} loc={loc_count}")
                    await _snapshot(page, f"attempt{attempt}_bad_load")
                    try:
                        await ctx.close()
                        await browser.close()
                    except Exception:
                        pass
                    ctx = None
                    browser = None
                    await asyncio.sleep(2.0 * attempt)
                    continue
                await _snapshot(page, f"attempt{attempt}_initial_load")

                rows = await _search_and_collect(page, query, location, max_listings)
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
