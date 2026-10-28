from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO

import pytest
from fastapi.testclient import TestClient

from ai_bench.api import create_app
from ai_bench.artifacts import (
    ArtifactIntegrityError,
    ArtifactReadCapacityError,
    ArtifactStore,
    FileArtifactStore,
)
from ai_bench.azure_artifacts import AzureBlobArtifactStore
from ai_bench.contracts import Modality
from ai_bench.storage_state import AzureTableStateStore, SqliteStateStore
from tests.test_core_blob import FakeContainer
from tests.test_hosted_core import FakeTable, engine_for


@pytest.fixture(params=["file", "blob"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> ArtifactStore:
    if request.param == "file":
        return FileArtifactStore(tmp_path / "local")
    return AzureBlobArtifactStore(
        FakeContainer(),
        SqliteStateStore(tmp_path / "state.sqlite3"),
        tmp_path / "spools",
        match_condition="IfNotModified",
    )


def test_verified_stream_is_immediately_readable_and_caller_owned(
    store: ArtifactStore,
) -> None:
    artifact = store.put_bytes(b"verified bytes", modality=Modality.TEXT, media_type="text/plain")
    stream: BinaryIO = store.open_verified(artifact.id)
    try:
        assert stream.tell() == 0
        assert stream.read(4) == b"veri"
        assert stream.seek(9) == 9
        assert stream.read() == b"bytes"
        assert not stream.closed
    finally:
        stream.close()
    stream.close()
    assert stream.closed
    if isinstance(store, AzureBlobArtifactStore):
        assert not tuple(store.cache_root.iterdir())


def test_blob_spools_are_bounded_and_released_by_close(tmp_path: Path) -> None:
    store = AzureBlobArtifactStore(
        FakeContainer(),
        SqliteStateStore(tmp_path / "state.sqlite3"),
        tmp_path / "spools",
        match_condition="IfNotModified",
    )
    artifact = store.put_bytes(b"bounded", modality=Modality.TEXT, media_type="text/plain")
    streams = [store.open_verified(artifact.id) for _ in range(4)]
    try:
        assert len(tuple(store.cache_root.glob("*.spool"))) == 4
        with pytest.raises(ArtifactReadCapacityError, match="capacity"):
            store.open_verified(artifact.id)
        streams[0].close()
        streams[0].close()
        with store.open_verified(artifact.id) as replacement:
            assert replacement.tell() == 0
            assert replacement.read() == b"bounded"
    finally:
        for stream in streams:
            stream.close()
    assert not tuple(store.cache_root.iterdir())


def test_reader_waits_for_a_concurrently_released_permit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AzureBlobArtifactStore(
        FakeContainer(),
        SqliteStateStore(tmp_path / "state.sqlite3"),
        tmp_path / "spools",
        match_condition="IfNotModified",
    )
    artifact = store.put_bytes(b"bounded", modality=Modality.TEXT, media_type="text/plain")
    waiting = threading.Event()
    with ExitStack() as readers:
        streams = [readers.enter_context(store.open_verified(artifact.id)) for _ in range(4)]
        acquire = store._read_slots.acquire

        def observed_acquire(*, timeout: float) -> bool:
            assert 0 < timeout <= 1
            waiting.set()
            return acquire(timeout=timeout)

        monkeypatch.setattr(store._read_slots, "acquire", observed_acquire)
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(store.get_bytes, artifact.id)
            assert waiting.wait(3)
            streams[0].close()
            assert result.result(timeout=3) == b"bounded"
            assert len(tuple(store.cache_root.glob("*.spool"))) == 3
    assert not tuple(store.cache_root.iterdir())


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_reader_saturation_returns_retryable_backpressure_then_recovers(
    tmp_path: Path, pack_root: Path, method: str
) -> None:
    state = AzureTableStateStore(FakeTable(), "read-capacity", match_condition="IfNotModified")
    engine = engine_for(state, tmp_path, pack_root)
    manifest = engine.create_run("synthetic_text_intent", solution_ids=("keyword-standalone",))
    artifact_id = manifest.samples[0].input_artifact_id
    assert isinstance(engine.artifacts, AzureBlobArtifactStore)
    client = TestClient(create_app(tmp_path, pack_root, backend=engine.backend))
    path = f"/api/artifacts/{artifact_id}"
    with ExitStack() as readers:
        for _ in range(4):
            readers.enter_context(engine.artifacts.open_verified(artifact_id))
        response = client.request(method, path)
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "1"
        assert len(tuple(engine.artifacts.cache_root.glob("*.spool"))) == 4
    assert client.request(method, path).status_code == 200
    client.close()
    assert not tuple(engine.artifacts.cache_root.iterdir())


def test_blob_descriptor_length_is_verified_before_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = FakeContainer()
    store = AzureBlobArtifactStore(
        container,
        SqliteStateStore(tmp_path / "state.sqlite3"),
        tmp_path / "spools",
        match_condition="IfNotModified",
    )
    artifact = store.put_bytes(b"exact", modality=Modality.TEXT, media_type="text/plain")
    blob = store._blob(artifact.id)
    properties = blob.get_blob_properties()
    with monkeypatch.context() as scoped:
        scoped.setattr(
            blob,
            "get_blob_properties",
            lambda: SimpleNamespace(
                size=properties.size + 1,
                etag=properties.etag,
                metadata=properties.metadata,
            ),
        )
        with pytest.raises(ArtifactIntegrityError, match="descriptor length"):
            store.open_verified(artifact.id)
    assert not tuple(store.cache_root.iterdir())
    with store.open_verified(artifact.id) as stream:
        assert stream.read() == b"exact"


def test_file_descriptor_length_is_verified_before_return(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path)
    artifact = store.put_bytes(b"exact", modality=Modality.TEXT, media_type="text/plain")
    sidecar = store._path(artifact.id).with_suffix(".json")
    sidecar.write_text(
        artifact.model_copy(update={"size_bytes": artifact.size_bytes + 1}).model_dump_json(),
        encoding="utf-8",
    )
    with pytest.raises(ArtifactIntegrityError, match="descriptor length"):
        store.open_verified(artifact.id)


def test_blob_never_uses_existing_spool_as_authoritative_storage(tmp_path: Path) -> None:
    store = AzureBlobArtifactStore(
        FakeContainer(),
        SqliteStateStore(tmp_path / "state.sqlite3"),
        tmp_path / "spools",
        match_condition="IfNotModified",
    )
    artifact = store.put_bytes(b"original", modality=Modality.TEXT, media_type="text/plain")
    (store.cache_root / f"{artifact.id}.abandoned.spool").write_bytes(b"original")
    store._blob(artifact.id).content = b"tampered"
    with pytest.raises(ArtifactIntegrityError):
        store.open_verified(artifact.id)
    assert tuple(path.name for path in store.cache_root.iterdir()) == (
        f"{artifact.id}.abandoned.spool",
    )
