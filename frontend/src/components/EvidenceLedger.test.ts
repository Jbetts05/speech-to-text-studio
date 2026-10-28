import { describe, expect, it } from "vitest";
import type { Evidence, MetricValue } from "../types";
import {
  compareValues,
  evidenceMetricValues,
} from "./EvidenceLedger";

describe("EvidenceLedger sorting", () => {
  it("sorts decimal strings numerically", () => {
    expect(compareValues("1.10", "1.2", "ascending")).toBeLessThan(0);
    expect(compareValues("10.0", "2.0", "descending")).toBeLessThan(0);
    expect(compareValues("1E-7", "0.000002", "ascending")).toBeLessThan(0);
    expect(compareValues("-2.5e-3", "-1E-3", "ascending")).toBeLessThan(0);
  });

  it("does not attach successful retry metrics to failed-attempt evidence", () => {
    const successfulMetrics: MetricValue[] = [
      {
        metric_id: "exact_accuracy",
        family: "quality",
        value: "1",
        unit: "ratio",
        task_id: "task-1",
        solution_id: "solution-1",
      },
    ];
    const evidence: Evidence[] = [
      {
        id: "attempt-1-raw",
        task_id: "task-1",
        solution_id: "solution-1",
        modality: "structured",
        label: "attempt 1 provider response",
        attempt: 1,
        role: "raw",
        source: "attempt",
        metrics: [
          {
            metric_id: "attempt_duration_ms",
            family: "latency",
            value: "42",
            unit: "ms",
            task_id: "task-1",
            solution_id: "solution-1",
          },
        ],
      },
      {
        id: "successful-result",
        task_id: "task-1",
        solution_id: "solution-1",
        modality: "structured",
        label: "successful result",
        role: "result",
        source: "result",
      },
    ];

    const merged = evidenceMetricValues(evidence, successfulMetrics);

    expect(merged[0].metrics?.map((metric) => metric.metric_id)).toEqual([
      "attempt_duration_ms",
    ]);
    expect(merged[1].metrics?.map((metric) => metric.metric_id)).toEqual([
      "exact_accuracy",
    ]);
  });
});
