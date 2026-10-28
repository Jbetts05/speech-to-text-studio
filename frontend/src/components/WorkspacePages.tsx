import { useState, type ReactNode } from "react";
import type {
  BenchmarkManifest,
  LaunchRunInput,
  Run,
  RunReport,
  RuntimeInfo,
  WorkspaceView,
} from "../types";
import { formatCompactDate, formatRunId } from "../format";
import { DatasetStatus } from "./DatasetStatus";
import { RetentionNotice } from "./DatasetWorkspace";
import { launchBlockReason } from "../state/launchPolicy";
import { CostEvidence } from "./CostEvidence";
import { ServiceList } from "./ServiceList";
import {
  ArrowRightIcon,
  ChartIcon,
  CodeIcon,
  FlaskIcon,
  GridIcon,
  HistoryIcon,
  PlayIcon,
  PlusIcon,
  SettingsIcon,
} from "./Icons";

type Navigate = (view: WorkspaceView) => void;

function SenderPolicyNotice({ runtime }: { runtime?: RuntimeInfo }) {
  if (!runtime?.sender_policy && !runtime?.external_call_duplicate_risk) return null;
  return (
    <details className="usage-context">
      <summary>Provider delivery and billing context</summary>
      <p>
      {runtime.sender_policy === "current-generation" && <><strong>One current-generation coordinator. </strong>Opted-in runs share up to five concurrent tasks, one per solution; historical runs remain serial. </>}
      {runtime.external_call_duplicate_risk && (
        <>Rare duplicate or overlapping external calls can occur after crashes, paused-before-send takeovers, or lost responses. Physical non-overlap and exactly-once delivery are not guaranteed.</>
      )}
      </p>
    </details>
  );
}

function ViewHeader({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow?: string;
  title: string;
  description: string;
  actions?: ReactNode;
}) {
  return (
    <header className="view-header">
      <div>
        {eyebrow && <span className="section-kicker">{eyebrow}</span>}
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {actions && <div className="view-actions">{actions}</div>}
    </header>
  );
}

