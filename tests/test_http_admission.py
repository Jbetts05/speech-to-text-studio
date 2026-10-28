from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from ai_bench.api import create_app
from ai_bench.api_admission import HttpAdmission
from ai_bench.backends import local_backend
from ai_bench.runtime_policy import PublicExecutionPolicy
from ai_bench.storage_state import SqliteStateStore


def test_shared_admission_caps_a_window_and_recovers_without_a_daily_cap(tmp_path: Path) -> None:
    state = SqliteStateStore(tmp_path / "state.sqlite3")
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    first = HttpAdmission(state, clock=lambda: now, per_minute=2)
    second = HttpAdmission(state, clock=lambda: now, per_minute=2)
    first.admit("run")
    second.admit("run")
    try:
        first.admit("run")
    except HTTPException as error:
        assert error.status_code == 429
        assert error.headers == {"Retry-After": "60"}
    else:
        raise AssertionError("a third admission bypassed the shared window")
    first.admit("upload")
    now += timedelta(minutes=1)
    second.admit("run")
    first.admit("run")
    assert len(state.page("http-admission:").rows) == 2


def test_concurrent_new_requests_do_not_oversubscribe(tmp_path: Path) -> None:
    state = SqliteStateStore(tmp_path / "state.sqlite3")
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    admission = HttpAdmission(state, clock=lambda: now, per_minute=3)

    def admit(_index: int) -> int:
        try:
            admission.admit("run")
        except HTTPException as error:
            return error.status_code
        return 200

    with ThreadPoolExecutor(max_workers=6) as pool:
        statuses = list(pool.map(admit, range(6)))
    assert statuses.count(200) == statuses.count(429) == 3


def test_public_capacity_is_core_enforced_and_replay_does_not_reserve_twice(
    tmp_path: Path,
    pack_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_BENCH_PUBLIC_DEMO_MODE", "true")
    monkeypatch.setenv("AI_BENCH_PUBLIC_ORIGIN", "https://demo.example.test")
    backend = local_backend(tmp_path / "state")
    backend.public_policy = PublicExecutionPolicy(max_active_runs=1, max_outstanding_tasks=6)
    client = TestClient(create_app(tmp_path / "state", pack_root, backend=backend))
    payload = {
        "benchmark_id": "synthetic_text_intent",
        "solution_ids": ["keyword-standalone"],
        "retry_limit": 3,
        "allow_networked": False,
    }
    key = f"v1.{int(datetime.now(UTC).timestamp())}.{uuid4()}"
    headers = {"X-AI-Bench-Intent": "public-demo", "Idempotency-Key": key}
    first = client.post("/api/runs", json=payload, headers=headers)
    assert first.status_code == 202, first.text
    replay = client.post("/api/runs", json=payload, headers=headers)
    assert replay.status_code == 202
    assert replay.json()["run_id"] == first.json()["run_id"]
    another = client.post(
        "/api/runs",
        json=payload,
        headers={
            **headers,
            "Idempotency-Key": f"v1.{int(datetime.now(UTC).timestamp())}.{uuid4()}",
        },
    )
    assert another.status_code == 429, another.text
    assert another.headers["retry-after"] == "5"
    assert len(client.get("/api/runs").json()) == 1
    assert client.get("/api/runtime").json()["max_attempts"] == 3
