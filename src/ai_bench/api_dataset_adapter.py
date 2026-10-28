"""Translate generic durable dataset operations into the public wire contract."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from ai_bench.api_contracts import (
    CreateUploadRequest,
    DatasetImportDescription,
    DatasetPageResponse,
    DatasetSummaryResponse,
    UploadPartResponse,
    UploadSessionResponse,
)
from ai_bench.api_datasets import DatasetHttpError, PartReception
from ai_bench.api_upload_transport import ReceivedFile
from ai_bench.artifacts import ArtifactIntegrityError, ArtifactNotFoundError
from ai_bench.contracts import ArtifactEvidence
from ai_bench.datasets import DatasetRecord
from ai_bench.engine import BenchmarkEngine
from ai_bench.hashing import canonical_json, sha256_digest
from ai_bench.lifecycle import ExpiredRootError, RootUnavailableError
from ai_bench.packs import PackNotFoundError, get_pack
from ai_bench.repositories import IdempotencyConflictError, LeaseLostError, RepositoryError
from ai_bench.storage_state import StateConflictError
from ai_bench.uploads import (
    UploadCapacityError,
    UploadFileReservation,
    UploadPartLease,
    UploadSession,
)


def _translate[ResultT](operation: Callable[[], ResultT]) -> ResultT:
    try:
        return operation()
    except ExpiredRootError as error:
        raise DatasetHttpError(410, "Dataset or upload session expired.") from error
    except (RootUnavailableError, PackNotFoundError) as error:
        raise DatasetHttpError(404, "Dataset, upload session or benchmark not found.") from error
    except UploadCapacityError as error:
        raise DatasetHttpError(429, str(error), retry_after=error.retry_after) from error
    except (IdempotencyConflictError, LeaseLostError) as error:
        raise DatasetHttpError(409, str(error)) from error
    except (ArtifactIntegrityError, ArtifactNotFoundError) as error:
        raise DatasetHttpError(409, "Stored dataset evidence is unavailable or corrupt.") from error
    except (ValidationError, StateConflictError) as error:
        raise DatasetHttpError(
            503, "Dataset storage is temporarily unavailable.", retry_after=5
        ) from (error)
    except ValueError as error:
        raise DatasetHttpError(422, str(error)) from error
    except LookupError as error:
        if type(error) is not LookupError:
            raise
        raise DatasetHttpError(404, "Upload session or reserved part not found.") from error


def import_description(
    engine: BenchmarkEngine, benchmark_id: str
) -> DatasetImportDescription | None:
    pack = get_pack(benchmark_id, engine.pack_root)
    if getattr(pack, "dataset_importer", None) is None:
        return None
    importer = engine.dataset_importer(benchmark_id)
    return DatasetImportDescription.model_validate_json(canonical_json(dict(importer.descriptor)))


def _session(session: UploadSession) -> UploadSessionResponse:
    return UploadSessionResponse(
        upload_id=session.upload_id,
        benchmark_id=session.benchmark_id,
        name=session.name,
        status=session.status,
        created_at=session.created_at,
        expires_at=session.expires_at,
        parts=tuple(
            UploadPartResponse(
                part_id=part.part_id,
                name=part.name,
                size_bytes=part.size_bytes,
                sha256=part.sha256,
                media_type=part.media_type,
                status=part.status,
            )
            for part in session.parts
        ),
        dataset_id=session.dataset_id,
        error=session.error,
    )


class CoreDatasetHttpService:
    def __init__(
        self,
        engine: BenchmarkEngine,
        *,
        ensure_writable: Callable[[], None],
        admit_upload: Callable[[str], None],
    ) -> None:
        self.engine = engine
        self.ensure_writable = ensure_writable
        self.admit_upload = admit_upload
        self.owner = f"http-upload-{uuid4().hex}"

    def upload_key_exists(self, key: str) -> bool:
        return _translate(
            lambda: self.engine.backend.state.get(f"upload-key:{sha256_digest(key)}") is not None
        )

    def create_upload(self, request: CreateUploadRequest, key: str) -> UploadSessionResponse:
        def create() -> UploadSessionResponse:
            self.ensure_writable()
            self.admit_upload(key)
            importer = self.engine.dataset_importer(request.benchmark_id)
            reservations = tuple(
                UploadFileReservation(
                    name=file.name,
                    size_bytes=file.size_bytes,
                    sha256=file.sha256,
                    media_type=file.media_type,
                )
                for file in request.files
            )
            return _session(
                self.engine.uploads.admit(
                    benchmark_id=request.benchmark_id,
                    name=request.name,
                    language=request.language,
                    files=reservations,
                    idempotency_key=key,
                    importer=importer,
                )
            )

        return _translate(create)

    def get_upload(self, upload_id: str) -> UploadSessionResponse:
        return _translate(lambda: _session(self.engine.uploads.get(upload_id)))

    def begin_part(self, upload_id: str, part_id: str) -> PartReception[UploadPartLease]:
        def begin() -> PartReception[UploadPartLease]:
            self.ensure_writable()
            lease = self.engine.uploads.claim_part(
                upload_id, part_id, self.owner, receive_replay=True
            )
            if lease is None:
                raise RepositoryError("upload replay did not reserve its receiver")
            return PartReception(
                part=UploadPartResponse(
                    part_id=lease.part.part_id,
                    name=lease.part.name,
                    size_bytes=lease.part.size_bytes,
                    sha256=lease.part.sha256,
                    media_type=lease.part.media_type,
                    status=lease.part.status,
                ),
                lease=lease,
            )

        return _translate(begin)

    def receiver_workspace(self, lease: UploadPartLease) -> AbstractContextManager[Path]:
        return self.engine.uploads.receiver_workspace(lease)

    def complete_part(self, lease: UploadPartLease, source: ReceivedFile) -> UploadSessionResponse:
        return _translate(lambda: _session(self.engine.uploads.complete_part(lease, source.path)))

    def release_receiver(self, lease: UploadPartLease) -> None:
        _translate(lambda: self.engine.uploads.release_part(lease))

    def finalize_upload(self, upload_id: str) -> UploadSessionResponse:
        def finalize() -> UploadSessionResponse:
            self.ensure_writable()
            session = self.engine.uploads.get(upload_id)
            if any(part.status != "complete" for part in session.parts):
                raise DatasetHttpError(409, "All reserved parts must be complete before finalize.")
            return _session(self.engine.uploads.finalize(upload_id))

        return _translate(finalize)

    def list_datasets(
        self, benchmark_id: str, limit: int, cursor: str | None
    ) -> DatasetPageResponse:
        def get_page() -> DatasetPageResponse:
            get_pack(benchmark_id, self.engine.pack_root)
            page = self.engine.datasets.list(benchmark_id, cursor=cursor, limit=limit)
            return DatasetPageResponse(
                items=tuple(self._summary(record, detail=False) for record in page.items),
                next_cursor=page.next_cursor,
            )

        return _translate(get_page)

    def get_dataset(self, dataset_id: str) -> DatasetSummaryResponse:
        return _translate(lambda: self._summary(self.engine.datasets.get(dataset_id), detail=True))

    def _summary(self, record: DatasetRecord, *, detail: bool) -> DatasetSummaryResponse:
        artifacts: dict[str, tuple[ArtifactEvidence, ...]] = {}
        if detail:
            dataset = self.engine.datasets.resolve(record.id, record.benchmark_id)
            for sample in dataset.samples():
                evidence = [
                    ArtifactEvidence(
                        role="input",
                        artifact_id=sample.input.id,
                        modality=sample.input.modality,
                        media_type=sample.input.media_type,
                        size_bytes=sample.input.size_bytes,
                    )
                ]
                if sample.reference is not None:
                    evidence.append(
                        ArtifactEvidence(
                            role="reference",
                            artifact_id=sample.reference.id,
                            modality=sample.reference.modality,
                            media_type=sample.reference.media_type,
                            size_bytes=sample.reference.size_bytes,
                        )
                    )
                artifacts[sample.id] = tuple(evidence)
        return DatasetSummaryResponse(
            id=record.id,
            benchmark_id=record.benchmark_id,
            name=record.name or record.revision.id,
            revision=record.revision,
            sample_count=record.sample_count,
            scored_sample_count=record.scored_sample_count,
            published_at=record.published_at,
            expires_at=record.expires_at,
            pinned=record.pinned,
            sample_artifacts=artifacts,
        )
