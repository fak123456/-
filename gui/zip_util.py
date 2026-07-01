"""Extract a product folder from a .zip into project root."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from src.image_io import is_image_path

TITLE_FILE = "商品标题.txt"


def _sanitize_folder_name(name: str) -> str:
    s = name.strip()
    for bad in '<>:"/\\|?*':
        s = s.replace(bad, "_")
    s = re.sub(r"\s+", " ", s).strip()
    return (s[:120] if s else "商品ZIP")


def looks_like_valid_product_dir(p: Path) -> bool:
    """Cheap check used by batch resume to decide if an existing target
    directory was probably already extracted from this same ZIP before.

    Heuristic: the directory must contain a ``refs/`` subfolder with at least
    one image. We deliberately do NOT require ``商品标题.txt`` here, because
    the batch caller will rewrite that file from the task-list row anyway.
    """
    if not p.is_dir():
        return False
    refs = p / "refs"
    if not refs.is_dir():
        return False
    try:
        for child in refs.iterdir():
            if child.is_file() and is_image_path(child):
                return True
    except OSError:
        return False
    return False


def extract_product_zip(
    zip_path: Path,
    dest_root: Path,
    title: str,
    folder_name_override: str | None = None,
    *,
    reuse_if_existing_valid: bool = False,
) -> Path:
    """Extract a product ZIP into ``dest_root`` and write the title file.

    Parameters
    ----------
    zip_path
        Path to the source ``.zip``.
    dest_root
        Project root that should receive a top-level product folder.
    title
        Required. Written to ``<product>/商品标题.txt``.
    folder_name_override
        Optional Chinese (or any) folder name for the resulting product
        directory under ``dest_root``. When given, it ALWAYS wins over any
        folder name found inside the ZIP. Use this from the batch UI to give
        each product a friendly local name regardless of how the supplier
        packaged the archive.
    reuse_if_existing_valid
        Batch-flow knob. When True and the target folder already exists AND
        already looks like a successfully-extracted product (has ``refs/``
        with images), this function silently returns that existing path
        instead of raising "目标已存在". The caller is then expected to use
        the resume logic (``compute_missing_slots``) to figure out which
        images still need to be generated. Defaults to False so the manual
        "上传 ZIP → 解压" button keeps its loud-and-safe behaviour.
    """
    title_clean = (title or "").strip()
    if not title_clean:
        raise ValueError("请先填写「商品标题」再解压 ZIP。")

    override = (folder_name_override or "").strip()
    override = _sanitize_folder_name(override) if override else ""

    dest_root = dest_root.resolve()
    dest_root.mkdir(parents=True, exist_ok=True)

    # Fast-path: if the caller is OK with reusing an already-extracted folder
    # AND we can predict exactly where this ZIP would land (override given,
    # OR the ZIP's filename stem can serve as the folder name), peek at that
    # path BEFORE bothering to crack open the archive.
    if reuse_if_existing_valid:
        guessed_name = override or _sanitize_folder_name(zip_path.stem)
        if guessed_name:
            guessed_target = dest_root / guessed_name
            if looks_like_valid_product_dir(guessed_target):
                # Refresh the title file in case the batch row has a newer one,
                # then hand the existing folder back unchanged.
                try:
                    (guessed_target / TITLE_FILE).write_text(
                        title_clean, encoding="utf-8"
                    )
                except OSError:
                    pass
                return guessed_target

    tmp = Path(tempfile.mkdtemp(prefix="product_zip_"))

    def _finalize(src: Path, fallback_name: str) -> Path:
        target_name = override or _sanitize_folder_name(fallback_name)
        target = dest_root / target_name
        if target.exists():
            if reuse_if_existing_valid and looks_like_valid_product_dir(target):
                # Existing valid folder wins; drop the freshly-extracted copy.
                try:
                    (target / TITLE_FILE).write_text(title_clean, encoding="utf-8")
                except OSError:
                    pass
                return target
            raise ValueError(f"目标已存在，请先删除或改名: {target}")
        shutil.move(str(src), str(target))
        (target / TITLE_FILE).write_text(title_clean, encoding="utf-8")
        return target

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmp)

        tmp_r = tmp.resolve()

        for title_p in tmp.rglob(TITLE_FILE):
            folder = title_p.parent.resolve()
            if folder == tmp_r:
                raise ValueError("ZIP 根目录缺少独立商品文件夹（请打成「文件夹/…」结构）")
            return _finalize(folder, folder.name)

        imgs = [p for p in tmp.rglob("*") if p.is_file() and is_image_path(p)]
        if not imgs:
            raise ValueError("ZIP 内未找到图片或 商品标题.txt")

        parents = [p.resolve().parent for p in imgs]
        try:
            common = Path(os.path.commonpath([str(p) for p in parents])).resolve()
        except ValueError:
            common = tmp_r

        if common == tmp_r:
            target_name = override or _sanitize_folder_name(zip_path.stem)
            target = dest_root / target_name
            if target.exists():
                if reuse_if_existing_valid and looks_like_valid_product_dir(target):
                    try:
                        (target / TITLE_FILE).write_text(title_clean, encoding="utf-8")
                    except OSError:
                        pass
                    return target
                raise ValueError(f"目标已存在，请先删除或改名: {target}")
            target.mkdir(parents=True)
            for p in tmp.iterdir():
                if p.is_file():
                    shutil.move(str(p), str(target / p.name))
            (target / TITLE_FILE).write_text(title_clean, encoding="utf-8")
            return target

        rel = common.relative_to(tmp_r)
        src_folder = tmp_r / rel.parts[0]
        if not src_folder.is_dir():
            src_folder = common
        return _finalize(src_folder, src_folder.name)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
