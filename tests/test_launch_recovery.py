from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest

from ai_bench.artifacts import ArtifactIntegrityError, FileArtifactStore
from ai_bench.backends import local_backend
from ai_bench.contracts import (
    CompatibilityLevel,
    EvaluatorDescriptor,
    ExecutionOutput,
    ExecutionRequest,
    RunManifest,
    RunReport,
    SampleDescriptor,
    SampleRole,
    utc_now,
)
from ai_bench.engine import BenchmarkEngine, NetworkExecutionDisabledError
from ai_bench.hashing import sha256_digest
from ai_bench.lifecycle import ExpiredRootError
from ai_bench.packs import get_pack
from ai_bench.repositories import IdempotencyConflictError, RepositoryError
from ai_bench.storage_state import StateChange, StateConflictError
from ai_bench.worker import RunSupervisor
from tests.test_async_runtime import scoped_pack


def reserve_interrupted_v1_launch(
    engine: BenchmarkEngine,
    pack_root: Path,
    solution_ids: tuple[str, ...] | None,
) -> str:
    pack = get_pack("synthetic_text_intent", pack_root)
    samples = tuple(pack.dataset.samples())
    selected = (
        tuple(sorted(solution_ids))
        if solution_ids is not None
        else tuple(
            sorted(
                solution.id
                for solution in pack.solutions
                if solution.compatibility.level is not CompatibilityLevel.UNSUPPORTED
            )
        )
    )
    solutions = {solution.id: solution for solution in pack.solutions}
    descriptors = {
        identifier: {
            "id": identifier,
            "version": solutions[identifier].version,
            "pipeline": solutions[identifier].pipeline,
            "compatibility": solutions[identifier].compatibility,
            "pricing_snapshot": solutions[identifier].pricing,
        }
        for identifier in selected
    }
    sample_descriptors = tuple(
        SampleDescriptor(
            id=sample.id,
            role=sample.role,
            input_artifact_id=sample.input.id,
            reference_artifact_id=sample.reference.id if sample.reference else None,
        )
        for sample in samples
    )
    evaluator = EvaluatorDescriptor(
        id=pack.evaluator.id,
        version=pack.evaluator.version,
        metrics=tuple(pack.evaluator.metric_definitions),
    )
    hashes = {
        "benchmark": sha256_digest(pack.definition),
        "dataset": sha256_digest(
            {"revision": pack.dataset.revision, "samples": sample_descriptors}
        ),
        "evaluator": sha256_digest(evaluator),
        "viewer": sha256_digest(pack.viewer.descriptor),
        **{
            f"solution:{identifier}": sha256_digest(descriptor)
            for identifier, descriptor in descriptors.items()
        },
        **{
            f"pricing:{identifier}": sha256_digest(solutions[identifier].pricing)
            for identifier in selected
        },
    }
    fingerprint = sha256_digest(
        {
            "benchmark": pack.definition,
            "dataset_revision": pack.dataset.revision,
            "samples": sample_descriptors,
            "solution_descriptors": descriptors,
            "evaluator": evaluator,
            "viewer": pack.viewer.descriptor,
            "descriptor_hashes": hashes,
            "solutions": selected,
            "samples_scored": [sample.id for sample in samples if sample.role is SampleRole.SCORED],
            "networked_execution": any(solutions[identifier].networked for identifier in selected),
            "retry_limit": 2,
        }
    )
    key_hash = sha256_digest({"idempotency_key": "interrupted-v1"})
    run_id = sha256_digest({"idempotency_key_hash": key_hash, "payload_hash": fingerprint})
    engine.repository.reserve_launch(key_hash, fingerprint, run_id)
    assert engine.repository.get_run(run_id) is None
    return run_id


