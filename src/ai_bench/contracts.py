"""Immutable platform contracts shared by packs, execution, API, and reports."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    GetJsonSchemaHandler,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic import (
    JsonValue as PydanticJsonValue,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

type JsonValue = PydanticJsonValue


def utc_now() -> datetime:
    return datetime.now(UTC)


class Contract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class Modality(StrEnum):
    IMAGE = "image"
    DOCUMENT = "document"
    AUDIO = "audio"
    VIDEO = "video"
    TEXT = "text"
    STRUCTURED = "structured"


class MetricFamily(StrEnum):
    QUALITY = "quality"
    COST = "cost"
    LATENCY = "latency"
    USAGE = "usage"
    RELIABILITY = "reliability"


class PipelineType(StrEnum):
    STANDALONE = "standalone"
    ASSISTED = "assisted"


class SampleRole(StrEnum):
    SCORED = "scored"
    FEW_SHOT = "few-shot"
    EXCLUDED = "excluded"


class CompatibilityLevel(StrEnum):
    FULL = "full"
    PARTIAL_REFERENCE = "partial-reference"
    UNSUPPORTED = "unsupported"


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_FAILURES = "completed-with-failures"
    FAILED = "failed"


class TaskStatus(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    SUCCEEDED = "succeeded"
    POISONED = "poisoned"


class AttemptStatus(StrEnum):
    RUNNING = "running"
    RETRYABLE = "retryable"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Terminology(Contract):
    benchmark_singular: str = Field(min_length=1, max_length=80)
    sample_singular: str = Field(min_length=1, max_length=80)
    solution_singular: str = Field(min_length=1, max_length=80)
    reference_singular: str = Field(min_length=1, max_length=80)


class PackConfiguration(Contract):
    schema_version: Literal["1"]
    benchmark_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,63}$")
    offline_default: bool
    networked_execution: bool


class BenchmarkDefinition(Contract):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,63}$")
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=1000)
    input_modalities: tuple[Modality, ...] = Field(min_length=1)
    reference_modalities: tuple[Modality, ...] = Field(min_length=1)
    terminology: Terminology
    tags: tuple[str, ...] = ()


class Artifact(Contract):
    id: str = Field(pattern=r"^[a-f0-9]{64}$")
    modality: Modality
    media_type: str = Field(min_length=3, max_length=200)
    size_bytes: int = Field(ge=0)
    uri: str = Field(min_length=1, max_length=2000)
    metadata: Mapping[str, JsonValue] = Field(default_factory=dict)


class ImageArtifact(Artifact):
    modality: Literal[Modality.IMAGE]


class DocumentArtifact(Artifact):
    modality: Literal[Modality.DOCUMENT]


class AudioArtifact(Artifact):
    modality: Literal[Modality.AUDIO]


class VideoArtifact(Artifact):
    modality: Literal[Modality.VIDEO]


class TextArtifact(Artifact):
    modality: Literal[Modality.TEXT]


class StructuredArtifact(Artifact):
    modality: Literal[Modality.STRUCTURED]


type ModalityArtifact = Annotated[
    ImageArtifact
    | DocumentArtifact
    | AudioArtifact
    | VideoArtifact
    | TextArtifact
    | StructuredArtifact,
    Field(discriminator="modality"),
]


class BenchmarkSample(Contract):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    input: ModalityArtifact
    reference: ModalityArtifact | None = None
    metadata: Mapping[str, JsonValue] = Field(default_factory=dict)
    role: SampleRole = SampleRole.SCORED


class DatasetProvenance(Contract):
    source: str = Field(min_length=1, max_length=1000)
    license: str = Field(min_length=1, max_length=200)
    synthetic: bool


class DatasetRevision(Contract):
    id: str = Field(min_length=1, max_length=120)
    version: str = Field(min_length=1, max_length=80)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    provenance: DatasetProvenance


class DatasetAvailability(Contract):
    status: Literal["ready", "not-configured", "invalid"]
    reason: str = Field(min_length=1, max_length=1000)
    revision: DatasetRevision | None = None
    sample_count: int = Field(ge=0)
    scored_sample_count: int = Field(ge=0)

    @model_validator(mode="after")
    def ready_has_revision(self) -> DatasetAvailability:
        if self.status == "ready" and self.revision is None:
            raise ValueError("ready dataset availability requires a published revision")
        if self.scored_sample_count > self.sample_count:
            raise ValueError("scored count cannot exceed sample count")
        return self


@runtime_checkable
class ExternalDataset(Protocol):
    """External availability is inspected separately from catalog validity."""

    def availability(self) -> DatasetAvailability: ...

    def snapshot_metadata(self) -> Mapping[str, JsonValue]: ...


class SampleDescriptor(Contract):
    id: str
    role: SampleRole
    input_artifact_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    reference_artifact_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class SampleSnapshot(Contract):
    schema_version: Literal["1"] = "1"
    dataset_revision: DatasetRevision
    samples: tuple[BenchmarkSample, ...] = Field(min_length=1)
    metadata: Mapping[str, JsonValue] = Field(default_factory=dict)


class StandalonePipeline(Contract):
    pipeline_type: Literal[PipelineType.STANDALONE]
    adapter_id: str = Field(min_length=1, max_length=120)


class AssistedPipeline(Contract):
    pipeline_type: Literal[PipelineType.ASSISTED]
    adapter_id: str = Field(min_length=1, max_length=120)
    components: tuple[str, ...] = Field(min_length=2)


type PipelineConfiguration = Annotated[
    StandalonePipeline | AssistedPipeline, Field(discriminator="pipeline_type")
]


class Compatibility(Contract):
    level: CompatibilityLevel
    comparable_quality: bool
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unsupported_is_not_comparable(self) -> Compatibility:
        if self.level is CompatibilityLevel.UNSUPPORTED and self.comparable_quality:
            raise ValueError("unsupported compatibility cannot have comparable quality")
        return self


class Usage(Contract):
    dimensions: Mapping[str, Decimal] = Field(default_factory=dict)
    complete: bool = True
    uncertainty: str | None = None

    @field_validator("dimensions")
    @classmethod
    def nonnegative_dimensions(cls, value: Mapping[str, Decimal]) -> Mapping[str, Decimal]:
        if any(not amount.is_finite() or amount < 0 for amount in value.values()):
            raise ValueError("usage dimensions must be finite and nonnegative")
        return value

    @model_validator(mode="after")
    def incomplete_usage_is_explicit(self) -> Usage:
        if not self.complete and not self.uncertainty:
            raise ValueError("incomplete usage requires an uncertainty reason")
        return self


class PricingRate(Contract):
    dimension: str = Field(min_length=1, max_length=100)
    unit_size: Decimal = Field(gt=0)
    price: Decimal = Field(ge=0)


class PricingModel(Contract):
    id: str = Field(min_length=1, max_length=120)
    version: str = Field(min_length=1, max_length=80)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    region: str = Field(min_length=1, max_length=120)
    effective_at: datetime
    source: str = Field(min_length=1, max_length=500)
    rates: tuple[PricingRate, ...]

    def estimate(self, usage: Usage) -> CostEstimate:
        if not usage.complete:
            return CostEstimate(
                amount=None,
                currency=self.currency,
                pricing_snapshot=f"{self.id}@{self.version}",
                reason=usage.uncertainty,
            )
        if not usage.dimensions:
            return CostEstimate(
                amount=None,
                currency=self.currency,
                pricing_snapshot=f"{self.id}@{self.version}",
                reason="usage is empty and cannot establish a zero cost",
            )
        rates = {rate.dimension: rate for rate in self.rates}
        if any(dimension not in rates for dimension in usage.dimensions):
            return CostEstimate(
                amount=None,
                currency=self.currency,
                pricing_snapshot=f"{self.id}@{self.version}",
                reason="usage contains an unpriced dimension",
            )
        total = sum(
            (
                amount / rates[dimension].unit_size * rates[dimension].price
                for dimension, amount in usage.dimensions.items()
            ),
            Decimal("0"),
        )
        return CostEstimate(
            amount=total,
            currency=self.currency,
            pricing_snapshot=f"{self.id}@{self.version}",
            reason=None,
        )


class CostEstimate(Contract):
    amount: Decimal | None
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    pricing_snapshot: str
    reason: str | None

    @model_validator(mode="after")
    def unknown_has_reason(self) -> CostEstimate:
        if self.amount is None and not self.reason:
            raise ValueError("unknown cost requires a reason")
        return self


class MetricDefinition(Contract):
    id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,99}$")
    name: str
    family: MetricFamily
    unit: str
    higher_is_better: bool | None = None
    description: str


class MetricValue(Contract):
    metric_id: str
    family: MetricFamily
    value: Decimal | None
    unit: str
    evidence_artifact_id: str | None = None
    unknown_reason: str | None = None

    @model_validator(mode="after")
    def unknown_has_reason(self) -> MetricValue:
        if self.value is None and not self.unknown_reason:
            raise ValueError("unknown metric values require a reason")
        return self


class RendererDescriptor(Contract):
    id: str
    version: str = Field(min_length=1, max_length=80)
    modality: Modality
    media_types: tuple[str, ...]
    title: str
    fallback_download: bool = True


class RetryPolicy(Contract):
    version: Literal["bounded-exponential-v1"] = "bounded-exponential-v1"
    fallback_initial_seconds: int = Field(default=1, ge=1, le=60)
    fallback_max_seconds: int = Field(default=60, ge=1, le=3600)
    maximum_provider_delay_seconds: int = Field(default=86400, ge=1, le=86400)


class RetryDelay(Contract):
    scheduled_seconds: Decimal = Field(ge=0, le=86400)
    provider_seconds: Decimal | None = Field(default=None, ge=0)
    source: Literal[
        "retry-after-seconds",
        "retry-after-date",
        "retry-after-ms",
        "x-ms-retry-after-ms",
        "provider-message",
        "fallback",
    ]
    fallback_reason: str | None = None


class ProviderFailureEvidence(Contract):
    classification: Literal[
        "rate-limit",
        "authentication",
        "provider-http",
        "timeout",
        "transport",
        "invalid-response",
        "configuration",
    ]
    message: str = Field(min_length=1, max_length=500)
    http_status: int | None = Field(default=None, ge=100, le=599)
    provider_request_id: str | None = Field(default=None, max_length=200)
    retryable: bool
    usage: Usage
    retry_delay: RetryDelay | None = None
    raw_artifact_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    response_redacted: bool = False
    response_truncated: bool = False
    provider_latency_ms: Decimal | None = Field(default=None, ge=0)


class ExecutionRequest(Contract):
    run_id: str
    task_id: str
    sample: BenchmarkSample
    idempotency_key: str
    input_bytes: bytes = b""
    attempt_number: int = Field(default=1, ge=1)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)


class ExecutionOutput(Contract):
    raw: bytes
    modality: Modality
    media_type: str
    usage: Usage
    provider_request_id: str | None = None
    provider_latency_ms: Decimal | None = Field(default=None, ge=0)


class ParsedOutput(Contract):
    value: JsonValue
    media_type: str = "application/json"


class EvaluationResult(Contract):
    metrics: tuple[MetricValue, ...]
    comparison: JsonValue


class EvaluationRecord(Contract):
    sample_id: str
    solution_id: str
    metrics: tuple[MetricValue, ...]
    comparison: JsonValue


class AggregateEvaluation(Contract):
    metrics: tuple[MetricValue, ...]


@runtime_checkable
class DatasetAdapter(Protocol):
    id: str
    revision: DatasetRevision

    def samples(self) -> Sequence[BenchmarkSample]: ...

    def read_artifact(self, artifact: ModalityArtifact) -> bytes: ...


@runtime_checkable
class SolutionAdapter(Protocol):
    id: str
    version: str
    networked: bool
    pipeline: PipelineConfiguration
    compatibility: Compatibility
    pricing: PricingModel

    def execute(self, request: ExecutionRequest) -> ExecutionOutput: ...

    def parse(self, output: ExecutionOutput) -> ParsedOutput: ...


@runtime_checkable
class ConfiguredSolution(Protocol):
    def execution_settings(self) -> Mapping[str, JsonValue]: ...

    def validate_execution(self) -> None: ...

    @property
    def throttle_scope(self) -> str | None: ...


@runtime_checkable
class Evaluator(Protocol):
    id: str
    version: str
    metric_definitions: Sequence[MetricDefinition]

    def evaluate(self, sample: BenchmarkSample, output: ParsedOutput) -> EvaluationResult: ...

    def aggregate(self, records: Sequence[EvaluationRecord]) -> AggregateEvaluation: ...


@runtime_checkable
class ArtifactViewer(Protocol):
    descriptor: RendererDescriptor

    def render(self, artifact: ModalityArtifact) -> Mapping[str, JsonValue]: ...


class SolutionDescriptor(Contract):
    id: str
    version: str = Field(min_length=1, max_length=80)
    pipeline: PipelineConfiguration
    compatibility: Compatibility
    pricing_snapshot: PricingModel
    settings: Mapping[str, JsonValue] = Field(default_factory=dict)
    throttle_scope: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class EvaluatorDescriptor(Contract):
    id: str = Field(min_length=1, max_length=120)
    version: str = Field(min_length=1, max_length=80)
    metrics: tuple[MetricDefinition, ...]


class SchedulingPolicy(Contract):
    version: Literal["bounded-solutions-v1"] = "bounded-solutions-v1"
    max_in_flight: Literal[5] = 5
    max_per_solution: Literal[1] = 1


class RunManifest(Contract):
    schema_version: Literal["1", "2"]
    run_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    benchmark: BenchmarkDefinition
    dataset_revision: DatasetRevision
    samples: tuple[SampleDescriptor, ...] = Field(min_length=1)
    solution_ids: tuple[str, ...] = Field(min_length=1)
    solution_descriptors: Mapping[str, SolutionDescriptor]
    evaluator: EvaluatorDescriptor
    viewer: RendererDescriptor
    sample_ids: tuple[str, ...] = Field(min_length=1)
    created_at: datetime
    networked_execution: bool = False
    retry_limit: int = Field(ge=1, le=20)
    pricing_snapshots: Mapping[str, PricingModel]
    descriptor_hashes: Mapping[str, str]
    launch_idempotency_key_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    sample_snapshot_artifact_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    scheduling_policy: SchedulingPolicy | None = None

    @model_serializer(mode="wrap")
    def serialize_manifest(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        value: dict[str, Any] = handler(self)
        if self.scheduling_policy is None:
            value.pop("scheduling_policy", None)
        return value

    @classmethod
    def __get_pydantic_json_schema__(
        cls, schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        # The wrapper only omits the optional policy; wire types must still
        # describe the model fields, not the serializer's generic dict return.
        original = dict(schema)
        model = original
        while model["type"] != "model":
            nested = dict(model["schema"])
            model["schema"] = nested
            model = nested
        model.pop("serialization", None)
        return handler(original)

    @model_validator(mode="after")
    def current_manifests_require_snapshots(self) -> RunManifest:
        from ai_bench.hashing import sha256_digest

        if self.schema_version == "2" and self.sample_snapshot_artifact_id is None:
            raise ValueError("version 2 manifests require an immutable sample snapshot")
        if self.scheduling_policy is not None and self.schema_version != "2":
            raise ValueError("scheduling policies require a version 2 manifest")
        if self.scheduling_policy is not None and self.descriptor_hashes.get(
            "scheduling-policy"
        ) != sha256_digest(self.scheduling_policy):
            raise ValueError("scheduling policy must match its pinned descriptor hash")
        if self.scheduling_policy is None and "scheduling-policy" in self.descriptor_hashes:
            raise ValueError("scheduling policy descriptor requires a policy")
        return self

    @field_validator("descriptor_hashes")
    @classmethod
    def descriptor_hashes_are_sha256(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        if not value or any(
            len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest)
            for digest in value.values()
        ):
            raise ValueError("descriptor hashes must be lowercase SHA-256 digests")
        return value


class ArtifactEvidence(Contract):
    role: Literal["input", "reference", "raw", "parsed", "comparison", "result", "report"]
    artifact_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    modality: Modality
    media_type: str = Field(min_length=3, max_length=200)
    size_bytes: int = Field(ge=0)
    attempt_number: int | None = Field(default=None, ge=1)


class TelemetryEvidence(Contract):
    started_at: datetime
    finished_at: datetime
    latency_ms: Decimal | None = Field(ge=0)
    usage: Usage
    cost: CostEstimate
    provider_request_id: str | None = Field(default=None, max_length=500)


class TaskAttempt(Contract):
    task_id: str
    attempt_number: int = Field(ge=1)
    status: AttemptStatus
    lease_token: int = Field(ge=1)
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: Decimal | None = Field(default=None, ge=0)
    evidence: tuple[ArtifactEvidence, ...] = ()
    telemetry: TelemetryEvidence | None = None
    error_type: str | None = None
    error_message: str | None = None
    failure: ProviderFailureEvidence | None = None
    queue_wait_ms: Decimal | None = Field(default=None, ge=0)
    cooldown_wait_ms: Decimal | None = Field(default=None, ge=0)


class RunEvent(Contract):
    sequence: int = Field(ge=1)
    run_id: str
    event_type: str
    occurred_at: datetime
    payload: Mapping[str, JsonValue] = Field(default_factory=dict)


class TaskResult(Contract):
    task_id: str
    sample_id: str
    solution_id: str
    status: TaskStatus
    attempts: int
    metrics: tuple[MetricValue, ...] = ()
    evidence: tuple[ArtifactEvidence, ...] = ()
    telemetry: TelemetryEvidence | None = None
    error: str | None = None


class EvaluatedCoverage(Contract):
    expected_sample_ids: tuple[str, ...]
    evaluated_sample_ids: tuple[str, ...]
    complete: bool
    comparable: bool
    reason: str


class RunReport(Contract):
    schema_version: Literal["1"]
    run_id: str
    manifest_artifact_id: str
    status: RunStatus
    generated_at: datetime
    results: tuple[TaskResult, ...]
    attempt_history: Mapping[str, tuple[TaskAttempt, ...]] = Field(default_factory=dict)
    quality_comparability: Mapping[str, Compatibility]
    quality: Mapping[str, Mapping[str, Decimal | None]]
    cost: Mapping[str, Decimal | None]
    cost_currency: Mapping[str, str]
    latency: Mapping[str, Decimal | None]
    usage: Mapping[str, Mapping[str, Decimal]]
    reliability: Mapping[str, Mapping[str, Decimal]]
    evidence_links: Mapping[str, str]
    coverage: Mapping[str, EvaluatedCoverage] = Field(default_factory=dict)
    latency_breakdown: Mapping[str, Mapping[str, Decimal | None]] = Field(default_factory=dict)
    usage_uncertainty: Mapping[str, tuple[str, ...]] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def forbid_universal_score(cls, value: Any) -> Any:
        if isinstance(value, dict) and any(
            key in value for key in ("score", "overall_score", "universal_score", "winner")
        ):
            raise ValueError("reports must not define a universal score or winner")
        return value
