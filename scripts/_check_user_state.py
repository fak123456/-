"""Quick diag: scan candidate output roots for the 3 ASIN folders the user
has in their tasklists, and report whether each product actually got
generated (according to its ``output/meta.json``).

Run from repo root:

    python scripts/_check_user_state.py
"""
from __future__ import annotations

import json
from pathlib import Path

ASINS = ["B0GGRC537N", "B0GGRBZVH7", "B0GGQRK62J"]
CANDIDATE_ROOTS = [
    Path(r"C:\Users\封安康\Desktop\批量测试"),
    Path(r"C:\Users\封安康\Desktop\电商生图模型"),
    Path(r"C:\Users\封安康\Desktop\crawler_installer\output"),
    Path(r"C:\Users\封安康\Desktop\电商生图模型\installer"),
]


def main() -> int:
    found = []
    for root in CANDIDATE_ROOTS:
        if not root.is_dir():
            continue
        for asin in ASINS:
            d = root / asin
            if d.is_dir():
                found.append((asin, d))

    if not found:
        print("没找到任何 B0 产品目录——说明这 3 个 ZIP 还没解压过。")
        print("（用户的 xlsx 里 3 行都是「待处理」，跟磁盘状态一致。）")
        return 0

    print(f"找到 {len(found)} 个产品目录：\n")
    for asin, d in found:
        meta_p = d / "output" / "meta.json"
        refs_dir = d / "refs"
        n_refs = 0
        if refs_dir.is_dir():
            n_refs = sum(1 for c in refs_dir.iterdir() if c.is_file())

        if not meta_p.is_file():
            print(f"  {asin} -> {d}")
            print(f"      refs: {n_refs} 张  |  meta.json: 无")
            print()
            continue
        try:
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  {asin} -> {d}  (meta 读不到: {e})")
            continue
        imgs = meta.get("images", []) or []
        n_ok = sum(1 for im in imgs if im.get("status") == "ok")
        n_err = sum(1 for im in imgs if im.get("status") == "error")
        cancelled = bool(meta.get("cancelled"))
        ct = meta.get("counts_total")
        print(f"  {asin} -> {d}")
        print(
            f"      refs: {n_refs} 张  |  meta: ok={n_ok} err={n_err}"
            f" cancelled={cancelled} 总计={len(imgs)} counts_total={ct}"
        )
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
