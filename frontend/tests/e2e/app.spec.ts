import { expect, test, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import axe from "axe-core";
import { costFixture } from "../../src/testing/costFixtures";
import type { RunCosts } from "../../src/types";
import {
  speechBenchmarkFixture, speechRunFixture, speechReportFixture, transcriptComparisonFixture,
  speechSampleIds, speechSolutionIds,
} from "../../src/testing/speechFixtures";
import {
  benchmarkWireFixture,
  eventsWireFixture,
  reportWireFixture,
  runDetailWireFixture,
  runId,
  runSummaryWireFixture,
  inputArtifactId,
  referenceArtifactId,
  comparisonArtifactId,
  reportArtifactId,
} from "../../src/testing/fixtures";

declare global {
  interface Window {
    axe: typeof axe;
  }
}

async function mockSpeechApi(page: Page, terminal = false, available = true, costs?: RunCosts) {
  const flac = execFileSync("python", [
    "-c", "import sys; from tests.speech_support import make_flac; sys.stdout.buffer.write(make_flac(1600))",
  ], { cwd: resolve("..") });
  await page.unroute("**/api/**");
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === `/api/artifacts/${inputArtifactId}`) {
      await route.fulfill({ status: 200, contentType: "audio/flac", body: flac });
      return;
    }
    if (path === `/api/artifacts/${referenceArtifactId}`) {
      await route.fulfill({ status: 200, contentType: "text/plain", body: "THE SMALL CAT" });
      return;
    }
    let body: unknown;
    if (path === "/api/benchmarks") {
      body = [{
        ...speechBenchmarkFixture,
        dataset_availability: available ? speechBenchmarkFixture.dataset_availability : {
          status: "not-configured", reason: "Publish the external 26-sample pilot.",
          sample_count: 26, scored_sample_count: 26,
        },
      }];
    } else if (path === "/api/runtime") {
      body = {
        runtime: "local", execution_backend: "single-instance-sqlite-filesystem",
        horizontal_scaling: false, persistent_state_required: false,
        azure_storage_required: false, azure_storage_role: "readiness-and-explicit-repository-api",
      };
    } else if (path === "/api/runs") {
      body = [{ ...runSummaryWireFixture, benchmark_id: "speech_to_text" }];
    } else if (path === `/api/runs/${runId}`) {
      body = terminal ? {
        ...speechRunFixture, costs, status: "completed-with-failures",
        report_ref: `/api/artifacts/${reportArtifactId}`, report_artifact_id: reportArtifactId,
        tasks: speechRunFixture.tasks.map((task) => ({
          ...task, status: task.result ? "succeeded" : "poisoned",
          attempts: 1, cooldown_until: null,
          result: speechReportFixture.results.find((result) => result.task_id === task.id),
        })),
      } : speechRunFixture;
    } else if (path === `/api/runs/${runId}/events`) {
      body = [];
    } else if (path === `/api/runs/${runId}/finalized-evaluation`) {
      body = { record_source: "live_detail_required", run: { ...runSummaryWireFixture, benchmark_id: "speech_to_text" }, reason: "incomplete_results", detail: "Synthetic fixture has partial historical records; loading live detail." };
    } else if (path === `/api/runs/${runId}/report`) {
      body = speechReportFixture;
    } else if (path === `/api/artifacts/${comparisonArtifactId}`) {
      body = transcriptComparisonFixture;
    } else if (path.startsWith("/api/artifacts/")) {
      body = { error: { message: "Invented provider response; retained in operational detail." } };
    } else {
      await route.fulfill({ status: 404, body: "Not found" });
      return;
    }
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.goto(`/?view=evaluation&benchmark=speech_to_text&run=${runId}`);
  await page.getByRole("button", { name: `Inspect ${speechSampleIds[0]} / ${speechSolutionIds[0]}`, exact: true }).click();
  await page.getByText("Operational details", { exact: true }).click();
  await expect(page.getByRole("list", { name: "Normalized transcript alignment" }).first()).toBeVisible();
}

