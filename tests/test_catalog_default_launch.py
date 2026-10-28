from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from ai_bench.api import create_app
from ai_bench.archives import export_archive, import_archive
from ai_bench.backends import local_backend
from ai_bench.contracts import (
    BenchmarkSample,
    DatasetAvailability,
    JsonValue,
    ModalityArtifact,
)
from ai_bench.engine import BenchmarkEngine
from ai_bench.packs import DatasetUnavailableError, DiscoveredPack, discover_packs


def test_public_installed_default_launch_uses_imported_catalog_without_source(
    tmp_path: Path,
    pack_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = BenchmarkEngine(tmp_path / "source", pack_root)
    manifest = source.create_run(
        "synthetic_text_intent", solution_ids=("keyword-standalone",), retry_limit=2
    )
    source.run_to_completion(manifest.run_id)
    digest = export_archive(source, manifest.run_id, tmp_path / "bundle")
    target_root = tmp_path / "target"
    backend = local_backend(target_root, durable_projections=True)
    target = BenchmarkEngine(target_root, pack_root, backend=backend)
    import_archive(target, tmp_path / "bundle", expected_digest=digest)
    installed = target.datasets.list(manifest.benchmark.id).items[0]
    (tmp_path / "source").rename(tmp_path / "source-unavailable")
    discovered = discover_packs(pack_root)["synthetic_text_intent"]
    original = discovered.pack

    class UnavailableSource:
        id = "missing-acquisition"
        revision = original.dataset.revision

        def availability(self) -> DatasetAvailability:
            return DatasetAvailability(
                status="not-configured",
                reason="Original acquisition is deliberately unavailable.",
                sample_count=6,
                scored_sample_count=6,
            )

        def samples(self) -> tuple[BenchmarkSample, ...]:
            raise DatasetUnavailableError(self.availability())

        def read_artifact(self, artifact: ModalityArtifact) -> bytes:
            raise AssertionError("Launch must not read the mutable acquisition directory.")

        def snapshot_metadata(self) -> dict[str, JsonValue]:
            return {}

    @dataclass(frozen=True)
    class CatalogOnlyPack:
        definition = original.definition
        solutions = original.solutions
        evaluator = original.evaluator
        viewer = original.viewer
        dataset: UnavailableSource

    configured = CatalogOnlyPack(UnavailableSource())
    monkeypatch.setattr("ai_bench.engine.get_pack", lambda *_args: configured)
    monkeypatch.setattr("ai_bench.api_dataset_adapter.get_pack", lambda *_args: configured)
    monkeypatch.setattr(
        "ai_bench.api.discover_packs",
        lambda *_args: {
            manifest.benchmark.id: DiscoveredPack(discovered.path, discovered.config, configured)
        },
    )
    monkeypatch.setenv("AI_BENCH_PUBLIC_DEMO_MODE", "true")
    monkeypatch.setenv("AI_BENCH_PUBLIC_ORIGIN", "https://demo.example.test")
    client = TestClient(create_app(target_root, pack_root, backend=backend))
    catalog = client.get("/api/benchmarks").json()[0]
    assert catalog["dataset_availability"]["status"] == "ready"
    assert catalog["default_dataset_id"] == installed.id
    key = f"v1.{int(datetime.now(UTC).timestamp())}.{uuid4()}"
    headers = {"Idempotency-Key": key, "X-AI-Bench-Intent": "public-demo"}
    request = {
        "benchmark_id": manifest.benchmark.id,
        "solution_ids": list(manifest.solution_ids),
        "retry_limit": 2,
        "allow_networked": False,
    }
    created = client.post("/api/runs", json=request, headers=headers)
    assert created.status_code == 202, created.text
    replay = client.post("/api/runs", json=request, headers=headers)
    assert replay.json()["run_id"] == created.json()["run_id"]
    detail = client.get(f"/api/runs/{created.json()['run_id']}").json()
    assert detail["dataset_id"] == installed.id
    assert detail["manifest"]["dataset_revision"] == installed.revision.model_dump(mode="json")
