"""Independent metric-family report aggregation and export."""

from __future__ import annotations

import csv
import io
import json
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from ai_bench.artifacts import ArtifactStore, _write_once, artifact_json
from ai_bench.contracts import (
    Compatibility,
    EvaluatedCoverage,
    EvaluationRecord,
    Evaluator,
    MetricFamily,
    RunReport,
    RunStatus,
    SolutionDescriptor,
    TaskAttempt,
    TaskResult,
    TaskStatus,
    utc_now,
)


def build_report(
    run_id: str,
    manifest_artifact_id: str,
    results: tuple[TaskResult, ...],
    store: ArtifactStore,
    attempt_history: Mapping[str, tuple[TaskAttempt, ...]],
    evaluator: Evaluator,
    solution_descriptors: Mapping[str, SolutionDescriptor],
    *,
    created_at: datetime | None = None,
    expected_sample_ids: tuple[str, ...] | None = None,
) -> RunReport:
    solutions = sorted(solution_descriptors)
    quality_comparability: dict[str, Compatibility] = {}
    quality: dict[str, dict[str, Decimal | None]] = {}
    cost: dict[str, Decimal | None] = {}
    cost_currency: dict[str, str] = {}
    latency: dict[str, Decimal | None] = {}
    usage: dict[str, dict[str, Decimal]] = {}
    reliability: dict[str, dict[str, Decimal]] = {}
    evidence_links: dict[str, str] = {}
    coverage: dict[str, EvaluatedCoverage] = {}
    latency_breakdown: dict[str, dict[str, Decimal | None]] = {}
    usage_uncertainty: dict[str, tuple[str, ...]] = {}
    expected = expected_sample_ids or tuple(sorted({result.sample_id for result in results}))
    evaluated_sets = {
        solution_id: tuple(
            sorted(
                result.sample_id
                for result in results
                if result.solution_id == solution_id and result.status is TaskStatus.SUCCEEDED
            )
        )
        for solution_id in solutions
    }
    same_coverage = len(set(evaluated_sets.values())) == 1

    for solution_id in solutions:
        descriptor = solution_descriptors[solution_id]
        quality_comparability[solution_id] = descriptor.compatibility
        cost_currency[solution_id] = descriptor.pricing_snapshot.currency
        grouped = [result for result in results if result.solution_id == solution_id]
        succeeded = [result for result in grouped if result.status is TaskStatus.SUCCEEDED]
        complete = set(evaluated_sets[solution_id]) == set(expected)
        coverage[solution_id] = EvaluatedCoverage(
            expected_sample_ids=expected,
            evaluated_sample_ids=evaluated_sets[solution_id],
            complete=complete,
            comparable=complete and same_coverage and descriptor.compatibility.comparable_quality,
            reason=(
                "All expected cases evaluated with matching solution coverage."
                if complete and same_coverage and descriptor.compatibility.comparable_quality
                else "Partial or noncomparable quality; inspect evaluated case IDs."
            ),
        )
        evaluation_records: list[EvaluationRecord] = []
        for result in succeeded:
            comparison_evidence = next(
                (evidence for evidence in result.evidence if evidence.role == "comparison"), None
            )
            comparison = (
                None
                if comparison_evidence is None
                else artifact_json(store, comparison_evidence.artifact_id)
            )
            evaluation_records.append(
                EvaluationRecord(
                    sample_id=result.sample_id,
                    solution_id=result.solution_id,
                    metrics=result.metrics,
                    comparison=comparison,
                )
            )
        declared = {definition.id: definition for definition in evaluator.metric_definitions}
        if len(declared) != len(evaluator.metric_definitions):
            raise ValueError("evaluator metric definitions must have unique identifiers")
        aggregate_ids: set[str] = set()
        quality[solution_id] = {
            definition.id: None
            for definition in evaluator.metric_definitions
            if definition.family is MetricFamily.QUALITY
        }
        if descriptor.compatibility.comparable_quality:
            aggregate = evaluator.aggregate(tuple(evaluation_records))
            for metric in aggregate.metrics:
                definition = declared.get(metric.metric_id)
                if definition is None:
                    raise ValueError(
                        f"evaluator returned undeclared aggregate metric: {metric.metric_id}"
                    )
                if metric.metric_id in aggregate_ids:
                    raise ValueError(
                        f"evaluator returned duplicate aggregate metric: {metric.metric_id}"
                    )
                if metric.family is not definition.family or metric.unit != definition.unit:
                    raise ValueError(
                        f"aggregate metric does not match its declaration: {metric.metric_id}"
                    )
                aggregate_ids.add(metric.metric_id)
                if metric.family is MetricFamily.QUALITY:
                    quality[solution_id][metric.metric_id] = metric.value

        attempts = tuple(
            attempt for result in grouped for attempt in attempt_history.get(result.task_id, ())
        )
        known_costs = [
            attempt.telemetry.cost.amount
            for attempt in attempts
            if attempt.telemetry is not None and attempt.telemetry.cost.amount is not None
        ]
        has_unknown_cost = any(
            attempt.telemetry is None or attempt.telemetry.cost.amount is None
            for attempt in attempts
        )
        cost[solution_id] = (
            None if has_unknown_cost or not attempts else sum(known_costs, Decimal("0"))
        )
        attempt_latencies = [
            attempt.duration_ms for attempt in attempts if attempt.duration_ms is not None
        ]
        has_unknown_latency = any(attempt.duration_ms is None for attempt in attempts)
        latency[solution_id] = (
            None if has_unknown_latency or not attempts else sum(attempt_latencies, Decimal("0"))
        )
        finished = [attempt.finished_at for attempt in attempts if attempt.finished_at is not None]
        latency_breakdown[solution_id] = {
            "provider_ms": (
                sum(
                    (
                        attempt.telemetry.latency_ms
                        for attempt in attempts
                        if attempt.telemetry is not None
                        and attempt.telemetry.latency_ms is not None
                    ),
                    Decimal(0),
                )
                if attempts
                and all(
                    attempt.telemetry is not None and attempt.telemetry.latency_ms is not None
                    for attempt in attempts
                )
                else None
            ),
            "attempt_ms": latency[solution_id],
            "queue_wait_ms": (
                sum(
                    (
                        attempt.queue_wait_ms
                        for attempt in attempts
                        if attempt.queue_wait_ms is not None
                    ),
                    Decimal(0),
                )
                if attempts and all(attempt.queue_wait_ms is not None for attempt in attempts)
                else None
            ),
            "cooldown_wait_ms": (
                sum(
                    (
                        attempt.cooldown_wait_ms
                        for attempt in attempts
                        if attempt.cooldown_wait_ms is not None
                    ),
                    Decimal(0),
                )
                if attempts and all(attempt.cooldown_wait_ms is not None for attempt in attempts)
                else None
            ),
            "end_to_end_elapsed_ms": (
                max(Decimal(0), Decimal(str((max(finished) - created_at).total_seconds())) * 1000)
                if created_at is not None and finished
                else None
            ),
        }
        uncertainty_reasons: set[str] = set()
        for attempt in attempts:
            if attempt.telemetry is None:
                uncertainty_reasons.add("One or more attempts have no observed provider usage.")
            elif not attempt.telemetry.usage.complete:
                uncertainty_reasons.add(
                    attempt.telemetry.usage.uncertainty or "Provider usage is incomplete."
                )
        usage_uncertainty[solution_id] = tuple(sorted(uncertainty_reasons))
        dimensions: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for attempt in attempts:
            if attempt.telemetry:
                for dimension, amount in attempt.telemetry.usage.dimensions.items():
                    dimensions[dimension] += amount
        usage[solution_id] = dict(sorted(dimensions.items()))
        total = Decimal(len(grouped))
        reliability[solution_id] = {
            "success_ratio": Decimal(len(succeeded)) / total if total else Decimal("0"),
            "poisoned_tasks": Decimal(
                sum(result.status is TaskStatus.POISONED for result in grouped)
            ),
            "total_attempts": Decimal(sum(result.attempts for result in grouped)),
        }

    for result in results:
        for evidence in result.evidence:
            evidence_links[f"{result.task_id}:{evidence.role}"] = (
                f"/api/artifacts/{evidence.artifact_id}"
            )
        for attempt in attempt_history.get(result.task_id, ()):
            for evidence in attempt.evidence:
                evidence_links[
                    f"{result.task_id}:attempt:{attempt.attempt_number}:{evidence.role}"
                ] = f"/api/artifacts/{evidence.artifact_id}"
    status = (
        RunStatus.COMPLETED
        if all(result.status is TaskStatus.SUCCEEDED for result in results)
        else RunStatus.COMPLETED_WITH_FAILURES
    )
    return RunReport(
        schema_version="1",
        run_id=run_id,
        manifest_artifact_id=manifest_artifact_id,
        status=status,
        generated_at=utc_now(),
        results=results,
        attempt_history=attempt_history,
        quality_comparability=quality_comparability,
        quality=quality,
        cost=cost,
        cost_currency=cost_currency,
        latency=latency,
        usage=usage,
        reliability=reliability,
        evidence_links=evidence_links,
        coverage=coverage,
        latency_breakdown=latency_breakdown,
        usage_uncertainty=usage_uncertainty,
    )


