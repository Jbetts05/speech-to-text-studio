"""Durable bounded upload reservations, reception leases and validation jobs."""

from __future__ import annotations

import hashlib
import shutil
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field

from ai_bench.artifacts import ArtifactStore
from ai_bench.azure_artifacts import AzureBlobArtifactStore
from ai_bench.contracts import Artifact, Contract, Modality
from ai_bench.dataset_imports import DatasetAdmissionValidator, DatasetImporter, DatasetImportFile
from ai_bench.datasets import DatasetCatalog
from ai_bench.hashing import canonical_json, sha256_digest
from ai_bench.lifecycle import LifecycleRoot, LifecycleService, root_key
from ai_bench.repositories import IdempotencyConflictError, LeaseLostError
from ai_bench.runtime_policy import PublicExecutionPolicy
from ai_bench.storage_state import PrefixedStateStore, StateChange, StateConflictError, StateStore


class UploadCapacityError(RuntimeError):
    retry_after = 5


class UploadFileReservation(Contract):
    name: str = Field(min_length=1, max_length=120)
    size_bytes: int = Field(gt=0, le=25_000_000)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    media_type: str | None = Field(default=None, max_length=120)


class UploadPart(UploadFileReservation):
    part_id: str
    status: Literal["pending", "receiving", "complete"] = "pending"


class UploadSession(Contract):
    upload_id: str
    benchmark_id: str
    name: str
    language: str
    status: Literal["receiving", "validating", "ready", "failed"]
    created_at: datetime
    expires_at: datetime
    parts: tuple[UploadPart, ...]
    dataset_id: str | None = None
    error: str | None = None


class UploadPartLease(Contract):
    upload_id: str
    part_id: str
    owner: str
    token: str
    until: float
    part: UploadPart


