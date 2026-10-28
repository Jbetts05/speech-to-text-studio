from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_bench.api import create_app
from ai_bench.backends import local_backend
from ai_bench.contracts import JsonValue
from benchmark_packs.speech_to_text.adapters import HttpResponse
from benchmark_packs.speech_to_text.pack import get_pack
from tests.speech_support import make_flac
from tests.test_speech_adapters import provider_configuration


@pytest.mark.parametrize("durable", [False, True], ids=["local", "table-projection"])
def test_real_upload_publication_playback_restart_and_expiry(
    tmp_path: Path,
    pack_root: Path,
    durable: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

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
            assert audio.startswith(b"fLaC")
            assert media_type == "audio/flac"
            assert token == "synthetic-token"
            assert "reference" not in fields and "prompt" not in fields
            assert "FIRST SYNTHETIC REFERENCE" not in str(fields)
            assert "SECOND SYNTHETIC REFERENCE" not in str(fields)
            assert filename not in {"first.flac", "second.flac"}
            calls.append(hashlib.sha256(audio).hexdigest())
            body: JsonValue = (
                {"text": "synthetic transcript"}
                if audio_field == "file"
                else {"combinedPhrases": [{"text": "synthetic transcript"}]}
            )
            return HttpResponse(
                200,
                {"Content-Type": "application/json", "apim-request-id": "synthetic-request"},
                json.dumps(body).encode(),
                Decimal(1),
            )

    pack = get_pack()
    configured = replace(
        pack,
        solutions=tuple(
            replace(
                solution,
                configuration=provider_configuration(),
                transport=MockTransport(),
                token_provider=lambda: "synthetic-token",
            )
            for solution in pack.solutions
        ),
    )
    monkeypatch.setattr("ai_bench.engine.get_pack", lambda *_: configured)
    monkeypatch.setenv("AI_BENCH_ALLOW_NETWORKED_SOLUTIONS", "true")
    root = tmp_path / "runtime"
    backend = local_backend(root, durable_projections=durable)
    app = create_app(root, pack_root, backend=backend)
    client = TestClient(app)
    files = {
        "first.flac": make_flac(1600),
        "first.txt": b"FIRST SYNTHETIC REFERENCE",
        "second.flac": make_flac(3200),
        "second.txt": b"SECOND SYNTHETIC REFERENCE",
    }
    request = {
        "benchmark_id": "speech_to_text",
        "name": "Public synthetic dataset",
        "language": "en",
        "public_acknowledged": True,
        "rights_acknowledged": True,
        "language_acknowledged": True,
        "files": [
            {
                "name": name,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "media_type": "audio/flac" if name.endswith(".flac") else "text/plain",
            }
            for name, content in files.items()
        ],
    }
    headers = {"Idempotency-Key": "synthetic-session"}
    created = client.post("/api/dataset-uploads", json=request, headers=headers)
    assert created.status_code == 201, created.text
    session = created.json()
    session_path = f"/api/dataset-uploads/{session['upload_id']}"
    assert client.post(session_path + "/finalize", json={}).status_code == 409
    for part in session["parts"]:
        content = files[part["name"]]
        response = client.put(
            f"{session_path}/parts/{part['part_id']}",
            content=content,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert response.status_code == 200, response.text
        assert client.get(f"/api/artifacts/{part['sha256']}").status_code in {404, 410}, (
            "staging must not be public evidence"
        )
    replay = client.post("/api/dataset-uploads", json=request, headers=headers)
    assert replay.status_code == 201
    assert replay.json()["upload_id"] == session["upload_id"]
    assert client.post(session_path + "/finalize", json={}).status_code == 202
    assert app.state.engine.uploads.process_next(app.state.engine.dataset_importer)
    ready = client.get(session_path)
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready", ready.text
    dataset_id = ready.json()["dataset_id"]
    detail = client.get(f"/api/datasets/{dataset_id}")
    assert detail.status_code == 200, detail.text
    dataset = detail.json()
    assert dataset["name"] == request["name"]
    assert dataset["sample_count"] == dataset["scored_sample_count"] == 2
    assert not dataset["pinned"]
    assert datetime.fromisoformat(dataset["expires_at"]) - datetime.fromisoformat(
        dataset["published_at"]
    ) == timedelta(days=10)
    listing = client.get("/api/benchmarks/speech_to_text/datasets").json()
    assert [item["id"] for item in listing["items"]] == [dataset_id]
    assert not listing["items"][0]["sample_artifacts"]
    input_id = hashlib.sha256(files["first.flac"]).hexdigest()
    artifact_url = f"/api/artifacts/{input_id}"
    partial = client.get(artifact_url, headers={"Range": "bytes=0-3"})
    assert partial.status_code == 206
    assert partial.content == b"fLaC"
    assert client.head(artifact_url).headers["content-length"] == str(len(files["first.flac"]))
    assert client.get(artifact_url).content == files["first.flac"]

    run_request = {
        "benchmark_id": "speech_to_text",
        "dataset_id": dataset_id,
        "solution_ids": [solution.id for solution in configured.solutions],
        "retry_limit": 3,
        "allow_networked": True,
    }
    launched = client.post(
        "/api/runs", json=run_request, headers={"Idempotency-Key": "uploaded-five-solutions"}
    )
    assert launched.status_code == 202, launched.text
    run_id = launched.json()["run_id"]
    assert calls == []
    initial_run = client.get(f"/api/runs/{run_id}").json()
    assert initial_run["expires_at"] == dataset["expires_at"]
    assert len(initial_run["tasks"]) == 10

    restarted = create_app(
        root, pack_root, backend=local_backend(root, durable_projections=durable)
    )
    restored = TestClient(restarted)
    assert restored.get(f"/api/datasets/{dataset_id}").json() == dataset
    assert restored.get(artifact_url).content == files["first.flac"]
    assert restored.get(session_path).json()["status"] == "ready"
    report = restarted.state.engine.run_to_completion(run_id)
    assert report.status.value == "completed"
    assert len(report.results) == len(calls) == 10
    assert len(set(calls)) == 2
    report_response = restored.get(f"/api/runs/{run_id}/report")
    assert report_response.status_code == 200
    assert report_response.content == restarted.state.engine.get_report_bytes(run_id)
    assert (
        restored.post(
            "/api/runs", json=run_request, headers={"Idempotency-Key": "uploaded-five-solutions"}
        ).json()["run_id"]
        == run_id
    )
    assert len(calls) == 10
    expired_at = datetime.fromisoformat(dataset["expires_at"])
    restarted.state.engine.lifecycle.clock = lambda: expired_at
    restarted.state.engine.uploads.staging_lifecycle.clock = lambda: expired_at
    assert restored.get(f"/api/datasets/{dataset_id}").status_code == 410
    assert restored.get(f"/api/runs/{run_id}").status_code == 410
    assert restored.get(f"/api/runs/{run_id}/report").status_code == 410
    assert restored.get(f"/api/runs/{run_id}/events").status_code == 410
    assert restored.get(artifact_url).status_code == 410
    assert restored.head(artifact_url).status_code == 410
    assert restored.get(artifact_url, headers={"If-None-Match": "*"}).status_code == 410
    assert restored.get("/api/benchmarks/speech_to_text/datasets").json()["items"] == []
    for _ in range(20):
        restarted.state.engine.reap_expired(limit=10)
        if (
            restarted.state.engine.backend.state.get(f"dataset:{dataset_id}") is None
            and restarted.state.engine.repository.get_run(run_id) is None
        ):
            break
    assert restarted.state.engine.backend.state.get(f"dataset:{dataset_id}") is None
    assert restarted.state.engine.repository.get_run(run_id) is None
