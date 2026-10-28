from __future__ import annotations

import multiprocessing
import os
from collections.abc import Mapping
from contextlib import ExitStack
from multiprocessing.synchronize import Event
from pathlib import Path

import pytest

from ai_bench.artifact_inventory import InventoryEntry
from ai_bench.artifacts import ArtifactNotFoundError
from ai_bench.azure_artifacts import AzureBlobArtifactStore
from ai_bench.backends import BackendBundle, local_backend
from ai_bench.contracts import JsonValue, Modality, utc_now
from ai_bench.dataset_imports import DatasetImportFile, DatasetImportResult
from ai_bench.datasets import DatasetCatalog
from ai_bench.engine import BenchmarkEngine
from ai_bench.hashing import content_digest, sha256_digest
from ai_bench.lifecycle import LifecycleRoot, LifecycleService, RootUnavailableError
from ai_bench.repositories import RepositoryError
from ai_bench.storage_state import AzureTableStateStore, PrefixedStateStore
from ai_bench.table_repository import AzureTableRepository
from ai_bench.uploads import UploadFileReservation
from tests.test_core_blob import FakeContainer
from tests.test_core_uploads import SyntheticImporter
from tests.test_hosted_core import FakeTable


def _collect_and_republish(
    root: str, artifact_id: str, content: bytes, paused: Event, attempting: Event, finished: Event
) -> None:
    assert paused.wait(15)
    backend = local_backend(Path(root), durable_projections=True)
    attempting.set()
    backend.lifecycle._collect_orphan(artifact_id)
    backend.lifecycle.create("replacement-process-owner", "run")
    backend.lifecycle.attach("replacement-process-owner", artifact_id)
    backend.artifacts.put_bytes(content, modality=Modality.TEXT, media_type="text/plain")
    backend.lifecycle.publish("replacement-process-owner")
    finished.set()


@pytest.mark.parametrize("file_count", [6, 20], ids=["three-pairs", "ten-pairs"])
def test_validation_closes_each_blob_reader_before_opening_the_next(
    tmp_path: Path, pack_root: Path, file_count: int
) -> None:
    backend = local_backend(tmp_path / "runtime", durable_projections=True)
    staging = AzureBlobArtifactStore(
        FakeContainer(),
        PrefixedStateStore(backend.state, "staging:"),
        tmp_path / "spool",
        object_namespace="bounded-upload-test",
        match_condition="IfNotModified",
    )
    backend.staging = staging
    engine = BenchmarkEngine(tmp_path / "runtime", pack_root, backend=backend)
    observed: list[int] = []

    class ManyFilesImporter(SyntheticImporter):
        @property
        def descriptor(self) -> Mapping[str, JsonValue]:
            return {**super().descriptor, "max_files": 20}

        def import_files(self, files: tuple[DatasetImportFile, ...]) -> DatasetImportResult:
            observed.append(len(files))
            return super().import_files(files)

    content = [f"Synthetic part {index}".encode() for index in range(file_count)]
    importer = ManyFilesImporter()
    session = engine.uploads.admit(
        benchmark_id="synthetic_text_intent",
        name="Reader capacity regression",
        language="en",
        files=tuple(
            UploadFileReservation(
                name=f"part-{index}.txt", size_bytes=len(value), sha256=content_digest(value)
            )
            for index, value in enumerate(content)
        ),
        idempotency_key="many-parts",
        importer=importer,
    )
    for index, part in enumerate(session.parts):
        lease = engine.uploads.claim_part(session.upload_id, part.part_id, "receiver")
        assert lease is not None
        path = tmp_path / f"input-{index}"
        path.write_bytes(content[index])
        engine.uploads.complete_part(lease, path)
    engine.uploads.finalize(session.upload_id)
    assert engine.uploads.process_next(lambda _benchmark: importer)
    assert engine.uploads.get(session.upload_id).status == "ready"
    assert observed == [file_count]
    assert not list((tmp_path / "spool").iterdir())
    with ExitStack() as streams:
        for part in session.parts[:4]:
            stream = streams.enter_context(staging.open_verified(part.sha256))
            assert stream.read()


