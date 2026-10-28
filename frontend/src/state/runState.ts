import type {
  Evidence,
  MetricDefinition,
  MetricValue,
  Run,
  RunEvent,
  RunStatus,
  TaskRun,
  TaskStatus,
} from "../types";

const terminalRunStatuses = new Set<RunStatus>([
  "completed",
  "completed-with-failures",
  "failed",
]);
const terminalTaskStatuses = new Set<TaskStatus>([
  "succeeded",
  "poisoned",
]);

export interface RunViewState {
  generation: number;
  eventsRequest: number;
  run?: Run;
  tasks: Record<string, TaskRun>;
  metrics: MetricValue[];
  evidence: Evidence[];
  events: RunEvent[];
  cursor?: string;
}

export type RunViewAction =
  | { type: "select"; generation: number }
  | { type: "snapshot"; generation: number; run: Run }
  | {
      type: "events";
      generation: number;
      request: number;
      events: RunEvent[];
      cursor?: string;
    };

export const initialRunViewState: RunViewState = {
  generation: 0,
  eventsRequest: 0,
  tasks: {},
  metrics: [],
  evidence: [],
  events: [],
};

function preserveTerminalRunStatus(
  current: RunStatus | undefined,
  incoming: RunStatus,
): RunStatus {
  return current && terminalRunStatuses.has(current) ? current : incoming;
}

function mergeTask(
  current: TaskRun | undefined,
  incoming: TaskRun,
): TaskRun {
  if (current && terminalTaskStatuses.has(current.status)) {
    return current;
  }
  return { ...current, ...incoming };
}

function mergeById<T extends { id: string }>(
  current: T[],
  incoming: T[],
): T[] {
  const values = new Map(current.map((item) => [item.id, item]));
  for (const item of incoming) {
    values.set(item.id, { ...values.get(item.id), ...item });
  }
  return [...values.values()];
}

function mergeMetrics(
  current: MetricValue[],
  incoming: MetricValue[],
): MetricValue[] {
  const key = (metric: MetricValue) =>
    [
      metric.metric_id,
      metric.task_id ?? "",
      metric.solution_id ?? "",
    ].join(":");
  const values = new Map(current.map((item) => [key(item), item]));
  for (const item of incoming) values.set(key(item), item);
  return [...values.values()];
}

function mergeDefinitions(
  current: MetricDefinition[] = [],
  incoming: MetricDefinition[] = [],
): MetricDefinition[] {
  const definitions = new Map(
    current.map((definition) => [
      `${definition.family}:${definition.id}`,
      definition,
    ]),
  );
  for (const definition of incoming) {
    const key = `${definition.family}:${definition.id}`;
    definitions.set(key, { ...definitions.get(key), ...definition });
  }
  return [...definitions.values()];
}

function applyEvent(state: RunViewState, event: RunEvent): RunViewState {
  let next = state;
  if (event.run_status && state.run) {
    next = {
      ...next,
      run: {
        ...state.run,
        status: preserveTerminalRunStatus(state.run.status, event.run_status),
        updated_at: event.occurred_at,
      },
    };
  }
  if (event.task) {
    next = {
      ...next,
      tasks: {
        ...next.tasks,
        [event.task.id]: mergeTask(next.tasks[event.task.id], event.task),
      },
    };
  }
  if (event.metric) {
    next = { ...next, metrics: mergeMetrics(next.metrics, [event.metric]) };
  }
  if (event.evidence) {
    next = { ...next, evidence: mergeById(next.evidence, [event.evidence]) };
  }
  return next;
}

export function runViewReducer(
  state: RunViewState,
  action: RunViewAction,
): RunViewState {
  if (action.type === "select") {
    return {
      ...initialRunViewState,
      generation: action.generation,
    };
  }
  if (action.generation !== state.generation) return state;

  if (action.type === "snapshot") {
    const incomingTasks = action.run.tasks ?? [];
    const tasks = { ...state.tasks };
    for (const task of incomingTasks) {
      tasks[task.id] = mergeTask(tasks[task.id], task);
    }
    const status = preserveTerminalRunStatus(
      state.run?.status,
      action.run.status,
    );
    return {
      ...state,
      run: {
        ...state.run,
        ...action.run,
        status,
        metric_definitions: mergeDefinitions(
          state.run?.metric_definitions,
          action.run.metric_definitions,
        ),
      },
      tasks,
      metrics: mergeMetrics(state.metrics, action.run.metrics ?? []),
      evidence: mergeById(state.evidence, action.run.evidence ?? []),
    };
  }

  if (action.request < state.eventsRequest) return state;
  const knownEvents = new Set(state.events.map((event) => event.id));
  const freshEvents = action.events.filter((event) => {
    if (knownEvents.has(event.id)) return false;
    knownEvents.add(event.id);
    return true;
  });
  const merged = freshEvents.reduce(applyEvent, state);
  return {
    ...merged,
    eventsRequest: action.request,
    events: [...state.events, ...freshEvents],
    cursor: action.cursor ?? state.cursor,
  };
}

export function isTerminalRun(status: RunStatus | undefined): boolean {
  return status ? terminalRunStatuses.has(status) : false;
}
