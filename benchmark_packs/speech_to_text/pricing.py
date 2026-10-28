"""Reviewed list-rate estimates; never a replacement for immutable billed evidence."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import quote

from ai_bench.contracts import PricingModel, PricingRate, SolutionDescriptor
from ai_bench.costs import PricingQuote

CHECKED_AT = datetime(2026, 9, 19, tzinfo=UTC)
RETAIL = "https://prices.azure.com/api/retail/prices?api-version=2023-01-01-preview"
SPEECH_PRICING = "https://azure.microsoft.com/en-us/pricing/details/speech/"
OPENAI_PRICING = "https://azure.microsoft.com/en-us/pricing/details/azure-openai/"
MAI_MODELS = "https://learn.microsoft.com/en-us/azure/ai-services/speech-service/mai-transcribe"


class SpeechPricing:
    def quote(self, descriptor: SolutionDescriptor) -> PricingQuote:
        settings = descriptor.settings
        model = settings.get("model")
        region = descriptor.pricing_snapshot.region
        expected = {
            "azure-speech-fast": ("standard-fast", None, "swedencentral"),
            "mai-transcribe-1-5": ("MAI-Transcribe-1.5", None, "eastus"),
            "mai-transcribe-2": ("MAI-Transcribe-2", None, "eastus"),
            "gpt-transcribe": ("gpt-transcribe", "2026-07-28", "swedencentral"),
            "managed-whisper": ("whisper", "001", "swedencentral"),
        }.get(descriptor.id)
        basis = "Conditional, pro-rata list-rate estimate of recorded submitted audio seconds."
        if (
            expected is None
            or (model, settings.get("deployment_model_version"), region) != expected
            or descriptor.pipeline.adapter_id != descriptor.id
        ):
            return PricingQuote(
                pricing=None,
                checked_at=CHECKED_AT,
                basis=basis,
                unavailable_reason=(
                    f"No exact reviewed meter mapping for pinned model {model!s}, version "
                    f"{settings.get('deployment_model_version')!s}, region {region}."
                ),
            )
        assumptions = [
            "Not billed cost. Applies the public pay-as-you-go list rate, without discounts, "
            "promotional eligibility, taxes, minimum charges or billing-rounding adjustments.",
            "Includes all recorded submitted duration, including retries and failed attempts; "
            "a failed submission might not be billed. Unobserved request starts are excluded "
            "and make coverage partial.",
        ]
        if descriptor.id == "gpt-transcribe":
            product, sku, price = "Azure OpenAI Media", "gpt-transcribe Gl", "0.27"
            meter = "gpt-transcribe Gl Unit"
            effective = datetime(2026, 8, 1, tzinfo=UTC)
            deployment_type = "GlobalStandard"
            source = OPENAI_PRICING
        elif descriptor.id == "managed-whisper":
            product, sku, price = "Azure OpenAI", "Speech-to-Text-Batch-Whisper-regnl", "0.4356"
            meter = "Speech-to-Text-Batch-Whisper-regnl Unit"
            effective = datetime(2026, 3, 1, tzinfo=UTC)
            deployment_type = "Standard"
            source = OPENAI_PRICING
        else:
            product, sku, price = "Azure Speech", "Fast Transcription", "0.36"
            meter = "Fast Transcription Speech To Text"
            effective = datetime(2024, 11, 1, tzinfo=UTC)
            deployment_type = None
            source = SPEECH_PRICING
            if str(model).startswith("MAI-"):
                assumptions.append(
                    "Microsoft Speech pricing footnote 9 explicitly assigns LLM Speech "
                    "(enhanced-mode MAI) the same price and SKU as Fast Transcription."
                )
        if deployment_type is not None:
            declared = settings.get("deployment_type")
            if declared is not None and declared != deployment_type:
                return PricingQuote(
                    pricing=None,
                    checked_at=CHECKED_AT,
                    basis=basis,
                    unavailable_reason=(
                        f"No reviewed {model!s} rate for deployment type {declared!s}."
                    ),
                )
            assumptions.append(
                f"Uses the benchmark's declared {deployment_type} profile. "
                + (
                    "The historical manifest does not record deployment type; verify this "
                    "assumption before using the estimate for budgeting."
                    if declared is None
                    else "The deployment type is pinned in the manifest."
                )
            )
        query = (
            f"armRegionName eq '{region}' and productName eq '{product}' "
            f"and skuName eq '{sku}' and meterName eq '{meter}' "
            "and priceType eq 'Consumption' and tierMinimumUnits eq 0"
        )
        return PricingQuote(
            pricing=PricingModel(
                id=f"{descriptor.id}-submitted-audio-retail",
                version="2026-09-19",
                currency="USD",
                region=region,
                effective_at=effective,
                source=(
                    f"Azure Retail Prices API: product {product}; SKU {sku}; meter {meter}; "
                    "Consumption tier 0; 1 Hour."
                ),
                rates=(
                    PricingRate(
                        dimension="submitted_audio_seconds",
                        unit_size=Decimal(3600),
                        price=Decimal(price),
                    ),
                ),
            ),
            checked_at=CHECKED_AT,
            basis=basis,
            source_urls=(
                RETAIL + "&currencyCode=%27USD%27&$filter=" + quote(query, safe=""),
                source,
            )
            + ((MAI_MODELS,) if str(model).startswith("MAI-") else ()),
            assumptions=tuple(assumptions),
        )
