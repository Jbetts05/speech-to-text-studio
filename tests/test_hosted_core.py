from __future__ import annotations

from collections.abc import Iterator, Sequence
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from ai_bench.artifacts import ArtifactStore, FileArtifactStore
from ai_bench.azure_artifacts import AzureBlobArtifactStore
from ai_bench.backend_proof import REQUIRED_SCENARIOS
from ai_bench.backends import BackendBundle
from ai_bench.conformance import run_conformance
from ai_bench.contracts import RunStatus, SampleDescriptor, SampleSnapshot, utc_now
from ai_bench.datasets import DatasetCatalog
from ai_bench.engine import BenchmarkEngine
from ai_bench.hashing import sha256_digest
from ai_bench.lifecycle import ExpiredRootError, LifecycleService
from ai_bench.repositories import LeaseLostError
from ai_bench.storage_state import (
    AzureTableStateStore,
    PrefixedStateStore,
    SqliteStateStore,
    StateChange,
    StateConflictError,
    StateStore,
)
from ai_bench.table_repository import AzureTableRepository

if TYPE_CHECKING:
    from tests.test_core_blob import FakeContainer


class TableError(Exception):
    def __init__(self, status: int) -> None:
        self.status_code = status


class TableEntity(dict[str, Any]):
    def __init__(self, values: dict[str, str]) -> None:
        super().__init__(values)
        self._metadata: dict[str, Any] = {}

    @property
    def metadata(self) -> dict[str, Any]:
        return self._metadata


class FakeTable:
    def __init__(self) -> None:
        self.rows: dict[str, TableEntity] = {}
        self.lose_response = False
        self.batch_sizes: list[int] = []
        self.blob_containers: dict[str, FakeContainer] = {}
        self._lock = RLock()

    def get_entity(self, *, partition_key: str, row_key: str) -> TableEntity:
        with self._lock:
            if row_key not in self.rows:
                raise TableError(404)
            return deepcopy(self.rows[row_key])

    def query_entities(self, **kwargs: Any) -> Iterator[TableEntity]:
        parameters = kwargs["parameters"]
        with self._lock:
            rows = [
                deepcopy(self.rows[key])
                for key in sorted(self.rows)
                if parameters["prefix"] <= key < parameters["end"] and key > parameters["after"]
            ]
        yield from rows

    def submit_transaction(self, operations: list[Any]) -> None:
        # Model the service's atomic ETag check-and-commit, including concurrent
        # callers; copy-on-write without this lock loses unrelated transactions.
        with self._lock:
            self._submit_transaction(operations)

    def _submit_transaction(self, operations: list[Any]) -> None:
        assert len(operations) <= 100
        assert len({operation[1]["PartitionKey"] for operation in operations}) == 1
        self.batch_sizes.append(len(operations))
        replacement = self.rows.copy()
        for operation in operations:
            action, entity = operation[:2]
            key = entity["RowKey"]
            current = replacement.get(key)
            if action == "create":
                if current is not None:
                    raise TableError(409)
            else:
                assert len(operation) == 3
                assert operation[2]["etag"] != "*"
                if current is None or current.metadata["etag"] != operation[2]["etag"]:
                    raise TableError(412)
            if action == "delete":
                del replacement[key]
            else:
                row = TableEntity(entity)
                row._metadata = {"etag": uuid4().hex, "timestamp": utc_now()}
                replacement[key] = row
        self.rows = replacement
        if self.lose_response:
            self.lose_response = False
            raise TimeoutError("response lost after commit")


@pytest.fixture(params=["sqlite", "table"])
def state(request: pytest.FixtureRequest, tmp_path: Path) -> StateStore:
    if request.param == "sqlite":
        return SqliteStateStore(tmp_path / "coordination.sqlite3")
    return AzureTableStateStore(FakeTable(), "synthetic-test", match_condition="IfNotModified")


