import {
  mapEventPage,
  mapReport,
  mapRunDetail,
  type BenchmarkEnvelopeDto,
  type RunDetailDto,
  type RunEventDto,
  type RunReportDto,
  type RunSummaryDto,
} from "../api/wire";

export const runId = "a".repeat(64);
export const taskId = "b".repeat(64);
export const manifestArtifactId = "c".repeat(64);
export const reportArtifactId = "d".repeat(64);
export const rawArtifactId = "e".repeat(64);
export const comparisonArtifactId = "f".repeat(64);
export const inputArtifactId = "1".repeat(64);
export const referenceArtifactId = "4".repeat(64);
export const parsedArtifactId = "5".repeat(64);
const recordedAt = "2026-01-15T10:02:00.000Z";

const fixtureRevision = {
  id: "dataset-a",
  version: "1",
  content_hash: "1".repeat(64),
  provenance: { source: "Invented contract fixture", license: "Synthetic test", synthetic: true },
};
const fixturePricing = {
  id: "synthetic-pricing", version: "1", currency: "USD", region: "offline",
  effective_at: recordedAt, source: "Invented UI contract fixture", rates: [],
};

export const benchmarkWireFixture: BenchmarkEnvelopeDto = {
  config: {
    schema_version: "1", benchmark_id: "bench_general",
    offline_default: true, networked_execution: true,
  },
  dataset_availability: {
    status: "ready", reason: "Invented fixtures are available.", revision: fixtureRevision,
    sample_count: 12, scored_sample_count: 12,
  },
  definition: {
    id: "bench_general",
    version: "2.1.0",
    name: "General capability set",
    description: "12 samples · text and structured outputs",
    input_modalities: ["text"],
    reference_modalities: ["structured"],
    terminology: {
      benchmark_singular: "benchmark",
      sample_singular: "sample",
      solution_singular: "solution",
      reference_singular: "reference",
    },
    tags: ["general"],
  },
  solutions: [
    {
      id: "pipeline-a",
      networked: false,
      pipeline: {
        pipeline_type: "standalone",
        adapter_id: "adapter-a",
      },
      compatibility: {
        level: "full",
        comparable_quality: true,
        reason: "All required modalities are supported.",
      },
    },
    {
      id: "pipeline-b",
      networked: true,
      pipeline: {
        pipeline_type: "assisted",
        adapter_id: "adapter-b",
        components: ["retrieval", "generation"],
      },
      compatibility: {
        level: "partial-reference",
        comparable_quality: true,
        reason: "Reference content is converted before evaluation.",
      },
    },
  ],
  metrics: [
    {
      id: "exact_accuracy",
      name: "Exact accuracy",
      family: "quality",
      unit: "ratio",
      higher_is_better: true,
      description: "Exact match accuracy.",
    },
    {
      id: "macro_f1",
      name: "Macro F1",
      family: "quality",
      unit: "ratio",
      higher_is_better: true,
      description: "Macro-averaged F1.",
    },
  ],
  sample_count: 12,
  scored_sample_count: 12,
  viewer: {
    id: "structured-viewer",
    version: "1",
    modality: "structured",
    media_types: ["application/json"],
    title: "Structured result",
    fallback_download: true,
  },
};

export const runSummaryWireFixture: RunSummaryDto = {
  run_id: runId,
  benchmark_id: benchmarkWireFixture.definition.id,
  created_at: "2026-01-15T10:00:00.000Z",
  status: "running",
  manifest_artifact_id: manifestArtifactId,
  report_artifact_id: reportArtifactId,
};

const metricDefinitions = [
  {
    id: "exact_accuracy",
    name: "Exact accuracy",
    family: "quality" as const,
    unit: "ratio",
    higher_is_better: true,
    description: "Exact match accuracy.",
  },
  {
    id: "macro_f1",
    name: "Macro F1",
    family: "quality" as const,
    unit: "ratio",
    higher_is_better: true,
    description: "Macro-averaged F1.",
  },
  {
    id: "field_coverage",
    name: "Field coverage",
    family: "quality" as const,
    unit: "ratio",
    higher_is_better: true,
    description: "Expected fields returned.",
  },
];

const taskResult = {
  task_id: taskId,
  sample_id: "sample-001",
  solution_id: "pipeline-a",
  status: "succeeded" as const,
  attempts: 1,
  metrics: [
    {
      metric_id: "exact_accuracy",
      family: "quality" as const,
      value: "1.0",
      unit: "ratio",
      evidence_artifact_id: comparisonArtifactId,
      unknown_reason: null,
    },
    {
      metric_id: "field_coverage",
      family: "quality" as const,
      value: "0.95",
      unit: "ratio",
      evidence_artifact_id: comparisonArtifactId,
      unknown_reason: null,
    },
  ],
  evidence: [
    {
      role: "input" as const,
      artifact_id: inputArtifactId,
      modality: "text" as const,
      media_type: "text/plain",
      size_bytes: 24,
      attempt_number: null,
    },
    {
      role: "reference" as const,
      artifact_id: referenceArtifactId,
      modality: "structured" as const,
      media_type: "application/json",
      size_bytes: 18,
      attempt_number: null,
    },
    {
      role: "raw" as const,
      artifact_id: rawArtifactId,
      modality: "structured" as const,
      media_type: "application/json",
      size_bytes: 42,
      attempt_number: 1,
    },
    {
      role: "parsed" as const,
      artifact_id: parsedArtifactId,
      modality: "structured" as const,
      media_type: "application/json",
      size_bytes: 18,
      attempt_number: 1,
    },
    {
      role: "comparison" as const,
      artifact_id: comparisonArtifactId,
      modality: "structured" as const,
      media_type: "application/json",
      size_bytes: 64,
      attempt_number: 1,
    },
  ],
  telemetry: {
    started_at: "2026-01-15T10:01:59.000Z",
    finished_at: recordedAt,
    latency_ms: "1000",
    usage: { dimensions: { input_tokens: "1190", output_tokens: "260" } },
    cost: {
      amount: "0.013",
      currency: "USD",
      pricing_snapshot: "pricing@1",
      reason: null,
    },
    provider_request_id: "provider-request-001",
  },
  error: null,
};

