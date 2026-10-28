from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from fastapi.testclient import TestClient

from ai_bench.api import create_app
from ai_bench.contracts import (
    BenchmarkSample,
    Compatibility,
    CompatibilityLevel,
    DatasetAvailability,
    DatasetRevision,
    JsonValue,
    ModalityArtifact,
    RunManifest,
)
from ai_bench.engine import BenchmarkEngine, NetworkExecutionDisabledError
from ai_bench.hashing import sha256_digest
from ai_bench.packs import DatasetUnavailableError, PackNotFoundError, get_pack
from ai_bench.repositories import IdempotencyConflictError
from tests.test_async_runtime import scoped_pack


def assert_unreserved(engine: BenchmarkEngine, key: str) -> None:
    key_hash = sha256_digest({"idempotency_key": key})
    assert engine.repository.get_launch_reservation(key_hash) is None
    assert engine.repository.list_runs() == ()


@pytest.mark.parametrize(
    "invalid",
    [
        {"solution_ids": ("unknown-solution",)},
        {"solution_ids": ()},
        {"solution_ids": ("keyword-standalone", "keyword-standalone")},
        {"solution_ids": ("keyword-standalone",) * 21},
        {"retry_limit": 0},
        {"retry_limit": 21},
        {"retry_limit": True},
    ],
)
def test_invalid_request_does_not_consume_key(
    invalid: dict[str, Any],
    tmp_path: Path,
    pack_root: Path,
) -> None:
    engine = BenchmarkEngine(tmp_path / "state", pack_root)
    key = "correctable-request"
    with pytest.raises(ValueError):
        engine.create_run("synthetic_text_intent", idempotency_key=key, **invalid)
    assert_unreserved(engine, key)
    manifest = engine.create_run(
        "synthetic_text_intent",
        solution_ids=("keyword-standalone",),
        idempotency_key=key,
    )
    assert len(engine.repository.list_tasks(manifest.run_id)) == 6
    assert all(task.attempts == 0 for task in engine.repository.list_tasks(manifest.run_id))


def test_missing_pack_does_not_consume_key(tmp_path: Path, pack_root: Path) -> None:
    engine = BenchmarkEngine(tmp_path / "state", pack_root)
    key = "correctable-pack"
    with pytest.raises(PackNotFoundError):
        engine.create_run("missing_pack", idempotency_key=key)
    assert_unreserved(engine, key)
    assert engine.create_run("synthetic_text_intent", idempotency_key=key).run_id


@pytest.mark.parametrize("invalid", ["unsupported", "configuration"])
def test_invalid_solution_contract_does_not_consume_key(
    invalid: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pack_root: Path,
) -> None:
    _, solutions = scoped_pack(pack_root, monkeypatch)
    engine = BenchmarkEngine(tmp_path / "state", pack_root)
    key = "correctable-solution"

    def invalid_configuration() -> None:
        raise ValueError("Synthetic configuration is invalid.")

    with monkeypatch.context() as context:
        if invalid == "unsupported":
            context.setattr(
                solutions[0],
                "compatibility",
                Compatibility(
                    level=CompatibilityLevel.UNSUPPORTED,
                    comparable_quality=False,
                    reason="Unsupported synthetic configuration.",
                ),
            )
        else:
            context.setattr(solutions[0], "validate_execution", invalid_configuration)
        with pytest.raises(ValueError):
            engine.create_run(
                "synthetic_text_intent",
                solution_ids=(solutions[0].id,),
                idempotency_key=key,
            )
    assert_unreserved(engine, key)
    assert engine.create_run(
        "synthetic_text_intent",
        solution_ids=(solutions[0].id,),
        idempotency_key=key,
    ).run_id


@pytest.mark.parametrize(
    ("allow_request", "allow_runtime"),
    [(False, "true"), (True, "false")],
)
def test_missing_network_authorization_does_not_consume_key(
    allow_request: bool,
    allow_runtime: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pack_root: Path,
) -> None:
    _, solutions = scoped_pack(pack_root, monkeypatch)
    solutions[0].networked = True
    monkeypatch.setenv("AI_BENCH_ALLOW_NETWORKED_SOLUTIONS", allow_runtime)
    engine = BenchmarkEngine(tmp_path / "state", pack_root)
    key = "correctable-authorization"
    with pytest.raises(NetworkExecutionDisabledError):
        engine.create_run(
            "synthetic_text_intent",
            solution_ids=(solutions[0].id,),
            allow_networked=allow_request,
            idempotency_key=key,
        )
    assert_unreserved(engine, key)
    monkeypatch.setenv("AI_BENCH_ALLOW_NETWORKED_SOLUTIONS", "true")
    manifest = engine.create_run(
        "synthetic_text_intent",
        solution_ids=(solutions[0].id,),
        allow_networked=True,
        idempotency_key=key,
    )
    assert all(task.attempts == 0 for task in engine.repository.list_tasks(manifest.run_id))


