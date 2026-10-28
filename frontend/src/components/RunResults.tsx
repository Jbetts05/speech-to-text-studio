import { useMemo, useState } from "react";
import type { BenchmarkManifest, CostCoverage, Evidence, MetricDefinition, MetricValue, RecordedResult, Run, RunReport, TaskRun } from "../types";
import { formatMetricValue } from "../format";
import { compareValues } from "./EvidenceLedger";
import { CostAmount } from "./CostEvidence";
import { MetricHelp, metricLabel } from "./MetricLabel";
import "./run-results.css";

export interface ResultRow {
  key: string;
  sampleId: string;
  solutionId: string;
  task?: TaskRun;
  result?: RecordedResult;
  metrics: MetricValue[];
}

export function resultRows(run: Run, metrics: MetricValue[], evidence: Evidence[], coverage?: RunReport["coverage"]): ResultRow[] {
  const samples = new Set([
    ...(run.tasks ?? []).flatMap(task => task.sample_id ? [task.sample_id] : []),
    ...(run.finalized_results ?? []).map(result => result.sample_id),
    ...Object.keys(run.sample_artifacts ?? {}),
    ...Object.values(coverage ?? {}).flatMap(item => item.expected_sample_ids),
    ...evidence.flatMap(item => item.source === "result" && item.task_name ? [item.task_name] : []),
  ]);
  const tasks = new Map((run.tasks ?? []).map(task => [JSON.stringify([task.sample_id, task.solution_id]), task]));
  const results = new Map((run.finalized_results ?? []).map(result => [JSON.stringify([result.sample_id, result.solution_id]), result]));
  return [...samples].flatMap(sampleId => run.solution_ids.map(solutionId => {
    const key = JSON.stringify([sampleId, solutionId]);
    const task = tasks.get(key);
    const recorded = results.get(key);
    const result = evidence.find(item => item.source === "result" && item.task_name === sampleId && item.solution_id === solutionId);
    const taskId = recorded?.task_id ?? task?.task_id ?? result?.task_id;
    // Result metrics precede attempt telemetry in the wire projection. Never sum
    // those duplicated observations; retry-inclusive money comes from costs.
    const values = new Map<string, MetricValue>();
    for (const metric of [...(result?.metrics ?? []), ...metrics.filter(item => taskId && item.task_id === taskId && (!item.solution_id || item.solution_id === solutionId))]) {
      const id = `${metric.family}:${metric.metric_id}`;
      if (!values.has(id)) values.set(id, metric);
    }
    return { key, sampleId, solutionId, task, result: recorded, metrics: [...values.values()] };
  }));
}

function rowStatus(row: ResultRow): string {
  if (row.result) return row.result.status === "succeeded" ? "Succeeded" : "Failed";
  if (!row.task) return "Missing";
  if (row.task.status === "poisoned") return "Failed";
  if (row.task.status === "succeeded") return "Succeeded";
  if (row.task.status === "leased") return "Running";
  return row.task.wait_state === "cooldown" ? "Cooldown" : row.task.wait_state === "retry" ? "Retry pending" : "Pending";
}

export function compareResultValues(a: string | number | null | undefined, b: string | number | null | undefined, descending: boolean): number {
  const missing = (value: typeof a) => value === undefined || value === null || value === "" || (typeof value === "number" && !Number.isFinite(value));
  if (missing(a)) return missing(b) ? 0 : 1;
  if (missing(b)) return -1;
  return compareValues(a!, b!, descending ? "descending" : "ascending");
}

