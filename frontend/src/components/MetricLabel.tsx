import type { MetricDefinition } from "../types";

export function metricLabel(definition: MetricDefinition): string {
  if (definition.name.length <= 24) return definition.name;
  return `${definition.name.slice(0, 21).trimEnd()}...`;
}

export function MetricHelp({ definition }: { definition: MetricDefinition }) {
  return <details className="metric-help">
    <summary aria-label={`About ${definition.name}`}>?</summary>
    <p>{definition.name}. {definition.description ?? `${definition.family} metric`}
      {definition.unit ? ` (${definition.unit}).` : "."}
      {definition.direction && definition.direction !== "neutral" ? ` ${definition.direction === "lower" ? "Lower" : "Higher"} values are preferable within this metric only.` : ""}
    </p>
  </details>;
}
