"""Generate tab: pick product, counts, regen, run pipeline, preview gallery + per-slot redo."""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import gradio as gr

from gui.batch_tasklist import (
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_PENDING,
    STATUS_RUNNING,
    TaskRow,
    default_tasklist_path,
    format_summary,
    init_template_if_missing,
    load_tasklist,
    mark_row,
    open_in_default_app,
    reset_all_status,
    save_tasklist,
    to_df_rows,
)
from gui.failure_log import (
    RETRY_FAIL,
    RETRY_OK,
    RETRY_PENDING,
    FailureRow,
    clear_all,
    clear_resolved,
    derive_failure_log_path,
    format_summary as format_failure_summary,
    group_by_product as group_failures_by_product,
    init_template_if_missing as init_failure_log_if_missing,
    load_failure_log,
    mark_just_retried_failures,
    merge_failures_from_meta,
    open_in_default_app as open_failure_log,
    save_failure_log,
    to_df_rows as failure_to_df_rows,
)
from gui.config_store import load_saved_paths, save_paths_into_config
from gui.history_store import append_run
from gui.paths import app_workdir
from gui.runner import (
    compute_missing_slots,
    discover_under_root,
    effective_settings,
    gallery_images_from_meta,
    run_generation,
)
from src.config import PROJECT_ROOT  # noqa: F401  (kept for backwards-compat imports)
from src.counts_config import ALLOWED_TYPES, DEFAULT_COUNTS, TYPE_ORDER
from src.image_io import collect_reference_paths, list_ref_paths
from src.size_ref import (
    ensure_auto_size_ref,
    list_size_ref_candidates,
    save_selected_size_ref,
    score_size_ref,
)

TITLE_FILE = "商品标题.txt"
SLOT_COUNT = 20
SLOT_COLS = 4
SLOT_ROWS = SLOT_COUNT // SLOT_COLS
SLOT_WIDGETS = 6  # cap, im, combined_cg, note, prompt, col (outer Column visibility)
_OUT_IMG_RE = re.compile(r"^(main|scene|multi|size|detail|angle|material)_\d{2,}\.(?:png|jpe?g)$", re.I)
_NEW_OUTPUT_EXT = ".jpg"
_REFS_PREFIX = "refs/"
_OUT_PREFIX = "output/"
RECENT_LOG_MAX = 120
STUCK_WARN_SEC = 600.0  # ~10 min without any event => surface a stuck-warning


def _output_filename(type_name: str, idx: int, ext: str = _NEW_OUTPUT_EXT) -> str:
    return f"{type_name}_{idx:02d}{ext}"


def _output_path_for_slot(out_dir: Path, type_name: str, idx: int) -> Path:
    jpg = out_dir / _output_filename(type_name, idx, ".jpg")
    if jpg.is_file():
        return jpg
    jpeg = out_dir / _output_filename(type_name, idx, ".jpeg")
    if jpeg.is_file():
        return jpeg
    png = out_dir / _output_filename(type_name, idx, ".png")
    if png.is_file():
        return png
    return jpg


def _attach_log_sink(q: "queue.Queue") -> int | None:
    """Attach a loguru sink that forwards records into ``q`` as ``("log", level, text)``.

    Returns the sink id so the caller can later detach it. Failures (no loguru,
    sink rejected, etc.) are swallowed: a missing sink only loses retry/warning
    breadcrumbs in the UI but never breaks the worker.
    """
    try:
        from src.utils.logger import get_logger

        lg = get_logger()

        def _sink(message: Any) -> None:
            try:
                rec = getattr(message, "record", None)
                if rec is not None:
                    level = str(rec["level"].name)
                    txt = str(rec["message"])
                else:
                    level = "INFO"
                    txt = str(message).rstrip()
            except Exception:
                level = "INFO"
                txt = str(message).rstrip() if message is not None else ""
            try:
                q.put(("log", level, txt))
            except Exception:
                pass

        return lg.add(_sink, level="INFO", format="{message}")
    except Exception:
        return None


def _detach_log_sink(sink_id: int | None) -> None:
    if sink_id is None:
        return
    try:
        from src.utils.logger import get_logger

        get_logger().remove(sink_id)
    except Exception:
        pass


def _fmt_log_line(level: str, text: str) -> str:
    """One log line for the UI textbox; warnings/errors get an obvious prefix."""
    lvl = (level or "INFO").upper()
    if lvl in {"WARNING", "WARN"}:
        return f"⚠️  {text}"
    if lvl in {"ERROR", "CRITICAL"}:
        return f"❌ {text}"
    return text


def _render_log(recent: list[str], suffix: str = "") -> str:
    body = "\n".join(recent[-RECENT_LOG_MAX:])
    if suffix:
        return f"{body}\n{suffix}" if body else suffix
    return body


class _BusyTracker:
    """Process-wide guard so 单/批/重做 三个入口互斥，并承载 cancel 事件。

    Holds a single in-flight generation across all three click handlers; the
    second concurrent click gets a ``gr.Warning`` toast and returns without
    yielding so its outputs (gallery / slots / dropdown) stay untouched.
    The lock is plain ``threading.Lock`` because everything runs in one
    process; multiple Gradio queue workers would still serialise on it.

    ``try_acquire`` returns the per-run ``threading.Event`` on success (or
    ``None`` if something is already running); the pipeline polls that event
    before each image so the «终止生成» button can stop the run cleanly
    without killing the worker thread mid-HTTP.
    """

    _KIND_LABELS = {
        "single": "单商品生成",
        "batch": "批量生成",
        "regen": "单图重做",
    }

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = False
        self._kind = ""
        self._product = ""
        self._started_at = 0.0
        self._cancel_event: threading.Event | None = None

    def try_acquire(self, kind: str, product: str = "") -> threading.Event | None:
        with self._lock:
            if self._active:
                return None
            self._active = True
            self._kind = kind
            self._product = (product or "").strip()
            self._started_at = time.monotonic()
            self._cancel_event = threading.Event()
            return self._cancel_event

    def release(self) -> None:
        with self._lock:
            self._active = False
            self._kind = ""
            self._product = ""
            self._started_at = 0.0
            self._cancel_event = None

    def is_active(self) -> bool:
        """Read-only flag the browser watchdog uses to avoid yanking a busy run."""
        with self._lock:
            return self._active

    def request_cancel(self) -> tuple[bool, str]:
        """Signal the in-flight worker to stop before the next image.

        Returns ``(fired, message)``. ``fired=False`` means nothing was
        running so the click was a no-op.
        """
        with self._lock:
            if not self._active or self._cancel_event is None:
                return False, "当前没有正在运行的生成任务，无需终止。"
            already = self._cancel_event.is_set()
            self._cancel_event.set()
            label = self._KIND_LABELS.get(self._kind, self._kind or "图像生成")
            who = f"（{self._product}）" if self._product else ""
            if already:
                return True, f"已再次发出终止信号：{label}{who}。当前张完成后即停。"
            return True, (
                f"已请求终止：{label}{who}。"
                "本张完成后立即停止，已生成的图保留；可重新点开始。"
            )

    def busy_message(self) -> str:
        with self._lock:
            if not self._active:
                return "无正在执行的任务。"
            label = self._KIND_LABELS.get(self._kind, self._kind or "图像生成")
            elapsed = max(0, int(time.monotonic() - self._started_at))
            who = f"（{self._product}）" if self._product else ""
            return (
                f"图像生成 API 正在调用中：{label}{who}，已运行 {elapsed} 秒。\n"
                "请等当前任务结束后再发起新请求；参考图勾选 / 自定义提示词可以继续编辑，"
                "改完之后再点对应的「开始生成 / 批量开始 / 重做这张」即可。"
            )


_BUSY = _BusyTracker()


def is_generation_busy() -> bool:
    """Module-level accessor for the browser-close watchdog in ``gui.app``.

    Exposed so ``gui/browser_watchdog.start_browser_watchdog`` can pass it
    as ``is_busy=`` without importing the private ``_BUSY`` symbol.
    """
    return _BUSY.is_active()


def _friendly_error(exc: BaseException) -> str:
    msg = str(exc).lower()
    s = str(exc)
    if "401" in msg or "403" in msg or ("unauthorized" in msg) or ("invalid" in msg and "key" in msg):
        return "API 密钥无效或已过期，请在「设置」中检查 Xais / IMAGE_API_KEY。"
    if "500" in msg or "参考图最多" in s:
        return f"服务端错误（可能参考图过多或模型限制）: {exc}"
    if "timeout" in msg or "timed out" in msg:
        return "请求超时，请稍后重试或检查网络。"
    return f"生成失败: {exc}"


# ---- Dataframe truncation helpers ----------------------------------------
#
# The on-screen dataframes are *read-only previews* — for full inspection
# the user is expected to click the matching「📋 打开 Excel」button. Show
# at most ``_DF_PREVIEW_CAP`` data rows; if there are more, append a single
# placeholder row "… 还有 N 行 …" so the UI stays compact.

_DF_PREVIEW_CAP = 5


def _capped_tasklist_df(task_rows: list) -> list[list[str]]:
    """Return at most ``_DF_PREVIEW_CAP`` task-list rows + 1 ellipsis row.

    The 5-column ellipsis row points the user at「📋 打开 Excel 编辑」for
    the full content. When the list fits inside the cap we return all rows
    untouched so the dataframe shrinks naturally for tiny task lists.
    """
    flat = to_df_rows(task_rows)
    if len(flat) <= _DF_PREVIEW_CAP:
        return flat
    extra = len(flat) - _DF_PREVIEW_CAP
    visible = flat[:_DF_PREVIEW_CAP]
    visible.append([
        f"… 还有 {extra} 行未显示（共 {len(flat)} 行）。"
        "点上方「📋 打开 Excel 编辑」查看 / 编辑完整任务清单。",
        "", "", "", "",
    ])
    return visible


def _capped_failure_df(failure_rows: list) -> list[list[str]]:
    """Return at most ``_DF_PREVIEW_CAP`` failure rows + 1 ellipsis row.

    The 7-column ellipsis row points the user at「📋 打开失败记录 Excel」
    for the full content.
    """
    flat = failure_to_df_rows(failure_rows)
    if len(flat) <= _DF_PREVIEW_CAP:
        return flat
    extra = len(flat) - _DF_PREVIEW_CAP
    visible = flat[:_DF_PREVIEW_CAP]
    visible.append([
        f"… 还有 {extra} 行未显示（共 {len(flat)} 行）",
        "", "", "", "", "",
        "点上方「📋 打开失败记录 Excel」查看完整记录",
    ])
    return visible


def _read_title_file(product_dir: Path) -> str:
    p = product_dir / TITLE_FILE
    if not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write_title_file(product_dir: Path, title: str) -> None:
    t = (title or "").strip()
    if not t:
        return
    (product_dir / TITLE_FILE).write_text(t, encoding="utf-8")


def _load_meta_images(product_dir: Path) -> list[dict]:
    p = product_dir / "output" / "meta.json"
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    imgs = data.get("images")
    return imgs if isinstance(imgs, list) else []


def _combined_ref_choices(
    product_dir: Path, exclude_output_name: str | None
) -> tuple[list[str], list[str]]:
    """Return ``(choices, default_value)`` for the per-slot combined picker.

    - ``choices`` lists every selectable image: ``refs/<name>`` first, then
      ``output/<name>`` for each existing generated image (excluding the slot's
      own file if ``exclude_output_name`` is given).
    - ``default_value`` checks all ``refs/*`` entries and none of the outputs,
      matching the previous default behaviour where refs were used unless the
      user opted into adding outputs.
    """
    choices: list[str] = []
    refs_choices: list[str] = []
    for p in list_ref_paths(product_dir / "refs"):
        s = f"{_REFS_PREFIX}{p.name}"
        choices.append(s)
        refs_choices.append(s)
    out_dir = product_dir / "output"
    if out_dir.is_dir():
        for p in sorted(out_dir.iterdir(), key=lambda x: x.name.casefold()):
            if not p.is_file() or not _OUT_IMG_RE.match(p.name):
                continue
            if exclude_output_name and p.name == exclude_output_name:
                continue
            choices.append(f"{_OUT_PREFIX}{p.name}")
    return choices, refs_choices


def _preview_gallery_items(product_dir: Path) -> list[tuple[str, str]]:
    """Items for the shared preview gallery: refs first, then output images.

    Each item is ``(filepath, caption)`` where caption is the filename so the
    user can match the label against the per-slot checkbox names.
    """
    items: list[tuple[str, str]] = []
    refs_dir = product_dir / "refs"
    for p in list_ref_paths(refs_dir):
        items.append((str(p.resolve()), f"refs/{p.name}"))
    out_dir = product_dir / "output"
    if out_dir.is_dir():
        for p in sorted(out_dir.iterdir(), key=lambda x: x.name.casefold()):
            if not p.is_file():
                continue
            if not _OUT_IMG_RE.match(p.name):
                continue
            items.append((str(p.resolve()), f"output/{p.name}"))
    return items


def _ordered_slot_keys(product_dir: Path) -> list[tuple[str, int, Path | None]]:
    """Union of meta.json entries and disk output images, ordered by TYPE_ORDER then index.

    Each entry: ``(type_name, index, on_disk_path_or_None)``. Used as the source
    of truth for the 20-slot grid both at runtime (during a generation) and after
    completion. Meta.json provides the expected scaffold (so a freshly-deleted
    slot stays visible while it regenerates) and the disk fills in real files.
    """
    out_dir = product_dir / "output"
    by_key: dict[tuple[str, int], Path | None] = {}
    order: list[tuple[str, int]] = []
    type_rank = {t: i for i, t in enumerate(TYPE_ORDER)}

    for im in _load_meta_images(product_dir):
        t = str(im.get("type", "")).strip().lower()
        try:
            idx = int(im.get("index", 0))
        except (TypeError, ValueError):
            continue
        if t not in type_rank or idx < 1:
            continue
        key = (t, idx)
        if key not in by_key:
            order.append(key)
        p = _output_path_for_slot(out_dir, t, idx)
        by_key[key] = p if p.is_file() else None

    if out_dir.is_dir():
        for p in out_dir.iterdir():
            if not p.is_file():
                continue
            m = _OUT_IMG_RE.match(p.name)
            if not m:
                continue
            t = m.group(1).lower()
            try:
                idx = int(p.stem.rsplit("_", 1)[-1])
            except ValueError:
                continue
            if idx < 1:
                continue
            key = (t, idx)
            if key not in by_key:
                order.append(key)
            by_key[key] = p

    order.sort(key=lambda k: (type_rank.get(k[0], 99), k[1]))
    return [(t, i, by_key.get((t, i))) for (t, i) in order]


def _cap_for_slot(t: str, idx: int, disk_p: Path | None, marker: tuple[str, int] | None) -> str:
    """Caption for a single slot. Adds visual badges for in-progress/pending."""
    name = f"{t}_{idx:02d}"
    if disk_p is not None and disk_p.is_file():
        return f"**{name}**"
    if marker is not None and marker == (t, idx):
        return f"### 🔄 {name} 生成中…"
    return f"⏳ {name} 待生成"


def _slot_state_and_updates_full(
    product_dir: Path,
    *,
    marker: tuple[str, int] | None = None,
    schedule: list[tuple[str, int]] | None = None,
) -> tuple[list, list]:
    """20-slot state + SLOT_COUNT * SLOT_WIDGETS gr.update values.

    Drives the slot grid from the merge of meta.json and disk so that
    progressive yields during a run show new images immediately while still
    keeping placeholders visible for slots whose image was just deleted.

    When ``schedule`` is given, it overrides the meta+disk ordering and is
    used as the authoritative slot list (so 「待生成」 slots also appear).
    When ``marker`` is given, the matching slot's caption shows 「🔄 生成中…」.
    """
    if schedule:
        out_dir = product_dir / "output"
        pairs = [
            (t, i, _output_path_for_slot(out_dir, t, i) if _output_path_for_slot(out_dir, t, i).is_file() else None)
            for (t, i) in schedule
        ]
    else:
        pairs = _ordered_slot_keys(product_dir)
    state: list = [None] * SLOT_COUNT
    updates: list = []

    for i in range(SLOT_COUNT):
        if i < len(pairs):
            t, idx, disk_p = pairs[i]
            state[i] = [t, idx]
            ok_path = str(disk_p.resolve()) if disk_p is not None and disk_p.is_file() else None
            cap = _cap_for_slot(t, idx, disk_p, marker)
            excl = _output_filename(t, idx)
            choices, default_val = _combined_ref_choices(product_dir, excl)
            updates.extend(
                [
                    gr.update(value=cap),
                    gr.update(value=ok_path),
                    gr.update(choices=choices, value=default_val),
                    gr.update(value=""),
                    gr.update(value=""),
                    gr.update(visible=True),
                ]
            )
        else:
            updates.extend(
                [
                    gr.update(value=""),
                    gr.update(value=None),
                    gr.update(choices=[], value=[]),
                    gr.update(value=""),
                    gr.update(value=""),
                    gr.update(visible=False),
                ]
            )
    return state, updates


