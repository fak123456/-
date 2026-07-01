"""Resolve project / bundle paths for dev vs PyInstaller frozen exe."""

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def app_workdir() -> Path:
    """Writable directory: folder containing the exe when frozen, else repo root."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    from src.config import PROJECT_ROOT

    return PROJECT_ROOT


def bundle_root() -> Path:
    """Read-only bundled assets (MEIPASS when frozen)."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", app_workdir()))
    from src.config import PROJECT_ROOT

    return PROJECT_ROOT


def resolved_config_yaml() -> Path:
    """Prefer user-editable config next to exe, then bundled default."""
    wd = app_workdir() / "config.yaml"
    if wd.is_file():
        return wd
    br = bundle_root() / "config.yaml"
    if br.is_file():
        return br
    from src.config import PROJECT_ROOT

    return PROJECT_ROOT / "config.yaml"


def ensure_user_prompts() -> Path:
    """
    When frozen, copy bundled prompts/ into app_workdir()/prompts once
    so edits are writable and persist beside the exe.
    """
    user = app_workdir() / "prompts"
    if user.is_dir() and any(user.iterdir()):
        return user
    src = bundle_root() / "prompts"
    if src.is_dir():
        import shutil

        shutil.copytree(src, user, dirs_exist_ok=True)
    return user


def resolved_prompts_dir() -> Path:
    if is_frozen():
        return ensure_user_prompts()
    from src.config import PROJECT_ROOT

    return PROJECT_ROOT / "prompts"
