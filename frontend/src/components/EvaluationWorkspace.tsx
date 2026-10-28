import {
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
} from "react";
import type {
  BenchmarkManifest,
  Evidence,
  MetricDefinition,
  MetricFamily,
  MetricValue,
  Run,
  RunReport,
} from "../types";
import { formatCompactDate, formatMetricValue, formatRunId } from "../format";
import { ArtifactPreview, ArtifactValue } from "./ArtifactPreview";
import { EvidenceLedger } from "./EvidenceLedger";
import { ExecutionProgress } from "./ExecutionProgress";
import { ResultComparison } from "./TranscriptComparison";
import { DownloadIcon, GridIcon, PlayIcon } from "./Icons";
import { RetentionNotice } from "./DatasetWorkspace";
import { CostAmount, CostCell, CostEvidence, costHeadline } from "./CostEvidence";
import { compareResultValues, RunResults, type ResultRow } from "./RunResults";
import { MetricHelp, metricLabel } from "./MetricLabel";
import { CurrentDiagnostics } from "./CurrentDiagnostics";

const familyOrder: MetricFamily[] = [
  "quality",
  "cost",
  "latency",
  "usage",
  "reliability",
];
const detailOrder = ["evidence", "manifest"] as const;
type DetailTab = (typeof detailOrder)[number];

