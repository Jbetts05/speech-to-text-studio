from __future__ import annotations

from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest

from ai_bench.contracts import JsonValue, SolutionDescriptor
from benchmark_packs.speech_to_text.pack import get_pack
from benchmark_packs.speech_to_text.pricing import (
    CHECKED_AT,
    MAI_MODELS,
    SPEECH_PRICING,
    SpeechPricing,
)

PROFILES = [
    ("azure-speech-fast", "standard-fast", None, "swedencentral", "0.36", "Fast Transcription"),
    (
        "gpt-transcribe",
        "gpt-transcribe",
        "2026-07-28",
        "swedencentral",
        "0.27",
        "gpt-transcribe Gl",
    ),
    (
        "managed-whisper",
        "whisper",
        "001",
        "swedencentral",
        "0.4356",
        "Speech-to-Text-Batch-Whisper-regnl",
    ),
    ("mai-transcribe-1-5", "MAI-Transcribe-1.5", None, "eastus", "0.36", "Fast Transcription"),
    ("mai-transcribe-2", "MAI-Transcribe-2", None, "eastus", "0.36", "Fast Transcription"),
]


def descriptor(identifier: str, model: str, version: str | None) -> SolutionDescriptor:
    solution = next(solution for solution in get_pack().solutions if solution.id == identifier)
    return SolutionDescriptor(
        id=solution.id,
        version=solution.version,
        pipeline=solution.pipeline,
        compatibility=solution.compatibility,
        pricing_snapshot=solution.pricing,
        settings={"model": model, "deployment_model_version": version},
    )


@pytest.mark.parametrize("identifier,model,version,region,price,sku", PROFILES)
def test_exact_model_region_and_meter_have_dated_separate_estimates(
    identifier: str, model: str, version: str | None, region: str, price: str, sku: str
) -> None:
    pinned = descriptor(identifier, model, version)
    original = pinned.model_dump_json()
    quote = SpeechPricing().quote(pinned)
    assert pinned.model_dump_json() == original
    assert not pinned.pricing_snapshot.rates
    assert quote.pricing is not None
    assert quote.checked_at == CHECKED_AT
    assert quote.pricing.region == region
    assert quote.pricing.currency == "USD"
    assert quote.pricing.rates[0].dimension == "submitted_audio_seconds"
    assert quote.pricing.rates[0].unit_size == Decimal(3600)
    assert quote.pricing.rates[0].price == Decimal(price)
    query = parse_qs(urlparse(quote.source_urls[0]).query)["$filter"][0]
    assert f"skuName eq '{sku}'" in query
    assert "tierMinimumUnits eq 0" in query
    assert f"armRegionName eq '{region}'" in query
    assert "Conditional" in quote.basis
    assert "Not billed cost" in " ".join(quote.assumptions)
    assert "retries" in " ".join(quote.assumptions)
    if identifier.startswith("mai-"):
        assert SPEECH_PRICING in quote.source_urls
        assert MAI_MODELS in quote.source_urls
        assert "footnote 9" in " ".join(quote.assumptions)


@pytest.mark.parametrize("identifier,model,version,region,price,sku", PROFILES)
def test_nearby_models_versions_and_regions_are_not_silently_substituted(
    identifier: str, model: str, version: str | None, region: str, price: str, sku: str
) -> None:
    pinned = descriptor(identifier, model, version)
    for changed in (
        pinned.model_copy(update={"settings": {**pinned.settings, "model": "gpt-4o-transcribe"}}),
        pinned.model_copy(
            update={"settings": {**pinned.settings, "deployment_model_version": "not-reviewed"}}
        ),
        pinned.model_copy(
            update={"pricing_snapshot": pinned.pricing_snapshot.model_copy(update={"region": "x"})}
        ),
        pinned.model_copy(update={"id": "different-solution"}),
        pinned.model_copy(
            update={"pipeline": pinned.pipeline.model_copy(update={"adapter_id": "x"})}
        ),
    ):
        quote = SpeechPricing().quote(changed)
        assert quote.pricing is None
        assert quote.unavailable_reason is not None


@pytest.mark.parametrize("deployment_type", ["DataZoneStandard", "Standard", [], {}])
def test_incompatible_or_malformed_deployment_type_is_not_guessed(
    deployment_type: JsonValue,
) -> None:
    pinned = descriptor("gpt-transcribe", "gpt-transcribe", "2026-07-28")
    quote = SpeechPricing().quote(
        pinned.model_copy(
            update={"settings": {**pinned.settings, "deployment_type": deployment_type}}
        )
    )
    assert quote.pricing is None
    assert "deployment type" in (quote.unavailable_reason or "")


@pytest.mark.parametrize(
    "identifier,model,version,tier",
    [
        ("gpt-transcribe", "gpt-transcribe", "2026-07-28", "GlobalStandard"),
        ("managed-whisper", "whisper", "001", "Standard"),
    ],
)
def test_unpinned_tier_is_an_explicit_conditional_assumption(
    identifier: str, model: str, version: str, tier: str
) -> None:
    pinned = descriptor(identifier, model, version)
    conditional = SpeechPricing().quote(pinned)
    assert conditional.pricing is not None
    assert "historical manifest does not record deployment type" in " ".join(
        conditional.assumptions
    )
    assert tier in " ".join(conditional.assumptions)
    declared = SpeechPricing().quote(
        pinned.model_copy(update={"settings": {**pinned.settings, "deployment_type": tier}})
    )
    assert declared.pricing == conditional.pricing
    assert "deployment type is pinned" in " ".join(declared.assumptions)
