import type {
  BenchmarkManifest,
  Dataset,
  DatasetPage,
  EventPage,
  EvaluationRead,
  LaunchRunInput,
  Run,
  RunReport,
  RuntimeInfo,
  UploadRequest,
  UploadSession,
} from "../types";
import { OperationClock } from "../state/operationKey";
import {
  mapBenchmark,
  mapEventPage,
  mapFinalizedEvaluation,
  mapReport,
  mapRunDetail,
  mapRunSummary,
  type BenchmarkEnvelopeDto,
  type CreateRunResponseDto,
  type RunDetailDto,
  type RunEventDto,
  type RunReportDto,
  type RunSummaryDto,
  type ViewerDto,
  type FinalizedEvaluationDto,
  type LiveDetailRequiredDto,
} from "./wire";

export class ApiError extends Error {
  readonly status?: number;
  readonly retryAfter?: string;

  constructor(message: string, status?: number, retryAfter?: string) {
    super(status === 429
      ? `${message} Server is busy. ${retryAfter ? `Retry after ${/^\d+$/.test(retryAfter) ? `${retryAfter} seconds` : retryAfter}.` : "Wait before retrying."} Retry the same operation; no automatic duplicate launch will be made.`
      : message);
    this.name = "ApiError";
    this.status = status;
    this.retryAfter = retryAfter;
  }
}

export interface ApiClient {
  createOperationKey?(): string;
  serverNow?(): number;
  listDatasets?(benchmarkId: string, cursor?: string, signal?: AbortSignal): Promise<DatasetPage>;
  getDataset?(id: string, signal?: AbortSignal): Promise<Dataset>;
  createUpload?(input: UploadRequest, key: string, signal?: AbortSignal): Promise<UploadSession>;
  getUpload?(id: string, signal?: AbortSignal): Promise<UploadSession>;
  uploadPart?(id: string, partId: string, file: File, signal?: AbortSignal): Promise<UploadSession>;
  finalizeUpload?(id: string, key: string, signal?: AbortSignal): Promise<UploadSession>;
  listBenchmarks(signal?: AbortSignal): Promise<BenchmarkManifest[]>;
  listRuns(signal?: AbortSignal): Promise<Run[]>;
  getRuntime(signal?: AbortSignal): Promise<RuntimeInfo>;
  launchRun(
    input: LaunchRunInput,
    idempotencyKey: string,
    signal?: AbortSignal,
  ): Promise<Run>;
  getRun(id: string, signal?: AbortSignal): Promise<Run>;
  getFinalizedEvaluation(id: string, signal?: AbortSignal): Promise<EvaluationRead>;
  getRunEvents(
    id: string,
    after?: number,
    signal?: AbortSignal,
  ): Promise<EventPage>;
  getRunReport(id: string, signal?: AbortSignal): Promise<RunReport>;
  artifactUrl(sha256: string): string;
}

type ItemEnvelope<T> = T[] | { items: T[] };

function unwrapItems<T>(value: ItemEnvelope<T>): T[] {
  return Array.isArray(value) ? value : value.items;
}

export class HttpApiClient implements ApiClient {
  private readonly runViewers = new Map<string, ViewerDto>();
  private readonly clock = new OperationClock();

  constructor(private readonly baseUrl = "") {}

