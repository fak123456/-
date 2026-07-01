"""Volcano Ark / Doubao Seedream image API provider."""

from __future__ import annotations

import base64
import binascii
import re
from typing import Any

import requests

from src.providers.base import ImageProvider

_DEFAULT_API_BASE = "https://ark.cn-beijing.volces.com/api/v3"
_DEFAULT_MODEL = "doubao-seedream-5-0-260128"
_DEFAULT_TIMEOUT = 300
_MARKDOWN_IMG_RE = re.compile(r"!\[[^\]]*\]\(\s*([^)\s]+)\s*\)")


def _detect_mime(blob: bytes) -> str:
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if blob[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if blob.startswith(b"RIFF") and b"WEBP" in blob[:16]:
        return "image/webp"
    return "image/jpeg"


def _data_uri(blob: bytes) -> str:
    return f"data:{_detect_mime(blob)};base64,{base64.b64encode(blob).decode('ascii')}"


def _api_size(size: str) -> str:
    s = (size or "").strip().lower().replace(" ", "")
    if not s or s == "native":
        return "1024x1024"
    if "x" in s:
        return s
    return "1024x1024"


def _decode_data_uri(uri: str) -> bytes:
    if "," not in uri:
        raise RuntimeError("data URI missing comma separator")
    header, payload = uri.split(",", 1)
    if ";base64" not in header.lower():
        raise RuntimeError(f"data URI is not base64-encoded: {header[:80]!r}")
    try:
        return base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError) as e:
        raise RuntimeError(f"data URI base64 decode failed: {e}") from e


def _first_image_from_response(data: dict[str, Any]) -> bytes | str | None:
    rows = data.get("data")
    if isinstance(rows, list) and rows:
        first = rows[0]
        if isinstance(first, dict):
            for key in ("b64_json", "base64", "image"):
                val = first.get(key)
                if isinstance(val, str) and val:
                    if val.startswith("data:"):
                        return _decode_data_uri(val)
                    try:
                        return base64.b64decode(val, validate=False)
                    except Exception:
                        pass
            url = first.get("url")
            if isinstance(url, str) and url:
                return url
    images = data.get("images")
    if isinstance(images, list) and images:
        first = images[0]
        if isinstance(first, dict):
            url = first.get("url") or first.get("image_url")
            if isinstance(url, str) and url:
                return url
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        text = None
    if isinstance(text, str):
        m = _MARKDOWN_IMG_RE.search(text)
        if m:
            return m.group(1).strip()
    return None


class DoubaoImageProvider(ImageProvider):
    """Volcano Ark / Doubao Seedream image generation.

    The provider is intentionally OpenAI-compatible: callers fill API key,
    base URL, and model ID in the GUI/.env. It accepts multiple reference
    images as data URIs and supports common response shapes (base64 or URL).
    """

    def __init__(
        self,
        api_key: str = "",
        model_id: str = "",
        api_base: str = "",
        timeout: int = _DEFAULT_TIMEOUT,
        **_kwargs: str,
    ) -> None:
        if not api_key:
            raise ValueError("IMAGE_API_KEY is empty for doubao/Seedream provider.")
        self._api_key = api_key
        self._model_id = (model_id or _DEFAULT_MODEL).strip()
        self._api_base = (api_base or _DEFAULT_API_BASE).rstrip("/")
        self._timeout = max(30, int(timeout))

    def generate(
        self,
        prompt: str,
        reference_images: list[bytes],
        size: str = "native",
        seed: int | None = None,
        *,
        api_image_size: str | None = None,
    ) -> bytes:
        _ = seed, api_image_size
        payload: dict[str, Any] = {
            "model": self._model_id,
            "prompt": prompt,
            "size": _api_size(size),
            "response_format": "b64_json",
        }
        if reference_images:
            payload["image"] = [_data_uri(blob) for blob in reference_images]

        url = f"{self._api_base}/images/generations"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=self._timeout)
        if resp.status_code >= 400:
            raise RuntimeError(f"Doubao/Seedream HTTP {resp.status_code}: {resp.text[:500]}")
        try:
            data = resp.json()
        except ValueError as e:
            raise RuntimeError(f"Doubao/Seedream non-JSON response: {resp.text[:300]}") from e

        img = _first_image_from_response(data)
        if img is None:
            raise RuntimeError(f"Doubao/Seedream response had no image: {data!r}")
        if isinstance(img, bytes):
            return img
        if img.startswith("data:"):
            return _decode_data_uri(img)
        img_resp = requests.get(img, timeout=self._timeout)
        if img_resp.status_code >= 400:
            raise RuntimeError(f"Doubao/Seedream image download HTTP {img_resp.status_code}")
        return img_resp.content
