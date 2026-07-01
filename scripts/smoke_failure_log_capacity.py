"""Stress check: lots of failure rows survive the xlsx round-trip.

Builds 60 synthetic failure rows (well past the dataframe's initial visible
height of 12), saves to xlsx, reloads, asserts:

1. Count is preserved (no truncation at 5/7/anything else).
2. Every (product_path, slot) key round-trips byte-for-byte.
3. Adding a new failure for an *already-recorded* (product, slot) pair
   updates the existing row instead of duplicating it (dedup invariant).

The Gradio dataframe ``row_count=(N, "dynamic")`` only sets the *initial*
visible height — there is no upper cap on stored rows. This script proves
that the store layer (xlsx + merge logic) doesn't lose data either.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gui.failure_log import (  # noqa: E402
    RETRY_PENDING,
    FailureRow,
    derive_failure_log_path,
    init_template_if_missing,
    load_failure_log,
    merge_failures_from_meta,
    save_failure_log,
)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="failure_log_capacity_"))
    print(f"[smoke] tmp = {tmp}")
    try:
        tasklist_path = tmp / "批量任务_capacity.xlsx"
        flp = derive_failure_log_path(tasklist_path)
        init_template_if_missing(flp)

        # ---- Build 60 distinct failure rows --------------------------------
        # 10 fake products x 6 slot kinds each.
        slot_kinds = [
            ("main", 1), ("scene", 1), ("scene", 2),
            ("size", 1), ("detail", 1), ("angle", 1),
        ]
        rows: list[FailureRow] = []
        for pi in range(10):
            pname = f"PROD_{pi:02d}"
            for tname, idx in slot_kinds:
                rows.append(FailureRow(
                    product_dir_name=pname,
                    slot=f"{tname}_{idx:02d}",
                    error=f"Xais HTTP 500 (synthetic #{pi})",
                    failed_at=f"2026-05-16 19:{pi:02d}:00",
                    retry_status=RETRY_PENDING,
                    retried_at="",
                    product_path=str(tmp / "projects" / pname),
                ))
        assert len(rows) == 60, f"expected 60 seed rows, built {len(rows)}"
        save_failure_log(flp, rows)

        # ---- Round-trip ---------------------------------------------------
        rows2 = load_failure_log(flp)
        assert len(rows2) == 60, f"round-trip lost rows: {len(rows2)}"
        keys_before = {(r.product_path, r.slot) for r in rows}
        keys_after = {(r.product_path, r.slot) for r in rows2}
        assert keys_before == keys_after, "row keys diverged after round-trip"
        print(f"[smoke] saved + loaded 60 rows OK; keys match")

        # ---- Re-merge same data: should NOT grow ----------------------------
        # Simulate "user clicked 开始, same products failed again on the same
        # slots": merge_failures_from_meta should refresh the existing rows
        # rather than appending duplicates.
        for pi in range(10):
            pdir = tmp / "projects" / f"PROD_{pi:02d}"
            pdir.mkdir(parents=True, exist_ok=True)
            (pdir / "output").mkdir(exist_ok=True)
            fake_meta = {
                "images": [
                    {
                        "type": tname,
                        "index": idx,
                        "status": "error",
                        "error": f"Re-run failure #{pi}",
                    }
                    for tname, idx in slot_kinds
                ]
            }
            merge_failures_from_meta(rows2, product_dir=pdir, meta=fake_meta)
        save_failure_log(flp, rows2)
        rows3 = load_failure_log(flp)
        assert len(rows3) == 60, (
            f"merge created duplicates: had 60, now {len(rows3)}"
        )
        # All errors should now show "Re-run failure" text from the merge.
        n_updated = sum(1 for r in rows3 if r.error.startswith("Re-run failure"))
        assert n_updated == 60, (
            f"expected all 60 rows refreshed, only {n_updated} got new errors"
        )
        print(f"[smoke] re-merge same 60 keys → still 60 rows, all refreshed OK")

        # ---- Add 8 brand-new failures: list grows to 68 --------------------
        for tname, idx in [("multi", 1), ("material", 1)]:
            for pi in range(4):
                pdir = tmp / "projects" / f"PROD_{pi:02d}"
                fake_meta = {"images": [{
                    "type": tname,
                    "index": idx,
                    "status": "error",
                    "error": f"new slot type failure",
                }]}
                merge_failures_from_meta(rows3, product_dir=pdir, meta=fake_meta)
        save_failure_log(flp, rows3)
        rows4 = load_failure_log(flp)
        assert len(rows4) == 68, f"expected 68 rows after additions, got {len(rows4)}"
        print(f"[smoke] added 8 brand-new (product, slot) pairs → 68 rows OK")

        print("[smoke] ALL ASSERTIONS PASSED — dataframe will display all rows")
        return 0
    finally:
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
