from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from ai_bench.contracts import (
    CostEstimate,
    PricingModel,
    PricingRate,
    RunReport,
    RunStatus,
    Usage,
)
from ai_bench.hashing import canonical_json, sha256_digest


def test_canonical_json_hash_is_order_independent() -> None:
    left = {"b": [Decimal("1.00"), True], "a": "value"}
    right = {"a": "value", "b": [Decimal("1.00"), True]}
    assert canonical_json(left) == canonical_json(right)
    assert sha256_digest(left) == sha256_digest(right)


def test_contracts_are_strict_and_immutable() -> None:
    with pytest.raises(ValidationError):
        Usage(dimensions={"calls": "1"})  # type: ignore[dict-item]
    usage = Usage(dimensions={"calls": Decimal("1")})
    with pytest.raises(ValidationError):
        usage.dimensions = {}  # type: ignore[misc]


def test_unknown_cost_is_not_converted_to_zero() -> None:
    pricing = PricingModel(
        id="partial",
        version="v1",
        currency="USD",
        region="test",
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        source="test declaration",
        rates=(PricingRate(dimension="calls", unit_size=Decimal("1"), price=Decimal("0")),),
    )
    estimate = pricing.estimate(Usage(dimensions={"tokens": Decimal("10")}))
    assert estimate.amount is None
    assert estimate.reason
    empty_estimate = pricing.estimate(Usage())
    assert empty_estimate.amount is None
    assert empty_estimate.reason == "usage is empty and cannot establish a zero cost"
    explicit_zero = pricing.estimate(Usage(dimensions={"calls": Decimal("0")}))
    assert explicit_zero.amount == Decimal("0")
    assert explicit_zero.reason is None
    with pytest.raises(ValidationError):
        CostEstimate(
            amount=None,
            currency="USD",
            pricing_snapshot="partial@v1",
            reason=None,
        )


def test_report_rejects_universal_score() -> None:
    payload = {
        "schema_version": "1",
        "run_id": "a" * 64,
        "manifest_artifact_id": "b" * 64,
        "status": RunStatus.COMPLETED,
        "generated_at": datetime.now(UTC),
        "results": (),
        "quality": {},
        "cost": {},
        "latency": {},
        "usage": {},
        "reliability": {},
        "evidence_links": {},
        "overall_score": Decimal("1"),
    }
    with pytest.raises(ValidationError, match="universal score"):
        RunReport.model_validate(payload)
