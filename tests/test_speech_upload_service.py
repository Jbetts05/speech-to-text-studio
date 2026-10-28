from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from ai_bench.engine import BenchmarkEngine
from ai_bench.uploads import UploadFileReservation
from benchmark_packs.speech_to_text.upload import SpeechDatasetImporter
from tests.speech_support import make_audio
from tests.test_speech_upload import stage_file


@pytest.mark.parametrize("valid_reference", [True, False])
def test_durable_upload_service_publishes_only_fully_validated_speech(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, valid_reference: bool
) -> None:
    monkeypatch.setenv("AI_BENCH_RUNTIME", "local")
    monkeypatch.setenv("AI_BENCH_ALLOW_NETWORKED_SOLUTIONS", "false")
    importer = SpeechDatasetImporter()
    files = (
        stage_file(tmp_path, "invented.mp3", make_audio(".mp3"), "audio/mpeg"),
        stage_file(tmp_path, "invented.txt", b"Synthetic reference" if valid_reference else b"..."),
    )
    engine = BenchmarkEngine(tmp_path / "state")
    session = engine.uploads.admit(
        benchmark_id="speech_to_text",
        name="Synthetic upload integration",
        language="en",
        files=tuple(
            UploadFileReservation(
                name=file.name,
                size_bytes=file.size_bytes,
                sha256=file.sha256,
                media_type=file.media_type,
            )
            for file in files
        ),
        idempotency_key="synthetic-upload-integration",
        importer=importer,
    )
    assert engine.datasets.list("speech_to_text").items == ()
    for part, file in zip(session.parts, files, strict=True):
        lease = engine.uploads.claim_part(session.upload_id, part.part_id, "synthetic-receiver")
        assert lease is not None
        engine.uploads.complete_part(lease, file.path)
        assert (
            engine.uploads.claim_part(session.upload_id, part.part_id, "synthetic-receiver") is None
        )
    engine.uploads.finalize(session.upload_id)
    assert engine.datasets.list("speech_to_text").items == ()
    assert engine.uploads.process_next(lambda _: importer)
    completed = engine.uploads.get(session.upload_id)
    if not valid_reference:
        assert completed.status == "failed"
        assert completed.dataset_id is None
        assert engine.datasets.list("speech_to_text").items == ()
        return
    assert completed.status == "ready"
    assert completed.dataset_id is not None
    record = engine.datasets.get(completed.dataset_id)
    assert record.expires_at == record.published_at + timedelta(days=10)
    published = engine.datasets.resolve(completed.dataset_id, "speech_to_text")
    sample = published.samples()[0]
    assert sample.input.media_type == "audio/mpeg"
    assert published.read_artifact(sample.input) == files[0].path.read_bytes()
    assert sample.reference is not None
    assert published.read_artifact(sample.reference) == files[1].path.read_bytes()
    assert not list(engine.uploads.work_root.iterdir())