export function HomeWorkspace({
  benchmarks,
  benchmark,
  run,
  runs,
  onNavigate,
}: {
  benchmarks: BenchmarkManifest[];
  benchmark: BenchmarkManifest;
  run?: Run;
  runs: Run[];
  onNavigate: Navigate;
}) {
  const scopedRuns = runs.filter((item) => item.benchmark_id === benchmark.id);
  const comparableSolutions = benchmark.pipelines?.filter(
    (solution) => solution.compatibility?.comparable_quality,
  ).length;
  const completedTasks = run?.tasks?.filter(
    (task) => task.status === "succeeded" || task.status === "poisoned",
  ).length;

  return (
    <div className="view-shell home-view">
      <header className="home-header">
        <div>
          <h1>AI Bench</h1>
          <p>
            Compare AI models and services against known references, with quality,
            cost, latency, usage, and reliability kept as separate evidence.
          </p>
        </div>
        <div className="home-header-actions">
          <button
            className="secondary-button"
            type="button"
            onClick={() => onNavigate("benchmarks")}
          >
            <GridIcon />
            Explore benchmarks
          </button>
          <button
            className="primary-button"
            type="button"
            onClick={() => onNavigate("configure")}
          >
            <PlayIcon />
            Configure run
          </button>
        </div>
      </header>

      <section className="home-summary" aria-label="Workspace overview">
        <div>
          <span>Benchmarks</span>
          <strong>{benchmarks.length}</strong>
          <small>Installed benchmark packs</small>
        </div>
        <div>
          <span>Current samples</span>
          <strong>{benchmark.sample_count}</strong>
          <small>{benchmark.input_modalities?.join(", ")} inputs</small>
        </div>
        <div>
          <span>Quality-comparable</span>
          <strong>{comparableSolutions ?? 0}</strong>
          <small>{benchmark.pipelines?.length ?? 0} runnable configurations</small>
        </div>
        <div>
          <span>Recorded runs</span>
          <strong>{scopedRuns.length}</strong>
          <small>For {benchmark.name}</small>
        </div>
      </section>

      <div className="home-grid">
        <section className="home-panel" aria-labelledby="home-benchmark-heading">
          <div className="home-panel-heading">
            <div>
              <h2 id="home-benchmark-heading">Current benchmark</h2>
              <p>Every run pins its definition, evaluator, viewer, and solution contracts.</p>
            </div>
            <span className="stable-label">Version {benchmark.version}</span>
          </div>
          <h3>{benchmark.name}</h3>
          <p className="home-panel-description">{benchmark.description}</p>
          <DatasetStatus benchmark={benchmark} />
          <dl className="home-facts">
            <div>
              <dt>Inputs</dt>
              <dd>{benchmark.input_modalities?.join(", ")}</dd>
            </div>
            <div>
              <dt>References</dt>
              <dd>{benchmark.reference_modalities?.join(", ")}</dd>
            </div>
            <div>
              <dt>Metrics</dt>
              <dd>{benchmark.metrics.length}</dd>
            </div>
            <div>
              <dt>Viewer</dt>
              <dd>{benchmark.viewer?.title ?? "Download"}</dd>
            </div>
          </dl>
          <div className="home-panel-actions">
            <button
              className="secondary-button"
              type="button"
              onClick={() => onNavigate("benchmarks")}
            >
              View benchmark
            </button>
            <button
              className="text-button"
              type="button"
              onClick={() => onNavigate("create")}
            >
              <PlusIcon />
              Create benchmark
            </button>
          </div>
        </section>

        <section className="home-panel" aria-labelledby="home-run-heading">
          <div className="home-panel-heading">
            <div>
              <h2 id="home-run-heading">
                {run ? "Continue current run" : "Start a comparison"}
              </h2>
              <p>
                {run
                  ? "Open the recorded evidence without rebuilding the run."
                  : "Choose configurations, then pin the immutable run contract."}
              </p>
            </div>
            {run && (
              <span className={`runs-status status-${run.status}`}>
                <span aria-hidden="true" />
                {run.status}
              </span>
            )}
          </div>
          {run ? (
            <dl className="home-facts home-run-facts">
              <div>
                <dt>Progress</dt>
                <dd>
                  {completedTasks ?? 0}/{run.tasks?.length ?? 0} tasks
                </dd>
              </div>
              <div>
                <dt>Samples</dt>
                <dd>{new Set(run.tasks?.map((task) => task.sample_id)).size}</dd>
              </div>
              <div>
                <dt>Configurations</dt>
                <dd>{run.solution_ids.length}</dd>
              </div>
              <div>
                <dt>Networked</dt>
                <dd>{run.allow_networked ? "Allowed" : "Disabled"}</dd>
              </div>
            </dl>
          ) : (
            <div className="home-run-empty">
              <PlayIcon />
              <strong>No run selected for {benchmark.name}</strong>
              <p>Existing immutable runs remain available in run history.</p>
            </div>
          )}
          <div className="home-panel-actions">
            <button
              className="primary-button"
              type="button"
              onClick={() => onNavigate(run ? "evaluation" : "configure")}
            >
              {run ? "Open evaluation" : "Configure run"}
            </button>
            <button
              className="text-button"
              type="button"
              onClick={() => onNavigate("runs")}
            >
              View run history
            </button>
          </div>
        </section>
      </div>
    </div>
  );
}

