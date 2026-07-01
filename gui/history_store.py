"""Append-only run history for the GUI (merged with per-product meta.json in UI)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gui.config_store import CONFIG_DIR

HISTORY_FILE = CONFIG_DIR / "history.json"


def _load_list() -> list[dict[str, Any]]:
    if not HISTORY_FILE.is_file():
        return []
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def append_run(
    *,
    product: str,
    product_path: str,
    status: str,
    counts_total: int,
    provider: str,
    error: str | None = None,
) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    items = _load_list()
    items.append(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "product": product,
            "product_path": product_path,
            "status": status,
            "counts_total": counts_total,
            "image_provider": provider,
            "error": error,
        }
    )
    items = items[-500:]
    HISTORY_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def list_runs(limit: int = 100) -> list[dict[str, Any]]:
    return _load_list()[-limit:]
