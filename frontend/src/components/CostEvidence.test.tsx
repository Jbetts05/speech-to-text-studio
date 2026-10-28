import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { mapBenchmark, mapRunDetail } from "../api/wire";
import { costCoverageFixture, costFixture } from "../testing/costFixtures";
import { benchmarkWireFixture, runDetailWireFixture } from "../testing/fixtures";
import { CostCell, CostEvidence, costCoverageText, costHeadline, coverageAmount } from "./CostEvidence";
import { EvaluationWorkspace } from "./EvaluationWorkspace";
import { ReportsWorkspace } from "./WorkspacePages";

describe("separate recorded cost and current-rate evidence", () => {
  it("uses optional readable names in primary cost headings and retains ID fallback", async () => {
    const user = userEvent.setup();
    render(<CostEvidence costs={costFixture} solutionNames={{ "pipeline-a": "Synthetic service" }} />);
    const services = screen.getByRole("list", { name: "Per-service costs" });
    expect(within(services).getByRole("heading", { name: "Synthetic service" })).toBeInTheDocument();
    expect(within(services).getByRole("heading", { name: "pipeline-b" })).toBeInTheDocument();
    await user.click(screen.getByText("Per-example costs", { selector: "summary" }));
    const sample = screen.getByRole("heading", { name: "sample-001" }).closest("section")!;
    expect(within(sample).getByText("Synthetic service")).toBeInTheDocument();
  });

  it("distinguishes a real zero, unknown and a tiny positive amount", () => {
    expect(coverageAmount({ ...costCoverageFixture, amounts: { USD: "0" } })).toBe("$0.0000 USD");
    expect(coverageAmount({ ...costCoverageFixture, amounts: { USD: null } })).toBe("Unknown USD");
    expect(coverageAmount({ ...costCoverageFixture, amounts: { USD: "0.00000001" } })).toBe("<$0.000001 USD");
    expect(coverageAmount({ ...costCoverageFixture, amounts: {} })).toBe("Unknown");
  });

  it("never adds currencies together or calls a partial subtotal complete", () => {
    const mixed = { ...costCoverageFixture, amounts: { USD: "1", EUR: "2" } };
    expect(coverageAmount(mixed)).toBe("$1.0000 USD; €2.0000 EUR");
    expect(costHeadline({ ...costFixture, recorded_total: mixed }, []).value).toBe("By currency");
    expect(costHeadline(costFixture, []).detail).toBe("Partial recorded subtotal · 2/4 recorded attempts · 2/2 tasks terminal");
    expect(costCoverageText(costFixture.current_estimate_total, true)).toMatch(/^Partial estimate · 3\/4/);
  });

  it("labels a current-rate headline conditional when original amounts are unknown", () => {
    const costs = { ...costFixture, recorded_total: { ...costFixture.recorded_total, amounts: { USD: null } } };
    expect(costHeadline(costs, [])).toEqual({
      value: "$0.6000 USD", detail: "Partial estimate · 3/4 recorded attempts · 2/2 tasks terminal",
    });
    expect(costHeadline(undefined, [])).toMatchObject({ value: "Unknown" });
  });

  it("shows the estimate without an empty recorded-cost block and discloses its evidence on demand", async () => {
    const user = userEvent.setup();
    render(<CostCell cost={costFixture.solutions[1]} />);
    expect(screen.queryByText("Unknown USD", { exact: true })).not.toBeInTheDocument();
    expect(screen.getByText("Est. $0.2000 USD")).toBeVisible();
    expect(screen.getByText("Partial estimate", { exact: true })).toBeVisible();
    expect(screen.getByText(/0\/2 recorded attempts/)).not.toBeVisible();
    expect(screen.getByText(/1\/2 recorded attempts.*not billed cost/)).not.toBeVisible();
    expect(screen.getByText("The immutable run has no pinned price rates.")).not.toBeVisible();
    await user.click(screen.getByText("Cost details", { selector: "summary" }));
    expect(screen.getByText("Recorded cost: Unknown USD")).toBeVisible();
    expect(screen.getByText(/0\/2 recorded attempts/)).toBeVisible();
    expect(screen.getByText(/1\/2 recorded attempts.*not billed cost/)).toBeVisible();
    expect(screen.getByText("One attempt has no recorded duration.")).toBeVisible();
  });

  it.each(["0", "0.5"])("keeps an actual recorded amount (%s) visible separately from the estimate", amount => {
    render(<CostCell cost={{ ...costFixture.solutions[0], recorded: { ...costCoverageFixture, amounts: { USD: amount } } }} />);
    expect(screen.getByText(amount === "0" ? "$0.0000 USD" : "$0.5000 USD", { exact: true })).toBeVisible();
    expect(screen.getByText("Est. $0.4000 USD")).toBeVisible();
  });

  it("keeps unknown visible when no estimate is available", () => {
    const unknown = { ...costCoverageFixture, status: "unknown" as const, amounts: { USD: null }, covered_attempts: 0 };
    render(<CostCell cost={{ ...costFixture.solutions[1], recorded: unknown, current_estimate: unknown }} />);
    expect(screen.getByText("Unknown USD", { exact: true })).toBeVisible();
    expect(screen.queryByText(/^Est\./)).not.toBeInTheDocument();
  });

  it("does not hide a partial recorded subtotal or an unknown currency in mixed evidence", () => {
    render(<CostCell cost={{ ...costFixture.solutions[0], recorded: { ...costCoverageFixture, status: "partial", amounts: { USD: "0.5", EUR: null } } }} />);
    expect(screen.getByText("$0.5000 USD; Unknown EUR", { exact: true })).toBeVisible();
    expect(screen.getByText("Partial recorded subtotal", { exact: true })).toBeVisible();
  });

  it("uses the same estimate-first summary for totals without changing original costs", () => {
    const costs = { ...costFixture, recorded_total: { ...costFixture.recorded_total, status: "unknown" as const, amounts: { USD: null }, covered_attempts: 0 } };
    const before = JSON.stringify(costs);
    const { container } = render(<CostEvidence costs={costs} showSamples={false} />);
    const totals = container.querySelector<HTMLElement>(".cost-evidence > .cost-cell")!;
    expect(within(totals).getByText("Current-rate estimate: $0.6000 USD")).toBeVisible();
    expect(within(totals).getByText("Recorded cost: Unknown USD")).not.toBeVisible();
    expect(JSON.stringify(costs)).toBe(before);
  });

  it("shows original and reviewed price provenance, dates, assumptions and all-attempt usage", async () => {
    const user = userEvent.setup();
    render(<CostEvidence costs={costFixture} />);
    await user.click(screen.getByText("Pricing basis, sources and retry-inclusive usage"));
    const first = screen.getAllByRole("heading", { name: "pipeline-a" }).find(heading => heading.closest(".cost-source"))!.closest("section")!;
    expect(first).toHaveTextContent("synthetic-price@1");
    expect(first).toHaveTextContent("synthetic-price@2");
    expect(first).toHaveTextContent("Reviewed 2026-01-20");
    expect(first).toHaveTextContent("effective 2026-01-01");
    expect(first).toHaveTextContent("20 seconds; 2 calls");
    expect(first).toHaveTextContent("$0.2000 USD per 10 seconds");
    expect(first).toHaveTextContent("Synthetic profile assumption");
    expect(within(first).getByRole("link", { name: /Pricing source/ })).toHaveAttribute("href", "https://example.com/synthetic-pricing");
    expect(screen.getByText(/Original manifests and reports are unchanged/)).toBeInTheDocument();
    expect(screen.getByText("Usage is not recorded for every attempt.")).toBeInTheDocument();
    const second = screen.getAllByRole("heading", { name: "pipeline-b" }).find(heading => heading.closest(".cost-source"))!.closest("section")!;
    const missingUsage = within(second).getAllByText("One attempt has no recorded duration.")
      .find(item => item.parentElement === second);
    expect(missingUsage).toBeVisible();
  });

  it("does not turn untrusted non-HTTPS provenance into an active link", () => {
    const first = costFixture.solutions[0];
    render(<CostEvidence costs={{ ...costFixture, solutions: [{
      ...first, current_pricing: { ...first.current_pricing!, source_urls: ["javascript:alert(1)"] },
    }] }} />);
    expect(screen.queryByRole("link", { hidden: true })).not.toBeInTheDocument();
    expect(screen.getByText("Pricing source (not a web link): javascript:alert(1)")).toBeInTheDocument();
  });

  it("keeps COST visible with all-null metrics and shows per-sample cost in all results", () => {
    const run = {
      ...mapRunDetail(runDetailWireFixture), costs: costFixture,
      metrics: [], evidence: [], sample_artifacts: {},
    };
    const props = {
      benchmark: mapBenchmark(benchmarkWireFixture), eventCount: 0,
      artifactUrl: vi.fn(), onConfigure: vi.fn(), onInspect: vi.fn(),
    };
    const { rerender } = render(<EvaluationWorkspace
      {...props} run={run}
    />);
    const performance = screen.getByRole("region", { name: "Solution performance, horizontally scrollable" });
    expect(within(performance).getByRole("button", { name: "Cost (USD)" })).toBeInTheDocument();
    expect(within(performance).getByText("Unknown USD")).toBeInTheDocument();
    expect(within(performance).queryByRole("columnheader", { name: "Exact accuracy" })).not.toBeInTheDocument();
    const comparison = screen.getByRole("region", { name: "All results, horizontally scrollable" });
    expect(within(comparison).getByRole("columnheader", { name: "Cost (USD)" })).toBeInTheDocument();
    expect(within(comparison).getByText("$0.5000 USD")).toBeInTheDocument();
    expect(within(comparison).getByText("$0.2000 USD")).toBeInTheDocument();
    rerender(<EvaluationWorkspace {...props} run={{ ...run, costs: undefined }} />);
    expect(within(performance).getByRole("button", { name: "Cost" })).toBeInTheDocument();
    expect(within(performance).getAllByText("Unknown")).toHaveLength(2);
  });

  it("shows separate cost evidence on Reports without replacing original downloads", () => {
    const run = { ...mapRunDetail(runDetailWireFixture), costs: costFixture, status: "completed" as const };
    render(<ReportsWorkspace run={run} onNavigate={vi.fn()} />);
    expect(screen.getByRole("heading", { name: "Cost totals and coverage" })).toBeInTheDocument();
    expect(screen.getByText("Current-rate estimate: $0.6000 USD")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Download JSON" })).toHaveAttribute("href", `/api/runs/${run.id}/report?format=json`);
    expect(screen.getByRole("link", { name: "Download CSV" })).toHaveAttribute("href", `/api/runs/${run.id}/report?format=csv`);
  });
});
