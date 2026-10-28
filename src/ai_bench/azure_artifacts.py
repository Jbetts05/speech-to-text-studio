"""Private conditional Blob objects and immutable descriptor occurrences."""

from __future__ import annotations

import hashlib
import importlib
import io
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO, cast
from uuid import uuid4

from ai_bench.artifact_inventory import InventoryEntry, InventoryPage, parse_inventory_entry
from ai_bench.artifacts import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactReadCapacityError,
)
from ai_bench.contracts import Artifact, JsonValue, Modality
from ai_bench.hashing import canonical_json, content_digest, normalize_media_type, sha256_digest
from ai_bench.storage_state import StateChange, StateConflictError, StateStore


class _VerifiedSpool(io.BufferedReader):
    def __init__(self, path: Path, cleanup: Callable[[], None]) -> None:
        self._initialized = False
        self._close_lock = threading.Lock()
        self._cleanup: Callable[[], None] | None = None
        raw = io.FileIO(path, "r")
        try:
            super().__init__(raw, buffer_size=64 * 1024)
        except BaseException:
            raw.close()
            raise
        self._initialized = True
        self._cleanup = cleanup

    def close(self) -> None:
        if not self._initialized:
            return
        with self._close_lock:
            try:
                super().close()
            finally:
                cleanup, self._cleanup = self._cleanup, None
                if cleanup is not None:
                    cleanup()