def engine_for(state: StateStore, root: Path, pack_root: Path) -> BenchmarkEngine:
    artifacts: ArtifactStore
    staging: ArtifactStore
    if isinstance(state, AzureTableStateStore):
        from tests.test_core_blob import FakeContainer

        assert isinstance(state.client, FakeTable)
        containers = state.client.blob_containers
        object_container = containers.setdefault(f"{root.resolve()}:objects", FakeContainer())
        staging_container = containers.setdefault(f"{root.resolve()}:staging", FakeContainer())
        artifacts = AzureBlobArtifactStore(
            object_container, state, root / "cache", match_condition="IfNotModified"
        )
        staging = AzureBlobArtifactStore(
            staging_container,
            PrefixedStateStore(state, "staging:"),
            root / "staging-cache",
            match_condition="IfNotModified",
        )
    else:
        artifacts = FileArtifactStore(root / "objects")
        staging = FileArtifactStore(root / "staging")
    lifecycle = LifecycleService(state, artifacts)
    repository = AzureTableRepository(state, artifacts, lifecycle)
    bundle = BackendBundle(
        repository,
        artifacts,
        state,
        lifecycle,
        DatasetCatalog(state, artifacts, lifecycle),
        "local-durable",
        staging=staging,
    )
    return BenchmarkEngine(root, pack_root, backend=bundle)


def test_cas_requires_exact_generation(state: StateStore) -> None:
    state.transact((StateChange("one", {"value": 1}), StateChange("two", {"value": 2})))
    old = state.get("one")
    assert old is not None
    state.transact((StateChange("one", {"value": 3}, old.etag),))
    with pytest.raises(StateConflictError):
        state.transact((StateChange("one", {"value": 4}, old.etag), StateChange("three", {})))
    assert state.get("three") is None
    assert state.get("one") is not None
    assert state.page("", limit=1).cursor == "one"


def test_table_reconciles_lost_success_without_resending() -> None:
    client = FakeTable()
    store = AzureTableStateStore(client, "synthetic-test", match_condition="IfNotModified")
    client.lose_response = True
    store.transact((StateChange("one", {"value": "accepted"}),))
    assert client.batch_sizes == [1]
    assert store.get("one") is not None


def test_table_rejects_oversized_or_duplicate_batches(state: StateStore) -> None:
    with pytest.raises(ValueError, match="1-100"):
        state.transact(tuple(StateChange(f"key:{index}", {}) for index in range(101)))
    with pytest.raises(ValueError, match="once"):
        state.transact((StateChange("one", {}), StateChange("one", {})))
    with pytest.raises(ValueError, match="immutable payloads"):
        state.transact((StateChange("one", {"large": "x" * 40_000}),))


def test_durable_projection_engine_completes_offline(
    state: StateStore, tmp_path: Path, pack_root: Path
) -> None:
    engine = engine_for(state, tmp_path / "runtime", pack_root)
    manifest = engine.create_run(
        "synthetic_text_intent",
        solution_ids=("keyword-standalone",),
        idempotency_key="durable-test",
    )
    replay = engine.create_run(
        "synthetic_text_intent",
        solution_ids=("keyword-standalone",),
        idempotency_key="durable-test",
    )
    assert replay == manifest
    report = engine.run_to_completion(manifest.run_id)
    assert report.status is RunStatus.COMPLETED
    assert len(report.results) == len(manifest.sample_ids)
    events = engine.repository.list_events(manifest.run_id)
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert sum(event.event_type == "run.finalized" for event in events) == 1
    restarted = engine_for(state, tmp_path / "runtime", pack_root)
    assert restarted.get_report(manifest.run_id) == report


def test_supervisor_takeover_fences_old_task(
    state: StateStore, tmp_path: Path, pack_root: Path
) -> None:
    engine = engine_for(state, tmp_path / "runtime", pack_root)
    manifest = engine.create_run("synthetic_text_intent", solution_ids=("keyword-standalone",))
    repository = engine.repository
    assert isinstance(repository, AzureTableRepository)
    now = [utc_now()]
    repository.clock = lambda: now[0]
    first = repository.claim_task("first", lease_seconds=60)
    assert first is not None
    now[0] += timedelta(seconds=31)
    assert repository.acquire_supervisor("second")
    with pytest.raises(LeaseLostError):
        repository.renew_lease(first)
    with pytest.raises(LeaseLostError):
        repository.fail_attempt(first, RuntimeError("stale"))
    assert engine.repository.get_run(manifest.run_id) is not None


