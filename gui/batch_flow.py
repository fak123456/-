"""Helpers for global batch generation in the Generate tab."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from gui.batch_tasklist import TaskRow
from gui.runner import compute_missing_slots, parse_regen_targets
from src.batch_runner import BatchProductSpec
from src.config import Settings


@dataclass
class PendingBatchJob:
    row_idx: int
    ji: int
    n_jobs: int
    product_dir: Path
    current_md: str
    spec: BatchProductSpec


@dataclass
class BatchPrepareResult:
    pending: list[PendingBatchJob]
    logs: list[str]
    resolve_failed: list[tuple[int, str, str, str]]  # row_idx, err_msg, log, md_suffix
    skip_done: list[tuple[int, str, str, str]]  # row_idx, note, log, md_suffix


def prepare_batch_jobs(
    *,
    actionable_indices: list[int],
    task_rows: list[TaskRow],
    root_path: Path,
    settings: Settings,
    counts_arg: str | None,
    config_path: Path,
    user_note: str,
    resolve_one_input,
) -> BatchPrepareResult:
    """Resolve actionable rows into global batch specs."""
    pending: list[PendingBatchJob] = []
    logs: list[str] = []
    resolve_failed: list[tuple[int, str, str, str]] = []
    skip_done: list[tuple[int, str, str, str]] = []
    n_jobs = len(actionable_indices)

    for ji, row_idx in enumerate(actionable_indices, start=1):
        row = task_rows[row_idx]
        current_md = (
            f"### 当前任务 [{ji}/{n_jobs}]\n\n"
            f"- **路径**：`{row.path}`\n"
            f"- **标题**：{row.title or '（空）'}\n"
            f"- **状态**：{row.status} → 处理中…"
        )
        logs.append(f"[{ji}/{n_jobs}] ▶ {row.path}")

        product_dir, err_msg = resolve_one_input(row.path, row.title, root_path)
        if product_dir is None:
            resolve_failed.append(
                (row_idx, err_msg, f"❌ [{ji}/{n_jobs}] {row.path}: {err_msg}", f"❌ {err_msg}")
            )
            continue

        try:
            missing_labels, n_existing_ok, n_expected, was_cancelled_prev = compute_missing_slots(
                product_dir, settings, counts_arg
            )
        except Exception as e:
            missing_labels, n_existing_ok, n_expected, was_cancelled_prev = [], 0, 0, False
            logs.append(f"⚠️  续跑检测失败（按全新生成处理）: {e}")

        row_regen_str: str | None
        if n_expected > 0 and not missing_labels:
            resume_note = (
                f"已完成（meta 检测到 {n_existing_ok}/{n_expected} 张全部 OK，未重新生成）"
            )
            log_line = f"⏭ [{ji}/{n_jobs}] SKIP {product_dir.name}: {resume_note}"
            logs.append(log_line)
            skip_done.append(
                (
                    row_idx,
                    resume_note,
                    log_line,
                    f"⏭ 已完成({n_existing_ok}/{n_expected})",
                )
            )
            continue
        if n_existing_ok > 0 and missing_labels:
            row_regen_str = ",".join(missing_labels)
            resume_tag = "续跑(终止)" if was_cancelled_prev else "续跑"
            logs.append(
                f"♻️ [{ji}/{n_jobs}] {product_dir.name} {resume_tag}: 已有 "
                f"{n_existing_ok}/{n_expected}，本次只生成 {len(missing_labels)} 张 "
                f"({row_regen_str})"
            )
        else:
            row_regen_str = None

        regen_set = parse_regen_targets(row_regen_str)
        spec = BatchProductSpec(
            product_dir=product_dir,
            regen_targets=regen_set,
            user_note=(user_note or "").strip() or None,
            counts_str=counts_arg,
            config_path=config_path,
        )
        pending.append(
            PendingBatchJob(
                row_idx=row_idx,
                ji=ji,
                n_jobs=n_jobs,
                product_dir=product_dir,
                current_md=current_md,
                spec=spec,
            )
        )

    return BatchPrepareResult(
        pending=pending,
        logs=logs,
        resolve_failed=resolve_failed,
        skip_done=skip_done,
    )
