"""OpenAI GPT Image provider (stub — implement when API key is available)."""

from __future__ import annotations

from src.providers.base import ImageProvider


class OpenAIImageProvider(ImageProvider):
    """
    TODO: Implement with OpenAI Images API (e.g. gpt-image-1).

    - Use client.images.edit or generate with reference image bytes.
    - Map size string to API-supported dimensions if needed.
    """

    def __init__(self, api_key: str = "", model_id: str = "gpt-image-1", **_kwargs: str) -> None:
        self._api_key = api_key
        self._model_id = model_id

    def generate(
        self,
        prompt: str,
        reference_images: list[bytes],
        size: str = "native",
        seed: int | None = None,
        *,
        api_image_size: str | None = None,
    ) -> bytes:
        _ = seed, api_image_size, self._api_key, self._model_id
        raise NotImplementedError("OpenAIImageProvider: fill in API calls in openai_provider.py")
