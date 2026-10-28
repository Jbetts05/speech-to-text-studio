import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { ApiError, HttpApiClient, type ApiClient } from "./api/client";
import { AppShell } from "./components/AppShell";
import { EvaluationWorkspace } from "./components/EvaluationWorkspace";
import { EvidenceDrawer } from "./components/EvidenceDrawer";
import { DatasetWorkspace } from "./components/DatasetWorkspace";
import { artifactExpiredEvent } from "./api/expiry";
import {
  AdminWorkspace,
  BenchmarksWorkspace,
  ConfigureRunWorkspace,
  CreateBenchmarkWorkspace,
  HomeWorkspace,
  ReportsWorkspace,
  RunsWorkspace,
  SolutionsWorkspace,
  runLaunchInput,
} from "./components/WorkspacePages";
import { IdempotentLaunchLock } from "./state/launchLock";
import { launchBlockReason, launchNetworkPermission } from "./state/launchPolicy";
import { catalogPresentation, withCatalogNames } from "./state/catalogPresentation";
import { serviceCompatibilityReason } from "./components/ServiceList";
import type {
  BenchmarkManifest,
  Dataset,
  Evidence,
  Run,
  RunReport,
  RuntimeInfo,
  WorkspaceView,
} from "./types";

const routeViews = new Set<WorkspaceView>([
  "home",
  "benchmarks",
  "create",
  "solutions",
  "configure",
  "evaluation",
  "runs",
  "reports",
  "admin",
]);
const defaultApi = new HttpApiClient();

function routeState() {
  const params = new URLSearchParams(window.location.search);
  const candidate = params.get("view") as WorkspaceView | null;
  const runId = params.get("run") ?? undefined;
  return {
    view:
      candidate && routeViews.has(candidate)
        ? candidate
        : runId
          ? "evaluation"
          : "home",
    benchmarkId: params.get("benchmark") ?? undefined,
    runId,
    datasetId: params.get("dataset") ?? undefined,
    explicit: ["view", "benchmark", "run", "dataset"].some((key) => params.has(key)),
  };
}

function writeRoute(
  view: WorkspaceView,
  benchmarkId: string | undefined,
  runId: string | undefined,
  replace = false,
  datasetId = routeState().datasetId,
) {
  const url = new URL(window.location.href);
  if (view === "home") url.searchParams.delete("view");
  else url.searchParams.set("view", view);
  if (benchmarkId) url.searchParams.set("benchmark", benchmarkId);
  else url.searchParams.delete("benchmark");
  if (runId) url.searchParams.set("run", runId);
  else url.searchParams.delete("run");
  if (datasetId) url.searchParams.set("dataset", datasetId);
  else url.searchParams.delete("dataset");
  const method = replace ? "replaceState" : "pushState";
  window.history[method]({}, "", `${url.pathname}${url.search}${url.hash}`);
}

function isTerminal(status: Run["status"]): boolean {
  return ["completed", "completed-with-failures", "failed"].includes(status);
}

function mergeRunSummaries(
  current: Run[],
  summaries: Run[],
  preserveRunId?: string,
): Run[] {
  const merged = summaries.map((summary) => {
    const cached = current.find((item) => item.id === summary.id);
    if (!cached) return summary;
    const preserveTerminal =
      isTerminal(cached.status) && !isTerminal(summary.status);
    return {
      ...cached,
      ...summary,
      status: preserveTerminal ? cached.status : summary.status,
      manifest_artifact_id:
        summary.manifest_artifact_id ?? cached.manifest_artifact_id,
      report_artifact_id:
        preserveTerminal
          ? cached.report_artifact_id ?? summary.report_artifact_id
          : summary.report_artifact_id ?? cached.report_artifact_id,
      solution_ids:
        cached.solution_ids.length > 0
          ? cached.solution_ids
          : summary.solution_ids,
      tasks: cached.tasks,
      metrics: cached.metrics,
      evidence: cached.evidence,
      metric_definitions: cached.metric_definitions,
      quality_comparability: cached.quality_comparability,
    };
  });
  const preserved = preserveRunId
    ? current.find(
        (item) =>
          item.id === preserveRunId &&
          !summaries.some((summary) => summary.id === preserveRunId),
      )
    : undefined;
  return preserved ? [...merged, preserved] : merged;
}

