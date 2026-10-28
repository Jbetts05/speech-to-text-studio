import { webcrypto } from "node:crypto";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, type ApiClient } from "../api/client";
import { datasetImportFixture, uploadSessionFixture } from "../testing/hostedFixtures";
import type { UploadRequest, UploadSession } from "../types";
import { hashUploadFiles, readUploadIdentity, saveUploadIdentity, transmitUpload, validateUploadFiles, type UploadIdentity } from "./datasetUpload";

export function syntheticFile(name: string, text = "synthetic", size?: number): File {
  const file = new File([text], name);
  Object.defineProperty(file, "arrayBuffer", { value: async () => new TextEncoder().encode(text).buffer });
  if (size !== undefined) Object.defineProperty(file, "size", { value: size });
  return file;
}

function setupTransport() {
  let stored: UploadSession | undefined;
  const api = {
    createOperationKey: vi.fn(() => `operation-${Math.random()}`),
    createUpload: vi.fn(async (request: UploadRequest, _key: string) => stored ??= uploadSessionFixture(request)),
    getUpload: vi.fn(async () => structuredClone(stored!)),
    uploadPart: vi.fn(async (_id: string, partId: string) => {
      stored = { ...stored!, parts: stored!.parts.map((part) => part.part_id === partId ? { ...part, status: "complete" } : part) };
      return structuredClone(stored);
    }),
    finalizeUpload: vi.fn(async (_id: string, _key: string) => stored = { ...stored!, status: "validating" }),
  };
  let identity: UploadIdentity | undefined;
  const files = [syntheticFile("one.wav"), syntheticFile("one.txt")];
  const invoke = (selected = files) => transmitUpload({
    api: api as unknown as ApiClient, description: datasetImportFixture,
    benchmarkId: "bench_general", name: "Synthetic", files: selected, identity,
    signal: new AbortController().signal, onIdentity: (value) => {
      identity = value; saveUploadIdentity("bench_general", value);
    }, onSession: vi.fn(), onProgress: vi.fn(),
  });
  return { api, invoke, files, getIdentity: () => identity };
}

beforeEach(() => { vi.stubGlobal("crypto", webcrypto); sessionStorage.clear(); });
afterEach(() => vi.unstubAllGlobals());

