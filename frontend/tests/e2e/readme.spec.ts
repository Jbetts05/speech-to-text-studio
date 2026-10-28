import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const files = [
  "speech-studio.svg", "speech-studio-mobile.svg",
  "speech-workflow.svg", "speech-workflow-mobile.svg",
  "speech-studio-static.svg", "speech-studio-mobile-static.svg",
  "speech-workflow-static.svg", "speech-workflow-mobile-static.svg",
] as const;
const sources = new Map(files.map(name => [name, readFileSync(resolve(root, "docs", "assets", name), "utf8")]));
const readme = readFileSync(resolve(root, "README.md"), "utf8");

async function openSvg(page: Page, name: typeof files[number], width: number) {
  await page.setViewportSize({ width, height: 1000 });
  await page.goto("data:image/svg+xml;charset=utf-8," + encodeURIComponent(sources.get(name)!));
  await page.evaluate(width => {
    const svg = document.documentElement;
    if (!(svg instanceof SVGSVGElement)) throw new Error("Expected an SVG document");
    const ratio = svg.viewBox.baseVal.height / svg.viewBox.baseVal.width;
    svg.setAttribute("width", String(width));
    svg.setAttribute("height", String(width * ratio));
  }, width);
  await page.evaluate(() => document.fonts.ready.then(() => undefined));
  // Start the finite sequence after fonts are ready, independently of machine load.
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.evaluate(() => {
    for (const animation of document.getAnimations()) animation.pause();
  });
}

async function assertTextBounds(page: Page) {
  const result = await page.evaluate(() => {
    const svg = document.documentElement;
    if (!(svg instanceof SVGSVGElement)) throw new Error("Expected SVG");
    const scale = svg.getBoundingClientRect().width / svg.viewBox.baseVal.width;
    const text = [...document.querySelectorAll<SVGTextElement>("text")];
    const failures: string[] = [];
    const rects = text.map(node => {
      const label = node.textContent?.trim() ?? "";
      const box = document.getElementById(node.dataset.box ?? "");
      const rect = node.getBoundingClientRect();
      if (!box || !label || rect.width <= 0 || rect.height <= 0) {
        failures.push(`Missing visible label or bounding box: ${label}`);
      } else {
        const bounds = box.getBoundingClientRect();
        const inset = Number(node.dataset.inset ?? 0) * scale;
        if (
          rect.left < bounds.left + inset - 0.5 ||
          rect.right > bounds.right - inset + 0.5 ||
          rect.top < bounds.top + inset - 0.5 ||
          rect.bottom > bounds.bottom - inset + 0.5
        ) {
          failures.push(`Label exceeds padded box: ${label} ${JSON.stringify({
            text: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
            box: { x: bounds.x, y: bounds.y, width: bounds.width, height: bounds.height },
            inset,
          })}`);
        }
        if (getComputedStyle(node).opacity === "0" || getComputedStyle(node).visibility !== "visible") {
          failures.push(`Essential text is hidden: ${label}`);
        }
      }
      return { label, rect };
    });
    for (let i = 0; i < rects.length; i++) {
      for (let j = i + 1; j < rects.length; j++) {
        const a = rects[i], b = rects[j];
        const overlapX = Math.min(a.rect.right, b.rect.right) - Math.max(a.rect.left, b.rect.left);
        const overlapY = Math.min(a.rect.bottom, b.rect.bottom) - Math.max(a.rect.top, b.rect.top);
        if (overlapX > 0.5 && overlapY > 0.5) failures.push(`Overlapping labels: ${a.label} / ${b.label}`);
      }
    }
    return { count: text.length, failures };
  });
  expect(result.count).toBeGreaterThan(8);
  expect(result.failures).toEqual([]);
}

