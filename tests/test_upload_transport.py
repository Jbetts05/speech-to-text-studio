from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.types import Message, Scope

from ai_bench.api_policy import ApiPolicy, RequestPolicyMiddleware
from ai_bench.api_upload_transport import receive_reserved_file


def _body(size: int) -> Iterator[bytes]:
    remaining = size
    while remaining:
        chunk = b"x" * min(remaining, 64 * 1024)
        remaining -= len(chunk)
        yield chunk


def _digest(size: int) -> str:
    digest = hashlib.sha256()
    for chunk in _body(size):
        digest.update(chunk)
    return digest.hexdigest()


def _app(
    size: int,
    *,
    digest: str | None = None,
    paths: list[Path] | None = None,
) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestPolicyMiddleware, policy=ApiPolicy())

    @app.put("/api/dataset-uploads/session/parts/file")
    async def part(request: Request) -> dict[str, object]:
        async with receive_reserved_file(
            request,
            size_bytes=size,
            sha256=digest or _digest(size),
        ) as received:
            assert received.path.stat().st_size == size
            if paths is not None:
                paths.append(received.path)
            return {"size_bytes": received.size_bytes, "sha256": received.sha256}

    return app


@pytest.mark.parametrize("size", [16_384, 25_000_000])
def test_real_full_limit_file_is_received_and_spool_removed(size: int) -> None:
    paths: list[Path] = []
    with TestClient(_app(size, paths=paths)) as client:
        response = client.put(
            "/api/dataset-uploads/session/parts/file",
            content=_body(size),
            headers={"Content-Type": "application/octet-stream"},
        )
    assert response.status_code == 200
    assert response.json() == {"size_bytes": size, "sha256": _digest(size)}
    assert len(paths) == 1 and not paths[0].exists() and not paths[0].parent.exists()


@pytest.mark.parametrize(
    ("reserved", "actual", "expected_status"),
    [(16_384, 16_385, 413), (25_000_000, 25_000_001, 413), (1_000, 999, 400)],
)
def test_raw_count_cannot_exceed_or_fall_short_of_reservation(
    reserved: int, actual: int, expected_status: int
) -> None:
    client = TestClient(_app(reserved))
    response = client.put(
        "/api/dataset-uploads/session/parts/file",
        content=_body(actual),
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == expected_status


def test_hash_conflict_cannot_be_published() -> None:
    paths: list[Path] = []
    client = TestClient(_app(3, digest=hashlib.sha256(b"old").hexdigest(), paths=paths))
    response = client.put(
        "/api/dataset-uploads/session/parts/file",
        content=b"new",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 422
    assert not paths


@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Type": "multipart/form-data"},
        {"Content-Type": "application/octet-stream", "Content-Encoding": "gzip"},
    ],
)
def test_no_multipart_dataset_or_hidden_http_expansion(headers: dict[str, str]) -> None:
    client = TestClient(_app(3))
    response = client.put(
        "/api/dataset-uploads/session/parts/file", content=b"xxx", headers=headers
    )
    assert response.status_code == 415


def test_disconnected_part_does_not_reach_durable_completion() -> None:
    paths: list[Path] = []
    captured: list[Message] = []

    async def run() -> None:
        app = _app(10, paths=paths)
        messages: list[Message] = [
            {"type": "http.request", "body": b"xxx", "more_body": True},
            {"type": "http.disconnect"},
        ]
        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "PUT",
            "scheme": "http",
            "path": "/api/dataset-uploads/session/parts/file",
            "raw_path": b"/api/dataset-uploads/session/parts/file",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"content-type", b"application/octet-stream")],
            "client": ("127.0.0.1", 1),
            "server": ("testserver", 80),
        }

        async def receive() -> Message:
            return messages.pop(0)

        async def send(message: Message) -> None:
            captured.append(message)

        await app(scope, receive, send)

    asyncio.run(run())
    assert not paths
    assert next(item["status"] for item in captured if item["type"] == "http.response.start") == 400
