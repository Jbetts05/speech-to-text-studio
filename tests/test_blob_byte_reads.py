from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ai_bench.artifacts import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactReadCapacityError,
)
from ai_bench.azure_artifacts import AzureBlobArtifactStore
from ai_bench.contracts import Modality
from ai_bench.storage_state import SqliteStateStore
from tests.test_core_blob import BlobError, FakeContainer


@pytest.mark.parametrize("variants", [1, 30])
def test_byte_reads_do_not_enumerate_descriptor_variants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variants: int
) -> None:
    store = AzureBlobArtifactStore(
        FakeContainer(),
        SqliteStateStore(tmp_path / "state.sqlite3"),
        tmp_path / "spools",
        match_condition="IfNotModified",
    )
    content = b'{"synthetic":"shared bytes"}'
    artifact = store.put_bytes(content, modality=Modality.STRUCTURED, media_type="application/json")
    for index in range(variants):
        store.register_descriptor(artifact.model_copy(update={"metadata": {"occurrence": index}}))
    assert len(store.descriptors(artifact.id)) == variants + 1
    blob = store._blob(artifact.id)
    head, download = blob.get_blob_properties, blob.download_blob
    calls = {"head": 0, "download": 0}

    def metadata() -> Any:
        calls["head"] += 1
        return head()

    def body(**kwargs: Any) -> Any:
        calls["download"] += 1
        assert kwargs["etag"] == blob.etag
        assert kwargs["match_condition"] == "IfNotModified"
        return download(**kwargs)

    monkeypatch.setattr(blob, "get_blob_properties", metadata)
    monkeypatch.setattr(blob, "download_blob", body)
    with monkeypatch.context() as byte_only:
        byte_only.setattr(
            store,
            "descriptors",
            lambda *_: pytest.fail("byte reads must not select presentation descriptors"),
        )
        assert store.get_bytes(artifact.id) == content
    assert calls == {"head": 1, "download": 1}
    assert not tuple(store.cache_root.iterdir())
    assert len(store.descriptors(artifact.id)) == variants + 1
    assert store.get(artifact.id, "application/json").media_type == "application/json"
    with store.open_verified(artifact.id) as stream:
        assert stream.read() == content


@pytest.mark.parametrize("fault", ["corrupt", "missing", "changed", "oversize", "length"])
def test_byte_read_failures_release_permits_and_spools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    store = AzureBlobArtifactStore(
        FakeContainer(),
        SqliteStateStore(tmp_path / "state.sqlite3"),
        tmp_path / "spools",
        match_condition="IfNotModified",
    )
    artifact = store.put_bytes(b"abcd", modality=Modality.TEXT, media_type="text/plain")
    blob = store._blob(artifact.id)
    if fault == "corrupt":
        blob.content = b"wxyz"
        expected: type[Exception] = ArtifactIntegrityError
    elif fault == "missing":
        blob.content = None
        expected = ArtifactNotFoundError
    elif fault == "changed":
        original = blob.download_blob

        def replace(**kwargs: Any) -> Any:
            blob.etag = "new-generation"
            return original(**kwargs)

        monkeypatch.setattr(blob, "download_blob", replace)
        expected = BlobError
    else:
        monkeypatch.setattr(
            blob,
            "get_blob_properties",
            lambda: SimpleNamespace(
                etag=blob.etag, size=store.maximum_object_bytes + 1 if fault == "oversize" else 2
            ),
        )
        expected = ArtifactIntegrityError
    with pytest.raises(expected):
        store.get_bytes(artifact.id)
    assert not tuple(store.cache_root.iterdir())
    for _ in range(4):
        assert store._read_slots.acquire(blocking=False)
    assert not store._read_slots.acquire(blocking=False)
    for _ in range(4):
        store._read_slots.release()


def test_byte_reads_keep_four_reader_capacity_and_registered_stream_length(
    tmp_path: Path,
) -> None:
    store = AzureBlobArtifactStore(
        FakeContainer(),
        SqliteStateStore(tmp_path / "state.sqlite3"),
        tmp_path / "spools",
        match_condition="IfNotModified",
    )
    artifact = store.put_bytes(b"abcd", modality=Modality.TEXT, media_type="text/plain")
    for _ in range(4):
        assert store._read_slots.acquire(blocking=False)
    try:
        with pytest.raises(ArtifactReadCapacityError):
            store.get_bytes(artifact.id)
    finally:
        for _ in range(4):
            store._read_slots.release()
    with pytest.raises(ArtifactIntegrityError, match="descriptor length"):
        store._open_verified_blob(artifact.id, 5)
    assert store.get_bytes(artifact.id) == b"abcd"
