import { useMemo } from "react";
import type {
  Compatibility,
  MetricDefinition,
  MetricFamily,
  MetricValue,
} from "../types";
import { formatMetricValue } from "../format";

function displayValue(
  metric: MetricValue,
  definition?: MetricDefinition,
) {
  if (metric.value === null) {
    return (
      <span className="aggregate-unknown">
        <span aria-hidden="true">—</span> Unknown
      </span>
    );
  }
  return <span>{formatMetricValue(metric, definition)}</span>;
}

export function AggregateSummary({
  family,
  metrics,
  definitions,
  qualityComparability,
}: {
  family: MetricFamily;
  metrics: MetricValue[];
  definitions: MetricDefinition[];
  qualityComparability?: Record<string, Compatibility>;
}) {
  const aggregates = useMemo(
    () =>
      metrics
        .filter((metric) => metric.family === family && !metric.task_id)
        .sort((left, right) => {
          const solutionOrder = (left.solution_id ?? "").localeCompare(
            right.solution_id ?? "",
          );
          return (
            solutionOrder ||
            left.metric_id.localeCompare(right.metric_id)
          );
        }),
    [family, metrics],
  );
  const definitionsById = new Map(
    definitions
      .filter((definition) => definition.family === family)
      .map((definition) => [definition.id, definition]),
  );

  return (
    <section
      className="aggregate-summary"
      aria-labelledby={`aggregate-heading-${family}`}
    >
      <div className="aggregate-heading">
        <h2 id={`aggregate-heading-${family}`}>Report aggregates</h2>
        <span>{family}</span>
      </div>
      {aggregates.length > 0 ? (
        <ul aria-live="polite">
          {aggregates.map((metric) => {
            const definition = definitionsById.get(metric.metric_id);
            return (
              <li
                key={`${metric.solution_id ?? "all"}:${metric.metric_id}`}
              >
                <code>{metric.solution_id ?? "All solutions"}</code>
                <span>{definition?.name ?? metric.metric_id}</span>
                <strong>{displayValue(metric, definition)}</strong>
                {family === "quality" &&
                  metric.solution_id &&
                  qualityComparability?.[metric.solution_id]
                    ?.comparable_quality === false && (
                    <small>
                      Not comparable:{" "}
                      {qualityComparability[metric.solution_id].reason}
                    </small>
                  )}
              </li>
            );
          })}
        </ul>
      ) : (
        <p>Finalized report aggregates are not available.</p>
      )}
    </section>
  );
}
