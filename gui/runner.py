"""Glue between Gradio UI and src pipeline (settings merge, providers)."""

from __future__ import annotations

import dataclasses
import json
import re
import threading
from pathlib import Path
from typing import Any

from src.config import PROJECT_ROOT, Settings, load_settings
from src.counts_config import ALLOWED_TYPES, TYPE_ORDER, load_resolved_counts
from src.main import discover_products, get_brief_generator, get_provider
from src.pipeline import process_product

from gui.config_store import load_gui_config
from gui.paths import resolved_config_yaml, resolved_prompts_dir
from src.batch_runner import BatchProductSpec, run_batch


def effective_settings(gui_cfg: dict[str, Any] | None = None) -> Settings:
    base = load_settings()
    g = gui_cfg if gui_cfg is not None else load_gui_config()
    api = str(g.get("image_api_key", "") or "").strip() or base.image_api_key
    prov = str(g.get("image_provider", "") or base.image_provider).strip().lower()
    return dataclasses.replace(
        base,
        image_api_key=api,
        image_provider=prov or base.image_provider,
        xais_model_id=str(g.get("xais_model_id", "") or base.xais_model_id).strip(),
        xais_api_base=str(g.get("xais_api_base", "") or base.xais_api_base).strip(),
        xais_timeout=int(g.get("xais_timeout", base.xais_timeout)),
        shiyun_model_id=str(g.get("shiyun_model_id", "") or base.shiyun_model_id).strip(),
        shiyun_api_base=str(g.get("shiyun_api_base", "") or base.shiyun_api_base).strip(),
        shiyun_timeout=int(g.get("shiyun_timeout", base.shiyun_timeout)),
        gemini_model_id=str(g.get("gemini_model_id", "") or base.gemini_model_id).strip(),
        prompts_dir=resolved_prompts_dir(),
    )


