from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from ai_bench.contracts import JsonValue, SampleSnapshot
from ai_bench.engine import BenchmarkEngine
from ai_bench.hashing import content_digest
from benchmark_packs.speech_to_text import dataset
from benchmark_packs.speech_to_text.adapters import HttpResponse
from benchmark_packs.speech_to_text.dataset import SpeechDataset, publish_archive
from benchmark_packs.speech_to_text.pack import get_pack
from tests.speech_support import make_archive
from tests.test_speech_adapters import provider_configuration


def test_complete_26_by_five_pilot_uses_only_immutable_bytes_and_mocked_transport(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    archive = make_archive(tmp_path / "synthetic.tar.gz")
    monkeypatch.setattr(
        dataset,
        "ARCHIVE_MD5",
        hashlib.md5(archive.read_bytes(), usedforsecurity=False).hexdigest(),
    )
    revision = publish_archive(archive, tmp_path / "publications")
    seen: list[tuple[str, str]] = []

    class MockTransport:
        def post(
            self,
            url: str,
            *,
            token: str,
            audio_field: str,
            filename: str,
            audio: bytes,
            media_type: str,
            fields: Mapping[str, str],
        ) -> HttpResponse:
            assert audio.startswith(b"fLaC") and filename.endswith(".flac")
            assert media_type == "audio/flac"
            assert token == "invented-token"
            assert "reference" not in fields and "prompt" not in fields
            seen.append((filename, content_digest(audio)))
            body: JsonValue = (
                {"text": "synthetic word"}
                if audio_field == "file"
                else {"combinedPhrases": [{"text": "synthetic word"}]}
            )
            return HttpResponse(
                200,
                {"Content-Type": "application/json", "apim-request-id": "synthetic-request"},
                json.dumps(body).encode(),
                Decimal(3),
            )

    pack = get_pack()
    configured = replace(
        pack,
        dataset=SpeechDataset(revision),
        solutions=tuple(
            replace(
                solution,
                configuration=provider_configuration(),
                transport=MockTransport(),
                token_provider=lambda: "invented-token",
            )
            for solution in pack.solutions
        ),
    )
    monkeypatch.setattr("ai_bench.engine.get_pack", lambda *_: configured)
    monkeypatch.setenv("AI_BENCH_ALLOW_NETWORKED_SOLUTIONS", "true")
    engine = BenchmarkEngine(tmp_path / "state")
    manifest = engine.create_run("speech_to_text", allow_networked=True)
    assert manifest.sample_snapshot_artifact_id is not None
    snapshot = SampleSnapshot.model_validate_json(
        engine.artifacts.get_bytes(manifest.sample_snapshot_artifact_id),
    )
    assert len(manifest.sample_ids) == 26
    assert len(manifest.solution_ids) == 5
    assert len(engine.repository.list_tasks(manifest.run_id)) == 130
    assert seen == []
    for sample in snapshot.samples:
        assert engine.artifacts.get_bytes(sample.input.id).startswith(b"fLaC")
        assert sample.reference is not None
        assert engine.artifacts.get_bytes(sample.reference.id) == b"SYNTHETIC WORD"
    shutil.rmtree(revision)
    archive.unlink()
    report = BenchmarkEngine(tmp_path / "state").run_to_completion(manifest.run_id)
    assert len(report.results) == len(seen) == 130
    assert len({filename for filename, _ in seen}) == 26
    assert len(set(seen)) == 26
    assert report.status.value == "completed"
    assert all(metrics["word_error_rate"] == 0 for metrics in report.quality.values())
    assert all(value is None for value in report.cost.values())
    assert all(value.complete and value.comparable for value in report.coverage.values())
    assert all(value["provider_ms"] == 78 for value in report.latency_breakdown.values())
    assert all(result.attempts == 1 for result in report.results)
