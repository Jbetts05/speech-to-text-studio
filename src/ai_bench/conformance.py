"""Plain-Python operator scenarios, also executed by the offline test suite."""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import patch

from ai_bench.archives import export_archive, import_archive
from ai_bench.artifacts import ArtifactIntegrityError, FileArtifactStore
from ai_bench.azure_artifacts import AzureBlobArtifactStore
from ai_bench.backend_proof import (
    REQUIRED_SCENARIOS,
    SUITE_VERSION,
    BackendConformanceReceipt,
    backend_code_signature,
    publish_receipt,
)
from ai_bench.contracts import (
    Contract,
    ExecutionRequest,
    Modality,
    ProviderFailureEvidence,
    RetryDelay,
    RunManifest,
    RunStatus,
    SampleDescriptor,
    SampleSnapshot,
    TaskStatus,
    Usage,
    utc_now,
)
from ai_bench.engine import BenchmarkEngine
from ai_bench.failures import ProviderFailure
from ai_bench.hashing import content_digest, sha256_digest
from ai_bench.lifecycle import ExpiredRootError
from ai_bench.packs import get_pack
from ai_bench.parallel_conformance import parallel_execution_scenarios
from ai_bench.receipts import ResponseReceiptRepository
from ai_bench.repositories import (
    FinalizerFenceError,
    IdempotencyConflictError,
    LeaseLostError,
    RepositoryError,
)
from ai_bench.storage_state import AzureTableStateStore, StateChange, StateConflictError
from ai_bench.table_repository import AzureTableRepository


class ConformanceResult(Contract):
    suite_version: str = SUITE_VERSION
    completed_at: datetime
    backend: str
    scenarios: tuple[str, ...]
    run_ids: tuple[str, ...]
    provider_calls: int = 0
    live_promotion: bool = False
    receipt_digest: str | None = None


def run_conformance(
    engine: BenchmarkEngine, *, benchmark_id: str, solution_id: str
) -> ConformanceResult:
    with engine.backend.execution_session():
        return _run_conformance(engine, benchmark_id=benchmark_id, solution_id=solution_id)


