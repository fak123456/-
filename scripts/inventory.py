"""Quick inventory script: list all product folders and their readiness."""

from __future__ import annotations

from pathlib import Path

from src.image_io import collect_reference_paths


def main() -> None:
    root = Path(".")
    products = sorted(
        [p for p in root.iterdir() if p.is_dir() and p.name.startswith("商品")],
        key=lambda p: int(p.name[2:]) if p.name[2:].isdigit() else 9999,
    )
    print(f"Found {len(products)} product folders:\n")
    print(f"{'name':<10} {'title':<8} {'refs':<6} {'output_png'}")
    print("-" * 50)
    ready = 0
    for p in products:
        title_ok = "OK" if (p / "商品标题.txt").is_file() else "MISSING"
        refs = len(collect_reference_paths(p)) if p.is_dir() else 0
        out_dir = p / "output"
        n_out = len(list(out_dir.glob("*.png"))) if out_dir.is_dir() else 0
        print(f"{p.name:<10} {title_ok:<8} {refs:<6} {n_out}")
        if title_ok == "OK" and refs > 0:
            ready += 1
    print()
    print(f"{ready}/{len(products)} ready to process.")


if __name__ == "__main__":
    main()
