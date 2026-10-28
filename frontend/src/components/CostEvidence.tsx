import { formatMetricValue } from "../format";
import type { CostCoverage, MetricValue, RunCosts, SampleCost } from "../types";
import "./run-results.css";

function money(amount: string, currency: string): string {
  const number = Number(amount);
  const minimum = 0.000001;
  if (number > 0 && number < minimum) {
    return `<${formatMetricValue({ metric_id: "cost.amount", family: "cost", value: minimum, unit: currency })} ${currency}`;
  }
  return `${formatMetricValue({ metric_id: "cost.amount", family: "cost", value: amount, unit: currency })} ${currency}`;
}

export function coverageAmount(value: CostCoverage): string {
  const entries = Object.entries(value.amounts);
  if (!entries.length) return "Unknown";
  return entries.map(([currency, amount]) => amount === null ? `Unknown ${currency}` : money(amount, currency)).join("; ");
}

export function costCoverageText(value: CostCoverage, estimated = false): string {
  const status = value.status === "known"
    ? estimated ? "Conditional estimate" : "Recorded"
    : value.status === "partial" ? estimated ? "Partial estimate" : "Partial recorded subtotal" : "Unknown";
  return `${status} · ${value.covered_attempts}/${value.total_attempts} recorded attempts · ${value.terminal_tasks}/${value.total_tasks} tasks terminal`;
}

export function costHeadline(costs: RunCosts | undefined, metrics: MetricValue[]): { value: string; detail: string } {
  if (costs) {
    const recorded = costs.recorded_total;
    const estimated = costs.current_estimate_total;
    const hasRecorded = Object.values(recorded.amounts).some(value => value !== null);
    const chosen = hasRecorded ? recorded : estimated;
    const hasAmount = Object.values(chosen.amounts).some(value => value !== null);
    const currencies = Object.keys(chosen.amounts);
    return {
      value: hasAmount ? currencies.length === 1 ? coverageAmount(chosen) : "By currency" : "Unknown",
      detail: hasAmount
        ? costCoverageText(chosen, !hasRecorded)
        : recorded.reasons?.[0] ?? "No supported cost amount; inspect pricing and usage coverage.",
    };
  }
  const values = metrics.filter(metric => metric.family === "cost" && !metric.task_id);
  const known = values.filter(metric => metric.value !== null && metric.value !== "" && Number.isFinite(Number(metric.value)));
  const currencies = new Set(values.map(metric => metric.unit).filter((value): value is string => Boolean(value)));
  if (!known.length) return { value: "Unknown", detail: "Original report has no known cost; pricing or usage evidence is missing." };
  if (currencies.size !== 1) return { value: "By currency", detail: "Mixed currencies cannot be combined into one run cost." };
  const currency = [...currencies][0];
  return {
    value: money(String(known.reduce((sum, value) => sum + Number(value.value), 0)), currency),
    detail: `${known.length === values.length ? "Recorded total" : "Partial recorded subtotal"} · ${known.length}/${values.length} solutions with known cost`,
  };
}

export function CostCell({ cost, fallback }: { cost?: SampleCost; fallback?: MetricValue }) {
  if (!cost) {
    return <div className="cost-cell">
      <span>{fallback && fallback.value !== null && fallback.unit
        ? money(String(fallback.value), fallback.unit) : "Unknown"}</span>
      <small>{fallback?.unit ?? "Currency not recorded"} · Original report; coverage details not supplied</small>
    </div>;
  }
  return <CostSummary recorded={cost.recorded} estimate={cost.current_estimate} />;
}

function CostSummary({ recorded, estimate, estimateLabel = "Est." }: {
  recorded: CostCoverage;
  estimate: CostCoverage;
  estimateLabel?: string;
}) {
  const hasRecorded = Object.values(recorded.amounts).some(amount => amount !== null);
  const hasEstimate = Object.values(estimate.amounts).some(amount => amount !== null);
  return <div className="cost-cell">
    {(hasRecorded || !hasEstimate) && <span>{coverageAmount(recorded)}</span>}
    {hasRecorded && recorded.status === "partial" && <small>Partial recorded subtotal</small>}
    {hasEstimate && <>
      <span className="cost-estimate">{estimateLabel} {coverageAmount(estimate)}</span>
      {estimate.status === "partial" && <small>Partial estimate</small>}
    </>}
    <details className="result-cost-details">
      <summary>Cost details</summary>
      {!hasRecorded && hasEstimate && <p>Recorded cost: {coverageAmount(recorded)}</p>}
      <p>{costCoverageText(recorded)}</p>
      {recorded.reasons?.map(reason => <p key={reason}>{reason}</p>)}
      <p>{costCoverageText(estimate, true)}; not billed cost</p>
      {estimate.reasons?.map(reason => <p key={reason}>{reason}</p>)}
    </details>
  </div>;
}

