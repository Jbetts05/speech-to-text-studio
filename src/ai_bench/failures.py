"""Typed attempt failures and bounded provider-directed retry delays."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime

from ai_bench.contracts import Modality, ProviderFailureEvidence, RetryDelay, RetryPolicy


class ProviderFailure(RuntimeError):
    def __init__(
        self,
        evidence: ProviderFailureEvidence,
        *,
        raw: bytes | None = None,
        media_type: str = "application/json",
        modality: Modality = Modality.STRUCTURED,
    ) -> None:
        self.evidence = evidence
        self.raw = raw
        self.media_type = media_type
        self.modality = modality
        super().__init__(evidence.message)


def retry_delay(
    headers: Mapping[str, str],
    *,
    provider_message: str | None,
    policy: RetryPolicy,
    attempt_number: int,
    now: datetime | None = None,
) -> RetryDelay:
    normalized = {key.casefold(): value.strip() for key, value in headers.items()}
    candidates: list[tuple[Decimal, str]] = []
    rejected = False
    if value := normalized.get("retry-after"):
        try:
            seconds = Decimal(value)
            candidates.append((seconds, "retry-after-seconds"))
        except InvalidOperation:
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    raise ValueError("Retry-After date has no timezone")
                seconds = Decimal(str((date - (now or datetime.now(UTC))).total_seconds()))
                candidates.append((max(Decimal(0), seconds), "retry-after-date"))
            except (ValueError, TypeError, OverflowError):
                rejected = True
    for header in ("retry-after-ms", "x-ms-retry-after-ms"):
        if value := normalized.get(header):
            try:
                candidates.append((Decimal(value) / 1000, header))
            except InvalidOperation:
                rejected = True
    if provider_message is not None:
        match = re.search(
            r"\bplease retry after ([0-9]+(?:\.[0-9]+)?) seconds\b",
            provider_message[:16384],
            flags=re.IGNORECASE,
        )
        if match:
            candidates.append((Decimal(match[1]), "provider-message"))
    for seconds, source in candidates:
        if seconds.is_finite() and 0 <= seconds <= policy.maximum_provider_delay_seconds:
            return RetryDelay.model_validate(
                {"scheduled_seconds": seconds, "provider_seconds": seconds, "source": source},
                strict=True,
            )
        rejected = True
    fallback = min(
        policy.fallback_max_seconds,
        policy.fallback_initial_seconds * 2 ** max(0, min(attempt_number - 1, 20)),
    )
    return RetryDelay(
        scheduled_seconds=Decimal(fallback),
        source="fallback",
        fallback_reason="invalid or out-of-bounds provider delay"
        if rejected
        else "provider delay absent",
    )
