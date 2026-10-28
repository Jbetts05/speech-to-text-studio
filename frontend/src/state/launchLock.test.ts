import { describe, expect, it, vi } from "vitest";
import { ApiError, type ApiClient } from "../api/client";
import { runFixture } from "../testing/fixtures";
import type { LaunchRunInput } from "../types";
import {
  IdempotentLaunchLock,
  isAmbiguousLaunchFailure,
} from "./launchLock";

const input: LaunchRunInput = {
  benchmark_id: "bench_general",
  solution_ids: ["pipeline-b", "pipeline-a"],
  retry_limit: 1,
  allow_networked: false,
};

function clientWithLaunch(
  launchRun: ApiClient["launchRun"],
): ApiClient {
  return {
    listBenchmarks: vi.fn(),
    listRuns: vi.fn(),
    getRuntime: vi.fn(),
    launchRun,
    getRun: vi.fn(),
    getFinalizedEvaluation: vi.fn(),
    getRunEvents: vi.fn(),
    getRunReport: vi.fn(),
    artifactUrl: (sha) => `/api/artifacts/${sha}`,
  };
}

describe("IdempotentLaunchLock", () => {
  it("reuses the same key after an ambiguous failure", async () => {
    const keys: string[] = [];
    const launchRun = vi
      .fn<ApiClient["launchRun"]>()
      .mockImplementationOnce((_value, key) => {
        keys.push(key);
        return Promise.reject(new TypeError("connection ended"));
      })
      .mockImplementationOnce((_value, key) => {
        keys.push(key);
        return Promise.resolve(runFixture);
      });
    const lock = new IdempotentLaunchLock(
      clientWithLaunch(launchRun),
      () => "stable-key",
    );

    await expect(lock.launch(input)).rejects.toThrow("connection ended");
    await expect(lock.launch({ ...input, solution_ids: [...input.solution_ids].reverse() }))
      .resolves.toEqual(runFixture);

    expect(keys).toEqual(["stable-key", "stable-key"]);
    expect(launchRun).toHaveBeenLastCalledWith(
      expect.objectContaining({
        solution_ids: ["pipeline-a", "pipeline-b"],
      }),
      "stable-key",
      undefined,
    );
  });

  it("coalesces concurrent identical launches", async () => {
    let resolve!: (run: typeof runFixture) => void;
    const pending = new Promise<typeof runFixture>((done) => {
      resolve = done;
    });
    const launchRun = vi.fn<ApiClient["launchRun"]>(() => pending);
    const lock = new IdempotentLaunchLock(
      clientWithLaunch(launchRun),
      () => "one-key",
    );

    const first = lock.launch(input);
    const second = lock.launch(input);
    expect(first).toBe(second);
    resolve(runFixture);
    await first;
    expect(launchRun).toHaveBeenCalledTimes(1);
  });

  it("classifies only retry-safe ambiguity as ambiguous", () => {
    expect(isAmbiguousLaunchFailure(new ApiError("server", 503))).toBe(true);
    expect(isAmbiguousLaunchFailure(new ApiError("invalid", 400))).toBe(false);
  });

  it("restores hosted ambiguous launch identity across reloads and clears it after success", async () => {
    sessionStorage.clear();
    const launch = vi.fn<ApiClient["launchRun"]>()
      .mockRejectedValueOnce(new ApiError("busy", 429, "12"))
      .mockResolvedValue(runFixture);
    const client = clientWithLaunch(launch);
    const first = new IdempotentLaunchLock(client, () => "v1.100.original", sessionStorage);
    await expect(first.launch(input)).rejects.toThrow(/Retry after/);
    const reloaded = new IdempotentLaunchLock(client, () => "v1.101.new", sessionStorage);
    await reloaded.launch(input);
    expect(launch.mock.calls.map((call) => call[1])).toEqual(["v1.100.original", "v1.100.original"]);
    expect(sessionStorage.getItem("ai-bench.pending-launch")).toBeNull();
    await reloaded.launch(input);
    expect(launch.mock.calls[2][1]).toBe("v1.101.new");
  });
});
