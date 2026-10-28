from __future__ import annotations

import json
import os
import shutil
import subprocess
import venv
from pathlib import Path

import pydantic

from benchmark_packs.speech_to_text.decode_process import decoder_environment
from infra.scripts import stage_package
from tests.speech_support import make_audio
from tests.test_speech_upload import stage_file


def test_actual_staged_upload_without_an_editable_application_install(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    for entry in stage_package.build_manifest():
        relative = Path(str(entry["path"]))
        target = package / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(stage_package.ROOT / relative, target)
    shutil.copy2(stage_package.ROOT / "requirements.lock", package / "requirements.txt")
    assert (package / "src" / "ai_bench" / "__init__.py").is_file()
    assert not (package / "ai_bench").exists()

    environment_root = tmp_path / "dependencies-only"
    venv.EnvBuilder(with_pip=False).create(environment_root)
    executable = (
        environment_root / "Scripts" / "python.exe"
        if os.name == "nt"
        else environment_root / "bin" / "python"
    )
    environment = decoder_environment()
    environment.pop("PYTHONPATH")
    purelib = subprocess.run(
        [str(executable), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        cwd=package,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    site_packages = Path(purelib.stdout.strip())
    dependencies = Path(pydantic.__file__).resolve().parents[1]
    # A literal .pth directory entry does not execute that directory's editable hooks.
    # This shares installed requirements, without installing or exposing the application.
    (site_packages / "requirements-only.pth").write_text(str(dependencies) + "\n", encoding="utf-8")
    assert not tuple(site_packages.glob("*editable*"))
    probe = subprocess.run(
        [
            str(executable),
            "-c",
            "import importlib.util, soundfile, pydantic; "
            "assert importlib.util.find_spec('ai_bench') is None; "
            "assert importlib.util.find_spec("
            "'benchmark_packs.speech_to_text.decode_worker') is not None; "
            "print('requirements-only')",
        ],
        cwd=package,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert probe.stdout.strip() == "requirements-only"

    files = tuple(
        file
        for extension in (".flac", ".wav", ".mp3")
        for file in (
            stage_file(tmp_path, f"synthetic-{extension[1:]}{extension}", make_audio(extension)),
            stage_file(tmp_path, f"synthetic-{extension[1:]}.txt", b"Synthetic reference"),
        )
    )
    payload = [
        {
            "name": file.name,
            "path": str(file.path),
            "size_bytes": file.size_bytes,
            "sha256": file.sha256,
            "media_type": file.media_type,
        }
        for file in files
    ]
    unbootstrapped = subprocess.run(
        [
            str(executable),
            "-m",
            "benchmark_packs.speech_to_text.decode_worker",
        ],
        input=json.dumps(payload),
        cwd=package,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert unbootstrapped.returncode != 0
    assert "No module named 'ai_bench'" in unbootstrapped.stderr
    startup = """
import json
import sys
from pathlib import Path

# Match the application's --app-dir src startup, which affects only this process.
sys.path.insert(0, str(Path("src").resolve()))
from ai_bench.dataset_imports import DatasetImportFile
from benchmark_packs.speech_to_text.upload import SpeechDatasetImporter

entries = json.load(sys.stdin)
files = tuple(DatasetImportFile(**{**entry, "path": Path(entry["path"])}) for entry in entries)
result = SpeechDatasetImporter().import_files(files)
print(json.dumps({
    "types": sorted(sample.input.media_type for sample in result.samples),
    "objects": sorted(result.files),
}))
"""
    completed = subprocess.run(
        [str(executable), "-c", startup],
        input=json.dumps(payload),
        cwd=package,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=150,
    )
    result = json.loads(completed.stdout)
    assert result["types"] == ["audio/flac", "audio/mpeg", "audio/wav"]
    assert result["objects"] == sorted({file.sha256 for file in files})
