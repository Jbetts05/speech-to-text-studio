import { benchmarkWireFixture } from "./fixtures";
import { mapBenchmark, type BenchmarkEnvelopeDto } from "../api/wire";
import { datasetFixture, hostedRuntimeFixture } from "./hostedFixtures";

export const serviceNames = {
  "azure-speech-fast": "Azure Speech Fast",
  "gpt-transcribe": "GPT-Transcribe",
  "managed-whisper": "Managed Whisper",
  "mai-transcribe-1-5": "MAI-Transcribe-1.5",
  "mai-transcribe-2": "MAI-Transcribe-2",
};

export const serviceCatalogFixture: BenchmarkEnvelopeDto = {
  ...benchmarkWireFixture,
  config: { ...benchmarkWireFixture.config, benchmark_id: "service_examples" },
  definition: { ...benchmarkWireFixture.definition, id: "service_examples", name: "Service examples" },
  sample_count: 26,
  scored_sample_count: 26,
  dataset_availability: { ...benchmarkWireFixture.dataset_availability, sample_count: 26, scored_sample_count: 26 },
  default_dataset_id: "service-fixtures",
  solutions: Object.entries(serviceNames).map(([id, display_name]) => ({
    ...benchmarkWireFixture.solutions[0],
    id,
    display_name,
    networked: true,
    pipeline: { pipeline_type: "standalone", adapter_id: id },
  })),
};

export const serviceBenchmark = mapBenchmark(serviceCatalogFixture);
export const serviceDataset = { ...datasetFixture, id: "service-fixtures", benchmark_id: serviceBenchmark.id, sample_count: 26, scored_sample_count: 26, pinned: true, expires_at: null };
export const serviceRuntime = { ...hostedRuntimeFixture, landing: { benchmark_id: serviceBenchmark.id, run_id: "fixture-landing", view: "evaluation" as const } };