export const runDetailWireFixture: RunDetailDto = {
  runtime_error: null,
  manifest: {
    schema_version: "1",
    run_id: runId,
    benchmark: benchmarkWireFixture.definition,
    dataset_revision: {
      id: "dataset-a",
      version: "1",
      content_hash: "1".repeat(64),
      provenance: {
        source: "deterministic fixture",
        license: "test",
        synthetic: true,
      },
    },
    samples: [{
      id: "sample-001", role: "scored",
      input_artifact_id: inputArtifactId, reference_artifact_id: referenceArtifactId,
    }],
    solution_ids: ["pipeline-a", "pipeline-b"],
    solution_descriptors: {
      "pipeline-a": {
        id: "pipeline-a",
        version: "1",
        pipeline: benchmarkWireFixture.solutions[0].pipeline,
        compatibility: benchmarkWireFixture.solutions[0].compatibility,
        pricing_snapshot: fixturePricing,
      },
      "pipeline-b": {
        id: "pipeline-b", version: "1",
        pipeline: benchmarkWireFixture.solutions[1].pipeline,
        compatibility: benchmarkWireFixture.solutions[1].compatibility,
        pricing_snapshot: fixturePricing,
      },
    },
    evaluator: {
      id: "evaluator-a",
      version: "1",
      metrics: metricDefinitions,
    },
    viewer: benchmarkWireFixture.viewer,
    sample_ids: ["sample-001"],
    created_at: runSummaryWireFixture.created_at,
    networked_execution: false,
    retry_limit: 3,
    pricing_snapshots: {},
    descriptor_hashes: { evaluator: "2".repeat(64) },
    launch_idempotency_key_hash: null,
  },
  status: "running",
  manifest_ref: `/api/artifacts/${manifestArtifactId}`,
  manifest_artifact_id: manifestArtifactId,
  report_ref: `/api/artifacts/${reportArtifactId}`,
  report_artifact_id: reportArtifactId,
  tasks: [
    {
      id: taskId,
      run_id: runId,
      sample_id: "sample-001",
      solution_id: "pipeline-a",
      status: "succeeded",
      attempts: 1,
      max_attempts: 3,
      lease_owner: null,
      lease_token: 1,
      result: taskResult,
      attempt_history: [
        {
          task_id: taskId,
          attempt_number: 1,
          status: "retryable",
          lease_token: 1,
          started_at: recordedAt,
          evidence: [
            {
              role: "raw",
              artifact_id: rawArtifactId,
              modality: "structured",
              media_type: "application/json",
              size_bytes: 42,
              attempt_number: 1,
            },
          ],
          telemetry: taskResult.telemetry,
          error_type: null,
          error_message: null,
        },
      ],
    },
    {
      id: "3".repeat(64),
      run_id: runId,
      sample_id: "sample-002",
      solution_id: "pipeline-b",
      status: "leased",
      attempts: 1,
      max_attempts: 3,
      lease_owner: "worker-a",
      lease_token: 1,
      result: null,
      attempt_history: [],
    },
  ],
};

export const eventsWireFixture: RunEventDto[] = [
  {
    sequence: 12,
    run_id: runId,
    event_type: "task.succeeded",
    occurred_at: recordedAt,
    payload: { task_id: taskId, attempt: 1 },
  },
];

export const reportWireFixture: RunReportDto = {
  schema_version: "1",
  run_id: runId,
  manifest_artifact_id: manifestArtifactId,
  status: "completed-with-failures",
  generated_at: recordedAt,
  results: [taskResult],
  attempt_history: {
    [taskId]: runDetailWireFixture.tasks[0].attempt_history,
  },
  quality_comparability: {
    "pipeline-a": {
      level: "full",
      comparable_quality: true,
      reason: "Comparable against the complete synthetic reference.",
    },
    "pipeline-b": {
      level: "partial-reference",
      comparable_quality: false,
      reason: "Reference coverage is incomplete.",
    },
  },
  quality: {
    "pipeline-a": { exact_accuracy: "1.0", macro_f1: "0.95" },
  },
  cost: { "pipeline-a": "0.013", "pipeline-b": null },
  cost_currency: { "pipeline-a": "USD", "pipeline-b": "EUR" },
  latency: { "pipeline-a": "1000" },
  usage: {
    "pipeline-a": { input_tokens: "1190", output_tokens: "260" },
  },
  reliability: {
    "pipeline-a": {
      success_ratio: "0.5",
      poisoned_tasks: "1",
      total_attempts: "2",
    },
  },
  evidence_links: {
    [`${taskId}:raw`]: `/api/artifacts/${rawArtifactId}`,
    [`${taskId}:comparison`]: `/api/artifacts/${comparisonArtifactId}`,
  },
};

export const runFixture = mapRunDetail(runDetailWireFixture);
export const eventsFixture = mapEventPage(eventsWireFixture);
export const reportFixture = mapReport(
  reportWireFixture,
  benchmarkWireFixture.viewer,
);