async function mockApi(
  page: Page,
  singleSolution = false,
  includeRunSummary = true,
) {
  const detail = singleSolution
    ? {
        ...runDetailWireFixture,
        manifest: {
          ...runDetailWireFixture.manifest,
          solution_ids: ["pipeline-a"],
          solution_descriptors: {
            "pipeline-a":
              runDetailWireFixture.manifest.solution_descriptors["pipeline-a"],
          },
        },
        tasks: runDetailWireFixture.tasks.filter(
          (task) => task.solution_id === "pipeline-a",
        ),
      }
    : runDetailWireFixture;
  const report = singleSolution
    ? {
        ...reportWireFixture,
        quality_comparability: {
          "pipeline-a": reportWireFixture.quality_comparability["pipeline-a"],
        },
        cost: { "pipeline-a": reportWireFixture.cost["pipeline-a"] },
        cost_currency: {
          "pipeline-a": reportWireFixture.cost_currency["pipeline-a"],
        },
      }
    : reportWireFixture;
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
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
    } else if (path === "/api/runs" && route.request().method() === "GET") {
      body = includeRunSummary ? [runSummaryWireFixture] : [];
    } else if (path === `/api/runs/${runId}`) {
      body = { ...detail, status: "completed" as const };
    } else if (path === `/api/runs/${runId}/events`) {
      body = eventsWireFixture;
    } else if (path === `/api/runs/${runId}/finalized-evaluation`) {
      body = { record_source: "live_detail_required", run: runSummaryWireFixture, reason: "incomplete_results", detail: "Synthetic fixture has partial historical records; loading live detail." };
    } else if (path === `/api/runs/${runId}/report`) {
      body = report;
    } else if (path.startsWith("/api/artifacts/")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          expected: "archive_note",
          predicted: "archive_note",
        }),
      });
      return;
    } else {
      await route.fulfill({ status: 404, body: "Not found" });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
  });
}

async function validateHorizontalLedgers(page: Page, scrollLeft: number) {
  const ledgers = page.locator("[data-horizontal-ledger]");
  const count = await ledgers.count();
  expect(count).toBe(4);

  for (let index = 0; index < count; index += 1) {
    const ledger = ledgers.nth(index);
    await ledger.evaluate((node, left) => {
      node.scrollLeft = left;
    }, scrollLeft);
    await page.waitForTimeout(40);

    const result = await ledger.evaluate((container) => {
      const tolerance = 1;
      const containerBounds = container.getBoundingClientRect();
      const trackCells = [
        ...container.querySelectorAll<HTMLElement>("[data-sticky-track]"),
      ];
      const groups = new Map<string, HTMLElement[]>();
      for (const cell of trackCells) {
        const key = cell.dataset.stickyTrack ?? "default";
        groups.set(key, [...(groups.get(key) ?? []), cell]);
      }
      const groupGeometry = [...groups.values()].map((cells) => {
        const first = cells[0];
        const firstStyle = getComputedStyle(first);
        const firstRect = first.getBoundingClientRect();
        return cells.every((cell) => {
          const style = getComputedStyle(cell);
          const rect = cell.getBoundingClientRect();
          const childBounds = [...cell.children].map((child) =>
            child.getBoundingClientRect(),
          );
          const textRange = document.createRange();
          textRange.selectNodeContents(cell);
          const textBounds = [...textRange.getClientRects()];
          const visible =
            rect.bottom > 0 &&
            rect.top < window.innerHeight &&
            rect.right > 0 &&
            rect.left < window.innerWidth &&
            rect.bottom > containerBounds.top &&
            rect.top < containerBounds.bottom;
          const ownsTrailingEdge =
            !visible ||
            document
              .elementsFromPoint(
                Math.min(rect.right - 2, window.innerWidth - 1),
                Math.max(0, Math.min(rect.top + rect.height / 2, window.innerHeight - 1)),
              )
              .some((element) => element === cell || cell.contains(element));
          return (
            Math.abs(rect.width - firstRect.width) <= tolerance &&
            style.width === firstStyle.width &&
            style.minWidth === firstStyle.minWidth &&
            style.maxWidth === firstStyle.maxWidth &&
            style.position === "sticky" &&
            style.zIndex !== "auto" &&
            style.backgroundColor !== "rgba(0, 0, 0, 0)" &&
            ["hidden", "clip"].includes(style.overflowX) &&
            style.boxShadow !== "none" &&
            childBounds.every(
              (child) =>
                child.left >= rect.left - tolerance &&
                child.right <= rect.right + tolerance,
            ) &&
            textBounds.every(
              (text) =>
                text.left >= rect.left - tolerance &&
                text.right <= rect.right + tolerance,
            ) &&
            ownsTrailingEdge
          );
        });
      });
      return {
        label: container.getAttribute("aria-label"),
        viewport: window.innerWidth,
        clientWidth: container.clientWidth,
        scrollWidth: container.scrollWidth,
        hasHorizontalOverflow: container.scrollWidth > container.clientWidth,
        actualScrollLeft: container.scrollLeft,
        validStickyTracks: groupGeometry.every(Boolean),
      };
    });

    if (scrollLeft > 0 && result.hasHorizontalOverflow) expect(result.actualScrollLeft).toBeGreaterThan(0);
    expect(result.validStickyTracks, JSON.stringify(result)).toBe(true);
  }
}

