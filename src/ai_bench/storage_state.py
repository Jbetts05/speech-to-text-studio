"""Bounded same-partition compare-and-swap primitives for durable projections."""

from __future__ import annotations

import importlib
import json
import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import uuid4

from ai_bench.contracts import JsonValue
from ai_bench.hashing import canonical_json


class StateConflictError(RuntimeError):
    """At least one exact generation changed; none of the transaction was applied."""


class AmbiguousCommitError(RuntimeError):
    """The response was lost and the intended state could not be confirmed."""


@dataclass(frozen=True)
class StateRow:
    key: str
    values: Mapping[str, JsonValue]
    etag: str


@dataclass(frozen=True)
class StateChange:
    key: str
    values: Mapping[str, JsonValue] | None
    etag: str | None = None


@dataclass(frozen=True)
class StatePage:
    rows: tuple[StateRow, ...]
    cursor: str | None


class StateStore(Protocol):
    def get(self, key: str) -> StateRow | None: ...

    def page(self, prefix: str, *, after: str | None = None, limit: int = 100) -> StatePage: ...

    def transact(self, changes: Sequence[StateChange]) -> None: ...


class PrefixedStateStore:
    """Isolated logical indexes in the same transactional partition."""

    def __init__(self, store: StateStore, prefix: str) -> None:
        self.store = store
        self.prefix = prefix

    def get(self, key: str) -> StateRow | None:
        row = self.store.get(self.prefix + key)
        return None if row is None else StateRow(key, row.values, row.etag)

    def page(self, prefix: str, *, after: str | None = None, limit: int = 100) -> StatePage:
        page = self.store.page(
            self.prefix + prefix,
            after=None if after is None else self.prefix + after,
            limit=limit,
        )
        return StatePage(
            tuple(StateRow(row.key[len(self.prefix) :], row.values, row.etag) for row in page.rows),
            None if page.cursor is None else page.cursor[len(self.prefix) :],
        )

    def transact(self, changes: Sequence[StateChange]) -> None:
        self.store.transact(
            tuple(
                StateChange(self.prefix + change.key, change.values, change.etag)
                for change in changes
            )
        )

    def run_fenced(self, key: str, etag: str, action: Callable[[], None]) -> None:
        if not isinstance(self.store, SqliteStateStore | PrefixedStateStore):
            raise TypeError("filesystem fencing requires local SQLite coordination")
        self.store.run_fenced(self.prefix + key, etag, action)


def validate_changes(changes: Sequence[StateChange]) -> None:
    if not 1 <= len(changes) <= 100:
        raise ValueError("a transaction must contain 1-100 distinct entities")
    if len({change.key for change in changes}) != len(changes):
        raise ValueError("an entity may occur only once per transaction")
    size = 0
    for change in changes:
        if (
            not change.key
            or len(change.key) > 900
            or any(character in change.key for character in "/\\#?\x00")
        ):
            raise ValueError("invalid coordination row key")
        if change.values is None and change.etag is None:
            raise ValueError("deletion requires an exact ETag")
        payload = canonical_json(change.values)
        # Payload is one Edm.String. Bound UTF-16, not only UTF-8 length.
        if len(payload.decode("utf-8").encode("utf-16-le")) > 60_000:
            raise ValueError("large immutable payloads must be stored as objects")
        size += len(payload) * 2 + 2048
    if size > 3_900_000:
        raise ValueError("transaction exceeds the safe 4 MiB envelope")