export function RunResults({ benchmark, run, metrics, definitions, evidence, coverage, onSelect }: {
  benchmark: BenchmarkManifest;
  run: Run;
  metrics: MetricValue[];
  definitions: MetricDefinition[];
  evidence: Evidence[];
  coverage?: RunReport["coverage"];
  onSelect: (row: ResultRow, trigger: HTMLButtonElement) => void;
}) {
  const [service, setService] = useState("");
  const [sort, setSort] = useState({ key: "sample", descending: false });
  const allRows = useMemo(() => resultRows(run, metrics, evidence, coverage), [run, metrics, evidence, coverage]);
  const labelFor = (id: string) => benchmark.pipelines?.find(item => item.id === id)?.name ?? id;
  const activeService = run.solution_ids.includes(service) ? service : "";
  const columns = definitions.filter(definition => definition.family !== "cost" &&
    (definition.family === "quality" || allRows.some(row => row.metrics.some(metric => metric.family === definition.family && metric.metric_id === definition.id && metric.value !== null))));
  const currencies = [...new Set([
    ...Object.keys(run.costs?.recorded_total.amounts ?? {}),
    ...Object.keys(run.costs?.current_estimate_total.amounts ?? {}),
    ...Object.values(run.costs?.samples ?? {}).flatMap(samples => samples.flatMap(cost => [...Object.keys(cost.recorded.amounts), ...Object.keys(cost.current_estimate.amounts)])),
    ...allRows.flatMap(row => row.metrics.flatMap(metric => metric.family === "cost" && metric.unit ? [metric.unit] : [])),
  ])];
  if (!currencies.length) currencies.push("");
  const costFor = (row: ResultRow, estimated: boolean): CostCoverage | undefined => {
    const cost = run.costs?.samples[row.sampleId]?.find(item => item.solution_id === row.solutionId);
    return estimated ? cost?.current_estimate : cost?.recorded;
  };
  const moneyValue = (row: ResultRow, currency: string, estimated: boolean) => {
    const cost = costFor(row, estimated);
    if (cost) return cost.amounts[currency];
    // No final-attempt fallback: it can omit billed retries.
    return undefined;
  };
  const valueFor = (row: ResultRow) => {
    if (sort.key === "sample") return row.sampleId;
    if (sort.key === "service") return labelFor(row.solutionId);
    if (sort.key === "status") return rowStatus(row);
    if (sort.key.startsWith("recorded:")) return moneyValue(row, sort.key.slice(9), false);
    if (sort.key.startsWith("estimate:")) return moneyValue(row, sort.key.slice(9), true);
    return row.metrics.find(metric => `${metric.family}:${metric.metric_id}` === sort.key)?.value;
  };
  const rows = allRows.filter(row => !activeService || row.solutionId === activeService)
    .sort((a, b) => compareResultValues(valueFor(a), valueFor(b), sort.descending) || a.key.localeCompare(b.key, undefined, { numeric: true }));
  const changeSort = (key: string) => setSort(current => ({ key, descending: current.key === key && !current.descending }));
  const sortState = (key: string) => sort.key === key ? sort.descending ? "descending" : "ascending" : "none";
  return <section className="evaluation-section run-results" aria-labelledby="results-heading">
    <div className="section-title-row">
      <div><h2 id="results-heading" tabIndex={-1}>All results</h2><p id="result-scope">{rows.length} of {allRows.length} results shown{activeService ? ` for ${labelFor(activeService)}` : " across all services"}. Summaries above cover the whole run.</p></div>
      <label className="sample-picker"><span>Service</span><select value={activeService} onChange={event => setService(event.target.value)}>
        <option value="">All services</option>
        {run.solution_ids.map(id => <option key={id} value={id}>{labelFor(id)}</option>)}
      </select></label>
    </div>
    <p className="cost-note">Costs include recorded retries. Estimates are conditional, not billed charges. Unknown values are not zero. Select a result to inspect its input and comparison.</p>
    <div className="results-scroll" tabIndex={0} role="region" aria-label="All results, horizontally scrollable" aria-describedby="result-scope" data-horizontal-ledger>
      <table className="results-table">
        <caption className="sr-only">One row per sample and service. Sort each metric independently; missing values stay last.</caption>
        <thead><tr>
          <th scope="col" data-sticky-track="sample" aria-sort={sortState("sample")}><button onClick={() => changeSort("sample")}>Sample</button></th>
          <th scope="col" aria-sort={sortState("service")}><button onClick={() => changeSort("service")}>Service</button></th>
          {columns.map(definition => <th key={`${definition.family}:${definition.id}`} scope="col" aria-sort={sortState(`${definition.family}:${definition.id}`)}><button onClick={() => changeSort(`${definition.family}:${definition.id}`)}>{metricLabel(definition)}</button><MetricHelp definition={definition} /></th>)}
          {currencies.map(currency => <th key={`recorded:${currency}`} scope="col" aria-sort={sortState(`recorded:${currency}`)}><button onClick={() => changeSort(`recorded:${currency}`)}>Cost{currency ? ` (${currency})` : ""}</button></th>)}
          {run.costs && currencies.map(currency => <th key={`estimate:${currency}`} scope="col" aria-sort={sortState(`estimate:${currency}`)}><button onClick={() => changeSort(`estimate:${currency}`)}>Est. cost{currency ? ` (${currency})` : ""}</button></th>)}
          <th scope="col" aria-sort={sortState("status")}><button onClick={() => changeSort("status")}>Status</button></th>
        </tr></thead>
        <tbody>{rows.map(row => <tr key={row.key}>
          <td data-sticky-track="sample"><button className="result-link" aria-label={`Inspect ${row.sampleId} / ${labelFor(row.solutionId)}`} onClick={event => onSelect(row, event.currentTarget)}><span>{row.sampleId}</span><small>{labelFor(row.solutionId)}</small></button></td>
          <td>{labelFor(row.solutionId)}
            {run.quality_comparability?.[row.solutionId]?.comparable_quality === false && <details>
              <summary>Quality not comparable</summary><p>{run.quality_comparability[row.solutionId].reason}</p>
            </details>}
          </td>
          {columns.map(definition => <td key={`${definition.family}:${definition.id}`}>{formatMetricValue(row.metrics.find(metric => metric.family === definition.family && metric.metric_id === definition.id), definition)}</td>)}
          {currencies.map(currency => <td key={`recorded:${currency}`}><CostAmount coverage={costFor(row, false)} currency={currency} /></td>)}
          {run.costs && currencies.map(currency => <td key={`estimate:${currency}`}><CostAmount coverage={costFor(row, true)} currency={currency} estimated /></td>)}
          <td><span>{rowStatus(row)}</span>{(row.result?.error || row.task?.message) && <details><summary>Failure reason</summary><p>{row.result?.error ?? row.task?.message}</p></details>}</td>
        </tr>)}
        {!rows.length && <tr><td data-sticky-track="sample">No results</td><td colSpan={columns.length + currencies.length * (run.costs ? 2 : 1) + 2}>Sample and service results will appear when the run detail is available.</td></tr>}
        </tbody>
      </table>
    </div>
  </section>;
}