for (const name of files) {
  for (const width of (name.includes("-mobile") ? [288, 343, 608] : [609, 736, 1012])) {
    test(`${name} labels fit at ${width}px`, async ({ page }) => {
      await openSvg(page, name, width);
      for (const time of [0, 1500, 3000, 6000, 9500]) {
        await page.evaluate(time => {
          for (const animation of document.getAnimations()) {
            animation.pause();
            animation.currentTime = time;
          }
        }, time);
        await assertTextBounds(page);
      }
      await page.evaluate(() => {
        for (const node of document.querySelectorAll<SVGTextElement>("text")) {
          node.style.fontFamily = node.classList.contains("mono") ? '"Courier New", monospace' : "Arial, sans-serif";
        }
      });
      await assertTextBounds(page);
    });
  }

  if (name.includes("-static")) {
    test(`${name} is a complete animation-free equivalent`, async ({ page }) => {
      await openSvg(page, name, name.includes("-mobile") ? 343 : 1012);
      expect(await page.evaluate(() => document.getAnimations().length)).toBe(0);
      const animated = files.find(file => file === name.replace("-static", ""));
      if (!animated) throw new Error("Static SVG has no animated counterpart");
      const equivalent = await page.evaluate(source => {
        const original = new DOMParser().parseFromString(source, "image/svg+xml");
        const fields = ["textContent", "x", "y", "width", "height", "data-box", "data-inset"];
        const signature = (doc: Document) => [...doc.querySelectorAll("text, rect[id]")].map(node =>
          fields.map(field => field === "textContent" ? node.textContent?.trim() : node.getAttribute(field)));
        return JSON.stringify(signature(document)) === JSON.stringify(signature(original));
      }, sources.get(animated)!);
      expect(equivalent).toBe(true);
      expect(sources.get(name)).not.toMatch(/@keyframes|animation:|<animate/);
      await assertTextBounds(page);
    });
    continue;
  }

  test(`${name} motion is optional and accessible`, async ({ page }, testInfo) => {
    const width = name.includes("-mobile") ? 343 : 1012;
    await page.emulateMedia({ reducedMotion: "no-preference" });
    await openSvg(page, name, width);
    const accessibility = await page.evaluate(() => {
      const svg = document.documentElement;
      return {
        role: svg.getAttribute("role"),
        labels: (svg.getAttribute("aria-labelledby") ?? "").split(" ").map(id => document.getElementById(id)?.textContent?.trim()),
        forbidden: document.querySelectorAll("script, foreignObject, image, a").length,
        externalReferences: [...document.querySelectorAll("[href]")].filter(node => !node.getAttribute("href")?.startsWith("#")).length,
        eventHandlers: [...document.querySelectorAll("*")].flatMap(node => [...node.attributes]).filter(attribute => /^on/i.test(attribute.name)).length,
      };
    });
    expect(accessibility.role).toBe("img");
    expect(accessibility.labels).toHaveLength(2);
    expect(accessibility.labels.every(value => value && value.length > 20)).toBe(true);
    expect(accessibility.forbidden + accessibility.externalReferences + accessibility.eventHandlers).toBe(0);
    expect(await page.evaluate(() => document.getAnimations().length)).toBeGreaterThan(0);
    const endTimes = await page.evaluate(() => document.getAnimations().map(animation => {
      if (!animation.effect) throw new Error("Missing motion effect");
      return animation.effect.getComputedTiming().endTime;
    }));
    expect(endTimes.every(time => typeof time === "number" && time <= 5000)).toBe(true);
    const states: number[][] = [];
    for (const time of [0, 3000]) {
      states.push(await page.evaluate(time => {
        for (const animation of document.getAnimations()) {
          animation.pause();
          animation.currentTime = time;
        }
        return [...document.querySelectorAll(".selection, .progress")].map(node => Number(getComputedStyle(node).opacity));
      }, time));
    }
    expect(states[0]).not.toEqual(states[1]);
    expect(states.every(state => state.filter(opacity => opacity > 0.1).length <= 1)).toBe(true);
    await assertTextBounds(page);
    await testInfo.attach("animated-frame", { body: await page.locator("svg").screenshot(), contentType: "image/png" });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await expect.poll(() => page.evaluate(() => document.getAnimations().length)).toBe(0);
    await assertTextBounds(page);
    await testInfo.attach("reduced-motion", { body: await page.locator("svg").screenshot(), contentType: "image/png" });
    await page.emulateMedia({ reducedMotion: "no-preference" });
    await page.evaluate(() => {
      const style = document.createElementNS("http://www.w3.org/2000/svg", "style");
      style.textContent = "* { animation: none !important; }";
      document.documentElement.append(style);
    });
    expect(await page.evaluate(() => document.getAnimations().length)).toBe(0);
    await assertTextBounds(page);
  });
}

test("README uses existing responsive SVGs and descriptive alternatives", async () => {
  const pictures = [...readme.matchAll(/<picture>([\s\S]*?)<\/picture>/g)];
  expect(pictures).toHaveLength(2);
  const references = [...readme.matchAll(/(?:src|srcset)="([^"]+)"/g)].map(match => match[1]);
  expect(references).toHaveLength(8);
  for (const reference of references) {
    expect(reference).toMatch(/^docs\/assets\/speech-[a-z-]+\.svg$/);
    expect(readFileSync(resolve(root, reference), "utf8")).toContain("<svg");
  }
  for (const [, picture] of pictures) {
    const media = [...picture.matchAll(/<source media="([^"]+)"/g)].map(match => match[1]);
    expect(media).toEqual([
      "(max-width: 640px) and (prefers-reduced-motion: reduce)",
      "(prefers-reduced-motion: reduce)",
      "(max-width: 640px)",
    ]);
    expect(picture).toContain('media="(max-width: 640px)"');
    expect(picture).toMatch(/srcset="[^"]+-mobile\.svg"/);
    expect(picture).toMatch(/srcset="[^"]+-mobile-static\.svg"/);
    expect(picture).toMatch(/srcset="[^"]+(?<!-mobile)-static\.svg"/);
    expect(picture.match(/alt="([^"]+)"/)?.[1].length).toBeGreaterThan(80);
    expect(picture).toContain('width="100%"');
  }
  expect(readme).not.toContain("azurewebsites.net");
  expect(readme).toContain("[Run locally](#run-locally)");
  expect(readme).toContain("[MIT license](LICENSE)");
  expect(readme).toContain("ten days after publication");
  expect(readme).toContain("exactly-once billing are not guaranteed");
});