@pytest.mark.parametrize("selected", [None, ("keyword-standalone",)])
def test_orphaned_v1_reservation_repairs_the_original_run(
    selected: tuple[str, ...] | None,
    tmp_path: Path,
    pack_root: Path,
) -> None:
    engine = BenchmarkEngine(tmp_path / "state", pack_root)
    reserved_run_id = reserve_interrupted_v1_launch(engine, pack_root, selected)
    restarted = BenchmarkEngine(tmp_path / "state", pack_root)
    manifest = restarted.create_run(
        "synthetic_text_intent",
        solution_ids=selected,
        retry_limit=2,
        idempotency_key="interrupted-v1",
    )
    assert manifest.run_id == reserved_run_id
    assert manifest.schema_version == "2" and manifest.sample_snapshot_artifact_id
    assert len(restarted.repository.list_tasks(reserved_run_id)) == 6 * len(manifest.solution_ids)
    assert (
        restarted.create_run(
            "synthetic_text_intent",
            solution_ids=selected,
            retry_limit=2,
            idempotency_key="interrupted-v1",
        )
        == manifest
    )
    assert len(restarted.repository.list_runs()) == 1
    assert all(task.attempts == 0 for task in restarted.repository.list_tasks(reserved_run_id))


def test_orphaned_v1_reservation_rejects_changed_parameters_without_overwriting(
    tmp_path: Path,
    pack_root: Path,
) -> None:
    engine = BenchmarkEngine(tmp_path / "state", pack_root)
    reserved_run_id = reserve_interrupted_v1_launch(engine, pack_root, None)
    with pytest.raises(IdempotencyConflictError):
        engine.create_run(
            "synthetic_text_intent",
            retry_limit=3,
            idempotency_key="interrupted-v1",
        )
    assert engine.repository.get_run(reserved_run_id) is None
    assert engine.repository.list_tasks(reserved_run_id) == ()
    restored = engine.create_run(
        "synthetic_text_intent",
        retry_limit=2,
        idempotency_key="interrupted-v1",
    )
    assert restored.run_id == reserved_run_id


def test_migrated_v1_reservation_survives_another_initialization_crash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pack_root: Path,
) -> None:
    engine = BenchmarkEngine(tmp_path / "state", pack_root)
    reserved_run_id = reserve_interrupted_v1_launch(engine, pack_root, None)

    def interrupted_publication(*_args: object) -> None:
        raise OSError("synthetic interruption after reservation migration")

    monkeypatch.setattr(engine, "_register_sample_artifacts", interrupted_publication)
    with pytest.raises(OSError, match="after reservation migration"):
        engine.create_run("synthetic_text_intent", retry_limit=2, idempotency_key="interrupted-v1")
    assert engine.repository.get_run(reserved_run_id) is None
    restarted = BenchmarkEngine(tmp_path / "state", pack_root)
    recovered = restarted.create_run(
        "synthetic_text_intent",
        retry_limit=2,
        idempotency_key="interrupted-v1",
    )
    assert recovered.run_id == reserved_run_id
    assert len(restarted.repository.list_tasks(reserved_run_id)) == 12


