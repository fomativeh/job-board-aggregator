from __future__ import annotations

import asyncio
import json as _json
import logging
import random
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Optional
from urllib.parse import urlparse

from patchright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    Route,
    TimeoutError as PlaywrightTimeoutError,
    ViewportSize,
    async_playwright,
)

from ..schema import JobListing, SalaryType, make_url_hash, utc_now_iso

log: logging.Logger = logging.getLogger(__name__)

SOURCE_NAME: Final[str] = "glassdoor"
START_URL: Final[str] = "https://www.glassdoor.com/Job/index.htm"
NAVIGATE_TIMEOUT_MS: Final[int] = 90_000
SCRAPER_TOTAL_TIMEOUT_SECONDS: Final[int] = 900
MAX_LOAD_MORE_BATCHES: Final[int] = 80
LOAD_MORE_POLL_DEADLINE_SEC: Final[float] = 25.0
NAVIGATE_MAX_ATTEMPTS: Final[int] = 3
SAMPLE_LOG_FIRST_N_CARDS: Final[int] = 5
RAW_DUMP_FIRST_N_CARDS: Final[int] = 5

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent.parent
SESSION_DIR: Final[Path] = PROJECT_ROOT / "session"
PROFILE_DIR: Final[Path] = SESSION_DIR / "patchright_chrome_profile_glassdoor"
for _sub in (SESSION_DIR, PROFILE_DIR):
    _sub.mkdir(parents=True, exist_ok=True)

_STALE_DIRS: tuple[Path, ...] = (
    PROFILE_DIR / "Default" / "GPUCache",
    PROFILE_DIR / "Default" / "Service Worker" / "ScriptCache",
    PROFILE_DIR / "Default" / "Code Cache",
    PROFILE_DIR / "Default" / "Cache",
)
for _d in _STALE_DIRS:
    if _d.exists():
        try:
            shutil.rmtree(_d)
        except Exception:
            pass

LANGS: Final[str] = "en-US,en;q=0.9"

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
        "united states",
        "us",
        "usa",
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
        if cand == "" or cand.lower() in {"", "any location", "multiple", "remote (temporarily remote", "remote", "hybrid remote"}:
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


def _normalize_job(
    title: str,
    company: str,
    location: str,
    salary_raw: str,
    url: str,
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
    if not _matches_location(location_filter, location):
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
    return listing


_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)
_CHROME_SEC_CH_UA = '"Chromium";v="128", "Not)A;Brand";v="24", "Google Chrome";v="128"'
_SEC_CH_UA_ARCH = '"x86"'
_SEC_CH_UA_BITNESS = '"64"'
_SEC_CH_UA_FULL_VERSION = '"128.0.6613.137"'
_SEC_CH_UA_FULL_VERSION_LIST = (
    '"Chromium";v="128.0.6613.137", '
    '"Not)A;Brand";v="24.0.0.0", '
    '"Google Chrome";v="128.0.6613.137"'
)
_SEC_CH_UA_PLATFORM_VERSION = '"15.0.0"'
_SEC_CH_UA_WOW64 = "?0"
_SEC_CH_UA_MODEL = '""'

_FETCH_LIKE_RE = re.compile(r"^(fetch|xhr|jsonp|cors|script)$", re.I)