def _run_conformance(
    engine: BenchmarkEngine, *, benchmark_id: str, solution_id: str
) -> ConformanceResult:
    """Run only in an empty operation-owned namespace; no provider can be selected."""
    if engine.repository.list_runs() or engine.backend.state.page("root:", limit=1).rows:
        raise ValueError("conformance requires an empty disposable namespace")
    if (
        isinstance(engine.backend.state, AzureTableStateStore)
        and engine.backend.name == "azure-table-blob"
        and not engine.backend.state.namespace.startswith("conformance-")
    ):
        raise ValueError("live conformance requires an operation-owned conformance- namespace")
    if not isinstance(engine.repository, AzureTableRepository):
        raise ValueError("operator conformance requires an explicit durable projection backend")
    pack = get_pack(benchmark_id, engine.pack_root)
    solution = next((item for item in pack.solutions if item.id == solution_id), None)
    if solution is None or solution.networked or not pack.dataset.revision.provenance.synthetic:
        raise ValueError("conformance requires an offline solution and synthetic dataset")
    scenarios = []
    manifest = engine.create_run(
        benchmark_id, solution_ids=(solution_id,), idempotency_key="conformance-replay"
    )
    replay = engine.create_run(
        benchmark_id, solution_ids=(solution_id,), idempotency_key="conformance-replay"
    )
    if replay != manifest:
        raise AssertionError("idempotent replay changed the immutable manifest")
    try:
        engine.create_run(
            benchmark_id,
            solution_ids=(solution_id,),
            retry_limit=2,
            idempotency_key="conformance-replay",
        )
    except IdempotencyConflictError:
        pass
    else:
        raise AssertionError("conflicting launch key was accepted")
    scenarios.append("reservation-replay-conflict")
    tasks = engine.repository.list_tasks(manifest.run_id)
    if len(tasks) != len(manifest.sample_ids) * len(manifest.solution_ids):
        raise AssertionError("published task matrix is incomplete")
    if engine.repository.acquire_finalizer(manifest.run_id) is not None:
        raise AssertionError("unfinished run acquired a finalizer")
    scenarios.append("complete-matrix-publication")
    lease = engine.repository.claim_task("conformance", run_id=manifest.run_id)
    if lease is None:
        raise AssertionError("ready offline work was not claimable")
    bad_lease = lease.model_copy(update={"token": lease.token + 1})
    try:
        engine.repository.renew_lease(bad_lease)
    except (LeaseLostError, RepositoryError):
        pass
    else:
        raise AssertionError("stale task token renewed a lease")
    engine.repository.release_unstarted(lease)
    task = engine.repository.get_task(lease.task.id)
    if task is None or task.attempts != 0 or task.status is not TaskStatus.PENDING:
        raise AssertionError("unstarted authorization deferral consumed an attempt")
    scenarios.append("task-fence-unstarted-release")
    report = engine.run_to_completion(manifest.run_id, worker_id="conformance")
    if report.status is not RunStatus.COMPLETED:
        raise AssertionError("offline execution did not complete")
    for result in report.results:
        history = engine.repository.list_attempts(result.task_id)
        if not history or not {"raw", "parsed", "result"}.issubset(
            {evidence.role for evidence in history[-1].evidence}
        ):
            raise AssertionError("canonical result was published without attempt evidence")
    scenarios.append("evidence-first-terminal-execution")
    events = engine.repository.list_events(manifest.run_id, limit=500)
    if [event.sequence for event in events] != sorted({event.sequence for event in events}) or sum(
        event.event_type == "run.finalized" for event in events
    ) != 1:
        raise AssertionError("event cursors or finalization publication are not stable")
    if events and engine.repository.list_events(manifest.run_id, after=events[-1].sequence):
        raise AssertionError("event cursor replay returned earlier events")
    if engine.run_to_completion(manifest.run_id, worker_id="conformance") != report:
        raise AssertionError("terminal report replay changed the original report")
    scenarios.append("event-cursor-terminal-replay")
    root = engine.lifecycle.require_live(manifest.run_id)
    if root.expires_at is None or root.pinned:
        raise AssertionError("ordinary conformance execution was accidentally pinned")
    scenarios.append("ordinary-run-retention")
    engine.repository.release_supervisor("conformance")
    scenarios.extend(parallel_execution_scenarios(engine, benchmark_id, solution_id))
    _state_scenarios(engine)
    scenarios.append("conditional-state-transactions")
    if _ambiguous_response(engine):
        scenarios.append("ambiguous-response-reconciliation")
    _large_matrix(engine, manifest)
    scenarios.append("large-matrix-crash-repair")
    _eligible_later_page(engine, manifest)
    scenarios.append("eligible-lanes-beyond-first-page")
    _recovery(engine, benchmark_id, solution_id, checkpoint=True)
    scenarios.append("supervisor-takeover-checkpoint-recovery")
    _recovery(engine, benchmark_id, solution_id, checkpoint=False)
    scenarios.append("indeterminate-attempt-recovery")
    _cooldown_and_finalizer(engine, manifest)
    scenarios.extend(("failure-cooldown-isolation", "finalizer-takeover"))
    _object_integrity(engine, manifest.run_id)
    scenarios.append("immutable-object-integrity-generations")
    _archive_and_expiry(engine, manifest)
    scenarios.extend(
        (
            "expiry-physical-collection",
            "exact-terminal-archive-transfer",
            "shared-object-baseline-pin",
        )
    )
    live = (
        engine.backend.name == "azure-table-blob"
        and os.getenv("AI_BENCH_RUNTIME", "local").lower() == "azure"
        and REQUIRED_SCENARIOS.issubset(scenarios)
    )
    completed = utc_now()
    receipt_digest = None
    if live:
        state = engine.backend.state
        objects = engine.artifacts
        if not isinstance(state, AzureTableStateStore) or not isinstance(
            objects, AzureBlobArtifactStore
        ):
            raise AssertionError("live checkpoint requires the actual Table and Blob transports")
        receipt_digest = publish_receipt(
            state,
            objects,
            BackendConformanceReceipt(
                code_signature=backend_code_signature(),
                configuration_signature=engine.backend.configuration_signature,
                source_namespace=state.namespace,
                completed_at=completed,
                scenarios=tuple(scenarios),
            ),
        )
    return ConformanceResult(
        completed_at=completed,
        backend=engine.backend.name,
        scenarios=tuple(scenarios),
        run_ids=(manifest.run_id,),
        live_promotion=live,
        receipt_digest=receipt_digest,
    )


