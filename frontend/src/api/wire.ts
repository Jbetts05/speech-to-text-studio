import type {
  BenchmarkManifest,
  Evidence,
  EventPage,
  EvaluationRead,
  MetricDefinition,
  MetricFamily,
  MetricValue,
  Run,
  RunEvent,
  RunReport,
  RunStatus,
  TaskRun,
  TaskStatus,
  ViewerDescriptor,
} from "../types";
import type * as Backend from "./contracts.generated";

export type WireRunStatus = Backend.BackendRunStatus;
export type WireTaskStatus = Backend.BackendTaskStatus;
export type BenchmarkDefinitionDto = Backend.BackendBenchmarkDefinition;
export type CompatibilityDto = Backend.BackendCompatibility;
export type PipelineDto = Backend.BackendPipelineConfiguration;
export type SolutionDto = Backend.BackendCatalogSolution;
export type ViewerDto = Backend.BackendRendererDescriptor;
export type BenchmarkEnvelopeDto = Backend.BackendBenchmarkCatalogEntry;
export type RunSummaryDto = Backend.BackendRunSummaryResponse;
export type CreateRunResponseDto = Backend.BackendCreateRunResponse;
export type MetricDefinitionDto = Backend.BackendMetricDefinition;
export type MetricValueDto = Backend.BackendMetricValue;
export type ArtifactEvidenceDto = Backend.BackendArtifactEvidence;
export type TelemetryDto = Backend.BackendTelemetryEvidence;
export type TaskAttemptDto = Backend.BackendTaskAttempt;
export type TaskResultDto = Backend.BackendTaskResult;
export type RunManifestDto = Backend.BackendRunManifest;
export type RunTaskDto = Backend.BackendRunTaskResponse;
export type RunDetailDto = Backend.BackendRunDetailResponse;
export type RunEventDto = Backend.BackendRunEvent;
export type RunReportDto = Backend.BackendRunReport;
export type FinalizedEvaluationDto = Backend.BackendFinalizedEvaluationResponse;
export type LiveDetailRequiredDto = Backend.BackendLiveDetailRequiredResponse;

function pipelineComponents(pipeline: PipelineDto): string[] | undefined {
  return pipeline.pipeline_type === "assisted" ? pipeline.components : undefined;
}

function mapDefinition(definition: MetricDefinitionDto): MetricDefinition {
  return {
    id: definition.id,
    name: definition.name,
    family: definition.family,
    unit: definition.unit,
    description: definition.description,
    direction:
      definition.higher_is_better == null
        ? "neutral"
        : definition.higher_is_better
          ? "higher"
          : "lower",
  };
}

export function mapBenchmark(dto: BenchmarkEnvelopeDto): BenchmarkManifest {
  return {
    id: dto.definition.id,
    name: dto.definition.name,
    version: dto.definition.version,
    description: dto.definition.description,
    tasks: [],
    metrics: dto.metrics.map(mapDefinition),
    sample_count: dto.sample_count,
    scored_sample_count: dto.scored_sample_count,
    dataset_availability: dto.dataset_availability,
    dataset_import: dto.dataset_import,
    default_dataset_id: dto.default_dataset_id,
    input_modalities: dto.definition.input_modalities,
    reference_modalities: dto.definition.reference_modalities,
    pipelines: dto.solutions.map((solution) => ({
      id: solution.id,
      name: solution.display_name ?? solution.id,
      mode: solution.pipeline.pipeline_type,
      networked: solution.networked,
      compatibility: solution.compatibility,
      metadata: {
        adapter_id: solution.pipeline.adapter_id,
        components: pipelineComponents(solution.pipeline),
      },
    })),
    viewer: {
      id: dto.viewer.id,
      version: dto.viewer.version,
      title: dto.viewer.title,
      modality: dto.viewer.modality,
      media_types: dto.viewer.media_types,
      fallback_download: dto.viewer.fallback_download ?? true,
    },
    metadata: {
      terminology: dto.definition.terminology,
      tags: dto.definition.tags,
    },
  };
}

