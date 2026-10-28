from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_bench.backends import local_backend
from ai_bench.contracts import (
    ArtifactViewer,
    BenchmarkDefinition,
    BenchmarkSample,
    DatasetAdapter,
    Evaluator,
    ExecutionOutput,
    ExecutionRequest,
    ModalityArtifact,
    ParsedOutput,
    ProviderFailureEvidence,
    RetryDelay,
    RunManifest,
    SchedulingPolicy,
    SolutionAdapter,
    TaskAttempt,
    TaskResult,
    TaskStatus,
    Usage,
    utc_now,
)
from ai_bench.engine import BenchmarkEngine
from ai_bench.failures import ProviderFailure
from ai_bench.hashing import canonical_json, sha256_digest
from ai_bench.packs import BenchmarkPack, get_pack, validate_pack
from ai_bench.repositories import EligibleTasks, IdempotencyConflictError, Lease, SqliteRepository
from ai_bench.storage_state import StateChange, StateConflictError
from ai_bench.table_repository import AzureTableRepository
from ai_bench.worker import RunSupervisor


class ProbeDataset:
    def __init__(self, delegate: DatasetAdapter, count: int = 2) -> None:
        self.delegate = delegate
        self.id = delegate.id
        self._samples = tuple(
            delegate.samples()[0].model_copy(update={"id": f"sample-{index:03d}"})
            for index in range(count)
        )
        self.revision = delegate.revision.model_copy(
            update={"content_hash": sha256_digest(self._samples)}
        )

    def samples(self) -> Sequence[BenchmarkSample]:
        return self._samples

    def read_artifact(self, artifact: ModalityArtifact) -> bytes:
        return self.delegate.read_artifact(artifact)


class ProbeSolution:
    def __init__(
        self,
        delegate: SolutionAdapter,
        index: int,
        execute: Callable[[str, ExecutionRequest], None],
    ) -> None:
        self.delegate = delegate
        self.id = f"probe-{index}"
        self.version = "1"
        self.networked = False
        self.pipeline = delegate.pipeline.model_copy(update={"adapter_id": self.id})
        self.compatibility = delegate.compatibility
        self.pricing = delegate.pricing
        self.invoke = execute
        self.throttle_scope: str | None = None

    def execution_settings(self) -> dict[str, str]:
        return {}

    def validate_execution(self) -> None:
        return None

    def execute(self, request: ExecutionRequest) -> ExecutionOutput:
        assert request.sample.reference is None
        self.invoke(self.id, request)
        return self.delegate.execute(request)

    def parse(self, output: ExecutionOutput) -> ParsedOutput:
        return self.delegate.parse(output)


@dataclass(frozen=True)
class ProbePack:
    definition: BenchmarkDefinition
    dataset: DatasetAdapter
    solutions: tuple[SolutionAdapter, ...]
    evaluator: Evaluator
    viewer: ArtifactViewer
    scheduling_policy: SchedulingPolicy | None


def install_probe(
    monkeypatch: pytest.MonkeyPatch,
    pack_root: Path,
    execute: Callable[[str, ExecutionRequest], None],
    *,
    parallel: bool = True,
    count: int = 6,
    samples: int = 2,
) -> Callable[..., BenchmarkPack]:
    def factory(*_: object) -> BenchmarkPack:
        original = get_pack("synthetic_text_intent", pack_root)
        return ProbePack(
            original.definition,
            ProbeDataset(original.dataset, samples),
            tuple(ProbeSolution(original.solutions[0], index, execute) for index in range(count)),
            original.evaluator,
            original.viewer,
            SchedulingPolicy() if parallel else None,
        )

    monkeypatch.setattr("ai_bench.engine.get_pack", factory)
    monkeypatch.setattr("ai_bench.worker.get_pack", factory)
    return factory


@pytest.fixture(params=[False, True], ids=["sqlite", "table-projection"])
def engine(request: pytest.FixtureRequest, tmp_path: Path, pack_root: Path) -> BenchmarkEngine:
    return BenchmarkEngine(
        tmp_path,
        pack_root,
        backend=local_backend(tmp_path, durable_projections=request.param),
    )


