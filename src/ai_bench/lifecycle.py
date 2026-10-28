"""Ten-day logical roots and generation-fenced shared-object collection."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Literal

from ai_bench.artifact_inventory import InventoryEntry, parse_inventory_entry
from ai_bench.artifacts import ArtifactStore, FileArtifactStore
from ai_bench.azure_artifacts import AzureBlobArtifactStore
from ai_bench.contracts import Contract, JsonValue, utc_now
from ai_bench.hashing import sha256_digest
from ai_bench.storage_state import (
    PrefixedStateStore,
    SqliteStateStore,
    StateChange,
    StateConflictError,
    StateRow,
    StateStore,
)

RETENTION = timedelta(days=10)
STAGING_RETENTION = timedelta(hours=24)
UPLOAD_SESSION_RETENTION = timedelta(hours=1)


class ExpiredRootError(LookupError):
    pass


class RootUnavailableError(LookupError):
    pass


class ObjectDeletionInProgressError(RuntimeError):
    pass


class LifecycleRoot(Contract):
    id: str
    kind: Literal["dataset", "run", "upload", "import"]
    created_at: datetime
    expires_at: datetime | None
    pinned: bool = False
    archival: bool = False
    state: Literal["staging", "ready", "deleting"] = "staging"
    generation: int = 1
    incarnation: int = 0
    parent_id: str | None = None


class LifecycleHealth(Contract):
    last_successful_sweep: datetime | None
    lag_seconds: float
    error: str | None


def root_key(identifier: str) -> str:
    return f"root:{sha256_digest(identifier)}"


def _expiry_key(root: LifecycleRoot) -> str:
    assert root.expires_at is not None
    return f"expiry:{root.expires_at.timestamp():020.6f}:{sha256_digest(root.id)}"


class LifecycleService:
    def __init__(
        self,
        state: StateStore,
        artifacts: ArtifactStore,
        *,
        clock: Callable[[], datetime] = utc_now,
        descriptor_cleanup: Callable[[str, int], None] | None = None,
        external_owner: Callable[[str], bool] | None = None,
        external_root: Callable[[str], bool] | None = None,
    ) -> None:
        self.state = state
        self.artifacts = artifacts
        self.clock = clock
        self.descriptor_cleanup = descriptor_cleanup
        self.external_owner = external_owner
        self.external_root = external_root
        self.last_successful_sweep: datetime | None = None
        self.reaper_error: str | None = None
        self._attach_lock = RLock()

    def health(self) -> LifecycleHealth:
        lag = 0.0
        due = self.state.page("expiry:", limit=1)
        if due.rows:
            root = self.get(str(due.rows[0].values["root_id"]))
            if root is not None and root.expires_at is not None:
                lag = max(0.0, (self.clock() - root.expires_at).total_seconds())
        return LifecycleHealth(
            last_successful_sweep=self.last_successful_sweep,
            lag_seconds=lag,
            error=self.reaper_error,
        )

    def get(self, identifier: str) -> LifecycleRoot | None:
        row = self.state.get(root_key(identifier))
        return None if row is None else LifecycleRoot.model_validate(row.values, strict=False)

    def is_expired(self, identifier: str) -> bool:
        """Unknown roots are not treated as live, and no expiry tombstone is required."""
        root = self.get(identifier)
        if root is None:
            raise RootUnavailableError(identifier)
        return root.state == "deleting" or (
            root.expires_at is not None and self.clock() >= root.expires_at
        )

    def require_live(self, identifier: str, *, ready: bool = True) -> LifecycleRoot:
        root = self.get(identifier)
        if root is None:
            raise RootUnavailableError(identifier)
        if root.state == "deleting" or (
            root.expires_at is not None and self.clock() >= root.expires_at
        ):
            raise ExpiredRootError(identifier)
        if ready and root.state != "ready":
            raise RootUnavailableError(identifier)
        return root

    def create(
        self,
        identifier: str,
        kind: Literal["dataset", "run", "upload", "import"],
        *,
        parent_id: str | None = None,
        accepted_at: datetime | None = None,
        staging_retention: bool = False,
        expires_not_after: datetime | None = None,
    ) -> LifecycleRoot:
        existing = self.get(identifier)
        if existing is not None:
            if existing.kind != kind or existing.parent_id != parent_id:
                raise ValueError("lifecycle identity conflict")
            return self.require_live(identifier, ready=False)
        now = accepted_at or self.clock()
        deadline = now + (
            UPLOAD_SESSION_RETENTION
            if kind == "upload"
            else STAGING_RETENTION
            if staging_retention or kind in {"import", "dataset"}
            else RETENTION
        )
        if expires_not_after is not None:
            deadline = min(deadline, self.as_utc(expires_not_after))
        parent_row = None
        if parent_id is not None:
            parent = self.require_live(parent_id)
            parent_row = self.state.get(root_key(parent_id))
            if parent.expires_at is not None:
                deadline = min(deadline, parent.expires_at)
        generation, generation_change = self._root_generation()
        root = LifecycleRoot(
            id=identifier,
            kind=kind,
            created_at=now,
            expires_at=deadline,
            parent_id=parent_id,
            generation=generation,
            incarnation=generation,
        )
        changes = [
            generation_change,
            StateChange(root_key(identifier), root.model_dump(mode="json")),
            StateChange(_expiry_key(root), {"root_id": identifier}),
        ]
        if parent_row is not None:
            changes.append(StateChange(parent_row.key, parent_row.values, parent_row.etag))
        try:
            self.state.transact(changes)
        except StateConflictError:
            return self.create(
                identifier,
                kind,
                parent_id=parent_id,
                accepted_at=accepted_at,
                staging_retention=staging_retention,
                expires_not_after=expires_not_after,
            )
        return root

    def publish(self, identifier: str) -> LifecycleRoot:
        for _ in range(20):
            row = self.state.get(root_key(identifier))
            root = self.require_live(identifier, ready=False)
            assert row is not None
            if root.state == "ready":
                return root
            generation, generation_change = self._root_generation()
            published = root.model_copy(update={"state": "ready", "generation": generation})
            try:
                self.state.transact(
                    (
                        StateChange(row.key, published.model_dump(mode="json"), row.etag),
                        generation_change,
                    )
                )
                return published
            except StateConflictError:
                continue
        raise StateConflictError("publication contention")

    def _root_generation(self, count: int = 1) -> tuple[int, StateChange]:
        row = self.state.get("retention-generation")
        generation = 1 if row is None else int(str(row.values["generation"])) + 1
        return generation, StateChange(
            "retention-generation",
            {"generation": generation + count - 1},
            None if row is None else row.etag,
        )

    def cleanup_guard(self, root: LifecycleRoot) -> StateChange | None:
        row = self.state.get(root_key(root.id))
        if row is None:
            return None
        current = LifecycleRoot.model_validate(row.values, strict=False)
        if current.generation != root.generation or current.state != "deleting" or current.pinned:
            return None
        return StateChange(row.key, row.values, row.etag)

    def attach(self, identifier: str, artifact_id: str) -> None:
        # Local reference writers share generation rows. Serialize only this
        # metadata transition; distributed fences still reject external races.
        with self._attach_lock:
            self._attach(identifier, artifact_id)

    def _attach(self, identifier: str, artifact_id: str) -> None:
        for _ in range(20):
            root_row = self.state.get(root_key(identifier))
            self.require_live(identifier, ready=False)
            assert root_row is not None
            object_key = f"object:{artifact_id}"
            object_row = self.state.get(object_key)
            if object_row is not None and object_row.values.get("state") == "deleting":
                raise ObjectDeletionInProgressError(artifact_id)
            owner_key = f"owner:{artifact_id}:{sha256_digest(identifier)}"
            owner = self.state.get(owner_key)
            if owner is not None:
                return
            generation, generation_change = self._object_generation()
            changes = [
                generation_change,
                StateChange(root_row.key, root_row.values, root_row.etag),
                StateChange(
                    object_key,
                    {"state": "live", "generation": generation},
                    None if object_row is None else object_row.etag,
                ),
                StateChange(owner_key, {"root_id": identifier, "artifact_id": artifact_id}),
                StateChange(
                    f"root-object:{sha256_digest(identifier)}:{artifact_id}",
                    {"artifact_id": artifact_id},
                ),
            ]
            try:
                self.state.transact(changes)
                return
            except StateConflictError:
                continue
        raise StateConflictError("object ownership contention")

    def _object_generation(self) -> tuple[int, StateChange]:
        row = self.state.get("object-generation")
        generation = 1 if row is None else int(str(row.values["generation"])) + 1
        return generation, StateChange(
            "object-generation", {"generation": generation}, None if row is None else row.etag
        )

    def artifact_has_live_owner(self, artifact_id: str) -> bool:
        if self.external_owner is not None and self.external_owner(artifact_id):
            return True
        cursor = None
        while True:
            page = self.state.page(f"owner:{artifact_id}:", after=cursor)
            for row in page.rows:
                try:
                    self.require_live(str(row.values["root_id"]))
                    return True
                except (ExpiredRootError, RootUnavailableError):
                    pass
            cursor = page.cursor
            if cursor is None:
                return False

    def pin_archive(self, dataset_id: str, run_id: str) -> None:
        """Operator-only atomic pin; callers must validate the archive graph first."""
        generation, generation_change = self._root_generation(2)
        changes = [generation_change]
        for offset, (identifier, kind) in enumerate(((dataset_id, "dataset"), (run_id, "run"))):
            row = self.state.get(root_key(identifier))
            root = self.require_live(identifier, ready=False)
            assert row is not None
            if root.kind != kind:
                raise ValueError("archive root kind mismatch")
            if root.pinned:
                continue
            if root.expires_at is not None:
                expiry = self.state.get(_expiry_key(root))
                if expiry is not None:
                    changes.append(StateChange(expiry.key, None, expiry.etag))
            pinned = root.model_copy(
                update={
                    "pinned": True,
                    "archival": kind == "run",
                    "expires_at": None,
                    "state": "ready",
                    "generation": generation + offset,
                }
            )
            changes.append(StateChange(row.key, pinned.model_dump(mode="json"), row.etag))
        if changes:
            self.state.transact(changes)

    def reap(
        self,
        cleanup_records: Callable[[LifecycleRoot], bool],
        *,
        limit: int = 10,
    ) -> int:
        try:
            completed = self._reap(cleanup_records, limit=limit)
            self._reconcile_inventory(limit=min(40, max(1, limit * 4)))
        except Exception as error:
            self.reaper_error = type(error).__name__
            raise
        self.last_successful_sweep = self.clock()
        self.reaper_error = None
        return completed

    def retain_system_object(self, artifact_id: str) -> None:
        """Protect installed catalog configuration, never public upload content."""
        for _ in range(20):
            key = f"system-object:{artifact_id}"
            if self.state.get(key) is not None:
                return
            object_key = f"object:{artifact_id}"
            existing = self.state.get(object_key)
            if existing is not None and existing.values.get("state") == "deleting":
                raise ObjectDeletionInProgressError(artifact_id)
            generation, generation_change = self._object_generation()
            try:
                self.state.transact(
                    (
                        generation_change,
                        StateChange(key, {"kind": "installed-catalog"}),
                        StateChange(
                            object_key,
                            {"state": "live", "generation": generation},
                            None if existing is None else existing.etag,
                        ),
                    )
                )
                return
            except StateConflictError:
                continue
        raise StateConflictError("system object retention contention")

    def _object_is_owned(self, artifact_id: str) -> bool:
        return (
            bool(self.state.page(f"owner:{artifact_id}:", limit=1).rows)
            or self.state.get(f"system-object:{artifact_id}") is not None
            or (self.external_owner is not None and self.external_owner(artifact_id))
        )

    def _reconcile_inventory(self, *, limit: int) -> None:
        # Physical inventory remains authoritative for late writes after all root
        # indexes have gone. Never depend solely on a pre-write liveness check.
        for row in self.state.page("orphan:", limit=limit).rows:
            self._collect_orphan(str(row.values["artifact_id"]))
        cursor_row = self.state.get("inventory-cursor")
        cursor = None if cursor_row is None else cursor_row.values.get("cursor")
        if cursor is not None and not isinstance(cursor, str):
            raise ValueError("invalid artifact inventory cursor")
        page = self.artifacts.inventory_page(cursor=cursor, limit=limit)
        for entry in page.entries:
            if entry.artifact_id is not None:
                if not self._object_is_owned(entry.artifact_id):
                    self._collect_orphan(entry.artifact_id)
            elif entry.run_id is not None:
                root = self.get(entry.run_id)
                if root is not None:
                    if root.incarnation == entry.incarnation and root.state != "deleting":
                        continue
                elif self.external_root is not None and self.external_root(entry.run_id):
                    continue
                self.artifacts.delete_inventory_entry(entry)
        with suppress(StateConflictError):
            self.state.transact(
                (
                    StateChange(
                        "inventory-cursor",
                        {"cursor": page.cursor},
                        None if cursor_row is None else cursor_row.etag,
                    ),
                )
            )

    def _collect_orphan(self, artifact_id: str) -> None:
        object_key = f"object:{artifact_id}"
        work_key = f"orphan:{artifact_id}"
        row = self.state.get(object_key)
        work = self.state.get(work_key)
        if self._object_is_owned(artifact_id):
            return
        if row is not None and row.values.get("state") == "deleting" and work is None:
            return
        if work is None:
            snapshot = self.artifacts.deletion_snapshot(artifact_id)
            generation, generation_change = self._object_generation()
            values: dict[str, JsonValue] = {"state": "deleting", "generation": generation}
            expected_generation = generation
            try:
                self.state.transact(
                    (
                        generation_change,
                        StateChange(object_key, values, None if row is None else row.etag),
                        StateChange(
                            work_key,
                            {
                                "artifact_id": artifact_id,
                                "generation": generation,
                                "targets": self._snapshot_values(snapshot),
                            },
                        ),
                    )
                )
            except StateConflictError:
                return
            row = self.state.get(object_key)
            work = self.state.get(work_key)
        else:
            expected_generation = int(str(work.values["generation"]))
        if row is None or work is None or row.values.get("state") != "deleting":
            return
        generation = int(str(row.values["generation"]))
        if generation != expected_generation or work.values.get("generation") != generation:
            return
        targets = self._snapshot_entries(work.values.get("targets"), artifact_id)
        if isinstance(self.artifacts, FileArtifactStore):
            if not isinstance(self.state, SqliteStateStore | PrefixedStateStore):
                raise TypeError("filesystem collection requires local SQLite coordination")
            try:
                self.state.run_fenced(
                    row.key, row.etag, lambda: self.artifacts.delete_snapshot(targets)
                )
            except StateConflictError:
                return
        else:
            self.artifacts.delete_snapshot(targets)
        remaining = self.artifacts.deletion_snapshot(artifact_id)
        if remaining:
            with suppress(StateConflictError):
                self.state.transact(
                    (
                        StateChange(row.key, row.values, row.etag),
                        StateChange(
                            work.key,
                            {**work.values, "targets": self._snapshot_values(remaining)},
                            work.etag,
                        ),
                    )
                )
            return
        descriptors = self.state.page(f"descriptor:{artifact_id}:", limit=90)
        if self.descriptor_cleanup is not None:
            self.descriptor_cleanup(artifact_id, generation)
        keep = descriptors.cursor is not None
        with suppress(StateConflictError):
            self.state.transact(
                (
                    *(StateChange(item.key, None, item.etag) for item in descriptors.rows),
                    StateChange(row.key, row.values if keep else None, row.etag),
                    StateChange(work.key, work.values if keep else None, work.etag),
                )
            )

    @staticmethod
    def _snapshot_values(entries: tuple[InventoryEntry, ...]) -> list[JsonValue]:
        return [{"key": entry.key, "etag": entry.etag} for entry in entries]

    @staticmethod
    def _snapshot_entries(value: JsonValue | None, artifact_id: str) -> tuple[InventoryEntry, ...]:
        if not isinstance(value, list) or len(value) > 40:
            raise ValueError("invalid orphan deletion targets")
        entries = []
        for item in value:
            if not isinstance(item, dict):
                raise ValueError("invalid orphan deletion target")
            key, etag = item.get("key"), item.get("etag")
            if not isinstance(key, str) or not isinstance(etag, str):
                raise ValueError("invalid orphan deletion identity")
            entry = parse_inventory_entry(key, etag)
            if entry is None or entry.artifact_id != artifact_id:
                raise ValueError("orphan deletion escaped its artifact")
            entries.append(entry)
        return tuple(entries)

    def _reap(
        self,
        cleanup_records: Callable[[LifecycleRoot], bool],
        *,
        limit: int = 10,
    ) -> int:
        """One bounded sweep. A false callback leaves the root resumably fenced."""
        completed = 0
        page = self.state.page("expiry:", limit=limit)
        for expiry in page.rows:
            root_id = str(expiry.values["root_id"])
            row = self.state.get(root_key(root_id))
            if row is None:
                self.state.transact((StateChange(expiry.key, None, expiry.etag),))
                continue
            root = LifecycleRoot.model_validate(row.values, strict=False)
            if root.pinned or root.expires_at is None:
                raise ValueError("a pinned root must never enter the expiry index")
            if root.expires_at > self.clock():
                break
            if root.state != "deleting":
                generation, generation_change = self._root_generation()
                root = root.model_copy(update={"state": "deleting", "generation": generation})
                try:
                    self.state.transact(
                        (
                            StateChange(row.key, root.model_dump(mode="json"), row.etag),
                            generation_change,
                        )
                    )
                except StateConflictError:
                    continue
            if not cleanup_records(root):
                continue
            ownership = self.state.page(f"root-object:{sha256_digest(root_id)}:", limit=40)
            for reference in ownership.rows:
                self._detach(root, reference)
            if ownership.cursor is not None:
                continue
            if self.state.page(f"root-object:{sha256_digest(root_id)}:", limit=1).rows:
                continue
            row = self.state.get(root_key(root_id))
            current_expiry = self.state.get(expiry.key)
            if (
                row is not None
                and current_expiry is not None
                and row.values.get("generation") == root.generation
                and row.values.get("state") == "deleting"
            ):
                self.state.transact(
                    (
                        StateChange(row.key, None, row.etag),
                        StateChange(current_expiry.key, None, current_expiry.etag),
                    )
                )
                completed += 1
        return completed

    def _detach(self, root: LifecycleRoot, reference: StateRow) -> None:
        root_guard = self.cleanup_guard(root)
        if root_guard is None:
            return
        artifact_id = str(reference.values["artifact_id"])
        owner_key = f"owner:{artifact_id}:{sha256_digest(root.id)}"
        owner = self.state.get(owner_key)
        object_row = self.state.get(f"object:{artifact_id}")
        if object_row is None:
            raise ValueError("object ownership projection is incomplete")
        owners = self.state.page(f"owner:{artifact_id}:", limit=2).rows
        other_owner = (
            any(row.key != owner_key for row in owners)
            or self.state.get(f"system-object:{artifact_id}") is not None
        ) or (self.external_owner is not None and self.external_owner(artifact_id))
        if other_owner:
            if owner is None:
                raise ValueError("missing owner record")
            with suppress(StateConflictError):
                self.state.transact(
                    (
                        StateChange(owner.key, None, owner.etag),
                        StateChange(reference.key, None, reference.etag),
                        StateChange(object_row.key, object_row.values, object_row.etag),
                        root_guard,
                    )
                )
            return
        if object_row.values.get("state") != "deleting":
            etag = (
                self.artifacts.object_etag(artifact_id)
                if isinstance(self.artifacts, AzureBlobArtifactStore | FileArtifactStore)
                else None
            )
            generation, generation_change = self._object_generation()
            deleting: dict[str, JsonValue] = {
                "state": "deleting",
                "generation": generation,
                "blob_etag": etag,
            }
            try:
                self.state.transact(
                    (
                        StateChange(object_row.key, deleting, object_row.etag),
                        generation_change,
                        root_guard,
                    )
                )
            except StateConflictError:
                return
            object_row = self.state.get(object_row.key)
            if (
                object_row is None
                or object_row.values.get("state") != "deleting"
                or object_row.values.get("generation") != generation
            ):
                return
        deletion_generation = int(str(object_row.values["generation"]))
        root_guard = self.cleanup_guard(root)
        if root_guard is None:
            return
        if isinstance(self.artifacts, AzureBlobArtifactStore):
            etag_value = object_row.values.get("blob_etag")
            if etag_value is None:
                current_etag = self.artifacts.object_etag(artifact_id)
                if current_etag is not None:
                    try:
                        self.state.transact(
                            (
                                StateChange(
                                    object_row.key,
                                    {**object_row.values, "blob_etag": current_etag},
                                    object_row.etag,
                                ),
                                root_guard,
                            )
                        )
                    except StateConflictError:
                        return
                    object_row = self.state.get(object_row.key)
                    if (
                        object_row is None
                        or object_row.values.get("state") != "deleting"
                        or object_row.values.get("generation") != deletion_generation
                    ):
                        return
                    etag_value = current_etag
            if not self.artifacts.delete(
                artifact_id, etag=None if etag_value is None else str(etag_value)
            ):
                return
        elif isinstance(self.artifacts, FileArtifactStore):
            etag_value = object_row.values.get("blob_etag")

            def delete_file() -> None:
                assert isinstance(self.artifacts, FileArtifactStore)
                self.artifacts.delete(
                    artifact_id, etag=None if etag_value is None else str(etag_value)
                )

            if isinstance(self.state, SqliteStateStore | PrefixedStateStore):
                try:
                    self.state.run_fenced(object_row.key, object_row.etag, delete_file)
                except StateConflictError:
                    return
            else:
                delete_file()
        else:
            raise TypeError("artifact store does not support conditional collection")
        descriptors = self.state.page(f"descriptor:{artifact_id}:", limit=90)
        root_guard = self.cleanup_guard(root)
        if root_guard is None:
            return
        if descriptors.rows:
            self.state.transact(
                (
                    *(StateChange(row.key, None, row.etag) for row in descriptors.rows),
                    StateChange(object_row.key, object_row.values, object_row.etag),
                    root_guard,
                )
            )
            object_row = self.state.get(object_row.key)
            if (
                object_row is None
                or object_row.values.get("state") != "deleting"
                or object_row.values.get("generation") != deletion_generation
            ):
                return
        if descriptors.cursor is not None:
            return
        if self.descriptor_cleanup is not None:
            self.descriptor_cleanup(artifact_id, deletion_generation)
        changes = [
            StateChange(object_row.key, None, object_row.etag),
            StateChange(reference.key, None, reference.etag),
        ]
        root_guard = self.cleanup_guard(root)
        if root_guard is None:
            return
        changes.append(root_guard)
        if owner is not None:
            changes.append(StateChange(owner.key, None, owner.etag))
        self.state.transact(changes)

    @staticmethod
    def as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("lifecycle timestamps require a timezone")
        return value.astimezone(UTC)
