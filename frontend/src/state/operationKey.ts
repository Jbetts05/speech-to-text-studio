import type { RuntimeInfo } from "../types";

export class OperationClock {
  private serverEpoch?: number;
  private receivedAt = 0;
  private publicDemo = false;

  update(runtime: RuntimeInfo) {
    this.publicDemo = runtime.public_demo === true;
    const epoch = Date.parse(runtime.server_time ?? "");
    this.serverEpoch = Number.isFinite(epoch) ? epoch : undefined;
    this.receivedAt = performance.now();
  }

  now(): number {
    return this.serverEpoch === undefined
      ? Date.now()
      : this.serverEpoch + performance.now() - this.receivedAt;
  }

  createKey(): string {
    if (!this.publicDemo) return crypto.randomUUID();
    if (this.serverEpoch === undefined) {
      throw new Error("Server time is unavailable. Refresh before starting a public operation.");
    }
    return `v1.${Math.floor(this.now() / 1000)}.${crypto.randomUUID()}`;
  }

  intentHeaders(): Record<string, string> {
    return this.publicDemo ? { "X-AI-Bench-Intent": "public-demo" } : {};
  }
}
