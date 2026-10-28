from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from ai_bench.api import create_app
from ai_bench.contracts import (
    ExecutionOutput,
    ExecutionRequest,
    ParsedOutput,
    ProviderFailureEvidence,
    RetryDelay,
    RunManifest,
    SolutionAdapter,
    Usage,
)
from ai_bench.engine import BenchmarkEngine
from ai_bench.failures import ProviderFailure
from ai_bench.hashing import content_digest, sha256_digest
from ai_bench.packs import BenchmarkPack, get_pack
from ai_bench.repositories import LeaseLostError, RepositoryError, SqliteRepository
from ai_bench.worker import RunSupervisor


class ScopedSolution:
    def __init__(self, delegate: SolutionAdapter) -> None:
        self.delegate = delegate
        self.id = delegate.id
        self.version = delegate.version
        self.networked = False
        self.pipeline = delegate.pipeline
        self.compatibility = delegate.compatibility
        self.pricing = delegate.pricing
        self.throttle_scope = content_digest(self.id.encode())
        self.failure: ProviderFailure | None = None
        self.started: threading.Event | None = None
        self.release: threading.Event | None = None

    def validate_execution(self) -> None:
        return None

    def execution_settings(self) -> dict[str, str]:
        return {"synthetic_test_contract": "1"}

    def execute(self, request: ExecutionRequest) -> ExecutionOutput:
        assert request.sample.reference is None
        assert content_digest(request.input_bytes) == request.sample.input.id
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            assert self.release.wait(15)
        if self.failure is not None:
            failure, self.failure = self.failure, None
            raise failure
        return self.delegate.execute(request)

    def parse(self, output: ExecutionOutput) -> ParsedOutput:
        return self.delegate.parse(output)


def scoped_pack(
    pack_root: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[BenchmarkPack, tuple[ScopedSolution, ...]]:
    original = get_pack("synthetic_text_intent", pack_root)
    solutions = tuple(ScopedSolution(delegate) for delegate in original.solutions)
    pack = replace(cast(Any, original), solutions=solutions)
    monkeypatch.setattr("ai_bench.engine.get_pack", lambda *_: pack)
    monkeypatch.setattr("ai_bench.worker.get_pack", lambda *_: pack)
    return pack, solutions


def rate_limit(seconds: int = 12, *, retryable: bool = True) -> ProviderFailure:
    return ProviderFailure(
        ProviderFailureEvidence(
            classification="rate-limit" if retryable else "authentication",
            message="Synthetic provider failure.",
            http_status=429 if retryable else 403,
            provider_request_id="synthetic-request-id",
            retryable=retryable,
            usage=Usage(
                dimensions={"calls": Decimal(1)},
                complete=False,
                uncertainty="Synthetic failure billing was not observed.",
            ),
            retry_delay=RetryDelay(
                scheduled_seconds=Decimal(seconds),
                provider_seconds=Decimal(seconds),
                source="retry-after-seconds",
            )
            if retryable
            else None,
        ),
        raw=b'{"error":{"message":"synthetic provider failure"}}',
    )


def test_catalog_discovery_and_health_do_not_require_external_speech_data(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pack_root: Path,
) -> None:
    monkeypatch.delenv("AI_BENCH_SPEECH_DATASET_REVISION", raising=False)
    monkeypatch.delenv("AI_BENCH_SPEECH_PROVIDER_CONFIG", raising=False)
    app = create_app(tmp_path / "state", pack_root)
    client = TestClient(app)
    speech = next(
        item
        for item in client.get("/api/benchmarks").json()
        if item["definition"]["id"] == "speech_to_text"
    )
    assert speech["dataset_availability"]["status"] == "not-configured"
    assert speech["sample_count"] == 26 and len(speech["solutions"]) == 5
    assert speech["sample_count"] * len(speech["solutions"]) == 130
    assert all(item["networked"] for item in speech["solutions"])
    assert client.get("/health").status_code == client.get("/ready").status_code == 200
    assert client.post("/api/runs", json={"benchmark_id": "speech_to_text"}).status_code == 422
    assert app.state.engine.repository.list_runs() == ()
    monkeypatch.setenv("AI_BENCH_SPEECH_DATASET_REVISION", str(tmp_path / "corrupt"))
    speech = next(
        item
        for item in client.get("/api/benchmarks").json()
        if item["definition"]["id"] == "speech_to_text"
    )
    assert speech["dataset_availability"]["status"] == "invalid"
    assert client.get("/ready").status_code == 200


def test_async_acknowledgment_does_not_wait_for_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pack_root: Path,
) -> None:
    _, solutions = scoped_pack(pack_root, monkeypatch)
    started, release = threading.Event(), threading.Event()
    solutions[0].started, solutions[0].release = started, release
    app = create_app(tmp_path / "state", pack_root)
    with TestClient(app) as client:
        try:
            response = client.post(
                "/api/runs",
                json={
                    "benchmark_id": "synthetic_text_intent",
                    "solution_ids": [solutions[0].id],
                },
            )
            assert response.status_code == 202
            assert response.json()["report_ref"] is None
            assert started.wait(5)
            assert not release.is_set()
            run_id = response.json()["run_id"]
            detail = client.get(f"/api/runs/{run_id}").json()
            assert len(detail["tasks"]) == 6 and detail["status"] == "running"
            assert len(detail["sample_artifacts"]) == 6
            assert all(len(artifacts) == 2 for artifacts in detail["sample_artifacts"].values())
            assert detail["manifest"]["schema_version"] == "2"
            snapshot = detail["manifest"]["sample_snapshot_artifact_id"]
            assert client.get(f"/api/artifacts/{snapshot}").status_code == 200
            assert client.get("/health").status_code == 200
        finally:
            release.set()


