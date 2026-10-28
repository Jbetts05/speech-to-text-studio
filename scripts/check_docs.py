from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*]\(([^)]+)\)")


def _markdown_files() -> list[Path]:
    ignored_parts = {
        ".ai-bench",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "dist",
        "node_modules",
        "playwright-report",
        "test-results",
    }
    return sorted(
        path
        for path in ROOT.rglob("*.md")
        if not any(part in ignored_parts for part in path.relative_to(ROOT).parts)
        and Path("infra/package") not in path.relative_to(ROOT).parents
    )


def _target(source: Path, raw_target: str) -> Path | None:
    target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or target.startswith("#"):
        return None
    path_text = unquote(parsed.path)
    if not path_text:
        return None
    candidate = (source.parent / path_text).resolve()
    try:
        candidate.relative_to(ROOT)
    except ValueError as error:
        raise ValueError(
            f"{source.relative_to(ROOT)} links outside the repository: {target}"
        ) from error
    return candidate


def main() -> int:
    failures: list[str] = []
    files = _markdown_files()
    for source in files:
        text = source.read_text(encoding="utf-8")
        for match in MARKDOWN_LINK.finditer(text):
            raw_target = match.group(1)
            try:
                target = _target(source, raw_target)
            except ValueError as exc:
                failures.append(str(exc))
                continue
            if target is not None and not target.exists():
                failures.append(
                    f"{source.relative_to(ROOT)} has a missing link target: {raw_target}"
                )
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"Validated internal links in {len(files)} Markdown files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
