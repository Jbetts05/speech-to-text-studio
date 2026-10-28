from __future__ import annotations

import threading
import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from ai_bench.azure_storage import (
    AzureStorageConfig,
    AzureStorageConfigurationError,
    AzureStorageDependencyError,
    AzureStorageRepositories,
)
from ai_bench.contracts import (
    Artifact,
    ArtifactEvidence,
    BenchmarkSample,
    Compatibility,
    CompatibilityLevel,
    ExecutionOutput,
    ExecutionRequest,
    ImageArtifact,
    Modality,
    ParsedOutput,
    RunStatus,
)
from ai_bench.engine import (
    BenchmarkEngine,
    NetworkExecutionDisabledError,
)
from ai_bench.hashing import content_digest
from ai_bench.packs import get_pack
from ai_bench.repositories import (
    AzureRepositories,
    FinalizerFenceError,
    LeaseLostError,
    RepositoryError,
)


def test_retry_then_poison_is_durable(tmp_path: Path, pack_root: Path) -> None:
    engine = BenchmarkEngine(tmp_path / "state", pack_root)
    manifest = engine.create_run(
        "synthetic_text_intent",
        solution_ids=("keyword-standalone",),
        retry_limit=2,
    )
    lease_one = engine.repository.claim_task("worker")
    assert lease_one is not None
    assert engine.repository.fail_attempt(lease_one, RuntimeError("transient")) is False
    lease_two = engine.repository.claim_task("worker")
    assert lease_two is not None
    assert lease_two.task.id == lease_one.task.id
    assert lease_two.attempt_number == 2
    assert engine.repository.fail_attempt(lease_two, RuntimeError("permanent")) is True
    task = engine.repository.get_task(lease_one.task.id)
    assert task is not None
    assert task.status.value == "poisoned"
    assert task.result is not None
    assert task.result.error == "RuntimeError: permanent"
    assert any(
        event.event_type == "task.poisoned"
        for event in engine.repository.list_events(manifest.run_id)
    )


def test_expired_lease_recovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pack_root: Path
) -> None:
    clock = [1000.0]
    monkeypatch.setattr("ai_bench.repositories.time.time", lambda: clock[0])
    engine = BenchmarkEngine(tmp_path / "state", pack_root)
    engine.create_run(
        "synthetic_text_intent",
        solution_ids=("keyword-standalone",),
        retry_limit=2,
    )
    lease = engine.repository.claim_task("lost-worker", lease_seconds=1)
    assert lease is not None
    clock[0] += 2
    assert engine.repository.recover_expired_leases() == (1, 0)
    recovered = engine.repository.claim_task("new-worker")
    assert recovered is not None
    assert recovered.task.id == lease.task.id
    assert recovered.token > lease.token
    with pytest.raises(LeaseLostError):
        engine.repository.fail_attempt(lease, RuntimeError("stale worker"))


def test_finalizer_is_fenced(tmp_path: Path, pack_root: Path) -> None:
    engine = BenchmarkEngine(tmp_path / "state", pack_root)
    manifest = engine.create_run(
        "synthetic_text_intent",
        solution_ids=("keyword-standalone",),
        retry_limit=1,
    )
    while lease := engine.repository.claim_task("poisoner"):
        engine.repository.fail_attempt(lease, RuntimeError("poison"))
    old_token = engine.repository.acquire_finalizer(manifest.run_id)
    new_token = engine.repository.acquire_finalizer(manifest.run_id)
    assert old_token is not None and new_token is not None and new_token > old_token
    with pytest.raises(FinalizerFenceError):
        engine.repository.finalize_run(
            manifest.run_id,
            old_token,
            status=RunStatus.FAILED,
            report_artifact_id="f" * 64,
        )


def test_azure_boundary_fails_clearly_without_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AI_BENCH_AZURE_STORAGE_ACCOUNT_URL", raising=False)
    monkeypatch.delenv("AI_BENCH_AZURE_STORAGE_CONNECTION_STRING", raising=False)
    with pytest.raises(AzureStorageConfigurationError, match="Azure Storage is not configured"):
        AzureRepositories()


def test_azure_boundary_fails_clearly_without_optional_packages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable() -> tuple[object, object, object, object]:
        raise AzureStorageDependencyError("install the azure extra")

    monkeypatch.setattr("ai_bench.azure_storage._azure_modules", unavailable)
    config = AzureStorageConfig(
        account_url="https://example.blob.core.windows.net",
    )
    with pytest.raises(AzureStorageDependencyError, match="azure extra"):
        AzureStorageRepositories(config)


