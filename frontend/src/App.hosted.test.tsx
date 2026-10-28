import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { ApiError, HttpApiClient } from "./api/client";
import { benchmarkWireFixture, runId, runDetailWireFixture, runSummaryWireFixture, reportWireFixture, reportArtifactId } from "./testing/fixtures";
import { datasetFixture, datasetImportFixture, hostedRuntimeFixture } from "./testing/hostedFixtures";
import { artifactExpiredEvent } from "./api/expiry";
import type { Dataset, Run } from "./types";

function hostedApi({
  datasetExpired = false, runExpired = false, reportExpired = false,
  runMissing = false, eventsMissing = false, landing = false, networkAllowed = true,
  publishedDefault = false, local = false, loseLaunchResponse = false,
  launchGone = false, datasetGoneAfterLaunch = false,
} = {}) {
  const pinnedDataset = { ...datasetFixture, id: "pinned-published-default", pinned: true, expires_at: null };
  let launchResponseLost = false;
  let launchGoneReturned = false;
  const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL(String(input), "http://localhost").pathname;
    let body: unknown;
    let status = 200;
    if (path === "/api/benchmarks") body = [{
      ...benchmarkWireFixture, dataset_import: datasetImportFixture,
      default_dataset_id: publishedDefault ? pinnedDataset.id : null,
    }];
    else if (path === "/api/runtime") body = {
      ...hostedRuntimeFixture, allow_networked: networkAllowed,
      ...(local ? { runtime: "local", public_demo: false, max_attempts: 20 } : {}),
      landing: !local || landing ? { benchmark_id: "bench_general", run_id: runId, view: "evaluation" } : null,
    };
    else if (path === "/api/benchmarks/bench_general/datasets") body = { items: publishedDefault ? [pinnedDataset, datasetFixture] : [datasetFixture] };
    else if (path === `/api/datasets/${pinnedDataset.id}`) body = pinnedDataset;
    else if (path === `/api/datasets/${datasetFixture.id}`) {
      const expired = datasetExpired || (datasetGoneAfterLaunch && launchGoneReturned);
      body = expired ? { detail: "dataset expired" } : datasetFixture;
      status = expired ? 410 : 200;
    } else if (path === "/api/runs" && init?.method === "POST") {
      if (publishedDefault && !local && JSON.parse(String(init.body)).dataset_id !== pinnedDataset.id) {
        body = { detail: "Legacy external source is unavailable after migration." }; status = 422;
      } else if (launchGone && !launchGoneReturned) {
        launchGoneReturned = true;
        body = { detail: "Operation key or prior run expired." }; status = 410;
      } else if (loseLaunchResponse && !launchResponseLost) {
        launchResponseLost = true;
        throw new TypeError("response lost");
      } else body = {
        run_id: runId, status: "created", manifest_ref: "manifest", manifest_artifact_id: "a".repeat(64),
        report_ref: null, report_artifact_id: null,
      };
    }
    else if (path === "/api/runs") body = [runSummaryWireFixture];
    else if (path === `/api/runs/${runId}/finalized-evaluation`) body = { record_source: "live_detail_required", run: runSummaryWireFixture, reason: "incomplete_results", detail: "Synthetic fixture has partial historical records; loading live detail." };
    else if (path === `/api/runs/${runId}`) {
      body = runExpired || runMissing ? { detail: "unavailable" } : { ...runDetailWireFixture, status: "completed", pinned: true, report_artifact_id: reportArtifactId };
      status = runExpired ? 410 : runMissing ? 404 : 200;
    } else if (path.endsWith("/events")) {
      body = eventsMissing ? { detail: "run not found after reaping" } : [];
      status = eventsMissing ? 404 : 200;
    }
    else if (path.endsWith("/report")) {
      body = reportExpired ? { detail: "expired" } : reportWireFixture;
      status = reportExpired ? 410 : 200;
    } else body = { content: "synthetic" };
    return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fetch);
  return { api: new HttpApiClient(), fetch };
}