def _slot_image_only_updates(
    product_dir: Path,
    *,
    marker: tuple[str, int] | None = None,
) -> tuple[list, list]:
    """Lightweight update: refresh ONLY caption + image preview per slot.

    Leaves CheckboxGroup/Textbox/Button values untouched so user input on a
    slot (e.g. custom prompt and chosen refs) survives in-progress yields
    during 「重做这张」.

    When ``marker`` is given, that slot's caption shows 「🔄 生成中…」.
    """
    pairs = _ordered_slot_keys(product_dir)
    state: list = [None] * SLOT_COUNT
    updates: list = []

    for i in range(SLOT_COUNT):
        if i < len(pairs):
            t, idx, disk_p = pairs[i]
            state[i] = [t, idx]
            ok_path = str(disk_p.resolve()) if disk_p is not None and disk_p.is_file() else None
            cap = _cap_for_slot(t, idx, disk_p, marker)
            updates.extend(
                [
                    gr.update(value=cap),
                    gr.update(value=ok_path),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(visible=True),
                ]
            )
        else:
            updates.extend(
                [
                    gr.update(value=""),
                    gr.update(value=None),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(visible=False),
                ]
            )
    return state, updates


def _empty_slot_updates_full() -> list:
    u: list = []
    for _ in range(SLOT_COUNT):
        u.extend(
            [
                gr.update(value=""),
                gr.update(value=None),
                gr.update(choices=[], value=[]),
                gr.update(value=""),
                gr.update(value=""),
                gr.update(visible=False),
            ]
        )
    return u


