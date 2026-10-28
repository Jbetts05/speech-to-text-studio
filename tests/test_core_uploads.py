from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path

import pytest

from ai_bench.backends import local_backend
from ai_bench.contracts import (
    BenchmarkSample,
    DatasetProvenance,
    DatasetRevision,
    JsonValue,
    Modality,
    TextArtifact,
)
from ai_bench.dataset_imports import DatasetImportFile, DatasetImportResult
from ai_bench.engine import BenchmarkEngine
from ai_bench.hashing import content_digest, sha256_digest
from ai_bench.repositories import IdempotencyConflictError
from ai_bench.uploads import UploadCapacityError, UploadFileReservation


class SyntheticImporter:
    @property
    def descriptor(self) -> Mapping[str, JsonValue]:
        return {
            "id": "synthetic-importer",
            "version": "1",
            "max_files": 3,
            "max_input_bytes": 1024,
            "max_reference_bytes": 1024,
            "max_total_bytes": 2048,
            "input_extensions": [".txt"],
            "reference_extensions": [".ref"],
            "language": "en",
        }

    def import_files(self, files: tuple[DatasetImportFile, ...]) -> DatasetImportResult:
        first = files[0]
        sample = BenchmarkSample(
            id="uploaded",
            input=TextArtifact(
                id=first.sha256,
                modality=Modality.TEXT,
                media_type="text/plain",
                size_bytes=first.size_bytes,
                uri=f"artifact://sha256/{first.sha256}",
            ),
        )
        return DatasetImportResult(
            revision=DatasetRevision(
                id="synthetic-upload",
                version="1",
                content_hash=sha256_digest((sample,)),
                provenance=DatasetProvenance(
                    source="Synthetic test", license="CC0", synthetic=True
                ),
            ),
            samples=(sample,),
            metadata={},
            files={first.sha256: first.path},
        )


def test_upload_reception_restart_finalize_and_publication(tmp_path: Path, pack_root: Path) -> None:
    engine = BenchmarkEngine(
        tmp_path, pack_root, backend=local_backend(tmp_path, durable_projections=True)
    )
    importer = SyntheticImporter()
    content = b"synthetic upload input"
    reservation = UploadFileReservation(
        name="sample.txt", size_bytes=len(content), sha256=content_digest(content)
    )
    request = {
        "benchmark_id": "synthetic_text_intent",
        "name": "Uploaded synthetic",
        "language": "en",
        "files": (reservation,),
        "idempotency_key": "upload-once",
        "importer": importer,
    }
    session = engine.uploads.admit(**request)  # type: ignore[arg-type]
    replay = engine.uploads.admit(**request)  # type: ignore[arg-type]
    assert replay.upload_id == session.upload_id
    assert not engine.datasets.list("synthetic_text_intent").items
    with pytest.raises(ValueError, match="durably complete"):
        engine.uploads.finalize(session.upload_id)
    lease = engine.uploads.claim_part(session.upload_id, session.parts[0].part_id, "receiver")
    assert lease is not None
    with pytest.raises(UploadCapacityError):
        engine.uploads.claim_part(session.upload_id, session.parts[0].part_id, "other")
    path = tmp_path / "received-body"
    path.write_bytes(content)
    engine.uploads.complete_part(lease, path)
    assert engine.uploads.claim_part(session.upload_id, session.parts[0].part_id, "replay") is None
    restarted = BenchmarkEngine(
        tmp_path, pack_root, backend=local_backend(tmp_path, durable_projections=True)
    )
    assert restarted.uploads.get(session.upload_id).parts[0].status == "complete"
    restarted.uploads.finalize(session.upload_id)
    assert restarted.uploads.process_next(lambda benchmark_id: importer)
    ready = restarted.uploads.get(session.upload_id)
    assert ready.status == "ready" and ready.dataset_id is not None
    record = restarted.datasets.get(ready.dataset_id)
    assert record.expires_at == record.published_at + timedelta(days=10)
    selected = restarted.datasets.resolve(ready.dataset_id, "synthetic_text_intent")
    assert selected.read_artifact(selected.samples()[0].input) == content
    run = restarted.create_run(
        "synthetic_text_intent", solution_ids=("keyword-standalone",), dataset_id=record.id
    )
    run_root = restarted.lifecycle.require_live(run.run_id)
    assert run_root.parent_id == record.id and run_root.expires_at == record.expires_at
    assert restarted.create_run(
        "synthetic_text_intent",
        solution_ids=("keyword-standalone",),
        dataset_id=record.id,
        idempotency_key="selection",
    )
    with pytest.raises(IdempotencyConflictError):
        restarted.create_run(
            "synthetic_text_intent",
            solution_ids=("keyword-standalone",),
            idempotency_key="selection",
        )
