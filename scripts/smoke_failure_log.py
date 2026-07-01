"""End-to-end smoke for the failure-log + 一键重跑 flow (auto-drop semantics).

Builds 2 fake products with seeded ``meta.json`` that has *some* slots in
``status=error`` (the kind real Xais 500s produce). Then we:

1. Hand the seeded metas to ``merge_failures_from_meta`` — expect rows to
   appear in the in-memory failure log.
2. Save and re-load the xlsx — expect the rows to round-trip 1:1.
3. Group by product, call ``run_generation`` with ``regen_str`` set to those
   exact slots (placeholder provider always succeeds → meta will now show
   ``status=ok`` for every retried slot).
4. Re-merge the new metas — under the new auto-drop semantics, every
   row that's now ``status=ok`` should **disappear** from the failure log
   (instead of being marked 已重跑成功). So the final list is empty.
5. Sanity check: hand-poke a meta to seed a NEW failure on the same
   product, re-merge, expect the new row to appear, then call
   ``mark_just_retried_failures`` to confirm it lands in 重跑仍失败.

Run from repo root:

    python scripts/smoke_failure_log.py

Exit code 0 = green; non-zero on any assertion miss.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

# Force placeholder provider so we never hit the real API.
os.environ["IMAGE_PROVIDER"] = "placeholder"
# Pin a throwaway instance label so effective_settings() loads an empty
# gui config (no ~/.amazon_img_gui_<label>/config.json on disk → env var
# IMAGE_PROVIDER wins). Without this, a real user config that has
# image_provider=xais would silently override the env var and the smoke
# would try to hit the live API.
os.environ.setdefault("AMAZON_IMG_GUI_INSTANCE", "smoketest_isolated")
# Quiet the heartbeat noise so the smoke output is readable.
os.environ.setdefault("LOGURU_LEVEL", "WARNING")

import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gui.failure_log import (  # noqa: E402
    RETRY_FAIL,
    RETRY_OK,
    RETRY_PENDING,
    FailureRow,
    derive_failure_log_path,
    group_by_product,
    init_template_if_missing,
    load_failure_log,
    mark_just_retried_failures,
    merge_failures_from_meta,
    save_failure_log,
)
from gui.runner import effective_settings, run_generation  # noqa: E402

PNG_HEADER = b"\x89PNG\r\n\x1a\n"


def _make_fake_product(root: Path, name: str, title: str) -> Path:
    """Build a minimal product folder mimicking the crawler's output."""
    pdir = root / name
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "商品标题.txt").write_text(title, encoding="utf-8")
    refs = pdir / "refs"
    refs.mkdir(exist_ok=True)
    # 1×1 PNG (8 bytes header + minimal — Pillow tolerates this for refs scan).
    (refs / "ref_01.png").write_bytes(PNG_HEADER + b"\x00" * 100)
    (pdir / "output").mkdir(exist_ok=True)
    return pdir


