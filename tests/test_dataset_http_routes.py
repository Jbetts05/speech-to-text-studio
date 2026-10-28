from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_bench.api_contracts import (
    CreateUploadRequest,
    DatasetPageResponse,
    DatasetSummaryResponse,
    UploadPartResponse,
    UploadSessionResponse,
)
from ai_bench.api_datasets import (
    DatasetHttpError,
    PartReception,
    register_dataset_routes,
)
from ai_bench.api_policy import ApiPolicy, RequestPolicyMiddleware
from ai_bench.api_upload_transport import ReceivedFile


class TransportTestService:
    def __init__(self) -> None:
        self.key: str | None = None
        self.request: CreateUploadRequest | None = None
        self.session: UploadSessionResponse | None = None
        self.receivers = 0
        self.completed: dict[str, bytes] = {}
        self.validation_jobs = 0
        self.expired = False
        self.busy = False

    def upload_key_exists(self, key: str) -> bool:
        return key == self.key

    def create_upload(self, request: CreateUploadRequest, key: str) -> UploadSessionResponse:
        if self.key is not None:
            if key != self.key or request != self.request:
                raise DatasetHttpError(409, "Upload reservation conflicts.")
            return self.get_upload("upload-session")
        self.key, self.request = key, request
        now = datetime.now(UTC)
        self.session = UploadSessionResponse(
            upload_id="upload-session",
            benchmark_id=request.benchmark_id,
            name=request.name,
            status="receiving",
            created_at=now,
            expires_at=now + timedelta(hours=1),
            parts=tuple(
                UploadPartResponse(
                    part_id=f"part-{index}",
                    name=part.name,
                    size_bytes=part.size_bytes,
                    sha256=part.sha256,
                    media_type=part.media_type,
                    status="pending",
                )
                for index, part in enumerate(request.files)
            ),
        )
        return self.session

    def get_upload(self, upload_id: str) -> UploadSessionResponse:
        if self.expired:
            raise DatasetHttpError(410, "Upload session expired.")
        if self.session is None or upload_id != self.session.upload_id:
            raise DatasetHttpError(404, "Upload session not found.")
        return self.session

    def begin_part(self, upload_id: str, part_id: str) -> PartReception[str]:
        session = self.get_upload(upload_id)
        part = next((part for part in session.parts if part.part_id == part_id), None)
        if part is None:
            raise DatasetHttpError(404, "Reserved part not found.")
        if self.busy or self.receivers >= 2:
            raise DatasetHttpError(429, "Upload receivers are busy.", retry_after=2)
        self.receivers += 1
        return PartReception(part=part, lease=part_id)

    def complete_part(self, lease: str, source: ReceivedFile) -> UploadSessionResponse:
        session = self.get_upload("upload-session")
        self.completed[lease] = source.path.read_bytes()
        self.session = session.model_copy(
            update={
                "parts": tuple(
                    part.model_copy(update={"status": "complete"})
                    if part.part_id == lease
                    else part
                    for part in session.parts
                )
            }
        )
        return self.session

    @contextmanager
    def receiver_workspace(self, lease: str) -> Iterator[Path]:
        with tempfile.TemporaryDirectory(prefix="ai-bench-route-test-") as directory:
            yield Path(directory)

    def release_receiver(self, lease: str) -> None:
        self.receivers -= 1

    def finalize_upload(self, upload_id: str) -> UploadSessionResponse:
        session = self.get_upload(upload_id)
        if any(part.status != "complete" for part in session.parts):
            raise DatasetHttpError(409, "Upload has incomplete reserved parts.")
        if session.status == "receiving":
            self.validation_jobs += 1
            self.session = session.model_copy(update={"status": "validating"})
        return self.get_upload(upload_id)

    def list_datasets(
        self, benchmark_id: str, limit: int, cursor: str | None
    ) -> DatasetPageResponse:
        return DatasetPageResponse(items=())

    def get_dataset(self, dataset_id: str) -> DatasetSummaryResponse:
        raise DatasetHttpError(404, "Dataset not found.")


