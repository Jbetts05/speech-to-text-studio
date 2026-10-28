from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from ai_bench.artifacts import ArtifactIntegrityError, FileArtifactStore
from ai_bench.dataset_imports import DatasetAdmissionFile, DatasetImportFile
from ai_bench.engine import BenchmarkEngine
from ai_bench.hashing import content_digest
from ai_bench.lifecycle import ExpiredRootError
from ai_bench.repositories import IdempotencyConflictError, LeaseLostError
from ai_bench.uploads import UploadCapacityError, UploadFileReservation
from tests.test_core_uploads import SyntheticImporter


def test_session_facade_shares_receiver_limits_and_rejects_stale_completion(
    tmp_path: Path, pack_root: Path
) -> None:
    first = BenchmarkEngine(tmp_path / "state", pack_root)
    second = BenchmarkEngine(tmp_path / "state", pack_root)
    contents = (b"first", b"second", b"third")
    reservations = tuple(
        UploadFileReservation(
            name=f"{number}.txt", size_bytes=len(content), sha256=content_digest(content)
        )
        for number, content in enumerate(contents)
    )
    session = first.uploads.create_session(
        benchmark_id="synthetic_text_intent",
        name="Synthetic session",
        language="en",
        files=reservations,
        idempotency_key="session",
        importer=SyntheticImporter(),
    )
    assert str(tmp_path) not in session.model_dump_json()
    one = first.uploads.claim_part(session.upload_id, session.parts[0].part_id, "first")
    old_two = second.uploads.claim_part(session.upload_id, session.parts[1].part_id, "second")
    assert one is not None and old_two is not None
    with pytest.raises(UploadCapacityError):
        first.uploads.claim_part(session.upload_id, session.parts[2].part_id, "third")
    second.uploads.release_part(old_two)
    new_two = first.uploads.claim_part(session.upload_id, session.parts[1].part_id, "replacement")
    assert new_two is not None and new_two.token != old_two.token
    with first.uploads.receiver_workspace(new_two) as directory:
        source = directory / "body"
        source.write_bytes(contents[1])
        with pytest.raises(LeaseLostError):
            second.uploads.receive_file(old_two, source)
        updated = first.uploads.receive_file(new_two, source)
        assert updated.parts[1].status == "complete"
    assert second.uploads.status(session.upload_id).parts[1].status == "complete"
    assert not directory.exists()
    first.uploads.release_part(one)


def test_finalize_rechecks_durable_bytes_before_scheduling_validation(
    tmp_path: Path, pack_root: Path
) -> None:
    engine = BenchmarkEngine(tmp_path / "state", pack_root)
    content = b"valid synthetic reference"
    session = engine.uploads.create_session(
        benchmark_id="synthetic_text_intent",
        name="Synthetic session",
        language="en",
        files=(
            UploadFileReservation(
                name="input.txt",
                size_bytes=len(content),
                sha256=content_digest(content),
            ),
        ),
        idempotency_key="verified-finalize",
        importer=SyntheticImporter(),
    )
    lease = engine.uploads.claim_part(session.upload_id, session.parts[0].part_id, "receiver")
    assert lease is not None
    with engine.uploads.receiver_workspace(lease) as directory:
        source = directory / "body"
        source.write_bytes(content)
        engine.uploads.receive_file(lease, source)
    assert isinstance(engine.uploads.staging, FileArtifactStore)
    stored = engine.uploads.staging._path(content_digest(content))
    stored.write_bytes(b"x" * len(content))
    with pytest.raises(ArtifactIntegrityError):
        engine.uploads.finalize(session.upload_id)
    assert engine.uploads.status(session.upload_id).status == "receiving"
    assert engine.backend.state.get(f"upload-job:{session.upload_id}") is None
    stored.write_bytes(content)
    assert engine.uploads.finalize(session.upload_id).status == "validating"
    assert engine.backend.state.get(f"upload-job:{session.upload_id}") is not None


def test_metadata_completion_and_one_hour_expiry(tmp_path: Path, pack_root: Path) -> None:
    engine = BenchmarkEngine(tmp_path / "state", pack_root)
    content = b"exact original content"
    session = engine.uploads.create_upload_session(
        benchmark_id="synthetic_text_intent",
        name="Reserved",
        language="en",
        specs=(
            UploadFileReservation(
                name="input.txt",
                size_bytes=len(content),
                sha256=content_digest(content),
                media_type="text/plain",
            ),
        ),
        idempotency_key="metadata-contract",
        importer=SyntheticImporter(),
    )
    assert session.expires_at == session.created_at + timedelta(hours=1)
    lease = engine.uploads.claim_upload_part(
        session.upload_id, session.parts[0].part_id, "receiver"
    )
    assert lease is not None
    with engine.uploads.receiver_workspace(lease) as directory:
        source = directory / "body"
        source.write_bytes(content)
        received = DatasetImportFile(
            "input.txt", source, len(content), content_digest(content), "text/plain"
        )
        with pytest.raises(IdempotencyConflictError, match="metadata"):
            engine.uploads.complete_upload_part(lease, replace(received, name="different.txt"))
        assert engine.uploads.complete_upload_part(lease, received).parts[0].status == "complete"
    assert engine.uploads.get_upload(session.upload_id).status == "receiving"
    engine.lifecycle.clock = lambda: session.expires_at
    with pytest.raises(ExpiredRootError):
        engine.uploads.finalize_upload(session.upload_id)


def test_optional_admission_validator_runs_without_paths_or_reception(
    tmp_path: Path, pack_root: Path
) -> None:
    class RejectingImporter(SyntheticImporter):
        def validate_admission(self, files: Sequence[DatasetAdmissionFile]) -> None:
            assert all(not hasattr(file, "path") for file in files)
            raise ValueError("synthetic metadata rejection")

    engine = BenchmarkEngine(tmp_path / "state", pack_root)
    with pytest.raises(ValueError, match="metadata rejection"):
        engine.uploads.create_upload_session(
            benchmark_id="synthetic_text_intent",
            name="Rejected",
            language="en",
            specs=(
                UploadFileReservation(
                    name="input.txt",
                    size_bytes=1,
                    sha256=content_digest(b"a"),
                    media_type="text/plain",
                ),
            ),
            idempotency_key="rejected-metadata",
            importer=RejectingImporter(),
        )
    assert not engine.backend.state.page("upload:").rows
    assert not engine.backend.state.page("upload-receiver:").rows