def _seed_meta_with_errors(product_dir: Path, error_slots: list[tuple[str, int, str]]) -> None:
    """Write an output/meta.json that pretends some slots failed."""
    images = []
    # Fake "ok" slot for main_01 to show that good slots stay untouched.
    images.append({
        "type": "main",
        "index": 1,
        "status": "ok",
        "output_path": str(product_dir / "output" / "main_01.png"),
    })
    for type_name, idx, err in error_slots:
        images.append({
            "type": type_name,
            "index": idx,
            "status": "error",
            "error": err,
            "output_path": str(product_dir / "output" / f"{type_name}_{idx:02d}.png"),
            "is_placeholder": True,
        })
    meta = {"images": images, "counts_total": 1 + len(error_slots)}
    (product_dir / "output" / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> int:
    tmp_root = Path(tempfile.mkdtemp(prefix="failure_log_smoke_"))
    print(f"[smoke] tmp = {tmp_root}")
    try:
        projects = tmp_root / "projects"
        projects.mkdir()

        # ---- 1. Build 2 fake products with seeded errors -------------
        pa = _make_fake_product(projects, "Prod_A", "Product A test 标题")
        pb = _make_fake_product(projects, "Prod_B", "Product B test 标题")
        _seed_meta_with_errors(pa, [
            ("scene", 2, "Xais HTTP 500: 任务请求失败"),
            ("size", 1, "Xais HTTP 500: 任务请求失败"),
        ])
        _seed_meta_with_errors(pb, [
            ("detail", 1, "Network timeout"),
        ])
        print("[smoke] seeded 3 failures across 2 products")

        # ---- 2. Initial merge → failure log gets 3 rows --------------
        tasklist_path = tmp_root / "批量任务_smoketest.xlsx"
        flp = derive_failure_log_path(tasklist_path)
        init_template_if_missing(flp)
        rows: list[FailureRow] = []
        for pdir in (pa, pb):
            meta = json.loads((pdir / "output" / "meta.json").read_text(encoding="utf-8"))
            merge_failures_from_meta(rows, product_dir=pdir, meta=meta)
        save_failure_log(flp, rows)
        print(f"[smoke] failure log written: {flp} ({len(rows)} rows)")
        assert len(rows) == 3, f"expected 3 failure rows, got {len(rows)}"
        assert all(r.retry_status == RETRY_PENDING for r in rows), [r.retry_status for r in rows]

        # ---- 3. Round-trip xlsx -------------------------------------
        rows2 = load_failure_log(flp)
        assert len(rows2) == 3, f"round-trip dropped rows: {len(rows2)}"
        for a, b in zip(rows, rows2):
            assert a.slot == b.slot, f"slot mismatch: {a.slot} vs {b.slot}"
            assert a.product_path == b.product_path, "path mismatch on round-trip"
            assert a.retry_status == b.retry_status, "status mismatch on round-trip"
        print("[smoke] xlsx round-trip OK")

        # ---- 4. Group by product, retry via run_generation ----------
        groups = group_by_product(rows, include_already_retried=False)
        assert len(groups) == 2, f"expected 2 products in group, got {len(groups)}"
        # Use defaults for counts so placeholder provider produces all slots.
        settings = effective_settings()
        assert settings.image_provider == "placeholder", (
            f"expected placeholder provider via env, got {settings.image_provider}"
        )

        for product_dir, dir_name, items in groups:
            slot_str = ",".join(r.slot for _, r in items)
            print(f"[smoke] retry on {dir_name}: regen_str={slot_str!r}")
            meta_after = run_generation(
                project_root=Path("."),
                product_dir=product_dir,
                counts_str=None,
                regen_str=slot_str,
                user_note=None,
                settings=settings,
                progress_callback=None,
                cancel_event=None,
            )
            # Sanity: every retried slot should now be status=ok.
            statuses = {
                f"{im['type']}_{int(im['index']):02d}": im["status"]
                for im in (meta_after.get("images") or [])
                if isinstance(im, dict) and im.get("type") and im.get("index")
            }
            for _, r in items:
                got = statuses.get(r.slot)
                assert got == "ok", f"{dir_name}/{r.slot} expected ok, got {got!r}"
            # New auto-drop semantics: merge should REMOVE the now-ok rows.
            retried_slots = {r.slot for _, r in items}
            merge_failures_from_meta(rows, product_dir=product_dir, meta=meta_after)
            # And mark_just_retried_failures should be a no-op since all the
            # retried slots are now gone.
            n_marked = mark_just_retried_failures(
                rows, product_dir=product_dir, retried_slots=retried_slots
            )
            assert n_marked == 0, (
                f"expected no rows to mark for {dir_name} (all retried slots fixed), "
                f"got {n_marked}"
            )

        save_failure_log(flp, rows)
        # ---- 5. Assert the failure log is now EMPTY ------------------
        assert len(rows) == 0, (
            f"expected empty failure log after all-success retry, got {len(rows)} rows: "
            f"{[(r.product_dir_name, r.slot, r.retry_status) for r in rows]}"
        )
        print("[smoke] all 3 retried slots auto-dropped from the failure log OK")

        # ---- 6. Re-load from disk to confirm -------------------------
        rows_final = load_failure_log(flp)
        assert len(rows_final) == 0, "xlsx round-trip lost the auto-drop"
        print("[smoke] final xlsx is empty as expected")

        # ---- 7. Re-failure scenario: same product, same slot fails ---
        # Simulate: user runs again, scene_02 fails AGAIN. The merge should
        # re-record it (PENDING). Then mark_just_retried_failures should
        # flip it to 重跑仍失败.
        _seed_meta_with_errors(pa, [
            ("scene", 2, "Xais HTTP 500: still flaky"),
        ])
        meta_a2 = json.loads((pa / "output" / "meta.json").read_text(encoding="utf-8"))
        merge_failures_from_meta(rows_final, product_dir=pa, meta=meta_a2)
        assert any(r.product_path == str(pa.resolve()) and r.slot == "scene_02"
                   for r in rows_final), "re-failure not recorded"
        # All other slots in pa's meta are ok now → no leftover rows for them.
        scene_rows = [r for r in rows_final if r.product_path == str(pa.resolve())]
        assert len(scene_rows) == 1, f"expected 1 row for pa, got {len(scene_rows)}"
        assert scene_rows[0].retry_status == RETRY_PENDING, (
            f"freshly-recorded re-failure should be PENDING, got {scene_rows[0].retry_status}"
        )

        # Now simulate "user clicked 一键重跑, it failed again" → mark.
        n_marked = mark_just_retried_failures(
            rows_final, product_dir=pa, retried_slots={"scene_02"}
        )
        assert n_marked == 1
        scene_rows = [r for r in rows_final if r.product_path == str(pa.resolve())]
        assert scene_rows[0].retry_status == RETRY_FAIL, (
            f"after mark_just_retried_failures, expected {RETRY_FAIL}, "
            f"got {scene_rows[0].retry_status}"
        )
        print("[smoke] re-failure flow recorded + marked 重跑仍失败 OK")

        # ---- 8. Edge: a row that the meta no longer mentions ---------
        # Slot vanishes from meta entirely (e.g. user shrunk counts). The
        # merge fn shouldn't touch it — only mark_just_retried_failures
        # should, and only when the runner says it was actually retried.
        ghost = FailureRow(
            product_dir_name=pa.name,
            slot="scene_99",
            error="seeded ghost",
            failed_at="2026-01-01 00:00:00",
            retry_status=RETRY_PENDING,
            product_path=str(pa.resolve()),
        )
        rows_final.append(ghost)
        merge_failures_from_meta(rows_final, product_dir=pa, meta=meta_a2)
        for r in rows_final:
            if r.slot == "scene_99":
                assert r.retry_status == RETRY_PENDING, f"ghost flipped to {r.retry_status}"
        print("[smoke] ghost slot untouched by merge OK")

        print("[smoke] ALL ASSERTIONS PASSED")
        return 0
    finally:
        try:
            shutil.rmtree(tmp_root, ignore_errors=True)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
