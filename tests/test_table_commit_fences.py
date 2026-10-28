from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from ai_bench.backends import local_backend
from ai_bench.contracts import TaskAttempt, TelemetryEvidence, Usage, utc_now
from ai_bench.engine import BenchmarkEngine
from ai_bench.lifecycle import ExpiredRootError
from ai_bench.repositories import LeaseLostError
from ai_bench.storage_state import StateChange, StateRow
from ai_bench.table_repository import AzureTableRepository


@pytest.mark.parametrize("operation", ["evidence", "completion"])
@pytest.mark.parametrize("interference", ["renewal", "retry", "takeover", "expiry", "pointer"])
def test_commit_refreshes_fences_after_immutable_io(
    tmp_path: Path,
    pack_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    interference: str,
) -> None:
    engine = BenchmarkEngine(
        tmp_path, pack_root, backend=local_backend(tmp_path, durable_projections=True)
    )
    manifest = engine.create_run(
        "synthetic_text_intent", solution_ids=("keyword-standalone",), retry_limit=1
    )
    repo = engine.repository
    assert isinstance(repo, AzureTableRepository)
    lease = repo.claim_task("owner")
    assert lease is not None
    immutable, read_attempt, transact = repo._immutable, repo._attempt, repo.state.transact
    blob_reads = writes = commit_attempts = 0
    attempted_key = f"attempt:{lease.task.id}:{lease.attempt_number:010d}"

    def read(row: StateRow) -> TaskAttempt:
        nonlocal blob_reads
        blob_reads += 1
        return read_attempt(row)

    def store(value: Any, run_id: str | None = None) -> str:
        nonlocal writes
        identifier = immutable(value, run_id)
        if not isinstance(value, TaskAttempt):
            return identifier
        writes += 1
        if writes != 1:
            return identifier
        if interference in {"renewal", "retry"}:
            repo.renew_lease(lease)
        elif interference == "takeover":
            future = utc_now() + timedelta(seconds=31)
            repo.clock = lambda: future
            assert repo.acquire_supervisor("replacement")
        elif interference == "expiry":
            engine.lifecycle.clock = lambda: utc_now() + timedelta(days=11)
        else:
            row = repo._required(attempted_key)
            replacement = read_attempt(row).model_copy(update={"queue_wait_ms": Decimal(9)})
            updated = immutable(replacement, lease.task.run_id)
            transact((StateChange(row.key, {**row.values, "artifact_id": updated}, row.etag),))
        return identifier

    def commit(changes: Sequence[StateChange]) -> None:
        nonlocal commit_attempts
        if any(change.key == attempted_key for change in changes):
            commit_attempts += 1
            if interference == "retry" and commit_attempts <= 3:
                repo.renew_lease(lease)
        transact(changes)

    monkeypatch.setattr(repo, "_immutable", store)
    monkeypatch.setattr(repo, "_attempt", read)
    monkeypatch.setattr(repo.state, "transact", commit)
    usage = Usage(dimensions={"calls": Decimal(1)})
    telemetry = TelemetryEvidence(
        started_at=utc_now(),
        finished_at=utc_now(),
        latency_ms=Decimal(1),
        usage=usage,
        cost=manifest.pricing_snapshots[lease.task.solution_id].estimate(usage),
    )

    def perform() -> None:
        if operation == "evidence":
            repo.record_attempt_telemetry(lease, telemetry)
        else:
            assert repo.fail_attempt(lease, RuntimeError("synthetic terminal failure"))

    if interference in {"takeover", "expiry"}:
        with pytest.raises((LeaseLostError, ExpiredRootError)):
            perform()
        assert commit_attempts == 0
        engine.lifecycle.clock = utc_now
        stored = read_attempt(repo._required(attempted_key))
        assert stored.status.value == "running" and stored.telemetry is None
    else:
        perform()
        assert blob_reads == (2 if interference == "pointer" else 1)
        assert writes == (2 if interference == "pointer" else 1)
        assert commit_attempts == (4 if interference == "retry" else 1)
        stored = read_attempt(repo._required(attempted_key))
        if interference == "pointer":
            assert stored.queue_wait_ms == 9
        if operation == "evidence":
            assert stored.telemetry == telemetry
        else:
            events = repo.list_events(manifest.run_id)
            assert sum(event.event_type == "task.poisoned" for event in events) == 1
            assert [event.sequence for event in events] == sorted({e.sequence for e in events})