class AzureBlobArtifactStore:
    def __init__(
        self,
        container_client: Any,
        state: StateStore,
        cache_root: Path,
        *,
        object_namespace: str = "local-conformance",
        match_condition: Any = None,
    ) -> None:
        if not object_namespace or not all(
            character.isascii() and (character.isalnum() or character in "-_")
            for character in object_namespace
        ):
            raise ValueError("object namespace must be a portable opaque identifier")
        self.container = container_client
        self.state = state
        self.cache_root = cache_root.resolve()
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self._read_slots = threading.BoundedSemaphore(4)
        self.maximum_object_bytes = 128 * 1024 * 1024
        self.object_namespace = object_namespace
        self.match_condition = match_condition

    def _condition(self) -> Any:
        return (
            self.match_condition
            if self.match_condition is not None
            else importlib.import_module("azure.core").MatchConditions.IfNotModified
        )

    def _blob(self, artifact_id: str) -> Any:
        if len(artifact_id) != 64 or any(c not in "0123456789abcdef" for c in artifact_id):
            raise ArtifactNotFoundError("invalid artifact identifier")
        return self.container.get_blob_client(
            f"namespaces/{self.object_namespace}/sha256/{artifact_id[:2]}/{artifact_id}"
        )

    def put_bytes(
        self,
        content: bytes,
        *,
        modality: Modality,
        media_type: str,
        metadata: dict[str, JsonValue] | None = None,
    ) -> Artifact:
        artifact = Artifact(
            id=content_digest(content),
            modality=modality,
            media_type=media_type,
            size_bytes=len(content),
            uri=f"artifact://sha256/{content_digest(content)}",
            metadata=metadata or {},
        )
        self._upload(artifact, content)
        return artifact

    def put_file(self, path: Path, artifact: Artifact) -> Artifact:
        with path.open("rb") as stream:
            if (
                path.stat().st_size != artifact.size_bytes
                or hashlib.file_digest(stream, "sha256").hexdigest() != artifact.id
            ):
                raise ArtifactIntegrityError("publication file does not match its descriptor")
            stream.seek(0)
            self._upload(artifact, stream)
        return artifact

    def _upload(self, artifact: Artifact, content: bytes | BinaryIO) -> None:
        if artifact.size_bytes > self.maximum_object_bytes:
            raise ValueError("immutable object exceeds the bounded object size")
        blob = self._blob(artifact.id)
        try:
            blob.upload_blob(
                content,
                overwrite=False,
                length=artifact.size_bytes,
                metadata={"sha256": artifact.id},
                max_concurrency=1,
            )
        except Exception:
            # Includes lost successful upload responses. Length alone does not
            # establish the identity of a preexisting content-addressed object.
            with self._open_verified_blob(artifact.id, artifact.size_bytes) as stream:
                stream.seek(0, 2)
                if stream.tell() != artifact.size_bytes:
                    raise ArtifactIntegrityError("existing Blob has an unexpected size") from None
        self.register_descriptor(artifact)

    def register_descriptor(self, artifact: Artifact) -> None:
        descriptor_key = sha256_digest(artifact)
        key = f"descriptor:{artifact.id}:{descriptor_key}"
        values: dict[str, JsonValue] = {
            "descriptor_key": descriptor_key,
            "media_type": normalize_media_type(artifact.media_type),
            "modality": artifact.modality.value,
        }
        existing = self.state.get(key)
        if existing is not None and existing.values != values:
            raise ArtifactIntegrityError("immutable artifact descriptor conflict")
        document = canonical_json(artifact)
        if len(document) > 1024 * 1024:
            raise ValueError("artifact descriptor exceeds the bounded descriptor size")
        blob = self._descriptor_blob(artifact.id, descriptor_key)
        try:
            blob.upload_blob(document, overwrite=False, length=len(document), max_concurrency=1)
        except Exception:
            if self._descriptor_bytes(artifact.id, descriptor_key) != document:
                raise ArtifactIntegrityError("existing descriptor has different bytes") from None
        if existing is not None:
            return
        try:
            self.state.transact((StateChange(key, values),))
        except StateConflictError:
            existing = self.state.get(key)
            if existing is None or existing.values != values:
                raise

    def _descriptor_blob(self, artifact_id: str, descriptor_key: str) -> Any:
        self._blob(artifact_id)
        self._blob(descriptor_key)
        return self.container.get_blob_client(
            f"namespaces/{self.object_namespace}/descriptors/{artifact_id}/{descriptor_key}"
        )

    def _descriptor_bytes(self, artifact_id: str, descriptor_key: str) -> bytes:
        blob = self._descriptor_blob(artifact_id, descriptor_key)
        properties = blob.get_blob_properties()
        if int(properties.size) > 1024 * 1024:
            raise ArtifactIntegrityError("descriptor exceeds its bounded size")
        content = bytearray()
        for chunk in blob.download_blob(
            etag=properties.etag, match_condition=self._condition(), max_concurrency=1
        ).chunks():
            content.extend(chunk)
            if len(content) > 1024 * 1024:
                raise ArtifactIntegrityError("descriptor exceeds its bounded size")
        if len(content) != int(properties.size) or content_digest(bytes(content)) != descriptor_key:
            raise ArtifactIntegrityError("descriptor failed its immutable identity check")
        return bytes(content)

    def put_json(self, value: Any, *, metadata: dict[str, JsonValue] | None = None) -> Artifact:
        return self.put_bytes(
            canonical_json(value),
            modality=Modality.STRUCTURED,
            media_type="application/json",
            metadata=metadata,
        )

    def get(self, artifact_id: str, media_type: str | None = None) -> Artifact:
        descriptors = self.descriptors(artifact_id)
        matches = [
            descriptor
            for descriptor in descriptors
            if media_type is None
            or normalize_media_type(descriptor.media_type) == normalize_media_type(media_type)
        ]
        if not matches:
            raise ArtifactNotFoundError(artifact_id)
        return min(
            matches,
            key=lambda item: (
                normalize_media_type(item.media_type),
                item.modality.value,
                canonical_json(item),
            ),
        )

    def get_descriptor(self, artifact_id: str, descriptor_key: str) -> Artifact:
        artifact = Artifact.model_validate_json(
            self._descriptor_bytes(artifact_id, descriptor_key), strict=False
        )
        if artifact.id != artifact_id or sha256_digest(artifact) != descriptor_key:
            raise ArtifactIntegrityError("descriptor does not match its requested identity")
        return artifact

    def descriptors(self, artifact_id: str) -> tuple[Artifact, ...]:
        self._blob(artifact_id)
        cursor: str | None = None
        descriptors: list[Artifact] = []
        while True:
            page = self.state.page(f"descriptor:{artifact_id}:", after=cursor)
            descriptors.extend(
                Artifact.model_validate_json(
                    self._descriptor_bytes(artifact_id, str(row.values["descriptor_key"])),
                    strict=False,
                )
                for row in page.rows
            )
            cursor = page.cursor
            if cursor is None:
                return tuple(descriptors)

    def open_verified(self, artifact_id: str) -> BinaryIO:
        descriptor = self.get(artifact_id)
        if descriptor.id != artifact_id:
            raise ArtifactIntegrityError("stored descriptor belongs to another artifact")
        return self._open_verified_blob(artifact_id, descriptor.size_bytes)

    def _open_verified_blob(self, artifact_id: str, expected_size: int | None) -> BinaryIO:
        blob = self._blob(artifact_id)
        path = self.cache_root / f"{artifact_id}.{uuid4().hex}.spool"
        if not self._read_slots.acquire(timeout=1.0):
            raise ArtifactReadCapacityError("verified object reader capacity is temporarily full")

        def cleanup() -> None:
            try:
                path.unlink(missing_ok=True)
            finally:
                self._read_slots.release()

        try:
            try:
                properties = blob.get_blob_properties()
                size = int(properties.size)
                if not 0 <= size <= self.maximum_object_bytes:
                    raise ArtifactIntegrityError("Blob exceeds the bounded object size")
                if expected_size is not None and size != expected_size:
                    raise ArtifactIntegrityError(
                        "Blob does not match its registered descriptor length"
                    )
                downloader = blob.download_blob(
                    etag=properties.etag,
                    match_condition=self._condition(),
                    max_concurrency=1,
                )
            except Exception as error:
                if getattr(error, "status_code", None) == 404:
                    raise ArtifactNotFoundError(artifact_id) from error
                raise
            digest = hashlib.sha256()
            count = 0
            with path.open("xb+") as stream:
                for chunk in downloader.chunks():
                    count += len(chunk)
                    if count > size:
                        raise ArtifactIntegrityError("Blob exceeded its declared length")
                    digest.update(chunk)
                    stream.write(chunk)
                if count != size or digest.hexdigest() != artifact_id:
                    raise ArtifactIntegrityError("Blob failed its SHA-256 or length check")
                stream.flush()
                stream.seek(0)
            return cast(BinaryIO, _VerifiedSpool(path, cleanup))
        except BaseException:
            cleanup()
            raise

    def get_bytes(self, artifact_id: str) -> bytes:
        with self._open_verified_blob(artifact_id, None) as stream:
            return stream.read()

    def object_etag(self, artifact_id: str) -> str | None:
        try:
            return str(self._blob(artifact_id).get_blob_properties().etag)
        except Exception as error:
            if getattr(error, "status_code", None) == 404:
                return None
            raise

    def inventory_page(self, *, cursor: str | None = None, limit: int = 40) -> InventoryPage:
        if not 1 <= limit <= 100:
            raise ValueError("artifact inventory page must contain 1-100 entries")
        prefix = f"namespaces/{self.object_namespace}/"
        pages = self.container.list_blobs(name_starts_with=prefix, results_per_page=limit).by_page(
            continuation_token=cursor
        )
        page = next(pages, None)
        entries: list[InventoryEntry] = []
        if page is not None:
            for item in page:
                name = str(item.name)
                if not name.startswith(prefix):
                    raise ArtifactIntegrityError("inventory escaped its application namespace")
                entry = parse_inventory_entry(name.removeprefix(prefix), str(item.etag))
                if entry is not None:
                    entries.append(entry)
        next_cursor = pages.continuation_token
        if next_cursor is not None and (
            not isinstance(next_cursor, str) or len(next_cursor) > 8192
        ):
            raise ArtifactIntegrityError("unexpected artifact inventory continuation")
        return InventoryPage(tuple(entries), next_cursor)

    def delete_inventory_entry(self, entry: InventoryEntry) -> bool:
        if entry.kind != "response" or parse_inventory_entry(entry.key, entry.etag) != entry:
            raise ValueError("only an exact owned response alias may be deleted directly")
        blob = self.container.get_blob_client(f"namespaces/{self.object_namespace}/{entry.key}")
        try:
            blob.delete_blob(etag=entry.etag, match_condition=self._condition())
        except Exception as error:
            if getattr(error, "status_code", None) == 404:
                return True
            if getattr(error, "status_code", None) == 412:
                return False
            raise
        return True

    def deletion_snapshot(self, artifact_id: str, *, limit: int = 40) -> tuple[InventoryEntry, ...]:
        blob = self._blob(artifact_id)
        prefix = f"namespaces/{self.object_namespace}/"
        entries = []
        try:
            properties = blob.get_blob_properties()
        except Exception as error:
            if getattr(error, "status_code", None) != 404:
                raise
        else:
            entries.append(
                InventoryEntry(
                    f"sha256/{artifact_id[:2]}/{artifact_id}",
                    str(properties.etag),
                    "object",
                    artifact_id=artifact_id,
                )
            )
        for item in self.container.list_blobs(
            name_starts_with=f"{prefix}descriptors/{artifact_id}/", results_per_page=limit
        ):
            if len(entries) >= limit:
                break
            entry = parse_inventory_entry(str(item.name).removeprefix(prefix), str(item.etag))
            if entry is None or entry.artifact_id != artifact_id or entry.kind != "descriptor":
                raise ArtifactIntegrityError("object collection snapshot escaped its scope")
            entries.append(entry)
        return tuple(entries)

    def delete_snapshot(self, entries: tuple[InventoryEntry, ...]) -> None:
        prefix = f"namespaces/{self.object_namespace}/"
        for entry in entries:
            if (
                entry.kind not in {"object", "descriptor"}
                or parse_inventory_entry(entry.key, entry.etag) != entry
            ):
                raise ValueError("invalid immutable-object deletion snapshot")
            try:
                self.container.get_blob_client(prefix + entry.key).delete_blob(
                    etag=entry.etag, match_condition=self._condition()
                )
            except Exception as error:
                if getattr(error, "status_code", None) not in {404, 412}:
                    raise

    def _receipt_blob(self, run_id: str, task_id: str, attempt: int, incarnation: int) -> Any:
        self._blob(run_id)
        self._blob(task_id)
        if not 1 <= attempt <= 20:
            raise ValueError("invalid checkpoint attempt")
        if incarnation < 0:
            raise ValueError("invalid checkpoint incarnation")
        generation = f"generation-{incarnation:020d}/" if incarnation else ""
        return self.container.get_blob_client(
            f"namespaces/{self.object_namespace}/receipts/{run_id}/{generation}{task_id}/{attempt:010d}"
        )

    def put_receipt(
        self, run_id: str, task_id: str, attempt: int, content: bytes, *, incarnation: int = 0
    ) -> None:
        if len(content) > 96 * 1024 * 1024:
            raise ValueError("response checkpoint exceeds its bounded size")
        blob = self._receipt_blob(run_id, task_id, attempt, incarnation)
        try:
            blob.upload_blob(
                content,
                overwrite=False,
                length=len(content),
                metadata={"sha256": content_digest(content)},
                max_concurrency=1,
            )
        except Exception:
            if self.get_receipt(run_id, task_id, attempt, incarnation=incarnation) != content:
                raise ArtifactIntegrityError("immutable response checkpoint conflict") from None

    def get_receipt(
        self, run_id: str, task_id: str, attempt: int, *, incarnation: int = 0
    ) -> bytes | None:
        blob = self._receipt_blob(run_id, task_id, attempt, incarnation)
        try:
            properties = blob.get_blob_properties()
        except Exception as error:
            if getattr(error, "status_code", None) == 404:
                return None
            raise
        if int(properties.size) > 96 * 1024 * 1024:
            raise ArtifactIntegrityError("response checkpoint exceeds its bounded size")
        content = bytearray()
        for chunk in blob.download_blob(
            etag=properties.etag, match_condition=self._condition(), max_concurrency=1
        ).chunks():
            content.extend(chunk)
            if len(content) > 96 * 1024 * 1024:
                raise ArtifactIntegrityError("response checkpoint exceeds its bounded size")
        if len(content) != int(properties.size) or content_digest(
            bytes(content)
        ) != properties.metadata.get("sha256"):
            raise ArtifactIntegrityError("response checkpoint failed its SHA-256 check")
        return bytes(content)

    def delete_receipts(self, run_id: str, *, limit: int = 90, incarnation: int = 0) -> bool:
        self._blob(run_id)
        prefix = f"namespaces/{self.object_namespace}/receipts/{run_id}/"
        if incarnation:
            prefix += f"generation-{incarnation:020d}/"
        items = (
            item
            for item in self.container.list_blobs(name_starts_with=prefix, results_per_page=100)
            if incarnation or len(item.name.removeprefix(prefix).split("/")) == 2
        )
        for deleted, item in enumerate(items):
            if deleted >= limit:
                return False
            blob = self.container.get_blob_client(item.name)
            try:
                blob.delete_blob(etag=str(item.etag), match_condition=self._condition())
            except Exception as error:
                if getattr(error, "status_code", None) != 404:
                    raise
        return True

    def delete(self, artifact_id: str, *, etag: str | None) -> bool:
        """Lifecycle must hold the deletion generation throughout this call."""
        descriptors: list[tuple[Any, str]] = []
        prefix = f"namespaces/{self.object_namespace}/descriptors/{artifact_id}/"
        more = False
        for item in self.container.list_blobs(name_starts_with=prefix, results_per_page=100):
            if len(descriptors) == 90:
                more = True
                break
            descriptors.append((self.container.get_blob_client(item.name), str(item.etag)))
        if etag is None:
            if self.object_etag(artifact_id) is not None:
                return False
        else:
            try:
                self._blob(artifact_id).delete_blob(etag=etag, match_condition=self._condition())
            except Exception:
                if self.object_etag(artifact_id) is not None:
                    raise
        for blob, descriptor_etag in descriptors:
            try:
                blob.delete_blob(etag=descriptor_etag, match_condition=self._condition())
            except Exception as error:
                if getattr(error, "status_code", None) != 404:
                    raise
        for path in self.cache_root.glob(f"{artifact_id}.*.spool"):
            path.unlink(missing_ok=True)
        return not more
