import { ApiError, type ApiClient } from "../api/client";
import type { LaunchRunInput, Run } from "../types";

type KeyFactory = () => string;

function canonicalInput(input: LaunchRunInput): LaunchRunInput {
  return {
    ...input,
    solution_ids: [...new Set(input.solution_ids)].sort(),
  };
}

function fingerprint(input: LaunchRunInput): string {
  return JSON.stringify(canonicalInput(input));
}

export function isAmbiguousLaunchFailure(error: unknown): boolean {
  if (error instanceof TypeError) return true;
  if (!(error instanceof ApiError)) return false;
  return (
    error.status === undefined ||
    error.status === 408 ||
    error.status === 429 ||
    (error.status >= 500 && error.status <= 599)
  );
}

export class IdempotentLaunchLock {
  private retained?: { fingerprint: string; key: string };
  private inFlight?: { fingerprint: string; promise: Promise<Run> };

  constructor(
    private readonly client: ApiClient,
    private readonly keyFactory: KeyFactory = () =>
      client.createOperationKey?.() ?? crypto.randomUUID(),
    private readonly storage?: Storage,
  ) {
    try {
      const value = storage?.getItem("ai-bench.pending-launch");
      if (value) {
        const retained = JSON.parse(value) as { fingerprint: string; key: string };
        if (typeof retained.fingerprint === "string" && typeof retained.key === "string") this.retained = retained;
      }
    } catch { /* A blocked store is reported before any launch when its identity is persisted. */ }
  }

  launch(input: LaunchRunInput, signal?: AbortSignal): Promise<Run> {
    const request = canonicalInput(input);
    const requestFingerprint = fingerprint(request);
    if (this.inFlight?.fingerprint === requestFingerprint) {
      return this.inFlight.promise;
    }

    const key =
      this.retained?.fingerprint === requestFingerprint
        ? this.retained.key
        : this.keyFactory();
    this.retained = { fingerprint: requestFingerprint, key };
    this.storage?.setItem("ai-bench.pending-launch", JSON.stringify(this.retained));

    const promise = this.client.launchRun(request, key, signal);
    this.inFlight = { fingerprint: requestFingerprint, promise };
    void promise.then(
      () => {
        if (this.inFlight?.promise === promise) {
          this.inFlight = undefined;
          this.retained = undefined;
          this.storage?.removeItem("ai-bench.pending-launch");
        }
      },
      (error: unknown) => {
        if (this.inFlight?.promise === promise) this.inFlight = undefined;
        if (!isAmbiguousLaunchFailure(error)) {
          this.retained = undefined;
          this.storage?.removeItem("ai-bench.pending-launch");
        }
      },
    );
    return promise;
  }
}
