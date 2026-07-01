"""Quick connectivity / auth probe for Google Gemini image API.

Usage (PowerShell):
    # 如有需要先设代理（端口换成你代理软件实际的端口，clash 一般是 7890，v2rayN 是 10809）：
    $env:HTTPS_PROXY = "http://127.0.0.1:7890"
    $env:HTTP_PROXY  = "http://127.0.0.1:7890"

    .\.venv-build\Scripts\python.exe probe_gemini.py <YOUR_AIza_KEY>

Exit codes:
    0 = 完全打通：能调到 Google，能拿到一张图
    2 = 能登录但模型/账号问题
    3 = 网络不通（很可能没走代理）
    4 = Key 无效
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) < 2 or not argv[1].strip():
        print("用法: python probe_gemini.py <AIza...>")
        return 1
    api_key = argv[1].strip()

    print(f"[1/4] 当前代理环境变量:")
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        v = os.environ.get(k)
        print(f"      {k} = {v!r}")

    print("[2/4] 导入 google.genai ...")
    try:
        from google import genai
        from google.genai import types
    except Exception as e:
        print(f"  ✗ 导入失败: {type(e).__name__}: {e}")
        return 4

    print("[3/4] 构造 Client 并发起一次文生图请求 (gemini-2.5-flash-image)...")
    client = genai.Client(api_key=api_key)
    t0 = time.monotonic()
    try:
        resp = client.models.generate_content(
            model="gemini-2.5-flash-image",
            contents=["A tiny test thumbnail: a single red apple on a white background"],
            config=types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"]),
        )
    except Exception as e:
        msg = str(e)
        elapsed = time.monotonic() - t0
        print(f"  ✗ 请求失败 ({elapsed:.1f}s): {type(e).__name__}: {msg[:400]}")
        low = msg.lower()
        if "unauthor" in low or "permission" in low or "api key" in low or "invalid" in low:
            print("\n  → 看起来是 Key/权限问题:")
            print("    - 去 https://aistudio.google.com/apikey 确认 Key 启用了 Gemini API")
            print("    - 确认你的 Google 账号所在地区支持 Gemini API（大陆地区一般不行，需要其他地区账号）")
            return 4
        if "connect" in low or "ssl" in low or "timeout" in low or "name or service" in low or "refus" in low:
            print("\n  → 看起来是网络问题：")
            print("    - 确认浏览器能打开 https://aistudio.google.com")
            print("    - 在当前 PowerShell 设环境变量 $env:HTTPS_PROXY=代理地址，再重跑此脚本")
            return 3
        return 2

    elapsed = time.monotonic() - t0
    print(f"  ✓ HTTP 请求成功 ({elapsed:.1f}s)")

    print("[4/4] 解析响应是否含图片字节 ...")
    img_bytes: bytes | None = None
    for cand in (resp.candidates or []):
        for part in ((cand.content.parts if cand.content else None) or []):
            inline = getattr(part, "inline_data", None)
            if inline and getattr(inline, "data", None):
                img_bytes = bytes(inline.data)
                break
        if img_bytes:
            break
    if not img_bytes:
        print("  ✗ 响应里没有图片字节 (可能账号无图像权限或模型 ID 错)")
        print(f"    响应粗略: {str(resp)[:500]}")
        return 2

    out = Path("probe_gemini_output.png")
    out.write_bytes(img_bytes)
    print(f"  ✓ 已写出 {out.resolve()} ({len(img_bytes)} 字节)")
    print("\n=== 完全打通 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