def _state_scenarios(engine: BenchmarkEngine) -> None:
    state = engine.backend.state
    state.transact((StateChange("conformance:cas", {"value": 1}),))
    first = state.get("conformance:cas")
    assert first is not None
    state.transact((StateChange(first.key, {"value": 2}, first.etag),))
    try:
        state.transact(
            (
                StateChange(first.key, {"value": 3}, first.etag),
                StateChange("conformance:must-not-exist", {}),
            )
        )
    except StateConflictError:
        pass
    else:
        raise AssertionError("a stale ETag changed a row")
    if state.get("conformance:must-not-exist") is not None:
        raise AssertionError("failed entity-group transaction partially committed")
    try:
        state.transact(tuple(StateChange(f"conformance:limit:{index}", {}) for index in range(101)))
    except ValueError:
        pass
    else:
        raise AssertionError("transaction exceeded the 100-entity limit")
    try:
        state.transact((StateChange("conformance:large-property", {"value": "x" * 40_000}),))
    except ValueError:
        pass
    else:
        raise AssertionError("large immutable payload was admitted into a Table property")


def _ambiguous_response(engine: BenchmarkEngine) -> bool:
    state = engine.backend.state
    if not isinstance(state, AzureTableStateStore):
        return False
    original = state.client.submit_transaction
    calls = 0

    def lost(operations: Any) -> None:
        nonlocal calls
        calls += 1
        original(operations)
        raise TimeoutError("conformance: lost successful transaction response")

    with patch.object(state.client, "submit_transaction", lost):
        state.transact((StateChange("conformance:lost-response", {"committed": True}),))
    if calls != 1 or state.get("conformance:lost-response") is None:
        raise AssertionError("ambiguous success was not reconciled by reading its exact result")
    return True


def _large_matrix(engine: BenchmarkEngine, original: RunManifest) -> None:
    sample = engine._samples_for_manifest(original)[0]
    samples = tuple(sample.model_copy(update={"id": f"case-{index:03d}"}) for index in range(130))
    revision = original.dataset_revision.model_copy(
        update={
            "id": "conformance-large-matrix",
            "content_hash": sha256_digest(samples),
        }
    )
    snapshot = engine.artifacts.put_json(SampleSnapshot(dataset_revision=revision, samples=samples))
    descriptors = tuple(
        SampleDescriptor(
            id=item.id,
            role=item.role,
            input_artifact_id=item.input.id,
            reference_artifact_id=None if item.reference is None else item.reference.id,
        )
        for item in samples
    )
    manifest = original.model_copy(
        update={
            "run_id": sha256_digest({"matrix": original.run_id}),
            "dataset_revision": revision,
            "samples": descriptors,
            "sample_ids": tuple(item.id for item in samples),
            "sample_snapshot_artifact_id": snapshot.id,
            "launch_idempotency_key_hash": None,
            "descriptor_hashes": {
                **original.descriptor_hashes,
                "dataset": sha256_digest({"revision": revision, "samples": descriptors}),
                "sample-snapshot": snapshot.id,
            },
        }
    )
    artifact = engine.artifacts.put_json(manifest)
    engine.repository.create_run(manifest, artifact.id)
    state = engine.backend.state
    original_transact = state.transact
    batches = 0

    def crash(changes: Sequence[StateChange]) -> None:
        nonlocal batches
        if any(change.key.startswith("task:") and change.etag is None for change in changes):
            batches += 1
            if batches == 2:
                raise InterruptedError("conformance: initialization interruption")
        original_transact(changes)

    with patch.object(state, "transact", crash):
        try:
            engine.repair_initialization(manifest)
        except InterruptedError:
            pass
        else:
            raise AssertionError("large initialization did not cross a transaction boundary")
    if engine.repository.claim_task("conformance", run_id=manifest.run_id) is not None:
        raise AssertionError("a partially initialized matrix became executable")
    engine.repair_initialization(manifest)
    if len(engine.repository.list_tasks(manifest.run_id)) != 130:
        raise AssertionError("large initialization did not resume to the exact task matrix")
    engine.repository.release_supervisor("conformance")