def test_hosted_azure_uses_managed_identity_and_local_uses_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential_calls: list[tuple[str, str | None]] = []

    class FakeIdentity:
        @staticmethod
        def ManagedIdentityCredential(*, client_id: str | None = None) -> tuple[str, str | None]:
            credential_calls.append(("managed", client_id))
            return ("managed", client_id)

        @staticmethod
        def DefaultAzureCredential() -> tuple[str, None]:
            credential_calls.append(("default", None))
            return ("default", None)

    class FakeService:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def get_container_client(self, name: str) -> object:
            return object()

        def get_queue_client(self, name: str) -> object:
            return object()

        def get_table_client(self, name: str) -> object:
            return object()

    service_module = SimpleNamespace(
        BlobServiceClient=FakeService,
        QueueServiceClient=FakeService,
        TableServiceClient=FakeService,
    )
    monkeypatch.setattr(
        "ai_bench.azure_storage._azure_modules",
        lambda: (
            service_module,
            service_module,
            service_module,
            FakeIdentity,
        ),
    )
    monkeypatch.setattr("ai_bench.azure_storage._resource_exists_exception", lambda: RuntimeError)
    config = AzureStorageConfig(
        account_url="https://example.blob.core.windows.net",
    )

    monkeypatch.setenv("AI_BENCH_RUNTIME", "azure")
    monkeypatch.setenv("AI_BENCH_AZURE_MANAGED_IDENTITY_CLIENT_ID", "client-id")
    AzureStorageRepositories(config)
    assert credential_calls == [("managed", "client-id")]

    credential_calls.clear()
    monkeypatch.setenv("AI_BENCH_RUNTIME", "local")
    AzureStorageRepositories(config)
    assert credential_calls == [("default", None)]


def test_hosted_azure_rejects_connection_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_BENCH_RUNTIME", "azure")
    config = AzureStorageConfig(connection_string="UseDevelopmentStorage=true")
    with pytest.raises(AzureStorageConfigurationError, match="forbidden"):
        AzureStorageRepositories(config)


def test_parse_retry_retains_billed_usage_and_raw_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pack_root: Path
) -> None:
    original_pack = get_pack("synthetic_text_intent", pack_root)
    delegate = original_pack.solutions[0]

    class ParseOnceSolution:
        id = delegate.id
        version = delegate.version
        networked = delegate.networked
        pipeline = delegate.pipeline
        compatibility = delegate.compatibility
        pricing = delegate.pricing

        def __init__(self) -> None:
            self.parse_calls = 0

        def execute(self, request: ExecutionRequest) -> ExecutionOutput:
            return delegate.execute(request).model_copy(
                update={"provider_request_id": f"request-{request.task_id}"}
            )

        def parse(self, output: ExecutionOutput) -> ParsedOutput:
            self.parse_calls += 1
            if self.parse_calls == 1:
                raise ValueError("synthetic parse failure")
            return delegate.parse(output)

    flaky = ParseOnceSolution()
    pack = replace(cast(Any, original_pack), solutions=(flaky,))
    monkeypatch.setattr("ai_bench.engine.get_pack", lambda *_: pack)
    engine = BenchmarkEngine(tmp_path / "state", pack_root)
    manifest = engine.create_run(
        "synthetic_text_intent",
        solution_ids=(flaky.id,),
        retry_limit=2,
    )
    report = engine.run_to_completion(manifest.run_id)
    retried = next(result for result in report.results if result.attempts == 2)
    attempts = engine.repository.list_attempts(retried.task_id)
    assert len(attempts) == 2
    assert attempts[0].telemetry is not None
    assert attempts[0].telemetry.usage.dimensions["calls"] == 1
    assert attempts[0].telemetry.provider_request_id == f"request-{retried.task_id}"
    assert [evidence.role for evidence in attempts[0].evidence] == ["raw"]
    assert engine.artifacts.get_bytes(attempts[0].evidence[0].artifact_id)
    report_attempts = report.attempt_history[retried.task_id]
    assert len(report_attempts) == 2
    assert [attempt.attempt_number for attempt in report_attempts] == [1, 2]
    assert report.usage[flaky.id]["calls"] == 7
    assert report.cost[flaky.id] == 0
    failed_raw_link = f"{retried.task_id}:attempt:1:raw"
    assert failed_raw_link in report.evidence_links


