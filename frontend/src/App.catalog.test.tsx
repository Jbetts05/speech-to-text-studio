import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import type { ApiClient } from "./api/client";
import { mapBenchmark } from "./api/wire";
import { benchmarkWireFixture, reportFixture, runFixture, runId } from "./testing/fixtures";
import { serviceBenchmark, serviceDataset, serviceNames, serviceRuntime } from "./testing/serviceFixtures";
import { costFixture } from "./testing/costFixtures";
import type { RuntimeInfo } from "./types";
import { withCatalogNames } from "./state/catalogPresentation";

const developerBenchmark = { ...mapBenchmark(benchmarkWireFixture), name: "Synthetic text intent" };

function client(runtime: RuntimeInfo = serviceRuntime): ApiClient {
  return {
    listBenchmarks: vi.fn().mockResolvedValue([developerBenchmark, serviceBenchmark]),
    listRuns: vi.fn().mockResolvedValue([runFixture]),
    getRuntime: vi.fn().mockResolvedValue(runtime),
    getDataset: vi.fn().mockResolvedValue(serviceDataset),
    getRun: vi.fn().mockResolvedValue({ ...runFixture, status: "completed" }),
    getFinalizedEvaluation: vi.fn().mockResolvedValue({ record_source: "live_detail_required", run: runFixture, reason: "incomplete_results", detail: "Synthetic fixture has partial recorded results; loading live detail." }),
    getRunEvents: vi.fn().mockResolvedValue({ events: [], next_after: 0 }),
    getRunReport: vi.fn().mockResolvedValue(reportFixture),
    launchRun: vi.fn().mockResolvedValue({ ...runFixture, status: "completed" }),
    artifactUrl: id => `/api/artifacts/${id}`,
  };
}

beforeEach(() => {
  window.history.replaceState({}, "", "/?view=configure");
  sessionStorage.clear();
});

