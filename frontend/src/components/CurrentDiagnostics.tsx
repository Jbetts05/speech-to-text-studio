import { useEffect, useState } from "react";
import { ApiError } from "../api/client";
import type { Run } from "../types";
import { ExecutionProgress } from "./ExecutionProgress";

export function CurrentDiagnostics({ runId, load, onUnavailable }: {
  runId: string;
  load: (id: string, signal: AbortSignal) => Promise<Run>;
  onUnavailable: (id: string, reason: "expired" | "missing") => void;
}) {
  const [request, setRequest] = useState(0);
  const [diagnostics, setDiagnostics] = useState<Run>();
  const [error, setError] = useState<string>();
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    if (!request) return;
    let cancelled = false;
    const controller = new AbortController();
    setLoading(true);
    setError(undefined);
    setDiagnostics(undefined);
    void load(runId, controller.signal).then(value => {
      if (cancelled) return;
      if (value.id !== runId) throw new Error("Diagnostics do not match the selected run.");
      setDiagnostics(value);
    }).catch((reason: unknown) => {
      if (cancelled) return;
      if (reason instanceof ApiError && [404, 410].includes(reason.status ?? 0)) {
        onUnavailable(runId, reason.status === 410 ? "expired" : "missing");
        return;
      }
      setError(reason instanceof Error ? reason.message : "Current diagnostics are unavailable.");
    }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; controller.abort(); };
  }, [load, onUnavailable, request, runId]);
  return <section aria-label="Current operational diagnostics">
    <p>Current task state is loaded separately. It does not replace the recorded evaluation.</p>
    {!request && <button type="button" className="secondary-button" onClick={() => setRequest(1)}>Load current diagnostics</button>}
    {loading && <p role="status">Loading current diagnostics...</p>}
    {error && <><p className="inline-error" role="alert">Current diagnostics unavailable: {error}</p>
      <button type="button" className="secondary-button" onClick={() => setRequest(value => value + 1)}>Retry current diagnostics</button></>}
    {diagnostics && <><p className="cost-note">Current operational snapshot, separate from historical results.</p><ExecutionProgress run={diagnostics} /></>}
  </section>;
}