export function mapRunSummary(dto: RunSummaryDto): Run {
  return {
    id: dto.run_id,
    dataset_id: dto.dataset_id ?? undefined,
    expires_at: dto.expires_at,
    pinned: dto.pinned,
    archival: dto.archival,
    benchmark_id: dto.benchmark_id,
    solution_ids: [],
    status: dto.status,
    created_at: dto.created_at,
    manifest_artifact_id: dto.manifest_artifact_id,
    report_artifact_id: dto.report_artifact_id ?? undefined,
  };
}

function mapTask(task: RunTaskDto): TaskRun {
  const cooldown = task.cooldown_until && Date.parse(task.cooldown_until) > Date.now();
  const retry = task.next_available_at && Date.parse(task.next_available_at) > Date.now();
  return {
    id: task.id,
    task_id: task.id,
    sample_id: task.sample_id,
    solution_id: task.solution_id,
    status: task.status,
    attempt: task.attempts,
    message: task.result?.error ?? undefined,
    next_available_at: task.next_available_at ?? undefined,
    cooldown_until: task.cooldown_until ?? undefined,
    wait_state: task.status !== "pending" ? undefined : cooldown ? "cooldown" : retry ? "retry" : "queued",
    attempt_history: task.attempt_history,
  };
}

function telemetryMetrics(
  telemetry: TelemetryDto,
  taskId: string,
  solutionId: string,
): MetricValue[] {
  const values: MetricValue[] = [
    {
      metric_id: "cost.amount",
      family: "cost",
      value: telemetry.cost.amount,
      unit: telemetry.cost.currency,
      task_id: taskId,
      solution_id: solutionId,
    },
    {
      metric_id: "latency_ms",
      family: "latency",
      value: telemetry.latency_ms,
      unit: "ms",
      task_id: taskId,
      solution_id: solutionId,
    },
  ];
  for (const [dimension, value] of Object.entries(
    telemetry.usage.dimensions ?? {},
  )) {
    values.push({
      metric_id: `usage.${dimension}`,
      family: "usage",
      value,
      unit: dimension,
      task_id: taskId,
      solution_id: solutionId,
    });
  }
  return values;
}

function resultMetrics(result: TaskResultDto): MetricValue[] {
  const values: MetricValue[] = (result.metrics ?? []).map((metric) => ({
    metric_id: metric.metric_id,
    family: metric.family,
    value: metric.value,
    unit: metric.unit,
    task_id: result.task_id,
    solution_id: result.solution_id,
  }));
  if (result.telemetry) {
    values.push(
      ...telemetryMetrics(
        result.telemetry,
        result.task_id,
        result.solution_id,
      ),
      {
        metric_id: "attempts",
        family: "reliability",
        value: result.attempts,
        task_id: result.task_id,
        solution_id: result.solution_id,
      },
      {
        metric_id: "task_success",
        family: "reliability",
        value: result.status === "succeeded" ? 1 : 0,
        unit: "ratio",
        task_id: result.task_id,
        solution_id: result.solution_id,
      },
    );
  }
  return values;
}

function artifactEvidence(
  result: TaskResultDto,
  viewer: ViewerDto,
): Evidence[] {
  return (result.evidence ?? []).map((evidence) => ({
    id: `${result.task_id}:${evidence.role}:${evidence.artifact_id}`,
    task_id: result.task_id,
    task_name: result.sample_id,
    solution_id: result.solution_id,
    solution_name: result.solution_id,
    modality: evidence.modality,
    label: `${evidence.role} · ${result.sample_id}`,
    summary: `${viewer.title} ${evidence.role} artifact.`,
    artifact_sha256: evidence.artifact_id,
    media_type: evidence.media_type,
    byte_size: evidence.size_bytes,
    attempt: evidence.attempt_number ?? undefined,
    role: evidence.role,
    source: "result",
    metrics: resultMetrics(result),
  }));
}

