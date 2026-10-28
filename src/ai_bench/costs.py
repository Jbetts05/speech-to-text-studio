"""Read-only cost coverage and explicitly separate current-rate estimates."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable

from pydantic import Field

from ai_bench.contracts import (
    Contract,
    PricingModel,
    RunManifest,
    RunReport,
    SolutionDescriptor,
    TaskAttempt,
    TaskStatus,
    utc_now,
)
from ai_bench.repositories import TaskRecord


class PricingQuote(Contract):
    pricing: PricingModel | None
    checked_at: datetime
    basis: str
    source_urls: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    unavailable_reason: str | None = None


@runtime_checkable
class CurrentPricingProvider(Protocol):
    def quote(self, descriptor: SolutionDescriptor) -> PricingQuote: ...


class CostCoverage(Contract):
    status: Literal["known", "partial", "unknown"]
    amounts: Mapping[str, Decimal | None]
    covered_attempts: int = Field(ge=0)
    total_attempts: int = Field(ge=0)
    terminal_tasks: int = Field(ge=0)
    total_tasks: int = Field(ge=0)
    reasons: tuple[str, ...] = ()


class SampleCost(Contract):
    solution_id: str
    sample_id: str | None = None
    recorded: CostCoverage
    current_estimate: CostCoverage
    recorded_usage: Mapping[str, Decimal]
    usage_uncertainty: tuple[str, ...]


class SolutionCost(SampleCost):
    pinned_pricing: PricingModel
    current_pricing: PricingQuote | None
    recorded_pricing_snapshots: tuple[str, ...]


class RunCostSummary(Contract):
    schema_version: Literal["1"] = "1"
    run_id: str
    manifest_artifact_id: str
    assessed_at: datetime
    solutions: tuple[SolutionCost, ...]
    samples: Mapping[str, tuple[SampleCost, ...]]
    recorded_total: CostCoverage
    current_estimate_total: CostCoverage
    note: str = (
        "Recorded costs retain the original attempt evidence. Current-rate estimates are "
        "separate, conditional views of recorded usage, not original prices or billed charges. "
        "Original manifests and reports are unchanged."
    )


@dataclass(frozen=True)
class _CostTask:
    """Coverage facts, independent of live scheduler records."""

    id: str
    sample_id: str
    solution_id: str
    attempts: int
    terminal: bool


def _coverage(
    observations: Sequence[tuple[str, Decimal | None]],
    *,
    total_attempts: int,
    terminal_tasks: int,
    total_tasks: int,
    reasons: set[str],
    currencies: set[str],
) -> CostCoverage:
    amounts: dict[str, Decimal | None] = dict.fromkeys(sorted(currencies))
    covered = 0
    for currency, amount in observations:
        amounts.setdefault(currency, None)
        if amount is not None:
            covered += 1
            amounts[currency] = (amounts[currency] or Decimal(0)) + amount
    complete = (
        total_attempts > 0
        and covered == total_attempts
        and total_tasks > 0
        and terminal_tasks == total_tasks
    )
    if terminal_tasks != total_tasks:
        reasons.add("Work is incomplete; any amount is a subtotal, not a final run total.")
    if total_attempts == 0:
        reasons.add("No attempt usage has been recorded.")
    return CostCoverage(
        status="known" if complete else "partial" if covered else "unknown",
        amounts=amounts,
        covered_attempts=covered,
        total_attempts=total_attempts,
        terminal_tasks=terminal_tasks,
        total_tasks=total_tasks,
        reasons=tuple(sorted(reasons)),
    )


def _solution_cost(
    descriptor: SolutionDescriptor,
    tasks: Sequence[_CostTask],
    histories: Mapping[str, tuple[TaskAttempt, ...]],
    quote: PricingQuote | None,
    *,
    sample_id: str | None = None,
    expected_tasks: int,
) -> SolutionCost:
    attempts = tuple(attempt for task in tasks for attempt in histories.get(task.id, ()))
    total_attempts = sum(max(task.attempts, len(histories.get(task.id, ()))) for task in tasks)
    terminal = sum(task.terminal for task in tasks)
    recorded: list[tuple[str, Decimal | None]] = []
    estimated: list[tuple[str, Decimal | None]] = []
    recorded_reasons: set[str] = set()
    estimate_reasons: set[str] = set()
    usage: dict[str, Decimal] = {}
    uncertainty: set[str] = set()
    currency = descriptor.pricing_snapshot.currency
    current = None if quote is None else quote.pricing
    estimate_currency = currency if current is None else current.currency
    if not descriptor.pricing_snapshot.rates:
        recorded_reasons.add("The immutable run has no pinned price rates.")
    if current is None:
        estimate_reasons.add(
            (None if quote is None else quote.unavailable_reason)
            or "No reviewed current pricing is available for this pinned configuration."
        )
    elif not current.rates:
        estimate_reasons.add("The current pricing quote has no reviewed rates.")
    if len(attempts) < total_attempts:
        recorded_reasons.add("Some attempt history is missing; a complete cost is unsupported.")
        estimate_reasons.add("Some attempt history is missing; a complete estimate is unsupported.")
        uncertainty.add("Usage is not recorded for every attempt.")
    for attempt in attempts:
        telemetry = attempt.telemetry
        if telemetry is None:
            recorded.append((currency, None))
            estimated.append((estimate_currency, None))
            recorded_reasons.add("One or more attempts have no cost telemetry.")
            estimate_reasons.add("One or more attempts have no recorded usage to price.")
            uncertainty.add("Usage is not recorded for every attempt.")
            continue
        recorded.append((telemetry.cost.currency, telemetry.cost.amount))
        if telemetry.cost.amount is None:
            recorded_reasons.add(telemetry.cost.reason or "An attempt cost is unknown.")
        for dimension, value in telemetry.usage.dimensions.items():
            usage[dimension] = usage.get(dimension, Decimal(0)) + value
        if not telemetry.usage.complete:
            uncertainty.add(telemetry.usage.uncertainty or "Provider usage is incomplete.")
        if current is None or not current.rates:
            estimated.append((estimate_currency, None))
        elif any(rate.dimension not in telemetry.usage.dimensions for rate in current.rates):
            estimated.append((estimate_currency, None))
            estimate_reasons.add("A required priced usage dimension is missing for an attempt.")
        else:
            estimated.append(
                (
                    estimate_currency,
                    sum(
                        (
                            telemetry.usage.dimensions[rate.dimension] / rate.unit_size * rate.price
                            for rate in current.rates
                        ),
                        Decimal(0),
                    ),
                )
            )
    return SolutionCost(
        solution_id=descriptor.id,
        sample_id=sample_id,
        recorded=_coverage(
            recorded,
            total_attempts=total_attempts,
            terminal_tasks=terminal,
            total_tasks=expected_tasks,
            reasons=recorded_reasons,
            currencies=(
                {observation[0] for observation in recorded}
                | ({currency} if len(attempts) < total_attempts or not attempts else set())
            ),
        ),
        current_estimate=_coverage(
            estimated,
            total_attempts=total_attempts,
            terminal_tasks=terminal,
            total_tasks=expected_tasks,
            reasons=estimate_reasons,
            currencies={estimate_currency},
        ),
        pinned_pricing=descriptor.pricing_snapshot,
        current_pricing=quote,
        recorded_pricing_snapshots=tuple(
            sorted(
                {
                    attempt.telemetry.cost.pricing_snapshot
                    for attempt in attempts
                    if attempt.telemetry is not None
                }
            )
        ),
        recorded_usage=usage,
        usage_uncertainty=tuple(sorted(uncertainty)),
    )


def _total(lines: Sequence[SolutionCost], *, estimate: bool) -> CostCoverage:
    values = [line.current_estimate if estimate else line.recorded for line in lines]
    currencies = {currency for value in values for currency in value.amounts}
    amounts: dict[str, Decimal | None] = dict.fromkeys(sorted(currencies))
    for value in values:
        for currency, amount in value.amounts.items():
            if amount is not None:
                amounts[currency] = (amounts[currency] or Decimal(0)) + amount
    covered = sum(value.covered_attempts for value in values)
    return CostCoverage(
        status="known"
        if values and all(value.status == "known" for value in values)
        else "partial"
        if covered
        else "unknown",
        amounts=amounts,
        covered_attempts=covered,
        total_attempts=sum(value.total_attempts for value in values),
        terminal_tasks=sum(value.terminal_tasks for value in values),
        total_tasks=sum(value.total_tasks for value in values),
        reasons=tuple(sorted({reason for value in values for reason in value.reasons})),
    )


def build_cost_summary(
    manifest: RunManifest,
    manifest_artifact_id: str,
    tasks: Sequence[TaskRecord],
    histories: Mapping[str, tuple[TaskAttempt, ...]],
    provider: CurrentPricingProvider | None = None,
    report: RunReport | None = None,
) -> RunCostSummary:
    if report is not None and report.attempt_history:
        histories = report.attempt_history
    return _build_cost_summary(
        manifest,
        manifest_artifact_id,
        tuple(
            _CostTask(
                task.id,
                task.sample_id,
                task.solution_id,
                task.attempts,
                task.status in {TaskStatus.SUCCEEDED, TaskStatus.POISONED},
            )
            for task in tasks
        ),
        histories,
        provider,
    )


def build_finalized_cost_summary(
    manifest: RunManifest,
    report: RunReport,
    provider: CurrentPricingProvider | None = None,
) -> RunCostSummary:
    return _build_cost_summary(
        manifest,
        report.manifest_artifact_id,
        tuple(
            _CostTask(
                result.task_id,
                result.sample_id,
                result.solution_id,
                result.attempts,
                result.status in {TaskStatus.SUCCEEDED, TaskStatus.POISONED},
            )
            for result in report.results
        ),
        report.attempt_history,
        provider,
    )


def _build_cost_summary(
    manifest: RunManifest,
    manifest_artifact_id: str,
    tasks: Sequence[_CostTask],
    histories: Mapping[str, tuple[TaskAttempt, ...]],
    provider: CurrentPricingProvider | None,
) -> RunCostSummary:
    quotes = {
        identifier: None
        if provider is None
        else provider.quote(manifest.solution_descriptors[identifier])
        for identifier in manifest.solution_ids
    }
    solutions = tuple(
        _solution_cost(
            manifest.solution_descriptors[identifier],
            [task for task in tasks if task.solution_id == identifier],
            histories,
            quotes[identifier],
            expected_tasks=len(manifest.sample_ids),
        )
        for identifier in manifest.solution_ids
    )
    samples = {
        sample: tuple(
            SampleCost.model_validate(
                _solution_cost(
                    manifest.solution_descriptors[identifier],
                    [
                        task
                        for task in tasks
                        if task.solution_id == identifier and task.sample_id == sample
                    ],
                    histories,
                    quotes[identifier],
                    sample_id=sample,
                    expected_tasks=1,
                ).model_dump(
                    exclude={"pinned_pricing", "current_pricing", "recorded_pricing_snapshots"}
                )
            )
            for identifier in manifest.solution_ids
        )
        for sample in manifest.sample_ids
    }
    return RunCostSummary(
        run_id=manifest.run_id,
        manifest_artifact_id=manifest_artifact_id,
        assessed_at=utc_now(),
        solutions=solutions,
        samples=samples,
        recorded_total=_total(solutions, estimate=False),
        current_estimate_total=_total(solutions, estimate=True),
    )
