"""Volcano / Doubao image API provider (stub)."""

from __future__ import annotations

from src.providers.base import ImageProvider


class DoubaoImageProvider(ImageProvider):
    """TODO: Implement with Volcano Engine (Doubao / Seedream) image API."""

    def __init__(self, api_key: str = "", model_id: str = "", api_base: str = "", **_kwargs: str) -> None:
        self._api_key = api_key
        self._model_id = model_id
        self._api_base = api_base

    def generate(
        self,
        prompt: str,
        reference_images: list[bytes],
        size: str = "native",
        seed: int | None = None,
        *,
        api_image_size: str | None = None,
    ) -> bytes:
        _ = seed, api_image_size, self._api_key, self._model_id, self._api_base
        raise NotImplementedError("DoubaoImageProvider: fill in API calls in doubao_provider.py")