def test_five_global_lanes_fast_progress_and_one_owner_across_runs(
    engine: BenchmarkEngine, pack_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    condition = threading.Condition()
    releases = {f"probe-{index}": threading.Event() for index in range(6)}
    executing: set[str] = set()
    started: list[tuple[str, str]] = []
    maximum = 0
    progressed = threading.Event()

    def execute(identifier: str, request: ExecutionRequest) -> None:
        nonlocal maximum
        with condition:
            assert identifier not in executing
            executing.add(identifier)
            maximum = max(maximum, len(executing))
            assert maximum <= 5
            started.append((identifier, request.run_id))
            condition.notify_all()
        try:
            assert releases[identifier].wait(30)
        finally:
            with condition:
                executing.remove(identifier)
                condition.notify_all()

    install_probe(monkeypatch, pack_root, execute)
    manifests = [engine.create_run("synthetic_text_intent") for _ in range(2)]
    supervisor = RunSupervisor(engine)
    claim_threads: set[int] = set()
    claim = engine.repository.claim_task

    def claim_on_owner(
        owner: str,
        lease_seconds: int = 60,
        run_id: str | None = None,
        *,
        allow_networked: bool = True,
        eligible_tasks: EligibleTasks | None = None,
    ) -> Lease | None:
        claim_threads.add(threading.get_ident())
        return claim(
            owner,
            lease_seconds,
            run_id,
            allow_networked=allow_networked,
            eligible_tasks=eligible_tasks,
        )

    monkeypatch.setattr(engine.repository, "claim_task", claim_on_owner)
    complete = engine.repository.complete_attempt
    completed: list[str] = []

    def completion(lease: Lease, result: object) -> None:
        from ai_bench.contracts import TaskResult

        assert isinstance(result, TaskResult)
        complete(lease, result)
        completed.append(lease.task.solution_id)
        if len(completed) >= 2:
            progressed.set()

    monkeypatch.setattr(engine.repository, "complete_attempt", completion)
    supervisor.start()
    try:
        with condition:
            assert condition.wait_for(lambda: len(executing) == 5, timeout=30)
            assert len({identifier for identifier, _ in started}) == 5
            slow = next(iter(executing))
        for identifier, event in releases.items():
            if identifier != slow:
                event.set()
        assert progressed.wait(30)
        assert not releases[slow].is_set()
        with condition:
            assert condition.wait_for(
                lambda: any(
                    sum(identifier == candidate for identifier, _ in started) >= 2
                    for candidate in releases
                    if candidate != slow
                ),
                timeout=30,
            )
            assert slow in executing
    finally:
        for event in releases.values():
            event.set()
        supervisor.stop()
    assert not supervisor.running
    assert maximum == 5
    assert len(claim_threads) == 1
    assert claim_threads != {threading.get_ident()}
    for manifest in manifests:
        report = engine.run_to_completion(manifest.run_id)
        assert len(report.results) == 12
        assert all(result.status is TaskStatus.SUCCEEDED for result in report.results)
        assert all(result.attempts == 1 for result in report.results)
        assert sum(values["calls"] for values in report.usage.values()) == 12
        assert len(report.quality) == len(report.cost) == len(report.latency) == 6
        events = engine.repository.list_events(manifest.run_id)
        assert [event.sequence for event in events] == sorted({event.sequence for event in events})
        assert sum(event.event_type == "task.succeeded" for event in events) == 12
        assert sum(event.event_type == "run.finalized" for event in events) == 1


def test_serial_policy_does_not_admit_while_a_call_is_blocked(
    engine: BenchmarkEngine, pack_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, release, waiting = threading.Event(), threading.Event(), threading.Event()
    calls: list[str] = []

    def execute(identifier: str, _: ExecutionRequest) -> None:
        calls.append(identifier)
        started.set()
        assert release.wait(30)

    install_probe(monkeypatch, pack_root, execute, parallel=False)
    manifest = engine.create_run("synthetic_text_intent")
    delay = engine.repository.next_work_delay

    def next_delay(*args: object, **kwargs: object) -> float:
        assert kwargs["eligible_tasks"] == frozenset()
        waiting.set()
        return 5.0

    monkeypatch.setattr(engine.repository, "next_work_delay", next_delay)
    supervisor = RunSupervisor(engine)
    supervisor.start()
    try:
        assert started.wait(30) and waiting.wait(30)
        assert len(calls) == 1
        assert supervisor._pool is not None
        pool = supervisor._pool
        supervisor.stop()
        assert supervisor.running
        supervisor.start()
        assert supervisor._pool is pool
        assert len(calls) == 1
    finally:
        release.set()
        supervisor.stop()
        monkeypatch.setattr(engine.repository, "next_work_delay", delay)
    assert not supervisor.running
    finalized = threading.Event()
    finalize = engine._finalize

    def restarted_finish(run_id: str) -> object:
        report = finalize(run_id)
        finalized.set()
        return report

    monkeypatch.setattr(engine, "_finalize", restarted_finish)
    supervisor.start()
    try:
        assert finalized.wait(30)
    finally:
        supervisor.stop()
    report = engine.get_report(manifest.run_id)
    assert report is not None and all(result.attempts == 1 for result in report.results)


def test_eligibility_skips_more_than_one_page_without_leasing_waiters(
    engine: BenchmarkEngine, pack_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_probe(monkeypatch, pack_root, lambda *_: None, count=2, samples=120)
    manifest = engine.create_run("synthetic_text_intent")
    allowed = frozenset({(manifest.run_id, "probe-1")})
    repository = engine.repository
    if isinstance(repository, SqliteRepository):
        with repository._connection(immediate=True) as connection:
            connection.execute(
                "UPDATE tasks SET next_available=1 WHERE run_id=? AND solution_id='probe-1'",
                (manifest.run_id,),
            )
    else:
        assert isinstance(repository, AzureTableRepository)
        for task in repository.list_tasks(manifest.run_id):
            if task.solution_id != "probe-1":
                continue
            row = repository._required(f"task:{task.id}")
            due = repository._required(str(row.values["due_key"]))
            key = repository._due_key(task.id, 1)
            repository.state.transact(
                (
                    StateChange(
                        row.key, {**row.values, "due_key": key, "next_available": 1}, row.etag
                    ),
                    StateChange(due.key, None, due.etag),
                    StateChange(key, due.values),
                )
            )
        first_page = repository.state.page("due:", limit=100)
        assert len(first_page.rows) == 100
        assert all(
            repository._required(f"task:{row.values['task_id']}").values["solution_id"] == "probe-0"
            for row in first_page.rows
        )
    for _ in range(3):
        lease = engine.repository.claim_task("owner", eligible_tasks=allowed)
        assert lease is not None and lease.task.solution_id == "probe-1"
        engine.repository.fail_attempt(lease, RuntimeError("synthetic"), retry_delay_seconds=60)
    blocked = [
        task
        for task in engine.repository.list_tasks(manifest.run_id)
        if task.solution_id == "probe-0"
    ]
    assert len(blocked) == 120 and all(task.attempts == 0 for task in blocked)
    assert engine.repository.claim_task("owner", eligible_tasks=frozenset()) is None
    assert engine.repository.next_work_delay(eligible_tasks=frozenset()) == 5
    assert all(engine.repository.list_attempts(task.id) == () for task in blocked)


def test_old_manifest_bytes_and_idempotency_survive_pack_opt_in(
    engine: BenchmarkEngine, pack_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_probe(monkeypatch, pack_root, lambda *_: None, parallel=False)
    previous = engine.create_run("synthetic_text_intent", idempotency_key="old-request")
    encoded = canonical_json(previous)
    assert b"scheduling_policy" not in encoded
    assert "scheduling-policy" not in previous.descriptor_hashes
    install_probe(monkeypatch, pack_root, lambda *_: None)
    replay = engine.create_run("synthetic_text_intent", idempotency_key="old-request")
    assert canonical_json(replay) == encoded
    assert replay.run_id == previous.run_id
    with pytest.raises(IdempotencyConflictError):
        engine.create_run("synthetic_text_intent", retry_limit=2, idempotency_key="old-request")
    fresh = engine.create_run("synthetic_text_intent", idempotency_key="new-request")
    assert fresh.scheduling_policy == SchedulingPolicy()
    assert fresh.descriptor_hashes["scheduling-policy"] == sha256_digest(SchedulingPolicy())
    assert RunManifest.model_validate_json(encoded) == previous
    assert engine.run_to_completion(previous.run_id).status.value == "completed"


def test_unknown_policy_fails_closed(pack_root: Path) -> None:
    with pytest.raises(ValidationError):
        SchedulingPolicy.model_validate({"version": "future-policy"})
    with pytest.raises(ValidationError):
        SchedulingPolicy.model_validate({"max_in_flight": 6})
    original = get_pack("synthetic_text_intent", pack_root)
    invalid = ProbePack(
        original.definition,
        original.dataset,
        original.solutions,
        original.evaluator,
        original.viewer,
        None,
    )
    object.__setattr__(invalid, "scheduling_policy", {"version": "bounded-solutions-v1"})
    with pytest.raises(ValueError, match="typed versioned"):
        validate_pack(invalid)


def test_authorization_loss_before_dispatch_releases_without_consuming_attempt(
    engine: BenchmarkEngine, pack_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_bench.engine import NetworkExecutionDisabledError

    install_probe(monkeypatch, pack_root, lambda *_: pytest.fail("dispatch was forbidden"))
    manifest = engine.create_run("synthetic_text_intent")
    lease = engine.repository.claim_task("owner")
    assert lease is not None
    with pytest.raises(NetworkExecutionDisabledError):
        engine._execute_lease(lease, dispatch_allowed=lambda: False)
    engine.repository.release_unstarted(lease)
    assert engine.repository.list_attempts(lease.task.id) == ()
    assert all(task.attempts == 0 for task in engine.repository.list_tasks(manifest.run_id))


def test_expiry_during_call_does_not_publish_a_result(
    engine: BenchmarkEngine, pack_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_bench.repositories import LeaseLostError

    def expire(*_: object) -> None:
        engine.lifecycle.clock = lambda: utc_now() + timedelta(days=11)

    install_probe(monkeypatch, pack_root, expire)
    engine.create_run("synthetic_text_intent")
    lease = engine.repository.claim_task("owner")
    assert lease is not None
    with pytest.raises(LeaseLostError):
        engine._execute_lease(lease)
    engine.lifecycle.clock = utc_now
    task = engine.repository.get_task(lease.task.id)
    assert task is not None and task.result is None
    assert engine.repository.acquire_finalizer(lease.task.run_id) is None


def test_table_completion_conflicts_keep_unique_events_and_bounded_payload_writes(
    tmp_path: Path, pack_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = BenchmarkEngine(
        tmp_path, pack_root, backend=local_backend(tmp_path, durable_projections=True)
    )
    install_probe(monkeypatch, pack_root, lambda *_: None, count=5, samples=1)
    manifest = engine.create_run("synthetic_text_intent", retry_limit=1)
    repository = engine.repository
    assert isinstance(repository, AzureTableRepository)
    leases = [repository.claim_task("owner") for _ in range(5)]
    assert all(lease is not None for lease in leases)
    barrier = threading.Barrier(5)
    local = threading.local()
    transaction = repository.state.transact
    writes: dict[str, int] = {}
    write_lock = threading.Lock()
    immutable = repository._immutable

    def store(value: object, run_id: str | None = None) -> str:
        if isinstance(value, TaskAttempt) and value.status.value == "failed":
            with write_lock:
                writes[value.task_id] = writes.get(value.task_id, 0) + 1
        return immutable(value, run_id)

    def contend(changes: Sequence[StateChange]) -> None:
        if any(
            change.values is not None and change.values.get("event_type") == "task.poisoned"
            for change in changes
        ) and not getattr(local, "contended", False):
            local.contended = True
            barrier.wait(timeout=30)
            raise StateConflictError("synthetic simultaneous ETag conflict")
        transaction(changes)

    monkeypatch.setattr(repository, "_immutable", store)
    monkeypatch.setattr(repository.state, "transact", contend)

    def fail(lease: Lease | None) -> bool:
        assert lease is not None
        return repository.fail_attempt(lease, RuntimeError("synthetic terminal error"))

    with ThreadPoolExecutor(max_workers=5) as pool:
        assert all(pool.map(fail, leases))
    assert writes == {lease.task.id: 1 for lease in leases if lease is not None}
    tasks = repository.list_tasks(manifest.run_id)
    assert all(task.status is TaskStatus.POISONED for task in tasks)
    events = repository.list_events(manifest.run_id)
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert sum(event.event_type == "task.poisoned" for event in events) == 5


def test_table_release_conflicts_retry_or_surface_without_discarding_lease(
    tmp_path: Path, pack_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = BenchmarkEngine(
        tmp_path, pack_root, backend=local_backend(tmp_path, durable_projections=True)
    )
    engine.create_run("synthetic_text_intent")
    repository = engine.repository
    lease = repository.claim_task("owner")
    assert isinstance(repository, AzureTableRepository) and lease is not None
    transaction = repository.state.transact
    conflicts = 0

    def conflict(changes: Sequence[StateChange]) -> None:
        nonlocal conflicts
        conflicts += 1
        if conflicts <= 20:
            raise StateConflictError("synthetic release conflict")
        transaction(changes)

    monkeypatch.setattr(repository.state, "transact", conflict)
    with pytest.raises(StateConflictError, match="unstarted release contention"):
        repository.release_unstarted(lease)
    assert conflicts == 20
    task = repository.get_task(lease.task.id)
    assert task is not None and task.status is TaskStatus.LEASED and task.attempts == 1
    repository.release_unstarted(lease)
    task = repository.get_task(lease.task.id)
    assert task is not None and task.status is TaskStatus.PENDING and task.attempts == 0


def test_pack_discovery_is_serialized_but_instances_are_private(
    pack_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_bench import packs

    load = packs._load_pack
    lock = threading.Lock()
    loading = maximum = 0
    barrier = threading.Barrier(5)

    def observed(path: Path, name: str) -> BenchmarkPack:
        nonlocal loading, maximum
        with lock:
            loading += 1
            maximum = max(maximum, loading)
        try:
            return load(path, name)
        finally:
            with lock:
                loading -= 1

    def discover(_: int) -> BenchmarkPack:
        barrier.wait(timeout=10)
        return get_pack("speech_to_text", pack_root)

    monkeypatch.setattr(packs, "_load_pack", observed)
    with ThreadPoolExecutor(max_workers=5) as pool:
        discovered = list(pool.map(discover, range(5)))
    assert maximum == 1
    assert len({id(pack.evaluator) for pack in discovered}) == 5
    assert len({id(pack.solutions[0]) for pack in discovered}) == 5


def test_shared_scope_cooldown_blocks_both_solutions_but_not_independent_work(
    engine: BenchmarkEngine, pack_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    factory = install_probe(monkeypatch, pack_root, lambda *_: None, count=3)
    source = factory()
    assert isinstance(source, ProbePack)
    solutions = source.solutions
    shared = sha256_digest({"synthetic-scope": "shared"})
    for index, solution in enumerate(solutions):
        assert isinstance(solution, ProbeSolution)
        solution.throttle_scope = shared if index < 2 else sha256_digest({"scope": "independent"})
    selected = replace(source, solutions=solutions)
    monkeypatch.setattr("ai_bench.engine.get_pack", lambda *_: selected)
    manifest = engine.create_run("synthetic_text_intent")
    repository = engine.repository
    first = repository.claim_task("owner", eligible_tasks=frozenset({(manifest.run_id, "probe-0")}))
    assert first is not None
    failure = ProviderFailureEvidence(
        classification="rate-limit",
        message="Synthetic shared-scope cooldown.",
        http_status=429,
        retryable=True,
        usage=Usage(
            dimensions={"calls": Decimal(1)}, complete=False, uncertainty="Unknown billing."
        ),
        retry_delay=RetryDelay(scheduled_seconds=Decimal(60), source="retry-after-seconds"),
    )
    repository.record_attempt_failure(first, failure)
    repository.fail_attempt(first, ProviderFailure(failure))
    assert (
        repository.claim_task("owner", eligible_tasks=frozenset({(manifest.run_id, "probe-1")}))
        is None
    )
    independent = repository.claim_task(
        "owner", eligible_tasks=frozenset({(manifest.run_id, "probe-2")})
    )
    assert independent is not None
    engine._execute_lease(independent)
    tasks = repository.list_tasks(manifest.run_id)
    assert all(task.attempts == 0 for task in tasks if task.solution_id == "probe-1")
    assert repository.list_attempts(first.task.id)[0].failure == failure


def test_expired_lease_recovery_excludes_running_executor_tasks(
    engine: BenchmarkEngine, pack_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_probe(monkeypatch, pack_root, lambda *_: None, count=2, samples=1)
    engine.create_run("synthetic_text_intent")
    first = engine.repository.claim_task("owner", lease_seconds=1)
    second = engine.repository.claim_task("owner", lease_seconds=1)
    assert first is not None and second is not None
    now = utc_now() + timedelta(seconds=3)
    if isinstance(engine.repository, AzureTableRepository):
        engine.repository.clock = lambda: now
    else:
        monkeypatch.setattr("ai_bench.repositories.time.time", lambda: now.timestamp())
    assert engine.repository.recover_expired_leases(active_task_ids=frozenset({first.task.id})) == (
        1,
        0,
    )
    task = engine.repository.get_task(first.task.id)
    assert task is not None and task.status is TaskStatus.LEASED
    assert engine.repository.recover_expired_leases() == (1, 0)


def test_manifest_serialization_schema_keeps_typed_fields() -> None:
    schema = RunManifest.model_json_schema(mode="serialization")
    assert {"run_id", "solution_descriptors", "scheduling_policy"} <= schema["properties"].keys()
    assert "scheduling_policy" not in schema["required"]
    assert schema["$defs"]["SchedulingPolicy"]["properties"]["max_in_flight"]["const"] == 5


def test_prepared_execution_rechecks_pinned_settings_before_dispatch(
    engine: BenchmarkEngine, pack_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_bench.repositories import RepositoryError

    factory = install_probe(
        monkeypatch,
        pack_root,
        lambda *_: pytest.fail("changed settings must not dispatch"),
        count=1,
        samples=1,
    )
    manifest = engine.create_run("synthetic_text_intent")
    prepared = engine.prepare_execution(manifest, factory())
    solution = prepared.pack.solutions[0]
    assert isinstance(solution, ProbeSolution)
    monkeypatch.setattr(solution, "execution_settings", lambda: {"selector": "changed"})
    lease = engine.repository.claim_task("owner")
    assert lease is not None
    with pytest.raises(RepositoryError, match="descriptor drift"):
        engine._execute_lease(lease, prepared=prepared)
    assert engine.repository.list_attempts(lease.task.id)[0].telemetry is None


def test_finalization_waits_for_executor_completion_and_uploads_remain_responsive(
    engine: BenchmarkEngine, pack_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_probe(monkeypatch, pack_root, lambda *_: None, count=1, samples=1)
    manifest = engine.create_run("synthetic_text_intent")
    committed, release = threading.Event(), threading.Event()
    finalized, maintained = threading.Event(), threading.Event()
    complete, finalize = engine.repository.complete_attempt, engine._finalize
    supervisor = RunSupervisor(engine)
    process = engine.uploads.process_next

    def finish_attempt(lease: Lease, result: TaskResult) -> None:
        complete(lease, result)
        committed.set()
        supervisor.wake()
        assert release.wait(30)

    def finish_run(run_id: str) -> object:
        assert release.is_set()
        assert not any(active.lease.task.run_id == run_id for active in supervisor._active.values())
        report = finalize(run_id)
        finalized.set()
        return report

    def upload_work(resolver: object) -> bool:
        if committed.is_set():
            maintained.set()
        return False

    monkeypatch.setattr(engine.repository, "complete_attempt", finish_attempt)
    monkeypatch.setattr(engine, "_finalize", finish_run)
    monkeypatch.setattr(engine.uploads, "process_next", upload_work)
    supervisor.start()
    try:
        assert committed.wait(30) and maintained.wait(30)
        assert engine.get_report(manifest.run_id) is None
        assert not finalized.is_set()
        release.set()
        assert finalized.wait(30)
    finally:
        release.set()
        supervisor.stop()
        monkeypatch.setattr(engine.uploads, "process_next", process)
