import type { RunStatus, TaskStatus } from "../types";

type Status = RunStatus | TaskStatus;

const statusDetails: Record<Status, { symbol: string; label: string }> = {
  created: { symbol: "◷", label: "Created" },
  pending: { symbol: "○", label: "Pending" },
  running: { symbol: "●", label: "Running" },
  leased: { symbol: "●", label: "Leased" },
  completed: { symbol: "✓", label: "Completed" },
  "completed-with-failures": {
    symbol: "!",
    label: "Completed with failures",
  },
  succeeded: { symbol: "✓", label: "Succeeded" },
  failed: { symbol: "×", label: "Failed" },
  poisoned: { symbol: "!", label: "Poisoned" },
};

export function StatusCue({
  status,
  live = false,
}: {
  status: Status;
  live?: boolean;
}) {
  const detail = statusDetails[status];
  return (
    <span
      className={`status-cue status-${status.replaceAll("-", "_")}${live ? " is-live" : ""}`}
      aria-label={detail.label}
    >
      <span className="status-symbol" aria-hidden="true">
        {detail.symbol}
      </span>
      <span>{detail.label}</span>
    </span>
  );
}
