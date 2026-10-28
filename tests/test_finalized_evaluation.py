from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_bench.api import create_app
from ai_bench.api_contracts import FinalizedEvaluationResponse
from ai_bench.artifacts import ArtifactIntegrityError, ArtifactReadCapacityError
from ai_bench.contracts import (
    ArtifactEvidence,
    AttemptStatus,
    RunManifest,
    RunReport,
    RunStatus,
    TaskStatus,
)
from ai_bench.costs import build_cost_summary, build_finalized_cost_summary
from ai_bench.engine import BenchmarkEngine
from ai_bench.finalized import FinalizedIntegrityError, validate_finalized_report
from ai_bench.lifecycle import root_key
from ai_bench.repositories import RunRecord
from ai_bench.storage_state import StateChange


@pytest.fixture
def completed(
    tmp_path: Path, pack_root: Path
) -> tuple[FastAPI, BenchmarkEngine, RunRecord, RunReport]:
    app = create_app(tmp_path / "state", pack_root)
    engine: BenchmarkEngine = app.state.engine
    manifest = engine.create_run("synthetic_text_intent")
    lease = engine.repository.claim_task("synthetic-failure", run_id=manifest.run_id)
    assert lease is not None
    engine.repository.fail_attempt(lease, ValueError("Synthetic unobserved attempt"))
    report = engine.run_to_completion(manifest.run_id)
    record = engine.repository.get_run(manifest.run_id)
    assert record is not None and record.report_artifact_id is not None
    return app, engine, record, report


