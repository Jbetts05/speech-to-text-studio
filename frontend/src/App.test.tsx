import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { HttpApiClient } from "./api/client";
import { App } from "./App";
import {
  benchmarkWireFixture,
  eventsFixture,
  eventsWireFixture,
  parsedArtifactId,
  comparisonArtifactId,
  referenceArtifactId,
  reportArtifactId,
  reportWireFixture,
  runDetailWireFixture,
  runId,
  runSummaryWireFixture,
} from "./testing/fixtures";

function fixtureClient(
  reportStatus = 200,
  includeRunSummary = true,
  terminalDetail = true,
  reportFixture = reportWireFixture,
  networkedRun = false,
): HttpApiClient {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      const path = url.pathname;
      let status = 200;
      let body: unknown;
      if (path === "/api/benchmarks") body = [benchmarkWireFixture];
      else if (path === "/api/runtime") {
        body = {
          runtime: "local",
          execution_backend: "single-instance-sqlite-filesystem",
          horizontal_scaling: false,
          persistent_state_required: false,
          azure_storage_required: false,
          azure_storage_role: "readiness-and-explicit-repository-api",
        };
      } else if (path === "/api/runs") {
        body = includeRunSummary ? [runSummaryWireFixture] : [];
      } else if (path === `/api/runs/${runId}`) {
        body = terminalDetail
          ? {
              ...runDetailWireFixture,
              status: "completed" as const,
              manifest: {
                ...runDetailWireFixture.manifest,
                networked_execution: networkedRun,
              },
            }
          : {
              ...runDetailWireFixture,
              manifest: {
                ...runDetailWireFixture.manifest,
                networked_execution: networkedRun,
              },
            };
      } else if (path === `/api/runs/${runId}/events`) {
        body = eventsWireFixture;
      } else if (path === `/api/runs/${runId}/finalized-evaluation`) {
        body = { record_source: "live_detail_required", run: runSummaryWireFixture, reason: "incomplete_results", detail: "Synthetic fixture has partial historical records; loading live detail." };
      } else if (path === `/api/runs/${runId}/report`) {
        status = reportStatus;
        body =
          status === 200
            ? reportFixture
            : { detail: "run report is not finalized" };
      } else if (path.startsWith("/api/artifacts/")) {
        if (path.endsWith(referenceArtifactId)) body = { intent: "find_note" };
        else if (path.endsWith(parsedArtifactId)) body = { intent: "find_note" };
        else if (path.endsWith(comparisonArtifactId)) body = { alignment_evidence: "recorded" };
        else body = { provider_payload: "recorded" };
      } else {
        status = 404;
        body = { detail: "not found" };
      }
      return new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
  return new HttpApiClient();
}

