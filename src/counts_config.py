"""Load and merge image_counts from defaults, config.yaml, product yaml, CLI."""

from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from src.config import PROJECT_ROOT

MAX_CONCURRENCY = 8
DEFAULT_CONCURRENCY = 5
DEFAULT_RETRY_FAILED_ROUNDS = 1

ALLOWED_TYPES: tuple[str, ...] = (
    "main",
    "scene",
    "multi",
    "size",
    "detail",
    "angle",
    "material",
)

DEFAULT_COUNTS: dict[str, int] = {
    "main": 1,
    "scene": 2,
    "multi": 1,
    "size": 1,
    "detail": 1,
    "angle": 1,
    "material": 1,
}

MAX_TOTAL_IMAGES = 20

TYPE_ORDER: tuple[str, ...] = ALLOWED_TYPES


@dataclass
class GenerationSettings:
    """Subset of config.yaml generation block."""

    size: str = "native"
    max_refs_per_call: int = 6
    gemini_native_image_size: str = "2K"
    concurrency: int = DEFAULT_CONCURRENCY
    retry_failed_rounds: int = DEFAULT_RETRY_FAILED_ROUNDS


@dataclass
class ResolvedCounts:
    """Effective counts + provenance + generation options."""

    image_counts: dict[str, int]
    counts_source: dict[str, str]
    counts_total: int
    generation: GenerationSettings = field(default_factory=GenerationSettings)


def _parse_cli_counts(s: str | None) -> dict[str, int] | None:
    if not s or not s.strip():
        return None
    out: dict[str, int] = {}
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(f"Invalid --counts segment (expected key=int): {part!r}")
        k, v = part.split("=", 1)
        k = k.strip().lower()
        if k not in ALLOWED_TYPES:
            raise ValueError(f"--counts: unknown key {k!r}; allowed: {list(ALLOWED_TYPES)}")
        v = v.strip()
        if not re.fullmatch(r"-?\d+", v):
            raise ValueError(f"Invalid count for {k!r}: {v!r}")
        out[k] = int(v)
    return out


def _load_yaml_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML root must be a mapping: {path}")
    return data


def _merge_generation_block(raw: dict[str, Any]) -> GenerationSettings:
    gen = raw.get("generation") or {}
    if not isinstance(gen, dict):
        raise ValueError("config: generation must be a mapping")
    size = str(gen.get("size", "native")).strip()
    mrc = int(gen.get("max_refs_per_call", 6))
    gnis = str(gen.get("gemini_native_image_size", "2K")).strip()
    concurrency = int(gen.get("concurrency", DEFAULT_CONCURRENCY))
    retry_failed_rounds = int(gen.get("retry_failed_rounds", DEFAULT_RETRY_FAILED_ROUNDS))
    if "IMAGE_CONCURRENCY" in os.environ:
        concurrency = int(os.environ["IMAGE_CONCURRENCY"].strip())
    if "IMAGE_RETRY_ROUNDS" in os.environ:
        retry_failed_rounds = int(os.environ["IMAGE_RETRY_ROUNDS"].strip())
    if mrc < 1:
        raise ValueError("generation.max_refs_per_call must be >= 1")
    if concurrency < 1 or concurrency > MAX_CONCURRENCY:
        raise ValueError(
            f"generation.concurrency must be between 1 and {MAX_CONCURRENCY}, got {concurrency}"
        )
    if retry_failed_rounds < 0:
        raise ValueError(f"generation.retry_failed_rounds must be >= 0, got {retry_failed_rounds}")
    return GenerationSettings(
        size=size,
        max_refs_per_call=mrc,
        gemini_native_image_size=gnis,
        concurrency=concurrency,
        retry_failed_rounds=retry_failed_rounds,
    )


def _validate_counts_dict(counts: dict[str, int], *, label: str) -> None:
    unknown = set(counts) - set(ALLOWED_TYPES)
    if unknown:
        raise ValueError(f"{label}: unknown image_types {sorted(unknown)}; allowed: {list(ALLOWED_TYPES)}")
    for k, v in counts.items():
        if v < 0:
            raise ValueError(f"{label}: negative count for {k}: {v}")
    total = sum(counts[t] for t in ALLOWED_TYPES)
    if total == 0:
        raise ValueError(f"{label}: total image count is 0 (forbidden)")
    if total > MAX_TOTAL_IMAGES:
        raise ValueError(
            f"{label}: total images {total} exceeds MAX_TOTAL_IMAGES={MAX_TOTAL_IMAGES}"
        )


def _extract_image_counts(raw: dict[str, Any], *, label: str) -> dict[str, int]:
    ic = raw.get("image_counts")
    if ic is None:
        return {}
    if not isinstance(ic, dict):
        raise ValueError(f"{label}: image_counts must be a mapping")
    out: dict[str, int] = {}
    for k, v in ic.items():
        key = str(k).strip().lower()
        if key not in ALLOWED_TYPES:
            raise ValueError(f"{label}: unknown image_counts key {key!r}; allowed: {list(ALLOWED_TYPES)}")
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError(f"{label}: image_counts.{key} must be int")
        out[key] = int(v)
    return out


def merge_counts_layers(
    *,
    default: dict[str, int],
    global_yaml: dict[str, int] | None,
    product_yaml: dict[str, int] | None,
    cli: dict[str, int] | None,
) -> tuple[dict[str, int], dict[str, str]]:
    """Return merged counts and per-type source label."""
    merged = copy.deepcopy(default)
    sources: dict[str, str] = {k: "default" for k in ALLOWED_TYPES}

    def apply_layer(layer: dict[str, int] | None, source: str) -> None:
        if not layer:
            return
        for k, v in layer.items():
            kk = k.strip().lower()
            if kk in ALLOWED_TYPES:
                merged[kk] = int(v)
                sources[kk] = source

    apply_layer(global_yaml, "global_yaml")
    apply_layer(product_yaml, "product_yaml")
    apply_layer(cli, "cli")
    return merged, sources


def load_resolved_counts(
    *,
    product_dir: Path,
    cli_counts: str | None = None,
    config_path: Path | None = None,
    env_output_size: str | None = None,
) -> ResolvedCounts:
    """
    Merge: CLI --counts > 商品/counts.yaml > config.yaml > DEFAULT_COUNTS.

    env_output_size: if set (e.g. from IMAGE_OUTPUT_SIZE), overrides generation.size from yaml.
    """
    cfg_path = config_path or (PROJECT_ROOT / "config.yaml")
    root_raw = _load_yaml_file(cfg_path)
    gen = _merge_generation_block(root_raw)
    if env_output_size and env_output_size.strip():
        gen.size = env_output_size.strip()

    global_counts = _extract_image_counts(root_raw, label=str(cfg_path))

    prod_path = product_dir / "counts.yaml"
    prod_raw = _load_yaml_file(prod_path)
    product_counts = _extract_image_counts(prod_raw, label=str(prod_path))

    cli_layer = _parse_cli_counts(cli_counts)

    base = {k: int(DEFAULT_COUNTS[k]) for k in ALLOWED_TYPES}
    merged, sources = merge_counts_layers(
        default=base,
        global_yaml=global_counts or None,
        product_yaml=product_counts or None,
        cli=cli_layer,
    )
    _validate_counts_dict(merged, label="effective counts")
    total = sum(merged[t] for t in ALLOWED_TYPES)
    return ResolvedCounts(
        image_counts=merged,
        counts_source=sources,
        counts_total=total,
        generation=gen,
    )
