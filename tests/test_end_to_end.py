from __future__ import annotations

import csv
import io
import json
from decimal import Decimal
from pathlib import Path

from ai_bench.engine import BenchmarkEngine
from ai_bench.hashing import sha256_digest
from ai_bench.reports import report_csv


def test_synthetic_pack_runs_offline_end_to_end(tmp_path: Path, pack_root: Path) -> None:
    engine = BenchmarkEngine(tmp_path / "state", pack_root)
    manifest = engine.create_run("synthetic_text_intent")
    assert manifest.dataset_revision.version == "1.0.0"
    assert manifest.dataset_revision.provenance.synthetic is True
    assert {sample.role.value for sample in manifest.samples} == {"scored"}
    assert all(
        descriptor.version == "1.0.0" for descriptor in manifest.solution_descriptors.values()
    )
    assert all(
        descriptor.pipeline.adapter_id == solution_id
        for solution_id, descriptor in manifest.solution_descriptors.items()
    )
    assert manifest.evaluator.id == "synthetic-intent-evaluator"
    assert manifest.viewer.id == "plain-text"
    assert all(pricing.region == "offline" for pricing in manifest.pricing_snapshots.values())
    assert manifest.descriptor_hashes["benchmark"] == sha256_digest(manifest.benchmark)
    assert all(len(value) == 64 for value in manifest.descriptor_hashes.values())
    report = engine.run_to_completion(manifest.run_id)

    assert report.status.value == "completed"
    assert len(report.results) == 12
    assert set(report.quality) == {"keyword-standalone", "two-stage-assisted"}
    assert all(values["exact_accuracy"] == Decimal("1") for values in report.quality.values())
    assert all(values["macro_f1"] == Decimal("1") for values in report.quality.values())
    assert report.cost == {
        "keyword-standalone": Decimal("0"),
        "two-stage-assisted": Decimal("0"),
    }
    assert report.cost_currency == {
        "keyword-standalone": "USD",
        "two-stage-assisted": "USD",
    }
    assert all(value.comparable_quality for value in report.quality_comparability.values())
    assert all(result.evidence for result in report.results)
    assert all(result.telemetry is not None for result in report.results)
    for evidence in (item for result in report.results for item in result.evidence):
        artifact = engine.artifacts.get(evidence.artifact_id)
        assert evidence.modality == artifact.modality
        assert evidence.media_type == artifact.media_type
        assert evidence.size_bytes == artifact.size_bytes
    for evidence in (
        item
        for attempts in report.attempt_history.values()
        for attempt in attempts
        for item in attempt.evidence
    ):
        artifact = engine.artifacts.get(evidence.artifact_id)
        assert evidence.modality == artifact.modality
        assert evidence.media_type == artifact.media_type
        assert evidence.size_bytes == artifact.size_bytes
    assert "score" not in report.model_dump()

    json_path, csv_path = engine.export_report(manifest.run_id)
    exported = json.loads(json_path.read_text(encoding="utf-8"))
    assert exported["manifest_artifact_id"] == report.manifest_artifact_id
    rows = list(csv.DictReader(io.StringIO(csv_path.read_text(encoding="utf-8"))))
    assert len(rows) == 12
    assert all(row["manifest_artifact_id"] == report.manifest_artifact_id for row in rows)
    assert report_csv(report).startswith("run_id,task_id")


def test_run_creation_and_reconciliation_are_idempotent(tmp_path: Path, pack_root: Path) -> None:
    engine = BenchmarkEngine(tmp_path / "state", pack_root)
    manifest = engine.create_run("synthetic_text_intent", solution_ids=("keyword-standalone",))
    assert len(engine.repository.list_tasks(manifest.run_id)) == 6
    assert engine.repository.reconcile_tasks(manifest) == 0
    assert len(engine.repository.list_tasks(manifest.run_id)) == 6


def test_core_reporting_has_no_synthetic_metric_or_schema_assumptions() -> None:
    source = (Path(__file__).resolve().parents[1] / "src" / "ai_bench" / "reports.py").read_text(
        encoding="utf-8"
    )
    for pack_specific_term in (
        "exact_accuracy",
        "macro_f1",
        '"expected"',
        '"predicted"',
    ):
        assert pack_specific_term not in source
