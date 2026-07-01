"""Placeholder provider: gray canvas (no external API)."""

from __future__ import annotations

import io
import textwrap

from PIL import Image, ImageDraw, ImageFont

from src.providers.base import ImageProvider


class PlaceholderProvider(ImageProvider):
    """Draw a gray PNG; native -> 1024x1024 for pipeline smoke tests."""

    def generate(
        self,
        prompt: str,
        reference_images: list[bytes],
        size: str = "native",
        seed: int | None = None,
        *,
        api_image_size: str | None = None,
    ) -> bytes:
        _ = seed, api_image_size
        width, height = _parse_size(size)
        img = Image.new("RGB", (width, height), color=(220, 220, 220))
        draw = ImageDraw.Draw(img)

        try:
            font = ImageFont.truetype("arial.ttf", 28)
            font_small = ImageFont.truetype("arial.ttf", 20)
        except OSError:
            font = ImageFont.load_default()
            font_small = font

        title = "PLACEHOLDER (no API)"
        draw.text((40, 40), title, fill=(40, 40, 40), font=font)

        refs_line = f"reference_images: {len(reference_images)} byte blob(s)"
        draw.text((40, 90), refs_line, fill=(60, 60, 60), font=font_small)

        excerpt = textwrap.fill(prompt[:800], width=70)
        draw.text((40, 130), excerpt, fill=(30, 30, 30), font=font_small)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()


def _parse_size(size: str) -> tuple[int, int]:
    s = size.lower().replace(" ", "")
    if s == "native":
        return 1024, 1024
    if "x" in s:
        w, h = s.split("x", 1)
        return int(w), int(h)
    return 1024, 1024


def _load_cjk_font(target_size: int) -> ImageFont.ImageFont:
    """Try Windows CJK fonts first so 中文 doesn't render as tofu boxes."""
    for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "simsun.ttc", "arial.ttf"):
        try:
            return ImageFont.truetype(name, target_size)
        except OSError:
            continue
    return ImageFont.load_default()


def make_failure_placeholder_png(
    *,
    type_name: str,
    idx: int,
    max_attempts: int,
    error_message: str,
    prompt_excerpt: str,
    size: str,
) -> bytes:
    """Render a clearly-marked red 'GENERATION FAILED' card as PNG bytes.

    Saved at the slot's normal output path so the GUI slot picker shows it,
    and the existing '重做这张' button (which deletes the file before regen)
    will replace it on retry.
    """
    width, height = _parse_size(size)
    bg = (255, 230, 230)
    border_color = (200, 40, 40)
    ink_dark = (120, 20, 20)
    ink_mid = (60, 60, 60)

    img = Image.new("RGB", (width, height), color=bg)
    draw = ImageDraw.Draw(img)

    border = max(8, int(min(width, height) * 0.015))
    draw.rectangle(
        [(0, 0), (width - 1, height - 1)],
        outline=border_color,
        width=border,
    )

    base = max(18, int(min(width, height) * 0.022))
    font_title = _load_cjk_font(int(base * 2.0))
    font_h2 = _load_cjk_font(int(base * 1.3))
    font_body = _load_cjk_font(base)
    font_small = _load_cjk_font(max(14, int(base * 0.85)))

    margin_x = border + max(20, int(width * 0.04))
    y = border + max(20, int(height * 0.04))

    title = "生成失败 / GENERATION FAILED"
    draw.text((margin_x, y), title, fill=border_color, font=font_title)
    y += int(base * 2.4)

    slot_label = f"{type_name}_{idx:02d}"
    draw.text((margin_x, y), slot_label, fill=ink_dark, font=font_h2)
    y += int(base * 1.7)

    hint = f"重试 {max_attempts} 次未成功，请点「重做这张」重新生成。"
    draw.text((margin_x, y), hint, fill=ink_dark, font=font_body)
    y += int(base * 1.6)

    body_wrap = max(20, int((width - margin_x * 2) / max(1, int(base * 0.55))))

    draw.text((margin_x, y), "错误信息 / Error:", fill=ink_mid, font=font_small)
    y += int(base * 1.1)
    err_text = textwrap.fill((error_message or "")[:300], width=body_wrap)
    draw.multiline_text((margin_x, y), err_text, fill=ink_mid, font=font_small, spacing=4)
    y += int(base * 1.1) * (err_text.count("\n") + 1) + int(base * 0.8)

    if prompt_excerpt:
        draw.text((margin_x, y), "Prompt 摘要:", fill=ink_mid, font=font_small)
        y += int(base * 1.1)
        prompt_text = textwrap.fill(prompt_excerpt[:200], width=body_wrap)
        draw.multiline_text(
            (margin_x, y), prompt_text, fill=ink_mid, font=font_small, spacing=4
        )

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