def _build_counts_str(
    override: bool,
    *nums: object,
) -> str | None:
    if not override:
        return None
    if len(nums) != len(ALLOWED_TYPES):
        return None
    parts: list[str] = []
    for k, v in zip(ALLOWED_TYPES, nums, strict=True):
        try:
            iv = int(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            iv = 0
        parts.append(f"{k}={iv}")
    return ",".join(parts)


def _pick_folder(initial_dir: str | None = None) -> str:
    """Open a native folder picker on the local machine.

    Returns the absolute path that the user selected, or ``""`` if they
    cancelled / the picker is unavailable (e.g. no display in a headless env).
    Tkinter ships with CPython on Windows, so this works out of the box for the
    local-only Gradio deployment we ship.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        return ""
    try:
        root = tk.Tk()
    except Exception:
        return ""
    chosen: str = ""
    try:
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        try:
            root.update()
        except Exception:
            pass
        init: str | None = None
        if initial_dir:
            try:
                cand = Path(initial_dir).expanduser().resolve()
                if cand.is_dir():
                    init = str(cand)
            except Exception:
                init = None
        chosen = filedialog.askdirectory(title="选择文件夹", initialdir=init or "") or ""
    finally:
        try:
            root.destroy()
        except Exception:
            pass
    if not chosen:
        return ""
    try:
        return str(Path(chosen).expanduser().resolve())
    except Exception:
        return chosen


def _resolve_root_paths(root_str: str, output_dest_str: str = "") -> list[Path]:
    """Resolve the user-facing root + output destination into existing dirs.

    Order matters: the first directory wins for ambiguity (e.g. a product
    with the same name exists in both). The list is de-duplicated by
    case-insensitive path. Empty / non-existent entries are dropped silently.
    """
    out: list[Path] = []
    seen: set[str] = set()
    for raw in (root_str, output_dest_str):
        if not raw or not str(raw).strip():
            continue
        try:
            p = Path(str(raw).strip()).expanduser().resolve()
        except Exception:
            continue
        if not p.is_dir():
            continue
        key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _resolve_product_dir(
    root_str: str, output_dest_str: str, product_name: str | None
) -> Path | None:
    name = (product_name or "").strip()
    if not name:
        return None
    for r in _resolve_root_paths(root_str, output_dest_str):
        cand = r / name
        if cand.is_dir():
            return cand
    return None


def _discover_products_across(
    root_str: str, output_dest_str: str = ""
) -> list[Path]:
    """Union of product folders discovered under root + output destination."""
    out: list[Path] = []
    seen: set[str] = set()
    for r in _resolve_root_paths(root_str, output_dest_str):
        for p in discover_under_root(r):
            key = str(p.resolve()).lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
    return out


def _effective_output_root(
    root_str: str, output_dest_str: str, default_root_str: str
) -> Path:
    """Return where new ZIP extractions and outputs should be written.

    Preference: ``output_dest_str`` (created if missing) > ``root_str`` (must
    exist) > ``default_root_str``. The returned directory is guaranteed to
    exist when this returns.
    """
    s = (output_dest_str or "").strip()
    if s:
        p = Path(s).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p
    r = (root_str or "").strip() or default_root_str
    p = Path(r).expanduser().resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def refresh_products(root_str: str, output_dest_str: str = ""):
    # Default to the exe's own folder (or repo root in dev), NEVER PROJECT_ROOT —
    # the latter resolves to the PyInstaller _MEI temp dir when frozen and gets
    # wiped on exit. See gui/config_store._looks_like_pyinstaller_tempdir.
    default_root = str(app_workdir())
    roots = _resolve_root_paths(root_str or default_root, output_dest_str)
    if not roots:
        return gr.update(choices=[], value=None), "项目根目录与输出目录均不存在"
    prods = _discover_products_across(root_str or default_root, output_dest_str)
    names = [p.name for p in prods]
    label = " + ".join(str(r) for r in roots)
    return (
        gr.update(choices=names, value=names[0] if names else None),
        f"已发现 **{len(names)}** 个商品文件夹（扫描：{label}）",
    )


def _cell_to_str(v: Any) -> str:
    """Normalise a Gradio Dataframe cell value to ``str``.

    Pandas backs the default Dataframe with object dtype and uses ``NaN`` for
    empty cells; ``str(NaN)`` is the literal ``"nan"`` which would otherwise be
    treated as a real value. Also strips wrapping double-quotes so Windows'
    「Copy as path」 (which produces ``"C:\foo\bar.zip"``) works unchanged.
    """
    if v is None:
        return ""
    try:
        import math

        if isinstance(v, float) and math.isnan(v):
            return ""
    except Exception:
        pass
    s = str(v).strip()
    if s.lower() in {"nan", "none", "null"}:
        return ""
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        s = s[1:-1].strip()
    return s


def _sniff_xls_kind(path: Path) -> str:
    """Best-effort detect what a `.xls`-named file actually is.

    Returns one of: ``"biff"`` (real binary Excel), ``"html"`` (HTML table
    saved with .xls extension — a very common shape for backend exports), or
    ``"text"`` (CSV/TSV in disguise).
    """
    try:
        head = path.open("rb").read(2048)
    except OSError:
        return "biff"
    if not head:
        return "biff"
    if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "biff"  # OLE2 compound document → real BIFF .xls
    sniff = head.lstrip().lower()
    if sniff.startswith((b"<!doctype", b"<html", b"<table", b"<?xml", b"<meta")):
        return "html"
    if sniff.startswith(b"<"):
        return "html"
    return "text"


def _read_batch_file(filepath: str | None) -> tuple[list[list[str]], str]:
    """Parse an Excel (.xlsx/.xlsm/.xls) or CSV file into batch rows.

    Returns ``(rows, message)`` where ``rows`` is ``[[path, title], ...]`` and
    ``message`` describes outcome (count loaded, columns picked, errors).

    ``.xls`` is special: backend exports often save HTML tables with the
    ``.xls`` extension, so we sniff the file header before deciding whether to
    use ``xlrd`` (real BIFF), ``read_html`` (HTML disguise), or ``read_csv``.
    """
    if not filepath:
        return [], "未提供文件"
    p = Path(str(filepath)).expanduser()
    if not p.is_file():
        return [], f"文件不存在: {p}"
    try:
        import pandas as pd
    except Exception as e:
        return [], f"未安装 pandas: {e}"
    ext = p.suffix.lower()

    df = None
    attempts: list[str] = []

    def _try(label: str, fn):
        nonlocal df
        if df is not None:
            return
        try:
            res = fn()
        except Exception as e:
            attempts.append(f"{label} 失败: {e}")
            return
        if isinstance(res, list):
            res = res[0] if res else None
        if res is None or res.empty:
            attempts.append(f"{label}: 空表")
            return
        df = res

    if ext == ".csv":
        _try("CSV (utf-8)", lambda: pd.read_csv(p, dtype=str, header=0, keep_default_na=False))
        _try("CSV (utf-8-sig)", lambda: pd.read_csv(p, dtype=str, header=0, keep_default_na=False, encoding="utf-8-sig"))
        _try("CSV (gbk)", lambda: pd.read_csv(p, dtype=str, header=0, keep_default_na=False, encoding="gbk"))
        _try("TSV (\\t)", lambda: pd.read_csv(p, dtype=str, header=0, keep_default_na=False, sep="\t"))
    elif ext in {".xlsx", ".xlsm"}:
        _try("openpyxl", lambda: pd.read_excel(p, dtype=str, header=0, engine="openpyxl"))
    elif ext == ".xls":
        kind = _sniff_xls_kind(p)
        if kind == "html":
            _try("HTML 表格 (.xls 伪装)", lambda: pd.read_html(p, header=0, encoding="utf-8"))
            _try("HTML 表格 (gbk)", lambda: pd.read_html(p, header=0, encoding="gbk"))
        elif kind == "text":
            _try("CSV (.xls 实为文本)", lambda: pd.read_csv(p, dtype=str, header=0, keep_default_na=False))
            _try("TSV (.xls 实为文本)", lambda: pd.read_csv(p, dtype=str, header=0, keep_default_na=False, sep="\t"))
        _try("xlrd (二进制 BIFF)", lambda: pd.read_excel(p, dtype=str, header=0, engine="xlrd"))
        _try("HTML 表格 (兜底)", lambda: pd.read_html(p, header=0))
    else:
        return [], f"不支持的文件类型: {ext}（仅 .xlsx / .xlsm / .xls / .csv）"

    if df is None:
        return [], "读取失败：\n" + "\n".join(f"· {a}" for a in attempts)

    df = df.fillna("")
    if df.shape[1] < 1:
        return [], "表格至少需要 1 列"
    rows_out: list[list[str]] = []
    for raw in df.values.tolist():
        path_s = _cell_to_str(raw[0]) if len(raw) > 0 else ""
        title_s = _cell_to_str(raw[1]) if len(raw) > 1 else ""
        if not path_s:
            continue
        rows_out.append([path_s, title_s])
    cols = list(df.columns)[:2]
    return rows_out, f"已读入 **{len(rows_out)}** 行（取自列：{cols}）"


def _write_batch_xlsx(rows: list[list[str]], dest: Path) -> Path:
    """Write ``rows`` as a 2-column xlsx (``ZIP/文件夹路径`` | ``商品标题``)."""
    import pandas as pd

    pdf = pd.DataFrame(rows or [["", ""]], columns=["ZIP/文件夹路径", "商品标题"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    pdf.to_excel(dest, index=False, engine="openpyxl")
    return dest


def _parse_batch_df(df: Any) -> list[tuple[str, str]]:
    """Return ``[(path_or_zip, title), ...]`` from a Gradio Dataframe.

    Handles every value type the Gradio Dataframe component can yield in 4.x:
    ``pandas.DataFrame`` (the default), ``list[list[Any]]``, ``dict`` with a
    ``"data"`` / ``"values"`` key, ``numpy.ndarray``, or ``None``.
    """
    rows: list[tuple[str, str]] = []
    if df is None:
        return rows
    data: Any = df
    if isinstance(data, dict):
        data = data.get("data", data.get("values", []))
    try:
        import pandas as _pd

        if isinstance(data, _pd.DataFrame):
            data = data.values.tolist()
    except Exception:
        pass
    if hasattr(data, "tolist") and not isinstance(data, (list, tuple)):
        try:
            data = data.tolist()  # numpy.ndarray
        except Exception:
            pass
    if not isinstance(data, (list, tuple)):
        return rows
    for row in data:
        if row is None:
            continue
        if not isinstance(row, (list, tuple)):
            continue
        if len(row) < 1:
            continue
        path_s = _cell_to_str(row[0])
        title_s = _cell_to_str(row[1]) if len(row) > 1 else ""
        if not path_s:
            continue
        rows.append((path_s, title_s))
    return rows


def _candidate_label(path: Path, score: float, reason: str) -> str:
    return f"{path.name}  | score={score:g}  | {reason}"


def _candidate_from_label(product_dir: Path, label: str) -> Path | None:
    name = str(label or "").split("|", 1)[0].strip()
    if not name:
        return None
    for p in collect_reference_paths(product_dir):
        if p.name == name:
            return p
    return None


def _size_review_rows(product_dirs: list[Path]) -> list[list[str]]:
    rows: list[list[str]] = []
    for pdir in product_dirs:
        picked = ensure_auto_size_ref(pdir)
        if picked is None:
            rows.append([pdir.name, "", "未找到可用图片"])
            continue
        source = "auto"
        try:
            from src.size_ref import load_selected_size_ref

            rec = load_selected_size_ref(pdir) or {}
            source = str(rec.get("source") or "auto")
        except Exception:
            pass
        rows.append([pdir.name, str(picked.path.resolve()), f"{picked.path.name} ({source}, score={picked.score:g})"])
    return rows


def _review_product_dirs_from_tasklist(
    root_str: str,
    output_dest_str: str,
    tasklist_path_str: str,
    default_root: str,
) -> list[Path]:
    path = Path((tasklist_path_str or "").strip()).expanduser()
    if not path.is_file():
        return []
    try:
        rows = load_tasklist(path)
        root_path = _effective_output_root(root_str, output_dest_str, default_root)
    except Exception:
        return []
    out: list[Path] = []
    seen: set[str] = set()
    for r in rows:
        raw = Path(str(r.path or "")).expanduser()
        pdir: Path | None = None
        if raw.is_dir():
            pdir = raw.resolve()
        elif raw.is_file() and raw.suffix.lower() == ".zip":
            name = (r.title or raw.stem).strip() or raw.stem
            pdir = (root_path / name).resolve()
            if not pdir.is_dir():
                continue
        if pdir is None or not pdir.is_dir():
            continue
        key = str(pdir)
        if key not in seen:
            out.append(pdir)
            seen.add(key)
    return out


def _build_custom_ref_paths(
    product_dir: Path,
    slot_type: str,
    slot_idx: int,
    selected: list | None,
) -> list[Path]:
    """Resolve combined CheckboxGroup picks into absolute file paths.

    Choices are formatted as ``refs/<name>`` or ``output/<name>``; the slot's
    own output image is always skipped to avoid self-referencing.
    """
    paths: list[Path] = []
    refs_dir = product_dir / "refs"
    out_dir = product_dir / "output"
    self_names = {
        _output_filename(slot_type, slot_idx, ".jpg"),
        _output_filename(slot_type, slot_idx, ".jpeg"),
        _output_filename(slot_type, slot_idx, ".png"),
    }
    for raw in selected or []:
        s = str(raw) if raw is not None else ""
        if s.startswith(_REFS_PREFIX):
            p = refs_dir / s[len(_REFS_PREFIX):]
        elif s.startswith(_OUT_PREFIX):
            name = s[len(_OUT_PREFIX):]
            if name in self_names:
                continue
            p = out_dir / name
        else:
            continue
        if p.is_file():
            paths.append(p.resolve())
    return paths


@dataclass
class GenerateTabResult:
    root_in: gr.Textbox
    product_dd: gr.Dropdown
    status_refresh: gr.Markdown
    title_in: gr.Textbox
    gallery: gr.Gallery
    slot_state: gr.State
    initial_refresh: Any
    initial_load_outputs: list[Any]


def build_generate_tab() -> GenerateTabResult:
    # Persist the two top-of-page directory text boxes between launches so
    # the user doesn't have to retype them every time. Falls back to the
    # exe's own folder (app_workdir) if nothing was saved yet — NEVER
    # PROJECT_ROOT, which resolves to the PyInstaller _MEI temp dir when
    # frozen and gets deleted on exit (taking the user's work with it).
    saved_root, saved_dest = load_saved_paths()
    default_root = saved_root or str(app_workdir())
    default_dest = saved_dest

    dc = DEFAULT_COUNTS
    count_inputs: list[gr.Number] = []
    slot_caps: list[gr.Markdown] = []
    slot_imgs: list[gr.Image] = []
    slot_combined_cg: list[gr.CheckboxGroup] = []
    slot_notes: list[gr.Textbox] = []
    slot_prompts: list[gr.Textbox] = []
    slot_btns: list[gr.Button] = []
    slot_cols: list[gr.Column] = []

    def _counts_str_from_meta(meta: dict | None) -> str | None:
        """Build a CLI-style ``main=1,scene=2,…`` string from the run meta.

        Used by the sync helper so ``compute_missing_slots`` measures
        completeness against EXACTLY the counts the just-finished run used,
        even when the user toggled "启用张数覆盖" on the page.
        """
        if not isinstance(meta, dict):
            return None
        eff = meta.get("counts_effective")
        if not isinstance(eff, dict) or not eff:
            return None
        parts = []
        for k, v in eff.items():
            try:
                parts.append(f"{k}={int(v)}")
            except (TypeError, ValueError):
                continue
        return ",".join(parts) if parts else None

    def _sync_batch_xlsx_after_meta(
        tasklist_path_str: str,
        product_dir: Path,
        meta: dict | None,
        settings,
        *,
        retried_slots: set[str] | None = None,
    ) -> str:
        """Reconcile both batch xlsx files after a single product just ran.

        Called from EVERY entry point that writes ``output/meta.json`` —
        single-product 开始, per-slot 重做这张, 一键重跑失败图片, batch
        runner — so the failure log and task list always agree with what's
        actually on disk.

        Behaviour:

        * Auto-removes failure-log rows whose slot is now ``status=ok`` in
          the meta (the user requested behaviour: "if the retry succeeds,
          the failure record disappears").
        * Auto-(re)-records failure-log rows for slots that are still
          ``status=error`` (so a re-failure stays visible).
        * Updates the matching task-list row's status / 备注 / processed_at
          using ``compute_missing_slots`` against the run's actual counts.

        Returns a one-line log message (empty if nothing got synced —
        product not in the task list, or files locked, etc.).
        """
        ts = (tasklist_path_str or "").strip()
        if not ts:
            return ""
        try:
            tasklist_path = Path(ts).expanduser()
        except Exception:
            return ""
        flp = derive_failure_log_path(tasklist_path)

        # ---- Failure log update -----------------------------------------
        fl_msg = ""
        try:
            failure_rows = load_failure_log(flp)
            n_before = len(failure_rows)
            touched = merge_failures_from_meta(
                failure_rows, product_dir=product_dir, meta=meta
            )
            # If the caller just retried specific slot(s) (per-slot 重做这张),
            # flip rows that ACTUALLY got an attempt-and-failed (status=error
            # in meta) to 重跑仍失败. Slots that didn't run (cancel before
            # attempt, counts mismatch, etc.) stay 待重跑 so the next
            # retry pass picks them up.
            n_marked = 0
            if retried_slots:
                meta_err_slots: set[str] = set()
                for im in (meta.get("images") or []) if isinstance(meta, dict) else []:
                    if not isinstance(im, dict):
                        continue
                    if im.get("status") != "error":
                        continue
                    t_name = str(im.get("type") or "").strip()
                    try:
                        t_idx = int(im.get("index") or 0)
                    except (TypeError, ValueError):
                        continue
                    if t_name and t_idx > 0:
                        meta_err_slots.add(f"{t_name}_{t_idx:02d}")
                actually_failed = set(retried_slots) & meta_err_slots
                if actually_failed:
                    n_marked = mark_just_retried_failures(
                        failure_rows,
                        product_dir=product_dir,
                        retried_slots=actually_failed,
                    )
            if touched or n_marked:
                save_failure_log(flp, failure_rows)
                n_after = len(failure_rows)
                delta = n_after - n_before
                if delta < 0:
                    fl_msg = f"失败记录 -{-delta} 条（已修复）"
                elif delta > 0:
                    fl_msg = f"失败记录 +{delta} 条（新失败）"
                elif n_marked:
                    fl_msg = f"失败记录 {n_marked} 行 → 重跑仍失败"
                else:
                    fl_msg = "失败记录已刷新"
        except FileNotFoundError:
            pass
        except PermissionError:
            fl_msg = f"⚠️ 失败记录被占用（{flp.name}），未同步"
        except Exception as e:
            fl_msg = f"⚠️ 失败记录写入失败: {e}"

        # ---- Task list update -------------------------------------------
        tl_msg = ""
        if not tasklist_path.is_file():
            return f"📒 已同步 {fl_msg}".strip() if fl_msg else ""
        try:
            task_rows = load_tasklist(tasklist_path)
        except PermissionError:
            tl_msg = f"⚠️ 任务清单被占用（{tasklist_path.name}），未同步"
            task_rows = None
        except Exception as e:
            tl_msg = f"⚠️ 任务清单读取失败: {e}"
            task_rows = None

        if task_rows is not None:
            pdir_resolved = str(product_dir.resolve()).lower()
            pdir_name_lower = product_dir.name.lower()
            matched: list[int] = []
            for i, tr in enumerate(task_rows):
                if not tr.path.strip():
                    continue
                p = Path(tr.path).expanduser()
                # Folder-row: the row literally points at product_dir.
                try:
                    if str(p.resolve()).lower() == pdir_resolved:
                        matched.append(i)
                        continue
                except OSError:
                    pass
                # Zip-row: the row points at a .zip whose extracted folder
                # is named after the zip stem. Match by stem so we hit the
                # one row whose zip would unpack to *this* product_dir,
                # not every zip row in the file.
                if p.suffix.lower() == ".zip" and p.stem.lower() == pdir_name_lower:
                    matched.append(i)

            if matched:
                counts_for_compute = _counts_str_from_meta(meta)
                try:
                    miss, n_ok_in_exp, n_expected, was_cancelled = (
                        compute_missing_slots(
                            product_dir, settings, counts_for_compute
                        )
                    )
                except Exception:
                    miss, n_ok_in_exp, n_expected, was_cancelled = (
                        [], 0, 0, False
                    )
                # The current run's meta might have ``cancelled: true`` from
                # the user clicking 终止 mid-flight — prefer that over what
                # was on disk previously.
                if isinstance(meta, dict) and meta.get("cancelled"):
                    was_cancelled = True

                # n_expected==0 means counts resolved to nothing for this
                # product (counts.yaml all zeros / weird config) — leave
                # the row's existing 状态 alone instead of stamping a
                # bogus 部分完成.
                new_status: str | None = None
                note = ""
                if was_cancelled and miss:
                    # Mirror the batch flow's STATUS_CANCELLED so 续跑「已终止」
                    #行 picks this up next time. Only when slots are still
                    # missing — if the user cancelled but the previous run
                    # already had every expected slot ok, the row is just done.
                    new_status = STATUS_CANCELLED
                    note = (
                        f"用户终止：ok={n_ok_in_exp}/{n_expected}, 缺 {len(miss)} 张 "
                        f"| 输出目录: {product_dir.name}"
                    )
                elif n_expected > 0 and not miss:
                    new_status = STATUS_DONE
                    note = (
                        f"已完成: ok={n_ok_in_exp}/{n_expected} "
                        f"| 输出目录: {product_dir.name}"
                    )
                elif miss:
                    new_status = STATUS_PARTIAL
                    note = (
                        f"还缺 {len(miss)} 张: ok={n_ok_in_exp}/{n_expected} "
                        f"| 输出目录: {product_dir.name}"
                    )

                if new_status is not None:
                    for mi in matched:
                        mark_row(task_rows, mi, status=new_status, note=note)
                    try:
                        save_tasklist(tasklist_path, task_rows)
                        tl_msg = f"任务清单 {len(matched)} 行 → {new_status}"
                    except PermissionError:
                        tl_msg = f"⚠️ 任务清单被占用（{tasklist_path.name}），未保存"
                    except Exception as e:
                        tl_msg = f"⚠️ 任务清单写入失败: {e}"

        bits = [m for m in (fl_msg, tl_msg) if m]
        if not bits:
            return ""
        return "📒 已同步: " + " ｜ ".join(bits)

    def _yield_run_outputs(
        product_dir: Path,
        meta: dict | None,
        err: str | None,
        settings,
        *,
        empty_state: list,
        empty_slots: list,
        recent_logs: list[str] | None = None,
        tasklist_path_str: str | None = None,
    ):
        recent_logs = list(recent_logs or [])
        if err:
            append_run(
                product=product_dir.name,
                product_path=str(product_dir.resolve()),
                status="error",
                counts_total=0,
                provider=settings.image_provider,
                error=err,
            )
            recent_logs.append(f"❌ {err}")
            yield [], _render_log(recent_logs), "", [], empty_state, *empty_slots
            return
        assert meta is not None
        n_ok = sum(1 for im in meta.get("images", []) if im.get("status") == "ok")
        n_err = sum(1 for im in meta.get("images", []) if im.get("status") == "error")
        was_cancelled = bool(meta.get("cancelled"))
        if n_err:
            for im in meta.get("images", []):
                if im.get("status") == "error":
                    nm = f"{im.get('type')}_{int(im.get('index', 0)):02d}"
                    recent_logs.append(f"❌ {nm} 失败: {im.get('error', '未知错误')}")
        if was_cancelled:
            summary_icon = "⏹"
            summary_word = "已终止"
        elif n_err == 0:
            summary_icon = "✅"
            summary_word = "完成"
        else:
            summary_icon = "⚠️"
            summary_word = "完成（部分失败）"
        recent_logs.append(
            f"{summary_icon} {summary_word}: 成功 {n_ok} 张, 失败 {n_err} 张。"
            f"输出目录: {product_dir / 'output'}"
        )
        # Mirror the batch flow: cancelled runs go to history as
        # ``cancelled`` so the 历史记录 page can distinguish them from
        # natural partial-failure runs.
        if was_cancelled:
            hist_status = "cancelled"
            hist_err = f"用户终止；ok={n_ok}, err={n_err}"
        elif n_err == 0:
            hist_status = "ok"
            hist_err = None
        else:
            hist_status = "partial"
            hist_err = f"{n_err} errors"
        append_run(
            product=product_dir.name,
            product_path=str(product_dir.resolve()),
            status=hist_status,
            counts_total=int(meta.get("counts_total", 0)),
            provider=settings.image_provider,
            error=hist_err,
        )
        # Sync the failure log + task list xlsx so a per-slot 重做这张 (or
        # a single-product 开始生成) success makes the matching row vanish
        # from 失败记录, and the row's 状态 column reflects current truth.
        if tasklist_path_str:
            sync_msg = _sync_batch_xlsx_after_meta(
                tasklist_path_str, product_dir, meta, settings
            )
            if sync_msg:
                recent_logs.append(sync_msg)

        imgs = gallery_images_from_meta(product_dir)
        st, slot_upd = _slot_state_and_updates_full(product_dir)
        preview = _preview_gallery_items(product_dir)
        yield imgs, _render_log(recent_logs), "\n".join(imgs) if imgs else "", preview, st, *slot_upd

    def run_one(
        root_str: str,
        output_dest_str: str,
        product_name: str | None,
        title_text: str,
        override_counts: bool,
        regen_str: str,
        user_note: str,
        n_main: float | int | None,
        n_scene: float | int | None,
        n_multi: float | int | None,
        n_size: float | int | None,
        n_detail: float | int | None,
        n_angle: float | int | None,
        n_material: float | int | None,
        tasklist_path_str: str = "",
    ):
        cancel_evt = _BUSY.try_acquire("single", str(product_name or ""))
        if cancel_evt is None:
            gr.Warning(_BUSY.busy_message())
            return
        try:
            yield from _run_one_inner(
                root_str,
                output_dest_str,
                product_name,
                title_text,
                override_counts,
                regen_str,
                user_note,
                n_main,
                n_scene,
                n_multi,
                n_size,
                n_detail,
                n_angle,
                n_material,
                cancel_evt,
                tasklist_path_str,
            )
        finally:
            _BUSY.release()

    def _run_one_inner(
        root_str: str,
        output_dest_str: str,
        product_name: str | None,
        title_text: str,
        override_counts: bool,
        regen_str: str,
        user_note: str,
        n_main: float | int | None,
        n_scene: float | int | None,
        n_multi: float | int | None,
        n_size: float | int | None,
        n_detail: float | int | None,
        n_angle: float | int | None,
        n_material: float | int | None,
        cancel_evt: threading.Event,
        tasklist_path_str: str = "",
    ):
        empty_state = [None] * SLOT_COUNT
        empty_slots = _empty_slot_updates_full()
        product_dir = _resolve_product_dir(root_str or default_root, output_dest_str, product_name)
        if product_dir is None:
            yield [], "请选择有效的商品文件夹（在项目根目录或输出目录中找不到）", "", [], empty_state, *empty_slots
            return
        root = product_dir.parent
        _write_title_file(product_dir, title_text)
        if not (product_dir / TITLE_FILE).is_file():
            yield [], f"请填写商品标题（将写入 {TITLE_FILE}）: {product_dir}", "", [], empty_state, *empty_slots
            return

        settings = effective_settings()
        if settings.image_provider in ("xais", "shiyun", "gemini", "doubao") and not (settings.image_api_key or "").strip():
            yield [], "请先在「设置」中填写 API Key（IMAGE_API_KEY）。", "", [], empty_state, *empty_slots
            return

        counts_arg = _build_counts_str(
            bool(override_counts),
            n_main,
            n_scene,
            n_multi,
            n_size,
            n_detail,
            n_angle,
            n_material,
        )
        regen_arg = (regen_str or "").strip() or None
        note_arg = (user_note or "").strip() or None

        schedule: list[tuple[str, int]] = []
        try:
            from gui.paths import resolved_config_yaml as _rcy
            from gui.runner import parse_regen_targets
            from src.counts_config import load_resolved_counts

            _resolved = load_resolved_counts(
                product_dir=product_dir,
                cli_counts=counts_arg,
                config_path=_rcy(),
                env_output_size=settings.output_size,
            )
            schedule = [
                (t, i)
                for t in TYPE_ORDER
                for i in range(1, int(_resolved.image_counts.get(t, 0)) + 1)
            ]
            if regen_arg:
                regen_set = parse_regen_targets(regen_arg)
                if regen_set:
                    schedule = [pair for pair in schedule if pair in regen_set]
        except Exception:
            schedule = []

        q: queue.Queue = queue.Queue()
        holder: dict[str, Any] = {}

        def worker() -> None:
            sink_id = _attach_log_sink(q)
            try:

                def cb(type_name: str, done: int, total: int) -> None:
                    q.put(("tick", type_name, done, total))

                holder["meta"] = run_generation(
                    project_root=root,
                    product_dir=product_dir,
                    counts_str=counts_arg,
                    regen_str=regen_arg,
                    user_note=note_arg,
                    settings=settings,
                    progress_callback=cb,
                    cancel_event=cancel_evt,
                )
            except BaseException as e:  # noqa: BLE001 - never silently die in worker thread
                holder["exc"] = e
            finally:
                _detach_log_sink(sink_id)
                q.put(("done", None, None, None))

        worker_t = threading.Thread(target=worker, daemon=True)
        worker_t.start()
        recent_logs: list[str] = ["已启动，正在调用模型（首张图通常需 30~60 秒）"]
        last_status = recent_logs[0]
        cur_marker: tuple[str, int] | None = schedule[0] if schedule else None
        imgs0 = gallery_images_from_meta(product_dir)
        st0, upd0 = _slot_state_and_updates_full(product_dir, marker=cur_marker, schedule=schedule)
        yield (
            imgs0,
            _render_log(recent_logs),
            "\n".join(imgs0) if imgs0 else "",
            _preview_gallery_items(product_dir),
            st0,
            *upd0,
        )
        heartbeat = 0
        done_count = 0
        last_event_t = time.monotonic()
        stuck_warned = False
        done_flag = False
        while True:
            try:
                ev = q.get(timeout=2.0)
            except queue.Empty:
                if not worker_t.is_alive() and q.empty():
                    holder.setdefault(
                        "exc",
                        RuntimeError("生成线程异常退出（worker 已停止但未上报完成）。"),
                    )
                    recent_logs.append("❌ 生成线程异常退出，未收到完成信号。")
                    break
                heartbeat += 1
                idle_s = time.monotonic() - last_event_t
                if idle_s > STUCK_WARN_SEC and not stuck_warned:
                    recent_logs.append(
                        f"⚠️  已 {int(idle_s)} 秒未收到任何进度，可能卡住；检查终端 / 网络后可重启或中止。"
                    )
                    stuck_warned = True
                dots = "." * (1 + heartbeat % 4)
                cur_marker = schedule[done_count] if schedule and done_count < len(schedule) else None
                imgs_hb = gallery_images_from_meta(product_dir)
                st_hb, upd_hb = _slot_state_and_updates_full(
                    product_dir, marker=cur_marker, schedule=schedule
                )
                yield (
                    imgs_hb,
                    _render_log(recent_logs, f"{last_status} {dots}"),
                    "\n".join(imgs_hb) if imgs_hb else "",
                    _preview_gallery_items(product_dir),
                    st_hb,
                    *upd_hb,
                )
                continue
            # one or more events arrived: drain everything currently buffered
            events = [ev]
            while True:
                try:
                    events.append(q.get_nowait())
                except queue.Empty:
                    break
            last_event_t = time.monotonic()
            stuck_warned = False
            for e in events:
                kind = e[0] if e else None
                if kind == "done":
                    done_flag = True
                    continue
                if kind == "tick":
                    _, tn, done, tot = e
                    if tot and tot > 0:
                        done_count = int(done)
                        last_status = f"生成中… ({done}/{tot}) {tn}"
                    continue
                if kind == "log":
                    _, lvl, txt = e
                    recent_logs.append(_fmt_log_line(lvl, txt))
                    lvl_u = (lvl or "").upper()
                    if lvl_u in {"WARNING", "WARN", "ERROR", "CRITICAL"}:
                        last_status = _fmt_log_line(lvl, txt)
                    continue
            cur_marker = schedule[done_count] if schedule and done_count < len(schedule) else None
            imgs = gallery_images_from_meta(product_dir)
            st, slot_upd = _slot_state_and_updates_full(
                product_dir, marker=cur_marker, schedule=schedule
            )
            preview = _preview_gallery_items(product_dir)
            yield imgs, _render_log(recent_logs, last_status), "\n".join(imgs) if imgs else "", preview, st, *slot_upd
            if done_flag:
                break

        exc = holder.get("exc")
        if exc is not None:
            err = _friendly_error(exc)
            append_run(
                product=product_dir.name,
                product_path=str(product_dir.resolve()),
                status="error",
                counts_total=0,
                provider=settings.image_provider,
                error=str(exc),
            )
            recent_logs.append(f"❌ {err}")
            yield [], _render_log(recent_logs), "", _preview_gallery_items(product_dir), empty_state, *empty_slots
            return

        yield from _yield_run_outputs(
            product_dir,
            holder.get("meta"),
            None,
            settings,
            empty_state=empty_state,
            empty_slots=empty_slots,
            recent_logs=recent_logs,
            tasklist_path_str=tasklist_path_str,
        )

    def run_batch(
        root_str: str,
        output_dest_str: str,
        tasklist_path_str: str,
        retry_failed: bool,
        retry_partial: bool,
        retry_cancelled: bool,
        override_counts: bool,
        user_note: str,
        n_main: float | int | None,
        n_scene: float | int | None,
        n_multi: float | int | None,
        n_size: float | int | None,
        n_detail: float | int | None,
        n_angle: float | int | None,
        n_material: float | int | None,
    ):
        cancel_evt = _BUSY.try_acquire("batch")
        if cancel_evt is None:
            gr.Warning(_BUSY.busy_message())
            return
        try:
            yield from _run_tasklist_inner(
                root_str,
                output_dest_str,
                tasklist_path_str,
                bool(retry_failed),
                bool(retry_partial),
                bool(retry_cancelled),
                override_counts,
                user_note,
                n_main,
                n_scene,
                n_multi,
                n_size,
                n_detail,
                n_angle,
                n_material,
                cancel_evt,
            )
        finally:
            _BUSY.release()

    def run_retry_failures(
        root_str: str,
        output_dest_str: str,
        tasklist_path_str: str,
        include_already_retried: bool,
        override_counts: bool,
        user_note: str,
        n_main: float | int | None,
        n_scene: float | int | None,
        n_multi: float | int | None,
        n_size: float | int | None,
        n_detail: float | int | None,
        n_angle: float | int | None,
        n_material: float | int | None,
    ):
        cancel_evt = _BUSY.try_acquire("retry_failures")
        if cancel_evt is None:
            gr.Warning(_BUSY.busy_message())
            return
        try:
            yield from _run_retry_failures_inner(
                root_str,
                output_dest_str,
                tasklist_path_str,
                bool(include_already_retried),
                override_counts,
                user_note,
                n_main,
                n_scene,
                n_multi,
                n_size,
                n_detail,
                n_angle,
                n_material,
                cancel_evt,
            )
        finally:
            _BUSY.release()

    def _resolve_one_input(
        path_s: str,
        title_s: str,
        root_path: Path,
    ) -> tuple[Path | None, str]:
        """Extract zip / validate folder for a single task row.

        Returns ``(product_dir, error_message)``. On success, ``product_dir`` is
        the resolved product folder and ``error_message`` is ``""``. On
        failure, ``product_dir`` is ``None`` and ``error_message`` is a short
        Chinese reason suitable for the row's 备注 column.
        """
        raw_p = Path(path_s).expanduser().resolve()
        is_zip = raw_p.is_file() and raw_p.suffix.lower() == ".zip"

        if is_zip:
            if not title_s:
                return None, "ZIP 未填商品标题"
            try:
                from gui.zip_util import extract_product_zip
                # Batch context: if the ZIP was already extracted before
                # (e.g. on an earlier interrupted run), silently reuse the
                # existing folder instead of failing loudly. The resume
                # logic in _run_tasklist_inner will then figure out which
                # images still need generating.
                pd = extract_product_zip(
                    raw_p, root_path, title_s, reuse_if_existing_valid=True
                )
            except Exception as e:
                return None, f"解压失败: {e}"
        else:
            if not raw_p.is_dir():
                return None, "路径既不是 ZIP 也不是目录"
            pd = raw_p

        if title_s:
            try:
                _write_title_file(pd, title_s)
            except OSError as e:
                return None, f"写入商品标题失败: {e}"
        if not (pd / TITLE_FILE).is_file():
            return None, "商品目录缺少 商品标题.txt"
        try:
            if not collect_reference_paths(pd):
                return None, "商品目录下没有参考图（refs/）"
        except OSError as e:
            return None, f"读取商品目录失败: {e}"
        return pd, ""

    def _run_tasklist_inner(
        root_str: str,
        output_dest_str: str,
        tasklist_path_str: str,
        retry_failed: bool,
        retry_partial: bool,
        retry_cancelled: bool,
        override_counts: bool,
        user_note: str,
        n_main: float | int | None,
        n_scene: float | int | None,
        n_multi: float | int | None,
        n_size: float | int | None,
        n_detail: float | int | None,
        n_angle: float | int | None,
        n_material: float | int | None,
        cancel_evt: threading.Event,
    ):
        empty_state = [None] * SLOT_COUNT
        empty_slots = _empty_slot_updates_full()
        dd_noop = gr.update()
        title_noop = gr.update()

        def _y(
            *,
            gallery_imgs=None,
            log_text: str = "",
            preview=None,
            slot_state=None,
            dd=None,
            title=None,
            df_rows=None,
            summary: str = "",
            current: str = "",
            failure_df_rows=None,
            failure_summary_text=None,
            slot_updates=None,
        ):
            """Build a yield tuple matching ``batch_outputs`` order.

            Empty defaults map to ``gr.update()`` (no-op) so the failure
            section / summary don't blink to blank during heartbeat ticks.
            """
            return (
                gallery_imgs if gallery_imgs is not None else [],
                log_text,
                preview if preview is not None else [],
                slot_state if slot_state is not None else empty_state,
                dd if dd is not None else dd_noop,
                title if title is not None else title_noop,
                df_rows if df_rows is not None else gr.update(),
                summary,
                current,
                failure_df_rows if failure_df_rows is not None else gr.update(),
                failure_summary_text if failure_summary_text is not None else gr.update(),
                *(slot_updates if slot_updates is not None else empty_slots),
            )

        # ---- Load task list ------------------------------------------------
        tasklist_path = Path((tasklist_path_str or "").strip()).expanduser()
        if not str(tasklist_path):
            yield _y(log_text="任务清单路径为空。请先在上方填写一个 .xlsx 路径。")
            return
        try:
            init_template_if_missing(tasklist_path)
            task_rows: list[TaskRow] = load_tasklist(tasklist_path)
        except PermissionError:
            yield _y(log_text=f"任务清单被占用（请先关闭 Excel 再点开始）：{tasklist_path}")
            return
        except Exception as e:
            yield _y(log_text=f"读取任务清单失败 {tasklist_path}: {e}")
            return

        # ---- Load failure log (sibling xlsx; auto-derived from tasklist) --
        failure_log_path = derive_failure_log_path(tasklist_path)
        try:
            init_failure_log_if_missing(failure_log_path)
            failure_rows: list[FailureRow] = load_failure_log(failure_log_path)
        except PermissionError:
            yield _y(log_text=f"失败记录文件被占用（请先关闭 Excel）：{failure_log_path}")
            return
        except Exception as e:
            # Non-fatal: a corrupt failure log shouldn't block a fresh run.
            failure_rows = []
            yield _y(log_text=f"⚠️ 失败记录读取失败 {failure_log_path}: {e}（本次开新的）")

        if not task_rows:
            yield _y(
                log_text=(
                    f"任务清单为空：{tasklist_path}\n"
                    "请先点上方「📋 打开 Excel 编辑」，在 A 列填路径、B 列填商品标题，保存关闭后再点开始。"
                ),
                df_rows=gr.update(value=[]),
                summary=format_summary(task_rows),
                failure_df_rows=gr.update(value=_capped_failure_df(failure_rows)),
                failure_summary_text=format_failure_summary(failure_rows),
            )
            return

        # ---- Validate settings + root path --------------------------------
        settings = effective_settings()
        if settings.image_provider in ("xais", "shiyun", "gemini", "doubao") and not (settings.image_api_key or "").strip():
            yield _y(
                log_text="请先在「设置」中填写 API Key。",
                df_rows=gr.update(value=_capped_tasklist_df(task_rows)),
                summary=format_summary(task_rows),
            )
            return

        try:
            root_path = _effective_output_root(root_str, output_dest_str, default_root)
        except Exception as e:
            yield _y(log_text=f"输出目录无法创建: {e}")
            return
        if not root_path.is_dir():
            yield _y(log_text=f"输出目录不存在: {root_path}")
            return

        counts_arg = _build_counts_str(
            bool(override_counts),
            n_main,
            n_scene,
            n_multi,
            n_size,
            n_detail,
            n_angle,
            n_material,
        )

        # ---- Decide which rows to actually run this pass ------------------
        actionable_indices = [
            i for i, r in enumerate(task_rows)
            if r.is_actionable(
                retry_failed=retry_failed,
                retry_partial=retry_partial,
                retry_cancelled=retry_cancelled,
            )
        ]
        n_jobs = len(actionable_indices)
        all_imgs: list[str] = []
        logs: list[str] = []
        last_loaded: Path | None = None

        # Initial yield: show table + summary so user sees what's queued
        # before the first row even starts.
        head_lines = [
            f"任务清单: {tasklist_path}",
            f"失败记录: {failure_log_path}",
            f"待处理 {n_jobs} 行 / 总 {len(task_rows)} 行（已完成的会自动跳过）",
        ]
        if not n_jobs:
            head_lines.append("没有需要处理的行。如要重做失败 / 部分完成的行，请勾选上方对应复选框再开始。")
        yield _y(
            log_text="\n".join(head_lines),
            df_rows=gr.update(value=_capped_tasklist_df(task_rows)),
            summary=format_summary(task_rows),
            failure_df_rows=gr.update(value=_capped_failure_df(failure_rows)),
            failure_summary_text=format_failure_summary(failure_rows),
        )
        if not n_jobs:
            return

        def _persist(extra_log: str | None = None) -> str:
            """Save tasklist to disk; return any warning to surface in the log."""
            try:
                save_tasklist(tasklist_path, task_rows)
                return ""
            except PermissionError:
                return (
                    f"⚠️  状态写入失败：{tasklist_path.name} 被占用（请关闭 Excel）；"
                    "本行的处理结果在内存里，但下次重启会需要重新跑。"
                )
            except Exception as e:
                return f"⚠️  状态写入失败: {e}"

        # ---- Global batch: prepare all rows, then one shared thread pool ----
        from gui.batch_flow import prepare_batch_jobs
        from gui.paths import resolved_config_yaml
        from gui.runner import run_batch_generation
        from src.counts_config import load_resolved_counts

        cfg_path = resolved_config_yaml()
        prep = prepare_batch_jobs(
            actionable_indices=actionable_indices,
            task_rows=task_rows,
            root_path=root_path,
            settings=settings,
            counts_arg=counts_arg,
            config_path=cfg_path,
            user_note=user_note,
            resolve_one_input=_resolve_one_input,
        )
        logs.extend(prep.logs)

        for row_idx, err_msg, log_line, md_suffix in prep.resolve_failed:
            mark_row(task_rows, row_idx, status=STATUS_FAILED, note=err_msg)
            warn = _persist()
            logs.append(log_line)
            if warn:
                logs.append(warn)
            yield _y(
                gallery_imgs=all_imgs,
                log_text="\n".join(logs),
                df_rows=gr.update(value=_capped_tasklist_df(task_rows)),
                summary=format_summary(task_rows),
                current=f"**当前任务**：{md_suffix}",
            )

        for row_idx, note, log_line, md_suffix in prep.skip_done:
            mark_row(task_rows, row_idx, status=STATUS_DONE, note=note)
            warn = _persist()
            if warn:
                logs.append(warn)
            yield _y(
                gallery_imgs=all_imgs,
                log_text="\n".join(logs),
                df_rows=gr.update(value=_capped_tasklist_df(task_rows)),
                summary=format_summary(task_rows),
                current=f"**当前任务**：{md_suffix}",
            )

        pending_jobs = prep.pending
        if not pending_jobs:
            pass
        elif cancel_evt.is_set():
            logs.append("⏹  收到终止信号，未启动全局并发生成。")
            yield _y(
                gallery_imgs=all_imgs,
                log_text="\n".join(logs),
                df_rows=gr.update(value=_capped_tasklist_df(task_rows)),
                summary=format_summary(task_rows),
                current="**当前任务**：已终止",
            )
        else:
            for job in pending_jobs:
                mark_row(task_rows, job.row_idx, status=STATUS_RUNNING, note="处理中…")
            warn = _persist()
            if warn:
                logs.append(warn)

            pdir_to_job = {j.product_dir.resolve(): j for j in pending_jobs}
            q: queue.Queue = queue.Queue()
            holder: dict[str, Any] = {}
            io_lock = threading.Lock()
            products_finished: set[str] = set()

            def _apply_product_result(job, meta: dict) -> bool:
                """Update task row / failure log for one finished product. Returns stop_batch."""
                nonlocal last_loaded
                product_dir = job.product_dir
                ji, row_idx = job.ji, job.row_idx
                n_err = sum(1 for im in meta.get("images", []) if im.get("status") == "error")
                n_ok = sum(1 for im in meta.get("images", []) if im.get("status") == "ok")
                was_cancelled_now = bool(meta.get("cancelled"))
                try:
                    still_missing, _, _, _ = compute_missing_slots(
                        product_dir, settings, counts_arg
                    )
                except Exception:
                    still_missing = []

                if was_cancelled_now or still_missing:
                    new_status = STATUS_CANCELLED if was_cancelled_now else STATUS_PARTIAL
                elif n_err == 0:
                    new_status = STATUS_DONE
                else:
                    new_status = STATUS_PARTIAL

                if was_cancelled_now:
                    note_text = (
                        f"已终止: ok={n_ok}, err={n_err}, 待续 {len(still_missing)} 张 "
                        f"| 输出目录: {product_dir.name}"
                    )
                else:
                    note_text = f"ok={n_ok}, err={n_err} | 输出目录: {product_dir.name}"

                if was_cancelled_now:
                    logs.append(
                        f"⏹  [{ji}/{job.n_jobs}] CANCELLED {product_dir.name} "
                        f"(ok={n_ok}, err={n_err}, 待续={len(still_missing)})"
                    )
                elif n_err == 0 and not still_missing:
                    logs.append(f"✅ [{ji}/{job.n_jobs}] OK {product_dir.name} (ok={n_ok})")
                else:
                    logs.append(
                        f"⚠️  [{ji}/{job.n_jobs}] PARTIAL {product_dir.name} "
                        f"(ok={n_ok}, err={n_err}, 待续={len(still_missing)})"
                    )

                if was_cancelled_now:
                    hist_status = "cancelled"
                    hist_err = f"{len(still_missing)} pending after ⏹"
                elif n_err == 0 and not still_missing:
                    hist_status = "ok"
                    hist_err = None
                else:
                    hist_status = "partial"
                    hist_err = f"{n_err} errors" if n_err > 0 else f"{len(still_missing)} pending"

                append_run(
                    product=product_dir.name,
                    product_path=str(product_dir),
                    status=hist_status,
                    counts_total=int(meta.get("counts_total", 0)),
                    provider=settings.image_provider,
                    error=hist_err,
                )
                mark_row(task_rows, row_idx, status=new_status, note=note_text)
                warn_p = _persist()
                if warn_p:
                    logs.append(warn_p)

                try:
                    touched = merge_failures_from_meta(
                        failure_rows, product_dir=product_dir, meta=meta
                    )
                    if touched:
                        save_failure_log(failure_log_path, failure_rows)
                except PermissionError:
                    logs.append(
                        f"⚠️  失败记录写入失败：{failure_log_path.name} 被占用，"
                        f"本商品的失败槽位仅在内存中（关掉 Excel 后下次保存会补回）。"
                    )
                except Exception as e:
                    logs.append(f"⚠️  失败记录写入失败: {e}")

                imgs = gallery_images_from_meta(product_dir)
                all_imgs.extend(imgs[:3])
                if (product_dir / "output").is_dir() and gallery_images_from_meta(product_dir):
                    last_loaded = product_dir
                return was_cancelled_now

            def worker() -> None:
                sink_id = _attach_log_sink(q)
                try:

                    def cb(pdir: Path, type_name: str, done: int, total: int) -> None:
                        q.put(("tick", str(pdir.resolve()), type_name, done, total))

                    def on_done(pdir: Path, meta: dict) -> None:
                        q.put(("product_done", str(pdir.resolve()), meta))

                    def on_err(pdir: Path, err: str) -> None:
                        q.put(("product_error", str(pdir.resolve()), err))

                    holder["results"] = run_batch_generation(
                        [j.spec for j in pending_jobs],
                        settings,
                        progress_callback=cb,
                        product_done_callback=on_done,
                        product_error_callback=on_err,
                        cancel_event=cancel_evt,
                    )
                except BaseException as e:  # noqa: BLE001
                    holder["exc"] = e
                finally:
                    _detach_log_sink(sink_id)
                    q.put(("done",))

            worker_t = threading.Thread(target=worker, daemon=True)
            worker_t.start()
            _conc = load_resolved_counts(
                product_dir=pending_jobs[0].product_dir,
                cli_counts=counts_arg,
                config_path=cfg_path,
                env_output_size=settings.output_size,
            ).generation.concurrency
            last_batch_log = (
                f"全局并发已启动：{len(pending_jobs)} 个商品，并行上限 {_conc} 路…"
            )
            logs.append(last_batch_log)
            yield _y(
                gallery_imgs=all_imgs,
                log_text="\n".join(logs),
                df_rows=gr.update(value=_capped_tasklist_df(task_rows)),
                summary=format_summary(task_rows),
                current=f"**当前任务**：全局并发生成 {len(pending_jobs)} 个商品…",
            )

            heartbeat = 0
            last_event_t = time.monotonic()
            stuck_warned = False
            batch_stopped = False
            done_flag = False

            while True:
                try:
                    ev = q.get(timeout=2.0)
                except queue.Empty:
                    if not worker_t.is_alive() and q.empty():
                        holder.setdefault(
                            "exc",
                            RuntimeError("生成线程异常退出（worker 已停止但未上报完成）。"),
                        )
                        break
                    heartbeat += 1
                    idle_s = time.monotonic() - last_event_t
                    if idle_s > STUCK_WARN_SEC and not stuck_warned:
                        logs.append(f"⚠️  全局批次: 已 {int(idle_s)} 秒无进度，可能卡住。")
                        stuck_warned = True
                    dots = "." * (1 + heartbeat % 4)
                    yield _y(
                        gallery_imgs=all_imgs,
                        log_text="\n".join([*logs, last_batch_log + dots]),
                        df_rows=gr.update(value=_capped_tasklist_df(task_rows)),
                        summary=format_summary(task_rows),
                        current="**当前任务**：全局并发生成中…",
                    )
                    continue

                events = [ev]
                while True:
                    try:
                        events.append(q.get_nowait())
                    except queue.Empty:
                        break
                last_event_t = time.monotonic()
                stuck_warned = False

                for e in events:
                    kind = e[0] if e else None
                    if kind == "done":
                        done_flag = True
                        continue
                    if kind == "tick":
                        _, pstr, tn, done, tot = e
                        job = pdir_to_job.get(Path(pstr))
                        if job and tot:
                            last_batch_log = (
                                f"[{job.ji}/{job.n_jobs}] {job.product_dir.name} "
                                f"{tn} {done}/{tot} (全局 {done}/{tot})"
                            )
                        continue
                    if kind == "product_done":
                        _, pstr, meta = e
                        job = pdir_to_job.get(Path(pstr))
                        if job is None:
                            continue
                        with io_lock:
                            stop = _apply_product_result(job, meta if isinstance(meta, dict) else {})
                        if stop:
                            batch_stopped = True
                        continue
                    if kind == "product_error":
                        _, pstr, err = e
                        job = pdir_to_job.get(Path(pstr))
                        if job is None or pstr in products_finished:
                            continue
                        with io_lock:
                            if pstr in products_finished:
                                continue
                            fe = (err or "")[:300]
                            logs.append(
                                f"❌ [{job.ji}/{job.n_jobs}] FAIL {job.product_dir.name}: {fe}"
                            )
                            mark_row(
                                task_rows,
                                job.row_idx,
                                status=STATUS_FAILED,
                                note=f"生成失败: {fe} | 输出目录: {job.product_dir.name}",
                            )
                            _persist()
                            products_finished.add(pstr)
                        continue
                    if kind == "log":
                        _, lvl, txt = e
                        logs.append(_fmt_log_line(lvl, txt))
                        continue

                yield _y(
                    gallery_imgs=all_imgs,
                    log_text="\n".join([*logs, last_batch_log]),
                    df_rows=gr.update(value=_capped_tasklist_df(task_rows)),
                    summary=format_summary(task_rows),
                    failure_df_rows=gr.update(value=_capped_failure_df(failure_rows)),
                    failure_summary_text=format_failure_summary(failure_rows),
                    current="**当前任务**：全局并发生成中…",
                )
                if done_flag:
                    break

            if holder.get("exc"):
                fe = _friendly_error(holder["exc"])
                logs.append(f"❌ 全局批次异常: {fe}")

            if batch_stopped:
                logs.append(
                    "⏹  本批因终止结束；下次点「开始」会自动续跑未完成行（默认勾选）。"
                )
                yield _y(
                    gallery_imgs=all_imgs,
                    log_text="\n".join(logs),
                    df_rows=gr.update(value=_capped_tasklist_df(task_rows)),
                    summary=format_summary(task_rows),
                    failure_df_rows=gr.update(value=_capped_failure_df(failure_rows)),
                    failure_summary_text=format_failure_summary(failure_rows),
                )
        # ---- Final yield: refresh dropdown + auto-load last successful product
        new_choices = [p.name for p in _discover_products_across(root_str or default_root, output_dest_str)]
        # Surface a clear hint so the user remembers they can click 重跑失败.
        n_failure_pending = sum(1 for r in failure_rows if r.retry_status == RETRY_PENDING)
        if n_failure_pending > 0:
            logs.append(
                f"📛 本批共记录 {n_failure_pending} 个待重跑的失败槽位 → "
                f"点下方「🔁 重跑全部失败图片」一键重试。"
            )
        if last_loaded is None or not last_loaded.is_dir():
            yield _y(
                gallery_imgs=all_imgs,
                log_text="\n".join(logs),
                df_rows=gr.update(value=_capped_tasklist_df(task_rows)),
                summary=format_summary(task_rows),
                dd=gr.update(choices=new_choices),
                current="**当前任务**：本批已结束",
                failure_df_rows=gr.update(value=_capped_failure_df(failure_rows)),
                failure_summary_text=format_failure_summary(failure_rows),
            )
            return

        title_text = _read_title_file(last_loaded)
        last_gallery = gallery_images_from_meta(last_loaded)
        last_preview = _preview_gallery_items(last_loaded)
        last_state, last_upd = _slot_state_and_updates_full(last_loaded)
        logs.append(
            f"📂 已自动加载「{last_loaded.name}」({len(last_gallery)} 张) 到下方逐张预览，"
            f"切换商品请用上方下拉；有问题的图点对应槽位的「重做这张」。"
        )
        yield _y(
            gallery_imgs=last_gallery,
            log_text="\n".join(logs),
            preview=last_preview,
            slot_state=last_state,
            dd=gr.update(choices=new_choices, value=last_loaded.name),
            title=gr.update(value=title_text),
            df_rows=gr.update(value=_capped_tasklist_df(task_rows)),
            summary=format_summary(task_rows),
            current="**当前任务**：本批已结束",
            failure_df_rows=gr.update(value=_capped_failure_df(failure_rows)),
            failure_summary_text=format_failure_summary(failure_rows),
            slot_updates=last_upd,
        )

    def _run_retry_failures_inner(
        root_str: str,
        output_dest_str: str,
        tasklist_path_str: str,
        include_already_retried: bool,
        override_counts: bool,
        user_note: str,
        n_main: float | int | None,
        n_scene: float | int | None,
        n_multi: float | int | None,
        n_size: float | int | None,
        n_detail: float | int | None,
        n_angle: float | int | None,
        n_material: float | int | None,
        cancel_evt: threading.Event,
    ):
        """One-click retry just the failed slots tracked in the failure log.

        Reads ``批量失败记录_*.xlsx``, groups待重跑 rows by product, then for
        each product calls ``run_generation`` with ``regen_str`` set to those
        slots only (e.g. ``main_01,scene_02``). After each product finishes,
        merges the new ``meta.json`` back into the failure log so successful
        slots flip to **已重跑成功** and persistent failures become **重跑仍失败**.
        """
        empty_state = [None] * SLOT_COUNT
        empty_slots = _empty_slot_updates_full()
        dd_noop = gr.update()
        title_noop = gr.update()

        def _y(
            *,
            gallery_imgs=None,
            log_text: str = "",
            preview=None,
            slot_state=None,
            dd=None,
            title=None,
            df_rows=None,
            summary=None,
            current=None,
            failure_df_rows=None,
            failure_summary_text=None,
            slot_updates=None,
        ):
            return (
                gallery_imgs if gallery_imgs is not None else [],
                log_text,
                preview if preview is not None else [],
                slot_state if slot_state is not None else empty_state,
                dd if dd is not None else dd_noop,
                title if title is not None else title_noop,
                df_rows if df_rows is not None else gr.update(),
                summary if summary is not None else gr.update(),
                current if current is not None else gr.update(),
                failure_df_rows if failure_df_rows is not None else gr.update(),
                failure_summary_text if failure_summary_text is not None else gr.update(),
                *(slot_updates if slot_updates is not None else empty_slots),
            )

        # ---- Resolve failure log + task list paths ------------------------
        tasklist_path = Path((tasklist_path_str or "").strip()).expanduser()
        if not str(tasklist_path):
            yield _y(log_text="任务清单路径为空，无法定位失败记录文件。")
            return
        failure_log_path = derive_failure_log_path(tasklist_path)
        try:
            failure_rows: list[FailureRow] = load_failure_log(failure_log_path)
        except PermissionError:
            yield _y(log_text=f"失败记录文件被占用：{failure_log_path}（请先关闭 Excel）")
            return
        except Exception as e:
            yield _y(log_text=f"读取失败记录失败 {failure_log_path}: {e}")
            return

        # Also load the task list so we can keep its 状态 column in sync
        # after each product's slots are retried (otherwise a row stays
        # 部分完成 even though all its slots are now OK on disk).
        task_rows: list[TaskRow] = []
        task_rows_loaded = False
        if tasklist_path.is_file():
            try:
                task_rows = load_tasklist(tasklist_path)
                task_rows_loaded = True
            except PermissionError:
                # Non-fatal: we'll still update the failure log, just skip
                # the task list write-back this run.
                yield _y(
                    log_text=(
                        f"⚠️  任务清单被占用（{tasklist_path.name}）；本次只更新失败记录，"
                        f"任务清单状态需要等下次开始批量时重算。"
                    ),
                    failure_df_rows=gr.update(value=_capped_failure_df(failure_rows)),
                    failure_summary_text=format_failure_summary(failure_rows),
                )
            except Exception as e:
                yield _y(log_text=f"⚠️ 任务清单读取失败 {tasklist_path}: {e}（仅更新失败记录）")

        # ---- Validate settings ---------------------------------------------
        settings = effective_settings()
        if settings.image_provider in ("xais", "shiyun", "gemini", "doubao") and not (settings.image_api_key or "").strip():
            yield _y(
                log_text="请先在「设置」中填写 API Key。",
                failure_df_rows=gr.update(value=_capped_failure_df(failure_rows)),
                failure_summary_text=format_failure_summary(failure_rows),
            )
            return

        try:
            root_path = _effective_output_root(root_str, output_dest_str, default_root)
        except Exception as e:
            yield _y(log_text=f"输出目录无法解析: {e}")
            return
        _ = root_path  # not strictly needed; product paths are absolute in failure log

        counts_arg = _build_counts_str(
            bool(override_counts),
            n_main,
            n_scene,
            n_multi,
            n_size,
            n_detail,
            n_angle,
            n_material,
        )

        groups = group_failures_by_product(
            failure_rows, include_already_retried=include_already_retried
        )
        if not groups:
            head = (
                "没有需要重跑的失败槽位。\n"
                "（如想重跑「已重跑成功」的行，请勾选上方对应的复选框。）"
            )
            yield _y(
                log_text=head,
                failure_df_rows=gr.update(value=_capped_failure_df(failure_rows)),
                failure_summary_text=format_failure_summary(failure_rows),
            )
            return

        n_groups = len(groups)
        total_slots = sum(len(items) for _, _, items in groups)
        all_imgs: list[str] = []
        # Cumulative tally — needed for the final 🏁 summary because under
        # the new auto-drop semantics, fixed rows vanish from failure_rows
        # so we can't recompute the count from the in-memory list at the
        # end. We accumulate as we go.
        cum_fixed = 0
        cum_still = 0
        logs: list[str] = [
            f"失败记录: {failure_log_path}",
            f"待重跑 {total_slots} 个槽位 / 涉及 {n_groups} 个商品",
        ]
        yield _y(
            log_text="\n".join(logs),
            failure_df_rows=gr.update(value=_capped_failure_df(failure_rows)),
            failure_summary_text=format_failure_summary(failure_rows),
        )

        last_loaded: Path | None = None

        def _persist_log() -> str:
            try:
                save_failure_log(failure_log_path, failure_rows)
                return ""
            except PermissionError:
                return (
                    f"⚠️  失败记录写入失败：{failure_log_path.name} 被占用（关掉 Excel 后下次保存补回）。"
                )
            except Exception as e:
                return f"⚠️  失败记录写入失败: {e}"

        # ---- Global retry: all failure slots in one pool -------------------
        from gui.paths import resolved_config_yaml
        from gui.runner import parse_regen_targets, run_batch_generation
        from src.batch_runner import BatchProductSpec

        cfg_path = resolved_config_yaml()

        @dataclass
        class _RetryJob:
            gi: int
            product_dir: Path
            dir_name: str
            items: list
            slot_labels: list[str]
            regen_str: str
            spec: BatchProductSpec

        retry_jobs: list[_RetryJob] = []
        for gi, (product_dir, dir_name, items) in enumerate(groups, start=1):
            slot_labels = [r.slot for _, r in items]
            regen_str = ",".join(slot_labels)
            logs.append(
                f"▶️ [{gi}/{n_groups}] {dir_name or product_dir.name} "
                f"重跑 {len(slot_labels)} 个槽位: {regen_str}"
            )
            if not product_dir.is_dir():
                err_note = "商品目录不存在（可能已被移动 / 删除）"
                logs.append(f"⏭ [{gi}/{n_groups}] SKIP {dir_name or product_dir.name}: {err_note}")
                for _row_i, r in items:
                    r.retry_status = RETRY_FAIL
                    r.retried_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    r.error = err_note
                cum_still += len(items)
                warn = _persist_log()
                if warn:
                    logs.append(warn)
                continue
            retry_jobs.append(
                _RetryJob(
                    gi=gi,
                    product_dir=product_dir,
                    dir_name=dir_name,
                    items=items,
                    slot_labels=slot_labels,
                    regen_str=regen_str,
                    spec=BatchProductSpec(
                        product_dir=product_dir,
                        regen_targets=parse_regen_targets(regen_str),
                        user_note=(user_note or "").strip() or None,
                        counts_str=counts_arg,
                        config_path=cfg_path,
                    ),
                )
            )

        yield _y(
            gallery_imgs=all_imgs,
            log_text="\n".join(logs),
            failure_df_rows=gr.update(value=_capped_failure_df(failure_rows)),
            failure_summary_text=format_failure_summary(failure_rows),
            current=f"**当前任务（重跑）**：全局并发 {len(retry_jobs)} 个商品…",
        )

        if retry_jobs and not cancel_evt.is_set():
            pdir_to_retry = {j.product_dir.resolve(): j for j in retry_jobs}
            q: queue.Queue = queue.Queue()
            holder: dict[str, object] = {}
            io_lock = threading.Lock()
            retry_finished: set[str] = set()

            def _apply_retry_result(job: _RetryJob, meta: dict) -> None:
                nonlocal cum_fixed, cum_still, last_loaded
                product_dir = job.product_dir
                items = job.items
                gi = job.gi
                ok_slots: set[str] = set()
                err_slots: dict[str, str] = {}
                for im in (meta.get("images") or []) if isinstance(meta, dict) else []:
                    if not isinstance(im, dict):
                        continue
                    t_name = str(im.get("type") or "").strip()
                    try:
                        t_idx = int(im.get("index") or 0)
                    except (TypeError, ValueError):
                        continue
                    if not t_name or t_idx <= 0:
                        continue
                    key = f"{t_name}_{t_idx:02d}"
                    if im.get("status") == "ok":
                        ok_slots.add(key)
                    elif im.get("status") == "error":
                        err_slots[key] = str(im.get("error") or "(no error)")

                retried_slots = {r.slot for _, r in items}
                n_fixed = len(retried_slots & ok_slots)
                n_still = len(retried_slots & set(err_slots.keys()))
                cum_fixed += n_fixed
                cum_still += n_still

                merge_failures_from_meta(
                    failure_rows, product_dir=product_dir, meta=meta
                )
                actually_attempted_and_failed = retried_slots & set(err_slots.keys())
                mark_just_retried_failures(
                    failure_rows,
                    product_dir=product_dir,
                    retried_slots=actually_attempted_and_failed,
                )
                warn = _persist_log()
                if warn:
                    logs.append(warn)

                if task_rows_loaded and task_rows:
                    product_dir_resolved = str(product_dir.resolve()).lower()
                    pdir_name_lower = product_dir.name.lower()
                    matched_indices: list[int] = []
                    for i, tr in enumerate(task_rows):
                        if not tr.path.strip():
                            continue
                        p = Path(tr.path).expanduser()
                        try:
                            if str(p.resolve()).lower() == product_dir_resolved:
                                matched_indices.append(i)
                                continue
                        except OSError:
                            pass
                        if p.suffix.lower() == ".zip" and p.stem.lower() == pdir_name_lower:
                            matched_indices.append(i)
                    if matched_indices:
                        try:
                            new_missing, n_ok_in_exp, n_expected, was_cancelled = (
                                compute_missing_slots(product_dir, settings, counts_arg)
                            )
                        except Exception:
                            new_missing, n_ok_in_exp, n_expected, was_cancelled = (
                                list(err_slots.keys()),
                                len(ok_slots),
                                len(ok_slots) + len(err_slots),
                                False,
                            )
                        if isinstance(meta, dict) and meta.get("cancelled"):
                            was_cancelled = True
                        new_status: str | None = None
                        new_note = ""
                        if was_cancelled and new_missing:
                            new_status = STATUS_CANCELLED
                            new_note = (
                                f"重跑被终止：ok={n_ok_in_exp}/{n_expected}, "
                                f"缺 {len(new_missing)} 张 "
                                f"| 输出目录: {product_dir.name}"
                            )
                        elif n_expected > 0 and not new_missing:
                            new_status = STATUS_DONE
                            new_note = (
                                f"重跑后已完成: ok={n_ok_in_exp}/{n_expected} "
                                f"| 输出目录: {product_dir.name}"
                            )
                        elif new_missing:
                            new_status = STATUS_PARTIAL
                            new_note = (
                                f"重跑后仍缺 {len(new_missing)} 张: "
                                f"ok={n_ok_in_exp}/{n_expected} "
                                f"| 输出目录: {product_dir.name}"
                            )
                        if new_status is not None:
                            for mi in matched_indices:
                                mark_row(task_rows, mi, status=new_status, note=new_note)
                            try:
                                save_tasklist(tasklist_path, task_rows)
                            except PermissionError:
                                logs.append(
                                    f"⚠️  任务清单写入失败：{tasklist_path.name} 被占用"
                                    f"（关掉 Excel 后下次开始时会重算状态）。"
                                )
                            except Exception as e:
                                logs.append(f"⚠️  任务清单写入失败: {e}")

                imgs = gallery_images_from_meta(product_dir)
                all_imgs.extend(imgs[:3])
                if (product_dir / "output").is_dir() and gallery_images_from_meta(product_dir):
                    last_loaded = product_dir
                logs.append(
                    f"{'✅' if n_still == 0 else '⚠️ '} [{gi}/{n_groups}] "
                    f"{job.dir_name or product_dir.name} 重跑结果：修好 {n_fixed} / 仍失败 {n_still}"
                )

            def _retry_worker() -> None:
                try:

                    def on_done(pdir: Path, meta: dict) -> None:
                        q.put(("product_done", str(pdir.resolve()), meta))

                    def on_err(pdir: Path, err: str) -> None:
                        q.put(("product_error", str(pdir.resolve()), err))

                    holder["results"] = run_batch_generation(
                        [j.spec for j in retry_jobs],
                        settings,
                        product_done_callback=on_done,
                        product_error_callback=on_err,
                        cancel_event=cancel_evt,
                    )
                except Exception as e:
                    holder["exc"] = e
                finally:
                    q.put(("done",))

            t = threading.Thread(target=_retry_worker, daemon=True)
            t.start()
            done_flag = False
            while True:
                try:
                    ev = q.get(timeout=2.0)
                except queue.Empty:
                    if not t.is_alive() and q.empty():
                        break
                    yield _y(
                        gallery_imgs=all_imgs,
                        log_text="\n".join(logs),
                        failure_df_rows=gr.update(value=_capped_failure_df(failure_rows)),
                        failure_summary_text=format_failure_summary(failure_rows),
                        current="**当前任务（重跑）**：全局并发生成中…",
                    )
                    continue
                kind = ev[0] if ev else None
                if kind == "done":
                    done_flag = True
                elif kind == "product_done":
                    _, pstr, meta = ev
                    job = pdir_to_retry.get(Path(pstr))
                    if job:
                        with io_lock:
                            _apply_retry_result(job, meta if isinstance(meta, dict) else {})
                elif kind == "product_error":
                    _, pstr, err = ev
                    job = pdir_to_retry.get(Path(pstr))
                    if job and pstr not in retry_finished:
                        with io_lock:
                            if pstr not in retry_finished:
                                fe = (err or "")[:300]
                                err_note = f"重跑异常: {fe}"
                                logs.append(
                                    f"❌ [{job.gi}/{n_groups}] FAIL "
                                    f"{job.dir_name or job.product_dir.name}: {fe}"
                                )
                                for _row_i, r in job.items:
                                    r.retry_status = RETRY_FAIL
                                    r.retried_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                    r.error = err_note
                                cum_still += len(job.items)
                                _persist_log()
                                retry_finished.add(pstr)
                yield _y(
                    gallery_imgs=all_imgs,
                    log_text="\n".join(logs),
                    df_rows=(
                        gr.update(value=_capped_tasklist_df(task_rows))
                        if task_rows_loaded
                        else gr.update()
                    ),
                    summary=format_summary(task_rows) if task_rows_loaded else gr.update(),
                    failure_df_rows=gr.update(value=_capped_failure_df(failure_rows)),
                    failure_summary_text=format_failure_summary(failure_rows),
                    current="**当前任务（重跑）**：全局并发生成中…",
                )
                if done_flag:
                    break

            if holder.get("exc"):
                logs.append(f"❌ 全局重跑异常: {str(holder['exc'])[:300]}")

        elif cancel_evt.is_set():
            logs.append("⏹ 用户终止，未启动全局重跑。")
        # ---- Final yield ---------------------------------------------------
        # n_ok_now from failure_rows is now ALWAYS 0 (修好的行被自动删了),
        # so use the cumulative tally we kept in the loop instead.
        n_pending = sum(1 for r in failure_rows if r.retry_status == RETRY_PENDING)
        n_fail_now = sum(1 for r in failure_rows if r.retry_status == RETRY_FAIL)
        logs.append(
            f"🏁 重跑结束：本次修好 {cum_fixed} / 仍失败 {cum_still}"
            f" ｜ 待重跑剩余 {n_pending} / 重跑仍失败 {n_fail_now}（已写入 xlsx）。"
        )
        df_final = (
            gr.update(value=_capped_tasklist_df(task_rows)) if task_rows_loaded else gr.update()
        )
        summary_final = format_summary(task_rows) if task_rows_loaded else gr.update()
        if last_loaded is not None and last_loaded.is_dir():
            title_text = _read_title_file(last_loaded)
            last_gallery = gallery_images_from_meta(last_loaded)
            last_preview = _preview_gallery_items(last_loaded)
            last_state, last_upd = _slot_state_and_updates_full(last_loaded)
            new_choices = [p.name for p in _discover_products_across(root_str or default_root, output_dest_str)]
            yield _y(
                gallery_imgs=last_gallery,
                log_text="\n".join(logs),
                preview=last_preview,
                slot_state=last_state,
                dd=gr.update(choices=new_choices, value=last_loaded.name),
                title=gr.update(value=title_text),
                df_rows=df_final,
                summary=summary_final,
                failure_df_rows=gr.update(value=_capped_failure_df(failure_rows)),
                failure_summary_text=format_failure_summary(failure_rows),
                slot_updates=last_upd,
            )
        else:
            yield _y(
                gallery_imgs=all_imgs,
                log_text="\n".join(logs),
                df_rows=df_final,
                summary=summary_final,
                failure_df_rows=gr.update(value=_capped_failure_df(failure_rows)),
                failure_summary_text=format_failure_summary(failure_rows),
            )

    gr.Markdown(
        "### 目录设置（两个目录都可点「浏览…」从本机选）\n"
        "**填好两个路径后，请点下方「💾 保存路径」按钮**。保存后下次重新打开本实例会自动回填，"
        "不会再变成默认值。每个实例的路径存在各自独立的配置文件里，互不覆盖。"
    )
    with gr.Row():
        root_in = gr.Textbox(
            label="项目根目录（扫描已有商品文件夹）",
            value=default_root,
            scale=5,
        )
        root_browse_btn = gr.Button("浏览…", scale=1, min_width=80, size="sm")
    with gr.Row():
        output_dest_in = gr.Textbox(
            label="输出商品保存位置（批量解压 + 生成结果都落到这里；留空则同项目根目录）",
            value=default_dest,
            placeholder="例：D:/已生成/商品输出  —— 留空时复用上方项目根目录",
            scale=5,
        )
        output_dest_browse_btn = gr.Button("浏览…", scale=1, min_width=80, size="sm")
    with gr.Row():
        save_paths_btn = gr.Button(
            "💾 保存路径（点完关浏览器也不会丢）", variant="primary", scale=5, size="sm"
        )
    save_paths_msg = gr.Markdown("")

    gr.Markdown(
        "### 图片张数设置（单商品 / 批量共用；勾选「启用张数覆盖」后这里的数字生效）"
    )
    with gr.Row():
        override_chk = gr.Checkbox(
            label="启用下方张数覆盖（关闭则仅用 config.yaml / 商品 counts.yaml）",
            value=False,
        )
    with gr.Row():
        for t in ALLOWED_TYPES:
            count_inputs.append(
                gr.Number(
                    label=t,
                    value=int(dc[t]),
                    minimum=0,
                    maximum=20,
                    step=1,
                    precision=0,
                )
            )

    with gr.Row():
        with gr.Column(scale=1):
            gr.Markdown("### 单商品生成")
            refresh_btn = gr.Button("刷新商品列表")
            status_refresh = gr.Markdown("")
            product_dd = gr.Dropdown(label="选择商品", choices=[], allow_custom_value=True)
            title_in = gr.Textbox(label="商品标题（写入该商品文件夹下的 商品标题.txt）", lines=2)
            save_title_btn = gr.Button("保存标题到磁盘")
            title_save_msg = gr.Markdown("")
            zip_in = gr.File(
                label="上传商品 ZIP（解压到「输出商品保存位置」，留空时落项目根目录）",
                file_types=[".zip"],
                type="filepath",
            )
            unzip_btn = gr.Button("解压 ZIP")
            zip_msg = gr.Markdown("")
            with gr.Accordion("高级：仅重跑（文本，逗号分隔）", open=False):
                regen_tb = gr.Textbox(
                    label="仅重跑（可选）",
                    placeholder="例: scene_02 或 scene_02,detail_01",
                    lines=1,
                )
            note_tb = gr.Textbox(label="一次性备注（可选，等同 CLI --note）", lines=2)
            with gr.Row():
                go_btn = gr.Button("开始生成", variant="primary", scale=3)
                stop_single_btn = gr.Button("终止生成", variant="stop", scale=1)
            stop_single_msg = gr.Markdown("")
            log_out = gr.Textbox(
                label="日志（含重试 / 失败原因 / 完成状态）",
                lines=14,
                max_lines=20,
                autoscroll=True,
            )
            paths_out = gr.Textbox(label="输出文件列表", lines=4)

        with gr.Column(scale=1):
            gr.Markdown(
                "### 批量生成（断点续跑模式）\n"
                "- 任务清单是一张 **Excel 文件**，每个实例一份独立的（默认在 exe 同目录）。\n"
                "- 在 Excel 里 A 列填路径、B 列填标题，**保存关闭** Excel 后回到这里点「开始」。\n"
                "- 处理完一行就立即写一行回 Excel：随时关机 / 中断都不会丢进度，再点开始会自动从下一行继续。\n"
                "- C/D/E 列由程序写入：状态、处理时间、备注（包含输出商品文件夹名）。"
            )
            with gr.Row():
                tasklist_path_in = gr.Textbox(
                    label="任务清单 .xlsx",
                    value=str(default_tasklist_path()),
                    placeholder="例：D:/批量任务_实例1.xlsx",
                    scale=5,
                )
                tasklist_browse_btn = gr.Button("浏览…", scale=1, min_width=80, size="sm")
            with gr.Row():
                tasklist_open_btn = gr.Button("📋 打开 Excel 编辑", size="sm", scale=2)
                tasklist_refresh_btn = gr.Button("🔄 刷新表格", size="sm", scale=2)
                tasklist_reset_btn = gr.Button("♻️ 全部重置状态", size="sm", scale=2)
            tasklist_summary = gr.Markdown("（点击 🔄 刷新表格 加载任务清单）")
            current_processing_md = gr.Markdown("**当前任务**：尚未开始")
            batch_df = gr.Dataframe(
                headers=["ZIP/文件夹路径", "商品标题", "状态", "处理时间", "备注"],
                datatype=["str", "str", "str", "str", "str"],
                # 表格仅作为预览；最多 5 数据行 + 1 行省略号；完整内容请打开
                # Excel 查看。Dataframe 内部用 ``"dynamic"`` 让空清单时表格
                # 自动收紧、不留空行。
                row_count=(6, "dynamic"),
                col_count=(5, "fixed"),
                type="array",
                label=f"任务清单预览（最多显示 {_DF_PREVIEW_CAP} 行；完整内容请点上方「📋 打开 Excel 编辑」）",
                wrap=True,
                interactive=False,
            )
            with gr.Row():
                retry_failed_chk = gr.Checkbox(
                    label="重试「失败」行（默认跳过；勾选后失败行会重跑）",
                    value=False,
                )
                retry_partial_chk = gr.Checkbox(
                    label="重做「部分完成」行",
                    value=False,
                )
                retry_cancelled_chk = gr.Checkbox(
                    label="续跑「已终止」行（默认开；只生成没做完的图）",
                    value=True,
                )
            with gr.Row():
                batch_go = gr.Button("🚀 开始（断点续跑）", variant="primary", scale=3)
                stop_batch_btn = gr.Button("⏹ 终止", variant="stop", scale=1)
            stop_batch_msg = gr.Markdown("")
            batch_log = gr.Textbox(
                label="批量日志（每行处理 / 重试 / 失败 / 完成）",
                lines=14,
                max_lines=20,
                autoscroll=True,
            )

    gr.Markdown("### 尺寸图审核（生成 size 图前确认使用哪一张参考图）")
    with gr.Row():
        size_review_btn = gr.Button("识别/刷新尺寸图", size="sm")
        size_save_btn = gr.Button("保存重选尺寸图", variant="primary", size="sm")
    size_review_msg = gr.Markdown("")
    size_review_df = gr.Dataframe(
        headers=["商品编号", "图片展示", "重选尺寸图"],
        datatype=["str", "str", "str"],
        row_count=(6, "dynamic"),
        col_count=(3, "fixed"),
        type="array",
        label="尺寸图审核表",
        wrap=True,
        interactive=False,
    )
    with gr.Row():
        size_product_dd = gr.Dropdown(label="审核商品", choices=[], allow_custom_value=True, scale=2)
        size_candidate_dd = gr.Dropdown(label="重选尺寸图", choices=[], allow_custom_value=True, scale=4)
    size_preview_img = gr.Image(label="当前尺寸图预览", type="filepath", height=260)

    # ---- Full-width batch outputs (gallery + failure log) ------------------
    # These were inside the right-hand batch column before, but at scale=1
    # they only used half the page width. Pulling them out to the page level
    # gives the gallery thumbnails real estate to breathe in and lets the
    # 7-column failure dataframe show all its columns without a scrollbar.
    gr.Markdown("### 🖼️ 批量预览（最近 N 个商品的前 3 张缩略图）")
    gallery = gr.Gallery(label="预览", columns=6, height=300)

    # ---- Failure log (per-slot) ------------------------------------------
    gr.Markdown(
        "### 📛 失败图片记录（按槽位）\n"
        "- 批量跑过程中，每张失败的图（重试 3 次仍 500 / 网络错误等）都会在这里登记一条。\n"
        "- 文件位置自动放在**任务清单同目录**（自动用 `批量失败记录_*.xlsx` 文件名）。\n"
        "- 点「🔁 重跑全部失败图片」会按商品分组，**只重跑失败槽位**，已成功的不会重做、不会消耗 API；"
        "并且会把任务清单的状态一起更新，避免和上方表格不一致。"
    )
    with gr.Row():
        failure_open_btn = gr.Button("📋 打开失败记录 Excel", size="sm", scale=2)
        failure_refresh_btn = gr.Button("🔄 刷新失败记录", size="sm", scale=2)
        failure_clear_btn = gr.Button("♻️ 清空全部失败记录", size="sm", scale=2)
    failure_summary = gr.Markdown("（点 🔄 加载或先跑一次批量）")
    failure_df = gr.Dataframe(
        headers=["商品文件夹", "槽位", "错误信息", "失败时间", "重跑状态", "重跑时间", "商品路径"],
        datatype=["str", "str", "str", "str", "str", "str", "str"],
        # 同任务清单：最多 5 数据行 + 1 行省略号；详情请打开失败记录 Excel。
        row_count=(6, "dynamic"),
        col_count=(7, "fixed"),
        type="array",
        label=f"失败槽位预览（最多显示 {_DF_PREVIEW_CAP} 行；完整内容请点上方「📋 打开失败记录 Excel」）",
        wrap=True,
        interactive=False,
    )
    with gr.Row():
        retry_include_done_chk = gr.Checkbox(
            label="也重跑「已重跑成功」的行（一般不勾，除非你不满意之前重跑的结果）",
            value=False,
        )
    with gr.Row():
        retry_failures_btn = gr.Button(
            "🔁 重跑全部失败图片", variant="primary", scale=3
        )
        stop_retry_btn = gr.Button("⏹ 终止", variant="stop", scale=1)
    stop_retry_msg = gr.Markdown("")

    gr.Markdown(
        "### 参考图与本次输出图（点击缩略图可放大查看；下方各槽位按文件名勾选）"
    )
    preview_gallery = gr.Gallery(
        label="refs/* + output/*（点击放大）",
        columns=6,
        height=180,
        show_label=True,
        object_fit="contain",
        preview=False,
        allow_preview=True,
    )

    gr.Markdown(
        "### 逐张预览与重做（切换商品或生成后显示；展开「选择参考图」勾选 refs/ 与本次输出图，自定义提示词后点「重做这张」）"
    )
    slot_state = gr.State([None] * SLOT_COUNT)
    for _row in range(SLOT_ROWS):
        with gr.Row(equal_height=False):
            for _col in range(SLOT_COLS):
                col = gr.Column(scale=1, min_width=220, visible=False)
                with col:
                    cap = gr.Markdown("")
                    im = gr.Image(label="预览", height=160, type="filepath")
                    with gr.Accordion("选择参考图 / 自定义提示词", open=False):
                        combined_cg = gr.CheckboxGroup(
                            label="参考图（refs/ 与本次输出图）",
                            choices=[],
                            value=[],
                        )
                        nb = gr.Textbox(
                            label="本张备注",
                            lines=1,
                            placeholder="重做时附加说明",
                        )
                        prompt_tb = gr.Textbox(
                            label="本张自定义提示词（覆盖 brief）",
                            lines=2,
                        )
                    bt = gr.Button("重做这张", size="sm")
                slot_caps.append(cap)
                slot_imgs.append(im)
                slot_combined_cg.append(combined_cg)
                slot_notes.append(nb)
                slot_prompts.append(prompt_tb)
                slot_btns.append(bt)
                slot_cols.append(col)

    slot_flat_outputs: list = []
    for i in range(SLOT_COUNT):
        slot_flat_outputs.extend(
            [
                slot_caps[i],
                slot_imgs[i],
                slot_combined_cg[i],
                slot_notes[i],
                slot_prompts[i],
                slot_cols[i],
            ]
        )

    def on_product_change(
        root_str: str, output_dest_str: str, product_name: str | None
    ):
        empty_state = [None] * SLOT_COUNT
        empty_slots = _empty_slot_updates_full()
        product_dir = _resolve_product_dir(root_str or default_root, output_dest_str, product_name)
        if product_dir is None:
            return "", [], [], empty_state, *empty_slots
        title = _read_title_file(product_dir)
        gallery_imgs = gallery_images_from_meta(product_dir)
        preview = _preview_gallery_items(product_dir)
        st, upd = _slot_state_and_updates_full(product_dir)
        return title, gallery_imgs, preview, st, *upd

    def save_title_disk(
        root_str: str, output_dest_str: str, product_name: str | None, title_text: str
    ):
        product_dir = _resolve_product_dir(root_str or default_root, output_dest_str, product_name)
        if product_dir is None:
            return "未选择商品 / 路径不存在"
        if not (title_text or "").strip():
            return "标题为空，未写入"
        _write_title_file(product_dir, title_text)
        return f"已写入: {product_dir / TITLE_FILE}"

    def browse_into(current: str) -> str:
        picked = _pick_folder(current or default_root)
        return picked or (current or "")

    def _persist_root(val: str) -> None:
        save_paths_into_config(project_root=val, output_dest=None)

    def _persist_dest(val: str) -> None:
        save_paths_into_config(project_root=None, output_dest=val)

    def browse_root(current: str) -> str:
        picked = browse_into(current)
        _persist_root(picked)
        return picked

    def browse_dest(current: str) -> str:
        picked = browse_into(current)
        _persist_dest(picked)
        return picked

    def save_paths_explicit(root_str: str, output_dest_str: str) -> str:
        """Persist both path text boxes in one shot from the 💾 保存路径 button.

        Unlike ``blur`` which races the browser-tab close, this fires from a
        click whose round-trip completes BEFORE the user can navigate away,
        so even «五个窗口一起 Alt+F4» can't lose the paths. Returns a Markdown
        line that shows the exact JSON file the values landed in so the user
        can verify per-instance isolation at a glance.
        """
        from gui.config_store import CONFIG_FILE

        r = (root_str or "").strip()
        d = (output_dest_str or "").strip()
        save_paths_into_config(project_root=r, output_dest=d)
        instance_label = os.environ.get("AMAZON_IMG_GUI_INSTANCE", "").strip() or "（无后缀，启动.exe 直开）"
        return (
            f"✅ 已保存路径到本实例（实例标识：**{instance_label}**）。\n\n"
            f"- 项目根目录：`{r or '（空）'}`\n"
            f"- 输出商品保存位置：`{d or '（空）— 留空则复用项目根目录'}`\n"
            f"- 配置文件：`{CONFIG_FILE}`\n\n"
            "下次重新打开本实例会自动回填这两个路径，不会变回默认值。"
        )

    root_browse_btn.click(
        browse_root, [root_in], [root_in], show_progress="hidden"
    )
    output_dest_browse_btn.click(
        browse_dest, [output_dest_in], [output_dest_in], show_progress="hidden"
    )
    # Also save when the user types/blurs out of the box (Gradio fires `blur`
    # once focus leaves; `change` is too chatty mid-typing on Windows IME).
    # NOTE: blur 是 best-effort — 浏览器 tab 关得太快时这个 POST 可能在路上被掐。
    # 真正可靠的入口是 「💾 保存路径」按钮，见下方 save_paths_btn.click。
    root_in.blur(_persist_root, [root_in], None, show_progress="hidden")
    output_dest_in.blur(_persist_dest, [output_dest_in], None, show_progress="hidden")
    save_paths_btn.click(
        save_paths_explicit,
        [root_in, output_dest_in],
        [save_paths_msg],
        show_progress="hidden",
    )

    def _on_stop_click() -> str:
        fired, msg = _BUSY.request_cancel()
        if fired:
            gr.Info(msg)
        else:
            gr.Warning(msg)
        return msg

    stop_single_btn.click(_on_stop_click, None, [stop_single_msg], show_progress="hidden")
    stop_batch_btn.click(_on_stop_click, None, [stop_batch_msg], show_progress="hidden")

    def _size_review_product_dirs(
        root_str: str,
        output_dest_str: str,
        product_name: str | None,
        tasklist_path_str: str,
    ) -> list[Path]:
        dirs: list[Path] = []
        selected = _resolve_product_dir(root_str or default_root, output_dest_str, product_name)
        if selected is not None:
            dirs.append(selected)
        for p in _review_product_dirs_from_tasklist(root_str or default_root, output_dest_str, tasklist_path_str, default_root):
            if all(str(p.resolve()) != str(x.resolve()) for x in dirs):
                dirs.append(p)
        if not dirs:
            try:
                dirs = _discover_products_across(root_str or default_root, output_dest_str)
            except Exception:
                dirs = []
        return dirs

    def _refresh_size_review(
        root_str: str,
        output_dest_str: str,
        product_name: str | None,
        tasklist_path_str: str,
    ):
        dirs = _size_review_product_dirs(root_str, output_dest_str, product_name, tasklist_path_str)
        rows = _size_review_rows(dirs)
        names = [p.name for p in dirs]
        value = product_name if product_name in names else (names[0] if names else None)
        msg = f"已识别 {len(rows)} 个商品的尺寸图候选。" if rows else "未找到可审核的商品。"
        cand_choices, cand_value, preview = _size_candidates_for_product(root_str, output_dest_str, value)
        return (
            gr.update(value=rows),
            gr.update(choices=names, value=value),
            gr.update(choices=cand_choices, value=cand_value),
            preview,
            msg,
        )

    def _size_candidates_for_product(
        root_str: str,
        output_dest_str: str,
        product_name: str | None,
    ):
        pdir = _resolve_product_dir(root_str or default_root, output_dest_str, product_name)
        if pdir is None:
            return [], None, None
        candidates = list_size_ref_candidates(pdir)
        picked = ensure_auto_size_ref(pdir)
        labels = [_candidate_label(c.path, c.score, c.reason) for c in candidates]
        value = None
        if picked is not None:
            for lab in labels:
                if lab.split("|", 1)[0].strip() == picked.path.name:
                    value = lab
                    break
        return labels, value, str(picked.path.resolve()) if picked else None

    def _on_size_product_change(root_str: str, output_dest_str: str, product_name: str | None):
        choices, value, preview = _size_candidates_for_product(root_str, output_dest_str, product_name)
        return gr.update(choices=choices, value=value), preview

    def _preview_size_candidate(
        root_str: str,
        output_dest_str: str,
        product_name: str | None,
        label: str,
    ) -> str | None:
        pdir = _resolve_product_dir(root_str or default_root, output_dest_str, product_name)
        if pdir is None:
            return None
        path = _candidate_from_label(pdir, label)
        return str(path.resolve()) if path is not None else None

    def _save_size_choice(
        root_str: str,
        output_dest_str: str,
        product_name: str | None,
        selected_label: str,
        tasklist_path_str: str,
    ):
        pdir = _resolve_product_dir(root_str or default_root, output_dest_str, product_name)
        if pdir is None:
            return gr.update(), None, "请选择有效商品后再保存。"
        path = _candidate_from_label(pdir, selected_label)
        if path is None:
            return gr.update(), None, "请选择一张有效的尺寸图候选。"
        cand = score_size_ref(path)
        score = float(cand.score)
        reason = str(cand.reason)
        save_selected_size_ref(pdir, path, source="manual", score=score, reason=reason)
        rows = _size_review_rows(_size_review_product_dirs(root_str, output_dest_str, product_name, tasklist_path_str))
        return gr.update(value=rows), str(path.resolve()), f"已保存 {pdir.name} 的尺寸图：{path.name}"

    refresh_btn.click(
        refresh_products,
        [root_in, output_dest_in],
        [product_dd, status_refresh],
    )
    refresh_btn.click(
        _refresh_size_review,
        [root_in, output_dest_in, product_dd, tasklist_path_in],
        [size_review_df, size_product_dd, size_candidate_dd, size_preview_img, size_review_msg],
        show_progress="hidden",
    )

    product_dd.change(
        on_product_change,
        [root_in, output_dest_in, product_dd],
        [title_in, gallery, preview_gallery, slot_state, *slot_flat_outputs],
        show_progress="hidden",
    )
    product_dd.change(
        _refresh_size_review,
        [root_in, output_dest_in, product_dd, tasklist_path_in],
        [size_review_df, size_product_dd, size_candidate_dd, size_preview_img, size_review_msg],
        show_progress="hidden",
    )

    size_review_btn.click(
        _refresh_size_review,
        [root_in, output_dest_in, product_dd, tasklist_path_in],
        [size_review_df, size_product_dd, size_candidate_dd, size_preview_img, size_review_msg],
        show_progress="hidden",
    )
    size_product_dd.change(
        _on_size_product_change,
        [root_in, output_dest_in, size_product_dd],
        [size_candidate_dd, size_preview_img],
        show_progress="hidden",
    )
    size_candidate_dd.change(
        _preview_size_candidate,
        [root_in, output_dest_in, size_product_dd, size_candidate_dd],
        [size_preview_img],
        show_progress="hidden",
    )
    size_save_btn.click(
        _save_size_choice,
        [root_in, output_dest_in, size_product_dd, size_candidate_dd, tasklist_path_in],
        [size_review_df, size_preview_img, size_review_msg],
        show_progress="hidden",
    )

    save_title_btn.click(
        save_title_disk,
        [root_in, output_dest_in, product_dd, title_in],
        [title_save_msg],
    )

    go_inputs = [
        root_in,
        output_dest_in,
        product_dd,
        title_in,
        override_chk,
        regen_tb,
        note_tb,
        *count_inputs,
        tasklist_path_in,
    ]
    go_outputs = [gallery, log_out, paths_out, preview_gallery, slot_state, *slot_flat_outputs]
    go_btn.click(
        run_one,
        go_inputs,
        go_outputs,
        show_progress="hidden",
    )

    batch_inputs = [
        root_in,
        output_dest_in,
        tasklist_path_in,
        retry_failed_chk,
        retry_partial_chk,
        retry_cancelled_chk,
        override_chk,
        note_tb,
        *count_inputs,
    ]
    batch_outputs = [
        gallery,
        batch_log,
        preview_gallery,
        slot_state,
        product_dd,
        title_in,
        batch_df,
        tasklist_summary,
        current_processing_md,
        failure_df,
        failure_summary,
        *slot_flat_outputs,
    ]
    batch_go.click(
        run_batch,
        batch_inputs,
        batch_outputs,
        show_progress="hidden",
    )

    retry_inputs = [
        root_in,
        output_dest_in,
        tasklist_path_in,
        retry_include_done_chk,
        override_chk,
        note_tb,
        *count_inputs,
    ]
    retry_failures_btn.click(
        run_retry_failures,
        retry_inputs,
        batch_outputs,
        show_progress="hidden",
    )

    # ---- Task-list controls (browse / open / refresh / reset) ----
    def _refresh_tasklist(path_str: str) -> tuple[Any, str]:
        path = Path((path_str or "").strip()).expanduser()
        if not str(path):
            return gr.update(value=[]), "⚠️ 任务清单路径为空。"
        try:
            init_template_if_missing(path)
            rows = load_tasklist(path)
        except PermissionError:
            return gr.update(), f"⚠️ 文件被占用（请关闭 Excel 再点刷新）：{path}"
        except Exception as e:
            return gr.update(), f"❌ 读取失败 {path}: {e}"
        if not rows:
            return gr.update(value=[]), f"📋 已建立空白任务清单：{path}（请打开它填路径）"
        return gr.update(value=_capped_tasklist_df(rows)), f"📋 {path}\n\n{format_summary(rows)}"

    def _open_tasklist(path_str: str) -> tuple[Any, str]:
        path = Path((path_str or "").strip()).expanduser()
        if not str(path):
            return gr.update(), "⚠️ 任务清单路径为空。"
        try:
            init_template_if_missing(path)
        except Exception as e:
            return gr.update(), f"❌ 创建失败 {path}: {e}"
        ok, msg = open_in_default_app(path)
        df_upd, _summary = _refresh_tasklist(path_str)
        prefix = "✅ " if ok else "⚠️ "
        return df_upd, f"{prefix}{msg}\n\n（在 Excel 中编辑保存后，回这里点「🔄 刷新表格」同步）"

    def _reset_tasklist(path_str: str) -> tuple[Any, str]:
        path = Path((path_str or "").strip()).expanduser()
        if not path.is_file():
            return gr.update(value=[]), "⚠️ 文件不存在，无法重置。"
        try:
            rows = load_tasklist(path)
        except Exception as e:
            return gr.update(), f"❌ 读取失败 {path}: {e}"
        if not rows:
            return gr.update(value=[]), "（任务清单为空，无须重置）"
        reset_all_status(rows)
        try:
            save_tasklist(path, rows)
        except PermissionError:
            return gr.update(), f"⚠️ 写入失败（请关闭 Excel）：{path}"
        except Exception as e:
            return gr.update(), f"❌ 写入失败: {e}"
        return gr.update(value=_capped_tasklist_df(rows)), f"♻️ 已把全部 {len(rows)} 行重置为「待处理」。"

    def _browse_tasklist(current: str) -> str:
        # Reuse the simple folder-picker pattern (text-only fallback when no Tk).
        picked = _pick_folder(str(Path(current).parent) if current else default_root)
        if not picked:
            return current or ""
        suggested = Path(picked) / Path(current).name if current else Path(picked) / "批量任务.xlsx"
        return str(suggested)

    tasklist_browse_btn.click(
        _browse_tasklist, [tasklist_path_in], [tasklist_path_in], show_progress="hidden"
    )
    tasklist_open_btn.click(
        _open_tasklist, [tasklist_path_in], [batch_df, tasklist_summary], show_progress="hidden"
    )
    tasklist_refresh_btn.click(
        _refresh_tasklist, [tasklist_path_in], [batch_df, tasklist_summary], show_progress="hidden"
    )
    tasklist_refresh_btn.click(
        _refresh_size_review,
        [root_in, output_dest_in, product_dd, tasklist_path_in],
        [size_review_df, size_product_dd, size_candidate_dd, size_preview_img, size_review_msg],
        show_progress="hidden",
    )
    tasklist_reset_btn.click(
        _reset_tasklist, [tasklist_path_in], [batch_df, tasklist_summary], show_progress="hidden"
    )

    # ---- Failure-log controls (open / refresh / clear / stop) -----------
    def _failure_path_for(tasklist_path_str: str) -> Path | None:
        path = Path((tasklist_path_str or "").strip()).expanduser()
        if not str(path):
            return None
        return derive_failure_log_path(path)

    def _refresh_failure_log(tasklist_path_str: str) -> tuple[Any, str]:
        flp = _failure_path_for(tasklist_path_str)
        if flp is None:
            return gr.update(value=[]), "⚠️ 任务清单路径为空，无法定位失败记录文件。"
        try:
            init_failure_log_if_missing(flp)
            rows = load_failure_log(flp)
        except PermissionError:
            return gr.update(), f"⚠️ 文件被占用（请关闭 Excel 再点刷新）：{flp}"
        except Exception as e:
            return gr.update(), f"❌ 读取失败 {flp}: {e}"
        head = f"📛 失败记录文件：`{flp}`\n\n"
        return gr.update(value=_capped_failure_df(rows)), head + format_failure_summary(rows)

    def _open_failure_log(tasklist_path_str: str) -> tuple[Any, str]:
        flp = _failure_path_for(tasklist_path_str)
        if flp is None:
            return gr.update(), "⚠️ 任务清单路径为空，无法定位失败记录文件。"
        try:
            init_failure_log_if_missing(flp)
        except Exception as e:
            return gr.update(), f"❌ 创建失败 {flp}: {e}"
        ok, msg = open_failure_log(flp)
        df_upd, summary = _refresh_failure_log(tasklist_path_str)
        prefix = "✅ " if ok else "⚠️ "
        return df_upd, f"{prefix}{msg}\n\n{summary}"

    def _clear_resolved_failures(tasklist_path_str: str) -> tuple[Any, str]:
        """Wipe the entire failure log (the ♻️ 清空全部失败记录 button).

        Successful retries auto-vanish (handled by ``merge_failures_from_meta``),
        so this button is now the nuclear "I want a clean slate" action —
        e.g. after the user has manually triaged everything and just wants
        the slate empty before the next batch.
        """
        flp = _failure_path_for(tasklist_path_str)
        if flp is None:
            return gr.update(value=[]), "⚠️ 任务清单路径为空，无法定位失败记录文件。"
        try:
            init_failure_log_if_missing(flp)
            rows = load_failure_log(flp)
        except PermissionError:
            return gr.update(), f"⚠️ 文件被占用（请关闭 Excel）：{flp}"
        except Exception as e:
            return gr.update(), f"❌ 读取失败 {flp}: {e}"
        before = len(rows)
        cleared = clear_all(rows)
        try:
            save_failure_log(flp, rows)
        except PermissionError:
            return gr.update(), f"⚠️ 写入失败（请关闭 Excel）：{flp}"
        except Exception as e:
            return gr.update(), f"❌ 写入失败: {e}"
        return (
            gr.update(value=_capped_failure_df(rows)),
            f"♻️ 已清空全部失败记录（共 {cleared} 条；之前: {before}）。"
            if cleared
            else "失败记录原本就是空的。",
        )

    failure_open_btn.click(
        _open_failure_log,
        [tasklist_path_in],
        [failure_df, failure_summary],
        show_progress="hidden",
    )
    failure_refresh_btn.click(
        _refresh_failure_log,
        [tasklist_path_in],
        [failure_df, failure_summary],
        show_progress="hidden",
    )
    failure_clear_btn.click(
        _clear_resolved_failures,
        [tasklist_path_in],
        [failure_df, failure_summary],
        show_progress="hidden",
    )
    stop_retry_btn.click(_on_stop_click, None, [stop_retry_msg], show_progress="hidden")

    def _regen_one_slot(
        slot_index: int,
        root_str: str,
        output_dest_str: str,
        product_name: str | None,
        state_list: list,
        slot_note: str,
        combined_selected: list | None,
        custom_prompt: str,
        tasklist_path_str: str = "",
    ):
        cancel_evt = _BUSY.try_acquire("regen", str(product_name or ""))
        if cancel_evt is None:
            gr.Warning(_BUSY.busy_message())
            return
        try:
            yield from _regen_one_slot_inner(
                slot_index,
                root_str,
                output_dest_str,
                product_name,
                state_list,
                slot_note,
                combined_selected,
                custom_prompt,
                cancel_evt,
                tasklist_path_str,
            )
        finally:
            _BUSY.release()

    def _regen_one_slot_inner(
        slot_index: int,
        root_str: str,
        output_dest_str: str,
        product_name: str | None,
        state_list: list,
        slot_note: str,
        combined_selected: list | None,
        custom_prompt: str,
        cancel_evt: threading.Event,
        tasklist_path_str: str = "",
    ):
        empty_state = [None] * SLOT_COUNT
        empty_slots = _empty_slot_updates_full()
        product_dir = _resolve_product_dir(root_str or default_root, output_dest_str, product_name)
        if product_dir is None:
            yield [], "请选择有效的商品文件夹（在项目根目录或输出目录中找不到）", "", [], empty_state, *empty_slots
            return
        root = product_dir.parent
        if slot_index < 0 or slot_index >= len(state_list) or state_list[slot_index] is None:
            yield (
                [],
                "该槽位暂无图：请先生成或刷新。",
                "",
                _preview_gallery_items(product_dir),
                state_list if isinstance(state_list, list) else empty_state,
                *empty_slots,
            )
            return
        pair = state_list[slot_index]
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            yield [], "槽位数据无效", "", [], empty_state, *empty_slots
            return
        t, idx = str(pair[0]), int(pair[1])
        regen_s = f"{t}_{idx:02d}"
        settings = effective_settings()
        if settings.image_provider in ("xais", "shiyun", "gemini", "doubao") and not (settings.image_api_key or "").strip():
            yield [], "请先在「设置」中填写 API Key。", "", [], empty_state, *empty_slots
            return

        custom_paths = _build_custom_ref_paths(
            product_dir,
            t,
            idx,
            combined_selected,
        )
        if t == "size":
            picked = ensure_auto_size_ref(product_dir)
            custom_paths = [picked.path] if picked is not None else custom_paths
        if not custom_paths:
            yield [], "请至少勾选一张参考图（refs/ 或本次输出图）。", "", _preview_gallery_items(product_dir), empty_state, *empty_slots
            return

        cp = (custom_prompt or "").strip() or None

        q: queue.Queue = queue.Queue()
        holder: dict[str, Any] = {}

        def worker() -> None:
            sink_id = _attach_log_sink(q)
            try:

                def cb(type_name: str, done: int, total: int) -> None:
                    q.put(("tick", type_name, done, total))

                holder["meta"] = run_generation(
                    project_root=root,
                    product_dir=product_dir,
                    counts_str=None,
                    regen_str=regen_s,
                    user_note=(slot_note or "").strip() or None,
                    settings=settings,
                    progress_callback=cb,
                    custom_ref_paths=custom_paths,
                    custom_prompt=cp,
                    cancel_event=cancel_evt,
                )
            except BaseException as e:  # noqa: BLE001
                holder["exc"] = e
            finally:
                _detach_log_sink(sink_id)
                q.put(("done", None, None, None))

        worker_t = threading.Thread(target=worker, daemon=True)
        worker_t.start()
        recent_logs: list[str] = [f"重绘 {regen_s} 已启动（通常 30~60 秒）"]
        last_status = recent_logs[0]
        regen_marker = (t, idx)
        imgs0 = gallery_images_from_meta(product_dir)
        st0, upd0 = _slot_image_only_updates(product_dir, marker=regen_marker)
        yield (
            imgs0,
            _render_log(recent_logs),
            "\n".join(imgs0) if imgs0 else "",
            _preview_gallery_items(product_dir),
            st0,
            *upd0,
        )
        heartbeat = 0
        last_event_t = time.monotonic()
        stuck_warned = False
        done_flag = False
        while True:
            try:
                ev = q.get(timeout=2.0)
            except queue.Empty:
                if not worker_t.is_alive() and q.empty():
                    holder.setdefault(
                        "exc",
                        RuntimeError("重绘线程异常退出（worker 已停止但未上报完成）。"),
                    )
                    recent_logs.append("❌ 重绘线程异常退出，未收到完成信号。")
                    break
                heartbeat += 1
                idle_s = time.monotonic() - last_event_t
                if idle_s > STUCK_WARN_SEC and not stuck_warned:
                    recent_logs.append(
                        f"⚠️  已 {int(idle_s)} 秒未收到任何进度，可能卡住。"
                    )
                    stuck_warned = True
                dots = "." * (1 + heartbeat % 4)
                imgs_hb = gallery_images_from_meta(product_dir)
                st_hb, upd_hb = _slot_image_only_updates(product_dir, marker=regen_marker)
                yield (
                    imgs_hb,
                    _render_log(recent_logs, f"{last_status} {dots}"),
                    "\n".join(imgs_hb) if imgs_hb else "",
                    _preview_gallery_items(product_dir),
                    st_hb,
                    *upd_hb,
                )
                continue
            events = [ev]
            while True:
                try:
                    events.append(q.get_nowait())
                except queue.Empty:
                    break
            last_event_t = time.monotonic()
            stuck_warned = False
            for e in events:
                kind = e[0] if e else None
                if kind == "done":
                    done_flag = True
                    continue
                if kind == "tick":
                    _, tn, done, tot = e
                    if tot and tot > 0:
                        last_status = f"重绘 {regen_s} 中… {tn} {done}/{tot}"
                    continue
                if kind == "log":
                    _, lvl, txt = e
                    recent_logs.append(_fmt_log_line(lvl, txt))
                    if (lvl or "").upper() in {"WARNING", "WARN", "ERROR", "CRITICAL"}:
                        last_status = _fmt_log_line(lvl, txt)
                    continue
            imgs = gallery_images_from_meta(product_dir)
            st, slot_upd = _slot_image_only_updates(product_dir, marker=regen_marker)
            preview = _preview_gallery_items(product_dir)
            yield imgs, _render_log(recent_logs, last_status), "\n".join(imgs) if imgs else "", preview, st, *slot_upd
            if done_flag:
                break

        if holder.get("exc") is not None:
            recent_logs.append(f"❌ {_friendly_error(holder['exc'])}")
            _, supd = _slot_state_and_updates_full(product_dir)
            yield [], _render_log(recent_logs), "", _preview_gallery_items(product_dir), state_list, *supd
            return

        imgs = gallery_images_from_meta(product_dir)
        recent_logs.append(f"✅ 已重跑 {regen_s}。输出目录: {product_dir / 'output'}")
        # Sync the failure log + task list xlsx so 重做这张 success makes
        # the matching row vanish from 失败记录, and the task-list row's
        # status reflects the latest meta.json. Pass ``retried_slots`` so
        # a regen that produced an error placeholder again is flagged as
        # 重跑仍失败 (consistent with batch retry's behaviour).
        if tasklist_path_str:
            sync_msg = _sync_batch_xlsx_after_meta(
                tasklist_path_str,
                product_dir,
                holder.get("meta"),
                settings,
                retried_slots={regen_s},
            )
            if sync_msg:
                recent_logs.append(sync_msg)
        st, slot_upd = _slot_state_and_updates_full(product_dir)
        yield imgs, _render_log(recent_logs), "\n".join(imgs) if imgs else "", _preview_gallery_items(product_dir), st, *slot_upd

    def _make_regen_handler(si: int):
        def _handler(
            root_str: str,
            output_dest_str: str,
            product_name: str | None,
            state_list: list,
            slot_note: str,
            combined_sel: list | None,
            custom_pr: str,
            tasklist_path_str: str = "",
        ):
            yield from _regen_one_slot(
                si,
                root_str,
                output_dest_str,
                product_name,
                state_list,
                slot_note,
                combined_sel,
                custom_pr,
                tasklist_path_str,
            )

        return _handler

    for si in range(SLOT_COUNT):
        slot_btns[si].click(
            _make_regen_handler(si),
            [
                root_in,
                output_dest_in,
                product_dd,
                slot_state,
                slot_notes[si],
                slot_combined_cg[si],
                slot_prompts[si],
                tasklist_path_in,
            ],
            go_outputs,
            show_progress="hidden",
        )

    def unzip_zip(
        root_str: str,
        output_dest_str: str,
        zip_path: str | list | None,
        title_text: str,
    ):
        dd, st = refresh_products(root_str, output_dest_str)
        zp = zip_path
        if isinstance(zp, (list, tuple)) and zp:
            zp = zp[0]
        if zp is None or (isinstance(zp, str) and not zp.strip()):
            return "请先选择 ZIP 文件。", dd, st
        if not (title_text or "").strip():
            return "请先填写「商品标题」再解压。", dd, st
        from gui.zip_util import extract_product_zip

        try:
            target_root = _effective_output_root(root_str, output_dest_str, default_root)
            extract_product_zip(Path(str(zp)), target_root, title_text)
            msg = f"解压完成 → {target_root}（商品列表已刷新）。"
        except Exception as e:
            msg = f"解压失败: {e}"
        dd, st = refresh_products(root_str, output_dest_str)
        return msg, dd, st

    unzip_btn.click(
        unzip_zip,
        [root_in, output_dest_in, zip_in, title_in],
        [zip_msg, product_dd, status_refresh],
    )

    def initial_refresh(root_str: str):
        dd_upd, st = refresh_products(root_str)
        root = Path((root_str or default_root).strip()).expanduser().resolve()
        empty_st = [None] * SLOT_COUNT
        empty_sl = _empty_slot_updates_full()
        if not root.is_dir():
            return dd_upd, st, "", [], [], gr.update(value=[]), gr.update(choices=[], value=None), gr.update(choices=[], value=None), None, "", empty_st, *empty_sl
        prods = discover_under_root(root)
        if not prods:
            return dd_upd, st, "", [], [], gr.update(value=[]), gr.update(choices=[], value=None), gr.update(choices=[], value=None), None, "", empty_st, *empty_sl
        p0 = prods[0]
        title = _read_title_file(p0)
        gal = gallery_images_from_meta(p0)
        preview = _preview_gallery_items(p0)
        st0, upd = _slot_state_and_updates_full(p0)
        rows = _size_review_rows([p0])
        choices, value, size_preview = _size_candidates_for_product(str(root), "", p0.name)
        return dd_upd, st, title, gal, preview, gr.update(value=rows), gr.update(choices=[p0.name], value=p0.name), gr.update(choices=choices, value=value), size_preview, "已识别 1 个商品的尺寸图候选。", st0, *upd

    initial_load_outputs = [
        product_dd,
        status_refresh,
        title_in,
        gallery,
        preview_gallery,
        size_review_df,
        size_product_dd,
        size_candidate_dd,
        size_preview_img,
        size_review_msg,
        slot_state,
        *slot_flat_outputs,
    ]

    return GenerateTabResult(
        root_in=root_in,
        product_dd=product_dd,
        status_refresh=status_refresh,
        title_in=title_in,
        gallery=gallery,
        slot_state=slot_state,
        initial_refresh=initial_refresh,
        initial_load_outputs=initial_load_outputs,
    )
