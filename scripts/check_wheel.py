from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path

from scripts.package_policy import external_payload_name

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
REQUIRED_SUFFIXES = (
    "ai_bench/web/static/index.html",
    "benchmark_packs/synthetic_text_intent/config.json",
    "benchmark_packs/synthetic_text_intent/pack.py",
    "benchmark_packs/speech_to_text/config.json",
    "benchmark_packs/speech_to_text/pack.py",
)
FORBIDDEN_PARTS = {
    ".env",
    ".git",
    ".venv",
    "__pycache__",
    "results",
    "runs",
}


def main() -> int:
    wheels = sorted(DIST.glob("*.whl"), key=lambda path: path.stat().st_mtime)
    if not wheels:
        print("No wheel found under dist; run python -m build first.", file=sys.stderr)
        return 1
    wheel = wheels[-1]
    with zipfile.ZipFile(wheel) as archive:
        names = tuple(archive.namelist())
    failures = [
        f"Wheel is missing {suffix}"
        for suffix in REQUIRED_SUFFIXES
        if not any(name.endswith(suffix) for name in names)
    ]
    failures.extend(
        f"Wheel contains forbidden state: {name}"
        for name in names
        if any(part.casefold() in FORBIDDEN_PARTS for part in Path(name).parts)
    )
    failures.extend(
        f"Wheel contains external data/configuration: {name}"
        for name in names
        if external_payload_name(name)
    )
    for source in DIST.glob("*.tar.gz"):
        with tarfile.open(source) as archive:
            failures.extend(
                f"Source package contains external data/configuration: {member.name}"
                for member in archive.getmembers()
                if external_payload_name(member.name)
            )
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"Validated packaged runtime contents in {wheel.name}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
