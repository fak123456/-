"""
Build a Windows one-file exe with PyInstaller.

Prefer a **clean venv** with only `requirements.txt` installed so PyInstaller does not
see optional heavy packages (torch, jupyter, …) from a global Anaconda install.

Usage (from repo root):
    python -m venv .venv
    .venv\\Scripts\\activate
    pip install -r requirements.txt
    python build_exe.py

Output: dist/AmazonImgGUI.exe
Ship with: installer/README_GUI.md, installer/使用说明.html, installer/README.txt
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SPEC = ROOT / "AmazonImgGUI.spec"


def _stop_running_gui_exe() -> None:
    """Avoid PermissionError when PyInstaller overwrites dist\\AmazonImgGUI.exe."""
    if sys.platform != "win32":
        return
    subprocess.run(
        ["taskkill", "/F", "/IM", "AmazonImgGUI.exe"],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["taskkill", "/F", "/IM", "启动.exe"],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1.5)


def main() -> int:
    if not SPEC.is_file():
        print(f"Missing spec file: {SPEC}")
        return 1
    prompts = ROOT / "prompts"
    cfg = ROOT / "config.yaml"
    if not prompts.is_dir():
        print(f"Missing prompts dir: {prompts}")
        return 1
    if not cfg.is_file():
        print(f"Missing config.yaml: {cfg}")
        return 1

    _stop_running_gui_exe()

    dist = ROOT / "dist"
    build = ROOT / "build"
    for d in (dist, build):
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        str(SPEC),
    ]
    print("Running:", " ".join(cmd))
    env = os.environ.copy()
    env.setdefault("GRADIO_ANALYTICS_ENABLED", "0")
    subprocess.check_call(cmd, cwd=str(ROOT), env=env)
    exe = dist / "AmazonImgGUI.exe"
    if not exe.is_file():
        print("Build finished but exe not found under dist/")
        return 1
    mb = exe.stat().st_size / (1024 * 1024)
    print(f"Built: {exe} ({mb:.1f} MB)")
    installer_exe = ROOT / "installer" / "启动.exe"
    installer_exe.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(exe, installer_exe)
    print(f"Copied to: {installer_exe}")
    for junk in (ROOT / "dist_test_err.log", ROOT / "dist_test_out.log"):
        if junk.is_file():
            junk.unlink(missing_ok=True)
    if mb > 150:
        print("Warning: exe size exceeds 150 MB target; use a clean venv with only requirements.txt.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
