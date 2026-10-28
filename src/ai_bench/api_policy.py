"""Public-demo request boundaries, independent of benchmark-specific validation."""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import HTTPException
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_JSON_BYTES = 32 * 1024
MAX_FILE_BYTES = 25_000_000
_PART_PATH = re.compile(r"^/api/dataset-uploads/[a-zA-Z0-9_-]{1,96}/parts/[a-zA-Z0-9_-]{1,80}$")
_KEY = re.compile(r"^v1\.([0-9]{10,12})\.([a-f0-9-]{36})$")
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class ApiPolicyConfigurationError(ValueError):
    pass


def _boolean(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().casefold()
    if value not in {"true", "false"}:
        raise ApiPolicyConfigurationError(f"{name} must be true or false.")
    return value == "true"


def _origin(value: str) -> str:
    if any(ord(character) <= 32 or ord(character) == 127 for character in value):
        raise ValueError("An origin cannot contain whitespace or control characters.")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("A complete HTTP origin without a path or credentials is required.")
    host = parsed.hostname.casefold()
    if ":" in host:
        host = f"[{host}]"
    default_port = 443 if parsed.scheme == "https" else 80
    port = f":{parsed.port}" if parsed.port not in {None, default_port} else ""
    return f"{parsed.scheme}://{host}{port}"


@dataclass(frozen=True)
class ApiPolicy:
    public_demo: bool = False
    public_origin: str | None = None
    allow_networked: bool = False
    max_json_bytes: int = MAX_JSON_BYTES
    max_file_bytes: int = MAX_FILE_BYTES
    receive_idle_seconds: float = 30
    receive_total_seconds: float = 180

    @property
    def max_attempts(self) -> int:
        return 3 if self.public_demo else 20

    @classmethod
    def from_environment(cls) -> ApiPolicy:
        public_demo = _boolean("AI_BENCH_PUBLIC_DEMO_MODE")
        allow_networked = _boolean("AI_BENCH_ALLOW_NETWORKED_SOLUTIONS")
        origin = os.getenv("AI_BENCH_PUBLIC_ORIGIN") or None
        if origin:
            try:
                origin = _origin(origin)
            except ValueError as error:
                raise ApiPolicyConfigurationError("AI_BENCH_PUBLIC_ORIGIN is invalid.") from error
        if public_demo:
            if origin is None:
                raise ApiPolicyConfigurationError(
                    "Public demo mode requires an explicit AI_BENCH_PUBLIC_ORIGIN."
                )
            runtime = os.getenv("AI_BENCH_RUNTIME", "local").casefold()
            parsed = urlsplit(origin)
            if parsed.scheme != "https" and not (
                runtime == "local" and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
            ):
                raise ApiPolicyConfigurationError("Public hosted ingress must use HTTPS.")
            if runtime == "azure" and os.getenv("AI_BENCH_AUTH_MODE", "entra") != "entra":
                raise ApiPolicyConfigurationError("The public hosted runtime requires Entra.")
        return cls(
            public_demo=public_demo,
            public_origin=origin,
            allow_networked=allow_networked,
        )

    def validate_write(self, headers: Headers) -> None:
        if not self.public_demo:
            return
        if headers.get("x-ai-bench-intent") != "public-demo":
            raise HTTPException(
                status_code=403, detail="Explicit X-AI-Bench-Intent: public-demo is required."
            )
        if headers.get("sec-fetch-site") not in {None, "same-origin", "none"}:
            raise HTTPException(status_code=403, detail="Cross-origin writes are not allowed.")
        supplied = headers.get("origin")
        if supplied is not None:
            try:
                origin = _origin(supplied)
            except ValueError:
                raise HTTPException(
                    status_code=403, detail="Cross-origin writes are not allowed."
                ) from None
            if origin != self.public_origin:
                raise HTTPException(status_code=403, detail="Cross-origin writes are not allowed.")

    def validate_key(
        self,
        key: str | None,
        *,
        known: bool = False,
        now: datetime | None = None,
    ) -> None:
        if not self.public_demo:
            return
        match = _KEY.fullmatch(key or "")
        if match is None:
            raise HTTPException(
                status_code=422,
                detail="Use a timestamped v1.<server-seconds>.<UUID4> Idempotency-Key.",
            )
        try:
            nonce = UUID(match[2])
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid idempotency UUID.") from None
        if nonce.version != 4:
            raise HTTPException(status_code=422, detail="Idempotency keys require a UUID4.")
        age = (now or datetime.now(UTC)).timestamp() - int(match[1])
        if age < -300:
            raise HTTPException(status_code=422, detail="Idempotency key is ahead of server time.")
        if not known and age > 300:
            raise HTTPException(
                status_code=410,
                detail="Operation key expired; refresh before starting a new operation.",
            )


class RequestPolicyMiddleware:
    def __init__(self, app: ASGIApp, *, policy: ApiPolicy) -> None:
        self.app = app
        self.policy = policy

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        method = scope["method"]
        limit = (
            self.policy.max_file_bytes
            if method == "PUT" and _PART_PATH.fullmatch(scope["path"])
            else self.policy.max_json_bytes
        )
        try:
            if method in _WRITE_METHODS:
                self.policy.validate_write(headers)
            lengths = headers.getlist("content-length")
            if len(lengths) > 1 or (lengths and (not re.fullmatch(r"[0-9]{1,12}", lengths[0]))):
                raise HTTPException(status_code=400, detail="Invalid Content-Length.")
            if lengths and headers.get("transfer-encoding") is not None:
                raise HTTPException(status_code=400, detail="Conflicting request length headers.")
            if lengths and int(lengths[0]) > limit:
                raise HTTPException(status_code=413, detail="Request exceeds its byte limit.")
        except HTTPException as error:
            await JSONResponse(
                {"detail": error.detail},
                status_code=error.status_code,
                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
            )(scope, receive, send)
            return

        received = 0
        finished = False
        deadline = time.monotonic() + self.policy.receive_total_seconds

        async def bounded_receive() -> Message:
            nonlocal received, finished
            if finished or method not in _WRITE_METHODS:
                return await receive()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HTTPException(status_code=408, detail="Request body deadline exceeded.")
            try:
                message = await asyncio.wait_for(
                    receive(), timeout=min(remaining, self.policy.receive_idle_seconds)
                )
            except TimeoutError:
                raise HTTPException(status_code=408, detail="Request body timed out.") from None
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise HTTPException(status_code=413, detail="Request exceeds its byte limit.")
                finished = not message.get("more_body", False)
                if finished and lengths and received != int(lengths[0]):
                    raise HTTPException(
                        status_code=400, detail="Request length differs from Content-Length."
                    )
            return message

        async def safe_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() not in {b"cache-control", b"x-content-type-options"}
                ]
                message = {
                    **message,
                    "headers": [
                        *response_headers,
                        (b"cache-control", b"no-store"),
                        (b"x-content-type-options", b"nosniff"),
                    ],
                }
            await send(message)

        await self.app(scope, bounded_receive, safe_send)