@pytest.mark.parametrize("unavailable", ["not-configured", "invalid"])
def test_unavailable_dataset_does_not_consume_key_and_existing_replay_is_independent(
    unavailable: Literal["not-configured", "invalid"],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pack_root: Path,
) -> None:
    original = get_pack("synthetic_text_intent", pack_root)
    delegate = original.dataset

    class SwitchableDataset:
        id = delegate.id
        available = False

        def availability(self) -> DatasetAvailability:
            return DatasetAvailability(
                status="ready" if self.available else unavailable,
                reason="Synthetic publication state.",
                revision=delegate.revision if self.available else None,
                sample_count=6,
                scored_sample_count=6,
            )

        @property
        def revision(self) -> DatasetRevision:
            if not self.available:
                raise DatasetUnavailableError(self.availability())
            return delegate.revision

        def samples(self) -> tuple[BenchmarkSample, ...]:
            if not self.available:
                raise DatasetUnavailableError(self.availability())
            return tuple(delegate.samples())

        def read_artifact(self, artifact: ModalityArtifact) -> bytes:
            return delegate.read_artifact(artifact)

        def snapshot_metadata(self) -> dict[str, JsonValue]:
            return {}

    dataset = SwitchableDataset()
    pack = replace(cast(Any, original), dataset=dataset)
    monkeypatch.setattr("ai_bench.engine.get_pack", lambda *_: pack)
    engine = BenchmarkEngine(tmp_path / "state", pack_root)
    key = "correctable-data"
    with pytest.raises(DatasetUnavailableError):
        engine.create_run("synthetic_text_intent", idempotency_key=key)
    assert_unreserved(engine, key)
    dataset.available = True
    manifest = engine.create_run("synthetic_text_intent", idempotency_key=key)
    dataset.available = False
    assert engine.create_run("synthetic_text_intent", idempotency_key=key) == manifest
    assert all(task.attempts == 0 for task in engine.repository.list_tasks(manifest.run_id))


def test_artifact_validation_precedes_new_reservation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pack_root: Path,
) -> None:
    pack = get_pack("synthetic_text_intent", pack_root)
    monkeypatch.setattr("ai_bench.engine.get_pack", lambda *_: pack)
    engine = BenchmarkEngine(tmp_path / "state", pack_root)
    key = "correctable-artifact"
    with monkeypatch.context() as context:
        context.setattr(pack.dataset, "read_artifact", lambda _: b"wrong synthetic bytes")
        with pytest.raises(ValueError, match="artifact size mismatch"):
            engine.create_run("synthetic_text_intent", idempotency_key=key)
    assert_unreserved(engine, key)
    assert engine.create_run("synthetic_text_intent", idempotency_key=key).run_id


def test_fresh_concurrent_valid_requests_share_one_manifest(
    tmp_path: Path, pack_root: Path
) -> None:
    engine = BenchmarkEngine(tmp_path / "state", pack_root)

    def launch(_index: int) -> RunManifest:
        return engine.create_run("synthetic_text_intent", idempotency_key="new-concurrent")

    with ThreadPoolExecutor(max_workers=2) as executor:
        manifests = tuple(executor.map(launch, range(2)))
    assert manifests[0] == manifests[1]
    assert len(engine.repository.list_runs()) == 1
    assert len(engine.repository.list_tasks(manifests[0].run_id)) == 12


def test_fresh_concurrent_different_requests_still_conflict(
    tmp_path: Path, pack_root: Path
) -> None:
    engine = BenchmarkEngine(tmp_path / "state", pack_root)

    def launch(limit: int) -> RunManifest | None:
        try:
            return engine.create_run(
                "synthetic_text_intent",
                retry_limit=limit,
                idempotency_key="new-conflict",
            )
        except IdempotencyConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(launch, (1, 2)))
    assert sum(result is not None for result in results) == 1
    assert len(engine.repository.list_runs()) == 1


def test_api_corrected_solution_can_reuse_rejected_request_key(
    tmp_path: Path,
    pack_root: Path,
) -> None:
    app = create_app(tmp_path / "state", pack_root)
    client = TestClient(app)
    headers = {"Idempotency-Key": "correctable-api-request"}
    rejected = client.post(
        "/api/runs",
        headers=headers,
        json={
            "benchmark_id": "synthetic_text_intent",
            "solution_ids": ["unknown-solution"],
        },
    )
    assert rejected.status_code == 422
    assert_unreserved(app.state.engine, headers["Idempotency-Key"])
    accepted = client.post(
        "/api/runs",
        headers=headers,
        json={
            "benchmark_id": "synthetic_text_intent",
            "solution_ids": ["keyword-standalone"],
        },
    )
    assert accepted.status_code == 202
    assert all(
        task.attempts == 0
        for task in app.state.engine.repository.list_tasks(accepted.json()["run_id"])
    )
