"""Global batch orchestration: flatten slots across products into one thread pool."""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from src.config import Settings
from src.counts_config import TYPE_ORDER, load_resolved_counts
from src.pipeline import (
    ProductRunContext,
    SlotTask,
    _entry_template,
    build_product_run_context,
    prepare_slot_tasks,
    run_one_slot,
    write_product_meta,
)
from src.providers.base import ImageProvider
from src.providers.brief_base import BriefGenerator
from src.briefs import resolve_briefs
from src.image_io import list_ref_paths, read_image_bytes
from src.refs_layout import ensure_refs_folder
from src.utils.logger import get_logger

logger = get_logger()


@dataclass
class BatchProductSpec:
    """One product to include in a global batch run."""

    product_dir: Path
    regen_targets: set[tuple[str, int]] | None = None
    user_note: str | None = None
    counts_str: str | None = None
    config_path: Path | None = None
    custom_ref_paths: list[Path] | None = None
    custom_prompt: str | None = None
    job_label: str = ""


@dataclass
class _GlobalSlotJob:
    product_key: str
    ctx: ProductRunContext
    task: SlotTask
    regen_targets: set[tuple[str, int]] | None


@dataclass
class _ProductAggregate:
    ctx: ProductRunContext
    meta: dict
    regen_targets: set[tuple[str, int]] | None
    entries: dict[tuple[str, int], dict] = field(default_factory=dict)
    remaining: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    finalized: bool = False


def _product_key(product_dir: Path) -> str:
    return str(product_dir.resolve())


def _prepare_product_jobs(
    spec: BatchProductSpec,
    provider: ImageProvider,
    settings: Settings,
    brief_generator: BriefGenerator,
) -> tuple[_ProductAggregate | None, list[_GlobalSlotJob], str | None]:
    """Return (aggregate, slot_jobs, error_message)."""
    try:
        resolved = load_resolved_counts(
            product_dir=spec.product_dir,
            cli_counts=spec.counts_str,
            config_path=spec.config_path,
            env_output_size=settings.output_size,
        )
        ctx = build_product_run_context(
            spec.product_dir,
            provider,
            settings,
            resolved,
            regen_targets=spec.regen_targets,
            custom_ref_paths=spec.custom_ref_paths,
        )

        if spec.custom_ref_paths is not None:
            ref_images_for_brief = list(ctx.ref_bytes_ordered)
        else:
            ensure_refs_folder(spec.product_dir)
            all_ref_paths = list_ref_paths(spec.product_dir / "refs")
            ref_images_for_brief = [read_image_bytes(p) for p in all_ref_paths]

        brief_resolution = resolve_briefs(
            product_dir=spec.product_dir,
            prompts_dir=settings.prompts_dir,
            counts=resolved.image_counts,
            product_title=ctx.title,
            reference_images=ref_images_for_brief,
            brief_generator=brief_generator,
            persist_llm_cache=True,
        )
        tasks = prepare_slot_tasks(
            ctx=ctx,
            brief_resolution=brief_resolution,
            regen_targets=spec.regen_targets,
            user_note=spec.user_note,
            custom_prompt=spec.custom_prompt,
        )
        if not tasks:
            return None, [], None

        meta: dict = {
            "product": ctx.product_name,
            "title": ctx.title,
            "counts_effective": dict(resolved.image_counts),
            "counts_total": resolved.counts_total,
            "counts_source": dict(resolved.counts_source),
            "generation_size": ctx.out_size,
            "max_refs_per_call": resolved.generation.max_refs_per_call,
            "gemini_native_image_size": resolved.generation.gemini_native_image_size,
            "concurrency": resolved.generation.concurrency,
            "image_provider": settings.image_provider,
            "gemini_model_id": settings.gemini_model_id,
            "openai_model_id": settings.openai_model_id,
            "images": [],
        }
        agg = _ProductAggregate(
            ctx=ctx,
            meta=meta,
            regen_targets=spec.regen_targets,
            remaining=len(tasks),
        )
        pkey = _product_key(spec.product_dir)
        jobs = [
            _GlobalSlotJob(
                product_key=pkey,
                ctx=ctx,
                task=task,
                regen_targets=spec.regen_targets,
            )
            for task in tasks
        ]
        return agg, jobs, None
    except Exception as e:
        return None, [], str(e)


