from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI, Header, Request
from fastapi.testclient import TestClient
from starlette.responses import Response

from ai_bench.api_artifacts import artifact_response
from ai_bench.artifacts import ArtifactIntegrityError
from ai_bench.hashing import content_digest

CONTENT = b"synthetic bounded artifact"
IDENTITY = content_digest(CONTENT)


def _client(
    *,
    media_type: str = "audio/wav",
    expires_at: datetime | None = None,
) -> tuple[TestClient, list[io.BytesIO]]:
    app = FastAPI()
    streams: list[io.BytesIO] = []

    @app.api_route("/artifact", methods=["GET", "HEAD"])
    def artifact(
        request: Request,
        range_value: str | None = Header(default=None, alias="Range"),
        if_range: str | None = Header(default=None, alias="If-Range"),
        if_none_match: str | None = Header(default=None, alias="If-None-Match"),
        download: bool = False,
    ) -> Response:
        source = io.BytesIO(CONTENT)
        streams.append(source)
        return artifact_response(
            source,
            artifact_id=IDENTITY,
            size_bytes=len(CONTENT),
            media_type=media_type,
            method=request.method,
            range_header=range_value,
            if_range=if_range,
            if_none_match=if_none_match,
            download=download,
            expires_at=expires_at,
        )

    return TestClient(app), streams


@pytest.mark.parametrize("media_type", ["audio/flac", "audio/wav", "audio/mpeg"])
def test_native_audio_and_explicit_download(media_type: str) -> None:
    client, streams = _client(media_type=media_type)
    response = client.get("/artifact")
    assert response.status_code == 200
    assert response.content == CONTENT
    assert response.headers["content-type"] == media_type
    assert response.headers["content-disposition"].startswith("inline;")
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["cache-control"] == "no-store"
    assert (
        client.get("/artifact?download=true")
        .headers["content-disposition"]
        .startswith("attachment;")
    )
    assert all(stream.closed for stream in streams)


@pytest.mark.parametrize(
    ("header", "start", "end"),
    [
        ("bytes=0-3", 0, 3),
        ("bytes=4-", 4, len(CONTENT) - 1),
        ("bytes=-5", len(CONTENT) - 5, len(CONTENT) - 1),
        ("bytes=0-9999", 0, len(CONTENT) - 1),
        ("bytes=-9999", 0, len(CONTENT) - 1),
    ],
)
def test_satisfiable_single_range(header: str, start: int, end: int) -> None:
    client, streams = _client()
    response = client.get("/artifact", headers={"Range": header})
    assert response.status_code == 206
    assert response.content == CONTENT[start : end + 1]
    assert response.headers["content-length"] == str(end - start + 1)
    assert response.headers["content-range"] == f"bytes {start}-{end}/{len(CONTENT)}"
    assert streams[0].closed


@pytest.mark.parametrize(
    "header",
    ["bytes=-", "bytes=-0", "bytes=9999-", "bytes=9-1", "bytes=0-2,4-6", "items=0-1"],
)
def test_invalid_ranges_do_not_leak_open_streams(header: str) -> None:
    client, streams = _client()
    response = client.get("/artifact", headers={"Range": header})
    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{len(CONTENT)}"
    assert response.headers["cache-control"] == "no-store"
    assert streams[0].closed


def test_head_and_conditional_requests() -> None:
    client, streams = _client()
    response = client.head("/artifact", headers={"Range": "bytes=0-2"})
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-length"] == str(len(CONTENT))
    etag = response.headers["etag"]
    assert client.get("/artifact", headers={"If-None-Match": f"W/{etag}"}).status_code == 304
    assert (
        client.get("/artifact", headers={"Range": "bytes=0-2", "If-Range": etag}).status_code == 206
    )
    mismatch = client.get("/artifact", headers={"Range": "bytes=0-2", "If-Range": '"different"'})
    assert mismatch.status_code == 200
    assert mismatch.content == CONTENT
    assert (
        client.get("/artifact", headers={"Range": "bytes=0-2", "If-Range": f"W/{etag}"}).status_code
        == 200
    )
    assert all(stream.closed for stream in streams)


def test_expiry_precedes_range_head_and_not_modified() -> None:
    client, streams = _client(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    assert client.get("/artifact", headers={"If-None-Match": "*"}).status_code == 410
    assert client.get("/artifact", headers={"Range": "bytes=0-2"}).status_code == 410
    assert client.head("/artifact").status_code == 410
    assert all(stream.closed for stream in streams)


def test_active_content_is_always_an_attachment() -> None:
    client, _ = _client(media_type="text/html")
    response = client.get("/artifact")
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "sandbox; default-src 'none'"


def test_descriptor_length_mismatch_closes_stream() -> None:
    source = io.BytesIO(CONTENT)
    with pytest.raises(ArtifactIntegrityError):
        artifact_response(
            source,
            artifact_id=IDENTITY,
            size_bytes=len(CONTENT) + 1,
            media_type="text/plain",
        )
    assert source.closed


def test_verified_stream_context_is_released_once() -> None:
    closed: list[bool] = []
    app = FastAPI()

    @app.api_route("/managed", methods=["GET", "HEAD"])
    def managed(request: Request) -> Response:
        return artifact_response(
            io.BytesIO(CONTENT),
            artifact_id=IDENTITY,
            size_bytes=len(CONTENT),
            media_type="audio/flac",
            method=request.method,
            on_close=lambda: closed.append(True),
        )

    client = TestClient(app)
    assert client.get("/managed").content == CONTENT
    assert closed == [True]
    assert client.head("/managed").content == b""
    assert closed == [True, True]
