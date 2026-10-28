"""Content-addressed local artifact storage."""

from __future__ import annotations

import base64
import heapq
import json
import os
import threading
from pathlib import Path
from typing import Any, BinaryIO, Protocol, cast
from uuid import uuid4

from ai_bench.artifact_inventory import InventoryEntry, InventoryPage, parse_inventory_entry
from ai_bench.contracts import Artifact, JsonValue, Modality
from ai_bench.hashing import (
    canonical_json,
    content_digest,
    normalize_media_type,
    sha256_digest,
)


class ArtifactNotFoundError(FileNotFoundError):
    pass


class ArtifactIntegrityError(ValueError):
    pass


class ArtifactReadCapacityError(RuntimeError):
    pass


_PUBLICATION_LOCK = threading.Lock()


class ArtifactStore(Protocol):
    def put_bytes(
        self,
        content: bytes,
        *,
        modality: Modality,
        media_type: str,
        metadata: dict[str, JsonValue] | None = None,
    ) -> Artifact: ...

    def put_json(self, value: Any, *, metadata: dict[str, JsonValue] | None = None) -> Artifact: ...

    def get_bytes(self, artifact_id: str) -> bytes: ...

    def get(self, artifact_id: str, media_type: str | None = None) -> Artifact: ...

    def register_descriptor(self, artifact: Artifact) -> None: ...

    def get_descriptor(self, artifact_id: str, descriptor_key: str) -> Artifact: ...

    def descriptors(self, artifact_id: str) -> tuple[Artifact, ...]: ...

    def inventory_page(self, *, cursor: str | None = None, limit: int = 40) -> InventoryPage: ...

    def delete_inventory_entry(self, entry: InventoryEntry) -> bool: ...

    def deletion_snapshot(
        self, artifact_id: str, *, limit: int = 40
    ) -> tuple[InventoryEntry, ...]: ...

    def delete_snapshot(self, entries: tuple[InventoryEntry, ...]) -> None: ...

    def open_verified(self, artifact_id: str) -> BinaryIO: ...

    def put_receipt(
        self, run_id: str, task_id: str, attempt: int, content: bytes, *, incarnation: int = 0
    ) -> None: ...

    def get_receipt(
        self, run_id: str, task_id: str, attempt: int, *, incarnation: int = 0
    ) -> bytes | None: ...

    def delete_receipts(self, run_id: str, *, limit: int = 90, incarnation: int = 0) -> bool: ...


