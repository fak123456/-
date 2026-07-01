"""One-shot helper: regenerate the four ``installer/启动_实例N.bat`` files
with a UTF-8 BOM so cmd.exe parses the embedded Chinese exe name
``启动.exe`` correctly on every Windows locale.

Each .bat:

* sets ``AMAZON_IMG_GUI_INSTANCE`` (drives the per-instance config dir +
  default tasklist filename suffix);
* sets ``GRADIO_SERVER_PORT`` so the four GUIs don't collide on 7860;
* ``start ""``-launches ``启动.exe`` so the cmd window closes immediately.

Run from repo root after rebuilding ``installer/启动.exe``::

    python scripts/_make_instance_bats.py
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "installer"

TEMPLATE = (
    "@echo off\r\n"
    "chcp 65001 >nul\r\n"
    'cd /d "%~dp0"\r\n'
    'set "AMAZON_IMG_GUI_INSTANCE={n}"\r\n'
    'set "GRADIO_SERVER_PORT={port}"\r\n'
    'start "" "启动.exe"\r\n'
)


def main() -> int:
    INSTALLER.mkdir(parents=True, exist_ok=True)
    for n in range(1, 5):
        body = TEMPLATE.format(n=n, port=7860 + n)
        path = INSTALLER / f"启动_实例{n}.bat"
        path.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
        raw = path.read_bytes()
        print(f"  {path.name}  ({len(raw)} bytes, BOM={raw[:3] == b'\xef\xbb\xbf'})")
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