beforeEach(() => { window.history.replaceState({}, "", "/"); sessionStorage.clear(); });
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

describe("hosted dataset and landing state", () => {
  it("keeps routine delivery risk in an accessible quiet disclosure", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/?view=configure&benchmark=bench_general");
    render(<App api={hostedApi({ publishedDefault: true }).api} />);
    const summary = await screen.findByText("Provider delivery and billing context", { selector: "summary" });
    expect(summary.closest("details")).not.toHaveClass("inline-warning");
    expect(summary.closest("details")).not.toHaveAttribute("open");
    await user.click(summary);
    expect(summary.closest("details")).toHaveAttribute("open");
    expect(screen.getByText(/Rare duplicate or overlapping external calls can occur/)).toBeVisible();
    expect(screen.getByText(/Physical non-overlap and exactly-once delivery are not guaranteed/)).toBeVisible();
  });

  it.each(["Retry selected dataset", "Refresh datasets"])("recovers a transient selected-dataset GET through %s", async (action) => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/?view=configure&benchmark=bench_general");
    const { api, fetch } = hostedApi({ publishedDefault: true });
    const detail = vi.spyOn(api, "getDataset").mockRejectedValueOnce(new ApiError("Temporary read pressure", 503));
    render(<App api={api} />);
    const launch = await screen.findByRole("button", { name: "Launch benchmark" });
    await waitFor(() => expect(launch).toHaveAccessibleDescription(/Unable to load selected dataset.*Temporary read pressure/));
    expect(launch).toBeDisabled();
    expect(document.querySelector(".dataset-details")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: action }));
    await waitFor(() => expect(launch).toBeEnabled());
    expect(document.querySelector(".dataset-details")).toHaveTextContent("Synthetic public examples");
    expect(detail).toHaveBeenCalledTimes(2);
    expect(screen.queryByLabelText(/Allow networked execution/)).not.toBeInTheDocument();
    await user.dblClick(launch);
    await screen.findByRole("heading", { name: "Evaluation detail" });
    const posts = fetch.mock.calls.filter(([input, init]) => String(input) === "/api/runs" && init?.method === "POST");
    expect(posts).toHaveLength(1);
    expect(JSON.parse(String(posts[0][1]?.body))).toMatchObject({ dataset_id: "pinned-published-default", allow_networked: true });
  });

  it("keeps a newer selection when an old dataset response ignores abort", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/?view=configure&benchmark=bench_general");
    const { api } = hostedApi({ publishedDefault: true });
    let finishOld: (dataset: Dataset) => void = () => { throw new Error("Old request has not started"); };
    vi.spyOn(api, "getDataset").mockImplementationOnce(() => new Promise<Dataset>(resolve => { finishOld = resolve; }));
    render(<App api={api} />);
    const launch = await screen.findByRole("button", { name: "Launch benchmark" });
    await waitFor(() => expect(launch).toHaveAccessibleDescription(/Loading the selected dataset/));
    await user.selectOptions(screen.getByLabelText("Dataset for the next run"), datasetFixture.id);
    await waitFor(() => expect(launch).toBeEnabled());
    await act(async () => finishOld({ ...datasetFixture, id: "pinned-published-default", name: "Stale pinned response", pinned: true, expires_at: null }));
    expect(screen.queryByText("Stale pinned response")).not.toBeInTheDocument();
    expect(document.querySelector(".dataset-details")).toHaveTextContent("uploaded-synthetic");
    expect(launch).toBeEnabled();
  });

  it("does not let an older same-selection request replace a refreshed result", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/?view=configure&benchmark=bench_general");
    const { api } = hostedApi({ publishedDefault: true });
    let finishOld: (dataset: Dataset) => void = () => { throw new Error("Old request has not started"); };
    vi.spyOn(api, "getDataset").mockImplementationOnce(() => new Promise<Dataset>(resolve => { finishOld = resolve; }));
    render(<App api={api} />);
    await screen.findByRole("button", { name: "Refresh datasets" });
    await waitFor(() => expect(screen.getByRole("button", { name: "Refresh datasets" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Refresh datasets" }));
    const launch = screen.getByRole("button", { name: "Launch benchmark" });
    await waitFor(() => expect(launch).toBeEnabled());
    await act(async () => finishOld({ ...datasetFixture, id: "pinned-published-default", name: "Stale refreshed response", pinned: true, expires_at: null }));
    expect(screen.queryByText("Stale refreshed response")).not.toBeInTheDocument();
    expect(launch).toBeEnabled();
  });

  it("explains the hosted kill switch beside launch without adding an opt-in control", async () => {
    window.history.replaceState({}, "", "/?view=configure&benchmark=bench_general");
    const { api, fetch } = hostedApi({ publishedDefault: true, networkAllowed: false });
    render(<App api={api} />);
    const launch = await screen.findByRole("button", { name: "Launch benchmark" });
    await waitFor(() => expect(launch).toHaveAccessibleDescription("Provider-backed launches are disabled by the runtime."));
    expect(launch).toBeDisabled();
    expect(screen.queryByLabelText(/Allow networked execution/)).not.toBeInTheDocument();
    expect(fetch.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
  });

  it("explains invalid attempt configuration without bypassing hosted limits", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/?view=configure&benchmark=bench_general");
    render(<App api={hostedApi({ publishedDefault: true }).api} />);
    const launch = await screen.findByRole("button", { name: "Launch benchmark" });
    await waitFor(() => expect(launch).toBeEnabled());
    await user.clear(screen.getByRole("spinbutton", { name: "Retry limit" }));
    await user.type(screen.getByRole("spinbutton", { name: "Retry limit" }), "4");
    expect(launch).toBeDisabled();
    expect(launch).toHaveAccessibleDescription("Choose a whole-number attempt limit from 1 to 3.");
  });

  it.each([true, false])("does not expire a live dataset when only the launch operation returns410 (pinned=%s)", async (pinned) => {
    const user = userEvent.setup();
    const selected = pinned ? "pinned-published-default" : datasetFixture.id;
    window.history.replaceState({}, "", `/?view=configure&benchmark=bench_general${pinned ? "" : `&dataset=${selected}`}`);
    const { api, fetch } = hostedApi({ publishedDefault: pinned, launchGone: true });
    render(<App api={api} />);
    expect(screen.queryByLabelText(/Allow networked execution/)).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Launch benchmark" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Launch benchmark" }));
    await screen.findByText("Operation key or prior run expired.");
    await waitFor(() => expect(screen.getByRole("button", { name: "Launch benchmark" })).toBeEnabled());
    expect(screen.queryByText(/Dataset expired/)).not.toBeInTheDocument();
    expect(fetch.mock.calls.filter(([input]) => String(input) === `/api/datasets/${selected}`).length).toBeGreaterThan(1);
    await user.click(screen.getByRole("button", { name: "Launch benchmark" }));
    await screen.findByRole("heading", { name: "Evaluation detail" });
    const posts = fetch.mock.calls.filter(([input, init]) => String(input) === "/api/runs" && init?.method === "POST");
    expect(posts).toHaveLength(2);
    expect(JSON.parse(String(posts[1][1]?.body)).dataset_id).toBe(selected);
    expect(new Headers(posts[0][1]?.headers).get("Idempotency-Key")).not.toBe(new Headers(posts[1][1]?.headers).get("Idempotency-Key"));
  });

  it("expires a selected dataset only after the dataset endpoint confirms launch410", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", `/?view=configure&benchmark=bench_general&dataset=${datasetFixture.id}`);
    render(<App api={hostedApi({ launchGone: true, datasetGoneAfterLaunch: true }).api} />);
    expect(screen.queryByLabelText(/Allow networked execution/)).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Launch benchmark" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Launch benchmark" }));
    await screen.findByText("Dataset expired. Select another dataset.");
    expect(screen.getByRole("button", { name: "Launch benchmark" })).toBeDisabled();
  });

  it("launches the pinned published default from the bare baseline without source paths or picker changes and retains its fingerprint on retry", async () => {
    const user = userEvent.setup();
    const { api, fetch } = hostedApi({ publishedDefault: true, landing: true, loseLaunchResponse: true });
    render(<App api={api} />);
    await screen.findByRole("heading", { name: "Evaluation detail" });
    await user.click(within(screen.getByRole("navigation", { name: "Primary navigation" })).getByRole("button", { name: "Configure run" }));
    expect(screen.getByLabelText("Dataset for the next run")).toHaveValue("");
    expect(screen.queryByLabelText(/Allow networked execution/)).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Launch benchmark" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Launch benchmark" }));
    await screen.findByText("response lost");
    const pending = JSON.parse(sessionStorage.getItem("ai-bench.pending-launch")!);
    expect(JSON.parse(pending.fingerprint).dataset_id).toBe("pinned-published-default");
    expect(fetch.mock.calls.filter(([input, init]) => String(input) === "/api/runs" && init?.method === "POST")).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Launch benchmark" }));
    await screen.findByRole("heading", { name: "Evaluation detail" });
    const posts = fetch.mock.calls.filter(([input, init]) => String(input) === "/api/runs" && init?.method === "POST");
    expect(posts).toHaveLength(2);
    for (const [, init] of posts) {
      expect(JSON.parse(String(init?.body)).dataset_id).toBe("pinned-published-default");
      expect(init?.headers).toMatchObject({ "Idempotency-Key": pending.key, "X-AI-Bench-Intent": "public-demo" });
    }
  });

  it("keeps local default launches selector-free even when a published default exists", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/?view=configure&benchmark=bench_general");
    const { api, fetch } = hostedApi({ publishedDefault: true, local: true });
    render(<App api={api} />);
    await user.click(await screen.findByLabelText(/Allow networked execution/));
    await user.click(screen.getByRole("button", { name: "Launch benchmark" }));
    await screen.findByRole("heading", { name: "Evaluation detail" });
    const [, post] = fetch.mock.calls.find(([input, init]) => String(input) === "/api/runs" && init?.method === "POST")!;
    expect(JSON.parse(String(post?.body))).not.toHaveProperty("dataset_id");
    expect(fetch.mock.calls.some(([input]) => String(input) === "/api/datasets/pinned-published-default")).toBe(false);
  });

  it("fails closed when a hosted launch has no published default or explicit dataset", async () => {
    window.history.replaceState({}, "", "/?view=configure&benchmark=bench_general");
    const { api, fetch } = hostedApi();
    render(<App api={api} />);
    await screen.findByText("No published default dataset is available. Select a published dataset before launching.");
    expect(screen.queryByLabelText(/Allow networked execution/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Launch benchmark" })).toBeDisabled();
    expect(fetch.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
  });

  it.each([{ runMissing: true }, { eventsMissing: true }, { runMissing: true, eventsMissing: true }])(
    "stops both polls for a post-reaping 404 bookmark %j", async (options) => {
      window.history.replaceState({}, "", `/?view=evaluation&run=${runId}`);
      const { api, fetch } = hostedApi(options);
      render(<App api={api} />);
      expect(await screen.findByRole("heading", { name: "Run unavailable" })).toBeInTheDocument();
      expect(screen.queryByText("Solution performance")).not.toBeInTheDocument();
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      const calls = fetch.mock.calls.length;
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 1500)); });
      expect(fetch.mock.calls).toHaveLength(calls);
    },
  );

  it("clears already displayed evidence and its drawer when the next run poll returns 404", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", `/?view=evaluation&run=${runId}`);
    const { api } = hostedApi();
    const getRun = api.getRun.bind(api);
    let missing = false;
    const detail = vi.spyOn(api, "getRun").mockImplementation(async (...args) => {
      if (missing) throw new ApiError("run was reaped", 404);
      return { ...await getRun(...args), status: "running" };
    });
    const events = vi.spyOn(api, "getRunEvents");
    render(<App api={api} />);
    await user.click(await screen.findByText("Operational details", { selector: "summary" }));
    await user.click(await screen.findByRole("button", { name: /comparison · sample-001/i }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    missing = true;
    await screen.findByRole("heading", { name: "Run unavailable" }, { timeout: 5000 });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByText("Solution performance")).not.toBeInTheDocument();
    const calls = [detail.mock.calls.length, events.mock.calls.length];
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 1500)); });
    expect([detail.mock.calls.length, events.mock.calls.length]).toEqual(calls);
  });

  it("shows hosted launch consent and sender risk without a redundant network checkbox", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/?view=configure&benchmark=bench_general");
    render(<App api={hostedApi().api} />);
    expect(await screen.findByText("One current-generation coordinator.")).toBeInTheDocument();
    expect(screen.getByText(/Rare duplicate or overlapping external calls/)).toHaveTextContent("Physical non-overlap and exactly-once delivery are not guaranteed.");
    expect(screen.queryByLabelText(/Allow networked execution/)).not.toBeInTheDocument();
    expect(await screen.findByText(/Launch authorizes live service calls/)).toBeInTheDocument();
    expect(screen.getByRole("spinbutton", { name: "Retry limit" })).toHaveAttribute("max", "3");
    await user.click(screen.getByRole("button", { name: "Admin" }));
    expect(await screen.findByRole("heading", { name: "Admin" })).toBeInTheDocument();
    expect(screen.getByText("One current-generation coordinator.")).toBeInTheDocument();
    expect(screen.getByText(/Rare duplicate or overlapping external calls/)).toBeInTheDocument();
  });

  it.each([false, true])("opens the designated baseline only on a bare URL (deferred hydration=%s)", async (deferred) => {
    const { api, fetch } = hostedApi({ landing: true });
    const getRun = api.getRun.bind(api);
    const detail = vi.spyOn(api, "getRun");
    let releaseRun: () => void = () => { throw new Error("No deferred run request"); };
    if (deferred) {
      const ready = new Promise<void>(resolve => { releaseRun = resolve; });
      detail.mockImplementation(async (...args) => {
        await ready;
        return getRun(...args);
      });
    }
    render(<App api={api} />);
    let placeholderHeading: HTMLElement | undefined;
    if (deferred) {
      placeholderHeading = await screen.findByRole("heading", { name: "Evaluation detail" });
      expect(placeholderHeading).toBeInTheDocument();
      expect(screen.getByText("Select an existing run or configure a new benchmark execution.")).toBeInTheDocument();
      expect(screen.queryByText("Pinned · no expiry")).not.toBeInTheDocument();
      await act(async () => releaseRun());
    }
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Evaluation detail" })).toBeInTheDocument();
      expect(screen.getByText("Pinned · no expiry")).toBeInTheDocument();
      expect(screen.getByRole("region", { name: "Solution performance, horizontally scrollable" })).toBeInTheDocument();
      expect(window.location.pathname).toBe("/");
      expect(Object.fromEntries(new URLSearchParams(window.location.search))).toEqual({
        view: "evaluation", benchmark: "bench_general", run: runId,
      });
    });
    if (placeholderHeading) expect(placeholderHeading).not.toBeInTheDocument();
    expect(detail).toHaveBeenCalledWith(runId, expect.any(AbortSignal));
    expect(fetch.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
  });

  it.each(["?view=configure", "?benchmark=bench_general", `?dataset=${datasetFixture.id}`, "?view=home"])(
    "honors explicit bookmark %s instead of the landing",
    async (query) => {
      window.history.replaceState({}, "", `/${query}`);
      render(<App api={hostedApi({ landing: true }).api} />);
      await screen.findByRole("navigation", { name: "Primary navigation" });
      expect(screen.queryByRole("heading", { name: "Evaluation detail" })).not.toBeInTheDocument();
      expect(window.location.search).not.toContain(`run=${runId}`);
      expect(window.location.search).toContain(query.slice(1).split("&")[0] === "view=home" ? "benchmark=bench_general" : query.slice(1));
    },
  );

  it("restores selected counts and launch dataset without modifying pinned run context", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", `/?view=configure&benchmark=bench_general&dataset=${datasetFixture.id}`);
    const { api, fetch } = hostedApi();
    render(<App api={api} />);
    const selector = await screen.findByLabelText("Dataset for the next run");
    await waitFor(() => expect(selector).toHaveValue(datasetFixture.id));
    await waitFor(() => expect(document.querySelector(".contract-summary")).toHaveTextContent("Tasks6"));
    expect(screen.getByRole("spinbutton", { name: "Retry limit" })).toHaveAttribute("max", "3");
    expect(screen.queryByLabelText(/Allow networked execution/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Launch benchmark" }));
    await waitFor(() => expect(fetch.mock.calls.some(([input, init]) => String(input) === "/api/runs" && init?.method === "POST")).toBe(true));
    const [, post] = fetch.mock.calls.find(([input, init]) => String(input) === "/api/runs" && init?.method === "POST")!;
    expect(JSON.parse(String(post?.body))).toMatchObject({ dataset_id: datasetFixture.id, retry_limit: 2, allow_networked: true });
    expect(post?.headers).toMatchObject({ "X-AI-Bench-Intent": "public-demo", "Idempotency-Key": expect.stringMatching(/^v1\./) });
    expect(await screen.findByRole("heading", { name: "Evaluation detail" })).toBeInTheDocument();
    expect(screen.queryByText("uploaded-synthetic")).not.toBeInTheDocument();
  });

  it("blocks an expired dataset bookmark until another dataset is explicitly selected", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", `/?view=configure&benchmark=bench_general&dataset=${datasetFixture.id}`);
    render(<App api={hostedApi({ datasetExpired: true }).api} />);
    expect(await screen.findByText(/^Dataset expired. Select another dataset;/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Launch benchmark" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Launch benchmark" })).toHaveAccessibleDescription(/Dataset expired/);
    await user.selectOptions(screen.getByLabelText("Dataset for the next run"), "");
    expect(window.location.search).not.toContain("dataset=");
  });

  it.each([{ runExpired: true }, { reportExpired: true }])("stops polling and clears cached report/evidence on 410 %j", async (options) => {
    window.history.replaceState({}, "", `/?view=evaluation&run=${runId}`);
    const { api, fetch } = hostedApi(options);
    render(<App api={api} />);
    expect(await screen.findByRole("heading", { name: "Run expired" })).toBeInTheDocument();
    expect(screen.queryByText("Solution performance")).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    const calls = fetch.mock.calls.length;
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 1400)); });
    expect(fetch.mock.calls).toHaveLength(calls);
  });

  it("ignores late expiration from an unrelated artifact but clears the current run's evidence", async () => {
    window.history.replaceState({}, "", `/?view=evaluation&run=${runId}`);
    const { api } = hostedApi();
    const loadedRun = await api.getRun(runId);
    let finishRun: (run: Run) => void = () => { throw new Error("Run detail has not started"); };
    const detail = vi.spyOn(api, "getRun").mockImplementationOnce(() => new Promise<Run>(resolve => { finishRun = resolve; }));
    render(<App api={api} />);
    await waitFor(() => expect(detail).toHaveBeenCalled());
    await act(async () => finishRun(loadedRun));
    expect(await screen.findByRole("button", { name: "Inspect sample-001 / pipeline-a" })).toBeInTheDocument();
    act(() => window.dispatchEvent(new CustomEvent(artifactExpiredEvent, { detail: { url: "/api/artifacts/unrelated" } })));
    expect(screen.queryByRole("heading", { name: "Run expired" })).not.toBeInTheDocument();
    const artifact = runDetailWireFixture.tasks.flatMap((task) => task.result?.evidence ?? [])[0].artifact_id;
    act(() => window.dispatchEvent(new CustomEvent(artifactExpiredEvent, { detail: { url: `/api/artifacts/${artifact}` } })));
    expect(await screen.findByRole("heading", { name: "Run expired" })).toBeInTheDocument();
  });
});
