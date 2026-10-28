from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from ai_bench.api import RuntimeConfigurationError, create_app, resolve_state_root
from ai_bench.contracts import Modality
from ai_bench.repositories import SqliteRepository


def test_api_health_packs_run_and_evidence(tmp_path: Path, pack_root: Path) -> None:
    app = create_app(tmp_path / "state", pack_root)
    client = TestClient(app)
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").json() == {"status": "ready"}
    packs = client.get("/api/benchmarks")
    assert packs.status_code == 200
    synthetic = next(
        pack for pack in packs.json() if pack["definition"]["id"] == "synthetic_text_intent"
    )
    assert synthetic["sample_count"] == 6
    assert synthetic["scored_sample_count"] == 6
    assert [metric["id"] for metric in synthetic["metrics"]] == [
        "exact_accuracy",
        "macro_f1",
    ]

    request_payload = {
        "benchmark_id": "synthetic_text_intent",
        "solution_ids": ["keyword-standalone"],
        "retry_limit": 2,
        "allow_networked": False,
    }
    created = client.post(
        "/api/runs",
        json=request_payload,
        headers={"Idempotency-Key": "launch-contract-test"},
    )
    assert created.status_code == 202
    run_id = created.json()["run_id"]
    assert created.json()["manifest_ref"].startswith("/api/artifacts/")
    assert created.json()["report_ref"] is None
    assert len(created.json()["manifest_artifact_id"]) == 64
    assert created.json()["report_artifact_id"] is None
    assert all(task.attempts == 0 for task in app.state.engine.repository.list_tasks(run_id))
    repeated = client.post(
        "/api/runs",
        json=request_payload,
        headers={"Idempotency-Key": "launch-contract-test"},
    )
    assert repeated.status_code == 202
    assert repeated.json() == created.json()
    assert len(client.get("/api/runs").json()) == 1
    conflict = client.post(
        "/api/runs",
        json={**request_payload, "solution_ids": ["two-stage-assisted"]},
        headers={"Idempotency-Key": "launch-contract-test"},
    )
    assert conflict.status_code == 409
    with client:
        deadline = time.monotonic() + 30
        while client.get(f"/api/runs/{run_id}").json()["report_ref"] is None:
            assert time.monotonic() < deadline, "durable supervisor did not finalize the run"
            time.sleep(0.05)
    detail = client.get(f"/api/runs/{run_id}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "completed"
    assert detail.json()["manifest_ref"].startswith("/api/artifacts/")
    assert detail.json()["report_ref"].startswith("/api/artifacts/")
    events = client.get(f"/api/runs/{run_id}/events", params={"limit": 10})
    assert events.status_code == 200
    assert events.json()
    report = client.get(f"/api/runs/{run_id}/report")
    assert report.status_code == 200
    assert "score" not in report.json()
    csv_report = client.get(f"/api/runs/{run_id}/report", params={"format": "csv"})
    assert csv_report.status_code == 200
    assert csv_report.text.startswith("run_id,task_id")
    runtime = client.get("/api/runtime")
    assert runtime.status_code == 200
    assert runtime.json()["execution_backend"] == "single-instance-sqlite-filesystem"
    assert runtime.json()["horizontal_scaling"] is False
    frontend = client.get("/")
    assert frontend.status_code == 200
    assert "default-src 'self'" in frontend.headers["content-security-policy"]
    assert frontend.headers["x-frame-options"] == "DENY"
    assert client.get("/api/not-a-route").status_code == 404

    artifact_id = report.json()["results"][0]["evidence"][0]["artifact_id"]
    artifact = client.get(f"/api/artifacts/{artifact_id}")
    assert artifact.status_code == 200
    assert artifact.headers["x-content-type-options"] == "nosniff"
    assert artifact.headers["content-disposition"].startswith("attachment;")
    assert artifact.headers["content-security-policy"] == "sandbox; default-src 'none'"


def test_api_rejects_invalid_and_unbounded_inputs(tmp_path: Path, pack_root: Path) -> None:
    client = TestClient(create_app(tmp_path / "state", pack_root))
    assert client.get("/api/artifacts/not-a-sha256-digest").status_code == 404
    response = client.post(
        "/api/runs",
        json={"benchmark_id": "INVALID PATH", "solution_ids": [], "allow_networked": False},
    )
    assert response.status_code == 422
    assert client.get("/api/runs", params={"limit": 501}).status_code == 422


def test_artifact_endpoint_honors_evidence_media_type_for_identical_bytes(
    tmp_path: Path,
    pack_root: Path,
) -> None:
    app = create_app(tmp_path / "state", pack_root)
    content = b'{"intent":"support"}'
    text_descriptor = app.state.engine.artifacts.put_bytes(
        content,
        modality=Modality.TEXT,
        media_type="text/plain; charset=utf-8",
    )
    structured_descriptor = app.state.engine.artifacts.put_bytes(
        content,
        modality=Modality.STRUCTURED,
        media_type="application/json",
    )
    assert text_descriptor.id == structured_descriptor.id
    app.state.engine.repository.register_artifact(text_descriptor)
    app.state.engine.repository.register_artifact(structured_descriptor)
    client = TestClient(app)

    text = client.get(
        f"/api/artifacts/{text_descriptor.id}",
        headers={"Accept": "text/plain; charset=utf-8"},
    )
    structured = client.get(
        f"/api/artifacts/{text_descriptor.id}",
        headers={"Accept": "application/json"},
    )

    assert text.status_code == 200
    assert text.headers["content-type"] == "text/plain; charset=utf-8"
    assert text.content == content
    assert structured.status_code == 200
    assert structured.headers["content-type"] == "application/json"
    assert structured.content == content


def test_readiness_fails_when_configured_dependency_is_unavailable(
    tmp_path: Path, pack_root: Path
) -> None:
    client = TestClient(
        create_app(tmp_path / "state", pack_root, readiness_checks=(lambda: False,))
    )
    response = client.get("/ready")
    assert response.status_code == 503


def test_state_root_uses_environment_in_local_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pack_root: Path
) -> None:
    state_root = tmp_path / "configured-state"
    monkeypatch.setenv("AI_BENCH_RUNTIME", "local")
    monkeypatch.setenv("AI_BENCH_STATE_ROOT", str(state_root))
    app = create_app(pack_root=pack_root)
    assert app.state.engine.state_root == state_root.resolve()


