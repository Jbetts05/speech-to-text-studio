from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Sequence
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ai_bench.backends import local_backend
from ai_bench.contracts import (
    BenchmarkSample,
    DatasetAdapter,
    DatasetRevision,
    ExecutionOutput,
    ExecutionRequest,
    ModalityArtifact,
    ParsedOutput,
    SolutionAdapter,
    TaskStatus,
    Usage,
    utc_now,
)
from ai_bench.engine import BenchmarkEngine
from ai_bench.hashing import sha256_digest
from ai_bench.packs import BenchmarkPack, get_pack
from ai_bench.receipts import ResponseReceiptRepository, UncertainDispatchError
from ai_bench.repositories import Lease, LeaseLostError, SqliteRepository
from ai_bench.runtime_policy import PublicExecutionPolicy
from ai_bench.storage_state import StateChange, StateConflictError
from ai_bench.table_repository import AzureTableRepository


class OneSampleDataset:
    def __init__(self, delegate: DatasetAdapter) -> None:
        self.delegate = delegate
        self.id = "synthetic-dispatch-data"
        self._samples = (delegate.samples()[0],)
        self.revision: DatasetRevision = delegate.revision.model_copy(
            update={"content_hash": sha256_digest(self._samples)}
        )

    def samples(self) -> Sequence[BenchmarkSample]:
        return self._samples

    def read_artifact(self, artifact: ModalityArtifact) -> bytes:
        return self.delegate.read_artifact(artifact)


class SimulatedRemoteSolution:
    def __init__(self, delegate: SolutionAdapter, send: Callable[[ExecutionRequest], None]) -> None:
        self.delegate = delegate
        self.send = send
        self.id = delegate.id
        self.version = "synthetic-dispatch-v1"
        self.networked = True
        self.pipeline = delegate.pipeline
        self.compatibility = delegate.compatibility
        self.pricing = delegate.pricing

    def execute(self, request: ExecutionRequest) -> ExecutionOutput:
        assert request.sample.reference is None
        assert request.sample.metadata == {}
        self.send(request)
        return self.delegate.execute(request).model_copy(
            update={
                "usage": Usage(dimensions={"calls": Decimal(1)}),
                "provider_request_id": f"synthetic-request-{request.attempt_number}",
                "provider_latency_ms": Decimal(1),
            }
        )

    def parse(self, output: ExecutionOutput) -> ParsedOutput:
        return self.delegate.parse(output)


class SimulatedPack:
    def __init__(self, original: BenchmarkPack, solution: SolutionAdapter) -> None:
        self.definition = original.definition
        self.dataset = OneSampleDataset(original.dataset)
        self.solutions = (solution,)
        self.evaluator = original.evaluator
        self.viewer = original.viewer


def engines(
    tmp_path: Path,
    pack_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    durable: bool,
    send: Callable[[ExecutionRequest], None],
) -> tuple[BenchmarkEngine, BenchmarkEngine, str]:
    original = get_pack("synthetic_text_intent", pack_root)
    delegate = next(
        solution for solution in original.solutions if solution.id == "keyword-standalone"
    )
    simulated = SimulatedRemoteSolution(delegate, send)
    pack = SimulatedPack(original, simulated)
    monkeypatch.setattr("ai_bench.engine.get_pack", lambda *_: pack)
    monkeypatch.setenv("AI_BENCH_ALLOW_NETWORKED_SOLUTIONS", "true")
    policy = PublicExecutionPolicy()
    first = BenchmarkEngine(
        tmp_path,
        pack_root,
        lease_seconds=300,
        backend=local_backend(tmp_path, durable_projections=durable, public_policy=policy),
    )
    second = BenchmarkEngine(
        tmp_path,
        pack_root,
        lease_seconds=300,
        backend=local_backend(tmp_path, durable_projections=durable, public_policy=policy),
    )
    manifest = first.create_run(
        original.definition.id,
        solution_ids=(simulated.id,),
        allow_networked=True,
        retry_limit=3,
        idempotency_key="synthetic-dispatch",
    )
    return first, second, manifest.run_id


