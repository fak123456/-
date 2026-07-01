"""Launch Gradio web UI (local only). Run: python -m gui.app"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "0")
os.environ["NO_PROXY"] = (os.environ.get("NO_PROXY", "") + ",127.0.0.1,localhost,::1").lstrip(",")
os.environ["no_proxy"] = (os.environ.get("no_proxy", "") + ",127.0.0.1,localhost,::1").lstrip(",")


def _redirect_stdio_if_headless() -> None:
    """When packaged as a no-console exe (``console=False``), Windows sets
    ``sys.stdout`` / ``sys.stderr`` to ``None``. Any ``print(...)`` or a
    Loguru ``stderr`` sink would then raise ``AttributeError`` on first
    write and the exe would crash silently. Redirect both to a rolling
    log file next to the exe before anything else (Gradio, Loguru, etc.)
    can grab the originals."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        if getattr(sys, "frozen", False):
            log_dir = os.path.dirname(os.path.abspath(sys.executable))
        else:
            log_dir = os.getcwd()
        log_path = os.path.join(log_dir, "AmazonImgGUI.log")
        f = open(log_path, "a", buffering=1, encoding="utf-8", errors="replace")
    except OSError:
        f = open(os.devnull, "w", encoding="utf-8", errors="replace")
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f


_redirect_stdio_if_headless()

import threading
import webbrowser

import gradio as gr

try:
    from gradio_client import utils as _gc_utils

    _orig_get_type = _gc_utils.get_type
    _orig_json_to_py = _gc_utils._json_schema_to_python_type

    def _safe_get_type(schema):
        if not isinstance(schema, dict):
            return "Any"
        return _orig_get_type(schema)

    def _safe_json_to_py(schema, defs=None):
        if not isinstance(schema, dict):
            return "Any"
        return _orig_json_to_py(schema, defs)

    _gc_utils.get_type = _safe_get_type
    _gc_utils._json_schema_to_python_type = _safe_json_to_py
except Exception:
    pass

try:
    from gradio import networking as _gnet

    def _socket_url_ok(url: str) -> bool:
        """Replace Gradio's httpx-based localhost check with a stdlib socket connect
        so that system HTTP/HTTPS proxies cannot break it on Windows."""
        import socket
        import time
        from urllib.parse import urlparse

        u = urlparse(url)
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if u.scheme == "https" else 80)
        for _ in range(15):
            try:
                with socket.create_connection((host, port), timeout=1.0):
                    return True
            except OSError:
                time.sleep(0.3)
        return False

    _gnet.url_ok = _socket_url_ok
except Exception:
    pass

from gui.browser_watchdog import (
    HEARTBEAT_JS,
    browser_heartbeat_middleware,
    start_browser_watchdog,
)
from gui.idle_watchdog import exit_process_now  # noqa: F401  (kept for the manual-exit button)
from gui.pages.generate import build_generate_tab, is_generation_busy
from gui.pages.history import build_history_tab
from gui.pages.settings import build_settings_tab
from gui.pages.templates import build_templates_tab
from gui.paths import ensure_user_prompts, is_frozen
from src.utils.logger import configure_logging


def create_app() -> gr.Blocks:
    if is_frozen():
        ensure_user_prompts()
    configure_logging(verbose=False)

    with gr.Blocks(title="亚马逊电商图组生成器", js=HEARTBEAT_JS) as demo:
        gr.Markdown(
            "# 亚马逊电商图组生成器\n\n"
            "在本地运行；请在「设置」中填写自己的 API Key。生成结果写入各商品文件夹下的 `output/`。"
        )
        with gr.Tabs():
            with gr.Tab("生成"):
                generate_tab = build_generate_tab()
            with gr.Tab("设置"):
                build_settings_tab()
            with gr.Tab("历史"):
                build_history_tab()
            with gr.Tab("提示词"):
                build_templates_tab()

        demo.load(
            generate_tab.initial_refresh,
            inputs=[generate_tab.root_in],
            outputs=generate_tab.initial_load_outputs,
        )

    return demo


def _resolved_port(default: int = 7860) -> int:
    """Pick the listen port. Env var ``GRADIO_SERVER_PORT`` wins so multi-instance
    .bat launchers can each open on a distinct local port."""
    raw = os.environ.get("GRADIO_SERVER_PORT", "").strip()
    if raw.isdigit():
        p = int(raw)
        if 1 <= p <= 65535:
            return p
    return default


def launch(port: int | None = None) -> None:
    if port is None:
        port = _resolved_port()
    demo = create_app()
    demo.queue()
    url = f"http://127.0.0.1:{port}"
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    # Auto-exit when every browser tab has been closed (heartbeat timeout).
    # Skips the kill while a generation is mid-flight so an accidental close
    # doesn't ruin a batch run; the next tick after it finishes will exit.
    # 60 s threshold gives the user enough room to mis-click the tab close
    # without losing the running instance — but still guarantees no zombie
    # process is left around once they've actually finished for the day.
    start_browser_watchdog(
        missing_threshold=60.0,
        tick_seconds=3.0,
        is_busy=is_generation_busy,
    )
    demo.launch(
        server_name="127.0.0.1",
        server_port=port,
        inbrowser=False,
        share=False,
        show_error=True,
        app_kwargs={"middleware": [browser_heartbeat_middleware()]},
    )


if __name__ == "__main__":
    launch()
