"""Detect and persist the per-product source image used for size diagrams."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageFilter, ImageStat

from src.image_io import collect_reference_paths, is_image_path

SIZE_REF_FILENAME = "size_ref.json"

_NAME_KEYWORDS = {
    "size": 45,
    "dimension": 45,
    "dimensions": 45,
    "measurement": 40,
    "measure": 30,
    "cm": 25,
    "inch": 25,
    "inches": 25,
    "mm": 20,
    "尺寸": 50,
    "尺码": 35,
    "大小": 25,
    "规格": 25,
    "长": 15,
    "宽": 15,
    "高": 15,
}


@dataclass(frozen=True)
class SizeRefCandidate:
    path: Path
    score: float
    reason: str


def size_ref_record_path(product_dir: Path) -> Path:
    return product_dir / "refs" / SIZE_REF_FILENAME


def _path_token_score(path: Path) -> tuple[float, list[str]]:
    name = path.name.casefold()
    score = 0.0
    reasons: list[str] = []
    for kw, pts in _NAME_KEYWORDS.items():
        if kw.casefold() in name:
            score += pts
            reasons.append(f"name:{kw}")
    return score, reasons


def _image_shape_score(path: Path) -> tuple[float, list[str]]:
    reasons: list[str] = []
    score = 0.0
    try:
        with Image.open(path) as im:
            im = im.convert("RGB")
            thumb = im.copy()
            thumb.thumbnail((360, 360))
            gray = thumb.convert("L")
            stat = ImageStat.Stat(gray)
            mean = float(stat.mean[0])
            if mean > 205:
                score += 12
                reasons.append("bright-bg")
            edges = gray.filter(ImageFilter.FIND_EDGES)
            edge_stat = ImageStat.Stat(edges)
            edge_mean = float(edge_stat.mean[0])
            if edge_mean > 9:
                score += min(22, edge_mean)
                reasons.append("line-detail")
            px = gray.histogram()
            total = max(1, thumb.size[0] * thumb.size[1])
            dark_ratio = sum(px[:80]) / total
            if 0.015 <= dark_ratio <= 0.22:
                score += 12
                reasons.append("dark-labels")
            w, h = thumb.size
            if min(w, h) > 0 and max(w, h) / min(w, h) < 1.6:
                score += 4
                reasons.append("balanced-frame")
    except Exception:
        return 0.0, ["unreadable"]
    return score, reasons


def score_size_ref(path: Path) -> SizeRefCandidate:
    score, reasons = _path_token_score(path)
    img_score, img_reasons = _image_shape_score(path)
    score += img_score
    reasons.extend(img_reasons)
    if not reasons:
        reasons.append("fallback")
    return SizeRefCandidate(path=path, score=round(score, 2), reason=", ".join(reasons))


def list_size_ref_candidates(product_dir: Path) -> list[SizeRefCandidate]:
    paths = [p for p in collect_reference_paths(product_dir) if p.is_file() and is_image_path(p)]
    candidates = [score_size_ref(p) for p in paths]
    return sorted(candidates, key=lambda c: (-c.score, c.path.name.casefold()))


def _resolve_saved_path(product_dir: Path, saved: str) -> Path | None:
    raw = (saved or "").strip()
    if not raw:
        return None
    p = Path(raw)
    candidates: list[Path] = []
    if p.is_absolute():
        candidates.append(p)
    candidates.extend(
        [
            product_dir / raw,
            product_dir / "refs" / raw,
            product_dir / p.name,
            product_dir / "refs" / p.name,
        ]
    )
    for c in candidates:
        try:
            rp = c.expanduser().resolve()
        except OSError:
            continue
        if rp.is_file() and is_image_path(rp):
            return rp
    return None


def load_selected_size_ref(product_dir: Path) -> dict[str, Any] | None:
    path = size_ref_record_path(product_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def save_selected_size_ref(
    product_dir: Path,
    image_path: Path,
    *,
    source: str,
    score: float = 0.0,
    reason: str = "",
) -> dict[str, Any]:
    refs_dir = product_dir / "refs"
    refs_dir.mkdir(parents=True, exist_ok=True)
    try:
        rel = str(image_path.resolve().relative_to(product_dir.resolve()))
    except ValueError:
        rel = image_path.name
    data: dict[str, Any] = {
        "selected": rel.replace("\\", "/"),
        "filename": image_path.name,
        "source": source,
        "score": score,
        "reason": reason,
        "updated_at": int(time.time()),
    }
    size_ref_record_path(product_dir).write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return data


def get_selected_size_ref_path(product_dir: Path) -> Path | None:
    data = load_selected_size_ref(product_dir)
    if not data:
        return None
    selected = str(data.get("selected") or data.get("filename") or "")
    return _resolve_saved_path(product_dir, selected)


def ensure_auto_size_ref(product_dir: Path) -> SizeRefCandidate | None:
    saved = get_selected_size_ref_path(product_dir)
    if saved is not None:
        data = load_selected_size_ref(product_dir) or {}
        return SizeRefCandidate(
            path=saved,
            score=float(data.get("score") or 0),
            reason=str(data.get("reason") or data.get("source") or "saved"),
        )
    candidates = list_size_ref_candidates(product_dir)
    if not candidates:
        return None
    best = candidates[0]
    save_selected_size_ref(
        product_dir,
        best.path,
        source="auto",
        score=best.score,
        reason=best.reason,
    )
    return best