describe("catalog presentation scope", () => {
  it("waits for explicit runtime scope without flashing a developer pack", async () => {
    const api = client();
    let resolveRuntime!: (runtime: RuntimeInfo) => void;
    api.getRuntime = vi.fn(() => new Promise<RuntimeInfo>(resolve => { resolveRuntime = resolve; }));
    render(<App api={api} />);
    await waitFor(() => expect(api.getRuntime).toHaveBeenCalled());
    expect(screen.getByRole("heading", { name: /Loading AI Bench/ })).toBeInTheDocument();
    expect(screen.queryByText("Synthetic text intent")).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    await act(async () => resolveRuntime(serviceRuntime));
    expect(await screen.findByRole("heading", { name: "Select services" })).toBeInTheDocument();
    expect(screen.queryByText("Synthetic text intent")).not.toBeInTheDocument();
    expect(screen.getAllByRole("option", { name: "Service examples" })).toHaveLength(2);
    expect(screen.getAllByRole("checkbox")).toHaveLength(5);
  });

  it.each([
    { ...serviceRuntime, landing: null },
    { ...serviceRuntime, landing: { ...serviceRuntime.landing, benchmark_id: "missing_pack" } },
    { ...serviceRuntime, landing: { ...serviceRuntime.landing, run_id: "" } },
    { ...serviceRuntime, runtime: "unknown" },
  ])("fails closed with an actionable unavailable state for %j", async runtime => {
    const api = client(runtime);
    render(<App api={api} />);
    expect(await screen.findByRole("heading", { name: "Benchmark catalog unavailable" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reload catalog" })).toBeInTheDocument();
    expect(screen.queryByText("Synthetic text intent")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Launch benchmark" })).not.toBeInTheDocument();
    expect(api.launchRun).not.toHaveBeenCalled();
  });

  it("does not claim local offline availability when runtime retrieval fails", async () => {
    const api = client();
    api.getRuntime = vi.fn().mockRejectedValue(new Error("Runtime could not be loaded"));
    render(<App api={api} />);
    expect(await screen.findByRole("heading", { name: "Unable to load AI Bench" })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Runtime could not be loaded");
    expect(screen.getByText("Runtime unavailable")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reload catalog" })).toBeInTheDocument();
    expect(screen.queryByText("Local · offline")).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("does not automatically restore a landing run under an unknown runtime", async () => {
    window.history.replaceState({}, "", "/");
    const api = client({ ...serviceRuntime, runtime: "unknown" });
    render(<App api={api} />);
    await screen.findByRole("heading", { name: "Benchmark catalog unavailable" });
    expect(api.getRun).not.toHaveBeenCalled();
    expect(screen.queryByRole("heading", { name: "Evaluation detail" })).not.toBeInTheDocument();
  });

  it("retains local developer catalog and explicit network opt-in", async () => {
    const user = userEvent.setup();
    const api = client({ ...serviceRuntime, runtime: "local", public_demo: false, landing: null });
    render(<App api={api} />);
    expect(await screen.findByRole("heading", { name: "Select services" })).toBeInTheDocument();
    expect(screen.getAllByRole("option", { name: "Synthetic text intent" })).toHaveLength(2);
    expect(screen.getByRole("checkbox", { name: /Allow networked execution/ })).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Launch benchmark" })).toBeDisabled();
    await user.selectOptions(screen.getByLabelText("Current benchmark"), serviceBenchmark.id);
    await user.click(screen.getByRole("checkbox", { name: /Allow networked execution/ }));
    expect(screen.getByRole("button", { name: "Launch benchmark" })).toBeEnabled();
    expect(api.launchRun).not.toHaveBeenCalled();
  });

  it("retains a non-public Azure catalog without landing and keeps hosted launch consent", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/?view=configure&benchmark=not-installed");
    const api = client({ ...serviceRuntime, public_demo: false, landing: null });
    render(<App api={api} />);
    await screen.findByRole("heading", { name: "Select services" });
    expect(screen.getByLabelText("Current benchmark")).toHaveValue(developerBenchmark.id);
    expect(screen.getAllByRole("option", { name: "Synthetic text intent" })).toHaveLength(2);
    expect(screen.getAllByRole("option", { name: "Service examples" })).toHaveLength(2);
    await user.selectOptions(screen.getByLabelText("Current benchmark"), serviceBenchmark.id);
    await waitFor(() => expect(screen.getByRole("button", { name: "Launch benchmark" })).toBeEnabled());
    expect(screen.queryByRole("checkbox", { name: /Allow networked execution/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Launch benchmark" }));
    expect(api.launchRun).toHaveBeenCalledWith(expect.objectContaining({
      benchmark_id: serviceBenchmark.id,
      solution_ids: Object.keys(serviceNames).sort(),
      allow_networked: true,
    }), expect.any(String), undefined);
  });

  it.each(["azure", "local"])("retains uninstalled saved-run history in non-public %s", async runtime => {
    window.history.replaceState({}, "", "/?view=runs&benchmark=retired_pack");
    const api = client({ ...serviceRuntime, runtime, public_demo: false, landing: null });
    api.listRuns = vi.fn().mockResolvedValue([{ ...runFixture, benchmark_id: "retired_pack" }]);
    render(<App api={api} />);
    await screen.findByRole("heading", { name: "Runs" });
    expect(screen.getAllByRole("button", { name: `Open run ${runId}` }).length).toBeGreaterThan(0);
    expect(screen.queryByRole("heading", { name: "Benchmark catalog unavailable" })).not.toBeInTheDocument();
    expect(api.launchRun).not.toHaveBeenCalled();
  });

  it("keeps all five service identities and only launches the checked services", async () => {
    const user = userEvent.setup();
    const api = client();
    render(<App api={api} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Launch benchmark" })).toBeEnabled());
    for (const name of Object.values(serviceNames)) expect(screen.getByRole("checkbox", { name })).toBeChecked();
    await user.click(screen.getByRole("checkbox", { name: "MAI-Transcribe-1.5" }));
    await user.click(screen.getByRole("checkbox", { name: "Managed Whisper" }));
    await user.click(screen.getByRole("button", { name: "Launch benchmark" }));
    expect(api.launchRun).toHaveBeenCalledWith(expect.objectContaining({
      benchmark_id: serviceBenchmark.id,
      solution_ids: ["azure-speech-fast", "gpt-transcribe", "mai-transcribe-2"],
      dataset_id: serviceDataset.id,
      allow_networked: true,
    }), expect.any(String), undefined);
  });

  it("does not offer hidden benchmark services from an explicit setup bookmark", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", `/?view=configure&benchmark=${developerBenchmark.id}`);
    const api = client();
    render(<App api={api} />);
    expect(await screen.findByRole("heading", { name: "Benchmark catalog unavailable" })).toBeInTheDocument();
    expect(screen.queryByText("Synthetic text intent")).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Choose an available benchmark" }));
    expect(await screen.findByRole("heading", { name: "Benchmarks" })).toBeInTheDocument();
    expect(screen.queryByText("Synthetic text intent")).not.toBeInTheDocument();
    expect(api.launchRun).not.toHaveBeenCalled();
  });

  it("preserves direct saved-run evidence outside hosted scope, then configures only the scoped catalog", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", `/?view=evaluation&run=${runId}`);
    const api = client();
    render(<App api={api} />);
    expect(await screen.findByRole("heading", { name: "Evaluation detail" })).toBeInTheDocument();
    expect(screen.getByLabelText("Current location")).toHaveTextContent(runFixture.pinned_benchmark!.name);
    expect(screen.queryByRole("option", { name: "Synthetic text intent" })).not.toBeInTheDocument();
    await user.click(within(screen.getByRole("navigation", { name: "Primary navigation" })).getByRole("button", { name: "Configure run" }));
    await waitFor(() => expect(screen.getAllByRole("checkbox")).toHaveLength(5));
    expect(screen.getByLabelText("Current location")).toHaveTextContent("Service examples");
    expect(screen.queryByRole("checkbox", { name: "pipeline-a" })).not.toBeInTheDocument();
    expect(window.location.search).not.toContain(`run=${runId}`);
    expect(api.launchRun).not.toHaveBeenCalled();
  });

  it("keeps a saved run readable even if hosted catalog setup is missing", async () => {
    window.history.replaceState({}, "", `/?view=evaluation&run=${runId}`);
    render(<App api={client({ ...serviceRuntime, landing: null })} />);
    expect(await screen.findByRole("heading", { name: "Evaluation detail" })).toBeInTheDocument();
    expect(screen.getByLabelText("Current location")).toHaveTextContent(runFixture.pinned_benchmark!.name);
    expect(screen.queryByRole("option", { name: "Synthetic text intent" })).not.toBeInTheDocument();
  });

  it("scopes an explicitly public local runtime without changing local launch consent", async () => {
    const api = client({ ...serviceRuntime, runtime: "local", public_demo: true });
    render(<App api={api} />);
    expect(await screen.findByRole("checkbox", { name: /Allow networked execution/ })).not.toBeChecked();
    expect(screen.queryByRole("option", { name: "Synthetic text intent" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Launch benchmark" })).toBeDisabled();
  });

  it("does not expose hidden services through browser history", async () => {
    render(<App api={client()} />);
    await screen.findByRole("checkbox", { name: "Azure Speech Fast" });
    act(() => {
      window.history.pushState({}, "", `/?view=solutions&benchmark=${developerBenchmark.id}`);
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(await screen.findByRole("heading", { name: "Benchmark catalog unavailable" })).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Services" })).not.toBeInTheDocument();
  });

  it("overlays only catalog names for saved evidence without mutating pinned contracts", () => {
    const pinned = structuredClone(runFixture.pinned_benchmark!);
    const presentation = withCatalogNames(runFixture.pinned_benchmark!, [{
      ...developerBenchmark,
      version: "not the pinned version",
      pipelines: developerBenchmark.pipelines!.map(item => ({ ...item, mode: "assisted", name: `Readable ${item.id}` })),
    }]);
    expect(presentation.pipelines?.[0].name).toBe("Readable pipeline-a");
    expect(presentation.version).toBe(pinned.version);
    expect(presentation.pipelines?.[0].mode).toBe(pinned.pipelines?.[0].mode);
    expect(runFixture.pinned_benchmark).toEqual(pinned);
    expect(withCatalogNames(pinned, [serviceBenchmark])).toEqual(pinned);
  });

  it("uses catalog names in saved Reports while retaining pinned identities and downloads", async () => {
    window.history.replaceState({}, "", `/?view=reports&run=${runId}`);
    const api = client({ ...serviceRuntime, runtime: "local", public_demo: false, landing: null });
    api.listBenchmarks = vi.fn().mockResolvedValue([{
      ...developerBenchmark,
      pipelines: developerBenchmark.pipelines!.map(item => ({ ...item, name: `Readable ${item.id}` })),
    }]);
    const savedRun = { ...runFixture, status: "completed" as const, costs: costFixture };
    const original = structuredClone(savedRun);
    api.getRun = vi.fn().mockResolvedValue(savedRun);
    render(<App api={api} />);
    const services = await screen.findByRole("list", { name: "Per-service costs" });
    await waitFor(() => expect(within(services).getByRole("heading", { name: "Readable pipeline-a" })).toBeInTheDocument());
    expect(within(services).getByRole("heading", { name: "Readable pipeline-b" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Download JSON" })).toHaveAttribute("href", `/api/runs/${runId}/report?format=json`);
    expect(screen.getByRole("link", { name: "Download CSV" })).toHaveAttribute("href", `/api/runs/${runId}/report?format=csv`);
    expect(savedRun).toEqual(original);
    expect(api.launchRun).not.toHaveBeenCalled();
  });
});