@pytest.mark.parametrize("durable", [False, True])
def test_pause_before_send_allows_bounded_external_overlap_but_fences_old_results(
    tmp_path: Path, pack_root: Path, monkeypatch: pytest.MonkeyPatch, durable: bool
) -> None:
    old_paused = threading.Event()
    allow_old_send = threading.Event()
    old_sent = threading.Event()
    new_in_flight = threading.Event()
    finish_new = threading.Event()
    lock = threading.Lock()
    calls: list[int] = []
    active = maximum_active = 0

    def send(request: ExecutionRequest) -> None:
        nonlocal active, maximum_active
        if request.attempt_number == 1:
            old_paused.set()
            assert allow_old_send.wait(120)
        with lock:
            calls.append(request.attempt_number)
            active += 1
            maximum_active = max(maximum_active, active)
        try:
            if request.attempt_number == 1:
                old_sent.set()
            else:
                new_in_flight.set()
                assert finish_new.wait(120)
        finally:
            with lock:
                active -= 1

    first, second, run_id = engines(tmp_path, pack_root, monkeypatch, durable, send)
    now = [utc_now()]
    if durable:
        assert isinstance(first.repository, AzureTableRepository)
        assert isinstance(second.repository, AzureTableRepository)
        first.repository.clock = lambda: now[0]
        second.repository.clock = lambda: now[0]
    else:
        monkeypatch.setattr("ai_bench.repositories.time.time", lambda: now[0].timestamp())
    assert first.repository.acquire_supervisor("original", lease_seconds=600)
    old = first.repository.claim_task("original", lease_seconds=300, run_id=run_id)
    assert old is not None
    errors: dict[str, Exception] = {}

    def execute(engine: BenchmarkEngine, lease: Lease, name: str) -> None:
        try:
            engine._execute_lease(lease)
        except Exception as error:
            errors[name] = error

    old_thread = threading.Thread(target=execute, args=(first, old, "old"))
    new_thread: threading.Thread | None = None
    old_thread.start()
    try:
        assert old_paused.wait(120)
        assert calls == []
        started = [
            event
            for event in first.repository.list_events(run_id)
            if event.event_type == "attempt.request_started"
        ]
        assert len(started) == 1
        assert started[0].payload["attempt"] == 1
        assert started[0].payload["lease_token"] == old.token
        assert started[0].payload["submission"] == "unconfirmed"
        assert started[0].payload["supervisor_generation"] == old.supervisor_generation
        assert isinstance(first.repository, ResponseReceiptRepository)
        with pytest.raises(UncertainDispatchError):
            first.repository.begin_dispatch(old)
        now[0] += timedelta(seconds=601)
        assert second.repository.acquire_supervisor("replacement", lease_seconds=600)
        assert second.repository.recover_expired_leases() == (1, 0)
        now[0] += timedelta(seconds=2)
        current = second.repository.claim_task("replacement", lease_seconds=300, run_id=run_id)
        assert current is not None and current.attempt_number == 2
        assert current.supervisor_generation > old.supervisor_generation
        new_thread = threading.Thread(target=execute, args=(second, current, "new"))
        new_thread.start()
        assert new_in_flight.wait(120)
        allow_old_send.set()
        assert old_sent.wait(120)
        assert maximum_active == 2
    finally:
        allow_old_send.set()
        finish_new.set()
        old_thread.join(timeout=120)
        if new_thread is not None:
            new_thread.join(timeout=120)
    assert not old_thread.is_alive()
    assert new_thread is not None and not new_thread.is_alive()
    assert sorted(calls) == [1, 2]
    assert isinstance(errors.get("old"), LeaseLostError)
    assert "new" not in errors
    task = second.repository.get_task(old.task.id)
    assert task is not None and task.status is TaskStatus.SUCCEEDED and task.result is not None
    assert task.attempts == 2
    assert task.result.telemetry is not None
    assert task.result.telemetry.provider_request_id == "synthetic-request-2"
    history = second.repository.list_attempts(task.id)
    assert history[0].error_type == "IndeterminateProviderOutcome"
    assert history[0].telemetry is not None and not history[0].telemetry.usage.complete
    assert history[0].telemetry.cost.amount is None
    with pytest.raises(LeaseLostError):
        first.repository.complete_attempt(old, task.result.model_copy(update={"attempts": 1}))
    events = second.repository.list_events(run_id)
    assert sum(event.event_type == "task.succeeded" for event in events) == 1
    started = [event for event in events if event.event_type == "attempt.request_started"]
    assert [event.payload["attempt"] for event in started] == [1, 2]
    assert [event.payload["lease_token"] for event in started] == [old.token, current.token]
    assert [event.payload["supervisor_generation"] for event in started] == [
        old.supervisor_generation,
        current.supervisor_generation,
    ]
    assert all(event.payload["submission"] == "unconfirmed" for event in started)
    assert [event.sequence for event in events] == sorted({event.sequence for event in events})