def _eligible_later_page(engine: BenchmarkEngine, original: RunManifest) -> None:
    repository = engine.repository
    assert isinstance(repository, AzureTableRepository)
    manifest = engine.create_run(original.benchmark.id, solution_ids=original.solution_ids)
    for task in repository.list_tasks(manifest.run_id):
        row = repository._required(f"task:{task.id}")
        due = repository._required(str(row.values["due_key"]))
        key = repository._due_key(task.id, 1)
        repository.state.transact(
            (
                StateChange(row.key, {**row.values, "due_key": key, "next_available": 1}, row.etag),
                StateChange(due.key, None, due.etag),
                StateChange(key, due.values),
            )
        )
    first = repository.state.page("due:", limit=100)
    if len(first.rows) != 100 or any(row.values["run_id"] == manifest.run_id for row in first.rows):
        raise AssertionError("the probe did not place eligible work beyond a blocked first page")
    repository._claim_cursors.clear()
    lease = repository.claim_task(
        "conformance",
        eligible_tasks=frozenset((manifest.run_id, item) for item in manifest.solution_ids),
    )
    if lease is None or lease.task.run_id != manifest.run_id:
        raise AssertionError("blocked first-page work starved an eligible lane")
    repository.release_unstarted(lease)
    repository.release_supervisor("conformance")


def _recovery(
    engine: BenchmarkEngine, benchmark_id: str, solution_id: str, *, checkpoint: bool
) -> None:
    repository = engine.repository
    assert isinstance(repository, AzureTableRepository)
    manifest = engine.create_run(benchmark_id, solution_ids=(solution_id,), retry_limit=2)
    clock = repository.clock
    now = [clock()]
    repository.clock = lambda: now[0]
    try:
        lease = repository.claim_task("conformance", lease_seconds=300, run_id=manifest.run_id)
        if lease is None:
            raise AssertionError("conformance work could not be claimed")
        assert isinstance(repository, ResponseReceiptRepository)
        if checkpoint:
            pack = get_pack(benchmark_id, engine.pack_root)
            solution = next(item for item in pack.solutions if item.id == solution_id)
            sample = next(
                item
                for item in engine._samples_for_manifest(manifest)
                if item.id == lease.task.sample_id
            )
            request = ExecutionRequest(
                run_id=manifest.run_id,
                task_id=lease.task.id,
                sample=sample.model_copy(update={"reference": None, "metadata": {}}),
                input_bytes=engine.artifacts.get_bytes(sample.input.id),
                idempotency_key=lease.task.id,
                attempt_number=lease.attempt_number,
                retry_policy=manifest.retry_policy,
            )
            engine._execute_response(lease, solution, request)
            if repository.response_receipt(lease) is None:
                raise AssertionError("response was not durably receipted")
        else:
            repository.begin_dispatch(lease)
        now[0] += timedelta(seconds=301)
        if not repository.acquire_supervisor("conformance-replacement"):
            raise AssertionError("expired supervisor could not be replaced")
        try:
            repository.renew_lease(lease)
        except LeaseLostError:
            pass
        else:
            raise AssertionError("a replaced supervisor renewed its old task")
        if repository.recover_expired_leases() != (1, 0):
            raise AssertionError("expired attempt did not recover once")
        if checkpoint:
            resumed = repository.claim_task(
                "conformance-replacement", lease_seconds=300, run_id=manifest.run_id
            )
            if (
                resumed is None
                or resumed.task.id != lease.task.id
                or resumed.attempt_number != lease.attempt_number
            ):
                raise AssertionError("recorded response was turned into a new physical attempt")
            original_receipt = repository.response_receipt(resumed)
            engine._execute_lease(resumed)
            if (
                repository.list_attempts(resumed.task.id)[0].attempt_number != 1
                or original_receipt is None
            ):
                raise AssertionError("response recovery did not preserve the original attempt")
        else:
            attempt = repository.list_attempts(lease.task.id)[0]
            if (
                attempt.error_type != "IndeterminateProviderOutcome"
                or attempt.telemetry is None
                or attempt.telemetry.usage.complete
                or attempt.telemetry.usage.dimensions
                or attempt.telemetry.cost.amount is not None
            ):
                raise AssertionError("indeterminate dispatch was converted to invented known usage")
    finally:
        repository.release_supervisor("conformance")
        repository.release_supervisor("conformance-replacement")
        repository.clock = clock


