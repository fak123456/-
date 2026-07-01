"""Image read/write; optional resize to target square."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image


def parse_size(size: str) -> tuple[int, int]:
    s = size.lower().replace(" ", "")
    if s == "native":
        raise ValueError("parse_size: use ensure_png_size for native")
    if "x" in s:
        w, h = s.split("x", 1)
        return int(w), int(h)
    return 1600, 1600


def is_image_path(path: Path) -> bool:
    return path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}


def list_ref_paths(refs_dir: Path) -> list[Path]:
    """List image files in refs directory (sorted by name, casefold)."""
    if not refs_dir.is_dir():
        return []
    return sorted(
        (p for p in refs_dir.iterdir() if p.is_file() and is_image_path(p)),
        key=lambda p: p.name.casefold(),
    )


def collect_reference_paths(product_dir: Path) -> list[Path]:
    """
    Prefer images in refs/; if refs empty or missing, use images in product root.

    Used for dry-run without moving files.
    """
    refs_dir = product_dir / "refs"
    in_refs = list_ref_paths(refs_dir)
    if in_refs:
        return in_refs
    return sorted(
        (p for p in product_dir.iterdir() if p.is_file() and is_image_path(p)),
        key=lambda p: p.name.casefold(),
    )


def read_image_bytes(path: Path) -> bytes:
    """Read file as raw bytes (original encoding)."""
    return path.read_bytes()


def read_image_pil(path: Path) -> Image.Image:
    """Open image with Pillow (RGB)."""
    with Image.open(path) as im:
        return im.convert("RGB")


def ensure_jpg_size(image_bytes: bytes, size: str) -> bytes:
    """If size is native, re-encode as JPG without resizing; else cover-resize to WxH."""
    if size.strip().lower() == "native":
        with Image.open(io.BytesIO(image_bytes)) as im:
            im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=95, subsampling=0, optimize=True)
            return buf.getvalue()
    w, h = parse_size(size)
    with Image.open(io.BytesIO(image_bytes)) as im:
        im = im.convert("RGB")
        im = _cover_resize(im, w, h)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=95, subsampling=0, optimize=True)
        return buf.getvalue()


def save_jpg(image_bytes: bytes, dest: Path, size: str) -> None:
    """Normalize to target size (or native) and write high-quality JPG."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    normalized = ensure_jpg_size(image_bytes, size)
    dest.write_bytes(normalized)


def save_png(image_bytes: bytes, dest: Path, size: str) -> None:
    """Backward-compatible alias; new output bytes are JPG."""
    save_jpg(image_bytes, dest, size)


def _cover_resize(im: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Scale to cover target box then center-crop."""
    tw, th = target_w, target_h
    src_w, src_h = im.size
    scale = max(tw / src_w, th / src_h)
    new_w = int(round(src_w * scale))
    new_h = int(round(src_h * scale))
    im = im.resize((new_w, new_h), Image.Resampling.LANCZOS)
    left = (new_w - tw) // 2
    top = (new_h - th) // 2
    return im.crop((left, top, left + tw, top + th))