function attemptEvidence(
  task: Pick<RunTaskDto, "id" | "sample_id" | "solution_id">,
  attempt: TaskAttemptDto,
  viewer: ViewerDto,
): Evidence[] {
  const roleLabels: Record<ArtifactEvidenceDto["role"], string> = {
    input: "input",
    reference: "reference",
    raw: "provider response",
    parsed: "parsed output",
    comparison: "evaluation comparison",
    result: "task result",
    report: "report",
  };
  const metrics = attempt.telemetry
    ? telemetryMetrics(attempt.telemetry, task.id, task.solution_id)
    : [];
  if (attempt.duration_ms !== undefined && attempt.duration_ms !== null) {
    metrics.push({
      metric_id: "attempt_duration_ms",
      family: "latency",
      value: attempt.duration_ms,
      unit: "ms",
      task_id: task.id,
      solution_id: task.solution_id,
    });
  }
  return (attempt.evidence ?? []).map((evidence) => ({
    id: `${task.id}:attempt:${attempt.attempt_number}:${evidence.role}:${evidence.artifact_id}`,
    task_id: task.id,
    task_name: task.sample_id,
    solution_id: task.solution_id,
    solution_name: task.solution_id,
    modality: evidence.modality,
    label: `attempt ${attempt.attempt_number} ${roleLabels[evidence.role]} · ${task.sample_id}`,
    summary:
      attempt.error_message ??
      `${viewer.title} ${evidence.role} artifact from attempt ${attempt.attempt_number}.`,
    artifact_sha256: evidence.artifact_id,
    media_type: evidence.media_type,
    byte_size: evidence.size_bytes,
    attempt: attempt.attempt_number,
    role: evidence.role,
    source: "attempt",
    metrics,
  }));
}

function mapManifestContext(manifest: RunManifestDto): Omit<Run, "status"> {
  const pinnedPipelines = Object.entries(
    manifest.solution_descriptors,
  ).map(([solutionId, descriptor]) => ({
    id: solutionId,
    name: solutionId,
    version: descriptor.version,
    mode: descriptor.pipeline.pipeline_type,
    compatibility: descriptor.compatibility,
    metadata: {
      adapter_id: descriptor.pipeline.adapter_id,
      components: pipelineComponents(descriptor.pipeline),
    },
  }));
  const pinnedViewer = mapViewer(manifest.viewer);
  return {
    id: manifest.run_id,
    benchmark_id: manifest.benchmark.id,
    benchmark_version: manifest.benchmark.version,
    pinned_benchmark: {
      id: manifest.benchmark.id,
      name: manifest.benchmark.name,
      version: manifest.benchmark.version,
      description: manifest.benchmark.description,
      tasks: [],
      metrics: manifest.evaluator.metrics.map(mapDefinition),
      sample_count: manifest.sample_ids.length,
      scored_sample_count: manifest.sample_ids.length,
      dataset_availability: {
        status: "ready",
        reason: "This run uses pinned, persisted artifacts, independent of current acquisition paths.",
        revision: manifest.dataset_revision,
        sample_count: manifest.samples.length,
        scored_sample_count: manifest.sample_ids.length,
      },
      pipelines: pinnedPipelines,
      input_modalities: manifest.benchmark.input_modalities,
      reference_modalities: manifest.benchmark.reference_modalities,
      viewer: pinnedViewer,
      metadata: {
        terminology: manifest.benchmark.terminology,
        tags: manifest.benchmark.tags,
      },
    },
    solution_ids: manifest.solution_ids,
    created_at: manifest.created_at,
    retry_limit: manifest.retry_limit,
    allow_networked: manifest.networked_execution,
    metric_definitions: manifest.evaluator.metrics.map(mapDefinition),
    quality_comparability: Object.fromEntries(
      Object.entries(manifest.solution_descriptors).map(
        ([solutionId, descriptor]) => [solutionId, descriptor.compatibility],
      ),
    ),
  };
}