def _finalize_aggregate(
    agg: _ProductAggregate,
    *,
    cancelled: bool,
) -> dict:
    ordered = sorted(
        agg.entries.values(),
        key=lambda e: (
            TYPE_ORDER.index(e.get("type")) if e.get("type") in TYPE_ORDER else 99,
            int(e.get("index", 0)),
        ),
    )
    agg.meta["images"] = ordered
    if cancelled:
        agg.meta["cancelled"] = True
    write_product_meta(agg.ctx, agg.meta, regen_targets=agg.regen_targets)
    agg.finalized = True
    return agg.meta


def _has_error_entries(agg: _ProductAggregate) -> bool:
    return any(e.get("status") == "error" for e in agg.entries.values())


def _run_job_batch(
    jobs: list[_GlobalSlotJob],
    *,
    aggregates: dict[str, _ProductAggregate],
    concurrency: int,
    cancel_event: threading.Event | None,
    on_slot_done: Callable[[Path, str, int, int], None] | None,
    on_product_done: Callable[[Path, dict], None] | None,
    request_sem: threading.Semaphore,
    progress_lock: threading.Lock,
    global_done: list[int],
    global_total: int,
    is_last_round: bool,
    advance_progress: bool,
) -> bool:
    """Execute one round of global slot jobs. Returns True if cancelled.

    A product is finalized (meta written + on_product_done) at most ONCE: when
    all its slots in this round are done AND it has no remaining errors OR this
    is the last round (so it won't be retried again).
    """
    cancelled = False
    if not jobs:
        return cancelled

    workers = min(max(1, concurrency), len(jobs))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map: dict[Future, _GlobalSlotJob] = {}
        for job in jobs:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            fut = pool.submit(run_one_slot, job.ctx, job.task, request_sem=request_sem)
            future_map[fut] = job

        for fut in as_completed(future_map):
            job = future_map[fut]
            agg = aggregates[job.product_key]
            try:
                entry = fut.result()
            except Exception as e:
                logger.exception(
                    f"Unexpected worker failure {job.task.type_name}_{job.task.idx:02d} "
                    f"for {job.ctx.product_name}"
                )
                entry = _entry_template(job.task, job.ctx, status="error")
                entry["error"] = str(e)

            product_finished = False
            finished_meta: dict | None = None
            with agg.lock:
                agg.entries[(job.task.type_name, job.task.idx)] = entry
                agg.remaining -= 1
                if agg.remaining <= 0 and not agg.finalized:
                    is_cancelled = bool(cancelled) or (
                        cancel_event is not None and cancel_event.is_set()
                    )
                    will_retry = (not is_last_round) and (not is_cancelled) and _has_error_entries(agg)
                    if not will_retry:
                        finished_meta = _finalize_aggregate(agg, cancelled=is_cancelled)
                        product_finished = True

            if advance_progress:
                with progress_lock:
                    global_done[0] += 1
                    done = min(global_done[0], global_total)
            else:
                done = global_total
            if on_slot_done is not None:
                on_slot_done(
                    job.ctx.product_dir,
                    job.task.type_name,
                    done,
                    global_total,
                )
            if product_finished and finished_meta is not None and on_product_done is not None:
                on_product_done(job.ctx.product_dir, finished_meta)

    return cancelled