export function App({
  api = defaultApi,
}: {
  api?: ApiClient;
}) {
  const initialRoute = useRef(routeState());
  const [benchmarks, setBenchmarks] = useState<BenchmarkManifest[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [runtime, setRuntime] = useState<RuntimeInfo>();
  const [benchmarkId, setBenchmarkId] = useState(
    initialRoute.current.benchmarkId ?? "",
  );
  const [selectedRunId, setSelectedRunId] = useState(
    initialRoute.current.runId ?? "",
  );
  const [run, setRun] = useState<Run>();
  const [report, setReport] = useState<RunReport>();
  const [datasetId, setDatasetId] = useState(initialRoute.current.datasetId ?? "");
  const [dataset, setDataset] = useState<Dataset>();
  const [datasetError, setDatasetError] = useState<string>();
  const [datasetStatus, setDatasetStatus] = useState<"idle" | "loading" | "ready" | "error" | "expired">("idle");
  const [datasetReload, setDatasetReload] = useState(0);
  const datasetRequestRef = useRef(0);
  const [unavailableRun, setUnavailableRun] = useState<{ id: string; reason: "expired" | "missing" }>();
  const [eventCount, setEventCount] = useState(0);
  const [view, setView] = useState<WorkspaceView>(initialRoute.current.view);
  const [solutionIds, setSolutionIds] = useState<string[]>([]);
  const [retryLimit, setRetryLimit] = useState(2);
  const [allowNetworked, setAllowNetworked] = useState(false);
  const [launching, setLaunching] = useState(false);
  const launchingRef = useRef(false);
  const [bootstrapping, setBootstrapping] = useState(true);
  const [bootstrapError, setBootstrapError] = useState<string>();
  const [catalogError, setCatalogError] = useState<string>();
  const [launchError, setLaunchError] = useState<string>();
  const [error, setError] = useState<string>();
  const [selectedEvidence, setSelectedEvidence] = useState<Evidence>();
  const restoreFocusRef = useRef<HTMLButtonElement | undefined>(undefined);
  const hydratedBenchmarkRef = useRef<string | undefined>(undefined);
  const solutionBenchmarkRef = useRef<string | undefined>(undefined);
  const selectedRunIdRef = useRef(selectedRunId);
  const eventProgressRef = useRef({ runId: "", after: 0, count: 0 });
  const launchLock = useMemo(() => {
    try {
      return new IdempotentLaunchLock(api, undefined, runtime?.public_demo ? sessionStorage : undefined);
    } catch {
      return new IdempotentLaunchLock(api, () => {
        throw new Error("Session storage is unavailable. Enable it before launching so an interrupted public operation can be retried safely.");
      });
    }
  }, [api, runtime?.public_demo]);
  selectedRunIdRef.current = selectedRunId;

  const catalogBenchmark =
    benchmarks.find((benchmark) => benchmark.id === benchmarkId) ??
    (benchmarkId ? undefined : benchmarks[0]);
  const catalogRuns = runtime && ["azure", "local"].includes(runtime.runtime) && runtime.public_demo !== true
    ? runs
    : runs.filter(item => benchmarks.some(benchmark => benchmark.id === item.benchmark_id));
  const pinnedRunBenchmark =
    run?.id === selectedRunId ? run.pinned_benchmark : undefined;
  const selectedBenchmark =
    (view === "evaluation" || view === "reports" || view === "runs") &&
    pinnedRunBenchmark
      ? pinnedRunBenchmark
      : catalogBenchmark;
  const hostedDatasetRequired = runtime?.public_demo === true || runtime?.runtime === "azure";
  const effectiveDatasetId = datasetId || (hostedDatasetRequired ? catalogBenchmark?.default_dataset_id ?? "" : "");
  const effectiveDatasetIdRef = useRef(effectiveDatasetId);
  effectiveDatasetIdRef.current = effectiveDatasetId;
  const selectedDataset = datasetStatus === "ready" && dataset?.id === effectiveDatasetId && dataset.benchmark_id === catalogBenchmark?.id && !datasetError ? dataset : undefined;
  const networkPermission = launchNetworkPermission(runtime, allowNetworked);
  const datasetBlocked = bootstrapping || !catalogBenchmark || Boolean((effectiveDatasetId || hostedDatasetRequired) && !selectedDataset);
  const datasetBlockReason = datasetStatus === "loading"
    ? "Loading the selected dataset and its evidence before launch."
    : datasetError ?? (datasetBlocked ? "Select a ready published dataset before launching." : undefined);
  const reloadSelectedDataset = useCallback(() => {
    datasetRequestRef.current++;
    setDataset(undefined);
    setDatasetError(undefined);
    setDatasetStatus("loading");
    setDatasetReload(value => value + 1);
  }, []);
  const configureBenchmark = selectedDataset && catalogBenchmark
    ? { ...catalogBenchmark,
        sample_count: selectedDataset.sample_count,
        scored_sample_count: selectedDataset.scored_sample_count,
        dataset_availability: { status: "ready" as const, reason: "Selected immutable published dataset.",
          revision: selectedDataset.revision, sample_count: selectedDataset.sample_count, scored_sample_count: selectedDataset.scored_sample_count },
      }
    : selectedBenchmark;

  const expireRun = useCallback((id: string, reason: "expired" | "missing" = "expired") => {
    if (selectedRunIdRef.current !== id) return;
    setUnavailableRun({ id, reason });
    setRun(undefined);
    setReport(undefined);
    setSelectedEvidence(undefined);
    setRuns((current) => current.filter((item) => item.id !== id));
    eventProgressRef.current = { runId: id, after: 0, count: 0 };
    setEventCount(0);
    setError(undefined);
  }, []);
  const loadDiagnostics = useCallback((id: string, signal: AbortSignal) => api.getRun(id, signal), [api]);

  useEffect(() => {
    const expiredArtifact = (event: Event) => {
      const url = (event as CustomEvent<{ url: string }>).detail?.url;
      const belongsTo = (evidence: Evidence) => evidence.artifact_sha256 && api.artifactUrl(evidence.artifact_sha256) === url;
      const runEvidence = [...(run?.evidence ?? []), ...Object.values(run?.sample_artifacts ?? {}).flat(), ...(report?.evidence ?? [])];
      if (selectedRunId && ["evaluation", "reports"].includes(view) && runEvidence.some(belongsTo)) expireRun(selectedRunId);
      else if (effectiveDatasetId && Object.values(dataset?.sample_artifacts ?? {}).flat().some((item) => api.artifactUrl(item.artifact_id) === url)) {
        setDataset(undefined);
        setDatasetStatus("expired");
        setSelectedEvidence(undefined);
        setDatasetError("Dataset expired. Select another dataset; its artifacts are no longer available.");
      }
    };
    window.addEventListener(artifactExpiredEvent, expiredArtifact);
    return () => window.removeEventListener(artifactExpiredEvent, expiredArtifact);
  }, [api, dataset, effectiveDatasetId, selectedRunId, view, run, report, expireRun]);

  useEffect(() => {
    const request = ++datasetRequestRef.current;
    setDataset(undefined);
    setDatasetError(undefined);
    setDatasetStatus("idle");
    if (bootstrapping) return;
    if (!effectiveDatasetId) {
      if (hostedDatasetRequired) {
        setDatasetStatus("error");
        setDatasetError("No published default dataset is available. Select a published dataset before launching.");
      }
      return;
    }
    const abort = new AbortController();
    if (!api.getDataset) {
      setDatasetStatus("error");
      setDatasetError("Dataset selection is unavailable from this API.");
      return;
    }
    setDatasetStatus("loading");
    void api.getDataset(effectiveDatasetId, abort.signal).then((next) => {
      if (abort.signal.aborted || request !== datasetRequestRef.current || effectiveDatasetIdRef.current !== effectiveDatasetId) return;
      if (next.id !== effectiveDatasetId) throw new Error("The dataset response does not match the selected revision. Retry its details.");
      if (next.benchmark_id !== catalogBenchmark?.id) {
        if (initialRoute.current.datasetId === datasetId && !initialRoute.current.benchmarkId && !initialRoute.current.runId && benchmarks.some(item => item.id === next.benchmark_id)) {
          setBenchmarkId(next.benchmark_id);
          writeRoute(routeState().view, next.benchmark_id, undefined, true);
          return;
        }
        throw new Error("This dataset belongs to a different benchmark. Select a dataset for the current benchmark.");
      }
      if (!next.pinned && next.expires_at && Date.parse(next.expires_at) <= (api.serverNow?.() ?? Date.now())) {
        throw new ApiError("Dataset expired.", 410);
      }
      setDataset(next);
      setDatasetStatus("ready");
    }).catch((reason: unknown) => {
      if (abort.signal.aborted || request !== datasetRequestRef.current || effectiveDatasetIdRef.current !== effectiveDatasetId) return;
      const expired = reason instanceof ApiError && reason.status === 410;
      setDatasetStatus(expired ? "expired" : "error");
      setDatasetError(expired
        ? "Dataset expired. Select another dataset; its files and derived results are no longer available."
        : reason instanceof ApiError && reason.status === 404
          ? "Dataset unavailable. Refresh datasets or select another revision."
          : `Unable to load selected dataset. ${reason instanceof Error ? reason.message : "Retry its details."}`);
    });
    return () => abort.abort();
  }, [api, datasetId, effectiveDatasetId, hostedDatasetRequired, catalogBenchmark?.id, bootstrapping, datasetReload, benchmarks]);

  useEffect(() => {
    if (bootstrapping) return;
    const deadlines = [
      ...(run?.expires_at && !run.pinned ? [{ at: run.expires_at, expire: () => expireRun(run.id) }] : []),
      ...(dataset?.expires_at && !dataset.pinned && !datasetError ? [{ at: dataset.expires_at, expire: () => {
        setDataset(undefined); setDatasetStatus("expired"); setSelectedEvidence(undefined); setDatasetError("Dataset expired. Select another dataset.");
      } }] : []),
    ];
    const timers = deadlines.filter(({ at }) => Number.isFinite(Date.parse(at))).map(({ at, expire }) =>
      window.setTimeout(expire, Math.min(2_147_483_647, Math.max(0, Date.parse(at) - (api.serverNow?.() ?? Date.now())))));
    return () => timers.forEach(window.clearTimeout);
  }, [api, run, dataset, datasetError, expireRun, bootstrapping]);

  const refreshRuns = useCallback(async () => {
    try {
      const nextRuns = await api.listRuns();
      setRuns((current) =>
        mergeRunSummaries(
          current,
          nextRuns,
          selectedRunIdRef.current || undefined,
        ),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to refresh runs.");
    }
  }, [api]);

  useEffect(() => {
    const controller = new AbortController();
    void Promise.all([
      api.listBenchmarks(controller.signal),
      api.listRuns(controller.signal),
      api.getRuntime(controller.signal),
    ])
      .then(([nextBenchmarks, nextRuns, nextRuntime]) => {
        const catalog = catalogPresentation(nextBenchmarks, nextRuntime);
        let currentRoute = routeState();
        if (!currentRoute.explicit && !catalog.reason && nextRuntime.landing?.run_id && catalog.benchmarks.some(item => item.id === nextRuntime.landing?.benchmark_id)) {
          currentRoute = { ...currentRoute, view: "evaluation", benchmarkId: nextRuntime.landing.benchmark_id, runId: nextRuntime.landing.run_id };
          setView("evaluation");
          setSelectedRunId(currentRoute.runId!);
        }
        setBootstrapError(undefined);
        setCatalogError(catalog.reason);
        setBenchmarks(catalog.benchmarks);
        setRuns((current) =>
          mergeRunSummaries(current, nextRuns, currentRoute.runId),
        );
        setRuntime(nextRuntime);
        setRetryLimit((value) => Math.min(value, nextRuntime.max_attempts ?? 20));
        const desiredBenchmark = currentRoute.benchmarkId;
        const runBenchmark = currentRoute.runId
          ? nextRuns.find((item) => item.id === currentRoute.runId)
              ?.benchmark_id ?? hydratedBenchmarkRef.current
          : undefined;
        const historicalBenchmark =
          currentRoute.view === "runs" &&
          desiredBenchmark &&
          nextRuns.some((item) => item.benchmark_id === desiredBenchmark)
            ? desiredBenchmark
            : undefined;
        const requestedCatalogBenchmark =
          ["azure", "local"].includes(nextRuntime.runtime) && nextRuntime.public_demo !== true
            ? catalog.benchmarks.find(item => item.id === desiredBenchmark)?.id
            : desiredBenchmark;
        const activeBenchmark =
          runBenchmark
            ? runBenchmark
            : historicalBenchmark
              ? historicalBenchmark
            : requestedCatalogBenchmark ?? catalog.benchmarks[0]?.id;
        setBenchmarkId(activeBenchmark ?? "");
        if (!activeBenchmark) {
          if (currentRoute.view === "runs" && nextRuns.length > 0) {
            writeRoute("runs", undefined, currentRoute.runId, true);
          }
          return;
        }
        writeRoute(
          currentRoute.view,
          activeBenchmark,
          currentRoute.runId,
          true,
        );
      })
      .catch((reason: unknown) => {
        if (!(reason instanceof DOMException && reason.name === "AbortError")) {
          setBootstrapError(
            reason instanceof Error ? reason.message : "Unable to load AI Bench.",
          );
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setBootstrapping(false);
      });
    return () => controller.abort();
  }, [api]);

  useEffect(() => {
    const onPopState = () => {
      const route = routeState();
      setView(route.view);
      setBenchmarkId(route.benchmarkId ?? benchmarks[0]?.id ?? "");
      setSelectedRunId(route.runId ?? "");
      setDatasetId(route.datasetId ?? "");
      setRun((current) => (current?.id === route.runId ? current : undefined));
      setReport((current) =>
        current?.run_id === route.runId ? current : undefined,
      );
      if (route.runId !== selectedRunId) setEventCount(0);
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [benchmarks, selectedRunId]);

  useEffect(() => {
    if (selectedRunId && unavailableRun?.id === selectedRunId) return;
    if (!selectedRunId) {
      eventProgressRef.current = { runId: "", after: 0, count: 0 };
      setRun(undefined);
      setReport(undefined);
      setEventCount(0);
      return;
    }
    let cancelled = false;
    let timer: number | undefined;
    let failureCount = 0;
    let liveReason: string | undefined;
    let finalizationPending = false;
    const controller = new AbortController();
    setRun((current) => (current?.id === selectedRunId ? current : undefined));
    setReport((current) =>
      current?.run_id === selectedRunId ? current : undefined,
    );
    if (eventProgressRef.current.runId !== selectedRunId) {
      eventProgressRef.current = {
        runId: selectedRunId,
        after: 0,
        count: 0,
      };
    }
    setEventCount(eventProgressRef.current.count);

    const schedule = (afterFailure: boolean) => {
      if (cancelled) return;
      const delay = afterFailure
        ? Math.min(1200 * 2 ** Math.min(failureCount++, 3), 9600)
        : 1200;
      if (!afterFailure) failureCount = 0;
      timer = window.setTimeout(refresh, delay);
    };

    const stopIfUnavailable = (reason: unknown) => {
      if (cancelled || selectedRunIdRef.current !== selectedRunId || !(reason instanceof ApiError) || ![404, 410].includes(reason.status ?? 0)) return false;
      cancelled = true;
      controller.abort();
      if (timer !== undefined) window.clearTimeout(timer);
      expireRun(selectedRunId, reason.status === 410 ? "expired" : "missing");
      return true;
    };
    const rethrowAfterCheckingAvailability = (reason: unknown): never => {
      stopIfUnavailable(reason);
      throw reason;
    };

    async function refreshEvents() {
      for (let pageCount = 0; pageCount < 10_000; pageCount += 1) {
        const previous = eventProgressRef.current;
        const page = await api.getRunEvents(
          selectedRunId,
          previous.after,
          controller.signal,
        );
        if (
          cancelled ||
          eventProgressRef.current.runId !== selectedRunId
        ) {
          return;
        }
        const freshEvents = page.events.filter(
          (event) => event.sequence > previous.after,
        );
        const nextAfter = Math.max(previous.after, page.next_after);
        const nextProgress = {
          runId: selectedRunId,
          after: nextAfter,
          count: previous.count + freshEvents.length,
        };
        eventProgressRef.current = nextProgress;
        setEventCount(nextProgress.count);
        if (freshEvents.length === 0 || nextAfter === previous.after) return;
      }
      throw new Error("Run event pagination exceeded the safety limit.");
    }

    function applyRun(refreshedRun: Run) {
      hydratedBenchmarkRef.current = refreshedRun.benchmark_id;
      setRun(refreshedRun);
      setRuns((current) => [
        refreshedRun,
        ...current.filter((item) => item.id !== refreshedRun.id),
      ]);
      setBenchmarkId(refreshedRun.benchmark_id);
      const currentRoute = routeState();
      if (currentRoute.benchmarkId !== refreshedRun.benchmark_id || currentRoute.runId !== selectedRunId) {
        writeRoute(currentRoute.view, refreshedRun.benchmark_id, selectedRunId, true);
      }
    }

    async function loadFinalized(): Promise<boolean> {
      const evaluation = await api.getFinalizedEvaluation(selectedRunId, controller.signal);
      if (cancelled || selectedRunIdRef.current !== selectedRunId) return true;
      if (evaluation.record_source === "live_detail_required") {
        finalizationPending = ["run_not_finalized", "report_missing"].includes(evaluation.reason);
        liveReason = evaluation.reason === "run_not_finalized" ? undefined : evaluation.detail;
        return false;
      }
      applyRun(evaluation.run);
      setReport(evaluation.report);
      setError(undefined);
      return true;
    }

    function finalizedFailure(reason: unknown) {
      if (cancelled || selectedRunIdRef.current !== selectedRunId || stopIfUnavailable(reason)) return;
      setError(reason instanceof Error ? `Finalized evaluation unavailable: ${reason.message}` : "Finalized evaluation unavailable.");
      schedule(true);
    }

    async function refresh() {
      try {
        if (await loadFinalized()) return;
      } catch (reason: unknown) {
        finalizedFailure(reason);
        return;
      }
      const [runResult, eventResult] = await Promise.allSettled([
        api.getRun(selectedRunId, controller.signal).catch(rethrowAfterCheckingAvailability),
        refreshEvents().catch(rethrowAfterCheckingAvailability),
      ]);
      if (cancelled || selectedRunIdRef.current !== selectedRunId) return;

      let requestFailed = false;
      let nextRun: Run | undefined;
      if (runResult.status === "fulfilled") {
        const refreshedRun = { ...runResult.value, live_detail_reason: liveReason };
        nextRun = refreshedRun;
        applyRun(refreshedRun);
      } else {
        requestFailed = true;
        setError(
          runResult.reason instanceof Error
            ? runResult.reason.message
            : "Unable to restore run.",
        );
      }

      if (eventResult.status === "rejected") {
        requestFailed = true;
        setError(
          eventResult.reason instanceof Error
            ? eventResult.reason.message
            : "Unable to refresh run events.",
        );
      }

      if (!nextRun) {
        schedule(true);
        return;
      }
      const terminal =
        nextRun.status === "completed" ||
        nextRun.status === "completed-with-failures" ||
        nextRun.status === "failed";
      let reportFailed = false;
      if (terminal) {
        if (finalizationPending) {
          try {
            if (await loadFinalized()) return;
          } catch (reason: unknown) {
            finalizedFailure(reason);
            return;
          }
        }
        try {
          await refreshEvents();
        } catch (reason: unknown) {
          if (cancelled) return;
          if (stopIfUnavailable(reason)) return;
          requestFailed = true;
          setError(
            reason instanceof Error
              ? reason.message
              : "Unable to refresh final run events.",
          );
        }
        try {
          const nextReport = await api.getRunReport(
            selectedRunId,
            controller.signal,
          );
          if (nextReport.run_id !== selectedRunId) {
            throw new Error("Report does not match the selected run.");
          }
          if (!cancelled) setReport(nextReport);
        } catch (reason: unknown) {
          if (cancelled) return;
          if (stopIfUnavailable(reason)) return;
          reportFailed = true;
          setReport(undefined);
          setError(
            reason instanceof Error
              ? `Final report unavailable: ${reason.message}`
              : "Final report unavailable.",
          );
        }
      }

      if (!requestFailed && !reportFailed) setError(undefined);
      if (!terminal || requestFailed || reportFailed) {
        schedule(requestFailed || reportFailed);
      }
    }
    void refresh();
    return () => {
      cancelled = true;
      controller.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [api, selectedRunId, unavailableRun, expireRun]);

  useEffect(() => {
    if (!catalogBenchmark) return;
    const available = new Set(catalogBenchmark.pipelines?.filter(item => !serviceCompatibilityReason(item)).map((item) => item.id));
    if (solutionBenchmarkRef.current !== catalogBenchmark.id) {
      solutionBenchmarkRef.current = catalogBenchmark.id;
      setSolutionIds(catalogBenchmark.pipelines?.filter(item => !serviceCompatibilityReason(item)).map((item) => item.id) ?? []);
      return;
    }
    const retained = solutionIds.filter((id) => available.has(id));
    if (retained.length !== solutionIds.length) {
      setSolutionIds(retained);
    }
  }, [catalogBenchmark, solutionIds]);

  const navigate = (nextView: WorkspaceView) => {
    if (!catalogBenchmark && !["evaluation", "reports", "runs"].includes(nextView)) {
      const nextBenchmarkId = benchmarks[0]?.id;
      setBenchmarkId(nextBenchmarkId ?? "");
      setSelectedRunId("");
      setRun(undefined);
      setReport(undefined);
      setDatasetId("");
      setDataset(undefined);
      setDatasetStatus("idle");
      setView(nextView);
      setLaunchError(undefined);
      writeRoute(nextView, nextBenchmarkId, undefined, false, "");
      return;
    }
    const nextRunId = nextView === "home" ? undefined : selectedRunId || undefined;
    const installedHomeBenchmarkId = benchmarks.some(
      (benchmark) => benchmark.id === benchmarkId,
    )
      ? benchmarkId
      : benchmarks[0]?.id;
    const historicalBenchmarkId =
      nextView === "runs" &&
      benchmarkId &&
      runs.some((item) => item.benchmark_id === benchmarkId)
        ? benchmarkId
        : undefined;
    if (nextView !== "configure") setLaunchError(undefined);
    if (nextView === "home") {
      setBenchmarkId(installedHomeBenchmarkId ?? "");
      setSelectedRunId("");
      setRun(undefined);
      setReport(undefined);
      setEventCount(0);
    }
    setView(nextView);
    writeRoute(
      nextView,
      nextView === "home"
        ? installedHomeBenchmarkId
        : selectedBenchmark?.id ?? historicalBenchmarkId,
      nextRunId,
    );
  };

  const selectBenchmark = (nextBenchmarkId: string) => {
    setLaunchError(undefined);
    setBenchmarkId(nextBenchmarkId);
    setSelectedRunId("");
    setRun(undefined);
    setReport(undefined);
    setDatasetId("");
    setDataset(undefined);
    setDatasetStatus("idle");
    writeRoute(view, nextBenchmarkId, undefined, false, "");
  };

  const selectRun = (selected: Run) => {
    const isCurrentRun = selected.id === selectedRunId;
    setSelectedRunId(selected.id);
    setBenchmarkId(selected.benchmark_id);
    if (!isCurrentRun || !run) setRun(selected);
    if (!isCurrentRun) {
      setReport(undefined);
      setEventCount(0);
    }
    setView("evaluation");
    setLaunchError(undefined);
    setError(undefined);
    writeRoute("evaluation", selected.benchmark_id, selected.id);
  };

  const launch = async () => {
    if (launchingRef.current) return;
    if (!configureBenchmark) {
      setLaunchError("Select an available benchmark before launching.");
      return;
    }
    const blocked = launchBlockReason({
      benchmark: configureBenchmark, solutionIds, retryLimit,
      allowNetworked: networkPermission, launching: false,
      datasetBlocked, datasetBlockReason, maxAttempts: runtime?.max_attempts ?? 20,
      networkAvailable: runtime?.allow_networked === true, runtimeLoading: bootstrapping, runtime,
    });
    if (blocked) {
      setLaunchError(blocked);
      return;
    }
    launchingRef.current = true;
    setLaunching(true);
    setLaunchError(undefined);
    const input = runLaunchInput(
      configureBenchmark,
      solutionIds,
      retryLimit,
      networkPermission,
      effectiveDatasetId || undefined,
    );
    try {
      if (effectiveDatasetId && api.getDataset) await api.getDataset(effectiveDatasetId);
      const launched = await launchLock.launch(input);
      setRuns((current) => [
        launched,
        ...current.filter((item) => item.id !== launched.id),
      ]);
      setRun(launched);
      setReport(undefined);
      setEventCount(0);
      setSelectedRunId(launched.id);
      setView("evaluation");
      writeRoute("evaluation", configureBenchmark.id, launched.id);
    } catch (reason) {
      let failureMessage = reason instanceof Error ? reason.message : "Unable to launch run.";
      if (reason instanceof ApiError && reason.status === 410 && input.dataset_id && api.getDataset) {
        try {
          const current = await api.getDataset(input.dataset_id);
          if (current.id !== input.dataset_id || current.benchmark_id !== input.benchmark_id) {
            throw new ApiError("Dataset response does not match the selected revision.", 502);
          }
          if (effectiveDatasetIdRef.current === input.dataset_id) {
            if (!current.pinned && current.expires_at && Date.parse(current.expires_at) <= (api.serverNow?.() ?? Date.now())) {
              setDataset(undefined);
              setDatasetStatus("expired");
              setDatasetError("Dataset expired. Select another dataset.");
            } else {
              setDataset(current);
              setDatasetError(undefined);
              setDatasetStatus("ready");
            }
          }
        } catch (datasetReason) {
          if (effectiveDatasetIdRef.current === input.dataset_id && datasetReason instanceof ApiError && [404, 410].includes(datasetReason.status ?? 0)) {
            setDataset(undefined);
            setDatasetStatus(datasetReason.status === 410 ? "expired" : "error");
            setDatasetError(datasetReason.status === 410
              ? "Dataset expired. Select another dataset."
              : "Dataset unavailable. Select another dataset.");
          } else if (effectiveDatasetIdRef.current === input.dataset_id) {
            failureMessage += " The dataset could not be rechecked; retry after connectivity is restored.";
          }
        }
      }
      setLaunchError(failureMessage);
    } finally {
      launchingRef.current = false;
      setLaunching(false);
    }
  };

  const inspect = (evidence: Evidence, trigger: HTMLButtonElement) => {
    restoreFocusRef.current = trigger;
    setSelectedEvidence(evidence);
  };

  const closeDrawer = useCallback(() => {
    setSelectedEvidence(undefined);
    window.setTimeout(() => restoreFocusRef.current?.focus(), 0);
  }, []);

  const unavailableWorkspace = unavailableRun?.id === selectedRunId && ["evaluation", "reports"].includes(view)
    ? <div className="view-shell"><header className="view-header"><div><h1>{unavailableRun.reason === "expired" ? "Run expired" : "Run unavailable"}</h1><p role="status">{unavailableRun.reason === "expired" ? "This run and its evidence are no longer available." : "This run no longer exists or has been removed."} Cached reports and artifacts have been cleared.</p></div></header><button type="button" className="primary-button" onClick={() => navigate("configure")}>Configure another run</button></div>
    : undefined;

  if (
    !selectedBenchmark &&
    !bootstrapping &&
    !bootstrapError &&
    view === "runs" &&
    catalogRuns.length > 0
  ) {
    const historicalRunScope =
      benchmarkId &&
      catalogRuns.some((item) => item.benchmark_id === benchmarkId)
        ? benchmarkId
        : undefined;
    const historicalRuns = historicalRunScope
      ? catalogRuns.filter((item) => item.benchmark_id === historicalRunScope)
      : catalogRuns;
    return (
      <AppShell
        benchmarks={benchmarks}
        benchmarkSelectionId={undefined}
        run={run}
        runs={catalogRuns}
        runScopeBenchmarkId={historicalRunScope}
        runtime={runtime}
        view={view}
        onNavigate={navigate}
        onBenchmarkChange={selectBenchmark}
        onRunSelect={selectRun}
        onRefreshRuns={() => void refreshRuns()}
      >
        <>
          <RunsWorkspace
            runs={historicalRuns}
            activeRunId={selectedRunId || undefined}
            onSelect={selectRun}
            onRefresh={() => void refreshRuns()}
          />
          {error && <div className="global-error" role="alert">{error}</div>}
        </>
      </AppShell>
    );
  }

  if (!selectedBenchmark) {
    if (!bootstrapping) {
      const emptyWorkspace = unavailableWorkspace ?? (
        bootstrapError ? (
          <div className="view-shell">
            <header className="view-header">
              <div>
                <span className="section-kicker">Benchmark catalog</span>
                <h1>Unable to load AI Bench</h1>
                <p className="inline-error" role="alert">{bootstrapError}</p>
              </div>
            </header>
            <button type="button" className="secondary-button" onClick={() => window.location.reload()}>Reload catalog</button>
          </div>
        ) : catalogError || (benchmarkId && !catalogBenchmark) ? (
          <div className="view-shell">
            <header className="view-header">
              <div>
                <h1>Benchmark catalog unavailable</h1>
                <p role="status">{catalogError ?? "This benchmark is not available in the current catalog. Saved run links remain available for reviewing evidence."}</p>
              </div>
            </header>
            {benchmarks.length > 0 && <button type="button" className="primary-button" onClick={() => navigate("benchmarks")}>Choose an available benchmark</button>}
            <button type="button" className="secondary-button" onClick={() => window.location.reload()}>Reload catalog</button>
          </div>
        ) : view === "create" ? (
          <CreateBenchmarkWorkspace onNavigate={navigate} />
        ) : view === "benchmarks" ? (
          <BenchmarksWorkspace
            benchmarks={benchmarks}
            selectedId=""
            onSelect={selectBenchmark}
            onNavigate={navigate}
          />
        ) : (
          <div className="view-shell">
            <header className="view-header">
              <div>
                <span className="section-kicker">Benchmark catalog</span>
                <h1>No benchmark packs installed</h1>
                <p>
                  Scaffold or install a benchmark pack before configuring a new
                  run.
                </p>
              </div>
              <button
                className="primary-button"
                type="button"
                onClick={() => navigate("create")}
              >
                Create benchmark
              </button>
            </header>
          </div>
        ));
      return (
        <AppShell
          benchmarks={benchmarks}
          benchmarkSelectionId={undefined}
          run={run}
          runs={catalogRuns}
          runtime={runtime}
          view={view}
          onNavigate={navigate}
          onBenchmarkChange={selectBenchmark}
          onRunSelect={selectRun}
          onRefreshRuns={() => void refreshRuns()}
        >
          {emptyWorkspace}
        </AppShell>
      );
    }
    return (
      <main className="loading-screen">
        <span className="brand-symbol" aria-hidden="true">AI</span>
        <h1>{error ?? "Loading AI Bench…"}</h1>
      </main>
    );
  }

  const scopedRuns = runs.filter(
    (item) => item.benchmark_id === selectedBenchmark.id,
  );
  const activeReport =
    run && report?.run_id === run.id ? report : undefined;
  const pinnedEvaluationBenchmark =
    run?.pinned_benchmark?.id === selectedBenchmark.id
      ? run.pinned_benchmark
      : selectedBenchmark;
  const evaluationBenchmark = withCatalogNames(pinnedEvaluationBenchmark, benchmarks);
  const datasetControls = catalogBenchmark?.dataset_import ? (
    <DatasetWorkspace
      key={catalogBenchmark.id}
      api={api}
      benchmark={catalogBenchmark}
      selectedId={datasetId || undefined}
      selected={selectedDataset}
      selectionError={datasetError}
      onRefreshSelected={reloadSelectedDataset}
      onSelect={(id) => {
        setDatasetId(id);
        setLaunchError(undefined);
        writeRoute(view, catalogBenchmark.id, selectedRunId || undefined, false, id);
      }}
      onInspect={inspect}
    />
  ) : undefined;

  let workspace: ReactNode;
  switch (view) {
    case "benchmarks":
      workspace = (
        <BenchmarksWorkspace
          benchmarks={benchmarks}
          selectedId={selectedBenchmark.id}
          onSelect={selectBenchmark}
          onNavigate={navigate}
          datasetControls={datasetControls}
        />
      );
      break;
    case "create":
      workspace = <CreateBenchmarkWorkspace onNavigate={navigate} />;
      break;
    case "solutions":
      workspace = (
        <SolutionsWorkspace benchmark={selectedBenchmark} onNavigate={navigate} runtime={runtime} />
      );
      break;
    case "configure":
      workspace = (
        <ConfigureRunWorkspace
          benchmark={configureBenchmark!}
          solutionIds={solutionIds}
          retryLimit={retryLimit}
          allowNetworked={networkPermission}
          launching={launching}
          error={launchError ?? bootstrapError ?? error ?? (catalogBenchmark?.dataset_import ? undefined : datasetError)}
          onSolutionIdsChange={setSolutionIds}
          onRetryLimitChange={setRetryLimit}
          onAllowNetworkedChange={setAllowNetworked}
          onLaunch={() => void launch()}
          datasetControls={datasetControls}
          datasetBlocked={datasetBlocked}
          datasetBlockReason={datasetBlockReason}
          onRetryDataset={datasetStatus === "error" && effectiveDatasetId ? reloadSelectedDataset : undefined}
          runtimeLoading={bootstrapping}
          maxAttempts={runtime?.max_attempts ?? 20}
          networkAvailable={runtime?.allow_networked === true}
          runtime={runtime}
        />
      );
      break;
    case "evaluation":
      workspace = (
        <EvaluationWorkspace
          benchmark={evaluationBenchmark}
          run={run}
          report={activeReport}
          eventCount={eventCount}
          artifactUrl={(sha256) => api.artifactUrl(sha256)}
          onConfigure={() => navigate("configure")}
          onInspect={inspect}
          loadDiagnostics={loadDiagnostics}
          onUnavailable={expireRun}
        />
      );
      break;
    case "runs":
      workspace = (
        <RunsWorkspace
          runs={scopedRuns}
          activeRunId={run?.id}
          onSelect={selectRun}
          onRefresh={() => void refreshRuns()}
        />
      );
      break;
    case "reports":
      workspace = (
        <ReportsWorkspace
          run={run}
          report={activeReport}
          solutionNames={Object.fromEntries(evaluationBenchmark.pipelines?.map(item => [item.id, item.name]) ?? [])}
          onNavigate={navigate}
        />
      );
      break;
    case "admin":
      workspace = (
        <AdminWorkspace runtime={runtime} benchmark={selectedBenchmark} />
      );
      break;
    default:
      workspace = (
        <HomeWorkspace
          benchmarks={benchmarks}
          benchmark={selectedBenchmark}
          run={run}
          runs={catalogRuns}
          onNavigate={navigate}
        />
      );
  }
  if (unavailableWorkspace) workspace = unavailableWorkspace;

  return (
    <>
      <AppShell
        benchmarks={benchmarks}
        selectedBenchmark={
          view === "evaluation" ? evaluationBenchmark : selectedBenchmark
        }
        benchmarkSelectionId={
          benchmarks.some((benchmark) => benchmark.id === selectedBenchmark.id)
            ? selectedBenchmark.id
            : undefined
        }
        run={run}
        runs={catalogRuns}
        runtime={runtime}
        view={view}
        onNavigate={navigate}
        onBenchmarkChange={selectBenchmark}
        onRunSelect={selectRun}
        onRefreshRuns={() => void refreshRuns()}
      >
        {(bootstrapError ?? error) && view !== "configure" && (
          <div className="global-error" role="alert">
            {bootstrapError ?? error}
          </div>
        )}
        {workspace}
      </AppShell>
      <EvidenceDrawer
        evidence={selectedEvidence}
        artifactUrl={(sha256) => api.artifactUrl(sha256)}
        onClose={closeDrawer}
      />
    </>
  );
}

export default App;
