"""Per-product orchestrator: URL -> {ASIN}.zip on disk.

Steps for one URL:

1. Extract ASIN from URL.
2. If ``skip_existing`` and ``out_dir/{ASIN}.zip`` already exists, treat as success.
3. ``session.get_html`` -> CAPTCHA / fetch errors map to a non-ok status.
4. ``parse_html`` -> if no title or no images, skip with a meaningful status.
5. Stage in a temp dir: ``{ASIN}/商品标题.txt`` (UTF-8) and
   ``{ASIN}/refs/{NN}_{role}.jpg`` (best-effort: a single image failure
   logs a warning but does not abort the product).
6. ``shutil.make_archive`` -> ``out_dir/{ASIN}.zip``, then clean the temp dir.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from crawler.fetcher import AmazonSession, CaptchaError, FetchError
from crawler.parser import extract_asin, parse_html

logger = logging.getLogger(__name__)


_TITLE_FILE = "商品标题.txt"
_VALID_IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

# Status enum (use plain strings so they survive JSON / xlsx round-trips).
S_OK = "ok"
S_CAPTCHA = "captcha"
S_NO_ASIN = "no_asin"
S_NO_TITLE = "no_title"
S_NO_IMAGES = "no_images"
S_FETCH_ERROR = "fetch_error"


@dataclass
class ProductResult:
    url: str
    asin: str | None
    title: str
    zip_path: Path | None
    image_count: int
    status: str
    error: str = ""


def _ext_from_url(url: str) -> str:
    """Pick a sane image extension from the URL path; default ``.jpg``."""
    try:
        path = urlparse(url).path or ""
    except ValueError:
        return ".jpg"
    suffix = os.path.splitext(path)[1].lower()
    if suffix in _VALID_IMG_EXTS:
        return suffix
    return ".jpg"


_FNAME_BAD_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_role(role: str) -> str:
    return _FNAME_BAD_RE.sub("_", role)[:40] or "img"


def process_one(
    url: str,
    session: AmazonSession,
    out_dir: Path,
    *,
    max_images: int = 8,
    skip_existing: bool = False,
) -> ProductResult:
    url = (url or "").strip()
    asin = extract_asin(url)

    if not asin:
        return ProductResult(
            url=url,
            asin=None,
            title="",
            zip_path=None,
            image_count=0,
            status=S_NO_ASIN,
            error="No ASIN found in URL (need /dp/XXXXXXXXXX or /gp/product/XXXXXXXXXX)",
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    out_zip = out_dir / f"{asin}.zip"

    if skip_existing and out_zip.is_file() and out_zip.stat().st_size > 0:
        return ProductResult(
            url=url,
            asin=asin,
            title="",
            zip_path=out_zip.resolve(),
            image_count=0,
            status=S_OK,
            error="(skip-existing: zip already on disk)",
        )

    try:
        html_text = session.get_html(url)
    except CaptchaError as e:
        return ProductResult(url, asin, "", None, 0, S_CAPTCHA, str(e))
    except FetchError as e:
        return ProductResult(url, asin, "", None, 0, S_FETCH_ERROR, str(e))
    except Exception as e:
        return ProductResult(url, asin, "", None, 0, S_FETCH_ERROR, repr(e))

    parsed = parse_html(html_text, url=url)

    if parsed.is_captcha:
        return ProductResult(url, asin, "", None, 0, S_CAPTCHA, "CAPTCHA detected in body")
    if not parsed.title:
        return ProductResult(url, asin, "", None, 0, S_NO_TITLE, "productTitle not found")
    if not parsed.image_urls:
        return ProductResult(url, asin, parsed.title, None, 0, S_NO_IMAGES, "no image URLs extracted")

    targets = parsed.image_urls[: max(1, int(max_images))]

    tmp_root = Path(tempfile.mkdtemp(prefix="amzn_"))
    try:
        product_dir = tmp_root / asin
        refs_dir = product_dir / "refs"
        refs_dir.mkdir(parents=True, exist_ok=True)

        (product_dir / _TITLE_FILE).write_text(parsed.title, encoding="utf-8")

        saved = 0
        for i, img_url in enumerate(targets, start=1):
            role = "main" if i == 1 else f"alt_{i - 1:02d}"
            ext = _ext_from_url(img_url)
            fname = f"{i:02d}_{_safe_role(role)}{ext}"
            try:
                blob = session.download_image(img_url)
            except Exception as e:
                logger.warning("[%s] image %d/%d failed: %s | %s", asin, i, len(targets), e, img_url)
                continue
            (refs_dir / fname).write_bytes(blob)
            saved += 1

        if saved == 0:
            return ProductResult(
                url, asin, parsed.title, None, 0, S_NO_IMAGES,
                "all image downloads failed",
            )

        zip_base = out_dir / asin
        if out_zip.exists():
            try:
                out_zip.unlink()
            except OSError:
                pass
        shutil.make_archive(str(zip_base), "zip", root_dir=str(tmp_root), base_dir=asin)

        return ProductResult(
            url=url,
            asin=asin,
            title=parsed.title,
            zip_path=out_zip.resolve(),
            image_count=saved,
            status=S_OK,
            error="",
        )
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
