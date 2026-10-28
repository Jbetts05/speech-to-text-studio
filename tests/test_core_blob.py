from __future__ import annotations

from pathlib import Path
from threading import RLock
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from ai_bench.artifacts import ArtifactIntegrityError
from ai_bench.azure_artifacts import AzureBlobArtifactStore
from ai_bench.backend_proof import (
    REQUIRED_SCENARIOS,
    BackendConformanceReceipt,
    backend_code_signature,
    publish_receipt,
    verify_receipt,
)
from ai_bench.contracts import Modality, utc_now
from ai_bench.hashing import content_digest
from ai_bench.storage_state import AzureTableStateStore, SqliteStateStore
from tests.test_hosted_core import FakeTable


class BlobError(Exception):
    def __init__(self, status: int) -> None:
        self.status_code = status


class FakeBlob:
    def __init__(self) -> None:
        self.content: bytes | None = None
        self.etag = ""
        self.metadata: dict[str, str] = {}
        self._lock = RLock()

    def upload_blob(self, data: Any, **kwargs: Any) -> None:
        with self._lock:
            self._upload_blob(data, **kwargs)

    def _upload_blob(self, data: Any, **kwargs: Any) -> None:
        if kwargs["overwrite"]:
            assert kwargs.get("match_condition") == "IfNotModified"
            if kwargs.get("etag") != self.etag:
                raise BlobError(412)
        elif self.content is not None:
            raise BlobError(409)
        self.content = data if isinstance(data, bytes) else data.read()
        self.etag = uuid4().hex
        self.metadata = kwargs.get("metadata", {})

    def get_blob_properties(self) -> Any:
        with self._lock:
            if self.content is None:
                raise BlobError(404)
            return SimpleNamespace(size=len(self.content), etag=self.etag, metadata=self.metadata)

    def download_blob(self, **kwargs: Any) -> Any:
        with self._lock:
            if kwargs["etag"] != self.etag:
                raise BlobError(412)
            assert self.content is not None
            chunks = [self.content[index : index + 2] for index in range(0, len(self.content), 2)]
            return SimpleNamespace(chunks=lambda: iter(chunks))

    def delete_blob(self, **kwargs: Any) -> None:
        with self._lock:
            if kwargs["etag"] != self.etag:
                raise BlobError(412)
            self.content = None


class FakeContainer:
    def __init__(self) -> None:
        self.blobs: dict[str, FakeBlob] = {}
        self._lock = RLock()

    def get_blob_client(self, name: str) -> FakeBlob:
        with self._lock:
            return self.blobs.setdefault(name, FakeBlob())

    def list_blobs(self, *, name_starts_with: str, results_per_page: int) -> FakeBlobListing:
        with self._lock:
            return FakeBlobListing(
                [
                    SimpleNamespace(name=name, etag=blob.etag)
                    for name, blob in sorted(self.blobs.items())
                    if name.startswith(name_starts_with) and blob.content is not None
                ],
                results_per_page,
            )


class FakeBlobListing(list[Any]):
    def __init__(self, values: list[Any], page_size: int) -> None:
        super().__init__(values)
        self.page_size = page_size

    def by_page(self, *, continuation_token: str | None = None) -> FakeBlobPages:
        return FakeBlobPages(self, self.page_size, int(continuation_token or 0))


class FakeBlobPages:
    def __init__(self, values: list[Any], page_size: int, offset: int) -> None:
        self.values = values
        self.page_size = page_size
        self.offset = offset
        self.continuation_token: str | None = None

    def __iter__(self) -> FakeBlobPages:
        return self

    def __next__(self) -> list[Any]:
        if self.offset >= len(self.values):
            raise StopIteration
        end = min(self.offset + self.page_size, len(self.values))
        page = self.values[self.offset : end]
        self.offset = end
        self.continuation_token = str(end) if end < len(self.values) else None
        return page


def test_blob_verifies_equal_length_collision_and_descriptor_variants(tmp_path: Path) -> None:
    container = FakeContainer()
    store = AzureBlobArtifactStore(
        container,
        SqliteStateStore(tmp_path / "state.sqlite3"),
        tmp_path / "spool",
        match_condition="IfNotModified",
    )
    original = store.put_bytes(b"abc", modality=Modality.TEXT, media_type="text/plain")
    store.put_bytes(b"abc", modality=Modality.STRUCTURED, media_type="application/json")
    assert len(store.descriptors(original.id)) == 2
    assert store.get(original.id, "text/plain").media_type == "text/plain"
    with store.open_verified(original.id) as stream:
        assert stream.read(1) == b"a"
        stream.seek(1)
        assert stream.read() == b"bc"
    assert not list((tmp_path / "spool").iterdir())
    blob = next(iter(container.blobs.values()))
    blob.content = b"xyz"
    with pytest.raises(ArtifactIntegrityError):
        store.put_bytes(b"abc", modality=Modality.TEXT, media_type="text/plain")
    with pytest.raises(ArtifactIntegrityError):
        store.get_bytes(content_digest(b"abc"))


def test_blob_delete_does_not_remove_recreated_generation(tmp_path: Path) -> None:
    container = FakeContainer()
    store = AzureBlobArtifactStore(
        container,
        SqliteStateStore(tmp_path / "state.sqlite3"),
        tmp_path / "spool",
        match_condition="IfNotModified",
    )
    artifact = store.put_bytes(b"same content", modality=Modality.TEXT, media_type="text/plain")
    first = store.object_etag(artifact.id)
    assert first is not None
    store.delete(artifact.id, etag=first)
    store.put_bytes(b"same content", modality=Modality.TEXT, media_type="text/plain")
    with pytest.raises(BlobError):
        store.delete(artifact.id, etag=first)
    assert store.get_bytes(artifact.id) == b"same content"


def test_live_receipt_is_hash_bound_to_configuration_and_exact_build_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = AzureTableStateStore(FakeTable(), "conformance-proof", match_condition="IfNotModified")
    objects = AzureBlobArtifactStore(
        FakeContainer(),
        state,
        tmp_path / "proof-spool",
        object_namespace="conformance-proof",
        match_condition="IfNotModified",
    )
    checkpoint = BackendConformanceReceipt(
        code_signature=backend_code_signature(),
        configuration_signature="a" * 64,
        source_namespace="conformance-proof",
        completed_at=utc_now(),
        scenarios=tuple(sorted(REQUIRED_SCENARIOS)),
    )
    digest = publish_receipt(state, objects, checkpoint)
    assert verify_receipt(state, objects, digest, "a" * 64)
    with pytest.raises(ValueError, match="does not match"):
        verify_receipt(state, objects, digest, "b" * 64)
    with pytest.raises(ValueError, match="incomplete"):
        publish_receipt(state, objects, checkpoint.model_copy(update={"scenarios": ()}))
    assert not verify_receipt(state, objects, "", "a" * 64)
    monkeypatch.setattr("ai_bench.backend_proof.backend_code_signature", lambda: "b" * 64)
    assert verify_receipt(state, objects, digest, "a" * 64)
    with pytest.raises(ValueError, match="exact build"):
        verify_receipt(state, objects, digest, "a" * 64, require_current_build=True)
    with pytest.raises(ValueError, match="stale"):
        publish_receipt(state, objects, checkpoint)
