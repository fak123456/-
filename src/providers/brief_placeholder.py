"""Copy global briefs.yaml lists; no LLM call."""

from __future__ import annotations

import copy
from pathlib import Path

import yaml

from src.counts_config import ALLOWED_TYPES
from src.providers.brief_base import BriefGenerator


def _expand_list(items: list[str], need: int) -> list[str]:
    if need <= 0:
        return []
    if not items:
        return ["(no brief template)"] * need
    out: list[str] = []
    for i in range(need):
        out.append(str(items[i % len(items)]))
    return out


class BriefPlaceholderGenerator(BriefGenerator):
    """Use prompts/briefs.yaml and repeat modulo if N > template count."""

    def __init__(self, prompts_dir: Path) -> None:
        self._prompts_dir = prompts_dir
        self._path = prompts_dir / "briefs.yaml"

    def _load_global(self) -> dict[str, list[str]]:
        if not self._path.is_file():
            raise FileNotFoundError(f"Missing global briefs: {self._path}")
        raw = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
        out: dict[str, list[str]] = {}
        for k in ALLOWED_TYPES:
            v = raw.get(k) or []
            if not isinstance(v, list):
                raise ValueError(f"briefs.yaml: {k} must be a list of strings")
            out[k] = [str(x) for x in v]
        return out

    def generate_briefs(
        self,
        product_title: str,
        reference_images: list[bytes],
        counts: dict[str, int],
    ) -> dict[str, list[str]]:
        _ = product_title, reference_images
        base = self._load_global()
        result: dict[str, list[str]] = {}
        for t in ALLOWED_TYPES:
            n = int(counts.get(t, 0))
            result[t] = _expand_list(copy.deepcopy(base.get(t, [])), n)
        return result
