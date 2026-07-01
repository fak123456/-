"""Targeted smoke for the zip-stem task-list matching fix.

Before the fix, syncing per-product status against a task list with
multiple zip rows would match EVERY zip row, because the code did
``cand.append(product_dir.resolve())`` unconditionally for any zip-suffix
row. The failure log + task list could then have wrong rows updated.

After the fix, a zip row only matches if ``Path(row.path).stem ==
product_dir.name`` (case-insensitive on Windows).

This smoke replicates the matching loop from
``_sync_batch_xlsx_after_meta`` and ``_run_retry_failures_inner`` and
verifies the new behaviour against a 4-row task list.

Run from repo root:

    python scripts/smoke_zip_stem_match.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def replicate_matcher(task_paths: list[str], product_dir: Path) -> list[int]:
    """Carbon-copy of the post-fix matching loop from generate.py."""
    pdir_resolved = str(product_dir.resolve()).lower()
    pdir_name_lower = product_dir.name.lower()
    matched: list[int] = []
    for i, raw in enumerate(task_paths):
        if not raw.strip():
            continue
        p = Path(raw).expanduser()
        try:
            if str(p.resolve()).lower() == pdir_resolved:
                matched.append(i)
                continue
        except OSError:
            pass
        if p.suffix.lower() == ".zip" and p.stem.lower() == pdir_name_lower:
            matched.append(i)
    return matched


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="zip_stem_smoke_"))
    print(f"[smoke] tmp = {tmp}")
    try:
        # Build folder for product B0GGRC537N (the user's actual ASIN).
        out_root = tmp / "批量测试"
        out_root.mkdir()
        pdir = out_root / "B0GGRC537N"
        pdir.mkdir()

        # Plus an unrelated folder D0XYZ9999 for an independent product.
        other_pdir = out_root / "D0XYZ9999"
        other_pdir.mkdir()

        # Task list with 4 rows:
        # 0: zip pointing at the right ASIN — should match
        # 1: zip pointing at a DIFFERENT ASIN — must NOT match
        # 2: folder pointing at the right product — should match
        # 3: folder pointing at the OTHER product — must NOT match
        task_paths = [
            str(tmp / "downloads" / "B0GGRC537N.zip"),
            str(tmp / "downloads" / "D0XYZ9999.zip"),
            str(pdir),
            str(other_pdir),
        ]

        # ---- 1. Match against B0GGRC537N folder --------------------
        matched = replicate_matcher(task_paths, pdir)
        assert matched == [0, 2], (
            f"matching B0GGRC537N: expected rows [0, 2], got {matched}"
        )
        print(f"[smoke] B0GGRC537N matched rows {matched} (expected [0, 2]) OK")

        # ---- 2. Match against D0XYZ9999 folder ---------------------
        matched2 = replicate_matcher(task_paths, other_pdir)
        assert matched2 == [1, 3], (
            f"matching D0XYZ9999: expected rows [1, 3], got {matched2}"
        )
        print(f"[smoke] D0XYZ9999 matched rows {matched2} (expected [1, 3]) OK")

        # ---- 3. Case-insensitive match on Windows ------------------
        upper_paths = [
            str(tmp / "DOWNLOADS" / "b0ggrc537n.ZIP"),  # mixed case zip
            str(pdir),
        ]
        matched3 = replicate_matcher(upper_paths, pdir)
        assert matched3 == [0, 1], (
            f"case-insensitive match: expected [0, 1], got {matched3}"
        )
        print(f"[smoke] case-insensitive zip stem matched rows {matched3} OK")

        # ---- 4. No spurious match against an unrelated stem --------
        misc_paths = [
            str(tmp / "downloads" / "totally_different.zip"),
            str(tmp / "downloads" / "B0GGRC537N_old_v2.zip"),  # similar but not equal stem
        ]
        matched4 = replicate_matcher(misc_paths, pdir)
        assert matched4 == [], (
            f"unrelated stems should not match: got {matched4}"
        )
        print(f"[smoke] unrelated zips correctly didn't match {matched4} OK")

        # ---- 5. Ghost test: empty list ------------------------------
        assert replicate_matcher([], pdir) == []
        # Empty path → skipped
        assert replicate_matcher(["", "  "], pdir) == []

        print("[smoke] ALL ASSERTIONS PASSED")
        return 0
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