function mapSampleArtifacts(
  samples: RunDetailDto["sample_artifacts"],
  taskForSample: (sampleId: string) => string | undefined,
): Record<string, Evidence[]> {
  return Object.fromEntries(
    Object.entries(samples ?? {}).map(([sampleId, artifacts]) => [
      sampleId,
      artifacts.map((artifact): Evidence => ({
        id: `sample:${sampleId}:${artifact.role}:${artifact.artifact_id}`,
        task_id: taskForSample(sampleId) ?? sampleId,
        task_name: sampleId,
        label: `${artifact.role} · ${sampleId}`,
        role: artifact.role,
        source: "sample",
        modality: artifact.modality,
        media_type: artifact.media_type,
        byte_size: artifact.size_bytes,
        artifact_sha256: artifact.artifact_id,
      })),
    ]),
  );
}

export function mapFinalizedEvaluation(dto: FinalizedEvaluationDto): EvaluationRead & { record_source: "finalized_report" } {
  const report = mapReport(dto.report, dto.manifest.viewer);
  return {
    record_source: "finalized_report",
    report,
    run: {
      ...mapRunSummary(dto.run),
      ...mapManifestContext(dto.manifest),
      record_source: "finalized_report",
      finalized_results: dto.report.results,
      costs: dto.costs,
      runtime_error: dto.run.runtime_error ?? undefined,
      metrics: report.families.flatMap(family => family.metrics),
      evidence: report.evidence,
      sample_artifacts: mapSampleArtifacts(dto.sample_artifacts,
        sample => dto.report.results.find(result => result.sample_id === sample)?.task_id),
    },
  };
}

export function mapRunDetail(dto: RunDetailDto): Run {
  const results = dto.tasks.flatMap((task) => task.result ? [task.result] : []);
  const attempts = dto.tasks.flatMap((task) => task.attempt_history);
  return {
    ...mapManifestContext(dto.manifest),
    record_source: "live_detail",
    costs: dto.costs ?? undefined,
    dataset_id: dto.dataset_id ?? undefined,
    expires_at: dto.expires_at,
    pinned: dto.pinned,
    archival: dto.archival,
    status: dto.status,
    runtime_error: dto.runtime_error ?? undefined,
    sample_artifacts: mapSampleArtifacts(dto.sample_artifacts,
      sample => dto.tasks.find(task => task.sample_id === sample)?.id),
    manifest_artifact_id: dto.manifest_artifact_id,
    report_artifact_id: dto.report_artifact_id ?? undefined,
    tasks: dto.tasks.map(mapTask),
    metrics: [
      ...results.flatMap(resultMetrics),
      ...attempts.flatMap((attempt) =>
        attempt.telemetry
          ? telemetryMetrics(
              attempt.telemetry,
              attempt.task_id,
              dto.tasks.find((task) => task.id === attempt.task_id)
                ?.solution_id ?? "",
            )
          : [],
      ),
    ],
    evidence: [
      ...results.flatMap((result) =>
        artifactEvidence(result, dto.manifest.viewer),
      ),
      ...dto.tasks.flatMap((task) =>
        task.attempt_history.flatMap((attempt) =>
          attemptEvidence(task, attempt, dto.manifest.viewer),
        ),
      ),
    ],
  };
}

function taskStatusForEvent(eventType: string): TaskStatus | undefined {
  const statuses: Record<string, TaskStatus> = {
    "task.enqueued": "pending",
    "task.leased": "leased",
    "task.succeeded": "succeeded",
    "task.poisoned": "poisoned",
    "task.retry_scheduled": "pending",
    "task.recovered": "pending",
    "task.deferred": "pending",
  };
  return statuses[eventType];
}

function asRunStatus(value: unknown): RunStatus | undefined {
  return [
    "created",
    "running",
    "completed",
    "completed-with-failures",
    "failed",
  ].includes(String(value))
    ? (value as RunStatus)
    : undefined;
}