def _infer_resource_type(request: Any) -> str:
    try:
        rt = str(request.resource_type or "")
    except Exception:
        rt = ""
    if rt:
        return rt
    try:
        u = str(request.url or "")
    except Exception:
        u = ""
    lower = u.lower()
    if any(lower.endswith(ext) for ext in (".css",)):
        return "stylesheet"
    if any(lower.endswith(ext) for ext in (".js", ".mjs")):
        return "script"
    if any(lower.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico")):
        return "image"
    if any(lower.endswith(ext) for ext in (".woff", ".woff2", ".ttf", ".otf")):
        return "font"
    return "document"


async def _route_stealth(route: Route) -> None:
    try:
        request = route.request
        headers = dict(request.headers or {})
        resource_type = _infer_resource_type(request)
    except Exception:
        try:
            await route.continue_()
        except Exception:
            pass
        return
    try:
        headers.setdefault("User-Agent", _CHROME_UA)
        headers["Accept-Language"] = LANGS
        headers["Sec-CH-UA-Mobile"] = "?0"
        headers["Sec-CH-UA-Platform"] = '"Windows"'
        headers["Sec-CH-UA"] = _CHROME_SEC_CH_UA
        headers["Sec-CH-UA-Arch"] = _SEC_CH_UA_ARCH
        headers["Sec-CH-UA-Bitness"] = _SEC_CH_UA_BITNESS
        headers["Sec-CH-UA-Full-Version"] = _SEC_CH_UA_FULL_VERSION
        headers["Sec-CH-UA-Full-Version-List"] = _SEC_CH_UA_FULL_VERSION_LIST
        headers["Sec-CH-UA-Platform-Version"] = _SEC_CH_UA_PLATFORM_VERSION
        headers["Sec-CH-UA-WoW64"] = _SEC_CH_UA_WOW64
        headers["Sec-CH-UA-Model"] = _SEC_CH_UA_MODEL
        headers["Upgrade-Insecure-Requests"] = "1"

        if resource_type in ("document", "manifest", "other"):
            headers["Sec-Fetch-Dest"] = "document"
            headers["Sec-Fetch-Mode"] = "navigate"
            headers["Sec-Fetch-Site"] = headers.get("Referer") and "same-origin" or "none"
            headers["Sec-Fetch-User"] = "?1"
        elif resource_type == "script":
            headers["Sec-Fetch-Dest"] = "script"
            headers["Sec-Fetch-Mode"] = "no-cors"
            headers["Sec-Fetch-Site"] = headers.get("Referer") and "same-origin" or "cross-site"
        elif resource_type == "stylesheet":
            headers["Sec-Fetch-Dest"] = "style"
            headers["Sec-Fetch-Mode"] = "no-cors"
            headers["Sec-Fetch-Site"] = "same-origin"
        elif resource_type == "image":
            headers["Sec-Fetch-Dest"] = "image"
            headers["Sec-Fetch-Mode"] = "no-cors"
            headers["Sec-Fetch-Site"] = "same-origin"
        elif resource_type == "font":
            headers["Sec-Fetch-Dest"] = "font"
            headers["Sec-Fetch-Mode"] = "no-cors"
            headers["Sec-Fetch-Site"] = "same-origin"
        else:
            headers["Sec-Fetch-Dest"] = "empty"
            headers["Sec-Fetch-Mode"] = "cors" if _FETCH_LIKE_RE.match(resource_type) else "no-cors"
            headers["Sec-Fetch-Site"] = "same-origin"
        if not headers.get("Referer"):
            try:
                url = str(request.url or "")
            except Exception:
                url = ""
            host = ""
            try:
                host = urlparse(url).netloc.lower()
            except Exception:
                host = ""
            if host and not host.endswith("google.com"):
                headers["Referer"] = "https://www.google.com/"
    except Exception:
        pass
    try:
        await route.continue_(headers=headers)
    except Exception:
        try:
            await route.continue_()
        except Exception:
            pass


_AUTH_INIT_JS = r"""
(() => {
  const closeBtnSel = 'button[data-test="auth-modal-close-button"]';
  const dialogSel = 'dialog[aria-modal="true"]';
  const topSectionSel = 'div[data-test="unified-auth-modal-top-section"]';
  const backdropSel = 'div[data-test="modal-backdrop"], [data-test*="ModalBackdrop"]';

  function dismissAuthOnce() {
    let changed = false;
    try {
      const btn = document.querySelector(closeBtnSel);
      if (btn && btn.isConnected) {
        try { btn.click(); changed = true; } catch (_) {}
      }
    } catch (_) {}
    try {
      const topSec = document.querySelector(topSectionSel);
      if (topSec && topSec.isConnected) {
        const host = topSec.closest('dialog, [role="dialog"], [aria-modal="true"]')
                   || topSec.parentElement?.closest('[aria-modal="true"]')
                   || (topSec.parentElement?.parentElement);
        if (host) {
          if (typeof host.close === 'function') { try { host.close(); } catch (_) {} }
          host.removeAttribute('open');
          host.setAttribute('aria-hidden', 'true');
          host.style.display = 'none';
          host.style.visibility = 'hidden';
          changed = true;
        }
      }
    } catch (_) {}
    try {
      document.querySelectorAll(dialogSel).forEach((d) => {
        const content = d.innerHTML || '';
        if (
          content.includes('unified-auth-modal') ||
          content.includes('auth-modal-close-button') ||
          d.querySelector(topSectionSel)
        ) {
          if (typeof d.close === 'function') { try { d.close(); } catch (_) {} }
          d.removeAttribute('open');
          d.setAttribute('aria-hidden', 'true');
          d.style.display = 'none';
          d.style.visibility = 'hidden';
          changed = true;
        }
      });
    } catch (_) {}
    try {
      document.querySelectorAll(backdropSel).forEach((b) => {
        b.style.display = 'none';
        b.style.visibility = 'hidden';
        b.style.pointerEvents = 'none';
      });
    } catch (_) {}
    try {
      if (document.body.style.overflow === 'hidden' || document.documentElement.style.overflow === 'hidden') {
        if (document.querySelector(topSectionSel) || document.querySelector(closeBtnSel)) {
          document.body.style.overflow = '';
          document.documentElement.style.overflow = '';
        }
      }
    } catch (_) {}
    return changed;
  }

  let lastDismissAt = 0;
  function poll() {
    try {
      const changed = dismissAuthOnce();
      if (changed) {
        const now = Date.now();
        if (now - lastDismissAt > 400) { lastDismissAt = now; }
      }
    } catch (_) {}
    setTimeout(poll, 250);
  }

  try {
    const mo = new MutationObserver(() => {
      try { dismissAuthOnce(); } catch (_) {}
    });
    mo.observe(document.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ['open', 'aria-modal', 'class', 'style'] });
  } catch (_) {}

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', poll, { once: true });
  } else {
    poll();
  }
})();
"""

_EXTRACT_CARD_JS = """(liEl) => {
  const qs = (root, sel) => (root ? root.querySelector(sel) : null);
  const txt = (el) => (el && typeof el.textContent === 'string' ? el.textContent.trim() : '');
  const safe = (s) => (typeof s === 'string' ? s.trim() : '');

  const employerName = '';
  const empNameEl = qs(liEl, 'span[class*="EmployerProfile_compactEmployerName__"], div[class*="EmployerProfile_employerNameContainer__"] span[class*="EmployerProfile_employerNameHeading"]');
  const companyText = (empNameEl && (empNameEl.textContent || empNameEl.innerText || '')).toString().trim() || '';

  const titleEl = qs(liEl, 'a[data-test="job-title"], a[class*="JobCard_jobTitle__"]');
  const title = txt(titleEl) || '';

  const locEl = qs(liEl, '[data-test="emp-location"], [class*="JobCard_location__"]');
  const location = txt(locEl) || '';

  const salEl = qs(liEl, '[data-test="detailSalary"], [class*="JobCard_salaryEstimate__"]');
  const salaryRaw = txt(salEl) || '';
  let salary = salaryRaw;
  if (salaryRaw) {
    const br = salaryRaw.indexOf('(');
    if (br > 0) salary = salaryRaw.slice(0, br).trim();
  }

  let url = '';
  const urlEls = liEl.querySelectorAll('a[data-test="job-title"], a[class*="JobCard_jobTitle__"]');
  for (const ue of urlEls) {
    const h = ue.getAttribute && ue.getAttribute('href');
    if (h) { url = h; break; }
  }
  if (!url) {
    const alt = liEl.querySelectorAll('a[data-test="job-link"], a[class*="JobCard_trackingLink__"]');
    for (const ue of alt) {
      const h = ue.getAttribute && ue.getAttribute('href');
      if (h) { url = h; break; }
    }
  }
  if (url && !url.startsWith('http')) {
    try { url = new URL(url, location.href).href; } catch (_) {}
  }

  return {
    "Job title": safe(title) || null,
    "company": safe(companyText) || null,
    "location": safe(location) || null,
    "salary": safe(salary) || null,
    "url": safe(url) || null,
  };
}"""


def _absolutize(url: str, base: str) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    if u.startswith("http://") or u.startswith("https://"):
        return u
    try:
        from urllib.parse import urljoin
        return urljoin(base, u)
    except Exception:
        return u


async def _dismiss_with_locator_if_visible(page: Page) -> bool:
    close_sel = 'button[data-test="auth-modal-close-button"]'
    top_sel = 'div[data-test="unified-auth-modal-top-section"]'
    dialog_sel = 'dialog[aria-modal="true"]'
    try:
        has_top = False
        try:
            has_top = bool(await page.evaluate(
                f'() => !!document.querySelector(' + repr(top_sel) + ')'
            ))
        except Exception:
            has_top = False
        if not has_top:
            return False
    except Exception:
        pass
    try:
        try:
            vis = await page.evaluate(
                """([closeSel]) => {
                    const btn = document.querySelector(closeSel);
                    if (!btn) return false;
                    const st = btn.ownerDocument && btn.ownerDocument.defaultView ? btn.ownerDocument.defaultView.getComputedStyle(btn) : null;
                    if (!st) return !!btn.isConnected;
                    return st.display !== 'none' && st.visibility !== 'hidden';
                }""",
                [close_sel],
            )
            if vis:
                await page.evaluate(
                    """([closeSel, topSel, dialogSel]) => {
                        let changed = 0;
                        try {
                            const b = document.querySelector(closeSel);
                            if (b && b.isConnected && typeof b.click === 'function') { b.click(); changed++; }
                        } catch (_) {}
                        try {
                            const top = document.querySelector(topSel);
                            if (top && top.isConnected) {
                                const host = top.closest('dialog, [role="dialog"], [aria-modal="true"]')
                                           || (top.parentElement && top.parentElement.closest('[aria-modal="true"]'));
                                if (host) {
                                    if (typeof host.close === 'function') { try { host.close(); } catch (_) {} }
                                    host.removeAttribute('open');
                                    host.style.display = 'none';
                                    changed++;
                                }
                            }
                        } catch (_) {}
                        try {
                            document.querySelectorAll(dialogSel).forEach((d) => {
                                const h = d.innerHTML || '';
                                if (h.indexOf('unified-auth-modal') !== -1 || h.indexOf('auth-modal-close') !== -1 || d.querySelector(topSel)) {
                                    if (typeof d.close === 'function') { try { d.close(); } catch (_) {} }
                                    d.removeAttribute('open');
                                    d.style.display = 'none';
                                    changed++;
                                }
                            });
                        } catch (_) {}
                        return changed;
                    }""",
                    [close_sel, top_sel, dialog_sel],
                )
                return True
        except Exception:
            return False
    except Exception:
        return False


def _count_lis(page: Page, li_sel: str) -> int:
    pass


async def _count_li_count(page: Page, li_sel: str) -> int:
    try:
        n = await page.evaluate(
            """(sel) => {
                const list = document.querySelectorAll(sel);
                return list ? list.length : 0;
            }""",
            li_sel,
        )
    except Exception:
        return 0
    try:
        return int(n or 0)
    except Exception:
        return 0


async def _batch_extract(page: Page, li_sel: str, start_idx: int = 0) -> list[dict[str, Any]]:
    try:
        raw: Any = await page.evaluate(
            """([liSel, cardFn, startIdx]) => {
                const fn = eval('(' + cardFn + ')');
                const nodes = document.querySelectorAll(liSel);
                const out = [];
                const end = nodes ? nodes.length : 0;
                for (let i = startIdx; i < end; i++) {
                    try { out.push(fn(nodes[i])); }
                    catch (e) { out.push({ "Job title": null, "company": null, "location": null, "salary": null, "url": null }); }
                }
                return out;
            }""",
            [li_sel, _EXTRACT_CARD_JS, int(start_idx)],
        )
    except Exception as e:
        log.warning("batch_extract evaluate exc: %s", e)
        return []
    if not isinstance(raw, list):
        return []
    return [r for r in raw if isinstance(r, dict)]


async def _submit_search(page: Page, query: str, location_filter: str) -> str:
    job_sel = "#searchBar-jobTitle"
    loc_sel = "#searchBar-location"
    try:
        role = page.locator(job_sel)
        await role.wait_for(timeout=30_000, state="visible")
        await role.click(timeout=4000)
        await page.wait_for_timeout(int(_jitter(300, 600)))
        await role.fill(query, timeout=6000)
        await page.wait_for_timeout(int(_jitter(600, 1200)))
    except Exception as e:
        raise GlassdoorScrapeError(f"role input failed: {e}")
    try:
        loc = page.locator(loc_sel)
        await loc.wait_for(timeout=30_000, state="visible")
        await loc.click(timeout=4000)
        await page.wait_for_timeout(int(_jitter(300, 600)))
        await loc.fill(location_filter, timeout=6000)
        await page.wait_for_timeout(int(_jitter(600, 1200)))
    except Exception as e:
        raise GlassdoorScrapeError(f"location input failed: {e}")
    try:
        async with page.expect_navigation(timeout=NAVIGATE_TIMEOUT_MS, wait_until="domcontentloaded") as nav:
            await page.locator(loc_sel).press("Enter", timeout=6000)
        resp = await nav.value
        new_url = str(resp.url) if resp else ""
    except Exception:
        new_url = page.url
    return new_url or ""


async def _wait_for_search_url_change(
    page: Page,
    pre_submit_url: str,
    query: str,
    location_filter: str,
    timeout_ms: int = 45_000,
) -> str:
    q_tokens = _tokens(query)
    l_tokens = _tokens(location_filter)

    def _looks_like_search_result(u: str, pre: str) -> bool:
        if not u or not pre:
            return False
        if u.rstrip("/") == pre.rstrip("/"):
            return False
        try:
            p = urlparse(u)
        except Exception:
            return False
        path = p.path or ""
        if "/SRCH_" in path:
            return True
        if "/job/" in path.lower() or "/jobs/" in path.lower() or "/Job/" in path:
            return True
        combined = f"{p.path}?{p.query}".lower()
        for t in q_tokens:
            if len(t) >= 4 and t in combined:
                return True
        for t in l_tokens:
            if len(t) >= 3 and t in combined:
                return True
        if "pos=" in p.query.lower() or "srch" in p.query.lower():
            return True
        return False

    deadline = _perf_counter() + (timeout_ms / 1000.0)
    last = page.url or pre_submit_url
    while _perf_counter() < deadline:
        current = page.url or last
        if _looks_like_search_result(current, pre_submit_url):
            return current
        try:
            await page.wait_for_timeout(500)
        except Exception:
            break
    if _looks_like_search_result(page.url or "", pre_submit_url):
        return page.url or ""
    raise GlassdoorScrapeError(
        f"post-submit URL did not change to a search-result page within {timeout_ms/1000:.0f}s "
        f"(pre={pre_submit_url[:180]!r} post={(page.url or '')[:180]!r}); "
        "aborting to avoid scraping pre-loaded/recent-searches cards"
    )


async def _search_and_collect(
    page: Page,
    query: str,
    location_filter: str,
    target_cap: Optional[int],
    pre_search_li_count: int = 0,
) -> list[JobListing]:
    try:
        await page.wait_for_timeout(int(_jitter(3000, 5000)))
    except Exception:
        pass
    try:
        await _dismiss_with_locator_if_visible(page)
    except Exception:
        pass

    ul_sel = 'ul[aria-label="Jobs List"][class*="JobsList_jobsList__"]'
    li_sel = ul_sel + " > li"
    load_more_btn_sel = 'button[data-test="load-more"]'

    try:
        await page.wait_for_selector(ul_sel, timeout=30_000, state="visible")
    except Exception as e:
        raise GlassdoorScrapeError(f"jobs list ul not found: {e}")

    total = await _count_li_count(page, li_sel)
    effective_first_start = max(0, int(pre_search_li_count or 0))
    if effective_first_start > 0:
        log.info(
            "first-batch slice guard: extracting POST-SEARCH lis only from idx=%d (pre-search had %d stale cards; current post-search ul total=%d)",
            effective_first_start,
            effective_first_start,
            total,
        )
        if total <= effective_first_start:
            raise GlassdoorScrapeError(
                f"post-search ul only has {total} li <= pre-search stale count={effective_first_start}; "
                "no new cards added after real search — guard aborted to skip pre-loaded list"
            )
    if total == 0:
        try:
            await page.evaluate(
                '() => { const n = document.querySelector(\'ul[aria-label="Jobs List"]\'); if (n) n.scrollIntoView({block: "start"}); window.scrollBy(0, 600); }'
            )
            await page.wait_for_timeout(1500)
            total = await _count_li_count(page, li_sel)
        except Exception:
            pass
    if total == 0:
        raise GlassdoorScrapeError("no li children found under jobs list ul")

    seen_urls: set[str] = set()
    out_rows: list[JobListing] = []
    page_base = page.url or ""
    stop_reason = ""

    async def _ingest(batch: list[dict[str, Any]], start_label: str) -> tuple[int, int]:
        kept_now = 0
        seen_now = 0
        for i, row in enumerate(batch):
            if not isinstance(row, dict):
                continue
            seen_now += 1
            title = str(row.get("Job title") or "")
            company = str(row.get("company") or "")
            loc = str(row.get("location") or "")
            sal = str(row.get("salary") or "")
            u_raw = str(row.get("url") or "")
            url = _absolutize(u_raw, page_base)
            if not url:
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)
            listing = _normalize_job(title, company, loc, sal, url, query, location_filter)
            if listing is None:
                continue
            out_rows.append(listing)
            kept_now += 1
            if len(out_rows) <= SAMPLE_LOG_FIRST_N_CARDS:
                log.info(
                    "progress %d/%d keep=%d keep_total=%d",
                    len(out_rows),
                    total,
                    kept_now,
                    len(out_rows),
                )
            if target_cap is not None and len(out_rows) >= target_cap:
                return seen_now, kept_now
        return seen_now, kept_now

    def _log_progress(seen_total: int) -> None:
        if seen_total % 5 == 0 or (seen_total == total):
            log.info(
                "progress %d/%d keep=%d keep_total=%d",
                seen_total,
                max(total, seen_total),
                0,
                len(out_rows),
            )

    initial_batch = await _batch_extract(page, li_sel, effective_first_start)
    if not initial_batch:
        try:
            await page.evaluate(
                '() => { const n = document.querySelector(\'ul[aria-label="Jobs List"]\'); if (n) n.scrollIntoView({block: "start"}); window.scrollBy(0, 600); }'
            )
            await page.wait_for_timeout(1500)
            initial_batch = await _batch_extract(page, li_sel, effective_first_start)
        except Exception:
            pass
    if initial_batch:
        for idx_diag in range(min(RAW_DUMP_FIRST_N_CARDS, len(initial_batch))):
            r = initial_batch[idx_diag]
            if isinstance(r, dict):
                log.info(
                    "GLASS_DOOR_RAW_CARD_%d/%d: jlid=<n/a> title=%r location=%r salary=%r",
                    idx_diag + 1,
                    RAW_DUMP_FIRST_N_CARDS,
                    str(r.get("Job title") or "")[:120],
                    str(r.get("location") or "")[:80],
                    str(r.get("salary") or "")[:100],
                )
    _seen, _kept = await _ingest(initial_batch, "batch-0")
    cursor_total = len(initial_batch) if isinstance(initial_batch, list) else total
    _log_progress(cursor_total)
    log.info(
        "initial page  seen=%d  keep=%d  keep_total=%d  url=%s",
        cursor_total,
        len(out_rows),
        len(out_rows),
        (page.url or "")[:160],
    )
    if target_cap is not None and len(out_rows) >= target_cap:
        stop_reason = "max_listings_satisfied"

    load_more_clicks = 0
    consecutive_no_growth = 0
    last_batch_before = len(out_rows)

    while not stop_reason and load_more_clicks < MAX_LOAD_MORE_BATCHES:
        if target_cap is not None and len(out_rows) >= target_cap:
            stop_reason = "max_listings_satisfied"
            break
        initial_count = cursor_total
        try:
            btn_exists = bool(await page.evaluate(
                f'() => {{ const b = document.querySelector({repr(load_more_btn_sel)}); if (!b) return false; const st = b.ownerDocument && b.ownerDocument.defaultView ? b.ownerDocument.defaultView.getComputedStyle(b) : null; if (!st) return !!b.isConnected; return st.display !== "none" && st.visibility !== "hidden" && !b.hasAttribute("disabled"); }}'
            ))
        except Exception:
            btn_exists = False
        if not btn_exists:
            log.info("load-more button absent => done")
            stop_reason = "all_pages_exhausted"
            break
        try:
            await page.evaluate(
                f'() => {{ const b = document.querySelector({repr(load_more_btn_sel)}); if (b) b.scrollIntoView({{block: "center", inline: "center"}}); window.scrollBy(0, 150); }}'
            )
        except Exception:
            pass
        await page.wait_for_timeout(int(_jitter(400, 900)))
        try:
            await page.locator(load_more_btn_sel).click(timeout=10_000)
            load_more_clicks += 1
        except Exception as e:
            log.warning("Glassdoor load-more click failed (iter %d): %s", load_more_clicks, e)
            consecutive_no_growth += 1
            if consecutive_no_growth >= 2:
                stop_reason = "all_pages_exhausted"
                break
            continue
        polled_new = 0
        deadline = time.monotonic() + LOAD_MORE_POLL_DEADLINE_SEC
        while time.monotonic() < deadline:
            try:
                now_count = await _count_li_count(page, li_sel)
            except Exception:
                now_count = initial_count
            if now_count > initial_count:
                polled_new = now_count - initial_count
                break
            try:
                await page.evaluate(
                    f'() => {{ const b = document.querySelector({repr(load_more_btn_sel)}); if (b) b.scrollIntoView({{block: "center"}}); window.scrollBy(0, 120); }}'
                )
            except Exception:
                pass
            await page.wait_for_timeout(400)
        try:
            after_count = await _count_li_count(page, li_sel)
        except Exception:
            after_count = initial_count
        new_added = max(after_count - initial_count, polled_new)
        if new_added <= 0:
            consecutive_no_growth += 1
            if consecutive_no_growth >= 3:
                log.info("3 consecutive clicks with 0 new lis => done")
                stop_reason = "all_pages_exhausted"
                break
            await page.wait_for_timeout(int(_jitter(700, 1400)))
            continue
        consecutive_no_growth = 0
        post_batch = await _batch_extract(page, li_sel, initial_count)
        if post_batch:
            await _ingest(post_batch, f"batch-{load_more_clicks}")
        cursor_total = initial_count + new_added
        _log_progress(cursor_total)
        if len(out_rows) == last_batch_before:
            consecutive_no_growth += 1
            if consecutive_no_growth >= 3:
                stop_reason = "all_pages_exhausted"
                break
        else:
            consecutive_no_growth = 0
            last_batch_before = len(out_rows)

    if not stop_reason:
        stop_reason = "max_load_more_batches"

    log.info(
        "final  seen=%d  keep=%d  clicks=%d  stop=%s  elapsed=%.2fs",
        cursor_total,
        len(out_rows),
        load_more_clicks,
        stop_reason,
        0.0,
    )
    if target_cap is not None and len(out_rows) > target_cap:
        out_rows = out_rows[:target_cap]
    return out_rows