def test_startup_repairs_initialization_and_runs_without_mutable_dataset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pack_root: Path,
) -> None:
    pack, _ = scoped_pack(pack_root, monkeypatch)
    engine = BenchmarkEngine(tmp_path / "state", pack_root)
    original_reconcile = engine.repository.reconcile_tasks
    monkeypatch.setattr(
        engine.repository,
        "reconcile_tasks",
        lambda _: (_ for _ in ()).throw(OSError("synthetic interruption")),
    )
    with pytest.raises(OSError, match="interruption"):
        engine.create_run("synthetic_text_intent", idempotency_key="recover-initialization")
    run = engine.repository.list_runs()[0]
    assert engine.repository.list_tasks(run.manifest.run_id) == ()
    monkeypatch.setattr(engine.repository, "reconcile_tasks", original_reconcile)

    class UnavailableDataset:
        id = "unavailable"

        @property
        def revision(self) -> Any:
            raise AssertionError("resume must not read a mutable dataset revision")

        def samples(self) -> Any:
            raise AssertionError("resume must use the persisted sample snapshot")

    changed = replace(cast(Any, pack), dataset=UnavailableDataset())
    monkeypatch.setattr("ai_bench.engine.get_pack", lambda *_: changed)
    monkeypatch.setattr("ai_bench.worker.get_pack", lambda *_: changed)
    app = create_app(tmp_path / "state", pack_root)
    with TestClient(app) as client:
        replay = client.post(
            "/api/runs",
            json={"benchmark_id": "synthetic_text_intent"},
            headers={"Idempotency-Key": "recover-initialization"},
        )
        assert replay.status_code == 202 and replay.json()["run_id"] == run.manifest.run_id
        deadline = time.monotonic() + 30
        while client.get(f"/api/runs/{run.manifest.run_id}").json()["report_ref"] is None:
            assert time.monotonic() < deadline
            time.sleep(0.05)
    assert len(app.state.engine.get_report(run.manifest.run_id).results) == 12


@pytest.mark.parametrize("crash_before_schedule", [False, True])
def test_persisted_cooldown_survives_restart_and_other_scopes_progress(
    crash_before_schedule: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pack_root: Path,
) -> None:
    clock = [1000.0]
    monkeypatch.setattr("ai_bench.repositories.time.time", lambda: clock[0])
    _, solutions = scoped_pack(pack_root, monkeypatch)
    engine = BenchmarkEngine(tmp_path / "state", pack_root)
    manifest = engine.create_run("synthetic_text_intent")
    first = engine.repository.claim_task("first", lease_seconds=1)
    assert first is not None
    solution = next(solution for solution in solutions if solution.id == first.task.solution_id)
    solution.failure = rate_limit()
    with pytest.raises(ProviderFailure) as caught:
        engine._execute_lease(first)
    if not crash_before_schedule:
        engine.repository.fail_attempt(first, caught.value)
    else:
        clock[0] += 2
    reopened = BenchmarkEngine(tmp_path / "state", pack_root)
    if crash_before_schedule:
        assert reopened.repository.recover_expired_leases() == (1, 0)
        with pytest.raises(LeaseLostError):
            engine.repository.fail_attempt(first, caught.value)
    blocked = reopened.repository.get_task(first.task.id)
    assert blocked is not None and blocked.attempts == 1 and blocked.lease_owner is None
    assert blocked.cooldown_until is not None and blocked.cooldown_until.timestamp() == 1012
    attempts = reopened.repository.list_attempts(first.task.id)
    assert attempts[0].failure is not None and attempts[0].failure.http_status == 429
    assert attempts[0].failure.retry_delay is not None
    assert attempts[0].failure.retry_delay.provider_seconds == 12
    assert reopened.artifacts.get_bytes(attempts[0].evidence[0].artifact_id) == caught.value.raw
    progressed = 0
    while lease := reopened.repository.claim_task("other"):
        assert lease.task.throttle_scope != first.task.throttle_scope
        reopened._execute_lease(lease)
        progressed += 1
    assert progressed == 6
    assert reopened.repository.next_work_delay(manifest.run_id) > 1
    assert all(
        task.attempts == (1 if task.id == first.task.id else 0)
        for task in reopened.repository.list_tasks(manifest.run_id)
        if task.throttle_scope == first.task.throttle_scope
    )
    clock[0] = 1012
    report = reopened.run_to_completion(manifest.run_id)
    assert report.cost[solution.id] is None
    assert report.reliability[solution.id]["total_attempts"] == 7
    assert report.latency_breakdown[solution.id]["cooldown_wait_ms"] == 72000
    assert report.coverage[solution.id].complete
    assert report.usage_uncertainty[solution.id]