export function BenchmarksWorkspace({
  benchmarks,
  selectedId,
  onSelect,
  onNavigate,
  datasetControls,
}: {
  benchmarks: BenchmarkManifest[];
  selectedId: string;
  onSelect: (id: string) => void;
  onNavigate: Navigate;
  datasetControls?: ReactNode;
}) {
  const benchmark = benchmarks.find((item) => item.id === selectedId);
  return (
    <div className="view-shell">
      <ViewHeader
        eyebrow="Benchmark packs"
        title="Benchmarks"
        description="Inspect versioned benchmark contracts before launching comparable work."
        actions={
          <button
            className="primary-button"
            type="button"
            onClick={() => onNavigate("create")}
          >
            <PlusIcon />
            Create benchmark
          </button>
        }
      />
      {datasetControls}
      <div className="catalog-layout">
        <section className="catalog-list" aria-label="Installed benchmarks">
          {benchmarks.map((item) => (
            <button
              key={item.id}
              type="button"
              className={item.id === selectedId ? "active" : ""}
              onClick={() => onSelect(item.id)}
            >
              <span className="catalog-monogram">{item.name.slice(0, 2)}</span>
              <span>
                <strong>{item.name}</strong>
                <small>
                  {item.sample_count} samples · {item.pipelines?.length ?? 0} solutions
                </small>
              </span>
              <ArrowRightIcon />
            </button>
          ))}
        </section>
        {benchmark && (
          <section className="detail-panel" aria-labelledby="benchmark-detail-title">
            <span className="stable-label">Version {benchmark.version}</span>
            <h2 id="benchmark-detail-title">{benchmark.name}</h2>
            <p>{benchmark.description}</p>
            <DatasetStatus benchmark={benchmark} />
            <dl className="detail-facts">
              <div>
                <dt>Identifier</dt>
                <dd><code>{benchmark.id}</code></dd>
              </div>
              <div>
                <dt>Input modalities</dt>
                <dd>{benchmark.input_modalities?.join(", ")}</dd>
              </div>
              <div>
                <dt>Reference modalities</dt>
                <dd>{benchmark.reference_modalities?.join(", ")}</dd>
              </div>
              <div>
                <dt>Renderer</dt>
                <dd>{benchmark.viewer?.title}</dd>
              </div>
            </dl>
            <div className="metric-definition-list">
              <h3>Evaluation contract</h3>
              {benchmark.metrics.map((metric) => (
                <div key={metric.id}>
                  <span className={`metric-family family-${metric.family}`}>
                    {metric.family}
                  </span>
                  <strong>{metric.name}</strong>
                  <p>{metric.description}</p>
                </div>
              ))}
            </div>
            <button
              className="primary-button"
              type="button"
              onClick={() => onNavigate("configure")}
            >
              <PlayIcon />
              Configure run
            </button>
          </section>
        )}
      </div>
    </div>
  );
}

export function CreateBenchmarkWorkspace({
  onNavigate,
}: {
  onNavigate: Navigate;
}) {
  const [name, setName] = useState("Speech transcription");
  const [slug, setSlug] = useState("speech_transcription");
  const [modality, setModality] = useState("audio");
  const [copied, setCopied] = useState(false);
  const command = `ai-bench scaffold ${slug || "<benchmark_id>"} --brief BENCHMARK.md`;

  const copy = async () => {
    await navigator.clipboard.writeText(command);
    setCopied(true);
  };

  return (
    <div className="view-shell">
      <ViewHeader
        eyebrow="Pack scaffold"
        title="Create benchmark"
        description="Generate a benchmark pack at the plugin boundary without modifying platform core."
      />
      <div className="builder-layout">
        <form className="builder-form" onSubmit={(event) => event.preventDefault()}>
          <label>
            <span>Benchmark name</span>
            <input
              value={name}
              onChange={(event) => {
                setName(event.target.value);
                setSlug(
                  event.target.value
                    .trim()
                    .toLowerCase()
                    .replace(/[^a-z0-9]+/g, "_")
                    .replace(/^_|_$/g, ""),
                );
              }}
            />
          </label>
          <label>
            <span>Benchmark identifier</span>
            <input
              className="code-input"
              value={slug}
              pattern="[a-z][a-z0-9_]*"
              onChange={(event) => setSlug(event.target.value)}
            />
          </label>
          <label>
            <span>Input modality</span>
            <select value={modality} onChange={(event) => setModality(event.target.value)}>
              <option value="audio">Audio</option>
              <option value="document">Document</option>
              <option value="image">Image</option>
              <option value="structured">Structured</option>
              <option value="text">Text</option>
              <option value="video">Video</option>
            </select>
          </label>
          <div className="command-preview" aria-live="polite">
            <CodeIcon />
            <code>{command}</code>
          </div>
          <div className="builder-actions">
            <button type="button" className="primary-button" onClick={() => void copy()}>
              {copied ? "Copied" : "Copy scaffold command"}
            </button>
            <button
              type="button"
              className="secondary-button"
              onClick={() => onNavigate("benchmarks")}
            >
              Back to benchmarks
            </button>
          </div>
        </form>
        <aside className="builder-guidance">
          <span className="section-kicker">Generated contract</span>
          <h2>{name || "New benchmark"}</h2>
          <ol>
            <li>Fill in and validate <code>BENCHMARK.md</code>.</li>
            <li>Run the scaffold command from the repository root.</li>
            <li>Implement only the generated dataset, solution, evaluator, and viewer files.</li>
            <li>Run the offline contract and end-to-end gates before deployment.</li>
          </ol>
          <p>
            Set the brief input modality to <strong>{modality}</strong>. The generated
            pack inherits the AI Bench visual system; visual overrides are opt-in and
            must not replace the platform shell.
          </p>
        </aside>
      </div>
    </div>
  );
}

