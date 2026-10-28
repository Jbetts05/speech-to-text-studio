import type { MetricDefinition, MetricValue } from "./types";

const integer = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
const decimal = new Intl.NumberFormat(undefined, {
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
});
function formatCurrency(value: number, currency: string): string {
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency,
      currencyDisplay: "narrowSymbol",
      minimumFractionDigits: 4,
      maximumFractionDigits: 6,
    }).format(value);
  } catch {
    return `${decimal.format(value)} ${currency}`;
  }
}

export function formatMetricValue(
  value: MetricValue | undefined,
  definition?: MetricDefinition,
): string {
  if (value === undefined || value.value === null) return "Not available";
  const numeric =
    typeof value.value === "number"
      ? value.value
      : value.value.trim() !== "" && Number.isFinite(Number(value.value))
        ? Number(value.value)
        : undefined;
  if (numeric === undefined) return String(value.value);

  const unit = value.unit ?? definition?.unit ?? "";
  if (
    unit === "ratio" ||
    (unit === "" && value.metric_id.toLowerCase().endsWith("_ratio"))
  ) {
    return `${decimal.format(numeric * 100)}%`;
  }
  if (unit === "ms") return `${decimal.format(numeric)} ms`;
  if (value.family === "cost" && /^[A-Z]{3}$/.test(unit)) {
    return formatCurrency(numeric, unit);
  }
  if (Number.isInteger(numeric)) {
    return unit ? `${integer.format(numeric)} ${unit}` : integer.format(numeric);
  }
  return unit
    ? `${decimal.format(numeric)} ${unit}`
    : decimal.format(numeric);
}

export function formatCompactDate(value: string | undefined): string {
  if (!value) return "Not available";
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.valueOf())) return value;
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(timestamp);
}

export function formatRunId(value: string): string {
  return value.length > 14 ? `${value.slice(0, 8)}…${value.slice(-5)}` : value;
}

export function formatBytes(value: number | undefined): string {
  if (value === undefined) return "Unknown size";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${decimal.format(value / 1024)} KB`;
  return `${decimal.format(value / (1024 * 1024))} MB`;
}
