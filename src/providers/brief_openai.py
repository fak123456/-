"""OpenAI LLM brief generator (stub)."""

from __future__ import annotations

from src.providers.brief_base import BriefGenerator


class BriefOpenAIGenerator(BriefGenerator):
    """TODO: call OpenAI chat completions (e.g. gpt-4o-mini) with title + optional refs."""

    def __init__(self) -> None:
        pass

    def generate_briefs(
        self,
        product_title: str,
        reference_images: list[bytes],
        counts: dict[str, int],
    ) -> dict[str, list[str]]:
        raise NotImplementedError(
            "BriefOpenAIGenerator: implement API in brief_openai.py (use BRIEF_LLM_API_KEY)."
        )
