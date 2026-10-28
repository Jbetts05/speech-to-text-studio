import { expect, test, type Page } from "@playwright/test";
import axe from "axe-core";
import { benchmarkWireFixture } from "../../src/testing/fixtures";
import { serviceCatalogFixture, serviceDataset, serviceNames, serviceRuntime } from "../../src/testing/serviceFixtures";

async function catalog(page: Page, local = false) {
  const writes: string[] = [];
  await page.route("**/api/**", async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() !== "GET") {
      writes.push(path);
      await route.abort();
      return;
    }
    const responses: Record<string, unknown> = {
      "/api/benchmarks": [
        { ...benchmarkWireFixture, definition: { ...benchmarkWireFixture.definition, name: "Synthetic text intent" } },
        serviceCatalogFixture,
      ],
      "/api/runtime": local ? { ...serviceRuntime, runtime: "local", public_demo: false, landing: null } : serviceRuntime,
      "/api/runs": [],
      [`/api/datasets/${serviceDataset.id}`]: serviceDataset,
    };
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(responses[path] ?? {}) });
  });
  return writes;
}

for (const width of [1440, 768, 375]) {
  test(`simple service selection is accessible without page overflow at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const writes = await catalog(page);
    await page.goto("/?view=configure");
    const list = page.getByRole("list", { name: "Services" });
    await expect(list.getByRole("checkbox")).toHaveCount(5);
    for (const name of Object.values(serviceNames)) await expect(list.getByRole("checkbox", { name, exact: true })).toBeChecked();
    await expect(list).not.toContainText(/networked|standalone|comparable|adapter|compatibility/i);
    await expect(page.getByRole("button", { name: "Launch benchmark" })).toBeEnabled();
    await expect(page.getByRole("checkbox", { name: /Allow networked execution/ })).toHaveCount(0);
    const first = list.getByRole("checkbox").first();
    await first.focus();
    await page.keyboard.press("Space");
    await expect(first).not.toBeChecked();
    await expect(first).toBeFocused();
    const outline = await first.locator("..").evaluate(element => getComputedStyle(element).outlineStyle);
    expect(outline).not.toBe("none");
    await page.keyboard.press("Tab");
    await expect(list.getByRole("checkbox").nth(1)).toBeFocused();
    const fits = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
    expect(fits).toBe(true);
    if (width < 900) {
      const open = page.getByRole("button", { name: "Open navigation" });
      await open.click();
      await expect(page.getByRole("option", { name: "Synthetic text intent" })).toHaveCount(0);
      await page.keyboard.press("Escape");
      await expect(open).toBeFocused();
    }
    await expect(page.locator(".view-shell")).toHaveCSS("opacity", "1");
    await page.addScriptTag({ content: axe.source });
    expect(await page.evaluate(async () => (await window.axe.run(document.body)).violations)).toEqual([]);
    await page.screenshot({ path: testInfo.outputPath(`services-${width}.png`), fullPage: true });
    await page.goto("/?view=solutions");
    await expect(page.getByRole("list", { name: "Services" }).getByRole("listitem")).toHaveCount(5);
    await expect(page.getByRole("checkbox")).toHaveCount(0);
    await expect(page.getByText("Synthetic text intent", { exact: true })).toHaveCount(0);
    await page.screenshot({ path: testInfo.outputPath(`solutions-${width}.png`), fullPage: true });
    expect(writes).toEqual([]);
  });
}

test("local catalog retains the developer fixture and explicit opt-in", async ({ page }) => {
  const writes = await catalog(page, true);
  await page.goto("/?view=configure");
  await expect(page.getByRole("combobox", { name: "Current benchmark", exact: true })).toHaveValue(benchmarkWireFixture.definition.id);
  await expect(page.getByRole("checkbox", { name: /Allow networked execution/ })).not.toBeChecked();
  await page.getByRole("combobox", { name: "Current benchmark", exact: true }).selectOption(serviceCatalogFixture.definition.id);
  await expect(page.getByRole("list", { name: "Services" }).getByRole("checkbox")).toHaveCount(5);
  await expect(page.getByRole("button", { name: "Launch benchmark" })).toBeDisabled();
  await page.getByRole("checkbox", { name: /Allow networked execution/ }).check();
  await expect(page.getByRole("button", { name: "Launch benchmark" })).toBeEnabled();
  expect(writes).toEqual([]);
});