export function SolutionsWorkspace({
  benchmark,
  onNavigate,
  runtime,
}: {
  benchmark: BenchmarkManifest;
  onNavigate: Navigate;
  runtime?: RuntimeInfo;
}) {
  return (
    <div className="view-shell">
      <ViewHeader
        eyebrow={benchmark.name}
        title="Solutions"
        description="Services for this benchmark."
        actions={
          <button
            className="primary-button"
            type="button"
            onClick={() => onNavigate("configure")}
          >
            <PlayIcon />
            Configure run
          </button>
        }
      />
      <ServiceList solutions={benchmark.pipelines} runtime={runtime} />
    </div>
  );
}

export function ConfigureRunWorkspace({
  benchmark,
  solutionIds,
  retryLimit,
  allowNetworked,
  launching,
  error,
  onSolutionIdsChange,
  onRetryLimitChange,
  onAllowNetworkedChange,
  onLaunch,
  datasetControls,
  datasetBlocked = false,
  datasetBlockReason,
  onRetryDataset,
  runtimeLoading = false,
  maxAttempts = 20,
  networkAvailable = true,
  runtime,
}: {
  benchmark: BenchmarkManifest;
  solutionIds: string[];
  retryLimit: number;
  allowNetworked: boolean;
  launching: boolean;
  error?: string;
  onSolutionIdsChange: (ids: string[]) => void;
  onRetryLimitChange: (value: number) => void;
  onAllowNetworkedChange: (value: boolean) => void;
  onLaunch: () => void;
  datasetControls?: ReactNode;
  datasetBlocked?: boolean;
  datasetBlockReason?: string;
  onRetryDataset?: () => void;
  runtimeLoading?: boolean;
  maxAttempts?: number;
  networkAvailable?: boolean;
  runtime?: RuntimeInfo;
}) {
  const networkedSelected = benchmark.pipelines?.some(
    (solution) => solutionIds.includes(solution.id) && solution.networked,
  );
  const blockedReason = launchBlockReason({
    benchmark, solutionIds, retryLimit, allowNetworked, launching, datasetBlocked,
    datasetBlockReason, maxAttempts, networkAvailable, runtimeLoading, runtime,
  });
  const hosted = runtime?.runtime === "azure";

  return (
    <div className="view-shell">
      <ViewHeader
        eyebrow={benchmark.name}
        title="Configure run"
        description="Choose services and attempt limits, then launch on the selected dataset."
      />
      <DatasetStatus benchmark={benchmark} />
      {datasetControls}
      <div className="configure-layout">
        <section className="configuration-panel" aria-labelledby="solution-selection-heading">
          <div className="section-title-row">
            <div>
              <span className="section-kicker">Step 1</span>
              <h2 id="solution-selection-heading">Select services</h2>
            </div>
            <span>{solutionIds.length} selected</span>
          </div>
          <div className="solution-options">
            <ServiceList
              solutions={benchmark.pipelines}
              runtime={runtime}
              selectedIds={solutionIds}
              onSelectionChange={onSolutionIdsChange}
            />
          </div>
        </section>

        <aside className="run-contract-panel" aria-labelledby="run-contract-heading">
          <span className="section-kicker">Step 2</span>
          <h2 id="run-contract-heading">Run settings</h2>
          <label>
            <span>Retry limit</span>
            <input
              type="number"
              min={1}
              max={maxAttempts}
              step={1}
              value={retryLimit}
              onChange={(event) => onRetryLimitChange(Number(event.target.value))}
            />
          </label>
          <p>At most {maxAttempts} total attempts per task, including the initial attempt.</p>
          <SenderPolicyNotice runtime={runtime} />
          {!hosted && <label className="network-policy">
            <input
              type="checkbox"
              checked={allowNetworked}
              onChange={(event) => onAllowNetworkedChange(event.target.checked)}
            />
            <span>
              <strong>Allow networked execution</strong>
              <small>Required only for selected provider-backed solutions.</small>
            </span>
          </label>}
          {hosted && networkedSelected && (
            <p className="service-usage-note">Launch authorizes live service calls. Usage and retries may incur charges; estimates are not bills.</p>
          )}
          <dl className="contract-summary">
            <div>
              <dt>Samples</dt>
              <dd>{benchmark.scored_sample_count}</dd>
            </div>
            <div>
              <dt>Benchmark</dt>
              <dd>{benchmark.id}@{benchmark.version}</dd>
            </div>
            <div>
              <dt>Tasks</dt>
              <dd>{benchmark.scored_sample_count * solutionIds.length}</dd>
            </div>
            <div>
              <dt>Viewer</dt>
              <dd>{benchmark.viewer?.id}</dd>
            </div>
          </dl>
          {error && <p className="inline-error" role="alert">{error}</p>}
          <p id="launch-availability" role="status" aria-live="polite">
            {blockedReason ?? "Ready to launch the selected immutable dataset."}
          </p>
          {onRetryDataset && (
            <button type="button" className="secondary-button" onClick={onRetryDataset}>Retry selected dataset</button>
          )}
          <button
            type="button"
            className="primary-button launch-button"
            disabled={Boolean(blockedReason)}
            aria-describedby="launch-availability"
            onClick={onLaunch}
          >
            <PlayIcon />
            {launching ? "Launching…" : "Launch benchmark"}
          </button>
        </aside>
      </div>
    </div>
  );
}

