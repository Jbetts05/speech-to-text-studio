"""Typed public API envelopes shared with the generated frontend wire contracts."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from ai_bench.contracts import (
    ArtifactEvidence,
    BenchmarkDefinition,
    Compatibility,
    Contract,
    DatasetAvailability,
    DatasetRevision,
    MetricDefinition,
    PackConfiguration,
    PipelineConfiguration,
    RendererDescriptor,
    RunManifest,
    RunReport,
    RunStatus,
    TaskAttempt,
)
from ai_bench.costs import RunCostSummary
from ai_bench.repositories import TaskRecord


class CatalogSolution(Contract):
    id: str
    display_name: str | None = None
    networked: bool
    pipeline: PipelineConfiguration
    compatibility: Compatibility

    @field_validator("display_name", mode="before")
    @classmethod
    def optional_display_name(cls, value: object) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None


class BenchmarkCatalogEntry(Contract):
    config: PackConfiguration
    definition: BenchmarkDefinition
    solutions: tuple[CatalogSolution, ...]
    metrics: tuple[MetricDefinition, ...]
    sample_count: int
    scored_sample_count: int
    dataset_availability: DatasetAvailability
    viewer: RendererDescriptor
    dataset_import: DatasetImportDescription | None = None
    default_dataset_id: str | None = None


class DatasetImportDescription(Contract):
    id: str
    version: str
    title: str
    input_extensions: tuple[str, ...]
    reference_extensions: tuple[str, ...]
    max_files: int = Field(ge=1, le=20)
    max_input_bytes: int = Field(gt=0, le=25_000_000)
    max_reference_bytes: int = Field(gt=0, le=25_000_000)
    max_total_bytes: int = Field(gt=0, le=250_000_000)
    language: str
    instructions: tuple[str, ...]


class DatasetSummaryResponse(Contract):
    id: str
    benchmark_id: str
    name: str
    revision: DatasetRevision
    sample_count: int = Field(ge=1)
    scored_sample_count: int = Field(ge=1)
    published_at: datetime
    expires_at: datetime | None
    pinned: bool = False
    sample_artifacts: Mapping[str, tuple[ArtifactEvidence, ...]] = Field(default_factory=dict)


class DatasetPageResponse(Contract):
    items: tuple[DatasetSummaryResponse, ...]
    next_cursor: str | None = None


class UploadPartResponse(Contract):
    part_id: str
    name: str
    size_bytes: int = Field(gt=0, le=25_000_000)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    media_type: str | None
    status: Literal["pending", "receiving", "complete"]


class UploadPartRequest(Contract):
    name: str = Field(min_length=1, max_length=120)
    size_bytes: int = Field(gt=0, le=25_000_000)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    media_type: str | None = Field(default=None, max_length=120)


class CreateUploadRequest(Contract):
    benchmark_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,63}$")
    name: str = Field(min_length=1, max_length=80)
    language: str = Field(pattern=r"^[a-z][a-z0-9-]{0,15}$")
    public_acknowledged: Literal[True]
    rights_acknowledged: Literal[True]
    language_acknowledged: Literal[True]
    files: list[UploadPartRequest] = Field(min_length=1, max_length=20)

    @field_validator(
        "public_acknowledged", "rights_acknowledged", "language_acknowledged", mode="before"
    )
    @classmethod
    def explicit_acknowledgment(cls, value: object) -> bool:
        if value is not True:
            raise ValueError("An explicit boolean true acknowledgment is required.")
        return True

    @model_validator(mode="after")
    def bounded_reservation(self) -> CreateUploadRequest:
        if sum(part.size_bytes for part in self.files) > 250_000_000:
            raise ValueError("Total reserved file data exceeds 250,000,000 bytes.")
        if len({part.name for part in self.files}) != len(self.files):
            raise ValueError("Each reserved filename must be unique.")
        return self


class UploadSessionResponse(Contract):
    upload_id: str
    benchmark_id: str
    name: str
    status: Literal["receiving", "validating", "ready", "failed", "expired"]
    created_at: datetime
    expires_at: datetime
    parts: tuple[UploadPartResponse, ...]
    dataset_id: str | None = None
    error: str | None = None


class LandingContext(Contract):
    benchmark_id: str
    run_id: str
    view: Literal["evaluation"] = "evaluation"


class RuntimeResponse(Contract):
    runtime: str
    execution_backend: str
    horizontal_scaling: bool
    persistent_state_required: bool
    azure_storage_required: bool
    azure_storage_role: str
    worker_running: bool = False
    worker_error: str | None = None
    public_demo: bool = False
    allow_networked: bool = False
    max_attempts: int = Field(default=20, ge=1, le=20)
    retention_days: int | None = None
    server_time: datetime | None = None
    landing: LandingContext | None = None
    sender_policy: Literal["current-generation"] = "current-generation"
    external_call_duplicate_risk: bool = True


class CreateRunResponse(Contract):
    run_id: str
    status: RunStatus
    manifest_ref: str
    manifest_artifact_id: str
    report_ref: str | None
    report_artifact_id: str | None


class RunSummaryResponse(Contract):
    run_id: str
    benchmark_id: str
    created_at: datetime
    status: RunStatus
    manifest_artifact_id: str
    report_artifact_id: str | None
    runtime_error: str | None = None
    dataset_id: str | None = None
    expires_at: datetime | None = None
    pinned: bool = False
    archival: bool = False


class RunTaskResponse(TaskRecord):
    attempt_history: tuple[TaskAttempt, ...]


class RunDetailResponse(Contract):
    costs: RunCostSummary | None = None
    manifest: RunManifest
    manifest_ref: str
    manifest_artifact_id: str
    status: RunStatus
    runtime_error: str | None
    report_ref: str | None
    report_artifact_id: str | None
    tasks: tuple[RunTaskResponse, ...]
    sample_artifacts: Mapping[str, tuple[ArtifactEvidence, ...]] = Field(default_factory=dict)
    dataset_id: str | None = None
    expires_at: datetime | None = None
    pinned: bool = False
    archival: bool = False


class FinalizedEvaluationResponse(Contract):
    record_source: Literal["finalized_report"]
    run: RunSummaryResponse
    manifest: RunManifest
    report: RunReport
    costs: RunCostSummary
    sample_artifacts: Mapping[str, tuple[ArtifactEvidence, ...]] = Field(default_factory=dict)


class LiveDetailRequiredResponse(Contract):
    record_source: Literal["live_detail_required"]
    run: RunSummaryResponse
    reason: Literal[
        "run_not_finalized", "report_missing", "incomplete_results", "incomplete_attempt_history"
    ]
    detail: str


BenchmarkCatalogEntry.model_rebuild()