def _object_integrity(engine: BenchmarkEngine, run_id: str) -> None:
    objects = engine.artifacts
    content = b"synthetic conformance object"
    digest = content_digest(content)
    engine.lifecycle.attach(run_id, digest)
    artifact = objects.put_bytes(content, modality=Modality.TEXT, media_type="text/plain")
    engine.repository.register_artifact(artifact)
    if isinstance(objects, FileArtifactStore):
        old = objects.object_etag(digest)
        objects._path(digest).write_bytes(b"x" * len(content))
        try:
            objects.get_bytes(digest)
        except ArtifactIntegrityError:
            pass
        else:
            raise AssertionError("equal-size corrupt object was accepted")
        objects._path(digest).write_bytes(content)
        try:
            objects.delete(digest, etag=old)
        except FileExistsError:
            pass
        else:
            raise AssertionError("stale file generation deleted the replacement")
    elif isinstance(objects, AzureBlobArtifactStore):
        blob = objects._blob(digest)
        old = str(blob.get_blob_properties().etag)
        blob.upload_blob(
            b"x" * len(content),
            overwrite=True,
            etag=old,
            match_condition=objects._condition(),
        )
        try:
            objects.get_bytes(digest)
        except ArtifactIntegrityError:
            pass
        else:
            raise AssertionError("equal-size corrupt Blob was accepted")
        blob.upload_blob(
            content,
            overwrite=True,
            etag=str(blob.get_blob_properties().etag),
            match_condition=objects._condition(),
        )
        try:
            objects.delete(digest, etag=old)
        except Exception as error:
            if getattr(error, "status_code", None) != 412:
                raise
        else:
            raise AssertionError("stale Blob ETag deleted the replacement")
    else:
        raise TypeError("conformance requires an integrity-checking artifact store")
    if objects.get_bytes(digest) != content:
        raise AssertionError("conditional-delete test damaged current bytes")


def _cooldown_and_finalizer(engine: BenchmarkEngine, original: RunManifest) -> None:
    repository = engine.repository
    scope = sha256_digest({"synthetic-throttle-scope": original.run_id})
    solution_id = original.solution_ids[0]
    descriptor = original.solution_descriptors[solution_id].model_copy(
        update={"throttle_scope": scope}
    )
    cooled = original.model_copy(
        update={
            "run_id": sha256_digest({"cooled": original.run_id}),
            "retry_limit": 2,
            "solution_descriptors": {solution_id: descriptor},
            "launch_idempotency_key_hash": None,
            "descriptor_hashes": {
                **original.descriptor_hashes,
                f"solution:{solution_id}": sha256_digest(descriptor),
            },
        }
    )
    stored = engine.artifacts.put_json(cooled)
    repository.create_run(cooled, stored.id)
    engine.repair_initialization(cooled)
    lease = repository.claim_task("conformance", run_id=cooled.run_id)
    if lease is None:
        raise AssertionError("cooldown scenario could not claim initial work")
    raw = engine.artifacts.put_bytes(
        b'{"error":"synthetic conformance throttle"}',
        modality=Modality.STRUCTURED,
        media_type="application/json",
    )
    engine._record(lease, raw, "raw")
    failure = ProviderFailureEvidence(
        classification="rate-limit",
        message="Synthetic conformance throttle.",
        http_status=429,
        retryable=True,
        usage=Usage(
            dimensions={},
            complete=False,
            uncertainty="Synthetic quota response has unknown usage.",
        ),
        retry_delay=RetryDelay(scheduled_seconds=Decimal(60), source="retry-after-seconds"),
        raw_artifact_id=raw.id,
    )
    repository.record_attempt_failure(lease, failure)
    repository.fail_attempt(lease, ProviderFailure(failure))
    if repository.claim_task("conformance", run_id=cooled.run_id) is not None:
        raise AssertionError("shared cooldown did not suppress eligible tasks in its scope")
    unrelated = engine.create_run(
        original.benchmark.id, solution_ids=original.solution_ids, retry_limit=1
    )
    while (candidate := repository.claim_task("conformance", run_id=unrelated.run_id)) is not None:
        repository.fail_attempt(candidate, RuntimeError("synthetic conformance terminal failure"))
    if any(
        task.status is not TaskStatus.POISONED for task in repository.list_tasks(unrelated.run_id)
    ):
        raise AssertionError("one throttle scope blocked unrelated work")
    stale = repository.acquire_finalizer(unrelated.run_id)
    current = repository.acquire_finalizer(unrelated.run_id)
    if stale is None or current is None or stale >= current:
        raise AssertionError("finalizer generations did not increase")
    completed = repository.get_run(original.run_id)
    assert completed is not None and completed.report_artifact_id is not None
    try:
        repository.finalize_run(
            unrelated.run_id, stale, RunStatus.FAILED, completed.report_artifact_id
        )
    except FinalizerFenceError:
        pass
    else:
        raise AssertionError("an old finalizer published a report")
    repository.release_supervisor("conformance")
    if not repository.acquire_supervisor("conformance-replacement"):
        raise AssertionError("finalizer supervisor takeover failed")
    try:
        repository.finalize_run(
            unrelated.run_id, current, RunStatus.FAILED, completed.report_artifact_id
        )
    except FinalizerFenceError:
        pass
    else:
        raise AssertionError("a prior supervisor finalized after takeover")
    with engine.supervisor_session("conformance-replacement"):
        engine._finalize(unrelated.run_id)
    repository.release_supervisor("conformance-replacement")


