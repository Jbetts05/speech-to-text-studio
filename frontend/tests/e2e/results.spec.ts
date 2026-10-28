import { expect, test, type Page } from "@playwright/test";
import axe from "axe-core";
import { speechBenchmarkFixture, speechRunFixture, speechReportFixture, speechSampleIds, speechSolutionIds, transcriptComparisonFixture } from "../../src/testing/speechFixtures";
import { runId, runSummaryWireFixture, comparisonArtifactId, referenceArtifactId } from "../../src/testing/fixtures";
import { costFixture } from "../../src/testing/costFixtures";
import type { RunCosts } from "../../src/types";

// A synthetic complete matrix, not the preserved pilot or a copied provider run.
const tasks = speechRunFixture.tasks.map((task, index) => ({
  ...task, status: "succeeded" as const, cooldown_until: null, attempts: 1,
  result: { ...speechRunFixture.tasks[0].result!, task_id: task.id, sample_id: task.sample_id, solution_id: task.solution_id,
    metrics: [{ metric_id: "word_error_rate", family: "quality" as const, unit: "ratio", value: String(index / 130) }] },
}));
const sampleCost = {
  ...costFixture.solutions[1],
  recorded_usage: { seconds: "0.0001", calls: "1" }, usage_uncertainty: ["Submitted work does not establish billed usage."],
  recorded: { ...costFixture.solutions[1].recorded, covered_attempts: 0, total_attempts: 1 },
  current_estimate: { ...costFixture.solutions[1].current_estimate, status: "known" as const, covered_attempts: 1, total_attempts: 1, amounts: { USD: "0.000002" }, reasons: [] },
};
const costs: RunCosts = {
  ...costFixture,
  recorded_total: { ...sampleCost.recorded, total_attempts: tasks.length, total_tasks: tasks.length, terminal_tasks: tasks.length },
  current_estimate_total: { ...sampleCost.current_estimate, covered_attempts: tasks.length, total_attempts: tasks.length, total_tasks: tasks.length, terminal_tasks: tasks.length, amounts: { USD: "0.00026" } },
  solutions: speechSolutionIds.map(solution_id => ({
    ...sampleCost, solution_id,
    recorded_usage: { seconds: "0.0026", calls: "26" },
    recorded: { ...sampleCost.recorded, total_attempts: speechSampleIds.length, total_tasks: speechSampleIds.length, terminal_tasks: speechSampleIds.length },
    current_estimate: { ...sampleCost.current_estimate, covered_attempts: speechSampleIds.length, total_attempts: speechSampleIds.length, total_tasks: speechSampleIds.length, terminal_tasks: speechSampleIds.length, amounts: { USD: "0.000052" } },
  })),
  samples: Object.fromEntries(speechSampleIds.map(sample => [sample, speechSolutionIds.map(solution_id => ({
    ...sampleCost, solution_id,
  }))])),
};