describe("bounded paired dataset upload", () => {
  it.each([
    [["a.wav"], /Missing exact/],
    [["a.wav", "A.txt"], /case and Unicode/],
    [["é.wav", "e\u0301.txt"], /case and Unicode/],
    [["a.wav", "A.WAV", "a.txt"], /collision/],
    [["a.wav", "a.mp3", "a.txt"], /exactly one/],
    [["../a.wav", "a.txt"], /plain filenames/],
    [["a\\b.wav", "b.txt"], /plain filenames/],
    [["a.zip", "a.txt"], /Unsupported/],
    [["a.wav", "a.txt", "a.txt"], /collision/],
  ])("rejects invalid selection %j before hashing", (names, error) => {
    expect(() => validateUploadFiles(names.map((name) => syntheticFile(name)), datasetImportFixture)).toThrow(error);
  });

  it("enforces each size, count and aggregate boundary without reading file content", () => {
    expect(() => validateUploadFiles([syntheticFile("a.wav", "", 25_000_000), syntheticFile("a.txt", "", 16_384)], datasetImportFixture)).not.toThrow();
    expect(() => validateUploadFiles([syntheticFile("a.wav", "", 25_000_001), syntheticFile("a.txt")], datasetImportFixture)).toThrow(/25,000,000/);
    expect(() => validateUploadFiles([syntheticFile("a.wav"), syntheticFile("a.txt", "", 16_385)], datasetImportFixture)).toThrow(/16,384/);
    const twenty = Array.from({ length: 10 }, (_, i) => [syntheticFile(`${i}.wav`, "", 25_000_000), syntheticFile(`${i}.txt`)]).flat();
    expect(() => validateUploadFiles(twenty, datasetImportFixture)).toThrow(/total/);
    expect(() => validateUploadFiles([...twenty, syntheticFile("extra.wav")], datasetImportFixture)).toThrow(/20 files/);
    expect(() => validateUploadFiles([syntheticFile("a.wav", "", 0), syntheticFile("a.txt")], datasetImportFixture)).toThrow(/nonempty/);
  });

  it("hashes files sequentially with exact SHA-256 identities", async () => {
    let active = 0;
    const files = ["b.txt", "b.wav", "a.wav", "a.txt"].map((name) => {
      const file = new File(["synthetic"], name);
      Object.defineProperty(file, "arrayBuffer", { value: async () => {
        active += 1; expect(active).toBe(1);
        await Promise.resolve(); active -= 1;
        return new TextEncoder().encode("synthetic").buffer;
      } });
      return file;
    });
    const result = await hashUploadFiles(files, new AbortController().signal, vi.fn());
    expect(result.map((file) => file.name)).toEqual(["a.txt", "a.wav", "b.txt", "b.wav"]);
    expect(result.every((file) => /^[a-f0-9]{64}$/.test(file.sha256))).toBe(true);
    expect(new Set(result.map((file) => file.sha256)).size).toBe(1);
  });

  it("reserves JSON, sends individual files, and only then finalizes", async () => {
    const { api, invoke, getIdentity } = setupTransport();
    expect((await invoke()).status).toBe("validating");
    expect(api.createUpload).toHaveBeenCalledTimes(1);
    expect(api.uploadPart).toHaveBeenCalledTimes(2);
    expect(api.finalizeUpload).toHaveBeenCalledTimes(1);
    expect(readUploadIdentity("bench_general")).toEqual(getIdentity());
    expect(sessionStorage.getItem("ai-bench.upload.bench_general")).not.toContain("sha256");
  });

  it("reuses a lost admission response key, never reserving a second session", async () => {
    const { api, invoke } = setupTransport();
    const create = api.createUpload.getMockImplementation()!;
    api.createUpload.mockImplementationOnce(async (request, key) => {
      await create(request, key); throw new TypeError("response lost");
    });
    await expect(invoke()).rejects.toThrow("response lost");
    await invoke();
    expect(api.createUpload.mock.calls[0][1]).toBe(api.createUpload.mock.calls[1][1]);
    expect(api.uploadPart).toHaveBeenCalledTimes(2);
  });

  it("reads server status after a lost part response and skips completed parts", async () => {
    const { api, invoke } = setupTransport();
    const put = api.uploadPart.getMockImplementation()!;
    api.uploadPart.mockImplementationOnce(async (id, partId) => {
      await put(id, partId); throw new TypeError("part response lost");
    });
    await expect(invoke()).rejects.toThrow("part response lost");
    await invoke();
    expect(api.getUpload).toHaveBeenCalledTimes(1);
    expect(api.createUpload).toHaveBeenCalledTimes(1);
    expect(api.uploadPart).toHaveBeenCalledTimes(2);
  });

  it("does not transmit changed files into an existing reservation", async () => {
    const { api, invoke, files } = setupTransport();
    api.uploadPart.mockRejectedValueOnce(new ApiError("busy", 429, "15"));
    await expect(invoke()).rejects.toThrow(/Retry after 15 seconds/);
    await expect(invoke([files[0], syntheticFile("one.txt", "changed")])).rejects.toThrow(/original files/);
    expect(api.uploadPart).toHaveBeenCalledTimes(1);
    expect(api.finalizeUpload).not.toHaveBeenCalled();
  });

  it("retains finalize identity after ambiguous failure without resending completed files", async () => {
    const { api, invoke } = setupTransport();
    api.finalizeUpload.mockRejectedValueOnce(new ApiError("busy", 429, "10"));
    await expect(invoke()).rejects.toThrow(/Server is busy/);
    await invoke();
    expect(api.finalizeUpload.mock.calls[0][1]).toBe(api.finalizeUpload.mock.calls[1][1]);
    expect(api.uploadPart).toHaveBeenCalledTimes(2);
  });

  it("does not finalize when a part response has not confirmed durable completion", async () => {
    const { api, invoke } = setupTransport();
    api.uploadPart.mockImplementationOnce(async () => api.getUpload());
    await expect(invoke()).rejects.toThrow(/not confirmed this file/);
    expect(api.finalizeUpload).not.toHaveBeenCalled();
  });

  it("a paused hash pass never admits or transmits a dataset", async () => {
    const { api, files } = setupTransport();
    const controller = new AbortController();
    await expect(transmitUpload({
      api: api as unknown as ApiClient, description: datasetImportFixture,
      benchmarkId: "bench_general", name: "Synthetic", files, signal: controller.signal,
      onIdentity: vi.fn(), onSession: vi.fn(), onProgress: () => controller.abort(),
    })).rejects.toMatchObject({ name: "AbortError" });
    expect(api.createUpload).not.toHaveBeenCalled();
    expect(api.uploadPart).not.toHaveBeenCalled();
  });
});