def test_ten_day_expiry_denies_reads_before_reaping(
    state: StateStore, tmp_path: Path, pack_root: Path
) -> None:
    engine = engine_for(state, tmp_path / "runtime", pack_root)
    manifest = engine.create_run("synthetic_text_intent", solution_ids=("keyword-standalone",))
    root = engine.lifecycle.get(manifest.run_id)
    assert root is not None
    assert root.expires_at == manifest.created_at + timedelta(days=10)
    engine.lifecycle.clock = lambda: manifest.created_at + timedelta(days=10)
    with pytest.raises(ExpiredRootError):
        engine.repository.get_run(manifest.run_id)
    assert not engine.repository.list_runs()
    assert engine.repository.claim_task("worker") is None


def test_shared_operator_conformance_scenarios(
    state: StateStore, tmp_path: Path, pack_root: Path
) -> None:
    result = run_conformance(
        engine_for(state, tmp_path / "operator", pack_root),
        benchmark_id="synthetic_text_intent",
        solution_id="keyword-standalone",
    )
    assert REQUIRED_SCENARIOS - {"ambiguous-response-reconciliation"} <= set(result.scenarios)
    assert result.provider_calls == 0
    assert not result.live_promotion


def test_large_task_matrix_remains_hidden_until_crash_repair(
    state: StateStore, tmp_path: Path, pack_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = engine_for(state, tmp_path / "large", pack_root)
    original = engine.create_run("synthetic_text_intent", solution_ids=("keyword-standalone",))
    samples = tuple(
        engine._samples_for_manifest(original)[0].model_copy(update={"id": f"case-{index:03d}"})
        for index in range(130)
    )
    revision = original.dataset_revision.model_copy(update={"content_hash": sha256_digest(samples)})
    snapshot = engine.artifacts.put_json(
        SampleSnapshot(
            dataset_revision=revision,
            samples=samples,
        )
    )
    descriptors = tuple(
        SampleDescriptor(
            id=sample.id,
            role=sample.role,
            input_artifact_id=sample.input.id,
            reference_artifact_id=None if sample.reference is None else sample.reference.id,
        )
        for sample in samples
    )
    manifest = original.model_copy(
        update={
            "run_id": sha256_digest({"large": original.run_id}),
            "dataset_revision": revision,
            "samples": descriptors,
            "sample_ids": tuple(sample.id for sample in samples),
            "sample_snapshot_artifact_id": snapshot.id,
            "descriptor_hashes": {
                **original.descriptor_hashes,
                "dataset": sha256_digest({"revision": revision, "samples": descriptors}),
                "sample-snapshot": snapshot.id,
            },
        }
    )
    artifact = engine.artifacts.put_json(manifest)
    engine.repository.create_run(manifest, artifact.id)
    transact = state.transact
    task_batches = [0]

    def interrupted(changes: Sequence[StateChange]) -> None:
        if any(change.key.startswith("task:") and change.etag is None for change in changes):
            task_batches[0] += 1
            if task_batches[0] == 2:
                raise RuntimeError("synthetic initialization crash")
        transact(changes)

    monkeypatch.setattr(state, "transact", interrupted)
    with pytest.raises(RuntimeError, match="initialization crash"):
        engine.repair_initialization(manifest)
    assert len(engine.repository.list_tasks(manifest.run_id)) == 24
    assert engine.repository.claim_task("worker", run_id=manifest.run_id) is None
    assert engine.repository.acquire_finalizer(manifest.run_id) is None
    monkeypatch.setattr(state, "transact", transact)
    engine.repair_initialization(manifest)
    assert len(engine.repository.list_tasks(manifest.run_id)) == 130
    assert engine.repository.claim_task("worker", run_id=manifest.run_id) is not None
    assert len(engine.repository.list_events(manifest.run_id)) == 132