def test_attempt_evidence_preserves_descriptor_for_identical_bytes(
    tmp_path: Path,
    pack_root: Path,
) -> None:
    engine = BenchmarkEngine(tmp_path / "state", pack_root)
    engine.create_run(
        "synthetic_text_intent",
        solution_ids=("keyword-standalone",),
        retry_limit=1,
    )
    lease = engine.repository.claim_task("worker")
    assert lease is not None
    artifact_id = content_digest(b'{"intent":"support"}')
    text_descriptor = Artifact(
        id=artifact_id,
        modality=Modality.TEXT,
        media_type="text/plain",
        size_bytes=20,
        uri=f"artifact://sha256/{artifact_id}",
    )
    structured_descriptor = text_descriptor.model_copy(
        update={
            "modality": Modality.STRUCTURED,
            "media_type": "application/json",
        }
    )
    engine.repository.register_artifact(text_descriptor)
    engine.repository.register_artifact(structured_descriptor)
    engine.repository.record_attempt_artifact(
        lease,
        ArtifactEvidence(
            role="raw",
            artifact_id=artifact_id,
            modality=Modality.TEXT,
            media_type="text/plain",
            size_bytes=20,
            attempt_number=lease.attempt_number,
        ),
    )
    engine.repository.record_attempt_artifact(
        lease,
        ArtifactEvidence(
            role="parsed",
            artifact_id=artifact_id,
            modality=Modality.STRUCTURED,
            media_type="application/json",
            size_bytes=20,
            attempt_number=lease.attempt_number,
        ),
    )

    evidence = engine.repository.list_attempts(lease.task.id)[0].evidence
    assert [(item.role, item.modality, item.media_type) for item in evidence] == [
        ("raw", Modality.TEXT, "text/plain"),
        ("parsed", Modality.STRUCTURED, "application/json"),
    ]
    assert engine.repository.get_artifact(artifact_id, "text/plain") == text_descriptor
    assert engine.repository.get_artifact(artifact_id, "application/json") == structured_descriptor


def test_execute_failure_contributes_attempt_duration_to_latency(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pack_root: Path
) -> None:
    original_pack = get_pack("synthetic_text_intent", pack_root)
    delegate = original_pack.solutions[0]

    class ExecuteOnceSolution:
        id = delegate.id
        version = delegate.version
        networked = delegate.networked
        pipeline = delegate.pipeline
        compatibility = delegate.compatibility
        pricing = delegate.pricing

        def __init__(self) -> None:
            self.execute_calls = 0

        def execute(self, request: ExecutionRequest) -> ExecutionOutput:
            self.execute_calls += 1
            if self.execute_calls == 1:
                time.sleep(0.02)
                raise TimeoutError("synthetic provider timeout")
            return delegate.execute(request)

        def parse(self, output: ExecutionOutput) -> ParsedOutput:
            return delegate.parse(output)

    flaky = ExecuteOnceSolution()
    pack = replace(cast(Any, original_pack), solutions=(flaky,))
    monkeypatch.setattr("ai_bench.engine.get_pack", lambda *_: pack)
    engine = BenchmarkEngine(tmp_path / "state", pack_root)
    manifest = engine.create_run(
        "synthetic_text_intent",
        solution_ids=(flaky.id,),
        retry_limit=2,
    )

    report = engine.run_to_completion(manifest.run_id)
    retried = next(result for result in report.results if result.attempts == 2)
    failed_attempt = report.attempt_history[retried.task_id][0]
    assert failed_attempt.telemetry is None
    assert failed_attempt.finished_at is not None
    assert failed_attempt.duration_ms is not None
    assert failed_attempt.duration_ms >= Decimal("10")

    recorded_durations = [
        attempt.duration_ms for attempts in report.attempt_history.values() for attempt in attempts
    ]
    assert all(duration is not None for duration in recorded_durations)
    assert report.latency[flaky.id] == sum(
        (duration for duration in recorded_durations if duration is not None),
        Decimal("0"),
    )


def test_heartbeat_prevents_long_execution_lease_recovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pack_root: Path
) -> None:
    original_pack = get_pack("synthetic_text_intent", pack_root)
    delegate = original_pack.solutions[0]

    class SlowSolution:
        id = delegate.id
        version = delegate.version
        networked = delegate.networked
        pipeline = delegate.pipeline
        compatibility = delegate.compatibility
        pricing = delegate.pricing

        def __init__(self) -> None:
            self.execute_calls = 0

        def execute(self, request: ExecutionRequest) -> ExecutionOutput:
            self.execute_calls += 1
            if self.execute_calls == 1:
                time.sleep(1.2)
            return delegate.execute(request)

        def parse(self, output: ExecutionOutput) -> ParsedOutput:
            return delegate.parse(output)

    pack = replace(cast(Any, original_pack), solutions=(SlowSolution(),))
    monkeypatch.setattr("ai_bench.engine.get_pack", lambda *_: pack)
    engine = BenchmarkEngine(tmp_path / "state", pack_root, lease_seconds=1)
    manifest = engine.create_run(
        "synthetic_text_intent",
        solution_ids=(delegate.id,),
        retry_limit=2,
    )
    recovery_results: list[tuple[int, int]] = []

    def attempt_recovery() -> None:
        time.sleep(1.05)
        recovery_results.append(engine.repository.recover_expired_leases())

    recovery = threading.Thread(target=attempt_recovery)
    recovery.start()
    report = engine.run_to_completion(manifest.run_id)
    recovery.join()
    assert recovery_results == [(0, 0)]
    assert report.status.value == "completed"
    assert all(result.attempts == 1 for result in report.results)


