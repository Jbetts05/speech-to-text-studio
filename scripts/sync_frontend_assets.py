from __future__ import annotations

import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "frontend" / "dist"
TARGET = ROOT / "src" / "ai_bench" / "web" / "static"
EXPECTED_TARGET = ROOT / "src" / "ai_bench" / "web" / "static"


def main() -> int:
    if SOURCE.is_symlink():
        raise ValueError("refusing to copy frontend assets through a symlink")
    if not (SOURCE / "index.html").is_file():
        raise ValueError("frontend/dist is missing; run the Vite build first")
    source_symlinks = sorted(path for path in SOURCE.rglob("*") if path.is_symlink())
    if source_symlinks:
        raise ValueError(
            "frontend output contains symlinks: "
            + ", ".join(str(path.relative_to(SOURCE)) for path in source_symlinks)
        )
    source_maps = sorted(SOURCE.rglob("*.map"))
    if source_maps:
        raise ValueError(
            "production source maps are not allowed: "
            + ", ".join(str(path.relative_to(SOURCE)) for path in source_maps)
        )
    if TARGET.is_symlink() or EXPECTED_TARGET.resolve() != TARGET.resolve():
        raise ValueError("refusing to replace an unexpected static asset directory")
    if TARGET.exists():
        shutil.rmtree(TARGET)
    shutil.copytree(SOURCE, TARGET)
    for path in TARGET.rglob("*"):
        if path.is_file() and path.suffix.casefold() in {".css", ".html", ".js"}:
            content = path.read_bytes()
            normalized = re.sub(rb"\r+\n", b"\n", content).replace(b"\r", b"\n")
            if normalized != content:
                path.write_bytes(normalized)
    print(f"Copied generated frontend assets to {TARGET.relative_to(ROOT)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
