import { afterEach, describe, expect, it, vi } from "vitest";
import { HttpApiClient } from "./client";
import { finalizedWireFixture } from "../testing/finalizedFixtures";
import { runId } from "../testing/fixtures";

afterEach(() => vi.unstubAllGlobals());

describe("finalized HTTP contract", () => {
  it("requests one no-store finalized representation without hidden live/report requests", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(finalizedWireFixture)));
    vi.stubGlobal("fetch", fetch);
    const controller = new AbortController();
    const result = await new HttpApiClient().getFinalizedEvaluation(runId, controller.signal);
    expect(result.record_source).toBe("finalized_report");
    expect(result.run.tasks).toBeUndefined();
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch).toHaveBeenCalledWith(`/api/runs/${runId}/finalized-evaluation`, expect.objectContaining({ signal: controller.signal, cache: "no-store" }));
  });

  it("returns only an explicit typed live fallback", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      record_source: "live_detail_required", run: finalizedWireFixture.run,
      reason: "incomplete_attempt_history", detail: "Historical attempts are incomplete.",
    }))));
    const result = await new HttpApiClient().getFinalizedEvaluation(runId);
    expect(result.record_source).toBe("live_detail_required");
  });

  it("rejects mismatched immutable identity rather than interpreting it as a fallback", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      ...finalizedWireFixture,
      report: { ...finalizedWireFixture.report, run_id: "9".repeat(64) },
    }))));
    await expect(new HttpApiClient().getFinalizedEvaluation(runId)).rejects.toThrow("Finalized evaluation identity is invalid.");
  });

  it.each([409, 410, 503])("retains HTTP %s as an error without live fallback", async status => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "Read denied" }), { status }));
    vi.stubGlobal("fetch", fetch);
    await expect(new HttpApiClient().getFinalizedEvaluation(runId)).rejects.toMatchObject({ status });
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});