  private async request<T>(
    path: string,
    init: RequestInit = {},
  ): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, {
      ...init,
      headers: {
        Accept: "application/json",
        ...init.headers,
      },
    });
    if (!response.ok) {
      const payload: unknown = await response.json().catch(() => undefined);
      const detail =
        payload !== null && typeof payload === "object" &&
        "detail" in payload && typeof payload.detail === "string"
          ? payload.detail
          : `Request failed with status ${response.status}`;
      throw new ApiError(
        detail,
        response.status,
        response.headers.get("Retry-After") ?? undefined,
      );
    }
    return (await response.json()) as T;
  }

  async listBenchmarks(signal?: AbortSignal): Promise<BenchmarkManifest[]> {
    const values = unwrapItems(
      await this.request<ItemEnvelope<BenchmarkEnvelopeDto>>(
        "/api/benchmarks",
        { signal },
      ),
    );
    return values.map(mapBenchmark);
  }

  async listRuns(signal?: AbortSignal): Promise<Run[]> {
    const values = unwrapItems(
      await this.request<ItemEnvelope<RunSummaryDto>>("/api/runs", { signal }),
    );
    return values.map(mapRunSummary);
  }

  async getRuntime(signal?: AbortSignal): Promise<RuntimeInfo> {
    const runtime = await this.request<RuntimeInfo>("/api/runtime", { signal });
    this.clock.update(runtime);
    return runtime;
  }

  createOperationKey = () => this.clock.createKey();
  serverNow = () => this.clock.now();

  listDatasets(benchmarkId: string, cursor?: string, signal?: AbortSignal): Promise<DatasetPage> {
    const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
    return this.request(`/api/benchmarks/${encodeURIComponent(benchmarkId)}/datasets${query}`, { signal });
  }

  getDataset(id: string, signal?: AbortSignal): Promise<Dataset> {
    return this.request(`/api/datasets/${encodeURIComponent(id)}`, { signal });
  }

  createUpload(input: UploadRequest, key: string, signal?: AbortSignal): Promise<UploadSession> {
    return this.request("/api/dataset-uploads", {
      method: "POST", signal,
      headers: { "Content-Type": "application/json", "Idempotency-Key": key, ...this.clock.intentHeaders() },
      body: JSON.stringify(input),
    });
  }

  getUpload(id: string, signal?: AbortSignal): Promise<UploadSession> {
    return this.request(`/api/dataset-uploads/${encodeURIComponent(id)}`, { signal });
  }

  uploadPart(id: string, partId: string, file: File, signal?: AbortSignal): Promise<UploadSession> {
    return this.request(`/api/dataset-uploads/${encodeURIComponent(id)}/parts/${encodeURIComponent(partId)}`, {
      method: "PUT", signal,
      headers: { "Content-Type": "application/octet-stream", ...this.clock.intentHeaders() },
      body: file,
    });
  }

  finalizeUpload(id: string, key: string, signal?: AbortSignal): Promise<UploadSession> {
    return this.request(`/api/dataset-uploads/${encodeURIComponent(id)}/finalize`, {
      method: "POST", signal,
      headers: { "Content-Type": "application/json", "Idempotency-Key": key, ...this.clock.intentHeaders() },
      body: "{}",
    });
  }

  async launchRun(
    input: LaunchRunInput,
    idempotencyKey: string,
    signal?: AbortSignal,
  ): Promise<Run> {
    const response = await this.request<CreateRunResponseDto>("/api/runs", {
      method: "POST",
      signal,
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
        ...this.clock.intentHeaders(),
      },
      body: JSON.stringify(input),
    });
    return {
      id: response.run_id,
      benchmark_id: input.benchmark_id,
      solution_ids: input.solution_ids,
      status: response.status,
      created_at: new Date().toISOString(),
      retry_limit: input.retry_limit,
      allow_networked: input.allow_networked,
      manifest_artifact_id: response.manifest_artifact_id,
      report_artifact_id: response.report_artifact_id ?? undefined,
    };
  }

  async getRun(id: string, signal?: AbortSignal): Promise<Run> {
    const detail = await this.request<RunDetailDto>(
      `/api/runs/${encodeURIComponent(id)}`,
      { signal },
    );
    this.runViewers.set(id, detail.manifest.viewer);
    return mapRunDetail(detail);
  }

  async getFinalizedEvaluation(id: string, signal?: AbortSignal): Promise<EvaluationRead> {
    const dto = await this.request<FinalizedEvaluationDto | LiveDetailRequiredDto>(
      `/api/runs/${encodeURIComponent(id)}/finalized-evaluation`, { signal, cache: "no-store" },
    );
    if (dto.run?.run_id !== id) throw new ApiError("Evaluation does not match the selected run.");
    if (dto.record_source === "live_detail_required") {
      return { ...dto, run: mapRunSummary(dto.run) };
    }
    if (dto.record_source !== "finalized_report" || dto.manifest.run_id !== id ||
      dto.report.run_id !== id || dto.report.manifest_artifact_id !== dto.run.manifest_artifact_id) {
      throw new ApiError("Finalized evaluation identity is invalid.");
    }
    this.runViewers.set(id, dto.manifest.viewer);
    return mapFinalizedEvaluation(dto);
  }

  async getRunEvents(
    id: string,
    after = 0,
    signal?: AbortSignal,
  ): Promise<EventPage> {
    const events = await this.request<RunEventDto[]>(
      `/api/runs/${encodeURIComponent(id)}/events?after=${after}`,
      { signal },
    );
    return mapEventPage(events);
  }

  async getRunReport(id: string, signal?: AbortSignal): Promise<RunReport> {
    let viewer = this.runViewers.get(id);
    if (!viewer) {
      const detail = await this.request<RunDetailDto>(
        `/api/runs/${encodeURIComponent(id)}`,
        { signal },
      );
      viewer = detail.manifest.viewer;
      this.runViewers.set(id, viewer);
    }
    const report = await this.request<RunReportDto>(
      `/api/runs/${encodeURIComponent(id)}/report`,
      { signal },
    );
    return mapReport(report, viewer);
  }

  artifactUrl(sha256: string): string {
    return `${this.baseUrl}/api/artifacts/${encodeURIComponent(sha256)}`;
  }
}
