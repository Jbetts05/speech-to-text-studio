import { webcrypto } from "node:crypto";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { HttpApiClient } from "../api/client";
import { mapBenchmark } from "../api/wire";
import { benchmarkWireFixture } from "../testing/fixtures";
import { datasetFixture, datasetImportFixture, hostedRuntimeFixture, uploadSessionFixture } from "../testing/hostedFixtures";
import type { Dataset, UploadRequest, UploadSession } from "../types";
import { DatasetWorkspace } from "./DatasetWorkspace";
import { saveUploadIdentity } from "../state/datasetUpload";

const benchmark = mapBenchmark({ ...benchmarkWireFixture, dataset_import: datasetImportFixture });
beforeEach(() => { sessionStorage.clear(); vi.stubGlobal("crypto", webcrypto); });
afterEach(() => vi.unstubAllGlobals());

function setup() {
  let session: UploadSession | undefined;
  const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    let body: unknown;
    if (path === "/api/runtime") body = hostedRuntimeFixture;
    else if (path.includes("/datasets")) body = { items: [datasetFixture], next_cursor: null };
    else if (path === "/api/dataset-uploads") {
      session = uploadSessionFixture(JSON.parse(String(init?.body)) as UploadRequest);
      body = session;
    } else if (init?.method === "PUT") {
      session = { ...session!, parts: session!.parts.map((part) => path.endsWith(part.part_id) ? { ...part, status: "complete" } : part) };
      body = session;
    } else if (path.endsWith("/finalize")) {
      session = { ...session!, status: "ready", dataset_id: datasetFixture.id }; body = session;
    } else body = session;
    return new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fetch);
  return { api: new HttpApiClient(), fetch };
}

describe("public dataset workspace", () => {
  it("previews one ready sample at a time with native audio, safe downloads and reusable inspection before any run", async () => {
    const user = userEvent.setup();
    const selected: Dataset = {
      ...datasetFixture, sample_count: 10, scored_sample_count: 10,
      sample_artifacts: Object.fromEntries(Array.from({ length: 10 }, (_, index) => [
        `sample-${index}`,
        [
          { role: "input", artifact_id: `input-${index}`, modality: "audio", media_type: "audio/wav", size_bytes: 32044 },
          { role: "reference", artifact_id: `reference-${index}`, modality: "text", media_type: "text/plain", size_bytes: 9 },
        ],
      ])),
    };
    const fetch = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) =>
      String(input).includes("/api/artifacts/")
        ? new Response(`Synthetic reference ${String(input).split("-").at(-1)}`, { headers: { "Content-Type": "text/plain" } })
        : new Response(JSON.stringify({ items: [selected] })),
    );
    vi.stubGlobal("fetch", fetch);
    const inspect = vi.fn();
    render(<DatasetWorkspace api={new HttpApiClient()} benchmark={benchmark} selected={selected} selectedId={selected.id} onSelect={vi.fn()} onInspect={inspect} />);
    await screen.findByText("Synthetic reference 0");
    expect(await screen.findByRole("option", { name: /Synthetic public examples · 10 scored samples · expires/ })).toBeInTheDocument();
    expect(document.querySelectorAll("audio")).toHaveLength(1);
    expect(screen.getByLabelText("input · sample-0")).toHaveAttribute("controls");
    expect(screen.getByLabelText("input · sample-0")).toHaveAttribute("preload", "metadata");
    expect(screen.getByRole("link", { name: "Download input · sample-0" })).toHaveAttribute("href", "/api/artifacts/input-0");
    expect(screen.getByRole("link", { name: "Download reference · sample-0" })).toHaveAttribute("download");
    expect(fetch.mock.calls.filter(([input]) => String(input).includes("/api/artifacts/"))).toHaveLength(1);
    await user.selectOptions(screen.getByLabelText("Dataset sample"), "sample-9");
    await screen.findByText("Synthetic reference 9");
    expect(document.querySelectorAll("audio")).toHaveLength(1);
    expect(screen.queryByLabelText("input · sample-0")).not.toBeInTheDocument();
    expect(screen.getByLabelText("input · sample-9")).toHaveAttribute("src", "/api/artifacts/input-9");
    await user.click(screen.getAllByRole("button", { name: "Inspect artifact" })[0]);
    expect(inspect).toHaveBeenCalledWith(expect.objectContaining({ source: "sample", artifact_sha256: "input-9", role: "input" }), expect.any(HTMLButtonElement));
    expect(fetch.mock.calls.some(([input, init]) => String(input).includes("/api/runs") || init?.method === "POST")).toBe(false);
    expect((await axe.run(document.body, { rules: { "color-contrast": { enabled: false }, region: { enabled: false } } })).violations).toEqual([]);
  });

  it("requires all explicit warnings before hashing/transmission and passes axe", async () => {
    const user = userEvent.setup();
    const { api, fetch } = setup();
    await api.getRuntime();
    const selected = vi.fn();
    render(<DatasetWorkspace api={api} benchmark={benchmark} onSelect={selected} onInspect={vi.fn()} />);
    await user.click(screen.getByText("Import a public dataset"));
    await user.type(screen.getByLabelText("Dataset name"), "Synthetic");
    const readFile = vi.fn(async () => new TextEncoder().encode("synthetic").buffer);
    const files = ["one.wav", "one.txt"].map((name) => {
      const file = new File(["synthetic"], name);
      Object.defineProperty(file, "arrayBuffer", { value: readFile });
      return file;
    });
    await user.upload(screen.getByLabelText("Select input and reference files"), files);
    const submit = screen.getByRole("button", { name: "Upload public dataset" });
    expect(submit).toBeDisabled();
    expect(readFile).not.toHaveBeenCalled();
    expect(screen.getByText(/PUBLIC: anyone can view, play and download/)).toBeInTheDocument();
    expect(screen.getByText(/expire ten days after publication/)).toBeInTheDocument();
    for (const checkbox of screen.getAllByRole("checkbox")) await user.click(checkbox);
    expect(submit).toBeEnabled();
    expect((await axe.run(document.body, { rules: { "color-contrast": { enabled: false }, region: { enabled: false } } })).violations).toEqual([]);
    await user.click(submit);
    await waitFor(() => expect(selected).toHaveBeenCalledWith(datasetFixture.id));
    expect(readFile).toHaveBeenCalledTimes(2);
    expect(fetch.mock.calls.filter(([, init]) => init?.method === "PUT")).toHaveLength(2);
    expect(fetch.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(2);
    expect(screen.getByText("Dataset published.")).toBeInTheDocument();
  });

  it("restores a session without automatically retransmitting and marks HTTP 410 expired", async () => {
    saveUploadIdentity(benchmark.id, { key: "same-operation", name: "Synthetic", uploadId: "upload-expired" });
    const fetch = vi.fn(async (input: RequestInfo | URL) => new Response(JSON.stringify(
      String(input).includes("dataset-uploads") ? { detail: "upload expired" } : { items: [], next_cursor: null },
    ), { status: String(input).includes("dataset-uploads") ? 410 : 200 }));
    vi.stubGlobal("fetch", fetch);
    render(<DatasetWorkspace api={new HttpApiClient()} benchmark={benchmark} onSelect={vi.fn()} onInspect={vi.fn()} />);
    expect(await screen.findByText("Upload expired. It cannot be resumed.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Resume upload" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Check upload status" })).not.toBeInTheDocument();
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("button", { name: "Forget resume link (not delete)" })).toBeEnabled();
  });
});