def replace_report(
    engine: BenchmarkEngine,
    record: RunRecord,
    report: RunReport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = engine.artifacts.put_json(report)
    engine.lifecycle.attach(record.manifest.run_id, artifact.id)
    changed = record.model_copy(update={"report_artifact_id": artifact.id})
    monkeypatch.setattr(engine.repository, "get_run", lambda _: changed)


def test_finalized_read_has_no_operational_reads_and_preserves_retry_costs_and_bytes(
    completed: tuple[FastAPI, BenchmarkEngine, RunRecord, RunReport],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, engine, record, report = completed
    assert record.report_artifact_id is not None
    run_id = record.manifest.run_id
    identifiers = {
        record.manifest_artifact_id,
        record.report_artifact_id,
        *(item.artifact_id for result in report.results for item in result.evidence),
        *(
            item.artifact_id
            for history in report.attempt_history.values()
            for attempt in history
            for item in attempt.evidence
        ),
    }
    before = {identifier: engine.artifacts.get_bytes(identifier) for identifier in identifiers}
    operational_costs = build_cost_summary(
        record.manifest,
        record.manifest_artifact_id,
        engine.repository.list_tasks(run_id),
        report.attempt_history,
    )
    historical_costs = build_finalized_cost_summary(record.manifest, report)
    assert operational_costs.model_dump(exclude={"assessed_at"}) == historical_costs.model_dump(
        exclude={"assessed_at"}
    )

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("finalized evaluation must not hydrate operational tasks, attempts or events")

    for method in ("list_tasks", "get_task", "list_attempts", "list_events"):
        monkeypatch.setattr(engine.repository, method, forbidden)
    client = TestClient(app)
    for _ in range(2):
        response = client.get(f"/api/runs/{run_id}/finalized-evaluation")
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        dto = FinalizedEvaluationResponse.model_validate_json(response.content, strict=False)
        assert dto.record_source == "finalized_report"
        assert dto.run.run_id == run_id
        assert dto.manifest == record.manifest
        assert dto.report == report
        assert len(dto.report.results) == 12
        assert dto.costs.recorded_total.total_attempts == 13
        assert dto.costs.recorded_total.covered_attempts == 12
        assert dto.costs.recorded_total.status == "partial"
        assert dto.costs.current_estimate_total.status == "unknown"
        assert set(dto.sample_artifacts) == set(record.manifest.sample_ids)
        assert "tasks" not in response.json()
        assert "finalizer_token" not in response.json()["run"]
    assert before == {
        identifier: engine.artifacts.get_bytes(identifier) for identifier in identifiers
    }
    assert client.get(f"/api/runs/{run_id}/report").content == before[record.report_artifact_id]


def test_nonterminal_is_explicit_live_fallback_without_reading_report(
    tmp_path: Path, pack_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(tmp_path / "state", pack_root)
    engine: BenchmarkEngine = app.state.engine
    manifest = engine.create_run("synthetic_text_intent")

    def forbidden(_: str) -> bytes:
        pytest.fail("nonterminal route must not load report or evidence")

    monkeypatch.setattr(engine.artifacts, "get_bytes", forbidden)
    response = TestClient(app).get(f"/api/runs/{manifest.run_id}/finalized-evaluation")
    assert response.status_code == 200
    assert response.json()["record_source"] == "live_detail_required"
    assert response.json()["reason"] == "run_not_finalized"


def test_terminal_failure_is_recorded_without_inventing_success_or_zero_usage(
    tmp_path: Path, pack_root: Path
) -> None:
    app = create_app(tmp_path / "state", pack_root)
    engine: BenchmarkEngine = app.state.engine
    manifest = engine.create_run("synthetic_text_intent", retry_limit=1)
    lease = engine.repository.claim_task("synthetic-failure", run_id=manifest.run_id)
    assert lease is not None
    engine.repository.fail_attempt(lease, ValueError("Synthetic terminal failure"))
    engine.run_to_completion(manifest.run_id)
    response = TestClient(app).get(f"/api/runs/{manifest.run_id}/finalized-evaluation")
    assert response.status_code == 200, response.text
    dto = FinalizedEvaluationResponse.model_validate_json(response.content, strict=False)
    assert dto.run.status is RunStatus.COMPLETED_WITH_FAILURES
    assert sum(result.status is TaskStatus.POISONED for result in dto.report.results) == 1
    assert dto.costs.recorded_total.status == "partial"
    assert dto.costs.recorded_total.total_attempts == 12
    assert dto.costs.recorded_total.covered_attempts == 11
    assert any(line.usage_uncertainty for line in dto.costs.solutions)


def test_unknown_and_invalid_run_identifiers_are_not_fallback(
    tmp_path: Path, pack_root: Path
) -> None:
    client = TestClient(create_app(tmp_path / "state", pack_root))
    for identifier in ("not-a-run", "a" * 64):
        response = client.get(f"/api/runs/{identifier}/finalized-evaluation")
        assert response.status_code == 404
        assert "record_source" not in response.json()


@pytest.mark.parametrize(
    "reason", ["report_missing", "incomplete_results", "incomplete_attempt_history"]
)
def test_legacy_or_incomplete_evidence_explicitly_requires_live_detail(
    completed: tuple[FastAPI, BenchmarkEngine, RunRecord, RunReport],
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
) -> None:
    app, engine, record, report = completed
    if reason == "report_missing":
        monkeypatch.setattr(
            engine.repository,
            "get_run",
            lambda _: record.model_copy(update={"report_artifact_id": None}),
        )
    else:
        changed = report.model_copy(
            update={"results": report.results[:-1]}
            if reason == "incomplete_results"
            else {"attempt_history": {}}
        )
        replace_report(engine, record, changed, monkeypatch)
    response = TestClient(app).get(f"/api/runs/{record.manifest.run_id}/finalized-evaluation")
    assert response.status_code == 200
    assert response.json()["reason"] == reason
    assert response.json()["record_source"] == "live_detail_required"
    assert "report" not in response.json() and "costs" not in response.json()


@pytest.mark.parametrize(
    "change",
    [
        lambda report: report.model_copy(update={"run_id": "f" * 64}),
        lambda report: report.model_copy(update={"manifest_artifact_id": "f" * 64}),
        lambda report: report.model_copy(update={"status": RunStatus.FAILED}),
        lambda report: report.model_copy(update={"results": (*report.results, report.results[0])}),
        lambda report: report.model_copy(
            update={
                "results": (
                    report.results[0].model_copy(update={"task_id": "foreign"}),
                    *report.results[1:],
                )
            }
        ),
        lambda report: report.model_copy(
            update={
                "results": (
                    report.results[0].model_copy(update={"sample_id": "foreign"}),
                    *report.results[1:],
                )
            }
        ),
        lambda report: report.model_copy(
            update={
                "results": (
                    report.results[0].model_copy(update={"status": TaskStatus.LEASED}),
                    *report.results[1:],
                )
            }
        ),
        lambda report: report.model_copy(
            update={"attempt_history": {**report.attempt_history, "foreign": ()}}
        ),
    ],
)
def test_integrity_mismatch_is_error_not_live_fallback(
    completed: tuple[FastAPI, BenchmarkEngine, RunRecord, RunReport],
    monkeypatch: pytest.MonkeyPatch,
    change: Callable[[RunReport], RunReport],
) -> None:
    app, engine, record, report = completed
    replace_report(engine, record, change(report), monkeypatch)
    response = TestClient(app).get(f"/api/runs/{record.manifest.run_id}/finalized-evaluation")
    assert response.status_code == 409
    assert "record_source" not in response.json()


@pytest.mark.parametrize(
    "change", ["gap", "duplicate", "reordered", "foreign", "running", "outcome"]
)
def test_attempt_contiguity_identity_and_outcome(
    completed: tuple[FastAPI, BenchmarkEngine, RunRecord, RunReport], change: str
) -> None:
    _, _, record, report = completed
    task_id, history = next(
        (key, value) for key, value in report.attempt_history.items() if len(value) == 2
    )
    changed = {
        "gap": history[1:],
        "duplicate": (history[0], history[0], history[1]),
        "reordered": tuple(reversed(history)),
        "foreign": (history[0].model_copy(update={"task_id": "foreign"}), history[1]),
        "running": (history[0].model_copy(update={"status": AttemptStatus.RUNNING}), history[1]),
        "outcome": (history[0], history[1].model_copy(update={"status": AttemptStatus.FAILED})),
    }[change]
    candidate = report.model_copy(
        update={"attempt_history": {**report.attempt_history, task_id: changed}}
    )
    if change in {"gap", "running"}:
        assert (
            validate_finalized_report(record.manifest.run_id, record, record.manifest, candidate)
            == "incomplete_attempt_history"
        )
    else:
        with pytest.raises(FinalizedIntegrityError):
            validate_finalized_report(record.manifest.run_id, record, record.manifest, candidate)


def test_immutable_manifest_must_match_current_run(
    completed: tuple[FastAPI, BenchmarkEngine, RunRecord, RunReport],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, engine, record, _ = completed
    changed = record.model_copy(
        update={"manifest": record.manifest.model_copy(update={"retry_limit": 20})}
    )
    monkeypatch.setattr(engine.repository, "get_run", lambda _: changed)
    assert (
        TestClient(app).get(f"/api/runs/{record.manifest.run_id}/finalized-evaluation").status_code
        == 409
    )


@pytest.mark.parametrize("during_read", [False, True])
def test_expiry_is_enforced_before_and_after_evidence_read(
    completed: tuple[FastAPI, BenchmarkEngine, RunRecord, RunReport],
    monkeypatch: pytest.MonkeyPatch,
    during_read: bool,
) -> None:
    app, engine, record, _ = completed
    root = engine.lifecycle.require_live(record.manifest.run_id)
    assert root.expires_at is not None
    expired_at = root.expires_at + timedelta(seconds=1)
    if during_read:
        original = engine.sample_evidence

        def expiring(manifest: RunManifest) -> dict[str, tuple[ArtifactEvidence, ...]]:
            evidence = original(manifest)
            engine.lifecycle.clock = lambda: expired_at
            return evidence

        monkeypatch.setattr(engine, "sample_evidence", expiring)
    else:
        engine.lifecycle.clock = lambda: expired_at
    response = TestClient(app).get(f"/api/runs/{record.manifest.run_id}/finalized-evaluation")
    assert response.status_code == 410


def test_missing_live_owner_denies_valid_immutable_report(
    completed: tuple[FastAPI, BenchmarkEngine, RunRecord, RunReport],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, engine, record, _ = completed
    monkeypatch.setattr(engine, "artifact_has_live_owner", lambda _: False)
    assert (
        TestClient(app).get(f"/api/runs/{record.manifest.run_id}/finalized-evaluation").status_code
        == 410
    )


@pytest.mark.parametrize("during_read", [False, True])
def test_parent_dataset_expiry_is_checked_independently(
    completed: tuple[FastAPI, BenchmarkEngine, RunRecord, RunReport],
    monkeypatch: pytest.MonkeyPatch,
    during_read: bool,
) -> None:
    app, engine, record, _ = completed
    dataset = engine.lifecycle.create("synthetic-finalized-dataset", "dataset")
    dataset = engine.lifecycle.publish(dataset.id)
    assert dataset.expires_at is not None
    key = root_key(record.manifest.run_id)
    row = engine.backend.state.get(key)
    assert row is not None
    engine.backend.state.transact(
        (StateChange(key, {**row.values, "parent_id": dataset.id}, row.etag),)
    )
    deadline = dataset.expires_at
    if during_read:
        original = engine.sample_evidence

        def expiring(manifest: RunManifest) -> dict[str, tuple[ArtifactEvidence, ...]]:
            evidence = original(manifest)
            engine.lifecycle.clock = lambda: deadline
            return evidence

        monkeypatch.setattr(engine, "sample_evidence", expiring)
    else:
        engine.lifecycle.clock = lambda: deadline
    response = TestClient(app).get(f"/api/runs/{record.manifest.run_id}/finalized-evaluation")
    assert response.status_code == 410
    assert engine.lifecycle.require_live(record.manifest.run_id)


@pytest.mark.parametrize("capacity", [False, True])
def test_hash_failure_and_capacity_are_explicit_errors(
    completed: tuple[FastAPI, BenchmarkEngine, RunRecord, RunReport],
    monkeypatch: pytest.MonkeyPatch,
    capacity: bool,
) -> None:
    app, engine, record, _ = completed
    original = engine.artifacts.get_bytes

    def failing(identifier: str) -> bytes:
        if identifier == record.report_artifact_id:
            if capacity:
                raise ArtifactReadCapacityError("busy")
            raise ArtifactIntegrityError("corrupt bytes")
        return original(identifier)

    monkeypatch.setattr(engine.artifacts, "get_bytes", failing)
    response = TestClient(app).get(f"/api/runs/{record.manifest.run_id}/finalized-evaluation")
    assert response.status_code == (503 if capacity else 409)
    if capacity:
        assert response.headers["retry-after"] == "1"
