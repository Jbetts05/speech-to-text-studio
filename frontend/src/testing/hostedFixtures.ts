import type { Dataset, DatasetImport, RuntimeInfo, UploadRequest, UploadSession } from "../types";

export const datasetImportFixture: DatasetImport = {
  id: "paired-inputs", version: "1", title: "Import a public dataset",
  input_extensions: [".flac", ".wav", ".mp3"], reference_extensions: [".txt"],
  max_files: 20, max_input_bytes: 25_000_000, max_reference_bytes: 16_384,
  max_total_bytes: 250_000_000, language: "en",
  instructions: ["1–10 exact basename pairs.", "Maximum 5 minutes per input and 30 minutes per dataset."],
};

export const datasetFixture: Dataset = {
  id: "dataset-synthetic", benchmark_id: "bench_general", name: "Synthetic public examples",
  revision: { id: "uploaded-synthetic", version: "1", content_hash: "a".repeat(64), provenance: { source: "Synthetic fixture", license: "Synthetic fixture", synthetic: true } },
  sample_count: 3, scored_sample_count: 3, published_at: "2026-09-18T10:00:00Z",
  expires_at: "2026-09-28T10:00:00Z", pinned: false,
};

export const hostedRuntimeFixture: RuntimeInfo = {
  runtime: "azure", execution_backend: "test-memory", horizontal_scaling: false,
  persistent_state_required: true, azure_storage_required: false, azure_storage_role: "test",
  public_demo: true, allow_networked: true, max_attempts: 3, retention_days: 10,
  worker_running: true, worker_error: null,
  server_time: "2026-09-18T10:00:00Z",
  sender_policy: "current-generation", external_call_duplicate_risk: true,
};

export function uploadSessionFixture(request: UploadRequest): UploadSession {
  return {
    upload_id: "upload-synthetic", benchmark_id: request.benchmark_id, name: request.name,
    status: "receiving", created_at: "2026-09-18T10:00:00Z", expires_at: "2026-09-19T10:00:00Z",
    parts: request.files.map((part, index) => ({
      ...part, media_type: null, part_id: `part-${index}`, status: "pending",
    })),
  };
}
