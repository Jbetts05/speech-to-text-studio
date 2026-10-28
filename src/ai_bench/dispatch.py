"""Availability-first recovery without inventing a response or known usage."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from ai_bench.contracts import (
    ProviderFailureEvidence,
    RetryDelay,
    RunManifest,
    TelemetryEvidence,
    Usage,
)

INDETERMINATE_MESSAGE = (
    "A request-start checkpoint has no recorded response. Submission, usage and billing are "
    "indeterminate; availability-first recovery may duplicate or overlap the original request."
)


def indeterminate_evidence(
    manifest: RunManifest,
    solution_id: str,
    attempt_number: int,
    started_at: datetime,
    finished_at: datetime,
) -> tuple[ProviderFailureEvidence, TelemetryEvidence]:
    usage = Usage(dimensions={}, complete=False, uncertainty=INDETERMINATE_MESSAGE)
    delay = min(
        manifest.retry_policy.fallback_max_seconds,
        manifest.retry_policy.fallback_initial_seconds * 2 ** max(0, attempt_number - 1),
    )
    failure = ProviderFailureEvidence(
        classification="transport",
        message=INDETERMINATE_MESSAGE,
        retryable=attempt_number < manifest.retry_limit,
        usage=usage,
        retry_delay=RetryDelay(
            scheduled_seconds=Decimal(delay),
            source="fallback",
            fallback_reason="Availability-first recovery after an unconfirmed request start.",
        ),
    )
    telemetry = TelemetryEvidence(
        started_at=started_at,
        finished_at=finished_at,
        latency_ms=None,
        usage=usage,
        cost=manifest.pricing_snapshots[solution_id].estimate(usage),
    )
    return failure, telemetry
