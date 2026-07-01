"""Settings tab: API key, provider, Xais model, API base."""

from __future__ import annotations

import os
import threading
from typing import Any

import gradio as gr
import requests

from gui.config_store import load_gui_config, save_gui_config
from src.config import load_settings


_EXIT_PAGE_JS = """
() => {
    try {
        document.body.innerHTML = '<div style="padding:80px;text-align:center;font-family:system-ui;color:#333"><div style="font-size:28px;margin-bottom:16px">程序已退出</div><div style="font-size:16px;color:#666">可以关掉这个浏览器页面了。</div></div>';
        document.title = '程序已退出';
        setTimeout(() => { try { window.close(); } catch(e) {} }, 600);
    } catch(e) {}
    return [];
}
"""


def _delayed_exit() -> None:
    """Give the click round-trip ~0.8s to land, then take down the process.

    A small delay lets the browser receive the click ACK and finish running
    the page-replacement JS, so the user sees the friendly '程序已退出'
    screen instead of a half-loaded page that just dies on the next request.
    """
    threading.Timer(0.8, lambda: os._exit(0)).start()


def _fetch_xais_models(api_key: str, api_base: str) -> list[str]:
    base = (api_base or "https://sg2.dchai.cn").rstrip("/")
    if not api_key.strip():
        return []
    try:
        r = requests.get(
            f"{base}/v1/models",
            headers={"Authorization": f"Bearer {api_key.strip()}"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        rows = data.get("data") or []
        return [str(x.get("id")) for x in rows if isinstance(x, dict) and x.get("id")]
    except Exception:
        return []


def build_settings_tab() -> None:
    base = load_settings()
    cfg = load_gui_config()

    def save_ui(api_key, provider, xais_mid, xais_base, doubao_mid, doubao_base, shi_mid, shi_base, gem_mid):
        data: dict[str, Any] = {
            "image_api_key": str(api_key or "").strip(),
            "image_provider": str(provider or "xais").strip().lower(),
            "xais_model_id": str(xais_mid or "").strip(),
            "xais_api_base": str(xais_base or "").strip(),
            "doubao_model_id": str(doubao_mid or "").strip(),
            "doubao_api_base": str(doubao_base or "").strip(),
            "shiyun_model_id": str(shi_mid or "").strip(),
            "shiyun_api_base": str(shi_base or "").strip(),
            "gemini_model_id": str(gem_mid or "").strip(),
        }
        save_gui_config(data)
        from gui.config_store import CONFIG_FILE
        return (
            f"已保存到 {CONFIG_FILE}（密钥已简单编码存储）。"
            "空闲自动退出已关闭：程序只会在终端 Ctrl+C 或点击下方「立即退出程序」时结束。"
        )

    def refresh_models(api_key, api_base):
        ids = _fetch_xais_models(api_key, api_base)
        if not ids:
            return gr.update(choices=[], value=None), "无法拉取模型列表（检查 Key 与网络）"
        return gr.update(choices=ids, value=ids[0] if ids else None), f"已加载 {len(ids)} 个模型"

    gr.Markdown(
        "### API 与模型（保存后「生成」页立即生效）\n\n"
        "**IMAGE_PROVIDER** 决定调用哪条线路，下面三个 Accordion 各管一条：\n"
        "- `xais`：Xais 中转站（sg2.dchai.cn），用 Nano_Banana_Pro 系列\n"
        "- `shiyun`：诗云中转站（shiyunapi.com），用 gemini-2.5-flash-image 等\n"
        "- `gemini`：Google 官方直连，**国内需要科学上网**\n\n"
        "切换 provider 后只需要填**对应那一组**的字段，其他组的旧值留着不影响。"
    )
    with gr.Row():
        api_key = gr.Textbox(
            label="IMAGE_API_KEY（当前所选 provider 的 Token / Key）",
            type="password",
            value=cfg.get("image_api_key", ""),
            scale=5,
        )
        api_key_eye_btn = gr.Button(
            "👁 显示", scale=1, min_width=80, size="sm",
        )
        provider = gr.Dropdown(
            label="IMAGE_PROVIDER",
            choices=["xais", "shiyun", "placeholder", "gemini", "openai", "doubao"],
            value=str(cfg.get("image_provider", "xais") or "xais"),
            scale=3,
        )
    api_key_visible_state = gr.State(False)

    def _toggle_api_key_visibility(visible_now: bool):
        """Flip the api_key textbox between password / text mode.

        Returning a fresh ``gr.update(type=...)`` is the only way Gradio lets
        us re-type a Textbox after construction; combined with a State holder
        the button label stays in sync with the textbox so a refreshed page
        can't end up with "👁 显示" lit while the secret is already visible.
        """
        new_visible = not bool(visible_now)
        return (
            gr.update(type="text" if new_visible else "password"),
            gr.update(value=("🙈 隐藏" if new_visible else "👁 显示")),
            new_visible,
        )

    api_key_eye_btn.click(
        _toggle_api_key_visibility,
        [api_key_visible_state],
        [api_key, api_key_eye_btn, api_key_visible_state],
        show_progress="hidden",
    )

    with gr.Accordion("Xais 配置（IMAGE_PROVIDER=xais）", open=False):
        api_base = gr.Textbox(
            label="XAIS_API_BASE",
            value=str(cfg.get("xais_api_base", base.xais_api_base) or ""),
            info="Xais 官方域名，默认 https://sg2.dchai.cn",
        )
        mid = str(cfg.get("xais_model_id", base.xais_model_id) or "").strip() or "Nano_Banana_Pro_2K_0"
        model_id = gr.Dropdown(
            label="XAIS_MODEL_ID",
            choices=[mid],
            value=mid,
            allow_custom_value=True,
            info="Xais 私有命名：Nano_Banana_Pro_2K_5 / Nano_Banana_Pro_2K_0 / Nano_Banana_Pro_4K_0 等",
        )
        ref_models = gr.Button("从 Xais 拉取模型列表")
        model_status = gr.Markdown("")

    with gr.Accordion("字节/豆包 Seedream 配置（IMAGE_PROVIDER=doubao）", open=False):
        doubao_base_val = str(cfg.get("doubao_api_base", base.doubao_api_base) or "").strip() or "https://ark.cn-beijing.volces.com/api/v3"
        doubao_api_base = gr.Textbox(
            label="DOUBAO_API_BASE",
            value=doubao_base_val,
            info="火山方舟默认 https://ark.cn-beijing.volces.com/api/v3，也可填写兼容中转地址",
        )
        doubao_mid_val = str(cfg.get("doubao_model_id", base.doubao_model_id) or "").strip() or "doubao-seedream-5-0-260128"
        doubao_model_id = gr.Dropdown(
            label="DOUBAO_MODEL_ID",
            choices=[
                "doubao-seedream-5-0-260128",
                "doubao-seedream-4-5-251128",
                "doubao-seedream-4-0-250828",
                doubao_mid_val,
            ],
            value=doubao_mid_val,
            allow_custom_value=True,
            info="填写你已开通的 Seedream 模型 ID；具体以火山方舟控制台为准",
        )

    with gr.Accordion("诗云配置（IMAGE_PROVIDER=shiyun）", open=True):
        shi_base_val = str(cfg.get("shiyun_api_base", base.shiyun_api_base) or "").strip() or "https://shiyunapi.com"
        shiyun_api_base = gr.Textbox(
            label="SHIYUN_API_BASE",
            value=shi_base_val,
            info="默认 https://shiyunapi.com，一般不用改",
        )
        shi_mid_val = str(cfg.get("shiyun_model_id", base.shiyun_model_id) or "").strip() or "gemini-2.5-flash-image"
        shiyun_model_id = gr.Dropdown(
            label="SHIYUN_MODEL_ID",
            choices=[
                "gemini-2.5-flash-image",
                "gemini-2.5-flash-image-preview",
                "gemini-3-pro-image-preview",
                shi_mid_val,
            ],
            value=shi_mid_val,
            allow_custom_value=True,
            info="标准命名：gemini-2.5-flash-image（Nano Banana）。完整列表见 https://shiyunapi.com/pricing",
        )
        gr.Markdown(
            "Token 创建：https://shiyunapi.com/console/token  ·  "
            "充值：1 元 = 1 美元额度  ·  "
            "接口文档：https://shiyunapi.apifox.cn/"
        )

    with gr.Accordion("Gemini 直连配置（IMAGE_PROVIDER=gemini）", open=False):
        gem_mid_val = str(cfg.get("gemini_model_id", base.gemini_model_id) or "").strip() or "gemini-2.5-flash-image"
        gemini_model_id = gr.Dropdown(
            label="GEMINI_MODEL_ID",
            choices=[
                "gemini-2.5-flash-image",
                "gemini-2.5-flash-image-preview",
                "gemini-3-pro-image-preview",
                gem_mid_val,
            ],
            value=gem_mid_val,
            allow_custom_value=True,
            info="Google AI Studio 模型 ID；URL 由 google-genai SDK 内部固定，无需配置。需要科学上网。",
        )

    save_btn = gr.Button("保存设置", variant="primary")
    save_msg = gr.Markdown("")
    gr.Markdown(
        "**空闲自动退出已关闭。** 程序只在以下情况结束："
        "①启动它的终端按 `Ctrl+C`（仅命令行启动时）；"
        "②点击下方「立即退出程序」按钮，浏览器页面会同时被替换为"
        "「程序已退出」提示，可以放心关掉网页。"
    )
    exit_now_btn = gr.Button("立即退出程序", variant="stop")

    ref_models.click(refresh_models, [api_key, api_base], [model_id, model_status])
    save_btn.click(
        save_ui,
        [api_key, provider, model_id, api_base, doubao_model_id, doubao_api_base, shiyun_model_id, shiyun_api_base, gemini_model_id],
        [save_msg],
    )
    exit_now_btn.click(_delayed_exit, inputs=None, outputs=None, js=_EXIT_PAGE_JS)

    gr.Markdown("首次打开请点击「保存设置」。也可继续在本机 `.env` 中配置，GUI 保存的值会覆盖同名项。")
