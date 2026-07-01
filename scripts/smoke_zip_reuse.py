"""Smoke for ``extract_product_zip(reuse_if_existing_valid=True)``.

Reproduces the user-facing failure mode:
    解压失败: 目标已存在，请先删除或改名: <path>
…and confirms that with the new ``reuse_if_existing_valid=True`` flag the
batch flow silently reuses an already-extracted product directory instead
of bombing the row.

Coverage:
1. First call extracts normally.
2. Second call without the flag → raises (legacy single-product behaviour).
3. Second call with the flag → returns the same path, doesn't re-extract.
4. Title file gets refreshed even when reusing.
5. Existing folder that *isn't* a valid product (no refs/) → still raises
   with the flag (we don't blindly trust just any same-named folder).
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gui.zip_util import (  # noqa: E402
    extract_product_zip,
    looks_like_valid_product_dir,
)

PNG_HEADER = b"\x89PNG\r\n\x1a\n"


def _build_product_zip(zip_path: Path, asin: str = "B0GGRC537N") -> None:
    """Write a tiny valid product zip to ``zip_path``.

    Layout inside the ZIP:
        <asin>/
            refs/
                ref_01.jpg
                ref_02.jpg
            商品标题.txt   (will be overwritten by extract_product_zip anyway)
    """
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{asin}/refs/ref_01.jpg", PNG_HEADER + b"\x00" * 1024)
        zf.writestr(f"{asin}/refs/ref_02.jpg", PNG_HEADER + b"\x00" * 1024)
        zf.writestr(f"{asin}/商品标题.txt", "old title from supplier")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="zip_reuse_smoke_"))
    print(f"[smoke] tmp = {tmp}")
    try:
        zip_path = tmp / "B0GGRC537N.zip"
        dest_root = tmp / "批量测试"
        _build_product_zip(zip_path, asin="B0GGRC537N")
        title = "Beckenbodentrainingsgerät | USB-Ladegerät | 续跑测试标题"

        # ---- 1. First extract: normal happy path -----------------------
        out1 = extract_product_zip(zip_path, dest_root, title)
        print(f"[smoke] first extract -> {out1}")
        assert out1.is_dir(), "expected extract to return an existing dir"
        assert (out1 / "refs").is_dir(), "missing refs/"
        title_after = (out1 / "商品标题.txt").read_text(encoding="utf-8").strip()
        assert title_after == title.strip(), (
            f"title not written: got {title_after!r}, expected {title.strip()!r}"
        )
        # Capture the ref_01.jpg mtime so we can prove no re-extract happened.
        ref01 = out1 / "refs" / "ref_01.jpg"
        seeded_ref_mtime = ref01.stat().st_mtime_ns

        # ---- 2. Second extract WITHOUT the flag: legacy raise ----------
        try:
            extract_product_zip(zip_path, dest_root, title)
        except ValueError as e:
            assert "目标已存在" in str(e), f"unexpected error: {e}"
            print(f"[smoke] legacy mode correctly refused: {e}")
        else:
            raise AssertionError("legacy mode should have raised")

        # ---- 3. Second extract WITH the flag: silent reuse -------------
        out2 = extract_product_zip(
            zip_path, dest_root, title + " v2", reuse_if_existing_valid=True
        )
        print(f"[smoke] reuse mode returned -> {out2}")
        assert out2 == out1, f"reuse should return same path, got {out2}"
        new_ref_mtime = ref01.stat().st_mtime_ns
        assert new_ref_mtime == seeded_ref_mtime, (
            f"ref_01.jpg mtime changed (re-extracted!) "
            f"seeded={seeded_ref_mtime}, now={new_ref_mtime}"
        )
        print("[smoke] reuse did NOT rewrite refs/ref_01.jpg OK")

        # ---- 4. Title file gets refreshed even on reuse ----------------
        title_now = (out2 / "商品标题.txt").read_text(encoding="utf-8").strip()
        assert title_now == (title + " v2").strip(), (
            f"title should refresh on reuse, got {title_now!r}"
        )
        print("[smoke] reuse refreshed 商品标题.txt OK")

        # ---- 5. Existing folder without refs/ → still raises -----------
        # Build a same-named target directory that's NOT a valid product
        # (just an empty folder). Then make a *different* zip.
        bad_root = tmp / "bad_dest"
        bad_root.mkdir()
        bad_target = bad_root / "B0GGRC537N"
        bad_target.mkdir()
        (bad_target / "stray.txt").write_text("not a product", encoding="utf-8")
        assert not looks_like_valid_product_dir(bad_target), (
            "smoke setup: bad_target should fail validity check"
        )
        try:
            extract_product_zip(
                zip_path, bad_root, title, reuse_if_existing_valid=True
            )
        except ValueError as e:
            assert "目标已存在" in str(e), f"unexpected error: {e}"
            print(f"[smoke] reuse refused empty-but-existing folder: {e}")
        else:
            raise AssertionError(
                "reuse mode should still raise for non-product folders"
            )

        # ---- 6. looks_like_valid_product_dir on a real product ---------
        assert looks_like_valid_product_dir(out1), "real product should be valid"

        print("[smoke] ALL ASSERTIONS PASSED")
        return 0
    finally:
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