def report_csv(report: RunReport) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        [
            "run_id",
            "task_id",
            "sample_id",
            "solution_id",
            "status",
            "attempts",
            "quality_metrics_json",
            "attempt_history_json",
            "cost_amount",
            "cost_currency",
            "latency_ms",
            "usage_json",
            "evidence_json",
            "error",
            "manifest_artifact_id",
        ]
    )
    for result in report.results:
        attempts = report.attempt_history.get(result.task_id, ())
        attempt_telemetry = [
            attempt.telemetry for attempt in attempts if attempt.telemetry is not None
        ]
        attempt_durations = [
            attempt.duration_ms for attempt in attempts if attempt.duration_ms is not None
        ]
        costs = [item.cost.amount for item in attempt_telemetry if item.cost.amount is not None]
        cost_unknown = any(
            attempt.telemetry is None or attempt.telemetry.cost.amount is None
            for attempt in attempts
        )
        task_usage: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for item in attempt_telemetry:
            for name, amount in item.usage.dimensions.items():
                task_usage[name] += amount
        writer.writerow(
            [
                report.run_id,
                result.task_id,
                result.sample_id,
                result.solution_id,
                result.status.value,
                result.attempts,
                json.dumps(
                    {
                        metric.metric_id: (None if metric.value is None else str(metric.value))
                        for metric in result.metrics
                        if metric.family.value == "quality"
                    },
                    sort_keys=True,
                ),
                json.dumps(
                    [attempt.model_dump(mode="json") for attempt in attempts],
                    sort_keys=True,
                ),
                ("" if cost_unknown or not attempts else str(sum(costs, Decimal("0")))),
                ("" if not attempt_telemetry else attempt_telemetry[0].cost.currency),
                (
                    ""
                    if not attempts or len(attempt_durations) != len(attempts)
                    else str(sum(attempt_durations, Decimal("0")))
                ),
                json.dumps(
                    {name: str(amount) for name, amount in task_usage.items()},
                    sort_keys=True,
                ),
                json.dumps(
                    {
                        key: link
                        for key, link in report.evidence_links.items()
                        if key.startswith(f"{result.task_id}:")
                    },
                    sort_keys=True,
                ),
                result.error or "",
                report.manifest_artifact_id,
            ]
        )
    return output.getvalue()


def write_report_files(report: RunReport, root: Path) -> tuple[Path, Path]:
    destination = root.resolve() / report.run_id
    destination.mkdir(parents=True, exist_ok=True)
    json_path = destination / "report.json"
    csv_path = destination / "report.csv"
    json_content = report.model_dump_json(indent=2).encode("utf-8")
    csv_content = report_csv(report).encode("utf-8")
    _write_immutable(json_path, json_content)
    _write_immutable(csv_path, csv_content)
    return json_path, csv_path


def _write_immutable(path: Path, content: bytes) -> None:
    try:
        _write_once(path, content, verify_existing=True)
    except FileExistsError:
        raise FileExistsError(
            f"immutable report already exists with different content: {path}"
        ) from None
