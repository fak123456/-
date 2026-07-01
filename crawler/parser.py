"""Pure HTML parser for Amazon product detail pages.

This module is **stateless and network-free**: feed it an HTML string and it
returns the structured product info (title + hi-res image URLs) plus a
CAPTCHA flag. All retry / fetching / IO concerns live elsewhere.

Image-URL extraction has three priority tiers, each falling through if empty:

1. The ``data-a-dynamic-image`` attribute on ``img#landingImage`` is a JSON
   dict mapping every available size of the *main* image to its
   ``[width, height]``; we pick the URL with the largest area.
2. The product detail page also embeds a ``colorImages.initial`` JSON array
   inside one of the inline ``<script>`` blocks. Each entry has ``hiRes`` /
   ``large`` / ``thumb`` keys covering the full gallery (main + alts).
3. Last resort: scrape ``#altImages li img/@src`` thumbnails and rewrite
   the size token (``_AC_US40_`` / ``_AC_SR\\d+,\\d+_``) to ``_SL1500_``
   to upgrade them to hi-res. This works because Amazon's image CDN
   accepts arbitrary size tokens.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Iterable

from lxml import html as lxml_html


_ASIN_RE = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})")
_COLOR_IMAGES_RE = re.compile(
    r"['\"]colorImages['\"]\s*:\s*(\{.*?\})\s*,\s*['\"]colorToAsin['\"]",
    re.DOTALL,
)
_FALLBACK_COLOR_IMAGES_RE = re.compile(
    r"['\"]colorImages['\"]\s*:\s*(\{.*?\})\s*[,}]",
    re.DOTALL,
)
_SIZE_TOKEN_RE = re.compile(
    r"_(?:AC_US\d+|AC_SR\d+,\d+|AC_SX\d+|AC_SY\d+|AC_SL\d+|"
    r"SL\d+|SS\d+|SX\d+|SY\d+|SR\d+,\d+|UL\d+|UF\d+,\d+)_"
)
# Token we substitute in to ask the CDN for the max source size. Amazon's
# image CDN serves min(source, requested), so a big number is "give me the
# source"; smaller sources just come back at their native dimensions.
_MAX_SIZE_TOKEN = "_SL2000_"


def _upscale_to_source(url: str) -> str:
    """Rewrite Amazon CDN URL to request the largest available source size.

    The CDN ignores any request larger than the source. So this is always
    safe: real-source-1601 stays 1601, real-source-500 stays 500. The only
    effect is *never accidentally undersizing* when the URL was harvested
    from a JSON variant list that registers only smaller crops.
    """
    if not url or "media-amazon.com" not in url:
        return url
    if _SIZE_TOKEN_RE.search(url):
        return _SIZE_TOKEN_RE.sub(_MAX_SIZE_TOKEN, url)
    # No size token at all means the bare URL, which already serves source.
    return url
_CAPTCHA_TEXT_NEEDLES = (
    "captchacharacters",
    "validateCaptcha",
    "Type the characters you see in this image",
    "Geben Sie die Zeichen unten ein",
    "Enter the characters you see below",
    "Sorry, we just need to make sure you're not a robot",
)


@dataclass
class ParsedProduct:
    asin: str | None
    title: str
    image_urls: list[str] = field(default_factory=list)
    is_captcha: bool = False
    raw_excerpt: str = ""


def extract_asin(url: str) -> str | None:
    """Pull a 10-char Amazon ASIN out of a /dp/ or /gp/product/ URL."""
    if not url:
        return None
    m = _ASIN_RE.search(url)
    return m.group(1) if m else None


def detect_captcha(html_text: str, url: str = "") -> bool:
    """True if the response is an anti-bot CAPTCHA / robot-check page."""
    if "validateCaptcha" in (url or ""):
        return True
    if not html_text:
        return False
    head = html_text[:8000]
    return any(needle in head for needle in _CAPTCHA_TEXT_NEEDLES)


def _clean_text(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip()


def _largest_in_dynamic_image(blob: str) -> str | None:
    """data-a-dynamic-image is a JSON dict {url: [w, h], ...}."""
    try:
        data = json.loads(blob)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or not data:
        return None
    best_url, best_area = None, -1
    for url, dims in data.items():
        if not isinstance(dims, (list, tuple)) or len(dims) < 2:
            continue
        try:
            area = int(dims[0]) * int(dims[1])
        except (TypeError, ValueError):
            continue
        if area > best_area:
            best_area = area
            best_url = url
    return best_url


def _color_images_urls(html_text: str) -> list[str]:
    """Pull ``colorImages.initial`` from any inline ``<script>`` and return
    its hi-res (or large) URLs in order. Returns ``[]`` if no JSON found."""
    candidates = list(_COLOR_IMAGES_RE.finditer(html_text))
    if not candidates:
        candidates = list(_FALLBACK_COLOR_IMAGES_RE.finditer(html_text))
    for m in candidates:
        raw = m.group(1)
        # The JSON we extracted is a fragment of a larger JS object; balance
        # braces by counting until we get a parseable string.
        for end in range(len(raw), 0, -1):
            try:
                obj = json.loads(raw[:end])
                break
            except ValueError:
                continue
        else:
            continue
        if not isinstance(obj, dict):
            continue
        initial = obj.get("initial")
        if not isinstance(initial, list):
            continue
        urls: list[str] = []
        for item in initial:
            if not isinstance(item, dict):
                continue
            for k in ("hiRes", "large", "mainUrl"):
                u = item.get(k)
                if isinstance(u, str) and u.startswith(("http://", "https://")):
                    urls.append(u)
                    break
        if urls:
            return urls
    return []


def _alt_image_urls(tree) -> list[str]:
    """``#altImages li img/@src`` thumbnails. Sizes are normalized later
    by ``_upscale_to_source`` in ``parse_html``."""
    out: list[str] = []
    seen: set[str] = set()
    for src in tree.xpath('//div[@id="altImages"]//li//img/@src'):
        if not isinstance(src, str) or not src.startswith(("http://", "https://")):
            continue
        if "play-button" in src or "video" in src.lower():
            continue
        if src not in seen:
            seen.add(src)
            out.append(src)
    return out


def _dedupe(urls: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if not isinstance(u, str) or not u:
            continue
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def parse_html(html_text: str, *, url: str = "") -> ParsedProduct:
    """Parse a saved Amazon product page; never raises on parse errors."""
    asin = extract_asin(url)
    excerpt = (html_text or "")[:500]

    if detect_captcha(html_text, url):
        return ParsedProduct(asin=asin, title="", image_urls=[], is_captcha=True, raw_excerpt=excerpt)

    if not html_text:
        return ParsedProduct(asin=asin, title="", image_urls=[], is_captcha=False, raw_excerpt=excerpt)

    try:
        tree = lxml_html.fromstring(html_text)
    except (ValueError, lxml_html.etree.ParserError):
        return ParsedProduct(asin=asin, title="", image_urls=[], is_captcha=False, raw_excerpt=excerpt)

    title_nodes = tree.xpath('//*[@id="productTitle"]//text()')
    title = _clean_text("".join(title_nodes))

    main_url: str | None = None
    landing = tree.xpath('//img[@id="landingImage"]/@data-a-dynamic-image')
    if landing:
        main_url = _largest_in_dynamic_image(landing[0])
    if not main_url:
        landing_src = tree.xpath('//img[@id="landingImage"]/@src')
        if landing_src and landing_src[0].startswith(("http://", "https://")):
            main_url = landing_src[0]

    gallery = _color_images_urls(html_text)
    if not gallery:
        gallery = _alt_image_urls(tree)

    ordered_raw: list[str] = []
    if main_url:
        ordered_raw.append(main_url)
    for u in gallery:
        ordered_raw.append(u)

    # Normalize every URL to "ask for the source size" before deduping.
    # Without this step we sometimes pick the smaller variant amazon
    # registered in data-a-dynamic-image even though the source on its
    # CDN is much bigger (seen e.g. on B0GJD5HCTW: 679x654 picked, 1601
    # actually available).
    ordered = _dedupe(_upscale_to_source(u) for u in ordered_raw)

    return ParsedProduct(
        asin=asin,
        title=title,
        image_urls=ordered,
        is_captcha=False,
        raw_excerpt=excerpt,
    )