beforeEach(() => {
  window.history.replaceState({}, "", "/");
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("AI Bench workspace", () => {
  it("renders the platform workspace shell without axe violations", async () => {
    render(<App api={fixtureClient()} />);
    expect(await screen.findByRole("heading", { name: "AI Bench" })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Primary navigation" })).toBeInTheDocument();
    expect(screen.getByRole("complementary", { name: "Benchmark context" })).toBeInTheDocument();

    const results = await axe.run(document.body, {
      rules: { "color-contrast": { enabled: false } },
    });
    expect(results.violations).toEqual([]);
  });

  it("restores an evaluation URL and formats ratios as percentages", async () => {
    window.history.replaceState(
      {},
      "",
      `/?view=evaluation&benchmark=bench_general&run=${runId}`,
    );
    render(<App api={fixtureClient()} />);

    expect(await screen.findByRole("heading", { name: "Evaluation detail" })).toBeInTheDocument();
    const performance = screen.getByRole("region", {
      name: "Solution performance, horizontally scrollable",
    });
    expect(within(performance).getAllByText("100.0%").length).toBeGreaterThan(0);
    expect(within(performance).queryByText("1 ratio")).not.toBeInTheDocument();
    expect(window.location.search).toContain("view=evaluation");
  });

  it("labels a local provider-backed run as networked", async () => {
    window.history.replaceState({}, "", `/?view=evaluation&run=${runId}`);
    render(<App api={fixtureClient(200, true, true, reportWireFixture, true)} />);

    expect(await screen.findByText("Local · networked")).toBeInTheDocument();
    expect(screen.queryByText("Local · offline")).not.toBeInTheDocument();
  });

  it("announces run completion and task progress", async () => {
    window.history.replaceState({}, "", `/?view=evaluation&run=${runId}`);
    render(<App api={fixtureClient()} />);

    const announcement = await screen.findByText(
      "Run completed. 1 of 2 tasks terminal.",
    );
    expect(announcement).toHaveAttribute("role", "status");
    expect(announcement).toHaveAttribute("aria-live", "polite");
  });

  it("provides roving keyboard navigation for metric tabs", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", `/?view=evaluation&run=${runId}`);
    render(<App api={fixtureClient()} />);
    await user.click(await screen.findByText("Operational details", { selector: "summary" }));
    const quality = await screen.findByRole("tab", { name: "Quality" });
    quality.focus();
    await user.keyboard("{ArrowRight}");

    const cost = screen.getByRole("tab", { name: "Cost" });
    expect(cost).toHaveFocus();
    expect(cost).toHaveAttribute("aria-selected", "true");
    expect(quality).toHaveAttribute("tabindex", "-1");
  });

  it("defaults operational evidence to result-level comparisons", async () => {
    window.history.replaceState({}, "", `/?view=evaluation&run=${runId}`);
    render(<App api={fixtureClient()} />);
    await userEvent.click(await screen.findByText("Operational details", { selector: "summary" }));
    const ledger = await screen.findByRole("region", {
      name: "quality evidence ledger, horizontally scrollable",
    });
    expect(within(ledger).getByRole("button", { name: /comparison · sample-001/i })).toBeInTheDocument();
    expect(within(ledger).queryByRole("button", { name: /provider response/i })).not.toBeInTheDocument();
  });

  it("restores focus after closing the evidence drawer", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", `/?view=evaluation&run=${runId}`);
    render(<App api={fixtureClient()} />);
    await user.click(await screen.findByText("Operational details", { selector: "summary" }));
    const evidence = await screen.findByRole("button", {
      name: /comparison · sample-001/i,
    });
    await user.click(evidence);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(evidence).toHaveFocus());
  });

  it("surfaces report failures without removing snapshot evidence", async () => {
    window.history.replaceState({}, "", `/?view=evaluation&run=${runId}`);
    render(<App api={fixtureClient(409)} />);

    expect(await screen.findByText(/final report unavailable/i)).toBeInTheDocument();
    await userEvent.click(screen.getByText("Operational details", { selector: "summary" }));
    expect(
      screen.getByRole("button", { name: /comparison · sample-001/i }),
    ).toBeInTheDocument();
  });

  it("hydrates a bookmarked terminal run omitted from recent history", async () => {
    window.history.replaceState({}, "", `/?view=evaluation&run=${runId}`);
    render(<App api={fixtureClient(200, false, true)} />);

    expect(await screen.findByText("Solution performance")).toBeInTheDocument();
    expect(screen.getAllByText("Macro F1").length).toBeGreaterThan(0);
  });

  it("keeps a bookmarked run associated with its manifest benchmark", async () => {
    let releaseCatalog!: () => void;
    const catalogGate = new Promise<void>((resolve) => {
      releaseCatalog = resolve;
    });
    const client = fixtureClient(200, false, true);
    const listBenchmarks = client.listBenchmarks.bind(client);
    const getRun = client.getRun.bind(client);
    let runCalls = 0;
    client.listBenchmarks = async (signal) => {
      await catalogGate;
      const [first] = await listBenchmarks(signal);
      return [
        first,
        {
          ...first,
          id: "bench_manifest",
          name: "Manifest benchmark",
        },
      ];
    };
    client.getRun = async (id, signal) => {
      runCalls += 1;
      return {
        ...(await getRun(id, signal)),
        benchmark_id: "bench_manifest",
      };
    };
    window.history.replaceState({}, "", `/?view=evaluation&run=${runId}`);
    render(<App api={client} />);

    await waitFor(() => expect(runCalls).toBeGreaterThan(0));
    releaseCatalog();

    expect(
      (await screen.findAllByText("Manifest benchmark")).length,
    ).toBeGreaterThan(0);
    await waitFor(() =>
      expect(window.location.search).toContain("benchmark=bench_manifest"),
    );
  });

  it("renders a historical run when its benchmark pack is not installed", async () => {
    const user = userEvent.setup();
    const client = fixtureClient();
    const listBenchmarks = client.listBenchmarks.bind(client);
    client.listBenchmarks = async (signal) => {
      const [benchmark] = await listBenchmarks(signal);
      return [
        {
          ...benchmark,
          id: "unrelated_pack",
          name: "Unrelated benchmark",
          viewer: benchmark.viewer
            ? { ...benchmark.viewer, id: "unrelated-viewer", version: "9" }
            : undefined,
        },
      ];
    };
    window.history.replaceState({}, "", `/?view=evaluation&run=${runId}`);
    render(<App api={client} />);

    expect(
      await screen.findByRole("heading", { name: "Evaluation detail" }),
    ).toBeInTheDocument();
    const location = screen.getByLabelText("Current location");
    expect(within(location).getByText("General capability set")).toBeInTheDocument();
    expect(screen.getByLabelText("Current benchmark")).toHaveValue("");

    await user.click(screen.getByText("Operational details", { selector: "summary" }));
    await user.click(screen.getByRole("tab", { name: "Run manifest" }));
    expect(screen.getByText("structured-viewer@1")).toBeInTheDocument();
    expect(screen.queryByText("unrelated-viewer@9")).not.toBeInTheDocument();

    await user.click(
      within(
        screen.getByRole("navigation", { name: "Primary navigation" }),
      ).getByRole("button", { name: "Runs" }),
    );
    const history = await screen.findByRole("region", { name: "Run history" });
    expect(within(history).getByTitle(runId)).toBeInTheDocument();
  });

  it("lists removed-pack history without requiring a run ID", async () => {
    const client = fixtureClient();
    const listBenchmarks = client.listBenchmarks.bind(client);
    client.listBenchmarks = async (signal) => {
      const [benchmark] = await listBenchmarks(signal);
      return [
        {
          ...benchmark,
          id: "unrelated_pack",
          name: "Unrelated benchmark",
        },
      ];
    };
    window.history.replaceState(
      {},
      "",
      "/?view=runs&benchmark=bench_general",
    );
    render(<App api={client} />);

    expect(
      await screen.findByRole("heading", { name: "Runs" }),
    ).toBeInTheDocument();
    const history = screen.getByRole("region", { name: "Run history" });
    expect(within(history).getByTitle(runId)).toBeInTheDocument();
    expect(screen.getByLabelText("Current benchmark")).toHaveValue("");
    expect(
      within(screen.getByLabelText("Current location")).getByText("bench_general"),
    ).toBeInTheDocument();
    expect(window.location.search).toContain("benchmark=bench_general");
  });

  it("renders a historical run when no benchmark packs are installed", async () => {
    const user = userEvent.setup();
    const client = fixtureClient();
    client.listBenchmarks = async () => [];
    window.history.replaceState({}, "", `/?view=evaluation&run=${runId}`);
    render(<App api={client} />);

    expect(
      await screen.findByRole("heading", { name: "Evaluation detail" }),
    ).toBeInTheDocument();
    const location = screen.getByLabelText("Current location");
    expect(within(location).getByText("General capability set")).toBeInTheDocument();

    await user.click(screen.getByText("Operational details", { selector: "summary" }));
    await user.click(screen.getByRole("tab", { name: "Run manifest" }));
    expect(screen.getByText("structured-viewer@1")).toBeInTheDocument();
    expect(screen.queryByText("No benchmark packs are installed.")).not.toBeInTheDocument();

    await user.click(
      within(
        screen.getByRole("navigation", { name: "Primary navigation" }),
      ).getByRole("button", { name: "Runs" }),
    );
    const history = await screen.findByRole("region", { name: "Run history" });
    expect(within(history).getByTitle(runId)).toBeInTheDocument();

    await user.click(
      within(
        screen.getByRole("navigation", { name: "Primary navigation" }),
      ).getByRole("button", { name: "Home" }),
    );
    expect(
      await screen.findByRole("heading", {
        name: "No benchmark packs installed",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("navigation", { name: "Primary navigation" }),
    ).toBeInTheDocument();
  });

  it("navigates from home to all retained history when no packs are installed", async () => {
    const user = userEvent.setup();
    const client = fixtureClient();
    const listRuns = client.listRuns.bind(client);
    client.listBenchmarks = async () => [];
    client.listRuns = async (signal) => {
      const [summary] = await listRuns(signal);
      return Array.from({ length: 12 }, (_, index) => ({
        ...summary,
        id: `${runId}-${index}`,
        benchmark_id: index < 8 ? "bench_general" : "bench_archived",
      }));
    };
    render(<App api={client} />);

    expect(
      await screen.findByRole("heading", {
        name: "No benchmark packs installed",
      }),
    ).toBeInTheDocument();

    await user.click(
      within(
        screen.getByRole("navigation", { name: "Primary navigation" }),
      ).getByRole("button", { name: "Runs" }),
    );

    const history = await screen.findByRole("region", { name: "Run history" });
    expect(within(history).getAllByRole("row")).toHaveLength(13);
    expect(within(history).getAllByText("bench_general")).toHaveLength(8);
    expect(within(history).getAllByText("bench_archived")).toHaveLength(4);
    expect(new URLSearchParams(window.location.search).has("benchmark")).toBe(false);
  });

  it("surfaces a benchmark bootstrap failure instead of an empty catalog", async () => {
    const client = fixtureClient();
    client.listBenchmarks = async () => {
      throw new Error("benchmark service unavailable");
    };
    render(<App api={client} />);

    expect(
      await screen.findByRole("heading", { name: "Unable to load AI Bench" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("alert", { name: "" }),
    ).toHaveTextContent("benchmark service unavailable");
    expect(
      screen.queryByRole("heading", { name: "No benchmark packs installed" }),
    ).not.toBeInTheDocument();
  });

  it("preserves navigation that occurs before bootstrap completes", async () => {
    const user = userEvent.setup();
    let releaseCatalog!: () => void;
    const catalogGate = new Promise<void>((resolve) => {
      releaseCatalog = resolve;
    });
    const client = fixtureClient();
    const listBenchmarks = client.listBenchmarks.bind(client);
    client.listBenchmarks = async (signal) => {
      const loaded = await listBenchmarks(signal);
      await catalogGate;
      return loaded;
    };
    window.history.replaceState({}, "", `/?view=evaluation&run=${runId}`);
    render(<App api={client} />);

    expect(
      await screen.findByRole("heading", { name: "Evaluation detail" }),
    ).toBeInTheDocument();
    await user.click(
      within(
        screen.getByRole("navigation", { name: "Primary navigation" }),
      ).getByRole("button", { name: "Reports" }),
    );
    expect(
      await screen.findByRole("heading", { name: "Reports" }),
    ).toBeInTheDocument();
    expect(new URL(window.location.href).searchParams.get("view")).toBe("reports");

    releaseCatalog();

    await waitFor(() =>
      expect(screen.getByRole("heading", { name: "Reports" })).toBeInTheDocument(),
    );
    expect(new URL(window.location.href).searchParams.get("view")).toBe("reports");
  });

  it("rejects a report for a different run", async () => {
    window.history.replaceState({}, "", `/?view=evaluation&run=${runId}`);
    render(
      <App
        api={fixtureClient(200, true, true, {
          ...reportWireFixture,
          run_id: "9".repeat(64),
        })}
      />,
    );

    expect(
      await screen.findByText(/does not match the selected run/i),
    ).toBeInTheDocument();
  });

  it("clears the previous report while a newly selected run is loading", async () => {
    const user = userEvent.setup();
    const client = fixtureClient();
    const baseFetch = vi.mocked(fetch);
    const secondRunId = "8".repeat(64);
    let resolveSecondReport!: (response: Response) => void;
    const secondReport = new Promise<Response>((resolve) => {
      resolveSecondReport = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const path = new URL(String(input), "http://localhost").pathname;
        if (path === "/api/runs") {
          return Promise.resolve(
            new Response(
              JSON.stringify([
                runSummaryWireFixture,
                { ...runSummaryWireFixture, run_id: secondRunId },
              ]),
              { status: 200, headers: { "Content-Type": "application/json" } },
            ),
          );
        }
        if (path === `/api/runs/${secondRunId}`) {
          return Promise.resolve(
            new Response(
              JSON.stringify({
                ...runDetailWireFixture,
                id: secondRunId,
                status: "completed",
                manifest: {
                  ...runDetailWireFixture.manifest,
                  run_id: secondRunId,
                },
                tasks: runDetailWireFixture.tasks.map((task) => ({
                  ...task,
                  run_id: secondRunId,
                })),
              }),
              { status: 200, headers: { "Content-Type": "application/json" } },
            ),
          );
        }
        if (path === `/api/runs/${secondRunId}/events`) {
          return Promise.resolve(
            new Response(JSON.stringify([]), {
              status: 200,
              headers: { "Content-Type": "application/json" },
            }),
          );
        }
        if (path === `/api/runs/${secondRunId}/finalized-evaluation`) {
          return Promise.resolve(new Response(JSON.stringify({
            record_source: "live_detail_required", run: { ...runSummaryWireFixture, run_id: secondRunId },
            reason: "incomplete_results", detail: "Synthetic fixture has partial historical records; loading live detail.",
          }), { status: 200, headers: { "Content-Type": "application/json" } }));
        }
        if (path === `/api/runs/${secondRunId}/report`) {
          return secondReport;
        }
        if (path === `/api/runs/${runId}/report`) {
          return Promise.resolve(
            new Response(
              JSON.stringify({
                ...reportWireFixture,
                quality: {
                  ...reportWireFixture.quality,
                  "pipeline-a": {
                    ...reportWireFixture.quality["pipeline-a"],
                    macro_f1: "0.77",
                  },
                },
              }),
              { status: 200, headers: { "Content-Type": "application/json" } },
            ),
          );
        }
        return baseFetch(input, init);
      }),
    );
    window.history.replaceState({}, "", `/?view=evaluation&run=${runId}`);
    render(<App api={client} />);
    expect(await screen.findByText("77.0%")).toBeInTheDocument();

    await user.click(
      screen.getAllByRole("button", { name: `Open run ${secondRunId}` })[0],
    );
    await waitFor(() =>
      expect(screen.queryByText("77.0%")).not.toBeInTheDocument(),
    );
    expect(window.location.search).toContain(`run=${secondRunId}`);

    resolveSecondReport(
      new Response(
        JSON.stringify({
          ...reportWireFixture,
          run_id: secondRunId,
          quality: {
            ...reportWireFixture.quality,
            "pipeline-a": {
              ...reportWireFixture.quality["pipeline-a"],
              macro_f1: "0.88",
            },
          },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    expect(await screen.findByText("88.0%")).toBeInTheDocument();
  });

  it("keeps aggregate results when the active run is selected again", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", `/?view=evaluation&run=${runId}`);
    render(<App api={fixtureClient()} />);

    const performance = await screen.findByRole("region", {
      name: "Solution performance, horizontally scrollable",
    });
    expect(within(performance).getAllByText("100.0%").length).toBeGreaterThan(0);

    await user.click(
      screen.getAllByRole("button", { name: `Open run ${runId}` })[0],
    );

    await waitFor(() =>
      expect(
        within(performance).getAllByText("100.0%").length,
      ).toBeGreaterThan(0),
    );
  });

  it("does not let a stale running summary downgrade a completed run", async () => {
    let releaseCatalog!: () => void;
    const catalogGate = new Promise<void>((resolve) => {
      releaseCatalog = resolve;
    });
    const client = fixtureClient();
    const listBenchmarks = client.listBenchmarks.bind(client);
    const getRunReport = client.getRunReport.bind(client);
    let reportCalls = 0;
    client.listBenchmarks = async (signal) => {
      const loaded = await listBenchmarks(signal);
      await catalogGate;
      return loaded;
    };
    client.getRunReport = async (id, signal) => {
      reportCalls += 1;
      return getRunReport(id, signal);
    };
    window.history.replaceState(
      {},
      "",
      `/?view=runs&benchmark=bench_general&run=${runId}`,
    );
    render(<App api={client} />);

    await waitFor(() => expect(reportCalls).toBeGreaterThan(0));
    releaseCatalog();

    const history = await screen.findByRole("region", { name: "Run history" });
    expect(await within(history).findByText("completed")).toBeInTheDocument();
    expect(within(history).queryByText("running")).not.toBeInTheDocument();
  });

  it("preserves a selected historical run omitted from refreshed summaries", async () => {
    let releaseCatalog!: () => void;
    const catalogGate = new Promise<void>((resolve) => {
      releaseCatalog = resolve;
    });
    const client = fixtureClient(200, false, true);
    const listBenchmarks = client.listBenchmarks.bind(client);
    client.listBenchmarks = async (signal) => {
      const loaded = await listBenchmarks(signal);
      await catalogGate;
      return loaded;
    };
    window.history.replaceState(
      {},
      "",
      `/?view=runs&benchmark=bench_general&run=${runId}`,
    );
    render(<App api={client} />);

    const history = await screen.findByRole("region", { name: "Run history" });
    expect(await within(history).findByTitle(runId)).toBeInTheDocument();
    await act(async () => {
      releaseCatalog();
      await catalogGate;
      await Promise.resolve();
    });

    await waitFor(() =>
      expect(within(history).getByTitle(runId)).toBeInTheDocument(),
    );
  });

  it("preserves the live selection when navigation changes during refresh", async () => {
    const user = userEvent.setup();
    const recentRunId = "8".repeat(64);
    const refreshMarkerId = "7".repeat(64);
    let releaseRefresh!: () => void;
    const refreshGate = new Promise<void>((resolve) => {
      releaseRefresh = resolve;
    });
    const client = fixtureClient();
    const listRuns = client.listRuns.bind(client);
    const getRun = client.getRun.bind(client);
    const getRunReport = client.getRunReport.bind(client);
    let listCalls = 0;
    let historicalDetailCalls = 0;
    let historicalReportCalls = 0;
    client.listRuns = async (signal) => {
      listCalls += 1;
      const [summary] = await listRuns(signal);
      const recentSummary = { ...summary, id: recentRunId };
      if (listCalls === 1) return [recentSummary];
      await refreshGate;
      return [
        recentSummary,
        { ...recentSummary, id: refreshMarkerId },
      ];
    };
    client.getRun = async (id, signal) => {
      const detail = await getRun(id, signal);
      if (id === runId) historicalDetailCalls += 1;
      return detail;
    };
    client.getRunReport = async (id, signal) => {
      const nextReport = await getRunReport(id, signal);
      if (id === runId) historicalReportCalls += 1;
      return nextReport;
    };
    window.history.replaceState(
      {},
      "",
      `/?view=runs&benchmark=bench_general&run=${runId}`,
    );
    render(<App api={client} />);

    const history = await screen.findByRole("region", { name: "Run history" });
    expect(await within(history).findByTitle(runId)).toBeInTheDocument();
    act(() => {
      window.history.replaceState(
        {},
        "",
        `/?view=runs&benchmark=bench_general&run=${recentRunId}`,
      );
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    const runsHeader = screen
      .getByRole("heading", { name: "Runs" })
      .closest(".view-header");
    expect(runsHeader).not.toBeNull();
    await user.click(
      within(runsHeader as HTMLElement).getByRole("button", { name: "Refresh" }),
    );
    await waitFor(() => expect(listCalls).toBe(2));

    act(() => {
      window.history.replaceState(
        {},
        "",
        `/?view=runs&benchmark=bench_general&run=${runId}`,
      );
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    await waitFor(() => expect(historicalDetailCalls).toBeGreaterThanOrEqual(2));
    await waitFor(() => expect(historicalReportCalls).toBeGreaterThanOrEqual(2));
    await act(async () => {
      releaseRefresh();
      await refreshGate;
    });

    expect(await within(history).findByTitle(refreshMarkerId)).toBeInTheDocument();
    expect(within(history).getByTitle(runId)).toBeInTheDocument();
  });

  it("keeps successful snapshot data and retries a transient event failure", async () => {
    const client = fixtureClient();
    const baseFetch = vi.mocked(fetch);
    let eventCalls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const path = new URL(String(input), "http://localhost").pathname;
        if (path === `/api/runs/${runId}/events`) {
          eventCalls += 1;
          if (eventCalls === 1) {
            return Promise.reject(new TypeError("temporary event failure"));
          }
        }
        return baseFetch(input, init);
      }),
    );
    window.history.replaceState({}, "", `/?view=evaluation&run=${runId}`);
    render(<App api={client} />);

    expect(await screen.findByText("Solution performance")).toBeInTheDocument();
    await waitFor(() => expect(eventCalls).toBeGreaterThanOrEqual(2), {
      timeout: 3_000,
    });
  });

  it("drains an event persisted alongside a terminal snapshot", async () => {
    const client = fixtureClient();
    const getRun = client.getRun.bind(client);
    let releaseInitialDrain!: () => void;
    const initialDrain = new Promise<void>((resolve) => {
      releaseInitialDrain = resolve;
    });
    let terminalObserved = false;
    const afterCalls: number[] = [];
    client.getRun = async (id, signal) => {
      await initialDrain;
      terminalObserved = true;
      return { ...(await getRun(id, signal)), status: "completed" };
    };
    client.getRunEvents = async (_id, after = 0) => {
      afterCalls.push(after);
      if (after === 0) {
        return {
          events: Array.from({ length: 38 }, (_, index) => ({
            ...eventsFixture.events[0],
            id: String(index + 1),
            sequence: index + 1,
          })),
          next_after: 38,
        };
      }
      if (after === 38 && !terminalObserved) {
        releaseInitialDrain();
        return { events: [], next_after: 38 };
      }
      if (after === 38) {
        return {
          events: [
            {
              ...eventsFixture.events[0],
              id: "39",
              sequence: 39,
            },
          ],
          next_after: 39,
        };
      }
      return { events: [], next_after: after };
    };
    window.history.replaceState({}, "", `/?view=evaluation&run=${runId}`);
    render(<App api={client} />);

    expect(await screen.findByText(/39 events/)).toBeInTheDocument();
    expect(afterCalls).toEqual([0, 38, 38, 39]);
  });

  it("paginates the complete run event history", async () => {
    const client = fixtureClient();
    const calls: number[] = [];
    client.getRunEvents = async (_id, after = 0) => {
      calls.push(after);
      const count = after === 0 ? 1_000 : after === 1_000 ? 50 : 0;
      const events = Array.from({ length: count }, (_, index) => {
        const sequence = after + index + 1;
        return {
          ...eventsFixture.events[0],
          id: String(sequence),
          sequence,
        };
      });
      return {
        events,
        next_after: events.at(-1)?.sequence ?? after,
      };
    };
    window.history.replaceState({}, "", `/?view=evaluation&run=${runId}`);
    render(<App api={client} />);

    expect(await screen.findByText(/1,050 events/)).toBeInTheDocument();
    expect(calls).toEqual([0, 1_000, 1_050, 1_050]);
  });

  it("keeps a launch failure visible while background polling succeeds", async () => {
    const user = userEvent.setup();
    const client = fixtureClient(200, true, false);
    const getRuntime = client.getRuntime.bind(client);
    client.getRuntime = async (signal) => ({ ...await getRuntime(signal), allow_networked: true });
    const getRun = client.getRun.bind(client);
    let runCalls = 0;
    client.getRun = async (id, signal) => {
      runCalls += 1;
      return getRun(id, signal);
    };
    client.launchRun = async () => {
      throw new Error("launch rejected");
    };
    window.history.replaceState({}, "", `/?view=configure&run=${runId}`);
    render(<App api={client} />);

    expect(
      await screen.findByRole("heading", { name: "Configure run" }),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("checkbox", { name: /allow networked execution/i }),
    );
    expect(screen.getByRole("button", { name: "Launch benchmark" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Launch benchmark" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("launch rejected");

    const callsAtFailure = runCalls;
    await waitFor(() => expect(runCalls).toBeGreaterThan(callsAtFailure), {
      timeout: 3_000,
    });
    expect(screen.getByRole("alert")).toHaveTextContent("launch rejected");
  });

  it("preserves the event total across view-only popstate navigation", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", `/?view=evaluation&run=${runId}`);
    render(<App api={fixtureClient()} />);

    expect(await screen.findByText(/1 events/)).toBeInTheDocument();
    await user.click(
      within(
        screen.getByRole("navigation", { name: "Primary navigation" }),
      ).getByRole("button", { name: "Reports" }),
    );
    expect(
      await screen.findByRole("heading", { name: "Reports" }),
    ).toBeInTheDocument();

    act(() => {
      window.history.replaceState(
        {},
        "",
        `/?view=evaluation&benchmark=bench_general&run=${runId}`,
      );
      window.dispatchEvent(new PopStateEvent("popstate"));
    });

    expect(
      await screen.findByRole("heading", { name: "Evaluation detail" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/1 events/)).toBeInTheDocument();
  });

  it("retries a transient terminal report failure", async () => {
    const client = fixtureClient();
    const baseFetch = vi.mocked(fetch);
    let reportCalls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const path = new URL(String(input), "http://localhost").pathname;
        if (path === `/api/runs/${runId}/report`) {
          reportCalls += 1;
          if (reportCalls === 1) {
            return Promise.resolve(
              new Response(
                JSON.stringify({ detail: "run report is not finalized" }),
                {
                  status: 503,
                  headers: { "Content-Type": "application/json" },
                },
              ),
            );
          }
        }
        return baseFetch(input, init);
      }),
    );
    window.history.replaceState({}, "", `/?view=evaluation&run=${runId}`);
    render(<App api={client} />);

    expect(await screen.findByText(/final report unavailable/i)).toBeInTheDocument();
    await waitFor(
      () => {
        expect(reportCalls).toBeGreaterThanOrEqual(2);
        expect(screen.getByText("Solution performance")).toBeInTheDocument();
        expect(
          screen.queryByText(/final report unavailable/i),
        ).not.toBeInTheDocument();
      },
      { timeout: 3_000 },
    );
  });

  it("uses manifest-pinned metric semantics for historical reports", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", `/?view=evaluation&run=${runId}`);
    const client = fixtureClient();
    const getRunReport = client.getRunReport.bind(client);
    const listBenchmarks = client.listBenchmarks.bind(client);
    client.listBenchmarks = async (signal) =>
      (await listBenchmarks(signal)).map((benchmark) => ({
        ...benchmark,
        pipelines: benchmark.pipelines?.map((pipeline) =>
          pipeline.id === "pipeline-a"
            ? { ...pipeline, mode: "assisted" as const }
            : pipeline,
        ),
        viewer: benchmark.viewer
          ? {
              ...benchmark.viewer,
              id: "current-viewer",
              version: "9",
            }
          : undefined,
      }));
    client.getRunReport = async (id, signal) => {
      const loaded = await getRunReport(id, signal);
      return {
        ...loaded,
        definitions: loaded.definitions.map((definition) =>
          definition.id === "macro_f1"
            ? { ...definition, name: "Current Macro F1" }
            : definition,
        ),
      };
    };
    render(<App api={client} />);

    expect(await screen.findByText("Solution performance")).toBeInTheDocument();
    expect(screen.getAllByText("Macro F1").length).toBeGreaterThan(0);
    expect(screen.queryByText("Current Macro F1")).not.toBeInTheDocument();
    const performance = screen.getByRole("region", {
      name: "Solution performance, horizontally scrollable",
    });
    const pipelineRow = within(performance).getByText("pipeline-a").closest("tr");
    expect(pipelineRow).not.toBeNull();
    expect(within(performance).queryByRole("columnheader", { name: "Pipeline" })).not.toBeInTheDocument();

    await user.click(screen.getByText("Operational details", { selector: "summary" }));
    await user.click(screen.getByRole("tab", { name: "Run manifest" }));
    expect(screen.getByText("structured-viewer@1")).toBeInTheDocument();
    expect(screen.getByText(/pipeline-a: standalone/)).toBeInTheDocument();
    expect(screen.queryByText("current-viewer@9")).not.toBeInTheDocument();
  });

  it("selects the minimum result for lower-is-better quality metrics", async () => {
    const client = fixtureClient(200, true, true, {
      ...reportWireFixture,
      quality: {
        "pipeline-a": { exact_accuracy: "0.8", macro_f1: "0.8" },
        "pipeline-b": { exact_accuracy: "0.2", macro_f1: "0.2" },
      },
    });
    const getRun = client.getRun.bind(client);
    client.getRun = async (id, signal) => {
      const loaded = await getRun(id, signal);
      return {
        ...loaded,
        metric_definitions: loaded.metric_definitions?.map((definition) =>
          definition.id === "exact_accuracy"
            ? { ...definition, direction: "lower" as const }
            : definition,
        ),
      };
    };
    window.history.replaceState({}, "", `/?view=evaluation&run=${runId}`);
    render(<App api={client} />);

    const summary = await screen.findByRole("region", {
      name: "Independent metric summary",
    });
    const qualityCard = within(summary)
      .getByText("Exact accuracy")
      .closest("article");
    expect(qualityCard).not.toBeNull();
    expect(within(qualityCard!).getByText("20.0%")).toBeInTheDocument();
    expect(within(qualityCard!).getByText("pipeline-b")).toBeInTheDocument();
  });

  it("does not claim a best result for neutral quality metrics", async () => {
    const client = fixtureClient();
    const getRun = client.getRun.bind(client);
    client.getRun = async (id, signal) => {
      const loaded = await getRun(id, signal);
      return {
        ...loaded,
        metric_definitions: loaded.metric_definitions?.map((definition) =>
          definition.id === "exact_accuracy"
            ? { ...definition, direction: "neutral" as const }
            : definition,
        ),
      };
    };
    window.history.replaceState({}, "", `/?view=evaluation&run=${runId}`);
    render(<App api={client} />);

    const summary = await screen.findByRole("region", {
      name: "Independent metric summary",
    });
    const qualityCard = within(summary)
      .getByText("Exact accuracy")
      .closest("article");
    expect(qualityCard).not.toBeNull();
    expect(within(qualityCard!).getByText("Not available")).toBeInTheDocument();
    expect(
      within(qualityCard!).getByText("No ranking direction"),
    ).toBeInTheDocument();
  });

  it("refreshes run summary status without discarding hydrated details", async () => {
    const user = userEvent.setup();
    const client = fixtureClient(200, true, false);
    const listRuns = client.listRuns.bind(client);
    let listCalls = 0;
    client.listRuns = async (signal) => {
      listCalls += 1;
      const listed = await listRuns(signal);
      return listed.map((item) => ({
        ...item,
        status: listCalls === 1 ? ("running" as const) : ("completed" as const),
        report_artifact_id:
          listCalls === 1 ? undefined : reportArtifactId,
      }));
    };
    window.history.replaceState(
      {},
      "",
      `/?view=runs&benchmark=bench_general&run=${runId}`,
    );
    render(<App api={client} />);

    const history = await screen.findByRole("region", { name: "Run history" });
    expect(within(history).getByText("running")).toBeInTheDocument();
    await waitFor(() =>
      expect(within(history).getByText("2")).toBeInTheDocument(),
    );
    const page = screen.getByRole("heading", { name: "Runs" }).closest(".view-shell");
    expect(page).not.toBeNull();
    await user.click(
      within(page as HTMLElement).getByRole("button", { name: "Refresh" }),
    );

    await waitFor(() =>
      expect(within(history).getByText("completed")).toBeInTheDocument(),
    );
    expect(within(history).getByText("2")).toBeInTheDocument();
  });

  it("preserves drawer focus while a running snapshot refreshes", async () => {
    const user = userEvent.setup();
    const client = fixtureClient(200, true, false);
    const getRun = client.getRun.bind(client);
    let runCalls = 0;
    client.getRun = async (id, signal) => {
      runCalls += 1;
      return getRun(id, signal);
    };
    window.history.replaceState({}, "", `/?view=evaluation&run=${runId}`);
    render(<App api={client} />);

    await user.click(await screen.findByText("Operational details", { selector: "summary" }));
    const evidence = await screen.findByRole("button", {
      name: /comparison · sample-001/i,
    });
    await user.click(evidence);
    const download = await screen.findByRole("link", {
      name: /download original/i,
    });
    download.focus();
    expect(download).toHaveFocus();
    const callsAtFocus = runCalls;

    await waitFor(() => expect(runCalls).toBeGreaterThan(callsAtFocus), {
      timeout: 3_000,
    });
    expect(download).toHaveFocus();
  });

  it("renders canonical generic comparisons before parsed artifacts", async () => {
    window.history.replaceState({}, "", `/?view=evaluation&run=${runId}`);
    render(<App api={fixtureClient()} />);

    await userEvent.click(await screen.findByRole("button", { name: "Inspect sample-001 / pipeline-a" }));
    const comparison = await screen.findByRole("region", {
      name: "Reference and solution output comparison, horizontally scrollable",
    });
    expect(
      await within(comparison).findAllByText(/"intent": "find_note"/),
    ).toHaveLength(1);
    expect(await within(comparison).findByText(/"alignment_evidence": "recorded"/)).toBeInTheDocument();
    expect(within(comparison).queryByText(/"provider_payload"/)).not.toBeInTheDocument();
    expect(within(comparison).queryByText("expected")).not.toBeInTheDocument();
    expect(within(comparison).queryByText("predicted")).not.toBeInTheDocument();
  });

  it("does not combine costs with different currencies", async () => {
    window.history.replaceState({}, "", `/?view=evaluation&run=${runId}`);
    render(
      <App
        api={fixtureClient(200, true, true, {
          ...reportWireFixture,
          cost: {
            ...reportWireFixture.cost,
            "pipeline-b": "0.02",
          },
          cost_currency: {
            ...reportWireFixture.cost_currency,
            "pipeline-b": "EUR",
          },
        })}
      />,
    );

    expect(
      await screen.findByText("Mixed currencies cannot be combined into one run cost."),
    ).toBeInTheDocument();
    expect(screen.queryByText("$0.00")).not.toBeInTheDocument();
  });

  it("shows the supported scaffold command and retry bounds", async () => {
    const user = userEvent.setup();
    render(<App api={fixtureClient()} />);

    await user.click(
      (await screen.findAllByRole("button", { name: "Create benchmark" }))[0],
    );
    expect(
      await screen.findByText(
        "ai-bench scaffold speech_transcription --brief BENCHMARK.md",
      ),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Configure run" }));
    const retryLimit = await screen.findByRole("spinbutton", {
      name: "Retry limit",
    });
    expect(retryLimit).toHaveAttribute("min", "1");
    expect(retryLimit).toHaveAttribute("max", "20");
  });

  it("uses scored samples for the run task estimate", async () => {
    const user = userEvent.setup();
    const client = fixtureClient();
    const listBenchmarks = client.listBenchmarks.bind(client);
    client.listBenchmarks = async (signal) =>
      (await listBenchmarks(signal)).map((benchmark) => ({
        ...benchmark,
        sample_count: 6,
        scored_sample_count: 4,
      }));
    render(<App api={client} />);

    await user.click(
      (await screen.findAllByRole("button", { name: "Configure run" }))[0],
    );
    const contract = screen
      .getByRole("heading", { name: "Run settings" })
      .closest("aside");
    expect(contract).not.toBeNull();
    const tasks = within(contract as HTMLElement).getByText("Tasks").closest("div");
    expect(tasks).not.toBeNull();
    expect(within(tasks as HTMLElement).getByText("8")).toBeInTheDocument();
  });

  it("keeps report downloads unavailable until a report is persisted", async () => {
    const client = fixtureClient(409, true, false);
    const getRun = client.getRun.bind(client);
    client.getRun = async (id, signal) => ({
      ...(await getRun(id, signal)),
      status: "running",
      report_artifact_id: undefined,
    });
    window.history.replaceState({}, "", `/?view=reports&run=${runId}`);
    render(<App api={client} />);

    expect(await screen.findByText("Report pending")).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: "Download JSON" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: "Download CSV" }),
    ).not.toBeInTheDocument();
  });

  it("allows every solution configuration to remain unselected", async () => {
    const user = userEvent.setup();
    render(<App api={fixtureClient()} />);

    await user.click(
      (await screen.findAllByRole("button", { name: "Configure run" }))[0],
    );
    const heading = await screen.findByRole("heading", {
      name: "Select services",
    });
    const selection = heading.closest("section");
    expect(selection).not.toBeNull();
    const solutions = within(selection as HTMLElement).getAllByRole("checkbox");
    expect(solutions).toHaveLength(2);
    expect(solutions.every((checkbox) => (checkbox as HTMLInputElement).checked)).toBe(true);

    await user.click(solutions[0]);
    await user.click(solutions[1]);

    expect(solutions.every((checkbox) => !(checkbox as HTMLInputElement).checked)).toBe(true);
    expect(within(selection as HTMLElement).getByText("0 selected")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Launch benchmark" })).toBeDisabled();
  });
});
