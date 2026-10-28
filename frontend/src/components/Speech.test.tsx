import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mapBenchmark, mapReport, mapRunDetail } from "../api/wire";
import { referenceArtifactId } from "../testing/fixtures";
import {
  speechBenchmarkFixture, speechRunFixture, speechReportFixture, speechSolutionIds,
  transcriptComparisonFixture,
} from "../testing/speechFixtures";
import { ConfigureRunWorkspace } from "./WorkspacePages";
import { EvaluationWorkspace } from "./EvaluationWorkspace";
import { isTranscriptComparison } from "./TranscriptComparison";

afterEach(() => vi.unstubAllGlobals());

describe("Speech evidence extension", () => {
  it("shows availability and the 26 sample / 130 task boundary without allowing missing data", () => {
    const benchmark = mapBenchmark({
      ...speechBenchmarkFixture,
      dataset_availability: {
        status: "not-configured", reason: "Publish the external pilot.", sample_count: 26, scored_sample_count: 26,
      },
    });
    render(<ConfigureRunWorkspace
      benchmark={benchmark} solutionIds={speechSolutionIds} retryLimit={3}
      allowNetworked={true} launching={false}
      onSolutionIdsChange={() => undefined} onRetryLimitChange={() => undefined}
      onAllowNetworkedChange={() => undefined} onLaunch={() => undefined}
    />);
    expect(screen.getByText("Dataset: Not configured")).toBeInTheDocument();
    expect(screen.getByText("130")).toBeInTheDocument();
    expect(screen.getByText("26")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /launch/i })).toBeDisabled();
  });

  it("renders canonical audio/alignment and separates partial coverage from cooldown details", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) =>
      new Response(url.endsWith(referenceArtifactId) ? "THE SMALL CAT" : JSON.stringify(transcriptComparisonFixture), {
        headers: { "Content-Type": url.endsWith(referenceArtifactId) ? "text/plain" : "application/json" },
      }),
    ));
    const run = mapRunDetail(speechRunFixture);
    const { container } = render(<EvaluationWorkspace
      benchmark={mapBenchmark(speechBenchmarkFixture)} run={run}
      report={mapReport(speechReportFixture, speechBenchmarkFixture.viewer)}
      eventCount={3} artifactUrl={(sha) => `/api/artifacts/${sha}`}
      onConfigure={() => undefined} onInspect={() => undefined}
    />);
    fireEvent.click(within(screen.getByRole("region", { name: "All results, horizontally scrollable" })).getByRole("button", { name: `Inspect synthetic-1 / ${speechSolutionIds[0]}` }));
    const alignments = await screen.findAllByRole("list", { name: "Normalized transcript alignment" });
    expect(alignments).toHaveLength(1);
    expect(within(alignments[0]).getByText("substitution")).toBeInTheDocument();
    expect(within(alignments[0]).getByText("insertion")).toBeInTheDocument();
    expect(screen.getAllByText(/WER 66.7%/)).toHaveLength(1);
    expect(container.querySelectorAll(".kpi-card")).toHaveLength(5);
    expect(container.querySelector("audio")).toHaveAttribute("controls");
    expect(container.querySelector("audio")).not.toHaveAttribute("autoplay");
    expect(screen.getByText(/Partial or different coverage/)).toBeInTheDocument();
    expect(screen.getAllByText("1/26 partial")).toHaveLength(2);
    fireEvent.click(screen.getByText("Operational details", { selector: "summary" }));
    expect(screen.getByText(/26 samples \/ 130 tasks/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("Task status and attempt history", { selector: "summary" }));
    expect(screen.getByText(/HTTP 429/)).toBeInTheDocument();
    expect(screen.getByText(/delay 9s \(provider-message\)/)).toBeInTheDocument();
    const waiting = run.tasks?.filter((task) => task.wait_state === "cooldown") ?? [];
    expect(waiting.filter((task) => task.attempt === 0)).toHaveLength(24);
    expect(waiting.filter((task) => task.attempt === 1)).toHaveLength(1);
  });

  it("rejects malformed renderer content and preserves explicit empty successes", () => {
    expect(isTranscriptComparison({ ...transcriptComparisonFixture, alignment: ["invalid"] })).toBe(false);
    expect(isTranscriptComparison({ ...transcriptComparisonFixture, word_error_rate: "NaN" })).toBe(false);
    expect(isTranscriptComparison({
      ...transcriptComparisonFixture,
      hypothesis_text: "", hypothesis_tokens: [], substitutions: 0, insertions: 0, deletions: 3,
      word_error_rate: "1", alignment: transcriptComparisonFixture.reference_tokens.map((reference) => ({
        operation: "deletion", reference, hypothesis: null,
      })),
    })).toBe(true);
  });
});
