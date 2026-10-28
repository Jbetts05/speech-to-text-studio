from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from ai_bench.artifacts import ArtifactIntegrityError, ArtifactStore, FileArtifactStore
from ai_bench.azure_artifacts import AzureBlobArtifactStore
from ai_bench.contracts import Artifact, ArtifactEvidence, Modality, utc_now
from ai_bench.hashing import canonical_json, content_digest, sha256_digest
from ai_bench.lifecycle import ExpiredRootError
from ai_bench.repositories import LeaseLostError, RepositoryError
from ai_bench.storage_state import AzureTableStateStore, SqliteStateStore, StateChange
from ai_bench.table_repository import AzureTableRepository
from tests.test_core_blob import BlobError, FakeContainer
from tests.test_hosted_core import FakeTable, engine_for


@pytest.mark.parametrize("variants", [1, 30])
def test_evidence_validation_reads_one_registered_descriptor(
    tmp_path: Path, pack_root: Path, monkeypatch: pytest.MonkeyPatch, variants: int
) -> None:
    state = AzureTableStateStore(FakeTable(), "synthetic-test", match_condition="IfNotModified")
    engine = engine_for(state, tmp_path, pack_root)
    engine.create_run("synthetic_text_intent", solution_ids=("keyword-standalone",))
    lease = engine.repository.claim_task("owner")
    assert lease is not None
    store = engine.artifacts
    assert isinstance(store, AzureBlobArtifactStore)
    artifact = store.put_bytes(b"synthetic", modality=Modality.TEXT, media_type="text/plain")
    for index in range(variants):
        store.register_descriptor(artifact.model_copy(update={"metadata": {"occurrence": index}}))
    read = store._descriptor_bytes
    reads = 0

    def counted(artifact_id: str, descriptor_key: str) -> bytes:
        nonlocal reads
        reads += 1
        return read(artifact_id, descriptor_key)

    monkeypatch.setattr(store, "_descriptor_bytes", counted)
    evidence = ArtifactEvidence(
        role="raw",
        artifact_id=artifact.id,
        modality=artifact.modality,
        media_type=artifact.media_type,
        size_bytes=artifact.size_bytes,
        attempt_number=lease.attempt_number,
    )
    engine.repository.record_attempt_artifact(lease, evidence)
    assert engine.repository.list_attempts(lease.task.id)[0].evidence == (evidence,)
    assert reads == 1