def test_concurrent_v1_reservation_repair_has_one_immutable_winner(
    tmp_path: Path,
    pack_root: Path,
) -> None:
    state = tmp_path / "state"
    engine = BenchmarkEngine(state, pack_root)
    reserved_run_id = reserve_interrupted_v1_launch(engine, pack_root, None)

    def replay(_index: int) -> RunManifest:
        return BenchmarkEngine(state, pack_root).create_run(
            "synthetic_text_intent",
            retry_limit=2,
            idempotency_key="interrupted-v1",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        manifests = tuple(executor.map(replay, range(2)))
    assert manifests[0] == manifests[1]
    assert manifests[0].run_id == reserved_run_id
    assert len(engine.repository.list_runs()) == 1
    assert len(engine.repository.list_tasks(reserved_run_id)) == 12


@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize(
    "conflict", ["root-once", "root-always", "different-intent", "identical-intent", "expired"]
)
def test_launch_intent_distinguishes_root_contention_from_conflicting_payload(
    tmp_path: Path,
    pack_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    durable: bool,
    conflict: str,
) -> None:
    engine = BenchmarkEngine(
        tmp_path, pack_root, backend=local_backend(tmp_path, durable_projections=durable)
    )
    reserved = reserve_interrupted_v1_launch(engine, pack_root, None)
    transact = engine.backend.state.transact
    attempts = 0

    def contend(changes: Sequence[StateChange]) -> None:
        nonlocal attempts
        intent = next(
            (
                change
                for change in changes
                if change.key.startswith("launch-intent:") and change.values is not None
            ),
            None,
        )
        if intent is not None:
            attempts += 1
            if conflict == "root-always" or attempts == 1:
                if conflict in {"different-intent", "identical-intent"}:
                    assert intent.values is not None
                    values = dict(intent.values)
                    if conflict == "different-intent":
                        values["manifest_artifact_id"] = "f" * 64
                    transact((StateChange(intent.key, values),))
                else:
                    root = next(change for change in changes if change.key.startswith("root:"))
                    transact((root,))
                    if conflict == "expired":
                        engine.lifecycle.clock = lambda: utc_now() + timedelta(days=11)
        transact(changes)

    monkeypatch.setattr(engine.backend.state, "transact", contend)
    if conflict == "different-intent":
        with pytest.raises(IdempotencyConflictError, match="intent conflicts"):
            engine.create_run(
                "synthetic_text_intent", retry_limit=2, idempotency_key="interrupted-v1"
            )
        assert attempts == 1
        stored = engine.backend.state.get(f"launch-intent:{reserved}")
        assert stored is not None and stored.values["manifest_artifact_id"] == "f" * 64
    elif conflict == "root-always":
        with pytest.raises(StateConflictError, match="initialization contention"):
            engine.create_run(
                "synthetic_text_intent", retry_limit=2, idempotency_key="interrupted-v1"
            )
        assert attempts == 20
        assert engine.repository.list_tasks(reserved) == ()
    elif conflict == "expired":
        with pytest.raises(ExpiredRootError):
            engine.create_run(
                "synthetic_text_intent", retry_limit=2, idempotency_key="interrupted-v1"
            )
        assert attempts == 1
        assert engine.backend.state.get(f"launch-intent:{reserved}") is None
    else:
        manifest = engine.create_run(
            "synthetic_text_intent", retry_limit=2, idempotency_key="interrupted-v1"
        )
        assert manifest.run_id == reserved
        assert attempts == (1 if conflict == "identical-intent" else 2)
        assert len(engine.repository.list_tasks(reserved)) == 12


@pytest.mark.parametrize("driver", ["supervisor", "synchronous"])
def test_terminal_networked_run_finalizes_offline_after_restart(
    driver: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pack_root: Path,
) -> None:
    _, solutions = scoped_pack(pack_root, monkeypatch)
    for solution in solutions:
        solution.networked = True
    monkeypatch.setenv("AI_BENCH_ALLOW_NETWORKED_SOLUTIONS", "true")
    engine = BenchmarkEngine(tmp_path / "state", pack_root)
    manifest = engine.create_run("synthetic_text_intent", allow_networked=True)
    while lease := engine.repository.claim_task("before-crash"):
        engine._execute_lease(lease)
    assert engine.get_report(manifest.run_id) is None
    attempts = sum(task.attempts for task in engine.repository.list_tasks(manifest.run_id))
    monkeypatch.setenv("AI_BENCH_ALLOW_NETWORKED_SOLUTIONS", "false")

    def forbidden_execution(_request: ExecutionRequest) -> ExecutionOutput:
        raise AssertionError("finalization must not invoke a provider")

    for solution in solutions:
        monkeypatch.setattr(solution, "execute", forbidden_execution)
    resumed = BenchmarkEngine(tmp_path / "state", pack_root)
    resumed.repository.set_runtime_error(
        manifest.run_id, "Runtime network authorization is disabled."
    )
    report: RunReport | None
    if driver == "supervisor":
        assert RunSupervisor(resumed).step()
        report = resumed.get_report(manifest.run_id)
    else:
        report = resumed.run_to_completion(manifest.run_id)
    assert report is not None and report.status.value == "completed"
    assert len(report.results) == 12
    assert sum(task.attempts for task in resumed.repository.list_tasks(manifest.run_id)) == attempts
    assert not resumed._network_execution_enabled()
    persisted = resumed.repository.get_run(manifest.run_id)
    assert persisted is not None and persisted.runtime_error is None


def test_synchronous_driver_resumes_supervisor_blocked_pending_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pack_root: Path,
) -> None:
    _, solutions = scoped_pack(pack_root, monkeypatch)
    solutions[0].networked = True
    monkeypatch.setenv("AI_BENCH_ALLOW_NETWORKED_SOLUTIONS", "true")
    engine = BenchmarkEngine(tmp_path / "state", pack_root)
    manifest = engine.create_run(
        "synthetic_text_intent",
        solution_ids=(solutions[0].id,),
        allow_networked=True,
    )
    monkeypatch.setenv("AI_BENCH_ALLOW_NETWORKED_SOLUTIONS", "false")
    assert not RunSupervisor(engine).step()
    blocked = engine.repository.get_run(manifest.run_id)
    assert blocked is not None and blocked.runtime_error is not None
    assert all(task.attempts == 0 for task in engine.repository.list_tasks(manifest.run_id))

    def unexpected_wait(_seconds: float) -> None:
        pytest.fail("The restored synchronous driver left eligible tasks blocked.")

    monkeypatch.setattr("ai_bench.engine.time.sleep", unexpected_wait)
    monkeypatch.setenv("AI_BENCH_ALLOW_NETWORKED_SOLUTIONS", "true")
    resumed = BenchmarkEngine(tmp_path / "state", pack_root)
    report = resumed.run_to_completion(manifest.run_id)
    assert report.status.value == "completed" and len(report.results) == 6
    assert all(result.attempts == 1 for result in report.results)
    current = resumed.repository.get_run(manifest.run_id)
    assert current is not None and current.runtime_error is None
    assert (
        sum(
            event.event_type == "run.resumed"
            for event in resumed.repository.list_events(manifest.run_id)
        )
        == 1
    )


