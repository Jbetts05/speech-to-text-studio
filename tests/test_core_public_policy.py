from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_bench.artifacts import ArtifactNotFoundError
from ai_bench.backends import local_backend
from ai_bench.dataset_imports import DatasetImportFile
from ai_bench.engine import BenchmarkEngine
from ai_bench.hashing import content_digest, sha256_digest
from ai_bench.lifecycle import RootUnavailableError
from ai_bench.repositories import AdmissionCapacityError
from ai_bench.runtime_policy import PublicExecutionPolicy
from tests.test_core_uploads import SyntheticImporter


@pytest.mark.parametrize("durable", [False, True])
def test_core_policy_reserves_capacity_with_key_and_replays_once(
    tmp_path: Path, pack_root: Path, durable: bool
) -> None:
    policy = PublicExecutionPolicy(max_active_runs=1, max_outstanding_tasks=10, max_attempts=2)
    engine = BenchmarkEngine(
        tmp_path,
        pack_root,
        backend=local_backend(tmp_path, durable_projections=durable, public_policy=policy),
    )
    first = engine.create_run(
        "synthetic_text_intent",
        solution_ids=("keyword-standalone",),
        retry_limit=2,
        idempotency_key="first",
    )
    assert (
        engine.create_run(
            "synthetic_text_intent",
            solution_ids=("keyword-standalone",),
            retry_limit=2,
            idempotency_key="first",
        )
        == first
    )
    with pytest.raises(AdmissionCapacityError):
        engine.create_run(
            "synthetic_text_intent",
            solution_ids=("keyword-standalone",),
            retry_limit=2,
            idempotency_key="rejected",
        )
    assert (
        engine.repository.get_launch_reservation(sha256_digest({"idempotency_key": "rejected"}))
        is None
    )
    assert engine.recover_initializations() == 0
    assert len(engine.repository.list_runs()) == 1
    with pytest.raises(ValueError, match="total-attempt"):
        engine.create_run("synthetic_text_intent", retry_limit=3)


def test_selected_dataset_alias_and_raw_artifact_expiry(tmp_path: Path, pack_root: Path) -> None:
    engine = BenchmarkEngine(tmp_path / "state", pack_root)
    content = b"yes"
    source = tmp_path / "input"
    source.write_bytes(content)
    result = SyntheticImporter().import_files(
        (DatasetImportFile("input.txt", source, len(content), content_digest(content)),)
    )
    dataset = engine.datasets.publish("synthetic_text_intent", "operation", result)
    manifest = engine.create_run(
        "synthetic_text_intent",
        solution_ids=("keyword-standalone",),
        selected_dataset_id=dataset.id,
        idempotency_key="selected",
    )
    assert (
        engine.create_run(
            "synthetic_text_intent",
            solution_ids=("keyword-standalone",),
            dataset_id=dataset.id,
            idempotency_key="selected",
        )
        == manifest
    )
    with pytest.raises(ValueError, match="same dataset"):
        engine.create_run(
            "synthetic_text_intent", dataset_id=dataset.id, selected_dataset_id="different"
        )
    assert not engine.is_expired(dataset.id)
    assert engine.get_artifact_bytes(content_digest(content), "text/plain") == content
    assert dataset.expires_at == dataset.published_at + timedelta(days=10)
    expires_at = dataset.expires_at
    assert expires_at is not None
    engine.lifecycle.clock = lambda: expires_at
    assert engine.is_expired(dataset.id)
    with pytest.raises(ArtifactNotFoundError, match="live published owner"):
        engine.get_artifact_bytes(content_digest(content))
    with pytest.raises(RootUnavailableError):
        engine.is_expired("unknown")


def test_public_retention_policy_cannot_be_extended() -> None:
    with pytest.raises(ValidationError):
        PublicExecutionPolicy.model_validate({"retention_days": 30})
    with pytest.raises(ValidationError):
        PublicExecutionPolicy(max_receivers=3)


@pytest.mark.parametrize("durable", [False, True])
def test_abandoned_capacity_reservation_is_reaped(tmp_path: Path, durable: bool) -> None:
    policy = PublicExecutionPolicy(max_active_runs=1, max_outstanding_tasks=1)
    backend = local_backend(tmp_path, durable_projections=durable, public_policy=policy)
    engine = BenchmarkEngine(tmp_path, backend=backend)
    run_id = sha256_digest("abandoned run")
    root = engine.lifecycle.create(run_id, "run")
    key = sha256_digest("abandoned key")
    engine.repository.reserve_launch(key, sha256_digest("request"), run_id, expected_tasks=1)
    with pytest.raises(AdmissionCapacityError):
        engine.repository.reserve_launch(
            sha256_digest("second key"),
            sha256_digest("second request"),
            sha256_digest("second run"),
            expected_tasks=1,
        )
    deadline = root.expires_at
    assert deadline is not None
    engine.lifecycle.clock = lambda: deadline
    engine.reap_expired()
    assert engine.repository.get_launch_reservation(key) is None
    accepted = engine.repository.reserve_launch(
        sha256_digest("third key"),
        sha256_digest("third request"),
        sha256_digest("third run"),
        expected_tasks=1,
    )
    assert accepted == sha256_digest("third run")
