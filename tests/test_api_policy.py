from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from starlette.datastructures import Headers
from starlette.types import Message, Scope

from ai_bench.api_policy import ApiPolicy, ApiPolicyConfigurationError, RequestPolicyMiddleware


def test_public_policy_requires_explicit_origin_and_entra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_BENCH_PUBLIC_DEMO_MODE", "true")
    monkeypatch.setenv("AI_BENCH_RUNTIME", "azure")
    monkeypatch.delenv("AI_BENCH_PUBLIC_ORIGIN", raising=False)
    with pytest.raises(ApiPolicyConfigurationError, match="explicit"):
        ApiPolicy.from_environment()
    monkeypatch.setenv("AI_BENCH_PUBLIC_ORIGIN", "http://public.invalid")
    with pytest.raises(ApiPolicyConfigurationError, match="HTTPS"):
        ApiPolicy.from_environment()
    monkeypatch.setenv("AI_BENCH_PUBLIC_ORIGIN", "https://public.invalid/path")
    with pytest.raises(ApiPolicyConfigurationError, match="invalid"):
        ApiPolicy.from_environment()
    monkeypatch.setenv("AI_BENCH_PUBLIC_ORIGIN", "https://public.invalid/")
    monkeypatch.setenv("AI_BENCH_AUTH_MODE", "key")
    with pytest.raises(ApiPolicyConfigurationError, match="Entra"):
        ApiPolicy.from_environment()
    monkeypatch.setenv("AI_BENCH_AUTH_MODE", "entra")
    policy = ApiPolicy.from_environment()
    assert policy.public_origin == "https://public.invalid"
    assert policy.max_attempts == 3


def test_public_write_intent_is_not_an_authentication_gate() -> None:
    policy = ApiPolicy(public_demo=True, public_origin="https://public.invalid")
    policy.validate_write(Headers({"X-AI-Bench-Intent": "public-demo"}))
    policy.validate_write(
        Headers(
            {
                "X-AI-Bench-Intent": "public-demo",
                "Origin": "https://public.invalid",
                "Sec-Fetch-Site": "same-origin",
            }
        )
    )
    for headers in (
        {},
        {"X-AI-Bench-Intent": "public-demo", "Origin": "https://different.invalid"},
        {"X-AI-Bench-Intent": "public-demo", "Origin": "null"},
        {"X-AI-Bench-Intent": "public-demo", "Sec-Fetch-Site": "cross-site"},
        {"X-AI-Bench-Intent": "public-demo", "Sec-Fetch-Site": "same-site"},
    ):
        with pytest.raises(HTTPException) as caught:
            policy.validate_write(Headers(headers))
        assert caught.value.status_code == 403
    assert ApiPolicy().max_attempts == 20
    ApiPolicy().validate_write(Headers())


def test_timestamped_keys_allow_known_replay_but_not_expired_resurrection() -> None:
    policy = ApiPolicy(public_demo=True, public_origin="https://public.invalid")
    now = datetime.now(UTC)
    key = f"v1.{int(now.timestamp())}.{uuid4()}"
    policy.validate_key(key, now=now)
    policy.validate_key(key, known=True, now=now + timedelta(days=9))
    with pytest.raises(HTTPException) as caught:
        policy.validate_key(key, now=now + timedelta(days=10))
    assert caught.value.status_code == 410
    with pytest.raises(HTTPException) as future:
        policy.validate_key(key, now=now - timedelta(minutes=6))
    assert future.value.status_code == 422
    with pytest.raises(HTTPException):
        policy.validate_key(f"v1.{int(now.timestamp())}.00000000-0000-0000-0000-000000000000")
    ApiPolicy().validate_key("legacy-key")


def _client(policy: ApiPolicy) -> TestClient:
    app = FastAPI()
    app.add_middleware(RequestPolicyMiddleware, policy=policy)

    @app.post("/api/admission")
    async def admission(request: Request) -> dict[str, int]:
        return {"bytes": len(await request.body())}

    @app.put("/api/dataset-uploads/session/parts/part")
    async def part(request: Request) -> dict[str, int]:
        count = 0
        async for chunk in request.stream():
            count += len(chunk)
        return {"bytes": count}

    return TestClient(app)


def test_small_admission_and_bounded_file_requests_have_distinct_limits() -> None:
    client = _client(ApiPolicy(max_json_bytes=8, max_file_bytes=16))
    assert client.post("/api/admission", content=b"x" * 8).status_code == 200
    assert client.post("/api/admission", content=b"x" * 9).status_code == 413
    path = "/api/dataset-uploads/session/parts/part"
    assert client.put(path, content=b"x" * 16).status_code == 200
    assert client.put(path, content=b"x" * 17).status_code == 413
    assert client.put(path, content=iter((b"x" * 8, b"x" * 9))).status_code == 413
    assert client.put(path, content=iter((b"x" * 8, b"x" * 8))).status_code == 200
    assert client.post("/api/admission", content=b"ok").headers["cache-control"] == "no-store"


def test_length_headers_are_checked_before_parsing() -> None:
    client = _client(ApiPolicy(max_json_bytes=8, max_file_bytes=16))
    assert (
        client.post("/api/admission", content=b"short", headers={"Content-Length": "6"}).status_code
        == 400
    )
    assert (
        client.post(
            "/api/admission", content=b"short", headers={"Content-Length": "-1"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/admission",
            content=b"short",
            headers={"Content-Length": "5", "Transfer-Encoding": "chunked"},
        ).status_code
        == 400
    )


def test_stalled_body_is_bounded_without_waiting_for_large_timeout() -> None:
    captured: list[Message] = []

    async def run() -> None:
        app = FastAPI()
        app.add_middleware(
            RequestPolicyMiddleware,
            policy=ApiPolicy(receive_idle_seconds=0.01, receive_total_seconds=0.02),
        )

        @app.post("/api/admission")
        async def admission(request: Request) -> dict[str, int]:
            return {"bytes": len(await request.body())}

        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/admission",
            "raw_path": b"/api/admission",
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("testserver", 80),
        }

        async def receive() -> Message:
            await asyncio.sleep(1)
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: Message) -> None:
            captured.append(message)

        await app(scope, receive, send)

    asyncio.run(run())
    assert next(item["status"] for item in captured if item["type"] == "http.response.start") == 408
