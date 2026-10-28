import type { BenchmarkManifest, RuntimeInfo } from "../types";

export function catalogPresentation(
  benchmarks: BenchmarkManifest[],
  runtime?: RuntimeInfo,
): { benchmarks: BenchmarkManifest[]; reason?: string } {
  if (!runtime || !["azure", "local"].includes(runtime.runtime)) {
    return { benchmarks: [], reason: "Runtime settings are unavailable or unrecognized. Refresh the page to reload the catalog." };
  }

  if (runtime.public_demo !== true) return { benchmarks };
  const scope = runtime.landing?.benchmark_id;
  if (typeof scope !== "string" || !scope.trim() || typeof runtime.landing?.run_id !== "string" || !runtime.landing.run_id.trim()) {
    return { benchmarks: [], reason: "The hosted catalog is not configured. Ask the operator to restore its published landing benchmark, then refresh the page." };
  }

  const scoped = benchmarks.filter(benchmark => benchmark.id === scope);
  return scoped.length
    ? { benchmarks: scoped }
    : { benchmarks: [], reason: "The configured hosted benchmark is unavailable. Ask the operator to restore it, then refresh the page. Saved run links remain available." };
}

export function withCatalogNames(
  pinned: BenchmarkManifest,
  catalog: BenchmarkManifest[],
): BenchmarkManifest {
  const installed = catalog.find(item => item.id === pinned.id);
  return {
    ...pinned,
    pipelines: pinned.pipelines?.map(solution => ({
      ...solution,
      name: installed?.pipelines?.find(item => item.id === solution.id)?.name ?? solution.name,
    })),
  };
}
