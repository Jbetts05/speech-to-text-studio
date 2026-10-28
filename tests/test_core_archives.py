from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from ai_bench.archives import export_archive, import_archive, validate_archive
from ai_bench.backends import local_backend
from ai_bench.engine import BenchmarkEngine
from ai_bench.hashing import content_digest


def test_exact_terminal_transfer_survives_missing_source(tmp_path: Path, pack_root: Path) -> None:
    source = BenchmarkEngine(tmp_path / "source", pack_root)
    manifest = source.create_run(
        "synthetic_text_intent", solution_ids=("keyword-standalone",), retry_limit=2
    )
    first = source.repository.claim_task("worker")
    assert first is not None
    source.repository.fail_attempt(first, RuntimeError("synthetic first-attempt failure"))
    report = source.run_to_completion(manifest.run_id)
    source_bytes = source.get_report_bytes(manifest.run_id)
    original_exports = [path.read_bytes() for path in source.export_report(manifest.run_id)]
    bundle = tmp_path / "bundle"
    digest = export_archive(source, manifest.run_id, bundle)
    assert content_digest((bundle / "index.json").read_bytes()) == digest
    index = validate_archive(bundle, expected_digest=digest)
    assert any(len(attempts) == 2 for attempts in index.attempts.values())
    (tmp_path / "source").rename(tmp_path / "unavailable-source")
    target_root = tmp_path / "target"
    target = BenchmarkEngine(
        target_root, pack_root, backend=local_backend(target_root, durable_projections=True)
    )
    assert import_archive(target, bundle, expected_digest=digest) == manifest.run_id
    assert export_archive(target, manifest.run_id, tmp_path / "reexported") == digest
    assert target.get_report_bytes(manifest.run_id) == source_bytes
    assert target.get_report(manifest.run_id) == report
    assert [path.read_bytes() for path in target.export_report(manifest.run_id)] == original_exports
    assert import_archive(target, bundle, expected_digest=digest) == manifest.run_id
    root = target.lifecycle.require_live(manifest.run_id)
    assert root.pinned and root.archival and root.expires_at is None
    assert not target.repository.unfinished_runs()
    assert target.repository.claim_task("must-not-execute") is None
    assert len(target.datasets.list(manifest.benchmark.id).items) == 1
    target.repair_initialization(manifest)
    assert target.repository.claim_task("still-must-not-execute") is None
    installed = target.datasets.list(manifest.benchmark.id).items[0]
    new_run = target.create_run(
        manifest.benchmark.id, solution_ids=manifest.solution_ids, dataset_id=installed.id
    )
    new_root = target.lifecycle.require_live(new_run.run_id)
    assert not new_root.pinned
    assert new_root.expires_at == new_run.created_at + timedelta(days=10)


def test_archive_corruption_is_rejected_before_target_writes(
    tmp_path: Path, pack_root: Path
) -> None:
    source = BenchmarkEngine(tmp_path / "source", pack_root)
    manifest = source.create_run("synthetic_text_intent", solution_ids=("keyword-standalone",))
    source.run_to_completion(manifest.run_id)
    bundle = tmp_path / "bundle"
    digest = export_archive(source, manifest.run_id, bundle)
    index = validate_archive(bundle)
    (bundle / "objects" / index.objects[0].artifact_id).write_bytes(b"corrupt")
    target_root = tmp_path / "target"
    target = BenchmarkEngine(
        target_root, pack_root, backend=local_backend(target_root, durable_projections=True)
    )
    with pytest.raises(ValueError, match="exact-byte"):
        import_archive(target, bundle, expected_digest=digest)
    assert not target.repository.list_runs()
    assert not target.datasets.list(manifest.benchmark.id).items
