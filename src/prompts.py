"""Load prompt templates and inject variables (safe str.replace, not format)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.config import Settings

TYPE_FILES: dict[str, str] = {
    "main": "type_main.md",
    "scene": "type_scene.md",
    "multi": "type_multi.md",
    "size": "type_size.md",
    "detail": "type_detail.md",
    "angle": "type_angle.md",
    "material": "type_material.md",
}


@dataclass
class BuiltPrompt:
    """Composed prompt + parts for diagnostics."""

    full: str
    global_text: str
    type_text: str


def build_prompt(
    type_name: str,
    type_index: int,
    type_total: int,
    product_title: str,
    user_brief: str,
    settings: Settings,
    *,
    user_note: str | None = None,
) -> BuiltPrompt:
    """
    Combine global rules + type template.

    Placeholders: {product_title} {type_index} {type_total} {type_name} {user_brief}

    ``user_note`` (optional): a one-shot user instruction (CLI --note) injected
    at the top of the PRODUCT header as the highest-priority rule.
    """
    prompts_dir: Path = settings.prompts_dir
    global_path = prompts_dir / "global_rules.md"
    tmpl = TYPE_FILES.get(type_name)
    if not tmpl:
        raise KeyError(f"Unknown type_name: {type_name}")
    type_path = prompts_dir / tmpl

    if not global_path.is_file():
        raise FileNotFoundError(f"Missing global rules: {global_path}")
    if not type_path.is_file():
        raise FileNotFoundError(f"Missing type template: {type_path}")

    global_text = global_path.read_text(encoding="utf-8").strip()
    type_text = type_path.read_text(encoding="utf-8")
    title_clean = product_title.strip()
    brief_clean = (user_brief or "").strip()
    repl = (
        ("{product_title}", title_clean),
        ("{type_index}", str(int(type_index))),
        ("{type_total}", str(int(type_total))),
        ("{type_name}", str(type_name)),
        ("{user_brief}", brief_clean),
    )
    for ph, val in repl:
        type_text = type_text.replace(ph, val)
    type_text = type_text.strip()

    # Surface the product title, type, and brief at the very top so the model
    # sees them before the long global rules section.
    header_lines: list[str] = []

    note_clean = (user_note or "").strip()
    if note_clean:
        header_lines.extend(
            [
                "=== ONE-SHOT FIX INSTRUCTION FROM USER (HIGHEST PRIORITY) ===",
                note_clean,
                "Apply the above on top of every other rule below. If anything below conflicts with this one-shot instruction, this one-shot instruction WINS.",
                "",
            ]
        )

    header_lines.extend(
        [
            "=== PRODUCT (read this first) ===",
            f"Title: {title_clean}",
            f"Image type: {type_name} (image {int(type_index)} of {int(type_total)})",
        ]
    )
    if brief_clean:
        header_lines.append(f"Per-image brief: {brief_clean}")
    header_lines.extend(
        [
            "",
        "=== THREE ABSOLUTE RULES THAT APPLY TO EVERY OUTPUT IMAGE ===",
        "1. CANVAS: Output MUST be exactly 1600×1600 pixels, 1:1 square aspect "
        "ratio. No portrait, landscape, or non-square crop.",
        "2. IDENTITY LOCK: Preserve the exact product identity from the reference "
        "images — same color, shape, geometry, proportions, materials, surface "
        "finish, parts, number of units, printed markings. Do NOT redesign, "
        "restyle, or reinterpret the product.",
        "3. ZERO DEFECTS: The product must appear factory-new, pristine, flawless. "
        "ABSOLUTELY NO scratches, dust, smudges, fingerprints, dents, chips, "
        "cracks, frayed edges, loose threads, color bleeding, uneven paint, wear "
        "marks, burrs, fading, deformation, or any other defect of any kind. "
        "This applies to every surface, every part, every close-up. Treat the "
        "product as a brand-new sample for a glossy catalog photo.",
        ]
    )
    header = "\n".join(header_lines)

    full = f"{header}\n\n---\n\n{global_text}\n\n---\n\n{type_text}"
    return BuiltPrompt(full=full, global_text=global_text, type_text=type_text)
