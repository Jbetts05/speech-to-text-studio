import { createHash } from "node:crypto";
import { expect, test, type Page } from "@playwright/test";
import axe from "axe-core";
import { benchmarkWireFixture, runId, runDetailWireFixture, runSummaryWireFixture, reportWireFixture } from "../../src/testing/fixtures";
import { datasetImportFixture, datasetFixture, hostedRuntimeFixture, uploadSessionFixture } from "../../src/testing/hostedFixtures";
import type { Dataset, UploadSession } from "../../src/types";

function syntheticWav() {
  const data = Buffer.alloc(44 + 3200);
  data.write("RIFF", 0); data.writeUInt32LE(data.length - 8, 4); data.write("WAVEfmt ", 8);
  data.writeUInt32LE(16, 16); data.writeUInt16LE(1, 20); data.writeUInt16LE(1, 22);
  data.writeUInt32LE(16000, 24); data.writeUInt32LE(32000, 28); data.writeUInt16LE(2, 32); data.writeUInt16LE(16, 34);
  data.write("data", 36); data.writeUInt32LE(3200, 40);
  return data;
}

async function mockHosted(page: Page, losePart = false, multipleSamples = false) {
  let session: UploadSession | undefined;
  let puts = 0;
  let creates = 0;
  const requests: { path: string; method: string }[] = [];
  const wav = syntheticWav();
  const pinnedDefault: Dataset = { ...datasetFixture, id: "pinned-published-default", pinned: true, expires_at: null };
  const sample: Dataset = {
    ...datasetFixture,
    sample_count: 1, scored_sample_count: 1,
    sample_artifacts: { synthetic: [
      { role: "input", artifact_id: "a".repeat(64), modality: "audio", media_type: "audio/wav", size_bytes: wav.length },
      { role: "reference", artifact_id: "b".repeat(64), modality: "text", media_type: "text/plain", size_bytes: 9 },
    ] },
  };
  if (multipleSamples) {
    sample.sample_count = sample.scored_sample_count = 2;
    sample.sample_artifacts!["synthetic-2"] = [
      { role: "input", artifact_id: "c".repeat(64), modality: "audio", media_type: "audio/wav", size_bytes: wav.length },
      { role: "reference", artifact_id: "d".repeat(64), modality: "text", media_type: "text/plain", size_bytes: 9 },
    ];
  }
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    requests.push({ path, method: request.method() });
    let body: unknown;
    if (path === "/api/benchmarks") body = [{
      ...benchmarkWireFixture, dataset_import: datasetImportFixture,
      default_dataset_id: pinnedDefault.id,
    }];
    else if (path === "/api/runtime") body = {
      ...hostedRuntimeFixture, landing: { benchmark_id: "bench_general", run_id: runId, view: "evaluation" },
    };
    else if (path === "/api/runs" && request.method() === "POST") {
      expect(request.postDataJSON().dataset_id).toBe(pinnedDefault.id);
      expect(request.postDataJSON().allow_networked).toBe(true);
      expect(request.headers()["x-ai-bench-intent"]).toBe("public-demo");
      body = {
        run_id: runId, status: "created", manifest_ref: "manifest", manifest_artifact_id: "a".repeat(64),
        report_ref: null, report_artifact_id: null,
      };
    }
    else if (path === "/api/runs") body = [runSummaryWireFixture];
    else if (path === `/api/runs/${runId}/finalized-evaluation`) body = { record_source: "live_detail_required", run: runSummaryWireFixture, reason: "incomplete_results", detail: "Synthetic fixture has partial historical records; loading live detail." };
    else if (path === `/api/runs/${runId}`) body = { ...runDetailWireFixture, status: "completed", pinned: true };
    else if (path.endsWith("/events")) body = [];
    else if (path.endsWith("/report")) body = reportWireFixture;
    else if (path === "/api/benchmarks/bench_general/datasets") body = { items: session?.status === "ready" ? [pinnedDefault, sample] : [pinnedDefault] };
    else if (path === `/api/datasets/${pinnedDefault.id}`) body = pinnedDefault;
    else if (path === `/api/datasets/${datasetFixture.id}`) body = sample;
    else if (path === "/api/dataset-uploads") {
      expect(request.headers()["x-ai-bench-intent"]).toBe("public-demo");
      expect(request.headers()["idempotency-key"]).toMatch(/^v1\.\d+\.[a-f0-9-]+$/);
      expect(request.postDataBuffer()!.length).toBeLessThan(32768);
      creates += 1;
      session = uploadSessionFixture(request.postDataJSON());
      body = session;
    } else if (path.includes("/parts/")) {
      puts += 1;
      expect(request.method()).toBe("PUT");
      expect(request.headers()["content-type"]).toBe("application/octet-stream");
      expect(request.headers()["x-ai-bench-intent"]).toBe("public-demo");
      const partId = path.split("/").at(-1)!;
      const part = session!.parts.find((item) => item.part_id === partId)!;
      expect(createHash("sha256").update(request.postDataBuffer()!).digest("hex")).toBe(part.sha256);
      expect(part.status).toBe("pending");
      session = { ...session!, parts: session!.parts.map((item) => item.part_id === partId ? { ...item, status: "complete" } : item) };
      if (losePart && puts === 1) { await route.abort("failed"); return; }
      body = session;
    } else if (path.endsWith("/finalize")) {
      expect(session!.parts.every((part) => part.status === "complete")).toBe(true);
      expect(request.postDataJSON()).toEqual({});
      session = { ...session!, status: "ready", dataset_id: datasetFixture.id };
      body = session;
    } else if (path.startsWith("/api/dataset-uploads/")) body = session;
    else if ([`/api/artifacts/${"a".repeat(64)}`, `/api/artifacts/${"c".repeat(64)}`].includes(path)) {
      await route.fulfill({ contentType: "audio/wav", body: wav }); return;
    } else if ([`/api/artifacts/${"b".repeat(64)}`, `/api/artifacts/${"d".repeat(64)}`].includes(path)) {
      await route.fulfill({ contentType: "text/plain", body: "synthetic" }); return;
    } else body = {};
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
  });
  return {
    files: [
      { name: "synthetic.wav", mimeType: "audio/wav", buffer: wav },
      { name: "synthetic.txt", mimeType: "text/plain", buffer: Buffer.from("synthetic") },
    ],
    counts: () => ({ puts, creates }),
    requests,
  };
}