function numeric(value: number | string | null): number | undefined {
  if (value === null || value === "") return undefined;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function familyLabel(family: MetricFamily): string {
  return family.charAt(0).toUpperCase() + family.slice(1);
}

function MetricCard({
  label,
  value,
  detail,
  family,
}: {
  label: string;
  value: string;
  detail: string;
  family: MetricFamily;
}) {
  return (
    <article className={`kpi-card family-${family}`}>
      <div className="kpi-title-row">
        <span>{label}</span>
        <span className="kpi-accent" aria-hidden="true" />
      </div>
      <strong>{value}</strong>
      <small>{detail}</small>
    </article>
  );
}

function SolutionPerformance({
  benchmark,
  run,
  metrics,
  definitions,
  coverage,
}: {
  benchmark: BenchmarkManifest;
  run: Run;
  metrics: MetricValue[];
  definitions: MetricDefinition[];
  coverage?: RunReport["coverage"];
}) {
  const aggregateMetrics = metrics.filter(
    (metric) => !metric.task_id && metric.solution_id,
  );
  const visibleDefinitions = definitions.filter((definition) =>
    !(definition.family === "cost" && definition.id === "cost.amount") && aggregateMetrics.some(
      (metric) =>
        metric.family === definition.family && metric.metric_id === definition.id && metric.value !== null,
    ),
  );
  const currencies = [...new Set([
    ...Object.keys(run.costs?.recorded_total.amounts ?? {}),
    ...Object.keys(run.costs?.current_estimate_total.amounts ?? {}),
    ...aggregateMetrics.flatMap(metric => metric.family === "cost" && metric.metric_id === "cost.amount" && metric.unit ? [metric.unit] : []),
  ])];
  if (!currencies.length) currencies.push("");
  const costColumns = currencies.flatMap(currency => [
    { key: `recorded:${currency}`, currency, estimated: false, label: `Cost${currency ? ` (${currency})` : ""}` },
    ...(run.costs ? [{ key: `estimate:${currency}`, currency, estimated: true, label: `Est. cost${currency ? ` (${currency})` : ""}` }] : []),
  ]);
  const firstSort = visibleDefinitions.find(item => item.family === "quality") ?? visibleDefinitions[0];
  const [sortId, setSortId] = useState(
    firstSort ? `${firstSort.family}:${firstSort.id}` : "solution",
  );
  const [descending, setDescending] = useState(true);
  const definitionsByKey = new Map(
    definitions.map((definition) => [`${definition.family}:${definition.id}`, definition]),
  );
  const statusFor = (solutionId: string) => {
    const tasks = (run.finalized_results ?? run.tasks ?? []).filter((task) => task.solution_id === solutionId);
    if (tasks.some((task) => task.status === "leased")) return "running";
    if (tasks.some((task) => task.status === "poisoned")) return "failed";
    if (tasks.length > 0 && tasks.every((task) => task.status === "succeeded")) {
      return "completed";
    }
    return run.status;
  };
  const rows = [...run.solution_ids].sort((left, right) => {
    if (
      definitionsByKey.get(sortId)?.family === "quality" &&
      Object.values(coverage ?? {}).some((item) => !item.comparable)
    ) {
      return left.localeCompare(right);
    }
    if (sortId === "solution") {
      return left.localeCompare(right) * (descending ? -1 : 1);
    }
    const costColumn = costColumns.find(column => column.key === sortId);
    if (costColumn) {
      const amount = (id: string) => {
        const cost = run.costs?.solutions.find(item => item.solution_id === id);
        if (cost) return (costColumn.estimated ? cost.current_estimate : cost.recorded).amounts[costColumn.currency];
        return !costColumn.estimated ? aggregateMetrics.find(metric => metric.solution_id === id && metric.family === "cost" && metric.metric_id === "cost.amount" && metric.unit === costColumn.currency)?.value : undefined;
      };
      return compareResultValues(amount(left), amount(right), descending);
    }
    const leftValue = numeric(
      aggregateMetrics.find(
        (metric) => metric.solution_id === left && `${metric.family}:${metric.metric_id}` === sortId,
      )?.value ?? null,
    );
    const rightValue = numeric(
      aggregateMetrics.find(
        (metric) => metric.solution_id === right && `${metric.family}:${metric.metric_id}` === sortId,
      )?.value ?? null,
    );
    return compareResultValues(leftValue, rightValue, descending);
  });
  const changeSort = (id: string) => {
    if (sortId === id) setDescending((value) => !value);
    else {
      setSortId(id);
      setDescending(true);
    }
  };

  return (
    <section className="evaluation-section" aria-labelledby="performance-heading">
      <div className="section-title-row">
        <div>
          <span className="section-kicker">Recorded results</span>
          <h2 id="performance-heading">Solution performance</h2>
          <p>Whole-run service summaries. Each metric is independent; quality covers evaluated samples only. Recorded costs and conditional estimates sort separately within each currency.</p>
          {Object.values(coverage ?? {}).some((item) => !item.comparable) && (
            <p className="inline-warning">
              Partial or different coverage is not a complete comparable result. Quality values describe evaluated cases only.
            </p>
          )}
        </div>
      </div>
      <div
        className="performance-scroll"
        tabIndex={0}
        role="region"
        aria-label="Solution performance, horizontally scrollable"
        data-horizontal-ledger
      >
        <table className="performance-table">
          <thead>
            <tr>
              <th scope="col" aria-sort={sortId === "solution" ? (descending ? "descending" : "ascending") : "none"}>
                <button type="button" onClick={() => changeSort("solution")}>Service</button>
              </th>
              <th scope="col">Evaluated coverage</th>
              {visibleDefinitions.map((definition) => (
                <th
                  key={`${definition.family}:${definition.id}`}
                  scope="col"
                  aria-sort={sortId === `${definition.family}:${definition.id}` ? (descending ? "descending" : "ascending") : "none"}
                >
                  <button type="button" onClick={() => changeSort(`${definition.family}:${definition.id}`)}>
                    {metricLabel(definition)}
                  </button>
                  <MetricHelp definition={definition} />
                </th>
              ))}
              {costColumns.map(column => <th key={column.key} scope="col" aria-sort={sortId === column.key ? (descending ? "descending" : "ascending") : "none"}>
                <button type="button" onClick={() => changeSort(column.key)}>{column.label}</button>
                <MetricHelp definition={{ id: column.key, name: column.label, family: "cost", unit: column.currency, description: column.estimated ? "Conditional current-rate estimate of recorded usage across all attempts; not billed cost." : "Recorded cost across all attempts. Unknown is not zero." }} />
              </th>)}
              <th scope="col">Status</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((solutionId) => (
              <tr key={solutionId}>
                <td>{benchmark.pipelines?.find(item => item.id === solutionId)?.name ?? solutionId}</td>
                <td>
                  {coverage?.[solutionId] ? (
                    <span>
                      {coverage[solutionId].evaluated_sample_ids.length}/{coverage[solutionId].expected_sample_ids.length}
                      {" "}{coverage[solutionId].complete ? "complete" : "partial"}
                    </span>
                  ) : "Not recorded"}
                </td>
                {visibleDefinitions.map((definition) => {
                  const metric = aggregateMetrics.find(
                    (item) =>
                      item.solution_id === solutionId &&
                      item.family === definition.family &&
                      item.metric_id === definition.id,
                  );
                  return (
                    <td key={`${definition.family}:${definition.id}`}>
                      {formatMetricValue(metric, definition)}
                    </td>
                  );
                })}
                {costColumns.map(column => {
                  const cost = run.costs?.solutions.find(item => item.solution_id === solutionId);
                  const fallback = aggregateMetrics.find(metric => metric.solution_id === solutionId && metric.family === "cost" && metric.metric_id === "cost.amount" && metric.unit === column.currency);
                  return <td key={column.key}><CostAmount currency={column.currency} estimated={column.estimated}
                    coverage={cost ? column.estimated ? cost.current_estimate : cost.recorded : undefined} fallback={fallback} /></td>;
                })}
                <td>
                  <span className={`runs-status status-${statusFor(solutionId)}`}>
                    <span aria-hidden="true" />
                    {statusFor(solutionId)}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <details className="cost-samples"><summary>Cost totals and pricing</summary><CostEvidence costs={run.costs} showSamples={false} solutionNames={Object.fromEntries(benchmark.pipelines?.map(item => [item.id, item.name]) ?? [])} /></details>
    </section>
  );
}

function ComparisonWorkspace({
  benchmark,
  run,
  evidence,
  artifactUrl,
  onInspect,
  initialSampleId,
}: {
  benchmark: BenchmarkManifest;
  run: Run;
  evidence: Evidence[];
  artifactUrl: (sha256: string) => string;
  onInspect: (evidence: Evidence, trigger: HTMLButtonElement) => void;
  initialSampleId: string;
}) {
  const resultEvidence = evidence.filter((item) => item.source === "result");
  const sampleId = initialSampleId;

  const findEvidence = (role: Evidence["role"], solutionId?: string) => {
    const recorded = resultEvidence.find(
      (item) =>
        item.task_name === sampleId &&
        item.role === role &&
        (solutionId === undefined || item.solution_id === solutionId),
    );
    return recorded ?? (
      role === "input" || role === "reference"
        ? run.sample_artifacts?.[sampleId]?.find((item) => item.role === role)
        : undefined
    );
  };
  const source = findEvidence("input");
  const reference = run.solution_ids
    .map((solutionId) => findEvidence("reference", solutionId))
    .find(Boolean);
  const solutionCount = Math.max(run.solution_ids.length, 1);
  const comparisonTableWidth = 104 + 158 + 155 * solutionCount;
  const narrowComparisonTableWidth = 68 + 92 + 132 * solutionCount;

  return (
    <section className="evaluation-section" aria-labelledby="comparison-heading">
      <div className="section-title-row">
        <div>
          <span className="section-kicker">Input and outputs</span>
          <h2 id="comparison-heading">Comparison inspector</h2>
          <p>Review the source beside result-level evaluator evidence.</p>
        </div>
      </div>
      <div className="comparison-workspace">
        <div className="comparison-source">
          <div className="column-heading">
            <div>
              <span className="section-kicker">Source</span>
              <h3>{benchmark.viewer?.title ?? "Input artifact"}</h3>
            </div>
            <span className="modality-label">{source?.modality ?? benchmark.input_modalities?.[0]}</span>
          </div>
          <ArtifactPreview
            evidence={source}
            viewer={benchmark.viewer}
            artifactUrl={artifactUrl}
            onInspect={onInspect}
          />
        </div>
        <div className="comparison-fields">
          <div className="column-heading">
            <div>
              <span className="section-kicker">Evaluator output</span>
              <h3>Reference comparison</h3>
            </div>
            <span>{run.solution_ids.length} services</span>
          </div>
          <div
            className="comparison-scroll"
            tabIndex={0}
            role="region"
            aria-label="Reference and solution output comparison, horizontally scrollable"
            data-horizontal-ledger
          >
            <table
              className="comparison-table"
              style={{
                "--comparison-table-width": `${comparisonTableWidth}px`,
                "--comparison-table-narrow-width": `${narrowComparisonTableWidth}px`,
              } as CSSProperties}
            >
              <thead>
                <tr>
                  <th className="field-column" data-sticky-track="field" scope="col">Field</th>
                  <th className="reference-column" data-sticky-track="reference" scope="col">Reference</th>
                  {run.solution_ids.map((solutionId) => (
                    <th scope="col" key={solutionId}>{benchmark.pipelines?.find(item => item.id === solutionId)?.name ?? solutionId}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                <tr>
                  <th className="field-column" data-sticky-track="field" scope="row">Output</th>
                  <td className="reference-column" data-sticky-track="reference">
                    <ArtifactValue
                      evidence={reference}
                      artifactUrl={artifactUrl}
                    />
                  </td>
                  {run.solution_ids.map((solutionId) => {
                    const output =
                      findEvidence("comparison", solutionId) ??
                      findEvidence("parsed", solutionId);
                    return (
                      <td key={solutionId}>
                        {output && (
                          <>
                            <span className="comparison-state recorded" aria-hidden="true" />
                            <span className="sr-only">Recorded output</span>
                          </>
                        )}
                        <ResultComparison
                          evidence={output}
                          viewer={benchmark.viewer}
                          artifactUrl={artifactUrl}
                        />
                      </td>
                    );
                  })}
                </tr>
                <tr>
                  <th className="field-column" data-sticky-track="field" scope="row">Cost</th>
                  <td className="reference-column" data-sticky-track="reference">Not applicable</td>
                  {run.solution_ids.map(solutionId => <td key={solutionId}>
                    <CostCell cost={run.costs?.samples[sampleId]?.find(item => item.solution_id === solutionId)} />
                  </td>)}
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </section>
  );
}

export function EvaluationWorkspace({
  benchmark,
  run,
  report,
  eventCount,
  artifactUrl,
  onConfigure,
  onInspect,
  loadDiagnostics,
  onUnavailable,
}: {
  benchmark: BenchmarkManifest;
  run?: Run;
  report?: RunReport;
  eventCount: number;
  artifactUrl: (sha256: string) => string;
  onConfigure: () => void;
  onInspect: (evidence: Evidence, trigger: HTMLButtonElement) => void;
  loadDiagnostics?: (id: string, signal: AbortSignal) => Promise<Run>;
  onUnavailable?: (id: string, reason: "expired" | "missing") => void;
}) {
  const [detailTab, setDetailTab] = useState<DetailTab>("evidence");
  const [family, setFamily] = useState<MetricFamily>("quality");
  const [operationsRunId, setOperationsRunId] = useState<string>();
  const operationsOpen = Boolean(run && operationsRunId === run.id);
  const [selected, setSelected] = useState<{ row: ResultRow; trigger: HTMLButtonElement; runId: string }>();
  const drilldownHeading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (selected?.runId === run?.id) drilldownHeading.current?.focus();
  }, [selected, run?.id]);
  const closeDrilldown = () => {
    if (selected?.trigger.isConnected) selected.trigger.focus();
    else document.getElementById("results-heading")?.focus();
    setSelected(undefined);
  };
  const moveDetailFocus = (
    event: KeyboardEvent<HTMLButtonElement>,
    current: DetailTab,
  ) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const index = detailOrder.indexOf(current);
    const next =
      event.key === "Home"
        ? detailOrder[0]
        : event.key === "End"
          ? detailOrder.at(-1)!
          : detailOrder[
              (index + (event.key === "ArrowRight" ? 1 : -1) +
                detailOrder.length) %
                detailOrder.length
            ];
    setDetailTab(next);
    document.getElementById(`detail-tab-${next}`)?.focus();
  };
  const moveFamilyFocus = (
    event: KeyboardEvent<HTMLButtonElement>,
    current: MetricFamily,
  ) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const index = familyOrder.indexOf(current);
    const next =
      event.key === "Home"
        ? familyOrder[0]
        : event.key === "End"
          ? familyOrder.at(-1)!
          : familyOrder[
              (index + (event.key === "ArrowRight" ? 1 : -1) + familyOrder.length) %
                familyOrder.length
            ];
    setFamily(next);
    document.getElementById(`tab-${next}`)?.focus();
  };

  if (!run) {
    return (
      <div className="view-shell">
        <header className="evaluation-empty">
          <GridIcon />
          <h1>Evaluation detail</h1>
          <p>Select an existing run or configure a new benchmark execution.</p>
          <button className="primary-button" type="button" onClick={onConfigure}>
            <PlayIcon />
            Configure run
          </button>
        </header>
      </div>
    );
  }

  const metrics = report?.families.flatMap((item) => item.metrics) ?? run.metrics ?? [];
  const definitionsByKey = new Map<string, MetricDefinition>();
  const keyFor = (definition: MetricDefinition) =>
    `${definition.family}:${definition.id}`;
  for (const definition of report?.definitions ?? []) {
    definitionsByKey.set(keyFor(definition), definition);
  }
  for (const definition of run.metric_definitions ?? []) {
    definitionsByKey.set(keyFor(definition), definition);
  }
  if (definitionsByKey.size === 0) {
    for (const definition of benchmark.metrics) {
      definitionsByKey.set(keyFor(definition), definition);
    }
  }
  if (!definitionsByKey.has("cost:cost.amount")) {
    definitionsByKey.set("cost:cost.amount", { id: "cost.amount", name: "Cost", family: "cost", direction: "neutral" });
  }
  for (const metric of metrics) {
    const key = `${metric.family}:${metric.metric_id}`;
    if (!definitionsByKey.has(key)) {
      definitionsByKey.set(key, { id: metric.metric_id, name: metric.metric_id.replace(/[._]/g, " "), family: metric.family, unit: metric.unit, direction: "neutral" });
    }
  }
  const definitions = [...definitionsByKey.values()];
  const aggregates = metrics.filter((metric) => !metric.task_id);
  const firstQualityDefinition = definitions.find(
    (definition) =>
      definition.family === "quality" &&
      aggregates.some((metric) => metric.family === "quality" && metric.metric_id === definition.id),
  );
  const qualityCandidates = aggregates.filter(
    (metric) =>
      metric.family === "quality" && metric.metric_id === firstQualityDefinition?.id &&
      numeric(metric.value) !== undefined &&
      (!metric.solution_id || report?.coverage?.[metric.solution_id]?.comparable !== false),
  );
  const bestQuality =
    firstQualityDefinition?.direction === "neutral"
      ? undefined
      : qualityCandidates.sort((left, right) =>
          firstQualityDefinition?.direction === "lower"
            ? (numeric(left.value) ?? Infinity) -
              (numeric(right.value) ?? Infinity)
            : (numeric(right.value) ?? -Infinity) -
              (numeric(left.value) ?? -Infinity),
        )[0];
  const displayedCost = costHeadline(run.costs, aggregates);
  const latencyMetricId = aggregates.some((metric) => metric.family === "latency" && metric.metric_id === "latency.provider_ms")
    ? "latency.provider_ms"
    : "latency_ms";
  const latencyMetrics = aggregates.filter((metric) => metric.family === "latency" && metric.metric_id === latencyMetricId);
  const bestLatency = [...latencyMetrics].sort(
    (left, right) => (numeric(left.value) ?? Infinity) - (numeric(right.value) ?? Infinity),
  )[0];
  const usageMetric = aggregates.find((metric) => metric.family === "usage");
  const outcomes = run.finalized_results ?? run.tasks ?? [];
  const historical = run.record_source === "finalized_report";
  const completedTasks = outcomes.filter(
    (task) => task.status === "succeeded" || task.status === "poisoned",
  ).length;
  const totalTasks = outcomes.length;
  const completion = totalTasks === 0 ? undefined : completedTasks / totalTasks;
  const evidence = report?.evidence ?? run.evidence ?? [];
  const usageNotes = new Map<string, string[]>();
  for (const [solutionId, reasons] of Object.entries(report?.usage_uncertainty ?? {})) {
    for (const reason of new Set(reasons)) {
      usageNotes.set(reason, [...(usageNotes.get(reason) ?? []), benchmark.pipelines?.find(item => item.id === solutionId)?.name ?? solutionId]);
    }
  }

  return (
    <div className="view-shell evaluation-view">
      <p className="sr-only" role="status" aria-live="polite" aria-atomic="true">
        Run {run.status}. {completedTasks} of {totalTasks} {historical ? "recorded outcomes" : "tasks"} terminal.
      </p>
      <header className="evaluation-toolbar">
        <div>
          <span className="section-kicker">{benchmark.name}</span>
          <h1>Evaluation detail</h1>
          <p>
            Run <code title={run.id}>{formatRunId(run.id)}</code> · started {formatCompactDate(run.created_at)}
          </p>
          <RetentionNotice {...run} />
          {benchmark.dataset_availability?.revision && (
            <p>Dataset revision <code>{benchmark.dataset_availability.revision.id}</code></p>
          )}
        </div>
        <div className="evaluation-actions">
          <span className={`runs-status status-${run.status}`}>
            <span aria-hidden="true" />
            {run.status}
          </span>
          <button className="secondary-button" type="button" onClick={onConfigure}>
            <PlayIcon />
            New run
          </button>
          {run.report_artifact_id && (
            <a
              className="primary-button"
              href={`/api/runs/${run.id}/report?format=json`}
              download
            >
              <DownloadIcon />
              Download report
            </a>
          )}
        </div>
      </header>
      {run.runtime_error && <p className="inline-error" role="alert">{run.runtime_error}</p>}
      {historical && <p className="cost-note">Historical record from the finalized report. Outcomes, metrics and costs describe recorded evidence; current task diagnostics load only on request.</p>}
      {run.live_detail_reason && <p className="cost-note">{run.live_detail_reason}</p>}
      <p className="cost-note">Whole-run summary across all selected services. The result filter below does not change these summaries.</p>

      <section className="evaluation-kpis" aria-label="Independent metric summary">
        <MetricCard
          family="quality"
          label={firstQualityDefinition?.name ?? "Quality"}
          value={formatMetricValue(bestQuality, firstQualityDefinition)}
          detail={
            firstQualityDefinition?.direction === "neutral"
              ? "No ranking direction"
              : bestQuality?.solution_id ?? "No complete comparable result"
          }
        />
        <MetricCard
          family="cost"
          label="Cost"
          value={displayedCost.value}
          detail={displayedCost.detail}
        />
        <MetricCard
          family="latency"
          label={latencyMetricId === "latency.provider_ms" ? "Provider request time" : "Recorded attempt time"}
          value={formatMetricValue(bestLatency)}
          detail={bestLatency?.solution_id ?? "No completed result"}
        />
        <MetricCard
          family="usage"
          label={
            definitions.find((definition) => definition.family === "usage" && definition.id === usageMetric?.metric_id)?.name ??
            "Recorded usage"
          }
          value={formatMetricValue(usageMetric)}
          detail={usageMetric?.solution_id ?? "No usage dimensions"}
        />
        <MetricCard
          family="reliability"
          label="Task completion"
          value={
            completion === undefined
              ? "Not available"
              : formatMetricValue({
                  metric_id: "completion",
                  family: "reliability",
                  value: completion,
                  unit: "ratio",
                })
          }
          detail={historical
            ? `${completedTasks} of ${totalTasks} recorded outcomes · ${(run.finalized_results ?? []).reduce((count, result) => count + result.attempts, 0)} recorded attempts`
            : `${completedTasks} of ${totalTasks} terminal · ${eventCount.toLocaleString()} events`}
        />
      </section>

      <SolutionPerformance
        key={`performance:${run.id}`}
        benchmark={benchmark}
        run={run}
        metrics={metrics}
        definitions={definitions}
        coverage={report?.coverage}
      />
      <RunResults key={`results:${run.id}`} benchmark={benchmark} run={run} metrics={metrics} definitions={definitions} evidence={evidence} coverage={report?.coverage}
        onSelect={(row, trigger) => setSelected({ row, trigger, runId: run.id })} />
      {selected?.runId === run.id && <section className="result-drilldown" aria-labelledby="result-detail-heading" onKeyDown={event => {
        if (event.key === "Escape") { event.stopPropagation(); closeDrilldown(); }
      }}>
        <div className="section-title-row"><h2 id="result-detail-heading" tabIndex={-1} ref={drilldownHeading}>Result detail: {selected.row.sampleId}</h2>
          <button type="button" className="secondary-button" onClick={closeDrilldown}>Back to results</button></div>
        <ComparisonWorkspace key={selected.row.key} benchmark={benchmark} run={{ ...run, solution_ids: [selected.row.solutionId] }} initialSampleId={selected.row.sampleId}
          evidence={evidence} artifactUrl={artifactUrl} onInspect={onInspect} />
      </section>}

      {usageNotes.size > 0 && <details className="usage-context">
        <summary>Usage and billing context</summary>
        <p>Submitted work and calls do not establish billed usage. These caveats do not change recorded results.</p>
        {[...usageNotes].map(([reason, services]) => <p key={reason}>{reason} <small>Applies to: {services.join(", ")}.</small></p>)}
      </details>}
      <details className="operational-disclosure" open={operationsOpen}>
        <summary onClick={event => { event.preventDefault(); setOperationsRunId(operationsOpen ? undefined : run.id); }}>Operational details</summary>
      {operationsOpen &&
      <section className="evaluation-section operational-section" aria-labelledby="operational-heading">
        <div className="section-title-row">
          <div>
            <span className="section-kicker">Audit trail</span>
            <h2 id="operational-heading">Artifacts and provenance</h2>
            <p>Inspect artifacts, attempts, manifest hashes, and report provenance.</p>
          </div>
          {!historical && <ExecutionProgress run={{ ...run, runtime_error: undefined }} />}
        </div>
        {historical && loadDiagnostics && onUnavailable && <CurrentDiagnostics key={run.id} runId={run.id} load={loadDiagnostics} onUnavailable={onUnavailable} />}
        <div className="detail-tabs" role="tablist" aria-label="Operational detail views">
          <button
            id="detail-tab-evidence"
            role="tab"
            type="button"
            aria-selected={detailTab === "evidence"}
            aria-controls="detail-panel-evidence"
            tabIndex={detailTab === "evidence" ? 0 : -1}
            onClick={() => setDetailTab("evidence")}
            onKeyDown={(event) => moveDetailFocus(event, "evidence")}
          >
            Evidence artifacts <span>{evidence.length}</span>
          </button>
          <button
            id="detail-tab-manifest"
            role="tab"
            type="button"
            aria-selected={detailTab === "manifest"}
            aria-controls="detail-panel-manifest"
            tabIndex={detailTab === "manifest" ? 0 : -1}
            onClick={() => setDetailTab("manifest")}
            onKeyDown={(event) => moveDetailFocus(event, "manifest")}
          >
            Run manifest
          </button>
        </div>
        <div
          id="detail-panel-evidence"
          role="tabpanel"
          aria-labelledby="detail-tab-evidence"
          hidden={detailTab !== "evidence"}
        >
            <div className="family-tabs" role="tablist" aria-label="Metric families">
              {familyOrder.map((item) => (
                <button
                  key={item}
                  id={`tab-${item}`}
                  type="button"
                  role="tab"
                  aria-selected={family === item}
                  aria-controls={`panel-${item}`}
                  tabIndex={family === item ? 0 : -1}
                  onClick={() => setFamily(item)}
                  onKeyDown={(event) => moveFamilyFocus(event, item)}
                >
                  {familyLabel(item)}
                </button>
              ))}
            </div>
            {family === "cost" ? <section id="panel-cost" role="tabpanel" aria-labelledby="tab-cost"><CostEvidence costs={run.costs} solutionNames={Object.fromEntries(benchmark.pipelines?.map(item => [item.id, item.name]) ?? [])} /></section> : <EvidenceLedger
              evidence={evidence}
              metrics={metrics}
              definitions={definitions}
              family={family}
              qualityComparability={report?.quality_comparability ?? run.quality_comparability}
              onInspect={onInspect}
            />}
        </div>
        <div
          id="detail-panel-manifest"
          className="manifest-panel"
          role="tabpanel"
          aria-labelledby="detail-tab-manifest"
          hidden={detailTab !== "manifest"}
        >
            <dl>
              <div><dt>Run ID</dt><dd><code>{run.id}</code></dd></div>
              <div><dt>Benchmark</dt><dd><code>{run.benchmark_id}@{run.benchmark_version}</code></dd></div>
              <div><dt>Pipelines</dt><dd>{benchmark.pipelines?.map(item => `${item.id}: ${item.mode}`).join("; ") ?? "Not recorded"}</dd></div>
              <div><dt>Manifest artifact</dt><dd><code>{run.manifest_artifact_id}</code></dd></div>
              <div><dt>Report artifact</dt><dd><code>{run.report_artifact_id ?? "Pending"}</code></dd></div>
              <div><dt>Renderer</dt><dd><code>{benchmark.viewer?.id}@{benchmark.viewer?.version}</code></dd></div>
            </dl>
        </div>
      </section>}
      </details>

      <footer className="provenance-footer">
        <div>
          <span className="section-kicker">Evidence provenance</span>
          <strong>Immutable run {formatRunId(run.id)}</strong>
        </div>
        <p>
          Metrics link to content-addressed artifacts. Unknown cost remains unknown,
          and metric families are never collapsed into a universal score.
        </p>
      </footer>
    </div>
  );
}
