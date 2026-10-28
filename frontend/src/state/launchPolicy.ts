import type { BenchmarkManifest, RuntimeInfo } from "../types";

export function launchNetworkPermission(runtime: RuntimeInfo | undefined, localOptIn: boolean): boolean {
  return runtime?.runtime === "azure"
    ? runtime.allow_networked === true
    : runtime?.runtime === "local" && localOptIn;
}

export interface LaunchAvailability {
  benchmark: BenchmarkManifest;
  solutionIds: string[];
  retryLimit: number;
  allowNetworked: boolean;
  launching: boolean;
  datasetBlocked?: boolean;
  datasetBlockReason?: string;
  maxAttempts?: number;
  networkAvailable?: boolean;
  runtimeLoading?: boolean;
  runtime?: RuntimeInfo;
}

export function launchBlockReason({
  benchmark, solutionIds, retryLimit, allowNetworked, launching,
  datasetBlocked, datasetBlockReason, maxAttempts = 20,
  networkAvailable = false, runtimeLoading = false, runtime,
}: LaunchAvailability): string | undefined {
  if (launching) return "Launching this run. Its operation identity is being preserved.";
  if (runtimeLoading) return "Loading runtime settings before launch.";
  if (!runtime) return "Runtime settings are unavailable. Refresh the page before launching.";
  if (!["azure", "local"].includes(runtime.runtime)) return "The runtime mode is not recognized. Launch is unavailable.";
  if (datasetBlocked) return datasetBlockReason ? `Cannot launch: ${datasetBlockReason}` : "The selected dataset is not ready for launch.";
  if (benchmark.dataset_availability && benchmark.dataset_availability.status !== "ready") {
    return `Dataset unavailable: ${benchmark.dataset_availability.reason}`;
  }
  if (!solutionIds.length) return "Select at least one solution before launching.";
  const selected = benchmark.pipelines?.filter(solution => solutionIds.includes(solution.id)) ?? [];
  if (selected.length !== solutionIds.length || selected.some(solution => !solution.compatibility || solution.compatibility.level === "unsupported")) {
    return "The selected configuration includes an unavailable or unsupported solution.";
  }
  if (!Number.isInteger(retryLimit) || retryLimit < 1 || retryLimit > maxAttempts) {
    return `Choose a whole-number attempt limit from 1 to ${maxAttempts}.`;
  }
  if (runtime.runtime === "azure" && (!runtime.worker_running || runtime.worker_error)) {
    return "Hosted execution is not ready. Wait for the worker to recover, then refresh the page.";
  }
  if (selected.some(solution => solution.networked)) {
    if (!networkAvailable) return "Provider-backed launches are disabled by the runtime.";
    if (!allowNetworked) return "Enable networked execution or deselect the networked configuration.";
  }
  return undefined;
}