class SqliteStateStore:
    """Local CAS rows; never used on hosted application/network storage."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._read_lock = threading.RLock()
        self._reader: sqlite3.Connection | None = None
        self._sessions = 0
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS coordination_rows "
                "(row_key TEXT PRIMARY KEY, payload TEXT NOT NULL, etag TEXT NOT NULL)"
            )

    @contextmanager
    def execution_session(self) -> Iterator[None]:
        """Keep WAL open across a work batch without retaining per-thread cursors."""
        with self._read_lock:
            if self._reader is None:
                self._reader = sqlite3.connect(
                    self.path, timeout=30, check_same_thread=False, isolation_level=None
                )
                try:
                    self._reader.execute("SELECT row_key FROM coordination_rows LIMIT 1").fetchall()
                except Exception:
                    self._reader.close()
                    self._reader = None
                    raise
            self._sessions += 1
        try:
            yield
        finally:
            with self._read_lock:
                self._sessions -= 1
                if self._sessions == 0 and self._reader is not None:
                    self._reader.close()
                    self._reader = None

    def _read(self, query: str, parameters: tuple[object, ...]) -> list[tuple[Any, ...]]:
        with self._read_lock:
            if self._reader is not None:
                return self._reader.execute(query, parameters).fetchall()
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute(query, parameters).fetchall()

    def get(self, key: str) -> StateRow | None:
        rows = self._read("SELECT payload, etag FROM coordination_rows WHERE row_key=?", (key,))
        return None if not rows else StateRow(key, json.loads(rows[0][0]), str(rows[0][1]))

    def page(self, prefix: str, *, after: str | None = None, limit: int = 100) -> StatePage:
        limit = max(1, min(limit, 500))
        rows = self._read(
            "SELECT row_key, payload, etag FROM coordination_rows "
            "WHERE row_key >= ? AND row_key < ? AND row_key > ? "
            "ORDER BY row_key LIMIT ?",
            (prefix, prefix + "\uffff", after or "", limit + 1),
        )
        selected = tuple(
            StateRow(str(row[0]), json.loads(row[1]), str(row[2])) for row in rows[:limit]
        )
        return StatePage(selected, selected[-1].key if len(rows) > limit else None)

    def transact(self, changes: Sequence[StateChange]) -> None:
        validate_changes(changes)
        with closing(sqlite3.connect(self.path, timeout=30)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            for change in changes:
                current = connection.execute(
                    "SELECT etag FROM coordination_rows WHERE row_key=?", (change.key,)
                ).fetchone()
                if (None if current is None else str(current[0])) != change.etag:
                    raise StateConflictError(change.key)
            for change in changes:
                if change.values is None:
                    connection.execute(
                        "DELETE FROM coordination_rows WHERE row_key=?", (change.key,)
                    )
                elif change.etag is None:
                    connection.execute(
                        "INSERT INTO coordination_rows VALUES (?, ?, ?)",
                        (change.key, canonical_json(change.values).decode(), uuid4().hex),
                    )
                else:
                    connection.execute(
                        "UPDATE coordination_rows SET payload=?, etag=? WHERE row_key=?",
                        (canonical_json(change.values).decode(), uuid4().hex, change.key),
                    )

    def run_fenced(self, key: str, etag: str, action: Callable[[], None]) -> None:
        with closing(sqlite3.connect(self.path, timeout=30)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT etag FROM coordination_rows WHERE row_key=?", (key,)
            ).fetchone()
            if row is None or str(row[0]) != etag:
                raise StateConflictError("filesystem collection generation changed")
            action()

    def has_legacy_runs(self) -> bool:
        with closing(sqlite3.connect(self.path)) as connection:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'"
            ).fetchone()
            return (
                table is not None
                and connection.execute("SELECT 1 FROM runs LIMIT 1").fetchone() is not None
            )


class AzureTableStateStore:
    """One precreated Table and namespace; never creates resources or uses upsert."""

    def __init__(self, table_client: Any, namespace: str, *, match_condition: Any = None) -> None:
        if (
            not namespace
            or len(namespace) > 128
            or not all(
                character.isascii() and (character.isalnum() or character in "-_")
                for character in namespace
            )
        ):
            raise ValueError("namespace must be a portable opaque identifier")
        self.client = table_client
        self.namespace = namespace
        self.match_condition = match_condition

    def _row(self, entity: Any) -> StateRow:
        metadata = getattr(entity, "metadata", {})
        etag = metadata.get("etag") or entity.get("etag") or entity.get("odata.etag")
        if not etag:
            raise ValueError("Table returned no ETag; unconditional updates are forbidden")
        return StateRow(
            str(entity["RowKey"]),
            cast(dict[str, JsonValue], json.loads(entity["Payload"])),
            str(etag),
        )

    def get(self, key: str) -> StateRow | None:
        try:
            entity = self.client.get_entity(partition_key=self.namespace, row_key=key)
        except Exception as error:
            if getattr(error, "status_code", None) == 404:
                return None
            raise
        return self._row(entity)

    def page(self, prefix: str, *, after: str | None = None, limit: int = 100) -> StatePage:
        limit = max(1, min(limit, 500))
        rows: list[StateRow] = []
        entities = self.client.query_entities(
            query_filter=(
                "PartitionKey eq @partition and RowKey ge @prefix "
                "and RowKey lt @end and RowKey gt @after"
            ),
            parameters={
                "partition": self.namespace,
                "prefix": prefix,
                "end": prefix + "\uffff",
                "after": after or "",
            },
            results_per_page=limit + 1,
        )
        for entity in entities:
            rows.append(self._row(entity))
            if len(rows) > limit:
                break
        selected = tuple(rows[:limit])
        return StatePage(selected, selected[-1].key if len(rows) > limit else None)

    def transact(self, changes: Sequence[StateChange]) -> None:
        validate_changes(changes)
        match_condition = self.match_condition
        if match_condition is None:
            match_condition = importlib.import_module("azure.core").MatchConditions.IfNotModified
        operations: list[Any] = []
        for change in changes:
            entity = {"PartitionKey": self.namespace, "RowKey": change.key}
            if change.values is not None:
                entity["Payload"] = canonical_json(change.values).decode()
            if change.etag is None:
                operations.append(("create", entity))
            else:
                options = {
                    "etag": change.etag,
                    "match_condition": match_condition,
                }
                if change.values is not None:
                    options["mode"] = "replace"
                operations.append(
                    ("delete" if change.values is None else "update", entity, options)
                )
        try:
            self.client.submit_transaction(operations)
        except Exception as error:
            # A successful batch can lose its HTTP response. Reconcile without
            # resending a transition that might consume an attempt or generation.
            try:
                observed = [self.get(change.key) for change in changes]
                if all(
                    (row is None and change.values is None)
                    or (
                        row is not None
                        and change.values is not None
                        and row.values == change.values
                    )
                    for change, row in zip(changes, observed, strict=True)
                ):
                    return
            except Exception:
                pass
            if getattr(error, "status_code", None) in {409, 412}:
                raise StateConflictError("Table transaction conflicted") from error
            raise AmbiguousCommitError("Table commit could not be reconciled") from error
