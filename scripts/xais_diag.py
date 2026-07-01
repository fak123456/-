"""Diagnostic script: minimal calls to Xais to isolate failures.

Usage:
    python scripts/xais_diag.py

Loads .env automatically. Runs:
  1) GET /v1/models  (verify key + reachability)
  2) POST /v1/chat/completions txt2img (no reference image)
  3) POST /v1/chat/completions img2img with 1 reference (use 商品1 first ref)
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

API_KEY = os.getenv("IMAGE_API_KEY", "").strip()
API_BASE = os.getenv("XAIS_API_BASE", "https://sg2.dchai.cn").rstrip("/")
MODEL = os.getenv("XAIS_MODEL_ID", "Nano_Banana_Pro_2K_0").strip()

if not API_KEY:
    print("FATAL: IMAGE_API_KEY empty in .env"); sys.exit(2)

H = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}


def section(title: str) -> None:
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)


def short(s: object, n: int = 400) -> str:
    t = str(s)
    return t if len(t) <= n else t[:n] + "...(truncated)"


# 1. GET /v1/models
section(f"1) GET {API_BASE}/v1/models  (verify auth)")
try:
    r = requests.get(f"{API_BASE}/v1/models", headers=H, timeout=30)
    print(f"HTTP {r.status_code}")
    try:
        data = r.json()
        if isinstance(data, dict) and "data" in data:
            ids = [m.get("id") for m in data["data"]][:20]
            print("models[:20] =", ids)
        else:
            print(short(data))
    except ValueError:
        print(short(r.text))
except Exception as e:
    print(f"EXCEPTION: {e!r}")

# 2. txt2img only
section(f"2) POST chat/completions  txt2img  model={MODEL}")
payload2 = {
    "model": MODEL,
    "messages": [{"role": "user", "content": "A simple red apple on white background, 长宽比:1:1"}],
}
try:
    r = requests.post(f"{API_BASE}/v1/chat/completions", headers=H, json=payload2, timeout=180)
    print(f"HTTP {r.status_code}")
    try:
        data = r.json()
        if r.status_code < 400:
            content = data["choices"][0]["message"]["content"]
            print("content =", short(content, 600))
        else:
            print("body =", short(data))
    except ValueError:
        print(short(r.text))
except Exception as e:
    print(f"EXCEPTION: {e!r}")

# 3. img2img with first ref of 商品1
section(f"3) POST chat/completions  img2img  model={MODEL}  (1 ref from 商品1)")
refs_dir = PROJECT_ROOT / "商品1" / "refs"
ref_imgs = sorted([p for p in refs_dir.glob("*") if p.suffix.lower() in {".jpg", ".jpeg", ".png"}])
if not ref_imgs:
    print("FATAL: no reference image found under 商品1/refs/")
    sys.exit(3)
ref_path = ref_imgs[0]
blob = ref_path.read_bytes()
print(f"ref = {ref_path.name}  size={len(blob)/1024:.1f} KiB")
mime = "image/png" if blob.startswith(b"\x89PNG") else "image/jpeg"
b64 = base64.b64encode(blob).decode("ascii")
print(f"base64 length = {len(b64)/1024:.1f} KiB")

payload3 = {
    "model": MODEL,
    "messages": [{
        "role": "user",
        "content": [
            {"type": "text", "text": "Make a clean white-background product hero shot, 长宽比:1:1"},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ],
    }],
}
try:
    r = requests.post(f"{API_BASE}/v1/chat/completions", headers=H, json=payload3, timeout=300)
    print(f"HTTP {r.status_code}")
    try:
        data = r.json()
        if r.status_code < 400:
            content = data["choices"][0]["message"]["content"]
            print("content =", short(content, 600))
        else:
            print("body =", short(data))
    except ValueError:
        print(short(r.text))
except Exception as e:
    print(f"EXCEPTION: {e!r}")

print("\n" + "=" * 60)
print("DONE. Send the full output above to assistant for analysis.")