export function RunsWorkspace({
  runs,
  activeRunId,
  onSelect,
  onRefresh,
}: {
  runs: Run[];
  activeRunId?: string;
  onSelect: (run: Run) => void;
  onRefresh: () => void;
}) {
  return (
    <div className="view-shell">
      <ViewHeader
        eyebrow="Immutable history"
        title="Runs"
        description="Restore any run by URL and inspect the exact manifest and evidence it produced."
        actions={
          <button className="secondary-button" type="button" onClick={onRefresh}>
            Refresh
          </button>
        }
      />
      <div className="standard-table-scroll" role="region" aria-label="Run history">
        <table className="standard-table">
          <thead>
            <tr>
              <th scope="col">Run</th>
              <th scope="col">Benchmark</th>
              <th scope="col">Status</th>
              <th scope="col">Solutions</th>
              <th scope="col">Created</th>
              <th scope="col"><span className="sr-only">Action</span></th>
            </tr>
          </thead>
          <tbody>
            {runs.map((item) => (
              <tr key={item.id} className={activeRunId === item.id ? "selected-row" : ""}>
                <td><code title={item.id}>{formatRunId(item.id)}</code></td>
                <td>{item.benchmark_id}</td>
                <td>
                  <span className={`runs-status status-${item.status}`}>
                    <span aria-hidden="true" />
                    {item.status}
                  </span>
                </td>
                <td>{item.solution_ids.length || "—"}</td>
                <td>{formatCompactDate(item.created_at)}<RetentionNotice {...item} /></td>
                <td>
                  <button className="text-button" type="button" onClick={() => onSelect(item)}>
                    Open evaluation
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function ReportsWorkspace({
  run,
  report,
  solutionNames,
  onNavigate,
}: {
  run?: Run;
  report?: RunReport;
  solutionNames?: Record<string, string>;
  onNavigate: Navigate;
}) {
  if (!run) {
    return (
      <div className="view-shell">
        <ViewHeader
          eyebrow="Evidence exports"
          title="Reports"
          description="Select a completed run before opening its immutable JSON or CSV report."
        />
        <div className="empty-workspace">
          <ChartIcon />
          <h2>No run selected</h2>
          <button className="primary-button" type="button" onClick={() => onNavigate("runs")}>
            Browse runs
          </button>
        </div>
      </div>
    );
  }
  const reportAvailable =
    Boolean(run.report_artifact_id) &&
    ["completed", "completed-with-failures", "failed"].includes(run.status);

  return (
    <div className="view-shell">
      <ViewHeader
        eyebrow={`Run ${formatRunId(run.id)}`}
        title="Reports"
        description="Download report formats that preserve independent metric families and evidence links."
        actions={
          <button className="secondary-button" type="button" onClick={() => onNavigate("evaluation")}>
            Open evaluation
          </button>
        }
      />
      {reportAvailable ? (
        <section className="report-downloads">
          <article>
            <CodeIcon />
            <div>
              <h2>JSON evidence report</h2>
              <p>Structured metrics, provenance, immutable artifacts, and compatibility.</p>
            </div>
            <a className="primary-button" href={`/api/runs/${run.id}/report?format=json`} download>
              Download JSON
            </a>
          </article>
          <article>
            <GridIcon />
            <div>
              <h2>CSV result export</h2>
              <p>One result row per sample and solution for downstream analysis.</p>
            </div>
            <a className="secondary-button" href={`/api/runs/${run.id}/report?format=csv`} download>
              Download CSV
            </a>
          </article>
        </section>
      ) : (
        <section className="report-pending" role="status" aria-live="polite">
          <h2>Report pending</h2>
          <p>
            Exports become available after the run reaches a terminal state and
            its immutable report is persisted.
          </p>
        </section>
      )}
      <dl className="report-facts">
        <div><dt>Status</dt><dd>{report?.status ?? run.status}</dd></div>
        <div><dt>Metric families</dt><dd>{report?.families.length ?? 0}</dd></div>
        <div><dt>Evidence records</dt><dd>{report?.evidence.length ?? 0}</dd></div>
        <div><dt>Generated</dt><dd>{formatCompactDate(report?.generated_at)}</dd></div>
      </dl>
      <section className="evaluation-section" aria-label="Report cost evidence">
        <CostEvidence costs={run.costs} solutionNames={solutionNames} />
      </section>
    </div>
  );
}

export function AdminWorkspace({
  runtime,
  benchmark,
}: {
  runtime?: RuntimeInfo;
  benchmark: BenchmarkManifest;
}) {
  return (
    <div className="view-shell">
      <ViewHeader
        eyebrow="Read-only status"
        title="Admin"
        description="Inspect runtime and contract boundaries. Deployment changes remain an infrastructure workflow."
      />
      <section className="admin-sections">
        <article>
          <div className="admin-heading">
            <SettingsIcon />
            <div><h2>Runtime</h2><p>Current API execution contract.</p></div>
          </div>
          <dl className="detail-facts">
            <div><dt>Environment</dt><dd>{runtime?.runtime ?? "Loading"}</dd></div>
            <div><dt>Backend</dt><dd>{runtime?.execution_backend ?? "Loading"}</dd></div>
            <div><dt>Horizontal scaling</dt><dd>{runtime?.horizontal_scaling ? "Enabled" : "Disabled"}</dd></div>
            <div><dt>Persistent state</dt><dd>{runtime?.persistent_state_required ? "Required" : "Optional"}</dd></div>
          </dl>
          <SenderPolicyNotice runtime={runtime} />
        </article>
        <article>
          <div className="admin-heading">
            <FlaskIcon />
            <div><h2>Active contract</h2><p>Resolved from the installed benchmark pack.</p></div>
          </div>
          <dl className="detail-facts">
            <div><dt>Benchmark</dt><dd>{benchmark.id}@{benchmark.version}</dd></div>
            <div><dt>Solutions</dt><dd>{benchmark.pipelines?.length ?? 0}</dd></div>
            <div><dt>Metrics</dt><dd>{benchmark.metrics.length}</dd></div>
            <div><dt>Viewer</dt><dd>{benchmark.viewer?.id}@{benchmark.viewer?.version}</dd></div>
          </dl>
        </article>
      </section>
    </div>
  );
}

export function runLaunchInput(
  benchmark: BenchmarkManifest,
  solutionIds: string[],
  retryLimit: number,
  allowNetworked: boolean,
  datasetId?: string,
): LaunchRunInput {
  return {
    benchmark_id: benchmark.id,
    solution_ids: solutionIds,
    retry_limit: retryLimit,
    allow_networked: allowNetworked,
    ...(datasetId ? { dataset_id: datasetId } : {}),
  };
}
