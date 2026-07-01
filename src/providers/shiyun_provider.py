"""Shiyun (shiyunapi.com) relay provider via OpenAI-compatible /v1/chat/completions.

诗云 API 与 Xais 同样走 OpenAI 兼容的 chat completions 协议，请求 / 响应格式完全一致，
只是 base URL 和默认模型 ID 不同。所以这个类直接继承 ``XaisImageProvider`` 的请求逻辑，
仅 override ``__init__`` 改默认值、改报错文案。

文档:  https://shiyunapi.apifox.cn/
控制台: https://shiyunapi.com/console
拿 Token: https://shiyunapi.com/console/token
模型广场: https://shiyunapi.com/pricing

诗云常用图像模型（实际可用 ID 请以 pricing 页为准）:
  - gemini-2.5-flash-image           Nano Banana 标准版（推荐）
  - gemini-2.5-flash-image-preview   官方预览版
  - gemini-3-pro-image-preview       Nano Banana Pro，多轮编辑
  - gpt-image-2                      OpenAI 风格，文档显示走 /v1/images/generations
                                     可能不支持参考图，慎用
"""

from __future__ import annotations

from src.providers.xais_provider import XaisImageProvider

_DEFAULT_API_BASE = "https://shiyunapi.com"
_DEFAULT_MODEL = "gemini-2.5-flash-image"
_DEFAULT_TIMEOUT = 300


class ShiyunImageProvider(XaisImageProvider):
    """OpenAI-compatible chat-completions relay (shiyunapi.com)."""

    _provider_label = "Shiyun"

    def __init__(
        self,
        api_key: str,
        model_id: str = _DEFAULT_MODEL,
        *,
        api_base: str = _DEFAULT_API_BASE,
        timeout: int = _DEFAULT_TIMEOUT,
    ) -> None:
        if not api_key:
            raise ValueError(
                "IMAGE_API_KEY is empty for shiyun provider. "
                "请到 https://shiyunapi.com/console/token 创建 Token，"
                "并在 GUI「设置」或 .env 中填入 IMAGE_API_KEY。"
            )
        # Bypass XaisImageProvider.__init__'s own validation (which references xais.dchai.cn).
        self._api_key = api_key
        self._model_id = (model_id or _DEFAULT_MODEL).strip()
        self._api_base = (api_base or _DEFAULT_API_BASE).rstrip("/")
        self._timeout = max(30, int(timeout))
