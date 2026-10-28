import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mapBenchmark, mapRunDetail } from "../api/wire";
import { speechBenchmarkFixture, speechRunFixture } from "../testing/speechFixtures";
import { costFixture } from "../testing/costFixtures";
import type { MetricDefinition, Run } from "../types";
import { EvaluationWorkspace } from "./EvaluationWorkspace";
import { compareResultValues, resultRows, RunResults } from "./RunResults";

afterEach(() => vi.unstubAllGlobals());
const benchmark = mapBenchmark(speechBenchmarkFixture);
const run = mapRunDetail(speechRunFixture);
const focusedRun: Run = { ...run, tasks: run.tasks!.slice(0, 2), sample_artifacts: { "synthetic-1": run.sample_artifacts!["synthetic-1"] }, solution_ids: run.solution_ids.slice(0, 2) };
const props = { benchmark, run, metrics: run.metrics ?? [], evidence: run.evidence ?? [], definitions: benchmark.metrics, onSelect: vi.fn() };

describe("whole-run results", () => {
  it("renders all 130 sample/service pairs without reading any artifact", async () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    render(<EvaluationWorkspace benchmark={benchmark} run={run} eventCount={0} artifactUrl={vi.fn()} onConfigure={vi.fn()} onInspect={vi.fn()} />);
    const table = screen.getByRole("region", { name: "All results, horizontally scrollable" });
    expect(within(table).getAllByRole("row")).toHaveLength(131);
    expect(screen.getByText("130 of 130 results shown across all services. Summaries above cover the whole run.")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Comparison inspector" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Cost" })).not.toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("filters services without changing whole-run scope and retains missing/failed/pending rows", async () => {
    const user = userEvent.setup();
    const changed = { ...run, tasks: run.tasks!.slice(1).map((task, index) => index === 1 ? { ...task, status: "poisoned" as const, message: "Synthetic failure" } : task) };
    render(<RunResults {...props} run={changed} />);
    expect(screen.getByText("Missing")).toBeInTheDocument();
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(screen.getAllByText("Pending").length).toBeGreaterThan(0);
    await user.selectOptions(screen.getByRole("combobox", { name: "Service" }), run.solution_ids[0]);
    expect(screen.getAllByRole("row")).toHaveLength(27);
    expect(screen.getByText(/26 of 130 results shown/)).toHaveTextContent("Summaries above cover the whole run");
    await user.selectOptions(screen.getByRole("combobox", { name: "Service" }), "");
    expect(screen.getAllByRole("row")).toHaveLength(131);
  });

  it("sorts underlying decimals rather than displayed percentages and keeps unknown last", async () => {
    const user = userEvent.setup();
    const smallRun: Run = { ...run, solution_ids: ["one"], sample_artifacts: {}, tasks: ["a", "b", "c"].map(sample => ({ id: sample, task_id: sample, sample_id: sample, solution_id: "one", status: "succeeded" })) };
    const metrics = ["0.12", "0.9", null].map((value, index) => ({ metric_id: "word_error_rate", family: "quality" as const, unit: "ratio", value, task_id: ["a", "b", "c"][index] }));
    render(<RunResults {...props} run={smallRun} metrics={metrics} evidence={[]} />);
    await user.click(screen.getByRole("button", { name: "Word error rate" }));
    expect(screen.getAllByRole("row").slice(1).map(row => within(row).getByRole("button").querySelector("span")?.textContent)).toEqual(["a", "b", "c"]);
    await user.click(screen.getByRole("button", { name: "Word error rate" }));
    expect(screen.getAllByRole("row").slice(1).map(row => within(row).getByRole("button").querySelector("span")?.textContent)).toEqual(["b", "a", "c"]);
    expect(compareResultValues(undefined, 0, true)).toBe(1);
    expect(compareResultValues(null, undefined, false)).toBe(0);
    const help = screen.getByText("?", { selector: "summary" });
    help.focus();
    expect(help).toHaveFocus();
    await user.click(help);
    expect(help.parentElement).toHaveAttribute("open");
    expect(screen.getByText(/Corpus word errors divided/)).toBeVisible();
  });

  it("updates polling rows without losing the selected filter", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<RunResults {...props} metrics={[]} evidence={[]} />);
    await user.selectOptions(screen.getByRole("combobox", { name: "Service" }), run.solution_ids[0]);
    rerender(<RunResults {...props} />);
    expect(screen.getAllByRole("row")).toHaveLength(27);
    expect(screen.getByText("66.7%")).toBeInTheDocument();
    expect(screen.getByRole("combobox")).toHaveValue(run.solution_ids[0]);
  });

  it("shows observed telemetry before a finalized report supplies its definitions", () => {
    const initialRun: Run = { ...focusedRun, evidence: [], metrics: [
      { metric_id: "latency_ms", family: "latency", value: 25, unit: "ms", task_id: focusedRun.tasks![0].task_id },
      { metric_id: "usage.calls", family: "usage", value: 1, unit: "calls", task_id: focusedRun.tasks![0].task_id },
    ] };
    render(<EvaluationWorkspace benchmark={benchmark} run={initialRun} eventCount={0} artifactUrl={vi.fn()} onConfigure={vi.fn()} onInspect={vi.fn()} />);
    const table = screen.getByRole("region", { name: "All results, horizontally scrollable" });
    expect(within(table).getByText("25.0 ms")).toBeInTheDocument();
    expect(within(table).getByText("1 calls")).toBeInTheDocument();
  });

  it("sorts recorded and conditional costs independently and preserves unknown retry coverage", async () => {
    const user = userEvent.setup();
    const sampleIds = ["low-recorded", "high-recorded", "unknown"];
    const costRun: Run = {
      ...run, solution_ids: ["pipeline-a"], sample_artifacts: {},
      tasks: sampleIds.map(id => ({ id, task_id: id, sample_id: id, solution_id: "pipeline-a", status: "succeeded" })),
      costs: { ...costFixture, samples: Object.fromEntries(sampleIds.map((id, index) => [id, [{
        ...costFixture.solutions[0],
        recorded: { ...costFixture.solutions[0].recorded, amounts: { USD: ["0.12", "0.9", null][index] } },
        current_estimate: { ...costFixture.solutions[0].current_estimate, amounts: { USD: ["0.9", "0.12", "0.1"][index] } },
      }]])) },
    };
    render(<RunResults {...props} run={costRun} metrics={[]} evidence={[]} />);
    const order = () => screen.getAllByRole("button", { name: /^Inspect / }).map(button => button.querySelector("span")?.textContent);
    await user.click(screen.getByRole("button", { name: "Cost (USD)" }));
    expect(order()).toEqual(sampleIds);
    await user.click(screen.getByRole("button", { name: "Cost (USD)" }));
    expect(order()).toEqual(["high-recorded", "low-recorded", "unknown"]);
    await user.click(screen.getByRole("button", { name: "Est. cost (USD)" }));
    expect(order()).toEqual(["unknown", "high-recorded", "low-recorded"]);
  });

  it("keeps actionable run failures visible outside collapsed operations", () => {
    render(<EvaluationWorkspace benchmark={benchmark} run={{ ...run, runtime_error: "Synthetic worker failure" }} eventCount={0} artifactUrl={vi.fn()} onConfigure={vi.fn()} onInspect={vi.fn()} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Synthetic worker failure");
    expect(screen.getByRole("alert")).toBeVisible();
  });

  it("uses family and ID together for values and sorting", async () => {
    const user = userEvent.setup();
    const definitions: MetricDefinition[] = [
      { id: "shared", family: "quality", name: "Accuracy", unit: "ratio" },
      { id: "shared", family: "usage", name: "Calls", unit: "calls" },
    ];
    const collisionRun = { ...focusedRun, evidence: [], metrics: [
      { metric_id: "shared", family: "quality" as const, value: 0.25, unit: "ratio", task_id: focusedRun.tasks![0].task_id },
      { metric_id: "shared", family: "usage" as const, value: 2, unit: "calls", task_id: focusedRun.tasks![0].task_id },
    ] };
    render(<RunResults {...props} run={collisionRun} metrics={collisionRun.metrics} evidence={[]} definitions={definitions} />);
    expect(screen.getByText("25.0%")).toBeInTheDocument();
    expect(screen.getByText("2 calls")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Calls" }));
    expect(screen.getByRole("button", { name: "Calls" }).closest("th")).toHaveAttribute("aria-sort", "ascending");
    expect(screen.getByRole("button", { name: "Accuracy" }).closest("th")).toHaveAttribute("aria-sort", "none");
  });

  it("sorts service estimates when recorded amounts are all unknown without mixing the two", async () => {
    const user = userEvent.setup();
    const namedBenchmark = { ...benchmark, pipelines: [
      { id: "pipeline-a", name: "Service Alpha", mode: "standalone" as const },
      { id: "pipeline-b", name: "Service Beta", mode: "standalone" as const },
    ] };
    const summaryRun: Run = { ...focusedRun, solution_ids: ["pipeline-a", "pipeline-b"], costs: { ...costFixture, solutions: costFixture.solutions.map(solution => ({
      ...solution, recorded: { ...solution.recorded, status: "unknown", amounts: { USD: null }, covered_attempts: 0 },
    })) } };
    render(<EvaluationWorkspace benchmark={namedBenchmark} run={summaryRun} eventCount={0} artifactUrl={vi.fn()} onConfigure={vi.fn()} onInspect={vi.fn()} />);
    const performance = screen.getByRole("region", { name: "Solution performance, horizontally scrollable" });
    const order = () => within(performance).getAllByRole("row").slice(1).map(row => within(row).getAllByRole("cell")[0].textContent);
    expect(within(performance).queryByRole("columnheader", { name: "Pipeline" })).not.toBeInTheDocument();
    await user.click(within(performance).getByRole("button", { name: "Est. cost (USD)" }));
    expect(order()).toEqual(["Service Alpha", "Service Beta"]);
    await user.click(within(performance).getByRole("button", { name: "Est. cost (USD)" }));
    expect(order()).toEqual(["Service Beta", "Service Alpha"]);
    await user.click(within(performance).getByRole("button", { name: "Cost (USD)" }));
    expect(order()).toEqual(["Service Alpha", "Service Beta"]);
    expect(within(performance).queryByText("Unknown", { selector: "small" })).not.toBeInTheDocument();
    await user.click(screen.getByText("Operational details", { selector: "summary" }));
    await user.click(screen.getByRole("tab", { name: "Cost" }));
    const services = within(screen.getByRole("tabpanel", { name: "Cost" })).getByRole("list", { name: "Per-service costs" });
    expect(within(services).getByRole("heading", { name: "Service Alpha" })).toBeInTheDocument();
  });

  it("keeps same-ID summary metrics and usage labels in their declared families", () => {
    const definitions: MetricDefinition[] = [
      { id: "shared", family: "quality", name: "Accuracy", unit: "ratio" },
      { id: "shared", family: "usage", name: "Calls", unit: "calls" },
    ];
    const summaryRun: Run = { ...focusedRun, evidence: [], metric_definitions: definitions, metrics: [
      { metric_id: "shared", family: "quality", value: 0.25, unit: "ratio", solution_id: focusedRun.solution_ids[0] },
      { metric_id: "shared", family: "usage", value: 2, unit: "calls", solution_id: focusedRun.solution_ids[0] },
    ] };
    render(<EvaluationWorkspace benchmark={benchmark} run={summaryRun} eventCount={0} artifactUrl={vi.fn()} onConfigure={vi.fn()} onInspect={vi.fn()} />);
    const performance = screen.getByRole("region", { name: "Solution performance, horizontally scrollable" });
    expect(within(performance).getByText("25.0%")).toBeInTheDocument();
    expect(within(performance).getByText("2 calls")).toBeInTheDocument();
    const usageCard = screen.getAllByRole("article").find(article => article.classList.contains("family-usage"));
    expect(usageCard).toHaveTextContent("Calls");
    expect(usageCard).toHaveTextContent("2 calls");
  });

  it("drills into only one result and restores keyboard focus", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("fetch", vi.fn(async () => new Response("Synthetic reference")));
    render(<EvaluationWorkspace benchmark={benchmark} run={focusedRun} eventCount={0} artifactUrl={sha => `/api/artifacts/${sha}`} onConfigure={vi.fn()} onInspect={vi.fn()} />);
    const trigger = screen.getAllByRole("button", { name: /^Inspect / })[0];
    await user.click(trigger);
    expect(screen.getByRole("heading", { name: /^Result detail:/ })).toHaveFocus();
    expect(screen.getByRole("heading", { name: "Comparison inspector" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(trigger).toHaveFocus();
    expect(screen.queryByRole("heading", { name: "Comparison inspector" })).not.toBeInTheDocument();
  });

  it("every operational Cost entry displays amounts, not an artifact ledger", async () => {
    const user = userEvent.setup();
    render(<EvaluationWorkspace benchmark={benchmark} run={{ ...focusedRun, costs: costFixture }} eventCount={0} artifactUrl={vi.fn()} onConfigure={vi.fn()} onInspect={vi.fn()} />);
    await user.click(screen.getByText("Operational details", { selector: "summary" }));
    await user.click(screen.getByRole("tab", { name: "Cost" }));
    const panel = screen.getByRole("tabpanel", { name: "Cost" });
    expect(within(panel).getByRole("heading", { name: "Cost totals and coverage" })).toBeInTheDocument();
    expect(within(panel).getByText("Current-rate estimate: $0.6000 USD")).toBeInTheDocument();
    expect(within(panel).getByRole("list", { name: "Per-service costs" })).toHaveTextContent("pipeline-b");
    expect(within(panel).queryByText(/0 applicable metrics/)).not.toBeInTheDocument();
  });

  it("handles empty results and coalesces duplicate attempt observations without summing", () => {
    expect(resultRows({ ...run, tasks: [], sample_artifacts: {} }, [], [])).toEqual([]);
    render(<RunResults {...props} run={{ ...run, tasks: [], sample_artifacts: {} }} metrics={[]} evidence={[]} />);
    expect(screen.getByText("No results")).toBeInTheDocument();
    const rows = resultRows(run, [{ metric_id: "word_error_rate", family: "quality", task_id: run.tasks![0].task_id, value: "99" }], run.evidence!);
    expect(rows[0].metrics.find(metric => metric.metric_id === "word_error_rate")?.value).toBe("0.6666666667");
  });
});
