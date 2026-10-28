import { afterEach, describe, expect, it, vi } from "vitest";
import { HttpApiClient, ApiError } from "./client";
import { OperationClock } from "../state/operationKey";
import { hostedRuntimeFixture, uploadSessionFixture } from "../testing/hostedFixtures";
import type { UploadRequest } from "../types";

afterEach(() => vi.unstubAllGlobals());

describe("hosted API operations", () => {
  it("uses server epoch plus elapsed monotonic time, not a skewed client clock", () => {
    vi.spyOn(Date, "now").mockReturnValue(1);
    const clock = new OperationClock();
    clock.update(hostedRuntimeFixture);
    expect(clock.createKey()).toMatch(new RegExp(`^v1\\.${Date.parse(hostedRuntimeFixture.server_time!) / 1000}\\.\\w{8}-\\w{4}-4\\w{3}-[89ab]\\w{3}-\\w{12}$`));
    expect(clock.intentHeaders()).toEqual({ "X-AI-Bench-Intent": "public-demo" });
    clock.update({ ...hostedRuntimeFixture, server_time: null });
    expect(() => clock.createKey()).toThrow(/Server time/);
    clock.update({ ...hostedRuntimeFixture, public_demo: false });
    expect(clock.createKey()).not.toMatch(/^v1\./);
    vi.restoreAllMocks();
  });

  it("sends bounded reservation JSON and raw part bodies with public intent", async () => {
    const request: UploadRequest = {
      benchmark_id: "bench_general", name: "Synthetic", language: "en",
      public_acknowledged: true, rights_acknowledged: true, language_acknowledged: true,
      files: [{ name: "one.wav", size_bytes: 9, sha256: "a".repeat(64) }],
    };
    const mock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify(
      String(input) === "/api/runtime" ? hostedRuntimeFixture : uploadSessionFixture(request),
    ), { headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", mock);
    const api = new HttpApiClient();
    await api.getRuntime();
    const key = api.createOperationKey();
    await api.createUpload(request, key);
    const file = new File(["synthetic"], "one.wav");
    await api.uploadPart("upload", "part", file);
    await api.finalizeUpload("upload", key);
    expect(mock.mock.calls[1][1]).toMatchObject({
      method: "POST", body: JSON.stringify(request),
      headers: { "Idempotency-Key": key, "X-AI-Bench-Intent": "public-demo" },
    });
    expect(mock.mock.calls[2][1]).toMatchObject({
      method: "PUT", body: file,
      headers: { "Content-Type": "application/octet-stream", "X-AI-Bench-Intent": "public-demo" },
    });
    expect(mock.mock.calls[3][1]).toMatchObject({ method: "POST", body: "{}" });
  });

  it("preserves status 410 and Retry-After backpressure without retrying writes", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "expired" }), { status: 410 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "receivers occupied" }), { status: 429, headers: { "Retry-After": "12" } }));
    vi.stubGlobal("fetch", fetch);
    const api = new HttpApiClient();
    await expect(api.getDataset("expired")).rejects.toMatchObject({ status: 410 });
    await expect(api.finalizeUpload("upload", "key")).rejects.toEqual(new ApiError("receivers occupied", 429, "12"));
    expect(fetch).toHaveBeenCalledTimes(2);
  });
});