class UploadService:
    def __init__(
        self,
        state: StateStore,
        staging: ArtifactStore,
        lifecycle: LifecycleService,
        catalog: DatasetCatalog,
        work_root: Path,
        admission_guard: Callable[[], None] | None = None,
        public_policy: PublicExecutionPolicy | None = None,
    ) -> None:
        self.state = state
        self.staging = staging
        self.lifecycle = lifecycle
        self.catalog = catalog
        self.work_root = work_root
        self.admission_guard = admission_guard
        self.public_policy = public_policy or PublicExecutionPolicy()
        self.staging_lifecycle = LifecycleService(
            PrefixedStateStore(state, "staging:"), staging, clock=lifecycle.clock
        )

    def _guard(self, upload_id: str) -> StateChange:
        row = self.state.get(root_key(upload_id))
        self.lifecycle.require_live(upload_id, ready=False)
        assert row is not None
        return StateChange(row.key, row.values, row.etag)

    def create_session(
        self,
        *,
        benchmark_id: str,
        name: str,
        language: str,
        files: tuple[UploadFileReservation, ...],
        idempotency_key: str,
        importer: DatasetImporter,
    ) -> UploadSession:
        """Reserve metadata only; original bytes arrive in separate leased file requests."""
        return self.admit(
            benchmark_id=benchmark_id,
            name=name,
            language=language,
            files=files,
            idempotency_key=idempotency_key,
            importer=importer,
        )

    def status(self, upload_id: str) -> UploadSession:
        return self.get(upload_id)

    def receive_file(self, lease: UploadPartLease, path: Path) -> UploadSession:
        """Complete one bounded server-owned file, never a whole dataset body."""
        return self.complete_part(lease, path)

    def create_upload_session(
        self,
        *,
        benchmark_id: str,
        name: str,
        language: str,
        specs: tuple[UploadFileReservation, ...],
        idempotency_key: str,
        importer: DatasetImporter,
    ) -> UploadSession:
        return self.create_session(
            benchmark_id=benchmark_id,
            name=name,
            language=language,
            files=specs,
            idempotency_key=idempotency_key,
            importer=importer,
        )

    def claim_upload_part(
        self, session_id: str, part_id: str, owner: str, *, receive_replay: bool = False
    ) -> UploadPartLease | None:
        return self.claim_part(session_id, part_id, owner, receive_replay=receive_replay)

    def complete_upload_part(
        self, lease: UploadPartLease, file: DatasetImportFile
    ) -> UploadSession:
        if (
            file.name != lease.part.name
            or file.size_bytes != lease.part.size_bytes
            or file.sha256 != lease.part.sha256
            or file.media_type != lease.part.media_type
        ):
            raise IdempotencyConflictError("received file metadata differs from its reservation")
        return self.complete_part(lease, file.path)

    def release_upload_part(self, lease: UploadPartLease) -> None:
        self.release_part(lease)

    def finalize_upload(self, session_id: str) -> UploadSession:
        return self.finalize(session_id)

    def get_upload(self, session_id: str) -> UploadSession:
        return self.get(session_id)

    def admit(
        self,
        *,
        benchmark_id: str,
        name: str,
        language: str,
        files: tuple[UploadFileReservation, ...],
        idempotency_key: str,
        importer: DatasetImporter,
    ) -> UploadSession:
        if self.admission_guard is not None:
            self.admission_guard()
        descriptor = importer.descriptor
        if not files or len(files) > min(20, int(str(descriptor["max_files"]))):
            raise ValueError("upload file count exceeds importer limits")
        if not name or len(name) > 80 or language != descriptor["language"]:
            raise ValueError("invalid upload name or language")
        if (
            len(
                canonical_json(
                    {
                        "benchmark_id": benchmark_id,
                        "name": name,
                        "language": language,
                        "files": files,
                    }
                )
            )
            > 32 * 1024
        ):
            raise ValueError("upload admission metadata exceeds 32 KiB")
        if isinstance(importer, DatasetAdmissionValidator):
            importer.validate_admission(files)
        seen: set[str] = set()
        input_extensions = descriptor["input_extensions"]
        reference_extensions = descriptor["reference_extensions"]
        if not isinstance(input_extensions, list | tuple) or not isinstance(
            reference_extensions, list | tuple
        ):
            raise ValueError("invalid importer extension descriptor")
        for part in files:
            folded = unicodedata.normalize("NFKC", part.name).casefold()
            if (
                folded in seen
                or part.name != Path(part.name).name
                or any(character in part.name for character in "/\\:")
                or any(unicodedata.category(character).startswith("C") for character in part.name)
                or part.name in {".", ".."}
            ):
                raise ValueError("unsafe or colliding filename")
            seen.add(folded)
            extension = Path(part.name).suffix.lower()
            ceiling = (
                descriptor["max_input_bytes"]
                if extension in input_extensions
                else descriptor["max_reference_bytes"]
                if extension in reference_extensions
                else 0
            )
            if part.size_bytes > int(str(ceiling)):
                raise ValueError("file extension or size is not admitted")
        total = sum(part.size_bytes for part in files)
        if total > min(250_000_000, int(str(descriptor["max_total_bytes"]))):
            raise ValueError("upload aggregate reservation exceeds importer limits")
        key_hash = sha256_digest(idempotency_key)
        request_hash = sha256_digest(
            {
                "benchmark_id": benchmark_id,
                "name": name,
                "language": language,
                "files": files,
                "importer": descriptor,
            }
        )
        upload_id = sha256_digest(
            {
                "upload_key": key_hash,
                "request_hash": request_hash,
                "nonce": uuid4().hex,
            }
        )
        for _ in range(20):
            existing = self.state.get(f"upload-key:{key_hash}")
            if existing is not None:
                if existing.values["request_hash"] != request_hash:
                    raise IdempotencyConflictError("upload idempotency key has a different request")
                return self.get(str(existing.values["upload_id"]))
            root = self.lifecycle.create(upload_id, "upload")
            self.staging_lifecycle.create(upload_id, "upload", accepted_at=root.created_at)
            assert root.expires_at is not None
            session = UploadSession(
                upload_id=upload_id,
                benchmark_id=benchmark_id,
                name=name,
                language=language,
                status="receiving",
                created_at=root.created_at,
                expires_at=root.expires_at,
                parts=tuple(
                    UploadPart(
                        **part.model_dump(),
                        part_id=sha256_digest({"upload_id": upload_id, "part": index}),
                    )
                    for index, part in enumerate(files)
                ),
            )
            admission = self.state.get("upload-admission")
            sessions = 0 if admission is None else int(str(admission.values["sessions"]))
            reserved = 0 if admission is None else int(str(admission.values["bytes"]))
            if (
                sessions >= self.public_policy.max_upload_sessions
                or reserved + total > self.public_policy.max_reserved_upload_bytes
            ):
                raise UploadCapacityError("validation backlog is full; retry later")
            try:
                self.state.transact(
                    (
                        StateChange(
                            f"upload:{upload_id}",
                            {
                                "session": session.model_dump(mode="json"),
                                "key_hash": key_hash,
                                "reserved_bytes": total,
                                "request_hash": request_hash,
                                "admission_released": False,
                            },
                        ),
                        StateChange(
                            f"upload-key:{key_hash}",
                            {
                                "request_hash": request_hash,
                                "upload_id": upload_id,
                            },
                        ),
                        StateChange(
                            "upload-admission",
                            {
                                "sessions": sessions + 1,
                                "bytes": reserved + total,
                            },
                            None if admission is None else admission.etag,
                        ),
                        self._guard(upload_id),
                    )
                )
                return session
            except StateConflictError:
                continue
        raise StateConflictError("upload admission contention")

    def get(self, upload_id: str) -> UploadSession:
        self.lifecycle.require_live(upload_id, ready=False)
        row = self.state.get(f"upload:{upload_id}")
        if row is None:
            raise LookupError(upload_id)
        session = UploadSession.model_validate(row.values["session"], strict=False)
        parts = []
        for part in session.parts:
            completed = self.state.get(f"upload-part:{upload_id}:{part.part_id}")
            status: Literal["pending", "receiving", "complete"] = "pending"
            if completed is not None:
                status = "complete"
            elif (lease := self.state.get(f"upload-receiver:{part.part_id}")) is not None and float(
                str(lease.values["until"])
            ) > self.lifecycle.clock().timestamp():
                status = "receiving"
            parts.append(part.model_copy(update={"status": status}))
        return session.model_copy(update={"parts": tuple(parts)})

    def claim_part(
        self, upload_id: str, part_id: str, owner: str, *, receive_replay: bool = False
    ) -> UploadPartLease | None:
        session = self.get(upload_id)
        part = next((part for part in session.parts if part.part_id == part_id), None)
        if part is None:
            raise LookupError("unknown reserved upload part")
        if part.status == "complete" and not receive_replay:
            return None
        if session.status != "receiving" and part.status != "complete":
            raise ValueError("upload no longer accepts parts")
        for _ in range(20):
            slots = self.state.get("upload-receivers")
            now = self.lifecycle.clock().timestamp()
            active = {} if slots is None else dict(slots.values)
            active = {
                key: value
                for key, value in active.items()
                if isinstance(value, dict) and float(str(value["until"])) > now
            }
            if len(active) >= self.public_policy.max_receivers:
                raise UploadCapacityError("file receiver capacity is temporarily full")
            row = self.state.get(f"upload-receiver:{part_id}")
            if row is not None and float(str(row.values["until"])) > now:
                raise UploadCapacityError("this file is already being received")
            token = uuid4().hex
            until = now + 900
            active[part_id] = {"until": until, "token": token}
            lease = UploadPartLease(
                upload_id=upload_id,
                part_id=part_id,
                owner=owner,
                token=token,
                until=until,
                part=part,
            )
            try:
                self.state.transact(
                    (
                        StateChange(
                            "upload-receivers", active, None if slots is None else slots.etag
                        ),
                        StateChange(
                            f"upload-receiver:{part_id}",
                            {
                                "owner": owner,
                                "token": token,
                                "until": until,
                                "upload_id": upload_id,
                            },
                            None if row is None else row.etag,
                        ),
                        self._guard(upload_id),
                    )
                )
                return lease
            except StateConflictError:
                continue
        raise StateConflictError("receiver reservation contention")

    @contextmanager
    def receiver_workspace(self, lease: UploadPartLease) -> Iterator[Path]:
        receiver = self.state.get(f"upload-receiver:{lease.part_id}")
        if (
            receiver is None
            or receiver.values["token"] != lease.token
            or float(str(receiver.values["until"])) <= self.lifecycle.clock().timestamp()
        ):
            raise LeaseLostError("upload receiver lease expired or changed")
        key = f"upload-work:{lease.upload_id}:{lease.token}"
        self.state.transact(
            (
                StateChange(key, {"token": lease.token, "kind": "receiver"}),
                StateChange(receiver.key, receiver.values, receiver.etag),
                self._guard(lease.upload_id),
            )
        )
        directory = self.work_root / lease.token
        directory.mkdir(parents=True, exist_ok=False)
        try:
            yield directory
        finally:
            if directory.exists():
                shutil.rmtree(directory)
            row = self.state.get(key)
            if row is not None:
                with suppress(StateConflictError):
                    self.state.transact((StateChange(row.key, None, row.etag),))

    def complete_part(self, lease: UploadPartLease, path: Path) -> UploadSession:
        with path.open("rb") as stream:
            if (
                path.stat().st_size != lease.part.size_bytes
                or hashlib.file_digest(stream, "sha256").hexdigest() != lease.part.sha256
            ):
                raise ValueError("received bytes do not match the immutable reservation")
        existing = self.state.get(f"upload-part:{lease.upload_id}:{lease.part_id}")
        if existing is not None:
            if existing.values["artifact_id"] != lease.part.sha256:
                raise IdempotencyConflictError("reserved part contains different bytes")
            self.release_part(lease)
            return self.get(lease.upload_id)
        receiver = self.state.get(f"upload-receiver:{lease.part_id}")
        if (
            receiver is None
            or receiver.values["token"] != lease.token
            or float(str(receiver.values["until"])) <= self.lifecycle.clock().timestamp()
        ):
            raise LeaseLostError("upload receiver lease expired or changed")
        descriptor = Artifact(
            id=lease.part.sha256,
            modality=Modality.STRUCTURED,
            media_type="application/octet-stream",
            size_bytes=lease.part.size_bytes,
            uri=f"artifact://sha256/{lease.part.sha256}",
        )
        self.staging_lifecycle.attach(lease.upload_id, descriptor.id)
        if isinstance(self.staging, AzureBlobArtifactStore):
            self.staging.put_file(path, descriptor)
        else:
            self.staging.put_bytes(
                path.read_bytes(), modality=descriptor.modality, media_type=descriptor.media_type
            )
        for _ in range(20):
            key = f"upload-part:{lease.upload_id}:{lease.part_id}"
            existing = self.state.get(key)
            if existing is not None:
                if existing.values["artifact_id"] != lease.part.sha256:
                    raise IdempotencyConflictError("reserved part contains different bytes")
                return self.get(lease.upload_id)
            receiver = self.state.get(f"upload-receiver:{lease.part_id}")
            if (
                receiver is None
                or receiver.values["token"] != lease.token
                or float(str(receiver.values["until"])) <= self.lifecycle.clock().timestamp()
            ):
                raise LeaseLostError("upload receiver lease expired or changed")
            slots = self.state.get("upload-receivers")
            assert slots is not None
            active = {key: value for key, value in slots.values.items() if key != lease.part_id}
            try:
                self.state.transact(
                    (
                        StateChange(key, {"artifact_id": descriptor.id}),
                        StateChange(receiver.key, None, receiver.etag),
                        StateChange(slots.key, active, slots.etag),
                        self._guard(lease.upload_id),
                    )
                )
                return self.get(lease.upload_id)
            except StateConflictError:
                continue
        raise StateConflictError("part completion contention")

    def release_part(self, lease: UploadPartLease) -> None:
        for _ in range(20):
            receiver = self.state.get(f"upload-receiver:{lease.part_id}")
            if receiver is None or receiver.values["token"] != lease.token:
                return
            slots = self.state.get("upload-receivers")
            assert slots is not None
            try:
                self.state.transact(
                    (
                        StateChange(receiver.key, None, receiver.etag),
                        StateChange(
                            slots.key,
                            {
                                key: value
                                for key, value in slots.values.items()
                                if key != lease.part_id
                            },
                            slots.etag,
                        ),
                    )
                )
                return
            except StateConflictError:
                continue
        raise StateConflictError("receiver release contention")

    def finalize(self, upload_id: str) -> UploadSession:
        for _ in range(20):
            row = self.state.get(f"upload:{upload_id}")
            if row is None:
                raise LookupError(upload_id)
            session = self.get(upload_id)
            if session.status != "receiving":
                return session
            if any(part.status != "complete" for part in session.parts):
                raise ValueError("all reserved files must be durably complete before finalize")
            if sum(part.size_bytes for part in session.parts) != int(
                str(row.values["reserved_bytes"])
            ):
                raise ValueError("reserved upload aggregate changed")
            prefix = f"upload-part:{upload_id}:"
            completed = self.state.page(prefix, limit=len(session.parts) + 1)
            expected = {part.part_id for part in session.parts}
            if {
                part.key.removeprefix(prefix) for part in completed.rows
            } != expected or completed.cursor is not None:
                raise ValueError("completed upload parts do not match the exact reservation")
            parts_by_id = {part.key.removeprefix(prefix): part for part in completed.rows}
            for part in session.parts:
                self.lifecycle.require_live(upload_id, ready=False)
                stored = parts_by_id[part.part_id]
                if stored.values["artifact_id"] != part.sha256:
                    raise ValueError("stored part identity differs from its reservation")
                descriptor = self.staging.get(part.sha256)
                if descriptor.size_bytes != part.size_bytes:
                    raise ValueError("stored part size differs from its reservation")
                with self.staging.open_verified(part.sha256) as stream:
                    if stream.seek(0, 2) != part.size_bytes:
                        raise ValueError("stored part length differs from its reservation")
            updated = session.model_copy(update={"status": "validating"})
            try:
                self.state.transact(
                    (
                        StateChange(
                            row.key,
                            {**row.values, "session": updated.model_dump(mode="json")},
                            row.etag,
                        ),
                        StateChange(f"upload-job:{upload_id}", {"upload_id": upload_id}),
                        self._guard(upload_id),
                        *(StateChange(part.key, part.values, part.etag) for part in completed.rows),
                    )
                )
                return updated
            except StateConflictError:
                continue
        raise StateConflictError("upload finalize contention")

    def process_next(self, resolve_importer: Callable[[str], DatasetImporter]) -> bool:
        jobs = self.state.page("upload-job:", limit=1)
        if not jobs.rows:
            return False
        job = jobs.rows[0]
        upload_id = str(job.values["upload_id"])
        session = self.get(upload_id)
        validator = self.state.get("upload-validator")
        now = self.lifecycle.clock().timestamp()
        if validator is not None and float(str(validator.values["until"])) > now:
            return False
        token = uuid4().hex
        work_key = f"upload-work:{upload_id}:{token}"
        try:
            self.state.transact(
                (
                    StateChange(
                        "upload-validator",
                        {
                            "upload_id": upload_id,
                            "token": token,
                            "until": now + 900,
                        },
                        None if validator is None else validator.etag,
                    ),
                    StateChange(work_key, {"token": token}),
                    self._guard(upload_id),
                )
            )
        except StateConflictError:
            return False
        directory = self.work_root / token
        directory.mkdir(parents=True)
        try:
            importer = resolve_importer(session.benchmark_id)
            files = []
            for index, part in enumerate(session.parts):
                path = directory / f"part-{index}"
                with self.staging.open_verified(part.sha256) as stream, path.open("xb") as output:
                    shutil.copyfileobj(stream, output, length=64 * 1024)
                files.append(
                    DatasetImportFile(
                        name=part.name,
                        path=path,
                        size_bytes=part.size_bytes,
                        sha256=part.sha256,
                        media_type=part.media_type,
                    )
                )
            result = importer.import_files(tuple(files))
            for file in result.files.values():
                if not file.resolve().is_relative_to(directory.resolve()):
                    raise ValueError("importer returned a file outside its trusted staging root")
            # The validator token fences publication after a paused decoder.
            current = self.state.get("upload-validator")
            if (
                current is None
                or current.values["token"] != token
                or float(str(current.values["until"])) <= self.lifecycle.clock().timestamp()
            ):
                raise LeaseLostError("upload validator lease changed")
            published = self.catalog.publish(
                session.benchmark_id,
                upload_id,
                result,
                publication_fences=lambda: self._validation_fences(upload_id, token),
                name=session.name,
                staging_expires_at=session.expires_at,
            )
            self._finish_validation(upload_id, token, published.id, None)
        except (ValueError, UnicodeError) as error:
            self._finish_validation(
                upload_id,
                token,
                None,
                f"Validation rejected these files ({type(error).__name__}); "
                "check the importer's file roles, formats and limits.",
            )
        finally:
            shutil.rmtree(directory)
            work = self.state.get(work_key)
            if work is not None:
                self.state.transact((StateChange(work.key, None, work.etag),))
        return True

    def _validation_fences(self, upload_id: str, token: str) -> tuple[StateChange, ...]:
        current = self.state.get("upload-validator")
        if (
            current is None
            or current.values["token"] != token
            or float(str(current.values["until"])) <= self.lifecycle.clock().timestamp()
        ):
            raise LeaseLostError("upload validator generation expired or changed")
        return (
            StateChange(current.key, current.values, current.etag),
            self._guard(upload_id),
        )

    def _finish_validation(
        self, upload_id: str, token: str, dataset_id: str | None, error: str | None
    ) -> None:
        row = self.state.get(f"upload:{upload_id}")
        validator = self.state.get("upload-validator")
        job = self.state.get(f"upload-job:{upload_id}")
        if row is None or validator is None or validator.values["token"] != token:
            raise LeaseLostError("upload validation ownership changed")
        session = UploadSession.model_validate(row.values["session"], strict=False)
        updated = session.model_copy(
            update={
                "status": "ready" if dataset_id is not None else "failed",
                "dataset_id": dataset_id,
                "error": error,
            }
        )
        changes = [
            StateChange(
                row.key,
                {
                    **row.values,
                    "session": updated.model_dump(mode="json"),
                    "admission_released": True,
                },
                row.etag,
            ),
            StateChange(validator.key, {**validator.values, "until": 0}, validator.etag),
            self._guard(upload_id),
        ]
        if job is not None:
            changes.append(StateChange(job.key, None, job.etag))
        admission = self.state.get("upload-admission")
        if admission is not None and not row.values["admission_released"]:
            changes.append(
                StateChange(
                    admission.key,
                    {
                        "sessions": max(0, int(str(admission.values["sessions"])) - 1),
                        "bytes": max(
                            0,
                            int(str(admission.values["bytes"]))
                            - int(str(row.values["reserved_bytes"])),
                        ),
                    },
                    admission.etag,
                )
            )
        self.state.transact(changes)

    def cleanup_records(self, root: LifecycleRoot) -> bool:
        if root.kind != "upload":
            return True
        row = self.state.get(f"upload:{root.id}")
        if row is None:
            return True
        session = UploadSession.model_validate(row.values["session"], strict=False)
        changes = []
        work = self.state.page(f"upload-work:{root.id}:", limit=20)
        for item in work.rows:
            directory = self.work_root / str(item.values["token"])
            if directory.is_dir():
                shutil.rmtree(directory)
            changes.append(StateChange(item.key, None, item.etag))
        if work.cursor is not None:
            self.state.transact(changes)
            return False
        for part in session.parts:
            for key in (f"upload-part:{root.id}:{part.part_id}", f"upload-receiver:{part.part_id}"):
                existing = self.state.get(key)
                if existing is not None:
                    changes.append(StateChange(key, None, existing.etag))
        receivers = self.state.get("upload-receivers")
        if receivers is not None:
            part_ids = {part.part_id for part in session.parts}
            remaining = {
                key: value for key, value in receivers.values.items() if key not in part_ids
            }
            if remaining != receivers.values:
                changes.append(StateChange(receivers.key, remaining, receivers.etag))
        validator = self.state.get("upload-validator")
        if validator is not None and validator.values["upload_id"] == root.id:
            changes.append(StateChange(validator.key, None, validator.etag))
        changes.extend((StateChange(row.key, None, row.etag),))
        for key in (f"upload-key:{row.values['key_hash']}", f"upload-job:{root.id}"):
            existing = self.state.get(key)
            if existing is not None:
                changes.append(StateChange(existing.key, None, existing.etag))
        admission = self.state.get("upload-admission")
        if admission is not None and not row.values["admission_released"]:
            changes.append(
                StateChange(
                    admission.key,
                    {
                        "sessions": max(0, int(str(admission.values["sessions"])) - 1),
                        "bytes": max(
                            0,
                            int(str(admission.values["bytes"]))
                            - int(str(row.values["reserved_bytes"])),
                        ),
                    },
                    admission.etag,
                )
            )
        self.state.transact(changes)
        return True
