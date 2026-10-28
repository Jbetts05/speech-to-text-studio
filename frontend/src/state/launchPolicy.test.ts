import { describe, expect, it } from "vitest";
import { mapBenchmark } from "../api/wire";
import { benchmarkWireFixture } from "../testing/fixtures";
import { hostedRuntimeFixture } from "../testing/hostedFixtures";
import { launchBlockReason, launchNetworkPermission, type LaunchAvailability } from "./launchPolicy";

const ready: LaunchAvailability = {
  benchmark: mapBenchmark(benchmarkWireFixture),
  solutionIds: ["pipeline-a", "pipeline-b"],
  retryLimit: 2, allowNetworked: true, networkAvailable: true,
  launching: false, runtime: hostedRuntimeFixture,
};

describe("explicit launch policy", () => {
  it("grants hosted consent only for a recognized authorized cloud runtime", () => {
    expect(launchNetworkPermission(hostedRuntimeFixture, false)).toBe(true);
    expect(launchNetworkPermission({ ...hostedRuntimeFixture, allow_networked: false }, true)).toBe(false);
    expect(launchNetworkPermission(undefined, true)).toBe(false);
    expect(launchNetworkPermission({ ...hostedRuntimeFixture, runtime: "unknown" }, true)).toBe(false);
  });

  it("keeps local opt-in and operator authorization independent", () => {
    const runtime = { ...hostedRuntimeFixture, runtime: "local", public_demo: false };
    expect(launchNetworkPermission(runtime, false)).toBe(false);
    expect(launchNetworkPermission(runtime, true)).toBe(true);
    expect(launchBlockReason({ ...ready, runtime, allowNetworked: false })).toMatch(/Enable networked execution/);
    expect(launchBlockReason({ ...ready, runtime, networkAvailable: false })).toMatch(/disabled by the runtime/);
    expect(launchBlockReason({ ...ready, runtime, solutionIds: ["pipeline-a"], allowNetworked: false })).toBeUndefined();
  });

  it("explains loading, missing runtime, worker failure and unsupported configurations", () => {
    expect(launchBlockReason({ ...ready, runtimeLoading: true })).toMatch(/Loading runtime/);
    expect(launchBlockReason({ ...ready, runtime: undefined })).toMatch(/Runtime settings are unavailable/);
    expect(launchBlockReason({ ...ready, runtime: { ...hostedRuntimeFixture, runtime: "unknown" } })).toMatch(/not recognized/);
    expect(launchBlockReason({ ...ready, runtime: { ...hostedRuntimeFixture, worker_running: false } })).toMatch(/worker to recover/);
    expect(launchBlockReason({ ...ready, runtime: { ...hostedRuntimeFixture, worker_error: "unavailable" } })).toMatch(/worker to recover/);
    expect(launchBlockReason({ ...ready, solutionIds: ["missing"] })).toMatch(/unavailable or unsupported/);
    expect(launchBlockReason({ ...ready, solutionIds: [] })).toMatch(/Select at least one/);
    expect(launchBlockReason({ ...ready, retryLimit: 1.5 })).toMatch(/whole-number/);
    expect(launchBlockReason({ ...ready, datasetBlocked: true, datasetBlockReason: "Dataset expired." })).toBe("Cannot launch: Dataset expired.");
    expect(launchBlockReason(ready)).toBeUndefined();
  });

  it("fails closed when a selected service has no compatibility declaration", () => {
    expect(launchBlockReason({
      ...ready,
      benchmark: { ...ready.benchmark, pipelines: ready.benchmark.pipelines?.map(solution => ({ ...solution, compatibility: undefined })) },
    })).toMatch(/unavailable or unsupported/);
  });
});