@pytest.mark.parametrize("durable", [False, True])
def test_lost_responses_keep_unknown_usage_and_never_exceed_three_attempts(
    tmp_path: Path, pack_root: Path, monkeypatch: pytest.MonkeyPatch, durable: bool
) -> None:
    calls: list[int] = []

    def lost_response(request: ExecutionRequest) -> None:
        calls.append(request.attempt_number)
        raise OSError("synthetic remote work may have completed; response was lost")

    first, second, run_id = engines(tmp_path, pack_root, monkeypatch, durable, lost_response)
    now = [utc_now()]
    if durable:
        assert isinstance(first.repository, AzureTableRepository)
        assert isinstance(second.repository, AzureTableRepository)
        first.repository.clock = lambda: now[0]
        second.repository.clock = lambda: now[0]
    else:
        monkeypatch.setattr("ai_bench.repositories.time.time", lambda: now[0].timestamp())
    task_id = ""
    for number in range(1, 4):
        owner = f"generation-{number}"
        assert second.repository.acquire_supervisor(owner, lease_seconds=600)
        lease = second.repository.claim_task(owner, lease_seconds=300, run_id=run_id)
        assert lease is not None and lease.attempt_number == number
        task_id = lease.task.id
        with pytest.raises(UncertainDispatchError):
            second._execute_lease(lease)
        now[0] += timedelta(seconds=601)
        assert first.repository.acquire_supervisor(f"recovery-{number}", lease_seconds=600)
        assert first.repository.recover_expired_leases() == ((1, 0) if number < 3 else (0, 1))
        assert first.repository.claim_task(f"recovery-{number}", run_id=run_id) is None
        recovered = first.repository.list_attempts(task_id)[-1]
        assert recovered.failure is not None and recovered.failure.retry_delay is not None
        assert recovered.failure.retry_delay.scheduled_seconds == Decimal(2 ** (number - 1))
        first.repository.release_supervisor(f"recovery-{number}")
        now[0] += timedelta(seconds=5)
    assert calls == [1, 2, 3]
    assert second.repository.claim_task("no-fourth-call", run_id=run_id) is None
    task = second.repository.get_task(task_id)
    assert task is not None and task.status is TaskStatus.POISONED and task.attempts == 3
    history = second.repository.list_attempts(task_id)
    assert len(history) == 3
    started = [
        event
        for event in second.repository.list_events(run_id)
        if event.event_type == "attempt.request_started"
    ]
    assert [event.payload["attempt"] for event in started] == [1, 2, 3]
    for attempt in history:
        assert attempt.error_type == "IndeterminateProviderOutcome"
        assert attempt.telemetry is not None
        assert not attempt.telemetry.usage.complete
        assert attempt.telemetry.usage.dimensions == {}
        assert attempt.telemetry.cost.amount is None
        assert attempt.telemetry.latency_ms is None


@pytest.mark.parametrize("durable", [False, True])
def test_request_start_and_event_are_atomic_and_not_repeated(
    tmp_path: Path, pack_root: Path, monkeypatch: pytest.MonkeyPatch, durable: bool
) -> None:
    engine, _, run_id = engines(tmp_path, pack_root, monkeypatch, durable, lambda _: None)
    repository = engine.repository
    assert isinstance(repository, ResponseReceiptRepository)
    lease = repository.claim_task("sender", run_id=run_id)
    assert lease is not None
    before = repository.list_events(run_id)
    with monkeypatch.context() as failure:
        if isinstance(repository, SqliteRepository):
            append_event = repository._append_event_tx

            def failed_event(
                connection: sqlite3.Connection,
                identifier: str,
                event_type: str,
                payload: dict[str, object],
            ) -> int:
                append_event(connection, identifier, event_type, payload)
                raise StateConflictError("synthetic failure after event insertion")

            failure.setattr(repository, "_append_event_tx", failed_event)
        else:
            transact = engine.backend.state.transact

            def event_conflict(changes: Sequence[StateChange]) -> None:
                assert any(change.key.startswith(f"event:{run_id}:") for change in changes)
                transact(
                    tuple(
                        StateChange(change.key, change.values, "synthetic-stale-event-etag")
                        if change.key.startswith(f"event:{run_id}:")
                        else change
                        for change in changes
                    )
                )

            failure.setattr(engine.backend.state, "transact", event_conflict)
        with pytest.raises(StateConflictError):
            repository.begin_dispatch(lease)
    assert repository.list_events(run_id) == before
    repository.begin_dispatch(lease)
    recorded = repository.list_events(run_id)
    assert len(recorded) == len(before) + 1
    assert recorded[-1].event_type == "attempt.request_started"
    with pytest.raises(UncertainDispatchError):
        repository.begin_dispatch(lease)
    assert repository.list_events(run_id) == recorded
