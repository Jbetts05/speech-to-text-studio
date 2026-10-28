"""Validate immutable evaluation evidence without reading live task records."""

from __future__ import annotations

from typing import Literal

from ai_bench.contracts import (
    AttemptStatus,
    RunManifest,
    RunReport,
    RunStatus,
    SampleRole,
    TaskStatus,
)
from ai_bench.hashing import stable_task_id
from ai_bench.repositories import RunRecord

TERMINAL_RUN_STATUSES = {
    RunStatus.COMPLETED,
    RunStatus.COMPLETED_WITH_FAILURES,
    RunStatus.FAILED,
}


class FinalizedIntegrityError(ValueError):
    pass


def validate_finalized_report(
    run_id: str, run: RunRecord, manifest: RunManifest, report: RunReport
) -> Literal["incomplete_results", "incomplete_attempt_history"] | None:
    if (
        manifest.run_id != run_id
        or manifest != run.manifest
        or report.run_id != run_id
        or report.manifest_artifact_id != run.manifest_artifact_id
        or report.status != run.status
        or run.status not in TERMINAL_RUN_STATUSES
    ):
        raise FinalizedIntegrityError("Current run, immutable manifest and report disagree.")
    samples = manifest.sample_ids
    solutions = manifest.solution_ids
    if (
        len(set(samples)) != len(samples)
        or len(set(solutions)) != len(solutions)
        or len({sample.id for sample in manifest.samples}) != len(manifest.samples)
        or tuple(sample.id for sample in manifest.samples if sample.role is SampleRole.SCORED)
        != samples
        or set(manifest.solution_descriptors) != set(solutions)
        or any(key != descriptor.id for key, descriptor in manifest.solution_descriptors.items())
        or set(manifest.pricing_snapshots) != set(solutions)
        or any(
            manifest.pricing_snapshots[key] != descriptor.pricing_snapshot
            for key, descriptor in manifest.solution_descriptors.items()
        )
    ):
        raise FinalizedIntegrityError("Manifest sample, solution or pricing identities disagree.")
    expected = {
        stable_task_id(run_id, sample, solution): (sample, solution)
        for sample in samples
        for solution in solutions
    }
    results = {result.task_id: result for result in report.results}
    if len(results) != len(report.results) or set(results) - set(expected):
        raise FinalizedIntegrityError("Report contains duplicate or unexpected task identities.")
    if set(report.attempt_history) - set(expected):
        raise FinalizedIntegrityError("Report attempt history belongs to an unexpected task.")
    for task_id, history in report.attempt_history.items():
        numbers = [attempt.attempt_number for attempt in history]
        if numbers != sorted(set(numbers)) or any(
            attempt.task_id != task_id
            or any(
                evidence.attempt_number not in {None, attempt.attempt_number}
                for evidence in attempt.evidence
            )
            for attempt in history
        ):
            raise FinalizedIntegrityError("Report attempt identities or ordering disagree.")
    incomplete_history = False
    for result in report.results:
        if (
            (result.sample_id, result.solution_id) != expected[result.task_id]
            or result.status not in {TaskStatus.SUCCEEDED, TaskStatus.POISONED}
            or not 1 <= result.attempts <= manifest.retry_limit
        ):
            raise FinalizedIntegrityError(
                "Report result identity, outcome or attempt count is invalid."
            )
        history = report.attempt_history.get(result.task_id, ())
        numbers = [attempt.attempt_number for attempt in history]
        if any(number > result.attempts for number in numbers):
            raise FinalizedIntegrityError("Report attempt count disagrees with its result.")
        if numbers != list(range(1, result.attempts + 1)):
            incomplete_history = True
        if any(
            attempt.status is AttemptStatus.RUNNING or attempt.finished_at is None
            for attempt in history
        ):
            incomplete_history = True
        if history and numbers[-1] == result.attempts:
            final = history[-1]
            if (
                result.status is TaskStatus.SUCCEEDED
                and final.status is not AttemptStatus.SUCCEEDED
            ) or (result.status is TaskStatus.POISONED and final.status is AttemptStatus.SUCCEEDED):
                raise FinalizedIntegrityError("Report outcome disagrees with its last attempt.")
    if run.status is RunStatus.COMPLETED and any(
        result.status is not TaskStatus.SUCCEEDED for result in report.results
    ):
        raise FinalizedIntegrityError("Completed report contains failed results.")
    if set(results) != set(expected):
        return "incomplete_results"
    if run.status is RunStatus.COMPLETED_WITH_FAILURES and all(
        result.status is TaskStatus.SUCCEEDED for result in report.results
    ):
        raise FinalizedIntegrityError("Report failure status disagrees with its result matrix.")
    if incomplete_history:
        return "incomplete_attempt_history"
    return None
