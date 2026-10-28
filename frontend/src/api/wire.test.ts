import { describe, expect, it } from "vitest";
import { costFixture } from "../testing/costFixtures";
import { finalizedWireFixture } from "../testing/finalizedFixtures";
import {
  benchmarkWireFixture,
  eventsWireFixture,
  reportWireFixture,
  runDetailWireFixture,
  runId,
  runSummaryWireFixture,
} from "../testing/fixtures";
import {
  mapBenchmark,
  mapEventPage,
  mapFinalizedEvaluation,
  mapReport,
  mapRunDetail,
  mapRunSummary,
} from "./wire";

describe("wire DTO mapping", () => {
  it("keeps finalized records distinct from current operational tasks", () => {
    const original = JSON.stringify(finalizedWireFixture);
    const mapped = mapFinalizedEvaluation(finalizedWireFixture);
    expect(mapped.run.record_source).toBe("finalized_report");
    expect(mapped.run.tasks).toBeUndefined();
    expect(mapped.run.finalized_results).toEqual(finalizedWireFixture.report.results);
    expect(mapped.run.costs).toEqual(finalizedWireFixture.costs);
    expect(mapped.run.pinned_benchmark?.metrics).toEqual(mapRunDetail(runDetailWireFixture).pinned_benchmark?.metrics);
    expect(mapped.report.families).toEqual(mapReport(finalizedWireFixture.report, finalizedWireFixture.manifest.viewer).families);
    expect(JSON.stringify(finalizedWireFixture)).toBe(original);
  });
  it("preserves the additive cost projection without mutating original report values", () => {
    const original = JSON.stringify(reportWireFixture);
    const detail = mapRunDetail({ ...runDetailWireFixture, costs: costFixture });
    expect(detail.costs).toEqual(costFixture);
    expect(mapRunDetail(runDetailWireFixture).costs).toBeUndefined();
    expect(JSON.stringify(reportWireFixture)).toBe(original);
  });

  it("maps benchmark envelopes and solution pipeline metadata", () => {
    const benchmark = mapBenchmark(benchmarkWireFixture);

    expect(benchmark.id).toBe(benchmarkWireFixture.definition.id);
    expect(benchmark.viewer?.modality).toBe("structured");
    expect(benchmark.pipelines).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          id: "pipeline-b",
          mode: "assisted",
          networked: true,
          compatibility: expect.objectContaining({
            level: "partial-reference",
          }),
        }),
      ]),
    );
    expect(benchmark.sample_count).toBe(12);
    expect(benchmark.scored_sample_count).toBe(12);
  });

  it("preserves the catalog identity of the published default dataset", () => {
    expect(mapBenchmark({ ...benchmarkWireFixture, default_dataset_id: "pinned-published-default" }).default_dataset_id).toBe("pinned-published-default");
    expect(mapBenchmark(benchmarkWireFixture).default_dataset_id).toBeUndefined();
  });

  it("maps run summaries and full task results without losing wire status", () => {
    const summary = mapRunSummary(runSummaryWireFixture);
    const detail = mapRunDetail(runDetailWireFixture);

    expect(summary.id).toBe(runId);
    expect(summary.manifest_artifact_id).toHaveLength(64);
    expect(detail.status).toBe("running");
    expect(detail.tasks?.map((task) => task.status)).toEqual([
      "succeeded",
      "leased",
    ]);
    expect(detail.evidence).toHaveLength(6);
    expect(detail.evidence?.some((item) => item.attempt === 1)).toBe(true);
    expect(detail.metric_definitions?.[0].direction).toBe("higher");
    expect(detail.pinned_benchmark?.viewer).toEqual(
      expect.objectContaining({ id: "structured-viewer", version: "1" }),
    );
    expect(detail.pinned_benchmark?.pipelines?.[0]).toEqual(
      expect.objectContaining({
        id: "pipeline-a",
        version: "1",
        mode: "standalone",
      }),
    );
  });

  it("maps numeric event sequences and derives the next after cursor", () => {
    const page = mapEventPage(eventsWireFixture);

    expect(page.next_after).toBe(12);
    expect(page.events[0]).toEqual(
      expect.objectContaining({
        id: "12",
        sequence: 12,
        type: "task.succeeded",
        task: expect.objectContaining({ status: "succeeded" }),
      }),
    );
  });

  it("keeps report families independent and builds evidence links", () => {
    const report = mapReport(
      reportWireFixture,
      benchmarkWireFixture.viewer,
    );

    expect(report.status).toBe("completed-with-failures");
    expect(report.families.map((family) => family.family)).toEqual([
      "quality",
      "cost",
      "latency",
      "usage",
      "reliability",
    ]);
    expect(
      report.families.find((family) => family.family === "cost")?.metrics,
    ).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          metric_id: "cost.amount",
          solution_id: "pipeline-a",
        }),
      ]),
    );
    const rawEvidence = report.evidence.find((item) => item.role === "raw");
    expect(rawEvidence?.artifact_sha256).toHaveLength(64);
    expect(rawEvidence).toEqual(
      expect.objectContaining({
        modality: "structured",
        media_type: "application/json",
        byte_size: 42,
      }),
    );
    expect(report.quality_comparability["pipeline-b"].comparable_quality)
      .toBe(false);
    const aggregateCosts = report.families
      .find((family) => family.family === "cost")
      ?.metrics.filter((metric) => !metric.task_id);
    expect(aggregateCosts).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ solution_id: "pipeline-a", unit: "USD" }),
        expect.objectContaining({ solution_id: "pipeline-b", unit: "EUR" }),
      ]),
    );

    const definitions = new Set(
      report.definitions.map(
        (definition) => `${definition.family}:${definition.id}`,
      ),
    );
    const aggregateKeys = report.families.flatMap((family) =>
      family.metrics
        .filter((metric) => !metric.task_id)
        .map((metric) => `${family.family}:${metric.metric_id}`),
    );
    expect([...new Set(aggregateKeys)].every((key) => definitions.has(key)))
      .toBe(true);
    expect(definitions).toContain("quality:macro_f1");
    expect(definitions).toContain("reliability:reliability.success_ratio");
    expect(definitions).not.toContain("latency:latency.attempt_ms");
    expect(aggregateKeys).not.toContain("latency:latency.attempt_ms");
  });
});
