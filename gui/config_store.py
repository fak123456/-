"""Persist GUI settings (API key, model, etc.) under ~/.amazon_img_gui/.

Multi-instance: when ``AMAZON_IMG_GUI_INSTANCE`` is set in the environment,
each value gets its own isolated config dir (``~/.amazon_img_gui_<value>``)
so several instances on the same Windows account can have independent
settings, history, project root, and output destination without
overwriting each other.
"""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import Any


def _instance_suffix() -> str:
    raw = os.environ.get("AMAZON_IMG_GUI_INSTANCE", "").strip()
    if not raw:
        return ""
    # Whitelist a-z 0-9 _ - only, so a malformed value cannot escape the
    # home directory or create odd folder names.
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", raw)
    return f"_{cleaned}" if cleaned else ""


CONFIG_DIR = Path.home() / f".amazon_img_gui{_instance_suffix()}"
CONFIG_FILE = CONFIG_DIR / "config.json"


def _obfuscate(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def _deobfuscate(s: str) -> str:
    try:
        return base64.b64decode(s.encode("ascii")).decode("utf-8")
    except Exception:
        return ""


def load_gui_config() -> dict[str, Any]:
    if not CONFIG_FILE.is_file():
        return {}
    try:
        raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    key = raw.get("image_api_key_obf")
    if isinstance(key, str) and key:
        raw["image_api_key"] = _deobfuscate(key)
    return raw


def save_gui_config(data: dict[str, Any]) -> None:
    prev = load_gui_config()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    out = dict(data)
    api = str(out.get("image_api_key", "") or "").strip()
    if not api:
        api = str(prev.get("image_api_key", "") or "").strip()
    if api:
        out["image_api_key_obf"] = _obfuscate(api)
    out.pop("image_api_key", None)
    CONFIG_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


def merge_api_key_into_config(plain_key: str) -> None:
    cfg = load_gui_config()
    cfg["image_api_key"] = plain_key.strip()
    save_gui_config(cfg)


def save_paths_into_config(project_root: str | None, output_dest: str | None) -> None:
    """Persist the two directory text boxes ("项目根目录" / "输出商品保存位置").

    Stores raw text exactly as the user typed it (or returned from the
    folder picker). Either argument can be ``None`` to leave the existing
    value untouched; pass empty string to explicitly clear.
    """
    cfg = load_gui_config()
    if project_root is not None:
        cfg["project_root"] = str(project_root).strip()
    if output_dest is not None:
        cfg["output_dest"] = str(output_dest).strip()
    save_gui_config(cfg)


# PyInstaller --onefile extracts the bundle into a fresh ``<tempdir>/_MEI<digits>``
# directory on every launch and *deletes it again on exit*. An older bug
# defaulted the GUI's project_root text box to that path, and any user who
# tabbed out of the field persisted the doomed string into config.json — so
# the next launch happily loaded a path that points at a folder PyInstaller
# was about to wipe. We treat any saved value living inside a ``_MEI*`` part
# as poisoned, drop it on read, AND scrub it from disk so the user doesn't
# keep tripping over it.
_PYI_TMP_PART_RE = re.compile(r"^_MEI[\w-]*$", re.IGNORECASE)


def _looks_like_pyinstaller_tempdir(raw: str) -> bool:
    s = (raw or "").strip()
    if not s:
        return False
    try:
        parts = Path(s).parts
    except Exception:
        return False
    return any(_PYI_TMP_PART_RE.match(part) for part in parts)


def load_saved_paths() -> tuple[str, str]:
    """Return ``(project_root, output_dest)`` previously saved by the GUI.

    Missing keys collapse to ``""`` so callers can ``or`` in their own
    default. Poisoned values pointing inside a PyInstaller ``_MEI*`` temp
    directory are silently dropped (and removed from the on-disk config) to
    keep the historical onefile-cleanup data-loss bug from re-triggering.
    """
    cfg = load_gui_config()
    pr = str(cfg.get("project_root", "") or "").strip()
    od = str(cfg.get("output_dest", "") or "").strip()
    dirty = False
    if _looks_like_pyinstaller_tempdir(pr):
        pr = ""
        cfg["project_root"] = ""
        dirty = True
    if _looks_like_pyinstaller_tempdir(od):
        od = ""
        cfg["output_dest"] = ""
        dirty = True
    if dirty:
        try:
            save_gui_config(cfg)
        except OSError:
            pass
    return pr, od
