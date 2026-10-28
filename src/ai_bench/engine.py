"""Offline-first benchmark orchestration."""

from __future__ import annotations

import os
import re
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import uuid4

from ai_bench.artifacts import ArtifactNotFoundError
from ai_bench.backends import BackendBundle, configured_backend
from ai_bench.contracts import (
    Artifact,
    ArtifactEvidence,
    ArtifactViewer,
    BenchmarkDefinition,
    BenchmarkSample,
    CompatibilityLevel,
    ConfiguredSolution,
    DatasetAdapter,
    DatasetRevision,
    Evaluator,
    EvaluatorDescriptor,
    ExecutionOutput,
    ExecutionRequest,
    ExternalDataset,
    JsonValue,
    ProviderFailureEvidence,
    RetryPolicy,
    RunManifest,
    RunReport,
    SampleDescriptor,
    SampleRole,
    SampleSnapshot,
    SchedulingPolicy,
    SolutionAdapter,
    SolutionDescriptor,
    TaskResult,
    TaskStatus,
    TelemetryEvidence,
    utc_now,
)
from ai_bench.dataset_imports import DatasetImporter
from ai_bench.failures import ProviderFailure
from ai_bench.hashing import content_digest, sha256_digest
from ai_bench.lifecycle import LifecycleRoot, root_key
from ai_bench.packs import BenchmarkPack, get_pack, scheduling_policy, validate_dataset
from ai_bench.receipts import (
    AttemptPersistencePendingError,
    ProviderReceipt,
    ResponseReceiptRepository,
    UncertainDispatchError,
    encode_receipt,
    materialize_receipt,
)
from ai_bench.reports import build_report, write_report_files
from ai_bench.repositories import (
    FinalizerFenceError,
    IdempotencyConflictError,
    Lease,
    LeaseLostError,
    RepositoryError,
    SqliteRepository,
)
from ai_bench.runtime_policy import PublicExecutionPolicy
from ai_bench.storage_state import StateChange, StateConflictError
from ai_bench.table_repository import AzureTableRepository
from ai_bench.uploads import UploadService


class NetworkExecutionDisabledError(RuntimeError):
    pass


@dataclass(frozen=True)
class _DatasetSelection:
    definition: BenchmarkDefinition
    dataset: DatasetAdapter
    solutions: tuple[SolutionAdapter, ...]
    evaluator: Evaluator
    viewer: ArtifactViewer
    scheduling_policy: SchedulingPolicy | None = None


@dataclass(frozen=True)
class PreparedExecution:
    manifest: RunManifest
    pack: BenchmarkPack
    samples: tuple[BenchmarkSample, ...]


