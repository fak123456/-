"""End-to-end smoke for the new "续跑被终止行" / "已终止" flow.

Exercises ``compute_missing_slots`` against a fake product whose
``output/meta.json`` says "I was cancelled mid-flight; I have main_01,
scene_01 OK and the rest never ran". We assert:

1. The missing-slot computation matches what's actually missing (no false
   positives or false negatives).
2. ``run_generation(regen_str=...)`` with that missing list:
   - reuses the existing OK slots (no rewrite of files we already had)
   - produces all expected slots in the merged meta.json
   - flips ``meta["cancelled"]`` away (the new run wasn't cancelled)
3. After the resume run, ``compute_missing_slots`` returns empty (nothing
   left to do) and ``was_cancelled`` is False.
4. ``TaskRow.is_actionable`` correctly handles ``STATUS_CANCELLED`` with
   ``retry_cancelled`` toggled.

Run from repo root:

    python scripts/smoke_resume_cancelled.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Pin placeholder provider before any GUI / runner import.
os.environ["IMAGE_PROVIDER"] = "placeholder"
# Use a throwaway instance label so we don't pick up the user's real
# ~/.amazon_img_gui/config.json (which may have image_provider=xais).
os.environ.setdefault("AMAZON_IMG_GUI_INSTANCE", "smoketest_isolated")
os.environ.setdefault("LOGURU_LEVEL", "WARNING")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gui.batch_tasklist import (  # noqa: E402
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_PENDING,
    TaskRow,
)
from gui.runner import compute_missing_slots, effective_settings, run_generation  # noqa: E402

PNG_HEADER = b"\x89PNG\r\n\x1a\n"


def _make_fake_product(root: Path, name: str, title: str) -> Path:
    pdir = root / name
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "商品标题.txt").write_text(title, encoding="utf-8")
    refs = pdir / "refs"
    refs.mkdir(exist_ok=True)
    (refs / "ref_01.png").write_bytes(PNG_HEADER + b"\x00" * 100)
    (pdir / "output").mkdir(exist_ok=True)
    return pdir


def _seed_partial_cancelled_meta(product_dir: Path, ok_slots: list[tuple[str, int]]) -> None:
    """Write meta.json + the matching PNGs that simulate a 'I was cancelled' state."""
    images = []
    out = product_dir / "output"
    for type_name, idx in ok_slots:
        fname = f"{type_name}_{idx:02d}.png"
        png_path = out / fname
        png_path.write_bytes(PNG_HEADER + b"\x00" * 1024)
        images.append({
            "type": type_name,
            "index": idx,
            "status": "ok",
            "output_path": str(png_path),
        })
    meta = {"images": images, "counts_total": len(ok_slots), "cancelled": True}
    (out / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _slots(meta: dict, status: str) -> set[str]:
    out = set()
    for im in meta.get("images") or []:
        if not isinstance(im, dict):
            continue
        if im.get("status") != status:
            continue
        t = im.get("type")
        try:
            i = int(im.get("index") or 0)
        except (TypeError, ValueError):
            continue
        if t and i:
            out.add(f"{t}_{i:02d}")
    return out


def main() -> int:
    tmp_root = Path(tempfile.mkdtemp(prefix="resume_cancelled_smoke_"))
    print(f"[smoke] tmp = {tmp_root}")
    try:
        # ---- 1. Build product with seeded "cancelled mid-flight" meta ----
        proj = tmp_root / "projects"
        proj.mkdir()
        pa = _make_fake_product(proj, "Prod_A", "Test product 标题")
        # Default counts (single-product yaml, env, etc.) → 8 expected:
        # main=1 scene=2 multi=1 size=1 detail=1 angle=1 material=1.
        # Seed only main_01 + scene_01 as "OK so far". The remaining 6 slots
        # should be detected as missing.
        _seed_partial_cancelled_meta(pa, [("main", 1), ("scene", 1)])
        seeded_main_mtime = (pa / "output" / "main_01.png").stat().st_mtime_ns

        settings = effective_settings()
        assert settings.image_provider == "placeholder", (
            f"expected placeholder provider, got {settings.image_provider}"
        )

        # ---- 2. compute_missing_slots before resume ----------------------
        missing, n_ok, n_expected, was_cancelled = compute_missing_slots(
            pa, settings, counts_str=None
        )
        print(f"[smoke] before resume: missing={missing}, ok={n_ok}/{n_expected}, cancelled={was_cancelled}")
        assert was_cancelled, "expected meta.cancelled=True"
        assert n_ok == 2, f"expected 2 ok slots, got {n_ok}"
        assert n_expected == 8, f"expected 8 total slots, got {n_expected}"
        assert sorted(missing) == sorted([
            "scene_02", "multi_01", "size_01", "detail_01", "angle_01", "material_01"
        ]), f"unexpected missing list: {missing}"

        # ---- 3. Run regen on the missing slots ---------------------------
        regen_str = ",".join(missing)
        print(f"[smoke] resuming with regen_str={regen_str!r}")
        meta_after = run_generation(
            project_root=Path("."),
            product_dir=pa,
            counts_str=None,
            regen_str=regen_str,
            user_note=None,
            settings=settings,
            progress_callback=None,
            cancel_event=None,
        )

        # ---- 4. Verify merged meta is now complete -----------------------
        ok_slots_after = _slots(meta_after, "ok")
        err_slots_after = _slots(meta_after, "error")
        print(f"[smoke] after resume: ok={sorted(ok_slots_after)}")
        print(f"[smoke] after resume: err={sorted(err_slots_after)}")
        expected_full = {
            "main_01", "scene_01", "scene_02", "multi_01",
            "size_01", "detail_01", "angle_01", "material_01",
        }
        assert ok_slots_after == expected_full, (
            f"meta missing slots after resume: want {sorted(expected_full)}, "
            f"got {sorted(ok_slots_after)}"
        )
        assert not err_slots_after, f"unexpected errors after resume: {err_slots_after}"
        # cancelled flag should NOT be set on the new run.
        assert not meta_after.get("cancelled"), "expected cancelled=False after resume"

        # ---- 5. The pre-existing main_01.png must NOT have been rewritten -
        new_main_mtime = (pa / "output" / "main_01.png").stat().st_mtime_ns
        assert new_main_mtime == seeded_main_mtime, (
            f"main_01.png was rewritten by resume! seeded mtime={seeded_main_mtime}, "
            f"now={new_main_mtime}"
        )
        print("[smoke] kept main_01.png intact (mtime unchanged) OK")

        # ---- 6. compute_missing_slots after resume must be empty ---------
        missing2, n_ok2, n_expected2, was_cancelled2 = compute_missing_slots(
            pa, settings, counts_str=None
        )
        print(f"[smoke] after resume: missing={missing2}, ok={n_ok2}/{n_expected2}, cancelled={was_cancelled2}")
        assert missing2 == [], f"expected nothing missing, got {missing2}"
        assert n_ok2 == 8 and n_expected2 == 8
        assert not was_cancelled2

        # ---- 7. TaskRow.is_actionable behaviour on STATUS_CANCELLED ------
        cancel_row = TaskRow(path="x", title="t", status=STATUS_CANCELLED)
        # default: retry_cancelled=True, treat as actionable
        assert cancel_row.is_actionable(retry_failed=False, retry_partial=False)
        # opt-out: retry_cancelled=False, treat as not actionable
        assert not cancel_row.is_actionable(
            retry_failed=False, retry_partial=False, retry_cancelled=False
        )
        # done row stays done regardless
        done_row = TaskRow(path="x", title="t", status=STATUS_DONE)
        assert not done_row.is_actionable(
            retry_failed=True, retry_partial=True, retry_cancelled=True
        )
        # pending row always actionable
        pending_row = TaskRow(path="x", title="t", status=STATUS_PENDING)
        assert pending_row.is_actionable(
            retry_failed=False, retry_partial=False, retry_cancelled=False
        )
        # failed needs retry_failed=True
        failed_row = TaskRow(path="x", title="t", status=STATUS_FAILED)
        assert not failed_row.is_actionable(
            retry_failed=False, retry_partial=False, retry_cancelled=True
        )
        assert failed_row.is_actionable(
            retry_failed=True, retry_partial=False, retry_cancelled=True
        )
        # partial needs retry_partial=True
        partial_row = TaskRow(path="x", title="t", status=STATUS_PARTIAL)
        assert not partial_row.is_actionable(
            retry_failed=True, retry_partial=False, retry_cancelled=True
        )
        assert partial_row.is_actionable(
            retry_failed=True, retry_partial=True, retry_cancelled=True
        )
        print("[smoke] is_actionable matrix OK")

        # ---- 8. Counts override edge: smaller plan still detects done ----
        # Resolve with main=0, scene=1 only → should detect 1/1 done since
        # scene_01 is among existing OK.
        missing3, n_ok3, n_expected3, _ = compute_missing_slots(
            pa, settings, counts_str="main=0,scene=1,multi=0,size=0,detail=0,angle=0,material=0"
        )
        print(f"[smoke] custom counts: missing={missing3}, ok={n_ok3}/{n_expected3}")
        assert n_expected3 == 1 and missing3 == [] and n_ok3 == 1, (
            "custom counts (main=0,scene=1) should resolve to 1/1 done"
        )

        print("[smoke] ALL ASSERTIONS PASSED")
        return 0
    finally:
        try:
            shutil.rmtree(tmp_root, ignore_errors=True)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
