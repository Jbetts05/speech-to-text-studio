from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from ai_bench.artifacts import ArtifactNotFoundError, FileArtifactStore
from ai_bench.backends import local_backend
from ai_bench.contracts import Modality, utc_now
from ai_bench.engine import BenchmarkEngine
from ai_bench.lifecycle import ExpiredRootError, LifecycleService
from ai_bench.storage_state import SqliteStateStore


def test_shared_objects_survive_other_root_expiry(tmp_path: Path) -> None:
    state = SqliteStateStore(tmp_path / "state.sqlite3")
    objects = FileArtifactStore(tmp_path / "objects")
    now = [utc_now()]
    service = LifecycleService(state, objects, clock=lambda: now[0])
    first = service.create("first", "run")
    artifact = objects.put_bytes(
        b"synthetic shared bytes", modality=Modality.TEXT, media_type="text/plain"
    )
    service.attach("first", artifact.id)
    service.publish("first")
    now[0] += timedelta(days=1)
    second = service.create("second", "run")
    service.attach("second", artifact.id)
    service.publish("second")
    assert first.expires_at is not None and second.expires_at is not None
    now[0] = first.expires_at
    assert service.reap(lambda root: True) == 1
    assert objects.get_bytes(artifact.id) == b"synthetic shared bytes"
    assert service.artifact_has_live_owner(artifact.id)
    now[0] = second.expires_at
    assert not service.artifact_has_live_owner(artifact.id)
    assert service.reap(lambda root: True) == 1
    with pytest.raises(ArtifactNotFoundError):
        objects.get_bytes(artifact.id)
    assert not state.page("owner:").rows
    assert not state.page("object:").rows
    assert not state.page("expiry:").rows


@pytest.mark.parametrize("durable", [False, True])
def test_actual_run_records_and_exports_are_reaped(
    tmp_path: Path, pack_root: Path, durable: bool
) -> None:
    engine = BenchmarkEngine(
        tmp_path, pack_root, backend=local_backend(tmp_path, durable_projections=durable)
    )
    manifest = engine.create_run("synthetic_text_intent", solution_ids=("keyword-standalone",))
    engine.run_to_completion(manifest.run_id)
    run = engine.repository.get_run(manifest.run_id)
    assert run is not None
    manifest_artifact_id = run.manifest_artifact_id
    root = engine.lifecycle.require_live(manifest.run_id)
    assert root.expires_at is not None
    expires_at = root.expires_at
    engine.lifecycle.clock = lambda: expires_at
    with pytest.raises(ExpiredRootError):
        engine.repository.list_tasks(manifest.run_id)
    for _ in range(10):
        engine.reap_expired()
        if engine.lifecycle.get(manifest.run_id) is None:
            break
    assert engine.lifecycle.get(manifest.run_id) is None
    assert engine.repository.get_run(manifest.run_id) is None
    assert not (engine.reports_root / manifest.run_id).exists()
    assert not engine.backend.state.page("owner:").rows
    assert engine.repository.get_artifact(manifest_artifact_id) is None
