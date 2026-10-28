"""Bounded HTTP delivery of already verified immutable artifact streams."""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import BinaryIO

from fastapi import HTTPException
from starlette.background import BackgroundTask
from starlette.responses import Response, StreamingResponse

from ai_bench.artifacts import ArtifactIntegrityError

INLINE_MEDIA_TYPES = frozenset(
    {
        "image/avif",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
        "audio/flac",
        "audio/wav",
        "audio/mpeg",
    }
)
_EXTENSIONS = {
    "audio/flac": ".flac",
    "audio/wav": ".wav",
    "audio/mpeg": ".mp3",
    "application/json": ".json",
    "text/plain": ".txt",
    "text/csv": ".csv",
}
_RANGE = re.compile(r"^bytes=(\d{0,20})-(\d{0,20})$")
_CHUNK_BYTES = 64 * 1024


def _byte_range(header: str, size: int) -> tuple[int, int]:
    match = _RANGE.fullmatch(header.strip())
    if match is None or not any(match.groups()) or size == 0:
        raise ValueError("A single satisfiable byte range is required.")
    first, last = match.groups()
    if first:
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
        if start >= size or end < start:
            raise ValueError("The byte range is outside this artifact.")
        return start, end
    suffix = int(last)
    if suffix == 0:
        raise ValueError("A suffix range must contain at least one byte.")
    return max(0, size - suffix), size - 1


def _matches_etag(header: str | None, etag: str) -> bool:
    if header is None:
        return False
    return any(token.strip().removeprefix("W/") in {"*", etag} for token in header.split(","))


def _stream(
    source: BinaryIO,
    count: int,
    expires_at: datetime | None,
    close: Callable[[], None],
) -> Iterator[bytes]:
    try:
        remaining = count
        while remaining:
            if expires_at is not None and datetime.now(UTC) >= expires_at:
                return
            chunk = source.read(min(remaining, _CHUNK_BYTES))
            if not chunk:
                raise ArtifactIntegrityError("Verified artifact stream ended unexpectedly.")
            remaining -= len(chunk)
            yield chunk
    finally:
        close()


def artifact_response(
    source: BinaryIO,
    *,
    artifact_id: str,
    size_bytes: int,
    media_type: str,
    method: str = "GET",
    range_header: str | None = None,
    if_range: str | None = None,
    if_none_match: str | None = None,
    download: bool = False,
    expires_at: datetime | None = None,
    on_close: Callable[[], None] | None = None,
) -> Response:
    """Take ownership of a seekable, hash-verified stream, including on failure."""
    closed = False
    close_lock = threading.Lock()

    def close() -> None:
        nonlocal closed
        with close_lock:
            if closed:
                return
            closed = True
        try:
            source.close()
        finally:
            if on_close is not None:
                on_close()

    try:
        actual_size = source.seek(0, os.SEEK_END)
        source.seek(0)
    except (OSError, ValueError):
        close()
        raise
    if size_bytes < 0 or actual_size != size_bytes:
        close()
        raise ArtifactIntegrityError("Artifact byte length differs from its registered descriptor.")
    if expires_at is not None and datetime.now(UTC) >= expires_at:
        close()
        raise HTTPException(status_code=410, detail="artifact expired")

    etag = f'"sha256:{artifact_id}"'
    base_type = media_type.split(";", 1)[0].strip().casefold()
    disposition = "inline" if not download and base_type in INLINE_MEDIA_TYPES else "attachment"
    headers = {
        "ETag": etag,
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox; default-src 'none'",
        "Content-Disposition": (
            f'{disposition}; filename="{artifact_id}{_EXTENSIONS.get(base_type, "")}"'
        ),
    }
    if _matches_etag(if_none_match, etag):
        close()
        return Response(status_code=304, headers=headers)
    if method == "HEAD":
        close()
        return Response(
            content=b"",
            media_type=media_type,
            headers={**headers, "Content-Length": str(size_bytes)},
        )

    start, end = 0, size_bytes - 1
    status = 200
    if range_header is not None and (if_range is None or if_range == etag):
        try:
            start, end = _byte_range(range_header, size_bytes)
        except ValueError as error:
            close()
            raise HTTPException(
                status_code=416,
                detail=str(error),
                headers={**headers, "Content-Range": f"bytes */{size_bytes}"},
            ) from error
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size_bytes}"
    try:
        source.seek(start)
    except (OSError, ValueError):
        close()
        raise
    count = end - start + 1
    headers["Content-Length"] = str(count)
    return StreamingResponse(
        _stream(source, count, expires_at, close),
        status_code=status,
        media_type=media_type,
        headers=headers,
        background=BackgroundTask(close),
    )
