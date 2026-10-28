"""ETag-fenced execution projections over a bounded, single Table partition."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import suppress
from datetime import datetime
from decimal import Decimal
from typing import Any

from ai_bench.artifacts import ArtifactStore
from ai_bench.contracts import (
    Artifact,
    ArtifactEvidence,
    AttemptStatus,
    JsonValue,
    ProviderFailureEvidence,
    RunEvent,
    RunManifest,
    RunStatus,
    TaskAttempt,
    TaskResult,
    TaskStatus,
    TelemetryEvidence,
    utc_now,
)
from ai_bench.dispatch import indeterminate_evidence
from ai_bench.failures import ProviderFailure
from ai_bench.hashing import (
    canonical_json,
    normalize_media_type,
    sha256_digest,
    stable_task_id,
)
from ai_bench.lifecycle import (
    ExpiredRootError,
    LifecycleRoot,
    LifecycleService,
    RootUnavailableError,
    root_key,
)
from ai_bench.repositories import (
    AdmissionCapacityError,
    EligibleTasks,
    FinalizerFenceError,
    IdempotencyConflictError,
    LaunchReservation,
    Lease,
    LeaseLostError,
    RepositoryError,
    RunRecord,
    TaskRecord,
)
from ai_bench.runtime_policy import PublicExecutionPolicy
from ai_bench.storage_state import StateChange, StateConflictError, StateRow, StateStore

_TERMINAL = {
    RunStatus.COMPLETED.value,
    RunStatus.COMPLETED_WITH_FAILURES.value,
    RunStatus.FAILED.value,
}
_EVIDENCE_ORDER = {"raw": 1, "parsed": 2, "comparison": 3, "result": 4}


class AzureTableRepository:
    """Complete execution protocol; transport is injected for offline conformance.

    Immutable documents live in the artifact store. Table only holds small
    projections, ownership fences, and explicitly indexed work identities.
    """

    def __init__(
        self,
        state: StateStore,
        artifacts: ArtifactStore,
        lifecycle: LifecycleService,
        *,
        clock: Callable[[], datetime] = utc_now,
        public_policy: PublicExecutionPolicy | None = None,
    ) -> None:
        self.state = state
        self.artifacts = artifacts
        self.lifecycle = lifecycle
        self.clock = clock
        self.public_policy = public_policy
        self._claim_cursors: dict[str, str | None] = {}

    def _now(self) -> float:
        return self.clock().timestamp()

    def _required(self, key: str) -> StateRow:
        row = self.state.get(key)
        if row is None:
            raise RepositoryError(f"missing coordination record: {key}")
        return row

    @staticmethod
    def _change(row: StateRow, values: dict[str, JsonValue] | None = None) -> StateChange:
        return StateChange(row.key, row.values if values is None else values, row.etag)

    def _immutable(self, value: Any, run_id: str | None = None) -> str:
        if run_id is not None:
            self.lifecycle.attach(run_id, sha256_digest(value))
        artifact = self.artifacts.put_json(value)
        self.register_artifact(artifact)
        return artifact.id

    def _guard(self, run_id: str, *, executable: bool = False) -> StateChange:
        row = self._required(root_key(run_id))
        root = self.lifecycle.require_live(run_id, ready=False)
        if executable and root.archival:
            raise RepositoryError("archival evidence is never executable")
        return self._change(row)

    def _supervisor(self, generation: int | None = None) -> StateRow:
        row = self._required("supervisor")
        if float(str(row.values["until"])) <= self._now() or (
            generation is not None and int(str(row.values["generation"])) != generation
        ):
            raise LeaseLostError("supervisor generation is no longer current")
        return row

    def _event(
        self,
        run: StateRow,
        values: dict[str, JsonValue],
        event_type: str,
        payload: dict[str, JsonValue],
    ) -> StateChange:
        sequence = int(str(values["event_sequence"])) + 1
        values["event_sequence"] = sequence
        run_id = str(values["run_id"])
        event = RunEvent(
            sequence=sequence,
            run_id=run_id,
            event_type=event_type,
            occurred_at=self.clock(),
            payload=payload,
        )
        return StateChange(f"event:{run_id}:{sequence:020d}", event.model_dump(mode="json"))

    def reserve_launch(
        self, key_hash: str, payload_hash: str, proposed_run_id: str, *, expected_tasks: int = 0
    ) -> str:
        if expected_tasks < 0:
            raise ValueError("expected task count cannot be negative")
        key = f"launch:{key_hash}"
        values: dict[str, JsonValue] = {
            "payload_hash": payload_hash,
            "run_id": proposed_run_id,
            "reserved_tasks": expected_tasks,
            "capacity_transferred": False,
        }
        for _ in range(20):
            existing = self.state.get(key)
            if existing is not None:
                if existing.values["payload_hash"] != payload_hash:
                    raise IdempotencyConflictError("idempotency key has a different request")
                return str(existing.values["run_id"])
            if self.lifecycle.get(proposed_run_id) is None:
                self.lifecycle.create(proposed_run_id, "run")
            changes = [
                StateChange(key, values),
                StateChange(f"launch-owner:{proposed_run_id}:{key_hash}", {"key_hash": key_hash}),
                self._guard(proposed_run_id),
            ]
            if expected_tasks:
                limits = self.public_policy or PublicExecutionPolicy()
                admission = self.state.get("run-admission")
                active = 0 if admission is None else int(str(admission.values["runs"]))
                slots = 0 if admission is None else int(str(admission.values["tasks"]))
                if (
                    active >= limits.max_active_runs
                    or slots + expected_tasks > limits.max_outstanding_tasks
                ):
                    raise AdmissionCapacityError(
                        "outstanding run/task capacity is temporarily full"
                    )
                changes.append(
                    StateChange(
                        "run-admission",
                        {"runs": active + 1, "tasks": slots + expected_tasks},
                        None if admission is None else admission.etag,
                    )
                )
            try:
                self.state.transact(changes)
                return proposed_run_id
            except StateConflictError:
                continue
        raise StateConflictError("launch reservation contention")

    def get_launch_reservation(self, key_hash: str) -> LaunchReservation | None:
        row = self.state.get(f"launch:{key_hash}")
        return (
            None
            if row is None
            else LaunchReservation(
                payload_hash=str(row.values["payload_hash"]), run_id=str(row.values["run_id"])
            )
        )

    def launch_for_key(self, key_hash: str) -> RunRecord | None:
        reservation = self.get_launch_reservation(key_hash)
        return None if reservation is None else self.get_run(reservation.run_id)

    def upgrade_legacy_launch(
        self, key_hash: str, legacy_payload_hash: str, request_hash: str
    ) -> str:
        row = self._required(f"launch:{key_hash}")
        expected = sha256_digest(
            {"idempotency_key_hash": key_hash, "payload_hash": legacy_payload_hash}
        )
        if row.values["payload_hash"] == request_hash:
            return str(row.values["run_id"])
        if row.values["payload_hash"] != legacy_payload_hash or row.values["run_id"] != expected:
            raise IdempotencyConflictError("interrupted legacy reservation does not match")
        self.state.transact((self._change(row, {**row.values, "payload_hash": request_hash}),))
        return expected

    def put_benchmark(self, benchmark_id: str, definition_json: str) -> None:
        self.lifecycle.retain_system_object(sha256_digest({"definition": definition_json}))
        artifact_id = self._immutable({"definition": definition_json})
        key = f"benchmark:{sha256_digest(benchmark_id)}"
        for _ in range(20):
            row = self.state.get(key)
            if row is not None and row.values["artifact_id"] == artifact_id:
                return
            try:
                self.state.transact(
                    (
                        StateChange(
                            key,
                            {"artifact_id": artifact_id},
                            None if row is None else row.etag,
                        ),
                    )
                )
                return
            except StateConflictError:
                continue
        raise StateConflictError("benchmark publication contention")

    def get_benchmark(self, benchmark_id: str) -> str | None:
        import json

        row = self.state.get(f"benchmark:{sha256_digest(benchmark_id)}")
        if row is None:
            return None
        return str(
            json.loads(self.artifacts.get_bytes(str(row.values["artifact_id"])))["definition"]
        )

    def create_run(self, manifest: RunManifest, manifest_artifact_id: str) -> None:
        existing = self.state.get(f"run:{manifest.run_id}")
        if existing is not None:
            previous = RunManifest.model_validate_json(
                self.artifacts.get_bytes(str(existing.values["manifest_artifact_id"])),
                strict=False,
            )
            if (
                previous != manifest
                or existing.values["manifest_artifact_id"] != manifest_artifact_id
            ):
                raise IdempotencyConflictError("immutable run identity conflict")
            return
        limits = self.public_policy or PublicExecutionPolicy()
        if self.public_policy is not None and manifest.retry_limit > limits.max_attempts:
            raise ValueError("run exceeds the configured total-attempt limit")
        if self.lifecycle.get(manifest.run_id) is None:
            self.lifecycle.create(manifest.run_id, "run", accepted_at=manifest.created_at)
        self.lifecycle.attach(manifest.run_id, manifest_artifact_id)
        if manifest.sample_snapshot_artifact_id is not None:
            self.lifecycle.attach(manifest.run_id, manifest.sample_snapshot_artifact_id)
        for sample in manifest.samples:
            self.lifecycle.attach(manifest.run_id, sample.input_artifact_id)
            if sample.reference_artifact_id is not None:
                self.lifecycle.attach(manifest.run_id, sample.reference_artifact_id)
        history_key = (
            f"history:{99999999999 - manifest.created_at.timestamp():020.6f}:{manifest.run_id}"
        )
        values: dict[str, JsonValue] = {
            "run_id": manifest.run_id,
            "manifest_artifact_id": manifest_artifact_id,
            "status": RunStatus.CREATED.value,
            "report_artifact_id": None,
            "finalizer_token": 0,
            "finalizer_generation": 0,
            "runtime_error": None,
            "event_sequence": 1,
            "initialized": 0,
            "expected": len(manifest.sample_ids) * len(manifest.solution_ids),
            "remaining": len(manifest.sample_ids) * len(manifest.solution_ids),
            "ready": False,
            "networked": manifest.networked_execution,
            "history_key": history_key,
            "launch_key_hash": manifest.launch_idempotency_key_hash,
            "archival": False,
        }
        event = RunEvent(
            sequence=1,
            run_id=manifest.run_id,
            event_type="run.created",
            occurred_at=self.clock(),
        )
        admission = self.state.get("run-admission")
        active = 0 if admission is None else int(str(admission.values["runs"]))
        slots = 0 if admission is None else int(str(admission.values["tasks"]))
        expected = len(manifest.sample_ids) * len(manifest.solution_ids)
        reservation = (
            self.state.get(f"launch:{manifest.launch_idempotency_key_hash}")
            if manifest.launch_idempotency_key_hash is not None
            else None
        )
        reserved_tasks = (
            int(str(reservation.values.get("reserved_tasks", 0)))
            if reservation is not None and not reservation.values.get("capacity_transferred", False)
            else 0
        )
        if reserved_tasks and (
            reservation is None
            or reservation.values["run_id"] != manifest.run_id
            or reserved_tasks != expected
        ):
            raise IdempotencyConflictError("run does not match its reserved task capacity")
        if not reserved_tasks and (
            active >= limits.max_active_runs or slots + expected > limits.max_outstanding_tasks
        ):
            raise AdmissionCapacityError("outstanding run/task capacity is temporarily full")
        changes = [
            StateChange(f"run:{manifest.run_id}", values),
            StateChange(history_key, {"run_id": manifest.run_id}),
            StateChange(f"active:{manifest.run_id}", {"run_id": manifest.run_id}),
            StateChange(f"event:{manifest.run_id}:{1:020d}", event.model_dump(mode="json")),
            self._guard(manifest.run_id),
        ]
        if reserved_tasks:
            assert reservation is not None
            changes.append(
                self._change(
                    reservation,
                    {
                        **reservation.values,
                        "capacity_transferred": True,
                    },
                )
            )
        else:
            changes.append(
                StateChange(
                    "run-admission",
                    {"runs": active + 1, "tasks": slots + expected},
                    None if admission is None else admission.etag,
                )
            )
        try:
            self.state.transact(changes)
        except StateConflictError:
            if self.state.get(f"run:{manifest.run_id}") is None:
                raise
            self.create_run(manifest, manifest_artifact_id)

    def _run_record(self, row: StateRow) -> RunRecord:
        return RunRecord(
            manifest=RunManifest.model_validate_json(
                self.artifacts.get_bytes(str(row.values["manifest_artifact_id"])), strict=False
            ),
            manifest_artifact_id=str(row.values["manifest_artifact_id"]),
            status=RunStatus(str(row.values["status"])),
            report_artifact_id=(
                None
                if row.values["report_artifact_id"] is None
                else str(row.values["report_artifact_id"])
            ),
            finalizer_token=int(str(row.values["finalizer_token"])),
            runtime_error=(
                None if row.values["runtime_error"] is None else str(row.values["runtime_error"])
            ),
        )

    def get_run(self, run_id: str) -> RunRecord | None:
        row = self.state.get(f"run:{run_id}")
        if row is None:
            return None
        self.lifecycle.require_live(run_id, ready=False)
        return self._run_record(row)

    def _indexed_runs(self, prefix: str, limit: int) -> tuple[RunRecord, ...]:
        records = []
        for index in self.state.page(prefix, limit=limit).rows:
            try:
                record = self.get_run(str(index.values["run_id"]))
            except (ExpiredRootError, RootUnavailableError):
                continue
            if record is not None:
                records.append(record)
        return tuple(records)

    def list_runs(self, limit: int = 100) -> Sequence[RunRecord]:
        return self._indexed_runs("history:", max(1, min(limit, 500)))

    def unfinished_runs(self) -> Sequence[RunRecord]:
        records: list[RunRecord] = []
        cursor = None
        while True:
            page = self.state.page("active:", after=cursor, limit=100)
            for index in page.rows:
                try:
                    record = self.get_run(str(index.values["run_id"]))
                except (ExpiredRootError, RootUnavailableError):
                    continue
                if record is not None:
                    records.append(record)
            cursor = page.cursor
            if cursor is None:
                return tuple(records)

    def set_runtime_error(self, run_id: str, message: str | None) -> None:
        for _ in range(20):
            row = self._required(f"run:{run_id}")
            if row.values["runtime_error"] == message:
                return
            values = {**row.values, "runtime_error": message}
            event = self._event(
                row, values, "run.blocked" if message else "run.resumed", {"message": message}
            )
            try:
                self.state.transact((self._change(row, values), event, self._guard(run_id)))
                return
            except StateConflictError:
                continue
        raise StateConflictError("runtime status contention")

    @staticmethod
    def _due_key(task_id: str, due: float) -> str:
        return f"due:{due:020.6f}:{task_id}"

    def reconcile_tasks(self, manifest: RunManifest) -> int:
        run_id = manifest.run_id
        total_created = 0
        expected_ids = [
            (stable_task_id(run_id, sample_id, solution_id), sample_id, solution_id)
            for sample_id in manifest.sample_ids
            for solution_id in manifest.solution_ids
        ]
        for start in range(0, len(expected_ids), 24):
            batch = expected_ids[start : start + 24]
            for _ in range(20):
                run = self._required(f"run:{run_id}")
                if run.values["archival"] or run.values["status"] in _TERMINAL:
                    return 0
                values = dict(run.values)
                changes: list[StateChange] = []
                created = 0
                for task_id, sample_id, solution_id in batch:
                    existing = self.state.get(f"task:{task_id}")
                    if existing is not None:
                        if (
                            existing.values["run_id"] != run_id
                            or existing.values["sample_id"] != sample_id
                            or existing.values["solution_id"] != solution_id
                        ):
                            raise RepositoryError("task identity conflicts with immutable matrix")
                        continue
                    due = self._due_key(task_id, 0)
                    task: dict[str, JsonValue] = {
                        "id": task_id,
                        "run_id": run_id,
                        "sample_id": sample_id,
                        "solution_id": solution_id,
                        "status": TaskStatus.PENDING.value,
                        "attempts": 0,
                        "max_attempts": manifest.retry_limit,
                        "lease_owner": None,
                        "lease_token": 0,
                        "lease_until": 0,
                        "supervisor_generation": 0,
                        "result_id": None,
                        "throttle_scope": manifest.solution_descriptors[solution_id].throttle_scope,
                        "next_available": 0,
                        "waiting_since": self._now(),
                        "due_key": due,
                        "lease_key": None,
                    }
                    changes.extend(
                        (
                            StateChange(f"task:{task_id}", task),
                            StateChange(f"run-task:{run_id}:{task_id}", {"task_id": task_id}),
                            StateChange(due, {"task_id": task_id, "run_id": run_id}),
                            self._event(
                                run,
                                values,
                                "task.enqueued",
                                {
                                    "task_id": task_id,
                                    "sample_id": sample_id,
                                    "solution_id": solution_id,
                                },
                            ),
                        )
                    )
                    created += 1
                if not changes:
                    break
                values["initialized"] = int(str(values["initialized"])) + created
                changes.extend((self._change(run, values), self._guard(run_id, executable=True)))
                try:
                    self.state.transact(changes)
                    total_created += created
                    break
                except StateConflictError:
                    continue
            else:
                raise StateConflictError("task matrix initialization contention")
        for _ in range(20):
            run = self._required(f"run:{run_id}")
            if run.values["ready"]:
                return total_created
            if int(str(run.values["initialized"])) != len(expected_ids):
                raise RepositoryError("task matrix is not complete")
            root_row = self._required(root_key(run_id))
            root = self.lifecycle.require_live(run_id, ready=False)
            generation, generation_change = self.lifecycle._root_generation()
            ready = root.model_copy(update={"state": "ready", "generation": generation})
            try:
                self.state.transact(
                    (
                        self._change(
                            run,
                            {
                                **run.values,
                                "ready": True,
                                "status": RunStatus.RUNNING.value,
                            },
                        ),
                        self._change(root_row, ready.model_dump(mode="json")),
                        generation_change,
                    )
                )
                return total_created
            except StateConflictError:
                continue
        raise StateConflictError("ready publication contention")

    def _task_record(self, row: StateRow) -> TaskRecord:
        values = row.values
        cooldown = (
            self.state.get(f"cooldown:{values['throttle_scope']}")
            if values["throttle_scope"] is not None
            else None
        )
        return TaskRecord(
            id=str(values["id"]),
            run_id=str(values["run_id"]),
            sample_id=str(values["sample_id"]),
            solution_id=str(values["solution_id"]),
            status=TaskStatus(str(values["status"])),
            attempts=int(str(values["attempts"])),
            max_attempts=int(str(values["max_attempts"])),
            lease_owner=None if values["lease_owner"] is None else str(values["lease_owner"]),
            lease_token=int(str(values["lease_token"])),
            result=(
                None
                if values["result_id"] is None
                else TaskResult.model_validate_json(
                    self.artifacts.get_bytes(str(values["result_id"])), strict=False
                )
            ),
            throttle_scope=(
                None if values["throttle_scope"] is None else str(values["throttle_scope"])
            ),
            next_available_at=(
                None
                if not values["next_available"]
                else datetime.fromtimestamp(
                    float(str(values["next_available"])), self.clock().tzinfo
                )
            ),
            cooldown_until=(
                None
                if cooldown is None
                else datetime.fromtimestamp(
                    float(str(cooldown.values["until"])), self.clock().tzinfo
                )
            ),
        )

    def list_tasks(self, run_id: str) -> Sequence[TaskRecord]:
        self.lifecycle.require_live(run_id, ready=False)
        cursor = None
        tasks: list[TaskRecord] = []
        while True:
            page = self.state.page(f"run-task:{run_id}:", after=cursor)
            tasks.extend(
                self._task_record(self._required(f"task:{row.values['task_id']}"))
                for row in page.rows
            )
            cursor = page.cursor
            if cursor is None:
                return tuple(sorted(tasks, key=lambda task: (task.sample_id, task.solution_id)))

    def get_task(self, task_id: str) -> TaskRecord | None:
        row = self.state.get(f"task:{task_id}")
        if row is None:
            return None
        root = self.lifecycle.require_live(str(row.values["run_id"]), ready=False)
        if root.archival and root.state != "ready":
            raise RootUnavailableError(root.id)
        return self._task_record(row)

    def list_attempts(self, task_id: str) -> Sequence[TaskAttempt]:
        task = self.get_task(task_id)
        if task is None:
            return ()
        attempts: list[TaskAttempt] = []
        cursor = None
        while True:
            page = self.state.page(f"attempt:{task_id}:", after=cursor)
            attempts.extend(self._attempt(row) for row in page.rows)
            cursor = page.cursor
            if cursor is None:
                return tuple(attempts)

    def _attempt(self, row: StateRow) -> TaskAttempt:
        return TaskAttempt.model_validate_json(
            self.artifacts.get_bytes(str(row.values["artifact_id"])), strict=False
        )

    def acquire_supervisor(self, owner: str, lease_seconds: int = 30) -> bool:
        if not owner or len(owner) > 200 or not 1 <= lease_seconds <= 3600:
            raise ValueError("invalid supervisor lease")
        for _ in range(20):
            row = self.state.get("supervisor")
            now = self._now()
            if (
                row is not None
                and row.values["owner"] != owner
                and float(str(row.values["until"])) > now
            ):
                return False
            generation = 1 if row is None else int(str(row.values["generation"]))
            if row is not None and (
                row.values["owner"] != owner or float(str(row.values["until"])) <= now
            ):
                generation += 1
            try:
                self.state.transact(
                    (
                        StateChange(
                            "supervisor",
                            {
                                "owner": owner,
                                "until": now + lease_seconds,
                                "generation": generation,
                            },
                            None if row is None else row.etag,
                        ),
                    )
                )
                return True
            except StateConflictError:
                continue
        return False

    def renew_supervisor(
        self, owner: str, *, generation: int | None = None, lease_seconds: int = 30
    ) -> int | None:
        if not owner or len(owner) > 200 or not 1 <= lease_seconds <= 3600:
            raise ValueError("invalid supervisor renewal")
        for _ in range(20):
            row = self.state.get("supervisor")
            now = self._now()
            if (
                row is None
                or row.values["owner"] != owner
                or float(str(row.values["until"])) <= now
                or (generation is not None and int(str(row.values["generation"])) != generation)
            ):
                return None
            try:
                self.state.transact(
                    (self._change(row, {**row.values, "until": now + lease_seconds}),)
                )
                return int(str(row.values["generation"]))
            except StateConflictError:
                continue
        raise StateConflictError("supervisor renewal contention")

    def release_supervisor(self, owner: str, *, generation: int | None = None) -> None:
        row = self.state.get("supervisor")
        if (
            row is not None
            and row.values["owner"] == owner
            and (generation is None or int(str(row.values["generation"])) == generation)
        ):
            with suppress(StateConflictError):
                self.state.transact((self._change(row, {**row.values, "until": 0}),))

    def claim_task(
        self,
        owner: str,
        lease_seconds: int = 60,
        run_id: str | None = None,
        *,
        allow_networked: bool = True,
        eligible_tasks: EligibleTasks | None = None,
    ) -> Lease | None:
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("invalid task lease duration")
        if not self.acquire_supervisor(owner):
            return None
        prefix = "due:" if run_id is None else f"run-task:{run_id}:"
        for candidate in self._claim_candidates(prefix):
            task = self._required(f"task:{candidate.values['task_id']}")
            if (
                eligible_tasks is not None
                and (str(task.values["run_id"]), str(task.values["solution_id"]))
                not in eligible_tasks
            ):
                continue
            if task.values["status"] != TaskStatus.PENDING.value:
                continue
            due = candidate if run_id is None else self._required(str(task.values["due_key"]))
            if float(str(task.values["next_available"])) > self._now():
                if run_id is None:
                    self._claim_cursors[prefix] = None
                    break
                continue
            try:
                guard = self._guard(str(task.values["run_id"]), executable=True)
            except (ExpiredRootError, RootUnavailableError):
                continue
            run = self._required(f"run:{task.values['run_id']}")
            if (
                not run.values["ready"]
                or run.values["status"] in _TERMINAL
                or run.values["runtime_error"] is not None
                or (run.values["networked"] and not allow_networked)
                or task.values["status"] != TaskStatus.PENDING.value
            ):
                continue
            scope = task.values["throttle_scope"]
            cooldown = None if scope is None else self.state.get(f"cooldown:{scope}")
            if cooldown is not None and float(str(cooldown.values["until"])) > self._now():
                continue
            supervisor = self._supervisor()
            if supervisor.values["owner"] != owner:
                return None
            token = int(str(task.values["lease_token"])) + 1
            resume = bool(task.values.get("resume", False))
            number = int(str(task.values["attempts"])) + (0 if resume else 1)
            now = self._now()
            waiting_since = float(str(task.values["waiting_since"]))
            cooldown_ms = self._cooldown_wait(
                None if scope is None else str(scope), waiting_since, now
            )
            lease_key = f"lease:{now + lease_seconds:020.6f}:{task.values['id']}"
            attempt = TaskAttempt(
                task_id=str(task.values["id"]),
                attempt_number=number,
                status=AttemptStatus.RUNNING,
                lease_token=token,
                started_at=self.clock(),
                queue_wait_ms=Decimal(str(max(0, (now - waiting_since) * 1000 - cooldown_ms))),
                cooldown_wait_ms=Decimal(str(cooldown_ms)),
            )
            previous_attempt = (
                self._required(f"attempt:{task.values['id']}:{number:010d}") if resume else None
            )
            if previous_attempt is not None:
                attempt = self._attempt(previous_attempt).model_copy(update={"lease_token": token})
            attempt_id = self._immutable(attempt, str(task.values["run_id"]))
            guard = self._guard(str(task.values["run_id"]), executable=True)
            supervisor = self._supervisor(int(str(supervisor.values["generation"])))
            now = self._now()
            lease_key = f"lease:{now + lease_seconds:020.6f}:{task.values['id']}"
            values: dict[str, JsonValue] = {
                **task.values,
                "status": TaskStatus.LEASED.value,
                "attempts": number,
                "lease_owner": owner,
                "lease_token": token,
                "lease_until": now + lease_seconds,
                "supervisor_generation": supervisor.values["generation"],
                "lease_key": lease_key,
                "due_key": None,
                "resume": False,
            }
            run_values = dict(run.values)
            changes = [
                self._change(task, values),
                StateChange(due.key, None, due.etag),
                StateChange(
                    lease_key, {"task_id": task.values["id"], "run_id": task.values["run_id"]}
                ),
                StateChange(
                    f"attempt:{task.values['id']}:{number:010d}",
                    (
                        {
                            "artifact_id": attempt_id,
                            "retry_at": None,
                            "sent": False,
                            "checkpoint": None,
                        }
                        if previous_attempt is None
                        else {**previous_attempt.values, "artifact_id": attempt_id}
                    ),
                    None if previous_attempt is None else previous_attempt.etag,
                ),
                self._event(
                    run,
                    run_values,
                    "task.leased",
                    {"task_id": task.values["id"], "attempt": number, "lease_token": token},
                ),
                self._change(run, run_values),
                self._change(supervisor),
                guard,
            ]
            if cooldown is not None:
                changes.append(self._change(cooldown))
            try:
                self.state.transact(changes)
                root = self.lifecycle.require_live(str(task.values["run_id"]))
                return Lease(
                    task=self._task_record(StateRow(task.key, values, task.etag)),
                    owner=owner,
                    token=token,
                    attempt_number=number,
                    supervisor_generation=int(str(supervisor.values["generation"])),
                    retention_generation=root.generation,
                    retention_incarnation=root.incarnation,
                )
            except StateConflictError:
                continue
        return None

    def _claim_candidates(self, prefix: str) -> Iterator[StateRow]:
        start = self._claim_cursors.get(prefix)
        cursor = start
        wrapped = False
        while True:
            page = self.state.page(prefix, after=cursor, limit=100)
            for row in page.rows:
                if wrapped and start is not None and row.key > start:
                    return
                self._claim_cursors[prefix] = row.key
                yield row
            cursor = page.cursor
            if cursor is None:
                if start is None or wrapped:
                    self._claim_cursors[prefix] = None
                    return
                wrapped, cursor = True, None

    def _lease_rows(self, lease: Lease) -> tuple[StateRow, StateRow, StateRow]:
        task = self._required(f"task:{lease.task.id}")
        if (
            task.values["status"] != TaskStatus.LEASED.value
            or task.values["lease_owner"] != lease.owner
            or task.values["lease_token"] != lease.token
            or task.values["attempts"] != lease.attempt_number
            or float(str(task.values["lease_until"])) <= self._now()
            or task.values["run_id"] != lease.task.run_id
            or task.values["sample_id"] != lease.task.sample_id
            or task.values["solution_id"] != lease.task.solution_id
        ):
            raise LeaseLostError(lease.task.id)
        root = self.lifecycle.require_live(lease.task.run_id)
        if (
            root.archival
            or root.generation != lease.retention_generation
            or root.incarnation != lease.retention_incarnation
        ):
            raise LeaseLostError("retention generation changed")
        attempt = self._required(f"attempt:{lease.task.id}:{lease.attempt_number:010d}")
        supervisor = self._supervisor(lease.supervisor_generation)
        return task, attempt, supervisor

    def begin_dispatch(self, lease: Lease) -> None:
        for _ in range(20):
            try:
                self._begin_dispatch(lease)
                return
            except StateConflictError:
                continue
        raise StateConflictError("request-start contention")

    def _begin_dispatch(self, lease: Lease) -> None:
        from ai_bench.receipts import UncertainDispatchError

        task, attempt, supervisor = self._lease_rows(lease)
        if attempt.values["sent"]:
            raise UncertainDispatchError(
                "a request-start checkpoint already exists for this attempt"
            )
        run = self._required(f"run:{lease.task.run_id}")
        values = dict(run.values)
        event = self._event(
            run,
            values,
            "attempt.request_started",
            {
                "task_id": lease.task.id,
                "attempt": lease.attempt_number,
                "lease_token": lease.token,
                "supervisor_generation": lease.supervisor_generation,
                "submission": "unconfirmed",
            },
        )
        self.state.transact(
            (
                self._change(attempt, {**attempt.values, "sent": True}),
                self._change(task),
                self._change(supervisor),
                self._guard(lease.task.run_id, executable=True),
                self._change(run, values),
                event,
            )
        )

    def response_receipt(self, lease: Lease) -> str | None:
        _, attempt, _ = self._lease_rows(lease)
        value = attempt.values["checkpoint"]
        return None if value is None else str(value)

    def record_response_receipt(self, lease: Lease, artifact_id: str) -> None:
        for _ in range(20):
            task, attempt, supervisor = self._lease_rows(lease)
            if attempt.values["checkpoint"] is not None:
                if attempt.values["checkpoint"] != artifact_id:
                    raise RepositoryError("response receipts are immutable")
                return
            try:
                self.state.transact(
                    (
                        self._change(attempt, {**attempt.values, "checkpoint": artifact_id}),
                        self._change(task),
                        self._change(supervisor),
                        self._guard(lease.task.run_id, executable=True),
                    )
                )
                return
            except StateConflictError:
                continue
        raise StateConflictError("response checkpoint contention")

    def renew_lease(self, lease: Lease, lease_seconds: int = 60) -> None:
        for _ in range(20):
            task, _, supervisor = self._lease_rows(lease)
            old_index = self._required(str(task.values["lease_key"]))
            until = self._now() + max(1, min(lease_seconds, 3600))
            key = f"lease:{until:020.6f}:{lease.task.id}"
            values = {**task.values, "lease_until": until, "lease_key": key}
            changes = [
                self._change(task, values),
                self._change(supervisor),
                self._guard(lease.task.run_id, executable=True),
            ]
            if key != old_index.key:
                changes.extend(
                    (
                        StateChange(old_index.key, None, old_index.etag),
                        StateChange(key, old_index.values),
                    )
                )
            try:
                self.state.transact(changes)
                return
            except StateConflictError:
                continue
        raise LeaseLostError("lease renewal contention")

    def register_artifact(self, artifact: Artifact) -> None:
        key = f"descriptor:{artifact.id}:{sha256_digest(artifact)}"
        values: dict[str, JsonValue] = {
            "descriptor_key": sha256_digest(artifact),
            "media_type": normalize_media_type(artifact.media_type),
            "modality": artifact.modality.value,
        }
        row = self.state.get(key)
        if row is not None:
            if row.values != values:
                raise RepositoryError("immutable descriptor conflict")
            return
        self.artifacts.register_descriptor(artifact)
        try:
            self.state.transact((StateChange(key, values),))
        except StateConflictError:
            row = self._required(key)
            if row.values != values:
                raise RepositoryError("immutable descriptor conflict") from None

    def get_artifact(self, artifact_id: str, media_type: str | None = None) -> Artifact | None:
        cursor = None
        keys = set()
        while True:
            page = self.state.page(f"descriptor:{artifact_id}:", after=cursor)
            for row in page.rows:
                if media_type is None or normalize_media_type(
                    str(row.values["media_type"])
                ) == normalize_media_type(media_type):
                    keys.add(str(row.values["descriptor_key"]))
            cursor = page.cursor
            if cursor is None:
                break
        if not keys:
            return None
        candidates = [
            artifact
            for artifact in self.artifacts.descriptors(artifact_id)
            if sha256_digest(artifact) in keys
        ]
        return min(
            candidates,
            key=lambda item: (
                normalize_media_type(item.media_type),
                item.modality.value,
                canonical_json(item),
            ),
            default=None,
        )

    def _update_attempt(
        self,
        lease: Lease,
        update: Callable[[TaskAttempt], TaskAttempt],
        *,
        failure: ProviderFailureEvidence | None = None,
    ) -> None:
        cached_attempt: TaskAttempt | None = None
        artifact_id: str | None = None
        previous_id: str | None = None
        previous: TaskAttempt | None = None
        observed_at = self._now()
        for _ in range(20):
            task, row, supervisor = self._lease_rows(lease)
            if previous_id != row.values["artifact_id"]:
                previous = self._attempt(row)
                previous_id = str(row.values["artifact_id"])
            assert previous is not None
            attempt = update(previous)
            if attempt == previous:
                return
            if cached_attempt != attempt:
                artifact_id = self._immutable(attempt, lease.task.run_id)
                cached_attempt = attempt
            assert artifact_id is not None
            fresh_task, fresh_row, _ = self._lease_rows(lease)
            if fresh_row.etag != row.etag:
                continue
            task, row = fresh_task, fresh_row
            values = {**row.values, "artifact_id": artifact_id}
            changes: list[StateChange] = []
            if failure is not None:
                delay = float(failure.retry_delay.scheduled_seconds) if failure.retry_delay else 0
                values["retry_at"] = observed_at + delay
                scope = task.values["throttle_scope"]
                if failure.retryable and scope is not None and delay > 0:
                    cooldown_key = f"cooldown:{scope}"
                    cooldown = self.state.get(cooldown_key)
                    until = observed_at + delay
                    if cooldown is not None:
                        until = max(until, float(str(cooldown.values["until"])))
                    changes.append(
                        StateChange(
                            cooldown_key,
                            {"until": until},
                            None if cooldown is None else cooldown.etag,
                        )
                    )
                    started = observed_at
                    interval_id = sha256_digest(
                        {
                            "scope": scope,
                            "started": started,
                            "until": observed_at + delay,
                        }
                    )
                    changes.append(
                        StateChange(
                            f"cooldown-interval:{scope}:{started:020.6f}:{interval_id}",
                            {"started": started, "until": observed_at + delay},
                        )
                    )
            guard = self._guard(lease.task.run_id, executable=True)
            supervisor = self._supervisor(lease.supervisor_generation)
            changes.extend(
                (
                    self._change(row, values),
                    self._change(task),
                    self._change(supervisor),
                    guard,
                )
            )
            try:
                self.state.transact(changes)
                return
            except StateConflictError:
                continue
        raise StateConflictError("attempt evidence contention")

    def _has_registered_descriptor(self, artifact_id: str, media_type: str) -> bool:
        cursor = None
        while True:
            page = self.state.page(f"descriptor:{artifact_id}:", after=cursor)
            for row in page.rows:
                if normalize_media_type(str(row.values["media_type"])) != normalize_media_type(
                    media_type
                ):
                    continue
                artifact = self.artifacts.get_descriptor(
                    artifact_id, str(row.values["descriptor_key"])
                )
                if (
                    normalize_media_type(artifact.media_type) != normalize_media_type(media_type)
                    or artifact.modality.value != row.values["modality"]
                ):
                    raise RepositoryError("registered descriptor does not match its index")
                return True
            cursor = page.cursor
            if cursor is None:
                return False

    def _cooldown_wait(self, scope: str | None, start: float, finish: float) -> float:
        if scope is None:
            return 0.0
        prefix = f"cooldown-interval:{scope}:"
        after: str | None = f"{prefix}{max(0, start - 86400):020.6f}:"
        cursor = start
        total = 0.0
        while True:
            page = self.state.page(prefix, after=after, limit=100)
            for interval in page.rows:
                left = max(cursor, float(str(interval.values["started"])))
                right = min(finish, float(str(interval.values["until"])))
                if float(str(interval.values["started"])) > finish:
                    return total * 1000
                if right > left:
                    total += right - left
                    cursor = right
            after = page.cursor
            if after is None:
                return total * 1000

    def record_attempt_artifact(self, lease: Lease, evidence: ArtifactEvidence) -> None:
        if evidence.attempt_number != lease.attempt_number or evidence.role not in _EVIDENCE_ORDER:
            raise ValueError("attempt artifact does not match the lease")
        if not self._has_registered_descriptor(evidence.artifact_id, evidence.media_type):
            raise RepositoryError("artifact descriptor must be persisted before evidence")
        self.lifecycle.attach(lease.task.run_id, evidence.artifact_id)

        def update(attempt: TaskAttempt) -> TaskAttempt:
            existing = next((item for item in attempt.evidence if item.role == evidence.role), None)
            if existing is not None:
                if existing != evidence:
                    raise RepositoryError("attempt evidence is immutable")
                return attempt
            previous = max((_EVIDENCE_ORDER[item.role] for item in attempt.evidence), default=0)
            if _EVIDENCE_ORDER[evidence.role] <= previous:
                raise RepositoryError("attempt evidence must be persisted in evidence order")
            return attempt.model_copy(update={"evidence": (*attempt.evidence, evidence)})

        self._update_attempt(lease, update)

    def record_attempt_telemetry(self, lease: Lease, telemetry: TelemetryEvidence) -> None:
        def update(attempt: TaskAttempt) -> TaskAttempt:
            if attempt.telemetry is not None and attempt.telemetry != telemetry:
                raise RepositoryError("attempt telemetry is immutable")
            return attempt.model_copy(update={"telemetry": telemetry})

        self._update_attempt(lease, update)

    def record_attempt_failure(self, lease: Lease, failure: ProviderFailureEvidence) -> None:
        finished_at = self.clock()

        def update(attempt: TaskAttempt) -> TaskAttempt:
            if attempt.failure is not None and attempt.failure != failure:
                raise RepositoryError("attempt failure evidence is immutable")
            if failure.raw_artifact_id is not None and not any(
                item.role == "raw" and item.artifact_id == failure.raw_artifact_id
                for item in attempt.evidence
            ):
                raise RepositoryError("raw failure evidence must precede failure classification")
            return attempt.model_copy(
                update={"failure": failure, "finished_at": attempt.finished_at or finished_at}
            )

        self._update_attempt(lease, update, failure=failure)

    def complete_attempt(self, lease: Lease, result: TaskResult) -> None:
        if (
            result.task_id != lease.task.id
            or result.status is not TaskStatus.SUCCEEDED
            or result.sample_id != lease.task.sample_id
            or result.solution_id != lease.task.solution_id
            or result.attempts != lease.attempt_number
        ):
            raise ValueError("successful task result does not match lease")
        self._finish_attempt(lease, result=result)

    def fail_attempt(self, lease: Lease, error: Exception, retry_delay_seconds: int = 0) -> bool:
        return self._finish_attempt(lease, error=error, delay=retry_delay_seconds)

    def _finish_attempt(
        self,
        lease: Lease,
        *,
        result: TaskResult | None = None,
        error: Exception | None = None,
        delay: int = 0,
    ) -> bool:
        finished_at = self.clock()
        cached_attempt: TaskAttempt | None = None
        attempt_id: str | None = None
        result_id: str | None = None
        requested_result = result
        previous_id: str | None = None
        attempt: TaskAttempt | None = None
        for _ in range(20):
            result = requested_result
            # Reconcile a lost successful response without adding another event.
            task = self._required(f"task:{lease.task.id}")
            if task.values["status"] != TaskStatus.LEASED.value:
                attempts = self.list_attempts(lease.task.id)
                if (
                    attempts
                    and attempts[-1].attempt_number == lease.attempt_number
                    and attempts[-1].lease_token == lease.token
                    and attempts[-1].status is not AttemptStatus.RUNNING
                ):
                    prior = attempts[-1]
                    if result is not None and prior.status is AttemptStatus.SUCCEEDED:
                        stored = self._task_record(task).result
                        if stored != result:
                            raise RepositoryError("completed task result is immutable")
                        return False
                    if (
                        error is not None
                        and prior.status in {AttemptStatus.FAILED, AttemptStatus.RETRYABLE}
                        and prior.error_type == type(error).__name__
                        and prior.error_message == str(error)[:2000]
                    ):
                        return task.values["status"] == TaskStatus.POISONED.value
                raise LeaseLostError(lease.task.id)
            task, attempt_row, supervisor = self._lease_rows(lease)
            if previous_id != attempt_row.values["artifact_id"]:
                attempt = self._attempt(attempt_row)
                previous_id = str(attempt_row.values["artifact_id"])
            assert attempt is not None
            failure = error.evidence if isinstance(error, ProviderFailure) else None
            poison = False
            due = self._now() + max(0, min(delay, 86400))
            if result is not None:
                if not {"raw", "parsed", "result"}.issubset(
                    {item.role for item in attempt.evidence}
                ):
                    raise RepositoryError(
                        "raw, parsed, and result evidence must precede completion"
                    )
                status = TaskStatus.SUCCEEDED
                attempt_status = AttemptStatus.SUCCEEDED
            else:
                if failure is not None:
                    if attempt.failure != failure or attempt_row.values["retry_at"] is None:
                        raise RepositoryError(
                            "typed failure evidence must precede retry scheduling"
                        )
                    due = float(str(attempt_row.values["retry_at"]))
                poison = lease.attempt_number >= lease.task.max_attempts or (
                    failure is not None and not failure.retryable
                )
                status = TaskStatus.POISONED if poison else TaskStatus.PENDING
                attempt_status = AttemptStatus.FAILED if poison else AttemptStatus.RETRYABLE
                if poison:
                    result = TaskResult(
                        task_id=lease.task.id,
                        sample_id=lease.task.sample_id,
                        solution_id=lease.task.solution_id,
                        status=status,
                        attempts=lease.attempt_number,
                        error=f"{type(error).__name__}: {str(error)[:2000]}",
                    )
            finished = attempt.finished_at or finished_at
            updated_attempt = attempt.model_copy(
                update={
                    "status": attempt_status,
                    "finished_at": finished,
                    "duration_ms": Decimal(
                        str(max(0, (finished - attempt.started_at).total_seconds()) * 1000)
                    ),
                    "error_type": None if error is None else type(error).__name__,
                    "error_message": None if error is None else str(error)[:2000],
                }
            )
            if cached_attempt != updated_attempt:
                attempt_id = self._immutable(updated_attempt, lease.task.run_id)
                cached_attempt = updated_attempt
            if result is not None and result_id is None:
                result_id = self._immutable(result, lease.task.run_id)
            assert attempt_id is not None
            fresh_task, fresh_attempt, _ = self._lease_rows(lease)
            if fresh_attempt.etag != attempt_row.etag:
                continue
            task, attempt_row = fresh_task, fresh_attempt
            due_key = self._due_key(lease.task.id, due) if status is TaskStatus.PENDING else None
            values = {
                **task.values,
                "status": status.value,
                "lease_owner": None,
                "lease_until": 0,
                "lease_key": None,
                "due_key": due_key,
                "next_available": due,
                "waiting_since": self._now(),
                "result_id": result_id,
            }
            run = self._required(f"run:{lease.task.run_id}")
            run_values = dict(run.values)
            if status is not TaskStatus.PENDING:
                run_values["remaining"] = int(str(run_values["remaining"])) - 1
            lease_index = self._required(str(task.values["lease_key"]))
            event_type = (
                "task.succeeded"
                if status is TaskStatus.SUCCEEDED
                else "task.poisoned"
                if poison
                else "task.retry_scheduled"
            )
            guard = self._guard(lease.task.run_id, executable=True)
            supervisor = self._supervisor(lease.supervisor_generation)
            changes = [
                self._change(task, values),
                self._change(attempt_row, {**attempt_row.values, "artifact_id": attempt_id}),
                StateChange(lease_index.key, None, lease_index.etag),
                self._event(
                    run,
                    run_values,
                    event_type,
                    {
                        "task_id": lease.task.id,
                        "attempt": lease.attempt_number,
                        "delay_seconds": max(0, due - self._now()),
                    },
                ),
                self._change(run, run_values),
                self._change(supervisor),
                guard,
            ]
            if due_key is not None:
                changes.append(
                    StateChange(due_key, {"task_id": lease.task.id, "run_id": lease.task.run_id})
                )
            try:
                self.state.transact(changes)
                return poison
            except StateConflictError:
                continue
        raise StateConflictError("attempt completion contention")

    def release_unstarted(self, lease: Lease) -> None:
        for _ in range(20):
            try:
                self._release_unstarted(lease)
                return
            except StateConflictError:
                continue
        raise StateConflictError("unstarted release contention")

    def _release_unstarted(self, lease: Lease) -> None:
        task, attempt_row, supervisor = self._lease_rows(lease)
        attempt = self._attempt(attempt_row)
        if (
            self.artifacts.get_receipt(
                lease.task.run_id,
                lease.task.id,
                lease.attempt_number,
                incarnation=lease.retention_incarnation,
            )
            is not None
        ):
            raise RepositoryError("cannot release an attempt with a durable response checkpoint")
        if (
            attempt.telemetry
            or attempt.failure
            or attempt.evidence
            or attempt_row.values["sent"]
            or attempt_row.values["checkpoint"]
        ):
            raise RepositoryError("cannot release an attempt after provider execution")
        due_key = self._due_key(lease.task.id, self._now())
        lease_index = self._required(str(task.values["lease_key"]))
        self.state.transact(
            (
                self._change(
                    task,
                    {
                        **task.values,
                        "status": TaskStatus.PENDING.value,
                        "attempts": lease.attempt_number - 1,
                        "lease_owner": None,
                        "lease_until": 0,
                        "lease_key": None,
                        "due_key": due_key,
                    },
                ),
                StateChange(attempt_row.key, None, attempt_row.etag),
                StateChange(lease_index.key, None, lease_index.etag),
                StateChange(due_key, {"task_id": lease.task.id, "run_id": lease.task.run_id}),
                self._change(supervisor),
                self._guard(lease.task.run_id, executable=True),
            )
        )

    def recover_expired_leases(
        self, *, active_task_ids: frozenset[str] = frozenset()
    ) -> tuple[int, int]:
        recovered = poisoned = 0
        for index in self.state.page("lease:", limit=100).rows:
            if str(index.values["task_id"]) in active_task_ids:
                continue
            task = self._required(f"task:{index.values['task_id']}")
            if float(str(task.values["lease_until"])) > self._now():
                break
            run_id = str(task.values["run_id"])
            try:
                root = self.lifecycle.require_live(run_id)
            except (ExpiredRootError, RootUnavailableError):
                continue
            attempt_row = self._required(
                f"attempt:{task.values['id']}:{int(str(task.values['attempts'])):010d}"
            )
            attempt = self._attempt(attempt_row)
            receipt_id = attempt_row.values["checkpoint"]
            if receipt_id is None:
                received = self.artifacts.get_receipt(
                    run_id,
                    str(task.values["id"]),
                    int(str(task.values["attempts"])),
                    incarnation=root.incarnation,
                )
                if received is not None:
                    from ai_bench.receipts import materialize_receipt

                    receipt_id = materialize_receipt(
                        self.artifacts,
                        self.lifecycle,
                        self.register_artifact,
                        run_id,
                        str(task.values["id"]),
                        int(str(task.values["attempts"])),
                        received,
                    )
            failure = attempt.failure
            indeterminate = bool(attempt_row.values["sent"]) and receipt_id is None
            retry_at = float(str(attempt_row.values["retry_at"] or 0))
            if indeterminate and failure is None:
                record = self.get_run(run_id)
                assert record is not None
                failure, telemetry = indeterminate_evidence(
                    record.manifest,
                    str(task.values["solution_id"]),
                    int(str(task.values["attempts"])),
                    attempt.started_at,
                    self.clock(),
                )
                attempt = attempt.model_copy(update={"failure": failure, "telemetry": telemetry})
                retry_at = self._now() + (
                    0
                    if failure.retry_delay is None
                    else float(failure.retry_delay.scheduled_seconds)
                )
            poison = int(str(task.values["attempts"])) >= int(str(task.values["max_attempts"])) or (
                failure is not None and not failure.retryable
            )
            if receipt_id is not None and failure is None:
                resume_key = self._due_key(str(task.values["id"]), -1)
                try:
                    self.state.transact(
                        (
                            self._change(
                                task,
                                {
                                    **task.values,
                                    "status": TaskStatus.PENDING.value,
                                    "lease_owner": None,
                                    "lease_until": 0,
                                    "lease_key": None,
                                    "due_key": resume_key,
                                    "resume": True,
                                },
                            ),
                            self._change(
                                attempt_row, {**attempt_row.values, "checkpoint": receipt_id}
                            ),
                            StateChange(index.key, None, index.etag),
                            StateChange(
                                resume_key, {"task_id": task.values["id"], "run_id": run_id}
                            ),
                            self._guard(run_id, executable=True),
                        )
                    )
                except StateConflictError:
                    continue
                recovered += 1
                continue
            due = max(self._now(), retry_at)
            status = TaskStatus.POISONED if poison else TaskStatus.PENDING
            message = (
                failure.message if failure else "worker lease expired before a recorded response"
            )
            finished = attempt.finished_at or self.clock()
            recovered_attempt = attempt.model_copy(
                update={
                    "status": AttemptStatus.FAILED if poison else AttemptStatus.RETRYABLE,
                    "finished_at": finished,
                    "duration_ms": Decimal(
                        str(max(0, (finished - attempt.started_at).total_seconds()) * 1000)
                    ),
                    "error_type": (
                        "IndeterminateProviderOutcome"
                        if indeterminate
                        else "ProviderFailure"
                        if failure
                        else "LeaseExpired"
                    ),
                    "error_message": message,
                }
            )
            attempt_id = self._immutable(recovered_attempt, run_id)
            result_id = (
                self._immutable(
                    TaskResult(
                        task_id=str(task.values["id"]),
                        sample_id=str(task.values["sample_id"]),
                        solution_id=str(task.values["solution_id"]),
                        status=TaskStatus.POISONED,
                        attempts=int(str(task.values["attempts"])),
                        error=message,
                    ),
                    run_id,
                )
                if poison
                else None
            )
            run = self._required(f"run:{run_id}")
            run_values = dict(run.values)
            if poison:
                run_values["remaining"] = int(str(run_values["remaining"])) - 1
            due_key = None if poison else self._due_key(str(task.values["id"]), due)
            changes = [
                self._change(
                    task,
                    {
                        **task.values,
                        "status": status.value,
                        "lease_owner": None,
                        "lease_until": 0,
                        "lease_key": None,
                        "due_key": due_key,
                        "next_available": due,
                        "result_id": result_id,
                    },
                ),
                self._change(
                    attempt_row,
                    {
                        **attempt_row.values,
                        "artifact_id": attempt_id,
                        "retry_at": retry_at,
                    },
                ),
                StateChange(index.key, None, index.etag),
                self._event(
                    run,
                    run_values,
                    "task.poisoned" if poison else "task.recovered",
                    {"task_id": task.values["id"]},
                ),
                self._change(run, run_values),
                self._guard(run_id, executable=True),
            ]
            supervisor = self.state.get("supervisor")
            if supervisor is not None:
                changes.append(self._change(supervisor))
            if due_key is not None:
                changes.append(
                    StateChange(due_key, {"task_id": task.values["id"], "run_id": run_id})
                )
            try:
                self.state.transact(changes)
            except StateConflictError:
                continue
            if poison:
                poisoned += 1
            else:
                recovered += 1
        if recovered or poisoned:
            self._claim_cursors.clear()
        return recovered, poisoned

    def next_work_delay(
        self,
        run_id: str | None = None,
        *,
        allow_networked: bool = True,
        eligible_tasks: EligibleTasks | None = None,
    ) -> float:
        if eligible_tasks is not None and not eligible_tasks:
            return 5.0
        delay = 5.0
        cursor = None
        while True:
            page = self.state.page("due:", after=cursor, limit=100)
            for index in page.rows:
                delay = min(
                    delay,
                    self._candidate_delay(index, run_id, allow_networked, eligible_tasks),
                )
                if delay <= 0.05:
                    return delay
            cursor = page.cursor
            if cursor is None:
                return delay

    def _candidate_delay(
        self,
        index: StateRow,
        run_id: str | None,
        allow_networked: bool,
        eligible_tasks: EligibleTasks | None,
    ) -> float:
        if run_id is not None and index.values["run_id"] != run_id:
            return 5.0
        run = self._required(f"run:{index.values['run_id']}")
        if run.values["runtime_error"] is not None or (
            run.values["networked"] and not allow_networked
        ):
            return 5.0
        task = self._required(f"task:{index.values['task_id']}")
        if (
            eligible_tasks is not None
            and (str(task.values["run_id"]), str(task.values["solution_id"])) not in eligible_tasks
        ):
            return 5.0
        cooldown = self.state.get(f"cooldown:{task.values['throttle_scope']}")
        due = max(
            float(str(task.values["next_available"])),
            0 if cooldown is None else float(str(cooldown.values["until"])),
        )
        return max(0.05, min(5.0, due - self._now()))

    def acquire_finalizer(self, run_id: str) -> int | None:
        for _ in range(20):
            row = self._required(f"run:{run_id}")
            if (
                row.values["status"] in _TERMINAL
                or row.values["archival"]
                or not row.values["ready"]
                or row.values["remaining"] != 0
                or row.values["initialized"] != row.values["expected"]
            ):
                return None
            supervisor = self._supervisor()
            clock = self.state.get("finalizer-generation")
            token = (
                max(
                    int(str(row.values["finalizer_token"])),
                    0 if clock is None else int(str(clock.values["generation"])),
                )
                + 1
            )
            values = {
                **row.values,
                "finalizer_token": token,
                "finalizer_generation": supervisor.values["generation"],
            }
            event = self._event(row, values, "run.finalizer_acquired", {"token": token})
            try:
                self.state.transact(
                    (
                        self._change(row, values),
                        event,
                        self._change(supervisor),
                        self._guard(run_id, executable=True),
                        StateChange(
                            "finalizer-generation",
                            {"generation": token},
                            None if clock is None else clock.etag,
                        ),
                    )
                )
                return token
            except StateConflictError:
                continue
        raise FinalizerFenceError("finalizer contention")

    def finalize_run(
        self, run_id: str, token: int, status: RunStatus, report_artifact_id: str
    ) -> None:
        if status.value not in _TERMINAL:
            raise ValueError("invalid terminal run status")
        self.lifecycle.attach(run_id, report_artifact_id)
        for _ in range(20):
            row = self._required(f"run:{run_id}")
            if row.values["finalizer_token"] != token:
                raise FinalizerFenceError(run_id)
            if row.values["status"] in _TERMINAL:
                if (
                    row.values["status"] == status.value
                    and row.values["report_artifact_id"] == report_artifact_id
                ):
                    return
                raise FinalizerFenceError(run_id)
            if not row.values["ready"] or row.values["remaining"] != 0 or row.values["archival"]:
                raise FinalizerFenceError(run_id)
            try:
                supervisor = self._supervisor(int(str(row.values["finalizer_generation"])))
            except LeaseLostError as error:
                raise FinalizerFenceError(run_id) from error
            values = {
                **row.values,
                "status": status.value,
                "report_artifact_id": report_artifact_id,
                "runtime_error": None,
            }
            event = self._event(
                row,
                values,
                "run.finalized",
                {"status": status.value, "report_artifact_id": report_artifact_id},
            )
            active = self._required(f"active:{run_id}")
            admission = self._required("run-admission")
            try:
                self.state.transact(
                    (
                        self._change(row, values),
                        event,
                        StateChange(active.key, None, active.etag),
                        self._change(supervisor),
                        self._guard(run_id, executable=True),
                        self._change(
                            admission,
                            {
                                "runs": max(0, int(str(admission.values["runs"])) - 1),
                                "tasks": max(
                                    0,
                                    int(str(admission.values["tasks"]))
                                    - int(str(row.values["expected"])),
                                ),
                            },
                        ),
                    )
                )
                return
            except StateConflictError:
                continue
        raise FinalizerFenceError(run_id)

    def append_event(self, run_id: str, event_type: str, payload: dict[str, object]) -> int:
        import json

        normalized: dict[str, JsonValue] = json.loads(json.dumps(payload))
        for _ in range(20):
            run = self._required(f"run:{run_id}")
            values = dict(run.values)
            event = self._event(run, values, event_type, normalized)
            try:
                self.state.transact((self._change(run, values), event, self._guard(run_id)))
                return int(str(values["event_sequence"]))
            except StateConflictError:
                continue
        raise StateConflictError("event append contention")

    def list_events(self, run_id: str, after: int = 0, limit: int = 1000) -> Sequence[RunEvent]:
        self.lifecycle.require_live(run_id, ready=False)
        page = self.state.page(
            f"event:{run_id}:",
            after=f"event:{run_id}:{max(0, after):020d}",
            limit=min(limit, 500),
        )
        return tuple(RunEvent.model_validate(row.values, strict=False) for row in page.rows)

    def cleanup_records(self, root: LifecycleRoot) -> bool:
        if root.kind != "run":
            return True
        run = self.state.get(f"run:{root.id}")
        if root.pinned:
            raise RepositoryError("pinned roots cannot be collected")

        def commit(changes: Sequence[StateChange]) -> None:
            guard = self.lifecycle.cleanup_guard(root)
            if guard is None:
                raise StateConflictError("retention generation changed")
            self.state.transact((*changes, guard))

        reservations = self.state.page(f"launch-owner:{root.id}:", limit=40)
        reservation_changes = []
        pending_count = pending_tasks = 0
        for owner in reservations.rows:
            reservation = self.state.get(f"launch:{owner.values['key_hash']}")
            if reservation is not None and reservation.values["run_id"] == root.id:
                reserved = int(str(reservation.values.get("reserved_tasks", 0)))
                if reserved and not reservation.values.get("capacity_transferred", False):
                    pending_count += 1
                    pending_tasks += reserved
                reservation_changes.append(StateChange(reservation.key, None, reservation.etag))
            reservation_changes.append(StateChange(owner.key, None, owner.etag))
        if reservation_changes:
            if pending_count:
                admission = self._required("run-admission")
                reservation_changes.append(
                    self._change(
                        admission,
                        {
                            "runs": max(0, int(str(admission.values["runs"])) - pending_count),
                            "tasks": max(0, int(str(admission.values["tasks"])) - pending_tasks),
                        },
                    )
                )
            commit(reservation_changes)
        if reservations.cursor is not None:
            return False
        tasks = self.state.page(f"run-task:{root.id}:", limit=10)
        for index in tasks.rows:
            task = self._required(f"task:{index.values['task_id']}")
            attempts = self.state.page(f"attempt:{task.values['id']}:", limit=90)
            if attempts.rows:
                commit(tuple(StateChange(row.key, None, row.etag) for row in attempts.rows))
            if attempts.cursor is not None:
                return False
            changes = [
                StateChange(task.key, None, task.etag),
                StateChange(index.key, None, index.etag),
            ]
            for key in (task.values["due_key"], task.values["lease_key"]):
                if key is not None:
                    projection = self.state.get(str(key))
                    if projection is not None:
                        changes.append(StateChange(projection.key, None, projection.etag))
            commit(changes)
        if tasks.cursor is not None:
            return False
        events = self.state.page(f"event:{root.id}:", limit=90)
        if events.rows:
            commit(tuple(StateChange(row.key, None, row.etag) for row in events.rows))
        if events.cursor is not None:
            return False
        archive_claim = self.state.get(f"archive-reservation:{root.id}")
        if archive_claim is not None:
            commit((StateChange(archive_claim.key, None, archive_claim.etag),))
        if run is None:
            return True
        changes = [StateChange(run.key, None, run.etag)]
        if run.values["status"] not in _TERMINAL:
            admission = self._required("run-admission")
            changes.append(
                self._change(
                    admission,
                    {
                        "runs": max(0, int(str(admission.values["runs"])) - 1),
                        "tasks": max(
                            0,
                            int(str(admission.values["tasks"])) - int(str(run.values["expected"])),
                        ),
                    },
                )
            )
        for key in (
            run.values["history_key"],
            f"active:{root.id}",
            None
            if run.values["launch_key_hash"] is None
            else f"launch:{run.values['launch_key_hash']}",
        ):
            if key is not None:
                row = self.state.get(str(key))
                if row is not None:
                    changes.append(StateChange(row.key, None, row.etag))
        commit(changes)
        return True

    def cleanup_cooldowns(self) -> None:
        now = self.lifecycle.clock().timestamp()
        for prefix, cutoff in (
            ("cooldown:", now),
            ("cooldown-interval:", now - 10 * 24 * 60 * 60),
        ):
            key = f"maintenance:{prefix}"
            progress = self.state.get(key)
            cursor = None if progress is None else progress.values.get("cursor")
            page = self.state.page(prefix, after=None if cursor is None else str(cursor), limit=90)
            changes = [
                StateChange(row.key, None, row.etag)
                for row in page.rows
                if float(str(row.values["until"])) <= cutoff
            ]
            changes.append(
                StateChange(
                    key,
                    {"cursor": page.cursor},
                    None if progress is None else progress.etag,
                )
            )
            try:
                self.state.transact(changes)
            except StateConflictError:
                continue
