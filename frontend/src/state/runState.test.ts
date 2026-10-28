import { describe, expect, it } from "vitest";
import type { Run, RunEvent } from "../types";
import {
  initialRunViewState,
  runViewReducer,
} from "./runState";

const baseRun: Run = {
  id: "run-a",
  benchmark_id: "bench-a",
  solution_ids: ["solution-a"],
  status: "running",
  created_at: "2026-01-01T00:00:00Z",
  tasks: [
    {
      id: "task-run-a",
      task_id: "task-a",
      status: "leased",
    },
  ],
};

describe("runViewReducer", () => {
  it("ignores stale responses from a prior run generation", () => {
    const selected = runViewReducer(initialRunViewState, {
      type: "select",
      generation: 4,
    });
    const result = runViewReducer(selected, {
      type: "snapshot",
      generation: 3,
      run: baseRun,
    });

    expect(result).toBe(selected);
    expect(result.run).toBeUndefined();
  });

  it("never regresses terminal run or task states", () => {
    const selected = runViewReducer(initialRunViewState, {
      type: "select",
      generation: 1,
    });
    const terminal = runViewReducer(selected, {
      type: "snapshot",
      generation: 1,
      run: {
        ...baseRun,
        status: "failed",
        tasks: [{ ...baseRun.tasks![0], status: "poisoned" }],
      },
    });
    const regressive = runViewReducer(terminal, {
      type: "snapshot",
      generation: 1,
      run: baseRun,
    });
    const event: RunEvent = {
      id: "event-regressive",
      sequence: 1,
      run_id: baseRun.id,
      type: "task.status",
      occurred_at: "2026-01-01T00:01:00Z",
      run_status: "running",
      task: baseRun.tasks![0],
    };
    const result = runViewReducer(regressive, {
      type: "events",
      generation: 1,
      request: 1,
      events: [event],
    });

    expect(result.run?.status).toBe("failed");
    expect(result.tasks["task-run-a"].status).toBe("poisoned");
  });

  it("deduplicates events and rejects out-of-order event pages", () => {
    const selected = runViewReducer(initialRunViewState, {
      type: "select",
      generation: 2,
    });
    const event: RunEvent = {
      id: "event-a",
      sequence: 2,
      run_id: "run-a",
      type: "message",
      message: "started",
      occurred_at: "2026-01-01T00:00:00Z",
    };
    const latest = runViewReducer(selected, {
      type: "events",
      generation: 2,
      request: 4,
      events: [event, event],
      cursor: "4",
    });
    const stale = runViewReducer(latest, {
      type: "events",
      generation: 2,
      request: 3,
      events: [{ ...event, id: "old-event" }],
      cursor: "3",
    });

    expect(latest.events).toHaveLength(1);
    expect(stale).toBe(latest);
    expect(stale.cursor).toBe("4");
  });

  it("retains aggregate metric definitions across later snapshots", () => {
    const selected = runViewReducer(initialRunViewState, {
      type: "select",
      generation: 5,
    });
    const reportState = runViewReducer(selected, {
      type: "snapshot",
      generation: 5,
      run: {
        ...baseRun,
        metric_definitions: [
          {
            id: "reliability.success_ratio",
            name: "Success ratio",
            family: "reliability",
          },
        ],
      },
    });
    const refreshed = runViewReducer(reportState, {
      type: "snapshot",
      generation: 5,
      run: {
        ...baseRun,
        metric_definitions: [
          {
            id: "exact_accuracy",
            name: "Exact accuracy",
            family: "quality",
          },
        ],
      },
    });

    expect(refreshed.run?.metric_definitions?.map(({ id }) => id)).toEqual([
      "reliability.success_ratio",
      "exact_accuracy",
    ]);
  });
});