def test_stale_dataset_cleanup_cannot_remove_a_reimported_pinned_catalog(
    tmp_path: Path,
) -> None:
    backend = local_backend(tmp_path / "runtime", durable_projections=True)
    now = [backend.lifecycle.clock()]
    backend.lifecycle.clock = lambda: now[0]
    path = tmp_path / "input"
    content = b"Synthetic archive dataset"
    path.write_bytes(content)
    result = SyntheticImporter().import_files(
        (DatasetImportFile("input.txt", path, len(content), content_digest(content)),)
    )
    first = backend.datasets.publish("synthetic_text_intent", "stable-import", result)
    assert first.expires_at is not None
    now[0] = first.expires_at
    captured: list[LifecycleRoot] = []

    def pause_cleanup(root: LifecycleRoot) -> bool:
        captured.append(root)
        return False

    backend.lifecycle.reap(pause_cleanup)
    stale = captured[0]
    for _ in range(10):
        backend.lifecycle.reap(backend.datasets.cleanup_records)
        if backend.lifecycle.get(first.id) is None:
            break
    assert backend.lifecycle.get(first.id) is None
    replacement = backend.datasets.publish("synthetic_text_intent", "stable-import", result)
    backend.lifecycle.create("replacement-baseline", "run")
    backend.lifecycle.pin_archive(replacement.id, "replacement-baseline")
    assert replacement.id == first.id
    assert backend.datasets.get(replacement.id).pinned
    assert not backend.datasets.cleanup_records(stale)
    assert backend.datasets.get(replacement.id).pinned
    assert [record.id for record in backend.datasets.list("synthetic_text_intent").items] == [
        replacement.id
    ]


@pytest.mark.parametrize("azure", [False, True], ids=["filesystem", "blob-table"])
def test_late_physical_writes_remain_discoverable_after_all_root_indexes_are_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, azure: bool
) -> None:
    if azure:
        state = AzureTableStateStore(FakeTable(), "review", match_condition="IfNotModified")
        objects = AzureBlobArtifactStore(
            FakeContainer(),
            state,
            tmp_path / "spool",
            object_namespace="review",
            match_condition="IfNotModified",
        )
        lifecycle = LifecycleService(state, objects)
        backend = BackendBundle(
            AzureTableRepository(state, objects, lifecycle),
            objects,
            state,
            lifecycle,
            DatasetCatalog(state, objects, lifecycle),
            "azure-table-blob",
        )
    else:
        backend = local_backend(tmp_path / "runtime", durable_projections=True)
    now = [utc_now()]
    backend.lifecycle.clock = lambda: now[0]
    engine = BenchmarkEngine(tmp_path / "runtime", backend=backend)
    run_id = content_digest(b"synthetic expiring run")
    task_id = content_digest(b"synthetic physical call")
    original_root = engine.lifecycle.create(run_id, "run")
    engine.lifecycle.publish(run_id)
    assert original_root.expires_at is not None
    expiry = original_root.expires_at
    original_attach = engine.lifecycle.attach

    def paused_writer(identifier: str, artifact_id: str) -> None:
        original_attach(identifier, artifact_id)
        now[0] = expiry
        for _ in range(10):
            engine.reap_expired(limit=10)
            if engine.lifecycle.get(run_id) is None:
                break
        assert engine.lifecycle.get(run_id) is None
        assert not backend.state.page(f"owner:{artifact_id}:").rows

    monkeypatch.setattr(engine.lifecycle, "attach", paused_writer)
    late = engine._owned_json(run_id, {"synthetic": "late result"}, metadata={"role": "comparison"})
    assert engine.artifacts.get_bytes(late.id)
    engine.artifacts.put_receipt(
        run_id, task_id, 1, b"synthetic late response", incarnation=original_root.incarnation
    )
    assert not engine.artifact_has_live_owner(late.id)
    for _ in range(5):
        engine.reap_expired(limit=10)
    with pytest.raises(ArtifactNotFoundError):
        engine.artifacts.get(late.id)
    assert (
        engine.artifacts.get_receipt(run_id, task_id, 1, incarnation=original_root.incarnation)
        is None
    )
    assert not backend.state.page(f"descriptor:{late.id}:").rows
    assert not backend.state.page("orphan:").rows
    with pytest.raises((RootUnavailableError, RepositoryError)):
        engine._owned_json(run_id, {"synthetic": "new attempt"}, metadata={})


def test_orphan_reconciliation_preserves_system_objects_and_foreign_blob_namespaces(
    tmp_path: Path,
) -> None:
    state = AzureTableStateStore(FakeTable(), "review", match_condition="IfNotModified")
    container = FakeContainer()
    objects = AzureBlobArtifactStore(
        container,
        state,
        tmp_path / "spool",
        object_namespace="review",
        match_condition="IfNotModified",
    )
    lifecycle = LifecycleService(state, objects)
    protected = b"synthetic installed configuration"
    identifier = content_digest(protected)
    lifecycle.retain_system_object(identifier)
    objects.put_bytes(protected, modality=Modality.TEXT, media_type="text/plain")
    foreign_name = f"namespaces/other/sha256/{identifier[:2]}/{identifier}"
    container.get_blob_client(foreign_name).upload_blob(protected, overwrite=False)
    unknown_name = "namespaces/review/operator-notes.txt"
    container.get_blob_client(unknown_name).upload_blob(b"not owned by GC", overwrite=False)
    orphan = objects.put_json({"unowned": True})
    for _ in range(5):
        lifecycle.reap(lambda _root: True, limit=1)
    assert objects.get_bytes(identifier) == protected
    with pytest.raises(ArtifactNotFoundError):
        objects.get(orphan.id)
    assert container.blobs[foreign_name].content == protected
    assert container.blobs[unknown_name].content == b"not owned by GC"
    assert state.get(f"system-object:{identifier}") is not None
    assert state.get(f"object:{sha256_digest({'unowned': True})}") is None