def test_resume_fails_closed_when_pinned_solution_descriptor_drifts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pack_root: Path
) -> None:
    original_pack = get_pack("synthetic_text_intent", pack_root)
    delegate = original_pack.solutions[0]
    engine = BenchmarkEngine(tmp_path / "state", pack_root)
    manifest = engine.create_run(
        "synthetic_text_intent",
        solution_ids=(delegate.id,),
    )

    class ChangedSolution:
        id = delegate.id
        version = "2.0.0"
        networked = delegate.networked
        pipeline = delegate.pipeline
        compatibility = delegate.compatibility
        pricing = delegate.pricing

        def execute(self, request: ExecutionRequest) -> ExecutionOutput:
            return delegate.execute(request)

        def parse(self, output: ExecutionOutput) -> ParsedOutput:
            return delegate.parse(output)

    changed = replace(cast(Any, original_pack), solutions=(ChangedSolution(),))
    monkeypatch.setattr("ai_bench.engine.get_pack", lambda *_: changed)

    with pytest.raises(RepositoryError, match="descriptor drift"):
        engine.run_to_completion(manifest.run_id)
    assert all(
        task.status.value == "pending" for task in engine.repository.list_tasks(manifest.run_id)
    )


def test_idempotent_replay_repairs_interrupted_sample_publication(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pack_root: Path
) -> None:
    original_pack = get_pack("synthetic_text_intent", pack_root)
    delegate = original_pack.dataset

    class FlakyDataset:
        id = delegate.id
        revision = delegate.revision

        def __init__(self) -> None:
            self.calls = 0
            self.failed = False

        def samples(self) -> Any:
            return delegate.samples()

        def read_artifact(self, artifact: Any) -> bytes:
            self.calls += 1
            if self.calls == 2 and not self.failed:
                self.failed = True
                raise OSError("interrupted publication")
            return delegate.read_artifact(artifact)

    dataset = FlakyDataset()
    pack = replace(cast(Any, original_pack), dataset=dataset)
    monkeypatch.setattr("ai_bench.engine.get_pack", lambda *_: pack)
    engine = BenchmarkEngine(tmp_path / "state", pack_root)

    with pytest.raises(OSError, match="interrupted publication"):
        engine.create_run(
            "synthetic_text_intent",
            solution_ids=("keyword-standalone",),
            idempotency_key="stable-launch",
        )
    assert engine.repository.list_runs() == ()

    manifest = engine.create_run(
        "synthetic_text_intent",
        solution_ids=("keyword-standalone",),
        idempotency_key="stable-launch",
    )
    for sample in delegate.samples():
        assert engine.artifacts.get_bytes(sample.input.id)
        assert sample.reference is not None
        assert engine.artifacts.get_bytes(sample.reference.id)
    assert len(engine.repository.list_tasks(manifest.run_id)) == 6


def test_runtime_network_disable_blocks_provider_before_invocation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pack_root: Path
) -> None:
    original_pack = get_pack("synthetic_text_intent", pack_root)
    delegate = original_pack.solutions[0]

    class NetworkedSolution:
        id = delegate.id
        version = delegate.version
        networked = True
        pipeline = delegate.pipeline
        compatibility = delegate.compatibility
        pricing = delegate.pricing

        def __init__(self) -> None:
            self.execute_calls = 0

        def execute(self, request: ExecutionRequest) -> ExecutionOutput:
            self.execute_calls += 1
            return delegate.execute(request)

        def parse(self, output: ExecutionOutput) -> ParsedOutput:
            return delegate.parse(output)

    networked = NetworkedSolution()
    pack = replace(cast(Any, original_pack), solutions=(networked,))
    monkeypatch.setattr("ai_bench.engine.get_pack", lambda *_: pack)
    monkeypatch.setenv("AI_BENCH_ALLOW_NETWORKED_SOLUTIONS", "true")
    engine = BenchmarkEngine(tmp_path / "state", pack_root)
    manifest = engine.create_run(
        "synthetic_text_intent",
        solution_ids=(networked.id,),
        allow_networked=True,
    )
    monkeypatch.setenv("AI_BENCH_ALLOW_NETWORKED_SOLUTIONS", "false")

    with pytest.raises(NetworkExecutionDisabledError, match="currently disabled"):
        engine.run_to_completion(manifest.run_id)
    assert networked.execute_calls == 0
    assert all(
        task.status.value == "pending" for task in engine.repository.list_tasks(manifest.run_id)
    )