@pytest.fixture(params=["file", "blob"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> ArtifactStore:
    if request.param == "file":
        return FileArtifactStore(tmp_path / "objects")
    return AzureBlobArtifactStore(
        FakeContainer(),
        SqliteStateStore(tmp_path / "state.sqlite3"),
        tmp_path / "spool",
        match_condition="IfNotModified",
    )


def replace_descriptor(
    store: ArtifactStore, artifact_id: str, descriptor_key: str, content: bytes | None
) -> None:
    if isinstance(store, FileArtifactStore):
        path = store.objects / artifact_id[:2] / f"{artifact_id}.{descriptor_key}.json"
        if content is None:
            path.unlink()
            (path.parent / f"{artifact_id}.json").unlink()
        else:
            path.write_bytes(content)
    else:
        assert isinstance(store, AzureBlobArtifactStore)
        store._descriptor_blob(artifact_id, descriptor_key).content = content


def test_exact_descriptor_read_preserves_selection(
    store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = store.put_bytes(b"synthetic", modality=Modality.TEXT, media_type="text/plain")
    variants = [
        artifact.model_copy(update={"metadata": {"occurrence": index}}) for index in range(10)
    ]
    for variant in variants:
        store.register_descriptor(variant)
    selected = store.get(artifact.id)
    with monkeypatch.context() as exact:
        exact.setattr(store, "get", lambda *_: pytest.fail("must not select a descriptor"))
        exact.setattr(store, "descriptors", lambda *_: pytest.fail("must not enumerate variants"))
        for variant in variants:
            assert store.get_descriptor(artifact.id, sha256_digest(variant)) == variant
    assert store.get(artifact.id) == selected
    assert len(store.descriptors(artifact.id)) == 11


@pytest.mark.parametrize("fault", ["corrupt", "missing", "wrong_id"])
def test_exact_descriptor_rejects_invalid_document(store: ArtifactStore, fault: str) -> None:
    artifact = store.put_bytes(b"synthetic", modality=Modality.TEXT, media_type="text/plain")
    key = sha256_digest(artifact)
    if fault == "wrong_id":
        other_id = content_digest(b"another synthetic object")
        other = artifact.model_copy(update={"id": other_id, "uri": f"artifact://sha256/{other_id}"})
        key = sha256_digest(other)
        content: bytes | None = canonical_json(other)
    else:
        content = None if fault == "missing" else b"corrupt"
    replace_descriptor(store, artifact.id, key, content)
    with pytest.raises(
        (FileNotFoundError, BlobError) if fault == "missing" else ArtifactIntegrityError
    ):
        store.get_descriptor(artifact.id, key)


def test_exact_file_descriptor_reads_legacy_canonical_sidecar(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path)
    artifact = store.put_bytes(b"synthetic", modality=Modality.TEXT, media_type="text/plain")
    key = sha256_digest(artifact)
    (store.objects / artifact.id[:2] / f"{artifact.id}.{key}.json").unlink()
    assert store.get_descriptor(artifact.id, key) == artifact


def test_exact_blob_descriptor_rejects_changed_etag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AzureBlobArtifactStore(
        FakeContainer(),
        SqliteStateStore(tmp_path / "state.sqlite3"),
        tmp_path / "spool",
        match_condition="IfNotModified",
    )
    artifact = store.put_bytes(b"synthetic", modality=Modality.TEXT, media_type="text/plain")
    key = sha256_digest(artifact)
    blob = store._descriptor_blob(artifact.id, key)
    download = blob.download_blob

    def changed(**kwargs: Any) -> Any:
        assert kwargs["etag"] == blob.etag and kwargs["match_condition"] == "IfNotModified"
        blob.etag = "replacement"
        return download(**kwargs)

    monkeypatch.setattr(blob, "download_blob", changed)
    with pytest.raises(BlobError) as failure:
        store.get_descriptor(artifact.id, key)
    assert failure.value.status_code == 412


def test_registered_descriptor_search_passes_first_page(
    tmp_path: Path, pack_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = AzureTableStateStore(FakeTable(), "synthetic-test", match_condition="IfNotModified")
    engine = engine_for(state, tmp_path, pack_root)
    repo = engine.repository
    assert isinstance(repo, AzureTableRepository)
    artifact = engine.artifacts.put_bytes(
        b"synthetic", modality=Modality.TEXT, media_type="application/x-other"
    )
    candidates = [
        artifact.model_copy(
            update={"media_type": "text/plain;charset=utf-8", "metadata": {"occurrence": index}}
        )
        for index in range(150)
    ]
    matching = max(candidates, key=sha256_digest)
    # Populate an entire nonmatching page before the matching descriptor.
    preceding = [
        artifact.model_copy(update={"metadata": {"occurrence": index}}) for index in range(150)
    ]
    preceding = [item for item in preceding if sha256_digest(item) < sha256_digest(matching)]
    assert len(preceding) > 100
    for variant in (*preceding[:110], matching):
        engine.artifacts.register_descriptor(variant)
    reads = 0
    read = engine.artifacts.get_descriptor

    def counted(artifact_id: str, descriptor_key: str) -> Artifact:
        nonlocal reads
        reads += 1
        return read(artifact_id, descriptor_key)

    monkeypatch.setattr(engine.artifacts, "get_descriptor", counted)
    assert repo._has_registered_descriptor(artifact.id, "TEXT/PLAIN; charset=utf-8")
    assert reads == 1
    assert not repo._has_registered_descriptor(artifact.id, "image/png")
    assert not repo._has_registered_descriptor(artifact.id, "text/plain;charset=ascii")
    assert reads == 1


@pytest.mark.parametrize("backend", ["sqlite", "table"])
@pytest.mark.parametrize("fault", ["unregistered", "corrupt", "index", "expiry", "takeover"])
def test_evidence_descriptor_failures_preserve_attempt(
    tmp_path: Path,
    pack_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    fault: str,
) -> None:
    state = (
        SqliteStateStore(tmp_path / "state.sqlite3")
        if backend == "sqlite"
        else AzureTableStateStore(FakeTable(), "synthetic-test", match_condition="IfNotModified")
    )
    engine = engine_for(state, tmp_path / "engine", pack_root)
    engine.create_run("synthetic_text_intent", solution_ids=("keyword-standalone",))
    repo = engine.repository
    assert isinstance(repo, AzureTableRepository)
    lease = repo.claim_task("owner")
    assert lease is not None
    store = engine.artifacts
    artifact = store.put_bytes(b"synthetic", modality=Modality.TEXT, media_type="text/plain")
    repo.register_artifact(artifact)
    key = sha256_digest(artifact)
    row = state.get(f"descriptor:{artifact.id}:{key}")
    assert row is not None
    expected: type[Exception] = RepositoryError
    if fault == "unregistered":
        state.transact((StateChange(row.key, None, row.etag),))
    elif fault == "corrupt":
        replace_descriptor(store, artifact.id, key, b"corrupt")
        expected = ArtifactIntegrityError
    elif fault == "index":
        state.transact((StateChange(row.key, {**row.values, "modality": "image"}, row.etag),))
    else:
        read = store.get_descriptor

        def interfere(artifact_id: str, descriptor_key: str) -> Artifact:
            result = read(artifact_id, descriptor_key)
            if fault == "expiry":
                engine.lifecycle.clock = lambda: utc_now() + timedelta(days=11)
            else:
                future = utc_now() + timedelta(seconds=31)
                repo.clock = lambda: future
                assert repo.acquire_supervisor("replacement")
            return result

        monkeypatch.setattr(store, "get_descriptor", interfere)
        expected = ExpiredRootError if fault == "expiry" else LeaseLostError
    evidence = ArtifactEvidence(
        role="raw",
        artifact_id=artifact.id,
        modality=artifact.modality,
        media_type=artifact.media_type,
        size_bytes=artifact.size_bytes,
        attempt_number=lease.attempt_number,
    )
    with pytest.raises(expected):
        repo.record_attempt_artifact(lease, evidence)
    engine.lifecycle.clock = utc_now
    assert repo.list_attempts(lease.task.id)[0].evidence == ()