class BenchmarkEngine:
    def __init__(
        self,
        state_root: Path,
        pack_root: Path | None = None,
        *,
        lease_seconds: int = 60,
        backend: BackendBundle | None = None,
        public_policy: PublicExecutionPolicy | None = None,
    ) -> None:
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        self.state_root = state_root.resolve()
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.pack_root = pack_root
        self.lease_seconds = lease_seconds
        self.backend = backend or configured_backend(self.state_root, public_policy=public_policy)
        if public_policy is not None and backend is not None:
            if backend.name == "azure-table-blob" and backend.public_policy != public_policy:
                raise ValueError("Azure backend proof and requested public policy must agree")
            self.backend.public_policy = public_policy
            if isinstance(self.backend.repository, SqliteRepository | AzureTableRepository):
                self.backend.repository.public_policy = public_policy
        self.public_policy = self.backend.public_policy
        self.artifacts = self.backend.artifacts
        self.repository = self.backend.repository
        self.datasets = self.backend.datasets
        self.lifecycle = self.backend.lifecycle
        self.uploads = UploadService(
            self.backend.state,
            self.backend.staging or self.artifacts,
            self.lifecycle,
            self.datasets,
            self.state_root / "upload-work",
            admission_guard=self.backend.require_execution_ready,
            public_policy=self.public_policy,
        )
        self.reports_root = self.state_root / "reports"
        self._initialization_cursor: str | None = None

    def create_run(
        self,
        pack_id: str,
        *,
        solution_ids: tuple[str, ...] | None = None,
        retry_limit: int = 3,
        allow_networked: bool = False,
        idempotency_key: str | None = None,
        dataset_id: str | None = None,
        selected_dataset_id: str | None = None,
    ) -> RunManifest:
        self.backend.require_execution_ready()
        if selected_dataset_id is not None:
            if dataset_id is not None and dataset_id != selected_dataset_id:
                raise ValueError("dataset_id and selected_dataset_id must select the same dataset")
            dataset_id = selected_dataset_id
        if not 1 <= retry_limit <= 20:
            raise ValueError("retry_limit must be between 1 and 20")
        if self.public_policy is not None and retry_limit > self.public_policy.max_attempts:
            raise ValueError("run exceeds the configured total-attempt limit")
        if self.backend.name == "azure-table-blob" and not 1 <= retry_limit <= 3:
            raise ValueError("hosted runs allow one to three total attempts")
        key_hash: str | None = None
        request_hash: str | None = None
        run_id_hint: str | None = None
        legacy_request_hash: str | None = None
        if idempotency_key is not None:
            if not re.fullmatch(r"[\x21-\x7e]{1,200}", idempotency_key):
                raise ValueError("idempotency key must be 1-200 visible ASCII characters")
            key_hash = sha256_digest({"idempotency_key": idempotency_key})
            reservation = self.repository.get_launch_reservation(key_hash)
            if reservation is not None:
                self.assert_run_live(reservation.run_id)
            historical = self.repository.launch_for_key(key_hash)
            if historical is not None and historical.manifest.schema_version == "1":
                previous = historical.manifest
                if (
                    previous.benchmark.id != pack_id
                    or dataset_id is not None
                    or (
                        solution_ids is not None
                        and tuple(sorted(solution_ids)) != previous.solution_ids
                    )
                    or previous.retry_limit != retry_limit
                    or (previous.networked_execution and not allow_networked)
                ):
                    raise IdempotencyConflictError(
                        "the Idempotency-Key was already used with a different request"
                    )
                if historical.report_artifact_id is None:
                    self._assert_manifest_matches_pack(previous, get_pack(pack_id, self.pack_root))
                    self.repair_initialization(previous)
                return previous
            request_hash = sha256_digest(
                {
                    "benchmark_id": pack_id,
                    "solution_ids": None if solution_ids is None else tuple(sorted(solution_ids)),
                    "retry_limit": retry_limit,
                    "allow_networked": allow_networked,
                    **({"dataset_id": dataset_id} if dataset_id is not None else {}),
                }
            )
            if reservation is None:
                run_id_hint = None
            elif (
                historical is None
                and reservation.payload_hash != request_hash
                and reservation.run_id
                == sha256_digest(
                    {
                        "idempotency_key_hash": key_hash,
                        "payload_hash": reservation.payload_hash,
                    }
                )
            ):
                legacy_request_hash = request_hash
            else:
                if reservation.payload_hash != request_hash:
                    raise IdempotencyConflictError(
                        "the Idempotency-Key was already used with a different request"
                    )
                run_id_hint = reservation.run_id
                existing = self.repository.get_run(run_id_hint)
                if existing is not None:
                    self.repair_initialization(existing.manifest)
                    return existing.manifest
                restored = self._restore_initialization(run_id_hint)
                if restored is not None:
                    return restored
        pack = get_pack(pack_id, self.pack_root)
        if dataset_id is not None:
            pack = _DatasetSelection(
                definition=pack.definition,
                dataset=self.datasets.resolve(dataset_id, pack_id),
                solutions=pack.solutions,
                evaluator=pack.evaluator,
                viewer=pack.viewer,
                scheduling_policy=scheduling_policy(pack),
            )
        validate_dataset(pack)
        samples = tuple(pack.dataset.samples())
        solutions_by_id = {solution.id: solution for solution in pack.solutions}
        if solution_ids is not None and not solution_ids:
            raise ValueError("solution_ids cannot be empty when provided")
        selected_ids = (
            tuple(
                sorted(
                    solution.id
                    for solution in pack.solutions
                    if solution.compatibility.level is not CompatibilityLevel.UNSUPPORTED
                )
            )
            if solution_ids is None
            else tuple(sorted(solution_ids))
        )
        if (
            not selected_ids
            or len(selected_ids) > 20
            or len(set(selected_ids)) != len(selected_ids)
        ):
            raise ValueError("select between 1 and 20 unique solutions")
        unknown = set(selected_ids) - set(solutions_by_id)
        if unknown:
            raise ValueError(f"unknown solutions: {', '.join(sorted(unknown))}")
        unsupported = [
            solution_id
            for solution_id in selected_ids
            if solutions_by_id[solution_id].compatibility.level is CompatibilityLevel.UNSUPPORTED
        ]
        if unsupported:
            raise ValueError("unsupported solutions cannot execute: " + ", ".join(unsupported))
        networked = any(solutions_by_id[solution_id].networked for solution_id in selected_ids)
        environment_allows_network = (
            os.getenv("AI_BENCH_ALLOW_NETWORKED_SOLUTIONS", "false").lower() == "true"
        )
        if networked and not (allow_networked and environment_allows_network):
            raise NetworkExecutionDisabledError(
                "networked execution requires both an explicit run option and "
                "AI_BENCH_ALLOW_NETWORKED_SOLUTIONS=true"
            )
        if networked and self.backend.name == "azure-table-blob" and not self.backend.live_proof:
            raise NetworkExecutionDisabledError(
                "hosted provider execution requires separately authorized live backend proof"
            )
        sample_descriptors = tuple(
            SampleDescriptor(
                id=sample.id,
                role=sample.role,
                input_artifact_id=sample.input.id,
                reference_artifact_id=(None if sample.reference is None else sample.reference.id),
            )
            for sample in samples
        )
        for solution_id in selected_ids:
            solution = solutions_by_id[solution_id]
            if isinstance(solution, ConfiguredSolution):
                solution.validate_execution()
        solution_descriptors = {
            solution_id: self._solution_descriptor(solutions_by_id[solution_id])
            for solution_id in selected_ids
        }
        evaluator_descriptor = EvaluatorDescriptor(
            id=pack.evaluator.id,
            version=pack.evaluator.version,
            metrics=tuple(pack.evaluator.metric_definitions),
        )
        launch_payload: dict[str, object] = {
            "benchmark": pack.definition,
            "dataset_revision": pack.dataset.revision,
            "samples": sample_descriptors,
            "evaluator": evaluator_descriptor,
            "viewer": pack.viewer.descriptor,
            "solutions": selected_ids,
            "samples_scored": [sample.id for sample in samples if sample.role is SampleRole.SCORED],
            "networked_execution": networked,
            "retry_limit": retry_limit,
        }
        if legacy_request_hash is not None and key_hash is not None:
            legacy_hash = sha256_digest(
                {
                    **launch_payload,
                    "solution_descriptors": {
                        identifier: descriptor.model_dump(
                            mode="json",
                            exclude={"settings", "throttle_scope"},
                        )
                        for identifier, descriptor in solution_descriptors.items()
                    },
                    "descriptor_hashes": self._descriptor_hashes(
                        pack,
                        sample_descriptors,
                        solution_descriptors,
                        evaluator_descriptor,
                        legacy=True,
                    ),
                }
            )
            run_id_hint = self.repository.upgrade_legacy_launch(
                key_hash,
                legacy_hash,
                legacy_request_hash,
            )
            existing = self.repository.get_run(run_id_hint)
            if existing is not None:
                self.repair_initialization(existing.manifest)
                return existing.manifest
        retry_policy = RetryPolicy()
        policy = scheduling_policy(pack)
        snapshot_value = SampleSnapshot(
            dataset_revision=pack.dataset.revision,
            samples=samples,
            metadata=(
                pack.dataset.snapshot_metadata()
                if isinstance(pack.dataset, ExternalDataset)
                else {}
            ),
        )
        snapshot_id = sha256_digest(snapshot_value)
        descriptor_hashes = self._descriptor_hashes(
            pack,
            sample_descriptors,
            solution_descriptors,
            evaluator_descriptor,
            dataset_revision=pack.dataset.revision,
            snapshot_id=snapshot_id,
            retry_policy=retry_policy,
            scheduling_policy=policy,
        )
        payload_hash = sha256_digest(
            {
                **launch_payload,
                "solution_descriptors": solution_descriptors,
                "descriptor_hashes": descriptor_hashes,
            }
        )
        run_id = run_id_hint or sha256_digest({"nonce": uuid4().hex, "payload_hash": payload_hash})
        created_at = utc_now()
        manifest = RunManifest(
            schema_version="2",
            run_id=run_id,
            benchmark=pack.definition,
            dataset_revision=pack.dataset.revision,
            samples=sample_descriptors,
            solution_ids=selected_ids,
            solution_descriptors=solution_descriptors,
            evaluator=evaluator_descriptor,
            viewer=pack.viewer.descriptor,
            sample_ids=tuple(sample.id for sample in samples if sample.role is SampleRole.SCORED),
            created_at=created_at,
            networked_execution=networked,
            retry_limit=retry_limit,
            pricing_snapshots={
                solution_id: solutions_by_id[solution_id].pricing for solution_id in selected_ids
            },
            descriptor_hashes=descriptor_hashes,
            launch_idempotency_key_hash=key_hash,
            sample_snapshot_artifact_id=snapshot_id,
            retry_policy=retry_policy,
            scheduling_policy=policy,
        )
        root = self.lifecycle.create(
            run_id, "run", parent_id=dataset_id, accepted_at=manifest.created_at
        )
        if root.created_at != manifest.created_at:
            manifest = manifest.model_copy(update={"created_at": root.created_at})
        for sample in samples:
            for artifact in (sample.input, sample.reference):
                if artifact is not None:
                    self.lifecycle.attach(run_id, artifact.id)
        self.lifecycle.attach(run_id, snapshot_id)
        self._register_sample_artifacts(pack.dataset, samples)
        snapshot = self.artifacts.put_json(snapshot_value, metadata={"role": "sample-snapshot"})
        if snapshot.id != snapshot_id:
            raise RepositoryError("sample snapshot changed during immutable publication")
        self.repository.register_artifact(snapshot)
        self.lifecycle.attach(run_id, sha256_digest(manifest))
        staged_manifest = self.artifacts.put_json(
            manifest, metadata={"role": "manifest", "run_id": run_id}
        )
        self.repository.register_artifact(staged_manifest)
        intent_key = f"launch-intent:{run_id}"
        intent_values = {
            "manifest_artifact_id": staged_manifest.id,
            "key_hash": key_hash,
            "request_hash": request_hash,
        }
        for _ in range(20):
            root_row = self.backend.state.get(root_key(run_id))
            self.lifecycle.require_live(run_id, ready=False)
            assert root_row is not None
            try:
                self.backend.state.transact(
                    (
                        StateChange(intent_key, intent_values),
                        StateChange(root_row.key, root_row.values, root_row.etag),
                    )
                )
                break
            except StateConflictError:
                intent = self.backend.state.get(intent_key)
                if intent is None:
                    continue
                if intent.values != intent_values:
                    raise IdempotencyConflictError(
                        "immutable launch initialization intent conflicts"
                    ) from None
                break
        else:
            raise StateConflictError("launch initialization contention")
        if key_hash is not None and request_hash is not None:
            run_id = self.repository.reserve_launch(
                key_hash,
                request_hash,
                run_id,
                expected_tasks=len(manifest.sample_ids) * len(manifest.solution_ids),
            )
            existing = self.repository.get_run(run_id)
            if existing is not None:
                self.repair_initialization(existing.manifest)
                return existing.manifest
            if run_id != manifest.run_id:
                restored = self._restore_initialization(run_id)
                if restored is None:
                    raise RepositoryError("winning launch initialization intent is unavailable")
                return restored
        self.repository.put_benchmark(pack.definition.id, pack.definition.model_dump_json())
        self.repository.create_run(manifest, staged_manifest.id)
        persisted = self.repository.get_run(run_id)
        if persisted is None:
            raise RepositoryError("run initialization did not persist its manifest")
        self.repair_initialization(persisted.manifest)
        return persisted.manifest

    def _restore_initialization(self, run_id: str) -> RunManifest | None:
        self.assert_run_live(run_id)
        intent = self.backend.state.get(f"launch-intent:{run_id}")
        if intent is None:
            return None
        key_hash = intent.values["key_hash"]
        if key_hash is None and self.repository.get_run(run_id) is None:
            return None
        if key_hash is not None:
            reservation = self.repository.get_launch_reservation(str(key_hash))
            if (
                reservation is None
                or reservation.run_id != run_id
                or reservation.payload_hash != intent.values["request_hash"]
            ):
                return None
        artifact_id = str(intent.values["manifest_artifact_id"])
        manifest = RunManifest.model_validate_json(
            self.artifacts.get_bytes(artifact_id), strict=False
        )
        if manifest.run_id != run_id:
            raise RepositoryError("launch intent does not match its immutable manifest")
        self.repository.create_run(manifest, artifact_id)
        self.repair_initialization(manifest)
        return manifest

    def recover_initializations(self, *, limit: int = 10) -> int:
        page = self.backend.state.page(
            "launch-intent:", after=self._initialization_cursor, limit=limit
        )
        self._initialization_cursor = page.cursor
        recovered = 0
        for intent in page.rows:
            run_id = intent.key.removeprefix("launch-intent:")
            root = self.lifecycle.get(run_id)
            if (
                root is None
                or root.state == "deleting"
                or (root.expires_at is not None and root.expires_at <= self.lifecycle.clock())
            ):
                continue
            if self._restore_initialization(run_id) is not None:
                recovered += 1
        return recovered

    def repair_initialization(self, manifest: RunManifest) -> None:
        root = self.assert_run_live(manifest.run_id)
        if root is not None and root.archival:
            return
        samples = self._samples_for_manifest(manifest)
        for sample in samples:
            for artifact in (sample.input, sample.reference):
                if artifact is not None:
                    content = self.artifacts.get_bytes(artifact.id)
                    if (
                        content_digest(content) != artifact.id
                        or len(content) != artifact.size_bytes
                    ):
                        raise RepositoryError("persisted input/reference artifact is corrupt")
                    self.repository.register_artifact(artifact)
        self.repository.reconcile_tasks(manifest)
        if root is not None:
            self.lifecycle.publish(manifest.run_id)
        intent = self.backend.state.get(f"launch-intent:{manifest.run_id}")
        if intent is not None:
            with suppress(StateConflictError):
                self.backend.state.transact((StateChange(intent.key, None, intent.etag),))

    def assert_run_live(self, run_id: str) -> LifecycleRoot | None:
        if self.lifecycle.get(run_id) is None:
            if self.backend.name == "azure-table-blob":
                return self.lifecycle.require_live(run_id, ready=False)
            return None
        return self.lifecycle.require_live(run_id, ready=False)

    def artifact_has_live_owner(self, artifact_id: str) -> bool:
        return self.lifecycle.artifact_has_live_owner(artifact_id)

    def is_expired(self, identifier: str) -> bool:
        return self.lifecycle.is_expired(identifier)

    def get_artifact_bytes(self, artifact_id: str, media_type: str | None = None) -> bytes:
        if not self.artifact_has_live_owner(artifact_id):
            raise ArtifactNotFoundError("artifact has no live published owner")
        self.artifacts.get(artifact_id, media_type)
        content = self.artifacts.get_bytes(artifact_id)
        if not self.artifact_has_live_owner(artifact_id):
            raise ArtifactNotFoundError("artifact has no live published owner")
        return content

    def reap_expired(self, *, limit: int = 10) -> int:
        def cleanup(root: LifecycleRoot) -> bool:
            if root.kind == "run":
                if not isinstance(self.repository, SqliteRepository | AzureTableRepository):
                    raise TypeError("execution backend does not implement lifecycle cleanup")
                if not self.repository.cleanup_records(root):
                    return False
                intent = self.backend.state.get(f"launch-intent:{root.id}")
                if intent is not None:
                    self.backend.state.transact((StateChange(intent.key, None, intent.etag),))
                if not self.artifacts.delete_receipts(root.id, incarnation=root.incarnation):
                    return False
                directory = self.reports_root / root.id
                if directory.is_dir():
                    shutil.rmtree(directory)
            elif root.kind == "dataset":
                return self.datasets.cleanup_records(root)
            elif root.kind == "upload":
                return self.uploads.cleanup_records(root)
            return True

        completed = self.lifecycle.reap(cleanup, limit=limit)
        self.uploads.staging_lifecycle.reap(lambda root: True, limit=limit)
        if isinstance(self.repository, AzureTableRepository):
            self.repository.cleanup_cooldowns()
        return completed

    def landing_context(self) -> dict[str, str] | None:
        landing = self.backend.state.get("landing")
        if landing is None:
            return None
        run_id = str(landing.values["run_id"])
        root = self.lifecycle.require_live(run_id)
        if not root.pinned or not root.archival or self.repository.get_run(run_id) is None:
            raise RepositoryError("designated landing archive is unavailable")
        return {
            "benchmark_id": str(landing.values["benchmark_id"]),
            "run_id": run_id,
            "view": "evaluation",
        }

    def dataset_importer(self, benchmark_id: str) -> DatasetImporter:
        pack = get_pack(benchmark_id, self.pack_root)
        importer = getattr(pack, "dataset_importer", None)
        if importer is None:
            raise ValueError("this benchmark has no dataset import capability")
        return cast(DatasetImporter, importer)

    def sample_evidence(self, manifest: RunManifest) -> dict[str, tuple[ArtifactEvidence, ...]]:
        self.assert_run_live(manifest.run_id)
        if (
            manifest.sample_snapshot_artifact_id is None
            and self.backend.state.get(f"archive-run:{manifest.run_id}") is None
        ):
            return {}
        result: dict[str, tuple[ArtifactEvidence, ...]] = {}
        for sample in self._samples_for_manifest(manifest):
            evidence = [
                ArtifactEvidence(
                    role="input",
                    artifact_id=sample.input.id,
                    modality=sample.input.modality,
                    media_type=sample.input.media_type,
                    size_bytes=sample.input.size_bytes,
                )
            ]
            if sample.reference is not None:
                evidence.append(
                    ArtifactEvidence(
                        role="reference",
                        artifact_id=sample.reference.id,
                        modality=sample.reference.modality,
                        media_type=sample.reference.media_type,
                        size_bytes=sample.reference.size_bytes,
                    )
                )
            result[sample.id] = tuple(evidence)
        return result

    def run_to_completion(self, run_id: str, worker_id: str = "local-worker") -> RunReport:
        with self.backend.execution_session():
            existing = self.get_report(run_id)
            if existing is not None:
                return existing
            while not self.repository.acquire_supervisor(worker_id):
                existing = self.get_report(run_id)
                if existing is not None:
                    return existing
                time.sleep(self.repository.next_work_delay(run_id))
            with self.supervisor_session(worker_id) as admission_allowed:
                return self._run_to_completion(run_id, worker_id, admission_allowed)

    @contextmanager
    def supervisor_session(self, owner: str) -> Iterator[Callable[[], bool]]:
        if not self.repository.acquire_supervisor(owner):
            raise LeaseLostError("another supervisor owns execution")
        generation = self.repository.renew_supervisor(owner)
        if generation is None:
            raise LeaseLostError("supervisor expired before execution")
        stopped, lost = threading.Event(), threading.Event()
        failures: list[Exception] = []

        def admission_allowed() -> bool:
            if lost.is_set():
                return False
            try:
                if self.repository.renew_supervisor(owner, generation=generation) is None:
                    lost.set()
            except Exception as error:
                failures.append(error)
                lost.set()
            return not lost.is_set()

        def renew() -> None:
            while not stopped.wait(5):
                if not admission_allowed():
                    return

        heartbeat = threading.Thread(target=renew, name="ai-bench-sync-supervisor", daemon=True)
        heartbeat.start()
        try:
            yield admission_allowed
            if lost.is_set():
                raise LeaseLostError("synchronous supervisor renewal lost") from (
                    failures[0] if failures else None
                )
        finally:
            stopped.set()
            heartbeat.join()
            self.repository.release_supervisor(owner, generation=generation)

    def _run_to_completion(
        self, run_id: str, worker_id: str, admission_allowed: Callable[[], bool]
    ) -> RunReport:
        self.assert_run_live(run_id)
        run = self.repository.get_run(run_id)
        if run is None:
            raise RepositoryError(f"unknown run: {run_id}")
        if run.report_artifact_id:
            return RunReport.model_validate_json(
                self.artifacts.get_bytes(run.report_artifact_id), strict=False
            )
        pack = get_pack(run.manifest.benchmark.id, self.pack_root)
        self._assert_manifest_matches_pack(run.manifest, pack)
        self.repository.recover_expired_leases()
        self.repair_initialization(run.manifest)
        while any(
            task.status in {TaskStatus.PENDING, TaskStatus.LEASED}
            for task in self.repository.list_tasks(run_id)
        ):
            if not admission_allowed():
                raise LeaseLostError("synchronous supervisor renewal lost")
            if run.manifest.networked_execution and not self._network_execution_enabled():
                raise NetworkExecutionDisabledError(
                    "networked execution is currently disabled by "
                    "AI_BENCH_ALLOW_NETWORKED_SOLUTIONS"
                )
            self.repository.set_runtime_error(run_id, None)
            lease = self.repository.claim_task(worker_id, self.lease_seconds, run_id=run_id)
            if lease is None:
                self.repository.recover_expired_leases()
                time.sleep(self.repository.next_work_delay(run_id))
                continue
            try:
                self._execute_lease(lease, dispatch_allowed=admission_allowed)
            except LeaseLostError:
                continue
            except NetworkExecutionDisabledError:
                self.repository.release_unstarted(lease)
                raise
            except (AttemptPersistencePendingError, UncertainDispatchError):
                raise
            except Exception as error:
                self.repository.fail_attempt(lease, error)
        if not admission_allowed():
            raise LeaseLostError("synchronous supervisor renewal lost before finalization")
        return self._finalize(run_id)

    def get_report(self, run_id: str) -> RunReport | None:
        self.assert_run_live(run_id)
        run = self.repository.get_run(run_id)
        if run is None or run.report_artifact_id is None:
            return None
        return RunReport.model_validate_json(
            self.artifacts.get_bytes(run.report_artifact_id), strict=False
        )

    def get_report_bytes(self, run_id: str) -> bytes:
        self.assert_run_live(run_id)
        run = self.repository.get_run(run_id)
        if run is None or run.report_artifact_id is None:
            raise RepositoryError(f"run has no report: {run_id}")
        return self.artifacts.get_bytes(run.report_artifact_id)

    def export_report(self, run_id: str) -> tuple[Path, Path]:
        self.assert_run_live(run_id)
        report = self.get_report(run_id)
        if report is None:
            raise RepositoryError(f"run has no report: {run_id}")
        destination = self.reports_root / run_id
        json_path = destination / "report.json"
        csv_path = destination / "report.csv"
        archive = self.backend.state.get(f"archive-run:{run_id}")
        if archive is not None:
            from ai_bench.artifacts import _write_once

            marker = self.backend.state.get(f"archive:{archive.values['archive_digest']}")
            exports = None if marker is None else marker.values.get("exports")
            if not isinstance(exports, dict) or not {"report.json", "report.csv"}.issubset(exports):
                raise RepositoryError("archive has no original derived exports")
            destination.mkdir(parents=True, exist_ok=True)
            for name in ("report.json", "report.csv"):
                _write_once(
                    destination / name,
                    self.artifacts.get_bytes(str(exports[name])),
                    verify_existing=True,
                )
            return json_path, csv_path
        if json_path.is_file() and csv_path.is_file():
            return json_path, csv_path
        return write_report_files(report, self.reports_root)

    def prepare_execution(self, manifest: RunManifest, pack: BenchmarkPack) -> PreparedExecution:
        samples = self._samples_for_manifest(manifest, pack)
        self._assert_manifest_matches_pack(manifest, pack, samples)
        return PreparedExecution(manifest, pack, samples)

    def _execute_lease(
        self,
        lease: Lease,
        *,
        prepared: PreparedExecution | None = None,
        dispatch_allowed: Callable[[], bool] | None = None,
    ) -> None:
        with self.backend.execution_session():
            self._execute_lease_scoped(lease, prepared, dispatch_allowed)

    def _execute_lease_scoped(
        self,
        lease: Lease,
        prepared: PreparedExecution | None = None,
        dispatch_allowed: Callable[[], bool] | None = None,
    ) -> None:
        self.assert_run_live(lease.task.run_id)
        run = self._persist(lambda: self.repository.get_run(lease.task.run_id))
        if run is None:
            raise RepositoryError(f"run disappeared: {lease.task.run_id}")
        if prepared is None:
            prepared = self.prepare_execution(
                run.manifest, get_pack(run.manifest.benchmark.id, self.pack_root)
            )
        elif prepared.manifest != run.manifest:
            raise RepositoryError("prepared execution does not match the leased manifest")
        else:
            self._assert_manifest_matches_pack(run.manifest, prepared.pack, prepared.samples)
        pack, samples = prepared.pack, prepared.samples
        sample = next(sample for sample in samples if sample.id == lease.task.sample_id)
        solution = next(
            solution for solution in pack.solutions if solution.id == lease.task.solution_id
        )
        if solution.networked and (
            not run.manifest.networked_execution
            or not self._network_execution_enabled()
            or (self.backend.name == "azure-table-blob" and not self.backend.live_proof)
        ):
            raise NetworkExecutionDisabledError(
                "networked execution requires both manifest authorization and the "
                "current AI_BENCH_ALLOW_NETWORKED_SOLUTIONS=true runtime setting"
            )
        with self._lease_heartbeat(lease):
            request = ExecutionRequest(
                run_id=run.manifest.run_id,
                task_id=lease.task.id,
                sample=sample.model_copy(update={"reference": None, "metadata": {}}),
                idempotency_key=lease.task.id,
                input_bytes=self._persist(lambda: self.artifacts.get_bytes(sample.input.id)),
                attempt_number=lease.attempt_number,
                retry_policy=run.manifest.retry_policy,
            )
            output, telemetry, raw = self._execute_response(
                lease, solution, request, dispatch_allowed=dispatch_allowed
            )
            self._persist(lambda: self.repository.record_attempt_telemetry(lease, telemetry))
            if isinstance(output, ProviderFailure):
                if raw is not None:
                    self._record(lease, raw, "raw")
                self._persist(
                    lambda: self.repository.record_attempt_failure(lease, output.evidence)
                )
                raise output
            assert raw is not None
            self._record(lease, raw, "raw")
            parsed = solution.parse(output)
            parsed_artifact = self._persist(
                lambda: self._owned_json(
                    lease.task.run_id,
                    parsed.value,
                    metadata={"role": "parsed", "task_id": lease.task.id},
                )
            )
            self._record(lease, parsed_artifact, "parsed")
            evaluation = pack.evaluator.evaluate(sample, parsed)
            comparison = self._persist(
                lambda: self._owned_json(
                    lease.task.run_id,
                    evaluation.comparison,
                    metadata={"role": "comparison", "task_id": lease.task.id},
                )
            )
            self._record(lease, comparison, "comparison")
            evidence = [
                ArtifactEvidence(
                    role="input",
                    artifact_id=sample.input.id,
                    modality=sample.input.modality,
                    media_type=sample.input.media_type,
                    size_bytes=sample.input.size_bytes,
                ),
                ArtifactEvidence(
                    role="raw",
                    artifact_id=raw.id,
                    modality=raw.modality,
                    media_type=raw.media_type,
                    size_bytes=raw.size_bytes,
                    attempt_number=lease.attempt_number,
                ),
                ArtifactEvidence(
                    role="parsed",
                    artifact_id=parsed_artifact.id,
                    modality=parsed_artifact.modality,
                    media_type=parsed_artifact.media_type,
                    size_bytes=parsed_artifact.size_bytes,
                    attempt_number=lease.attempt_number,
                ),
                ArtifactEvidence(
                    role="comparison",
                    artifact_id=comparison.id,
                    modality=comparison.modality,
                    media_type=comparison.media_type,
                    size_bytes=comparison.size_bytes,
                    attempt_number=lease.attempt_number,
                ),
            ]
            if sample.reference is not None:
                evidence.insert(
                    1,
                    ArtifactEvidence(
                        role="reference",
                        artifact_id=sample.reference.id,
                        modality=sample.reference.modality,
                        media_type=sample.reference.media_type,
                        size_bytes=sample.reference.size_bytes,
                    ),
                )
            preliminary = TaskResult(
                task_id=lease.task.id,
                sample_id=sample.id,
                solution_id=solution.id,
                status=TaskStatus.SUCCEEDED,
                attempts=lease.attempt_number,
                metrics=tuple(evaluation.metrics),
                evidence=tuple(evidence),
                telemetry=telemetry,
            )
            result_artifact = self._persist(
                lambda: self._owned_json(
                    lease.task.run_id,
                    preliminary,
                    metadata={"role": "result", "task_id": lease.task.id},
                )
            )
            self._record(lease, result_artifact, "result")
            final_result = preliminary.model_copy(
                update={
                    "evidence": (
                        *preliminary.evidence,
                        ArtifactEvidence(
                            role="result",
                            artifact_id=result_artifact.id,
                            modality=result_artifact.modality,
                            media_type=result_artifact.media_type,
                            size_bytes=result_artifact.size_bytes,
                            attempt_number=lease.attempt_number,
                        ),
                    )
                }
            )
        self._persist(lambda: self.repository.complete_attempt(lease, final_result))

    def _owned_json(
        self, run_id: str, value: object, *, metadata: dict[str, JsonValue]
    ) -> Artifact:
        root = self.assert_run_live(run_id)
        if root is not None:
            self.lifecycle.attach(run_id, sha256_digest(value))
        elif self.repository.get_run(run_id) is None:
            raise RepositoryError("a missing run cannot publish evidence")
        return self.artifacts.put_json(value, metadata=metadata)

    @staticmethod
    def _persist[T](operation: Callable[[], T]) -> T:
        for index in range(5):
            try:
                return operation()
            except (LeaseLostError, UncertainDispatchError):
                raise
            except Exception as error:
                if index == 4:
                    raise AttemptPersistencePendingError(
                        "evidence persistence is pending; do not repeat the physical request"
                    ) from error
                time.sleep(0.1 * 2**index)
        raise AssertionError("unreachable persistence retry loop")

    def _execute_response(
        self,
        lease: Lease,
        solution: SolutionAdapter,
        request: ExecutionRequest,
        *,
        dispatch_allowed: Callable[[], bool] | None = None,
    ) -> tuple[ExecutionOutput | ProviderFailure, TelemetryEvidence, Artifact | None]:
        repository = self.repository
        if not isinstance(repository, ResponseReceiptRepository):
            raise RepositoryError("execution backend has no response-checkpoint contract")
        retained = self._persist(lambda: self.assert_run_live(lease.task.run_id))
        receipt_id = self._persist(lambda: repository.response_receipt(lease))
        if receipt_id is None:
            recovered_bytes = self._persist(
                lambda: self.artifacts.get_receipt(
                    lease.task.run_id,
                    lease.task.id,
                    lease.attempt_number,
                    incarnation=lease.retention_incarnation,
                )
            )
            if recovered_bytes is not None:
                receipt_id = self._persist(
                    lambda: materialize_receipt(
                        self.artifacts,
                        self.lifecycle,
                        self.repository.register_artifact,
                        lease.task.run_id,
                        lease.task.id,
                        lease.attempt_number,
                        recovered_bytes,
                    )
                )

                def recover_receipt() -> None:
                    assert receipt_id is not None and recovered_bytes is not None
                    repository.record_response_receipt(lease, receipt_id)

                self._persist(recover_receipt)
        if receipt_id is not None:
            checkpoint = ProviderReceipt.model_validate_json(
                self._persist(lambda: self.artifacts.get_bytes(receipt_id)), strict=False
            )
            if (
                checkpoint.run_id not in {None, lease.task.run_id}
                or checkpoint.task_id not in {None, lease.task.id}
                or checkpoint.attempt_number not in {None, lease.attempt_number}
            ):
                raise RepositoryError("response checkpoint belongs to another attempt")
            raw_bytes = (
                None
                if checkpoint.raw is None
                else self._persist(lambda: self.artifacts.get_bytes(checkpoint.raw.id))  # type: ignore[union-attr]
            )
            if checkpoint.failure is not None:
                return (
                    ProviderFailure(
                        checkpoint.failure,
                        raw=raw_bytes,
                        media_type="application/json"
                        if checkpoint.raw is None
                        else checkpoint.raw.media_type,
                    ),
                    checkpoint.telemetry,
                    checkpoint.raw,
                )
            if raw_bytes is None or checkpoint.raw is None:
                raise RepositoryError("successful response checkpoint omitted its raw object")
            return (
                ExecutionOutput(
                    raw=raw_bytes,
                    modality=checkpoint.raw.modality,
                    media_type=checkpoint.raw.media_type,
                    usage=checkpoint.telemetry.usage,
                    provider_request_id=checkpoint.telemetry.provider_request_id,
                    provider_latency_ms=checkpoint.telemetry.latency_ms,
                ),
                checkpoint.telemetry,
                checkpoint.raw,
            )
        if (dispatch_allowed is not None and not dispatch_allowed()) or (
            solution.networked and not self._network_execution_enabled()
        ):
            raise NetworkExecutionDisabledError("execution admission closed before dispatch")
        self.backend.require_execution_ready()
        if solution.networked:
            # This is a durable uncertainty checkpoint, not an exactly-once or
            # physical non-overlap guarantee across a paused process.
            self._persist(lambda: repository.begin_dispatch(lease))
        started = datetime.now(UTC)
        timer = time.perf_counter()
        response: ExecutionOutput | ProviderFailure
        try:
            response = solution.execute(request)
        except ProviderFailure as error:
            response = error
        except Exception as error:
            if solution.networked:
                raise UncertainDispatchError("provider request has no recorded response") from error
            raise
        finished = datetime.now(UTC)
        if (
            retained is not None
            and retained.expires_at is not None
            and self.lifecycle.clock() >= retained.expires_at
        ):
            raise LeaseLostError("run expired while its provider request was in flight")
        if response.raw is not None and len(response.raw) > 64 * 1024 * 1024:
            original_usage = (
                response.evidence.usage if isinstance(response, ProviderFailure) else response.usage
            )
            response = ProviderFailure(
                ProviderFailureEvidence(
                    classification="invalid-response",
                    message="Provider response exceeded the platform's bounded raw-response size.",
                    retryable=False,
                    usage=original_usage,
                    response_truncated=True,
                ),
                raw=response.raw[: 1024 * 1024],
                modality=response.modality,
                media_type=response.media_type,
            )
        failure = response.evidence if isinstance(response, ProviderFailure) else None
        usage = failure.usage if failure is not None else cast(ExecutionOutput, response).usage
        latency = (
            failure.provider_latency_ms
            if failure is not None
            else cast(ExecutionOutput, response).provider_latency_ms
        )
        telemetry = TelemetryEvidence(
            started_at=started,
            finished_at=finished,
            latency_ms=(
                latency
                if latency is not None or failure is not None
                else Decimal(str((time.perf_counter() - timer) * 1000))
            ),
            usage=usage,
            cost=solution.pricing.estimate(usage),
            provider_request_id=(
                failure.provider_request_id
                if failure is not None
                else cast(ExecutionOutput, response).provider_request_id
            ),
        )
        raw = None
        if response.raw is not None:
            content = response.raw
            raw = Artifact(
                id=content_digest(content),
                size_bytes=len(content),
                modality=response.modality,
                media_type=response.media_type,
                uri=f"artifact://sha256/{content_digest(content)}",
                metadata={
                    "role": "raw",
                    "task_id": lease.task.id,
                    **({"redacted": failure.response_redacted} if failure is not None else {}),
                },
            )
            if failure is not None:
                failure = failure.model_copy(update={"raw_artifact_id": raw.id})
                cast(ProviderFailure, response).evidence = failure
        checkpoint = ProviderReceipt(
            run_id=lease.task.run_id,
            task_id=lease.task.id,
            attempt_number=lease.attempt_number,
            raw=raw,
            telemetry=telemetry,
            failure=failure,
        )
        encoded = encode_receipt(checkpoint, response.raw)
        self._persist(
            lambda: self.artifacts.put_receipt(
                lease.task.run_id,
                lease.task.id,
                lease.attempt_number,
                encoded,
                incarnation=lease.retention_incarnation,
            )
        )

        def persist_receipt() -> None:
            artifact_id = materialize_receipt(
                self.artifacts,
                self.lifecycle,
                self.repository.register_artifact,
                lease.task.run_id,
                lease.task.id,
                lease.attempt_number,
                encoded,
            )
            repository.record_response_receipt(lease, artifact_id)

        self._persist(persist_receipt)
        return response, telemetry, raw

    @contextmanager
    def _lease_heartbeat(self, lease: Lease) -> Iterator[None]:
        stopped = threading.Event()
        failures: list[Exception] = []

        def renew() -> None:
            interval = max(0.1, self.lease_seconds / 3)
            while not stopped.wait(interval):
                try:
                    self.repository.renew_lease(lease, self.lease_seconds)
                except Exception as error:
                    failures.append(error)
                    return

        heartbeat = threading.Thread(target=renew, daemon=True)
        heartbeat.start()
        try:
            yield
        finally:
            stopped.set()
            heartbeat.join(timeout=1)
        if failures:
            raise LeaseLostError(lease.task.id) from failures[0]
        self.repository.renew_lease(lease, self.lease_seconds)

    def _record(self, lease: Lease, artifact: object, role: str) -> None:
        from ai_bench.contracts import Artifact

        if not isinstance(artifact, Artifact):
            raise TypeError("expected artifact")
        evidence = ArtifactEvidence(
            role=role,  # type: ignore[arg-type]
            artifact_id=artifact.id,
            modality=artifact.modality,
            media_type=artifact.media_type,
            size_bytes=artifact.size_bytes,
            attempt_number=lease.attempt_number,
        )

        def persist_evidence() -> None:
            self.repository.register_artifact(artifact)
            if self.lifecycle.get(lease.task.run_id) is not None:
                self.lifecycle.attach(lease.task.run_id, artifact.id)
            self.repository.record_attempt_artifact(lease, evidence)

        self._persist(persist_evidence)

    def _finalize(self, run_id: str) -> RunReport:
        self.assert_run_live(run_id)
        run = self.repository.get_run(run_id)
        if run is None:
            raise RepositoryError(run_id)
        results = tuple(
            task.result for task in self.repository.list_tasks(run_id) if task.result is not None
        )
        attempt_history = {
            result.task_id: tuple(self.repository.list_attempts(result.task_id))
            for result in results
        }
        token = self.repository.acquire_finalizer(run_id)
        if token is None:
            existing = self.get_report(run_id)
            if existing is None:
                raise RepositoryError("run is not ready for finalization")
            return existing
        pack = get_pack(run.manifest.benchmark.id, self.pack_root)
        self._assert_manifest_matches_pack(run.manifest, pack)
        report = build_report(
            run_id,
            run.manifest_artifact_id,
            results,
            self.artifacts,
            attempt_history,
            pack.evaluator,
            run.manifest.solution_descriptors,
            created_at=run.manifest.created_at,
            expected_sample_ids=run.manifest.sample_ids,
        )
        if self.lifecycle.get(run_id) is not None:
            self.lifecycle.attach(run_id, sha256_digest(report))
        report_artifact = self.artifacts.put_json(
            report, metadata={"role": "report", "run_id": run_id}
        )
        self.repository.register_artifact(report_artifact)
        if self.lifecycle.get(run_id) is not None:
            self.lifecycle.attach(run_id, report_artifact.id)
        try:
            self.repository.finalize_run(run_id, token, report.status, report_artifact.id)
        except FinalizerFenceError:
            return self._wait_for_winning_report(run_id)
        write_report_files(report, self.reports_root)
        return report

    def _wait_for_winning_report(self, run_id: str, timeout_seconds: float = 30) -> RunReport:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            report = self.get_report(run_id)
            if report is not None:
                return report
            time.sleep(0.1)
        raise RepositoryError(
            f"winning finalizer did not publish an immutable report for run {run_id}"
        )

    def _network_execution_enabled(self) -> bool:
        return os.getenv("AI_BENCH_ALLOW_NETWORKED_SOLUTIONS", "false").lower() == "true" and (
            self.backend.name != "azure-table-blob" or self.backend.live_proof
        )

    @staticmethod
    def _descriptor_hashes(
        pack: BenchmarkPack,
        sample_descriptors: tuple[SampleDescriptor, ...],
        solution_descriptors: dict[str, SolutionDescriptor],
        evaluator_descriptor: EvaluatorDescriptor,
        *,
        dataset_revision: DatasetRevision | None = None,
        snapshot_id: str | None = None,
        retry_policy: RetryPolicy | None = None,
        scheduling_policy: SchedulingPolicy | None = None,
        legacy: bool = False,
    ) -> dict[str, str]:
        return {
            "benchmark": sha256_digest(pack.definition),
            "dataset": sha256_digest(
                {
                    "revision": dataset_revision or pack.dataset.revision,
                    "samples": sample_descriptors,
                }
            ),
            "evaluator": sha256_digest(evaluator_descriptor),
            "viewer": sha256_digest(pack.viewer.descriptor),
            **{
                f"solution:{solution_id}": sha256_digest(
                    descriptor.model_dump(mode="json", exclude={"settings", "throttle_scope"})
                    if legacy
                    else descriptor
                )
                for solution_id, descriptor in solution_descriptors.items()
            },
            **{
                f"pricing:{solution_id}": sha256_digest(descriptor.pricing_snapshot)
                for solution_id, descriptor in solution_descriptors.items()
            },
            **({"sample-snapshot": snapshot_id} if snapshot_id is not None else {}),
            **({"retry-policy": sha256_digest(retry_policy)} if retry_policy is not None else {}),
            **(
                {"scheduling-policy": sha256_digest(scheduling_policy)}
                if scheduling_policy is not None
                else {}
            ),
        }

    @staticmethod
    def _solution_descriptor(solution: SolutionAdapter) -> SolutionDescriptor:
        return SolutionDescriptor(
            id=solution.id,
            version=solution.version,
            pipeline=solution.pipeline,
            compatibility=solution.compatibility,
            pricing_snapshot=solution.pricing,
            settings=solution.execution_settings()
            if isinstance(solution, ConfiguredSolution)
            else {},
            throttle_scope=solution.throttle_scope
            if isinstance(solution, ConfiguredSolution)
            else None,
        )

    def _samples_for_manifest(
        self,
        manifest: RunManifest,
        pack: BenchmarkPack | None = None,
    ) -> tuple[BenchmarkSample, ...]:
        if manifest.sample_snapshot_artifact_id is None:
            archive = self.backend.state.get(f"archive-run:{manifest.run_id}")
            if archive is not None:
                marker = self.backend.state.get(f"archive:{archive.values['archive_digest']}")
                if marker is None:
                    raise RepositoryError("archive publication marker is missing")
                return tuple(
                    self.datasets.resolve(
                        str(marker.values["dataset_id"]), manifest.benchmark.id
                    ).samples()
                )
            legacy_pack = pack or get_pack(manifest.benchmark.id, self.pack_root)
            samples = tuple(legacy_pack.dataset.samples())
            self._register_sample_artifacts(legacy_pack.dataset, samples)
            return samples
        content = self.artifacts.get_bytes(manifest.sample_snapshot_artifact_id)
        snapshot = SampleSnapshot.model_validate_json(content)
        descriptors = tuple(
            SampleDescriptor(
                id=sample.id,
                role=sample.role,
                input_artifact_id=sample.input.id,
                reference_artifact_id=None if sample.reference is None else sample.reference.id,
            )
            for sample in snapshot.samples
        )
        if (
            content_digest(content) != manifest.sample_snapshot_artifact_id
            or snapshot.dataset_revision != manifest.dataset_revision
            or sha256_digest(snapshot.samples) != manifest.dataset_revision.content_hash
            or descriptors != manifest.samples
            or tuple(sample.id for sample in snapshot.samples if sample.role is SampleRole.SCORED)
            != manifest.sample_ids
        ):
            raise RepositoryError("persisted sample snapshot does not match the immutable manifest")
        return snapshot.samples

    def _assert_manifest_matches_pack(
        self,
        manifest: RunManifest,
        pack: BenchmarkPack,
        samples: tuple[BenchmarkSample, ...] | None = None,
    ) -> None:
        current_samples = samples or self._samples_for_manifest(manifest, pack)
        sample_descriptors = tuple(
            SampleDescriptor(
                id=sample.id,
                role=sample.role,
                input_artifact_id=sample.input.id,
                reference_artifact_id=(None if sample.reference is None else sample.reference.id),
            )
            for sample in current_samples
        )
        solutions_by_id = {solution.id: solution for solution in pack.solutions}
        missing = set(manifest.solution_ids) - set(solutions_by_id)
        if missing:
            raise RepositoryError(
                "benchmark pack descriptor drift: missing pinned solutions "
                + ", ".join(sorted(missing))
            )
        solution_descriptors = {
            solution_id: self._solution_descriptor(solutions_by_id[solution_id])
            for solution_id in manifest.solution_ids
        }
        for solution_id in manifest.solution_ids:
            solution = solutions_by_id[solution_id]
            if isinstance(solution, ConfiguredSolution):
                solution.validate_execution()
        evaluator = EvaluatorDescriptor(
            id=pack.evaluator.id,
            version=pack.evaluator.version,
            metrics=tuple(pack.evaluator.metric_definitions),
        )
        current_hashes = self._descriptor_hashes(
            pack,
            sample_descriptors,
            solution_descriptors,
            evaluator,
            dataset_revision=manifest.dataset_revision if manifest.schema_version == "2" else None,
            snapshot_id=manifest.sample_snapshot_artifact_id,
            retry_policy=manifest.retry_policy if manifest.schema_version == "2" else None,
            scheduling_policy=(
                scheduling_policy(pack) if manifest.scheduling_policy is not None else None
            ),
            legacy=manifest.schema_version == "1",
        )
        drifted = sorted(
            key
            for key in set(manifest.descriptor_hashes) | set(current_hashes)
            if manifest.descriptor_hashes.get(key) != current_hashes.get(key)
        )
        if drifted:
            raise RepositoryError(
                "benchmark pack descriptor drift prevents resume: " + ", ".join(drifted)
            )

    def _register_sample_artifacts(
        self,
        dataset: DatasetAdapter,
        samples: tuple[BenchmarkSample, ...],
    ) -> None:
        for sample in samples:
            for artifact in (sample.input, sample.reference):
                if artifact is None:
                    continue
                content = dataset.read_artifact(artifact)
                if not isinstance(content, bytes):
                    raise TypeError(f"dataset read_artifact must return bytes: {artifact.id}")
                if len(content) != artifact.size_bytes:
                    raise ValueError(f"artifact size mismatch: {artifact.id}")
                if content_digest(content) != artifact.id:
                    raise ValueError(f"artifact content hash mismatch: {artifact.id}")
                stored = self.artifacts.put_bytes(
                    content,
                    modality=artifact.modality,
                    media_type=artifact.media_type,
                    metadata=dict(artifact.metadata),
                )
                if not isinstance(stored, Artifact) or stored.id != artifact.id:
                    raise RepositoryError(
                        f"artifact store returned an invalid identity: {artifact.id}"
                    )
                self.repository.register_artifact(stored)
