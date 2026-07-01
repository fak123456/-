"""Abstract image generation provider."""

from __future__ import annotations

from abc import ABC, abstractmethod


class ImageProvider(ABC):
    """Generate one image from prompt + optional reference images."""

    @abstractmethod
    def generate(
        self,
        prompt: str,
        reference_images: list[bytes],
        size: str = "native",
        seed: int | None = None,
        *,
        api_image_size: str | None = None,
    ) -> bytes:
        """Return image bytes (typically PNG). api_image_size: provider hint (e.g. Gemini 2K)."""