def _archive_and_expiry(engine: BenchmarkEngine, manifest: RunManifest) -> None:
    report_bytes = engine.get_report_bytes(manifest.run_id)
    directory = engine.state_root / "conformance-archive"
    digest = export_archive(engine, manifest.run_id, directory)
    clock = engine.lifecycle.clock
    engine.lifecycle.clock = lambda: clock() + timedelta(days=11)
    try:
        try:
            engine.get_report_bytes(manifest.run_id)
        except ExpiredRootError:
            pass
        else:
            raise AssertionError("expired report remained readable")
        if engine.repository.claim_task("conformance") is not None:
            raise AssertionError("expired work was claimed")
        for _ in range(100):
            engine.reap_expired(limit=20)
            if not engine.backend.state.page("root:", limit=1).rows:
                break
        else:
            raise AssertionError("bounded resumable reaping did not remove expired roots")
        if engine.repository.list_runs():
            raise AssertionError("expired history remained indexed")
    finally:
        engine.lifecycle.clock = clock
        engine.repository.release_supervisor("conformance")
    if import_archive(engine, directory, expected_digest=digest) != manifest.run_id:
        raise AssertionError("archive import changed the original run identity")
    if engine.get_report_bytes(manifest.run_id) != report_bytes:
        raise AssertionError("archive import changed original report bytes")
    directory.rename(engine.state_root / "conformance-archive-offline")
    if engine.get_report_bytes(manifest.run_id) != report_bytes:
        raise AssertionError("imported baseline still depends on its source archive")
    if engine.repository.claim_task("conformance") is not None:
        raise AssertionError("imported archival work became executable")
    engine.repository.release_supervisor("conformance")
    installed = engine.datasets.list(manifest.benchmark.id).items[0]
    ordinary = engine.create_run(
        manifest.benchmark.id, solution_ids=manifest.solution_ids, dataset_id=installed.id
    )
    root = engine.lifecycle.require_live(ordinary.run_id)
    if root.pinned or root.expires_at != ordinary.created_at + timedelta(days=10):
        raise AssertionError("ordinary work on pinned data inherited the baseline pin")
    engine.lifecycle.clock = lambda: clock() + timedelta(days=11)
    try:
        for _ in range(100):
            engine.reap_expired(limit=20)
            if engine.lifecycle.get(ordinary.run_id) is None:
                break
        else:
            raise AssertionError("ordinary derived work could not be collected")
        if engine.get_report_bytes(manifest.run_id) != report_bytes:
            raise AssertionError("collection removed shared baseline evidence")
        engine.datasets.resolve(installed.id, manifest.benchmark.id)
    finally:
        engine.lifecycle.clock = clock
