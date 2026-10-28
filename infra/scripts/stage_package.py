from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from scripts.package_policy import external_payload_name

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "infra" / "package"
ALLOWED_FILES = (
    "pyproject.toml",
    "requirements.lock",
    "infra/__init__.py",
    "infra/scripts/__init__.py",
    "infra/scripts/runtime_probe.py",
    "infra/scripts/release_runtime.py",
)
ALLOWED_DIRECTORIES = ("src", "benchmark_packs")
FORBIDDEN_PARTS = {
    ".azure",
    ".env",
    ".git",
    ".venv",
    "node_modules",
    "results",
    "runs",
    "uploads",
    "archives",
    "migration-bundles",
    "publication",
}
IGNORED_GENERATED_PARTS = {
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
}
MAX_FILE_BYTES = 20 * 1024 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_files() -> list[Path]:
    files: list[Path] = []
    for relative in ALLOWED_FILES:
        path = ROOT / relative
        if not path.is_file():
            raise ValueError(f"Required deployment file is missing: {relative}")
        files.append(path)
    for relative in ALLOWED_DIRECTORIES:
        directory = ROOT / relative
        if not directory.is_dir():
            raise ValueError(f"Required deployment directory is missing: {relative}")
        files.extend(
            path
            for path in directory.rglob("*")
            if path.is_file()
            and not any(
                part.casefold() in IGNORED_GENERATED_PARTS for part in path.relative_to(ROOT).parts
            )
        )
    return sorted(files)


def _validate(path: Path) -> None:
    relative = path.relative_to(ROOT)
    if path.is_symlink() or any(
        parent.is_symlink() for parent in path.parents if parent != ROOT.parent
    ):
        raise ValueError(f"Deployment package cannot contain a symlink: {relative}")
    if path.name.casefold().startswith(".env"):
        raise ValueError(f"Environment file cannot enter the package: {relative}")
    if external_payload_name(relative.as_posix()):
        raise ValueError(f"External dataset/provider payload cannot enter the package: {relative}")
    if any(part.casefold() in FORBIDDEN_PARTS for part in relative.parts):
        raise ValueError(f"Forbidden deployment path: {relative}")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"Deployment file exceeds {MAX_FILE_BYTES} bytes: {relative}")


def rebuild_frontend() -> None:
    static = ROOT / "src" / "ai_bench" / "web" / "static"
    before = {
        path.relative_to(static).as_posix(): _sha256(path)
        for path in static.rglob("*")
        if path.is_file()
    }
    npm = shutil.which("npm")
    if npm is None:
        raise ValueError("npm is required to rebuild the reviewed frontend before packaging.")
    command = [npm, "run", "build"]
    if Path(npm).suffix.casefold() in {".cmd", ".bat", ".ps1"}:
        node = shutil.which("node")
        cli = Path(npm).parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
        if node is None or not cli.is_file():
            raise ValueError("Cannot resolve the npm JavaScript CLI without a shell launcher.")
        command = [node, str(cli), "run", "build"]
    import os

    environment = dict(os.environ)
    environment["PATH"] = (
        str(Path(sys.executable).parent) + os.pathsep + environment.get("PATH", "")
    )
    subprocess.run(command, cwd=ROOT / "frontend", env=environment, check=True)
    after = {
        path.relative_to(static).as_posix(): _sha256(path)
        for path in static.rglob("*")
        if path.is_file()
    }
    if before != after:
        raise ValueError(
            "Frontend rebuild changed generated assets; review and commit "
            "the normal build output before packaging."
        )


def build_manifest() -> list[dict[str, object]]:
    manifest: list[dict[str, object]] = []
    for path in _source_files():
        _validate(path)
        relative = path.relative_to(ROOT)
        manifest.append(
            {
                "path": relative.as_posix(),
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    return manifest


def stage(manifest: list[dict[str, object]]) -> None:
    expected = (ROOT.resolve() / "infra" / "package").absolute()
    if PACKAGE.is_symlink() or PACKAGE.absolute() != expected:
        raise ValueError("Refusing to replace an unexpected package path.")
    if PACKAGE.exists():
        shutil.rmtree(PACKAGE)
    PACKAGE.mkdir(parents=True)

    for entry in manifest:
        relative = Path(str(entry["path"]))
        destination = PACKAGE / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)

    shutil.copy2(ROOT / "requirements.lock", PACKAGE / "requirements.txt")
    (PACKAGE / "package-manifest.json").write_text(
        json.dumps({"files": manifest}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Build an allowlisted App Service package.")
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args()

    rebuild_frontend()
    manifest = build_manifest()
    static_index = ROOT / "src" / "ai_bench" / "web" / "static" / "index.html"
    if not static_index.is_file():
        raise ValueError(
            "Packaged frontend assets are missing. Run the frontend build before staging."
        )
    if arguments.dry_run:
        print(f"Deployment package dry run passed for {len(manifest)} files.")
        return 0
    stage(manifest)
    print(f"Staged {len(manifest)} allowlisted files in {PACKAGE}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