export function mapEvent(dto: RunEventDto): RunEvent {
  const payload = dto.payload ?? {};
  const taskId =
    typeof payload.task_id === "string" ? payload.task_id : undefined;
  const taskStatus = taskStatusForEvent(dto.event_type);
  return {
    id: String(dto.sequence),
    sequence: dto.sequence,
    run_id: dto.run_id,
    type: dto.event_type,
    occurred_at: dto.occurred_at,
    run_status:
      dto.event_type === "run.finalized"
        ? asRunStatus(payload.status)
        : dto.event_type === "run.created"
          ? "running"
          : undefined,
    task:
      taskId && taskStatus
        ? {
            id: taskId,
            task_id: taskId,
            status: taskStatus,
            attempt:
              typeof payload.attempt === "number"
                ? payload.attempt
                : undefined,
          }
        : undefined,
    message:
      typeof payload.message === "string"
        ? payload.message
        : undefined,
  };
}

export function mapEventPage(dtos: RunEventDto[]): EventPage {
  return {
    events: dtos.map(mapEvent),
    next_after: dtos.reduce(
      (latest, event) => Math.max(latest, event.sequence),
      0,
    ),
  };
}

function titleFromId(value: string): string {
  return value
    .replace(/[._-]+/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function reportDefinitions(dto: RunReportDto): MetricDefinition[] {
  const definitions = new Map<string, MetricDefinition>();
  for (const result of dto.results) {
    for (const metric of result.metrics ?? []) {
      definitions.set(metric.metric_id, {
        id: metric.metric_id,
        name: titleFromId(metric.metric_id),
        family: metric.family,
        unit: metric.unit,
        direction: "neutral",
      });
    }
  }
  const add = (
    id: string,
    name: string,
    family: MetricFamily,
    unit?: string,
  ) => {
    if (!definitions.has(id)) {
      definitions.set(id, {
        id,
        name,
        family,
        unit,
        direction: "neutral",
      });
    }
  };
  for (const values of Object.values(dto.quality)) {
    for (const metricId of Object.keys(values)) {
      add(metricId, titleFromId(metricId), "quality");
    }
  }
  const currencies = new Set(Object.values(dto.cost_currency));
  const costUnit = currencies.size === 1 ? [...currencies][0] : undefined;
  add("cost.amount", "Cost", "cost", costUnit);
  add("latency_ms", "Total attempt time", "latency", "ms");
  const latencyNames: Record<string, string> = {
    provider_ms: "Provider request time",
    queue_wait_ms: "Aggregate queue wait",
    cooldown_wait_ms: "Aggregate cooldown wait",
    end_to_end_elapsed_ms: "Elapsed to solution completion",
  };
  for (const values of Object.values(dto.latency_breakdown ?? {})) {
    for (const dimension of Object.keys(values)) {
      if (dimension === "attempt_ms") continue;
      add(
        `latency.${dimension}`,
        latencyNames[dimension] ?? titleFromId(dimension),
        "latency",
        "ms",
      );
    }
  }
  add("attempts", "Attempts", "reliability");
  add("task_success", "Task success", "reliability", "ratio");
  for (const dimensions of Object.values(dto.usage)) {
    for (const dimension of Object.keys(dimensions)) {
      add(`usage.${dimension}`, titleFromId(dimension), "usage", dimension);
    }
  }
  for (const values of Object.values(dto.reliability)) {
    for (const metricId of Object.keys(values)) {
      add(
        `reliability.${metricId}`,
        titleFromId(metricId),
        "reliability",
      );
    }
  }
  return [...definitions.values()];
}

function reportFamilyMetrics(dto: RunReportDto): MetricValue[] {
  const metrics: MetricValue[] = [];
  for (const [solutionId, values] of Object.entries(dto.quality)) {
    for (const [metricId, value] of Object.entries(values)) {
      metrics.push({
        metric_id: metricId,
        family: "quality",
        value,
        solution_id: solutionId,
      });
    }
  }
  for (const [solutionId, value] of Object.entries(dto.cost)) {
    metrics.push({
      metric_id: "cost.amount",
      family: "cost",
      value,
      unit: dto.cost_currency[solutionId],
      solution_id: solutionId,
    });
  }
  for (const [solutionId, value] of Object.entries(dto.latency)) {
    metrics.push({
      metric_id: "latency_ms",
      family: "latency",
      value,
      unit: "ms",
      solution_id: solutionId,
    });
  }
  for (const [solutionId, values] of Object.entries(dto.usage)) {
    for (const [dimension, value] of Object.entries(values)) {
      metrics.push({
        metric_id: `usage.${dimension}`,
        family: "usage",
        value,
        unit: dimension,
        solution_id: solutionId,
      });
    }
  }
  for (const [solutionId, values] of Object.entries(dto.latency_breakdown ?? {})) {
    for (const [dimension, value] of Object.entries(values)) {
      if (dimension === "attempt_ms") continue;
      metrics.push({
        metric_id: `latency.${dimension}`,
        family: "latency",
        value,
        unit: "ms",
        solution_id: solutionId,
      });
    }
  }
  for (const [solutionId, values] of Object.entries(dto.reliability)) {
    for (const [metricId, value] of Object.entries(values)) {
      metrics.push({
        metric_id: `reliability.${metricId}`,
        family: "reliability",
        value,
        solution_id: solutionId,
      });
    }
  }
  return metrics;
}

export function mapReport(
  dto: RunReportDto,
  viewer: ViewerDto,
): RunReport {
  const taskMetrics = dto.results.flatMap(resultMetrics);
  const aggregates = reportFamilyMetrics(dto);
  const definitions = reportDefinitions(dto);
  const evidence = dto.results.flatMap((result) =>
    artifactEvidence(result, viewer),
  );
  for (const result of dto.results) {
    const task = {
      id: result.task_id,
      sample_id: result.sample_id,
      solution_id: result.solution_id,
    };
    evidence.push(
      ...(dto.attempt_history?.[result.task_id] ?? []).flatMap((attempt) =>
        attemptEvidence(task, attempt, viewer),
      ),
    );
  }
  const knownArtifacts = new Set(
    evidence.map((item) => item.artifact_sha256),
  );
  for (const [key, href] of Object.entries(dto.evidence_links)) {
    const artifactId = href.split("/").filter(Boolean).at(-1);
    if (!artifactId || knownArtifacts.has(artifactId)) continue;
    const [taskId, role = "evidence"] = key.split(":");
    evidence.push({
      id: `${key}:${artifactId}`,
      task_id: taskId,
      modality: viewer.modality,
      label: `${role} · ${taskId}`,
      summary: `${viewer.title} ${role} artifact.`,
      artifact_sha256: artifactId,
      media_type: viewer.media_types[0],
      role: isEvidenceRole(role) ? role : "result",
      source: "result",
    });
  }
  return {
    run_id: dto.run_id,
    generated_at: dto.generated_at,
    status: dto.status,
    definitions,
    viewer: mapViewer(viewer),
    families: (
      ["quality", "cost", "latency", "usage", "reliability"] as MetricFamily[]
    ).map((family) => ({
      family,
      metrics: [...taskMetrics, ...aggregates].filter(
        (metric) => metric.family === family,
      ),
    })),
    evidence,
    quality_comparability: dto.quality_comparability,
    coverage: dto.coverage,
    usage_uncertainty: dto.usage_uncertainty,
    metadata: {
      manifest_artifact_id: dto.manifest_artifact_id,
      evidence_links: dto.evidence_links,
    },
  };
}

function mapViewer(viewer: ViewerDto): ViewerDescriptor {
  return {
    id: viewer.id,
    version: viewer.version,
    title: viewer.title,
    modality: viewer.modality,
    media_types: [...viewer.media_types],
    fallback_download: viewer.fallback_download ?? true,
  };
}

function isEvidenceRole(value: string): value is ArtifactEvidenceDto["role"] {
  return [
    "input",
    "reference",
    "raw",
    "parsed",
    "comparison",
    "result",
    "report",
  ].includes(value);
}