def test_stale_orphan_delete_snapshot_cannot_delete_a_new_live_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = AzureTableStateStore(FakeTable(), "review", match_condition="IfNotModified")
    objects = AzureBlobArtifactStore(
        FakeContainer(),
        state,
        tmp_path / "spool",
        object_namespace="review",
        match_condition="IfNotModified",
    )
    first = LifecycleService(state, objects)
    second = LifecycleService(state, objects)
    content = b"Synthetic recreated generation"
    artifact = objects.put_bytes(content, modality=Modality.TEXT, media_type="text/plain")
    original_delete = objects.delete_snapshot
    paused = False

    def delete_after_recreation(entries: tuple[InventoryEntry, ...]) -> None:
        nonlocal paused
        if not paused:
            paused = True
            second._collect_orphan(artifact.id)
            assert state.get(f"object:{artifact.id}") is None
            second.create("new-live-owner", "run")
            second.attach("new-live-owner", artifact.id)
            objects.put_bytes(content, modality=Modality.TEXT, media_type="text/plain")
            second.publish("new-live-owner")
        original_delete(entries)

    monkeypatch.setattr(objects, "delete_snapshot", delete_after_recreation)
    first._collect_orphan(artifact.id)
    assert objects.get_bytes(artifact.id) == content
    assert first.artifact_has_live_owner(artifact.id)
    row = state.get(f"object:{artifact.id}")
    assert row is not None and row.values["state"] == "live"
    assert not state.page("orphan:").rows


def test_new_pinned_owner_invalidates_orphan_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = local_backend(tmp_path / "runtime", durable_projections=True)
    lifecycle = backend.lifecycle
    content = b"Synthetic pinned acquisition race"
    artifact = backend.artifacts.put_bytes(content, modality=Modality.TEXT, media_type="text/plain")
    original_check = lifecycle._object_is_owned
    paused = False

    def attach_after_ownership_read(artifact_id: str) -> bool:
        nonlocal paused
        observed = original_check(artifact_id)
        if not paused:
            paused = True
            lifecycle.create("replacement-dataset", "dataset")
            lifecycle.attach("replacement-dataset", artifact_id)
            lifecycle.create("replacement-run", "run")
            lifecycle.pin_archive("replacement-dataset", "replacement-run")
        return observed

    monkeypatch.setattr(lifecycle, "_object_is_owned", attach_after_ownership_read)
    lifecycle._collect_orphan(artifact.id)
    row = backend.state.get(f"object:{artifact.id}")
    assert row is not None and row.values["state"] == "live"
    lifecycle.create("next-run", "run")
    lifecycle.attach("next-run", artifact.id)
    assert backend.artifacts.get_bytes(artifact.id) == content
    assert not backend.state.page("orphan:").rows


def test_filesystem_gc_holds_cross_process_fence_between_stat_and_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runtime"
    backend = local_backend(root, durable_projections=True)
    content = b"Synthetic cross-process replacement"
    artifact = backend.artifacts.put_bytes(content, modality=Modality.TEXT, media_type="text/plain")
    object_path = root / "artifacts" / "objects" / artifact.id[:2] / artifact.id
    context = multiprocessing.get_context("spawn")
    paused, attempting, finished = context.Event(), context.Event(), context.Event()
    child = context.Process(
        target=_collect_and_republish,
        args=(str(root), artifact.id, content, paused, attempting, finished),
    )
    original_delete = backend.artifacts.delete_snapshot
    original_stat = Path.stat
    armed = False
    intercepted = False

    def deletion(entries: tuple[InventoryEntry, ...]) -> None:
        nonlocal armed
        armed = True
        try:
            original_delete(entries)
        finally:
            armed = False

    def stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        nonlocal intercepted
        result = original_stat(path, follow_symlinks=follow_symlinks)
        if armed and path == object_path and not intercepted:
            intercepted = True
            paused.set()
            assert attempting.wait(15)
            assert not finished.wait(0.25), "another process replaced bytes inside the delete fence"
        return result

    monkeypatch.setattr(backend.artifacts, "delete_snapshot", deletion)
    monkeypatch.setattr(Path, "stat", stat)
    child.start()
    try:
        backend.lifecycle._collect_orphan(artifact.id)
        child.join(timeout=20)
        assert not child.is_alive()
        assert child.exitcode == 0 and finished.is_set()
        assert intercepted
        assert backend.artifacts.get_bytes(artifact.id) == content
        assert backend.lifecycle.artifact_has_live_owner(artifact.id)
    finally:
        if child.is_alive():
            child.terminate()
            child.join(timeout=10)
