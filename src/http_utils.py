from __future__ import annotations

import asyncio
import json
import logging
import random
from pathlib import Path
from typing import Any, Final, Mapping, MutableMapping, Optional, Union
from urllib.parse import urlparse

import httpx

log: logging.Logger = logging.getLogger(__name__)

USER_AGENTS: Final[list[str]] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0",
]

MIN_DELAY: Final[float] = 0.8
MAX_DELAY: Final[float] = 2.4

REQUEST_TIMEOUT: Final[float] = 20.0
MAX_RETRIES: Final[int] = 3

SESSION_DIR: Final[Path] = Path(__file__).resolve().parent.parent / "session"

HttpxParamValue = Union[str, int, float, bool, None]
HttpxParams = Mapping[str, HttpxParamValue]


class MaxRetriesExceeded(Exception):
    pass


def jitter() -> float:
    return random.uniform(MIN_DELAY, MAX_DELAY)


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def build_headers(url: str, referer: Optional[str] = None) -> dict[str, str]:
    if referer is None:
        sec_fetch_site = "none"
    elif _origin(url) == _origin(referer):
        sec_fetch_site = "same-origin"
    else:
        sec_fetch_site = "cross-site"
    headers: dict[str, str] = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "application/json;q=0.8,image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": sec_fetch_site,
        "Sec-Fetch-User": "?1",
    }
    if referer is not None:
        headers["Referer"] = referer
    return headers


def _cookie_path(source: str) -> Path:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    return SESSION_DIR / f"cookies_{source}.json"


def load_cookies(source: str) -> dict[str, str]:
    path = _cookie_path(source)
    if not path.exists():
        return {}
    try:
        data: Mapping[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Failed to read cookie jar %s: %s — starting fresh", path, exc)
        return {}
    if not isinstance(data, dict):
        log.warning("Cookie jar %s was not an object — starting fresh", path)
        return {}
    return {k: str(v) for k, v in data.items() if isinstance(v, str)}


def save_cookies(source: str, cookies: Mapping[str, str]) -> None:
    path = _cookie_path(source)
    try:
        path.write_text(
            json.dumps(dict(cookies), indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("Could not write cookies to %s: %s", path, exc)


def sync_cookies_to_jar(
    source: str, jar: MutableMapping[str, str], new_cookies: Mapping[str, str]
) -> None:
    jar.update(new_cookies)
    save_cookies(source, jar)


async def fetch_with_retries(
    client: httpx.AsyncClient,
    url: str,
    source: str,
    *,
    referer: Optional[str] = None,
    params: Optional[HttpxParams] = None,
) -> httpx.Response:
    last_exc: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            headers = build_headers(url, referer=referer)
            response: httpx.Response = await client.get(
                url,
                headers=headers,
                params=params,
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code == 200:
                return response
            if 400 <= response.status_code < 500 and response.status_code not in {408, 429}:
                log.warning(
                    "GET %s returned %s — non-retriable client error (attempt %d/%d)",
                    url,
                    response.status_code,
                    attempt,
                    MAX_RETRIES,
                )
                return response
            log.warning(
                "GET %s returned %s (attempt %d/%d) — retrying after jitter",
                url,
                response.status_code,
                attempt,
                MAX_RETRIES,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            last_exc = exc
            log.warning(
                "GET %s raised %s (attempt %d/%d) — retrying after jitter",
                url,
                type(exc).__name__,
                attempt,
                MAX_RETRIES,
            )
        if attempt < MAX_RETRIES:
            await asyncio.sleep(jitter())
    raise MaxRetriesExceeded(
        f"Failed GET {url} after {MAX_RETRIES} attempts: {last_exc or 'persistent non-2xx status'}"
    )
