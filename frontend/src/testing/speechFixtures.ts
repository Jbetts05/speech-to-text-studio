// Invented UI contract data, never acquired audio, transcripts, or provider results.
import type { BackendTranscriptComparison } from "../api/contracts.generated";
import type { BenchmarkEnvelopeDto, RunDetailDto, RunReportDto, RunTaskDto } from "../api/wire";
import {
  benchmarkWireFixture, runDetailWireFixture, reportWireFixture,
  inputArtifactId, referenceArtifactId, comparisonArtifactId, rawArtifactId, runId,
} from "./fixtures";

export const speechSolutionIds = [
  "azure-speech-fast", "gpt-transcribe", "managed-whisper",
  "mai-transcribe-1-5", "mai-transcribe-2",
];
export const speechSampleIds = Array.from({ length: 26 }, (_, index) => `synthetic-${index + 1}`);

export const speechBenchmarkFixture: BenchmarkEnvelopeDto = {
  ...benchmarkWireFixture,
  config: { schema_version: "1", benchmark_id: "speech_to_text", offline_default: false, networked_execution: true },
  definition: {
    ...benchmarkWireFixture.definition,
    id: "speech_to_text", name: "Speech-to-Text Studio", version: "1.0.0",
    description: "Invented UI data for the 26-sample pilot.",
    input_modalities: ["audio"], reference_modalities: ["text"],
  },
  solutions: speechSolutionIds.map((id) => ({
    id, networked: true, pipeline: { adapter_id: id, pipeline_type: "standalone" },
    compatibility: { level: "full", comparable_quality: true, reason: "Full transcript contract." },
  })),
  metrics: [{
    id: "word_error_rate", name: "Word error rate", family: "quality", unit: "ratio",
    higher_is_better: false, description: "Corpus word errors divided by evaluated reference words.",
  }],
  sample_count: 26, scored_sample_count: 26,
  dataset_availability: {
    status: "ready", reason: "Invented external publication is available.",
    revision: {
      id: "synthetic-pilot-revision", version: "1.0.0", content_hash: "7".repeat(64),
      provenance: { source: "Invented UI contract", license: "Synthetic test", synthetic: true },
    },
    sample_count: 26, scored_sample_count: 26,
  },
  viewer: {
    id: "transcript-alignment", version: "1.0.0", modality: "audio",
    media_types: ["audio/flac", "text/plain", "application/json"],
    title: "Audio and transcript evidence", fallback_download: true,
  },
};

export const transcriptComparisonFixture: BackendTranscriptComparison = {
  schema_version: "1", kind: "transcript-alignment", normalization_version: "librispeech-english-v1",
  reference_text: "THE SMALL CAT", hypothesis_text: "The tall cat purrs",
  reference_tokens: ["the", "small", "cat"], hypothesis_tokens: ["the", "tall", "cat", "purrs"],
  substitutions: 1, deletions: 0, insertions: 1, reference_words: 3,
  word_error_rate: "0.6666666666666666666666666667",
  alignment: [
    { operation: "match", reference: "the", hypothesis: "the" },
    { operation: "substitution", reference: "small", hypothesis: "tall" },
    { operation: "match", reference: "cat", hypothesis: "cat" },
    { operation: "insertion", reference: null, hypothesis: "purrs" },
  ],
};

