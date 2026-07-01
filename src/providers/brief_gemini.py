"""Gemini LLM brief generator (stub)."""

from __future__ import annotations

from src.providers.brief_base import BriefGenerator


class BriefGeminiGenerator(BriefGenerator):
    """TODO: call Gemini API for JSON briefs per type."""

    def __init__(self) -> None:
        pass

    def generate_briefs(
        self,
        product_title: str,
        reference_images: list[bytes],
        counts: dict[str, int],
    ) -> dict[str, list[str]]:
        raise NotImplementedError(
            "BriefGeminiGenerator: implement API in brief_gemini.py (use BRIEF_LLM_API_KEY)."
        )
