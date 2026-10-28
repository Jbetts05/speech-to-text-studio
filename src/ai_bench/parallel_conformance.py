"""Operator-only, zero-provider concurrency proof using an installed synthetic pack."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from unittest.mock import patch

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
    SchedulingPolicy,
    SolutionAdapter,
    TaskResult,
    TaskStatus,
)
from ai_bench.engine import BenchmarkEngine
from ai_bench.hashing import sha256_digest
from ai_bench.packs import BenchmarkPack, get_pack
from ai_bench.repositories import EligibleTasks, Lease
from ai_bench.worker import RunSupervisor

# Concurrency is observed under a tight budget because a serialized scheduler
# would never satisfy it. Draining durable evidence afterwards is throughput,
# not concurrency, so it only needs a hang guard.
OBSERVATION_SECONDS = 120
DRAIN_SECONDS = 900


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


class _Dataset:
    def __init__(self, delegate: DatasetAdapter) -> None:
        self.delegate = delegate
        self.id = delegate.id
        self._samples = tuple(
            delegate.samples()[0].model_copy(update={"id": f"parallel-case-{index}"})
            for index in range(2)
        )
        self.revision = delegate.revision.model_copy(
            update={"content_hash": sha256_digest(self._samples)}
        )

    def samples(self) -> Sequence[BenchmarkSample]:
        return self._samples

    def read_artifact(self, artifact: ModalityArtifact) -> bytes:
        return self.delegate.read_artifact(artifact)


class _Solution:
    def __init__(
        self,
        delegate: SolutionAdapter,
        index: int,
        observe: Callable[[str, ExecutionRequest], None],
    ) -> None:
        self.delegate = delegate
        self.id = f"parallel-probe-{index}"
        self.version = "1"
        self.networked = False
        self.pipeline = delegate.pipeline.model_copy(update={"adapter_id": self.id})
        self.compatibility = delegate.compatibility
        self.pricing = delegate.pricing
        self.observe = observe

    def execute(self, request: ExecutionRequest) -> ExecutionOutput:
        self.observe(self.id, request)
        return self.delegate.execute(request)

    def parse(self, output: ExecutionOutput) -> ParsedOutput:
        return self.delegate.parse(output)


@dataclass(frozen=True)
class _Pack:
    definition: BenchmarkDefinition
    dataset: DatasetAdapter
    solutions: tuple[SolutionAdapter, ...]
    evaluator: Evaluator
    viewer: ArtifactViewer
    scheduling_policy: SchedulingPolicy = field(default_factory=SchedulingPolicy)


def parallel_execution_scenarios(
    engine: BenchmarkEngine, benchmark_id: str, solution_id: str
) -> tuple[str, ...]:
    original = get_pack(benchmark_id, engine.pack_root)
    selected = next(item for item in original.solutions if item.id == solution_id)
    if selected.networked or not original.dataset.revision.provenance.synthetic:
        raise ValueError("parallel proof requires an offline solution and synthetic dataset")
    condition = threading.Condition()
    releases = {f"parallel-probe-{index}": threading.Event() for index in range(6)}
    executing: set[str] = set()
    calls: list[tuple[str, str]] = []
    maximum = 0
    progressed = threading.Event()
    finalized = threading.Event()
    completed = 0
    finalized_runs: set[str] = set()
    owner_threads: set[int] = set()
    slow: str | None = None
    invalid_progress = threading.Event()
    expired = threading.Event()
    cleanup_started = threading.Event()

    def observe(identifier: str, request: ExecutionRequest) -> None:
        nonlocal maximum
        with condition:
            if identifier in executing:
                raise AssertionError("a solution had overlapping executions across runs")
            executing.add(identifier)
            maximum = max(maximum, len(executing))
            if maximum > 5:
                raise AssertionError("global execution capacity exceeded five")
            calls.append((identifier, request.run_id))
            condition.notify_all()
        try:
            # Only the controller releases a lane. Its overall watchdog always
            # opens every barrier in finally, including failed observations.
            releases[identifier].wait()
        finally:
            with condition:
                executing.remove(identifier)
                condition.notify_all()

    def factory(*_: object) -> BenchmarkPack:
        pack = get_pack(benchmark_id, engine.pack_root)
        delegate = next(item for item in pack.solutions if item.id == solution_id)
        if delegate.networked or not pack.dataset.revision.provenance.synthetic:
            raise ValueError("parallel proof configuration changed to a non-synthetic provider")
        return _Pack(
            pack.definition,
            _Dataset(pack.dataset),
            tuple(_Solution(delegate, index, observe) for index in range(6)),
            pack.evaluator,
            pack.viewer,
        )

    claim = engine.repository.claim_task
    complete = engine.repository.complete_attempt
    finalize = engine._finalize

    def claim_owned(
        owner: str,
        lease_seconds: int = 60,
        run_id: str | None = None,
        *,
        allow_networked: bool = True,
        eligible_tasks: EligibleTasks | None = None,
    ) -> Lease | None:
        owner_threads.add(threading.get_ident())
        return claim(
            owner,
            lease_seconds,
            run_id,
            allow_networked=allow_networked,
            eligible_tasks=eligible_tasks,
        )

    def completion(lease: Lease, result: TaskResult) -> None:
        nonlocal completed
        complete(lease, result)
        with condition:
            completed += 1
            if completed == 2:
                if (
                    expired.is_set()
                    or _remaining(deadline) <= 0
                    or cleanup_started.is_set()
                    or slow is None
                    or slow not in executing
                    or releases[slow].is_set()
                ):
                    invalid_progress.set()
                progressed.set()

    def finish(run_id: str) -> object:
        report = finalize(run_id)
        finalized_runs.add(run_id)
        if len(finalized_runs) == 2:
            finalized.set()
        return report

    with (
        patch("ai_bench.engine.get_pack", factory),
        patch("ai_bench.worker.get_pack", factory),
        patch.object(engine.repository, "claim_task", claim_owned),
        patch.object(engine.repository, "complete_attempt", completion),
        patch.object(engine, "_finalize", finish),
    ):
        manifests = [engine.create_run(benchmark_id) for _ in range(2)]
        supervisor = RunSupervisor(engine)
        # Independent lane timers cannot expire the slow call during a later phase.
        deadline = time.monotonic() + OBSERVATION_SECONDS

        def expire() -> None:
            with condition:
                expired.set()
                for event in releases.values():
                    event.set()
                condition.notify_all()
                progressed.set()
                finalized.set()

        watchdog = threading.Timer(_remaining(deadline), expire)
        watchdog.daemon = True
        watchdog.start()
        supervisor.start()
        try:
            with condition:
                if (
                    not condition.wait_for(
                        lambda: len(executing) == 5 or expired.is_set(),
                        timeout=min(30, _remaining(deadline)),
                    )
                    or expired.is_set()
                    or _remaining(deadline) <= 0
                ):
                    raise AssertionError("five distinct solutions did not execute simultaneously")
                slow = min(executing)
            for identifier, event in releases.items():
                if identifier != slow:
                    event.set()
            if not progressed.wait(_remaining(deadline)) or invalid_progress.is_set():
                raise AssertionError("fast lanes did not progress beside the blocked request")
            with condition:
                if (
                    expired.is_set()
                    or _remaining(deadline) <= 0
                    or slow not in executing
                    or releases[slow].is_set()
                ):
                    raise AssertionError("slow invocation ended before the progress observation")
            releases[slow].set()
            # Concurrency is now proven. Publishing terminal reports only drains
            # durable evidence, so it is bounded to detect a hang, not to assert a
            # persistence rate.
            watchdog.cancel()
            watchdog.join(timeout=1)
            drain = time.monotonic() + DRAIN_SECONDS
            watchdog = threading.Timer(_remaining(drain), expire)
            watchdog.daemon = True
            watchdog.start()
            if not finalized.wait(_remaining(drain)) or expired.is_set():
                raise AssertionError("parallel runs did not publish complete terminal reports")
        finally:
            with condition:
                cleanup_started.set()
                for event in releases.values():
                    event.set()
            watchdog.cancel()
            watchdog.join(timeout=1)
            supervisor.stop()
        if supervisor.running or len(owner_threads) != 1 or maximum != 5:
            raise AssertionError("supervisor ownership, drain, or concurrency bounds failed")
        if len(calls) != 24:
            raise AssertionError("parallel execution duplicated or omitted a synthetic call")
        for manifest in manifests:
            report = engine.get_report(manifest.run_id)
            if (
                report is None
                or len(report.results) != 12
                or any(
                    item.status is not TaskStatus.SUCCEEDED or item.attempts != 1
                    for item in report.results
                )
            ):
                raise AssertionError("parallel execution lost or duplicated canonical results")
            events = engine.repository.list_events(manifest.run_id)
            if [event.sequence for event in events] != list(range(1, len(events) + 1)) or sum(
                event.event_type == "task.succeeded" for event in events
            ) != 12:
                raise AssertionError("same-run parallel completion dropped or duplicated events")
            if any(
                len(history) != 1 or history[0].telemetry is None
                for history in report.attempt_history.values()
            ):
                raise AssertionError("parallel completion lost attempt telemetry")
    return (
        "bounded-parallel-single-owner",
        "global-solution-lanes-fast-progress",
        "same-run-parallel-evidence-events",
    )
