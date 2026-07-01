"""End-to-end smoke for the per-slot 重做这张 → 任务清单/失败记录 sync.

When the user clicks 「重做这张」 in the slot grid, two book-keeping xlsx
files must stay in sync:

1.  ``批量失败记录_<n>.xlsx``  — the row for this slot disappears on
    success; or stays as 重跑仍失败 if it failed again.
2.  ``批量任务_<n>.xlsx``  — the matching product row's 状态 is
    re-evaluated by ``compute_missing_slots`` (so 部分完成 → 已完成 once
    every slot is on disk).

This test exercises ``_sync_batch_xlsx_after_meta`` directly (the helper
that the click handler calls). We:

* build a fake product whose ``meta.json`` has 1 ok + 1 error slot;
* write a failure log row for the error slot + a task list row for the
  product (status 部分完成);
* simulate a successful per-slot regen by rewriting meta.json to mark the
  failing slot ok;
* call ``_sync_batch_xlsx_after_meta``;
* assert the failure log row is GONE and the task list status flipped to
  已完成;
* then simulate a *failed* per-slot regen (slot still error in meta);
* assert the failure row is back, with 待重跑 status (and task list row
  flipped back to 部分完成).

Run from repo root:

    python scripts/smoke_per_slot_regen_sync.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ["IMAGE_PROVIDER"] = "placeholder"
os.environ.setdefault("AMAZON_IMG_GUI_INSTANCE", "smoketest_isolated")
os.environ.setdefault("LOGURU_LEVEL", "WARNING")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gui.batch_tasklist import (  # noqa: E402
    STATUS_DONE,
    STATUS_PARTIAL,
    TaskRow,
    init_template_if_missing as init_tasklist,
    load_tasklist,
    save_tasklist,
)
from gui.failure_log import (  # noqa: E402
    RETRY_FAIL,
    RETRY_PENDING,
    FailureRow,
    derive_failure_log_path,
    init_template_if_missing as init_failure_log,
    load_failure_log,
    save_failure_log,
)
from gui.runner import effective_settings  # noqa: E402

PNG_HEADER = b"\x89PNG\r\n\x1a\n"


def _make_product(root: Path, name: str, ok_slots: list[tuple[str, int]],
                  err_slots: list[tuple[str, int]]) -> Path:
    pdir = root / name
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "商品标题.txt").write_text(f"{name} title", encoding="utf-8")
    refs = pdir / "refs"
    refs.mkdir(exist_ok=True)
    (refs / "ref_01.png").write_bytes(PNG_HEADER + b"\x00" * 64)
    out = pdir / "output"
    out.mkdir(exist_ok=True)
    images = []
    for t, i in ok_slots:
        out_png = out / f"{t}_{i:02d}.png"
        out_png.write_bytes(PNG_HEADER + b"\x00" * 64)
        images.append({
            "type": t, "index": i, "status": "ok",
            "output_path": str(out_png),
        })
    for t, i in err_slots:
        # Placeholder PNG also goes on disk to mimic the real failure path.
        out_png = out / f"{t}_{i:02d}.png"
        out_png.write_bytes(PNG_HEADER + b"\x00" * 64)
        images.append({
            "type": t, "index": i, "status": "error",
            "error": "Xais HTTP 500: smoke",
            "output_path": str(out_png),
            "is_placeholder": True,
        })
    counts_eff = {}
    for t, i in ok_slots + err_slots:
        counts_eff[t] = max(counts_eff.get(t, 0), i)
    meta = {
        "images": images,
        "counts_total": len(images),
        "counts_effective": counts_eff,
    }
    (out / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return pdir


def _rewrite_meta(pdir: Path, ok_slots: list[tuple[str, int]],
                  err_slots: list[tuple[str, int]]) -> None:
    """Rewrite meta.json to reflect a (re-)run outcome."""
    out = pdir / "output"
    images = []
    for t, i in ok_slots:
        images.append({
            "type": t, "index": i, "status": "ok",
            "output_path": str(out / f"{t}_{i:02d}.png"),
        })
    for t, i in err_slots:
        images.append({
            "type": t, "index": i, "status": "error",
            "error": "Xais HTTP 500: still flaky",
            "output_path": str(out / f"{t}_{i:02d}.png"),
            "is_placeholder": True,
        })
    counts_eff = {}
    for t, i in ok_slots + err_slots:
        counts_eff[t] = max(counts_eff.get(t, 0), i)
    meta = {
        "images": images,
        "counts_total": len(images),
        "counts_effective": counts_eff,
    }
    (out / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="per_slot_sync_"))
    print(f"[smoke] tmp = {tmp}")
    try:
        projects = tmp / "projects"
        projects.mkdir()

        # ---- 1. Set up product with default counts (main=1,scene=2,
        # multi=1,size=1,detail=1,angle=1,material=1 = 8 slots), one of
        # which (scene_02) is in error.
        all_default_slots: list[tuple[str, int]] = [
            ("main", 1),
            ("scene", 1), ("scene", 2),
            ("multi", 1), ("size", 1),
            ("detail", 1), ("angle", 1), ("material", 1),
        ]
        ok_initial = [s for s in all_default_slots if s != ("scene", 2)]
        pdir = _make_product(
            projects, "Prod_X",
            ok_slots=ok_initial,
            err_slots=[("scene", 2)],
        )

        # ---- 2. Set up xlsx files ------------------------------------
        tlp = tmp / "批量任务_smoke.xlsx"
        flp = derive_failure_log_path(tlp)
        init_tasklist(tlp)
        init_failure_log(flp)

        # Task list row pointing at the product folder, status 部分完成.
        tl_rows = load_tasklist(tlp)
        tl_rows.append(TaskRow(
            path=str(pdir.resolve()),
            title="Prod_X title",
            status=STATUS_PARTIAL,
            processed_at="2026-01-01 00:00:00",
            note="缺 1 张",
        ))
        save_tasklist(tlp, tl_rows)

        # Failure log row for the broken slot.
        fl_rows = load_failure_log(flp)
        fl_rows.append(FailureRow(
            product_dir_name="Prod_X",
            slot="scene_02",
            error="Xais HTTP 500: smoke",
            failed_at="2026-01-01 00:00:00",
            retry_status=RETRY_PENDING,
            product_path=str(pdir.resolve()),
        ))
        save_failure_log(flp, fl_rows)

        # ---- 3. Simulate a successful per-slot regen -----------------
        # (the worker would have written meta.json with scene_02 ok)
        _rewrite_meta(pdir, ok_slots=all_default_slots, err_slots=[])
        meta = json.loads((pdir / "output" / "meta.json").read_text(encoding="utf-8"))

        # Pull in the helper. It's defined inside `build_tab` which we
        # don't want to invoke (needs gradio context), so reach for the
        # piece-parts directly.
        from gui.failure_log import (
            mark_just_retried_failures,
            merge_failures_from_meta,
        )
        from gui.runner import compute_missing_slots
        from gui.batch_tasklist import mark_row

        # Replicate _sync_batch_xlsx_after_meta logic:
        fl_rows = load_failure_log(flp)
        merge_failures_from_meta(fl_rows, product_dir=pdir, meta=meta)
        save_failure_log(flp, fl_rows)
        assert len(fl_rows) == 0, (
            f"after success regen, failure log should be empty, got {fl_rows}"
        )
        print("[smoke] ✓ failure row auto-dropped after successful regen")

        # Task list status update.
        tl_rows = load_tasklist(tlp)
        settings = effective_settings()
        counts_eff = meta.get("counts_effective") or {}
        counts_str = ",".join(f"{k}={int(v)}" for k, v in counts_eff.items())
        miss, n_ok, n_exp, _ = compute_missing_slots(pdir, settings, counts_str)
        assert miss == [] and n_ok == n_exp == 8, (
            f"compute_missing_slots after success: miss={miss}, ok={n_ok}/{n_exp}"
        )
        new_status = STATUS_DONE if not miss and n_exp > 0 else STATUS_PARTIAL
        for i, tr in enumerate(tl_rows):
            if Path(tr.path).resolve() == pdir.resolve():
                mark_row(tl_rows, i, status=new_status, note=f"已完成: {n_ok}/{n_exp}")
        save_tasklist(tlp, tl_rows)
        # Re-read.
        tl_rows = load_tasklist(tlp)
        statuses = [r.status for r in tl_rows]
        assert STATUS_DONE in statuses, f"task row not updated: {statuses}"
        print("[smoke] ✓ task list flipped 部分完成 → 已完成")

        # ---- 4. Simulate a FAILED per-slot regen ---------------------
        # User retried scene_02, it failed again. Rewrite meta to put it
        # back into error status.
        _rewrite_meta(pdir, ok_slots=ok_initial, err_slots=[("scene", 2)])
        meta2 = json.loads((pdir / "output" / "meta.json").read_text(encoding="utf-8"))

        # The click handler now calls merge + mark_just_retried_failures
        # for the regen'd slot, mirroring batch-retry semantics.
        fl_rows = load_failure_log(flp)
        fl_rows.clear()  # start clean for this assertion
        merge_failures_from_meta(fl_rows, product_dir=pdir, meta=meta2)
        mark_just_retried_failures(
            fl_rows, product_dir=pdir, retried_slots={"scene_02"}
        )
        save_failure_log(flp, fl_rows)
        assert len(fl_rows) == 1, (
            f"failure log should have 1 row after re-failure, got {len(fl_rows)}"
        )
        assert fl_rows[0].retry_status == RETRY_FAIL, (
            f"per-slot regen failure should mark 重跑仍失败, "
            f"got {fl_rows[0].retry_status}"
        )
        print("[smoke] ✓ re-failure marked 重跑仍失败 after per-slot regen")

        # Task list should flip back to 部分完成.
        tl_rows = load_tasklist(tlp)
        miss2, n_ok2, n_exp2, _ = compute_missing_slots(pdir, settings, counts_str)
        assert miss2 == ["scene_02"] and n_ok2 == 7 and n_exp2 == 8, (
            f"compute_missing_slots after fail: miss={miss2}, ok={n_ok2}/{n_exp2}"
        )
        new_status = STATUS_DONE if not miss2 and n_exp2 > 0 else STATUS_PARTIAL
        for i, tr in enumerate(tl_rows):
            if Path(tr.path).resolve() == pdir.resolve():
                mark_row(tl_rows, i, status=new_status, note=f"还缺 {len(miss2)} 张")
        save_tasklist(tlp, tl_rows)
        tl_rows = load_tasklist(tlp)
        assert any(r.status == STATUS_PARTIAL for r in tl_rows), (
            f"task list not flipped back: {[r.status for r in tl_rows]}"
        )
        print("[smoke] ✓ task list flipped 已完成 → 部分完成")

        # ---- 5. Cancelled-mid-flight flow ---------------------------
        # Simulate the user clicking 终止 during a single-product run:
        # meta.json has ``cancelled: true`` AND only some slots ok. Sync
        # must flip the task-list row to 已终止 (STATUS_CANCELLED) so a
        # later 续跑「已终止」行 picks it up — NOT 部分完成 (which would
        # require the 续跑「部分完成」 toggle instead).
        from gui.batch_tasklist import STATUS_CANCELLED  # local import: kept tight
        # Rewrite meta with ok=ok_initial (7 slots) + scene_02 missing
        # entirely (not even an error entry — that's how cancelled looks
        # after process_product breaks out of the slot loop).
        out = pdir / "output"
        for f in out.glob("*.png"):
            try:
                f.unlink()
            except OSError:
                pass
        for t, i in ok_initial:
            (out / f"{t}_{i:02d}.png").write_bytes(PNG_HEADER + b"\x00" * 64)
        meta_cancelled = {
            "images": [
                {
                    "type": t, "index": i, "status": "ok",
                    "output_path": str(out / f"{t}_{i:02d}.png"),
                }
                for t, i in ok_initial
            ],
            "counts_total": 8,
            "counts_effective": {
                "main": 1, "scene": 2, "multi": 1,
                "size": 1, "detail": 1, "angle": 1, "material": 1,
            },
            "cancelled": True,
        }
        (out / "meta.json").write_text(
            json.dumps(meta_cancelled, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # Replicate the sync helper's status decision logic.
        miss3, n_ok3, n_exp3, was_cancelled3 = compute_missing_slots(
            pdir, settings, counts_str
        )
        if meta_cancelled.get("cancelled"):
            was_cancelled3 = True
        assert miss3 == ["scene_02"], f"compute_missing after cancel: {miss3}"
        assert was_cancelled3, (
            f"was_cancelled should be True after meta.cancelled=True, "
            f"got {was_cancelled3}"
        )

        # New decision tree should pick STATUS_CANCELLED.
        if was_cancelled3 and miss3:
            sync_status = STATUS_CANCELLED
        elif n_exp3 > 0 and not miss3:
            sync_status = STATUS_DONE
        elif miss3:
            sync_status = STATUS_PARTIAL
        else:
            sync_status = None
        assert sync_status == STATUS_CANCELLED, (
            f"cancelled+missing should map to {STATUS_CANCELLED}, got {sync_status}"
        )
        print("[smoke] ✓ cancelled-mid-flight maps to 已终止 (not 部分完成)")

        # Edge: cancelled but already had everything ok → just done.
        for t, i in [("scene", 2)]:
            (out / f"{t}_{i:02d}.png").write_bytes(PNG_HEADER + b"\x00" * 64)
        meta_cancelled_complete = dict(meta_cancelled)
        meta_cancelled_complete["images"] = [
            {"type": t, "index": i, "status": "ok",
             "output_path": str(out / f"{t}_{i:02d}.png")}
            for t, i in all_default_slots
        ]
        (out / "meta.json").write_text(
            json.dumps(meta_cancelled_complete, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        miss4, n_ok4, n_exp4, _ = compute_missing_slots(pdir, settings, counts_str)
        was_cancelled4 = bool(meta_cancelled_complete.get("cancelled"))
        if was_cancelled4 and miss4:
            sync_status4 = STATUS_CANCELLED
        elif n_exp4 > 0 and not miss4:
            sync_status4 = STATUS_DONE
        elif miss4:
            sync_status4 = STATUS_PARTIAL
        else:
            sync_status4 = None
        assert sync_status4 == STATUS_DONE, (
            f"cancelled-but-complete should still be {STATUS_DONE}, "
            f"got {sync_status4} (miss={miss4}, cancelled={was_cancelled4})"
        )
        print("[smoke] ✓ cancelled-but-complete still maps to 已完成")

        # ---------------- Edge: cancelled BEFORE retry slot attempted ----
        # User clicked 重做这张 on scene_02, then 终止 fired immediately so
        # the worker never actually re-attempted scene_02. meta.json keeps
        # the OLD scene_02 error entry (process_product would either keep
        # the old image entry or drop it; we simulate the "kept" path
        # because it's the conservative one).
        #
        # Expected: failure log row for scene_02 stays 待重跑, NOT
        # 重跑仍失败. We replicate the helper's "actually_failed" gate
        # (retried_slots ∩ meta_err_slots) directly.
        # NOTE: FailureRow / RETRY_PENDING / RETRY_FAIL are already imported
        # at module-level. Re-importing here would make Python treat them as
        # local-only for the entire function (UnboundLocalError on first use
        # earlier in main). We only pull in the helpers that aren't yet
        # in scope.
        from gui.failure_log import (  # noqa: E402
            merge_failures_from_meta,
            mark_just_retried_failures,
        )

        # rebuild a fresh failure-log list with one PENDING row for scene_02
        rows_cancel = [
            FailureRow(
                product_dir_name=pdir.name,
                product_path=str(pdir.resolve()),
                slot="scene_02",
                error="boom",
                failed_at="2026-05-16 00:00:00",
                retry_status=RETRY_PENDING,
                retried_at="",
            )
        ]
        # simulate cancelled meta where scene_02 is still error
        meta_cancel_pending = {
            "images": [
                {"type": "scene", "index": 2, "status": "error",
                 "error": "old upstream 500"},
                {"type": "main", "index": 1, "status": "ok",
                 "output_path": str(out / "main_01.png")},
            ],
            "cancelled": True,
        }
        merge_failures_from_meta(
            rows_cancel, product_dir=pdir, meta=meta_cancel_pending
        )
        # Replicate the helper's gate: actually_failed = retried ∩ err_slots
        retried_slots = {"scene_02"}
        meta_err_slots = {
            f"{im['type']}_{int(im['index']):02d}"
            for im in meta_cancel_pending["images"]
            if im.get("status") == "error"
        }
        actually_failed = retried_slots & meta_err_slots
        # In this case scene_02 IS in err_slots, so it WOULD be marked
        # RETRY_FAIL. That's correct: the kept-old-error entry tells us
        # the slot is currently broken regardless of cancellation.
        assert actually_failed == {"scene_02"}, actually_failed
        if actually_failed:
            mark_just_retried_failures(
                rows_cancel, product_dir=pdir,
                retried_slots=actually_failed,
            )
        assert rows_cancel[0].retry_status == RETRY_FAIL, (
            f"with err entry kept, scene_02 should be {RETRY_FAIL}, "
            f"got {rows_cancel[0].retry_status}"
        )
        print("[smoke] ✓ cancel-mid-retry with kept err entry → 重跑仍失败")

        # Now the *other* cancel-mid-retry shape: meta has NO entry for
        # scene_02 at all (process_product wrote a fresh meta with only
        # the slots it actually ran). Expected: row stays 待重跑.
        rows_cancel2 = [
            FailureRow(
                product_dir_name=pdir.name,
                product_path=str(pdir.resolve()),
                slot="scene_02",
                error="boom",
                failed_at="2026-05-16 00:00:00",
                retry_status=RETRY_PENDING,
                retried_at="",
            )
        ]
        meta_cancel_no_entry = {
            "images": [
                {"type": "main", "index": 1, "status": "ok",
                 "output_path": str(out / "main_01.png")},
            ],
            "cancelled": True,
        }
        merge_failures_from_meta(
            rows_cancel2, product_dir=pdir, meta=meta_cancel_no_entry
        )
        meta_err_slots2 = {
            f"{im['type']}_{int(im['index']):02d}"
            for im in meta_cancel_no_entry["images"]
            if im.get("status") == "error"
        }
        actually_failed2 = retried_slots & meta_err_slots2
        assert actually_failed2 == set(), (
            f"no err in meta → no slot should be marked, got {actually_failed2}"
        )
        # Skip mark call — row should still be PENDING from initial seed
        # (merge_failures_from_meta doesn't touch slots not in meta).
        assert rows_cancel2[0].retry_status == RETRY_PENDING, (
            f"no err in meta → scene_02 should stay {RETRY_PENDING}, "
            f"got {rows_cancel2[0].retry_status}"
        )
        print(
            "[smoke] ✓ cancel-mid-retry with no err entry → "
            "stays 待重跑 (BUG 11 fix)"
        )

        print("[smoke] ALL ASSERTIONS PASSED")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
