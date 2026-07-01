"""HTTP layer: a single ``requests.Session`` with browser-like headers,
random pre-delays between page fetches, and exponential-backoff retries
that recognise Amazon's CAPTCHA wall as a retryable failure.

Image downloads (``m.media-amazon.com`` CDN) have no anti-bot, so we keep
that path lean: no pre-delay, no retry.
"""

from __future__ import annotations

import random
import time
from typing import Any

import requests

from crawler.parser import detect_captcha


class FetchError(Exception):
    """Generic page-fetch failure after all retries are exhausted."""


class CaptchaError(FetchError):
    """Amazon redirected us to (or returned) a CAPTCHA / robot-check page."""


_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": _DEFAULT_UA,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Ch-Ua": '"Google Chrome";v="124", "Chromium";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Connection": "keep-alive",
}

# Attempt N waits this many seconds before retrying. Same length as max_retries.
_BACKOFF_SCHEDULE = (2.0, 5.0, 10.0)


class AmazonSession:
    def __init__(
        self,
        *,
        delay_min: float = 2.0,
        delay_max: float = 5.0,
        timeout: float = 20.0,
        max_retries: int = 3,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        if delay_min < 0 or delay_max < delay_min:
            raise ValueError("delay_min/delay_max must satisfy 0 <= min <= max")
        self._delay_min = float(delay_min)
        self._delay_max = float(delay_max)
        self._timeout = float(timeout)
        self._max_retries = max(1, int(max_retries))

        self._session = requests.Session()
        self._session.headers.update(_DEFAULT_HEADERS)
        if extra_headers:
            self._session.headers.update(extra_headers)

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:
            pass

    def __enter__(self) -> "AmazonSession":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _sleep_pre_request(self) -> None:
        if self._delay_max > 0:
            time.sleep(random.uniform(self._delay_min, self._delay_max))

    @staticmethod
    def _backoff(attempt: int) -> float:
        if attempt - 1 < len(_BACKOFF_SCHEDULE):
            return _BACKOFF_SCHEDULE[attempt - 1]
        return _BACKOFF_SCHEDULE[-1]

    def get_html(self, url: str) -> str:
        """Fetch a product page as text. Retries on transient errors and
        CAPTCHA hits with exponential backoff. Raises ``CaptchaError`` if
        the final response is still a CAPTCHA, or ``FetchError`` for any
        other terminal failure."""
        last_exc: Exception | None = None
        last_was_captcha = False
        for attempt in range(1, self._max_retries + 1):
            self._sleep_pre_request()
            try:
                resp = self._session.get(url, timeout=self._timeout, allow_redirects=True)
            except requests.RequestException as e:
                last_exc = e
                last_was_captcha = False
            else:
                final_url = str(resp.url)
                if resp.status_code >= 500:
                    last_exc = FetchError(f"HTTP {resp.status_code} from {final_url}")
                    last_was_captcha = False
                elif resp.status_code in (301, 302, 303, 307, 308):
                    last_exc = FetchError(f"Unexpected redirect {resp.status_code} -> {final_url}")
                    last_was_captcha = False
                elif resp.status_code >= 400 and resp.status_code != 429:
                    raise FetchError(f"HTTP {resp.status_code} from {final_url}")
                else:
                    text = resp.text or ""
                    if detect_captcha(text, final_url):
                        last_exc = CaptchaError(f"CAPTCHA wall at {final_url}")
                        last_was_captcha = True
                    else:
                        return text

            if attempt < self._max_retries:
                time.sleep(self._backoff(attempt))

        if last_was_captcha:
            raise CaptchaError(str(last_exc) if last_exc else "CAPTCHA wall (final)")
        raise FetchError(str(last_exc) if last_exc else "fetch failed (final)")

    def download_image(self, url: str) -> bytes:
        """Download a single image from Amazon's CDN. 30s timeout, no retry,
        no pre-delay (CDN has no anti-bot). Returns the raw bytes; callers
        decide the file extension from the URL or content-type."""
        resp = self._session.get(url, timeout=30.0, allow_redirects=True)
        resp.raise_for_status()
        if not resp.content:
            raise FetchError(f"empty image body: {url}")
        return resp.content