def test_hosted_runtime_defaults_to_persistent_home_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_BENCH_RUNTIME", "azure")
    monkeypatch.delenv("AI_BENCH_STATE_ROOT", raising=False)
    assert resolve_state_root() == Path("/home/data/ai-bench").resolve()


def test_hosted_runtime_rejects_ephemeral_or_relative_state(
    monkeypatch: pytest.MonkeyPatch, pack_root: Path
) -> None:
    monkeypatch.setenv("AI_BENCH_RUNTIME", "azure")
    monkeypatch.setenv("AI_BENCH_STATE_ROOT", ".ai-bench")
    with pytest.raises(RuntimeConfigurationError, match="persistent state root"):
        create_app(pack_root=pack_root)


def test_hosted_runtime_rejects_traversal_outside_persistent_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_BENCH_RUNTIME", "azure")
    monkeypatch.setenv("AI_BENCH_STATE_ROOT", "/home/data/../../tmp")
    with pytest.raises(RuntimeConfigurationError, match="persistent state root"):
        resolve_state_root()


def test_concurrent_idempotent_launches_return_persisted_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pack_root: Path
) -> None:
    app = create_app(tmp_path / "state", pack_root)
    repository = cast(SqliteRepository, app.state.engine.repository)
    manifest = app.state.engine.create_run(
        "synthetic_text_intent",
        solution_ids=("keyword-standalone",),
        retry_limit=2,
        idempotency_key="concurrent-launch",
    )
    while lease := repository.claim_task("setup-worker"):
        app.state.engine._execute_lease(lease)
    assert all(task.status.value == "succeeded" for task in repository.list_tasks(manifest.run_id))
    payload: dict[str, Any] = {
        "benchmark_id": "synthetic_text_intent",
        "solution_ids": ["keyword-standalone"],
        "retry_limit": 2,
        "allow_networked": False,
    }

    def launch() -> tuple[int, dict[str, Any]]:
        response = TestClient(app).post(
            "/api/runs",
            json=payload,
            headers={"Idempotency-Key": "concurrent-launch"},
        )
        return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = tuple(executor.map(lambda _: launch(), range(2)))

    assert [status for status, _ in responses] == [202, 202]
    assert responses[0][1] == responses[1][1]
    assert responses[0][1]["report_ref"] is None
    original_acquire = repository.acquire_finalizer
    finalizer_barrier = threading.Barrier(2)

    def synchronized_acquire(run_id: str) -> int | None:
        token = original_acquire(run_id)
        if token is not None:
            finalizer_barrier.wait(timeout=10)
        return token

    monkeypatch.setattr(repository, "acquire_finalizer", synchronized_acquire)
    with ThreadPoolExecutor(max_workers=2) as executor:
        reports = tuple(
            executor.map(lambda _: app.state.engine._finalize(manifest.run_id), range(2))
        )
    assert reports[0] == reports[1]
    assert (
        sum(
            event.event_type == "run.finalized" for event in repository.list_events(manifest.run_id)
        )
        == 1
    )
