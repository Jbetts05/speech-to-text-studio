import { useRef, type KeyboardEvent } from "react";
import { metricFamilies, type MetricFamily } from "../types";

interface MetricTabsProps {
  active: MetricFamily;
  onChange: (family: MetricFamily) => void;
}

const labels: Record<MetricFamily, string> = {
  quality: "Quality",
  cost: "Cost",
  latency: "Latency",
  usage: "Usage",
  reliability: "Reliability",
};

export function MetricTabs({ active, onChange }: MetricTabsProps) {
  const refs = useRef<Array<HTMLButtonElement | null>>([]);

  const move = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let next: number | undefined;
    if (event.key === "ArrowRight") next = (index + 1) % metricFamilies.length;
    if (event.key === "ArrowLeft")
      next = (index - 1 + metricFamilies.length) % metricFamilies.length;
    if (event.key === "Home") next = 0;
    if (event.key === "End") next = metricFamilies.length - 1;
    if (next === undefined) return;
    event.preventDefault();
    const family = metricFamilies[next];
    onChange(family);
    refs.current[next]?.focus();
  };

  return (
    <nav className="metric-navigation" aria-label="Report metric families">
      <div className="metric-tabs" role="tablist" aria-label="Metric family">
        {metricFamilies.map((family, index) => (
          <button
            className="metric-tab"
            id={`tab-${family}`}
            key={family}
            ref={(node) => {
              refs.current[index] = node;
            }}
            type="button"
            role="tab"
            aria-selected={active === family}
            aria-controls={`panel-${family}`}
            tabIndex={active === family ? 0 : -1}
            onClick={() => onChange(family)}
            onKeyDown={(event) => move(event, index)}
          >
            {labels[family]}
          </button>
        ))}
      </div>
    </nav>
  );
}