async function fullRun(page: Page) {
  let artifactRequests = 0;
  let operationalRequests = 0;
  const report = {
    ...speechReportFixture, status: "completed", results: tasks.map(task => task.result),
    coverage: Object.fromEntries(speechSolutionIds.map(id => [id, { expected_sample_ids: speechSampleIds, evaluated_sample_ids: speechSampleIds, complete: true, comparable: true, reason: "Synthetic full coverage." }])),
    usage_uncertainty: Object.fromEntries(speechSolutionIds.map(id => [id, ["Submitted work does not establish billed usage."]])),
  };
  await page.route("**/api/**", async route => {
    const path = new URL(route.request().url()).pathname;
    let body: unknown;
    if (path === "/api/benchmarks") body = [speechBenchmarkFixture];
    else if (path === "/api/runtime") body = { runtime: "local", execution_backend: "single-instance-sqlite-filesystem", horizontal_scaling: false, persistent_state_required: false, azure_storage_required: false, azure_storage_role: "readiness-and-explicit-repository-api" };
    else if (path === "/api/runs") body = [{ ...runSummaryWireFixture, benchmark_id: "speech_to_text" }];
    else if (path === `/api/runs/${runId}/finalized-evaluation`) body = {
      record_source: "finalized_report",
      run: { ...runSummaryWireFixture, benchmark_id: "speech_to_text", status: "completed" },
      manifest: speechRunFixture.manifest, report, costs,
      sample_artifacts: speechRunFixture.sample_artifacts,
    };
    else if (path === `/api/runs/${runId}`) { operationalRequests++; body = { ...speechRunFixture, tasks, costs, status: "completed", report_artifact_id: "9".repeat(64) }; }
    else if (path === `/api/runs/${runId}/events`) { operationalRequests++; body = []; }
    else if (path === `/api/runs/${runId}/report`) { operationalRequests++; body = report; }
    else if (path.startsWith("/api/artifacts/")) {
      artifactRequests++;
      body = path.endsWith(comparisonArtifactId) ? transcriptComparisonFixture : path.endsWith(referenceArtifactId) ? "THE SMALL CAT" : {};
    } else { await route.fulfill({ status: 404 }); return; }
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.goto(`/?view=evaluation&benchmark=speech_to_text&run=${runId}`);
  await expect(page.getByRole("heading", { name: "All results", exact: true })).toBeVisible();
  return { artifacts: () => artifactRequests, operations: () => operationalRequests };
}

test("all 130 results are primary, filters and sorting retain explicit scope, detail is lazy", async ({ page }) => {
  const requestCount = await fullRun(page);
  const table = page.getByRole("region", { name: "All results, horizontally scrollable" });
  await expect(table.locator("tbody tr")).toHaveCount(130);
  expect(requestCount.artifacts()).toBe(0);
  expect(requestCount.operations()).toBe(0);
  await expect(page.getByText(/Historical record from the finalized report/)).toBeVisible();
  await expect(page.getByRole("heading", { name: "Comparison inspector" })).toHaveCount(0);
  await expect(page.getByRole("tab", { name: "Cost" })).toBeHidden();
  await page.getByRole("combobox", { name: "Service", exact: true }).selectOption(speechSolutionIds[2]);
  await expect(table.locator("tbody tr")).toHaveCount(26);
  await expect(page.getByText(/26 of 130 results shown/)).toContainText("Summaries above cover the whole run");
  const sort = table.getByRole("button", { name: "Word error rate", exact: true });
  await sort.click();
  await sort.click();
  await expect(table.locator("tbody tr").first()).toContainText(speechSampleIds[25]);
  await page.getByRole("combobox", { name: "Service", exact: true }).selectOption("");
  await expect(table.locator("tbody tr")).toHaveCount(130);
  const opener = table.getByRole("button", { name: /^Inspect / }).first();
  await opener.click();
  await expect(page.getByRole("heading", { name: /^Result detail:/ })).toBeFocused();
  await expect(page.getByRole("list", { name: "Normalized transcript alignment" })).toHaveCount(1);
  expect(requestCount.artifacts()).toBeGreaterThan(0);
  expect(requestCount.operations()).toBe(0);
  await page.keyboard.press("Escape");
  await expect(opener).toBeFocused();
  await expect(page.getByRole("heading", { name: "Comparison inspector" })).toHaveCount(0);
});

test("current diagnostics are explicitly loaded without replacing all recorded rows", async ({ page }) => {
  const requests = await fullRun(page);
  await page.getByText("Operational details", { exact: true }).click();
  expect(requests.operations()).toBe(0);
  await page.getByRole("button", { name: "Load current diagnostics" }).click();
  await expect(page.getByText("Current operational snapshot, separate from historical results.")).toBeVisible();
  expect(requests.operations()).toBe(1);
  await expect(page.locator(".results-table tbody tr")).toHaveCount(130);
  await expect(page.getByText(/Historical record from the finalized report/)).toBeVisible();
});

test("comparison cost leads with its estimate and reveals coverage only on request", async ({ page }) => {
  await fullRun(page);
  await page.locator(".results-table").getByRole("button", { name: /^Inspect / }).first().click();
  const comparison = page.getByRole("region", { name: "Reference and solution output comparison, horizontally scrollable" });
  await expect(comparison.getByText("Est. $0.000002 USD", { exact: true })).toBeVisible();
  await expect(comparison.getByText("Unknown USD", { exact: true })).toHaveCount(0);
  const recorded = comparison.getByText("Recorded cost: Unknown USD", { exact: true });
  await expect(recorded).toBeHidden();
  const disclosure = comparison.getByText("Cost details", { exact: true });
  await disclosure.focus();
  await page.keyboard.press("Enter");
  await expect(recorded).toBeVisible();
  await expect(comparison.getByText(/not billed cost/)).toBeVisible();
  await page.keyboard.press("Enter");
  await expect(recorded).toBeHidden();
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    await expect(comparison.getByText("Est. $0.000002 USD", { exact: true })).toBeVisible();
    await page.locator(".result-drilldown").screenshot({ path: `test-results/cost-comparison-${width}.png` });
  }
});

test("cost tab has monetary totals, services, examples and one quiet routine caveat", async ({ page }) => {
  await fullRun(page);
  await expect(page.locator(".usage-context")).toHaveCount(1);
  await expect(page.locator(".usage-context .inline-warning")).toHaveCount(0);
  await page.getByText("Operational details", { exact: true }).click();
  await page.getByRole("tab", { name: "Cost", exact: true }).click();
  const panel = page.getByRole("tabpanel", { name: "Cost", exact: true });
  await expect(panel.getByRole("heading", { name: "Cost totals and coverage" })).toBeVisible();
  await expect(panel.getByText("Current-rate estimate: $0.00026 USD")).toBeVisible();
  await expect(panel.getByRole("list", { name: "Per-service costs" }).locator(":scope > li")).toHaveCount(5);
  await panel.getByText("Per-example costs", { exact: true }).click();
  await expect(panel.getByText("Est. $0.000002 USD")).toHaveCount(130);
  await expect(panel.getByText(/0 applicable metrics/)).toHaveCount(0);
  await page.addScriptTag({ content: axe.source });
  expect((await page.evaluate(async () => window.axe.run(document, { runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"] } }))).violations).toEqual([]);
});

for (const width of [1220, 1100, 900, 640, 390]) {
  test(`whole-run ledger bounds and keyboard metric help at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await fullRun(page);
    const table = page.getByRole("region", { name: "All results, horizontally scrollable" });
    const help = table.getByLabel("About Word error rate");
    await help.focus();
    await page.keyboard.press("Enter");
    await expect(help.locator("..")).toHaveAttribute("open", "");
    await page.keyboard.press("Enter");
    for (const offset of [0, 240]) {
      await table.evaluate((node, left) => { node.scrollLeft = left; }, offset);
      const geometry = await table.evaluate(node => {
        const cells = [...node.querySelectorAll<HTMLElement>("[data-sticky-track]")];
        return {
          scroll: node.scrollLeft,
          valid: cells.every(cell => {
            const style = getComputedStyle(cell), bounds = cell.getBoundingClientRect();
            const range = document.createRange(); range.selectNodeContents(cell);
            return style.position === "sticky" && style.width === style.minWidth && style.width === style.maxWidth &&
              style.backgroundColor !== "rgba(0, 0, 0, 0)" && style.zIndex !== "auto" && style.boxShadow !== "none" &&
              [...cell.children].every(child => { const rect = child.getBoundingClientRect(); return rect.left >= bounds.left - 1 && rect.right <= bounds.right + 1; }) &&
              [...range.getClientRects()].every(rect => rect.left >= bounds.left - 1 && rect.right <= bounds.right + 1);
          }),
          pageOverflow: document.documentElement.scrollWidth > window.innerWidth,
        };
      });
      expect(geometry.valid).toBe(true);
      expect(geometry.pageOverflow).toBe(false);
      if (offset) expect(geometry.scroll).toBeGreaterThan(0);
      await page.screenshot({ path: `test-results/results-${width}-scroll-${offset}.png`, fullPage: false });
    }
    await table.evaluate(node => { node.scrollLeft = 0; });
    await page.screenshot({ path: `test-results/results-${width}.png`, fullPage: false });
  });
}