test.beforeEach(async ({ page }) => {
  await mockApi(page);
  await page.goto(`/?view=evaluation&benchmark=bench_general&run=${runId}`);
  await expect(
    page.getByRole("heading", { name: "Solution performance" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Inspect sample-001 / pipeline-a", exact: true }).click();
  await page.getByText("Operational details", { exact: true }).click();
});

test("inherits the exact Evidence Ledger shell and evaluation hierarchy", async ({
  page,
}) => {
  const contract = await page.evaluate(() => {
    const root = getComputedStyle(document.documentElement);
    const frame = getComputedStyle(document.querySelector(".app-frame")!);
    const body = getComputedStyle(document.body);
    const cards = [...document.querySelectorAll<HTMLElement>(".kpi-card")];
    return {
      canvas: root.getPropertyValue("--cp-bg").trim(),
      navigation: root.getPropertyValue("--cp-nav-bg").trim(),
      accent: root.getPropertyValue("--cp-accent").trim(),
      tracks: frame.gridTemplateColumns,
      rows: frame.gridTemplateRows,
      font: body.fontFamily,
      cardCount: cards.length,
      cardHeights: cards.map((card) => card.getBoundingClientRect().height),
    };
  });
  expect(contract.canvas).toBe("#f5f7fa");
  expect(contract.navigation).toBe("#071b33");
  expect(contract.accent).toBe("#0f5cc0");
  expect(contract.tracks).toMatch(/^218px 224px /);
  expect(contract.rows).toMatch(/^58px /);
  expect(contract.font).toContain("Segoe UI");
  expect(contract.cardCount).toBe(5);
  expect(contract.cardHeights.every((height) => height >= 112)).toBe(true);

  const headings = await page.locator("main h2").allTextContents();
  expect(headings.indexOf("Solution performance")).toBeLessThan(
    headings.indexOf("Comparison inspector"),
  );
  expect(headings.indexOf("Comparison inspector")).toBeLessThan(
    headings.indexOf("Artifacts and provenance"),
  );
  await expect(page.getByText("100.0%", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("1 ratio", { exact: true })).toHaveCount(0);
});

test("desktop benchmark selector has a visible keyboard focus indicator", async ({
  page,
}) => {
  const selector = page.locator(".rail-benchmark-select select");
  await selector.focus();
  await expect(selector).toBeFocused();
  const focusStyle = await selector.evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      outlineStyle: style.outlineStyle,
      outlineWidth: style.outlineWidth,
      outlineColor: style.outlineColor,
    };
  });
  expect(focusStyle.outlineStyle).not.toBe("none");
  expect(Number.parseFloat(focusStyle.outlineWidth)).toBeGreaterThanOrEqual(2);
  expect(focusStyle.outlineColor).not.toBe("rgba(0, 0, 0, 0)");
});

test("responsive navigation and small metadata meet contrast requirements", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await expect(page.locator(".recent-runs-navigation")).toBeHidden();
  await page.setViewportSize({ width: 1000, height: 800 });
  await expect(page.locator(".recent-runs-navigation")).toBeVisible();
  const colors = await page.evaluate(() => {
    const effectiveBackground = (element: Element) => {
      let current: Element | null = element;
      while (current) {
        const color = getComputedStyle(current).backgroundColor;
        const channels = color.match(/[\d.]+/g)?.map(Number) ?? [];
        if (channels.length === 3 || (channels[3] ?? 0) > 0) return color;
        current = current.parentElement;
      }
      return "rgb(255, 255, 255)";
    };
    const values = [
      document.querySelector(".recent-runs-navigation > header button")!,
      document.querySelector(".recent-runs-navigation .recent-run-status")!,
      document.querySelector(".kpi-card small")!,
    ];
    return values.map((element) => ({
      foreground: getComputedStyle(element).color,
      background: effectiveBackground(element),
    }));
  });
  const luminance = (color: string) => {
    const channels = (color.match(/[\d.]+/g)?.slice(0, 3).map(Number) ?? [])
      .map((channel) => channel / 255)
      .map((channel) =>
        channel <= 0.03928
          ? channel / 12.92
          : ((channel + 0.055) / 1.055) ** 2.4,
      );
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
  };
  const contrast = (foreground: string, background: string) => {
    const lighter = Math.max(luminance(foreground), luminance(background));
    const darker = Math.min(luminance(foreground), luminance(background));
    return (lighter + 0.05) / (darker + 0.05);
  };
  for (const { foreground, background } of colors) {
    expect(contrast(foreground, background)).toBeGreaterThanOrEqual(4.5);
  }
  const brand = page.getByRole("button", { name: /AI Bench/i }).first();
  await brand.focus();
  const brandFocusColors = await brand.evaluate((element) => {
    return {
      outline: getComputedStyle(element).outlineColor,
      background: getComputedStyle(document.querySelector(".navigation")!)
        .backgroundColor,
    };
  });
  expect(
    contrast(brandFocusColors.outline, brandFocusColors.background),
  ).toBeGreaterThanOrEqual(3);

  await page.unroute("**/api/**");
  await mockApi(page, false, false);
  await page.goto("/");
  const emptyHistory = page.locator(".recent-runs-navigation .recent-runs-empty");
  await expect(emptyHistory).toBeVisible();
  const emptyHistoryColors = await emptyHistory.evaluate((element) => ({
    foreground: getComputedStyle(element).color,
    background: getComputedStyle(element.closest(".navigation")!).backgroundColor,
  }));
  expect(
    contrast(emptyHistoryColors.foreground, emptyHistoryColors.background),
  ).toBeGreaterThanOrEqual(4.5);

  await page.setViewportSize({ width: 390, height: 844 });
  const mobileBrand = page.getByRole("button", { name: /AI Bench/i }).first();
  await mobileBrand.focus();
  const mobileBrandFocusColors = await mobileBrand.evaluate((element) => ({
    outline: getComputedStyle(element).outlineColor,
    background: getComputedStyle(element.closest(".topbar")!).backgroundColor,
  }));
  expect(
    contrast(
      mobileBrandFocusColors.outline,
      mobileBrandFocusColors.background,
    ),
  ).toBeGreaterThanOrEqual(3);

  await page.getByRole("button", { name: "Open navigation" }).click();
  const closeButton = page
    .getByRole("navigation", { name: "Primary navigation" })
    .getByRole("button", { name: "Close navigation" });
  await closeButton.focus();
  const focusColors = await closeButton.evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      outline: style.outlineColor,
      background: getComputedStyle(element.closest(".navigation")!).backgroundColor,
    };
  });
  expect(contrast(focusColors.outline, focusColors.background)).toBeGreaterThanOrEqual(3);

  await closeButton.hover();
  const hoverColors = await closeButton.evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      foreground: style.color,
      background: style.backgroundColor,
    };
  });
  expect(contrast(hoverColors.foreground, hoverColors.background)).toBeGreaterThanOrEqual(4.5);
});

