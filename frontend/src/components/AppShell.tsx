import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import type {
  BenchmarkManifest,
  Run,
  RuntimeInfo,
  WorkspaceView,
} from "../types";
import { formatCompactDate, formatRunId } from "../format";
import {
  ChartIcon,
  FlaskIcon,
  GridIcon,
  HistoryIcon,
  HomeIcon,
  MenuIcon,
  PlayIcon,
  PlusIcon,
  SettingsIcon,
  XIcon,
} from "./Icons";

const views: Record<
  WorkspaceView,
  { label: string; group: string; icon: typeof HomeIcon }
> = {
  home: { label: "Home", group: "Overview", icon: HomeIcon },
  benchmarks: { label: "Benchmarks", group: "Benchmarks", icon: GridIcon },
  create: { label: "Create benchmark", group: "Benchmarks", icon: PlusIcon },
  solutions: { label: "Solutions", group: "Solutions", icon: FlaskIcon },
  configure: { label: "Configure run", group: "Solutions", icon: PlayIcon },
  evaluation: {
    label: "Evaluation detail",
    group: "Evaluations",
    icon: GridIcon,
  },
  runs: { label: "Runs", group: "Evaluations", icon: HistoryIcon },
  reports: { label: "Reports", group: "Insights", icon: ChartIcon },
  admin: { label: "Admin", group: "Administration", icon: SettingsIcon },
};

const orderedGroups = [
  "Overview",
  "Benchmarks",
  "Solutions",
  "Evaluations",
  "Insights",
  "Administration",
];

