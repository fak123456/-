"""Resolve per-image user briefs: product yaml > LLM > global templates."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from src.counts_config import ALLOWED_TYPES
from src.providers.brief_base import BriefGenerator


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def _load_global_lists(prompts_dir: Path) -> dict[str, list[str]]:
    path = prompts_dir / "briefs.yaml"
    raw = _load_yaml(path)
    out: dict[str, list[str]] = {}
    for t in ALLOWED_TYPES:
        v = raw.get(t) or []
        if not isinstance(v, list):
            raise ValueError(f"{path}: {t} must be a list")
        out[t] = [str(x) for x in v]
    return out


def _expand_modulo(items: list[str], need: int, *, empty_fallback: str) -> list[str]:
    if need <= 0:
        return []
    if not items:
        return [empty_fallback] * need
    return [str(items[i % len(items)]) for i in range(need)]


def _merge_product_preset(
    counts: dict[str, int],
    global_lists: dict[str, list[str]],
    product_briefs: dict[str, Any],
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Product preset: per-type list from product briefs or fallback global."""
    pb = product_briefs.get("briefs") or {}
    if not isinstance(pb, dict):
        raise ValueError("briefs.yaml: briefs must be a mapping")
    texts: dict[str, list[str]] = {}
    sources: dict[str, str] = {}
    for t in ALLOWED_TYPES:
        n = int(counts.get(t, 0))
        if t in pb and pb[t] is not None:
            lst = pb[t]
            if not isinstance(lst, list):
                raise ValueError(f"briefs.yaml: briefs.{t} must be a list")
            pl = [str(x) for x in lst]
            if pl:
                texts[t] = _expand_modulo(pl, n, empty_fallback="(empty product brief)")
                sources[t] = "preset_product"
            else:
                texts[t] = _expand_modulo(global_lists.get(t, []), n, empty_fallback="(no brief)")
                sources[t] = "preset_global"
        else:
            texts[t] = _expand_modulo(global_lists.get(t, []), n, empty_fallback="(no brief)")
            sources[t] = "preset_global"
    return texts, sources


def _write_product_briefs_cache(product_path: Path, briefs: dict[str, list[str]], source_note: str) -> None:
    data = {
        "brief_source": "llm",
        "brief_cache_note": source_note,
        "briefs": {k: v for k, v in briefs.items() if v},
    }
    product_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


@dataclass
class BriefResolution:
    """Per-type lists aligned with 1..count indices."""

    texts: dict[str, list[str]]
    per_type_source: dict[str, str]

    def get(self, type_name: str, index_1based: int) -> tuple[str, str]:
        lst = self.texts.get(type_name) or []
        src = self.per_type_source.get(type_name, "template_default")
        i = index_1based - 1
        if 0 <= i < len(lst):
            return lst[i], src
        return "(no brief)", "template_default"


def resolve_briefs(
    *,
    product_dir: Path,
    prompts_dir: Path,
    counts: dict[str, int],
    product_title: str,
    reference_images: list[bytes],
    brief_generator: BriefGenerator,
    persist_llm_cache: bool = True,
) -> BriefResolution:
    """
    Priority:
    1) 商品/briefs.yaml brief_source=preset with briefs: -> preset merge + global fallback
    2) 商品/briefs.yaml brief_source=llm -> use cache if complete else LLM + write yaml
    3) Else -> global prompts/briefs.yaml modulo
    """
    global_lists = _load_global_lists(prompts_dir)
    path = product_dir / "briefs.yaml"
    raw = _load_yaml(path)

    if not raw:
        texts = {
            t: _expand_modulo(global_lists.get(t, []), int(counts.get(t, 0)), empty_fallback="(no brief)")
            for t in ALLOWED_TYPES
        }
        sources = {t: "preset_global" for t in ALLOWED_TYPES}
        return BriefResolution(texts=texts, per_type_source=sources)

    mode = str(raw.get("brief_source", "preset")).strip().lower()
    if mode not in {"preset", "llm"}:
        raise ValueError(f"{path}: brief_source must be 'preset' or 'llm', got {mode!r}")

    if mode == "preset":
        texts, sources = _merge_product_preset(counts, global_lists, raw)
        return BriefResolution(texts=texts, per_type_source=sources)

    # llm
    need_counts = {t: int(counts.get(t, 0)) for t in ALLOWED_TYPES}

    def _cache_complete(cached: dict[str, list[str]]) -> bool:
        for t in ALLOWED_TYPES:
            n = need_counts[t]
            if n == 0:
                continue
            lst = cached.get(t) or []
            if len(lst) < n:
                return False
        return True

    cached_briefs = raw.get("briefs") or {}
    if isinstance(cached_briefs, dict):
        cached_lists: dict[str, list[str]] = {}
        for t in ALLOWED_TYPES:
            v = cached_briefs.get(t)
            if isinstance(v, list):
                cached_lists[t] = [str(x) for x in v]
            else:
                cached_lists[t] = []
    else:
        cached_lists = {t: [] for t in ALLOWED_TYPES}

    if _cache_complete(cached_lists):
        texts = {t: (cached_lists.get(t) or [])[: need_counts[t]] for t in ALLOWED_TYPES}
        sources = {t: "llm_cached" for t in ALLOWED_TYPES}
        return BriefResolution(texts=texts, per_type_source=sources)

    generated = brief_generator.generate_briefs(product_title, reference_images, need_counts)
    if not isinstance(generated, dict):
        raise TypeError("brief_generator must return dict[str, list[str]]")

    texts = copy.deepcopy(generated)
    for t in ALLOWED_TYPES:
        n = need_counts[t]
        cur = [str(x) for x in (texts.get(t) or [])]
        if len(cur) < n:
            cur = _expand_modulo(cur, n, empty_fallback="(no brief)")
        else:
            cur = cur[:n]
        texts[t] = cur

    if persist_llm_cache:
        _write_product_briefs_cache(path, texts, source_note="auto-cached from BriefGenerator")
    sources = {t: "llm_generated" for t in ALLOWED_TYPES}
    return BriefResolution(texts=texts, per_type_source=sources)
