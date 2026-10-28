import type { Run } from "../types";

export function ExecutionProgress({ run }: { run: Run }) {
  const tasks = run.tasks ?? [];
  const cooldowns = tasks.filter((task) => task.wait_state === "cooldown").length;
  const retries = tasks.filter((task) => task.wait_state === "retry").length;
  const attempts = tasks.reduce((total, task) => total + (task.attempt ?? 0), 0);
  const terminal = tasks.filter((task) => ["succeeded", "poisoned"].includes(task.status)).length;
  return (
    <div className="execution-progress">
      <p>
        {new Set(tasks.map((task) => task.sample_id)).size} samples / {tasks.length} tasks.
        {" "}{terminal} terminal / {attempts} attempts / {cooldowns} on cooldown / {retries} waiting to retry.
      </p>
      {run.runtime_error && <p className="inline-error" role="alert">{run.runtime_error}</p>}
      <details>
        <summary>Task status and attempt history</summary>
        <ul className="attempt-history" tabIndex={0} aria-label="Task status and attempt history">
          {tasks.map((task) => (
            <li key={task.id}>
              <strong>{task.sample_id} / {task.solution_id}</strong>
              <p>{task.status}{task.wait_state ? ` / ${task.wait_state}` : ""}</p>
              {task.wait_state === "cooldown" && task.cooldown_until && (
                <p>Cooldown until <time dateTime={task.cooldown_until}>{task.cooldown_until}</time>. No attempt is consumed while waiting.</p>
              )}
              {task.wait_state === "retry" && <p>Retry due {task.next_available_at}</p>}
              {task.attempt_history?.map((attempt) => (
                <p key={attempt.attempt_number}>
                  Attempt {attempt.attempt_number}: {attempt.status}
                  {attempt.failure && (
                    <>
                      {" / "}{attempt.failure.classification}
                      {attempt.failure.http_status ? ` / HTTP ${attempt.failure.http_status}` : ""}
                      {attempt.failure.provider_request_id ? ` / request ${attempt.failure.provider_request_id}` : ""}
                      {attempt.failure.retry_delay && (
                        <> / delay {attempt.failure.retry_delay.scheduled_seconds}s ({attempt.failure.retry_delay.source})</>
                      )}
                      {attempt.failure.usage.uncertainty ? ` / ${attempt.failure.usage.uncertainty}` : ""}
                    </>
                  )}
                  {!attempt.failure && attempt.error_message ? ` / ${attempt.error_message}` : ""}
                </p>
              ))}
            </li>
          ))}
        </ul>
      </details>
    </div>
  );
}
