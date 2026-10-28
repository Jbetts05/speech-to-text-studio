"""Repository protocols and a durable SQLite implementation."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol, cast

from pydantic import Field

from ai_bench.azure_storage import AzureStorageRepositories
from ai_bench.contracts import (
    Artifact,
    ArtifactEvidence,
    AttemptStatus,
    Contract,
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
from ai_bench.hashing import canonical_json, normalize_media_type, sha256_digest, stable_task_id
from ai_bench.lifecycle import (
    ExpiredRootError,
    LifecycleRoot,
    LifecycleService,
    RootUnavailableError,
    root_key,
)
from ai_bench.runtime_policy import PublicExecutionPolicy

type EligibleTasks = frozenset[tuple[str, str]]


class RepositoryError(RuntimeError):
    pass


class LeaseLostError(RepositoryError):
    pass


class FinalizerFenceError(RepositoryError):
    pass


class IdempotencyConflictError(RepositoryError):
    pass


class AdmissionCapacityError(RepositoryError):
    retry_after = 5


class LaunchReservation(Contract):
    payload_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    run_id: str = Field(pattern=r"^[a-f0-9]{64}$")


class RunRecord(Contract):
    manifest: RunManifest
    manifest_artifact_id: str
    status: RunStatus
    report_artifact_id: str | None = None
    finalizer_token: int = Field(ge=0)
    runtime_error: str | None = None


class TaskRecord(Contract):
    id: str
    run_id: str
    sample_id: str
    solution_id: str
    status: TaskStatus
    attempts: int = Field(ge=0)
    max_attempts: int = Field(ge=1)
    lease_owner: str | None = None
    lease_token: int = Field(ge=0)
    result: TaskResult | None = None
    throttle_scope: str | None = None
    next_available_at: datetime | None = None
    cooldown_until: datetime | None = None


class Lease(Contract):
    task: TaskRecord
    owner: str
    token: int = Field(ge=1)
    attempt_number: int = Field(ge=1)
    supervisor_generation: int = Field(default=0, ge=0)
    retention_generation: int = Field(default=0, ge=0)
    retention_incarnation: int = Field(default=0, ge=0)


class BenchmarkDefinitionRepository(Protocol):
    def put_benchmark(self, benchmark_id: str, definition_json: str) -> None: ...

    def get_benchmark(self, benchmark_id: str) -> str | None: ...


class RunRepository(Protocol):
    def reserve_launch(
        self, key_hash: str, payload_hash: str, proposed_run_id: str, *, expected_tasks: int = 0
    ) -> str: ...

    def get_launch_reservation(self, key_hash: str) -> LaunchReservation | None: ...

    def upgrade_legacy_launch(
        self,
        key_hash: str,
        legacy_payload_hash: str,
        request_hash: str,
    ) -> str: ...

    def create_run(self, manifest: RunManifest, manifest_artifact_id: str) -> None: ...

    def get_run(self, run_id: str) -> RunRecord | None: ...

    def list_runs(self, limit: int = 100) -> Sequence[RunRecord]: ...

    def acquire_finalizer(self, run_id: str) -> int | None: ...

    def finalize_run(
        self, run_id: str, token: int, status: RunStatus, report_artifact_id: str
    ) -> None: ...


class TaskRepository(Protocol):
    def reconcile_tasks(self, manifest: RunManifest) -> int: ...

    def list_tasks(self, run_id: str) -> Sequence[TaskRecord]: ...

    def get_task(self, task_id: str) -> TaskRecord | None: ...

    def list_attempts(self, task_id: str) -> Sequence[TaskAttempt]: ...


class EventRepository(Protocol):
    def append_event(self, run_id: str, event_type: str, payload: dict[str, object]) -> int: ...

    def list_events(self, run_id: str, after: int = 0, limit: int = 1000) -> Sequence[RunEvent]: ...


class QueueRepository(Protocol):
    def claim_task(
        self,
        owner: str,
        lease_seconds: int = 60,
        run_id: str | None = None,
        *,
        allow_networked: bool = True,
        eligible_tasks: EligibleTasks | None = None,
    ) -> Lease | None: ...

    def complete_attempt(self, lease: Lease, result: TaskResult) -> None: ...

    def fail_attempt(
        self, lease: Lease, error: Exception, retry_delay_seconds: int = 0
    ) -> bool: ...

    def recover_expired_leases(
        self, *, active_task_ids: frozenset[str] = frozenset()
    ) -> tuple[int, int]: ...


class LeaseRepository(Protocol):
    def renew_lease(self, lease: Lease, lease_seconds: int = 60) -> None: ...


class ArtifactRepository(Protocol):
    def register_artifact(self, artifact: Artifact) -> None: ...

    def get_artifact(
        self,
        artifact_id: str,
        media_type: str | None = None,
    ) -> Artifact | None: ...

    def record_attempt_artifact(self, lease: Lease, evidence: ArtifactEvidence) -> None: ...

    def record_attempt_telemetry(self, lease: Lease, telemetry: TelemetryEvidence) -> None: ...


class ExecutionRepository(
    BenchmarkDefinitionRepository,
    RunRepository,
    TaskRepository,
    EventRepository,
    QueueRepository,
    LeaseRepository,
    ArtifactRepository,
    Protocol,
):
    def launch_for_key(self, key_hash: str) -> RunRecord | None: ...

    def unfinished_runs(self) -> Sequence[RunRecord]: ...

    def set_runtime_error(self, run_id: str, message: str | None) -> None: ...

    def record_attempt_failure(self, lease: Lease, failure: ProviderFailureEvidence) -> None: ...

    def release_unstarted(self, lease: Lease) -> None: ...

    def next_work_delay(
        self,
        run_id: str | None = None,
        *,
        allow_networked: bool = True,
        eligible_tasks: EligibleTasks | None = None,
    ) -> float: ...

    def acquire_supervisor(self, owner: str, lease_seconds: int = 30) -> bool: ...

    def renew_supervisor(
        self, owner: str, *, generation: int | None = None, lease_seconds: int = 30
    ) -> int | None: ...

    def release_supervisor(self, owner: str, *, generation: int | None = None) -> None: ...


class SqliteRepository(
    BenchmarkDefinitionRepository,
    RunRepository,
    TaskRepository,
    EventRepository,
    QueueRepository,
    LeaseRepository,
    ArtifactRepository,
):
    """A restart-safe local repository with transactional queue and fencing."""

    def __init__(
        self,
        path: Path,
        lifecycle: LifecycleService | None = None,
        public_policy: PublicExecutionPolicy | None = None,
    ) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lifecycle = lifecycle
        self.public_policy = public_policy
        self._initialize()

    def _assert_run_live(self, run_id: str, *, executable: bool = False) -> None:
        if self.lifecycle is not None and self.lifecycle.get(run_id) is not None:
            root = self.lifecycle.require_live(run_id, ready=False)
            if executable and root.archival:
                raise RepositoryError("archival evidence is never executable")

    @contextmanager
    def _connection(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            if immediate:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS benchmarks (
                    id TEXT PRIMARY KEY,
                    definition_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    manifest_json TEXT NOT NULL,
                    manifest_artifact_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    report_artifact_id TEXT,
                    finalizer_token INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS launch_requests (
                    key_hash TEXT PRIMARY KEY,
                    payload_hash TEXT NOT NULL,
                    run_id TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS runs_status ON runs(status);
                CREATE INDEX IF NOT EXISTS launch_requests_by_run ON launch_requests(run_id);
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    sample_id TEXT NOT NULL,
                    solution_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL,
                    next_available REAL NOT NULL DEFAULT 0,
                    lease_owner TEXT,
                    lease_token INTEGER NOT NULL DEFAULT 0,
                    lease_until REAL,
                    result_json TEXT,
                    UNIQUE(run_id, sample_id, solution_id)
                );
                CREATE INDEX IF NOT EXISTS tasks_queue
                    ON tasks(status, next_available, run_id, id);
                CREATE TABLE IF NOT EXISTS attempts (
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    attempt_number INTEGER NOT NULL,
                    lease_token INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    error_type TEXT,
                    error_message TEXT,
                    PRIMARY KEY(task_id, attempt_number)
                );
                CREATE TABLE IF NOT EXISTS attempt_artifacts (
                    task_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    evidence_json TEXT,
                    PRIMARY KEY(task_id, attempt_number, role),
                    FOREIGN KEY(task_id, attempt_number)
                        REFERENCES attempts(task_id, attempt_number)
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    event_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_by_run ON events(run_id, sequence);
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    artifact_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lifecycle_migrations (
                    version INTEGER PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS legacy_artifact_owners (
                    artifact_id TEXT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS artifact_variants (
                    id TEXT NOT NULL REFERENCES artifacts(id),
                    descriptor_key TEXT NOT NULL,
                    media_type_key TEXT NOT NULL,
                    modality TEXT NOT NULL,
                    artifact_json TEXT NOT NULL,
                    PRIMARY KEY(id, descriptor_key)
                );
                CREATE TABLE IF NOT EXISTS provider_cooldowns (
                    scope TEXT PRIMARY KEY,
                    until REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cooldown_intervals (
                    scope TEXT NOT NULL,
                    started REAL NOT NULL,
                    until REAL NOT NULL,
                    task_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    PRIMARY KEY(task_id, attempt_number)
                );
                CREATE INDEX IF NOT EXISTS cooldown_scope ON cooldown_intervals(scope, until);
                CREATE TABLE IF NOT EXISTS worker_supervisor (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    owner TEXT NOT NULL,
                    lease_until REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS finalizer_clock (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    generation INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO finalizer_clock(singleton, generation)
                    SELECT 1, COALESCE(MAX(finalizer_token), 0) FROM runs;
                """
            )
            migrated = connection.execute(
                "SELECT 1 FROM lifecycle_migrations WHERE version=1"
            ).fetchone()
            if migrated is None:
                connection.execute(
                    "INSERT OR IGNORE INTO legacy_artifact_owners SELECT id FROM artifacts"
                )
                connection.execute("INSERT INTO lifecycle_migrations(version) VALUES (1)")
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(attempts)").fetchall()
            }
            if "telemetry_json" not in columns:
                connection.execute("ALTER TABLE attempts ADD COLUMN telemetry_json TEXT")
            artifact_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(attempt_artifacts)").fetchall()
            }
            if "evidence_json" not in artifact_columns:
                connection.execute("ALTER TABLE attempt_artifacts ADD COLUMN evidence_json TEXT")
            for table, additions in {
                "runs": {
                    "runtime_error": "TEXT",
                    "finalizer_generation": "INTEGER NOT NULL DEFAULT 0",
                },
                "launch_requests": {
                    "expected_tasks": "INTEGER NOT NULL DEFAULT 0",
                    "capacity_transferred": "INTEGER NOT NULL DEFAULT 0",
                },
                "worker_supervisor": {"generation": "INTEGER NOT NULL DEFAULT 1"},
                "tasks": {
                    "throttle_scope": "TEXT",
                    "waiting_since": "REAL NOT NULL DEFAULT 0",
                    "resume_attempt": "INTEGER NOT NULL DEFAULT 0",
                },
                "attempts": {
                    "failure_json": "TEXT",
                    "retry_at": "REAL",
                    "queue_wait_ms": "TEXT",
                    "cooldown_wait_ms": "TEXT",
                    "dispatched": "INTEGER NOT NULL DEFAULT 0",
                    "receipt_id": "TEXT",
                },
            }.items():
                existing = {
                    str(row["name"])
                    for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
                }
                for name, declaration in additions.items():
                    if name not in existing:
                        connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    def reserve_launch(
        self, key_hash: str, payload_hash: str, proposed_run_id: str, *, expected_tasks: int = 0
    ) -> str:
        if expected_tasks < 0:
            raise ValueError("expected task count cannot be negative")
        with self._connection(immediate=True) as connection:
            existing = connection.execute(
                "SELECT payload_hash, run_id FROM launch_requests WHERE key_hash=?",
                (key_hash,),
            ).fetchone()
            if existing is not None:
                if str(existing["payload_hash"]) != payload_hash:
                    raise IdempotencyConflictError(
                        "the Idempotency-Key was already used with a different request"
                    )
                return str(existing["run_id"])
            if expected_tasks and self.public_policy is not None:
                active, slots = self._admission_totals(connection)
                if (
                    active >= self.public_policy.max_active_runs
                    or slots + expected_tasks > self.public_policy.max_outstanding_tasks
                ):
                    raise AdmissionCapacityError(
                        "outstanding run/task capacity is temporarily full"
                    )
            connection.execute(
                """
                INSERT INTO launch_requests(key_hash, payload_hash, run_id, expected_tasks)
                VALUES (?, ?, ?, ?)
                """,
                (key_hash, payload_hash, proposed_run_id, expected_tasks),
            )
            return proposed_run_id

    @staticmethod
    def _admission_totals(connection: sqlite3.Connection) -> tuple[int, int]:
        active = connection.execute(
            "SELECT COUNT(*) AS runs, COALESCE(SUM("
            "json_array_length(manifest_json, '$.sample_ids') * "
            "json_array_length(manifest_json, '$.solution_ids')), 0) AS tasks "
            "FROM runs WHERE status IN ('created', 'running')"
        ).fetchone()
        pending = connection.execute(
            "SELECT COUNT(*) AS runs, COALESCE(SUM(expected_tasks), 0) AS tasks "
            "FROM launch_requests WHERE expected_tasks>0 AND capacity_transferred=0"
        ).fetchone()
        assert active is not None and pending is not None
        return int(active["runs"]) + int(pending["runs"]), int(active["tasks"]) + int(
            pending["tasks"]
        )

    def launch_for_key(self, key_hash: str) -> RunRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT r.* FROM launch_requests l JOIN runs r ON r.id=l.run_id WHERE l.key_hash=?",
                (key_hash,),
            ).fetchone()
        if row is not None:
            self._assert_run_live(str(row["id"]))
        return None if row is None else self._run_record(row)

    def get_launch_reservation(self, key_hash: str) -> LaunchReservation | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload_hash, run_id FROM launch_requests WHERE key_hash=?",
                (key_hash,),
            ).fetchone()
        return (
            None
            if row is None
            else LaunchReservation(payload_hash=str(row["payload_hash"]), run_id=str(row["run_id"]))
        )

    def upgrade_legacy_launch(
        self,
        key_hash: str,
        legacy_payload_hash: str,
        request_hash: str,
    ) -> str:
        expected_run_id = sha256_digest(
            {
                "idempotency_key_hash": key_hash,
                "payload_hash": legacy_payload_hash,
            }
        )
        with self._connection(immediate=True) as connection:
            row = connection.execute(
                "SELECT payload_hash, run_id FROM launch_requests WHERE key_hash=?",
                (key_hash,),
            ).fetchone()
            if row is not None and row["payload_hash"] == request_hash:
                return str(row["run_id"])
            if (
                row is None
                or row["payload_hash"] != legacy_payload_hash
                or row["run_id"] != expected_run_id
            ):
                raise IdempotencyConflictError(
                    "the Idempotency-Key does not match the interrupted legacy launch descriptors"
                )
            connection.execute(
                "UPDATE launch_requests SET payload_hash=? WHERE key_hash=?",
                (request_hash, key_hash),
            )
            return str(row["run_id"])

    def put_benchmark(self, benchmark_id: str, definition_json: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO benchmarks(id, definition_json) VALUES (?, ?)
                ON CONFLICT(id) DO UPDATE SET definition_json=excluded.definition_json
                """,
                (benchmark_id, definition_json),
            )

    def get_benchmark(self, benchmark_id: str) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT definition_json FROM benchmarks WHERE id=?", (benchmark_id,)
            ).fetchone()
        return None if row is None else str(row["definition_json"])

    def create_run(self, manifest: RunManifest, manifest_artifact_id: str) -> None:
        with self._connection(immediate=True) as connection:
            self._assert_run_live(manifest.run_id)
            existing = connection.execute(
                "SELECT 1 FROM runs WHERE id=?", (manifest.run_id,)
            ).fetchone()
            reserved = connection.execute(
                "SELECT expected_tasks, capacity_transferred FROM launch_requests "
                "WHERE key_hash=? AND run_id=?",
                (manifest.launch_idempotency_key_hash, manifest.run_id),
            ).fetchone()
            reserved_tasks = (
                int(reserved["expected_tasks"])
                if reserved is not None and not reserved["capacity_transferred"]
                else 0
            )
            requested_tasks = len(manifest.sample_ids) * len(manifest.solution_ids)
            if reserved_tasks and reserved_tasks != requested_tasks:
                raise IdempotencyConflictError("run does not match its reserved task capacity")
            if existing is None and self.public_policy is not None:
                limits = self.public_policy
                if manifest.retry_limit > limits.max_attempts:
                    raise ValueError("run exceeds the configured total-attempt limit")
                active, slots = self._admission_totals(connection)
                if not reserved_tasks and (
                    active >= limits.max_active_runs
                    or slots + requested_tasks > limits.max_outstanding_tasks
                ):
                    raise AdmissionCapacityError(
                        "outstanding run/task capacity is temporarily full"
                    )
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO runs(
                    id, manifest_json, manifest_artifact_id, status, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    manifest.run_id,
                    manifest.model_dump_json(),
                    manifest_artifact_id,
                    RunStatus.RUNNING.value,
                    manifest.created_at.isoformat(),
                ),
            )
            if cursor.rowcount:
                self._append_event_tx(connection, manifest.run_id, "run.created", {})
                if reserved_tasks:
                    connection.execute(
                        "UPDATE launch_requests SET capacity_transferred=1 WHERE key_hash=?",
                        (manifest.launch_idempotency_key_hash,),
                    )

    def get_run(self, run_id: str) -> RunRecord | None:
        self._assert_run_live(run_id)
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return None if row is None else self._run_record(row)

    def list_runs(self, limit: int = 100) -> Sequence[RunRecord]:
        bounded = max(1, min(limit, 500))
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (bounded,)
            ).fetchall()
        result = []
        for row in rows:
            try:
                self._assert_run_live(str(row["id"]))
            except (ExpiredRootError, RootUnavailableError):
                continue
            result.append(self._run_record(row))
        return tuple(result)

    def unfinished_runs(self) -> Sequence[RunRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM runs WHERE status IN (?, ?) ORDER BY created_at",
                (RunStatus.RUNNING.value, RunStatus.CREATED.value),
            ).fetchall()
        result = []
        for row in rows:
            try:
                self._assert_run_live(str(row["id"]), executable=True)
            except (ExpiredRootError, RootUnavailableError):
                continue
            result.append(self._run_record(row))
        return tuple(result)

    def set_runtime_error(self, run_id: str, message: str | None) -> None:
        with self._connection(immediate=True) as connection:
            self._assert_run_live(run_id)
            cursor = connection.execute(
                "UPDATE runs SET runtime_error=? WHERE id=? AND runtime_error IS NOT ?",
                (message, run_id, message),
            )
            if cursor.rowcount:
                self._append_event_tx(
                    connection,
                    run_id,
                    "run.blocked" if message else "run.resumed",
                    {"message": message},
                )

    def reconcile_tasks(self, manifest: RunManifest) -> int:
        created = 0
        with self._connection(immediate=True) as connection:
            self._assert_run_live(manifest.run_id, executable=True)
            for sample_id in manifest.sample_ids:
                for solution_id in manifest.solution_ids:
                    task_id = stable_task_id(manifest.run_id, sample_id, solution_id)
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO tasks(
                            id, run_id, sample_id, solution_id, status, max_attempts,
                            throttle_scope, waiting_since
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            task_id,
                            manifest.run_id,
                            sample_id,
                            solution_id,
                            TaskStatus.PENDING.value,
                            manifest.retry_limit,
                            manifest.solution_descriptors[solution_id].throttle_scope,
                            time.time(),
                        ),
                    )
                    if cursor.rowcount:
                        created += 1
                        self._append_event_tx(
                            connection,
                            manifest.run_id,
                            "task.enqueued",
                            {
                                "task_id": task_id,
                                "sample_id": sample_id,
                                "solution_id": solution_id,
                            },
                        )
        return created

    def list_tasks(self, run_id: str) -> Sequence[TaskRecord]:
        self._assert_run_live(run_id)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT t.*, c.until AS cooldown_until FROM tasks t
                LEFT JOIN provider_cooldowns c ON c.scope=t.throttle_scope
                WHERE run_id=? ORDER BY sample_id, solution_id
                """,
                (run_id,),
            ).fetchall()
        return tuple(self._task_record(row) for row in rows)

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT t.*, c.until AS cooldown_until FROM tasks t
                LEFT JOIN provider_cooldowns c ON c.scope=t.throttle_scope WHERE t.id=?
                """,
                (task_id,),
            ).fetchone()
        if row is not None:
            self._assert_run_live(str(row["run_id"]))
        return None if row is None else self._task_record(row)

    def list_attempts(self, task_id: str) -> Sequence[TaskAttempt]:
        self.get_task(task_id)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM attempts WHERE task_id=?
                ORDER BY attempt_number
                """,
                (task_id,),
            ).fetchall()
            artifacts = connection.execute(
                """
                SELECT
                    aa.attempt_number,
                    aa.role,
                    aa.artifact_id,
                    aa.evidence_json,
                    a.artifact_json
                FROM attempt_artifacts AS aa
                LEFT JOIN artifacts AS a ON a.id = aa.artifact_id
                WHERE aa.task_id=?
                ORDER BY aa.attempt_number, aa.ordinal
                """,
                (task_id,),
            ).fetchall()
        evidence_by_attempt: dict[int, list[ArtifactEvidence]] = {}
        for artifact in artifacts:
            attempt_number = int(artifact["attempt_number"])
            if artifact["evidence_json"] is not None:
                evidence = ArtifactEvidence.model_validate_json(
                    str(artifact["evidence_json"]),
                    strict=False,
                )
            else:
                if artifact["artifact_json"] is None:
                    raise RepositoryError(
                        "legacy attempt evidence references an unregistered artifact"
                    )
                descriptor = Artifact.model_validate_json(
                    str(artifact["artifact_json"]),
                    strict=False,
                )
                evidence = ArtifactEvidence.model_validate(
                    {
                        "role": str(artifact["role"]),
                        "artifact_id": str(artifact["artifact_id"]),
                        "modality": descriptor.modality,
                        "media_type": descriptor.media_type,
                        "size_bytes": descriptor.size_bytes,
                        "attempt_number": attempt_number,
                    },
                    strict=False,
                )
            evidence_by_attempt.setdefault(attempt_number, []).append(evidence)
        attempts: list[TaskAttempt] = []
        for row in rows:
            started_at = datetime.fromisoformat(str(row["started_at"]))
            finished_at = (
                None
                if row["finished_at"] is None
                else datetime.fromisoformat(str(row["finished_at"]))
            )
            elapsed_microseconds: int | None = None
            if finished_at is not None:
                elapsed = finished_at - started_at
                candidate = (
                    elapsed.days * 86_400 + elapsed.seconds
                ) * 1_000_000 + elapsed.microseconds
                if candidate >= 0:
                    elapsed_microseconds = candidate
            attempts.append(
                TaskAttempt(
                    task_id=task_id,
                    attempt_number=int(row["attempt_number"]),
                    status=AttemptStatus(str(row["status"])),
                    lease_token=int(row["lease_token"]),
                    started_at=started_at,
                    finished_at=finished_at,
                    duration_ms=(
                        None
                        if elapsed_microseconds is None
                        else Decimal(elapsed_microseconds) / Decimal(1000)
                    ),
                    evidence=tuple(evidence_by_attempt.get(int(row["attempt_number"]), ())),
                    telemetry=(
                        None
                        if row["telemetry_json"] is None
                        else TelemetryEvidence.model_validate_json(
                            row["telemetry_json"], strict=False
                        )
                    ),
                    error_type=(None if row["error_type"] is None else str(row["error_type"])),
                    error_message=(
                        None if row["error_message"] is None else str(row["error_message"])
                    ),
                    failure=(
                        None
                        if row["failure_json"] is None
                        else ProviderFailureEvidence.model_validate_json(row["failure_json"])
                    ),
                    queue_wait_ms=(
                        None if row["queue_wait_ms"] is None else Decimal(row["queue_wait_ms"])
                    ),
                    cooldown_wait_ms=(
                        None
                        if row["cooldown_wait_ms"] is None
                        else Decimal(row["cooldown_wait_ms"])
                    ),
                )
            )
        return tuple(attempts)

    def claim_task(
        self,
        owner: str,
        lease_seconds: int = 60,
        run_id: str | None = None,
        *,
        allow_networked: bool = True,
        eligible_tasks: EligibleTasks | None = None,
    ) -> Lease | None:
        if not owner or len(owner) > 200:
            raise ValueError("lease owner must be 1-200 characters")
        now = time.time()
        with self._connection(immediate=True) as connection:
            run_filter = "" if run_id is None else " AND t.run_id=?"
            parameters: tuple[object, ...] = (TaskStatus.PENDING.value, now)
            if run_id is not None:
                parameters = (*parameters, run_id)
            candidates = connection.execute(
                f"""
                SELECT t.* FROM tasks t
                JOIN runs r ON r.id=t.run_id
                LEFT JOIN provider_cooldowns c ON c.scope=t.throttle_scope
                WHERE t.status=? AND MAX(t.next_available, COALESCE(c.until, 0))<=?{run_filter}
                    AND r.runtime_error IS NULL
                    AND (? OR json_extract(r.manifest_json, '$.networked_execution')=0)
                    AND (? IS NULL OR EXISTS (
                        SELECT 1 FROM json_each(?) e
                        WHERE json_extract(e.value, '$[0]')=t.run_id
                            AND json_extract(e.value, '$[1]')=t.solution_id
                    ))
                ORDER BY t.attempts DESC, t.next_available, t.run_id, t.id
                """,
                (
                    *parameters,
                    allow_networked,
                    None if eligible_tasks is None else json.dumps(sorted(eligible_tasks)),
                    None if eligible_tasks is None else json.dumps(sorted(eligible_tasks)),
                ),
            )
            row = None
            for candidate in candidates:
                try:
                    self._assert_run_live(str(candidate["run_id"]), executable=True)
                except (ExpiredRootError, RootUnavailableError):
                    continue
                row = candidate
                break
            if row is None:
                return None
            supervisor = connection.execute(
                "SELECT * FROM worker_supervisor WHERE singleton=1"
            ).fetchone()
            generation = 0
            if supervisor is not None and float(supervisor["lease_until"]) > now:
                if supervisor["owner"] != owner:
                    return None
                generation = int(supervisor["generation"])
            token = int(row["lease_token"]) + 1
            resume = bool(row["resume_attempt"])
            attempt = int(row["attempts"]) if resume else int(row["attempts"]) + 1
            waiting_since = float(row["waiting_since"])
            waiting_ms = max(0, (now - waiting_since) * 1000) if waiting_since else 0
            cooldown_ms = self._cooldown_wait(connection, row["throttle_scope"], waiting_since, now)
            cursor = connection.execute(
                """
                UPDATE tasks SET status=?, attempts=?, lease_owner=?, lease_token=?,
                    lease_until=?, resume_attempt=0
                WHERE id=? AND status=?
                """,
                (
                    TaskStatus.LEASED.value,
                    attempt,
                    owner,
                    token,
                    now + max(1, min(lease_seconds, 3600)),
                    row["id"],
                    TaskStatus.PENDING.value,
                ),
            )
            if cursor.rowcount != 1:
                return None
            if resume:
                connection.execute(
                    "UPDATE attempts SET lease_token=? WHERE task_id=? AND attempt_number=?",
                    (token, row["id"], attempt),
                )
            else:
                connection.execute(
                    """
                INSERT INTO attempts(
                    task_id, attempt_number, lease_token, status, started_at,
                    queue_wait_ms, cooldown_wait_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        row["id"],
                        attempt,
                        token,
                        AttemptStatus.RUNNING.value,
                        utc_now().isoformat(),
                        str(max(0, waiting_ms - cooldown_ms)),
                        str(cooldown_ms),
                    ),
                )
            self._append_event_tx(
                connection,
                str(row["run_id"]),
                "task.leased",
                {"task_id": str(row["id"]), "attempt": attempt, "lease_token": token},
            )
            updated = connection.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone()
            assert updated is not None
            root = None if self.lifecycle is None else self.lifecycle.get(str(row["run_id"]))
            return Lease(
                task=self._task_record(updated),
                owner=owner,
                token=token,
                attempt_number=attempt,
                supervisor_generation=generation,
                retention_generation=0 if root is None else root.generation,
                retention_incarnation=0 if root is None else root.incarnation,
            )

    def renew_lease(self, lease: Lease, lease_seconds: int = 60) -> None:
        with self._connection(immediate=True) as connection:
            self._assert_lease(connection, lease)
            cursor = connection.execute(
                """
                UPDATE tasks SET lease_until=?
                WHERE id=? AND status=? AND lease_owner=? AND lease_token=?
                """,
                (
                    time.time() + max(1, min(lease_seconds, 3600)),
                    lease.task.id,
                    TaskStatus.LEASED.value,
                    lease.owner,
                    lease.token,
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError(lease.task.id)

    def register_artifact(self, artifact: Artifact) -> None:
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT artifact_json FROM artifact_variants WHERE id=? AND descriptor_key=?",
                (artifact.id, sha256_digest(artifact)),
            ).fetchone()
            if existing is not None:
                if canonical_json(
                    Artifact.model_validate_json(existing["artifact_json"], strict=False)
                ) != canonical_json(artifact):
                    raise RepositoryError("immutable artifact descriptor conflict")
                return
            connection.execute(
                "INSERT OR IGNORE INTO artifacts(id, artifact_json) VALUES (?, ?)",
                (artifact.id, artifact.model_dump_json()),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO artifact_variants(
                    id, descriptor_key, media_type_key, modality, artifact_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    artifact.id,
                    sha256_digest(artifact),
                    normalize_media_type(artifact.media_type),
                    artifact.modality.value,
                    artifact.model_dump_json(),
                ),
            )
            variants = connection.execute(
                """
                SELECT artifact_json FROM artifact_variants
                WHERE id=?
                """,
                (artifact.id,),
            ).fetchall()
            canonical = min(
                (
                    Artifact.model_validate_json(row["artifact_json"], strict=False)
                    for row in variants
                ),
                key=lambda item: (
                    normalize_media_type(item.media_type),
                    item.modality.value,
                    canonical_json(item),
                ),
            )
            connection.execute(
                "UPDATE artifacts SET artifact_json=? WHERE id=?",
                (canonical.model_dump_json(), artifact.id),
            )

    def get_artifact(
        self,
        artifact_id: str,
        media_type: str | None = None,
    ) -> Artifact | None:
        with self._connection() as connection:
            if media_type is None:
                row = connection.execute(
                    "SELECT artifact_json FROM artifacts WHERE id=?", (artifact_id,)
                ).fetchone()
            else:
                variants = connection.execute(
                    """
                    SELECT artifact_json FROM artifact_variants
                    WHERE id=? AND media_type_key=?
                    """,
                    (artifact_id, normalize_media_type(media_type)),
                ).fetchall()
                return min(
                    (
                        Artifact.model_validate_json(item["artifact_json"], strict=False)
                        for item in variants
                    ),
                    key=lambda item: (item.modality.value, canonical_json(item)),
                    default=None,
                )
        return (
            None
            if row is None
            else Artifact.model_validate_json(row["artifact_json"], strict=False)
        )

    def record_attempt_artifact(self, lease: Lease, evidence: ArtifactEvidence) -> None:
        if evidence.attempt_number != lease.attempt_number:
            raise ValueError("attempt evidence number does not match lease")
        role_order = {"raw": 1, "parsed": 2, "comparison": 3, "result": 4}
        if evidence.role not in role_order:
            raise ValueError("only attempt-specific evidence may be recorded")
        with self._connection(immediate=True) as connection:
            self._assert_lease(connection, lease)
            existing = connection.execute(
                "SELECT evidence_json FROM attempt_artifacts "
                "WHERE task_id=? AND attempt_number=? AND role=?",
                (lease.task.id, lease.attempt_number, evidence.role),
            ).fetchone()
            if existing is not None:
                if existing["evidence_json"] != evidence.model_dump_json():
                    raise RepositoryError("attempt evidence is immutable")
                return
            previous = connection.execute(
                """
                SELECT MAX(ordinal) AS ordinal FROM attempt_artifacts
                WHERE task_id=? AND attempt_number=?
                """,
                (lease.task.id, lease.attempt_number),
            ).fetchone()
            prior_order = (
                0 if previous is None or previous["ordinal"] is None else int(previous["ordinal"])
            )
            if role_order[evidence.role] <= prior_order:
                raise RepositoryError("attempt evidence must be persisted in evidence order")
            connection.execute(
                """
                INSERT INTO attempt_artifacts(
                    task_id, attempt_number, role, artifact_id, ordinal, evidence_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    lease.task.id,
                    lease.attempt_number,
                    evidence.role,
                    evidence.artifact_id,
                    role_order[evidence.role],
                    evidence.model_dump_json(),
                ),
            )

    def record_attempt_telemetry(self, lease: Lease, telemetry: TelemetryEvidence) -> None:
        with self._connection(immediate=True) as connection:
            self._assert_lease(connection, lease)
            cursor = connection.execute(
                """
                UPDATE attempts SET telemetry_json=?
                WHERE task_id=? AND attempt_number=? AND lease_token=? AND status=?
                """,
                (
                    telemetry.model_dump_json(),
                    lease.task.id,
                    lease.attempt_number,
                    lease.token,
                    AttemptStatus.RUNNING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError(lease.task.id)

    def record_attempt_failure(self, lease: Lease, failure: ProviderFailureEvidence) -> None:
        with self._connection(immediate=True) as connection:
            task = self._assert_lease(connection, lease)
            existing = connection.execute(
                "SELECT failure_json FROM attempts WHERE task_id=? AND attempt_number=?",
                (lease.task.id, lease.attempt_number),
            ).fetchone()
            if existing is not None and existing["failure_json"] is not None:
                if existing["failure_json"] != failure.model_dump_json():
                    raise RepositoryError("attempt failure evidence is immutable")
                return
            if failure.raw_artifact_id is not None:
                raw = connection.execute(
                    """
                    SELECT artifact_id FROM attempt_artifacts
                    WHERE task_id=? AND attempt_number=? AND role='raw'
                    """,
                    (lease.task.id, lease.attempt_number),
                ).fetchone()
                if raw is None or raw["artifact_id"] != failure.raw_artifact_id:
                    raise RepositoryError(
                        "raw failure evidence must precede failure classification"
                    )
            now = time.time()
            delay = float(failure.retry_delay.scheduled_seconds) if failure.retry_delay else 0
            connection.execute(
                """
                UPDATE attempts SET failure_json=?, retry_at=?, finished_at=?
                WHERE task_id=? AND attempt_number=?
                """,
                (
                    failure.model_dump_json(),
                    now + delay,
                    utc_now().isoformat(),
                    lease.task.id,
                    lease.attempt_number,
                ),
            )
            if failure.retryable and task["throttle_scope"] and delay > 0:
                connection.execute(
                    """
                    INSERT INTO provider_cooldowns(scope, until) VALUES (?, ?)
                    ON CONFLICT(scope) DO UPDATE SET until=MAX(until, excluded.until)
                    """,
                    (task["throttle_scope"], now + delay),
                )
                connection.execute(
                    """
                    INSERT INTO cooldown_intervals(scope, started, until, task_id, attempt_number)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (task["throttle_scope"], now, now + delay, lease.task.id, lease.attempt_number),
                )

    def begin_dispatch(self, lease: Lease) -> None:
        from ai_bench.receipts import UncertainDispatchError

        with self._connection(immediate=True) as connection:
            self._assert_lease(connection, lease)
            row = connection.execute(
                "SELECT dispatched FROM attempts WHERE task_id=? AND attempt_number=?",
                (lease.task.id, lease.attempt_number),
            ).fetchone()
            if row is None or row["dispatched"]:
                raise UncertainDispatchError(
                    "a request-start checkpoint already exists for this attempt"
                )
            connection.execute(
                "UPDATE attempts SET dispatched=1 WHERE task_id=? AND attempt_number=?",
                (lease.task.id, lease.attempt_number),
            )
            self._append_event_tx(
                connection,
                lease.task.run_id,
                "attempt.request_started",
                {
                    "task_id": lease.task.id,
                    "attempt": lease.attempt_number,
                    "lease_token": lease.token,
                    "supervisor_generation": lease.supervisor_generation,
                    "submission": "unconfirmed",
                },
            )

    def response_receipt(self, lease: Lease) -> str | None:
        with self._connection() as connection:
            self._assert_lease(connection, lease)
            row = connection.execute(
                "SELECT receipt_id FROM attempts WHERE task_id=? AND attempt_number=?",
                (lease.task.id, lease.attempt_number),
            ).fetchone()
        return None if row is None or row["receipt_id"] is None else str(row["receipt_id"])

    def record_response_receipt(self, lease: Lease, artifact_id: str) -> None:
        with self._connection(immediate=True) as connection:
            self._assert_lease(connection, lease)
            row = connection.execute(
                "SELECT receipt_id FROM attempts WHERE task_id=? AND attempt_number=?",
                (lease.task.id, lease.attempt_number),
            ).fetchone()
            if row is None or (row["receipt_id"] is not None and row["receipt_id"] != artifact_id):
                raise RepositoryError("response receipts are immutable")
            connection.execute(
                "UPDATE attempts SET receipt_id=? WHERE task_id=? AND attempt_number=?",
                (artifact_id, lease.task.id, lease.attempt_number),
            )

    def complete_attempt(self, lease: Lease, result: TaskResult) -> None:
        if (
            result.task_id != lease.task.id
            or result.status is not TaskStatus.SUCCEEDED
            or result.sample_id != lease.task.sample_id
            or result.solution_id != lease.task.solution_id
            or result.attempts != lease.attempt_number
        ):
            raise ValueError("successful task result does not match lease")
        with self._connection(immediate=True) as connection:
            self._assert_lease(connection, lease)
            roles = {
                str(row["role"])
                for row in connection.execute(
                    """
                    SELECT role FROM attempt_artifacts
                    WHERE task_id=? AND attempt_number=?
                    """,
                    (lease.task.id, lease.attempt_number),
                ).fetchall()
            }
            if not {"raw", "parsed", "result"}.issubset(roles):
                raise RepositoryError("raw, parsed, and result evidence must precede completion")
            connection.execute(
                """
                UPDATE attempts SET status=?, finished_at=?
                WHERE task_id=? AND attempt_number=?
                """,
                (
                    AttemptStatus.SUCCEEDED.value,
                    utc_now().isoformat(),
                    lease.task.id,
                    lease.attempt_number,
                ),
            )
            connection.execute(
                """
                UPDATE tasks SET status=?, lease_owner=NULL, lease_until=NULL, result_json=?
                WHERE id=?
                """,
                (TaskStatus.SUCCEEDED.value, result.model_dump_json(), lease.task.id),
            )
            self._append_event_tx(
                connection,
                lease.task.run_id,
                "task.succeeded",
                {"task_id": lease.task.id, "attempt": lease.attempt_number},
            )

    def fail_attempt(self, lease: Lease, error: Exception, retry_delay_seconds: int = 0) -> bool:
        with self._connection(immediate=True) as connection:
            row = self._assert_lease(connection, lease)
            failure = error.evidence if isinstance(error, ProviderFailure) else None
            now = time.time()
            retry_at = now + max(0, min(retry_delay_seconds, 86400))
            waiting_since = now
            if failure is not None:
                recorded = connection.execute(
                    "SELECT failure_json, retry_at FROM attempts "
                    "WHERE task_id=? AND attempt_number=?",
                    (lease.task.id, lease.attempt_number),
                ).fetchone()
                if recorded is None or recorded["failure_json"] != failure.model_dump_json():
                    raise RepositoryError("typed failure evidence must precede retry scheduling")
                retry_at = float(recorded["retry_at"])
                waiting_since = retry_at - (
                    float(failure.retry_delay.scheduled_seconds) if failure.retry_delay else 0
                )
            poison = int(row["attempts"]) >= int(row["max_attempts"]) or (
                failure is not None and not failure.retryable
            )
            task_status = TaskStatus.POISONED if poison else TaskStatus.PENDING
            attempt_status = AttemptStatus.FAILED if poison else AttemptStatus.RETRYABLE
            message = str(error)[:2000]
            delay = max(0, retry_at - now)
            connection.execute(
                """
                UPDATE attempts
                SET status=?, finished_at=COALESCE(finished_at, ?), error_type=?, error_message=?
                WHERE task_id=? AND attempt_number=?
                """,
                (
                    attempt_status.value,
                    utc_now().isoformat(),
                    type(error).__name__,
                    message,
                    lease.task.id,
                    lease.attempt_number,
                ),
            )
            result = None
            if poison:
                result = TaskResult(
                    task_id=lease.task.id,
                    sample_id=lease.task.sample_id,
                    solution_id=lease.task.solution_id,
                    status=TaskStatus.POISONED,
                    attempts=lease.attempt_number,
                    error=f"{type(error).__name__}: {message}",
                ).model_dump_json()
            connection.execute(
                """
                UPDATE tasks SET status=?, next_available=?, lease_owner=NULL,
                    lease_until=NULL, result_json=?, waiting_since=?
                WHERE id=?
                """,
                (
                    task_status.value,
                    retry_at,
                    result,
                    waiting_since,
                    lease.task.id,
                ),
            )
            self._append_event_tx(
                connection,
                lease.task.run_id,
                "task.poisoned" if poison else "task.retry_scheduled",
                {
                    "task_id": lease.task.id,
                    "attempt": lease.attempt_number,
                    "error_type": type(error).__name__,
                    "delay_seconds": delay,
                    "delay_source": (
                        None
                        if failure is None or failure.retry_delay is None
                        else failure.retry_delay.source
                    ),
                },
            )
            return poison

    def recover_expired_leases(
        self, *, active_task_ids: frozenset[str] = frozenset()
    ) -> tuple[int, int]:
        recovered = 0
        poisoned = 0
        now = time.time()
        with self._connection(immediate=True) as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE status=? AND lease_until<?",
                (TaskStatus.LEASED.value, now),
            ).fetchall()
            for row in rows:
                if str(row["id"]) in active_task_ids:
                    continue
                try:
                    self._assert_run_live(str(row["run_id"]), executable=True)
                except (ExpiredRootError, RootUnavailableError):
                    continue
                recorded = connection.execute(
                    "SELECT failure_json, retry_at, dispatched, receipt_id FROM attempts "
                    "WHERE task_id=? AND attempt_number=?",
                    (row["id"], row["attempts"]),
                ).fetchone()
                if recorded is not None and recorded["failure_json"] is None:
                    has_receipt = recorded["receipt_id"] is not None
                    if not has_receipt and self.lifecycle is not None:
                        has_receipt = (
                            self.lifecycle.artifacts.get_receipt(
                                str(row["run_id"]),
                                str(row["id"]),
                                int(row["attempts"]),
                                incarnation=(
                                    0
                                    if (root := self.lifecycle.get(str(row["run_id"]))) is None
                                    else root.incarnation
                                ),
                            )
                            is not None
                        )
                    if has_receipt:
                        connection.execute(
                            "UPDATE tasks SET status='pending', lease_owner=NULL, "
                            "lease_until=NULL, resume_attempt=1 WHERE id=?",
                            (row["id"],),
                        )
                        recovered += 1
                        continue
                    if recorded["dispatched"]:
                        run = connection.execute(
                            "SELECT manifest_json FROM runs WHERE id=?", (row["run_id"],)
                        ).fetchone()
                        started = connection.execute(
                            "SELECT started_at FROM attempts WHERE task_id=? AND attempt_number=?",
                            (row["id"], row["attempts"]),
                        ).fetchone()
                        assert run is not None and started is not None
                        uncertain_failure, telemetry = indeterminate_evidence(
                            RunManifest.model_validate_json(run["manifest_json"], strict=False),
                            str(row["solution_id"]),
                            int(row["attempts"]),
                            datetime.fromisoformat(str(started["started_at"])),
                            utc_now(),
                        )
                        connection.execute(
                            "UPDATE attempts SET failure_json=?, telemetry_json=?, retry_at=? "
                            "WHERE task_id=? AND attempt_number=?",
                            (
                                uncertain_failure.model_dump_json(),
                                telemetry.model_dump_json(),
                                now
                                + (
                                    0
                                    if uncertain_failure.retry_delay is None
                                    else float(uncertain_failure.retry_delay.scheduled_seconds)
                                ),
                                row["id"],
                                row["attempts"],
                            ),
                        )
                        recorded = connection.execute(
                            "SELECT failure_json, retry_at, dispatched, receipt_id FROM attempts "
                            "WHERE task_id=? AND attempt_number=?",
                            (row["id"], row["attempts"]),
                        ).fetchone()
                failure = (
                    ProviderFailureEvidence.model_validate_json(recorded["failure_json"])
                    if recorded is not None and recorded["failure_json"] is not None
                    else None
                )
                retry_at = float(recorded["retry_at"]) if failure and recorded is not None else now
                waiting_since = (
                    retry_at - float(failure.retry_delay.scheduled_seconds)
                    if failure and failure.retry_delay
                    else now
                )
                is_poison = int(row["attempts"]) >= int(row["max_attempts"]) or (
                    failure is not None and not failure.retryable
                )
                status = TaskStatus.POISONED if is_poison else TaskStatus.PENDING
                attempt_status = AttemptStatus.FAILED if is_poison else AttemptStatus.RETRYABLE
                message = failure.message if failure else "worker lease expired"
                indeterminate = (
                    recorded is not None
                    and bool(recorded["dispatched"])
                    and recorded["receipt_id"] is None
                )
                connection.execute(
                    """
                    UPDATE attempts SET status=?, finished_at=COALESCE(finished_at, ?),
                        error_type=?, error_message=?
                    WHERE task_id=? AND attempt_number=?
                    """,
                    (
                        attempt_status.value,
                        utc_now().isoformat(),
                        "IndeterminateProviderOutcome"
                        if indeterminate
                        else "ProviderFailure"
                        if failure
                        else "LeaseExpired",
                        message,
                        row["id"],
                        row["attempts"],
                    ),
                )
                result_json = None
                if is_poison:
                    result_json = TaskResult(
                        task_id=str(row["id"]),
                        sample_id=str(row["sample_id"]),
                        solution_id=str(row["solution_id"]),
                        status=TaskStatus.POISONED,
                        attempts=int(row["attempts"]),
                        error=message,
                    ).model_dump_json()
                connection.execute(
                    """
                    UPDATE tasks SET status=?, lease_owner=NULL, lease_until=NULL, result_json=?,
                        waiting_since=?, next_available=MAX(next_available, ?)
                    WHERE id=?
                    """,
                    (status.value, result_json, waiting_since, retry_at, row["id"]),
                )
                self._append_event_tx(
                    connection,
                    str(row["run_id"]),
                    "task.poisoned" if is_poison else "task.recovered",
                    {"task_id": str(row["id"]), "attempt": int(row["attempts"])},
                )
                if is_poison:
                    poisoned += 1
                else:
                    recovered += 1
        return recovered, poisoned

    def release_unstarted(self, lease: Lease) -> None:
        if (
            self.lifecycle is not None
            and self.lifecycle.artifacts.get_receipt(
                lease.task.run_id,
                lease.task.id,
                lease.attempt_number,
                incarnation=lease.retention_incarnation,
            )
            is not None
        ):
            raise RepositoryError("cannot release an attempt with a durable response checkpoint")
        with self._connection(immediate=True) as connection:
            self._assert_lease(connection, lease)
            recorded = connection.execute(
                """
                SELECT telemetry_json, failure_json, dispatched, receipt_id FROM attempts
                WHERE task_id=? AND attempt_number=?
                """,
                (lease.task.id, lease.attempt_number),
            ).fetchone()
            if (
                recorded is None
                or recorded["telemetry_json"]
                or recorded["failure_json"]
                or recorded["dispatched"]
                or recorded["receipt_id"]
            ):
                raise RepositoryError("cannot release an attempt after provider execution")
            if (
                connection.execute(
                    "SELECT 1 FROM attempt_artifacts WHERE task_id=? AND attempt_number=? LIMIT 1",
                    (lease.task.id, lease.attempt_number),
                ).fetchone()
                is not None
            ):
                raise RepositoryError("cannot release an attempt after evidence persistence")
            connection.execute(
                "DELETE FROM attempts WHERE task_id=? AND attempt_number=?",
                (lease.task.id, lease.attempt_number),
            )
            connection.execute(
                """
                UPDATE tasks SET status=?, attempts=attempts-1, lease_owner=NULL, lease_until=NULL
                WHERE id=?
                """,
                (TaskStatus.PENDING.value, lease.task.id),
            )
            self._append_event_tx(
                connection,
                lease.task.run_id,
                "task.deferred",
                {
                    "task_id": lease.task.id,
                    "reason": "runtime authorization changed before execution",
                },
            )

    def next_work_delay(
        self,
        run_id: str | None = None,
        *,
        allow_networked: bool = True,
        eligible_tasks: EligibleTasks | None = None,
    ) -> float:
        if eligible_tasks is not None and not eligible_tasks:
            return 5.0
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT MIN(
                    CASE WHEN t.status='leased' THEN t.lease_until
                    ELSE MAX(t.next_available, COALESCE(c.until, 0)) END
                ) AS due FROM tasks t
                JOIN runs r ON r.id=t.run_id
                LEFT JOIN provider_cooldowns c ON c.scope=t.throttle_scope
                WHERE t.status IN ('pending', 'leased') AND r.runtime_error IS NULL
                    AND (? IS NULL OR t.run_id=?)
                    AND (? OR json_extract(r.manifest_json, '$.networked_execution')=0)
                    AND (? IS NULL OR EXISTS (
                        SELECT 1 FROM json_each(?) e
                        WHERE json_extract(e.value, '$[0]')=t.run_id
                            AND json_extract(e.value, '$[1]')=t.solution_id
                    ))
                """,
                (
                    run_id,
                    run_id,
                    allow_networked,
                    None if eligible_tasks is None else json.dumps(sorted(eligible_tasks)),
                    None if eligible_tasks is None else json.dumps(sorted(eligible_tasks)),
                ),
            ).fetchone()
        return (
            5.0
            if row is None or row["due"] is None
            else max(0.05, min(5.0, float(row["due"]) - time.time()))
        )

    def acquire_supervisor(self, owner: str, lease_seconds: int = 30) -> bool:
        if not owner or len(owner) > 200 or not 1 <= lease_seconds <= 3600:
            raise ValueError("invalid supervisor lease")
        with self._connection(immediate=True) as connection:
            cursor = connection.execute(
                """
                INSERT INTO worker_supervisor(singleton, owner, lease_until) VALUES (1, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET owner=excluded.owner,
                    lease_until=excluded.lease_until,
                    generation=CASE WHEN owner=excluded.owner AND lease_until>?
                        THEN generation ELSE generation+1 END
                WHERE owner=excluded.owner OR lease_until<?
                """,
                (owner, time.time() + lease_seconds, time.time(), time.time()),
            )
            return cursor.rowcount == 1

    def renew_supervisor(
        self, owner: str, *, generation: int | None = None, lease_seconds: int = 30
    ) -> int | None:
        if not owner or len(owner) > 200 or not 1 <= lease_seconds <= 3600:
            raise ValueError("invalid supervisor renewal")
        with self._connection(immediate=True) as connection:
            row = connection.execute("SELECT * FROM worker_supervisor WHERE singleton=1").fetchone()
            now = time.time()
            if (
                row is None
                or row["owner"] != owner
                or float(row["lease_until"]) <= now
                or (generation is not None and int(row["generation"]) != generation)
            ):
                return None
            connection.execute(
                "UPDATE worker_supervisor SET lease_until=? WHERE singleton=1",
                (now + lease_seconds,),
            )
            return int(row["generation"])

    def release_supervisor(self, owner: str, *, generation: int | None = None) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE worker_supervisor SET lease_until=0 WHERE owner=? "
                "AND (? IS NULL OR generation=?)",
                (owner, generation, generation),
            )

    @staticmethod
    def _cooldown_wait(
        connection: sqlite3.Connection,
        scope: str | None,
        start: float,
        finish: float,
    ) -> float:
        if scope is None or not start:
            return 0
        intervals = connection.execute(
            """
            SELECT started, until FROM cooldown_intervals
            WHERE scope=? AND until>? AND started<? ORDER BY started
            """,
            (scope, start, finish),
        ).fetchall()
        cursor = start
        total = 0.0
        for interval in intervals:
            left, right = (
                max(cursor, float(interval["started"])),
                min(finish, float(interval["until"])),
            )
            if right > left:
                total += right - left
                cursor = right
        return total * 1000

    def acquire_finalizer(self, run_id: str) -> int | None:
        with self._connection(immediate=True) as connection:
            self._assert_run_live(run_id, executable=True)
            run = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if run is None:
                raise RepositoryError(f"unknown run: {run_id}")
            if RunStatus(str(run["status"])) not in {RunStatus.RUNNING, RunStatus.CREATED}:
                return None
            unfinished = connection.execute(
                "SELECT COUNT(*) AS count FROM tasks WHERE run_id=? AND status IN (?, ?)",
                (run_id, TaskStatus.PENDING.value, TaskStatus.LEASED.value),
            ).fetchone()
            assert unfinished is not None
            if int(unfinished["count"]) != 0:
                return None
            manifest = RunManifest.model_validate_json(run["manifest_json"], strict=False)
            count = connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            if count != len(manifest.sample_ids) * len(manifest.solution_ids):
                return None
            clock = connection.execute(
                "SELECT generation FROM finalizer_clock WHERE singleton=1"
            ).fetchone()
            assert clock is not None
            token = max(int(clock["generation"]), int(run["finalizer_token"])) + 1
            connection.execute(
                "UPDATE finalizer_clock SET generation=? WHERE singleton=1", (token,)
            )
            supervisor = connection.execute(
                "SELECT generation FROM worker_supervisor WHERE singleton=1 AND lease_until>?",
                (time.time(),),
            ).fetchone()
            generation = 0 if supervisor is None else int(supervisor["generation"])
            connection.execute(
                "UPDATE runs SET finalizer_token=?, finalizer_generation=? WHERE id=?",
                (token, generation, run_id),
            )
            self._append_event_tx(connection, run_id, "run.finalizer_acquired", {"token": token})
            return token

    def finalize_run(
        self, run_id: str, token: int, status: RunStatus, report_artifact_id: str
    ) -> None:
        if status not in {RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_FAILURES, RunStatus.FAILED}:
            raise ValueError("invalid final run status")
        with self._connection(immediate=True) as connection:
            self._assert_run_live(run_id, executable=True)
            run = connection.execute(
                "SELECT finalizer_generation FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if run is not None and int(run["finalizer_generation"]):
                supervisor = connection.execute(
                    "SELECT generation FROM worker_supervisor WHERE singleton=1 AND lease_until>?",
                    (time.time(),),
                ).fetchone()
                if supervisor is None or int(supervisor["generation"]) != int(
                    run["finalizer_generation"]
                ):
                    raise FinalizerFenceError(run_id)
            cursor = connection.execute(
                """
                UPDATE runs SET status=?, report_artifact_id=?, runtime_error=NULL
                WHERE id=? AND finalizer_token=? AND status IN (?, ?)
                """,
                (
                    status.value,
                    report_artifact_id,
                    run_id,
                    token,
                    RunStatus.RUNNING.value,
                    RunStatus.CREATED.value,
                ),
            )
            if cursor.rowcount != 1:
                raise FinalizerFenceError(run_id)
            self._append_event_tx(
                connection,
                run_id,
                "run.finalized",
                {"status": status.value, "report_artifact_id": report_artifact_id},
            )

    def append_event(self, run_id: str, event_type: str, payload: dict[str, object]) -> int:
        with self._connection(immediate=True) as connection:
            self._assert_run_live(run_id)
            return self._append_event_tx(connection, run_id, event_type, payload)

    def list_events(self, run_id: str, after: int = 0, limit: int = 1000) -> Sequence[RunEvent]:
        self._assert_run_live(run_id)
        bounded = max(1, min(limit, 5000))
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM events WHERE run_id=? AND sequence>?
                ORDER BY sequence LIMIT ?
                """,
                (run_id, max(0, after), bounded),
            ).fetchall()
        return tuple(
            RunEvent(
                sequence=int(row["sequence"]),
                run_id=str(row["run_id"]),
                event_type=str(row["event_type"]),
                occurred_at=datetime.fromisoformat(str(row["occurred_at"])),
                payload=json.loads(row["payload_json"]),
            )
            for row in rows
        )

    def _append_event_tx(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        event_type: str,
        payload: dict[str, object],
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO events(run_id, event_type, occurred_at, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                run_id,
                event_type,
                utc_now().isoformat(),
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
            ),
        )
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)

    def _assert_lease(self, connection: sqlite3.Connection, lease: Lease) -> sqlite3.Row:
        self._assert_run_live(lease.task.run_id, executable=True)
        if self.lifecycle is not None and lease.retention_generation:
            root = self.lifecycle.get(lease.task.run_id)
            if (
                root is None
                or root.generation != lease.retention_generation
                or root.incarnation != lease.retention_incarnation
            ):
                raise LeaseLostError("retention generation changed")
        row = connection.execute("SELECT * FROM tasks WHERE id=?", (lease.task.id,)).fetchone()
        if (
            row is None
            or row["status"] != TaskStatus.LEASED.value
            or row["lease_owner"] != lease.owner
            or int(row["lease_token"]) != lease.token
            or int(row["attempts"]) != lease.attempt_number
            or row["run_id"] != lease.task.run_id
            or row["sample_id"] != lease.task.sample_id
            or row["solution_id"] != lease.task.solution_id
            or row["lease_until"] is None
            or float(row["lease_until"]) <= time.time()
        ):
            raise LeaseLostError(lease.task.id)
        if lease.supervisor_generation:
            supervisor = connection.execute(
                "SELECT * FROM worker_supervisor WHERE singleton=1"
            ).fetchone()
            if (
                supervisor is None
                or supervisor["owner"] != lease.owner
                or int(supervisor["generation"]) != lease.supervisor_generation
                or float(supervisor["lease_until"]) <= time.time()
            ):
                raise LeaseLostError("supervisor generation changed")
        return cast(sqlite3.Row, row)

    def cleanup_records(self, root: LifecycleRoot) -> bool:
        if root.kind != "run":
            return True
        if root.pinned or root.state != "deleting":
            raise RepositoryError("cleanup requires a fenced, unpinned expired root")
        with self._connection(immediate=True) as connection:
            current = connection.execute(
                "SELECT payload FROM coordination_rows WHERE row_key=?", (root_key(root.id),)
            ).fetchone()
            if current is None:
                return True
            retained = LifecycleRoot.model_validate_json(current["payload"])
            if (
                retained.generation != root.generation
                or retained.state != "deleting"
                or retained.pinned
            ):
                return False
            task_ids = [
                str(row[0])
                for row in connection.execute(
                    "SELECT id FROM tasks WHERE run_id=? LIMIT 40", (root.id,)
                ).fetchall()
            ]
            for task_id in task_ids:
                connection.execute("DELETE FROM attempt_artifacts WHERE task_id=?", (task_id,))
                connection.execute("DELETE FROM attempts WHERE task_id=?", (task_id,))
                connection.execute("DELETE FROM cooldown_intervals WHERE task_id=?", (task_id,))
                connection.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            if (
                connection.execute(
                    "SELECT 1 FROM tasks WHERE run_id=? LIMIT 1", (root.id,)
                ).fetchone()
                is not None
            ):
                return False
            connection.execute("DELETE FROM events WHERE run_id=?", (root.id,))
            connection.execute("DELETE FROM launch_requests WHERE run_id=?", (root.id,))
            connection.execute("DELETE FROM runs WHERE id=?", (root.id,))
        return True

    def has_legacy_artifact_owner(self, artifact_id: str) -> bool:
        with self._connection() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM legacy_artifact_owners WHERE artifact_id=?", (artifact_id,)
                ).fetchone()
                is not None
            )

    def delete_artifact_registration(self, artifact_id: str, generation: int) -> None:
        with self._connection(immediate=True) as connection:
            row = connection.execute(
                "SELECT payload FROM coordination_rows WHERE row_key=?",
                (f"object:{artifact_id}",),
            ).fetchone()
            if row is None:
                raise RepositoryError("artifact deletion generation disappeared")
            state = json.loads(row["payload"])
            if state.get("state") != "deleting" or state.get("generation") != generation:
                raise RepositoryError("artifact deletion generation changed")
            connection.execute("DELETE FROM artifact_variants WHERE id=?", (artifact_id,))
            connection.execute("DELETE FROM artifacts WHERE id=?", (artifact_id,))

    @staticmethod
    def _run_record(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            manifest=RunManifest.model_validate_json(row["manifest_json"], strict=False),
            manifest_artifact_id=str(row["manifest_artifact_id"]),
            status=RunStatus(str(row["status"])),
            report_artifact_id=(
                None if row["report_artifact_id"] is None else str(row["report_artifact_id"])
            ),
            finalizer_token=int(row["finalizer_token"]),
            runtime_error=None if row["runtime_error"] is None else str(row["runtime_error"]),
        )

    @staticmethod
    def _task_record(row: sqlite3.Row) -> TaskRecord:
        columns = set(row.keys())
        return TaskRecord(
            id=str(row["id"]),
            run_id=str(row["run_id"]),
            sample_id=str(row["sample_id"]),
            solution_id=str(row["solution_id"]),
            status=TaskStatus(str(row["status"])),
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            lease_owner=None if row["lease_owner"] is None else str(row["lease_owner"]),
            lease_token=int(row["lease_token"]),
            result=(
                None
                if row["result_json"] is None
                else TaskResult.model_validate_json(row["result_json"], strict=False)
            ),
            throttle_scope=None if row["throttle_scope"] is None else str(row["throttle_scope"]),
            next_available_at=datetime.fromtimestamp(float(row["next_available"]), UTC),
            cooldown_until=(
                None
                if "cooldown_until" not in columns or row["cooldown_until"] is None
                else datetime.fromtimestamp(float(row["cooldown_until"]), UTC)
            ),
        )


AzureRepositories = AzureStorageRepositories
