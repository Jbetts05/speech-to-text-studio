from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_bench.api import create_app
from ai_bench.backends import local_backend
from ai_bench.engine import BenchmarkEngine
from ai_bench.hashing import sha256_digest
from ai_bench.lifecycle import ExpiredRootError


@pytest.mark.parametrize("durable", [False, True])
def test_unreaped_accepted_key_replay_returns_410(
    tmp_path: Path, pack_root: Path, durable: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = local_backend(tmp_path, durable_projections=durable)
    engine = BenchmarkEngine(tmp_path, pack_root, backend=backend)
    manifest = engine.create_run(
        "synthetic_text_intent",
        solution_ids=("keyword-standalone",),
        idempotency_key="accepted-key",
    )
    root = engine.lifecycle.require_live(manifest.run_id)
    expires_at = root.expires_at
    assert expires_at is not None
    engine.lifecycle.clock = lambda: expires_at
    key_hash = sha256_digest({"idempotency_key": "accepted-key"})
    assert engine.repository.get_launch_reservation(key_hash) is not None

    def no_historical_read(key: str) -> None:
        raise AssertionError("expiry must precede historical manifest replay")

    monkeypatch.setattr(engine.repository, "launch_for_key", no_historical_read)
    with pytest.raises(ExpiredRootError):
        engine.create_run(
            "synthetic_text_intent",
            solution_ids=("keyword-standalone",),
            idempotency_key="accepted-key",
        )
    client = TestClient(create_app(tmp_path, pack_root, backend=backend))
    response = client.post(
        "/api/runs",
        json={"benchmark_id": "synthetic_text_intent", "solution_ids": ["keyword-standalone"]},
        headers={"Idempotency-Key": "accepted-key"},
    )
    assert response.status_code == 410
    assert engine.repository.get_launch_reservation(key_hash) is not None


@pytest.mark.parametrize("durable", [False, True])
def test_expired_reservation_without_run_row_cannot_restart_initialization(
    tmp_path: Path, pack_root: Path, durable: bool
) -> None:
    engine = BenchmarkEngine(
        tmp_path, pack_root, backend=local_backend(tmp_path, durable_projections=durable)
    )
    run_id = sha256_digest("interrupted initialization")
    root = engine.lifecycle.create(run_id, "run")
    key_hash = sha256_digest({"idempotency_key": "interrupted-key"})
    request_hash = sha256_digest(
        {
            "benchmark_id": "synthetic_text_intent",
            "solution_ids": ("keyword-standalone",),
            "retry_limit": 3,
            "allow_networked": False,
        }
    )
    engine.repository.reserve_launch(key_hash, request_hash, run_id)
    expires_at = root.expires_at
    assert expires_at is not None
    engine.lifecycle.clock = lambda: expires_at
    with pytest.raises(ExpiredRootError):
        engine.create_run(
            "synthetic_text_intent",
            solution_ids=("keyword-standalone",),
            idempotency_key="interrupted-key",
        )
    assert engine.repository.get_launch_reservation(key_hash) is not None
    assert not engine.repository.list_runs()
