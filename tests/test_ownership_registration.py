from __future__ import annotations

import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ai_bench.artifacts import FileArtifactStore
from ai_bench.hashing import sha256_digest
from ai_bench.lifecycle import ExpiredRootError, LifecycleService, root_key
from ai_bench.storage_state import SqliteStateStore, StateChange, StateRow


def test_local_reference_writers_are_serialized_without_lost_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = SqliteStateStore(tmp_path / "state.sqlite3")
    service = LifecycleService(state, FileArtifactStore(tmp_path / "objects"))
    service.create("synthetic-run", "run")
    mutex = threading.RLock()
    started, release, all_contending = threading.Event(), threading.Event(), threading.Event()
    counter_lock = threading.Lock()
    contenders = 0
    readers: set[int] = set()

    class ObservedLock:
        def __enter__(self) -> None:
            nonlocal contenders
            with counter_lock:
                contenders += 1
                if contenders == 5:
                    all_contending.set()
            mutex.acquire()

        def __exit__(self, *_: object) -> None:
            mutex.release()

    get = state.get

    def read(key: str) -> StateRow | None:
        if key == root_key("synthetic-run"):
            readers.add(threading.get_ident())
            started.set()
            assert release.wait(5)
        return get(key)

    monkeypatch.setattr(service, "_attach_lock", ObservedLock())
    monkeypatch.setattr(state, "get", read)
    identifiers = [sha256_digest({"synthetic": index}) for index in range(5)]
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(service.attach, "synthetic-run", identifiers[0])]
        try:
            assert started.wait(5)
            futures.extend(
                pool.submit(service.attach, "synthetic-run", identity)
                for identity in identifiers[1:]
            )
            assert all_contending.wait(5)
            assert len(readers) == 1
        finally:
            release.set()
        for future in futures:
            future.result(timeout=5)
    for identifier in identifiers:
        assert state.get(f"owner:{identifier}:{sha256_digest('synthetic-run')}") is not None


def test_reference_registration_is_reentrant_and_remains_cas_fenced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = SqliteStateStore(tmp_path / "state.sqlite3")
    service = LifecycleService(state, FileArtifactStore(tmp_path / "objects"))
    service.create("synthetic-run", "run")
    outer, inner = sha256_digest("outer"), sha256_digest("inner")
    transact = state.transact
    injected = False

    def nested(changes: Sequence[StateChange]) -> None:
        nonlocal injected
        if not injected and any(change.key.startswith("owner:") for change in changes):
            injected = True
            service.attach("synthetic-run", inner)
        transact(changes)

    monkeypatch.setattr(state, "transact", nested)
    errors: list[Exception] = []

    def attach() -> None:
        try:
            service.attach("synthetic-run", outer)
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=attach, daemon=True)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive(), "reference registration deadlocked on reentrancy"
    assert not errors and injected
    for identifier in (outer, inner):
        assert state.get(f"owner:{identifier}:{sha256_digest('synthetic-run')}") is not None


def test_local_lock_does_not_bypass_an_external_deletion_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = SqliteStateStore(tmp_path / "state.sqlite3")
    artifacts = FileArtifactStore(tmp_path / "objects")
    first, second = LifecycleService(state, artifacts), LifecycleService(state, artifacts)
    first.create("synthetic-run", "run")
    artifact_id = sha256_digest("synthetic")
    transact = state.transact
    injected = False

    def deleted(changes: Sequence[StateChange]) -> None:
        nonlocal injected
        if not injected and any(change.key.startswith("owner:") for change in changes):
            injected = True
            root = second.require_live("synthetic-run", ready=False)
            row = state.get(root_key(root.id))
            assert row is not None
            removed = root.model_copy(
                update={"state": "deleting", "generation": root.generation + 1}
            )
            transact((StateChange(row.key, removed.model_dump(mode="json"), row.etag),))
        transact(changes)

    monkeypatch.setattr(state, "transact", deleted)
    with pytest.raises(ExpiredRootError):
        first.attach("synthetic-run", artifact_id)
    assert injected
    assert state.get(f"owner:{artifact_id}:{sha256_digest('synthetic-run')}") is None
