"""Single-product image generation pipeline (type × count)."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from src.briefs import BriefResolution, resolve_briefs
from src.config import Settings
from src.counts_config import TYPE_ORDER, ResolvedCounts
from src.image_io import collect_reference_paths, list_ref_paths, read_image_bytes, save_jpg
from src.prompts import BuiltPrompt, build_prompt
from src.providers.base import ImageProvider
from src.providers.brief_base import BriefGenerator
from src.providers.placeholder import make_failure_placeholder_png
from src.refs_layout import ensure_refs_folder
from src.size_ref import ensure_auto_size_ref
from src.utils.logger import get_logger
from src.utils.retry import retry_with_backoff

logger = get_logger()

TITLE_FILE = "商品标题.txt"


OUTPUT_EXT = ".jpg"
_OUTPUT_NAME_RE = re.compile(r"^(main|scene|multi|size|detail|angle|material)_\d{2,}\.(?:png|jpg|jpeg)$")


@dataclass
class SlotTask:
    """One image slot prepared on the main thread before concurrent execution."""

    type_name: str
    idx: int
    type_total: int
    user_brief: str
    brief_src: str
    user_note_for_prompt: str | None
    built: BuiltPrompt
    out_path: Path
    ref_paths_send: list[Path]
    ref_bytes_ordered: list[bytes]


@dataclass
class ProductRunContext:
    """Shared per-product state for concurrent slot workers."""

    product_dir: Path
    product_name: str
    title: str
    provider: ImageProvider
    settings: Settings
    resolved: ResolvedCounts
    ref_paths_send: list[Path]
    ref_bytes_ordered: list[bytes]
    type_ref_paths_send: dict[str, list[Path]]
    type_ref_bytes_ordered: dict[str, list[bytes]]
    out_size: str
    output_dir: Path
    regen_targets: set[tuple[str, int]] | None = None


def _clean_stale_outputs(
    output_dir: Path,
    *,
    types_to_clean: set[str] | None = None,
    exact_files: set[str] | None = None,
) -> None:
    """Remove previously generated type_NN image files from output_dir."""
    if not output_dir.is_dir():
        return
    for p in output_dir.iterdir():
        if not p.is_file():
            continue
        m = _OUTPUT_NAME_RE.match(p.name)
        if not m:
            continue
        if exact_files is not None:
            if p.name not in exact_files and p.with_suffix(OUTPUT_EXT).name not in exact_files:
                continue
        elif types_to_clean is not None and m.group(1) not in types_to_clean:
            continue
        try:
            p.unlink()
        except OSError:
            logger.warning(f"Could not remove stale file: {p}")


def read_title(product_dir: Path) -> str:
    path = product_dir / TITLE_FILE
    if not path.is_file():
        raise FileNotFoundError(f"Missing title file: {path}")
    return path.read_text(encoding="utf-8").strip()


def _entry_template(
    task: SlotTask,
    ctx: ProductRunContext,
    *,
    status: str = "pending",
) -> dict:
    return {
        "type": task.type_name,
        "index": task.idx,
        "type_total": task.type_total,
        "output_path": str(task.out_path.resolve()),
        "ref_paths_sent": [str(p.resolve()) for p in task.ref_paths_send],
        "user_brief": task.user_brief,
        "brief_source": task.brief_src,
        "user_note": (task.user_note_for_prompt or "").strip() or None,
        "prompt_full_length": len(task.built.full),
        "prompt_global_excerpt": task.built.global_text[:200],
        "prompt_type_excerpt": task.built.type_text[:500],
        "status": status,
    }


def run_one_slot(
    ctx: ProductRunContext,
    task: SlotTask,
    *,
    request_sem: threading.Semaphore | None = None,
) -> dict:
    """Generate one image slot; returns the meta entry dict."""
    entry = _entry_template(task, ctx)

    @retry_with_backoff(max_attempts=ctx.settings.max_retries)
    def _generate() -> bytes:
        if request_sem is not None:
            with request_sem:
                return ctx.provider.generate(
                    prompt=task.built.full,
                    reference_images=task.ref_bytes_ordered,
                    size=ctx.out_size,
                    api_image_size=ctx.resolved.generation.gemini_native_image_size,
                )
        return ctx.provider.generate(
            prompt=task.built.full,
            reference_images=task.ref_bytes_ordered,
            size=ctx.out_size,
            api_image_size=ctx.resolved.generation.gemini_native_image_size,
        )

    try:
        image_bytes = _generate()
        save_jpg(image_bytes, task.out_path, ctx.out_size)
        entry["status"] = "ok"
    except Exception as e:
        logger.exception(f"{task.type_name} #{task.idx} failed for {ctx.product_name}")
        entry["status"] = "error"
        entry["error"] = str(e)
        try:
            ph_bytes = make_failure_placeholder_png(
                type_name=task.type_name,
                idx=task.idx,
                max_attempts=ctx.settings.max_retries,
                error_message=str(e),
                prompt_excerpt=task.built.full[:200],
                size=ctx.out_size,
            )
            save_jpg(ph_bytes, task.out_path, ctx.out_size)
            entry["is_placeholder"] = True
        except Exception:
            logger.warning(
                f"Failed to write failure placeholder for {task.type_name}_{task.idx:02d}"
            )
    return entry


def _sort_entries(entries: list[dict]) -> list[dict]:
    return sorted(
        entries,
        key=lambda e: (
            TYPE_ORDER.index(e.get("type")) if e.get("type") in TYPE_ORDER else 99,
            int(e.get("index", 0)),
        ),
    )


def execute_slots_concurrent(
    ctx: ProductRunContext,
    tasks: list[SlotTask],
    *,
    concurrency: int,
    retry_failed_rounds: int = 0,
    cancel_event: threading.Event | None = None,
    progress_callback: Callable[[str, int, int], None] | None = None,
    request_sem: threading.Semaphore | None = None,
) -> tuple[list[dict], bool]:
    """Run slot tasks with a thread pool; optionally retry failed entries.

    Returns ``(entries, cancelled)``.
    """
    if not tasks:
        return [], False

    total_steps = len(tasks)
    entries_by_key: dict[tuple[str, int], dict] = {}
    progress_lock = threading.Lock()
    step_done = 0
    cancelled = False

    def _run_pool(task_batch: list[SlotTask]) -> None:
        nonlocal step_done, cancelled
        if not task_batch:
            return
        workers = min(max(1, concurrency), len(task_batch))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_map: dict[Future, SlotTask] = {}
            for task in task_batch:
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    break
                fut = pool.submit(run_one_slot, ctx, task, request_sem=request_sem)
                future_map[fut] = task

            for fut in as_completed(future_map):
                task = future_map[fut]
                key = (task.type_name, task.idx)
                try:
                    entry = fut.result()
                except Exception as e:
                    logger.exception(
                        f"Unexpected worker failure {task.type_name}_{task.idx:02d} "
                        f"for {ctx.product_name}"
                    )
                    entry = _entry_template(task, ctx, status="error")
                    entry["error"] = str(e)
                entries_by_key[key] = entry
                with progress_lock:
                    step_done += 1
                    done = step_done
                if progress_callback is not None:
                    progress_callback(task.type_name, done, total_steps)

    _run_pool(tasks)

    for _round in range(retry_failed_rounds):
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            break
        failed_tasks = [
            t
            for t in tasks
            if entries_by_key.get((t.type_name, t.idx), {}).get("status") == "error"
        ]
        if not failed_tasks:
            break
        logger.info(
            f"Retry round {_round + 1}/{retry_failed_rounds} for {ctx.product_name}: "
            f"{len(failed_tasks)} failed slot(s)"
        )
        _run_pool(failed_tasks)

    ordered = _sort_entries(list(entries_by_key.values()))
    return ordered, cancelled


def prepare_slot_tasks(
    *,
    ctx: ProductRunContext,
    brief_resolution: BriefResolution,
    regen_targets: set[tuple[str, int]] | None,
    user_note: str | None,
    custom_prompt: str | None,
    dry_run: bool = False,
) -> list[SlotTask]:
    """Build SlotTask list for all slots that will run this pass."""
    cp_strip = (custom_prompt or "").strip()
    tasks: list[SlotTask] = []
    for type_name in TYPE_ORDER:
        n = int(ctx.resolved.image_counts.get(type_name, 0))
        for idx in range(1, n + 1):
            if regen_targets is not None and (type_name, idx) not in regen_targets:
                continue
            user_brief, brief_src = brief_resolution.get(type_name, idx)
            user_note_for_prompt = user_note
            if cp_strip and regen_targets is not None and (type_name, idx) in regen_targets:
                user_brief = cp_strip
                brief_src = "custom_gui"
                note_parts = [x for x in ((user_note or "").strip(), cp_strip) if x]
                user_note_for_prompt = "\n".join(note_parts) if note_parts else None
            built = build_prompt(
                type_name,
                idx,
                n,
                ctx.title,
                user_brief,
                ctx.settings,
                user_note=user_note_for_prompt,
            )
            out_path = ctx.output_dir / f"{type_name}_{idx:02d}{OUTPUT_EXT}"
            ref_paths_send = ctx.type_ref_paths_send.get(type_name, ctx.ref_paths_send)
            ref_bytes_ordered = ctx.type_ref_bytes_ordered.get(type_name, ctx.ref_bytes_ordered)
            tasks.append(
                SlotTask(
                    type_name=type_name,
                    idx=idx,
                    type_total=n,
                    user_brief=user_brief,
                    brief_src=brief_src,
                    user_note_for_prompt=user_note_for_prompt,
                    built=built,
                    out_path=out_path,
                    ref_paths_send=ref_paths_send,
                    ref_bytes_ordered=ref_bytes_ordered,
                )
            )
    if dry_run:
        return tasks
    return tasks


def write_product_meta(
    ctx: ProductRunContext,
    meta: dict,
    *,
    regen_targets: set[tuple[str, int]] | None,
) -> None:
    """Persist meta.json, merging with prior entries when regen_targets is set."""
    meta_path = ctx.output_dir / "meta.json"
    if regen_targets is not None and meta_path.is_file():
        try:
            old_meta = json.loads(meta_path.read_text(encoding="utf-8"))
            old_images = old_meta.get("images", []) if isinstance(old_meta, dict) else []
            kept = [
                e for e in old_images
                if (e.get("type"), e.get("index")) not in regen_targets
            ]
            merged = kept + meta["images"]
            meta["images"] = _sort_entries(merged)
        except Exception:
            logger.warning("Failed to merge old meta.json entries; writing fresh meta.")
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"Wrote {meta_path}")


def build_product_run_context(
    product_dir: Path,
    provider: ImageProvider,
    settings: Settings,
    resolved: ResolvedCounts,
    *,
    regen_targets: set[tuple[str, int]] | None = None,
    custom_ref_paths: list[Path] | None = None,
    type_ref_paths: dict[str, list[Path]] | None = None,
    dry_run: bool = False,
) -> ProductRunContext:
    """Load refs and prepare output dir for a product run."""
    if regen_targets is not None:
        bad = []
        for (t, i) in regen_targets:
            n = int(resolved.image_counts.get(t, 0))
            if i < 1 or i > n:
                bad.append(f"{t}_{i:02d} (current counts: {t}={n})")
        if bad:
            raise ValueError(
                "--regen targets out of range for current counts: " + "; ".join(bad)
            )

    if dry_run:
        if custom_ref_paths is not None:
            ref_paths = [p.expanduser().resolve() for p in custom_ref_paths]
        else:
            ref_paths = collect_reference_paths(product_dir)
    elif custom_ref_paths is not None:
        ref_paths = []
        for p in custom_ref_paths:
            rp = Path(p).expanduser().resolve()
            if rp.is_file():
                ref_paths.append(rp)
    else:
        ensure_refs_folder(product_dir)
        refs_dir = product_dir / "refs"
        ref_paths = list_ref_paths(refs_dir)

    type_ref_paths_resolved: dict[str, list[Path]] = {}
    if type_ref_paths:
        for type_name, paths in type_ref_paths.items():
            clean: list[Path] = []
            for p in paths:
                rp = Path(p).expanduser().resolve()
                if rp.is_file():
                    clean.append(rp)
            if clean:
                type_ref_paths_resolved[type_name] = clean
    elif not dry_run and custom_ref_paths is None and int(resolved.image_counts.get("size", 0)) > 0:
        picked = ensure_auto_size_ref(product_dir)
        if picked is not None and picked.path.is_file():
            type_ref_paths_resolved["size"] = [picked.path.expanduser().resolve()]

    if not ref_paths:
        raise ValueError(f"No reference images in {product_dir} (refs/ or root)")

    max_refs = resolved.generation.max_refs_per_call
    ref_paths_send = ref_paths[:max_refs]
    path_to_bytes: dict[Path, bytes] = {}
    ref_bytes_ordered: list[bytes] = []
    if not dry_run:
        for p in ref_paths:
            path_to_bytes[p] = read_image_bytes(p)
        for paths in type_ref_paths_resolved.values():
            for p in paths:
                if p not in path_to_bytes:
                    path_to_bytes[p] = read_image_bytes(p)
        ref_bytes_ordered = [path_to_bytes[p] for p in ref_paths_send]
    type_ref_paths_send: dict[str, list[Path]] = {}
    type_ref_bytes_ordered: dict[str, list[bytes]] = {}
    for type_name, paths in type_ref_paths_resolved.items():
        limited = paths[:max_refs]
        type_ref_paths_send[type_name] = limited
        if not dry_run:
            type_ref_bytes_ordered[type_name] = [path_to_bytes[p] for p in limited]

    output_dir = product_dir / "output"
    types_to_run = {t for t in TYPE_ORDER if int(resolved.image_counts.get(t, 0)) > 0}
    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)
        if regen_targets is not None:
            exact_files = {f"{t}_{i:02d}{OUTPUT_EXT}" for (t, i) in regen_targets}
            _clean_stale_outputs(output_dir, exact_files=exact_files)
        else:
            _clean_stale_outputs(output_dir, types_to_clean=types_to_run)

    title = read_title(product_dir)
    return ProductRunContext(
        product_dir=product_dir,
        product_name=product_dir.name,
        title=title,
        provider=provider,
        settings=settings,
        resolved=resolved,
        ref_paths_send=ref_paths_send,
        ref_bytes_ordered=ref_bytes_ordered,
        type_ref_paths_send=type_ref_paths_send,
        type_ref_bytes_ordered=type_ref_bytes_ordered,
        out_size=resolved.generation.size,
        output_dir=output_dir,
        regen_targets=regen_targets,
    )


def process_product(
    product_dir: Path,
    provider: ImageProvider,
    settings: Settings,
    resolved: ResolvedCounts,
    brief_generator: BriefGenerator,
    *,
    dry_run: bool = False,
    regen_targets: set[tuple[str, int]] | None = None,
    user_note: str | None = None,
    progress_callback: Callable[[str, int, int], None] | None = None,
    custom_ref_paths: list[Path] | None = None,
    type_ref_paths: dict[str, list[Path]] | None = None,
    custom_prompt: str | None = None,
    cancel_event: threading.Event | None = None,
    request_sem: threading.Semaphore | None = None,
) -> dict:
    """
    Generate images per resolved.image_counts.

    Writes output/{type}_{NN}.jpg and output/meta.json unless dry_run.
    """
    cp_strip = (custom_prompt or "").strip()
    if cp_strip and regen_targets is not None and len(regen_targets) != 1:
        raise ValueError("custom_prompt is only supported when regen_targets contains exactly one slot")

    ctx = build_product_run_context(
        product_dir,
        provider,
        settings,
        resolved,
        regen_targets=regen_targets,
        custom_ref_paths=custom_ref_paths,
        type_ref_paths=type_ref_paths,
        dry_run=dry_run,
    )
    product_name = ctx.product_name
    title = ctx.title
    logger.info(f"Processing {product_name}: {title[:80]}...")

    max_refs = resolved.generation.max_refs_per_call
    ref_paths_send = ctx.ref_paths_send
    if dry_run:
        total_refs = len(collect_reference_paths(product_dir) if custom_ref_paths is None else custom_ref_paths)
    else:
        ensure_refs_folder(product_dir) if custom_ref_paths is None else None
        total_refs = len(list_ref_paths(product_dir / "refs")) if custom_ref_paths is None else len(custom_ref_paths)
    counts_summary = " ".join(
        f"{t}={resolved.image_counts[t]}" for t in TYPE_ORDER if resolved.image_counts.get(t, 0) > 0
    )
    if regen_targets is not None:
        regen_summary = ",".join(sorted(f"{t}_{i:02d}" for (t, i) in regen_targets))
        logger.info(
            f"  plan: regen={regen_summary} ({len(regen_targets)} image(s)) | "
            f"refs sent={len(ref_paths_send)}/{total_refs} (cap={max_refs}) | "
            f"concurrency={resolved.generation.concurrency}"
        )
    else:
        logger.info(
            f"  plan: total={resolved.counts_total} ({counts_summary}) | "
            f"refs sent={len(ref_paths_send)}/{total_refs} (cap={max_refs}) | "
            f"concurrency={resolved.generation.concurrency}"
        )

    ref_images_for_brief: list[bytes] = []
    if not dry_run:
        if custom_ref_paths is not None:
            ref_images_for_brief = list(ctx.ref_bytes_ordered)
        else:
            refs_dir = product_dir / "refs"
            all_ref_paths = list_ref_paths(refs_dir)
            ref_images_for_brief = [read_image_bytes(p) for p in all_ref_paths]

    brief_resolution = resolve_briefs(
        product_dir=product_dir,
        prompts_dir=settings.prompts_dir,
        counts=resolved.image_counts,
        product_title=title,
        reference_images=ref_images_for_brief,
        brief_generator=brief_generator,
        persist_llm_cache=not dry_run,
    )

    tasks = prepare_slot_tasks(
        ctx=ctx,
        brief_resolution=brief_resolution,
        regen_targets=regen_targets,
        user_note=user_note,
        custom_prompt=custom_prompt,
        dry_run=dry_run,
    )

    meta: dict = {
        "product": product_name,
        "title": title,
        "counts_effective": dict(resolved.image_counts),
        "counts_total": resolved.counts_total,
        "counts_source": dict(resolved.counts_source),
        "generation_size": ctx.out_size,
        "max_refs_per_call": max_refs,
        "gemini_native_image_size": resolved.generation.gemini_native_image_size,
        "concurrency": resolved.generation.concurrency,
        "image_provider": settings.image_provider,
        "gemini_model_id": settings.gemini_model_id,
        "openai_model_id": settings.openai_model_id,
        "images": [],
    }

    if dry_run:
        for task in tasks:
            entry = _entry_template(task, ctx, status="dry_run")
            meta["images"].append(entry)
            if progress_callback is not None:
                progress_callback(task.type_name, len(meta["images"]), len(tasks))
        return meta

    if cancel_event is not None and cancel_event.is_set():
        meta["cancelled"] = True
        meta["images"] = []
        write_product_meta(ctx, meta, regen_targets=regen_targets)
        return meta

    entries, cancelled = execute_slots_concurrent(
        ctx,
        tasks,
        concurrency=resolved.generation.concurrency,
        retry_failed_rounds=resolved.generation.retry_failed_rounds,
        cancel_event=cancel_event,
        progress_callback=progress_callback,
        request_sem=request_sem,
    )
    meta["images"] = entries
    if cancelled:
        meta["cancelled"] = True

    write_product_meta(ctx, meta, regen_targets=regen_targets)
    return meta
