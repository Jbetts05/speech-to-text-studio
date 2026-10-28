import type { CostCoverage, RunCosts } from "../types";
import { manifestArtifactId, runId } from "./fixtures";

export const costCoverageFixture: CostCoverage = {
  status: "known", amounts: { USD: "0.5" }, covered_attempts: 2, total_attempts: 2,
  terminal_tasks: 1, total_tasks: 1, reasons: [],
};

const pricing = {
  id: "synthetic-price", version: "1", currency: "USD", region: "offline",
  effective_at: "2026-01-01T00:00:00Z", source: "Invented fixture rate, not a service price.",
  rates: [{ dimension: "seconds", unit_size: "10", price: "0.2" }],
};
const first = {
  solution_id: "pipeline-a", recorded: costCoverageFixture,
  current_estimate: { ...costCoverageFixture, amounts: { USD: "0.4" } },
  recorded_usage: { seconds: "20", calls: "2" }, usage_uncertainty: [],
  recorded_pricing_snapshots: ["synthetic-price@1"],
  pinned_pricing: pricing,
  current_pricing: {
    pricing: { ...pricing, version: "2" },
    checked_at: "2026-01-20T00:00:00Z",
    basis: "Conditional pro-rata estimate of recorded seconds; not billed cost.",
    source_urls: ["https://example.com/synthetic-pricing"],
    assumptions: ["Synthetic profile assumption, no discounts or rounding."],
  },
};
const second = {
  ...first, solution_id: "pipeline-b",
  recorded: { ...costCoverageFixture, status: "unknown" as const, amounts: { USD: null }, covered_attempts: 0, reasons: ["The immutable run has no pinned price rates."] },
  current_estimate: { ...costCoverageFixture, status: "partial" as const, amounts: { USD: "0.2" }, covered_attempts: 1, reasons: ["One attempt has no recorded duration."] },
  recorded_usage: { seconds: "10", calls: "1" },
  usage_uncertainty: ["Usage is not recorded for every attempt."],
  pinned_pricing: { ...pricing, rates: [] },
};

export const costFixture: RunCosts = {
  schema_version: "1", run_id: runId, manifest_artifact_id: manifestArtifactId,
  assessed_at: "2026-01-21T00:00:00Z",
  solutions: [first, second],
  samples: { "sample-001": [first, second] },
  recorded_total: { ...costCoverageFixture, status: "partial", covered_attempts: 2, total_attempts: 4, terminal_tasks: 2, total_tasks: 2 },
  current_estimate_total: { ...costCoverageFixture, status: "partial", amounts: { USD: "0.6" }, covered_attempts: 3, total_attempts: 4, terminal_tasks: 2, total_tasks: 2 },
  note: "Separate current-rate estimates, not original prices or billed charges. Original manifests and reports are unchanged.",
};