def test_nonretryable_failure_remains_terminal_after_crash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pack_root: Path,
) -> None:
    clock = [1000.0]
    monkeypatch.setattr("ai_bench.repositories.time.time", lambda: clock[0])
    _, solutions = scoped_pack(pack_root, monkeypatch)
    engine = BenchmarkEngine(tmp_path / "state", pack_root)
    manifest = engine.create_run("synthetic_text_intent")
    lease = engine.repository.claim_task("lost", lease_seconds=1)
    assert lease is not None
    selected = next(solution for solution in solutions if solution.id == lease.task.solution_id)
    selected.failure = rate_limit(retryable=False)
    with pytest.raises(ProviderFailure):
        engine._execute_lease(lease)
    clock[0] += 2
    assert engine.repository.recover_expired_leases() == (0, 1)
    report = engine.run_to_completion(manifest.run_id)
    assert report.status.value == "completed-with-failures"
    assert not report.coverage[selected.id].complete
    assert all(not coverage.comparable for coverage in report.coverage.values())
    assert len(report.coverage[selected.id].evaluated_sample_ids) == 5


def test_failure_evidence_must_precede_retry(tmp_path: Path, pack_root: Path) -> None:
    engine = BenchmarkEngine(tmp_path / "state", pack_root)
    engine.create_run("synthetic_text_intent")
    lease = engine.repository.claim_task("worker")
    assert lease is not None
    with pytest.raises(RepositoryError, match="must precede"):
        engine.repository.fail_attempt(lease, rate_limit())


def test_only_one_supervisor_owns_a_state_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clock = [1000.0]
    monkeypatch.setattr("ai_bench.repositories.time.time", lambda: clock[0])
    first, second = (
        SqliteRepository(tmp_path / "state.sqlite3"),
        SqliteRepository(tmp_path / "state.sqlite3"),
    )
    assert first.acquire_supervisor("first")
    assert not second.acquire_supervisor("second")
    clock[0] += 31
    assert second.acquire_supervisor("second")
    assert not first.acquire_supervisor("first")
    first.release_supervisor("first")
    assert second.acquire_supervisor("second")


def test_historical_v1_manifest_remains_readable_and_resumable(
    tmp_path: Path, pack_root: Path
) -> None:
    engine = BenchmarkEngine(tmp_path / "state", pack_root)
    current = engine.create_run("synthetic_text_intent")
    pack = get_pack("synthetic_text_intent", pack_root)
    legacy_hashes = engine._descriptor_hashes(
        pack,
        current.samples,
        dict(current.solution_descriptors),
        current.evaluator,
        legacy=True,
    )
    payload = current.model_dump(mode="json")
    payload.update(schema_version="1", run_id="f" * 64, descriptor_hashes=legacy_hashes)
    payload.pop("sample_snapshot_artifact_id")
    payload.pop("retry_policy")
    for descriptor in payload["solution_descriptors"].values():
        descriptor.pop("settings")
        descriptor.pop("throttle_scope")
    manifest = RunManifest.model_validate_json(json.dumps(payload))
    artifact = engine.artifacts.put_json(payload)
    engine.repository.register_artifact(artifact)
    engine.repository.create_run(manifest, artifact.id)
    engine.repository.reconcile_tasks(manifest)
    report = engine.run_to_completion(manifest.run_id)
    assert report.status.value == "completed"
    assert engine.artifacts.get_bytes(artifact.id)
    assert sha256_digest(payload) == artifact.id


def test_worker_waits_for_due_work_without_busy_polling(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pack_root: Path,
) -> None:
    engine = BenchmarkEngine(tmp_path / "state", pack_root)
    supervisor = RunSupervisor(engine)
    calls: list[float] = []
    original = supervisor.step

    def counted() -> bool:
        calls.append(time.monotonic())
        return original()

    monkeypatch.setattr(supervisor, "step", counted)
    supervisor.start()
    try:
        time.sleep(0.25)
        assert supervisor.running
    finally:
        supervisor.stop()
    assert len(calls) <= 1
