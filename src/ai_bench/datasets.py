"""Generic durable dataset publication and catalog selection."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Literal, cast

from ai_bench.artifacts import ArtifactStore
from ai_bench.azure_artifacts import AzureBlobArtifactStore
from ai_bench.contracts import (
    ArtifactEvidence,
    BenchmarkSample,
    Contract,
    DatasetAvailability,
    DatasetRevision,
    JsonValue,
    ModalityArtifact,
    SampleRole,
    SampleSnapshot,
)
from ai_bench.dataset_imports import DatasetImportResult
from ai_bench.hashing import content_digest, sha256_digest
from ai_bench.lifecycle import (
    RETENTION,
    ExpiredRootError,
    LifecycleRoot,
    LifecycleService,
    RootUnavailableError,
    _expiry_key,
    root_key,
)
from ai_bench.storage_state import StateChange, StateConflictError, StateStore


class DatasetRecord(Contract):
    id: str
    benchmark_id: str
    revision: DatasetRevision
    snapshot_artifact_id: str
    sample_count: int
    scored_sample_count: int
    published_at: datetime
    expires_at: datetime | None
    installed: bool = False
    pinned: bool = False
    name: str = ""


class DatasetPage(Contract):
    items: tuple[DatasetRecord, ...]
    next_cursor: str | None = None


class CatalogDataset:
    def __init__(
        self, record: DatasetRecord, snapshot: SampleSnapshot, artifacts: ArtifactStore
    ) -> None:
        self.id = record.id
        self.revision = record.revision
        self._snapshot = snapshot
        self._artifacts = artifacts

    def samples(self) -> Sequence[BenchmarkSample]:
        return self._snapshot.samples

    def read_artifact(self, artifact: ModalityArtifact) -> bytes:
        return self._artifacts.get_bytes(artifact.id)

    def snapshot_metadata(self) -> Mapping[str, JsonValue]:
        return self._snapshot.metadata

    def availability(self) -> DatasetAvailability:
        return DatasetAvailability(
            status="ready",
            reason="Complete immutable catalog publication.",
            revision=self.revision,
            sample_count=len(self._snapshot.samples),
            scored_sample_count=sum(
                sample.role is SampleRole.SCORED for sample in self._snapshot.samples
            ),
        )


class DatasetCatalog:
    def __init__(
        self, state: StateStore, artifacts: ArtifactStore, lifecycle: LifecycleService
    ) -> None:
        self.state = state
        self.artifacts = artifacts
        self.lifecycle = lifecycle

    def get(self, dataset_id: str) -> DatasetRecord:
        root = self.lifecycle.require_live(dataset_id)
        row = self.state.get(f"dataset:{dataset_id}")
        if row is None:
            raise RootUnavailableError(dataset_id)
        record = DatasetRecord.model_validate(row.values, strict=False)
        return record.model_copy(update={"pinned": root.pinned, "expires_at": root.expires_at})

    def list(
        self, benchmark_id: str, *, cursor: str | None = None, limit: int = 100
    ) -> DatasetPage:
        page = self.state.page(
            f"dataset-index:{sha256_digest(benchmark_id)}:", after=cursor, limit=limit
        )
        records = []
        for row in page.rows:
            try:
                records.append(self.get(str(row.values["dataset_id"])))
            except (ExpiredRootError, RootUnavailableError):
                continue
        return DatasetPage(items=tuple(records), next_cursor=page.cursor)

    def resolve(self, dataset_id: str, benchmark_id: str) -> CatalogDataset:
        record = self.get(dataset_id)
        if record.benchmark_id != benchmark_id:
            raise ValueError("selected dataset belongs to another benchmark")
        snapshot = SampleSnapshot.model_validate_json(
            self.artifacts.get_bytes(record.snapshot_artifact_id), strict=False
        )
        if snapshot.dataset_revision != record.revision:
            raise ValueError("catalog snapshot/revision mismatch")
        return CatalogDataset(record, snapshot, self.artifacts)

    def sample_evidence(self, dataset_id: str) -> dict[str, tuple[ArtifactEvidence, ...]]:
        record = self.get(dataset_id)
        dataset = self.resolve(dataset_id, record.benchmark_id)
        return {
            sample.id: tuple(
                ArtifactEvidence(
                    role=cast(Literal["input", "reference"], role),
                    artifact_id=artifact.id,
                    modality=artifact.modality,
                    media_type=artifact.media_type,
                    size_bytes=artifact.size_bytes,
                )
                for role, artifact in (("input", sample.input), ("reference", sample.reference))
                if artifact is not None
            )
            for sample in dataset.samples()
        }

    def publish(
        self,
        benchmark_id: str,
        operation_id: str,
        result: DatasetImportResult,
        *,
        publication_fences: Callable[[], tuple[StateChange, ...]] | None = None,
        name: str | None = None,
        staging_expires_at: datetime | None = None,
    ) -> DatasetRecord:
        """Replays repair the same hidden publication; paths never enter stored metadata."""
        dataset_id = sha256_digest(
            {"benchmark_id": benchmark_id, "publication_operation": operation_id}
        )
        if result.revision.content_hash != sha256_digest(result.samples):
            raise ValueError("importer revision hash does not match its sample snapshot")
        existing = self.state.get(f"dataset:{dataset_id}")
        if existing is not None:
            record = self.get(dataset_id)
            if record.revision != result.revision:
                raise ValueError("publication operation reused with different content")
            return record
        root = self.lifecycle.create(dataset_id, "dataset", expires_not_after=staging_expires_at)
        snapshot = SampleSnapshot(
            dataset_revision=result.revision, samples=result.samples, metadata=result.metadata
        )
        sample_ids = [sample.id for sample in result.samples]
        if len(sample_ids) != len(set(sample_ids)) or not any(
            sample.role is SampleRole.SCORED for sample in result.samples
        ):
            raise ValueError("a dataset must have unique sample IDs and a scored sample")
        for sample in result.samples:
            for descriptor in (sample.input, sample.reference):
                if descriptor is None:
                    continue
                path = result.files.get(descriptor.id)
                if path is None:
                    raise ValueError("importer omitted an input/reference object")
                self.lifecycle.attach(dataset_id, descriptor.id)
                if isinstance(self.artifacts, AzureBlobArtifactStore):
                    self.artifacts.put_file(path, descriptor)
                else:
                    content = path.read_bytes()
                    if (
                        len(content) != descriptor.size_bytes
                        or content_digest(content) != descriptor.id
                    ):
                        raise ValueError("importer file does not match its immutable descriptor")
                    self.artifacts.put_bytes(
                        content,
                        modality=descriptor.modality,
                        media_type=descriptor.media_type,
                        metadata=dict(descriptor.metadata),
                    )
        self.lifecycle.attach(dataset_id, sha256_digest(snapshot))
        stored = self.artifacts.put_json(snapshot, metadata={"role": "sample-snapshot"})
        self.lifecycle.attach(dataset_id, stored.id)
        record = DatasetRecord(
            id=dataset_id,
            benchmark_id=benchmark_id,
            revision=result.revision,
            snapshot_artifact_id=stored.id,
            sample_count=len(result.samples),
            scored_sample_count=sum(sample.role is SampleRole.SCORED for sample in result.samples),
            published_at=root.created_at,
            expires_at=root.expires_at,
            name=name or result.revision.id,
        )
        for _ in range(20):
            root_row = self.state.get(root_key(dataset_id))
            current = self.lifecycle.require_live(dataset_id, ready=False)
            assert root_row is not None
            published_at = self.lifecycle.clock()
            generation, generation_change = self.lifecycle._root_generation()
            ready = current.model_copy(
                update={
                    "state": "ready",
                    "generation": generation,
                    "created_at": published_at,
                    "expires_at": published_at + RETENTION,
                }
            )
            record = record.model_copy(
                update={
                    "published_at": ready.created_at,
                    "expires_at": ready.expires_at,
                }
            )
            changes = [
                generation_change,
                StateChange(root_row.key, ready.model_dump(mode="json"), root_row.etag),
                StateChange(f"dataset:{dataset_id}", record.model_dump(mode="json")),
                StateChange(
                    f"dataset-index:{sha256_digest(benchmark_id)}:{dataset_id}",
                    {"dataset_id": dataset_id},
                ),
            ]
            if current.expires_at is not None:
                previous_expiry = self.state.get(_expiry_key(current))
                if previous_expiry is not None and _expiry_key(current) != _expiry_key(ready):
                    changes.extend(
                        (
                            StateChange(previous_expiry.key, None, previous_expiry.etag),
                            StateChange(_expiry_key(ready), {"root_id": dataset_id}),
                        )
                    )
            if publication_fences is not None:
                changes.extend(publication_fences())
            try:
                self.state.transact(changes)
                return record
            except StateConflictError:
                if self.state.get(f"dataset:{dataset_id}") is not None:
                    return self.get(dataset_id)
        raise StateConflictError("dataset publication contention")

    def cleanup_records(self, root: LifecycleRoot) -> bool:
        guard = self.lifecycle.cleanup_guard(root)
        if guard is None:
            return False
        row = self.state.get(f"dataset:{root.id}")
        if row is None:
            return True
        record = DatasetRecord.model_validate(row.values, strict=False)
        index = self.state.get(f"dataset-index:{sha256_digest(record.benchmark_id)}:{record.id}")
        changes = [StateChange(row.key, None, row.etag), guard]
        if index is not None:
            changes.append(StateChange(index.key, None, index.etag))
        try:
            self.state.transact(changes)
        except StateConflictError:
            return False
        return True
