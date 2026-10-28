import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { ApiError, type ApiClient } from "./api/client";
import { mapBenchmark, mapFinalizedEvaluation } from "./api/wire";
import { benchmarkWireFixture, reportFixture, runFixture, runId } from "./testing/fixtures";
import { finalizedWireFixture } from "./testing/finalizedFixtures";
import type { EvaluationRead, Run } from "./types";

const finalized = mapFinalizedEvaluation(finalizedWireFixture);
const fallback: EvaluationRead = {
  record_source: "live_detail_required", run: runFixture,
  reason: "run_not_finalized", detail: "Run is active; load live detail.",
};
function client(): ApiClient {
  return {
    listBenchmarks: vi.fn().mockResolvedValue([mapBenchmark(benchmarkWireFixture)]),
    listRuns: vi.fn().mockResolvedValue([finalized.run]),
    getRuntime: vi.fn().mockResolvedValue({ runtime: "local", public_demo: false }),
    getFinalizedEvaluation: vi.fn().mockResolvedValue(finalized),
    getRun: vi.fn().mockResolvedValue(runFixture),
    getRunEvents: vi.fn().mockResolvedValue({ events: [], next_after: 0 }),
    getRunReport: vi.fn().mockResolvedValue(reportFixture),
    launchRun: vi.fn(),
    artifactUrl: id => `/api/artifacts/${id}`,
  };
}
beforeEach(() => window.history.replaceState({}, "", `/?view=evaluation&run=${runId}`));
afterEach(() => vi.useRealTimers());

