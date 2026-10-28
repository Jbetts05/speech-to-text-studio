import { useId } from "react";
import type { RuntimeInfo, SolutionPipeline } from "../types";
import "./ServiceList.css";

export function serviceCompatibilityReason(solution: SolutionPipeline): string | undefined {
  if (!solution.compatibility) return "Availability has not been confirmed for this benchmark.";
  if (solution.compatibility.level === "unsupported") {
    return solution.compatibility.reason || "This service does not support this benchmark.";
  }
  return undefined;
}

function serviceNote(solution: SolutionPipeline, runtime?: RuntimeInfo): string | undefined {
  const compatibility = serviceCompatibilityReason(solution);
  if (compatibility) return `Unavailable: ${compatibility}`;
  if (!runtime || !["azure", "local"].includes(runtime.runtime)) {
    return "Availability cannot be checked until runtime settings are loaded.";
  }
  if (solution.networked && !runtime.allow_networked) {
    return "Currently unavailable: the operator has disabled live service calls.";
  }
  if (runtime.runtime === "azure" && (!runtime.worker_running || runtime.worker_error)) {
    return "Currently unavailable: the hosted worker is not ready. Refresh after it recovers.";
  }
  if (solution.compatibility?.level === "partial-reference" || !solution.compatibility?.comparable_quality) {
    return solution.compatibility?.reason || "Results cannot be fully compared with this benchmark's references.";
  }
  return undefined;
}

export function ServiceList({
  solutions = [],
  runtime,
  selectedIds,
  onSelectionChange,
}: {
  solutions?: SolutionPipeline[];
  runtime?: RuntimeInfo;
  selectedIds?: string[];
  onSelectionChange?: (ids: string[]) => void;
}) {
  const prefix = useId();
  if (!solutions.length) return <p role="status">No services are available for this benchmark. Refresh the catalog or contact the operator.</p>;
  return (
    <ul className="service-list" aria-label="Services">
      {solutions.map((solution, index) => {
        const selected = selectedIds?.includes(solution.id) ?? false;
        const note = serviceNote(solution, runtime);
        const noteId = `${prefix}-${index}-note`;
        const nameId = `${prefix}-${index}-name`;
        const selecting = selectedIds !== undefined && onSelectionChange !== undefined;
        const content = (
          <>
            {selecting && (
              <input
                type="checkbox"
                checked={selected}
                disabled={Boolean(serviceCompatibilityReason(solution))}
                aria-labelledby={nameId}
                aria-describedby={note ? noteId : undefined}
                onChange={() => onSelectionChange(
                  selected ? selectedIds.filter(id => id !== solution.id) : [...selectedIds, solution.id],
                )}
              />
            )}
            <span className="service-copy">
              <strong id={nameId}>{solution.name}</strong>
              {note && <span className="service-note" id={noteId}>{note}</span>}
            </span>
          </>
        );
        return (
          <li key={solution.id} className={selected ? "service-selected" : undefined}>
            {selecting ? <label className="service-row">{content}</label> : <div className="service-row">{content}</div>}
          </li>
        );
      })}
    </ul>
  );
}
