"""Bounded decoder process isolation, not a security sandbox."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from ai_bench.contracts import SampleSnapshot
from ai_bench.dataset_imports import DatasetImportFile, DatasetImportResult
from ai_bench.hashing import canonical_json

from .dataset import DatasetValidationError

DECODE_TIMEOUT_SECONDS = 120
MAX_WORKER_REQUEST_BYTES = 64 * 1024
MAX_WORKER_RESPONSE_BYTES = 1024 * 1024
_WORKER_MODULE = "benchmark_packs.speech_to_text.decode_worker"


def decoder_environment() -> dict[str, str]:
    package_root = Path(__file__).resolve().parents[2]
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "LANG", "LC_ALL"}
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    environment.update(
        {
            # Staged deployments use uvicorn --app-dir src, not an editable app install.
            "PYTHONPATH": os.pathsep.join((str(package_root), str(package_root / "src"))),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
            "VECLIB_MAXIMUM_THREADS": "1",
            "BLIS_NUM_THREADS": "1",
        }
    )
    return environment


def import_in_decoder(files: tuple[DatasetImportFile, ...]) -> DatasetImportResult:
    request = canonical_json(
        [
            {
                "name": file.name,
                "path": str(file.path.absolute()),
                "size_bytes": file.size_bytes,
                "sha256": file.sha256,
                "media_type": file.media_type,
            }
            for file in files
        ]
    )
    if len(request) > MAX_WORKER_REQUEST_BYTES:
        raise DatasetValidationError("Decoder request exceeds its metadata bound.")
    try:
        completed = subprocess.run(
            [sys.executable, "-m", _WORKER_MODULE],
            input=request,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=Path(__file__).resolve().parents[2],
            env=decoder_environment(),
            timeout=DECODE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise DatasetValidationError("Upload decoder exceeded its time limit.") from None
    except OSError:
        raise DatasetValidationError("Upload decoder could not be started.") from None
    if len(completed.stdout) > MAX_WORKER_RESPONSE_BYTES:
        raise DatasetValidationError("Upload decoder response exceeds its bound.")
    if completed.returncode != 0:
        try:
            failure = json.loads(completed.stdout)
            message = failure.get("error") if isinstance(failure, dict) else None
        except (ValueError, UnicodeDecodeError):
            message = None
        raise DatasetValidationError(
            message
            if isinstance(message, str) and 0 < len(message) <= 500
            else "Upload decoder failed or exceeded its resource limits."
        )
    try:
        snapshot = SampleSnapshot.model_validate_json(completed.stdout)
    except ValueError:
        raise DatasetValidationError(
            "Upload decoder returned invalid validation evidence."
        ) from None
    paths = {file.sha256: file.path for file in files}
    for sample in snapshot.samples:
        for artifact in (sample.input, sample.reference):
            if artifact is not None and artifact.id not in paths:
                raise DatasetValidationError("Upload decoder returned an unknown file identity.")
    from types import MappingProxyType

    return DatasetImportResult(
        snapshot.dataset_revision,
        snapshot.samples,
        MappingProxyType(dict(snapshot.metadata)),
        MappingProxyType(paths),
    )