export function CostAmount({ coverage, currency, estimated = false, fallback }: {
  coverage?: CostCoverage;
  currency: string;
  estimated?: boolean;
  fallback?: MetricValue;
}) {
  if (!coverage) return <span>{!estimated && fallback?.unit && fallback.value !== null && String(fallback.value).trim() !== "" && Number.isFinite(Number(fallback.value))
    ? money(String(fallback.value), fallback.unit) : "Unknown"}</span>;
  return <div className="cost-cell">
    <span>{coverageAmount({ ...coverage, amounts: { [currency]: coverage.amounts[currency] ?? null } })}</span>
    {coverage.status !== "unknown" && <small>{coverage.status === "partial" ? "Partial" : estimated ? "Conditional" : "Recorded"}</small>}
    <details className="result-cost-details"><summary>Coverage</summary>
      <p>{costCoverageText(coverage, estimated)}{estimated ? "; not billed cost" : ""}.</p>
      {coverage.reasons?.map(reason => <p key={reason}>{reason}</p>)}
    </details>
  </div>;
}

export function CostEvidence({ costs, showSamples = true, solutionNames = {} }: { costs?: RunCosts; showSamples?: boolean; solutionNames?: Record<string, string> }) {
  if (!costs) return <p className="cost-note">Cost remains visible when unknown. Detailed pricing and retry coverage were not supplied by this response; no price or zero has been inferred.</p>;
  const nameFor = (id: string) => Object.hasOwn(solutionNames, id) ? solutionNames[id] : id;
  return <div className="cost-evidence">
    <h3>Cost totals and coverage</h3>
    <CostSummary recorded={costs.recorded_total} estimate={costs.current_estimate_total} estimateLabel="Current-rate estimate:" />
    <p className="cost-note">{costs.note}</p>
    <ul className="cost-service-list" aria-label="Per-service costs">
      {costs.solutions.map(solution => <li key={solution.solution_id}><h4>{nameFor(solution.solution_id)}</h4><CostCell cost={solution} /></li>)}
    </ul>
    {showSamples && <details className="cost-samples">
      <summary>Per-example costs</summary>
      {Object.entries(costs.samples).map(([sampleId, solutions]) => <section key={sampleId}>
        <h4>{sampleId}</h4>
        <ul className="cost-service-list">{solutions.map(solution => <li key={solution.solution_id}><strong>{nameFor(solution.solution_id)}</strong><CostCell cost={solution} /></li>)}</ul>
      </section>)}
      {!Object.keys(costs.samples).length && <p>Per-example cost evidence is not available.</p>}
    </details>}
    <details>
      <summary>Pricing basis, sources and retry-inclusive usage</summary>
      {costs.solutions.map(solution => {
        const quote = solution.current_pricing;
        return <section className="cost-source" key={solution.solution_id}>
          <h4><code>{solution.solution_id}</code></h4>
          <CostCell cost={solution} />
          <p>Pinned pricing: <code>{solution.pinned_pricing.id}@{solution.pinned_pricing.version}</code> · {solution.pinned_pricing.currency} · {solution.pinned_pricing.region}</p>
          <p>Effective {solution.pinned_pricing.effective_at.slice(0, 10)}. Recorded attempt pricing: {solution.recorded_pricing_snapshots.join("; ") || "Not recorded"}.</p>
          <p>{solution.pinned_pricing.source}</p>
          {solution.pinned_pricing.rates.length > 0 && <p>{solution.pinned_pricing.rates.map(rate => `${money(rate.price, solution.pinned_pricing.currency)} per ${rate.unit_size} ${rate.dimension}`).join("; ")}</p>}
          <p>Recorded usage across all attempts: {Object.entries(solution.recorded_usage).map(([dimension, value]) => `${value} ${dimension}`).join("; ") || "Not recorded"}.</p>
          {solution.usage_uncertainty.map(reason => <p key={reason}>{reason}</p>)}
          {solution.recorded.reasons?.map(reason => <p key={reason}>{reason}</p>)}
          {solution.current_estimate.reasons?.map(reason => <p key={reason}>{reason}</p>)}
          {quote ? <>
            <p>{quote.basis} Reviewed {quote.checked_at.slice(0, 10)}.</p>
            {quote.pricing ? <>
              <p><code>{quote.pricing.id}@{quote.pricing.version}</code> · {quote.pricing.region} · effective {quote.pricing.effective_at.slice(0, 10)}.</p>
              <p>{quote.pricing.rates.map(rate => `${money(rate.price, quote.pricing!.currency)} per ${rate.unit_size} ${rate.dimension}`).join("; ")}</p>
              <p>{quote.pricing.source}</p>
            </> : <p>{quote.unavailable_reason ?? "No exact reviewed price is available."}</p>}
            {quote.assumptions?.map(assumption => <p key={assumption}>{assumption}</p>)}
            {quote.source_urls?.map(source => URL.canParse(source) && new URL(source).protocol === "https:"
              ? <p key={source}><a href={source} target="_blank" rel="noreferrer">Pricing source: {source}</a></p>
              : <p key={source}>Pricing source (not a web link): {source}</p>)}
          </> : <p>No reviewed current-rate estimate is configured for this benchmark.</p>}
        </section>;
      })}
    </details>
  </div>;
}
