export const metricFamilies = [
  "quality",
  "cost",
  "latency",
  "usage",
  "reliability",
] as const;

export type MetricFamily = (typeof metricFamilies)[number];
export type Modality =
  | "text"
  | "structured"
  | "image"
  | "document"
  | "audio"
  | "video"
  | "unknown";

export type PipelineMode = "standalone" | "assisted";
export type EvidenceRole =
  | "input"
  | "reference"
  | "raw"
  | "parsed"
  | "comparison"
  | "result"
  | "report";
export type EvidenceSource = "result" | "attempt" | "sample";
export type WorkspaceView =
  | "home"
  | "benchmarks"
  | "create"
  | "solutions"
  | "configure"
  | "evaluation"
  | "runs"
  | "reports"
  | "admin";
export type RunStatus =
  | "created"
  | "running"
  | "completed"
  | "completed-with-failures"
  | "failed";
export type TaskStatus =
  | "pending"
  | "leased"
  | "succeeded"
  | "poisoned";

export interface Compatibility {
  level: "full" | "partial-reference" | "unsupported";
  comparable_quality: boolean;
  reason: string;
}

export interface MetricDefinition {
  id: string;
  name: string;
  family: MetricFamily;
  unit?: string;
  description?: string;
  direction?: "higher" | "lower" | "neutral";
}

export interface BenchmarkTask {
  id: string;
  name: string;
  description?: string;
  input_modalities: Modality[];
  expected_modalities: Modality[];
  metadata?: Record<string, unknown>;
}

export interface BenchmarkManifest {
  id: string;
  name: string;
  version: string;
  description?: string;
  tasks: BenchmarkTask[];
  metrics: MetricDefinition[];
  sample_count: number;
  scored_sample_count: number;
  dataset_availability?: BackendDatasetAvailability;
  dataset_import?: DatasetImport | null;
  default_dataset_id?: string | null;
  pipelines?: SolutionPipeline[];
  input_modalities?: Modality[];
  reference_modalities?: Modality[];
  viewer?: ViewerDescriptor;
  compatibility?: Compatibility;
  metadata?: Record<string, unknown>;
}

export interface ViewerDescriptor {
  id: string;
  version: string;
  title: string;
  modality: Modality;
  media_types: string[];
  fallback_download: boolean;
}

export type RuntimeInfo = BackendRuntimeResponse;
export type DatasetImport = BackendDatasetImportDescription;
export type Dataset = BackendDatasetSummaryResponse;
export type DatasetPage = BackendDatasetPageResponse;
export type UploadRequest = BackendCreateUploadRequest;
export type UploadSession = BackendUploadSessionResponse;
export type RunCosts = BackendRunCostSummary;
export type CostCoverage = BackendCostCoverage;
export type SampleCost = BackendSampleCost;
export type RecordedResult = BackendTaskResult;
export type EvaluationRead =
  | { record_source: "finalized_report"; run: Run; report: RunReport }
  | { record_source: "live_detail_required"; run: Run; reason: BackendLiveDetailRequiredResponse["reason"]; detail: string };

export interface SolutionPipeline {
  id: string;
  name: string;
  version?: string;
  mode: PipelineMode;
  networked?: boolean;
  compatibility?: Compatibility;
  metadata?: Record<string, unknown>;
}

export interface MetricValue {
  metric_id: string;
  family: MetricFamily;
  value: number | string | null;
  unit?: string;
  task_id?: string;
  solution_id?: string;
  recorded_at?: string;
}

export interface TaskRun {
  id: string;
  task_id: string;
  sample_id?: string;
  solution_id?: string;
  status: TaskStatus;
  started_at?: string;
  completed_at?: string;
  attempt?: number;
  message?: string;
  next_available_at?: string;
  cooldown_until?: string;
  wait_state?: "queued" | "retry" | "cooldown";
  attempt_history?: BackendTaskAttempt[];
}

export interface Evidence {
  id: string;
  task_id: string;
  task_name?: string;
  solution_id?: string;
  solution_name?: string;
  modality: Modality;
  label: string;
  summary?: string;
  content?: string | Record<string, unknown> | unknown[];
  artifact_sha256?: string;
  media_type?: string;
  byte_size?: number;
  created_at?: string;
  attempt?: number;
  role: EvidenceRole;
  source: EvidenceSource;
  metrics?: MetricValue[];
}

export interface Run {
  record_source?: "live_detail" | "finalized_report";
  finalized_results?: RecordedResult[];
  live_detail_reason?: string;
  costs?: RunCosts;
  id: string;
  dataset_id?: string;
  expires_at?: string | null;
  pinned?: boolean;
  archival?: boolean;
  benchmark_id: string;
  benchmark_version?: string;
  pinned_benchmark?: BenchmarkManifest;
  solution_ids: string[];
  status: RunStatus;
  created_at: string;
  updated_at?: string;
  started_at?: string;
  completed_at?: string;
  retry_limit?: number;
  allow_networked?: boolean;
  manifest_artifact_id?: string;
  report_artifact_id?: string;
  tasks?: TaskRun[];
  metrics?: MetricValue[];
  evidence?: Evidence[];
  metric_definitions?: MetricDefinition[];
  quality_comparability?: Record<string, Compatibility>;
  runtime_error?: string;
  sample_artifacts?: Record<string, Evidence[]>;
}

export interface RunEvent {
  id: string;
  sequence: number;
  run_id: string;
  type: string;
  occurred_at: string;
  run_status?: RunStatus;
  task?: TaskRun;
  metric?: MetricValue;
  evidence?: Evidence;
  message?: string;
}

export interface ReportFamily {
  family: MetricFamily;
  metrics: MetricValue[];
  notes?: string[];
}

export interface RunReport {
  run_id: string;
  generated_at: string;
  status: RunStatus;
  families: ReportFamily[];
  evidence: Evidence[];
  definitions: MetricDefinition[];
  viewer: ViewerDescriptor;
  quality_comparability: Record<string, Compatibility>;
  coverage?: Record<string, BackendEvaluatedCoverage>;
  usage_uncertainty?: Record<string, string[]>;
  metadata?: Record<string, unknown>;
}

export interface LaunchRunInput extends BackendCreateRunRequest {
  solution_ids: string[];
  retry_limit: number;
  allow_networked: boolean;
}

export interface EventPage {
  events: RunEvent[];
  next_after: number;
}
import type {
  BackendCreateRunRequest,
  BackendCreateUploadRequest,
  BackendDatasetAvailability,
  BackendDatasetImportDescription,
  BackendDatasetPageResponse,
  BackendDatasetSummaryResponse,
  BackendEvaluatedCoverage,
  BackendRuntimeResponse,
  BackendTaskAttempt,
  BackendUploadSessionResponse,
  BackendRunCostSummary,
  BackendCostCoverage,
  BackendSampleCost,
  BackendTaskResult,
  BackendLiveDetailRequiredResponse,
} from "./api/contracts.generated";
