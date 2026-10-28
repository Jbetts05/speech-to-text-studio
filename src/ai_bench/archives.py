"""Operator-only, exact-byte terminal evidence transfer; never provider execution."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol, runtime_checkable

from pydantic import Field

from ai_bench.artifacts import ArtifactStore, FileArtifactStore, _write_once
from ai_bench.azure_artifacts import AzureBlobArtifactStore
from ai_bench.contracts import (
    Artifact,
    Contract,
    JsonValue,
    Modality,
    RunEvent,
    RunManifest,
    RunReport,
    RunStatus,
    SampleRole,
    SampleSnapshot,
    TaskAttempt,
    TaskStatus,
)
from ai_bench.datasets import DatasetRecord
from ai_bench.engine import BenchmarkEngine
from ai_bench.hashing import canonical_json, content_digest, sha256_digest, stable_task_id
from ai_bench.lifecycle import LifecycleRoot, _expiry_key, root_key
from ai_bench.packs import get_pack
from ai_bench.receipts import decode_receipt
from ai_bench.repositories import ExecutionRepository, RunRecord, SqliteRepository, TaskRecord
from ai_bench.storage_state import StateChange, StateConflictError, StateStore
from ai_bench.table_repository import AzureTableRepository

MAX_OBJECTS = 50_000
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_INDEX_BYTES = 32 * 1024 * 1024


@runtime_checkable
class PublicationClosure(Protocol):
    def publication_files(self) -> Mapping[str, Path]: ...


class ArchiveObject(Contract):
    artifact_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0, le=512 * 1024 * 1024)
    descriptors: tuple[Artifact, ...] = Field(min_length=1)


class ArchiveIndex(Contract):
    schema_version: Literal["1"] = "1"
    run: RunRecord
    tasks: tuple[TaskRecord, ...]
    attempts: Mapping[str, tuple[TaskAttempt, ...]]
    events: tuple[RunEvent, ...]
    snapshot_artifact_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    dataset_objects: tuple[str, ...] = ()
    operational_objects: tuple[str, ...] = ()
    receipts: Mapping[str, Mapping[str, str]] = Field(default_factory=dict)
    objects: tuple[ArchiveObject, ...]
    publication: Mapping[str, str] = Field(default_factory=dict)
    exports: Mapping[str, str] = Field(default_factory=dict)


def _objects_required(index: ArchiveIndex) -> set[str]:
    required = {index.run.manifest_artifact_id, index.snapshot_artifact_id}
    required.update(index.dataset_objects)
    required.update(index.operational_objects)
    required.update(digest for attempts in index.receipts.values() for digest in attempts.values())
    if index.run.report_artifact_id is not None:
        required.add(index.run.report_artifact_id)
    for sample in index.run.manifest.samples:
        required.add(sample.input_artifact_id)
        if sample.reference_artifact_id is not None:
            required.add(sample.reference_artifact_id)
    for task in index.tasks:
        if task.result is not None:
            required.update(item.artifact_id for item in task.result.evidence)
        for attempt in index.attempts.get(task.id, ()):
            required.update(item.artifact_id for item in attempt.evidence)
            if attempt.failure is not None and attempt.failure.raw_artifact_id is not None:
                required.add(attempt.failure.raw_artifact_id)
    required.update(index.publication.values())
    required.update(index.exports.values())
    return required


def _safe_logical_name(name: str) -> None:
    if (
        not name
        or len(name) > 200
        or name in {".", ".."}
        or any(character in name for character in "\\:")
        or any(ord(character) < 32 for character in name)
        or PurePosixPath(name).is_absolute()
        or any(part in {"", ".", ".."} for part in name.split("/"))
    ):
        raise ValueError("archive logical filenames must be portable relative paths")


def _object_path(bundle: Path, digest: str) -> Path:
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("invalid archive object identity")
    path = bundle / "objects" / digest
    if (
        path.is_symlink()
        or path.parent.is_symlink()
        or not path.resolve().is_relative_to(bundle.resolve())
    ):
        raise ValueError("archive object paths must not traverse links")
    return path


def validate_archive(bundle: Path, *, expected_digest: str | None = None) -> ArchiveIndex:
    index_path = bundle / "index.json"
    if (
        bundle.is_symlink()
        or index_path.is_symlink()
        or index_path.stat().st_size > MAX_INDEX_BYTES
    ):
        raise ValueError("invalid archive index path or size")
    index_bytes = index_path.read_bytes()
    if expected_digest is not None and content_digest(index_bytes) != expected_digest:
        raise ValueError("archive index does not match the reviewed digest")
    index = ArchiveIndex.model_validate_json(index_bytes, strict=False)
    if not 1 <= len(index.objects) <= MAX_OBJECTS:
        raise ValueError("invalid archive object count")
    objects = {item.artifact_id: item for item in index.objects}
    if len(objects) != len(index.objects):
        raise ValueError("duplicate archive object identity")
    if sum(item.size_bytes for item in index.objects) > MAX_ARCHIVE_BYTES:
        raise ValueError("archive exceeds the expanded-byte limit")
    for item in index.objects:
        path = _object_path(bundle, item.artifact_id)
        with path.open("rb") as stream:
            if (
                path.stat().st_size != item.size_bytes
                or hashlib.file_digest(stream, "sha256").hexdigest() != item.artifact_id
            ):
                raise ValueError("archive object failed its exact-byte identity check")
        for descriptor in item.descriptors:
            if descriptor.id != item.artifact_id or descriptor.size_bytes != item.size_bytes:
                raise ValueError("archive descriptor/object mismatch")
    if _objects_required(index) != set(objects):
        raise ValueError("archive evidence closure is incomplete or contains undeclared objects")
    manifest = RunManifest.model_validate_json(
        _object_path(bundle, index.run.manifest_artifact_id).read_bytes(), strict=False
    )
    if manifest != index.run.manifest:
        raise ValueError("original manifest bytes disagree with the archive projection")
    if index.run.status not in {
        RunStatus.COMPLETED,
        RunStatus.COMPLETED_WITH_FAILURES,
        RunStatus.FAILED,
    }:
        raise ValueError("only terminal runs may be archived")
    if index.run.report_artifact_id is None:
        raise ValueError("terminal archive must contain the original report")
    report = RunReport.model_validate_json(
        _object_path(bundle, index.run.report_artifact_id).read_bytes(), strict=False
    )
    if (
        report.run_id != manifest.run_id
        or report.manifest_artifact_id != index.run.manifest_artifact_id
        or report.status != index.run.status
    ):
        raise ValueError("original report/manifest identity mismatch")
    expected = {
        stable_task_id(manifest.run_id, sample_id, solution_id): (sample_id, solution_id)
        for sample_id in manifest.sample_ids
        for solution_id in manifest.solution_ids
    }
    if {task.id for task in index.tasks} != set(expected) or len(index.tasks) != len(expected):
        raise ValueError("archive must contain the complete immutable task matrix")
    if set(index.attempts) != set(expected):
        raise ValueError("archive attempt history does not match the task matrix")
    for task_id, receipts in index.receipts.items():
        if task_id not in expected:
            raise ValueError("archive response checkpoint belongs to another task")
        for number, digest in receipts.items():
            if number not in {str(attempt.attempt_number) for attempt in index.attempts[task_id]}:
                raise ValueError("archive response checkpoint has no recorded attempt")
            checkpoint, _, _ = decode_receipt(_object_path(bundle, digest).read_bytes())
            if (
                checkpoint.run_id not in {None, manifest.run_id}
                or checkpoint.task_id not in {None, task_id}
                or checkpoint.attempt_number not in {None, int(number)}
                or (checkpoint.raw is not None and checkpoint.raw.id not in objects)
            ):
                raise ValueError("archive response checkpoint identity or raw closure is invalid")
    report_results = {result.task_id: result for result in report.results}
    if set(report_results) != set(expected) or len(report.results) != len(expected):
        raise ValueError("original report does not contain the complete task matrix")
    for task in index.tasks:
        if (
            (task.sample_id, task.solution_id) != expected[task.id]
            or task.run_id != manifest.run_id
            or task.status not in {TaskStatus.SUCCEEDED, TaskStatus.POISONED}
            or task.result is None
            or report_results.get(task.id) != task.result
        ):
            raise ValueError("archive task/result projection mismatch")
        attempts = index.attempts[task.id]
        if [attempt.attempt_number for attempt in attempts] != list(range(1, task.attempts + 1)):
            raise ValueError("archive omitted or reordered an attempt")
        if any(attempt.task_id != task.id for attempt in attempts):
            raise ValueError("archive attempt belongs to another task")
        if report.attempt_history and report.attempt_history.get(task.id) != attempts:
            raise ValueError("original report attempt history mismatch")
    snapshot = SampleSnapshot.model_validate_json(
        _object_path(bundle, index.snapshot_artifact_id).read_bytes(), strict=False
    )
    if (
        snapshot.dataset_revision != manifest.dataset_revision
        or sha256_digest(snapshot.samples) != manifest.dataset_revision.content_hash
        or tuple(sample.id for sample in snapshot.samples if sample.role is SampleRole.SCORED)
        != manifest.sample_ids
    ):
        raise ValueError("archive sample snapshot does not match the immutable revision")
    snapshot_ids = {
        artifact.id
        for sample in snapshot.samples
        for artifact in (sample.input, sample.reference)
        if artifact is not None
    }
    if not snapshot_ids.issubset(objects):
        raise ValueError("archive omitted snapshot inputs or references")
    sequences = [event.sequence for event in index.events]
    if sequences != sorted(set(sequences)) or any(
        event.run_id != manifest.run_id for event in index.events
    ):
        raise ValueError("archive events contain conflicting cursors")
    for name in (*index.publication, *index.exports):
        _safe_logical_name(name)
    if set(index.exports) - {"report.json", "report.csv"}:
        raise ValueError("archive exports must use the two declared report filenames")
    return index


def _descriptors(store: ArtifactStore, artifact_id: str) -> tuple[Artifact, ...]:
    if isinstance(store, FileArtifactStore | AzureBlobArtifactStore):
        return store.descriptors(artifact_id)
    return (store.get(artifact_id),)


def export_archive(engine: BenchmarkEngine, run_id: str, destination: Path) -> str:
    """The index is the final publication marker; partial exports are not valid."""
    engine.assert_run_live(run_id)
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("archive destination must be empty")
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "objects").mkdir()
    archival = engine.backend.state.get(f"archive-run:{run_id}")
    if archival is not None:
        digest = str(archival.values["archive_digest"])
        index_bytes = engine.artifacts.get_bytes(digest)
        preserved = ArchiveIndex.model_validate_json(index_bytes, strict=False)
        for item in preserved.objects:
            _write_once(
                _object_path(destination, item.artifact_id),
                engine.artifacts.get_bytes(item.artifact_id),
                verify_existing=True,
            )
        _write_once(destination / "index.json", index_bytes, verify_existing=True)
        validate_archive(destination, expected_digest=digest)
        return digest
    repository: ExecutionRepository = engine.repository
    export_root = engine.backend.state.get(root_key(run_id))
    snapshot_database: Path | None = None
    if isinstance(repository, SqliteRepository):
        snapshot_database = destination / "source-snapshot.sqlite3"
        with (
            closing(sqlite3.connect(repository.path)) as source,
            closing(sqlite3.connect(snapshot_database)) as target,
        ):
            source.backup(target)
        repository = SqliteRepository(snapshot_database)
    try:
        run = repository.get_run(run_id)
        if run is None or run.report_artifact_id is None:
            raise ValueError("select a terminal run with an original report")
        tasks = tuple(repository.list_tasks(run_id))
        attempts = {task.id: tuple(repository.list_attempts(task.id)) for task in tasks}
        receipts: dict[str, dict[str, str]] = {}
        receipt_objects: dict[str, bytes] = {}
        receipt_descriptors: dict[str, list[Artifact]] = {}
        for task in tasks:
            for attempt in attempts[task.id]:
                content = engine.artifacts.get_receipt(
                    run_id,
                    task.id,
                    attempt.attempt_number,
                    incarnation=0
                    if export_root is None
                    else int(str(export_root.values.get("incarnation", 0))),
                )
                if content is not None:
                    digest = content_digest(content)
                    receipts.setdefault(task.id, {})[str(attempt.attempt_number)] = digest
                    receipt_objects[digest] = content
                    checkpoint, raw_bytes, _ = decode_receipt(content)
                    if raw_bytes is not None and checkpoint.raw is not None:
                        receipt_objects[checkpoint.raw.id] = raw_bytes
                        receipt_descriptors.setdefault(checkpoint.raw.id, []).append(checkpoint.raw)
        events: list[RunEvent] = []
        after = 0
        while True:
            page = tuple(repository.list_events(run_id, after=after, limit=500))
            events.extend(page)
            if len(page) < 500:
                break
            after = page[-1].sequence
        snapshot_id = run.manifest.sample_snapshot_artifact_id
        extra: dict[str, bytes] = dict(receipt_objects)
        if snapshot_id is None:
            samples = engine._samples_for_manifest(run.manifest)
            snapshot = SampleSnapshot(
                dataset_revision=run.manifest.dataset_revision, samples=samples
            )
            content = canonical_json(snapshot)
            snapshot_id = content_digest(content)
            extra[snapshot_id] = content
        snapshot = SampleSnapshot.model_validate_json(
            extra[snapshot_id] if snapshot_id in extra else engine.artifacts.get_bytes(snapshot_id),
            strict=False,
        )
        dataset_objects = tuple(
            sorted(
                {
                    artifact.id
                    for sample in snapshot.samples
                    for artifact in (sample.input, sample.reference)
                    if artifact is not None
                }
            )
        )
        publication: dict[str, str] = {}
        dataset = get_pack(run.manifest.benchmark.id, engine.pack_root).dataset
        if isinstance(dataset, PublicationClosure):
            for name, path in dataset.publication_files().items():
                _safe_logical_name(name)
                if path.is_symlink() or not path.is_file():
                    raise ValueError("publication closure contains a non-regular file")
                content = path.read_bytes()
                digest = content_digest(content)
                publication[name] = digest
                extra[digest] = content
        exports: dict[str, str] = {}
        for name in ("report.json", "report.csv"):
            path = engine.reports_root / run_id / name
            if path.is_file():
                content = path.read_bytes()
                digest = content_digest(content)
                exports[name] = digest
                extra[digest] = content
        operational_objects: list[str] = []
        cursor = None
        while True:
            ownership_page = engine.backend.state.page(
                f"root-object:{sha256_digest(run_id)}:", after=cursor
            )
            operational_objects.extend(
                str(row.values["artifact_id"]) for row in ownership_page.rows
            )
            cursor = ownership_page.cursor
            if cursor is None:
                break
        index = ArchiveIndex(
            run=run,
            tasks=tasks,
            attempts=attempts,
            events=tuple(events),
            snapshot_artifact_id=snapshot_id,
            dataset_objects=dataset_objects,
            operational_objects=tuple(sorted(set(operational_objects) | set(receipt_objects))),
            receipts=receipts,
            objects=(),
            publication=publication,
            exports=exports,
        )
        objects = []
        for artifact_id in sorted(_objects_required(index)):
            engine.assert_run_live(run_id)
            if artifact_id in extra:
                content = extra[artifact_id]
                try:
                    descriptors = _descriptors(engine.artifacts, artifact_id)
                    if not descriptors:
                        raise FileNotFoundError(artifact_id)
                except FileNotFoundError:
                    descriptors = (
                        Artifact(
                            id=artifact_id,
                            modality=Modality.STRUCTURED,
                            media_type="application/octet-stream",
                            size_bytes=len(content),
                            uri=f"artifact://sha256/{artifact_id}",
                        ),
                    )
            else:
                content = engine.artifacts.get_bytes(artifact_id)
                descriptors = _descriptors(engine.artifacts, artifact_id)
            if artifact_id in receipt_descriptors:
                descriptors = tuple(
                    {
                        sha256_digest(descriptor): descriptor
                        for descriptor in (*descriptors, *receipt_descriptors[artifact_id])
                    }.values()
                )
            _write_once(_object_path(destination, artifact_id), content, verify_existing=True)
            objects.append(
                ArchiveObject(
                    artifact_id=artifact_id,
                    size_bytes=len(content),
                    descriptors=descriptors,
                )
            )
        index = index.model_copy(update={"objects": tuple(objects)})
        index_bytes = canonical_json(index)
        engine.assert_run_live(run_id)
        current_root = engine.backend.state.get(root_key(run_id))
        if export_root is not None and (
            current_root is None or current_root.etag != export_root.etag
        ):
            raise ValueError("terminal evidence graph changed during export; retry the snapshot")
        _write_once(destination / "index.json", index_bytes, verify_existing=True)
        validate_archive(destination)
        return content_digest(index_bytes)
    finally:
        if snapshot_database is not None:
            for suffix in ("", "-wal", "-shm"):
                snapshot_database.with_name(snapshot_database.name + suffix).unlink(missing_ok=True)


def import_archive(engine: BenchmarkEngine, bundle: Path, *, expected_digest: str) -> str:
    """Stage hidden projections, then atomically publish the only permanent pin pair."""
    index = validate_archive(bundle, expected_digest=expected_digest)
    repository = engine.repository
    if not isinstance(repository, AzureTableRepository):
        raise ValueError("archive target requires the local-durable or azure-table-blob backend")
    state = engine.backend.state
    marker_key = f"archive:{expected_digest}"
    run_id = index.run.manifest.run_id
    marker = state.get(marker_key)
    if marker is not None and marker.values["status"] == "published":
        engine.lifecycle.require_live(run_id)
        preserved = ArchiveIndex.model_validate_json(
            engine.artifacts.get_bytes(expected_digest), strict=False
        )
        for item in preserved.objects:
            content = engine.artifacts.get_bytes(item.artifact_id)
            if len(content) != item.size_bytes:
                raise ValueError("published archive object length changed")
        return run_id
    existing = state.get(f"run:{run_id}")
    if existing is not None and (existing.values.get("archive_digest") != expected_digest):
        raise ValueError("archive run identity collides with a different target record")
    landing = state.get("landing")
    if landing is not None and landing.values.get("archive_digest") != expected_digest:
        raise ValueError("the original baseline pin is already designated")
    dataset_id = sha256_digest({"archive_dataset": expected_digest})
    claim_key = f"archive-reservation:{run_id}"
    claim = state.get(claim_key)
    if claim is not None and claim.values["digest"] != expected_digest:
        raise ValueError("a different archive already reserved this run identity")
    current_root = engine.lifecycle.get(run_id)
    if (
        current_root is not None
        and not current_root.archival
        and (
            state.get(f"launch-intent:{run_id}") is not None
            or state.page(f"root-object:{sha256_digest(run_id)}:", limit=1).rows
        )
    ):
        raise ValueError("archive run identity collides with an existing launch")
    for identifier, kind in ((dataset_id, "dataset"), (run_id, "run")):
        if engine.lifecycle.get(identifier) is None:
            engine.lifecycle.create(identifier, kind, staging_retention=True)  # type: ignore[arg-type]
        if kind == "run":
            row = state.get(root_key(identifier))
            assert row is not None
            root = LifecycleRoot.model_validate(row.values, strict=False)
            if not root.archival:
                generation, generation_change = engine.lifecycle._root_generation()
                state.transact(
                    (
                        StateChange(
                            row.key,
                            {
                                **root.model_dump(mode="json"),
                                "archival": True,
                                "generation": generation,
                            },
                            row.etag,
                        ),
                        generation_change,
                    )
                )
    if claim is None:
        try:
            state.transact(
                (
                    StateChange(claim_key, {"digest": expected_digest, "dataset_id": dataset_id}),
                    repository._guard(run_id),
                )
            )
        except StateConflictError:
            claim = state.get(claim_key)
            if claim is None or claim.values["digest"] != expected_digest:
                raise ValueError("archive run identity was reserved concurrently") from None
    import_generation = engine.lifecycle.require_live(run_id, ready=False).generation

    def import_guard() -> StateChange:
        root = engine.lifecycle.require_live(run_id, ready=False)
        claim = state.get(claim_key)
        if (
            root.generation != import_generation
            or claim is None
            or claim.values["digest"] != expected_digest
        ):
            raise ValueError("archive import generation changed; replay the reviewed import")
        return repository._guard(run_id)

    snapshot = SampleSnapshot.model_validate_json(
        _object_path(bundle, index.snapshot_artifact_id).read_bytes(), strict=False
    )
    dataset_objects = {index.snapshot_artifact_id, *index.publication.values()}
    dataset_objects.update(
        artifact.id
        for sample in snapshot.samples
        for artifact in (sample.input, sample.reference)
        if artifact is not None
    )
    for item in index.objects:
        import_guard()
        engine.lifecycle.attach(run_id, item.artifact_id)
        if item.artifact_id in dataset_objects:
            engine.lifecycle.attach(dataset_id, item.artifact_id)
        path = _object_path(bundle, item.artifact_id)
        for descriptor in item.descriptors:
            if isinstance(engine.artifacts, AzureBlobArtifactStore):
                engine.artifacts.put_file(path, descriptor)
            else:
                engine.artifacts.put_bytes(
                    path.read_bytes(),
                    modality=descriptor.modality,
                    media_type=descriptor.media_type,
                    metadata=dict(descriptor.metadata),
                )
            repository.register_artifact(descriptor)
    for task_id, receipts in index.receipts.items():
        for number, digest in receipts.items():
            engine.artifacts.put_receipt(
                run_id,
                task_id,
                int(number),
                _object_path(bundle, digest).read_bytes(),
                incarnation=engine.lifecycle.require_live(run_id, ready=False).incarnation,
            )
    engine.lifecycle.attach(run_id, expected_digest)
    stored_index = engine.artifacts.put_bytes(
        (bundle / "index.json").read_bytes(),
        modality=Modality.STRUCTURED,
        media_type="application/json",
        metadata={"role": "archive-index"},
    )
    repository.register_artifact(stored_index)
    for task in index.tasks:
        result_id = repository._immutable(task.result, run_id)
        task_values: dict[str, JsonValue] = {
            **task.model_dump(
                mode="json", exclude={"result", "next_available_at", "cooldown_until"}
            ),
            "result_id": result_id,
            "lease_until": 0,
            "supervisor_generation": 0,
            "next_available": 0,
            "waiting_since": 0,
            "due_key": None,
            "lease_key": None,
        }
        changes: list[StateChange] = []
        publications: tuple[tuple[str, Mapping[str, JsonValue]], ...] = (
            (f"task:{task.id}", task_values),
            (f"run-task:{run_id}:{task.id}", {"task_id": task.id}),
        )
        for key, values in publications:
            _stage_change(state, changes, key, values)
        for attempt in index.attempts[task.id]:
            attempt_id = repository._immutable(attempt, run_id)
            _stage_change(
                state,
                changes,
                f"attempt:{task.id}:{attempt.attempt_number:010d}",
                {
                    "artifact_id": attempt_id,
                    "retry_at": None,
                    "sent": False,
                    "checkpoint": None,
                },
            )
        if changes:
            changes.append(import_guard())
            state.transact(changes)
    for start in range(0, len(index.events), 90):
        changes = []
        for event in index.events[start : start + 90]:
            _stage_change(
                state,
                changes,
                f"event:{run_id}:{event.sequence:020d}",
                event.model_dump(mode="json"),
            )
        if changes:
            changes.append(import_guard())
            state.transact(changes)
    root = engine.lifecycle.require_live(dataset_id, ready=False)
    record = DatasetRecord(
        id=dataset_id,
        benchmark_id=index.run.manifest.benchmark.id,
        revision=snapshot.dataset_revision,
        snapshot_artifact_id=index.snapshot_artifact_id,
        sample_count=len(snapshot.samples),
        scored_sample_count=sum(sample.role is SampleRole.SCORED for sample in snapshot.samples),
        published_at=root.created_at,
        expires_at=None,
        installed=True,
        pinned=True,
        name=index.run.manifest.dataset_revision.id,
    )
    history_key = (
        f"history:{99999999999 - index.run.manifest.created_at.timestamp():020.6f}:{run_id}"
    )
    run_values: dict[str, JsonValue] = {
        "run_id": run_id,
        "manifest_artifact_id": index.run.manifest_artifact_id,
        "status": index.run.status.value,
        "report_artifact_id": index.run.report_artifact_id,
        "finalizer_token": index.run.finalizer_token,
        "finalizer_generation": 0,
        "runtime_error": None,
        "event_sequence": max((event.sequence for event in index.events), default=0),
        "initialized": len(index.tasks),
        "expected": len(index.tasks),
        "remaining": 0,
        "ready": True,
        "networked": index.run.manifest.networked_execution,
        "history_key": history_key,
        "launch_key_hash": None,
        "archival": True,
        "archive_digest": expected_digest,
    }
    changes = []
    publications = (
        (f"run:{run_id}", run_values),
        (history_key, {"run_id": run_id}),
        (f"dataset:{dataset_id}", record.model_dump(mode="json")),
        (
            f"dataset-index:{sha256_digest(record.benchmark_id)}:{dataset_id}",
            {"dataset_id": dataset_id},
        ),
        (
            marker_key,
            {
                "status": "published",
                "run_id": run_id,
                "dataset_id": dataset_id,
                "exports": dict(index.exports),
            },
        ),
        (f"archive-run:{run_id}", {"archive_digest": expected_digest}),
        (
            "landing",
            {
                "run_id": run_id,
                "benchmark_id": record.benchmark_id,
                "dataset_id": dataset_id,
                "archive_digest": expected_digest,
            },
        ),
    )
    for key, values in publications:
        _stage_change(state, changes, key, values)
    generation, generation_change = engine.lifecycle._root_generation(2)
    import_guard()
    changes.append(generation_change)
    for offset, identifier in enumerate((dataset_id, run_id)):
        row = state.get(root_key(identifier))
        assert row is not None
        root = engine.lifecycle.require_live(identifier, ready=False)
        if root.expires_at is not None:
            expiry = state.get(_expiry_key(root))
            if expiry is not None:
                changes.append(StateChange(expiry.key, None, expiry.etag))
        pinned = root.model_copy(
            update={
                "state": "ready",
                "pinned": True,
                "expires_at": None,
                "generation": generation + offset,
                "parent_id": dataset_id if identifier == run_id else None,
            }
        )
        changes.append(StateChange(row.key, pinned.model_dump(mode="json"), row.etag))
    try:
        state.transact(changes)
    except StateConflictError:
        marker = state.get(marker_key)
        if marker is None or marker.values.get("status") != "published":
            raise
    for name, digest in index.exports.items():
        destination = engine.reports_root / run_id / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        _write_once(destination, _object_path(bundle, digest).read_bytes(), verify_existing=True)
    return run_id


def _stage_change(
    state: StateStore,
    changes: list[StateChange],
    key: str,
    values: Mapping[str, JsonValue],
) -> None:
    previous = state.get(key)
    if previous is None:
        changes.append(StateChange(key, values))
    elif previous.values != values:
        raise ValueError("archive import collides with different immutable projections")