describe("finalized-first evaluation", () => {
  it("loads historical rows and costs without detail, events or a duplicate report request", async () => {
    const api = client();
    render(<App api={api} />);
    await screen.findByText(/Historical record from the finalized report/);
    expect(within(screen.getByRole("region", { name: "All results, horizontally scrollable" })).getAllByRole("row")).toHaveLength(3);
    expect(screen.getByText(/2 of 2 recorded outcomes.*4 recorded attempts/)).toBeInTheDocument();
    expect(api.getFinalizedEvaluation).toHaveBeenCalledTimes(1);
    expect(api.getRun).not.toHaveBeenCalled();
    expect(api.getRunEvents).not.toHaveBeenCalled();
    expect(api.getRunReport).not.toHaveBeenCalled();
  });

  it("loads diagnostics only on explicit request and keeps finalized data through failure/retry", async () => {
    const user = userEvent.setup(), api = client();
    vi.mocked(api.getRun).mockRejectedValueOnce(new ApiError("Temporary storage pressure", 503));
    render(<App api={api} />);
    await screen.findByText(/Historical record/);
    await user.click(screen.getByText("Operational details", { selector: "summary" }));
    expect(api.getRun).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Load current diagnostics" }));
    await screen.findByRole("alert");
    expect(screen.getByText(/Historical record/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry current diagnostics" }));
    await screen.findByText("Current operational snapshot, separate from historical results.");
    expect(api.getRun).toHaveBeenCalledTimes(2);
    expect(screen.getByText(/2 of 2 recorded outcomes.*4 recorded attempts/)).toBeInTheDocument();
    expect(api.getFinalizedEvaluation).toHaveBeenCalledTimes(1);
    expect(api.getRunReport).not.toHaveBeenCalled();
  });

  it("does not erase a current-access historical view on transient diagnostic failure", async () => {
    const user = userEvent.setup(), api = client();
    vi.mocked(api.getRun).mockRejectedValue(new ApiError("Diagnostic unavailable", 503));
    render(<App api={api} />);
    await screen.findByText(/Historical record/);
    await user.click(screen.getByText("Operational details", { selector: "summary" }));
    await user.click(screen.getByRole("button", { name: "Load current diagnostics" }));
    await screen.findByRole("alert");
    expect(screen.getByRole("heading", { name: "All results" })).toBeInTheDocument();
  });

  it.each([404, 410])("purges historical data when requested diagnostics confirm missing or expired evidence (%s)", async status => {
    const user = userEvent.setup(), api = client();
    vi.mocked(api.getRun).mockRejectedValue(new ApiError("Run unavailable", status));
    render(<App api={api} />);
    await screen.findByText(/Historical record/);
    await user.click(screen.getByText("Operational details", { selector: "summary" }));
    await user.click(screen.getByRole("button", { name: "Load current diagnostics" }));
    await screen.findByRole("heading", { name: status === 410 ? "Run expired" : "Run unavailable" });
    expect(screen.queryByText(/Historical record/)).not.toBeInTheDocument();
  });

  it.each([404, 410])("cancels closed diagnostics and ignores late HTTP %s even when transport ignores abort", async status => {
    const user = userEvent.setup(), api = client();
    let reject!: (reason: Error) => void;
    let signal: AbortSignal | undefined;
    vi.mocked(api.getRun).mockImplementationOnce((_id, nextSignal) => {
      signal = nextSignal;
      return new Promise<Run>((_resolve, fail) => { reject = fail; });
    });
    render(<App api={api} />);
    await screen.findByText(/Historical record/);
    const disclosure = screen.getByText("Operational details", { selector: "summary" });
    await user.click(disclosure);
    await user.click(screen.getByRole("button", { name: "Load current diagnostics" }));
    await screen.findByText("Loading current diagnostics...");
    await user.click(disclosure);
    expect(signal?.aborted).toBe(true);
    await act(async () => reject(new ApiError("Late unavailable response", status)));
    expect(screen.getByText(/Historical record/)).toBeInTheDocument();
  });

  it("ignores an old finalized response after navigation to another run", async () => {
    const api = client();
    let resolve!: (value: EvaluationRead) => void;
    vi.mocked(api.getFinalizedEvaluation).mockImplementationOnce(() => new Promise(done => { resolve = done; }));
    const newer = { ...finalized, run: { ...finalized.run, id: "9".repeat(64) }, report: { ...finalized.report, run_id: "9".repeat(64) } };
    vi.mocked(api.getFinalizedEvaluation).mockResolvedValue(newer);
    render(<App api={api} />);
    await waitFor(() => expect(api.getFinalizedEvaluation).toHaveBeenCalledTimes(1));
    await act(async () => {
      window.history.pushState({}, "", `/?view=evaluation&run=${newer.run.id}`);
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    await screen.findByText(/Historical record/);
    await act(async () => resolve(finalized));
    expect(window.location.search).toContain(newer.run.id);
    expect(document.querySelector(".evaluation-toolbar code")).toHaveAttribute("title", newer.run.id);
  });

  it("transitions an active live view to finalized records without fetching the report again", async () => {
    const api = client();
    vi.mocked(api.getFinalizedEvaluation).mockResolvedValueOnce(fallback).mockResolvedValue(finalized);
    vi.mocked(api.getRun).mockResolvedValue({ ...runFixture, status: "completed" });
    render(<App api={api} />);
    await screen.findByText(/Historical record/);
    expect(api.getRun).toHaveBeenCalledTimes(1);
    expect(api.getFinalizedEvaluation).toHaveBeenCalledTimes(2);
    expect(api.getRunReport).not.toHaveBeenCalled();
  });

  it("keeps incomplete legacy reports on the explicit live-detail path", async () => {
    const api = client();
    vi.mocked(api.getFinalizedEvaluation).mockResolvedValue({ ...fallback, reason: "incomplete_attempt_history", detail: "Historical attempt coverage is incomplete; loading live detail." });
    vi.mocked(api.getRun).mockResolvedValue({ ...runFixture, status: "completed" });
    render(<App api={api} />);
    await screen.findByText("Historical attempt coverage is incomplete; loading live detail.");
    await waitFor(() => expect(api.getRunReport).toHaveBeenCalledTimes(1));
    expect(screen.queryByText(/Historical record from/)).not.toBeInTheDocument();
  });

  it.each([409, 503])("does not silently fall back after finalized HTTP %s", async status => {
    const api = client();
    vi.mocked(api.getFinalizedEvaluation).mockRejectedValue(new ApiError("Finalized read failed", status));
    render(<App api={api} />);
    await screen.findByText(/Finalized evaluation unavailable: Finalized read failed/);
    expect(api.getRun).not.toHaveBeenCalled();
    expect(api.getRunEvents).not.toHaveBeenCalled();
  });
});