class FileArtifactStore:
    """Immutable SHA-256 addressed objects with sidecar metadata."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.objects = self.root / "objects"
        self.objects.mkdir(parents=True, exist_ok=True)

    def put_bytes(
        self,
        content: bytes,
        *,
        modality: Modality,
        media_type: str,
        metadata: dict[str, JsonValue] | None = None,
    ) -> Artifact:
        digest = content_digest(content)
        directory = self.objects / digest[:2]
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / digest
        _write_once(target, content)
        if target.read_bytes() != content:
            raise FileExistsError(
                f"content-addressed artifact already exists with different bytes: {digest}"
            )
        artifact = Artifact(
            id=digest,
            modality=modality,
            media_type=media_type,
            size_bytes=len(content),
            uri=f"artifact://sha256/{digest}",
            metadata=metadata or {},
        )
        self.register_descriptor(artifact)
        return artifact

    def register_descriptor(self, artifact: Artifact) -> None:
        target = self._path(artifact.id)
        if not target.is_file():
            raise ArtifactNotFoundError(artifact.id)
        directory = target.parent
        sidecar = directory / f"{artifact.id}.json"
        descriptor = canonical_json(artifact)
        variant = directory / f"{artifact.id}.{sha256_digest(artifact)}.json"
        _write_once(variant, descriptor, verify_existing=True)
        _update_canonical_descriptor(sidecar, artifact, descriptor)

    def put_json(
        self,
        value: Any,
        *,
        metadata: dict[str, JsonValue] | None = None,
    ) -> Artifact:
        return self.put_bytes(
            canonical_json(value),
            modality=Modality.STRUCTURED,
            media_type="application/json",
            metadata=metadata,
        )

    def get_bytes(self, artifact_id: str) -> bytes:
        path = self._path(artifact_id)
        if not path.is_file():
            raise ArtifactNotFoundError(artifact_id)
        content = path.read_bytes()
        if content_digest(content) != artifact_id:
            raise ArtifactIntegrityError(f"stored artifact failed its SHA-256 check: {artifact_id}")
        return content

    def get(self, artifact_id: str, media_type: str | None = None) -> Artifact:
        path = self._path(artifact_id)
        sidecar = path.with_suffix(".json")
        if not path.is_file() or not sidecar.is_file():
            raise ArtifactNotFoundError(artifact_id)
        if media_type is not None:
            matches = sorted(
                (
                    Artifact.model_validate_json(candidate.read_bytes(), strict=False)
                    for candidate in path.parent.glob(f"{artifact_id}.*.json")
                ),
                key=_descriptor_sort_key,
            )
            selected = next(
                (
                    artifact
                    for artifact in matches
                    if normalize_media_type(artifact.media_type) == normalize_media_type(media_type)
                ),
                None,
            )
            if selected is not None:
                return selected
            canonical = Artifact.model_validate_json(sidecar.read_bytes(), strict=False)
            if canonical.media_type.casefold() == media_type.casefold():
                return canonical
            raise ArtifactNotFoundError(
                f"{artifact_id} has no descriptor for media type {media_type}"
            )
        return Artifact.model_validate_json(sidecar.read_bytes(), strict=False)

    def _path(self, artifact_id: str) -> Path:
        if len(artifact_id) != 64 or any(char not in "0123456789abcdef" for char in artifact_id):
            raise ArtifactNotFoundError("invalid artifact identifier")
        return self.objects / artifact_id[:2] / artifact_id

    def open_verified(self, artifact_id: str) -> BinaryIO:
        import hashlib

        path = self._path(artifact_id)
        descriptor = self.get(artifact_id)
        try:
            stream = path.open("rb")
        except FileNotFoundError as error:
            raise ArtifactNotFoundError(artifact_id) from error
        try:
            before = os.fstat(stream.fileno())
            if descriptor.id != artifact_id or before.st_size != descriptor.size_bytes:
                raise ArtifactIntegrityError("stored artifact does not match its descriptor length")
            if hashlib.file_digest(stream, "sha256").hexdigest() != artifact_id:
                raise ArtifactIntegrityError("stored artifact failed its SHA-256 check")
            after = os.fstat(stream.fileno())
            if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
                raise ArtifactIntegrityError("stored artifact changed during verification")
            stream.seek(0)
            return stream
        except BaseException:
            stream.close()
            raise

    def get_descriptor(self, artifact_id: str, descriptor_key: str) -> Artifact:
        path = self._path(artifact_id)
        self._path(descriptor_key)
        if not path.is_file():
            raise ArtifactNotFoundError(artifact_id)
        variant = path.parent / f"{artifact_id}.{descriptor_key}.json"
        # Historical stores may contain only the canonical sidecar.
        content = (variant if variant.is_file() else path.with_suffix(".json")).read_bytes()
        if content_digest(content) != descriptor_key:
            raise ArtifactIntegrityError("descriptor failed its immutable identity check")
        artifact = Artifact.model_validate_json(content, strict=False)
        if artifact.id != artifact_id or sha256_digest(artifact) != descriptor_key:
            raise ArtifactIntegrityError("descriptor does not match its requested identity")
        return artifact

    def descriptors(self, artifact_id: str) -> tuple[Artifact, ...]:
        path = self._path(artifact_id)
        descriptors = {
            sha256_digest(artifact): artifact
            for artifact in (
                self.get(artifact_id),
                *(
                    Artifact.model_validate_json(candidate.read_bytes(), strict=False)
                    for candidate in path.parent.glob(f"{artifact_id}.*.json")
                ),
            )
        }
        return tuple(sorted(descriptors.values(), key=_descriptor_sort_key))

    def object_etag(self, artifact_id: str) -> str | None:
        try:
            stat = self._path(artifact_id).stat()
        except FileNotFoundError:
            return None
        return f"{stat.st_dev}:{stat.st_ino}:{stat.st_mtime_ns}:{stat.st_size}"

    def inventory_page(self, *, cursor: str | None = None, limit: int = 40) -> InventoryPage:
        if not 1 <= limit <= 100:
            raise ValueError("artifact inventory page must contain 1-100 entries")
        entries: list[InventoryEntry] = []
        for area in (self.objects, self.root / "receipts"):
            if not area.exists():
                continue
            for path in area.rglob("*"):
                if not path.is_file() or path.is_symlink() or path.is_junction():
                    continue
                key = path.relative_to(self.root).as_posix()
                if cursor is not None and key <= cursor:
                    continue
                stat = path.stat()
                etag = f"{stat.st_dev}:{stat.st_ino}:{stat.st_mtime_ns}:{stat.st_size}"
                entry = parse_inventory_entry(key, etag)
                if entry is not None:
                    entries.append(entry)
                    if len(entries) > 2 * limit:
                        entries = heapq.nsmallest(limit + 1, entries, key=lambda item: item.key)
        selected = sorted(entries, key=lambda item: item.key)[: limit + 1]
        return InventoryPage(
            tuple(selected[:limit]), selected[limit - 1].key if len(selected) > limit else None
        )

    def delete_inventory_entry(self, entry: InventoryEntry) -> bool:
        if entry.kind != "response" or parse_inventory_entry(entry.key, entry.etag) != entry:
            raise ValueError("only an exact owned response alias may be deleted directly")
        path = self.root.joinpath(*entry.key.split("/"))
        if path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(self.root):
            raise ArtifactIntegrityError("inventory deletion cannot follow links")
        with _PUBLICATION_LOCK:
            try:
                stat = path.stat()
            except FileNotFoundError:
                return True
            current = f"{stat.st_dev}:{stat.st_ino}:{stat.st_mtime_ns}:{stat.st_size}"
            if current != entry.etag:
                return False
            path.unlink()
        return True

    def deletion_snapshot(self, artifact_id: str, *, limit: int = 40) -> tuple[InventoryEntry, ...]:
        path = self._path(artifact_id)
        candidates = [path, path.with_suffix(".json")]
        candidates.extend(heapq.nsmallest(limit, path.parent.glob(f"{artifact_id}.*.json")))
        entries = []
        for candidate in candidates[:limit]:
            if candidate.is_symlink() or candidate.is_junction():
                raise ArtifactIntegrityError("object collection cannot follow links")
            try:
                stat = candidate.stat()
            except FileNotFoundError:
                continue
            key = candidate.relative_to(self.root).as_posix()
            etag = f"{stat.st_dev}:{stat.st_ino}:{stat.st_mtime_ns}:{stat.st_size}"
            entry = parse_inventory_entry(key, etag)
            if entry is None or entry.artifact_id != artifact_id:
                raise ArtifactIntegrityError("object collection snapshot is invalid")
            entries.append(entry)
        return tuple(entries)

    def delete_snapshot(self, entries: tuple[InventoryEntry, ...]) -> None:
        with _PUBLICATION_LOCK:
            for entry in entries:
                if (
                    entry.kind not in {"object", "descriptor"}
                    or parse_inventory_entry(entry.key, entry.etag) != entry
                ):
                    raise ValueError("invalid immutable-object deletion snapshot")
                path = self.root.joinpath(*entry.key.split("/"))
                if path.is_symlink() or path.is_junction():
                    raise ArtifactIntegrityError("object deletion cannot follow links")
                try:
                    stat = path.stat()
                except FileNotFoundError:
                    continue
                current = f"{stat.st_dev}:{stat.st_ino}:{stat.st_mtime_ns}:{stat.st_size}"
                if current == entry.etag:
                    path.unlink()

    def _receipt_path(self, run_id: str, task_id: str, attempt: int, incarnation: int) -> Path:
        self._path(run_id)
        self._path(task_id)
        if not 1 <= attempt <= 20:
            raise ValueError("invalid checkpoint attempt")
        if incarnation < 0:
            raise ValueError("invalid checkpoint incarnation")
        directory = self.root / "receipts" / run_id
        if incarnation:
            directory /= f"generation-{incarnation:020d}"
        return directory / task_id / f"{attempt:010d}.checkpoint"

    def put_receipt(
        self, run_id: str, task_id: str, attempt: int, content: bytes, *, incarnation: int = 0
    ) -> None:
        if len(content) > 96 * 1024 * 1024:
            raise ValueError("response checkpoint exceeds its bounded size")
        path = self._receipt_path(run_id, task_id, attempt, incarnation)
        path.parent.mkdir(parents=True, exist_ok=True)
        document = canonical_json(
            {
                "sha256": content_digest(content),
                "content": base64.b64encode(content).decode("ascii"),
            }
        )
        _write_once(path, document, verify_existing=True)

    def get_receipt(
        self, run_id: str, task_id: str, attempt: int, *, incarnation: int = 0
    ) -> bytes | None:
        path = self._receipt_path(run_id, task_id, attempt, incarnation)
        if not path.exists():
            return None
        if path.is_symlink() or path.stat().st_size > 129 * 1024 * 1024:
            raise ArtifactIntegrityError("invalid checkpoint path or size")
        document = json.loads(path.read_bytes())
        content = base64.b64decode(document["content"], validate=True)
        if len(content) > 96 * 1024 * 1024 or content_digest(content) != document["sha256"]:
            raise ArtifactIntegrityError("response checkpoint failed its SHA-256 check")
        return content

    def delete_receipts(self, run_id: str, *, limit: int = 90, incarnation: int = 0) -> bool:
        self._path(run_id)
        directory = self.root / "receipts" / run_id
        if incarnation:
            directory /= f"generation-{incarnation:020d}"
        for removed, path in enumerate(directory.glob("*/*.checkpoint")):
            if removed >= limit:
                return False
            if path.is_symlink() or path.parent.is_symlink():
                raise ArtifactIntegrityError("checkpoint cleanup cannot follow links")
            self._path(path.parent.name)
            path.unlink(missing_ok=True)
        if directory.exists():
            for child in directory.iterdir():
                if len(child.name) != 64:
                    continue
                if child.is_dir() and not any(child.iterdir()):
                    child.rmdir()
            if not any(directory.iterdir()):
                directory.rmdir()
        return True

    def delete(self, artifact_id: str, *, etag: str | None = None) -> None:
        """Called only while the lifecycle service holds the deletion generation."""
        path = self._path(artifact_id)
        with _PUBLICATION_LOCK:
            current = self.object_etag(artifact_id)
            if current is not None and etag is not None and current != etag:
                raise FileExistsError("artifact generation changed before conditional deletion")
            path.unlink(missing_ok=True)
            path.with_suffix(".json").unlink(missing_ok=True)
            for variant in path.parent.glob(f"{artifact_id}.*.json"):
                variant.unlink(missing_ok=True)


def artifact_json(store: ArtifactStore, artifact_id: str) -> JsonValue:
    return cast(JsonValue, json.loads(store.get_bytes(artifact_id)))


def _write_once(path: Path, content: bytes, *, verify_existing: bool = False) -> None:
    with _PUBLICATION_LOCK:
        if path.exists():
            if verify_existing and path.read_bytes() != content:
                raise FileExistsError(
                    f"immutable file already exists with different content: {path}"
                )
            return
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if verify_existing and path.read_bytes() != content:
                    raise FileExistsError("immutable file publication conflict") from None
            if path.read_bytes() != content:
                raise OSError(f"atomic publication verification failed: {path}")
        finally:
            temporary.unlink(missing_ok=True)


def _descriptor_sort_key(artifact: Artifact) -> tuple[str, str, bytes]:
    return (
        normalize_media_type(artifact.media_type),
        artifact.modality.value,
        canonical_json(artifact),
    )


def _update_canonical_descriptor(
    path: Path,
    artifact: Artifact,
    content: bytes,
) -> None:
    with _PUBLICATION_LOCK:
        if path.exists():
            existing = Artifact.model_validate_json(path.read_bytes(), strict=False)
            if _descriptor_sort_key(existing) <= _descriptor_sort_key(artifact):
                return
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
