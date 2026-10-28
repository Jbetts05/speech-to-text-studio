from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_bench.api import create_app
from ai_bench.contracts import (
    AttemptStatus,
    CostEstimate,
    PricingModel,
    PricingRate,
    RunManifest,
    SolutionDescriptor,
    TaskAttempt,
    TaskStatus,
    TelemetryEvidence,
    Usage,
)
from ai_bench.costs import PricingQuote, build_cost_summary
from ai_bench.engine import BenchmarkEngine
from ai_bench.repositories import TaskRecord

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def manifest(tmp_path: Path, pack_root: Path) -> RunManifest:
    original = BenchmarkEngine(tmp_path / "state", pack_root).create_run("synthetic_text_intent")
    return original.model_copy(
        update={"sample_ids": original.sample_ids[:1], "samples": original.samples[:1]}
    )


def task(
    manifest: RunManifest,
    *,
    solution: int = 0,
    attempts: int = 1,
    status: TaskStatus = TaskStatus.SUCCEEDED,
    sample: str | None = None,
) -> TaskRecord:
    solution_id = manifest.solution_ids[solution]
    sample_id = sample or manifest.sample_ids[0]
    return TaskRecord(
        id=f"{solution_id}-{sample_id}",
        run_id=manifest.run_id,
        solution_id=solution_id,
        sample_id=sample_id,
        status=status,
        attempts=attempts,
        max_attempts=3,
        lease_token=attempts,
    )


def attempt(
    record: TaskRecord,
    amount: str | None,
    *,
    number: int = 1,
    currency: str = "USD",
    dimensions: Mapping[str, Decimal] | None = None,
    complete: bool = True,
    status: AttemptStatus = AttemptStatus.SUCCEEDED,
) -> TaskAttempt:
    return TaskAttempt(
        task_id=record.id,
        attempt_number=number,
        status=status,
        lease_token=number,
        started_at=NOW,
        finished_at=NOW,
        telemetry=TelemetryEvidence(
            started_at=NOW,
            finished_at=NOW,
            latency_ms=Decimal(0),
            usage=Usage(
                dimensions={"seconds": Decimal(10), "calls": Decimal(1)}
                if dimensions is None
                else dimensions,
                complete=complete,
                uncertainty=None if complete else "Billing was not reported.",
            ),
            cost=CostEstimate(
                amount=None if amount is None else Decimal(amount),
                currency=currency,
                pricing_snapshot="invented-attempt-price@1",
                reason="No pinned price for this attempt." if amount is None else None,
            ),
        ),
    )


class Pricing:
    def quote(self, descriptor: SolutionDescriptor) -> PricingQuote:
        return PricingQuote(
            pricing=PricingModel(
                id="invented-current-price",
                version="1",
                currency="USD",
                region="offline",
                effective_at=NOW,
                source="Synthetic test rate, never a service price.",
                rates=(PricingRate(dimension="seconds", unit_size=Decimal(10), price=Decimal(2)),),
            ),
            checked_at=NOW,
            basis="Conditional test estimate of recorded seconds; not billed cost.",
        )


@pytest.mark.parametrize("amount,status", [("0", "known"), ("0.125", "known"), (None, "unknown")])
def test_recorded_zero_known_and_unknown_remain_distinct(
    manifest: RunManifest, amount: str | None, status: str
) -> None:
    records = [task(manifest, solution=index) for index in range(2)]
    histories = {record.id: (attempt(record, amount),) for record in records}
    summary = build_cost_summary(manifest, "a" * 64, records, histories)
    assert summary.recorded_total.status == status
    expected = None if amount is None else Decimal(amount) * 2
    assert summary.recorded_total.amounts == {"USD": expected}
    assert summary.recorded_total.covered_attempts == (0 if amount is None else 2)
    assert summary.current_estimate_total.status == "unknown"
    assert summary.solutions[0].recorded_pricing_snapshots == ("invented-attempt-price@1",)


def test_retry_cost_usage_and_unknown_coverage_are_not_lost(manifest: RunManifest) -> None:
    first, second = task(manifest, attempts=2), task(manifest, solution=1)
    histories = {
        first.id: (
            attempt(first, "0.2", status=AttemptStatus.FAILED),
            attempt(first, "0.3", number=2),
        ),
        second.id: (attempt(second, None),),
    }
    summary = build_cost_summary(manifest, "a" * 64, [first, second], histories)
    assert summary.recorded_total.amounts == {"USD": Decimal("0.5")}
    assert summary.recorded_total.status == "partial"
    assert (summary.recorded_total.covered_attempts, summary.recorded_total.total_attempts) == (
        2,
        3,
    )
    assert summary.solutions[0].recorded_usage == {"seconds": Decimal(20), "calls": Decimal(2)}
    assert summary.samples[manifest.sample_ids[0]][0].recorded.amounts == {"USD": Decimal("0.5")}
    assert "No pinned price" in " ".join(summary.recorded_total.reasons)


def test_missing_history_is_counted_per_task_not_hidden_by_another_history(
    manifest: RunManifest,
) -> None:
    first, second = task(manifest, attempts=2), task(manifest, solution=1, attempts=0)
    histories = {
        first.id: (attempt(first, "0.1"),),
        second.id: (attempt(second, "0.2"),),
    }
    summary = build_cost_summary(manifest, "a" * 64, [first, second], histories, Pricing())
    assert summary.recorded_total.total_attempts == 3
    assert summary.recorded_total.covered_attempts == 2
    assert summary.recorded_total.status == "partial"
    assert "history is missing" in " ".join(summary.recorded_total.reasons)
    assert "every attempt" in " ".join(summary.solutions[0].usage_uncertainty)