def run_batch(
    products: list[BatchProductSpec],
    provider: ImageProvider,
    settings: Settings,
    brief_generator: BriefGenerator,
    *,
    concurrency: int | None = None,
    retry_failed_rounds: int | None = None,
    cancel_event: threading.Event | None = None,
    on_slot_done: Callable[[Path, str, int, int], None] | None = None,
    on_product_done: Callable[[Path, dict], None] | None = None,
    on_product_error: Callable[[Path, str], None] | None = None,
) -> dict[Path, dict | None]:
    """Run many products with one global concurrency cap.

    Returns ``{product_dir: meta_or_none}`` (``None`` if prepare failed).
    """
    if not products:
        return {}

    # Use first product's resolved settings as defaults for pool sizing
    first_resolved = load_resolved_counts(
        product_dir=products[0].product_dir,
        cli_counts=products[0].counts_str,
        config_path=products[0].config_path,
        env_output_size=settings.output_size,
    )
    pool_size = concurrency if concurrency is not None else first_resolved.generation.concurrency
    extra_rounds = (
        retry_failed_rounds
        if retry_failed_rounds is not None
        else first_resolved.generation.retry_failed_rounds
    )

    aggregates: dict[str, _ProductAggregate] = {}
    all_jobs: list[_GlobalSlotJob] = []
    results: dict[Path, dict | None] = {}

    for spec in products:
        agg, jobs, err = _prepare_product_jobs(spec, provider, settings, brief_generator)
        if err is not None:
            logger.error(f"Batch prepare failed for {spec.product_dir}: {err}")
            results[spec.product_dir] = None
            if on_product_error is not None:
                on_product_error(spec.product_dir, err)
            continue
        if agg is None or not jobs:
            results[spec.product_dir] = {"product": spec.product_dir.name, "images": []}
            if on_product_done is not None:
                on_product_done(spec.product_dir, results[spec.product_dir])
            continue
        pkey = _product_key(spec.product_dir)
        aggregates[pkey] = agg
        all_jobs.extend(jobs)

    global_total = len(all_jobs)
    global_done = [0]
    progress_lock = threading.Lock()
    request_sem = threading.Semaphore(pool_size)
    cancelled = False
    rounds_total = 1 + max(0, extra_rounds)

    if cancel_event is not None and cancel_event.is_set():
        cancelled = True
    else:
        cancelled = _run_job_batch(
            all_jobs,
            aggregates=aggregates,
            concurrency=pool_size,
            cancel_event=cancel_event,
            on_slot_done=on_slot_done,
            on_product_done=on_product_done,
            request_sem=request_sem,
            progress_lock=progress_lock,
            global_done=global_done,
            global_total=global_total,
            is_last_round=(rounds_total == 1),
            advance_progress=True,
        )

    for round_idx in range(1, rounds_total):
        if cancelled or (cancel_event is not None and cancel_event.is_set()):
            cancelled = True
            break
        failed_jobs = [
            job
            for job in all_jobs
            if aggregates[job.product_key].entries.get(
                (job.task.type_name, job.task.idx), {}
            ).get("status")
            == "error"
        ]
        if not failed_jobs:
            break
        logger.info(
            f"Global retry round {round_idx}/{rounds_total - 1}: "
            f"{len(failed_jobs)} failed slot(s)"
        )
        # Reset per-round remaining counters for products with failed slots.
        touched_keys = {j.product_key for j in failed_jobs}
        for pkey in touched_keys:
            agg = aggregates[pkey]
            with agg.lock:
                agg.remaining = sum(1 for j in failed_jobs if j.product_key == pkey)
        cancelled = _run_job_batch(
            failed_jobs,
            aggregates=aggregates,
            concurrency=pool_size,
            cancel_event=cancel_event,
            on_slot_done=on_slot_done,
            on_product_done=on_product_done,
            request_sem=request_sem,
            progress_lock=progress_lock,
            global_done=global_done,
            global_total=global_total,
            is_last_round=(round_idx == rounds_total - 1),
            advance_progress=False,
        )

    # Finalize any products not yet finalized (cancelled mid-run, or edge cases).
    for agg in aggregates.values():
        with agg.lock:
            if not agg.finalized:
                _finalize_aggregate(agg, cancelled=cancelled)
                if on_product_done is not None:
                    on_product_done(agg.ctx.product_dir, agg.meta)

    for spec in products:
        pkey = _product_key(spec.product_dir)
        agg = aggregates.get(pkey)
        if agg is not None:
            results[spec.product_dir] = agg.meta
        elif spec.product_dir not in results:
            results[spec.product_dir] = None

    return results
