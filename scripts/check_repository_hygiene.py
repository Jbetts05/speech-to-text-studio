from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from scripts.package_policy import external_payload_name

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PATH_PREFIXES = (
    Path("frontend/dist"),
    Path("frontend/playwright-report"),
    Path("frontend/test-results"),
    Path("infra/package"),
)
FORBIDDEN_BINARY_SUFFIXES = {
    ".avi",
    ".bmp",
    ".docx",
    ".gif",
    ".jpeg",
    ".jpg",
    ".mov",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".pptx",
    ".tiff",
    ".wav",
    ".webp",
    ".xlsx",
}
FORBIDDEN_PATH_PARTS = {
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
    "results",
    "runs",
}
TEXT_PATTERNS = {
    "domain-specific benchmark term": re.compile(
        r"\b(?:rec" + r"eipt|sro" + r"ie|co" + r"rd)\b",
        re.I,
    ),
    "Windows user path": re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+", re.I),
    "Unix user path": re.compile(r"/Us" r"ers/[^/\s]+|/ho" r"me/(?!data(?:/|\b))[^/\s]+"),
    "Azure subscription resource ID": re.compile(r"/subscriptions/[0-9a-f]{8}-[0-9a-f-]{27}", re.I),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "storage account key": re.compile(r"\bAccountKey=[A-Za-z0-9+/=]{20,}"),
}


def _tracked_files() -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [ROOT / Path(item.decode("utf-8")) for item in completed.stdout.split(b"\0") if item]


def main() -> int:
    failures: list[str] = []
    files = _tracked_files()
    for path in files:
        relative = path.relative_to(ROOT)
        normalized = Path(relative.as_posix())
        if normalized.parts and normalized.parts[0] == ".azure":
            failures.append(f"Azure environment/deployment state is tracked: {relative}")
        if any(part.casefold() in FORBIDDEN_PATH_PARTS for part in relative.parts):
            failures.append(f"Generated or local state is tracked: {relative}")
        if any(
            normalized == prefix or prefix in normalized.parents
            for prefix in FORBIDDEN_PATH_PREFIXES
        ):
            failures.append(f"Generated or local state is tracked: {relative}")
        if path.is_symlink():
            failures.append(f"Symlink requires explicit review: {relative}")
            continue
        if path.name.casefold().startswith(".env") and path.name.casefold() != ".env.example":
            failures.append(f"Environment file is tracked: {relative}")
        if path.suffix.casefold() in FORBIDDEN_BINARY_SUFFIXES:
            failures.append(f"Binary fixture/artifact is tracked: {relative}")
        if external_payload_name(relative.as_posix()):
            failures.append(f"External dataset/provider payload is tracked: {relative}")
        if not path.is_file():
            continue
        if path.stat().st_size > 2 * 1024 * 1024:
            failures.append(f"Oversized repository file requires explicit review: {relative}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            failures.append(f"Unexpected non-text file is tracked: {relative}")
            continue
        for label, pattern in TEXT_PATTERNS.items():
            if pattern.search(text):
                failures.append(f"{label} found in {relative}")
    if failures:
        print("\n".join(sorted(set(failures))), file=sys.stderr)
        return 1
    print(f"Repository hygiene passed for {len(files)} repository files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