def test_current_estimate_is_separate_retry_inclusive_and_not_billed_cost(
    manifest: RunManifest,
) -> None:
    records = [task(manifest, attempts=2), task(manifest, solution=1)]
    histories = {
        records[0].id: (
            attempt(records[0], None, complete=False, status=AttemptStatus.FAILED),
            attempt(records[0], None, number=2, complete=False),
        ),
        records[1].id: (attempt(records[1], None, complete=False),),
    }
    before = manifest.model_dump_json(), repr(histories)
    summary = build_cost_summary(manifest, "a" * 64, records, histories, Pricing())
    assert summary.recorded_total.amounts == {"USD": None}
    assert summary.current_estimate_total.amounts == {"USD": Decimal(6)}
    assert summary.current_estimate_total.status == "known"
    assert summary.current_estimate_total.covered_attempts == 3
    assert summary.solutions[0].current_pricing is not None
    assert "not billed cost" in summary.solutions[0].current_pricing.basis
    assert summary.solutions[0].usage_uncertainty == ("Billing was not reported.",)
    assert before == (manifest.model_dump_json(), repr(histories))


def test_unobserved_request_and_missing_dimensions_make_estimate_partial(
    manifest: RunManifest,
) -> None:
    first, second = task(manifest, attempts=2), task(manifest, solution=1)
    started = attempt(first, None, number=2).model_copy(update={"telemetry": None})
    histories = {
        first.id: (attempt(first, None), started),
        second.id: (attempt(second, None, dimensions={"calls": Decimal(1)}),),
    }
    summary = build_cost_summary(manifest, "a" * 64, [first, second], histories, Pricing())
    assert summary.current_estimate_total.amounts == {"USD": Decimal(2)}
    assert summary.current_estimate_total.status == "partial"
    assert summary.current_estimate_total.covered_attempts == 1
    assert summary.current_estimate_total.total_attempts == 3
    assert "no recorded usage" in " ".join(summary.current_estimate_total.reasons)
    assert "dimension is missing" in " ".join(summary.current_estimate_total.reasons)


def test_running_work_and_missing_tasks_cannot_claim_a_final_total(manifest: RunManifest) -> None:
    record = task(manifest, status=TaskStatus.LEASED)
    summary = build_cost_summary(
        manifest, "a" * 64, [record], {record.id: (attempt(record, "0.1"),)}, Pricing()
    )
    assert summary.recorded_total.status == "partial"
    assert summary.recorded_total.total_tasks == 2
    assert summary.recorded_total.terminal_tasks == 0
    assert "Work is incomplete" in " ".join(summary.recorded_total.reasons)
    assert summary.solutions[1].recorded.amounts == {"USD": None}


def test_mixed_currencies_remain_separate_without_spurious_pinned_currency(
    manifest: RunManifest,
) -> None:
    first, second = task(manifest), task(manifest, solution=1)
    histories = {
        first.id: (attempt(first, "0.1", currency="GBP"),),
        second.id: (attempt(second, "0.2", currency="EUR"),),
    }
    summary = build_cost_summary(manifest, "a" * 64, [first, second], histories)
    assert summary.recorded_total.status == "known"
    assert summary.recorded_total.amounts == {"GBP": Decimal("0.1"), "EUR": Decimal("0.2")}
    assert summary.solutions[0].recorded.amounts == {"GBP": Decimal("0.1")}


def test_empty_current_rates_are_unknown_not_free(manifest: RunManifest) -> None:
    class EmptyPricing:
        def quote(self, descriptor: SolutionDescriptor) -> PricingQuote:
            return (
                Pricing()
                .quote(descriptor)
                .model_copy(
                    update={"pricing": descriptor.pricing_snapshot.model_copy(update={"rates": ()})}
                )
            )

    record = task(manifest)
    summary = build_cost_summary(
        manifest, "a" * 64, [record], {record.id: (attempt(record, None),)}, EmptyPricing()
    )
    assert summary.current_estimate_total.amounts == {"USD": None}
    assert "no reviewed rates" in " ".join(summary.current_estimate_total.reasons)


def test_api_cost_projection_leaves_immutable_report_and_evidence_unchanged(
    tmp_path: Path, pack_root: Path
) -> None:
    app = create_app(tmp_path / "state", pack_root)
    engine = app.state.engine
    manifest = engine.create_run("synthetic_text_intent")
    report = engine.run_to_completion(manifest.run_id)
    record = engine.repository.get_run(manifest.run_id)
    assert record is not None and record.report_artifact_id is not None
    identifiers = {
        record.manifest_artifact_id,
        record.report_artifact_id,
        *(evidence.artifact_id for result in report.results for evidence in result.evidence),
    }
    before = {identifier: engine.artifacts.get_bytes(identifier) for identifier in identifiers}
    client = TestClient(app)
    for _ in range(2):
        response = client.get(f"/api/runs/{manifest.run_id}")
        assert response.status_code == 200
        costs = response.json()["costs"]
        assert costs["recorded_total"]["status"] == "known"
        assert Decimal(costs["recorded_total"]["amounts"]["USD"]) == 0
        assert costs["recorded_total"]["covered_attempts"] == 12
        assert costs["current_estimate_total"]["status"] == "unknown"
        assert all(line["current_pricing"] is None for line in costs["solutions"])
    assert (
        client.get(f"/api/runs/{manifest.run_id}/report").content
        == before[record.report_artifact_id]
    )
    assert before == {
        identifier: engine.artifacts.get_bytes(identifier) for identifier in identifiers
    }
    assert engine.repository.get_run(manifest.run_id) == record

    tasks = engine.repository.list_tasks(manifest.run_id)
    summary = build_cost_summary(manifest, record.manifest_artifact_id, tasks, {}, report=report)
    assert summary.recorded_total.status == "known"
    assert summary.recorded_total.covered_attempts == 12
