"""Invented, deterministic, and network-free example benchmark pack."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from ai_bench.contracts import (
    AggregateEvaluation,
    AssistedPipeline,
    BenchmarkDefinition,
    BenchmarkSample,
    Compatibility,
    CompatibilityLevel,
    DatasetProvenance,
    DatasetRevision,
    EvaluationRecord,
    EvaluationResult,
    ExecutionOutput,
    ExecutionRequest,
    JsonValue,
    MetricDefinition,
    MetricFamily,
    MetricValue,
    Modality,
    ModalityArtifact,
    ParsedOutput,
    PipelineType,
    PricingModel,
    PricingRate,
    RendererDescriptor,
    StandalonePipeline,
    Terminology,
    TextArtifact,
    Usage,
)
from ai_bench.hashing import content_digest, sha256_digest


def _text_artifact(text: str, role: str) -> TextArtifact:
    content = text.encode("utf-8")
    digest = content_digest(content)
    return TextArtifact(
        id=digest,
        modality=Modality.TEXT,
        media_type="text/plain",
        size_bytes=len(content),
        uri=f"pack://synthetic_text_intent/{role}/{digest}",
        metadata={"text": text, "synthetic": True},
    )


_EXAMPLES = (
    ("sample-001", "Please archive the cobalt note.", "archive_note"),
    ("sample-002", "Put the amber memo into the archive.", "archive_note"),
    ("sample-003", "Find my note about square clouds.", "find_note"),
    ("sample-004", "Search for the violet sketch note.", "find_note"),
    ("sample-005", "Count the triangles in the invented scene.", "count_shapes"),
    ("sample-006", "How many circles are in the pretend picture?", "count_shapes"),
)

_SAMPLES = tuple(
    BenchmarkSample(
        id=sample_id,
        input=_text_artifact(text, "input"),
        reference=_text_artifact(label, "reference"),
        metadata={"synthetic": True},
    )
    for sample_id, text, label in _EXAMPLES
)


class SyntheticDataset:
    id = "invented-intents-v1"
    revision = DatasetRevision(
        id=id,
        version="1.0.0",
        content_hash=sha256_digest(_SAMPLES),
        provenance=DatasetProvenance(
            source="Fully invented in-package text and labels",
            license="Project synthetic fixture terms",
            synthetic=True,
        ),
    )

    def samples(self) -> Sequence[BenchmarkSample]:
        return _SAMPLES

    def read_artifact(self, artifact: ModalityArtifact) -> bytes:
        text = artifact.metadata.get("text")
        if not isinstance(text, str):
            raise ValueError(f"synthetic artifact has no embedded text: {artifact.id}")
        content = text.encode("utf-8")
        if content_digest(content) != artifact.id:
            raise ValueError(f"synthetic artifact hash mismatch: {artifact.id}")
        return content


_PRICING = PricingModel(
    id="offline-zero-cost",
    version="2026-01-01",
    currency="USD",
    region="offline",
    effective_at=datetime(2026, 1, 1, tzinfo=UTC),
    source="Versioned benchmark-pack declaration for deterministic local execution",
    rates=(
        PricingRate(dimension="characters", unit_size=Decimal("1"), price=Decimal("0")),
        PricingRate(dimension="calls", unit_size=Decimal("1"), price=Decimal("0")),
    ),
)


class _BaseSolution:
    id: str
    version = "1.0.0"
    networked = False
    compatibility = Compatibility(
        level=CompatibilityLevel.FULL,
        comparable_quality=True,
        reason="Consumes text and emits one label from the complete synthetic label set.",
    )
    pricing = _PRICING

    def execute(self, request: ExecutionRequest) -> ExecutionOutput:
        text = str(request.sample.input.metadata["text"])
        prediction = self._classify(text)
        raw = json.dumps(
            {"intent": prediction, "adapter": self.id, "idempotency_key": request.idempotency_key},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        return ExecutionOutput(
            raw=raw,
            modality=Modality.STRUCTURED,
            media_type="application/json",
            usage=Usage(
                dimensions={
                    "characters": Decimal(len(text)),
                    "calls": Decimal("1"),
                }
            ),
        )

    def parse(self, output: ExecutionOutput) -> ParsedOutput:
        value = json.loads(output.raw)
        if not isinstance(value, dict) or not isinstance(value.get("intent"), str):
            raise ValueError("adapter output does not contain a string intent")
        return ParsedOutput(value={"intent": value["intent"]})

    def _classify(self, text: str) -> str:
        raise NotImplementedError


class KeywordStandaloneSolution(_BaseSolution):
    id = "keyword-standalone"
    pipeline = StandalonePipeline(
        pipeline_type=PipelineType.STANDALONE,
        adapter_id=id,
    )

    def _classify(self, text: str) -> str:
        lowered = text.lower()
        if "archive" in lowered:
            return "archive_note"
        if "find" in lowered or "search" in lowered:
            return "find_note"
        return "count_shapes"


class AssistedStyleSolution(_BaseSolution):
    id = "two-stage-assisted"
    pipeline = AssistedPipeline(
        pipeline_type=PipelineType.ASSISTED,
        adapter_id=id,
        components=("deterministic-normalizer", "deterministic-label-selector"),
    )

    def _classify(self, text: str) -> str:
        tokens = {token.strip(".,?!").lower() for token in text.split()}
        rules = (
            ({"archive"}, "archive_note"),
            ({"find", "search"}, "find_note"),
            ({"count", "many"}, "count_shapes"),
        )
        for keywords, label in rules:
            if tokens & keywords:
                return label
        return "find_note"


class IntentEvaluator:
    id = "synthetic-intent-evaluator"
    version = "1.0.0"
    metric_definitions = (
        MetricDefinition(
            id="exact_accuracy",
            name="Exact accuracy",
            family=MetricFamily.QUALITY,
            unit="ratio",
            higher_is_better=True,
            description="One when the predicted intent exactly matches the reference.",
        ),
        MetricDefinition(
            id="macro_f1",
            name="Macro F1",
            family=MetricFamily.QUALITY,
            unit="ratio",
            higher_is_better=True,
            description="Macro-averaged F1 across intent labels, aggregated in the run report.",
        ),
    )

    def evaluate(self, sample: BenchmarkSample, output: ParsedOutput) -> EvaluationResult:
        if sample.reference is None:
            raise ValueError("synthetic samples require a reference")
        expected = str(sample.reference.metadata["text"])
        if not isinstance(output.value, dict):
            raise ValueError("parsed output must be an object")
        predicted = str(output.value.get("intent", ""))
        exact = Decimal("1") if predicted == expected else Decimal("0")
        return EvaluationResult(
            metrics=(
                MetricValue(
                    metric_id="exact_accuracy",
                    family=MetricFamily.QUALITY,
                    value=exact,
                    unit="ratio",
                ),
            ),
            comparison={"expected": expected, "predicted": predicted},
        )

    def aggregate(self, records: Sequence[EvaluationRecord]) -> AggregateEvaluation:
        exact_values = [
            metric.value
            for record in records
            for metric in record.metrics
            if metric.metric_id == "exact_accuracy" and metric.value is not None
        ]
        comparisons: list[tuple[str, str]] = []
        for record in records:
            if not isinstance(record.comparison, dict):
                continue
            expected = record.comparison.get("expected")
            predicted = record.comparison.get("predicted")
            if isinstance(expected, str) and isinstance(predicted, str):
                comparisons.append((expected, predicted))
        exact_accuracy = (
            None
            if not exact_values
            else sum(exact_values, Decimal("0")) / Decimal(len(exact_values))
        )
        macro_f1 = _macro_f1(comparisons)
        return AggregateEvaluation(
            metrics=(
                MetricValue(
                    metric_id="exact_accuracy",
                    family=MetricFamily.QUALITY,
                    value=exact_accuracy,
                    unit="ratio",
                    unknown_reason=(
                        "no successful scored records" if exact_accuracy is None else None
                    ),
                ),
                MetricValue(
                    metric_id="macro_f1",
                    family=MetricFamily.QUALITY,
                    value=macro_f1,
                    unit="ratio",
                    unknown_reason=("no comparable intent records" if macro_f1 is None else None),
                ),
            )
        )


class TextViewer:
    descriptor = RendererDescriptor(
        id="plain-text",
        version="1.0.0",
        modality=Modality.TEXT,
        media_types=("text/plain",),
        title="Plain text evidence",
    )

    def render(self, artifact: object) -> Mapping[str, JsonValue]:
        if not isinstance(artifact, TextArtifact):
            return {"supported": False, "download": True}
        return {
            "supported": True,
            "text": artifact.metadata.get("text"),
            "download": True,
        }


@dataclass(frozen=True)
class SyntheticTextIntentPack:
    definition: BenchmarkDefinition
    dataset: SyntheticDataset
    solutions: tuple[KeywordStandaloneSolution | AssistedStyleSolution, ...]
    evaluator: IntentEvaluator
    viewer: TextViewer


PACK = SyntheticTextIntentPack(
    definition=BenchmarkDefinition(
        id="synthetic_text_intent",
        version="1.0.0",
        name="Synthetic text intent",
        description="Classify fully invented text into one of three invented intent labels.",
        input_modalities=(Modality.TEXT,),
        reference_modalities=(Modality.TEXT,),
        terminology=Terminology(
            benchmark_singular="intent benchmark",
            sample_singular="utterance",
            solution_singular="classifier",
            reference_singular="intent label",
        ),
        tags=("synthetic", "offline", "text"),
    ),
    dataset=SyntheticDataset(),
    solutions=(KeywordStandaloneSolution(), AssistedStyleSolution()),
    evaluator=IntentEvaluator(),
    viewer=TextViewer(),
)


def get_pack() -> SyntheticTextIntentPack:
    return PACK


def _macro_f1(comparisons: Sequence[tuple[str, str]]) -> Decimal | None:
    if not comparisons:
        return None
    labels = sorted({label for pair in comparisons for label in pair})
    scores: list[Decimal] = []
    for label in labels:
        true_positive = sum(
            expected == label and predicted == label for expected, predicted in comparisons
        )
        false_positive = sum(
            expected != label and predicted == label for expected, predicted in comparisons
        )
        false_negative = sum(
            expected == label and predicted != label for expected, predicted in comparisons
        )
        denominator = 2 * true_positive + false_positive + false_negative
        scores.append(
            Decimal("0") if denominator == 0 else Decimal(2 * true_positive) / Decimal(denominator)
        )
    return sum(scores, Decimal("0")) / Decimal(len(scores))