def discover_under_root(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return discover_products(root)


def parse_regen_targets(s: str | None) -> set[tuple[str, int]] | None:
    if not s or not str(s).strip():
        return None
    out: set[tuple[str, int]] = set()
    pattern = re.compile(r"^([a-z]+)_(\d+)$")
    for part in str(s).split(","):
        part = part.strip()
        if not part:
            continue
        m = pattern.fullmatch(part)
        if not m:
            raise ValueError(f"重跑格式应为 TYPE_NN，例如 scene_02，收到: {part!r}")
        t = m.group(1)
        idx = int(m.group(2))
        if t not in ALLOWED_TYPES:
            raise ValueError(f"未知类型 {t!r}，允许: {list(ALLOWED_TYPES)}")
        if idx < 1:
            raise ValueError(f"索引必须 >= 1: {part!r}")
        out.add((t, idx))
    return out or None


def run_generation(
    *,
    project_root: Path,
    product_dir: Path,
    counts_str: str | None,
    regen_str: str | None,
    user_note: str | None,
    settings: Settings,
    progress_callback: object = None,
    custom_ref_paths: list[Path] | None = None,
    custom_prompt: str | None = None,
    cancel_event: threading.Event | None = None,
) -> dict:
    """Run pipeline for one product; returns meta dict."""
    _ = project_root  # reserved for future path resolution
    cfg_path = resolved_config_yaml()
    resolved = load_resolved_counts(
        product_dir=product_dir,
        cli_counts=counts_str,
        config_path=cfg_path,
        env_output_size=settings.output_size,
    )
    provider = get_provider(settings)
    brief_gen = get_brief_generator(settings)
    regen = parse_regen_targets(regen_str)
    note = (user_note or "").strip() or None
    cp = (custom_prompt or "").strip() or None
    return process_product(
        product_dir,
        provider,
        settings,
        resolved,
        brief_gen,
        dry_run=False,
        regen_targets=regen,
        user_note=note,
        progress_callback=progress_callback,
        custom_ref_paths=custom_ref_paths,
        custom_prompt=cp,
        cancel_event=cancel_event,
    )


def run_batch_generation(
    specs: list[BatchProductSpec],
    settings: Settings,
    *,
    progress_callback: object = None,
    product_done_callback: object = None,
    product_error_callback: object = None,
    cancel_event: threading.Event | None = None,
    concurrency: int | None = None,
    retry_failed_rounds: int | None = None,
) -> dict[Path, dict | None]:
    """Run multiple products with one global concurrency pool."""
    provider = get_provider(settings)
    brief_gen = get_brief_generator(settings)

    def on_slot_done(pdir: Path, type_name: str, done: int, total: int) -> None:
        if progress_callback is not None:
            progress_callback(pdir, type_name, done, total)

    def on_product_done(pdir: Path, meta: dict) -> None:
        if product_done_callback is not None:
            product_done_callback(pdir, meta)

    def on_product_error(pdir: Path, err: str) -> None:
        if product_error_callback is not None:
            product_error_callback(pdir, err)

    return run_batch(
        specs,
        provider,
        settings,
        brief_gen,
        concurrency=concurrency,
        retry_failed_rounds=retry_failed_rounds,
        cancel_event=cancel_event,
        on_slot_done=on_slot_done,
        on_product_done=on_product_done,
        on_product_error=on_product_error,
    )


def gallery_images_from_meta(product_dir: Path) -> list[str]:
    """Return sorted list of output PNG paths for gallery."""
    out = product_dir / "output"
    if not out.is_dir():
        return []
    paths = sorted(out.glob("*.png"), key=lambda p: p.name)
    return [str(p.resolve()) for p in paths]


def compute_missing_slots(
    product_dir: Path,
    settings: Settings,
    counts_str: str | None = None,
) -> tuple[list[str], int, int, bool]:
    """Inspect ``output/meta.json`` and figure out which slots still need to run.

    Compares the counts that *should* be produced (resolved via
    ``load_resolved_counts`` exactly the same way ``run_generation`` does)
    against what is actually OK on disk. A slot only counts as "done" when
    BOTH (a) ``meta["images"]`` reports ``status=ok`` for it AND (b) the
    matching ``<type>_<NN>.png`` file truly exists. Failure-placeholder PNGs
    have ``status=error`` in meta so they correctly fall out as missing.

    Returns ``(missing_slot_labels, n_existing_ok, n_expected, was_cancelled)``
    where ``missing_slot_labels`` is the comma-feed for ``regen_str`` (e.g.
    ``["main_01", "scene_02"]``) and ``was_cancelled`` reflects the previous
    run's ``meta["cancelled"]`` flag — useful for the caller to surface "续跑
    被终止的行" wording in the UI log.
    """
    cfg_path = resolved_config_yaml()
    resolved = load_resolved_counts(
        product_dir=product_dir,
        cli_counts=counts_str,
        config_path=cfg_path,
        env_output_size=settings.output_size,
    )

    expected: list[tuple[str, int]] = []
    for t in TYPE_ORDER:
        n = int(resolved.image_counts.get(t, 0))
        for i in range(1, n + 1):
            expected.append((t, i))

    existing_ok: set[tuple[str, int]] = set()
    was_cancelled = False
    meta_path = product_dir / "output" / "meta.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = None
        if isinstance(meta, dict):
            was_cancelled = bool(meta.get("cancelled"))
            for im in meta.get("images") or []:
                if not isinstance(im, dict):
                    continue
                if im.get("status") != "ok":
                    continue
                t_name = str(im.get("type") or "").strip()
                try:
                    t_idx = int(im.get("index") or 0)
                except (TypeError, ValueError):
                    continue
                if not t_name or t_idx <= 0:
                    continue
                fname = f"{t_name}_{t_idx:02d}.png"
                if (product_dir / "output" / fname).is_file():
                    existing_ok.add((t_name, t_idx))

    missing = [(t, i) for (t, i) in expected if (t, i) not in existing_ok]
    missing_labels = [f"{t}_{i:02d}" for (t, i) in missing]
    # Count only the overlap with expected so display reads "ok/total" cleanly
    # even when the user shrunk counts and the meta still has stale slots.
    n_existing_in_expected = sum(1 for s in expected if s in existing_ok)
    return missing_labels, n_existing_in_expected, len(expected), was_cancelled
