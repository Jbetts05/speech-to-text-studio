import type { Run, TaskRun } from "../types";
import { StatusCue } from "./StatusCue";

function shortDate(value?: string): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "—"
    : new Intl.DateTimeFormat(undefined, {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      }).format(date);
}

export function RunRail({
  run,
  tasks,
  eventCount,
}: {
  run?: Run;
  tasks: TaskRun[];
  eventCount: number;
}) {
  if (!run) {
    return (
      <section className="run-rail empty-rail" aria-label="Run summary">
        <p>Select a run to inspect evidence.</p>
      </section>
    );
  }
  const complete = tasks.filter((task) =>
    ["succeeded", "poisoned"].includes(task.status),
  ).length;

  return (
    <section className="run-rail" aria-label="Run summary">
      <div className="rail-primary">
        <span className="rail-label">Status</span>
        <StatusCue status={run.status} live={run.status === "running"} />
      </div>
      <div>
        <span className="rail-label">Task progress</span>
        <strong>
          {complete} / {tasks.length || "—"}
        </strong>
      </div>
      <div>
        <span className="rail-label">Solutions</span>
        <strong>{run.solution_ids.length}</strong>
      </div>
      <div>
        <span className="rail-label">Events</span>
        <strong>{eventCount}</strong>
      </div>
      <div>
        <span className="rail-label">Last update</span>
        <strong>{shortDate(run.updated_at ?? run.created_at)}</strong>
      </div>
    </section>
  );
}