def _client(service: TransportTestService) -> TestClient:
    app = FastAPI()
    policy = ApiPolicy()
    app.add_middleware(RequestPolicyMiddleware, policy=policy)
    register_dataset_routes(app, service, policy=policy, wake_worker=lambda: None)
    return TestClient(app)


def _request() -> dict[str, object]:
    return {
        "benchmark_id": "generic_pack",
        "name": "Synthetic HTTP transport",
        "language": "en",
        "public_acknowledged": True,
        "rights_acknowledged": True,
        "language_acknowledged": True,
        "files": [
            {
                "name": name,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for name, content in (("sample.wav", b"audio"), ("sample.txt", b"WORDS"))
        ],
    }


def test_http_session_replays_and_resumes_parts_before_one_finalize_job() -> None:
    service = TransportTestService()
    client = _client(service)
    headers = {"Idempotency-Key": "session-key"}
    created = client.post("/api/dataset-uploads", json=_request(), headers=headers)
    assert created.status_code == 201
    assert client.post("/api/dataset-uploads", json=_request(), headers=headers).json() == (
        created.json()
    )
    base = "/api/dataset-uploads/upload-session"
    assert client.post(f"{base}/finalize", json={}).status_code == 409
    assert client.get("/api/benchmarks/generic_pack/datasets").json()["items"] == []
    part_headers = {"Content-Type": "application/octet-stream"}
    assert (
        client.put(f"{base}/parts/part-0", content=b"audio", headers=part_headers).status_code
        == 200
    )
    restored = _client(service)
    assert restored.get(base).json()["parts"][0]["status"] == "complete"
    assert (
        restored.put(f"{base}/parts/part-0", content=b"audio", headers=part_headers).status_code
        == 200
    )
    assert (
        restored.put(f"{base}/parts/part-1", content=b"WORDS", headers=part_headers).status_code
        == 200
    )
    assert restored.post(f"{base}/finalize", json={}).json()["status"] == "validating"
    assert (
        restored.post(
            f"{base}/finalize", content=b"{\n}", headers={"Content-Type": "application/json"}
        ).status_code
        == 202
    )
    assert service.validation_jobs == 1
    assert service.receivers == 0
    assert service.completed == {"part-0": b"audio", "part-1": b"WORDS"}


def test_conflicts_missing_parts_and_expiry_are_explicit() -> None:
    service = TransportTestService()
    client = _client(service)
    client.post("/api/dataset-uploads", json=_request(), headers={"Idempotency-Key": "session-key"})
    changed = {**_request(), "name": "Changed reservation"}
    assert (
        client.post(
            "/api/dataset-uploads", json=changed, headers={"Idempotency-Key": "session-key"}
        ).status_code
        == 409
    )
    base = "/api/dataset-uploads/upload-session"
    assert (
        client.put(
            f"{base}/parts/missing",
            content=b"data",
            headers={"Content-Type": "application/octet-stream"},
        ).status_code
        == 404
    )
    assert client.post(f"{base}/finalize", json={"replace": True}).status_code == 422
    assert service.receivers == 0
    service.expired = True
    assert client.get(base).status_code == 410
    assert client.post(f"{base}/finalize", json={}).status_code == 410


@pytest.mark.parametrize("body", [b"wrong", b"too much", b"tiny"])
def test_invalid_part_releases_receiver_without_completing(body: bytes) -> None:
    service = TransportTestService()
    client = _client(service)
    client.post("/api/dataset-uploads", json=_request(), headers={"Idempotency-Key": "session-key"})
    response = client.put(
        "/api/dataset-uploads/upload-session/parts/part-0",
        content=body,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code in {400, 413, 422}
    assert service.receivers == 0
    assert not service.completed


def test_receiver_backpressure_has_retry_after_and_does_not_accept_body() -> None:
    service = TransportTestService()
    client = _client(service)
    client.post("/api/dataset-uploads", json=_request(), headers={"Idempotency-Key": "session-key"})
    service.busy = True
    response = client.put(
        "/api/dataset-uploads/upload-session/parts/part-0",
        content=b"audio",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 429
    assert response.headers["retry-after"] == "2"
    assert service.receivers == 0 and not service.completed
