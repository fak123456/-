"""History tab: recent runs from GUI + scan meta.json under project root."""

from __future__ import annotations

import json
from pathlib import Path

import gradio as gr

from gui.config_store import CONFIG_DIR
from gui.history_store import HISTORY_FILE, list_runs
from gui.paths import app_workdir
from gui.runner import discover_under_root


def _scan_meta(root: Path, limit: int = 30) -> list[list[str]]:
    rows: list[list[str]] = []
    if not root.is_dir():
        return rows
    for p in discover_under_root(root):
        meta_path = p / "output" / "meta.json"
        if not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        n_ok = sum(1 for im in meta.get("images", []) if im.get("status") == "ok")
        n_err = sum(1 for im in meta.get("images", []) if im.get("status") == "error")
        rows.append(
            [
                p.name,
                str(meta.get("counts_total", "")),
                f"{n_ok} ok / {n_err} err",
                str(p.resolve()),
            ]
        )
        if len(rows) >= limit:
            break
    return rows


def build_history_tab() -> None:
    # See gui/pages/generate.py for why this uses app_workdir() instead of
    # PROJECT_ROOT (PyInstaller's _MEI temp dir would otherwise show up here).
    root_in = gr.Textbox(label="项目根目录", value=str(app_workdir()))

    def refresh_gui_history():
        items = list_runs(80)
        lines = []
        for it in reversed(items):
            lines.append(
                f"- `{it.get('ts','')}` **{it.get('product','')}** "
                f"{it.get('status','')} ({it.get('image_provider','')}) "
                f"{it.get('error') or ''}"
            )
        return "\n".join(lines) if lines else "（暂无 GUI 运行记录）"

    def refresh_meta_md(root_str: str):
        root = Path(root_str.strip() or str(app_workdir())).expanduser().resolve()
        data = _scan_meta(root, limit=40)
        if not data:
            return "（未找到含 output/meta.json 的商品）"
        lines = ["| 商品 | 张数 | 状态 | 路径 |", "| --- | --- | --- | --- |"]
        for r in data:
            lines.append(f"| {r[0]} | {r[1]} | {r[2]} | `{r[3]}` |")
        return "\n".join(lines)

    gr.Markdown(f"### 本机运行记录（{HISTORY_FILE}）")
    gui_hist = gr.Markdown()
    b1 = gr.Button("刷新 GUI 历史")
    gr.Markdown("### 各商品 output/meta.json（抽样）")
    meta_md = gr.Markdown()
    b2 = gr.Button("扫描商品 meta")

    b1.click(refresh_gui_history, None, [gui_hist])
    b2.click(refresh_meta_md, [root_in], [meta_md])