def test_run_worker_only_claims_tasks_for_requested_run(tmp_path: Path, pack_root: Path) -> None:
    engine = BenchmarkEngine(tmp_path / "state", pack_root)
    first = engine.create_run(
        "synthetic_text_intent",
        solution_ids=("keyword-standalone",),
    )
    second = engine.create_run(
        "synthetic_text_intent",
        solution_ids=("keyword-standalone",),
    )

    second_report = engine.run_to_completion(second.run_id)

    assert second_report.status.value == "completed"
    assert all(
        task.status.value == "pending" for task in engine.repository.list_tasks(first.run_id)
    )


def test_dataset_reader_publishes_non_text_artifact(tmp_path: Path, pack_root: Path) -> None:
    content = b"\x89PNG\r\n\x1a\nsynthetic"
    artifact = ImageArtifact(
        id=content_digest(content),
        modality=Modality.IMAGE,
        media_type="image/png",
        size_bytes=len(content),
        uri="pack://example/image",
        metadata={"synthetic": True},
    )
    sample = BenchmarkSample(id="image-001", input=artifact)

    class BinaryDataset:
        id = "binary"
        revision = get_pack("synthetic_text_intent", pack_root).dataset.revision

        def samples(self) -> tuple[BenchmarkSample, ...]:
            return (sample,)

        def read_artifact(self, requested: Any) -> bytes:
            assert requested.id == artifact.id
            return content

    engine = BenchmarkEngine(tmp_path / "state", pack_root)
    engine._register_sample_artifacts(BinaryDataset(), (sample,))

    assert engine.artifacts.get_bytes(artifact.id) == content


def test_unsupported_solution_cannot_execute(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pack_root: Path
) -> None:
    original_pack = get_pack("synthetic_text_intent", pack_root)
    delegate = original_pack.solutions[0]

    class UnsupportedSolution:
        id = delegate.id
        version = delegate.version
        networked = delegate.networked
        pipeline = delegate.pipeline
        compatibility = Compatibility(
            level=CompatibilityLevel.UNSUPPORTED,
            comparable_quality=False,
            reason="The required modality is unsupported.",
        )
        pricing = delegate.pricing
        execute = delegate.execute
        parse = delegate.parse

    pack = replace(cast(Any, original_pack), solutions=(UnsupportedSolution(),))
    monkeypatch.setattr("ai_bench.engine.get_pack", lambda *_: pack)
    engine = BenchmarkEngine(tmp_path / "state", pack_root)

    with pytest.raises(ValueError, match="unsupported solutions"):
        engine.create_run(
            "synthetic_text_intent",
            solution_ids=(delegate.id,),
        )


def test_noncomparable_solution_keeps_quality_aggregate_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pack_root: Path
) -> None:
    original_pack = get_pack("synthetic_text_intent", pack_root)
    delegate = original_pack.solutions[0]

    class PartialSolution:
        id = delegate.id
        version = delegate.version
        networked = delegate.networked
        pipeline = delegate.pipeline
        compatibility = Compatibility(
            level=CompatibilityLevel.PARTIAL_REFERENCE,
            comparable_quality=False,
            reason="Only a subset of reference semantics is available.",
        )
        pricing = delegate.pricing
        execute = delegate.execute
        parse = delegate.parse

    partial = PartialSolution()
    pack = replace(cast(Any, original_pack), solutions=(partial,))
    monkeypatch.setattr("ai_bench.engine.get_pack", lambda *_: pack)
    engine = BenchmarkEngine(tmp_path / "state", pack_root)
    manifest = engine.create_run(
        "synthetic_text_intent",
        solution_ids=(partial.id,),
    )

    report = engine.run_to_completion(manifest.run_id)

    assert all(value is None for value in report.quality[partial.id].values())
    assert report.quality_comparability[partial.id].comparable_quality is False
    assert report.cost[partial.id] == 0
