"""Xais (dchai.cn) relay provider via OpenAI-compatible /v1/chat/completions.

Docs reference (Xais AI Workshop, OpenAI-compatible mode):
  POST {api_base}/v1/chat/completions
  Authorization: Bearer ${xtoken}
  body = {
      "model": "Nano_Banana_Pro_2K_0" | "Nano_Banana_2_2K_0" | ...,
      "messages": [{
          "role": "user",
          "content": [
              {"type": "text", "text": "<prompt + aspect ratio hint>"},
              {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}},
              ...
          ]
      }]
  }
Response choices[0].message.content is a markdown image: "![image](<png_url>)".
The URL is usually an https:// link to a PNG, but some relays (e.g. 诗云)
inline the bytes as a `data:image/png;base64,...` URI instead; we handle both.
"""

from __future__ import annotations

import base64
import binascii
import re
from typing import Any

import requests

from src.providers.base import ImageProvider

_DEFAULT_API_BASE = "https://sg2.dchai.cn"
_DEFAULT_MODEL = "Nano_Banana_Pro_2K_0"
_DEFAULT_TIMEOUT = 300  # seconds; per Xais Java sample, 5 minutes is typical

_MARKDOWN_IMG_RE = re.compile(r"!\[[^\]]*\]\(\s*([^)\s]+)\s*\)")


def _ratio_for_size(size: str) -> str:
    """Map our output_size convention to Xais aspect ratio strings."""
    s = (size or "").strip().lower().replace(" ", "")
    if not s or s == "native":
        return "1:1"
    if "x" in s:
        try:
            w, h = s.split("x", 1)
            wi, hi = int(w), int(h)
            if wi == hi:
                return "1:1"
            from math import gcd
            g = gcd(wi, hi) or 1
            return f"{wi // g}:{hi // g}"
        except ValueError:
            return "1:1"
    return "1:1"


def _detect_mime(blob: bytes) -> str:
    """Lightweight signature sniff for jpeg vs png; default jpeg."""
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if blob[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    return "image/jpeg"


def _build_content(prompt: str, reference_images: list[bytes], ratio: str) -> list[dict[str, Any]]:
    text = prompt.rstrip()
    if "aspect ratio" not in text.lower():
        text = f"{text}\n\nAspect ratio: {ratio}\n长宽比:{ratio}"
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for blob in reference_images:
        mime = _detect_mime(blob)
        b64 = base64.b64encode(blob).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            }
        )
    return content


def _extract_image_url(text: str) -> str | None:
    """Return first markdown image URL from chat completions text.

    Handles literal unicode escapes (\\u0026) as a safety net even though
    json.loads usually decodes them already.
    """
    m = _MARKDOWN_IMG_RE.search(text or "")
    if not m:
        return None
    url = m.group(1).strip()
    url = url.replace("\\u0026", "&").replace("\\u003d", "=").replace("\\u002f", "/")
    return url or None


def _short_url(url: str, n: int = 100) -> str:
    """Truncate a URL for safe inclusion in error messages.

    Some relays (e.g. 诗云) inline images as `data:image/png;base64,...`
    URIs that can be megabytes long, which would otherwise be dumped verbatim
    into log lines via exception messages.
    """
    s = url or ""
    if len(s) <= n:
        return s
    return f"{s[:n]}...<truncated, total {len(s)} chars>"


def _decode_data_uri(uri: str) -> bytes:
    """Decode a data:<mime>;base64,<payload> URI into raw bytes.

    Raises RuntimeError with a short message if the URI is malformed; the
    base64 payload itself is never echoed back to the caller.
    """
    if "," not in uri:
        raise RuntimeError(
            f"data URI missing comma separator (prefix={_short_url(uri, 60)})"
        )
    header, payload = uri.split(",", 1)
    if ";base64" not in header.lower():
        raise RuntimeError(
            f"data URI is not base64-encoded (header={header[:60]!r})"
        )
    try:
        return base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError) as e:
        raise RuntimeError(
            f"data URI base64 decode failed: {e} (header={header[:60]!r}, "
            f"payload_len={len(payload)})"
        ) from e


class XaisImageProvider(ImageProvider):
    """Xais relay (dchai.cn) image generation via OpenAI-compatible chat completions.

    Subclasses (e.g. ShiyunImageProvider) override ``_provider_label`` so error
    messages reflect the actual relay name instead of the literal "Xais".

    Recommended models:
      - Nano_Banana_Pro_2K_5  (cheapest 2K, ~0.10 credit/image, line 5)
      - Nano_Banana_Pro_2K_0  (T0 fastest 2K, ~0.15 credit/image)
      - Nano_Banana_Pro_4K_0  (4K, ~0.18 credit/image)
      - Nano_Banana_2_2K_0    (NB2 2K)
      - Nano_Banana_2_4K_0    (NB2 4K)
    """

    #: Human-readable provider name; subclasses override to show e.g. "Shiyun"
    #: instead of the inherited literal "Xais" in error messages and logs.
    _provider_label: str = "Xais"

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
                "IMAGE_API_KEY is empty for the OpenAI-compatible chat relay "
                "provider. Set the Token issued by your relay station "
                "(e.g. Xais XTOKEN, Shiyun Token, etc.)."
            )
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
        ratio = _ratio_for_size(size)
        content = _build_content(prompt, reference_images, ratio)
        payload = {
            "model": self._model_id,
            "messages": [{"role": "user", "content": content}],
        }
        url = f"{self._api_base}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        label = self._provider_label
        resp = requests.post(url, headers=headers, json=payload, timeout=self._timeout)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"{label} chat/completions HTTP {resp.status_code}: {resp.text[:500]}"
            )
        try:
            data = resp.json()
        except ValueError as e:
            raise RuntimeError(f"{label} non-JSON response: {resp.text[:300]}") from e

        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise RuntimeError(f"{label} unexpected response shape: {data!r}") from e
        if not isinstance(text, str):
            raise RuntimeError(
                f"{label} content not a string: {type(text).__name__}: {text!r}"
            )

        image_url = _extract_image_url(text)
        if not image_url:
            raise RuntimeError(f"{label} response had no image URL: {text[:300]}")

        # Some relays (notably 诗云) inline the PNG as `data:image/png;base64,...`
        # in the markdown rather than returning an HTTPS URL. Detect that and
        # decode directly instead of trying to `requests.get(...)` a data URI
        # (which would raise InvalidSchema and dump the entire base64 payload
        # into the exception message).
        if image_url.startswith("data:"):
            return _decode_data_uri(image_url)

        img_resp = requests.get(image_url, timeout=self._timeout)
        if img_resp.status_code >= 400:
            raise RuntimeError(
                f"{label} image download HTTP {img_resp.status_code} from "
                f"{_short_url(image_url)}"
            )
        ctype = img_resp.headers.get("content-type", "").lower()
        if not (ctype.startswith("image/") or ctype.startswith("application/octet-stream") or not ctype):
            raise RuntimeError(
                f"{label} image url returned content-type={ctype!r}, "
                f"body[:200]={img_resp.content[:200]!r} (url={_short_url(image_url)})"
            )
        return img_resp.content