async function acknowledge(page: Page) {
  const checks = page.getByRole("group", { name: "Required before any transmission" }).getByRole("checkbox");
  for (let index = 0; index < 4; index += 1) await checks.nth(index).check();
}

test("public multiselect upload survives lost response and reload without resending completed files", async ({ page }) => {
  const { files, counts } = await mockHosted(page, true);
  await page.goto("/?view=configure&benchmark=bench_general");
  await page.getByText("Import a public dataset", { exact: true }).click();
  await page.getByLabel("Dataset name", { exact: true }).fill("Synthetic session");
  await page.getByLabel("Select input and reference files").setInputFiles(files);
  await expect(page.getByRole("button", { name: "Upload public dataset" })).toBeDisabled();
  await expect(page.getByText(/PUBLIC: anyone can view, play and download/)).toBeVisible();
  await acknowledge(page);
  await page.getByRole("button", { name: "Upload public dataset" }).click();
  await expect(page.getByText(/Failed to fetch/)).toBeVisible();
  await page.reload();
  await expect(page.getByText(/completed files will not be resent/)).toBeVisible();
  await expect(page.getByLabel("Select input and reference files")).toHaveValue("");
  await page.getByLabel("Select input and reference files").setInputFiles(files);
  await acknowledge(page);
  await page.getByRole("button", { name: "Resume upload" }).click();
  await expect(page.getByText("Dataset published.", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Dataset for the next run")).toHaveValue(datasetFixture.id);
  expect(counts()).toEqual({ creates: 1, puts: 2 });
  await expect(page).toHaveURL(new RegExp(`dataset=${datasetFixture.id}`));
  await expect(page.getByRole("combobox", { name: "Dataset sample", exact: true })).toHaveValue("synthetic");
  await expect(page.locator("audio")).toHaveAttribute("controls", "");
  await expect(page.getByRole("link", { name: "Download input · synthetic" })).toHaveAttribute("download", "");
  await page.addScriptTag({ content: axe.source });
  const violations = await page.evaluate(async () => (await window.axe.run(document.body)).violations);
  expect(violations).toEqual([]);
});

test("public baseline landing yields to explicit configure bookmarks", async ({ page }) => {
  await mockHosted(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Evaluation detail" })).toBeVisible();
  await expect(page.getByText("Pinned · no expiry")).toBeVisible();
  await page.goto("/?view=configure&benchmark=bench_general");
  await expect(page.getByRole("heading", { name: "Configure run", exact: true })).toBeVisible();
  await expect(page.getByRole("spinbutton", { name: "Retry limit" })).toHaveAttribute("max", "3");
  expect(new URL(page.url()).searchParams.has("run")).toBe(false);
});

test("ready dataset detail plays and downloads only the selected sample without a paid run", async ({ page }) => {
  const { requests } = await mockHosted(page, false, true);
  await page.goto(`/?view=benchmarks&benchmark=bench_general&dataset=${datasetFixture.id}`);
  await expect(page.getByRole("combobox", { name: "Dataset sample", exact: true })).toHaveValue("synthetic");
  await expect(page.locator("audio")).toHaveCount(1);
  await expect(page.locator("audio")).toHaveAttribute("controls", "");
  await expect(page.locator("audio")).toHaveAttribute("preload", "metadata");
  await expect.poll(() => page.locator("audio").evaluate((element) => (element as HTMLAudioElement).readyState)).toBeGreaterThan(0);
  await expect(page.getByRole("link", { name: "Download reference · synthetic", exact: true })).toHaveAttribute("download", "");
  expect(requests.some(({ path }) => path === `/api/artifacts/${"c".repeat(64)}` || path === `/api/artifacts/${"d".repeat(64)}`)).toBe(false);
  await page.getByRole("combobox", { name: "Dataset sample", exact: true }).selectOption("synthetic-2");
  await expect(page.locator("audio")).toHaveCount(1);
  await expect(page.locator("audio")).toHaveAttribute("src", `/api/artifacts/${"c".repeat(64)}`);
  await expect(page.getByRole("link", { name: "Download input · synthetic-2", exact: true })).toHaveAttribute("download", "");
  await page.getByRole("button", { name: "Inspect artifact", exact: true }).first().click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(page.getByRole("dialog").getByRole("link", { name: /Download/ })).toHaveAttribute("download", "");
  expect(requests.some(({ path, method }) => path === "/api/runs" && method === "POST")).toBe(false);
});

test("bare baseline launches its published default without a picker change or acquisition source", async ({ page }) => {
  const { requests } = await mockHosted(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Evaluation detail" })).toBeVisible();
  await page.getByRole("navigation", { name: "Primary navigation" }).getByRole("button", { name: "Configure run", exact: true }).click();
  await expect(page.getByLabel("Dataset for the next run")).toHaveValue("");
  await expect(page.getByLabel("Allow networked execution")).toHaveCount(0);
  await page.getByRole("button", { name: "Launch benchmark" }).click();
  await expect(page.getByRole("heading", { name: "Evaluation detail" })).toBeVisible();
  expect(requests.filter(({ path, method }) => path === "/api/runs" && method === "POST")).toHaveLength(1);
});

test("post-reaping 404 bookmarks stop detail and event polling", async ({ page }) => {
  await page.clock.install();
  await mockHosted(page);
  const runReads: string[] = [];
  await page.route("**/api/runs/**", async (route) => {
    runReads.push(new URL(route.request().url()).pathname);
    await route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ detail: "run was reaped" }) });
  });
  await page.goto(`/?view=evaluation&run=${runId}`);
  await expect(page.getByRole("heading", { name: "Run unavailable" })).toBeVisible();
  await expect(page.getByText("Solution performance", { exact: true })).toHaveCount(0);
  const count = runReads.length;
  await page.clock.runFor(30_000);
  expect(runReads).toHaveLength(count);
  await expect(page.getByRole("dialog")).toHaveCount(0);
});