@dataclass(frozen=True)
class _BrowserHandles:
    ctx: BrowserContext
    browser: Optional[Browser] = None


async def _launch_context(pw: Playwright) -> _BrowserHandles:
    args = [
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--start-maximized",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
    ]
    ctx: BrowserContext = await pw.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        channel="chrome",
        headless=False,
        args=args,
        no_viewport=True,
        viewport=None,
        locale="en-US",
        timezone_id="America/New_York",
        extra_http_headers={
            "Accept-Language": LANGS,
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"Windows"',
        },
        ignore_default_args=[
            "--enable-automation",
        ],
        handle_sigint=False,
        handle_sigterm=False,
        handle_sighup=False,
    )
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
    start_ts = time.monotonic()
    try:
        if not external_pw:
            playwright = await async_playwright().start()
        assert playwright is not None
        for attempt in range(1, NAVIGATE_MAX_ATTEMPTS + 1):
            log.info("Glassdoor launch attempt %d/%d (headless=False, real Chrome channel=chrome, persistent profile)", attempt, NAVIGATE_MAX_ATTEMPTS)
            ctx: Optional[BrowserContext] = None
            handles: Optional[_BrowserHandles] = None
            page: Optional[Page] = None
            try:
                handles = await _launch_context(playwright)
                ctx = handles.ctx
                try:
                    await ctx.route("**/*", _route_stealth)
                except Exception:
                    pass
                try:
                    await ctx.add_init_script(_AUTH_INIT_JS)
                except Exception:
                    pass
                pages = ctx.pages
                page = pages[0] if pages else await ctx.new_page()
                try:
                    await page.goto(START_URL, wait_until="domcontentloaded", timeout=NAVIGATE_TIMEOUT_MS)
                except PlaywrightTimeoutError as e:
                    if attempt < NAVIGATE_MAX_ATTEMPTS:
                        log.warning("Glassdoor initial goto timeout (attempt %d): %s", attempt, e)
                        try:
                            if page:
                                await page.wait_for_timeout(int(_jitter(1500, 2500)))
                            await ctx.close()
                        except Exception:
                            pass
                        ctx = None
                        handles = None
                        continue
                    else:
                        last_err = f"nav timeout after {NAVIGATE_MAX_ATTEMPTS} attempts: {e}"
                        break
                except PlaywrightError as e:
                    if attempt < NAVIGATE_MAX_ATTEMPTS:
                        log.warning("Glassdoor nav error (attempt %d): %s", attempt, e)
                        try:
                            if page:
                                await page.wait_for_timeout(int(_jitter(1500, 2500)))
                            await ctx.close()
                        except Exception:
                            pass
                        ctx = None
                        handles = None
                        continue
                    else:
                        last_err = f"nav error after {NAVIGATE_MAX_ATTEMPTS} attempts: {e}"
                        break
                assert page is not None
                await page.wait_for_timeout(int(_jitter(2500, 4500)))
                try:
                    title_text = await page.title()
                except Exception:
                    title_text = ""
                log.info("loaded title=%s", title_text[:100])
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
                if bad_title:
                    err = f"bad glassdoor landing title={title_text!r}"
                    if attempt < NAVIGATE_MAX_ATTEMPTS:
                        log.warning("Glassdoor %s: retry %d/%d", err, attempt, NAVIGATE_MAX_ATTEMPTS)
                        try:
                            if page:
                                await page.wait_for_timeout(int(_jitter(2000, 3500)))
                            await ctx.close()
                        except Exception:
                            pass
                        ctx = None
                        handles = None
                        continue
                    else:
                        last_err = err
                        break
                try:
                    role_cnt = await page.evaluate("() => document.querySelectorAll('#searchBar-jobTitle').length")
                    loc_cnt = await page.evaluate("() => document.querySelectorAll('#searchBar-location').length")
                except Exception:
                    role_cnt = 0
                    loc_cnt = 0
                try:
                    role_cnt = int(role_cnt or 0)
                    loc_cnt = int(loc_cnt or 0)
                except Exception:
                    role_cnt = 0
                    loc_cnt = 0
                if role_cnt == 0 or loc_cnt == 0:
                    err = f"searchBar inputs missing (role={role_cnt} location={loc_cnt}) title={title_text!r}"
                    if attempt < NAVIGATE_MAX_ATTEMPTS:
                        log.warning("Glassdoor %s: retry %d/%d", err, attempt, NAVIGATE_MAX_ATTEMPTS)
                        try:
                            if page:
                                await page.wait_for_timeout(int(_jitter(2000, 3500)))
                            await ctx.close()
                        except Exception:
                            pass
                        ctx = None
                        handles = None
                        continue
                    else:
                        last_err = err
                        break
                pre_submit_url = ""
                pre_search_li_count = 0
                try:
                    pre_submit_url = str(page.url or "")
                    ul_sel_tmp = 'ul[aria-label="Jobs List"][class*="JobsList_jobsList__"]'
                    li_sel_tmp = ul_sel_tmp + " > li"
                    pre_search_li_count = int(await _count_li_count(page, li_sel_tmp) or 0)
                except Exception:
                    pre_submit_url = str(page.url or "")
                    pre_search_li_count = 0
                if pre_search_li_count > 0:
                    log.info(
                        "pre-search guard: %d <li> cards already visible on landing (recent/searches); will skip in first batch (url=%s)",
                        pre_search_li_count,
                        pre_submit_url[:140],
                    )
                try:
                    result_url = await _submit_search(page, query, location)
                    if result_url:
                        log.info("URL changed after submit (took 0ms): %s", result_url[:160])
                except Exception as e:
                    last_err = f"submit failed: {e}"
                    if attempt < NAVIGATE_MAX_ATTEMPTS:
                        log.warning("Glassdoor %s retry %d/%d", last_err, attempt, NAVIGATE_MAX_ATTEMPTS)
                        try:
                            if page:
                                await page.wait_for_timeout(int(_jitter(2000, 3500)))
                            await ctx.close()
                        except Exception:
                            pass
                        ctx = None
                        handles = None
                        continue
                    else:
                        break
                try:
                    confirmed_url = await _wait_for_search_url_change(
                        page,
                        pre_submit_url=pre_submit_url,
                        query=query,
                        location_filter=location,
                        timeout_ms=45_000,
                    )
                    log.info("post-search URL confirmed: %s", confirmed_url[:180])
                except Exception as e:
                    last_err = f"search URL guard failed: {e}"
                    if attempt < NAVIGATE_MAX_ATTEMPTS:
                        log.warning("Glassdoor %s retry %d/%d", last_err, attempt, NAVIGATE_MAX_ATTEMPTS)
                        try:
                            if page:
                                await page.wait_for_timeout(int(_jitter(2000, 3500)))
                            await ctx.close()
                        except Exception:
                            pass
                        ctx = None
                        handles = None
                        continue
                    else:
                        break
                rows = await _search_and_collect(
                    page=page,
                    query=query,
                    location_filter=location,
                    target_cap=target_cap,
                    pre_search_li_count=pre_search_li_count,
                )
                collected.extend(rows)
                break
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                log.warning("Glassdoor launch err (attempt %d): %s", attempt, last_err)
                try:
                    if page:
                        await page.wait_for_timeout(int(_jitter(1500, 2500)))
                except Exception:
                    pass
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
    _ = start_ts
    if not collected and last_err:
        log.warning("Glassdoor: no rows collected, last_err=%s", last_err)
    return collected