@pytest.mark.parametrize(
    "unresolved",
    ["authorization", "descriptor", "snapshot", "input", "reference"],
)
def test_synchronous_driver_does_not_clear_unresolved_runtime_block(
    unresolved: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pack_root: Path,
) -> None:
    _, solutions = scoped_pack(pack_root, monkeypatch)
    solutions[0].networked = True
    monkeypatch.setenv("AI_BENCH_ALLOW_NETWORKED_SOLUTIONS", "true")
    engine = BenchmarkEngine(tmp_path / "state", pack_root)
    manifest = engine.create_run(
        "synthetic_text_intent",
        solution_ids=(solutions[0].id,),
        allow_networked=True,
    )
    monkeypatch.setenv("AI_BENCH_ALLOW_NETWORKED_SOLUTIONS", "false")
    assert not RunSupervisor(engine).step()
    blocked = engine.repository.get_run(manifest.run_id)
    assert blocked is not None and blocked.runtime_error is not None
    expected: type[Exception] = NetworkExecutionDisabledError
    assert isinstance(engine.artifacts, FileArtifactStore)
    if unresolved != "authorization":
        monkeypatch.setenv("AI_BENCH_ALLOW_NETWORKED_SOLUTIONS", "true")
        if unresolved == "descriptor":
            solutions[0].version = "2.0.0"
            expected = RepositoryError
        elif unresolved == "snapshot":
            snapshot = manifest.sample_snapshot_artifact_id
            assert snapshot is not None
            engine.artifacts._path(snapshot).write_bytes(b"synthetic corrupt snapshot")
            expected = ArtifactIntegrityError
        else:
            artifact_id = (
                manifest.samples[0].input_artifact_id
                if unresolved == "input"
                else manifest.samples[0].reference_artifact_id
            )
            assert artifact_id is not None
            engine.artifacts._path(artifact_id).write_bytes(b"synthetic corrupt artifact")
            expected = ArtifactIntegrityError
    with pytest.raises(expected):
        BenchmarkEngine(tmp_path / "state", pack_root).run_to_completion(manifest.run_id)
    current = engine.repository.get_run(manifest.run_id)
    assert current is not None and current.runtime_error == blocked.runtime_error
    assert all(task.attempts == 0 for task in engine.repository.list_tasks(manifest.run_id))
    assert not any(
        event.event_type == "run.resumed"
        for event in engine.repository.list_events(manifest.run_id)
    )
