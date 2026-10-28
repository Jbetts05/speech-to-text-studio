"""One fenced admission coordinator with bounded, independently advancing lanes."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from uuid import uuid4

from ai_bench.contracts import TaskStatus
from ai_bench.engine import BenchmarkEngine, NetworkExecutionDisabledError, PreparedExecution
from ai_bench.lifecycle import ExpiredRootError, RootUnavailableError
from ai_bench.packs import PackNotFoundError, get_pack
from ai_bench.receipts import AttemptPersistencePendingError, UncertainDispatchError
from ai_bench.repositories import EligibleTasks, Lease, LeaseLostError, RepositoryError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _InFlight:
    lease: Lease
    lane: tuple[str, str]
    parallel: bool


class RunSupervisor:
    def __init__(self, engine: BenchmarkEngine) -> None:
        self.engine = engine
        self.owner = f"supervisor-{uuid4().hex}"
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._active: dict[Future[None], _InFlight] = {}
        self._eligible: EligibleTasks = frozenset()
        self.error: str | None = None
        self._next_maintenance = 0.0
        self._validated_runs: set[str] = set()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        # The owner remains alive while draining. A timed-out stop cannot create
        # a replacement executor alongside requests that are still in flight.
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ai-bench-worker", daemon=True)
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            if not self._thread.is_alive():
                self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with self.engine.backend.execution_session():
                    self._run_loop()
            except Exception:
                self.error = "Storage session is temporarily unavailable; startup is retrying."
                logger.exception(self.error)
                self._stop.wait(5)

    def _run_loop(self) -> None:
        backoff = 0.5
        while not self._stop.is_set():
            try:
                self.engine.backend.refresh_proof()
                self.engine.backend.require_execution_ready()
                if not self.engine.repository.acquire_supervisor(self.owner):
                    self._wake.wait(5)
                    self._wake.clear()
                    continue
                self._validated_runs.clear()
                self._lost.clear()
                heartbeat_stop = threading.Event()
                heartbeat = threading.Thread(
                    target=self._renew,
                    args=(heartbeat_stop,),
                    name="ai-bench-supervisor-lease",
                    daemon=True,
                )
                heartbeat.start()
                self._pool = ThreadPoolExecutor(max_workers=5, thread_name_prefix="ai-bench-lane")
                try:
                    while self._admitting():
                        self._wake.clear()
                        self.error = None
                        worked = self.step()
                        backoff = 0.5
                        if not worked:
                            self._wake.wait(
                                self.engine.repository.next_work_delay(
                                    allow_networked=self.engine._network_execution_enabled(),
                                    eligible_tasks=self._eligible,
                                )
                            )
                finally:
                    # Futures cannot cancel an in-flight HTTP request. Close
                    # admission, drain, then release leadership and its heartbeat.
                    self._lost.set()
                    try:
                        self._pool.shutdown(wait=True)
                        self._collect()
                    finally:
                        self._pool = None
                        heartbeat_stop.set()
                        heartbeat.join()
                        self.engine.repository.release_supervisor(self.owner)
            except Exception:
                self.error = "Durable execution is temporarily unavailable; admission is paused."
                logger.exception(self.error)
                self._stop.wait(backoff)
                backoff = min(30.0, backoff * 2)

    def _admitting(self) -> bool:
        return (
            not self._stop.is_set()
            and not self._lost.is_set()
            and (
                self.engine.backend.name != "azure-table-blob"
                or self.engine.backend.live_proof
                or self.engine.backend.operator_conformance
            )
        )

    def _renew(self, stopped: threading.Event) -> None:
        try:
            while not stopped.wait(5):
                if not self.engine.repository.acquire_supervisor(self.owner):
                    self._lost.set()
                    self._wake.set()
                    return
        except Exception:
            self.error = "Supervisor lease renewal failed; execution admission is paused."
            logger.exception(self.error)
            self._lost.set()
            self._wake.set()

    def _collect(self) -> bool:
        worked = False
        for future, active in tuple(self._active.items()):
            if not future.done():
                continue
            try:
                self._settle(future, active.lease)
            except LeaseLostError:
                logger.warning("Attempt lost its lease: %s", active.lease.task.id)
            except (ExpiredRootError, RootUnavailableError):
                logger.warning("Attempt retention ended: %s", active.lease.task.id)
            finally:
                # On a storage error the owner closes admission and drains. The
                # still-leased record, not a discarded future, drives recovery.
                del self._active[future]
            worked = True
        return worked

    def _settle(self, future: Future[None], lease: Lease) -> None:
        try:
            future.result()
        except (LeaseLostError, ExpiredRootError, RootUnavailableError):
            raise
        except NetworkExecutionDisabledError:
            self.engine.repository.release_unstarted(lease)
        except AttemptPersistencePendingError:
            self.error = (
                "Attempt persistence is pending; recorded responses resume without another call."
            )
            logger.warning(self.error)
        except UncertainDispatchError:
            self.error = "Provider outcome is indeterminate; availability-first recovery may retry."
            logger.warning(self.error)
        except Exception as error:
            self.engine.repository.fail_attempt(lease, error)

    def step(self) -> bool:
        repository = self.engine.repository
        worked = self._collect()
        self.engine.backend.refresh_proof()
        self.engine.backend.require_execution_ready()
        if not self._active and time.monotonic() >= self._next_maintenance:
            self.engine.reap_expired()
            self._next_maintenance = time.monotonic() + 30
        if self.engine.uploads.process_next(self.engine.dataset_importer):
            worked = True
        self.engine.recover_initializations()
        repository.recover_expired_leases(
            active_task_ids=frozenset(active.lease.task.id for active in self._active.values())
        )
        if not self._admitting() or len(self._active) >= 5:
            self._eligible = frozenset()
            return worked
        lanes = {active.lane for active in self._active.values()}
        active_runs = {active.lease.task.run_id for active in self._active.values()}
        serial_active = any(not active.parallel for active in self._active.values())
        runs = repository.unfinished_runs()
        self._validated_runs.intersection_update(run.manifest.run_id for run in runs)
        eligible: set[tuple[str, str]] = set()
        prepared: dict[str, PreparedExecution] = {}
        for run in runs:
            manifest, run_id = run.manifest, run.manifest.run_id
            if serial_active or (self._active and manifest.scheduling_policy is None):
                continue
            available = {
                (run_id, solution_id)
                for solution_id in manifest.solution_ids
                if (manifest.benchmark.id, solution_id) not in lanes
            }
            if not available:
                continue
            try:
                if run_id not in self._validated_runs:
                    self.engine.repair_initialization(manifest)
                    self._validated_runs.add(run_id)
                # Discovery and construction run only on the admission thread.
                # Every submission gets its own pack/evaluator/adapter instances.
                pack = get_pack(manifest.benchmark.id, self.engine.pack_root)
                prepared[run_id] = self.engine.prepare_execution(manifest, pack)
            except (ValueError, OSError, RepositoryError, PackNotFoundError) as error:
                repository.set_runtime_error(
                    run_id,
                    "Run is blocked by unavailable or changed pinned evidence/configuration "
                    f"({type(error).__name__}).",
                )
                continue
            if run_id not in active_runs:
                tasks = repository.list_tasks(run_id)
                if tasks and all(
                    task.status in {TaskStatus.SUCCEEDED, TaskStatus.POISONED} for task in tasks
                ):
                    self.engine._finalize(run_id)
                    worked = True
                    continue
            if manifest.networked_execution and not self.engine._network_execution_enabled():
                repository.set_runtime_error(run_id, "Runtime network authorization is disabled.")
                continue
            repository.set_runtime_error(run_id, None)
            eligible.update(available)
        self._eligible = frozenset(eligible)
        if not self._eligible or not self._admitting():
            return worked
        lease = repository.claim_task(
            self.owner,
            self.engine.lease_seconds,
            allow_networked=self.engine._network_execution_enabled(),
            eligible_tasks=self._eligible,
        )
        if lease is None:
            return worked
        execution = prepared[lease.task.run_id]
        if self._pool is None:
            raise RepositoryError("supervisor execution requires its lifecycle-managed pool")
        future = self._pool.submit(
            self.engine._execute_lease,
            lease,
            prepared=execution,
            dispatch_allowed=self._admitting,
        )
        self._active[future] = _InFlight(
            lease,
            (execution.manifest.benchmark.id, lease.task.solution_id),
            execution.manifest.scheduling_policy is not None,
        )
        future.add_done_callback(lambda _: self._wake.set())
        return True
