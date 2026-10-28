"""HTTP transport for durable, independently resumable dataset upload sessions."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass
from pathlib import Path as FilesystemPath
from typing import Annotated, Literal, Protocol

from fastapi import FastAPI, Header, HTTPException, Path, Query, Request

from ai_bench.api_contracts import (
    CreateUploadRequest,
    DatasetPageResponse,
    DatasetSummaryResponse,
    UploadPartResponse,
    UploadSessionResponse,
)
from ai_bench.api_policy import ApiPolicy
from ai_bench.api_upload_transport import ReceivedFile, receive_reserved_file

_IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9_-]{2,95}$"
_PART_IDENTIFIER = r"^[A-Za-z0-9_-]{1,80}$"
type DatasetHttpStatus = Literal[400, 404, 409, 410, 413, 415, 422, 429, 503]


class DatasetHttpError(Exception):
    def __init__(
        self,
        status_code: DatasetHttpStatus,
        detail: str,
        *,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.retry_after = retry_after


@dataclass(frozen=True)
class PartReception[LeaseT]:
    part: UploadPartResponse
    lease: LeaseT


class DatasetHttpService[LeaseT](Protocol):
    def upload_key_exists(self, key: str) -> bool: ...

    def create_upload(self, request: CreateUploadRequest, key: str) -> UploadSessionResponse: ...

    def get_upload(self, upload_id: str) -> UploadSessionResponse: ...

    def begin_part(self, upload_id: str, part_id: str) -> PartReception[LeaseT]: ...

    def complete_part(self, lease: LeaseT, source: ReceivedFile) -> UploadSessionResponse: ...

    def receiver_workspace(self, lease: LeaseT) -> AbstractContextManager[FilesystemPath]: ...

    def release_receiver(self, lease: LeaseT) -> None: ...

    def finalize_upload(self, upload_id: str) -> UploadSessionResponse: ...

    def list_datasets(
        self, benchmark_id: str, limit: int, cursor: str | None
    ) -> DatasetPageResponse: ...

    def get_dataset(self, dataset_id: str) -> DatasetSummaryResponse: ...


async def _call[ResultT](operation: Callable[[], ResultT]) -> ResultT:
    try:
        return await asyncio.to_thread(operation)
    except DatasetHttpError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail=error.detail,
            headers=(
                {"Retry-After": str(error.retry_after)} if error.retry_after is not None else None
            ),
        ) from error


def register_dataset_routes[LeaseT](
    app: FastAPI,
    service: DatasetHttpService[LeaseT],
    *,
    policy: ApiPolicy,
    wake_worker: Callable[[], None],
) -> None:
    @app.post("/api/dataset-uploads", status_code=201)
    async def create_upload(
        request: CreateUploadRequest,
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key",
                min_length=1,
                max_length=200,
                pattern=r"^[\x21-\x7e]+$",
            ),
        ],
    ) -> UploadSessionResponse:
        known = await _call(lambda: service.upload_key_exists(idempotency_key))
        policy.validate_key(idempotency_key, known=known)
        return await _call(lambda: service.create_upload(request, idempotency_key))

    @app.get("/api/dataset-uploads/{upload_id}")
    async def upload_status(
        upload_id: Annotated[str, Path(pattern=_IDENTIFIER)],
    ) -> UploadSessionResponse:
        return await _call(lambda: service.get_upload(upload_id))

    @app.put(
        "/api/dataset-uploads/{upload_id}/parts/{part_id}",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
                },
            }
        },
    )
    async def upload_part(
        request: Request,
        upload_id: Annotated[str, Path(pattern=_IDENTIFIER)],
        part_id: Annotated[str, Path(pattern=_PART_IDENTIFIER)],
    ) -> UploadSessionResponse:
        reception = await _call(lambda: service.begin_part(upload_id, part_id))
        resources = ExitStack()
        try:
            workspace = await _call(
                lambda: resources.enter_context(service.receiver_workspace(reception.lease))
            )
            async with receive_reserved_file(
                request,
                size_bytes=reception.part.size_bytes,
                sha256=reception.part.sha256,
                directory=workspace,
            ) as source:
                return await _call(lambda: service.complete_part(reception.lease, source))
        finally:
            try:
                await asyncio.to_thread(resources.close)
            finally:
                await _call(lambda: service.release_receiver(reception.lease))

    @app.post("/api/dataset-uploads/{upload_id}/finalize", status_code=202)
    async def finalize_upload(
        upload_id: Annotated[str, Path(pattern=_IDENTIFIER)],
        request: Request,
    ) -> UploadSessionResponse:
        body = await request.body()
        if body.strip():
            try:
                payload = json.loads(body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise HTTPException(
                    status_code=422, detail="Finalize accepts only an empty JSON object."
                ) from None
            if payload != {}:
                raise HTTPException(
                    status_code=422, detail="Finalize does not accept new file metadata."
                )
        result = await _call(lambda: service.finalize_upload(upload_id))
        if result.status == "validating":
            wake_worker()
        return result

    @app.get("/api/benchmarks/{benchmark_id}/datasets")
    async def datasets(
        benchmark_id: Annotated[str, Path(pattern=r"^[a-z][a-z0-9_-]{2,63}$")],
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> DatasetPageResponse:
        return await _call(lambda: service.list_datasets(benchmark_id, limit, cursor))

    @app.get("/api/datasets/{dataset_id}")
    async def dataset_detail(
        dataset_id: Annotated[str, Path(pattern=_IDENTIFIER)],
    ) -> DatasetSummaryResponse:
        return await _call(lambda: service.get_dataset(dataset_id))
