"""Abstract brief generator for per-product differentiated prompts."""

from __future__ import annotations

from abc import ABC, abstractmethod


class BriefGenerator(ABC):
    """Produce per-type brief strings (one per output image index)."""

    @abstractmethod
    def generate_briefs(
        self,
        product_title: str,
        reference_images: list[bytes],
        counts: dict[str, int],
    ) -> dict[str, list[str]]:
        """Return e.g. {\"scene\": [\"...\", \"...\"], ...} with len == counts[type]."""
