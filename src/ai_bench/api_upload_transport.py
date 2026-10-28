"""Bounded raw-file reception for a previously reserved durable upload part."""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, Request
from starlette.requests import ClientDisconnect

from ai_bench.api_policy import MAX_FILE_BYTES


@dataclass(frozen=True)
class ReceivedFile:
    path: Path
    size_bytes: int
    sha256: str


@asynccontextmanager
async def receive_reserved_file(
    request: Request,
    *,
    size_bytes: int,
    sha256: str,
    directory: Path | None = None,
) -> AsyncIterator[ReceivedFile]:
    if not 0 < size_bytes <= MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="Reserved file exceeds the upload limit.")
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length.") from None
        if declared != size_bytes:
            raise HTTPException(
                status_code=413 if declared > size_bytes else 400,
                detail="File length must match its immutable upload reservation.",
            )
    if request.headers.get("content-encoding", "identity").casefold() != "identity":
        raise HTTPException(
            status_code=415, detail="Compressed HTTP request bodies are not allowed."
        )
    if request.headers.get("content-type", "").split(";", 1)[0].casefold() != (
        "application/octet-stream"
    ):
        raise HTTPException(
            status_code=415, detail="Send one raw file with Content-Type: application/octet-stream."
        )

    with tempfile.TemporaryDirectory(prefix="part-", dir=directory) as temporary:
        path = Path(temporary) / "body"
        digest = hashlib.sha256()
        received = 0
        try:
            with path.open("xb") as output:
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > size_bytes:
                        raise HTTPException(
                            status_code=413, detail="File exceeds its immutable size reservation."
                        )
                    digest.update(chunk)
                    await asyncio.to_thread(output.write, chunk)
        except ClientDisconnect:
            raise HTTPException(
                status_code=400, detail="File upload was interrupted; retry this reserved part."
            ) from None
        if received != size_bytes:
            raise HTTPException(
                status_code=400, detail="File is incomplete; retry this reserved part."
            )
        actual_digest = digest.hexdigest()
        if actual_digest != sha256:
            raise HTTPException(
                status_code=422, detail="File SHA-256 does not match its immutable reservation."
            )
        yield ReceivedFile(path=path, size_bytes=received, sha256=actual_digest)
