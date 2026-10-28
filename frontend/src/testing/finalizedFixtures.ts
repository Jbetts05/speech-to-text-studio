import type { FinalizedEvaluationDto } from "../api/wire";
import { costFixture } from "./costFixtures";
import { reportWireFixture, runDetailWireFixture, runSummaryWireFixture } from "./fixtures";

const first = { ...reportWireFixture.results[0], attempts: 2 };
const second = {
  ...first, task_id: "3".repeat(64), solution_id: "pipeline-b",
  status: "poisoned" as const, metrics: [], evidence: [], telemetry: null,
  error: "Synthetic terminal failure",
};

export const finalizedWireFixture: FinalizedEvaluationDto = {
  record_source: "finalized_report",
  run: { ...runSummaryWireFixture, status: "completed-with-failures", pinned: true, expires_at: null },
  manifest: runDetailWireFixture.manifest,
  report: {
    ...reportWireFixture,
    results: [first, second],
    attempt_history: Object.fromEntries([first, second].map(result => [
      result.task_id,
      [1, 2].map(number => ({
        ...runDetailWireFixture.tasks[0].attempt_history[0], task_id: result.task_id,
        attempt_number: number, lease_token: number,
        finished_at: reportWireFixture.generated_at,
        status: number === 1 ? "retryable" : result.status === "succeeded" ? "succeeded" : "failed",
        evidence: [], telemetry: result.telemetry,
      })),
    ])),
  },
  costs: costFixture,
  sample_artifacts: {},
};