function RecentRuns({
  runs,
  activeRunId,
  onSelect,
  onRefresh,
  variant,
}: {
  runs: Run[];
  activeRunId?: string;
  onSelect: (run: Run) => void;
  onRefresh: () => void;
  variant: "rail" | "navigation";
}) {
  return (
    <section
      className={`recent-runs recent-runs-${variant}`}
      aria-label={
        variant === "rail"
          ? "Recent runs for current benchmark"
          : "Recent runs in navigation"
      }
    >
      <header>
        <span className="nav-kicker">
          Recent runs
        </span>
        <button type="button" onClick={onRefresh}>
          Refresh
        </button>
      </header>
      {runs.length === 0 ? (
        <p className="recent-runs-empty">No runs for this benchmark yet.</p>
      ) : (
        <ol>
          {runs.slice(0, 8).map((item) => {
            const active = item.id === activeRunId;
            return (
              <li key={item.id}>
                <button
                  type="button"
                  className={active ? "active" : ""}
                  aria-current={active ? "true" : undefined}
                  aria-label={`Open run ${item.id}`}
                  onClick={() => onSelect(item)}
                >
                  <span className={`recent-run-status status-${item.status}`}>
                    <span aria-hidden="true" />
                    {item.status}
                  </span>
                  <code title={item.id}>{formatRunId(item.id)}</code>
                  <span className="recent-run-detail">
                    <strong>{item.solution_ids.length || "—"}</strong>
                    <span>solutions</span>
                    <time dateTime={item.created_at}>
                      {formatCompactDate(item.created_at)}
                    </time>
                  </span>
                </button>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}

export function AppShell({
  benchmarks,
  selectedBenchmark,
  benchmarkSelectionId,
  runScopeBenchmarkId,
  run,
  runs,
  runtime,
  view,
  onNavigate,
  onBenchmarkChange,
  onRunSelect,
  onRefreshRuns,
  children,
}: {
  benchmarks: BenchmarkManifest[];
  selectedBenchmark?: BenchmarkManifest;
  benchmarkSelectionId?: string;
  runScopeBenchmarkId?: string;
  run?: Run;
  runs: Run[];
  runtime?: RuntimeInfo;
  view: WorkspaceView;
  onNavigate: (view: WorkspaceView) => void;
  onBenchmarkChange: (benchmarkId: string) => void;
  onRunSelect: (run: Run) => void;
  onRefreshRuns: () => void;
  children: ReactNode;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const drawerRef = useRef<HTMLElement>(null);
  const menuButtonRef = useRef<HTMLButtonElement>(null);
  const closeMenu = useCallback((restoreFocus = true) => {
    setMenuOpen(false);
    if (restoreFocus) {
      window.setTimeout(() => menuButtonRef.current?.focus(), 0);
    }
  }, []);

  useEffect(() => {
    if (!menuOpen) return;
    drawerRef.current?.querySelector<HTMLElement>("button, select")?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        closeMenu();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = Array.from(
        drawerRef.current?.querySelectorAll<HTMLElement>(
          'button:not([disabled]), select:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
        ) ?? [],
      );
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [closeMenu, menuOpen]);

  useEffect(() => {
    if (!menuOpen) return;
    const onResize = () => {
      if (window.innerWidth > 900) closeMenu(false);
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [closeMenu, menuOpen]);

  const scopedBenchmarkId = runScopeBenchmarkId ?? selectedBenchmark?.id;
  const scopedRuns = scopedBenchmarkId
    ? runs.filter((item) => item.benchmark_id === scopedBenchmarkId)
    : runs;
  const benchmarkLabel =
    selectedBenchmark?.name ?? runScopeBenchmarkId ?? "No benchmark";
  const navigate = (next: WorkspaceView) => {
    onNavigate(next);
    closeMenu(menuOpen);
  };

  const navigation = (
    <div className="navigation-scroll">
      <div className="mobile-drawer-heading">
        <strong>AI Bench</strong>
        <button
          type="button"
          className="icon-button"
          aria-label="Close navigation"
          onClick={() => closeMenu()}
        >
          <XIcon />
        </button>
      </div>
      <label className="mobile-benchmark-select">
        <span>Benchmark</span>
        <select
          value={benchmarkSelectionId ?? ""}
          disabled={benchmarks.length === 0}
          onChange={(event) => onBenchmarkChange(event.target.value)}
        >
          {!benchmarkSelectionId && (
            <option value="" disabled>{run ? "Saved run context" : "Catalog unavailable"}</option>
          )}
          {benchmarks.map((benchmark) => (
            <option value={benchmark.id} key={benchmark.id}>
              {benchmark.name}
            </option>
          ))}
        </select>
      </label>
      {orderedGroups.map((group) => (
        <section className="nav-section" key={group}>
          <div className="nav-section-heading">{group}</div>
          <div className="workspace-nav">
            {(Object.entries(views) as [WorkspaceView, (typeof views)[WorkspaceView]][])
              .filter(([, item]) => item.group === group)
              .map(([id, item]) => {
                const Icon = item.icon;
                return (
                  <button
                    type="button"
                    className={view === id ? "active" : ""}
                    aria-current={view === id ? "page" : undefined}
                    onClick={() => navigate(id)}
                    key={id}
                  >
                    <Icon />
                    <span>{item.label}</span>
                  </button>
                );
              })}
          </div>
        </section>
      ))}
      <RecentRuns
        runs={scopedRuns}
        activeRunId={run?.id}
        variant="navigation"
        onSelect={(item) => {
          onRunSelect(item);
          closeMenu(menuOpen);
        }}
        onRefresh={onRefreshRuns}
      />
    </div>
  );

  return (
    <div className="app-frame">
      <header className="topbar">
        <button
          ref={menuButtonRef}
          type="button"
          className="icon-button mobile-menu"
          aria-label={menuOpen ? "Close navigation" : "Open navigation"}
          aria-expanded={menuOpen}
          onClick={() => (menuOpen ? closeMenu() : setMenuOpen(true))}
        >
          <MenuIcon />
        </button>
        <button type="button" className="brand" onClick={() => navigate("home")}>
          <span className="brand-symbol">
            <FlaskIcon />
          </span>
          <span>
            <strong>AI Bench</strong>
            <small>Evidence-first evaluation</small>
          </span>
        </button>
        <div className="topbar-context" aria-label="Current location">
          <span>Benchmarks</span>
          <span aria-hidden="true">/</span>
          <strong>{benchmarkLabel}</strong>
          <span aria-hidden="true">/</span>
          <span>{views[view].label}</span>
        </div>
        <span className="topbar-mobile-project">
          {benchmarkLabel}
        </span>
        <div className="topbar-actions">
          <span className="offline-status">
            <span aria-hidden="true" />
            {!runtime || !["azure", "local"].includes(runtime.runtime)
              ? "Runtime unavailable"
              : runtime.runtime === "azure"
              ? "Azure · Entra"
              : run?.allow_networked
                ? "Local · networked"
                : "Local · offline"}
          </span>
        </div>
      </header>

      <nav
        ref={drawerRef}
        className={menuOpen ? "navigation mobile-open" : "navigation"}
        aria-label="Primary navigation"
      >
        {navigation}
      </nav>
      {menuOpen && (
        <button
          type="button"
          className="mobile-backdrop"
          aria-label="Close navigation"
          onClick={() => closeMenu()}
        />
      )}

      <aside className="context-rail" aria-label="Benchmark context">
        <div className="context-rail-scroll">
          <label className="rail-benchmark-select">
            <span className="nav-kicker">Current benchmark</span>
            <select
              value={benchmarkSelectionId ?? ""}
              disabled={benchmarks.length === 0}
              onChange={(event) => onBenchmarkChange(event.target.value)}
            >
              {!benchmarkSelectionId && (
                <option value="" disabled>{run ? "Saved run context" : "Catalog unavailable"}</option>
              )}
              {benchmarks.map((benchmark) => (
                <option value={benchmark.id} key={benchmark.id}>
                  {benchmark.name}
                </option>
              ))}
            </select>
          </label>
          {selectedBenchmark && (
            <>
              <section className="rail-section">
                <span className="nav-kicker">Contract</span>
                <dl className="rail-facts">
                  <div>
                    <dt>Version</dt>
                    <dd>{selectedBenchmark.version}</dd>
                  </div>
                  <div>
                    <dt>Samples</dt>
                    <dd>{selectedBenchmark.sample_count}</dd>
                  </div>
                  <div>
                    <dt>Input</dt>
                    <dd>{selectedBenchmark.input_modalities?.join(", ")}</dd>
                  </div>
                  <div>
                    <dt>Viewer</dt>
                    <dd>{selectedBenchmark.viewer?.title}</dd>
                  </div>
                </dl>
              </section>
              <section className="rail-section">
                <span className="nav-kicker">Evidence contract</span>
                <p className="rail-note">
                  Quality, cost, latency, usage, and reliability remain separate.
                </p>
              </section>
            </>
          )}
          {run && (
            <section className="rail-section">
              <span className="nav-kicker">Current run</span>
              <button
                type="button"
                className="rail-run-link"
                onClick={() => onNavigate("evaluation")}
              >
                <code>{formatRunId(run.id)}</code>
                <span className={`runs-status status-${run.status}`}>
                  <span aria-hidden="true" />
                  {run.status}
                </span>
              </button>
            </section>
          )}
          <RecentRuns
            runs={scopedRuns}
            activeRunId={run?.id}
            variant="rail"
            onSelect={onRunSelect}
            onRefresh={onRefreshRuns}
          />
        </div>
      </aside>

      <main className="workspace">{children}</main>
    </div>
  );
}