test("all workspace destinations are URL-backed", async ({ page }) => {
  const destinations = [
    ["Home", "home", "AI Bench"],
    ["Benchmarks", "benchmarks", "Benchmarks"],
    ["Create benchmark", "create", "Create benchmark"],
    ["Solutions", "solutions", "Solutions"],
    ["Configure run", "configure", "Configure run"],
    ["Evaluation detail", "evaluation", "Evaluation detail"],
    ["Runs", "runs", "Runs"],
    ["Reports", "reports", "Reports"],
    ["Admin", "admin", "Admin"],
  ] as const;

  for (const [label, view, heading] of destinations) {
    await page
      .getByRole("navigation", { name: "Primary navigation" })
      .getByRole("button", { name: label, exact: true })
      .click();
    if (view === "home") {
      expect(new URL(page.url()).searchParams.get("view")).toBeNull();
    } else {
      expect(new URL(page.url()).searchParams.get("view")).toBe(view);
    }
    await expect(page.getByRole("heading", { name: heading, exact: true }).first()).toBeVisible();
  }
});

test("horizontal ledgers remain clipped and layered at nonzero scroll", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await validateHorizontalLedgers(page, 0);
  await validateHorizontalLedgers(page, 220);
});

test("single-solution comparison keeps fixed sticky tracks aligned", async ({
  page,
}) => {
  await page.unroute("**/api/**");
  await mockApi(page, true);
  await page.setViewportSize({ width: 500, height: 844 });
  await page.goto(`/?view=evaluation&benchmark=bench_general&run=${runId}`);
  await expect(
    page.getByRole("heading", { name: "Solution performance" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Inspect sample-001 / pipeline-a", exact: true }).click();
  const ledger = page.getByRole("region", {
    name: "Reference and solution output comparison, horizontally scrollable",
  });
  await ledger.evaluate((element) => {
    element.scrollLeft = 220;
  });
  const geometry = await ledger.evaluate((element) => {
    const field = element.querySelector<HTMLElement>(
      '[data-sticky-track="field"]',
    )!;
    const reference = element.querySelector<HTMLElement>(
      '[data-sticky-track="reference"]',
    )!;
    const fieldRect = field.getBoundingClientRect();
    const referenceRect = reference.getBoundingClientRect();
    return {
      fieldWidth: fieldRect.width,
      referenceWidth: referenceRect.width,
      overlap: fieldRect.right - referenceRect.left,
      scrollLeft: element.scrollLeft,
    };
  });
  expect(geometry.fieldWidth).toBeCloseTo(104, 0);
  expect(geometry.referenceWidth).toBeCloseTo(158, 0);
  expect(geometry.overlap).toBeLessThanOrEqual(1);
});

test("narrow sticky tracks leave solution output visible at 320px", async ({
  page,
}) => {
  await page.setViewportSize({ width: 320, height: 844 });
  await page.goto(`/?view=evaluation&benchmark=bench_general&run=${runId}`);
  await expect(
    page.getByRole("heading", { name: "Solution performance" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Inspect sample-001 / pipeline-a", exact: true }).click();
  const ledger = page.getByRole("region", {
    name: "Reference and solution output comparison, horizontally scrollable",
  });
  await ledger.evaluate((element) => {
    element.scrollLeft = element.scrollWidth;
  });
  const geometry = await ledger.evaluate((element) => {
    const field = element.querySelector<HTMLElement>(
      '[data-sticky-track="field"]',
    )!;
    const reference = element.querySelector<HTMLElement>(
      '[data-sticky-track="reference"]',
    )!;
    const solution = element.querySelector<HTMLElement>(
      "tbody tr td:last-child",
    )!;
    const containerRect = element.getBoundingClientRect();
    const fieldRect = field.getBoundingClientRect();
    const referenceRect = reference.getBoundingClientRect();
    const solutionRect = solution.getBoundingClientRect();
    const visibleLeft = Math.max(solutionRect.left, referenceRect.right);
    const visibleRight = Math.min(solutionRect.right, containerRect.right);
    return {
      fieldWidth: fieldRect.width,
      referenceWidth: referenceRect.width,
      scrollLeft: element.scrollLeft,
      visibleSolutionWidth: Math.max(0, visibleRight - visibleLeft),
    };
  });
  expect(geometry.fieldWidth).toBeCloseTo(68, 0);
  expect(geometry.referenceWidth).toBeCloseTo(92, 0);
  expect(geometry.scrollLeft).toBeGreaterThan(0);
  expect(geometry.visibleSolutionWidth).toBeGreaterThan(48);
});

test("detail and metric tabs use semantic keyboard navigation", async ({ page }) => {
  const evidence = page.getByRole("tab", { name: /Evidence artifacts/ });
  const manifest = page.getByRole("tab", { name: "Run manifest" });
  await expect(evidence).toHaveAttribute("aria-controls", "detail-panel-evidence");
  await expect(manifest).toHaveAttribute("aria-controls", "detail-panel-manifest");
  await evidence.focus();
  await evidence.press("ArrowRight");
  await expect(manifest).toBeFocused();
  await expect(manifest).toHaveAttribute("aria-selected", "true");
  await expect(
    page.getByRole("tabpanel", { name: "Run manifest" }),
  ).toBeVisible();
  await manifest.press("Home");
  await expect(evidence).toBeFocused();
  await expect(evidence).toHaveAttribute("aria-selected", "true");

  const quality = page.getByRole("tab", { name: "Quality" });
  await quality.focus();
  await quality.press("End");
  const reliability = page.getByRole("tab", { name: "Reliability" });
  await expect(reliability).toBeFocused();
  await expect(reliability).toHaveAttribute("aria-selected", "true");
  await expect(
    page.getByRole("region", {
      name: /reliability evidence ledger/i,
    }),
  ).toBeVisible();
});

test("result comparisons are primary and all-empty non-cost metric columns are omitted", async ({
  page,
}) => {
  const ledger = page.getByRole("region", {
    name: /quality evidence ledger/i,
  });
  await expect(
    ledger.getByRole("button", { name: /comparison · sample-001/i }),
  ).toBeVisible();
  await expect(ledger.getByText(/provider response/i)).toHaveCount(0);

  const table = page.getByRole("region", {
    name: "Solution performance, horizontally scrollable",
  });
  const headers = table.locator("thead th");
  const rows = table.locator("tbody tr");
  const headerCount = await headers.count();
  for (let column = 2; column < headerCount - 1; column += 1) {
    if ((await headers.nth(column).textContent())?.trim() === "Cost") continue;
    const values = await rows.locator(`td:nth-child(${column + 1})`).allTextContents();
    expect(values.some((value) => value.trim() !== "Not available")).toBe(true);
  }
});

test("cost coverage and provenance preserve primary ledgers and report accessibility", async ({ page }) => {
  const sampleCosts = speechSolutionIds.map((id, index) => ({
    ...costFixture.solutions[index === 0 ? 0 : 1], solution_id: id,
  }));
  const totals = { total_attempts: 135, terminal_tasks: 130, total_tasks: 130 };
  const costs: RunCosts = {
    ...costFixture,
    solutions: sampleCosts.map(cost => ({
      ...cost,
      recorded: { ...cost.recorded, status: cost.recorded.status === "known" ? "partial" : cost.recorded.status, total_attempts: 27, terminal_tasks: 26, total_tasks: 26 },
      current_estimate: { ...cost.current_estimate, status: "partial", total_attempts: 27, terminal_tasks: 26, total_tasks: 26 },
    })),
    samples: { [speechSampleIds[0]]: sampleCosts },
    recorded_total: { ...costFixture.recorded_total, ...totals },
    current_estimate_total: { ...costFixture.current_estimate_total, ...totals, amounts: { USD: "1.2" }, covered_attempts: 6 },
  };
  await mockSpeechApi(page, true, true, costs);
  const performance = page.getByRole("region", { name: "Solution performance, horizontally scrollable" });
  await expect(performance.getByRole("button", { name: "Cost (USD)", exact: true })).toBeVisible();
  await expect(performance.getByText("Unknown USD").first()).toBeVisible();
  await expect(performance.getByText("$0.2000 USD").first()).toBeVisible();
  const comparison = page.getByRole("region", { name: "Reference and solution output comparison, horizontally scrollable" });
  await expect(comparison.getByRole("rowheader", { name: "Cost" })).toBeVisible();
  await expect(comparison.getByText("$0.5000 USD")).toBeVisible();
  await page.getByText("Cost totals and pricing", { exact: true }).click();
  await page.getByText("Pricing basis, sources and retry-inclusive usage").click();
  await expect(page.getByText(/Reviewed 2026-01-20/).first()).toBeVisible();
  await expect(page.getByText(/Recorded usage across all attempts: 20 seconds; 2 calls/)).toBeVisible();
  for (const width of [1220, 1100, 900, 640, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await validateHorizontalLedgers(page, 0);
    await validateHorizontalLedgers(page, 220);
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole("navigation", { name: "Primary navigation" })
    .getByRole("button", { name: "Reports", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Cost totals and coverage" })).toBeVisible();
  await expect(page.getByText("Current-rate estimate: $1.2000 USD")).toBeVisible();
  await page.addScriptTag({ content: axe.source });
  const accessibility = await page.evaluate(async () => window.axe.run(document, {
    runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"] },
  }));
  expect(accessibility.violations).toEqual([]);
});

test("evidence drawer traps focus, closes with Escape, and restores focus", async ({
  page,
}) => {
  const trigger = page.getByRole("button", {
    name: /comparison · sample-001/i,
  }).first();
  await trigger.click();
  const drawer = page.getByRole("dialog", {
    name: /comparison · sample-001/i,
  });
  await expect(drawer).toBeVisible();
  const close = drawer.getByRole("button", {
    name: "Close evidence inspector",
  });
  await expect(close).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(drawer.getByRole("link", { name: /download original/i }))
    .toBeFocused();
  await page.keyboard.press("Escape");
  await expect(drawer).toBeHidden();
  await expect(trigger).toBeFocused();
});

test("mobile drawer preserves benchmark context and focus", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator(".topbar-mobile-project")).toHaveText(
    "General capability set",
  );
  const menu = page.getByRole("button", { name: "Open navigation" });
  await menu.click();
  const close = page
    .getByRole("navigation", { name: "Primary navigation" })
    .getByRole("button", { name: "Close navigation" });
  await expect(close).toBeVisible();
  const closeIsTopmost = await close.evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    const hit = document.elementFromPoint(
      bounds.left + bounds.width / 2,
      bounds.top + bounds.height / 2,
    );
    return hit === element || (hit !== null && element.contains(hit));
  });
  expect(closeIsTopmost).toBe(true);
  await page.keyboard.press("Escape");
  await expect(menu).toHaveAttribute("aria-expanded", "false");
  const navigation = page.locator(".navigation");
  await expect(navigation).not.toHaveClass(/mobile-open/);
  await expect(navigation).toBeHidden();
  await expect(close).toBeHidden();
  await expect(menu).toBeFocused();
});

test("speech pilot preserves all ledger bounds at every responsive breakpoint", async ({ page }) => {
  await mockSpeechApi(page);
  await expect(page.locator("audio")).toHaveAttribute("controls", "");
  await expect.poll(() => page.locator("audio").evaluate((audio: HTMLAudioElement) => audio.readyState))
    .toBeGreaterThanOrEqual(1);
  expect(await page.locator("audio").evaluate((audio: HTMLAudioElement) => audio.duration)).toBeCloseTo(0.1);
  await expect(page.getByText(/26 samples \/ 130 tasks/)).toBeVisible();
  for (const width of [1220, 1100, 900, 640, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await validateHorizontalLedgers(page, 0);
    await validateHorizontalLedgers(page, 220);
  }
  expect(new URL(page.url()).searchParams.get("benchmark")).toBe("speech_to_text");
  expect(new URL(page.url()).searchParams.get("run")).toBe(runId);
});

test("speech cooldowns remain operational and keyboard accessible", async ({ page }) => {
  await mockSpeechApi(page);
  await page.getByText("Task status and attempt history", { exact: true }).first().click();
  await expect(page.getByText(/HTTP 429/)).toBeVisible();
  await expect(page.getByText(/delay 9s \(provider-message\)/)).toBeVisible();
  await page.addScriptTag({ content: axe.source });
  const accessibility = await page.evaluate(async () => window.axe.run(document, {
    runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"] },
  }));
  expect(accessibility.violations).toEqual([]);
  const comparison = page.getByRole("region", {
    name: "Reference and solution output comparison, horizontally scrollable",
  });
  await expect(comparison.getByText(/HTTP 429|provider-message/)).toHaveCount(0);
});

test("speech partial coverage is explicit and missing data cannot launch", async ({ page }) => {
  await mockSpeechApi(page, true, false);
  await expect(page.getByText(/Partial or different coverage/)).toBeVisible();
  await expect(page.getByText("1/26 partial")).toHaveCount(2);
  await expect(page.getByText("No complete comparable result")).toBeVisible();
  await page.getByRole("navigation", { name: "Primary navigation" })
    .getByRole("button", { name: "Configure run", exact: true }).click();
  await expect(page.getByText("Dataset: Not configured")).toBeVisible();
  for (const checkbox of await page.locator(".solution-options input").all()) {
    await checkbox.check();
  }
  await page.getByRole("checkbox", { name: /Allow networked execution/ }).check();
  await expect(page.getByText("130", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Launch benchmark" })).toBeDisabled();
});
