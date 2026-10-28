from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from ai_bench.backends import local_backend
from ai_bench.contracts import utc_now
from ai_bench.engine import BenchmarkEngine
from ai_bench.packs import get_pack
from ai_bench.receipts import (
    AttemptPersistencePendingError,
    ResponseReceiptRepository,
    UncertainDispatchError,
)
from ai_bench.repositories import LeaseLostError
from ai_bench.table_repository import AzureTableRepository


@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("failure_point", ["record_attempt_telemetry", "record_response_receipt"])
def test_response_receipt_resume_does_not_repeat_physical_call(
    tmp_path: Path,
    pack_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    durable: bool,
    failure_point: str,
) -> None:
    engine = BenchmarkEngine(
        tmp_path,
        pack_root,
        lease_seconds=1,
        backend=local_backend(tmp_path, durable_projections=durable),
    )
    pack = get_pack("synthetic_text_intent", pack_root)
    monkeypatch.setattr("ai_bench.engine.get_pack", lambda *args: pack)
    calls = [0]
    solution = next(item for item in pack.solutions if item.id == "keyword-standalone")
    execute = type(solution).execute

    def counted(self: Any, request: Any) -> Any:
        calls[0] += 1
        return execute(self, request)

    monkeypatch.setattr(type(solution), "execute", counted)
    manifest = engine.create_run("synthetic_text_intent", solution_ids=("keyword-standalone",))
    lease = engine.repository.claim_task("worker", lease_seconds=1)
    assert lease is not None
    record = getattr(engine.repository, failure_point)

    def unavailable(*args: Any) -> None:
        raise OSError("synthetic storage outage after checkpoint")

    monkeypatch.setattr(engine.repository, failure_point, unavailable)
    with pytest.raises(AttemptPersistencePendingError):
        engine._execute_lease(lease)
    assert calls[0] == 1
    assert isinstance(engine.repository, ResponseReceiptRepository)
    assert (
        engine.artifacts.get_receipt(
            lease.task.run_id,
            lease.task.id,
            lease.attempt_number,
            incarnation=lease.retention_incarnation,
        )
        is not None
    )
    future = utc_now() + timedelta(seconds=3)
    if isinstance(engine.repository, AzureTableRepository):
        engine.repository.clock = lambda: future
    else:
        monkeypatch.setattr("ai_bench.repositories.time.time", lambda: future.timestamp())
    monkeypatch.setattr(engine.repository, failure_point, record)
    assert engine.repository.recover_expired_leases() == (1, 0)
    resumed = engine.repository.claim_task("worker", lease_seconds=30, run_id=manifest.run_id)
    assert resumed is not None
    assert resumed.attempt_number == lease.attempt_number
    engine._execute_lease(resumed)
    assert calls[0] == 1


def test_sent_checkpoint_is_not_a_replay_permission(tmp_path: Path, pack_root: Path) -> None:
    engine = BenchmarkEngine(
        tmp_path, pack_root, backend=local_backend(tmp_path, durable_projections=True)
    )
    engine.create_run("synthetic_text_intent", solution_ids=("keyword-standalone",))
    lease = engine.repository.claim_task("worker")
    assert lease is not None
    assert isinstance(engine.repository, ResponseReceiptRepository)
    engine.repository.begin_dispatch(lease)
    with pytest.raises(UncertainDispatchError):
        engine.repository.begin_dispatch(lease)


@pytest.mark.parametrize("durable", [False, True])
def test_availability_recovery_preserves_indeterminate_usage_and_attempt_ceiling(
    tmp_path: Path, pack_root: Path, monkeypatch: pytest.MonkeyPatch, durable: bool
) -> None:
    engine = BenchmarkEngine(
        tmp_path,
        pack_root,
        backend=local_backend(tmp_path, durable_projections=durable),
    )
    manifest = engine.create_run(
        "synthetic_text_intent", solution_ids=("keyword-standalone",), retry_limit=2
    )
    now = [utc_now()]
    if isinstance(engine.repository, AzureTableRepository):
        engine.repository.clock = lambda: now[0]
    else:
        monkeypatch.setattr("ai_bench.repositories.time.time", lambda: now[0].timestamp())
    first = engine.repository.claim_task("original", lease_seconds=1, run_id=manifest.run_id)
    assert first is not None
    assert isinstance(engine.repository, ResponseReceiptRepository)
    engine.repository.begin_dispatch(first)
    now[0] += timedelta(seconds=32)
    assert engine.repository.acquire_supervisor("replacement")
    assert engine.repository.recover_expired_leases() == (1, 0)
    attempt = engine.repository.list_attempts(first.task.id)[0]
    assert attempt.error_type == "IndeterminateProviderOutcome"
    assert attempt.failure is not None and not attempt.failure.usage.complete
    assert attempt.failure.usage.dimensions == {}
    assert attempt.telemetry is not None and attempt.telemetry.cost.amount is None
    assert attempt.telemetry.latency_ms is None
    assert "overlap" in (attempt.failure.usage.uncertainty or "")
    with pytest.raises(LeaseLostError):
        engine.repository.begin_dispatch(first)
    now[0] += timedelta(seconds=2)
    second = None
    for _ in range(len(manifest.sample_ids)):
        candidate = engine.repository.claim_task(
            "replacement", lease_seconds=1, run_id=manifest.run_id
        )
        assert candidate is not None
        if candidate.task.id == first.task.id:
            second = candidate
            break
        engine.repository.fail_attempt(candidate, RuntimeError("synthetic unrelated task"))
    assert second is not None and second.attempt_number == 2
    engine.repository.begin_dispatch(second)
    now[0] += timedelta(seconds=2)
    recovered, poisoned = engine.repository.recover_expired_leases()
    assert (recovered, poisoned) == (0, 1)
    history = engine.repository.list_attempts(first.task.id)
    assert len(history) == 2
    assert all(item.error_type == "IndeterminateProviderOutcome" for item in history)
    task = engine.repository.get_task(first.task.id)
    assert task is not None and task.status.value == "poisoned"
