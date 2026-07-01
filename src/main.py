"""CLI entry for batch e-commerce image generation."""

from __future__ import annotations

import argparse
import dataclasses
import re
import sys
from pathlib import Path

from src.config import PROJECT_ROOT, Settings, load_settings
from src.counts_config import ALLOWED_TYPES, load_resolved_counts
from src.pipeline import process_product
from src.providers.base import ImageProvider
from src.providers.brief_base import BriefGenerator
from src.providers.brief_gemini import BriefGeminiGenerator
from src.providers.brief_openai import BriefOpenAIGenerator
from src.providers.brief_placeholder import BriefPlaceholderGenerator
from src.providers.doubao_provider import DoubaoImageProvider
from src.providers.gemini_provider import GeminiImageProvider
from src.providers.openai_provider import OpenAIImageProvider
from src.providers.placeholder import PlaceholderProvider
from src.providers.shiyun_provider import ShiyunImageProvider
from src.providers.xais_provider import XaisImageProvider
from src.utils.logger import configure_logging, get_logger

TITLE_FILE = "商品标题.txt"


def discover_products(root: Path) -> list[Path]:
    products: list[Path] = []
    for p in root.iterdir():
        if p.is_dir() and (p / TITLE_FILE).is_file():
            products.append(p)
    return sorted(products, key=_product_sort_key)


def _product_sort_key(p: Path) -> tuple[int, str]:
    m = re.match(r"^商品(\d+)$", p.name)
    if m:
        return (int(m.group(1)), p.name)
    return (9999, p.name)


def _parse_product_index(name: str) -> int | None:
    m = re.match(r"^商品(\d+)$", name)
    return int(m.group(1)) if m else None


def _parse_regen_targets(s: str | None) -> set[tuple[str, int]] | None:
    """Parse --regen string like 'scene_02,detail_01' into {(type, index), ...}."""
    if not s or not s.strip():
        return None
    out: set[tuple[str, int]] = set()
    pattern = re.compile(r"^([a-z]+)_(\d+)$")
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        m = pattern.fullmatch(part)
        if not m:
            raise ValueError(
                f"--regen segment must look like TYPE_NN (e.g. 'scene_02'), got: {part!r}"
            )
        t = m.group(1)
        idx = int(m.group(2))
        if t not in ALLOWED_TYPES:
            raise ValueError(
                f"--regen: unknown type {t!r}; allowed: {list(ALLOWED_TYPES)}"
            )
        if idx < 1:
            raise ValueError(f"--regen: index must be >= 1, got {idx} in {part!r}")
        out.add((t, idx))
    return out or None


def filter_products_by_range(
    products: list[Path],
    from_name: str | None,
    to_name: str | None,
) -> list[Path]:
    if not from_name and not to_name:
        return products
    fi = _parse_product_index(from_name) if from_name else None
    ti = _parse_product_index(to_name) if to_name else None
    if fi is None and from_name:
        raise ValueError(f"--from must match 商品N pattern, got: {from_name}")
    if ti is None and to_name:
        raise ValueError(f"--to must match 商品N pattern, got: {to_name}")

    out: list[Path] = []
    for p in products:
        idx = _parse_product_index(p.name)
        if idx is None:
            continue
        if fi is not None and idx < fi:
            continue
        if ti is not None and idx > ti:
            continue
        out.append(p)
    return out


def get_provider(settings: Settings) -> ImageProvider:
    """Instantiate image provider from settings."""
    name = settings.image_provider.strip().lower()
    if name == "placeholder":
        return PlaceholderProvider()
    if name == "openai":
        return OpenAIImageProvider(
            api_key=settings.image_api_key,
            model_id=settings.openai_model_id,
        )
    if name == "doubao":
        return DoubaoImageProvider(
            api_key=settings.image_api_key,
            model_id=settings.doubao_model_id,
            api_base=settings.doubao_api_base,
            timeout=settings.doubao_timeout,
        )
    if name == "gemini":
        return GeminiImageProvider(
            api_key=settings.image_api_key,
            model_id=settings.gemini_model_id,
            native_api_image_size="2K",
        )
    if name == "xais":
        return XaisImageProvider(
            api_key=settings.image_api_key,
            model_id=settings.xais_model_id,
            api_base=settings.xais_api_base,
            timeout=settings.xais_timeout,
        )
    if name == "shiyun":
        return ShiyunImageProvider(
            api_key=settings.image_api_key,
            model_id=settings.shiyun_model_id,
            api_base=settings.shiyun_api_base,
            timeout=settings.shiyun_timeout,
        )
    raise ValueError(f"Unknown IMAGE_PROVIDER: {name}")


