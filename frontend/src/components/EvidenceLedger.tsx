import { useMemo, useState } from "react";
import type {
  Compatibility,
  Evidence,
  MetricDefinition,
  MetricFamily,
  MetricValue,
} from "../types";
import { formatMetricValue } from "../format";

type SortDirection = "ascending" | "descending";
type SortKey = "evidence" | "task" | "solution" | "modality" | string;

function displayMetric(
  evidence: Evidence,
  definition: MetricDefinition,
): string {
  const value = evidence.metrics?.find(
    (metric) => metric.family === definition.family && metric.metric_id === definition.id,
  );
  if (!value || value.value === null) return "—";
  return formatMetricValue(value, definition);
}

function comparableMetric(
  evidence: Evidence,
  metricId: string,
  family: MetricFamily,
): string | number {
  const value = evidence.metrics?.find(
    (metric) => metric.family === family && metric.metric_id === metricId,
  )?.value;
  return value ?? "";
}

export function compareValues(
  a: string | number,
  b: string | number,
  direction: SortDirection,
): number {
  const factor = direction === "ascending" ? 1 : -1;
  const decimal = /^[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$/i;
  const leftNumber =
    typeof a === "number"
      ? a
      : decimal.test(a.trim())
        ? Number(a)
        : undefined;
  const rightNumber =
    typeof b === "number"
      ? b
      : decimal.test(b.trim())
        ? Number(b)
        : undefined;
  if (
    leftNumber !== undefined &&
    rightNumber !== undefined &&
    Number.isFinite(leftNumber) &&
    Number.isFinite(rightNumber)
  ) {
    return (leftNumber - rightNumber) * factor;
  }
  return String(a).localeCompare(String(b), undefined, {
    numeric: true,
    sensitivity: "base",
  }) * factor;
}

export function evidenceMetricValues(
  evidence: Evidence[],
  metrics: MetricValue[],
): Evidence[] {
  return evidence.map((item) => {
    if (item.attempt !== undefined) return item;
    return {
      ...item,
      metrics: [
        ...(item.metrics ?? []),
        ...metrics.filter(
          (metric) =>
            metric.task_id === item.task_id &&
            (!metric.solution_id || metric.solution_id === item.solution_id),
        ),
      ],
    };
  });
}

export function EvidenceLedger({
  evidence,
  metrics,
  definitions,
  family,
  qualityComparability,
  onInspect,
}: {
  evidence: Evidence[];
  metrics: MetricValue[];
  definitions: MetricDefinition[];
  family: MetricFamily;
  qualityComparability?: Record<string, Compatibility>;
  onInspect: (evidence: Evidence, trigger: HTMLButtonElement) => void;
}) {
  const [sort, setSort] = useState<{
    key: SortKey;
    direction: SortDirection;
  }>({ key: "evidence", direction: "ascending" });
  const [scope, setScope] = useState<"comparisons" | "all">("comparisons");
  const rows = useMemo(() => {
    const relevantEvidence =
      scope === "comparisons"
        ? evidence.filter(
            (item) =>
              item.source === "result" && item.role === "comparison",
          )
        : evidence;
    const merged = evidenceMetricValues(relevantEvidence, metrics);
    return [...merged].sort((a, b) => {
      const values: Record<string, [string | number, string | number]> = {
        evidence: [a.label, b.label],
        task: [a.task_name ?? a.task_id, b.task_name ?? b.task_id],
        solution: [
          a.solution_name ?? a.solution_id ?? "",
          b.solution_name ?? b.solution_id ?? "",
        ],
        modality: [a.modality, b.modality],
      };
      const [left, right] = values[sort.key] ?? [
        comparableMetric(a, sort.key, family),
        comparableMetric(b, sort.key, family),
      ];
      return compareValues(left, right, sort.direction);
    });
  }, [evidence, metrics, scope, sort, family]);
  const familyDefinitions = definitions.filter(
    (definition) =>
      definition.family === family &&
      rows.some((row) =>
        row.metrics?.some(
          (metric) =>
            metric.family === definition.family && metric.metric_id === definition.id && metric.value !== null,
        ),
      ),
  );

  const changeSort = (key: SortKey) => {
    setSort((current) => ({
      key,
      direction:
        current.key === key && current.direction === "ascending"
          ? "descending"
          : "ascending",
    }));
  };

  const sortValue = (key: SortKey) =>
    sort.key === key ? sort.direction : "none";

  return (
    <section
      id={`panel-${family}`}
      role="tabpanel"
      aria-labelledby={`tab-${family}`}
      className="ledger-panel"
    >
      <div className="ledger-heading">
        <div>
          <h2>Evidence ledger</h2>
          <p>{rows.length} useful evidence records · {familyDefinitions.length} applicable metrics</p>
        </div>
        <div className="ledger-controls">
          <span className="family-marker">{family}</span>
          <label>
            <span className="sr-only">Evidence scope</span>
            <select
              value={scope}
              onChange={(event) =>
                setScope(event.target.value as "comparisons" | "all")
              }
            >
              <option value="comparisons">Result comparisons</option>
              <option value="all">All operational artifacts</option>
            </select>
          </label>
        </div>
      </div>
      <div
        className="ledger-scroll"
        tabIndex={0}
        role="region"
        aria-label={`${family} evidence ledger, horizontally scrollable`}
        data-horizontal-ledger
      >
        <table>
          <caption className="visually-hidden">
            Evidence and {family} metrics. Activate an evidence name to inspect
            its artifact.
          </caption>
          <thead>
            <tr>
              <th
                scope="col"
                className="sticky-track"
                data-sticky-track="evidence"
                aria-sort={sortValue("evidence")}
              >
                <button type="button" onClick={() => changeSort("evidence")}>
                  Evidence
                </button>
              </th>
              <th scope="col" aria-sort={sortValue("task")}>
                <button type="button" onClick={() => changeSort("task")}>
                  Task
                </button>
              </th>
              <th scope="col" aria-sort={sortValue("solution")}>
                <button type="button" onClick={() => changeSort("solution")}>
                  Solution
                </button>
              </th>
              <th scope="col" aria-sort={sortValue("modality")}>
                <button type="button" onClick={() => changeSort("modality")}>
                  Modality
                </button>
              </th>
              {familyDefinitions.map((definition) => (
                <th
                  scope="col"
                  key={`${definition.family}:${definition.id}`}
                  aria-sort={sortValue(definition.id)}
                >
                  <button
                    type="button"
                    title={definition.description}
                    onClick={() => changeSort(definition.id)}
                  >
                    {definition.name}
                  </button>
                </th>
              ))}
              <th scope="col">Provenance</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((item) => (
              <tr key={item.id}>
                <td className="sticky-track" data-sticky-track="evidence">
                  <button
                    type="button"
                    className="evidence-link"
                    onClick={(event) => onInspect(item, event.currentTarget)}
                  >
                    <span>{item.label}</span>
                    <span aria-hidden="true">↗</span>
                  </button>
                </td>
                <td>{item.task_name ?? item.task_id}</td>
                <td>
                  {item.solution_name ?? item.solution_id ?? "Unspecified"}
                  {family === "quality" &&
                    item.solution_id &&
                    qualityComparability?.[item.solution_id]
                      ?.comparable_quality === false && (
                      <span
                        className="comparison-warning"
                        title={qualityComparability[item.solution_id].reason}
                      >
                        Not comparable
                      </span>
                    )}
                </td>
                <td>
                  <span className="modality-label">{item.modality}</span>
                </td>
                {familyDefinitions.map((definition) => (
                  <td key={`${definition.family}:${definition.id}`}>
                    {displayMetric(item, definition)}
                  </td>
                ))}
                <td>
                  {item.source}
                  {item.source === "attempt" && item.attempt
                    ? ` · attempt ${item.attempt}`
                    : ""}
                </td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td className="sticky-track" data-sticky-track="evidence">No evidence</td>
                <td colSpan={Math.max(4, familyDefinitions.length + 4)}>
                  Evidence will appear as tasks complete.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
