"""Move product images from product folder root into refs/."""

from __future__ import annotations

from pathlib import Path

from src.image_io import is_image_path
from src.utils.logger import get_logger

logger = get_logger()

SKIP_DIRS = {"refs", "output", "src", "prompts", "__pycache__"}


def ensure_refs_folder(product_dir: Path) -> Path:
    """
    Create refs/ and move image files from product_dir root into refs/.

    Skips if destination already exists (same filename). Does not move dirs.
    """
    refs_dir = product_dir / "refs"
    refs_dir.mkdir(parents=True, exist_ok=True)

    for path in sorted(product_dir.iterdir(), key=lambda p: p.name.lower()):
        if path.is_dir():
            if path.name in SKIP_DIRS:
                continue
            logger.debug(f"Skipping subdirectory: {path.name}")
            continue
        if not path.is_file():
            continue
        if not is_image_path(path):
            continue

        dest = refs_dir / path.name
        if dest.exists():
            logger.debug(f"refs already has {dest.name}, skip move")
            continue
        path.rename(dest)
        logger.info(f"Moved {path.name} -> refs/")

    return refs_dir