def get_brief_generator(settings: Settings) -> BriefGenerator:
    name = settings.brief_llm_provider.strip().lower()
    if name in ("placeholder", "", "none"):
        return BriefPlaceholderGenerator(settings.prompts_dir)
    if name == "openai":
        return BriefOpenAIGenerator()
    if name == "gemini":
        return BriefGeminiGenerator()
    raise ValueError(f"Unknown BRIEF_LLM_PROVIDER: {name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Batch Amazon EU listing images (type × count; see config.yaml).",
    )
    parser.add_argument("--product", type=str, help="Single product folder name, e.g. 商品1")
    parser.add_argument("--all", action="store_true", help="Process all product folders with 商品标题.txt")
    parser.add_argument("--from", dest="from_name", type=str, help="Range start: 商品1")
    parser.add_argument("--to", dest="to_name", type=str, help="Range end: 商品3")
    parser.add_argument("--dry-run", action="store_true", help="Print plan only; no API / no files written")
    parser.add_argument("--provider", type=str, help="Override IMAGE_PROVIDER from .env")
    parser.add_argument(
        "--counts",
        type=str,
        help='Override counts, e.g. main=2,scene=0,detail=3 (comma-separated key=int)',
    )
    parser.add_argument(
        "--config",
        type=str,
        help="Path to alternate config.yaml (image_counts + generation)",
    )
    parser.add_argument(
        "--regen",
        type=str,
        help=(
            "Regenerate ONLY the listed images, e.g. 'scene_02' or "
            "'scene_02,detail_01'. All other output files and meta entries "
            "are preserved. Indexes must already exist in current counts."
        ),
    )
    parser.add_argument(
        "--note",
        type=str,
        help=(
            "One-shot extra instruction injected into every image generated in "
            "this run as the highest-priority rule. Useful with --regen to fix "
            "a specific issue without editing any prompt files, e.g. "
            "'--note \"main background must be soft beige\"' or "
            "'--note \"product should be slightly larger\"'. "
            "Leave empty (default) to use prompts as-is."
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args(argv)

    configure_logging(verbose=args.verbose)
    logger = get_logger()

    settings = load_settings()
    if args.provider:
        settings = dataclasses.replace(settings, image_provider=args.provider.strip().lower())

    config_path = Path(args.config).resolve() if args.config else None

    if args.dry_run:
        logger.info("Dry-run mode: no API calls, no files written.")
        provider: ImageProvider = PlaceholderProvider()
        effective_provider_name = "placeholder (dry-run)"
    else:
        try:
            provider = get_provider(settings)
        except ValueError as e:
            logger.error(str(e))
            return 2
        effective_provider_name = settings.image_provider

    root = PROJECT_ROOT
    products = discover_products(root)

    if args.product:
        target = root / args.product
        if not target.is_dir() or not (target / TITLE_FILE).is_file():
            logger.error(f"Product not found or missing {TITLE_FILE}: {target}")
            return 2
        products = [target]
    elif args.all or args.from_name or args.to_name:
        products = filter_products_by_range(products, args.from_name, args.to_name)
    else:
        parser.print_help()
        logger.error("Specify --product NAME, --all, or --from/--to")
        return 2

    if not products:
        logger.error("No products to process.")
        return 1

    logger.info(f"Products to process: {[p.name for p in products]} (provider={effective_provider_name})")

    brief_generator = get_brief_generator(settings)

    try:
        regen_targets = _parse_regen_targets(args.regen)
    except ValueError as e:
        logger.error(str(e))
        return 2

    exit_code = 0
    for product_dir in products:
        try:
            resolved = load_resolved_counts(
                product_dir=product_dir,
                cli_counts=args.counts,
                config_path=config_path,
                env_output_size=settings.output_size,
            )
            process_product(
                product_dir,
                provider,
                settings,
                resolved,
                brief_generator,
                dry_run=args.dry_run,
                regen_targets=regen_targets,
                user_note=args.note,
            )
        except Exception as e:
            logger.exception(f"Failed {product_dir.name}: {e}")
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
