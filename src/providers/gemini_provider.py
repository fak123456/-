"""Google Gemini native image generation (Nano Banana / Gemini 3 image)."""

from __future__ import annotations

import io
from typing import Any

from PIL import Image

from src.providers.base import ImageProvider


def _lazy_genai():
    """Defer google.genai import to runtime so the package is optional."""
    try:
        from google import genai  # type: ignore[import-not-found]
        from google.genai import types  # type: ignore[import-not-found]
    except ImportError as e:
        raise RuntimeError(
            "google-genai 未安装。若需要 Gemini provider，请 `pip install google-genai`。"
        ) from e
    return genai, types


def _map_size_to_gemini_image_size(size: str, native_default: str) -> str:
    s = size.strip().lower().replace(" ", "")
    if s == "native":
        return native_default.strip() or "2K"
    mapping = {
        "512x512": "512",
        "1024x1024": "1K",
        "2048x2048": "2K",
        "4096x4096": "4K",
    }
    return mapping.get(s, "2K")


def _iter_response_parts(response: Any) -> list[Any]:
    parts: list[Any] = []
    if getattr(response, "parts", None):
        parts.extend(list(response.parts))
    cands = getattr(response, "candidates", None) or []
    for c in cands:
        content = getattr(c, "content", None)
        if content and getattr(content, "parts", None):
            parts.extend(list(content.parts))
    return parts


class GeminiImageProvider(ImageProvider):
    """
    Models (examples):
      - gemini-2.5-flash-image (Nano Banana)
      - gemini-3.1-flash-image-preview (Nano Banana 2)
      - gemini-3-pro-image-preview (Nano Banana Pro)
    """

    def __init__(
        self,
        api_key: str,
        model_id: str = "gemini-2.5-flash-image",
        *,
        native_api_image_size: str = "2K",
    ) -> None:
        if not api_key:
            raise ValueError("IMAGE_API_KEY is empty (Google AI Studio / Gemini API key).")
        self._model_id = model_id.strip()
        self._native_api_image_size = native_api_image_size.strip() or "2K"
        genai, _types = _lazy_genai()
        self._client = genai.Client(api_key=api_key)

    def _is_gemini3_image_model(self) -> bool:
        mid = self._model_id.lower()
        return "gemini-3" in mid and "image" in mid

    def generate(
        self,
        prompt: str,
        reference_images: list[bytes],
        size: str = "native",
        seed: int | None = None,
        *,
        api_image_size: str | None = None,
    ) -> bytes:
        _ = seed
        contents: list[Any] = [prompt]
        for blob in reference_images:
            contents.append(Image.open(io.BytesIO(blob)))

        _genai, types = _lazy_genai()
        config_kwargs: dict[str, Any] = {
            "response_modalities": ["TEXT", "IMAGE"],
        }
        if self._is_gemini3_image_model():
            token = api_image_size or _map_size_to_gemini_image_size(
                size, self._native_api_image_size
            )
            config_kwargs["image_config"] = types.ImageConfig(
                aspect_ratio="1:1",
                image_size=token,
            )

        response = self._client.models.generate_content(
            model=self._model_id,
            contents=contents,
            config=types.GenerateContentConfig(**config_kwargs),
        )

        for part in _iter_response_parts(response):
            inline = getattr(part, "inline_data", None)
            if inline is not None and getattr(inline, "data", None):
                data = inline.data
                if isinstance(data, (bytes, bytearray)):
                    return bytes(data)
            if hasattr(part, "as_image"):
                try:
                    im = part.as_image()
                    if im is not None:
                        buf = io.BytesIO()
                        im.save(buf, format="PNG")
                        return buf.getvalue()
                except Exception:
                    continue

        raise RuntimeError(
            f"Gemini returned no image bytes (model={self._model_id}). "
            f"Check API key, model id, and account image generation access."
        )
