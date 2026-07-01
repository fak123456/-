"""Edit prompt templates (markdown + briefs.yaml) under resolved prompts dir."""

from __future__ import annotations

import shutil
from pathlib import Path

import gradio as gr

from gui.paths import bundle_root, is_frozen, resolved_prompts_dir


def _list_rel_files() -> list[str]:
    pd = resolved_prompts_dir()
    if not pd.is_dir():
        return []
    out: list[str] = []
    for pat in ("*.md", "*.yaml", "*.yml"):
        for p in sorted(pd.glob(pat)):
            if p.name.startswith("."):
                continue
            if "_defaults" in p.parts:
                continue
            out.append(p.name)
    return sorted(set(out))


def build_templates_tab() -> None:
    file_dd = gr.Dropdown(label="选择文件", choices=_list_rel_files(), allow_custom_value=True)
    refresh_files = gr.Button("刷新文件列表")
    load_btn = gr.Button("加载所选文件")
    editor = gr.Code(label="内容", language="markdown")
    status = gr.Markdown("")
    save_btn = gr.Button("保存", variant="primary")
    restore_btn = gr.Button("从默认副本恢复当前文件")

    def refresh():
        return gr.update(choices=_list_rel_files())

    def load_file(name: str | None):
        if not name:
            return "", "请选择文件"
        p = resolved_prompts_dir() / name
        if not p.is_file():
            return "", f"文件不存在: {p}"
        return p.read_text(encoding="utf-8"), f"已加载: {p}"

    def save_file(name: str | None, text: str):
        if not name:
            return "未选择文件"
        p = resolved_prompts_dir() / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text or "", encoding="utf-8")
        return f"已保存: {p}"

    def restore_file(name: str | None):
        if not name:
            return "", "未选择文件"
        dst = resolved_prompts_dir() / name
        defaults = resolved_prompts_dir() / "_defaults" / name
        if defaults.is_file():
            shutil.copy2(defaults, dst)
            return dst.read_text(encoding="utf-8"), f"已从 prompts/_defaults 恢复: {dst}"
        if is_frozen():
            src = bundle_root() / "prompts" / "_defaults" / name
            if not src.is_file():
                src = bundle_root() / "prompts" / name
            if not src.is_file():
                return "", f"内置目录缺少该文件: {src}"
            shutil.copy2(src, dst)
            return dst.read_text(encoding="utf-8"), f"已从内置副本恢复: {dst}"
        return (
            "",
            "未找到 prompts/_defaults 下的同名备份。请确认仓库内已包含 prompts/_defaults/。",
        )

    refresh_files.click(refresh, None, [file_dd])
    load_btn.click(load_file, [file_dd], [editor, status])
    save_btn.click(save_file, [file_dd, editor], [status])
    restore_btn.click(restore_file, [file_dd], [editor, status])

    gr.Markdown(
        "说明：打包版首次运行会在程序目录下复制一份 `prompts/` 供修改；"
        "「从默认副本恢复」用仓库中的 `prompts/_defaults/` 覆盖当前文件（开发与打包版均可用）。"
    )
