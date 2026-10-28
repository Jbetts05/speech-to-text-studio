import type { BenchmarkManifest } from "../types";

export function DatasetStatus({ benchmark }: { benchmark: BenchmarkManifest }) {
  const availability = benchmark.dataset_availability;
  if (!availability) return null;
  const label = {
    ready: "Ready",
    "not-configured": "Not configured",
    invalid: "Invalid",
  }[availability.status];
  return (
    <div className="dataset-status">
      <strong>Dataset: {label}</strong>
      <p>{availability.reason}</p>
      {availability.revision && (
        <p>
          Revision <code>{availability.revision.id}</code>
        </p>
      )}
    </div>
  );
}