const speechTasks: RunTaskDto[] = speechSampleIds.flatMap((sampleId, sampleIndex) =>
  speechSolutionIds.map((solutionId, solutionIndex) => {
    const id = (sampleIndex * 5 + solutionIndex + 100).toString(16).padStart(64, "0");
    const evaluated = sampleIndex === 0 && solutionIndex < 2;
    return {
      id, run_id: runId, sample_id: sampleId, solution_id: solutionId,
      status: evaluated ? "succeeded" : "pending",
      attempts: evaluated || (solutionIndex === 1 && sampleIndex === 1) ? 1 : 0,
      max_attempts: 3, lease_owner: null, lease_token: evaluated ? 1 : 0,
      cooldown_until: !evaluated && solutionIndex === 1 ? "2099-01-01T00:00:00Z" : null,
      next_available_at: null,
      result: evaluated ? {
        task_id: id, sample_id: sampleId, solution_id: solutionId,
        status: "succeeded", attempts: 1,
        metrics: [{ metric_id: "word_error_rate", family: "quality", unit: "ratio", value: "0.6666666667" }],
        evidence: [
          { role: "input", artifact_id: inputArtifactId, modality: "audio", media_type: "audio/flac", size_bytes: 100 },
          { role: "reference", artifact_id: referenceArtifactId, modality: "text", media_type: "text/plain", size_bytes: 13 },
          { role: "comparison", artifact_id: comparisonArtifactId, modality: "structured", media_type: "application/json", size_bytes: 400 },
        ],
      } : null,
      attempt_history: !evaluated && solutionIndex === 1 && sampleIndex === 1 ? [{
        task_id: id, attempt_number: 1, status: "retryable", lease_token: 1,
        started_at: "2026-01-01T00:00:00Z", finished_at: "2026-01-01T00:00:01Z",
        failure: {
          classification: "rate-limit", message: "Invented rate limit.", http_status: 429,
          provider_request_id: "invented-request", retryable: true,
          usage: { dimensions: { calls: "1" }, complete: false, uncertainty: "Billing is unknown." },
          retry_delay: { scheduled_seconds: "9", provider_seconds: "9", source: "provider-message" },
          raw_artifact_id: rawArtifactId,
        },
        evidence: [{ role: "raw", artifact_id: rawArtifactId, modality: "structured", media_type: "application/json", size_bytes: 80, attempt_number: 1 }],
      }] : [],
    };
  }),
);

export const speechRunFixture: RunDetailDto = {
  ...runDetailWireFixture,
  manifest: {
    ...runDetailWireFixture.manifest,
    schema_version: "2", benchmark: speechBenchmarkFixture.definition,
    sample_snapshot_artifact_id: "8".repeat(64),
    sample_ids: speechSampleIds,
    samples: speechSampleIds.map((id) => ({
      id, role: "scored", input_artifact_id: inputArtifactId, reference_artifact_id: referenceArtifactId,
    })),
    solution_ids: speechSolutionIds,
    solution_descriptors: Object.fromEntries(speechBenchmarkFixture.solutions.map((solution) => [
      solution.id, {
        ...runDetailWireFixture.manifest.solution_descriptors["pipeline-a"],
        id: solution.id, version: "1.0.0", pipeline: solution.pipeline, compatibility: solution.compatibility,
      },
    ])),
    viewer: speechBenchmarkFixture.viewer,
    evaluator: { id: "english-word-error", version: "1.0.0", metrics: speechBenchmarkFixture.metrics },
    networked_execution: true,
    dataset_revision: speechBenchmarkFixture.dataset_availability.revision!,
  },
  report_ref: null, report_artifact_id: null, tasks: speechTasks,
  sample_artifacts: Object.fromEntries(speechSampleIds.map((id) => [
    id, [
      { role: "input", artifact_id: inputArtifactId, modality: "audio", media_type: "audio/flac", size_bytes: 100 },
      { role: "reference", artifact_id: referenceArtifactId, modality: "text", media_type: "text/plain", size_bytes: 13 },
    ],
  ])),
};

export const speechReportFixture: RunReportDto = {
  ...reportWireFixture,
  results: speechTasks.map((task) => task.result ?? {
    task_id: task.id, sample_id: task.sample_id, solution_id: task.solution_id,
    status: "poisoned", attempts: 1, error: "Invented terminal failure.",
    metrics: [], evidence: [],
  }),
  attempt_history: Object.fromEntries(speechTasks.map((task) => [task.id, task.attempt_history])),
  quality_comparability: Object.fromEntries(speechBenchmarkFixture.solutions.map((solution) => [solution.id, solution.compatibility])),
  quality: Object.fromEntries(speechSolutionIds.map((id, index) => [id, { word_error_rate: index < 2 ? "0.6666666667" : null }])),
  cost: Object.fromEntries(speechSolutionIds.map((id) => [id, null])),
  cost_currency: Object.fromEntries(speechSolutionIds.map((id) => [id, "USD"])),
  latency: {}, latency_breakdown: {}, usage: {}, reliability: {}, evidence_links: {},
  coverage: Object.fromEntries(speechSolutionIds.map((id, index) => [
    id, {
      expected_sample_ids: speechSampleIds, evaluated_sample_ids: index < 2 ? [speechSampleIds[0]] : [],
      complete: false, comparable: false, reason: "Invented partial coverage.",
    },
  ])),
};
